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

from ticket_board_pane_env import (
    candidate_live_pane_paths,
    candidate_live_pane_state_paths,
    snapshot_diff,
    snapshot_path,
    snapshot_paths,
    stripped_ticket_board_pane_env,
)
from standalone_test_runner import run_module_tests

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
REAL_INSPECTOR_SESSION_ID = "98759ea4-3597-4d25-933d-7b5059a0db61"
TRANSIENT_AGY_SESSION_ID = "ebd87947-ab41-4bcd-a487-109fa5750566"


def _hook_env(**extra: str) -> dict[str, str]:
    return stripped_ticket_board_pane_env(**extra)


def _inspector_session_record() -> dict[str, Any]:
    return {
        "target": "pgu-inspector:0.0",
        "session_id": REAL_INSPECTOR_SESSION_ID,
        "source": "gemini.SessionStart",
        "payload": {"session_id": REAL_INSPECTOR_SESSION_ID, "cwd": "/home/agent/Projects/pgu"},
    }


def _transient_agy_payload() -> str:
    return json.dumps(
        {
            "event": "SessionStart",
            "conversationId": TRANSIENT_AGY_SESSION_ID,
            "artifactDirectoryPath": "/tmp/transient-artifacts",
            "transcriptPath": "/tmp/transient-transcript.jsonl",
        }
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

        before = snapshot_path(session_dir)
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "12345678-1234-5678-9abc-def012345679"}) + "\n",
            encoding="utf-8",
        )
        after = snapshot_path(session_dir)

        assert before is not None
        assert after is not None
        assert len(before) == len(after) == 1
        assert before != after


def test_pane_state_snapshot_detects_modified_deleted_and_added_records() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        state_dir = Path(tmp) / "pane-state"
        state_dir.mkdir()
        ops_path = state_dir / "pgu-ops_0.0.json"
        audit_path = state_dir / "pgu-audit_0.0.json"
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")

        before_modify = snapshot_paths([state_dir])
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "busy"}) + "\n", encoding="utf-8")
        assert snapshot_diff(before_modify, snapshot_paths([state_dir])) == [f"{state_dir}/pgu-ops_0.0.json"]

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        before_delete = snapshot_paths([state_dir])
        ops_path.unlink()
        assert snapshot_diff(before_delete, snapshot_paths([state_dir])) == [f"{state_dir}/pgu-ops_0.0.json"]

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        before_add = snapshot_paths([state_dir])
        audit_path.write_text(json.dumps({"target": "pgu-audit:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        assert snapshot_diff(before_add, snapshot_paths([state_dir])) == [f"{state_dir}/pgu-audit_0.0.json"]


def test_live_pane_state_candidates_are_explicit_runtime_paths_not_xdg_state_home() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        env = _hook_env(HOME=str(Path(tmp) / "home"), XDG_STATE_HOME=str(Path(tmp) / "xdg-state"))
        candidates = candidate_live_pane_state_paths(env)

    assert candidates == [Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")]


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
                env=_hook_env(HOME=str(home), XDG_STATE_HOME=str(xdg_state_home)),
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


def test_non_session_start_hook_updates_existing_session_when_payload_matches_launched_id() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
        session_dir.mkdir(parents=True)
        session_path = session_dir / "pgu-ops_0.0.json"
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "old_session", "source": "codex.SessionStart"})
            + "\n",
            encoding="utf-8",
        )

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
            input=json.dumps({"event": "Stop", "session_id": "new_session"}),
            text=True,
            check=True,
            env=_hook_env(TICKET_BOARD_PANE_SESSION_ID="new_session"),
        )

        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert session["session_id"] == "new_session"
        assert session["source"] == "codex.Stop"
        assert session["payload"]["event"] == "Stop"


def test_non_session_start_hook_with_existing_session_does_not_block_or_rewrite_without_payload() -> None:
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


def test_session_start_updates_existing_record_when_it_matches_launched_session() -> None:
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
            env=_hook_env(TICKET_BOARD_PANE_SESSION_ID="new_session"),
        )

        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert session["session_id"] == "new_session"
        assert session["source"] == "codex.SessionStart"


def test_foreign_session_start_cannot_clobber_launched_pane_session_record() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
        session_dir.mkdir(parents=True)
        session_path = session_dir / "pgu-inspector_0.0.json"
        original_record = _inspector_session_record()
        session_path.write_text(json.dumps(original_record, sort_keys=True) + "\n", encoding="utf-8")

        proc = subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-inspector:0.0",
                "--source",
                "gemini.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=_transient_agy_payload(),
            text=True,
            capture_output=True,
            check=True,
            env=_hook_env(TICKET_BOARD_PANE_SESSION_ID=REAL_INSPECTOR_SESSION_ID),
        )

        state = json.loads((state_dir / "pgu-inspector_0.0.json").read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert state["target"] == "pgu-inspector:0.0"
        assert state["state"] == "idle"
        assert state["source"] == "gemini.SessionStart"
        assert session == original_record
        assert "ignoring foreign session id" in proc.stderr


def test_foreign_agy_turn_events_cannot_clobber_launched_pane_session_record() -> None:
    for source, state in [
        ("gemini.PreInvocation", "busy"),
        ("gemini.PostInvocation", "idle"),
        ("gemini.Stop", "idle"),
    ]:
        with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
            home = Path(tmp) / "home"
            state_dir = Path(tmp) / "state"
            session_dir = Path(tmp) / "sessions"
            bin_path = home / ".local" / "bin" / HOOK_NAME
            subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
            session_dir.mkdir(parents=True)
            session_path = session_dir / "pgu-inspector_0.0.json"
            original_record = _inspector_session_record()
            session_path.write_text(json.dumps(original_record, sort_keys=True) + "\n", encoding="utf-8")

            proc = subprocess.run(
                [
                    str(bin_path),
                    state,
                    "--target",
                    "pgu-inspector:0.0",
                    "--source",
                    source,
                    "--state-dir",
                    str(state_dir),
                    "--session-dir",
                    str(session_dir),
                    "--stdin-timeout",
                    "0",
                ],
                input=_transient_agy_payload(),
                text=True,
                capture_output=True,
                check=True,
                env=_hook_env(TICKET_BOARD_PANE_SESSION_ID=REAL_INSPECTOR_SESSION_ID),
            )

            state_record = json.loads((state_dir / "pgu-inspector_0.0.json").read_text(encoding="utf-8"))
            session = json.loads(session_path.read_text(encoding="utf-8"))
            assert state_record["state"] == state
            assert state_record["source"] == source
            assert session == original_record
            assert "ignoring foreign session id" in proc.stderr


def test_foreign_session_start_without_launched_id_cannot_clobber_existing_record() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)
        session_dir.mkdir(parents=True)
        session_path = session_dir / "pgu-inspector_0.0.json"
        original_record = _inspector_session_record()
        session_path.write_text(json.dumps(original_record, sort_keys=True) + "\n", encoding="utf-8")

        proc = subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-inspector:0.0",
                "--source",
                "gemini.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=_transient_agy_payload(),
            text=True,
            capture_output=True,
            check=True,
            env=_hook_env(),
        )

        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert session == original_record
        assert "would replace existing session" in proc.stderr


def test_first_session_event_claims_record_after_fresh_launch_clear() -> None:
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
                "pgu-inspector:0.0",
                "--source",
                "gemini.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=_transient_agy_payload(),
            text=True,
            check=True,
            env=_hook_env(),
        )

        session = json.loads((session_dir / "pgu-inspector_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == TRANSIENT_AGY_SESSION_ID
        assert session["source"] == "gemini.SessionStart"


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
    live_pane_paths = candidate_live_pane_paths()
    live_session_snapshot = snapshot_paths(live_pane_paths)
    try:
        run_module_tests(globals())
    finally:
        after_snapshot = snapshot_paths(live_pane_paths)
        changed = snapshot_diff(live_session_snapshot, after_snapshot)
        assert not changed, changed
    print("ticket_board_pane_hooks_install_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
