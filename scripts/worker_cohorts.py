"""Provider cohorts of a worker pool: their status, and drain-safe convergence (SYRD-628).

A cohort is the pool's claimants of one runtime, declared in the workflow's
`scheduling.cohorts` with a target of running workers, a ceiling on the
tickets they hold (the board enforces that on every claim), and the model and
effort its workers are pinned to.

Convergence brings each cohort's running count to its target by stopping and
starting declared workers -- never by switching a worker's runtime, model or
effort, so every exact choice is kept and no conversation is lost to a switch.
Each pass makes at most one stop and one start, and only:

* stops a worker that is running, idle by the pane gate (which fails closed),
  holding no ticket and with no pinned rework waiting for it -- and only after
  the board has marked it draining, so no claim can land on it meanwhile;
* starts a stopped worker whose own model and effort match the cohort's pins,
  that start would not refuse, in a cohort that is not exhausted.

Everything else is deferred and said so: busy and reserved workers drain
naturally. Every pass is computed from what is observed, so a launcher
restart, a lost provider process, a stale registration or a worker that went
idle since are all simply the next pass's facts. Every start, stop and
deferral is recorded on the board (`cohort_events`) before and after it acts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Worker:
    role: str
    runtime: str
    model: str = ""
    effort: str = ""
    order: int = 0
    running: bool = False
    #: The pane gate's answer; it fails closed, so no record reads as busy.
    busy: bool = True
    stopped_at: str = ""
    held: str = ""
    held_known: bool = False
    pinned: tuple[str, ...] = ()
    #: What `worker-pool start` would refuse it for.
    blockers: tuple[str, ...] = ()

    @property
    def working(self) -> bool:
        return self.running and (self.busy or bool(self.held) or bool(self.stopped_at) or not self.held_known)

    def why_kept(self) -> str:
        """Why a running worker is not stopped, in the order it matters."""
        if self.held:
            return f"holding {self.held}; it drains first"
        if not self.held_known:
            return "the board did not report its serial slot"
        if self.stopped_at:
            return f"stopped on a prompt: {self.stopped_at}"
        if self.busy:
            return "a turn is in progress (or its pane state is unknown)"
        if self.pinned:
            return f"rework is pinned to it: {', '.join(self.pinned)}"
        return ""


@dataclass(frozen=True)
class Cohort:
    runtime: str
    target: int | None = None
    ceiling: int | None = None
    effective_ceiling: int | None = None
    model: str = ""
    effort: str = ""
    quota: tuple[Mapping[str, Any], ...] = ()
    telemetry: str = "static"
    holding: int = 0
    draining: tuple[str, ...] = ()
    workers: tuple[Worker, ...] = ()

    def off_pin(self, worker: Worker) -> str:
        wrong = [f"{key} {getattr(worker, key) or 'unset'}, the cohort wants {want}"
                 for key, want in (("model", self.model), ("effort", self.effort))
                 if want and getattr(worker, key) != want]
        return "; ".join(wrong)

    @property
    def live(self) -> list[Worker]:
        return [w for w in self.workers if w.running]

    @property
    def busy(self) -> int:
        return sum(1 for w in self.live if w.working)

    @property
    def idle(self) -> int:
        return len(self.live) - self.busy

    @property
    def deficit(self) -> int:
        return max(self.target - len(self.live), 0) if self.target is not None else 0

    @property
    def excess(self) -> int:
        return max(len(self.live) - self.target, 0) if self.target is not None else 0

    @property
    def exhausted(self) -> bool:
        return self.effective_ceiling == 0


@dataclass(frozen=True)
class Move:
    kind: str  # "stop", "start" or "deferred"
    runtime: str
    role: str
    reason: str
    #: A start that fits the pool only because of this pass's stop names that
    #: stop's worker, and waits unless the stop actually happened (SYRD-628).
    after_stop: str = ""

    def describe(self, *, would: bool = False) -> str:
        verb = {"stop": "stop ", "start": "start", "deferred": "defer"}[self.kind]
        who = f"{self.role} ({self.runtime})" if self.role else self.runtime
        return f"{'would ' if would and self.kind != 'deferred' else ''}{verb} {who}: {self.reason}"


def stoppable(worker: Worker) -> bool:
    return worker.running and not worker.working and not worker.pinned


def plan(cohorts: Sequence[Cohort]) -> list[Move]:
    """At most one stop and one start, each justified by its own cohort's target; the rest deferred.

    A start also has to fit the pool: the running total stays within the sum
    of the targets, counting this pass's stop. So a pool that is the right size
    but the wrong mix converges by replacement, one drained worker at a time,
    and never runs more sessions than it was sized for.
    """
    stop: Move | None = None
    start: Move | None = None
    deferred: list[Move] = []
    for cohort in sorted(cohorts, key=lambda c: (-c.excess, c.runtime)):
        if not cohort.excess:
            continue
        idle = sorted((w for w in cohort.live if stoppable(w)), key=lambda w: -w.order)
        for worker in cohort.live:
            if not stoppable(worker):
                deferred.append(Move("deferred", cohort.runtime, worker.role, worker.why_kept()))
        if stop is None and idle:
            stop = Move("stop", cohort.runtime, idle[0].role,
                        f"{cohort.runtime} runs {len(cohort.live)}, its target is {cohort.target}; "
                        f"{idle[0].role} is idle and holds nothing")
    size = sum(c.target if c.target is not None else len(c.live) for c in cohorts)
    running = sum(len(c.live) for c in cohorts) - (1 if stop is not None else 0)
    for cohort in sorted(cohorts, key=lambda c: (-c.deficit, c.runtime)):
        if not cohort.deficit:
            continue
        if cohort.exhausted:
            deferred.append(Move("deferred", cohort.runtime, "",
                                 f"{cohort.runtime} is {cohort.deficit} under target but exhausted (ceiling 0); "
                                 "nothing is started until its quota recovers"))
            continue
        stopped = [w for w in cohort.workers if not w.running]
        candidates = sorted((w for w in stopped if not w.blockers and not cohort.off_pin(w)),
                            key=lambda w: (not w.pinned, w.order))
        for worker in stopped:
            if cohort.off_pin(worker):
                deferred.append(Move("deferred", cohort.runtime, worker.role, f"off-pin: {cohort.off_pin(worker)}"))
            elif worker.blockers:
                deferred.append(Move("deferred", cohort.runtime, worker.role,
                                     f"start would refuse it: {worker.blockers[0]}"))
        if not candidates:
            deferred.append(Move("deferred", cohort.runtime, "",
                                 f"{cohort.runtime} is {cohort.deficit} under target and no stopped worker on its "
                                 "pins can start"))
        elif start is None and running >= size:
            deferred.append(Move("deferred", cohort.runtime, "",
                                 f"{cohort.runtime} is {cohort.deficit} under target, but the pool runs {running} of "
                                 f"the {size} its targets add up to; a start waits for a drained worker to stop"))
        elif start is None:
            pick = candidates[0]
            why = f"; rework is pinned to it: {', '.join(pick.pinned)}" if pick.pinned else ""
            # Without the stop the pool would already be at its size: the start replaces it.
            replaces = stop.role if stop is not None and running + 1 >= size else ""
            start = Move("start", cohort.runtime, pick.role,
                         f"{cohort.runtime} runs {len(cohort.live)}, its target is {cohort.target}{why}"
                         + (f"; it replaces {replaces}" if replaces else ""), after_stop=replaces)
        else:
            deferred.append(Move("deferred", cohort.runtime, "",
                                 f"{cohort.runtime} is {cohort.deficit} under target; one start per pass, so it waits "
                                 "for the next"))
    return [move for move in (stop, start) if move is not None] + deferred


def format_status(project: str, pool_name: str, cohorts: Sequence[Cohort], moves: Sequence[Move], *,
                  uncohorted: Sequence[str] = (), would: bool = True) -> list[str]:
    telemetry = "a provider signal" if any(c.telemetry == "signal" for c in cohorts) else "none; static limits only"
    lines = [f"switchyard: {pool_name} cohorts on {project} (usage telemetry: {telemetry})"]
    for c in cohorts:
        limit = "unlimited" if c.effective_ceiling is None else c.effective_ceiling
        pins = " ".join(f"{key}={value}" for key, value in (("model", c.model), ("effort", c.effort)) if value)
        lines.append(f"  {c.runtime}: target {'-' if c.target is None else c.target}, ceiling "
                     f"{'-' if c.ceiling is None else c.ceiling} (now {limit}), holding {c.holding}"
                     + (f"; pinned {pins}" if pins else ""))
        gap = f"deficit {c.deficit}" if c.deficit else f"excess {c.excess}" if c.excess else "on target"
        lines.append(f"    live {len(c.live)}, busy {c.busy}, idle {c.idle}; {gap}")
        for q in c.quota:
            lines.append(f"    quota {q.get('source')}: ceiling {q.get('ceiling')} -- {q.get('reason')}")
        for w in c.workers:
            if w.running:
                state = "running, " + ("busy (" + w.why_kept() + ")" if w.working else "idle")
            else:
                state = "stopped" + (f", off-pin: {c.off_pin(w)}" if c.off_pin(w) else
                                     f", blocked: {w.blockers[0]}" if w.blockers else ", startable")
            draining = ", draining" if w.role in c.draining else ""
            pinned = f", rework pinned: {', '.join(w.pinned)}" if w.pinned and not w.running else ""
            lines.append(f"    {w.role}: {state}{draining}{pinned}")
    for role in uncohorted:
        lines.append(f"  {role}: in the pool, but its runtime has no cohort; nothing limits or moves it")
    lines.append("plan (at most one stop and one start per pass):" if moves else "plan: nothing to do; every cohort is on target")
    lines.extend(f"  {move.describe(would=would)}" for move in moves)
    return lines


def gather(document: Mapping[str, Any], *, board_status: Mapping[str, Any] | None,
           readiness: Iterable[Any], models: Mapping[str, tuple[str, str]], orders: Mapping[str, int],
           busy_of: Callable[[str], bool], pinned: Mapping[str, Sequence[str]]) -> tuple[list[Cohort], list[str]]:
    """The cohorts as observed now. `readiness` is worker_pool.WorkerReadiness for the pool's members."""
    from scripts.ticket_board import scheduling_cohorts

    reported = {c.get("runtime"): c for c in (board_status or {}).get("cohorts") or []}
    by_runtime: dict[str, list[Worker]] = {}
    uncohorted: list[str] = []
    for state in readiness:
        cohort = scheduling_cohorts.cohort_of(document, state.role)
        if cohort is None:
            uncohorted.append(state.role)
            continue
        model, effort = models.get(state.role, ("", ""))
        running = bool(state.session)
        by_runtime.setdefault(cohort["runtime"], []).append(Worker(
            role=state.role, runtime=cohort["runtime"], model=model, effort=effort, order=orders.get(state.role, 0),
            running=running, busy=busy_of(state.role) if running else False, stopped_at=state.stopped_at,
            held=state.reserved_by, held_known=state.reservation_known, pinned=tuple(pinned.get(state.role, ())),
            blockers=tuple(state.blockers)))
    cohorts = []
    for declared in scheduling_cohorts.cohorts(document):
        runtime = declared["runtime"]
        board = reported.get(runtime) or {}
        cohorts.append(Cohort(
            runtime=runtime, target=declared.get("target"), ceiling=declared.get("ceiling"),
            # A board that did not answer leaves the declared ceiling: honest, if not the whole story.
            effective_ceiling=board.get("effective_ceiling", declared.get("ceiling")),
            model=declared.get("model", ""), effort=declared.get("effort", ""),
            quota=tuple(board.get("quota") or ()), telemetry=board.get("telemetry", "static"),
            holding=len(board.get("holding") or ()) or sum(1 for w in by_runtime.get(runtime, []) if w.held),
            draining=tuple(board.get("draining") or ()),
            workers=tuple(sorted(by_runtime.get(runtime, []), key=lambda w: w.order))))
    return cohorts, uncohorted


@dataclass
class Outcome:
    acted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def execute(moves: Sequence[Move], *, writer: Any, start: Callable[[str], Any], stop: Callable[[str], Any],
            still_idle: Callable[[str], bool], source: str, record_deferrals: bool = True,
            print_func: Callable[[str], None] = print) -> Outcome:
    """Carry out one pass's moves, writing each down before and after; nothing acts unrecorded.

    `start` and `stop` return worker_pool.WorkerAction; `still_idle` asks the
    pane gate again, after the drain, whether a stop would cut into a turn.
    """
    outcome = Outcome()
    stopped: set[str] = set()
    for move in moves:
        if move.kind == "start" and move.after_stop and move.after_stop not in stopped:
            # The capacity it was to fill was never freed: starting anyway would grow the pool.
            reason = f"not started: it was to replace {move.after_stop}, which was not stopped"
            writer.record_cohort_change(move.runtime, move.role, kind="deferred", source=source, reason=reason)
            print_func(f"switchyard: kept {move.role} stopped: {reason}")
            continue
        if move.kind == "deferred":
            if record_deferrals and move.role:
                writer.record_cohort_change(move.runtime, move.role, kind="deferred", source=source, reason=move.reason)
            print_func(f"switchyard: {move.describe()}")
            continue
        writer.record_cohort_change(move.runtime, move.role, kind=move.kind, source=source, reason=move.reason,
                                    detail={"phase": "intended"})
        if move.kind == "stop":
            drained = writer.begin_cohort_drain(move.role, reason=move.reason)
            found = (drained or {}).get("result") or {}
            try:
                took = (f"it took {found['held']}" if found.get("held") else
                        f"rework was pinned to it: {', '.join(found['pinned'])}" if found.get("pinned") else
                        "" if still_idle(move.role) else "a turn began")
                if took:
                    writer.record_cohort_change(move.runtime, move.role, kind="deferred", source=source,
                                                reason=f"not stopped: {took} before the drain")
                    print_func(f"switchyard: kept {move.role}: {took} before the drain")
                    continue
                result = stop(move.role)
            finally:
                writer.end_cohort_drain(move.role)
        else:
            result = start(move.role)
        done = bool(getattr(result, "succeeded", False))
        writer.record_cohort_change(move.runtime, move.role, kind=move.kind if done else f"{move.kind}_failed",
                                    source=source, reason=move.reason,
                                    detail={"phase": "done" if done else "failed", "said": result.describe()})
        (outcome.acted if done else outcome.failed).append(move.role)
        if done and move.kind == "stop":
            stopped.add(move.role)
        print_func(result.describe())
    return outcome


def converge_passes(observe: Callable[[], tuple[list[Cohort], list[str]]], *, act: Callable[[list[Move], bool], Outcome],
                    passes: int | None, interval: float, sleep: Callable[[float], None] = time.sleep,
                    print_func: Callable[[str], None] = print) -> int:
    """Pass after pass (forever when `passes` is None). A deferral is recorded when it changes, not every pass."""
    seen: set[tuple[str, str]] = set()
    failed = 0
    count = 0
    while passes is None or count < passes:
        cohorts, _uncohorted = observe()
        moves = plan(cohorts)
        deferrals = {(m.role, m.reason) for m in moves if m.kind == "deferred"}
        outcome = act(moves, deferrals != seen)
        seen = deferrals
        failed += len(outcome.failed)
        count += 1
        if passes is None or count < passes:
            sleep(interval)
    return 1 if failed else 0


def run_command(config, pool, *, document: Mapping[str, Any] | None, action: str, config_path, dry_run: bool = False,
                watch: bool = False, interval: float = 60.0, passes: int | None = None,
                read_readiness: Callable[[], Sequence[Any]], read_snapshot: Callable[[], Mapping[str, Any] | None],
                read_status: Callable[[], Mapping[str, Any] | None], busy_of: Callable[[str], bool] | None = None,
                writer: Any = None, runner=None, sleep: Callable[[float], None] = time.sleep,
                start: Callable[[str], Any] | None = None, stop: Callable[[str], Any] | None = None,
                print_func: Callable[[str], None] = print) -> int:
    """`switchyard worker-pool <project> cohorts` and `converge [--dry-run] [--watch]`."""
    from scripts import worker_pool
    from scripts.ticket_board import scheduling_cohorts

    document = dict(document or {})
    if not scheduling_cohorts.cohorts(document):
        print_func(f"switchyard: {config.project}'s workflow declares no scheduling cohorts; there is nothing to {action}. "
                   "Declare them under scheduling.cohorts, e.g. "
                   '[{"runtime": "claude", "target": 4, "ceiling": 4}, {"runtime": "codex", "target": 4, "ceiling": 4}].')
        return 1
    busy_of = busy_of or _pane_busy(config, runner=runner)
    ready_stage = str((document.get("scheduling") or {}).get("ready_stage") or "")
    roles = {role.role: role for role in config.roles}

    def observe() -> tuple[list[Cohort], list[str]]:
        snapshot = read_snapshot() or {}
        pinned: dict[str, list[str]] = {}
        for ticket in snapshot.get("tickets") or []:
            owner = str(ticket.get("queued_for_assignee") or "").strip()
            if owner and ticket.get("state") == ready_stage:
                pinned.setdefault(owner, []).append(str(ticket.get("id")))
        readiness = list(read_readiness())
        return gather(document, board_status=read_status(), readiness=readiness,
                      models={name: (getattr(role, "model", "") or "", getattr(role, "effort", "") or "")
                              for name, role in roles.items()},
                      orders={state.role: worker_pool.member_index(pool.name, state.role) or 0 for state in readiness},
                      busy_of=busy_of, pinned=pinned)

    if action == "cohorts" or dry_run:
        cohorts, uncohorted = observe()
        for line in format_status(config.project, pool.name, cohorts, plan(cohorts), uncohorted=uncohorted,
                                  would=True):
            print_func(line)
        if dry_run:
            print_func("switchyard: dry run -- nothing was started, stopped or recorded.")
        return 0

    from scripts.ticket_board.cohort_client import CohortWriter

    writer = writer or CohortWriter(board_url=config.board_url, caller_role="director")
    source = "auto" if watch else "operator"

    last: dict[str, Any] = {}

    def observed() -> tuple[list[Cohort], list[str]]:
        last["view"] = observe()
        return last["view"]

    def act(moves: list[Move], record_deferrals: bool) -> Outcome:
        cohorts, uncohorted = last["view"]
        for line in format_status(config.project, pool.name, cohorts, moves, uncohorted=uncohorted, would=False):
            print_func(line)
        return execute(
            moves, writer=writer, source=source, record_deferrals=record_deferrals, print_func=print_func,
            stop=stop or (lambda member: worker_pool.stop_worker(config, member, runner=runner)),
            start=start or (lambda member: worker_pool.start_worker(config, pool, member, readiness=read_readiness(),
                                                                    config_path=config_path, runner=runner)),
            still_idle=lambda member: not busy_of(member))

    return converge_passes(observed, act=act, passes=passes if passes is not None else (None if watch else 1),
                           interval=interval, sleep=sleep, print_func=print_func)


def _pane_busy(config, *, runner=None) -> Callable[[str], bool]:
    """The launcher's own activity gate for each worker's pane; it fails closed (no record reads as busy)."""
    from scripts import team_launcher as launcher
    from scripts.ticket_board.notify_listener import PaneActivityGate, PaneHookStateStore

    def busy(member: str) -> bool:
        role = launcher._role_by_name(config, member)
        state_dir = launcher.default_pane_state_dir_for_user(launcher.role_run_as_user(config, role),
                                                             project=config.project)
        return PaneActivityGate(state_store=PaneHookStateStore(state_dir)).is_busy(role.target)

    return busy


def operator_change(config, document: Mapping[str, Any] | None, member: str, action: str, *, writer: Any = None,
                    print_func: Callable[[str], None] = print) -> Callable[..., None] | None:
    """How a hand-run `worker-pool start|stop|restart` of a cohort member is recorded; None for anyone else.

    An operator's start or stop changes a cohort as surely as convergence's,
    so it is written down the same way, as the operator's. A record the board
    refuses is said, not hidden; the session move itself is not held back by
    it, since start and stop are also how an operator recovers a worker.
    """
    from scripts.ticket_board import scheduling_cohorts

    cohort = scheduling_cohorts.cohort_of(document or {}, member)
    if cohort is None or not getattr(config, "board_url", ""):
        return None
    if writer is None:
        from scripts.ticket_board.cohort_client import CohortWriter

        writer = CohortWriter(board_url=config.board_url, caller_role="director")
    reason = f"switchyard worker-pool {action} {member}, run by the operator"

    def record(kind: str, phase: str, said: str = "") -> None:
        try:
            writer.record_cohort_change(cohort["runtime"], member, kind=kind, source="operator", reason=reason,
                                        detail={"phase": phase, **({"said": said} if said else {})})
        except Exception as exc:  # said, never fatal
            print_func(f"switchyard: the board did not record {member}'s {kind}: {exc}")

    return record
