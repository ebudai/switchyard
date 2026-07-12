#!/usr/bin/env python3
"""Regression tests for ticket-board pane hook installation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "ticket-board-install-pane-hooks"
HOOK_NAME = "pgu-ticket-board-pane-idle-hook"
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


def test_installer_writes_durable_cli_hook_configs_idempotently() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
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
        gemini = _load(home / ".gemini" / "antigravity-cli" / "settings.json")

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
            "gemini.BeforeAgent",
            "gemini.AfterAgent",
            "--record-session",
        ):
            assert required in all_commands
        assert _managed_command_count(claude) == 4
        assert _managed_command_count(codex) == 4
        assert _managed_command_count(gemini) == 4


def test_installed_hook_writes_state_and_verify_state_checks_all_panes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        subprocess.run(
            [str(bin_path), "idle", "--target", "pgu-ops:0.0", "--source", "codex.Stop", "--state-dir", str(state_dir)],
            check=True,
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
            subprocess.run([str(bin_path), "idle", "--target", target, "--state-dir", str(state_dir)], check=True)
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


def main() -> int:
    test_installer_writes_durable_cli_hook_configs_idempotently()
    test_installed_hook_writes_state_and_verify_state_checks_all_panes()
    test_session_start_hook_seeds_idle_state_and_records_resume_session()
    print("ticket_board_pane_hooks_install_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
