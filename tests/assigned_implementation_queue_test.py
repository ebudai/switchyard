#!/usr/bin/env python3
"""SYRD-568: an implementer's assigned Implementation work is a visible queue, and only one ticket in it is active.

Live on syrd (2026-10-07): the Director could not keep SYRD-548/557/558/560/561/
563..566 in Implementation behind the active SYRD-550/556/559. The declared
update trigger diverted each second route to app/ops/main into analysis/director,
force-move and override-move were diverted the same way, and the cohort was
parked in manually controlled Analysis to keep its reminders quiet.

Production-built board (companion roles, schema.sql, the real ticket-board-
migrate, rbac.sql) with the example workflow and syrd's holding destination,
the real HTTP server, the real notify listener with an injected sender, and the
real nudge generators. The before-run is main's code and SQL, in a child built
from a git archive; an upgrade case builds main's board, diverts a ticket the old
way, and then applies this tree's migrations and rbac.sql to it.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "3832d97f8db1bed555733efee758938d8f3b4a37"  # main before SYRD-568
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


def build_board(fixture, cluster, root: Path, dbname: str) -> str:
    """A board as production builds one: schema.sql, roles, rbac.sql, every migration, rbac.sql again."""
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
    fixture.create_roles(admin)
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
    migrate(fixture, admin, root)
    return admin


def migrate(fixture, admin: str, root: Path) -> None:
    """An upgrade as production deploys one: every migration, then rbac.sql."""
    result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())


def workflow(root: Path) -> dict:
    cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}  # syrd's holding destination
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def act(ticket: str, operation: str, actor: str, **payload):
            try:
                return {"ok": fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)}
            except Exception as exc:  # a refusal is an answer
                return {"refused": str(exc)}

        def sql(statement: str, url: str = admin) -> str:
            return fixture.psql(url, statement).strip()

        def where(ticket: str) -> list:
            return sql(f"SELECT state || '|' || assignee || '|' || queued_for_assignee || '|' || queued_behind_ticket "
                       f"FROM ticket_board.tickets WHERE id = '{ticket}';").split("|")

        def reservations() -> dict:
            raw = sql("SELECT coalesce(json_object_agg(implementer, ticket_id), '{}')::text FROM ticket_board.serial_reservations();")
            return json.loads(raw)

        sent: list[tuple[str, str]] = []
        busy: set[str] = set()

        def new_listener():
            return TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=lambda target: target in busy, target_exists=lambda _target: True,
                                             submission_witness=lambda target, _since: any(t == target for t, _m in sent),
                                             poll_seconds=0, project="pgu")

        listener = new_listener()

        def one_pass(listener_now=None) -> list[tuple[str, str]]:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            current = listener_now or listener
            before = len(sent)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                current.refresh_workflow(conn)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    if hasattr(current, step):
                        getattr(current, step)(conn)
                current.process_due_notifications(conn)
            return sent[before:]

        def to_role(batch, role, ticket):
            return [m for t, m in batch if t == f"pgu-{role}:0.0" and m.startswith(ticket + " ")]

        def handoffs(batch, role, ticket):
            """Handoffs only: the in-place change notice an owner gets when the Director edits its ticket is not one."""
            return [m for m in to_role(batch, role, ticket) if " entered " in m or " is active again in " in m]

        def to_app(batch, ticket):
            return to_role(batch, "app", ticket)

        def queue_of(role: str) -> list:
            try:
                return json.loads(sql(
                    "SELECT coalesce(json_agg(json_build_array(ticket_id, queue_position, active, waiting_on) "
                    f"ORDER BY queue_position), '[]')::text FROM ticket_board.serial_queue() WHERE implementer = '{role}';"))
            except Exception as exc:  # main has no queue
                return [f"unavailable: {str(exc)[-80:]}"]

        def reminders(role: str, tickets) -> dict:
            """Every reminder the board would send, made due: a far-off "now", the role idle well before it."""
            later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            idle = json.dumps({role: (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()})
            sql(f"SELECT ticket_board.notify_due_nudges('{later}'::timestamptz);")  # run by the board's own schedule
            sql(f"SELECT ticket_board.notify_idle_stall_nudges('{idle}'::jsonb, '{later}'::timestamptz);", listener_url)
            counts = {t: int(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                 f"WHERE ticket_id = '{t}' AND kind <> 'transition';")) for t in tickets}
            sql("DELETE FROM ticket_board.ticket_notification_queue WHERE kind <> 'transition';")
            return counts

        A, B, C = "PGU-601", "PGU-602", "PGU-603"           # app's chain A -> B -> C
        D, E = "PGU-611", "PGU-612"                         # ops: two unblocked
        P, K = "PGU-621", "PGU-622"                         # main: parent P, child K
        F, G = "PGU-631", "PGU-632"                         # main again: Director overrides
        Z, H, M = "PGU-641", "PGU-642", "PGU-643"           # main: a blocker, a Director edit, a manual hold
        Q, R = "PGU-651", "PGU-652"                         # ops: Q waits on R, behind D
        for ticket in (A, B, C, D, E, P, K, F, G, Z, H, M, Q, R):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
        one_pass()
        sent.clear()

        # The chain first, so C and B arrive blocked; then the routes, C first.
        seen["chain"] = [act(B, "set_blockers", "director", blocked_by=[A], blocked_reason="after A"),
                         act(C, "set_blockers", "director", blocked_by=[B], blocked_reason="after B")]
        seen["routes"] = {t: act(t, "route", "director", state="in_progress", assignee="app") for t in (C, B, A)}
        seen["placed"] = {t: where(t) for t in (A, B, C)}
        seen["reservations_after_route"] = reservations()
        first = one_pass()
        seen["handoffs_after_route"] = {t: to_app(first, t) for t in (A, B, C)}
        seen["turn_resolved"] = {t: sql(f"SELECT ticket_board.ticket_turn_is_resolved('{t}', clock_timestamp());") for t in (A, B, C)}

        seen["reminders"] = reminders("app", (A, B, C))

        # The board shows one current ticket per role, and the queue in order.
        board = json.loads(fixture.get_text(base, "/api/board")) if hasattr(fixture, "get_text") else None
        if board is None:
            import urllib.request
            with urllib.request.urlopen(base + "/api/board") as response:
                board = json.loads(response.read())
        tickets = {t["id"]: t for t in board["tickets"]}
        seen["highlight"] = {t: tickets[t].get("active_work_highlight") for t in (A, B, C)}
        seen["serial_queue_field"] = {t: tickets[t].get("serial_queue") for t in (A, B, C)}
        try:
            seen["serial_queue"] = json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(implementer, ticket_id, queue_position, active, waiting_on) "
                "ORDER BY implementer, queue_position), '[]')::text FROM ticket_board.serial_queue() WHERE implementer = 'app';"))
        except Exception as exc:
            seen["serial_queue"] = f"unavailable: {str(exc)[-120:]}"

        # The same order through the status endpoint and the ticket view.
        import urllib.request
        with urllib.request.urlopen(base + "/api/reservations") as response:
            seen["status_queue"] = json.loads(response.read()).get("queues", {}).get("app")
        cli = subprocess.run([str(root / "scripts/ticket-board-read"), "ticket", C], capture_output=True, text=True, timeout=60,
                             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TICKET_BOARD_URL": base})
        lines = cli.stdout.splitlines()
        seen["cli_queue"] = lines[lines.index("Queue:") + 1].strip() if "Queue:" in lines else cli.stdout[-300:] + cli.stderr[-300:]

        # Blocked work cannot be submitted, whatever stage it sits in.
        seen["blocked_submit"] = act(B, "submit_to_audit_without_commit", "app", reason="done early")

        # Two unblocked tickets for ops: the second waits, and cannot be submitted.
        seen["ops_routes"] = {t: act(t, "route", "director", state="in_progress", assignee="ops") for t in (D, E)}
        seen["ops_placed"] = {t: where(t) for t in (D, E)}
        ops_batch = one_pass()
        seen["ops_handoffs"] = {t: [m for tgt, m in ops_batch if tgt == "pgu-ops:0.0" and m.startswith(t + " ")] for t in (D, E)}
        seen["ops_reminders"] = reminders("ops", (D, E))  # E waits unblocked: quiet all the same
        seen["waiting_submit"] = act(E, "submit_to_audit_without_commit", "ops", reason="not mine yet")
        seen["active_submit"] = act(D, "submit_to_audit_without_commit", "ops", reason="finished")
        # Unblocking work that still waits behind active work hands nothing over;
        # nor does a ticket put straight into a busy implementer's stage.
        act(Q, "set_blockers", "director", blocked_by=[R], blocked_reason="after R")
        act(Q, "route", "director", state="in_progress", assignee="ops")
        one_pass()
        seen["unblock_r"] = act(R, "force_move", "director", state="done", assignee="unassigned")
        unblocked_batch = one_pass()
        fixture.seed_postgres_ticket(admin, "PGU-653", title="PGU-653", state="in_progress", assignee="ops", commit_exempt=True)
        inserted_batch = one_pass()
        # Queued as well as delivered: the listener's admission holds a waiting
        # ticket's notice rather than sending it, so it would arrive later.
        pending = {t: int(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                              f"WHERE ticket_id = '{t}' AND kind = 'transition' AND target_role = 'ops' "
                              "AND dead_lettered_at IS NULL;")) for t in (Q, "PGU-653")}
        seen["unblocked_waiting"] = [where(Q), handoffs(unblocked_batch, "ops", Q), reservations().get("ops"),
                                     where("PGU-653"), to_role(inserted_batch, "ops", "PGU-653"), pending]

        # Parent and child, no blocker: the hierarchy orders and blocks nothing.
        sql(f"SELECT set_config('ticket_board.workflow_actor', 'director', false); "
            f"SELECT set_config('ticket_board.director_edit_target', '{K}', false); "
            f"UPDATE ticket_board.tickets SET parent_id = '{P}' WHERE id = '{K}';")
        seen["parent"] = sql(f"SELECT parent_id FROM ticket_board.tickets WHERE id = '{K}';")
        seen["family_routes"] = {t: act(t, "route", "director", state="in_progress", assignee="main") for t in (K, P)}
        seen["family_reservation"] = reservations().get("main")
        seen["family_blocked"] = sql(f"SELECT ticket_board.ticket_has_unresolved_blockers('{P}');")

        # A Director override into a busy implementer's stage lands there too.
        seen["force_move"] = act(F, "force_move", "director", state="in_progress", assignee="main")
        seen["force_placed"] = where(F)
        seen["override_move"] = act(G, "override_move", "director", state="in_progress", assignee="main")
        seen["override_placed"] = where(G)
        seen["main_reservation"] = reservations().get("main")

        # A session starting in main's pane is told its active ticket: the
        # child K, though the waiting parent P has the lower number. The real
        # hook, against this board, with every path it writes sandboxed.
        with tempfile.TemporaryDirectory(prefix="syrd568-hook.") as hook_home:
            hook = subprocess.run(
                [str(root / "scripts/ticket-board-pane-idle-hook"), "idle", "--target", "pgu-main:0.0",
                 "--source", "claude.SessionStart", "--state-dir", f"{hook_home}/state", "--session-dir", f"{hook_home}/sessions",
                 "--record-session"],
                input=json.dumps({"source": "compact", "session_id": "syrd568"}), text=True, capture_output=True, timeout=60,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": hook_home, "LANG": "C.UTF-8",
                     "TICKET_BOARD_URL": base})
            context = json.loads(hook.stdout or "{}").get("hookSpecificOutput", {}).get("additionalContext", "")
            seen["hook_active_work"] = context[context.find("ACTIVE work:"):][:40] if "ACTIVE work:" in context else context[:200] or hook.stderr[-300:]

        # Blocking the active ticket hands the slot on, once; clearing the
        # blocker does not take it back from the work now under way.
        one_pass()
        seen["block_active"] = act(K, "set_blockers", "director", blocked_by=[Z], blocked_reason="wait for Z")
        blocked_pass = one_pass()
        seen["after_block"] = [reservations().get("main"), {t: handoffs(blocked_pass, "main", t) for t in (P, K, F, G)}]
        seen["main_queue_blocked"] = queue_of("main")
        seen["unblock_active"] = act(K, "set_blockers", "director", blocked_by=[], blocked_reason="")
        unblocked_pass = one_pass() + one_pass(new_listener())
        seen["after_unblock"] = [reservations().get("main"), {t: handoffs(unblocked_pass, "main", t) for t in (P, K, F, G)}]
        import urllib.request
        with urllib.request.urlopen(base + "/api/board") as response:
            board_now = {t["id"]: t for t in json.loads(response.read())["tickets"]}
        seen["main_highlight"] = {t: board_now[t].get("active_work_highlight") for t in (P, K, F, G)}
        # A Director edit into the busy stage lands there too.
        seen["director_edit"] = act(H, "director_edit", "director", patch={"state": "in_progress", "assignee": "main"},
                                    reason="queue it")
        seen["edit_placed"] = where(H)
        # Manual control is the Director's own hold, not a queue: its handoff goes as it always did.
        seen["manual"] = act(M, "set_manually_controlled", "director", manually_controlled=True)
        seen["manual_route"] = act(M, "route", "director", state="in_progress", assignee="main")
        manual_pass = one_pass()
        held = sql("SELECT coalesce(string_agg(coalesce(last_error, ''), ','), 'none') FROM ticket_board.ticket_notification_queue "
                   f"WHERE ticket_id = '{M}' AND kind = 'transition' AND target_role = 'main' AND dead_lettered_at IS NULL;")
        seen["manual_after"] = [where(M), handoffs(manual_pass, "main", M), held, reservations().get("main")]

        # A goes all the way to done. In review it still holds app; at done B is
        # handed over exactly once, and C still waits on B.
        def complete(ticket: str, author: str) -> list:
            steps = [act(ticket, "submit_to_audit_without_commit", author, reason="finished"),
                     act(ticket, "audit_sign_off", "audit", text="Audited.")]
            seen.setdefault("review_holds", {})[ticket] = reservations().get(author)
            steps.append(act(ticket, "mark_done", "director"))
            return steps

        sent.clear()
        seen["a_steps"] = complete(A, "app")
        seen["a_where"] = where(A)
        after_a = one_pass()
        seen["after_a"] = {t: to_app(after_a, t) for t in (B, C)}
        seen["reservations_after_a"] = reservations()
        # The listener stops; a new one, and later passes, send nothing more.
        restarted = one_pass(new_listener()) + one_pass() + one_pass()
        seen["after_restart"] = {t: to_app(restarted, t) for t in (B, C)}
        seen["b_sends"] = sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'send' "
                              f"AND ticket_id = '{B}' AND target_role = 'app' AND kind = 'transition';")

        seen["b_steps"] = complete(B, "app")
        after_b = one_pass() + one_pass(new_listener())
        seen["after_b"] = {t: to_app(after_b, t) for t in (B, C)}
        seen["reservations_after_b"] = reservations()
        seen["c_sends"] = sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'send' "
                              f"AND ticket_id = '{C}' AND target_role = 'app' AND kind = 'transition';")
        # Who may run the new functions, and a fresh schema.sql board installs the same text.
        new = ("serial_queue()", "settle_serial_focus(text, boolean, text)", "ticket_serial_waiting(text)",
               "ticket_current_reserved_ticket(text, text)", "enforce_declared_ticket_update(ticket_board.tickets, ticket_board.tickets)",
               "finish_current_stage_blocker(text, text, text, timestamp with time zone, interval)",
               "notify_unblocked_dependents()", "notify_ticket_state_transition()", "force_move(text, text, text, boolean)",
               "ticket_serial_focus_reservation_is_current(text, text, text)")
        try:
            seen["grantees"] = {f: sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, '+' "
                                       "ORDER BY CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END), '') "
                                       f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{f}'::regprocedure "
                                       "AND a.privilege_type = 'EXECUTE';") for f in new[:2]}
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            definition = "SELECT md5(pg_get_functiondef('ticket_board.{}'::regprocedure));"
            seen["fresh_equals_migrated"] = {f: fixture.psql(fresh, definition.format(f)).strip() == sql(definition.format(f)) for f in new}
            triggers = ("SELECT string_agg(tgname || ':' || tgfoid::regproc::text, ',' ORDER BY tgname) FROM pg_trigger "
                        "WHERE tgname LIKE '%settle_serial_focus%';")
            seen["fresh_triggers"] = [fixture.psql(fresh, triggers).strip(), sql(triggers)]
        except Exception as exc:  # main has none of them
            seen["grantees"] = seen["fresh_equals_migrated"] = f"unavailable: {str(exc)[-160:]}"
        server.shutdown()
        server.server_close()
    return seen


def upgrade(before_root: Path, prefix: str) -> dict:
    """main's board with a ticket diverted the old way, then this tree's migrations and rbac.sql."""
    sys.path.insert(0, str(ROOT / "tests"))
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, before_root, "old")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "old", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        app.apply_workflow(workflow(before_root), expected_revision=0, dry_run=False, caller_role="director")
        X, Y, Z = "PGU-701", "PGU-702", "PGU-703"
        for ticket in (X, Y, Z):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
        for ticket in (X, Y):
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": "app"},
                              caller="director")
        server.shutdown()
        server.server_close()

        def where(ticket: str) -> list:
            return fixture.psql(admin, f"SELECT state || '|' || assignee || '|' || queued_for_assignee || '|' || queued_behind_ticket "
                                       f"FROM ticket_board.tickets WHERE id = '{ticket}';").strip().split("|")

        seen["diverted_before"] = where(Y)
        # Every notice so far was delivered: the migration owes nobody one.
        fixture.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        queued = int(fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue;").strip())
        migrate(fixture, admin, ROOT)
        seen["queue_rows_added"] = int(fixture.psql(admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue;").strip()) - queued
        seen["focus"] = fixture.psql(admin, "SELECT coalesce(json_object_agg(implementer, ticket_id), '{}')::text "
                                            "FROM ticket_board.serial_focus;").strip()
        seen["diverted_after"] = where(Y)
        seen["diverted_quiet"] = fixture.psql(admin, f"SELECT ticket_board.ticket_serial_focus_reservation_is_current("
                                                     f"'{Y}', 'app', '{X}');").strip()

        app = fixture.TicketBoardApp(cluster.root / "frames2", cluster.root / "assets2", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "old", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        for ticket in (Y, Z):
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": "app"},
                              caller="director")
        seen["rerouted"] = {t: where(t) for t in (Y, Z)}
        seen["reservation"] = fixture.psql(admin, "SELECT ticket_id FROM ticket_board.serial_reservations() "
                                                  "WHERE implementer = 'app';").strip()
        server.shutdown()
        server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    env = clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "s568b")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd568.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
        upgraded = upgrade(before_root, "s568u")

    A, B, C, D, E, P, K, F, G = "PGU-601", "PGU-602", "PGU-603", "PGU-611", "PGU-612", "PGU-621", "PGU-622", "PGU-631", "PGU-632"
    Z, M, Q = "PGU-641", "PGU-643", "PGU-651"
    # main: blocked work could not be placed with its implementer at all; a
    # second unblocked route, and both Director overrides, were diverted; and
    # when A finished, app was left with nothing.
    check(all("unresolved blocker prevents forward promotion" in before["routes"][t].get("refused", "") for t in (B, C))
          and before["placed"][B][:2] == ["analysis", "director"] and before["placed"][C][:2] == ["analysis", "director"],
          f"before: blocked B and C could not be routed to app: {before['placed']}")
    check(before["ops_placed"][E] == ["analysis", "director", "ops", D] and before["family_routes"][P]["ok"]["ticket"]["state"] == "analysis",
          f"before: a second route to a busy implementer was diverted to analysis/director: {before['ops_placed']}")
    check(before["force_placed"][0] == "backlog" and before["override_placed"][0] == "backlog",
          f"before: Director overrides were diverted to backlog: {before['force_placed']} {before['override_placed']}")
    check(before["reservations_after_a"].get("app") is None and not before["after_a"][B],
          f"before: finishing A activated nothing: {before['reservations_after_a']}")

    after = scenario(ROOT, "s568a")
    # 1. Three ordinary routes; all three stay assigned in Implementation.
    check(all("ok" in r for r in after["routes"].values()), f"the ordinary route succeeds: {after['routes']}")
    check(all(after["placed"][t] == ["in_progress", "app", "", ""] for t in (A, B, C)),
          f"all three stay in Implementation, assigned to app, with no diversion markers: {after['placed']}")
    # 2. Only A holds the slot, is handed over, highlighted -- and reminded.
    check(after["reservations_after_route"].get("app") == A, f"serial_reservations reports only A: {after['reservations_after_route']}")
    check(len(after["handoffs_after_route"][A]) == 1 and not after["handoffs_after_route"][B] and not after["handoffs_after_route"][C],
          f"only A is handed over: {after['handoffs_after_route']}")
    check(after["turn_resolved"] == {A: "f", B: "t", C: "t"}, f"only A owes a turn: {after['turn_resolved']}")
    check(after["reminders"][A] > 0 and after["reminders"][B] == 0 and after["reminders"][C] == 0,
          f"due reminders go to A and to nothing queued behind it: {after['reminders']}")
    check(after["highlight"] == {A: True, B: False, C: False}, f"the board highlights A alone: {after['highlight']}")
    check([row[1:4] for row in after["serial_queue"]] == [[A, 1, True], [B, 2, False], [C, 3, False]]
          and after["serial_queue"][1][4] == [A] and after["serial_queue"][2][4] == [B],
          f"the queue is published in order, with what each waits on: {after['serial_queue']}")
    sq = after["serial_queue_field"]
    check(sq[A] and sq[A]["active"] is True and sq[B] and sq[B]["position"] == 2 and sq[B]["active_ticket"] == A
          and sq[C]["waiting_on"] == [B], f"each ticket shows its place in the queue: {sq}")
    check([(q["ticket"], q["position"], q["active"], q["waiting_on"]) for q in after["status_queue"] or []]
          == [(A, 1, True, []), (B, 2, False, [A]), (C, 3, False, [B])], f"the status endpoint publishes it: {after['status_queue']}")
    check(after["cli_queue"] == f"#3 in app's queue, behind {A}, blocked by {B}", f"the ticket view says where C stands: {after['cli_queue']}")
    sys.path.insert(0, str(ROOT / "tests"))
    from active_work_delivery_frontend_test import function_source

    program = function_source("serialQueueText") + """
process.stdout.write(JSON.stringify(%s.map((queue) => serialQueueText(queue))));
""" % json.dumps([sq[A], sq[C], None])
    node = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    check(node.returncode == 0 and json.loads(node.stdout) == ["Queue: app's active ticket",
                                                               f"Queue: #3 in app's queue, behind {A}, blocked by {B}", ""],
          f"the board's ticket view says the same: {node.stdout} {node.stderr[-300:]}")
    check("const queueText = serialQueueText(ticket.serial_queue);" in function_source("renderDetail"),
          "and renderDetail shows it")
    # 4. Neither blocked nor waiting work can be submitted; active work can.
    check("refused" in after["blocked_submit"] and "unresolved blocker" in after["blocked_submit"]["refused"],
          f"blocked work cannot be submitted: {after['blocked_submit']}")
    check(after["ops_placed"] == {D: ["in_progress", "ops", "", ""], E: ["in_progress", "ops", "", ""]}
          and len(after["ops_handoffs"][D]) == 1 and not after["ops_handoffs"][E],
          f"two unblocked tickets: both assigned, the first handed over: {after['ops_placed']} {after['ops_handoffs']}")
    check("refused" in after["waiting_submit"] and f"{E} is waiting in ops" in after["waiting_submit"]["refused"]
          and f"queue behind {D}; it can be submitted once it is the active ticket" in after["waiting_submit"]["refused"],
          f"waiting work cannot be submitted: {after['waiting_submit']}")
    check("ok" in after["active_submit"], f"active work can: {after['active_submit']}")
    # 5. Hierarchy alone neither blocks nor orders.
    check(after["parent"] == P and after["family_blocked"] == "f" and after["family_reservation"] == K,
          f"a child routed first is active, its parent waits only as the queue's second: {after['family_reservation']}")
    # Overrides land in place too.
    check(after["force_placed"] == ["in_progress", "main", "", ""] and after["override_placed"] == ["in_progress", "main", "", ""],
          f"force-move and override-move land in Implementation: {after['force_placed']} {after['override_placed']}")
    check(after["main_reservation"] == K, f"and do not take the slot from the active ticket: {after['main_reservation']}")
    check(after["unblocked_waiting"] == [["in_progress", "ops", "", ""], [], D, ["in_progress", "ops", "", ""], [], {Q: 0, "PGU-653": 0}],
          f"unblocked or inserted work that still waits behind D is handed nothing: {after['unblocked_waiting']}")
    check(after["ops_reminders"][D] > 0 and after["ops_reminders"][E] == 0,
          f"a ticket waiting unblocked behind active work is not reminded: {after['ops_reminders']}")
    check(after["after_block"][0] == P and len(after["after_block"][1][P]) == 1
          and not any(after["after_block"][1][t] for t in (K, F, G)),
          f"blocking the active ticket hands the slot to the next, once: {after['after_block']}")
    check([row[:3] for row in after["main_queue_blocked"]] == [[P, 1, True], [F, 2, False], [G, 3, False], [K, 4, False]]
          and after["main_queue_blocked"][3][3] == [Z],
          f"blocked work is listed after work that can start: {after['main_queue_blocked']}")
    check(after["after_unblock"][0] == P and not any(after["after_unblock"][1].values()),
          f"clearing the blocker does not take the slot back, and hands nothing over: {after['after_unblock']}")
    check(after["main_highlight"] == {P: True, K: False, F: False, G: False},
          f"the board highlights the active ticket, not the waiting one that arrived first: {after['main_highlight']}")
    check("ok" in after["director_edit"] and after["edit_placed"] == ["in_progress", "main", "", ""],
          f"a Director edit lands in Implementation: {after['director_edit']} {after['edit_placed']}")
    check(after["manual_after"][0] == ["in_progress", "main", "", ""] and len(after["manual_after"][1]) == 1
          and after["manual_after"][2] == "none" and after["manual_after"][3] == P,
          f"a manually controlled ticket is handed over at once, as before, and holds nothing: {after['manual_after']}")
    check(before["manual_after"][0][:2] == ["in_progress", "main"] and len(before["manual_after"][1]) == 1,
          f"as it was on main: {before['manual_after']}")
    check(after["hook_active_work"].startswith(f"ACTIVE work: {K} -- "),
          f"a new session in main's pane is told its active ticket, not the lowest-numbered one: {after['hook_active_work']}")
    # 3. A's review still holds app; at done B is handed over once, C still waits.
    check(all("ok" in s for s in after["a_steps"]) and after["a_where"][0] == "done", f"A completes: {after['a_steps']} {after['a_where']}")
    check(after["review_holds"][A] == A, f"A in review still holds app: {after['review_holds']}")
    check(len(after["after_a"][B]) == 1 and after["after_a"][B][0].startswith(f"{B} -- {B} entered Implementation")
          and not after["after_a"][C],
          f"completing A hands B over once (its unblock is its handoff), and not C: {after['after_a']}")
    check(after["reservations_after_a"].get("app") == B, f"B holds app: {after['reservations_after_a']}")
    check(not after["after_restart"][B] and not after["after_restart"][C] and after["b_sends"] == "1",
          f"a restarted listener sends nothing again: {after['after_restart']} sends={after['b_sends']}")
    check(all("ok" in s for s in after["b_steps"]) and len(after["after_b"][C]) == 1 and not after["after_b"][B]
          and after["c_sends"] == "1" and after["reservations_after_b"].get("app") == C,
          f"completing B hands C over once: {after['after_b']} sends={after['c_sends']} {after['reservations_after_b']}")
    # Upgrade: an old diversion survives, stays quiet, and routes in place.
    check(upgraded["diverted_before"][:3] == ["analysis", "director", "app"], f"main diverted Y: {upgraded['diverted_before']}")
    check(upgraded["queue_rows_added"] == 0 and json.loads(upgraded["focus"]) == {"app": "PGU-701"},
          f"the migration records today's slot and sends nothing: {upgraded}")
    check(upgraded["diverted_after"] == upgraded["diverted_before"] and upgraded["diverted_quiet"] == "t",
          f"an old diversion keeps its markers and stays quiet while its reservation holds: {upgraded['diverted_after']}")
    check(upgraded["rerouted"] == {"PGU-702": ["in_progress", "app", "", ""], "PGU-703": ["in_progress", "app", "", ""]}
          and upgraded["reservation"] == "PGU-701",
          f"the Director's ordinary route now places it in app's queue: {upgraded['rerouted']} {upgraded['reservation']}")
    # Copy parity and grants last: a check that the two copies agree must not stand in for one of behaviour.
    # A mutation run edits the migration's copy alone, so it sets SYRD568_MUTATION_SKIP_COPY_PARITY to
    # reach the next suite instead of stopping here on a disagreement it caused.
    if os.environ.get("SYRD568_MUTATION_SKIP_COPY_PARITY") == "1":
        print(f"assigned_implementation_queue_test: {CHECKS} checks ok (copy parity skipped for a mutation run)")
        return 0
    check(after["grantees"] == {"serial_queue()": "postgres+ticket_board_listener+ticket_board_service",
                                "settle_serial_focus(text, boolean, text)": "postgres"},
          f"the queue is readable by the board and its listener; settling is the triggers' alone: {after['grantees']}")
    check(all(after["fresh_equals_migrated"].values()) and after["fresh_triggers"][0] == after["fresh_triggers"][1]
          and after["fresh_triggers"][0].count("settle_serial_focus") == 6,
          f"a fresh schema.sql board installs the same functions and triggers: {after['fresh_equals_migrated']} {after['fresh_triggers']}")
    print(f"assigned_implementation_queue_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
