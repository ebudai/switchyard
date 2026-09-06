#!/usr/bin/env python3
"""Group a local socket peer's commands under the role session they belong to.

This is a DURABILITY mechanism, not an authorization boundary. Read
docs/ticket-board-service.md before relying on it for anything else.

The problem it solves: `ticket-board-write` opens a fresh short-lived connection
per command, so binding a caller role to the connecting pid binds it to a
process that exits immediately, and the role is unheld between commands. Walking
up to the pane process a role's commands descend from gives the board something
that lasts as long as the role does.

The problem it does NOT solve: the pane root is identified by looking for an
ancestor whose parent's comm begins with "tmux", and a process sets its own comm
with prctl(PR_SET_NAME). One prctl plus one fork manufactures a fresh unused
"pane" identity from inside any pane, with no tmux and no ptrace (SYRD-39).
Authenticating the real tmux server is not possible from here either:
/proc/<pid>/fd is unreadable across uids, and every input this module can read is
writable by any process sharing the tenant uid.

So this distinguishes roles that are not trying to lie. It does not withstand a
role that is. Role separation needs per-role Unix identities; see the minimum
isolation change in the service doc.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Walking up from a pane's leaf command reaches the pane root in a handful of
# hops; the bound only stops a cycle from hanging the board.
MAX_ANCESTRY_DEPTH = 128
PROC_ROOT = Path("/proc")
# The tmux server's comm is "tmux: server"; the 15-character /proc limit can
# truncate it, so match on the prefix rather than the whole string. comm is set
# by the process itself via prctl(PR_SET_NAME), so this is a label, not proof.
TMUX_SERVER_COMM_PREFIX = "tmux"


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    comm: str
    ppid: int
    # Start time in clock ticks since boot. Paired with the pid it survives pid
    # reuse, so a recycled pid cannot inherit a dead pane's authority.
    start_time: int


@dataclass(frozen=True)
class SessionIdentity:
    """The durable pane process a peer belongs to."""

    pid: int
    start_time: int
    comm: str

    def key(self) -> tuple[int, int]:
        return (self.pid, self.start_time)


def _stat_path(pid: int, proc_root: Path) -> Path:
    return proc_root / str(pid) / "stat"


def read_process(pid: int, *, proc_root: Path = PROC_ROOT) -> ProcessInfo | None:
    """Read one process's identity, or None when it is gone or unreadable."""
    if pid <= 0:
        return None
    try:
        raw = _stat_path(pid, proc_root).read_bytes().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    # comm is an arbitrary string in parentheses and may itself contain spaces
    # or parentheses, so split around the LAST ')' rather than tokenising.
    try:
        open_paren = raw.index("(")
        close_paren = raw.rindex(")")
    except ValueError:
        return None
    comm = raw[open_paren + 1 : close_paren]
    fields = raw[close_paren + 2 :].split()
    # After comm: state ppid pgrp session ... field 22 overall is starttime,
    # which is index 19 of this remainder.
    if len(fields) < 20:
        return None
    try:
        ppid = int(fields[1])
        start_time = int(fields[19])
    except ValueError:
        return None
    return ProcessInfo(pid=pid, comm=comm, ppid=ppid, start_time=start_time)


def process_ancestry(pid: int, *, proc_root: Path = PROC_ROOT) -> list[ProcessInfo]:
    """The chain from `pid` up towards init, nearest first."""
    chain: list[ProcessInfo] = []
    seen: set[int] = set()
    current = pid
    for _ in range(MAX_ANCESTRY_DEPTH):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        info = read_process(current, proc_root=proc_root)
        if info is None:
            break
        chain.append(info)
        current = info.ppid
    return chain


def session_identity(pid: int, *, proc_root: Path = PROC_ROOT) -> SessionIdentity | None:
    """Resolve a peer pid to the pane process it descends from.

    The pane root is taken to be the ancestor whose parent looks like the tmux
    server. "Looks like" is the whole caveat: the test is the parent's comm,
    which that parent controls, so this identifies an honest pane and is
    trivially forged by a dishonest one. A peer under no such ancestor gets
    None, and callers refuse it rather than defaulting it to a role.
    """
    chain = process_ancestry(pid, proc_root=proc_root)
    for info in chain:
        parent = read_process(info.ppid, proc_root=proc_root)
        if parent is None:
            continue
        if parent.comm.startswith(TMUX_SERVER_COMM_PREFIX):
            return SessionIdentity(pid=info.pid, start_time=info.start_time, comm=info.comm)
    return None


def session_is_live(identity: SessionIdentity, *, proc_root: Path = PROC_ROOT) -> bool:
    """Whether the pane process still exists as the same process."""
    info = read_process(identity.pid, proc_root=proc_root)
    return info is not None and info.start_time == identity.start_time
