#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_design_writes_artifact_that_new_from_consumes_for_missing_owner_user() -> None:
    missing_owner = "pgu647-missing-agent"
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        design_dir = tmp_path / "design"
        provision_dir = tmp_path / "provision"
        source_repo = tmp_path / "source-repo"
        project_repo = tmp_path / "project-repo"
        source_repo.mkdir()
        project_repo.mkdir()
        design_stdout = StringIO()
        provision_stdout = StringIO()
        runner = FakeRunner()

        with redirect_stdout(design_stdout):
            assert (
                design_project_command(
                    "porter",
                    output_dir=design_dir,
                    design_title="Porter",
                    design_body="A small coordination tool.",
                    repository=project_repo,
                    remote="upstream",
                    default_branch="trunk",
                    worktree_policy="shared",
                    owner_user=missing_owner,
                    ticket_prefix="PORT",
                    push_policy="director-main-only",
                    audit_signoff=True,
                    needs_inspection=False,
                    needs_user_signoff=False,
                    board_service_traversal=True,
                    supplementary_groups=[],
                    linger=True,
                    owner_shell="fish",
                    input_func=_no_input,
                )
                == 0
            )
        artifact_path = design_dir / "porter.project.json"
        design_doc = design_dir / "porter-design.md"
        artifact = load_project_design_artifact(artifact_path, expected_project="porter")

        with redirect_stdout(provision_stdout):
            assert (
                new_project_command(
                    "porter",
                    from_artifact=artifact_path,
                    source_repo=source_repo,
                    output_dir=provision_dir,
                    dry_run=True,
                    runner=runner,
                    port_in_use=lambda _port: False,
                    socket_exists=lambda _path: False,
                )
                == 0
            )

        plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        config = json.loads((provision_dir / "porter.json").read_text(encoding="utf-8"))
        board_unit = (provision_dir / "porter-ticket-board.service").read_text(encoding="utf-8")
        design_doc_text = design_doc.read_text(encoding="utf-8")

    assert "team-launcher: wrote design document" in design_stdout.getvalue()
    assert "team-launcher: dry-run for porter" in provision_stdout.getvalue()
    assert design_doc_text.startswith("# Porter\n\nA small coordination tool.\n")
    assert artifact.owner_user == missing_owner
    assert artifact.repository == project_repo
    assert artifact.ticket_prefix == "PORT"
    assert plan["owner_user"] == missing_owner
    assert plan["ticket_prefix"] == "PORT"
    assert config["ticket_prefix"] == "PORT"
    assert config["repository"] == str(project_repo)
    assert config["session_dir"] == f"/home/{missing_owner}/.local/state/porter-ticket-board/pane-sessions"
    assert not config["session_dir"].startswith("/run/")
    assert config["worktree_remote"] == "upstream"
    assert config["worktree_branch"] == "trunk"
    assert config["control_repository"] == f"/home/{missing_owner}/.local/state/switchyard/projects/porter/control.git"
    assert config["worktree_base"] == f"/home/{missing_owner}/porter-worktrees"
    assert "Environment=TICKET_BOARD_PROJECT=porter" in board_unit
    assert "Environment=TICKET_BOARD_TICKET_PREFIX=PORT" in board_unit
    assert not any(call[:1] == ["sudo"] for call in runner.calls)

def test_design_owner_user_argument_is_verbatim_not_suffixed() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()

        assert (
            design_project_command(
                "porter",
                output_dir=tmp_path / "design",
                design_title="Porter",
                design_body="A small coordination tool.",
                repository=project_repo,
                remote="origin",
                default_branch="main",
                worktree_policy="shared",
                owner_user="eric",
                ticket_prefix="PORT",
                push_policy="director-main-only",
                audit_signoff=True,
                needs_inspection=False,
                needs_user_signoff=False,
                board_service_traversal=True,
                supplementary_groups=[],
                linger=True,
                owner_shell="fish",
                input_func=_no_input,
            )
            == 0
        )

        artifact = load_project_design_artifact(tmp_path / "design" / "porter.project.json", expected_project="porter")

    assert artifact.owner_user == "eric"

def test_design_cli_writes_project_artifact_noninteractively() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.main(
                    [
                        "porter",
                        "design",
                        "--design-output-dir",
                        str(tmp_path / "design"),
                        "--design-title",
                        "Porter",
                        "--design-body",
                        "CLI design body.",
                        "--repository",
                        str(project_repo),
                        "--owner-user",
                        "pgu647-missing-agent",
                        "--ticket-prefix",
                        "PORT",
                        "--remote",
                        "origin",
                        "--default-branch",
                        "main",
                        "--worktree-policy",
                        "shared",
                        "--push-policy",
                        "director-main-only",
                        "--audit-signoff",
                        "--no-needs-inspection",
                        "--no-needs-user-signoff",
                        "--board-service-traversal",
                        "--supplementary-group",
                        "kvm",
                        "--linger",
                        "--owner-shell",
                        "fish",
                    ]
                )
                == 0
            )

        artifact = load_project_design_artifact(tmp_path / "design" / "porter.project.json", expected_project="porter")

    assert "team-launcher: wrote project artifact" in stdout.getvalue()
    assert artifact.ticket_prefix == "PORT"
    assert artifact.owner_user == "pgu647-missing-agent"

def test_switchyard_menu_lists_new_first_then_projects() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-menu.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        (tmp_path / "porter.json").write_text(
            json.dumps(
                {
                    "project": "porter",
                    "project_name": "Porter System",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert switchyard_menu_command(config_dir=tmp_path, registry_dir=registry_dir, print_func=lines.append) == 0

    assert lines == ["new...", "Porter System (porter)"]

def test_switchyard_help_lists_verbs_and_bare_project_start() -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        assert switchyard_main(["--help"]) == 0

    output = stdout.getvalue()
    for verb in ("new", "register", "upgrade", "add-role", "set-vcs-close-role", "stop", "status", "validate-models"):
        assert verb in output
    assert "switchyard <project name or slug>" in output
    assert "Bare project names start or attach the project" in output

def test_switchyard_version_prints_without_project_resolution() -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        assert switchyard_main(["--version"]) == 0

    assert stdout.getvalue().strip() == f"switchyard {team_launcher.SWITCHYARD_VERSION}"

def test_switchyard_status_lists_registered_projects_from_process_snapshot() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-status.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        pgu_config = tmp_path / "pgu.json"
        pgu_config.write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "project_name": "PGU",
                    "layout": str(layout),
                    "repository": str(tmp_path / "pgu-repo"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "pgu-director:0.0", "tmux_session": "pgu-director", "cli": ["claude"]},
                        {"role": "ops", "slot": 1, "target": "pgu-ops:0.0", "tmux_session": "pgu-ops", "cli": ["codex"]},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        otto_config = tmp_path / "otto.json"
        otto_config.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto Scheduler",
                    "layout": str(layout),
                    "repository": str(tmp_path / "otto-repo"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "otto-director:0.0", "tmux_session": "otto-director", "cli": ["claude"]},
                        {"role": "main", "slot": 1, "target": "otto-main:0.0", "tmux_session": "otto-main", "cli": ["codex"]},
                        {"role": "audit", "slot": 2, "target": "otto-audit:0.0", "tmux_session": "otto-audit", "cli": ["claude"]},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        stopped_config = tmp_path / "stopped.json"
        stopped_config.write_text(
            json.dumps(
                {
                    "project": "stopped",
                    "project_name": "Something Else",
                    "layout": str(layout),
                    "repository": str(tmp_path / "stopped-repo"),
                    "roles": [
                        {"role": "director", "slot": 0, "target": "stopped-director:0.0", "tmux_session": "stopped-director", "cli": ["claude"]},
                        {"role": "app", "slot": 1, "target": "stopped-app:0.0", "tmux_session": "stopped-app", "cli": ["codex"]},
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        for slug, name, config_path in (
            ("pgu", "PGU", pgu_config),
            ("otto", "Otto Scheduler", otto_config),
            ("stopped", "Something Else", stopped_config),
        ):
            (registry_dir / f"{slug}.json").write_text(
                json.dumps(
                    {
                        "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                        "slug": slug,
                        "name": name,
                        "config_path": str(config_path),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        lines: list[str] = []

        assert (
            switchyard_status_command(
                config_dir=config_dir,
                registry_dir=registry_dir,
                process_commands=[
                    "fish -c env TICKET_BOARD_PANE_TARGET=pgu-director:0.0 claude",
                    "fish -c env TICKET_BOARD_PANE_TARGET=pgu-ops:0.0 codex",
                    "fish -c env TICKET_BOARD_PANE_TARGET=otto-main:0.0 codex",
                    "python3 /service/ticket-board.py --project otto",
                ],
                source_repo=tmp_path / "missing-source",
                switchyard_install_path=tmp_path / "missing-switchyard",
                print_func=lines.append,
            )
            == 0
        )

    assert lines == [
        "NAME            SLUG     STATE    PANES  VIEWER",
        "Otto Scheduler  otto     running  1/3    otto-viewer",
        "PGU             pgu      running  2/2    pgu-viewer",
        "Something Else  stopped  stopped  0/2    stopped-viewer",
    ]

def test_switchyard_status_json_reports_same_project_facts() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-status-json.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "atlas.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "atlas",
                    "project_name": "Atlas",
                    "layout": str(layout),
                    "repository": str(tmp_path / "atlas-repo"),
                    "roles": [{"role": "director", "slot": 0, "target": "atlas-director:0.0", "tmux_session": "atlas-director", "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "atlas.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "atlas",
                    "name": "Atlas",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert (
            switchyard_status_command(
                config_dir=config_dir,
                registry_dir=registry_dir,
                json_output=True,
                process_commands=["fish -c env TICKET_BOARD_PANE_TARGET=atlas-director:0.0 claude"],
                source_repo=tmp_path / "missing-source",
                switchyard_install_path=tmp_path / "missing-switchyard",
                print_func=lines.append,
            )
            == 0
        )
        payload = json.loads("\n".join(lines))

    assert payload == {
        "projects": [
            {
                "name": "Atlas",
                "slug": "atlas",
                "state": "running",
                "panes_up": 1,
                "panes_total": 1,
                "panes": "1/1",
                "viewer_session": "atlas-viewer",
                "config_path": str(config_path),
                "error": "",
            }
        ],
        "runtime_copies": [],
    }

def test_switchyard_status_lists_unreadable_config_as_unknown() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-status-unknown.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        bad_config_path = tmp_path / "private-config"
        bad_config_path.mkdir()
        (registry_dir / "private.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "private",
                    "name": "Private Tenant",
                    "config_path": str(bad_config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert (
            switchyard_status_command(
                config_dir=config_dir,
                registry_dir=registry_dir,
                process_commands=["fish -c env TICKET_BOARD_PANE_TARGET=private-director:0.0 claude"],
                source_repo=tmp_path / "missing-source",
                switchyard_install_path=tmp_path / "missing-switchyard",
                print_func=lines.append,
            )
            == 0
        )

    assert lines == [
        "NAME            SLUG     STATE    PANES  VIEWER",
        "Private Tenant  private  unknown  ?/?    -",
    ]

def test_switchyard_status_ignores_tmux_server_new_session_argv_with_pane_target() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-status-tmux-server.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "atlas.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "atlas",
                    "project_name": "Atlas",
                    "layout": str(layout),
                    "repository": str(tmp_path / "atlas-repo"),
                    "roles": [{"role": "research", "slot": 0, "target": "atlas-research:0.0", "tmux_session": "atlas-research", "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "atlas.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "atlas",
                    "name": "Atlas",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert (
            switchyard_status_command(
                config_dir=config_dir,
                registry_dir=registry_dir,
                process_commands=[
                    "tmux new-session -d -s atlas-research -c /repo env TICKET_BOARD_PANE_TARGET=atlas-research:0.0 claude",
                    "tmux: server (/tmp/tmux-1001/default) for atlas",
                ],
                source_repo=tmp_path / "missing-source",
                switchyard_install_path=tmp_path / "missing-switchyard",
                print_func=lines.append,
            )
            == 0
        )

    assert lines == [
        "NAME   SLUG   STATE    PANES  VIEWER",
        "Atlas  atlas  stopped  0/1    atlas-viewer",
    ]

def test_switchyard_status_default_probe_uses_ps_without_root_or_tmux() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-status-probe.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "atlas.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "atlas",
                    "project_name": "Atlas",
                    "layout": str(layout),
                    "repository": str(tmp_path / "atlas-repo"),
                    "roles": [{"role": "director", "slot": 0, "target": "atlas-director:0.0", "tmux_session": "atlas-director", "cli": ["claude"]}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "atlas.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "atlas",
                    "name": "Atlas",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(list(args))
            assert args[0] == "ps", args
            stdout = "fish -c env TICKET_BOARD_PANE_TARGET=atlas-director:0.0 claude\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        lines: list[str] = []
        assert (
            switchyard_status_command(
                config_dir=config_dir,
                registry_dir=registry_dir,
                runner=runner,
                source_repo=tmp_path / "missing-source",
                switchyard_install_path=tmp_path / "missing-switchyard",
                print_func=lines.append,
            )
            == 0
        )

    assert calls == [["ps", "-eo", "args=", "--no-headers"]]
    assert lines[-1].strip().endswith("running  1/1    atlas-viewer")

def test_switchyard_status_reports_runtime_copy_staleness_without_fetching() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-status-stale.") as tmp:
        tmp_path = Path(tmp)
        origin, source_repo = _make_origin_backed_repo(tmp_path)
        old_sha = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()
        project_repo = tmp_path / "project-repo"
        role_worktree = tmp_path / "ops-worktree"
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(project_repo)])
        _run_git(["git", "clone", str(origin), str(role_worktree)])
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("remote update\n", encoding="utf-8")
        _commit_all(updater, "remote update")
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"},
        )
        target_sha = _run_git(["git", "rev-parse", "HEAD"], cwd=updater).stdout.strip()
        board_root = tmp_path / "otto-ticketboard-live"
        release = board_root / "releases" / old_sha
        release.mkdir(parents=True)
        (release / ".pgu-deploy-sha").write_text(old_sha + "\n", encoding="utf-8")
        (board_root / "current").symlink_to(release)
        installed_wrapper = tmp_path / "bin" / "switchyard"
        installed_wrapper.parent.mkdir()
        installed_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f"readonly SWITCHYARD_TARGET={shlex.quote(str(source_repo / 'switchyard'))}\n"
            "switchyard_invocation_requires_root() { return 1; }\n",
            encoding="utf-8",
        )
        installed_wrapper.chmod(0o755)
        (source_repo / "switchyard").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (source_repo / "switchyard").chmod(0o755)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "otto.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "otto",
                    "project_name": "Otto Scheduler",
                    "layout": str(layout),
                    "repository": str(project_repo),
                    "worktree_remote": "origin",
                    "worktree_branch": "main",
                    "pane_launcher": str(board_root / "current" / "scripts" / "team-launcher"),
                    "roles": [
                        {
                            "role": "ops",
                            "slot": 0,
                            "target": "otto-ops:0.0",
                            "tmux_session": "otto-ops",
                            "workdir": str(role_worktree),
                            "cli": ["codex"],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        lines: list[str] = []

        assert (
            switchyard_status_command(
                config_dir=config_dir,
                registry_dir=registry_dir,
                process_commands=[],
                source_repo=source_repo,
                switchyard_install_path=installed_wrapper,
                print_func=lines.append,
            )
            == 0
        )
        output = "\n".join(lines)
        source_head_after = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()

    assert "RUNTIME COPIES" in output
    assert "switchyard" in output and "installed wrapper" in output and str(installed_wrapper) in output
    assert "stale: embedded verb classification" in output
    assert "otto" in output and "invoking checkout" in output and str(source_repo) in output
    assert "otto" in output and "project checkout" in output and str(project_repo) in output
    assert "otto" in output and "ops worktree" in output and str(role_worktree) in output
    assert "behind at least 1 commit(s)" in output
    assert "otto" in output and "tenant release" in output and str(board_root) in output
    assert f"stale: {old_sha} -> {target_sha}" in output
    assert source_head_after == old_sha

def test_switchyard_upgrade_warns_when_artifact_source_checkout_is_stale() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-stale-source.") as tmp:
        tmp_path = Path(tmp)
        origin, source_repo = _make_origin_backed_repo(tmp_path)
        old_sha = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()
        updater = tmp_path / "updater"
        _run_git(["git", "clone", str(origin), str(updater)])
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=updater)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=updater)
        (updater / "tracked.txt").write_text("remote update\n", encoding="utf-8")
        _commit_all(updater, "remote update")
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=updater,
            check=True,
            text=True,
            capture_output=True,
            env={**os.environ, "PGU_ALLOW_MAIN_PUSH": "director"},
        )
        project_dir = tmp_path / "project"
        provision_dir = project_dir / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)
        lines: list[str] = []

        assert (
            team_launcher.upgrade_project_command(
                config,
                config_path=config_path,
                source_repo=source_repo,
                print_func=lines.append,
            )
            == 0
        )

        output = "\n".join(lines)
        source_head_after = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()

    assert source_head_after == old_sha
    assert f"warning: switchyard: artifact source checkout {source_repo} is at least 1 commit(s) behind origin/main" in output
    assert "generated files will reflect this checkout, not the remote tip" in output

def test_switchyard_known_project_plus_unknown_word_suggests_bare_project() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-project-command-hint.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        registry_dir.mkdir()
        config_path = tmp_path / "mefp.json"
        config_path.write_text("{}\n", encoding="utf-8")
        (registry_dir / "mefp.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "mefp",
                    "name": "MEFP",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            team_launcher._resolve_switchyard_project(
                "mefp attach",
                config_dir=config_dir,
                registry_dir=registry_dir,
            )
            raise AssertionError("expected helpful unknown command failure")
        except SystemExit as exc:
            message = str(exc)

    assert message == (
        "switchyard: 'mefp' is a project; did you mean `switchyard mefp`? "
        "A bare project name starts or attaches it."
    )

def test_switchyard_resolves_legacy_dashed_name_selector_without_allowing_dashed_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-selector.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        registry_dir.mkdir()
        config_path = tmp_path / "otto.json"
        config_path.write_text("{}\n", encoding="utf-8")
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        entry = team_launcher._resolve_switchyard_project(
            "otto-scheduler",
            config_dir=config_dir,
            registry_dir=registry_dir,
        )

    assert entry.slug == "otto"
    assert entry.config_path == config_path

def test_switchyard_name_fallback_ignores_entries_whose_name_derives_no_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-selector.") as tmp:
        tmp_path = Path(tmp)
        registry_dir = tmp_path / "registry"
        config_dir = tmp_path / "configs"
        registry_dir.mkdir()
        config_dir.mkdir()
        atlas_config = tmp_path / "atlas.json"
        nihon_config = tmp_path / "nihon.json"
        atlas_config.write_text("{}\n", encoding="utf-8")
        nihon_config.write_text("{}\n", encoding="utf-8")
        (registry_dir / "atlas.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "atlas",
                    "name": "Atlas",
                    "config_path": str(atlas_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "nihon.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "nihon",
                    "name": "日本",
                    "config_path": str(nihon_config),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        assert team_launcher._resolve_switchyard_project("atlas", config_dir=config_dir, registry_dir=registry_dir).slug == "atlas"
        assert team_launcher._resolve_switchyard_project("Atlas", config_dir=config_dir, registry_dir=registry_dir).slug == "atlas"
        assert team_launcher._resolve_switchyard_project("nihon", config_dir=config_dir, registry_dir=registry_dir).slug == "nihon"
        try:
            team_launcher._resolve_switchyard_project("anything-else", config_dir=config_dir, registry_dir=registry_dir)
            raise AssertionError("expected unknown project")
        except SystemExit as exc:
            message = str(exc)

    assert message == "switchyard: unknown project 'anything-else'"

def test_project_config_slug_validation_rejects_dash_and_normalizes_uppercase() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-config-slug.") as tmp:
        tmp_path = Path(tmp)
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        dashed = tmp_path / "dashed.json"
        dashed.write_text(
            json.dumps(
                {
                    "project": "bad-project",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path)}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        uppercase = tmp_path / "uppercase.json"
        uppercase.write_text(
            json.dumps(
                {
                    "project": "OTTO",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path)}],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            load_project_config("bad_project", dashed)
            raise AssertionError("expected dashed config project rejection")
        except SystemExit as exc:
            message = str(exc)
        config = load_project_config("otto", uppercase)

    assert "project slug must match ^[a-z0-9][a-z0-9_]{0,39}$" in message
    assert config.project == "otto"

def test_project_config_default_path_normalizes_requested_slug() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-config-default.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        layout = config_dir / "pgu-konsole-layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        (config_dir / "pgu.json").write_text(
            json.dumps(
                {
                    "project": "pgu",
                    "layout": str(layout),
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(tmp_path)}],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        original_config_dir = team_launcher.DEFAULT_CONFIG_DIR
        try:
            team_launcher.DEFAULT_CONFIG_DIR = config_dir
            upper = load_project_config("PGU")
            padded = load_project_config(" pgu ")
        finally:
            team_launcher.DEFAULT_CONFIG_DIR = original_config_dir

    assert upper.project == "pgu"
    assert padded.project == "pgu"

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_design_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
