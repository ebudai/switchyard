#!/usr/bin/env python3
"""SYRD-696: a ticket waiting on the User is not runnable work for the board stall alert, however long it waits.

SYRD-691 sat in Implementation with Ops waiting on the User's approval. Four hours after each wait was recorded the
board stall alert counted it as Ops' runnable work, and an hour later the Director was told the board had stalled.

On production-built boards (router_role_board_test's), with the evaluator called as the listener calls it at
simulated times (board_stall_test's `Stall`), main first from a git archive, then this tree:

- **SYRD-691's state:** Implementation, owned by Ops, with a wait on the User recorded before SYRD-662 -- the state
  an upgraded board keeps. It is written as the live board wrote it, only the SYRD-662 write guard set aside for that
  one row, so the board's own triggers record the wait as they did.
- **The Director's await-user,** in a stage the workflow's `user_waits` lists (Audit, owned by Audit).

Main counts both from the moment the waits are four hours old and opens an episode with its digest half an hour
later. Here neither ever counts, and no episode opens; a mixed board still alerts for its runnable ticket and for a
pane-role wait past its handoff's window, and not for the User's; clearing the wait counts the ticket again from the
clearing, its own hand-over time unchanged, and the wait's recorded progress kept.
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
BEFORE = "274b984e7668d675b2ed60b7d36eb65193e38760"
THRESHOLD = 1800
LAPSE = 240  # minutes: ticket_awaiting_role_is_active's four hours
REASON = "waiting on user, the User, until the wait is cleared"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, upgrade_from: Path | None = None) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from board_stall_test import Stall, example, short
    from router_role_board_test import Board, temporary_cluster
    from scripts.ticket_board import board_stall

    cfg = example()
    cfg["user_waits"] = ["audit"]
    out: dict = {}

    def legacy_user_wait(board, ticket: str, role: str = "user") -> None:
        """A wait on the User recorded before SYRD-662, as SYRD-691's was: every trigger but the write guard runs."""
        board.sql("ALTER TABLE ticket_board.ticket_notification_state DISABLE TRIGGER ticket_notification_state_user_wait_guard; "
                  f"UPDATE ticket_board.ticket_notification_state SET awaiting_role = '{role}', "
                  f"awaiting_since_at = clock_timestamp() WHERE ticket_id = '{ticket}'; "
                  "ALTER TABLE ticket_board.ticket_notification_state ENABLE TRIGGER ticket_notification_state_user_wait_guard;")

    def seen(stall, minutes: float, *tickets: str) -> dict:
        stall.evaluate(minutes, threshold=THRESHOLD)
        state = stall.state()
        return {"runnable": sorted(stall.runnable()), "excluded": {t: state["excluded"].get(t) for t in tickets},
                "episodes": [(e["closed"], e["reason"], sorted(i["ticket"] for i in e["tickets"])) for e in stall.episodes()],
                "digests": stall.digests()}

    def hand_over(board, ticket: str) -> str:
        return board.sql("SELECT greatest(entered_current_state_at, current_assignment_at)::text "
                         f"FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket}';")

    with temporary_cluster(prefix="syrd696-pg-") as cluster:
        # -- SYRD-691's wait, and the Director's await-user, alone on the board -------------------------------------
        board = Board(cluster, "userwait", cfg)
        try:
            stall = Stall(board)
            implementing = stall.ticket("in_progress", "ops")
            handed = hand_over(board, implementing)
            legacy_user_wait(board, implementing)
            auditing = stall.ticket("audit", "audit")
            out["await_user"] = short(board.act(auditing, "await_user", "director", reason="Only the User can approve it.",
                                                request="Approve the packet on the ticket."))
            out["waits"] = board.sql("SELECT string_agg(ticket_id || '=' || awaiting_role, ',' ORDER BY ticket_id) "
                                     "FROM ticket_board.ticket_notification_state WHERE awaiting_role <> '';")
            out["at"] = {str(m): seen(stall, m, implementing, auditing)
                         for m in (0, LAPSE - 1, LAPSE + 1, LAPSE + 1 + THRESHOLD / 60, 600)}
            # -- the User answers: the wait is cleared, and the ticket counts again from then ------------------------
            mark = stall.last_event_id()
            out["clear"] = short(board.act(implementing, "clear_awaiting_role", "director"))
            stall.stamp_progress(mark, 601)
            out["cleared"] = {str(m): seen(stall, m, implementing) for m in (602, 630, 632)}
            # The digest's hand-over time for it, against the board's own, compared in the board's clock.
            out["since_is_hand_over"] = board.sql(
                "SELECT coalesce(bool_and((x->>'waiting_since')::timestamptz = greatest(ns.entered_current_state_at, "
                "ns.current_assignment_at)), false) FROM ticket_board.board_stall_state s, jsonb_array_elements(s.runnable) x "
                f"JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = x->>'ticket' WHERE x->>'ticket' = '{implementing}';")
            out["same_hand_over"] = hand_over(board, implementing) == handed
            out["progress"] = board.sql("SELECT string_agg(event, ' | ' ORDER BY id) FROM ticket_board.board_progress_events "
                                        f"WHERE ticket_id = '{implementing}';")
            out["last_episode"] = stall.episodes()[-1] if stall.episodes() else None
            out["tickets"] = {"implementing": implementing, "auditing": auditing}
        finally:
            board.close()

        # -- a mixed board: the User's wait, a pane-role wait, and plain runnable work -------------------------------
        board = Board(cluster, "mixed", cfg)
        try:
            stall = Stall(board)
            waiting_user = stall.ticket("in_progress", "ops")
            legacy_user_wait(board, waiting_user)
            waiting_pane = stall.ticket("in_progress", "main")
            out["pane_dependency"] = short(board.act(waiting_pane, "request_dependency", "main", role="director",
                                                     reason="Which database?"))
            plain = stall.ticket("in_progress", "app")
            out["mixed"] = {str(m): seen(stall, m, waiting_user, waiting_pane, plain) for m in (0, LAPSE + 1)}
            out["mixed_tickets"] = {"user": waiting_user, "pane": waiting_pane, "plain": plain}
            out["describe"] = [line for line in board_stall.describe(stall.state()).splitlines() if waiting_user in line]
        finally:
            board.close()

        # -- a second role of the User's kind, by its own name: the board knows the User by kind (SYRD-549) ------
        second = json.loads(json.dumps(cfg))
        second["roles"].append({**next(r for r in cfg["roles"] if r["name"] == "user"), "name": "patron"})
        board = Board(cluster, "patron", second)
        try:
            stall = Stall(board)
            waiting_patron = stall.ticket("in_progress", "ops")
            legacy_user_wait(board, waiting_patron, role="patron")
            out["patron"] = {str(m): seen(stall, m, waiting_patron) for m in (0, LAPSE + 1)}
            out["patron_ticket"] = waiting_patron
        finally:
            board.close()

        # -- main's board, upgraded by this tree's migrations ---------------------------------------------------------
        if upgrade_from is not None:
            board = Board(cluster, "upgraded", cfg, before=upgrade_from)
            try:
                stall = Stall(board)
                waiting_user = stall.ticket("in_progress", "ops")
                legacy_user_wait(board, waiting_user)
                out["upgraded"] = {str(m): seen(stall, m, waiting_user) for m in (0, LAPSE + 1 + THRESHOLD / 60)}
                out["upgraded_ticket"] = waiting_user
                out["upgraded_migrations"] = board.sql("SELECT count(*) FROM ticket_board.schema_migrations "
                                                       "WHERE name LIKE 'pgu1018_syrd696%';")
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


def common(got: dict) -> tuple[str, str]:
    t = got["tickets"]
    check(got["await_user"] == "ok" and got["waits"] == f"{t['implementing']}=user,{t['auditing']}=user",
          f"SYRD-691's wait and the Director's await-user are both recorded: {got['await_user']} {got['waits']}")
    check(got["pane_dependency"] == "ok", f"a pane-role dependency is recorded: {got['pane_dependency']}")
    return t["implementing"], t["auditing"]


def before(got: dict) -> None:
    implementing, auditing = common(got)
    first, lapsed = got["at"]["0"], got["at"][str(LAPSE + 1)]
    check(first["excluded"] == {implementing: "waiting on user", auditing: "waiting on user"} and first["runnable"] == [],
          f"main: both waits excluded while four hours young: {first}")
    check(lapsed["runnable"] == sorted([implementing, auditing]),
          f"main: four hours on, both count as runnable work: {lapsed}")
    fired = got["at"][str(LAPSE + 1 + THRESHOLD / 60)]
    check(fired["digests"] == 1 and fired["episodes"] == [[False, None, sorted([implementing, auditing])]],
          f"main: and half an hour later the Director is sent a board stall digest naming them: {fired}")
    check(got["patron"][str(LAPSE + 1)]["runnable"] == [got["patron_ticket"]],
          f"main: a wait on another role of the User's kind counts too: {got['patron']}")
    mixed, m = got["mixed"][str(LAPSE + 1)], got["mixed_tickets"]
    check(mixed["digests"] == 1 and mixed["episodes"] == [[False, None, sorted(m.values())]],
          f"main: a mixed board's one digest names the User's wait with the runnable work: {mixed}")


def candidate(got: dict) -> None:
    implementing, auditing = common(got)
    for minutes, at in got["at"].items():
        check(at["runnable"] == [] and at["excluded"] == {implementing: REASON, auditing: REASON},
              f"at {minutes} min neither wait counts, and the status says why: {at}")
        check(at["episodes"] == [] and at["digests"] == 0, f"at {minutes} min no episode opens and nothing is sent: {at}")
    check(got["clear"] == "ok", f"the wait is cleared: {got['clear']}")
    early, still, fired = (got["cleared"][k] for k in ("602", "630", "632"))
    check(early["runnable"] == [implementing] and early["episodes"] == [] and still["episodes"] == [],
          f"cleared, the ticket counts again, on a clock started by the clearing: {early} {still}")
    check(fired["digests"] == 1 and fired["episodes"] == [[False, None, [implementing]]],
          f"and half an hour after the clearing, exactly one digest: {fired}")
    check(got["same_hand_over"] and got["since_is_hand_over"] == "t",
          f"its own hand-over time is unchanged by the wait, and the digest gives it: {got['same_hand_over']} "
          f"{got['since_is_hand_over']}")
    check(got["progress"].split(" | ")[-2:] == [f"{implementing} waits on user", f"{implementing} dependency cleared"],
          f"the wait's recorded progress is kept, and the clearing is the last: {got['progress']}")
    check(got["last_episode"]["tickets"][0]["ticket"] == implementing, f"the digest names it: {got['last_episode']}")
    m = got["mixed_tickets"]
    young, lapsed = got["mixed"]["0"], got["mixed"][str(LAPSE + 1)]
    check(young["excluded"] == {m["user"]: REASON, m["pane"]: "waiting on director", m["plain"]: None}
          and young["runnable"] == [m["plain"]],
          f"mixed: the pane-role wait is excluded while its handoff's window is open: {young}")
    check(lapsed["runnable"] == sorted([m["pane"], m["plain"]]) and lapsed["excluded"][m["user"]] == REASON,
          f"mixed: past its window the pane-role wait counts as before; the User's never does: {lapsed}")
    check(lapsed["digests"] == 1 and lapsed["episodes"] == [[False, None, sorted([m["pane"], m["plain"]])]],
          f"mixed: one digest, for the runnable work and not the User's wait: {lapsed}")
    check(got["describe"] == [f"  not counted: {m['user']}: {REASON}"],
          f"`ticket-board-read stall` says why it is not counted: {got['describe']}")
    patron = got["patron_ticket"]
    for minutes, at in got["patron"].items():
        check(at["runnable"] == [] and at["excluded"] == {patron: "waiting on patron, the User, until the wait is cleared"},
              f"a second role of the User's kind is the User all the same, at {minutes} min: {at}")
    if "upgraded" in got:
        u = got["upgraded_ticket"]
        check(got["upgraded_migrations"] == "1", f"main's board takes this tree's migration: {got['upgraded_migrations']}")
        for minutes, at in got["upgraded"].items():
            check(at["runnable"] == [] and at["excluded"] == {u: REASON} and at["digests"] == 0,
                  f"upgraded, at {minutes} min, SYRD-691's wait is not counted and nothing is sent: {at}")


def mirrors() -> None:
    """schema.sql carries pgu1018's function byte for byte, last, so fresh and upgraded boards agree."""
    migration = (ROOT / "scripts/ticket_board/migrations/pgu1018_syrd696_user_wait_not_runnable.sql").read_text()
    body = migration.split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;\n", 1)[0]
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    check(body.startswith("CREATE OR REPLACE FUNCTION ticket_board.board_stall_runnable(") and schema.endswith(body),
          "schema.sql ends with pgu1018's board_stall_runnable, byte for byte")


def main() -> int:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) == 4 else None), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd696-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD696_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree))
        mirrors()
        candidate(run(ROOT, main_tree))
    print(f"board_stall_user_wait_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
