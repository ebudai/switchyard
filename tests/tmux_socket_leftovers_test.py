#!/usr/bin/env python3
"""SYRD-823: a test's tmux server leaves no socket behind in the account's tmux directory, however the test ends.

tmux never removes its socket, not when the last session ends and not on kill-server (tmux 3.8, measured). About 169
dead sockets had piled up in /tmp/tmux-1006 -- beside the live role sessions' own -- from suites that ended their
sessions but never the file, from a clean-up that gave up while the server still had a session the code under test
had made, and from runs a timeout stopped before any `finally`.

Here, against real tmux servers, each in a private TMUX_TMPDIR so nothing lands in the live directory:
* the clean-up's path is the one tmux itself reports, for an existing, a missing and an unset TMUX_TMPDIR;
* ending a test's server removes the server and its socket, even with a session the test never named;
* a server a process registered is ended, socket and all, when the process exits normally, by an uncaught exception,
  and by SIGTERM;
* a live server's socket, or one any other process holds, is never removed;
* only a server this process registered is ended: `default`, the live sessions' server, and any name it never
  registered are refused before tmux is asked anything. (This process runs with a private TMUX_TMPDIR throughout, so
  even a broken refusal could not reach the live `default`.)
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import tmux_socket_cleanup as cleanup  # noqa: E402 - it isolates the user bus on import

CHECKS = 0
READY = "tmux_socket_leftovers_test: server up "


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def reported_socket(server: str, env: dict[str, str]) -> Path:
    """Start `server` under `env` and return the socket path tmux itself reports."""
    cleanup.run_isolated_tmux(server, ["-f", "/dev/null", "new-session", "-d", "-s", "s", "sleep 60"], env=env, check=True)
    said = cleanup.run_isolated_tmux(server, ["display-message", "-p", "#{socket_path}"], env=env,
                                     capture_output=True, text=True, check=True).stdout.strip()
    return Path(said)


def test_the_path_is_where_tmux_puts_it(private: Path) -> None:
    base = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
    cases = {
        "an existing TMUX_TMPDIR": {**base, "TMUX_TMPDIR": str(private)},
        "a TMUX_TMPDIR that does not exist": {**base, "TMUX_TMPDIR": str(private / "missing")},
        "no TMUX_TMPDIR": {k: v for k, v in base.items() if k != "TMUX_TMPDIR"},
    }
    for label, env in cases.items():
        server = f"syrd823-path-{os.getpid()}-{len(label)}"
        try:
            said = reported_socket(server, env)
            check(cleanup.isolated_tmux_socket_path(server, env) == said,
                  f"{label}: the clean-up looks where tmux put the socket: {cleanup.isolated_tmux_socket_path(server, env)} "
                  f"vs tmux's {said}")
        finally:
            check(cleanup.end_isolated_tmux_server(server, env) and not said.exists(),
                  f"{label}: the server and its socket are gone: {said}")


def test_ending_a_server_removes_it_whatever_it_holds(env: dict[str, str]) -> None:
    server = f"syrd823-extra-{os.getpid()}"
    said = reported_socket(server, env)
    cleanup.run_isolated_tmux(server, ["new-session", "-d", "-s", "made-by-the-code-under-test", "sleep 60"], env=env,
                              check=True)
    cleanup.cleanup_isolated_tmux_sessions(server, ["s"])  # names only one of its two sessions
    alive = cleanup.run_isolated_tmux(server, ["list-sessions"], env=env, capture_output=True).returncode == 0
    check(not alive and not said.exists(),
          f"a session the test never named does not keep the server, or its socket: alive={alive} left={said.exists()}")


def test_a_live_server_keeps_its_socket(env: dict[str, str]) -> None:
    server = f"syrd823-live-{os.getpid()}"
    said = reported_socket(server, env)
    try:
        held = dict(os.environ)
        os.environ.update({"TMUX_TMPDIR": env["TMUX_TMPDIR"]})
        try:
            cleanup.cleanup_dead_isolated_tmux_socket(server)
        finally:
            os.environ.clear()
            os.environ.update(held)
        answers = cleanup.run_isolated_tmux(server, ["list-sessions"], env=env, capture_output=True).returncode == 0
        check(said.exists() and answers and cleanup.socket_is_bound(said),
              f"the dead-socket clean-up leaves a live server and its socket alone: exists={said.exists()} answers={answers}")
    finally:
        cleanup.end_isolated_tmux_server(server, env)
    check(not said.exists() and not cleanup.socket_is_bound(said), "and once ended, its socket is unbound and gone")


def test_a_socket_held_by_anything_is_kept(env: dict[str, str]) -> None:
    """Not every holder answers as tmux does: whatever process holds a socket at that path, the file stays."""
    import socket

    server = f"syrd823-held-{os.getpid()}"
    path = cleanup.isolated_tmux_socket_path(server, env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        holder.bind(str(path))  # bound, not listening: tmux is refused at once and sees no server, so only the hold shows
        held = dict(os.environ)
        os.environ["TMUX_TMPDIR"] = env["TMUX_TMPDIR"]
        try:
            cleanup.cleanup_dead_isolated_tmux_socket(server)
        finally:
            os.environ.clear()
            os.environ.update(held)
        kept_by_dead_cleanup = path.exists()
        cleanup.own_isolated_tmux_server(server, env)  # as if this test had started a server by that name
        gone = cleanup.end_isolated_tmux_server(server, env)
        check(kept_by_dead_cleanup and not gone and path.exists(),
              f"a socket some process holds is never removed: after the dead-socket clean-up {kept_by_dead_cleanup}, "
              f"after ending the server {path.exists()} (reported gone: {gone})")
    finally:
        holder.close()
        path.unlink(missing_ok=True)
        cleanup._OWNED.pop(server, None)


CHILD = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
import tmux_socket_cleanup as cleanup
server, how = sys.argv[2], sys.argv[3]
cleanup.run_isolated_tmux(server, ["-f", "/dev/null", "new-session", "-d", "-s", "s", "sleep 60"], check=True)
print("%s%s" % (sys.argv[4], server), flush=True)
if how == "raises":
    raise RuntimeError("the test failed")
if how == "waits":
    time.sleep(60)
"""


def test_every_exit_ends_the_server(env: dict[str, str]) -> None:
    for how, expected in (("returns", 0), ("raises", 1), ("waits", 128 + signal.SIGTERM)):
        server = f"syrd823-exit-{how}-{os.getpid()}"
        socket_path = cleanup.isolated_tmux_socket_path(server, env)
        child = subprocess.Popen([sys.executable, "-c", CHILD, str(ROOT / "tests"), server, how, READY], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            first = child.stdout.readline()
            if how == "waits" and first.startswith(READY):
                child.send_signal(signal.SIGTERM)
            rest, _ = child.communicate(timeout=60)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        alive = cleanup.run_isolated_tmux(server, ["list-sessions"], env=env, capture_output=True).returncode == 0
        left = socket_path.exists()
        if alive or left:  # this test's own leftovers: end them, so even a failure leaves nothing running
            cleanup.own_isolated_tmux_server(server, env)
            cleanup.end_isolated_tmux_server(server, env)
        check(first.startswith(READY) and child.returncode == expected and not alive and not left,
              f"a process that {how}: its server is ended and its socket removed: rc={child.returncode} "
              f"alive={alive} left={left} {first}{rest[-400:]}")


def refused(call) -> str:
    try:
        call()
    except AssertionError as exc:
        return str(exc)
    return ""


def test_only_its_own_servers_are_ended(env: dict[str, str]) -> None:
    server = f"syrd823-foreign-{os.getpid()}"
    # Started without the helper, as another process's server would be: never registered here.
    subprocess.run(["tmux", "-L", server, "-f", "/dev/null", "new-session", "-d", "-s", "s", "sleep 60"], env=env,
                   check=True)
    socket_path = cleanup.isolated_tmux_socket_path(server, env)
    try:
        for label, call in (("end_isolated_tmux_server", lambda: cleanup.end_isolated_tmux_server(server, env)),
                            ("cleanup_isolated_tmux_sessions", lambda: cleanup.cleanup_isolated_tmux_sessions(server, ["s"]))):
            why = refused(call)
            answers = subprocess.run(["tmux", "-L", server, "list-sessions"], env=env, capture_output=True).returncode == 0
            check(why and answers and socket_path.exists(),
                  f"{label} refuses a server this process never registered, and leaves it running: {why!r} "
                  f"answers={answers}")
    finally:
        cleanup.own_isolated_tmux_server(server, env)
        cleanup.end_isolated_tmux_server(server, env)
    check(not socket_path.exists(), "and once registered, it is ended like any other")
    for label, call in (("end_isolated_tmux_server", lambda: cleanup.end_isolated_tmux_server("default", env)),
                        ("cleanup_isolated_tmux_sessions", lambda: cleanup.cleanup_isolated_tmux_sessions("default", [])),
                        ("own_isolated_tmux_server", lambda: cleanup.own_isolated_tmux_server("default"))):
        check("default" in refused(call), f"{label} refuses the live sessions' server, default")


def main() -> int:
    if shutil.which("tmux") is None:
        print("tmux_socket_leftovers_test: skipped (no tmux)")
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd823.", dir="/tmp") as raw:
        private = Path(raw)
        env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "TMUX_PANE"}}
        env["TMUX_TMPDIR"] = str(private)
        os.environ["TMUX_TMPDIR"] = str(private)  # every tmux this process runs without an env of its own lands here
        test_the_path_is_where_tmux_puts_it(private)
        test_ending_a_server_removes_it_whatever_it_holds(env)
        test_a_live_server_keeps_its_socket(env)
        test_a_socket_held_by_anything_is_kept(env)
        test_every_exit_ends_the_server(env)
        test_only_its_own_servers_are_ended(env)
        left = sorted(p.name for p in (private / f"tmux-{os.getuid()}").glob("*")) if (private / f"tmux-{os.getuid()}").is_dir() else []
        check(left == [], f"nothing is left in the private socket directory: {left}")
    print(f"tmux_socket_leftovers_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
