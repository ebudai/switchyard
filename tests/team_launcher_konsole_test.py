#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

# board_url marker keeps this split suite in board-adjacent discovery.
from team_launcher_test_helpers import *

def test_konsole_launch_uses_gui_user_display_environment() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_sudo_user = os.environ.get("SUDO_USER")
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        os.environ["SUDO_USER"] = "root"
        team_launcher.uid_for_user = lambda _user_name: None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user

def test_konsole_launch_normalizes_relative_host_wayland_display() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_sudo_user = os.environ.get("SUDO_USER")
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        os.environ["SUDO_USER"] = "eric"
        team_launcher.current_user_name = lambda: "root"

        def fake_uid_for_user(user_name: str) -> int | None:
            return {"eric": 1000, "root": 0}.get(user_name)

        team_launcher.uid_for_user = fake_uid_for_user

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_gui_user is not None:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user

def test_konsole_launch_can_fallback_to_gui_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name

def test_konsole_launch_prefers_host_wayland_over_configured_wayland_name() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    env_keys = [
        "HOST_WAYLAND_DISPLAY",
        "PGU_HOST_WAYLAND_DISPLAY",
        "TEAM_LAUNCHER_WAYLAND_DISPLAY",
        "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY",
    ]
    original_env = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-host"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-configured"
        os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-host",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def test_konsole_launch_prefers_legacy_host_wayland_over_legacy_configured_wayland_name() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    env_keys = [
        "HOST_WAYLAND_DISPLAY",
        "PGU_HOST_WAYLAND_DISPLAY",
        "TEAM_LAUNCHER_WAYLAND_DISPLAY",
        "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY",
    ]
    original_env = {key: os.environ.get(key) for key in env_keys}
    try:
        os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        os.environ["PGU_HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-legacy-host"
        os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-legacy-configured"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "user" else None

        assert konsole_launch_args(Path("/tmp/layout.json"), gui_user="user") == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-legacy-host",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def test_konsole_launch_defaults_to_invoking_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_sudo_user = os.environ.pop("SUDO_USER", None)
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = "wayland-test"
        team_launcher.current_user_name = lambda: "otto-agent"
        team_launcher.uid_for_user = lambda user_name: 4242 if user_name == "otto-agent" else None

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/4242/wayland-test",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_gui_user is not None:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is not None:
            os.environ["SUDO_USER"] = original_sudo_user
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name

def test_konsole_launch_defaults_to_sudo_user_uid_without_host_var() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_current_user_name = team_launcher.current_user_name
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    original_gui_user = os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
    original_legacy_gui_user = os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
    original_sudo_user = os.environ.get("SUDO_USER")
    original_wayland_name = os.environ.get("TEAM_LAUNCHER_WAYLAND_DISPLAY")
    original_legacy_wayland_name = os.environ.pop("PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
    try:
        os.environ["SUDO_USER"] = "eric"
        os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        team_launcher.current_user_name = lambda: "root"
        team_launcher.uid_for_user = lambda user_name: 1000 if user_name == "eric" else 0 if user_name == "root" else None

        assert konsole_launch_args(Path("/tmp/layout.json")) == [
            "env",
            "QT_QPA_PLATFORM=wayland",
            "WAYLAND_DISPLAY=/run/user/1000/wayland-0",
            "konsole",
            "--separate",
            "--layout",
            "/tmp/layout.json",
        ]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        team_launcher.current_user_name = original_current_user_name
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland
        if original_gui_user is not None:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is not None:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user
        if original_wayland_name is None:
            os.environ.pop("TEAM_LAUNCHER_WAYLAND_DISPLAY", None)
        else:
            os.environ["TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_wayland_name
        if original_legacy_wayland_name is not None:
            os.environ["PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"] = original_legacy_wayland_name

def test_configured_gui_user_overrides_sudo_user() -> None:
    original_current_user_name = team_launcher.current_user_name
    original_gui_user = os.environ.get("TEAM_LAUNCHER_GUI_USER")
    original_legacy_gui_user = os.environ.get("PGU_TEAM_LAUNCHER_GUI_USER")
    original_sudo_user = os.environ.get("SUDO_USER")
    try:
        os.environ["TEAM_LAUNCHER_GUI_USER"] = "desktop-user"
        os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
        os.environ["SUDO_USER"] = "eric"
        team_launcher.current_user_name = lambda: "root"

        assert team_launcher.default_gui_user() == "desktop-user"
    finally:
        team_launcher.current_user_name = original_current_user_name
        if original_gui_user is None:
            os.environ.pop("TEAM_LAUNCHER_GUI_USER", None)
        else:
            os.environ["TEAM_LAUNCHER_GUI_USER"] = original_gui_user
        if original_legacy_gui_user is None:
            os.environ.pop("PGU_TEAM_LAUNCHER_GUI_USER", None)
        else:
            os.environ["PGU_TEAM_LAUNCHER_GUI_USER"] = original_legacy_gui_user
        if original_sudo_user is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = original_sudo_user

def test_konsole_launch_falls_back_to_clear_error_when_gui_user_missing() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    try:
        team_launcher.uid_for_user = lambda _user_name: None

        command = konsole_launch_args(Path("/tmp/layout.json"), gui_user="nobody")
        assert command[:2] == ["sh", "-lc"]
        assert "no host Wayland display" in command[2]
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

def test_launch_konsole_window_detaches_real_spawn_and_returns_status_line() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stdout = sys.stdout
    stdout = StringIO()
    popen_calls: list[dict[str, object]] = []
    expected_args: list[str] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            popen_calls.append({"args": args, "kwargs": kwargs})
            self.pid = 424242

        def poll(self) -> int | None:
            return None

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stdout = stdout
        expected_args = konsole_launch_args(Path("/tmp/layout.json"))

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == 0
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stdout = original_stdout
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["args"] == expected_args
    kwargs = call["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert hasattr(kwargs["stdout"], "name")
    assert kwargs["stderr"] is kwargs["stdout"]
    assert "team-launcher: started pgu in background (Konsole pid 424242; log " in stdout.getvalue()

def test_launch_konsole_window_injected_runner_still_detaches_process() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_sleep = team_launcher.time.sleep
    runner_calls: list[list[str]] = []
    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            popen_calls.append({"args": args, "kwargs": kwargs})
            self.pid = 424245

        def poll(self) -> int | None:
            return None

    def blocking_runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        runner_calls.append(args)
        return subprocess.CompletedProcess(args, 99)

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.time.sleep = lambda _seconds: None
        expected_args = konsole_launch_args(Path("/tmp/layout.json"))

        assert (
            launch_konsole_window(
                Path("/tmp/layout.json"),
                project="pgu",
                runner=blocking_runner,
                process_launcher=FakePopen,
            )
            == 0
        )
    finally:
        team_launcher.time.sleep = original_sleep
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert runner_calls == []
    assert len(popen_calls) == 1
    assert popen_calls[0]["args"] == expected_args
    assert popen_calls[0]["kwargs"]["start_new_session"] is True

def test_launch_konsole_window_real_spawn_returns_promptly_in_own_session() -> None:
    original_path = os.environ.get("PATH")
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_stdout = sys.stdout
    stdout = StringIO()
    pid: int | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-konsole-detach.") as tmp:
            tmp_path = Path(tmp)
            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            fake_konsole = fake_bin / "konsole"
            fake_konsole.write_text("#!/bin/sh\nprintf 'fake konsole started\\n'\nsleep 30\n", encoding="utf-8")
            fake_konsole.chmod(0o755)
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path or ''}"
            os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
            sys.stdout = stdout

            start = time.monotonic()
            result = launch_konsole_window(tmp_path / "layout.json", project="pgu")
            elapsed = time.monotonic() - start
            output = stdout.getvalue()
            marker = "Konsole pid "
            pid = int(output.split(marker, 1)[1].split(";", 1)[0])

            assert result == 0
            assert elapsed < 2.0
            assert os.getsid(pid) == pid
            assert os.getpgid(pid) == pid
    finally:
        sys.stdout = original_stdout
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

def test_launch_konsole_window_fallback_error_remains_synchronous_runner() -> None:
    original_uid_for_user = team_launcher.uid_for_user
    original_host_wayland = os.environ.pop("HOST_WAYLAND_DISPLAY", None)
    original_legacy_host_wayland = os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
    runner_calls: list[list[str]] = []
    popen_calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        runner_calls.append(args)
        return subprocess.CompletedProcess(args, 7)

    def process_launcher(args: list[str], **_kwargs: object) -> object:
        popen_calls.append(args)
        raise AssertionError("fallback command should not be backgrounded")

    try:
        team_launcher.uid_for_user = lambda _user_name: None

        assert (
            launch_konsole_window(
                Path("/tmp/layout.json"),
                project="pgu",
                gui_user="nobody",
                runner=runner,
                process_launcher=process_launcher,
            )
            == 7
        )
    finally:
        team_launcher.uid_for_user = original_uid_for_user
        if original_host_wayland is not None:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is not None:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(runner_calls) == 1
    assert runner_calls[0][:2] == ["sh", "-lc"]
    assert popen_calls == []

def test_launch_konsole_window_prints_captured_output_on_immediate_exit() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stderr = sys.stderr
    stderr = StringIO()
    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            popen_calls.append({"args": args, "kwargs": kwargs})
            handle = kwargs["stderr"]
            handle.write(b"qt.qpa.wayland: XDG_RUNTIME_DIR is invalid or not set\n")
            handle.flush()
            self.pid = 424243

        def poll(self) -> int | None:
            return -6

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stderr = stderr

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == -6
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stderr = original_stderr
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert "QT_LOGGING_RULES=qt.qpa.wayland.warning=false" not in call["args"]
    log_path = Path(call["kwargs"]["stdout"].name)
    assert log_path.stat().st_mode & 0o777 == 0o644
    message = stderr.getvalue()
    assert "team-launcher: Konsole exited immediately with status -6; captured output:" in message
    assert "qt.qpa.wayland: XDG_RUNTIME_DIR is invalid or not set" in message
    assert "; see " not in message

def test_launch_konsole_window_names_empty_capture_on_immediate_exit() -> None:
    original_host_wayland = os.environ.get("HOST_WAYLAND_DISPLAY")
    original_legacy_host_wayland = os.environ.get("PGU_HOST_WAYLAND_DISPLAY")
    original_popen = team_launcher.subprocess.Popen
    original_sleep = team_launcher.time.sleep
    original_stderr = sys.stderr
    stderr = StringIO()
    log_paths: list[Path] = []

    class FakePopen:
        def __init__(self, _args: list[str], **kwargs: Any) -> None:
            log_paths.append(Path(kwargs["stdout"].name))
            self.pid = 424244

        def poll(self) -> int | None:
            return -6

    try:
        os.environ["HOST_WAYLAND_DISPLAY"] = "/run/user/1000/wayland-0"
        os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        team_launcher.subprocess.Popen = FakePopen
        team_launcher.time.sleep = lambda _seconds: None
        sys.stderr = stderr

        assert launch_konsole_window(Path("/tmp/layout.json"), project="pgu") == -6
    finally:
        team_launcher.subprocess.Popen = original_popen
        team_launcher.time.sleep = original_sleep
        sys.stderr = original_stderr
        if original_host_wayland is None:
            os.environ.pop("HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["HOST_WAYLAND_DISPLAY"] = original_host_wayland
        if original_legacy_host_wayland is None:
            os.environ.pop("PGU_HOST_WAYLAND_DISPLAY", None)
        else:
            os.environ["PGU_HOST_WAYLAND_DISPLAY"] = original_legacy_host_wayland

    assert len(log_paths) == 1
    assert f"team-launcher: Konsole log {log_paths[0]} was empty" in stderr.getvalue()

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_konsole_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
