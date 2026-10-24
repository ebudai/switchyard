#!/usr/bin/env python3
"""SYRD-583: a completed ticket's background shell may not hold a role's later work for good.

Live syrd, 2026-10-11: a shell from completed SYRD-579 ran for 18 h under App's
Claude, ending in

    while pgrep -f "sweep.sh /home/switchyard-agent/syrd-worktrees/syrd-579-app" >/dev/null; do sleep 20; done

Its own command line carried the pattern, so it could never end. SYRD-581's
handoff waited behind it from 04:40 to 11:36 -- every deferral `pane_child_work`
at the pre-send recheck -- until the Director stopped that one process group by
hand. App's provider stayed up, and SYRD-581 went out within seconds.

Everything here is real except tmux's own answers and the send:
* a production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
  with the example workflow, its HTTP server, and its Unix socket with the
  production `ProcessRoleAuthority`;
* each pane a real process under a process named `tmux: server`, registered
  through the real `ticket-board-register-runtime`, which then execs the
  provider stand-in -- so the board, not the test, decides who is registered;
* the waiters real self-matching `pgrep -f` loops, started by the provider in a
  session of their own from a ticket worktree, exactly as a provider's tool does;
* the real notify listener, PaneActivityGate and hook-state store, sampling the
  real /proc.

The before-run is main's code and SQL, in a child built from a git archive.
"""

from __future__ import annotations

import getpass
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "0b3e2c66"  # main before SYRD-583
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
ALERT_ENV = "TICKET_BOARD_BACKGROUND_HOLD_ALERT_SECONDS"
RULE = "─" * 100
CHECKS = 0
#: How long each wait loop sleeps between searches: long enough that a whole
#: listener pass fits inside one sleep (see `settle`).
WAIT_SECONDS = 4

#: A process whose name is what the board's session lookup needs a pane's parent
#: to be, and which starts the panes it is given and reaps whatever they leave.
TMUX_SERVER = """
import json, os, signal, subprocess, sys, time
open('/proc/self/comm', 'w').write('tmux: server')
panes = [subprocess.Popen(argv, start_new_session=True) for argv in json.loads(sys.argv[1])]
open(sys.argv[2], 'w').write(json.dumps([p.pid for p in panes]))
def stop(*_):
    for p in panes:
        try: p.terminate()
        except OSError: pass
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.1)
"""

#: The provider stand-in: it starts each command it is asked to in a session of
#: its own, as a provider's tool does, and reaps what ends.
PROVIDER = """
import json, os, subprocess, sys, time
from pathlib import Path
ctl = Path(sys.argv[1])
while True:
    for request in sorted(ctl.glob('spawn-*.json')):
        job = json.loads(request.read_text())
        child = subprocess.Popen(['bash', '-c', job['command']], cwd=job['cwd'], start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        request.with_suffix('.pid').write_text(str(child.pid))
        request.unlink()
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
    time.sleep(0.05)
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def alive(pid: int, start: int | None = None) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    fields = stat[stat.rfind(")") + 2:].split()
    return fields[0] != "Z" and (start is None or int(fields[19]) == start)


def start_time(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat[stat.rfind(")") + 2:].split()[19])


def ticket_worktree(path: Path) -> Path:
    """A real ticket worktree, cut by `git worktree add` from a repository beside it: only a worktree git registered
    names a child's ticket, never a directory's name alone (SYRD-636)."""
    repo = path.parent / ".repo"
    run = lambda *argv, cwd: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv], cwd=cwd,
                                             check=True, capture_output=True)
    if not repo.exists():
        repo.mkdir()
        run("init", "-q", cwd=repo)
        run("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    run("worktree", "add", "-q", "-b", path.name, str(path), cwd=repo)
    return path


def scenario(root: Path, full: bool) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board.board_notifications import TicketBoardEventHub
    from scripts.ticket_board.local_peer_authority import ProcessRoleAuthority
    from scripts.ticket_board.server import TicketBoardUnixServer
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    os.environ.pop(ALERT_ENV, None)
    tmux_server = None
    jobs: list[int] = []
    with temporary_cluster(prefix="s583", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd583.") as tmp:
        tmp_path = Path(tmp)
        state_dir, sessions, scratch = tmp_path / "state", tmp_path / "sessions", tmp_path / "scratch"
        worktrees = tmp_path / "worktrees"
        for path in (state_dir, sessions, scratch, worktrees):
            path.mkdir()
        (tmp_path / "provider.py").write_text(PROVIDER)

        admin = build_board(fixture, cluster, root, "board")
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=service)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        sock = tmp_path / "board.sock"
        unix = TicketBoardUnixServer(sock, app, events=TicketBoardEventHub(app), director_notifier=fixture.QuietNotifier(),
                                     role_authority=ProcessRoleAuthority(app, getpass.getuser()))
        threading.Thread(target=unix.serve_forever, daemon=True).start()
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        # app's and the Director's panes: each registers itself over the board's
        # socket, then becomes its provider.
        roles = ("app", "director")
        targets = {role: f"pgu-{role}:0.0" for role in roles}
        for role in roles:
            (tmp_path / f"ctl-{role}").mkdir()
            (worktrees / f"{role}-home").mkdir()
        panes = [[sys.executable, str(root / "scripts/ticket-board-register-runtime"), "--socket", str(sock), "--role", role,
                  "--runtime", "claude", "--target", targets[role], "--worktree", str(worktrees / f"{role}-home"),
                  "--session-dir", str(sessions), "--", sys.executable, str(tmp_path / "provider.py"),
                  str(tmp_path / f"ctl-{role}")] for role in roles]
        tmux_server = subprocess.Popen([sys.executable, "-c", TMUX_SERVER, json.dumps(panes), str(tmp_path / "panes.json")],
                                       stdin=subprocess.DEVNULL)

        def registered() -> dict:
            return json.loads(sql("SELECT coalesce(json_object_agg(role, json_build_array(process_pid, process_start_time, "
                                  "generation)), '{}')::text FROM ticket_board.role_runtime_assignments;"))

        deadline = time.time() + 30
        while (len(registered()) < 2 or not (tmp_path / "panes.json").exists()) and time.time() < deadline:
            time.sleep(0.1)
        pane_pids = dict(zip(roles, json.loads((tmp_path / "panes.json").read_text())))
        seen["registered"] = registered()
        provider = {role: (pid, start_time(pid)) for role, pid in pane_pids.items()}
        seen["providers_registered_as_themselves"] = all(
            seen["registered"].get(role, [0, 0])[:2] == list(provider[role]) for role in roles)

        def spawn(role: str, cwd: Path, command: str) -> int:
            """The provider's tool starts a command, in a session of its own."""
            ctl = tmp_path / f"ctl-{role}"
            n = len(list(ctl.glob("spawn-*")))
            request = ctl / f"spawn-{n:03d}.json"
            request.write_text(json.dumps({"cwd": str(cwd), "command": command}))
            for _ in range(200):
                if request.with_suffix(".pid").exists():
                    pid = int(request.with_suffix(".pid").read_text())
                    jobs.append(pid)
                    return pid
                time.sleep(0.05)
            raise AssertionError("the provider did not start the job")

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
                    return subprocess.CompletedProcess(argv, 0, f"{pane_pids[target[4:-4]]}\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")  # anything that could interrupt a pane

        store = nl.PaneHookStateStore(state_dir)
        sends: list[tuple[str, str]] = []
        clock = [10_000.0]

        def sender(target: str, message: str) -> dict:
            sends.append((target, message))
            store.write(target, "busy", source="claude.UserPromptSubmit")  # the notice starts a turn
            return {}

        listener = None

        def restart() -> None:
            """A new listener and gate: nothing remembered but what the board keeps."""
            nonlocal listener
            gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                       capture_pane_runner=tmux, pane_pid_runner=tmux, sleeper=time.sleep,
                                       role_runtimes={role: "claude" for role in roles})
            store.write(targets["director"], "idle", source="claude.Stop")  # its startup latch settled
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                target_exists=lambda target: target in targets.values(), submission_witness=lambda *_a: True,
                poll_seconds=0, monotonic=lambda: clock[0])
            listener._wait_for_notification = lambda _conn: False

        def sleeps_of(waiter: int) -> set[int]:
            found = set()
            for entry in Path("/proc").iterdir():
                try:
                    stat = (entry / "stat").read_text() if entry.name.isdigit() else ""
                except OSError:
                    continue
                if stat and "(sleep)" in stat and int(stat[stat.rfind(")") + 2:].split()[1]) == waiter:
                    found.add(int(entry.name))
            return found

        def settle(waiter: int) -> None:
            """Start a pass just after the waiter starts its next `sleep`, so the whole pass sees that one.

            Between two iterations the loop's shell is briefly alone, and a probe
            landing there sees nothing but an old shell, and calls it stale
            (measured: 0.81% of samples of a 1 s loop). Timing each pass to the
            loop keeps both trees' verdicts what they are the rest of the time. The sleep is one born after this call, so after every
            turn end the test has written.
            """
            before = sleeps_of(waiter)
            deadline = time.time() + 3 * WAIT_SECONDS
            while time.time() < deadline:
                if sleeps_of(waiter) - before:
                    time.sleep(0.05)
                    return
                time.sleep(0.02)
            raise AssertionError(f"waiter {waiter} started no new sleep")

        def one_pass(waiter: int | None = None) -> None:
            if waiter is not None and alive(waiter):
                settle(waiter)
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")  # the requeue delays, elapsed
            listener.listen_once(max_notifications=10)

        def turn_ends(role: str) -> None:
            """The runtime's own trusted turn end, with time either side for the kernel's start times to tell."""
            time.sleep(2.5)
            store.write(targets[role], "idle", source="claude.Stop")
            time.sleep(1.5)

        def idle(role: str) -> None:
            """The turn a notice started ends. Start times come from the kernel's boot time, which is whole
            seconds, so what starts in the next second could date before this; nothing is asked until then."""
            store.write(targets[role], "idle", source="claude.Stop")
            time.sleep(1.5)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def finish(ticket: str, state: str = "done", assignee: str = "unassigned") -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": state, "assignee": assignee},
                              caller="director")

        def comment(ticket: str, text: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/add_comment", {"text": text}, caller="director")

        def held(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, coalesce(last_error, '')) ORDER BY id), "
                                  "'[]')::text FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}' AND target_role = 'app' AND kind IN ('transition', 'ticket_update');"))

        def sent_to_app(ticket: str) -> list:
            return [m for t, m in sends if t == targets["app"] and m.startswith(f"{ticket} -- ")]

        def director_heard() -> list:
            """What the Director was sent, or still waits to be, about a hold or a child -- nothing else."""
            waiting = json.loads(sql("SELECT coalesce(json_agg('waiting: ' || message ORDER BY id), '[]')::text "
                                     "FROM ticket_board.ticket_notification_queue WHERE target_role = 'director' "
                                     "AND payload->>'kind' IN ('background_hold', 'background_child');"))
            return [m for t, m in sends if t == targets["director"] and ("pane has held" in m or "went out past" in m
                                                                           or "hold on" in m)] + waiting

        def escalations(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(busy_reason, detail->'holders') ORDER BY id), "
                                  "'[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND event = 'gate_escalate';"))

        def defer_reasons(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(DISTINCT busy_reason), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND event = 'gate_defer';"))

        def story(ticket: str) -> list:
            """What happened to the ticket's notices to app, in order: a failing check's own evidence."""
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(to_char(ts, 'HH24:MI:SS.MS'), kind, event, "
                                  "coalesce(busy_reason, ''), coalesce(detail->>'phase', '')) ORDER BY id), '[]')::text "
                                  "FROM ticket_board.notification_trace WHERE ticket_id = '" + ticket + "' AND target_role = 'app' "
                                  "AND event NOT IN ('claim', 'listener_claim', 'listener_ack');"))

        def backdate(hours: float) -> None:
            clock[0] += hours * 3600
            try:
                sql(f"UPDATE ticket_board.background_hold_episodes SET held_since = held_since - interval '{hours} hours' "
                    "WHERE cleared_at IS NULL;")
            except AssertionError:
                pass

        try:
            restart()
            store.write(targets["app"], "idle", source="claude.Stop")
            # 1. PGU-579 goes to app. Its turn starts the sweep's waiter from the
            #    ticket's worktree -- a loop whose pgrep matches itself -- and ends.
            route("PGU-579", "app")
            one_pass()
            seen["first_delivered"] = sent_to_app("PGU-579")
            wt579 = ticket_worktree(worktrees / "pgu-579-app")
            # Started from a directory whose name looks like an id the board does
            # not have; only the command names the ticket's worktree.
            decoy = worktrees / "node-18-scratch"
            decoy.mkdir()
            waiter = spawn("app", decoy, f'while pgrep -f "sweep.sh {wt579}" >/dev/null; do sleep {WAIT_SECONDS}; done')
            waiter_start = start_time(waiter)
            turn_ends("app")
            # 2. PGU-579 is finished, still assigned to app, so app is told: the
            #    first notice the waiter meets is its own ticket's. Then PGU-581
            #    goes to app.
            finish("PGU-579", assignee="app")
            one_pass(waiter)
            seen["done_notice"] = {"held": held("PGU-579"), "sent": sent_to_app("PGU-579"), "director": director_heard()}
            idle("app")  # whatever that notice started is over
            route("PGU-581", "app")
            for _ in range(3):
                one_pass(waiter)
            seen["after_three_passes"] = {"held": held("PGU-581"), "sent": sent_to_app("PGU-581"), "director": director_heard(),
                                          "escalations": escalations("PGU-579") + escalations("PGU-581"),
                                          "defers": defer_reasons("PGU-581")}
            # 3. Eighteen hours of the listener trying.
            backdate(18)
            for _ in range(2):
                one_pass(waiter)
            seen["after_eighteen_hours"] = {"held": held("PGU-581"), "sent": sent_to_app("PGU-581"), "director": director_heard(),
                                            "waiter_alive": alive(waiter, waiter_start)}
            # 4. Whatever app was doing ends; the Director adds a note. A
            #    restarted listener and gate decide it.
            turn_ends("app")
            comment("PGU-581", "Also check the retry path.")
            restart()
            one_pass(waiter)
            seen["note"] = {"held": held("PGU-581"), "sent": sent_to_app("PGU-581"), "director": director_heard(),
                            "waiter_alive": alive(waiter, waiter_start),
                            "providers_alive": all(alive(*provider[role]) for role in roles)}
            # 5. The Director's targeted recovery: only the waiter's process group.
            os.killpg(waiter, signal.SIGTERM)
            time.sleep(0.5)
            for _ in range(4):
                one_pass(waiter)
                store.write(targets["app"], "idle", source="claude.Stop")  # the turn each notice starts ends
            seen["recovered"] = {"held": held("PGU-579") + held("PGU-581"), "sent": sent_to_app("PGU-581"),
                                 "done_sent": sent_to_app("PGU-579"),
                                 "waiter_alive": alive(waiter, waiter_start),
                                 "providers_alive": all(alive(*provider[role]) for role in roles),
                                 "registered": registered()}
            if not full:
                return seen

            # 6. A finished turn's waiter whose ticket nobody can tell: bounded.
            turn_ends("app")
            finish("PGU-581")
            unowned = spawn("app", scratch, f'while pgrep -f "syrd583-unowned-wait" >/dev/null; do sleep {WAIT_SECONDS}; done '
                                            '# syrd583-unowned-wait')
            unowned_start = start_time(unowned)
            turn_ends("app")
            route("PGU-590", "app")
            one_pass(unowned)
            clock[0] += 14 * 60
            one_pass(unowned)
            seen["bound_inside"] = {"held": held("PGU-590"), "sent": sent_to_app("PGU-590"), "director": director_heard(),
                                    "defers": defer_reasons("PGU-590"), "story": story("PGU-590")}
            clock[0] += 2 * 60
            one_pass(unowned)
            seen["bound_past"] = {"held": held("PGU-590"), "sent": sent_to_app("PGU-590"), "director": director_heard(),
                                  "escalations": escalations("PGU-590"), "unowned_alive": alive(unowned, unowned_start)}

            # 7. The same waiter holds a later notice; the Director acts on the
            #    hold alert, running exactly the stop it names.
            os.environ[ALERT_ENV] = "600"
            turn_ends("app")
            comment("PGU-590", "Please also cover the restart case.")
            one_pass(unowned)
            backdate(11 / 60)
            one_pass(unowned)
            alert = [m for m in director_heard() if "has held 1 ticket notice (PGU-590)" in m]
            seen["alert"] = alert
            stop = re.findall(r"`(kill -TERM -- -\d+)`", alert[-1]) if alert else []
            seen["stop"] = stop
            if stop:
                subprocess.run(["bash", "-c", stop[0]], check=True)
            time.sleep(0.5)
            one_pass(unowned)
            one_pass(unowned)
            seen["after_stop"] = {"held": held("PGU-590"), "sent": sent_to_app("PGU-590"), "unowned_alive": alive(unowned, unowned_start),
                                  "providers_alive": all(alive(*provider[role]) for role in roles), "registered": registered(),
                                  "episodes": json.loads(sql("SELECT coalesce(json_agg(json_build_array(reason, clear_reason) "
                                                             "ORDER BY id), '[]')::text FROM ticket_board.background_hold_episodes "
                                                             "WHERE target_role = 'app';"))}

            # 8. Work for the ticket app still owns is not a lapsed lease: a build
            #    the last turn left running keeps app's pane, untouched.
            wt590 = ticket_worktree(worktrees / "pgu-590-app")
            build = spawn("app", wt590, "while :; do :; done")
            build_start = start_time(build)
            wt581 = ticket_worktree(worktrees / "pgu-581-app")
            lapsed_waiter = spawn("app", wt581, f'while pgrep -f "syrd583-lapsed-wait" >/dev/null; do sleep {WAIT_SECONDS}; done '
                                                '# syrd583-lapsed-wait')
            turn_ends("app")
            comment("PGU-590", "One more: the docs.")
            told_before = len(director_heard())
            for _ in range(2):
                one_pass(lapsed_waiter)
            seen["own_work"] = {"held": held("PGU-590"), "defers": defer_reasons("PGU-590"),
                                "told": director_heard()[told_before:], "build_alive": alive(build, build_start),
                                "escalations": escalations("PGU-590"), "lapsed_alive": alive(lapsed_waiter)}

            # 9. PGU-590 is handed to main, still in progress: its build, known
            #    only by the directory it runs in, is a lapsed lease too, and
            #    app's next ticket goes out.
            idle("app")
            finish("PGU-590", state="in_progress", assignee="main")
            told_before = len(director_heard())
            route("PGU-595", "app")
            one_pass(lapsed_waiter)
            seen["build_lapsed"] = {"held": held("PGU-595"), "sent": sent_to_app("PGU-595"),
                                    "told": director_heard()[told_before:], "escalations": escalations("PGU-595"),
                                    "build_alive": alive(build, build_start), "lapsed_alive": alive(lapsed_waiter)}
            seen["background_child_alerts"] = json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(target_role, child, ticket_id) ORDER BY alerted_at), '[]')::text "
                "FROM ticket_board.background_child_alerts;"))
            new = "note_background_child(bigint, text, text)"
            seen["grantees"] = sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                                   "ELSE a.grantee::regrole::text END, '+'), '') "
                                   f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{new}'::regprocedure "
                                   "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;")
            if os.environ.get("SYRD583_MUTATION_SKIP_COPY_PARITY") != "1":
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                definition = f"SELECT md5(pg_get_functiondef('ticket_board.{new}'::regprocedure));"
                columns = ("SELECT string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
                           "FROM information_schema.columns WHERE table_schema = 'ticket_board' "
                           "AND table_name = 'background_child_alerts';")
                seen["fresh_equals_migrated"] = [fixture.psql(fresh, definition).strip() == sql(definition),
                                                 fixture.psql(fresh, columns).strip() == sql(columns) != ""]
        finally:
            for pid in jobs:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except OSError:
                    pass
            if tmux_server is not None:
                tmux_server.terminate()
                tmux_server.wait(timeout=10)
            unix.shutdown()
            unix.server_close()
            server.shutdown()
            server.server_close()
    return seen


def units() -> None:
    """The parts with no process or board behind them."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board import background_child as bc

    terminal = {"done", "cancelled"}
    check([bc.lapsed(origin, "app", terminal) for origin in (
        {"id": "X-1", "state": "done", "assignee": "app"}, {"id": "X-2", "state": "audit", "assignee": "audit"},
        {"id": "X-3", "state": "in_progress", "assignee": "app"}, None)] == [True, True, False, False],
          "a lease lapses when its ticket is finished, even still assigned, or another role's; never when unknown")
    commands = {
        "live": 'zsh -c eval \'while pgrep -f "sweep.sh /w/syrd-579-app" >/dev/null; do sleep 20; done\'',
        "otto": 'bash -c until ! pgrep -f "x-wait" >/dev/null; do sleep 1; done',
        "options": "sh -c while pgrep -af mutate_run; do sleep 5; done",
        "bracketed": 'bash -c while pgrep -f "[s]weep.sh" >/dev/null; do sleep 20; done',
        "by name": "bash -c while pgrep sweep >/dev/null; do sleep 1; done",
        "exact name": "bash -c while pgrep -x bash >/dev/null; do sleep 1; done",
        "variable": 'sh -c while pgrep -f "$pat"; do sleep 5; done',
        "work": "python3 -m pytest tests",
    }
    flagged = {name: bc.Holder(1, 1, 1, 0.0, command).self_matching for name, command in commands.items()}
    check(flagged == {"live": True, "otto": True, "options": True, "bracketed": False, "by name": False,
                      "exact name": False, "variable": False, "work": False}, f"only a pgrep -f that finds itself is called endless: {flagged}")
    holder = bc.Holder(10, 10, 10, 1.0, "bash -c x", "/w/syrd-579-app")
    pane = bc.Holder(4, 4, 12, 1.0, "helper", "/w", detached=False)
    check("`kill -TERM -- -10`" in holder.recovery(7) and "never the provider (pid 7)" in holder.recovery(7)
          and "`kill -TERM 12`" in pane.recovery(7) and "-- -4" not in pane.recovery(7),
          f"the stop is its own process group, or for a child in the pane's group the process alone: {pane.recovery(7)}")
    with tempfile.TemporaryDirectory(prefix="syrd583u.") as tmp:
        worktree = ticket_worktree(Path(tmp) / "syrd-579-app")
        named = bc.Holder(10, 10, 10, 1.0, "bash -c x", str(worktree)).candidate_tickets()
        by_command = bc.Holder(10, 10, 10, 1.0, f"bash -c make -C {worktree}/tests", "/tmp/syrd-12-scratch").candidate_tickets()
        loose = bc.Holder(1, 1, 1, 0.0, "x", "/tmp/claude-1006/pgu-12-a").candidate_tickets()
    check(named == by_command == ["SYRD-579"] and loose == [],
          f"ids are read from a worktree the directory is in, then one the command names; never from a name alone: "
          f"{named} {by_command} {loose}")


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
        print(json.dumps(scenario(Path(sys.argv[2]), full=False), default=str))
        return 0
    after = json.loads(json.dumps(scenario(ROOT, full=True), default=str))
    if os.environ.get("SYRD583_MUTATION_SKIP_BEFORE") != "1":
        before_run()
    candidate(after)
    units()
    print(f"background_child_lease_test: {CHECKS} checks ok")
    return 0


def before_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd583.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(before["providers_registered_as_themselves"] and before["first_delivered"],
          f"before: the panes register themselves and PGU-579 reaches app: {before['registered']} {before['first_delivered']}")
    done = before["done_notice"]
    check(done["held"] == [["transition", "pane busy"]] and len(done["sent"]) == 1,
          f"before: PGU-579's own done notice is held behind its waiter: {done}")
    three = before["after_three_passes"]
    check(three["held"] == [["transition", "pane busy"]] and three["sent"] == [] and three["defers"] == ["pane_child_work"],
          f"before: PGU-581 is held behind finished PGU-579's waiter, read as app's current work: {three}")
    eighteen = before["after_eighteen_hours"]
    check(eighteen["held"] == [["transition", "pane busy"]] and eighteen["sent"] == [] and eighteen["waiter_alive"]
          and not any("pid" in message for message in eighteen["director"]),
          f"before: eighteen hours later it is still held, and nothing tells the Director which process: {eighteen}")
    note = before["note"]
    check([kind for kind, _ in note["held"]] == ["transition", "ticket_update"] and note["sent"] == [],
          f"before: a restarted listener holds the Director's note too: {note}")
    recovered = before["recovered"]
    check(recovered["held"] == [] and len(recovered["sent"]) == 2 and len(recovered["done_sent"]) == 2
          and not recovered["waiter_alive"]
          and recovered["providers_alive"] and recovered["registered"] == before["registered"],
          f"before: only the Director stopping that process group by hand releases them: {recovered}")


def candidate(after: dict) -> None:
    check(after["providers_registered_as_themselves"] and after["first_delivered"],
          f"each pane registered itself, as its own provider process, and PGU-579 reached app: {after['registered']}")
    done = after["done_notice"]
    check(done["held"] == [] and len(done["sent"]) == 2 and "entered Done" in done["sent"][1],
          f"PGU-579's own done notice goes out past its waiter at once: {done}")
    three = after["after_three_passes"]
    escalated = three["escalations"]
    check(three["held"] == [] and len(three["sent"]) == 1 and "entered Implementation" in three["sent"][0],
          f"PGU-581 is delivered at once, once, past the finished ticket's waiter: {three}")
    check(len(escalated) == 2 and {e[0] for e in escalated} == {"completed_ticket_child"} and escalated[0][1] == escalated[1][1]
          and len(escalated[0][1]) == 1
          and escalated[0][1][0]["ticket"] == {"id": "PGU-579", "state": "done", "assignee": "app"}
          and escalated[0][1][0]["finished_turn"] is True and escalated[0][1][0]["cwd"].endswith("/node-18-scratch")
          # What is recorded of its command is a summary (SYRD-616); its ticket and its endless wait were read off the
          # raw command line, which is never kept.
          and escalated[0][1][0]["command"] == "bash -c [redacted]" and escalated[0][1][0]["endless"] is True,
          f"both deliveries are recorded against the child they went past, and the ticket its command names: {escalated}")
    told = [m for m in three["director"] if "went out past background work" in m]
    holder = escalated[0][1][0] if escalated else {}
    check(len(told) == 1 and told[0].startswith("app's PGU-579 notice went out past background work a finished turn left running")
          and done["director"] == three["director"]
          and f"pid {holder.get('pid')} (process group {holder.get('pgid')})" in told[0]
          and "started for PGU-579, which is now done (assigned to app)" in told[0] and "running for " in told[0]
          and "can never end by itself" in told[0] and f"`kill -TERM -- -{holder.get('pgid')}`" in told[0]
          and "never the provider" in told[0] and "Nothing was stopped" in told[0],
          f"the Director is told once: which process, how long, whose ticket, that it never ends, and the one stop: {told}")
    eighteen = after["after_eighteen_hours"]
    check(eighteen["held"] == [] and eighteen["sent"] == three["sent"] and eighteen["waiter_alive"]
          and eighteen["director"] == three["director"],
          f"eighteen hours on: nothing held, nothing sent twice, nothing stopped, nobody told again: {eighteen}")
    note = after["note"]
    check(note["held"] == [] and len(note["sent"]) == 2 and "new comment" in note["sent"][1]
          and note["director"] == three["director"] and note["waiter_alive"] and note["providers_alive"],
          f"a restarted listener lets the Director's note past the same child, and tells nobody twice: {note}")
    recovered = after["recovered"]
    check(not recovered["waiter_alive"] and recovered["providers_alive"] and recovered["registered"] == after["registered"]
          and recovered["sent"] == note["sent"] and recovered["held"] == [],
          f"stopping only the waiter's group leaves both providers running and registered as they were: {recovered}")

    inside = after["bound_inside"]
    check(inside["held"] == [["transition", "pane busy"]] and inside["sent"] == [] and inside["defers"] == ["prior_turn_child_work"],
          f"a finished turn's waiter whose ticket is unknown holds PGU-590 -- named as a finished turn's, not app's work: {inside}")
    past = after["bound_past"]
    told = [m for m in past["director"] if "PGU-590 notice went out past" in m]
    check(past["held"] == [] and len(past["sent"]) == 1 and past["unowned_alive"]
          and [e[0] for e in past["escalations"]] == ["orphaned_prior_turn_waiter"]
          and len(told) == 1 and "its ticket is not known" in told[0] and "can never end by itself" in told[0],
          f"past SYRD-212's fifteen minutes it goes out once, the Director is told what holds it, and nothing is stopped: {past}")

    alert = after["alert"]
    check(len(alert) == 1 and "for 11 minutes: behind background work under its pane." in alert[0]
          and "its ticket is not known" in alert[0] and "can never end by itself" in alert[0]
          and len(after["stop"]) == 1 and after["stop"][0] in alert[0],
          f"SYRD-557's hold alert names the child and the one stop, which is a command that runs: {alert} {after['stop']}")
    stopped = after["after_stop"]
    check(stopped["held"] == [] and len(stopped["sent"]) == 2 and "new comment" in stopped["sent"][1]
          and not stopped["unowned_alive"] and stopped["providers_alive"] and stopped["registered"] == after["registered"]
          and stopped["episodes"][-1] == ["prior_turn_child_work", "delivered"],
          f"the stop ends only the waiter: the held note is delivered once, the providers stay registered: {stopped}")

    own = after["own_work"]
    check(own["held"] == [["ticket_update", "pane busy"]] and "prior_turn_child_work" in own["defers"]
          and own["told"] == [] and own["build_alive"] and own["escalations"] == past["escalations"],
          f"a build for the ticket app still owns keeps its pane, untouched, and is no lapsed lease: {own}")
    check(own["lapsed_alive"], f"and a finished ticket's waiter beside that build does not release it: {own}")
    lapsed = after["build_lapsed"]
    waiter_told = [m for m in lapsed["told"] if "started for PGU-581, which is now done (unassigned)" in m]
    build_told = [m for m in lapsed["told"] if "started for PGU-590, which is now in_progress (assigned to main)" in m]
    check(lapsed["held"] == [] and len(lapsed["sent"]) == 1 and [e[0] for e in lapsed["escalations"]] == ["completed_ticket_child"]
          and len(lapsed["told"]) == 2 and len(waiter_told) == len(build_told) == 1 and "can never end by itself" in waiter_told[0]
          and "`bash -c [redacted]`" in build_told[0] and "can never end by itself" not in build_told[0]
          and lapsed["build_alive"] and lapsed["lapsed_alive"],
          f"once PGU-590 is main's, its build -- known only by its directory -- lets PGU-595 go at once; each child named once: {lapsed}")
    check([row[2] for row in after["background_child_alerts"]] == ["PGU-579", "PGU-590", "PGU-595", "PGU-595"],
          f"one record per child, however many notices went past it: {after['background_child_alerts']}")
    check(after["grantees"] == "ticket_board_listener", f"only the listener may note a child: {after['grantees']}")
    if os.environ.get("SYRD583_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["fresh_equals_migrated"] == [True, True],
              f"a schema.sql board installs exactly what the migration does: {after['fresh_equals_migrated']}")


if __name__ == "__main__":
    raise SystemExit(main())
