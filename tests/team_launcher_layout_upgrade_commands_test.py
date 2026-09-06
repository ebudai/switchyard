#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def _layout_titles(node: object) -> list[str]:
    if isinstance(node, dict) and isinstance(node.get("Widgets"), list):
        titles: list[str] = []
        for child in node["Widgets"]:
            titles.extend(_layout_titles(child))
        return titles
    if isinstance(node, dict):
        return [str(node.get("Title") or "")]
    return []


def test_generated_project_hook_install_args_match_otto_one_time_repair_command() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)
        team_launcher.current_user_name = lambda: "root"
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-hook-args.") as tmp:
            provision_dir = Path(tmp) / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = "otto-agent"
            raw_config["pane_launcher"] = "/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher"
            raw_config["session_dir"] = "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)

            args = team_launcher.install_generated_project_pane_hooks_args(
                config,
                script_path=ROOT / "scripts" / "team-launcher",
                pane_state_dir=Path("/run/user/1005/otto-ticket-board/pane-state"),
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name

    assert args == [
        "sudo",
        "-u",
        "otto-agent",
        "-H",
        "env",
        "XDG_RUNTIME_DIR=/run/user/1005",
        "TICKET_BOARD_PROJECT=otto",
        "TICKET_BOARD_PANE_STATE_DIR=/run/user/1005/otto-ticket-board/pane-state",
        "TICKET_BOARD_PANE_SESSION_DIR=/home/otto-agent/.local/state/otto-ticket-board/pane-sessions",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-install-pane-hooks",
        "install",
        "--home",
        "/home/otto-agent",
        "--hook-source",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-pane-idle-hook",
        "--bin-path",
        "/home/otto-agent/.local/bin/ticket-board-pane-idle-hook",
    ]

def test_upgrade_leaves_hand_maintained_pgu_layout_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        root = Path(tmp)
        config_dir = root / "config" / "team-launcher"
        config_dir.mkdir(parents=True)
        layout_path = config_dir / "pgu-konsole-layout.json"
        original_layout = json.loads((ROOT / "config" / "team-launcher" / "pgu-konsole-layout.json").read_text(encoding="utf-8"))
        layout_path.write_text(json.dumps(original_layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = _write_launcher_config(config_dir, project="pgu", layout="pgu-konsole-layout.json")
        config = load_project_config("pgu", config_path)

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert not result.changed
        assert "hand-maintained" in result.message
        assert json.loads(layout_path.read_text(encoding="utf-8")) == original_layout

def test_upgrade_leaves_real_pgu_layout_hash_and_plan_unchanged() -> None:
    config_path = ROOT / "config" / "team-launcher" / "pgu.json"
    config = load_project_config("pgu", config_path)
    before_hash = hashlib.md5(config.layout.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        layout_output = Path(tmp) / "pgu-launch-layout.json"
        before_stdout = StringIO()
        with redirect_stdout(before_stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=FakeRunner(),
                    dry_run=True,
                    layout_output=layout_output,
                )
                == 0
            )

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        after_stdout = StringIO()
        with redirect_stdout(after_stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=FakeRunner(),
                    dry_run=True,
                    layout_output=layout_output,
                )
                == 0
            )

    assert not result.changed
    assert hashlib.md5(config.layout.read_bytes()).hexdigest() == before_hash
    assert after_stdout.getvalue() == before_stdout.getvalue()

def test_upgrade_leaves_customized_provision_layout_unchanged() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        custom_layout = {
            "Orientation": "Vertical",
            "Widgets": [
                {"Command": "", "SessionRestoreId": index, "WorkingDirectory": ""}
                for index in range(6)
            ],
        }
        layout_path.write_text(json.dumps(custom_layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert not result.changed
        assert "differs from the known generated legacy shapes" in result.message
        assert json.loads(layout_path.read_text(encoding="utf-8")) == custom_layout

def test_launch_auto_upgrades_column_major_layout_before_materializing() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)
        launch_layout = Path(tmp) / "launch-layout.json"
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=launch_layout,
                layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                no_launcher_self_deploy=True,
                allow_stale_launcher=True,
                pane_state_dir=Path(tmp) / "pane-state",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        assert len(process_launcher.calls) == 1
        assert process_launcher.calls[0]["args"] == konsole_launch_args(launch_layout, window_title="otto")
        assert process_launcher.calls[0]["kwargs"]["start_new_session"] is True
        assert not any(call == konsole_launch_args(launch_layout, window_title="otto") for call in runner.calls)
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)
        materialized = json.loads(launch_layout.read_text(encoding="utf-8"))
        assert _layout_session_tree(materialized) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
            ],
        }
        assert _layout_titles(materialized) == ["otto"] * 6

def test_konsole_window_title_uses_project_name_for_every_layout_leaf() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-konsole-title.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["project_name"] = "Otto Scheduler"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)
        launch_layout = Path(tmp) / "launch-layout.json"
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=launch_layout,
                layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                no_launcher_self_deploy=True,
                allow_stale_launcher=True,
                pane_state_dir=Path(tmp) / "pane-state",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        materialized = json.loads(launch_layout.read_text(encoding="utf-8"))

    assert process_launcher.calls[0]["args"] == konsole_launch_args(
        launch_layout,
        window_title="Otto Scheduler",
    )
    assert _layout_titles(materialized) == ["Otto Scheduler"] * 6

def test_two_konsole_projects_get_distinct_window_titles() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-konsole-two-title.") as tmp:
        tmp_path = Path(tmp)
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

        for project, project_name in (("porter", "Porter System"), ("otto", "Otto Scheduler")):
            provision_dir = tmp_path / project / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / f"{project}-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir, project=project, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["project_name"] = project_name
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config(project, config_path)

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / f"{project}-launch-layout.json",
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                    no_launcher_self_deploy=True,
                    allow_stale_launcher=True,
                    pane_state_dir=tmp_path / project / "pane-state",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

    assert [call["args"] for call in process_launcher.calls] == [
        konsole_launch_args(tmp_path / "porter-launch-layout.json", window_title="Porter System"),
        konsole_launch_args(tmp_path / "otto-launch-layout.json", window_title="Otto Scheduler"),
    ]

def test_gnome_auto_layout_starts_with_tmux_viewer_without_konsole() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-gnome-viewer.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()
        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=Path(tmp) / "launch-layout.json",
                layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                no_launcher_self_deploy=True,
                allow_stale_launcher=True,
                pane_state_dir=pane_state_dir,
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        assert {path.name for path in pane_state_dir.iterdir()} == {
            pane_state_file_name(f"otto-role{index}:0.0") for index in range(6)
        }

    assert process_launcher.calls == []
    assert any(
        call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] == "otto-viewer"
        for call in runner.calls
    )
    assert not any("konsole" in " ".join(call) for call in runner.calls)

def test_materialize_layout_places_six_roles_row_major() -> None:
    roles = ["designer", "director", "audit", "ops", "app", "main"]
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-row-major.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(len(roles)), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, roles=roles)
        config = load_project_config("otto", config_path)
        output_path = Path(tmp) / "materialized-layout.json"

        materialize_layout(
            config,
            config_path=config_path,
            mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher",
            output_path=output_path,
        )

        assert _layout_command_role_tree(json.loads(output_path.read_text(encoding="utf-8"))) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": ["designer", "director", "audit"]},
                {"Orientation": "Horizontal", "Widgets": ["ops", "app", "main"]},
            ],
        }

def test_launch_dry_run_does_not_upgrade_legacy_generated_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        legacy_layout = FROZEN_STACKED_SIX_LAYOUT
        legacy_text = json.dumps(legacy_layout, indent=2, sort_keys=True) + "\n"
        layout_path.write_text(legacy_text, encoding="utf-8")
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)
        launch_layout = Path(tmp) / "launch-layout.json"
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=FakeRunner(),
                    dry_run=True,
                    layout_output=launch_layout,
                )
                == 0
            )

        assert layout_path.read_text(encoding="utf-8") == legacy_text
        assert _layout_session_tree(json.loads(launch_layout.read_text(encoding="utf-8"))) == {
            "Orientation": "Horizontal",
            "Widgets": [
                0,
                {"Orientation": "Vertical", "Widgets": [1, 2, 3, 4, 5]},
            ],
        }
        assert "upgraded generated layout template" not in stdout.getvalue()

def test_switchyard_upgrade_command_updates_registered_project_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade.") as tmp:
        root = Path(tmp)
        provision_dir = root / "otto" / ".switchyard" / "provision"
        registry_dir = root / "registry"
        provision_dir.mkdir(parents=True)
        registry_dir.mkdir()
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        (registry_dir / "otto.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "otto",
                    "name": "Otto Scheduler",
                    "config_path": str(config_path),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        previous_registry = team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR
        team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = registry_dir
        stdout = StringIO()
        try:
            with redirect_stdout(stdout):
                assert switchyard_main(["upgrade", "otto"]) == 0
        finally:
            team_launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR = previous_registry

        assert "upgraded generated layout template" in stdout.getvalue()
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)

def test_switchyard_upgrade_reports_tenant_release_update_command() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-release.") as tmp:
        root = Path(tmp)
        _origin, source_repo = _make_origin_backed_repo(root)
        target_sha = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()
        old_sha = "1111111111111111111111111111111111111111"
        board_root = root / "otto-ticketboard-live"
        old_release = board_root / "releases" / old_sha
        old_release.mkdir(parents=True)
        (old_release / ".pgu-deploy-sha").write_text(old_sha + "\n", encoding="utf-8")
        (board_root / "current").symlink_to(old_release)
        provision_dir = root / "otto" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        team_launcher.write_artifacts(
            team_launcher.build_plan(
                project="otto",
                owner_user=team_launcher.current_user_name(),
                owner_home=root,
                board_root=board_root,
                source_repo=root / "obsolete-source",
                commit_git_dir="/srv/git/obsolete-cache.git",
            ),
            provision_dir,
            enable_owner_linger=False,
        )
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(
            provision_dir,
            pane_launcher=board_root / "current" / "scripts" / "team-launcher",
        )
        config = load_project_config("otto", config_path)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.upgrade_project_command(
                    config,
                    config_path=config_path,
                    source_repo=source_repo,
                    commit_git_dir="/srv/git/review-cache.git",
                    deploy_ref="origin/main",
                )
                == 0
            )

        rendered = stdout.getvalue()
        updated_plan = json.loads((provision_dir / "plan.json").read_text(encoding="utf-8"))
        updated_unit = (provision_dir / "otto-ticket-board.service").read_text(encoding="utf-8")
        updated_commands = (provision_dir / "operator-commands.sh").read_text(encoding="utf-8")

    assert f"old: {old_sha} at {old_release}" in rendered
    assert f"new: {target_sha} from origin/main" in rendered
    assert "matching-release deployment sequence" in rendered
    assert "systemctl --user stop otto-ticket-board-notify-listener.service" in rendered
    assert "systemctl --user daemon-reload && systemctl --user start otto-ticket-board-notify-listener.service" in rendered
    assert f"HOME={root}" in rendered
    assert f"TICKET_BOARD_OWNER_HOME={root}" in rendered
    assert "TICKET_BOARD_PROJECT=otto" in rendered
    assert f"SOURCE_REPO={source_repo}" in rendered
    assert f"BOARD_ROOT={board_root}" in rendered
    assert "DEPLOY_REF=origin/main" in rendered
    assert f"TICKET_BOARD_PROVISIONED_SYSTEM_UNIT={provision_dir / 'otto-ticket-board.service'}" in rendered
    assert "TICKET_BOARD_COMMIT_GIT_DIR=/srv/git/review-cache.git" in rendered
    assert f"{source_repo}/scripts/ticket-board-service.sh deploy-restart" in rendered
    assert updated_plan["source_repo"] == str(source_repo)
    assert updated_plan["commit_git_dir"] == "/srv/git/review-cache.git"
    assert "Environment=TICKET_BOARD_COMMIT_GIT_DIR=/srv/git/review-cache.git" in updated_unit
    assert f"SOURCE_REPO='{source_repo}'" in updated_commands
    assert "TICKET_BOARD_COMMIT_GIT_DIR='/srv/git/review-cache.git'" in updated_commands
    assert "panes must be restarted after the release update" in rendered
    assert "deploy-restart" in rendered


def test_switchyard_upgrade_from_shared_release_prints_exact_cache_export_deploy_command() -> None:
    original_shared_root = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT")
    original_bare_repo = os.environ.get("SWITCHYARD_BARE_REPO")
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-shared-release.") as tmp:
            root = Path(tmp)
            origin, _source_repo = _make_origin_backed_repo(root)
            target_sha = _run_git(["git", "--git-dir", str(origin), "rev-parse", "refs/heads/main"]).stdout.strip()
            cache = root / "cache.git"
            _run_git(["git", "init", "--bare", str(cache)])
            _run_git(["git", "--git-dir", str(cache), "remote", "add", "origin", str(origin)])
            _run_git([
                "git", "--git-dir", str(cache), "config", "remote.origin.fetch",
                "+refs/heads/*:refs/remotes/origin/*",
            ])
            _run_git(["git", "--git-dir", str(cache), "fetch", "origin"])
            shared_root = root / "opt" / "switchyard"
            release = shared_root / "releases" / target_sha
            release.mkdir(parents=True)
            (release / ".switchyard-release.json").write_text(
                json.dumps({"commit": target_sha}) + "\n",
                encoding="utf-8",
            )
            (shared_root / "current").symlink_to(release)
            os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(shared_root)
            os.environ["SWITCHYARD_BARE_REPO"] = str(cache)
            old_sha = "1111111111111111111111111111111111111111"
            board_root = root / "otto-ticketboard-live"
            old_release = board_root / "releases" / old_sha
            old_release.mkdir(parents=True)
            (old_release / ".pgu-deploy-sha").write_text(old_sha + "\n", encoding="utf-8")
            (board_root / "current").symlink_to(old_release)
            provision_dir = root / "otto" / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            team_launcher.write_artifacts(
                team_launcher.build_plan(
                    project="otto",
                    owner_user=team_launcher.current_user_name(),
                    owner_home=root,
                    board_root=board_root,
                    source_repo=release,
                ),
                provision_dir,
                enable_owner_linger=False,
            )
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(
                provision_dir,
                pane_launcher=shared_root / "current" / "scripts" / "team-launcher",
            )
            config = load_project_config("otto", config_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                assert (
                    team_launcher.upgrade_project_command(
                        config,
                        config_path=config_path,
                        source_repo=release,
                        deploy_ref="origin/main",
                    )
                    == 0
                )

            rendered = stdout.getvalue()

        assert f"artifact source is shared release {release} at {target_sha}" in rendered
        assert "cannot determine artifact source checkout freshness" not in rendered
        assert f"old: {old_sha} at {old_release}" in rendered
        assert f"new: {target_sha} from origin/main" in rendered
        assert "matching-release deployment sequence" in rendered
        assert f"TICKET_BOARD_OWNER_HOME={root}" in rendered
        assert f"git --git-dir={cache} archive {target_sha}" in rendered
        assert f'{{"commit":"{target_sha}"}}' in rendered
        assert 'SOURCE_REPO="$tmpdir"' in rendered
        assert f"BOARD_ROOT={board_root}" in rendered
        assert "DEPLOY_REF=origin/main" in rendered
        assert f"TICKET_BOARD_PROVISIONED_SYSTEM_UNIT={provision_dir / 'otto-ticket-board.service'}" in rendered
        assert '"$tmpdir/scripts/ticket-board-service.sh" deploy-restart' in rendered
        assert f"SOURCE_REPO={release}" not in rendered
        assert f"{release}/scripts/ticket-board-service.sh deploy-restart" not in rendered
        assert "panes must be restarted after the release update" in rendered
        assert "deploy-restart" in rendered
    finally:
        if original_shared_root is None:
            os.environ.pop("SWITCHYARD_SHARED_INSTALL_ROOT", None)
        else:
            os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = original_shared_root
        if original_bare_repo is None:
            os.environ.pop("SWITCHYARD_BARE_REPO", None)
        else:
            os.environ["SWITCHYARD_BARE_REPO"] = original_bare_repo


def test_bare_source_cache_resolves_remote_tracking_origin_main_before_stale_local_main() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-origin-main-cache.") as tmp:
        root = Path(tmp)
        origin, source_repo = _make_origin_backed_repo(root)
        stale_sha = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()
        (source_repo / "tracked.txt").write_text("current\n", encoding="utf-8")
        _commit_all(source_repo, "current")
        _run_git(["git", "push", "origin", "HEAD:main"], cwd=source_repo)
        current_sha = _run_git(["git", "rev-parse", "HEAD"], cwd=source_repo).stdout.strip()

        cache = root / "cache.git"
        _run_git(["git", "init", "--bare", str(cache)])
        _run_git(["git", "--git-dir", str(cache), "remote", "add", "origin", str(origin)])
        _run_git([
            "git", "--git-dir", str(cache), "config", "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ])
        _run_git(["git", "--git-dir", str(cache), "fetch", "origin"])
        _run_git(["git", "--git-dir", str(cache), "update-ref", "refs/heads/main", stale_sha])

        resolved, error = team_launcher._resolve_deploy_ref_from_bare_repo(cache, "origin/main")

    assert error == ""
    assert resolved == current_sha
    assert resolved != stale_sha


def test_shared_release_requires_explicit_source_cache_instead_of_data_default() -> None:
    original_shared_root = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT")
    original_bare_repo = os.environ.pop("SWITCHYARD_BARE_REPO", None)
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-switchyard-explicit-cache.") as tmp:
            root = Path(tmp)
            shared_root = root / "opt" / "switchyard"
            release = shared_root / "releases" / ("a" * 40)
            release.mkdir(parents=True)
            (release / ".switchyard-release.json").write_text(
                json.dumps({"commit": "a" * 40}) + "\n",
                encoding="utf-8",
            )
            (shared_root / "current").symlink_to(release)
            os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(shared_root)
            board_root = root / "otto-ticketboard-live"
            provision_dir = root / "otto" / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            team_launcher.write_artifacts(
                team_launcher.build_plan(
                    project="otto",
                    owner_user=team_launcher.current_user_name(),
                    owner_home=root,
                    board_root=board_root,
                    source_repo=release,
                ),
                provision_dir,
                enable_owner_linger=False,
            )
            config_path = _write_launcher_config(
                provision_dir,
                pane_launcher=shared_root / "current" / "scripts" / "team-launcher",
            )
            config = load_project_config("otto", config_path)
            stdout = StringIO()

            with redirect_stdout(stdout):
                team_launcher.report_tenant_release_upgrade(
                    config,
                    config_path=config_path,
                    source_repo=release,
                )
            rendered = stdout.getvalue()

        assert "require an explicit source cache in SWITCHYARD_BARE_REPO" in rendered
        assert "matching-release deployment sequence" not in rendered
        assert "/data/git/switchyard.git" not in rendered
    finally:
        if original_shared_root is None:
            os.environ.pop("SWITCHYARD_SHARED_INSTALL_ROOT", None)
        else:
            os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = original_shared_root
        if original_bare_repo is not None:
            os.environ["SWITCHYARD_BARE_REPO"] = original_bare_repo


def test_switchyard_upgrade_reports_unchanged_tenant_release() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-release.") as tmp:
        root = Path(tmp)
        _origin, source_repo = _make_origin_backed_repo(root)
        target_sha = _run_git(["git", "rev-parse", "origin/main"], cwd=source_repo).stdout.strip()
        board_root = root / "otto-ticketboard-live"
        release = board_root / "releases" / target_sha
        release.mkdir(parents=True)
        (release / ".pgu-deploy-sha").write_text(target_sha + "\n", encoding="utf-8")
        (board_root / "current").symlink_to(release)
        provision_dir = root / "otto" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(
            provision_dir,
            pane_launcher=board_root / "current" / "scripts" / "team-launcher",
        )
        config = load_project_config("otto", config_path)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.upgrade_project_command(
                    config,
                    config_path=config_path,
                    source_repo=source_repo,
                    deploy_ref="origin/main",
                )
                == 0
            )

        rendered = stdout.getvalue()

    assert f"old: {target_sha} at {release}" in rendered
    assert f"new: {target_sha} from origin/main" in rendered
    assert "deployed board release unchanged; no release deploy needed" in rendered
    assert "matching-release deployment sequence" not in rendered
    assert "panes must be restarted after the release update" not in rendered

def test_switchyard_upgrade_pgu_without_tenant_release_emits_no_release_report() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-upgrade-pgu.") as tmp:
        root = Path(tmp)
        _origin, source_repo = _make_origin_backed_repo(root)
        provision_dir = root / "pgu" / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "pgu-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, project="pgu")
        config = load_project_config("pgu", config_path)
        stdout = StringIO()

        with redirect_stdout(stdout):
            assert (
                team_launcher.upgrade_project_command(
                    config,
                    config_path=config_path,
                    source_repo=source_repo,
                    deploy_ref="origin/main",
                )
                == 0
            )

        rendered = stdout.getvalue()

    assert "deployed board release" not in rendered
    assert "matching-release deployment sequence" not in rendered
    assert "sudo env" not in rendered
    assert "panes must be restarted after the release update" not in rendered

def test_layout_detection_uses_invoking_user_and_falls_back_to_viewer_when_undetected() -> None:
    class DesktopRunner:
        def __init__(self, *, desktop_output: str = "") -> None:
            self.desktop_output = desktop_output
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["loginctl", "show-user", "eric"]:
                return subprocess.CompletedProcess(args, 0, stdout="7\n")
            if args[:3] == ["loginctl", "show-session", "7"]:
                return subprocess.CompletedProcess(args, 0, stdout=self.desktop_output)
            return subprocess.CompletedProcess(args, 1, stdout="")

    kde_runner = DesktopRunner(desktop_output="Desktop=KDE\n")
    assert (
        team_launcher.detected_invoking_desktop(
            environ={"SUDO_USER": "eric"},
            runner=kde_runner,
        )
        == "KDE"
    )
    assert kde_runner.calls[0] == ["loginctl", "show-user", "eric", "-p", "Display", "--value"]
    assert kde_runner.calls[1] == ["loginctl", "show-session", "7", "-p", "Desktop"]
    assert team_launcher.resolve_layout_mode("auto", environ={"SUDO_USER": "eric"}, runner=kde_runner) == "separate"

    unknown_runner = DesktopRunner(desktop_output="")
    assert (
        team_launcher.resolve_layout_mode(
            "auto",
            environ={"SUDO_USER": "eric", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
            runner=unknown_runner,
        )
        == "viewer"
    )
    type_only_runner = DesktopRunner(desktop_output="Type=wayland\n")
    assert (
        team_launcher.resolve_layout_mode(
            "auto",
            environ={"SUDO_USER": "eric", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
            runner=type_only_runner,
        )
        == "viewer"
    )
    for desktop in ("GNOME", "ubuntu:GNOME", "XFCE"):
        assert team_launcher.resolve_layout_mode(
            "auto",
            environ={"XDG_CURRENT_DESKTOP": desktop},
            runner=FakeRunner(),
        ) == "viewer"
    for desktop in ("KDE", "plasma", "KDE:plasma"):
        assert team_launcher.resolve_layout_mode(
            "auto",
            environ={"XDG_CURRENT_DESKTOP": desktop},
            runner=FakeRunner(),
        ) == "separate"
    assert team_launcher.resolve_layout_mode("viewer", environ={"XDG_CURRENT_DESKTOP": "KDE"}, runner=FakeRunner()) == "viewer"
    assert team_launcher.resolve_layout_mode("separate", environ={"XDG_CURRENT_DESKTOP": "GNOME"}, runner=FakeRunner()) == "separate"
    assert (
        team_launcher.resolve_layout_mode(
            "separate",
            environ={"SUDO_USER": "eric", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
            runner=DesktopRunner(desktop_output=""),
        )
        == "separate"
    )

def test_explicit_separate_layout_still_launches_konsole_when_desktop_is_undetected() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-explicit-separate.") as tmp:
        tmp_path = Path(tmp)
        layout_path = tmp_path / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(tmp_path)
        config = load_project_config("otto", config_path)
        layout_output = tmp_path / "layout.json"
        runner = FakeRunner()
        process_launcher = RecordingProcessLauncher()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                layout_mode=team_launcher.LAYOUT_MODE_SEPARATE,
                layout_environ={"SUDO_USER": "eric", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
                no_launcher_self_deploy=True,
                allow_stale_launcher=True,
                pane_state_dir=tmp_path / "pane-state",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

    assert len(process_launcher.calls) == 1
    assert process_launcher.calls[0]["args"] == konsole_launch_args(layout_output, window_title="otto")

def test_materialize_layout_refuses_more_than_six_visible_roles() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-visible-cap.") as tmp:
        tmp_path = Path(tmp)
        layout_path = tmp_path / "porter-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._new_project_layout_payload(7), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        roles = [f"role{index}" for index in range(7)]
        repo = tmp_path / "repo"
        repo.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "desktop_access": {"mode": "headless"},
                    "project": "porter",
                    "layout": str(layout_path),
                    "repository": str(repo),
                    "roles": [
                        {
                            "role": role,
                            "slot": index,
                            "cli": ["codex"],
                            "target": f"porter-{role}:0.0",
                            "tmux_session": f"porter-{role}",
                        }
                        for index, role in enumerate(roles)
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)

        try:
            materialize_layout(
                config,
                config_path=config_path,
                mode="attach-or-start",
                script_path=ROOT / "scripts" / "team-launcher",
                output_path=tmp_path / "materialized.json",
            )
            raise AssertionError("expected visible pane cap failure")
        except SystemExit as exc:
            message = str(exc)

    assert "has 7 visible roles" in message
    assert "at most 6 panes can be visible in one window" in message

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_layout_upgrade_commands_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
