#!/usr/bin/env python3
"""SYRD-767, live: `restart-role` on a process-authority board, with the real registrar and the real board handler.

On syrd (999e7a67) every role's reload start died within two seconds and its second start came up; Audit's second
start died too, set its conversation aside, and its fresh fallback died as well. Nothing recorded why.

Here a role's pane runs exactly what a live pane runs: the launcher's own pane command, whose
`ticket-board-register-runtime` registers with the board before it execs the CLI, against the production
`TicketBoardUnixServer` and `ProcessRoleAuthority` on a real AF_UNIX socket, where SO_PEERCRED and /proc decide which
pane is which. Only the board's storage is in memory. The tmux server is isolated (TMUX removed, TMUX_TMPDIR
temporary, socket asserted). The CLI is a stand-in that takes `HUP_SECONDS` to leave after its hang-up, as a real CLI
does while it shuts down (SYRD-559 measured about three seconds on otto).
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class Store:
    """The board's runtime-assignment storage, in memory; everything that decides is the production handler's."""

    def __init__(self) -> None:
        self.assignment: dict | None = None
        self.events: list[dict] = []

    def runtime_assignment_for_process(self, pid: int, start: int, uid: int):
        row = self.assignment
        if row and (row["process_pid"], row["process_start_time"], row["process_uid"]) == (pid, start, uid):
            return row
        return None

    def runtime_assignment(self, role: str):
        return self.assignment if self.assignment and self.assignment["role"] == role else None

    def register_runtime_assignment(self, **row):
        self.assignment = {**row, "generation": int(row["expected_generation"]) + 1}
        self.events.append({"registered": row["process_pid"], "generation": self.assignment["generation"]})
        return self.assignment


class Quiet:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class Board:
    def __init__(self, path: Path) -> None:
        import pwd

        from scripts.ticket_board import server as board_server

        self.store = Store()
        authority = board_server.ProcessRoleAuthority(self.store, pwd.getpwuid(os.getuid()).pw_name)
        handler = board_server.TicketBoardHandler.handle_register_caller
        store = self.store

        def recorded(handler_self, payload):  # the production handler; its refusals are only noted on the way out
            try:
                return handler(handler_self, payload)
            except Exception as exc:
                store.events.append({"refused": str(exc), "role": payload.get("role")})
                raise

        self.patch = mock.patch.object(board_server.TicketBoardHandler, "handle_register_caller", recorded)
        self.patch.start()
        self.server = board_server.TicketBoardUnixServer(path, self.store, events=Quiet(), director_notifier=Quiet(),
                                                         role_authority=authority)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.patch.stop()


class Live:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s767."))
        for name, sub in (("HOME", "home"), ("XDG_STATE_HOME", "home/.local/state"), ("XDG_CONFIG_HOME", "home/.config")):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(self.tmp / sub)
        for key in [k for k in os.environ if k.startswith(("TMUX", "TICKET_BOARD_", "PGU_", "CLAUDE"))]:
            os.environ.pop(key, None)
        os.environ["TMUX_TMPDIR"] = str(self.tmp / "tmux")
        (self.tmp / "tmux").mkdir()
        self.socket = self.tmp / "tmux" / f"tmux-{os.getuid()}" / "default"
        probe = tmux("list-sessions")
        assert probe.returncode != 0 and str(self.socket) in probe.stderr, f"not the isolated socket: {probe.stderr!r}"
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        self.hup = self.tmp / "hup-seconds"
        self.hup.write_text("0")
        # A Python script by the CLI's own name, so the launcher's checks find it by name. After its hang-up it takes
        # the seconds in `hup-seconds` to leave, as a real CLI shutting down does.
        (self.bindir / "claude").write_text(
            f"#!{sys.executable}\n"
            "import os, signal, sys, time\n"
            "if sys.argv[1:2] == ['--version']:\n    print('2.1.318 (Claude Code)'); sys.exit(0)\n"
            f"linger = float(open({str(self.hup)!r}).read() or 0)\n"
            "def leave(*_):\n    time.sleep(linger); os._exit(0)\n"
            "signal.signal(signal.SIGHUP, leave)\n"
            f"open({str(self.tmp)!r} + '/up-' + str(os.getpid()), 'w').close()\n"
            "while True:\n    time.sleep(600)\n")
        (self.bindir / "claude").chmod(0o755)
        os.environ["PATH"] = f"{self.bindir}:{os.environ['PATH']}"
        self.panes = self.tmp / "panes"
        self.panes.mkdir()
        # As syrd's server is set (`default-shell /bin/sh`): the pane's command execs down to the CLI, so the pane's
        # root process -- what the board registers -- is the CLI itself, not a login shell above it.
        os.environ["SHELL"] = "/bin/sh"
        self.board_socket = self.tmp / "board.sock"
        self.board = Board(self.board_socket)

    def config(self):
        from scripts import team_launcher as launcher

        (self.tmp / "app").mkdir(exist_ok=True)
        doc = {"project": "p767", "board_url": "http://127.0.0.1:1/", "board_socket": str(self.board_socket),
               "role_state_isolation": True, "session_dir": str(self.tmp / "sessions"),
               "desktop_access": {"mode": "headless"},
               "roles": [{"role": "app", "slot": 0, "workdir": str(self.tmp / "app"), "cli": [str(self.bindir / "claude")],
                          "env": {"TICKET_BOARD_DIRECTORCTL": str(ROOT / "scripts" / "directorctl")}}]}
        path = self.tmp / "p767.json"
        path.write_text(json.dumps(doc))
        return launcher.load_project_config("p767", path), path

    def registered_pane(self) -> int | None:
        row = self.board.store.assignment or {}
        return row.get("process_pid")

    def start(self, config):
        from scripts import team_launcher as launcher
        from scripts.ticket_board.notify_listener import PaneHookStateStore
        from scripts.ticket_board.peer_identity import read_process

        role = config.roles[0]
        started = launcher.ensure_visible_role_session_for_viewer(
            role, mode="attach-or-start", session_dir=launcher.role_session_dir(config, role), pane_state_dir=self.panes,
            bin_user="", runner=subprocess.run)
        check(started == 0 and wait_for(lambda: self.registered_pane() == launcher.pane_pid_for_role(role) > 0),
              f"the role started and its pane registered with the board (exit {started}; {self.board.store.events})")
        PaneHookStateStore(self.panes).write(role.target, "idle", source="claude.Stop")
        return read_process(launcher.pane_pid_for_role(role))

    def restart(self, config, path) -> tuple[int, list[str]]:
        from scripts import role_restart, role_self_restart
        from scripts import team_launcher as launcher

        store = self.board.store

        def opener(url: str, timeout: float = 0):
            body = {
                "workflow": {"document": {"roles": [{"name": "app", "active": True}]}},
                "runtime-assignments": {"project": "p767", "authority_mode": "process",
                                        "assignments": {"app": dict(store.assignment)} if store.assignment else {}},
                "reservations": {"reservations": {"app": None}},
                "execution-leases": {"leases": []},
                "provider-holds": {"holds": []},
            }[url.split("/api/", 1)[1]]
            return io.BytesIO(json.dumps(body).encode())

        said: list[str] = []
        with mock.patch.object(role_restart, "caller_authority", lambda *a, **k: ""), \
                mock.patch.object(role_restart, "REGISTER_TIMEOUT_SECONDS", 15.0), \
                mock.patch.object(launcher, "default_pane_state_dir_for_user", lambda *a, **k: self.panes), \
                mock.patch.object(role_self_restart, "lock_path", lambda c, r: self.tmp / "locks" / f"{r.role}.lock"):
            code = role_restart.restart_role_command(config, config_path=path, role_name="app", runner=subprocess.run,
                                                     opener=opener, print_func=said.append)
        return code, said

    def record_conversation(self, config, conversation: str) -> None:
        """A recorded conversation, with the transcript Claude Code keeps for it, so the launcher resumes it."""
        from scripts import team_launcher as launcher

        role = config.roles[0]
        sessions = launcher.role_session_dir(config, role)
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / launcher.session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": conversation}))
        transcript = Path.home() / ".claude" / "projects" / str(role.workdir).replace("/", "-") / f"{conversation}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "user", "sessionId": conversation, "cwd": str(role.workdir)}) + "\n")

    def reset(self) -> None:
        tmux("kill-server")
        wait_for(lambda: tmux("list-sessions").returncode != 0, 5.0)
        self.board.store.assignment = None
        self.board.store.events.clear()


def resume_log(config) -> list[dict]:
    from scripts import team_launcher as launcher

    role = config.roles[0]
    path = launcher.role_session_dir(config, role) / f"{launcher.session_file_name(role.target)}".replace(
        ".json", ".resume.log")
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except OSError:
        return []


def test_a_restart_comes_back_on_its_first_start(live: Live) -> None:
    for linger in ("0", "2.5"):
        live.hup.write_text(linger)
        config, path = live.config()
        before = live.start(config)
        code, said = live.restart(config, path)
        events = list(live.board.store.events)
        check(code == 0 and not any("second start" in s for s in said),
              f"old CLI leaving {linger}s after its hang-up: restarted on its first start: {said} board: {events}")
        check(not any("refused" in e for e in events),
              f"old CLI leaving {linger}s after its hang-up: the board never refused the replacement: {events}")
        check(live.registered_pane() not in (None, before.pid),
              f"old CLI leaving {linger}s after its hang-up: the board names the replacement: {events}")
        live.reset()


def test_a_refused_registration_keeps_the_conversation(live: Live) -> None:
    """Audit's case: the board refuses the new pane because a live process still holds the role. The CLI never ran,
    so its recorded conversation is not set aside and no fresh one is started; the refusal is said and kept.

    Once with the pane's command exec'd down to the CLI (`default-shell /bin/sh`, as syrd's server is set), and once
    under a shell that forks it instead, as fish -- every tenant owner's login shell -- does, and takes a moment
    before it does, as fish reading its configuration takes: there the registrar is not yet in the pane at the start's
    first look, and the CLI's name is already on the shell's `-c` line (Audit's finding on 902caf55)."""
    forking = live.tmp / "forking-shell"
    forking.write_text('#!/bin/sh\n[ "$1" = -c ] && shift\nexec /bin/sh -c "sleep 0.5; $1; :"\n')
    forking.chmod(0o755)
    try:
        for shell in ("/bin/sh", str(forking)):
            os.environ["SHELL"] = shell
            _a_refused_registration(live, forked=shell != "/bin/sh")
    finally:
        os.environ["SHELL"] = "/bin/sh"


def _a_refused_registration(live: Live, *, forked: bool) -> None:
    import contextlib

    from scripts import team_launcher as launcher
    from scripts.ticket_board.peer_identity import read_process

    live.hup.write_text("0")
    config, _path = live.config()
    role = config.roles[0]
    sessions = launcher.role_session_dir(config, role)
    live.record_conversation(config, "app-conversation")
    tag = "forking shell: " if forked else ""
    holder = subprocess.Popen(["sleep", "600"])
    try:
        held = read_process(holder.pid)
        live.board.store.assignment = {"role": "app", "runtime": "claude", "target": role.target,
                                       "process_pid": held.pid, "process_start_time": held.start_time,
                                       "process_uid": os.getuid(), "generation": 1}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = launcher.ensure_visible_role_session_for_viewer(
                role, mode="attach-or-start", session_dir=sessions, pane_state_dir=live.panes, bin_user="",
                runner=subprocess.run)
        said = err.getvalue()
        events = [entry.get("event") for entry in resume_log(config)]
        check(code == 1 and tmux("has-session", "-t", role.tmux_session).returncode != 0,
              f"{tag}the refused start fails and leaves no session (exit {code}): {said}")
        check(events[-1:] == ["registration_refused"] and "resume_failed" not in events and "fallback_started" not in events,
              f"{tag}recorded as a refused registration, not a failed resume, and no fresh fallback: {events}")
        check("the board refused app's new pane (role app is held by a live pane)" in said
              and "its recorded conversation is kept" in said, f"{tag}the refusal is said, from the pane's own line: {said}")
        check(launcher.session_id_for_role(role, sessions) == "app-conversation"
              and not any(p.name.endswith(".superseded") for p in sessions.iterdir()),
              f"{tag}the recorded conversation is kept, nothing set aside: {sorted(p.name for p in sessions.iterdir())}")
        capture = sessions / f"{launcher.session_file_name(role.target)}.startup-exit"
        check(capture.exists() and "runtime registration refused" in capture.read_text(errors="replace"),
              f"{tag}the start that died wrote its startup capture: {capture}")
    finally:
        holder.kill()
        holder.wait()
    began = time.monotonic()
    code = launcher.ensure_visible_role_session_for_viewer(
        role, mode="attach-or-start", session_dir=sessions, pane_state_dir=live.panes, bin_user="", runner=subprocess.run)
    took = time.monotonic() - began
    # Promptly: the start sees its CLI running, rather than waiting out the registrar's whole bound.
    from scripts import role_sessions
    check(took < role_sessions.REGISTRAR_HANDOVER_SECONDS / 2,
          f"{tag}the start that comes up returns once its CLI runs, not at the bound: {took:.1f}s")
    check(code == 0 and resume_log(config)[-1].get("event") == "resumed"
          and resume_log(config)[-1].get("session_id") == "app-conversation",
          f"{tag}once the holder is gone, the role resumes its kept conversation: {resume_log(config)[-1:]}")
    root = read_process(launcher.pane_pid_for_role(role))
    check(root is not None and (root.comm == "sh") == forked,
          f"the case's precondition: the pane's root is {'a shell that forked the CLI' if forked else 'the CLI'}: {root}")
    live.reset()


def test_a_reload_whose_old_pane_will_not_end_starts_nothing(live: Live) -> None:
    """The old pane's process outlives SIGTERM and SIGKILL (the handover's wait raises, as it does then): the reload
    refuses, says why, and starts no replacement the board would refuse."""
    import contextlib

    from scripts import runtime_handover
    from scripts import team_launcher as launcher

    live.hup.write_text("0")
    config, _path = live.config()
    role = config.roles[0]
    live.start(config)
    events = len(live.board.store.events)

    def stays(holders, **_kwargs):
        raise RuntimeError(f"p767-app's stopped pane process (pid {', '.join(str(p) for p, _ in sorted(holders))}) "
                           "did not exit")

    err = io.StringIO()
    with mock.patch.object(runtime_handover, "await_holders_gone", stays), contextlib.redirect_stderr(err):
        code = launcher.ensure_visible_role_session_for_viewer(
            role, mode="reload", session_dir=launcher.role_session_dir(config, role), pane_state_dir=live.panes,
            bin_user="", runner=subprocess.run)
    check(code == 1 and "did not exit" in err.getvalue(), f"the reload refuses and says why (exit {code}): {err.getvalue()}")
    check(tmux("has-session", "-t", role.tmux_session).returncode != 0 and len(live.board.store.events) == events,
          f"and starts nothing: no session, no registration attempt: {live.board.store.events[events:]}")
    live.reset()


def test_a_start_through_an_exact_target_runner_is_armed(live: Live) -> None:
    """Presentation recovery starts a role through its own runner, which makes every tmux target exact
    (`=<session>`). The start's capture is armed in the same invocation, and `=<session>` alone names no window or
    pane there: the arm targets `<session>:`, so the start comes up, registered, and is disarmed once it is up."""
    from scripts import presentation_controller
    from scripts import team_launcher as launcher

    exact = presentation_controller._exact_tmux_runner(subprocess.run)
    live.hup.write_text("0")
    config, _path = live.config()
    role = config.roles[0]
    code = launcher.ensure_visible_role_session_for_viewer(
        role, mode="attach-or-start", session_dir=launcher.role_session_dir(config, role), pane_state_dir=live.panes,
        bin_user="", runner=exact)
    check(code == 0 and wait_for(lambda: live.registered_pane() == launcher.pane_pid_for_role(role) > 0),
          f"started through an exact-target runner, up and registered (exit {code}): {live.board.store.events}")
    # Disarmed, by what it does (tmux's show-hooks lists nothing even while armed): the capture file was emptied at
    # the start, and a pane that ends now goes as an unarmed one does -- its session closes, nothing is captured.
    sessions = launcher.role_session_dir(config, role)
    capture = sessions / f"{launcher.session_file_name(role.target)}.startup-exit"
    check(capture.exists() and capture.stat().st_size == 0, f"the start was armed, its capture empty: {capture}")
    os.kill(launcher.pane_pid_for_role(role), signal.SIGKILL)
    check(wait_for(lambda: tmux("has-session", "-t", role.tmux_session).returncode != 0, 10.0),
          "a pane that ends after the start closes its session")
    check(capture.stat().st_size == 0, f"and is not captured: the start disarmed itself once up: {capture.read_text()!r}")
    live.reset()


def main() -> int:
    live = Live()
    try:
        for name, case in sorted(globals().items()):
            if name.startswith("test_") and callable(case):
                case(live)
    finally:
        live.reset()
        live.board.close()
    print(f"role_restart_registration_live_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
