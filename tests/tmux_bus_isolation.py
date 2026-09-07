"""Keep disposable tmux panes off a live user manager's bus.

tmux built with systemd support asks the *user* systemd manager to put every
pane in its own transient scope, so systemd-oomd can isolate it:

    org.freedesktop.systemd1.Manager.StartTransientUnit
    tmux-spawn-<uuid>.scope    "Started tmux child pane"

That is correct in production and is not weakened here. It is not correct in a
test process, which inherits the live tenant's `DBUS_SESSION_BUS_ADDRESS` and
`XDG_RUNTIME_DIR` and therefore aims those calls at the tenant's own manager
however temporary its tmux session names and sockets are. A broad suite creates
hundreds of panes in minutes; SYRD-54 found that manager wedged in a userspace
spin after 1,255 such scopes, 161 of them in the nine minutes of two sweeps.

The fix is to fail closed rather than to unset: libdbus autolaunches a bus when
`DBUS_SESSION_BUS_ADDRESS` is absent, and `sd_bus_open_user` falls back to
`$XDG_RUNTIME_DIR/bus`. So both are pointed inside a private directory at a
socket that is never created. tmux reports `StartTransientUnit call failed` at
its own debug level and starts the pane anyway.

Version boundary, measured on this host: tmux 3.7c linked against
libsystemd.so.0 makes one such call per pane -- four panes, four connections.
A tmux without systemd support makes none, and then this changes nothing at
all: an unreachable bus address it never opens costs it nothing. The isolation
is therefore portable and needs no version test.

A test that genuinely needs a user manager must bring its own -- a fake, or a
disposable manager it starts and stops itself -- and can read what was replaced
through `replaced_bus_address()`.
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

#: What a process must not inherit before it starts a tmux pane.
BUS_ENV_KEYS = (
    "DBUS_SESSION_BUS_ADDRESS",
    "DBUS_STARTER_ADDRESS",
    "DBUS_STARTER_BUS_TYPE",
    "XDG_RUNTIME_DIR",
)

#: Set to the address this isolation replaced, so an explicit user-manager test
#: can still find the one it was given.
REPLACED_BUS_ENV = "SWITCHYARD_TEST_REPLACED_BUS_ADDRESS"

_ISOLATION_DIR_PREFIX = "switchyard-tmux-bus-isolation"
_DEAD_BUS_NAME = "no-user-bus"


def isolation_dir(*, base: Path | None = None) -> Path:
    """A private directory holding the runtime dir and the address that is not there."""
    root = base if base is not None else Path(tempfile.gettempdir())
    return root / f"{_ISOLATION_DIR_PREFIX}-{os.getuid()}"


def dead_bus_address(*, base: Path | None = None) -> str:
    """A well-formed address whose socket is deliberately never created."""
    return f"unix:path={isolation_dir(base=base) / _DEAD_BUS_NAME}"


def is_isolated(environ: dict[str, str], *, base: Path | None = None) -> bool:
    return environ.get("DBUS_SESSION_BUS_ADDRESS", "") == dead_bus_address(base=base)


def isolated_bus_environment(
    environ: dict[str, str] | None = None, *, base: Path | None = None
) -> dict[str, str]:
    """The same environment, with no route to any user bus.

    The runtime directory really is created: tmux and others fall back to it for
    their own sockets, and one that does not exist produces failures that have
    nothing to do with what is being tested. The bus socket inside it is not.
    """
    source = dict(os.environ if environ is None else environ)
    if is_isolated(source, base=base):
        return source
    directory = isolation_dir(base=base)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    replaced = source.get("DBUS_SESSION_BUS_ADDRESS", "")
    if replaced:
        source[REPLACED_BUS_ENV] = replaced
    source["DBUS_SESSION_BUS_ADDRESS"] = dead_bus_address(base=base)
    source["XDG_RUNTIME_DIR"] = str(directory)
    source.pop("DBUS_STARTER_ADDRESS", None)
    source.pop("DBUS_STARTER_BUS_TYPE", None)
    return source


def isolate_tmux_bus(*, base: Path | None = None) -> dict[str, str]:
    """Apply it to this process, so everything it spawns inherits it.

    Idempotent, and safe to call from a module that may or may not reach tmux:
    a process that never opens a bus is not affected by the address it was
    given.
    """
    isolated = isolated_bus_environment(dict(os.environ), base=base)
    for key in (*BUS_ENV_KEYS, REPLACED_BUS_ENV):
        if key in isolated:
            os.environ[key] = isolated[key]
        else:
            os.environ.pop(key, None)
    return isolated


def replaced_bus_address() -> str:
    """The user bus this process was given before it was isolated, if any."""
    return os.environ.get(REPLACED_BUS_ENV, "")


def shell_exports(*, base: Path | None = None) -> str:
    """The same isolation, for a shell test to `eval`.

    Single-sourced deliberately: a second copy of the path rule in shell is a
    copy that drifts.
    """
    isolated = isolated_bus_environment(dict(os.environ), base=base)
    lines = []
    for key in BUS_ENV_KEYS:
        if key in isolated:
            lines.append(f"export {key}={shlex.quote(isolated[key])}")
        else:
            lines.append(f"unset {key}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - exercised through the shell test
    import sys as _sys

    if _sys.argv[1:2] == ["--export"]:
        print(shell_exports())
    else:
        raise SystemExit("usage: tmux_bus_isolation.py --export")
