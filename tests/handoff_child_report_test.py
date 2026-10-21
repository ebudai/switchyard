#!/usr/bin/env python3
"""SYRD-636: background work an earlier ticket left running is reported to the Director at the next hand-over.

Live syrd, 2026-10-20/21: two QEMU VMs App started for SYRD-627/629 -- from the
VM kit's directory, `~/syrd-248-vm`, as `./resume-vm.sh syrd627 8 16384` -- ran
on under App's Claude through the hand-over of SYRD-624 and overnight, 2 x 8
vCPU and 2 x 16 GiB. They held no notice: a finished turn's sessions that start
nothing new are `stale_prior_turn_child_work`, which holds nothing, so SYRD-583
never described them and nobody told the Director, who found them in the pane's
footer and stopped them by hand. Had SYRD-583 described them, it would have
said they were SYRD-248's: the kit's directory is named like a ticket's
worktree, but it is not one.

Everything here is real except tmux's own answers and the send:
* a production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
  with the example workflow, its HTTP server, and its Unix socket with the
  production `ProcessRoleAuthority`;
* each pane a real process under a process named `tmux: server`, registered
  through the real `ticket-board-register-runtime`, which then execs the
  provider stand-in, which starts each command in a session of its own, as a
  provider's tool does;
* ticket worktrees are real `git worktree add` worktrees; the VM kit is a plain
  directory whose name looks like one;
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
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9af9af13"  # main before SYRD-636
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
RULE = "─" * 100
CHECKS = 0
HANDED = "was handed"  # what a hand-over report to the Director says

#: The VM kit's launcher: a long-lived process with a disk image named like the
#: ticket it was for, minus the hyphen -- exactly what nobody can read a ticket from.
RESUME_VM = """#!/bin/bash
exec "{python}" -c 'import time; time.sleep(86400)' -name "$1" -smp "$2" -m "$3" -drive "file=$1.qcow2,if=virtio"
"""
#: A long job that uses no CPU and starts nothing new, as a VM or a parked build looks between samples. Its
#: shell stays its session's leader, as a provider's compound command's does, so the label stays visible.
SLEEPER = "{python} -c 'import time; time.sleep(86400)'; exit 0 # {label}"
#: How App started the VMs: from the kit's directory, its output to a log, the shell waiting on it.
VM = "./resume-vm.sh {name} 8 16384 > {name}.qemu.log 2>&1 < /dev/null; exit 0"

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
if len(sys.argv) > 2:  # a daemon of its own, in a session of its own, as Codex starts its app-server (SYRD-589)
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(86400)', sys.argv[2]], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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


def git(*argv: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *argv],
                   cwd=cwd, check=True, capture_output=True)


def scenario(root: Path, full: bool) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import background_child, notify_listener as nl
    from scripts.ticket_board.board_notifications import TicketBoardEventHub
    from scripts.ticket_board.local_peer_authority import ProcessRoleAuthority
    from scripts.ticket_board.server import TicketBoardUnixServer
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    tmux_server = None
    jobs: list[int] = []
    with temporary_cluster(prefix="s636", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd636.") as tmp:
        tmp_path = Path(tmp)
        state_dir, sessions, scratch, homes = (tmp_path / name for name in ("state", "sessions", "scratch", "homes"))
        for path in (state_dir, sessions, scratch, homes):
            path.mkdir()
        (tmp_path / "provider.py").write_text(PROVIDER)
        # The project's repository and two real ticket worktrees cut from it.
        repo, worktrees = tmp_path / "repo", tmp_path / "worktrees"
        repo.mkdir()
        worktrees.mkdir()
        git("init", "-q", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        for name in ("pgu-627-app", "pgu-641-app"):
            git("worktree", "add", "-q", "-b", name, str(worktrees / name), cwd=repo)
        # The VM kit: named like PGU-248's worktree -- the ticket that built it -- but a plain directory.
        kit = tmp_path / "pgu-248-vm"
        kit.mkdir()
        (kit / "resume-vm.sh").write_text(RESUME_VM.format(python=sys.executable))
        (kit / "resume-vm.sh").chmod(0o755)

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

        roles = ("app", "director")
        targets = {role: f"pgu-{role}:0.0" for role in roles}
        for role in roles:
            (tmp_path / f"ctl-{role}").mkdir()
            (homes / f"{role}-home").mkdir()
        panes = [[sys.executable, str(root / "scripts/ticket-board-register-runtime"), "--socket", str(sock), "--role", role,
                  "--runtime", "claude", "--target", targets[role], "--worktree", str(homes / f"{role}-home"),
                  "--session-dir", str(sessions), "--", sys.executable, str(tmp_path / "provider.py"),
                  str(tmp_path / f"ctl-{role}")] + (["app-server-daemon"] if role == "app" else []) for role in roles]
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
        registered_at = time.time()  # every provider started before this

        def daemon_of(parent: int) -> int:
            """The daemon App's provider started at its own start."""
            for entry in Path("/proc").iterdir():
                try:
                    argv = (entry / "cmdline").read_bytes().split(b"\0") if entry.name.isdigit() else []
                    ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1]) if argv else 0
                except (OSError, IndexError, ValueError):
                    continue
                if ppid == parent and argv[1:2] == [b"-c"] and b"app-server-daemon" in argv:
                    return int(entry.name)
            return 0

        def spawn(role: str, cwd: Path, command: str) -> tuple[int, int]:
            """The provider's tool starts a command, in a session of its own."""
            ctl = tmp_path / f"ctl-{role}"
            n = len(list(ctl.glob("spawn-*")))
            request = ctl / f"spawn-{n:03d}.json"
            request.write_text(json.dumps({"cwd": str(cwd), "command": command}))
            for _ in range(200):
                if request.with_suffix(".pid").exists():
                    pid = int(request.with_suffix(".pid").read_text())
                    jobs.append(pid)
                    time.sleep(0.2)  # its exec has happened
                    return pid, start_time(pid)
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
                poll_seconds=0)
            listener._wait_for_notification = lambda _conn: False

        def one_pass() -> None:
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")  # the requeue delays, elapsed
            listener.listen_once(max_notifications=listener.delivered_count + 20)
            store.write(targets["director"], "idle", source="claude.Stop")  # the Director reads what it was sent

        def turn_ends(role: str) -> None:
            """The runtime's own trusted turn end, with time either side for the kernel's start times to tell."""
            time.sleep(2.5)
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

        def sent_to_app(ticket: str) -> list:
            return [m for t, m in sends if t == targets["app"] and m.startswith(f"{ticket} -- ")]

        def reports() -> list:
            """What the Director was sent, or still waits to be, about background work under a pane, in order."""
            waiting = json.loads(sql("SELECT coalesce(json_agg(message ORDER BY id), '[]')::text "
                                     "FROM ticket_board.ticket_notification_queue WHERE target_role = 'director' "
                                     "AND payload->>'kind' = 'background_child';"))
            return [m for t, m in sends if t == targets["director"] and (HANDED in m or "went out past" in m)] + waiting

        def records() -> list:
            try:
                return json.loads(sql("SELECT coalesce(json_agg(json_build_array(target_role, child, ticket_id) "
                                      "ORDER BY alerted_at), '[]')::text FROM ticket_board.background_child_alerts;"))
            except AssertionError:
                return []

        def story(ticket: str) -> list:
            """What happened to the ticket's notices to app, in order: a failing check's own evidence."""
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, event, coalesce(busy_reason, ''), "
                                  "coalesce(detail->>'phase', '')) ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND target_role = 'app' "
                                  "AND event NOT IN ('claim', 'listener_claim', 'listener_ack');"))

        def about(pid: int, messages: list) -> list:
            return [m for m in messages if f"pid {pid} (process group {pid})" in m]

        try:
            restart()
            store.write(targets["app"], "idle", source="claude.Stop")
            fixture.seed_postgres_ticket(admin, "PGU-248", title="the VM kit", state="done", assignee="unassigned",
                                         commit_exempt=True)
            # 1. PGU-627 is App's; PGU-641 waits in App's queue behind it.
            route("PGU-627", "app")
            one_pass()
            route("PGU-641", "app")
            one_pass()
            seen["first"] = {"627": sent_to_app("PGU-627"), "641": sent_to_app("PGU-641")}
            daemon = daemon_of(pane_pids["app"])
            # No tool runs a command in a provider's first minute -- the board counts that minute as the start's
            # own -- so the work below starts after it, as App's VMs did, hours in.
            while time.time() < registered_at + 62:
                time.sleep(0.5)
            # 2. App's turns leave work running: two VMs from the kit, a sweep from
            #    PGU-627's worktree, a job for queued PGU-641 from its worktree, a
            #    run bounded by `timeout` and one whose bound has passed.
            vm_a = spawn("app", kit, VM.format(name="pgu627"))
            vm_b = spawn("app", kit, VM.format(name="pgu627b"))
            sweep = spawn("app", worktrees / "pgu-627-app", SLEEPER.format(python=sys.executable, label="sweep"))
            own = spawn("app", worktrees / "pgu-641-app", SLEEPER.format(python=sys.executable, label="prepare"))
            bounded = spawn("app", scratch, f"timeout 3600 {sys.executable} -c 'import time; time.sleep(86400)'")
            overdue = spawn("app", scratch, f"timeout -s CONT 2 {sys.executable} -c 'import time; time.sleep(86400)'")
            children = {"vm_a": vm_a, "vm_b": vm_b, "sweep": sweep, "own": own, "bounded": bounded, "overdue": overdue,
                        "daemon": (daemon, start_time(daemon) if daemon else 0)}
            seen["children"] = {name: pid for name, (pid, _start) in children.items()}
            turn_ends("app")
            # What SYRD-583's reader makes of a VM's directory.
            vm_holder = background_child.Holder(vm_a[0], vm_a[0], vm_a[0], 1.0, "bash -c " + VM.format(name="pgu627"),
                                                str(kit))
            with nl.psycopg.connect(listener_url) as conn:
                seen["vm_origin"] = background_child.origins(conn, [vm_holder]).get(vm_holder.key)
            # 3. PGU-627 is finished: PGU-641 becomes App's active ticket and is handed over.
            finish("PGU-627")
            for _ in range(3):
                one_pass()
            told = reports()
            seen["handoff"] = {"641": sent_to_app("PGU-641"), "told": told, "records": records(),
                               "per_child": {name: about(pid, told) for name, (pid, _s) in children.items()},
                               "alive": {name: alive(*child) for name, child in children.items()},
                               "providers_alive": all(alive(*provider[role]) for role in roles)}
            if not full:
                return seen

            # 4. The next ticket works: App's turn on it ends, a note on it goes out, and its own job starts.
            turn_ends("app")
            comment("PGU-641", "Also cover the restart.")
            one_pass()
            fresh = spawn("app", scratch, SLEEPER.format(python=sys.executable, label="after the hand-over"))
            turn_ends("app")
            one_pass()
            seen["usable"] = {"641": sent_to_app("PGU-641"), "told": reports(), "fresh_alive": alive(*fresh)}
            # 5. A restarted listener looks at the same hand-over again and tells nobody twice.
            restart()
            for _ in range(2):
                one_pass()
            seen["restarted"] = {"told": reports(), "records": records()}
            # 6. PGU-641 is finished, still App's, and App is told so: no hand-over, so nothing is
            #    reported yet. Then PGU-650 is handed over, and the listener restarts in between: what
            #    PGU-641 left, and what was started after its hand-over, is reported now, by the new
            #    listener; nothing reported before is reported again.
            turn_ends("app")  # whatever App's last notice started is over
            finish("PGU-641", assignee="app")
            for _ in range(2):
                one_pass()
            seen["done"] = {"641": sent_to_app("PGU-641"), "told": reports()}
            turn_ends("app")
            route("PGU-650", "app")
            one_pass()  # the hand-over is delivered; the listener restarts before its next pass looks
            restart()
            for _ in range(3):
                one_pass()
            told = reports()
            seen["second"] = {"650": sent_to_app("PGU-650"), "story": story("PGU-650"), "told": told, "records": records(),
                              "own": about(own[0], told), "fresh": about(fresh[0], told),
                              "bounded": about(bounded[0], told)}
            # The Director's own pane is not looked under: its leftover is not reported when a ticket is
            # handed to it.
            mine = spawn("director", scratch, SLEEPER.format(python=sys.executable, label="the Director's own"))
            time.sleep(2.5)  # past the start-time slack
            finish("PGU-650", state="audit", assignee="audit")
            fixture.post_json(base, "/api/tickets/PGU-650/actions/audit_sign_off", {}, caller="audit")  # into DAT, the Director's
            for _ in range(3):
                one_pass()
            seen["director"] = {"handed": [m for t, m in sends if t == targets["director"] and m.startswith("PGU-650 -- ")],
                                "about_it": about(mine[0], reports()), "alive": alive(*mine)}
            # 7. The Director runs the stop the report gives for one VM, then again; and one aimed at a
            #    process whose number was reused.
            stop_a = re.findall(r"run `(sh -c '[^`]*')`", "".join(about(vm_a[0], seen["handoff"]["told"])))
            stop_b = re.findall(r"run `(sh -c '[^`]*')`", "".join(about(vm_b[0], seen["handoff"]["told"])))
            seen["stop"] = stop_a
            # From here this process adopts the stopped VM's orphans and never reaps them, as a provider that
            # is a child subreaper -- or bounded_run -- would: a zombie left behind is not a VM still running.
            import ctypes
            seen["subreaper"] = ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
            first = subprocess.run(["bash", "-c", stop_a[0]], capture_output=True, text=True, timeout=30) if stop_a else None
            deadline = time.time() + 10
            while Path(f"/proc/{vm_a[0]}").exists() and time.time() < deadline:
                time.sleep(0.05)  # the provider reaps the leader it started; until then a rerun still finds it
            # Again, from another shell than the first: zsh where it is installed, else dash or sh.
            other = next(shell for shell in ("zsh", "dash", "sh") if shutil.which(shell))
            again = subprocess.run([other, "-c", stop_a[0]], capture_output=True, text=True, timeout=30) if stop_a else None
            reused = re.sub(r'!= "\d+" \]', '!= "1" ]', stop_b[0]) if stop_b else ""
            wrong = subprocess.run(["bash", "-c", reused], capture_output=True, text=True, timeout=30) if reused else None
            seen["stopped"] = {
                "first": [first.returncode, first.stdout.strip()] if first else None,
                "again": [again.returncode, again.stdout.strip(), other] if again else None,
                "wrong": [wrong.returncode, wrong.stdout.strip(), reused != stop_b[0] if stop_b else False] if wrong else None,
                "alive": {name: alive(*child) for name, child in children.items()},
                "providers_alive": all(alive(*provider[role]) for role in roles), "registered": registered()}
            new = "note_handoff_child(text, text, text, text)"
            seen["grantees"] = sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                                   "ELSE a.grantee::regrole::text END, '+'), '') "
                                   f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{new}'::regprocedure "
                                   "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;")
            if os.environ.get("SYRD636_MUTATION_SKIP_COPY_PARITY") != "1":
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fresh_db = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.psql(fresh_db, (root / "scripts/ticket_board/schema.sql").read_text())
                definition = f"SELECT md5(pg_get_functiondef('ticket_board.{new}'::regprocedure));"
                seen["fresh_equals_migrated"] = fixture.psql(fresh_db, definition).strip() == sql(definition)
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
    """The parts with no board behind them: where an origin may come from, and what a bound is."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board import background_child as bc, handoff_children as hc

    with tempfile.TemporaryDirectory(prefix="syrd636u.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        git("worktree", "add", "-q", "-b", "w", str(tmp_path / "syrd-579-app"), cwd=repo)
        (tmp_path / "syrd-579-app" / "tests").mkdir()
        (tmp_path / "syrd-248-vm").mkdir()
        clone = tmp_path / "pgu-12-source"
        git("clone", "-q", str(repo), str(clone), cwd=tmp_path)
        # A copied worktree: its .git file names a registration that points elsewhere.
        copied = tmp_path / "syrd-580-copy"
        copied.mkdir()
        (copied / ".git").write_text((tmp_path / "syrd-579-app" / ".git").read_text())
        found = {name: bc.worktree_ticket(str(tmp_path / name)) for name in
                 ("syrd-579-app", "syrd-579-app/tests", "syrd-248-vm", "pgu-12-source", "syrd-580-copy", "nowhere/syrd-9-x")}
        check(found == {"syrd-579-app": "SYRD-579", "syrd-579-app/tests": "SYRD-579", "syrd-248-vm": "",
                        "pgu-12-source": "", "syrd-580-copy": "", "nowhere/syrd-9-x": ""},
              f"a ticket is read only from a worktree git registered, at or above the path: {found}")
        wt = tmp_path / "syrd-579-app"
        holders = {
            "cwd": bc.Holder(1, 1, 1, 0.0, "bash -c make", str(wt / "tests")),
            "command": bc.Holder(1, 1, 1, 0.0, f'bash -c while pgrep -f "sweep.sh {wt}" >/dev/null; do sleep 20; done',
                                 str(tmp_path / "node-18-scratch")),
            "kit": bc.Holder(1, 1, 1, 0.0, "bash -c ./resume-vm.sh syrd627 8 16384", str(tmp_path / "syrd-248-vm")),
            "temp": bc.Holder(1, 1, 1, 0.0, "x", "/tmp/claude-1006/pgu-12-a"),
        }
        tickets = {name: holder.candidate_tickets() for name, holder in holders.items()}
        check(tickets == {"cwd": ["SYRD-579"], "command": ["SYRD-579"], "kit": [], "temp": []},
              f"ids come from a worktree the directory is in, then one a command names; never from a name alone: {tickets}")

    leftovers_at_a_handover()

    start = 1000.0
    bounds = {
        "plain": hc.bounded_until(["timeout", "900", "python3", "x"], start),
        "path": hc.bounded_until(["/usr/bin/timeout", "30s", "x"], start),
        "kill-after": hc.bounded_until(["timeout", "-k", "5", "1m", "x"], start),
        "long options": hc.bounded_until(["timeout", "--signal=KILL", "--kill-after=1h", "--preserve-status", "2h", "x"], start),
        "joined": hc.bounded_until(["timeout", "-sINT", "-v", "--foreground", "1.5", "x"], start),
        "double dash": hc.bounded_until(["timeout", "--", "1d", "x"], start),
        "zero": hc.bounded_until(["timeout", "0", "x"], start),
        "no command": hc.bounded_until(["timeout", "10"], start),
        "not timeout": hc.bounded_until(["sleep", "900"], start),
        "named like it": hc.bounded_until(["timeouts", "900", "x"], start),
        "garbage": hc.bounded_until(["timeout", "soon", "x"], start),
    }
    check(bounds == {"plain": 1900.0, "path": 1030.0, "kill-after": 1065.0, "long options": 1000.0 + 3 * 3600,
                     "joined": 1001.5, "double dash": 1000.0 + 86400, "zero": None, "no command": None,
                     "not timeout": None, "named like it": None, "garbage": None},
          f"a bound is coreutils timeout's own deadline, kill-after included; nothing else is one: {bounds}")
    stop = bc.checked_stop(4242, 777)
    check(stop.startswith("sh -c '") and stop.endswith("'") and stop.count("'") == 2 and "\\" not in stop
          and "kill -s TERM -- -$g" in stop and "g=4242" in stop and '!= "777" ]' in stop,
          f"the stop is one quoted sh -c, the same in bash, zsh or fish, and checks the leader before it signals: {stop}")


def leftovers_at_a_handover() -> None:
    """Which sessions under a pane a hand-over reports, decided over a fake /proc and a fake board."""
    from types import SimpleNamespace
    from scripts.ticket_board import background_child as bc, handoff_children as hc

    sent_at, now = 1000.0, 2000.0
    with tempfile.TemporaryDirectory(prefix="syrd636l.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        git("init", "-q", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        for name in ("own-1-app", "old-2-app"):
            git("worktree", "add", "-q", "-b", name, str(tmp_path / name), cwd=repo)
        kit = tmp_path / "old-2-vm"
        kit.mkdir()
        proc = tmp_path / "proc"
        # pid: (session, start, cwd, argv); the pane's own session is 1.
        table = {
            10: (10, 900.0, kit, ["bash", "-c", "./resume-vm.sh old2"]),          # unknown: reported
            20: (20, sent_at - 0.5, kit, ["bash", "-c", "x"]),                      # too close to tell: not
            30: (30, 900.0, tmp_path / "own-1-app", ["bash", "-c", "build"]),       # the role's own ticket: not
            40: (40, 900.0, tmp_path / "old-2-app", ["bash", "-c", "sweep"]),       # a finished ticket's: reported
            50: (50, 900.0, kit, ["bash", "-c", "timeout 3600 x"]),
            51: (50, 900.0, kit, ["timeout", "3600", "x"]),                         # bounded, deadline ahead: not
            60: (60, 900.0, kit, ["bash", "-c", "timeout 30 x"]),
            61: (60, 900.0, kit, ["timeout", "30", "x"]),                           # bounded, deadline past: reported
            70: (1, 900.0, kit, ["mcp-server"]),                                    # the pane's own session: not
            80: (80, 0.0, kit, ["bash", "-c", "y"]),                                # start unknown: not
            90: (90, 100.4, kit, ["codex", "app-server"]),                          # the provider's own, at its start: not
            91: (91, 161.0, kit, ["bash", "-c", "z"]),                              # past its first minute: reported
            95: (95, 900.0, kit, ["bash", "-c", "gone"]),                           # ended since the sample: not
        }
        for pid, (session, _start, cwd, argv) in table.items():
            if pid == 95:
                continue  # sampled, then gone before it is described
            (proc / str(pid)).mkdir(parents=True)
            fields = ["S", "1", str(session), str(session)] + ["0"] * 15 + [str(pid * 100)]
            (proc / str(pid) / "stat").write_text(f"{pid} (x) " + " ".join(fields) + "\n")
            (proc / str(pid) / "cmdline").write_bytes(b"\0".join(part.encode() for part in argv) + b"\0")
            (proc / str(pid) / "cwd").symlink_to(cwd)
        sample = SimpleNamespace(observed=True, pids=frozenset(table),
                                 detached=frozenset(pid for pid, row in table.items() if row[0] != 1),
                                 starts=tuple((pid, row[1]) for pid, row in table.items()),
                                 sessions=tuple((pid, row[0]) for pid, row in table.items()))
        gate = SimpleNamespace(role_targets={"app": "pgu-app:0.0"}, proc_root=str(proc), LAUNCHER_SEED_STARTUP_SECONDS=60.0,
                               _pane_pid=lambda target: 4 if target == "pgu-app:0.0" else None,
                               _child_work_sample=lambda pane: sample, provider_started_at=lambda target: 100.0)
        board = {"OWN-1": ("OWN-1", "in_progress", "app"), "OLD-2": ("OLD-2", "done", "unassigned")}
        conn = SimpleNamespace(execute=lambda _sql, params: SimpleNamespace(
            fetchall=lambda: [board[ticket] for ticket in params[0] if ticket in board]))
        found = hc.leftovers(gate, conn, "app", sent_at, {"done", "cancelled"}, now)
        reported = {holder.pid: (origin or {}).get("id") for holder, origin in found}
        check(reported == {10: None, 40: "OLD-2", 60: None, 91: None},
              f"reported: what was running a second or more before the hand-over, in a session of its own, started "
              f"after its provider's first minute, for no ticket the role owns, and not inside a timeout's deadline: "
              f"{reported}")
        check(hc.leftovers(gate, conn, "main", sent_at, {"done"}, now) == [],
              "a role with no pane is looked at for nothing")


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
    if os.environ.get("SYRD636_MUTATION_SKIP_BEFORE") != "1":
        before_run()
    candidate(after)
    units()
    print(f"handoff_child_report_test: {CHECKS} checks ok")
    return 0


def before_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd636.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    handoff = before["handoff"]
    check(len(handoff["641"]) == 1 and "is now your active ticket" in handoff["641"][0],
          f"before: PGU-641 is handed to App past the work earlier tickets left: {handoff['641']}")
    check(handoff["told"] == [] and handoff["records"] == [] and all(handoff["alive"].values()),
          f"before: nobody tells the Director about any of it, and it all runs on: {handoff}")
    check((before["vm_origin"] or {}).get("id") == "PGU-248",
          f"before: a VM run from the kit's directory would be called PGU-248's: {before['vm_origin']}")


def candidate(after: dict) -> None:
    first = after["first"]
    check(len(first["627"]) == 1 and first["641"] == [],
          f"PGU-627 reaches App; PGU-641, queued behind it, is not announced yet: {first}")
    check(after["vm_origin"] is None, f"a VM run from the kit's directory has no known ticket: {after['vm_origin']}")
    handoff = after["handoff"]
    check(len(handoff["641"]) == 1 and "is now your active ticket" in handoff["641"][0],
          f"PGU-641 is handed to App once: {handoff['641']}")
    per = handoff["per_child"]
    check({name: len(told) for name, told in per.items()}
          == {"vm_a": 1, "vm_b": 1, "sweep": 1, "own": 0, "bounded": 0, "overdue": 1, "daemon": 0}
          and len(handoff["told"]) == 4 and after["children"]["daemon"] > 0,
          f"the Director is told once about each child an earlier ticket left, and nothing else: {per}")
    vm = per["vm_a"][0] if per["vm_a"] else ""
    pid = after["children"]["vm_a"]
    check(vm.startswith("app was handed PGU-641 with background work still running in its pane from earlier work: ")
          and f"pid {pid} (process group {pid}), running for " in vm and "its ticket is not known" in vm
          and "PGU-248" not in vm and "`bash -c ./resume-vm.sh pgu627 8 16384 > pgu627.qemu.log" in vm and "pgu-248-vm" in vm
          and "Nothing was stopped." in vm and f"run `sh -c 'g={pid};" in vm and "never the provider" in vm,
          f"a VM's report: pid, group, age, command, directory, no guessed ticket, nothing stopped, the checked stop: {vm}")
    sweep = per["sweep"][0] if per["sweep"] else ""
    check("started for PGU-627, which is now done (unassigned)" in sweep and "exit 0 # sweep" in sweep,
          f"the sweep from PGU-627's worktree names PGU-627: {sweep}")
    overdue = per["overdue"][0] if per["overdue"] else ""
    check("timeout -s CONT 2" in overdue, f"a run past its own timeout is no longer bounded work: {overdue}")
    check(all(handoff["alive"].values()) and handoff["providers_alive"],
          f"nothing is stopped, the providers least of all: {handoff['alive']}")
    check(sorted(row[2] for row in handoff["records"]) == ["PGU-641"] * 4
          and len({row[1] for row in handoff["records"]}) == 4 and {row[0] for row in handoff["records"]} == {"app"},
          f"one record per child, against the hand-over that found it: {handoff['records']}")

    usable = after["usable"]
    check(len(usable["641"]) == 2 and "new comment" in usable["641"][1] and sorted(usable["told"]) == sorted(handoff["told"])
          and usable["fresh_alive"],
          f"the next ticket works: its note goes out past the leftovers, and nobody is told again: {usable}")
    restarted = after["restarted"]
    check(sorted(restarted["told"]) == sorted(handoff["told"]) and restarted["records"] == handoff["records"],
          f"a restarted listener reads the same hand-over again and tells nobody twice: {restarted}")
    done = after["done"]
    check(len(done["641"]) == 3 and "entered Done" in done["641"][2] and sorted(done["told"]) == sorted(handoff["told"]),
          f"a ticket's own done notice is no hand-over: App is told PGU-641 is done, and nothing is reported: {done}")
    second = after["second"]
    new = sorted(set(second["told"]) - set(handoff["told"]))
    check(len(second["650"]) == 1 and len(second["told"]) == len(handoff["told"]) + 2 and len(new) == 2
          and len(second["own"]) == 1 and len(second["fresh"]) == 1
          and "started for PGU-641, which is now done (assigned to app)" in second["own"][0]
          and "its ticket is not known" in second["fresh"][0] and second["bounded"] == [],
          f"at the next hand-over only what has since become left over is reported, once: {new}")
    check([row[2] for row in second["records"]] == ["PGU-641"] * 4 + ["PGU-650"] * 2,
          f"each child has one record: {second['records']}")
    mine = after["director"]
    check(len(mine["handed"]) == 1 and mine["about_it"] == [] and mine["alive"],
          f"a ticket handed to the Director reports nothing about the Director's own pane: {mine}")

    stopped = after["stopped"]
    check(len(after["stop"]) == 1 and after["subreaper"] and stopped["first"] == [0, f"process group {after['children']['vm_a']} stopped"]
          and not stopped["alive"]["vm_a"],
          f"the report's stop ends that VM, and says so: {stopped['first']}")
    check(stopped["again"] is not None and stopped["again"][0] == 1 and "nothing sent" in stopped["again"][1],
          f"run again, it finds the group gone and sends nothing: {stopped['again']}")
    check(stopped["wrong"] is not None and stopped["wrong"][2] and stopped["wrong"][0] == 1
          and "nothing sent" in stopped["wrong"][1] and stopped["alive"]["vm_b"],
          f"aimed at a number now another process's, it sends nothing: {stopped['wrong']}")
    check({name for name, up in stopped["alive"].items() if up} == {"vm_b", "sweep", "own", "bounded", "overdue", "daemon"}
          and stopped["providers_alive"] and stopped["registered"] == after["registered"],
          f"only that VM stopped; the providers stay registered as they were: {stopped}")
    check(after["grantees"] == "ticket_board_listener", f"only the listener may record a hand-over report: {after['grantees']}")
    if os.environ.get("SYRD636_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["fresh_equals_migrated"] is True,
              f"a schema.sql board installs exactly what the migration does: {after['fresh_equals_migrated']}")


if __name__ == "__main__":
    raise SystemExit(main())
