#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

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
        process_launcher = RecordingProcessLauncher()
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
                konsole_process_launcher=process_launcher,
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

def test_pane_start_reexecs_as_owner_before_detached_tmux_or_state_work() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-owner.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
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
                    "roles": [
                        {
                            "role": "research",
                            "detached": True,
                            "cli": ["claude"],
                            "target": "porter-research:0.0",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        calls: list[list[str]] = []
        original_run = team_launcher.subprocess.run
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

        def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        try:
            team_launcher.subprocess.run = fake_run
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            result = team_launcher.main(
                [
                    "porter",
                    "pane",
                    "start",
                    "research",
                    "--config",
                    str(config_path),
                    "--script-path",
                    str(ROOT / "scripts" / "team-launcher"),
                    "--allow-stale-launcher",
                ]
            )
        finally:
            team_launcher.subprocess.run = original_run
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        assert result == 0
        assert calls == [
            [
                "sudo",
                "-u",
                "porter-agent",
                "-H",
                str(ROOT / "scripts" / "team-launcher"),
                "porter",
                "pane",
                "start",
                "research",
                "--config",
                str(config_path),
                "--allow-stale-launcher",
                "--pane-state-dir",
                str(owner_runtime / "porter-ticket-board" / "pane-state"),
            ]
        ]
        assert not (tmp_path / "run" / "user" / "0" / "porter-ticket-board" / "pane-state").exists()

def test_pane_attach_role_reexecs_as_owner_with_slot_before_tmux_or_state_work() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-attach-role-owner.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text(
            json.dumps(
                {
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [
                        {
                            "role": "research",
                            "detached": True,
                            "cli": ["claude"],
                            "target": "porter-research:0.0",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        calls: list[list[str]] = []
        original_run = team_launcher.subprocess.run
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

        def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)

        try:
            team_launcher.subprocess.run = fake_run
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            result = team_launcher.main(
                [
                    "porter",
                    "pane",
                    "attach-role",
                    "research",
                    "--slot",
                    "1",
                    "--config",
                    str(config_path),
                    "--script-path",
                    str(ROOT / "scripts" / "team-launcher"),
                ]
            )
        finally:
            team_launcher.subprocess.run = original_run
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        assert result == 0
        assert calls == [
            [
                "sudo",
                "-u",
                "porter-agent",
                "-H",
                str(ROOT / "scripts" / "team-launcher"),
                "porter",
                "pane",
                "attach-role",
                "research",
                "--config",
                str(config_path),
                "--slot",
                "1",
                "--pane-state-dir",
                str(owner_runtime / "porter-ticket-board" / "pane-state"),
            ]
        ]
        assert not (tmp_path / "run" / "user" / "0" / "porter-ticket-board" / "pane-state").exists()

def test_pane_no_attach_uses_viewer_ensure_path_without_tmux_attach() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-pane-no-attach.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        session_dir = tmp_path / "sessions"
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(session_dir),
                    "roles": [
                        {
                            "role": "app",
                            "slot": 0,
                            "cli": ["codex"],
                            "target": "porter-app:0.0",
                            "tmux_session": "porter-app",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        pane_state_dir = tmp_path / "pane-state"
        calls: list[dict[str, Any]] = []
        original_ensure = team_launcher.ensure_visible_role_session_for_viewer
        original_run_role_pane = team_launcher.run_role_pane

        def fake_ensure(role: team_launcher.RoleConfig, **kwargs: Any) -> int:
            calls.append({"role": role.role, **kwargs})
            return 0

        def fake_run_role_pane(*_args: Any, **_kwargs: Any) -> int:
            raise AssertionError("pane --no-attach must not call run_role_pane")

        try:
            team_launcher.ensure_visible_role_session_for_viewer = fake_ensure
            team_launcher.run_role_pane = fake_run_role_pane
            result = team_launcher.main(
                [
                    "porter",
                    "pane",
                    "start",
                    "app",
                    "--config",
                    str(config_path),
                    "--skip-launcher-check",
                    "--no-attach",
                    "--pane-state-dir",
                    str(pane_state_dir),
                ]
            )
        finally:
            team_launcher.ensure_visible_role_session_for_viewer = original_ensure
            team_launcher.run_role_pane = original_run_role_pane

        assert result == 0
        assert len(calls) == 1
        assert calls[0]["role"] == "app"
        assert calls[0]["mode"] == "start"
        assert calls[0]["session_dir"] == session_dir
        assert calls[0]["pane_state_dir"] == pane_state_dir

def test_attach_role_config_write_failure_does_not_start_or_attach_session() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-attach-role-config-denied.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text(
            json.dumps(
                {
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                    ]
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
                        {
                            "role": "director",
                            "slot": 0,
                            "target": "porter-director:0.0",
                            "tmux_session": "porter-director",
                            "cli": ["claude"],
                        },
                        {
                            "role": "research",
                            "detached": True,
                            "target": "porter-research:0.0",
                            "tmux_session": "porter-research",
                            "cli": ["codex"],
                        },
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        research = next(role for role in config.roles if role.role == "research")
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = tmp_path / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (session_dir / session_file_name(research.target)).write_text(
            _session_payload(research.target, session_id, {"transcript_path": str(transcript)}),
            encoding="utf-8",
        )
        runner = FakeRunner()
        original_write_json_atomic = team_launcher._write_json_atomic

        def fake_write_json_atomic(_path: Path, _payload: dict[str, Any]) -> None:
            raise PermissionError("permission denied")

        try:
            team_launcher._write_json_atomic = fake_write_json_atomic
            try:
                team_launcher.attach_role_to_slot(
                    config,
                    config_path=config_path,
                    role_name="research",
                    slot=1,
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                raise AssertionError("expected config write failure")
            except SystemExit as exc:
                message = str(exc)
        finally:
            team_launcher._write_json_atomic = original_write_json_atomic

        assert "team-launcher: cannot update launcher config" in message
        assert str(config_path) in message
        assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)
        assert not any(call[:2] == ["tmux", "attach"] for call in runner.calls)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        roles = {role["role"]: role for role in raw_config["roles"]}
        assert roles["research"]["detached"] is True
        assert "slot" not in roles["research"]

def test_attach_headless_role_to_free_slot_resumes_without_touching_neighbours() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface.") as tmp:
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
                },
                indent=2,
                sort_keys=True,
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
                    "session_dir": str(tmp_path / "home" / ".local" / "state" / "porter-ticket-board" / "pane-sessions"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
                        {"role": "worker7", "detached": True, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        worker = next(role for role in config.roles if role.role == "worker7")
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = tmp_path / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        config.session_dir.mkdir(parents=True)
        (config.session_dir / session_file_name(worker.target)).write_text(
            _session_payload(worker.target, session_id, {"transcript_path": str(transcript)}),
            encoding="utf-8",
        )
        runner = FakeRunner()
        messages: list[str] = []

        assert (
            team_launcher.attach_role_to_slot(
                config,
                config_path=config_path,
                role_name="worker7",
                slot=1,
                session_dir=config.session_dir,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
                print_func=messages.append,
            )
            == 0
        )

        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        roles = {role["role"]: role for role in raw_config["roles"]}
        assert roles["director"]["slot"] == 0
        assert roles["worker7"]["slot"] == 1
        assert roles["worker7"]["detached"] is False
        assert (config.session_dir / session_file_name(worker.target)).exists()
        new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "porter-worker7"]]
        assert len(new_sessions) == 1
        assert f"resume {session_id}" in new_sessions[0][-1]
        assert not any(call[:5] == ["tmux", "new-session", "-d", "-s", "porter-director"] for call in runner.calls)
        assert not any(call[:2] == ["tmux", "kill-session"] for call in runner.calls)
        assert ["tmux", "attach", "-t", "porter-worker7"] in runner.calls
        assert messages == ["team-launcher: attached role worker7 to slot 1; refusing to relayout other panes"]

def test_attach_headless_role_refuses_occupied_slot_without_mutating_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface-occupied.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Widgets":[{"Command":"","SessionRestoreId":0,"WorkingDirectory":""}]}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        repo = tmp_path / "repo"
        repo.mkdir()
        payload = {
            "project": "porter",
            "layout": str(layout),
            "repository": str(repo),
            "roles": [
                {"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
                {"role": "worker7", "detached": True, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]},
            ],
        }
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("porter", config_path)
        before = config_path.read_text(encoding="utf-8")
        runner = FakeRunner()

        try:
            team_launcher.attach_role_to_slot(
                config,
                config_path=config_path,
                role_name="worker7",
                slot=0,
                session_dir=tmp_path / "sessions",
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )
            raise AssertionError("expected occupied slot failure")
        except SystemExit as exc:
            assert str(exc) == "team-launcher: cannot attach worker7 to slot 0; slot 0 is occupied by director"

        assert config_path.read_text(encoding="utf-8") == before
        assert not any(call[:2] in (["tmux", "new-session"], ["tmux", "kill-session"]) for call in runner.calls)

def test_attach_headless_role_refuses_seventh_visible_pane_without_mutating_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface-visible-cap.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        layout_path = tmp_path / "porter-layout.json"
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        layout["Widgets"].append({"Command": "", "SessionRestoreId": 6, "WorkingDirectory": ""})
        layout_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["roles"].append(
            {"role": "worker7", "detached": True, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]}
        )
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("porter", config_path)
        before = config_path.read_text(encoding="utf-8")
        runner = FakeRunner()

        try:
            team_launcher.attach_role_to_slot(
                config,
                config_path=config_path,
                role_name="worker7",
                slot=6,
                session_dir=tmp_path / "sessions",
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )
            raise AssertionError("expected seventh visible pane attach failure")
        except SystemExit as exc:
            message = str(exc)

        assert "cannot attach worker7" in message
        assert "at most 6 panes can be visible in one window" in message
        assert config_path.read_text(encoding="utf-8") == before
        assert not any(call[:2] in (["tmux", "new-session"], ["tmux", "kill-session"]) for call in runner.calls)

def test_attach_headless_role_without_live_session_or_record_refuses_fresh_start() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface-no-record.") as tmp:
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
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
                        {"role": "worker7", "detached": True, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        before = config_path.read_text(encoding="utf-8")
        runner = FakeRunner()
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                team_launcher.attach_role_to_slot(
                    config,
                    config_path=config_path,
                    role_name="worker7",
                    slot=1,
                    session_dir=config.session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 1
            )

        assert config_path.read_text(encoding="utf-8") == before
        assert stderr.getvalue() == "team-launcher: cannot attach worker7; no live session or recorded resume id\n"
        assert not any(call[:2] in (["tmux", "new-session"], ["tmux", "kill-session"]) for call in runner.calls)

def test_attach_headless_role_failed_start_rolls_config_back_to_detached() -> None:
    class FailedStartRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["tmux", "new-session", "-d"]:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 3)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface-start-fails.") as tmp:
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
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
                        {"role": "worker7", "detached": True, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        worker = next(role for role in config.roles if role.role == "worker7")
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = tmp_path / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        config.session_dir.mkdir(parents=True)
        (config.session_dir / session_file_name(worker.target)).write_text(
            _session_payload(worker.target, session_id, {"transcript_path": str(transcript)}),
            encoding="utf-8",
        )
        before = config_path.read_text(encoding="utf-8")
        runner = FailedStartRunner()
        messages: list[str] = []

        assert (
            team_launcher.attach_role_to_slot(
                config,
                config_path=config_path,
                role_name="worker7",
                slot=1,
                session_dir=config.session_dir,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
                print_func=messages.append,
            )
            == 3
        )

        assert config_path.read_text(encoding="utf-8") == before
        assert runner.existing_sessions == set()
        assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "porter-worker7"] for call in runner.calls)
        assert not any(call[:3] == ["tmux", "attach", "-t"] for call in runner.calls)
        assert messages == []

def test_attach_headless_role_unverified_resume_does_not_mutate_config() -> None:
    class UnverifiedResumeRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["tmux", "new-session", "-d"]:
                self.calls.append(args)
                self.existing_sessions.add(args[args.index("-s") + 1])
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-surface-unverified.") as tmp:
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
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
                        {"role": "worker7", "detached": True, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        worker = next(role for role in config.roles if role.role == "worker7")
        session_id = "12345678-1234-5678-9abc-def012345678"
        transcript = tmp_path / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        config.session_dir.mkdir(parents=True)
        (config.session_dir / session_file_name(worker.target)).write_text(
            _session_payload(worker.target, session_id, {"transcript_path": str(transcript)}),
            encoding="utf-8",
        )
        before = config_path.read_text(encoding="utf-8")
        runner = UnverifiedResumeRunner()
        messages: list[str] = []
        stderr = StringIO()
        original_timeout = team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS
        original_poll = team_launcher.RESUME_STARTUP_POLL_SECONDS

        try:
            team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.02
            team_launcher.RESUME_STARTUP_POLL_SECONDS = 0.001
            with redirect_stderr(stderr):
                assert (
                    team_launcher.attach_role_to_slot(
                        config,
                        config_path=config_path,
                        role_name="worker7",
                        slot=1,
                        session_dir=config.session_dir,
                        pane_state_dir=tmp_path / "pane-state",
                        runner=runner,
                        print_func=messages.append,
                    )
                    == 1
                )
        finally:
            team_launcher.RESUME_STARTUP_TIMEOUT_SECONDS = original_timeout
            team_launcher.RESUME_STARTUP_POLL_SECONDS = original_poll

        assert config_path.read_text(encoding="utf-8") == before
        assert runner.existing_sessions == {"porter-worker7"}
        assert (config.session_dir / session_file_name(worker.target)).exists()
        assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "porter-worker7"] for call in runner.calls)
        assert not any(call[:3] == ["tmux", "attach", "-t"] for call in runner.calls)
        assert not any(call[:2] == ["tmux", "kill-session"] for call in runner.calls)
        assert messages == []
        assert stderr.getvalue() == (
            f"team-launcher: resume for worker7 using session {session_id} was not verified; "
            "leaving tmux session and session record intact\n"
        )

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_reload_attach_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
