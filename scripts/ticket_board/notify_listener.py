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

from .board_skill import SKILL_NAME as BOARD_SKILL_NAME, skills_for_role
from .peer_identity import SessionIdentity, session_is_live
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
DEFAULT_BUSY_REQUEUE_SECONDS = 1.0
# These strings are persisted in ticket_notification_queue.last_error and must
# match schema.sql reset predicates.
PANE_BUSY_REQUEUE_ERROR = "pane busy"
FINISH_CURRENT_REQUEUE_ERROR = "finish current"
DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_IDLE_STALL_GRACE_SECONDS = 45.0
DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS = 30 * 60.0
DEFAULT_IDLE_STALL_ESCALATE_AFTER = 2
# Present-idle fallback is age-unbounded: an idle hook remains valid until the
# pane reports busy/blocked again, and the live activity gate still runs before
# any send.
DEFAULT_PRESENT_IDLE_FRESHNESS_SECONDS = 0.0
IDLE_TURN_END_SOURCES = frozenset(
    {
        "claude.Stop",
        "codex.Stop",
        "gemini.AfterAgent",
        "gemini.PostInvocation",
        "gemini.Stop",
        "hermes.post_llm_call",
        "hermes.on_session_end",
        "hermes.on_session_finalize",
    }
)
TRUSTED_IDLE_SOURCES = IDLE_TURN_END_SOURCES | frozenset({"claude.Notification.idle_prompt", "listener.stale_codex_busy_recovery"})
DEFAULT_ROLE_RUNTIMES = {
    "director": "claude",
    "main": "codex",
    "app": "codex",
    "perf": "codex",
    "research": "claude",
    "ops": "codex",
    "audit": "claude",
    "inspector": "gemini",
}
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
DEFAULT_DIRECTOR_COMPOSER_HOME_X = 2
DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS = 0.0
DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS = 1.2
DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS = 120.0
#: How far apart the two process-tree samples are taken. Long enough that a
#: running child advances the CPU clock by more than its resolution, short
#: enough to sit inside a delivery decision.
DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS = 0.6
#: CPU the pane's descendants may use across that window while still counting
#: as idle. A resting runtime with a helper subprocess ticks a little; a test,
#: build or mutation sweep does not stay under this.
DEFAULT_CHILD_WORK_CPU_TICKS = 2
MIN_RECOVERABLE_HOOK_EPOCH_SECONDS = 1_700_000_000.0
DEFAULT_PANE_STATE_DIR = (
    Path(os.environ["TICKET_BOARD_PANE_STATE_DIR"]).expanduser()
    if os.environ.get("TICKET_BOARD_PANE_STATE_DIR")
    else Path(os.environ["PGU_TICKET_BOARD_PANE_STATE_DIR"]).expanduser()
    if os.environ.get("PGU_TICKET_BOARD_PANE_STATE_DIR")
    else Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")
)
DEFAULT_PROJECT = os.environ.get("TICKET_BOARD_PROJECT", "").strip() or os.environ.get("PGU_TICKET_BOARD_PROJECT", "").strip() or "pgu"
ROLE_TO_TARGET = {
    role: f"{DEFAULT_PROJECT}-{role}:0.0"
    for role in ("director", "main", "app", "perf", "research", "ops", "audit", "inspector")
}
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
TERMINAL_STATES = {"done", "cancelled"}
NUDGE_ELIGIBLE_STATES = {"in_progress", "inspection", "audit", "dat", "director_review", "analysis", "backlog"}
#: Kinds that say "this role has not moved". A handoff established after one of
#: them was generated says the opposite, so delivering it afterwards reports a
#: stall the board itself no longer believes in. `awaiting_role` is deliberately
#: absent: it IS the handoff's own bounded schedule, and dropping it here would
#: silence the very notifications the wait exists to send (SYRD-99).
SUPERSEDABLE_REMINDER_KINDS = frozenset({"idle_reminder", "nudge", "escalation"})
#: Distinct from `stale_notification`, which means the ticket moved, and from
#: `pane busy`, which means delivery was only postponed. This one means the
#: reminder was answered before it could be delivered.
SUPERSEDED_BY_AWAITING_ROLE = "superseded_by_awaiting_role"
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


@dataclass(frozen=True)
class PaneHookState:
    target: str
    state: str
    updated_at: float
    source: str = ""


@dataclass(frozen=True)
class WorkingProbe:
    captured: bool
    observable: bool
    digest: str = ""


@dataclass(frozen=True)
class ChildWorkSample:
    """What the pane's process tree looked like at one instant."""

    observed: bool
    pids: frozenset[int] = frozenset()
    cpu_ticks: int = 0
    #: Descendants in a session of their own rather than the pane's. A tool that
    #: starts a shell puts it in a new session; a runtime and the helpers it
    #: keeps stay in the pane's. Verified on this host: the pane shell and the
    #: CLI under it share one session id, while a shell the CLI started for a
    #: verification run is its own session leader (SYRD-58).
    detached: frozenset[int] = frozenset()


@dataclass(frozen=True)
class ChildWorkMemory:
    """The last tree seen under a pane, and which of it this turn started.

    A tree that was already there the first time the gate looked is a runtime's
    own furniture -- an MCP server, a language server -- and treating that as
    work would silence a pane's reminders for as long as its runtime lives
    (SYRD-58).
    """

    #: Everything under the pane at the last sample.
    pids: frozenset[int]
    #: The subset this gate watched appear during the current turn, minus any
    #: that have since exited. Work until it leaves.
    arrived: frozenset[int]


def read_process_table(proc_root: Path = Path("/proc")) -> tuple[tuple[int, int, int, int], ...]:
    """(pid, ppid, cpu ticks, session) for every process this account can see.

    Read from /proc rather than by running ps: the gate runs on every delivery
    decision, and a fork per decision is a cost the listener does not need.
    The comm field can contain spaces and parentheses, so the split is on its
    closing parenthesis rather than on whitespace.
    """
    rows: list[tuple[int, int, int, int]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            # A process that exited between the listing and the read.
            continue
        close = stat.rfind(")")
        if close < 0:
            continue
        fields = stat[close + 2 :].split()
        if len(fields) < 13:
            continue
        try:
            rows.append(
                (
                    int(entry.name),
                    int(fields[1]),
                    int(fields[11]) + int(fields[12]),
                    int(fields[3]),
                )
            )
        except ValueError:
            continue
    return tuple(rows)


def descendant_work_sample(
    pane_pid: int, table: Sequence[tuple[int, int, int, int]]
) -> ChildWorkSample:
    """Every process under a pane, and the CPU they have used between them.

    Descendants rather than a named set of programs: what a turn runs is the
    tenant's business, and a rule that named commands would be a list to keep
    in step with every runtime and every tool (SYRD-58).
    """
    children: dict[int, list[int]] = {}
    cpu_by_pid: dict[int, int] = {}
    session_by_pid: dict[int, int] = {}
    for pid, ppid, cpu_ticks, session in table:
        children.setdefault(ppid, []).append(pid)
        cpu_by_pid[pid] = cpu_ticks
        session_by_pid[pid] = session
    seen: set[int] = set()
    frontier = list(children.get(pane_pid, ()))
    while frontier:
        pid = frontier.pop()
        if pid in seen or pid == pane_pid:
            continue
        seen.add(pid)
        frontier.extend(children.get(pid, ()))
    pane_session = session_by_pid.get(pane_pid)
    detached = (
        frozenset(pid for pid in seen if session_by_pid.get(pid, pane_session) != pane_session)
        if pane_session is not None
        else frozenset()
    )
    return ChildWorkSample(
        True,
        frozenset(seen),
        sum(cpu_by_pid.get(pid, 0) for pid in seen),
        detached,
    )


def pane_content_digest(pane_text: str) -> str:
    return hashlib.sha256(pane_text.encode("utf-8")).hexdigest()



BOARD_SKILL_INSTRUCTION = f"Load the {BOARD_SKILL_NAME} skill, then read the whole ticket before acting."


def board_skill_instruction_for_role(role: str = "") -> str:
    """Name the skills this role should load, in one line.

    A Director is pointed at the overlay as well. That is a pointer, not a
    permission: the board authorizes by caller role whatever a session has read.
    """
    names = [skill.name for skill in skills_for_role(role)]
    joined = " and ".join(names) if len(names) > 1 else names[0]
    plural = "skills" if len(names) > 1 else "skill"
    return f"Load the {joined} {plural}, then read the whole ticket before acting."


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


def with_board_skill_instruction(message: str, *, kind: str = "transition", role: str = "") -> str:
    """Point a handed-off pane at its skills without pasting their bodies.

    Only real hand-offs get the line. Nudges, idle reminders and escalations go
    to a pane that is already working the ticket, and repeating the pointer
    there is noise.
    """
    if kind != "transition" or not message.strip():
        return message
    instruction = board_skill_instruction_for_role(role)
    if instruction in message:
        return message
    return f"{message} {instruction}"


def composer_snapshot_from_pane_text(pane_text: str) -> ComposerSnapshot:
    if not pane_text:
        return ComposerSnapshot(False, error="empty_capture")
    lines = pane_text.splitlines()
    horizontal_indices = [idx for idx, line in enumerate(lines) if line.count("─") >= 40]
    if len(horizontal_indices) < 2:
        return ComposerSnapshot(True, marker_found=False)
    composer_content = "\n".join(lines[horizontal_indices[-2] + 1 : horizontal_indices[-1]])
    cleaned = re.sub(r"^[❯\u276f\s\>\-\*]+", "", composer_content, flags=re.MULTILINE)
    stripped = cleaned.strip()
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest() if stripped else ""
    return ComposerSnapshot(
        True,
        marker_found=True,
        active=bool(stripped),
        content_sha256=digest,
        content_length=len(stripped),
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


def delivery_failure_reason(exc: BaseException, target: str) -> str:
    output_parts: list[str] = [str(exc)]
    if isinstance(exc, subprocess.CalledProcessError):
        for value in (exc.stderr, exc.stdout):
            if isinstance(value, bytes):
                output_parts.append(value.decode("utf-8", errors="replace"))
            elif isinstance(value, str):
                output_parts.append(value)
    output = "\n".join(part for part in output_parts if part).lower()
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


@dataclass(frozen=True)
class PaneStateAuthority:
    """Whether the directory the listener reads is the one the panes write to.

    The listener decides delivery from hook state, and a missing file reads as
    "cannot tell, assume busy" -- which is the right default for one pane and
    the wrong one for all of them at once. Pointed at a directory no hook
    writes to, it defers every notification forever while the process, the
    socket and the runtime assignments all look healthy. That is what a
    process-active check cannot see, so this is the thing to check instead
    (SYRD-95).
    """

    state_dir: Path
    registered: tuple[str, ...]
    with_state: tuple[str, ...]
    without_state: tuple[str, ...]

    @property
    def ok(self) -> bool:
        # Nothing registered is not a disagreement: a board with no panes yet
        # has nobody to serve and nothing to be wrong about. Registered roles
        # with hook state for none of them is the failure, because it can only
        # mean the two halves are looking at different directories.
        return not self.registered or bool(self.with_state)

    def describe(self) -> str:
        if not self.registered:
            return f"pane-state authority: no registered roles; nothing to serve from {self.state_dir}"
        if not self.with_state:
            return (
                f"pane-state authority: {self.state_dir} holds hook state for none of the "
                f"{len(self.registered)} registered roles ({', '.join(self.registered)}). "
                "The listener and the pane hooks are reading and writing different "
                "directories, so every pane reads as busy and nothing is delivered."
            )
        line = (
            f"pane-state authority: {self.state_dir} holds hook state for "
            f"{len(self.with_state)} of {len(self.registered)} registered roles"
        )
        if self.without_state:
            # Not a failure. A pane that has not run a hook yet has no file,
            # and one role being quiet is not the two halves disagreeing.
            line += f"; no state yet for {', '.join(self.without_state)}"
        return line


def pane_state_authority(
    targets: Iterable[str], store: "PaneHookStateStore"
) -> PaneStateAuthority:
    """Compare the configured directory against the roles actually registered."""

    registered = tuple(dict.fromkeys(target for target in targets if target))
    with_state = tuple(target for target in registered if store.read(target) is not None)
    without_state = tuple(target for target in registered if target not in set(with_state))
    return PaneStateAuthority(
        state_dir=store.state_dir,
        registered=registered,
        with_state=with_state,
        without_state=without_state,
    )


def registered_pane_targets(payload: Any) -> tuple[str, ...]:
    """The targets a board's runtime assignments say are live.

    Read from the board rather than assumed from a role list, because the
    question is what the listener would actually try to deliver to.
    """

    assignments = (payload or {}).get("assignments") if isinstance(payload, dict) else None
    if not isinstance(assignments, dict):
        return ()
    targets: list[str] = []
    for assignment in assignments.values():
        if not isinstance(assignment, dict):
            continue
        target = str(assignment.get("actual_target") or "").strip()
        if target:
            targets.append(target)
    return tuple(dict.fromkeys(targets))


class PaneHookStateStore:
    def __init__(self, state_dir: str | Path = DEFAULT_PANE_STATE_DIR) -> None:
        self.state_dir = Path(state_dir).expanduser()

    def _target_path(self, target: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
        return self.state_dir / f"{safe}.json"

    def read(self, target: str) -> PaneHookState | None:
        path = self._target_path(target)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        state = str(parsed.get("state") or "").strip().lower()
        if state not in {"idle", "busy", "blocked"}:
            return None
        try:
            updated_at = float(parsed.get("updated_at"))
        except (TypeError, ValueError):
            return None
        return PaneHookState(
            target=str(parsed.get("target") or target),
            state=state,
            updated_at=updated_at,
            source=str(parsed.get("source") or ""),
        )

    def write(self, target: str, state: str, *, source: str = "", now: float | None = None) -> Path:
        normalized_state = state.strip().lower()
        if normalized_state not in {"idle", "busy", "blocked"}:
            raise ValueError("pane hook state must be idle, busy, or blocked")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "target": target,
            "state": normalized_state,
            "updated_at": time.time() if now is None else now,
            "source": source,
        }
        path = self._target_path(target)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


class PaneActivityGate:
    def __init__(
        self,
        *,
        state_store: PaneHookStateStore | None = None,
        director_target: str = ROLE_TO_TARGET["director"],
        client_activity_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        cursor_position_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        capture_pane_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        pane_pid_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        process_table_reader: Callable[[], Sequence[tuple[int, int, int]]] = read_process_table,
        child_work_sample_delay_seconds: float = DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS,
        child_work_cpu_ticks: int = DEFAULT_CHILD_WORK_CPU_TICKS,
        director_composing_timeout_seconds: float = DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS,
        director_startup_hold_seconds: float = DEFAULT_BUSY_REQUEUE_SECONDS,
        director_composer_home_x: int = DEFAULT_DIRECTOR_COMPOSER_HOME_X,
        working_timer_sample_delay_seconds: float = DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS,
        idle_working_timer_sample_delay_seconds: float = DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS,
        stale_codex_busy_hook_seconds: float = DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS,
        role_runtimes: dict[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.role_targets = dict(ROLE_TO_TARGET)
        self.active_runtime_roles: set[str] = set()
        self.state_store = state_store or PaneHookStateStore()
        self.director_target = director_target
        self.cursor_position_runner = cursor_position_runner
        self.capture_pane_runner = capture_pane_runner
        # The same kind of `display-message -p` call as the cursor probe, so a
        # caller that supplied one runner has supplied both. A runner that does
        # not understand this format returns something unparseable, the pane pid
        # is unknown, and the probe simply declines to answer (SYRD-58).
        self.pane_pid_runner = pane_pid_runner or cursor_position_runner
        self.process_table_reader = process_table_reader
        self.child_work_sample_delay_seconds = max(0.0, child_work_sample_delay_seconds)
        self.child_work_cpu_ticks = max(0, child_work_cpu_ticks)
        self._child_work_memory_by_target: dict[str, ChildWorkMemory] = {}
        self.director_startup_hold_seconds = director_startup_hold_seconds
        self.director_composer_home_x = director_composer_home_x
        self.working_timer_sample_delay_seconds = max(0.0, working_timer_sample_delay_seconds)
        self.idle_working_timer_sample_delay_seconds = max(0.0, idle_working_timer_sample_delay_seconds)
        self.stale_codex_busy_hook_seconds = max(0.0, stale_codex_busy_hook_seconds)
        self.role_runtimes = {
            str(role).strip().lower(): str(runtime).strip().lower()
            for role, runtime in (role_runtimes or DEFAULT_ROLE_RUNTIMES).items()
            if str(role).strip() and str(runtime).strip()
        }
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.sleeper = sleeper
        self._last_trace_by_target: dict[str, ActivityTrace] = {}
        self._last_hook_state_by_target: dict[str, tuple[str, float]] = {}
        self._last_working_timer_by_target: dict[str, int] = {}
        self._started_at = self.wall_time()
        self._director_startup_hold_state_ts: float | None = None
        self._director_startup_hold_started_at: float | None = None
        self._director_startup_released_state_ts: float | None = None
        #: Work last actually observed in each pane, as (the hook state it was
        #: observed against, when it was observed). A turn-end hook dates the
        #: runtime's own turn, not the shells that turn started, so evidence
        #: gathered against that same hook state is what the idle clock is
        #: reset to. Evidence against an older state is superseded by the new
        #: one and does not count (SYRD-58).
        self._work_evidence_by_target: dict[str, tuple[float, float]] = {}
        self._observed_state_ts_by_target: dict[str, float] = {}

    def _record_trace(self, target: str, trace: ActivityTrace) -> bool:
        self._last_trace_by_target[target] = trace
        if trace.busy and trace.reason in WORK_EVIDENCE_REASONS:
            state_ts = self._observed_state_ts_by_target.get(target)
            if state_ts is not None:
                self._work_evidence_by_target[target] = (state_ts, self.wall_time())
        return trace.busy

    def last_trace(self, target: str) -> ActivityTrace | None:
        return self._last_trace_by_target.get(target)

    def missing_hook_targets(self, targets: list[str] | None = None) -> list[str]:
        checked_targets = targets or sorted(set(self.role_targets.values()))
        return [target for target in checked_targets if self.state_store.read(target) is None]

    def _idle_hook_states_by_role(self, roles: list[str] | None = None) -> dict[str, PaneHookState]:
        checked_roles = roles or sorted(self.role_targets)
        idle_states: dict[str, PaneHookState] = {}
        for role in checked_roles:
            target = self.role_targets.get(role)
            if target is None:
                continue
            state = self.state_store.read(target)
            if state is None or state.state != "idle":
                continue
            idle_states[role] = state
        return idle_states

    def eligibility_busy(self, target: str) -> bool:
        """The gate a reminder is minted against, at the strength it is sent against.

        Generation used the weaker gate and delivery the stronger one, so a
        reminder could be minted for a role the pre-send gate then held -- and
        the stall counter behind it advanced against a role that had never
        stopped working (SYRD-58).
        """
        return self.pre_send_busy(target)

    def _confirmed_idle_since(self, target: str, state: PaneHookState) -> float:
        """When this pane's whole turn actually went quiet.

        A hook timestamp dates the runtime's own turn, not the shells that turn
        started. If work was observed after the hook wrote idle, the turn was
        still running then, so the clock a reminder is measured against starts
        at that observation instead. A pane in which nothing was ever observed
        keeps its hook timestamp, so a genuinely idle pane is not made to wait
        by a listener restart (SYRD-58).
        """
        evidence = self._work_evidence_by_target.get(target)
        if evidence is None or evidence[0] < state.updated_at:
            return state.updated_at
        return max(state.updated_at, evidence[1])

    def _forget_confirmed_idle(self, target: str) -> None:
        """A pane that is working has no idle clock to keep."""
        return None

    def idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        checked_roles = roles or sorted(self.role_targets)
        idle_since: dict[str, str] = {}
        for role in checked_roles:
            target = self.role_targets.get(role)
            if target is None:
                continue
            if self.eligibility_busy(target):
                self._forget_confirmed_idle(target)
                continue
            state = self.state_store.read(target)
            if state is None or state.state != "idle":
                self._forget_confirmed_idle(target)
                continue
            idle_since[role] = datetime.fromtimestamp(
                self._confirmed_idle_since(target, state), timezone.utc
            ).isoformat()
        return idle_since

    def turn_end_idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        turn_end_idle_since: dict[str, str] = {}
        for role, state in self._idle_hook_states_by_role(roles).items():
            if state.source not in IDLE_TURN_END_SOURCES:
                continue
            target = self.role_targets.get(role)
            # A turn-end hook only reports that the previous turn finished, and
            # agy raises PostInvocation between the turns of one review. Consult
            # the live gate before minting a reminder, exactly as
            # idle_since_by_role already does for the stall generator, so
            # generation and delivery agree about who is working (SYRD-32).
            if target is not None and self.eligibility_busy(target):
                self._forget_confirmed_idle(target)
                continue
            if target is None:
                continue
            turn_end_idle_since[role] = datetime.fromtimestamp(
                self._confirmed_idle_since(target, state), timezone.utc
            ).isoformat()
        return turn_end_idle_since

    def _tmux_session_for_target(self, target: str) -> str:
        return target.split(":", 1)[0]

    def _role_for_target(self, target: str) -> str:
        for role, configured_target in self.role_targets.items():
            if configured_target == target:
                return role
        session = self._tmux_session_for_target(target)
        prefix = f"{DEFAULT_PROJECT}-"
        if session.startswith(prefix):
            return session[len(prefix) :].strip().lower()
        if "-" not in session:
            return ""
        return session.split("-", 1)[1].strip().lower()

    def _runtime_for_source(self, source: str) -> str:
        if source.startswith("team_launcher."):
            return ""
        runtime = source.split(".", 1)[0].strip().lower()
        if runtime == "agy":
            return "gemini"
        return runtime

    def _expected_runtime_for_target(self, target: str) -> str:
        return self.role_runtimes.get(self._role_for_target(target), "")

    def _captured_working_timer_probe(self, target: str) -> WorkingProbe:
        try:
            proc = self.capture_pane_runner(
                ["tmux", "capture-pane", "-p", "-J", "-t", target],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return WorkingProbe(False, False)
        return WorkingProbe(True, True, pane_content_digest(proc.stdout))

    def _working_timer_probe_trace(self, target: str, *, sample_delay_seconds: float) -> ActivityTrace:
        first = self._captured_working_timer_probe(target)
        if not first.captured or not first.observable:
            return ActivityTrace(True, "working_timer_unobservable")
        # Interior offsets avoid the common 0.2s/0.4s spinner aliases that hit 0.4/0.8 sampling.
        first_inner_gap = sample_delay_seconds * (23.0 / 120.0)
        second_inner_gap = sample_delay_seconds * (1.0 / 15.0)
        final_gap = sample_delay_seconds - first_inner_gap - second_inner_gap
        probes = [first]
        for gap in (first_inner_gap, second_inner_gap, final_gap):
            if gap > 0:
                self.sleeper(gap)
            probe = self._captured_working_timer_probe(target)
            if not probe.captured or not probe.observable:
                return ActivityTrace(True, "working_timer_unobservable")
            probes.append(probe)
        for probe in probes[1:]:
            if probe.digest != first.digest:
                return ActivityTrace(True, "pane_content_changed", region_digest=probe.digest)
        return ActivityTrace(False, "working_timer_idle", region_digest=probes[-1].digest)

    def composer_snapshot(self, target: str) -> ComposerSnapshot:
        try:
            proc = self.capture_pane_runner(
                ["tmux", "capture-pane", "-p", "-J", "-t", target],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ComposerSnapshot(False, error=str(exc))
        return composer_snapshot_from_pane_text(proc.stdout)

    def _working_timer_trace(self, target: str, *, sample_delay_seconds: float | None = None) -> ActivityTrace | None:
        sample_delay = self.working_timer_sample_delay_seconds if sample_delay_seconds is None else sample_delay_seconds
        trace = self._working_timer_probe_trace(target, sample_delay_seconds=sample_delay)
        if trace.busy:
            return trace
        return None

    def _working_timer_idle_probe_trace(self, target: str) -> ActivityTrace:
        return self._working_timer_probe_trace(
            target,
            sample_delay_seconds=self.idle_working_timer_sample_delay_seconds,
        )

    def _target_cursor_state(self, target: str) -> bool | None:
        try:
            proc = self.cursor_position_runner(
                ["tmux", "display-message", "-p", "-t", target, "#{cursor_x} #{cursor_y} #{pane_height}"],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        fields = proc.stdout.strip().split()
        if len(fields) != 3:
            return None
        try:
            cursor_x = int(fields[0])
            cursor_y = int(fields[1])
            pane_height = int(fields[2])
        except ValueError:
            return None
        if pane_height <= 0 or cursor_x < 0 or cursor_y < 0:
            return None
        return cursor_x > self.director_composer_home_x

    def _reset_director_startup_hold(self, *, clear_released: bool = True) -> None:
        self._director_startup_hold_state_ts = None
        self._director_startup_hold_started_at = None
        if clear_released:
            self._director_startup_released_state_ts = None

    def _director_startup_hold_trace(self, state: PaneHookState) -> ActivityTrace | None:
        if state.updated_at >= self._started_at:
            self._reset_director_startup_hold()
            return None
        if self._director_startup_released_state_ts == state.updated_at:
            return None
        now = self.monotonic()
        if self._director_startup_hold_state_ts != state.updated_at:
            self._director_startup_hold_state_ts = state.updated_at
            self._director_startup_hold_started_at = now
            return ActivityTrace(True, "startup_latch_unestablished")
        hold_started_at = self._director_startup_hold_started_at if self._director_startup_hold_started_at is not None else now
        if now - hold_started_at < self.director_startup_hold_seconds:
            return ActivityTrace(True, "startup_latch_unestablished")
        self._director_startup_released_state_ts = state.updated_at
        self._reset_director_startup_hold(clear_released=False)
        return None

    def _pane_pid(self, target: str) -> int | None:
        try:
            proc = self.pane_pid_runner(
                ["tmux", "display-message", "-p", "-t", target, "#{pane_pid}"],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        raw = str(getattr(proc, "stdout", "") or "").strip()
        try:
            pane_pid = int(raw)
        except ValueError:
            return None
        return pane_pid if pane_pid > 0 else None

    def _child_work_sample(self, pane_pid: int) -> ChildWorkSample:
        try:
            table = self.process_table_reader()
        except OSError:
            return ChildWorkSample(False)
        if not table:
            return ChildWorkSample(False)
        return descendant_work_sample(pane_pid, table)

    def child_work_trace(self, target: str) -> ActivityTrace | None:
        """Work still running under the pane, whether or not it reaches the screen.

        A turn-end hook says the runtime finished its own turn. It says nothing
        about the shells that turn started, and a long test, build or mutation
        sweep prints nothing for minutes -- so the hook reads idle, the visible
        region does not change, and every existing probe agrees the role is
        free. SYRD-57's trace caught exactly that: a reminder minted eleven
        seconds into a sweep that was still running, and two director
        escalations behind it.

        Both signals are differential, so nothing has to be named: the turn is
        working if its process tree changed shape or if its descendants used
        more than a resting runtime's worth of CPU between the two samples. A
        pane with no descendants at all is idle, which is what keeps a genuinely
        idle pane reachable (SYRD-58).
        """
        pane_pid = self._pane_pid(target)
        if pane_pid is None:
            return None
        first = self._child_work_sample(pane_pid)
        if not first.observed:
            return None
        if self.child_work_sample_delay_seconds > 0:
            self.sleeper(self.child_work_sample_delay_seconds)
        second = self._child_work_sample(pane_pid)
        if not second.observed:
            return None
        remembered = self._child_work_memory_by_target.get(target)
        known = remembered.pids if remembered is not None else first.pids
        # Only arrivals are movement. A tree that shrinks is a turn finishing,
        # and treating that as work would make every turn end busy.
        appeared = second.pids - known
        advanced = second.cpu_ticks - first.cpu_ticks > self.child_work_cpu_ticks
        arrived = (
            (remembered.arrived if remembered is not None else frozenset()) | appeared
        ) & second.pids
        self._child_work_memory_by_target[target] = ChildWorkMemory(second.pids, arrived)
        if second.detached:
            # A descendant in a session of its own. A tool that starts a shell
            # gives it a new session; the runtime and the helpers it keeps stay
            # in the pane's. This needs no history, so it is the signal that
            # survives a listener restart in the middle of a turn's work.
            return ActivityTrace(True, "pane_child_work")
        if appeared or advanced:
            return ActivityTrace(True, "pane_child_work")
        if arrived:
            # A child this turn started, sitting on a fetch, a lock or a long
            # build, using no CPU and printing nothing. It is work until it
            # leaves: a wait has no length at which it stops being a wait.
            return ActivityTrace(True, "pane_child_work")
        return None

    def _trusted_idle_source_trace(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        return self.child_work_trace(target)

    def _idle_cursor_trace(self, target: str, state: PaneHookState, *, check_trusted_working: bool = False) -> ActivityTrace:
        cursor_composing = self._target_cursor_state(target)
        if target == self.director_target:
            startup_trace = self._director_startup_hold_trace(state)
            if startup_trace is not None and cursor_composing is not True:
                return startup_trace
        if cursor_composing is None:
            return ActivityTrace(True, "cursor_state_unavailable")
        if cursor_composing:
            return ActivityTrace(True, "human_composing")
        untrusted_idle_trace = self._untrusted_idle_source_trace(target, state)
        if untrusted_idle_trace is not None and untrusted_idle_trace.busy:
            return untrusted_idle_trace
        # Every route to an idle verdict passes the trusted probe, not just the
        # one that reaches the bottom of this function. A stale trusted hook
        # returns its own not-busy trace here, and returning that directly is
        # how a turn with its verification still running was called idle
        # (SYRD-58).
        if check_trusted_working:
            trusted_idle_trace = self._trusted_idle_source_trace(target, state)
            if trusted_idle_trace is not None:
                return trusted_idle_trace
        if untrusted_idle_trace is not None:
            return untrusted_idle_trace
        return ActivityTrace(False, "hook_idle")

    def _untrusted_idle_source_trace(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        if state.state != "idle":
            return None
        if state.source.startswith("listener."):
            return None
        source_runtime = self._runtime_for_source(state.source)
        expected_runtime = self._expected_runtime_for_target(target)
        if source_runtime and expected_runtime and source_runtime != expected_runtime:
            probe_trace = self._working_timer_idle_probe_trace(target)
            if probe_trace.busy:
                return ActivityTrace(True, f"foreign_runtime_{probe_trace.reason}")
            return ActivityTrace(False, "foreign_runtime_working_timer_idle")
        if state.source in TRUSTED_IDLE_SOURCES:
            if (
                state.updated_at >= MIN_RECOVERABLE_HOOK_EPOCH_SECONDS
                and self.stale_codex_busy_hook_seconds > 0
                and self.wall_time() - state.updated_at >= self.stale_codex_busy_hook_seconds
            ):
                return self._working_timer_idle_probe_trace(target)
            return None
        probe_trace = self._working_timer_idle_probe_trace(target)
        if probe_trace.busy:
            return probe_trace
        return probe_trace

    def _stale_codex_busy_trace(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        if state.state != "busy" or not state.source.startswith("codex."):
            return None
        if self.stale_codex_busy_hook_seconds <= 0:
            return None
        if state.updated_at < MIN_RECOVERABLE_HOOK_EPOCH_SECONDS:
            return None
        now = self.wall_time()
        if now - state.updated_at < self.stale_codex_busy_hook_seconds:
            return None
        working_trace = self._working_timer_trace(
            target,
            sample_delay_seconds=self.idle_working_timer_sample_delay_seconds,
        )
        if working_trace is not None:
            return working_trace
        cursor_composing = self._target_cursor_state(target)
        if cursor_composing is None:
            return ActivityTrace(True, "cursor_state_unavailable")
        if cursor_composing:
            return ActivityTrace(True, "human_composing")
        try:
            self.state_store.write(target, "idle", source="listener.stale_codex_busy_recovery", now=now)
        except OSError as exc:
            LOGGER.warning("Failed to recover stale Codex busy hook state for %s: %s", target, exc)
        return ActivityTrace(False, "stale_codex_busy_recovered")

    def anti_clobber_trace(self, target: str) -> ActivityTrace:
        return self._anti_clobber_trace(target, check_trusted_working=False)

    def pre_send_anti_clobber_trace(self, target: str) -> ActivityTrace:
        return self._anti_clobber_trace(target, check_trusted_working=True)

    def _anti_clobber_trace(self, target: str, *, check_trusted_working: bool) -> ActivityTrace:
        state = self.state_store.read(target)
        if state is None:
            if target == self.director_target:
                self._reset_director_startup_hold()
            return ActivityTrace(True, "no_hook_state")
        previous = self._last_hook_state_by_target.get(target)
        current = (state.state, state.updated_at)
        self._last_hook_state_by_target[target] = current
        self._observed_state_ts_by_target[target] = state.updated_at
        if state.state != "idle" and (previous is None or previous[0] == "idle"):
            # A new turn is starting. The arrivals the gate remembers belong to
            # the turn before it, and this turn's own children will be observed
            # as they appear -- so the older evidence is superseded rather than
            # carried forward for ever (SYRD-58).
            self._child_work_memory_by_target.pop(target, None)
        if state.state != "idle":
            if target == self.director_target:
                self._reset_director_startup_hold()
            cursor_composing = self._target_cursor_state(target)
            if cursor_composing is None:
                return ActivityTrace(True, "cursor_state_unavailable")
            if cursor_composing:
                return ActivityTrace(True, "human_composing")
            return ActivityTrace(False, "hook_idle")
        if previous is not None and previous[0] != "idle" and target == self.director_target:
            self._reset_director_startup_hold()
        return self._idle_cursor_trace(target, state, check_trusted_working=check_trusted_working)

    def anti_clobber_busy(self, target: str) -> bool:
        return self._record_trace(target, self.anti_clobber_trace(target))

    def pre_send_anti_clobber_busy(self, target: str) -> bool:
        return self._record_trace(target, self.pre_send_anti_clobber_trace(target))

    def _full_activity_trace(self, target: str, *, check_trusted_working: bool) -> ActivityTrace:
        state = self.state_store.read(target)
        if state is None:
            if target == self.director_target:
                self._reset_director_startup_hold()
            return ActivityTrace(True, "no_hook_state")
        previous = self._last_hook_state_by_target.get(target)
        current = (state.state, state.updated_at)
        self._last_hook_state_by_target[target] = current
        self._observed_state_ts_by_target[target] = state.updated_at
        if state.state != "idle" and (previous is None or previous[0] == "idle"):
            # A new turn is starting. The arrivals the gate remembers belong to
            # the turn before it, and this turn's own children will be observed
            # as they appear -- so the older evidence is superseded rather than
            # carried forward for ever (SYRD-58).
            self._child_work_memory_by_target.pop(target, None)
        if state.state != "idle":
            if target == self.director_target:
                self._reset_director_startup_hold()
            reason = "hook_blocked" if state.state == "blocked" else "hook_busy"
            stale_trace = self._stale_codex_busy_trace(target, state)
            if stale_trace is not None:
                return stale_trace
            return ActivityTrace(True, reason)
        if previous is not None and previous[0] != "idle" and target == self.director_target:
            self._reset_director_startup_hold()
        return self._idle_cursor_trace(target, state, check_trusted_working=check_trusted_working)

    def is_busy(self, target: str) -> bool:
        return self._record_trace(target, self._full_activity_trace(target, check_trusted_working=False))

    def pre_send_busy(self, target: str) -> bool:
        """Full activity gate for the pre-send recheck.

        The anti-clobber gate deliberately reports a working agent as free so an
        immediate ticket_update still reaches a busy pane; it only guards a
        human's half-typed line. Reusing it for the pre-send recheck of every
        kind (SYRD-32) let a reminder that passed the first gate during a brief
        turn gap land in a pane that had gone busy again, because a busy hook
        was reported as "hook_idle". This keeps the full gate's strength, and
        carries check_trusted_working so a subclass that overrides
        _trusted_idle_source_trace can re-probe a trusted idle hook at send time;
        the base implementation returns None, so today that flag adds nothing on
        its own.
        """
        return self._record_trace(target, self._full_activity_trace(target, check_trusted_working=True))

    def is_working(self, target: str) -> bool:
        return self.is_busy(target)


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
        idle_stall_grace_seconds: float = DEFAULT_IDLE_STALL_GRACE_SECONDS,
        idle_stall_nudge_cadence_seconds: float = DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS,
        idle_stall_escalate_after: int = DEFAULT_IDLE_STALL_ESCALATE_AFTER,
        present_idle_freshness_seconds: float = DEFAULT_PRESENT_IDLE_FRESHNESS_SECONDS,
        pre_send_recheck_delay_seconds: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
        stop_event: threading.Event | None = None,
        logger: logging.Logger = LOGGER,
        target_exists: Callable[[str], bool | None] | None = None,
    ) -> None:
        self.role_targets = dict(ROLE_TO_TARGET)
        self.workflow = None
        self.project = project
        self.conninfo = conninfo
        self.channel = channel
        self.sender = sender or DirectorctlSender()
        self.activity_gate = activity_gate or PaneActivityGate().is_working
        self.connector = connector
        self.reconnect_seconds = reconnect_seconds
        self.poll_seconds = poll_seconds
        self.requeue_base_seconds = requeue_base_seconds
        self.requeue_max_seconds = requeue_max_seconds
        self.busy_requeue_seconds = busy_requeue_seconds
        self.idle_stall_grace_seconds = idle_stall_grace_seconds
        self.idle_stall_nudge_cadence_seconds = idle_stall_nudge_cadence_seconds
        self.idle_stall_escalate_after = idle_stall_escalate_after
        # Deprecated compatibility knob: PGU-562 removed the present-idle age
        # cutoff because the live activity gate is the delivery safety check.
        self.present_idle_freshness_seconds = max(0.0, present_idle_freshness_seconds)
        self.pre_send_recheck_delay_seconds = max(0.0, pre_send_recheck_delay_seconds)
        self.sleeper = sleeper
        self.stop_event = stop_event or threading.Event()
        self.logger = logger
        self.target_exists = target_exists or tmux_target_exists
        self.delivered_count = 0
        self._traced_gate_defer_notifications: set[int] = set()
        self._seen_turn_end_idle_since_by_role: dict[str, str] = {}
        self._consumed_present_idle_since_by_role: dict[str, str] = {}
        self._work_observed_at_by_role: dict[str, str] = {}

    def _connector_kwargs(self) -> dict[str, int | bool]:
        return {
            "autocommit": True,
            "connect_timeout": DEFAULT_CONNECT_TIMEOUT_SECONDS,
            "keepalives": 1,
            "keepalives_idle": DEFAULT_KEEPALIVES_IDLE_SECONDS,
            "keepalives_interval": DEFAULT_KEEPALIVES_INTERVAL_SECONDS,
            "keepalives_count": DEFAULT_KEEPALIVES_COUNT,
        }

    def _backoff_seconds(self, attempts: int) -> float:
        exponent = max(attempts - 1, 0)
        return min(self.requeue_max_seconds, self.requeue_base_seconds * (2**exponent))

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
        self.sender(
            target,
            with_board_skill_instruction(
                display_message(message), kind=transition.kind, role=transition.target_role
            ),
        )
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

    def _safe_json_payload(self, payload: str) -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload

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
        self._trace_notification(
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
        self._trace_notification(
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
        self._discard_notification(conn, notification_id, "stale_reminder_work_observed")
        self._traced_gate_defer_notifications.discard(notification_id)

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

    def _trace_notification(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        event: str,
        pane_busy: bool | None = None,
        busy_reason: str | None = None,
        region_digest: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        pane_state = None if pane_busy is None else ("busy" if pane_busy else "idle")
        try:
            conn.execute(
                """
SELECT ticket_board.record_notification_trace(
    %s::text,
    %s::bigint,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::text,
    %s::jsonb
)
""",
                (
                    ticket_id,
                    notification_id,
                    target_role,
                    kind,
                    event,
                    pane_state,
                    busy_reason,
                    region_digest,
                    json.dumps(detail or {}, sort_keys=True),
                ),
            )
        except Exception as exc:  # Trace failures must not wedge delivery.
            self.logger.warning("Failed to record notification trace for %s/%s: %s", notification_id, event, exc)

    def _ack_notification(self, conn: Any, notification_id: int) -> None:
        conn.execute("SELECT ticket_board.ack_notification(%s::bigint)", (notification_id,))

    def _discard_notification(self, conn: Any, notification_id: int, reason: str) -> None:
        """Remove a queued notification that was never delivered.

        Not an ack: ack_notification records delivery accounting, and for an
        idle_reminder that means incrementing idle_reminder_count, which turns
        the next idle wave into an escalation to the director (SYRD-32).
        """
        conn.execute(
            "SELECT ticket_board.discard_notification(%s::bigint, %s::text)",
            (notification_id, reason),
        )

    def _requeue_notification(
        self,
        conn: Any,
        notification_id: int,
        attempts: int,
        error: str,
        *,
        delay_seconds: float | None = None,
    ) -> None:
        delay_seconds = self._backoff_seconds(attempts) if delay_seconds is None else delay_seconds
        conn.execute(
            "SELECT ticket_board.requeue_notification(%s::bigint, %s::interval, %s::text)",
            (notification_id, f"{delay_seconds:g} seconds", error[:500]),
        )

    def _dead_letter_notification(
        self,
        conn: Any,
        notification_id: int,
        reason: str,
        *,
        target: str,
        message: str,
        attempts: int,
        payload: str,
    ) -> None:
        conn.execute(
            "SELECT ticket_board.dead_letter_notification(%s::bigint, %s::text, %s::jsonb)",
            (
                notification_id,
                reason[:500],
                json.dumps(
                    {
                        "target": target,
                        "message": message,
                        "attempts": attempts,
                        "payload": self._safe_json_payload(payload),
                    },
                    sort_keys=True,
                ),
            ),
        )
        self._traced_gate_defer_notifications.discard(notification_id)

    def _reset_busy_backoff_for_idle_roles(self, conn: Any, idle_since_by_role: dict[str, str]) -> int:
        if not idle_since_by_role:
            return 0
        try:
            result = conn.execute(
                "SELECT ticket_board.reset_notification_backoff_for_idle_roles(%s::jsonb)",
                (json.dumps(idle_since_by_role, sort_keys=True),),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to reset busy notification backoff for idle panes: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            reset_count = int(value)
        except (TypeError, ValueError):
            return 0
        if reset_count:
            self.logger.info("Reset busy notification backoff for %s idle-pane rows", reset_count)
        return reset_count

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

    def _idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        idle_since_by_role = getattr(gate_owner, "idle_since_by_role", None)
        if not callable(idle_since_by_role):
            return {}
        try:
            checked_roles = roles if roles is not None else sorted(self.role_targets)
            idle_since = dict(idle_since_by_role(checked_roles))
            self._record_work_observed_from_gate(checked_roles)
            return idle_since
        except Exception as exc:
            self.logger.warning("Failed to read pane idle hook state for stall nudges: %s", exc)
            return {}

    def _record_work_observed_from_gate(self, roles: list[str]) -> None:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        last_trace = getattr(gate_owner, "last_trace", None)
        if not callable(last_trace):
            return
        observed_at = datetime.now(timezone.utc).isoformat()
        for role in roles:
            target = self.role_targets.get(role)
            if target is None:
                continue
            try:
                trace = last_trace(target)
            except Exception:
                continue
            if isinstance(trace, ActivityTrace) and trace.busy and trace.reason in WORK_EVIDENCE_REASONS:
                self._work_observed_at_by_role[role] = observed_at

    def _work_observed_at_for_roles(self, roles: list[str]) -> dict[str, str]:
        return {role: self._work_observed_at_by_role[role] for role in roles if role in self._work_observed_at_by_role}

    def _turn_end_idle_since_by_role(self) -> dict[str, str]:
        gate_owner = getattr(self.activity_gate, "__self__", None)
        turn_end_idle_since_by_role = getattr(gate_owner, "turn_end_idle_since_by_role", None)
        if not callable(turn_end_idle_since_by_role):
            return {}
        try:
            return dict(turn_end_idle_since_by_role())
        except Exception as exc:
            self.logger.warning("Failed to read pane turn-end idle hook state for reminders: %s", exc)
            return {}

    def _fresh_turn_end_idle_since_by_role(self) -> dict[str, str]:
        turn_end_idle_since = self._turn_end_idle_since_by_role()
        fresh_turn_end_idle_since: dict[str, str] = {}
        for role, idle_since in turn_end_idle_since.items():
            if self._seen_turn_end_idle_since_by_role.get(role) != idle_since:
                fresh_turn_end_idle_since[role] = idle_since
            self._seen_turn_end_idle_since_by_role[role] = idle_since
        stale_roles = set(self._seen_turn_end_idle_since_by_role) - set(turn_end_idle_since)
        for role in stale_roles:
            self._seen_turn_end_idle_since_by_role.pop(role, None)
        return fresh_turn_end_idle_since

    def _present_fresh_idle_since_by_role(self) -> dict[str, str]:
        idle_since_by_role = self._idle_since_by_role(
            [role for role in sorted(self.role_targets) if role != "director"]
        )
        stale_roles = set(self._consumed_present_idle_since_by_role) - set(idle_since_by_role)
        for role in stale_roles:
            self._consumed_present_idle_since_by_role.pop(role, None)
        fresh_idle_since: dict[str, str] = {}
        for role, idle_since in idle_since_by_role.items():
            if role == "director":
                continue
            if self._consumed_present_idle_since_by_role.get(role) == idle_since:
                continue
            try:
                datetime.fromisoformat(idle_since)
            except ValueError:
                self._consumed_present_idle_since_by_role.pop(role, None)
                continue
            fresh_idle_since[role] = idle_since
            self._consumed_present_idle_since_by_role[role] = idle_since
        return fresh_idle_since

    def process_idle_turn_end_nudges(self, conn: Any) -> int:
        idle_since = self._fresh_turn_end_idle_since_by_role()
        if idle_since:
            self._consumed_present_idle_since_by_role.update(idle_since)
        else:
            idle_since = self._present_fresh_idle_since_by_role()
        if not idle_since:
            return 0
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_idle_turn_end_nudges(
    %s::jsonb,
    clock_timestamp()
)
""",
                (json.dumps(idle_since, sort_keys=True),),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue idle turn-end reminders: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info("Enqueued %s idle turn-end ticket nudges", enqueued)
        return enqueued

    def process_idle_stall_nudges(self, conn: Any) -> int:
        idle_since = self._idle_since_by_role()
        work_observed_at = self._work_observed_at_for_roles(sorted(self.role_targets))
        if not idle_since and not work_observed_at:
            return 0
        if idle_since:
            self._reset_busy_backoff_for_idle_roles(conn, idle_since)
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_idle_stall_nudges(
    %s::jsonb,
    clock_timestamp(),
    %s::interval,
    %s::interval,
    %s::integer,
    %s::jsonb
)
""",
                (
                    json.dumps(idle_since, sort_keys=True),
                    f"{self.idle_stall_grace_seconds:g} seconds",
                    f"{self.idle_stall_nudge_cadence_seconds:g} seconds",
                    self.idle_stall_escalate_after,
                    json.dumps(work_observed_at, sort_keys=True),
                ),
            )
            row = result.fetchone()
        except Exception as exc:
            self.logger.warning("Failed to enqueue idle-stall nudges: %s", exc)
            return 0
        if row is None:
            return 0
        value = row[0] if not isinstance(row, dict) else next(iter(row.values()))
        try:
            enqueued = int(value)
        except (TypeError, ValueError):
            return 0
        if enqueued:
            self.logger.info("Enqueued %s idle-stall ticket nudges", enqueued)
        return enqueued

    def _current_ticket_state(self, conn: Any, ticket_id: str) -> tuple[str, str, bool, bool, bool] | None:
        result = conn.execute(
            """
SELECT state,
       assignee,
       manually_controlled,
       coalesce((to_jsonb(t)->>'parked')::boolean, false) AS parked,
       ticket_board.ticket_has_unresolved_blockers(id) AS has_unresolved_blockers
FROM ticket_board.tickets t
WHERE id = %s
""",
            (ticket_id,),
        )
        row = result.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return (
                self._decode_text(row["state"]),
                self._decode_text(row["assignee"]),
                bool(row["manually_controlled"]),
                bool(row["parked"]),
                bool(row["has_unresolved_blockers"]),
            )
        return self._decode_text(row[0]), self._decode_text(row[1]), bool(row[2]), bool(row[3]), bool(row[4])

    def _current_target_role(self, kind: str, state: str, assignee: str) -> str | None:
        cfg = getattr(self, "workflow", None)
        if cfg and kind != "escalation":
            from .workflow_config import notification_role
            return notification_role(cfg, state, assignee)
        if kind == "transition":
            if state == "analysis":
                return "director"
            if state == "in_progress":
                return assignee if assignee != "unassigned" else None
            if state == "inspection":
                return "inspector"
            if state == "audit":
                return "audit"
            if state == "dat":
                return "director"
            if state == "user_review":
                return None
            if state == "director_review":
                return "director"
            return None
        if kind == "escalation":
            return "director"
        if state == "in_progress":
            return assignee if assignee != "unassigned" else None
        if state == "inspection":
            return "inspector"
        if state in {"audit", "dat", "director_review", "analysis", "backlog"}:
            return "director" if state in {"analysis", "backlog", "dat", "director_review"} else "audit"
        return None

    def _superseding_awaiting_role(
        self, conn: Any, notification_id: int, ticket_id: str, kind: str
    ) -> dict[str, Any] | None:
        """The handoff that makes an already-queued reminder wrong to deliver.

        A reminder, a nudge and an escalation all assert the same thing: the role
        that owns this ticket has not moved it. An awaiting-role handoff
        established after that wave was generated asserts the opposite -- the
        owner did move, and the next step belongs to somebody else.

        `notify_idle_turn_end_nudges()` and `notify_idle_stall_nudges()` already
        refuse to generate reminders while a wait is active. That is the enqueue
        half, and it is not enough on its own. The wait can
        also begin *after* the wave is generated, while the target's pane is
        busy, and the queued row then survives every deferral and is delivered
        later against a board that no longer agrees with it. That is what
        happened: an Ops escalation queued at 07:40:18 was deferred repeatedly on
        a busy Director pane and delivered at 07:50:56, more than eight minutes
        after Ops set `awaiting_role=director` at 07:42:37. Ops was not stuck, and
        the Director and the User were interrupted for it repeatedly (SYRD-99).

        Decided from the ticket's current notification state -- the awaiting role,
        the identity of the wait and its age -- rather than from the payload's
        copy of the state and assignee, which is precisely what did not change.
        Returns the trace detail when the reminder is superseded, or None.
        """
        if kind not in SUPERSEDABLE_REMINDER_KINDS:
            return None
        # `created_at` is when this wave was generated, and it is the only column
        # that means that: `updated_at` moves on every claim and every requeue,
        # so comparing against it would make each deferral look like a fresh
        # reminder and nothing would ever be superseded. The dedupe upsert leaves
        # `created_at` alone for the same reason.
        result = conn.execute(
            """
SELECT ns.awaiting_role,
       ns.awaiting_since_at,
       q.created_at AS queued_at,
       ticket_board.ticket_awaiting_role_is_active(
           ns.awaiting_role, ns.awaiting_since_at, clock_timestamp()
       ) AS wait_is_active,
       (ns.awaiting_since_at > q.created_at) AS established_after_queueing
FROM ticket_board.ticket_notification_queue q
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = q.ticket_id
WHERE q.id = %s AND q.ticket_id = %s
""",
            (notification_id, ticket_id),
        )
        row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
        if row is None:
            return None
        if isinstance(row, dict):
            awaiting_role, awaiting_since_at, queued_at, wait_is_active, established_after = (
                row["awaiting_role"], row["awaiting_since_at"], row["queued_at"],
                row["wait_is_active"], row["established_after_queueing"],
            )
        else:
            awaiting_role, awaiting_since_at, queued_at, wait_is_active, established_after = row[:5]
        # A wait that was cleared, or one whose window has expired, supersedes
        # nothing: the owner is answerable again and the reminder is the truth.
        if not wait_is_active or not established_after:
            return None
        return {
            "awaiting_role": self._decode_text(awaiting_role),
            "awaiting_since_at": str(awaiting_since_at),
            "queued_at": str(queued_at),
            "superseded_kind": kind,
        }

    def _drop_superseded_notification(
        self,
        conn: Any,
        *,
        notification_id: int,
        ticket_id: str,
        target_role: str,
        kind: str,
        detail: dict[str, Any],
        phase: str,
    ) -> None:
        """Remove one superseded reminder, saying exactly why it went."""
        self.logger.info(
            "Dropping %s notification %s for %s: %s took the handoff at %s, after it was queued at %s",
            kind, notification_id, ticket_id,
            detail.get("awaiting_role"), detail.get("awaiting_since_at"), detail.get("queued_at"),
        )
        self._trace_notification(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="drop",
            busy_reason=SUPERSEDED_BY_AWAITING_ROLE,
            detail={**detail, "phase": phase},
        )
        self._trace_notification(
            conn,
            notification_id=notification_id,
            ticket_id=ticket_id,
            target_role=target_role,
            kind=kind,
            event="listener_discard",
            detail={"reason": SUPERSEDED_BY_AWAITING_ROLE, "phase": phase},
        )
        # Discarded, never acked. This reminder was answered before it could be
        # delivered, and `ack_notification` records delivery accounting:
        # for an idle_reminder it increments idle_reminder_count, which the next
        # idle wave reads as "already reminded" and turns into an escalation to
        # the Director (SYRD-32). Acking here would answer one false escalation
        # by scheduling the next one.
        self._discard_notification(conn, notification_id, SUPERSEDED_BY_AWAITING_ROLE)
        self._traced_gate_defer_notifications.discard(notification_id)

    def _notification_is_current(self, conn: Any, ticket_id: str, target_role: str, payload: str) -> bool:
        current = self._current_ticket_state(conn, ticket_id)
        if current is None:
            return False
        current_state, current_assignee, manually_controlled, parked, has_unresolved_blockers = current

        terminal_states = {stage["name"] for stage in self.workflow["stages"] if stage["terminal"]} if getattr(self, "workflow", None) else TERMINAL_STATES
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return current_state not in terminal_states
        if not isinstance(parsed, dict):
            return current_state not in terminal_states

        payload_ticket_id = str(parsed.get("id", ticket_id)).strip().upper()
        if payload_ticket_id and payload_ticket_id != ticket_id:
            return False

        expected_state = str(parsed.get("state") or parsed.get("new_state") or "").strip()
        if expected_state and current_state != expected_state:
            return False

        expected_assignee = str(parsed.get("assignee") or "").strip().lower()
        if expected_assignee and current_assignee != expected_assignee:
            return False

        kind = str(parsed.get("kind") or "").strip().lower()
        terminal_states = {stage["name"] for stage in self.workflow["stages"] if stage["terminal"]} if getattr(self,"workflow",None) else TERMINAL_STATES
        if current_state in terminal_states:
            return (
                kind == "transition"
                and expected_state in terminal_states
                and current_state == expected_state
            )
        if kind == "awaiting_role":
            # Wait identity, not delivery ACK or comments, controls resolution.
            # Expired windows prevent a restart from delivering a reminder burst.
            if (self.workflow and not any(stage["name"] == current_state and not stage["terminal"] and stage["kind"] != "draft" for stage in self.workflow["stages"])) or (not self.workflow and current_state not in {"in_progress", "inspection", "audit", "user_review"}):
                return False
            expected_target = "director" if parsed.get("step") == 4 else parsed.get("awaiting_role")
            if target_role != expected_target:
                return False
            row = conn.execute(
                """
SELECT EXISTS (
    SELECT 1 FROM ticket_board.ticket_notification_state
    WHERE ticket_id = %s AND awaiting_role = %s
      AND awaiting_since_at = %s::timestamptz
      AND clock_timestamp() < %s::timestamptz
)
""",
                (ticket_id, parsed.get("awaiting_role"), parsed.get("awaiting_since_at"), parsed.get("expires_at")),
            ).fetchone()
            return bool(row["exists"] if isinstance(row, dict) else row[0])
        if kind == "escalation":
            return (
                target_role == "director"
                and current_state in NUDGE_ELIGIBLE_STATES
                and current_assignee != "unassigned"
                and not manually_controlled
                and not parked
                and not has_unresolved_blockers
            )
        required_final_review_handoff = (
            kind == "transition"
            and expected_state == "director_review"
            and current_state == "director_review"
            and expected_assignee == "director"
            and current_assignee == "director"
            and target_role == "director"
        )
        if kind in {"transition", "idle_reminder", "nudge"} and (
            manually_controlled or parked or has_unresolved_blockers
        ) and not required_final_review_handoff:
            # Scheduling flags hold owner work and optional reminders. They do
            # not undo a completed handoff to the Director's final review.
            return False

        current_target_role = self._current_target_role(kind, current_state, current_assignee)
        return current_target_role == target_role if getattr(self, "workflow", None) else current_target_role is None or current_target_role == target_role

    def _finish_current_blocker(self, conn: Any, ticket_id: str, target_role: str, payload: str) -> str:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        if str(parsed.get("kind") or "").strip().lower() != "transition":
            return ""
        if str(parsed.get("new_state") or "").strip() != "in_progress":
            return ""
        assignee = str(parsed.get("assignee") or target_role).strip().lower()
        if assignee != target_role or target_role in {"director", "audit", "inspector"}:
            return ""
        result = conn.execute(
            "SELECT ticket_board.finish_current_blocker(%s::text, %s::text)",
            (ticket_id, target_role),
        )
        row = result.fetchone()
        if row is None:
            return ""
        if isinstance(row, dict):
            return self._decode_text(row.get("id", ""))
        return self._decode_text(row[0])

    def refresh_workflow(self, conn: Any) -> None:
        from .workflow_config import read_configuration, validate
        document = read_configuration(conn)
        self.workflow = validate(document, project=self.project) if document else None
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
            self._trace_notification(
                conn,
                notification_id=notification_id,
                ticket_id=ticket_id,
                target_role=target_role,
                kind=kind,
                event="listener_claim",
                detail={"attempts": attempts, "payload": self._safe_json_payload(payload)},
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
                    self._requeue_notification(
                        conn, notification_id, attempts, "role_runtime_unassigned"
                    )
                    continue
                self.logger.warning("Acking notification %s with unknown target role %s", notification_id, target_role)
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="drop",
                    busy_reason="unknown_target_role",
                    detail={"payload": self._safe_json_payload(payload)},
                )
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="listener_ack",
                    detail={"reason": "unknown_target_role"},
                )
                self._ack_notification(conn, notification_id)
                self._traced_gate_defer_notifications.discard(notification_id)
                continue
            if not self._notification_is_current(conn, ticket_id, target_role, payload):
                self.logger.info("Dropping stale notification %s for %s: %s", notification_id, ticket_id, payload)
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="drop",
                    busy_reason="stale_notification",
                    detail={"payload": self._safe_json_payload(payload)},
                )
                self._trace_notification(
                    conn,
                    notification_id=notification_id,
                    ticket_id=ticket_id,
                    target_role=target_role,
                    kind=kind,
                    event="listener_ack",
                    detail={"reason": "stale_notification"},
                )
                self._ack_notification(conn, notification_id)
                self._traced_gate_defer_notifications.discard(notification_id)
                continue
            superseded = self._superseding_awaiting_role(conn, notification_id, ticket_id, kind)
            if superseded is not None:
                self._drop_superseded_notification(
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
                self._dead_letter_notification(
                    conn,
                    notification_id,
                    "tmux_target_missing",
                    target=target,
                    message=message,
                    attempts=attempts,
                    payload=payload,
                )
                continue
            finish_current_ticket = self._finish_current_blocker(conn, ticket_id, target_role, payload)
            if finish_current_ticket:
                self.logger.info(
                    "Holding notification %s for %s until %s leaves in_progress",
                    notification_id,
                    ticket_id,
                    finish_current_ticket,
                )
                if notification_id not in self._traced_gate_defer_notifications:
                    self._trace_notification(
                        conn,
                        notification_id=notification_id,
                        ticket_id=ticket_id,
                        target_role=target_role,
                        kind=kind,
                        event="finish_current_defer",
                        pane_busy=True,
                        busy_reason="finish_current",
                        detail={"current_ticket_id": finish_current_ticket, "attempts": attempts},
                    )
                    self._traced_gate_defer_notifications.add(notification_id)
                self._requeue_notification(
                    conn,
                    notification_id,
                    attempts,
                    FINISH_CURRENT_REQUEUE_ERROR,
                )
                continue
            pane_busy, activity_trace = self._activity_state_for_notification(kind, target)
            composer_before = self._composer_snapshot(target)
            if self._should_defer_for_activity(activity_trace):
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
                if notification_id not in self._traced_gate_defer_notifications:
                    self._trace_notification(
                        conn,
                        notification_id=notification_id,
                        ticket_id=ticket_id,
                        target_role=target_role,
                        kind=kind,
                        event="gate_defer",
                        pane_busy=True,
                        busy_reason=activity_trace.reason,
                        region_digest=activity_trace.region_digest,
                        detail=self._delivery_diagnostic_detail(
                            target=target,
                            message=message,
                            attempts=attempts,
                            activity_trace=activity_trace,
                            before=composer_before,
                            decision="defer",
                            reason=activity_trace.reason,
                        ),
                    )
                    self._traced_gate_defer_notifications.add(notification_id)
                self._requeue_notification(
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
            if self._should_defer_for_activity(activity_trace):
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
                if notification_id not in self._traced_gate_defer_notifications:
                    self._trace_notification(
                        conn,
                        notification_id=notification_id,
                        ticket_id=ticket_id,
                        target_role=target_role,
                        kind=kind,
                        event="gate_defer",
                        pane_busy=True,
                        busy_reason=activity_trace.reason,
                        region_digest=activity_trace.region_digest,
                        detail={
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
                    self._traced_gate_defer_notifications.add(notification_id)
                self._requeue_notification(
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
            superseded = self._superseding_awaiting_role(conn, notification_id, ticket_id, kind)
            if superseded is not None:
                self._drop_superseded_notification(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, detail=superseded,
                    phase="pre_send_recheck",
                )
                continue
            # Recheck handoffs too, so a resolution during that probe suppresses
            # this delivery.
            if kind == "awaiting_role" and not self._notification_is_current(conn, ticket_id, target_role, payload):
                self._trace_notification(
                    conn, notification_id=notification_id, ticket_id=ticket_id,
                    target_role=target_role, kind=kind, event="drop",
                    busy_reason="stale_notification", detail={"phase": "pre_send_recheck"},
                )
                self._ack_notification(conn, notification_id)
                continue
            directorctl_diagnostic: dict[str, Any] = {}
            try:
                sender_result = self.sender(
                    target,
                    with_board_skill_instruction(display_message(message), kind=kind, role=target_role),
                )
                if isinstance(sender_result, dict):
                    directorctl_diagnostic = sender_result
            except (subprocess.SubprocessError, OSError) as exc:
                if isinstance(exc, subprocess.CalledProcessError):
                    stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
                    directorctl_diagnostic = parse_directorctl_diagnostic(stdout if isinstance(stdout, str) else None)
                failure_reason = delivery_failure_reason(exc, target)
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
                self._trace_notification(
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
                    ),
                )
                if failure_reason == "tmux_target_missing":
                    self._dead_letter_notification(
                        conn,
                        notification_id,
                        failure_reason,
                        target=target,
                        message=message,
                        attempts=attempts,
                        payload=payload,
                    )
                else:
                    self._requeue_notification(conn, notification_id, attempts, failure_reason)
                continue
            composer_after = self._composer_snapshot(target)
            self._trace_notification(
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
                ),
            )
            self._trace_notification(
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
            self._ack_notification(conn, notification_id)
            self._traced_gate_defer_notifications.discard(notification_id)
            self.delivered_count += 1
            delivered += 1
            self.logger.info("Delivered queued notification %s for %s to %s: %s", notification_id, ticket_id, target, payload)
        return delivered

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


def load_runtime_assignments(*, board_url: str, assignments_json: str) -> Any:
    if assignments_json:
        return json.loads(Path(assignments_json).read_text(encoding="utf-8"))
    if not board_url:
        raise SystemExit(
            "ticket notify listener: --verify-pane-state-authority needs --board-url or TICKET_BOARD_URL"
        )
    import urllib.request

    with urllib.request.urlopen(board_url.rstrip("/") + "/api/runtime-assignments", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def verify_pane_state_authority(args: argparse.Namespace) -> int:
    """Fail closed when the configured directory serves none of the live roles.

    Deliberately not a liveness check. The listener process, its socket and the
    board's runtime assignments were all healthy throughout SYRD-95; the one
    thing that was wrong was which directory it read, and only this comparison
    can see that.
    """

    payload = load_runtime_assignments(
        board_url=args.board_url, assignments_json=args.assignments_json
    )
    report = pane_state_authority(
        registered_pane_targets(payload), PaneHookStateStore(args.pane_state_dir)
    )
    print(report.describe())
    if report.ok:
        return 0
    print(
        "the listener would defer every notification while looking healthy; "
        "point TICKET_BOARD_PANE_STATE_DIR at the directory the installed pane hooks write to",
        file=sys.stderr,
    )
    return 1


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
