"""Regression tests for the JSON-driven team launcher."""

from __future__ import annotations

import hashlib

import inspect

import json

import os

import signal

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

from typing import Any, Callable, Sequence

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

from tmux_socket_cleanup import (
    cleanup_dead_isolated_tmux_socket as _cleanup_dead_isolated_tmux_socket,
    cleanup_isolated_tmux_sessions as _cleanup_isolated_tmux_sessions,
    isolated_tmux_socket_path as _isolated_tmux_socket_path,
    run_isolated_tmux as _run_isolated_tmux,
    tmux_args as _tmux_args,
)

LIVE_PANE_SESSION_PATHS = candidate_live_pane_session_paths()

LIVE_PANE_STATE_PATHS = candidate_live_pane_state_paths()

strip_ticket_board_pane_env(os.environ)
TEST_SWITCHYARD_SHARED_INSTALL_ROOT = Path(tempfile.gettempdir()) / f"switchyard-test-{os.getpid()}" / "opt" / "switchyard"
os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(TEST_SWITCHYARD_SHARED_INSTALL_ROOT)
os.environ["XDG_CURRENT_DESKTOP"] = "GNOME"
os.environ["KDE_FULL_SESSION"] = ""

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
    set_project_vcs_close_role_command,
    session_file_name,
    switchyard_main,
    switchyard_menu_command,
    switchyard_new_command,
    switchyard_register_command,
    switchyard_status_command,
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

class RecordingProcessLauncher:
    def __init__(self, *, pid: int = 424242, poll_result: int | None = None) -> None:
        self.pid = pid
        self.poll_result = poll_result
        self.calls: list[dict[str, object]] = []

    def __call__(self, args: list[str], **kwargs: object) -> object:
        self.calls.append({"args": args, "kwargs": kwargs})

        class Process:
            pid = self.pid

            def poll(_self: object) -> int | None:
                return self.poll_result

        return Process()

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
            elif Path(token).name in {"agy", "claude", "codex", "hermes"}:
                command = Path(token).name
                break
        return target, command

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["sudo", "-u"] and len(args) >= 4:
            inner = args[4:] if len(args) >= 5 and args[3] == "-H" else args[3:]
            if inner[:2] == ["tmux", "has-session"]:
                session = inner[-1]
                return subprocess.CompletedProcess(args, 0 if session in self.existing_sessions else 1)
            if inner[:3] == ["tmux", "display-message", "-p"]:
                target = inner[inner.index("-t") + 1]
                if inner[-1] == "#{pane_pid}":
                    pane_pid = int(self._value_for(self.pane_pids, target, 0) or 0)
                    return subprocess.CompletedProcess(args, 0 if pane_pid else 1, stdout=f"{pane_pid}\n" if pane_pid else "")
                command = str(self._value_for(self.current_commands, target, "") or "")
                return subprocess.CompletedProcess(args, 0 if command else 1, stdout=command + "\n")
            if inner[:2] == ["tmux", "new-session"]:
                session = inner[inner.index("-s") + 1]
                self.existing_sessions.add(session)
                target, command = self._target_and_command_from_shell(str(inner[-1]))
                if target and command and target not in self.current_commands:
                    self.current_commands[target] = command
                return subprocess.CompletedProcess(args, 0)
            if inner[:2] == ["tmux", "kill-session"]:
                session = inner[-1]
                self.existing_sessions.discard(session)
                return subprocess.CompletedProcess(args, 0)
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

class KeywordRecordingFakeRunner(FakeRunner):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls_with_kwargs: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls_with_kwargs.append((args, kwargs))
        return super().__call__(args, **kwargs)

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

def _layout_row_lengths(tree: object) -> list[int]:
    if isinstance(tree, dict) and tree.get("Orientation") == "Horizontal":
        return [len(tree.get("Widgets", []))]
    if isinstance(tree, dict) and tree.get("Orientation") == "Vertical":
        return [
            len(row.get("Widgets", [])) if isinstance(row, dict) else 1
            for row in tree.get("Widgets", [])
        ]
    return [1]

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

FROZEN_COLUMN_MAJOR_FOUR_LAYOUT = _literal_layout_from_session_tree(
    {
        "Orientation": "Horizontal",
        "Widgets": [0, 1, 2, 3],
    }
)

FROZEN_COLUMN_MAJOR_THREE_LAYOUT = _literal_layout_from_session_tree(
    {
        "Orientation": "Horizontal",
        "Widgets": [
            {"Orientation": "Vertical", "Widgets": [0, 1]},
            2,
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

def _write_source_onboarding_docs(source_repo: Path, *, initialize_git: bool = False) -> str:
    docs_dir = source_repo / "docs" / "onboarding"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for name in team_launcher.SWITCHYARD_ONBOARDING_DOC_NAMES:
        docs_dir.joinpath(name).write_text(f"# {name}\n\nSource copy for {name}.\n", encoding="utf-8")
    if not initialize_git:
        return ""
    _run_git(["git", "init", "-b", "main"], cwd=source_repo)
    _run_git(["git", "add", "docs/onboarding"], cwd=source_repo)
    _run_git(
        [
            "git",
            "-c",
            "user.name=Switchyard Test",
            "-c",
            "user.email=switchyard-test@example.invalid",
            "commit",
            "-m",
            "Add onboarding docs",
        ],
        cwd=source_repo,
    )
    return _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()

def _owner_file_install_runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    if args[:1] == ["git"]:
        run_kwargs = dict(kwargs)
        run_kwargs.setdefault("text", True)
        run_kwargs.setdefault("stdout", subprocess.PIPE)
        run_kwargs.setdefault("stderr", subprocess.PIPE)
        return subprocess.run(args, **run_kwargs)
    if args[:1] == ["install"]:
        for raw_path in args[args.index("-g") + 2 :]:
            Path(raw_path).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0)
    if args[:1] == ["chown"]:
        return subprocess.CompletedProcess(args, 0)
    return subprocess.CompletedProcess(args, 0)

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

def _live_session_record_fingerprint() -> dict[str, str]:
    files = sorted(path for path in LIVE_SESSION_DIR.glob("*.json") if path.is_file())
    return {path.name: hashlib.md5(path.read_bytes()).hexdigest() for path in files}

def _assert_live_session_records_unchanged(before: dict[str, str], after: dict[str, str]) -> None:
    assert len(after) == len(before), {"before": before, "after": after}
    assert after == before

def _env_entries(command: list[str]) -> list[str]:
    assert command[0] == "env"
    entries: list[str] = []
    index = 1
    while index < len(command):
        token = command[index]
        if token == "-u":
            index += 2
            continue
        if "=" not in token:
            break
        entries.append(token)
        index += 1
    return entries

def _command_tail(command: list[str]) -> list[str]:
    assert command[0] == "env"
    index = 1
    while index < len(command):
        token = command[index]
        if token == "-u":
            index += 2
            continue
        if "=" not in token:
            return command[index:]
        index += 1
    return []

def _env_unsets(command: list[str]) -> set[str]:
    assert command[0] == "env"
    result: set[str] = set()
    index = 1
    while index < len(command):
        token = command[index]
        if token == "-u":
            result.add(command[index + 1])
            index += 2
            continue
        if "=" not in token:
            break
        index += 1
    return result

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

def _write_first_run_auth_config(
    tmp_path: Path,
    *,
    roles: list[tuple[str, str]],
    owner_user: str = "otto-agent",
    role_models: dict[str, str] | None = None,
) -> Path:
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
                        **{
                            "role": role,
                            "slot": index,
                            "target": f"otto-{role}:0.0",
                            "tmux_session": f"otto-{role}",
                            "workdir": str(tmp_path / "worktrees" / role),
                            "cli": [cli],
                            "yolo": True,
                        },
                        **({"model": role_models[role]} if role_models and role in role_models else {}),
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

def _mark_first_run_roles_detached(config_path: Path, *role_names: str) -> None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    detached = set(role_names)
    for role in raw["roles"]:
        if role["role"] in detached:
            role["detached"] = True
            role.pop("slot", None)
    config_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")

class FirstRunAuthRunner:
    def __init__(
        self,
        *,
        authenticated_after_login: bool = True,
        missing_clis: set[str] | None = None,
        missing_cli_status_returncode: int = 127,
        invalid_models: dict[tuple[str, str], str] | None = None,
        empty_success_models: set[tuple[str, str]] | None = None,
        model_catalogs: dict[str, list[str]] | None = None,
        unauthenticated_clis: set[str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.call_kwargs: list[dict[str, object]] = []
        self.authenticated_after_login = authenticated_after_login
        self.login_seen: set[str] = set()
        self.missing_clis = set(missing_clis or ())
        self.missing_cli_status_returncode = missing_cli_status_returncode
        self.invalid_models = dict(invalid_models or {})
        self.empty_success_models = set(empty_success_models or ())
        self.unauthenticated_clis = set(unauthenticated_clis or ())
        self.model_catalogs = {
            "agy": ["gemini-3.7-flash-high", "gemini-3.7-pro"],
            **dict(model_catalogs or {}),
        }

    @staticmethod
    def _model_from_command(command: list[str], model_arg: str = "--model") -> str:
        for index, token in enumerate(command):
            if token == model_arg and index + 1 < len(command):
                return command[index + 1]
            if token.startswith(f"{model_arg}="):
                return token.split("=", 1)[1]
        return ""

    def _model_probe(self, args: list[str], command: list[str], cli: str, model: str) -> subprocess.CompletedProcess[str]:
        if (cli, model) in self.empty_success_models:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        reason = self.invalid_models.get((cli, model))
        if reason:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=reason)
        return subprocess.CompletedProcess(args, 0, stdout="model-ok\n")

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.call_kwargs.append(dict(kwargs))
        command = args[3:] if args[:2] == ["sudo", "-u"] else args
        if command and command[0] == "env":
            index = 1
            while index < len(command) and "=" in command[index]:
                index += 1
            command = command[index:]
        self.calls.append([*args[:3], *command] if args[:2] == ["sudo", "-u"] else list(command))
        if command[:2] == ["sh", "-lc"] and len(command) == 3 and command[2].startswith("command -v "):
            cli = command[2].removeprefix("command -v ").strip()
            if cli in self.missing_clis:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=f"/usr/bin/{cli}\n")
        if command == ["claude", "auth", "status", "--json"]:
            if "claude" in self.missing_clis:
                return subprocess.CompletedProcess(
                    args,
                    self.missing_cli_status_returncode,
                    stdout="",
                    stderr="claude: command not found\n",
                )
            logged_in = "claude" in self.login_seen if self.authenticated_after_login else False
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"loggedIn": logged_in}) + "\n")
        if command == ["codex", "login", "status"]:
            if "codex" in self.missing_clis:
                return subprocess.CompletedProcess(
                    args,
                    self.missing_cli_status_returncode,
                    stdout="",
                    stderr="codex: command not found\n",
                )
            if "codex" in self.unauthenticated_clis:
                return subprocess.CompletedProcess(args, 1, stderr="Not logged in\n")
            if self.authenticated_after_login and "codex" in self.login_seen:
                return subprocess.CompletedProcess(args, 0, stdout="Logged in using ChatGPT\n")
            return subprocess.CompletedProcess(args, 1, stderr="Not logged in\n")
        if command == ["agy", "models"]:
            if "agy" in self.missing_clis:
                return subprocess.CompletedProcess(
                    args,
                    self.missing_cli_status_returncode,
                    stdout="",
                    stderr="agy: command not found\n",
                )
            if self.authenticated_after_login and "agy" in self.login_seen:
                catalog = self.model_catalogs.get("agy", [])
                return subprocess.CompletedProcess(args, 0, stdout="".join(f"{model} description\n" for model in catalog))
            return subprocess.CompletedProcess(args, 1, stderr="You are not logged into Antigravity.\n")
        if command and command[0] == "claude" and "-p" in command:
            return self._model_probe(args, command, "claude", self._model_from_command(command))
        if command[:2] == ["codex", "exec"]:
            return self._model_probe(args, command, "codex", self._model_from_command(command))
        if command and command[0] == "agy" and "-p" in command:
            return self._model_probe(args, command, "agy", self._model_from_command(command))
        if command and command[0] == "hermes" and "-z" in command:
            return self._model_probe(args, command, "hermes", self._model_from_command(command, "-m"))
        if command == ["hermes", "config", "check"]:
            if "hermes" in self.missing_clis:
                return subprocess.CompletedProcess(
                    args,
                    self.missing_cli_status_returncode,
                    stdout="",
                    stderr="hermes: command not found\n",
                )
            if "hermes" in self.unauthenticated_clis:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="📋 Configuration Status\n\n  Optional:\n    ○ OPENROUTER_API_KEY → vision_analyze, mixture_of_agents\n",
                )
            if self.authenticated_after_login and "hermes" in self.login_seen:
                return subprocess.CompletedProcess(args, 0, stdout="📋 Configuration Status\n\n  Optional:\n    ✓ OPENROUTER_API_KEY\n")
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="📋 Configuration Status\n\n  Optional:\n    ○ OPENROUTER_API_KEY → vision_analyze, mixture_of_agents\n",
            )
        if command == ["claude", "auth", "login"]:
            self.login_seen.add("claude")
            return subprocess.CompletedProcess(args, 0)
        if command == ["codex", "login"]:
            self.login_seen.add("codex")
            return subprocess.CompletedProcess(args, 0)
        if command == ["agy"]:
            self.login_seen.add("agy")
            return subprocess.CompletedProcess(args, 0)
        if command == ["hermes", "model"]:
            self.login_seen.add("hermes")
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)

def _write_codex_first_run_hooks(owner_home: Path) -> None:
    hooks_path = owner_home / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "/home/otto-agent/.local/bin/ticket-board-pane-idle-hook "
                                        "idle --source codex.SessionStart --state-dir "
                                        "/run/user/1001/otto-ticket-board/pane-state --session-dir "
                                        "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions --record-session"
                                    ),
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "/home/otto-agent/.local/bin/ticket-board-pane-idle-hook "
                                        "idle --source codex.Stop --state-dir "
                                        "/run/user/1001/otto-ticket-board/pane-state --session-dir "
                                        "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions"
                                    ),
                                }
                            ]
                        }
                    ],
                }
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

def _write_matching_codex_hook_trust(owner_home: Path) -> None:
    entries = team_launcher._codex_command_hook_trust_entries(owner_home)
    config_path = owner_home / ".codex" / "config.toml"
    lines = ["[hooks.state]", ""]
    for _event, hook_key, current_hash in entries:
        lines.extend(
            [
                f'[hooks.state."{hook_key}"]',
                f'trusted_hash = "{current_hash}"',
                "",
            ]
        )
    config_path.write_text("\n".join(lines), encoding="utf-8")

def run_team_launcher_tests(module_globals: dict[str, object], *, first: tuple[str, ...] = ()) -> None:
    live_session_snapshot = snapshot_paths(LIVE_PANE_SESSION_PATHS)
    live_pane_state_snapshot = snapshot_paths_file_set(LIVE_PANE_STATE_PATHS)
    live_pane_state_anomalies = pane_state_record_anomalies(LIVE_PANE_STATE_PATHS)
    live_session_fingerprint = _live_session_record_fingerprint()
    try:
        run_module_tests(module_globals, first=first)
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


__all__ = [name for name in globals() if not (name.startswith('__') and name.endswith('__'))]
