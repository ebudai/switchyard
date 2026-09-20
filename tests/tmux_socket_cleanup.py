"""Helpers for tests that create isolated tmux servers."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmux_bus_isolation import isolate_tmux_bus

# An isolated socket is not isolation: without this the panes on that socket
# still create transient scopes in the live tenant's user manager (SYRD-55).
isolate_tmux_bus()


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


# --- A server a test owns, and can prove it owns before killing it ---------
#
# `TMUX_TMPDIR` is not isolation. Measured from inside a live role pane, with
# `TMUX` set: a bare `tmux` and a `TMUX_TMPDIR`-only `tmux` both resolve to the
# pane's own server, while `-L` and `-S` resolve where they say. A suite that
# isolated itself with the directory alone therefore built its sessions on the
# caller's server and ended by killing it -- which disconnected a live desktop
# twice during SYRD-216, and is what SYRD-219 exists to make impossible.
#
# So the socket is named explicitly and passed on every command, the inherited
# pane variables are dropped as well, and the teardown proves which server it
# is about to stop before stopping it.

#: Dropped rather than overridden: `TMUX` names a socket and wins over
#: `TMUX_TMPDIR`, and `TMUX_PANE` only makes sense beside it.
INHERITED_PANE_VARS = ("TMUX", "TMUX_PANE")


def private_tmux_socket(tmpdir: Path) -> Path:
    """Where a private server's socket lives, under a directory the test owns.

    Deliberately the exact path `TMUX_TMPDIR` would derive, rather than a name
    of our own. The fixture pins the socket with `-S`, but the code under test
    re-invokes tmux for itself -- a pane's attach command, a launcher's own
    calls -- carrying no socket flag and finding the server through the
    directory. Pick a different path for the two and they are two servers: the
    fixture creates a session the code under test cannot see, and the symptom
    is "there is nothing to attach to" rather than anything about sockets.
    """
    directory = Path(tmpdir).resolve() / f"tmux-{os.getuid()}"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / "default"


def private_tmux_env(tmpdir: Path, **extra: str) -> dict[str, str]:
    """An environment that cannot reach the caller's tmux server.

    `TMUX_TMPDIR` is set as well as the socket being named, because commands
    the code under test starts for itself -- a pane's own `tmux attach`, a
    resize hook's helper -- carry no socket flag and only the directory
    reaches them.
    """
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_PANE_VARS}
    env["TMUX_TMPDIR"] = str(Path(tmpdir).resolve())
    env.update(extra)
    return env


def private_tmux_args(socket: Path, args: list[str]) -> list[str]:
    return ["tmux", "-S", str(socket), *args]


def socket_is_private(socket: Path, tmpdir: Path) -> bool:
    """Containment by path components, not by string prefix.

    `/tmp/fixture.ab` is not inside `/tmp/fixture.a`, however the two read as
    strings.
    """
    try:
        return Path(socket).resolve().is_relative_to(Path(tmpdir).resolve())
    except (OSError, ValueError):
        return False


def assert_private_tmux_socket(socket: Path, tmpdir: Path) -> None:
    """Refuse before touching anything, rather than after.

    Proving provenance only after a session has been created is proving it too
    late: a redirected fixture has already mutated somebody else's server by
    then.
    """
    if not socket_is_private(socket, tmpdir):
        raise AssertionError(
            f"refusing to drive tmux at {socket}: it is outside {tmpdir}, so this "
            "fixture would be operating on a server it does not own -- and its "
            "teardown stops servers"
        )


def kill_private_tmux_server(socket: Path, tmpdir: Path, env: dict[str, str] | None = None) -> bool:
    """Stop a server this fixture owns, and refuse to stop one it does not.

    Returns whether a shutdown was actually issued, so a caller can assert
    that none was.
    """
    assert_private_tmux_socket(socket, tmpdir)
    if not Path(socket).exists():
        return False        # nothing running: nothing to stop, and no guess
    subprocess.run(
        private_tmux_args(Path(socket), ["kill-server"]),
        env=env if env is not None else private_tmux_env(tmpdir),
        capture_output=True,
        text=True,
    )
    return True
