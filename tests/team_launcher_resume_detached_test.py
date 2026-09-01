#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_visible_start_fails_before_attach_when_cli_exits_immediately() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    class DeadFreshRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.existing = False

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 0 if self.existing else 1)
            if args[:2] == ["tmux", "new-session"]:
                self.existing = True
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 1, stdout="")
            if args[:2] == ["tmux", "attach"]:
                raise AssertionError("dead fresh pane should fail before attach")
            return subprocess.CompletedProcess(args, 0)

    runner = DeadFreshRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        pane_state_dir = tmp_path / "pane-state"
        with redirect_stderr(stderr):
            assert (
                run_role_pane(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=pane_state_dir,
                    runner=runner,
                )
                == 1
            )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"] for call in runner.calls)
    assert "role ops did not leave a live pgu-ops session" in stderr.getvalue()

def test_clear_session_record_preserves_superseded_history_after_second_fallback() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp)
        live_path = session_dir / session_file_name(role.target)
        sidecar_path = live_path.with_name(f"{live_path.name}.superseded")
        previous_sidecar_path = live_path.with_name(f"{live_path.name}.superseded.1")
        first_session_id = "11111111-1111-4111-8111-111111111111"
        second_session_id = "22222222-2222-4222-8222-222222222222"
        live_path.write_text(
            json.dumps({"target": role.target, "session_id": first_session_id}) + "\n",
            encoding="utf-8",
        )

        assert clear_session_record_for_role(role, session_dir) is True
        assert not live_path.exists()
        assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == first_session_id
        assert not previous_sidecar_path.exists()

        live_path.write_text(
            json.dumps({"target": role.target, "session_id": second_session_id}) + "\n",
            encoding="utf-8",
        )

        assert clear_session_record_for_role(role, session_dir) is True
        assert not live_path.exists()
        assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == second_session_id
        assert json.loads(previous_sidecar_path.read_text(encoding="utf-8"))["session_id"] == first_session_id
        saved_session_ids = {
            json.loads(path.read_text(encoding="utf-8"))["session_id"]
            for path in (sidecar_path, previous_sidecar_path)
        }
        assert first_session_id in saved_session_ids
        assert clear_session_record_for_role(role, session_dir) is False
        assert json.loads(sidecar_path.read_text(encoding="utf-8"))["session_id"] == second_session_id
        assert json.loads(previous_sidecar_path.read_text(encoding="utf-8"))["session_id"] == first_session_id

def test_start_seeds_initial_idle_state_for_codex_agy_and_claude_panes() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    expected_cli_by_role = {
        "ops": "codex",
        "inspector": "agy",
        "director": "claude",
    }

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        for role_name, cli_name in expected_cli_by_role.items():
            role = roles[role_name]
            runner = FakeRunner()
            assert (
                run_role_pane(
                    role,
                    mode="start",
                    session_dir=tmp_path / role_name / "sessions",
                    pane_state_dir=tmp_path / role_name / "pane-state",
                    runner=runner,
                )
                == 0
            )
            state = _read_pane_state(tmp_path / role_name / "pane-state", role.target)
            assert state == {
                "source": "team_launcher.start",
                "state": "idle",
                "target": role.target,
                "updated_at": state["updated_at"],
            }
            assert isinstance(state["updated_at"], float)
            assert f" {cli_name} " in f" {runner.calls[1][-1]} "

def test_start_resumes_recorded_session_when_recreating_missing_pane() -> None:
    # start (attach-or-start) must resume a tracked session id when relaunching a
    # stopped pane -- resume is not reload-only. A cold restart uses start mode.
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
        runner = FakeRunner(current_commands={"pgu-ops:0.0": "codex"})

        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        state = _read_pane_state(pane_state_dir, role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert f"codex resume {session_id}" in runner.calls[1][-1]
    # resume verified via pane inspection -> no fresh fallback, then attach
    assert ["tmux", "kill-session", "-t", "pgu-ops"] not in runner.calls
    assert runner.calls[-1] == ["tmux", "attach", "-t", "pgu-ops"]

def test_seed_session_dir_from_legacy_sources_copies_valid_records_once() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    roles = {role.role: role for role in config.roles}
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        durable = tmp_path / "durable"
        legacy_runtime = tmp_path / "runtime" / "pane-sessions"
        interim_backup = tmp_path / "backup" / "pane-sessions"
        legacy_runtime.mkdir(parents=True)
        interim_backup.mkdir(parents=True)
        ops_id = "12345678-1234-5678-9abc-def012345678"
        audit_id = "22345678-1234-5678-9abc-def012345678"
        (legacy_runtime / session_file_name(roles["ops"].target)).write_text(
            json.dumps({"target": roles["ops"].target, "session_id": ops_id, "payload": {"model": "gpt-5.5"}}) + "\n",
            encoding="utf-8",
        )
        (interim_backup / session_file_name(roles["audit"].target)).write_text(
            json.dumps({"target": roles["audit"].target, "session_id": audit_id}) + "\n",
            encoding="utf-8",
        )
        (interim_backup / "bad.json").write_text(json.dumps({"target": roles["main"].target}) + "\n", encoding="utf-8")

        copied = seed_session_dir_from_legacy_sources(durable, candidates=[legacy_runtime, interim_backup])

        assert sorted(path.name for path in copied) == sorted(
            [session_file_name(roles["ops"].target), session_file_name(roles["audit"].target)]
        )
        assert session_id_for_role(roles["ops"], durable) == ops_id
        assert session_id_for_role(roles["audit"], durable) == audit_id
        assert oct(durable.stat().st_mode & 0o777) == "0o700"
        assert oct((durable / session_file_name(roles["ops"].target)).stat().st_mode & 0o777) == "0o600"

        (legacy_runtime / session_file_name(roles["main"].target)).write_text(
            json.dumps({"target": roles["main"].target, "session_id": "new-session"}) + "\n",
            encoding="utf-8",
        )
        assert seed_session_dir_from_legacy_sources(durable, candidates=[legacy_runtime]) == []
        assert session_id_for_role(roles["main"], durable) == ""

def test_start_resumes_from_durable_session_dir_after_runtime_tmpfs_is_cleared() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(current_commands={"pgu-ops:0.0": "codex"})
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        durable = tmp_path / "state-home" / "pgu-ticket-board" / "pane-sessions"
        runtime_tmpfs = tmp_path / "run" / "pane-sessions"
        durable.mkdir(parents=True)
        runtime_tmpfs.mkdir(parents=True)
        session_id = "32345678-1234-5678-9abc-def012345678"
        transcript = tmp_path / "codex-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (durable / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
            + "\n",
            encoding="utf-8",
        )
        shutil.rmtree(runtime_tmpfs)

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=durable,
                pane_state_dir=tmp_path / "pane-state",
                runner=runner,
            )
            == 0
        )

    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert f"TICKET_BOARD_PANE_SESSION_DIR={durable}" in runner.calls[1][-1]
    assert f"TICKET_BOARD_PANE_STATE_DIR={tmp_path / 'pane-state'}" in runner.calls[1][-1]
    assert f"PGU_TICKET_BOARD_PANE_SESSION_DIR={durable}" in runner.calls[1][-1]
    assert f"PGU_TICKET_BOARD_PANE_STATE_DIR={tmp_path / 'pane-state'}" in runner.calls[1][-1]
    assert f"codex resume {session_id}" in runner.calls[1][-1]

def test_start_runs_research_detached_before_opening_visible_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        process_launcher = RecordingProcessLauncher()
        layout_output = tmp_path / "layout.json"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
                layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        assert runner.calls[:3] == [
            git_launcher_checkout_check_args(ROOT),
            git_launcher_head_args(ROOT),
            git_launcher_ls_remote_ref_args(config, ROOT),
        ]
        shared_checkout_calls = [
            git_fetch_worktree_ref_args(config),
            git_shared_checkout_check_args(config),
            team_launcher.git_shared_checkout_status_porcelain_args(config),
            team_launcher.git_clean_shared_checkout_dry_run_args(config),
            git_checkout_shared_ref_args(config),
            git_clean_shared_checkout_args(config),
        ]
        shared_checkout_indexes = [runner.calls.index(call) for call in shared_checkout_calls]
        assert shared_checkout_indexes == sorted(shared_checkout_indexes)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        research_new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"])
        assert research_new_session[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"]
        assert research_new_session[5:7] == ["-c", str(tmp_path / "repo")]
        assert "TICKET_BOARD_PANE_TARGET=pgu-research:0.0" in research_new_session[-1]
        assert "PGU_PANE_TARGET=pgu-research:0.0" in research_new_session[-1]
        assert "--model claude-opus-5" in research_new_session[-1]
        state = _read_pane_state(tmp_path / "pane-state", "pgu-research:0.0")
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        layout = json.loads(layout_output.read_text(encoding="utf-8"))
        assert {leaf.get("WorkingDirectory") for leaf in team_launcher._layout_leaves(layout)} == {str(tmp_path / "repo")}
        assert len(process_launcher.calls) == 1
        assert process_launcher.calls[0]["args"] == konsole_launch_args(layout_output)
        assert process_launcher.calls[0]["kwargs"]["start_new_session"] is True
        assert not any(call == konsole_launch_args(layout_output) for call in runner.calls)
        assert ["tmux", "attach", "-t", "pgu-research"] not in runner.calls

def test_plain_shared_checkout_git_runs_as_owner_when_launcher_user_differs() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-git.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = SudoAwareFakeRunner(current_commands={"pgu-research:0.0": "claude"})
        process_launcher = RecordingProcessLauncher()
        layout_output = tmp_path / "layout.json"
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "root"
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=layout_output,
                    pane_state_dir=tmp_path / "pane-state",
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

        owner_git_calls = [
            call[3:]
            for call in runner.calls
            if call[:3] == ["sudo", "-u", "agent"] and len(call) > 3 and call[3] == "git"
        ]
        expected_refresh_calls = {
            tuple(git_fetch_worktree_ref_args(config)),
            tuple(git_shared_checkout_check_args(config)),
            tuple(team_launcher.git_shared_checkout_status_porcelain_args(config)),
            tuple(team_launcher.git_clean_shared_checkout_dry_run_args(config)),
            tuple(git_checkout_shared_ref_args(config)),
            tuple(git_clean_shared_checkout_args(config)),
        }
        assert {tuple(call) for call in owner_git_calls} >= expected_refresh_calls
        direct_refresh_calls = [
            call
            for call in runner.calls
            if tuple(call) in expected_refresh_calls
        ]
        assert direct_refresh_calls == []
        assert [
            "sudo",
            "-u",
            "agent",
            "-H",
            str(ROOT / "scripts" / "team-launcher"),
            "pgu",
            "pane",
            "attach-or-start",
            "research",
            "--config",
            str(config_path),
            "--skip-launcher-check",
            "--pane-state-dir",
            str(tmp_path / "pane-state"),
        ] in runner.calls

def test_plain_shared_checkout_git_stays_direct_when_already_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-owner-git.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        runner = FakeRunner(current_commands={"pgu-research:0.0": "claude"})
        process_launcher = RecordingProcessLauncher()
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "agent"
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )
        finally:
            team_launcher.current_user_name = original_current_user_name

        assert git_fetch_worktree_ref_args(config) in runner.calls
        assert not any(call[:3] == ["sudo", "-u", "agent"] and len(call) > 3 and call[3] == "git" for call in runner.calls)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_resume_detached_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
