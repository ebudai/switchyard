"""What a notice's delivery proved, kept until it is settled (SYRD-565).

Three things are proved apart, because each fails on its own:

* **input** -- directorctl typed the text into the pane (its exit status);
* **submission** -- the text left the composer, read from the pane itself; and
* **receipt** -- the recipient's runtime started a turn, from its own hooks.

On otto a nudge's text sat in a Hermes composer, unsubmitted, for 50 minutes
while the board believed it delivered; and four real turns were recorded
unconfirmed for good because their first act -- a slow preflight compression --
started 15-16 s after the send, just past a fixed witness window.

So text still in the composer is never acknowledged: the notice is parked and
this pass presses submit on it, only while the composer holds exactly what it
held after the send, and tells the Director once if that never works. Text that
left the composer with no turn inside the short window is acknowledged -- it is
in -- and its receipt is watched here up to a horizon: a late turn records the
delivery, none tells the Director. Both live in ticket_board.notification_proofs,
so a restarted listener carries on rather than sending again.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any

from . import pane_composer

#: How long a submitted notice's receipt is watched before it is reported missed.
RECEIPT_HORIZON_SECONDS = 900.0
#: How long an unsubmitted notice is parked; this pass decides before then.
UNSUBMITTED_PARK_SECONDS = 3600.0
#: Presses of submit before the Director is told the text will not go.
SUBMIT_ATTEMPTS_MAX = 3
#: A pressed submit is read back after this long.
SUBMIT_SETTLE_SECONDS = 1.0

UNSUBMITTED_EVENT = "send_unsubmitted"
RECEIPT_PENDING = "receipt_pending"
RECEIPT_MISSED = "receipt_missed"


def gate_of(dispatch: Any) -> Any:
    """The PaneActivityGate behind the dispatcher's activity gate, if it has one."""
    return getattr(dispatch.activity_gate(), "__self__", None)


def read_composer(gate: Any, target: str) -> str | None:
    """What the target's composer holds now: '' empty, None when it cannot be read.

    Through the gate's own tmux runners, unjoined so a row is the cursor's row.
    """
    cursor_runner = getattr(gate, "cursor_position_runner", None)
    capture_runner = getattr(gate, "capture_pane_runner", None)
    if cursor_runner is None or capture_runner is None:
        return None
    try:
        cursor = cursor_runner(["tmux", "display-message", "-p", "-t", target, "#{cursor_x} #{cursor_y}"],
                               check=True, text=True, capture_output=True, timeout=2.0)
        x, y = (int(part) for part in str(cursor.stdout).split()[:2])
        capture = capture_runner(["tmux", "capture-pane", "-p", "-t", target],
                                 check=True, text=True, capture_output=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not capture.stdout:
        # A real pane always captures its rows, blank or not; nothing at all is
        # not a composer that can be read.
        return None
    return pane_composer.composer_text(str(capture.stdout).splitlines(), x, y)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def left_in_composer(before: str | None, after: str | None) -> bool | None:
    """Did this send leave text in the composer? None when that cannot be told.

    The gate sends only to an idle pane, so an empty composer before and text
    after is this notice's text, still there.
    """
    if before is None or after is None or before != "":
        return None
    return after != ""


def park_unsubmitted(conn: Any, notification_id: int, target: str, sent_at: float, composer: str) -> None:
    conn.execute(
        "SELECT ticket_board.record_unsubmitted_notification(%s::bigint, %s::text, to_timestamp(%s), %s::text, %s::interval)",
        (notification_id, target, sent_at, sha(composer), f"{UNSUBMITTED_PARK_SECONDS:g} seconds"),
    )


def watch_receipt(conn: Any, notification_id: int, target: str, sent_at: float, submitted_at: float) -> None:
    conn.execute(
        "SELECT ticket_board.watch_notification_receipt(%s::bigint, %s::text, to_timestamp(%s), to_timestamp(%s), %s::interval)",
        (notification_id, target, sent_at, submitted_at, f"{RECEIPT_HORIZON_SECONDS:g} seconds"),
    )


def _resolve(conn: Any, notification_id: int, state: str, detail: str, director_notice: str | None = None) -> None:
    conn.execute("SELECT ticket_board.resolve_notification_proof(%s::bigint, %s::text, %s::text, %s::text)",
                 (notification_id, state, detail, director_notice))


def _submitter(listener: Any) -> Any:
    """How to press submit: an injected one, else directorctl's, else none (never a guess)."""
    explicit = getattr(listener, "delivery_submitter", None)
    if callable(explicit):
        return explicit
    return getattr(listener.sender, "submit", None)


def _present(listener: Any, conn: Any) -> bool:
    """Whether this board has the proof record yet: a listener can be newer than its board's migrations."""
    present = getattr(listener, "_delivery_proof_present", None)
    if present is None:
        row = conn.execute(
            "SELECT to_regprocedure('ticket_board.open_notification_proofs()') IS NOT NULL AS present").fetchone()
        present = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
        listener._delivery_proof_present = present
    return present


def run(listener: Any, conn: Any) -> int:
    """One pass over every notice whose delivery is still owed; returns how many settled."""
    if not _present(listener, conn):
        return 0
    result = conn.execute("SELECT * FROM ticket_board.open_notification_proofs()")
    rows = result.fetchall() if hasattr(result, "fetchall") else []  # a cursor always has it
    gate = gate_of(listener.dispatch)
    witnessed = getattr(gate, "submission_witnessed", None)
    settled = 0
    for row in rows:
        get = row.get if hasattr(row, "get") else None
        proof = dict(row) if get else dict(zip(
            ("notification_id", "ticket_id", "target_role", "kind", "target", "state", "sent_at", "composer_sha256",
             "submit_attempts", "submitted_at", "horizon_at", "director_told", "queued", "payload"), row))
        # The listener's connection hands text columns back as bytes.
        proof = {key: value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                 for key, value in proof.items()}
        if proof["state"] == RECEIPT_PENDING:
            settled += _settle_receipt(listener, conn, proof, witnessed)
        else:
            settled += _settle_unsubmitted(listener, conn, proof, gate, witnessed)
    return settled


def _trace(listener: Any, conn: Any, proof: dict, event: str, reason: str, detail: dict) -> None:
    listener.ledger.trace(conn, notification_id=proof["notification_id"], ticket_id=proof["ticket_id"],
                          target_role=proof["target_role"], kind=proof["kind"], event=event, pane_busy=False,
                          busy_reason=reason, region_digest="", detail={"proof": proof["state"], **detail})


def _settle_receipt(listener: Any, conn: Any, proof: dict, witnessed: Any) -> int:
    since = proof["submitted_at"].timestamp()
    answer = witnessed(proof["target"], since) if callable(witnessed) else None
    if answer:
        # The turn came late -- a slow preflight, say -- but it came: delivered.
        _trace(listener, conn, proof, "send", "receipt_late", {"receipt": "witnessed_late"})
        _resolve(conn, proof["notification_id"], "delivered", "receipt witnessed after the short window")
        return 1
    if listener.dispatch.wall_clock() < proof["horizon_at"].timestamp():
        return 0
    _trace(listener, conn, proof, "send_unconfirmed", RECEIPT_MISSED, {"receipt": "missed"})
    _resolve(conn, proof["notification_id"], RECEIPT_MISSED, "no turn started before the horizon",
             f"{proof['ticket_id']}: the notice to {proof['target_role']} left its composer at "
             f"{proof['submitted_at'].isoformat(timespec='seconds')} but no turn has started since; it may not have "
             f"been taken. Check {proof['target_role']}'s pane.")
    return 1


def _settle_unsubmitted(listener: Any, conn: Any, proof: dict, gate: Any, witnessed: Any) -> int:
    notification_id = proof["notification_id"]
    if not proof["queued"]:
        # Its notice was dropped or superseded meanwhile. The text may still be
        # in the composer; it is not this pass's to clear.
        _resolve(conn, notification_id, "abandoned", "the notice was settled elsewhere while its text was unsubmitted")
        return 1
    payload = proof.get("payload")
    payload_text = payload if isinstance(payload, str) else json.dumps(payload or {})
    if not listener.eligibility._notification_is_current(conn, proof["ticket_id"], proof["target_role"], payload_text):
        # Superseded while it sat there: the ticket moved on, so submitting the
        # old text would tell its owner something no longer true.
        listener.ledger.discard(conn, notification_id, "superseded_while_unsubmitted")
        _resolve(conn, notification_id, "abandoned", "the notice was superseded while its text was unsubmitted",
                 f"{proof['ticket_id']}: a notice to {proof['target_role']} that no longer applies was left unsubmitted "
                 "in its composer; the board will not submit it. Clear it from the pane.")
        return 1
    composer = read_composer(gate, proof["target"]) if gate is not None else None
    if composer is None:
        return 0  # Cannot see the composer: nothing is pressed blind.
    if composer == "":
        since = proof["sent_at"].timestamp()
        if callable(witnessed) and witnessed(proof["target"], since):
            # Submitted by someone, and taken: the notice is in.
            _trace(listener, conn, proof, "send", "submitted_late", {"receipt": "witnessed"})
            listener.ledger.ack(conn, notification_id)
            _resolve(conn, notification_id, "delivered", "the composer emptied and a turn started")
            return 1
        # The text is gone and nothing took it: typing it again is safe now.
        _resolve(conn, notification_id, "abandoned", "the composer was cleared; the notice is sent again")
        listener.ledger.requeue(conn, notification_id, 0, "unsubmitted_cleared", delay_seconds=0)
        return 1
    if sha(composer) != proof["composer_sha256"]:
        # Somebody edited it: it is theirs now. Never submit another's text.
        _resolve(conn, notification_id, "unsubmitted", "the composer was edited after the send; submit is not pressed",
                 f"{proof['ticket_id']}: a notice to {proof['target_role']} is sitting unsubmitted in its composer and "
                 "the text has since been edited, so the board will not submit it. Check the pane.")
        return 0
    submit = _submitter(listener)
    if proof["submit_attempts"] >= SUBMIT_ATTEMPTS_MAX or not callable(submit):
        _resolve(conn, notification_id, "unsubmitted", "submit did not take the text",
                 f"{proof['ticket_id']}: a notice to {proof['target_role']} has sat unsubmitted in its composer since "
                 f"{proof['sent_at'].isoformat(timespec='seconds')}; pressing submit did not send it. Check the pane.")
        return 0
    conn.execute("SELECT ticket_board.note_notification_submit_attempt(%s::bigint)", (notification_id,))
    pressed_at = listener.dispatch.wall_clock()
    try:
        submit(proof["target"])
    except (OSError, subprocess.SubprocessError) as exc:
        _trace(listener, conn, proof, UNSUBMITTED_EVENT, "submit_failed", {"error": str(exc)[-300:]})
        return 0
    listener.dispatch.sleeper(SUBMIT_SETTLE_SECONDS)
    after = read_composer(gate, proof["target"])
    if after != "":
        _trace(listener, conn, proof, UNSUBMITTED_EVENT, "still_in_composer", {"submit_attempt": proof["submit_attempts"] + 1})
        return 0
    # The press took the text: now it is a submitted notice like any other.
    _trace(listener, conn, proof, "submitted", "submit_pressed", {"submit_attempt": proof["submit_attempts"] + 1})
    watch_receipt(conn, notification_id, proof["target"], proof["sent_at"].timestamp(), pressed_at)
    return 1
