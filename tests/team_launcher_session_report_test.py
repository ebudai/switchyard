#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

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


def test_launch_session_record_report_attached_roles_without_fresh_record_warnings() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-attached.") as tmp:
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
                        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
                        {"role": "ops", "slot": 1, "cli": ["codex"], "target": "porter-ops:0.0"},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            pane_state_dir=tmp_path / "pane-state",
            pane_state_updated_since=time.time(),
            attached_roles=config.roles,
            print_func=messages.append,
        )

    assert all(status.attached_to_running for status in statuses)
    assert messages == [
        "switchyard: attached to running pane for main (porter-main:0.0)",
        "switchyard: attached to running pane for ops (porter-ops:0.0)",
    ]
    assert not any("session record missing" in message for message in messages)
    assert not any("codex runtime hook did not report" in message for message in messages)


def test_launch_project_reports_running_attach_or_start_panes_as_attached() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-running-attach.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
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
                        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
                        {"role": "ops", "slot": 1, "cli": ["codex"], "target": "porter-ops:0.0"},
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        runner = FakeRunner(existing_sessions={"porter-main", "porter-ops"})
        process_launcher = RecordingProcessLauncher()
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
                konsole_process_launcher=process_launcher,
                print_func=messages.append,
            )
            == 0
        )

    assert any("opened a new window attached to running panes: main, ops" in message for message in messages)
    assert any("switchyard: attached to running pane for main (porter-main:0.0)" == message for message in messages)
    assert any("switchyard: attached to running pane for ops (porter-ops:0.0)" == message for message in messages)
    assert not any("session record missing" in message for message in messages)
    assert not any("codex runtime hook did not report" in message for message in messages)
    assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)


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
    assert not any("codex runtime hook did not report" in message for message in messages)

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
    assert not any("codex runtime hook did not report" in message for message in messages)

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

def test_launch_session_record_report_aggregates_expected_idle_codex_startup_warning() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-codex-idle-startup.") as tmp:
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
                        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
                        {"role": "app", "slot": 1, "cli": ["codex"], "target": "porter-app:0.0"},
                        {"role": "ops", "slot": 2, "cli": ["codex"], "target": "porter-ops:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        pane_state_since = time.time()
        for role in config.roles:
            team_launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=pane_state_since,
            print_func=messages.append,
        )

    assert all(status.pane_state_source == "team_launcher.start" for status in statuses)
    assert all(status.session_id == "" for status in statuses)
    warning_messages = [message for message in messages if message.startswith("warning: switchyard:")]
    assert len(warning_messages) == 1
    assert "codex runtime hook did not report for 3 pane(s)" in warning_messages[0]
    assert "main (porter-main:0.0)" in warning_messages[0]
    assert "app (porter-app:0.0)" in warning_messages[0]
    assert "ops (porter-ops:0.0)" in warning_messages[0]
    assert "session record missing" not in warning_messages[0]

def test_launch_session_record_report_aggregates_codex_warning_with_existing_records() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-codex-existing-records.") as tmp:
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
                        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
                        {"role": "app", "slot": 1, "cli": ["codex"], "target": "porter-app:0.0"},
                        {"role": "ops", "slot": 2, "cli": ["codex"], "target": "porter-ops:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        pane_state_since = time.time()
        for role in config.roles:
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": f"{role.role}-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=pane_state_since,
            print_func=messages.append,
        )

    assert all(status.pane_state_source == "team_launcher.start" for status in statuses)
    assert all(status.session_id.endswith("-session") for status in statuses)
    warning_messages = [message for message in messages if message.startswith("warning: switchyard:")]
    assert len(warning_messages) == 1
    assert "codex runtime hook did not report for 3 pane(s)" in warning_messages[0]
    assert not any("codex runtime hook did not report for main" in message for message in messages)
    assert not any("codex runtime hook did not report for app" in message for message in messages)
    assert not any("codex runtime hook did not report for ops" in message for message in messages)

def test_launch_session_record_report_warns_when_codex_runtime_hook_never_reports() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-codex-hook-missing.") as tmp:
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
                        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        pane_state_since = time.time()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "existing-session"}) + "\n",
            encoding="utf-8",
        )
        team_launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=pane_state_since,
            print_func=messages.append,
        )

    assert statuses[0].pane_state_source == "team_launcher.start"
    assert statuses[0].runtime_hook_source == ""
    assert any("warning: switchyard: codex runtime hook did not report for 1 pane(s): main" in message for message in messages)
    assert any("expected fresh codex.* pane state" in message for message in messages)

def test_launch_session_record_statuses_wait_for_codex_runtime_hook_after_launcher_seed() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-codex-hook-wait.") as tmp:
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
                        {"role": "main", "slot": 0, "cli": ["codex"], "target": "porter-main:0.0"},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        role = config.roles[0]
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "existing-session"}) + "\n",
            encoding="utf-8",
        )
        pane_state_since = time.time()
        team_launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
        started = time.monotonic()

        def write_codex_hook_state() -> None:
            time.sleep(0.05)
            team_launcher.seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="codex.SessionStart")

        writer = threading.Thread(target=write_codex_hook_state)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)
        elapsed = time.monotonic() - started

    assert elapsed >= 0.04
    assert elapsed < 0.3
    assert statuses[0].pane_state_source == ""
    assert statuses[0].runtime_hook_source == "codex.SessionStart"

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_session_report_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
