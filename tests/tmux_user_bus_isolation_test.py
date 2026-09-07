#!/usr/bin/env python3
"""SYRD-55: a test's tmux panes must not reach a live user systemd manager.

tmux built with systemd support asks the *user* manager for a transient scope
per pane, so a suite that inherits the tenant's `DBUS_SESSION_BUS_ADDRESS`
churns that manager however temporary its tmux sockets are. SYRD-54 found the
tenant's manager wedged after 1,255 such scopes.

This counts the connections rather than trusting the environment: a real
listening socket stands in for the user bus, a real tmux server opens real
panes against it, and the count is the observation. It is the same measurement
in both directions -- with the bus reachable it must be greater than zero, or
this host's tmux has no systemd support and there is nothing to isolate; with
the isolation applied it must be exactly zero, and the panes must still open.
"""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import (
    BUS_ENV_KEYS,
    REPLACED_BUS_ENV,
    dead_bus_address,
    isolate_tmux_bus,
    isolated_bus_environment,
    is_isolated,
)
from tmux_test_invocation_lint_test import bus_isolation_violations, _python_source_paths

isolate_tmux_bus()

PANES = 4


class CountingBus:
    """A socket that answers nothing and remembers who knocked."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connections = 0
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(64)
        self._listener.settimeout(0.25)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            connection.close()

    def __enter__(self) -> "CountingBus":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._listener.close()

    @property
    def address(self) -> str:
        return f"unix:path={self.path}"


def _open_panes(*, environment: dict[str, str], server: str, session: str) -> int:
    """Start a real tmux server of its own and open PANES panes in it.

    `-L` rather than a socket path so the repository's tmux lint sees the
    isolation it asks for; the socket lands under the private XDG_RUNTIME_DIR
    this module is already running with.
    """
    base = ["tmux", "-L", server]
    subprocess.run(
        [*base, "new-session", "-d", "-s", session, "sleep 30"], env=environment, check=True
    )
    try:
        for _ in range(PANES - 1):
            subprocess.run(
                [*base, "split-window", "-t", session, "sleep 30"], env=environment, check=True
            )
        listed = subprocess.run(
            [*base, "list-panes", "-t", session],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return len(listed.stdout.strip().splitlines())
    finally:
        subprocess.run(
            [*base, "kill-session", "-t", session],
            env=environment,
            check=False,
            capture_output=True,
        )
        # The scope requests are made as the panes start; give the last of them
        # time to reach the socket before the count is read.
        time.sleep(0.75)


def test_the_isolated_environment_names_no_user_bus() -> None:
    live = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
        "XDG_RUNTIME_DIR": "/run/user/4242",
        "DBUS_STARTER_ADDRESS": "unix:path=/run/user/4242/bus",
        "DBUS_STARTER_BUS_TYPE": "session",
    }
    isolated = isolated_bus_environment(live)
    assert is_isolated(isolated), isolated
    for key in BUS_ENV_KEYS:
        assert "/run/user/4242" not in isolated.get(key, ""), (key, isolated.get(key))
    assert "DBUS_STARTER_ADDRESS" not in isolated, isolated
    assert "DBUS_STARTER_BUS_TYPE" not in isolated, isolated
    # What it replaced is still findable, for a test that brings its own manager.
    assert isolated[REPLACED_BUS_ENV] == live["DBUS_SESSION_BUS_ADDRESS"]
    # The runtime directory exists -- tmux falls back to it for its own sockets
    # -- and the bus inside it does not.
    runtime = Path(isolated["XDG_RUNTIME_DIR"])
    assert runtime.is_dir(), runtime
    assert not Path(dead_bus_address().split("unix:path=", 1)[1]).exists()
    # Idempotent: applying it again changes nothing.
    assert isolated_bus_environment(isolated) == isolated


def test_this_process_is_already_isolated() -> None:
    """Importing the helper is what protects a suite run directly, not the runner."""
    assert is_isolated(dict(os.environ)), os.environ.get("DBUS_SESSION_BUS_ADDRESS")


def test_the_suite_runner_isolates_every_suite() -> None:
    """A broad run gets this without each caller remembering to."""
    from ticket_board_suite_runner_test import load_runner

    environment = load_runner().sanitized_environment(
        {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/4242/bus",
            "XDG_RUNTIME_DIR": "/run/user/4242",
        }
    )
    assert is_isolated(environment), environment["DBUS_SESSION_BUS_ADDRESS"]
    assert environment["XDG_RUNTIME_DIR"] != "/run/user/4242"


def test_every_test_that_runs_tmux_isolates_the_user_bus() -> None:
    """The rule, applied to the repository, so a new suite cannot skip it."""
    violations = []
    for path in _python_source_paths(ROOT / "tests"):
        violations.extend(bus_isolation_violations(path.read_text(encoding="utf-8"), path))
    assert violations == [], "\n".join(
        f"{violation.path}:{violation.line}: {violation.detail}" for violation in violations
    )


def test_the_real_tmux_shell_suite_isolates_the_user_bus() -> None:
    """The one shell suite that starts a real tmux server, which the lint cannot read."""
    script = ROOT / "tests" / "ticket_board_directorctl_copy_mode_real_tmux_test.sh"
    text = script.read_text(encoding="utf-8")
    assert "tmux_bus_isolation.py" in text, script
    assert "--export" in text, script


def counts_connections_to_a_stand_in_user_bus() -> None:
    """The measurement itself, with a real tmux server and real panes."""
    if not _tmux_available():
        print("tmux_user_bus_isolation_test: skipped (tmux not installed)")
        return
    with tempfile.TemporaryDirectory(prefix="tmux-user-bus.") as tmp:
        root = Path(tmp)
        with CountingBus(root / "stand-in-bus") as bus:
            reachable = dict(os.environ)
            reachable["DBUS_SESSION_BUS_ADDRESS"] = bus.address
            reachable.pop(REPLACED_BUS_ENV, None)
            panes = _open_panes(
                environment=reachable,
                server=f"syrd55-reachable-{os.getpid()}",
                session="reachable",
            )
            assert panes == PANES, panes
            reached = bus.connections
            if reached == 0:
                # No systemd support in this tmux: there is no transient scope
                # to keep off anything, and the isolation costs it nothing.
                print(
                    "tmux_user_bus_isolation_test: coverage reduced: "
                    "this tmux makes no user-bus call per pane"
                )
            else:
                assert reached >= PANES, (reached, PANES)

            before = bus.connections
            isolated = isolated_bus_environment(reachable)
            panes = _open_panes(
                environment=isolated,
                server=f"syrd55-isolated-{os.getpid()}",
                session="isolated",
            )
            assert panes == PANES, panes
            assert bus.connections == before, (bus.connections, before)


def _tmux_available() -> bool:
    try:
        # -L even here: the lint asks every tmux argv in tests/ for an explicit
        # server, and a version probe is no exception to that habit.
        subprocess.run(["tmux", "-L", "syrd55-version-probe", "-V"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def main() -> int:
    test_the_isolated_environment_names_no_user_bus()
    test_this_process_is_already_isolated()
    test_the_suite_runner_isolates_every_suite()
    test_every_test_that_runs_tmux_isolates_the_user_bus()
    test_the_real_tmux_shell_suite_isolates_the_user_bus()
    counts_connections_to_a_stand_in_user_bus()
    print("tmux_user_bus_isolation_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
