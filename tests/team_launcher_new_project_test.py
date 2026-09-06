#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_new_project_dry_run_writes_board_and_launcher_artifacts() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        runner = FakeRunner()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=runner,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )

        rendered = stdout.getvalue()
        config = json.loads((output_dir / "porter.json").read_text(encoding="utf-8"))
        layout = json.loads((output_dir / "porter-konsole-layout.json").read_text(encoding="utf-8"))
        plan = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
        board_unit = (output_dir / "porter-ticket-board.service").read_text(encoding="utf-8")
        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")
        workflow_sql = (output_dir / "porter-workflow.sql").read_text(encoding="utf-8")
        loaded_config = load_project_config("porter", output_dir / "porter.json")
        launch_output = tmp_path / "launch-layout.json"
        launch_stdout = StringIO()

        with redirect_stdout(launch_stdout):
            assert (
                launch_project(
                    loaded_config,
                    config_path=output_dir / "porter.json",
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    dry_run=True,
                    layout_output=launch_output,
                )
                == 0
            )
        launch_plan = json.loads(launch_stdout.getvalue())
        launch_output_exists = launch_output.exists()

    assert plan["project"] == "porter"
    assert plan["ticket_prefix"] == "PORTER"
    assert plan["owner_user"] == current_user
    assert plan["commit_git_dir"] == "/srv/git/review-cache.git"
    assert plan["port"] == 23682
    assert config["project"] == "porter"
    assert config["ticket_prefix"] == "PORTER"
    assert config["run_as_user"] == current_user
    assert config["board_url"] == "http://127.0.0.1:23682"
    assert config["board_socket"] == "/run/porter-ticket-board/ticket-board.sock"
    assert config["repository"] == str(project_repo)
    assert config["repository"] != str(source_repo)
    assert config["control_repository"] == f"/home/{current_user}/.local/state/switchyard/projects/porter/control.git"
    assert config["worktree_base"] == f"/home/{current_user}/porter-worktrees"
    assert config["session_dir"] == f"/home/{current_user}/.local/state/porter-ticket-board/pane-sessions"
    assert not config["session_dir"].startswith("/run/")
    assert [role["role"] for role in config["roles"]] == ["designer", "director", "audit", "app", "main"]
    assert [role["tmux_session"] for role in config["roles"]] == [
        "porter-designer",
        "porter-director",
        "porter-audit",
        "porter-app",
        "porter-main",
    ]
    assert {role["role"]: role["cli"] for role in config["roles"]} == {
        "designer": ["claude"],
        "director": ["claude"],
        "audit": ["claude"],
        "app": ["codex"],
        "main": ["codex"],
    }
    assert {"designer", "director", "main", "app", "audit"} <= {role["role"] for role in config["roles"]}
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[]" in workflow_sql
    assert "('in_progress', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('done', 'Done', 9, ARRAY[]::text[], NULL, NULL, NULL, true)" in workflow_sql
    assert "('cancelled', 'Cancelled', 10, ARRAY[]::text[], NULL, NULL, NULL, true)" in workflow_sql
    assert "('audit', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('audit', 'in_progress', 'audit_kick_back', ARRAY['audit']::text[]" in workflow_sql
    assert "('director_review', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('director_review', 'analysis', 'user_reopen', ARRAY['user']::text[]" in workflow_sql
    assert "('director_review', 'in_progress', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('director_review', 'done', 'mark_done', ARRAY['director']::text[]" in workflow_sql
    assert "('director_review', 'cancelled', 'cancel', ARRAY['director']::text[]" in workflow_sql
    assert "('done', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert "('done', 'analysis', 'user_reopen', ARRAY['user']::text[]" in workflow_sql
    assert "('cancelled', 'analysis', 'route', ARRAY['director']::text[]" in workflow_sql
    assert len(team_launcher._layout_leaves(layout)) == 5
    assert [role.role for role in loaded_config.roles] == ["designer", "director", "audit", "app", "main"]
    assert loaded_config.roles[0].target == "porter-designer:0.0"
    assert loaded_config.roles[3].target == "porter-app:0.0"
    assert loaded_config.roles[4].target == "porter-main:0.0"
    assert loaded_config.control_repository == Path(config["control_repository"])
    assert loaded_config.worktree_base == Path(config["worktree_base"])
    assert loaded_config.session_dir == Path(config["session_dir"])
    assert {role.role: role.workdir for role in loaded_config.roles} == {
        "designer": f"/home/{current_user}/porter-worktrees/designer",
        "director": f"/home/{current_user}/porter-worktrees/director",
        "audit": f"/home/{current_user}/porter-worktrees/audit",
        "app": f"/home/{current_user}/porter-worktrees/app",
        "main": f"/home/{current_user}/porter-worktrees/main",
    }
    assert {role.env["TICKET_BOARD_TICKET_PREFIX"] for role in loaded_config.roles} == {"PORTER"}
    assert commands.splitlines()[:2] == ["#!/usr/bin/env bash", "set -euo pipefail"]
    assert "Environment=TICKET_BOARD_PROJECT=porter" in board_unit
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=PORTER" in board_unit
    assert "Environment=TICKET_BOARD_COMMIT_GIT_DIR=/srv/git/review-cache.git" in board_unit
    assert "TICKET_BOARD_COMMIT_GIT_DIR='/srv/git/review-cache.git'" in commands
    assert f"team-launcher: dry-run for porter; artifacts in {output_dir}" in rendered
    assert "  sudo -v\n" in rendered
    assert "  bash operator-commands.sh\n" in rendered
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert launch_plan["project"] == "porter"
    assert launch_plan["mode"] == "attach-or-start"
    assert [role["target"] for role in launch_plan["roles"]] == [
        "porter-designer:0.0",
        "porter-director:0.0",
        "porter-audit:0.0",
        "porter-app:0.0",
        "porter-main:0.0",
    ]
    assert [role["workdir"] for role in launch_plan["roles"]] == [
        f"/home/{current_user}/porter-worktrees/designer",
        f"/home/{current_user}/porter-worktrees/director",
        f"/home/{current_user}/porter-worktrees/audit",
        f"/home/{current_user}/porter-worktrees/app",
        f"/home/{current_user}/porter-worktrees/main",
    ]
    cli_env_entries = _env_entries(cli_command_for_role(loaded_config.roles[0], session_dir=loaded_config.session_dir))
    assert f"TICKET_BOARD_PANE_SESSION_DIR={config['session_dir']}" in cli_env_entries
    assert not any(entry.startswith("TICKET_BOARD_PANE_SESSION_DIR=/run/") for entry in cli_env_entries)
    assert launch_output_exists

def test_new_project_caps_automatic_window_to_six_visible_roles() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-visible-cap.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        output_dir.mkdir()
        source_repo.mkdir()
        project_repo.mkdir()
        plan = team_launcher.build_plan(
            project="porter",
            owner_user=current_user,
            source_repo=source_repo,
            implementer_roles=("ops", "app", "main", "perf", "research"),
        )
        messages: list[str] = []

        config_path = team_launcher.write_new_project_launcher_artifacts(
            plan,
            output_dir,
            repository=project_repo,
            implementer_roles=("ops", "app", "main", "perf", "research"),
            print_func=messages.append,
        )

        config = json.loads(config_path.read_text(encoding="utf-8"))
        layout = json.loads((output_dir / "porter-konsole-layout.json").read_text(encoding="utf-8"))
        loaded_config = load_project_config("porter", output_dir / "porter.json")
        visible_roles = [role for role in config["roles"] if not role.get("detached")]
        detached_roles = [role for role in config["roles"] if role.get("detached")]

    assert len(config["roles"]) == 8
    assert [role["role"] for role in visible_roles] == ["designer", "director", "audit", "ops", "app", "main"]
    assert [role["slot"] for role in visible_roles] == list(range(team_launcher.MAX_VISIBLE_PANES_PER_WINDOW))
    assert [role["role"] for role in detached_roles] == ["perf", "research"]
    assert not any("slot" in role for role in detached_roles)
    assert len(team_launcher._layout_leaves(layout)) == team_launcher.MAX_VISIBLE_PANES_PER_WINDOW
    assert len([role for role in loaded_config.roles if not role.detached]) == team_launcher.MAX_VISIBLE_PANES_PER_WINDOW
    assert len(loaded_config.roles) == 8
    assert messages == [
        "team-launcher: auto-detached roles beyond the 6-pane window cap: perf, research; "
        "use attach-role to surface one later or detach another role first"
    ]

def test_add_role_updates_generated_config_board_registration_and_starts_only_new_role() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role.") as tmp:
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
        (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
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
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(5), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner(existing_sessions={"mefp-director", "mefp-main"})
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.add_project_role_command(
                    config,
                    config_path=config_path,
                    role_name="ops",
                    cli="codex",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                )
                == 0
            )

        updated_config_json = json.loads(config_path.read_text(encoding="utf-8"))
        updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        updated_unit = (provision_dir / "mefp-ticket-board.service").read_text(encoding="utf-8")
        add_role_sql = (provision_dir / "mefp-add-role.sql").read_text(encoding="utf-8")
        updated_layout_leaf_count = len(team_launcher._layout_leaves(json.loads(layout_path.read_text(encoding="utf-8"))))
        updated_config = load_project_config("mefp", config_path)

    assert [role["role"] for role in updated_config_json["roles"]] == ["designer", "director", "audit", "app", "main", "ops"]
    assert updated_config_json["roles"][-1]["slot"] == 5
    assert updated_config_json["roles"][-1]["tmux_session"] == "mefp-ops"
    assert updated_config_json["roles"][-1]["target"] == "mefp-ops:0.0"
    assert updated_config_json["roles"][-1]["workdir"] == f"/home/{current_user}/mefp-worktrees/ops"
    assert updated_layout_leaf_count == 6
    assert updated_plan["implementer_roles"] == ["app", "main", "ops"]
    assert updated_plan["assignee_roles"] == ["unassigned", "designer", "app", "main", "ops", "audit", "director", "user"]
    assert updated_plan["caller_roles"] == ["director", "designer", "app", "main", "ops", "audit", "user"]
    assert "TICKET_BOARD_IMPLEMENTER_ROLES=app,main,ops" in updated_unit
    assert "TICKET_BOARD_ASSIGNEES=unassigned,designer,app,main,ops,audit,director,user" in updated_unit
    assert "TICKET_BOARD_CALLER_ROLES=director,designer,app,main,ops,audit,user" in updated_unit
    assert "('in_progress', 'Implementation', 2, ARRAY['main', 'app', 'ops']::text[]" in add_role_sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main', 'ops']::text[]" in add_role_sql
    assert any(call[:5] == ["git", "--git-dir", f"/home/{current_user}/.local/state/switchyard/projects/mefp/control.git", "worktree", "add"] for call in runner.calls)
    assert any(call == ["sudo", "systemctl", "restart", "mefp-ticket-board.service"] for call in runner.calls)
    pane_calls = [call for call in runner.calls if call[1:5] == ["mefp", "pane", "attach-or-start", "ops"]]
    assert len(pane_calls) == 1
    psql_calls = [
        (args, kwargs)
        for args, kwargs in runner.calls_with_kwargs
        if args[:4] == ["sudo", "-u", "postgres", "psql"]
    ]
    assert len(psql_calls) == 1
    assert "ARRAY['app', 'main', 'ops']::text[]" in str(psql_calls[0][1]["input"])
    assert [role.role for role in updated_config.roles] == ["designer", "director", "audit", "app", "main", "ops"]
    assert updated_config.roles[-1].env["TICKET_BOARD_CALLER_ROLE"] == "ops"
    assert "team-launcher: added role ops to mefp" in stdout.getvalue()

def test_add_role_can_add_auditor_to_existing_project() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-auditor.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "project"
        provision_dir = project_dir / ".switchyard" / "provision"
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
            audit_roles=("audit_gemini",),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
        artifact_path = project_dir / ".switchyard" / "mefp.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "project": {
                        "roles": ["app", "main"],
                        "audit_roles": ["audit_gemini"],
                        "role_clis": {"audit_gemini": "agy", "app": "codex", "main": "codex"},
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main"),
                    audit_roles=("audit_gemini",),
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(5), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner(existing_sessions={"mefp-director", "mefp-main"})
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.add_project_role_command(
                    config,
                    config_path=config_path,
                    role_name="audit_gpt",
                    cli="agy",
                    audit_role=True,
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                )
                == 0
            )

        updated_config_json = json.loads(config_path.read_text(encoding="utf-8"))
        updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        updated_unit = (provision_dir / "mefp-ticket-board.service").read_text(encoding="utf-8")
        add_role_sql = (provision_dir / "mefp-add-role.sql").read_text(encoding="utf-8")
        updated_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert [role["role"] for role in updated_config_json["roles"]] == [
        "designer",
        "director",
        "audit_gemini",
        "app",
        "main",
        "audit_gpt",
    ]
    assert updated_config_json["roles"][-1]["slot"] == 5
    assert updated_config_json["roles"][-1]["tmux_session"] == "mefp-audit_gpt"
    assert updated_config_json["roles"][-1]["target"] == "mefp-audit_gpt:0.0"
    assert updated_config_json["roles"][-1]["workdir"] == f"/home/{current_user}/mefp-worktrees/audit_gpt"
    assert updated_plan["implementer_roles"] == ["app", "main"]
    assert updated_plan["audit_roles"] == ["audit_gemini", "audit_gpt"]
    assert updated_plan["assignee_roles"] == [
        "unassigned",
        "designer",
        "app",
        "main",
        "audit_gemini",
        "audit_gpt",
        "director",
        "user",
    ]
    assert updated_plan["caller_roles"] == ["director", "designer", "app", "main", "audit_gemini", "audit_gpt", "user"]
    assert "TICKET_BOARD_IMPLEMENTER_ROLES=app,main" in updated_unit
    assert "TICKET_BOARD_ASSIGNEES=unassigned,designer,app,main,audit_gemini,audit_gpt,director,user" in updated_unit
    assert "TICKET_BOARD_CALLER_ROLES=director,designer,app,main,audit_gemini,audit_gpt,user" in updated_unit
    assert "TICKET_BOARD_OPERATION_ALLOWED_ROLES=" in updated_unit
    assert "audit_sign_off=audit_gemini,audit_gpt" in updated_unit
    assert "Add auditor role audit_gpt to the existing project workflow for mefp" in add_role_sql
    assert "('audit', 'Audit', 3, ARRAY['audit_gemini', 'audit_gpt']::text[]" in add_role_sql
    assert "('audit', 'director_review', 'audit_sign_off', ARRAY['audit_gemini', 'audit_gpt']::text[]" in add_role_sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main']::text[]" in add_role_sql
    assert updated_artifact["project"]["roles"] == ["app", "main"]
    assert updated_artifact["project"]["audit_roles"] == ["audit_gemini", "audit_gpt"]
    assert updated_artifact["project"]["include_audit"] is True
    assert updated_artifact["project"]["role_clis"]["audit_gpt"] == "agy"
    assert any(call == ["sudo", "systemctl", "restart", "mefp-ticket-board.service"] for call in runner.calls)
    pane_calls = [call for call in runner.calls if call[1:5] == ["mefp", "pane", "attach-or-start", "audit_gpt"]]
    assert len(pane_calls) == 1
    psql_calls = [
        (args, kwargs)
        for args, kwargs in runner.calls_with_kwargs
        if args[:4] == ["sudo", "-u", "postgres", "psql"]
    ]
    assert len(psql_calls) == 1
    assert "ARRAY['audit_gemini', 'audit_gpt']::text[]" in str(psql_calls[0][1]["input"])
    assert "team-launcher: added auditor role audit_gpt to mefp" in stdout.getvalue()

def test_add_role_can_add_conventional_audit_role_to_auditless_project() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-default-audit-role.") as tmp:
        tmp_path = Path(tmp)
        project_dir = tmp_path / "project"
        provision_dir = project_dir / ".switchyard" / "provision"
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
            include_audit=False,
            audit_roles=(),
        )
        (provision_dir / "plan.json").write_text(json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
        artifact_path = project_dir / ".switchyard" / "mefp.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "project": {
                        "roles": ["app", "main"],
                        "audit_roles": [],
                        "include_audit": False,
                        "role_clis": {"app": "codex", "main": "codex"},
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config_path = provision_dir / "mefp.json"
        config_path.write_text(
            json.dumps(
                team_launcher._new_project_launcher_config_payload(
                    plan,
                    repository=project_repo,
                    implementer_roles=("app", "main"),
                    include_audit=False,
                    audit_roles=(),
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        layout_path = provision_dir / "mefp-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(4), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config = load_project_config("mefp", config_path)
        runner = KeywordRecordingFakeRunner()

        assert (
            team_launcher.add_project_role_command(
                config,
                config_path=config_path,
                role_name="audit",
                cli="claude",
                audit_role=True,
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
            )
            == 0
        )

        updated_config_json = json.loads(config_path.read_text(encoding="utf-8"))
        updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        add_role_sql = (provision_dir / "mefp-add-role.sql").read_text(encoding="utf-8")
        updated_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert [role["role"] for role in updated_config_json["roles"]] == ["designer", "director", "app", "main", "audit"]
    assert updated_plan["implementer_roles"] == ["app", "main"]
    assert updated_plan["audit_roles"] == ["audit"]
    assert updated_plan["assignee_roles"] == ["unassigned", "designer", "app", "main", "audit", "director", "user"]
    assert "Add auditor role audit to the existing project workflow for mefp" in add_role_sql
    assert "('audit', 'Audit', 3, ARRAY['audit']::text[]" in add_role_sql
    assert "('analysis', 'in_progress', 'start_work', ARRAY['app', 'main']::text[]" in add_role_sql
    assert updated_artifact["project"]["roles"] == ["app", "main"]
    assert updated_artifact["project"]["audit_roles"] == ["audit"]
    assert updated_artifact["project"]["include_audit"] is True
    assert updated_artifact["project"]["role_clis"]["audit"] == "claude"

def test_add_role_uses_project_pane_launcher_when_run_as_user_differs() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "eric"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-owner-launcher.") as tmp:
            tmp_path = Path(tmp)
            provision_dir = tmp_path / "project" / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            project_repo = tmp_path / "project-repo"
            project_repo.mkdir()
            owner_launcher = (
                tmp_path
                / "home"
                / "porter-agent"
                / "mefp-ticketboard-live"
                / "current"
                / "scripts"
                / "team-launcher"
            )
            owner_launcher.parent.mkdir(parents=True)
            owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            owner_launcher.chmod(0o755)
            config_path = provision_dir / "mefp.json"
            layout_path = provision_dir / "mefp-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "desktop_access": {"mode": "headless"},
                        "project": "mefp",
                        "run_as_user": "porter-agent",
                        "layout": str(layout_path),
                        "pane_launcher": str(owner_launcher),
                        "repository": str(project_repo),
                        "roles": [
                            {"role": "app", "slot": 0, "cli": ["codex"], "target": "mefp-app:0.0"},
                            {"role": "main", "slot": 1, "cli": ["codex"], "target": "mefp-main:0.0"},
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_project_config("mefp", config_path)
            runner = KeywordRecordingFakeRunner()

            assert (
                team_launcher.add_project_role_command(
                    config,
                    config_path=config_path,
                    role_name="ops",
                    cli="codex",
                    script_path=ROOT / "scripts" / "team-launcher",
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

        pane_calls = [
            call
            for call in runner.calls
            if call[:4] == ["sudo", "-u", "porter-agent", "-H"] and call[5:8] == ["mefp", "pane", "attach-or-start"]
        ]
    finally:
        team_launcher.current_user_name = original_current_user_name

    assert len(pane_calls) == 1
    assert pane_calls[0][4] == str(owner_launcher)
    assert str(ROOT / "scripts" / "team-launcher") not in pane_calls[0]

def test_add_role_can_recover_half_added_role_without_reappending_config() -> None:
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.current_user_name = lambda: "eric"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-add-role-recovery.") as tmp:
            tmp_path = Path(tmp)
            provision_dir = tmp_path / "project" / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            project_repo = tmp_path / "project-repo"
            project_repo.mkdir()
            owner_launcher = (
                tmp_path
                / "home"
                / "porter-agent"
                / "mefp-ticketboard-live"
                / "current"
                / "scripts"
                / "team-launcher"
            )
            owner_launcher.parent.mkdir(parents=True)
            owner_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            owner_launcher.chmod(0o755)
            plan = team_launcher.build_plan(
                project="mefp",
                owner_user="porter-agent",
                port=18811,
                source_repo=tmp_path / "source-repo",
                implementer_roles=("app", "main"),
            )
            (tmp_path / "source-repo").mkdir()
            (provision_dir / "plan.json").write_text(
                json.dumps(plan.__dict__, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (provision_dir / plan.board_unit).write_text(team_launcher.render_board_unit(plan), encoding="utf-8")
            config_path = provision_dir / "mefp.json"
            layout_path = provision_dir / "mefp-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(3), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "desktop_access": {"mode": "headless"},
                        "project": "mefp",
                        "run_as_user": "porter-agent",
                        "layout": str(layout_path),
                        "pane_launcher": str(owner_launcher),
                        "repository": str(project_repo),
                        "roles": [
                            {"role": "app", "slot": 0, "cli": ["codex"], "target": "mefp-app:0.0"},
                            {"role": "main", "slot": 1, "cli": ["codex"], "target": "mefp-main:0.0"},
                            {"role": "ops", "slot": 2, "cli": ["codex"], "target": "mefp-ops:0.0"},
                        ],
                    },
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
                    team_launcher.add_project_role_command(
                        config,
                        config_path=config_path,
                        role_name="ops",
                        cli="codex",
                        script_path=ROOT / "scripts" / "team-launcher",
                        pane_state_dir=tmp_path / "pane-state",
                        runner=runner,
                    )
                    == 0
                )

            updated_config_json = json.loads(config_path.read_text(encoding="utf-8"))
            updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
            add_role_sql = (provision_dir / "mefp-add-role.sql").read_text(encoding="utf-8")
            output = stdout.getvalue()
    finally:
        team_launcher.current_user_name = original_current_user_name

    assert [role["role"] for role in updated_config_json["roles"]] == ["app", "main", "ops"]
    assert updated_plan["implementer_roles"] == ["app", "main", "ops"]
    assert "ARRAY['app', 'main', 'ops']::text[]" in add_role_sql
    assert any(call[:4] == ["sudo", "-u", "postgres", "psql"] for call in runner.calls)
    pane_calls = [
        call
        for call in runner.calls
        if call[:4] == ["sudo", "-u", "porter-agent", "-H"] and call[5:8] == ["mefp", "pane", "attach-or-start"]
    ]
    assert len(pane_calls) == 1
    assert pane_calls[0][4] == str(owner_launcher)
    assert "role ops already exists in mefp; reapplied board registration" in output
    assert "started tmux session for the existing role" in output

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_new_project_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
