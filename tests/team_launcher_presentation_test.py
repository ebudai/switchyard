#!/usr/bin/env python3
"""Regression coverage for stable runtime presentation slots."""

from __future__ import annotations

from team_launcher_test_helpers import *

from scripts import presentation_controller as presentation


def _write_presentation_config(root: Path, *, project: str = "porter") -> Path:
    layout = root / "layout.json"
    layout.write_text(
        json.dumps(team_launcher._new_project_layout_payload(2), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    session_dir = root / "sessions"
    roles = []
    for slot, role in enumerate(("director", "app")):
        roles.append(
            {
                "role": role,
                "slot": slot,
                "tmux_session": f"{project}-{role}",
                "target": f"{project}-{role}:0.0",
                "workdir": str(root / role),
                "cli": ["codex"],
                "live_commands": ["codex"],
            }
        )
        (root / role).mkdir()
    path = root / f"{project}.json"
    path.write_text(
        json.dumps(
            {
                "project": project,
                "layout": str(layout),
                "session_dir": str(session_dir),
                "presentation": {
                    "slot_count": 2,
                    "layouts": {"default": {"0": "director", "1": "app"}},
                },
                "roles": roles,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


class PresentationRunner:
    def __init__(self, project: str = "porter") -> None:
        self.sessions = {f"{project}-director", f"{project}-app"}
        self.proxy_roles: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.fail_next_respawn_target = ""
        self.fail_next_select_target = ""
        self.worker_pids = {f"{project}-director": 4101, f"{project}-app": 4102}

    @staticmethod
    def _session(target: str) -> str:
        return target.split(":", 1)[0]

    def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        assert args[0] == "tmux"
        command = args[1]
        target = args[args.index("-t") + 1] if "-t" in args else ""
        session = self._session(target)
        if command == "has-session":
            return subprocess.CompletedProcess(args, 0 if session in self.sessions else 1)
        if command == "new-session":
            self.sessions.add(args[args.index("-s") + 1])
            return subprocess.CompletedProcess(args, 0)
        if command == "kill-session":
            self.sessions.discard(session)
            return subprocess.CompletedProcess(args, 0)
        if command == "respawn-pane":
            if target == self.fail_next_respawn_target:
                self.fail_next_respawn_target = ""
                return subprocess.CompletedProcess(args, 9, stderr="injected failure")
            return subprocess.CompletedProcess(args, 0)
        if command == "select-pane" and target == self.fail_next_select_target:
            self.fail_next_select_target = ""
            return subprocess.CompletedProcess(args, 8, stderr="injected focus failure")
        if command == "display-message":
            template = args[-1]
            if template == "#{pane_dead}":
                return subprocess.CompletedProcess(args, 0, stdout="0\n")
            if template == "#{pane_pid}":
                pid = self.worker_pids.get(session, 0)
                return subprocess.CompletedProcess(args, 0 if pid else 1, stdout=f"{pid}\n" if pid else "")
            if template == "#{@switchyard_role}":
                role = self.proxy_roles.get(session, "")
                return subprocess.CompletedProcess(args, 0, stdout=f"{role}\n")
            if template == "#{@switchyard_project}":
                project = session.split("-display-", 1)[0] if "-display-" in session else ""
                return subprocess.CompletedProcess(args, 0, stdout=f"{project}\n")
            return subprocess.CompletedProcess(args, 0, stdout="\n")
        if command == "set-option" and "-p" in args and args[-2] == "@switchyard_role":
            self.proxy_roles[session] = args[-1]
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)


DIRECTOR_ENV = {"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": "porter"}


def test_present_cli_is_owner_runtime_command_with_explicit_targets() -> None:
    assert "present" in team_launcher.SWITCHYARD_COMMANDS
    assert not team_launcher.switchyard_invocation_requires_root(["present", "porter", "list"])
    parser = team_launcher._build_switchyard_present_parser()
    show = parser.parse_args(["porter", "show", "app", "--slot", "1"])
    assert (show.project, show.action, show.role, show.slot) == ("porter", "show", "app", 1)
    swap = parser.parse_args(["porter", "swap", "0", "1"])
    assert (swap.slot_a, swap.slot_b) == (0, 1)


def test_fresh_project_config_records_projection_derived_presentation_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-fresh.") as tmp:
        root = Path(tmp)
        source = root / "source"
        repository = root / "repository"
        output = root / "output"
        source.mkdir()
        repository.mkdir()
        output.mkdir()
        plan = team_launcher.build_plan(
            project="porter",
            owner_user=team_launcher.current_user_name(),
            source_repo=source,
            implementer_roles=("app",),
        )
        config_path = team_launcher.write_new_project_launcher_artifacts(
            plan,
            output,
            repository=repository,
            implementer_roles=("app",),
        )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        visible = [role for role in raw["roles"] if not role.get("detached")]
        assert raw["presentation"] == {
            "slot_count": len(visible),
            "layouts": {"default": {str(role["slot"]): role["role"] for role in visible}},
        }


def test_runtime_mapping_preserves_worker_identity_and_restores_defaults() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()
        config_before = config_path.read_bytes()
        pids_before = dict(runner.worker_pids)
        config.session_dir.mkdir(parents=True)
        resume_records: dict[Path, bytes] = {}
        for role in config.roles:
            record = config.session_dir / session_file_name(role.target)
            record.write_text(
                json.dumps({"target": role.target, "session_id": f"resume-{role.role}"}),
                encoding="utf-8",
            )
            resume_records[record] = record.read_bytes()

        first = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="bootstrap",
            layout="viewer", environ=DIRECTOR_ENV, runner=runner,
        )
        assert first["slots"] == {"0": "director", "1": "app"}
        stable_sessions = {"porter-display-0", "porter-display-1", "porter-viewer"}
        assert stable_sessions <= runner.sessions

        swapped = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="swap",
            slot=0, other_slot=1, environ=DIRECTOR_ENV, runner=runner,
        )
        assert swapped["slots"] == {"0": "app", "1": "director"}
        presentation.launch_presentation(
            config, config_path=config_path, state_path=state_path, layout="viewer", runner=runner,
        )
        relaunched = json.loads(state_path.read_text(encoding="utf-8"))
        assert relaunched["slots"] == {"0": "app", "1": "director"}
        assert relaunched["revision"] == 2
        hidden = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="hide",
            slot=0, environ=DIRECTOR_ENV, runner=runner,
        )
        assert hidden["slots"] == {"0": None, "1": "director"}
        shown = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="show", role_name="app",
            slot=1, environ=DIRECTOR_ENV, runner=runner,
        )
        assert shown["slots"] == {"0": None, "1": "app"}
        restored = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="restore",
            layout="default", environ=DIRECTOR_ENV, runner=runner,
        )
        assert restored["slots"] == {"0": "director", "1": "app"}
        focused = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="focus",
            slot=1, environ=DIRECTOR_ENV, runner=runner,
        )
        assert focused["focused_slot"] == 1
        assert ["tmux", "select-pane", "-t", "porter-viewer:0.1"] in runner.calls
        assert [entry["action"] for entry in restored["history"]] == [
            "bootstrap", "swap", "hide", "show", "restore"
        ]
        assert focused["history"][-1]["action"] == "focus"
        assert all(entry["actor"] == "director" for entry in restored["history"])
        assert runner.worker_pids == pids_before
        assert {"porter-director", "porter-app"} <= runner.sessions
        assert stable_sessions <= runner.sessions
        assert config_path.read_bytes() == config_before
        assert {path: path.read_bytes() for path in resume_records} == resume_records
        assert json.loads(state_path.read_text(encoding="utf-8"))["revision"] == 6

        report = presentation.presentation_report(
            config, config_path=config_path, state_path=state_path, runner=runner,
        )
        assert [(item["desired_role"], item["actual_role"]) for item in report["slots"]] == [
            ("director", "director"), ("app", "app")
        ]
        assert report["hidden_roles"] == []
        assert report["focused_slot"] == 1
        assert presentation.stop_presentation(config, runner=runner) == 0
        assert "porter-display-0" not in runner.sessions
        assert "porter-display-1" not in runner.sessions
        assert {"porter-director", "porter-app"} <= runner.sessions


def test_restore_uses_current_projected_roles_when_config_default_is_stale() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-projection.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()
        presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="bootstrap",
            layout="viewer", environ=DIRECTOR_ENV, runner=runner,
        )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["roles"][1].update(
            role="main", tmux_session="porter-main", target="porter-main:0.0",
            workdir=str(root / "main"),
        )
        (root / "main").mkdir()
        # SYRD-11 preserves top-level metadata; its default may therefore still
        # name the prior role. RoleConfig slots must win.
        assert raw["presentation"]["layouts"]["default"]["1"] == "app"
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        runner.sessions.add("porter-main")
        runner.worker_pids["porter-main"] = 4103
        projected = load_project_config("porter", config_path)
        restored = presentation.presentation_action(
            projected, config_path=config_path, state_path=state_path, action="restore",
            layout="default", environ=DIRECTOR_ENV, runner=runner,
        )
        assert restored["slots"] == {"0": "director", "1": "main"}


def test_named_config_layout_is_saved_and_restorable() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-layout.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["presentation"]["layouts"]["app_only"] = {"0": "app", "1": None}
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()
        restored = presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="restore",
            layout="app_only", environ=DIRECTOR_ENV, runner=runner,
        )
        assert restored["slots"] == {"0": "app", "1": None}
        assert restored["active_layout"] == "app_only"
        assert restored["layouts"]["app_only"] == {"0": "app", "1": None}


def test_existing_state_grows_safely_when_role_projection_adds_a_visible_slot() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-grow.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()
        presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="bootstrap",
            layout="viewer", environ=DIRECTOR_ENV, runner=runner,
        )
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        (root / "main").mkdir()
        raw["roles"].append(
            {
                "role": "main", "slot": 2, "tmux_session": "porter-main",
                "target": "porter-main:0.0", "workdir": str(root / "main"),
                "cli": ["codex"], "live_commands": ["codex"],
            }
        )
        assert raw["presentation"]["slot_count"] == 2
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        runner.sessions.add("porter-main")
        runner.worker_pids["porter-main"] = 4103
        expanded = load_project_config("porter", config_path)
        presentation.launch_presentation(
            expanded, config_path=config_path, state_path=state_path, layout="viewer", runner=runner,
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["slot_count"] == 3
        assert state["slots"] == {"0": "director", "1": "app", "2": "main"}
        assert "porter-display-2" in runner.sessions


def test_full_launch_keeps_starting_workers_and_builds_recovery_slots_after_failures() -> None:
    for layout_mode in (team_launcher.LAYOUT_MODE_VIEWER, team_launcher.LAYOUT_MODE_SEPARATE):
        with tempfile.TemporaryDirectory(prefix=f"switchyard-presentation-entry-{layout_mode}.") as tmp:
            root = Path(tmp)
            config_path = _write_presentation_config(root)
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            (root / "research").mkdir()
            raw["desktop_access"] = {"mode": "headless"}
            raw["roles"].append(
                {
                    "role": "research", "detached": True, "tmux_session": "porter-research",
                    "target": "porter-research:0.0", "workdir": str(root / "research"),
                    "cli": ["codex"], "live_commands": ["codex"],
                }
            )
            config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            config = load_project_config("porter", config_path)
            attempts: list[str] = []
            runner = FakeRunner()
            process_launcher = RecordingProcessLauncher()
            original_detached = team_launcher.run_detached_role
            original_visible = team_launcher.ensure_visible_role_session_for_viewer
            original_freshness = team_launcher.ensure_launcher_checkout_current
            try:
                def detached(role: team_launcher.RoleConfig, **_kwargs: Any) -> int:
                    attempts.append(role.role)
                    return 7

                def visible(role: team_launcher.RoleConfig, **_kwargs: Any) -> int:
                    attempts.append(role.role)
                    if role.role == "director":
                        return 7
                    runner.existing_sessions.add(role.tmux_session)
                    return 0

                team_launcher.run_detached_role = detached
                team_launcher.ensure_visible_role_session_for_viewer = visible
                team_launcher.ensure_launcher_checkout_current = lambda *_args, **_kwargs: None
                errors = StringIO()
                with redirect_stderr(errors):
                    result = launch_project(
                        config,
                        config_path=config_path,
                        mode="attach-or-start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=runner,
                        layout_output=root / "materialized.json",
                        pane_state_dir=root / "pane-state",
                        layout_mode=layout_mode,
                        report_session_records=False,
                        konsole_process_launcher=process_launcher,
                    )
            finally:
                team_launcher.run_detached_role = original_detached
                team_launcher.ensure_visible_role_session_for_viewer = original_visible
                team_launcher.ensure_launcher_checkout_current = original_freshness

            assert result == 7
            assert attempts == ["research", "director", "app"]
            assert {"porter-display-0", "porter-display-1"} <= runner.existing_sessions
            assert any(
                call[:2] == ["tmux", "new-session"] and "director is missing" in call[-1]
                for call in runner.calls
            )
            if layout_mode == team_launcher.LAYOUT_MODE_VIEWER:
                assert "porter-viewer" in runner.existing_sessions
                assert process_launcher.calls == []
            else:
                assert "porter-viewer" not in runner.existing_sessions
                assert len(process_launcher.calls) == 1
            assert "pane start failed with exit 7 for research" in errors.getvalue()
            assert "pane start failed with exit 7 for director" in errors.getvalue()


def test_recover_command_requires_desktop_readiness_and_uses_prepared_role_environment() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-recover-entry.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["desktop_access"] = {"mode": "headless"}
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("porter", config_path)
        runner = PresentationRunner()
        args = team_launcher._build_switchyard_present_parser().parse_args(["porter", "recover", "app"])
        prepare_calls = 0
        start_calls = 0
        prior_caller = os.environ.get("TICKET_BOARD_CALLER_ROLE")
        prior_project = os.environ.get("TICKET_BOARD_PROJECT")
        original_prepare = team_launcher.prepare_project_desktop
        original_start = team_launcher.ensure_visible_role_session_for_viewer
        original_auth = team_launcher.run_switchyard_launch_first_run_auth
        try:
            os.environ.update(DIRECTOR_ENV)

            def reject_readiness(_config: team_launcher.ProjectConfig, **_kwargs: Any) -> team_launcher.ProjectConfig:
                nonlocal prepare_calls
                prepare_calls += 1
                raise SystemExit("desktop readiness rejected")

            def unexpected_start(*_args: Any, **_kwargs: Any) -> int:
                nonlocal start_calls
                start_calls += 1
                return 0

            team_launcher.prepare_project_desktop = reject_readiness
            team_launcher.ensure_visible_role_session_for_viewer = unexpected_start
            try:
                team_launcher.switchyard_present_command(
                    config, config_path=config_path, args=args, runner=runner,
                )
            except SystemExit as exc:
                assert "desktop readiness rejected" in str(exc)
            else:
                raise AssertionError("recovery bypassed rejected desktop readiness")
            assert prepare_calls == 1
            assert start_calls == 0

            prepared_env: dict[str, str] = {}

            def prepare_environment(
                candidate: team_launcher.ProjectConfig, **_kwargs: Any
            ) -> team_launcher.ProjectConfig:
                nonlocal prepare_calls
                prepare_calls += 1
                return team_launcher.replace(
                    candidate,
                    roles=[
                        team_launcher.replace(
                            role,
                            env={**role.env, "WAYLAND_DISPLAY": "wayland-ready"},
                        )
                        for role in candidate.roles
                    ],
                )

            def capture_start(role: team_launcher.RoleConfig, **_kwargs: Any) -> int:
                nonlocal start_calls
                start_calls += 1
                prepared_env.update(role.env)
                return 0

            team_launcher.prepare_project_desktop = prepare_environment
            team_launcher.ensure_visible_role_session_for_viewer = capture_start
            team_launcher.run_switchyard_launch_first_run_auth = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("recovery repeated interactive first-run consent")
            )
            output = StringIO()
            with redirect_stdout(output):
                result = team_launcher.switchyard_present_command(
                    config, config_path=config_path, args=args, runner=runner,
                )
            state_path = presentation.presentation_state_path(config, config_path=config_path)
            recovered = json.loads(state_path.read_text(encoding="utf-8"))
            assert result == 0
            assert recovered["history"][-1]["action"] == "recover"
            assert prepared_env["WAYLAND_DISPLAY"] == "wayland-ready"
            assert prepare_calls == 2
            assert start_calls == 1
        finally:
            team_launcher.prepare_project_desktop = original_prepare
            team_launcher.ensure_visible_role_session_for_viewer = original_start
            team_launcher.run_switchyard_launch_first_run_auth = original_auth
            if prior_caller is None:
                os.environ.pop("TICKET_BOARD_CALLER_ROLE", None)
            else:
                os.environ["TICKET_BOARD_CALLER_ROLE"] = prior_caller
            if prior_project is None:
                os.environ.pop("TICKET_BOARD_PROJECT", None)
            else:
                os.environ["TICKET_BOARD_PROJECT"] = prior_project


def test_failed_proxy_update_rolls_back_mapping_and_revision() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-rollback.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()
        presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="bootstrap",
            layout="viewer", environ=DIRECTOR_ENV, runner=runner,
        )
        before = state_path.read_bytes()
        runner.fail_next_respawn_target = "porter-display-1:0.0"
        try:
            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="swap",
                slot=0, other_slot=1, environ=DIRECTOR_ENV, runner=runner,
            )
        except SystemExit as exc:
            assert "prior mapping restored" in str(exc)
        else:
            raise AssertionError("injected tmux failure was accepted")
        assert state_path.read_bytes() == before
        assert runner.proxy_roles["porter-display-0"] == "director"
        assert runner.proxy_roles["porter-display-1"] == "app"
        runner.fail_next_select_target = "porter-viewer:0.1"
        try:
            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="focus",
                slot=1, environ=DIRECTOR_ENV, runner=runner,
            )
        except SystemExit as exc:
            assert "prior focus preserved" in str(exc)
        else:
            raise AssertionError("injected focus failure was accepted")
        assert state_path.read_bytes() == before


def test_authorization_and_project_namespace_bound_runtime_changes() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-auth.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        runner = PresentationRunner()
        for environ, expected in (
            ({"TICKET_BOARD_CALLER_ROLE": "app", "TICKET_BOARD_PROJECT": "porter"}, "require"),
            ({"TICKET_BOARD_CALLER_ROLE": "director", "TICKET_BOARD_PROJECT": "otto"}, "cross-project"),
        ):
            try:
                presentation.presentation_action(
                    config, config_path=config_path, state_path=root / "state.json", action="hide",
                    slot=0, environ=environ, runner=runner,
                )
            except SystemExit as exc:
                assert expected in str(exc)
            else:
                raise AssertionError("unauthorized presentation change was accepted")

        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["roles"][1]["target"] = "otto-app:0.0"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        foreign = load_project_config("porter", config_path)
        try:
            presentation.presentation_report(foreign, config_path=config_path, runner=runner)
        except SystemExit as exc:
            assert "foreign presentation target" in str(exc)
        else:
            raise AssertionError("foreign role target was accepted")


def test_list_reports_hidden_missing_and_resumable_roles_without_creating_state() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-status.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()
        runner.sessions.remove("porter-app")
        config.session_dir.mkdir(parents=True)
        app = next(role for role in config.roles if role.role == "app")
        (config.session_dir / session_file_name(app.target)).write_text(
            json.dumps({"target": app.target, "session_id": "resume-app"}), encoding="utf-8"
        )
        report = presentation.presentation_report(
            config, config_path=config_path, state_path=state_path, runner=runner,
        )
        assert not state_path.exists()
        assert report["slots"][1]["worker"] == {
            "role": "app", "state": "missing", "live": False, "resumable": True
        }
        assert report["slots"][1]["client_state"] == "disconnected"


def test_separate_bootstrap_attaches_konsole_leaves_to_stable_slots() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-separate.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        runner = PresentationRunner()
        process_launcher = RecordingProcessLauncher()
        presentation.presentation_action(
            config, config_path=config_path, state_path=root / "presentation.json", action="bootstrap",
            layout="separate", environ=DIRECTOR_ENV, runner=runner, process_launcher=process_launcher,
        )
        assert len(process_launcher.calls) == 1
        layout_path = root / ".switchyard" / "porter" / "porter-presentation-layout.json"
        leaves = team_launcher._layout_leaves(json.loads(layout_path.read_text(encoding="utf-8")))
        assert [shlex.split(leaf["Command"])[-1] for leaf in leaves] == [
            "porter-display-0", "porter-display-1"
        ]
        assert {"porter-display-0", "porter-display-1"} <= runner.sessions


def test_isolated_tmux_swap_hide_show_preserves_worker_pid_and_typed_composer() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-real.") as tmp:
        root = Path(tmp)
        tmux_tmp = root / "tmux"
        tmux_tmp.mkdir(mode=0o700)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        tmux_env = dict(os.environ)
        tmux_env.pop("TMUX", None)
        tmux_env.pop("TMUX_PANE", None)
        tmux_env["TMUX_TMPDIR"] = str(tmux_tmp)

        def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
            call_env = dict(tmux_env)
            supplied_env = kwargs.pop("env", None)
            if supplied_env:
                call_env.update(supplied_env)
            return subprocess.run(args, env=call_env, **kwargs)

        try:
            for role in config.roles:
                runner([
                    "tmux", "new-session", "-d", "-s", role.tmux_session, "-c", role.workdir,
                    "bash", "--noprofile", "--norc",
                ], check=True)
            app = next(role for role in config.roles if role.role == "app")
            runner(["tmux", "send-keys", "-l", "-t", app.target, "draft-preserved-42"], check=True)
            pid_before = runner(
                ["tmux", "display-message", "-p", "-t", app.target, "#{pane_pid}"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            capture_before = runner(
                ["tmux", "capture-pane", "-p", "-t", app.target],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout
            assert "draft-preserved-42" in capture_before

            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="bootstrap",
                layout="viewer", environ=DIRECTOR_ENV, runner=runner,
            )
            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="swap",
                slot=0, other_slot=1, environ=DIRECTOR_ENV, runner=runner,
            )
            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="hide",
                slot=0, environ=DIRECTOR_ENV, runner=runner,
            )
            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="show", role_name="app",
                slot=0, environ=DIRECTOR_ENV, runner=runner,
            )

            pid_after = runner(
                ["tmux", "display-message", "-p", "-t", app.target, "#{pane_pid}"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            capture_after = runner(
                ["tmux", "capture-pane", "-p", "-t", app.target],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout
            assert pid_after == pid_before
            assert "draft-preserved-42" in capture_after
            for role in config.roles:
                pane_count = runner(
                    ["tmux", "list-panes", "-t", role.tmux_session, "-F", "#{pane_id}"],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip().splitlines()
                assert len(pane_count) == 1

            runner(["tmux", "send-keys", "-t", app.target, "C-u"], check=True)
            runner(["tmux", "send-keys", "-t", app.target, "exit", "Enter"], check=True)
            for _attempt in range(40):
                recovery_surface = runner(
                    ["tmux", "capture-pane", "-p", "-t", "porter-display-0:0.0"],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout
                if "app disconnected" in recovery_surface:
                    break
                time.sleep(0.025)
            pane_dead = runner(
                ["tmux", "display-message", "-p", "-t", "porter-display-0:0.0", "#{pane_dead}"],
                check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            assert pane_dead == "0"
            assert "app disconnected" in recovery_surface
            assert "switchyard present porter recover app" in recovery_surface
        finally:
            runner(["tmux", "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    run_module_tests(globals())
    print("team_launcher_presentation_test: ok")
