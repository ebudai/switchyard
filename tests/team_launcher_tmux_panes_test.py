#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_konsole_log_is_readable_by_sudo_invoking_user() -> None:
    original_chown = team_launcher.os.chown
    chown_calls: list[tuple[Path, int, int]] = []

    def fake_chown(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], uid: int, gid: int) -> None:
        chown_calls.append((Path(path), uid, gid))

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-konsole-log.") as tmp:
        log_path = Path(tmp) / "konsole.log"
        log_path.write_text("", encoding="utf-8")
        log_path.chmod(0o600)
        try:
            team_launcher.os.chown = fake_chown
            team_launcher._make_konsole_log_readable(
                log_path,
                environ={"SUDO_USER": "user", "SUDO_UID": "1000", "SUDO_GID": "1000"},
            )
        finally:
            team_launcher.os.chown = original_chown

        assert chown_calls == [(log_path, 1000, 1000)]
        assert log_path.stat().st_mode & 0o777 == 0o644

def test_start_is_attach_or_start_and_never_duplicates_existing_session() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={"pgu-ops"})

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        session_dir = Path(tmp) / "sessions"
        pane_state_dir = Path(tmp) / "pane-state"

        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        assert not (pane_state_dir / pane_state_file_name(role.target)).exists()

    assert runner.calls == [
        ["tmux", "has-session", "-t", "pgu-ops"],
        ["tmux", "attach", "-t", "pgu-ops"],
    ]

def test_start_creates_missing_session_once_then_attaches() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner()

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher.") as tmp:
        # No recorded session id -> start falls back to a fresh launch (no resume args).
        tmp_path = Path(tmp)
        session_dir = tmp_path / "sessions"
        pane_state_dir = tmp_path / "pane-state"
        assert (
            run_role_pane(
                role,
                mode="start",
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                runner=runner,
            )
            == 0
        )

        state = _read_pane_state(pane_state_dir, role.target)
        assert state["target"] == role.target
        assert state["state"] == "idle"
        assert state["source"] == "team_launcher.start"
        assert isinstance(state["updated_at"], float)

    assert runner.calls[0] == ["tmux", "has-session", "-t", "pgu-ops"]
    assert runner.calls[1][:5] == ["tmux", "new-session", "-d", "-s", "pgu-ops"]
    assert runner.calls[1][5:7] == ["-c", str(_pgu_project_root())]
    assert runner.calls[1][7:9] == ["-n", "ops"]
    assert "TICKET_BOARD_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "PGU_PANE_TARGET=pgu-ops:0.0" in runner.calls[1][-1]
    assert "--model gpt-5.5" in runner.calls[1][-1]
    assert "-c reasoning_effort=high" in runner.calls[1][-1]
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.calls[1][-1]
    assert "--dangerously-bypass-hook-trust" in runner.calls[1][-1]
    assert "--reasoning-effort" not in runner.calls[1][-1]
    assert " resume " not in f" {runner.calls[1][-1]} "
    assert runner.calls[2] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_pid}"]
    assert runner.calls[3] == ["tmux", "display-message", "-p", "-t", "pgu-ops:0.0", "#{pane_current_command}"]
    assert ["tmux", "set-option", "-t", "pgu-ops", "mouse", "on"] in runner.calls
    assert ["tmux", "set-option", "-t", "pgu-ops", "history-limit", "200000"] in runner.calls
    assert ["tmux", "attach", "-t", "pgu-ops"] in runner.calls

def test_tmux_new_session_names_window_for_every_role() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-role-window.") as tmp:
        session_dir = Path(tmp) / "sessions"
        for role in config.roles:
            args = team_launcher.tmux_new_session_args(role, session_dir=session_dir)
            assert args[:5] == ["tmux", "new-session", "-d", "-s", role.tmux_session]
            assert args[args.index("-n") + 1] == role.role

def test_tmux_role_window_name_survives_cli_pane_title_write() -> None:
    if shutil.which("tmux") is None:
        return
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-role-title.") as tmp:
        tmp_path = Path(tmp)
        server = f"pgu800-role-title-{os.getpid()}"
        runner = _isolated_tmux_runner(server)
        helper = tmp_path / "fake-cli"
        helper.write_text(
            "#!/bin/sh\n"
            "title=\"$1\"\n"
            "marker=\"$2\"\n"
            "printf '\\033]2;%s\\033\\\\' \"$title\"\n"
            "touch \"$marker\"\n"
            "trap 'exit 0' TERM INT\n"
            "while :; do sleep 1; done\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        base_roles = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json").roles
        measured_cli_titles = {
            "director": "X Director",
            "audit": "X Audit",
            "research": "X Claude Code",
            "inspector": "machine",
            "main": "pgu",
            "app": "pgu",
            "ops": "pgu",
        }
        roles = []
        try:
            for base_role in base_roles:
                session = f"pgu800-role-title-{os.getpid()}-{base_role.role}"
                marker = tmp_path / f"{base_role.role}.running"
                role = base_role.__class__(
                    role=base_role.role,
                    slot=base_role.slot,
                    detached=base_role.detached,
                    tmux_session=session,
                    target=f"{session}:0.0",
                    workdir=str(tmp_path),
                    cli=[str(helper), measured_cli_titles[base_role.role], str(marker)],
                    model="",
                    model_arg=base_role.model_arg,
                    effort="",
                    yolo=False,
                    extra_args=[],
                    resume_mode=base_role.resume_mode,
                    resume_flag=base_role.resume_flag,
                    resume_subcommand=base_role.resume_subcommand,
                    live_commands=["fake-cli"],
                    env={},
                )
                roles.append(role)
                proc = runner(
                    team_launcher.tmux_new_session_args(role, session_dir=tmp_path / "sessions"),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                assert proc.returncode == 0, proc.stderr
                for _ in range(50):
                    if marker.exists():
                        break
                    time.sleep(0.1)
                assert marker.exists(), f"fake CLI never started for {base_role.role}"

            for role in roles:
                status = _run_isolated_tmux(
                    server,
                    ["display-message", "-p", "-t", role.target, "#{window_name}|#{pane_title}|#{automatic-rename}"],
                    text=True,
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout.strip()
                assert status == f"{role.role}|{measured_cli_titles[role.role]}|0"
        finally:
            _cleanup_isolated_tmux_sessions(server, [role.tmux_session for role in roles])

def test_pane_start_from_another_pane_uses_target_session_and_clears_caller_tmux_env() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    runner = FakeRunner()
    ambient_env_keys = [
        "TMUX",
        "TMUX_PANE",
        "TICKET_BOARD_PANE_TARGET",
        "PGU_PANE_TARGET",
        "TICKET_BOARD_CALLER_ROLE",
        "TICKET_BOARD_PANE_SESSION_DIR",
        "TICKET_BOARD_PANE_STATE_DIR",
        "PGU_TICKET_BOARD_PANE_SESSION_DIR",
        "PGU_TICKET_BOARD_PANE_STATE_DIR",
    ]
    original_env = {key: os.environ.get(key) for key in ambient_env_keys}
    try:
        os.environ["TMUX"] = "/tmp/tmux-1001/default,123,0"
        os.environ["TMUX_PANE"] = "%director"
        os.environ["TICKET_BOARD_PANE_TARGET"] = "pgu-director:0.0"
        os.environ["PGU_PANE_TARGET"] = "pgu-director:0.0"
        os.environ["TICKET_BOARD_CALLER_ROLE"] = "director"
        os.environ["TICKET_BOARD_PANE_SESSION_DIR"] = "/live/director/sessions"
        os.environ["TICKET_BOARD_PANE_STATE_DIR"] = "/live/director/state"
        os.environ["PGU_TICKET_BOARD_PANE_SESSION_DIR"] = "/live/director/legacy-sessions"
        os.environ["PGU_TICKET_BOARD_PANE_STATE_DIR"] = "/live/director/legacy-state"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-target-session.") as tmp:
            tmp_path = Path(tmp)
            session_dir = tmp_path / "sessions"
            pane_state_dir = tmp_path / "pane-state"
            session_dir.mkdir()
            session_id = "research-session-id"
            transcript = tmp_path / "research.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": session_id, "payload": {"transcript_path": str(transcript)}})
                + "\n",
                encoding="utf-8",
            )

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
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    new_session = next(call for call in runner.calls if call[:2] == ["tmux", "new-session"])
    assert f"claude --resume {session_id}" in new_session[-1]
    assert "director-session-id" not in new_session[-1]
    assert "TICKET_BOARD_PANE_TARGET=pgu-director:0.0" not in new_session[-1]
    assert "PGU_PANE_TARGET=pgu-director:0.0" not in new_session[-1]
    assert "/live/director" not in new_session[-1]
    assert "TICKET_BOARD_CALLER_ROLE=research" in new_session[-1]
    for key in set(team_launcher.PANE_TARGET_ENV_KEYS):
        assert f"-u {key}" in new_session[-1]
    assert "-u TMUX " not in f"{new_session[-1]} "
    assert "-u TMUX_PANE" not in new_session[-1]

def test_pane_reload_refuses_conflicting_ambient_session_id_before_killing_target() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "research")
    runner = FakeRunner(existing_sessions={role.tmux_session}, current_commands={role.target: "claude"})
    original_env = {
        "PGU_PANE_SESSION_ID": os.environ.get("PGU_PANE_SESSION_ID"),
        "TICKET_BOARD_PANE_SESSION_ID": os.environ.get("TICKET_BOARD_PANE_SESSION_ID"),
    }
    stderr = StringIO()
    try:
        os.environ["PGU_PANE_SESSION_ID"] = "director-session-id"
        os.environ["TICKET_BOARD_PANE_SESSION_ID"] = "director-session-id"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-session-conflict.") as tmp:
            session_dir = Path(tmp) / "sessions"
            session_dir.mkdir()
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": "research-session-id"}) + "\n",
                encoding="utf-8",
            )
            with redirect_stderr(stderr):
                result = run_detached_role(role, mode="reload", session_dir=session_dir, runner=runner)
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert result == 1
    assert "refusing to launch research" in stderr.getvalue()
    assert "director-session-id" in stderr.getvalue()
    assert "research-session-id" in stderr.getvalue()
    assert not any(call[:2] == ["tmux", "kill-session"] for call in runner.calls)
    assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)

def test_visible_pane_reload_refuses_conflicting_ambient_session_id_before_killing_target() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner(existing_sessions={role.tmux_session}, current_commands={role.target: "codex"})
    original_env = {
        "PGU_PANE_SESSION_ID": os.environ.get("PGU_PANE_SESSION_ID"),
        "TICKET_BOARD_PANE_SESSION_ID": os.environ.get("TICKET_BOARD_PANE_SESSION_ID"),
    }
    stderr = StringIO()
    try:
        os.environ["PGU_PANE_SESSION_ID"] = "director-session-id"
        os.environ["TICKET_BOARD_PANE_SESSION_ID"] = "director-session-id"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-visible-session-conflict.") as tmp:
            session_dir = Path(tmp) / "sessions"
            session_dir.mkdir()
            (session_dir / session_file_name(role.target)).write_text(
                json.dumps({"target": role.target, "session_id": "ops-session-id"}) + "\n",
                encoding="utf-8",
            )
            with redirect_stderr(stderr):
                result = run_role_pane(role, mode="reload", session_dir=session_dir, runner=runner)
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert result == 1
    assert "refusing to launch ops" in stderr.getvalue()
    assert "director-session-id" in stderr.getvalue()
    assert "ops-session-id" in stderr.getvalue()
    assert not any(call[:2] == ["tmux", "kill-session"] for call in runner.calls)
    assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)
    assert not any(call[:2] == ["tmux", "attach"] for call in runner.calls)

def test_viewer_visible_reload_refuses_conflicting_ambient_session_id_before_killing_target() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-viewer-session-conflict.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        config = load_project_config("porter", config_path)
        role = next(role for role in config.roles if role.role == "app")
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "app-session-id"}) + "\n",
            encoding="utf-8",
        )
        runner = FakeRunner(existing_sessions={role.tmux_session}, current_commands={role.target: "codex"})
        original_env = {
            "PGU_PANE_SESSION_ID": os.environ.get("PGU_PANE_SESSION_ID"),
            "TICKET_BOARD_PANE_SESSION_ID": os.environ.get("TICKET_BOARD_PANE_SESSION_ID"),
        }
        stderr = StringIO()
        try:
            os.environ["PGU_PANE_SESSION_ID"] = "director-session-id"
            os.environ["TICKET_BOARD_PANE_SESSION_ID"] = "director-session-id"
            with redirect_stderr(stderr):
                result = team_launcher.ensure_visible_role_session_for_viewer(
                    role,
                    mode="reload",
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    assert result == 1
    assert "refusing to launch app" in stderr.getvalue()
    assert "director-session-id" in stderr.getvalue()
    assert "app-session-id" in stderr.getvalue()
    assert not any(call[:2] == ["tmux", "kill-session"] for call in runner.calls)
    assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)

def test_attach_headless_role_refuses_conflicting_ambient_session_id_before_mutating_config() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-attach-session-conflict.") as tmp:
        tmp_path = Path(tmp)
        config_path = _write_six_visible_role_config(tmp_path)
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_config["roles"] = raw_config["roles"][:5]
        raw_config["roles"].append(
            {"role": "research", "detached": True, "target": "porter-research:0.0", "tmux_session": "porter-research", "cli": ["codex"]}
        )
        config_path.write_text(json.dumps(raw_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        config = load_project_config("porter", config_path)
        role = next(role for role in config.roles if role.role == "research")
        session_dir = tmp_path / "sessions"
        session_dir.mkdir()
        (session_dir / session_file_name(role.target)).write_text(
            json.dumps({"target": role.target, "session_id": "research-session-id"}) + "\n",
            encoding="utf-8",
        )
        before = config_path.read_text(encoding="utf-8")
        runner = FakeRunner()
        original_env = {
            "PGU_PANE_SESSION_ID": os.environ.get("PGU_PANE_SESSION_ID"),
            "TICKET_BOARD_PANE_SESSION_ID": os.environ.get("TICKET_BOARD_PANE_SESSION_ID"),
        }
        stderr = StringIO()
        try:
            os.environ["PGU_PANE_SESSION_ID"] = "director-session-id"
            os.environ["TICKET_BOARD_PANE_SESSION_ID"] = "director-session-id"
            with redirect_stderr(stderr):
                result = team_launcher.attach_role_to_slot(
                    config,
                    config_path=config_path,
                    role_name="research",
                    slot=5,
                    session_dir=session_dir,
                    pane_state_dir=tmp_path / "pane-state",
                    runner=runner,
                )
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        after = config_path.read_text(encoding="utf-8")

    assert result == 1
    assert after == before
    assert "refusing to launch research" in stderr.getvalue()
    assert "director-session-id" in stderr.getvalue()
    assert "research-session-id" in stderr.getvalue()
    assert not any(call[:2] == ["tmux", "has-session"] for call in runner.calls)
    assert not any(call[:2] == ["tmux", "new-session"] for call in runner.calls)
    assert not any(call[:2] == ["tmux", "attach"] for call in runner.calls)

def test_pane_start_without_recorded_session_ignores_ambient_session_and_starts_fresh() -> None:
    config = load_project_config("pgu", ROOT / "config" / "team-launcher" / "pgu.json")
    role = next(role for role in config.roles if role.role == "ops")
    runner = FakeRunner()
    original_env = {
        "PGU_PANE_SESSION_ID": os.environ.get("PGU_PANE_SESSION_ID"),
        "TICKET_BOARD_PANE_SESSION_ID": os.environ.get("TICKET_BOARD_PANE_SESSION_ID"),
    }
    stderr = StringIO()
    try:
        os.environ["PGU_PANE_SESSION_ID"] = "director-session-id"
        os.environ["TICKET_BOARD_PANE_SESSION_ID"] = "director-session-id"
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-fresh-session.") as tmp:
            with redirect_stderr(stderr):
                assert (
                    run_role_pane(
                        role,
                        mode="start",
                        session_dir=Path(tmp) / "sessions",
                        pane_state_dir=Path(tmp) / "pane-state",
                        runner=runner,
                    )
                    == 0
                )
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    new_session = next(call for call in runner.calls if call[:2] == ["tmux", "new-session"])
    assert "director-session-id" not in new_session[-1]
    assert " resume " not in f" {new_session[-1]} "
    assert "--resume" not in new_session[-1]
    assert "-u PGU_PANE_SESSION_ID" in new_session[-1]
    assert "-u TICKET_BOARD_PANE_SESSION_ID" in new_session[-1]
    assert "no recorded session id for ops; starting fresh session" in stderr.getvalue()

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_tmux_panes_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
