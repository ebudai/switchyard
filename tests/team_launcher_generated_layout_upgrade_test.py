#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_upgrade_refreshes_legacy_generated_provision_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_STACKED_SIX_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir)
        config = load_project_config("otto", config_path)

        assert team_launcher._legacy_new_project_stacked_layout_payload(6) == FROZEN_STACKED_SIX_LAYOUT
        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)
        second_result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)
        assert not second_result.changed
        assert "already current" in second_result.message

def test_upgrade_refreshes_column_major_generated_provision_layout() -> None:
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

        assert team_launcher._legacy_new_project_column_major_layout_payload(6) == FROZEN_COLUMN_MAJOR_SIX_LAYOUT
        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert json.loads(layout_path.read_text(encoding="utf-8")) == team_launcher._new_project_layout_payload(6)

def test_four_role_legacy_builders_keep_historical_single_row_cutoff() -> None:
    assert team_launcher.NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES == 3
    assert (
        team_launcher._legacy_new_project_column_major_layout_payload(4)
        == FROZEN_COLUMN_MAJOR_FOUR_LAYOUT
    )
    assert (
        team_launcher._legacy_new_project_chunked_row_major_layout_payload(4)
        == FROZEN_COLUMN_MAJOR_FOUR_LAYOUT
    )

def test_upgrade_refreshes_old_four_role_single_row_generated_layout_to_two_by_two() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_FOUR_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, role_count=4)
        config = load_project_config("otto", config_path)

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert "upgraded generated layout template" in result.message
        assert _layout_session_tree(json.loads(layout_path.read_text(encoding="utf-8"))) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1]},
                {"Orientation": "Horizontal", "Widgets": [2, 3]},
            ],
        }

def test_upgrade_refreshes_chunked_seven_role_generated_provision_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        legacy_layout = team_launcher._legacy_new_project_chunked_row_major_layout_payload(7)
        layout_path.write_text(
            json.dumps(legacy_layout, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, role_count=7)
        config = load_project_config("otto", config_path)

        assert _layout_session_tree(legacy_layout) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
                6,
            ],
        }
        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert _layout_session_tree(json.loads(layout_path.read_text(encoding="utf-8"))) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4]},
                {"Orientation": "Horizontal", "Widgets": [5, 6]},
            ],
        }

def test_upgrade_generated_project_layout_counts_visible_roles_only() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-visible-count.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(team_launcher._legacy_new_project_stacked_layout_payload(6), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, role_count=6)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["roles"].append(
            {
                "cli": ["codex"],
                "detached": True,
                "live_commands": ["codex"],
                "role": "detached-worker",
                "target": "otto-detached-worker:0.0",
                "tmux_session": "otto-detached-worker",
                "workdir": str(provision_dir / "worktrees" / "detached-worker"),
                "yolo": True,
            }
        )
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)

        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)
        upgraded_layout = json.loads(layout_path.read_text(encoding="utf-8"))

        assert result.changed
        assert len(team_launcher._layout_leaves(upgraded_layout)) == 6
        assert len([role for role in config.roles if not role.detached]) == 6
        assert len(config.roles) == 7
        assert _layout_session_tree(upgraded_layout) == {
            "Orientation": "Vertical",
            "Widgets": [
                {"Orientation": "Horizontal", "Widgets": [0, 1, 2]},
                {"Orientation": "Horizontal", "Widgets": [3, 4, 5]},
            ],
        }

def test_upgrade_refreshes_old_three_role_column_major_generated_layout() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade.") as tmp:
        provision_dir = Path(tmp) / ".switchyard" / "provision"
        provision_dir.mkdir(parents=True)
        layout_path = provision_dir / "otto-konsole-layout.json"
        layout_path.write_text(
            json.dumps(FROZEN_COLUMN_MAJOR_THREE_LAYOUT, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config_path = _write_launcher_config(provision_dir, role_count=3)
        config = load_project_config("otto", config_path)

        assert (
            team_launcher._legacy_new_project_sqrt_column_major_layout_payload(3)
            == FROZEN_COLUMN_MAJOR_THREE_LAYOUT
        )
        result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path)

        assert result.changed
        assert _layout_session_tree(json.loads(layout_path.read_text(encoding="utf-8"))) == {
            "Orientation": "Horizontal",
            "Widgets": [0, 1, 2],
        }

def test_upgrade_repairs_generated_runtime_session_dir_to_durable_owner_state() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_geteuid = team_launcher.os.geteuid
    original_current_user_name = team_launcher.current_user_name
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        team_launcher.current_user_name = lambda: "root"
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade-session.") as tmp:
            provision_dir = Path(tmp) / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(6), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = "otto-agent"
            raw_config["session_dir"] = "/run/user/1005/otto-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)
            runner = FakeRunner()

            result = team_launcher.upgrade_generated_project_layout(config, config_path=config_path, runner=runner)
            upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]
        team_launcher.current_user_name = original_current_user_name

    assert result.changed
    assert "upgraded session dir" in result.message
    assert upgraded["session_dir"] == "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions"
    assert team_launcher.chown_owner_file_args(config, config_path) in runner.calls

def test_launch_auto_upgrades_generated_session_dir_before_starting_panes() -> None:
    current_user = team_launcher.current_user_name()
    original_uid_for_user = team_launcher.uid_for_user
    try:
        team_launcher.uid_for_user = lambda user_name: os.getuid() if user_name == current_user else original_uid_for_user(user_name)
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-upgrade-session.") as tmp:
            provision_dir = Path(tmp) / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path = _write_launcher_config(provision_dir, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = current_user
            raw_config["session_dir"] = f"/run/user/{os.getuid()}/otto-ticket-board/pane-sessions"
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)
            runner = FakeRunner()
            stdout = StringIO()

            with redirect_stdout(stdout):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=runner,
                        layout_output=Path(tmp) / "launch-layout.json",
                        layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                        no_launcher_self_deploy=True,
                        allow_stale_launcher=True,
                    )
                    == 0
                )
            upgraded = json.loads(config_path.read_text(encoding="utf-8"))
    finally:
        team_launcher.uid_for_user = original_uid_for_user

    expected_session_dir = f"/home/{current_user}/.local/state/otto-ticket-board/pane-sessions"
    assert upgraded["session_dir"] == expected_session_dir
    assert "upgraded session dir" in stdout.getvalue()
    hook_install_calls = [
        call
        for call in runner.calls
        if "ticket-board-install-pane-hooks" in " ".join(str(part) for part in call)
    ]
    assert hook_install_calls
    assert hook_install_calls[0][:1] == ["env"]
    assert f"TICKET_BOARD_PROJECT=otto" in hook_install_calls[0]
    assert f"TICKET_BOARD_PANE_STATE_DIR=/run/user/{os.getuid()}/otto-ticket-board/pane-state" in hook_install_calls[0]
    assert f"TICKET_BOARD_PANE_SESSION_DIR={expected_session_dir}" in hook_install_calls[0]
    assert str(ROOT / "scripts" / "ticket-board-install-pane-hooks") in hook_install_calls[0]
    assert "install" in hook_install_calls[0]
    assert "--home" in hook_install_calls[0]
    assert f"/home/{current_user}" in hook_install_calls[0]
    tmux_new_calls = [call for call in runner.calls if call[:3] == ["tmux", "new-session", "-d"]]
    assert tmux_new_calls
    assert runner.calls.index(hook_install_calls[0]) < runner.calls.index(tmux_new_calls[0])
    assert f"TICKET_BOARD_PANE_SESSION_DIR={expected_session_dir}" in tmux_new_calls[0][-1]
    assert "TICKET_BOARD_PANE_SESSION_DIR=/run/" not in tmux_new_calls[0][-1]

def test_launch_repairs_generated_project_owner_hooks_before_starting_panes() -> None:
    class OwnerTmuxRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["sudo", "-u", "otto-agent"]:
                inner = args[4:] if len(args) > 3 and args[3] == "-H" else args[3:]
                if inner[:1] == ["tmux"]:
                    result = super().__call__(inner, **kwargs)
                    if self.calls and self.calls[-1] == inner:
                        self.calls.pop()
                    return subprocess.CompletedProcess(
                        args,
                        result.returncode,
                        stdout=getattr(result, "stdout", None),
                        stderr=getattr(result, "stderr", None),
                    )
                return subprocess.CompletedProcess(args, 0)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            return super().__call__(args, **kwargs)

    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_seed_initial_pane_idle_state = team_launcher.seed_initial_pane_idle_state
    try:
        team_launcher.uid_for_user = lambda user_name: 1005 if user_name == "otto-agent" else original_uid_for_user(user_name)
        team_launcher.current_user_name = lambda: "root"
        team_launcher.seed_initial_pane_idle_state = lambda *_args, **_kwargs: None
        with tempfile.TemporaryDirectory(prefix="pgu-launcher-hooks-repair.") as tmp:
            tmp_path = Path(tmp)
            provision_dir = tmp_path / "home" / "otto-agent" / "Projects" / "otto" / ".switchyard" / "provision"
            provision_dir.mkdir(parents=True)
            layout_path = provision_dir / "otto-konsole-layout.json"
            layout_path.write_text(
                json.dumps(team_launcher._new_project_layout_payload(1), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            pane_launcher = Path("/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher")
            config_path = _write_launcher_config(provision_dir, role_count=1)
            raw_config = json.loads(config_path.read_text(encoding="utf-8"))
            raw_config["run_as_user"] = "otto-agent"
            raw_config["pane_launcher"] = str(pane_launcher)
            raw_config["session_dir"] = str(tmp_path / "durable-sessions")
            config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("otto", config_path)
            runner = OwnerTmuxRunner()
            pane_state_dir = tmp_path / "pane-state"
            session_dir = raw_config["session_dir"]

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "launch-layout.json",
                    pane_state_dir=pane_state_dir,
                    layout_mode=team_launcher.LAYOUT_MODE_VIEWER,
                    no_launcher_self_deploy=True,
                    allow_stale_launcher=True,
                )
                == 0
            )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        team_launcher.seed_initial_pane_idle_state = original_seed_initial_pane_idle_state

    hook_install_call = next(
        call for call in runner.calls if "ticket-board-install-pane-hooks" in " ".join(str(part) for part in call)
    )
    pane_start_call = next(
        call
        for call in runner.calls
        if call[:5] == ["sudo", "-u", "otto-agent", "-H", "/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher"]
        and call[5:8] == ["otto", "pane", "attach-or-start"]
    )
    assert hook_install_call == [
        "sudo",
        "-u",
        "otto-agent",
        "-H",
        "env",
        "XDG_RUNTIME_DIR=/run/user/1005",
        "TICKET_BOARD_PROJECT=otto",
        f"TICKET_BOARD_PANE_STATE_DIR={pane_state_dir}",
        f"TICKET_BOARD_PANE_SESSION_DIR={session_dir}",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-install-pane-hooks",
        "install",
        "--home",
        "/home/otto-agent",
        "--hook-source",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-pane-idle-hook",
        "--bin-path",
        "/home/otto-agent/.local/bin/ticket-board-pane-idle-hook",
    ]
    assert pane_start_call == [
        "sudo",
        "-u",
        "otto-agent",
        "-H",
        "/home/otto-agent/otto-ticketboard-live/current/scripts/team-launcher",
        "otto",
        "pane",
        "attach-or-start",
        "role0",
        "--config",
        str(config_path),
        "--skip-launcher-check",
        "--allow-stale-launcher",
        "--no-attach",
        "--pane-state-dir",
        str(pane_state_dir),
    ]
    assert runner.calls.index(hook_install_call) < runner.calls.index(pane_start_call)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_generated_layout_upgrade_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
