#!/usr/bin/env python3
"""SYRD-264: a notice the board could not route is not a pane that does not exist.

MEFP-1 was routed to in_progress/ops. Its one transition notice was claimed,
then dead-lettered 28 seconds later as `tmux_target_missing` for
`mefp-ops:0.0` -- while the Ops worker in that pane had been alive since the
morning and still was. Nothing retried it; the pane sat at an empty prompt.

`directorctl send mefp-ops:0.0` resolves `ops` through the board's runtime
assignment (`GET /api/runtime-assignments/ops`). While that role's assignment
is hidden or absent -- mefp's declaration and its workers disagreed for much
of that day, and were rebound around then -- the board answers 404 and
directorctl exits:

    error: cannot resolve runtime assignment for ops: HTTP Error 404: Not Found

The listener's classifier had a fallback: target session name in the output
AND "not found" in the output means a missing pane. The output it searched
included the CalledProcessError's own text -- "Command '[..., 'send',
'mefp-ops:0.0', ...]'" -- so the session name was always there, and ANY
failure saying "not found" became a terminal missing-pane dead letter.

The real directorctl is run here against a board that answers 404, so the
marker is checked against what directorctl actually prints, not against a
string typed into this file.
"""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

# Before the listener is imported: its default role targets are derived from
# the project at import time, and this reproduces mefp's, where the listener's
# own target and the failing command's target are the same `mefp-ops:0.0`.
os.environ["TICKET_BOARD_PROJECT"] = "mefp"
os.environ.pop("PGU_TICKET_BOARD_PROJECT", None)

import ticket_board_notify_listener_test as lt  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402

CHECKS = 0
TARGET = "mefp-ops:0.0"
DIRECTORCTL = ROOT / "scripts" / "directorctl"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class NotFoundBoard(http.server.BaseHTTPRequestHandler):
    """A board whose runtime assignment for every role is absent: 404."""

    requests: list[str] = []

    def do_GET(self):  # noqa: N802
        NotFoundBoard.requests.append(self.path)
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"runtime assignment not found")

    def log_message(self, *_args):
        pass


def real_directorctl_failure() -> subprocess.CalledProcessError:
    """What the listener's sender raises when the board cannot route `ops`."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotFoundBoard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "TICKET_BOARD_PROJECT": "mefp",
        "TICKET_BOARD_PROCESS_AUTHORITY": "1",
        "TICKET_BOARD_URL": f"http://127.0.0.1:{server.server_port}",
        "DIRECTORCTL_DIAGNOSTICS": "1",
        # Never a live tmux server: resolution must refuse before tmux is used.
        "TMUX_TMPDIR": "/nonexistent-syrd264",
    }
    try:
        proc = subprocess.run(
            [str(DIRECTORCTL), "send", TARGET, "MEFP-1 is yours"],
            env=env, text=True, capture_output=True, timeout=30, check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    check(proc.returncode != 0, f"directorctl refuses: {proc.returncode} {proc.stdout} {proc.stderr}")
    check(any(path.endswith("/api/runtime-assignments/ops") for path in NotFoundBoard.requests),
          f"it asked the board for ops's assignment: {NotFoundBoard.requests}")
    return subprocess.CalledProcessError(
        proc.returncode, [str(DIRECTORCTL), "send", TARGET, "MEFP-1 is yours"],
        output=proc.stdout, stderr=proc.stderr,
    )


def listener_for(error: BaseException) -> tuple[nl.TicketBoardNotifyListener, "lt.FakeConnection"]:
    conn = lt.FakeConnection([lt.queue_row(41, "MEFP-1", attempts=1, assignee="ops", target_role="ops")])

    def sender(_target: str, _message: str) -> None:
        raise error

    sent_to: list[str] = []

    def recording_sender(target: str, message: str) -> None:
        sent_to.append(target)
        sender(target, message)

    listener = nl.TicketBoardNotifyListener(
        conninfo="dbname=test",
        project="mefp",
        sender=recording_sender,
        activity_gate=lambda _target: False,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
        # The pane exists: the pre-gate probe is not what fails in this case.
        target_exists=lambda _target: True,
    )
    listener.sent_to = sent_to  # type: ignore[attr-defined]
    return listener, conn


def test_an_unroutable_role_is_retried_not_dead_lettered() -> None:
    error = real_directorctl_failure()
    check("cannot resolve runtime assignment for ops" in error.stderr,
          f"the real directorctl says what failed: {error.stderr!r}")
    check("mefp-ops" in str(error) and "not found" in error.stderr.lower(),
          "and the old rule's two conditions both hold -- which is what misfired")
    check(nl.delivery_failure_reason(error, TARGET) == nl.RUNTIME_ASSIGNMENT_UNRESOLVED,
          f"classified as the routing fault it is: {nl.delivery_failure_reason(error, TARGET)}")

    listener, conn = listener_for(error)
    check(listener.listen_once(max_notifications=1) == 0, "nothing delivered this round")
    check(listener.sent_to == [TARGET], f"the listener sent to mefp's ops pane: {listener.sent_to}")
    check(conn.dead_lettered == [], f"MEFP-1's notice is not dead-lettered: {conn.dead_lettered}")
    check(len(conn.requeued) == 1, f"it is requeued, to be delivered once ops routes again: {conn.requeued}")
    check(conn.requeued[0][1][2] == nl.RUNTIME_ASSIGNMENT_UNRESOLVED,
          f"with the reason recorded: {conn.requeued[0]}")
    check(lt.trace_events(conn) == ["listener_claim", "send_failed"], f"{lt.trace_events(conn)}")
    detail = json.loads(conn.traces[1][8])
    check(detail["decision_reason"] == nl.RUNTIME_ASSIGNMENT_UNRESOLVED, f"{detail}")
    check("cannot resolve runtime assignment for ops" in detail["error_output"],
          f"and the trace keeps what directorctl actually said: {detail.get('error_output')!r}")


def test_a_pane_that_is_really_gone_is_still_dead_lettered_with_its_evidence() -> None:
    error = subprocess.CalledProcessError(
        1, [str(DIRECTORCTL), "send", TARGET, "m"], output="", stderr="can't find pane: mefp-ops:0.0\n",
    )
    listener, conn = listener_for(error)
    listener.listen_once(max_notifications=1)
    check(len(conn.dead_lettered) == 1 and conn.requeued == [], "a missing pane is still terminal")
    check(conn.dead_lettered[0][1][1] == "tmux_target_missing", f"{conn.dead_lettered}")
    detail = json.loads(conn.dead_lettered[0][1][2])
    check("can't find pane" in detail["error_output"],
          f"and the dead letter now keeps the text it was judged from: {detail}")


def test_the_command_line_is_not_evidence() -> None:
    """"not found" about something else is not the pane."""
    cmd = [str(DIRECTORCTL), "send", TARGET, "m"]
    unrelated = subprocess.CalledProcessError(1, cmd, output="", stderr="sudo: tmux: command not found\n")
    check(nl.delivery_failure_reason(unrelated, TARGET) != "tmux_target_missing",
          "a missing program is not a missing pane")
    listener, conn = listener_for(unrelated)
    listener.listen_once(max_notifications=1)
    check(conn.dead_lettered == [] and len(conn.requeued) == 1, "so it is retried")

    # directorctl's own wording for a missing session names the session in its
    # OUTPUT, which is what that fallback rule exists for -- and it still holds.
    own = subprocess.CalledProcessError(1, cmd, output="", stderr="error: mefp-ops not found\n")
    check(nl.delivery_failure_reason(own, TARGET) == "tmux_target_missing",
          "a message naming the session as not found is still a missing pane")
    check(nl.delivery_error_output(own) == "error: mefp-ops not found\n",
          "and the evidence is the output alone")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name}: ok", flush=True)
    print(f"notification_routing_unresolved_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
