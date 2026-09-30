#!/usr/bin/env python3
"""Bounded execution for mutation and other adversarial test runs (SYRD-403).

On 2026-09-28 a scratch mutation runner started a test child for the mutant
"a cycle not stopped". The mutant turned a loop guard into one that never
fired, the child spun at 100% CPU, and the runner's `subprocess.run` had no
timeout. The Bash tool moved the chain to the background at its 600-second
limit, the output file stayed empty because nothing was flushed, the
worktree stayed mutated, and the chain ran for 6h21m until the user noticed.

This module is the reusable fix. Import it from a scratch runner
(`sys.path.insert(0, "<repo>/tests")`) or call its command line.

* `run_bounded(argv, timeout=...)` runs one command in a new session and
  process group, and returns within `timeout` + 2 x `grace` plus polling
  slack.
  - **Files, not pipes:** stdin, stdout and stderr are temporary files. A
    child that never reads its input cannot block the runner before the
    deadline starts, and a child left behind cannot hold output open.
  - **One cleanup budget:** after a timeout, a normal exit or an error while
    waiting, one cleanup budget of 2 x `grace` covers the whole group and
    every descendant the runner gained. SIGTERM goes first, then SIGKILL, and
    every wait is a non-blocking poll.
  - **Reported, not awaited:** whatever is still present when the budget
    runs out is reported in `leftover_group` or `unreaped`, and
    `Outcome.clean` is False.
  - **Bounded output:** at most `output_limit` bytes of each output file are
    read, as head and tail.
  - The result says `completed` or `timeout`, never both.
* `Deadline(seconds)` is a whole-run budget; `case_timeout()` never gives a
  case more time than the run has left.
* `run_mutation_plan(...)` applies each mutant with exact-count edits, runs
  the suites through `run_bounded`, and restores the file byte-for-byte and
  clears `__pycache__` in `finally`. It records every mutant as `killed`,
  `survived` or `inconclusive`. A timeout, a run that left processes behind
  (not `clean`), a spent whole-run budget or an edit that does not apply is
  `inconclusive`: never a kill, and never a pass.
* On the command line, `run` exits 124 on a timeout and 125 when processes
  outlived cleanup. `mutate` exits 0 when every mutant was killed, 1 when any
  survived, and 2 when any was inconclusive.

Limits. The defaults come from measured runtimes (2026-09-30):
- 80 focused suite runs from SYRD-515 to SYRD-518 had a median under 1s, a
  90th percentile of 6s and a maximum of 32s
  (`ticket_board_listener_pane_state_authority_test`).
- SYRD-402's whole 34-mutant run took 14s once it was bounded.

So a case gets 300s (about nine times the slowest suite). A whole run gets
540s, just under the Bash tool's 600-second foreground limit, so an
over-long run fails inside the turn that started it instead of drifting into
the background. Override them deliberately with `--case-seconds` and
`--total-seconds`, or with `SWITCHYARD_BOUNDED_CASE_SECONDS` and
`SWITCHYARD_BOUNDED_TOTAL_SECONDS`.

Descendants that leave the group. A process that leaves the process group
on purpose (`setsid`, `start_new_session=True`) is not reached by the group
signal. A nested harness is exactly that case. The runner therefore makes
itself a child subreaper (Linux `prctl`), so such a process is reparented to
it when its parent dies. Within the same cleanup budget, it stops and reaps
every child it did not have before the call, reading only its own
`/proc/self/task/*/children` list and never another process's `/proc`.
Because a subreaper stays one, unrelated orphans reparent to a process that
has used `run_bounded` until that process exits.
`Outcome.escaped` counts them. Where the runner cannot become a subreaper or
list its own children, `sweep_available` is False and they would go unseen.

This module bounds time, not reach: run anything that touches the host
inside its own sandbox.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

DEFAULT_CASE_SECONDS = 300.0
DEFAULT_TOTAL_SECONDS = 540.0
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_OUTPUT_LIMIT = 64 * 1024

COMPLETED = "completed"
TIMEOUT = "timeout"
KILLED = "killed"
SURVIVED = "survived"
INCONCLUSIVE = "inconclusive"


def configured_seconds(value: float | None, env_name: str, default: float) -> float:
    """An explicit value, else the environment override, else the documented default."""
    if value is not None:
        return float(value)
    raw = os.environ.get(env_name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True)
class Outcome:
    """What happened to one bounded command. `status` is `completed` or `timeout`."""

    label: str
    status: str
    returncode: int | None
    seconds: float
    timeout: float
    stdout: str
    stderr: str
    reason: str = ""
    #: the group could still be signalled when the cleanup budget ran out: a
    #: member in uninterruptible sleep, or a killed member waiting as a zombie
    #: for a reaper other than this runner
    leftover_group: bool = False
    #: descendants that had left the command's process group (setsid, a new
    #: session), were reparented to this runner and were stopped
    escaped: int = 0
    #: False where this runner cannot become a child subreaper or list its own
    #: children; escaped descendants would then go unseen
    sweep_available: bool = True
    #: processes this runner was responsible for and had not reaped when the
    #: cleanup budget ran out; reported, never waited for
    unreaped: tuple[int, ...] = ()

    @property
    def timed_out(self) -> bool:
        return self.status == TIMEOUT

    @property
    def clean(self) -> bool:
        """Nothing this run started is known to be left: no signalable group, nothing unreaped."""
        return not self.leftover_group and not self.unreaped


def _read_bounded(handle, limit: int) -> str:
    """At most `limit` bytes of a finished output file, head and tail, without reading the rest."""
    size = os.fstat(handle.fileno()).st_size
    handle.seek(0)
    if size <= limit:
        return handle.read(size).decode("utf-8", errors="replace")
    half = limit // 2
    head = handle.read(half)
    handle.seek(size - half)
    tail = handle.read(half)
    return (head.decode("utf-8", errors="replace") + f"\n[... {size - 2 * half} bytes omitted ...]\n"
            + tail.decode("utf-8", errors="replace"))


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


PR_SET_CHILD_SUBREAPER = 36
_subreaper: bool | None = None


def _become_subreaper() -> bool:
    """Make orphaned descendants reparent to this process instead of init (Linux), once."""
    global _subreaper
    if _subreaper is None:
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            _subreaper = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
        except (OSError, AttributeError):
            _subreaper = False
    return _subreaper


def _own_children() -> set[int] | None:
    """This process's own children, from its own /proc entry; None where the kernel does not list them."""
    files = list(Path(f"/proc/{os.getpid()}/task").glob("*/children"))
    if not files:
        return None
    children: set[int] = set()
    for entry in files:
        try:
            children |= {int(pid) for pid in entry.read_text().split()}
        except OSError:
            continue
    return children


def _outside_group(pid: int, pgid: int) -> bool:
    try:
        return os.getpgid(pid) != pgid
    except ProcessLookupError:
        return False


def _cleanup(proc: subprocess.Popen, *, stop_group: bool, before: set[int], sweep: bool,
             grace: float) -> tuple[bool, tuple[int, ...], int]:
    """Stop the command's group and every child this runner gained, all within one budget of 2 x `grace`.

    SIGTERM first, SIGKILL from `grace` on, to the group and to each gained
    child, including descendants reparented here mid-cleanup. Every wait is a
    non-blocking poll against the shared deadline. Whatever is still present
    when the budget runs out is returned, never waited for. Returns
    (leftover_group, unreaped, escaped).
    """
    pgid = proc.pid  # start_new_session made the child its own group leader
    started = time.monotonic()
    signalled: dict[int, int] = {}
    escaped: set[int] = set()

    def gained() -> set[int]:
        return ((_own_children() or set()) - before - {proc.pid}) if sweep else set()

    def settle() -> tuple[bool, set[int]]:
        proc.poll()
        for pid in gained():
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        remaining = gained()
        group = stop_group and _group_alive(pgid)
        return proc.returncode is not None and not group and not remaining, remaining

    done, remaining = settle()
    for sig, until in ((signal.SIGTERM, started + grace), (signal.SIGKILL, started + 2 * grace)):
        if done:
            break
        if stop_group:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
        while True:
            for pid in remaining:
                if signalled.get(pid) != sig:
                    if _outside_group(pid, pgid):
                        escaped.add(pid)
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
                    signalled[pid] = sig
            done, remaining = settle()
            if done or time.monotonic() >= until:
                break
            time.sleep(0.02)
    leftover = stop_group and _group_alive(pgid)
    unreaped = tuple(sorted(({proc.pid} if proc.returncode is None else set()) | remaining))
    return leftover, unreaped, len(escaped)


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    label: str = "",
    grace: float = DEFAULT_GRACE_SECONDS,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
) -> Outcome:
    """Run `argv` in its own process group; return within `timeout` + 2 x `grace` (plus polling slack)."""
    started = time.monotonic()
    sweep = _become_subreaper() and _own_children() is not None
    before = (_own_children() or set()) if sweep else set()
    # Everything the child reads or writes is a file, never a pipe. A child that
    # never reads stdin cannot block the runner before the deadline starts, and
    # a child left behind cannot hold output open and turn a finished command
    # into a timeout.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as feed:
        if input_text is not None:
            feed.write(input_text.encode("utf-8"))
            feed.seek(0)
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=feed if input_text is not None else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        status, returncode, reason = TIMEOUT, None, ""
        try:
            try:
                proc.wait(timeout=timeout)
                status, returncode = COMPLETED, proc.returncode
            except subprocess.TimeoutExpired:
                reason = f"no result within {timeout:g}s; process group {proc.pid} stopped"
        finally:
            # Runs for a timeout, a normal exit and any error while waiting alike.
            seconds = round(time.monotonic() - started, 3)
            leftover, unreaped, escaped = _cleanup(
                proc, stop_group=status == TIMEOUT or _group_alive(proc.pid), before=before, sweep=sweep, grace=grace)
        if unreaped or leftover:
            reason = (reason + "; " if reason else "") + (
                f"still present after cleanup: group={'yes' if leftover else 'no'} unreaped={list(unreaped)}")
        stdout = _read_bounded(out, output_limit)
        stderr = _read_bounded(err, output_limit)
    return Outcome(
        label=label,
        status=status,
        returncode=returncode,
        seconds=seconds,
        timeout=timeout,
        stdout=stdout,
        stderr=stderr,
        reason=reason,
        leftover_group=leftover,
        escaped=escaped,
        sweep_available=sweep,
        unreaped=unreaped,
    )


class Deadline:
    """A whole-run budget shared by every case in the run."""

    def __init__(self, seconds: float) -> None:
        self.seconds = float(seconds)
        self.started = time.monotonic()

    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def case_timeout(self, case_seconds: float) -> float:
        return min(float(case_seconds), self.remaining())


@dataclass(frozen=True)
class Mutant:
    """One planted fault: exact replacements in one file, each old text present exactly once."""

    name: str
    path: str
    edits: tuple[tuple[str, str], ...]


@dataclass
class MutantResult:
    name: str
    result: str
    reason: str
    seconds: float
    suites: list[dict] = field(default_factory=list)


def _apply(root: Path, mutant: Mutant) -> bytes:
    target = root / mutant.path
    original = target.read_bytes()
    text = original.decode("utf-8")
    for old, new in mutant.edits:
        count = text.count(old)
        if count != 1:
            raise ValueError(f"{mutant.name}: expected one occurrence in {mutant.path}, found {count}: {old[:60]!r}")
        text = text.replace(old, new)
    target.write_text(text, encoding="utf-8")
    return original


def _restore(root: Path, mutant: Mutant, original: bytes) -> None:
    target = root / mutant.path
    target.write_bytes(original)
    if target.read_bytes() != original:
        raise RuntimeError(f"{mutant.name}: {mutant.path} was not restored byte-for-byte")
    # A same-second restore can leave bytecode compiled from the mutant.
    shutil.rmtree(target.parent / "__pycache__", ignore_errors=True)


def run_mutation_plan(
    mutants: Sequence[Mutant],
    suites: Sequence[Sequence[str]],
    *,
    root: str | os.PathLike,
    case_seconds: float | None = None,
    total_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
    killed_when: Callable[[Outcome], bool] = lambda outcome: outcome.returncode != 0,
    report: Callable[[MutantResult], None] | None = None,
    grace: float = DEFAULT_GRACE_SECONDS,
) -> list[MutantResult]:
    """Run every suite against every mutant, in order, stopping a mutant's suites at its first kill.

    `killed_when` decides what a completed non-passing run means. The default
    counts any non-zero exit. A caller whose sandbox refuses host access
    should exclude those refusals, because a refusal is not the test catching
    the mutant. A timeout never reaches `killed_when`.
    """
    root = Path(root)
    case = configured_seconds(case_seconds, "SWITCHYARD_BOUNDED_CASE_SECONDS", DEFAULT_CASE_SECONDS)
    deadline = Deadline(configured_seconds(total_seconds, "SWITCHYARD_BOUNDED_TOTAL_SECONDS", DEFAULT_TOTAL_SECONDS))
    results: list[MutantResult] = []
    for mutant in mutants:
        started = time.monotonic()
        if deadline.expired():
            result = MutantResult(mutant.name, INCONCLUSIVE, f"not run: the whole-run budget of {deadline.seconds:g}s was spent", 0.0)
        else:
            result = _run_one(root, mutant, suites, case, deadline, env, killed_when, grace)
        result.seconds = round(time.monotonic() - started, 3)
        results.append(result)
        if report is not None:
            report(result)
    return results


def _run_one(root, mutant, suites, case, deadline, env, killed_when, grace) -> MutantResult:
    try:
        original = _apply(root, mutant)
    except (OSError, ValueError) as exc:
        return MutantResult(mutant.name, INCONCLUSIVE, f"not applied: {exc}", 0.0)
    try:
        records = []
        for argv in suites:
            budget = deadline.case_timeout(case)
            if budget <= 0:
                return MutantResult(mutant.name, INCONCLUSIVE, "the whole-run budget ran out mid-mutant", 0.0, records)
            outcome = run_bounded(argv, timeout=budget, cwd=root, env=env, label=f"{mutant.name} :: {' '.join(argv)}", grace=grace)
            records.append({"argv": list(argv), "status": outcome.status, "returncode": outcome.returncode,
                            "seconds": outcome.seconds, "tail": (outcome.stdout + outcome.stderr).strip()[-300:]})
            if outcome.timed_out:
                return MutantResult(mutant.name, INCONCLUSIVE, f"timeout: {outcome.reason}", 0.0, records)
            if not outcome.clean:
                return MutantResult(mutant.name, INCONCLUSIVE, f"processes outlived cleanup: {outcome.reason}", 0.0, records)
            if killed_when(outcome):
                return MutantResult(mutant.name, KILLED, f"failed {' '.join(argv)} (exit {outcome.returncode})", 0.0, records)
        return MutantResult(mutant.name, SURVIVED, "every suite passed", 0.0, records)
    finally:
        _restore(root, mutant, original)


def summarize(results: Sequence[MutantResult]) -> tuple[str, int]:
    """One line for a human, and the exit code: 0 all killed, 1 any survivor, 2 any inconclusive and no survivor."""
    counts = {state: sum(r.result == state for r in results) for state in (KILLED, SURVIVED, INCONCLUSIVE)}
    line = (f"{len(results)} mutants: {counts[KILLED]} killed, {counts[SURVIVED]} survived, "
            f"{counts[INCONCLUSIVE]} inconclusive (a timeout is inconclusive, never killed)")
    code = 1 if counts[SURVIVED] else 2 if counts[INCONCLUSIVE] else 0
    return line, code


# -- command line ---------------------------------------------------------------------------------------------------
def _cli_run(args: argparse.Namespace) -> int:
    timeout = configured_seconds(args.case_seconds, "SWITCHYARD_BOUNDED_CASE_SECONDS", DEFAULT_CASE_SECONDS)
    outcome = run_bounded(args.command, timeout=timeout, label=" ".join(args.command))
    sys.stdout.write(outcome.stdout)
    sys.stderr.write(outcome.stderr)
    if outcome.timed_out:
        print(f"BOUNDED-RUN TIMEOUT after {outcome.seconds:g}s: {outcome.reason}", file=sys.stderr, flush=True)
        return 124
    if not outcome.clean:
        print(f"BOUNDED-RUN UNCLEAN: {outcome.reason}", file=sys.stderr, flush=True)
        return 125
    return int(outcome.returncode or 0)


def _cli_mutate(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    mutants = [Mutant(m["name"], m["path"], tuple(tuple(edit) for edit in m["edits"])) for m in plan["mutants"]]

    def emit(result: MutantResult) -> None:
        print(json.dumps(asdict(result), sort_keys=True), flush=True)

    results = run_mutation_plan(mutants, plan["suites"], root=plan.get("root", "."), case_seconds=args.case_seconds,
                                total_seconds=args.total_seconds, report=emit)
    line, code = summarize(results)
    print(line, flush=True)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded execution for mutation and adversarial test runs.")
    sub = parser.add_subparsers(dest="verb", required=True)
    run = sub.add_parser("run", help="run one command with a deadline; exit 124 on timeout, like timeout(1)")
    run.add_argument("--case-seconds", type=float, default=None)
    run.add_argument("command", nargs=argparse.REMAINDER)
    mutate = sub.add_parser("mutate", help="run a JSON mutation plan; one JSON line per mutant, then a summary")
    mutate.add_argument("plan")
    mutate.add_argument("--case-seconds", type=float, default=None)
    mutate.add_argument("--total-seconds", type=float, default=None)
    args = parser.parse_args(argv)
    if args.verb == "run":
        if args.command and args.command[0] == "--":
            args.command = args.command[1:]
        if not args.command:
            parser.error("run needs a command after --")
        return _cli_run(args)
    return _cli_mutate(args)


if __name__ == "__main__":
    raise SystemExit(main())
