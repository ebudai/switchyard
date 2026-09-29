"""LISTEN shim for PostgreSQL ticket-board transition notifications."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import psycopg
from psycopg import sql

from .pane_activity_gate import (
    ChildWorkMemory,
    ChildWorkSample,
    DEFAULT_BUSY_REQUEUE_SECONDS,
    DEFAULT_CHILD_WORK_CPU_TICKS,
    DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS,
    DEFAULT_DIRECTOR_COMPOSER_HOME_X,
    DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS,
    DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS,
    DEFAULT_PROJECT,
    DEFAULT_ROLE_RUNTIMES,
    DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS,
    DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS,
    IDLE_TURN_END_SOURCES,
    MIN_RECOVERABLE_HOOK_EPOCH_SECONDS,
    PERMISSION_PROMPT_BLOCK_SOURCES,
    PaneActivityGate,
    ROLE_TO_TARGET,
    STALE_PRIOR_TURN_CHILD_WORK,
    TRUSTED_IDLE_SOURCES,
    WorkingProbe,
    _boot_time_epoch_seconds,
    composer_snapshot_from_pane_text,
    descendant_work_sample,
    pane_content_digest,
    read_process_table,
)
from .idle_nudges import IdleNudges
from .notification_ledger import NotificationLedger
from .notification_session_clear import (
    NotificationSessionClear, SESSION_CLEAR_COMMANDS, SESSION_CLEAR_KINDS,
    SESSION_CLEAR_FAILED_ERROR, SESSION_CLEAR_UNSUPPORTED_RUNTIME_ERROR,
    DEFAULT_SESSION_CLEAR_SETTLE_SECONDS,
)
from .notification_eligibility import (
    NotificationEligibility,
    TERMINAL_STATES,
    NUDGE_ELIGIBLE_STATES,
    SUPERSEDABLE_REMINDER_KINDS,
    DIRECTOR_BOUND_KINDS,
    SUPERSEDED_BY_AWAITING_ROLE,
    LEGACY_SERIAL_STAGE,
    LEGACY_NON_SERIAL_ROLES,
    SUPERSEDED_QUEUE_NOTICE,
    UNNAMED_RESERVATION,
)
from .pane_state import (
    DEFAULT_PANE_STATE_DIR,
    PaneHookState,
    PaneHookStateStore,
    PaneStateAuthority,
    TURN_START_EVENTS,
    _pid_exists,
    assignment_process_gone,
    hook_turn_started_at,
    live_pane_targets,
    load_runtime_assignments,
    pane_state_authority,
    registered_pane_targets,
    verify_pane_state_authority,
)
from .peer_identity import PROC_ROOT, SessionIdentity, read_process, session_is_live
from .runtime_paths import directorctl_path

CHANNEL = "ticket_board_state_transition"
DEFAULT_DATABASE_URL = (
    os.environ.get("TICKET_BOARD_NOTIFY_DATABASE_URL")
    or os.environ.get("TICKET_BOARD_DATABASE_URL")
    or os.environ.get("DATABASE_URL", "")
)
DEFAULT_RECONNECT_SECONDS = 2.0
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_KEEPALIVES_IDLE_SECONDS = 30
DEFAULT_KEEPALIVES_INTERVAL_SECONDS = 10
DEFAULT_KEEPALIVES_COUNT = 3
DEFAULT_DIRECTORCTL_SEND_TIMEOUT_SECONDS = 10.0
DEFAULT_REQUEUE_BASE_SECONDS = 5.0
DEFAULT_REQUEUE_MAX_SECONDS = 300.0
# These strings are persisted in ticket_notification_queue.last_error and must
# match schema.sql reset predicates.
PANE_BUSY_REQUEUE_ERROR = "pane busy"
FINISH_CURRENT_REQUEUE_ERROR = "finish current"
DEFAULT_IDLE_STALL_GRACE_SECONDS = 45.0
DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS = 30 * 60.0
DEFAULT_IDLE_STALL_ESCALATE_AFTER = 2
# Present-idle fallback is age-unbounded: an idle hook remains valid until the
# pane reports busy/blocked again, and the live activity gate still runs before
# any send.
DEFAULT_PRESENT_IDLE_FRESHNESS_SECONDS = 0.0
# Nothing bypasses the activity gate any more. A ticket_update used to, on the
# reasoning that a pane working *this* ticket wants to know it changed. In
# practice that is precisely the pane mid-turn, and the update landed in a
# running composer: SYRD-32 saw it as an Inspector reminder, SYRD-46 saw the
# same delivery interrupt Main mid-implementation. An update is not lost by
# waiting -- it is requeued and retried once the role is idle.
IMMEDIATE_DELIVERY_KINDS: frozenset[str] = frozenset()
# Reminders addressed to the same role they are about. Their whole claim is
# "you appear idle on this ticket", so observing that role work voids them.
# 'escalation' is deliberately absent: it goes to the director about someone
# else's stall, so the director's own pane says nothing about that stall.
SELF_REMINDER_KINDS = frozenset({"nudge", "idle_reminder"})
#: A runtime is named one way in the workflow document and another by the hook
#: source it writes. This is the whole of that vocabulary difference, in one
#: place, so neither name is special-cased at a decision site.
HOOK_RUNTIME_NAMES = {"agy": "gemini"}
DEFAULT_PRE_SEND_RECHECK_DELAY_SECONDS = 0.5
#: How many consecutive probes a prior turn's descendant must keep using CPU on
#: before it is believed to be progressing rather than waking. See
#: `ORPHANED_PRIOR_TURN_WAITER` (SYRD-212).
#: How long a handoff may wait behind a FINISHED turn's descendants before it
#: is delivered anyway and the wait is escalated in the record. Fifteen minutes:
#: long enough that an ordinary verification run started before the turn-end
#: hook finishes inside it, short enough that nobody loses most of an hour to a
#: poll loop nothing will ever satisfy. The live stall ran 38 minutes and was
#: ended by a human (SYRD-212).
DEFAULT_PRIOR_TURN_HOLD_MAX_SECONDS = 900.0
STATE_RANK = {
    "backlog": 0,
    "analysis": 1,
    "in_progress": 3,
    "inspection": 4,
    "audit": 5,
    "dat": 6,
    "user_review": 7,
    "director_review": 8,
    "done": 9,
    "cancelled": 10,
}
#: Pane-hook sources that mean a prompt is on screen with nobody to answer it.
#: Deliberately not in TRUSTED_IDLE_SOURCES: a pane waiting on a prompt is not
#: idle, and treating it as idle would deliver into a stopped pane (SYRD-234).
#: Seconds a pane may sit on a prompt before the Director hears about it.
PERMISSION_PROMPT_GRACE_SECONDS = 120
#: A descendant of a finished turn that is still spending CPU, but only in the
#: bursts a poll loop spends it in: awake on one probe, asleep on the next.
#:
#: SYRD-101 classified a prior turn's leftovers by lifecycle and made an
#: exception for progress, so that an hour-long sweep from before the turn-end
#: hook keeps its pane quiet. The exception was tested only for whether ANY
#: survivor's CPU had risen, and a shell polling a task-output file every ten
#: seconds raises it too. So the exception swallowed the rule: routing SYRD-211
#: to App on 2026-09-18, notification 2842 sat undelivered for 38 minutes
#: against a pane whose trusted hook had said idle at 21:02, because a SYRD-210
#: poll loop started at 20:49 woke often enough to look like work (SYRD-212).
#:
#: The difference is not what the process is called; it is whether it is making
#: progress or waiting. A build advances on every probe. A waiter advances on
#: some and not others, which is what this reason names.
ORPHANED_PRIOR_TURN_WAITER = "orphaned_prior_turn_waiter"
#: A prior turn's descendant that IS progressing: CPU rising on consecutive
#: probes, not in bursts. Held as work, exactly as SYRD-101 requires, but said
#: with its own name so that a trace can tell it apart from this turn's work
#: and from a waiter (SYRD-212).
PRIOR_TURN_CHILD_WORK = "prior_turn_child_work"
#: The holds a later handoff may not wait behind indefinitely: both come from a
#: turn the runtime has already declared finished. `pane_child_work` is absent
#: on purpose -- that is the CURRENT turn working, and interrupting it is what
#: the activity gate exists to prevent.
PRIOR_TURN_HOLD_REASONS = frozenset({PRIOR_TURN_CHILD_WORK, ORPHANED_PRIOR_TURN_WAITER})
#: Why a queued handoff was dropped instead of delivered: the owner reached the
#: ticket without it, so delivering would hand them the same assignment twice.
OWNER_ALREADY_ACTED = "owner_already_acted"
PRIOR_TURN_HOLD_DELIVER = "deliver"
PRIOR_TURN_HOLD_DISCARD = "discard"
LOGGER = logging.getLogger(__name__)
DEFAULT_DIRECTORCTL = directorctl_path(__file__)
ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ACCOUNT_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def configured_role_accounts(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Parse the root-installed legacy role authority map, failing closed.

    Process-authority projects deliberately carry no such map.  Older
    per-role-account units do, and both notification delivery and its activity
    probes must consult the same declarative mapping instead of guessing an
    account from a tmux target (SYRD-66).
    """
    env = os.environ if environ is None else environ
    raw = str(env.get("TICKET_BOARD_ROLE_ACCOUNTS") or "").strip()
    if not raw:
        return {}
    accounts: dict[str, str] = {}
    for item in raw.split(","):
        fields = item.strip().split("=", 1)
        if len(fields) != 2:
            raise ValueError("TICKET_BOARD_ROLE_ACCOUNTS must contain role=account entries")
        role, account = (field.strip() for field in fields)
        if not ROLE_NAME_RE.fullmatch(role) or not ACCOUNT_NAME_RE.fullmatch(account):
            raise ValueError(f"invalid TICKET_BOARD_ROLE_ACCOUNTS entry {item!r}")
        if role in accounts:
            raise ValueError(f"duplicate TICKET_BOARD_ROLE_ACCOUNTS role {role!r}")
        accounts[role] = account
    return accounts


def role_aware_tmux_runner(
    *,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Route legacy tmux probes through their configured Unix authority.

    The wrapper accepts only tmux argv.  A configured map makes unknown,
    cross-project and target-less calls errors rather than silently probing the
    project owner's server.  With no map, the argv is unchanged so an existing
    shared-account tenant needs no new artifact.
    """
    env = os.environ if environ is None else environ
    process_authority = str(env.get("TICKET_BOARD_PROCESS_AUTHORITY") or "").strip() == "1"
    accounts = {} if process_authority else configured_role_accounts(env)
    project = str(env.get("TICKET_BOARD_PROJECT") or env.get("PGU_TICKET_BOARD_PROJECT") or "").strip()

    def refused(
        args: list[str], message: str, *, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if check:
            raise subprocess.CalledProcessError(
                125, args, output="", stderr=message
            )
        return subprocess.CompletedProcess(args, 125, stdout="", stderr=message)

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if not accounts:
            return runner(args, **kwargs)
        if not args or args[0] != "tmux":
            return refused(
                args, "role-aware runner accepts only tmux",
                check=bool(kwargs.get("check")),
            )
        target = ""
        for flag in ("-t", "-s"):
            if flag in args:
                index = args.index(flag) + 1
                if index < len(args):
                    target = str(args[index]).lstrip("=")
                    break
        session = target.split(":", 1)[0]
        prefix = f"{project}-"
        if not project or not session.startswith(prefix):
            return refused(
                args, f"refusing foreign tmux target {target!r}",
                check=bool(kwargs.get("check")),
            )
        role = session[len(prefix) :]
        account = accounts.get(role)
        if not account:
            return refused(
                args, f"no configured role account for tmux target {target!r}",
                check=bool(kwargs.get("check")),
            )
        return runner(
            ["sudo", "-n", "-u", account, "/usr/bin/tmux", *args[1:]],
            **kwargs,
        )

    return run
WORK_EVIDENCE_REASONS = frozenset(
    {
        "hook_busy",
        "pane_content_changed",
        "working_timer",
        "human_composing",
        "foreign_runtime_pane_content_changed",
        "foreign_runtime_working_timer",
        # A turn whose verification is still running is work, whether or not
        # anything reaches the screen (SYRD-58).
        "pane_child_work",
        # The same, from a turn that has already ended: still work, still
        # holding, and distinguishable in a trace (SYRD-212).
        PRIOR_TURN_CHILD_WORK,
    }
)


@dataclass(frozen=True)
class Transition:
    ticket_id: str
    title: str
    old_state: str
    new_state: str
    assignee: str
    message: str = ""
    target_role: str = ""
    kind: str = "transition"


@dataclass(frozen=True)
class ActivityTrace:
    busy: bool
    reason: str
    region_digest: str = ""


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



def parse_transition_payload(payload: str) -> Transition:
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("notification payload must be a JSON object")
    return Transition(
        ticket_id=str(parsed.get("id", "")).strip().upper(),
        title=str(parsed.get("title", "")).strip(),
        old_state=str(parsed.get("old_state", "")).strip(),
        new_state=str(parsed.get("new_state", "")).strip(),
        assignee=str(parsed.get("assignee", "")).strip().lower(),
        message=str(parsed.get("message", "")).strip(),
        target_role=str(parsed.get("target_role", "")).strip().lower(),
        kind=str(parsed.get("kind", "transition")).strip().lower() or "transition",
    )


def target_for_transition(transition: Transition) -> str | None:
    if transition.target_role:
        return ROLE_TO_TARGET.get(transition.target_role)
    if transition.new_state == "analysis":
        return ROLE_TO_TARGET["director"]
    if transition.new_state == "in_progress":
        return ROLE_TO_TARGET.get(transition.assignee)
    if transition.new_state == "inspection":
        return ROLE_TO_TARGET["inspector"]
    if transition.new_state == "audit":
        return ROLE_TO_TARGET["audit"]
    if transition.new_state == "dat":
        return ROLE_TO_TARGET["director"]
    if transition.new_state == "user_review":
        return None
    if transition.new_state == "director_review":
        return ROLE_TO_TARGET["director"]
    return None


def message_for_transition(transition: Transition) -> str | None:
    """Fallback wording for a payload that carries no message of its own.

    This is NOT where live notifications get their text. The queued path takes
    `message` straight from the row, and that row is written by
    ticket_board.transition_message in schema.sql -- which is why PGU-912's
    re-entry wording lives there and not here. Changing this function alone
    changes nothing a pane will ever see; a fix applied here would pass every
    test and fix no behaviour.

    Nor can this function distinguish first entry from re-entry: it is pure on
    the payload, and the payload carries no delivery history.
    """
    if target_for_transition(transition) is None:
        return None
    if not transition.ticket_id:
        raise ValueError("notification payload missing ticket id")
    if transition.message:
        return transition.message
    title_suffix = f" -- {transition.title}" if transition.title else ""
    old_rank = STATE_RANK.get(transition.old_state)
    new_rank = STATE_RANK.get(transition.new_state)
    if old_rank is not None and new_rank is not None and new_rank < old_rank:
        return f"{transition.ticket_id}{title_suffix} kicked back to you"
    if transition.new_state == "analysis":
        return f"New ticket for you: {transition.ticket_id}{title_suffix}"
    if transition.new_state == "in_progress":
        return f"New ticket for you: {transition.ticket_id}{title_suffix}"
    if transition.new_state == "inspection":
        return f"{transition.ticket_id}{title_suffix} ready for inspection"
    if transition.new_state == "audit":
        return f"{transition.ticket_id}{title_suffix} ready for audit"
    if transition.new_state == "dat":
        return f"{transition.ticket_id}{title_suffix} ready for Director Acceptance Testing"
    if transition.new_state == "user_review":
        return f"{transition.ticket_id}{title_suffix} ready for User UAT"
    if transition.new_state == "director_review":
        return f"{transition.ticket_id}{title_suffix} ready for your review"
    return None


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


#: Why a send failed when the fault is the BOARD'S routing, not the pane:
#: directorctl resolves a role's live target through the board's runtime
#: assignment and refuses when it cannot. The pane may be perfectly alive -- on
#: mefp it was, the whole time -- and the assignment comes back when the
#: declaration and the worker agree again, so this is retried, never
#: dead-lettered as a missing pane (SYRD-264).
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


#: The owner's window to answer their own unresolved-turn prompt before the
#: Director hears about it. Long enough that an ordinary forgotten turn is
#: repaired by the person who forgot it, short enough that a genuinely stalled
#: ticket still reaches the Director the User would otherwise have to notice
#: for them (SYRD-203).
UNRESOLVED_TURN_GRACE_SECONDS = 600


class TicketBoardNotifyListener:
    def __init__(
        self,
        *,
        conninfo: str,
        project: str = DEFAULT_PROJECT,
        channel: str = CHANNEL,
        sender: Callable[[str, str], None] | None = None,
        activity_gate: Callable[[str], bool] | None = None,
        connector: Callable[..., Any] = psycopg.connect,
        reconnect_seconds: float = DEFAULT_RECONNECT_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        requeue_base_seconds: float = DEFAULT_REQUEUE_BASE_SECONDS,
        requeue_max_seconds: float = DEFAULT_REQUEUE_MAX_SECONDS,
        busy_requeue_seconds: float = DEFAULT_BUSY_REQUEUE_SECONDS,
        prior_turn_hold_max_seconds: float = DEFAULT_PRIOR_TURN_HOLD_MAX_SECONDS,
        #: Injectable so a fixture can age a handoff without sleeping. Monotonic
        #: because this measures how long a wait has lasted, and a wall clock
        #: that steps backwards would shorten or erase it.
        monotonic: Callable[[], float] = time.monotonic,
        idle_stall_grace_seconds: float = DEFAULT_IDLE_STALL_GRACE_SECONDS,
        idle_stall_nudge_cadence_seconds: float = DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS,
        idle_stall_escalate_after: int = DEFAULT_IDLE_STALL_ESCALATE_AFTER,
        present_idle_freshness_seconds: float = DEFAULT_PRESENT_IDLE_FRESHNESS_SECONDS,
        pre_send_recheck_delay_seconds: float = 0.0,
        session_clear_settle_seconds: float = DEFAULT_SESSION_CLEAR_SETTLE_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        stop_event: threading.Event | None = None,
        logger: logging.Logger = LOGGER,
        target_exists: Callable[[str], bool | None] | None = None,
        #: Hook timestamps are epoch seconds written by the pane's CLI, so the
        #: moment a send started is taken from the same clock (SYRD-268).
        wall_clock: Callable[[], float] = time.time,
        submission_confirm_seconds: float = DEFAULT_SUBMISSION_CONFIRM_SECONDS,
        submission_poll_seconds: float = DEFAULT_SUBMISSION_POLL_SECONDS,
        submission_witness: Callable[[str, float], bool | None] | None = None,
    ) -> None:
        self.role_targets = dict(ROLE_TO_TARGET)
        self.workflow = None
        # Both read from the declared document on every refresh: which roles
        # start each ticket cleared, and which CLI each of them is running.
        self.ephemeral_roles: set[str] = set()
        self.role_runtimes: dict[str, str] = {}
        self.project = project
        self.conninfo = conninfo
        self.channel = channel
        self.sender = sender or DirectorctlSender()
        self.activity_gate = activity_gate or PaneActivityGate().is_working
        self.connector = connector
        self.reconnect_seconds = reconnect_seconds
        self.poll_seconds = poll_seconds
        self.busy_requeue_seconds = busy_requeue_seconds
        self.prior_turn_hold_max_seconds = max(0.0, prior_turn_hold_max_seconds)
        self.monotonic = monotonic
        #: When each still-queued handoff first waited behind a finished turn's
        #: descendants. Keyed by notification, because the bound belongs to the
        #: handoff that is waiting rather than to the pane it waits on.
        self._prior_turn_hold_started_at: dict[int, float] = {}
        # Deprecated compatibility knob: PGU-562 removed the present-idle age
        # cutoff because the live activity gate is the delivery safety check.
        self.present_idle_freshness_seconds = max(0.0, present_idle_freshness_seconds)
        self.pre_send_recheck_delay_seconds = max(0.0, pre_send_recheck_delay_seconds)
        self.session_clear_settle_seconds = max(0.0, session_clear_settle_seconds)
        self.sleeper = sleeper
        self.wall_clock = wall_clock
        self.submission_confirm_seconds = max(0.0, submission_confirm_seconds)
        self.submission_poll_seconds = max(0.01, submission_poll_seconds)
        # Who says a notice was taken: by default the activity gate's own
        # reading of the recipient's hook state (SYRD-268).
        self.submission_witness = submission_witness
        self.stop_event = stop_event or threading.Event()
        self.logger = logger
        self.target_exists = target_exists or tmux_target_exists
        self.delivered_count = 0
        # What the listener writes to the board about each notification, and
        # which deferrals it has already traced (SYRD-484).
        self.ledger = NotificationLedger(
            logger=logger,
            requeue_base_seconds=requeue_base_seconds,
            requeue_max_seconds=requeue_max_seconds,
        )
        self.eligibility = NotificationEligibility(
            logger=logger,
            ledger=self.ledger,
            workflow=lambda: self.workflow,
            decode_text=lambda value: self._decode_text(value),
        )
        self.session_clear = NotificationSessionClear(
            logger=logger,
            ledger=self.ledger,
            role_runtimes=lambda: self.role_runtimes,
            ephemeral_roles=lambda: self.ephemeral_roles,
            announced_queue_identity=lambda payload: self.eligibility._announced_queue_identity(payload),
            sender=lambda: self.sender,
            failure_reason=delivery_failure_reason,
            sleeper=sleeper,
            settle_seconds=lambda: self.session_clear_settle_seconds,
        )
        # The per-pass reminder generators, and the turn boundaries they have
        # already acted on (SYRD-480). They read the gate and the role targets
        # through this listener when a pass runs, because both are rebound.
        self.idle_nudges = IdleNudges(
            activity_gate=lambda: self.activity_gate,
            role_targets=lambda: self.role_targets,
            logger=logger,
            idle_stall_grace_seconds=idle_stall_grace_seconds,
            idle_stall_nudge_cadence_seconds=idle_stall_nudge_cadence_seconds,
            idle_stall_escalate_after=idle_stall_escalate_after,
            # How long an owner has to answer their own repair prompt before the
            # Director is told. Staged recovery, not dual delivery: one unresolved
            # turn is first of all news for the person who can resolve it
            # (SYRD-203).
            unresolved_turn_grace_seconds=int(
                os.environ.get("TICKET_BOARD_UNRESOLVED_TURN_GRACE_SECONDS", "") or
                UNRESOLVED_TURN_GRACE_SECONDS
            ),
            # How long a pane may sit on a permission prompt before the Director is
            # told. Short, because unlike an unresolved turn there is nobody in the
            # pane who can clear it: the role is stopped, not slow (SYRD-234).
            permission_prompt_grace_seconds=int(
                os.environ.get("TICKET_BOARD_PERMISSION_PROMPT_GRACE_SECONDS", "") or
                PERMISSION_PROMPT_GRACE_SECONDS
            ),
        )

    @property
    def idle_stall_grace_seconds(self) -> float:
        return self.idle_nudges.idle_stall_grace_seconds

    @idle_stall_grace_seconds.setter
    def idle_stall_grace_seconds(self, value: float) -> None:
        self.idle_nudges.idle_stall_grace_seconds = value

    @property
    def idle_stall_nudge_cadence_seconds(self) -> float:
        return self.idle_nudges.idle_stall_nudge_cadence_seconds

    @idle_stall_nudge_cadence_seconds.setter
    def idle_stall_nudge_cadence_seconds(self, value: float) -> None:
        self.idle_nudges.idle_stall_nudge_cadence_seconds = value

    @property
    def idle_stall_escalate_after(self) -> int:
        return self.idle_nudges.idle_stall_escalate_after

    @idle_stall_escalate_after.setter
    def idle_stall_escalate_after(self, value: int) -> None:
        self.idle_nudges.idle_stall_escalate_after = value

    @property
    def unresolved_turn_grace_seconds(self) -> int:
        return self.idle_nudges.unresolved_turn_grace_seconds

    @unresolved_turn_grace_seconds.setter
    def unresolved_turn_grace_seconds(self, value: int) -> None:
        self.idle_nudges.unresolved_turn_grace_seconds = value

    @property
    def permission_prompt_grace_seconds(self) -> int:
        return self.idle_nudges.permission_prompt_grace_seconds

    @permission_prompt_grace_seconds.setter
    def permission_prompt_grace_seconds(self, value: int) -> None:
        self.idle_nudges.permission_prompt_grace_seconds = value

    @property
    def requeue_base_seconds(self) -> float:
        return self.ledger.requeue_base_seconds

    @requeue_base_seconds.setter
    def requeue_base_seconds(self, value: float) -> None:
        self.ledger.requeue_base_seconds = value

    @property
    def requeue_max_seconds(self) -> float:
        return self.ledger.requeue_max_seconds

    @requeue_max_seconds.setter
    def requeue_max_seconds(self, value: float) -> None:
        self.ledger.requeue_max_seconds = value

    def _connector_kwargs(self) -> dict[str, int | bool]:
        return {
            "autocommit": True,
            "connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS,
            "keepalives": 1,
            "keepalives_idle": DEFAULT_KEEPALIVES_IDLE_SECONDS,
            "keepalives_interval": DEFAULT_KEEPALIVES_INTERVAL_SECONDS,
            "keepalives_count": DEFAULT_KEEPALIVES_COUNT,
        }

    def deliver_payload(self, payload: str) -> bool:
        transition = parse_transition_payload(payload)
        target = target_for_transition(transition)
        message = message_for_transition(transition)
        if not target or not message:
            self.logger.debug("Skipping transition notification: %s", payload)
            return False
        if self.activity_gate(target):
            self.logger.info("Deferred notification for active pane %s", target)
            return False
        # The message and nothing else. The skills are installed for every
        # runtime and their own trigger descriptions name a ticket, so a pointer
        # appended here only repeats what the catalog already says, to a pane
        # that is about to read the ticket anyway (SYRD-106).
        self.sender(target, display_message(message))
        self.delivered_count += 1
        self.logger.info("Delivered %s transition to %s", transition.ticket_id, target)
        return True

    def reconcile_pending_notifications(self, conn: Any, *, max_notifications: int | None = None) -> int:
        return self.process_due_notifications(conn, max_notifications=max_notifications)

    def _claim_notification(self, conn: Any) -> tuple[int, str, str, str, str, int] | None:
        result = conn.execute(
            """
SELECT notification_id, ticket_id, target_role, message, payload::text, attempts
FROM ticket_board.claim_notification()
"""
        )
        row = result.fetchone()
        if row is None:
            return None
        notification_id, ticket_id, target_role, message, payload, attempts = row
        return (
            int(notification_id),
            self._decode_text(ticket_id),
            self._decode_text(target_role),
            self._decode_text(message),
            self._decode_text(payload),
            int(attempts),
        )

    def _decode_text(self, value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)

    def _payload_kind(self, payload: str) -> str:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return str(parsed.get("kind") or "").strip().lower()

    def _activity_trace(self, target: str, pane_busy: bool) -> ActivityTrace:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        last_trace = getattr(gate_owner, "last_trace", None)
        if callable(last_trace):
            trace = last_trace(target)
            if isinstance(trace, ActivityTrace):
                return trace
        return ActivityTrace(pane_busy, "busy" if pane_busy else "idle")

    def _composer_snapshot(self, target: str) -> ComposerSnapshot:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        composer_snapshot = getattr(gate_owner, "composer_snapshot", None)
        if callable(composer_snapshot):
            try:
                snapshot = composer_snapshot(target)
            except Exception as exc:
                return ComposerSnapshot(False, error=str(exc))
            if isinstance(snapshot, ComposerSnapshot):
                return snapshot
        return ComposerSnapshot(False, error="snapshot_unavailable")

    def _activity_state_for_notification(self, kind: str, target: str, *, pre_send_recheck: bool = False) -> tuple[bool, ActivityTrace]:
        """Whether this destination is working, for any kind and any role.

        One gate for everything. The anti-clobber gate reports a working agent
        as free -- it only guards a human's half-typed line -- so any kind
        allowed to use it is a kind that can be typed into a running turn. That
        exemption is what put "please read /tmp/directorctl_payload..." into
        Main's composer mid-implementation, and it is gone.

        Nothing here reads a role name, a project, a port or a CLI name. The
        activity gate resolves the destination's runtime from the declared
        workflow and reads that runtime's own hook state, so a role added or
        re-hosted later is covered without being named.
        """
        if pre_send_recheck:
            gate_owner = getattr(self.activity_gate, "__self__", None)
            pre_send_full_busy = getattr(gate_owner, "pre_send_busy", None)
            if callable(pre_send_full_busy):
                pane_busy = bool(pre_send_full_busy(target))
                return pane_busy, self._activity_trace(target, pane_busy)
        pane_busy = self.activity_gate(target)
        return pane_busy, self._activity_trace(target, pane_busy)

    def _should_defer_for_activity(self, activity_trace: ActivityTrace) -> bool:
        return activity_trace.busy

    def _reminder_is_stale_for_activity(self, kind: str, activity_trace: ActivityTrace) -> bool:
        """Whether a queued self-directed reminder has been voided by real work.

        Requeueing such a reminder behind a busy pane only re-delivers a claim
        that is already false, which is how SYRD-32 was seen live: the Inspector
        was mid-review and still received "you appear idle". The idle generators
        raise a fresh reminder if the owner genuinely stalls again, so dropping
        loses nothing. Only work evidence counts -- a pane held busy for some
        other reason still requeues.
        """
        return kind in SELF_REMINDER_KINDS and activity_trace.reason in WORK_EVIDENCE_REASONS

    def _drop_stale_reminder(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        target: str,
        message: str,
        attempts: int,
        activity_trace: ActivityTrace,
        composer_before: ComposerSnapshot,
        phase: str,
    ) -> None:
        self.logger.info(
            "Dropping stale %s notification %s for %s: %s is working (%s)",
            kind,
            notification_id,
            ticket_id,
            target,
            activity_trace.reason,
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="drop",
            pane_busy=True,
            busy_reason="stale_reminder_work_observed",
            region_digest=activity_trace.region_digest,
            detail={
                **self._delivery_diagnostic_detail(
                    target=target,
                    message=message,
                    attempts=attempts,
                    activity_trace=activity_trace,
                    before=composer_before,
                    decision="drop",
                    reason="stale_reminder_work_observed",
                ),
                "phase": phase,
            },
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="listener_discard",
            detail={"reason": "stale_reminder_work_observed", "phase": phase},
        )
        # Never ack here: this reminder was suppressed, not delivered, and
        # ack_notification would credit it as a completed reminder round.
        self.ledger.discard(conn, notification_id, "stale_reminder_work_observed")
        self.ledger.forget(notification_id)

    def _delivery_diagnostic_detail(
        self,
        *,
        target: str,
        message: str,
        attempts: int,
        activity_trace: ActivityTrace,
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

    def _release_prior_turn_hold(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        activity_trace: ActivityTrace,
    ) -> str:
        """Decide whether a handoff has waited behind a finished turn too long.

        Returns "" to leave the ordinary gate alone, `PRIOR_TURN_HOLD_DISCARD`
        when the owner has already reached this ticket without us, and
        `PRIOR_TURN_HOLD_DELIVER` when the wait is over the bound and the
        handoff should go out with the wait recorded against it.

        Only holds from a turn that has ENDED are bounded. This turn's own work
        is not: interrupting it is precisely what the activity gate exists to
        prevent, and no clock here may override that.
        """
        if activity_trace is None or activity_trace.reason not in PRIOR_TURN_HOLD_REASONS:
            # Whatever is holding it now, it is not a finished turn's leftovers.
            # Forgetting the wait is deliberate: a pane that went back to work
            # for its CURRENT turn starts the bound again if it later falls back
            # to a prior-turn hold.
            self._prior_turn_hold_started_at.pop(notification_id, None)
            return ""
        now = self.monotonic()
        started = self._prior_turn_hold_started_at.setdefault(notification_id, now)
        held = now - started
        if held < self.prior_turn_hold_max_seconds:
            return ""
        notified_at = self._owner_already_notified_at(conn, ticket_id, target_role)
        if notified_at:
            self.eligibility._drop_superseded_notification(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                detail={
                    "held_seconds": round(held, 1),
                    "held_reason": activity_trace.reason,
                    "owner_notified_at": notified_at,
                },
                phase="prior_turn_hold",
                reason=OWNER_ALREADY_ACTED,
            )
            self._prior_turn_hold_started_at.pop(notification_id, None)
            return PRIOR_TURN_HOLD_DISCARD
        self.logger.warning(
            "Delivering notification %s for %s past a finished turn's %s held %.0fs",
            notification_id, ticket_id, activity_trace.reason, held,
        )
        self.ledger.trace(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="gate_escalate",
            pane_busy=True,
            busy_reason=ORPHANED_PRIOR_TURN_WAITER,
            detail={
                "held_seconds": round(held, 1),
                "held_reason": activity_trace.reason,
                "bound_seconds": self.prior_turn_hold_max_seconds,
            },
        )
        self._prior_turn_hold_started_at.pop(notification_id, None)
        return PRIOR_TURN_HOLD_DELIVER

    def _owner_already_notified_at(self, conn: Any, ticket_id: str, target_role: str) -> str:
        """When this ticket's owner was last sent this assignment, if ever.

        The board's own `active_work_notified_at`: the newest successful send of
        a transition for this ticket, to this role, in the state it is in now.
        Read here rather than assumed, because it is the difference between a
        handoff nobody has seen -- which must go out -- and a second copy of one
        they already have (SYRD-212).
        """
        result = conn.execute(
            """
SELECT max(trace.ts)::text AS last_sent_at
FROM ticket_board.notification_trace trace
JOIN ticket_board.tickets t ON t.id = trace.ticket_id
WHERE trace.ticket_id = %s
  AND trace.target_role = %s
  AND trace.kind = 'transition'
  AND trace.event = 'send'
  AND trace.ticket_state_at_event = t.state
""",
            (ticket_id, target_role),
        )
        row = result.fetchone()
        if row is None:
            return ""
        value = row["last_sent_at"] if isinstance(row, dict) else row[0]
        return self._decode_text(value).strip()

    def _next_notification_attempt_at(self, conn: Any) -> datetime | None:
        try:
            result = conn.execute("SELECT ticket_board.next_notification_attempt()")
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to read next ticket notification attempt: %s", exc)
            return None
        if row is None:
            return None
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        return None

    def _wait_timeout_seconds(self, conn: Any) -> float:
        timeout = max(self.poll_seconds, 0.0)
        next_attempt_at = self._next_notification_attempt_at(conn)
        if next_attempt_at is None:
            return timeout
        if next_attempt_at.tzinfo is None:
            next_attempt_at = next_attempt_at.replace(tzinfo=timezone.utc)
        seconds_until_due = (next_attempt_at - datetime.now(timezone.utc)).total_seconds()
        due_timeout = max(0.0, seconds_until_due)
        return min(timeout, due_timeout) if timeout > 0 else due_timeout

    def process_idle_turn_end_nudges(self, conn: Any) -> int:
        return self.idle_nudges.process_idle_turn_end_nudges(conn)

    def process_serial_focus_queue_wakeups(self, conn: Any) -> int:
        return self.idle_nudges.process_serial_focus_queue_wakeups(conn)

    def process_idle_stall_nudges(self, conn: Any) -> int:
        return self.idle_nudges.process_idle_stall_nudges(conn)


    def refresh_workflow(self, conn: Any) -> None:
        from .workflow_config import ephemeral_roles, read_configuration, validate
        document = read_configuration(conn)
        self.workflow = validate(document, project=self.project) if document else None
        # A tenant with no declared workflow declares no ephemeral role, which
        # is the same answer as declaring them all false.
        self.ephemeral_roles = ephemeral_roles(self.workflow) if self.workflow else set()
        self.role_runtimes = {
            role["name"]: role["runtime"]
            for role in (self.workflow["roles"] if self.workflow else [])
            if role.get("runtime")
        }
        self.role_targets = dict(ROLE_TO_TARGET)
        self.active_runtime_roles = set()
        # Runtime rows are the routing authority for both declarative and
        # static compatibility workflows. A replacement pane may deliberately
        # use a non-conventional target; reconstructing the name would send
        # work to a missing listener.
        result = conn.execute(
            """
SELECT a.role, a.actual_target, a.runtime, a.process_pid, a.process_start_time
FROM ticket_board.role_runtime_assignments a
JOIN ticket_board.workflow_roles r ON r.name=a.role
WHERE (r.definition->>'active')::boolean
  AND r.definition->>'runtime'=a.runtime
  AND r.definition->>'target'=a.actual_target
"""
        )
        rows = result.fetchall() if result is not None and hasattr(result, "fetchall") else []
        if rows:
            assignments: dict[str, tuple[str, str]] = {}
            for row in rows:
                if isinstance(row, dict):
                    self.active_runtime_roles.add(self._decode_text(row["role"]))
                    if row.get("process_pid") and not session_is_live(
                        SessionIdentity(int(row["process_pid"]), int(row["process_start_time"]))
                    ):
                        continue
                    assignments[self._decode_text(row["role"])] = (
                        self._decode_text(row["actual_target"]),
                        self._decode_text(row["runtime"]),
                    )
                else:
                    self.active_runtime_roles.add(self._decode_text(row[0]))
                    if len(row) >= 5:
                        if not session_is_live(SessionIdentity(int(row[3]), int(row[4]))):
                            continue
                    assignments[self._decode_text(row[0])] = (
                        self._decode_text(row[1]), self._decode_text(row[2])
                    )
            self.role_targets = {role: target for role, (target, _runtime) in assignments.items()}
            gate = getattr(self.activity_gate, "__self__", None)
            if isinstance(gate, PaneActivityGate):
                gate.role_targets = self.role_targets.copy()
                gate.role_runtimes = {
                    role: HOOK_RUNTIME_NAMES.get(runtime, runtime)
                    for role, (_target, runtime) in assignments.items()
                }
                gate.director_target = self.role_targets.get("director", gate.director_target)

    def process_due_notifications(self, conn: Any, *, max_notifications: int | None = None) -> int:
        self.refresh_workflow(conn)
        delivered = 0
        while not self.stop_event.is_set():
            if max_notifications is not None and self.delivered_count >= max_notifications:
                break
            row = self._claim_notification(conn)
            if row is None:
                break
            notification_id, ticket_id, target_role, message, payload, attempts = row
            kind = self._payload_kind(payload)
            self.ledger.trace(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="listener_claim",
                detail={"attempts": attempts, "payload": self.ledger.safe_json_payload(payload)},
            )
            target = self.role_targets.get(target_role)
            if self.stop_event.is_set():
                break
            if target is None:
                active_role = target_role in self.active_runtime_roles or (
                    bool(self.workflow) and any(
                        role["name"] == target_role and role["active"]
                        for role in self.workflow["roles"]
                    )
                )
                if active_role:
                    self.logger.info(
                        "Requeueing notification %s: active role %s has no live runtime assignment",
                        notification_id, target_role,
                    )
                    self.ledger.requeue(
                        conn, notification_id, attempts, "role_runtime_unassigned"
                    )
                    continue
                self.logger.warning("Acking notification %s with unknown target role %s", notification_id, target_role)
                self.ledger.trace(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="drop",
                    busy_reason="unknown_target_role",
                    detail={"payload": self.ledger.safe_json_payload(payload)},
                )
                self.ledger.trace(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="listener_ack",
                    detail={"reason": "unknown_target_role"},
                )
                self.ledger.ack(conn, notification_id)
                self.ledger.forget(notification_id)
                continue
            superseded_queue = self.eligibility._superseded_queue_notice(conn, ticket_id, payload)
            if superseded_queue is not None:
                self.eligibility._drop_superseded_notification(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, detail=superseded_queue,
                    phase="claim", reason=SUPERSEDED_QUEUE_NOTICE,
                )
                continue
            if not self.eligibility._notification_is_current(conn, ticket_id, target_role, payload):
                self.logger.info("Dropping stale notification %s for %s: %s", notification_id, ticket_id, payload)
                self.ledger.trace(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="drop",
                    busy_reason="stale_notification",
                    detail={"payload": self.ledger.safe_json_payload(payload)},
                )
                self.ledger.trace(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="listener_ack",
                    detail={"reason": "stale_notification"},
                )
                self.ledger.ack(conn, notification_id)
                self.ledger.forget(notification_id)
                continue
            superseded = self.eligibility._superseding_awaiting_role(conn, notification_id, ticket_id, kind)
            if superseded is not None:
                self.eligibility._drop_superseded_notification(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, detail=superseded, phase="claim",
                )
                continue
            # Pane hook state can outlive its tmux pane. Probe the target once per
            # claimed notification before the activity gate so stale state files
            # cannot hold delivery forever; this keeps the subprocess cost off the
            # gate's repeated sampling path.
            target_exists = self.target_exists(target)
            if target_exists is False:
                self.logger.error(
                    "Dead-lettering ticket notification %s for %s: target %s does not exist",
                    notification_id,
                    ticket_id,
                    target,
                )
                self.ledger.dead_letter(
                    conn,
                    notification_id,
                    "tmux_target_missing",
                    target=target,
                    message=message,
                    attempts=attempts,
                    payload=payload,
                )
                continue
            finish_current_ticket = self.eligibility._finish_current_blocker(conn, ticket_id, target_role, payload)
            if finish_current_ticket:
                self.logger.info(
                    "Holding notification %s for %s until %s leaves %s's hands",
                    notification_id,
                    ticket_id,
                    finish_current_ticket,
                    target_role,
                )
                self.ledger.trace_deferral_once(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="finish_current_defer",
                    pane_busy=True,
                    busy_reason="finish_current",
                    detail=lambda: {"current_ticket_id": finish_current_ticket, "attempts": attempts},
                )
                self.ledger.requeue(
                    conn,
                    notification_id,
                    attempts,
                    FINISH_CURRENT_REQUEUE_ERROR,
                )
                continue
            pane_busy, activity_trace = self._activity_state_for_notification(kind, target)
            composer_before = self._composer_snapshot(target)
            # A hold that comes from a turn which has already ended is the one
            # that can starve a handoff assigned after that turn-end. SYRD-101
            # was right that it must not be broken by a clock -- an hour-long
            # sweep has to keep its pane quiet -- but "not by a clock" cannot
            # mean "for ever": notification 2842 sat undelivered for 38 minutes
            # behind a SYRD-210 poll loop while App's own hook had said idle,
            # and only a human stopping that process group released it. The
            # probe still decides what is work; the bound lives here, where the
            # age of the HANDOFF is known (SYRD-212).
            release = self._release_prior_turn_hold(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                activity_trace=activity_trace,
            )
            if release == PRIOR_TURN_HOLD_DISCARD:
                continue
            if self._should_defer_for_activity(activity_trace) and release != PRIOR_TURN_HOLD_DELIVER:
                if self._reminder_is_stale_for_activity(kind, activity_trace):
                    self._drop_stale_reminder(
                        conn,
                        notification_id=notification_id,
                        ticket_id=ticket_id,
                        target_role=target_role,
                        kind=kind,
                        target=target,
                        message=message,
                        attempts=attempts,
                        activity_trace=activity_trace,
                        composer_before=composer_before,
                        phase="activity_gate",
                    )
                    continue
                self.logger.info("Pane %s is active; requeueing notification %s for %s", target, notification_id, ticket_id)
                self.ledger.trace_deferral_once(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="gate_defer",
                    pane_busy=True,
                    busy_reason=activity_trace.reason,
                    region_digest=activity_trace.region_digest,
                    detail=lambda: self._delivery_diagnostic_detail(
                        target=target,
                        message=message,
                        attempts=attempts,
                        activity_trace=activity_trace,
                        before=composer_before,
                        decision="defer",
                        reason=activity_trace.reason,
                    ),
                )
                self.ledger.requeue(
                    conn,
                    notification_id,
                    attempts,
                    PANE_BUSY_REQUEUE_ERROR,
                )
                continue
            if self.pre_send_recheck_delay_seconds > 0:
                self.sleeper(self.pre_send_recheck_delay_seconds)
                if self.stop_event.is_set():
                    break
            pane_busy, activity_trace = self._activity_state_for_notification(kind, target, pre_send_recheck=True)
            composer_before = self._composer_snapshot(target)
            # The same release the first gate honoured. Without it the recheck a
            # moment later re-imposes the hold this handoff has already waited
            # out, and the bound buys nothing (SYRD-212).
            if self._should_defer_for_activity(activity_trace) and not (
                release == PRIOR_TURN_HOLD_DELIVER
                and activity_trace.reason in PRIOR_TURN_HOLD_REASONS
            ):
                if self._reminder_is_stale_for_activity(kind, activity_trace):
                    self._drop_stale_reminder(
                        conn,
                        notification_id=notification_id,
                        ticket_id=ticket_id,
                        target_role=target_role,
                        kind=kind,
                        target=target,
                        message=message,
                        attempts=attempts,
                        activity_trace=activity_trace,
                        composer_before=composer_before,
                        phase="pre_send_recheck",
                    )
                    continue
                self.logger.info(
                    "Pane %s became active before send; requeueing notification %s for %s",
                    target,
                    notification_id,
                    ticket_id,
                )
                self.ledger.trace_deferral_once(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="gate_defer",
                    pane_busy=True,
                    busy_reason=activity_trace.reason,
                    region_digest=activity_trace.region_digest,
                    detail=lambda: {
                        **self._delivery_diagnostic_detail(
                            target=target,
                            message=message,
                            attempts=attempts,
                            activity_trace=activity_trace,
                            before=composer_before,
                            decision="defer",
                            reason=activity_trace.reason,
                        ),
                        "phase": "pre_send_recheck",
                    },
                )
                self.ledger.requeue(
                    conn,
                    notification_id,
                    attempts,
                    PANE_BUSY_REQUEUE_ERROR,
                )
                continue
            # Activity probing can take time, and a deferred reminder can have
            # been waiting far longer than that. Recheck immediately before
            # sending so a handoff established in either window suppresses this
            # delivery rather than being overtaken by it (SYRD-99).
            superseded = self.eligibility._superseding_awaiting_role(conn, notification_id, ticket_id, kind)
            if superseded is not None:
                self.eligibility._drop_superseded_notification(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, detail=superseded,
                    phase="pre_send_recheck",
                )
                continue
            # The same reread, immediately before the send. A queue announcement
            # waits out the Director's own activity here, and that is exactly
            # the window a reroute lands in: claiming it while the identity was
            # still current says nothing about whether it is current now.
            superseded_queue = self.eligibility._superseded_queue_notice(conn, ticket_id, payload)
            if superseded_queue is not None:
                self.eligibility._drop_superseded_notification(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, detail=superseded_queue,
                    phase="pre_send_recheck", reason=SUPERSEDED_QUEUE_NOTICE,
                )
                continue
            # Recheck handoffs too, so a resolution during that probe suppresses
            # this delivery.
            if kind == "awaiting_role" and not self.eligibility._notification_is_current(conn, ticket_id, target_role, payload):
                self.ledger.trace(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, event="drop",
                    busy_reason="stale_notification", detail={"phase": "pre_send_recheck"},
                )
                self.ledger.ack(conn, notification_id)
                continue
            # Last, deliberately: every gate above decides whether this role is
            # free to be handed this ticket at all, and a clear is only allowed
            # once that answer is yes. Anywhere earlier and a role that turns
            # out to be busy -- or a notification that turns out to be stale --
            # would have had its conversation deleted for nothing (SYRD-135).
            if self.session_clear._session_clear_is_due(conn, ticket_id, target_role, kind, payload):
                if not self.session_clear._clear_role_session(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    target=target,
                    message=message,
                    attempts=attempts,
                ):
                    continue
            directorctl_diagnostic: dict[str, Any] = {}
            send_started_at = self.wall_clock()
            try:
                sender_result = self.sender(target, display_message(message))
                if isinstance(sender_result, dict):
                    directorctl_diagnostic = sender_result
            except (subprocess.SubprocessError, OSError) as exc:
                if isinstance(exc, subprocess.CalledProcessError):
                    stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
                    directorctl_diagnostic = parse_directorctl_diagnostic(stdout if isinstance(stdout, str) else None)
                failure_reason = delivery_failure_reason(exc, target)
                error_output = delivery_error_output(exc)
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
                continue
            composer_after = self._composer_snapshot(target)
            # directorctl returning 0 is not delivery: its check is what the
            # pane looks like, and a pane already busy on another turn looks
            # submitted whatever happened -- so MEFP-1's Final Sign-Off notice
            # was recorded delivered and never seen (SYRD-268). Delivered now
            # needs the recipient's own hooks to have recorded a turn.
            # "Cannot tell" is not receipt either: without the recipient's
            # own record the notice is unconfirmed, whatever the reason.
            submission, unconfirmed_reason = self._await_submission(target, send_started_at)
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
                    "waited_seconds": self.submission_confirm_seconds,
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
                self.ledger.ack(conn, notification_id)
                self.ledger.forget(notification_id)
                continue
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
            self.delivered_count += 1
            delivered += 1
            self.logger.info("Delivered queued notification %s for %s to %s: %s", notification_id, ticket_id, target, payload)
        return delivered

    def _await_submission(self, target: str, since: float) -> tuple[bool | None, str]:
        """Wait, bounded, for the recipient's own hooks to record a turn.

        (True, ""): a turn started after the send. Anything else is not
        receipt, and says why: (False, NO_SUBMISSION_WITNESSED) -- none within
        the bound; (None, NO_HOOK_STATE) -- the target has no hook state to
        read; (None, NO_SUBMISSION_WITNESS) -- this gate cannot be asked at all.
        "Cannot tell" stays None in the record, never either answer.
        """
        witnessed = self.submission_witness
        if witnessed is None:
            gate_owner = getattr(self.activity_gate, "__self__", None)
            witnessed = getattr(gate_owner, "submission_witnessed", None)
        if not callable(witnessed):
            return None, NO_SUBMISSION_WITNESS
        deadline = self.monotonic() + self.submission_confirm_seconds
        while True:
            answer = witnessed(target, since)
            if answer is None:
                return None, NO_HOOK_STATE
            if answer:
                return True, ""
            if self.monotonic() >= deadline:
                return False, NO_SUBMISSION_WITNESSED
            self.sleeper(self.submission_poll_seconds)

    def _wait_for_notification(self, conn: Any) -> bool:
        notifications = conn.notifies(timeout=self._wait_timeout_seconds(conn), stop_after=1)
        return next(iter(notifications), None) is not None

    def _log_missing_hook_state(self) -> None:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        missing_hook_targets = getattr(gate_owner, "missing_hook_targets", None)
        if not callable(missing_hook_targets):
            return
        missing = missing_hook_targets()
        if missing:
            self.logger.warning(
                "Pane hook state missing for %s; notification delivery fails closed until hooks write state",
                ", ".join(missing),
            )
        else:
            self.logger.info("Pane hook state present for all notification targets")

    def listen_once(self, *, max_notifications: int | None = None) -> int:
        delivered_before = self.delivered_count
        with self.connector(self.conninfo, **self._connector_kwargs()) as conn:
            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
            self.logger.info("LISTEN %s established", self.channel)
            self._log_missing_hook_state()
            while not self.stop_event.is_set():
                self.refresh_workflow(conn)
                self.process_idle_turn_end_nudges(conn)
                self.process_idle_stall_nudges(conn)
                self.process_serial_focus_queue_wakeups(conn)
                delivered = self.process_due_notifications(conn, max_notifications=max_notifications)
                if max_notifications is not None and self.delivered_count >= max_notifications:
                    break
                if not delivered and max_notifications is not None and self.poll_seconds <= 0:
                    break
                notified = self._wait_for_notification(conn)
                if not notified and max_notifications is not None and self.poll_seconds <= 0:
                    break
        return self.delivered_count - delivered_before

    def run_forever(
        self,
        *,
        max_connections: int | None = None,
        max_notifications: int | None = None,
    ) -> None:
        attempts = 0
        while not self.stop_event.is_set():
            if max_connections is not None and attempts >= max_connections:
                return
            attempts += 1
            try:
                self.listen_once(max_notifications=max_notifications)
            except (psycopg.Error, OSError) as exc:
                self.logger.warning("ticket notify listener disconnected: %s", exc)
            if not self.stop_event.is_set():
                self.stop_event.wait(self.reconnect_seconds)


def database_url_from_environment() -> str:
    return DEFAULT_DATABASE_URL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver ticket-board pg_notify state transitions to tmux panes.")
    parser.add_argument("--database", default=database_url_from_environment(), help="PostgreSQL connection string. Defaults to TICKET_BOARD_NOTIFY_DATABASE_URL, TICKET_BOARD_DATABASE_URL, or DATABASE_URL; otherwise libpq environment.")
    parser.add_argument("--channel", default=CHANNEL, help=f"LISTEN channel (default: {CHANNEL})")
    parser.add_argument("--directorctl", default=DEFAULT_DIRECTORCTL, help=f"directorctl path (default: {DEFAULT_DIRECTORCTL})")
    parser.add_argument("--reconnect-seconds", type=float, default=DEFAULT_RECONNECT_SECONDS)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--pane-state-dir", default=str(DEFAULT_PANE_STATE_DIR), help=f"per-pane hook state directory (default: {DEFAULT_PANE_STATE_DIR})")
    parser.add_argument("--director-composing-timeout-seconds", type=float, default=DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS)
    parser.add_argument("--pre-send-recheck-delay-seconds", type=float, default=DEFAULT_PRE_SEND_RECHECK_DELAY_SECONDS)
    parser.add_argument("--idle-working-timer-sample-delay-seconds", type=float, default=DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--verify-pane-state-authority",
        action="store_true",
        help="check that the configured pane-state directory holds hook state for the board's registered roles, then exit",
    )
    parser.add_argument(
        "--board-url",
        default=os.environ.get("TICKET_BOARD_URL", "").strip(),
        help="board HTTP root to read registered roles from (default: TICKET_BOARD_URL)",
    )
    parser.add_argument(
        "--assignments-json",
        default="",
        help="read runtime assignments from this file instead of the board (for provisioning checks and tests)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.verify_pane_state_authority:
        return verify_pane_state_authority(args)
    try:
        tmux_runner = role_aware_tmux_runner()
    except ValueError as exc:
        raise SystemExit(f"ticket notify listener: {exc}") from exc
    gate = PaneActivityGate(
        state_store=PaneHookStateStore(args.pane_state_dir),
        cursor_position_runner=tmux_runner,
        capture_pane_runner=tmux_runner,
        pane_pid_runner=tmux_runner,
        director_composing_timeout_seconds=args.director_composing_timeout_seconds,
        idle_working_timer_sample_delay_seconds=args.idle_working_timer_sample_delay_seconds,
    )
    listener = TicketBoardNotifyListener(
        conninfo=args.database,
        channel=args.channel,
        sender=DirectorctlSender(args.directorctl),
        activity_gate=gate.is_working,
        target_exists=lambda target: tmux_target_exists(target, runner=tmux_runner),
        reconnect_seconds=args.reconnect_seconds,
        poll_seconds=args.poll_seconds,
        pre_send_recheck_delay_seconds=args.pre_send_recheck_delay_seconds,
    )
    # Rediscovery on every start. A restarted listener re-reads whatever the
    # hooks have written since, and says out loud which directory that is --
    # so a disagreement appears in the log at startup instead of only as
    # notifications that quietly never arrive (SYRD-95).
    startup_report = pane_state_authority(
        [target for target in ROLE_TO_TARGET.values()], gate.state_store
    )
    LOGGER.info("%s", startup_report.describe())
    if not startup_report.ok:
        LOGGER.error(
            "pane hook state is unreadable for every configured role; delivery will defer until "
            "TICKET_BOARD_PANE_STATE_DIR names the directory the pane hooks write to"
        )
    listener.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
