#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_pane_runtime_hook_source_rejects_stale_codex_state() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-codex-state.") as tmp:
        pane_state_dir = Path(tmp) / "pane-state"
        pane_state_dir.mkdir()
        (pane_state_dir / pane_state_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "state": "idle",
                    "updated_at": time.time() - 60.0,
                    "source": "codex.Stop",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        source = team_launcher.pane_runtime_hook_source_for_role(
            role,
            pane_state_dir,
            updated_since=time.time(),
        )

    assert source == ""

def test_pane_launch_outcome_source_rejects_stale_launcher_state() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-launcher-state.") as tmp:
        pane_state_dir = Path(tmp) / "pane-state"
        pane_state_dir.mkdir()
        (pane_state_dir / pane_state_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "state": "idle",
                    "updated_at": time.time() - 60.0,
                    "source": "team_launcher.start",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        source = team_launcher.pane_launch_outcome_source_for_role(
            role,
            pane_state_dir,
            updated_since=time.time(),
        )

    assert source == ""

def test_pane_runtime_hook_source_rejects_wrong_target_codex_state() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-wrong-target-codex-state.") as tmp:
        pane_state_dir = Path(tmp) / "pane-state"
        pane_state_dir.mkdir()
        (pane_state_dir / pane_state_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": "pgu-main:0.0",
                    "state": "idle",
                    "updated_at": time.time(),
                    "source": "codex.Stop",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        source = team_launcher.pane_runtime_hook_source_for_role(
            role,
            pane_state_dir,
            updated_since=time.time() - 1.0,
        )

    assert source == ""

def test_pane_launch_outcome_source_rejects_wrong_target_launcher_state() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-wrong-target-launcher-state.") as tmp:
        pane_state_dir = Path(tmp) / "pane-state"
        pane_state_dir.mkdir()
        (pane_state_dir / pane_state_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": "pgu-main:0.0",
                    "state": "idle",
                    "updated_at": time.time(),
                    "source": "team_launcher.start",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        source = team_launcher.pane_launch_outcome_source_for_role(
            role,
            pane_state_dir,
            updated_since=time.time() - 1.0,
        )

    assert source == ""

def test_launch_session_record_report_ignores_stale_resume_fallback_sidecars() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-fallback.") as tmp:
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
            json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        sidecar.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        time.sleep(0.12)
        fallback_since_ns = time.time_ns()
        while fallback_since_ns <= sidecar.stat().st_ctime_ns:
            fallback_since_ns = time.time_ns()
        messages: list[str] = []

        statuses = team_launcher.report_launch_session_records(
            config,
            timeout_seconds=0,
            fallback_changed_since_ns=fallback_since_ns,
            print_func=messages.append,
        )

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == ""
    assert not any("fell back to a fresh session" in message for message in messages)

def test_launch_session_record_statuses_wait_for_late_visible_fallback_outcome() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-late-fallback.") as tmp:
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
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()

        def write_late_fallback() -> None:
            time.sleep(0.05)
            active_record.replace(sidecar)
            active_record.write_text(
                json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(
                role,
                pane_state_dir=pane_state_dir,
                source="team_launcher.start.fallback",
            )

        writer = threading.Thread(target=write_late_fallback)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                fallback_changed_since_ns=fallback_since_ns,
                fallback_grace_seconds=0.0,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert statuses[0].pane_state_source == "team_launcher.start.fallback"

def test_launch_session_record_statuses_ignore_stale_pane_state_before_late_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-stale-pane-state.") as tmp:
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
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        team_launcher.seed_initial_pane_idle_state(
            role,
            pane_state_dir=pane_state_dir,
            source="team_launcher.start",
            now=time.time() - 1.0,
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()

        def write_late_fallback() -> None:
            time.sleep(0.05)
            active_record.replace(sidecar)
            active_record.write_text(
                json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(
                role,
                pane_state_dir=pane_state_dir,
                source="team_launcher.start.fallback",
            )

        writer = threading.Thread(target=write_late_fallback)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                fallback_changed_since_ns=fallback_since_ns,
                fallback_grace_seconds=0.0,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert statuses[0].pane_state_source == "team_launcher.start.fallback"

def test_launch_session_record_statuses_ignore_non_launcher_pane_state_before_late_fallback() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-hook-pane-state.") as tmp:
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
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        (pane_state_dir / pane_state_file_name(role.target)).write_text(
            json.dumps(
                {
                    "target": role.target,
                    "state": "idle",
                    "updated_at": time.time(),
                    "source": "claude.UserPromptSubmit",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"
        fallback_since_ns = time.time_ns()
        pane_state_since = time.time()

        def write_late_fallback() -> None:
            time.sleep(0.05)
            active_record.replace(sidecar)
            active_record.write_text(
                json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                encoding="utf-8",
            )
            team_launcher.seed_initial_pane_idle_state(
                role,
                pane_state_dir=pane_state_dir,
                source="team_launcher.start.fallback",
            )

        writer = threading.Thread(target=write_late_fallback)
        writer.start()
        try:
            statuses = team_launcher.launch_session_record_statuses(
                config,
                timeout_seconds=1.0,
                poll_seconds=0.01,
                fallback_changed_since_ns=fallback_since_ns,
                fallback_grace_seconds=0.0,
                pane_state_dir=pane_state_dir,
                pane_state_updated_since=pane_state_since,
            )
        finally:
            writer.join(timeout=1.0)

    assert statuses[0].session_id == "fresh-session"
    assert statuses[0].superseded_session_id == "old-session"
    assert statuses[0].pane_state_source == "team_launcher.start.fallback"

def test_superseded_session_freshness_uses_rename_ctime_not_record_mtime() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-session-ctime.") as tmp:
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
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        time.sleep(0.12)
        fallback_since_ns = time.time_ns()

        assert clear_session_record_for_role(role, session_dir) is True

        assert (
            team_launcher.superseded_session_id_for_role(
                role,
                session_dir,
                changed_since_ns=fallback_since_ns,
            )
            == "old-session"
        )

def test_launch_session_record_timeout_default_is_ten_seconds() -> None:
    assert team_launcher.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS == 10.0
    for fn in (team_launcher.launch_project, team_launcher.switchyard_new_command):
        assert (
            inspect.signature(fn).parameters["session_record_timeout"].default
            == team_launcher.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS
        )

def test_launch_project_reports_missing_session_record_after_reboot_resume() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-resume-session-records.") as tmp:
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
        designer = next(role for role in config.roles if role.role == "designer")
        (session_dir / session_file_name(designer.target)).write_text(
            json.dumps({"target": designer.target, "session_id": "designer-resume-session"}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(
            current_commands={
                "porter-designer:0.0": "claude",
                "porter-main:0.0": "codex",
            }
        )
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

    assert messages[0] == "switchyard: checking session records for 2 pane(s) (waiting up to 0s)"
    assert any("session record found for designer (porter-designer:0.0): designer-resume-session" in message for message in messages)
    assert any("warning: switchyard: session record missing for main (porter-main:0.0)" in message for message in messages)

def test_launch_project_reports_visible_role_resume_fallback_to_operator() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launch-visible-fallback.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [
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
                        {"role": "app", "slot": 0, "cli": ["codex"], "target": "porter-app:0.0"},
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
        active_record = session_dir / session_file_name(role.target)
        active_record.write_text(
            json.dumps({"target": role.target, "session_id": "old-session"}) + "\n",
            encoding="utf-8",
        )
        sidecar = session_dir / f"{session_file_name(role.target)}.superseded"

        detached_launcher = RecordingProcessLauncher()

        def process_launcher(args: list[str], **kwargs: object) -> object:
            if "konsole" in args and active_record.exists() and not sidecar.exists():
                active_record.replace(sidecar)
                active_record.write_text(
                    json.dumps({"target": role.target, "session_id": "fresh-session"}) + "\n",
                    encoding="utf-8",
                )
            return detached_launcher(args, **kwargs)

        class VisibleFallbackRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                return super().__call__(args, **kwargs)

        messages: list[str] = []

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=VisibleFallbackRunner(),
                layout_output=tmp_path / "launch-layout.json",
                pane_state_dir=tmp_path / "pane-state",
                report_session_records=True,
                session_record_timeout=0,
                konsole_process_launcher=process_launcher,
                print_func=messages.append,
            )
            == 0
        )

    assert any("session record found for app (porter-app:0.0): fresh-session" in message for message in messages)
    assert any(
        "warning: switchyard: app (porter-app:0.0) fell back to a fresh session; superseded session old-session"
        in message
        for message in messages
    )

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

def main() -> int:
    run_team_launcher_tests(globals(), first=('test_ticket_board_pane_env_is_stripped_before_launcher_import',))
    print("team_launcher_session_sources_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
