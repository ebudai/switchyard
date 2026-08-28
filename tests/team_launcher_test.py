#!/usr/bin/env python3
"""Regression tests for the JSON-driven team launcher."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
REMOTE_HEAD = "0123456789abcdef0123456789abcdef01234567"
LOCAL_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ticket_board_pane_env import (
    TICKET_BOARD_PANE_ENV_KEYS,
    candidate_live_pane_session_paths,
    candidate_live_pane_state_paths,
    pane_state_record_anomalies,
    pane_state_record_anomaly_diff,
    snapshot_diff,
    snapshot_file_set_diff,
    snapshot_paths_file_set,
    snapshot_paths,
    strip_ticket_board_pane_env,
)
from standalone_test_runner import run_module_tests

LIVE_PANE_SESSION_PATHS = candidate_live_pane_session_paths()
LIVE_PANE_STATE_PATHS = candidate_live_pane_state_paths()
strip_ticket_board_pane_env(os.environ)

import scripts.team_launcher as team_launcher
from scripts.ticket_board.notify_listener import PaneHookStateStore
from scripts.team_launcher import (
    cli_command_for_role,
    clear_session_record_for_role,
    default_user_bin,
    deploy_launcher_checkout,
    ensure_configured_runtime_user,
    ensure_project_worktrees,
    ensure_user_linger_runtime,
    git_checkout_launcher_branch_args,
    git_clean_launcher_checkout_args,
    git_clean_shared_checkout_args,
    git_fast_forward_launcher_ref_args,
    git_fetch_launcher_ref_args,
    git_checkout_shared_ref_args,
    git_fetch_worktree_ref_args,
    git_launcher_ahead_behind_args,
    git_launcher_checkout_check_args,
    git_launcher_commit_exists_args,
    git_launcher_current_branch_args,
    git_launcher_head_args,
    git_launcher_head_short_args,
    git_launcher_ls_remote_ref_args,
    git_launcher_status_porcelain_args,
    git_shared_checkout_check_args,
    konsole_launch_args,
    launch_konsole_window,
    launch_project,
    live_command_matches_role,
    materialize_layout,
    design_project_command,
    load_project_config,
    load_project_design_artifact,
    new_project_command,
    pane_state_file_name,
    provision_runtime_command,
    run_detached_role,
    run_role_pane,
    seed_session_dir_from_legacy_sources,
    session_id_for_role,
    session_file_name,
    switchyard_main,
    switchyard_menu_command,
    switchyard_new_command,
    switchyard_register_command,
    tmux_has_session_args,
    worktree_ref,
)

LIVE_SESSION_DIR = team_launcher.DEFAULT_SESSION_DIR
LEGACY_SWITCHYARD_ROLE_CLIS = (
    ("designer", "claude"),
    ("director", "claude"),
    ("audit", "claude"),
    ("ops", "codex"),
    ("app", "codex"),
    ("main", "codex"),
)


class FakeRunner:
    def __init__(
        self,
        existing_sessions: set[str] | None = None,
        *,
        current_commands: dict[str, str | list[str]] | None = None,
        pane_pids: dict[str, int | list[int]] | None = None,
    ) -> None:
        self.existing_sessions = set(existing_sessions or set())
        self.current_commands = current_commands or {}
        self.pane_pids = pane_pids or {}
        self.calls: list[list[str]] = []

    @staticmethod
    def _value_for(mapping: dict[str, Any], key: str, default: Any) -> Any:
        value = mapping.get(key, default)
        if isinstance(value, list):
            if len(value) > 1:
                return value.pop(0)
            if len(value) == 1:
                return value[0]
            return default
        return value

    @staticmethod
    def _target_and_command_from_shell(shell_command: str) -> tuple[str, str]:
        try:
            tokens = shlex.split(shell_command)
        except ValueError:
            return "", ""
        target = ""
        command = ""
        for token in tokens:
            if token.startswith("TICKET_BOARD_PANE_TARGET="):
                target = token.split("=", 1)[1]
            elif token.startswith("PGU_PANE_TARGET=") and not target:
                target = token.split("=", 1)[1]
            elif Path(token).name in {"agy", "claude", "codex"}:
                command = Path(token).name
                break
        return target, command

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["tmux", "has-session"]:
            session = args[-1]
            return subprocess.CompletedProcess(args, 0 if session in self.existing_sessions else 1)
        if args[:3] == ["tmux", "display-message", "-p"]:
            target = args[args.index("-t") + 1]
            if args[-1] == "#{pane_pid}":
                pane_pid = int(self._value_for(self.pane_pids, target, 0) or 0)
                return subprocess.CompletedProcess(args, 0 if pane_pid else 1, stdout=f"{pane_pid}\n" if pane_pid else "")
            command = str(self._value_for(self.current_commands, target, "") or "")
            return subprocess.CompletedProcess(args, 0 if command else 1, stdout=command + "\n")
        if args[:2] == ["tmux", "new-session"]:
            session = args[args.index("-s") + 1]
            self.existing_sessions.add(session)
            target, command = self._target_and_command_from_shell(str(args[-1]))
            if target and command and target not in self.current_commands:
                self.current_commands[target] = command
        if args[:2] == ["tmux", "kill-session"]:
            session = args[-1]
            self.existing_sessions.discard(session)
        if len(args) >= 4 and args[:4] == ["git", "-C", args[2], "fetch"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "worktree", "add"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 6 and args[:6] == ["git", "-C", args[2], "checkout", "--detach", "--force"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "clean", "-fdx"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\n")
        if len(args) >= 6 and args[:6] == ["git", "-C", args[2], "ls-remote", "--exit-code"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
        if len(args) >= 5 and args[:5] == ["git", "-C", args[2], "cat-file", "-e"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 6 and args[:6] == ["git", "-C", args[2], "rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0 0\n")
        return subprocess.CompletedProcess(args, 0)


class SudoAwareFakeRunner(FakeRunner):
    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["sudo", "-u", "agent"]:
            self.calls.append(args)
            inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
            result = super().__call__(inner, **kwargs)
            if self.calls and self.calls[-1] == inner:
                self.calls.pop()
            return result
        return super().__call__(args, **kwargs)


class ResumeExitFakeRunner(FakeRunner):
    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["tmux", "new-session", "-d"] and " --resume " in f" {args[-1]} ":
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        return super().__call__(args, **kwargs)


def _leaf_commands(node: object) -> list[str]:
    commands: list[str] = []
    if isinstance(node, dict):
        widgets = node.get("Widgets")
        if isinstance(widgets, list):
            for child in widgets:
                commands.extend(_leaf_commands(child))
        elif "Command" in node:
            commands.append(str(node["Command"]))
    return commands


def _layout_shape(node: object) -> object:
    if isinstance(node, dict) and isinstance(node.get("Widgets"), list):
        return {
            "Orientation": node.get("Orientation"),
            "Widgets": [_layout_shape(child) for child in node["Widgets"]],
        }
    return "leaf"


def _layout_session_tree(node: object) -> object:
    if isinstance(node, dict) and isinstance(node.get("Widgets"), list):
        return {
            "Orientation": node.get("Orientation"),
            "Widgets": [_layout_session_tree(child) for child in node["Widgets"]],
        }
    if isinstance(node, dict):
        return node.get("SessionRestoreId")
    return None


def _layout_command_role_tree(node: object) -> object:
    if isinstance(node, dict) and isinstance(node.get("Widgets"), list):
        return {
            "Orientation": node.get("Orientation"),
            "Widgets": [_layout_command_role_tree(child) for child in node["Widgets"]],
        }
    if isinstance(node, dict):
        try:
            parts = shlex.split(str(node.get("Command") or ""))
        except ValueError:
            return ""
        for mode in ("start", "attach", "attach-or-start", "reload"):
            if mode in parts:
                index = parts.index(mode)
                if index + 1 < len(parts):
                    return parts[index + 1]
        return ""
    return None


def _literal_layout_from_session_tree(node: object) -> dict[str, object]:
    if isinstance(node, dict):
        return {
            "Orientation": node["Orientation"],
            "Widgets": [_literal_layout_from_session_tree(child) for child in node["Widgets"]],
        }
    return {
        "Command": "",
        "SessionRestoreId": int(node),
        "WorkingDirectory": "",
    }


FROZEN_STACKED_SIX_LAYOUT = _literal_layout_from_session_tree(
    {
        "Orientation": "Horizontal",
        "Widgets": [
            0,
            {"Orientation": "Vertical", "Widgets": [1, 2, 3, 4, 5]},
        ],
    }
)

FROZEN_COLUMN_MAJOR_SIX_LAYOUT = _literal_layout_from_session_tree(
    {
        "Orientation": "Horizontal",
        "Widgets": [
            {"Orientation": "Vertical", "Widgets": [0, 1]},
            {"Orientation": "Vertical", "Widgets": [2, 3]},
            {"Orientation": "Vertical", "Widgets": [4, 5]},
        ],
    }
)


def _write_launcher_config(
    directory: Path,
    *,
    project: str = "otto",
    layout: str | None = None,
    role_count: int = 6,
    roles: list[str] | None = None,
    pane_launcher: Path | None = None,
) -> Path:
    role_names = roles or [f"role{index}" for index in range(role_count)]
    layout_name = layout or f"{project}-konsole-layout.json"
    payload: dict[str, Any] = {
        "project": project,
        "layout": layout_name,
        "roles": [
            {
                "cli": ["codex"],
                "live_commands": ["codex"],
                "role": role,
                "slot": index,
                "target": f"{project}-{role}:0.0",
                "tmux_session": f"{project}-{role}",
                "workdir": str(directory / "worktrees" / role),
                "yolo": True,
            }
            for index, role in enumerate(role_names)
        ],
    }
    if pane_launcher is not None:
        payload["pane_launcher"] = str(pane_launcher)
    config_path = directory / f"{project}.json"
    config_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return config_path


PANEL_CLOCKWISE_SLOT_ORDER = (0, 2, 3, 5, 4, 1)


def _pgu_project_root() -> Path:
    return Path("/home/agent/Projects/pgu")


def test_pgu_layout_matches_reference_six_pane_geometry() -> None:
    layout = json.loads((ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json").read_text(encoding="utf-8"))

    assert _layout_shape(layout) == {
        "Orientation": "Horizontal",
        "Widgets": [
            {
                "Orientation": "Vertical",
                "Widgets": ["leaf", "leaf"],
            },
            {
                "Orientation": "Vertical",
                "Widgets": [
                    {
                        "Orientation": "Horizontal",
                        "Widgets": ["leaf", "leaf"],
                    },
                    {
                        "Orientation": "Horizontal",
                        "Widgets": ["leaf", "leaf"],
                    },
                ],
            },
        ],
    }
    assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {""}


def test_generated_new_project_layouts_are_balanced_row_major_grids() -> None:
    expected_by_count = {
        1: 0,
        2: {
            "Orientation": "Horizontal",
            "Widgets": [0, 1],
        },
        3: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1]},
                2,
            ],
        },
        4: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1]},
                {"Orientation": "Horizontal", "Widgets": [2, 3]},
            ],
        },
        5: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4]},
            ],
        },
        6: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
            ],
        },
        7: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
                6,
            ],
        },
        8: {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
                {"Orientation": "Horizontal", "Widgets": [6, 7]},
            ],
        },
    }

    for role_count, expected in expected_by_count.items():
        layout = team_launcher._new_project_layout_payload(role_count)
        assert _layout_session_tree(layout) == expected
        leaves = team_launcher._layout_leaves(layout)
        assert [leaf["SessionRestoreId"] for leaf in leaves] == list(range(role_count))
        assert {leaf.get("Command") for leaf in leaves} == {""}
        assert {leaf.get("WorkingDirectory") for leaf in leaves} == {""}


def test_upgrade_refreshes_legacy_generated_provision_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_STACKED_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)

        assert team_launcher._legacy_new_project_stacked_layout_payload(6) == FROZEN_STACKED_SIX_LAYOUT
        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)
        second_result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)
        assert not second_result.changed
        assert "already current" in second_result.message


def test_upgrade_refreshes_column_major_generated_provision_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)

        assert team_launcher._legacy_new_project_column_major_layout_payload(6) == FROZEN_COLUMN_MAJOR_SIX_LAYOUT
        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)


def test_upgrade_repairs_generated_runtime_session_dir_to_durable_owner_state() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_geteuid = team_launcher.os.geteuid
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        team_launcher.current_user_name = lambda: "root"
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade-session.") as tmp:
            provision_dir = Path(tmp) / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = "otto-agent"
            raw_config["session_dir"] = "/run/user/1005/otto-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)
            runner = FakeRunner()

            result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path, runner=runner)
            upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
        team_launcher.current_user_name = original_current_user_name

    assert result.changed
    assert "upgraded session dir" in result.message
    assert upgraded["session_dir"] == "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions"
    assert team_launcher.chown_owner_file_args(config, config_path) in runner.calls


def test_launch_auto_upgrades_generated_session_dir_before_starting_panes() -> None:
    current_user = team_launcher.current_user_name()
    original_uid_for_user = team_launcher.uid_for_user
    try:
        team_launcher.uid_for_user = lambda user_name: os.getuid() if user_name == current_user else original_uid_for_user(user_name)
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade-session.") as tmp:
            provision_dir = Path(tmp) / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = current_user
            raw_config["session_dir"] = f"/run/user/{os.getuid()}/otto-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)
            runner = FakeRunner()
            stdout = StringIO()

            with redirect_stdout(stdout):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=runner,
                        layout_output=Path(tmp) / "launch-layout.json",
                        layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                        no_launcher_self_deploy=True,
                        allow_stale_launcher=True,
                    )
                    == 0
                )
            upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    expected_session_dir = f"/home/{current_user}/.local/state/otto-ticket-board/pane-sessions"
    assert upgraded["session_dir"] == expected_session_dir
    assert "upgraded session dir" in stdout.getvalue()
    hook_install_calls = [
        call
        for call in runner.calls
        if "ticket-board-install-pane-hooks" in " ".join(str(part) for part in call)
    ]
    assert hook_install_calls
    assert hook_install_calls[0][:1] == ["env"]
    assert f"TICKET_BOARD_PROJECT=otto" in hook_install_calls[0]
    assert f"TICKET_BOARD_PANE_STATE_DIR=/run/user/{os.getuid()}/otto-ticket-board/pane-state" in hook_install_calls[0]
    assert f"TICKET_BOARD_PANE_SESSION_DIR={expected_session_dir}" in hook_install_calls[0]
    assert str(ROOT / "scripts" / "ticket-board-install-pane-hooks") in hook_install_calls[0]
    assert "install" in hook_install_calls[0]
    assert "--home" in hook_install_calls[0]
    assert f"/home/{current_user}" in hook_install_calls[0]
    tmux_new_calls = [call for call in runner.calls if call[:3] == ["tmux", "new-session", "-d"]]
    assert tmux_new_calls
    assert runner.calls.index(hook_install_calls[0]) < runner.calls.index(tmux_new_calls[0])
    assert f"TICKET_BOARD_PANE_SESSION_DIR={expected_session_dir}" in tmux_new_calls[0][-1]
    assert "TICKET_BOARD_PANE_SESSION_DIR=/run/" not in tmux_new_calls[0][-1]


def test_launch_repairs_generated_project_owner_hooks_before_starting_panes() -> None:
    class OwnerTmuxRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["sudo", "-u", "otto-agent"]:
                inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                if inner[:1] == ["tmux"]:
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return subprocess.CompletedProcess(
                        args,
                        result.returncode,
                        stdout=getattr(result, "stdout", None),
                        stderr=getattr(result, "stderr", None),
                    )
                return subprocess.CompletedProcess(args, 0)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            return super().__call__(args, **kwargs)

    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_seed_initial_pane_idle_state = team_launcher.seed_initial_pane_idle_state
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)
        team_launcher.current_user_name = lambda: "root"
        team_launcher.seed_initial_pane_idle_state = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-hooks-repair.") as tmp:
            tmp_path = Path(tmp)
            provision_dir = tmp_path / "home" / "otto-agent" / "Projects" / "otto" / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            pane_launcher = Path("/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher")
            config_path = _write_launcher_config(provision_dir, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = "otto-agent"
            raw_config["pane_launcher"] = str(pane_launcher)
            raw_config["session_dir"] = str(tmp_path / "durable-sessions")
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)
            runner = OwnerTmuxRunner()
            pane_state_dir = tmp_path / "pane-state"
            session_dir = raw_config["session_dir"]

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "launch-layout.json",
                    pane_state_dir=pane_state_dir,
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                    no_launcher_self_deploy=True,
                    allow_stale_launcher=True,
                )
                == 0
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        team_launcher.seed_initial_pane_idle_state = original_seed_initial_pane_idle_state

    hook_install_call = next(
        call for call in runner.calls if "ticket-board-install-pane-hooks" in " ".join(str(part) for part in call)
    )
    tmux_new_call = next(call for call in runner.calls if call[:3] == ["sudo", "-u", "otto-agent"] and call[4:7] == ["tmux", "new-session", "-d"])
    assert hook_install_call == [
        "sudo",
        "-u",
        "otto-agent",
        "-H",
        "env",
        "XDG_RUNTIME_DIR=/run/user/1005",
        "TICKET_BOARD_PROJECT=otto",
        f"TICKET_BOARD_PANE_STATE_DIR={pane_state_dir}",
        f"TICKET_BOARD_PANE_SESSION_DIR={session_dir}",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-install-pane-hooks",
        "install",
        "--home",
        "/home/otto-agent",
        "--hook-source",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-pane-idle-hook",
        "--bin-path",
        "/home/otto-agent/.local/bin/ticket-board-pane-idle-hook",
    ]
    assert runner.calls.index(hook_install_call) < runner.calls.index(tmux_new_call)


def test_generated_project_hook_install_args_match_otto_one_time_repair_command() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)
        team_launcher.current_user_name = lambda: "root"
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-hook-args.") as tmp:
            provision_dir = Path(tmp) / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = "otto-agent"
            raw_config["pane_launcher"] = "/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher"
            raw_config["session_dir"] = "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)

            args = team_launcher.install_generated_project_pane_hooks_args(
                config,
                script_path=ROOT / "scripts" / "team-launcher",
                pane_state_dir=Path("/run/user/1005/otto-ticket-board/pane-state"),
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name

    assert args == [
        "sudo",
        "-u",
        "otto-agent",
        "-H",
        "env",
        "XDG_RUNTIME_DIR=/run/user/1005",
        "TICKET_BOARD_PROJECT=otto",
        "TICKET_BOARD_PANE_STATE_DIR=/run/user/1005/otto-ticket-board/pane-state",
        "TICKET_BOARD_PANE_SESSION_DIR=/home/otto-agent/.local/state/otto-ticket-board/pane-sessions",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-install-pane-hooks",
        "install",
        "--home",
        "/home/otto-agent",
        "--hook-source",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-pane-idle-hook",
        "--bin-path",
        "/home/otto-agent/.local/bin/ticket-board-pane-idle-hook",
    ]


def test_upgrade_leaves_hand_maintained_pgu_layout_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        root = Path(tmp)
        config_dir = root / "config" / "team-launcher"
        config_dir.mkdir(parents=True)
        layout_path = config_dir / "pgu-konsole-layout.json"
        original_layout = json.loads((ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json").read_text(encoding="utf-8"))
        layout_path.write_text(json.dumps(original_layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = _write_launcher_config(config_dir, project="pgu", layout="pgu-konsole-layout.json")
        config = load_project_config("pgu", config_path)

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert not result.changed
        assert "hand-maintained" in result.message
        assert json.loads(layout_path.read_text(encoding="utf-8")) == original_layout


def test_upgrade_leaves_real_pgu_layout_hash_and_plan_unchanged() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    before_hash = hashlib.md5(config.layout.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        layout_output = Path(tmp) / "pgu-launch-layout.json"
        before_stdout = StringIO()
        with redirect_stdout(before_stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=FakeRunner(),
                    dry_run=True,
                    layout_output=layout_output,
                )
                == 0
            )

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        after_stdout = StringIO()
        with redirect_stdout(after_stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=FakeRunner(),
                    dry_run=True,
                    layout_output=layout_output,
                )
                == 0
            )

    assert not result.changed
    assert hashlib.md5(config.layout.read_bytes()).hexdigest() == before_hash
    assert after_stdout.getvalue() == before_stdout.getvalue()


def test_upgrade_leaves_customized_provision_layout_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        custom_layout = {
            "Orientation": "Vertical",
            "Widgets": [
                {"Command": "", "SessionRestoreId": index, "WorkingDirectory": ""}
                for index in range(6)
            ],
        }
        layout_path.write_text(json.dumps(custom_layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert not result.changed
        assert "differs from the known generated legacy shapes" in result.message
        assert json.loads(layout_path.read_text(encoding="utf-8")) == custom_layout


def test_launch_auto_upgrades_column_major_layout_before_materializing() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)
        launch_layout = Path(tmp) / "launch-layout.json"
        runner = FakeRunner()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=launch_layout,
                no_launcher_self_deploy=True,
                allow_stale_launcher=True,
            )
            == 0
        )

        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)
        assert _layout_session_tree(json.loads(launch_layout.read_text(encoding="utf-8"))) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
            ],
        }


def test_materialize_layout_places_six_roles_row_major() -> None:
    roles = ["designer", "director", "audit", "ops", "app", "main"]
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-row-major.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(len(roles)), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, roles=roles)
        config = load_project_config("otto", config_path)
        output_path = Path(tmp) / "materialized-layout.json"

        materialize_layout(
            config,
            config_path=config_path,
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            output_path=output_path,
        )

        assert _layout_command_role_tree(json.loads(output_path.read_text(encoding="utf-8"))) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": ["designer", "director", "audit"]},
                {"Orientation": "Horizontal", "Widgets": ["ops", "app", "main"]},
            ],
        }


def test_launch_dry_run_does_not_upgrade_legacy_generated_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        legacy_layout = FROZEN_STACKED_SIX_LAYOUT
        legacy_text = json.dumps(legacy_layout, indent=2, sort_keys=True) + "\n"
        layout_path.write_text(legacy_text, encoding="utf-8")
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)
        launch_layout = Path(tmp) / "launch-layout.json"
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=FakeRunner(),
                    dry_run=True,
                    layout_output=launch_layout,
                )
                == 0
            )

        assert layout_path.read_text(encoding="utf-8") == legacy_text
        assert _layout_session_tree(json.loads(launch_layout.read_text(encoding="utf-8"))) == {
            "Orientation": "Horizontal",
            "Widgets": [
                0,
                {"Orientation": "Vertical", "Widgets": [1, 2, 3, 4, 5]},
            ],
        }
        assert "upgraded generated layout template" not in stdout.getvalue()


def test_switchyard_upgrade_command_updates_registered_project_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade.") as tmp:
        root = Path(tmp)
        provision_dir = root / "otto" / ".switchyard" / "provision"
        registry_dir = root / "registry"
        provision_dir.mkdir(parents=True)
        registry_dir.mkdir()
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(config_path),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        previous_registry = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
        stdout = StringIO()
        try:
            with redirect_stdout(stdout):
                assert switchyard_main(["upgrade", "otto"]) == 0
        finally:
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = previous_registry

        assert "upgraded generated layout template" in stdout.getvalue()
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)


def test_switchyard_upgrade_reports_tenant_release_update_command() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-release.") as tmp:
        root = Path(tmp)
        _origin, source_repo = _make_origin_backed_repo(root)
        target_sha = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()
        old_sha = "1111111111111111111111111111111111111111"
        board_root = root / "otto-ticketboard-live"
        old_release = board_root / "releases" / old_sha
        old_release.mkdir(parents=True)
        (old_release / ".pgu-deploy-sha").write_text(old_sha + "\n", encoding="utf-8")
        (board_root / "current").symlink_to(old_release)
        provision_dir = root / "otto" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(
            provision_dir,
            pane_launcher=board_root / "current" / "scripts" / "team-launcher",
        )
        config = load_project_config("otto", config_path)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.upgrade_project_command(
                    config,
                    config_path=config_path,
                    source_repo=source_repo,
                    deploy_ref="origin/main",
                )
                == 0
            )

        rendered = stdout.getvalue()

    assert f"old: {old_sha} at {old_release}" in rendered
    assert f"new: {target_sha} from origin/main" in rendered
    assert "privileged release update command for Eric" in rendered
    assert "sudo env" in rendered
    assert "TICKET_BOARD_PROJECT=otto" in rendered
    assert f"SOURCE_REPO={source_repo}" in rendered
    assert f"BOARD_ROOT={board_root}" in rendered
    assert "DEPLOY_REF=origin/main" in rendered
    assert f"{source_repo}/scripts/ticket-board-service.sh deploy" in rendered
    assert "panes must be restarted after the release update" in rendered
    assert "deploy-restart" not in rendered


def test_switchyard_upgrade_reports_unchanged_tenant_release() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-release.") as tmp:
        root = Path(tmp)
        _origin, source_repo = _make_origin_backed_repo(root)
        target_sha = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()
        board_root = root / "otto-ticketboard-live"
        release = board_root / "releases" / target_sha
        release.mkdir(parents=True)
        (release / ".pgu-deploy-sha").write_text(target_sha + "\n", encoding="utf-8")
        (board_root / "current").symlink_to(release)
        provision_dir = root / "otto" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(
            provision_dir,
            pane_launcher=board_root / "current" / "scripts" / "team-launcher",
        )
        config = load_project_config("otto", config_path)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.upgrade_project_command(
                    config,
                    config_path=config_path,
                    source_repo=source_repo,
                    deploy_ref="origin/main",
                )
                == 0
            )

        rendered = stdout.getvalue()

    assert f"old: {target_sha} at {release}" in rendered
    assert f"new: {target_sha} from origin/main" in rendered
    assert "deployed board release unchanged; no release deploy needed" in rendered
    assert "sudo env" not in rendered
    assert "panes must be restarted after the release update" not in rendered


def test_switchyard_upgrade_pgu_without_tenant_release_emits_no_release_report() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-pgu.") as tmp:
        root = Path(tmp)
        _origin, source_repo = _make_origin_backed_repo(root)
        provision_dir = root / "pgu" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "pgu-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, project="pgu")
        config = load_project_config("pgu", config_path)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.upgrade_project_command(
                    config,
                    config_path=config_path,
                    source_repo=source_repo,
                    deploy_ref="origin/main",
                )
                == 0
            )

        rendered = stdout.getvalue()

    assert "deployed board release" not in rendered
    assert "privileged release update command" not in rendered
    assert "sudo env" not in rendered
    assert "panes must be restarted after the release update" not in rendered


def test_layout_detection_uses_invoking_user_and_falls_back_to_separate() -> None:
    class DesktopRunner:
        def __init__(self, *, desktop_output: str = "") -> None:
            self.desktop_output = desktop_output
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["loginctl", "show-user", "eric"]:
                return subprocess.CompletedProcess(args, 0, stdout="7\n")
            if args[:3] == ["loginctl", "show-session", "7"]:
                return subprocess.CompletedProcess(args, 0, stdout=self.desktop_output)
            return subprocess.CompletedProcess(args, 1, stdout="")

    kde_runner = DesktopRunner(desktop_output="Desktop=KDE\n")
    assert (
        team_launcher.detected_invoking_desktop(
            environ={"SUDO_USER": "eric"},
            runner=kde_runner,
        )
        == "KDE"
    )
    assert kde_runner.calls[0] == ["loginctl", "show-user", "eric", "-p", "Display", "--value"]
    assert kde_runner.calls[1] == ["loginctl", "show-session", "7", "-p", "Desktop"]
    assert team_launcher.resolve_layout_mode(
        "auto",
        environ={"SUDO_USER": "eric"},
        runner=kde_runner,
    ) == "separate"

    unknown_runner = DesktopRunner(desktop_output="")
    assert team_launcher.resolve_layout_mode("auto", environ={"SUDO_USER": "eric"}, runner=unknown_runner) == "separate"
    type_only_runner = DesktopRunner(desktop_output="Type=wayland\n")
    assert team_launcher.resolve_layout_mode("auto", environ={"SUDO_USER": "eric"}, runner=type_only_runner) == "separate"
    assert team_launcher.resolve_layout_mode("auto", environ={"XDG_CURRENT_DESKTOP": "GNOME"}, runner=FakeRunner()) == "viewer"
    assert team_launcher.resolve_layout_mode("viewer", environ={"XDG_CURRENT_DESKTOP": "KDE"}, runner=FakeRunner()) == "viewer"
    assert team_launcher.resolve_layout_mode("separate", environ={"XDG_CURRENT_DESKTOP": "GNOME"}, runner=FakeRunner()) == "separate"


def _write_pgu_config_with_shared_checkout(tmp: Path) -> Path:
    source = json.loads((ROOT / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    source["layout"] = str(ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json")
    source["repository"] = str(tmp / "repo")
    source["worktree_base"] = str(tmp / "worktrees")
    source["session_dir"] = str(tmp / "sessions")
    config_path = tmp / "pgu.json"
    config_path.write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def _write_six_visible_role_config(tmp: Path, project: str = "porter") -> Path:
    layout = {
        "Orientation": "Horizontal",
        "Widgets": [
            {"Command": "", "SessionRestoreId": index, "WorkingDirectory": ""}
            for index in range(6)
        ],
    }
    layout_path = tmp / f"{project}-layout.json"
    layout_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    roles = ["designer", "director", "audit", "ops", "app", "main"]
    repo = tmp / "repo"
    repo.mkdir(exist_ok=True)
    config_path = tmp / f"{project}.json"
    config_path.write_text(
        json.dumps(
            {
                "project": project,
                "layout": str(layout_path),
                "repository": str(repo),
                "session_dir": str(tmp / "sessions"),
                "roles": [
                    {
                        "role": role,
                        "slot": index,
                        "cli": ["claude" if role in {"designer", "director", "audit"} else "codex"],
                        "live_commands": ["claude" if role in {"designer", "director", "audit"} else "codex"],
                        "target": f"{project}-{role}:0.0",
                        "tmux_session": f"{project}-{role}",
                    }
                    for index, role in enumerate(roles)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


def _tmux_args(server: str, args: list[str]) -> list[str]:
    return ["tmux", "-L", server, *args]


def _run_isolated_tmux(server: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(_tmux_args(server, args), **kwargs)


def _cleanup_isolated_tmux_sessions(server: str, sessions: list[str]) -> None:
    for session in sessions:
        _run_isolated_tmux(server, ["kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _isolated_tmux_runner(server: str) -> Any:
    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args and args[0] == "tmux":
            isolated_args = ["tmux", "-L", server, *args[1:]]
            isolated_args = [
                token.replace("env TMUX= tmux attach -t", f"env TMUX= tmux -L {server} attach -t")
                if isinstance(token, str)
                else token
                for token in isolated_args
            ]
            return subprocess.run(isolated_args, **kwargs)
        return subprocess.run(args, **kwargs)

    return runner


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["git", "add", "."], cwd=repo)
    _run_git(["git", "commit", "-m", message], cwd=repo)


def _make_origin_backed_repo(tmp: Path) -> tuple[Path, Path]:
    seed = tmp / "seed"
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    seed.mkdir()
    _run_git(["git", "init", "-b", "main"], cwd=seed)
    _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=seed)
    _run_git(["git", "config", "user.name", "PGU Agent"], cwd=seed)
    (seed / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _commit_all(seed, "initial")
    _run_git(["git", "clone", "--bare", str(seed), str(origin)])
    _run_git(["git", "clone", str(origin), str(repo)])
    _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=repo)
    _run_git(["git", "config", "user.name", "PGU Agent"], cwd=repo)
    return origin, repo


def _write_minimal_shared_checkout_config(
    tmp: Path,
    repo: Path,
    roles: list[str],
    *,
    run_as_user: str = "",
) -> Path:
    layout = {
        "KonsoleTabs": [
            {
                "Widgets": [
                    {"SessionRestoreId": index, "Command": "", "WorkingDirectory": ""}
                    for index, _role in enumerate(roles)
                ]
            }
        ]
    }
    layout_path = tmp / "layout.json"
    layout_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config = {
        "project": "pgu",
        "layout": str(layout_path),
        "repository": str(repo),
        "run_as_user": run_as_user,
        "worktree_base": str(tmp / "worktrees"),
        "roles": [
            {
                "role": role,
                "slot": index,
                "target": f"pgu-{role}:0.0",
                "cli": ["codex"],
            }
            for index, role in enumerate(roles)
        ],
    }
    config_path = tmp / "pgu.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def _session_payload(target: str, session_id: str, payload: dict[str, object]) -> str:
    return json.dumps(
        {
            "target": target,
            "session_id": session_id,
            "source": "codex.SessionStart",
            "payload": payload,
        }
    ) + "\n"


def _read_pane_state(state_dir: Path, target: str) -> dict[str, object]:
    return json.loads((state_dir / pane_state_file_name(target)).read_text(encoding="utf-8"))


def test_launch_session_record_report_waits_and_warns_for_missing_records() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-records.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "designer", "slot": 0, "cli": ["claude"], "target": "porter-designer:0.0"},
                        {"role": "main", "slot": 1, "cli": ["codex"], "target": "porter-main:0.0"},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        designer = config.roles[0]
        start = time.monotonic()
        stamps: list[tuple[float, str]] = []

        def write_designer_record() -> None:
            time.sleep(0.05)
            (session_dir / session_file_name(designer.target)).write_text(
                json.dumps({"target": designer.target, "session_id": "designer-session"}) + "\n",
                encoding="utf-8",
            )

        writer = threading.Thread(target=write_designer_record)
        writer.start()
        try:
            statuses = team_launcher.report_launch_session_records(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                print_func=lambda message: stamps.append((time.monotonic() - start, message)),
            )
        finally:
            writer.join(timeout=1.0)

    by_role = {status.role: status for status in statuses}
    assert by_role["designer"].session_id == "designer-session"
    assert by_role["main"].session_id == ""
    messages = [message for _elapsed, message in stamps]
    assert stamps[0][0] < 0.05, stamps
    assert messages[0] == "switchyard: checking session records for 2 pane(s) (waiting up to 1s)"
    assert any("session record found for designer (porter-designer:0.0): designer-session" in message for message in messages)
    assert any("warning: switchyard: session record missing for main (porter-main:0.0)" in message for message in messages)


def test_launch_session_record_report_names_recent_resume_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-fallback.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        fallback_since_ns = time.time_ns()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
            encoding="utf-8",
        )
        (session_dir / f"{session_file_name(role.target)}.superseded").write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            fallback_changed_since_ns=fallback_since_ns,
            print_func=messages.append,
        )

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert any(
        "warning: switchyard: app (porter-app:0.0) fell back to a fresh session; superseded session old-session"
        in message
        for message in messages
    )


def test_launch_session_record_report_names_recent_unverified_resume_timeout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-timeout.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        fallback_since_ns = time.time_ns()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "kept-session"}) + "\n",
            encoding="utf-8",
        )
        team_launcher.record_unverified_resume_for_role(role, session_dir, "kept-session")
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            fallback_changed_since_ns=fallback_since_ns,
            print_func=messages.append,
        )

    assert statuses[0].session_id == "kept-session"
    assert statuses[0].unverified_resume_session_id == "kept-session"
    assert any(
        "warning: switchyard: app (porter-app:0.0) resume for session kept-session was not verified"
        in message
        for message in messages
    )
    assert any("did not mark pane idle" in message for message in messages)


def test_launch_session_record_report_ignores_stale_unverified_resume_timeout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-timeout.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "healthy-session"}) + "\n",
            encoding="utf-8",
        )
        team_launcher.record_unverified_resume_for_role(role, session_dir, "old-timeout-session")
        marker = session_dir / f"{session_file_name(role.target)}.resume_timeout"
        time.sleep(0.12)
        fallback_since_ns = time.time_ns()
        while fallback_since_ns <= marker.stat().st_ctime_ns:
            fallback_since_ns = time.time_ns()
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            fallback_changed_since_ns=fallback_since_ns,
            print_func=messages.append,
        )

    assert statuses[0].session_id == "healthy-session"
    assert statuses[0].unverified_resume_session_id == ""
    assert not any("was not verified before startup timeout" in message for message in messages)


def test_launch_session_record_statuses_treat_unverified_resume_as_reported_outcome() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-timeout-reported.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        pane_state_dir = tmp_path / "pane-state"
        pane_state_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "kept-session"}) + "\n",
            encoding="utf-8",
        )
        team_launcher.record_unverified_resume_for_role(role, session_dir, "kept-session")
        started = time.monotonic()

        statuses = team_launcher.launch_session_record_statuses(
            config,
            timeout_seconds=0.5,
            poll_seconds=0.01,
            fallback_changed_since_ns=fallback_since_ns,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=pane_state_since,
        )
        elapsed = time.monotonic() - started

    assert statuses[0].pane_launch_reported
    assert statuses[0].pane_state_source == ""
    assert statuses[0].unverified_resume_session_id == "kept-session"
    assert elapsed < 0.1


def test_launch_session_record_report_ignores_stale_resume_fallback_sidecars() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-fallback.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        sidecar.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        time.sleep(0.12)
        fallback_since_ns = time.time_ns()
        while fallback_since_ns <= sidecar.stat().st_ctime_ns:
            fallback_since_ns = time.time_ns()
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            fallback_changed_since_ns=fallback_since_ns,
            print_func=messages.append,
        )

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == ""
    assert not any("fell back to a fresh session" in message for message in messages)


def test_launch_session_record_statuses_wait_for_late_visible_fallback_outcome() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-late-fallback.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        pane_state_dir = tmp_path / "pane-state"
        pane_state_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()

        def write_late_fallback() -> None:
            time.sleep(0.05)
            active_record.replace(sidecar)
            active_record.write_text(
                json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(
                role,
                pane_state_dir=pane_state_dir,
                source="team_launcher.start.fallback",
            )

        writer = threading.Thread(target=write_late_fallback)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                fallback_changed_since_ns=fallback_since_ns,
                fallback_grace_seconds=0.0,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert statuses[0].pane_state_source == "team_launcher.start.fallback"


def test_launch_session_record_statuses_ignore_stale_pane_state_before_late_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-pane-state.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        pane_state_dir = tmp_path / "pane-state"
        pane_state_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        team_launcher.seed_initial_pane_idle_state(
            role,
            pane_state_dir=pane_state_dir,
            source="team_launcher.start",
            now=time.time() - 1.0,
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()

        def write_late_fallback() -> None:
            time.sleep(0.05)
            active_record.replace(sidecar)
            active_record.write_text(
                json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(
                role,
                pane_state_dir=pane_state_dir,
                source="team_launcher.start.fallback",
            )

        writer = threading.Thread(target=write_late_fallback)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                fallback_changed_since_ns=fallback_since_ns,
                fallback_grace_seconds=0.0,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert statuses[0].pane_state_source == "team_launcher.start.fallback"


def test_launch_session_record_statuses_ignore_non_launcher_pane_state_before_late_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-hook-pane-state.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        pane_state_dir = tmp_path / "pane-state"
        pane_state_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        (pane_state_dir / pane_state_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "state": "idle",
                    "updated_at": time.time(),
                    "source": "claude.UserPromptSubmit",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()

        def write_late_fallback() -> None:
            time.sleep(0.05)
            active_record.replace(sidecar)
            active_record.write_text(
                json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(
                role,
                pane_state_dir=pane_state_dir,
                source="team_launcher.start.fallback",
            )

        writer = threading.Thread(target=write_late_fallback)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                fallback_changed_since_ns=fallback_since_ns,
                fallback_grace_seconds=0.0,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert statuses[0].pane_state_source == "team_launcher.start.fallback"


def test_superseded_session_freshness_uses_rename_ctime_not_record_mtime() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-ctime.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        time.sleep(0.12)
        fallback_since_ns = time.time_ns()

        assert clear_session_record_for_role(role, session_dir) is True

        assert (
            team_launcher.superseded_session_id_for_role(
                role,
                session_dir,
                changed_since_ns=fallback_since_ns,
            )
            == "old-session"
        )


def test_launch_session_record_timeout_default_is_ten_seconds() -> None:
    assert team_launcher.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS == 10.0
    for fn in (team_launcher.launch_project, team_launcher.switchyard_new_command):
        assert (
            inspect.signature(fn).parameters["session_record_timeout"].default
            == team_launcher.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS
        )


def test_launch_project_reports_missing_session_record_after_reboot_resume() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-resume-session-records.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "designer", "slot": 0, "cli": ["claude"], "target": "porter-designer:0.0"},
                        {"role": "main", "slot": 1, "cli": ["codex"], "target": "porter-main:0.0"},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        designer = next(role for role in config.roles if role.role == "designer")
        (session_dir / session_file_name(designer.target)).write_text(
            json.dumps({"target": designer.target, "session_id": "designer-resume-session"}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(
            current_commands={
                "porter-designer:0.0": "claude",
                "porter-main:0.0": "codex",
            }
        )
        messages: list[str] = []

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "launch-layout.json",
                pane_state_dir=tmp_path / "pane-state",
                report_session_records=True,
                session_record_timeout=0,
                print_func=messages.append,
            )
            == 0
        )

    assert messages[0] == "switchyard: checking session records for 2 pane(s) (waiting up to 0s)"
    assert any("session record found for designer (porter-designer:0.0): designer-resume-session" in message for message in messages)
    assert any("warning: switchyard: session record missing for main (porter-main:0.0)" in message for message in messages)


def test_launch_project_reports_visible_role_resume_fallback_to_operator() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-visible-fallback.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"

        class VisibleFallbackRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if "konsole" in args and active_record.exists() and not sidecar.exists():
                    active_record.replace(sidecar)
                    active_record.write_text(
                        json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                        encoding="utf-8",
                    )
                return super().__call__(args, **kwargs)

        messages: list[str] = []

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=VisibleFallbackRunner(),
                layout_output=tmp_path / "launch-layout.json",
                pane_state_dir=tmp_path / "pane-state",
                report_session_records=True,
                session_record_timeout=0,
                print_func=messages.append,
            )
            == 0
        )

    assert any("session record found for app (porter-app:0.0): fresh-session" in message for message in messages)
    assert any(
        "warning: switchyard: app (porter-app:0.0) fell back to a fresh session; superseded session old-session"
        in message
        for message in messages
    )


def _live_session_record_fingerprint() -> dict[str, str]:
    files = sorted(path for path in LIVE_SESSION_DIR.glob("*.json") if path.is_file())
    return {path.name: hashlib.md5(path.read_bytes()).hexdigest() for path in files}


def _assert_live_session_records_unchanged(before: dict[str, str], after: dict[str, str]) -> None:
    assert len(after) == len(before), {"before": before, "after": after}
    assert after == before


def test_live_session_record_guard_detects_modified_and_deleted_records() -> None:
    before = {
        "pgu-ops_0.0.json": "ops-md5",
        "pgu-research_0.0.json": "research-md5",
    }

    _assert_live_session_records_unchanged(before, dict(before))
    try:
        _assert_live_session_records_unchanged(
            before,
            {"pgu-ops_0.0.json": "changed-md5", "pgu-research_0.0.json": "research-md5"},
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("live session guard did not detect a modified record")
    try:
        _assert_live_session_records_unchanged(before, {"pgu-ops_0.0.json": "ops-md5"})
    except AssertionError:
        pass
    else:
        raise AssertionError("live session guard did not detect a deleted record")


def test_ticket_board_pane_env_is_stripped_before_launcher_import() -> None:
    for key in TICKET_BOARD_PANE_ENV_KEYS:
        assert key not in os.environ
    assert str(team_launcher.DEFAULT_SESSION_DIR) == str(
        Path.home() / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
    )
    assert str(team_launcher.DEFAULT_PANE_STATE_DIR) == f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state"


def _env_entries(command: list[str]) -> list[str]:
    assert command[0] == "env"
    entries: list[str] = []
    for token in command[1:]:
        if "=" not in token:
            break
        entries.append(token)
    return entries


def _command_tail(command: list[str]) -> list[str]:
    assert command[0] == "env"
    for index, token in enumerate(command[1:], start=1):
        if "=" not in token:
            return command[index:]
    return []


def _no_input(prompt: str) -> str:
    raise AssertionError(f"unexpected prompt: {prompt}")


def _patch_process_snapshot(
    parents_by_pid: dict[int, int],
    children_by_parent: dict[int, list[int]],
    process_names: dict[int, set[str]],
    argv_by_pid: dict[int, list[str]],
) -> Any:
    original = team_launcher._process_snapshot
    team_launcher._process_snapshot = lambda: (parents_by_pid, children_by_parent, process_names, argv_by_pid)
    return original


def test_dry_run_materializes_pgu_layout_with_six_visible_role_commands() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        layout_output = Path(tmp) / "layout.json"
        result = launch_project(
            config,
            config_path=config_path,
            mode="start",
            script_path=ROOT / "scripts" / "team-launcher",
            dry_run=True,
            layout_output=layout_output,
        )

        assert result == 0
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert len(commands) == 6
        active_commands = [command for command in commands if command]
        assert len(active_commands) == 6
        clockwise_commands = [commands[index] for index in PANEL_CLOCKWISE_SLOT_ORDER]
        expected_clockwise_roles = ["inspector", "director", "audit", "ops", "app", "main"]
        assert config.pane_launcher is None
        for role in ("director", "main", "app", "ops", "audit", "inspector"):
            matching = [command for command in commands if f" {role} " in f" {command} " or command.endswith(f" {role}")]
            assert matching, (role, commands)
            assert " pane attach-or-start " in matching[0]
            assert "team-launcher" in matching[0]
        for role, command in zip(expected_clockwise_roles, clockwise_commands):
            assert f" {role} " in f" {command} ", (role, clockwise_commands)
        assert not any(" research " in f" {command} " for command in commands)
        assert not any(" perf " in f" {command} " for command in commands)


def test_pane_command_sudos_to_configured_owner_when_launcher_user_differs() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "root"
        command = team_launcher.pane_command(
            config.project,
            role,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            skip_launcher_check=True,
            run_as_user="agent",
        )
    finally:
        team_launcher.current_user_name = original_current_user_name

    tokens = shlex.split(command)
    assert tokens[:4] == ["sudo", "-u", "agent", "-H"]
    assert tokens[4:] == [
        str(ROOT / "scripts" / "team-launcher"),
        "pgu",
        "pane",
        "attach-or-start",
        "ops",
        "--config",
        str(ROOT / "config" / "team-launcher" / "pgu.json"),
        "--skip-launcher-check",
    ]


def test_pane_command_does_not_sudo_when_already_owner() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "agent"
        command = team_launcher.pane_command(
            config.project,
            role,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            skip_launcher_check=True,
            run_as_user="agent",
        )
    finally:
        team_launcher.current_user_name = original_current_user_name

    tokens = shlex.split(command)
    assert tokens[:4] != ["sudo", "-u", "agent", "-H"]
    assert tokens[0] == str(ROOT / "scripts" / "team-launcher")


def test_launch_project_default_layout_uses_project_scoped_switchyard_not_tmp() -> None:
    project = f"layoutsafe{os.getpid()}"
    stale_tmp_layout = Path(tempfile.gettempdir()) / f"{project}-team-layout.json"
    stale_tmp_layout.write_text("stale shared tmp layout\n", encoding="utf-8")
    stale_tmp_layout.chmod(0o444)
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-safe.") as tmp:
            tmp_path = Path(tmp)
            config_path = _write_six_visible_role_config(tmp_path, project=project)
            config = load_project_config(project, config_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=FakeRunner(),
                        dry_run=True,
                    )
                    == 0
                )

            expected_layout = config_path.parent / ".switchyard" / project / f"{project}-team-layout.json"
            plan = json.loads(stdout.getvalue())
            assert plan["layout"] == str(expected_layout)
            assert expected_layout.is_file()
            assert stale_tmp_layout.read_text(encoding="utf-8") == "stale shared tmp layout\n"
    finally:
        stale_tmp_layout.chmod(0o644)
        stale_tmp_layout.unlink(missing_ok=True)


def test_default_layout_paths_are_project_scoped() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-scope.") as tmp:
        tmp_path = Path(tmp)
        porter_root = tmp_path / "porter-user"
        zeta_root = tmp_path / "zeta-user"
        porter_root.mkdir()
        zeta_root.mkdir()
        porter_config_path = _write_six_visible_role_config(porter_root, project="porter")
        zeta_config_path = _write_six_visible_role_config(zeta_root, project="zeta")
        porter = load_project_config("porter", porter_config_path)
        zeta = load_project_config("zeta", zeta_config_path)

        porter_layout = team_launcher.default_layout_output_path(porter, config_path=porter_config_path)
        zeta_layout = team_launcher.default_layout_output_path(zeta, config_path=zeta_config_path)

    assert porter_layout == porter_config_path.parent / ".switchyard" / "porter" / "porter-team-layout.json"
    assert zeta_layout == zeta_config_path.parent / ".switchyard" / "zeta" / "zeta-team-layout.json"
    assert porter_layout != zeta_layout


def test_default_layout_path_for_run_as_user_lives_outside_shared_checkout() -> None:
    original_getpwnam = team_launcher.pwd.getpwnam
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-owner.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        config_path = _write_six_visible_role_config(tmp_path, project="porter")
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["run_as_user"] = "porter-agent"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        class FakeUserInfo:
            pw_dir = str(owner_home)
            pw_uid = os.getuid()

        try:
            def fake_getpwnam(user_name: str) -> object:
                return FakeUserInfo() if user_name == "porter-agent" else original_getpwnam(user_name)

            team_launcher.pwd.getpwnam = fake_getpwnam
            config = load_project_config("porter", config_path)
            layout = team_launcher.default_layout_output_path(config, config_path=config_path)
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam

    assert layout == owner_home / ".local" / "state" / "switchyard" / "projects" / "porter" / "porter-team-layout.json"
    assert config.repository is not None
    assert not layout.is_relative_to(config.repository)


def test_launch_project_uses_configured_owner_readable_pane_launcher() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        control_repo = tmp_path / "owner-home" / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        owner_launcher.parent.mkdir(parents=True)
        owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        owner_launcher.chmod(0o755)
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "repository": str(tmp_path / "project"),
                    "control_repository": str(control_repo),
                    "run_as_user": "porter-agent",
                    "worktree_base": str(worktree_base),
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path / "owner-home"
            config = load_project_config("porter", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        layout_output = tmp_path / "materialized.json"
        runner = FakeRunner()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in runner.calls
        assert ["test", "-x", str(owner_launcher)] not in runner.calls
        commands = _leaf_commands(json.loads(layout_output.read_text(encoding="utf-8")))
        assert len(commands) == 1
        assert str(owner_launcher) in commands[0]
        assert str(ROOT / "scripts" / "team-launcher") not in commands[0]
        assert "--skip-launcher-check" in commands[0]


def test_launch_project_probes_pane_launcher_as_owner_without_control_repository() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher-no-control.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        owner_launcher.parent.mkdir(parents=True)
        owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        owner_launcher.chmod(0o755)
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "repository": str(tmp_path / "project"),
                    "run_as_user": "porter-agent",
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        layout_output = tmp_path / "materialized.json"
        runner = FakeRunner()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert config.control_repository is None
        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in runner.calls
        assert ["test", "-x", str(owner_launcher)] not in runner.calls


def test_launch_project_probes_pane_launcher_as_owner_without_repository() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher-no-repo.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        owner_launcher.parent.mkdir(parents=True)
        owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        owner_launcher.chmod(0o755)
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "run_as_user": "porter-agent",
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        layout_output = tmp_path / "materialized.json"
        runner = FakeRunner()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert config.repository is None
        assert config.control_repository is None
        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in runner.calls
        assert ["test", "-x", str(owner_launcher)] not in runner.calls


def test_launch_project_refuses_missing_configured_pane_launcher() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-launcher-missing.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        owner_launcher = tmp_path / "owner-home" / "porter-ticketboard-live" / "current" / "scripts" / "team-launcher"
        control_repo = tmp_path / "owner-home" / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path = tmp_path / "porter.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "pane_launcher": str(owner_launcher),
                    "repository": str(tmp_path / "project"),
                    "control_repository": str(control_repo),
                    "run_as_user": "porter-agent",
                    "worktree_base": str(worktree_base),
                    "roles": [{"role": "director", "slot": 0, "target": "porter-director:0.0", "cli": ["claude"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path / "owner-home"
            config = load_project_config("porter", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        layout_output = tmp_path / "materialized.json"
        calls: list[list[str]] = []

        def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)]:
                return subprocess.CompletedProcess(args, 1)
            return subprocess.CompletedProcess(args, 0)

        try:
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
            )
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            message = str(exc)

        assert str(owner_launcher) in message
        assert "configured pane_launcher" in message
        assert "not readable/executable by porter-agent" in message
        assert ["sudo", "-u", "porter-agent", "test", "-x", str(owner_launcher)] in calls
        assert ["test", "-x", str(owner_launcher)] not in calls
        assert not layout_output.exists()


def test_kde_auto_layout_preserves_separate_konsole_plan_shape() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    runner = FakeRunner()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-kde-layout.") as tmp:
        tmp_path = Path(tmp)
        layout_output = tmp_path / "layout.json"
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    dry_run=True,
                    layout_output=layout_output,
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                )
                == 0
            )

        plan = json.loads(stdout.getvalue())
        layout = json.loads(layout_output.read_text(encoding="utf-8"))

    assert runner.calls == []
    assert "layout_mode" not in plan
    assert "viewer_session" not in plan
    assert [role["target"] for role in plan["roles"]] == [
        "pgu-director:0.0",
        "pgu-main:0.0",
        "pgu-app:0.0",
        "pgu-ops:0.0",
        "pgu-audit:0.0",
        "pgu-inspector:0.0",
    ]
    assert len(_leaf_commands(layout)) == 6


def test_pgu_config_matches_director_supplied_live_role_assignments() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_repository = Path("/home/agent/Projects/pgu")

    assert set(roles) == {"director", "main", "app", "research", "ops", "audit", "inspector"}
    assert all(role.yolo for role in roles.values())
    assert config.repository == expected_repository
    assert config.board_url == "http://127.0.0.1:8770"
    assert config.board_socket == "/run/pgu-ticket-board/ticket-board.sock"
    assert config.run_as_user == "agent"
    assert config.control_repository is None
    assert config.worktree_base is None
    assert worktree_ref(config) == "origin/main"
    assert roles["research"].detached
    assert roles["research"].slot is None
    assert {role.role: role.slot for role in roles.values() if not role.detached} == {
        "director": 2,
        "main": 1,
        "app": 4,
        "ops": 5,
        "audit": 3,
        "inspector": 0,
    }
    for role_name, role in roles.items():
        assert role.workdir == str(expected_repository), role_name
    assert (roles["director"].cli, roles["director"].model, roles["director"].effort) == (
        ["claude"],
        "claude-opus-5",
        "high",
    )
    assert (roles["director"].resume_mode, roles["director"].resume_flag) == ("flag", "--resume")
    assert (roles["ops"].cli, roles["ops"].model, roles["ops"].effort, roles["ops"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["ops"].resume_mode, roles["ops"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["main"].cli, roles["main"].model, roles["main"].effort, roles["main"].extra_args) == (
        ["codex"],
        "gpt-5.6-sol",
        "high",
        [],
    )
    assert (roles["main"].resume_mode, roles["main"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["app"].cli, roles["app"].model, roles["app"].effort, roles["app"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["app"].resume_mode, roles["app"].resume_subcommand) == ("subcommand", "resume")
    assert (roles["research"].cli, roles["research"].model, roles["research"].effort) == (
        ["claude"],
        "claude-opus-5",
        "high",
    )
    assert (roles["research"].resume_mode, roles["research"].resume_flag) == ("flag", "--resume")
    assert (roles["audit"].cli, roles["audit"].model, roles["audit"].effort) == (
        ["claude"],
        "claude-opus-5",
        "high",
    )
    assert (roles["audit"].resume_mode, roles["audit"].resume_flag) == ("flag", "--resume")
    assert (roles["inspector"].cli, roles["inspector"].model, roles["inspector"].effort) == (
        ["agy"],
        "gemini-3.7-flash-high",
        "",
    )
    assert (roles["inspector"].resume_mode, roles["inspector"].resume_flag) == ("flag", "--conversation")


def test_pgu_config_keeps_live_agent_repository_with_foreign_home() -> None:
    original_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = "/home/user"
        config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert config.run_as_user == "agent"
    assert config.repository == Path("/home/agent/Projects/pgu")
    assert {role.workdir for role in config.roles} == {"/home/agent/Projects/pgu"}


def test_default_session_dir_uses_run_as_user_home_when_launcher_default_is_foreign() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-session.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_getpwnam = team_launcher.pwd.getpwnam

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.pwd.getpwnam = fake_getpwnam
            config = load_project_config("porter", config_path)
        finally:
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.pwd.getpwnam = original_getpwnam

    expected_session_dir = owner_home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
    role = config.roles[0]
    entries = _env_entries(cli_command_for_role(role, session_dir=config.session_dir))
    assert config.session_dir == expected_session_dir
    assert f"TICKET_BOARD_PANE_SESSION_DIR={expected_session_dir}" in entries
    assert not any("/root/.local/state/pgu-ticket-board/pane-sessions" in entry for entry in entries)


def test_explicit_session_dir_env_beats_run_as_user_default() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-env-session.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        env_session_dir = tmp_path / "env" / "sessions"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        env_keys = ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
        original_env = {key: os.environ.get(key) for key in env_keys}
        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_getpwnam = team_launcher.pwd.getpwnam

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = str(env_session_dir)
            os.environ.pop("PGU_TICKET_BOARD_PANE_SESSION_DIR", None)
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.pwd.getpwnam = fake_getpwnam
            config = load_project_config("porter", config_path)
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.pwd.getpwnam = original_getpwnam

    role = config.roles[0]
    entries = _env_entries(cli_command_for_role(role, session_dir=config.session_dir))
    assert config.session_dir == env_session_dir
    assert f"TICKET_BOARD_PANE_SESSION_DIR={env_session_dir}" in entries
    assert not any(str(owner_home) in entry for entry in entries)
    assert not any("/root/.local/state/pgu-ticket-board/pane-sessions" in entry for entry in entries)


def test_configured_session_dir_beats_inherited_session_dir_env() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-config-session.") as tmp:
        tmp_path = Path(tmp)
        configured_session_dir = tmp_path / "config" / "sessions"
        inherited_session_dir = tmp_path / "inherited" / "sessions"
        legacy_inherited_session_dir = tmp_path / "legacy-inherited" / "sessions"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(configured_session_dir),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        env_keys = ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
        original_env = {key: os.environ.get(key) for key in env_keys}
        try:
            os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = str(inherited_session_dir)
            os.environ["PGU_TICKET_BOARD_PANE_SESSION_DIR"] = str(legacy_inherited_session_dir)
            config = load_project_config("porter", config_path)
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    role = config.roles[0]
    entries = _env_entries(cli_command_for_role(role, session_dir=config.session_dir))
    assert config.session_dir == configured_session_dir
    assert f"TICKET_BOARD_PANE_SESSION_DIR={configured_session_dir}" in entries
    assert not any(str(inherited_session_dir) in entry for entry in entries)
    assert not any(str(legacy_inherited_session_dir) in entry for entry in entries)


def test_launch_project_default_pane_state_dir_uses_run_as_user_runtime_when_launcher_default_is_foreign() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-pane-state.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        root_pane_state = tmp_path / "run" / "user" / "0" / "pgu-ticket-board" / "pane-state"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_default_pane_state_dir = team_launcher.DEFAULT_PANE_STATE_DIR
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid
        original_current_user_name = team_launcher.current_user_name

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        class OwnerAwareRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if args[:3] == ["sudo", "-u", "porter-agent"]:
                    self.calls.append(args)
                    inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return result
                return super().__call__(args, **kwargs)

        try:
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.DEFAULT_PANE_STATE_DIR = root_pane_state
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            config = load_project_config("porter", config_path)
            runner = OwnerAwareRunner()

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-output.json",
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                )
                == 0
            )
        finally:
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = original_default_pane_state_dir
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        expected_state = owner_runtime / "porter-ticket-board" / "pane-state" / pane_state_file_name("porter-ops:0.0")
        assert expected_state.exists()
        assert not (root_pane_state / pane_state_file_name("porter-ops:0.0")).exists()


def test_root_created_owner_state_dirs_are_owned_by_run_as_user() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-state-chown.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        root_session_dir = tmp_path / "root" / "sessions"
        root_pane_state = tmp_path / "run" / "user" / "0" / "pgu-ticket-board" / "pane-state"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_default_pane_state_dir = team_launcher.DEFAULT_PANE_STATE_DIR
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid
        original_current_user_name = team_launcher.current_user_name

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242
            pw_gid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        class OwnershipRunner(FakeRunner):
            def __init__(self) -> None:
                super().__init__()
                self.installed_dirs: dict[Path, tuple[str, str, int]] = {}

            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["install", "-d"]:
                    self.calls.append(args)
                    mode = int(args[args.index("-m") + 1], 8)
                    owner = args[args.index("-o") + 1]
                    group = args[args.index("-g") + 1]
                    target = Path(args[-1])
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(mode)
                    self.installed_dirs[target] = (owner, group, mode)
                    return subprocess.CompletedProcess(args, 0)
                if args[:3] == ["sudo", "-u", "porter-agent"]:
                    self.calls.append(args)
                    inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return result
                return super().__call__(args, **kwargs)

        try:
            team_launcher.DEFAULT_SESSION_DIR = root_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = root_pane_state
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            config = load_project_config("porter", config_path)
            runner = OwnershipRunner()

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-output.json",
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                )
                == 0
            )
        finally:
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = original_default_pane_state_dir
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        expected_session_dir = owner_home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        expected_pane_state_dir = owner_runtime / "porter-ticket-board" / "pane-state"
        expected_state = expected_pane_state_dir / pane_state_file_name("porter-ops:0.0")
        expected_session_install = [
            "install",
            "-d",
            "-m",
            "700",
            "-o",
            "porter-agent",
            "-g",
            "porter-agent",
            str(expected_session_dir),
        ]
        expected_pane_state_install = [
            "install",
            "-d",
            "-m",
            "700",
            "-o",
            "porter-agent",
            "-g",
            "porter-agent",
            str(expected_pane_state_dir),
        ]
        assert expected_session_dir.is_dir()
        assert expected_pane_state_dir.is_dir()
        assert expected_state.exists()
        assert not root_session_dir.exists()
        assert not root_pane_state.exists()
        assert runner.calls.count(expected_session_install) == 2
        assert runner.calls.count(expected_pane_state_install) == 2
        assert runner.installed_dirs[expected_session_dir] == ("porter-agent", "porter-agent", 0o700)
        assert runner.installed_dirs[expected_pane_state_dir] == ("porter-agent", "porter-agent", 0o700)


def test_owner_state_dir_install_args_take_effect_on_real_filesystem_for_current_user() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-state-real.") as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / "porter.json"
        layout_path = tmp_path / "layout.json"
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": current_user,
                    "layout": str(layout_path),
                    "repository": str(repo_path),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        target = tmp_path / "owner-home" / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"

        result = subprocess.run(team_launcher.install_owner_state_dir_args(config, target), text=True, capture_output=True)

        assert result.returncode == 0, result.stderr
        info = target.stat()
        assert target.owner() == current_user
        assert target.group() == current_user
        assert info.st_mode & 0o777 == 0o700


def test_owner_state_dir_install_is_skipped_when_already_running_as_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-state-same-user.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        config_path = tmp_path / "porter.json"
        layout_path = tmp_path / "layout.json"
        repo_path = tmp_path / "repo"
        session_dir = owner_home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        pane_state_dir = owner_runtime / "pgu-ticket-board" / "pane-state"
        repo_path.mkdir()
        layout_path.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout_path),
                    "repository": str(repo_path),
                    "session_dir": str(session_dir),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        original_current_user_name = team_launcher.current_user_name
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242
            pw_gid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            team_launcher.current_user_name = lambda: "porter-agent"
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            config = load_project_config("porter", config_path)
            runner = FakeRunner()

            team_launcher.ensure_owner_state_dirs(config, pane_state_dir=pane_state_dir, runner=runner)
        finally:
            team_launcher.current_user_name = original_current_user_name
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid

        assert not any(call[:2] == ["install", "-d"] for call in runner.calls)


def test_explicit_pane_state_dir_env_beats_run_as_user_runtime_default() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-env-pane-state.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        env_pane_state = tmp_path / "env" / "pane-state"
        root_pane_state = tmp_path / "run" / "user" / "0" / "pgu-ticket-board" / "pane-state"
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "target": "porter-ops:0.0"}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        env_keys = ("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")
        original_env = {key: os.environ.get(key) for key in env_keys}
        original_default_session_dir = team_launcher.DEFAULT_SESSION_DIR
        original_default_pane_state_dir = team_launcher.DEFAULT_PANE_STATE_DIR
        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid
        original_current_user_name = team_launcher.current_user_name

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        class OwnerAwareRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if args[:3] == ["sudo", "-u", "porter-agent"]:
                    self.calls.append(args)
                    inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return result
                return super().__call__(args, **kwargs)

        try:
            os.environ["TICKET_BOARD_PANE_STATE_DIR"] = str(env_pane_state)
            os.environ.pop("PGU_TICKET_BOARD_PANE_STATE_DIR", None)
            team_launcher.DEFAULT_SESSION_DIR = Path("/root/.local/state/pgu-ticket-board/pane-sessions")
            team_launcher.DEFAULT_PANE_STATE_DIR = root_pane_state
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            config = load_project_config("porter", config_path)
            runner = OwnerAwareRunner()

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-output.json",
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                )
                == 0
            )
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            team_launcher.DEFAULT_SESSION_DIR = original_default_session_dir
            team_launcher.DEFAULT_PANE_STATE_DIR = original_default_pane_state_dir
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        expected_state = env_pane_state / pane_state_file_name("porter-ops:0.0")
        owner_state = owner_runtime / "pgu-ticket-board" / "pane-state" / pane_state_file_name("porter-ops:0.0")
        assert expected_state.exists()
        assert not owner_state.exists()
        assert not (root_pane_state / pane_state_file_name("porter-ops:0.0")).exists()


def test_cli_command_prepends_invoking_user_bin() -> None:
    original_home = os.environ.get("HOME")
    original_generic_bin_dir = os.environ.pop("TEAM_LAUNCHER_BIN_DIR", None)
    original_bin_dir = os.environ.pop("PGU_TEAM_LAUNCHER_BIN_DIR", None)
    try:
        os.environ["HOME"] = "/home/otto-agent"
        config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
        role = next(role for role in config.roles if role.role == "ops")

        command = cli_command_for_role(role, session_dir=config.session_dir)
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
        if original_generic_bin_dir is not None:
            os.environ["TEAM_LAUNCHER_BIN_DIR"] = original_generic_bin_dir
        if original_bin_dir is not None:
            os.environ["PGU_TEAM_LAUNCHER_BIN_DIR"] = original_bin_dir

    assert any(entry.startswith("PATH=/home/otto-agent/bin:") for entry in _env_entries(command))


def test_modern_env_names_win_over_legacy_aliases_with_legacy_fallback() -> None:
    def wayland_from_command(command: list[str]) -> str:
        return next(entry.split("=", 1)[1] for entry in command if entry.startswith("WAYLAND_DISPLAY="))

    def konsole_wayland() -> str:
        return wayland_from_command(konsole_launch_args(Path("/tmp/layout.json"), gui_user="user"))

    cases = [
        {
            "name": "user bin",
            "modern": team_launcher.USER_BIN_ENV,
            "legacy": team_launcher.LEGACY_USER_BIN_ENV,
            "modern_value": "/modern/bin",
            "legacy_value": "/legacy/bin",
            "resolve": default_user_bin,
            "expected_modern": "/modern/bin",
            "expected_legacy": "/legacy/bin",
        },
        {
            "name": "gui user",
            "modern": team_launcher.GUI_USER_ENV,
            "legacy": team_launcher.LEGACY_GUI_USER_ENV,
            "modern_value": "modern-user",
            "legacy_value": "legacy-user",
            "resolve": team_launcher.default_gui_user,
            "expected_modern": "modern-user",
            "expected_legacy": "legacy-user",
        },
        {
            "name": "configured wayland display",
            "modern": team_launcher.GUI_WAYLAND_ENV,
            "legacy": team_launcher.LEGACY_GUI_WAYLAND_ENV,
            "modern_value": "wayland-modern",
            "legacy_value": "wayland-legacy",
            "resolve": konsole_wayland,
            "expected_modern": "/run/user/4242/wayland-modern",
            "expected_legacy": "/run/user/4242/wayland-legacy",
        },
        {
            "name": "host wayland display",
            "modern": team_launcher.HOST_WAYLAND_ENV,
            "legacy": team_launcher.LEGACY_HOST_WAYLAND_ENV,
            "modern_value": "/run/user/1000/wayland-modern-host",
            "legacy_value": "/run/user/1001/wayland-legacy-host",
            "resolve": konsole_wayland,
            "expected_modern": "/run/user/1000/wayland-modern-host",
            "expected_legacy": "/run/user/1001/wayland-legacy-host",
        },
        {
            "name": "pane session dir",
            "modern": "TICKET_BOARD_PANE_SESSION_DIR",
            "legacy": "PGU_TICKET_BOARD_PANE_SESSION_DIR",
            "modern_value": "/tmp/modern-pane-sessions",
            "legacy_value": "/tmp/legacy-pane-sessions",
            "resolve": lambda: str(team_launcher.default_session_dir_for_user("owner")),
            "expected_modern": "/tmp/modern-pane-sessions",
            "expected_legacy": "/tmp/legacy-pane-sessions",
        },
        {
            "name": "pane state dir",
            "modern": "TICKET_BOARD_PANE_STATE_DIR",
            "legacy": "PGU_TICKET_BOARD_PANE_STATE_DIR",
            "modern_value": "/tmp/modern-pane-state",
            "legacy_value": "/tmp/legacy-pane-state",
            "resolve": lambda: str(team_launcher.default_pane_state_dir_for_user("owner", project="pgu")),
            "expected_modern": "/tmp/modern-pane-state",
            "expected_legacy": "/tmp/legacy-pane-state",
        },
    ]
    env_keys = {
        key
        for case in cases
        for key in (str(case["modern"]), str(case["legacy"]))
    }
    original_env = {key: os.environ.get(key) for key in env_keys}
    original_uid_for_user = team_launcher.uid_for_user
    try:
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None
        for case in cases:
            for key in env_keys:
                os.environ.pop(key, None)
            os.environ[str(case["modern"])] = str(case["modern_value"])
            os.environ[str(case["legacy"])] = str(case["legacy_value"])
            assert case["resolve"]() == case["expected_modern"], case["name"]

            os.environ.pop(str(case["modern"]))
            assert case["resolve"]() == case["expected_legacy"], case["name"]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_cli_command_exports_durable_session_dir_for_pane_hooks() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    command = cli_command_for_role(role, session_dir=config.session_dir)

    entries = _env_entries(command)
    assert entries[0] == "TICKET_BOARD_PANE_TARGET=pgu-ops:0.0"
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in entries
    assert f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
    assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
    assert str(config.session_dir) == str(Path.home() / ".local" / "state" / "pgu-ticket-board" / "pane-sessions")
    assert not any(entry.startswith("TICKET_BOARD_PANE_SESSION_ID=") for entry in entries)


def test_cli_command_exports_known_resume_session_id_for_hooks() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = Path(tmp) / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )

        command = cli_command_for_role(role, session_dir=session_dir, resume=True)

    entries = _env_entries(command)
    assert f"TICKET_BOARD_PANE_SESSION_ID={session_id}" in entries
    assert f"PGU_PANE_SESSION_ID={session_id}" in entries
    assert _command_tail(command)[:3] == ["codex", "resume", session_id]


def test_custom_config_without_run_as_user_tracks_invoking_home() -> None:
    original_home = os.environ.get("HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            tmp_path = Path(tmp)
            layout = {"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}
            layout_path = tmp_path / "layout.json"
            layout_path.write_text(json.dumps(layout), encoding="utf-8")
            config_path = tmp_path / "porter.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "porter",
                        "layout": str(layout_path),
                        "repository": "$HOME/Projects/porter",
                        "roles": [{"role": "ops", "slot": 0, "cli": ["codex"]}],
                    }
                ),
                encoding="utf-8",
            )
            os.environ["HOME"] = "/home/otto-agent"

            config = load_project_config("porter", config_path)
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home

    assert config.run_as_user == ""
    assert config.board_url == "http://127.0.0.1:23682"
    assert config.board_socket == "/run/porter-ticket-board/ticket-board.sock"
    assert config.repository == Path("/home/otto-agent/Projects/porter")
    assert config.roles[0].workdir == "/home/otto-agent/Projects/porter"


def test_custom_project_pane_env_is_wired_to_project_board_and_role_map() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        layout = {"KonsoleTabs": [{"Widgets": [{"Command": "", "WorkingDirectory": ""}]}]}
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(layout_path),
                    "repository": "/home/otto-agent/Projects/otto",
                    "roles": [
                        {"role": "director", "slot": 0, "tmux_session": "otto-director", "cli": ["claude"]},
                        {"role": "implementer", "slot": 1, "tmux_session": "otto-implementer", "cli": ["codex"]},
                    ],
                }
            ),
            encoding="utf-8",
        )

        config = load_project_config("otto", config_path)
        role = next(role for role in config.roles if role.role == "implementer")
        command = cli_command_for_role(role, session_dir=config.session_dir)

    entries = _env_entries(command)
    role_map = json.loads(next(entry.split("=", 1)[1] for entry in entries if entry.startswith("TICKET_BOARD_CALLER_ROLE_MAP=")))
    assert config.board_url == "http://127.0.0.1:20740"
    assert config.board_socket == "/run/otto-ticket-board/ticket-board.sock"
    assert f"TICKET_BOARD_PANE_TARGET={role.target}" in entries
    assert "TICKET_BOARD_PROJECT=otto" in entries
    assert "TICKET_BOARD_URL=http://127.0.0.1:20740" in entries
    assert "TICKET_BOARD_SOCKET=/run/otto-ticket-board/ticket-board.sock" in entries
    assert "TICKET_BOARD_CALLER_ROLE=implementer" in entries
    assert role_map == {"otto-director": "director", "otto-implementer": "implementer"}
    assert not any(entry.startswith("PGU_TICKET_BOARD_SOCKET=") for entry in entries)
    assert not any(entry.startswith("PGU_TICKET_BOARD_URL=") for entry in entries)


def test_pane_state_filename_matches_notify_listener_target_path() -> None:
    target = "pgu-ops:0.0"
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        listener_path = PaneHookStateStore(Path(tmp))._target_path(target)

    assert pane_state_file_name(target) == listener_path.name == "pgu-ops_0.0.json"


def test_ensure_user_linger_runtime_enables_linger_and_waits_for_runtime_dir() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    calls: list[list[str]] = []
    checks: list[Path] = []
    sleeps: list[float] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def runtime_exists(path: Path) -> bool:
        checks.append(path)
        return len(checks) == 3

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None

        runtime_dir = ensure_user_linger_runtime(
            "otto-agent",
            runner=runner,
            runtime_exists=runtime_exists,
            sleeper=sleeps.append,
            attempts=5,
            poll_seconds=0.25,
        )
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert runtime_dir == Path("/run/user/1005")
    assert calls == [["loginctl", "enable-linger", "otto-agent"]]
    assert checks == [Path("/run/user/1005"), Path("/run/user/1005"), Path("/run/user/1005")]
    assert sleeps == [0.25, 0.25]


def test_ensure_user_linger_runtime_fails_loud_when_loginctl_is_denied() -> None:
    original_uid_for_user = team_launcher.uid_for_user

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Access denied")

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None

        try:
            ensure_user_linger_runtime(
                "otto-agent",
                runner=runner,
                runtime_exists=lambda _path: False,
                sleeper=lambda _seconds: None,
                attempts=1,
            )
            raise AssertionError("expected SystemExit")
        except SystemExit as exc:
            message = str(exc)
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert "failed to enable linger for 'otto-agent': Access denied" in message
    assert "sudo loginctl enable-linger otto-agent" in message


def test_configured_runtime_check_skips_non_runtime_session_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
        config = load_project_config("pgu", config_path)

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f"unexpected runtime provisioning call: {args}")

        assert ensure_configured_runtime_user(config, runner=runner) is None


def test_configured_runtime_check_uses_run_as_user_for_default_runtime_dir() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else None
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["run_as_user"] = "otto-agent"
            raw["session_dir"] = "/run/user/1005/pgu-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("pgu", config_path)

            runtime_dir = ensure_configured_runtime_user(
                config,
                runner=runner,
                runtime_exists=lambda path: path == Path("/run/user/1005"),
                sleeper=lambda _seconds: None,
                attempts=1,
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    assert runtime_dir == Path("/run/user/1005")
    assert calls == [["loginctl", "enable-linger", "otto-agent"]]


def test_provision_runtime_command_uses_configured_project_user() -> None:
    original_ensure = team_launcher.ensure_user_linger_runtime
    calls: list[str] = []

    def fake_ensure(user_name: str) -> Path:
        calls.append(user_name)
        return Path("/run/user/1005")

    try:
        team_launcher.ensure_user_linger_runtime = fake_ensure
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            config_path = _write_pgu_config_with_shared_checkout(Path(tmp))
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            raw["run_as_user"] = "otto-agent"
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("pgu", config_path)

            assert provision_runtime_command(None, config) == 0
    finally:
        team_launcher.ensure_user_linger_runtime = original_ensure

    assert calls == ["otto-agent"]


def test_yolo_config_translates_to_cli_specific_bypass_flags() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    expected_flags = {
        "director": ["--dangerously-skip-permissions"],
        "main": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "app": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "research": ["--dangerously-skip-permissions"],
        "ops": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
        "audit": ["--dangerously-skip-permissions"],
        "inspector": ["--dangerously-skip-permissions"],
    }

    for role_name, flags in expected_flags.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        for expected_flag in flags:
            assert expected_flag in command, (role_name, command)


def test_effort_config_translates_to_cli_specific_args() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    for role_name in ("director", "research", "audit"):
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert "--effort" in command, (role_name, command)
        assert command[command.index("--effort") + 1] == "high"
        assert "reasoning_effort=high" not in command

    for role_name in ("main", "app", "ops"):
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert "-c" in command, (role_name, command)
        assert command[command.index("-c") + 1] == "reasoning_effort=high"
        assert "--effort" not in command

    inspector_command = cli_command_for_role(roles["inspector"], session_dir=config.session_dir)
    assert "--effort" not in inspector_command
    assert "-c" not in inspector_command
    assert "reasoning_effort=high" not in inspector_command


def test_pgu_launch_commands_include_model_and_bypass_flags() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_by_role = {
        "director": ["claude", "--model", "claude-opus-5", "--effort", "high", "--dangerously-skip-permissions"],
        "main": [
            "codex",
            "--model",
            "gpt-5.6-sol",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "app": [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "research": ["claude", "--model", "claude-opus-5", "--effort", "high", "--dangerously-skip-permissions"],
        "ops": [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "audit": ["claude", "--model", "claude-opus-5", "--effort", "high", "--dangerously-skip-permissions"],
        "inspector": ["agy", "--model", "gemini-3.7-flash-high", "--dangerously-skip-permissions"],
    }

    for role_name, expected_tail in expected_by_role.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        entries = _env_entries(command)
        assert entries[0] == f"TICKET_BOARD_PANE_TARGET=pgu-{role_name}:0.0"
        assert f"PGU_PANE_TARGET=pgu-{role_name}:0.0" in entries
        assert f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
        assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}" in entries
        assert any(entry.startswith(f"PATH={default_user_bin()}:") for entry in entries), (role_name, command)
        assert _command_tail(command) == expected_tail, (role_name, command)
        assert "--reasoning-effort" not in command
        assert "--continue" not in command
        assert "--last" not in command
        assert "gemini" not in command
        assert "--yolo" not in command


def test_start_auto_fast_forwards_stale_launcher_checkout_once_before_panes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        fake = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        counts = ["0 173\n", "0 0\n"]
        fast_forwarded = False
        calls: list[list[str]] = []

        def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal fast_forwarded
            calls.append(args)
            if args == git_launcher_head_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD if fast_forwarded else LOCAL_HEAD}\n")
            if args == git_launcher_ls_remote_ref_args(config, ROOT):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
            if args == git_launcher_commit_exists_args(ROOT, REMOTE_HEAD):
                return subprocess.CompletedProcess(args, 0)
            if args == git_launcher_ahead_behind_args(config, ROOT, REMOTE_HEAD):
                return subprocess.CompletedProcess(args, 0, stdout=counts.pop(0))
            if args == git_launcher_current_branch_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout="main\n")
            if args == git_launcher_status_porcelain_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout="")
            if args == git_launcher_head_short_args(ROOT):
                return subprocess.CompletedProcess(args, 0, stdout="abc1234\n" if counts else "def5678\n")
            if args == git_fast_forward_launcher_ref_args(config, ROOT):
                fast_forwarded = True
                return subprocess.CompletedProcess(args, 0)
            return fake(args, **kwargs)

        stderr = StringIO()
        with redirect_stderr(stderr):
            result = launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout.json",
                pane_state_dir=tmp_path / "pane-state",
            )

        assert result == 0
        assert git_fast_forward_launcher_ref_args(config, ROOT) in calls
        assert calls.count(git_fast_forward_launcher_ref_args(config, ROOT)) == 1
        assert "auto-fast-forwarded launcher checkout" in stderr.getvalue()
        assert "was 173 commit(s) behind origin/main" in stderr.getvalue()
        layout = json.loads((tmp_path / "layout.json").read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert commands
        assert all("--skip-launcher-check" in command for command in commands if " pane " in command)


def test_launcher_freshness_probe_uses_owner_runner_when_launcher_user_differs() -> None:
    original_current_user_name = team_launcher.current_user_name
    original_repo_root = team_launcher._repo_root
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-freshness.") as tmp:
            tmp_path = Path(tmp)
            config_path = _write_pgu_config_with_shared_checkout(tmp_path)
            launcher_repo = tmp_path / "repo"
            launcher_repo.mkdir()
            config = load_project_config("pgu", config_path)
            runner = SudoAwareFakeRunner()
            team_launcher.current_user_name = lambda: "root"
            team_launcher._repo_root = lambda: launcher_repo

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "launch-layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )

        launcher_probe_calls = [
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]
        for call in launcher_probe_calls:
            assert ["sudo", "-u", "agent", *call] in runner.calls
            assert call not in runner.calls
    finally:
        team_launcher.current_user_name = original_current_user_name
        team_launcher._repo_root = original_repo_root


def test_launcher_freshness_probe_uses_launcher_checkout_owner_for_generated_project_shape() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-generated-freshness.") as tmp:
            tmp_path = Path(tmp)
            launcher_repo = tmp_path / "owner-home" / "otto-ticketboard-live" / "current"
            pane_launcher = launcher_repo / "scripts" / "team-launcher"
            project_repo = tmp_path / "project"
            layout = tmp_path / "layout.json"
            config_path = tmp_path / "otto.json"
            pane_launcher.parent.mkdir(parents=True)
            pane_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            pane_launcher.chmod(0o755)
            project_repo.mkdir()
            layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "project": "otto",
                        "layout": str(layout),
                        "pane_launcher": str(pane_launcher),
                        "repository": str(project_repo),
                        "run_as_user": "otto-agent",
                        "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_project_config("otto", config_path)
            checkout_owner = launcher_repo.owner()
            runner = SudoAwareFakeRunner()
            project_owner_runner = team_launcher._owner_project_git_runner(
                owner_user="otto-agent",
                project_dir=project_repo,
                owned_roots=team_launcher._control_repository_owned_roots(config),
                runner=runner,
            )
            team_launcher.current_user_name = lambda: "root"

            team_launcher.ensure_launcher_checkout_current(
                config,
                launcher_repo=launcher_repo,
                runner=project_owner_runner,
                auto_deploy=False,
            )

        launcher_probe_calls = [
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]
        for call in launcher_probe_calls:
            assert ["sudo", "-u", checkout_owner, *call] in runner.calls
            assert ["sudo", "-u", "otto-agent", *call] not in runner.calls
            assert call not in runner.calls
    finally:
        team_launcher.current_user_name = original_current_user_name


def test_launcher_freshness_probe_runs_direct_when_checkout_owner_is_current_user() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-current-owner-freshness.") as tmp:
            launcher_repo = Path(tmp) / "repo"
            launcher_repo.mkdir()
            config_path = _write_minimal_shared_checkout_config(Path(tmp), launcher_repo, ["ops"])
            config = load_project_config("pgu", config_path)
            runner = SudoAwareFakeRunner()
            checkout_owner = launcher_repo.owner()
            team_launcher.current_user_name = lambda: checkout_owner

            team_launcher.ensure_launcher_checkout_current(config, launcher_repo=launcher_repo, runner=runner)

        assert git_launcher_checkout_check_args(launcher_repo) in runner.calls
        assert not any(call[:3] == ["sudo", "-u", checkout_owner] for call in runner.calls)
    finally:
        team_launcher.current_user_name = original_current_user_name


def test_owner_correct_git_skips_missing_target_without_owner_rule() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-missing-git-target.") as tmp:
        missing_repo = Path(tmp) / "missing-repo"
        args = ["git", "-C", str(missing_repo), "status"]

        result = team_launcher.run_owner_correct_git(args, runner=runner)

    assert result.returncode == 125
    assert calls == []
    assert "owner-correct git skipped" in str(result.stderr)
    assert "does not exist and no owner rule matched" in str(result.stderr)


def test_owner_correct_git_skips_command_without_target_path() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0)

    result = team_launcher.run_owner_correct_git(["git", "status"], runner=runner)

    assert result.returncode == 125
    assert calls == []
    assert "owner-correct git skipped" in str(result.stderr)
    assert "does not declare a target path" in str(result.stderr)


def test_launcher_freshness_probe_skips_when_checkout_owner_is_unknown() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    launcher_repo = Path("/tmp/pgu-launcher-owner-unknown")
    calls: list[list[str]] = []
    original_getpwuid = team_launcher.pwd.getpwuid
    try:
        team_launcher.pwd.getpwuid = lambda _uid: (_ for _ in ()).throw(KeyError(_uid))

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        stderr = StringIO()
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-unknown-owner.") as tmp:
            repo = Path(tmp)
            with redirect_stderr(stderr):
                team_launcher.ensure_launcher_checkout_current(
                    config,
                    launcher_repo=repo,
                    runner=runner,
                    auto_deploy=True,
                )
    finally:
        team_launcher.pwd.getpwuid = original_getpwuid

    assert not calls
    assert "cannot determine launcher checkout owner" in stderr.getvalue()
    assert "skipping freshness probe" in stderr.getvalue()


def test_deploy_launcher_checkout_uses_launcher_checkout_owner() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-deploy-owner.") as tmp:
            tmp_path = Path(tmp)
            launcher_repo = tmp_path / "repo"
            launcher_repo.mkdir()
            config_path = _write_minimal_shared_checkout_config(tmp_path, tmp_path / "project", ["ops"])
            config = load_project_config("pgu", config_path)
            checkout_owner = launcher_repo.owner()
            calls: list[list[str]] = []

            def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
                calls.append(args)
                effective_args = args[3:] if args[:3] == ["sudo", "-u", checkout_owner] else args
                if effective_args == git_launcher_head_args(launcher_repo):
                    return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\n")
                if effective_args == git_launcher_ls_remote_ref_args(config, launcher_repo):
                    return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
                return subprocess.CompletedProcess(args, 0)

            team_launcher.current_user_name = lambda: "root"

            assert team_launcher.deploy_launcher_checkout(config, launcher_repo=launcher_repo, runner=runner) == 0

        expected_calls = [
            git_launcher_checkout_check_args(launcher_repo),
            git_fetch_launcher_ref_args(config, launcher_repo),
            git_checkout_launcher_branch_args(config, launcher_repo),
            git_fast_forward_launcher_ref_args(config, launcher_repo),
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]
        for call in expected_calls:
            assert ["sudo", "-u", checkout_owner, *call] in calls
            assert call not in calls
    finally:
        team_launcher.current_user_name = original_current_user_name


def test_start_no_self_deploy_refuses_stale_launcher_checkout() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:] == ["rev-parse", "--is-inside-work-tree"]:
            return subprocess.CompletedProcess(args, 0)
        if args == git_launcher_head_args(ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{LOCAL_HEAD}\n")
        if args == git_launcher_ls_remote_ref_args(config, ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
        if args == git_launcher_commit_exists_args(ROOT, REMOTE_HEAD):
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["git", "-C", str(ROOT)] and args[3:6] == ["rev-list", "--left-right", "--count"]:
            return subprocess.CompletedProcess(args, 0, stdout="0 173\n")
        raise AssertionError(f"unexpected call after stale launcher refusal: {args}")

    try:
        launch_project(
            config,
            config_path=ROOT / "config" / "team-launcher" / "pgu.json",
            mode="start",
            script_path=ROOT / "scripts" / "team-launcher",
            runner=runner,
            no_launcher_self_deploy=True,
        )
        raise AssertionError("expected stale launcher checkout to abort")
    except SystemExit as exc:
        message = str(exc)

    assert "refusing to launch from stale checkout" in message
    assert "173 commit(s) behind origin/main" in message
    assert "deploy-launcher" in message
    assert calls == [
        git_launcher_checkout_check_args(ROOT),
        git_launcher_head_args(ROOT),
        git_launcher_ls_remote_ref_args(config, ROOT),
        git_launcher_commit_exists_args(ROOT, REMOTE_HEAD),
        git_launcher_ahead_behind_args(config, ROOT, REMOTE_HEAD),
    ]


def test_allow_stale_launcher_override_warns_and_continues_without_fast_forward() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == git_launcher_checkout_check_args(ROOT):
            return subprocess.CompletedProcess(args, 0)
        if args == git_launcher_head_args(ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{LOCAL_HEAD}\n")
        if args == git_launcher_ls_remote_ref_args(config, ROOT):
            return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
        if args == git_launcher_commit_exists_args(ROOT, REMOTE_HEAD):
            return subprocess.CompletedProcess(args, 0)
        if args == git_launcher_ahead_behind_args(config, ROOT, REMOTE_HEAD):
            return subprocess.CompletedProcess(args, 0, stdout="0 12\n")
        raise AssertionError(f"unexpected call after override: {args}")

    stderr = StringIO()
    with redirect_stderr(stderr):
        team_launcher.ensure_launcher_checkout_current(
            config,
            runner=runner,
            auto_deploy=False,
            allow_stale=True,
        )

    assert "OVERRIDE proceeding with stale launcher checkout" in stderr.getvalue()
    assert git_fast_forward_launcher_ref_args(config, ROOT) not in calls
    assert git_fetch_launcher_ref_args(config, ROOT) not in calls


def test_launcher_status_probe_uses_ls_remote_without_writing_git_objects() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        origin, launcher_repo = _make_origin_backed_repo(tmp_path)
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("remote update\n", encoding="utf-8")
        _commit_all(updater, "remote update")
        push_env = {**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"}
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env=push_env,
        )
        config_path = _write_minimal_shared_checkout_config(tmp_path, launcher_repo, ["ops"])
        config = load_project_config("pgu", config_path)

        def git_metadata_snapshot() -> dict[Path, bytes | None]:
            git_dir = launcher_repo / ".git"
            snapshot: dict[Path, bytes | None] = {}
            for path in git_dir.rglob("*"):
                relative = path.relative_to(git_dir)
                snapshot[relative] = path.read_bytes() if path.is_file() else None
            return snapshot

        git_metadata_before = git_metadata_snapshot()

        ahead, behind = team_launcher.launcher_checkout_status(config, launcher_repo=launcher_repo)

        git_metadata_after = git_metadata_snapshot()
        assert (ahead, behind) == (0, 1)
        assert git_metadata_after == git_metadata_before


def test_auto_fast_forward_launcher_checkout_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        origin, launcher_repo = _make_origin_backed_repo(tmp_path)
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("updated\n", encoding="utf-8")
        _commit_all(updater, "update")
        push_env = {**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"}
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env=push_env,
        )
        config_path = _write_minimal_shared_checkout_config(tmp_path, launcher_repo, ["ops"])
        config = load_project_config("pgu", config_path)
        untracked_settings = launcher_repo / ".claude" / "settings.json"
        untracked_settings.parent.mkdir()
        untracked_settings.write_text("{}\n", encoding="utf-8")

        before = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()
        stderr = StringIO()
        with redirect_stderr(stderr):
            team_launcher.ensure_launcher_checkout_current(
                config,
                launcher_repo=launcher_repo,
                auto_deploy=True,
            )
        after = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()

        assert before != after
        assert _run_git(["git", "status", "--porcelain"], cwd=launcher_repo).stdout == "?? .claude/\n"
        assert untracked_settings.read_text(encoding="utf-8") == "{}\n"
        assert "auto-fast-forwarded launcher checkout" in stderr.getvalue()


def test_auto_fast_forward_launcher_checkout_refuses_tracked_modifications() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        origin, launcher_repo = _make_origin_backed_repo(tmp_path)
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("upstream\n", encoding="utf-8")
        _commit_all(updater, "upstream")
        push_env = {**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"}
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env=push_env,
        )
        config_path = _write_minimal_shared_checkout_config(tmp_path, launcher_repo, ["ops"])
        config = load_project_config("pgu", config_path)
        before = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()
        (launcher_repo / "tracked.txt").write_text("local edit\n", encoding="utf-8")

        try:
            team_launcher.ensure_launcher_checkout_current(
                config,
                launcher_repo=launcher_repo,
                auto_deploy=True,
            )
            raise AssertionError("expected tracked local edit to block auto fast-forward")
        except SystemExit as exc:
            message = str(exc)
        after = _run_git(["git", "rev-parse", "HEAD"], cwd=launcher_repo).stdout.strip()

        assert before == after
        assert "local changes are present" in message
        assert (launcher_repo / "tracked.txt").read_text(encoding="utf-8") == "local edit\n"


def test_undeterminable_launcher_checkout_warns_and_continues() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == git_launcher_checkout_check_args(ROOT):
            return subprocess.CompletedProcess(args, 1)
        raise AssertionError(f"unexpected call after undeterminable launcher check: {args}")

    stderr = StringIO()
    with redirect_stderr(stderr):
        team_launcher.ensure_launcher_checkout_current(config, runner=runner, auto_deploy=True)

    assert "cannot determine launcher checkout freshness" in stderr.getvalue()
    assert "continuing" in stderr.getvalue()
    assert calls == [git_launcher_checkout_check_args(ROOT)]


def test_deploy_launcher_checkout_updates_and_verifies_configured_ref() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-deploy-current-owner.") as tmp:
        launcher_repo = Path(tmp) / "pgu"
        launcher_repo.mkdir()
        calls: list[list[str]] = []

        def runner(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args == git_launcher_head_args(launcher_repo):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\n")
            if args == git_launcher_ls_remote_ref_args(config, launcher_repo):
                return subprocess.CompletedProcess(args, 0, stdout=f"{REMOTE_HEAD}\trefs/heads/main\n")
            if args[:3] == ["git", "-C", str(launcher_repo)] and args[3:6] == ["rev-list", "--left-right", "--count"]:
                return subprocess.CompletedProcess(args, 0, stdout="0 0\n")
            return subprocess.CompletedProcess(args, 0)

        assert deploy_launcher_checkout(config, launcher_repo=launcher_repo, runner=runner, clean=True) == 0

        assert calls == [
            git_launcher_checkout_check_args(launcher_repo),
            git_fetch_launcher_ref_args(config, launcher_repo),
            git_checkout_launcher_branch_args(config, launcher_repo),
            git_fast_forward_launcher_ref_args(config, launcher_repo),
            git_clean_launcher_checkout_args(launcher_repo),
            git_launcher_checkout_check_args(launcher_repo),
            git_launcher_head_args(launcher_repo),
            git_launcher_ls_remote_ref_args(config, launcher_repo),
        ]


def test_konsole_launch_uses_gui_user_display_environment() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_sudo_user = os.environ.get("SUDO_USER")
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        os.environ["SUDO_USER"] = "root"
        team_launcher.uid_for_user = lambda _user_name: None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user


def test_konsole_launch_normalizes_relative_host_wayland_display() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_sudo_user = os.environ.get("SUDO_USER")
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        os.environ["SUDO_USER"] = "eric"
        team_launcher.current_user_name = lambda: "root"

        def fake_uid_for_user(user_name: str) -> int | None:
            return {"eric": 1000, "root": 0}.get(user_name)

        team_launcher.uid_for_user = fake_uid_for_user

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_gui_user is not None:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user


def test_konsole_launch_can_fallback_to_gui_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name


def test_konsole_launch_prefers_host_wayland_over_configured_wayland_name() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    env_keys = [
        "HOST_WAYLAND_DISPLAY",
        "PGU_HOST_WAYLAND_DISPLAY",
        "TEAM_LAUNCHER_WAYLAND_DISPLAY",
        "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY",
    ]
    original_env = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-host"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-configured"
        os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-host",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_konsole_launch_prefers_legacy_host_wayland_over_legacy_configured_wayland_name() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    env_keys = [
        "HOST_WAYLAND_DISPLAY",
        "PGU_HOST_WAYLAND_DISPLAY",
        "TEAM_LAUNCHER_WAYLAND_DISPLAY",
        "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY",
    ]
    original_env = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        os.environ["PGU_HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-legacy-host"
        os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-legacy-configured"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-legacy-host",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_konsole_launch_defaults_to_invoking_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_sudo_user = os.environ.pop("SUDO_USER", None)
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        team_launcher.current_user_name = lambda: "otto-agent"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "otto-agent" else None

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_gui_user is not None:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is not None:
            os.environ["SUDO_USER"] = original_sudo_user
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name


def test_konsole_launch_defaults_to_sudo_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_sudo_user = os.environ.get("SUDO_USER")
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["SUDO_USER"] = "eric"
        os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        team_launcher.current_user_name = lambda: "root"
        team_launcher.uid_for_user = lambda user_name: 1000 if user_name == "eric" else 0 if user_name == "root" else None

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_gui_user is not None:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name


def test_configured_gui_user_overrides_sudo_user() -> None:
    original_current_user_name = team_launcher.current_user_name
    original_gui_user = os.environ.get("TEAM_LAUNCHER_GUI_USER")
    original_legacy_gui_user = os.environ.get("PGU_TEAM_LAUNCHER_GUI_USER")
    original_sudo_user = os.environ.get("SUDO_USER")
    try:
        os.environ["TEAM_LAUNCHER_GUI_USER"] = "desktop-user"
        os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
        os.environ["SUDO_USER"] = "eric"
        team_launcher.current_user_name = lambda: "root"

        assert team_launcher.default_gui_user() == "desktop-user"
    finally:
        team_launcher.current_user_name = original_current_user_name
        if original_gui_user is None:
            os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
        else:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is None:
            os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
        else:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user


def test_konsole_launch_falls_back_to_clear_error_when_gui_user_missing() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    try:
        team_launcher.uid_for_user = lambda _user_name: None

        command = konsole_launch_args(Path("/tmp/layout.json"), gui_user="nobody")
        assert command[:2] == ["sh", "-lc"]
        assert "no host Wayland display" in command[2]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland


def test_launch_konsole_window_detaches_real_spawn_and_returns_status_line() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stdout = sys.stdout
    expected_args: list[str]
    stdout = StringIO()
    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            popen_calls.append({"args": args, "kwargs": kwargs})
            self.pid = 424242

        def poll(self) -> int | None:
            return None

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stdout = stdout
        expected_args = konsole_launch_args(Path("/tmp/layout.json"))

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == 0
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stdout = original_stdout
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["args"] == expected_args
    kwargs = call["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert hasattr(kwargs["stdout"], "name")
    assert kwargs["stderr"] is kwargs["stdout"]
    assert "team-launcher: started pgu in background (Konsole pid 424242; log " in stdout.getvalue()


def test_launch_konsole_window_prints_captured_output_on_immediate_exit() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stderr = sys.stderr
    stderr = StringIO()
    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            popen_calls.append({"args": args, "kwargs": kwargs})
            handle = kwargs["stderr"]
            handle.write(b"qt.qpa.wayland: XDG_RUNTIME_DIR is invalid or not set\n")
            handle.flush()
            self.pid = 424243

        def poll(self) -> int | None:
            return -6

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stderr = stderr

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == -6
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stderr = original_stderr
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert "QT_LOGGING_RULES=qt.qpa.wayland.warning=false" not in call["args"]
    log_path = Path(call["kwargs"]["stdout"].name)
    assert log_path.stat().st_mode & 0o777 == 0o644
    message = stderr.getvalue()
    assert "team-launcher: Konsole exited immediately with status -6; captured output:" in message
    assert "qt.qpa.wayland: XDG_RUNTIME_DIR is invalid or not set" in message
    assert "; see " not in message


def test_launch_konsole_window_names_empty_capture_on_immediate_exit() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stderr = sys.stderr
    stderr = StringIO()
    log_paths: list[Path] = []

    class FakePopen:
        def __init__(self, _args: list[str], **kwargs: Any) -> None:
            log_paths.append(Path(kwargs["stdout"].name))
            self.pid = 424244

        def poll(self) -> int | None:
            return -6

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stderr = stderr

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == -6
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stderr = original_stderr
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(log_paths) == 1
    assert f"team-launcher: Konsole log {log_paths[0]} was empty" in stderr.getvalue()


def test_konsole_log_is_readable_by_sudo_invoking_user() -> None:
    original_chown = team_launcher.os.chown
    chown_calls: list[tuple[Path, int, int]] = []

    def fake_chown(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], uid: int, gid: int) -> None:
        chown_calls.append((Path(path), uid, gid))

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-konsole-log.") as tmp:
        log_path = Path(tmp) / "konsole.log"
        log_path.write_text("", encoding="utf-8")
        log_path.chmod(0o600)
        try:
            team_launcher.os.chown = fake_chown
            team_launcher._make_konsole_log_readable(
                log_path,
                environ={"SUDO_USER": "user", "SUDO_UID": "1000", "SUDO_GID": "1000"},
            )
        finally:
            team_launcher.os.chown = original_chown

        assert chown_calls == [(log_path, 1000, 1000)]
        assert log_path.stat().st_mode & 0o777 == 0o644


def test_start_is_attach_or_start_and_never_duplicates_existing_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp) / "sessions"
        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "attach", "-t", "pgu-ops"],
    ]


def test_start_creates_missing_session_once_then_attaches() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        # No recorded session id -> start falls back to a fresh launch (no resume args).
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        pane_state_dir = tmp_path / "pane-state"
        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        state = _read_pane_state(pane_state_dir, role.target)
        assert state["target"] == role.target
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        assert isinstance(state["updated_at"], float)

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert runner.calls[1][5:7] == ["-c", str(_pgu_project_root())]
    assert "TICKET_BOARD_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "--model gpt-5.5" in runner.calls[1][-1]
    assert "-c reasoning_effort=high" in runner.calls[1][-1]
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.calls[1][-1]
    assert "--dangerously-bypass-hook-trust" in runner.calls[1][-1]
    assert "--reasoning-effort" not in runner.calls[1][-1]
    assert " resume " not in f" {runner.calls[1][-1]} "
    assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
    assert runner.calls[3] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
    assert runner.calls[4] == ["tmux", "attach", "-t", "pgu-ops"]


def test_visible_start_fails_before_attach_when_cli_exits_immediately() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    class DeadFreshRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.existing = False

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 0 if self.existing else 1)
            if args[:2] == ["tmux", "new-session"]:
                self.existing = True
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 1, stdout="")
            if args[:2] == ["tmux", "attach"]:
                raise AssertionError("dead fresh pane should fail before attach")
            return subprocess.CompletedProcess(args, 0)

    runner = DeadFreshRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        pane_state_dir = tmp_path / "pane-state"
        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=pane_state_dir,
                    runner=runner,
                )
                == 1
            )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"] for call in runner.calls)
    assert "role ops did not leave a live pgu-ops session" in stderr.getvalue()


def test_clear_session_record_preserves_superseded_history_after_second_fallback() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        live_path = session_dir / session_file_name(role.target)
        sidecar_path = live_path.with_name(f"{live_path.name}.superseded")
        previous_sidecar_path = live_path.with_name(f"{live_path.name}.superseded.1")
        first_session_id = "11111111-1111-4111-8111-111111111111"
        second_session_id = "22222222-2222-4222-8222-222222222222"
        live_path.write_text(
            json.dumps({"target": role.target, "session_id": first_session_id}) + "\n",
            encoding="utf-8",
        )

        assert clear_session_record_for_role(role, session_dir) is True
        assert not live_path.exists()
        assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == first_session_id
        assert not previous_sidecar_path.exists()

        live_path.write_text(
            json.dumps({"target": role.target, "session_id": second_session_id}) + "\n",
            encoding="utf-8",
        )

        assert clear_session_record_for_role(role, session_dir) is True
        assert not live_path.exists()
        assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == second_session_id
        assert json.loads(previous_sidecar_path.read_text(encoding="utf-8"))["session_id"] == first_session_id
        saved_session_ids = {
            json.loads(path.read_text(encoding="utf-8"))["session_id"]
            for path in (sidecar_path, previous_sidecar_path)
        }
        assert first_session_id in saved_session_ids
        assert clear_session_record_for_role(role, session_dir) is False
        assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == second_session_id
        assert json.loads(previous_sidecar_path.read_text(encoding="utf-8"))["session_id"] == first_session_id


def test_start_seeds_initial_idle_state_for_codex_agy_and_claude_panes() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_cli_by_role = {
        "ops": "codex",
        "inspector": "agy",
        "director": "claude",
    }

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        for role_name, cli_name in expected_cli_by_role.items():
            role = roles[role_name]
            runner = FakeRunner()
            assert (
                run_role_pane(
                    role,
                    mode="start",
                    session_dir=tmp_path / role_name / "sessions",
                    pane_state_dir=tmp_path / role_name / "pane-state",
                    runner=runner,
                )
                == 0
            )
            state = _read_pane_state(tmp_path / role_name / "pane-state", role.target)
            assert state == {
                "source": "team_launcher.start",
                "state": "idle",
                "target": role.target,
                "updated_at": state["updated_at"],
            }
            assert isinstance(state["updated_at"], float)
            assert f" {cli_name} " in f" {runner.calls[1][-1]} "


def test_start_resumes_recorded_session_when_recreating_missing_pane() -> None:
    # start (attach-or-start) must resume a tracked session id when relaunching a
    # stopped pane -- resume is not reload-only. A cold restart uses start mode.
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = Path(tmp) / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(current_commands={"pgu-ops:0.0": "codex"})

        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        state = _read_pane_state(pane_state_dir, role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert f"codex resume {session_id}" in runner.calls[1][-1]
    # resume verified via pane inspection -> no fresh fallback, then attach
    assert ["tmux", "kill-session", "-t", "pgu-ops"] not in runner.calls
    assert runner.calls[-1] == ["tmux", "attach", "-t", "pgu-ops"]


def test_seed_session_dir_from_legacy_sources_copies_valid_records_once() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        durable = tmp_path / "durable"
        legacy_runtime = tmp_path / "runtime" / "pane-sessions"
        interim_backup = tmp_path / "backup" / "pane-sessions"
        legacy_runtime.mkdir(parents=True)
        interim_backup.mkdir(parents=True)
        ops_id = "12345678-1234-5678-9abc-def012345678"
        audit_id = "22345678-1234-5678-9abc-def012345678"
        (legacy_runtime / session_file_name(roles["ops"].target)).write_text(
            json.dumps({"target": roles["ops"].target, "session_id": ops_id, "payload": {"model": "gpt-5.5"}}) + "\n",
            encoding="utf-8",
        )
        (interim_backup / session_file_name(roles["audit"].target)).write_text(
            json.dumps({"target": roles["audit"].target, "session_id": audit_id}) + "\n",
            encoding="utf-8",
        )
        (interim_backup / "bad.json").write_text(json.dumps({"target": roles["main"].target}) + "\n", encoding="utf-8")

        copied = seed_session_dir_from_legacy_sources(durable, candidates=[legacy_runtime, interim_backup])

        assert sorted(path.name for path in copied) == sorted(
            [session_file_name(roles["ops"].target), session_file_name(roles["audit"].target)]
        )
        assert session_id_for_role(roles["ops"], durable) == ops_id
        assert session_id_for_role(roles["audit"], durable) == audit_id
        assert oct(durable.stat().st_mode & 0o777) == "0o700"
        assert oct((durable / session_file_name(roles["ops"].target)).stat().st_mode & 0o777) == "0o600"

        (legacy_runtime / session_file_name(roles["main"].target)).write_text(
            json.dumps({"target": roles["main"].target, "session_id": "new-session"}) + "\n",
            encoding="utf-8",
        )
        assert seed_session_dir_from_legacy_sources(durable, candidates=[legacy_runtime]) == []
        assert session_id_for_role(roles["main"], durable) == ""


def test_start_resumes_from_durable_session_dir_after_runtime_tmpfs_is_cleared() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(current_commands={"pgu-ops:0.0": "codex"})
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        durable = tmp_path / "state-home" / "pgu-ticket-board" / "pane-sessions"
        runtime_tmpfs = tmp_path / "run" / "pane-sessions"
        durable.mkdir(parents=True)
        runtime_tmpfs.mkdir(parents=True)
        session_id = "32345678-1234-5678-9abc-def012345678"
        transcript = tmp_path / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (durable / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(runtime_tmpfs)

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=durable,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )
            == 0
        )

    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert f"TICKET_BOARD_PANE_SESSION_DIR={durable}" in runner.calls[1][-1]
    assert f"TICKET_BOARD_PANE_STATE_DIR={tmp_path / 'pane-state'}" in runner.calls[1][-1]
    assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={durable}" in runner.calls[1][-1]
    assert f"PGU_TICKET_BOARD_PANE_STATE_DIR={tmp_path / 'pane-state'}" in runner.calls[1][-1]
    assert f"codex resume {session_id}" in runner.calls[1][-1]


def test_start_runs_research_detached_before_opening_visible_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert runner.calls[:3] == [
            git_launcher_checkout_check_args(ROOT),
            git_launcher_head_args(ROOT),
            git_launcher_ls_remote_ref_args(config, ROOT),
        ]
        assert runner.calls[3] == git_fetch_worktree_ref_args(config)
        assert runner.calls[4] == git_shared_checkout_check_args(config)
        assert runner.calls[5] == team_launcher.git_shared_checkout_status_porcelain_args(config)
        assert runner.calls[6] == team_launcher.git_clean_shared_checkout_dry_run_args(config)
        assert runner.calls[7] == git_checkout_shared_ref_args(config)
        assert runner.calls[8] == git_clean_shared_checkout_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        research_has_session_index = runner.calls.index(["tmux", "has-session", "-t", "pgu-research"])
        research_new_session = runner.calls[research_has_session_index + 1]
        assert research_new_session[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"]
        assert research_new_session[5:7] == ["-c", str(tmp_path / "repo")]
        assert "TICKET_BOARD_PANE_TARGET=pgu-research:0.0" in research_new_session[-1]
        assert "PGU_PANE_TARGET=pgu-research:0.0" in research_new_session[-1]
        assert "--model claude-opus-5" in research_new_session[-1]
        state = _read_pane_state(tmp_path / "pane-state", "pgu-research:0.0")
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {str(tmp_path / "repo")}
        assert runner.calls[-1] == konsole_launch_args(layout_output)
        assert ["tmux", "attach", "-t", "pgu-research"] not in runner.calls


def test_plain_shared_checkout_git_runs_as_owner_when_launcher_user_differs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-git.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = SudoAwareFakeRunner(current_commands={"pgu-research:0.0": "claude"})
        layout_output = tmp_path / "layout.json"
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "root"
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=layout_output,
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

        owner_git_calls = [
            call[3:]
            for call in runner.calls
            if call[:3] == ["sudo", "-u", "agent"] and len(call) > 3 and call[3] == "git"
        ]
        expected_refresh_calls = {
            tuple(git_fetch_worktree_ref_args(config)),
            tuple(git_shared_checkout_check_args(config)),
            tuple(team_launcher.git_shared_checkout_status_porcelain_args(config)),
            tuple(team_launcher.git_clean_shared_checkout_dry_run_args(config)),
            tuple(git_checkout_shared_ref_args(config)),
            tuple(git_clean_shared_checkout_args(config)),
        }
        assert {tuple(call) for call in owner_git_calls} >= expected_refresh_calls
        direct_refresh_calls = [
            call
            for call in runner.calls
            if tuple(call) in expected_refresh_calls
        ]
        assert direct_refresh_calls == []
        assert ["sudo", "-u", "agent", "-H", *tmux_has_session_args(next(role for role in config.roles if role.role == "research"))] in runner.calls
        assert any(
            call[:9] == ["sudo", "-u", "agent", "-H", "tmux", "new-session", "-d", "-s", "pgu-research"]
            for call in runner.calls
        )


def test_plain_shared_checkout_git_stays_direct_when_already_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-git.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "agent"
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

        assert git_fetch_worktree_ref_args(config) in runner.calls
        assert not any(call[:3] == ["sudo", "-u", "agent"] and len(call) > 3 and call[3] == "git" for call in runner.calls)


def test_control_repository_launch_uses_role_worktrees_and_preserves_repository() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, root: Path) -> None:
            super().__init__()
            self.root = root.resolve(strict=False)

        def _is_scratch_path(self, path: Path) -> bool:
            resolved = path.resolve(strict=False)
            return resolved == self.root or resolved.is_relative_to(self.root)

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            run_kwargs = dict(kwargs)
            run_kwargs.setdefault("text", True)
            if "stdout" not in run_kwargs:
                run_kwargs["stdout"] = subprocess.PIPE
            if "stderr" not in run_kwargs:
                run_kwargs["stderr"] = subprocess.PIPE
            if args[:1] == ["mkdir"] and len(args) >= 3 and self._is_scratch_path(Path(args[2])):
                return subprocess.run(args, **run_kwargs)
            if args[:1] == ["git"]:
                git_path: Path | None = None
                if len(args) >= 3 and args[1] == "-C":
                    git_path = Path(args[2])
                elif len(args) >= 4 and args[1:3] == ["clone", "--bare"]:
                    git_path = Path(args[3])
                else:
                    for index, arg in enumerate(args):
                        if arg == "--git-dir" and index + 1 < len(args):
                            git_path = Path(args[index + 1])
                            break
                if git_path is not None and self._is_scratch_path(git_path):
                    return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _run_git(["git", "add", ".gitignore"], cwd=repo)
        _run_git(["git", "commit", "-m", "ignore bytecode"], cwd=repo)
        (repo / "tracked.txt").write_text("user's local edit\n", encoding="utf-8")
        (repo / "notes.txt").write_text("user notes\n", encoding="utf-8")
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "porter.json"
        worktree_base = tmp_path / "worktrees"
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "porter" / "control.git"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "worktree_base": str(worktree_base),
                    "roles": [
                        {"role": "ops", "slot": 0, "target": "porter-ops:0.0", "cli": ["codex"]},
                        {"role": "app", "slot": 1, "target": "porter-app:0.0", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("porter", config_path)
            runner = ProjectGitRunner(tmp_path)

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "launch-layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )

            assert (repo / "tracked.txt").read_text(encoding="utf-8") == "user's local edit\n"
            assert (repo / "notes.txt").read_text(encoding="utf-8") == "user notes\n"
            assert config.roles[0].workdir == str(worktree_base / "ops")
            assert config.roles[1].workdir == str(worktree_base / "app")
            assert _run_git(["git", "-C", worktree_base / "ops", "rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
            assert _run_git(["git", "-C", worktree_base / "app", "rev-parse", "--is-inside-work-tree"]).stdout.strip() == "true"
            assert _run_git(["git", "-C", worktree_base / "ops", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() == "HEAD"
            assert _run_git(["git", "-C", control_repo, "config", "--get-all", "remote.origin.fetch"]).stdout.strip() == (
                "+refs/heads/*:refs/remotes/origin/*"
            )
            assert git_checkout_shared_ref_args(config) not in runner.calls
            assert git_clean_shared_checkout_args(config) not in runner.calls
            assert any(call == team_launcher.git_control_worktree_add_args(config, config.roles[0]) for call in runner.calls)
            assert any(call == team_launcher.git_control_worktree_add_args(config, config.roles[1]) for call in runner.calls)

            ops_worktree = worktree_base / "ops"
            app_worktree = worktree_base / "app"
            (ops_worktree / "tracked.txt").write_text("agent local edit\n", encoding="utf-8")
            (ops_worktree / "agent_notes.txt").write_text("agent notes\n", encoding="utf-8")
            (app_worktree / "__pycache__").mkdir()
            (app_worktree / "__pycache__" / "x.pyc").write_bytes(b"pyc\n")
            stderr = StringIO()
            with redirect_stderr(stderr):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=ProjectGitRunner(tmp_path),
                        layout_output=tmp_path / "launch-layout-2.json",
                        pane_state_dir=tmp_path / "pane-state-2",
                    )
                    == 0
                )
            warning = stderr.getvalue()
            assert "will reset managed role worktree" in warning
            assert "for ops to origin/main" in warning
            assert "tracked.txt" in warning
            assert "agent_notes.txt" in warning
            assert "__pycache__" not in warning
            assert (ops_worktree / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
            assert not (ops_worktree / "agent_notes.txt").exists()
            assert not (app_worktree / "__pycache__").exists()
        finally:
            team_launcher._control_repository_owner_home = original_home


def test_control_repository_launch_runs_bootstrap_as_configured_owner() -> None:
    class OwnerRecordingRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["sudo", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-owner.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "otto.json"
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        worktree_base = tmp_path / "worktrees"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": "otto-agent",
                    "worktree_base": str(worktree_base),
                    "roles": [
                        {"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]},
                        {"role": "app", "slot": 1, "target": "otto-app:0.0", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        runner = OwnerRecordingRunner()
        layout_output = tmp_path / "launch-layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        owner_calls = [call for call in runner.calls if call[:3] == ["sudo", "-u", "otto-agent"]]
        assert ["sudo", "-u", "otto-agent", *team_launcher.mkdir_p_args(control_repo.parent)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_clone_control_repository_args(config)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_control_fetch_refspec_args(config)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_fetch_control_ref_args(config)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.mkdir_p_args(worktree_base)] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_control_worktree_add_args(config, config.roles[0])] in owner_calls
        assert ["sudo", "-u", "otto-agent", *team_launcher.git_control_worktree_add_args(config, config.roles[1])] in owner_calls
        assert team_launcher.git_clone_control_repository_args(config) not in runner.calls
        assert team_launcher.git_control_worktree_add_args(config, config.roles[0]) not in runner.calls
        assert not any(call[:2] == ["chown", "-R"] for call in runner.calls)
        assert runner.calls[-1] == konsole_launch_args(layout_output)


def test_control_repository_bootstrap_failure_aborts_without_opening_window() -> None:
    class FailingControlRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["sudo", "-u", "otto-agent"] and args[3:6] == ["git", "clone", "--bare"]:
                return subprocess.CompletedProcess(
                    args,
                    128,
                    stderr="fatal: detected dubious ownership in repository at '/home/otto-agent/Projects/otto/.git'\n",
                )
            if args[:3] == ["sudo", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-fail.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "otto.json"
        control_repo = tmp_path / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": "otto-agent",
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home
        runner = FailingControlRunner()
        layout_output = tmp_path / "launch-layout.json"
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )

        assert result == 1
        assert "failed to prepare control repository for otto" in stderr.getvalue()
        assert "detected dubious ownership" in stderr.getvalue()
        assert not layout_output.exists()
        assert not any(call == konsole_launch_args(layout_output) for call in runner.calls)


def test_control_repository_repairs_owner_mismatch_before_clone() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-repair.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        current_info = team_launcher.pwd.getpwnam(current_user)
        repo = tmp_path / "repo"
        repo.mkdir()
        state_dir = tmp_path / ".local" / "state"
        switchyard_dir = state_dir / "switchyard"
        projects_dir = switchyard_dir / "projects"
        projects_dir.mkdir(parents=True)
        control_repo = projects_dir / "otto" / "control.git"
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        root_owned = {state_dir, switchyard_dir, projects_dir}

        class RepairRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                for path in tuple(root_owned):
                    if args == team_launcher.chown_control_repository_args(config, path, recursive=False):
                        self.calls.append(args)
                        root_owned.remove(path)
                        return subprocess.CompletedProcess(args, 0)
                if args[:2] == ["chown", "-R"] or args[:1] == ["chown"]:
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 1, stderr=f"unexpected chown: {shlex.join(args)}\n")
                return super().__call__(args, **kwargs)

        original_home = team_launcher._control_repository_owner_home
        original_owner_ids = team_launcher._path_owner_ids
        original_label = team_launcher._path_owner_label
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
            team_launcher._path_owner_ids = (
                lambda path: (0, 0)
                if path in root_owned
                else (current_info.pw_uid, current_info.pw_gid)
                if path.exists()
                else None
            )
            team_launcher._path_owner_label = lambda _path: "root:root"
            runner = RepairRunner()

            result = team_launcher.ensure_control_repository(config, runner=runner)
        finally:
            team_launcher._control_repository_owner_home = original_home
            team_launcher._path_owner_ids = original_owner_ids
            team_launcher._path_owner_label = original_label

        assert result.ok
        assert root_owned == set()
        state_chown_index = runner.calls.index(team_launcher.chown_control_repository_args(config, state_dir, recursive=False))
        switchyard_chown_index = runner.calls.index(team_launcher.chown_control_repository_args(config, switchyard_dir, recursive=False))
        projects_chown_index = runner.calls.index(team_launcher.chown_control_repository_args(config, projects_dir, recursive=False))
        mkdir_index = runner.calls.index(team_launcher.mkdir_p_args(control_repo.parent))
        clone_index = runner.calls.index(team_launcher.git_clone_control_repository_args(config))
        assert state_chown_index < switchyard_chown_index < projects_chown_index < mkdir_index < clone_index


def test_control_repository_chown_failure_names_actual_and_expected_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-repair-fail.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        current_info = team_launcher.pwd.getpwnam(current_user)
        repo = tmp_path / "repo"
        repo.mkdir()
        projects_dir = tmp_path / ".local" / "state" / "switchyard" / "projects"
        projects_dir.mkdir(parents=True)
        control_repo = projects_dir / "otto" / "control.git"
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        class RefusingRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args == team_launcher.chown_control_repository_args(config, projects_dir, recursive=False):
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 1, stderr="chown: Operation not permitted\n")
                return super().__call__(args, **kwargs)

        original_home = team_launcher._control_repository_owner_home
        original_owner_ids = team_launcher._path_owner_ids
        original_label = team_launcher._path_owner_label
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
            team_launcher._path_owner_ids = (
                lambda path: (0, 0)
                if path == projects_dir
                else (current_info.pw_uid, current_info.pw_gid)
                if path.exists()
                else None
            )
            team_launcher._path_owner_label = lambda _path: "root:root"
            runner = RefusingRunner()

            result = team_launcher.ensure_control_repository(config, runner=runner)
        finally:
            team_launcher._control_repository_owner_home = original_home
            team_launcher._path_owner_ids = original_owner_ids
            team_launcher._path_owner_label = original_label

        assert not result.ok
        reason = result.failed_roles["ops"]
        assert str(control_repo) in reason
        assert "root:root" in reason
        assert f"expected {current_user}:{current_user}" in reason
        assert "Operation not permitted" in reason
        assert team_launcher.git_clone_control_repository_args(config) not in runner.calls


def test_control_repository_repair_requires_existing_target_user() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-repair-user.") as tmp:
        tmp_path = Path(tmp)
        missing_user = "pgu-nosuch-control-user-680"
        repo = tmp_path / "repo"
        repo.mkdir()
        projects_dir = tmp_path / ".local" / "state" / "switchyard" / "projects"
        projects_dir.mkdir(parents=True)
        control_repo = projects_dir / "otto" / "control.git"
        control_repo.parent.mkdir()
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(control_repo),
                    "run_as_user": missing_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            config = load_project_config("otto", config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home

        runner = FakeRunner()
        result = team_launcher.repair_control_repository_ownership(config, runner=runner)

        assert not result.ok
        reason = result.failed_roles["ops"]
        assert "refusing to repair control repository ownership" in reason
        assert f"target user {missing_user!r} does not exist" in reason
        assert not any(call[:1] == ["chown"] or call[:2] == ["sudo", "chown"] for call in runner.calls)


def test_control_repository_repair_refuses_unmanaged_or_slugless_paths() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-boundary.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        repo = tmp_path / "repo"
        repo.mkdir()
        managed_root = tmp_path / ".local" / "state" / "switchyard" / "projects"
        managed_root.mkdir(parents=True)
        configs: list[Path] = [
            managed_root / "control.git",
            tmp_path / "control.git",
            managed_root / "otto" / ".." / ".." / ".." / "control.git",
        ]
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            for index, control_repo in enumerate(configs):
                config_path = tmp_path / f"otto-{index}.json"
                config_path.write_text(
                    json.dumps(
                        {
                            "project": "otto",
                            "layout": str(tmp_path / "layout.json"),
                            "repository": str(repo),
                            "control_repository": str(control_repo),
                            "run_as_user": current_user,
                            "worktree_base": str(tmp_path / "worktrees"),
                            "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                try:
                    load_project_config("otto", config_path)
                    raise AssertionError("expected SystemExit")
                except SystemExit as exc:
                    message = str(exc)

                assert "control_repository" in message
                assert "not a switchyard-managed control directory" in message
        finally:
            team_launcher._control_repository_owner_home = original_home


def test_control_repository_load_rejects_unmanaged_path_with_expected_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-load-boundary.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(tmp_path / "control.git"),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            try:
                load_project_config("otto", config_path)
                raise AssertionError("expected SystemExit")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher._control_repository_owner_home = original_home

        assert "control_repository" in message
        assert "not a switchyard-managed control directory" in message
        assert str(tmp_path / ".local" / "state" / "switchyard" / "projects") in message


def test_control_repository_load_accepts_generated_prefix_and_pgu_without_control_repo() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-control-load-valid.") as tmp:
        tmp_path = Path(tmp)
        current_user = team_launcher.current_user_name()
        repo = tmp_path / "repo"
        repo.mkdir()
        managed_control = tmp_path / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "control_repository": str(managed_control),
                    "run_as_user": current_user,
                    "worktree_base": str(tmp_path / "worktrees"),
                    "roles": [{"role": "ops", "slot": 0, "target": "otto-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        pgu_config_path = tmp_path / "pgu.json"
        pgu_config_path.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(tmp_path / "layout.json"),
                    "repository": str(repo),
                    "run_as_user": current_user,
                    "roles": [{"role": "ops", "slot": 0, "target": "pgu-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        original_home = team_launcher._control_repository_owner_home
        try:
            team_launcher._control_repository_owner_home = lambda _config: tmp_path
            otto_config = load_project_config("otto", config_path)
            pgu_config = load_project_config("pgu", pgu_config_path)
        finally:
            team_launcher._control_repository_owner_home = original_home

        assert otto_config.control_repository == managed_control
        assert pgu_config.control_repository is None


def test_launch_without_control_repository_does_not_owner_wrap_pgu_plan() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-no-control.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout_path = tmp_path / "layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "pgu.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "run_as_user": "agent",
                    "roles": [{"role": "ops", "slot": 0, "target": "pgu-ops:0.0", "cli": ["codex"]}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("pgu", config_path)
        runner = FakeRunner()
        layout_output = tmp_path / "launch-layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert not any(call[:3] == ["sudo", "-u", "agent"] for call in runner.calls)
        assert not any(call[:2] == ["chown", "-R"] for call in runner.calls)
        assert runner.calls[-1] == konsole_launch_args(layout_output)


def test_viewer_layout_starts_role_sessions_and_additive_viewer() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        config = load_project_config("porter", config_path)
        runner = FakeRunner()
        messages: list[str] = []

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout.json",
                pane_state_dir=tmp_path / "pane-state",
                layout_mode="viewer",
                layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                print_func=messages.append,
            )
            == 0
        )

    role_sessions = ["porter-designer", "porter-director", "porter-audit", "porter-ops", "porter-app", "porter-main"]
    role_new_sessions = [
        call for call in runner.calls
        if call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] in role_sessions
    ]
    assert [call[call.index("-s") + 1] for call in role_new_sessions] == role_sessions
    viewer_new_sessions = [
        call for call in runner.calls
        if call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] == "viewer"
    ]
    assert len(viewer_new_sessions) == 1
    assert "env TMUX= tmux attach -t porter-designer" in viewer_new_sessions[0][-1]
    viewer_splits = [call for call in runner.calls if call[:2] == ["tmux", "split-window"]]
    assert len(viewer_splits) == 5
    for session in role_sessions:
        assert ["tmux", "set-option", "-t", session, "status", "off"] in runner.calls
        assert ["tmux", "set-window-option", "-t", f"{session}:0", "pane-border-status", "off"] in runner.calls
    assert ["tmux", "set-option", "-t", "viewer", "status", "on"] in runner.calls
    assert ["tmux", "set-option", "-t", "viewer", "prefix", "C-a"] in runner.calls
    assert ["tmux", "set-window-option", "-t", "viewer:0", "pane-border-status", "top"] in runner.calls
    assert ["tmux", "set-window-option", "-t", "viewer:0", "pane-border-format", " #{@role} "] in runner.calls
    assert [call for call in runner.calls if call[:4] == ["tmux", "set-option", "-p", "-t"]] == [
        ["tmux", "set-option", "-p", "-t", f"viewer.{index}", "@role", role]
        for index, role in enumerate(["designer", "director", "audit", "ops", "app", "main"])
    ]
    assert not any(call[:2] == ["env", "QT_QPA_PLATFORM=wayland"] for call in runner.calls)
    assert messages == []


def test_viewer_visible_start_verifies_cli_before_reporting_success() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-visible.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        config = load_project_config("porter", config_path)
        role = next(role for role in config.roles if role.role == "app")
        runner = FakeRunner()
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                team_launcher.ensure_visible_role_session_for_viewer(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

        state = _read_pane_state(tmp_path / "pane-state", role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "porter-app"]
        assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "porter-app:0.0", "#{pane_pid}"]
        assert runner.calls[3] == ["tmux", "display-message", "-p", "-t", "porter-app:0.0", "#{pane_current_command}"]
        assert "did not leave a live" not in stderr.getvalue()


def test_viewer_visible_start_fails_when_cli_exits_immediately() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "app")

    class VanishedViewerRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.created = False

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:2] == ["tmux", "new-session"]:
                self.created = True
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 0 if self.created else 1, stdout="fish\n" if self.created else "")
            return subprocess.CompletedProcess(args, 0)

    runner = VanishedViewerRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        pane_state_dir = tmp_path / "pane-state"
        with redirect_stderr(stderr):
            assert (
                team_launcher.ensure_visible_role_session_for_viewer(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=pane_state_dir,
                    runner=runner,
                )
                == 1
            )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-app"] for call in runner.calls)
    assert ["tmux", "has-session", "-t", "pgu-app"] in runner.calls
    assert "role app did not leave a live pgu-app session" in stderr.getvalue()


def test_viewer_pane_death_does_not_change_role_targets_in_real_tmux() -> None:
    if shutil.which("tmux") is None:
        return
    roles = ["designer", "director", "audit", "ops", "app", "main"]
    suffix = f"pgu660_{os.getpid()}"
    server = f"{suffix}-server"
    runner = _isolated_tmux_runner(server)
    viewer_session = f"{suffix}-viewer"
    sessions = {role: f"{suffix}-{role}" for role in roles}
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-real.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path, project=suffix)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        for role_config in raw_config["roles"]:
            role = role_config["role"]
            role_config["tmux_session"] = sessions[role]
            role_config["target"] = f"{sessions[role]}:0.0"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config(suffix, config_path)
        try:
            for session in sessions.values():
                _run_isolated_tmux(server, ["new-session", "-d", "-s", session, "-c", str(tmp_path), "/bin/sh"], check=True)
            assert team_launcher.launch_tmux_viewer_session(
                team_launcher.visible_roles_for_viewer(config),
                viewer_session=viewer_session,
                runner=runner,
            ) == 0
            panes = _run_isolated_tmux(
                server,
                ["list-panes", "-t", f"{viewer_session}:0", "-F", "#{pane_index}:#{@role}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip().splitlines()
            assert panes == [f"{index}:{role}" for index, role in enumerate(roles)]

            _run_isolated_tmux(server, ["kill-pane", "-t", f"{viewer_session}:0.1"], check=True)
            after = _run_isolated_tmux(
                server,
                ["list-panes", "-t", f"{viewer_session}:0", "-F", "#{pane_index}:#{@role}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip().splitlines()
            assert len(after) == 5

            for role, session in sessions.items():
                marker = tmp_path / f"{role}.txt"
                _run_isolated_tmux(server, ["has-session", "-t", session], check=True)
                _run_isolated_tmux(
                    server,
                    ["send-keys", "-t", f"{session}:0.0", f"printf {role!r} > {shlex.quote(str(marker))}", "Enter"],
                    check=True,
                )
                for _ in range(50):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                assert marker.read_text(encoding="utf-8") == role
        finally:
            _cleanup_isolated_tmux_sessions(server, [viewer_session, *sessions.values()])


def test_detached_research_start_fails_visible_when_session_disappears() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")

    class VanishingDetachedRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:2] == ["tmux", "new-session"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(args, 0)

    runner = VanishingDetachedRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        pane_state_dir = tmp_path / "pane-state"
        with redirect_stderr(stderr):
            assert (
                run_detached_role(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=pane_state_dir,
                    runner=runner,
                )
                == 1
            )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"] for call in runner.calls)
    assert "role research did not leave a live pgu-research session" in stderr.getvalue()


def test_detached_research_resume_falls_back_when_failed_session_is_already_gone() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    stderr = StringIO()

    class ResumeGoneThenFreshRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.fresh_live = False

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 0 if self.fresh_live else 1)
            if args[:2] == ["tmux", "new-session"]:
                self.fresh_live = "--resume " not in str(args[-1])
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                if args[-1] == "#{pane_pid}":
                    return subprocess.CompletedProcess(args, 1, stdout="")
                if self.fresh_live:
                    return subprocess.CompletedProcess(args, 0, stdout="claude\n")
                return subprocess.CompletedProcess(args, 1, stdout="")
            if args[:2] == ["tmux", "kill-session"]:
                raise AssertionError("missing failed resume session should not be killed before fallback")
            return subprocess.CompletedProcess(args, 0)

    original_stability = team_launcher.DETACHED_SESSION_STABILITY_SECONDS
    try:
        team_launcher.DETACHED_SESSION_STABILITY_SECONDS = 0.0
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            tmp_path = Path(tmp)
            session_dir = tmp_path / "sessions"
            session_dir.mkdir()
            session_id = "12345678-1234-5678-9abc-def012345678"
            transcript = tmp_path / "claude-session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps(
                    {"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}}
                )
                + "\n",
                encoding="utf-8",
            )
            pane_state_dir = tmp_path / "pane-state"
            runner = ResumeGoneThenFreshRunner()
            with redirect_stderr(stderr):
                assert (
                    run_detached_role(
                        role,
                        mode="start",
                        session_dir=session_dir,
                        pane_state_dir=pane_state_dir,
                        runner=runner,
                    )
                    == 0
                )
            state = _read_pane_state(pane_state_dir, role.target)
            assert state["state"] == "idle"
            assert state["source"] == "team_launcher.start.fallback"
    finally:
        team_launcher.DETACHED_SESSION_STABILITY_SECONDS = original_stability

    assert f"team-launcher: resume failed for research using session {session_id}; falling back to fresh session" in stderr.getvalue()
    assert "team-launcher: started fresh session for research after resume fallback" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"]]
    assert len(new_sessions) == 2
    assert "--resume" in new_sessions[0][-1]
    assert "--resume" not in new_sessions[1][-1]


def test_start_force_refreshes_dirty_shared_checkout_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        config = load_project_config("pgu", config_path)
        shared_checkout = Path(config.roles[0].workdir)
        assert shared_checkout == repo

        assert ensure_project_worktrees(config, refresh=True).ok

        (shared_checkout / "tracked.txt").write_text("dirty tracked edit\n", encoding="utf-8")
        (shared_checkout / "untracked.tmp").write_text("junk\n", encoding="utf-8")

        assert ensure_project_worktrees(config, refresh=True).ok

        assert (shared_checkout / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
        assert not (shared_checkout / "untracked.tmp").exists()
        status = _run_git(["git", "status", "--porcelain"], cwd=shared_checkout).stdout
        assert status == ""


def test_launch_warns_before_resetting_dirty_shared_checkout_with_real_git() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, repo: Path) -> None:
            super().__init__()
            self.repo = repo

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.repo:
                run_kwargs = dict(kwargs)
                run_kwargs.setdefault("text", True)
                if "stdout" not in run_kwargs:
                    run_kwargs["stdout"] = subprocess.PIPE
                if "stderr" not in run_kwargs:
                    run_kwargs["stderr"] = subprocess.PIPE
                return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-dirty-project.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        layout_path = tmp_path / "launch-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["layout"] = str(layout_path)
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("pgu", config_path)
        runner = ProjectGitRunner(repo)
        stderr = StringIO()

        (repo / "tracked.txt").write_text("uncommitted tracked edit\n", encoding="utf-8")
        (repo / "my_notes.txt").write_text("notes that will be removed\n", encoding="utf-8")

        with redirect_stderr(stderr):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-out.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )

        warning = stderr.getvalue()
        assert "will reset managed project checkout" in warning
        assert "tracked changes will be discarded" in warning
        assert "tracked.txt" in warning
        assert "untracked files will be removed" in warning
        assert "my_notes.txt" in warning
        assert (repo / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
        assert not (repo / "my_notes.txt").exists()


def test_clean_launch_does_not_warn_about_shared_checkout_refresh() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, repo: Path) -> None:
            super().__init__()
            self.repo = repo

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.repo:
                run_kwargs = dict(kwargs)
                run_kwargs.setdefault("text", True)
                if "stdout" not in run_kwargs:
                    run_kwargs["stdout"] = subprocess.PIPE
                if "stderr" not in run_kwargs:
                    run_kwargs["stderr"] = subprocess.PIPE
                return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-clean-project.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _run_git(["git", "add", ".gitignore"], cwd=repo)
        _run_git(["git", "commit", "-m", "ignore launcher bytecode"], cwd=repo)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        layout_path = tmp_path / "launch-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["layout"] = str(layout_path)
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("pgu", config_path)
        (repo / "scripts" / "ticket_board" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "ticket_board" / "__pycache__" / "team_launcher.cpython-313.pyc").write_bytes(b"pyc\n")
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=ProjectGitRunner(repo),
                    layout_output=tmp_path / "layout-out.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )

        assert "will reset managed project checkout" not in stderr.getvalue()


def test_default_layout_does_not_create_untracked_switchyard_in_shared_checkout() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, repo: Path) -> None:
            super().__init__()
            self.repo = repo

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.repo:
                run_kwargs = dict(kwargs)
                run_kwargs.setdefault("text", True)
                if "stdout" not in run_kwargs:
                    run_kwargs["stdout"] = subprocess.PIPE
                if "stderr" not in run_kwargs:
                    run_kwargs["stderr"] = subprocess.PIPE
                return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    original_getpwnam = team_launcher.pwd.getpwnam
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-clean-project.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"], run_as_user="agent")
        layout_path = tmp_path / "launch-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["layout"] = str(layout_path)
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("pgu", config_path)
        runner = ProjectGitRunner(repo)
        stderr = StringIO()

        class FakeUserInfo:
            pw_dir = str(owner_home)
            pw_uid = os.getuid()

        try:
            def fake_getpwnam(user_name: str) -> object:
                return FakeUserInfo() if user_name == "agent" else original_getpwnam(user_name)

            team_launcher.pwd.getpwnam = fake_getpwnam
            expected_layout = owner_home / ".local" / "state" / "switchyard" / "projects" / "pgu" / "pgu-team-layout.json"

            for _ in range(2):
                with redirect_stderr(stderr):
                    assert (
                        launch_project(
                            config,
                            config_path=config_path,
                            mode="start",
                            script_path=ROOT / "scripts" / "team-launcher",
                            runner=runner,
                            pane_state_dir=tmp_path / "pane-state",
                        )
                        == 0
                    )
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam

        status = _run_git(["git", "status", "--porcelain"], cwd=repo).stdout
        assert expected_layout.is_file()
        assert not (repo / ".switchyard").exists()

    assert "will reset managed project checkout" not in stderr.getvalue()
    assert status == ""


def test_root_materialized_owner_state_layout_is_owner_writable_on_next_launch() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-owner-transfer.") as tmp:
        tmp_path = Path(tmp)
        layout_template = tmp_path / "layout-template.json"
        layout_template.write_text(
            json.dumps({"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}) + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout_template),
                    "run_as_user": "porter-agent",
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {
                            "role": "ops",
                            "slot": 0,
                            "target": "porter-ops:0.0",
                            "tmux_session": "porter-ops",
                            "cli": ["codex"],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        layout_output = (
            tmp_path
            / "home"
            / "porter-agent"
            / ".local"
            / "state"
            / "switchyard"
            / "projects"
            / "porter"
            / "porter-team-layout.json"
        )
        synthetic_owner: dict[Path, str] = {}
        active_user = "root"
        expected_chown_args = ["chown", "-R", "porter-agent:porter-agent", str(layout_output.parent)]

        class OwnershipRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:1] == ["chown"]:
                    self.calls.append(args)
                    owner = args[-2] if len(args) >= 3 else ""
                    target = Path(args[-1]) if len(args) >= 3 else Path("/nonexistent")
                    recursive = "-R" in args[1:-2]
                    if owner == f"{config.run_as_user}:{config.run_as_user}":
                        synthetic_owner[target] = config.run_as_user
                        if recursive:
                            for path in list(synthetic_owner):
                                if path == target or path.is_relative_to(target):
                                    synthetic_owner[path] = config.run_as_user
                    return subprocess.CompletedProcess(args, 0)
                return super().__call__(args, **kwargs)

        def fake_materialize_layout(
            _config: team_launcher.ProjectConfig,
            *,
            output_path: Path,
            **_kwargs: object,
        ) -> Path:
            writer = team_launcher.current_user_name()
            existing_owner = synthetic_owner.get(output_path)
            if existing_owner is not None and existing_owner != writer:
                raise PermissionError(f"{writer} cannot overwrite layout owned by {existing_owner}: {output_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("{}\n", encoding="utf-8")
            synthetic_owner[output_path] = writer
            return output_path

        runner = OwnershipRunner()
        original_current_user_name = team_launcher.current_user_name
        original_materialize_layout = team_launcher.materialize_layout
        try:
            team_launcher.current_user_name = lambda: active_user
            team_launcher.materialize_layout = fake_materialize_layout

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=layout_output,
                    assign_layout_owner=True,
                    pane_state_dir=tmp_path / "pane-state",
                    layout_mode="viewer",
                    layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                )
                == 0
            )
            assert synthetic_owner[layout_output] == "porter-agent"

            active_user = "porter-agent"
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=layout_output,
                    assign_layout_owner=True,
                    pane_state_dir=tmp_path / "pane-state",
                    layout_mode="viewer",
                    layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                )
                == 0
            )
        finally:
            team_launcher.current_user_name = original_current_user_name
            team_launcher.materialize_layout = original_materialize_layout

    assert runner.calls.count(expected_chown_args) == 1


def test_bad_shared_checkout_blocks_all_roles_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "not-a-git-checkout"
        repo.mkdir()
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops", "app"])
        config = load_project_config("pgu", config_path)

        result = ensure_project_worktrees(config, refresh=True)

        assert set(result.failed_roles) == {"ops", "app"}


def test_reload_fetches_without_refreshing_shared_checkout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="reload",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert runner.calls[:3] == [
            git_launcher_checkout_check_args(ROOT),
            git_launcher_head_args(ROOT),
            git_launcher_ls_remote_ref_args(config, ROOT),
        ]
        assert runner.calls[3] == git_fetch_worktree_ref_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        assert git_checkout_shared_ref_args(config) not in runner.calls
        assert git_clean_shared_checkout_args(config) not in runner.calls


def test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["live_commands"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        transcript = tmp_path / "claude-research-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {
                    "session_id": "live-research-session",
                    "cwd": "/home/agent/Projects/pgu",
                    "transcript_path": str(transcript),
                },
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1200})
        original_snapshot = _patch_process_snapshot(
            {1200: 6388, 1201: 1200},
            {1200: [1201]},
            {1200: {"fish", "claude"}, 1201: {"claude"}},
            {
                1200: ["fish", "-c", "claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
                1201: ["claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
            },
        )
        try:
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="reload",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                )
                == 0
            )
        finally:
            team_launcher._process_snapshot = original_snapshot

        synced = json.loads(config_path.read_text(encoding="utf-8"))
        synced_research = next(role for role in synced["roles"] if role["role"] == "research")
        assert synced_research["cli"] == ["claude"]
        assert synced_research["live_commands"] == ["claude"]
        assert synced_research["model"] == "claude-sonnet-5"
        assert synced_research["detached"] is True
        assert synced_research["target"] == "pgu-research:0.0"
        assert synced_research["effort"] == "high"
        assert synced_research["yolo"] is True

        research_new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"])
        assert "claude --resume live-research-session --model claude-sonnet-5" in research_new_session[-1]
        assert "codex resume" not in research_new_session[-1]


def test_reload_sync_leaves_config_unchanged_when_live_matches() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1300})
        before = config_path.read_text(encoding="utf-8")
        original_snapshot = _patch_process_snapshot(
            {1300: 6388, 1301: 1300},
            {1300: [1301]},
            {1300: {"fish", "claude"}, 1301: {"claude"}},
            {
                1300: ["fish", "-c", "claude", "--model", "claude-opus-5", "--dangerously-skip-permissions"],
                1301: ["claude", "--model", "claude-opus-5", "--dangerously-skip-permissions"],
            },
        )
        try:
            synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
        finally:
            team_launcher._process_snapshot = original_snapshot

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before


def test_reload_sync_leaves_config_unchanged_when_role_not_running() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["live_commands"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner()
        before = config_path.read_text(encoding="utf-8")

        synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before


def test_reload_sync_leaves_config_unchanged_when_model_is_unparseable() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1400})
        before = config_path.read_text(encoding="utf-8")
        original_snapshot = _patch_process_snapshot(
            {1400: 6388, 1401: 1400},
            {1400: [1401]},
            {1400: {"fish", "claude"}, 1401: {"claude"}},
            {
                1400: ["fish", "-c", "claude", "--dangerously-skip-permissions"],
                1401: ["claude", "--dangerously-skip-permissions"],
            },
        )
        try:
            synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
        finally:
            team_launcher._process_snapshot = original_snapshot

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before


def test_dry_run_reports_shared_checkout_without_syncing_it() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    runner = FakeRunner()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        layout_output = Path(tmp) / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                dry_run=True,
                layout_output=layout_output,
            )
            == 0
        )

        assert runner.calls == []
        plan = json.loads(layout_output.read_text(encoding="utf-8"))
        assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(plan)} == {
            str(_pgu_project_root())
        }
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert len(commands) == 6


def test_start_does_not_sync_live_cli_or_model() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["live_commands"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(current_commands={"pgu-research:0.0": "codex"})
        before = config_path.read_text(encoding="utf-8")

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout.json",
                pane_state_dir=tmp_path / "pane-state",
            )
            == 0
        )

        assert config_path.read_text(encoding="utf-8") == before
        research_new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"])
        # start resumes the recorded id; cli/model stay configured (not synced from live)
        assert "live-research-session" in research_new_session[-1]
        assert "--model gpt-5.5" in research_new_session[-1]
        assert "claude-sonnet-5" not in research_new_session[-1]


def test_detached_research_attach_checks_session_without_attaching() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    runner = FakeRunner(existing_sessions={"pgu-research"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp) / "sessions"
        assert (
            run_detached_role(
                role,
                mode="attach",
                session_dir=session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                runner=runner,
            )
            == 0
        )

    assert runner.calls == [["tmux", "has-session", "-t", "pgu-research"]]


def test_stop_project_kills_only_configured_sessions_as_owner_and_is_idempotent() -> None:
    class OwnerTmuxRunner:
        def __init__(self, existing_sessions: set[str]) -> None:
            self.existing_sessions = set(existing_sessions)
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:4] != ["sudo", "-u", "otto-agent", "-H"]:
                return subprocess.CompletedProcess(args, 0)
            inner = args[4:]
            if inner[:2] == ["tmux", "has-session"]:
                session = inner[-1]
                return subprocess.CompletedProcess(args, 0 if session in self.existing_sessions else 1)
            if inner[:2] == ["tmux", "kill-session"]:
                session = inner[-1]
                self.existing_sessions.discard(session)
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 0)

    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "root"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-stop.") as tmp:
            tmp_path = Path(tmp)
            layout = tmp_path / "layout.json"
            layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
            for role_name in ("designer", "director", "audit", "ops", "app", "main"):
                (tmp_path / "worktrees" / role_name).mkdir(parents=True)
            config_path = tmp_path / "otto.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "otto",
                        "layout": str(layout),
                        "run_as_user": "otto-agent",
                        "roles": [
                            {"role": "designer", "slot": 0, "tmux_session": "otto-designer", "target": "otto-designer:0.0", "cli": ["claude"], "workdir": str(tmp_path / "worktrees" / "designer")},
                            {"role": "director", "slot": 1, "tmux_session": "otto-director", "target": "otto-director:0.0", "cli": ["claude"], "workdir": str(tmp_path / "worktrees" / "director")},
                            {"role": "audit", "slot": 2, "tmux_session": "otto-audit", "target": "otto-audit:0.0", "cli": ["claude"], "workdir": str(tmp_path / "worktrees" / "audit")},
                            {"role": "ops", "slot": 3, "tmux_session": "otto-ops", "target": "otto-ops:0.0", "cli": ["codex"], "workdir": str(tmp_path / "worktrees" / "ops")},
                            {"role": "app", "slot": 4, "tmux_session": "otto-app", "target": "otto-app:0.0", "cli": ["codex"], "workdir": str(tmp_path / "worktrees" / "app")},
                            {"role": "main", "slot": 5, "tmux_session": "otto-main", "target": "otto-main:0.0", "cli": ["codex"], "workdir": str(tmp_path / "worktrees" / "main")},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_project_config("otto", config_path)
            runner = OwnerTmuxRunner(
                {
                    "otto-designer",
                    "otto-director",
                    "otto-audit",
                    "otto-ops",
                    "otto-app",
                    "otto-main",
                    "pgu-director",
                    "pgu-ops",
                }
            )
            first_output: list[str] = []
            second_output: list[str] = []

            assert team_launcher.stop_project(config, runner=runner, print_func=first_output.append) == 0
            assert team_launcher.stop_project(config, runner=runner, print_func=second_output.append) == 0
    finally:
        team_launcher.current_user_name = original_current_user_name

    assert runner.existing_sessions == {"pgu-director", "pgu-ops"}
    kill_calls = [call for call in runner.calls if call[4:6] == ["tmux", "kill-session"]]
    assert [call[-1] for call in kill_calls] == [
        "otto-designer",
        "otto-director",
        "otto-audit",
        "otto-ops",
        "otto-app",
        "otto-main",
    ]
    assert all(call[:4] == ["sudo", "-u", "otto-agent", "-H"] for call in kill_calls)
    assert all(call[6] == "-t" for call in kill_calls)
    forbidden_tmux_server_shutdown = "kill" + "-server"
    assert not any(part == forbidden_tmux_server_shutdown for call in runner.calls for part in call)
    assert first_output == [
        "stopped designer: otto-designer",
        "stopped director: otto-director",
        "stopped audit: otto-audit",
        "stopped ops: otto-ops",
        "stopped app: otto-app",
        "stopped main: otto-main",
    ]
    assert second_output == [
        "already stopped designer: otto-designer",
        "already stopped director: otto-director",
        "already stopped audit: otto-audit",
        "already stopped ops: otto-ops",
        "already stopped app: otto-app",
        "already stopped main: otto-main",
    ]


def test_stop_project_continues_after_failed_kill_and_reports_all_roles() -> None:
    class FailingOwnerTmuxRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:4] != ["sudo", "-u", "otto-agent", "-H"]:
                return subprocess.CompletedProcess(args, 0)
            inner = args[4:]
            if inner[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 0)
            if inner[:2] == ["tmux", "kill-session"] and inner[-1] == "otto-director":
                return subprocess.CompletedProcess(args, 7, stderr="permission denied\n")
            return subprocess.CompletedProcess(args, 0)

    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "root"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-stop-fail.") as tmp:
            tmp_path = Path(tmp)
            layout = tmp_path / "layout.json"
            layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
            for role_name in ("designer", "director", "audit"):
                (tmp_path / "worktrees" / role_name).mkdir(parents=True)
            config_path = tmp_path / "otto.json"
            config_path.write_text(
                json.dumps(
                    {
                        "project": "otto",
                        "layout": str(layout),
                        "run_as_user": "otto-agent",
                        "roles": [
                            {"role": "designer", "slot": 0, "tmux_session": "otto-designer", "target": "otto-designer:0.0", "cli": ["claude"], "workdir": str(tmp_path / "worktrees" / "designer")},
                            {"role": "director", "slot": 1, "tmux_session": "otto-director", "target": "otto-director:0.0", "cli": ["claude"], "workdir": str(tmp_path / "worktrees" / "director")},
                            {"role": "audit", "slot": 2, "tmux_session": "otto-audit", "target": "otto-audit:0.0", "cli": ["claude"], "workdir": str(tmp_path / "worktrees" / "audit")},
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_project_config("otto", config_path)
            runner = FailingOwnerTmuxRunner()
            output: list[str] = []

            assert team_launcher.stop_project(config, runner=runner, print_func=output.append) == 7
    finally:
        team_launcher.current_user_name = original_current_user_name

    kill_calls = [call for call in runner.calls if call[4:6] == ["tmux", "kill-session"]]
    assert [call[-1] for call in kill_calls] == ["otto-designer", "otto-director", "otto-audit"]
    assert output == [
        "stopped designer: otto-designer",
        "failed to stop director: otto-director: permission denied",
        "stopped audit: otto-audit",
    ]


def test_reload_uses_recorded_resume_uuid_when_recreating_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = Path(tmp) / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "codex"})

        assert _command_tail(cli_command_for_role(role, session_dir=session_dir, resume=True))[:3] == [
            "codex",
            "resume",
            session_id,
        ]
        pane_state_dir = Path(tmp) / "pane-state"
        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )
        state = _read_pane_state(pane_state_dir, role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.reload"

        assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
        assert runner.calls[1] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
        assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
        assert runner.calls[3] == ["tmux", "kill-session", "-t", "pgu-ops"]
        assert runner.calls[4][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
        assert f"codex resume {session_id}" in runner.calls[4][-1]
        assert "--continue" not in runner.calls[4][-1]
        assert "--last" not in runner.calls[4][-1]
        assert runner.calls[5] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
        assert runner.calls[6] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
        assert runner.calls[7] == ["tmux", "attach", "-t", "pgu-ops"]


def test_reload_clears_unverified_resume_marker_after_verified_resume() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        timeout_path = session_dir / f"{session_file_name(role.target)}.resume_timeout"
        team_launcher.record_unverified_resume_for_role(role, session_dir, session_id)
        assert timeout_path.exists()
        runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "codex"})

        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                runner=runner,
            )
            == 0
        )
        assert not timeout_path.exists()


def test_resume_commands_use_cli_specific_shapes_and_front_position() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    session_id = "12345678-1234-5678-9abc-def012345678"

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        for role_name in ("director", "ops", "inspector"):
            role = roles[role_name]
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": session_id}) + "\n",
                encoding="utf-8",
            )

        director_command = cli_command_for_role(roles["director"], session_dir=session_dir, resume=True)
        ops_command = cli_command_for_role(roles["ops"], session_dir=session_dir, resume=True)
        inspector_command = cli_command_for_role(roles["inspector"], session_dir=session_dir, resume=True)

        assert any(entry.startswith(f"PATH={default_user_bin()}:") for entry in _env_entries(director_command))
        assert _command_tail(director_command)[:3] == ["claude", "--resume", session_id]
        assert _command_tail(director_command)[3:7] == ["--model", "claude-opus-5", "--effort", "high"]

        assert any(entry.startswith(f"PATH={default_user_bin()}:") for entry in _env_entries(ops_command))
        assert _command_tail(ops_command)[:3] == ["codex", "resume", session_id]
        assert _command_tail(ops_command)[3:7] == ["--model", "gpt-5.5", "-c", "reasoning_effort=high"]

        assert any(entry.startswith(f"PATH={default_user_bin()}:") for entry in _env_entries(inspector_command))
        assert _command_tail(inspector_command)[:3] == ["agy", "--conversation", session_id]
        assert _command_tail(inspector_command)[3:5] == ["--model", "gemini-3.7-flash-high"]


def test_reload_without_recorded_resume_id_logs_fresh_start() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "codex"})
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=Path(tmp) / "sessions",
                    pane_state_dir=Path(tmp) / "pane-state",
                    runner=runner,
                )
                == 0
            )

    assert "team-launcher: no recorded session id for ops; starting fresh session" in stderr.getvalue()
    assert runner.calls[4][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert " resume " not in f" {runner.calls[4][-1]} "
    assert "--resume" not in runner.calls[4][-1]


def test_claude_reload_skips_missing_local_transcript_instead_of_exiting() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "director")
    runner = FakeRunner(existing_sessions={"pgu-director"}, current_commands={"pgu-director:0.0": "claude"})
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        missing_transcript = tmp_path / "missing-claude-session.jsonl"
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "session_id": session_id,
                    "payload": {"transcript_path": str(missing_transcript)},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        assert not (session_dir / session_file_name(role.target)).exists()
        assert json.loads(sidecar.read_text(encoding="utf-8"))["session_id"] == session_id

    assert "recorded claude session" in stderr.getvalue()
    assert "is not present at" in stderr.getvalue()
    assert "starting fresh instead of passing claude --resume" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-director"]]
    assert len(new_sessions) == 1
    assert "--resume" not in new_sessions[0][-1]
    assert session_id not in new_sessions[0][-1]


def test_claude_reload_uses_recorded_id_when_local_transcript_exists() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "director")
    runner = FakeRunner(existing_sessions={"pgu-director"}, current_commands={"pgu-director:0.0": "claude"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        transcript = tmp_path / "claude-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "session_id": session_id,
                    "payload": {"transcript_path": str(transcript)},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )
            == 0
        )

    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-director"]]
    assert len(new_sessions) == 1
    assert f"claude --resume {session_id}" in new_sessions[0][-1]


def test_claude_no_transcript_path_record_uses_project_key_fallback() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "director")
    assert role.workdir == "/home/agent/Projects/pgu"

    session_id = "12345678-1234-5678-9abc-def012345678"

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        session_dir = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        session_dir.mkdir(parents=True)
        claude_project_dir = home / ".claude" / "projects" / "-home-agent-Projects-pgu"
        claude_project_dir.mkdir(parents=True)
        (claude_project_dir / f"{session_id}.jsonl").write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-director"}, current_commands={"pgu-director:0.0": "claude"})

        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )
            == 0
        )

        new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-director"]]
        assert len(new_sessions) == 1
        assert f"claude --resume {session_id}" in new_sessions[0][-1]

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        session_dir = home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
        session_dir.mkdir(parents=True)
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-director"}, current_commands={"pgu-director:0.0": "claude"})
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        assert not (session_dir / session_file_name(role.target)).exists()
        assert json.loads(sidecar.read_text(encoding="utf-8"))["session_id"] == session_id

    assert "recorded claude session" in stderr.getvalue()
    assert "-home-agent-Projects-pgu" in stderr.getvalue()
    assert "starting fresh instead of passing claude --resume" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-director"]]
    assert len(new_sessions) == 1
    assert "--resume" not in new_sessions[0][-1]
    assert session_id not in new_sessions[0][-1]


def test_codex_reload_skips_missing_local_transcript_instead_of_passing_resume() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "codex"})
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        missing_transcript = tmp_path / "missing-codex-session.jsonl"
        session_id = "019f4ba6-c163-7793-af7a-710b6ab09283"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "session_id": session_id,
                    "payload": {"transcript_path": str(missing_transcript)},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

    assert "recorded codex session" in stderr.getvalue()
    assert "starting fresh instead of passing codex resume" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]]
    assert len(new_sessions) == 1
    assert f"codex resume {session_id}" not in new_sessions[0][-1]


def test_claude_start_falls_back_fresh_when_resume_attempt_exits_immediately() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "director")
    runner = ResumeExitFakeRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        transcript = tmp_path / "claude-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "session_id": session_id,
                    "payload": {"transcript_path": str(transcript)},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="start",
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        assert not (session_dir / session_file_name(role.target)).exists()
        assert json.loads(sidecar.read_text(encoding="utf-8"))["session_id"] == session_id

    assert "resume failed for director using session" in stderr.getvalue()
    assert "falling back to fresh session" in stderr.getvalue()
    assert "started fresh session for director after resume fallback" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-director"]]
    assert len(new_sessions) == 2
    assert f"claude --resume {session_id}" in new_sessions[0][-1]
    assert "--resume" not in new_sessions[1][-1]


def test_reload_preserves_session_record_when_resume_cli_is_slow_to_exec() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    stderr = StringIO()
    original_timeout = team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS
    original_poll = team_launcher.RESUME_STARTUP_POLL_SECONDS
    try:
        team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.02
        team_launcher.RESUME_STARTUP_POLL_SECONDS = 0.0
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            session_dir = Path(tmp)
            session_id = "12345678-1234-5678-9abc-def012345678"
            transcript = Path(tmp) / "codex-session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps(
                    {"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}}
                )
                + "\n",
                encoding="utf-8",
            )
            sidecar_path = (session_dir / session_file_name(role.target)).with_name(
                f"{session_file_name(role.target)}.superseded"
            )
            timeout_path = (session_dir / session_file_name(role.target)).with_name(
                f"{session_file_name(role.target)}.resume_timeout"
            )
            runner = FakeRunner(
                existing_sessions={"pgu-ops"},
                current_commands={"pgu-ops:0.0": ["codex", "fish", "fish", "fish"]},
            )

            with redirect_stderr(stderr):
                pane_state_dir = Path(tmp) / "pane-state"
                assert (
                    run_role_pane(
                        role,
                        mode="reload",
                        session_dir=session_dir,
                        pane_state_dir=pane_state_dir,
                        runner=runner,
                    )
                    == 0
                )
                assert not (pane_state_dir / pane_state_file_name(role.target)).exists()
                assert json.loads((session_dir / session_file_name(role.target)).read_text(encoding="utf-8"))[
                    "session_id"
                ] == session_id
                assert not sidecar_path.exists()
                timeout_record = json.loads(timeout_path.read_text(encoding="utf-8"))
                assert timeout_record["target"] == role.target
                assert timeout_record["session_id"] == session_id
                assert timeout_record["reason"] == "resume_startup_timeout"

        assert (
            f"team-launcher: resume for ops using session {session_id} was not verified within 0.02s; "
            "leaving tmux session and session record intact"
        ) in stderr.getvalue()
        assert "falling back to fresh session" not in stderr.getvalue()
        new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]]
        kill_sessions = [call for call in runner.calls if call[:2] == ["tmux", "kill-session"]]
        assert len(new_sessions) == 1
        assert len(kill_sessions) == 1
        assert f"codex resume {session_id}" in new_sessions[0][-1]
    finally:
        team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS = original_timeout
        team_launcher.RESUME_STARTUP_POLL_SECONDS = original_poll


def test_agy_reload_skips_missing_local_conversation_store_instead_of_silent_fallback() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "inspector")
    runner = FakeRunner(existing_sessions={"pgu-inspector"}, current_commands={"pgu-inspector:0.0": "agy"})
    stderr = StringIO()
    original_root = team_launcher.AGY_CONVERSATION_ROOT

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        sidecar_path = (session_dir / session_file_name(role.target)).with_name(
            f"{session_file_name(role.target)}.superseded"
        )
        team_launcher.AGY_CONVERSATION_ROOT = tmp_path / "empty-agy-store"
        try:
            with redirect_stderr(stderr):
                assert (
                    run_role_pane(
                        role,
                        mode="reload",
                        session_dir=session_dir,
                        pane_state_dir=tmp_path / "pane-state",
                        runner=runner,
                    )
                    == 0
                )
                assert not (session_dir / session_file_name(role.target)).exists()
                assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == session_id
        finally:
            team_launcher.AGY_CONVERSATION_ROOT = original_root

    assert "recorded agy/gemini conversation" in stderr.getvalue()
    assert "silently falls back" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-inspector"]]
    assert len(new_sessions) == 1
    assert "--conversation" not in new_sessions[0][-1]
    assert session_id not in new_sessions[0][-1]


def test_agy_reload_uses_recorded_id_when_local_conversation_store_exists() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "inspector")
    runner = FakeRunner(existing_sessions={"pgu-inspector"}, current_commands={"pgu-inspector:0.0": "agy"})
    original_root = team_launcher.AGY_CONVERSATION_ROOT

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).parent.mkdir(parents=True, exist_ok=True)
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )
        agy_root = tmp_path / "agy-store"
        (agy_root / "conversations").mkdir(parents=True)
        (agy_root / "conversations" / f"{session_id}.db").write_text("sqlite placeholder\n", encoding="utf-8")
        team_launcher.AGY_CONVERSATION_ROOT = agy_root
        try:
            assert (
                run_role_pane(
                    role,
                    mode="reload",
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )
        finally:
            team_launcher.AGY_CONVERSATION_ROOT = original_root

    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-inspector"]]
    assert len(new_sessions) == 1
    assert f"agy --conversation {session_id}" in new_sessions[0][-1]


def test_reload_refuses_to_kill_session_when_live_command_mismatches_config() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp) / "sessions"
        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                runner=runner,
            )
            == 1
        )

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"],
    ]


def test_reload_force_bypasses_live_command_guard() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp) / "sessions"
        assert (
            run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=Path(tmp) / "pane-state",
                force_reload=True,
                runner=runner,
            )
            == 0
        )

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1] == ["tmux", "kill-session", "-t", "pgu-ops"]


def test_reload_guard_matches_cli_child_under_shell_in_real_tmux() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-real-tmux.") as tmp:
        server = f"pgu321-reload-guard-{os.getpid()}"
        runner = _isolated_tmux_runner(server)
        helper = Path(tmp) / "codex"
        marker = Path(tmp) / "running"
        helper.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            "trap 'exit 0' TERM INT\n"
            "while :; do sleep 1; done\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        session = f"pgu-321-reload-guard-{os.getpid()}"
        role = next(role for role in load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json").roles if role.role == "ops")
        role = role.__class__(
            role=role.role,
            slot=role.slot,
            detached=False,
            tmux_session=session,
            target=f"{session}:0.0",
            workdir=tmp,
            cli=[str(helper)],
            model="",
            model_arg=role.model_arg,
            effort="",
            yolo=False,
            extra_args=[],
            resume_mode=role.resume_mode,
            resume_flag=role.resume_flag,
            resume_subcommand=role.resume_subcommand,
            live_commands=["codex"],
            env={},
        )
        try:
            _run_isolated_tmux(
                server,
                [
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "sh",
                    "-c",
                    f"exec {helper}",
                ],
                check=True,
            )
            for _ in range(50):
                if marker.exists():
                    break
                time.sleep(0.1)
            assert marker.exists(), "real tmux helper never started"
            pane_command = _run_isolated_tmux(
                server,
                ["display-message", "-p", "-t", role.target, "#{pane_current_command}"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            assert pane_command in {"sh", "fish", "bash", "zsh"}, pane_command
            assert live_command_matches_role(role, runner=runner)
        finally:
            _cleanup_isolated_tmux_sessions(server, [session])


def test_removed_bootstrap_command_names_new_as_replacement() -> None:
    try:
        team_launcher.main(["porter", "bootstrap"])
        raise AssertionError("expected removed command failure")
    except SystemExit as exc:
        message = str(exc)

    assert "bootstrap has been removed" in message
    assert "switchyard new" in message


def test_new_project_dry_run_writes_board_and_launcher_artifacts() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        runner = FakeRunner()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=runner,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )

        rendered = stdout.getvalue()
        config = json.loads((output_dir / "porter.json").read_text(encoding="utf-8"))
        layout = json.loads((output_dir / "porter-konsole-layout.json").read_text(encoding="utf-8"))
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        board_unit = (output_dir / "porter-ticket-board.service").read_text(encoding="utf-8")
        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")
        workflow_sql = (output_dir / "porter-workflow.sql").read_text(encoding="utf-8")
        loaded_config = load_project_config("porter", output_dir / "porter.json")
        launch_output = tmp_path / "launch-layout.json"
        launch_stdout = StringIO()

        with redirect_stdout(launch_stdout):
            assert (
                launch_project(
                    loaded_config,
                    config_path=output_dir / "porter.json",
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    dry_run=True,
                    layout_output=launch_output,
                )
                == 0
            )
        launch_plan = json.loads(launch_stdout.getvalue())
        launch_output_exists = launch_output.exists()

    assert plan["project"] == "porter"
    assert plan["ticket_prefix"] == "PORTER"
    assert plan["owner_user"] == current_user
    assert plan["port"] == 23682
    assert config["project"] == "porter"
    assert config["ticket_prefix"] == "PORTER"
    assert config["run_as_user"] == current_user
    assert config["board_url"] == "http://127.0.0.1:23682"
    assert config["board_socket"] == "/run/porter-ticket-board/ticket-board.sock"
    assert config["repository"] == str(project_repo)
    assert config["repository"] != str(source_repo)
    assert config["control_repository"] == f"/home/{current_user}/.local/state/switchyard/projects/porter/control.git"
    assert config["worktree_base"] == f"/home/{current_user}/porter-worktrees"
    assert config["session_dir"] == f"/home/{current_user}/.local/state/porter-ticket-board/pane-sessions"
    assert not config["session_dir"].startswith("/run/")
    assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "app", "main"]
    assert [role["tmux_session"] for role in config["roles"]] == [
        "porter-designer",
        "porter-director",
        "porter-audit",
        "porter-app",
        "porter-main",
    ]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "designer": ["claude"],
        "director": ["claude"],
        "audit": ["claude"],
        "app": ["codex"],
        "main": ["codex"],
    }
    assert {"designer", "director", "main", "app", "audit"} <= {role["role"] for role in config["roles"]}
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[]" in workflow_sql
    assert "('in_progress', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('done', 'Done', 9, ARRAY[]::text[], NULL, NULL, NULL, true)" in workflow_sql
    assert "('cancelled', 'Cancelled', 10, ARRAY[]::text[], NULL, NULL, NULL, true)" in workflow_sql
    assert "('audit', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('audit', 'in_progress', 'audit_kick_back', ARRAY['audit']::text[]" in workflow_sql
    assert "('director_review', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('director_review', 'analysis', 'user_reopen', ARRAY['user']::text[]" in workflow_sql
    assert "('director_review', 'in_progress', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" in workflow_sql
    assert "('director_review', 'cancelled', 'cancel', ARRAY['director']::text[]" in workflow_sql
    assert "('done', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('done', 'analysis', 'user_reopen', ARRAY['user']::text[]" in workflow_sql
    assert "('cancelled', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert len(team_launcher._layout_leaves(layout)) == 5
    assert [role.role for role in loaded_config.roles] == ["designer", "director", "audit", "app", "main"]
    assert loaded_config.roles[0].target == "porter-designer:0.0"
    assert loaded_config.roles[3].target == "porter-app:0.0"
    assert loaded_config.roles[4].target == "porter-main:0.0"
    assert loaded_config.control_repository == Path(config["control_repository"])
    assert loaded_config.worktree_base == Path(config["worktree_base"])
    assert loaded_config.session_dir == Path(config["session_dir"])
    assert {role.role: role.workdir for role in loaded_config.roles} == {
        "designer": f"/home/{current_user}/porter-worktrees/designer",
        "director": f"/home/{current_user}/porter-worktrees/director",
        "audit": f"/home/{current_user}/porter-worktrees/audit",
        "app": f"/home/{current_user}/porter-worktrees/app",
        "main": f"/home/{current_user}/porter-worktrees/main",
    }
    assert {role.env["TICKET_BOARD_TICKET_PREFIX"] for role in loaded_config.roles} == {"PORTER"}
    assert commands.splitlines()[:2] == ["#!/usr/bin/env bash", "set -euo pipefail"]
    assert "Environment=TICKET_BOARD_PROJECT=porter" in board_unit
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=PORTER" in board_unit
    assert f"team-launcher: dry-run for porter; artifacts in {output_dir}" in rendered
    assert "  sudo -v\n" in rendered
    assert "  bash operator-commands.sh\n" in rendered
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert launch_plan["project"] == "porter"
    assert launch_plan["mode"] == "attach-or-start"
    assert [role["target"] for role in launch_plan["roles"]] == [
        "porter-designer:0.0",
        "porter-director:0.0",
        "porter-audit:0.0",
        "porter-app:0.0",
        "porter-main:0.0",
    ]
    assert [role["workdir"] for role in launch_plan["roles"]] == [
        f"/home/{current_user}/porter-worktrees/designer",
        f"/home/{current_user}/porter-worktrees/director",
        f"/home/{current_user}/porter-worktrees/audit",
        f"/home/{current_user}/porter-worktrees/app",
        f"/home/{current_user}/porter-worktrees/main",
    ]
    cli_env_entries = _env_entries(cli_command_for_role(loaded_config.roles[0], session_dir=loaded_config.session_dir))
    assert f"TICKET_BOARD_PANE_SESSION_DIR={config['session_dir']}" in cli_env_entries
    assert not any(entry.startswith("TICKET_BOARD_PANE_SESSION_DIR=/run/") for entry in cli_env_entries)
    assert launch_output_exists


def test_design_writes_artifact_that_new_from_consumes_for_missing_owner_user() -> None:
    missing_owner = "pgu647-missing-agent"
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        design_dir = tmp_path / "design"
        provision_dir = tmp_path / "provision"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        design_stdout = StringIO()
        provision_stdout = StringIO()
        runner = FakeRunner()

        with redirect_stdout(design_stdout):
            assert (
                design_project_command(
                    "porter",
                    output_dir=design_dir,
                    design_title="Porter",
                    design_body="A small coordination tool.",
                    repository=project_repo,
                    remote="upstream",
                    default_branch="trunk",
                    worktree_policy="shared",
                    owner_user=missing_owner,
                    ticket_prefix="PORT",
                    push_policy="director-main-only",
                    audit_signoff=True,
                    needs_inspection=False,
                    needs_user_signoff=False,
                    board_service_traversal=True,
                    supplementary_groups=[],
                    linger=True,
                    owner_shell="fish",
                    input_func=_no_input,
                )
                == 0
            )
        artifact_path = design_dir / "porter.project.json"
        design_doc = design_dir / "porter-design.md"
        artifact = load_project_design_artifact(artifact_path, expected_project="porter")

        with redirect_stdout(provision_stdout):
            assert (
                new_project_command(
                    "porter",
                    from_artifact=artifact_path,
                    source_repo=source_repo,
                    output_dir=provision_dir,
                    dry_run=True,
                    runner=runner,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )

        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((provision_dir / "porter.json").read_text(encoding="utf-8"))
        board_unit = (provision_dir / "porter-ticket-board.service").read_text(encoding="utf-8")
        design_doc_text = design_doc.read_text(encoding="utf-8")

    assert "team-launcher: wrote design document" in design_stdout.getvalue()
    assert "team-launcher: dry-run for porter" in provision_stdout.getvalue()
    assert design_doc_text.startswith("# Porter\n\nA small coordination tool.\n")
    assert artifact.owner_user == missing_owner
    assert artifact.repository == project_repo
    assert artifact.ticket_prefix == "PORT"
    assert plan["owner_user"] == missing_owner
    assert plan["ticket_prefix"] == "PORT"
    assert config["ticket_prefix"] == "PORT"
    assert config["repository"] == str(project_repo)
    assert config["session_dir"] == f"/home/{missing_owner}/.local/state/porter-ticket-board/pane-sessions"
    assert not config["session_dir"].startswith("/run/")
    assert config["worktree_remote"] == "upstream"
    assert config["worktree_branch"] == "trunk"
    assert config["control_repository"] == f"/home/{missing_owner}/.local/state/switchyard/projects/porter/control.git"
    assert config["worktree_base"] == f"/home/{missing_owner}/porter-worktrees"
    assert "Environment=TICKET_BOARD_PROJECT=porter" in board_unit
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=PORT" in board_unit
    assert not any(call[:1] == ["sudo"] for call in runner.calls)


def test_design_cli_writes_project_artifact_noninteractively() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.main(
                    [
                        "porter",
                        "design",
                        "--design-output-dir",
                        str(tmp_path / "design"),
                        "--design-title",
                        "Porter",
                        "--design-body",
                        "CLI design body.",
                        "--repository",
                        str(project_repo),
                        "--owner-user",
                        "pgu647-missing-agent",
                        "--ticket-prefix",
                        "PORT",
                        "--remote",
                        "origin",
                        "--default-branch",
                        "main",
                        "--worktree-policy",
                        "shared",
                        "--push-policy",
                        "director-main-only",
                        "--audit-signoff",
                        "--no-needs-inspection",
                        "--no-needs-user-signoff",
                        "--board-service-traversal",
                        "--supplementary-group",
                        "kvm",
                        "--linger",
                        "--owner-shell",
                        "fish",
                    ]
                )
                == 0
            )

        artifact = load_project_design_artifact(tmp_path / "design" / "porter.project.json", expected_project="porter")

    assert "team-launcher: wrote project artifact" in stdout.getvalue()
    assert artifact.ticket_prefix == "PORT"
    assert artifact.owner_user == "pgu647-missing-agent"


def test_switchyard_menu_lists_new_first_then_projects() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-menu.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        (tmp_path / "porter.json").write_text(
            json.dumps(
                {
                    "project": "porter",
                    "project_name": "Porter System",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert switchyard_menu_command(config_dir=tmp_path, registry_dir=registry_dir, print_func=lines.append) == 0

    assert lines == ["new...", "Porter System (porter)"]


def test_switchyard_registry_registers_pointer_and_survives_checkout_clean() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-registry.") as tmp:
        tmp_path = Path(tmp)
        checkout_config_dir = tmp_path / "checkout" / "config" / "team-launcher"
        registry_dir = tmp_path / "etc" / "switchyard" / "projects"
        project_config_dir = tmp_path / "otto" / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        checkout_config_dir.mkdir(parents=True)
        project_config_dir.mkdir(parents=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        pgu_config = checkout_config_dir / "pgu.json"
        pgu_config.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "pgu")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        otto_config = project_config_dir / "otto.json"
        otto_config.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "otto")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert switchyard_register_command(otto_config, registry_dir=registry_dir, print_func=lines.append) == 0
        pointer = json.loads((registry_dir / "otto.json").read_text(encoding="utf-8"))
        assert pointer == {
            "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
            "slug": "otto",
            "name": "Otto System",
            "config_path": str(otto_config.resolve(strict=False)),
        }
        assert (registry_dir / "otto.json").stat().st_mode & 0o777 == 0o644
        assert team_launcher._resolve_switchyard_project("otto", config_dir=checkout_config_dir, registry_dir=registry_dir).config_path == otto_config

        _run_git(["git", "init", "-b", "main"], cwd=tmp_path / "checkout")
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=tmp_path / "checkout")
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=tmp_path / "checkout")
        _run_git(["git", "add", "config/team-launcher/pgu.json"], cwd=tmp_path / "checkout")
        _run_git(["git", "commit", "-m", "tracked pgu config"], cwd=tmp_path / "checkout")
        (checkout_config_dir / "stray.json").write_text("{}\n", encoding="utf-8")
        _run_git(["git", "clean", "-fdx"], cwd=tmp_path / "checkout")

        menu_lines: list[str] = []
        assert switchyard_menu_command(config_dir=checkout_config_dir, registry_dir=registry_dir, print_func=menu_lines.append) == 0
        assert menu_lines == ["new...", "Otto System (otto)", "pgu"]
        assert (registry_dir / "otto.json").is_file()
        assert team_launcher._resolve_switchyard_project("pgu", config_dir=checkout_config_dir, registry_dir=registry_dir).config_path == pgu_config

    assert lines and "switchyard: registered" in lines[0]


def test_switchyard_register_rejects_dashed_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        config_path = tmp_path / "bad-project.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "project": "bad-project",
                    "project_name": "Bad Project",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path)}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            switchyard_register_command(config_path, registry_dir=tmp_path / "registry", print_func=lambda _line: None)
            raise AssertionError("expected dashed slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug must match" in message
    assert "^[a-z0-9][a-z0-9_]{0,39}$" in message


def test_switchyard_register_rejects_duplicate_slug_without_overwriting_pointer() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        layout = tmp_path / "layout.json"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        existing_config = config_dir / "existing.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        existing_payload = {
            "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
            "slug": "otto",
            "name": "Otto Existing",
            "config_path": str(existing_config),
        }
        pointer = registry_dir / "otto.json"
        pointer.write_text(json.dumps(existing_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        before = pointer.read_bytes()
        duplicate_config = config_dir / "duplicate.json"
        duplicate_config.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Different Otto",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "other")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            switchyard_register_command(duplicate_config, registry_dir=registry_dir, print_func=lambda _line: None)
            raise AssertionError("expected duplicate slug rejection")
        except SystemExit as exc:
            message = str(exc)

        after = pointer.read_bytes()

    assert "project slug 'otto' is already registered to 'Otto Existing'" in message
    assert str(existing_config) in message
    assert after == before


def test_switchyard_register_rejects_duplicate_project_name_without_writing_pointer() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        layout = tmp_path / "layout.json"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        existing_config = config_dir / "otto.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(existing_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        duplicate_config = config_dir / "atlas.json"
        duplicate_config.write_text(
            json.dumps(
                {
                    "project": "atlas",
                    "project_name": "Otto Scheduler",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "atlas")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            switchyard_register_command(duplicate_config, registry_dir=registry_dir, print_func=lambda _line: None)
            raise AssertionError("expected duplicate project name rejection")
        except SystemExit as exc:
            message = str(exc)
        duplicate_pointer_exists = (registry_dir / "atlas.json").exists()

    assert "project name 'Otto Scheduler' is already registered as 'otto'" in message
    assert str(existing_config) in message
    assert not duplicate_pointer_exists


def test_switchyard_new_rejects_duplicate_slug_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        registry_dir.mkdir()
        existing_config = tmp_path / "otto.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(existing_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="otto",
                agent_name="atlas",
                project_name="Atlas",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                registry_dir=registry_dir,
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected duplicate slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug 'otto' is already registered to 'Otto Scheduler'" in message
    assert str(existing_config) in message
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "atlas-agent").exists()


def test_switchyard_new_rejects_duplicate_project_name_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        registry_dir.mkdir()
        existing_config = tmp_path / "otto.json"
        existing_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(existing_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="atlas",
                agent_name="atlas",
                project_name="Otto Scheduler",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                registry_dir=registry_dir,
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected duplicate project name rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project name 'Otto Scheduler' is already registered as 'otto'" in message
    assert str(existing_config) in message
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "atlas-agent").exists()


def test_switchyard_register_cli_enables_launch_by_name_without_config_flag() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-register.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "empty-checkout-config"
        registry_dir = tmp_path / "registry"
        project_config_dir = tmp_path / "otto" / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        project_config_dir.mkdir(parents=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = project_config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "pane_launcher": "/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher",
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path / "otto")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, "pane_launcher": config.pane_launcher, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])
            assert switchyard_main(["register", str(config_path)]) == 0
            assert switchyard_main(["otto"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls
    assert calls[0]["project"] == "otto"
    assert calls[0]["config_path"] == config_path
    assert calls[0]["mode"] == "start"
    assert calls[0]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert calls[0]["pane_launcher"] == Path("/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher")
    assert calls[0]["report_session_records"] is True


def test_switchyard_launch_runs_first_run_auth_before_panes_and_warns_after() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-first-run-launch.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [{"role": "ops", "slot": 0, "cli": ["codex"], "workdir": str(tmp_path / "repo")}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        events: list[tuple[str, object]] = []
        report = team_launcher.FirstRunAuthReport({"codex": ["ops"]}, [])
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_run_first_run_auth_phase = team_launcher.run_first_run_auth_phase
        original_report_first_run_auth_warnings = team_launcher.report_first_run_auth_warnings
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_run_first_run_auth_phase(
                config: team_launcher.ProjectConfig,
                **kwargs: object,
            ) -> team_launcher.FirstRunAuthReport:
                events.append(
                    (
                        "auth",
                        {
                            "project": config.project,
                            "owner_user": kwargs["owner_user"],
                            "owner_home": kwargs["owner_home"],
                        },
                    )
                )
                return report

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                events.append(("launch", {"project": config.project, **kwargs}))
                return 0

            def fake_report_first_run_auth_warnings(
                auth_report: team_launcher.FirstRunAuthReport,
                **_kwargs: object,
            ) -> None:
                events.append(("warn", auth_report))

            team_launcher.run_first_run_auth_phase = fake_run_first_run_auth_phase
            team_launcher.launch_project = fake_launch_project
            team_launcher.report_first_run_auth_warnings = fake_report_first_run_auth_warnings

            assert switchyard_main(["otto"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_first_run_auth_phase = original_run_first_run_auth_phase
            team_launcher.report_first_run_auth_warnings = original_report_first_run_auth_warnings

    assert [name for name, _payload in events] == ["auth", "launch", "warn"]
    assert events[0][1] == {"project": "otto", "owner_user": "otto-agent", "owner_home": Path("/home/otto-agent")}
    assert events[1][1]["config_path"] == config_path
    assert events[1][1]["mode"] == "start"
    assert events[1][1]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert events[1][1]["report_session_records"] is True
    assert events[2][1] is report


def test_switchyard_stop_resolves_project_without_launching_or_authenticating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-stop.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = config_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [{"role": "ops", "slot": 0, "tmux_session": "otto-ops", "target": "otto-ops:0.0", "cli": ["codex"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[tuple[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_stop_project = team_launcher.stop_project
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_stop_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append(("stop", {"project": config.project, "run_as_user": config.run_as_user}))
                return 0

            def fake_launch_project(_config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append(("launch", {}))
                return 0

            def fake_first_run_auth(_config: team_launcher.ProjectConfig) -> team_launcher.FirstRunAuthReport:
                calls.append(("auth", {}))
                return team_launcher.FirstRunAuthReport({}, [])

            team_launcher.stop_project = fake_stop_project
            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = fake_first_run_auth

            assert switchyard_main(["stop", "Otto", "System"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.stop_project = original_stop_project
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls == [("stop", {"project": "otto", "run_as_user": "otto-agent"})]


def test_switchyard_registry_does_not_change_pgu_resolution_or_launch_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-pgu-registry.") as tmp:
        registry_dir = Path(tmp) / "registry"
        registry_dir.mkdir()
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto System",
                    "config_path": "/home/otto-agent/Projects/otto/.switchyard/provision/otto.json",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append(
                    {
                        "project": config.project,
                        "pane_launcher": config.pane_launcher,
                        "workdirs": [role.workdir for role in config.roles],
                        **kwargs,
                    }
                )
                return 0

            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])
            assert switchyard_main(["pgu"]) == 0
        finally:
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls
    assert calls[0]["project"] == "pgu"
    assert calls[0]["config_path"] == ROOT / "config" / "team-launcher" / "pgu.json"
    assert calls[0]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert calls[0]["pane_launcher"] is None
    assert set(calls[0]["workdirs"]) == {"/home/agent/Projects/pgu"}


def test_legacy_and_switchyard_entrypoints_render_same_plain_konsole_command() -> None:
    def run_entrypoint(
        argv: list[str],
        *,
        config_path: Path,
        config_dir: Path,
        registry_dir: Path,
        layout_output: Path,
    ) -> tuple[list[str], list[str]]:
        runner = FakeRunner()
        original_launch_project = team_launcher.launch_project
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_current_user_name = team_launcher.current_user_name
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            team_launcher.current_user_name = lambda: "root"
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])

            def launch_with_fake_runner(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                kwargs["runner"] = runner
                kwargs["layout_output"] = layout_output
                kwargs["layout_mode"] = team_launcher.LAYOUT_MODE_SEPARATE
                kwargs["report_session_records"] = False
                kwargs["no_launcher_self_deploy"] = True
                return original_launch_project(config, **kwargs)

            team_launcher.launch_project = launch_with_fake_runner
            if argv[0] == "team-launcher":
                assert team_launcher.main(["pgu", "start", "--config", str(config_path)]) == 0
            else:
                assert switchyard_main(["pgu"]) == 0
        finally:
            team_launcher.launch_project = original_launch_project
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.current_user_name = original_current_user_name
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

        konsole_calls = [call for call in runner.calls if "konsole" in call]
        assert len(konsole_calls) == 1
        pane_commands = _leaf_commands(json.loads(layout_output.read_text(encoding="utf-8")))
        return konsole_calls[0], pane_commands

    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-entrypoint-konsole.") as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            config_dir = tmp_path / "config"
            config_dir.mkdir()
            registry_dir = tmp_path / "registry"
            registry_dir.mkdir()
            config_path = _write_minimal_shared_checkout_config(config_dir, repo, ["ops"], run_as_user="agent")
            (config_dir / "layout.json").write_text(
                json.dumps(
                    {
                        "Widgets": [
                            {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            legacy_layout = tmp_path / "legacy-layout.json"
            switchyard_layout = tmp_path / "switchyard-layout.json"

            legacy_command, legacy_pane_commands = run_entrypoint(
                ["team-launcher"],
                config_path=config_path,
                config_dir=config_dir,
                registry_dir=registry_dir,
                layout_output=legacy_layout,
            )
            switchyard_command, switchyard_pane_commands = run_entrypoint(
                ["switchyard"],
                config_path=config_path,
                config_dir=config_dir,
                registry_dir=registry_dir,
                layout_output=switchyard_layout,
            )

        assert legacy_command == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            str(legacy_layout),
        ]
        assert switchyard_command == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            str(switchyard_layout),
        ]
        assert len(legacy_pane_commands) == 1
        assert len(switchyard_pane_commands) == 1
        assert shlex.split(legacy_pane_commands[0])[:4] == ["sudo", "-u", "agent", "-H"]
        assert shlex.split(switchyard_pane_commands[0])[:4] == ["sudo", "-u", "agent", "-H"]
        assert " pane attach-or-start ops " in f" {legacy_pane_commands[0]} "
        assert " pane attach-or-start ops " in f" {switchyard_pane_commands[0]} "
    finally:
        os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland


def test_switchyard_project_name_argv_joins_and_resumes_matching_project() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-open.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "project_name": "My Project Name",
                    "layout": str(layout),
                    "pane_launcher": "/home/porter-agent/porter-ticketboard-live/current/scripts/team-launcher",
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        original_first_run_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            team_launcher.DEFAULT_CONFIG_DIR = tmp_path
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, "pane_launcher": config.pane_launcher, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = lambda _config: team_launcher.FirstRunAuthReport({}, [])
            assert switchyard_main(["my", "project", "name"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project
            team_launcher.run_switchyard_launch_first_run_auth = original_first_run_auth

    assert calls
    assert calls[0]["project"] == "porter"
    assert calls[0]["config_path"] == config_path
    assert calls[0]["mode"] == "start"
    assert calls[0]["script_path"] == ROOT / "scripts" / "team-launcher"
    assert calls[0]["pane_launcher"] == Path("/home/porter-agent/porter-ticketboard-live/current/scripts/team-launcher")
    assert calls[0]["report_session_records"] is True


def test_team_launcher_main_resolves_registered_project_for_pane_operations() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-pane.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "empty-checkout-config"
        registry_dir = tmp_path / "registry"
        project_dir = tmp_path / "otto-project"
        provision_dir = project_dir / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        session_dir = tmp_path / "sessions"
        pane_state_dir = tmp_path / "pane-state"
        config_dir.mkdir()
        registry_dir.mkdir()
        provision_dir.mkdir(parents=True)
        project_dir.mkdir(exist_ok=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = provision_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "session_dir": str(session_dir),
                    "roles": [
                        {
                            "role": "app",
                            "slot": 0,
                            "tmux_session": "otto-app",
                            "target": "otto-app:0.0",
                            "cli": ["codex"],
                            "workdir": str(project_dir),
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto System",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_run_role_pane = team_launcher.run_role_pane
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_run_role_pane(role: team_launcher.RoleConfig, **kwargs: object) -> int:
                calls.append({"role": role.role, "tmux_session": role.tmux_session, **kwargs})
                return 0

            team_launcher.run_role_pane = fake_run_role_pane

            assert team_launcher.main(["otto", "pane", "reload", "app", "--skip-launcher-check", "--pane-state-dir", str(pane_state_dir)]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.run_role_pane = original_run_role_pane

    assert calls == [
        {
            "role": "app",
            "tmux_session": "otto-app",
            "mode": "reload",
            "session_dir": session_dir,
            "pane_state_dir": pane_state_dir,
            "force_reload": False,
        }
    ]


def test_team_launcher_main_resolves_registered_project_for_stop() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-stop.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "empty-checkout-config"
        registry_dir = tmp_path / "registry"
        project_dir = tmp_path / "otto-project"
        provision_dir = project_dir / ".switchyard" / "provision"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        provision_dir.mkdir(parents=True)
        project_dir.mkdir(exist_ok=True)
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = provision_dir / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto System",
                    "layout": str(layout),
                    "run_as_user": "otto-agent",
                    "roles": [
                        {
                            "role": "app",
                            "slot": 0,
                            "tmux_session": "otto-app",
                            "target": "otto-app:0.0",
                            "cli": ["codex"],
                            "workdir": str(project_dir),
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto System",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_stop_project = team_launcher.stop_project
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_stop_project(config: team_launcher.ProjectConfig, **_kwargs: object) -> int:
                calls.append({"project": config.project, "run_as_user": config.run_as_user})
                return 0

            team_launcher.stop_project = fake_stop_project

            assert team_launcher.main(["otto", "stop"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.stop_project = original_stop_project

    assert calls == [{"project": "otto", "run_as_user": "otto-agent"}]


def test_team_launcher_main_prefers_checkout_config_before_registry_for_pgu() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-precedence.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        checkout_repo = tmp_path / "checkout-repo"
        registry_repo = tmp_path / "registry-repo"
        layout = tmp_path / "layout.json"
        config_dir.mkdir()
        registry_dir.mkdir()
        checkout_repo.mkdir()
        registry_repo.mkdir()
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        checkout_config = config_dir / "pgu.json"
        registry_config = tmp_path / "registry-pgu.json"
        for path, workdir, role in [
            (checkout_config, checkout_repo, "ops"),
            (registry_config, registry_repo, "app"),
        ]:
            path.write_text(
                json.dumps(
                    {
                        "project": "pgu",
                        "layout": str(layout),
                        "roles": [{"role": role, "slot": 0, "cli": ["codex"], "workdir": str(workdir)}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        (registry_dir / "pgu.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "pgu",
                    "name": "pgu",
                    "config_path": str(registry_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        original_launch_project = team_launcher.launch_project
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"config_path": kwargs["config_path"], "workdirs": [role.workdir for role in config.roles]})
                return 0

            team_launcher.launch_project = fake_launch_project

            assert team_launcher.main(["pgu", "start", "--dry-run"]) == 0
            assert team_launcher.main(["pgu", "start", "--dry-run", "--config", str(checkout_config)]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir
            team_launcher.launch_project = original_launch_project

    assert calls == [
        {"config_path": checkout_config, "workdirs": [str(checkout_repo)]},
        {"config_path": checkout_config, "workdirs": [str(checkout_repo)]},
    ]


def test_team_launcher_unknown_project_names_config_and_registry_dirs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-registry-missing.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        registry_dir = tmp_path / "registry"
        config_dir.mkdir()
        registry_dir.mkdir()
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_registry_dir = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
            try:
                team_launcher.main(["unknown", "start", "--dry-run"])
                raise AssertionError("expected unknown project failure")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = original_registry_dir

    assert message == (
        f"team-launcher: unknown project 'unknown'; searched config dir {config_dir} "
        f"and registry dir {registry_dir}"
    )


def test_main_start_and_reload_enable_session_record_report() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-entry-session-records.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_launch_project = team_launcher.launch_project
        try:
            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            assert team_launcher.main(["porter", "start", "--config", str(config_path)]) == 0
            assert team_launcher.main(["porter", "reload", "--config", str(config_path), "--layout", "viewer"]) == 0
        finally:
            team_launcher.launch_project = original_launch_project

    assert [call["mode"] for call in calls] == ["start", "reload"]
    assert [call["report_session_records"] for call in calls] == [True, True]
    assert [call["layout_mode"] for call in calls] == ["auto", "viewer"]


def test_switchyard_new_without_sudo_fails_before_writing() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="porter",
                agent_name="otto",
                project_name="Porter System",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 1000,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "requires sudo" in message
    assert not output_dir.exists()
    assert not (home_base / "otto-agent" / "Projects" / "porter_system").exists()
    assert calls == []


def test_switchyard_new_rejects_dashed_slug_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        calls: list[list[str]] = []

        try:
            switchyard_new_command(
                slug="otto-scheduler",
                agent_name="otto",
                project_name="Otto Scheduler",
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                registry_dir=tmp_path / "registry",
                input_func=lambda _prompt: "",
            )
            raise AssertionError("expected dashed slug rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "project slug must match" in message
    assert calls == []
    assert not output_dir.exists()
    assert not (home_base / "otto-agent").exists()


def test_switchyard_new_unit_without_database_precheck_runs_before_user_and_project_creation() -> None:
    class UnitWithoutDatabaseRunner(FakeRunner):
        def __init__(self, *, home_base: Path) -> None:
            super().__init__()
            self.home_base = home_base

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service disabled\n")
            if args[:2] == ["psql", "-XAt"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            if args == ["id", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                user_home = self.home_base / "otto-agent"
                user_home.mkdir(parents=True, exist_ok=True)
                (user_home / ".created-by-useradd").write_text("created\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        project_dir = home_base / "otto-agent" / "Projects" / "porter_system"
        created_user_marker = home_base / "otto-agent" / ".created-by-useradd"
        runner = UnitWithoutDatabaseRunner(home_base=home_base)

        try:
            switchyard_new_command(
                slug="porter",
                agent_name="otto",
                project_name="Porter System",
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                input_func=lambda _prompt: "",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected unit-without-database precheck failure")
        except SystemExit as exc:
            message = str(exc)

        assert not created_user_marker.exists()
        assert not project_dir.exists()
        assert not output_dir.exists()

    assert "porter-ticket-board.service is installed" in message
    assert "database 'porter_ticket_board' does not exist" in message
    assert not any(call[:1] == ["useradd"] for call in runner.calls)
    assert not any(call == ["loginctl", "enable-linger", "otto-agent"] for call in runner.calls)
    assert not any(call[:1] == ["install"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def test_switchyard_new_prompts_name_first_and_derives_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        prompts: list[str] = []
        output: list[str] = []
        answers = iter(["Otto Scheduler", "", "", ""])

        def input_func(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        try:
            switchyard_new_command(
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 1000,
                runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                registry_dir=tmp_path / "registry",
                input_func=input_func,
                print_func=output.append,
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    expected_path = home_base / "otto_scheduler-agent" / "Projects" / "otto_scheduler"
    assert "requires sudo" in message
    assert prompts == [
        "Project name: ",
        "Slug [otto_scheduler]: ",
        "Agent user [otto_scheduler-agent]: ",
        f"Project path [{expected_path}]: ",
    ]
    agent_prompt = prompts[2]
    agent_default = agent_prompt.removeprefix("Agent user [").removesuffix("]: ")
    owner_summary = next(line.removeprefix("switchyard: owner user: ") for line in output if line.startswith("switchyard: owner user: "))
    assert agent_default == owner_summary == "otto_scheduler-agent"
    assert f"switchyard: project path: {expected_path}" in output


def test_switchyard_new_edited_slug_does_not_change_name_derived_default_path() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output: list[str] = []

        try:
            switchyard_new_command(
                project_name="Otto Scheduler",
                slug="otto",
                agent_name="otto",
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 1000,
                runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0),
                registry_dir=tmp_path / "registry",
                input_func=lambda _prompt: "",
                print_func=output.append,
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    expected_path = home_base / "otto-agent" / "Projects" / "otto_scheduler"
    assert "requires sudo" in message
    assert "switchyard: slug: otto" in output
    assert f"switchyard: project path: {expected_path}" in output


def test_switchyard_new_writes_initial_artifact_and_starts_full_pane_window() -> None:
    class NewProjectRunner(FakeRunner):
        def __init__(self, *, owner_user: str, project_dir: Path) -> None:
            super().__init__()
            self.call_kwargs: list[dict[str, object]] = []
            self.owner_user = owner_user
            self.project_dir = project_dir
            self.git_output = ""

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.call_kwargs.append(dict(kwargs))
            self.calls.append(args)
            if args[:4] == ["sudo", "-u", self.owner_user, "git"]:
                git_args = args[3:]
                if len(git_args) >= 3 and git_args[:2] == ["git", "-C"] and Path(git_args[2]) == self.project_dir:
                    return self._run_project_git(git_args, **kwargs)
                return subprocess.CompletedProcess(args, 0)
            if args[:4] == ["sudo", "-u", self.owner_user, "mkdir"]:
                return subprocess.CompletedProcess(args, 0)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.project_dir:
                return self._run_project_git(args, **kwargs)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

        def _run_project_git(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            run_kwargs = dict(kwargs)
            run_kwargs.setdefault("text", True)
            if "stdout" not in run_kwargs:
                run_kwargs["stdout"] = subprocess.PIPE
            if "stderr" not in run_kwargs:
                run_kwargs["stderr"] = subprocess.PIPE
            proc = subprocess.run(args, **run_kwargs)
            stdout = str(proc.stdout or "")
            stderr = str(proc.stderr or "")
            self.git_output += stdout + stderr
            return proc

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        pane_state_dir = tmp_path / "pane-state"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        project_dir = home_base / "otto-agent" / "Projects" / "porter_system"
        provision_dir = project_dir / ".switchyard" / "provision"
        registry_dir = tmp_path / "registry"
        runner = NewProjectRunner(owner_user="otto-agent", project_dir=project_dir)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    slug="porter",
                    agent_name="otto",
                    project_name="Porter System",
                    source_repo=source_repo,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=pane_state_dir,
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=registry_dir,
                )
                == 0
        )

        chown_call_index = next(index for index, call in enumerate(runner.calls) if call[:2] == ["chown", "-R"])
        config = json.loads((provision_dir / "porter.json").read_text(encoding="utf-8"))
        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        artifact = json.loads((project_dir / ".switchyard" / "porter.project.json").read_text(encoding="utf-8"))
        generated_layout_path = (
            home_base
            / "otto-agent"
            / ".local"
            / "state"
            / "switchyard"
            / "projects"
            / "porter"
            / "porter-team-layout.json"
        )
        generated_layout = json.loads(generated_layout_path.read_text(encoding="utf-8"))
        generated_commands = _leaf_commands(generated_layout)
        designer_env = next(role["env"] for role in config["roles"] if role["role"] == "designer")
        director_env = next(role["env"] for role in config["roles"] if role["role"] == "director")
        designer_onboarding = (project_dir / ".switchyard" / "DESIGNER_ONBOARDING.md").read_text(encoding="utf-8")
        registry_pointer = json.loads((registry_dir / "porter.json").read_text(encoding="utf-8"))
        expected_pane_launcher = "/home/otto-agent/porter-ticketboard-live/current/scripts/team-launcher"

        assert chown_call_index < next(index for index, call in enumerate(runner.calls) if call == ["sudo", "-v"])
        assert artifact["project"]["repository"] == str(project_dir)
        assert registry_pointer["config_path"] == str((provision_dir / "porter.json").resolve(strict=False))
        assert registry_pointer["name"] == "Porter System"
        assert registry_pointer["slug"] == "porter"
        assert artifact["project"]["roles"] == ["ops", "app", "main"]
        assert plan["implementer_roles"] == ["ops", "app", "main"]
        assert config["pane_launcher"] == expected_pane_launcher
        assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "ops", "app", "main"]
        assert designer_env["SWITCHYARD_PROJECT_ARTIFACT"] == str(project_dir / ".switchyard" / "porter.project.json")
        assert designer_env["SWITCHYARD_DESIGNER_ONBOARDING"].endswith("DESIGNER_ONBOARDING.md")
        assert director_env["SWITCHYARD_PROJECT_DESIGN"] == str(project_dir / "PROJECT_DESIGN.md")
        assert director_env["SWITCHYARD_DIRECTOR_ONBOARDING"].endswith("DIRECTOR_ONBOARDING.md")
        assert len(generated_commands) == 6
        assert all(f"{expected_pane_launcher} porter pane attach-or-start" in command for command in generated_commands)
        assert not any(f"{ROOT / 'scripts' / 'team-launcher'} porter pane attach-or-start" in command for command in generated_commands)
        assert not any("switchyard porter pane attach-or-start" in command for command in generated_commands)
        assert _run_git(["git", "rev-parse", "--is-inside-work-tree"], cwd=project_dir).stdout.strip() == "true"
        assert _run_git(["git", "rev-list", "--count", "HEAD"], cwd=project_dir).stdout.strip() == "2"
        assert _run_git(["git", "remote", "get-url", "origin"], cwd=project_dir).stdout.strip() == str(project_dir)
        assert (provision_dir / "porter.json").is_file()
        assert (provision_dir / "operator-commands.sh").is_file()
        assert "local repository as a bootstrap placeholder" in designer_onboarding
        role_worktree = tmp_path / "porter-ops-worktree"
        _run_git(["git", "worktree", "add", "--detach", str(role_worktree), "HEAD"], cwd=project_dir)
        assert (role_worktree / ".git").is_file()
        assert any(call == team_launcher._owner_git_args("otto-agent", project_dir, "init", "-b", "main") for call in runner.calls)
        assert any(
            call[:6] == ["sudo", "-u", "otto-agent", "git", "-C", str(project_dir)]
            and "commit" in call
            and call[-2:] == ["-m", "Record Switchyard provisioning artifacts"]
            for call in runner.calls
        )
        assert any(call[:6] == ["sudo", "-u", "otto-agent", "git", "-C", str(project_dir)] for call in runner.calls)
        assert "not a git repository" not in runner.git_output
        assert any(call[:1] in (["env"], ["sh"]) for call in runner.calls)
        output = stdout.getvalue()
        assert "switchyard: full pane window started for porter" in output
        assert output.count("warning: switchyard: session record missing for ") == 6
        assert "warning: switchyard: session record missing for designer (porter-designer:0.0)" in output
        assert "warning: switchyard: session record missing for main (porter-main:0.0)" in output
        assert "Konsole Ctrl+Shift+E" in output


def test_switchyard_new_prompts_roles_and_skips_designer_when_absent() -> None:
    class PromptedRoleRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-roles.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        prompts: list[str] = []
        answers = iter(["n", "claude", "agy", "code-review, runtime", "agy", "codex"])
        runner = PromptedRoleRunner()
        stdout = StringIO()

        def input_func(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    slug="mycoolthing",
                    agent_name="otto",
                    project_name="My Cool Thing",
                    project_path=home_base / "otto-agent" / "Projects" / "mycoolthing",
                    source_repo=source_repo,
                    output_dir=output_dir,
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=tmp_path / "pane-state",
                    input_func=input_func,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                )
                == 0
            )

        project_dir = home_base / "otto-agent" / "Projects" / "mycoolthing"
        artifact = json.loads((project_dir / ".switchyard" / "mycoolthing.project.json").read_text(encoding="utf-8"))
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((output_dir / "mycoolthing.json").read_text(encoding="utf-8"))
        workflow_sql = (output_dir / "mycoolthing-workflow.sql").read_text(encoding="utf-8")
        rendered = stdout.getvalue()

    assert prompts == [
        "Include designer role [Y/n]: ",
        "director CLI (claude/codex/agy) [claude]: ",
        "audit CLI (claude/codex/agy) [claude]: ",
        "Implementer roles (comma-separated): ",
        "code-review CLI (claude/codex/agy) [codex]: ",
        "runtime CLI (claude/codex/agy) [codex]: ",
    ]
    assert artifact["project"]["include_designer"] is False
    assert artifact["project"]["roles"] == ["code-review", "runtime"]
    assert artifact["project"]["role_clis"] == {
        "director": "claude",
        "audit": "agy",
        "code-review": "agy",
        "runtime": "codex",
    }
    assert plan["draft_roles"] == []
    assert plan["implementer_roles"] == ["code-review", "runtime"]
    assert plan["assignee_roles"] == ["unassigned", "code-review", "runtime", "audit", "director", "user"]
    assert plan["caller_roles"] == ["director", "code-review", "runtime", "audit", "user"]
    assert [role["role"] for role in config["roles"]] == ["director", "audit", "code-review", "runtime"]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "director": ["claude"],
        "audit": ["agy"],
        "code-review": ["agy"],
        "runtime": ["codex"],
    }
    assert [role["target"] for role in config["roles"]] == [
        "mycoolthing-director:0.0",
        "mycoolthing-audit:0.0",
        "mycoolthing-code-review:0.0",
        "mycoolthing-runtime:0.0",
    ]
    assert "('draft', 'Draft', 0, ARRAY[]::text[]" in workflow_sql
    assert "('draft', 'analysis', 'release_draft', ARRAY['director', 'user']::text[]" in workflow_sql
    assert not (project_dir / "PROJECT_DESIGN.md").exists()
    assert not (project_dir / ".switchyard" / "DESIGNER_ONBOARDING.md").exists()
    assert "switchyard: design phase skipped; no designer pane configured" in rendered
    assert "maximize the designer pane" not in rendered


def test_switchyard_new_rejects_role_choices_without_required_director() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-required-roles.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        try:
            switchyard_new_command(
                slug="atlas",
                agent_name="atlas",
                project_name="Atlas Project",
                project_path=home_base / "atlas-agent" / "Projects" / "atlas-project",
                output_dir=output_dir,
                role_clis=(("audit", "claude"), ("app", "codex")),
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=FakeRunner(),
            )
            raise AssertionError("expected missing required role failure")
        except SystemExit as exc:
            message = str(exc)

    assert "required roles missing: director" in message
    assert not output_dir.exists()
    assert not (home_base / "atlas-agent" / "Projects" / "atlas-project").exists()


def test_switchyard_new_reports_project_specific_pane_state_dir_without_override() -> None:
    class NewProjectRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    captured_pane_state_dirs: list[Path | None] = []
    original_uid_for_user = team_launcher.uid_for_user
    original_report_launch_session_records = team_launcher.report_launch_session_records
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)

        def fake_report_launch_session_records(
            _config: team_launcher.ProjectConfig,
            _roles: Sequence[team_launcher.RoleConfig] | None = None,
            **kwargs: object,
        ) -> list[team_launcher.LaunchSessionRecordStatus]:
            captured_pane_state_dirs.append(kwargs.get("pane_state_dir"))  # type: ignore[arg-type]
            return []

        team_launcher.report_launch_session_records = fake_report_launch_session_records

        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-pane-state.") as tmp:
            tmp_path = Path(tmp)
            source_repo = tmp_path / "source-repo"
            source_repo.mkdir()
            assert (
                switchyard_new_command(
                    slug="porter",
                    agent_name="otto",
                    project_name="Porter System",
                    source_repo=source_repo,
                    output_dir=tmp_path / "out",
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    home_base=tmp_path / "home",
                    euid_getter=lambda: 0,
                    runner=NewProjectRunner(),
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                )
                == 0
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.report_launch_session_records = original_report_launch_session_records

    assert captured_pane_state_dirs == [Path("/run/user/1005/porter-ticket-board/pane-state")]


def test_switchyard_new_custom_project_path_sets_artifact_repository_and_pane_workdirs() -> None:
    class NewProjectRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        custom_path = tmp_path / "external" / "otto-scheduler"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        runner = NewProjectRunner()

        assert (
            switchyard_new_command(
                project_name="Otto Scheduler",
                slug="otto",
                agent_name="otto",
                project_path=custom_path,
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                pane_state_dir=tmp_path / "pane-state",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                session_record_timeout=0,
                registry_dir=tmp_path / "registry",
            )
            == 0
        )

        config = load_project_config("otto", output_dir / "otto.json")
        install_call = next(call for call in runner.calls if call[:1] == ["install"])

        assert custom_path.is_dir()
        assert json.loads((custom_path / ".switchyard" / "otto.project.json").read_text(encoding="utf-8"))["project"]["repository"] == str(custom_path)
        assert install_call[-1] == str(custom_path)
        assert config.repository == custom_path
        assert config.control_repository == Path("/home/otto-agent/.local/state/switchyard/projects/otto/control.git")
        assert {role.role: role.workdir for role in config.roles} == {
            "designer": "/home/otto-agent/otto-worktrees/designer",
            "director": "/home/otto-agent/otto-worktrees/director",
            "audit": "/home/otto-agent/otto-worktrees/audit",
            "ops": "/home/otto-agent/otto-worktrees/ops",
            "app": "/home/otto-agent/otto-worktrees/app",
            "main": "/home/otto-agent/otto-worktrees/main",
        }


def test_switchyard_new_creates_absent_zeta_owner_with_linger_and_initial_artifact() -> None:
    class ZetaRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["id", "-u", "zeta-agent"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args == ["loginctl", "enable-linger", "zeta-agent"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        runner = ZetaRunner()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    slug="zeta",
                    agent_name="zeta",
                    project_name="Zeta System",
                    source_repo=source_repo,
                    output_dir=output_dir,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=tmp_path / "pane-state",
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                )
                == 0
            )

        project_dir = home_base / "zeta-agent" / "Projects" / "zeta_system"
        expected_useradd = team_launcher._owner_project_install_args("zeta-agent", project_dir)[1]
        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")
        artifact_created = (project_dir / ".switchyard" / "zeta.project.json").is_file()

    assert expected_useradd in runner.calls
    assert "-G" not in expected_useradd
    assert ["loginctl", "enable-linger", "zeta-agent"] in runner.calls
    assert artifact_created
    assert f"switchyard: created user zeta-agent with shell fish; linger enabled" in stdout.getvalue()
    assert "sudo loginctl enable-linger 'zeta-agent'" not in commands
    assert "loginctl show-user 'zeta-agent' -p Linger --value" in commands


def test_switchyard_new_reuses_existing_owner_without_account_mutation() -> None:
    class ExistingOwnerRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.owner_marker = {
                "shell": "/usr/local/bin/project-shell",
                "groups": ("project-extra",),
            }

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["id", "-u", "marked-agent"]:
                return subprocess.CompletedProcess(args, 0, stdout="4242\n")
            if args == ["loginctl", "show-user", "marked-agent", "-p", "Linger", "--value"]:
                return subprocess.CompletedProcess(args, 0, stdout="yes\n")
            if args[:1] in (["useradd"], ["usermod"], ["chsh"]):
                raise AssertionError(f"existing owner must not be modified: {args}")
            if args == ["loginctl", "enable-linger", "marked-agent"]:
                raise AssertionError(f"existing owner linger must not be changed during account bootstrap: {args}")
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-existing.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "home" / "marked-agent" / "Projects" / "marked"
        source_repo = tmp_path / "source-repo"
        output_dir = tmp_path / "out"
        source_repo.mkdir()
        artifact_path = tmp_path / "marked.project.json"
        design_document = tmp_path / "MARKED_DESIGN.md"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": str(design_document),
                    "project": {
                        "slug": "marked",
                        "name": "Marked Project",
                        "ticket_prefix": "MARK",
                        "owner_user": "marked-agent",
                        "repository": str(project_dir),
                        "capability_grants": {
                            "board_service_traversal": True,
                            "supplementary_groups": ["project-extra"],
                            "linger": True,
                            "shell": "/usr/local/bin/project-shell",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        runner = ExistingOwnerRunner()
        marker_before = dict(runner.owner_marker)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    from_artifact=artifact_path,
                    source_repo=source_repo,
                    output_dir=output_dir,
                    yes=True,
                    home_base=tmp_path / "home",
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=tmp_path / "pane-state",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    session_record_timeout=0,
                    registry_dir=tmp_path / "registry",
                )
                == 0
            )

        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")

    assert runner.owner_marker == marker_before
    assert not any(call[:1] == ["useradd"] for call in runner.calls)
    assert not any(call[:1] in (["usermod"], ["chsh"]) for call in runner.calls)
    assert ["loginctl", "show-user", "marked-agent", "-p", "Linger", "--value"] in runner.calls
    assert ["loginctl", "enable-linger", "marked-agent"] not in runner.calls
    assert f"switchyard: using existing user marked-agent (not modifying)" in stdout.getvalue()
    assert "sudo loginctl enable-linger 'marked-agent'" not in commands
    assert "loginctl show-user 'marked-agent' -p Linger --value" in commands


def test_switchyard_new_unwritable_existing_project_path_refuses_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        project_path = tmp_path / "unwritable"
        project_path.mkdir()
        project_path.chmod(0o500)
        output_dir = tmp_path / "out"
        calls: list[list[str]] = []
        try:
            try:
                switchyard_new_command(
                    project_name="Otto Scheduler",
                    slug="otto",
                    agent_name="otto",
                    project_path=project_path,
                    output_dir=output_dir,
                    role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                    yes=True,
                    euid_getter=lambda: 0,
                    runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
                    registry_dir=tmp_path / "registry",
                )
                raise AssertionError("expected unwritable path failure")
            except SystemExit as exc:
                message = str(exc)
        finally:
            project_path.chmod(0o700)

    assert "not readable and writable by otto-agent" in message
    assert calls == []
    assert not output_dir.exists()
    assert not (project_path / ".switchyard").exists()


def test_new_project_from_handwritten_artifact_uses_same_provision_path() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        provision_dir = tmp_path / "provision"
        source_repo.mkdir()
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "remote": "origin",
                        "default_branch": "mainline",
                        "worktree_policy": "isolated",
                        "push_policy": "director-main-only",
                        "gates": {
                            "audit_signoff": True,
                            "needs_inspection": False,
                            "needs_user_signoff": True,
                        },
                        "capability_grants": {
                            "board_service_traversal": True,
                            "supplementary_groups": ["kvm"],
                            "linger": True,
                            "shell": "bash",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner()

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=provision_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((provision_dir / "atlas.json").read_text(encoding="utf-8"))
        board_unit = (provision_dir / "atlas-ticket-board.service").read_text(encoding="utf-8")

    assert plan["project"] == "atlas"
    assert plan["ticket_prefix"] == "ATL"
    assert plan["board_service_traversal"] is True
    assert config["ticket_prefix"] == "ATL"
    assert config["worktree_branch"] == "mainline"
    assert config["worktree_base"] == f"/home/{current_user}/atlas-worktrees"
    assert config["session_dir"] == f"/home/{current_user}/.local/state/atlas-ticket-board/pane-sessions"
    assert not config["session_dir"].startswith("/run/")
    assert config["control_repository"] == f"/home/{current_user}/.local/state/switchyard/projects/atlas/control.git"
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=ATL" in board_unit
    assert not any(call[:1] == ["sudo"] for call in runner.calls)


def test_new_project_artifact_board_service_traversal_false_omits_owner_home_acls() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        provision_dir = tmp_path / "provision"
        source_repo.mkdir()
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "capability_grants": {
                            "board_service_traversal": False,
                            "supplementary_groups": [],
                            "linger": True,
                            "shell": "bash",
                        },
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=provision_dir,
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        commands = (provision_dir / "operator-commands.sh").read_text(encoding="utf-8")

    assert plan["board_service_traversal"] is False
    assert "sudo setfacl -m u:boardsvc:--x" not in commands
    assert "sudo setfacl -R -m u:boardsvc:rwx" not in commands
    assert "sudo setfacl -R -m u:boardsvc:rx" not in commands
    assert "board_service_traversal=false" in commands
    assert "board health check will fail" in commands


def test_project_artifact_accepts_designer_chosen_roles() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        source_repo = tmp_path / "source-repo"
        project_repo.mkdir()
        source_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["ops", "app"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert (
            new_project_command(
                "atlas",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=tmp_path / "out",
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

        plan = json.loads((tmp_path / "out" / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((tmp_path / "out" / "atlas.json").read_text(encoding="utf-8"))

    assert plan["implementer_roles"] == ["ops", "app"]
    assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "ops", "app"]


def test_project_artifact_rejects_unknown_role_cli() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["backend"],
                        "role_clis": {"backend": "cluade"},
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_design_artifact(artifact_path)
            raise AssertionError("expected bad CLI failure")
        except SystemExit as exc:
            message = str(exc)

    assert "CLI for backend must be one of claude, codex, agy" in message


def test_project_artifact_rejects_non_bool_include_designer() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        artifact_path = tmp_path / "atlas.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "atlas-design.md",
                    "project": {
                        "slug": "atlas",
                        "name": "Atlas Project",
                        "ticket_prefix": "ATL",
                        "owner_user": current_user,
                        "repository": str(project_repo),
                        "roles": ["backend"],
                        "include_designer": "yes",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_design_artifact(artifact_path)
            raise AssertionError("expected include_designer type failure")
        except SystemExit as exc:
            message = str(exc)

    assert "field 'project.include_designer' must be a JSON boolean" in message


def _write_first_run_auth_config(tmp_path: Path, *, roles: list[tuple[str, str]], owner_user: str = "otto-agent") -> Path:
    layout_path = tmp_path / "layout.json"
    layout_path.write_text(
        json.dumps(
            {
                "Orientation": "Horizontal",
                "Widgets": [
                    {"Command": "", "SessionRestoreId": index, "WorkingDirectory": ""}
                    for index, _role in enumerate(roles)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = tmp_path / "otto.json"
    config_path.write_text(
        json.dumps(
            {
                "project": "otto",
                "layout": str(layout_path),
                "repository": str(repo),
                "run_as_user": owner_user,
                "roles": [
                    {
                        "role": role,
                        "slot": index,
                        "target": f"otto-{role}:0.0",
                        "tmux_session": f"otto-{role}",
                        "workdir": str(tmp_path / "worktrees" / role),
                        "cli": [cli],
                        "yolo": True,
                    }
                    for index, (role, cli) in enumerate(roles)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for role, _cli in roles:
        (tmp_path / "worktrees" / role).mkdir(parents=True, exist_ok=True)
    return config_path


class FirstRunAuthRunner:
    def __init__(self, *, authenticated_after_login: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.call_kwargs: list[dict[str, object]] = []
        self.authenticated_after_login = authenticated_after_login
        self.login_seen: set[str] = set()

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.call_kwargs.append(dict(kwargs))
        command = args[3:] if args[:2] == ["sudo", "-u"] else args
        if command == ["claude", "auth", "status", "--json"]:
            logged_in = "claude" in self.login_seen if self.authenticated_after_login else False
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"loggedIn": logged_in}) + "\n")
        if command == ["codex", "login", "status"]:
            if self.authenticated_after_login and "codex" in self.login_seen:
                return subprocess.CompletedProcess(args, 0, stdout="Logged in using ChatGPT\n")
            return subprocess.CompletedProcess(args, 1, stderr="Not logged in\n")
        if command == ["agy", "models"]:
            if self.authenticated_after_login and "agy" in self.login_seen:
                return subprocess.CompletedProcess(args, 0, stdout="gemini-3.7-flash-high\n")
            return subprocess.CompletedProcess(args, 1, stderr="You are not logged into Antigravity.\n")
        if command == ["claude", "auth", "login"]:
            self.login_seen.add("claude")
            return subprocess.CompletedProcess(args, 0)
        if command == ["codex", "login"]:
            self.login_seen.add("codex")
            return subprocess.CompletedProcess(args, 0)
        if command == ["agy"]:
            self.login_seen.add("agy")
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)


def test_first_run_auth_phase_is_silent_when_auth_and_trust_already_pass() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("designer", "claude"),
                    ("director", "claude"),
                    ("audit", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                ],
            ),
        )
        owner_home.joinpath(".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        str(Path(role.workdir).resolve(strict=False)): {"hasTrustDialogAccepted": True}
                        for role in config.roles
                        if role.cli == ["claude"]
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        agy_settings = owner_home / ".gemini" / "antigravity-cli" / "settings.json"
        agy_settings.parent.mkdir(parents=True)
        agy_settings.write_text(
            json.dumps(
                {
                    "trustedWorkspaces": [
                        str(Path(role.workdir).resolve(strict=False))
                        for role in config.roles
                        if role.cli == ["agy"]
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex"})
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
    ]
    assert {kwargs.get("cwd") for kwargs in runner.call_kwargs} == {str(owner_home)}


def test_first_run_auth_phase_does_not_sudo_wrap_same_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                ],
            ),
        )
        owner_home.joinpath(".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        str(Path(role.workdir).resolve(strict=False)): {"hasTrustDialogAccepted": True}
                        for role in config.roles
                        if role.cli == ["claude"]
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        agy_settings = owner_home / ".gemini" / "antigravity-cli" / "settings.json"
        agy_settings.parent.mkdir(parents=True)
        agy_settings.write_text(
            json.dumps(
                {
                    "trustedWorkspaces": [
                        str(Path(role.workdir).resolve(strict=False))
                        for role in config.roles
                        if role.cli == ["agy"]
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.update({"agy", "claude", "codex"})
        messages: list[str] = []
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "otto-agent"
            report = team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=messages.append,
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert runner.calls == [
        ["claude", "auth", "status", "--json"],
        ["codex", "login", "status"],
        ["agy", "models"],
    ]
    assert {kwargs.get("cwd") for kwargs in runner.call_kwargs} == {str(owner_home)}


def test_first_run_auth_phase_sequences_distinct_logins_and_skips_visible_worktree_trust() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path,
                roles=[
                    ("designer", "claude"),
                    ("director", "claude"),
                    ("ops", "codex"),
                    ("inspector", "agy"),
                    ("main", "codex"),
                ],
            ),
        )
        runner = FirstRunAuthRunner()
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {}
    assert report.untrusted_roles == []
    assert runner.calls == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "login"],
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "codex", "login"],
        ["sudo", "-u", "otto-agent", "codex", "login", "status"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
        ["sudo", "-u", "otto-agent", "agy"],
        ["sudo", "-u", "otto-agent", "agy", "models"],
    ]
    assert [kwargs.get("cwd") for kwargs in runner.call_kwargs] == [str(owner_home)] * 9
    assert not (owner_home / ".claude.json").exists()
    assert not (owner_home / ".gemini" / "antigravity-cli" / "settings.json").exists()
    assert messages[:3] == [
        "switchyard: first-run claude login required for designer, director; running claude auth login as otto-agent",
        "switchyard: first-run codex login required for ops, main; running codex login as otto-agent",
        "switchyard: first-run agy login required for inspector; running agy as otto-agent",
    ]
    assert messages[3:] == []


def test_first_run_trust_handles_detached_roles_and_persists_for_later_launches() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-trust-repeat.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config_path = _write_first_run_auth_config(tmp_path, roles=[("research", "claude")])
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["roles"][0]["detached"] = True
        raw["roles"][0].pop("slot", None)
        config_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)

        calls: list[list[str]] = []
        call_kwargs: list[dict[str, object]] = []

        def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            call_kwargs.append(dict(kwargs))
            command = args[3:] if args[:2] == ["sudo", "-u"] else args
            if command == ["claude", "auth", "status", "--json"]:
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"loggedIn": True}) + "\n")
            if command == ["claude"]:
                cwd = str(kwargs["cwd"])
                claude_config = owner_home / ".claude.json"
                try:
                    payload = json.loads(claude_config.read_text(encoding="utf-8"))
                except OSError:
                    payload = {}
                projects = payload.setdefault("projects", {})
                projects[cwd] = {"hasTrustDialogAccepted": True}
                claude_config.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 1, stderr="unexpected command\n")

        first_messages: list[str] = []
        first_report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=first_messages.append,
        )
        calls_after_first = list(calls)
        second_messages: list[str] = []
        second_report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=second_messages.append,
        )

    assert first_report == team_launcher.FirstRunAuthReport({}, [])
    assert second_report == team_launcher.FirstRunAuthReport({}, [])
    assert calls_after_first == [
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
        ["sudo", "-u", "otto-agent", "claude"],
    ]
    assert calls == [
        *calls_after_first,
        ["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"],
    ]
    assert call_kwargs[1].get("cwd") == str(tmp_path / "worktrees" / "research")
    assert len(first_messages) == 1
    assert all("folder trust required" in message and "not account login" in message for message in first_messages)
    assert second_messages == []


def test_first_run_trust_matches_configured_symlink_path_without_reprompting() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-trust-path.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        target = tmp_path / "real-worktree"
        target.mkdir()
        link = tmp_path / "linked-worktree"
        link.symlink_to(target, target_is_directory=True)
        config_path = _write_first_run_auth_config(tmp_path, roles=[("director", "claude")])
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["roles"][0]["detached"] = True
        raw["roles"][0].pop("slot", None)
        raw["roles"][0]["workdir"] = str(link)
        config_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)
        owner_home.joinpath(".claude.json").write_text(
            json.dumps({"projects": {str(link): {"hasTrustDialogAccepted": True}}}) + "\n",
            encoding="utf-8",
        )
        runner = FirstRunAuthRunner()
        runner.login_seen.add("claude")
        messages: list[str] = []

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report == team_launcher.FirstRunAuthReport({}, [])
    assert messages == []
    assert runner.calls == [["sudo", "-u", "otto-agent", "claude", "auth", "status", "--json"]]


def test_first_run_auth_phase_declined_login_reports_affected_roles_without_aborting() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-first-run-auth.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(tmp_path, roles=[("ops", "codex"), ("main", "codex")]),
        )
        messages: list[str] = []
        runner = FirstRunAuthRunner(authenticated_after_login=False)

        report = team_launcher.run_first_run_auth_phase(
            config,
            owner_user="otto-agent",
            owner_home=owner_home,
            runner=runner,
            print_func=messages.append,
        )

    assert report.unauthenticated_roles == {"codex": ["ops", "main"]}
    assert report.untrusted_roles == []
    output: list[str] = []
    team_launcher.report_first_run_auth_warnings(report, print_func=output.append)
    assert output == ["warning: switchyard: codex is still unauthenticated; affected roles: ops, main"]


def test_project_artifact_rejects_stage_configuration() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        source_repo = tmp_path / "source-repo"
        project_repo.mkdir()
        source_repo.mkdir()
        artifact_path = tmp_path / "bad.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "bad-design.md",
                    "project": {
                        "slug": "bad",
                        "ticket_prefix": "BAD",
                        "owner_user": team_launcher.current_user_name(),
                        "repository": str(project_repo),
                        "stages": ["analysis", "implementation"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            new_project_command(
                "bad",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=tmp_path / "out",
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected artifact stage-shaping rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "must not preconfigure stages or roles" in message
    assert "project.stages" in message


def test_new_project_requires_project_repository() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "source-repo",
                output_dir=output_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected missing repository failure")
        except SystemExit as exc:
            message = str(exc)

    assert "--repository" in message
    assert not output_dir.exists()
    assert not runner.calls


def test_new_project_precheck_fails_when_project_repository_is_absent() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "source-repo",
                repository=tmp_path / "missing-project-repo",
                output_dir=output_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected absent repository precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "project repository" in message
    assert "does not exist" in message
    assert not output_dir.exists()
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def test_new_project_precheck_fails_before_sudo_when_repo_is_dirty() -> None:
    current_user = team_launcher.current_user_name()

    class DirtyRepoRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout=" M scripts/team_launcher.py\n")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = DirtyRepoRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected dirty repo precheck failure")
        except SystemExit as exc:
            message = str(exc)
        assert not output_dir.exists()

    assert "deploy checkout" in message
    assert "uncommitted changes" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def test_new_project_precheck_ignores_untracked_checkout_junk() -> None:
    current_user = team_launcher.current_user_name()

    class UntrackedOnlyRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                assert args[-1] == "--untracked-files=no"
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="",
                )
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        (source_repo / ".claude").mkdir()
        (source_repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        (source_repo / "scripts" / "__pycache__").mkdir(parents=True)
        (source_repo / "scripts" / "ticket_board" / "__pycache__").mkdir(parents=True)
        runner = UntrackedOnlyRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        assert any(
            call[:3] == ["git", "-C", str(source_repo)] and call[3:] == ["status", "--porcelain", "--untracked-files=no"]
            for call in runner.calls
        )


def test_git_status_porcelain_ignores_untracked_junk_but_reports_tracked_edits() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-status.") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _run_git(["git", "init", "-b", "main"], cwd=repo)
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=repo)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=repo)
        tracked = repo / "tracked.txt"
        tracked.write_text("clean\n", encoding="utf-8")
        _run_git(["git", "add", "tracked.txt"], cwd=repo)
        _run_git(["git", "commit", "-m", "initial"], cwd=repo)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        (repo / "scripts" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "ticket_board" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "__pycache__" / "team_launcher.cpython-313.pyc").write_bytes(b"pyc\n")
        (repo / "scripts" / "ticket_board" / "__pycache__" / "project_provision.cpython-313.pyc").write_bytes(
            b"pyc\n"
        )

        plain_status = _run_git(["git", "status", "--porcelain"], cwd=repo).stdout
        clean_status = team_launcher._git_status_porcelain(repo, runner=subprocess.run)
        tracked.write_text("dirty\n", encoding="utf-8")
        dirty_status = team_launcher._git_status_porcelain(repo, runner=subprocess.run)

    assert "?? .claude/" in plain_status
    assert "?? scripts/" in plain_status
    assert clean_status == ""
    assert dirty_status == " M tracked.txt"


def test_gitignore_hides_launcher_generated_junk() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-gitignore.") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _run_git(["git", "init", "-b", "main"], cwd=repo)
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=repo)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=repo)
        (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
        _run_git(["git", "add", ".gitignore"], cwd=repo)
        _run_git(["git", "commit", "-m", "initial"], cwd=repo)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        (repo / "scripts" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "__pycache__" / "team_launcher.cpython-313.pyc").write_bytes(b"pyc\n")

        status = _run_git(["git", "status", "--porcelain"], cwd=repo).stdout

    assert status == ""


def test_database_exists_uses_postgres_os_user_when_running_as_root() -> None:
    calls: list[list[str]] = []
    original_geteuid = team_launcher.os.geteuid

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="1\n")

    try:
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        assert team_launcher._database_exists("porter_board", runner=runner) is True
    finally:
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [
        [
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-XAt",
            "postgresql:///postgres?host=/var/run/postgresql",
            "-c",
            "SELECT 1 FROM pg_database WHERE datname = 'porter_board'",
        ]
    ]
    assert "user=postgres" not in calls[0][5]


def test_database_exists_keeps_non_root_psql_path_unchanged() -> None:
    calls: list[list[str]] = []
    original_geteuid = team_launcher.os.geteuid

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="")

    try:
        team_launcher.os.geteuid = lambda: 1001  # type: ignore[method-assign]
        assert team_launcher._database_exists("porter_board", runner=runner) is False
    finally:
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [
        [
            "psql",
            "-XAt",
            "postgresql:///postgres?host=/var/run/postgresql",
            "-c",
            "SELECT 1 FROM pg_database WHERE datname = 'porter_board'",
        ]
    ]


def test_ticket_board_table_count_uses_target_database_and_postgres_os_user_when_root() -> None:
    calls: list[list[str]] = []
    original_geteuid = team_launcher.os.geteuid

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="12\n")

    try:
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        assert team_launcher._ticket_board_table_count("porter_ticket_board", runner=runner) == 12
    finally:
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [
        [
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-XAt",
            "postgresql:///porter_ticket_board?host=/var/run/postgresql",
            "-c",
            "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'",
        ]
    ]


def test_system_unit_successful_empty_listing_means_unit_absent() -> None:
    original_path_exists = team_launcher._path_exists

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["systemctl", "list-unit-files", "--no-legend", "porter-ticket-board.service"]
        return subprocess.CompletedProcess(args, 0, stdout="")

    try:
        team_launcher._path_exists = lambda _path: True  # type: ignore[assignment]
        assert team_launcher._system_unit_file_exists("porter-ticket-board.service", runner=runner) is False
    finally:
        team_launcher._path_exists = original_path_exists  # type: ignore[assignment]


def test_new_project_execute_warms_sudo_once_then_runs_generated_script() -> None:
    current_user = team_launcher.current_user_name()

    class RecordingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.call_kwargs: list[dict[str, object]] = []

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.call_kwargs.append(dict(kwargs))
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = RecordingRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

    sudo_index = next(index for index, call in enumerate(runner.calls) if call == ["sudo", "-v"])
    bash_index = next(index for index, call in enumerate(runner.calls) if call[:1] == ["bash"])
    assert sudo_index < bash_index
    assert runner.calls[bash_index] == ["bash", str(output_dir / "operator-commands.sh")]
    assert runner.call_kwargs[bash_index]["cwd"] == str(output_dir)


def test_new_project_rerun_rejects_fully_provisioned_project_before_mutating() -> None:
    current_user = team_launcher.current_user_name()

    class FullyProvisionedRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service enabled\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///postgres?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="1\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///porter_ticket_board?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="12\n")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = FullyProvisionedRunner()

        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                execute=True,
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: True,
            )
            raise AssertionError("expected fully provisioned precheck failure")
        except SystemExit as exc:
            message = str(exc)

        assert not (tmp_path / "out").exists()

    assert "project 'porter' is already provisioned" in message
    assert "database porter_ticket_board has 12 ticket_board tables" in message
    assert "porter-ticket-board.service is installed" in message
    assert "to launch it:      switchyard porter" in message
    assert "to start over:" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def test_new_project_rerun_allows_installed_unit_with_empty_database_recovery_path() -> None:
    current_user = team_launcher.current_user_name()

    class EmptyDatabaseRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service enabled\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///postgres?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="1\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///porter_ticket_board?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="0\n")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = EmptyDatabaseRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: True,
            )
            == 0
        )


def test_new_project_rejects_installed_unit_without_database() -> None:
    current_user = team_launcher.current_user_name()

    class ExistingUnitNoDatabaseRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service disabled\n")
            if args[:2] == ["psql", "-XAt"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = ExistingUnitNoDatabaseRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected unit-without-database precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "porter-ticket-board.service is installed" in message
    assert "database 'porter_ticket_board' does not exist" in message
    assert not output_dir.exists()
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def test_new_project_rejects_port_collision_before_mutating() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected port collision precheck failure")
        except SystemExit as exc:
            message = str(exc)
        assert not output_dir.exists()

    assert "port 23682 is already in use" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def main() -> int:
    live_session_snapshot = snapshot_paths(LIVE_PANE_SESSION_PATHS)
    live_pane_state_snapshot = snapshot_paths_file_set(LIVE_PANE_STATE_PATHS)
    live_pane_state_anomalies = pane_state_record_anomalies(LIVE_PANE_STATE_PATHS)
    live_session_fingerprint = _live_session_record_fingerprint()
    try:
        run_module_tests(
            globals(),
            first=("test_ticket_board_pane_env_is_stripped_before_launcher_import",),
        )
    finally:
        changed = [
            *snapshot_diff(live_session_snapshot, snapshot_paths(LIVE_PANE_SESSION_PATHS)),
            *snapshot_file_set_diff(live_pane_state_snapshot, snapshot_paths_file_set(LIVE_PANE_STATE_PATHS)),
            *pane_state_record_anomaly_diff(
                live_pane_state_anomalies,
                pane_state_record_anomalies(LIVE_PANE_STATE_PATHS),
            ),
        ]
        assert not changed, changed
        _assert_live_session_records_unchanged(live_session_fingerprint, _live_session_record_fingerprint())
    print("team_launcher_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
