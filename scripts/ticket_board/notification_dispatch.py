"""Outbound ticket-notification transport, diagnostics, and receipt confirmation."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

from . import delivery_proof
from .notification_ledger import NotificationLedger
from .runtime_paths import directorctl_path

DEFAULT_DIRECTORCTL_SEND_TIMEOUT_SECONDS = 10.0
DEFAULT_DIRECTORCTL = directorctl_path(__file__)


@dataclass(frozen=True)
class ComposerSnapshot:
    available: bool
    marker_found: bool = False
    active: bool = False
    content_sha256: str = ""
    content_length: int = 0
    error: str = ""

    def as_trace_detail(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "marker_found": self.marker_found,
            "active": self.active,
            "content_sha256": self.content_sha256,
            "content_length": self.content_length,
            "error": self.error,
        }


def display_message(message: str) -> str:
    return message.replace(
        "needs director triage in analysis",
        "needs director triage in Triage",
    )


class DirectorctlSender:
    def __init__(
        self,
        directorctl_bin: str = DEFAULT_DIRECTORCTL,
        *,
        timeout_seconds: float = DEFAULT_DIRECTORCTL_SEND_TIMEOUT_SECONDS,
        director_typing_max_attempts: int = 0,
    ) -> None:
        self.directorctl_bin = directorctl_bin
        self.timeout_seconds = timeout_seconds
        self.director_typing_max_attempts = director_typing_max_attempts

    def __call__(self, target: str, message: str) -> dict[str, Any]:
        env = os.environ.copy()
        env["DIRECTORCTL_DIRECTOR_TYPING_MAX_ATTEMPTS"] = str(self.director_typing_max_attempts)
        env["DIRECTORCTL_DIAGNOSTICS"] = "1"
        proc = subprocess.run(
            [self.directorctl_bin, "send", target, message],
            check=True,
            timeout=self.timeout_seconds,
            env=env,
            text=True,
            capture_output=True,
        )
        return parse_directorctl_diagnostic(proc.stdout)

    def submit(self, target: str) -> None:
        """Press submit on text already in the composer, typing nothing (SYRD-565)."""
        subprocess.run([self.directorctl_bin, "submit", target], check=True, timeout=self.timeout_seconds,
                       text=True, capture_output=True)


def parse_directorctl_diagnostic(output: str | None) -> dict[str, Any]:
    if not output:
        return {}
    for line in output.splitlines():
        prefix = "directorctl: diagnostic "
        if not line.startswith(prefix):
            continue
        try:
            parsed = json.loads(line[len(prefix):])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


#: How long a sent notice has to show up as a turn in the recipient's own
#: hook state before it is recorded as unconfirmed rather than delivered.
DEFAULT_SUBMISSION_CONFIRM_SECONDS = 15.0
DEFAULT_SUBMISSION_POLL_SECONDS = 0.25
SEND_UNCONFIRMED_EVENT = "send_unconfirmed"
# Why a send is unconfirmed: no turn started within the bound; the target has
# no hook state; the activity gate has no witness to ask.
NO_SUBMISSION_WITNESSED = "no_submission_witnessed"
NO_HOOK_STATE = "no_hook_state"
NO_SUBMISSION_WITNESS = "no_submission_witness"


#: Why a send failed when the fault is the BOARD'S routing, not the pane:
#: directorctl resolves a role's live target through the board's runtime
#: assignment and refuses when it cannot. The pane may be perfectly alive -- on
#: mefp it was, the whole time -- and the assignment comes back when the
#: declaration and the worker agree again, so this is retried, never
#: dead-lettered as a missing pane (SYRD-264).
RUNTIME_ASSIGNMENT_UNRESOLVED = "runtime_assignment_unresolved"
RUNTIME_ASSIGNMENT_MARKERS = (
    "cannot resolve runtime assignment",
    "runtime assignment for",
)


def delivery_error_output(exc: BaseException) -> str:
    """What the failed process SAID -- never the command line that ran it.

    A CalledProcessError's own text is "Command '[..., 'send', 'mefp-ops:0.0',
    ...]' returned non-zero exit status 1": it contains the target by
    construction. Reading that as evidence is how any failure whose output
    merely contained "not found" was classified as a missing tmux pane.
    """
    if isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        parts: list[str] = []
        for value in (exc.stderr, getattr(exc, "stdout", None) or getattr(exc, "output", None)):
            if isinstance(value, bytes):
                parts.append(value.decode("utf-8", errors="replace"))
            elif isinstance(value, str):
                parts.append(value)
        return "\n".join(part for part in parts if part)
    return str(exc)


def delivery_failure_reason(exc: BaseException, target: str) -> str:
    output = delivery_error_output(exc).lower()
    if any(marker in output for marker in RUNTIME_ASSIGNMENT_MARKERS):
        return RUNTIME_ASSIGNMENT_UNRESOLVED
    target_session = target.split(":", 1)[0].lower()
    missing_target_markers = (
        "can't find pane",
        "can't find window",
        "can't find session",
        "can't find client",
        "no such session",
        "session not found",
        "can't establish current session",
    )
    if any(marker in output for marker in missing_target_markers):
        return "tmux_target_missing"
    if target_session and target_session in output and "not found" in output:
        return "tmux_target_missing"
    return str(exc)


def tmux_target_exists(target: str, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> bool | None:
    try:
        proc = runner(
            ["tmux", "has-session", "-t", target],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    reason = delivery_failure_reason(
        subprocess.CalledProcessError(proc.returncode, proc.args, output=proc.stdout, stderr=proc.stderr),
        target,
    )
    if reason == "tmux_target_missing":
        return False
    return None


class NotificationDispatch:
    def __init__(
        self, *, logger: logging.Logger, ledger: NotificationLedger,
        sender: Callable[[], Callable[[str, str], object]],
        activity_gate: Callable[[], Callable[[str], bool]],
        submission_witness: Callable[[], Callable[[str, float], bool | None] | None],
        wall_clock: Callable[[], float], monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        submission_confirm_seconds: Callable[[], float],
        submission_poll_seconds: Callable[[], float],
    ) -> None:
        self.logger = logger
        self.ledger = ledger
        self.sender = sender
        self.activity_gate = activity_gate
        self.submission_witness = submission_witness
        self.wall_clock = wall_clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.submission_confirm_seconds = submission_confirm_seconds
        self.submission_poll_seconds = submission_poll_seconds

    def _composer_snapshot(self, target: str) -> ComposerSnapshot:
        gate_owner = getattr(self.activity_gate(), "__self__", None)
        composer_snapshot = getattr(gate_owner, "composer_snapshot", None)
        if callable(composer_snapshot):
            try:
                snapshot = composer_snapshot(target)
            except Exception as exc:
                return ComposerSnapshot(False, error=str(exc))
            if isinstance(snapshot, ComposerSnapshot):
                return snapshot
        return ComposerSnapshot(False, error="snapshot_unavailable")

    def _delivery_diagnostic_detail(
        self,
        *,
        target: str,
        message: str,
        attempts: int,
        activity_trace: Any,
        before: ComposerSnapshot,
        decision: str,
        reason: str,
        after: ComposerSnapshot | None = None,
        directorctl_diagnostic: dict[str, Any] | None = None,
        error_output: str = "",
    ) -> dict[str, Any]:
        after_detail = after.as_trace_detail() if after is not None else None
        composer_changed = (
            after is not None
            and before.available
            and after.available
            and before.content_sha256 != after.content_sha256
        )
        suspected_clobber = decision == "send" and before.active
        return {
            "target": target,
            "message": message,
            "attempts": attempts,
            # What the failed send actually printed. The reason above is a
            # classification of this, and mefp's dead letter kept only the
            # classification -- so "tmux_target_missing" could not be checked
            # against the text it was derived from (SYRD-264).
            **({"error_output": error_output[-1000:]} if error_output else {}),
            "anti_clobber": {
                "busy": activity_trace.busy,
                "reason": activity_trace.reason,
                "region_digest": activity_trace.region_digest,
            },
            "decision": decision,
            "decision_reason": reason,
            "composer_before": before.as_trace_detail(),
            "composer_after": after_detail,
            "composer_changed": composer_changed,
            "suspected_clobber": suspected_clobber,
            "directorctl": directorctl_diagnostic or {},
        }

    def _park_if_unsubmitted(self, conn: Any, gate: Any, before: str | None, *, notification_id: int, ticket_id: str,
                             target_role: str, kind: str, target: str, sent_at: float, reason: str,
                             error_output: str = "") -> bool:
        """SYRD-565: text still in the composer is never delivered; park it for the proof pass."""
        after = delivery_proof.read_composer(gate, target) if gate is not None else None
        if not delivery_proof.left_in_composer(before, after):
            return False
        self.logger.warning("Notification %s for %s is still in %s's composer (%s); parked, not delivered",
                            notification_id, ticket_id, target, reason)
        self.ledger.trace(conn, notification_id=notification_id, ticket_id=ticket_id, target_role=target_role,
                          kind=kind, event=delivery_proof.UNSUBMITTED_EVENT, pane_busy=False, busy_reason=reason,
                          region_digest="", detail={"composer_length": len(after or ""),
                                                    **({"error_output": error_output[-500:]} if error_output else {})})
        delivery_proof.park_unsubmitted(conn, notification_id, target, sent_at, after or "")
        self.ledger.forget(notification_id)
        return True

    def _await_submission(self, target: str, since: float) -> tuple[bool | None, str]:
        """Wait, bounded, for the recipient's own hooks to record a turn.

        (True, ""): a turn started after the send. Anything else is not
        receipt, and says why: (False, NO_SUBMISSION_WITNESSED) -- none within
        the bound; (None, NO_HOOK_STATE) -- the target has no hook state to
        read; (None, NO_SUBMISSION_WITNESS) -- this gate cannot be asked at all.
        "Cannot tell" stays None in the record, never either answer.
        """
        witnessed = self.submission_witness()
        if witnessed is None:
            gate_owner = getattr(self.activity_gate(), "__self__", None)
            witnessed = getattr(gate_owner, "submission_witnessed", None)
        if not callable(witnessed):
            return None, NO_SUBMISSION_WITNESS
        deadline = self.monotonic() + self.submission_confirm_seconds()
        while True:
            answer = witnessed(target, since)
            if answer is None:
                return None, NO_HOOK_STATE
            if answer:
                return True, ""
            if self.monotonic() >= deadline:
                return False, NO_SUBMISSION_WITNESSED
            self.sleeper(self.submission_poll_seconds())

    def send(
        self, conn: Any, *, notification_id: int, ticket_id: str,
        target_role: str, kind: str, target: str, message: str,
        payload: str, attempts: int, pane_busy: bool,
        activity_trace: Any, composer_before: ComposerSnapshot,
    ) -> bool:
        """Send once and classify receipt; never resend an unconfirmed composer."""
        directorctl_diagnostic: dict[str, Any] = {}
        # SYRD-565: submission is read from the composer itself, before and after.
        gate = delivery_proof.gate_of(self)
        composer_text_before = delivery_proof.read_composer(gate, target) if gate is not None else None
        send_started_at = self.wall_clock()
        try:
            sender_result = self.sender()(target, display_message(message))
            if isinstance(sender_result, dict):
                directorctl_diagnostic = sender_result
        except (subprocess.SubprocessError, OSError) as exc:
            if isinstance(exc, subprocess.CalledProcessError):
                stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
                directorctl_diagnostic = parse_directorctl_diagnostic(stdout if isinstance(stdout, str) else None)
            failure_reason = delivery_failure_reason(exc, target)
            error_output = delivery_error_output(exc)
            # directorctl can fail AFTER typing -- its own submit check gave up.
            # Requeued as a failure, the text would be typed a second time.
            if self._park_if_unsubmitted(conn, gate, composer_text_before, notification_id=notification_id,
                                         ticket_id=ticket_id, target_role=target_role, kind=kind, target=target,
                                         sent_at=send_started_at, reason="sender_failed_after_input",
                                         error_output=error_output):
                return False
            if failure_reason == "tmux_target_missing":
                self.logger.error(
                    "Dead-lettering ticket notification %s because target %s does not exist; role %s is undeliverable until its tmux session is restored",
                    notification_id,
                    target,
                    target_role,
                )
            else:
                self.logger.warning("Failed to deliver queued ticket notification through directorctl: %s", exc)
            composer_after = self._composer_snapshot(target)
            self.ledger.trace(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="send_failed",
                pane_busy=pane_busy,
                busy_reason=failure_reason,
                region_digest=activity_trace.region_digest,
                detail=self._delivery_diagnostic_detail(
                    target=target,
                    message=message,
                    attempts=attempts,
                    activity_trace=activity_trace,
                    before=composer_before,
                    after=composer_after,
                    decision="send_failed",
                    reason=failure_reason,
                    directorctl_diagnostic=directorctl_diagnostic,
                    error_output=error_output,
                ),
            )
            if failure_reason == "tmux_target_missing":
                self.ledger.dead_letter(
                    conn,
                    notification_id,
                    failure_reason,
                    target=target,
                    message=message,
                    attempts=attempts,
                    payload=payload,
                    error_output=error_output,
                )
            else:
                # Including runtime_assignment_unresolved: the board's
                # routing for this role will return, and the notice is
                # delivered then rather than lost now (SYRD-264).
                self.ledger.requeue(conn, notification_id, attempts, failure_reason)
            return False
        composer_after = self._composer_snapshot(target)
        if self._park_if_unsubmitted(conn, gate, composer_text_before, notification_id=notification_id,
                                     ticket_id=ticket_id, target_role=target_role, kind=kind, target=target,
                                     sent_at=send_started_at, reason="text_left_in_composer"):
            return False
        # directorctl returning 0 is not delivery: its check is what the
        # pane looks like, and a pane already busy on another turn looks
        # submitted whatever happened -- so MEFP-1's Final Sign-Off notice
        # was recorded delivered and never seen (SYRD-268). Delivered now
        # needs the recipient's own hooks to have recorded a turn.
        # "Cannot tell" is not receipt either: without the recipient's
        # own record the notice is unconfirmed, whatever the reason.
        submission, unconfirmed_reason = self._await_submission(target, send_started_at)
        if submission is False and gate is not None and delivery_proof.read_composer(gate, target) == "":
            # SYRD-565: the text left the composer and no turn has started yet --
            # a slow preflight looks exactly like this. The notice is in, so it is
            # acknowledged with its receipt still watched, not settled as missed.
            unconfirmed_reason = delivery_proof.RECEIPT_PENDING
        if submission is not True:
            self.logger.warning(
                "Notification %s for %s was sent to %s but its receipt is not witnessed (%s); "
                "recording it unconfirmed, not delivered",
                notification_id, ticket_id, target, unconfirmed_reason,
            )
            unconfirmed_detail = self._delivery_diagnostic_detail(
                target=target,
                message=message,
                attempts=attempts,
                activity_trace=activity_trace,
                before=composer_before,
                after=composer_after,
                decision=SEND_UNCONFIRMED_EVENT,
                reason=unconfirmed_reason,
                directorctl_diagnostic=directorctl_diagnostic,
            )
            unconfirmed_detail["submission"] = {
                "witnessed": submission,
                "waited_seconds": self.submission_confirm_seconds(),
                "since": send_started_at,
            }
            self.ledger.trace(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event=SEND_UNCONFIRMED_EVENT,
                pane_busy=pane_busy,
                busy_reason=unconfirmed_reason,
                region_digest=activity_trace.region_digest,
                detail=unconfirmed_detail,
            )
            # Not re-sent automatically: the text may be sitting in the
            # composer, and typing it again would put it there twice. The
            # board reports it unconfirmed; the owner of the stage decides.
            if unconfirmed_reason == delivery_proof.RECEIPT_PENDING:
                delivery_proof.watch_receipt(conn, notification_id, target, send_started_at, send_started_at)
            else:
                self.ledger.ack(conn, notification_id)
            self.ledger.forget(notification_id)
            return False
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="send",
            pane_busy=pane_busy,
            busy_reason=activity_trace.reason,
            region_digest=activity_trace.region_digest,
            detail=self._delivery_diagnostic_detail(
                target=target,
                message=message,
                attempts=attempts,
                activity_trace=activity_trace,
                before=composer_before,
                after=composer_after,
                decision="send",
                reason=activity_trace.reason,
                directorctl_diagnostic=directorctl_diagnostic,
            ) | {"submission": {"witnessed": submission, "since": send_started_at}},
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="listener_ack",
            pane_busy=pane_busy,
            busy_reason=activity_trace.reason,
            region_digest=activity_trace.region_digest,
            detail={"target": target},
        )
        self.ledger.ack(conn, notification_id)
        self.ledger.forget(notification_id)
        self.logger.info("Delivered queued notification %s for %s to %s: %s", notification_id, ticket_id, target, payload)
        return True
