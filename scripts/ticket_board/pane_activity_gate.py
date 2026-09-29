"""Whether a role's pane may be typed into now: the pane activity gate and the evidence it reads.

`PaneActivityGate` decides whether a role's tmux pane is busy -- its runtime's
hook state, the composer and cursor, the working timer, a permission prompt,
and work still running in the pane's children -- and records why, so the
listener delivers a notification only into an idle pane (SYRD-58). Beside it,
the evidence it reads and nothing else does: the process table and the child
work sample (`read_process_table`, `descendant_work_sample`, `ChildWorkSample`,
`ChildWorkMemory`), the working-timer probe (`WorkingProbe`), the composer
snapshot of a captured pane, and its tuning constants. The role-to-pane map
(`DEFAULT_PROJECT`, `ROLE_TO_TARGET`) and the three timing defaults the
listener and its parser share with the gate move with it, because the gate's
defaults bind them when the class is defined.

Moved out of `scripts/ticket_board/notify_listener.py` unchanged (SYRD-475).
`notify_listener` imports this module and re-exports every name, so every module
and test that imports them from there, or patches them there, still reaches the
same objects -- including `role_runtime` and `role_pane_entry`, which build a
gate from there. What the moved code reads of `notify_listener` when it runs --
the hook-state store, the traces, the composer snapshot type, the logger, and
each other -- is read through it, so a patch there still reaches the gate. This
module imports `notify_listener` only inside the functions and methods that
need it, when they run; at load it imports the standard library alone.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_BUSY_REQUEUE_SECONDS = 1.0


DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS = 15 * 60.0


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


DEFAULT_PROJECT = os.environ.get("TICKET_BOARD_PROJECT", "").strip() or os.environ.get("PGU_TICKET_BOARD_PROJECT", "").strip() or "pgu"
ROLE_TO_TARGET = {
    role: f"{DEFAULT_PROJECT}-{role}:0.0"
    for role in ("director", "main", "app", "perf", "research", "ops", "audit", "inspector")
}


PERMISSION_PROMPT_BLOCK_SOURCES = frozenset({
    "claude.Notification.permission_prompt",
    "codex.PermissionRequest",
})


#: Descendants an earlier turn left behind, which the runtime has since declared
#: itself idle after and which are making no progress. Distinct from
#: `pane_child_work`, which is this turn's work and still holds delivery; from
#: the hook's own busy verdict; and from a human at the composer (SYRD-101).
STALE_PRIOR_TURN_CHILD_WORK = "stale_prior_turn_child_work"


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
    #: When each descendant started, in epoch seconds, for the descendants whose
    #: start time could be read. This is what tells a child the current turn
    #: launched from one an earlier turn left behind, without naming a program
    #: and without a timeout (SYRD-101).
    starts: tuple[tuple[int, float], ...] = ()
    #: CPU per descendant, not only the total. A total cannot say who used it,
    #: and a descendant that exits takes its accumulated ticks out of the sum:
    #: one old child leaving with 100 ticks hides a surviving sibling's 3 and
    #: makes real work look like nothing at all (SYRD-101 review).
    cpu_by_pid: tuple[tuple[int, int], ...] = ()


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


def _boot_time_epoch_seconds(proc_root: Path = Path("/proc")) -> float:
    """When this machine booted, so a process's start time can be a wall clock.

    `/proc/<pid>/stat` gives a start time in clock ticks since boot, which says
    nothing on its own about whether a descendant predates a turn that ended at
    a known moment. Boot time turns it into a comparable one (SYRD-101).
    """
    try:
        stat = (proc_root / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for line in stat.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split(None, 1)[1].strip())
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def read_process_table(
    proc_root: Path = Path("/proc"),
) -> tuple[tuple[int, int, int, int, float], ...]:
    """(pid, ppid, cpu ticks, session, start time) for every visible process.

    Read from /proc rather than by running ps: the gate runs on every delivery
    decision, and a fork per decision is a cost the listener does not need.
    The comm field can contain spaces and parentheses, so the split is on its
    closing parenthesis rather than on whitespace.

    The start time is epoch seconds, or 0.0 when it could not be worked out.
    Consumers accept rows without it, and treat a missing one as unknown rather
    than as old -- guessing the other way would let an unreadable process be
    dismissed as somebody else's leftovers (SYRD-101).
    """
    from . import notify_listener as listener

    rows: list[tuple[int, int, int, int, float]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return ()
    boot = listener._boot_time_epoch_seconds(proc_root)
    try:
        ticks_per_second = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        ticks_per_second = 0.0
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
        started_at = 0.0
        if boot > 0 and ticks_per_second > 0 and len(fields) >= 20:
            try:
                started_at = boot + int(fields[19]) / ticks_per_second
            except ValueError:
                started_at = 0.0
        try:
            rows.append(
                (
                    int(entry.name),
                    int(fields[1]),
                    int(fields[11]) + int(fields[12]),
                    int(fields[3]),
                    started_at,
                )
            )
        except ValueError:
            continue
    return tuple(rows)


def descendant_work_sample(
    pane_pid: int, table: Sequence[Sequence[Any]]
) -> ChildWorkSample:
    """Every process under a pane, and the CPU they have used between them.

    Descendants rather than a named set of programs: what a turn runs is the
    tenant's business, and a rule that named commands would be a list to keep
    in step with every runtime and every tool (SYRD-58).
    """
    from . import notify_listener as listener

    children: dict[int, list[int]] = {}
    cpu_by_pid: dict[int, int] = {}
    session_by_pid: dict[int, int] = {}
    started_by_pid: dict[int, float] = {}
    for row in table:
        # A row carrying no start time is read as one whose start time is
        # unknown, so a caller that never supplied one keeps exactly the
        # behaviour it had (SYRD-101).
        pid, ppid, cpu_ticks, session = int(row[0]), int(row[1]), int(row[2]), int(row[3])
        children.setdefault(ppid, []).append(pid)
        cpu_by_pid[pid] = cpu_ticks
        session_by_pid[pid] = session
        started_by_pid[pid] = float(row[4]) if len(row) > 4 else 0.0
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
    return listener.ChildWorkSample(
        True,
        frozenset(seen),
        sum(cpu_by_pid.get(pid, 0) for pid in seen),
        detached,
        tuple(sorted((pid, started_by_pid.get(pid, 0.0)) for pid in seen)),
        tuple(sorted((pid, cpu_by_pid.get(pid, 0)) for pid in seen)),
    )


def pane_content_digest(pane_text: str) -> str:
    return hashlib.sha256(pane_text.encode("utf-8")).hexdigest()


def composer_snapshot_from_pane_text(pane_text: str) -> ComposerSnapshot:
    from . import notify_listener as listener

    if not pane_text:
        return listener.ComposerSnapshot(False, error="empty_capture")
    lines = pane_text.splitlines()
    horizontal_indices = [idx for idx, line in enumerate(lines) if line.count("─") >= 40]
    if len(horizontal_indices) < 2:
        return listener.ComposerSnapshot(True, marker_found=False)
    composer_content = "\n".join(lines[horizontal_indices[-2] + 1 : horizontal_indices[-1]])
    cleaned = re.sub(r"^[❯\u276f\s\>\-\*]+", "", composer_content, flags=re.MULTILINE)
    stripped = cleaned.strip()
    digest = hashlib.sha256(stripped.encode("utf-8")).hexdigest() if stripped else ""
    return listener.ComposerSnapshot(
        True,
        marker_found=True,
        active=bool(stripped),
        content_sha256=digest,
        content_length=len(stripped),
    )


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
        from . import notify_listener as listener

        self.role_targets = dict(listener.ROLE_TO_TARGET)
        self.active_runtime_roles: set[str] = set()
        self.state_store = state_store or listener.PaneHookStateStore()
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
            for role, runtime in (role_runtimes or listener.DEFAULT_ROLE_RUNTIMES).items()
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
        from . import notify_listener as listener

        self._last_trace_by_target[target] = trace
        if trace.busy and trace.reason in listener.WORK_EVIDENCE_REASONS:
            state_ts = self._observed_state_ts_by_target.get(target)
            if state_ts is not None:
                self._work_evidence_by_target[target] = (state_ts, self.wall_time())
        return trace.busy

    def last_trace(self, target: str) -> ActivityTrace | None:
        return self._last_trace_by_target.get(target)

    def submission_witnessed(self, target: str, since: float) -> bool | None:
        """Did the recipient's OWN runtime record a turn after ``since``?

        directorctl's check is what the pane looks like, and a busy agent's
        pane looks submitted whatever happened: its output moves and its
        status line says "Working". The runtime's hooks are a separate witness,
        written by the CLI itself when a prompt starts a turn: UserPromptSubmit
        (claude, codex), PreInvocation (gemini), pre_llm_call (hermes). The gate
        only sends to an idle pane, so a turn START after the send is the notice
        being taken (SYRD-268). A turn END is not: it may close a turn that was
        already running when the notice was typed into its composer.

        None when there is no hook state to ask: that is "cannot tell", and it
        must never read as either answer.
        """
        from . import notify_listener as listener

        state = self.state_store.read(target)
        if state is None:
            return None
        if state.turn_started_at is not None:
            return state.turn_started_at >= since
        # A state file written before turn starts were recorded: only a start
        # that is still the latest write can be read from it.
        event = (state.source or "").rsplit(".", 1)[-1]
        return state.updated_at >= since and event in listener.TURN_START_EVENTS

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
        from . import notify_listener as listener

        turn_end_idle_since: dict[str, str] = {}
        for role, state in self._idle_hook_states_by_role(roles).items():
            if state.source not in listener.IDLE_TURN_END_SOURCES:
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
        from . import notify_listener as listener

        for role, configured_target in self.role_targets.items():
            if configured_target == target:
                return role
        session = self._tmux_session_for_target(target)
        prefix = f"{listener.DEFAULT_PROJECT}-"
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
        from . import notify_listener as listener

        try:
            proc = self.capture_pane_runner(
                ["tmux", "capture-pane", "-p", "-J", "-t", target],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return listener.WorkingProbe(False, False)
        return listener.WorkingProbe(True, True, listener.pane_content_digest(proc.stdout))

    def _working_timer_probe_trace(self, target: str, *, sample_delay_seconds: float) -> ActivityTrace:
        from . import notify_listener as listener

        first = self._captured_working_timer_probe(target)
        if not first.captured or not first.observable:
            return listener.ActivityTrace(True, "working_timer_unobservable")
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
                return listener.ActivityTrace(True, "working_timer_unobservable")
            probes.append(probe)
        for probe in probes[1:]:
            if probe.digest != first.digest:
                return listener.ActivityTrace(True, "pane_content_changed", region_digest=probe.digest)
        return listener.ActivityTrace(False, "working_timer_idle", region_digest=probes[-1].digest)

    def composer_snapshot(self, target: str) -> ComposerSnapshot:
        from . import notify_listener as listener

        try:
            proc = self.capture_pane_runner(
                ["tmux", "capture-pane", "-p", "-J", "-t", target],
                check=True,
                text=True,
                capture_output=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return listener.ComposerSnapshot(False, error=str(exc))
        return listener.composer_snapshot_from_pane_text(proc.stdout)

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
        from . import notify_listener as listener

        if state.updated_at >= self._started_at:
            self._reset_director_startup_hold()
            return None
        if self._director_startup_released_state_ts == state.updated_at:
            return None
        now = self.monotonic()
        if self._director_startup_hold_state_ts != state.updated_at:
            self._director_startup_hold_state_ts = state.updated_at
            self._director_startup_hold_started_at = now
            return listener.ActivityTrace(True, "startup_latch_unestablished")
        hold_started_at = self._director_startup_hold_started_at if self._director_startup_hold_started_at is not None else now
        if now - hold_started_at < self.director_startup_hold_seconds:
            return listener.ActivityTrace(True, "startup_latch_unestablished")
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
        from . import notify_listener as listener

        try:
            table = self.process_table_reader()
        except OSError:
            return listener.ChildWorkSample(False)
        if not table:
            return listener.ChildWorkSample(False)
        return listener.descendant_work_sample(pane_pid, table)

    def _survivors_advanced(self, first: ChildWorkSample, second: ChildWorkSample) -> bool:
        """Whether the descendants present in both samples used any real CPU.

        Measured over survivors rather than over the tree's total, because a
        total is not monotonic: a descendant that exits between the two samples
        takes its accumulated ticks with it, and the sum can fall while a
        sibling is working hard. One old child leaving with 100 ticks masked a
        surviving child's 3, and the pane was called idle while that child was
        making progress (SYRD-101 review).

        A survivor's own CPU only ever rises, so this cannot be hidden by
        anything else in the tree leaving. Arrivals need no accounting here:
        a tree that grew is handled as movement in its own right.
        """
        return bool(self._survivors_that_advanced(first, second))

    def _survivors_that_advanced(
        self, first: ChildWorkSample, second: ChildWorkSample
    ) -> frozenset[int]:
        """WHICH survivors used the CPU, not merely whether any did.

        A sum cannot say whose it was, and that is the whole of SYRD-212: a poll
        loop left behind by a finished turn contributed the same few ticks a
        working child would, so "something advanced" settled the verdict before
        the prior-turn classification could be applied. Attributing the ticks
        lets the two be told apart without naming either process.
        """
        before = dict(first.cpu_by_pid)
        advanced: set[int] = set()
        for pid, cpu in second.cpu_by_pid:
            if pid in before and cpu - before[pid] > self.child_work_cpu_ticks:
                advanced.add(pid)
        if advanced:
            return frozenset(advanced)
        # The tree as a whole may still have moved by less than any single
        # process's allowance -- several helpers each ticking once. Kept as it
        # was, attributed to whoever contributed, so no existing verdict
        # changes for a tree that has no prior-turn children in it.
        used = sum(
            max(0, cpu - before[pid])
            for pid, cpu in second.cpu_by_pid
            if pid in before
        )
        if used > self.child_work_cpu_ticks:
            return frozenset(
                pid for pid, cpu in second.cpu_by_pid
                if pid in before and cpu > before[pid]
            )
        return frozenset()

    def _prior_turn_children(
        self, sample: ChildWorkSample, trusted_idle_at: float | None
    ) -> frozenset[int]:
        """Descendants that were already running when the turn declared itself done.

        A start time earlier than the runtime's own trusted idle hook is
        lifecycle evidence, not a timeout: it says this process belongs to a turn
        that has since ended. A start time that could not be read counts as this
        turn's, because dismissing what cannot be seen is the wrong direction to
        guess in (SYRD-101).
        """
        if trusted_idle_at is None or trusted_idle_at <= 0:
            return frozenset()
        return frozenset(
            pid
            for pid, started in sample.starts
            if started > 0 and started <= trusted_idle_at
        )

    def child_work_trace(
        self, target: str, *, trusted_idle_at: float | None = None
    ) -> ActivityTrace | None:
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
        from . import notify_listener as listener

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
        advancing = self._survivors_that_advanced(first, second)
        advanced = bool(advancing)
        arrived = (
            (remembered.arrived if remembered is not None else frozenset()) | appeared
        ) & second.pids
        self._child_work_memory_by_target[target] = listener.ChildWorkMemory(second.pids, arrived)
        # Everything under the pane that the current turn did not start. A turn
        # that ends can leave shells behind -- a poll loop waiting on a file that
        # will never say what it is waiting for is the case that produced this
        # ticket -- and they stay detached and keep sleeping indefinitely. Held
        # against them, the detached signal below overrides a newer trusted idle
        # hook forever and the pane never receives another notification, which is
        # exactly what happened: 23 claims of one transition over 90 minutes
        # against a pane that had been idle at its composer the whole time
        # (SYRD-101).
        prior_turn = self._prior_turn_children(second, trusted_idle_at)
        # The descendants that would hold delivery on their own: the detached
        # ones, and the ones this gate watched arrive. The runtime itself is a
        # descendant of the pane shell and holds nothing, so it is not part of
        # this and an ordinary idle pane keeps reporting `hook_idle`.
        holding = second.detached | arrived
        if advanced:
            # Progress settles it before anything else does. A build or a sweep
            # the previous turn started and that is still running is real work,
            # whatever its age, and this is what keeps the ticket's "do not
            # interrupt a legitimate long task" requirement true without a clock.
            #
            # Which turn started it decides only what the trace is CALLED, not
            # whether it holds. Naming it is the point: a hold that comes from a
            # finished turn's descendant is the one that can starve a later
            # handoff, and until SYRD-212 it was indistinguishable in the record
            # from this turn's own work. The bound that stops it starving
            # anything is the listener's, not this probe's -- a probe cannot
            # know how long a build should be allowed to run.
            if advancing and advancing <= prior_turn:
                return listener.ActivityTrace(True, listener.PRIOR_TURN_CHILD_WORK)
            return listener.ActivityTrace(True, "pane_child_work")
        if holding and not appeared and holding <= prior_turn:
            # Everything that would have held this pane predates the turn's own
            # idle hook, and none of it is progressing. Said out loud, with a
            # reason of its own, so it can never be read as this turn's work.
            return listener.ActivityTrace(False, listener.STALE_PRIOR_TURN_CHILD_WORK)
        if second.detached:
            # A descendant in a session of its own. A tool that starts a shell
            # gives it a new session; the runtime and the helpers it keeps stay
            # in the pane's. This needs no history, so it is the signal that
            # survives a listener restart in the middle of a turn's work.
            return listener.ActivityTrace(True, "pane_child_work")
        if appeared:
            return listener.ActivityTrace(True, "pane_child_work")
        if arrived:
            # A child this turn started, sitting on a fetch, a lock or a long
            # build, using no CPU and printing nothing. It is work until it
            # leaves: a wait has no length at which it stops being a wait.
            return listener.ActivityTrace(True, "pane_child_work")
        return None

    def _trusted_idle_source_trace(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        # The runtime's own statement that its turn ended, and when. That is the
        # line between work this turn started and what an earlier one left
        # behind; without it the probe has no way to tell them apart (SYRD-101).
        from . import notify_listener as listener

        trusted_idle_at = (
            state.updated_at
            if state.state == "idle" and state.source in listener.TRUSTED_IDLE_SOURCES
            else None
        )
        return self.child_work_trace(target, trusted_idle_at=trusted_idle_at)

    def _idle_cursor_trace(self, target: str, state: PaneHookState, *, check_trusted_working: bool = False) -> ActivityTrace:
        from . import notify_listener as listener

        cursor_composing = self._target_cursor_state(target)
        if target == self.director_target:
            startup_trace = self._director_startup_hold_trace(state)
            if startup_trace is not None and cursor_composing is not True:
                return startup_trace
        if cursor_composing is None:
            return listener.ActivityTrace(True, "cursor_state_unavailable")
        if cursor_composing:
            return listener.ActivityTrace(True, "human_composing")
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
        return listener.ActivityTrace(False, "hook_idle")

    def _untrusted_idle_source_trace(self, target: str, state: PaneHookState) -> ActivityTrace | None:
        from . import notify_listener as listener

        if state.state != "idle":
            return None
        if state.source.startswith("listener."):
            return None
        source_runtime = self._runtime_for_source(state.source)
        expected_runtime = self._expected_runtime_for_target(target)
        if source_runtime and expected_runtime and source_runtime != expected_runtime:
            probe_trace = self._working_timer_idle_probe_trace(target)
            if probe_trace.busy:
                return listener.ActivityTrace(True, f"foreign_runtime_{probe_trace.reason}")
            return listener.ActivityTrace(False, "foreign_runtime_working_timer_idle")
        if state.source in listener.TRUSTED_IDLE_SOURCES:
            if (
                state.updated_at >= listener.MIN_RECOVERABLE_HOOK_EPOCH_SECONDS
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
        from . import notify_listener as listener

        if state.state != "busy" or not state.source.startswith("codex."):
            return None
        if self.stale_codex_busy_hook_seconds <= 0:
            return None
        if state.updated_at < listener.MIN_RECOVERABLE_HOOK_EPOCH_SECONDS:
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
            return listener.ActivityTrace(True, "cursor_state_unavailable")
        if cursor_composing:
            return listener.ActivityTrace(True, "human_composing")
        try:
            self.state_store.write(target, "idle", source="listener.stale_codex_busy_recovery", now=now)
        except OSError as exc:
            listener.LOGGER.warning("Failed to recover stale Codex busy hook state for %s: %s", target, exc)
        return listener.ActivityTrace(False, "stale_codex_busy_recovered")

    def anti_clobber_trace(self, target: str) -> ActivityTrace:
        return self._anti_clobber_trace(target, check_trusted_working=False)

    def pre_send_anti_clobber_trace(self, target: str) -> ActivityTrace:
        return self._anti_clobber_trace(target, check_trusted_working=True)

    def _anti_clobber_trace(self, target: str, *, check_trusted_working: bool) -> ActivityTrace:
        from . import notify_listener as listener

        state = self.state_store.read(target)
        if state is None:
            if target == self.director_target:
                self._reset_director_startup_hold()
            return listener.ActivityTrace(True, "no_hook_state")
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
                return listener.ActivityTrace(True, "cursor_state_unavailable")
            if cursor_composing:
                return listener.ActivityTrace(True, "human_composing")
            return listener.ActivityTrace(False, "hook_idle")
        if previous is not None and previous[0] != "idle" and target == self.director_target:
            self._reset_director_startup_hold()
        return self._idle_cursor_trace(target, state, check_trusted_working=check_trusted_working)

    def anti_clobber_busy(self, target: str) -> bool:
        return self._record_trace(target, self.anti_clobber_trace(target))

    def pre_send_anti_clobber_busy(self, target: str) -> bool:
        return self._record_trace(target, self.pre_send_anti_clobber_trace(target))

    def _full_activity_trace(self, target: str, *, check_trusted_working: bool) -> ActivityTrace:
        from . import notify_listener as listener

        state = self.state_store.read(target)
        if state is None:
            if target == self.director_target:
                self._reset_director_startup_hold()
            return listener.ActivityTrace(True, "no_hook_state")
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
            return listener.ActivityTrace(True, reason)
        if previous is not None and previous[0] != "idle" and target == self.director_target:
            self._reset_director_startup_hold()
        return self._idle_cursor_trace(target, state, check_trusted_working=check_trusted_working)

    def permission_prompt_waits(self) -> dict[str, str]:
        """Roles whose pane is stopped on a permission prompt, and since when.

        On the gate rather than the listener because this is a question about
        pane state, which is what the gate holds. Read straight from the hook
        state: the hook writes `blocked` with its own source when Claude raises
        a prompt nothing answered (SYRD-234).
        """
        from . import notify_listener as listener

        waiting: dict[str, str] = {}
        for role, target in sorted(self.role_targets.items()):
            state = self.state_store.read(target)
            if state is None or state.state != "blocked":
                continue
            if state.source not in listener.PERMISSION_PROMPT_BLOCK_SOURCES:
                continue
            waiting[role] = datetime.fromtimestamp(state.updated_at, tz=timezone.utc).isoformat()
        return waiting

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
