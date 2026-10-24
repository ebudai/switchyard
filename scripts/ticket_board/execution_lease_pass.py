"""The listener's lease pass: watch long commands under each role's pane against a wall-clock deadline (SYRD-616).

Once per listener iteration, with one read of the process table for every
role, this looks under each registered role's pane (never the Director's) for
sessions of their own -- the unit a provider's tool starts a command in --
leaving out what the provider started while it was still starting up, before
any notice could start a turn (SYRD-589).

* **Leases.** A session is recorded once it has run `MIN_AGE_SECONDS`, or at
  once when `switchyard-bounded` declared it. Its ticket is the role's active
  work when first recorded, and its turn the pane hook's `turn_started_at`.
  Each look adds a little evidence (CPU ticks, bytes read and written, member
  count): evidence for a person, never proof of progress.
* **Expiry.** Past its deadline -- the default observation threshold from its
  start, a declaration, or the Director's extension -- the Director is told
  once for that deadline, with the commands to inspect, extend or interrupt
  it. Nothing is stopped: only `switchyard-bounded` enforces a hard deadline,
  on its own group.
* **Stale.** When the lease's ticket stops being the role's active work, the
  lease is stale at once and the Director is told once, through SYRD-583's
  per-child record. SYRD-583's lease reads the lease's ticket first
  (`background_child.origins`), so a stale command holds no later delivery.
* **Clearing.** A new turn clears an alert; an exit ends the lease and clears
  its alert; an extension or an interrupt request clears it in the database.
* **Interrupt.** The Director's request is carried out in steps across passes,
  never waiting inside one: SIGINT, then SIGTERM, then SIGKILL `INTERRUPT_GRACE
  _SECONDS` apart, to the process groups of the lease's session only, after
  checking its leader is still the process recorded. The role is told once,
  after, with the reason.

The pane's text is never read. Never fatal: a failure is logged, and the pass
goes on.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from . import background_child, execution_lease as lease

#: How long a session runs before it is recorded at all: a short command can reach no deadline.
MIN_AGE_SECONDS = 60.0
#: How often a recorded lease's evidence is refreshed.
OBSERVE_EVERY_SECONDS = 30.0
#: The wait between the steps of the Director's interrupt.
INTERRUPT_GRACE_SECONDS = 10.0
INTERRUPT_STEPS = (("requested", "int", signal.SIGINT), ("int", "term", signal.SIGTERM), ("term", "kill", signal.SIGKILL))
PRUNE_EVERY_SECONDS = 3600.0
#: A hard deadline is the wrapper's to enforce; only a command still running this long after it means the wrapper failed.
HARD_SLACK_SECONDS = 60.0


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else ("" if value is None else str(value))


def _one(conn: Any, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    result = conn.execute(sql, params)
    names = [column.name for column in result.description] if result.description else []
    return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in result.fetchall()]


def _present(listener: Any, conn: Any) -> bool:
    present = getattr(listener, "_execution_leases_present", None)
    if present is None:
        value = _one(conn, "SELECT to_regprocedure('ticket_board.observe_execution_lease(text, text, integer, integer, "
                           "bigint, timestamptz, text, text, timestamptz, timestamptz, text, boolean, jsonb)') IS NOT NULL")
        present = listener._execution_leases_present = value is True or value in ("t", b"t", 1)
    return present


def role_ticket(conn: Any, role: str, terminal: set[str]) -> str:
    """The role's active work: its serial focus, else its most recently changed open ticket; "" when none."""
    active = _one(conn, "SELECT active_ticket FROM ticket_board.serial_queue() WHERE implementer = %s "
                        "AND active_ticket <> '' LIMIT 1", (role,))
    if active:
        return _text(active)
    return _text(_one(conn, "SELECT id FROM ticket_board.tickets WHERE assignee = %s AND NOT (state = ANY(%s::text[])) "
                            "ORDER BY updated_at DESC LIMIT 1", (role, sorted(terminal))))


def still_role_work(conn: Any, role: str, ticket: str, terminal: set[str]) -> tuple[bool, dict[str, str]]:
    """Whether `ticket` is still `role`'s active work, and the ticket as the board has it."""
    rows = _rows(conn, "SELECT id, state, assignee FROM ticket_board.tickets WHERE id = %s", (ticket,))
    if not rows:
        return False, {"id": ticket, "state": "gone", "assignee": ""}
    found = {key: _text(rows[0][key]) for key in ("id", "state", "assignee")}
    if found["assignee"] != role or found["state"] in terminal:
        return False, found
    active = _one(conn, "SELECT active_ticket FROM ticket_board.serial_queue() WHERE implementer = %s "
                        "AND active_ticket <> '' LIMIT 1", (role,))
    return (not active or _text(active) == ticket), found


def _anchor(conn: Any, role: str, ticket: str, terminal: set[str]) -> str:
    """The ticket a notice about this lease hangs on: its own, else the role's work, else its last hand-over."""
    return ticket or role_ticket(conn, role, terminal) or _text(_one(
        conn, "SELECT ticket_id FROM ticket_board.notification_trace WHERE target_role = %s AND kind = 'transition' "
              "AND event = 'send' AND ticket_id IS NOT NULL ORDER BY id DESC LIMIT 1", (role,)))


def _io(proc_root: Path, pid: int) -> tuple[int, int]:
    read = write = 0
    try:
        for line in (proc_root / str(pid) / "io").read_text().splitlines():
            name, _, value = line.partition(":")
            if name == "rchar":
                read = int(value)
            elif name == "wchar":
                write = int(value)
    except (OSError, ValueError):
        pass
    return read, write


def evidence(table: list, session: int, proc_root: Path, now: float) -> dict[str, Any]:
    """One look at a session: its members, their CPU ticks, bytes read and written."""
    members = [row for row in table if len(row) > 3 and int(row[3]) == session]
    io = [_io(proc_root, int(row[0])) for row in members]
    return {"at": round(now, 1), "members": len(members), "cpu_ticks": sum(int(row[2]) for row in members),
            "read_bytes": sum(r for r, _ in io), "written_bytes": sum(w for _, w in io)}


def describe_evidence(points: list) -> str:
    points = [point for point in points if isinstance(point, dict)][-3:]
    if len(points) < 2:
        return "one look only"
    first, last = points[0], points[-1]
    span = max(1, int(float(last["at"]) - float(first["at"])))
    return (f"over the last {span} s: {int(last['cpu_ticks']) - int(first['cpu_ticks'])} CPU ticks, "
            f"{int(last['read_bytes']) - int(first['read_bytes'])} bytes read, "
            f"{int(last['written_bytes']) - int(first['written_bytes'])} bytes written, "
            f"{last['members']} processes (evidence only: silence is not a hang, output is not progress)")


def _when(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(epoch))


def _actions(lease_id: int, role: str) -> str:
    return (f"Nothing was stopped. Inspect it: `ticket-board-read leases --role {role}`; extend it: "
            f"`ticket-board-write extend-lease {lease_id} --minutes 30 --reason \"...\"`; interrupt only it: "
            f"`ticket-board-write interrupt-lease {lease_id} --reason \"...\"`; or leave it running")


def _epoch(value: Any) -> float:
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    from datetime import datetime

    return datetime.fromisoformat(_text(value)).timestamp()


def run(listener: Any, conn: Any) -> int:
    """One pass; returns how many alerts the Director was newly sent."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "_child_work_sample") or not getattr(gate, "role_targets", None):
        return 0
    told = 0
    try:
        if not _present(listener, conn):
            return 0
        from . import notify_listener as nl

        now = time.time()
        terminal = listener.activity_hold._terminal_states()
        table = list(gate.process_table_reader())
        proc_root = Path(gate.proc_root)
        directory = lease.lease_dir(Path(gate.state_store.state_dir))
        observe_seconds = lease.configured_seconds(lease.OBSERVE_ENV, lease.DEFAULT_OBSERVE_SECONDS)
        seen_at: dict[str, float] = listener.__dict__.setdefault("_execution_lease_seen", {})
        for role, target in sorted(gate.role_targets.items()):
            if role == "director":
                continue
            pane_pid = gate._pane_pid(target)
            if pane_pid is None:
                continue  # cannot tell what is gone from a pane we cannot see
            told += _role_pass(listener, conn, gate, nl, role, target, pane_pid, table, proc_root, directory, terminal,
                               observe_seconds, seen_at, now)
        if now - getattr(listener, "_execution_leases_pruned", 0.0) > PRUNE_EVERY_SECONDS:
            _one(conn, "SELECT ticket_board.prune_execution_leases()")
            listener._execution_leases_pruned = now
    except Exception as exc:  # The watch is never worth a pass.
        listener.logger.warning("Execution lease pass failed: %s", exc)
    return told


def _role_pass(listener: Any, conn: Any, gate: Any, nl: Any, role: str, target: str, pane_pid: int, table: list,
               proc_root: Path, directory: Path, terminal: set[str], observe_seconds: float,
               seen_at: dict[str, float], now: float) -> int:
    sample = nl.descendant_work_sample(pane_pid, table)
    provider_started = next((float(row[4]) for row in table if int(row[0]) == pane_pid and len(row) > 4), 0.0)
    furniture_until = provider_started + gate.LAUNCHER_SEED_SETTLE_SECONDS
    holders = {holder.key: holder for holder in background_child.describe(sample, sample.detached, (), proc_root=proc_root)
               if holder.detached and holder.pgid > 0 and holder.started_at > furniture_until}
    state = gate.state_store.read(target)
    turn = getattr(state, "turn_started_at", None) if state is not None else None
    open_leases = {_text(row["child"]): row for row in _rows(
        conn, "SELECT * FROM ticket_board.execution_leases WHERE role = %s AND ended_at IS NULL", (role,))}
    told = 0
    ticket_now: str | None = None
    for key, holder in holders.items():
        ticks = lease.start_ticks(holder.pid, proc_root)
        declared = lease.declaration(directory, holder.session, ticks, now=now) if ticks else None
        row = open_leases.get(key)
        if row is None and declared is None and now - holder.started_at < MIN_AGE_SECONDS:
            continue
        if row is None or now - seen_at.get(f"{role}:{key}", 0.0) >= OBSERVE_EVERY_SECONDS or declared is not None:
            if ticket_now is None:
                ticket_now = role_ticket(conn, role, terminal)
            deadline = declared["deadline"] if declared else holder.started_at + observe_seconds
            observed = _one(conn, "SELECT ticket_board.observe_execution_lease(%s, %s, %s, %s, %s, to_timestamp(%s), %s, "
                                  "%s, to_timestamp(%s), to_timestamp(%s), %s, %s, %s)",
                            (role, key, holder.pid, holder.pgid, ticks, holder.started_at, holder.summary,
                             ticket_now or None, turn, deadline, declared["source"] if declared else "default",
                             bool(declared and declared["hard"]), Jsonb(evidence(table, holder.session, proc_root, now))))
            row = observed if isinstance(observed, dict) else row
            seen_at[f"{role}:{key}"] = now
        if row is None:
            continue
        told += _decide(conn, row, holder, role, turn, terminal, directory, table, proc_root, now)
    for key, row in open_leases.items():
        if key not in holders:
            interrupt = row.get("interrupt") or {}
            if interrupt:
                _finish_interrupt(conn, row, role, terminal, "it ended")
            else:
                _one(conn, "SELECT ticket_board.end_execution_lease(%s, 'exited', %s)", (row["id"], "the process exited"))
            seen_at.pop(f"{role}:{key}", None)
    return told


def _decide(conn: Any, row: dict[str, Any], holder: background_child.Holder, role: str, turn: float | None,
            terminal: set[str], directory: Path, table: list, proc_root: Path, now: float) -> int:
    lease_id = int(row["id"])
    if row.get("interrupt"):
        _step_interrupt(conn, row, holder, role, terminal, directory, table, proc_root, now)
        return 0
    ticket = _text(row.get("ticket_id"))
    if ticket and _text(row.get("state")) != "stale":
        current, found = still_role_work(conn, role, ticket, terminal)
        if not current:
            owner = found["assignee"] or "unassigned"
            what = (f"lease {lease_id}: pid {holder.pid} (process group {holder.pgid}), started "
                    f"{_when(holder.started_at)} for {ticket}, which is now {found['state']} "
                    f"({'unassigned' if owner == 'unassigned' else 'assigned to ' + owner}): `{_text(row['command'])}`"
                    f"{' in ' + holder.cwd if holder.cwd else ''}. It no longer holds {role}'s later work. "
                    f"Nothing was stopped. To end only it: `ticket-board-write interrupt-lease {lease_id} --reason \"...\"`")
            return int(bool(_one(conn, "SELECT ticket_board.note_execution_lease_alert(%s, 'stale', %s, %s)",
                                 (lease_id, ticket, what))))
    if turn is not None and row.get("turn_started_at") is not None and abs(_epoch(row["turn_started_at"]) - turn) > 0.001:
        _one(conn, "SELECT ticket_board.note_execution_lease_turn(%s, to_timestamp(%s))", (lease_id, turn))
    deadline = _epoch(row["deadline"])
    # Once per deadline: the database says whether this one was already told (note_execution_lease_alert).
    if now < deadline + (HARD_SLACK_SECONDS if row.get("hard") else 0.0) or _text(row.get("state")) == "stale":
        return 0
    anchor = _anchor(conn, role, ticket, terminal)
    source = {"default": "the default observation threshold", "declared": "a budget it declared",
              "hard": "its own hard deadline", "extended": "the Director's extension"}.get(_text(row["deadline_source"]), "")
    what = (f"lease {lease_id}{' on ' + ticket if ticket else ''}: pid {holder.pid} (process group {holder.pgid}), running "
            f"{background_child._elapsed(now - holder.started_at)} since {_when(holder.started_at)}, past {source} at "
            f"{_when(deadline)}: `{_text(row['command'])}`{' in ' + holder.cwd if holder.cwd else ''}. Activity "
            f"{describe_evidence(list(row.get('evidence') or []))}. {_actions(lease_id, role)}")
    return int(bool(anchor and _one(conn, "SELECT ticket_board.note_execution_lease_alert(%s, 'expired', %s, %s)",
                                    (lease_id, anchor, what))))


def _session_groups(table: list, session: int, proc_root: Path) -> set[int]:
    return {background_child._process_group(proc_root, int(row[0])) for row in table
            if len(row) > 3 and int(row[3]) == session} - {0}


def _step_interrupt(conn: Any, row: dict[str, Any], holder: background_child.Holder, role: str, terminal: set[str],
                    directory: Path, table: list, proc_root: Path, now: float) -> None:
    interrupt = dict(row["interrupt"])
    stage = _text(interrupt.get("stage"))
    if stage != "requested" and now - _epoch(interrupt.get("stage_at")) < INTERRUPT_GRACE_SECONDS:
        return
    step = next(((after, sig) for before, after, sig in INTERRUPT_STEPS if before == stage), None)
    if step is None:
        return  # SIGKILL was sent; the lease ends when the session is gone
    if lease.start_ticks(holder.pid, proc_root) != int(row["start_ticks"]):
        _finish_interrupt(conn, row, role, terminal, "its leader was gone before any signal")
        return
    if stage == "requested":
        lease.mark_interrupt(directory, holder.session, int(row["start_ticks"]),
                             actor=_text(interrupt.get("requested_by")) or "Director", reason=_text(interrupt.get("reason")))
    after, sig = step
    for pgid in _session_groups(table, holder.session, proc_root):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    _one(conn, "SELECT ticket_board.record_execution_lease_interrupt(%s, %s, %s, NULL, NULL)",
         (row["id"], after, signal.Signals(sig).name))


def _finish_interrupt(conn: Any, row: dict[str, Any], role: str, terminal: set[str], how: str) -> None:
    interrupt = dict(row.get("interrupt") or {})
    sent = {"requested": "no signal", "int": "SIGINT", "term": "SIGINT, then SIGTERM",
            "kill": "SIGINT, SIGTERM, then SIGKILL"}.get(_text(interrupt.get("stage")), "")
    by = _text(interrupt.get("requested_by")) or "Director"
    notice = (f"Your command `{_text(row['command'])}` (pid {row['pid']}, process group {row['pgid']}) was interrupted by "
              f"the {by}: {_text(interrupt.get('reason'))}. It was sent {sent}, and {how}. Nothing else was touched.")
    _one(conn, "SELECT ticket_board.record_execution_lease_interrupt(%s, 'done', %s, %s, %s)",
         (row["id"], f"{sent}; {how}", _anchor(conn, role, _text(row.get("ticket_id")), terminal) or None, notice))
