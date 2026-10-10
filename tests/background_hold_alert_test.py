#!/usr/bin/env python3
"""SYRD-557: background work may hold a role's notices, but not without the Director hearing of it.

On otto (release 2951c5c9) a wait loop whose `pgrep -f` matched itself ran for
2 h 44 min. SYRD-538 rightly read the pane as busy with background work and
held two ticket notices behind it, and nothing told the Director that ready
work had sat undelivered for hours.

Production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
with the example workflow, the real HTTP server, the real notify listener, the
real PaneActivityGate and hook-state store, and the real pane hook's own
background-work writer. Every pass is a new listener, so every case also covers
a restart. Stood in for: tmux, the process table the gate samples, and the
/proc the registered provider identities are read from. The pathological job is
a real self-matching wait loop, which is still running when the test ends it.
The before-run is main's code and SQL, in a child built from a git archive.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "ced1bbf77c1b8c1b9a51a198db117adb9cae847a"  # main before SYRD-557
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
ALERT_ENV = "TICKET_BOARD_BACKGROUND_HOLD_ALERT_SECONDS"
ROLES = ("main", "app", "ops", "director")
#: The loop's own command line carries this, so its `pgrep -f` always finds itself.
LOOP_TAG = "syrd557-self-matching-wait"
RULE = "─" * 100
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board.peer_identity import SessionIdentity
    from temporary_cluster import temporary_cluster

    loader = importlib.machinery.SourceFileLoader("ticket_board_pane_idle_hook_557", str(root / "scripts/ticket-board-pane-idle-hook"))
    hook = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(hook)

    seen: dict = {}
    loop = None
    os.environ.pop(ALERT_ENV, None)
    with temporary_cluster(prefix="s557", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd557-state.") as tmp:
        tmp_path = Path(tmp)
        state_dir, proc = tmp_path / "state", tmp_path / "proc"
        state_dir.mkdir()
        proc.mkdir()

        def proc_entry(pid: int, ppid: int, start: int, comm: str) -> None:
            (proc / str(pid)).mkdir(exist_ok=True)
            (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {' '.join(['0'] * 17)} {start} 0 0\n")

        # Each role's registered provider, under a tmux server, and the hook
        # process that runs beneath it when a turn ends.
        proc_entry(900, 1, 10, "tmux: server")
        targets = {role: f"pgu-{role}:0.0" for role in ROLES}
        for n, role in enumerate(ROLES, start=1):
            proc_entry(4100 + n, 900, 5000 + n, "claude")
            proc_entry(4200 + n, 4100 + n, 5100 + n, "python3")

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

        store = nl.PaneHookStateStore(state_dir)
        for target in targets.values():
            store.write(target, "idle", source="claude.Stop", now=time.time() - 120)

        # The pane process tree the gate samples: a shell, the runtime under it,
        # and whatever detached children a case starts there.
        epoch = time.time() - 86_400.0
        children: dict[str, list[tuple[int, float]]] = {target: [] for target in targets.values()}

        def process_table() -> tuple:
            rows = []
            for n, role in enumerate(ROLES, start=1):
                rows += [(1000 + n, 1, 0, 1000 + n, epoch), (2000 + n, 1000 + n, 500, 1000 + n, epoch)]
                rows += [(pid, 2000 + n, 0, pid, started) for pid, started in children[targets[role]]]
            return tuple(rows)

        pane_mutations: list[list[str]] = []

        def tmux(argv, **_kwargs):
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            if target not in targets.values():
                raise AssertionError(f"no fake pane for {target!r}: {argv}")
            if argv[:2] == ["tmux", "capture-pane"]:
                lines = ["", "  the last answer, finished", "", RULE, "❯\xa0", RULE, "  ⏵⏵ status"] + [""] * 13
                return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                if "cursor_x" in fmt:
                    return subprocess.CompletedProcess(argv, 0, "2 4 20\n", "")
                if fmt == "#{pane_pid}":
                    n = ROLES.index(target[4:-4]) + 1
                    return subprocess.CompletedProcess(argv, 0, f"{1000 + n}\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            pane_mutations.append(list(argv))  # anything that could interrupt a pane
            raise AssertionError(f"unexpected tmux call {argv}")

        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=process_table,
                                   sleeper=lambda _s: None, role_runtimes={role: "claude" for role in ROLES})
        gate.proc_root = proc
        # The Director has ended a turn since this listener started, so its
        # startup latch is settled and an alert can reach it.
        store.write(targets["director"], "idle", source="claude.Stop")
        gate.role_identities = {role: SessionIdentity(4100 + n, 5000 + n) for n, role in enumerate(ROLES, start=1)}
        sends: list[tuple[str, str]] = []
        starts_a_turn: set[str] = set()
        gone: set[str] = set()  # panes that no longer exist

        def sender(target: str, message: str) -> dict:
            sends.append((target, message))
            if target in starts_a_turn:  # the notice is taken, and its turn begins
                store.write(target, "busy", source="claude.UserPromptSubmit")
            return {}

        def one_pass() -> None:
            """One iteration of the listen loop, by a listener started for it: every pass is a restart."""
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")  # the requeue delays, elapsed
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                target_exists=lambda target: target not in gone, submission_witness=lambda *_a: True, poll_seconds=0)
            listener._wait_for_notification = lambda _conn: False  # one iteration, without waiting for the next due row
            listener.listen_once(max_notifications=10)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def held(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, target_role, payload->>'kind', "
                                  "coalesce(last_error, '')) ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}' AND target_role <> 'director';"))

        def episodes(role: str):
            try:
                return json.loads(sql("SELECT coalesce(json_agg(json_build_array(reason, alerted_at IS NOT NULL, "
                                      "coalesce(clear_reason, ''), ticket_ids) ORDER BY id), '[]')::text "
                                      f"FROM ticket_board.background_hold_episodes WHERE target_role = '{role}';"))
            except AssertionError:
                return None  # main has no such table

        def backdate(role: str, minutes: int) -> None:
            """The hold has gone on this much longer than the test has."""
            try:
                sql(f"UPDATE ticket_board.background_hold_episodes SET held_since = held_since - interval '{minutes} minutes' "
                    f"WHERE target_role = '{role}' AND cleared_at IS NULL;")
            except AssertionError:
                pass  # main has nothing to backdate

        def director_heard(ticket: str) -> list:
            """What the Director was sent about this ticket, and what still waits to be."""
            waiting = json.loads(sql("SELECT coalesce(json_agg('waiting: ' || message ORDER BY id), '[]')::text "
                                     "FROM ticket_board.ticket_notification_queue "
                                     f"WHERE ticket_id = '{ticket}' AND target_role = 'director';"))
            return [m for t, m in sends if t == targets["director"] and ticket in m] + waiting

        def sent_to(target: str, ticket: str) -> int:
            return sum(1 for t, m in sends if t == target and ticket in m)

        def held_notices_sent(target: str, ticket: str) -> int:
            """The ticket's own notices sent to the target: its transition and its change notices."""
            return sum(1 for t, m in sends if t == target and m.startswith(f"{ticket} -- "))

        def withdrawn(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(busy_reason ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND target_role = 'director' AND event = 'discard';"))

        try:
            # 1. A long legitimate task. app's turn on PGU-801 ends unresolved
            #    while a background build it started is still running: the
            #    board's own turn-end generator raises the repair prompt, and
            #    SYRD-538 holds it behind the build.
            route("PGU-801", "app")
            one_pass()
            work = hook._background_work(
                {"session_id": "s557", "background_tasks": [{"id": "b1", "type": "local_bash", "status": "running"}]},
                "claude.Stop", pid=4202, proc_root=proc)
            hook._write_state(state_dir, targets["app"], "idle", source="claude.Stop", background_work=work)
            with psycopg.connect(listener_url, autocommit=True) as conn:  # the generator is the listener's alone
                conn.execute("SELECT ticket_board.notify_unresolved_turn_end(%s::jsonb, clock_timestamp(), '0 seconds')",
                             (json.dumps({"app": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}),))
            one_pass()
            one_pass()
            seen["build_held"] = [held("PGU-801"), episodes("app"), director_heard("PGU-801")]
            backdate("app", 29)
            one_pass()
            seen["build_under_threshold"] = [episodes("app"), director_heard("PGU-801")]
            backdate("app", 2)
            one_pass()
            seen["build_alerted"] = [held("PGU-801"), episodes("app"), director_heard("PGU-801")]
            for _ in range(3):
                one_pass()
            seen["build_once"] = [held("PGU-801"), episodes("app"), director_heard("PGU-801"), sent_to(targets["app"], "PGU-801")]
            # The build finishes; Claude's next turn end has nothing in flight.
            hook._write_state(state_dir, targets["app"], "idle", source="claude.Stop",
                              background_work=hook._background_work({"session_id": "s557", "background_tasks": []},
                                                                    "claude.Stop", pid=4202, proc_root=proc))
            one_pass()
            one_pass()
            seen["build_delivered"] = [held("PGU-801"), episodes("app"), director_heard("PGU-801"),
                                       sent_to(targets["app"], "PGU-801")]

            # 2. The pathological job: a wait loop whose pgrep matches itself,
            #    running detached in ops's pane, holding ops's next ticket. The
            #    threshold is configured shorter here.
            os.environ[ALERT_ENV] = "600"
            loop = subprocess.Popen(["bash", "-c", f'until ! pgrep -f "{LOOP_TAG}" >/dev/null; do sleep 0.2; done', LOOP_TAG],
                                    start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            children[targets["ops"]].append((loop.pid, time.time()))
            route("PGU-802", "ops")
            one_pass()
            # The Director adds a note to the ticket: a second notice, held by the same loop.
            fixture.post_json(base, "/api/tickets/PGU-802/actions/add_comment", {"text": "Also check the retry path."}, caller="director")
            one_pass()
            seen["loop_held"] = [held("PGU-802"), episodes("ops"), director_heard("PGU-802")]
            backdate("ops", 9)
            one_pass()
            seen["loop_under_threshold"] = [episodes("ops"), director_heard("PGU-802")]
            backdate("ops", 2)
            for _ in range(5):
                one_pass()
            seen["loop_alerted"] = [held("PGU-802"), episodes("ops"), director_heard("PGU-802"), sent_to(targets["ops"], "PGU-802"),
                                    loop.poll() is None, pane_mutations[:]]
            # Somebody looks, as the alert asks, and ends the loop. The first
            # notice goes out and starts ops's turn, so the second waits behind
            # that turn: the episode is not over while a notice it held is not out.
            loop.kill()
            loop.wait()
            children[targets["ops"]].clear()
            starts_a_turn.add(targets["ops"])
            one_pass()
            seen["loop_one_out"] = [held("PGU-802"), episodes("ops"), director_heard("PGU-802"), held_notices_sent(targets["ops"], "PGU-802")]
            starts_a_turn.clear()
            store.write(targets["ops"], "idle", source="claude.Stop")
            one_pass()
            seen["loop_delivered"] = [held("PGU-802"), episodes("ops"), director_heard("PGU-802"), held_notices_sent(targets["ops"], "PGU-802")]

            # 3. Supersession. main is held the same way, and so is the
            #    Director's own pane, so the alert waits -- and then the ticket is
            #    cancelled. The hold is over, and the Director never hears of it.
            #    The Director's own hold opens no episode: its alert would wait
            #    behind that same hold.
            children[targets["main"]].append((7001, time.time()))
            children[targets["director"]].append((7002, time.time()))
            route("PGU-803", "main")
            one_pass()
            backdate("main", 11)
            one_pass()
            seen["superseded_alert_waits"] = [held("PGU-803"), episodes("main"), director_heard("PGU-803")]
            seen["director_own_hold"] = episodes("director")
            fixture.post_json(base, "/api/tickets/PGU-803/actions/force_move", {"state": "cancelled", "assignee": "unassigned"},
                              caller="director")
            one_pass()
            children[targets["director"]].clear()
            one_pass()
            seen["superseded"] = [held("PGU-803"), episodes("main"), director_heard("PGU-803"), withdrawn("PGU-803"),
                                  sent_to(targets["main"], "PGU-803")]

            # 3b. A held notice whose pane is gone is dead-lettered: that ends
            #     the episode too.
            route("PGU-805", "main")
            one_pass()
            seen["pane_gone_held"] = [held("PGU-805"), episodes("main")]
            gone.add(targets["main"])
            one_pass()
            seen["pane_gone"] = [held("PGU-805"), episodes("main")]
            gone.clear()
            children[targets["main"]].clear()

            # 4. A hold that is not background work -- app mid-turn -- opens no
            #    episode. PGU-801 is finished first, so PGU-804 is app's active
            #    work rather than queued behind it (SYRD-568).
            fixture.post_json(base, "/api/tickets/PGU-801/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")
            store.write(targets["app"], "busy", source="claude.UserPromptSubmit")
            route("PGU-804", "app")
            one_pass()
            one_pass()
            seen["foreground_turn"] = [held("PGU-804"), episodes("app")]

            # Who may run the new functions, and a fresh schema.sql board installs the same text.
            new = ("note_background_hold(bigint, text, interval)", "background_hold_notice_left()")
            try:
                seen["grantees"] = {f: sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                                           "ELSE a.grantee::regrole::text END, '+'), '') "
                                           f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{f}'::regprocedure "
                                           "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;") for f in new}
                if os.environ.get("SYRD557_MUTATION_SKIP_COPY_PARITY") == "1":
                    raise AssertionError("copy parity skipped for mutation")
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                definition = "SELECT md5(pg_get_functiondef('ticket_board.{}'::regprocedure));"
                trigger = ("SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                           "WHERE tgname = 'background_hold_notice_left' AND NOT tgisinternal;")
                seen["fresh_equals_migrated"] = {
                    **{f: fixture.psql(fresh, definition.format(f)).strip() == sql(definition.format(f)) for f in new},
                    "trigger": fixture.psql(fresh, trigger).strip() == sql(trigger) != "",
                }
            except AssertionError as exc:  # main has none of them
                seen.setdefault("grantees", f"unavailable: {str(exc)[-120:]}")
                seen["fresh_equals_migrated"] = f"unavailable: {str(exc)[-120:]}"
        finally:
            if loop is not None and loop.poll() is None:
                loop.kill()
                loop.wait()
            server.shutdown()
            server.server_close()
    return seen


def configuration() -> dict:
    """The threshold's configuration, and which holds count as background work."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board import background_hold

    class Ledger:
        def __init__(self) -> None:
            self.executed: list = []
            self.logger = None

    class Conn:
        def __init__(self, ledger: Ledger) -> None:
            self.ledger = ledger

        def execute(self, statement, params=None):
            self.ledger.executed.append((statement, params))
            raise AssertionError("no hold that is not background work reaches the board")

    ledger = Ledger()
    for reason in ("human_composing", "hook_busy", "cursor_state_unavailable", "startup_latch_unestablished", ""):
        background_hold.note(ledger, Conn(ledger), 1, reason)

    class Logger:
        def __init__(self) -> None:
            self.warnings: list = []

        def warning(self, *args) -> None:
            self.warnings.append(args[0] % args[1:])

    failing = Ledger()
    failing.logger = Logger()
    failing._background_hold_present = True
    try:
        background_hold.note(failing, Conn(failing), 7, "pane_child_work")
        failure = failing.logger.warnings
    except Exception as exc:  # the hold must not depend on its bookkeeping
        failure = f"raised: {exc!r}"
    return {
        "failing_board": failure,
        "thresholds": {raw: background_hold.alert_after_seconds({background_hold.ALERT_AFTER_ENV: raw})
                       for raw in ("", "600", "0", "-5", "half an hour")},
        "unset": background_hold.alert_after_seconds({}),
        "other_holds_reached_the_board": ledger.executed,
    }


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
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    before_run()
    candidate(after)
    print(f"background_hold_alert_test: {CHECKS} checks ok")
    return 0


def before_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd557.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    # main: the hold is right, and silent -- for as long as it lasts.
    check(before["build_once"][0] and before["build_once"][0][0][3].startswith("owner_background_work")
          and before["build_once"][2] == [] and before["build_once"][3] == 1,
          f"before: a reminder held behind background work, and the Director never told: {before['build_once']}")
    check(before["loop_alerted"][0] and before["loop_alerted"][0][0][3] == "pane busy" and before["loop_alerted"][2] == []
          and before["loop_alerted"][4] is True,
          f"before: a ticket held behind a self-matching wait loop, and the Director never told: {before['loop_alerted']}")


def candidate(after: dict) -> None:
    # 1. A long legitimate task: held, and quiet until the threshold.
    held_build = after["build_held"]
    check(len(held_build[0]) == 1 and held_build[0][0][:3] == ["unresolved_turn_repair", "app", "unresolved_turn_repair"]
          and held_build[0][0][3].startswith("owner_background_work")
          and held_build[1] == [["owner_background_work", False, "", ["PGU-801"]]] and held_build[2] == [],
          f"the repair prompt is held behind app's background work, and an episode opened -- nobody told yet: {held_build}")
    check(after["build_under_threshold"] == [[["owner_background_work", False, "", ["PGU-801"]]], []],
          f"29 minutes in, under the default half hour: still nobody told: {after['build_under_threshold']}")
    alerted = after["build_alerted"]
    check(alerted[0] == held_build[0] and alerted[1] == [["owner_background_work", True, "", ["PGU-801"]]]
          and len(alerted[2]) == 1 and alerted[2][0].startswith("app's pane has held 1 ticket notice (PGU-801) for 31 minutes")
          and "owner_background_work" in alerted[2][0] and "Nothing was interrupted" in alerted[2][0],
          f"past it, the Director is told once, the hold untouched: {alerted}")
    once = after["build_once"]
    check(once[0] == held_build[0] and once[1] == alerted[1] and once[2] == alerted[2] and once[3] == 1,
          f"three more passes, each a restarted listener: the same episode, still one alert, the reminder never sent: {once}")
    done = after["build_delivered"]
    check(done[0] == [] and done[1] == [["owner_background_work", True, "delivered", ["PGU-801"]]] and done[3] == 2
          and len(done[2]) == 2 and done[2][0] == alerted[2][0] and "has ended: its ticket notices were delivered" in done[2][1],
          f"the build ends: the reminder is delivered, the episode cleared, and the Director told it is over: {done}")

    # 2. The pathological job: a self-matching wait loop, never interrupted.
    loop_held = after["loop_held"]
    check(loop_held[0] == [["transition", "ops", "transition", "pane busy"], ["ticket_update", "ops", "ticket_update", "pane busy"]]
          and loop_held[1] == [["pane_child_work", False, "", ["PGU-802"]]] and loop_held[2] == [],
          f"ops's new ticket, and then the Director's note on it, are held behind the loop in its pane -- one episode: {loop_held}")
    check(after["loop_under_threshold"] == [[["pane_child_work", False, "", ["PGU-802"]]], []],
          f"9 minutes in, under the configured 10: nobody told: {after['loop_under_threshold']}")
    loop_alerted = after["loop_alerted"]
    check(loop_alerted[0] == loop_held[0] and loop_alerted[1] == [["pane_child_work", True, "", ["PGU-802"]]]
          and len(loop_alerted[2]) == 1 and loop_alerted[2][0].startswith("ops's pane has held 2 ticket notices (PGU-802) for 11 minutes")
          and "pgrep matches itself" in loop_alerted[2][0] and loop_alerted[3] == 0,
          f"past it, one alert over five restarted passes, and the ticket still never sent: {loop_alerted}")
    check(loop_alerted[4] is True and loop_alerted[5] == [],
          f"and the loop is still running, nothing having touched its pane: alive={loop_alerted[4]} {loop_alerted[5]}")
    one_out = after["loop_one_out"]
    check(one_out[0] == [["ticket_update", "ops", "ticket_update", "pane busy"]] and one_out[1] == loop_alerted[1]
          and one_out[2] == loop_alerted[2] and one_out[3] == 1,
          f"the loop ends; one notice out and the other behind the turn it started: the episode is not over yet: {one_out}")
    loop_done = after["loop_delivered"]
    check(loop_done[0] == [] and loop_done[1] == [["pane_child_work", True, "delivered", ["PGU-802"]]] and loop_done[3] == 2
          and len(loop_done[2]) == 2 and "has ended: its ticket notices were delivered" in loop_done[2][1],
          f"once somebody ends the loop the ticket is delivered once and the episode cleared: {loop_done}")

    # 3. Superseded while held: the alert that had not gone yet is withdrawn.
    waits = after["superseded_alert_waits"]
    check(waits[0] == [["transition", "main", "transition", "pane busy"]] and waits[1] == [["pane_child_work", True, "", ["PGU-803"]]]
          and len(waits[2]) == 1 and waits[2][0].startswith("waiting: main's pane has held 1 ticket notice (PGU-803)"),
          f"the alert is raised while the Director is mid-turn, so it waits: {waits}")
    check(after["director_own_hold"] == [], f"the Director's own held pane opens no episode: {after['director_own_hold']}")
    superseded = after["superseded"]
    check(superseded[0] == [] and superseded[1] == [["pane_child_work", True, "superseded", ["PGU-803"]]]
          and superseded[2] == [] and superseded[3] == ["background_hold_cleared"] and superseded[4] == 0,
          f"the ticket is cancelled: the episode cleared as superseded, and the Director never told of a hold that is over: {superseded}")

    gone_held = after["pane_gone_held"]
    check(gone_held[0] == [["transition", "main", "transition", "pane busy"]]
          and gone_held[1][-1] == ["pane_child_work", False, "", ["PGU-805"]],
          f"main's next ticket is held the same way, in a new episode: {gone_held}")
    gone = after["pane_gone"]
    check(gone[0] == [["transition", "main", "transition", "tmux_target_missing"]]
          and gone[1][-1] == ["pane_child_work", False, "dead_lettered", ["PGU-805"]],
          f"its pane disappears: the notice is dead-lettered and the episode ends with it: {gone}")

    # 4. Not background work, not an episode.
    foreground = after["foreground_turn"]
    check(foreground[0] == [["transition", "app", "transition", "pane busy"]]
          and foreground[1] == [["owner_background_work", True, "delivered", ["PGU-801"]]],
          f"a notice held behind app's own turn opens no episode: {foreground}")

    config = configuration()
    check(config["thresholds"] == {"": 1800.0, "600": 600.0, "0": 0.0, "-5": 1800.0, "half an hour": 1800.0}
          and config["unset"] == 1800.0 and config["other_holds_reached_the_board"] == []
          and len(config["failing_board"]) == 1 and "notification 7" in config["failing_board"][0],
          f"half an hour unless configured, a bad value falls back, and no other hold reaches the board: {config}")

    check(after["grantees"] == {"note_background_hold(bigint, text, interval)": "ticket_board_listener",
                                "background_hold_notice_left()": ""},
          f"only the listener may note a hold; nobody may call the trigger function: {after['grantees']}")
    if os.environ.get("SYRD557_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["fresh_equals_migrated"] == {"note_background_hold(bigint, text, interval)": True,
                                                 "background_hold_notice_left()": True, "trigger": True},
              f"a schema.sql board installs exactly what the migration does: {after['fresh_equals_migrated']}")


if __name__ == "__main__":
    raise SystemExit(main())
