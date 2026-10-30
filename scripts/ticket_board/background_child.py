"""What is holding a role's pane, whose ticket started it, and how to stop only it (SYRD-583).

On syrd a shell from completed SYRD-579 ran for 18 h under App's provider:

    while pgrep -f "sweep.sh .../syrd-579-app" >/dev/null; do sleep 20; done

Its own command line carried the pattern, so it could never end, and SYRD-581's
handoff waited behind it as "pane busy" until the Director stopped that one
process group by hand. Three things were missing, and this module holds the
parts of each that are not the gate's sampling or the listener's decision:

* **What the work is.** A `Holder` is one detached session under the pane --
  the unit a provider's tool starts a command in -- with its process group, its
  oldest member, when that started, its command line and its working directory.
* **Whose ticket started it.** Ticket worktrees are named after their ticket
  (`syrd-579-app`), and commands name the paths they work on, so a ticket id is
  read from the directory first and the command second -- but only from a
  worktree git itself registered, never from a name alone: the VM kit's
  `~/syrd-248-vm` is SYRD-248's name, not its worktree (SYRD-636). Only an id
  the board actually has counts.
* **Whether that ticket is still the role's.** A child whose ticket has gone to
  another role, or finished, is a lease that has lapsed: it may not hold that
  role's later work. Nothing here stops it -- the Director is told how to.

Never fatal: a failure to describe or record is logged, and delivery goes on
exactly as it would have without it.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

#: A ticket id as worktrees spell it: `syrd-579`, `PGU-801`.
_TICKET_ID = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{0,15})-(\d{1,7})(?!\d)")
#: An absolute path in a command line, up to whatever ends a shell word.
_PATH = re.compile(r"""(?<![\w.~-])(/[^\s"';|&()<>`]+)""")
#: A `pgrep` with `-f` in its options, and its pattern.
_PGREP_F = re.compile(r"""\bpgrep((?:\s+-[A-Za-z]+)+)\s+(?:--\s+)?(?:"([^"]*)"|'([^']*)'|([^\s;|&)]+))""")
#: What a ticket's holder is called once its ticket is no longer the role's.
COMPLETED_TICKET_CHILD = "completed_ticket_child"


@dataclass(frozen=True)
class Holder:
    """One detached session under a pane, holding its delivery."""

    session: int
    pgid: int
    #: The session's oldest member the gate saw: normally its leader, the shell
    #: the provider's tool started.
    pid: int
    #: Epoch seconds; 0.0 when the kernel's start time could not be read.
    started_at: float
    #: The command line, raw, for judging it here (its ticket, an endless wait). Never written anywhere: what is
    #: recorded or told is `summary` (SYRD-616).
    command: str = ""
    cwd: str = ""
    #: Started before the runtime's own trusted turn end: a finished turn's.
    finished_turn: bool = False
    #: Whether the session is its own, apart from the pane's. Only then is its
    #: process group surely not the provider's.
    detached: bool = True
    #: The raw argv, when read from /proc; the summary is made from it.
    argv: tuple[str, ...] = ()
    #: Whether it never ends by itself, as judged on the raw command line -- kept for a holder read back from a
    #: record, which holds only the summary.
    endless: bool | None = None

    @property
    def summary(self) -> str:
        """What may be recorded of its command line (`execution_lease.sanitize_command`)."""
        from .execution_lease import sanitize_command

        return sanitize_command(list(self.argv) or self.command.split(), cwd=self.cwd)

    @property
    def key(self) -> str:
        """The child, never a reused number: its process group and when it started."""
        return f"{self.pgid or self.pid}@{int(self.started_at)}"

    def candidate_tickets(self) -> list[str]:
        """Ticket ids of the worktrees this child is in, or names: its directory's first, then its command's."""
        found: list[str] = []
        for path in [self.cwd] + _PATH.findall(self.command or ""):
            ticket = worktree_ticket(path)
            if ticket and ticket not in found:
                found.append(ticket)
        return found

    @property
    def self_matching(self) -> bool:
        """Whether it waits on a `pgrep -f` whose pattern matches its own command line.

        Such a loop always finds itself, so it never ends by itself. A pattern
        written not to match itself (`[s]weep`) is not flagged.
        """
        if self.endless is not None:
            return self.endless
        for match in _PGREP_F.finditer(self.command):
            pattern = next((group for group in match.groups()[1:] if group is not None), "")
            if "f" not in match.group(1).replace("-", "") or not pattern or pattern.startswith("$"):
                continue
            try:
                if re.search(pattern, self.command):
                    return True
            except re.error:
                continue
        return False

    def recovery(self, provider_pid: int | None = None) -> str:
        """The one stop that ends this child and nothing else."""
        keep = f" (pid {provider_pid})" if provider_pid else ""
        if self.detached and self.pgid > 1 and self.pgid == self.session:
            return (f"check it with `ps -o pid,pgid,lstart,args -g {self.pgid}`, then stop only that process group: "
                    f"`kill -TERM -- -{self.pgid}`. That ends this child and its own children, never the provider{keep}")
        return (f"check it with `ps -o pid,pgid,lstart,args -p {self.pid}`, then stop only that process: "
                f"`kill -TERM {self.pid}`. Its process group is the pane's own, so never stop the group or the provider{keep}")

    def as_detail(self) -> dict[str, Any]:
        """The holder as a record keeps it: its summary, never its raw command line (SYRD-616)."""
        detail = {key: value for key, value in asdict(self).items() if key not in ("argv", "endless")}
        return detail | {"command": self.summary, "endless": self.self_matching}

    @classmethod
    def from_detail(cls, raw: Any) -> "Holder | None":
        if not isinstance(raw, dict):
            return None
        try:
            endless = raw.get("endless")
            return cls(int(raw["session"]), int(raw["pgid"]), int(raw["pid"]), float(raw["started_at"]),
                       str(raw.get("command") or ""), str(raw.get("cwd") or ""), bool(raw.get("finished_turn")),
                       bool(raw.get("detached", True)), endless=endless if isinstance(endless, bool) else None)
        except (KeyError, TypeError, ValueError):
            return None


def worktree_ticket(path: str) -> str:
    """The ticket id of the git worktree `path` is in, or "" when it is in none.

    A worktree is a directory whose `.git` file names a registration whose own
    `gitdir` names that file back: what `git worktree add` writes. A clone, a
    copied worktree and a plain directory named like a ticket are not one.
    """
    if not path.startswith("/"):
        return ""
    for directory in (Path(path), *Path(path).parents):
        dot_git = directory / ".git"
        try:
            pointer = dot_git.read_text(encoding="utf-8", errors="replace")[:4096] if dot_git.is_file() else ""
        except OSError:
            continue
        if not pointer.startswith("gitdir: "):
            continue
        registration = Path(pointer[len("gitdir: "):].strip())
        try:
            back = (registration / "gitdir").read_text(encoding="utf-8", errors="replace")[:4096].strip()
        except OSError:
            return ""
        if back != str(dot_git):
            return ""
        found = _TICKET_ID.search(directory.name)
        return f"{found.group(1).upper()}-{int(found.group(2))}" if found else ""
    return ""


def checked_stop(pgid: int, start_ticks: int) -> str:
    """A stop that signals the process group only while its leader is still the one reported, and says what it did.

    One `sh -c` with no backslash and no inner single quote, so bash, zsh and
    fish pass it on alike. The leader must still have started at the same tick:
    a pid and its start time are one process, and a session leader keeps its
    group, so a number since reused by anything -- the provider included -- is
    never signalled. Whether it stopped counts only members that are not
    zombies: one orphaned to a parent that reaps late has stopped (SYRD-636).
    """
    return (f"sh -c 'g={pgid}; s=$(sed \"s/.*) //\" /proc/$g/stat 2>/dev/null | cut -d\" \" -f20); "
            f"if [ \"$s\" != \"{start_ticks}\" ]; then echo \"process group $g is not the one reported "
            "(gone, or its number reused): nothing sent\"; exit 1; fi; kill -s TERM -- -$g; n=0; "
            "while ps -e -o pgid= -o stat= | grep -q \"^ *$g [^Z]\" && [ $n -lt 10 ]; do sleep 1; n=$((n+1)); done; "
            "if ps -e -o pgid= -o stat= | grep -q \"^ *$g [^Z]\"; then echo \"process group $g is still running\"; "
            "exit 1; fi; "
            "echo \"process group $g stopped\"'")


def start_ticks(proc_root: Path, pid: int) -> int:
    """When `pid` started, in the kernel's clock ticks since boot; 0 when that cannot be read."""
    stat = _read(proc_root / str(pid) / "stat").decode("utf-8", errors="replace")
    fields = stat[stat.rfind(")") + 2:].split() if ")" in stat else []
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return 0


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _process_group(proc_root: Path, pid: int) -> int:
    stat = _read(proc_root / str(pid) / "stat").decode("utf-8", errors="replace")
    fields = stat[stat.rfind(")") + 2:].split() if ")" in stat else []
    try:
        return int(fields[2])
    except (IndexError, ValueError):
        return 0


def _argv(proc_root: Path, pid: int) -> tuple[str, ...]:
    raw = _read(proc_root / str(pid) / "cmdline")
    return tuple(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)


def _cwd(proc_root: Path, pid: int) -> str:
    try:
        return os.readlink(proc_root / str(pid) / "cwd")
    except OSError:
        return ""


def describe(sample: Any, pids: Iterable[int], finished: Iterable[int], *, proc_root: Path) -> tuple[Holder, ...]:
    """The holders behind `pids`: one per detached session, and one per process in the pane's own.

    `sample` is the gate's `ChildWorkSample`; `finished` the pids it judged a
    finished turn's.
    """
    session_of = dict(getattr(sample, "sessions", ()) or ())
    started = dict(sample.starts)
    finished = frozenset(finished)
    groups: dict[tuple[str, int], list[int]] = {}
    for pid in sorted(set(pids)):
        if pid in sample.detached and pid in session_of:
            groups.setdefault(("session", session_of[pid]), []).append(pid)
        else:
            groups.setdefault(("process", pid), []).append(pid)
    holders = []
    for (kind, ident), members in sorted(groups.items()):
        # The leader if it is still there, else the member that started first.
        known = [pid for pid in members if started.get(pid, 0.0) > 0]
        root = ident if kind == "session" and ident in members else min(
            known or members, key=lambda pid: (started.get(pid, 0.0), pid))
        holders.append(Holder(
            session=session_of.get(root, 0), pgid=_process_group(proc_root, root), pid=root,
            started_at=min((started[pid] for pid in known), default=0.0),
            command=" ".join(argv := _argv(proc_root, root)), cwd=_cwd(proc_root, root),
            finished_turn=bool(set(members) & finished), detached=kind == "session", argv=argv))
    return tuple(holders)


def _elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {rest // 60} min" if hours else f"{rest // 60} min {rest % 60} s"


def sentence(holder: Holder, origin: dict[str, Any] | None, *, now: float | None = None,
             provider_pid: int | None = None, stop: str = "", under: str = "") -> str:
    """What the Director is told about one holder; `stop`, when given, is the way to end it.

    `under`, when given, names what else runs under it (SYRD-704)."""
    now = time.time() if now is None else now
    running = (f"running for {_elapsed(now - holder.started_at)}, since "
               f"{time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(holder.started_at))}"
               if holder.started_at > 0 else "start time unknown")
    if origin:
        owner = origin.get("assignee") or "unassigned"
        whose = (f"started for {origin['id']}, which is now {origin['state']} "
                 f"({'unassigned' if owner == 'unassigned' else 'assigned to ' + owner})")
    else:
        whose = "its ticket is not known"
    turn = "left running by a finished turn" if holder.finished_turn else "started since the runtime's last turn ended"
    command = f": `{holder.summary}`" if holder.summary else ""
    where = f" in {holder.cwd}" if holder.cwd else ""
    endless = (" It waits on a `pgrep -f` whose pattern matches its own command line, so it can never end by itself."
               if holder.self_matching else "")
    return (f"pid {holder.pid} (process group {holder.pgid or 'unknown'}), {running}, {turn}, {whose}{command}{where}.{endless}{under} "
            f"Nothing was stopped. To end it: {stop or holder.recovery(provider_pid)}.")


def _lease_tickets(conn: Any, holders: list[Holder]) -> dict[str, str]:
    """The ticket each holder's execution lease was recorded for (SYRD-616), by holder key; never fatal."""
    try:
        if not _one_value(conn.execute("SELECT to_regclass('ticket_board.execution_leases') IS NOT NULL").fetchone()):
            return {}
        rows = conn.execute("SELECT child, ticket_id FROM ticket_board.execution_leases WHERE child = ANY(%s::text[]) "
                            "AND ticket_id IS NOT NULL", ([holder.key for holder in holders],)).fetchall()
    except Exception:  # an older board, or a connection that cannot answer: the worktree still can
        return {}
    found = {}
    for row in rows:
        values = [row["child"], row["ticket_id"]] if isinstance(row, dict) else list(row)[:2]
        child, ticket = (value.decode() if isinstance(value, bytes) else str(value) for value in values)
        found[child] = ticket
    return found


def _one_value(row: Any) -> bool:
    value = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else False
    return value is True or value in ("t", b"t", 1)


def origins(conn: Any, holders: Iterable[Holder]) -> dict[str, dict[str, Any]]:
    """The board's record of each ticket the holders were started for, by holder key; holders with none are absent.

    A holder's execution lease names the ticket that was its role's active work
    when it was recorded (SYRD-616), and that comes first; a worktree it runs
    in or names, second.
    """
    holders = list(holders)
    leased = _lease_tickets(conn, holders)
    wanted = sorted({ticket for holder in holders
                     for ticket in ([leased[holder.key]] if holder.key in leased else holder.candidate_tickets())})
    if not wanted:
        return {}
    rows = conn.execute("SELECT id, state, assignee FROM ticket_board.tickets WHERE id = ANY(%s::text[])",
                        (wanted,)).fetchall()
    known: dict[str, dict[str, Any]] = {}
    for row in rows:
        values = [row[name] for name in ("id", "state", "assignee")] if isinstance(row, dict) else list(row)[:3]
        ticket, state, assignee = (value.decode() if isinstance(value, bytes) else str(value or "") for value in values)
        known[ticket] = {"id": ticket, "state": state, "assignee": assignee}
    found: dict[str, dict[str, Any]] = {}
    for holder in holders:
        ticket = leased.get(holder.key) if holder.key in leased else next(
            (ticket for ticket in holder.candidate_tickets() if ticket in known), "")
        if ticket in known:
            found[holder.key] = known[ticket]
    return found


def lapsed(origin: dict[str, Any] | None, role: str, terminal_states: Iterable[str]) -> bool:
    """Whether the ticket a child was started for is no longer this role's work."""
    if not origin:
        return False
    return origin.get("assignee") != role or origin.get("state") in set(terminal_states)


def _present(ledger: Any, conn: Any) -> bool:
    """Whether this board can record a child alert: a listener can be newer than its board's migrations."""
    present = getattr(ledger, "_background_child_present", None)
    if present is None:
        row = conn.execute(
            "SELECT to_regprocedure('ticket_board.note_background_child(bigint, text, text)') IS NOT NULL"
        ).fetchone()
        value = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else False
        present = value is True or value in ("t", b"t", 1)
        ledger._background_child_present = present
    return present


def tell(ledger: Any, conn: Any, notification_id: int, holder: Holder, what: str) -> bool:
    """Tell the Director about this child, once for as long as the board remembers it; whether it was newly told.

    Never fatal."""
    try:
        if _present(ledger, conn):
            return _one_value(conn.execute("SELECT ticket_board.note_background_child(%s::bigint, %s::text, %s::text)",
                                           (notification_id, holder.key, what)).fetchone())
    except Exception as exc:  # The delivery must never depend on its own bookkeeping.
        ledger.logger.warning("Failed to note background child %s for notification %s: %s",
                              holder.key, notification_id, exc)
    return False
