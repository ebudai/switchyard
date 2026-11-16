"""Helpers for tests that create isolated tmux servers.

A server a test starts is the test's to end, socket file included: tmux never removes its socket, not when the last
session ends and not on kill-server (measured, tmux 3.8, SYRD-823), so every run that only ended its sessions left one
behind in the account's tmux directory -- the directory the live role sessions' own sockets are in. A test registers
each server it starts (`own_isolated_tmux_server`; `run_isolated_tmux` does it for a new-session or start-server), and
every registered server still there when the process exits -- normally, by an exception, or by SIGTERM, as a runner's
timeout stops it -- is killed and its socket removed.
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import threading
import time
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
    if "new-session" in args or "start-server" in args:
        own_isolated_tmux_server(server, kwargs.get("env"))
    return subprocess.run(tmux_args(server, args), **kwargs)


def tmux_socket_directory(env: dict[str, str] | None = None) -> Path:
    """tmux's own rule, measured (SYRD-823): $TMUX_TMPDIR when it names an existing absolute directory, else /tmp --
    never XDG_RUNTIME_DIR, and never $TMPDIR, which tempfile follows (SYRD-809: under a disposable TMPDIR this looked
    in the wrong directory, and the cleanup missed the socket it was asked to remove)."""
    base = (os.environ if env is None else env).get("TMUX_TMPDIR") or ""
    if not (os.path.isabs(base) and os.path.isdir(base)):
        base = "/tmp"
    return Path(base) / f"tmux-{os.getuid()}"


def isolated_tmux_socket_path(server: str, env: dict[str, str] | None = None) -> Path:
    return tmux_socket_directory(env) / server


def socket_is_bound(path: Path) -> bool:
    """Whether a process holds a socket bound at `path`: the kernel's own list, so a server that does not answer
    (busy, or not tmux) still counts as alive, and its socket is never removed under it."""
    try:
        listed = Path("/proc/net/unix").read_text(encoding="utf-8", errors="replace").splitlines()[1:]
    except OSError:
        return True  # cannot tell: treat it as held
    target = os.fsdecode(os.fsencode(str(path)))
    return any(line.split(None, 7)[7:] == [target] for line in listed)


def _server_answers(server: str, env: dict[str, str] | None) -> bool:
    try:
        probe = run_isolated_tmux(server, ["list-sessions"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5, check=False, **({"env": env} if env is not None else {}))
    except (OSError, subprocess.TimeoutExpired):
        return True
    return probe.returncode == 0


def end_isolated_tmux_server(server: str, env: dict[str, str] | None = None) -> bool:
    """Kill this test's own server if it still runs, wait for it to go, then remove its socket; whether it is gone.

    Only a server this process registered (`own_isolated_tmux_server`) is ended, and only a socket no process holds is
    removed. Looked for under the environment the server was started with and under this process's own, which differ
    when a test hands tmux an environment of its own."""
    _assert_owned(server)
    kwargs = {"env": env} if env is not None else {}
    try:
        run_isolated_tmux(server, ["kill-server"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=5, check=False, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        pass
    paths = {isolated_tmux_socket_path(server, env), isolated_tmux_socket_path(server)}
    deadline = time.monotonic() + 3
    while any(socket_is_bound(path) for path in paths) and time.monotonic() < deadline:
        time.sleep(0.05)
    gone = True
    for path in paths:
        if socket_is_bound(path):
            gone = False
        elif path.is_socket():
            path.unlink(missing_ok=True)
    return gone  # it stays registered: ending it again, or at exit, finds nothing and does nothing


#: Servers this process started and has not ended yet: name -> the environment it was started with (None: ours).
_OWNED: dict[str, dict[str, str] | None] = {}


#: The server the account's live sessions run on, beside every isolated socket: never a test's to end.
LIVE_SERVER = "default"


def _assert_owned(server: str) -> None:
    """The rule `kill_private_tmux_server` keeps (a test once disconnected a live desktop): end only what is ours."""
    if server == LIVE_SERVER or server not in _OWNED:
        raise AssertionError(f"refusing to end tmux server {server!r}: this process never registered it "
                             f"(own_isolated_tmux_server), and {LIVE_SERVER!r} is the live sessions' server")


def own_isolated_tmux_server(server: str, env: dict[str, str] | None = None) -> str:
    """Register a server this test starts, so it is ended, socket and all, however the process exits. Returns it."""
    if not server or server == LIVE_SERVER:
        raise AssertionError(f"a test's tmux server needs a name of its own, not {server!r}")
    if not _OWNED:
        _arm_exit_cleanup()
    _OWNED.setdefault(server, dict(env) if env is not None else None)
    return server


def end_owned_isolated_tmux_servers() -> None:
    for server, env in list(_OWNED.items()):
        end_isolated_tmux_server(server, env)


_ARMED = False


def _arm_exit_cleanup() -> None:
    global _ARMED
    if _ARMED:
        return
    _ARMED = True
    atexit.register(end_owned_isolated_tmux_servers)
    # A runner's timeout sends SIGTERM, whose default ends the process without atexit or any `finally`. Turned into
    # an ordinary exit -- only where nobody chose a handler already, and only on the main thread, where one can be set.
    if threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
        signal.signal(signal.SIGTERM, _exit_on_sigterm)


def _exit_on_sigterm(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def cleanup_dead_isolated_tmux_socket(server: str) -> None:
    """Remove a left-over socket of this name whose server is gone; a live server, and its socket, are left alone."""
    socket_path = isolated_tmux_socket_path(server)
    if not socket_path.is_socket() or _server_answers(server, None):
        return
    deadline = time.monotonic() + 3  # a server whose last session just ended is still letting go of its socket
    while socket_is_bound(socket_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    if not socket_is_bound(socket_path):
        socket_path.unlink(missing_ok=True)


def cleanup_isolated_tmux_sessions(server: str, sessions: list[str]) -> None:
    """End a test's own server: its sessions, then the server itself and its socket. The server may hold sessions the
    code under test made beyond `sessions`; while it ran, its socket stayed, and outlived it (SYRD-823)."""
    _assert_owned(server)
    for session in sessions:
        run_isolated_tmux(server, ["kill-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end_isolated_tmux_server(server, _OWNED.get(server))


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
