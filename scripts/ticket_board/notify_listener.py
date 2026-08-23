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
from typing import Any, Callable

import psycopg
from psycopg import sql

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
DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_IDLE_STALL_GRACE_SECONDS = 45.0
DEFAULT_IDLE_STALL_NUDGE_CADENCE_SECONDS = 30 * 60.0
DEFAULT_IDLE_STALL_ESCALATE_AFTER = 2
# Present-idle fallback is age-unbounded: an idle hook remains valid until the
# pane reports busy/blocked again, and the live activity gate still runs before
# any send.
DEFAULT_PRESENT_IDLE_FRESHNESS_SECONDS = 0.0
IDLE_TURN_END_SOURCES = frozenset(
    {"claude.Stop", "codex.Stop", "gemini.AfterAgent", "gemini.PostInvocation", "gemini.Stop"}
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
IMMEDIATE_DELIVERY_KINDS = frozenset({"ticket_update"})
DEFAULT_PRE_SEND_RECHECK_DELAY_SECONDS = 0.5
DEFAULT_DIRECTOR_COMPOSER_HOME_X = 2
DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS = 0.0
DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS = 1.2
DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS = 120.0
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
LOGGER = logging.getLogger(__name__)
DEFAULT_DIRECTORCTL = "/home/agent/bin/directorctl"


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


def pane_content_digest(pane_text: str) -> str:
    return hashlib.sha256(pane_text.encode("utf-8")).hexdigest()



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
        return ROLE_TO_TARGET["director"]
    if transition.new_state == "director_review":
        return ROLE_TO_TARGET["director"]
    return None


def message_for_transition(transition: Transition) -> str | None:
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
        self.state_store = state_store or PaneHookStateStore()
        self.director_target = director_target
        self.cursor_position_runner = cursor_position_runner
        self.capture_pane_runner = capture_pane_runner
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

    def _record_trace(self, target: str, trace: ActivityTrace) -> bool:
        self._last_trace_by_target[target] = trace
        return trace.busy

    def last_trace(self, target: str) -> ActivityTrace | None:
        return self._last_trace_by_target.get(target)

    def missing_hook_targets(self, targets: list[str] | None = None) -> list[str]:
        checked_targets = targets or sorted(set(ROLE_TO_TARGET.values()))
        return [target for target in checked_targets if self.state_store.read(target) is None]

    def _idle_hook_states_by_role(self, roles: list[str] | None = None) -> dict[str, PaneHookState]:
        checked_roles = roles or sorted(ROLE_TO_TARGET)
        idle_states: dict[str, PaneHookState] = {}
        for role in checked_roles:
            target = ROLE_TO_TARGET.get(role)
            if target is None:
                continue
            state = self.state_store.read(target)
            if state is None or state.state != "idle":
                continue
            idle_states[role] = state
        return idle_states

    def idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        checked_roles = roles or sorted(ROLE_TO_TARGET)
        idle_since: dict[str, str] = {}
        for role in checked_roles:
            target = ROLE_TO_TARGET.get(role)
            if target is None:
                continue
            if target == self.director_target and self.is_busy(target):
                continue
            if target != self.director_target and self.is_busy(target):
                continue
            state = self.state_store.read(target)
            if state is None or state.state != "idle":
                continue
            idle_since[role] = datetime.fromtimestamp(state.updated_at, timezone.utc).isoformat()
        return idle_since

    def turn_end_idle_since_by_role(self, roles: list[str] | None = None) -> dict[str, str]:
        return {
            role: datetime.fromtimestamp(state.updated_at, timezone.utc).isoformat()
            for role, state in self._idle_hook_states_by_role(roles).items()
            if state.source in IDLE_TURN_END_SOURCES
        }

    def _tmux_session_for_target(self, target: str) -> str:
        return target.split(":", 1)[0]

    def _role_for_target(self, target: str) -> str:
        session = self._tmux_session_for_target(target)
        return session.rsplit("-", 1)[-1].strip().lower()

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
        first = self._captured_working_timer_probe(target)
        if not first.captured or not first.observable:
            return ActivityTrace(True, "working_timer_unobservable")
        sample_delay = self.working_timer_sample_delay_seconds if sample_delay_seconds is None else sample_delay_seconds
        if sample_delay > 0:
            self.sleeper(sample_delay)
        second = self._captured_working_timer_probe(target)
        if not second.captured or not second.observable:
            return ActivityTrace(True, "working_timer_unobservable")
        if second.digest != first.digest:
            return ActivityTrace(True, "pane_content_changed", region_digest=second.digest)
        return None

    def _working_timer_idle_probe_trace(self, target: str) -> ActivityTrace:
        first = self._captured_working_timer_probe(target)
        if not first.captured or not first.observable:
            return ActivityTrace(True, "working_timer_unobservable")
        if self.idle_working_timer_sample_delay_seconds > 0:
            self.sleeper(self.idle_working_timer_sample_delay_seconds)
        second = self._captured_working_timer_probe(target)
        if not second.captured or not second.observable:
            return ActivityTrace(True, "working_timer_unobservable")
        if second.digest != first.digest:
            return ActivityTrace(True, "pane_content_changed", region_digest=second.digest)
        return ActivityTrace(False, "working_timer_idle", region_digest=second.digest)

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

    def _trusted_idle_source_trace(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        return None

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
        if untrusted_idle_trace is not None:
            return untrusted_idle_trace
        if check_trusted_working:
            trusted_idle_trace = self._trusted_idle_source_trace(target, state)
            if trusted_idle_trace is not None:
                return trusted_idle_trace
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

    def is_busy(self, target: str) -> bool:
        state = self.state_store.read(target)
        if state is None:
            if target == self.director_target:
                self._reset_director_startup_hold()
            return self._record_trace(target, ActivityTrace(True, "no_hook_state"))
        previous = self._last_hook_state_by_target.get(target)
        current = (state.state, state.updated_at)
        self._last_hook_state_by_target[target] = current
        if state.state != "idle":
            if target == self.director_target:
                self._reset_director_startup_hold()
            reason = "hook_blocked" if state.state == "blocked" else "hook_busy"
            stale_trace = self._stale_codex_busy_trace(target, state)
            if stale_trace is not None:
                return self._record_trace(target, stale_trace)
            return self._record_trace(target, ActivityTrace(True, reason))
        if previous is not None and previous[0] != "idle" and target == self.director_target:
            self._reset_director_startup_hold()
        return self._record_trace(target, self._idle_cursor_trace(target, state))

    def is_working(self, target: str) -> bool:
        return self.is_busy(target)


class TicketBoardNotifyListener:
    def __init__(
        self,
        *,
        conninfo: str,
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
    ) -> None:
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
        self.delivered_count = 0
        self._traced_gate_defer_notifications: set[int] = set()
        self._seen_turn_end_idle_since_by_role: dict[str, str] = {}
        self._consumed_present_idle_since_by_role: dict[str, str] = {}

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
        gate_owner = getattr(self.activity_gate, "__self__", None)
        if pre_send_recheck:
            pre_send_busy = getattr(gate_owner, "pre_send_anti_clobber_busy", None)
            if callable(pre_send_busy):
                pane_busy = bool(pre_send_busy(target))
                return pane_busy, self._activity_trace(target, pane_busy)
        if kind in IMMEDIATE_DELIVERY_KINDS:
            anti_clobber_busy = getattr(gate_owner, "anti_clobber_busy", None)
            if callable(anti_clobber_busy):
                pane_busy = bool(anti_clobber_busy(target))
                return pane_busy, self._activity_trace(target, pane_busy)
        pane_busy = self.activity_gate(target)
        return pane_busy, self._activity_trace(target, pane_busy)

    def _should_defer_for_activity(self, activity_trace: ActivityTrace) -> bool:
        return activity_trace.busy

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
            return dict(idle_since_by_role(roles)) if roles is not None else dict(idle_since_by_role())
        except Exception as exc:
            self.logger.warning("Failed to read pane idle hook state for stall nudges: %s", exc)
            return {}

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
            [role for role in sorted(ROLE_TO_TARGET) if role != "director"]
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
        if not idle_since:
            return 0
        self._reset_busy_backoff_for_idle_roles(conn, idle_since)
        try:
            result = conn.execute(
                """
SELECT ticket_board.notify_idle_stall_nudges(
    %s::jsonb,
    clock_timestamp(),
    %s::interval,
    %s::interval,
    %s::integer
)
""",
                (
                    json.dumps(idle_since, sort_keys=True),
                    f"{self.idle_stall_grace_seconds:g} seconds",
                    f"{self.idle_stall_nudge_cadence_seconds:g} seconds",
                    self.idle_stall_escalate_after,
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
                return "director"
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

    def _notification_is_current(self, conn: Any, ticket_id: str, target_role: str, payload: str) -> bool:
        current = self._current_ticket_state(conn, ticket_id)
        if current is None:
            return False
        current_state, current_assignee, manually_controlled, parked, has_unresolved_blockers = current

        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            if current_state in TERMINAL_STATES:
                return False
            return True
        if not isinstance(parsed, dict):
            if current_state in TERMINAL_STATES:
                return False
            return True

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
        if current_state in TERMINAL_STATES:
            return (
                kind == "transition"
                and expected_state in TERMINAL_STATES
                and current_state == expected_state
            )
        if kind == "escalation":
            return (
                target_role == "director"
                and current_state in NUDGE_ELIGIBLE_STATES
                and current_assignee != "unassigned"
                and not manually_controlled
                and not parked
                and not has_unresolved_blockers
            )
        if kind in {"transition", "idle_reminder", "nudge"} and (
            manually_controlled or parked or has_unresolved_blockers
        ):
            return False

        current_target_role = self._current_target_role(kind, current_state, current_assignee)
        return current_target_role is None or current_target_role == target_role

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

    def process_due_notifications(self, conn: Any, *, max_notifications: int | None = None) -> int:
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
            target = ROLE_TO_TARGET.get(target_role)
            if self.stop_event.is_set():
                break
            if target is None:
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
                    "finish current",
                    delay_seconds=self.busy_requeue_seconds,
                )
                continue
            pane_busy, activity_trace = self._activity_state_for_notification(kind, target)
            composer_before = self._composer_snapshot(target)
            if self._should_defer_for_activity(activity_trace):
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
                    "pane busy",
                )
                continue
            if self.pre_send_recheck_delay_seconds > 0:
                self.sleeper(self.pre_send_recheck_delay_seconds)
                if self.stop_event.is_set():
                    break
            pane_busy, activity_trace = self._activity_state_for_notification(kind, target, pre_send_recheck=True)
            composer_before = self._composer_snapshot(target)
            if self._should_defer_for_activity(activity_trace):
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
                    "pane busy",
                )
                continue
            directorctl_diagnostic: dict[str, Any] = {}
            try:
                sender_result = self.sender(target, display_message(message))
                if isinstance(sender_result, dict):
                    directorctl_diagnostic = sender_result
            except (subprocess.SubprocessError, OSError) as exc:
                if isinstance(exc, subprocess.CalledProcessError):
                    stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
                    directorctl_diagnostic = parse_directorctl_diagnostic(stdout if isinstance(stdout, str) else None)
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
                    busy_reason=str(exc),
                    region_digest=activity_trace.region_digest,
                    detail=self._delivery_diagnostic_detail(
                        target=target,
                        message=message,
                        attempts=attempts,
                        activity_trace=activity_trace,
                        before=composer_before,
                        after=composer_after,
                        decision="send_failed",
                        reason=str(exc),
                        directorctl_diagnostic=directorctl_diagnostic,
                    ),
                )
                self._requeue_notification(conn, notification_id, attempts, str(exc))
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    listener = TicketBoardNotifyListener(
        conninfo=args.database,
        channel=args.channel,
        sender=DirectorctlSender(args.directorctl),
        activity_gate=PaneActivityGate(
            state_store=PaneHookStateStore(args.pane_state_dir),
            director_composing_timeout_seconds=args.director_composing_timeout_seconds,
            idle_working_timer_sample_delay_seconds=args.idle_working_timer_sample_delay_seconds,
        ).is_working,
        reconnect_seconds=args.reconnect_seconds,
        poll_seconds=args.poll_seconds,
        pre_send_recheck_delay_seconds=args.pre_send_recheck_delay_seconds,
    )
    listener.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
