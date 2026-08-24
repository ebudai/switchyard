#!/usr/bin/env python3
"""Regression tests for the JSON-driven team launcher."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

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
    worktree_ref,
)

LIVE_SESSION_DIR = team_launcher.DEFAULT_SESSION_DIR


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


def _write_pgu_config_with_shared_checkout(tmp: Path) -> Path:
    source = json.loads((ROOT / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    source["layout"] = str(ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json")
    source["repository"] = str(tmp / "repo")
    source["worktree_base"] = str(tmp / "worktrees")
    source["session_dir"] = str(tmp / "sessions")
    config_path = tmp / "pgu.json"
    config_path.write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True)


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


def _write_minimal_shared_checkout_config(tmp: Path, repo: Path, roles: list[str]) -> Path:
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
        for role in ("director", "main", "app", "ops", "audit", "inspector"):
            matching = [command for command in commands if f" {role} " in f" {command} " or command.endswith(f" {role}")]
            assert matching, (role, commands)
            assert " pane attach-or-start " in matching[0]
            assert "team-launcher" in matching[0]
        for role, command in zip(expected_clockwise_roles, clockwise_commands):
            assert f" {role} " in f" {command} ", (role, clockwise_commands)
        assert not any(" research " in f" {command} " for command in commands)
        assert not any(" perf " in f" {command} " for command in commands)


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
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
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
    launcher_repo = Path("/home/user/Projects/pgu")
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
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.uid_for_user = lambda _user_name: None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "QT_LOGGING_RULES=qt.qpa.wayland.warning=false",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
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
            "QT_LOGGING_RULES=qt.qpa.wayland.warning=false",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
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


def test_konsole_launch_defaults_to_invoking_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        team_launcher.current_user_name = lambda: "otto-agent"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "otto-agent" else None

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "QT_LOGGING_RULES=qt.qpa.wayland.warning=false",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
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
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name


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
    assert runner.calls[2] == ["tmux", "attach", "-t", "pgu-ops"]


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
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
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
        (durable / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
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
    assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={durable}" in runner.calls[1][-1]
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
        assert runner.calls[5] == git_checkout_shared_ref_args(config)
        assert runner.calls[6] == git_clean_shared_checkout_args(config)
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
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": session_id}) + "\n",
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
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
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


def test_reload_uses_recorded_resume_uuid_when_recreating_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        session_id = "12345678-1234-5678-9abc-def012345678"
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id}) + "\n",
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


def test_reload_logs_resume_fallback_when_cli_never_starts() -> None:
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
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": session_id}) + "\n",
                encoding="utf-8",
            )
            sidecar_path = (session_dir / session_file_name(role.target)).with_name(
                f"{session_file_name(role.target)}.superseded"
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
                state = _read_pane_state(pane_state_dir, role.target)
                assert state["state"] == "idle"
                assert state["source"] == "team_launcher.reload.fallback"
                assert not (session_dir / session_file_name(role.target)).exists()
                assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == session_id

        assert f"team-launcher: resume failed for ops using session {session_id}; falling back to fresh session" in stderr.getvalue()
        assert "team-launcher: started fresh session for ops after resume fallback" in stderr.getvalue()
        new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]]
        assert len(new_sessions) == 2
        assert f"codex resume {session_id}" in new_sessions[0][-1]
        assert " resume " not in f" {new_sessions[1][-1]} "
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
            subprocess.run(
                [
                    "tmux",
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
            pane_command = subprocess.run(
                ["tmux", "display-message", "-p", "-t", role.target, "#{pane_current_command}"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()
            assert pane_command in {"sh", "fish", "bash", "zsh"}, pane_command
            assert live_command_matches_role(role)
        finally:
            subprocess.run(["tmux", "kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
    assert [role["role"] for role in config["roles"]] == ["director", "audit", "ops"]
    assert [role["tmux_session"] for role in config["roles"]] == [
        "porter-director",
        "porter-audit",
        "porter-ops",
    ]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "director": ["claude"],
        "audit": ["claude"],
        "ops": ["codex"],
    }
    assert len(team_launcher._layout_leaves(layout)) == 3
    assert [role.role for role in loaded_config.roles] == ["director", "audit", "ops"]
    assert loaded_config.roles[0].target == "porter-director:0.0"
    assert loaded_config.roles[2].target == "porter-ops:0.0"
    assert all(role.workdir == str(project_repo) for role in loaded_config.roles)
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
        "porter-director:0.0",
        "porter-audit:0.0",
        "porter-ops:0.0",
    ]
    assert [role["workdir"] for role in launch_plan["roles"]] == [str(project_repo)] * 3
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
    assert config["worktree_remote"] == "upstream"
    assert config["worktree_branch"] == "trunk"
    assert "worktree_base" not in config
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

        assert switchyard_menu_command(config_dir=tmp_path, print_func=lines.append) == 0

    assert lines == ["new...", "Porter System (porter)"]


def test_switchyard_project_name_argv_joins_and_resumes_matching_project() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-open.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "project_name": "My Project Name",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[dict[str, object]] = []
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        original_launch_project = team_launcher.launch_project
        try:
            team_launcher.DEFAULT_CONFIG_DIR = tmp_path

            def fake_launch_project(config: team_launcher.ProjectConfig, **kwargs: object) -> int:
                calls.append({"project": config.project, **kwargs})
                return 0

            team_launcher.launch_project = fake_launch_project
            assert switchyard_main(["my", "project", "name"]) == 0
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir
            team_launcher.launch_project = original_launch_project

    assert calls
    assert calls[0]["project"] == "porter"
    assert calls[0]["config_path"] == config_path
    assert calls[0]["mode"] == "start"


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
    assert not (home_base / "otto-agent" / "Projects" / "porter-system").exists()
    assert calls == []


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
                input_func=input_func,
                print_func=output.append,
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    expected_path = home_base / "otto-scheduler-agent" / "Projects" / "otto-scheduler"
    assert "requires sudo" in message
    assert prompts == [
        "Project name: ",
        "Slug [otto-scheduler]: ",
        "Agent name [otto-scheduler]: ",
        f"Project path [{expected_path}]: ",
    ]
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
                input_func=lambda _prompt: "",
                print_func=output.append,
            )
            raise AssertionError("expected sudo precheck failure")
        except SystemExit as exc:
            message = str(exc)

    expected_path = home_base / "otto-agent" / "Projects" / "otto-scheduler"
    assert "requires sudo" in message
    assert "switchyard: slug: otto" in output
    assert f"switchyard: project path: {expected_path}" in output


def test_switchyard_designer_session_cwd_guard_accepts_expected_session_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-session-cwd.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        project_dir = owner_home / "Projects" / "porter"
        session_dir = team_launcher._claude_project_session_dir(owner_home, project_dir)

        assert (
            team_launcher._verify_designer_session_cwd(
                owner_home=owner_home,
                project_dir=project_dir,
                exists=lambda path: path == session_dir,
            )
            == session_dir
        )


def test_switchyard_designer_session_cwd_guard_rejects_missing_session_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-session-cwd.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        project_dir = owner_home / "Projects" / "porter"
        session_dir = team_launcher._claude_project_session_dir(owner_home, project_dir)

        try:
            team_launcher._verify_designer_session_cwd(
                owner_home=owner_home,
                project_dir=project_dir,
                exists=lambda _path: False,
            )
            raise AssertionError("expected missing designer session cwd failure")
        except SystemExit as exc:
            message = str(exc)

    assert str(session_dir) in message
    assert str(project_dir) in message
    assert "would not resume" in message


def test_switchyard_new_launches_designer_from_derived_cwd_and_starts_director() -> None:
    class NewProjectRunner(FakeRunner):
        def __init__(self, home_base: Path) -> None:
            super().__init__()
            self.home_base = home_base
            self.call_kwargs: list[dict[str, object]] = []
            self.designer_cwd = ""

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.call_kwargs.append(dict(kwargs))
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:5] == ["sudo", "-u", "otto-agent", "-H", "env"] and args[-1] == "claude":
                cwd = Path(str(kwargs["cwd"]))
                self.designer_cwd = str(cwd)
                env_values = {
                    item.split("=", 1)[0]: item.split("=", 1)[1]
                    for item in args
                    if item.startswith("SWITCHYARD_") and "=" in item
                }
                artifact_path = Path(env_values["SWITCHYARD_PROJECT_ARTIFACT"])
                design_doc = Path(env_values["SWITCHYARD_PROJECT_DESIGN"])
                design_doc.write_text("# Porter System\n\nDesigned.\n", encoding="utf-8")
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    json.dumps(
                        {
                            "schema": "switchyard.project.v1",
                            "design_document": str(design_doc),
                            "project": {
                                "slug": "porter",
                                "name": "Porter System",
                                "ticket_prefix": "PORT",
                                "owner_user": "otto-agent",
                                "repository": str(cwd),
                                "roles": ["ops", "app"],
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                session_dir = team_launcher._claude_project_session_dir(self.home_base / "otto-agent", cwd)
                session_dir.mkdir(parents=True, exist_ok=True)
                (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        output_dir = tmp_path / "out"
        pane_state_dir = tmp_path / "pane-state"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        runner = NewProjectRunner(home_base)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                switchyard_new_command(
                    slug="porter",
                    agent_name="otto",
                    project_name="Porter System",
                    source_repo=source_repo,
                    output_dir=output_dir,
                    yes=True,
                    home_base=home_base,
                    euid_getter=lambda: 0,
                    runner=runner,
                    pane_state_dir=pane_state_dir,
                    input_func=lambda _prompt: "",
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )

        project_dir = home_base / "otto-agent" / "Projects" / "porter-system"
        designer_call_index = next(
            index for index, call in enumerate(runner.calls) if call[:5] == ["sudo", "-u", "otto-agent", "-H", "env"] and call[-1] == "claude"
        )
        chown_call_index = next(index for index, call in enumerate(runner.calls) if call[:2] == ["chown", "-R"])
        config = json.loads((output_dir / "porter.json").read_text(encoding="utf-8"))
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        director_env = next(role["env"] for role in config["roles"] if role["role"] == "director")
        session_dir = home_base / "otto-agent" / ".claude" / "projects" / team_launcher._claude_project_key(project_dir)

        assert runner.designer_cwd == str(project_dir)
        assert chown_call_index < designer_call_index
        assert session_dir.is_dir()
        assert plan["implementer_roles"] == ["ops", "app"]
        assert [role["role"] for role in config["roles"]] == ["director", "audit", "ops", "app"]
        assert director_env["SWITCHYARD_PROJECT_DESIGN"] == str(project_dir / "PROJECT_DESIGN.md")
        assert director_env["SWITCHYARD_DIRECTOR_ONBOARDING"].endswith("DIRECTOR_ONBOARDING.md")
        assert any(call[:2] == ["tmux", "new-session"] and "porter-director" in call for call in runner.calls)
        assert "switchyard: verified designer session cwd" in stdout.getvalue()


def test_switchyard_new_custom_project_path_sets_designer_cwd_and_pane_workdirs() -> None:
    class NewProjectRunner(FakeRunner):
        def __init__(self, home_base: Path) -> None:
            super().__init__()
            self.home_base = home_base
            self.designer_cwd = ""

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:5] == ["sudo", "-u", "otto-agent", "-H", "env"] and args[-1] == "claude":
                cwd = Path(str(kwargs["cwd"]))
                self.designer_cwd = str(cwd)
                env_values = {
                    item.split("=", 1)[0]: item.split("=", 1)[1]
                    for item in args
                    if item.startswith("SWITCHYARD_") and "=" in item
                }
                artifact_path = Path(env_values["SWITCHYARD_PROJECT_ARTIFACT"])
                design_doc = Path(env_values["SWITCHYARD_PROJECT_DESIGN"])
                design_doc.write_text("# Otto Scheduler\n\nDesigned.\n", encoding="utf-8")
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    json.dumps(
                        {
                            "schema": "switchyard.project.v1",
                            "design_document": str(design_doc),
                            "project": {
                                "slug": "otto",
                                "name": "Otto Scheduler",
                                "ticket_prefix": "OTTO",
                                "owner_user": "otto-agent",
                                "repository": str(cwd),
                                "roles": ["ops"],
                            },
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                session_dir = team_launcher._claude_project_session_dir(self.home_base / "otto-agent", cwd)
                session_dir.mkdir(parents=True, exist_ok=True)
                (session_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new.") as tmp:
        tmp_path = Path(tmp)
        home_base = tmp_path / "home"
        custom_path = tmp_path / "external" / "otto-scheduler"
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        runner = NewProjectRunner(home_base)

        assert (
            switchyard_new_command(
                project_name="Otto Scheduler",
                slug="otto",
                agent_name="otto",
                project_path=custom_path,
                source_repo=source_repo,
                output_dir=output_dir,
                yes=True,
                home_base=home_base,
                euid_getter=lambda: 0,
                runner=runner,
                pane_state_dir=tmp_path / "pane-state",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

        config = load_project_config("otto", output_dir / "otto.json")
        install_call = next(call for call in runner.calls if call[:1] == ["install"])

        assert custom_path.is_dir()
        assert runner.designer_cwd == str(custom_path)
        assert install_call[-1] == str(custom_path)
        assert {role.role: role.workdir for role in config.roles} == {
            "director": str(custom_path),
            "audit": str(custom_path),
            "ops": str(custom_path),
        }


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
                    yes=True,
                    euid_getter=lambda: 0,
                    runner=lambda args, **_kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0),
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
    assert config["ticket_prefix"] == "ATL"
    assert config["worktree_branch"] == "mainline"
    assert config["worktree_base"] == f"/home/{current_user}/atlas-worktrees"
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=ATL" in board_unit
    assert not any(call[:1] == ["sudo"] for call in runner.calls)


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
    assert [role["role"] for role in config["roles"]] == ["director", "audit", "ops", "app"]


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
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = DirtyRepoRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "repo",
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


def test_new_project_precheck_ignores_python_bytecode_created_by_cli_imports() -> None:
    current_user = team_launcher.current_user_name()

    class BytecodeOnlyRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="?? scripts/__pycache__/\n?? scripts/ticket_board/__pycache__/project_provision.cpython-313.pyc\n",
                )
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = BytecodeOnlyRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "repo",
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )


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
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = RecordingRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "repo",
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


def test_new_project_rerun_allows_existing_same_slug_resources() -> None:
    current_user = team_launcher.current_user_name()

    class ExistingUnitRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service enabled\n")
            if args[:2] == ["psql", "-XAt"]:
                return subprocess.CompletedProcess(args, 0, stdout="1\n")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = ExistingUnitRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "repo",
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: True,
            )
            == 0
        )


def test_new_project_rejects_port_collision_before_mutating() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "repo",
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
