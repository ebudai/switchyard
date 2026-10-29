#!/usr/bin/env python3
"""SYRD-697: clearing a wait tells the ticket's owner, a dropped notice is not a delivery, and an owner's own write
confirms a hand-over its replaced provider was sent.

On pie (PIE-17) Ops asked Audit for a review and went idle. Audit reviewed, said "Clearing the wait." and cleared it.
Nothing reached Ops' pane for an hour:

- clearing a wait notified nobody, and a role's comment notifies the owner only when it is urgent;
- the wait's 30-minute escalation, dropped by the listener because the wait was gone, was acked like a delivery, and
  the board stall alert counted it as "awaiting_role notice delivered to director": durable progress, which restarted
  the ticket's stall clock;
- the ticket's hand-over, sent to an Ops provider since replaced, read "unconfirmed: sent_to_retired_runtime" a day
  later, while Ops had been working the ticket all along.

On production-built boards (router_role_board_test's: schema.sql, the real migrations, rbac.sql, the HTTP server and
the real listener, only the panes stood in for), main first from a git archive, then this tree.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "bf564449d59d3e8f4e79d66acf69269158bb6a74"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, upgrade_from: Path | None = None) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from board_stall_test import example, short
    from router_role_board_test import Board, temporary_cluster

    out: dict = {}

    def to(board, sent, role: str) -> list[str]:
        return [m for t, m in sent if t == f"pgu-{role}:0.0"]

    def deliver(board) -> list:
        """One listener pass, every pane idle: what reached which pane."""
        board.busy.clear()
        return board.one_pass()

    def progress(board, ticket: str) -> list[str]:
        return json.loads(board.sql("SELECT coalesce(json_agg(event ORDER BY id), '[]')::text "
                                    f"FROM ticket_board.board_progress_events WHERE ticket_id = '{ticket}';"))

    def traces(board, ticket: str, kind: str) -> list[list]:
        return json.loads(board.sql("SELECT coalesce(json_agg(json_build_array(target_role, event, busy_reason) ORDER BY id), "
                                    f"'[]')::text FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' "
                                    f"AND kind = '{kind}' AND event IN ('send', 'drop', 'ack');"))

    def make_due(board, ticket: str) -> None:
        board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                  f"WHERE ticket_id = '{ticket}' AND claimed_at IS NULL;")

    def ticket(board, number: int, owner: str) -> str:
        """Routed from Analysis to its owner, the hand-over delivered."""
        from router_role_board_test import fixture

        t = f"PGU-{number}"
        fixture.seed_postgres_ticket(board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
        out.setdefault("routes", {})[t] = short(board.act(t, "route", "director", state="in_progress", assignee=owner))
        out.setdefault("handed_over", {})[t] = len(to(board, deliver(board), owner))
        return t

    def delivery(board, t: str) -> dict:
        return board.ticket(t).get("active_work_delivery") or {}

    def workflow() -> dict:
        """The example workflow, its Inspection sign-off a declared flag of its own: no ticket column records it."""
        cfg = example()
        cfg["flags"]["inspection_passed"] = cfg["flags"].pop("inspector_signoff")
        for stage in cfg["stages"]:
            if stage.get("signoff") == "inspector_signoff":
                stage["signoff"] = "inspection_passed"
        for transition in cfg["transitions"]:
            transition["clear_signoffs"] = [("inspection_passed" if f == "inspector_signoff" else f)
                                            for f in transition.get("clear_signoffs", [])]
        return cfg

    with temporary_cluster(prefix="syrd697-pg-") as cluster:
        board = Board(cluster, "pie17", workflow(), before=upgrade_from)
        try:
            # -- PIE-17: Ops asks Audit for a review, goes idle; Audit reviews and clears the wait ------------------
            pie = ticket(board, 17, "ops")
            # Ops' provider is replaced after the hand-over was typed into the old one.
            board.sql("UPDATE ticket_board.role_runtime_assignments SET registered_at = clock_timestamp() "
                      "WHERE role = 'ops';")
            out["delivery_replaced"] = delivery(board, pie)
            out["reviewer_note"] = short(board.act(pie, "add_comment", "audit", text="I will look once it is asked."))
            out["delivery_after_reviewer_write"] = delivery(board, pie)
            out["request"] = short(board.act(pie, "request_dependency", "ops", role="audit",
                                             reason="Please review the image build on the ticket."))
            out["delivery_after_owner_write"] = delivery(board, pie)
            sent = deliver(board)
            out["to_audit"] = to(board, sent, "audit")
            out["review"] = short(board.act(pie, "add_comment", "audit", text="The image clears. Clearing the wait."))
            out["clear"] = short(board.act(pie, "clear_awaiting_role", "audit"))
            out["awaiting_after_clear"] = board.sql(f"SELECT awaiting_role FROM ticket_board.ticket_notification_state "
                                                    f"WHERE ticket_id = '{pie}';")
            sent = deliver(board)
            out["to_ops_after_clear"] = to(board, sent, "ops")
            out["others_after_clear"] = [m for t, m in sent if t != "pgu-ops:0.0"]
            # -- the wait's later notices fall due: 5 and 15 minutes to Audit, 30 to the Director -----------------
            mark = int(board.sql("SELECT coalesce(max(id), 0) FROM ticket_board.board_progress_events;"))
            make_due(board, pie)
            out["late"] = deliver(board)
            out["late_progress"] = json.loads(board.sql(
                "SELECT coalesce(json_agg(event ORDER BY id), '[]')::text FROM ticket_board.board_progress_events "
                f"WHERE ticket_id = '{pie}' AND id > {mark};"))
            out["awaiting_traces"] = traces(board, pie, "awaiting_role")
            out["progress"] = progress(board, pie)
            # -- the Director's comment, as at 19:20, still reaches Ops ----------------------------------------------
            out["director_comment"] = short(board.act(pie, "add_comment", "director", text="Ops, the review is in."))
            out["to_ops_after_director"] = to(board, deliver(board), "ops")
            out["delivery_end"] = delivery(board, pie)

            # -- the owner clears its own wait: nothing to tell it ----------------------------------------------------
            # Its owner wrote on it before the hand-over, then its provider is replaced: that write confirms nothing.
            from router_role_board_test import fixture

            own = "PGU-18"
            fixture.seed_postgres_ticket(board.admin, own, title=f"Fixture {own}", state="analysis", assignee="director")
            out["own_early_note"] = short(board.act(own, "add_comment", "app", text="I can take this one."))
            out.setdefault("routes", {})[own] = short(board.act(own, "route", "director", state="in_progress",
                                                                assignee="app"))
            out.setdefault("handed_over", {})[own] = len(to(board, deliver(board), "app"))
            board.sql("UPDATE ticket_board.role_runtime_assignments SET registered_at = clock_timestamp() "
                      "WHERE role = 'app';")
            out["delivery_own_early"] = delivery(board, own)
            out["own_request"] = short(board.act(own, "request_dependency", "app", role="audit", reason="Review?"))
            deliver(board)
            out["own_clear"] = short(board.act(own, "clear_awaiting_role", "app"))
            out["to_app_after_own_clear"] = to(board, deliver(board), "app")
            out["delivery_own_after_request"] = delivery(board, own)

            # -- the Director clears another role's wait; clearing again, with no wait, says nothing ----------------
            other = ticket(board, 19, "main")
            out["other_request"] = short(board.act(other, "request_dependency", "main", role="audit", reason="Review?"))
            deliver(board)
            out["director_clear"] = short(board.act(other, "clear_awaiting_role", "director"))
            out["to_main_after_director_clear"] = to(board, deliver(board), "main")
            out["director_clear_again"] = short(board.act(other, "clear_awaiting_role", "director"))
            out["to_main_after_clear_again"] = to(board, deliver(board), "main")

            # -- a held review: once its owner has approved, the wait is the next stage's (SYRD-22, SYRD-180) ---------
            # PGU-20: Audit approves a held ticket; the approval is deferred and the Director is awaited. PGU-21: held,
            # not yet approved, Audit asks the Director a question. The Director clears each wait. PGU-22: the
            # Inspector approves a held ticket, its sign-off in no column, and Audit, awaited, clears the wait.
            for number, stage, owner, approve, clearer in ((20, "audit", "audit", "audit_sign_off", "director"),
                                                           (21, "audit", "audit", None, "director"),
                                                           (22, "inspection", "inspector", "inspector_sign_off", "audit")):
                held = f"PGU-{number}"
                fixture.seed_postgres_ticket(board.admin, held, title=f"Fixture {held}", state=stage, assignee=owner)
                board.app.update_ticket(held, {"manually_controlled": True}, caller_role="director")
                waited = (board.act(held, approve, owner, text="Approved.") if approve else
                          board.act(held, "request_dependency", owner, role="director", reason="Which image?"))
                out.setdefault("held_wait", {})[held] = [short(waited), *board.where(held), board.sql(
                    f"SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = '{held}';")]
                deliver(board)
                out.setdefault("held_clear", {})[held] = short(board.act(held, "clear_awaiting_role", clearer))
                out.setdefault("to_owner_after_held_clear", {})[held] = [
                    m for m in to(board, deliver(board), owner) if m.startswith(held)]
                out.setdefault("held_flags", {})[held] = board.sql(
                    "SELECT inspector_signoff || '|' || audit_signoff || '|' || coalesce(workflow_flags->>'inspection_passed', '') "
                    f"FROM ticket_board.tickets WHERE id = '{held}';")

            out["tickets"] = {"pie": pie, "own": own, "other": other}
            if upgrade_from is not None:
                out["migrations"] = board.sql("SELECT count(*) FROM ticket_board.schema_migrations "
                                              "WHERE name LIKE 'pgu1019_syrd697%';")
        finally:
            board.close()
    return out


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, upgrade_from: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL",
                                                                     "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree),
                            *([str(upgrade_from)] if upgrade_from else [])],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=500,
                           stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


CLEARED = "the wait on audit was cleared"
RETIRED = "sent_to_retired_runtime"


def common(got: dict) -> str:
    """What main and this tree agree on: the scenario ran as PIE-17 did."""
    t = got["tickets"]
    pie = t["pie"]
    check(set(got["routes"].values()) == {"ok"} and got["handed_over"] == {pie: 1, t["own"]: 1, t["other"]: 1},
          f"each ticket is routed to its owner and the hand-over reaches its pane: {got['routes']} {got['handed_over']}")
    check(got["delivery_replaced"].get("state") == "unconfirmed" and got["delivery_replaced"].get("reason") == RETIRED,
          f"Ops' provider replaced after the hand-over: sent to the retired runtime: {got['delivery_replaced']}")
    check(got["delivery_after_reviewer_write"] == got["delivery_replaced"],
          f"a reviewer's comment confirms nothing about the owner: {got['delivery_after_reviewer_write']}")
    check(got["delivery_own_early"].get("state") == "unconfirmed" and got["delivery_own_early"].get("reason") == RETIRED,
          f"nor does the owner's own comment from before the hand-over: {got['delivery_own_early']}")
    check(all(got[k] == "ok" for k in ("request", "reviewer_note", "review", "clear", "director_comment", "own_request",
                                       "own_clear", "other_request", "director_clear", "director_clear_again")),
          f"every write is accepted: {[(k, v) for k, v in got.items() if isinstance(v, str) and v.startswith('refused')]}")
    check(len(got["to_audit"]) == 1 and "ops is awaiting audit. New handoff." in got["to_audit"][0],
          f"Ops' review request reaches Audit: {got['to_audit']}")
    check(got["awaiting_after_clear"] == "", f"Audit's clear clears the wait: {got['awaiting_after_clear']!r}")
    check(got["late"] == [] and [e for e in got["awaiting_traces"] if e[1] == "drop"]
          == [["audit", "drop", "stale_notification"]] * 2 + [["director", "drop", "stale_notification"]],
          f"the wait's later notices are dropped, nothing typed anywhere: {got['late']} {got['awaiting_traces']}")
    check(len(got["to_ops_after_director"]) == 1 and "changed by director: new comment" in got["to_ops_after_director"][0],
          f"the Director's comment reaches Ops, as on pie at 19:20: {got['to_ops_after_director']}")
    check(got["to_app_after_own_clear"] == [], f"an owner clearing its own wait is told nothing: {got['to_app_after_own_clear']}")
    check(got["to_main_after_clear_again"] == [],
          f"clearing a ticket with no wait tells nobody: {got['to_main_after_clear_again']}")
    check(got["held_wait"] == {"PGU-20": ["ok", "audit", "audit", "director"], "PGU-21": ["ok", "audit", "audit", "director"],
                               "PGU-22": ["ok", "inspection", "inspector", "audit"]}
          and got["held_clear"] == {"PGU-20": "ok", "PGU-21": "ok", "PGU-22": "ok"},
          f"each held ticket stays where it is, its owner's, awaiting the next stage's owner or the Director, and the "
          f"wait is cleared: {got['held_wait']} {got['held_clear']}")
    check(got["held_flags"]["PGU-22"] == "false|false|true",
          f"the Inspector's approval is recorded only in its declared flag: {got['held_flags']}")
    check(got["to_owner_after_held_clear"]["PGU-20"] == [] and got["to_owner_after_held_clear"]["PGU-22"] == [],
          f"a held review its owner approved: clearing its wait tells the owner nothing: {got['to_owner_after_held_clear']}")
    return pie


def before(got: dict) -> None:
    pie = common(got)
    check(got["to_ops_after_clear"] == [] and got["others_after_clear"] == [],
          f"main: Audit clears the wait and nothing reaches Ops: {got['to_ops_after_clear']}")
    check(got["late_progress"] == [f"{pie} awaiting_role notice delivered to audit"] * 2
          + [f"{pie} awaiting_role notice delivered to director"],
          f"main: each dropped notice is recorded as a delivery, progress: {got['late_progress']}")
    check(got["to_main_after_director_clear"] == [] and got["to_owner_after_held_clear"]["PGU-21"] == [],
          f"main: the Director clearing another role's wait tells its owner nothing: {got['to_main_after_director_clear']} "
          f"{got['to_owner_after_held_clear']}")
    check(got["delivery_after_owner_write"] == got["delivery_replaced"] == got["delivery_end"],
          f"main: through Ops' own review request and to the end, sent to the retired runtime: {got['delivery_end']}")
    check(got["delivery_own_after_request"].get("reason") == RETIRED,
          f"main: likewise for App's: {got['delivery_own_after_request']}")


def candidate(got: dict) -> None:
    pie = common(got)
    check(got["to_ops_after_clear"] == [f"{pie} -- Fixture {pie} (in in_progress, that you own) was changed by audit: "
                                        f"{CLEARED} -- re-read it before continuing."] and got["others_after_clear"] == [],
          f"Audit clears the wait and Ops is told, once, by whom and what: {got['to_ops_after_clear']}")
    check(got["late_progress"] == [], f"a dropped notice is not a delivery, and is not progress: {got['late_progress']}")
    progress = got["progress"]
    check(progress.count(f"{pie} awaiting_role notice delivered to audit") == 1
          and progress.index(f"{pie} awaiting_role notice delivered to audit") < progress.index(f"{pie} dependency cleared")
          and progress[-1] == f"{pie} dependency cleared" and f"{pie} transition notice delivered to ops" in progress,
          f"the notices actually sent still are: the hand-over and the review request: {progress}")
    other = got["tickets"]["other"]
    check(len(got["to_main_after_director_clear"]) == 1
          and f"was changed by director: {CLEARED}" in got["to_main_after_director_clear"][0]
          and got["to_main_after_director_clear"][0].startswith(other),
          f"the Director clearing another role's wait tells its owner: {got['to_main_after_director_clear']}")
    check(got["to_owner_after_held_clear"]["PGU-21"] == ["PGU-21 -- Fixture PGU-21 (in audit, that you own) was changed "
                                                         "by director: the wait on director was cleared -- re-read it "
                                                         "before continuing."],
          f"held, but not yet signed off: the review is still its owner's, and it is told: {got['to_owner_after_held_clear']}")
    for key in ("delivery_after_owner_write", "delivery_end"):
        d = got[key]
        check(set(d) == {"state", "at", "confirmed_by", "confirmed_at"} and d["state"] == "delivered"
              and d["at"] == got["delivery_replaced"]["at"] and d["confirmed_by"] == "owner_comment"
              and d["confirmed_at"] > d["at"],
              f"{key}: Ops' own review request confirms it has the work: {d}")
    check(got["delivery_after_owner_write"] == got["delivery_end"],
          f"confirmed by the first such write, and stays so: {got['delivery_end']}")
    d = got["delivery_own_after_request"]
    check(d.get("state") == "delivered" and d.get("confirmed_by") == "owner_comment",
          f"App's own request after the hand-over confirms it, where its earlier note did not: {d}")
    if "migrations" in got:
        check(got["migrations"] == "1", f"main's board takes this tree's migration once: {got['migrations']}")


def mirrors() -> None:
    """schema.sql's last definition of each of pgu1019's functions is pgu1019's, byte for byte, so fresh and upgraded
    boards agree. A later ticket's functions may follow it."""
    migration = (ROOT / "scripts/ticket_board/migrations/pgu1019_syrd697_wait_cleared_owner_notice.sql").read_text()
    body = migration.split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;\n", 1)[0]
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    at = schema.rfind(body)
    heads = ("CREATE OR REPLACE FUNCTION ticket_board.clear_awaiting_role(",
             "CREATE OR REPLACE FUNCTION ticket_board.board_stall_delivery_progress(")
    check(body.startswith(heads[0]) and at >= 0 and all(at <= schema.rfind(h) < at + len(body) for h in heads),
          "schema.sql's last clear_awaiting_role and board_stall_delivery_progress are pgu1019's, byte for byte")


def main() -> int:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) == 4 else None), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd697-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD697_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree))
        mirrors()
        candidate(run(ROOT))
        candidate(run(ROOT, main_tree))
    print(f"wait_cleared_owner_notice_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
