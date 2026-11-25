#!/usr/bin/env python3
"""A cold sweep: each named suite run where it can work, from nothing of the invoking pane (SYRD-815).

    python3 tests/sweep.py [--root TREE] [--out DIR] [--jobs N] [--timeout SECONDS] [--no-sandbox] SUITE...

A SUITE is `tests/<name>.py`, `<name>.py` or `<name>`. Each runs:
- **in a bwrap sandbox** by default: the tree and a disposable HOME writable, everything else read-only, a private
  /tmp and /run, no network, a new pid namespace and session, `env -i` with only HOME, the XDG directories, PATH,
  LANG and PYTHONDONTWRITEBYTECODE=1;
- **on the host** when `tests/sweep_requirements.py` says the sandbox cannot give it what it needs, when bwrap is
  absent, or with --no-sandbox: still `env -i`, a temporary directory other accounts can traverse (a suite that
  drops to another uid must reach its own files), and a disposable HOME -- or none for a suite that calls
  `live_boundary.isolate()` first (read from its syntax), which then builds its own disposable root, as it does
  for a run from a pane.

A suite declared load-sensitive that fails is run once more, alone, and reported as passing on retry if it does.
Each suite's output goes to DIR/<name>.log; one line per suite is printed, then a summary. The exit status is 1 when
any suite failed. It never runs as root: guard-only suites (SYRD-808) are not for a sweep that does.

The sweep tells the board how long its own guards let it run (SYRD-882): ceil(suites / jobs) x timeout, twice for a
retry, plus one more timeout, declared for its session as `switchyard-bounded --expect` declares one, and withdrawn
when it ends. So the Director hears only of a sweep that outlives its own guard -- a hang -- not of every long healthy
one. A declaration already standing for the session (an outer `switchyard-bounded`) is left as it is.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import live_boundary  # noqa: E402
import sweep_requirements  # noqa: E402

ROOT = HERE.parent
sys.path.insert(1, str(ROOT))
from scripts.ticket_board import execution_lease  # noqa: E402
PATH = "/usr/local/bin:/usr/bin:/bin"
LIVE_PANE_STORES = ("TICKET_BOARD_PANE_SESSION_DIR", "TICKET_BOARD_PANE_STATE_DIR")
_alone = threading.Lock()  # a retry runs with nothing else running


@dataclass
class Plan:
    suite: str  # tests/<name>.py
    where: str  # "sandbox" or "host"
    why: str  # why the host, or ""


@dataclass
class Result:
    plan: Plan
    status: str  # PASS, FAIL, PASS-ON-RETRY, TIMEOUT
    seconds: float
    log: Path


def normalise(suite: str) -> str:
    name = suite.rsplit("/", 1)[-1]
    if not name.endswith((".py", ".sh")):
        name += ".py"
    return f"tests/{name}"


def plan(suite: str, *, sandbox: bool = True, bwrap: str | None = None) -> Plan:
    path = normalise(suite)
    reason = sweep_requirements.host_reason(path)
    if reason:
        return Plan(path, "host", f"needs {reason}")
    if not sandbox:
        return Plan(path, "host", "--no-sandbox")
    if not (bwrap if bwrap is not None else shutil.which("bwrap")):
        return Plan(path, "host", "bwrap is not installed")
    return Plan(path, "sandbox", "")


def _scratch() -> Path:
    """A fresh directory under the system temporary directory, traversable by other accounts, its own subdirs ours."""
    root = Path(tempfile.mkdtemp(prefix="sweep.", dir="/tmp"))
    root.chmod(0o711)
    for name in ("home", "config", "state", "run", "tmp"):
        (root / name).mkdir(mode=0o700)
    (root / "tmp").chmod(0o1777)  # a TMPDIR the suite's namespace uids can create their own entries in
    return root


def base_env(scratch: Path) -> dict[str, str]:
    return {"PATH": PATH, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(scratch / "home"),
            "XDG_CONFIG_HOME": str(scratch / "config"), "XDG_STATE_HOME": str(scratch / "state"),
            "XDG_RUNTIME_DIR": str(scratch / "run"), "TMPDIR": str(scratch / "tmp")}


def host_env(plan_: Plan, scratch: Path, root: Path) -> dict[str, str]:
    env = base_env(scratch)
    source = root / plan_.suite
    # Read from its syntax: a module-level isolate() before any production import, or a suite declared to call it
    # first in main(). One that only mentions it, or calls it later, would hold the live store names (SYRD-820).
    text = source.read_text(encoding="utf-8", errors="replace") if source.suffix == ".py" else ""
    if text and (live_boundary.isolates_first(text) or sweep_requirements.isolates_in_main(plan_.suite)):
        # Its own disposable root, as for a run from a pane; a fixture HOME would read as "prepared on purpose"
        # and leave its namespace children without one (SYRD-806 triage).
        env["HOME"] = ""
        for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
            env.pop(name)
        # And which stores the live pane this sweep runs from writes in: isolate() reads these before it removes
        # them, so its sentinel lets that pane's own hooks keep rewriting their records (and nothing else) while
        # the suite runs. Without them every such rewrite is drift (SYRD-815).
        for name in LIVE_PANE_STORES:
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


def command(plan_: Plan) -> list[str]:
    return ["bash", plan_.suite] if plan_.suite.endswith(".sh") else [sys.executable, "-B", plan_.suite]


def sandbox_argv(plan_: Plan, scratch: Path, root: Path, *, bwrap: str = "bwrap") -> list[str]:
    env = base_env(scratch)
    return [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/run", "--tmpfs", "/tmp",
            "--bind", str(root), str(root), "--bind", str(scratch), str(scratch),
            "--unshare-net", "--unshare-pid", "--die-with-parent", "--new-session", "--chdir", str(root),
            "env", "-i", *[f"{key}={value}" for key, value in env.items()], *command(plan_)]


def run_once(plan_: Plan, *, root: Path, log: Path, timeout: float) -> str:
    scratch = _scratch()
    try:
        if plan_.where == "sandbox":
            argv, env = sandbox_argv(plan_, scratch, root), {"PATH": PATH}
        else:
            argv, env = command(plan_), host_env(plan_, scratch, root)
        with log.open("w", encoding="utf-8") as out:
            out.write(f"# {plan_.suite}: {plan_.where}{' (' + plan_.why + ')' if plan_.why else ''}\n")
            out.flush()
            try:
                done = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                      stderr=subprocess.STDOUT, timeout=timeout)
            except subprocess.TimeoutExpired:
                return "TIMEOUT"
        return "PASS" if done.returncode == 0 else "FAIL"
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_suite(plan_: Plan, *, root: Path, out: Path, timeout: float) -> Result:
    started = time.monotonic()
    log = out / f"{Path(plan_.suite).stem}.log"
    status = run_once(plan_, root=root, log=log, timeout=timeout)
    if status != "PASS" and sweep_requirements.load_sensitive(plan_.suite):
        with _alone:
            first = log.read_text(encoding="utf-8", errors="replace")
            retry = log.with_suffix(".retry.log")
            if run_once(plan_, root=root, log=retry, timeout=timeout) == "PASS":
                status = "PASS-ON-RETRY"
                log.write_text(first + "\n# retried alone (load-sensitive): passed; see the .retry.log\n")
    return Result(plan_, status, time.monotonic() - started, log)


def budget(suites: int, jobs: int, timeout: float) -> float:
    """How long this sweep's own guards let it run: every round of `jobs` suites up to `timeout`, twice for a retry,
    and one more `timeout` of margin (SYRD-882)."""
    return math.ceil(max(1, suites) / max(1, jobs)) * timeout * 2 + timeout


def declare(suites: int, jobs: int, timeout: float, argv: list[str]) -> execution_lease.SessionLease | None:
    """Declare this sweep's budget for its session, unless one already stands; say what the board will hold it to."""
    own = execution_lease.SessionLease()
    wanted = budget(suites, jobs, timeout)
    honoured, cap = execution_lease.capped(wanted)
    if own.standing():
        print("sweep: this session already declares its budget to the board; leaving it as it is", file=sys.stderr)
        return None
    try:
        declared = own.declare(wanted, hard=False, label=f"sweep of {suites} suites", run="",
                               command=execution_lease.sanitize_command([Path(sys.argv[0]).name, *argv]))
    except OSError as exc:
        print(f"sweep: the board will not see this sweep's budget ({exc})", file=sys.stderr)
        return None
    if declared is None:
        return None
    print(f"sweep: told the board to expect up to {wanted:.0f}s ({suites} suites, {jobs} jobs, {timeout:g}s each, a retry)"
          + (f", capped at its {cap:.0f}s" if honoured < wanted else ""), file=sys.stderr)
    return own


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("suites", nargs="+")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=1500)
    parser.add_argument("--no-sandbox", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        raise SystemExit("sweep: refusing to run as root; guard-only suites are never run as root (SYRD-808)")
    root = args.root.resolve()
    out = args.out or Path(tempfile.mkdtemp(prefix="sweep-logs."))
    out.mkdir(parents=True, exist_ok=True)
    plans = [plan(suite, sandbox=not args.no_sandbox) for suite in args.suites]
    own = declare(len(plans), args.jobs, args.timeout, list(sys.argv[1:] if argv is None else argv))
    try:
        with ThreadPoolExecutor(max(1, args.jobs)) as pool:
            results = list(pool.map(lambda p: run_suite(p, root=root, out=out, timeout=args.timeout), plans))
    finally:
        if own is not None:
            own.withdraw()
    for result in results:
        where = result.plan.where + (f" ({result.plan.why})" if result.plan.why else "")
        print(f"{Path(result.plan.suite).stem} {result.status} {where} {result.seconds:.0f}s")
    failed = [r for r in results if r.status in ("FAIL", "TIMEOUT")]
    counts = {status: sum(r.status == status for r in results) for status in ("PASS", "PASS-ON-RETRY", "FAIL", "TIMEOUT")}
    print("sweep: " + ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if n) + f"; logs in {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
