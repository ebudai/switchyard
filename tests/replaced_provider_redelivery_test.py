#!/usr/bin/env python3
"""SYRD-589: a restored worker gets its current ticket, once, and a provider at its startup prompt is deliverable.

MEFP restored its worker panes on 2026-10-13. Six workers had a current ticket
whose notice had been sent on 2026-10-10, so the board read each as delivered,
while every pane showed a fresh Codex 0.162.0 at "Ask Codex to do anything".
Routing one ticket out and back made an ordinary notice, and it was requeued as
"pane busy" against that visibly idle pane.

Two causes, both measured:

* Codex 0.162.0 starts `codex app-server --managed-daemon` as its child, in a
  session of its own (measured in a private tmux server, offline). The gate's
  pre-send recheck holds a pane while it has a detached descendant, unless a
  trusted turn end dates it as an earlier turn's -- and a provider that has
  run no turn has no turn end. So the first gate said idle, the recheck said
  `pane_child_work`, and the notice was requeued, every time.
* Nothing ever looked at a delivery again once its send was traced.

Gate cases use the real PaneActivityGate on the measured Codex 0.162 screen
(tests/fixtures/syrd566_provider_screens.json) and its measured process shape.
The board cases use a production-built board (schema.sql, the real
ticket-board-migrate, rbac.sql), the real HTTP server, the real listener and
gate; only tmux, directorctl and the process table are stood in for. The
before-run is main's code and SQL, in a child built from a git archive.
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
BEFORE = "d57749b2302b0ae3a2b1a04b7f63cbe8543b5c84"  # main before SYRD-589
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
WORKER = "pgu-main:0.0"  # the example workflow's Codex implementer
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def codex_ready_screen() -> tuple[list[str], int, int]:
    """Codex 0.162.0 at its startup prompt, as captured for SYRD-566."""
    screens = json.loads((ROOT / "tests/fixtures/syrd566_provider_screens.json").read_text())
    ready = screens["captures"]["codex_ready"]
    return list(ready["rows"]), int(ready["cursor"][0]), int(ready["cursor"][1])


class Provider:
    """One generation of a pane's provider: the pane process and the daemon it starts in a session of its own."""

    def __init__(self, pid: int, started: float) -> None:
        self.pid, self.started = pid, started
        self.children: list[tuple[int, int, float]] = [(pid + 89, pid + 89, started + 0.4)]  # (pid, session, start)

    def rows(self, with_starts: bool = True) -> list[tuple]:
        rows = [(self.pid, 1, 50, self.pid, self.started)] + [(pid, self.pid, 7, session, start)
                                                              for pid, session, start in self.children]
        return rows if with_starts else [row[:4] for row in rows]


class Pane:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.flicker = False
        self.captures = 0
        self.with_starts = True

    def screen(self) -> tuple[list[str], int, int]:
        rows, x, y = codex_ready_screen()
        self.captures += 1
        if self.flicker:
            rows[43] = f"  Working ({self.captures}s)"
        return rows, x, y


def fake_tmux(panes: dict[str, Pane]):
    def tmux(argv, **_kwargs):
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        pane = panes.get(target)
        if pane is None:
            raise subprocess.CalledProcessError(1, argv, "", f"can't find pane: {target}")
        if argv[:2] == ["tmux", "capture-pane"]:
            rows, _x, _y = pane.screen()
            return subprocess.CompletedProcess(argv, 0, "".join(row + "\n" for row in rows), "")
        if argv[:2] == ["tmux", "display-message"]:
            fmt = argv[-1]
            if "cursor_x" in fmt:
                _rows, x, y = pane.screen()
                return subprocess.CompletedProcess(argv, 0, f"{x} {y} 45\n", "")
            if fmt == "#{pane_pid}":
                return subprocess.CompletedProcess(argv, 0, f"{pane.provider.pid}\n", "")
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        raise AssertionError(f"unexpected tmux call {argv}")

    def table():
        return [row for pane in panes.values() for row in pane.provider.rows(pane.with_starts)]

    return tmux, table


def gate_cases(nl) -> dict:
    """The gate's two verdicts on one Codex pane, for each hook state a restored pane can hold."""
    now = time.time()
    started = now - 120.0
    seen: dict = {}
    cases = {
        # The launcher's own seed, settled: what a restored or started pane holds until its first turn.
        "launcher_seed": dict(state=("idle", "team_launcher.start", started + 2)),
        # Codex announced its session (on 0.162 that waits for the first input, but a resume may send it).
        "session_start": dict(state=("idle", "codex.SessionStart", started + 5)),
        # The previous provider's turn end, left in the file when the pane was replaced without a reseed.
        "stale_turn_end": dict(state=("idle", "codex.Stop", now - 3 * 86400)),
        # A turn ended in THIS provider: its daemon is older than the turn end -- unchanged, idle.
        "turn_ended_here": dict(state=("idle", "codex.Stop", started + 30)),
        # A turn ended here and left a command running in a session of its own: unchanged, held.
        "turn_left_a_command": dict(state=("idle", "codex.Stop", started + 30), command_at=started + 40),
        # A turn is running.
        "turn_running": dict(state=("busy", "codex.UserPromptSubmit", started + 20)),
        # No turn yet, but the screen is moving: the first gate's probe still holds it.
        "launcher_seed_screen_moving": dict(state=("idle", "team_launcher.start", started + 2), flicker=True),
        # A viewer's attach-or-start re-seeded a pane running for minutes, erasing its turns: no answer, held as before.
        "viewer_reseed": dict(state=("idle", "team_launcher.start", started + 90)),
        # The previous provider's turn never ended; the first gate recovers it. Still no turn in this one.
        "stale_busy_recovered": dict(state=("busy", "codex.UserPromptSubmit", now - 3 * 86400)),
        # A turn started here, left a command, and the listener later recovered its stale busy hook: a turn ran.
        "turn_then_recovered": dict(prior=("busy", "codex.UserPromptSubmit", started + 20),
                                    state=("idle", "listener.stale_codex_busy_recovery", started + 100),
                                    command_at=started + 25),
        # Claude's idle prompt, from a record with no turn start (SYRD-101's incident shape) and a command
        # a turn left sleeping: missing evidence of a turn is not proof of none -- the turn's command is judged as before.
        "idle_prompt_no_turn_record": dict(state=("idle", "claude.Notification.idle_prompt", started + 3600),
                                           command_at=started + 1800, runtime="claude"),
        # This provider's own busy hook, with no turn start on record, recovered as stale: no proof of no turn.
        "recovered_unknown_turn": dict(state=("busy", "codex.PreToolUse", started), command_at=started + 15),
        # A process table without start times cannot say when the provider started: unchanged, held.
        "launcher_seed_unknown_start": dict(state=("idle", "team_launcher.start", started + 2), unknown_start=True),
    }
    for name, case in cases.items():
        provider = Provider(700, started)
        if "command_at" in case:
            provider.children.append((751, 751, case["command_at"]))
        pane = Pane(provider)
        pane.flicker = bool(case.get("flicker"))
        pane.with_starts = not case.get("unknown_start")
        tmux, table = fake_tmux({WORKER: pane})
        with tempfile.TemporaryDirectory(prefix="syrd589-gate.") as state_dir:
            store = nl.PaneHookStateStore(Path(state_dir))
            for state, source, at in (case["prior"], case["state"]) if "prior" in case else (case["state"],):
                store.write(WORKER, state, source=source, now=at)
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       role_runtimes={"main": case.get("runtime", "codex")})
            gate.role_targets = {"main": WORKER}
            turnless = getattr(gate, "turnless_since", None)
            asked_first = (turnless(WORKER) is not None) if turnless else None  # before the gate rewrites anything
            first = gate.is_busy(WORKER)
            first_reason = gate.last_trace(WORKER).reason
            recheck = gate.pre_send_busy(WORKER)
            recheck_reason = gate.last_trace(WORKER).reason
            seen[name] = {"first": [first, first_reason], "recheck": [recheck, recheck_reason],
                          "turnless": (turnless(WORKER) == started) if turnless else None, "asked_first": asked_first}
    return seen


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from scripts.session_paths import seed_initial_pane_idle_state
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    seen: dict = {"gate": gate_cases(nl)}
    with temporary_cluster(prefix="s589", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd589-state.") as state_dir:
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
        pane = Pane(Provider(600, time.time() - 300))
        tmux, table = fake_tmux({WORKER: pane})
        store.write(WORKER, "idle", source="codex.Stop", now=time.time() - 200)  # a turn ended in that provider
        # The listener's gate; its clock runs a minute ahead so a launcher seed written just now reads as settled.
        gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                   pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                   role_runtimes={"main": "codex", "app": "claude", "ops": "claude", "director": "claude"},
                                   wall_time=lambda: time.time() + 60)
        sends: list[tuple[str, str]] = []

        def directorctl(target: str, message: str) -> dict:
            """Typed and submitted; the provider takes it and its turn starts."""
            sends.append((target, message))
            store.write(target, "busy", source="codex.UserPromptSubmit")
            return {}

        def new_listener():
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=gate.is_working,
                target_exists=lambda _target: True, poll_seconds=0, submission_confirm_seconds=0.4,
                submission_poll_seconds=0.05)
            listener.delivery_submitter = lambda _target: None
            return listener

        def one_pass() -> None:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            listener = new_listener()
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                from scripts.ticket_board import delivery_proof
                try:
                    from scripts.ticket_board import replaced_delivery
                except ImportError:  # main has none
                    replaced_delivery = None
                delivery_proof.run(listener, conn)
                if replaced_delivery is not None:
                    replaced_delivery.run(listener, conn)
                listener.process_due_notifications(conn)

        def real_pass() -> None:
            """The listener's own loop, once: stops after its first delivery, or after a pass that delivers nothing."""
            listener = new_listener()
            listener.listen_once(max_notifications=listener.delivered_count + 1)

        def redeliver_as_listener(started: float) -> int | None:
            try:
                with psycopg.connect(listener_url, autocommit=True) as conn:
                    row = conn.execute("SELECT ticket_board.redeliver_replaced_work(%s::jsonb)",
                                       (json.dumps({"main": datetime.fromtimestamp(started, timezone.utc).isoformat()}),)).fetchone()
            except psycopg.errors.UndefinedFunction:  # main has none
                return None
            return int(row[0])

        def dismiss(ticket: str) -> None:
            """The Director dismisses whatever is queued for the ticket, as the listener's discard does."""
            with psycopg.connect(listener_url, autocommit=True) as conn:
                for (notification,) in conn.execute("SELECT id FROM ticket_board.ticket_notification_queue "
                                                    f"WHERE ticket_id = '{ticket}'").fetchall():
                    conn.execute("SELECT ticket_board.discard_notification(%s, 'Director dismissed')", (notification,))

        def turn_ends() -> None:
            store.write(WORKER, "idle", source="codex.Stop")

        def restore() -> Provider:
            """The pane is replaced: a new provider process, its daemon, and the launcher's seed. No turn."""
            time.sleep(1.2)
            provider = Provider(pane.provider.pid + 100, time.time() - 0.5)
            pane.provider = provider
            seed(provider.started + 0.3)
            return provider

        def seed(at: float) -> None:
            """The launcher's own seed writer: a fresh idle record, carrying nothing."""
            seed_initial_pane_idle_state(SimpleNamespace(target=WORKER), pane_state_dir=Path(state_dir),
                                         source="team_launcher.start", now=at)

        def route(ticket: str, state: str, role: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": state, "assignee": role},
                              caller="director")

        def queued(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(target_role, coalesce(last_error, ''), "
                                  "attempts, revision, dead_lettered_at IS NOT NULL, payload ? 'redelivery') ORDER BY id), "
                                  f"'[]')::text FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';"))

        def holds(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(busy_reason ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND event = 'gate_defer';"))

        def enqueued(ticket: str) -> list[str]:
            """What each notice queued for the ticket was, by its dedupe key's kind, oldest first."""
            return json.loads(sql("SELECT coalesce(json_agg(split_part(detail->>'dedupe_key', ':', 1) ORDER BY id), '[]')::text "
                                  f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' AND event = 'enqueue';"))

        def delivery(ticket: str) -> dict:
            import urllib.request
            with urllib.request.urlopen(f"{base}/api/tickets/{ticket}") as response:
                return json.loads(response.read()).get("active_work_delivery") or {}

        def worker_sends() -> list[str]:
            return [message for target, message in sends if target == WORKER]

        def notified_after(ticket: str, provider: Provider) -> bool | None:
            at = delivery(ticket).get("at")
            if not at:
                return None
            from datetime import datetime
            return datetime.fromisoformat(at).timestamp() > provider.started

        # 1. Delivered to the provider of the day: the ordinary notice, typed once.
        fixture.seed_postgres_ticket(admin, "PGU-801", title="Restored worker", state="analysis", assignee="director",
                                     commit_exempt=True)
        route("PGU-801", "in_progress", "main")
        one_pass()
        turn_ends()
        seen["delivered"] = [len(worker_sends()), delivery("PGU-801").get("state"), queued("PGU-801")]

        # 2. MEFP: the pane is restored. A new provider at its startup prompt, the ticket still read as delivered.
        replaced = restore()
        mark = len(enqueued("PGU-801"))
        real_pass()
        seen["restored_pass"] = [worker_sends()[1:], queued("PGU-801"), delivery("PGU-801").get("state"),
                                 notified_after("PGU-801", replaced), enqueued("PGU-801")[mark:]]
        turn_ends()
        one_pass()
        one_pass()
        seen["restored_later_passes"] = [len(worker_sends()), queued("PGU-801")]

        # 3. The canary: restored again, and the Director routes the ticket out and back -- an ordinary notice.
        replaced = restore()
        before, mark = len(worker_sends()), len(enqueued("PGU-801"))
        fixture.post_json(base, "/api/tickets/PGU-801/actions/force_move", {"state": "analysis", "assignee": "director"},
                          caller="director")
        route("PGU-801", "in_progress", "main")
        one_pass()
        seen["canary"] = [worker_sends()[before:], queued("PGU-801"), holds("PGU-801"),
                          delivery("PGU-801").get("state"), notified_after("PGU-801", replaced), enqueued("PGU-801")[mark:]]
        turn_ends()

        # 4. Restored, then delivered by hand (the Director's bounded manual delivery): a turn ran, nothing more is sent.
        replaced = restore()
        before = len(worker_sends())
        store.write(WORKER, "busy", source="codex.UserPromptSubmit")
        turn_ends()
        one_pass()
        one_pass()
        seen["manual_delivery"] = [worker_sends()[before:], queued("PGU-801")]

        # 5. Once per provider: restored, the redelivery cannot be sent (the screen keeps moving), and the
        #    Director dismisses it -- it is never queued again for that provider.
        replaced = restore()
        before = len(worker_sends())
        future = redeliver_as_listener(time.time() + 3600)  # a start later than now names no provider
        future_queue = queued("PGU-801")
        pane.flicker = True
        one_pass()
        first_queue = queued("PGU-801")
        one_pass()
        second_queue = queued("PGU-801")
        dismiss("PGU-801")
        pane.flicker = False
        one_pass()
        one_pass()
        seen["once_per_provider"] = [first_queue, second_queue, queued("PGU-801"), worker_sends()[before:],
                                     future, future_queue]

        # 5b. Another notice of the ticket is already queued for the role: nothing is queued beside it. Once it
        #     is gone, the replaced provider is told.
        replaced = restore()
        before, mark = len(worker_sends()), len(enqueued("PGU-801"))
        pane.flicker = True
        sql("SELECT ticket_board.enqueue_notification('PGU-801', 'transition', 'main', 'PGU-801 -- other notice', "
            "'{\"kind\": \"transition\", \"id\": \"PGU-801\", \"new_state\": \"in_progress\"}'::jsonb, 'test:other');")
        one_pass()
        beside = [queued("PGU-801"), enqueued("PGU-801")[mark:]]
        dismiss("PGU-801")
        pane.flicker = False
        one_pass()
        turn_ends()
        seen["beside_another"] = beside + [worker_sends()[before:], enqueued("PGU-801")[mark:]]

        # 5c. MEFP's own plan: the canary's notice is dismissed before it was ever sent, for a delivery by hand.
        #     The stage visit has no send, so nothing is sent for it.
        replaced = restore()
        before, mark = len(worker_sends()), len(enqueued("PGU-801"))
        pane.flicker = True
        fixture.post_json(base, "/api/tickets/PGU-801/actions/force_move", {"state": "analysis", "assignee": "director"},
                          caller="director")
        route("PGU-801", "in_progress", "main")
        one_pass()
        dismiss("PGU-801")
        pane.flicker = False
        one_pass()
        one_pass()
        seen["dismissed_unsent"] = [worker_sends()[before:], queued("PGU-801"), enqueued("PGU-801")[mark:],
                                    delivery("PGU-801").get("state")]

        # 6. A provider started after its ticket's notice was sent got that notice: nothing is sent again.
        turn_ends()
        fixture.seed_postgres_ticket(admin, "PGU-802", title="Second", state="analysis", assignee="director", commit_exempt=True)
        fixture.post_json(base, "/api/tickets/PGU-801/actions/force_move", {"state": "done", "assignee": "unassigned"},
                          caller="director")
        replaced = restore()
        route("PGU-802", "in_progress", "main")
        before = len(worker_sends())
        one_pass()
        seed(time.time())  # a viewer's attach-or-start re-seeds it, inside its startup: no turn on record
        turnless = getattr(gate, "turnless_since", None)
        precondition = (turnless(WORKER) == replaced.started) if turnless else None
        one_pass()
        one_pass()
        seen["sent_to_this_provider"] = [worker_sends()[before:], queued("PGU-802"), precondition]

        # Who may run it, and a fresh schema.sql board installs the same function.
        function = "redeliver_replaced_work(jsonb, timestamp with time zone)"
        try:
            seen["grantees"] = sql("SELECT string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, '+' "
                                   "ORDER BY CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END) "
                                   f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{function}'::regprocedure "
                                   "AND a.privilege_type = 'EXECUTE';")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            definition = f"SELECT md5(pg_get_functiondef('ticket_board.{function}'::regprocedure));"
            seen["fresh_equals_migrated"] = fixture.psql(fresh, definition).strip() == sql(definition)
            try:
                with psycopg.connect(fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE),
                                     autocommit=True) as conn:
                    conn.execute("SELECT ticket_board.redeliver_replaced_work('{}'::jsonb)")
                seen["service_refused"] = False
            except psycopg.Error as exc:
                seen["service_refused"] = "permission denied" in str(exc)
        except AssertionError as exc:  # main has none
            seen["grantees"] = seen["fresh_equals_migrated"] = seen["service_refused"] = f"unavailable: {str(exc)[-120:]}"
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
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    # Mutation runs may keep main's observations in a file: they come from BEFORE's archive and this
    # file's scenario, which no mutant of the tree touches.
    cached = Path(os.environ.get("SYRD589_BEFORE_JSON", "")) if os.environ.get("SYRD589_BEFORE_JSON") else None
    if cached is not None and cached.is_file():
        before = json.loads(cached.read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="syrd589.") as tmp:
            before_root = tree_at(BEFORE, Path(tmp) / "before")
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                                   capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                   timeout=900)
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        if cached is not None:
            cached.write_text(json.dumps(before))
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    print("OBSERVED " + json.dumps({"before": before, "after": after}), file=sys.stderr)

    # main: an untouched Codex pane passes the first gate and is held at the recheck by its own daemon,
    # and a restored worker's ticket stays "delivered" with nothing ever sent to the new provider.
    for name in ("launcher_seed", "session_start", "stale_turn_end"):
        check(before["gate"][name]["first"] == [False, "working_timer_idle"]
              and before["gate"][name]["recheck"] == [True, "pane_child_work"],
              f"before: {name}: idle at the first gate, held by the daemon at the recheck: {before['gate'][name]}")
    check(before["restored_pass"][0] == [] and before["restored_pass"][2] == "delivered" and before["restored_pass"][3] is False,
          f"before: the restored provider is sent nothing and the ticket reads delivered to the old one: {before['restored_pass']}")
    check(before["canary"][0] == [] and before["canary"][1] and before["canary"][1][0][1] == "pane busy"
          and "pane_child_work" in before["canary"][2],
          f"before: the canary's ordinary notice is requeued as pane busy by the daemon: {before['canary']}")

    gate = after["gate"]
    # The pane at its startup prompt is deliverable, at both gates, whatever the launcher or an old provider left.
    for name in ("launcher_seed", "session_start", "stale_turn_end"):
        check(gate[name]["first"] == [False, "working_timer_idle"] and gate[name]["recheck"] == [False, "working_timer_idle"]
              and gate[name]["turnless"] is True,
              f"{name}: a provider that has run no turn is deliverable at the recheck too: {gate[name]}")
    # Everything a turn makes evident is held exactly as before.
    for name in ("turn_ended_here", "turn_left_a_command", "turn_running", "launcher_seed_screen_moving",
                 "viewer_reseed", "turn_then_recovered", "idle_prompt_no_turn_record", "recovered_unknown_turn",
                 "launcher_seed_unknown_start"):
        check(gate[name]["first"] == before["gate"][name]["first"] and gate[name]["recheck"] == before["gate"][name]["recheck"],
              f"{name}: unchanged from main: {gate[name]} vs {before['gate'][name]}")
    check(gate["turn_ended_here"]["recheck"] == [False, "stale_prior_turn_child_work"] and gate["turn_ended_here"]["turnless"] is False,
          f"a turn ended in this provider: the daemon is that turn's furniture, as before: {gate['turn_ended_here']}")
    check(gate["turn_left_a_command"]["recheck"] == [True, "pane_child_work"],
          f"a command started after the turn ended still holds the pane: {gate['turn_left_a_command']}")
    check(gate["turn_running"]["first"] == [True, "hook_busy"] and gate["turn_running"]["turnless"] is False,
          f"a running turn holds the pane: {gate['turn_running']}")
    check(gate["launcher_seed_screen_moving"]["first"] == [True, "pane_content_changed"],
          f"a moving screen still holds a provider with no turn: {gate['launcher_seed_screen_moving']}")
    check(gate["stale_busy_recovered"]["first"] == [False, "stale_codex_busy_recovered"]
          and gate["stale_busy_recovered"]["recheck"] == [False, "hook_idle"] and gate["stale_busy_recovered"]["turnless"] is True
          and before["gate"]["stale_busy_recovered"]["recheck"] == [False, "stale_prior_turn_child_work"],
          f"a previous provider's unfinished turn, recovered, is not a turn of this one: {gate['stale_busy_recovered']}")
    check(gate["turn_then_recovered"]["recheck"] == [False, "stale_prior_turn_child_work"]
          and gate["turn_then_recovered"]["turnless"] is False,
          f"a turn that started here counts, whatever wrote the state since: {gate['turn_then_recovered']}")
    check(gate["idle_prompt_no_turn_record"]["recheck"] == [False, "stale_prior_turn_child_work"]
          and gate["idle_prompt_no_turn_record"]["turnless"] is False,
          f"a record that merely lacks a turn start is no proof of none: {gate['idle_prompt_no_turn_record']}")
    check(gate["recovered_unknown_turn"]["first"] == [False, "stale_codex_busy_recovered"]
          and gate["recovered_unknown_turn"]["turnless"] is False,
          f"a recovered busy hook of this provider's, with no turn start, is no proof of none: {gate['recovered_unknown_turn']}")
    check(gate["turn_running"]["asked_first"] is False and gate["stale_busy_recovered"]["asked_first"] is False
          and gate["launcher_seed"]["asked_first"] is True,
          f"a hook that says busy is never a provider without a turn, whatever its age: "
          f"{[gate[k]['asked_first'] for k in ('turn_running', 'stale_busy_recovered', 'launcher_seed')]}")
    check(gate["viewer_reseed"]["recheck"] == [True, "pane_child_work"] and gate["viewer_reseed"]["turnless"] is False,
          f"a seed long after the provider started is a re-seed of a running pane: held as before: {gate['viewer_reseed']}")
    check(gate["launcher_seed_unknown_start"]["recheck"] == [True, "pane_child_work"]
          and gate["launcher_seed_unknown_start"]["turnless"] is False,
          f"an unknown start time is no answer: held as before: {gate['launcher_seed_unknown_start']}")

    check(after["delivered"][:2] == [1, "delivered"] and after["delivered"][2] == [],
          f"the ordinary notice is delivered once: {after['delivered']}")
    restored = after["restored_pass"]
    check(len(restored[0]) == 1 and restored[0][0].startswith("PGU-801 -- Restored worker is active again in ")
          and restored[1] == [] and restored[2] == "delivered" and restored[3] is True
          and restored[4].count("redelivery") == 1,
          f"the restored provider is told its ticket once, in the repeat wording, and the delivery is now its: {restored}")
    check(after["restored_later_passes"] == [2, []],
          f"later passes send nothing more: {after['restored_later_passes']}")
    canary = after["canary"]
    check(len(canary[0]) == 1 and canary[0][0].startswith("PGU-801 -- Restored worker ")
          and canary[1] == [] and canary[2] == [] and canary[3] == "delivered" and canary[4] is True
          and canary[5] and "redelivery" not in canary[5],
          f"the canary's ordinary notice reaches the restored pane at once, alone: {canary}")
    check(after["manual_delivery"] == [[], []],
          f"a provider that already ran a turn since it started is sent nothing: {after['manual_delivery']}")
    once = after["once_per_provider"]
    check(len(once[0]) == 1 and once[0][0][1] == "pane busy" and once[0][0][5] is True
          and len(once[1]) == 1 and once[1][0][3] == once[0][0][3]
          and once[2] == [] and once[3] == [] and once[4] == 0 and once[5] == [],
          f"one redelivery per provider: held, never re-queued or refreshed, and gone for good once dismissed: {once}")
    beside = after["beside_another"]
    check(len(beside[0]) == 1 and beside[0][0][5] is False and "redelivery" not in beside[1]
          and len(beside[2]) == 1 and "active again" in beside[2][0] and beside[3][-1] == "redelivery",
          f"nothing is queued beside another notice of the ticket; once it is gone the provider is told: {beside}")
    unsent = after["dismissed_unsent"]
    check(unsent[0] == [] and unsent[1] == [] and "redelivery" not in unsent[2],
          f"a stage visit whose notice was dismissed unsent is not sent by the board: {unsent}")
    check(after["sent_to_this_provider"][0] and len(after["sent_to_this_provider"][0]) == 1
          and "active again" not in after["sent_to_this_provider"][0][0] and after["sent_to_this_provider"][1] == []
          and after["sent_to_this_provider"][2] is True,
          f"a notice sent to the provider now running is never sent again: {after['sent_to_this_provider']}")
    # Grants and copy parity last: they must not stand in for a behaviour check under mutation.
    if os.environ.get("SYRD589_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["grantees"] == "postgres+ticket_board_listener" and after["service_refused"] is True,
              f"only the listener (and the owner) may run it: {after['grantees']}, service refused {after['service_refused']}")
        check(after["fresh_equals_migrated"] is True, "a fresh schema.sql board installs the same function")
    print(f"replaced_provider_redelivery_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
