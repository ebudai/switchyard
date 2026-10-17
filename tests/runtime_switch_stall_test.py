#!/usr/bin/env python3
"""SYRD-595: across a runtime switch, a stalled ticket is told to one recipient at a time, a bounded number of times.

On 2026-10-16 SYRD Audit's agy provider ran out of quota. Its agy log shows the
two SYRD-594 notices it was given each failing on `RESOURCE_EXHAUSTED (code
429)`; the board then saw no turn end for 5.5 hours and told nobody. The
Director switched Audit to Codex, and the new pane got no work until it was sent
some by hand: the live release (3d410ca) predates SYRD-589's redelivery.

Measured on main (BEFORE) with a provider whose every turn fails: the owner is
prompted "this turn ended without resolving it" once per grace period without
end, and the Director is told "may be stuck" and "the grace period has passed"
in between. With a hook left busy after the failure, nothing is sent to anyone.

Every board case runs on a production-built board, the real server, the real
listener loop and the real gate, in real time, every timing a thirtieth of
production's; each pass builds a new listener and the gate starts again every
few seconds. Runtime registrations are real board rows backed by live
processes. The before-run is BEFORE, in a child process built from a git
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
BEFORE = "9f762d180e572e21c8c5ab807384ab066f433b9c"  # main when SYRD-595 was cut (SYRD-594 merged)
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
WORKER = "pgu-main:0.0"
DIRECTOR = "pgu-director:0.0"
#: Production's idle grace (45 s) and unresolved-turn grace (600 s), a thirtieth of each.
GRACE, UNRESOLVED = 1.5, 20
ATTEMPTS = 3
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Provider:
    """One runtime generation of the worker's pane: a live process the board registers, and how its turns go."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(["sleep", "900"])
        self.pid = self.process.pid
        self.ticks = int(Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        self.started = time.time()

    def stop(self) -> None:
        self.process.kill()
        self.process.wait()


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    from types import SimpleNamespace

    import codex_capacity_retry_test as c594
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow

    from scripts.session_paths import seed_initial_pane_idle_state
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    shown = c594.screens()
    seen: dict = {}
    providers: list[Provider] = []
    with temporary_cluster(prefix="s595", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd595-state.") as state_dir:
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

        def register(role: str, runtime: str, target: str, provider: Provider) -> int:
            previous = sql(f"SELECT coalesce((SELECT generation FROM ticket_board.role_runtime_assignments WHERE role = '{role}'), 0);")
            return int(sql(
                f"SELECT generation FROM ticket_board.register_role_runtime('{role}', '{runtime}', '{target}', '/tmp', '/tmp', "
                f"{provider.pid}, {provider.ticks}, {os.getuid()}, {previous});"))

        store = nl.PaneHookStateStore(Path(state_dir))
        worker_provider, director_provider = Provider(), Provider()
        providers += [worker_provider, director_provider]
        worker = c594.Pane(shown["recovered"], worker_provider.pid, worker_provider.started)
        director = c594.Pane(shown["director"], director_provider.pid, director_provider.started)
        tmux, table = c594.fake_tmux({WORKER: worker, DIRECTOR: director})
        current = {"generation": register("main", "codex", WORKER, worker_provider)}
        register("director", "claude", DIRECTOR, director_provider)
        store.write(WORKER, "idle", source="codex.Stop", now=time.time() - 600)
        store.write(DIRECTOR, "idle", source="claude.Stop", now=time.time() - 600)
        sends: list[list] = []  # [seconds, recipient, generation it reached, message]
        clock = {"t0": time.monotonic(), "mode": "fails"}  # how the worker's provider takes a turn

        def directorctl(target: str, message: str) -> dict:
            at = round(time.monotonic() - clock["t0"], 1)
            if target == DIRECTOR:
                sends.append([at, "director", None, message])
                store.write(target, "busy", source="claude.UserPromptSubmit")
                store.write(target, "idle", source="claude.Stop")
                return {}
            sends.append([at, "main", current["generation"], message])
            store.write(target, "busy", source="codex.UserPromptSubmit")
            worker.screen = shown["quota"]
            if clock["mode"] == "fails":  # the provider fails on its quota a moment later, and the turn ends
                threading.Timer(0.8, lambda: store.write(target, "idle", source="codex.Stop")).start()
            return {}  # "stuck": the hook says busy from here on, and nothing on the screen moves

        def new_gate():
            return nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=0,
                                       role_runtimes={"main": "codex", "director": "claude"})

        process = {"gate": new_gate(), "restarts": 0}

        def real_pass() -> None:
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=process["gate"].is_working,
                target_exists=lambda target: target in (WORKER, DIRECTOR), poll_seconds=0,
                submission_confirm_seconds=0.4, submission_poll_seconds=0.05, idle_stall_grace_seconds=GRACE)
            listener.unresolved_turn_grace_seconds = UNRESOLVED
            listener.delivery_submitter = lambda _target: None
            listener.listen_once(max_notifications=listener.delivered_count + 1)

        def production_pass() -> None:
            """The listener's loop once as production runs it: every producer, then every notice due, delivered."""
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=process["gate"].is_working,
                target_exists=lambda target: target in (WORKER, DIRECTOR), poll_seconds=0,
                submission_confirm_seconds=0.4, submission_poll_seconds=0.05, idle_stall_grace_seconds=GRACE)
            listener.unresolved_turn_grace_seconds = UNRESOLVED
            listener.delivery_submitter = lambda _target: None
            listener.listen_once(max_notifications=100)

        def run_for(seconds: float) -> None:
            end = time.monotonic() + seconds
            last_restart = time.monotonic()
            while time.monotonic() < end:
                if time.monotonic() - last_restart >= 4.0:  # the listener process starts again
                    process["gate"] = new_gate()
                    process["restarts"] += 1
                    last_restart = time.monotonic()
                real_pass()
                time.sleep(0.2)

        def run_until(predicate, limit: float) -> bool:
            end = time.monotonic() + limit
            while time.monotonic() < end:
                if predicate():
                    return True
                run_for(1.0)
            return predicate()

        def act(ticket: str, action: str, body: dict, caller: str = "director") -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/{action}", body, caller=caller)

        def start(ticket: str, title: str, mode: str) -> None:
            clock["mode"], clock["t0"] = mode, time.monotonic()
            sends.clear()
            worker.screen = shown["recovered"]
            # The pane's provider ended its last turn a moment ago, after everything it runs had started.
            store.write(WORKER, "idle", source="codex.Stop", now=time.time() - 3)
            fixture.seed_postgres_ticket(admin, ticket, title=title, state="analysis", assignee="director", commit_exempt=True)
            act(ticket, "route", {"state": "in_progress", "assignee": "main"})

        def finish(ticket: str) -> None:
            act(ticket, "force_move", {"state": "done", "assignee": "unassigned"})
            run_for(0.6)

        def told(ticket: str) -> list[list]:
            """Every notice about the ticket: [seconds since its first, recipient, generation reached, first words]."""
            return [[at, to, generation, message if to == "director" else message[:70]]
                    for at, to, generation, message in sends if ticket in message]

        def delivery(ticket: str) -> dict:
            import urllib.request
            with urllib.request.urlopen(f"{base}/api/tickets/{ticket}") as response:
                return json.loads(response.read()).get("active_work_delivery") or {}

        def episodes(ticket: str):
            if sql("SELECT to_regclass('ticket_board.role_stall_episodes') IS NULL;") == "t":
                return None
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_object('phase', phase, 'phase_no', phase_no, 'closed', close_reason, "
                "'phases', (SELECT json_agg(json_build_array(p->>'recipient', p->>'reason')) FROM jsonb_array_elements(phases) p)) "
                f"ORDER BY id), '[]')::text FROM ticket_board.role_stall_episodes WHERE ticket_id = '{ticket}';"))

        def escalation_traces() -> list:
            """Each stall escalation's own trace events: it was claimed and sent, durably, whatever it says."""
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(e.notification_id, (SELECT array_agg(DISTINCT t.event ORDER BY t.event) "
                "FROM ticket_board.notification_trace t WHERE t.notification_id = e.notification_id)) ORDER BY e.id), '[]')::text "
                "FROM ticket_board.notification_trace e WHERE e.event = 'enqueue' "
                "AND e.detail->>'dedupe_key' LIKE 'stall-escalation:%';"))

        def escalations() -> list[str]:
            return [message for _at, to, _g, message in sends if to == "director"]

        # 1. Runtime A fails on its quota at every turn; the Director replaces it with runtime B, which fails too.
        start("PGU-960", "Work whose provider is out of quota", "fails")
        # Long enough for three prompts a grace apart, the escalation, and the silence after it.
        run_for(2.5 * UNRESOLVED + 8.0)
        reached = len(escalations()) >= 1
        before_switch = told("PGU-960")
        # The switch: A's process ends, B registers as the next generation, its state file starts from the seed.
        worker_provider.stop()
        replacement = Provider()
        providers.append(replacement)
        current["generation"] = register("main", "codex", WORKER, replacement)
        (Path(state_dir) / (WORKER.replace(":", "_") + ".json")).unlink(missing_ok=True)
        worker.pid, worker.started, worker.screen = replacement.pid, replacement.started, shown["recovered"]
        seed_initial_pane_idle_state(SimpleNamespace(target=WORKER), pane_state_dir=Path(state_dir),
                                     source="team_launcher.start", now=replacement.started + 0.3)
        switched_at = round(time.monotonic() - clock["t0"], 1)
        delivery_after_switch = delivery("PGU-960")
        run_for(2.5 * UNRESOLVED + 8.0)
        seen["switch"] = {"reached_first": reached, "before_switch": before_switch, "switched_at": switched_at,
                          "generations": [1, current["generation"]], "told": told("PGU-960"),
                          "delivery_after_switch": delivery_after_switch, "delivery_end": delivery("PGU-960"),
                          "episodes": episodes("PGU-960"), "restarts": process["restarts"]}
        finish("PGU-960")

        # 2. The provider takes the work and its hook says busy from then on, nothing moving on screen.
        start("PGU-961", "Work whose provider stopped mid-turn", "stuck")
        run_for(UNRESOLVED + 8.0)
        seen["stuck"] = {"told": told("PGU-961"), "episodes": episodes("PGU-961"), "trace": json.loads(sql(
            "SELECT coalesce(json_agg(json_build_array(target_role, kind, event, coalesce(busy_reason, '')) ORDER BY id), "
            "'[]')::text FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-961' AND event <> 'gate_defer';"))[:12]}
        finish("PGU-961")
        seen["stuck"]["after_done"] = episodes("PGU-961")

        # 3. Escalated, then handed back by a Director comment: a new phase for the owner, then one more escalation.
        start("PGU-962", "Work handed back by the Director", "fails")
        first = run_until(lambda: len(escalations()) >= 1, 45)
        run_for(3.0)
        act("PGU-962", "add_comment", {"text": "Director: the provider has capacity again; carry on with PGU-962."})
        commented_at = round(time.monotonic() - clock["t0"], 1)
        run_until(lambda: len(escalations()) >= 2, 60)
        run_for(4.0)
        seen["rearm"] = {"first": first, "commented_at": commented_at, "told": told("PGU-962"),
                         "episodes": episodes("PGU-962")}
        finish("PGU-962")

        # 4. The listener delivers the notice and is down before the turn's end is seen; it comes back after both
        #    the unresolved-turn grace and the idle grace have passed, so one pass finds every reminder due at once.
        start("PGU-964", "Work the listener comes back to", "fails")
        end = time.monotonic() + 10
        while time.monotonic() < end and not any(to == "main" for _a, to, _g, _m in sends):
            real_pass()
            time.sleep(0.2)
        time.sleep(UNRESOLVED + 3.0)
        process["gate"] = new_gate()
        back = round(time.monotonic() - clock["t0"], 1)
        production_pass()
        first_pass = [row[1] for row in told("PGU-964") if row[0] >= back]
        run_for(2.5 * UNRESOLVED + 8.0)
        seen["downtime"] = {"back": back, "first_pass": first_pass, "told": told("PGU-964")}
        finish("PGU-964")

        # 5. The owner writes on the ticket after its first prompt: progress, not a stall.
        start("PGU-963", "Work whose owner answers", "fails")
        run_until(lambda: any("still yours" in m for _a, to, _g, m in sends if to == "main"), 15)
        act("PGU-963", "add_comment", {"text": "main: working on PGU-963, the provider is back."}, caller="main")
        run_for(4.0)
        seen["progress"] = {"told": told("PGU-963"), "episodes": episodes("PGU-963")}
        finish("PGU-963")
        seen["progress"]["after_done"] = episodes("PGU-963")

        seen["escalation_traces"] = escalation_traces()
        seen["functions"] = function_cases(fixture, admin, base, sql, nl, psycopg, listener_url, register, current,
                                           Provider, providers)
        server.shutdown()
        server.server_close()
    for provider in providers:
        provider.stop()
    return seen


def function_cases(fixture, admin, base, sql, nl, psycopg, listener_url, register, current, Provider, providers) -> dict:
    """The board's own answers, asked as the listener asks them."""
    seen: dict = {}

    def as_listener(statement: str, params: tuple = ()):
        with psycopg.connect(listener_url, autocommit=True) as conn:
            return conn.execute(statement, params).fetchone()[0]

    def deliverable(ticket: str, target_role: str, payload: dict) -> bool:
        listener = nl.TicketBoardNotifyListener(conninfo=listener_url, project="pgu", sender=lambda *_a: None,
                                                activity_gate=lambda _t: True, target_exists=lambda _t: True, poll_seconds=0)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            return listener.eligibility._notification_is_current(conn, ticket, target_role, json.dumps(payload))

    fixture.seed_postgres_ticket(admin, "PGU-970", title="Board case", state="analysis", assignee="director", commit_exempt=True)
    fixture.post_json(base, "/api/tickets/PGU-970/actions/route", {"state": "in_progress", "assignee": "main"}, caller="director")
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-970';")  # its notice went
    ticket = {"id": "PGU-970", "state": "in_progress", "assignee": "main"}
    registered = sql("SELECT registered_at FROM ticket_board.role_runtime_assignments WHERE role = 'main';")
    before = sql(f"SELECT ('{registered}'::timestamptz - interval '1 minute')::text;")
    after = sql(f"SELECT ('{registered}'::timestamptz + interval '1 minute')::text;")
    has_episodes = sql("SELECT to_regclass('ticket_board.role_stall_episodes') IS NOT NULL;") == "t"
    # A repair prompt about a turn that ended before the live runtime registered reached the retired provider.
    seen["retired_repair"] = [deliverable("PGU-970", "main", dict(ticket, kind="unresolved_turn_repair", target_role="main",
                                                                  owner_role="main", turn_id=before)),
                              deliverable("PGU-970", "main", dict(ticket, kind="unresolved_turn_repair", target_role="main",
                                                                  owner_role="main", turn_id=after))]
    if has_episodes:
        # What an open episode allows, phase by phase.
        sql("INSERT INTO ticket_board.role_stall_episodes (ticket_id, owner_role, ticket_state, ticket_assignee, round_at, "
            "phase, phase_no, phase_started_at, max_attempts, opened_at) VALUES ('PGU-970', 'main', 'in_progress', 'main', "
            "NULL, 'role', 1, clock_timestamp(), 3, clock_timestamp());")
        cases = {
            "director unresolved_turn": ("director", {"kind": "unresolved_turn"}),
            "director nudge": ("director", {"kind": "nudge"}),
            "director escalation": ("director", {"kind": "escalation"}),
            "director permission prompt": ("director", {"kind": "escalation", "reason": "permission_prompt"}),
            "owner idle_reminder": ("main", {"kind": "idle_reminder"}),
            "owner repair": ("main", {"kind": "unresolved_turn_repair", "turn_id": after}),
            "owner capacity_retry": ("main", {"kind": "capacity_retry"}),
            "owner transition": ("main", {"kind": "transition"}),
        }

        def answers() -> dict:
            return {name: as_listener("SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb)",
                                      ("PGU-970", role, json.dumps(payload)))
                    for name, (role, payload) in cases.items()}

        seen["role_phase"] = answers()
        sql("UPDATE ticket_board.role_stall_episodes SET phase = 'director', escalated_at = clock_timestamp() "
            "WHERE ticket_id = 'PGU-970';")
        seen["director_phase"] = answers()
        episode = int(sql("SELECT id FROM ticket_board.role_stall_episodes WHERE ticket_id = 'PGU-970';"))
        seen["escalation_current"] = [
            as_listener("SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb)",
                        ("PGU-970", "director", json.dumps({"kind": "stall_escalation", "episode": episode, "phase_no": 1}))),
            as_listener("SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb)",
                        ("PGU-970", "director", json.dumps({"kind": "stall_escalation", "episode": episode, "phase_no": 2}))),
            as_listener("SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb)",
                        ("PGU-970", "main", json.dumps({"kind": "stall_escalation", "episode": episode, "phase_no": 1})))]
        sql("UPDATE ticket_board.role_stall_episodes SET phase = 'role' WHERE ticket_id = 'PGU-970';")
        seen["escalation_current"].append(as_listener(
            "SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb)",
            ("PGU-970", "director", json.dumps({"kind": "stall_escalation", "episode": episode, "phase_no": 1}))))
        sql("DELETE FROM ticket_board.role_stall_episodes WHERE ticket_id = 'PGU-970';")
        seen["no_episode"] = answers()
        # No episode yet, the owner's repair prompt on its way: the owner is the one to be told.
        sql("SELECT ticket_board.enqueue_notification('PGU-970', 'unresolved_turn_repair', 'main', 'm', "
            "'{\"kind\": \"unresolved_turn_repair\", \"id\": \"PGU-970\"}'::jsonb, 'probe-repair:PGU-970');")
        seen["pending_repair"] = answers()
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-970';")
        # A fourth repair in one phase is not typed: a phase allows only so many.
        sql("INSERT INTO ticket_board.role_stall_episodes (ticket_id, owner_role, ticket_state, ticket_assignee, round_at, "
            "phase, phase_no, phase_started_at, max_attempts, opened_at) VALUES ('PGU-970', 'main', 'in_progress', 'main', "
            "NULL, 'role', 1, clock_timestamp() - interval '1 hour', 3, clock_timestamp());")
        for n in range(3):
            sql(f"SELECT ticket_board.record_notification_trace('PGU-970', {9000 + n}, 'main', 'unresolved_turn_repair', 'send');")
        seen["fourth_repair"] = deliverable("PGU-970", "main", dict(ticket, kind="unresolved_turn_repair", target_role="main",
                                                                    owner_role="main", turn_id=after))
        sql("DELETE FROM ticket_board.role_stall_episodes WHERE ticket_id = 'PGU-970';")
    # SYRD-589's redelivery, asked about a provider that resumed the conversation the notice went to, and one that
    # did not; and a notice sent but never seen to arrive.
    started = sql("SELECT (clock_timestamp() + interval '1 second')::text;")
    plain = sql("SELECT (clock_timestamp() + interval '2 seconds')::text;")
    sql("SELECT ticket_board.record_notification_trace('PGU-970', 9100, 'main', 'transition', 'send', NULL, NULL, NULL, "
        "'{\"provider_session\": \"conv-1\"}'::jsonb);")
    time.sleep(2.2)
    redeliver = "SELECT ticket_board.redeliver_replaced_work(%s::jsonb)"
    if has_episodes:  # BEFORE knows a provider only by when it started
        seen["resumed"] = as_listener(redeliver, (json.dumps({"main": {"started": started, "session": "conv-1"}}),))
        seen["fresh"] = as_listener(redeliver, (json.dumps({"main": {"started": started, "session": "conv-2"}}),))
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-970';")
    # A provider known only by its start, whatever conversation it holds: handed the ticket on both.
    seen["plain"] = as_listener(redeliver, (json.dumps({"main": plain}),))
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-970';")
    fixture.seed_postgres_ticket(admin, "PGU-971", title="Unconfirmed case", state="analysis", assignee="director",
                                 commit_exempt=True)
    fixture.post_json(base, "/api/tickets/PGU-970/actions/force_move", {"state": "done", "assignee": "unassigned"}, caller="director")
    fixture.post_json(base, "/api/tickets/PGU-971/actions/route", {"state": "in_progress", "assignee": "main"}, caller="director")
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-971';")
    sql("SELECT ticket_board.record_notification_trace('PGU-971', 9200, 'main', 'transition', 'send_unconfirmed');")
    later = sql("SELECT (clock_timestamp() + interval '1 second')::text;")
    time.sleep(1.2)
    seen["unconfirmed"] = as_listener(redeliver, (json.dumps({"main": later}),))
    fixture.post_json(base, "/api/tickets/PGU-971/actions/force_move", {"state": "done", "assignee": "unassigned"}, caller="director")
    if has_episodes:
        # A pane reported busy with nothing moving: escalated only past the grace, and only for a turn the
        # handoff started. The clock is given; nothing else is the owner's current work by now.
        fixture.seed_postgres_ticket(admin, "PGU-972", title="Busy case", state="analysis", assignee="director",
                                     commit_exempt=True)
        fixture.post_json(base, "/api/tickets/PGU-972/actions/route", {"state": "in_progress", "assignee": "main"},
                          caller="director")
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-972';")
        sql("SELECT ticket_board.record_notification_trace('PGU-972', 9300, 'main', 'transition', 'send');")
        handed = float(sql("SELECT extract(epoch FROM max(ts)) FROM ticket_board.notification_trace "
                           "WHERE ticket_id = 'PGU-972' AND event = 'send';"))

        def iso(epoch: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + f".{int(epoch % 1 * 1e6):06d}+00:00"

        def busy(turn: float, now: float) -> int:
            report = {"main": {"state": "busy", "updated_at": iso(turn), "turn_started_at": iso(turn), "busy_still": True,
                               "condition": None}}
            return as_listener("SELECT ticket_board.note_role_stall(%s::jsonb, 3, '600 seconds'::interval, %s::timestamptz)",
                               (json.dumps(report), iso(now)))

        seen["busy"] = [busy(handed + 1, handed + 60), busy(handed - 30, handed + 601), busy(handed + 1, handed + 601)]
        episode = int(sql("SELECT coalesce(max(id), 0) FROM ticket_board.role_stall_episodes WHERE ticket_id = 'PGU-972';"))
        escalation = dict(id="PGU-972", state="in_progress", assignee="main", kind="stall_escalation", target_role="director",
                          episode=episode, phase_no=1)
        current_before = deliverable("PGU-972", "director", escalation)
        # Its owner's own notice on its way: resolved for now, and the escalation is not typed.
        sql("SELECT ticket_board.enqueue_notification('PGU-972', 'transition', 'main', 'm', "
            "'{\"kind\": \"transition\", \"id\": \"PGU-972\", \"new_state\": \"in_progress\", \"assignee\": \"main\"}'::jsonb, "
            "'probe:PGU-972');")
        seen["escalation_resolved"] = [current_before, deliverable("PGU-972", "director", escalation)]
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-972';")
        fixture.post_json(base, "/api/tickets/PGU-972/actions/force_move", {"state": "done", "assignee": "unassigned"},
                          caller="director")
        # Audit's reproduction: back after downtime, one pass queues the owner's repair prompt and a Director nudge
        # before any is sent. The pass that runs before delivery opens the episode on the queued prompt.
        fixture.seed_postgres_ticket(admin, "PGU-973", title="Queued case", state="analysis", assignee="director",
                                     commit_exempt=True)
        fixture.post_json(base, "/api/tickets/PGU-973/actions/route", {"state": "in_progress", "assignee": "main"},
                          caller="director")
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-973';")
        sql("SELECT ticket_board.enqueue_notification('PGU-973', 'unresolved_turn_repair', 'main', 'm', "
            "'{\"kind\": \"unresolved_turn_repair\", \"id\": \"PGU-973\", \"state\": \"in_progress\", "
            "\"assignee\": \"main\"}'::jsonb, 'probe-repair:PGU-973');")
        now = time.time()
        report = {"main": {"state": "idle", "updated_at": iso(now - 1800), "turn_started_at": iso(now - 1810),
                           "busy_still": False, "condition": None}}
        queued_episode = as_listener("SELECT ticket_board.note_role_stall(%s::jsonb, 3, '600 seconds'::interval)",
                                     (json.dumps(report),))
        seen["queued_repair"] = [queued_episode, json.loads(sql(
            "SELECT coalesce(json_agg(phase), '[]')::text FROM ticket_board.role_stall_episodes "
            "WHERE ticket_id = 'PGU-973' AND closed_at IS NULL;")), deliverable("PGU-973", "director", dict(
                id="PGU-973", state="in_progress", assignee="main", kind="nudge", target_role="director"))]
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-973';")
        fixture.post_json(base, "/api/tickets/PGU-973/actions/force_move", {"state": "done", "assignee": "unassigned"},
                          caller="director")
        # A repair queued in an earlier round of the ticket: sent to Audit and back to its owner before it went, it
        # is never delivered (SYRD-527), so it holds nobody and opens nothing -- before or after it is discarded.
        nudge = dict(id="PGU-974", state="in_progress", assignee="main", kind="nudge", target_role="director")
        fixture.seed_postgres_ticket(admin, "PGU-974", title="Round-trip case", state="analysis", assignee="director",
                                     commit_exempt=True)
        fixture.post_json(base, "/api/tickets/PGU-974/actions/route", {"state": "in_progress", "assignee": "main"},
                          caller="director")
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-974';")
        sql("SELECT ticket_board.enqueue_notification('PGU-974', 'unresolved_turn_repair', 'main', 'm', "
            "'{\"kind\": \"unresolved_turn_repair\", \"id\": \"PGU-974\", \"state\": \"in_progress\", "
            "\"assignee\": \"main\"}'::jsonb, 'probe-repair:PGU-974');")
        fixture.post_json(base, "/api/tickets/PGU-974/actions/force_move", {"state": "audit", "assignee": "audit"},
                          caller="director")
        fixture.post_json(base, "/api/tickets/PGU-974/actions/force_move", {"state": "in_progress", "assignee": "main"},
                          caller="director")
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-974' AND kind <> 'unresolved_turn_repair';")

        def nudge_current() -> bool:
            return as_listener("SELECT ticket_board.role_stall_notice_is_current(%s, %s, %s::jsonb)",
                               ("PGU-974", "director", json.dumps(nudge)))

        def stall_now() -> list:
            note = as_listener("SELECT ticket_board.note_role_stall(%s::jsonb, 3, '600 seconds'::interval)",
                               (json.dumps(report),))
            return [note, json.loads(sql("SELECT coalesce(json_agg(json_build_array(phase, close_reason) ORDER BY id), "
                                         "'[]')::text FROM ticket_board.role_stall_episodes WHERE ticket_id = 'PGU-974';"))]

        stale = [nudge_current(), *stall_now()]
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-974';")  # discarded as stale
        seen["stale_round"] = [stale, [nudge_current(), *stall_now()]]
        # A repair queued in this round opens the owner's phase; dropped before it went, the episode closes as
        # withdrawn and holds nobody.
        sql("SELECT ticket_board.enqueue_notification('PGU-974', 'unresolved_turn_repair', 'main', 'm', "
            "'{\"kind\": \"unresolved_turn_repair\", \"id\": \"PGU-974\", \"state\": \"in_progress\", "
            "\"assignee\": \"main\"}'::jsonb, 'probe-repair-2:PGU-974');")
        opened = stall_now()
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-974';")
        seen["withdrawn"] = [opened, stall_now(), nudge_current()]
        fixture.post_json(base, "/api/tickets/PGU-974/actions/force_move", {"state": "done", "assignee": "unassigned"},
                          caller="director")
    # Who may run it, and a board built from schema.sql alone installs the same.
    functions = ("note_role_stall(jsonb, integer, interval, timestamp with time zone)",
                 "role_stall_notice_is_current(text, text, jsonb)", "close_role_stall(bigint, text)",
                 "role_generation_at(text, timestamp with time zone)", "redeliver_replaced_work(jsonb, timestamp with time zone)")
    try:
        seen["grantees"] = {function.split("(")[0]: sql(
            "SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, '+'), '') "
            f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{function}'::regprocedure "
            "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;") for function in functions}
        try:
            with psycopg.connect(listener_url.replace("ticket_board_listener", fixture.SERVICE_ROLE), autocommit=True) as conn:
                conn.execute("SELECT ticket_board.note_role_stall('{}'::jsonb, 3, '1 minute'::interval)")
            seen["service_refused"] = False
        except psycopg.Error as exc:
            seen["service_refused"] = "permission denied" in str(exc)
    except AssertionError as exc:  # BEFORE has none
        seen["grantees"] = seen["service_refused"] = f"unavailable: {str(exc)[-120:]}"
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def schema_parity(root: Path) -> dict:
    """A board built from schema.sql alone installs exactly what the migrations install."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board
    from temporary_cluster import temporary_cluster

    functions = ("note_role_stall(jsonb, integer, interval, timestamp with time zone)",
                 "role_stall_notice_is_current(text, text, jsonb)", "close_role_stall(bigint, text)",
                 "role_generation_at(text, timestamp with time zone)", "redeliver_replaced_work(jsonb, timestamp with time zone)")
    digest = (" || ".join(f"md5(pg_get_functiondef('ticket_board.{f}'::regprocedure))" for f in functions)
              + " || (SELECT md5(string_agg(attname || ' ' || format_type(atttypid, atttypmod), ', ' ORDER BY attnum)) "
                "FROM pg_attribute WHERE attrelid = 'ticket_board.role_stall_episodes'::regclass AND attnum > 0 AND NOT attisdropped)")
    with temporary_cluster(prefix="s595p", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        return {"fresh_equals_migrated": fixture.psql(fresh, f"SELECT {digest};").strip() == fixture.psql(admin, f"SELECT {digest};").strip()}


def session_cases() -> dict:
    """The conversation a notice is typed into, and the one a replaced provider holds, as the listener reads them."""
    import logging
    from types import SimpleNamespace

    from scripts.ticket_board import notification_dispatch, replaced_delivery

    class Gate:
        def __init__(self, start: dict | None) -> None:
            self.role_targets, self.start = {"main": WORKER}, start
            self.state_store = SimpleNamespace(read=lambda _target: SimpleNamespace(last_session_start=self.start))

        def is_working(self, _target: str) -> bool:
            return False

        def turnless_since(self, _target: str) -> float:
            return 1000.0

    def detail(gate: Gate) -> dict:
        dispatch = notification_dispatch.NotificationDispatch(
            logger=logging.getLogger("syrd595"), ledger=None, sender=lambda: None, activity_gate=lambda: gate.is_working,
            submission_witness=lambda: None, wall_clock=time.time, monotonic=time.monotonic, sleeper=lambda _s: None,
            submission_confirm_seconds=lambda: 0.0, submission_poll_seconds=lambda: 0.0)
        snapshot = notification_dispatch.ComposerSnapshot(False)
        return dispatch._delivery_diagnostic_detail(
            target=WORKER, message="m", attempts=1, activity_trace=SimpleNamespace(busy=False, reason="hook_idle", region_digest=None),
            before=snapshot, decision="send", reason="hook_idle")

    return {
        "sent_into": [detail(Gate({"session_id": "conv-9", "at": 1001.0})).get("provider_session"),
                      detail(Gate(None)).get("provider_session", "absent")],
        "holds": [replaced_delivery.turnless_roles(Gate({"session_id": "conv-9", "at": 1001.0})),
                  replaced_delivery.turnless_roles(Gate({"session_id": "conv-8", "at": 999.0}))],
    }


def pane_cases() -> dict:
    """What a pass reports about the worker's pane, read by the real gate from captured screens."""
    import codex_capacity_retry_test as c594

    from scripts.ticket_board import notify_listener as nl, stall_episode

    shown = c594.screens()
    out: dict = {}
    with tempfile.TemporaryDirectory(prefix="syrd595-pane.") as state_dir:
        store = nl.PaneHookStateStore(Path(state_dir))
        pane = c594.Pane(shown["quota"], 600, time.time() - 3600)
        tmux, table = c594.fake_tmux({WORKER: pane})
        gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                   pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None)
        gate.role_targets = {"main": WORKER}
        store.write(WORKER, "busy", source="codex.UserPromptSubmit", now=time.time() - 700)
        state = store.read(WORKER)
        out["still"] = stall_episode.busy_still(gate, WORKER, state, 600)
        out["within grace"] = stall_episode.busy_still(gate, WORKER, state, 800)
        pane.moving = True
        out["moving"] = stall_episode.busy_still(gate, WORKER, state, 600)
        pane.moving = False
        out["report busy"] = stall_episode.report(gate, 600, set())["main"]
        store.write(WORKER, "idle", source="codex.Stop")
        out["idle"] = stall_episode.busy_still(gate, WORKER, store.read(WORKER), 600)
        out["report idle"] = stall_episode.report(gate, 600, set())["main"]["condition"]
        out["report watched"] = stall_episode.report(gate, 600, {"main"})["main"]["condition"]
    return out


def ledger_cases() -> list:
    """What the listener's ledger writes for a notice that says something outside ASCII: the text, not escapes."""
    import logging

    from scripts.ticket_board.notification_ledger import NotificationLedger

    calls: list = []
    conn = type("Conn", (), {"execute": lambda self, query, params=None: calls.append(params)})()
    ledger = NotificationLedger(logger=logging.getLogger("syrd595"), requeue_base_seconds=1, requeue_max_seconds=1)
    said = "Its pane shows: \u25a0 Quota exceeded."
    ledger.trace(conn, notification_id=1, ticket_id="PGU-1", target_role="director", kind="unresolved_turn", event="send",
                 detail={"message": said})
    ledger.dead_letter(conn, 1, "gone", target=DIRECTOR, message=said, attempts=1, payload="{}")
    return [said in params[-1] and "\\u25a0" not in params[-1] for params in calls]


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    os.environ["TICKET_BOARD_STALL_MAX_ATTEMPTS"] = str(ATTEMPTS)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    cached = Path(os.environ["SYRD595_BEFORE_JSON"]) if os.environ.get("SYRD595_BEFORE_JSON") else None
    if cached is not None and cached.is_file():
        before = json.loads(cached.read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="syrd595.") as tmp:
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario",
                                    str(tree_at(BEFORE, Path(tmp) / "before"))],
                                   capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1200)
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        if cached is not None:
            cached.write_text(json.dumps(before))
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    after["parity"] = schema_parity(ROOT)
    after["sessions"] = session_cases()
    after["panes"] = pane_cases()
    after["ledger"] = ledger_cases()
    print("OBSERVED " + json.dumps({"before": before, "after": after}, ensure_ascii=False), file=sys.stderr)

    def role(told: list) -> list:
        return [row for row in told if row[1] == "main"]

    def director(told: list) -> list:
        return [row for row in told if row[1] == "director"]

    def repairs(told: list) -> list:
        return [row for row in role(told) if "still yours" in row[3]]

    # 1. Main: a provider failing every turn is prompted without end, and the Director is told in between.
    b, a = before["switch"], after["switch"]
    check(len(repairs(b["before_switch"])) > ATTEMPTS and director(b["before_switch"])
          and any(d[0] < repairs(b["before_switch"])[-1][0] for d in director(b["before_switch"])),
          f"main prompts the failing owner past any bound and tells the Director in between: {b['before_switch']}")
    # Here: exactly ATTEMPTS prompts, then one escalation, then nothing to anyone.
    pre = a["before_switch"]
    check(a["reached_first"] and len(repairs(pre)) == ATTEMPTS and len(director(pre)) == 1,
          f"runtime A: {ATTEMPTS} prompts, then the Director once: {pre}")
    escalated_at = director(pre)[0][0]
    check(all(row[0] < escalated_at for row in role(pre)) and escalated_at >= repairs(pre)[-1][0],
          f"the owner was the only one told until the escalation, and nothing went to it after: {pre}")
    check("PRIORITY PGU-960" in director(pre)[0][3] and "prompted 3 times" in director(pre)[0][3]
          and 'Its pane shows: "■ Quota exceeded. Check your plan and billing details."' in director(pre)[0][3],
          f"the escalation names the ticket, the attempts and what the pane shows: {director(pre)}")
    # The switch: the retired runtime's delivery is not read as the live one's, and B is told once.
    check(b["delivery_after_switch"].get("state") == "delivered"
          and a["delivery_after_switch"] == dict(a["delivery_after_switch"], state="unconfirmed", reason="sent_to_retired_runtime"),
          f"after the switch, main still reads the ticket as delivered; here it reads sent to the retired runtime: "
          f"{b['delivery_after_switch']} / {a['delivery_after_switch']}")
    post = [row for row in a["told"] if row[0] >= a["switched_at"]]
    redelivered = [row for row in role(post) if "active again" in row[3]]
    check(len(redelivered) == 1 and all(row[2] == a["generations"][1] for row in role(post)),
          f"runtime B is handed the ticket exactly once, and nothing after the switch reaches runtime A: {post}")
    check(len(repairs(post)) == ATTEMPTS and len(director(post)) == 1
          and all(row[0] < director(post)[0][0] for row in role(post)),
          f"runtime B gets its own {ATTEMPTS} prompts, then the Director once more, never both at once: {post}")
    check(f"generation {a['generations'][1]}, which replaced generation 1" in director(post)[0][3],
          f"the second escalation names the current runtime generation and the one it replaced: {director(post)}")
    check(a["delivery_end"].get("state") == "delivered" and a["restarts"] >= 5,
          f"B's delivery reads delivered; the listener restarted throughout: {a['delivery_end']}, {a['restarts']}")
    check(a["episodes"] and [p[1] for p in a["episodes"][0]["phases"]] == [None, "attempts_exhausted", "runtime_replaced",
                                                                           "attempts_exhausted"],
          f"one episode, its phases recorded: owner, Director, owner again on the new runtime, Director: {a['episodes']}")

    # 2. A hook left busy: main tells nobody; here the Director once, after the grace.
    check(director(before["stuck"]["told"]) == [] and len(role(before["stuck"]["told"])) == 1,
          f"main: the stuck provider is handed the work and nobody hears anything again: {before['stuck']}")
    stuck = after["stuck"]["told"]
    check(len(role(stuck)) == 1 and len(director(stuck)) == 1 and "busy since" in director(stuck)[0][3]
          and director(stuck)[0][0] >= UNRESOLVED,
          f"here: the Director is told once, after the grace, that the pane has said busy since: {stuck}")
    check([e["closed"] for e in after["stuck"]["after_done"]] == ["moved"],
          f"its episode closes when the ticket moves on: {after['stuck']['after_done']}")

    # 3. Handed back by a Director comment: a new phase for the owner, then one more escalation.
    rearm = after["rearm"]["told"]
    phase_two = [row for row in rearm if row[0] >= after["rearm"]["commented_at"]]
    check(after["rearm"]["first"] and len(director(rearm)) == 2 and len(repairs(phase_two)) == ATTEMPTS
          and all(row[0] < director(phase_two)[0][0] for row in role(phase_two))
          and [p[1] for p in after["rearm"]["episodes"][0]["phases"]] == [None, "attempts_exhausted", "director_comment",
                                                                          "attempts_exhausted"],
          f"the Director's comment hands it back for {ATTEMPTS} more prompts, then one escalation: {after['rearm']}")

    # 4. Back after downtime with every reminder due at once: main tells the owner and the Director on the same
    #    pass; here only the owner, until the one escalation.
    db, da = before["downtime"], after["downtime"]
    first_b = [row for row in db["told"] if row[0] >= db["back"]]
    first_a = [row for row in da["told"] if row[0] >= da["back"]]
    check(repairs(first_b) and any(d[0] - db["back"] < UNRESOLVED and d[0] < repairs(first_b)[-1][0] for d in director(first_b)),
          f"main: back after downtime, the Director is told while the owner is still being prompted: {first_b}")
    check(set(da["first_pass"]) == {"main"}, f"the first pass back tells only the owner: {da['first_pass']}")
    check(len(repairs(first_a)) == ATTEMPTS and len(director(first_a)) == 1 and "PRIORITY PGU-964" in director(first_a)[0][3]
          and all(row[0] < director(first_a)[0][0] for row in role(first_a)),
          f"here: back after downtime, only the owner is told until the one escalation: {first_a}")
    check(after["ledger"] == [True, True],
          f"the listener's trace and dead letter carry text outside ASCII as itself, which a SQL_ASCII board takes: "
          f"{after['ledger']}")
    check(len(after["escalation_traces"]) >= 4
          and all("listener_claim" in events and ({"send", "send_unconfirmed"} & set(events)) for _n, events in after["escalation_traces"]),
          f"every escalation, whatever its text, has its claim and send recorded: {after['escalation_traces']}")

    # 5. The owner writes on it: the episode closes as progress, and the Director hears nothing.
    check(director(after["progress"]["told"]) == [] and after["progress"]["episodes"][0]["closed"] == "progress",
          f"the owner's own comment closes it as progress: {after['progress']}")

    f, fb = after["functions"], before["functions"]
    check(fb["retired_repair"] == [True, True] and f["retired_repair"] == [False, True],
          f"a repair prompt about the retired runtime's turn is not typed into the new one: {fb['retired_repair']} / "
          f"{f['retired_repair']}")
    check(f["role_phase"] == {"director unresolved_turn": False, "director nudge": False, "director escalation": False,
                              "director permission prompt": True, "owner idle_reminder": False, "owner repair": True,
                              "owner capacity_retry": True, "owner transition": True},
          f"while the owner is being prompted, none of the Director's stall notices go: {f['role_phase']}")
    check(f["director_phase"] == {"director unresolved_turn": False, "director nudge": False, "director escalation": False,
                                  "director permission prompt": True, "owner idle_reminder": False, "owner repair": False,
                                  "owner capacity_retry": False, "owner transition": True},
          f"once escalated, no automatic prompt to the owner and no second Director stall notice: {f['director_phase']}")
    check(f["pending_repair"] == dict(f["no_episode"], **{"director nudge": False, "director escalation": False}),
          f"no episode yet, the owner's prompt on its way: the Director's nudge and escalation do not go; the handoff "
          f"about a prompt still undelivered after the grace still does (SYRD-584): {f['pending_repair']}")
    check(f["escalation_current"] == [True, False, False, False] and all(f["no_episode"].values()) and f["fourth_repair"] is False,
          f"the escalation is current in its own phase only; no episode, no opinion; a fourth prompt is not typed: {f}")
    check(fb["plain"] == 1 and f["plain"] == 1 and f["resumed"] == 0 and f["fresh"] == 1,
          f"main hands the ticket to a replaced provider whatever conversation it resumed; here one that resumed "
          f"the very conversation it was typed into is not handed it again, and a fresh one is: {fb['plain']} / "
          f"{f['plain']}, {f['resumed']}, {f['fresh']}")
    check(f["queued_repair"] == [0, ["role"], False],
          f"a repair prompt on its way opens the episode in the owner's phase before delivery, so the Director's "
          f"nudge queued with it is not typed: {f['queued_repair']}")
    check(f["stale_round"] == [[True, 0, []], [True, 0, []]],
          f"a repair left queued from an earlier round holds no Director notice and opens no episode, before or after "
          f"it is discarded: {f['stale_round']}")
    check(f["withdrawn"] == [[0, [["role", None]]], [0, [["role", "withdrawn"]]], True],
          f"a repair of this round opens the owner's phase; dropped before it went, the episode closes as withdrawn "
          f"and holds nobody: {f['withdrawn']}")
    check(f["busy"] == [0, 0, 1] and f["escalation_resolved"] == [True, False],
          f"a busy pane is escalated only past the grace and for a turn the handoff started; an escalation is not "
          f"typed while its ticket is resolved for now: {f['busy']}, {f['escalation_resolved']}")
    pane = after["panes"]
    check(pane["still"] is True and pane["within grace"] is False and pane["moving"] is False and pane["idle"] is False
          and pane["report busy"]["busy_still"] is True
          and pane["report busy"]["condition"] == "■ Quota exceeded. Check your plan and billing details."
          and pane["report idle"] is None and pane["report watched"] == pane["report busy"]["condition"],
          f"busy with nothing moving only past the grace, never while the screen moves or once idle; the pane's last "
          f"line read only when it matters: {pane}")
    started = "1970-01-01T00:16:40+00:00"
    check(after["sessions"] == {"sent_into": ["conv-9", "absent"],
                                "holds": [{"main": {"started": started, "session": "conv-9"}}, {"main": started}]},
          f"a send records the conversation its pane's hook last announced; a replaced provider is reported with the "
          f"conversation it announced since it started, and an older one carried forward is not its own: {after['sessions']}")
    check(fb["unconfirmed"] == 0 and f["unconfirmed"] == 1,
          f"a notice never seen to arrive at the old provider is redelivered to the new one: {fb['unconfirmed']} / {f['unconfirmed']}")
    check(f["grantees"] == {"note_role_stall": "ticket_board_listener", "role_stall_notice_is_current": "ticket_board_listener",
                            "close_role_stall": "", "role_generation_at": "", "redeliver_replaced_work": "ticket_board_listener"}
          and f["service_refused"] is True and after["parity"]["fresh_equals_migrated"] is True,
          f"only the listener may report or ask; schema.sql installs the same: {f['grantees']}, {f['service_refused']}, "
          f"{after['parity']}")
    print(f"runtime_switch_stall_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
