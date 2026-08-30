"""Helpers for tests that create isolated tmux servers."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def tmux_args(server: str, args: list[str]) -> list[str]:
    return ["tmux", "-L", server, *args]


def run_isolated_tmux(server: str, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(tmux_args(server, args), **kwargs)


def isolated_tmux_socket_path(server: str) -> Path:
    return Path(os.environ.get("TMUX_TMPDIR", tempfile.gettempdir())) / f"tmux-{os.getuid()}" / server


def cleanup_dead_isolated_tmux_socket(server: str) -> None:
    socket_path = isolated_tmux_socket_path(server)
    if not socket_path.exists():
        return
    try:
        probe = run_isolated_tmux(
            server,
            ["list-sessions"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if probe.returncode == 0:
        return
    socket_path.unlink(missing_ok=True)


def cleanup_isolated_tmux_sessions(server: str, sessions: list[str]) -> None:
    for session in sessions:
        run_isolated_tmux(server, ["kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cleanup_dead_isolated_tmux_socket(server)
