#!/usr/bin/env python3
"""Regression coverage for stable runtime presentation slots."""

from __future__ import annotations

from team_launcher_test_helpers import *

import fcntl
import pty
import struct
import termios

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
        # tty -> session, for the presentation-owned panes and any client that
        # already displays a worker session on its own.
        self.pane_ttys: dict[str, list[str]] = {}
        self.session_clients: dict[str, list[str]] = {}

    @staticmethod
    def _session(target: str) -> str:
        return target.removeprefix("=").split(":", 1)[0]

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
        if command == "list-panes":
            if session not in self.sessions:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(
                args, 0, stdout="".join(f"{tty}\n" for tty in self.pane_ttys.get(session, []))
            )
        if command == "list-clients":
            if session not in self.sessions:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(
                args, 0, stdout="".join(f"{tty}\n" for tty in self.session_clients.get(session, []))
            )
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
        assert ["tmux", "select-pane", "-t", "=porter-viewer:0.1"] in runner.calls
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
        runner.fail_next_respawn_target = "=porter-display-1:0.0"
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
        runner.fail_next_select_target = "=porter-viewer:0.1"
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
            "=porter-display-0", "=porter-display-1"
        ]
        assert {"porter-display-0", "porter-display-1"} <= runner.sessions


def _proxy_attach_argv(pane_command: str) -> list[str]:
    """The attach the display slot runs inside its `sh -lc` proxy script."""
    shell, flag, script = shlex.split(pane_command)
    assert [shell, flag] == ["sh", "-lc"], pane_command
    return shlex.split(script.split(";", 1)[0])


def _status_options(runner: PresentationRunner) -> dict[str, str]:
    return {
        call[call.index("-t") + 1]: call[-1]
        for call in runner.calls
        if call[:2] == ["tmux", "set-option"] and call[-2] == "status"
    }


def test_presentation_frames_drop_their_status_bar_and_yield_worker_geometry() -> None:
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-observers.") as tmp:
        root = Path(tmp)
        config_path = _write_presentation_config(root)
        config = load_project_config("porter", config_path)
        state_path = root / "presentation.json"
        runner = PresentationRunner()

        presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="bootstrap",
            layout="viewer", environ=DIRECTOR_ENV, runner=runner,
        )
        # Both presentation frames are chrome around a worker that draws its own
        # status line, so neither adds a second bar.
        assert _status_options(runner) == {
            "=porter-display-0:": "off",
            "=porter-display-1:": "off",
            "=porter-viewer:": "off",
        }
        viewer_panes = [
            call[-1] for call in runner.calls
            if (call[:2] == ["tmux", "new-session"] and "porter-viewer" in call)
            or call[:2] == ["tmux", "split-window"]
        ]
        assert [shlex.split(command)[:2] for command in viewer_panes] == [["sh", "-lc"], ["sh", "-lc"]]
        assert [shlex.split(shlex.split(command)[2]) for command in viewer_panes] == [
            ["env", "TMUX=", "tmux", "attach", "-f", "ignore-size", "-t", "=porter-display-0"],
            ["env", "TMUX=", "tmux", "attach", "-f", "ignore-size", "-t", "=porter-display-1"],
        ]
        # No worker is displayed anywhere else yet, so each slot stays the
        # client that sizes the worker it presents.
        proxy_commands = [
            call[-1] for call in runner.calls
            if call[:2] == ["tmux", "new-session"]
            and call[call.index("-s") + 1].startswith("porter-display-")
        ]
        assert [_proxy_attach_argv(command) for command in proxy_commands] == [
            ["env", "TMUX=", "tmux", "attach", "-f", "!no-detach-on-destroy", "-t", "=porter-director"],
            ["env", "TMUX=", "tmux", "attach", "-f", "!no-detach-on-destroy", "-t", "=porter-app"],
        ]

        # The director worker is only on its slot; the app worker is also open
        # in a window of its own.
        runner.pane_ttys = {
            "porter-display-0": ["/dev/pts/100"],
            "porter-display-1": ["/dev/pts/101"],
            "porter-viewer": ["/dev/pts/200", "/dev/pts/201"],
        }
        runner.session_clients = {
            "porter-director": ["/dev/pts/100"],
            "porter-app": ["/dev/pts/101", "/dev/pts/9"],
        }
        runner.calls.clear()
        presentation.presentation_action(
            config, config_path=config_path, state_path=state_path, action="restore",
            layout="default", environ=DIRECTOR_ENV, runner=runner,
        )
        respawned = {
            call[call.index("-t") + 1]: call[-1]
            for call in runner.calls
            if call[1] == "respawn-pane"
        }
        assert _proxy_attach_argv(respawned["=porter-display-0:0.0"]) == [
            "env", "TMUX=", "tmux", "attach", "-f", "!no-detach-on-destroy", "-t", "=porter-director",
        ]
        assert _proxy_attach_argv(respawned["=porter-display-1:0.0"]) == [
            "env", "TMUX=", "tmux", "attach", "-f", "ignore-size,!no-detach-on-destroy", "-t", "=porter-app",
        ]
        # Reconciliation repeats the policy on the live viewer instead of
        # waiting for the next bootstrap.
        assert _status_options(runner) == {
            "=porter-display-0:": "off",
            "=porter-display-1:": "off",
            "=porter-viewer:": "off",
        }
        for tty in ("/dev/pts/200", "/dev/pts/201"):
            assert ["tmux", "refresh-client", "-f", "ignore-size", "-t", tty] in runner.calls


def test_isolated_tmux_clients_preserve_visible_sizes_and_a_single_status_bar() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-clients.") as tmp:
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

        def tmux(*args: str) -> str:
            proc = runner(list(args), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            return str(proc.stdout or "").strip()

        def window_size(session: str) -> str:
            return tmux("tmux", "display-message", "-p", "-t", f"={session}:0", "#{window_width}x#{window_height}")

        def status(session: str) -> str:
            # -A resolves the inherited global default for a session that never
            # set the option itself.
            return tmux("tmux", "show-options", "-A", "-v", "-t", f"={session}:", "status")

        def client_flags(session: str) -> dict[str, str]:
            listed = tmux("tmux", "list-clients", "-t", f"={session}", "-F", "#{client_tty} #{client_flags}")
            return dict(line.split(" ", 1) for line in listed.splitlines() if " " in line)

        def wait_for(predicate: Callable[[], bool], message: str) -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.05)
            raise AssertionError(message)

        clients: list[tuple[subprocess.Popen[bytes], int]] = []

        def attach(session: str, columns: int, rows: int) -> None:
            before = len(client_flags(session))
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
            proc = subprocess.Popen(
                ["tmux", "attach", "-t", f"={session}"],
                stdin=slave, stdout=slave, stderr=slave, env=tmux_env, start_new_session=True,
            )
            os.close(slave)
            clients.append((proc, master))
            wait_for(lambda: len(client_flags(session)) > before, f"no client attached to {session}")

        try:
            for role in ("director", "app"):
                runner(["tmux", "new-session", "-d", "-x", "240", "-y", "80", "-s", f"porter-{role}", "sleep", "600"], check=True)
            # The app worker is already visible in a window of its own, at the
            # size the operator actually sees.
            attach("porter-app", 106, 44)
            assert window_size("porter-app") == "106x43"

            presentation.launch_presentation(
                config, config_path=config_path, state_path=state_path, layout="viewer", runner=runner,
            )
            for slot in (0, 1):
                attach(f"porter-display-{slot}", 106, 44)
            watched = ("porter-display-0", "porter-display-1", "porter-app", "porter-director")
            wait_for(
                lambda: all(window_size(session).startswith("106x") for session in watched),
                "display slots never took the size of the windows showing them",
            )
            visible_sizes = {session: window_size(session) for session in watched}

            # A secondary viewer, smaller than every window it aggregates.
            attach("porter-viewer", 119, 25)

            viewer_ttys = set(tmux("tmux", "list-panes", "-s", "-t", "=porter-viewer", "-F", "#{pane_tty}").split())
            assert len(viewer_ttys) == 2
            for slot in (0, 1):
                observers = {
                    tty: flags for tty, flags in client_flags(f"porter-display-{slot}").items()
                    if tty in viewer_ttys
                }
                assert len(observers) == 1
                assert all("ignore-size" in flags for flags in observers.values())

            def sizes_preserved() -> bool:
                return {session: window_size(session) for session in watched} == visible_sizes

            # The viewer client is settled; nothing it observes may have moved.
            time.sleep(0.5)
            assert {session: window_size(session) for session in watched} == visible_sizes
            # Each frame drops its own status line, so a slot shows its whole
            # client and the worker keeps the row its own status line needs.
            assert visible_sizes == {
                "porter-display-0": "106x44",
                "porter-display-1": "106x44",
                "porter-app": "106x43",
                "porter-director": "106x43",
            }
            # One status bar per visible worker: the frames carry none, and the
            # worker session keeps its own.
            assert status("porter-display-0") == "off"
            assert status("porter-display-1") == "off"
            assert status("porter-viewer") == "off"
            assert status("porter-app") == "on"

            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="swap",
                slot=0, other_slot=1, environ=DIRECTOR_ENV, runner=runner,
            )
            wait_for(sizes_preserved, "reconciliation resized a visible window")
            assert status("porter-display-0") == "off"
            assert status("porter-viewer") == "off"
            assert status("porter-app") == "on"
        finally:
            for proc, master in clients:
                proc.kill()
                os.close(master)
            runner(["tmux", "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_isolated_tmux_exact_targets_preserve_prefix_collision_sessions() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="switchyard-presentation-collision.") as tmp:
        root = Path(tmp)
        tmux_tmp = root / "tmux"
        tmux_tmp.mkdir(mode=0o700)
        config_path = _write_presentation_config(root)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["desktop_access"] = {"mode": "headless"}
        bin_dir = root / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\nexec sleep 300\n", encoding="utf-8")
        codex.chmod(0o755)
        for role in raw["roles"]:
            if role["role"] == "app":
                role["env"] = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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

        collisions = ("porter-app-extra", "porter-display-0-extra", "porter-viewer-extra")

        def collision_snapshot() -> dict[str, tuple[str, str, str, str]]:
            snapshot: dict[str, tuple[str, str, str, str]] = {}
            for session in collisions:
                pane_pid = runner(
                    ["tmux", "display-message", "-p", "-t", f"={session}:0.0", "#{pane_pid}"],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip()
                attached = runner(
                    ["tmux", "display-message", "-p", "-t", f"={session}", "#{session_attached}"],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip()
                status_left = runner(
                    ["tmux", "show-options", "-v", "-t", f"={session}:", "status-left"],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip()
                role_label = runner(
                    [
                        "tmux", "show-options", "-p", "-v", "-t", f"={session}:0.0",
                        "@switchyard_role",
                    ],
                    check=True, text=True, stdout=subprocess.PIPE,
                ).stdout.strip()
                snapshot[session] = (pane_pid, attached, status_left, role_label)
            return snapshot

        try:
            for session in collisions:
                runner(["tmux", "new-session", "-d", "-s", session, "sleep", "300"], check=True)
                runner([
                    "tmux", "set-option", "-t", f"={session}:",
                    "status-left", f" safe-{session} ",
                ], check=True)
                runner([
                    "tmux", "set-option", "-p", "-t", f"={session}:0.0",
                    "@switchyard_role", f"safe-{session}",
                ], check=True)
            before = collision_snapshot()

            report = presentation.presentation_report(
                config, config_path=config_path, state_path=state_path, runner=runner,
            )
            assert report["slots"][0]["client_state"] == "disconnected"
            assert report["slots"][1]["worker"] == {
                "role": "app", "state": "missing", "live": False, "resumable": False,
            }
            assert collision_snapshot() == before

            presentation.launch_presentation(
                config, config_path=config_path, state_path=state_path,
                layout="viewer", runner=runner,
            )
            for session in ("porter-display-0", "porter-display-1", "porter-viewer"):
                runner(["tmux", "has-session", "-t", f"={session}"], check=True)
            assert collision_snapshot() == before

            presentation.presentation_action(
                config, config_path=config_path, state_path=state_path, action="recover",
                role_name="app", environ=DIRECTOR_ENV, runner=runner,
            )
            runner(["tmux", "has-session", "-t", "=porter-app"], check=True)
            assert presentation._role_status(config, "app", runner=runner)["state"] == "live"
            assert collision_snapshot() == before

            assert presentation.stop_presentation(config, runner=runner) == 0
            for session in collisions:
                runner(["tmux", "has-session", "-t", f"={session}"], check=True)
            assert collision_snapshot() == before
        finally:
            runner(["tmux", "kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
