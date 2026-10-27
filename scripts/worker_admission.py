#!/usr/bin/env python3
"""Which idle pull workers run, and starting one when ready work needs it (SYRD-688).

A project launch used to start every detached role, so a tenant declaring
fifteen ephemeral pull workers cold-started fifteen provider CLIs on every
launch, though none held a ticket. Pull pickup only gives work to a worker
whose provider is already running, so eager start was the only way to keep
capacity. Now:

* **At launch** (`launch_admission`) every role starts as before except an
  idle pull claimant -- an active, ephemeral implementer of the board's pull
  policy holding nothing. Those start only for work: a claimant the board
  says holds an active ticket or a continuation recovers it; one a pinned
  ticket waits for starts; ready unpinned work starts as many as it needs,
  less the idle claimants already running; `warm_workers` in the launcher
  configuration keeps that many more idle and ready. The rest are left
  stopped, each with its reason.
* **Later** the tenant's notify listener asks the same plan on each pass
  (`ticket_board/demand_start.py`) and starts what new ready work needs, by
  running this module's `demand-start` in a child process as the project
  account -- the same pane start `worker-pool start` makes. The board gains no
  authority: the listener chooses only among declared claimants, and only
  this command.

Every start is chosen under one lock, `<session dir>/.worker-admission.lock`,
and recorded in `<session dir>/worker-admission.json` until the worker
registers or the start ends, so concurrent passes, or a pass during a launch,
never start one worker twice or more than the work needs. The same record
keeps what must stay respected: a worker an operator stopped, and a start
that failed (not retried for FAILURE_BACKOFF_SECONDS). Provider quota holds
come from the board. A project whose core roles -- every role that is not a
pull claimant -- have no live session is stopped, and demand starts nothing
for it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

STATE_NAME = "worker-admission.json"
LOCK_NAME = ".worker-admission.lock"
LOG_NAME = "worker-admission.log"
#: How long a start is counted as capacity before its worker registers.
START_GRACE_SECONDS = 180
#: How long a worker whose start failed is left stopped before it is tried again.
FAILURE_BACKOFF_SECONDS = 600
#: The most workers one listener pass starts: demand beyond it waits for the next pass.
MAX_STARTS_PER_PASS = 2
#: How often the board is asked before admission gives up deciding, and the pause before each retry.
BOARD_RETRY_DELAYS = (0.5, 1.5)

RECOVER = "active-ticket recovery"
DEMAND = "ready-work demand start"
WARM = "retained warm worker"
RUNNING = "already running"
IDLE = "left stopped: no work needs it"
HELD = "left stopped: its provider is held for quota"
OPERATOR = "left stopped: an operator stopped it"
FAILED = "left stopped: its last start failed"
STARTING = "already starting"
UNDECIDED = "left stopped: admission could not decide"
COHORT = "left stopped: its cohort is at its declared capacity"
OFF_PIN = "left stopped: it is off its cohort's pins"
STARTS = frozenset({RECOVER, DEMAND, WARM, RUNNING})


@dataclass(frozen=True)
class Decision:
    role: str
    kind: str
    detail: str = ""

    @property
    def starts(self) -> bool:
        return self.kind in STARTS

    def line(self) -> str:
        return f"{self.role}: {self.kind}" + (f" ({self.detail})" if self.detail else "")


def _order(role: str) -> tuple[str, int, str]:
    """Pool members in index order (luna-2 before luna-10), then everything else by name."""
    matched = re.fullmatch(r"(.*?)-(\d+)", role)
    return (matched.group(1), int(matched.group(2)), "") if matched else (role, 0, role)


def plan(claimants: Iterable[str], *, ready_work: Iterable[Mapping[str, Any]], holders: Mapping[str, str] = {},
         running: Iterable[str] = (), held: Iterable[str] = (), operator_stopped: Iterable[str] = (),
         failed: Mapping[str, str] = {}, starting: Iterable[str] = (), warm: int = 0,
         busy: Mapping[str, str] = {}, cohorts: Mapping[str, Mapping[str, Any]] = {},
         runtime_of: Mapping[str, str] = {}, off_pin: Mapping[str, str] = {}) -> list[Decision]:
    """Each claimant's decision, in pool order. Pure: every fact is the caller's.

    `holders` maps a claimant holding work to what it holds; `running` are the
    claimants already live, `starting` those whose start is under way. Both are
    capacity, except a running claimant the listener saw `busy` -- working, on
    a prompt, or running background work -- which can take nothing now. Ready
    work counts when `claimable`; a ticket pinned to one claimant
    (`waiting_for`) needs that claimant and no other.

    A provider cohort (SYRD-628: `cohorts` by runtime, with `target`, the
    board's effective `ceiling` and the tickets it is `holding`) bounds what
    starts beyond recovery: never more of its members running than its
    target, never a worker to claim beyond its ceiling, and never one whose
    model or effort is off its pins (`off_pin`).
    """
    claimants = sorted(set(claimants), key=_order)
    running, held, starting = set(running), set(held), set(starting)
    operator_stopped = set(operator_stopped)
    pinned: dict[str, list[str]] = {}
    unpinned = 0
    for ticket in ready_work:
        if not ticket.get("claimable"):
            continue
        author = str(ticket.get("waiting_for") or "")
        if author:
            pinned.setdefault(author, []).append(str(ticket.get("id")))
        else:
            unpinned += 1
    decisions: dict[str, Decision] = {}
    for role in claimants:
        if role in holders and role in running:
            decisions[role] = Decision(role, RUNNING, holders[role])
        elif role in running:
            decisions[role] = Decision(role, RUNNING, f"busy: {busy[role]}" if role in busy else "")
        elif role in starting:
            decisions[role] = Decision(role, STARTING)
        elif role in held:
            decisions[role] = Decision(role, HELD)
        elif role in operator_stopped:
            decisions[role] = Decision(role, OPERATOR)
        elif role in failed:
            decisions[role] = Decision(role, FAILED, failed[role])
        elif role in off_pin:
            decisions[role] = Decision(role, OFF_PIN, off_pin[role])
        elif role in holders:
            decisions[role] = Decision(role, RECOVER, holders[role])
    # What each cohort runs and holds, as starts are chosen.
    load = {runtime: [sum(1 for role in claimants if runtime_of.get(role) == runtime and (
                          role in running or role in starting or decisions.get(role, Decision(role, "")).kind == RECOVER)),
                      int(cohort.get("holding") or 0)] for runtime, cohort in cohorts.items()}

    def full(role: str, claims: bool) -> str:
        runtime = runtime_of.get(role, "")
        cohort = cohorts.get(runtime)
        if cohort is None:
            return ""
        running_now, holding = load[runtime]
        target, ceiling = cohort.get("target"), cohort.get("ceiling")
        if target is not None and running_now >= target:
            return f"{runtime}: {running_now} running, target {target}"
        if claims and ceiling is not None and holding >= ceiling:
            return f"{runtime}: holds {holding} ticket(s), ceiling {ceiling}"
        load[runtime][0] += 1
        load[runtime][1] += 1 if claims else 0
        return ""

    for role in claimants:
        if role in pinned and role not in decisions:
            why = full(role, True)
            decisions[role] = Decision(role, COHORT, why) if why else \
                Decision(role, DEMAND, "rework waiting for it: " + ", ".join(pinned[role]))
    # Idle capacity already there: running or starting claimants that hold nothing, are not busy, and may claim.
    idle = sum(1 for role in claimants if role not in holders and role not in held and role not in pinned
               and role not in busy and (role in running or role in starting))
    demand, extra = max(0, unpinned - idle), max(0, warm - max(0, idle - unpinned))
    for role in claimants:
        if role in decisions:
            continue
        why = full(role, bool(demand)) if (demand or extra) else ""
        if why:
            decisions[role] = Decision(role, COHORT, why)
        elif demand:
            demand -= 1
            decisions[role] = Decision(role, DEMAND, f"{unpinned} ready ticket(s) unclaimed")
        elif extra:
            extra -= 1
            decisions[role] = Decision(role, WARM, f"warm_workers {warm}")
        else:
            decisions[role] = Decision(role, IDLE)
    return [decisions[role] for role in claimants]


# --- the record ------------------------------------------------------------------


def state_paths(session_dir: Path) -> tuple[Path, Path]:
    return Path(session_dir) / STATE_NAME, Path(session_dir) / LOCK_NAME


@contextlib.contextmanager
def locked_state(session_dir: Path):
    """The admission record, held under its lock for the block; written back when the block changed it."""
    state_path, lock_path = state_paths(session_dir)
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state = state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            state = {}
        for key in ("starting", "failed", "operator_stopped"):
            state[key] = dict(state.get(key) or {})
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) != before:
            temporary = state_path.with_name(f".{STATE_NAME}.{os.getpid()}")
            temporary.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, state_path)
    finally:
        os.close(fd)


def current(state: Mapping[str, Any], *, now: float, registered: Iterable[str] = ()) -> tuple[set[str], dict[str, str]]:
    """(starts still under way, failures still backing off), dropping any that no longer count."""
    registered = set(registered)
    starting = {role for role, at in state.get("starting", {}).items()
                if role not in registered and now - float(at) < START_GRACE_SECONDS}
    failed = {role: str(entry.get("reason", "")) for role, entry in state.get("failed", {}).items()
              if isinstance(entry, dict) and now - float(entry.get("at", 0)) < FAILURE_BACKOFF_SECONDS}
    return starting, failed


def record_operator_stop(session_dir: Path, roles: Iterable[str], stopped: bool, *, now: float | None = None) -> None:
    """A worker an operator stopped stays down until an operator starts it again."""
    with locked_state(session_dir) as state:
        for role in roles:
            if stopped:
                state["operator_stopped"][role] = time.time() if now is None else now
            else:
                state["operator_stopped"].pop(role, None)
                state["failed"].pop(role, None)


# --- deciding, as the project account ---------------------------------------------


def warm_workers(config_path: Path) -> int:
    """`warm_workers` in the launcher configuration: idle claimants kept running beyond demand (default 0)."""
    try:
        value = json.loads(Path(config_path).read_text(encoding="utf-8")).get("warm_workers", 0)
    except (OSError, ValueError, AttributeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _board_facts(config: Any, roles: Mapping[str, Any], getter: Callable[[str], Any], *, ask_holders: bool):
    """(the claimants the board's pull policy names among `roles`, its ready work, the held, who holds what, and
    the cohort facts `plan` takes: cohorts by runtime, each claimant's runtime, and who is off its cohort's pins)."""
    from urllib import parse

    from scripts.ticket_board import scheduling_cohorts
    from scripts.ticket_board.workflow_config import pull_claimant_pool

    base = str(config.board_url).rstrip("/")
    answer = getter(f"{base}/api/workflow")  # {"revision": N, "document": the declared workflow or None}
    document = answer.get("document") if isinstance(answer, dict) else None
    pool = set(pull_claimant_pool(document if isinstance(document, dict) else {}))
    claimants = {name: role for name, role in roles.items() if name in pool}
    if not claimants:
        return {}, [], set(), {}, ({}, {}, {})
    queue = (getter(f"{base}/api/reservations") or {}).get("pull_queue") or {}
    holds = (getter(f"{base}/api/provider-holds") or {}).get("holds") or []
    held = {str(hold.get("role")) for hold in holds if isinstance(hold, dict) and hold.get("current")}
    holders = {}
    for name in claimants if ask_holders else ():
        answer = getter(f"{base}/api/context-recovery/{parse.quote(name, safe='')}")
        if not isinstance(answer, dict) or answer.get("action") not in ("resume", "fresh", "refuse", "resume_recorded"):
            raise ValueError(f"/api/context-recovery/{name} answered {answer!r}")
        if answer.get("action") != "fresh" or answer.get("ticket"):
            holders[name] = str(answer.get("reason") or answer.get("ticket") or "holds work")
    declared = scheduling_cohorts.cohorts(document)
    reported = {c.get("runtime"): c for c in ((getter(f"{base}/api/cohorts") or {}).get("cohorts") or [])} \
        if declared else {}
    cohorts = {c["runtime"]: {"target": c.get("target"),
                              "ceiling": (reported.get(c["runtime"]) or {}).get("effective_ceiling", c.get("ceiling")),
                              "holding": len((reported.get(c["runtime"]) or {}).get("holding") or ())}
               for c in declared}
    runtime_of = {r.get("name"): r.get("runtime") for r in document.get("roles") or [] if r.get("name") in claimants}
    off_pin = {}
    for name, role in claimants.items():
        cohort = next((c for c in declared if c.get("runtime") == runtime_of.get(name)), None)
        wrong = [f"{key} {getattr(role, key) or 'unset'}, the cohort wants {cohort[key]}"
                 for key in ("model", "effort") if cohort and cohort.get(key) and getattr(role, key) != cohort[key]]
        if wrong:
            off_pin[name] = "; ".join(wrong)
    return claimants, list(queue.get("ready_work") or []), held, holders, (cohorts, runtime_of, off_pin)


def admit(project: str, config_path: Path, *, mode: str, runner=None, getter: Callable[[str], Any] | None = None,
          now: Callable[[], float] = time.time, busy: Mapping[str, str] = {},
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Decide, under the lock, which detached pull claimants run; in `demand` mode also start them.

    `launch`: every claimant holding work recovers, and the record notes the
    starts the launch is about to make, so a listener pass meanwhile does not
    make them again; it also clears a stop. `demand`: the listener asked
    because ready work outruns the claimants it saw able to take it (`busy`
    names the running ones that cannot), or because a claimant holds work
    and is not running. At most MAX_STARTS_PER_PASS start, recoveries first,
    each under the lock and only if the project is still up (`_start`).

    A board that cannot answer after BOARD_RETRY_DELAYS leaves every detached
    ephemeral role stopped (`UNDECIDED`), never started blind: held work is
    recovered by the listener's next ask, or by the next launch.
    """
    import subprocess

    from scripts import team_launcher as launcher
    from scripts.role_context_recovery import board_get

    runner, getter = runner or subprocess.run, getter or board_get
    config = launcher.load_project_config(project, Path(config_path))
    roles = {role.role: role for role in config.roles if role.detached and role.ephemeral}
    if not roles or not str(config.board_url or "").strip():
        return {"decisions": [], "reason": ""}
    for delay in (*BOARD_RETRY_DELAYS, None):
        try:
            claimants, ready, held, holders, (cohorts, runtime_of, off_pin) = _board_facts(
                config, roles, getter, ask_holders=True)
            break
        except Exception as exc:  # noqa: BLE001 - asked again, then said plainly
            why = f"the board could not say which pull workers have work ({type(exc).__name__}: {exc})"
            if delay is None:
                return {"reason": why, "decisions": [{"role": name, "kind": UNDECIDED, "detail": why}
                                                     for name in sorted(roles, key=_order)] if mode == "launch" else []}
            sleep(delay)

    def is_live(role: Any) -> bool:
        return launcher.role_process_runner_for(config, role, runner=runner)(
            launcher.tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    core = [role for role in config.roles if role.role not in claimants]

    def down() -> str:
        """Why the project is not up for a demand start, or ""; asked again under the lock before each start."""
        if state.get("stopped"):
            return "the project was stopped"
        if core and not any(is_live(role) for role in core):
            return "the project is stopped: none of its core roles has a live session"
        return ""

    at = now()
    with locked_state(config.session_dir) as state:
        # Under the lock: a start another ask finished while this one waited is live now, not a stale stopped.
        live = {name for name, role in claimants.items() if is_live(role)}
        if mode == "launch":
            state.pop("stopped", None)
        elif down():
            return {"decisions": [], "reason": down()}
        starting, failed = current(state, now=at, registered=live)
        state["starting"] = {role: state["starting"][role] for role in starting}
        decisions = plan(claimants, ready_work=ready, holders=holders, running=live, held=held,
                         operator_stopped=state["operator_stopped"], failed=failed, starting=starting,
                         warm=warm_workers(config_path), busy={r: b for r, b in busy.items() if r in live},
                         cohorts=cohorts, runtime_of=runtime_of, off_pin=off_pin)
        chosen = [d for d in decisions if d.kind in (RECOVER, DEMAND, WARM) and d.role not in live]
        if mode == "demand":
            chosen = sorted(chosen, key=lambda d: d.kind != RECOVER)[:MAX_STARTS_PER_PASS]
        for decision in chosen:
            state["starting"][decision.role] = at
    started = {}
    for decision in chosen if mode == "demand" else ():
        # Under the lock, the project asked again: a stop that came while this ask waited wins (Audit, SYRD-688).
        with locked_state(config.session_dir) as state:
            why = down() or ("an operator stopped it" if decision.role in state["operator_stopped"] else "") or (
                "it is already running" if is_live(claimants[decision.role]) else "")
            if why:
                state["starting"].pop(decision.role, None)
                started[decision.role] = f"not started: {why}"
                continue
            started[decision.role] = _start(config, config_path, claimants[decision.role], state, runner=runner,
                                             now=now, down=down)
    return {"decisions": [{"role": d.role, "kind": d.kind, "detail": d.detail} for d in decisions],
            "started": started, "reason": ""}


def _start(config: Any, config_path: Path, role: Any, state: dict[str, Any], *, runner, now: Callable[[], float],
           down: Callable[[], str] = lambda: "") -> str:
    """Start one claimant as `worker-pool start` would, inside the caller's lock; record how it went ("" when it runs).

    Holding the lock through the start is what makes `stop` safe: its record
    waits for a start already under way, and its sweep then ends that worker.
    A project found down once the worker runs -- a stop that could not record
    itself swept meanwhile -- ends the worker again here.
    """
    from scripts import role_pane_entry, team_launcher as launcher
    from scripts.worker_pool import observe_worker, stop_worker

    pane_user = launcher.role_run_as_user(config, role)
    args = launcher.pane_command_args(
        config.project, role, config_path=Path(config_path), mode="attach-or-start",
        script_path=config.pane_launcher or Path(launcher.__file__).with_name(launcher.TEAM_LAUNCHER_NAME),
        pane_state_dir=launcher.default_pane_state_dir_for_user(pane_user, project=config.project),
        skip_launcher_check=True, no_attach=True, run_as_user=pane_user)
    result = runner(args, env=role_pane_entry.without_ambient_pane_session())
    after, seen = observe_worker(config, role.role, runner=runner, watch=True)
    reason = "" if result.returncode == 0 and after == "running" else \
        f"the pane command exited {result.returncode}, and {seen}"
    state["starting"].pop(role.role, None)
    if not reason and (why := down()):
        ended = stop_worker(config, role.role, runner=runner)
        return f"not started: {why} while it started; {ended.action}: {ended.detail}"
    if reason:
        state["failed"][role.role] = {"at": now(), "reason": reason}
    else:
        state["failed"].pop(role.role, None)
    return reason


def for_launch(config: Any, *, config_path: Path, mode: str, runner: Callable[..., Any],
               print_func: Callable[[str], None]) -> dict[str, Decision]:
    """The launch's decision for each detached pull claimant, each said plainly.

    Admission that cannot run leaves every detached ephemeral role stopped and
    says how to recover: never a cold start of the whole pool (Audit, SYRD-688).
    """
    if mode not in ("start", "attach-or-start") or not str(getattr(config, "board_url", "") or "").strip() \
            or not any(getattr(role, "detached", False) and getattr(role, "ephemeral", False)
                       for role in getattr(config, "roles", ())):
        return {}
    try:
        result = runner(owner_command(config, config_path, "admit", "--mode", "launch"), capture_output=True, text=True)
        answer = json.loads(str(result.stdout or "").strip().splitlines()[-1])
        reason = str(answer.get("reason") or "")
        decisions = {d["role"]: Decision(d["role"], d["kind"], d.get("detail", "")) for d in answer["decisions"]}
    except Exception as exc:  # noqa: BLE001 - bounded: nothing of the pool is started blind
        reason = f"admission could not run ({type(exc).__name__}: {exc})"
        decisions = {role.role: Decision(role.role, UNDECIDED, reason) for role in config.roles
                     if getattr(role, "detached", False) and getattr(role, "ephemeral", False)}
    if reason:
        print_func(f"switchyard: {reason}. Every detached ephemeral worker is left stopped rather than all started; "
                   f"the listener recovers held work and starts what ready work needs once the board answers, "
                   f"or run `switchyard {config.project}` again.")
    for decision in decisions.values():
        if decision.kind != UNDECIDED:
            print_func(f"switchyard: {decision.line()}")
    return decisions


def record(project: str, config_path: Path, *, operator_stopped: Iterable[str] = (),
           operator_started: Iterable[str] = ()) -> None:
    """What `worker-pool stop/start` and `converge` leave for admission to respect, as the project account."""
    from scripts import team_launcher as launcher

    session_dir = launcher.load_project_config(project, Path(config_path)).session_dir
    if operator_stopped:
        record_operator_stop(session_dir, operator_stopped, True)
    if operator_started:
        record_operator_stop(session_dir, operator_started, False)


def mark(config: Any, config_path: Path | None, *arguments: str, runner: Callable[..., Any],
         print_func: Callable[[str], None] = print) -> None:
    """Record, as the project account, what admission must respect; only where a pull worker could be started."""
    if config_path is None or not str(getattr(config, "board_url", "") or "").strip() \
            or not any(getattr(role, "detached", False) and getattr(role, "ephemeral", False)
                       for role in getattr(config, "roles", ())):
        return
    result = runner(owner_command(config, config_path, "record", *arguments), capture_output=True, text=True)
    if getattr(result, "returncode", 0) != 0:
        print_func(f"switchyard: could not record {' '.join(arguments)} for worker admission: "
                   f"{str(getattr(result, 'stderr', '') or '').strip()[-300:]}")


def record_stop(config: Any, *, runner: Callable[..., Any], print_func: Callable[[str], None] = print) -> str:
    """`stop`'s record, as the project account, under the admission lock: it waits for a start under way, and
    from then on no demand start happens until the next launch clears it. Returns why it could not, or ""."""
    from scripts import team_launcher as launcher

    if not any(getattr(role, "detached", False) and getattr(role, "ephemeral", False)
               for role in getattr(config, "roles", ())):
        return ""
    try:
        owner = str(getattr(config, "run_as_user", "") or "") or launcher.current_user_name()
        result = runner(launcher._owner_command_args(owner, [sys.executable, str(Path(__file__).resolve()), "stopped",
                                                             "--session-dir", str(config.session_dir)]),
                        capture_output=True, text=True)
        why = "" if result.returncode == 0 else \
            f"exit {result.returncode}: {str(getattr(result, 'stderr', '') or '').strip()[-300:]}"
    except Exception as exc:  # noqa: BLE001 - said, and the stop is then not clean
        why = f"{type(exc).__name__}: {exc}"
    if why:
        print_func(f"switchyard: could not record the stop for worker admission ({why})")
    return why


def unrecorded_stop(config: Any, why: str, *, runner: Callable[..., Any], print_func: Callable[[str], None] = print) -> int:
    """`stop` without its record (Audit, SYRD-688): sweep again and say the stop is not clean.

    Without the record, `stop` could not wait for a demand start under way. One
    that finished during the first sweep is ended by this second one; one that
    finishes after it finds no core session and ends its own worker (`_start`).
    The stop still answers nonzero, so neither the operator nor a suspension
    takes it for a clean one.
    """
    from scripts import team_launcher as launcher

    if not why:
        return 0
    launcher.stop_role_sessions(config, runner=runner, print_func=print_func)
    print_func(f"switchyard: the stop is not clean: worker admission could not record it ({why}). Every role "
               f"session was swept twice; run `switchyard stop {config.project}` again to record it.")
    return 1


def owner_command(config: Any, config_path: Path, *arguments: str) -> list[str]:
    """This module run as the project account, wherever the caller runs."""
    from scripts import team_launcher as launcher

    owner = str(getattr(config, "run_as_user", "") or "") or launcher.current_user_name()
    action, *rest = arguments
    return launcher._owner_command_args(owner, [sys.executable, str(Path(__file__).resolve()), action, config.project,
                                                "--config", str(config_path), *rest])


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Which pull workers run, as the project account (SYRD-688).")
    parser.add_argument("action", choices=["admit", "record", "stopped"])
    parser.add_argument("project", nargs="?")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--mode", choices=["launch", "demand"], default="launch")
    parser.add_argument("--operator-stopped", nargs="*", default=[])
    parser.add_argument("--operator-started", nargs="*", default=[])
    parser.add_argument("--busy", nargs="*", default=[], help="demand: running claimants the listener saw unable to claim")
    args = parser.parse_args(argv)
    if args.action == "stopped":
        with locked_state(args.session_dir) as state:
            state["stopped"] = time.time()
            state["starting"].clear()
        return 0
    if not args.project or args.config is None:
        parser.error(f"{args.action} needs the project and --config")
    if args.action == "record":
        record(args.project, args.config, operator_stopped=args.operator_stopped,
               operator_started=args.operator_started)
        return 0
    answer = admit(args.project, args.config, mode=args.mode, busy={role: "busy" for role in args.busy})
    print(json.dumps(answer, sort_keys=True))
    return 1 if any(answer.get("started", {}).values()) else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
