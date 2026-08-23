#!/usr/bin/env python3
"""Regression tests for ticket-board pane hook installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "ticket-board-install-pane-hooks"
HOOK_NAME = "ticket-board-pane-idle-hook"
LEGACY_HOOK_NAME = "pgu-ticket-board-pane-idle-hook"
PANE_TARGETS = (
    "pgu-director:0.0",
    "pgu-main:0.0",
    "pgu-app:0.0",
    "pgu-perf:0.0",
    "pgu-research:0.0",
    "pgu-ops:0.0",
    "pgu-audit:0.0",
    "pgu-inspector:0.0",
)
SESSION_ENV_KEYS = (
    "TICKET_BOARD_PANE_SESSION_ID",
    "PGU_PANE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
    "GEMINI_SESSION_ID",
    "SESSION_ID",
)
HOOK_PATH_ENV_KEYS = (
    "TICKET_BOARD_PANE_SESSION_DIR",
    "PGU_TICKET_BOARD_PANE_SESSION_DIR",
    "TICKET_BOARD_PANE_STATE_DIR",
    "PGU_TICKET_BOARD_PANE_STATE_DIR",
)


def _hook_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in (*SESSION_ENV_KEYS, *HOOK_PATH_ENV_KEYS):
        env.pop(key, None)
    env.update(extra)
    return env


def _candidate_live_session_dirs() -> list[Path]:
    candidates: list[Path] = []
    for key in ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR"):
        value = os.environ.get(key, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    xdg_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    state_home = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local" / "state"
    candidates.append(state_home / "pgu-ticket-board" / "pane-sessions")
    candidates.append(Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions"))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(candidate.resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def _snapshot_session_dir(path: Path) -> dict[str, bytes] | None:
    try:
        if not path.exists():
            return None
        if not path.is_dir():
            return {}
        snapshot: dict[str, bytes] = {}
        for child in sorted(path.rglob("*")):
            if child.is_file():
                snapshot[str(child.relative_to(path))] = child.read_bytes()
        return snapshot
    except OSError:
        return {}


def _snapshot_live_session_dirs() -> dict[str, dict[str, bytes] | None]:
    return {str(path): _snapshot_session_dir(path) for path in _candidate_live_session_dirs()}


def _session_snapshot_diff(
    before: dict[str, dict[str, bytes] | None],
    after: dict[str, dict[str, bytes] | None],
) -> list[str]:
    changed: list[str] = []
    for root in sorted(set(before) | set(after)):
        before_files = before.get(root)
        after_files = after.get(root)
        if before_files is None or after_files is None:
            if before_files != after_files:
                changed.append(root)
            continue
        for name in sorted(set(before_files) | set(after_files)):
            if before_files.get(name) != after_files.get(name):
                changed.append(f"{root}/{name}")
    return changed


def _load(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _commands(value: Any) -> list[str]:
    if isinstance(value, dict):
        command = value.get("command")
        nested = [command] if isinstance(command, str) else []
        for child in value.values():
            nested.extend(_commands(child))
        return nested
    if isinstance(value, list):
        commands: list[str] = []
        for child in value:
            commands.extend(_commands(child))
        return commands
    return []


def _managed_command_count(config: dict[str, Any]) -> int:
    return sum(1 for command in _commands(config) if HOOK_NAME in command)


def _managed_commands_by_source(config: dict[str, Any]) -> dict[str, str]:
    commands: dict[str, str] = {}
    for command in _commands(config):
        if HOOK_NAME not in command or "--source " not in command:
            continue
        source = command.split("--source ", 1)[1].split()[0]
        commands[source] = command
    return commands


def test_installer_writes_durable_cli_hook_configs_idempotently() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        stale_gemini = home / ".gemini" / "config" / "hooks.json"
        stale_gemini.parent.mkdir(parents=True)
        stale_gemini.write_text(
            json.dumps(
                {
                    "hooks": {
                        "AfterAgent": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{LEGACY_HOOK_NAME} idle --source gemini.AfterAgent",
                                    }
                                ]
                            }
                        ]
                    },
                    "other-hook": {"Stop": [{"type": "command", "command": "true"}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        for _ in range(2):
            subprocess.run(
                [str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)],
                check=True,
                text=True,
                capture_output=True,
            )

        assert bin_path.exists()
        assert bin_path.stat().st_mode & 0o111
        claude = _load(home / ".claude" / "settings.json")
        codex = _load(home / ".codex" / "hooks.json")
        gemini = _load(home / ".gemini" / "config" / "hooks.json")

        all_commands = "\n".join(_commands([claude, codex, gemini]))
        for required in (
            "claude.SessionStart",
            "claude.Notification.idle_prompt",
            "claude.UserPromptSubmit",
            "claude.Stop",
            "codex.SessionStart",
            "codex.Stop",
            "codex.UserPromptSubmit",
            "codex.PermissionRequest",
            "gemini.SessionStart",
            "gemini.PreInvocation",
            "gemini.PostInvocation",
            "gemini.Stop",
            "--record-session",
        ):
            assert required in all_commands
        assert _managed_command_count(claude) == 4
        assert _managed_command_count(codex) == 4
        assert _managed_command_count(gemini) == 4
        assert "ticket-board-pane-state" in gemini
        assert "pgu-ticket-board-pane-state" not in gemini
        assert "hooks" not in gemini
        assert gemini["other-hook"] == {"Stop": [{"type": "command", "command": "true"}]}

        claude_commands = _managed_commands_by_source(claude)
        assert " idle --source claude.Stop" in claude_commands["claude.Stop"]
        assert " busy --source claude.Stop" not in claude_commands["claude.Stop"]


def test_session_dir_snapshot_detects_same_count_content_mutation() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        session_dir = Path(tmp) / "sessions"
        session_dir.mkdir()
        session_path = session_dir / "pgu-ops_0.0.json"
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "real-session"}) + "\n",
            encoding="utf-8",
        )

        before = _snapshot_session_dir(session_dir)
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "12345678-1234-5678-9abc-def012345679"}) + "\n",
            encoding="utf-8",
        )
        after = _snapshot_session_dir(session_dir)

        assert before is not None
        assert after is not None
        assert len(before) == len(after) == 1
        assert before != after


def test_installed_hook_writes_state_and_verify_state_checks_all_panes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        subprocess.run(
            [str(bin_path), "idle", "--target", "pgu-ops:0.0", "--source", "codex.Stop", "--state-dir", str(state_dir)],
            check=True,
            env=_hook_env(),
        )
        written = json.loads((state_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert written["target"] == "pgu-ops:0.0"
        assert written["state"] == "idle"
        assert written["source"] == "codex.Stop"

        missing = subprocess.run(
            [str(INSTALLER), "verify-state", "--state-dir", str(state_dir)],
            text=True,
            capture_output=True,
        )
        assert missing.returncode == 1
        assert "pgu-director:0.0" in missing.stderr

        for target in PANE_TARGETS:
            subprocess.run(
                [str(bin_path), "idle", "--target", target, "--state-dir", str(state_dir)],
                check=True,
                env=_hook_env(),
            )
        complete = subprocess.run(
            [str(INSTALLER), "verify-state", "--state-dir", str(state_dir)],
            text=True,
            capture_output=True,
        )
        assert complete.returncode == 0
        assert "present for all notification targets" in complete.stdout


def test_session_start_hook_seeds_idle_state_and_records_resume_session() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        session_id = "12345678-1234-5678-9abc-def012345678"
        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=json.dumps({"session_id": session_id, "cwd": "/home/agent/Projects/pgu"}),
            text=True,
            check=True,
            env=_hook_env(),
        )

        state = json.loads((state_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert state["target"] == "pgu-ops:0.0"
        assert state["state"] == "idle"
        assert state["source"] == "codex.SessionStart"

        session = json.loads((session_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert session["target"] == "pgu-ops:0.0"
        assert session["session_id"] == session_id
        assert session["source"] == "codex.SessionStart"
        assert session["payload"]["cwd"] == "/home/agent/Projects/pgu"


def test_session_start_hook_defaults_to_xdg_state_home_session_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        xdg_state_home = Path(tmp) / "xdg-state"
        ambient_session_dir = Path(tmp) / "ambient-live-session-dir"
        session_dir = xdg_state_home / "pgu-ticket-board" / "pane-sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        session_id = "12345678-1234-5678-9abc-def012345679"
        original_session_dir = os.environ.get("TICKET_BOARD_PANE_SESSION_DIR")
        try:
            os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = str(ambient_session_dir)
            subprocess.run(
                [
                    str(bin_path),
                    "idle",
                    "--target",
                    "pgu-ops:0.0",
                    "--source",
                    "codex.SessionStart",
                    "--state-dir",
                    str(state_dir),
                    "--record-session",
                ],
                input=json.dumps({"session_id": session_id}),
                text=True,
                check=True,
                env=_hook_env(XDG_STATE_HOME=str(xdg_state_home)),
            )
        finally:
            if original_session_dir is None:
                os.environ.pop("TICKET_BOARD_PANE_SESSION_DIR", None)
            else:
                os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = original_session_dir

        session = json.loads((session_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == session_id
        assert oct(session_dir.stat().st_mode & 0o777) == "0o700"
        assert oct((session_dir / "pgu-ops_0.0.json").stat().st_mode & 0o777) == "0o600"
        assert not (ambient_session_dir / "pgu-ops_0.0.json").exists()


def test_hook_prefers_generic_target_and_session_envs() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        session_id = "12345678-1234-5678-9abc-def012345680"
        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--source",
                "codex.Stop",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            check=True,
            env=_hook_env(
                TICKET_BOARD_PANE_TARGET="porter-ops:0.0",
                PGU_PANE_TARGET="pgu-ops:0.0",
                TICKET_BOARD_PANE_SESSION_ID=session_id,
                PGU_PANE_SESSION_ID="12345678-1234-5678-9abc-def012345681",
            ),
        )

        state = json.loads((state_dir / "porter-ops_0.0.json").read_text(encoding="utf-8"))
        session = json.loads((session_dir / "porter-ops_0.0.json").read_text(encoding="utf-8"))
        assert state["target"] == "porter-ops:0.0"
        assert session["session_id"] == session_id


def test_non_session_start_hook_records_resume_session_when_payload_has_id() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        session_id = "turn_session_789"
        subprocess.run(
            [
                str(bin_path),
                "busy",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.UserPromptSubmit",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
            ],
            input=json.dumps({"event": "UserPromptSubmit", "session_id": session_id}),
            text=True,
            check=True,
            env=_hook_env(),
        )

        state = json.loads((state_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert state["target"] == "pgu-ops:0.0"
        assert state["state"] == "busy"
        assert state["source"] == "codex.UserPromptSubmit"

        session = json.loads((session_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert session["target"] == "pgu-ops:0.0"
        assert session["session_id"] == session_id
        assert session["source"] == "codex.UserPromptSubmit"
        assert session["payload"]["event"] == "UserPromptSubmit"


def test_non_session_start_hook_with_existing_session_does_not_read_or_rewrite() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
        session_dir.mkdir(parents=True)
        session_path = session_dir / "pgu-ops_0.0.json"
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "existing_session", "source": "codex.SessionStart"})
            + "\n",
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [
                str(bin_path),
                "busy",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.UserPromptSubmit",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--stdin-timeout",
                "5",
            ],
            stdin=subprocess.PIPE,
            text=True,
            env=_hook_env(),
        )
        try:
            assert proc.wait(timeout=0.5) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1)

        state = json.loads((state_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert state["state"] == "busy"
        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert session["session_id"] == "existing_session"
        assert session["source"] == "codex.SessionStart"


def test_non_session_start_hook_without_session_id_only_writes_state() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.Stop",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
            ],
            input=json.dumps({"event": "Stop", "cwd": "/home/agent/Projects/pgu"}),
            text=True,
            check=True,
            env=_hook_env(),
        )

        state = json.loads((state_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert state["target"] == "pgu-ops:0.0"
        assert state["state"] == "idle"
        assert state["source"] == "codex.Stop"
        assert not (session_dir / "pgu-ops_0.0.json").exists()


def test_state_write_is_not_blocked_by_hung_stdin_pipe() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        proc = subprocess.Popen(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.Stop",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--stdin-timeout",
                "2",
            ],
            stdin=subprocess.PIPE,
            text=True,
            env=_hook_env(),
        )
        try:
            state_path = state_dir / "pgu-ops_0.0.json"
            for _ in range(20):
                if state_path.exists():
                    break
                time.sleep(0.05)
            assert state_path.exists()
            assert proc.poll() is None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1)

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["state"] == "idle"
        assert state["source"] == "codex.Stop"


def test_hung_stdin_pipe_exits_after_bounded_read() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        proc = subprocess.Popen(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.Stop",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--stdin-timeout",
                "0.05",
            ],
            stdin=subprocess.PIPE,
            text=True,
            env=_hook_env(),
        )
        try:
            assert proc.wait(timeout=1) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1)

        state = json.loads((state_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert state["state"] == "idle"
        assert not (session_dir / "pgu-ops_0.0.json").exists()


def test_session_start_records_non_uuid_named_session_id() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        session_id = "sess_abc123"
        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=json.dumps({"session_id": session_id, "cwd": "/home/agent/Projects/pgu"}),
            text=True,
            check=True,
            env=_hook_env(),
        )

        session = json.loads((session_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == session_id


def test_session_start_always_updates_existing_session_file() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
        session_dir.mkdir(parents=True)
        session_path = session_dir / "pgu-ops_0.0.json"
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "old_session", "source": "codex.Stop"}) + "\n",
            encoding="utf-8",
        )

        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=json.dumps({"session_id": "new_session"}),
            text=True,
            check=True,
            env=_hook_env(),
        )

        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert session["session_id"] == "new_session"
        assert session["source"] == "codex.SessionStart"


def test_session_start_records_env_session_id_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=json.dumps({"event": "SessionStart"}),
            text=True,
            check=True,
            env=_hook_env(CODEX_SESSION_ID="codex_env_session_456"),
        )

        session = json.loads((session_dir / "pgu-ops_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == "codex_env_session_456"


def main() -> int:
    live_session_snapshot = _snapshot_live_session_dirs()
    try:
        test_installer_writes_durable_cli_hook_configs_idempotently()
        test_session_dir_snapshot_detects_same_count_content_mutation()
        test_installed_hook_writes_state_and_verify_state_checks_all_panes()
        test_session_start_hook_seeds_idle_state_and_records_resume_session()
        test_session_start_hook_defaults_to_xdg_state_home_session_dir()
        test_hook_prefers_generic_target_and_session_envs()
        test_non_session_start_hook_records_resume_session_when_payload_has_id()
        test_non_session_start_hook_with_existing_session_does_not_read_or_rewrite()
        test_non_session_start_hook_without_session_id_only_writes_state()
        test_state_write_is_not_blocked_by_hung_stdin_pipe()
        test_hung_stdin_pipe_exits_after_bounded_read()
        test_session_start_records_non_uuid_named_session_id()
        test_session_start_always_updates_existing_session_file()
        test_session_start_records_env_session_id_fallback()
    finally:
        after_snapshot = _snapshot_live_session_dirs()
        changed = _session_snapshot_diff(live_session_snapshot, after_snapshot)
        assert not changed, changed
    print("ticket_board_pane_hooks_install_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
