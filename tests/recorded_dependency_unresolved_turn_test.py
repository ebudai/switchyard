#!/usr/bin/env python3
"""SYRD-619: a recorded dependency still let the unresolved-turn prompt and its escalation through.

SYRD-609 sat in director_review with `awaiting_role=user`, recorded through
`request-dependency` at 03:16:35 UTC, 32 seconds after the Director's
unresolved-turn prompt. At 07:17 UTC the Director was told "director ended a
turn without resolving SYRD-609 (director_review), and the grace period has
passed after being prompted it is still unresolved". The prompts came back on
every turn end until the wait was released at 11:06.

Two causes. A wait counted only while ticket_awaiting_role_is_active, which
lapses after four hours. And the escalation re-raised the prompt the dependency
had answered, because nothing recorded that the wait came after it.

This suite runs on a production-built board (companion roles, schema.sql, the
real migrate runner, rbac.sql, the inspection workflow), with the real notify
listener driven one loop iteration at a time and the real PaneActivityGate
reading real hook files. Only the gate's live-pane probes are the scenario's.
The grace is a few seconds and passes for real. A wait's four hours are the one
thing not waited out: once it is recorded, its start (awaiting_since_at) is
moved back four hours and a minute. The cases:

- **U, SYRD-609's shape:** a director_review ticket waits on the User, past four
  hours, through several unresolved Director turns. The Director then clears the
  wait.
- **H, delivery:** an implementer's prompt is held behind a menu while the owner
  records a wait on another role (ops), which then passes four hours. The pane
  comes back, and later the owner clears the wait.
- **C, inside four hours:** a wait on the User is recorded after the prompt and
  cleared after the prompt's grace.
- **E, inside the grace:** a wait recorded and cleared within the prompt's
  grace; the next unresolved turn is prompted.
- **X, the upgrade (this tree only):** the migration's backfill keeps a wait
  recorded before it as the answer to the prompt before it; and a prompt main
  queued after the four-hour lapse, still on the board when it upgrades, does
  not escalate while the wait stands.

It runs on main, from a git archive in a child process, and on this tree.
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
BEFORE = "3ab25df988fc6112876cbd932bafc725bec6d85f"  # main before SYRD-619
GRACE = 4.0
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board.notify_listener import ActivityTrace, PaneActivityGate, PaneHookStateStore, TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster

    class Gate(PaneActivityGate):
        """The real gate over real hook files; only what it would read from tmux and /proc is the scenario's."""

        def __init__(self, store) -> None:
            super().__init__(state_store=store)
            self.busy: dict[str, str] = {}  # target -> why the pane looks busy

        def _full_activity_trace(self, target, check_trusted_working=False):
            reason = self.busy.get(target)
            return ActivityTrace(reason is not None, reason or "scenario_idle")

        def background_work_roles(self, roles=None):
            return set()

        def composer_snapshot(self, target):
            return None

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as panes:
        dbname = "board"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert result.returncode == 0, result.stderr[-1500:]
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")

        def act(ticket: str, operation: str, actor: str, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        store = PaneHookStateStore(panes)
        gate = Gate(store)
        sent: list[tuple[str, str]] = []
        listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.is_working, target_exists=lambda _target: True,
                                             submission_witness=lambda _target, _since: True, poll_seconds=0, project="pgu")
        listener.unresolved_turn_grace_seconds = GRACE

        def target(role: str) -> str:
            return f"pgu-{role}:0.0"

        def hook(role: str, state: str, event: str) -> None:
            store.write(target(role), state, source=f"codex.{event}")

        def turn(role: str) -> None:
            """A short turn that ends without resolving anything."""
            hook(role, "busy", "UserPromptSubmit")
            time.sleep(0.05)
            hook(role, "idle", "Stop")

        def one_pass(menu_after: str | None = None) -> list[str]:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            before = len(sent)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                gate.role_targets = dict(listener.role_targets)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    if hasattr(listener, step):
                        getattr(listener, step)(conn)
                if menu_after:
                    gate.busy[target(menu_after)] = "provider_menu"
                listener.process_due_notifications(conn)
            return [message for _target, message in sent[before:]]

        def escalations(ticket: str) -> list[str]:
            return [m for t, m in sent if t == target("director") and f"without resolving {ticket} " in m]

        def prompts(ticket: str, role: str) -> list[str]:
            return [m for t, m in sent if t == target(role) and m.startswith(f"{ticket} is still yours")]

        def enqueued(ticket: str) -> int:
            """Unresolved-turn notices the generator has queued for `ticket`, delivered or not."""
            return int(sql("SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = "
                           f"'{ticket}' AND kind IN ('unresolved_turn_repair', 'unresolved_turn') AND event = 'enqueue';"))

        def watch(seconds: float) -> None:
            """Passes, once a second, for `seconds`."""
            start = time.time()
            while time.time() - start < seconds:
                one_pass()
                time.sleep(1.0)

        def unresolved_turns(role: str) -> None:
            """Two unresolved turns, each followed by more than a grace of quiet."""
            for _ in range(2):
                turn(role)
                watch(GRACE + 2)

        def age_wait(ticket: str) -> None:
            """The wait has been recorded for four hours and a minute."""
            sql("UPDATE ticket_board.ticket_notification_state SET "
                "awaiting_since_at = awaiting_since_at - interval '4 hours 1 minute', "
                "awaiting_notified_since_at = awaiting_notified_since_at - interval '4 hours 1 minute' "
                f"WHERE ticket_id = '{ticket}';")

        def waiting(ticket: str) -> str:
            return sql(f"SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket}';")

        def resume(ticket: str, role: str, key: str) -> None:
            """After the wait clears: a grace with no turn, then one unresolved turn and a silent owner."""
            before = (len(escalations(ticket)), len(prompts(ticket, role)))
            watch(GRACE + 2)
            seen[f"{key}_after_clear"] = escalations(ticket)[before[0]:]
            turn(role)
            one_pass()
            delivered = time.time()
            seen[f"{key}_resumed_prompts"] = prompts(ticket, role)[before[1]:]
            watch(GRACE + 3)
            seen[f"{key}_resumed_escalations"] = escalations(ticket)[before[0]:]
            told = sql("SELECT extract(epoch FROM max(ts))::text FROM ticket_board.notification_trace WHERE ticket_id = "
                       f"'{ticket}' AND target_role = 'director' AND kind = 'unresolved_turn' AND event = 'enqueue';")
            seen[f"{key}_delivery_to_escalation"] = float(told) - delivered if told else None

        for role in ("director", "main", "app", "ops"):
            hook(role, "idle", "Stop")
        one_pass()

        # U: SYRD-609. The Director's own review waits on the User, past four hours.
        T = "PGU-609"
        fixture.seed_postgres_ticket(admin, T, title=T, state="director_review", assignee="director", commit_exempt=True)
        one_pass()  # handed over: its "entered Director review" notice delivered (SYRD-513)
        turn("director")
        one_pass()
        seen["u_prompted"] = prompts(T, "director")
        act(T, "request_dependency", "director", role="user",
            reason="Approve the pending polkit authentication dialog.")
        seen["u_waiting"] = waiting(T)
        age_wait(T)
        queued_before, sent_before = enqueued(T), len(sent)
        unresolved_turns("director")
        seen["u_while_waiting"] = [m for t, m in sent[sent_before:] if T in m]
        seen["u_enqueued_while_waiting"] = enqueued(T) - queued_before
        seen["u_still_waiting"] = waiting(T)
        act(T, "clear_awaiting_role", "director")
        resume(T, "director", "u")

        # H: the owner's prompt is held behind a menu, and the wait is recorded while it is.
        T = "PGU-610"
        fixture.seed_postgres_ticket(admin, T, title=T, state="analysis", assignee="director", commit_exempt=True)
        act(T, "route", "director", state="in_progress", assignee="app")
        one_pass()
        turn("app")
        one_pass(menu_after="app")
        seen["h_held"] = prompts(T, "app")
        act(T, "request_dependency", "app", role="ops", reason="The VM image is ops's to build.")
        age_wait(T)
        gate.busy.pop(target("app"))
        sent_before = len(sent)
        unresolved_turns("app")
        seen["h_while_waiting"] = [m for t, m in sent[sent_before:] if T in m]
        # Queued, not delivered: moving the wait's start makes its handoff stale, which is this test's doing.
        seen["h_ops_handed"] = sql("SELECT string_agg(target_role || ':' || (detail #>> '{payload,step}'), ' ' ORDER BY ts) "
                                   "FROM ticket_board.notification_trace WHERE ticket_id = "
                                   f"'{T}' AND kind = 'awaiting_role' AND event = 'enqueue';")
        seen["h_repair_events"] = sql("SELECT string_agg(event, ' ' ORDER BY ts) FROM ticket_board.notification_trace "
                                      f"WHERE ticket_id = '{T}' AND kind = 'unresolved_turn_repair' "
                                      "AND event IN ('drop', 'send', 'send_unconfirmed');")
        act(T, "clear_awaiting_role", "app")
        resume(T, "app", "h")

        # C: inside four hours. The wait answers the prompt, and is cleared after the prompt's grace.
        T = "PGU-611"
        fixture.seed_postgres_ticket(admin, T, title=T, state="analysis", assignee="director", commit_exempt=True)
        act(T, "route", "director", state="in_progress", assignee="main")
        one_pass()
        turn("main")
        one_pass()
        seen["c_prompted"] = prompts(T, "main")
        act(T, "request_dependency", "main", role="user", reason="Which of the two layouts does the User want?")
        watch(1.5 * GRACE)
        seen["c_while_waiting"] = escalations(T)
        act(T, "clear_awaiting_role", "main")
        resume(T, "main", "c")

        # E: recorded and cleared within the prompt's grace. The prompt was answered; the next turn is news.
        T = "PGU-612"
        fixture.seed_postgres_ticket(admin, T, title=T, state="analysis", assignee="director", commit_exempt=True)
        act(T, "route", "director", state="in_progress", assignee="ops")
        one_pass()
        turn("ops")
        one_pass()
        seen["e_prompted"] = prompts(T, "ops")
        act(T, "request_dependency", "ops", role="user", reason="A quick question for the User.")
        act(T, "clear_awaiting_role", "ops")
        turn("ops")
        one_pass()
        seen["e_reprompted"] = prompts(T, "ops")

        migration = root / "scripts/ticket_board/migrations/pgu1009_syrd619_recorded_dependency_answers_turn.sql"
        if migration.exists():
            def upgrade(ticket: str) -> None:
                """The migration as an upgrade runs it over a board whose wait predates the column."""
                sql(f"UPDATE ticket_board.ticket_notification_state SET wait_recorded_at = NULL WHERE ticket_id = '{ticket}';")
                fixture.psql(admin, migration.read_text())

            # app takes one ticket at a time (serial focus); H's is finished with.
            act("PGU-610", "cancel", "director")
            one_pass()

            # X1: SYRD-609 at the upgrade -- prompted, then waiting. Cleared after the upgrade, nothing escalates.
            T = "PGU-613"
            fixture.seed_postgres_ticket(admin, T, title=T, state="analysis", assignee="director", commit_exempt=True)
            act(T, "route", "director", state="in_progress", assignee="app")
            one_pass()
            turn("app")
            one_pass()
            seen["x1_prompted"] = prompts(T, "app")
            act(T, "request_dependency", "app", role="user", reason="Waiting on the User's choice.")
            upgrade(T)
            seen["x1_backfilled"] = sql("SELECT (wait_recorded_at = awaiting_since_at)::text FROM "
                                        f"ticket_board.ticket_notification_state WHERE ticket_id = '{T}';")
            act(T, "clear_awaiting_role", "app")
            before_clear = len(escalations(T))
            watch(GRACE + 2)
            seen["x1_after_clear"] = escalations(T)[before_clear:]

            act("PGU-613", "cancel", "director")
            one_pass()

            # X2: past the lapse, main queued a prompt after the wait; the board then upgrades.
            T = "PGU-614"
            fixture.seed_postgres_ticket(admin, T, title=T, state="analysis", assignee="director", commit_exempt=True)
            act(T, "route", "director", state="in_progress", assignee="app")
            one_pass()
            act(T, "request_dependency", "app", role="user", reason="Waiting on the User's sign-off.")
            age_wait(T)
            sql("SELECT ticket_board.enqueue_notification('PGU-614', 'unresolved_turn_repair', 'app', "
                "'PGU-614 is still yours and this turn ended without resolving it.', jsonb_build_object("
                "'kind', 'unresolved_turn_repair', 'id', 'PGU-614', 'state', 'in_progress', 'assignee', 'app', "
                "'owner_role', 'app', 'target_role', 'app', 'turn_id', 'after-the-lapse', 'grace', 4), "
                "'repair:' || ticket_board.turn_unresolved_identity('PGU-614', 'in_progress', 'app', 'after-the-lapse'));")
            upgrade(T)
            watch(GRACE + 3)
            seen["x2_while_waiting"] = [m for t, m in sent if "PGU-614" in m and ("still yours" in m or "without resolving" in m)]
            seen["x2_enqueued"] = enqueued(T)

        # The same objects on a fresh install as on this migrated board.
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
        shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                 "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                 "FROM pg_proc p WHERE p.proname IN ('notify_unresolved_turn_end', 'ticket_turn_end_is_resolved', "
                 "'stamp_wait_recorded') AND p.pronamespace = 'ticket_board'::regnamespace;")
        triggers = ("SELECT coalesce(string_agg(tgname || ' ' || pg_get_triggerdef(oid), ' | ' ORDER BY tgname), '') FROM pg_trigger "
                    "WHERE tgrelid = 'ticket_board.ticket_notification_state'::regclass AND NOT tgisinternal;")
        seen["fresh_shape"] = [json.loads(fixture.psql(fresh, shape)), fixture.psql(fresh, triggers).strip()]
        seen["migrated_shape"] = [json.loads(sql(shape)), sql(triggers)]
        server.shutdown()
        server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd619b-")))
        return 0
    if not os.environ.get("SYRD619_MUTATION_SKIP_BEFORE"):
        with tempfile.TemporaryDirectory(prefix="syrd619.") as tmp:
            before_root = tree_at(BEFORE, Path(tmp) / "before")
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                                   capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        check(len(before["u_prompted"]) == 1 and before["u_waiting"] == "user" and before["u_still_waiting"] == "user"
              and any("without resolving PGU-609 (director_review)" in m and "the grace period has passed" in m
                      for m in before["u_while_waiting"]),
              f"main: past four hours, a ticket still recorded as waiting on the User told the Director its owner had "
              f"not resolved it: {before['u_while_waiting']}")
        check(before["h_repair_events"].split()[:1] == ["send"],
              f"main: a prompt held while the owner recorded a wait was delivered once the wait passed four hours: "
              f"{before['h_repair_events']} {before['h_while_waiting']}")
        check(before["c_while_waiting"] == [] and len(before["c_after_clear"]) == 1,
              f"main: inside four hours the wait held, but clearing it escalated the prompt it had answered, at once: "
              f"{before['c_after_clear']}")

    a = scenario(ROOT, "syrd619a-")
    for key, role, ticket in (("u", "director", "PGU-609"), ("h", "app", "PGU-610"), ("c", "main", "PGU-611")):
        if key != "h":
            check(len(a[f"{key}_prompted"]) == 1, f"{key.upper()}: an unresolved turn prompts its owner: {a[f'{key}_prompted']}")
        check(a[f"{key}_after_clear"] == [],
              f"{key.upper()}: once the wait clears, the prompt it answered is not escalated: {a[f'{key}_after_clear']}")
        check(len(a[f"{key}_resumed_prompts"]) == 1 and len(a[f"{key}_resumed_escalations"]) == 1
              and GRACE - 0.5 <= a[f"{key}_delivery_to_escalation"] <= GRACE + 2.5,
              f"{key.upper()}: ordinary handling resumes -- the next unresolved turn prompts {role}, and a grace after "
              f"delivery the Director is told once: {a[f'{key}_resumed_prompts']} {a[f'{key}_resumed_escalations']} "
              f"{a[f'{key}_delivery_to_escalation']}")
    check(a["u_waiting"] == "user" and a["u_still_waiting"] == "user" and a["u_while_waiting"] == []
          and a["u_enqueued_while_waiting"] == 0,
          f"U: past four hours, through unresolved Director turns, a ticket waiting on the User queues and sends "
          f"nothing about an unresolved turn: {a['u_while_waiting']} ({a['u_enqueued_while_waiting']} queued)")
    check(a["h_held"] == [] and a["h_while_waiting"] == [] and a["h_repair_events"].split()[:1] == ["drop"],
          f"H: the prompt held when the wait was recorded is dropped, not delivered, once the pane is back, and "
          f"nothing follows past four hours: {a['h_repair_events']} {a['h_while_waiting']}")
    check(a["h_ops_handed"] == "ops:1 ops:2 ops:3 director:4",
          f"H: the wait's own path is armed as before -- ops is handed it, then the Director: {a['h_ops_handed']}")
    check(a["c_while_waiting"] == [], f"C: inside four hours nothing escalates while it waits: {a['c_while_waiting']}")
    check(len(a["e_prompted"]) == 1 and len(a["e_reprompted"]) == 2,
          f"E: a wait recorded and cleared inside the prompt's grace ends its episode, so the next unresolved turn "
          f"is prompted: {a['e_reprompted']}")
    check(len(a["x1_prompted"]) == 1 and a["x1_backfilled"] == "true" and a["x1_after_clear"] == [],
          f"X1: the upgrade's backfill dates a standing wait from when it began, so the prompt it answered does not "
          f"escalate once it clears: {a['x1_after_clear']}")
    check(a["x2_while_waiting"] == [] and a["x2_enqueued"] == 1,
          f"X2: a prompt main queued past the lapse, still on the board at the upgrade, is neither delivered nor "
          f"escalated while the wait stands: {a['x2_while_waiting']} ({a['x2_enqueued']} queued)")
    check(a["migrated_shape"] == a["fresh_shape"] and len(a["migrated_shape"][0]) == 3
          and "ticket_board_listener=X" in dict((s[0], s[3]) for s in a["migrated_shape"][0])[
              "ticket_board.ticket_turn_end_is_resolved(text,timestamp with time zone)"]
          and "ticket_notification_state_stamp_wait" in a["migrated_shape"][1],
          f"schema.sql and rbac.sql install what the migration does -- functions, owner, grants, trigger: "
          f"{a['migrated_shape']} {a['fresh_shape']}")
    print(f"recorded_dependency_unresolved_turn_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
