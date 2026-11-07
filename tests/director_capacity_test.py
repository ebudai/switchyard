#!/usr/bin/env python3
"""SYRD-728: the Director's Codex turn that ended on "Selected model is at capacity" is retried, then the operator told.

On 2026-11-02 the Director's Codex pane (0.164.0) ended its turn on "■ Selected model is at capacity. Please try a
different model." and returned to an empty prompt; a person's "please retry" met the same error, and nothing resumed
the Director when capacity came back. SYRD-594 retries ticket owners and leaves the Director out.

The screens are SYRD-594's real Codex captures (tests/fixtures/syrd594_codex_capacity_screens.json), and near misses
made from them; the gate is the real PaneActivityGate with tmux standing in for its captures. The board is
production-built, with the real server and the real listener, a new listener each pass and a new gate every few
seconds in the long case: a restart. Everything runs in real time, each delay a thirtieth of production's; the
board function's own cases give it the clock, at production's delays. No model is called: the Director's provider
is the scenario's.

- A failed turn the board did not start with a notice (no ticket): retried once, recovered, nothing else typed.
- A failed turn that a board notice about a ticket started: the retry names the ticket.
- Still at capacity: three retries, one at a time, across restarts, then one durable alert -- the phone bridge's
  event and the board page's banner -- and nothing typed into the Director's pane about it; a later normal turn
  clears it.
- A person typing under the error, or submitting a turn of their own: the board stops. A new Director runtime or
  pane process: it stops.
- Not this error: Codex's quota error, a reply that quotes the error, and the same layout naming an authentication
  failure, a refusal or a model that is not there.
- Privileges, the migration applied alone before rbac.sql, and the page's banner run in node.

Main runs first, from a git archive: the Director's failed turn is never retried.
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
BEFORE = "63926b5d0d7012c1780c7c54d978c02fbc2f5e1d"
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1028_syrd728_director_capacity.sql"
DIRECTOR = "pgu-director:0.0"
ERROR = "Selected model is at capacity. Please try a different model."
STALE, GRACE = 4.0, 1.5
DELAYS = (2.0, 4.0, 10.0)
CHECKS = 0
#: Which parts run (live, functions, page, boundary): all of them, unless a mutation run names fewer.
PARTS = set((os.environ.get("SYRD728_PARTS") or "live,functions,page,boundary").split(","))


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def scenario(root: Path) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    os.environ["TICKET_BOARD_CAPACITY_RETRY_DELAYS"] = ",".join(str(d) for d in DELAYS)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from codex_capacity_retry_test import Pane, fake_tmux, screens
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    assert Path(nl.__file__).resolve().is_relative_to(root.resolve()), nl.__file__
    shown = screens()
    base_screen = shown["capacity"]
    error_row = next(i for i, row in enumerate(base_screen["rows"]) if row.startswith("■"))

    def other_error(text: str) -> dict:
        rows = list(base_screen["rows"])
        rows[error_row] = f"■ {text}"
        return {"rows": rows, "joined": rows, "cursor": base_screen["cursor"], "hook": base_screen["hook"]}

    shown["auth"] = other_error("unexpected status 401 Unauthorized: Incorrect API key provided")
    shown["refusal"] = other_error("Your request was flagged as potentially violating our usage policy.")
    shown["no model"] = other_error("The model `gpt-6-sol` does not exist or you do not have access to it.")
    seen: dict = {}
    with temporary_cluster(prefix="s728", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd728-state.") as state_dir:
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
            if role["name"] == "director":
                role["runtime"] = "codex"  # syrd's Director runs Codex
        app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        sleeper = subprocess.Popen(["sleep", "3600"])
        from scripts.ticket_board.peer_identity import read_process

        def register(generation: int) -> None:
            """The Director's live runtime, as a pane's registration records it."""
            sql("DELETE FROM ticket_board.role_runtime_assignments WHERE role = 'director'; "
                "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                f"process_pid, process_start_time, process_uid, generation) VALUES ('director', 'codex', '{DIRECTOR}', "
                f"'/w', '/s', {sleeper.pid}, {read_process(sleeper.pid).start_time}, {os.getuid()}, {generation});")

        register(1)
        store = nl.PaneHookStateStore(Path(state_dir))
        pane = Pane(shown["recovered"], 900, time.time() - 3600)
        store.write(DIRECTOR, "idle", source="codex.Stop", now=time.time() - 600)
        tmux, table = fake_tmux({DIRECTOR: pane})
        sends: list[tuple[str, str, float]] = []
        provider = {"mode": "full"}
        phase = {"name": ""}

        def directorctl(target: str, message: str) -> dict:
            """Typed and submitted: the Director's Codex takes it and runs a turn, as its provider decides."""
            sends.append((phase["name"], message, time.monotonic()))
            store.write(target, "busy", source="codex.UserPromptSubmit")
            if provider["mode"] == "full":  # still at capacity: the turn ends on the error, and no Stop hook runs
                pane.screen = shown["capacity-twice"]
                if "after" in provider:  # what a person does right after
                    provider["after"]()
            else:  # the provider answers: the turn ends, and so does Codex's Stop
                pane.screen = shown["recovered"]
                store.write(target, "idle", source="codex.Stop")
            return {}

        def new_gate():
            return nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=STALE,
                                       role_runtimes={"director": "codex", "main": "codex"})

        process = {"gate": new_gate(), "restarts": 0}

        def real_pass() -> int:
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=process["gate"].is_working,
                target_exists=lambda target: target == DIRECTOR, poll_seconds=0,
                submission_confirm_seconds=0.4, submission_poll_seconds=0.05, idle_stall_grace_seconds=GRACE)
            listener.delivery_submitter = lambda _target: None
            return listener.listen_once(max_notifications=listener.delivered_count + 1)

        def run_for(seconds: float, name: str, restart_every: float = 0.0) -> None:
            phase["name"] = name
            end = last_restart = time.monotonic()
            end += seconds
            while time.monotonic() < end:
                if restart_every and time.monotonic() - last_restart >= restart_every:
                    process["gate"] = new_gate()
                    process["restarts"] += 1
                    last_restart = time.monotonic()
                real_pass()
                time.sleep(0.2)

        def fail_turn(screen: str = "capacity") -> None:
            """A turn the Director started itself -- a person's prompt, say -- ends on `screen`."""
            store.write(DIRECTOR, "busy", source="codex.UserPromptSubmit")
            pane.screen = shown[screen]

        def settle() -> None:
            """The Director's pane put back as it began: idle after a new turn that ended normally."""
            pane.screen = shown["recovered"]
            store.write(DIRECTOR, "busy", source="codex.UserPromptSubmit")
            store.write(DIRECTOR, "idle", source="codex.Stop")
            run_for(0.6, "settle")

        def typed(name: str) -> list[str]:
            return [message for phase_name, message, _at in sends if phase_name.startswith(name)]

        def episodes() -> list | str:
            if sql("SELECT to_regclass('ticket_board.director_capacity_episodes') IS NULL;") == "t":
                return "absent"
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_object('failures', jsonb_array_length(failures), 'attempts', "
                "(SELECT coalesce(json_agg(a->>'outcome'), '[]') FROM jsonb_array_elements(attempts) a), 'ticket', "
                "ticket_id, 'closed', close_reason, 'alert', user_event_id IS NOT NULL, 'cleared', cleared_at IS NOT NULL) "
                "ORDER BY id), '[]')::text FROM ticket_board.director_capacity_episodes;"))

        def director_notices() -> list[str]:
            """What the queue holds or held for the Director about capacity: nothing, ever."""
            return json.loads(sql(
                "SELECT coalesce(json_agg(detail->'payload'->>'kind'), '[]')::text FROM ticket_board.notification_trace "
                "WHERE target_role = 'director' AND event = 'enqueue' AND detail->'payload'->>'kind' LIKE '%capacity%';"))

        def page_status() -> object:
            import urllib.error
            import urllib.request
            try:
                with urllib.request.urlopen(base + "/api/director-capacity", timeout=10) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return f"HTTP {exc.code}"

        try:
            if "live" in PARTS:
                # 1. A turn the Director started itself fails; the first retry finds the provider answering.
                fail_turn()
                provider["mode"] = "answers"
                run_for(DELAYS[0] + 2.0, "transient")
                seen["transient"] = {"typed": typed("transient"), "episodes": episodes()}
                settle()

                # 2. A board notice about a ticket starts the turn, which fails; the retry names the ticket.
                provider["mode"] = "full"
                fixture.seed_postgres_ticket(admin, "PGU-911", title="Triage this", state="analysis", assignee="director",
                                             commit_exempt=True)
                fixture.post_json(base, "/api/tickets/PGU-911/actions/add_comment", {"text": "Please look."}, caller="user")
                run_for(1.0, "notice")
                provider["mode"] = "answers"
                run_for(DELAYS[0] + 2.0, "ticket")
                seen["ticket"] = {"notice": typed("notice"), "typed": typed("ticket"), "episodes": episodes()[-1:]}
                fixture.post_json(base, "/api/tickets/PGU-911/actions/force_move", {"state": "done", "assignee": "unassigned"},
                                  caller="director")  # its reminders would start the Director's later turns
                settle()

                # 3. Still at capacity through every retry, the listener restarting every few seconds: one alert, never
                #    to the Director's pane. Then a person's turn ends normally, and the alert clears.
                provider["mode"] = "full"
                time.sleep(10.5)  # well clear of PGU-911's last notice: this turn is a person's, and names no ticket
                fail_turn()
                run_for(sum(DELAYS) + 8.0, "exhausted", restart_every=3.0)
                seen["exhausted"] = {"typed": typed("exhausted"), "episodes": episodes()[-1:], "restarts": process["restarts"],
                                     "notices": director_notices(), "page": page_status()}
                if seen["exhausted"]["episodes"] != "absent":
                    seen["exhausted"]["events"] = json.loads(sql(
                        "SELECT (ticket_board.user_events_after(0, 50)->'events')::text;"))
                run_for(DELAYS[-1] + 2.0, "after exhausted")
                seen["exhausted"]["later"] = typed("after exhausted")
                store.write(DIRECTOR, "busy", source="codex.UserPromptSubmit")
                pane.screen = shown["recovered"]
                store.write(DIRECTOR, "idle", source="codex.Stop")
                run_for(1.0, "cleared")
                seen["cleared"] = {"page": page_status(), "episodes": episodes()[-1:]}
                if seen["cleared"]["episodes"] != "absent":
                    seen["cleared"]["events"] = json.loads(sql("SELECT (ticket_board.user_events_after(0, 50)->'events')::text;"))

                # 4. A person types under the error before the retry: the board stops, and their draft stays.
                fail_turn()
                run_for(1.0, "draft before")
                pane.screen = shown["draft-after-capacity"]
                run_for(DELAYS[0] + 2.0, "draft")
                seen["draft"] = {"typed": typed("draft"), "episodes": episodes()[-1:]}
                settle()

                # 5. A person submits their own turn, which fails too: the board stops.
                fail_turn()
                run_for(1.0, "own before")
                time.sleep(1.1)
                fail_turn("capacity-twice")
                run_for(DELAYS[0] + DELAYS[1] + 2.0, "own")
                seen["own"] = {"typed": typed("own"), "episodes": episodes()[-1:]}
                settle()

                # 6. The Director's runtime is registered again before the retry: a new conversation; the board stops.
                fail_turn()
                run_for(1.0, "runtime before")
                register(2)
                run_for(DELAYS[0] + 2.0, "runtime")
                seen["runtime"] = {"typed": typed("runtime"), "episodes": episodes()[-1:]}
                settle()

                # 6b. The pane's process is replaced, under the same registration: a new conversation; it stops.
                fail_turn()
                run_for(1.0, "pane before")
                pane.pid, pane.started = 950, time.time() - 5
                run_for(DELAYS[0] + 2.0, "pane")
                seen["pane"] = {"typed": typed("pane"), "episodes": episodes()[-1:]}
                settle()

                # 6c. Audit's case, live: retry 1 is typed and fails; a moment later, before any pass looks, a person
                #     submits their own turn, which fails too. The board stops: no more retries, and no alert.
                fail_turn()

                def person_submits() -> None:
                    store.write(DIRECTOR, "busy", source="codex.UserPromptSubmit")
                    pane.screen = shown["capacity"]

                provider["after"] = lambda: threading.Timer(0.15, person_submits).start()
                run_for(DELAYS[0] + 1.5, "person")
                provider.pop("after", None)
                run_for(DELAYS[1] + DELAYS[2] + 4.0, "person later")
                seen["person"] = {"typed": typed("person"), "later": typed("person later"), "episodes": episodes()[-1:],
                                  "notices": director_notices()}
                settle()

                # 6d. A board notice goes out while an episode is open, and its turn fails too: the board's own, so the
                #     episode carries on, and the next retry recovers it.
                fail_turn()
                run_for(1.0, "notice during before")
                fixture.seed_postgres_ticket(admin, "PGU-913", title="Another", state="analysis", assignee="director",
                                             commit_exempt=True)
                run_for(1.5, "notice during")
                provider["mode"] = "answers"
                run_for(DELAYS[0] + DELAYS[1] + 3.0, "notice during retry")
                provider["mode"] = "full"
                seen["notice during"] = {"notices": typed("notice during"), "episodes": episodes()[-1:]}
                fixture.post_json(base, "/api/tickets/PGU-913/actions/force_move", {"state": "done", "assignee": "unassigned"},
                                  caller="director")
                settle()
                time.sleep(10.5)

                # 7. Not this error.
                seen["others"] = {}
                for screen in ("quota", "quoted reply, turn still running", "auth", "refusal", "no model"):
                    before = episodes()
                    fail_turn(screen)
                    run_for(DELAYS[0] + 2.0, f"other {screen}")
                    seen["others"][screen] = {"typed": typed(f"other {screen}"), "new": episodes() != before}
                    settle()
                # A Director that is not a Codex pane, whatever its screen shows.
                before = episodes()
                fail_turn()
                store.write(DIRECTOR, "busy", source="claude.UserPromptSubmit")
                run_for(DELAYS[0] + 2.0, "other claude")
                seen["others"]["claude"] = {"typed": typed("other claude"), "new": episodes() != before}
                settle()

            if "functions" in PARTS and sql("SELECT to_regprocedure('ticket_board.note_director_capacity(jsonb, "
                                            "double precision[], timestamptz)') IS NOT NULL;") == "t":
                seen["run"] = run_cases(psycopg, listener_url, new_gate, fail_turn, settle, pane, shown, store)
                # Last: its failures are dated in 2030, and no earlier turn opens an episode after them.
                fixture.seed_postgres_ticket(admin, "PGU-912", title="Done already", state="done", assignee="unassigned",
                                             commit_exempt=True)
                seen["functions"] = function_cases(sql, psycopg, listener_url, register)
                seen["bridge"] = bridge_cases(sql)
        finally:
            server.shutdown()
            server.server_close()
            sleeper.kill()
            sleeper.wait()
    return seen


def function_cases(sql, psycopg, listener_url: str, register) -> dict:
    """The board function itself, as the listener calls it, at the moments given: production's delays."""
    seen: dict = {}
    sql("UPDATE ticket_board.director_capacity_episodes SET closed_at = coalesce(closed_at, now()), "
        "close_reason = coalesce(close_reason, 'recovered'), cleared_at = coalesce(cleared_at, now());")
    count = "SELECT count(*) FROM ticket_board.director_capacity_episodes;"
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE target_role = 'director';")  # the cases' own clock
    pane = {"pid": 4242, "start_time": 1000.0}
    t0 = 1_900_000_000.0

    def at(offset: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0 + offset)) + f".{int((t0 + offset) % 1 * 1e6):06d}+00:00"

    def note(turn: float | None, offset: float, **extra) -> dict:
        report = {"capacity_turn": at(turn) if turn is not None else None, "ended_turn": None, "human": False,
                  "pane": pane, **extra}
        try:
            with psycopg.connect(listener_url, autocommit=True) as conn:
                row = conn.execute("SELECT ticket_board.note_director_capacity(%s::jsonb, %s::double precision[], "
                                   "%s::timestamptz)", (json.dumps(report), [60.0, 120.0, 300.0], at(offset))).fetchone()
        except psycopg.Error as exc:  # an answer, which the checks compare
            return {"raised": str(exc).splitlines()[0][:200]}
        return {k: v for k, v in row[0].items() if k != "message"}

    def record(action: dict, outcome: str, turn: float | None = None) -> None:
        """What the listener did with it, and the turn it started, as the pane's hook recorded it."""
        if "episode" not in action:  # nothing was claimed: the checks below say so
            return
        try:
            with psycopg.connect(listener_url, autocommit=True) as conn:
                conn.execute("SELECT ticket_board.record_director_capacity_attempt(%s, %s, %s, %s::timestamptz)",
                             (action["episode"], action["attempt"], outcome, at(turn) if turn is not None else None))
        except psycopg.Error as exc:  # an answer, which the checks see
            seen.setdefault("record errors", []).append(str(exc).splitlines()[0][:160])

    # Nothing to tell a pane change by, or no registered runtime: no episode.
    before = sql(count)
    seen["no pane"] = note(0, 5, pane=None)
    sql("DELETE FROM ticket_board.role_runtime_assignments WHERE role = 'director';")
    seen["no runtime"] = note(0, 5)
    register(3)
    seen["none opened"] = sql(count) == before
    seen["opened"] = note(0, 5)
    seen["at 64"] = note(0, 64)
    # A notice already on its way to the Director goes first, and is itself the next turn.
    sql("SELECT ticket_board.enqueue_notification('PGU-912', 'ticket_update', 'director', 'a notice first', "
        "'{\"kind\": \"ticket_update\", \"id\": \"PGU-912\"}'::jsonb, 'syrd728-first');")
    seen["queued first at 65.5"] = note(0, 65.5)
    sql("DELETE FROM ticket_board.ticket_notification_queue WHERE dedupe_key = 'syrd728-first';")
    first = seen["at 66"] = note(0, 66)
    seen["again at 67"] = note(0, 67)  # claimed, its listener stopped before recording it: never typed again
    record(first, "sent", 67)
    record(first, "not_sent")  # once only: a second record of the same attempt changes nothing
    seen["attempts after"] = json.loads(sql("SELECT (SELECT json_agg(a->>'outcome') FROM jsonb_array_elements(attempts) a)"
                                            "::text FROM ticket_board.director_capacity_episodes WHERE closed_at IS NULL;"))
    # Typed, and nothing seen of it since: the next waits its delay from the attempt, not from the old failure.
    seen["lost at 150"] = note(0, 150)
    seen["second failure at 160"] = note(67, 160)
    seen["at 279"] = note(67, 279)
    second = seen["at 281"] = note(67, 281)
    record(second, "not_sent")  # the pane had changed: nothing typed, and it does not count
    seen["after not_sent at 282"] = note(67, 282)
    record(seen["after not_sent at 282"], "sent", 282)
    seen["third failure at 285"] = note(282, 285)
    seen["at 584"] = note(282, 584)
    third = seen["at 586"] = note(282, 586)
    record(third, "send_failed")
    record(third, "bogus")
    seen["bogus"] = seen.pop("record errors", ["recorded"])[-1]
    # Nothing new came of the third: after its own wait, the alert.
    seen["at 800"] = note(282, 800)
    seen["at 887"] = note(282, 887)
    seen["status"] = json.loads(sql("SELECT ticket_board.director_capacity_status()::text;"))
    seen["after alert"] = note(282, 900)
    # Audit's case: retry 1 is typed and its turn recorded; a person's own turn begins 13 s later and fails too. It
    # is not the retry's turn, however close: the board stops, and nothing more comes of the episode.
    seen["person opened"] = note(2000, 2005)
    person = seen["person at 2066"] = note(2000, 2066)
    record(person, "sent", 2067)
    seen["person's turn fails at 2085"] = note(2080, 2085)
    seen["person at 2400"] = note(2080, 2400)
    seen["person at 3000"] = note(2080, 3000)
    seen["person episode"] = json.loads(sql(
        "SELECT json_build_array(close_reason, jsonb_array_length(failures), user_event_id IS NULL)::text "
        "FROM ticket_board.director_capacity_episodes ORDER BY id DESC LIMIT 1;"))
    # A retry typed whose turn was never seen claims none: the next failed turn stops the board.
    seen["unseen opened"] = note(4000, 4005)
    unseen = seen["unseen at 4066"] = note(4000, 4066)
    record(unseen, "sent")
    seen["unseen turn fails at 4070"] = note(4067, 4070)
    seen["unseen episode"] = sql("SELECT close_reason FROM ticket_board.director_capacity_episodes ORDER BY id DESC "
                                 "LIMIT 1;")
    seen["grants"] = {f.split("(")[0]: sql(
        "SELECT coalesce(string_agg(DISTINCT a.grantee::regrole::text, '+'), '') FROM pg_proc p, aclexplode(p.proacl) a "
        f"WHERE p.oid = 'ticket_board.{f}'::regprocedure AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;")
        for f in ("note_director_capacity(jsonb, double precision[], timestamptz)",
                  "record_director_capacity_attempt(bigint, integer, text, timestamptz)", "director_capacity_status()")}
    return seen


def run_cases(psycopg, listener_url: str, new_gate, fail_turn, settle, pane, shown, store) -> dict:
    """director_capacity.run itself: the pane read again just before typing, and a sender that fails."""
    import logging

    from scripts.ticket_board import director_capacity

    seen: dict = {}

    class Listener:
        logger = logging.getLogger("syrd728")
        submission_confirm_seconds = 0.5

        def __init__(self, sender) -> None:
            self.sender = sender

    class Changing:
        """The listener's connection; the person starts typing the moment the board has claimed the attempt."""

        def __init__(self, conn) -> None:
            self.conn = conn

        def execute(self, statement, params=()):
            result = self.conn.execute(statement, params)
            if "note_director_capacity" in statement:
                pane.screen = shown["draft-after-capacity"]
            return result

    def outcomes(conn) -> list:
        """Each attempt's outcome, and whether it recorded the turn it started."""
        row = conn.execute("SELECT (SELECT json_agg(json_build_array(a->>'outcome', a->'turn' <> 'null'::jsonb)) "
                           "FROM jsonb_array_elements(attempts) a) FROM ticket_board.director_capacity_episodes "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] or []

    def typed_and_started(target, message):
        """Typed and taken: the pane's hook records the turn starting."""
        typed.append(message)
        store.write(target, "busy", source="codex.UserPromptSubmit")
        pane.screen = shown["capacity-twice"]

    with psycopg.connect(listener_url, autocommit=True) as conn:
        for name, sender, wrap in (
                ("pane changed", lambda target, message: typed.append(message), Changing),
                ("sender failed", lambda target, message: (_ for _ in ()).throw(OSError("tmux: no server")), None),
                # Typed, but the pane's hook records no turn: nothing to say which turn was the board's.
                ("hook silent", lambda target, message: typed.append(message), None),
                ("taken", typed_and_started, None)):
            typed: list[str] = []
            settle()
            fail_turn()
            gate = new_gate()
            director_capacity.run(Listener(sender), conn, gate)  # opens the episode
            time.sleep(DELAYS[0] + 0.3)
            gate = new_gate()
            seen[name] = {"returned": director_capacity.run(Listener(sender), wrap(conn) if wrap else conn, gate),
                          "typed": len(typed), "outcomes": outcomes(conn)}
    settle()
    # A gate that reads no hook state -- a delivery's stand-in, or an older one -- names no turn, and raises nothing.
    try:
        seen["no store"] = director_capacity.started_turn(object(), DIRECTOR, 0.0)
    except Exception as exc:  # an answer, which the check sees
        seen["no store"] = f"raised {type(exc).__name__}"
    return seen


def bridge_cases(sql) -> dict:
    """The phone bridge, reading the alert as the board serves it."""
    from scripts import user_notify, user_notify_pushover as pushover, user_notify_setup as setup

    answer = json.loads(sql("SELECT ticket_board.user_events_after(0, 50)::text;"))
    latest, events = user_notify.checked_events(answer, "pgu", "PGU", 0)
    alert = [e for e in events if e["kind"] == "director_capacity"]
    message = pushover.message_for({**alert[-1], "project": "pgu"}, link="https://host.ts.net:8443/") if alert else None
    return {"kinds": sorted({e["kind"] for e in events}), "alert": alert[-1:] and {k: alert[-1][k] for k in
                                                                                    ("ticket", "problem", "standing")},
            "message": message and [message.title, message.body, message.priority, message.url_title],
            "default_on": setup.DEFAULTS["events"].get("director_capacity")}


def page_cases(root: Path) -> dict:
    """The board page's banner, run in node with a fetch and a document standing in."""
    sys.path[:0] = [str(root)]
    from scripts.ticket_board import frontend

    html = frontend.render_html()
    try:
        from scripts.ticket_board.frontend_script_director_capacity import SCRIPT_DIRECTOR_CAPACITY
    except ImportError:
        return {"element": 'id="directorCapacityAlert"' in html, "script": None}
    harness = """
const answers = JSON.parse(process.argv[1]);
const results = [];
(async () => {
  // One banner across the answers, as on the page: shown, then cleared, then an answer it could not read.
  const element = {hidden: true, children: [], replaceChildren(...kids) { this.children = kids; }};
  for (const answer of answers) {
    globalThis.document = {getElementById: (id) => id === 'directorCapacityAlert' ? element : null,
                           createElement: (tag) => ({tag, textContent: ''})};
    globalThis.fetch = async () => answer === 'down' ? {ok: false, status: 502} : {ok: true, json: async () => answer};
    globalThis.setInterval = () => 0;
    eval(process.argv[2]);
    await new Promise((resolve) => setTimeout(resolve, 20));
    results.push({hidden: element.hidden, text: element.children.map((c) => c.textContent)});
  }
  console.log(JSON.stringify(results));
})();
"""
    answers = [{"alert": {"error": "Selected model is at capacity. Please try a different model.",
                          "since": "2026-11-02T13:20:00+00:00", "attempts": 3}}, {"alert": None}, "down"]
    done = subprocess.run(["node", "-e", harness, json.dumps(answers), SCRIPT_DIRECTOR_CAPACITY], capture_output=True,
                          text=True, timeout=60)
    from scripts.ticket_board import director_capacity

    try:
        no_database = director_capacity.status(object())
    except Exception as exc:  # an answer, which the check compares
        no_database = f"raised {type(exc).__name__}"
    return {"element": 'id="directorCapacityAlert"' in html, "included": SCRIPT_DIRECTOR_CAPACITY in html,
            "runs": json.loads(done.stdout) if done.returncode == 0 else done.stderr[-500:],
            # The in-memory board app has no database to keep episodes in: its page asks, and is told none.
            "no_database": no_database}


def boundary(root: Path, before: Path) -> dict:
    """Main's board as deployed, then pgu1028 alone, before the runner's revocation and rbac.sql; then the rest."""
    sys.path[:0] = [str(root), str(root / "tests")]
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, migrate
    from temporary_cluster import temporary_cluster

    roles = ("director", "user", "ops", "app", "main", "audit", fixture.SERVICE_ROLE, "ticket_board_listener")
    functions = ("note_director_capacity(jsonb, double precision[], timestamptz)",
                 "record_director_capacity_attempt(bigint, integer, text, timestamptz)", "director_capacity_status()")
    seen: dict = {}
    with temporary_cluster(prefix="s728b", shutdown="immediate") as cluster:
        for name, strip in (("migration", False), ("control", True)):
            admin = build_board(fixture, cluster, before, name) if name == "migration" else None
            if admin is None:
                admin = fixture.conninfo(cluster.socket_dir, cluster.port, name)
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", name])
                fixture.psql(admin, (before / "scripts/ticket_board/schema.sql").read_text())
                fixture.psql(admin, (before / "scripts/ticket_board/rbac.sql").read_text())
                migrate(fixture, admin, before)
            text = MIGRATION.read_text()
            if strip:
                text = "".join(l for l in text.splitlines(True) if not l.startswith("REVOKE EXECUTE ON FUNCTION"))
            fixture.psql(admin, text)

            def who() -> dict:
                return {f.split("(")[0]: sorted(r for r in roles if fixture.psql(
                    admin, f"SELECT has_function_privilege('{r}', 'ticket_board.{f}', 'EXECUTE');").strip() == "t")
                    for f in functions}
            seen[name] = {"window": who()}
            migrate(fixture, admin, root)
            seen[name]["after_rbac"] = who()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, *extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL",
                                                                     "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), *extra],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=580,
                           stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


RETRY = (f'Your last turn ended on Codex\'s error "{ERROR}" before it finished{{}} (automatic retry {{}} of 3). Before you '
         "repeat anything, check what that turn already did -- the board, git status and log, and any command it "
         "ran -- and do not redo what took effect. Then carry on from where it stopped, or say it was finished.")


def retries(typed: list[str]) -> list[str]:
    return [m for m in typed if "automatic retry" in m]


def before(got: dict) -> None:
    """Main: the Director's failed turn is never retried, whatever follows."""
    check(all(retries(got[case]["typed"]) == [] for case in ("transient", "ticket", "exhausted", "draft", "own", "runtime"))
          and got["transient"]["typed"] == [] and got["transient"]["episodes"] == "absent",
          f"main: nothing resumes the Director after its turn ends on the error: {got['transient']}")
    check(got["exhausted"]["page"] == "HTTP 404" and "functions" not in got,
          f"main: no alert and no episode anywhere: {got['exhausted']['page']}")


def candidate(got: dict) -> None:
    if "transient" in got:
        live(got)
    if "functions" in got:
        functions(got)


def live(got: dict) -> None:
    t = got["transient"]
    check(t["typed"] == [RETRY.format("", 1)] and t["episodes"] == [
        {"failures": 1, "attempts": ["sent"], "ticket": None, "closed": "recovered", "alert": False, "cleared": False}],
          f"a turn the Director began itself fails once: one retry, which recovers it, and nothing else typed: {t}")
    k = got["ticket"]
    check(retries(k["typed"]) == [RETRY.format("; it began with the board's notice about PGU-911", 1)]
          and k["episodes"] == [{"failures": 2, "attempts": ["sent"], "ticket": "PGU-911", "closed": "recovered",
                                 "alert": False, "cleared": False}],
          f"a turn a board notice began: its retry names the ticket, and it recovers: {k}")
    e = got["exhausted"]
    check(e["typed"] == [RETRY.format("", n) for n in (1, 2, 3)] and e["restarts"] >= 3 and e["later"] == []
          and e["episodes"] == [{"failures": 4, "attempts": ["sent"] * 3, "ticket": None, "closed": "exhausted",
                                 "alert": True, "cleared": False}],
          f"still at capacity: three retries, one at a time, across listener restarts, then nothing more: {e}")
    check(e["notices"] == [] and e["page"]["alert"]["attempts"] == 3 and e["page"]["alert"]["error"] == ERROR
          and [(x["kind"], x["ticket"], x["standing"]) for x in e["events"]] == [("director_capacity", "", True)],
          f"one alert, as the phone bridge's event and the board page's banner, and none to the Director's pane: {e}")
    c = got["cleared"]
    check(c["page"] == {"alert": None, "open": None} and c["episodes"][0]["cleared"]
          and [x["standing"] for x in c["events"]] == [False],
          f"a later turn that ends normally clears the alert: {c}")
    for case, closed, failures in (("draft", "superseded", 1), ("own", "superseded", 2), ("runtime", "runtime_changed", 1),
                                   ("pane", "runtime_changed", 1)):
        g = got[case]
        check(g["typed"] == [] and g["episodes"] == [{"failures": failures, "attempts": [], "ticket": None,
                                                      "closed": closed, "alert": False, "cleared": False}],
              f"{case}: the board stops ({closed}) and types nothing: {g}")
    h = got["person"]
    # The retry's own failure may or may not have been seen before the person's turn began: 2 or 3 failures.
    check(h["typed"] == [RETRY.format("", 1)] and h["later"] == [] and h["notices"] == []
          and [{**e, "failures": min(e["failures"], 3)} for e in h["episodes"]] in (
              [{"failures": n, "attempts": ["sent"], "ticket": None, "closed": "superseded", "alert": False,
                "cleared": False}] for n in (2, 3)),
          f"a person's turn right after a typed retry failed is theirs: the board stops, no more retries, no alert: {h}")
    n = got["notice during"]
    check(any(m.startswith("PGU-913 -- Another entered Analysis") for m in n["notices"])
          and n["episodes"] == [{"failures": 2, "attempts": ["sent"], "ticket": None, "closed": "recovered",
                                 "alert": False, "cleared": False}],
          f"a board notice's failed turn during an episode is the board's: it carries on and recovers: {n}")
    check(all(v == {"typed": [], "new": False} for v in got["others"].values()) and len(got["others"]) == 6,
          f"quota, a quoted reply, authentication, a refusal, a missing model, a pane that is not Codex: not this "
          f"error: {got['others']}")


def functions(got: dict) -> None:
    f = got["functions"]
    quiet = ("no pane", "no runtime", "opened", "at 64", "queued first at 65.5", "again at 67", "lost at 150",
             "second failure at 160", "at 279", "third failure at 285", "at 584", "at 800", "after alert")
    check({k: f[k] for k in quiet} == {k: {} for k in quiet} and f["none opened"]
          and (f["at 66"].get("retry"), f["at 281"].get("retry"), f["after not_sent at 282"].get("retry"),
               f["at 586"].get("retry")) == (1, 2, 2, 3) and f["attempts after"] == ["sent"]
          and f["at 887"].get("escalated") == f["at 66"].get("episode")
          and "an attempt is sent, send_failed or not_sent, not bogus" in f["bogus"]
          and f["person opened"] == {} and f["person at 2066"].get("retry") == 1
          and f["person's turn fails at 2085"] == f["person at 2400"] == f["person at 3000"] == {}
          and f["person episode"] == ["superseded", 2, True]
          and f["unseen at 4066"].get("retry") == 1 and f["unseen turn fails at 4070"] == {}
          and f["unseen episode"] == "superseded" and "record errors" not in f,
          f"at production's delays: no pane or runtime to tell a change by, nothing; a queued notice first; 1, 2 and "
          f"5 minutes from the failure each answers, or from the attempt when nothing came of it; a claimed attempt "
          f"is never typed again; one that typed nothing does not count; the alert after the last attempt's own wait, "
          f"once; a person's turn 13 s after a typed retry is theirs, not the retry's, and stops the board for good; "
          f"a retry whose turn was never seen claims none: {f}")
    r = got["run"]
    check(r == {"pane changed": {"returned": 0, "typed": 0, "outcomes": [["not_sent", False]]},
                "sender failed": {"returned": 0, "typed": 0, "outcomes": [["send_failed", False]]},
                "hook silent": {"returned": 1, "typed": 1, "outcomes": [["sent", False]]},
                "taken": {"returned": 1, "typed": 1, "outcomes": [["sent", True]]}, "no store": None},
          f"the pane read again just before typing: changed, nothing typed; a failed send recorded as such; a sent "
          f"retry records the turn it started, and none when the pane's hook recorded none: {r}")
    check(f["status"]["alert"]["attempts"] == 3 and f["grants"] == {
        "note_director_capacity": "ticket_board_listener", "record_director_capacity_attempt": "ticket_board_listener",
        "director_capacity_status": "ticket_board_service"},
          f"the alert counts what was typed; the listener keeps the episodes and the service reads them: {f}")
    b = got["bridge"]
    check(b == {"kinds": ["director_capacity"], "alert": {"ticket": "", "problem": "", "standing": True},
                "message": ["pgu: Director stopped", "the Director's model is at capacity and the automatic retries "
                            "failed; see the board page for what to do", 1, "Open pgu"], "default_on": True},
          f"the phone bridge takes the standing event, which names no ticket, and sends it at high priority: {b}")


def page_checks(got: dict) -> None:
    alert, cleared, down = got["runs"]
    check(got["element"] and got["included"] and alert["hidden"] is False
          and alert["text"][0] == "Director stopped: its model is at capacity"
          and f'stopped on "{ERROR}" at 2026-11-02T13:20:00+00:00, and 3 automatic retries' in alert["text"][1]
          and cleared == {"hidden": True, "text": []} and down == {"hidden": True, "text": []}
          and got["no_database"] == {"alert": None, "open": None},
          f"the board page shows the alert, hides it when none stands, and shows nothing it could not read: {got}")


def boundary_checks(got: dict) -> None:
    nobody = {"note_director_capacity": [], "record_director_capacity_attempt": [], "director_capacity_status": []}
    granted = {"note_director_capacity": ["ticket_board_listener"],
               "record_director_capacity_attempt": ["ticket_board_listener"],
               "director_capacity_status": ["ticket_board_service"]}
    m, c = got["migration"], got["control"]
    check(m == {"window": nobody, "after_rbac": granted},
          f"pgu1028 alone, before rbac.sql: no role may execute its functions; after: the listener and service: {m}")
    check(all("app" in c["window"][f] and "ops" in c["window"][f] for f in nobody) and c["after_rbac"] == granted,
          f"the control, without its revocations: pane roles may execute them until rbac.sql: {c}")


def mirrors() -> None:
    """schema.sql's last definition of each of pgu1028's functions is pgu1028's, byte for byte, unless a later
    migration redefines it."""
    body = MIGRATION.read_text().split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;\n", 1)[0]
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    at = schema.rfind(body)
    # A function a later migration redefines is that migration's to mirror (SYRD-672: user_events_after).
    from schema_function_drift import owning_migration
    heads = [f"CREATE OR REPLACE FUNCTION ticket_board.{name}(" for name in (
        "user_events_after", "note_director_capacity", "record_director_capacity_attempt", "director_capacity_status")
        if owning_migration(name).name == MIGRATION.name]
    check(at >= 0 and all(at <= schema.rfind(h) < at + len(body) for h in heads),
          "schema.sql's last copies of pgu1028's functions are pgu1028's, byte for byte")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        tree = Path(sys.argv[2])
        if sys.argv[3:] == ["--page"]:
            out = page_cases(tree)
        elif sys.argv[3:4] == ["--boundary"]:
            out = boundary(tree, Path(sys.argv[4]))
        else:
            out = scenario(tree)
        print(json.dumps(out, default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd728-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD728_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree))
        mirrors()
        if "page" in PARTS:
            page_checks(run(ROOT, "--page"))
        if "boundary" in PARTS:
            boundary_checks(run(ROOT, "--boundary", str(main_tree)))
        if PARTS & {"live", "functions"}:
            candidate(run(ROOT))
    print(f"director_capacity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
