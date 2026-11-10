#!/usr/bin/env python3
"""SYRD-752, live: `restart-role` from a Director's pane, on a real tmux server with the real launcher.

What sapi hit, reproduced: run from a Claude Code Director pane, the restart either refused (the Director's pane
session id is not the role's recorded one) or -- with that cleared by hand -- started every Claude role under the
Director's own CLAUDECODE / CLAUDE_CODE_* variables, where it ended and did not come back. Here this process stands in
for that pane: its environment carries the Director's pane identity and every supported runtime's nesting variables.
The stand-in CLIs behave as the real ones did: each refuses to run when it finds its runtime's nesting variable, and
otherwise only runs. A tmux server the launch starts takes the launching process's environment, which is how a caller's
variables reach a pane at all; so the role's session is the server's only one, and the reload's own start begins it.

1. Director -> Claude and Director -> Codex: the role is restarted and registered, and no caller identity or nesting
   variable is in the replacement's environment -- only values the target is configured with
   (CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK, which the launcher sets for every Claude role).
2. A replacement that fails to start after the old pane ended: one bounded second start recovers it; one that fails
   again leaves the role stopped and said so, with the command that starts it -- which then does.
3. A display slot showing the role -- a real nested attach, with a real outer client -- shows the replacement after
   the restart, as tmux itself reports it, with no `present show` step.

The tmux server is isolated (TMUX removed, TMUX_TMPDIR temporary, socket asserted). The board's GETs are stood in:
the role's registration is the process tmux says runs its pane, as its own registration would make it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

CHECKS = 0
#: The Director pane this process stands in for.
CALLER = {
    "TICKET_BOARD_PANE_SESSION_ID": "director-conversation", "PGU_PANE_SESSION_ID": "director-conversation",
    "TICKET_BOARD_PANE_TARGET": "p752-director:0.0", "PGU_PANE_TARGET": "p752-director:0.0",
    "TICKET_BOARD_CALLER_ROLE": "director", "TMUX_PANE": "%9",
    "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": "director-session", "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/director.sock",
    "CLAUDE_CODE_MESSAGING_TOKEN": "t", "CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_PID": "1", "CLAUDE_EFFORT": "high",
    "CODEX_THREAD_ID": "director-thread", "CODEX_SANDBOX_NETWORK_DISABLED": "1",
    "HERMES_SESSION_ID": "h", "COPILOT_CLI_VERSION": "1", "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:1",
}
NESTED = {"claude": ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"), "codex": ("CODEX_THREAD_ID",)}


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


class Live:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="s752live."))
        for name, sub in (("HOME", "home"), ("XDG_STATE_HOME", "home/.local/state"), ("XDG_CONFIG_HOME", "home/.config")):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
            os.environ[name] = str(self.tmp / sub)
        for key in ("TMUX", "TMUX_PANE", *CALLER):
            os.environ.pop(key, None)
        os.environ["TMUX_TMPDIR"] = str(self.tmp / "tmux")
        (self.tmp / "tmux").mkdir()
        self.socket = self.tmp / "tmux" / f"tmux-{os.getuid()}" / "default"
        probe = tmux("list-sessions")
        assert probe.returncode != 0 and str(self.socket) in probe.stderr, f"not the isolated socket: {probe.stderr!r}"
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        self.failures = self.tmp / "fail-next"  # how many next starts of a stand-in fail
        for runtime, version in (("claude", "2.1.318 (Claude Code)"), ("codex", "codex-cli 0.130.0")):
            # A Python script by the runtime's own name, so its command line names it for as long as it runs: the
            # launcher's start check finds the role's CLI in the pane's process tree by that name.
            (self.bindir / runtime).write_text(
                f"#!{sys.executable}\n"
                "import os, sys, time\n"
                f"if sys.argv[1:2] == ['--version']:\n    print({version!r}); sys.exit(0)\n"
                # As the real CLI did: under its runtime's nesting variables it does not run as a role of its own.
                f"for name in {NESTED[runtime]!r}:\n"
                "    if name in os.environ:\n        print('nested: ' + name, file=sys.stderr); sys.exit(3)\n"
                f"failures = {str(self.failures)!r}\n"
                "left = int(open(failures).read() or 0) if os.path.exists(failures) else 0\n"
                "if left > 0:\n    open(failures, 'w').write(str(left - 1)); sys.exit(4)\n"
                # Past every check: running as the role. Said where the case can wait for it.
                f"open({str(self.tmp)!r} + '/up-' + str(os.getpid()), 'w').close()\n"
                "time.sleep(600)\n")
            (self.bindir / runtime).chmod(0o755)
        os.environ["PATH"] = f"{self.bindir}:{os.environ['PATH']}"
        self.panes = self.tmp / "panes"
        self.panes.mkdir()

    def config(self, runtime: str, *, presentation: bool = False):
        from scripts import team_launcher as launcher

        (self.tmp / "app").mkdir(exist_ok=True)
        # Its stores in this case's own directory: never the account's real home, whatever HOME says.
        doc = {"project": "p752", "board_url": "http://127.0.0.1:1/", "session_dir": str(self.tmp / "sessions"),
               "desktop_access": {"mode": "headless"},
               "roles": [{"role": "app", "slot": 0, "workdir": str(self.tmp / "app"), "cli": [str(self.bindir / runtime)]}]}
        if presentation:
            doc["presentation"] = {}
        path = self.tmp / "p752.json"
        path.write_text(json.dumps(doc))
        return launcher.load_project_config("p752", path), path

    def start(self, config) -> object:
        from scripts import team_launcher as launcher
        from scripts.ticket_board.notify_listener import PaneHookStateStore
        from scripts.ticket_board.peer_identity import read_process

        role = config.roles[0]
        started = launcher.ensure_visible_role_session_for_viewer(
            role, mode="attach-or-start", session_dir=launcher.role_session_dir(config, role), pane_state_dir=self.panes,
            bin_user="", runner=subprocess.run)
        check(started == 0 and tmux("display-message", "-p", "#{socket_path}").stdout.strip() == str(self.socket),
              f"the role started, on the isolated server only (exit {started})")
        PaneHookStateStore(self.panes).write(role.target, "idle", source="claude.Stop")
        before = read_process(launcher.pane_pid_for_role(role))
        from scripts import role_restart

        # Its CLI itself past its own checks and running (a shell such as fish starts it as a child, and slowly):
        # only then is anything the case changes -- the stand-in's failure count -- read by the next start, not this.
        check(before is not None and wait_for(lambda: any(
            (self.tmp / f"up-{pid}").exists() for pid in role_restart.pane_tree(before.pid, Path("/proc")))),
              f"the role's CLI is running in its pane: {before}")
        return before

    def record_conversation(self, config, conversation: str) -> None:
        """A recorded conversation for the role, with the transcript Claude Code keeps for it: the launcher then
        compares a caller's pane session id with it, which is where sapi's first failure came from."""
        from scripts import team_launcher as launcher

        role = config.roles[0]
        sessions = launcher.role_session_dir(config, role)
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / launcher.session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": conversation}))
        transcript = Path.home() / ".claude" / "projects" / str(role.workdir).replace("/", "-") / f"{conversation}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "user", "sessionId": conversation, "cwd": str(role.workdir)}) + "\n")

    def restart(self, config, path) -> tuple[int, list[str]]:
        """`switchyard restart-role p752 app`, from the Director's pane this process stands in for."""
        from scripts import role_restart, role_self_restart
        from scripts import team_launcher as launcher
        from scripts.ticket_board.peer_identity import read_process

        def opener(url: str, timeout: float = 0):
            pane = read_process(launcher.pane_pid_for_role(config.roles[0])) \
                if tmux("has-session", "-t", "p752-app").returncode == 0 else None
            body = {
                "workflow": {"document": {"roles": [{"name": "app", "active": True}]}},
                "runtime-assignments": {"assignments": {"app": {
                    "process_pid": pane.pid if pane else None, "process_start_time": pane.start_time if pane else None,
                    "generation": 2}}},
                "reservations": {"reservations": {"app": None}},
                "execution-leases": {"leases": []},
                "provider-holds": {"holds": []},
            }[url.split("/api/", 1)[1]]
            return io.BytesIO(json.dumps(body).encode())

        said: list[str] = []
        # Who may ask is role_restart_test's (the control role's registered pane, or root's runuser): stood in here.
        with mock.patch.dict(os.environ, CALLER), \
                mock.patch.object(role_restart, "caller_authority", lambda *a, **k: ""), \
                mock.patch.object(role_restart, "REGISTER_TIMEOUT_SECONDS", 15.0), \
                mock.patch.object(launcher, "default_pane_state_dir_for_user", lambda *a, **k: self.panes), \
                mock.patch.object(role_self_restart, "lock_path", lambda c, r: self.tmp / "locks" / f"{r.role}.lock"):
            code = role_restart.restart_role_command(config, config_path=path, role_name="app", runner=subprocess.run,
                                                     opener=opener, print_func=said.append)
        return code, said

    def replacement_env(self, config) -> tuple[object, dict[str, str]]:
        from scripts import role_restart
        from scripts import team_launcher as launcher
        from scripts.ticket_board.peer_identity import read_process

        if tmux("has-session", "-t", "p752-app").returncode != 0:
            return None, {}
        after = read_process(launcher.pane_pid_for_role(config.roles[0]))
        env: dict[str, str] = {}
        wait_for(lambda: bool(env.update(role_restart.pane_environment(after.pid, "app", Path("/proc")))
                              or env.get("TICKET_BOARD_CALLER_ROLE") == "app"), 5.0)
        return after, env

    def reset(self) -> None:
        tmux("kill-server")
        wait_for(lambda: tmux("list-sessions").returncode != 0, 5.0)


def test_director_to_claude_and_codex(live: Live) -> None:
    for runtime in ("claude", "codex"):
        config, path = live.config(runtime)
        if runtime == "claude":
            live.record_conversation(config, "app-conversation")
        before = live.start(config)
        code, said = live.restart(config, path)
        after, env = live.replacement_env(config)
        check(code == 0 and after is not None and (after.pid, after.start_time) != (before.pid, before.start_time),
              f"Director -> {runtime}: restarted and registered as a new process: {before} -> {after} {said}")
        leaked = sorted(key for key, value in CALLER.items() if env.get(key) == value)
        check(not leaked, f"Director -> {runtime}: none of the Director's identity or nesting values reached it: {leaked}")
        from scripts import launch_environment
        nesting = sorted(key for key in env if launch_environment.is_runtime_nesting(key)
                         and key != "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK")
        check(not nesting, f"Director -> {runtime}: no runtime nesting variable at all: {nesting}")
        own = {"TICKET_BOARD_CALLER_ROLE": "app", "TICKET_BOARD_PANE_TARGET": "p752-app:0.0"}
        if runtime == "claude":
            own["TICKET_BOARD_PANE_SESSION_ID"] = "app-conversation"
        check(all(env.get(key) == value for key, value in own.items()),
              f"Director -> {runtime}: its pane identity is its own, as its pane command sets it: "
              f"{ {key: env.get(key) for key in own} }")
        if runtime == "claude":
            check(env.get("CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK") == "1",
                  "only what the target is configured with: the launcher's own Claude setting")
        live.reset()


def test_a_replacement_that_fails_to_start(live: Live) -> None:
    config, path = live.config("claude")
    before = live.start(config)
    live.failures.write_text("1")  # the next start fails; the one after works
    code, said = live.restart(config, path)
    after, _env = live.replacement_env(config)
    check(code == 0 and after is not None and after.pid != before.pid
          and any("on a second start" in s for s in said),
          f"a replacement that failed once after the old pane ended: one bounded second start recovers it: {said}")
    live.reset()

    config, path = live.config("claude")
    live.start(config)
    live.failures.write_text("5")  # every start fails
    code, said = live.restart(config, path)
    text = " ".join(said)
    check(code == 1 and tmux("has-session", "-t", "p752-app").returncode != 0
          and "app is stopped: its old pane was ended" in text and "`switchyard restart-role p752 app` starts it again" in text
          and "nothing was changed" not in text.lower(),
          f"one that fails again: stopped, said so, never 'nothing changed': {said}")
    live.failures.write_text("0")
    code, said = live.restart(config, path)
    after, _env = live.replacement_env(config)
    check(code == 0 and after is not None and any(s.startswith("switchyard: started app") for s in said),
          f"and the command it names starts it again: {said}")
    live.reset()


def test_the_display_slot_shows_the_replacement(live: Live) -> None:
    from scripts import presentation_controller as pc

    config, path = live.config("claude", presentation=True)
    live.start(config)
    pc._configure_display_session(config, 0, "app", runner=subprocess.run, presentation_ttys=set())

    def attached() -> bool:  # independent of the code under test: the slot's pane tty is a client of the role
        pane = tmux("display-message", "-p", "-t", "=p752-display-0:0.0", "#{pane_dead} #{pane_tty}").stdout.split()
        clients = tmux("list-clients", "-t", "=p752-app", "-F", "#{client_tty}").stdout.split()
        return len(pane) == 2 and pane[0] == "0" and pane[1] in clients

    check(wait_for(attached), "before: display slot 0 shows app")
    outer = subprocess.Popen(["script", "-qfc", "tmux attach -t =p752-display-0", "/dev/null"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             # `script -c` runs it with $SHELL, and zsh reads `=name` as a command path.
                             env={**os.environ, "TERM": "xterm-256color", "SHELL": "/bin/sh"})
    try:
        check(wait_for(lambda: bool(tmux("list-clients", "-t", "=p752-display-0").stdout.strip())),
              "the window's pane: a real outer client on the display session")
        code, said = live.restart(config, path)
        check(code == 0 and any("display slot(s) 0 show app again" in s for s in said) and wait_for(attached, 5.0),
              f"after the restart, slot 0 shows the replacement, as tmux reports it: {said}")
    finally:
        outer.kill()
        outer.wait()
    live.reset()


def main() -> int:
    live = Live()
    try:
        for name, case in sorted(globals().items()):
            if name.startswith("test_") and callable(case):
                case(live)
    finally:
        live.reset()
    print(f"role_restart_launch_live_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
