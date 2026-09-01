#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

def test_cleanup_isolated_tmux_sessions_removes_dead_reload_guard_socket() -> None:
    if shutil.which("tmux") is None:
        return
    server = f"pgu321-reload-guard-{os.getpid()}"
    session = f"pgu-791-reload-guard-cleanup-{os.getpid()}"
    socket_path = _isolated_tmux_socket_path(server)
    _run_isolated_tmux(
        server,
        ["new-session", "-d", "-s", session, "sleep", "60"],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert socket_path.exists(), socket_path
    _cleanup_isolated_tmux_sessions(server, [session])
    assert not socket_path.exists(), socket_path

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_tmux_cleanup_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
