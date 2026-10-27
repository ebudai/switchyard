"""Ask for a pull worker to be started when ready work outruns the running ones (SYRD-688): the listener's half.

A launch no longer starts idle pull workers, so ready work admitted later
may find none running, and pull pickup only gives work to a running worker.
After each pickup pass, `trigger` looks at what is left: claimable ready work
that no eligible worker took, a ticket pinned to a worker that is not
running, or a worker holding a pulled ticket that is not running -- held work
a launch could not recover, or a worker that died. It passes on which running
workers it saw unable to claim (`BUSY`), so the child never counts a busy
worker as idle capacity. Only then does it run `scripts/worker_admission.py admit --mode
demand` for its own project, as the project account it already is. That
child decides under the admission lock, from the board and the project's own
sessions, which declared claimants to start -- respecting quota holds,
operator stops, failed starts and a stopped project -- and starts at most
MAX_STARTS_PER_PASS of them. The listener runs no other command for this, and
one child at a time, at most every MIN_INTERVAL_SECONDS.

A listener without process authority -- one whose roles run as other
accounts -- is not the project account and asks for nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

REGISTRY_DIR_ENV = "SWITCHYARD_PROJECT_REGISTRY_DIR"
DEFAULT_REGISTRY_DIR = Path("/etc/switchyard/projects")
#: The shortest time between two asks: demand nothing can meet is not asked about every pass.
MIN_INTERVAL_SECONDS = 60.0
ADMISSION = Path(__file__).resolve().parents[1] / "worker_admission.py"
#: What `pull_pickup.why_not_eligible` says of a running worker that cannot take work now. Unknown readiness is not
#: here: a worker that has just started reads that way, and is capacity on its way.
BUSY = frozenset({"working", "stopped on a prompt", "background work running"})


def unmet(ready_work: list[dict[str, Any]], eligibility: dict[str, str], not_running: frozenset[str],
          claimed: list[dict[str, Any]] = ()) -> str:
    """Why a worker should be started now, or "": work no running worker took, a pinned author not running,
    or a worker holding a pulled ticket that is not running."""
    for claim in claimed:
        if eligibility.get(str(claim.get("role"))) in not_running:
            return f"{claim.get('role')} holds {claim.get('id')} and is not running"
    for ticket in ready_work:
        if not ticket.get("claimable"):
            continue
        author = str(ticket.get("waiting_for") or "")
        if not author:
            return f"{ticket.get('id')} is ready and no running worker took it"
        if eligibility.get(author) in not_running:
            return f"{ticket.get('id')} waits for {author}, which is not running"
    return ""


def _config_path(project: str, environ: Any) -> Path:
    registry = Path(str(environ.get(REGISTRY_DIR_ENV) or "").strip() or DEFAULT_REGISTRY_DIR)
    return Path(json.loads((registry / f"{project}.json").read_text(encoding="utf-8"))["config_path"])


def trigger(listener: Any, conn: Any, eligibility: dict[str, str], not_running: frozenset[str], *,
            environ: Any = None, spawn: Callable[..., Any] | None = None,
            clock: Callable[[], float] | None = None) -> str:
    """One pass: report a finished ask, then ask again if work is waiting and nothing is under way. Says why."""
    environ, spawn, clock = environ or os.environ, spawn or subprocess.Popen, clock or time.monotonic
    child = getattr(listener, "_demand_child", None)
    if child is not None:
        if child.poll() is None:
            return "a start is already being decided"
        _report(listener, child)
        listener._demand_child = None
    if str(environ.get("TICKET_BOARD_PROCESS_AUTHORITY") or "").strip() != "1":
        return "this listener is not the project account"
    if clock() - getattr(listener, "_demand_asked_at", float("-inf")) < MIN_INTERVAL_SECONDS:
        return "asked recently"
    row = conn.execute("SELECT ticket_board.pull_queue_status() AS status").fetchone()
    status = row["status"] if isinstance(row, dict) else row[0]
    status = json.loads(status) if isinstance(status, str) else status
    why = unmet(list((status or {}).get("ready_work") or []), eligibility, not_running,
                list((status or {}).get("claimed") or []))
    if not why:
        return ""
    try:
        config_path = _config_path(listener.project, environ)
        # Files, not pipes: the pane start writes to them too, and nobody reads until a later pass.
        out, err = tempfile.TemporaryFile("w+"), tempfile.TemporaryFile("w+")
        listener._demand_child = spawn(
            [sys.executable, str(ADMISSION), "admit", "--mode", "demand", listener.project, "--config", str(config_path),
             "--busy", *sorted(role for role, reason in eligibility.items() if reason in BUSY)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=err, text=True, start_new_session=True)
        listener._demand_child.outputs = (out, err)
    except Exception as exc:  # logged, never fatal to the pass
        listener.logger.warning("Demand start for %s could not be asked: %s", listener.project, exc)
        return f"could not ask: {exc}"
    listener._demand_asked_at = clock()
    listener.logger.info("Demand start: %s; asking which pull worker to start", why)
    return why


def _report(listener: Any, child: Any) -> None:
    """What the finished child decided and started, in the listener's log."""
    child.wait()
    outputs = getattr(child, "outputs", None)
    out = err = ""
    for handle, text in zip(outputs or (), ("out", "err")):
        handle.seek(0)
        if text == "out":
            out = handle.read()
        else:
            err = handle.read()
        handle.close()
    try:
        answer = json.loads(str(out or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        listener.logger.warning("Demand start exited %s without an answer: %s", child.returncode, str(err or "")[-500:])
        return
    if answer.get("reason"):
        listener.logger.info("Demand start: nothing started: %s", answer["reason"])
    for role, failure in (answer.get("started") or {}).items():
        if failure.startswith("not started:"):
            listener.logger.info("Demand start: %s %s", role, failure)
        elif failure:
            listener.logger.warning("Demand start: %s failed to start: %s", role, failure)
        else:
            listener.logger.info("Demand start: started %s for ready work", role)
    if not answer.get("started") and not answer.get("reason"):
        left = [f"{d['role']} ({d['kind']})" for d in answer.get("decisions", []) if d["kind"].startswith("left")]
        listener.logger.info("Demand start: no worker could be started: %s", ", ".join(left) or "none declared")
