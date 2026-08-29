#!/usr/bin/env python3
"""Regression tests for ticket-board pane hook installation."""

from __future__ import annotations

import json
import importlib.machinery
import importlib.util
import os
import shlex
import shutil
import threading
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ticket_board_pane_env import (
    candidate_live_pane_session_paths,
    candidate_live_pane_state_paths,
    pane_session_record_diff,
    pane_state_record_anomalies,
    pane_state_record_anomaly_diff,
    snapshot_diff,
    snapshot_pane_session_paths,
    snapshot_file_set_diff,
    snapshot_path,
    snapshot_paths_file_set,
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


class _BoardHandler(BaseHTTPRequestHandler):
    tickets: list[dict[str, object]] = []

    def do_GET(self) -> None:
        if self.path == "/api/board":
            body = json.dumps({"tickets": self.tickets}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _board_with_tickets(tickets: list[dict[str, object]]) -> Any:
    previous_tickets = _BoardHandler.tickets
    _BoardHandler.tickets = tickets
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BoardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        _BoardHandler.tickets = previous_tickets


def _hook_env(**extra: str) -> dict[str, str]:
    return stripped_ticket_board_pane_env(**extra)


def _without_hook_dir_env(environ: dict[str, str]) -> dict[str, str]:
    cleaned = dict(environ)
    for key in (
        "TICKET_BOARD_PANE_STATE_DIR",
        "TICKET_BOARD_PANE_SESSION_DIR",
        "PGU_TICKET_BOARD_PANE_STATE_DIR",
        "PGU_TICKET_BOARD_PANE_SESSION_DIR",
        "TICKET_BOARD_PROJECT",
        "PGU_TICKET_BOARD_PROJECT",
    ):
        cleaned.pop(key, None)
    return cleaned


def _load_hook_module() -> Any:
    loader = importlib.machinery.SourceFileLoader("ticket_board_pane_idle_hook_for_test", str(ROOT / "scripts" / HOOK_NAME))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
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
        hermes = _load_yaml(home / ".hermes" / "config.yaml")

        all_commands = "\n".join(_commands([claude, codex, gemini, hermes]))
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
            "hermes.on_session_start",
            "hermes.on_stream_start",
            "hermes.on_stream_end",
            "hermes.on_session_end",
            "hermes.on_session_finalize",
            "--record-session",
        ):
            assert required in all_commands
        assert _managed_command_count(claude) == 4
        assert _managed_command_count(codex) == 4
        assert _managed_command_count(gemini) == 4
        assert _managed_command_count(hermes) == 5
        assert "ticket-board-pane-state" in gemini
        assert "pgu-ticket-board-pane-state" not in gemini
        assert "hooks" not in gemini
        assert gemini["other-hook"] == {"Stop": [{"type": "command", "command": "true"}]}

        claude_commands = _managed_commands_by_source(claude)
        assert " idle --source claude.Stop" in claude_commands["claude.Stop"]
        assert " busy --source claude.Stop" not in claude_commands["claude.Stop"]
        gemini_commands = _managed_commands_by_source(gemini)
        assert "gemini.SessionStart" in gemini_commands["gemini.SessionStart"]
        assert "--record-session)" in gemini_commands["gemini.SessionStart"]
        assert ">/dev/null" not in gemini_commands["gemini.SessionStart"]
        hermes_commands = _managed_commands_by_source(hermes)
        assert "hermes.on_session_start" in hermes_commands["hermes.on_session_start"]
        assert "--record-session" in hermes_commands["hermes.on_session_start"]
        assert ">/dev/null" not in hermes_commands["hermes.on_session_start"]
        for command in hermes_commands.values():
            assert "$(" not in command
            assert "&&" not in command
            assert ">" not in command
            assert "printf" not in command
            parsed = shlex.split(command)
            assert parsed[0] == str(bin_path)
        for event in (
            "on_session_start",
            "on_stream_start",
            "on_stream_end",
            "on_session_end",
            "on_session_finalize",
        ):
            assert hermes["hooks"][event] == [{"command": hermes_commands[f"hermes.{event}"]}]


def test_installer_migrates_legacy_agy_named_hook_without_removing_foreign_keys() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        stale_gemini = home / ".gemini" / "config" / "hooks.json"
        stale_gemini.parent.mkdir(parents=True)
        stale_gemini.write_text(
            json.dumps(
                {
                    "pgu-ticket-board-pane-state": {
                        "SessionStart": [
                            {
                                "type": "command",
                                "command": (
                                    f"{home}/.local/bin/{LEGACY_HOOK_NAME} idle "
                                    "--source gemini.SessionStart --record-session >/dev/null && printf '{}'"
                                ),
                            }
                        ],
                        "PreInvocation": [
                            {
                                "type": "command",
                                "command": f"{home}/.local/bin/{LEGACY_HOOK_NAME} busy --source gemini.PreInvocation",
                            }
                        ],
                        "PostInvocation": [
                            {
                                "type": "command",
                                "command": f"{home}/.local/bin/{LEGACY_HOOK_NAME} idle --source gemini.PostInvocation",
                            }
                        ],
                        "Stop": [
                            {
                                "type": "command",
                                "command": f"{home}/.local/bin/{LEGACY_HOOK_NAME} idle --source gemini.Stop",
                            }
                        ],
                    },
                    "third-party": {
                        "SessionStart": [{"type": "command", "command": "printf third-party"}],
                    },
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

        gemini = _load(stale_gemini)

    assert set(gemini) == {"ticket-board-pane-state", "third-party"}
    assert _managed_command_count(gemini) == 4
    assert LEGACY_HOOK_NAME not in "\n".join(_commands(gemini))
    assert gemini["third-party"] == {
        "SessionStart": [{"type": "command", "command": "printf third-party"}],
    }
    gemini_commands = _managed_commands_by_source(gemini)
    assert set(gemini_commands) == {
        "gemini.SessionStart",
        "gemini.PreInvocation",
        "gemini.PostInvocation",
        "gemini.Stop",
    }
    assert ">/dev/null" not in gemini_commands["gemini.SessionStart"]


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


def test_session_record_guard_tolerates_same_pane_runtime_rewrite() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        session_dir = Path(tmp) / "sessions"
        session_dir.mkdir()
        session_path = session_dir / "pgu-ops_0.0.json"
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "stable-session", "source": "codex.SessionStart"}) + "\n",
            encoding="utf-8",
        )
        before = snapshot_pane_session_paths([session_dir])
        session_path.write_text(
            json.dumps(
                {
                    "target": "pgu-ops:0.0",
                    "session_id": "stable-session",
                    "source": "codex.Stop",
                    "updated_at": 123.0,
                    "payload": {"event": "turn-end"},
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert pane_session_record_diff(before, snapshot_pane_session_paths([session_dir])) == []


def test_session_record_guard_rejects_test_clobber_content() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        session_dir = Path(tmp) / "sessions"
        session_dir.mkdir()
        wrong_target_path = session_dir / "pgu-ops_0.0.json"
        wrong_target_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "old-session", "source": "codex.SessionStart"}) + "\n",
            encoding="utf-8",
        )
        before_wrong_target = snapshot_pane_session_paths([session_dir])
        wrong_target_path.write_text(
            json.dumps({"target": "pgu-audit:0.0", "session_id": "clobbered-session", "source": "codex.Stop"}) + "\n",
            encoding="utf-8",
        )

        assert pane_session_record_diff(before_wrong_target, snapshot_pane_session_paths([session_dir])) == [
            f"{session_dir}/pgu-ops_0.0.json"
        ]

        different_session_path = session_dir / "pgu-app_0.0.json"
        different_session_path.write_text(
            json.dumps({"target": "pgu-app:0.0", "session_id": "old-session", "source": "codex.SessionStart"}) + "\n",
            encoding="utf-8",
        )
        before_different_session = snapshot_pane_session_paths([session_dir])
        different_session_path.write_text(
            json.dumps({"target": "pgu-app:0.0", "session_id": "new-session", "source": "codex.Stop"}) + "\n",
            encoding="utf-8",
        )

        assert pane_session_record_diff(before_different_session, snapshot_pane_session_paths([session_dir])) == [
            f"{session_dir}/pgu-app_0.0.json"
        ]

        non_cli_source_path = session_dir / "pgu-audit_0.0.json"
        non_cli_source_path.write_text(
            json.dumps({"target": "pgu-audit:0.0", "session_id": "old-session", "source": "claude.SessionStart"}) + "\n",
            encoding="utf-8",
        )
        before_non_cli_source = snapshot_pane_session_paths([session_dir])
        non_cli_source_path.write_text(
            json.dumps({"target": "pgu-audit:0.0", "session_id": "old-session", "source": "test.synthetic"}) + "\n",
            encoding="utf-8",
        )

        assert pane_session_record_diff(before_non_cli_source, snapshot_pane_session_paths([session_dir])) == [
            f"{session_dir}/pgu-audit_0.0.json"
        ]


def test_session_record_guard_rejects_file_set_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        session_dir = Path(tmp) / "sessions"
        session_dir.mkdir()
        session_path = session_dir / "pgu-ops_0.0.json"
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "old-session", "source": "codex.SessionStart"}) + "\n",
            encoding="utf-8",
        )
        before_delete = snapshot_pane_session_paths([session_dir])
        session_path.unlink()
        assert pane_session_record_diff(before_delete, snapshot_pane_session_paths([session_dir])) == [
            f"{session_dir}/pgu-ops_0.0.json"
        ]

        before_add = snapshot_pane_session_paths([session_dir])
        session_path.write_text(
            json.dumps({"target": "pgu-ops:0.0", "session_id": "new-session", "source": "codex.SessionStart"}) + "\n",
            encoding="utf-8",
        )
        assert pane_session_record_diff(before_add, snapshot_pane_session_paths([session_dir])) == [
            f"{session_dir}/pgu-ops_0.0.json"
        ]


def test_pane_state_snapshot_ignores_content_churn_but_detects_file_set_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        state_dir = Path(tmp) / "pane-state"
        state_dir.mkdir()
        ops_path = state_dir / "pgu-ops_0.0.json"
        audit_path = state_dir / "pgu-audit_0.0.json"
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")

        content_before_modify = snapshot_paths([state_dir])
        before_modify = snapshot_paths_file_set([state_dir])
        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "busy"}) + "\n", encoding="utf-8")
        assert snapshot_diff(content_before_modify, snapshot_paths([state_dir])) == [f"{state_dir}/pgu-ops_0.0.json"]
        assert snapshot_file_set_diff(before_modify, snapshot_paths_file_set([state_dir])) == []

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        before_delete = snapshot_paths_file_set([state_dir])
        ops_path.unlink()
        assert snapshot_file_set_diff(before_delete, snapshot_paths_file_set([state_dir])) == [f"{state_dir}/pgu-ops_0.0.json"]

        ops_path.write_text(json.dumps({"target": "pgu-ops:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        before_add = snapshot_paths_file_set([state_dir])
        audit_path.write_text(json.dumps({"target": "pgu-audit:0.0", "state": "idle"}) + "\n", encoding="utf-8")
        assert snapshot_file_set_diff(before_add, snapshot_paths_file_set([state_dir])) == [f"{state_dir}/pgu-audit_0.0.json"]


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


def test_compact_session_start_injects_assigned_in_progress_ticket_context() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        with _board_with_tickets(
            [
                {"id": "PGU-557", "title": "Resume compacted work", "state": "in_progress", "assignee": "main"},
                {"id": "PGU-556", "title": "Other role", "state": "in_progress", "assignee": "ops"},
            ]
        ) as board_url:
            proc = subprocess.run(
                [
                    str(ROOT / "scripts" / HOOK_NAME),
                    "idle",
                    "--target",
                    "pgu-main:0.0",
                    "--source",
                    "claude.SessionStart",
                    "--state-dir",
                    str(state_dir),
                    "--session-dir",
                    str(session_dir),
                    "--record-session",
                ],
                input=json.dumps({"source": "compact", "session_id": "compact-session-1"}),
                text=True,
                capture_output=True,
                check=True,
                env=_hook_env(TICKET_BOARD_URL=board_url),
            )

        output = json.loads(proc.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "Your context was just compacted." in context
        assert "ACTIVE work: PGU-557 -- Resume compacted work" in context
        assert f"curl {board_url}/api/tickets/PGU-557" in context
        assert "do NOT wait for a new prompt" in context
        state = json.loads((state_dir / "pgu-main_0.0.json").read_text(encoding="utf-8"))
        assert state["state"] == "idle"
        session = json.loads((session_dir / "pgu-main_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == "compact-session-1"


def test_session_start_resume_injection_is_scoped_to_resume_sources_and_active_ticket() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        with _board_with_tickets(
            [{"id": "PGU-557", "title": "Resume compacted work", "state": "in_progress", "assignee": "main"}]
        ) as board_url:
            startup = subprocess.run(
                [
                    str(ROOT / "scripts" / HOOK_NAME),
                    "idle",
                    "--target",
                    "pgu-main:0.0",
                    "--source",
                    "claude.SessionStart",
                    "--state-dir",
                    str(state_dir),
                    "--session-dir",
                    str(session_dir),
                    "--record-session",
                ],
                input=json.dumps({"source": "startup", "session_id": "startup-session"}),
                text=True,
                capture_output=True,
                check=True,
                env=_hook_env(TICKET_BOARD_URL=board_url),
            )
        with _board_with_tickets(
            [{"id": "PGU-557", "title": "Resume compacted work", "state": "in_progress", "assignee": "main"}]
        ) as board_url:
            resume = subprocess.run(
                [
                    str(ROOT / "scripts" / HOOK_NAME),
                    "idle",
                    "--target",
                    "pgu-main:0.0",
                    "--source",
                    "claude.SessionStart",
                    "--state-dir",
                    str(state_dir),
                    "--session-dir",
                    str(session_dir),
                    "--record-session",
                ],
                input=json.dumps({"source": "resume", "session_id": "resume-session"}),
                text=True,
                capture_output=True,
                check=True,
                env=_hook_env(TICKET_BOARD_URL=board_url),
            )
        with _board_with_tickets(
            [{"id": "PGU-557", "title": "Not active", "state": "audit", "assignee": "main"}]
        ) as board_url:
            no_ticket = subprocess.run(
                [
                    str(ROOT / "scripts" / HOOK_NAME),
                    "idle",
                    "--target",
                    "pgu-main:0.0",
                    "--source",
                    "claude.SessionStart",
                    "--state-dir",
                    str(state_dir),
                    "--session-dir",
                    str(session_dir),
                    "--record-session",
                ],
                input=json.dumps({"source": "resume", "session_id": "resume-session"}),
                text=True,
                capture_output=True,
                check=True,
                env=_hook_env(TICKET_BOARD_URL=board_url),
            )

    assert startup.stdout == ""
    resume_context = json.loads(resume.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "Your session just resumed." in resume_context
    assert "Your context was just compacted." not in resume_context
    assert "ACTIVE work: PGU-557 -- Resume compacted work" in resume_context
    assert no_ticket.stdout == ""


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


def test_session_start_records_hermes_timestamp_session_id_from_documented_session_id_key() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        home = Path(tmp) / "home"
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        subprocess.run([str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)], check=True)

        session_id = "20260823_140512_b49a1a"
        subprocess.run(
            [
                str(bin_path),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "hermes.on_session_start",
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


def test_codex_installed_hooks_embed_project_state_and_session_dirs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        state_dir = tmp_path / "run" / "otto-ticket-board" / "pane-state"
        session_dir = tmp_path / "state" / "otto-ticket-board" / "pane-sessions"
        install_env = _hook_env(
            TICKET_BOARD_PROJECT="otto",
            TICKET_BOARD_PANE_STATE_DIR=str(state_dir),
            TICKET_BOARD_PANE_SESSION_DIR=str(session_dir),
        )
        subprocess.run(
            [str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)],
            check=True,
            env=install_env,
        )

        codex_commands = _managed_commands_by_source(_load(home / ".codex" / "hooks.json"))
        session_start = codex_commands["codex.SessionStart"]
        stop = codex_commands["codex.Stop"]
        for command in (session_start, stop):
            assert f"--state-dir {state_dir}" in command
            assert f"--session-dir {session_dir}" in command

        hook_runtime_env = _hook_env(
            HOME=str(home),
            TICKET_BOARD_PANE_TARGET="otto-ops:0.0",
            CODEX_SESSION_ID="codex_otto_session_456",
        )
        hook_runtime_env.pop("TICKET_BOARD_PANE_STATE_DIR", None)
        hook_runtime_env.pop("TICKET_BOARD_PANE_SESSION_DIR", None)
        hook_runtime_env.pop("PGU_TICKET_BOARD_PANE_STATE_DIR", None)
        hook_runtime_env.pop("PGU_TICKET_BOARD_PANE_SESSION_DIR", None)

        subprocess.run(["sh", "-c", session_start], check=True, env=hook_runtime_env)
        subprocess.run(["sh", "-c", stop], check=True, env=hook_runtime_env)

        session = json.loads((session_dir / "otto-ops_0.0.json").read_text(encoding="utf-8"))
        state = json.loads((state_dir / "otto-ops_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == "codex_otto_session_456"
        assert session["source"] == "codex.SessionStart"
        assert state["state"] == "idle"
        assert state["source"] == "codex.Stop"
        assert not (home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions" / "otto-ops_0.0.json").exists()


def test_claude_installed_hooks_embed_project_state_and_session_dirs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        state_dir = tmp_path / "run" / "otto-ticket-board" / "pane-state"
        session_dir = tmp_path / "state" / "otto-ticket-board" / "pane-sessions"
        install_env = _hook_env(
            TICKET_BOARD_PROJECT="otto",
            TICKET_BOARD_PANE_STATE_DIR=str(state_dir),
            TICKET_BOARD_PANE_SESSION_DIR=str(session_dir),
        )
        subprocess.run(
            [str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)],
            check=True,
            env=install_env,
        )

        claude_commands = _managed_commands_by_source(_load(home / ".claude" / "settings.json"))
        session_start = claude_commands["claude.SessionStart"]
        stop = claude_commands["claude.Stop"]
        for command in (session_start, stop):
            assert f"--state-dir {state_dir}" in command
            assert f"--session-dir {session_dir}" in command

        hook_runtime_env = _without_hook_dir_env(
            _hook_env(
                HOME=str(home),
                TICKET_BOARD_PANE_TARGET="otto-director:0.0",
                CLAUDE_SESSION_ID="claude_otto_session_456",
            )
        )
        subprocess.run(
            ["sh", "-c", session_start],
            input=json.dumps({"session_id": "claude_otto_session_456"}),
            text=True,
            check=True,
            env=hook_runtime_env,
        )
        subprocess.run(["sh", "-c", stop], check=True, env=hook_runtime_env)

        session = json.loads((session_dir / "otto-director_0.0.json").read_text(encoding="utf-8"))
        state = json.loads((state_dir / "otto-director_0.0.json").read_text(encoding="utf-8"))
        assert session["session_id"] == "claude_otto_session_456"
        assert session["source"] == "claude.SessionStart"
        assert state["state"] == "idle"
        assert state["source"] == "claude.Stop"
        assert not (home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions" / "otto-director_0.0.json").exists()


def test_gemini_installed_hooks_embed_project_state_and_session_dirs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        state_dir = tmp_path / "run" / "otto-ticket-board" / "pane-state"
        session_dir = tmp_path / "state" / "otto-ticket-board" / "pane-sessions"
        install_env = _hook_env(
            TICKET_BOARD_PROJECT="otto",
            TICKET_BOARD_PANE_STATE_DIR=str(state_dir),
            TICKET_BOARD_PANE_SESSION_DIR=str(session_dir),
        )
        subprocess.run(
            [str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)],
            check=True,
            env=install_env,
        )

        gemini_commands = _managed_commands_by_source(_load(home / ".gemini" / "config" / "hooks.json"))
        session_start = gemini_commands["gemini.SessionStart"]
        stop = gemini_commands["gemini.Stop"]
        for command in (session_start, stop):
            assert f"--state-dir {state_dir}" in command
            assert f"--session-dir {session_dir}" in command

        hook_runtime_env = _without_hook_dir_env(
            _hook_env(
                HOME=str(home),
                TICKET_BOARD_PANE_TARGET="otto-inspector:0.0",
                GEMINI_SESSION_ID="gemini_otto_session_456",
            )
        )
        start = subprocess.run(
            ["sh", "-c", session_start],
            input=json.dumps({"session_id": "gemini_otto_session_456"}),
            text=True,
            capture_output=True,
            check=True,
            env=hook_runtime_env,
        )
        subprocess.run(["sh", "-c", stop], text=True, capture_output=True, check=True, env=hook_runtime_env)

        session = json.loads((session_dir / "otto-inspector_0.0.json").read_text(encoding="utf-8"))
        state = json.loads((state_dir / "otto-inspector_0.0.json").read_text(encoding="utf-8"))
        assert start.stdout == "{}"
        assert session["session_id"] == "gemini_otto_session_456"
        assert session["source"] == "gemini.SessionStart"
        assert state["state"] == "idle"
        assert state["source"] == "gemini.Stop"
        assert not (home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions" / "otto-inspector_0.0.json").exists()


def test_hermes_installed_hooks_embed_project_state_and_session_dirs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        bin_path = home / ".local" / "bin" / HOOK_NAME
        state_dir = tmp_path / "run" / "otto-ticket-board" / "pane-state"
        session_dir = tmp_path / "state" / "otto-ticket-board" / "pane-sessions"
        install_env = _hook_env(
            TICKET_BOARD_PROJECT="otto",
            TICKET_BOARD_PANE_STATE_DIR=str(state_dir),
            TICKET_BOARD_PANE_SESSION_DIR=str(session_dir),
        )
        subprocess.run(
            [str(INSTALLER), "install", "--home", str(home), "--bin-path", str(bin_path)],
            check=True,
            env=install_env,
        )

        hermes_commands = _managed_commands_by_source(_load_yaml(home / ".hermes" / "config.yaml"))
        session_start = hermes_commands["hermes.on_session_start"]
        stream_start = hermes_commands["hermes.on_stream_start"]
        stream_end = hermes_commands["hermes.on_stream_end"]
        session_end = hermes_commands["hermes.on_session_end"]
        session_finalize = hermes_commands["hermes.on_session_finalize"]
        for command in (session_start, stream_start, stream_end, session_end, session_finalize):
            assert f"--state-dir {state_dir}" in command
            assert f"--session-dir {session_dir}" in command

        hook_runtime_env = _without_hook_dir_env(
            _hook_env(
                HOME=str(home),
                TICKET_BOARD_PANE_TARGET="otto-bulk:0.0",
            )
        )
        start = subprocess.run(
            ["sh", "-c", session_start],
            input=json.dumps({"session_id": "20260823_140512_b49a1a"}),
            text=True,
            capture_output=True,
            check=True,
            env=hook_runtime_env,
        )
        subprocess.run(["sh", "-c", stream_start], text=True, capture_output=True, check=True, env=hook_runtime_env)
        busy_state = json.loads((state_dir / "otto-bulk_0.0.json").read_text(encoding="utf-8"))
        subprocess.run(["sh", "-c", stream_end], text=True, capture_output=True, check=True, env=hook_runtime_env)
        idle_state = json.loads((state_dir / "otto-bulk_0.0.json").read_text(encoding="utf-8"))
        subprocess.run(["sh", "-c", session_end], text=True, capture_output=True, check=True, env=hook_runtime_env)
        subprocess.run(["sh", "-c", session_finalize], text=True, capture_output=True, check=True, env=hook_runtime_env)

        session = json.loads((session_dir / "otto-bulk_0.0.json").read_text(encoding="utf-8"))
        state = json.loads((state_dir / "otto-bulk_0.0.json").read_text(encoding="utf-8"))
        assert start.stdout == ""
        assert session["session_id"] == "20260823_140512_b49a1a"
        assert session["source"] == "hermes.on_session_start"
        assert busy_state["state"] == "busy"
        assert busy_state["source"] == "hermes.on_stream_start"
        assert idle_state["state"] == "idle"
        assert idle_state["source"] == "hermes.on_stream_end"
        assert state["state"] == "idle"
        assert state["source"] == "hermes.on_session_finalize"
        assert not (home / ".local" / "state" / "pgu-ticket-board" / "pane-sessions" / "otto-bulk_0.0.json").exists()


def test_hook_default_dirs_derive_project_from_tmux_target_without_pgu_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "run"
        state_home = tmp_path / "state"
        env = _without_hook_dir_env(
            _hook_env(
                XDG_RUNTIME_DIR=str(runtime_dir),
                XDG_STATE_HOME=str(state_home),
                CODEX_SESSION_ID="codex_otto_default_session_456",
            )
        )

        subprocess.run(
            [
                str(ROOT / "scripts" / HOOK_NAME),
                "idle",
                "--target",
                "otto-ops:0.0",
                "--source",
                "codex.SessionStart",
                "--record-session",
            ],
            check=True,
            env=env,
        )

        otto_state = runtime_dir / "otto-ticket-board" / "pane-state" / "otto-ops_0.0.json"
        otto_session = state_home / "otto-ticket-board" / "pane-sessions" / "otto-ops_0.0.json"
        pgu_state = runtime_dir / "pgu-ticket-board" / "pane-state" / "otto-ops_0.0.json"
        pgu_session = state_home / "pgu-ticket-board" / "pane-sessions" / "otto-ops_0.0.json"
        assert json.loads(otto_state.read_text(encoding="utf-8"))["target"] == "otto-ops:0.0"
        assert json.loads(otto_session.read_text(encoding="utf-8"))["session_id"] == "codex_otto_default_session_456"
        assert not pgu_state.exists()
        assert not pgu_session.exists()


def test_pgu_hook_default_dirs_remain_pgu_named() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "run"
        state_home = tmp_path / "state"
        env = _without_hook_dir_env(
            _hook_env(
                XDG_RUNTIME_DIR=str(runtime_dir),
                XDG_STATE_HOME=str(state_home),
                CODEX_SESSION_ID="codex_pgu_default_session_456",
            )
        )

        subprocess.run(
            [
                str(ROOT / "scripts" / HOOK_NAME),
                "idle",
                "--target",
                "pgu-ops:0.0",
                "--source",
                "codex.SessionStart",
                "--record-session",
            ],
            check=True,
            env=env,
        )

        pgu_state = runtime_dir / "pgu-ticket-board" / "pane-state" / "pgu-ops_0.0.json"
        pgu_session = state_home / "pgu-ticket-board" / "pane-sessions" / "pgu-ops_0.0.json"
        assert json.loads(pgu_state.read_text(encoding="utf-8"))["target"] == "pgu-ops:0.0"
        assert json.loads(pgu_session.read_text(encoding="utf-8"))["session_id"] == "codex_pgu_default_session_456"


def test_hook_project_env_disambiguates_hyphenated_project_and_role() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "run"
        state_home = tmp_path / "state"
        target = "my-cool-thing-code-review:0.0"
        env = _hook_env(
            TICKET_BOARD_PROJECT="my-cool-thing",
            XDG_RUNTIME_DIR=str(runtime_dir),
            XDG_STATE_HOME=str(state_home),
            CODEX_SESSION_ID="codex_hyphenated_role_session_456",
        )
        with _board_with_tickets(
            [
                {
                    "id": "PGU-72201",
                    "title": "Hyphenated role",
                    "state": "in_progress",
                    "assignee": "code-review",
                }
            ]
        ) as board_url:
            env["TICKET_BOARD_URL"] = board_url
            proc = subprocess.run(
                [
                    str(ROOT / "scripts" / HOOK_NAME),
                    "idle",
                    "--target",
                    target,
                    "--source",
                    "codex.SessionStart",
                    "--record-session",
                ],
                input=json.dumps({"source": "compact"}),
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )

        state_path = runtime_dir / "my-cool-thing-ticket-board" / "pane-state" / "my-cool-thing-code-review_0.0.json"
        session_path = (
            state_home
            / "my-cool-thing-ticket-board"
            / "pane-sessions"
            / "my-cool-thing-code-review_0.0.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        session = json.loads(session_path.read_text(encoding="utf-8"))
        output = json.loads(proc.stdout)
        additional_context = output["hookSpecificOutput"]["additionalContext"]
        assert state["target"] == target
        assert state["source"] == "codex.SessionStart"
        assert session["target"] == target
        assert session["session_id"] == "codex_hyphenated_role_session_456"
        assert "PGU-72201 -- Hyphenated role" in additional_context
        assert "assigned to you" in additional_context
        assert not (runtime_dir / "my-cool-thing-code-ticket-board").exists()
        assert not (state_home / "my-cool-thing-code-ticket-board").exists()


def test_hook_hyphenated_target_without_project_env_uses_first_dash_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "run"
        state_home = tmp_path / "state"
        env = _without_hook_dir_env(
            _hook_env(
                XDG_RUNTIME_DIR=str(runtime_dir),
                XDG_STATE_HOME=str(state_home),
                CODEX_SESSION_ID="codex_ambiguous_session_456",
            )
        )

        proc = subprocess.run(
            [
                str(ROOT / "scripts" / HOOK_NAME),
                "idle",
                "--target",
                "mycoolthing-code-review:0.0",
                "--source",
                "codex.SessionStart",
                "--record-session",
            ],
            text=True,
            capture_output=True,
            env=env,
        )

        assert proc.returncode == 0
        assert proc.stderr == ""
        assert (runtime_dir / "mycoolthing-ticket-board" / "pane-state" / "mycoolthing-code-review_0.0.json").is_file()
        assert (state_home / "mycoolthing-ticket-board" / "pane-sessions" / "mycoolthing-code-review_0.0.json").is_file()


def test_hook_session_start_refuses_role_when_target_lacks_project_prefix() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "run"
        state_home = tmp_path / "state"
        env = _hook_env(
            TICKET_BOARD_PROJECT="mycoolthing",
            XDG_RUNTIME_DIR=str(runtime_dir),
            XDG_STATE_HOME=str(state_home),
            CODEX_SESSION_ID="codex_wrong_prefix_session_456",
        )
        with _board_with_tickets(
            [
                {
                    "id": "PGU-72202",
                    "title": "Wrong prefix should not match",
                    "state": "in_progress",
                    "assignee": "-ops",
                }
            ]
        ) as board_url:
            env["TICKET_BOARD_URL"] = board_url
            proc = subprocess.run(
                [
                    str(ROOT / "scripts" / HOOK_NAME),
                    "idle",
                    "--target",
                    "other-project-ops:0.0",
                    "--source",
                    "codex.SessionStart",
                    "--record-session",
                ],
                input=json.dumps({"source": "compact"}),
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )

        assert proc.stdout == ""
        assert (runtime_dir / "mycoolthing-ticket-board" / "pane-state" / "other-project-ops_0.0.json").is_file()
        assert (state_home / "mycoolthing-ticket-board" / "pane-sessions" / "other-project-ops_0.0.json").is_file()


def test_hook_role_for_target_refuses_wrong_project_prefix() -> None:
    hook = _load_hook_module()

    assert hook._role_for_target("other-project-ops:0.0", {"TICKET_BOARD_PROJECT": "mycoolthing"}) == ""


def test_hook_no_env_target_fallback_splits_first_dash_for_hyphenated_role() -> None:
    hook = _load_hook_module()

    assert hook._project_from_target("otto-code-review:0.0") == "otto"
    assert hook._role_for_target("otto-code-review:0.0", {}) == "code-review"


def test_hook_unresolved_default_dirs_report_but_do_not_fail_startup() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks.") as tmp:
        tmp_path = Path(tmp)
        runtime_dir = tmp_path / "run"
        state_home = tmp_path / "state"
        env = _without_hook_dir_env(
            _hook_env(
                XDG_RUNTIME_DIR=str(runtime_dir),
                XDG_STATE_HOME=str(state_home),
                CODEX_SESSION_ID="codex_unknown_session_456",
            )
        )

        proc = subprocess.run(
            [
                str(ROOT / "scripts" / HOOK_NAME),
                "idle",
                "--target",
                "ops:0.0",
                "--source",
                "codex.SessionStart",
                "--record-session",
            ],
            text=True,
            capture_output=True,
            env=env,
        )

        assert proc.returncode == 0
        assert "cannot determine pane state directory" in proc.stderr
        assert "cannot determine pane session directory" in proc.stderr
        assert not runtime_dir.exists()
        assert not state_home.exists()


def test_hook_without_target_is_silent_noop() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks-no-target.") as tmp:
        tmp_path = Path(tmp)
        state_dir = tmp_path / "state"
        session_dir = tmp_path / "sessions"
        env = _hook_env(
            XDG_RUNTIME_DIR=str(tmp_path / "run"),
            XDG_STATE_HOME=str(tmp_path / "state-home"),
        )
        env.pop("TICKET_BOARD_PANE_TARGET", None)
        env.pop("PGU_PANE_TARGET", None)
        env.pop("TMUX_PANE", None)
        env.pop("TICKET_BOARD_PROJECT", None)
        env.pop("PGU_TICKET_BOARD_PROJECT", None)

        proc = subprocess.run(
            [
                str(ROOT / "scripts" / HOOK_NAME),
                "idle",
                "--source",
                "codex.SessionStart",
                "--state-dir",
                str(state_dir),
                "--session-dir",
                str(session_dir),
                "--record-session",
            ],
            input=json.dumps({"session_id": "human-session"}),
            text=True,
            capture_output=True,
            env=env,
        )

        assert proc.returncode == 0
        assert proc.stdout == ""
        assert proc.stderr == ""
        assert not state_dir.exists()
        assert not session_dir.exists()

        no_dir_proc = subprocess.run(
            [
                str(ROOT / "scripts" / HOOK_NAME),
                "idle",
                "--source",
                "codex.SessionStart",
                "--record-session",
            ],
            input=json.dumps({"session_id": "human-session"}),
            text=True,
            capture_output=True,
            env=env,
        )

        assert no_dir_proc.returncode == 0
        assert no_dir_proc.stdout == ""
        assert no_dir_proc.stderr == ""
        assert not (tmp_path / "run").exists()
        assert not (tmp_path / "state-home").exists()


def test_hook_ignores_tmux_pane_without_explicit_target() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="pgu-pane-hooks-tmux-no-target.") as tmp:
        tmp_path = Path(tmp)
        server = f"pgu731-hook-{os.getpid()}"
        session = "work"
        done_signal = "pgu731-hook-done"
        state_dir = tmp_path / "state"
        session_dir = tmp_path / "sessions"
        out_path = tmp_path / "hook.out"
        err_path = tmp_path / "hook.err"
        status_path = tmp_path / "hook.status"
        hook_path = ROOT / "scripts" / HOOK_NAME
        runner = tmp_path / "run-hook.sh"
        runner.write_text(
            "#!/bin/sh\n"
            "printf '%s' '{\"session_id\":\"human-session\"}' | "
            "env "
            "-u TICKET_BOARD_PANE_TARGET "
            "-u PGU_PANE_TARGET "
            "-u TICKET_BOARD_PROJECT "
            "-u PGU_TICKET_BOARD_PROJECT "
            "-u TICKET_BOARD_PANE_STATE_DIR "
            "-u PGU_TICKET_BOARD_PANE_STATE_DIR "
            "-u TICKET_BOARD_PANE_SESSION_DIR "
            "-u PGU_TICKET_BOARD_PANE_SESSION_DIR "
            "-u TICKET_BOARD_PANE_SESSION_ID "
            "-u PGU_PANE_SESSION_ID "
            "-u CODEX_SESSION_ID "
            "-u CLAUDE_SESSION_ID "
            f"HOME={shlex.quote(str(tmp_path / 'home'))} "
            f"XDG_RUNTIME_DIR={shlex.quote(str(tmp_path / 'run'))} "
            f"XDG_STATE_HOME={shlex.quote(str(tmp_path / 'state-home'))} "
            f"{shlex.quote(str(hook_path))} idle "
            "--source codex.Stop "
            f"--state-dir {shlex.quote(str(state_dir))} "
            f"--session-dir {shlex.quote(str(session_dir))} "
            "--record-session "
            f">{shlex.quote(str(out_path))} 2>{shlex.quote(str(err_path))}\n"
            f"printf '%s\\n' \"$?\" >{shlex.quote(str(status_path))}\n"
            f"env -u TMUX tmux -L {shlex.quote(server)} wait-for -S {shlex.quote(done_signal)}\n",
            encoding="utf-8",
        )
        runner.chmod(0o755)

        try:
            subprocess.run(
                ["tmux", "-L", server, "new-session", "-d", "-s", session, str(runner)],
                check=True,
                text=True,
                capture_output=True,
            )
            subprocess.run(
                ["tmux", "-L", server, "wait-for", done_signal],
                check=True,
                timeout=5.0,
                text=True,
                capture_output=True,
            )
        finally:
            subprocess.run(
                ["tmux", "-L", server, "kill-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        assert status_path.read_text(encoding="utf-8").strip() == "0"
        assert out_path.read_text(encoding="utf-8") == ""
        assert err_path.read_text(encoding="utf-8") == ""
        assert not state_dir.exists()
        assert not session_dir.exists()
        assert not (tmp_path / "run").exists()
        assert not (tmp_path / "state-home").exists()


def main() -> int:
    live_session_paths = candidate_live_pane_session_paths()
    live_pane_state_paths = candidate_live_pane_state_paths()
    live_session_snapshot = snapshot_pane_session_paths(live_session_paths)
    live_pane_state_snapshot = snapshot_paths_file_set(live_pane_state_paths)
    live_pane_state_anomalies = pane_state_record_anomalies(live_pane_state_paths)
    try:
        run_module_tests(globals())
    finally:
        changed = [
            *pane_session_record_diff(live_session_snapshot, snapshot_pane_session_paths(live_session_paths)),
            *snapshot_file_set_diff(live_pane_state_snapshot, snapshot_paths_file_set(live_pane_state_paths)),
            *pane_state_record_anomaly_diff(
                live_pane_state_anomalies,
                pane_state_record_anomalies(live_pane_state_paths),
            ),
        ]
        assert not changed, (
            changed,
            "Live pane hook state changed while the suite ran. This can also fire if a live pane rewrote "
            "its own record while the suite ran; re-run on a quiet fleet to distinguish.",
        )
    print("ticket_board_pane_hooks_install_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
