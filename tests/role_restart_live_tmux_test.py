#!/usr/bin/env python3
"""SYRD-750, live: `restart-role` on a real tmux server ends the old pane process, starts one replacement, and the
environment the launcher gives the role now -- a report link provisioned after it started -- reaches it.

The tmux server is isolated (TMUX removed, TMUX_TMPDIR in a temporary directory, and its socket asserted before
anything is created or killed); the launcher's start and reload are the real ones; the provider is a stand-in `claude`
that only runs; the kernel's /proc is the real one. The board's GETs are stood in: the role's registration is the
process tmux says runs its pane, as a pane's own registration would make it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

CHECKS = 0
TOKEN = "/home/owner/.config/p750/report-credentials/up.env"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="s750live."))
    # A home of its own: the launcher's records and the stand-in transcript land here, never in the caller's home.
    for name, sub in (("HOME", "home"), ("XDG_STATE_HOME", "home/.local/state"), ("XDG_CONFIG_HOME", "home/.config")):
        (tmp / sub).mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(tmp / sub)
    os.environ.pop("TMUX", None)
    os.environ["TMUX_TMPDIR"] = str(tmp / "tmux")
    (tmp / "tmux").mkdir()
    expected_socket = tmp / "tmux" / f"tmux-{os.getuid()}" / "default"
    bindir = tmp / "bin"
    bindir.mkdir()
    # A stand-in that only runs, keeping its arguments where /proc shows them.
    # Absolute paths throughout -- the interpreter and the CLI itself -- so no PATH a pane is given can change what runs.
    (bindir / "claude").write_text("#!/bin/sh\nif [ \"$1\" = --version ]; then echo '2.1.318 (Claude Code)'; exit 0; fi\n"
                                   f"exec {sys.executable} -c 'import time; time.sleep(600)' \"$@\"\n")
    (bindir / "claude").chmod(0o755)
    os.environ["PATH"] = f"{bindir}:{os.environ['PATH']}"

    from scripts import role_restart, role_self_restart
    from scripts import team_launcher as launcher
    from scripts.ticket_board.notify_listener import PaneHookStateStore
    from scripts.ticket_board.peer_identity import read_process

    path = tmp / "p750.json"
    doc = {"project": "p750", "board_url": "http://127.0.0.1:1/",
           "roles": [{"role": "app", "slot": 0, "workdir": str(tmp / "app"), "cli": [str(bindir / "claude")]}]}
    (tmp / "app").mkdir()
    path.write_text(json.dumps(doc))
    config = launcher.load_project_config("p750", path)
    role = config.roles[0]
    panes = tmp / "panes"
    panes.mkdir()
    try:
        started = launcher.ensure_visible_role_session_for_viewer(
            role, mode="attach-or-start", session_dir=launcher.role_session_dir(config, role), pane_state_dir=panes,
            bin_user="", runner=subprocess.run)
        socket = subprocess.run(["tmux", "display-message", "-p", "#{socket_path}"], capture_output=True, text=True).stdout.strip()
        why = "" if started == 0 else subprocess.run(  # what tmux saw, when the start failed: the run says why
            ["tmux", "list-panes", "-a", "-F", "#{session_name} dead=#{pane_dead} status=#{pane_dead_status} cmd=#{pane_start_command}"],
            capture_output=True, text=True).stdout.strip()
        check(started == 0 and socket == str(expected_socket),
              f"the role started, on the isolated server only: {socket} (launcher exit {started}; {why or 'no pane'})")
        before = read_process(launcher.pane_pid_for_role(role))
        old_tree: dict[int, int] = {}
        import time
        for _ in range(50):  # its command starting under the pane's shell
            first = role_restart.pane_environment(before.pid, "app", Path("/proc")) if before else {}
            if first.get("TICKET_BOARD_CALLER_ROLE") == "app":
                break
            time.sleep(0.1)
        old_tree = role_restart.pane_tree(before.pid, Path("/proc")) if before else {}
        check(before is not None and first.get("TICKET_BOARD_CALLER_ROLE") == "app"
              and "TICKET_BOARD_TENANT_REPORT_TOKEN_FILE" not in first,
              f"the pane's command started before any report link: {before} {sorted(first)[:4]}")
        PaneHookStateStore(panes).write(role.target, "idle", source="claude.Stop")
        # The conversation the role holds now: the restart must resume exactly it.
        sessions_dir = launcher.role_session_dir(config, role)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / launcher.session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "conv-750"}))
        # Its transcript, where Claude Code keeps one for this working directory: the launcher resumes only a
        # conversation that still exists, and starts fresh otherwise (its own rule, kept).
        transcript = Path.home() / ".claude" / "projects" / str(role.workdir).replace("/", "-") / "conv-750.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "user", "sessionId": "conv-750", "cwd": str(role.workdir)}) + "\n")

        # The link is provisioned after the pane started: only the next start of the pane carries it.
        doc.update({"upstream_report_url": "http://127.0.0.1:2/", "upstream_report_token_file": TOKEN})
        path.write_text(json.dumps(doc))
        config = launcher.load_project_config("p750", path)

        def opener(url: str, timeout: float = 0):
            pane = read_process(launcher.pane_pid_for_role(config.roles[0]))
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
        # Who may ask is proved by role_restart_test (the control role's registered pane, or root's runuser); this
        # process is neither, so that one check is stood in here and nothing else.
        with mock.patch.object(role_restart, "caller_authority", lambda *a, **k: ""), \
                mock.patch.object(launcher, "default_pane_state_dir_for_user", lambda *a, **k: panes), \
                mock.patch.object(role_self_restart, "lock_path", lambda c, r: tmp / "locks" / f"{r.role}.lock"):
            code = role_restart.restart_role_command(config, config_path=path, role_name="app", runner=subprocess.run,
                                                     opener=opener, print_func=said.append)
        after = read_process(launcher.pane_pid_for_role(config.roles[0]))
        env = role_restart.pane_environment(after.pid, "app", Path("/proc")) if after else {}
        sessions = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True).stdout.split()
        # Ended, from the kernel: gone, its pid reused, or a zombie. Under a subreaper (switchyard-bounded is one), the
        # old pane's processes outlive the tmux server that ran them as zombies until it collects them -- ended all the
        # same; only their exit status is left.
        lingering = sorted(pid for pid, start in old_tree.items() if not role_restart.ended(pid, start, Path("/proc")))
        check(code == 0 and after is not None and (after.pid, after.start_time) != (before.pid, before.start_time)
              and len(old_tree) >= 2 and not lingering and any("which has ended" in s for s in said),
              f"a new pane process replaced the old one, whose every process has ended: {before} -> {after} "
              f"{old_tree} lingering {lingering} {said}")
        check(env.get("TICKET_BOARD_TENANT_REPORT_TOKEN_FILE") == TOKEN and env.get("TICKET_BOARD_REPORT_URL") == "http://127.0.0.1:2/",
              f"the report link provisioned after the old pane started reached the replacement: {sorted(k for k in env if 'REPORT' in k)}")
        check(sessions.count(role.target.split(":")[0]) == 1,
              f"exactly one session for the role: no duplicate pane was started: {sessions}")
        check(any(s.startswith("switchyard: restarted app: pid ") and "conv-750" in s for s in said), f"it says so: {said}")
        provider = next((pid for pid in (int(e.name) for e in Path("/proc").iterdir() if e.name.isdigit())
                         if role_restart.environment_of(pid, Path("/proc")) == env), None)
        argv = (Path("/proc") / str(provider) / "cmdline").read_bytes().split(b"\0") if provider else []
        check(env.get("TICKET_BOARD_PANE_SESSION_ID") == "conv-750" and b"--resume" in argv
              and argv[argv.index(b"--resume") + 1] == b"conv-750",
              f"the replacement resumes the recorded conversation, so nothing of its context is lost: {argv[-4:]}")
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True)
    print(f"role_restart_live_tmux_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
