"""Background work the Director has authorized raises no background-work alert while that authorization stands (SYRD-708).

On syrd, 2026-10-30, App ran one declared, bounded 350-suite comparison for
SYRD-706 as process group 2036002 (lease 394). The Director extended the lease
at 08:53 through 10:23, after the lease's own expiry alert, and snoozed
SYRD-706's reminders through 10:30 with an instruction not to poll the pane.
At 09:15 the board told the Director anyway that App's pane had held one
SYRD-706 notice for 31 minutes behind that same process, and named sixteen
PostgreSQL processes the comparison had started seconds before.

What authorizes a process here:

* **An extension.** The process is under an open execution lease, its own or
  the nearest one above it in the process tree, matched by pid and start tick,
  so a replacement or a reused number is never covered. That lease is still
  running, has no interrupt asked of it, and its deadline is the Director's
  (`extended`) and has not passed. Its ticket is still the role's work.
* **A snooze.** The ticket the process works for (its lease's, or the git
  worktree it runs in) has an active reminder snooze, as the board itself
  decides (`ticket_reminders_snoozed`, which also ends when the ticket moves).

`covered` asks this of every holder: one that is not authorized, such as a
genuinely new command or a process that has exited, leaves the alert as it was.
A held-notice alert also needs the held notice to be for the authorized ticket,
so an unrelated notice held behind the same work is still told.

An alert already queued when the authorization begins is held back, not
dropped (`hold_alert`, asked at claim and before the send): it keeps its one-time
record, goes out once the authorization ends, and is withdrawn or dropped as
before if the hold or the process ends first.

Nothing here delivers or stops anything. When the authorization lapses,
expires, or the durable record no longer agrees, alerting is exactly what it
was, and since the hold's own clock never stopped, an alert that is due goes
at once. Never fatal: a failure reads as "not authorized".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import background_child, background_episode

#: Where processes are read; looked up when called, so a test can point it elsewhere.
PROC_ROOT = Path("/proc")
#: How far up the process tree a lease is looked for.
MAX_DEPTH = 64
#: What ends a ticket, when a caller has no workflow to say.
TERMINAL_STATES = frozenset({"done", "cancelled"})


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else ("" if value is None else str(value))


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    result = conn.execute(sql, params)
    names = [column.name for column in result.description] if getattr(result, "description", None) else []
    return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in result.fetchall()]


def _present(conn: Any, sql: str) -> bool:
    row = conn.execute(sql).fetchone()
    return background_child._one_value(row)


def _ancestry(proc_root: Path, pid: int) -> list[tuple[int, int]]:
    """(pid, start tick) of `pid` and each process above it, nearest first, as far as /proc can tell."""
    chain: list[tuple[int, int]] = []
    current = pid
    for _ in range(MAX_DEPTH):
        try:
            stat = (proc_root / str(current) / "stat").read_text(encoding="utf-8", errors="replace")
            fields = stat[stat.rfind(")") + 2:].split()
            state, ppid, ticks = fields[0], int(fields[1]), int(fields[19])
        except (OSError, IndexError, ValueError):
            break
        if state in ("Z", "X", "x"):
            break
        chain.append((current, ticks))
        if ppid <= 1:
            break
        current = ppid
    return chain


def _leases(conn: Any, role: str) -> dict[tuple[int, int], dict[str, Any]]:
    """This role's open execution leases by (pid, start tick); none on a board without them."""
    if not _present(conn, "SELECT to_regclass('ticket_board.execution_leases') IS NOT NULL"):
        return {}
    rows = _rows(conn, "SELECT id, pid, start_ticks, ticket_id, deadline_source, state, interrupt IS NOT NULL AS interrupt, "
                       "deadline > clock_timestamp() AS live, to_char(deadline, 'YYYY-MM-DD HH24:MI:SS TZ') AS deadline "
                       "FROM ticket_board.execution_leases WHERE role = %s AND ended_at IS NULL", (role,))
    return {(int(row["pid"]), int(row["start_ticks"])): row for row in rows}


class _Board:
    """The durable facts one question needs, each read at most once."""

    def __init__(self, conn: Any, role: str, terminal: Iterable[str]) -> None:
        self.conn, self.role, self.terminal = conn, role, set(terminal) or set(TERMINAL_STATES)
        self.leases = _leases(conn, role)
        self._tickets: dict[str, dict[str, Any] | None] = {}
        self._snoozed: dict[str, bool] = {}
        self._snooze_present: bool | None = None

    def ticket(self, ticket: str) -> dict[str, Any] | None:
        if ticket not in self._tickets:
            rows = _rows(self.conn, "SELECT id, state, assignee FROM ticket_board.tickets WHERE id = %s", (ticket,))
            self._tickets[ticket] = {key: _text(value) for key, value in rows[0].items()} if rows else None
        return self._tickets[ticket]

    def still_the_roles(self, ticket: str) -> bool:
        found = self.ticket(ticket)
        return found is not None and not background_child.lapsed(found, self.role, self.terminal)

    def snoozed(self, ticket: str) -> bool:
        if self._snooze_present is None:
            self._snooze_present = _present(
                self.conn, "SELECT to_regprocedure('ticket_board.ticket_reminders_snoozed(text,timestamptz)') IS NOT NULL")
        if not self._snooze_present:
            return False
        if ticket not in self._snoozed:
            self._snoozed[ticket] = background_child._one_value(self.conn.execute(
                "SELECT ticket_board.ticket_reminders_snoozed(%s, clock_timestamp())", (ticket,)).fetchone())
        return self._snoozed[ticket]


def _authority(board: _Board, holder: background_child.Holder, ticket: str | None, proc_root: Path) -> str:
    """What authorizes this holder for `ticket` (any ticket when None), or ""."""
    if not background_episode.still_there(holder, background_episode.read(proc_root, holder.pid,
                                                                           background_episode.Clock(proc_root))):
        return ""  # its identity is lost: whatever runs under that number now was never authorized
    lease = next((board.leases[link] for link in _ancestry(proc_root, holder.pid) if link in board.leases), None)
    if lease is not None:
        work = _text(lease.get("ticket_id"))
        if (_text(lease.get("deadline_source")) == "extended" and lease.get("live") is True
                and _text(lease.get("state")) == "running" and lease.get("interrupt") is not True
                and work and (ticket is None or work == ticket) and board.still_the_roles(work)):
            return f"lease {lease['id']} on {work}, extended by the Director until {_text(lease['deadline'])}"
    else:
        work = next(iter(holder.candidate_tickets()), "")
    if work and (ticket is None or work == ticket) and board.snoozed(work):
        return f"{work}'s reminders are snoozed"
    return ""


def covered(conn: Any, role: str, holders: Iterable[background_child.Holder], *, ticket: str | None = None,
            terminal: Iterable[str] = (), proc_root: Path | None = None) -> str:
    """Why every holder's background work is authorized for `ticket` right now, or "" when any is not.

    `ticket` is the held notice's, for a held-notice alert; None asks only about the work itself.
    """
    holders = list(holders)
    if not holders or not role:
        return ""
    proc_root = PROC_ROOT if proc_root is None else proc_root
    try:
        board = _Board(conn, role, terminal)
        reasons: list[str] = []
        for holder in holders:
            reason = _authority(board, holder, ticket, proc_root)
            if not reason:
                return ""
            if reason not in reasons:
                reasons.append(reason)
        return "; ".join(reasons)
    except Exception:  # an alert that could not be checked is told, as before
        return ""


#: How long a queued alert about authorized work waits before it is asked about again.
REQUEUE_SECONDS = 60.0
#: The requeue error a held alert carries: not `owner_background_work`, which would note a hold of its own.
HELD_ERROR = "background_work_authorized"


def _held_tickets(conn: Any, parsed: dict[str, Any]) -> list[str]:
    """Every ticket a held-notice alert's episode may hold now; its own, too.

    The listener cannot read the episode, so this is read from the trace it can:
    the notices the alert names, and every notice of that role's the gate has
    deferred since the episode began -- one held after the alert was queued
    included. More than the episode holds only ever means more to authorize.
    """
    tickets = [str(parsed.get("id") or "")]
    ids = [int(value) for value in parsed.get("notification_ids") or () if str(value).isdigit()]
    if ids:
        tickets += [_text(row["ticket_id"]) for row in _rows(
            conn, "SELECT DISTINCT ticket_id FROM ticket_board.notification_trace WHERE notification_id = ANY(%s) "
                  "AND ticket_id IS NOT NULL", (ids,))]
    if parsed.get("held_since") and parsed.get("role"):
        tickets += [_text(row["ticket_id"]) for row in _rows(
            conn, "SELECT DISTINCT ticket_id FROM ticket_board.notification_trace WHERE target_role = %s "
                  "AND event = 'gate_defer' AND ts >= %s::timestamptz AND ticket_id IS NOT NULL",
            (str(parsed["role"]), str(parsed["held_since"])))]
    return sorted({ticket for ticket in tickets if ticket})


def _child_holder(child: str, proc_root: Path) -> background_child.Holder | None:
    """The process a background-child alert's key names, if it is still that process."""
    pgid_text, _, start_text = child.partition("@")
    try:
        pgid, started = int(pgid_text), int(start_text)
    except ValueError:
        return None
    process = background_episode.read(proc_root, pgid, background_episode.Clock(proc_root))
    if process is None or not process.live or process.pgid != pgid or int(process.started_at) != started:
        return None
    return background_child.Holder(session=process.session, pgid=pgid, pid=pgid, started_at=process.started_at)


def alert_authority(gate: Any, conn: Any, parsed: dict[str, Any], *, terminal: Iterable[str] = (),
                    proc_root: Path | None = None) -> str:
    """Why a queued alert to the Director is about work authorized right now, or "".

    A held-notice alert (SYRD-557): every holder the gate last saw under that
    role's pane must be authorized for every ticket its episode held. A
    background-child alert (SYRD-583, SYRD-636): the process its key names,
    still that process, must be authorized.
    """
    kind = str(parsed.get("kind") or "")
    role = str(parsed.get("role") or "")
    proc_root = Path(getattr(gate, "proc_root", None) or PROC_ROOT) if proc_root is None else proc_root
    try:
        if kind == "background_hold" and not parsed.get("cleared"):
            target = (getattr(gate, "role_targets", None) or {}).get(role)
            child_holders = getattr(gate, "child_holders", None)
            holders = list(child_holders(target)) if target and callable(child_holders) else []
            tickets = _held_tickets(conn, parsed)
            reasons = [covered(conn, role, holders, ticket=ticket, terminal=terminal, proc_root=proc_root)
                       for ticket in tickets]
            return "; ".join(dict.fromkeys(reasons)) if reasons and all(reasons) else ""
        if kind == "background_child":
            holder = _child_holder(str(parsed.get("child") or ""), proc_root)
            return covered(conn, role, [holder], terminal=terminal, proc_root=proc_root) if holder else ""
    except Exception:  # an alert that could not be checked is delivered, as before
        return ""
    return ""


def hold_alert(listener: Any, conn: Any, notification_id: int, attempts: int, kind: str, target_role: str,
               payload: str, phase: str) -> bool:
    """Requeue a queued alert to the Director about work that is authorized now; True if it was.

    Asked at claim and again just before the send. The alert is not dropped: it
    keeps its one-time record, goes out once the authorization ends, and is
    withdrawn or dropped as before if the hold or the process ends first.
    """
    if target_role != "director":
        return False
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except ValueError:
        return False
    if not isinstance(parsed, dict) or parsed.get("kind") not in ("background_hold", "background_child"):
        return False
    gate = getattr(getattr(listener, "activity_gate", None), "__self__", None)
    terminal_states = getattr(getattr(listener, "activity_hold", None), "_terminal_states", None)
    why = alert_authority(gate, conn, parsed, terminal=terminal_states() if callable(terminal_states) else ())
    if not why:
        return False
    listener.logger.info("Holding %s notification %s at %s: the work it is about is authorized (%s)",
                         kind, notification_id, phase, why)
    listener.ledger.requeue(conn, notification_id, attempts, f"{HELD_ERROR} ({phase}): {why}"[:500],
                            delay_seconds=REQUEUE_SECONDS)
    return True
