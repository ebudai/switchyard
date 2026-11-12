#!/usr/bin/env python3
"""SYRD-756: the Director's pause switch, on a real board, through the real listener's delivery path.

A disposable PostgreSQL board built as production builds one (schema.sql, rbac.sql, every migration), its real board
server, and the real `TicketBoardNotifyListener` with the real `PaneActivityGate` over hook-state files. Only tmux
and the panes are stand-ins, and they answer as the measured CLIs do: each runtime's busy and interrupted screens
come from the offline probes (tests/fixtures/syrd756_interrupt_screens.json, made by tests/fixtures/syrd756_probe).

Cases:
  authority  -- the Director and the User set, change and end it, recorded with a required reason; no other role,
                at the server or in the database; the board page path; the reads; the CLI help.
  drain      -- a drained notice is held, not dropped, and delivered once at the resume; a notice made moot meanwhile
                collapses; the Director is still told things; a restarted listener delivers nothing while paused; a
                pause set between a notice's claim and its send holds it; a resume racing a transition delivers once.
  stop       -- each busy role's turn is interrupted once with its runtime's own interrupt (Claude, Codex and agy
                Esc, Copilot Esc Esc, Hermes its pause notice typed into the running turn); the Director's pane and a
                turn a person began after the stop are left alone; the interrupted turn is seen to end from its
                screen, then the role is told once; background commands are reported to the Director with pid and
                ticket and nothing is killed; the resume tells each such role once.
  clocks     -- paused time does not count: unpaused_since, the idle nudge, the board stall digest and a lease's
                observation deadline fire for unpaused time only, so resuming fires no burst.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
FIXTURE = json.loads((ROOT / "tests/fixtures/syrd756_interrupt_screens.json").read_text())
TARGETS = {"director": "pgu-director:0.0", "main": "pgu-main:0.0", "app": "pgu-app:0.0", "ops": "pgu-ops:0.0",
           "audit": "pgu-audit:0.0", "inspector": "pgu-inspector:0.0", "designer": "pgu-designer:0.0"}
RUNTIMES = {"director": "claude", "main": "claude", "app": "codex", "ops": "copilot", "audit": "hermes",
            "inspector": "agy", "designer": "claude"}


def check(condition: object, detail: object) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


class Pane:
    """A role's pane as its CLI draws it: a measured screen, and what each key does to it."""

    def __init__(self, role: str, pid: int) -> None:
        self.role, self.runtime, self.pid = role, RUNTIMES[role], pid
        self.screen = FIXTURE["screens"][self.runtime]["idle"]
        self.keys: list[str] = []
        self.typed: list[str] = []
        self.escapes = 0

    def busy(self, returned: bool = False) -> None:
        self.screen = FIXTURE["screens"][self.runtime]["busy"]
        self.escapes = 0
        self.returned = returned

    def press(self, keys: list[str], literal: bool) -> None:
        if literal:
            self.typed.append(" ".join(keys))
            return
        self.keys.extend(keys)
        if getattr(self, "deaf", False):  # an interrupt that does not take: the screen does not change
            return
        for key in keys:
            if key == "Escape" and self.screen is FIXTURE["screens"][self.runtime]["busy"]:
                self.escapes += 1
                needed = 2 if self.runtime == "copilot" else 1  # measured: one Esc does nothing in Copilot
                if self.runtime in ("claude", "codex", "copilot", "agy") and self.escapes >= needed:
                    name = "returned" if getattr(self, "returned", False) else "interrupted"
                    self.screen = FIXTURE["screens"][self.runtime][name]
            if key == "Enter" and self.runtime == "hermes" and self.typed:
                self.screen = FIXTURE["screens"]["hermes"]["after_message"]


def fake_tmux(panes: dict[str, Pane]):
    by_target = {TARGETS[role]: pane for role, pane in panes.items()}

    def tmux(argv, **_kwargs):
        argv = list(argv)
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        pane = by_target.get(target)
        if pane is None:
            raise subprocess.CalledProcessError(1, argv, "", f"can't find pane: {target}")
        if argv[:2] == ["tmux", "capture-pane"]:
            return subprocess.CompletedProcess(argv, 0, "".join(row + "\n" for row in pane.screen["rows"]), "")
        if argv[:2] == ["tmux", "display-message"]:
            fmt = argv[-1]
            if "cursor_x" in fmt:
                x, y = pane.screen["cursor"]
                return subprocess.CompletedProcess(argv, 0, f"{x} {y} {len(pane.screen['rows'])}\n", "")
            if fmt == "#{pane_pid}":
                return subprocess.CompletedProcess(argv, 0, f"{pane.pid}\n", "")
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        if argv[:2] == ["tmux", "send-keys"]:
            rest = argv[argv.index("-t") + 2:]
            literal = rest[:1] == ["-l"]
            pane.press(rest[1:] if literal else rest, literal)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected tmux call {argv}")

    def table():
        rows = []
        for pane in panes.values():
            rows.append((pane.pid, 1, 50, pane.pid, time.time() - 7200))
            for child in getattr(pane, "background", []):
                rows.append(child)
        return rows

    return tmux, table


def scenario(root: Path) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board import work_pause
    from temporary_cluster import temporary_cluster

    assert Path(nl.__file__).resolve().is_relative_to(root.resolve()), nl.__file__
    seen: dict = {}
    with temporary_cluster(prefix="s756", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd756-state.") as state_dir:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        document = workflow(root)
        for role in document["roles"]:
            if role["name"] in RUNTIMES:
                role["runtime"] = RUNTIMES[role["name"]]
        app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        service_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)

        def as_role(role: str, statement: str) -> str:
            """As the board service runs an operation for `role`: its caller set in the transaction."""
            import psycopg

            try:
                with psycopg.connect(service_url) as conn:
                    conn.execute("SELECT set_config('ticket_board.caller_role', %s, true)", (role,))
                    value = conn.execute(statement).fetchone()[0]
                return json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            except Exception as exc:  # the database's refusal, as words
                return f"ERROR {exc}"

        def pause(mode: str, reason: str = "test", role: str = "director") -> str:
            return as_role(role, f"SELECT ticket_board.pause_work('{mode}', '{reason}');")

        def resume(reason: str = "resume", role: str = "director") -> str:
            return as_role(role, f"SELECT ticket_board.resume_work('{reason}');")

        store = nl.PaneHookStateStore(Path(state_dir))
        panes = {role: Pane(role, 5000 + 10 * i) for i, role in enumerate(TARGETS)}
        tmux, table = fake_tmux(panes)
        for role, target in TARGETS.items():
            store.write(target, "idle", source=f"{RUNTIMES[role]}.Stop", now=time.time() - 3600)
        sends: list[tuple[str, str]] = []

        def sender(target: str, message: str) -> dict:
            """Typed and submitted; the pane's CLI takes it and runs a turn that ends at once."""
            sends.append((target, message))
            store.write(target, "busy", source="claude.UserPromptSubmit")
            store.write(target, "idle", source="claude.Stop")
            return {}

        def new_gate():
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, role_runtimes=dict(RUNTIMES))
            gate.proc_root = Path(state_dir) / "proc"
            return gate

        process = {"gate": new_gate()}

        def run_pass(fresh: bool = False) -> None:
            if fresh:  # a restarted listener: nothing remembered but what the board and the hook files hold
                process["gate"] = new_gate()
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=process["gate"].is_working,
                target_exists=lambda target: target in TARGETS.values(), poll_seconds=0,
                submission_confirm_seconds=0.3, submission_poll_seconds=0.05)
            listener.delivery_submitter = lambda _target: None
            listener._wait_for_notification = lambda _conn: False
            try:
                listener.listen_once(max_notifications=listener.delivered_count + 20)
            except Exception as exc:  # a pass that raises is a failure of its own, not a crash of this test
                check(False, f"a listener pass raised: {exc!r}")

        def sent_to(role: str) -> list[str]:
            return [message for target, message in sends if target == TARGETS[role]]

        def queued(role: str) -> list[dict]:
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_object('ticket', ticket_id, 'kind', payload->>'kind', "
                "'error', last_error) ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                f"WHERE target_role = '{role}' AND dead_lettered_at IS NULL;"))

        def ticket(ticket_id: str, state: str, assignee: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket_id, title=ticket_id, state=state, assignee=assignee,
                                         commit_exempt=True)

        # ---------------------------------------------------------------- authority
        refused = pause("drain", "not mine", role="main")
        check("only the Director or the User may pause or resume work" in refused,
              f"main may not pause work, and is told who may: {refused}")
        blank = as_role("director", "SELECT ticket_board.pause_work('drain', '  ');")
        check("pausing work needs a reason" in blank, f"a pause needs a reason: {blank}")
        check("drain or stop" in pause("halt"), "a pause is drain or stop")
        page = fixture.post_json(base, "/api/tickets/actions/pause_work", {"mode": "drain", "reason": "from the page"},
                                 caller="user")
        check(page["pause"]["began"] and page["pause"]["by"] == "user", f"the User pauses from the board page: {page}")
        server_refusal = fixture.post_json(base, "/api/tickets/actions/pause_work", {"mode": "stop", "reason": "x"},
                                           caller="audit", expect=403)
        check("cannot call pause_work" in server_refusal, f"the server refuses another role first: {server_refusal}")
        held_roles = sql("SELECT array[ticket_board.role_paused('main') IS NOT NULL, ticket_board.role_paused('director') "
                         "IS NULL, ticket_board.role_paused('user') IS NULL, ticket_board.role_paused('perf') IS NULL, "
                         "ticket_board.role_provider_hold_is_current('main', now()), NOT "
                         "ticket_board.role_provider_hold_is_current('director', now())]::text;")
        check(held_roles == "{t,t,t,t,t,t}", f"only role panes are paused -- not the Director, the User, or a role with "
              f"no runtime -- and the hold every reminder asks says so: {held_roles}")
        import logging
        import psycopg
        from types import SimpleNamespace

        from scripts.ticket_board import provider_refusal, review_balance

        probe_listener = SimpleNamespace(logger=logging.getLogger("syrd756"))
        with psycopg.connect(listener_url, autocommit=True) as conn:
            typed = (work_pause.typing_held(probe_listener, conn, "main"), work_pause.typing_held(probe_listener, conn, "director"),
                     provider_refusal.typing_held(probe_listener, conn, "main"),
                     provider_refusal.typing_held(probe_listener, conn, "main", pause=False))
            asked: list[str] = []
            real_eligible = review_balance.why_not_eligible
            review_balance.why_not_eligible = lambda _gate, role: asked.append(role) or "busy"
            try:
                balance_listener = SimpleNamespace(
                    logger=probe_listener.logger, activity_gate=process["gate"].is_working, _review_balance_present=True,
                    workflow={"stages": [{"name": "audit", "kind": "review", "owners": ["audit", "inspector"]}]})
                review_balance.run(balance_listener, conn)
            finally:
                review_balance.why_not_eligible = real_eligible
        check(typed == (True, False, True, False), f"nothing is typed into a paused pane but the Director's: {typed}")
        check(asked == [], f"no review moves while paused: {asked}")
        same = json.loads(pause("drain", "again"))
        check(not same["changed"] and same["by"] == "user", f"the standing mode again changes nothing: {same}")
        status = json.loads(fixture.get_json(base, "/api/work-pause") if hasattr(fixture, "get_json") else
                            __import__("urllib.request").request.urlopen(base + "/api/work-pause", timeout=5).read())
        check(status["paused"] and status["pause"]["mode"] == "drain" and status["pause"]["reason"] == "from the page"
              and status["pause"]["began_by"] == "user", f"GET /api/work-pause says what stands: {status}")
        text = work_pause.describe(status)
        check("WORK PAUSED -- DRAIN" in text and "set by user" in text and "from the page" in text
              and "resume-work" in text, f"ticket-board-read pause says mode, who, why and how to end it:\n{text}")
        def read_cli(*args: str) -> str:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_"))}
            return subprocess.run([sys.executable, str(root / "scripts/ticket-board-read"), "--board-url", base, *args],
                                  capture_output=True, text=True, timeout=30, env=env).stdout

        shown, queue = read_cli("pause"), read_cli("director")
        check(shown.startswith("WORK PAUSED -- DRAIN") and "from the page" in shown,
              f"ticket-board-read pause shows it: {shown[:200]}")
        check(queue.startswith("WORK PAUSED -- DRAIN"), f"and it heads ticket-board-read director: {queue[:200]}")
        changed = json.loads(pause("stop", "now it is urgent"))
        check(changed["changed"] and changed["mode"] == "stop", f"the Director changes the mode, no resume between: {changed}")
        changes = json.loads(sql("SELECT changes::text FROM ticket_board.work_pauses WHERE ended_at IS NULL;"))
        check(len(changes) == 1 and changes[0].get("from") == "drain" and changes[0].get("to") == "stop"
              and changes[0].get("by") == "director" and changes[0].get("reason") == "now it is urgent",
              f"the change is recorded with who and why: {changes}")
        check("only the Director or the User" in resume("x", role="ops"), "ops may not resume")
        ended = json.loads(resume("authority case over", role="user"))
        check(ended["resumed"] and ended["by"] == "user", f"the User resumes: {ended}")
        check("work is not paused" in resume("again"), "a resume needs a pause")
        last = json.loads(sql("SELECT to_jsonb(p)::text FROM ticket_board.work_pauses p ORDER BY id DESC LIMIT 1;"))
        check(last["ended_by"] == "user" and last["end_reason"] == "authority case over",
              f"the resume is recorded with who and why: {last}")
        help_text = subprocess.run([sys.executable, str(root / "scripts/ticket-board-write"), "pause-work", "--help"],
                                   capture_output=True, text=True, timeout=30).stdout
        check("--mode {drain,stop}" in help_text and "resume-work" in help_text,
              f"the write CLI documents both switches and the resume: {help_text[:300]}")
        seen["authority"] = "ok"

        # ---------------------------------------------------------------- drain on the delivery path
        ticket("PGU-1", "analysis", "director")
        run_pass()
        check(any("PGU-1" in m for m in sent_to("director")), "before any pause the Director is told about new work")
        sends.clear()
        pause("drain", "drain case")
        ticket("PGU-2", "in_progress", "main")   # a hand-over to main: its transition notice
        ticket("PGU-3", "in_progress", "app")    # and to app, which will be moved away meanwhile (moot)
        ticket("PGU-4", "analysis", "director")  # and the Director's own news
        run_pass()
        check(sent_to("main") == [] and sent_to("app") == [], f"drained: nothing is typed into a role pane: {sends}")
        check(any("PGU-4" in m for m in sent_to("director")), f"the Director's pane is exempt: {sent_to('director')}")
        panes["designer"].busy()
        store.write(TARGETS["designer"], "busy", source="claude.UserPromptSubmit", now=time.time() - 60)
        run_pass()
        check(panes["designer"].keys == [] and panes["designer"].typed == [],
              "drain interrupts nothing: a busy pane finishes its turn")
        panes["designer"].screen = FIXTURE["screens"]["claude"]["idle"]
        store.write(TARGETS["designer"], "idle", source="claude.Stop")
        held = queued("main")
        check(len(held) == 1 and held[0]["ticket"] == "PGU-2" and (held[0]["error"] or "").startswith("work_paused"),
              f"held, not dropped, and said to be the pause's: {held}")
        run_pass(fresh=True)
        check(sent_to("main") == [] and len(queued("main")) == 1,
              f"a restarted listener reads the pause from the board and delivers nothing: {sends}")
        fixture.post_json(base, "/api/tickets/PGU-3/actions/defer", {"reason": "made moot while paused"}, caller="director")
        resumed = json.loads(resume("drain over"))
        check(resumed["released_notices"] >= 1, f"the resume makes what waited due at once: {resumed}")
        run_pass()
        run_pass()
        check(len([m for m in sent_to("main") if m.startswith("PGU-2 --")]) == 1,
              f"the held notice goes once at the resume, and not again: {sent_to('main')}")
        check(not [m for m in sent_to("app") if m.startswith("PGU-3 --")],
              f"a notice made moot while paused collapses: {sent_to('app')}")
        sends.clear()

        # A pause set between a notice's claim and its send holds it at the pre-send check.
        real_hold = work_pause.hold_delivery
        armed = {"on": True}

        def pause_mid_delivery(listener, conn, notification_id, attempts, kind, target_role, ticket_id, payload, phase):
            answer = real_hold(listener, conn, notification_id, attempts, kind, target_role, ticket_id, payload, phase)
            if phase == "claim" and target_role == "ops" and armed["on"] and not answer:
                armed["on"] = False
                pause("drain", "set mid-delivery")
            return answer

        work_pause.hold_delivery = pause_mid_delivery
        try:
            ticket("PGU-5", "in_progress", "ops")
            run_pass()
        finally:
            work_pause.hold_delivery = real_hold
        check(not armed["on"] and sent_to("ops") == [] and len(queued("ops")) == 1,
              f"claimed before the pause, held at the pre-send check after it: {sends} {queued('ops')}")
        resume("mid-delivery over")
        run_pass()
        check(len([m for m in sent_to("ops") if m.startswith("PGU-5 --")]) == 1, f"and delivered once after: {sent_to('ops')}")
        fixture.post_json(base, "/api/tickets/PGU-5/actions/defer", {"reason": "clear ops"}, caller="director")
        sends.clear()

        # A resume racing a transition: whichever lands first, the notice goes exactly once. main's earlier ticket goes
        # first, so serial focus queues nothing behind it.
        fixture.post_json(base, "/api/tickets/PGU-2/actions/defer", {"reason": "clear main"}, caller="director")
        for n, order in ((6, "transition first"), (7, "resume first"), (8, "together")):
            pause("drain", f"race {order}")
            if order == "transition first":
                ticket(f"PGU-{n}", "in_progress", "main")
                resume(order)
            elif order == "resume first":
                resume(order)
                ticket(f"PGU-{n}", "in_progress", "main")
            else:
                threads = [threading.Thread(target=ticket, args=(f"PGU-{n}", "in_progress", "main")),
                           threading.Thread(target=resume, args=(order,))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            run_pass()
            run_pass()
            check(len([m for m in sent_to("main") if m.startswith(f"PGU-{n} --")]) == 1,
                  f"a resume racing a transition ({order}) delivers it once: {sent_to('main')}")
            fixture.post_json(base, f"/api/tickets/PGU-{n}/actions/defer", {"reason": "next race"}, caller="director")
        sends.clear()
        # A pause that cannot be read holds every role's notice and types nothing -- but never the Director's.
        real_paused = work_pause.role_paused

        def unreadable(_conn, _role):
            raise RuntimeError("the board cannot say")

        work_pause.role_paused = unreadable
        try:
            ticket("PGU-9", "in_progress", "main")
            ticket("PGU-10", "analysis", "director")
            run_pass()
            with psycopg.connect(listener_url, autocommit=True) as conn:
                typing = (work_pause.typing_held(probe_listener, conn, "main"),
                          work_pause.typing_held(probe_listener, conn, "director"))
        finally:
            work_pause.role_paused = real_paused
        held = queued("main")
        check(not [m for m in sent_to("main") if m.startswith("PGU-9 --")] and len(held) == 1
              and "could not be checked" in (held[0]["error"] or ""),
              f"an unreadable pause holds main's notice, saying why: {held} {sent_to('main')}")
        check(any("PGU-10" in m for m in sent_to("director")), f"and still delivers the Director's: {sent_to('director')}")
        check(typing == (True, False), f"and types nothing into main's pane, but the Director's: {typing}")
        sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = now() WHERE target_role = 'main';")
        run_pass()
        check(len([m for m in sent_to("main") if m.startswith("PGU-9 --")]) == 1, "readable again: delivered once")
        fixture.post_json(base, "/api/tickets/PGU-9/actions/defer", {"reason": "clear main"}, caller="director")
        sends.clear()
        seen["drain"] = "ok"

        # ---------------------------------------------------------------- stop now
        stop_tickets = {"main": "PGU-20", "app": "PGU-21", "ops": "PGU-22", "audit": "PGU-23", "inspector": "PGU-24"}
        for role, ticket_id in stop_tickets.items():
            stage = {"audit": "audit", "inspector": "inspection"}.get(role, "in_progress")
            ticket(ticket_id, stage, role)
        run_pass()
        sends.clear()
        before_stop = time.time() - 30
        for role in stop_tickets:
            panes[role].busy()
            store.write(TARGETS[role], "busy", source=f"{RUNTIMES[role]}.UserPromptSubmit", now=before_stop)
        panes["director"].busy()
        store.write(TARGETS["director"], "busy", source="claude.UserPromptSubmit", now=before_stop)
        panes["main"].background = [(7001, panes["main"].pid, 50, 7001, time.time() - 600)]
        stopped = json.loads(pause("stop", "stop everything"))
        run_pass()
        check(panes["main"].keys == ["Escape"] and panes["app"].keys == ["Escape"]
              and panes["inspector"].keys == ["Escape"], f"Claude, Codex and agy: Esc, once each: "
              f"{ {r: panes[r].keys for r in panes} }")
        check(panes["ops"].keys == ["Escape", "Escape"], f"Copilot: Esc Esc: {panes['ops'].keys}")
        check(panes["audit"].keys == ["Enter"] and len(panes["audit"].typed) == 1
              and panes["audit"].typed[0].startswith("WORK PAUSED"),
              f"Hermes: its own interrupt, the pause notice typed into the running turn: {panes['audit'].typed}")
        check(panes["director"].keys == [], "the Director's pane is left alone")
        check(all("C-c" not in panes[r].keys for r in panes), "never Ctrl-C")
        run_pass(fresh=True)
        check(panes["main"].keys == ["Escape"] and panes["ops"].keys == ["Escape", "Escape"]
              and len(panes["audit"].typed) == 1, "a restarted listener reads what the stop did: nobody twice")
        # Hermes, as measured: its message interrupt ends the turn (on_session_end), then runs the message as a turn.
        for source, state in (("hermes.on_session_end", "idle"), ("hermes.pre_llm_call", "busy"),
                              ("hermes.post_llm_call", "idle")):
            store.write(TARGETS["audit"], state, source=source)
        panes["director"].screen = FIXTURE["screens"]["claude"]["idle"]  # the Director's own turn ends as it would
        store.write(TARGETS["director"], "idle", source="claude.Stop")
        run_pass()
        run_pass()
        settled = {r["role"]: r for r in json.loads(sql("SELECT ticket_board.work_pause_status()::text;"))["pause"]["roles"]}
        for role in ("main", "app", "ops", "inspector"):
            state = store.read(TARGETS[role])
            check(settled[role]["settled"] == "idle" and state.state == "idle",
                  f"{role}'s interrupted turn is seen to end from its screen, then it takes the notice: "
                  f"{settled[role]} {state}")
            told = [m for m in sent_to(role) if m.startswith("WORK PAUSED")]
            check(len(told) == 1 and stop_tickets[role] in told[0], f"{role} is told once, on its ticket: {told} "
                  f"{queued(role)} " + sql("SELECT coalesce(json_agg(json_build_object('event', event, 'kind', kind, "
                  "'reason', busy_reason, 'detail', left(detail::text, 200)) ORDER BY id), '[]')::text FROM "
                  f"(SELECT * FROM ticket_board.notification_trace WHERE target_role = '{role}' ORDER BY id DESC "
                  "LIMIT 8) t;"))
        check(len(panes["main"].keys) == 1 and len(panes["ops"].keys) == 2, "nobody is interrupted twice")
        check(not [m for m in sent_to("audit") if m.startswith("WORK PAUSED")],
              "Hermes was told by its interrupt; nothing is typed again")
        director_bg = [m for m in sent_to("director") if "background commands" in m]
        check(len(director_bg) == 1 and "pid 7001" in director_bg[0] and "main" in director_bg[0]
              and "PGU-20" in director_bg[0] and "Nothing was killed" in director_bg[0],
              f"main's background command is reported to the Director with pid and ticket: {director_bg}")
        status = json.loads(__import__("urllib.request").request.urlopen(base + "/api/work-pause", timeout=5).read())
        roles = {r["role"]: r for r in status["pause"]["roles"]}
        check(roles["main"]["settled"] == "idle" and roles["audit"]["settled"] == "typed"
              and roles["main"]["background"][0]["pid"] == 7001, f"the read shows what the stop did: {roles}")
        sends.clear()
        ended = json.loads(resume("stop over"))
        check(sorted(ended["told"]) == sorted(stop_tickets), f"the resume tells each interrupted role: {ended}")
        run_pass()
        run_pass()
        for role in stop_tickets:
            told = [m for m in sent_to(role) if m.startswith("WORK RESUMED")]
            check(len(told) == 1, f"{role} is told once that work resumed: {sent_to(role)}")
        check(not [m for m in sent_to("director") if m.startswith("WORK RESUMED")], "the Director is not told it")
        # Claude interrupted before it said anything puts the prompt back in its composer: nothing is typed over it.
        sends.clear()
        panes["main"].keys, panes["main"].background = [], []
        panes["main"].busy(returned=True)
        store.write(TARGETS["main"], "busy", source="claude.UserPromptSubmit", now=time.time() - 20)
        # ops: a new turn on a screen still showing the last stop's "cancelled" line, and an interrupt that does not
        # take. audit: its hook says busy but its screen is Hermes's idle prompt, with no busy input row to type into.
        # inspector: interrupted while its provider account is held, so nothing may be typed to it yet.
        panes["ops"].keys, panes["ops"].deaf = [], True
        panes["ops"].screen = FIXTURE["screens"]["copilot"]["interrupted"]
        store.write(TARGETS["ops"], "busy", source="copilot.userPromptSubmitted", now=time.time() - 20)
        panes["audit"].typed, panes["audit"].keys = [], []
        panes["audit"].screen = FIXTURE["screens"]["hermes"]["idle"]
        store.write(TARGETS["audit"], "busy", source="hermes.pre_llm_call", now=time.time() - 20)
        panes["inspector"].keys = []
        panes["inspector"].busy()
        store.write(TARGETS["inspector"], "busy", source="agy.PreInvocation", now=time.time() - 20)
        check('"began": true' in as_role("director", "SELECT ticket_board.hold_provider_account('agy', 'a suspected refusal');"),
              "the agy account is held by hand")
        pause("stop", "second stop")
        run_pass()
        run_pass()
        check(store.read(TARGETS["main"]).state == "idle" and store.read(TARGETS["main"]).source == work_pause.SETTLE_SOURCE,
              f"main's interrupted turn is written over: {store.read(TARGETS['main'])}")
        record = {r["role"]: r for r in json.loads(sql("SELECT ticket_board.work_pause_status()::text;"))["pause"]["roles"]}
        check(panes["ops"].keys == ["Escape", "Escape"] and not record["ops"]["settled"]
              and not [m for m in sent_to("ops") if m.startswith("WORK PAUSED")],
              f"an interrupt that did not take: the old 'cancelled' line is not this turn's end; not settled, not told: "
              f"{record['ops']}")
        check(panes["audit"].typed == [] and "audit" not in record,
              f"Hermes is typed nothing unless its screen shows its busy input row: {panes['audit'].typed}")
        check(panes["inspector"].keys == ["Escape"] and record["inspector"]["settled"] == "idle"
              and not record["inspector"]["noticed_at"] and not sent_to("inspector"),
              f"interrupted while its provider account is held: nothing typed to it: {record['inspector']}")
        rows = list(FIXTURE["screens"]["agy"]["interrupted"]["rows"])
        y = FIXTURE["screens"]["agy"]["interrupted"]["cursor"][1]
        rows[y] = "> half-typed by a person"
        panes["inspector"].screen = {"rows": rows, "cursor": [len(rows[y]), y]}
        cleared = as_role("director", "SELECT ticket_board.clear_provider_account_hold('agy', 'safe');")
        check('"cleared_by": "director"' in cleared, f"the hold is cleared: {cleared}")
        run_pass()
        check(not sent_to("inspector"), "a person's text in its composer: the notice waits rather than type over it")
        panes["inspector"].screen = FIXTURE["screens"]["agy"]["interrupted"]
        run_pass()
        check(len([m for m in sent_to("inspector") if m.startswith("WORK PAUSED")]) == 1, "then it is told, once")
        # A person starts a turn in app's pane after the stop: it is theirs, and left alone.
        panes["app"].keys = []
        panes["app"].busy()
        store.write(TARGETS["app"], "busy", source="codex.UserPromptSubmit")
        run_pass()
        check(panes["app"].keys == [], f"a turn a person began after the stop is left alone: {panes['app'].keys}")
        check(panes["main"].keys == ["Escape"] and record["main"]["settled"] == "prompt_returned"
              and not [m for m in sent_to("main") if m.startswith("WORK PAUSED")],
              f"main's prompt came back to its composer: recorded, and no notice typed over it: {record['main']}")
        changed = json.loads(pause("drain", "quieter now"))
        run_pass()
        check(changed["changed"] and panes["app"].keys == [], "after stop -> drain, a busy pane is not interrupted")
        again = json.loads(pause("stop", "stop again"))
        run_pass()
        check(again["changed"] and panes["app"].keys == ["Escape"],
              f"drain -> stop again: the turn running when stop was set again is interrupted: {panes['app'].keys}")
        resume("second stop over")
        # A resume notice that waited out a later pause would say work resumed after the wrong one: it is dropped.
        sends.clear()
        panes["inspector"].keys = []
        panes["inspector"].busy()
        store.write(TARGETS["inspector"], "busy", source="agy.PreInvocation", now=time.time() - 20)
        pause("stop", "third stop")
        run_pass()
        told_resumed = json.loads(resume("third stop over"))
        pause("drain", "and again at once")
        run_pass()
        resume("fourth over")
        run_pass()
        check("inspector" in told_resumed["told"] and not [m for m in sent_to("inspector") if m.startswith("WORK RESUMED")],
              f"a resume notice overtaken by a later pause is not sent: {sent_to('inspector')}")
        seen["stop"] = "ok"

        # ---------------------------------------------------------------- clocks
        now = datetime.now(timezone.utc)

        def at(minutes: float) -> str:
            return (now + timedelta(minutes=minutes)).isoformat()

        def unpaused(start: float, until: float) -> float:
            return float(sql(f"SELECT extract(epoch FROM '{at(until)}'::timestamptz - "
                             f"ticket_board.unpaused_since('{at(start)}', '{at(until)}'));")) / 60

        sql("DELETE FROM ticket_board.work_pause_roles; DELETE FROM ticket_board.work_pauses;")
        sql(f"INSERT INTO ticket_board.work_pauses (mode, began_at, began_by, reason, ended_at, ended_by, end_reason) "
            f"VALUES ('drain', '{at(100)}', 'director', 'clock', '{at(160)}', 'director', 'clock');")
        check(abs(unpaused(90, 200) - 50) < 0.01, "a closed pause inside the span: its 60 minutes do not count")
        check(abs(unpaused(130, 200) - 40) < 0.01, "a clock started inside the pause counts from its end")
        check(abs(unpaused(90, 130) - 10) < 0.01, "while paused the clock stands still")
        check(abs(unpaused(170, 200) - 30) < 0.01, "a clock started after the pause is untouched")
        # The idle-stall nudge: main idle on PGU-30, last nudged 70 minutes before p_now, 60 of them paused; cadence 30.
        for ticket_id in stop_tickets.values():
            fixture.post_json(base, f"/api/tickets/{ticket_id}/actions/defer", {"reason": "clock case"}, caller="director")
        ticket("PGU-30", "in_progress", "main")
        sql("DELETE FROM ticket_board.ticket_notification_queue;")
        sql(f"UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = '{at(80)}', "
            f"last_nudged_at = '{at(90)}' WHERE ticket_id = 'PGU-30';")

        import psycopg

        def as_listener(statement: str):
            """As the listener's own database role calls it: the only one the reconcilers admit."""
            with psycopg.connect(listener_url, autocommit=True) as conn:
                return conn.execute(statement).fetchone()[0]

        def idle_nudges() -> int:
            as_listener(f"SELECT ticket_board.notify_idle_stall_nudges('{{\"main\": \"{at(80)}\"}}'::jsonb, "
                        f"'{at(160)}', interval '45 seconds', interval '30 minutes', 2);")
            return int(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-30' "
                           "AND kind IN ('nudge', 'escalation');"))

        check(idle_nudges() == 0, "ten unpaused minutes since the last nudge are not the cadence: none at the resume")
        sql("UPDATE ticket_board.work_pauses SET began_at = began_at + interval '50 minutes';")  # only 10 paused now
        check(idle_nudges() >= 1, "the same idle time with the pause shorter: the nudge comes, so the pause was why")
        # The due nudges pg_cron runs every five minutes: the same clock, from the last nudge.
        ticket("PGU-31", "in_progress", "ops")
        sql(f"UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = '{at(80)}', "
            f"last_nudged_at = '{at(90)}' WHERE ticket_id = 'PGU-31';")

        def due_nudges() -> int:
            sql(f"SELECT ticket_board.notify_due_nudges('{at(160)}', interval '30 minutes', 3);")
            return int(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-31' "
                           "AND kind IN ('nudge', 'escalation');"))

        sql("UPDATE ticket_board.work_pauses SET began_at = began_at - interval '50 minutes';")  # 60 paused again
        check(due_nudges() == 0, "pg_cron's due nudge: ten unpaused minutes since the last nudge, none")
        sql("UPDATE ticket_board.work_pauses SET began_at = began_at + interval '50 minutes';")
        check(due_nudges() >= 1, "and with only ten paused, the nudge comes")
        # The turn-end reminder: app's turn ended idle 70 minutes before p_now, 60 of them paused; grace 30 minutes.
        ticket("PGU-32", "in_progress", "app")
        sql("DELETE FROM ticket_board.ticket_notification_queue;")
        sql(f"UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = '{at(80)}', "
            f"last_idle_reminder_at = NULL WHERE ticket_id = 'PGU-32';")

        def turn_end_reminders() -> int:
            as_listener(f"SELECT ticket_board.notify_idle_turn_end_nudges('{{\"app\": \"{at(90)}\"}}'::jsonb, "
                        f"'{at(160)}', interval '30 minutes', '{{}}'::jsonb);")
            return int(sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-32' "
                           "AND kind IN ('idle_reminder', 'escalation');"))

        sql("UPDATE ticket_board.work_pauses SET began_at = began_at - interval '50 minutes';")
        check(turn_end_reminders() == 0, "the turn-end reminder: ten unpaused minutes past the turn's end, none")
        sql("UPDATE ticket_board.work_pauses SET began_at = began_at + interval '50 minutes';")
        check(turn_end_reminders() >= 1, "and with only ten paused, the reminder comes")
        # The board stall digest: a runnable board for 70 minutes, 60 of them paused, threshold 30 minutes.
        sql("DELETE FROM ticket_board.work_pauses; DELETE FROM ticket_board.ticket_notification_queue;")
        sql(f"INSERT INTO ticket_board.work_pauses (mode, began_at, began_by, reason, ended_at, ended_by, end_reason) "
            f"VALUES ('drain', '{at(100)}', 'director', 'clock', '{at(160)}', 'director', 'clock');")
        roles_json = json.dumps({r: {"state": "idle", "detail": ""} for r in TARGETS})

        def stall(minutes: float) -> dict:
            return json.loads(as_listener(f"SELECT ticket_board.note_board_stall('{roles_json}'::jsonb, 1800, "
                                          f"'{at(minutes)}')::text;"))

        sql("UPDATE ticket_board.board_stall_state SET runnable_since = NULL;")
        first = stall(90)
        later = stall(160)
        check(first.get("runnable", 0) >= 1 and later.get("runnable", 0) >= 1 and later.get("episode") is None,
              f"seventy runnable minutes, sixty paused: no stall digest for the paused time: {first} {later}")
        after = stall(190)
        check(after.get("episode") is not None, f"forty unpaused minutes: the digest comes as it would have: {after}")
        # A lease's observation deadline moves on by the paused time; a hard one is the wrapper's and does not.
        from scripts.ticket_board import work_pause as wp

        with psycopg.connect(listener_url, autocommit=True) as conn:
            moved = wp.paused_seconds(conn, (now + timedelta(minutes=90)).timestamp())
        check(moved == 0.0, "paused seconds are counted up to now only: a pause in the future adds nothing yet")
        sql("UPDATE ticket_board.work_pauses SET began_at = now() - interval '20 minutes', ended_at = now() - interval '5 minutes';")
        with psycopg.connect(listener_url, autocommit=True) as conn:
            moved = wp.paused_seconds(conn, (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp())
        check(abs(moved - 900) < 5, f"a lease started 30 minutes ago, 15 of them paused: its deadline moves 15 minutes: {moved}")
        # The pull policy's idle-capacity alert, on a board that declares one: a ticket ready to claim for 70 minutes,
        # 60 of them paused, the policy's 15-minute alert.
        from pull_scheduling_policy_test import pull_document

        from assigned_implementation_queue_test import migrate

        pull_admin = fixture.conninfo(cluster.socket_dir, cluster.port, "pull")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "pull"])
        fixture.psql(pull_admin, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.psql(pull_admin, (root / "scripts/ticket_board/rbac.sql").read_text())  # the roles are the cluster's
        migrate(fixture, pull_admin, root)
        pull_app = fixture.TicketBoardApp(cluster.root / "frames2", cluster.root / "assets2", project="pgu",
                                          ticket_prefix="PGU", database_url=fixture.conninfo(
                                              cluster.socket_dir, cluster.port, "pull", fixture.SERVICE_ROLE))
        cfg = pull_document()
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        pull_app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
        pull_listener = fixture.conninfo(cluster.socket_dir, cluster.port, "pull", "ticket_board_listener")
        fixture.seed_postgres_ticket(pull_admin, "PGU-40", title="ready", state="ready", assignee="unassigned",
                                     commit_exempt=True)
        fixture.psql(pull_admin, f"UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = "
                                 f"'{at(90)}' WHERE ticket_id = 'PGU-40'; INSERT INTO ticket_board.work_pauses (mode, "
                                 f"began_at, began_by, reason, ended_at, ended_by, end_reason) VALUES ('drain', "
                                 f"'{at(100)}', 'director', 'clock', '{at(160)}', 'director', 'clock');")

        def idle_capacity() -> int:
            with psycopg.connect(pull_listener, autocommit=True) as conn:
                conn.execute("SELECT ticket_board.notify_pull_idle_capacity('{\"main\": \"idle, nothing claimable\"}'"
                             f"::jsonb, '{at(160)}')")
            return int(fixture.psql(pull_admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE "
                                                "payload->>'kind' = 'pull_idle_capacity';"))

        check(idle_capacity() == 0, "ten unpaused minutes ready to claim: no idle-capacity alert for the paused time")
        fixture.psql(pull_admin, "UPDATE ticket_board.work_pauses SET began_at = began_at + interval '50 minutes';")
        check(idle_capacity() == 1, "sixty unpaused minutes: the alert comes, so the pause was why")
        # Rework pinned to an author who cannot take it, first seen 70 minutes before p_now, 60 of them paused.
        fixture.seed_postgres_ticket(pull_admin, "PGU-41", title="pinned", state="ready", assignee="unassigned",
                                     commit_exempt=True)
        fixture.psql(pull_admin, "SET session_replication_role = replica; UPDATE ticket_board.tickets SET "
                                 "queued_for_assignee = 'app' WHERE id = 'PGU-41'; UPDATE ticket_board.work_pauses SET "
                                 "began_at = began_at - interval '50 minutes';")

        def strands(minutes: float) -> int:
            with psycopg.connect(pull_listener, autocommit=True) as conn:
                conn.execute("SELECT ticket_board.notify_pull_pinned_rework_strands('{\"main\": \"idle, nothing "
                             f"claimable\"}}'::jsonb, '{{}}'::jsonb, '{at(minutes)}')")
            return int(fixture.psql(pull_admin, "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE "
                                                "payload->>'kind' = 'pull_pinned_rework';"))

        first_seen = strands(90)
        check(first_seen == 0 and strands(160) == 0,
              "rework pinned for ten unpaused minutes: no strand alert for the paused time")
        fixture.psql(pull_admin, "UPDATE ticket_board.work_pauses SET began_at = began_at + interval '50 minutes';")
        check(strands(160) == 1, "with only ten paused, the strand alert comes")
        # A lease's observation deadline: run out, it alerts the Director; paused for 35 of its 50 minutes, not yet.
        from types import SimpleNamespace as Holder

        from scripts.ticket_board import execution_lease_pass

        sql("DELETE FROM ticket_board.work_pauses; DELETE FROM ticket_board.ticket_notification_queue;")
        started = time.time() - 3000
        with psycopg.connect(listener_url, autocommit=True) as conn:
            conn.execute("SELECT ticket_board.observe_execution_lease(%s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, "
                         "to_timestamp(%s), to_timestamp(%s), %s, %s, %s::jsonb)",
                         ("main", "lease-paused", 4242, 4242, 1, started, "sleep 9999", "PGU-30", started, started + 1800,
                          "default", False, "[]"))
        holder = Holder(pid=4242, pgid=4242, started_at=started, cwd="", summary="sleep 9999")
        sql("INSERT INTO ticket_board.work_pauses (mode, began_at, began_by, reason, ended_at, ended_by, end_reason) "
            "VALUES ('drain', now() - interval '40 minutes', 'director', 'lease', now() - interval '5 minutes', "
            "'director', 'lease');")

        def lease_alerts() -> int:
            with psycopg.connect(listener_url, autocommit=True) as conn:
                row = conn.execute("SELECT to_jsonb(l) FROM ticket_board.execution_leases l WHERE child = 'lease-paused'"
                                   ).fetchone()[0]
                return execution_lease_pass._decide(conn, row, holder, "main", None, set(), Path(state_dir), [],
                                                    Path(state_dir), time.time())

        check(lease_alerts() == 0, "a lease run 50 minutes, 35 of them paused, has not reached its 30-minute deadline")
        sql("DELETE FROM ticket_board.work_pauses;")
        check(lease_alerts() == 1, "with no pause the same lease is past it, and the Director is told")
        seen["clocks"] = "ok"
        server.shutdown()
        server.server_close()
    return seen


def static_cases(root: Path) -> None:
    """What needs no board: the measured interrupts, `switchyard status`'s lines, and the board page's switches."""
    sys.path[:0] = [str(root), str(root / "tests")]
    from scripts import project_status
    from scripts.ticket_board import work_pause
    from scripts.ticket_board.frontend import HTML
    from scripts.ticket_board.frontend_script_work_pause import SCRIPT_WORK_PAUSE

    measured = FIXTURE["measured"]
    for run, runtime in (("claude-partial", "claude"), ("codex", "codex"), ("copilot-102", "copilot"), ("agy", "agy")):
        m = measured[run]
        check(list(work_pause.INTERRUPT_KEYS[runtime]) == m["interrupt_keys"] and m["cli_survived"]
              and m["pid_before"] == m["pid_after_interrupt"] and m["next_prompt_words"] == ["PROBE-BUSY", "PROBE-AFTER"]
              and m["resumed_by"] == "session id" and m["resumed_prompt_words"][:2] == ["PROBE-BUSY", "PROBE-AFTER"],
              f"{runtime}: the interrupt the board sends is the one measured to end a turn, keep the CLI and its "
              f"conversation, and resume: {m}")
        screens = FIXTURE["screens"][runtime]
        said = lambda name: work_pause._said(screens[name]["rows"], runtime)  # noqa: E731
        check(said("interrupted") == said("busy") + 1, f"{runtime}: its interrupted screen says so once more than busy")
    check(measured["copilot"]["interrupt_keys"] == ["Escape", "Escape"] and measured["copilot"]["cli_survived"],
          "Copilot 1.0.101 too: Esc Esc")
    hermes = measured["hermes"]
    check("hermes" not in work_pause.INTERRUPT_KEYS and work_pause.MESSAGE_INTERRUPT["hermes"] == "msg=interrupt"
          and any("msg=interrupt" in row for row in FIXTURE["screens"]["hermes"]["busy"]["rows"])
          and hermes["cli_survived"] and hermes["next_prompt_words"][-1] == "PROBE-AFTER"
          and any("Interrupted during API call" in row for row in FIXTURE["screens"]["hermes"]["after_message"]["rows"]),
          f"Hermes: Esc does nothing; a message typed while it works is its interrupt: {hermes}")
    returned = FIXTURE["screens"]["claude"]["returned"]
    check(work_pause._said(returned["rows"], "claude") == 0
          and any("PROBE-BUSY" in row for row in returned["rows"][returned["cursor"][1]:returned["cursor"][1] + 1]),
          "Claude interrupted before it said anything: the prompt is back in the composer, no 'Interrupted' line")
    lines = project_status.work_pause_lines("pgu", {"paused": True, "pause": {
        "mode": "stop", "began_at": "2026-11-12T10:00:00", "began_by": "director", "reason": "release broke",
        "held": {"main": 2}}})
    check(len(lines) == 2 and "pgu WORK PAUSED (stop now) since 2026-11-12T10:00:00 by director: release broke" in lines[0]
          and "main (2)" in lines[1] and "resume-work" in lines[1], f"switchyard status says so: {lines}")
    check(project_status.work_pause_lines("pgu", {"paused": False, "pause": None}) == []
          and project_status.work_pause_lines("pgu", None) == [], "and says nothing when not paused or unreadable")
    check('id="workPauseBanner"' in HTML and 'id="workPausePanel"' in HTML and "refreshWorkPause" in HTML,
          "the board page carries the banner and the switches")
    if subprocess.run(["sh", "-c", "command -v node"], capture_output=True).returncode != 0:
        return
    program = """
const elements = {};
function element(id) {
  const el = { id, hidden: true, children: [], dataset: {}, textContent: '', value: '', disabled: false, listeners: {},
    replaceChildren(...kids) { this.children = kids; }, focus() {},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelector(sel) { return elements[sel] || (elements[sel] = element(sel)); } };
  return el;
}
elements.workPauseBanner = element('workPauseBanner');
elements.workPausePanel = element('workPausePanel');
global.document = { getElementById: id => elements[id] || null,
  createElement: tag => ({ tag, textContent: '' }) };
global.setInterval = () => 0;
let status = { paused: true, pause: { id: 3, mode: 'drain', began_by: 'user', began_at: 'T', reason: 'why', held: { main: 1 } } };
const sent = [];
global.fetch = async () => ({ ok: true, json: async () => status });
global.sendBoardWrite = async (path, payload) => { sent.push([path, payload]); return {}; };
""" + SCRIPT_WORK_PAUSE + """
(async () => {
  await new Promise(r => setTimeout(r, 10));
  const banner = elements.workPauseBanner, panel = elements.workPausePanel;
  const out = { hidden: banner.hidden, title: banner.children[0] && banner.children[0].textContent,
    body: banner.children[1] && banner.children[1].textContent,
    drainDisabled: panel.querySelector('[data-pause-mode="drain"]').disabled,
    stopDisabled: panel.querySelector('[data-pause-mode="stop"]').disabled,
    resumeDisabled: panel.querySelector('[data-pause-resume]').disabled };
  panel.querySelector('[data-pause-mode="stop"]').listeners.click();
  await new Promise(r => setTimeout(r, 10));
  out.withoutReason = sent.length;
  out.statusText = panel.querySelector('.work-pause-status').textContent;
  panel.querySelector('[data-pause-reason]').value = 'it is urgent';
  status = { paused: true, pause: { ...status.pause, mode: 'stop' } };
  panel.querySelector('[data-pause-mode="stop"]').listeners.click();
  await new Promise(r => setTimeout(r, 10));
  out.sent = sent;
  out.stopDisabledAfter = panel.querySelector('[data-pause-mode="stop"]').disabled;
  status = { paused: false, pause: null };
  panel.querySelector('[data-pause-reason]').value = 'done';
  panel.querySelector('[data-pause-resume]').listeners.click();
  await new Promise(r => setTimeout(r, 10));
  out.sentAfterResume = sent.length;
  out.hiddenAfterResume = banner.hidden;
  console.log(JSON.stringify(out));
})();
"""
    run = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    check(run.returncode == 0, f"the page script runs: {run.stderr[-500:]}")
    page = json.loads(run.stdout.strip().splitlines()[-1])
    check(page["hidden"] is False and page["title"] == "Work paused: drain" and "Set by user" in page["body"]
          and "why" in page["body"] and "main (1)" in page["body"], f"the banner says what stands: {page}")
    check(page["drainDisabled"] and not page["stopDisabled"] and not page["resumeDisabled"],
          f"the standing mode's switch is off, the other and resume are on: {page}")
    check(page["withoutReason"] == 0 and "Say why first" in page["statusText"], "nothing is sent without a reason")
    check(page["sent"][0] == ["/api/tickets/actions/pause_work", {"mode": "stop", "reason": "it is urgent"}]
          and page["stopDisabledAfter"], f"Stop now sends the User's recorded action: {page['sent']}")
    check(page["sentAfterResume"] == 2 and page["sent"][1] == ["/api/tickets/actions/resume_work", {"reason": "done"}]
          and page["hiddenAfterResume"], f"Resume sends its action and the banner goes: {page}")


def main() -> int:
    static_cases(ROOT)
    seen = scenario(ROOT)
    print(json.dumps(seen))
    print(f"{CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
