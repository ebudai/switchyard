#!/usr/bin/env python3
"""SYRD-136: the deploy smoke must claim from a process no pane owns.

The check has always said "the deploy process is outside every registered
launcher pane". Under Polkit that is false: a deployment authorized from a
registered role pane is a descendant of it, the board resolves the probe as
that role and answers 200 -- correctly -- and the check read its own ancestry
as an authority failure and rolled a good release back. SYRD-134 journal
attempt 0008 is that, on a live host.

These cases run from this suite's own process, which on a Switchyard host is
itself inside a role pane. That is the point: the condition that produced the
false positive is the condition the tests run under, so a probe that claims
from its own ancestry fails them.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "ticket-board-socket-smoke"
sys.path.insert(0, str(ROOT / "scripts"))
from ticket_board.peer_identity import session_identity  # noqa: E402

# The probe is a program, but its two decisions are functions so they can be
# asked directly -- the fail-closed branches are the ones a live host will not
# reproduce on demand.
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("ticket_board_socket_smoke", str(ROOT / "scripts" / "ticket-board-socket-smoke"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
probe_module = importlib.util.module_from_spec(_spec)
_loader.exec_module(probe_module)

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1
    print(f"  ok  {message}")


class Board:
    """Just enough board to answer the two requests the probe makes.

    It records the pid on the other end of the claim, which is the whole
    question: the probe is supposed to be asking from somewhere no pane owns.
    """

    def __init__(self, socket_path: Path, *, claim_status: bytes) -> None:
        self.claim_status = claim_status
        self.claim_peer_pid: int | None = None
        self.claim_peer_pane = "not asked"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(socket_path))
        self.server.listen(8)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                raw = conn.recv(8192)
                if raw.startswith(b"POST /api/register-caller"):
                    import struct

                    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    self.claim_peer_pid = struct.unpack("3i", creds)[0]
                    # Resolved here, while the peer is still alive and still
                    # connected -- which is exactly when the real board asks.
                    self.claim_peer_pane = session_identity(self.claim_peer_pid)
                    conn.sendall(self.claim_status)
                else:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")

    def close(self) -> None:
        self.stop.set()
        try:
            self.server.close()
        except OSError:
            pass
        self.thread.join(timeout=2)


def run_probe(socket_path: Path, release_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), str(socket_path), "10", str(release_root)],
        text=True, capture_output=True,
    )


def test_the_claim_comes_from_a_process_no_pane_owns() -> None:
    """The regression itself: this test process has pane ancestry; the claim must not."""
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "board.sock"
        board = Board(path, claim_status=b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        try:
            result = run_probe(path, ROOT)
            check(result.returncode == 0, f"a refused claim passes the check: {result.stderr.strip()}")
            check("refused an unregistered process" in result.stdout, "and says so")
            check(board.claim_peer_pid is not None, "the board saw a claim")
            check(board.claim_peer_pid != os.getpid(),
                  "the claim did not come from this process")
            check(board.claim_peer_pane is None,
                  f"and the board resolved no pane above it ({board.claim_peer_pane!r})")
        finally:
            board.close()


def test_this_suite_runs_under_the_ancestry_that_caused_the_false_positive() -> None:
    """Without this, the case above could pass on a host where nothing has a pane."""
    owned = session_identity(os.getpid())
    check(owned is not None,
          f"this process is inside a pane ({owned}), which is the condition attempt 0008 hit")


def test_a_granted_claim_still_fails_the_deployment() -> None:
    """The check must still catch a board that hands authority to a stranger."""
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "board.sock"
        board = Board(path, claim_status=b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        try:
            result = run_probe(path, ROOT)
            check(result.returncode != 0, "a granted claim fails the check")
            check("was granted director" in result.stderr, "and names what went wrong")
        finally:
            board.close()


def test_a_boundary_it_cannot_establish_is_a_failure_not_a_pass() -> None:
    """If the probe cannot prove it is unregistered it must refuse, not assume.

    Reached by running a copy of the probe from a directory with no ticket_board
    package beside it -- which is also the real shape of the risk, since the
    probe finds the resolver next to itself in the release it ships in.
    """
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "board.sock"
        stray = Path(raw) / "stray"
        stray.mkdir()
        copied = stray / PROBE.name
        copied.write_text(PROBE.read_text(), encoding="utf-8")
        board = Board(path, claim_status=b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        try:
            result = subprocess.run(
                [sys.executable, str(copied), str(path), "10", str(stray)],
                text=True, capture_output=True,
            )
            check(result.returncode != 0,
                  "without the board's own resolver the check fails rather than passing")
            check("role-binding check failed" in result.stderr, "and says why")
            check(board.claim_peer_pid is None, "and no claim was made at all")
        finally:
            board.close()


def test_a_board_that_errors_on_the_claim_fails_the_deployment() -> None:
    """End to end, not only through verdict(): a 500 is not a refusal."""
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "board.sock"
        board = Board(path, claim_status=b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
        try:
            result = run_probe(path, ROOT)
            check(result.returncode != 0, "a board that errors on the claim fails the deployment")
            check("neither a grant nor an authorization refusal" in result.stderr,
                  "and says the boundary was not established")
        finally:
            board.close()


def test_an_unreachable_socket_still_fails() -> None:
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "absent.sock"
        result = run_probe(path, ROOT)
        check(result.returncode != 0, "an unreachable socket fails the check")
        check("Unix socket verification failed" in result.stderr, "as a reachability failure")


def test_a_process_a_pane_owns_is_refused_as_a_stand_in() -> None:
    """The check the orphan makes, asked from a process that is inside a pane.

    This suite runs under a role pane, so the answer here has to be the refusal.
    Without this the fail-closed path is never exercised at all: on a working
    host the orphan always succeeds, and a mutation that stops checking passes
    every end-to-end case.
    """
    check(probe_module.unregistered_problem() == "still-owned",
          "a process with a pane above it is refused as a stand-in for a stranger")


def test_every_verdict_fails_closed_except_an_actual_refusal() -> None:
    """Only an authorization refusal is evidence. Everything else is a failure.

    "not 200" is not the same as "refused": a 404 says the endpoint is not there
    to refuse anything and a 5xx says the board failed to decide, and a check
    that accepted either reported a broken board as a proven one. Review found
    exactly that, on the version this replaces.
    """
    for answer in ("still-owned", "", "claim-failed connection reset",
                   "resolver-unavailable no module", "HTTP/1.1 200 OK",
                   "HTTP/1.1 400 Bad Request", "HTTP/1.1 404 Not Found",
                   "HTTP/1.1 418 I'm a teapot", "HTTP/1.1 429 Too Many Requests",
                   "HTTP/1.1 500 Internal Server Error", "HTTP/1.1 502 Bad Gateway",
                   "HTTP/1.1 503 Service Unavailable", "garbage", "HTTP/1.1 notanumber"):
        status, _stream, message = probe_module.verdict(answer)
        check(status != 0, f"{answer!r} is a failure, not a pass: {message[:70]}")
    # Each failure says which failure it is. "still-owned" reaching the
    # operator as "not an HTTP status line" would be true and useless: the
    # thing they have to know is that the boundary was never established.
    _status, _stream, message = probe_module.verdict("still-owned")
    check("never actually made" in message,
          "an unestablished boundary says so, rather than blaming the answer")
    _status, _stream, message = probe_module.verdict("HTTP/1.1 500 Internal Server Error")
    check("neither a grant nor an authorization refusal" in message,
          "and an unexpected status says that instead")
    # These two fail either way -- each guard is caught by the next -- so the
    # only thing distinguishing them is what the operator is told. That is
    # worth pinning: a diagnosis that names the wrong cause sends the next
    # person to the wrong place.
    _status, _stream, message = probe_module.verdict("")
    check("no answer" in message, "no answer at all is reported as no answer")
    _status, _stream, message = probe_module.verdict("garbage")
    check("not an HTTP status line" in message,
          "and something that is not a status line is reported as that")

    for answer in ("HTTP/1.1 403 Forbidden", "HTTP/1.1 401 Unauthorized"):
        status, _stream, message = probe_module.verdict(answer)
        check(status == 0, f"{answer!r} is the one kind of answer that passes")
        check("refused an unregistered process" in message, "and it says what was proved")


def main() -> int:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        print(test.__name__)
        test()
    print(f"ticket_board_deploy_smoke_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
