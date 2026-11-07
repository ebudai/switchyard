#!/usr/bin/env python3
"""SYRD-736: pull-ready rework pinned to a worker that cannot take it is reported to the Director, once.

Live on MEFP (2026-11-06): Audit returned MEFP-768 to luna-9 while luna-9 held MEFP-760, so it waited in the ready
stage pinned to luna-9, behind MEFP-760. The Director then reassigned MEFP-760 to luna-2, and luna-9 was stopped.
Only luna-9 could claim MEFP-768, and the listener gives work only to a running worker. Eight other claimants ran idle
and could not claim it. Nobody was told for nine hours, until the Director found it (MEFP-769 and luna-10 the same).

A production-built board runs here: schema.sql, roles, rbac.sql, every migration and rbac.sql again, with the shipped
example workflow opted into pull scheduling. The real HTTP server, the real notify listener and its real pull pickup,
and a real PaneActivityGate read real hook-state files and a synthetic /proc. Only the panes are stood in for: a
recording sender, and a `busy` set for the gate's probes. app is the author, main the idle claimant, and ops takes
the predecessor.

Main runs first, from a git archive, and is shown leaving the ticket pinned and saying nothing. Then this tree, on
the board production builds (whose migrations run last, so the migration's copy of the function is what runs), on
schema.sql and rbac.sql alone (schema.sql's copy), and on main's board upgraded by this tree's migrations:
- **Pinned while its author still holds the predecessor:** not stranded, whoever is idle.
- **The predecessor reassigned away and the author stopped:** the stranding is recorded at once, with the author's
  reason and the idle claimants, and nothing is said within the policy's idle_alert_seconds.
- **Past that, with nobody idle:** still nothing; the record keeps counting.
- **Past that, with main idle:** one Director notice naming the ticket, its author, why the author cannot take it,
  the ticket it was queued behind, and main, and what to do. Further passes add nothing.
- **The author's reason follows what the listener sees:** a dead registered process, or a quota hold.
- **The author running again:** it claims its rework, the record goes, and the Director hears nothing more.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "9e7253c54f313765355b2c286ae8041897b187a7"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, shape: str, before_root: Path | None) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, migrate
    from pull_scheduling_policy_test import pull_document
    from scripts.ticket_board import pull_pickup
    from scripts.ticket_board.notify_listener import PaneActivityGate, PaneHookStateStore, TicketBoardNotifyListener
    from scripts.ticket_board.peer_identity import SessionIdentity
    from temporary_cluster import temporary_cluster

    class Gate(PaneActivityGate):
        busy: set = set()

        def pre_send_busy(self, target: str) -> bool:
            return target in self.busy

        def is_working(self, target: str) -> bool:
            return target in self.busy

    targets = {role: f"pgu-{role}:0.0" for role in ("main", "app", "ops", "director", "audit", "inspector", "user")}
    seen: dict = {}
    with temporary_cluster(prefix="s736", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd736-state.") as state_dir:
        if shape == "upgraded":  # main's board, then this tree's migrations and rbac.sql, as a deploy upgrades it
            admin = build_board(fixture, cluster, before_root, "board")
            migrate(fixture, admin, root)
        elif shape == "schema":  # schema.sql and rbac.sql alone: schema.sql's own copy of each function
            admin = fixture.conninfo(cluster.socket_dir, cluster.port, "board")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "board"])
            fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
            fixture.create_roles(admin)
            fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        else:
            admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def act(ticket: str, operation: str, actor: str, **payload):
                return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

            def where(ticket: str) -> str:
                return sql("SELECT state || '/' || assignee || '/' || coalesce(nullif(queued_for_assignee,''),'-') || '/' "
                           f"|| coalesce(nullif(queued_behind_ticket,''),'-') FROM ticket_board.tickets WHERE id='{ticket}';")

            def claim(role: str) -> dict:
                with psycopg.connect(listener_url, autocommit=True) as conn:
                    return conn.execute("SELECT ticket_board.claim_ready_ticket(%s)", (role,)).fetchone()[0]

            def admitted(ticket: str) -> None:
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             needs_inspection=True, commit_exempt=True)
                act(ticket, "admit", "director")

            def through_audit(ticket: str, author: str) -> None:
                act(ticket, "submit_to_inspection", author)
                if where(ticket).startswith("inspection/"):
                    act(ticket, "inspector_sign_off", "inspector")
                act(ticket, "audit_sign_off", "audit", text="Audited.")

            def recorded() -> bool:
                return sql("SELECT to_regclass('ticket_board.pull_pinned_rework_strands') IS NOT NULL;") == "t"

            def strand() -> dict | None:
                if not recorded():
                    return None
                row = sql("SELECT coalesce((SELECT jsonb_build_object('author', author, 'reason', reason, 'idle', idle, "
                          "'since_age', extract(epoch FROM clock_timestamp() - since)::int, 'notified', notified_at IS NOT NULL) "
                          "FROM ticket_board.pull_pinned_rework_strands WHERE ticket_id='PGU-1'), 'null'::jsonb);")
                return json.loads(row)

            def backdate(minutes: int) -> None:
                sql("UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = entered_current_state_at "
                    f"- interval '{minutes} minutes' WHERE ticket_id='PGU-1';")
                if recorded():
                    sql(f"UPDATE ticket_board.pull_pinned_rework_strands SET since = since - interval '{minutes} minutes';")

            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

            # app writes PGU-1; Audit approves it, which releases app (release_after: audit); app takes PGU-2.
            admitted("PGU-1")
            assert claim("app")["ticket"] == "PGU-1", where("PGU-1")
            through_audit("PGU-1", "app")
            admitted("PGU-2")
            assert claim("app")["ticket"] == "PGU-2", where("PGU-2")
            # PGU-1 comes back to app while app holds PGU-2: it waits in ready, pinned to app, behind PGU-2.
            for stage, step, actor in (("dat", "director_dat_sign_off", "director"), ("user_review", "user_sign_off", "user")):
                if where("PGU-1").startswith(stage + "/"):
                    act("PGU-1", step, actor, text="Accepted.")
            act("PGU-1", "director_kick_back", "director", reason="Final review found a gap.")
            seen["pinned"] = where("PGU-1")

            # The listener, with app stopped (no registration), main running and idle, ops running and busy.
            proc = Path(state_dir) / "proc"
            store = PaneHookStateStore(Path(state_dir) / "hooks")
            gate = Gate(state_store=store)
            gate.proc_root = proc
            gate.role_targets = dict(targets)
            gate.busy = {targets["ops"]}
            identities = {}
            for n, role in enumerate(("main", "app", "ops"), start=1):
                (proc / str(4100 + n)).mkdir(parents=True)
                (proc / str(4100 + n) / "stat").write_text(f"{4100 + n} (codex) S 1 " + " ".join(["0"] * 17) + f" {5000 + n} 0 0\n")
                identities[role] = SessionIdentity(4100 + n, 5000 + n)
                store.write(targets[role], "idle", source="codex.Stop")
            gate.role_identities = {role: identities[role] for role in ("main", "ops")}
            sent: list = []
            listener = TicketBoardNotifyListener(
                conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working, target_exists=lambda _t: True, submission_witness=lambda *_a: True,
                poll_seconds=0, logger=logging.getLogger("syrd736"))
            listener.role_targets = dict(targets)

            def passes(count: int = 2) -> list:
                sent.clear()
                for _ in range(count):
                    listener.listen_once(max_notifications=10)
                return [message for target, message in sent if target == targets["director"] and "PGU-1" in message]

            # The author still holds the predecessor: the pin is a wait, not a stranding, however long.
            backdate(30)
            seen["holding"] = {"told": passes(), "strand": strand(), "where": where("PGU-1")}

            # MEFP's move: the Director reassigns the predecessor away. app holds nothing and is not running.
            act("PGU-2", "reassign", "director", assignee="ops", reason="Taking PGU-2 over.")
            seen["freed"] = {"told": passes(), "strand": strand(), "where": where("PGU-1")}

            # Blocked, it is claimable by nobody: not stranded. Unblocked, it is again, from then.
            fixture.seed_postgres_ticket(admin, "PGU-3", title="PGU-3", state="analysis", assignee="director",
                                         commit_exempt=True)
            act("PGU-1", "set_blockers", "director", blocked_by=["PGU-3"], blocked_reason="Needs PGU-3.")
            seen["blocked"] = {"told": passes(), "strand": strand()}
            act("PGU-1", "set_blockers", "director", blocked_by=[], blocked_reason="")
            seen["unblocked"] = {"told": passes(), "strand": strand()}

            # Past idle_alert_seconds, with nobody idle: main busy too.
            gate.busy = {targets["ops"], targets["main"]}
            backdate(20)
            seen["nobody_idle"] = {"told": passes(), "strand": strand(), "where": where("PGU-1")}

            # And with main idle.
            gate.busy = {targets["ops"]}
            seen["stranded"] = {"told": passes(), "strand": strand(), "where": where("PGU-1"),
                                "enqueued": sql("SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id='PGU-1' "
                                                "AND event='enqueue' AND detail->>'dedupe_key' LIKE 'pull-pinned-rework:PGU-1:%';")}
            seen["again"] = {"told": passes(3), "strand": strand()}

            # What the listener sees of app, through its reason: a registered process that is gone, then a quota hold.
            gate.role_identities["app"] = SessionIdentity(4199, 5099)
            passes(1)
            seen["gone"] = strand()
            gate.role_identities.pop("app")
            real_held = pull_pickup._held
            pull_pickup._held = lambda _listener, _conn: {"app": "its provider is held for quota"}
            try:
                passes(1)
                seen["held"] = strand()
            finally:
                pull_pickup._held = real_held

            # app running but busy: it will claim when its turn ends, so its rework is not stranded.
            gate.role_identities["app"] = identities["app"]
            gate.busy = {targets["ops"], targets["app"]}
            seen["author_busy"] = {"told": passes(1), "strand": strand()}
            gate.role_identities.pop("app")
            gate.busy = {targets["ops"]}

            # The pin moved to another author between passes: a new stranding, not the old one's clock or notice.
            # Said straight to the function, as the listener would, with main the stopped author and ops idle.
            def pin(role: str) -> None:
                sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET queued_for_assignee = "
                    f"'{role}' WHERE id='PGU-1';")

            def report(at: str) -> int | str:
                try:
                    with psycopg.connect(listener_url, autocommit=True) as conn:
                        return conn.execute(
                            "SELECT ticket_board.notify_pull_pinned_rework_strands(%s::jsonb, %s::jsonb, clock_timestamp() "
                            "+ %s::interval)", ('{"ops": "idle, nothing claimable"}', '{}', at)).fetchone()[0]
                except psycopg.Error as exc:  # read as an answer, so a refusal fails a check rather than the run
                    return f"refused: {exc.sqlstate}"

            if recorded():  # main has no such function: nothing to say it to
                passes(1)
                pin("main")
                admitted("PGU-4")  # unpinned and claimable: whoever is idle takes it, so it is never stranded
                seen["moved"] = {"now": report("0 seconds"), "strand": strand(),
                                 "rows": sql("SELECT string_agg(ticket_id, ',') FROM ticket_board.pull_pinned_rework_strands;")}
                seen["moved"]["later"] = report("1 hour")
                seen["moved"]["queued"] = sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-1' "
                                              "AND payload->>'queued_for' = 'main';")
                # Moved back before that notice is delivered: it no longer says where the ticket waits, and is dropped.
                pin("app")
                seen["moved"]["told"] = passes(1)
                seen["moved"]["queued_after"] = sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                                    "WHERE ticket_id='PGU-1' AND payload->>'queued_for' = 'main';")

            # app runs again: it claims its own rework, the stranding ends, and nobody else is told anything.
            gate.role_identities["app"] = identities["app"]
            seen["recovered"] = {"told": passes(), "strand": strand(), "where": where("PGU-1"),
                                 "rows": sql("SELECT count(*) FROM ticket_board.pull_pinned_rework_strands;")
                                 if recorded() else None}
        finally:
            server.shutdown()
            server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path, shape: str, before_root: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), shape,
                            str(before_root or "")],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    check(got["pinned"] == "ready/unassigned/app/PGU-2", f"main: the rework waits pinned to app behind PGU-2: {got}")
    stranded = got["stranded"]
    check(stranded["where"] == "ready/unassigned/app/PGU-2" and stranded["told"] == [],
          f"main: with PGU-2 gone from app, app stopped and main idle past the alert interval, the rework stays pinned "
          f"and the Director is told nothing: {stranded}")
    check(got["again"]["told"] == [] and got["recovered"]["where"].startswith("in_progress/app/"),
          f"main: nothing later either, until app runs again and claims it: {got['again']} {got['recovered']}")


def candidate(got: dict, shape: str) -> None:
    def rec(phase: dict) -> dict:  # a stranding record, read as empty when there is none: a mutant fails a check, not the run
        return {"author": None, "reason": None, "idle": None, "since_age": -1, "notified": None, **(phase.get("strand") or {})}

    check(got["pinned"] == "ready/unassigned/app/PGU-2", f"{shape}: the rework waits pinned to app behind PGU-2: {got}")
    holding = got["holding"]
    check(holding["told"] == [] and holding["strand"] is None,
          f"{shape}: while app still holds PGU-2 the pin is a wait, not a stranding: {holding}")
    freed = got["freed"]
    check({**(freed["strand"] or {}), "since_age": (freed["strand"] or {}).get("since_age", 60) < 60}
          == {"author": "app", "reason": "it is not running (no registered provider process)",
              "idle": {"main": "idle, nothing claimable"}, "since_age": True, "notified": False},
          f"{shape}: once PGU-2 leaves app, the stranding is recorded at once, with app's reason and main idle: {freed}")
    check(freed["told"] == [] and freed["where"] == "ready/unassigned/app/PGU-2",
          f"{shape}: and nothing is said or moved within the alert interval: {freed}")
    check(got["blocked"]["strand"] is None and got["blocked"]["told"] == [],
          f"{shape}: blocked rework is claimable by nobody, so it is not stranded: {got['blocked']}")
    check(got["unblocked"]["strand"] is not None and rec(got["unblocked"])["since_age"] < 60
          and got["unblocked"]["told"] == [],
          f"{shape}: unblocked, it is stranded again, counted from then: {got['unblocked']}")
    quiet = got["nobody_idle"]
    check(quiet["told"] == [] and rec(quiet)["idle"] == {} and rec(quiet)["since_age"] >= 1200
          and not rec(quiet)["notified"],
          f"{shape}: past the interval with no claimant idle, nothing is said yet: {quiet}")
    stranded = got["stranded"]
    check(len(stranded["told"]) == 1 and stranded["enqueued"] == "1",
          f"{shape}: past the interval with main idle, the Director is told once: {stranded}")
    told = stranded["told"][0]
    for part in ("PGU-1 is rework pinned to app, and app has not been able to take it since ",
                 "app holds no ticket now (it was queued behind PGU-2), but it is not running (no registered provider "
                 "process).", "These implementers are idle and cannot claim it while it is pinned: main (idle, nothing "
                 "claimable).", "Start app, or move PGU-1 out of ready and route it to one of them."):
        check(part in told, f"{shape}: the notice says {part!r}: {told}")
    check(stranded["where"] == "ready/unassigned/app/PGU-2" and rec(stranded)["notified"],
          f"{shape}: the board moves nothing; the record says it was told: {stranded}")
    check(got["again"]["told"] == [] and rec(got["again"])["notified"],
          f"{shape}: later passes tell nobody again: {got['again']}")
    check(rec({"strand": got["gone"]})["reason"] == "it is not running (registered provider process is gone)"
          and rec({"strand": got["held"]})["reason"] == "its provider is held for quota",
          f"{shape}: the record follows what the listener sees of app: {got['gone']} {got['held']}")
    check(got["author_busy"]["strand"] is None and not any("rework pinned" in m for m in got["author_busy"]["told"]),
          f"{shape}: app running but busy is not stranded (the idle notice names a running worker that does not "
          f"claim): {got['author_busy']}")
    moved = got["moved"]
    check(moved["now"] == 0 and rec(moved)["author"] == "main" and rec(moved)["since_age"] < 60
          and not rec(moved)["notified"] and moved["rows"] == "PGU-1"
          and rec(moved)["idle"] == {"ops": "idle, nothing claimable"},
          f"{shape}: a pin moved to another author starts a new stranding, unannounced; unpinned work is none: {moved}")
    check(rec(moved)["reason"] == "it did not claim it on the listener's last pass",
          f"{shape}: an author the listener gave no reason for is said to have not claimed it: {moved}")
    check(moved["later"] == 1 and moved["queued"] == "1",
          f"{shape}: which is announced in its own right once it has waited: {moved}")
    check(moved["told"] == [] and moved["queued_after"] == "0",
          f"{shape}: and dropped undelivered when the pin moves on before it is sent: {moved}")
    recovered = got["recovered"]
    check(recovered["where"] == "in_progress/app/-/-" and recovered["strand"] is None and recovered["rows"] == "0"
          and recovered["told"] == [],
          f"{shape}: app running again claims its rework, the stranding ends, and the Director hears nothing: {recovered}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]) if sys.argv[4] else None)))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd736-before.", dir="/tmp") as tmp:
        tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD736_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
            before(run(tree, "built"))
        candidate(run(ROOT, "built"), "built")
        candidate(run(ROOT, "schema"), "schema")
        candidate(run(ROOT, "upgraded", tree), "upgraded")
    print(f"pinned_rework_strand_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
