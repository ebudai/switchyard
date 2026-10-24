"""`switchyard-bounded`: run one command under a deadline, as a role's tool would (SYRD-616).

SYRD-403's `tests/bounded_run.py` bounds the board's own mutation and
adversarial runs. This is the same discipline for any command a role starts:
a test, a mutation plan, a comparison.

* `--deadline DUR` is a hard deadline. At it the command's own process group,
  and every descendant that left it, is sent SIGINT, then SIGTERM, then
  SIGKILL, `--grace` apart. Whatever outlives that is reported, never waited
  for. A timeout exits 124, never 0.
* `--expect DUR` declares a longer budget than the board's observation
  threshold, so the Director is not told about a quiet long test too early.
  Nothing is stopped at it.
* `--run NAME --total DUR` shares one whole-run deadline between every command
  of a run: each gets no more than the run has left, and once it is spent the
  next command is refused (124) without starting.
* The declaration is written beside the pane-state files for the listener
  (`execution_lease.declare`) and withdrawn when the command ends.
* SIGINT, SIGTERM or SIGHUP sent to this wrapper -- a person, or the
  Director's interrupt carried out by the listener -- stops the command's
  group the same way and exits 130, saying who asked when the board left a
  marker.

The command's output goes to files, not pipes, as in SYRD-403: a child that
never reads its input cannot block it, and nothing left behind can hold its
output open. At most `--output-limit` bytes of each are printed, head and tail.
The command runs in a process group of its own inside the caller's session,
so the board sees one session for one tool command, and nothing outside that
group -- the provider, the caller's shell, another role -- is ever signalled.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import execution_lease as lease

DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_OUTPUT_LIMIT = 64 * 1024
EXIT_TIMEOUT = 124
EXIT_LEFTOVER = 125
EXIT_INTERRUPTED = 130
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
PR_SET_CHILD_SUBREAPER = 36


@dataclass
class Result:
    status: str  # completed | timeout | interrupted | refused
    returncode: int | None
    seconds: float
    reason: str = ""
    signals: list[str] = field(default_factory=list)
    leftover: list[int] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""

    @property
    def exit_code(self) -> int:
        if self.status == "completed":
            return EXIT_LEFTOVER if self.leftover and self.returncode == 0 else int(self.returncode or 0)
        return EXIT_INTERRUPTED if self.status == "interrupted" else EXIT_TIMEOUT


def _subreaper() -> bool:
    try:
        import ctypes

        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _own_children() -> set[int]:
    children: set[int] = set()
    for entry in Path(f"/proc/{os.getpid()}/task").glob("*/children"):
        try:
            children |= {int(pid) for pid in entry.read_text().split()}
        except OSError:
            continue
    return children


def _alive(pid: int) -> bool:
    """Running, not a zombie waiting for its reaper."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:].split()[:1] != ["Z"]


def _group_members(pgid: int) -> set[int]:
    members: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            fields = stat[stat.rfind(")") + 2:].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
            members.add(int(entry.name))
    return members


def _reap(pids: set[int]) -> None:
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _read_bounded(handle, limit: int) -> str:
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


def stop(pgid: int, gained, *, grace: float) -> tuple[list[str], list[int]]:
    """SIGINT, SIGTERM, SIGKILL to the group and to each gained descendant, `grace` apart; what was sent, what is left.

    `gained` is called again at each step, so a descendant that leaves the
    group or is reparented here mid-way is stopped too.
    """
    sent: list[str] = []
    for index, sig in enumerate(STOP_SIGNALS):
        targets = gained()
        if not _group_members(pgid) and not targets:
            break
        sent.append(signal.Signals(sig).name)
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in targets:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        until = time.monotonic() + grace
        while time.monotonic() < until:
            _reap(gained() | {pgid})
            if not _group_members(pgid) and not {pid for pid in gained() if _alive(pid)}:
                break
            time.sleep(0.02)
    _reap(gained() | {pgid})
    left = sorted(_group_members(pgid) | {pid for pid in gained() if _alive(pid)})
    return sent, left


def _run_budget(directory: Path, name: str, total: float, now: float) -> float:
    """Seconds the run named `name` has left, starting it on first use."""
    path = directory / f"run-{name}.json"
    record = lease._read_json(path)
    if record is None or float(record.get("total") or 0) != float(total):
        record = {"started": now, "total": float(total)}
        lease._write_json(path, record)
    return float(record["started"]) + float(record["total"]) - now


def run(argv: Sequence[str], *, deadline: float | None = None, expect: float | None = None, grace: float = DEFAULT_GRACE_SECONDS,
        output_limit: int = DEFAULT_OUTPUT_LIMIT, label: str = "", run_name: str = "", total: float | None = None,
        directory: Path | None = None, cwd: str | None = None) -> Result:
    """Run `argv` under its deadline; see the module docstring."""
    started, wall = time.monotonic(), time.time()
    directory = lease.lease_dir() if directory is None else directory
    limit = deadline
    if run_name and total is not None:
        left = _run_budget(directory, run_name, total, wall)
        if left <= 0:
            return Result("refused", None, 0.0, f"the run {run_name!r} has spent its whole-run budget of {total:g}s")
        limit = left if limit is None else min(limit, left)
    hard = limit is not None
    session = os.getsid(0)
    leader = lease.start_ticks(session)
    sweep = _subreaper()
    before = _own_children()
    requested: list[int] = []

    def ask_to_stop(signum, _frame) -> None:
        requested.append(signum)

    previous = {sig: signal.signal(sig, ask_to_stop) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, process_group=0)
        budget = limit if hard else expect
        declared = None
        if budget is not None and leader:
            try:
                declared = lease.declare(directory, session, leader, deadline=wall + budget, hard=hard, label=label,
                                         command=lease.sanitize_command(argv, cwd=cwd or os.getcwd()), run=run_name)
            except OSError as exc:
                print(f"switchyard-bounded: the board will not see this budget ({exc})", file=sys.stderr)

        def gained() -> set[int]:
            return (_own_children() - before - {proc.pid}) if sweep else set()

        status, reason = "completed", ""
        try:
            while proc.poll() is None:
                if requested:
                    status = "interrupted"
                    break
                if hard and time.monotonic() - started >= limit:
                    status, reason = "timeout", f"no result within {limit:g}s"
                    break
                time.sleep(0.05)
        finally:
            sent, left = (stop(proc.pid, gained, grace=grace) if status != "completed" or _group_members(proc.pid)
                          or gained() else ([], []))
            proc.poll()
            if declared is not None:
                lease.withdraw(directory, session, leader)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        if status == "interrupted":
            marker = lease.interrupt_marker(directory, session, leader) if leader else None
            reason = (f"interrupted by the {marker.get('actor') or 'Director'}: {marker.get('reason') or 'no reason given'}"
                      if marker else f"interrupted by {signal.Signals(requested[0]).name}")
        if sent:
            reason = (reason + "; " if reason else "") + f"process group {proc.pid} sent {', '.join(sent)}"
        if left:
            reason = (reason + "; " if reason else "") + f"still present after cleanup: {left}"
        return Result(status, proc.returncode if status == "completed" else None, round(time.monotonic() - started, 3),
                      reason, sent, left, _read_bounded(out, output_limit), _read_bounded(err, output_limit))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="switchyard-bounded",
        description="Run one command under a deadline the board can see. A timeout exits 124, never 0.")
    parser.add_argument("--deadline", type=lease.parse_duration, help="hard deadline: stop the command at it (e.g. 20m)")
    parser.add_argument("--expect", type=lease.parse_duration, help="expected budget: tell the board, stop nothing")
    parser.add_argument("--run", default="", help="a name shared by every command of one run")
    parser.add_argument("--total", type=lease.parse_duration, help="the run's whole-run deadline (with --run)")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE_SECONDS, help="seconds between stop signals")
    parser.add_argument("--output-limit", type=int, default=DEFAULT_OUTPUT_LIMIT, help="bytes of each output kept")
    parser.add_argument("--label", default="", help="what this run is, for the board")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command after --")
    if args.deadline is None and args.expect is None and args.total is None:
        parser.error("give --deadline, --expect, or --run with --total")
    if (args.total is None) != (not args.run):
        parser.error("--run and --total go together")
    result = run(command, deadline=args.deadline, expect=args.expect, grace=args.grace, output_limit=args.output_limit,
                 label=args.label, run_name=args.run, total=args.total)
    sys.stdout.write(result.stdout)
    sys.stdout.flush()
    sys.stderr.write(result.stderr)
    verdict = {"completed": f"exit {result.returncode}", "timeout": "timeout", "interrupted": "interrupted",
               "refused": "not started"}[result.status]
    print(f"switchyard-bounded: {verdict} after {result.seconds:g}s" + (f"; {result.reason}" if result.reason else ""),
          file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
