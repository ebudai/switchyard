#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

import stat

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
                    # The role's Unix account must exist before it can be started.
                    account_exists=lambda _account: True,
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
    pane_calls = [
        call
        for call in runner.calls
        if "pane" in call and call[call.index("pane") - 1 : call.index("pane") + 3]
        == ["mefp", "pane", "attach-or-start", "ops"]
    ]
    assert len(pane_calls) == 1
    assert "mefp-ops" not in pane_calls[0][:3]
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
                    # The role's Unix account must exist before it can be started.
                    account_exists=lambda _account: True,
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
    pane_calls = [
        call
        for call in runner.calls
        if "pane" in call and call[call.index("pane") - 1 : call.index("pane") + 3]
        == ["mefp", "pane", "attach-or-start", "audit_gpt"]
    ]
    assert len(pane_calls) == 1
    assert "mefp-audit_gpt" not in pane_calls[0][:3]
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
                # The role's Unix account must exist before it can be started.
                account_exists=lambda _account: True,
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
                    # The role's Unix account must exist before it can be started.
                    account_exists=lambda _account: True,
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
            if call[:4] == ["sudo", "-u", "porter-agent", "-H"]
            and call[5:8] == ["mefp", "pane", "attach-or-start"]
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
                        # The role's Unix account must exist before it can be started.
                        account_exists=lambda _account: True,
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


def test_generated_roles_run_as_the_project_account() -> None:
    """SYRD-69: fresh provisioning creates no per-role Unix identity."""
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="switchyard-role-accounts.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()

        with redirect_stdout(StringIO()):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=FakeRunner(),
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )

        config = json.loads((output_dir / "porter.json").read_text(encoding="utf-8"))
        board_unit = (output_dir / "porter-ticket-board.service").read_text(encoding="utf-8")
        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")
        loaded = load_project_config("porter", output_dir / "porter.json")

    assert config["role_state_isolation"] is True
    assert all("run_as_user" not in role for role in config["roles"])
    sessions = {role["tmux_session"] for role in config["roles"]}
    assert len(sessions) == len(config["roles"]), sessions

    assert "TICKET_BOARD_ROLE_ACCOUNTS" not in board_unit
    assert "getent passwd 'porter-director'" not in commands

    for role in loaded.roles:
        assert team_launcher.role_run_as_user(loaded, role) == current_user
        assert team_launcher.role_session_dir(loaded, role).name == role.role



def test_every_role_lifecycle_path_runs_as_project_account() -> None:
    """SYRD-39: the uid the board sees must be the role's, on every path.

    The regression this pins is precise: the layout selected the role account,
    then the pane dispatcher re-exec'd as the shared project owner, so the CLI
    that finally ran -- and therefore every board write -- carried the wrong
    uid while the generated config looked correct.
    """
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="switchyard-role-lifecycle.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()

        with redirect_stdout(StringIO()):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=FakeRunner(),
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )
        config_path = output_dir / "porter.json"
        config = load_project_config("porter", config_path)

        for role in config.roles:
            account = team_launcher.role_run_as_user(config, role)
            assert account == current_user, (role.role, account)

            # The command the launcher hands the window manager.
            outer = team_launcher.pane_command_args(
                config.project,
                role,
                config_path=config_path,
                mode="attach-or-start",
                script_path=Path("/opt/switchyard/current/scripts/team-launcher"),
                run_as_user=account,
            )
            assert outer[0] != "sudo", (role.role, outer[:4])

            # And the command the pane dispatcher re-execs when it is not
            # already that account. This is the step that used to drop back to
            # the shared owner.
            dispatch = team_launcher.pane_command_args(
                config.project,
                role,
                config_path=config_path,
                mode="attach-or-start",
                script_path=Path("/opt/switchyard/current/scripts/team-launcher"),
                run_as_user=team_launcher.role_run_as_user(config, role),
            )
            assert dispatch[0] != "sudo", (role.role, dispatch[:4])

            # Runtime state is per account too, so a role can still write it
            # once it stops running as the project owner.
            state_dir = team_launcher.default_pane_state_dir_for_user(account, project=config.project)
            assert account in str(state_dir) or str(state_dir).startswith("/run/user/"), (role.role, state_dir)


def test_upgrade_splits_existing_project_account_session_state() -> None:
    """SYRD-39: a tenant provisioned before per-role identities must migrate.

    Its config has no run_as_user at all, so without this every role keeps
    launching as the shared owner and the board cannot tell them apart.
    """
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="switchyard-role-upgrade.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()

        with redirect_stdout(StringIO()):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=FakeRunner(),
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )
        config_path = output_dir / "porter.json"

        # Age the config back to the pre-migration shape.
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        for role in payload["roles"]:
            role.pop("run_as_user", None)
        payload.pop("role_state_isolation", None)
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        legacy = load_project_config("porter", config_path)
        assert all(not role.run_as_user for role in legacy.roles)
        # Before migration every role resolves to the one shared account, which
        # is exactly what the board refuses to tell apart.
        assert len({team_launcher.role_run_as_user(legacy, role) for role in legacy.roles}) == 1

        changed, problems = team_launcher.repatriate_role_runtime_state(
            legacy, config_path=config_path, runner=FakeRunner()
        )
        assert changed and not problems
        upgraded = load_project_config("porter", config_path)
        assert upgraded.role_state_isolation
        assert all(not role.run_as_user for role in upgraded.roles)

        # Re-running is a no-op rather than a rewrite.
        changed_again, _ = team_launcher.repatriate_role_runtime_state(
            upgraded, config_path=config_path, runner=FakeRunner()
        )
        assert not changed_again



def test_each_role_gets_its_own_runtime_paths_and_prepared_tooling() -> None:
    """SYRD-39: a role account must be able to use the paths it is handed.

    The launcher used to pass one project-wide session directory under the
    owner's home into every role's CLI, which a role account cannot write, and
    the rollout prepared hooks and the board skill only for the owner.
    """
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="switchyard-role-runtime.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()

        with redirect_stdout(StringIO()):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=FakeRunner(),
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )
        config = load_project_config("porter", output_dir / "porter.json")
        commands = (output_dir / "operator-commands.sh").read_text(encoding="utf-8")

    for role in config.roles:
        session_dir = str(team_launcher.role_session_dir(config, role))
        assert session_dir == f"/home/{current_user}/.local/state/porter-ticket-board/pane-sessions/roles/{role.role}", (
            role.role,
            session_dir,
        )
        assert str(config.session_dir) != session_dir, (role.role, session_dir)
        state_dir = str(team_launcher.role_pane_state_dir(config, role))
        assert current_user in state_dir or state_dir.startswith("/run/user/"), (role.role, state_dir)
        assert f"getent passwd 'porter-{role.role}'" not in commands


def test_add_role_prepares_the_new_role_before_it_can_be_started() -> None:
    """SYRD-39: a rerun after creating the account must find a usable role.

    Creating the account alone leaves the role with an owner-owned worktree and
    no hooks, skill or runtime paths, so it would start under the right uid and
    be unable to work.
    """
    from scripts.ticket_board.project_provision import role_account_commands

    commands = role_account_commands(
        "otto",
        "perf",
        "otto-agent",
        "boardsvc",
        # SYRD-51: the control interface is installed whole, so the emitter takes
        # the project's whole role set rather than rendering it from one role.
        role_accounts=(("director", "otto-director"), ("perf", "otto-perf")),
        worktree="/home/otto-agent/otto-worktrees/perf",
    )
    assert "if ! getent passwd 'otto-perf'" in commands
    assert "sudo gpasswd -a 'otto-perf' 'otto-roles'" in commands
    # The tree it will work in becomes its own.
    assert "sudo chown -R 'otto-perf': '/home/otto-agent/otto-worktrees/perf'" in commands
    # The runtime paths the launcher will hand it.
    assert "/home/otto-perf/.local/state/otto-ticket-board/pane-sessions" in commands
    # Its own tooling, installed as itself.
    assert "ticket-board-install-pane-hooks' install --home '/home/otto-perf'" in commands
    assert "switchyard-board-skill' install --home '/home/otto-perf'" in commands
    # And the control interface is refreshed so the director can drive it -- from
    # the document the artifact carries, not from a command that regenerates it.
    assert "otto-director ALL=(otto-perf) NOPASSWD: /usr/bin/tmux" in commands
    assert "visudo -c -f /etc/sudoers.d/49-otto-role-control.staged" in commands
    assert "switchyard provision" not in commands




# The owner's real hermes .env mixes inference-provider keys with the owner's
# login password, messaging identities, GitHub and tool credentials, and general
# settings -- hermes' own terminal tool consumes SUDO_PASSWORD. None of that may
# reach a role (SYRD-39).
HERMES_ENV_BAIT_KEYS = (
    "SUDO_PASSWORD",
    "DISCORD_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "GITHUB_TOKEN",
    "MATRIX_PASSWORD",
    "TERMINAL_SSH_KEY",
    "NOTION_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "BROWSER_CDP_URL",
    "LLM_MODEL",
)
HERMES_ENV_WITH_BAIT = "\n".join(
    [
        "# owner environment",
        "OPENROUTER_API_KEY=or-secret",
        "OPENAI_BASE_URL=https://api.openai.com",
        "SUDO_PASSWORD=hunter2",
        "DISCORD_BOT_TOKEN=discord-secret",
        "TELEGRAM_BOT_TOKEN=telegram-secret",
        "SLACK_BOT_TOKEN=slack-secret",
        "GITHUB_TOKEN=github-secret",
        "MATRIX_PASSWORD=matrix-secret",
        "TERMINAL_SSH_KEY=/home/owner/.ssh/id_ed25519",
        "NOTION_API_KEY=notion-secret",
        "BRAVE_SEARCH_API_KEY=brave-secret",
        "BROWSER_CDP_URL=http://127.0.0.1:9222",
        "LLM_MODEL=some-model",
    ]
) + "\n"


def test_hermes_seeding_shares_only_inference_provider_credentials() -> None:
    """SYRD-39: KEY=value grammar stops shell, not over-sharing.

    Hermes keeps provider keys in the same file as the owner's sudo password and
    external identities, so the file is parsed against a provider allowlist and a
    NEW file is constructed; the source is never copied.
    """
    content, omitted, problem = team_launcher.select_hermes_provider_env(HERMES_ENV_WITH_BAIT)
    assert problem == "", problem
    assert "OPENROUTER_API_KEY=or-secret" in content
    assert "OPENAI_BASE_URL=https://api.openai.com" in content
    for bait in HERMES_ENV_BAIT_KEYS:
        assert bait not in content, bait
        assert bait in omitted, bait
    # No value from an omitted entry survives either.
    for secret in ("hunter2", "discord-secret", "github-secret", "matrix-secret", "notion-secret"):
        assert secret not in content, secret

    # A file with no provider credentials at all fails closed rather than
    # producing an empty, useless credential.
    _content, _omitted, only_bait = team_launcher.select_hermes_provider_env(
        "SUDO_PASSWORD=hunter2\nGITHUB_TOKEN=gh\n"
    )
    assert "no inference-provider credentials" in only_bait, only_bait

    # Grammar is still enforced, so nothing shell-shaped is accepted.
    _content, _omitted, malformed = team_launcher.select_hermes_provider_env("rm -rf /\n")
    assert "not KEY=value" in malformed, malformed


def _isolated_project_config(tmp_path: Path, clis: dict[str, str]) -> "team_launcher.ProjectConfig":
    """A migrated project: every role has its own account and its own worktree."""
    roles = []
    for index, (name, cli) in enumerate(sorted(clis.items())):
        workdir = tmp_path / "worktrees" / name
        workdir.mkdir(parents=True)
        roles.append(
            {
                "role": name,
                "slot": index,
                "tmux_session": f"porter-{name}",
                "target": f"porter-{name}:0.0",
                "cli": [cli],
                "workdir": str(workdir),
                "run_as_user": f"porter-{name}",
            }
        )
    config_path = tmp_path / "porter.json"
    config_path.write_text(
        json.dumps({"project": "porter", "run_as_user": "porter-agent", "roles": roles}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return load_project_config("porter", config_path)


def _fake_home(home_base: Path, user: str) -> Path:
    home = home_base / user
    home.mkdir(parents=True, exist_ok=True)
    return home


def test_role_credentials_are_private_copies_of_an_allowlist_only() -> None:
    """SYRD-39: seed the minimum each CLI needs, as the role's own private file.

    Never a whole CLI home, never a symlink, never a shared readable directory.
    Every supported CLI is covered here so a new one cannot be added silently.
    """
    chowns: list[list[str]] = []

    def runner(args, **_kwargs):
        chowns.append(list(args))
        return subprocess.CompletedProcess(list(args), 0)

    with tempfile.TemporaryDirectory(prefix="switchyard-role-creds.") as tmp:
        home_base = Path(tmp)
        config = _isolated_project_config(
            home_base / "project", {"director": "claude", "ops": "codex", "app": "agy", "perf": "hermes"}
        )
        owner_home = _fake_home(home_base, "porter-agent")
        # The owner is authenticated for the three file-based CLIs, plus a pile
        # of things that must never be copied.
        for relative in (
            ".claude/.credentials.json",
            ".codex/auth.json",
            ".gemini/antigravity-cli/antigravity-oauth-token",
            ".hermes/.env",
        ):
            path = owner_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            # Hermes reads a plain KEY=value environment file; the others are
            # opaque token blobs.
            body = (
                HERMES_ENV_WITH_BAIT
                if relative.endswith(".hermes/.env")
                else f"secret for {relative}"
            )
            path.write_text(body, encoding="utf-8")
            path.chmod(0o600)
        for noise in (".claude/sessions/a.jsonl", ".codex/session_index.jsonl", ".claude/settings.json"):
            path = owner_home / noise
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("must not be copied", encoding="utf-8")

        for role in config.roles:
            _fake_home(home_base, f"porter-{role.role}")

        # The SYRD-28 reachability proof requires the accounts to exist on the
        # host, which a test cannot create. Its own suite covers that check; this
        # one covers what is seeded, where, and with what ownership and modes.
        original_home_check = team_launcher._require_owner_home_traversable
        original_component_check = team_launcher._require_owner_traversable
        original_uid_for_user = team_launcher.uid_for_user
        team_launcher._require_owner_home_traversable = lambda *_args, **_kwargs: None
        team_launcher._require_owner_traversable = lambda *_args, **_kwargs: None
        # The seeded files are created by this test process, so every account in
        # the fixture resolves to it; the ownership CHECK is still exercised,
        # against a uid it can actually observe.
        team_launcher.uid_for_user = lambda _user: os.getuid()
        try:
            messages: list[str] = []
            assert (
                team_launcher.switchyard_seed_role_credentials_command(
                    config, home_base=home_base, runner=runner, print_func=messages.append
                )
                == 0
            )

            expected = {
                "director": ".claude/.credentials.json",
                "ops": ".codex/auth.json",
                "app": ".gemini/antigravity-cli/antigravity-oauth-token",
                # Hermes reads its keys from its own HERMES_HOME, which the
                # launcher already points the role at, so nothing is injected
                # into the environment and no shell file is sourced.
                "perf": ".hermes/.env",
            }
            for role_name, relative in expected.items():
                role = next(item for item in config.roles if item.role == role_name)
                artifact = next(
                    item
                    for item in team_launcher.role_credential_artifacts(role)
                    if item.relative_path.endswith(Path(relative).name)
                )
                base, target_relative = team_launcher._role_credential_target(
                    config, role, artifact, home_base=home_base
                )
                seeded = base / target_relative
                assert seeded.is_file(), (role_name, seeded)
                assert not seeded.is_symlink(), role_name
                assert stat.S_IMODE(seeded.stat().st_mode) == 0o600, (role_name, oct(seeded.stat().st_mode))
                assert stat.S_IMODE(seeded.parent.stat().st_mode) == 0o700, (role_name, oct(seeded.parent.stat().st_mode))
                if relative.endswith(".hermes/.env"):
                    # A newly constructed file: the provider key crosses and
                    # nothing else does.
                    seeded_env = seeded.read_text(encoding="utf-8")
                    assert "OPENROUTER_API_KEY=or-secret" in seeded_env, seeded_env
                    for bait in HERMES_ENV_BAIT_KEYS:
                        assert bait not in seeded_env, (bait, seeded_env)
                    assert "hunter2" not in seeded_env, seeded_env
                else:
                    assert seeded.read_text(encoding="utf-8") == f"secret for {relative}", role_name

            # Nothing outside the allowlist crossed over.
            for role_name in expected:
                role_home = home_base / f"porter-{role_name}"
                copied = {str(path.relative_to(role_home)) for path in role_home.rglob("*") if path.is_file()}
                assert len(copied) == 1, (role_name, copied)
                assert next(iter(copied)).endswith(Path(expected[role_name]).name), (role_name, copied)

            # Every copy is assigned to its own role, by file descriptor.
            assigned = {args[1] for args in chowns if args and args[0] == "chown"}
            assert {
                "porter-director:porter-director",
                "porter-ops:porter-ops",
                "porter-app:porter-app",
                "porter-perf:porter-perf",
            } <= assigned, assigned
            assert all(args[2].startswith("/proc/") for args in chowns if args and args[0] == "chown"), chowns

            # Idempotent: a second run changes nothing.
            second: list[str] = []
            assert (
                team_launcher.switchyard_seed_role_credentials_command(
                    config, home_base=home_base, runner=runner, print_func=second.append
                )
                == 0
            )
            assert any("already has" in message for message in second), second
            assert (home_base / "porter-ops/.codex/auth.json").read_text(encoding="utf-8") == "secret for .codex/auth.json"

            # --reseed is the deliberate repair path.
            (owner_home / ".codex/auth.json").write_text("rotated", encoding="utf-8")
            assert (
                team_launcher.switchyard_seed_role_credentials_command(
                    config, role_name="ops", reseed=True, home_base=home_base, runner=runner, print_func=lambda _m: None
                )
                == 0
            )
            assert (home_base / "porter-ops/.codex/auth.json").read_text(encoding="utf-8") == "rotated"
        finally:
            team_launcher._require_owner_home_traversable = original_home_check
            team_launcher._require_owner_traversable = original_component_check
            team_launcher.uid_for_user = original_uid_for_user


def test_seeding_content_cannot_be_redirected_after_validation() -> None:
    """SYRD-39: validate and read the same open file, never the same name twice.

    Reproduces the reported race: the owner artifact is validated, then swapped
    for a symlink to an outside file, then restored. A pathname read between
    those two points hands the privileged command bytes from outside the
    approved artifact; reading from the already-validated descriptor cannot.
    """
    chowns: list[list[str]] = []

    def runner(args, **_kwargs):
        chowns.append(list(args))
        return subprocess.CompletedProcess(list(args), 0)

    with tempfile.TemporaryDirectory(prefix="switchyard-cred-swap.") as tmp:
        home_base = Path(tmp)
        config = _isolated_project_config(home_base / "project", {"perf": "hermes"})
        owner_home = _fake_home(home_base, "porter-agent")
        _fake_home(home_base, "porter-perf")
        source = owner_home / ".hermes/.env"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("OPENROUTER_API_KEY=approved-secret\n", encoding="utf-8")
        source.chmod(0o600)

        outside = home_base / "outside.env"
        outside.write_text("OPENAI_API_KEY=redirected-secret\n", encoding="utf-8")
        outside.chmod(0o600)

        swapped: list[bool] = []
        real_open = os.open

        def swapping_open(path, flags, *args, **kwargs):
            # Swap the name for a symlink to the attacker's file the moment the
            # approved artifact has been opened, then restore it. A later read
            # by pathname would follow the swap.
            result = real_open(path, flags, *args, **kwargs)
            if not swapped and isinstance(path, str) and path.endswith(".env"):
                try:
                    source.unlink()
                    source.symlink_to(outside)
                    swapped.append(True)
                except OSError:
                    pass
            return result

        original_home_check = team_launcher._require_owner_home_traversable
        original_component_check = team_launcher._require_owner_traversable
        original_uid_for_user = team_launcher.uid_for_user
        team_launcher._require_owner_home_traversable = lambda *_a, **_k: None
        team_launcher._require_owner_traversable = lambda *_a, **_k: None
        team_launcher.uid_for_user = lambda _user: os.getuid()
        os.open = swapping_open
        try:
            team_launcher.switchyard_seed_role_credentials_command(
                config, home_base=home_base, runner=runner, print_func=lambda _m: None
            )
        finally:
            os.open = real_open
            team_launcher._require_owner_home_traversable = original_home_check
            team_launcher._require_owner_traversable = original_component_check
            team_launcher.uid_for_user = original_uid_for_user

        assert swapped, "the swap never fired; the test would prove nothing"
        role = config.roles[0]
        artifact = team_launcher.role_credential_artifacts(role)[0]
        base, relative = team_launcher._role_credential_target(
            config, role, artifact, home_base=home_base
        )
        seeded = base / relative
        body = seeded.read_text(encoding="utf-8") if seeded.exists() else ""
        # Either the approved bytes, or nothing at all. Never the outside file.
        assert "redirected-secret" not in body, body
        if body:
            assert "approved-secret" in body, body


def test_missing_owner_credential_fails_closed_with_a_manifest() -> None:
    """A role must not launch unauthenticated; the manifest says exactly why."""
    with tempfile.TemporaryDirectory(prefix="switchyard-cred-manifest.") as tmp:
        home_base = Path(tmp)
        config = _isolated_project_config(home_base / "project", {"ops": "codex"})
        _fake_home(home_base, "porter-agent")
        _fake_home(home_base, "porter-ops")

        original_home_dir_for_user = team_launcher.home_dir_for_user
        original_uid_for_user_outer = team_launcher.uid_for_user
        team_launcher.home_dir_for_user = lambda user: home_base / user
        # The fixture's accounts do not exist on the test host; the ownership
        # check is still exercised, against a uid it can observe.
        team_launcher.uid_for_user = lambda _user: os.getuid()
        try:
            manifest = team_launcher.role_credential_manifest(config)
            assert any(
                "the owner's .codex/auth.json missing" in line and "ops" in line
                for line in manifest
            ), manifest

            # Present but UNSAFE is refused too, not treated as ready: a
            # wrong-mode, wrong-owner or symlinked credential used to pass the
            # gate because it only asked whether a file existed (SYRD-39).
            owner_home = home_base / "porter-agent"
            auth = owner_home / ".codex/auth.json"
            auth.parent.mkdir(parents=True, exist_ok=True)
            auth.write_text("secret", encoding="utf-8")
            auth.chmod(0o644)
            original_uid_for_user = team_launcher.uid_for_user
            team_launcher.uid_for_user = lambda _user: os.getuid()
            try:
                unsafe = team_launcher.role_credential_manifest(config)
                assert any("readable beyond its owner" in line for line in unsafe), unsafe

                auth.chmod(0o600)
                role_home = home_base / "porter-ops"
                seeded = role_home / ".codex/auth.json"
                seeded.parent.mkdir(parents=True, exist_ok=True)
                seeded.parent.chmod(0o700)
                # A symlink where the private copy should be must not count.
                seeded.symlink_to(auth)
                linked = team_launcher.role_credential_manifest(config)
                assert any("is a symlink" in line for line in linked), linked
                seeded.unlink()

                # A world-readable private copy must not count either.
                seeded.write_text("secret", encoding="utf-8")
                seeded.chmod(0o644)
                loose = team_launcher.role_credential_manifest(config)
                assert any("readable beyond its owner" in line for line in loose), loose
                seeded.chmod(0o600)
                assert team_launcher.role_credential_manifest(config) == []

                # An ancestor symlink several levels up is refused. lstat on the
                # leaf and its immediate parent could not see this: the whole
                # subtree belongs to wherever the ancestor points (SYRD-39).
                seeded.unlink()
                seeded.parent.rmdir()
                elsewhere = home_base / "elsewhere"
                (elsewhere / "codex").mkdir(parents=True)
                (elsewhere / "codex").chmod(0o700)
                planted = elsewhere / "codex" / "auth.json"
                planted.write_text("attacker", encoding="utf-8")
                planted.chmod(0o600)
                (role_home / ".codex").symlink_to(elsewhere / "codex")
                via_symlink = team_launcher.role_credential_manifest(config)
                assert any("ancestor .codex is a symlink" in line for line in via_symlink), via_symlink

                # The same ancestor symlink is refused on the OWNER side, where
                # the check was weaker still.
                (role_home / ".codex").unlink()
                owner_codex = owner_home / ".codex"
                for child in list(owner_codex.iterdir()):
                    child.unlink()
                owner_codex.rmdir()
                owner_codex.symlink_to(elsewhere / "codex")
                owner_symlinked = team_launcher.role_credential_manifest(config)
                assert any(
                    "ancestor .codex is a symlink" in line for line in owner_symlinked
                ), owner_symlinked
                # Restore the fixture so the fail-closed assertion below sees
                # the state it is about.
                owner_codex.unlink()
            finally:
                team_launcher.uid_for_user = original_uid_for_user
                auth.unlink(missing_ok=True)
            try:
                team_launcher.switchyard_seed_role_credentials_command(
                    config, home_base=home_base, runner=lambda *a, **k: None, print_func=lambda _m: None
                )
            except SystemExit as exc:
                assert "authenticate codex" in str(exc), str(exc)
            else:
                raise AssertionError("seeding must fail closed when the owner has no credential")
        finally:
            team_launcher.home_dir_for_user = original_home_dir_for_user
            team_launcher.uid_for_user = original_uid_for_user_outer

def test_role_publishing_takes_nothing_about_where_from_its_caller() -> None:
    """SYRD-39, under one Unix account per project (SYRD-69, SYRD-93).

    The old shape of this took --ref and --bundle and read the calling role from
    SUDO_USER. With one account there is no role in SUDO_USER to read, and a
    caller that names its own ref is a caller that publishes whatever it likes.
    The publisher now takes one board request id and reads the rest from the
    board's own record, so the flags a caller could aim are simply gone.

    The trust rules those flags used to need -- the registry, the owner tree,
    the ref namespace, the bundle, the credential, the process asking -- are
    driven against the real program in switchyard_publish_ref_test.
    """
    helper = ROOT / "scripts" / "switchyard-publish-ref"

    def run(*extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(helper), *extra],
            capture_output=True,
            text=True,
            env={**os.environ, "SUDO_USER": team_launcher.current_user_name()},
        )

    for flag in ("--ref", "--bundle", "--repository", "--remote", "--commit"):
        rejected = run("--project", "porter", "--request", "1", flag, "value")
        assert rejected.returncode != 0, (flag, rejected.stdout)
        assert "unrecognized arguments" in rejected.stderr, (flag, rejected.stderr)

    # And the request id is required: there is no default publication.
    missing = run("--project", "porter")
    assert missing.returncode != 0
    assert "--request" in missing.stderr, missing.stderr

    # The project identifier is validated before it builds any path. A
    # traversal used to load a role-written document, and with it a role-chosen
    # remote (SYRD-39).
    traversal = run("--project", "../../tmp/forged", "--request", "1")
    assert traversal.returncode != 0, traversal.stdout
    assert "is not a valid project name" in traversal.stderr + traversal.stdout


def test_fresh_provisioning_emits_no_role_account_handoff() -> None:
    """SYRD-69: provisioning ends with the project account and launch gate."""
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="switchyard-handoff.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        printed: list[str] = []
        # SYRD-62: the handoff is published where only root can have written it,
        # so provisioning is driven as the root it is on a host.
        privileged_root = tmp_path / "etc-switchyard"

        with _provisioning_as_root(privileged_root), redirect_stdout(StringIO()):
            assert (
                new_project_command(
                    "porter",
                    owner_user=current_user,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    repository=project_repo,
                    output_dir=output_dir,
                    runner=FakeRunner(),
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                    print_func=printed.append,
                )
                == 0
            )

        handoff = privileged_root / "porter" / "porter-role-accounts.sh"
        assert not handoff.exists(), printed
        assert not (output_dir / "porter-role-accounts.sh").exists(), sorted(
            path.name for path in output_dir.iterdir()
        )


def test_fresh_project_launches_without_a_role_account_handoff() -> None:
    """SYRD-69: the project account can pass directly into the launch gate."""
    from scripts import presentation_controller

    class NewProjectRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    polled: list[object] = []
    printed: list[str] = []
    original_report = team_launcher.report_launch_session_records
    original_authority_probe = team_launcher.process_authority_board_compatibility
    original_runtime_config = presentation_controller.runtime_assignment_config
    try:
        team_launcher.report_launch_session_records = lambda *a, **kw: polled.append(kw) or []
        team_launcher.process_authority_board_compatibility = lambda _config: (
            True, "simulated provisioned process-authority board"
        )
        presentation_controller.runtime_assignment_config = lambda config: config
        with tempfile.TemporaryDirectory(prefix="switchyard-deferred-launch.") as tmp:
            tmp_path = Path(tmp)
            source_repo = tmp_path / "source-repo"
            source_repo.mkdir()
            # SYRD-62: the handoff it prints is root's published copy, so
            # root's directory goes in the sandbox. The command is already told
            # it is root through its own euid_getter, which is what publishing
            # asks -- faking the process's would send its unrelated chowns at
            # the real filesystem.
            privileged_root = tmp_path / "etc-switchyard"
            os.environ["SWITCHYARD_PRIVILEGED_PROVISION_ROOT"] = str(privileged_root)
            with redirect_stdout(StringIO()):
                assert (
                    switchyard_new_command(
                        desktop_policy=Path("headless"),
                        slug="porter",
                        agent_name="otto-agent",
                        project_name="Porter System",
                        source_repo=source_repo,
                        output_dir=tmp_path / "out",
                        role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                        yes=True,
                        allow_existing_owner_user=True,
                        home_base=tmp_path / "home",
                        euid_getter=lambda: 0,
                        runner=NewProjectRunner(),
                        input_func=lambda _prompt: "",
                        port_in_use=lambda _port: False,
                        socket_exists=lambda _path: False,
                        registry_dir=tmp_path / "registry",
                        konsole_process_launcher=RecordingProcessLauncher(),
                        print_func=printed.append,
                    )
                    == 0
                )
    finally:
        team_launcher.report_launch_session_records = original_report
        team_launcher.process_authority_board_compatibility = original_authority_probe
        presentation_controller.runtime_assignment_config = original_runtime_config
        os.environ.pop("SWITCHYARD_PRIVILEGED_PROVISION_ROOT", None)

    assert not any("were not started" in line for line in printed), printed
    assert not any("porter-role-accounts.sh" in line for line in printed), printed
    assert any("full pane window started" in line for line in printed), printed
    assert len(polled) == 1, polled
    assert any("design" in line for line in printed), printed


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_new_project_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
