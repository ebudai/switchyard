#!/usr/bin/env python3
"""SYRD-268: "delivered" needs the recipient to have taken the notice.

MEFP-1 entered Director Final Sign-Off. The board recorded the Director's
notice as delivered; the User reports it never arrived, and the Director only
acted once told.

A notice was recorded delivered whenever `directorctl send` returned 0. Its
own check is what the pane looks like -- the capture changed, or it shows
"Working" / "esc to interrupt" -- and a pane already busy on another turn looks
like that whatever happened to the notice. So "delivered" could mean only
"directorctl did not fail".

The recipient's runtime is a separate witness: its hooks (UserPromptSubmit and
Stop for Claude and Codex, Pre/PostInvocation for Gemini, pre/post_llm_call for
Hermes) record a turn when a prompt is taken. The activity gate only sends to
an idle pane, so a turn event after the send is the notice arriving. Without
one the notice is recorded `send_unconfirmed` -- never `send` -- and the board
reports it as such.

Everything here runs through the REAL activity gate and hook state store,
with a sender that "succeeds" exactly as directorctl does.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

os.environ["TICKET_BOARD_PROJECT"] = "mefp"
os.environ.pop("PGU_TICKET_BOARD_PROJECT", None)

import ticket_board_notify_listener_test as lt  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402

CHECKS = 0
DIRECTOR = "mefp-director:0.0"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class MisreadingGate(nl.PaneActivityGate):
    """The real gate, reading a pane that is mid-turn as idle.

    That is the only way a notice reaches a busy pane at all, so it is the
    case the witness exists for. Every activity answer says idle; the witness
    (`submission_witnessed`) is the real one, reading the real state file.
    """

    def is_working(self, target: str) -> bool:
        return False

    def pre_send_busy(self, target: str) -> bool:
        return False

    def last_trace(self, target: str) -> nl.ActivityTrace:
        return nl.ActivityTrace(False, "idle")


def deliver(pane_reaction=None, *, gate: str = "real", confirm_seconds: float = 0.4,
            history: tuple[tuple[str, str], ...] = (("idle", "codex.Stop"),)):
    """One MEFP-1 Final Sign-Off notice to an idle Codex Director.

    `pane_reaction(store, target)` is what the recipient's hooks do after the
    sender returns -- nothing, for the case this ticket is about.
    """
    with tempfile.TemporaryDirectory(prefix="syrd268.") as tmp:
        store = nl.PaneHookStateStore(Path(tmp))
        activity = (MisreadingGate if gate == "misreads" else nl.PaneActivityGate)(
            state_store=store,
            cursor_position_runner=lt.constant_cursor_runner(),
            capture_pane_runner=lt.sequenced_capture_runner(""),
        )
        # The Director's last turn ended while this listener was running, as
        # on the live host. (A state older than the listener is held by the
        # Director's startup latch, which is a different case.) It predates the
        # send, so it can never be mistaken for the notice arriving.
        for state, source in history:
            store.write(DIRECTOR, state, source=source)
        conn = lt.FakeConnection([lt.queue_row(
            57, "MEFP-1", assignee="director", target_role="director", state="director_review",
            message="MEFP-1 entered Director Final Sign-Off",
        )])
        sent: list[str] = []

        def directorctl_says_delivered(target: str, message: str) -> dict:
            sent.append(target)
            if pane_reaction is not None:
                pane_reaction(store, target)
            return {}

        listener = nl.TicketBoardNotifyListener(
            conninfo="dbname=test",
            project="mefp",
            sender=directorctl_says_delivered,
            # A bare function has no gate behind it: nothing to ask the witness.
            activity_gate=activity.is_working if gate in ("real", "misreads") else (lambda _target: False),
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
            target_exists=lambda _target: True,
            submission_confirm_seconds=confirm_seconds,
            submission_poll_seconds=0.05,
        )
        delivered = listener.listen_once(max_notifications=1)
    return delivered, conn, sent


def events(conn) -> list[str]:
    return lt.trace_events(conn)


def detail_of(conn, event: str) -> dict:
    for trace in conn.traces:
        if trace[4] == event:
            return json.loads(trace[8])
    raise AssertionError(f"no {event} trace: {events(conn)}")


def turn(source: str, state: str = "busy"):
    return lambda store, target: store.write(target, state, source=source)


def test_a_notice_nobody_took_is_not_delivered() -> None:
    """MEFP-1's shape: directorctl returned 0, the Director's pane took nothing."""
    delivered, conn, sent = deliver(pane_reaction=None)
    check(sent == [DIRECTOR], f"the notice was sent to the Director: {sent}")
    check(delivered == 0, "and is not counted delivered")
    check("send" not in events(conn) and "listener_ack" not in events(conn),
          f"no send or ack is recorded for it: {events(conn)}")
    check(nl.SEND_UNCONFIRMED_EVENT in events(conn), f"it is recorded unconfirmed: {events(conn)}")
    detail = detail_of(conn, nl.SEND_UNCONFIRMED_EVENT)
    check(detail["submission"]["witnessed"] is False, f"{detail}")
    check(detail["decision_reason"] == "no_submission_witnessed", f"{detail}")
    # Not retyped automatically -- the text may be sitting in the composer.
    check(conn.acked == [57] and conn.requeued == [] and conn.dead_lettered == [],
          f"taken off the queue, not retried or dead-lettered: {conn.acked} {conn.requeued}")


def test_a_notice_the_director_took_is_delivered() -> None:
    delivered, conn, _sent = deliver(turn("codex.UserPromptSubmit"))
    check(delivered == 1, "the Director's turn started: delivered")
    check(events(conn)[-2:] == ["send", "listener_ack"], f"{events(conn)}")
    check(detail_of(conn, "send")["submission"]["witnessed"] is True, "and the send says it was witnessed")


def whole_turn(start: str, end: str):
    """A turn the notice started and that has already finished."""
    def react(store, target):
        store.write(target, "busy", source=start)
        store.write(target, "idle", source=end)
    return react


def test_a_turn_that_starts_a_moment_after_directorctl_returns_counts() -> None:
    """A real hook fires after the keystrokes land, not inside directorctl."""
    import threading

    def later(store, target):
        threading.Timer(0.2, lambda: store.write(target, "busy", source="codex.UserPromptSubmit")).start()

    delivered, conn, _sent = deliver(later, confirm_seconds=2.0)
    check(delivered == 1 and "send" in events(conn), f"the listener waits for it: {events(conn)}")


def test_a_quick_turn_that_already_ended_still_counts() -> None:
    """The store keeps only the latest state; the start is carried past the end."""
    delivered, conn, _sent = deliver(whole_turn("codex.UserPromptSubmit", "codex.Stop"))
    check(delivered == 1 and "send" in events(conn), f"a finished turn it started counts: {events(conn)}")


def test_every_runtimes_turn_start_is_a_witness() -> None:
    for start, end in (
        ("claude.UserPromptSubmit", "claude.Stop"),
        ("codex.UserPromptSubmit", "codex.Stop"),
        ("gemini.PreInvocation", "gemini.PostInvocation"),
        ("hermes.pre_llm_call", "hermes.post_llm_call"),
    ):
        delivered, conn, _sent = deliver(turn(start))
        check(delivered == 1, f"{start} witnesses the notice: {events(conn)}")
        delivered, conn, _sent = deliver(whole_turn(start, end))
        check(delivered == 1, f"so does {start} followed by {end}: {events(conn)}")


def test_a_turn_ending_after_the_send_is_not_the_notice() -> None:
    """The busy-pane case: a turn already running when the notice was typed.

    Its END lands after the send and says nothing about the notice, which may
    be sitting unsubmitted in the composer. Only a turn START counts.
    """
    for end in ("codex.Stop", "claude.Stop", "gemini.PostInvocation", "hermes.post_llm_call"):
        delivered, conn, _sent = deliver(turn(end, "idle"))
        check(delivered == 0 and nl.SEND_UNCONFIRMED_EVENT in events(conn),
              f"{end} after the send is another turn ending: {events(conn)}")


def test_a_turn_already_running_when_the_notice_was_typed_is_not_the_notice() -> None:
    """The same case with the earlier turn's START on record, as the real hook
    writes it: the pane's own turn began before the send and ends after it.

    Its end is the latest write and is newer than the send; only the turn
    start says whether the notice was taken, and it is older.
    """
    started_before = (("idle", "codex.Stop"), ("busy", "codex.UserPromptSubmit"))
    delivered, conn, _sent = deliver(turn("codex.Stop", "idle"), gate="misreads", history=started_before)
    check(delivered == 0 and nl.SEND_UNCONFIRMED_EVENT in events(conn),
          f"a turn started before the send and ending after it is not the notice: {events(conn)}")
    # And a complete earlier turn followed by the notice's own turn still counts.
    finished_before = started_before + (("idle", "codex.Stop"),)
    delivered, conn, _sent = deliver(whole_turn("codex.UserPromptSubmit", "codex.Stop"), history=finished_before)
    check(delivered == 1 and "send" in events(conn), f"the notice's own turn after an earlier one: {events(conn)}")


def test_a_state_file_from_before_turn_starts_were_recorded() -> None:
    """Hooks not yet redeployed write no turn_started_at: read what they can say."""
    def legacy(source: str, state: str):
        def react(store, target):
            store.write(target, state, source=source)
            path = store._target_path(target)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw.pop("turn_started_at", None)
            path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        return react

    delivered, conn, _sent = deliver(legacy("codex.UserPromptSubmit", "busy"))
    check(delivered == 1, f"a legacy file whose latest write is a turn start: {events(conn)}")
    delivered, conn, _sent = deliver(legacy("codex.Stop", "idle"))
    check(delivered == 0, f"a legacy file whose latest write is an end cannot witness: {events(conn)}")


def test_the_hook_script_writes_what_the_listener_reads() -> None:
    """Two writers, one rule: the real hook script and the store must agree."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader(
        "pane_idle_hook", str(ROOT / "scripts" / "ticket-board-pane-idle-hook")
    )
    spec = importlib.util.spec_from_loader("pane_idle_hook", loader)
    hook = importlib.util.module_from_spec(spec)
    loader.exec_module(hook)
    check(set(hook.TURN_START_EVENTS) == set(nl.TURN_START_EVENTS),
          f"the same turn-start events: {hook.TURN_START_EVENTS} vs {nl.TURN_START_EVENTS}")
    sequence = (
        ("idle", "codex.SessionStart"),
        ("busy", "codex.UserPromptSubmit"),
        ("idle", "codex.Stop"),
        ("idle", "claude.Notification.idle_prompt"),
    )
    with tempfile.TemporaryDirectory(prefix="syrd268-writers.") as tmp:
        by_hook, by_store = Path(tmp) / "hook", Path(tmp) / "store"
        store = nl.PaneHookStateStore(by_store)
        seen: list[tuple[bool, bool]] = []
        for state, source in sequence:
            hook._write_state(by_hook, DIRECTOR, state, source=source)
            store.write(DIRECTOR, state, source=source)
            from_hook = nl.PaneHookStateStore(by_hook).read(DIRECTOR)
            from_store = store.read(DIRECTOR)
            seen.append((from_hook.turn_started_at is not None, from_store.turn_started_at is not None))
        check(seen == [(False, False), (True, True), (True, True), (True, True)],
              f"both set it on the start and carry it after: {seen}")


def test_things_that_are_not_turns_do_not_count() -> None:
    for label, reaction in (
        ("Claude's periodic idle notification", turn("claude.Notification.idle_prompt", "idle")),
        ("a session start", turn("codex.SessionStart", "idle")),
        ("the launcher", turn("team_launcher.start", "idle")),
        # A turn event from BEFORE the send is the pane's previous turn.
        ("a turn that predates the send",
         lambda store, target: store.write(target, "busy", source="codex.UserPromptSubmit",
                                           now=time.time() - 30)),
    ):
        delivered, conn, _sent = deliver(reaction)
        check(delivered == 0 and nl.SEND_UNCONFIRMED_EVENT in events(conn),
              f"{label} is not the notice arriving: {events(conn)}")


def test_cannot_tell_is_not_reported_as_either_answer() -> None:
    """A gate with no hook store to ask: behaviour as before, and said so."""
    delivered, conn, _sent = deliver(None, gate="fake")
    check(delivered == 1 and "send" in events(conn), "unchanged where nothing can be asked")
    check(detail_of(conn, "send")["submission"]["witnessed"] is None,
          "and the trace says it was not verifiable, rather than verified")


def test_the_wait_is_bounded() -> None:
    """What an unwitnessed notice costs over a witnessed one is the window.

    Measured as a difference: the delivery loop has its own sampling pauses
    either way, and those are not this wait.
    """
    def elapsed(reaction, window: float) -> float:
        started = time.monotonic()
        deliver(reaction, confirm_seconds=window)
        return time.monotonic() - started

    for window in (0.4, 1.5):
        extra = elapsed(None, window) - elapsed(turn("codex.UserPromptSubmit"), window)
        check(window - 0.3 <= extra <= window + 1.0,
              f"an unconfirmed notice waits about its {window}s window and no longer: +{extra:.2f}s")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            print(f"  {name}: ok", flush=True)
    print(f"notification_submission_witness_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
