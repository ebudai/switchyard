"""At a hand-over, tell the Director about background work earlier tickets left running in the role's pane (SYRD-636).

On syrd, 2026-10-20/21, two QEMU VMs App had started for earlier tickets ran
on under App's Claude through the hand-over of SYRD-624 and overnight, 2 x 8
vCPU and 2 x 16 GiB. They held no notice -- a finished turn's sessions that
start nothing new hold nothing -- so SYRD-583, which describes only what holds
a delivery, never saw them, and the Director found them in the pane's footer.

On every listener pass this reads the hand-overs delivered since the last one:
a `transition` notice the receipt of which was witnessed, sent to a role other
than the Director -- the board sends those only to the ticket's owner -- in a
state that is not terminal. For each role's newest it samples that role's pane
and tells the Director, once per child for as long as the board remembers it
(SYRD-583's `background_child_alerts`, so a child already named there is not
named again), about each session of its own under the pane that was already
running at the hand-over, except:
* work for a ticket the role still owns, read from a git worktree it runs in
  or names (`background_child.worktree_ticket`);
* a run bounded by coreutils `timeout` whose deadline has not passed;
* the provider and everything in the pane's own session, which are not a
  session of their own;
* a session the provider started at its own start -- within the minute the
  board already counts as a start's own (`LAUNCHER_SEED_STARTUP_SECONDS`):
  Codex starts its app-server daemon in a session of its own (SYRD-589), and
  no tool runs a command before a turn can begin.

Its ticket is said when a worktree tells it, and "not known" otherwise. Only
the Director is told, and nothing is stopped: the report carries a stop that
signals the group only while its leader is still the process reported.

Never fatal: a failure is logged and the pass goes on.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import background_child

#: How far back a listener that has just started looks for hand-overs it may not have checked.
RECENT_SECONDS = 900.0
#: The most hand-overs read in one pass.
BATCH = 50
#: How much earlier than it really did a process can seem to start: its start time is counted from the kernel's
#: boot time, which /proc gives in whole seconds. Anything that close to a hand-over may have followed it.
START_SLACK_SECONDS = 1.0
#: A duration as coreutils `timeout` takes it.
_DURATION = re.compile(r"^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$")
_UNIT = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def _seconds(text: str) -> float | None:
    found = _DURATION.match(text)
    return float(found.group(1)) * _UNIT[found.group(2)] if found else None


def bounded_until(argv: list[str], started_at: float) -> float | None:
    """When a coreutils `timeout` started at `started_at` stops its command at the latest; None when it is not one."""
    if not argv or Path(argv[0]).name != "timeout":
        return None
    args, extra = argv[1:], 0.0
    while args and args[0].startswith("-") and args[0] != "--":
        option = args.pop(0)
        if option in ("-k", "-s") and args:
            value = args.pop(0)
            extra += (_seconds(value) or 0.0) if option == "-k" else 0.0
        elif option.startswith("--kill-after="):
            extra += _seconds(option.split("=", 1)[1]) or 0.0
        elif option.startswith("-k") and len(option) > 2:
            extra += _seconds(option[2:]) or 0.0
    if args and args[0] == "--":
        args.pop(0)
    duration = _seconds(args[0]) if args else None
    if not duration or len(args) < 2:  # `timeout 0` never times out
        return None
    return started_at + duration + extra


def _argv(proc_root: Path, pid: int) -> list[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def _bounded(sample: Any, holder: background_child.Holder, proc_root: Path, now: float) -> bool:
    """Whether a `timeout` in the holder's session has a deadline still ahead of it."""
    started = dict(sample.starts)
    for pid, session in sample.sessions:
        if session == holder.session and started.get(pid, 0.0) > 0:
            deadline = bounded_until(_argv(proc_root, pid), started[pid])
            if deadline is not None and deadline > now:
                return True
    return False


def _rows(listener: Any, conn: Any, terminal: set[str]) -> list[tuple[int, float, str, str]]:
    """The hand-overs delivered since the last pass: (trace id, when, ticket, role)."""
    mark = getattr(listener, "handoff_children_mark", None)
    since = ("t.id > %s", (mark,)) if mark is not None else (
        "t.ts > clock_timestamp() - make_interval(secs => %s)", (RECENT_SECONDS,))
    rows = conn.execute(
        "SELECT t.id, extract(epoch FROM t.ts)::float8, t.ticket_id, t.target_role FROM ticket_board.notification_trace t "
        f"WHERE {since[0]} AND t.kind = 'transition' AND t.event = 'send' AND t.target_role <> 'director' "
        "AND t.ticket_id IS NOT NULL "
        "AND NOT (t.ticket_state_at_event = ANY(%s::text[])) ORDER BY t.id LIMIT %s",
        (*since[1], sorted(terminal), BATCH)).fetchall()
    found = [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in rows]
    if found:
        listener.handoff_children_mark = int(found[-1][0])
    elif mark is None:
        row = conn.execute("SELECT coalesce(max(id), 0) FROM ticket_board.notification_trace").fetchone()
        listener.handoff_children_mark = int(next(iter(row.values())) if isinstance(row, dict) else row[0])
    # Only each role's newest hand-over: what is current is judged now, so an older one -- re-read after a restart
    # -- would call the work of the ticket it handed over leftovers of earlier work.
    newest = {_text(role): (int(i), float(at), _text(ticket), _text(role)) for i, at, ticket, role in found}
    return sorted(newest.values())


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value or "")


def _present(listener: Any, conn: Any) -> bool:
    """Whether this board can record a hand-over report: a listener can be newer than its board's migrations."""
    present = getattr(listener, "_handoff_children_present", None)
    if present is None:
        row = conn.execute(
            "SELECT to_regprocedure('ticket_board.note_handoff_child(text, text, text, text)') IS NOT NULL").fetchone()
        value = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else False
        present = listener._handoff_children_present = value is True or value in ("t", b"t", 1)
    return present


def leftovers(gate: Any, conn: Any, role: str, sent_at: float, terminal: set[str],
              now: float) -> list[tuple[background_child.Holder, dict[str, Any] | None]]:
    """The sessions under `role`'s pane that were running at its hand-over and are no current or bounded work of its."""
    target = gate.role_targets.get(role)
    pane_pid = gate._pane_pid(target) if target else None
    if pane_pid is None:
        return []
    sample = gate._child_work_sample(pane_pid)
    if not sample.observed or not sample.detached:
        return []
    proc_root = Path(gate.proc_root)
    # The runtime's own: what its provider started before any turn could (SYRD-589).
    furniture_until = (gate.provider_started_at(target) or 0.0) + gate.LAUNCHER_SEED_STARTUP_SECONDS
    cutoff = sent_at - START_SLACK_SECONDS
    before = frozenset(pid for pid, started in sample.starts if 0 < started <= cutoff)
    holders = [holder for holder in background_child.describe(sample, sample.detached, before, proc_root=proc_root)
               # A group that cannot be read any more has ended since the sample: nothing survives to report.
               if holder.detached and holder.pgid > 0 and furniture_until < holder.started_at <= cutoff]
    origins = background_child.origins(conn, holders)
    return [(holder, origins.get(holder.key)) for holder in holders
            if (origins.get(holder.key) is None or background_child.lapsed(origins[holder.key], role, terminal))
            and not _bounded(sample, holder, proc_root, now)]


def run(listener: Any, conn: Any) -> int:
    """One pass; returns how many children the Director was newly told about."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "_child_work_sample"):
        return 0
    told = 0
    try:
        if not _present(listener, conn):
            return 0
        terminal = listener.activity_hold._terminal_states()
        handovers = _rows(listener, conn, terminal)
        if not handovers:
            return 0
        now = time.time()
        for _id, sent_at, ticket, role in handovers:
            identity = getattr(gate, "role_identities", {}).get(role)
            provider = getattr(identity, "pid", None)
            for holder, origin in leftovers(gate, conn, role, sent_at, terminal, now):
                ticks = background_child.start_ticks(Path(gate.proc_root), holder.pid)
                # Checked only against a leader that is still there; otherwise SYRD-583's stop, by hand.
                stop = (f"run `{background_child.checked_stop(holder.pgid, ticks)}`: it signals process group "
                        f"{holder.pgid} only while its leader is still this process, never the provider"
                        f"{f' (pid {provider})' if provider else ''}, and says whether it stopped"
                        ) if ticks and holder.pid == holder.pgid == holder.session else ""
                what = background_child.sentence(holder, origin, now=now, provider_pid=provider, stop=stop)
                row = conn.execute("SELECT ticket_board.note_handoff_child(%s, %s, %s, %s)",
                                   (role, ticket, holder.key, what)).fetchone()
                told += bool(next(iter(row.values())) if isinstance(row, dict) else row[0])
    except Exception as exc:  # A report is never worth a pass.
        listener.logger.warning("Failed to report background work left at a hand-over: %s", exc)
    return told
