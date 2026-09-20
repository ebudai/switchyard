#!/usr/bin/env python3
"""SYRD-219: a suite run from inside a tmux pane must leave that server alone.

During SYRD-216 the viewer geometry suite was run from App's live role pane and
disconnected every project presentation pane, twice. The User had to open a new
terminal and rebuild the six-pane presentation each time.

The cause was that `TMUX_TMPDIR` is not isolation. Measured with `TMUX` set: a
bare `tmux` and a `TMUX_TMPDIR`-only `tmux` both resolve to the caller's own
server, while `-L` and `-S` resolve where they say. So a fixture isolated by the
directory alone created its sessions on the live server and ended by killing it.

Every check here runs the workload **inside a real pane of a sentinel server**
that stands in for the live one, and proves the sentinel's sessions -- names and
creation timestamps -- are byte-identical afterwards. Timestamps as well as
names, because a server killed and rebuilt between two looks can present the
same names.

A test that merely called the fixture would have proved nothing: the defect only
appears when `TMUX` is inherited, which is exactly what running outside a pane
does not do. That is why Audit passed the suite while it was broken.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
from tmux_socket_cleanup import (  # noqa: E402
    assert_private_tmux_socket,
    kill_private_tmux_server,
    private_tmux_args,
    private_tmux_env,
    private_tmux_socket,
)

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class Sentinel:
    """A tmux server standing in for the live one, with sessions to lose."""

    SESSIONS = ("sentinel-director", "sentinel-main", "sentinel-app")

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="syrd219-sentinel.")).resolve()
        self.socket = private_tmux_socket(self.dir)
        assert_private_tmux_socket(self.socket, self.dir)
        self.env = private_tmux_env(self.dir, TERM="xterm-256color")
        for name in self.SESSIONS:
            self.tmux("new-session", "-d", "-s", name, "-x", "80", "-y", "24", "sleep 600")

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            private_tmux_args(self.socket, list(args)),
            env=self.env, capture_output=True, text=True,
        )

    def alive(self) -> bool:
        """Is this server still there at all?"""
        return self.socket.exists() and self.tmux("list-sessions", "-F", "#{session_name}").returncode == 0

    def fingerprint(self) -> str:
        """Names and creation times: a rebuilt server is not the same server."""
        return self.tmux(
            "list-sessions", "-F", "#{session_name} #{session_created}"
        ).stdout

    def run_inside_a_pane(self, command: str, timeout: float = 300.0) -> int:
        """Run a command in a pane of this server, as a person would.

        The point is the inheritance: a process started here has `TMUX` set to
        this server, which is the whole condition the defect needs.

        The exit status is collected by an explicit `/bin/sh` wrapper rather
        than by appending `; printf %s $?` to the command. A pane runs tmux's
        `default-shell`, which on this host is not `sh`: `$?` is not how fish
        spells the last status, so the marker was silently never written and
        the wait looked like a hang.
        """
        marker = self.dir / f"rc-{time.monotonic_ns()}"
        wrapper = self.dir / f"{marker.name}.sh"
        wrapper.write_text(f"#!/bin/sh\n{command}\nprintf %s $? > {marker}\n")
        wrapper.chmod(0o700)
        started = self.tmux(
            "new-session", "-d", "-s", f"work-{marker.name}", "-x", "200", "-y", "50",
            "/bin/sh", str(wrapper),
        )
        check(started.returncode == 0, f"the sentinel could not start the workload: {started.stderr}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker.exists():
                text = marker.read_text().strip()
                if text:
                    return int(text)
            # Watched as well as waited for. A workload that stops the server
            # it is running on takes its own pane with it, so the marker is
            # never written and the wait would otherwise run to the timeout
            # and report a hang -- when what actually happened is the exact
            # defect this suite exists to catch. Said plainly instead.
            if not self.alive():
                raise AssertionError(
                    "the workload stopped the tmux server it was running on: the "
                    f"sentinel at {self.socket} is gone. That is the SYRD-219 "
                    "defect -- an unqualified tmux command reaching the caller's "
                    "own server."
                )
            time.sleep(0.25)
        raise AssertionError(f"the workload did not finish within {timeout:g}s")

    def close(self) -> None:
        kill_private_tmux_server(self.socket, self.dir, self.env)
        shutil.rmtree(self.dir, ignore_errors=True)


def python_workload(body: str) -> str:
    """A shell command running `body` with the repository importable."""
    script = tempfile.NamedTemporaryFile(
        "w", prefix="syrd219-work.", suffix=".py", delete=False
    )
    script.write(body)
    script.close()
    return f"{sys.executable} {script.name}"


PROVES_TMUX_IS_INHERITED = f'''
import os, sys
sys.path.insert(0, {str(ROOT)!r})
sys.path.insert(0, {str(ROOT / "tests")!r})
# The condition the whole defect needs. If this is not true the case below is
# proving nothing, so it fails loudly rather than passing vacuously.
assert os.environ.get("TMUX"), "the workload did not inherit TMUX from its pane"
'''

BUILDS_AND_TEARS_DOWN_A_FIXTURE = PROVES_TMUX_IS_INHERITED + '''
from pathlib import Path
import tempfile
from tmux_socket_cleanup import (
    assert_private_tmux_socket, kill_private_tmux_server,
    private_tmux_args, private_tmux_env, private_tmux_socket,
)
import subprocess

own = Path(tempfile.mkdtemp(prefix="syrd219-own.")).resolve()
socket = private_tmux_socket(own)
assert_private_tmux_socket(socket, own)
env = private_tmux_env(own, TERM="xterm-256color")
subprocess.run(private_tmux_args(socket, ["new-session", "-d", "-s", "mine", "sleep 60"]),
               env=env, capture_output=True, text=True, check=True)
kill_private_tmux_server(socket, own, env)
'''

FAILS_AFTER_BUILDING_A_FIXTURE = BUILDS_AND_TEARS_DOWN_A_FIXTURE + '''
raise SystemExit("the workload failed on purpose, after its teardown ran")
'''


def test_a_successful_run_inside_a_pane_leaves_that_server_untouched() -> None:
    sentinel = Sentinel()
    try:
        before = sentinel.fingerprint()
        check(len(before.splitlines()) == len(Sentinel.SESSIONS),
              f"the sentinel did not come up with its sessions: {before!r}")
        rc = sentinel.run_inside_a_pane(python_workload(BUILDS_AND_TEARS_DOWN_A_FIXTURE))
        check(rc == 0, f"the workload failed inside the pane (rc={rc})")
        after = sentinel.fingerprint()
        check(after == before,
              f"the sentinel's sessions changed:\\n before {before!r}\\n after  {after!r}")
    finally:
        sentinel.close()


def test_a_failing_run_inside_a_pane_leaves_that_server_untouched() -> None:
    """The cleanup path, which is where the unqualified kill-server lived."""
    sentinel = Sentinel()
    try:
        before = sentinel.fingerprint()
        rc = sentinel.run_inside_a_pane(python_workload(FAILS_AFTER_BUILDING_A_FIXTURE))
        check(rc != 0, "the failing workload was supposed to fail, and did not")
        after = sentinel.fingerprint()
        check(after == before,
              f"a failing run took the sentinel's sessions with it:\\n"
              f" before {before!r}\\n after  {after!r}")
    finally:
        sentinel.close()


def test_the_viewer_suite_itself_runs_inside_a_pane_without_touching_it() -> None:
    """The suite from the incident, run the way that caused it.

    This is the case that would have caught the original defect. It is also the
    slowest, so it is the only full suite driven here; the rules the rest of the
    tree follows are enforced by the source lint instead.
    """
    suite = ROOT / "tests" / "viewer_landscape_layout_test.py"
    check(suite.exists(), "the viewer suite has moved; this case needs its new name")
    sentinel = Sentinel()
    try:
        before = sentinel.fingerprint()
        rc = sentinel.run_inside_a_pane(f"{sys.executable} {suite}", timeout=900.0)
        check(rc == 0, f"the viewer suite failed inside a pane (rc={rc})")
        after = sentinel.fingerprint()
        check(after == before,
              f"the viewer suite disturbed the server it was run from:\\n"
              f" before {before!r}\\n after  {after!r}")
    finally:
        sentinel.close()


def test_a_teardown_refuses_a_server_it_does_not_own() -> None:
    """And says so, rather than doing it and reporting success."""
    sentinel = Sentinel()
    mine = Path(tempfile.mkdtemp(prefix="syrd219-mine.")).resolve()
    try:
        before = sentinel.fingerprint()
        try:
            kill_private_tmux_server(sentinel.socket, mine, sentinel.env)
        except AssertionError as refusal:
            check("refusing to drive tmux" in str(refusal), f"unexpected refusal: {refusal}")
        else:
            raise AssertionError("a teardown accepted a socket outside its own directory")
        check(sentinel.fingerprint() == before,
              "the sentinel was disturbed by a teardown that should have refused")
    finally:
        shutil.rmtree(mine, ignore_errors=True)
        sentinel.close()


def main() -> int:
    if not shutil.which("tmux"):
        print("tmux_isolation_sentinel_test: tmux unavailable")
        return 0
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"tmux_isolation_sentinel_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
