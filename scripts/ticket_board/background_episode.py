"""After a notice goes out past background work, one Director alert per episode, and only for a real one (SYRD-704).

On syrd, 2026-10-29, a notice for SYRD-703 went out past a finished turn's
waiter in App's pane (SYRD-583). The Director was told about the waiter, and then
in eight more notices about eight PostgreSQL processes a test under it had
started three seconds earlier. PostgreSQL puts each of its children in a session
of its own, so each was a holder of its own. All of them had exited before the
Director could look.

So what follows a delivery past background work is told by episode:

* **Still there.** Each holder is read again from /proc just before anything is
  recorded. It must be the same pid, started at the same moment, in the same
  process group, and not a zombie. One that has exited, or whose number now
  names another process, is left out.
* **One per command.** Holders are grouped under the outermost process above
  them that is outside the pane's own session: the shell the provider's tool
  started. That process is the episode, named by its process group and command,
  with the other process groups under it listed. Two commands the provider
  started stay two episodes.
* **Material.** An episode is told once it has run `MIN_AGE_SECONDS`, SYRD-616's
  threshold ("a short command can reach no deadline"). It is told at once when a
  deadline declared for any part of it has passed (a `switchyard-bounded`
  declaration, or its execution lease's deadline), when its ticket is no longer
  the role's (SYRD-583's lapsed lease), or when it waits on a `pgrep -f` that
  matches itself and so can never end.
* **Still true when it is delivered.** A queued alert is dropped at delivery when
  its process group has no live member left, or its number now names a process
  that started at another moment (`alert_is_current`).

Each episode is told once, through SYRD-583's per-child record, keyed by its
lead's process group and start. The delivery itself never depends on any of
this: a failure is logged and nothing else changes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import background_child, execution_lease as lease
from .execution_lease_pass import MIN_AGE_SECONDS

#: Where processes are read. A parameter everywhere, looked up when called, so a test can point it elsewhere.
PROC_ROOT = Path("/proc")
#: How far up the process tree an episode's lead is looked for.
MAX_DEPTH = 64
#: How many of an episode's other process groups its alert lists by name.
LISTED = 5


@dataclass(frozen=True)
class Process:
    pid: int
    state: str
    ppid: int
    pgid: int
    session: int
    #: Epoch seconds, computed as the activity gate computes it; 0.0 when unknown.
    started_at: float

    @property
    def live(self) -> bool:
        return self.state not in ("Z", "X", "x")


@dataclass(frozen=True)
class Episode:
    """One command's background work: its lead and the holders under it."""

    lead: background_child.Holder
    members: tuple[background_child.Holder, ...]

    @property
    def others(self) -> tuple[background_child.Holder, ...]:
        """The members in process groups other than the lead's."""
        seen: dict[int, background_child.Holder] = {}
        for holder in self.members:
            if holder.pgid and holder.pgid != self.lead.pgid:
                seen.setdefault(holder.pgid, holder)
        return tuple(seen.values())


class Clock:
    """The kernel's boot time and tick rate, read once."""

    def __init__(self, proc_root: Path) -> None:
        from .pane_activity_gate import _boot_time_epoch_seconds

        self.boot = _boot_time_epoch_seconds(proc_root)
        try:
            self.hz = float(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError, AttributeError):
            self.hz = 0.0

    def epoch(self, ticks: int) -> float:
        return self.boot + ticks / self.hz if self.boot > 0 and self.hz > 0 else 0.0


def read(proc_root: Path, pid: int, clock: Clock) -> Process | None:
    """One process as /proc has it now; None when it is not there."""
    try:
        stat = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2:].split() if ")" in stat else []
    try:
        return Process(pid, fields[0], int(fields[1]), int(fields[2]), int(fields[3]), clock.epoch(int(fields[19])))
    except (IndexError, ValueError):
        return None


def _same_start(first: float, second: float) -> bool:
    return abs(first - second) < 0.005


def still_there(holder: background_child.Holder, now: Process | None) -> bool:
    """Whether `now` is the process this holder was: live, in its group, started when it did."""
    if now is None or not now.live or (holder.pgid and now.pgid != holder.pgid):
        return False
    # A start that could not be read then cannot tell a reused number now; the pid and group still have to match.
    return holder.started_at <= 0 or _same_start(now.started_at, holder.started_at)


def _lead_pid(proc_root: Path, pid: int, pane_session: int | None, clock: Clock) -> int:
    """The outermost process above `pid` that is outside the pane's own session, `pid` itself included."""
    if not pane_session:
        return pid
    top, current = pid, pid
    for _ in range(MAX_DEPTH):
        process = read(proc_root, current, clock)
        if process is None or process.session == pane_session:
            break
        top = current
        if process.ppid <= 1:
            break
        current = process.ppid
    return top


def _holder(proc_root: Path, process: Process, finished_turn: bool) -> background_child.Holder:
    argv = background_child._argv(proc_root, process.pid)
    return background_child.Holder(
        session=process.session, pgid=process.pgid, pid=process.pid, started_at=process.started_at,
        command=" ".join(argv), cwd=background_child._cwd(proc_root, process.pid), finished_turn=finished_turn,
        argv=argv)


def episodes(holders: Iterable[background_child.Holder], *, proc_root: Path | None = None,
             pane_pid: int | None = None) -> list[Episode]:
    """The holders still there, grouped by the command they belong to, oldest first."""
    proc_root = PROC_ROOT if proc_root is None else proc_root
    clock = Clock(proc_root)
    pane = read(proc_root, pane_pid, clock) if pane_pid else None
    pane_session = pane.session if pane is not None else None
    alive = [holder for holder in holders if still_there(holder, read(proc_root, holder.pid, clock))]
    grouped: dict[int, list[background_child.Holder]] = {}
    for holder in alive:
        lead = holder.pid if not holder.detached else _lead_pid(proc_root, holder.pid, pane_session, clock)
        grouped.setdefault(lead, []).append(holder)
    found = []
    for lead_pid, members in grouped.items():
        lead = next((holder for holder in members if holder.pid == lead_pid), None)
        if lead is None:
            process = read(proc_root, lead_pid, clock)
            lead = _holder(proc_root, process, any(m.finished_turn for m in members)) if process and process.live \
                else min(members, key=lambda holder: (holder.started_at <= 0, holder.started_at, holder.pid))
        found.append(Episode(lead, tuple(members)))
    return sorted(found, key=lambda episode: (episode.lead.started_at, episode.lead.pid))


def _deadlines(conn: Any, keys: list[str]) -> list[float]:
    """The open execution leases' deadlines for these children (SYRD-616); none on a board without them."""
    try:
        if not background_child._one_value(
                conn.execute("SELECT to_regclass('ticket_board.execution_leases') IS NOT NULL").fetchone()):
            return []
        rows = conn.execute(
            "SELECT extract(epoch FROM deadline)::float8 FROM ticket_board.execution_leases "
            "WHERE child = ANY(%s::text[]) AND ended_at IS NULL", (keys,)).fetchall()
    except Exception:  # a connection that cannot answer: a declaration may still say
        return []
    return [float(next(iter(row.values())) if isinstance(row, dict) else row[0]) for row in rows]


def material(episode: Episode, *, now: float, conn: Any = None, lease_dir: Path | None = None,
             proc_root: Path | None = None, lapsed: bool = False) -> bool:
    """Whether this episode is worth the Director's attention: long enough, or past a deadline declared for it.

    Whatever its age, so is one whose ticket is no longer the role's -- SYRD-583's lapsed lease, which holds
    nothing -- and one that waits on itself and can never end."""
    lead = episode.lead
    parts = {holder.key: holder for holder in (lead, *episode.members)}
    # An unknown start is not a short one: nothing says it is.
    if lapsed or lead.started_at <= 0 or now - lead.started_at >= MIN_AGE_SECONDS:
        return True
    if any(holder.self_matching for holder in parts.values()):
        return True
    if conn is not None and any(deadline <= now for deadline in _deadlines(conn, sorted(parts))):
        return True
    if lease_dir is not None:
        proc_root = PROC_ROOT if proc_root is None else proc_root
        for holder in parts.values():
            ticks = lease.start_ticks(holder.pid, proc_root) if holder.session else 0
            declared = lease.declaration(lease_dir, holder.session, ticks, now=now) if ticks else None
            if declared is not None and declared["deadline"] <= now:
                return True
    return False


def others_clause(episode: Episode) -> str:
    """The episode's other process groups, which a signal to the lead's group does not reach."""
    others = episode.others
    if not others:
        return ""
    named = ", ".join(f"{holder.pgid} (`{holder.summary}`)" if holder.summary else str(holder.pgid)
                      for holder in others[:LISTED])
    more = f" and {len(others) - LISTED} more" if len(others) > LISTED else ""
    groups = ",".join(str(holder.pgid) for holder in others)
    return (f" Under it, in process groups of their own: {named}{more}; a signal to its group does not reach "
            f"them, so check them with `ps -o pid,pgid,lstart,args -g {groups}`.")


def tell(ledger: Any, conn: Any, notification_id: int, holders: Iterable[background_child.Holder],
         origins: dict[str, dict[str, Any]], *, gate: Any = None, role: str = "", provider_pid: int | None = None,
         terminal: Iterable[str] = (), now: float | None = None) -> int:
    """Tell the Director about each episode still there and material; how many were newly told. Never fatal."""
    try:
        now = time.time() if now is None else now
        proc_root = Path(getattr(gate, "proc_root", None) or PROC_ROOT)
        target = (getattr(gate, "role_targets", None) or {}).get(role)
        pane_pid_of = getattr(gate, "_pane_pid", None)
        pane_pid = pane_pid_of(target) if target and callable(pane_pid_of) else None
        state_store = getattr(gate, "state_store", None)
        lease_dir = lease.lease_dir(Path(state_store.state_dir)) if getattr(state_store, "state_dir", None) else None
        told = 0
        for episode in episodes(holders, proc_root=proc_root, pane_pid=pane_pid):
            origin = origins.get(episode.lead.key) or next(
                (origins[holder.key] for holder in episode.members if holder.key in origins), None)
            if not material(episode, now=now, conn=conn, lease_dir=lease_dir, proc_root=proc_root,
                            lapsed=background_child.lapsed(origin, role, terminal)):
                continue
            what = background_child.sentence(episode.lead, origin, now=now, provider_pid=provider_pid,
                                             under=others_clause(episode))
            told += background_child.tell(ledger, conn, notification_id, episode.lead, what)
        return told
    except Exception as exc:  # The delivery must never depend on its own bookkeeping.
        ledger.logger.warning("Failed to tell the Director about background work behind notification %s: %s",
                              notification_id, exc)
        return 0


def alert_is_current(payload: dict[str, Any], *, proc_root: Path | None = None) -> bool:
    """Whether a queued background-child alert still names a live process group; True when it cannot tell."""
    child = str(payload.get("child") or "")
    pgid_text, _, start_text = child.partition("@")
    try:
        pgid, started = int(pgid_text), int(start_text)
    except ValueError:
        return True
    if pgid <= 1:
        return True
    proc_root = PROC_ROOT if proc_root is None else proc_root
    clock = Clock(proc_root)

    def reported(process: Process | None) -> bool:
        return (process is not None and process.live and process.pgid == pgid
                and (started <= 0 or process.started_at <= 0 or int(process.started_at) == started))

    if reported(read(proc_root, pgid, clock)):
        return True
    try:
        entries = [int(entry.name) for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError:
        return True
    group = [process for process in (read(proc_root, pid, clock) for pid in entries)
             if process is not None and process.live and process.pgid == pgid]
    if any(map(reported, group)):
        return True
    # A group outlives its leader while any member does, and its number is not reused while it does. A live
    # process under that number that is not the one reported means the group reported has gone.
    return bool(group) and not any(process.pid == pgid for process in group)
