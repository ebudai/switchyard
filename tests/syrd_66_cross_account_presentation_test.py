#!/usr/bin/env python3
"""Focused SYRD-66 compatibility and authority regressions."""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import presentation_controller as presentation
from scripts import team_launcher
from scripts.ticket_board import notify_listener
from scripts.ticket_board import project_provision as provision


def _config(tmp: Path, *, isolated: bool) -> team_launcher.ProjectConfig:
    role = team_launcher.RoleConfig(
        role="app",
        slot=0,
        detached=False,
        target="syrd-app:0.0",
        tmux_session="syrd-app",
        workdir=str(tmp / "worktrees" / "app"),
        cli=["codex"],
        model="",
        model_arg="",
        effort="",
        yolo=False,
        extra_args=[],
        resume_mode="none",
        resume_flag="",
        resume_subcommand="",
        fresh_session_per_ticket=False,
        live_commands=["codex"],
        env={},
        run_as_user="syrd-app",
    )
    return team_launcher.ProjectConfig(
        project="syrd",
        project_name="Switchyard",
        ticket_prefix="SYRD",
        layout=tmp / "layout.json",
        session_dir=tmp / "sessions",
        board_url="http://127.0.0.1:8771",
        board_socket="/run/syrd-ticket-board/board.sock",
        upstream_report_url="",
        upstream_report_token_file="",
        run_as_user="syrd-agent",
        pane_launcher=None,
        repository=tmp / "repo",
        control_repository=tmp / "repo.git",
        worktree_base=tmp / "worktrees",
        worktree_remote="origin",
        worktree_branch="main",
        roles=[role],
        role_state_isolation=isolated,
    )


def test_migrated_display_proxy_uses_only_configured_role_tmux() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-proxy.") as raw:
        config = _config(Path(raw), isolated=False)
        _workdir, command = presentation._proxy_command(
            config, "app", {"live": True}, observer=False
        )
    script = shlex.split(command)[2]
    # The privilege crossing names tmux itself, never a shell or arbitrary
    # caller-selected command.
    privileged = shlex.split(script.split(";", 1)[0])
    assert privileged == [
        "env", "TMUX=", "/usr/bin/sudo", "-n", "-u", "syrd-app", "/usr/bin/tmux",
        "attach", "-f", "!no-detach-on-destroy", "-t", "=syrd-app",
    ]


def test_cross_account_worker_transport_has_no_tmux_command_keys() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-lock.") as raw:
        config = _config(Path(raw), isolated=False)
        role = config.roles[0]
        assert presentation._proxy_crosses_account(config, role)
        assert presentation.display_lock_commands(role.tmux_session) == (
            ["tmux", "set-option", "-t", "=syrd-app:", "prefix", "None"],
            ["tmux", "set-option", "-t", "=syrd-app:", "prefix2", "None"],
            [
                "tmux", "set-option", "-t", "=syrd-app:", "key-table",
                presentation.DISPLAY_KEY_TABLE,
            ],
        )


def test_missing_cross_account_control_fails_before_display_mutation() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-partial.") as raw:
        config = _config(Path(raw), isolated=False)
        calls: list[list[str]] = []

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1, stderr="sudo denied")
            return subprocess.CompletedProcess(args, 0)

        try:
            presentation._configure_display_session(
                config, 0, "app", runner=runner, presentation_ttys=set()
            )
        except RuntimeError as exc:
            assert "could not query app's configured server: sudo denied" in str(exc)
        else:
            raise AssertionError("partial role-control installation was accepted")
    assert not any("syrd-display-0" in part for call in calls for part in call), calls


def test_shared_and_process_authority_proxies_need_no_role_bridge() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-shared.") as raw:
        base = _config(Path(raw), isolated=False)
        shared_role = replace(base.roles[0], run_as_user="syrd-agent")
        for config in (
            replace(base, roles=[shared_role]),
            replace(base, role_state_isolation=True),
        ):
            _workdir, command = presentation._proxy_command(
                config, "app", {"live": True}, observer=True
            )
            script = shlex.split(command)[2]
            attach = shlex.split(script.split(";", 1)[0])
            assert "/usr/bin/sudo" not in attach, script
            assert attach == [
                "env", "TMUX=", "tmux", "attach", "-f",
                "ignore-size,!no-detach-on-destroy", "-t", "=syrd-app",
            ]


def test_existing_role_worktree_git_uses_its_actual_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-worktree.") as raw:
        config = _config(Path(raw), isolated=False)
        target = Path(config.roles[0].workdir)
        target.mkdir(parents=True)
        calls: list[list[str]] = []
        original_current = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "syrd-agent"

            def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)

            proc = team_launcher.run_owner_correct_git(
                ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
                runner=runner,
                owner_rules=team_launcher._config_git_owner_rules(config),
            )
        finally:
            team_launcher.current_user_name = original_current
    assert proc.returncode == 0
    assert calls == [[
        "sudo", "-u", "syrd-app", "git", "-C", str(target),
        "rev-parse", "--is-inside-work-tree",
    ]]


def test_symlink_worktree_is_refused_before_git_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-symlink.") as raw:
        root = Path(raw)
        outside = root / "outside"
        outside.mkdir()
        target = root / "worktrees" / "app"
        target.parent.mkdir()
        target.symlink_to(outside, target_is_directory=True)
        calls: list[list[str]] = []

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        proc = team_launcher.run_owner_correct_git(
            ["git", "-C", str(target), "status"],
            runner=runner,
            owner_rules=[team_launcher.GitOwnerRule(target.parent, "syrd-agent")],
        )
    assert proc.returncode == 125
    assert calls == []
    assert "refusing symlink git target" in str(proc.stderr)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_notification_probes_follow_exact_installed_role_map() -> None:
    backend = RecordingRunner()
    runner = notify_listener.role_aware_tmux_runner(
        environ={
            "TICKET_BOARD_PROJECT": "syrd",
            "TICKET_BOARD_ROLE_ACCOUNTS": "director=syrd-director,app=syrd-app",
        },
        runner=backend,
    )
    assert notify_listener.tmux_target_exists("syrd-app:0.0", runner=runner) is True
    assert backend.calls == [[
        "sudo", "-n", "-u", "syrd-app", "/usr/bin/tmux",
        "has-session", "-t", "syrd-app:0.0",
    ]]


def test_notification_probe_refuses_wrong_role_and_cross_tenant() -> None:
    backend = RecordingRunner()
    runner = notify_listener.role_aware_tmux_runner(
        environ={
            "TICKET_BOARD_PROJECT": "syrd",
            "TICKET_BOARD_ROLE_ACCOUNTS": "app=syrd-app",
        },
        runner=backend,
    )
    for target in ("syrd-main:0.0", "other-app:0.0"):
        proc = runner(["tmux", "capture-pane", "-p", "-t", target])
        assert proc.returncode == 125
        try:
            runner(["tmux", "capture-pane", "-p", "-t", target], check=True)
        except subprocess.CalledProcessError as exc:
            assert exc.returncode == 125
        else:
            raise AssertionError("check=True did not fail closed")
    assert backend.calls == []


def test_shared_account_and_process_authority_keep_direct_tmux_path() -> None:
    for environ in (
        {"TICKET_BOARD_PROJECT": "pgu"},
        {
            "TICKET_BOARD_PROJECT": "syrd",
            "TICKET_BOARD_PROCESS_AUTHORITY": "1",
            # A stale map cannot override the atomic process-authority switch.
            "TICKET_BOARD_ROLE_ACCOUNTS": "app=syrd-app",
        },
    ):
        backend = RecordingRunner()
        runner = notify_listener.role_aware_tmux_runner(environ=environ, runner=backend)
        runner(["tmux", "display-message", "-p", "-t", "syrd-app:0.0", "#{pane_pid}"])
        assert backend.calls == [[
            "tmux", "display-message", "-p", "-t", "syrd-app:0.0", "#{pane_pid}",
        ]]


def test_units_select_one_authority_topology_atomically() -> None:
    modern = provision.build_plan(project="syrd", owner_user="syrd-agent")
    legacy = replace(
        modern,
        role_accounts=(("director", "syrd-director"), ("app", "custom-app")),
        roles_group="syrd-roles",
    )
    for render in (provision.render_board_unit, provision.render_listener_unit):
        modern_unit = render(modern)
        assert "Environment=TICKET_BOARD_PROCESS_AUTHORITY=1" in modern_unit
        assert "TICKET_BOARD_ROLE_ACCOUNTS" not in modern_unit

        legacy_unit = render(legacy)
        assert "Environment=TICKET_BOARD_PROCESS_AUTHORITY=1" not in legacy_unit
        assert (
            "Environment=TICKET_BOARD_ROLE_ACCOUNTS="
            "director=syrd-director,app=custom-app"
        ) in legacy_unit


def test_legacy_role_process_exports_same_declarative_account_map() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-role-env.") as raw:
        legacy = _config(Path(raw), isolated=False)
        env = team_launcher._role_board_env(legacy, legacy.roles[0], {})
        assert env["TICKET_BOARD_ROLE_ACCOUNTS"] == "app=syrd-app"
        assert "TICKET_BOARD_PROCESS_AUTHORITY" not in env

        modern = replace(legacy, role_state_isolation=True)
        modern_env = team_launcher._role_board_env(modern, modern.roles[0], {})
        assert modern_env["TICKET_BOARD_PROCESS_AUTHORITY"] == "1"
        assert "TICKET_BOARD_ROLE_ACCOUNTS" not in modern_env

        shared_role = replace(legacy.roles[0], run_as_user="")
        shared = replace(legacy, project="pgu", run_as_user="agent", roles=[shared_role])
        shared_env = team_launcher._role_board_env(shared, shared_role, {})
        assert "TICKET_BOARD_ROLE_ACCOUNTS" not in shared_env
        assert "TICKET_BOARD_PROCESS_AUTHORITY" not in shared_env


def test_invalid_or_duplicate_role_map_fails_before_any_probe() -> None:
    for value in ("app=../../root", "app=syrd-app,app=syrd-main", "app"):
        try:
            notify_listener.role_aware_tmux_runner(
                environ={
                    "TICKET_BOARD_PROJECT": "syrd",
                    "TICKET_BOARD_ROLE_ACCOUNTS": value,
                }
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid authority map {value!r}")


def test_directorctl_capture_uses_role_map_and_refuses_foreign_target() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-directorctl.") as raw:
        root = Path(raw)
        bindir = root / "bin"
        bindir.mkdir()
        log = root / "calls"
        fake_id = bindir / "id"
        fake_id.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -un ]; then echo syrd-agent; exit 0; fi\n"
            "if [ \"$1\" = -u ] && [ \"$2\" = custom-app ]; then echo 4242; exit 0; fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        fake_sudo = bindir / "sudo"
        fake_sudo.write_text(
            "#!/bin/sh\n"
            "printf 'sudo:%s\\n' \"$*\" >> \"$DIRECTORCTL_TEST_LOG\"\n"
            "printf 'mapped pane\\n'\n",
            encoding="utf-8",
        )
        fake_tmux = bindir / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "printf 'tmux:%s\\n' \"$*\" >> \"$DIRECTORCTL_TEST_LOG\"\n"
            "printf 'direct pane\\n'\n",
            encoding="utf-8",
        )
        fake_id.chmod(0o755)
        fake_sudo.chmod(0o755)
        fake_tmux.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bindir}:/usr/bin:/bin",
            "TICKET_BOARD_PROJECT": "syrd",
            "TICKET_BOARD_ROLE_ACCOUNTS": "app=custom-app",
            "DIRECTORCTL_TEST_LOG": str(log),
        }
        mapped = subprocess.run(
            [str(ROOT / "scripts" / "directorctl"), "capture", "app", "1"],
            env=env,
            text=True,
            capture_output=True,
        )
        assert mapped.returncode == 0, mapped.stderr
        assert mapped.stdout.strip() == "mapped pane"
        assert log.read_text(encoding="utf-8").splitlines() == [
            "sudo:-n -u custom-app tmux capture-pane -p -J -t syrd-app:0.0"
        ]

        before = log.read_text(encoding="utf-8")
        foreign = subprocess.run(
            [str(ROOT / "scripts" / "directorctl"), "capture", "other-app:0.0", "1"],
            env=env,
            text=True,
            capture_output=True,
        )
        assert foreign.returncode != 0
        assert "refusing foreign tmux target" in foreign.stderr
        assert log.read_text(encoding="utf-8") == before


def test_directorctl_staged_payload_is_group_readable_for_role_accounts() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd66-payload.") as raw:
        root = Path(raw)
        bindir = root / "bin"
        payload_dir = root / "payloads"
        bindir.mkdir()
        payload_dir.mkdir()
        log = root / "calls"
        capture_count = root / "capture-count"
        capture_count.write_text("0\n", encoding="utf-8")
        source = root / "message.txt"
        message = "cross-account long payload " + ("x" * 220)
        source.write_text(message, encoding="utf-8")

        fake_id = bindir / "id"
        fake_id.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  -un) echo syrd-director ;;\n"
            "  '-u syrd-app') echo 1011 ;;\n"
            "  -Gn) echo 'syrd-director syrd-roles' ;;\n"
            "  '-Gn syrd-app') echo 'syrd-app syrd-roles' ;;\n"
            "  *) exec /usr/bin/id \"$@\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_chgrp = bindir / "chgrp"
        fake_chgrp.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf 'chgrp:%s\\n' \"$*\" >> \"$DIRECTORCTL_TEST_LOG\"\n"
            "[ \"$1\" = -- ] && [ \"$2\" = syrd-roles ]\n"
            "exec /usr/bin/chgrp -- \"$(/usr/bin/id -gn)\" \"$3\"\n",
            encoding="utf-8",
        )
        fake_sudo = bindir / "sudo"
        fake_sudo.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf 'sudo:%s\\n' \"$*\" >> \"$DIRECTORCTL_TEST_LOG\"\n"
            "[ \"$1\" = -n ] && [ \"$2\" = -u ]\n"
            "shift 3\n"
            "exec \"$@\"\n",
            encoding="utf-8",
        )
        fake_tmux = bindir / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf 'tmux:%s\\n' \"$*\" >> \"$DIRECTORCTL_TEST_LOG\"\n"
            "case \"$1\" in\n"
            "  display-message) echo 0 ;;\n"
            "  send-keys) : ;;\n"
            "  capture-pane)\n"
            "    count=$(cat \"$DIRECTORCTL_CAPTURE_COUNT\")\n"
            "    count=$((count + 1))\n"
            "    printf '%s\\n' \"$count\" > \"$DIRECTORCTL_CAPTURE_COUNT\"\n"
            "    if [ \"$count\" -eq 1 ]; then echo before; else echo Working; fi\n"
            "    ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        for helper in (fake_id, fake_chgrp, fake_sudo, fake_tmux):
            helper.chmod(0o755)

        env = {
            **os.environ,
            "PATH": f"{bindir}:/usr/bin:/bin",
            "TMPDIR": str(payload_dir),
            "TICKET_BOARD_PROJECT": "syrd",
            "TICKET_BOARD_ROLE_ACCOUNTS": "app=syrd-app",
            "DIRECTORCTL_ENTER_DELAY": "0",
            "DIRECTORCTL_TEST_LOG": str(log),
            "DIRECTORCTL_CAPTURE_COUNT": str(capture_count),
            "DIRECTORCTL_TICKET_NOTIFICATION_JOURNAL": str(root / "journal.jsonl"),
        }
        sent = subprocess.run(
            [str(ROOT / "scripts" / "directorctl"), "send-file", "app", str(source)],
            env=env,
            text=True,
            capture_output=True,
        )
        assert sent.returncode == 0, sent.stderr
        calls = log.read_text(encoding="utf-8")
        match = re.search(r"please read (\S+/directorctl_payload\.[^ ]+\.txt) ", calls)
        assert match, calls
        staged = Path(match.group(1))
        assert staged.parent == payload_dir
        assert staged.read_text(encoding="utf-8") == message
        assert stat.S_IMODE(staged.stat().st_mode) == 0o640
        assert staged.stat().st_mode & stat.S_IRGRP
        assert not staged.stat().st_mode & stat.S_IROTH
        assert f"chgrp:-- syrd-roles {staged}" in calls


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print("syrd_66_cross_account_presentation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
