#!/usr/bin/env python3
"""SYRD-43: a role pane that detaches must never reveal a privileged shell.

The live defect was a presentation window whose terminal emulator ran as root:
every tab was a root shell, the pane's tmux client was a child of it, and a
detach handed the tab back to that shell. These cover both halves -- the window
never starts privileged, and the tab's program never falls back to a shell --
by process ancestry and uid rather than by inspecting strings alone.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from team_launcher_test_helpers import *


PANE_WINDOW = ROOT / "scripts" / "switchyard-pane-window"


def _fake_process(proc_root: Path, pid: int, *, uid: int, argv: list[str]) -> None:
    entry = proc_root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    (entry / "status").write_text(f"Name:\t{Path(argv[0]).name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")


def _config_for(tmp: Path):
    config_path = _write_six_visible_role_config(tmp, project="porter")
    return team_launcher.load_project_config("porter", config_path), config_path


def test_a_root_invocation_crosses_to_the_desktop_account_before_konsole() -> None:
    """A terminal started by root is a root shell behind every tab."""
    desktop_user = team_launcher.current_user_name()
    original = team_launcher.os.geteuid
    try:
        team_launcher.os.geteuid = lambda: 0
        args = team_launcher.konsole_launch_args(
            Path("/tmp/porter-konsole-layout.json"),
            gui_user=desktop_user,
            window_title="porter",
        )
    finally:
        team_launcher.os.geteuid = original

    assert args[:4] == ["sudo", "-u", desktop_user, "-H"], args
    konsole = next(index for index, part in enumerate(args) if Path(part).name == "konsole")
    assert args.index("sudo") < konsole, args
    # The environment is emptied at the transition and rebuilt from the
    # allowlist; the execution-level case proves nothing survives it.
    assert args[args.index("env") + 1] == "-i", args
    assignments = [part for part in args if "=" in part and not part.startswith("-")]
    assert {name.split("=", 1)[0] for name in assignments} <= set(
        team_launcher.GUI_ENVIRONMENT_ALLOWLIST
    ), assignments
    assert any(name.startswith("PATH=") for name in assignments), assignments


def test_no_privileged_variable_survives_the_identity_transition() -> None:
    """Execute the launch and read the desktop process's real environment.

    Inspecting argv cannot see what is inherited. sudo's env_reset is the
    host's policy, not this project's: a site whose sudoers keeps variables
    would carry them from root into the desktop account, and -H leaves more
    set. The command therefore empties the environment itself, and this proves
    it by running a permissive stand-in for sudo -- one that preserves
    everything, which is the worst case -- and dumping what the program at the
    end actually receives.
    """
    with tempfile.TemporaryDirectory(prefix="gui-env-boundary.") as tmp:
        tmp_path = Path(tmp)
        stub_bin = tmp_path / "bin"
        stub_bin.mkdir()
        sudo_env = tmp_path / "sudo.env"
        konsole_env = tmp_path / "konsole.env"
        (stub_bin / "sudo").write_text(
            "#!/bin/sh\n"
            "# Permissive stand-in: keeps the whole environment, as a host's\n"
            "# sudoers env_keep may. Nothing here resets anything.\n"
            f"/usr/bin/env > {shlex.quote(str(sudo_env))}\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -u) shift 2 ;;\n"
            "    -H) shift ;;\n"
            "    --) shift; break ;;\n"
            "    *) break ;;\n"
            "  esac\n"
            "done\n"
            'exec "$@"\n',
            encoding="utf-8",
        )
        (stub_bin / "konsole").write_text(
            "#!/bin/sh\n"
            # Absolute: the emptied PATH is the point of this case, so the stub
            # cannot rely on finding anything itself.
            f"/usr/bin/env > {shlex.quote(str(konsole_env))}\n"
            "exit 0\n",
            encoding="utf-8",
        )
        for name in ("sudo", "konsole"):
            (stub_bin / name).chmod(0o755)

        desktop_user = team_launcher.current_user_name()
        sentinels = {
            "SWITCHYARD_ROOT_SENTINEL": "must-not-cross",
            "AWS_SECRET_ACCESS_KEY": "must-not-cross",
            "SUDO_ASKPASS": "/root/askpass",
            "LD_PRELOAD": "/root/evil.so",
            "XAUTHORITY": "/root/.Xauthority",
        }
        original_geteuid = team_launcher.os.geteuid
        original_base_path = team_launcher.DEFAULT_PANE_BASE_PATH
        try:
            team_launcher.os.geteuid = lambda: 0
            team_launcher.DEFAULT_PANE_BASE_PATH = str(stub_bin)
            args = team_launcher.konsole_launch_args(
                tmp_path / "layout.json", gui_user=desktop_user, window_title="porter"
            )
        finally:
            team_launcher.os.geteuid = original_geteuid
            team_launcher.DEFAULT_PANE_BASE_PATH = original_base_path

        assert args[0] == "sudo", args
        assert args[args.index("env") + 1] == "-i", args
        environment = {**os.environ, **sentinels, "PATH": f"{stub_bin}:{os.environ.get('PATH', '')}"}
        result = subprocess.run(args, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

        assert konsole_env.exists(), result.stderr
        assert sudo_env.exists(), result.stderr

        def parsed(path: Path) -> dict[str, str]:
            values: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    name, value = line.split("=", 1)
                    values[name] = value
            return values

        # The sentinels really were present at the transition, so their absence
        # afterwards is the command's doing and not the fixture's.
        crossing = parsed(sudo_env)
        assert all(crossing.get(name) == value for name, value in sentinels.items()), crossing

        received = parsed(konsole_env)
        for name in sentinels:
            assert name not in received, (name, received)
        assert set(received) <= set(team_launcher.GUI_ENVIRONMENT_ALLOWLIST) | {"_", "PWD", "SHLVL"}, received
        assert received["HOME"] == team_launcher._gui_home(desktop_user), received
        assert received["USER"] == desktop_user and received["LOGNAME"] == desktop_user, received
        assert received["QT_QPA_PLATFORM"] == "wayland", received


def test_a_root_invocation_without_a_desktop_account_refuses_to_open_anything() -> None:
    """Fail closed: no identity to drop to means no window, not a root window."""
    original = team_launcher.os.geteuid
    launched: list[list[str]] = []
    try:
        team_launcher.os.geteuid = lambda: 0
        args = team_launcher.konsole_launch_args(Path("/tmp/x.json"), gui_user="root")
        assert "konsole" not in " ".join(args), args
        assert args[0] == "sh", args
        with tempfile.TemporaryDirectory(prefix="root-refusal.") as tmp:
            result = team_launcher.launch_konsole_window(
                Path(tmp) / "layout.json",
                project="porter",
                gui_user="root",
                runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1),
                process_launcher=lambda *a, **kw: launched.append(list(a[0])),
            )
    finally:
        team_launcher.os.geteuid = original
    assert result != 0
    assert launched == [], launched


def test_a_detached_pane_reaches_no_shell_and_runs_nothing_pasted() -> None:
    """Run the real pane program: after its client exits, nothing executes input."""
    with tempfile.TemporaryDirectory(prefix="pane-detach.") as tmp:
        marker = Path(tmp) / "pasted-command-ran"
        proc = subprocess.Popen(
            [str(PANE_WINDOW), "sh", "-c", "exit 7"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            # Exactly what a paste into a fallback shell would be.
            proc.stdin.write(f"touch {shlex.quote(str(marker))}\n")
            proc.stdin.flush()
            deadline = time.time() + 10
            comm = ""
            while time.time() < deadline:
                comm = Path(f"/proc/{proc.pid}/comm").read_text(encoding="utf-8").strip()
                if comm == "sleep":
                    break
                time.sleep(0.1)
            assert proc.poll() is None, "the pane exited instead of staying inert"
            # The wrapper execs, so no shell is left in the process at all: the
            # pane IS the inert wait, and it has no children to hand input to.
            assert comm == "sleep", comm
            assert _descendant_commands(proc.pid) == [], _descendant_commands(proc.pid)
            time.sleep(0.5)
            assert not marker.exists(), "input reached something that could run it"
        finally:
            proc.kill()
            proc.wait(timeout=5)


def _descendant_commands(pid: int) -> list[str]:
    found: list[str] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat_fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            parent = int(stat_fields[1])
            if parent != pid:
                continue
            found.append((entry / "comm").read_text(encoding="utf-8").strip())
        except (OSError, IndexError, ValueError):
            continue
    return found


def test_every_pane_leaf_runs_the_inert_pane_program() -> None:
    """The tab's program is the wrapper, not the attach command Konsole would fall out of."""
    with tempfile.TemporaryDirectory(prefix="pane-leaf.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _config_for(tmp_path)
        script_path = ROOT / "scripts" / team_launcher.TEAM_LAUNCHER_NAME
        output = tmp_path / "layout-out.json"
        team_launcher.materialize_layout(
            config,
            config_path=config_path,
            mode="attach-or-start",
            script_path=script_path,
            output_path=output,
        )
        commands = _leaf_commands(json.loads(output.read_text(encoding="utf-8")))
        assert commands, "no pane commands rendered"
        for command in commands:
            argv = shlex.split(command)
            assert argv[0] == str(PANE_WINDOW), argv
            assert "pane" in argv and "attach-or-start" in argv, argv


def test_a_failed_role_pane_stays_inert_instead_of_returning_to_a_shell() -> None:
    with tempfile.TemporaryDirectory(prefix="failed-role.") as tmp:
        config, _config_path = _config_for(Path(tmp))
        command = team_launcher.failed_role_command(config.roles[0], "checkout busy")
    assert "sleep 30" not in command, command
    assert command.startswith("sh -c "), command
    assert "exec sleep infinity" in command, command


def test_the_display_proxy_window_also_ends_inert() -> None:
    from scripts import presentation_controller

    with tempfile.TemporaryDirectory(prefix="proxy-leaf.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _config_for(tmp_path)
        layouts: list[Path] = []
        original_launch = team_launcher.launch_konsole_window
        try:
            team_launcher.launch_konsole_window = lambda output, **kwargs: (layouts.append(output) or 0)
            presentation_controller._launch_separate(
                config,
                {"slot_count": 2},
                config_path=config_path,
                output_path=tmp_path / "presentation-layout.json",
                runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
                process_launcher=None,
            )
        finally:
            team_launcher.launch_konsole_window = original_launch
        payload = json.loads((tmp_path / "presentation-layout.json").read_text(encoding="utf-8"))
        for command in _leaf_commands(payload):
            argv = shlex.split(command)
            assert Path(argv[0]).name == team_launcher.PANE_WINDOW_NAME, argv
            assert "attach" in argv, argv


def test_a_root_owned_presentation_window_is_detected_and_others_are_not() -> None:
    with tempfile.TemporaryDirectory(prefix="proc-scan.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _config_for(tmp_path)
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        _fake_process(proc_root, 101, uid=0, argv=["konsole", "--separate", "--layout", f"{tmp}/porter-konsole-layout.json"])
        _fake_process(proc_root, 102, uid=1000, argv=["konsole", "--separate", "--layout", f"{tmp}/porter-konsole-layout.json"])
        _fake_process(proc_root, 103, uid=0, argv=["konsole", "--separate", "--layout", "/somewhere/other-konsole-layout.json"])
        _fake_process(proc_root, 104, uid=0, argv=["sshd", "-D"])
        found = team_launcher.unsafe_root_presentation_windows(
            config, config_path=config_path, proc_root=proc_root
        )
        assert [window.pid for window in found] == [101], found
        report = team_launcher.unsafe_presentation_report(config, found)
        assert "NOT safely attached" in report
        assert "replace-window porter" in report
        # The real process table is readable and reports nothing for a project
        # whose window is not running.
        assert team_launcher.unsafe_root_presentation_windows(config, config_path=config_path) == []


def test_status_names_a_root_window_instead_of_reporting_the_project_attached() -> None:
    with tempfile.TemporaryDirectory(prefix="status-scan.") as tmp:
        tmp_path = Path(tmp)
        _config, config_path = _config_for(tmp_path)
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        (registry_dir / "porter.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "name": "Porter",
                    "slug": "porter",
                    "config_path": str(config_path),
                }
            ),
            encoding="utf-8",
        )
        empty_config_dir = tmp_path / "configs"
        empty_config_dir.mkdir()
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        _fake_process(proc_root, 201, uid=0, argv=["konsole", "--layout", f"{tmp}/porter-konsole-layout.json"])
        printed: list[str] = []
        original = team_launcher.PROC_ROOT
        try:
            team_launcher.PROC_ROOT = proc_root
            team_launcher.switchyard_status_command(
                config_dir=empty_config_dir,
                registry_dir=registry_dir,
                process_commands=[],
                runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", ""),
                print_func=printed.append,
            )
        finally:
            team_launcher.PROC_ROOT = original
        output = "\n".join(printed)
        assert "unsafe-root-window" in output, output
        assert "NOT safely attached" in output, output
        assert "sudo switchyard replace-window porter" in output, output


def test_replace_window_stops_only_the_window_and_leaves_workers_running() -> None:
    with tempfile.TemporaryDirectory(prefix="replace-window.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _config_for(tmp_path)
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        _fake_process(proc_root, 301, uid=0, argv=["konsole", "--layout", f"{tmp}/porter-konsole-layout.json"])
        signalled: list[tuple[int, int]] = []
        commands: list[list[str]] = []

        def signaller(pid: int, sig: int) -> None:
            signalled.append((pid, sig))
            (proc_root / str(pid) / "cmdline").unlink()

        def runner(argv, **kwargs):
            commands.append(list(argv))
            # every role's session is live and running its configured CLI
            if argv[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[:2] == ["tmux", "display-message"]:
                return subprocess.CompletedProcess(argv, 0, "codex\n", "")
            if argv[:2] == ["tmux", "list-panes"]:
                return subprocess.CompletedProcess(argv, 0, "codex\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        printed: list[str] = []
        launched: list[Path] = []
        original_launch = team_launcher.launch_konsole_window
        try:
            team_launcher.launch_konsole_window = lambda output, **kwargs: (launched.append(output) or 0)
            result = team_launcher.replace_presentation_window_command(
                config,
                config_path=config_path,
                proc_root=proc_root,
                euid_getter=lambda: 0,
                signaller=signaller,
                settle_seconds=0.2,
                runner=runner,
                print_func=printed.append,
            )
        finally:
            team_launcher.launch_konsole_window = original_launch

        assert result == 0, printed
        assert signalled == [(301, signal.SIGTERM)], signalled
        assert launched, "no replacement window was opened"
        assert not any(
            "kill-session" in " ".join(command) or "kill-server" in " ".join(command)
            for command in commands
        ), commands


def test_replace_window_refuses_when_it_is_not_root() -> None:
    with tempfile.TemporaryDirectory(prefix="replace-unpriv.") as tmp:
        tmp_path = Path(tmp)
        config, config_path = _config_for(tmp_path)
        proc_root = tmp_path / "proc"
        proc_root.mkdir()
        _fake_process(proc_root, 401, uid=0, argv=["konsole", "--layout", f"{tmp}/porter-konsole-layout.json"])
        signalled: list[tuple[int, int]] = []
        printed: list[str] = []
        result = team_launcher.replace_presentation_window_command(
            config,
            config_path=config_path,
            proc_root=proc_root,
            euid_getter=lambda: 1000,
            signaller=lambda pid, sig: signalled.append((pid, sig)),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", ""),
            print_func=printed.append,
        )
        assert result == 1
        assert signalled == []
        assert "must run as root" in "\n".join(printed)


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_unprivileged_presentation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
