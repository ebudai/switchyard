#!/usr/bin/env python3
"""SYRD-594: a Codex turn that ends on "Selected model is at capacity" is retried, then escalated.

On otto (2026-10-16, release c9fd9c6) the uiux pane's Codex 0.163.0 turn ended
on "Selected model is at capacity. Please try a different model." and the pane
sat idle at its prompt until the User nudged it by hand.

The screens are real (tests/fixtures/syrd594_codex_capacity_screens.json):
Codex 0.163.0, offline, against a local fake model server that answered
server_is_overloaded. Codex draws the error at the margin, returns to its empty
prompt, and runs no Stop hook: the hook state stays the turn's own
UserPromptSubmit. Each gate case runs the real PaneActivityGate with tmux
standing in for its captures. The board cases use a production-built board,
the real server, the real listener and the real gate, and every pass builds a
new listener and a new gate: each pass is a listener restart. Time passing is
the hook state and the board's recorded failure times moved back together.

The before-run is BEFORE (otto's release), in a child process built from a git
archive.
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
BEFORE = "c9fd9c6350751e32446281bc1abde79a39503d5b"  # otto's shared release when uiux stalled
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
WORKER = "pgu-main:0.0"  # the example workflow's Codex implementer
DIRECTOR = "pgu-director:0.0"
ERROR = "Selected model is at capacity. Please try a different model."
#: The board cases run in real time, every timing a thirtieth of production's: the stale-Codex
#: recovery's 120 s, the idle grace's 45 s, the unresolved-turn grace's 600 s, and the retries' 60, 120, 300 s.
STALE, GRACE, UNRESOLVED = 4.0, 1.5, 20
DELAYS = (2.0, 4.0, 10.0)
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def screens() -> dict:
    """The captured Codex screens, a Claude Director at its prompt, and near misses made from the capacity screen."""
    captures = json.loads((ROOT / "tests/fixtures/syrd594_codex_capacity_screens.json").read_text())["captures"]
    out = {name: {"rows": c["rows"], "joined": c["joined"], "cursor": c["cursor"], "hook": c["hook"]}
           for name, c in captures.items()}
    idle = json.loads((ROOT / "tests/fixtures/syrd550_claude_captures.json").read_text())["captures"]["idle"]
    out["director"] = {"rows": idle["lines"], "joined": idle["lines"], "cursor": [idle["cursor_x"], idle["cursor_y"]]}
    base, y = out["capacity"], out["capacity"]["cursor"][1]
    error_row = next(i for i, row in enumerate(base["rows"]) if row.startswith("■"))

    def variant(edit) -> dict:
        rows = list(base["rows"])
        edit(rows)
        return {"rows": rows, "joined": rows, "cursor": base["cursor"], "hook": base["hook"]}

    out["output after the error"] = variant(lambda rows: rows.__setitem__(y - 2, "• Ran the suite again"))
    out["error indented"] = variant(lambda rows: rows.__setitem__(error_row, "  " + rows[error_row]))
    out["error with more text"] = variant(lambda rows: rows.__setitem__(error_row, rows[error_row] + " (retrying)"))
    out["quoted reply, turn still running"] = dict(out["quoted"], hook=base["hook"])

    def question(rows: list[str]) -> None:  # a Codex menu opened under the error, its cursor on the first option
        rows[y:] = ["› 1. Use existing model", "  2. Try new model", "", "  Press enter to continue"] + [""] * (len(rows) - y - 4)

    out["a question under the error"] = variant(question)
    out["cursor unreadable on the second look"] = dict(base, cursor_reads=1)
    out["cursor off the prompt"] = dict(base, cursor=[0, error_row + 2])
    return out


class Pane:
    def __init__(self, screen: dict, pid: int, started: float) -> None:
        self.screen, self.pid, self.started, self.moving = screen, pid, started, False
        self.captures = 0
        self.cursor_reads = screen.get("cursor_reads")  # how many cursor reads succeed; None: all

    def rows(self, joined: bool) -> list[str]:
        self.captures += 1
        rows = list(self.screen["joined" if joined else "rows"])
        if self.moving:  # a footer that changes between captures: the pane is drawing
            rows[-1] = rows[-1] + " " * (self.captures % 3)
        return rows

    def table(self) -> list[tuple]:
        # The pane's Codex and the app-server daemon it starts in a session of its own (measured, SYRD-589).
        return [(self.pid, 1, 50, self.pid, self.started), (self.pid + 89, self.pid, 7, self.pid + 89, self.started + 0.4)]


def fake_tmux(panes: dict[str, Pane]):
    def tmux(argv, **_kwargs):
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        pane = panes.get(target)
        if pane is None:
            raise subprocess.CalledProcessError(1, argv, "", f"can't find pane: {target}")
        if argv[:2] == ["tmux", "capture-pane"]:
            return subprocess.CompletedProcess(argv, 0, "".join(row + "\n" for row in pane.rows("-J" in argv)), "")
        if argv[:2] == ["tmux", "display-message"]:
            fmt = argv[-1]
            if "cursor_x" in fmt:
                if pane.cursor_reads is not None:
                    if pane.cursor_reads <= 0:
                        raise subprocess.CalledProcessError(1, argv, "", "no cursor")
                    pane.cursor_reads -= 1
                x, y = pane.screen["cursor"]
                return subprocess.CompletedProcess(argv, 0, f"{x} {y} {len(pane.screen['rows'])}\n", "")
            if fmt == "#{pane_pid}":
                return subprocess.CompletedProcess(argv, 0, f"{pane.pid}\n", "")
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        raise AssertionError(f"unexpected tmux call {argv}")

    def table():
        return [row for pane in panes.values() for row in pane.table()]

    return tmux, table


def gate_cases(nl) -> dict:
    """The real gate on each screen, its hook state as Codex left it, 10 s and 130 s after the turn started."""
    seen = {}
    shown = screens()
    for name, screen in shown.items():
        if name == "director":
            continue
        for age, moving in ((10, False), (130, False), (130, True)):
            if moving and name != "capacity":
                continue
            pane = Pane(screen, 700, time.time() - 3600)
            pane.moving = moving
            tmux, table = fake_tmux({WORKER: pane})
            with tempfile.TemporaryDirectory(prefix="syrd594-gate.") as state_dir:
                store = nl.PaneHookStateStore(Path(state_dir))
                store.write(WORKER, screen["hook"]["state"], source=screen["hook"]["source"], now=time.time() - age)
                gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                           pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                           role_runtimes={"main": "codex"})
                gate.role_targets = {"main": WORKER}
                first = [gate.is_busy(WORKER), gate.last_trace(WORKER).reason]
                recheck = [gate.pre_send_busy(WORKER), gate.last_trace(WORKER).reason]
                after = store.read(WORKER)
                seen[f"{name} @{age}s{' moving' if moving else ''}"] = {
                    "first": first, "recheck": recheck, "hook_after": [after.state, after.source]}
    seen["report"] = report_cases(nl, shown)
    return seen


def report_cases(nl, shown: dict) -> dict | None:
    """What a pass reports of the pane to the board, for each hook state over the screens that matter."""
    try:
        from scripts.ticket_board import capacity_retry
    except ImportError:
        return None  # BEFORE has none
    cases = {
        "the failed turn": ("capacity", "busy", "codex.UserPromptSubmit"),
        "the error on screen, the turn ended": ("capacity", "idle", "codex.Stop"),
        "the error on screen, a new session": ("capacity", "idle", "codex.SessionStart"),
        "working": ("quoted", "busy", "codex.UserPromptSubmit"),
        "a draft under the error": ("draft-after-capacity", "busy", "codex.UserPromptSubmit"),
        "not a Codex hook": ("capacity", "busy", "claude.UserPromptSubmit"),
    }
    seen = {}
    for name, (screen, state, source) in cases.items():
        pane = Pane(shown[screen], 700, time.time() - 3600)
        tmux, table = fake_tmux({WORKER: pane})
        with tempfile.TemporaryDirectory(prefix="syrd594-report.") as state_dir:
            store = nl.PaneHookStateStore(Path(state_dir))
            store.write(WORKER, state, source=source, now=1_792_000_000.0)
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       role_runtimes={"main": "codex"})
            gate.role_targets = {"main": WORKER, "director": "pgu-director:0.0"}
            seen[name] = capacity_retry.report(gate).get("main")
    return seen


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow

    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    seen: dict = {"gate": gate_cases(nl)}
    shown = screens()
    with temporary_cluster(prefix="s594", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd594-state.") as state_dir:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        store = nl.PaneHookStateStore(Path(state_dir))
        worker = Pane(shown["recovered"], 600, time.time() - 3600)
        director = Pane(shown["director"], 900, time.time() - 3600)
        store.write(WORKER, "idle", source="codex.Stop", now=time.time() - 600)  # its previous turn, long over
        store.write(DIRECTOR, "idle", source="claude.Stop", now=time.time() - 600)
        tmux, table = fake_tmux({WORKER: worker, DIRECTOR: director})
        sends: list[tuple[str, str, str, float]] = []
        provider = {"mode": "full"}  # what the model provider does with the worker's next turn
        phase = {"name": ""}
        worker.screen = shown["recovered"]

        def directorctl(target: str, message: str) -> dict:
            """Typed and submitted: the pane's provider takes it and runs a turn."""
            sends.append((phase["name"], target, message, time.monotonic()))
            if target == DIRECTOR:
                store.write(target, "busy", source="claude.UserPromptSubmit")
                store.write(target, "idle", source="claude.Stop")
                return {}
            store.write(target, "busy", source="codex.UserPromptSubmit")
            if provider["mode"] == "full":  # still at capacity: the turn ends on the error, no Stop hook
                worker.screen = shown["capacity-twice"]
            elif provider["mode"] == "answers":  # the provider answers: the turn ends, and so does Codex's Stop
                worker.screen = shown["recovered"]
                store.write(target, "idle", source="codex.Stop")
            elif provider["mode"] == "quotes":  # an ordinary reply that quotes the error
                worker.screen = shown["quoted"]
                store.write(target, "idle", source="codex.Stop")
            elif provider["mode"] == "quota":
                worker.screen = shown["quota"]
            return {}

        def new_gate():
            return nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=STALE,
                                       role_runtimes={"main": "codex", "app": "claude", "ops": "claude",
                                                      "director": "claude"})

        process = {"gate": new_gate(), "restarts": 0}

        def restart() -> None:
            """The listener process starts again: nothing it held in memory survives."""
            process["gate"] = new_gate()
            process["restarts"] += 1

        def real_pass() -> int:
            """The listener's loop, once: stops after its first delivery, or after a pass that delivers nothing."""
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=process["gate"].is_working,
                target_exists=lambda target: target in (WORKER, DIRECTOR), poll_seconds=0,
                submission_confirm_seconds=0.4, submission_poll_seconds=0.05, idle_stall_grace_seconds=GRACE)
            listener.unresolved_turn_grace_seconds = UNRESOLVED
            listener.delivery_submitter = lambda _target: None
            return listener.listen_once(max_notifications=listener.delivered_count + 1)

        def run_for(seconds: float, name: str, restart_every: float = 0.0) -> None:
            """The listener running for a while, a pass every fifth of a second."""
            phase["name"] = name
            end = last_restart = time.monotonic()
            end += seconds
            while time.monotonic() < end:
                if restart_every and time.monotonic() - last_restart >= restart_every:
                    restart()
                    last_restart = time.monotonic()
                real_pass()
                time.sleep(0.2)

        def route(ticket: str, state: str, role: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": state, "assignee": role},
                              caller="director")

        def finish(ticket: str) -> None:
            """The ticket is done, and its pane put back as it began: idle after a turn that ended."""
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")
            run_for(0.6, "done")
            worker.screen = shown["recovered"]
            store.write(WORKER, "idle", source="codex.Stop", now=time.time() - 600)

        def start(ticket: str, title: str, mode: str) -> float:
            provider["mode"] = mode
            fixture.seed_postgres_ticket(admin, ticket, title=title, state="analysis", assignee="director", commit_exempt=True)
            route(ticket, "in_progress", "main")
            begun = time.monotonic()
            run_for(1.0, "notice")  # the notice goes, and its turn ends as the provider decides
            return begun

        def told(ticket: str, target: str = WORKER) -> list[list]:
            """What the pane was sent about the ticket: the phase, the message, seconds after the ticket's first notice."""
            first = next((at for _n, to, message, at in sends if to == WORKER and ticket in message), 0.0)
            return [[name, message, round(at - first, 1)] for name, to, message, at in sends
                    if to == target and ticket in message]

        def episodes(ticket: str) -> list | None:
            """Each episode of the ticket, None on a board without them: failure and retry times, as the board recorded them."""
            if sql("SELECT to_regclass('ticket_board.provider_capacity_episodes') IS NULL;") == "t":
                return None
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_object("
                "'failures', (SELECT json_agg(extract(epoch FROM (f->>'observed_at')::timestamptz)) FROM jsonb_array_elements(failures) f), "
                "'retries', (SELECT json_agg(json_build_array((r->>'retry')::int, (SELECT extract(epoch FROM min(t.ts)) FROM "
                "ticket_board.notification_trace t WHERE t.notification_id = (r->>'notification_id')::bigint AND t.event = 'send'))) "
                "FROM jsonb_array_elements(retries) r), "
                "'escalated', escalated_at IS NOT NULL, 'closed', close_reason) ORDER BY id), '[]')::text "
                f"FROM ticket_board.provider_capacity_episodes WHERE ticket_id = '{ticket}';"))

        def escalation(ticket: str):
            return json.loads(sql(
                "SELECT coalesce((SELECT detail->'payload' FROM ticket_board.notification_trace WHERE "
                f"ticket_id = '{ticket}' AND event = 'enqueue' AND target_role = 'director' AND "
                "detail->'payload'->>'kind' = 'provider_capacity' ORDER BY id LIMIT 1), 'null')::text;"))

        # 1. Transient: the notice's turn ends on the error; the first retry finds the provider answering.
        start("PGU-901", "Work that meets a passing capacity error", "full")
        provider["mode"] = "answers"
        run_for(DELAYS[0] + 1.5, "retry window")
        run_for(2.0, "after")
        seen["transient"] = {"told": told("PGU-901"), "episodes": episodes("PGU-901"), "director": told("PGU-901", DIRECTOR)}
        finish("PGU-901")

        # 2. Exhausted: the provider stays at capacity through every retry; the Director is told once. The
        #    listener restarts every few seconds throughout.
        start("PGU-902", "Work that meets a lasting capacity error", "full")
        run_for(sum(DELAYS) + 10.0, "retrying", restart_every=3.0)
        seen["exhausted"] = {"told": told("PGU-902"), "director": told("PGU-902", DIRECTOR),
                             "episodes": episodes("PGU-902"), "restarts": process["restarts"],
                             "escalation": escalation("PGU-902")}
        finish("PGU-902")
        seen["exhausted"]["episodes_after_done"] = episodes("PGU-902")

        # 3. Changes hands before its retry is due: nothing is retried to the old owner.
        start("PGU-903", "Work handed to another role", "full")
        fixture.post_json(base, "/api/tickets/PGU-903/actions/force_move", {"state": "in_progress", "assignee": "app"},
                          caller="director")
        run_for(DELAYS[0] + 2.0, "moved")
        seen["moved"] = {"told": told("PGU-903"), "episodes": episodes("PGU-903")}
        finish("PGU-903")

        # 4. Superseded: when the retry falls due a Director comment is on its way; it goes, and is the next
        #    turn. Then a person types under the error: the retry waits, and goes once the line is empty.
        begun = start("PGU-904", "Work whose retry is overtaken", "full")
        time.sleep(max(0.0, begun + DELAYS[0] + 0.5 - time.monotonic()))
        fixture.post_json(base, "/api/tickets/PGU-904/actions/add_comment", {"text": "Director note on PGU-904."},
                          caller="director")
        run_for(1.0, "comment")
        worker.screen = shown["draft-after-capacity"]
        time.sleep(DELAYS[0] + 0.5)
        run_for(1.5, "draft")
        draft = told("PGU-904")
        worker.screen = shown["capacity-twice"]
        run_for(1.0, "draft gone")
        seen["superseded"] = {"until_draft_gone": draft, "told": told("PGU-904"), "episodes": episodes("PGU-904")}
        finish("PGU-904")

        # 5. Not this error: an ordinary reply that quotes it, and Codex's quota error.
        for ticket, mode in (("PGU-905", "quotes"), ("PGU-906", "quota")):
            start(ticket, f"Work whose turn {mode}", mode)
            run_for(STALE + 3.0, "later")
            seen[mode] = {"told": told(ticket), "episodes": episodes(ticket)}
            finish(ticket)

        import psycopg

        if sql("SELECT to_regprocedure('ticket_board.note_provider_capacity(jsonb, double precision[], timestamptz)') IS NOT NULL;") == "t":
            seen["functions"] = function_cases(fixture, admin, base, sql, nl, psycopg,
                                               fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener"))

        # Who may run it, and a board built from schema.sql alone installs the same functions.

        functions = ("note_provider_capacity(jsonb, double precision[], timestamp with time zone)",
                     "provider_capacity_retry_is_current(jsonb)", "close_provider_capacity(bigint, text)",
                     "withdraw_provider_capacity_notice(bigint, bigint, text)")
        try:
            seen["grantees"] = {function.split("(")[0]: sql(
                "SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, '+'), '') "
                f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{function}'::regprocedure "
                "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;") for function in functions}
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            definitions = " || ".join(f"md5(pg_get_functiondef('ticket_board.{function}'::regprocedure))" for function in functions)
            seen["fresh_equals_migrated"] = fixture.psql(fresh, f"SELECT {definitions};").strip() == sql(f"SELECT {definitions};")
            try:
                with psycopg.connect(fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE),
                                     autocommit=True) as conn:
                    conn.execute("SELECT ticket_board.note_provider_capacity('{}'::jsonb, ARRAY[1.0]::double precision[])")
                seen["service_refused"] = False
            except psycopg.Error as exc:
                seen["service_refused"] = "permission denied" in str(exc)
        except AssertionError as exc:  # BEFORE has none
            seen["grantees"] = seen["fresh_equals_migrated"] = seen["service_refused"] = f"unavailable: {str(exc)[-120:]}"
        server.shutdown()
        server.server_close()
    return seen


def function_cases(fixture, admin: str, base: str, sql, nl, psycopg, listener_url: str) -> dict:
    """The board function itself, called as the listener calls it, at the moments given: production's delays."""
    seen: dict = {}
    delays = "ARRAY[60, 120, 300]::double precision[]"
    t0 = float(sql("SELECT extract(epoch FROM clock_timestamp());"))

    def at(offset: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 + offset)) + f".{int((t0 + offset) % 1 * 1e6):06d}+00:00"

    def note(report: dict, offset: float) -> int:
        with psycopg.connect(listener_url, autocommit=True) as conn:
            return conn.execute(f"SELECT ticket_board.note_provider_capacity(%s::jsonb, {delays}, %s::timestamptz)",
                                (json.dumps(report), at(offset))).fetchone()[0]

    def capacity(turn: float) -> dict:
        return {"main": {"capacity_turn": at(turn), "ended_turn": None, "working": False}}

    def queue() -> list:
        return json.loads(sql("SELECT coalesce(json_agg(json_build_array(id, target_role, payload->>'kind', payload->'retry') "
                              "ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                              "WHERE payload->>'kind' IN ('capacity_retry', 'provider_capacity', 'other');"))

    def deliver(notification_id: int, offset: float) -> None:
        """What the listener does with a notice it typed, at that moment: trace the send, take the row off the queue."""
        sql(f"SELECT ticket_board.record_notification_trace(q.ticket_id, q.id, q.target_role, q.kind, 'send') "
            f"FROM ticket_board.ticket_notification_queue q WHERE q.id = {notification_id}; "
            f"UPDATE ticket_board.notification_trace SET ts = '{at(offset)}' WHERE notification_id = {notification_id} "
            f"AND event = 'send'; DELETE FROM ticket_board.ticket_notification_queue WHERE id = {notification_id};")

    def is_current(payload: dict) -> bool:
        with psycopg.connect(listener_url, autocommit=True) as conn:
            return conn.execute("SELECT ticket_board.provider_capacity_retry_is_current(%s::jsonb)",
                                (json.dumps(payload),)).fetchone()[0]

    def queued_row(notification_id: int) -> list:
        return json.loads(sql(f"SELECT json_build_array(ticket_id, target_role, payload::text)::text "
                              f"FROM ticket_board.ticket_notification_queue WHERE id = {notification_id};"))

    def deliverable(notification_id: int | list) -> bool:
        """The listener's own currentness check, as delivery asks it, of a queued notice or one as it was queued."""
        row = queued_row(notification_id) if isinstance(notification_id, int) else notification_id
        listener = nl.TicketBoardNotifyListener(conninfo=listener_url, project="pgu", sender=lambda *_a: None,
                                                activity_gate=lambda _t: True, target_exists=lambda _t: True, poll_seconds=0)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            return listener.eligibility._notification_is_current(conn, row[0], row[1], row[2])

    def payload(notification_id: int) -> dict:
        return json.loads(sql(f"SELECT payload::text FROM ticket_board.ticket_notification_queue WHERE id = {notification_id};"))

    def act(ticket: str, action: str, body: dict) -> None:
        fixture.post_json(base, f"/api/tickets/{ticket}/actions/{action}", body, caller="director")

    def start(ticket: str) -> None:
        fixture.seed_postgres_ticket(admin, ticket, title=f"Board case {ticket}", state="analysis", assignee="director",
                                     commit_exempt=True)
        act(ticket, "route", {"state": "in_progress", "assignee": "main"})
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE target_role = 'main';")  # its notice went

    def finish(ticket: str) -> None:
        act(ticket, "force_move", {"state": "done", "assignee": "unassigned"})
        sql("DELETE FROM ticket_board.ticket_notification_queue;")

    # The schedule: 60, 120 and 300 s, each from the failure it answers; a retry withdrawn unsent does not count.
    start("PGU-911")
    steps = [note(capacity(1), 2), note(capacity(1), 61), note(capacity(1), 62)]
    first = queue()
    steps.append(note(capacity(1), 62))  # again at the same moment, as a restarted listener would
    seen["retry_1"] = {"steps": steps, "queue": first, "again": queue(),
                       "current": [is_current(payload(first[0][0])), deliverable(first[0][0])] if first else None}
    steps = [note({"main": {"capacity_turn": None, "ended_turn": None, "working": True}}, 63)]
    seen["withdrawn"] = {"steps": steps, "queue": queue(), "discards": json.loads(sql(
        "SELECT coalesce(json_agg(busy_reason ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
        "WHERE ticket_id = 'PGU-911' AND event = 'discard';"))}
    steps = [note(capacity(1), 64)]
    retry_1 = next(iter(queue()), [0, "", "", None])
    retry_1_payload = payload(retry_1[0]) if retry_1[0] else {}
    deliver(retry_1[0], 65)
    steps += [note(capacity(70), 71), note(capacity(70), 190), note(capacity(70), 191)]
    retry_2 = next(iter(queue()), [0, "", "", None])
    seen["retry_2"] = {"steps": steps, "queue": [retry_2], "current": [is_current(payload(retry_2[0])) if retry_2[0] else None, is_current(retry_1_payload)]}
    deliver(retry_2[0], 192)
    steps = [note(capacity(200), 201), note(capacity(200), 500), note(capacity(200), 501)]
    retry_3 = next(iter(queue()), [0, "", "", None])
    deliver(retry_3[0], 502)
    steps += [note(capacity(200), 505), note(capacity(510), 511)]  # its own turn has not failed yet; then it has
    escalation = queue()
    steps += [note(capacity(510), 2000), note(capacity(3000), 3001), note(capacity(3000), 9000)]
    seen["exhausted"] = {"steps": steps, "retry_3": retry_3, "escalation": escalation, "after": queue(),
                         "episode": json.loads(sql("SELECT json_build_array(jsonb_array_length(failures), "
                                                   "jsonb_array_length(retries), escalated_at IS NOT NULL)::text FROM "
                                                   "ticket_board.provider_capacity_episodes WHERE ticket_id = 'PGU-911';"))}
    # A normal turn end after the last failure closes it, and the escalation not yet sent is withdrawn.
    def closed() -> str:
        return sql("SELECT coalesce(close_reason, 'open') FROM ticket_board.provider_capacity_episodes WHERE ticket_id = 'PGU-911';")

    steps = [note({"main": {"capacity_turn": None, "ended_turn": at(3000), "working": False}}, 9001)]
    after_own_end = closed()  # the failed turn itself is no later turn
    steps.append(note({"main": {"capacity_turn": None, "ended_turn": at(9500), "working": False}}, 9501))
    seen["recovered"] = {"steps": steps, "queue": queue(), "closed": [after_own_end, closed()]}
    finish("PGU-911")

    # Superseded: anything else queued for the role goes first; once it has gone, the retry is due.
    start("PGU-912")
    steps = [note(capacity(10000), 10001)]
    sql("SELECT ticket_board.enqueue_notification('PGU-912', 'ticket_update', 'main', 'Something else for main.', "
        "'{\"kind\": \"other\", \"id\": \"PGU-912\"}'::jsonb, 'other:PGU-912');")
    steps.append(note(capacity(10000), 10061))
    held = queue()
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE dedupe_key = 'other:PGU-912';")
    steps.append(note(capacity(10000), 10062))
    retry = queue()
    # Delivery asks too: under manual control, or once its episode has closed, the retry is not typed.
    retry_row = queued_row(retry[0][0]) if retry else None
    act("PGU-912", "set_manually_controlled", {"manually_controlled": True})
    held_by_hand = deliverable(retry[0][0]) if retry else None
    act("PGU-912", "set_manually_controlled", {"manually_controlled": False})
    released = deliverable(retry[0][0]) if retry else None
    sql(f"SELECT ticket_board.close_provider_capacity(id, 'recovered') FROM ticket_board.provider_capacity_episodes "
        f"WHERE ticket_id = 'PGU-912' AND closed_at IS NULL;")
    seen["superseded"] = {"steps": steps, "held": held, "retry": retry, "manual": held_by_hand, "released": released,
                          "deliverable_after_close": deliverable(retry_row) if retry_row else None,
                          "after_close": [is_current(json.loads(sql(
                              "SELECT coalesce((SELECT detail->>'payload' FROM ticket_board.notification_trace WHERE "
                              "event = 'enqueue' AND detail->'payload'->>'kind' = 'capacity_retry' AND ticket_id = 'PGU-912' "
                              "ORDER BY id DESC LIMIT 1), '{}');"))), queue()]}
    finish("PGU-912")

    # Ignored: the Director's pane, a role with no current work, a turn from before the ticket was the role's,
    # a turn not started yet, and values that are not times.
    start("PGU-913")
    fixture.seed_postgres_ticket(admin, "PGU-914", title="Work the Director owns", state="analysis",
                                 assignee="director", commit_exempt=True)
    entered = float(sql("SELECT extract(epoch FROM entered_current_state_at) FROM ticket_board.ticket_notification_state "
                        "WHERE ticket_id = 'PGU-913';"))
    steps = [note({"director": {"capacity_turn": at(20000), "ended_turn": None, "working": False}}, 20001),
             note({"ops": {"capacity_turn": at(20000), "ended_turn": None, "working": False}}, 20001),
             note({"main": {"capacity_turn": at(entered - t0 - 30), "ended_turn": None, "working": False}}, 20001),
             note({"main": {"capacity_turn": at(30000), "ended_turn": None, "working": False}}, 20001),
             note({"main": {"capacity_turn": "garbage", "ended_turn": None, "working": False}}, 20001),
             note({"main": "garbage"}, 20001), note({}, 20001)]
    seen["ignored"] = {"steps": steps, "episodes": json.loads(sql(
        "SELECT coalesce(json_agg(target_role), '[]')::text FROM ticket_board.provider_capacity_episodes "
        "WHERE ticket_id = 'PGU-913' OR target_role IN ('director', 'ops');"))}
    finish("PGU-913")
    act("PGU-914", "force_move", {"state": "done", "assignee": "unassigned"})
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    os.environ["TICKET_BOARD_CAPACITY_RETRY_DELAYS"] = ",".join(f"{delay:g}" for delay in DELAYS)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    # Mutation runs may keep the before-run's observations in a file: they come from BEFORE's archive and
    # this file's scenario, which no mutant of the tree touches.
    cached = Path(os.environ["SYRD594_BEFORE_JSON"]) if os.environ.get("SYRD594_BEFORE_JSON") else None
    if cached is not None and cached.is_file():
        before = json.loads(cached.read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="syrd594.") as tmp:
            before_root = tree_at(BEFORE, Path(tmp) / "before")
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                                   capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                   timeout=900)
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        if cached is not None:
            cached.write_text(json.dumps(before))
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    print("OBSERVED " + json.dumps({"before": before, "after": after}, ensure_ascii=False), file=sys.stderr)
    from scripts.ticket_board import capacity_retry

    def retries(told: list) -> list[str]:
        return [message for _phase, message, _at in told if ERROR in message]

    # The defaults are the ticket's 1, 2 and 5 minutes; a bad override falls back to them.
    check(capacity_retry.delays_seconds({}) == (60.0, 120.0, 300.0)
          and capacity_retry.delays_seconds({capacity_retry.DELAYS_ENV: "1,x"}) == (60.0, 120.0, 300.0)
          and capacity_retry.delays_seconds({capacity_retry.DELAYS_ENV: "2,4,10"}) == DELAYS,
          "three retries, after 60, 120 and 300 seconds, unless the listener is told otherwise")

    # The gate, BEFORE: the capacity pane reads as working for 120 s, then is rewritten as idle like any
    # Codex error -- the quota error exactly the same.
    gb, ga = before["gate"], after["gate"]
    check(gb["capacity @10s"]["first"] == [True, "hook_busy"]
          and gb["capacity @130s"]["first"] == [False, "stale_codex_busy_recovered"]
          and gb["capacity @130s"]["hook_after"] == ["idle", "listener.stale_codex_busy_recovery"]
          and gb["quota @130s"] == gb["capacity @130s"],
          f"before: held as working, then recovered as any stale Codex turn is: {gb['capacity @10s']}, {gb['capacity @130s']}")
    # The gate now: a turn that ended on the capacity error is over at any age, and its hook is left alone.
    for name in ("capacity", "capacity-twice", "draft-cleared", "503"):
        for age in (10, 130):
            case = ga[f"{name} @{age}s"]
            check(case["first"] == [False, "codex_capacity_turn_end"] and case["recheck"] == [False, "codex_capacity_turn_end"]
                  and case["hook_after"] == ["busy", "codex.UserPromptSubmit"],
                  f"{name} @{age}s: the turn is over, deliverable at both gates, the hook unchanged: {case}")
    # Still held: a person typing under the error, and a pane that is drawing.
    check(all(ga[f"draft-after-capacity @{age}s"]["first"] == [True, "human_composing"]
              and ga[f"draft-after-capacity @{age}s"]["recheck"] == [True, "human_composing"] for age in (10, 130)),
          f"a draft typed under the error holds the pane: {ga['draft-after-capacity @10s']}")
    check(ga["capacity @130s moving"]["first"] == [True, "pane_content_changed"],
          f"a pane still drawing is not at rest: {ga['capacity @130s moving']}")
    check(ga["a question under the error @10s"]["first"][0] is True
          and ga["a question under the error @10s"]["first"][1].startswith("provider_prompt:"),
          f"a provider question is answered by Enter, so it holds: {ga['a question under the error @10s']}")
    check(ga["cursor unreadable on the second look @10s"]["first"] == [True, "cursor_state_unavailable"],
          f"a cursor that cannot be read holds: {ga['cursor unreadable on the second look @10s']}")
    # Everything else reads exactly as before: the quota error, a reply that quotes the error, the error
    # followed by more output, indented, or with more text on its line, and a turn that ended normally.
    for name in ("quota", "quoted", "recovered", "output after the error", "error indented", "error with more text",
                 "quoted reply, turn still running", "cursor off the prompt"):
        for age in (10, 130):
            check(ga[f"{name} @{age}s"] == gb[f"{name} @{age}s"],
                  f"{name} @{age}s reads as before: {gb[f'{name} @{age}s']} -> {ga[f'{name} @{age}s']}")

    # BEFORE on the board: nothing ever names the cause. A passing error is answered only by chance; a lasting
    # one gets the generic "still yours" and "waiting in your queue" prompts, and the Director a generic "stuck".
    for name in ("transient", "exhausted", "moved", "superseded"):
        check(not retries(before[name]["told"]) and not any(ERROR in m for _p, m, _a in before[name].get("director", [])),
              f"before: {name}: no message names the provider's error: {before[name]['told']}")
    generic = [message for _phase, message, _at in before["exhausted"]["told"][1:]]
    check(len(generic) >= 2 and all("still yours" in m or "waiting in your" in m for m in generic)
          and before["exhausted"]["told"][1][2] >= STALE,
          f"before: a lasting error: the generic prompts, the first after the stale-Codex recovery: {before['exhausted']['told']}")

    # 1. Transient: one retry, at its delay, and the provider's answer closes the episode.
    transient = after["transient"]
    episode = transient["episodes"][0] if transient["episodes"] else {}
    check(len(retries(transient["told"])) == 1 and "automatic retry 1 of 3" in retries(transient["told"])[0]
          and retries(transient["told"])[0].startswith("PGU-901 -- Work that meets a passing capacity error is active again in "),
          f"a passing error: retried once, saying why: {transient['told']}")
    check(len(transient["episodes"]) == 1 and episode["closed"] == "recovered" and not episode["escalated"]
          and len(episode["failures"]) == 1 and len(episode["retries"]) == 1
          and DELAYS[0] <= episode["retries"][0][1] - episode["failures"][0] < DELAYS[0] + 2.5,
          f"... {DELAYS[0]} s after the failure, and the episode closed as recovered: {transient['episodes']}")
    check(not any(ERROR in m for _p, m, _a in transient["director"]), f"the Director is not told about it: {transient['director']}")

    # 2. Exhausted: three retries, each its delay after the failure it answers; then the Director once.
    exhausted = after["exhausted"]
    episode = exhausted["episodes"][0]
    sent = retries(exhausted["told"])
    check([f"automatic retry {n} of 3" in message for n, message in enumerate(sent, 1)] == [True, True, True],
          f"a lasting error: exactly three retries: {sent}")
    check(len(episode["failures"]) == 4 and [retry for retry, _at in episode["retries"]] == [1, 2, 3]
          and all(delay <= at - failure < delay + 2.5
                  for delay, (_r, at), failure in zip(DELAYS, episode["retries"], episode["failures"])),
          f"each retry waits its delay after the failure it answers: {episode}")
    alerts = [message for _p, message, _a in exhausted["director"] if ERROR in message]
    check(len(alerts) == 1 and alerts[0].startswith("PRIORITY PGU-902 -- Work that meets a lasting capacity error: main's Codex pane")
          and "failed 4 times" in alerts[0] and "3 automatic retries" in alerts[0]
          and "switchyard attach pgu main" in alerts[0] and len(exhausted["director"]) == 1,
          f"then one escalation to the Director, and nothing else to them: {exhausted['director']}")
    payload = exhausted["escalation"]
    check(payload["role"] == "main" and payload["error"] == ERROR and payload["condition"] == "codex_capacity"
          and len(payload["failures"]) == 4 and len(payload["retries"]) == 3,
          f"... carrying the role, the ticket, the condition and the retry history: {payload}")
    check(exhausted["restarts"] >= 5 and episode["escalated"] and exhausted["episodes_after_done"][0]["closed"] == "ticket_moved",
          f"through {exhausted['restarts']} listener restarts nothing was reset or repeated; done closes it")

    # 3. Changes hands: nothing retried to the old owner.
    moved = after["moved"]
    check(not retries(moved["told"]) and moved["episodes"][0]["closed"] == "ticket_moved" and not moved["episodes"][0]["retries"],
          f"a ticket handed on is not retried: {moved}")

    # 4. Superseded, then a draft: the comment went first and was the next turn; the draft held the retry.
    superseded = after["superseded"]
    episode = superseded["episodes"][0]
    check(not retries(superseded["until_draft_gone"]) and "new comment" in superseded["until_draft_gone"][-1][1],
          f"the Director's comment goes instead of the retry, and the draft holds it: {superseded['until_draft_gone']}")
    check(len(retries(superseded["told"])) == 1 and len(episode["retries"]) == 1
          and episode["retries"][0][1] - episode["failures"][1] >= DELAYS[0],
          f"the draft gone, retry 1 goes, timed from the comment's own failed turn: {superseded}")

    # 5. Not this error: no episode, nothing more said than before.
    for mode in ("quotes", "quota"):
        check(after[mode]["episodes"] == [] and not retries(after[mode]["told"])
              and [m for _p, m, _a in after[mode]["told"]] == [m for _p, m, _a in before[mode]["told"]],
              f"{mode}: no capacity episode, and the pane told exactly what it was before: {after[mode]}")

    # What a pass reports: the failed turn only while the hook says it is running and the screen ends on the
    # error; a turn the runtime ended; working otherwise; nothing for a pane that is not Codex's.
    r, turn = ga["report"], "2026-10-14T17:46:40+00:00"
    check(r == {"the failed turn": {"capacity_turn": turn, "ended_turn": None, "working": False},
                "the error on screen, the turn ended": {"capacity_turn": None, "ended_turn": turn, "working": False},
                "the error on screen, a new session": {"capacity_turn": None, "ended_turn": None, "working": False},
                "working": {"capacity_turn": None, "ended_turn": None, "working": True},
                "a draft under the error": {"capacity_turn": None, "ended_turn": None, "working": True},
                "not a Codex hook": None},
          f"each pane's report: {r}")

    # The board function at production's delays, the clock given: 60, 120, 300 s, each from the failure it
    # answers; once each; a retry withdrawn unsent does not count; then the Director once, and nothing more.
    f = after["functions"]
    check(f["retry_1"]["steps"] == [0, 0, 1, 0] and f["retry_1"]["again"] == f["retry_1"]["queue"]
          and f["retry_1"]["current"] == [True, True],
          f"retry 1 at 60 s after the failure was first seen, not at 59; asked again, nothing more: {f['retry_1']}")
    check(f["withdrawn"] == {"steps": [0], "queue": [], "discards": ["provider_capacity_pane_busy"]},
          f"the pane working again withdraws the queued retry: {f['withdrawn']}")
    check(f["retry_2"]["steps"] == [1, 0, 0, 1] and f["retry_2"]["queue"][0][3] == 2 and f["retry_2"]["current"] == [True, False],
          f"the withdrawn retry is queued again; once sent, the next failure's retry 2 waits 120 s: {f['retry_2']}")
    check(f["exhausted"]["steps"] == [0, 0, 1, 0, 1, 0, 0, 0] and f["exhausted"]["retry_3"][3] == 3
          and [row[1:3] for row in f["exhausted"]["escalation"]] == [["director", "provider_capacity"]]
          and f["exhausted"]["after"] == f["exhausted"]["escalation"] and f["exhausted"]["episode"][2] is True,
          f"retry 3 at 300 s; its failure tells the Director once; later failures add nothing: {f['exhausted']}")
    check(f["recovered"] == {"steps": [0, 0], "queue": [], "closed": ["open", "recovered"]},
          f"a normal turn end after the last failure closes it and withdraws the unsent escalation: {f['recovered']}")
    check(f["superseded"]["steps"] == [0, 0, 1] and [r[2] for r in f["superseded"]["held"]] == ["other"]
          and f["superseded"]["retry"][0][2] == "capacity_retry" and f["superseded"]["manual"] is False
          and f["superseded"]["released"] is True and f["superseded"]["after_close"] == [False, []]
          and f["superseded"]["deliverable_after_close"] is False,
          f"another notice for the role goes first; delivery refuses a held ticket's retry and a closed episode's: "
          f"{f['superseded']}")
    check(f["ignored"] == {"steps": [0] * 7, "episodes": []},
          f"the Director's pane, a role without work, an older or future turn, and garbage are ignored: {f['ignored']}")

    check(after["grantees"] == {"note_provider_capacity": "ticket_board_listener",
                                "provider_capacity_retry_is_current": "ticket_board_listener",
                                "close_provider_capacity": "", "withdraw_provider_capacity_notice": ""}
          and after["service_refused"] is True and after["fresh_equals_migrated"] is True,
          f"only the listener may report or ask; schema.sql installs the same: {after['grantees']}, "
          f"{after['service_refused']}, {after['fresh_equals_migrated']}")
    print(f"codex_capacity_retry_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
