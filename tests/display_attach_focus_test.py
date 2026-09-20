#!/usr/bin/env python3
"""SYRD-211 live UAT: the viewer opened, and focus was trapped in one pane.

The display attach deliberately makes its client input-only: both prefixes are
removed and the session is pointed at a key table with no bindings, so every
key falls through to the pane and none of them reaches tmux. That is what stops
a desktop user getting a shell in the owner's account, a tmux command prompt,
or another tenant's session through this grant.

It also meant they could not leave whichever pane happened to be active. On
Zorin the viewer came up focused on slot 4, so the first-run prompts waiting in
Designer, Director, Audit and Main could not be answered at all.

Focus is not a capability. The table now holds exactly five bindings, all of
which move focus and nothing else, and everything else still falls through.

These cases drive a REAL tmux server with a REAL attached client on a pty,
because key tables are not exercised by `send-keys`: that writes to the pane's
terminal and never consults a binding. A test built on `send-keys` would have
reported this fixed while it was not.
"""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

# An isolated socket is not isolation on its own: without this the panes opened
# on it still ask the live tenant's user manager for transient scopes, however
# private the socket is (SYRD-55, SYRD-219).
isolate_tmux_bus()

HELPER = ROOT / "scripts" / "switchyard-display-attach"

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def _helper():
    module = types.ModuleType("display_attach_under_test")
    module.__dict__["__name__"] = "display_attach_under_test"
    exec(compile(HELPER.read_text(encoding="utf-8"), str(HELPER), "exec"), module.__dict__)  # noqa: S102
    return module


class _Server:
    """A private tmux server holding one viewer-shaped session."""

    def __init__(self, panes: int = 4) -> None:
        self.name = os.path.basename(tempfile.mktemp(prefix="syrd211-focus."))
        self.session = "test-viewer"
        self.sink = tempfile.mkdtemp(prefix="syrd211-panes.")
        self.tmux("new-session", "-d", "-s", self.session, "-x", "100", "-y", "30",
                  f"cat > {self.sink}/p0")
        for index in range(1, panes):
            self.tmux("split-window", "-t", f"{self.session}:0",
                      f"cat > {self.sink}/p{index}")
        self.tmux("select-layout", "-t", f"{self.session}:0", "tiled")

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.name, *args], capture_output=True, text=True
        )

    def ask(self, spec: str) -> str:
        return self.tmux(
            "display-message", "-p", "-t", f"{self.session}:0", spec
        ).stdout.strip()

    def active_pane(self) -> str:
        return self.ask("#{pane_index}")

    def close(self) -> None:
        self.tmux("kill-server")
        shutil.rmtree(self.sink, ignore_errors=True)


class _Client:
    """A real attached client, on a real terminal, so key tables apply."""

    def __init__(self, server: _Server) -> None:
        self._master, slave = pty.openpty()
        self._proc = subprocess.Popen(
            ["tmux", "-L", server.name, "attach", "-t", server.session],
            stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        time.sleep(1.0)

    def press(self, sequence: bytes, settle: float = 0.5) -> None:
        os.write(self._master, sequence)
        time.sleep(settle)

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        try:
            os.close(self._master)
        except OSError:
            pass


#: What a terminal sends for the keys the table binds.
ALT_DOWN = b"\x1b[1;3B"
ALT_UP = b"\x1b[1;3A"
ALT_LEFT = b"\x1b[1;3D"
ALT_RIGHT = b"\x1b[1;3C"


def _unavailable() -> str:
    if shutil.which("tmux") is None:
        return "tmux is not installed"
    return ""


def test_the_locked_table_holds_focus_bindings_and_nothing_else() -> None:
    """Read from a real tmux, because the table is real state."""
    unavailable = _unavailable()
    if unavailable:
        print(f"  (skipped: {unavailable})")
        return
    helper = _helper()
    server = _Server()
    try:
        result = server.tmux(*helper.lock_argv(server.session)[1:])
        check(result.returncode == 0, f"the lock applies: {result.stderr.strip()[:120]}")
        listed = server.tmux(
            "list-keys", "-T", helper.DISPLAY_KEY_TABLE
        ).stdout.strip().splitlines()
        check(len(listed) == len(helper.FOCUS_BINDINGS),
              f"exactly the bindings this release grants: {listed}")
        for line in listed:
            check("select-pane" in line,
                  f"every one of them only moves focus: {line.strip()}")
        for forbidden in ("new-window", "split-window", "command-prompt",
                          "choose-tree", "switch-client", "kill-pane", "send-prefix"):
            check(not any(forbidden in line for line in listed),
                  f"and none of them is {forbidden}: {listed}")
        check(server.ask("#{prefix}") in ("", "None", "none"),
              f"the prefix is still gone: {server.ask('#{prefix}')!r}")
    finally:
        server.close()


def test_a_stale_table_from_an_older_release_is_cleared_first() -> None:
    """This program is the boundary, so it applies the whole of it every time."""
    unavailable = _unavailable()
    if unavailable:
        print(f"  (skipped: {unavailable})")
        return
    helper = _helper()
    server = _Server()
    try:
        # Something a previous release might have left behind.
        server.tmux("bind-key", "-T", helper.DISPLAY_KEY_TABLE, "C-b", "send-prefix")
        server.tmux("bind-key", "-T", helper.DISPLAY_KEY_TABLE, "X", "kill-pane")
        server.tmux(*helper.lock_argv(server.session)[1:])
        listed = server.tmux(
            "list-keys", "-T", helper.DISPLAY_KEY_TABLE
        ).stdout.strip().splitlines()
        check(not any("send-prefix" in line for line in listed),
              f"a stale prefix binding is gone: {listed}")
        check(not any("kill-pane" in line for line in listed),
              f"and so is a stale kill: {listed}")
        check(len(listed) == len(helper.FOCUS_BINDINGS),
              f"leaving only what this release grants: {listed}")
    finally:
        server.close()


def test_a_desktop_user_can_focus_and_type_into_every_pane() -> None:
    """The failure, as the User met it: reach every role, and answer it."""
    unavailable = _unavailable()
    if unavailable:
        print(f"  (skipped: {unavailable})")
        return
    helper = _helper()
    server = _Server(panes=4)
    client = None
    try:
        server.tmux(*helper.lock_argv(server.session)[1:])
        server.tmux("select-pane", "-t", f"{server.session}:0.0")
        client = _Client(server)
        check(server.active_pane() == "0", f"it starts somewhere: {server.active_pane()}")

        visited = {server.active_pane()}
        for sequence in (ALT_DOWN, ALT_RIGHT, ALT_UP, ALT_LEFT, ALT_DOWN, ALT_RIGHT):
            client.press(sequence)
            visited.add(server.active_pane())
        check(len(visited) >= 4,
              f"focus reaches every pane, not just the one it started on: {sorted(visited)}")

        # And what is typed goes to the pane that has focus, not to tmux.
        typed_into: set[str] = set()
        for sequence in (ALT_UP, ALT_DOWN, ALT_LEFT, ALT_RIGHT):
            client.press(sequence, settle=0.3)
            here = server.active_pane()
            if here in typed_into:
                continue
            client.press(b"hello\r", settle=0.5)
            typed_into.add(here)
        landed = {
            name for name in os.listdir(server.sink)
            if (Path(server.sink) / name).read_text(errors="replace").strip() == "hello"
        }
        check(len(landed) >= 2,
              f"typing reaches the focused pane, in more than one of them: {landed}")
    finally:
        if client is not None:
            client.close()
        server.close()


def test_the_forbidden_controls_are_still_unavailable() -> None:
    """Pressed by a real client: none of them may reach tmux."""
    unavailable = _unavailable()
    if unavailable:
        print(f"  (skipped: {unavailable})")
        return
    helper = _helper()
    server = _Server()
    client = None
    try:
        server.tmux(*helper.lock_argv(server.session)[1:])
        client = _Client(server)
        before = (server.ask("#{session_windows}"), server.ask("#{window_panes}"))
        sessions_before = server.tmux("list-sessions", "-F", "#{session_name}").stdout

        # A new window, three kinds of split, the command prompt, both choosers,
        # a kill, and the classic prefixes.
        for key in (b"c", b"%", b'"', b":", b"s", b"w", b"x", b"&",
                    b"\x02", b"\x01", b"\x1b:"):
            client.press(key, settle=0.12)
        time.sleep(0.6)

        after = (server.ask("#{session_windows}"), server.ask("#{window_panes}"))
        check(before == after,
              f"no window or pane was created or destroyed: {before} -> {after}")
        check(server.tmux("list-sessions", "-F", "#{session_name}").stdout == sessions_before,
              "and no session was reached or made")
        check(server.ask("#{pane_in_mode}") == "0",
              "nothing entered a tmux mode")
    finally:
        if client is not None:
            client.close()
        server.close()


def test_the_lock_still_refuses_a_session_that_is_not_there() -> None:
    """The lock doubles as proof the slot was bootstrapped; it must keep doing so."""
    unavailable = _unavailable()
    if unavailable:
        print(f"  (skipped: {unavailable})")
        return
    helper = _helper()
    server = _Server()
    try:
        result = server.tmux(*helper.lock_argv("test-not-bootstrapped")[1:])
        check(result.returncode != 0,
              "a session that does not exist is refused rather than created")
        check(not server.tmux("has-session", "-t", "=test-not-bootstrapped").returncode == 0,
              "and nothing was made for it")
    finally:
        server.close()


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if not (name.startswith("test_") and callable(value)):
            continue
        try:
            value()
        except BaseException as exc:  # noqa: BLE001
            failures += 1
            print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"display_attach_focus_test: {failures} failed")
        return 1
    print(f"display_attach_focus_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
