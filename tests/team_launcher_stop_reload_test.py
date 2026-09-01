#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_detach_visible_role_updates_config_and_leaves_session_running() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface-detach.") as tmp:
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
        config_path = tmp_path / "porter.json"
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
                        {"role": "worker7", "slot": 1, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        runner = FakeRunner(existing_sessions={"porter-director", "porter-worker7"})
        messages: list[str] = []

        assert (
            team_launcher.detach_role_from_slot(
                config,
                config_path=config_path,
                role_name="worker7",
                runner=runner,
                print_func=messages.append,
            )
            == 0
        )

        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        roles = {role["role"]: role for role in raw_config["roles"]}
        assert roles["director"]["slot"] == 0
        assert roles["worker7"]["detached"] is True
        assert "slot" not in roles["worker7"]
        assert runner.existing_sessions == {"porter-director", "porter-worker7"}
        assert ["tmux", "detach-client", "-s", "porter-worker7"] in runner.calls
        assert not any(call[:2] == ["tmux", "kill-session"] for call in runner.calls)
        assert messages == ["team-launcher: detached role worker7 from slot 1; tmux session remains headless"]

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
        assert ["tmux", "set-option", "-t", "pgu-ops", "mouse", "on"] in runner.calls
        assert ["tmux", "set-option", "-t", "pgu-ops", "history-limit", "200000"] in runner.calls
        assert ["tmux", "attach", "-t", "pgu-ops"] in runner.calls

def test_hermes_reload_supersedes_recorded_session_and_starts_fresh() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-hermes-fresh.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {
                            "role": "bulk",
                            "slot": 0,
                            "target": "porter-bulk:0.0",
                            "tmux_session": "porter-bulk",
                            "cli": ["hermes"],
                            "model": "zai/glm-5.3",
                            "yolo": True,
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
        role = config.roles[0]
        session_dir = config.session_dir
        session_dir.mkdir()
        session_path = session_dir / session_file_name(role.target)
        session_path.write_text(
            json.dumps({"target": role.target, "session_id": "previous-hermes-ticket-session"}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"porter-bulk"}, current_commands={"porter-bulk:0.0": "hermes"})
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = run_role_pane(
                role,
                mode="reload",
                session_dir=session_dir,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )

        assert result == 0
        assert not session_path.exists()
        sidecar_path = session_dir / f"{session_file_name(role.target)}.superseded"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["session_id"] == "previous-hermes-ticket-session"
        assert "uses hermes, which has no dependable reset" in stderr.getvalue()
        new_session = next(call for call in runner.calls if call[:2] == ["tmux", "new-session"])
        assert "--resume" not in new_session[-1]
        assert "previous-hermes-ticket-session" not in new_session[-1]
        assert "TICKET_BOARD_PANE_SESSION_ID=previous-hermes-ticket-session" not in new_session[-1]

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

    assert "recorded agy conversation" in stderr.getvalue()
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

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_stop_reload_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
