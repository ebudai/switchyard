#!/usr/bin/env python3
"""Resolve a local socket peer to the durable role session that owns it.

SO_PEERCRED proves which process connected. It does not prove which role that
process belongs to, and `ticket-board-write` opens a fresh short-lived
connection per command, so binding authority to the connecting pid means the
binding evaporates the moment that helper exits (SYRD-39).

The durable identity is the pane process the role launcher started: every
command a role runs, however deeply nested, descends from it. Parentage is
maintained by the kernel and a process cannot rewrite its own ancestry, so the
pane root is derived here from /proc rather than taken from anything the caller
supplies -- environment, argv and headers are all writable by the caller.

Read the threat boundary in docs/ticket-board-service.md before relying on this
for isolation: it distinguishes role *panes*, which is strictly stronger than
trusting a role name off the wire, but every role in a project currently shares
one Unix uid and one tmux server, so it is not a substitute for uid separation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Walking up from a pane's leaf command reaches the pane root in a handful of
# hops; the bound only stops a cycle from hanging the board.
MAX_ANCESTRY_DEPTH = 128
PROC_ROOT = Path("/proc")
# The tmux server's comm is "tmux: server"; the 15-character /proc limit can
# truncate it, so match on the prefix rather than the whole string.
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
    """Resolve a peer pid to the pane process that owns it.

    The pane root is the ancestor whose parent is the tmux server: the launcher
    starts one such process per role, and everything the role runs descends from
    it. A peer that is not under a tmux server -- a service, a cron job, a
    developer shell -- has no pane identity and gets None; callers decide
    whether that is allowed rather than being silently granted a role here.
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
