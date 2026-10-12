#!/usr/bin/env python3
"""SYRD-389: the mouse wheel reaches a role's pane through its display slot.

Live syrd Audit, switched to Codex, could not scroll in its presentation tab.
The Director's live repair touched two layers:

- **The outer display client** is locked by `switchyard-display-attach` to the
  `switchyard-display` key table, which had click and modified-arrow focus and
  no wheel binding. A live binding was lost at the next attach, because every
  attach rebuilds the table.
- **The inner role session**, which the slot's pane attaches to as a nested
  client, had inherited the server's `mouse off`. A detached start sets it, but
  a session made by `attach-role` or an older release does not.

The user's UAT passed only once the inner mouse was on. Measured here on tmux
3.7c, that is the layer that decides it: the nested client asks for the mouse
only when its session's mouse is on, and an unbound wheel in the display table
already reaches a pane that asked. So configuring a slot for a live role now
turns that role session's mouse on, and that is what the real path below needs.
The helper also binds `WheelUpPane`/`WheelDownPane` to `send-keys -M`, stating
the forwarding in the table rather than leaving it to that fallback, and never
copy mode in the outer session.

The real path is driven with a REAL tmux server on a private socket, a real
nested client in the slot's pane, and a real outer client on a pty receiving a
terminal's wheel sequence. `send-keys` alone would not consult a key table.
`TMUX` is stripped, the server reads no config, and the bus is isolated.
"""

from __future__ import annotations

import json
import os
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)

from scripts import presentation_display_session as owner  # noqa: E402
from scripts import team_launcher  # noqa: E402

HELPER = ROOT / "scripts" / "switchyard-display-attach"
CHECKS = 0
WHEEL_UP = b"\x1b[<64;10;5M"
WHEEL_DOWN = b"\x1b[<65;10;5M"


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


def _config(root: Path):
    path = root / "sample.json"
    path.write_text(json.dumps({
        "project": "sample", "session_dir": str(root / "sessions"),
        "roles": [{"role": "director", "slot": 0, "cli": "codex", "workdir": str(root)}],
    }), encoding="utf-8")
    return team_launcher.load_project_config("sample", path)


class _Recorder:
    """The owner's tmux, recorded: the director's session is live, the slot is new."""

    def __init__(self, *, live: bool = True) -> None:
        self.live = live
        self.calls: list[list[str]] = []

    def __call__(self, args, **_kwargs):
        argv = list(args)
        self.calls.append(argv)
        if argv[1] == "has-session":
            found = self.live and argv[argv.index("-t") + 1] == "=sample-director"
            return subprocess.CompletedProcess(argv, 0 if found else 1, "", "")
        if argv[1] == "display-message":
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def _slot_calls(*, live: bool = True, role: str | None = "director") -> list[list[str]]:
    with tempfile.TemporaryDirectory(prefix="syrd389-cfg.") as raw:
        recorder = _Recorder(live=live)
        owner._configure_display_session(_config(Path(raw)), 0, role, runner=recorder, presentation_ttys=set())
    return recorder.calls


MOUSE_ON = ["tmux", "set-option", "-t", "=sample-director:", "mouse", "on"]


def test_the_display_table_hands_the_wheel_to_the_pane() -> None:
    helper = _helper()
    argv = helper.lock_argv("sample-display-0")
    parts = " ".join(argv[1:]).split(" ; ")
    clear = parts.index(f"unbind-key -a -T {helper.DISPLAY_KEY_TABLE}")
    bound = parts[clear + 1:]
    check(f"bind-key -T switchyard-display WheelUpPane send-keys -M" in bound
          and f"bind-key -T switchyard-display WheelDownPane send-keys -M" in bound,
          f"after the table is cleared, both wheel directions are bound to send-keys -M: {bound}")
    check(len(bound) == len(helper.FOCUS_BINDINGS) + 2
          and all(line.startswith("bind-key -T switchyard-display M") for line in bound[:len(helper.FOCUS_BINDINGS)]),
          f"beside exactly the focus bindings, which are unchanged: {bound}")
    check("set-option -t =sample-display-0: mouse on" in parts and "set-option -t =sample-display-0: prefix None" in parts,
          f"and the client is still locked, with the mouse on: {parts}")


def test_a_slot_turns_on_its_live_roles_mouse() -> None:
    calls = _slot_calls()
    check(MOUSE_ON in calls, f"configuring a slot for a live role turns that role session's mouse on: {calls}")
    mouse_at = calls.index(MOUSE_ON)
    detach_at = next(i for i, c in enumerate(calls) if c[-2:] == ["detach-on-destroy", "on"])
    create_at = next(i for i, c in enumerate(calls) if c[:2] == ["tmux", "new-session"])
    check(detach_at < mouse_at < create_at, f"before the slot's client can attach to it: {detach_at} {mouse_at} {create_at}")
    check(not any(c[-2:] == ["mouse", "on"] and "=sample-display-0:" in c for c in calls),
          "the display session's own mouse is the attach helper's to set, not this")
    for live, role in ((False, "director"), (True, None)):
        quiet = _slot_calls(live=live, role=role)
        check(not any(c[-2:] == ["mouse", "on"] for c in quiet),
              f"a slot with no live role touches no role session's mouse ({live}, {role}): {quiet}")


def test_a_failed_mouse_setting_stops_the_slot() -> None:
    class Failing(_Recorder):
        def __call__(self, args, **kwargs):
            if list(args)[-2:] == ["mouse", "on"]:
                self.calls.append(list(args))
                return subprocess.CompletedProcess(list(args), 5, "", "")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="syrd389-fail.") as raw:
        recorder = Failing()
        try:
            owner._configure_display_session(_config(Path(raw)), 0, "director", runner=recorder, presentation_ttys=set())
            raised = ""
        except RuntimeError as exc:
            raised = str(exc)
    check(raised == "tmux could not turn on the mouse for director (exit 5)", f"it is reported: {raised!r}")
    check(not any(c[:2] == ["tmux", "new-session"] for c in recorder.calls), "and no slot is made over it")


# --- the real path -------------------------------------------------------------------------------------------------

RECORDER = r"""
import os, sys, tty
log = sys.argv[1]
tty.setraw(0)
os.write(1, b"\x1b[?1000h\x1b[?1006h")
with open(log, "ab", buffering=0) as out:
    while True:
        data = os.read(0, 256)
        if not data:
            break
        out.write(data)
"""


class _Server:
    """A private tmux server holding a role session and its display slot, as the owner builds them."""

    def __init__(self, inner: str, *, inner_mouse: bool) -> None:
        """`inner_mouse`: configure the role session as the slot does; otherwise leave the server's default."""
        self.name = os.path.basename(tempfile.mktemp(prefix="syrd389-"))
        self.dir = Path(tempfile.mkdtemp(prefix="syrd389-wheel."))
        self.log = self.dir / "received"
        self.log.write_bytes(b"")
        program = {
            "mouse": shlex.join([sys.executable, "-c", RECORDER, str(self.log)]),
            "plain": "seq 1 300; exec cat > /dev/null",
        }[inner]
        self.tmux("new-session", "-d", "-s", "sample-director", "-x", "80", "-y", "24", program)
        if inner_mouse:
            # Exactly what configuring the slot sends to the role session, so
            # this path stands or falls with the product, not with this test.
            for argv in (c for c in _slot_calls() if c[-2:] == ["mouse", "on"]):
                self.tmux(*argv[1:])
        nested = shlex.join(["env", "TMUX=", "tmux", "-L", self.name, "-f", "/dev/null",
                             "attach", "-f", "!no-detach-on-destroy", "-t", "=sample-director"])
        self.tmux("new-session", "-d", "-s", "sample-display-0", "-x", "80", "-y", "24", nested)
        time.sleep(0.6)

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "-L", self.name, "-f", "/dev/null", *args], capture_output=True, text=True)

    def ask(self, target: str, spec: str) -> str:
        return self.tmux("display-message", "-p", "-t", target, spec).stdout.strip()

    def close(self) -> None:
        self.tmux("kill-server")
        shutil.rmtree(self.dir, ignore_errors=True)


class _Client:
    """A real outer client on a real terminal, so the display table applies to what it sends."""

    def __init__(self, server: _Server) -> None:
        self._master, slave = pty.openpty()
        self._proc = subprocess.Popen(
            ["tmux", "-L", server.name, "-f", "/dev/null", "attach", "-t", "=sample-display-0"],
            stdin=slave, stdout=slave, stderr=slave, env={**os.environ, "TERM": "xterm-256color"},
        )
        os.close(slave)
        time.sleep(1.0)

    def send(self, sequence: bytes, settle: float = 0.8) -> None:
        os.write(self._master, sequence)
        time.sleep(settle)

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._proc.kill()
        try:
            os.close(self._master)
        except OSError:
            pass


def _wheel(inner: str, *, inner_mouse: bool, helper=None) -> dict[str, str]:
    """Attach as a desktop tab does, turn the wheel up, and read both layers."""
    helper = helper or _helper()
    server = _Server(inner, inner_mouse=inner_mouse)
    client = None
    try:
        locked = server.tmux(*helper.lock_argv("sample-display-0")[1:])
        assert locked.returncode == 0, locked.stderr
        client = _Client(server)
        client.send(WHEEL_UP)
        return {
            "received": server.log.read_bytes().decode("latin-1"),
            "inner_mode": server.ask("=sample-director:", "#{pane_in_mode}"),
            "outer_mode": server.ask("=sample-display-0:", "#{pane_in_mode}"),
            "inner_mouse": server.ask("=sample-director:", "#{mouse}"),
            "attached": server.ask("=sample-director:", "#{session_attached}"),
        }
    finally:
        if client is not None:
            client.close()
        server.close()


def _unavailable() -> str:
    return "" if shutil.which("tmux") else "tmux is not installed"


def test_the_wheel_reaches_a_program_that_asked_for_the_mouse() -> None:
    if _unavailable():
        check(False, _unavailable())
    seen = _wheel("mouse", inner_mouse=True)
    check(seen["attached"] == "1", f"the slot's nested client is attached to the role session: {seen}")
    check("\x1b[<64;" in seen["received"],
          f"a wheel-up event arrives at the program in the role's pane: {seen['received']!r}")
    check(seen["outer_mode"] == "0", f"and the outer display session never enters copy mode: {seen}")


def test_the_wheel_scrolls_a_program_that_did_not() -> None:
    """Codex as it runs today: inline, no mouse capture, history in the scrollback."""
    if _unavailable():
        check(False, _unavailable())
    seen = _wheel("plain", inner_mouse=True)
    check(seen["inner_mode"] == "1", f"the role session enters copy mode over the role's scrollback: {seen}")
    check(seen["outer_mode"] == "0", f"while the outer display session does not: {seen}")


def test_with_the_role_sessions_mouse_off_the_wheel_does_nothing() -> None:
    """The live syrd Audit state, which the slot now repairs."""
    if _unavailable():
        check(False, _unavailable())
    seen = _wheel("plain", inner_mouse=False)
    check(seen["inner_mouse"] in ("0", "off") and seen["inner_mode"] == "0" and seen["outer_mode"] == "0",
          f"an inherited mouse off leaves the wheel with nowhere to go: {seen}")


def test_a_reattach_rebuilds_the_wheel_bindings() -> None:
    """The live repair was lost at the next attach; this is applied by every attach."""
    if _unavailable():
        check(False, _unavailable())
    helper = _helper()
    server = _Server("plain", inner_mouse=True)
    try:
        server.tmux(*helper.lock_argv("sample-display-0")[1:])
        server.tmux("unbind-key", "-T", helper.DISPLAY_KEY_TABLE, "WheelUpPane")
        server.tmux("bind-key", "-T", helper.DISPLAY_KEY_TABLE, "WheelDownPane", "copy-mode")
        again = server.tmux(*helper.lock_argv("sample-display-0")[1:])
        listed = server.tmux("list-keys", "-T", helper.DISPLAY_KEY_TABLE).stdout.strip().splitlines()
    finally:
        server.close()
    wheel = sorted(line.split()[3:] for line in listed if "Wheel" in line)
    check(again.returncode == 0 and wheel == [["WheelDownPane", "send-keys", "-M"], ["WheelUpPane", "send-keys", "-M"]],
          f"the next attach restores both wheel bindings, whatever was left: {listed}")
    check(len(listed) == len(helper.FOCUS_BINDINGS) + 2 and not any("copy-mode" in line for line in listed),
          f"and the table holds the focus and wheel bindings and nothing else: {listed}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"display_wheel_forwarding_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
