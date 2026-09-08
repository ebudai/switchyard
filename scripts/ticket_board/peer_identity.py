"""Kernel-backed identity for a process running inside a tmux pane."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROC_ROOT = Path("/proc")
MAX_ANCESTRY_DEPTH = 128


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    start_time: int
    comm: str


@dataclass(frozen=True)
class SessionIdentity:
    pid: int
    start_time: int


def read_process(pid: int, *, proc_root: Path = PROC_ROOT) -> ProcessInfo | None:
    if pid <= 0:
        return None
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        close = raw.rindex(")")
        open_ = raw.index("(")
        fields = raw[close + 2 :].split()
        return ProcessInfo(
            pid=pid,
            ppid=int(fields[1]),
            start_time=int(fields[19]),
            comm=raw[open_ + 1 : close],
        )
    except (OSError, ValueError, IndexError):
        return None


def session_identity(pid: int, *, proc_root: Path = PROC_ROOT) -> SessionIdentity | None:
    """Return the pane root whose parent is the real tmux server process."""
    seen: set[int] = set()
    current = pid
    for _ in range(MAX_ANCESTRY_DEPTH):
        if current <= 1 or current in seen:
            return None
        seen.add(current)
        child = read_process(current, proc_root=proc_root)
        if child is None:
            return None
        parent = read_process(child.ppid, proc_root=proc_root)
        if parent is not None and parent.comm.startswith("tmux"):
            return SessionIdentity(child.pid, child.start_time)
        current = child.ppid
    return None


def session_is_live(identity: SessionIdentity, *, proc_root: Path = PROC_ROOT) -> bool:
    process = read_process(identity.pid, proc_root=proc_root)
    return process is not None and process.start_time == identity.start_time
