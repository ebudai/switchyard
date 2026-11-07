#!/usr/bin/env python3
"""SYRD-745: waiting Audit work goes to an idle second auditor, and a reassignment to it is delivered.

Live on MEFP (2026-11-07): Audit is owned by audit and audit-2 (an ephemeral reviewer templated on audit), both live
and connected. Every submission went to audit -- a transition names no owner, so the declared update gives the stage's
first -- and MEFP-942, -953 and -956 waited behind MEFP-932 ("finish current") while audit-2 sat idle with an empty
queue. The Director moved MEFP-956 to audit-2 by hand; its delivery read pending at that instant, and audit-2 signed it
off 2 min 37 s later.

A production-built board runs here with the shipped example workflow given audit-2 the same way. The real HTTP server
and notify listener run, with the real PaneActivityGate reading real hook-state files. Every role is registered with a
live stand-in provider process (SYRD-644's rig), and the panes are a recording sender. A reviewer that receives a
ticket stays busy reviewing it until the test ends its turn.

Main runs first, from a git archive: all three submissions stay with audit, and audit-2 is given nothing. Then this
tree:
- **Balance:** the second submission, waiting behind audit's current review, goes to idle audit-2 and is delivered,
  recorded on the ticket. The third waits while audit-2 is busy, and goes to it once audit-2 has signed off and is
  idle again. audit's current review is never taken, and audit is sent nothing more.
- **Never moved:** to a busy auditor, one idle but still holding a review, one whose provider is held, or one whose
  provider process is gone; a blocked, a manually controlled or an awaited review, or a re-review its auditor began.
- **A direct reassignment** to idle audit-2 is delivered on the next pass (on main too).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "ed138c67953444f3c1da25427f8d68fc9246d33a"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
ROLES = ("director", "main", "app", "ops", "audit", "audit-2", "inspector")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import codex_capacity_retry_test as c594
    import provider_quota_hold_test as rigs
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    providers: dict = {}
    with temporary_cluster(prefix="s745", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd745.") as tmp:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "f", cluster.root / "a", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def act(ticket: str, op: str, who: str, **body):
                return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{op}", body, caller=who)

            doc = workflow(root)
            audit = next(r for r in doc["roles"] if r["name"] == "audit")
            doc["roles"].append({**audit, "name": "audit-2", "label": "Audit 2", "target": "pgu-audit-2:0.0", "slot": None,
                                 "ephemeral": True, "template_role": "audit"})
            for stage in doc["stages"]:
                if stage["name"] == "audit":
                    stage["owners"] = ["audit", "audit-2"]
            for transition in doc["transitions"]:
                if transition["from"] == "audit" and "audit" in transition["actors"]:
                    transition["actors"] = sorted(set(transition["actors"]) | {"audit-2"})
            app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
            runtimes = {r["name"]: r.get("runtime") for r in doc["roles"]}
            targets = {r["name"]: r["target"] for r in doc["roles"] if r["name"] in ROLES}
            for role in ROLES:
                providers[role] = rigs.Provider(Path(tmp))
                p = providers[role]
                sql(f"SELECT ticket_board.register_role_runtime('{role}', '{runtimes[role]}', '{targets[role]}', '/tmp', '/tmp', "
                    f"{p.pid}, {p.ticks}, {os.getuid()}, 0);")
            store = nl.PaneHookStateStore(Path(tmp) / "state")
            shown = c594.screens()
            panes = {targets[r]: c594.Pane(shown["director"], p.pid, p.started) for r, p in providers.items()}
            tmux, table = c594.fake_tmux(panes)
            for role in ROLES:
                store.write(targets[role], "idle", source=f"{runtimes[role]}.Stop", now=time.time() - 600)
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, role_runtimes={r: runtimes[r] for r in ROLES})
            sends: list = []
            reviewing = {"audit", "audit-2"}  # a reviewer handed a ticket stays busy reviewing it

            def sender(target: str, message: str) -> dict:
                role = next(r for r, t in targets.items() if t == target)
                sends.append((role, message))
                store.write(target, "busy", source=f"{runtimes[role]}.UserPromptSubmit")
                if role not in reviewing or message.startswith("/"):
                    store.write(target, "idle", source=f"{runtimes[role]}.Stop")
                return {}

            def passes(count: int = 3) -> None:
                nl.TicketBoardNotifyListener._wait_for_notification = lambda _self, _conn: False
                for _ in range(count):
                    listener = nl.TicketBoardNotifyListener(
                        conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                        target_exists=lambda _t: True, poll_seconds=0, submission_confirm_seconds=0.2,
                        submission_poll_seconds=0.05)
                    listener.delivery_submitter = lambda _t: None
                    listener.unresolved_turn_grace_seconds = 3600
                    listener.listen_once(max_notifications=50)
                    time.sleep(0.2)

            def owners() -> dict:
                return json.loads(sql("SELECT coalesce(json_object_agg(id, assignee), '{}') FROM ticket_board.tickets "
                                      "WHERE state = 'audit';"))

            def handed(role: str, since: int) -> list:
                return [m.split(" -- ")[0] for r, m in sends[since:] if r == role and " -- " in m]

            def idle(role: str) -> None:
                store.write(targets[role], "idle", source=f"{runtimes[role]}.Stop")

            def submitted(number: int, author: str) -> str:
                ticket = f"PGU-{number}"
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
                act(ticket, "route", "director", state="in_progress", assignee=author)
                act(ticket, "submit_to_audit", author)
                return ticket

            def comments(ticket: str) -> list:
                return [c["text"] for c in app.get_ticket(ticket)["comments"] if c["who"] == "ticket_board_service"]

            # 1. Three submissions; audit takes the first and is busy reviewing it.
            mark = len(sends)
            for number, author in ((1, "main"), (2, "app"), (3, "ops")):  # all three before any pass: oldest goes first
                submitted(number, author)
            passes(3)
            seen["submitted"] = {"owners": owners(), "to_audit": handed("audit", mark), "to_audit_2": handed("audit-2", mark),
                                 "balance_notes": comments("PGU-2")}
            # audit-2's turn ends with PGU-2 still under review: idle, but holding a review, it is given no second one.
            idle("audit-2")
            passes(2)
            seen["submitted"]["idle_holding"] = owners()

            # 2. audit-2 signs PGU-2 off and is idle again: the next waiting review goes to it.
            mark = len(sends)
            if owners().get("PGU-2") == "audit-2":
                act("PGU-2", "audit_sign_off", "audit-2", text="Audited.")
                idle("audit-2")
            passes(3)
            seen["freed"] = {"owners": owners(), "to_audit_2": handed("audit-2", mark), "to_audit": handed("audit", mark)}

            if freed_ok := (seen["freed"]["owners"].get("PGU-3") == "audit-2"):  # balancing happened: this tree
                # 3. Never moved. audit-2 signs off PGU-3 if it has it, then each case is tried with audit-2 idle and empty.
                if owners().get("PGU-3") == "audit-2":
                    act("PGU-3", "audit_sign_off", "audit-2", text="Audited.")
                idle("audit-2")
                for done in ("PGU-2", "PGU-3"):  # closed, which releases app and ops for the next submissions
                    if owners().get(done):  # still in Audit (crash-safe under a mutant)
                        act(done, "audit_sign_off", owners()[done], text="Audited.")
                    act(done, "cancel", "director")
                cases = {}
                # A blocked review, and a manually controlled one.
                blocked = submitted(4, "app")
                fixture.seed_postgres_ticket(admin, "PGU-90", title="PGU-90", state="analysis", assignee="director",
                                             commit_exempt=True)
                act(blocked, "set_blockers", "director", blocked_by=["PGU-90"], blocked_reason="needs PGU-90")
                held = submitted(5, "ops")
                sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET manually_controlled = true "
                    f"WHERE id = '{held}';")
                passes(2)
                cases["blocked"], cases["manual"] = owners().get(blocked), owners().get(held)
                act(blocked, "set_blockers", "director", blocked_by=[], blocked_reason="")
                sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET manually_controlled = false "
                    f"WHERE id = '{held}';")
                # audit-2 busy: nothing moves.
                store.write(targets["audit-2"], "busy", source="claude.UserPromptSubmit")
                passes(2)
                cases["busy"] = {blocked: owners().get(blocked), held: owners().get(held)}
                # A re-review audit already began (the trace of an earlier round: handed to audit in Audit before) stays
                # with audit, while the other waiting review goes to idle audit-2. audit's current review is never taken.
                sql(f"INSERT INTO ticket_board.notification_trace (ticket_id, target_role, kind, event, ticket_state_at_event) "
                    f"VALUES ('{blocked}', 'audit', 'transition', 'send', 'audit');")
                idle("audit-2")
                passes(3)
                seen["affinity"] = {"begun": blocked, "begun_owner": owners().get(blocked), "other": held,
                                    "other_owner": owners().get(held), "current": owners().get("PGU-1")}
                # A fresh waiting review, audit-2 empty again: not to audit-2 while its provider is held, nor once its
                # provider process is gone.
                if owners().get(held):  # still in Audit, with whoever holds it (crash-safe under a mutant)
                    act(held, "audit_sign_off", owners()[held], text="Audited.")
                act(held, "cancel", "director")
                # A direct reassignment to idle, empty audit-2: delivered on the next pass.
                idle("audit-2")  # its turn on PGU-5 ended
                mark = len(sends)
                if owners().get(blocked) == "audit":
                    act(blocked, "reassign", "director", assignee="audit-2", reason="balance by hand")
                passes(2)
                seen["reassigned"] = {"ticket": blocked, "to_audit_2": handed("audit-2", mark),
                                      "delivery": (app.get_ticket(blocked).get("active_work_delivery") or {}).get("state")}
                if owners().get(blocked):
                    act(blocked, "audit_sign_off", owners()[blocked], text="Audited.")
                act(blocked, "cancel", "director")
                fresh = submitted(6, "ops")
                passes(1)
                # Awaited: audit waits on Ops about it (the field await-role records). Not moved while that stands.
                idle("audit-2")  # its turn on PGU-4 ended
                sql(f"UPDATE ticket_board.ticket_notification_state SET awaiting_role = 'ops', awaiting_since_at = clock_timestamp() "
                    f"WHERE ticket_id = '{fresh}';")
                passes(2)
                cases["awaited"] = {fresh: owners().get(fresh)}
                sql(f"UPDATE ticket_board.ticket_notification_state SET awaiting_role = '', awaiting_since_at = NULL "
                    f"WHERE ticket_id = '{fresh}';")
                idle("audit-2")  # its turn on PGU-4 ended
                registration = json.loads(sql("SELECT row_to_json(a)::text FROM (SELECT generation, process_pid AS pid, "
                                              "process_start_time AS start_time FROM ticket_board.role_runtime_assignments "
                                              "WHERE role = 'audit-2') a;"))
                evidence = json.dumps({"runtime": runtimes["audit-2"], **registration, "source": "claude.StopFailure",
                                       # A failure after that turn end, so the hold is not ended as recovered (SYRD-644).
                                       "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5)),
                                       "turn": "stopfailure:test", "reset_at": time.strftime(
                                           "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)), "message": "limit"})
                sql(f"SET ROLE ticket_board_listener; SELECT ticket_board.note_provider_quota('audit-2', '{evidence}'::jsonb);")
                passes(2)
                cases["provider_held"] = {fresh: owners().get(fresh)}
                sql("UPDATE ticket_board.provider_quota_holds SET state = 'ended', ended_at = clock_timestamp(), "
                    "end_reason = 'cleared: test' WHERE role = 'audit-2' AND state IN ('held', 'released');")
                providers["audit-2"].stop()
                passes(2)
                cases["process_gone"] = {fresh: owners().get(fresh)}
                seen["never"] = cases

            if not freed_ok:  # main's tree: the same direct reassignment, with audit-2 alive
                # 5. A direct reassignment to idle audit-2: delivered on the next pass.
                store.write(targets["audit-2"], "idle", source="claude.Stop")
                waiting = sql("SELECT coalesce((SELECT id FROM ticket_board.tickets WHERE state = 'audit' AND assignee = 'audit' "
                              "AND id <> 'PGU-1' ORDER BY ticket_number LIMIT 1), '');")
                mark = len(sends)
                if waiting:
                    act(waiting, "reassign", "director", assignee="audit-2", reason="balance by hand")
                passes(2)
                seen["reassigned"] = {"ticket": waiting, "to_audit_2": handed("audit-2", mark),
                                      "delivery": (app.get_ticket(waiting).get("active_work_delivery") or {}).get("state") if waiting else None}
        finally:
            server.shutdown()
            server.server_close()
            for provider in providers.values():
                try:
                    provider.stop()
                except Exception:  # noqa: BLE001 -- already stopped
                    pass
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    submitted = got["submitted"]
    check(submitted["owners"] == {"PGU-1": "audit", "PGU-2": "audit", "PGU-3": "audit"} and submitted["to_audit_2"] == [],
          f"main: every submission stays with audit, and idle audit-2 is given nothing: {submitted}")
    check(got["freed"]["owners"].get("PGU-3") == "audit", f"main: nothing moves later either: {got['freed']}")
    reassigned = got["reassigned"]
    check(reassigned["to_audit_2"] == [reassigned["ticket"]] and reassigned["delivery"] == "delivered",
          f"main: a direct reassignment to idle audit-2 is delivered on the next pass: {reassigned}")


def candidate(got: dict) -> None:
    submitted = got["submitted"]
    check(submitted["owners"] == {"PGU-1": "audit", "PGU-2": "audit-2", "PGU-3": "audit"},
          f"the review waiting behind audit's current one goes to idle audit-2; the next waits, audit-2 now busy: {submitted}")
    check(submitted["idle_holding"].get("PGU-3") == "audit",
          f"idle audit-2, still holding PGU-2, is given no second review: {submitted['idle_holding']}")
    check(submitted["to_audit_2"] == ["PGU-2"] and submitted["to_audit"] == ["PGU-1"],
          f"audit-2 is handed it, and audit is handed only its current review: {submitted}")
    check(len(submitted["balance_notes"]) == 1 and "Board balance in audit: audit -> audit-2" in submitted["balance_notes"][0]
          and "audit is reviewing PGU-1" in submitted["balance_notes"][0],
          f"the move is recorded on the ticket, saying why: {submitted['balance_notes']}")
    freed = got["freed"]
    check(freed["owners"].get("PGU-3") == "audit-2" and freed["to_audit_2"] == ["PGU-3"] and freed["to_audit"] == [],
          f"once audit-2 has signed off and is idle, the next waiting review goes to it, and audit hears nothing: {freed}")
    never = got["never"]
    check(never["blocked"] == "audit" and never["manual"] == "audit",
          f"a blocked review and a manually controlled one stay where they are: {never}")
    for case in ("busy", "awaited", "provider_held", "process_gone"):
        check(set(never[case].values()) == {"audit"}, f"nothing moves to audit-2 when it is {case}: {never}")
    affinity = got["affinity"]
    check(affinity["begun"] and affinity["begun_owner"] == "audit" and affinity["other_owner"] == "audit-2",
          f"a re-review audit already began stays with audit, while another waiting review goes to audit-2: {affinity}")
    check(affinity["current"] == "audit", f"audit's current review is never taken: {affinity}")
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    mine = (ROOT / "scripts/ticket_board/migrations/pgu1033_syrd745_review_balance.sql").read_text()
    for name in ("enforce_declared_ticket_update", "balance_review_stage"):
        start = schema.rindex(f"CREATE OR REPLACE FUNCTION ticket_board.{name}(")
        check(schema[start:schema.index("\n$$;\n", schema.index("AS $$", start)) + 5] in mine,
              f"schema.sql's {name}, the one a board built from it runs, is pgu1033's")
    reassigned = got["reassigned"]
    check(reassigned["delivery"] == "delivered" and reassigned["to_audit_2"] == [reassigned["ticket"]],
          f"a direct reassignment to idle audit-2 is delivered on the next pass: {reassigned}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD745_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd745-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"review_balance_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
