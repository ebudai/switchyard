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
        if call[:3] == ["sudo", "-u", "mefp-ops"]
        and call[call.index("pane") - 1 : call.index("pane") + 3]
        == ["mefp", "pane", "attach-or-start", "ops"]
    ]
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
        if call[:3] == ["sudo", "-u", "mefp-audit_gpt"]
        and call[call.index("pane") - 1 : call.index("pane") + 3]
        == ["mefp", "pane", "attach-or-start", "audit_gpt"]
    ]
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
            # The pane now runs as the role's own account rather than the
            # project owner; the project pane_launcher choice is what this
            # test is about and is unchanged (SYRD-39).
            if call[:4] == ["sudo", "-u", "mefp-ops", "-H"] and call[5:8] == ["mefp", "pane", "attach-or-start"]
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


def test_generated_roles_each_run_as_their_own_unix_account() -> None:
    """SYRD-39: per-role accounts are what make the board's uid mapping real.

    They also give each role its own tmux server, because a tmux server is
    per-user, which removes the shared server a role could otherwise reach into.
    """
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

    accounts = {role["role"]: role.get("run_as_user", "") for role in config["roles"]}
    assert accounts, config
    for role_name, account in accounts.items():
        assert account == f"porter-{role_name}", (role_name, account)
    # No two roles share an account: a shared uid cannot be told apart.
    assert len(set(accounts.values())) == len(accounts), accounts
    # Each role also has its own tmux session, under its own account, so there
    # is no shared tmux server between roles.
    sessions = {role["tmux_session"] for role in config["roles"]}
    assert len(sessions) == len(config["roles"]), sessions

    # The board is told the same table it will resolve peer uids against, and
    # the operator artifact creates exactly those accounts.
    for role_name, account in accounts.items():
        assert f"{role_name}={account}" in board_unit, role_name
        assert f"if ! getent passwd '{account}'" in commands, role_name

    for role in loaded.roles:
        assert team_launcher.role_run_as_user(loaded, role) == f"porter-{role.role}", role.role



def test_every_role_lifecycle_path_runs_as_that_role_account() -> None:
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
            assert account == f"porter-{role.role}", (role.role, account)

            # The command the launcher hands the window manager.
            outer = team_launcher.pane_command_args(
                config.project,
                role,
                config_path=config_path,
                mode="attach-or-start",
                script_path=Path("/opt/switchyard/current/scripts/team-launcher"),
                run_as_user=account,
            )
            assert outer[:3] == ["sudo", "-u", account], (role.role, outer[:4])

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
            assert dispatch[:3] == ["sudo", "-u", account], (role.role, dispatch[:4])
            assert config.run_as_user not in dispatch[:3], (role.role, dispatch[:4])

            # Runtime state is per account too, so a role can still write it
            # once it stops running as the project owner.
            state_dir = team_launcher.default_pane_state_dir_for_user(account, project=config.project)
            assert account in str(state_dir) or str(state_dir).startswith("/run/user/"), (role.role, state_dir)


def test_upgrade_gives_an_existing_shared_uid_tenant_per_role_accounts() -> None:
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
        config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        legacy = load_project_config("porter", config_path)
        assert all(not role.run_as_user for role in legacy.roles)
        # Before migration every role resolves to the one shared account, which
        # is exactly what the board refuses to tell apart.
        assert len({team_launcher.role_run_as_user(legacy, role) for role in legacy.roles}) == 1

        changed, message = team_launcher.upgrade_role_accounts_in_config(config_path)
        assert changed, message
        upgraded = load_project_config("porter", config_path)
        accounts = {role.role: role.run_as_user for role in upgraded.roles}
        assert all(account == f"porter-{name}" for name, account in accounts.items()), accounts
        assert len(set(accounts.values())) == len(accounts), accounts

        # Re-running is a no-op rather than a rewrite.
        changed_again, _ = team_launcher.upgrade_role_accounts_in_config(config_path)
        assert not changed_again

        # And the operator artifact moves ownership of what each role uses.
        migration = team_launcher.render_role_account_migration(upgraded)
        for role in upgraded.roles:
            account = f"porter-{role.role}"
            assert f"if ! getent passwd '{account}'" in migration, role.role
            assert f"sudo chown -R '{account}': '{role.workdir}'" in migration, role.role
        assert "visudo -c" in migration
        assert "systemctl restart porter-ticket-board.service" in migration



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
        account = f"porter-{role.role}"
        # The CLI is handed a session directory the role account owns, not the
        # project owner's.
        # Project-scoped and ambient-free: the path must name THIS project and
        # this role's account, not whatever project the calling pane belongs to.
        session_dir = str(team_launcher.role_session_dir(config, role))
        assert session_dir == f"/home/{account}/.local/state/porter-ticket-board/pane-sessions", (
            role.role,
            session_dir,
        )
        assert str(config.session_dir) != session_dir, (role.role, session_dir)
        # Pane activity goes to the deliberate aggregation path: every role can
        # write it and the owner's notify listener can read it. Per-role
        # /run/user directories are writable by the role and unreadable by the
        # listener, which is where the state used to go unread (SYRD-39).
        state_dir = str(team_launcher.role_pane_state_dir(config, role))
        assert state_dir == "/run/porter-ticket-board/pane-state", (role.role, state_dir)

        # And the rollout actually prepares that role: its worktree, its runtime
        # paths, its pane hooks and its board skill.
        assert f"sudo chown -R '{account}': '{role.workdir}'" in commands, role.role
        assert f"/home/{account}/.local/state/porter-ticket-board/pane-sessions" in commands, role.role
        assert f"/home/{account}/.local/bin/ticket-board-pane-idle-hook" in commands, role.role
        assert f"ticket-board-install-pane-hooks' install --home '/home/{account}'" in commands, role.role
        assert f"switchyard-board-skill' install --home '/home/{account}'" in commands, role.role

    # Role runtime preparation runs from the deployed release, so it must come
    # after the board deploy rather than beside account creation.
    assert commands.index("useradd -m -d '/home/porter-director'") < commands.index(
        "ticket-board-install-pane-hooks' install --home '/home/porter-director'"
    ), "role runtime prepared before the release it installs from"


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
    # And the control interface is refreshed so the director can drive it.
    assert "role-control-sudoers" in commands
    assert "visudo -c" in commands



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
            ".hermes/provider.env",
        ):
            path = owner_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"secret for {relative}", encoding="utf-8")
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
                # Hermes resolves keys from the environment, and an ambient
                # environment does not survive sudo -u into another account, so
                # its key is a private file seeded like any other.
                "perf": ".hermes/provider.env",
            }
            for role_name, relative in expected.items():
                seeded = home_base / f"porter-{role_name}" / relative
                assert seeded.is_file(), (role_name, seeded)
                assert not seeded.is_symlink(), role_name
                assert stat.S_IMODE(seeded.stat().st_mode) == 0o600, (role_name, oct(seeded.stat().st_mode))
                assert stat.S_IMODE(seeded.parent.stat().st_mode) == 0o700, (role_name, oct(seeded.parent.stat().st_mode))
                assert seeded.read_text(encoding="utf-8") == f"secret for {relative}"

            # Nothing outside the allowlist crossed over.
            for role_name in expected:
                role_home = home_base / f"porter-{role_name}"
                copied = {str(path.relative_to(role_home)) for path in role_home.rglob("*") if path.is_file()}
                assert copied == {expected[role_name]}, (role_name, copied)

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


def test_missing_owner_credential_fails_closed_with_a_manifest() -> None:
    """A role must not launch unauthenticated; the manifest says exactly why."""
    with tempfile.TemporaryDirectory(prefix="switchyard-cred-manifest.") as tmp:
        home_base = Path(tmp)
        config = _isolated_project_config(home_base / "project", {"ops": "codex"})
        _fake_home(home_base, "porter-agent")
        _fake_home(home_base, "porter-ops")

        original_home_dir_for_user = team_launcher.home_dir_for_user
        team_launcher.home_dir_for_user = lambda user: home_base / user
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


def test_role_publishing_is_bound_to_the_role_and_cannot_use_its_own_git_config() -> None:
    """SYRD-39: the sudo grant allows any arguments, so the helper must not
    accept a caller-chosen repository, remote or commit, and must never run git
    inside the role's own checkout as the owner.

    A role controls its repository configuration, and core.sshCommand,
    uploadpack.packObjectsHook or an ext:: remote turn "run git there" into
    "run the role's command as the owner". The role hands over a bundle it made
    as itself; the owner only reads that.
    """
    helper = ROOT / "scripts" / "switchyard-publish-ref"
    caller = team_launcher.current_user_name()

    with tempfile.TemporaryDirectory(prefix="switchyard-publish.") as tmp:
        tmp_path = Path(tmp)
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "ops/topic", str(work)], check=True)
        (work / "file.txt").write_text("content", encoding="utf-8")
        for args in (
            ["git", "-C", str(work), "config", "user.email", "role@example.invalid"],
            ["git", "-C", str(work), "config", "user.name", "role"],
            ["git", "-C", str(work), "add", "file.txt"],
            ["git", "-C", str(work), "commit", "-q", "-m", "work"],
        ):
            subprocess.run(args, check=True)

        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "worktree_remote": str(origin),
                    "roles": [
                        {
                            "role": "ops",
                            "cli": ["codex"],
                            "tmux_session": "porter-ops",
                            "target": "porter-ops:0.0",
                            "workdir": str(work),
                            "run_as_user": caller,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        # The role makes the bundle as itself; the owner never runs git in the
        # role's checkout.
        bundle = tmp_path / "publish.bundle"
        subprocess.run(
            ["git", "-C", str(work), "bundle", "create", str(bundle), "refs/heads/ops/topic"],
            check=True,
            capture_output=True,
        )

        def run_helper(*extra: str, sudo_user: str | None = caller) -> subprocess.CompletedProcess[str]:
            env = {
                **os.environ,
                "SWITCHYARD_PUBLISH_CONFIG": str(config_path),
                "HOME": str(tmp_path / "ownerhome"),
            }
            if sudo_user is None:
                env.pop("SUDO_USER", None)
            else:
                env["SUDO_USER"] = sudo_user
            (tmp_path / "ownerhome").mkdir(exist_ok=True)
            return subprocess.run(
                [sys.executable, str(helper), "--project", "porter", *extra],
                capture_output=True,
                text=True,
                env=env,
            )

        # Nothing about where is caller-supplied: those flags do not exist.
        rejected_flags = run_helper("--ref", "ops/topic", "--bundle", str(bundle), "--repository", "/tmp")
        assert rejected_flags.returncode != 0, rejected_flags.stdout
        assert "unrecognized arguments" in rejected_flags.stderr, rejected_flags.stderr

        # Must arrive through sudo; an unattributed caller has no role.
        no_sudo = run_helper("--ref", "ops/topic", "--bundle", str(bundle), sudo_user=None)
        assert no_sudo.returncode != 0
        assert "run through sudo" in no_sudo.stderr + no_sudo.stdout

        # The integration branch is refused.
        protected = run_helper("--ref", "main", "--bundle", str(bundle))
        assert protected.returncode != 0
        assert "integration branch" in protected.stderr + protected.stdout

        # Another role's namespace is refused.
        foreign = run_helper("--ref", "director/topic", "--bundle", str(bundle))
        assert foreign.returncode != 0
        assert "may only publish refs under ops/" in foreign.stderr + foreign.stdout

        # Its own namespaced ref publishes, and main is untouched.
        published = run_helper("--ref", "ops/topic", "--bundle", str(bundle))
        assert published.returncode == 0, published.stderr
        listed = subprocess.run(
            ["git", "-C", str(origin), "for-each-ref", "--format=%(refname)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "refs/heads/ops/topic" in listed, listed
        assert "refs/heads/main" not in listed, listed


def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_new_project_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
