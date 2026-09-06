#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_add_role_unrecognized_layout_without_room_refuses_actionably_without_mutation() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-hand-layout.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            implementer_roles=("app", "main"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main"),
                    include_designer=False,
                    include_audit=False,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Vertical",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {
                            "Orientation": "Horizontal",
                            "Widgets": [
                                {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                                {"Command": "", "SessionRestoreId": 2, "WorkingDirectory": ""},
                            ],
                        },
                    ],
                    "SwitchyardEdited": True,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        before_config = config_path.read_text(encoding="utf-8")
        before_layout = layout_path.read_text(encoding="utf-8")
        before_plan = (provision_dir / "plan.json").read_text(encoding="utf-8")
        runner = KeywordRecordingFakeRunner()

        try:
            team_launcher.add_project_role_command(
                config,
                config_path=config_path,
                role_name="ops",
                cli="codex",
                runner=runner,
            )
            raise AssertionError("expected unrecognized layout without room to be rejected")
        except SystemExit as exc:
            message = str(exc)

        after_config = config_path.read_text(encoding="utf-8")
        after_layout = layout_path.read_text(encoding="utf-8")
        after_plan = (provision_dir / "plan.json").read_text(encoding="utf-8")
        add_role_sql_exists = (provision_dir / "mefp-add-role.sql").exists()

    assert "cannot add ops to visible slot 3" in message
    assert "has 3 slot(s), so no pane exists for the new role" in message
    assert "use --detached to add it headless" in message
    assert "pass --relayout to replace the existing layout with a generated 4-pane layout" in message
    assert after_config == before_config
    assert after_layout == before_layout
    assert after_plan == before_plan
    assert not add_role_sql_exists
    assert not any(call[:4] == ["sudo", "-u", "postgres", "psql"] for call in runner.calls)
    assert not any(call == ["sudo", "systemctl", "restart", "mefp-ticket-board.service"] for call in runner.calls)
    assert not any(call[:1] == ["git"] for call in runner.calls)

def test_add_role_relayout_replaces_unrecognized_layout_and_starts_new_role() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-relayout.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            commit_git_dir="/srv/git/review-cache.git",
            implementer_roles=("app", "main"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main"),
                    include_designer=False,
                    include_audit=False,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Vertical",
                    "Widgets": [
                        {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""},
                        {
                            "Orientation": "Horizontal",
                            "Widgets": [
                                {"Command": "", "SessionRestoreId": 1, "WorkingDirectory": ""},
                                {"Command": "", "SessionRestoreId": 2, "WorkingDirectory": ""},
                            ],
                        },
                    ],
                    "SwitchyardEdited": True,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner(existing_sessions={"mefp-director"})
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.add_project_role_command(
                    config,
                    config_path=config_path,
                    role_name="ops",
                    cli="codex",
                    relayout=True,
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                )
                == 0
            )

        updated_config_json = json.loads(config_path.read_text(encoding="utf-8"))
        updated_layout = json.loads(layout_path.read_text(encoding="utf-8"))
        updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        output = stdout.getvalue()

    assert [role["role"] for role in updated_config_json["roles"]] == ["director", "app", "main", "ops"]
    assert updated_config_json["roles"][-1]["slot"] == 3
    assert updated_layout == team_launcher._new_project_layout_payload(4)
    assert updated_plan["implementer_roles"] == ["app", "main", "ops"]
    assert updated_plan["commit_git_dir"] == "/srv/git/review-cache.git"
    assert any(call == ["sudo", "systemctl", "restart", "mefp-ticket-board.service"] for call in runner.calls)
    pane_calls = [call for call in runner.calls if call[1:5] == ["mefp", "pane", "attach-or-start", "ops"]]
    assert len(pane_calls) == 1
    assert "--no-attach" in pane_calls[0]
    assert "team-launcher: added role ops to mefp; regenerated layout for 4 visible pane(s)" in output
    assert "started tmux session for the new role" in output
    assert "relaunch the project window to display newly added visible slots" in output

def test_add_role_refuses_seventh_visible_pane_without_mutating_config() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-visible-cap.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            implementer_roles=("app", "main", "ops"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main", "ops"),
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        before_config = config_path.read_text(encoding="utf-8")
        before_layout = layout_path.read_text(encoding="utf-8")
        runner = KeywordRecordingFakeRunner()

        try:
            team_launcher.add_project_role_command(
                config,
                config_path=config_path,
                role_name="perf",
                cli="codex",
                runner=runner,
            )
            raise AssertionError("expected seventh visible role to be rejected")
        except SystemExit as exc:
            message = str(exc)
        after_config = config_path.read_text(encoding="utf-8")
        after_layout = layout_path.read_text(encoding="utf-8")

    assert "cannot add perf as visible" in message
    assert "at most 6 panes can be visible in one window" in message
    assert after_config == before_config
    assert after_layout == before_layout
    assert not any(call[:4] == ["sudo", "-u", "postgres", "psql"] for call in runner.calls)

def test_add_role_existing_role_reapplies_registration_without_reappending_config() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-duplicate.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            implementer_roles=("app", "main"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main"),
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_before = config_path.read_text(encoding="utf-8")
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.add_project_role_command(
                    config,
                    config_path=config_path,
                    role_name="main",
                    cli="codex",
                    runner=runner,
                )
                == 0
            )

        config_after = config_path.read_text(encoding="utf-8")
        output = stdout.getvalue()

    assert config_after == config_before
    assert any(call[:4] == ["sudo", "-u", "postgres", "psql"] for call in runner.calls)
    assert any(call == ["sudo", "systemctl", "restart", "mefp-ticket-board.service"] for call in runner.calls)
    assert any(
        call[1:5] == ["mefp", "pane", "attach-or-start", "main"]
        for call in runner.calls
    )
    assert "role main already exists in mefp; reapplied board registration" in output

def test_add_role_seventh_visible_role_rejects_before_privileged_mutation() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-seventh-repeat.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            implementer_roles=("app", "main", "ops", "perf", "research", "qa"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main", "ops", "perf", "research", "qa"),
                    include_designer=False,
                    include_audit=False,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        before_config = config_path.read_text(encoding="utf-8")
        before_layout = layout_path.read_text(encoding="utf-8")
        runner = KeywordRecordingFakeRunner()

        try:
            team_launcher.add_project_role_command(
                config,
                config_path=config_path,
                role_name="build",
                cli="codex",
                runner=runner,
            )
            raise AssertionError("expected seventh visible role to be rejected")
        except SystemExit as exc:
            message = str(exc)

        config_after = config_path.read_text(encoding="utf-8")
        layout_after = layout_path.read_text(encoding="utf-8")

    assert "cannot add build as visible" in message
    assert "at most 6 panes can be visible in one window" in message
    assert config_after == before_config
    assert layout_after == before_layout
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["git"] for call in runner.calls)

def test_set_vcs_close_role_updates_generated_artifacts_database_and_unit() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-vcs-close.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            commit_git_dir="/srv/git/review-cache.git",
            implementer_roles=("app", "main", "archivist"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main", "archivist"),
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                set_project_vcs_close_role_command(
                    config,
                    config_path=config_path,
                    role_name="archivist",
                    runner=runner,
                )
                == 0
            )

        updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        updated_unit = (provision_dir / "mefp-ticket-board.service").read_text(encoding="utf-8")
        workflow_sql = (provision_dir / "mefp-vcs-close-role.sql").read_text(encoding="utf-8")

    assert updated_plan["operation_allowed_roles"] == [["mark_done", ["archivist"]]]
    assert updated_plan["commit_git_dir"] == "/srv/git/review-cache.git"
    assert updated_plan["implementer_roles"] == ["app", "main"]
    assert updated_plan["assignee_roles"] == ["unassigned", "designer", "app", "main", "archivist", "audit", "director", "user"]
    assert updated_plan["caller_roles"] == ["director", "designer", "app", "main", "archivist", "audit", "user"]
    assert "Environment=TICKET_BOARD_IMPLEMENTER_ROLES=app,main" in updated_unit
    assert "Environment=TICKET_BOARD_ASSIGNEES=unassigned,designer,app,main,archivist,audit,director,user" in updated_unit
    assert "Environment=TICKET_BOARD_CALLER_ROLES=director,designer,app,main,archivist,audit,user" in updated_unit
    assert "Environment=TICKET_BOARD_OPERATION_ALLOWED_ROLES=mark_done=archivist" in updated_unit
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[]" in workflow_sql
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app', 'archivist']::text[]" not in workflow_sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main']::text[]" in workflow_sql
    assert "('in_progress', 'audit', 'submit_to_audit', ARRAY['app', 'main']::text[]" in workflow_sql
    assert "('vcs', 'VCS', 7, ARRAY['archivist']::text[]" in workflow_sql
    assert "('director_review', 'vcs', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('vcs', 'done', 'mark_done', ARRAY['archivist']::text[]" in workflow_sql
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" not in workflow_sql
    assert "DELETE FROM ticket_board.workflow_transitions\nWHERE action_name = 'mark_done';" in workflow_sql
    psql_calls = [
        (args, kwargs)
        for args, kwargs in runner.calls_with_kwargs
        if args[:4] == ["sudo", "-u", "postgres", "psql"]
    ]
    assert len(psql_calls) == 1
    assert "ARRAY['archivist']::text[]" in str(psql_calls[0][1]["input"])
    assert "ARRAY['main', 'app', 'archivist']::text[]" not in str(psql_calls[0][1]["input"])
    assert "DELETE FROM ticket_board.workflow_transitions\nWHERE action_name = 'mark_done';" in str(psql_calls[0][1]["input"])
    assert any(call == ["sudo", "systemctl", "restart", "mefp-ticket-board.service"] for call in runner.calls)
    assert "team-launcher: set VCS close role for mefp to archivist" in stdout.getvalue()

def test_set_vcs_close_role_refuses_unknown_role_before_mutating() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-vcs-close-unknown.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="mefp",
            owner_user=current_user,
            port=18811,
            source_repo=source_repo,
            implementer_roles=("app", "main"),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main"),
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_before = config_path.read_text(encoding="utf-8")
        plan_before = (provision_dir / "plan.json").read_text(encoding="utf-8")
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner()

        try:
            set_project_vcs_close_role_command(
                config,
                config_path=config_path,
                role_name="archivist",
                runner=runner,
            )
            raise AssertionError("expected unknown VCS close role to be rejected")
        except SystemExit as exc:
            message = str(exc)
        config_after = config_path.read_text(encoding="utf-8")
        plan_after = (provision_dir / "plan.json").read_text(encoding="utf-8")
        workflow_sql_exists = (provision_dir / "mefp-vcs-close-role.sql").exists()

    assert "VCS close role 'archivist' does not exist in project mefp" in message
    assert config_after == config_before
    assert plan_after == plan_before
    assert not workflow_sql_exists
    assert not any(call[:4] == ["sudo", "-u", "postgres", "psql"] for call in runner.calls)

def test_set_vcs_close_role_refuses_pgu_before_mutating() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-vcs-close-pgu.") as tmp:
        tmp_path = Path(tmp)
        provision_dir = tmp_path / "project" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="pgu",
            owner_user="agent",
            port=8770,
            source_repo=source_repo,
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = provision_dir / "pgu.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("main", "app", "ops", "perf", "research"),
                    include_designer=False,
                    include_audit=True,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_before = config_path.read_text(encoding="utf-8")
        config = load_project_config("pgu", config_path)
        runner = KeywordRecordingFakeRunner()

        try:
            set_project_vcs_close_role_command(
                config,
                config_path=config_path,
                role_name="ops",
                runner=runner,
            )
            raise AssertionError("expected pgu VCS close-role rejection")
        except SystemExit as exc:
            message = str(exc)
        config_after = config_path.read_text(encoding="utf-8")

    assert "pgu uses the full built-in workflow" in message
    assert config_after == config_before
    assert not any(call[:4] == ["sudo", "-u", "postgres", "psql"] for call in runner.calls)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_add_role_vcs_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
