#!/usr/bin/env python3
"""SYRD-225: a notice is delivered when it can be seen, not when a command returns.

Live on SYRD-221. A Director DAT kickback handed the ticket back to ops. The
listener deferred it while ops's pane looked busy, then sent it once the busy
reading became `stale_prior_turn_child_work` -- a previous turn's child
processes still attached to the pane. `directorctl send` returned, and the
listener traced `send`, then `listener_ack`, and acked. The composer read empty
before and after, and the User saw no handoff.

`directorctl` types, submits and reports "delivered" without reading anything
back. So a successful return proves a command was accepted, not that the role
can see the notice -- and a pane holding a child process is exactly the pane
where typed input can go to the child instead of the conversation.

The listener now acks only on proof: the notice's opening words appear on the
pane more times after the send than before. Not merely present -- a kickback's
text is identical every time the same ticket comes back, so the line from an
earlier stint can still be in the scrollback, and "it is on screen" would report
the new handoff as shown on the strength of the old one. That is the masking
this ticket describes, one layer down.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board.notify_listener import (  # noqa: E402
    DELIVERY_UNCONFIRMED,
    DELIVERY_UNCONFIRMED_LIMIT,
    SEND_UNCONFIRMED,
    SEND_UNVERIFIABLE,
    STALE_PRIOR_TURN_CHILD_WORK,
    PaneActivityGate,
    PaneHookStateStore,
    delivery_fingerprint,
    delivery_fingerprint_count,
)
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener as RealListener  # noqa: E402
from standalone_test_runner import run_module_tests  # noqa: E402
from ticket_board_notify_listener_test import FakeConnection, TemporaryStateDir, queue_row  # noqa: E402
from ticket_board_stale_prior_turn_child_work_test import Pane, the_incident  # noqa: E402

#: The notice the live run sent: a kickback handing SYRD-221 back to ops.
KICKBACK = "SYRD-221 -- REGRESSION: Fresh test2 launch stops in a standalone Claude window is active again in Implementation"
#: What the pane showed, which was the previous turn and nothing since.
QUIET = "SYRD-97 revision 6 is with Audit.\n"

CHECKS = 0


def check(condition: object, message: str) -> None:
    global CHECKS
    assert condition, message
    CHECKS += 1


EVENT, REASON, DETAIL = 4, 6, 8


def events(conn: FakeConnection) -> list[str]:
    return [trace[EVENT] for trace in conn.traces if trace]


def trace_for(conn: FakeConnection, event: str) -> tuple[Any, ...]:
    matches = [trace for trace in conn.traces if trace and trace[EVENT] == event]
    check(matches, f"no {event!r} trace: {events(conn)}")
    return matches[-1]


def deliver(pane: Pane, screen, *, notification_id: int = 24193, listener: Any = None):
    """One delivery attempt of the kickback, into a pane that shows `screen`.

    `screen` is what the pane can be read as showing, given everything sent so
    far -- the part of this that decides whether the notice arrived.
    """
    sent: list[tuple[str, str]] = []
    conn = FakeConnection([
        queue_row(notification_id, "SYRD-221", kind="transition", state="in_progress",
                  assignee=pane.role, target_role=pane.role, message=KICKBACK)
    ])
    if listener is None:
        listener = RealListener(
            conninfo="dbname=test",
            sender=lambda destination, message: sent.append((destination, message)),
            activity_gate=pane.gate.is_working,
            connector=lambda *_args, **_kwargs: conn,
            poll_seconds=0,
            pre_send_recheck_delay_seconds=0,
            target_exists=lambda _target: True,
        )
    else:
        listener.connector = lambda *_args, **_kwargs: conn
        listener.sender = lambda destination, message: sent.append((destination, message))
    pane.gate.pane_text = lambda target: screen(target, sent)
    listener.listen_once(max_notifications=1)
    return listener, conn, sent


def swallowed(_target: str, _sent: list[tuple[str, str]]) -> str:
    """A pane that took the keystrokes and shows nothing for them."""
    return QUIET


def shows_what_it_was_sent(_target: str, sent: list[tuple[str, str]]) -> str:
    return QUIET + "".join(f"> {message}\n" for _destination, message in sent)


def test_the_incident_is_the_classification_this_runs_on() -> None:
    """Otherwise every case below is testing some other pane."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        busy, reason = pane.verdict()
    check(busy is False, f"the incident's pane is not free to send to: {reason}")
    check(reason == STALE_PRIOR_TURN_CHILD_WORK,
          f"the incident's pane is not classified as it was live: {reason}")


def test_a_kickback_the_pane_swallowed_is_not_acked() -> None:
    """The live failure, as a test."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        _listener, conn, sent = deliver(pane, swallowed)

    check(len(sent) == 1, f"the kickback was not sent at all, so this proves nothing: {sent}")
    check(conn.acked == [],
          f"a notice nothing shows arrived was acked as delivered: {conn.acked}")
    check("listener_ack" not in events(conn),
          f"the listener claimed the notice as delivered: {events(conn)}")
    check([row[0] for row in conn.requeued] == [24193],
          f"the notice left the queue instead of waiting to be delivered: {conn.requeued}")
    unconfirmed = trace_for(conn, SEND_UNCONFIRMED)
    check(unconfirmed[REASON] == SEND_UNCONFIRMED,
          f"the trace does not say the delivery went unconfirmed: {unconfirmed[REASON]}")
    detail = json.loads(unconfirmed[DETAIL])
    check(detail.get("delivery_proof") == SEND_UNCONFIRMED,
          f"the trace does not record what proof was sought: {detail}")
    check(detail.get("fingerprint") == delivery_fingerprint(KICKBACK),
          f"the trace does not record what it looked for: {detail}")


def test_the_same_notice_from_an_earlier_stint_proves_nothing() -> None:
    """A kickback reads the same every time the ticket comes back.

    So the line from ops's previous stint can still be on the pane. Counting
    it as this delivery is the masking the ticket describes, one layer down.
    """
    def already_there(_target: str, _sent: list[tuple[str, str]]) -> str:
        return QUIET + f"> {KICKBACK}\n"

    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        _listener, conn, sent = deliver(pane, already_there)

    check(len(sent) == 1, "the kickback was not sent at all")
    check(delivery_fingerprint_count(already_there("", []), delivery_fingerprint(KICKBACK)) == 1,
          "the earlier stint's line is not on the pane, so this case tests nothing")
    check(conn.acked == [],
          f"an earlier stint's line was taken as proof of this one: {conn.acked}")


def test_a_notice_the_pane_shows_is_acked_and_says_how_it_was_proven() -> None:
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        _listener, conn, sent = deliver(pane, shows_what_it_was_sent)

    check(len(sent) == 1, "the kickback was not sent")
    check(conn.acked == [24193], f"a notice the pane shows was not acked: {conn.acked}")
    send = json.loads(trace_for(conn, "send")[DETAIL])
    check(send.get("delivery_proof") == "visible",
          f"the send does not say how it was proven: {send}")
    check(send.get("proof", {}).get("seen_after", 0) > send.get("proof", {}).get("seen_before", 0),
          f"the proof is not the notice appearing more times than before: {send}")


def test_a_pane_that_cannot_be_read_is_not_proof_either() -> None:
    def unreadable(_target: str, _sent: list[tuple[str, str]]) -> None:
        return None

    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        _listener, conn, _sent = deliver(pane, unreadable)

    check(conn.acked == [], f"an unreadable pane was taken as a delivery: {conn.acked}")
    check(trace_for(conn, SEND_UNCONFIRMED)[REASON] == SEND_UNVERIFIABLE,
          "an unreadable pane is not told apart from one that did not show it")


def test_a_pane_that_never_shows_it_ends_in_a_dead_letter_that_says_so() -> None:
    """A retry forever is its own failure, and nobody sees it either."""
    with TemporaryStateDir() as state_dir:
        pane = Pane(state_dir)
        the_incident(pane)
        listener = None
        dead: list[Any] = []
        acked: list[int] = []
        sends = 0
        for _attempt in range(DELIVERY_UNCONFIRMED_LIMIT):
            listener, conn, sent = deliver(pane, swallowed, listener=listener)
            sends += len(sent)
            dead.extend(conn.dead_lettered)
            acked.extend(conn.acked)

    check(sends == DELIVERY_UNCONFIRMED_LIMIT,
          f"each attempt did not actually send: {sends}")
    check(acked == [], f"a notice that never arrived was acked: {acked}")
    check(len(dead) == 1, f"the notice was not dead-lettered at the limit: {dead}")
    check(DELIVERY_UNCONFIRMED in json.dumps(dead[0], default=str),
          f"the dead letter does not say why: {dead[0]}")


def test_the_gate_reads_the_pane_with_enough_history() -> None:
    """The proof read is a real capture, deep enough not to lose the notice."""
    calls: list[list[str]] = []

    def capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="the screen\n")

    with TemporaryStateDir() as state_dir:
        gate = PaneActivityGate(state_store=PaneHookStateStore(state_dir), capture_pane_runner=capture)
        text = gate.pane_text("pgu-ops:0.0")

        def broken(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, args)

        failing = PaneActivityGate(state_store=PaneHookStateStore(state_dir), capture_pane_runner=broken)
        nothing = failing.pane_text("pgu-ops:0.0")

    check(text == "the screen\n", f"the pane was not read: {text!r}")
    args = calls[-1]
    check(args[:2] == ["tmux", "capture-pane"], f"not a pane capture: {args}")
    check("-S" in args and args[args.index("-S") + 1].startswith("-"),
          f"the capture reads no history, so a notice can scroll out of it: {args}")
    check(args[args.index("-t") + 1] == "pgu-ops:0.0", f"the wrong pane was read: {args}")
    check("-J" in args, f"wrapped lines are not joined, so a long notice cannot match: {args}")
    check(nothing is None, f"a failed read is not told apart from an empty pane: {nothing!r}")


def test_the_fingerprint_survives_every_way_directorctl_delivers() -> None:
    """Typed as-is, collapsed and truncated for the director, or staged.

    The shapes are directorctl's own: `normalize_director_payload` collapses
    whitespace and cuts to 200 characters, and a staged payload is typed as
    `please read <path> (<first line, collapsed, cut to 120>) and follow the
    instructions therein`.
    """
    notice = KICKBACK + "\n\nThe rest of it, which only a staged delivery carries."
    fingerprint = delivery_fingerprint(notice)
    first = " ".join(notice.splitlines()[0].split())
    direct = notice
    director = " ".join(notice.split())[:197] + "..."
    staged = f"please read /tmp/directorctl_payload.AbC123.txt ({first[:120]}) and follow the instructions therein"
    for label, shown in (("typed directly", direct), ("for the director", director), ("staged", staged)):
        check(delivery_fingerprint_count(shown, fingerprint) == 1,
              f"the notice {label} does not contain its own fingerprint: {shown!r}")
        # And as a CLI draws it: its own prompt glyph in front, broken at word
        # boundaries to a narrow pane, continuation lines indented. `-J` only
        # rejoins lines the TERMINAL wrapped; lines the application broke
        # itself stay broken, so the match has to survive that.
        words, lines, line = f"> {shown}".split(), [], ""
        for word in words:
            if line and len(line) + 1 + len(word) > 36:
                lines.append(line)
                line = "  " + word
            else:
                line = f"{line} {word}" if line else word
        lines.append(line)
        drawn = "\n".join(lines)
        check("\n" in drawn, f"the notice {label} was not wrapped, so this tests nothing")
        check(delivery_fingerprint_count(drawn, fingerprint) == 1,
              f"the notice {label}, as a CLI draws it, no longer matches: {drawn!r}")
    check(len(fingerprint) > 20, f"the fingerprint is too short to mean anything: {fingerprint!r}")


def main() -> int:
    run_module_tests(globals())
    print(f"ticket_board_delivery_proof_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
