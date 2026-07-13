#!/usr/bin/env python3
"""Regression tests for the JSON-driven team launcher."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.team_launcher as team_launcher
from scripts.team_launcher import (
    cli_command_for_role,
    ensure_project_worktrees,
    git_clean_shared_checkout_args,
    git_checkout_shared_ref_args,
    git_fetch_worktree_ref_args,
    git_shared_checkout_check_args,
    konsole_launch_args,
    launch_project,
    live_command_matches_role,
    load_project_config,
    run_detached_role,
    run_role_pane,
    session_file_name,
    worktree_ref,
    write_template,
)


class FakeRunner:
    def __init__(
        self,
        existing_sessions: set[str] | None = None,
        *,
        current_commands: dict[str, str] | None = None,
    ) -> None:
        self.existing_sessions = set(existing_sessions or set())
        self.current_commands = current_commands or {}
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["tmux", "has-session"]:
            session = args[-1]
            return subprocess.CompletedProcess(args, 0 if session in self.existing_sessions else 1)
        if args[:3] == ["tmux", "display-message", "-p"]:
            target = args[args.index("-t") + 1]
            command = self.current_commands.get(target, "")
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


def _write_pgu_config_with_shared_checkout(tmp: Path) -> Path:
    source = json.loads((ROOT / "config" / "team-launcher" / "pgu.json").read_text(encoding="utf-8"))
    source["layout"] = str(ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json")
    source["repository"] = str(tmp / "repo")
    source["worktree_base"] = str(tmp / "worktrees")
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
        for role in ("director", "main", "app", "ops", "audit", "inspector"):
            matching = [command for command in commands if f" {role} " in f" {command} " or command.endswith(f" {role}")]
            assert matching, (role, commands)
            assert " pane attach-or-start " in matching[0]
            assert "team-launcher" in matching[0]
        assert not any(" research " in f" {command} " for command in commands)
        assert not any(" perf " in f" {command} " for command in commands)


def test_pgu_config_matches_director_supplied_live_role_assignments() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}

    assert set(roles) == {"director", "main", "app", "research", "ops", "audit", "inspector"}
    assert all(role.yolo for role in roles.values())
    assert config.repository == Path("/home/agent/Projects/pgu")
    assert config.worktree_base is None
    assert worktree_ref(config) == "origin/main"
    assert roles["research"].detached
    assert roles["research"].slot is None
    assert {role.role: role.slot for role in roles.values() if not role.detached} == {
        "director": 0,
        "main": 1,
        "app": 2,
        "ops": 3,
        "audit": 4,
        "inspector": 5,
    }
    for role_name, role in roles.items():
        assert role.workdir == "/home/agent/Projects/pgu", role_name
    assert (roles["director"].cli, roles["director"].model, roles["director"].effort) == (
        ["claude"],
        "claude-opus-4-8",
        "high",
    )
    assert (roles["ops"].cli, roles["ops"].model, roles["ops"].effort, roles["ops"].extra_args) == (
        ["codex"],
        "gpt-5.5",
        "high",
        [],
    )
    assert (roles["main"].cli, roles["main"].model, roles["main"].effort, roles["main"].extra_args) == (
        ["codex"],
        "gpt-5.6-terra",
        "high",
        [],
    )
    assert (roles["app"].cli, roles["app"].model, roles["app"].effort, roles["app"].extra_args) == (
        ["codex"],
        "gpt-5.4",
        "high",
        [],
    )
    assert (roles["research"].cli, roles["research"].model, roles["research"].effort) == (
        ["claude"],
        "claude-sonnet-5",
        "high",
    )
    assert (roles["audit"].cli, roles["audit"].model, roles["audit"].effort) == (
        ["claude"],
        "claude-sonnet-5",
        "high",
    )
    assert (roles["inspector"].cli, roles["inspector"].model, roles["inspector"].effort) == (
        ["agy"],
        "Gemini 3.5 Flash (Medium)",
        "",
    )


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
        "director": ["claude", "--model", "claude-opus-4-8", "--effort", "high", "--dangerously-skip-permissions"],
        "main": [
            "codex",
            "--model",
            "gpt-5.6-terra",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "app": [
            "codex",
            "--model",
            "gpt-5.4",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "research": ["claude", "--model", "claude-sonnet-5", "--effort", "high", "--dangerously-skip-permissions"],
        "ops": [
            "codex",
            "--model",
            "gpt-5.5",
            "-c",
            "reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
        ],
        "audit": ["claude", "--model", "claude-sonnet-5", "--effort", "high", "--dangerously-skip-permissions"],
        "inspector": ["agy", "--model", "Gemini 3.5 Flash (Medium)", "--dangerously-skip-permissions"],
    }

    for role_name, expected_tail in expected_by_role.items():
        command = cli_command_for_role(roles[role_name], session_dir=config.session_dir)
        assert command[:2] == ["env", f"PGU_PANE_TARGET=pgu-{role_name}:0.0"]
        assert command[2:] == expected_tail, (role_name, command)
        assert "--reasoning-effort" not in command
        assert "--continue" not in command
        assert "--last" not in command
        assert "gemini" not in command
        assert "--yolo" not in command


def test_konsole_launch_uses_gui_user_display_environment() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    try:
        os.environ["PGU_HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        team_launcher.uid_for_user = lambda _user_name: None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="eric") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland


def test_konsole_launch_can_fallback_to_gui_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_wayland_name = os.environ.get("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY")
    try:
        os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "eric" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="eric") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_wayland_name is None:
            os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name


def test_konsole_launch_falls_back_to_clear_error_when_gui_user_missing() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    try:
        team_launcher.uid_for_user = lambda _user_name: None

        command = konsole_launch_args(Path("/tmp/layout.json"), gui_user="nobody")
        assert command[:2] == ["sh", "-lc"]
        assert "no host Wayland display" in command[2]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_host_wayland


def test_start_is_attach_or_start_and_never_duplicates_existing_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"})

    assert run_role_pane(role, mode="start", session_dir=config.session_dir, runner=runner) == 0

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "attach", "-t", "pgu-ops"],
    ]


def test_start_creates_missing_session_once_then_attaches() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner()

    assert run_role_pane(role, mode="start", session_dir=config.session_dir, runner=runner) == 0

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert runner.calls[1][5:7] == ["-c", "/home/agent/Projects/pgu"]
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "--model gpt-5.5" in runner.calls[1][-1]
    assert "-c reasoning_effort=high" in runner.calls[1][-1]
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.calls[1][-1]
    assert "--dangerously-bypass-hook-trust" in runner.calls[1][-1]
    assert "--reasoning-effort" not in runner.calls[1][-1]
    assert runner.calls[2] == ["tmux", "attach", "-t", "pgu-ops"]


def test_start_runs_research_detached_before_opening_visible_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner()
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
            )
            == 0
        )

        assert runner.calls[0] == git_fetch_worktree_ref_args(config)
        assert runner.calls[1] == git_shared_checkout_check_args(config)
        assert runner.calls[2] == git_checkout_shared_ref_args(config)
        assert runner.calls[3] == git_clean_shared_checkout_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        research_has_session_index = runner.calls.index(["tmux", "has-session", "-t", "pgu-research"])
        research_new_session = runner.calls[research_has_session_index + 1]
        assert research_new_session[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"]
        assert research_new_session[5:7] == ["-c", str(tmp_path / "repo")]
        assert "PGU_PANE_TARGET=pgu-research:0.0" in research_new_session[-1]
        assert "--model claude-sonnet-5" in research_new_session[-1]
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {str(tmp_path / "repo")}
        assert runner.calls[-1] == konsole_launch_args(layout_output)
        assert ["tmux", "attach", "-t", "pgu-research"] not in runner.calls


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
        runner = FakeRunner()
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="reload",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
            )
            == 0
        )

        assert runner.calls[0] == git_fetch_worktree_ref_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        assert git_checkout_shared_ref_args(config) not in runner.calls
        assert git_clean_shared_checkout_args(config) not in runner.calls


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
            "/home/agent/Projects/pgu"
        }
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        commands = _leaf_commands(layout)
        assert len(commands) == 6


def test_detached_research_attach_checks_session_without_attaching() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    runner = FakeRunner(existing_sessions={"pgu-research"})

    assert run_detached_role(role, mode="attach", session_dir=config.session_dir, runner=runner) == 0

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

        assert cli_command_for_role(role, session_dir=session_dir, resume=True)[-2:] == ["--resume", session_id]
        assert run_role_pane(role, mode="reload", session_dir=session_dir, runner=runner) == 0

        assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
        assert runner.calls[1] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
        assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
        assert runner.calls[3] == ["tmux", "kill-session", "-t", "pgu-ops"]
        assert runner.calls[4][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
        assert f"--resume {session_id}" in runner.calls[4][-1]
        assert "--continue" not in runner.calls[4][-1]
        assert "--last" not in runner.calls[4][-1]
        assert runner.calls[5] == ["tmux", "attach", "-t", "pgu-ops"]


def test_reload_refuses_to_kill_session_when_live_command_mismatches_config() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    assert run_role_pane(role, mode="reload", session_dir=config.session_dir, runner=runner) == 1

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"],
        ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"],
    ]


def test_reload_force_bypasses_live_command_guard() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"}, current_commands={"pgu-ops:0.0": "claude"})

    assert run_role_pane(
        role,
        mode="reload",
        session_dir=config.session_dir,
        force_reload=True,
        runner=runner,
    ) == 0

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
            resume_flag=role.resume_flag,
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


def test_bootstrap_template_does_not_guess_active_roles() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        output = Path(tmp) / "porter.json"

        assert write_template("porter", output) == 0

        template = json.loads(output.read_text(encoding="utf-8"))
        assert template["project"] == "porter"
        assert template["roles"] == []


def main() -> int:
    test_pgu_layout_matches_reference_six_pane_geometry()
    test_dry_run_materializes_pgu_layout_with_six_visible_role_commands()
    test_pgu_config_matches_director_supplied_live_role_assignments()
    test_yolo_config_translates_to_cli_specific_bypass_flags()
    test_effort_config_translates_to_cli_specific_args()
    test_pgu_launch_commands_include_model_and_bypass_flags()
    test_konsole_launch_uses_gui_user_display_environment()
    test_konsole_launch_can_fallback_to_gui_user_uid_without_host_var()
    test_konsole_launch_falls_back_to_clear_error_when_gui_user_missing()
    test_start_is_attach_or_start_and_never_duplicates_existing_session()
    test_start_creates_missing_session_once_then_attaches()
    test_start_runs_research_detached_before_opening_visible_layout()
    test_start_force_refreshes_dirty_shared_checkout_with_real_git()
    test_bad_shared_checkout_blocks_all_roles_with_real_git()
    test_reload_fetches_without_refreshing_shared_checkout()
    test_dry_run_reports_shared_checkout_without_syncing_it()
    test_detached_research_attach_checks_session_without_attaching()
    test_reload_uses_recorded_resume_uuid_when_recreating_session()
    test_reload_refuses_to_kill_session_when_live_command_mismatches_config()
    test_reload_force_bypasses_live_command_guard()
    test_reload_guard_matches_cli_child_under_shell_in_real_tmux()
    test_bootstrap_template_does_not_guess_active_roles()
    print("team_launcher_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
