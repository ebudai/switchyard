#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_viewer_session_argument_is_required_and_has_no_global_default() -> None:
    signature = inspect.signature(team_launcher.launch_tmux_viewer_session)
    assert signature.parameters["viewer_session"].default is inspect.Parameter.empty
    assert "DEFAULT_VIEWER_SESSION" not in (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")


class OwnerScopedTmuxRunner(FakeRunner):
    def __init__(self, owner_user: str) -> None:
        super().__init__()
        self.owner_user = owner_user
        self.sessions_by_user: dict[str, set[str]] = {"root": set(), owner_user: set()}
        self.socket_by_user = {
            "root": "/tmp/tmux-0/default",
            owner_user: "/tmp/tmux-4242/default",
        }
        self.used_sockets: list[str] = []
        self.tmux_calls: list[tuple[str, list[str]]] = []
        self.nested_attach_targets: list[str] = []

    @staticmethod
    def _target_session(args: list[str]) -> str:
        if "-t" not in args:
            return ""
        target = args[args.index("-t") + 1]
        return target.split(":", 1)[0].split(".", 1)[0]

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        user = "root"
        inner = args
        if args[:4] == ["sudo", "-u", self.owner_user, "-H"]:
            user = self.owner_user
            inner = args[4:]

        if len(inner) >= 5 and Path(inner[0]).name == "team-launcher" and inner[2] == "pane":
            self.calls.append(args)
            role = inner[4]
            self.sessions_by_user[user].add(f"{inner[1]}-{role}")
            return subprocess.CompletedProcess(args, 0)

        if inner[:1] != ["tmux"]:
            return super().__call__(args, **kwargs)

        self.calls.append(args)
        self.used_sockets.append(self.socket_by_user[user])
        self.tmux_calls.append((user, inner))
        sessions = self.sessions_by_user[user]
        command = inner[1] if len(inner) > 1 else ""
        target_session = self._target_session(inner)

        if command == "has-session":
            return subprocess.CompletedProcess(args, 0 if target_session in sessions else 1)
        if command == "display-message":
            role = target_session.rsplit("-", 1)[-1]
            live_command = "claude" if role in {"designer", "director", "audit"} else "codex"
            return subprocess.CompletedProcess(args, 0, stdout=f"{live_command}\n")
        if command == "kill-session":
            if target_session not in sessions:
                return subprocess.CompletedProcess(args, 1)
            sessions.remove(target_session)
            return subprocess.CompletedProcess(args, 0)
        if command == "new-session":
            nested = shlex.split(str(inner[-1]))
            nested_target = nested[nested.index("-t") + 1]
            self.nested_attach_targets.append(nested_target)
            if nested_target not in sessions:
                return subprocess.CompletedProcess(args, 1)
            sessions.add(inner[inner.index("-s") + 1])
            return subprocess.CompletedProcess(args, 0)
        if command == "split-window":
            nested = shlex.split(str(inner[-1]))
            nested_target = nested[nested.index("-t") + 1]
            self.nested_attach_targets.append(nested_target)
            if target_session not in sessions or nested_target not in sessions:
                return subprocess.CompletedProcess(args, 1)
            return subprocess.CompletedProcess(args, 0)
        if target_session and target_session not in sessions:
            return subprocess.CompletedProcess(args, 1)
        return subprocess.CompletedProcess(args, 0)


def test_cross_owner_viewer_uses_owner_tmux_for_fresh_and_existing_sessions() -> None:
    owner_user = "porter-agent"
    role_sessions = [
        "porter-designer",
        "porter-director",
        "porter-audit",
        "porter-ops",
        "porter-app",
        "porter-main",
    ]
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-owner.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["run_as_user"] = owner_user
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("porter", config_path)
        runner = OwnerScopedTmuxRunner(owner_user)
        original_current_user_name = team_launcher.current_user_name
        try:
            team_launcher.current_user_name = lambda: "root"
            # The first launch starts a fresh project's role and viewer sessions.
            # The second replaces its viewer while the registered role sessions remain live.
            for _ in range(2):
                assert (
                    launch_project(
                        config,
                        config_path=config_path,
                        mode="start",
                        script_path=ROOT / "scripts" / "team-launcher",
                        runner=runner,
                        layout_output=tmp_path / "layout-output.json",
                        pane_state_dir=tmp_path / "pane-state",
                        layout_mode="viewer",
                        layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                    )
                    == 0
                )
        finally:
            team_launcher.current_user_name = original_current_user_name

    assert runner.sessions_by_user[owner_user] == {*role_sessions, "porter-viewer"}
    assert runner.sessions_by_user["root"] == set()
    assert runner.tmux_calls
    assert {user for user, _args in runner.tmux_calls} == {owner_user}
    assert set(runner.used_sockets) == {"/tmp/tmux-4242/default"}
    assert "/tmp/tmux-0/default" not in runner.used_sockets
    assert all(
        call[:5] == ["sudo", "-u", owner_user, "-H", "tmux"]
        for call in runner.calls
        if call[:1] == ["tmux"] or (len(call) > 4 and call[4] == "tmux")
    )
    assert runner.nested_attach_targets == [*role_sessions, *role_sessions]


def test_viewer_layout_starts_role_sessions_and_additive_viewer() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        config = load_project_config("porter", config_path)
        runner = FakeRunner()
        messages: list[str] = []
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(owner_home)
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                    layout_mode="viewer",
                    layout_environ={"XDG_CURRENT_DESKTOP": "KDE"},
                    print_func=messages.append,
                )
                == 0
            )
        finally:
            if original_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = original_home
        assert not (owner_home / ".tmux.conf").exists()

    role_sessions = ["porter-designer", "porter-director", "porter-audit", "porter-ops", "porter-app", "porter-main"]
    role_new_sessions = [
        call for call in runner.calls
        if call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] in role_sessions
    ]
    assert [call[call.index("-s") + 1] for call in role_new_sessions] == role_sessions
    viewer_new_sessions = [
        call for call in runner.calls
        if call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] == "porter-viewer"
    ]
    assert len(viewer_new_sessions) == 1
    assert "env TMUX= tmux attach -t porter-designer" in viewer_new_sessions[0][-1]
    viewer_splits = [call for call in runner.calls if call[:2] == ["tmux", "split-window"]]
    assert len(viewer_splits) == 5
    for session in role_sessions:
        assert ["tmux", "set-option", "-t", session, "mouse", "on"] in runner.calls
        assert ["tmux", "set-option", "-t", session, "history-limit", "200000"] in runner.calls
        assert ["tmux", "set-option", "-t", session, "status", "off"] in runner.calls
        assert ["tmux", "set-window-option", "-t", f"{session}:0", "pane-border-status", "off"] in runner.calls
    assert ["tmux", "set-option", "-t", "porter-viewer", "mouse", "on"] in runner.calls
    assert ["tmux", "set-option", "-t", "porter-viewer", "history-limit", "200000"] in runner.calls
    assert ["tmux", "set-option", "-t", "porter-viewer", "status", "on"] in runner.calls
    assert ["tmux", "set-option", "-t", "porter-viewer", "set-titles", "on"] in runner.calls
    assert ["tmux", "set-option", "-t", "porter-viewer", "set-titles-string", "porter"] in runner.calls
    assert ["tmux", "set-option", "-t", "porter-viewer", "prefix", "C-a"] in runner.calls
    assert ["tmux", "set-window-option", "-t", "porter-viewer:0", "pane-border-status", "top"] in runner.calls
    assert ["tmux", "set-window-option", "-t", "porter-viewer:0", "pane-border-format", " #{@role} "] in runner.calls
    assert [call for call in runner.calls if call[:4] == ["tmux", "set-option", "-p", "-t"]] == [
        ["tmux", "set-option", "-p", "-t", f"porter-viewer.{index}", "@role", role]
        for index, role in enumerate(["designer", "director", "audit", "ops", "app", "main"])
    ]
    assert not any(call[:2] == ["env", "QT_QPA_PLATFORM=wayland"] for call in runner.calls)
    assert messages == []

def test_project_scoped_viewer_sessions_allow_two_projects_and_same_project_replacement() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-multi.") as tmp:
        tmp_path = Path(tmp)
        porter_dir = tmp_path / "porter"
        otto_dir = tmp_path / "otto"
        porter_dir.mkdir()
        otto_dir.mkdir()
        porter_config_path = _write_six_visible_role_config(porter_dir, project="porter")
        otto_config_path = _write_six_visible_role_config(otto_dir, project="otto")
        porter_config = load_project_config("porter", porter_config_path)
        otto_config = load_project_config("otto", otto_config_path)
        runner = FakeRunner()

        for config, config_path in ((porter_config, porter_config_path), (otto_config, otto_config_path)):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / f"{config.project}-layout-output.json",
                    pane_state_dir=tmp_path / "pane-state",
                    layout_mode="viewer",
                    layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                )
                == 0
            )

        assert "porter-viewer" in runner.existing_sessions
        assert "otto-viewer" in runner.existing_sessions

        assert (
            launch_project(
                porter_config,
                config_path=porter_config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "porter-layout-output.json",
                pane_state_dir=tmp_path / "pane-state",
                layout_mode="viewer",
                layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
            )
            == 0
        )

    kill_calls = [call for call in runner.calls if call[:2] == ["tmux", "kill-session"]]
    assert [call[-1] for call in kill_calls] == ["porter-viewer"]
    assert "porter-viewer" in runner.existing_sessions
    assert "otto-viewer" in runner.existing_sessions
    assert ["tmux", "set-option", "-t", "porter-viewer", "set-titles-string", "porter"] in runner.calls
    assert ["tmux", "set-option", "-t", "otto-viewer", "set-titles-string", "otto"] in runner.calls

def test_viewer_window_title_uses_project_name_and_survives_attach_cycle_options() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-title.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path, project="otto")
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["project_name"] = "Otto Scheduler"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("otto", config_path)
        runner = FakeRunner()

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout.json",
                pane_state_dir=tmp_path / "pane-state",
                layout_mode="viewer",
                layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
            )
            == 0
        )

    assert ["tmux", "set-option", "-t", "otto-viewer", "set-titles", "on"] in runner.calls
    assert ["tmux", "set-option", "-t", "otto-viewer", "set-titles-string", "Otto Scheduler"] in runner.calls

def test_project_scoped_viewer_ignores_legacy_global_viewer_session() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-legacy.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path, project="porter")
        config = load_project_config("porter", config_path)
        runner = FakeRunner(existing_sessions={"viewer"})

        assert (
            launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout-output.json",
                pane_state_dir=tmp_path / "pane-state",
                layout_mode="viewer",
                layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
            )
            == 0
        )

    assert "viewer" in runner.existing_sessions
    assert "porter-viewer" in runner.existing_sessions
    assert ["tmux", "kill-session", "-t", "viewer"] not in runner.calls

def test_viewer_visible_start_verifies_cli_before_reporting_success() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-visible.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        config = load_project_config("porter", config_path)
        role = next(role for role in config.roles if role.role == "app")
        runner = FakeRunner()
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                team_launcher.ensure_visible_role_session_for_viewer(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
                == 0
            )

        state = _read_pane_state(tmp_path / "pane-state", role.target)
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "porter-app"]
        assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "porter-app:0.0", "#{pane_pid}"]
        assert runner.calls[3] == ["tmux", "display-message", "-p", "-t", "porter-app:0.0", "#{pane_current_command}"]
        assert "did not leave a live" not in stderr.getvalue()

def test_viewer_visible_start_fails_when_cli_exits_immediately() -> None:
    config = team_launcher.replace(load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json"), desktop_access={"mode": "headless"})
    role = next(role for role in config.roles if role.role == "app")

    class VanishedViewerRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.created = False

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:2] == ["tmux", "new-session"]:
                self.created = True
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 0 if self.created else 1, stdout="fish\n" if self.created else "")
            return subprocess.CompletedProcess(args, 0)

    runner = VanishedViewerRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        pane_state_dir = tmp_path / "pane-state"
        with redirect_stderr(stderr):
            assert (
                team_launcher.ensure_visible_role_session_for_viewer(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=pane_state_dir,
                    runner=runner,
                )
                == 1
            )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-app"] for call in runner.calls)
    assert ["tmux", "has-session", "-t", "pgu-app"] in runner.calls
    assert "role app did not leave a live pgu-app session" in stderr.getvalue()

def test_viewer_pane_death_does_not_change_role_targets_in_real_tmux() -> None:
    if shutil.which("tmux") is None:
        return
    roles = ["designer", "director", "audit", "ops", "app", "main"]
    suffix = f"pgu660_{os.getpid()}"
    server = f"{suffix}-server"
    runner = _isolated_tmux_runner(server)
    viewer_session = f"{suffix}-viewer"
    sessions = {role: f"{suffix}-{role}" for role in roles}
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-real.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path, project=suffix)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        for role_config in raw_config["roles"]:
            role = role_config["role"]
            role_config["tmux_session"] = sessions[role]
            role_config["target"] = f"{sessions[role]}:0.0"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config(suffix, config_path)
        try:
            for session in sessions.values():
                _run_isolated_tmux(server, ["new-session", "-d", "-s", session, "-c", str(tmp_path), "/bin/sh"], check=True)
            assert team_launcher.launch_tmux_viewer_session(
                team_launcher.visible_roles_for_viewer(config),
                viewer_session=viewer_session,
                runner=runner,
            ) == 0
            panes = _run_isolated_tmux(
                server,
                ["list-panes", "-t", f"{viewer_session}:0", "-F", "#{pane_index}:#{@role}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip().splitlines()
            assert panes == [f"{index}:{role}" for index, role in enumerate(roles)]

            _run_isolated_tmux(server, ["kill-pane", "-t", f"{viewer_session}:0.1"], check=True)
            after = _run_isolated_tmux(
                server,
                ["list-panes", "-t", f"{viewer_session}:0", "-F", "#{pane_index}:#{@role}"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip().splitlines()
            assert len(after) == 5

            for role, session in sessions.items():
                marker = tmp_path / f"{role}.txt"
                _run_isolated_tmux(server, ["has-session", "-t", session], check=True)
                _run_isolated_tmux(
                    server,
                    ["send-keys", "-t", f"{session}:0.0", f"printf {role!r} > {shlex.quote(str(marker))}", "Enter"],
                    check=True,
                )
                for _ in range(50):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                assert marker.read_text(encoding="utf-8") == role
        finally:
            _cleanup_isolated_tmux_sessions(server, [viewer_session, *sessions.values()])

def test_two_project_viewer_sessions_coexist_in_real_isolated_tmux() -> None:
    if shutil.which("tmux") is None:
        return
    roles = ["designer", "director", "audit", "ops", "app", "main"]
    suffix = f"pgu874_{os.getpid()}"
    server = f"{suffix}-server"
    runner = _isolated_tmux_runner(server)
    created_sessions: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-two-real.") as tmp:
        tmp_path = Path(tmp)
        try:
            for project in ("porter", "otto"):
                project_dir = tmp_path / project
                project_dir.mkdir()
                config_path = _write_six_visible_role_config(project_dir, project=project)
                config = load_project_config(project, config_path)
                for role in roles:
                    session = f"{project}-{role}"
                    created_sessions.append(session)
                    _run_isolated_tmux(server, ["new-session", "-d", "-s", session, "-c", str(project_dir), "/bin/sh"], check=True)
                viewer_session = team_launcher.viewer_session_for_project(project)
                created_sessions.append(viewer_session)
                assert (
                    team_launcher.launch_tmux_viewer_session(
                        team_launcher.visible_roles_for_viewer(config),
                        viewer_session=viewer_session,
                        runner=runner,
                    )
                    == 0
                )

            _run_isolated_tmux(server, ["has-session", "-t", "porter-viewer"], check=True)
            _run_isolated_tmux(server, ["has-session", "-t", "otto-viewer"], check=True)

            porter_config = load_project_config("porter", tmp_path / "porter" / "porter.json")
            assert (
                team_launcher.launch_tmux_viewer_session(
                    team_launcher.visible_roles_for_viewer(porter_config),
                    viewer_session="porter-viewer",
                    runner=runner,
                )
                == 0
            )
            _run_isolated_tmux(server, ["has-session", "-t", "porter-viewer"], check=True)
            _run_isolated_tmux(server, ["has-session", "-t", "otto-viewer"], check=True)
        finally:
            _cleanup_isolated_tmux_sessions(server, created_sessions)

def test_detached_research_start_fails_visible_when_session_disappears() -> None:
    config = team_launcher.replace(load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json"), desktop_access={"mode": "headless"})
    role = next(role for role in config.roles if role.role == "research")

    class VanishingDetachedRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:2] == ["tmux", "new-session"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                return subprocess.CompletedProcess(args, 1, stdout="")
            return subprocess.CompletedProcess(args, 0)

    runner = VanishingDetachedRunner()
    stderr = StringIO()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        pane_state_dir = tmp_path / "pane-state"
        with redirect_stderr(stderr):
            assert (
                run_detached_role(
                    role,
                    mode="start",
                    session_dir=tmp_path / "sessions",
                    pane_state_dir=pane_state_dir,
                    runner=runner,
                )
                == 1
            )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert any(call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"] for call in runner.calls)
    assert "role research did not leave a live pgu-research session" in stderr.getvalue()

def test_detached_research_resume_falls_back_when_failed_session_is_already_gone() -> None:
    config = team_launcher.replace(load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json"), desktop_access={"mode": "headless"})
    role = next(role for role in config.roles if role.role == "research")
    stderr = StringIO()

    class ResumeGoneThenFreshRunner:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.fresh_live = False

        def __call__(self, args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 0 if self.fresh_live else 1)
            if args[:2] == ["tmux", "new-session"]:
                self.fresh_live = "--resume " not in str(args[-1])
                return subprocess.CompletedProcess(args, 0)
            if args[:3] == ["tmux", "display-message", "-p"]:
                if args[-1] == "#{pane_pid}":
                    return subprocess.CompletedProcess(args, 1, stdout="")
                if self.fresh_live:
                    return subprocess.CompletedProcess(args, 0, stdout="claude\n")
                return subprocess.CompletedProcess(args, 1, stdout="")
            if args[:2] == ["tmux", "kill-session"]:
                raise AssertionError("missing failed resume session should not be killed before fallback")
            return subprocess.CompletedProcess(args, 0)

    original_stability = team_launcher.DETACHED_SESSION_STABILITY_SECONDS
    try:
        team_launcher.DETACHED_SESSION_STABILITY_SECONDS = 0.0
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
            tmp_path = Path(tmp)
            session_dir = tmp_path / "sessions"
            session_dir.mkdir()
            session_id = "12345678-1234-5678-9abc-def012345678"
            transcript = tmp_path / "claude-session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps(
                    {"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}}
                )
                + "\n",
                encoding="utf-8",
            )
            pane_state_dir = tmp_path / "pane-state"
            runner = ResumeGoneThenFreshRunner()
            with redirect_stderr(stderr):
                assert (
                    run_detached_role(
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
            assert state["source"] == "team_launcher.start.fallback"
    finally:
        team_launcher.DETACHED_SESSION_STABILITY_SECONDS = original_stability

    assert f"team-launcher: resume failed for research using session {session_id}; falling back to fresh session" in stderr.getvalue()
    assert "team-launcher: started fresh session for research after resume fallback" in stderr.getvalue()
    new_sessions = [call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"]]
    assert len(new_sessions) == 2
    assert "--resume" in new_sessions[0][-1]
    assert "--resume" not in new_sessions[1][-1]

def test_full_launch_delegates_detached_role_start_to_owner_pane_subcommand() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-detached-owner.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        owner_runtime = tmp_path / "run" / "user" / "4242"
        owner_runtime.mkdir(parents=True)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "desktop_access": {"mode": "headless"},
                    "project": "porter",
                    "run_as_user": "porter-agent",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [
                        {
                            "role": "research",
                            "detached": True,
                            "cli": ["claude"],
                            "target": "porter-research:0.0",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        original_getpwnam = team_launcher.pwd.getpwnam
        original_runtime_dir_for_uid = team_launcher.runtime_dir_for_uid
        original_current_user_name = team_launcher.current_user_name

        class OwnerPw:
            pw_dir = str(owner_home)
            pw_uid = 4242
            pw_gid = 4242

        def fake_getpwnam(user_name: str) -> Any:
            if user_name == "porter-agent":
                return OwnerPw()
            return original_getpwnam(user_name)

        try:
            team_launcher.pwd.getpwnam = fake_getpwnam
            team_launcher.runtime_dir_for_uid = lambda uid: owner_runtime if uid == 4242 else Path(f"/run/user/{uid}")
            team_launcher.current_user_name = lambda: "root"
            config = load_project_config("porter", config_path)
            runner = FakeRunner()
            process_launcher = RecordingProcessLauncher()
            result = launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout-output.json",
                layout_environ={"SUDO_USER": "root", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
                konsole_process_launcher=process_launcher,
            )
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam
            team_launcher.runtime_dir_for_uid = original_runtime_dir_for_uid
            team_launcher.current_user_name = original_current_user_name

        pane_state_dir = owner_runtime / "porter-ticket-board" / "pane-state"
        expected = [
            "sudo",
            "-u",
            "porter-agent",
            "-H",
            str(ROOT / "scripts" / "team-launcher"),
            "porter",
            "pane",
            "attach-or-start",
            "research",
            "--config",
            str(config_path),
            "--skip-launcher-check",
            "--pane-state-dir",
            str(pane_state_dir),
        ]
        assert result == 0
        assert expected in runner.calls
        assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)
        assert not (tmp_path / "run" / "user" / "0" / "porter-ticket-board" / "pane-state").exists()

def test_all_detached_undetected_viewer_launch_still_reports_detached_start_failure() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-detached-failure.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        layout = tmp_path / "layout.json"
        layout.write_text('{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8")
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "desktop_access": {"mode": "headless"},
                    "project": "porter",
                    "layout": str(layout),
                    "repository": str(repo),
                    "roles": [
                        {
                            "role": "research",
                            "detached": True,
                            "cli": ["claude"],
                            "target": "porter-research:0.0",
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        class FailedDetachedStartRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                self.calls.append(args)
                if args[:2] == ["tmux", "has-session"]:
                    return subprocess.CompletedProcess(args, 1)
                if args[:2] == ["tmux", "new-session"]:
                    return subprocess.CompletedProcess(args, 0)
                if args[:3] == ["tmux", "display-message", "-p"]:
                    return subprocess.CompletedProcess(args, 1, stdout="")
                return super().__call__(args, **kwargs)

        config = load_project_config("porter", config_path)
        runner = FailedDetachedStartRunner()
        process_launcher = RecordingProcessLauncher()
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = launch_project(
                config,
                config_path=config_path,
                mode="start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout-output.json",
                layout_environ={"SUDO_USER": "root", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
                konsole_process_launcher=process_launcher,
            )

        assert result == 1
        assert "role research did not leave a live porter-research session" in stderr.getvalue()
        assert not process_launcher.calls
        assert not any(call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] == "viewer" for call in runner.calls)

def test_undetected_viewer_launch_reports_failure_when_every_visible_role_failed() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-visible-failure.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path, project="porter")
        config = load_project_config("porter", config_path)

        class FailingSharedFetchRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
                if args == git_fetch_worktree_ref_args(config):
                    self.calls.append(args)
                    return subprocess.CompletedProcess(args, 128, stderr="fatal: could not fetch\n")
                return super().__call__(args, **kwargs)

        runner = FailingSharedFetchRunner()
        process_launcher = RecordingProcessLauncher()
        stderr = StringIO()

        with redirect_stderr(stderr):
            result = launch_project(
                config,
                config_path=config_path,
                mode="attach-or-start",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=tmp_path / "layout-output.json",
                layout_environ={"SUDO_USER": "root", "XDG_CURRENT_DESKTOP": "", "KDE_FULL_SESSION": ""},
                konsole_process_launcher=process_launcher,
            )

        assert result == 1
        assert "skipping visible role main: fatal: could not fetch" in stderr.getvalue()
        assert not process_launcher.calls
        assert not any(call[:2] == ["tmux", "new-session"] and call[call.index("-s") + 1] == "viewer" for call in runner.calls)

def test_start_force_refreshes_dirty_shared_checkout_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        config = load_project_config("pgu", config_path)
        shared_checkout = Path(config.roles[0].workdir)
        assert shared_checkout == repo

        assert ensure_project_worktrees(config, refresh=True).ok

        (shared_checkout / "tracked.txt").write_text("dirty tracked edit\n", encoding="utf-8")
        (shared_checkout / "untracked.tmp").write_text("junk\n", encoding="utf-8")

        assert ensure_project_worktrees(config, refresh=True).ok

        assert (shared_checkout / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
        assert not (shared_checkout / "untracked.tmp").exists()
        status = _run_git(["git", "status", "--porcelain"], cwd=shared_checkout).stdout
        assert status == ""

def test_launch_warns_before_resetting_dirty_shared_checkout_with_real_git() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, repo: Path) -> None:
            super().__init__()
            self.repo = repo

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.repo:
                run_kwargs = dict(kwargs)
                run_kwargs.setdefault("text", True)
                if "stdout" not in run_kwargs:
                    run_kwargs["stdout"] = subprocess.PIPE
                if "stderr" not in run_kwargs:
                    run_kwargs["stderr"] = subprocess.PIPE
                return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-dirty-project.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        layout_path = tmp_path / "launch-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["layout"] = str(layout_path)
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("pgu", config_path)
        runner = ProjectGitRunner(repo)
        process_launcher = RecordingProcessLauncher()
        stderr = StringIO()

        (repo / "tracked.txt").write_text("uncommitted tracked edit\n", encoding="utf-8")
        (repo / "my_notes.txt").write_text("notes that will be removed\n", encoding="utf-8")

        with redirect_stderr(stderr):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout-out.json",
                    pane_state_dir=tmp_path / "pane-state",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

        warning = stderr.getvalue()
        assert "will reset managed project checkout" in warning
        assert "tracked changes will be discarded" in warning
        assert "tracked.txt" in warning
        assert "untracked files will be removed" in warning
        assert "my_notes.txt" in warning
        assert (repo / "tracked.txt").read_text(encoding="utf-8") == "initial\n"
        assert not (repo / "my_notes.txt").exists()

def test_clean_launch_does_not_warn_about_shared_checkout_refresh() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, repo: Path) -> None:
            super().__init__()
            self.repo = repo

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.repo:
                run_kwargs = dict(kwargs)
                run_kwargs.setdefault("text", True)
                if "stdout" not in run_kwargs:
                    run_kwargs["stdout"] = subprocess.PIPE
                if "stderr" not in run_kwargs:
                    run_kwargs["stderr"] = subprocess.PIPE
                return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-clean-project.") as tmp:
        tmp_path = Path(tmp)
        _origin, repo = _make_origin_backed_repo(tmp_path)
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _run_git(["git", "add", ".gitignore"], cwd=repo)
        _run_git(["git", "commit", "-m", "ignore launcher bytecode"], cwd=repo)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"])
        layout_path = tmp_path / "launch-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["layout"] = str(layout_path)
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("pgu", config_path)
        (repo / "scripts" / "ticket_board" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "ticket_board" / "__pycache__" / "team_launcher.cpython-313.pyc").write_bytes(b"pyc\n")
        process_launcher = RecordingProcessLauncher()
        stderr = StringIO()

        with redirect_stderr(stderr):
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=ProjectGitRunner(repo),
                    layout_output=tmp_path / "layout-out.json",
                    pane_state_dir=tmp_path / "pane-state",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )

        assert "will reset managed project checkout" not in stderr.getvalue()

def test_default_layout_does_not_create_untracked_switchyard_in_shared_checkout() -> None:
    class ProjectGitRunner(FakeRunner):
        def __init__(self, repo: Path) -> None:
            super().__init__()
            self.repo = repo

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 3 and args[:2] == ["git", "-C"] and Path(args[2]) == self.repo:
                run_kwargs = dict(kwargs)
                run_kwargs.setdefault("text", True)
                if "stdout" not in run_kwargs:
                    run_kwargs["stdout"] = subprocess.PIPE
                if "stderr" not in run_kwargs:
                    run_kwargs["stderr"] = subprocess.PIPE
                return subprocess.run(args, **run_kwargs)
            return super().__call__(args, **kwargs)

    original_getpwnam = team_launcher.pwd.getpwnam
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-clean-project.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "owner-home"
        owner_home.mkdir()
        _origin, repo = _make_origin_backed_repo(tmp_path)
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops"], run_as_user="agent")
        layout_path = tmp_path / "launch-layout.json"
        layout_path.write_text(
            json.dumps(
                {
                    "Orientation": "Horizontal",
                    "Widgets": [{"SessionRestoreId": 0, "Command": "", "WorkingDirectory": ""}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["layout"] = str(layout_path)
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("pgu", config_path)
        runner = ProjectGitRunner(repo)
        process_launcher = RecordingProcessLauncher()
        stderr = StringIO()

        class FakeUserInfo:
            pw_dir = str(owner_home)
            pw_uid = os.getuid()

        try:
            def fake_getpwnam(user_name: str) -> object:
                return FakeUserInfo() if user_name == "agent" else original_getpwnam(user_name)

            team_launcher.pwd.getpwnam = fake_getpwnam
            expected_layout = owner_home / ".local" / "state" / "switchyard" / "projects" / "pgu" / "pgu-team-layout.json"

            for _ in range(2):
                with redirect_stderr(stderr):
                    assert (
                        launch_project(
                            config,
                            config_path=config_path,
                            mode="start",
                            script_path=ROOT / "scripts" / "team-launcher",
                            runner=runner,
                            pane_state_dir=tmp_path / "pane-state",
                            konsole_process_launcher=process_launcher,
                        )
                        == 0
                    )
        finally:
            team_launcher.pwd.getpwnam = original_getpwnam

        status = _run_git(["git", "status", "--porcelain"], cwd=repo).stdout
        assert expected_layout.is_file()
        assert not (repo / ".switchyard").exists()

    assert "will reset managed project checkout" not in stderr.getvalue()
    assert status == ""

def test_root_materialized_owner_state_layout_is_owner_writable_on_next_launch() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-layout-owner-transfer.") as tmp:
        tmp_path = Path(tmp)
        layout_template = tmp_path / "layout-template.json"
        layout_template.write_text(
            json.dumps({"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}) + "\n",
            encoding="utf-8",
        )
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "desktop_access": {"mode": "headless"},
                    "project": "porter",
                    "layout": str(layout_template),
                    "run_as_user": "porter-agent",
                    "session_dir": str(tmp_path / "sessions"),
                    "roles": [
                        {
                            "role": "ops",
                            "slot": 0,
                            "target": "porter-ops:0.0",
                            "tmux_session": "porter-ops",
                            "cli": ["codex"],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_project_config("porter", config_path)
        layout_output = (
            tmp_path
            / "home"
            / "porter-agent"
            / ".local"
            / "state"
            / "switchyard"
            / "projects"
            / "porter"
            / "porter-team-layout.json"
        )
        synthetic_owner: dict[Path, str] = {}
        active_user = "root"
        expected_chown_args = ["chown", "-R", "porter-agent:porter-agent", str(layout_output.parent)]

        class OwnershipRunner(FakeRunner):
            def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:1] == ["chown"]:
                    self.calls.append(args)
                    owner = args[-2] if len(args) >= 3 else ""
                    target = Path(args[-1]) if len(args) >= 3 else Path("/nonexistent")
                    recursive = "-R" in args[1:-2]
                    if owner == f"{config.run_as_user}:{config.run_as_user}":
                        synthetic_owner[target] = config.run_as_user
                        if recursive:
                            for path in list(synthetic_owner):
                                if path == target or path.is_relative_to(target):
                                    synthetic_owner[path] = config.run_as_user
                    return subprocess.CompletedProcess(args, 0)
                return super().__call__(args, **kwargs)

        def fake_materialize_layout(
            _config: team_launcher.ProjectConfig,
            *,
            output_path: Path,
            **_kwargs: object,
        ) -> Path:
            writer = team_launcher.current_user_name()
            existing_owner = synthetic_owner.get(output_path)
            if existing_owner is not None and existing_owner != writer:
                raise PermissionError(f"{writer} cannot overwrite layout owned by {existing_owner}: {output_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("{}\n", encoding="utf-8")
            synthetic_owner[output_path] = writer
            return output_path

        runner = OwnershipRunner()
        original_current_user_name = team_launcher.current_user_name
        original_materialize_layout = team_launcher.materialize_layout
        try:
            team_launcher.current_user_name = lambda: active_user
            team_launcher.materialize_layout = fake_materialize_layout

            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=layout_output,
                    assign_layout_owner=True,
                    pane_state_dir=tmp_path / "pane-state",
                    layout_mode="viewer",
                    layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                )
                == 0
            )
            assert synthetic_owner[layout_output] == "porter-agent"

            active_user = "porter-agent"
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="start",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=layout_output,
                    assign_layout_owner=True,
                    pane_state_dir=tmp_path / "pane-state",
                    layout_mode="viewer",
                    layout_environ={"XDG_CURRENT_DESKTOP": "GNOME"},
                )
                == 0
            )
        finally:
            team_launcher.current_user_name = original_current_user_name
            team_launcher.materialize_layout = original_materialize_layout

    assert runner.calls.count(expected_chown_args) == 1

def test_bad_shared_checkout_blocks_all_roles_with_real_git() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-git.") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "not-a-git-checkout"
        repo.mkdir()
        config_path = _write_minimal_shared_checkout_config(tmp_path, repo, ["ops", "app"])
        config = load_project_config("pgu", config_path)

        result = ensure_project_worktrees(config, refresh=True)

        assert set(result.failed_roles) == {"ops", "app"}

def test_reload_fetches_without_refreshing_shared_checkout() -> None:
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
                mode="reload",
                script_path=ROOT / "scripts" / "team-launcher",
                runner=runner,
                layout_output=layout_output,
                pane_state_dir=tmp_path / "pane-state",
                konsole_process_launcher=process_launcher,
            )
            == 0
        )

        assert runner.calls[:3] == [
            git_launcher_checkout_check_args(ROOT),
            git_launcher_head_args(ROOT),
            git_launcher_ls_remote_ref_args(config, ROOT),
        ]
        assert runner.calls[3] == git_fetch_worktree_ref_args(config)
        assert not any(call[:5] == ["git", "-C", call[2], "worktree", "add"] for call in runner.calls)
        assert git_checkout_shared_ref_args(config) not in runner.calls
        assert git_clean_shared_checkout_args(config) not in runner.calls

def test_reload_syncs_live_cli_and_model_from_process_argv_before_relaunch() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["live_commands"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        transcript = tmp_path / "claude-research-session.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {
                    "session_id": "live-research-session",
                    "cwd": "/home/agent/Projects/pgu",
                    "transcript_path": str(transcript),
                },
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1200})
        process_launcher = RecordingProcessLauncher()
        original_snapshot = _patch_process_snapshot(
            {1200: 6388, 1201: 1200},
            {1200: [1201]},
            {1200: {"fish", "claude"}, 1201: {"claude"}},
            {
                1200: ["fish", "-c", "claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
                1201: ["claude", "--model", "claude-sonnet-5", "--dangerously-skip-permissions"],
            },
        )
        try:
            assert (
                launch_project(
                    config,
                    config_path=config_path,
                    mode="reload",
                    script_path=ROOT / "scripts" / "team-launcher",
                    runner=runner,
                    layout_output=tmp_path / "layout.json",
                    pane_state_dir=tmp_path / "pane-state",
                    konsole_process_launcher=process_launcher,
                )
                == 0
            )
        finally:
            team_launcher._process_snapshot = original_snapshot

        synced = json.loads(config_path.read_text(encoding="utf-8"))
        synced_research = next(role for role in synced["roles"] if role["role"] == "research")
        assert synced_research["cli"] == ["claude"]
        assert synced_research["live_commands"] == ["claude"]
        assert synced_research["model"] == "claude-sonnet-5"
        assert synced_research["detached"] is True
        assert synced_research["target"] == "pgu-research:0.0"
        assert synced_research["effort"] == "high"
        assert synced_research["yolo"] is True

        research_new_session = next(call for call in runner.calls if call[:5] == ["tmux", "new-session", "-d", "-s", "pgu-research"])
        assert "claude --resume live-research-session --model claude-sonnet-5" in research_new_session[-1]
        assert "codex resume" not in research_new_session[-1]

def test_reload_sync_leaves_config_unchanged_when_live_matches() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1300})
        before = config_path.read_text(encoding="utf-8")
        original_snapshot = _patch_process_snapshot(
            {1300: 6388, 1301: 1300},
            {1300: [1301]},
            {1300: {"fish", "claude"}, 1301: {"claude"}},
            {
                1300: ["fish", "-c", "claude", "--model", "claude-opus-5", "--dangerously-skip-permissions"],
                1301: ["claude", "--model", "claude-opus-5", "--dangerously-skip-permissions"],
            },
        )
        try:
            synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
        finally:
            team_launcher._process_snapshot = original_snapshot

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before

def test_reload_sync_leaves_config_unchanged_when_role_not_running() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["live_commands"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner()
        before = config_path.read_text(encoding="utf-8")

        synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before

def test_reload_sync_leaves_config_unchanged_when_model_is_unparseable() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_pgu_config_with_shared_checkout(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        research_raw = next(role for role in raw_config["roles"] if role["role"] == "research")
        research_raw["cli"] = ["codex"]
        research_raw["model"] = "gpt-5.5"
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        config = load_project_config("pgu", config_path)
        research = next(role for role in config.roles if role.role == "research")
        config.session_dir.mkdir(parents=True, exist_ok=True)
        (config.session_dir / session_file_name(research.target)).write_text(
            _session_payload(
                research.target,
                "live-research-session",
                {"session_id": "live-research-session", "cwd": "/home/agent/Projects/pgu"},
            ),
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={"pgu-research"}, pane_pids={research.target: 1400})
        before = config_path.read_text(encoding="utf-8")
        original_snapshot = _patch_process_snapshot(
            {1400: 6388, 1401: 1400},
            {1400: [1401]},
            {1400: {"fish", "claude"}, 1401: {"claude"}},
            {
                1400: ["fish", "-c", "claude", "--dangerously-skip-permissions"],
                1401: ["claude", "--dangerously-skip-permissions"],
            },
        )
        try:
            synced = team_launcher.sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
        finally:
            team_launcher._process_snapshot = original_snapshot

        assert synced is config
        assert config_path.read_text(encoding="utf-8") == before

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_viewer_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
