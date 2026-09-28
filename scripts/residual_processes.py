"""What still runs for a tenant after its panes are gone, and stopping it -- and nothing else.

- `residual_project_processes` finds the processes still carrying a project's
  identity in their environment -- the exact slug, and a caller role, which a
  managed service does not carry -- never this process or anything that
  started it, and never a member of one of the project's managed units by the
  kernel's own cgroup record.
- `contain_residual_project_processes` sends each of those, and only those,
  SIGTERM, and reports precisely what would not go.
- `ResidualProcess`, `_process_environ`, `_process_ancestry` and
  `process_systemd_unit` are the record and the `/proc` readers they use.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-402), in their original
order. The launcher imports this module and re-exports every name, so its
suspension, and every suite that calls these there, reach the same objects.
The project's managed unit names -- a launcher facility -- and every name
defined here that another definition here reads when it runs are read from
`team_launcher` when it runs, as they were, so a patch on the launcher still
intercepts. The record's decorator and the `signaller` and `print_func`
defaults are bound when each is defined, as they were. The standard-library
names are this module's own imports, the same objects. `ProjectConfig` is
imported for annotations only. This module never imports `team_launcher` at
its top.
"""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


@dataclass(frozen=True)
class ResidualProcess:
    """A process still carrying this project's identity after its session died."""

    pid: int
    uid: int
    command: str


def _process_environ(entry: Path) -> dict[str, str]:
    try:
        raw = (entry / "environ").read_bytes()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for item in raw.decode("utf-8", "replace").split("\0"):
        name, separator, value = item.partition("=")
        if separator:
            values[name] = value
    return values


def _process_ancestry(pid: int, *, proc_root: Path) -> set[int]:
    """This process and everything that started it, so a stop cannot kill itself."""
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        seen.add(current)
        try:
            stat = (proc_root / str(current) / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        # Field 4 is the parent, counted after the LAST ')': a comm may contain
        # spaces and parentheses, and splitting on whitespace reads the wrong
        # field for any process whose name does (SYRD-169).
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 2:
            break
        try:
            current = int(tail[1])
        except ValueError:
            break
    return seen


def process_systemd_unit(entry: Path) -> str:
    """The systemd unit a process belongs to, from its own cgroup.

    Real unit metadata, not a guess from a name: the kernel reports
    `0::/system.slice/<project>-ticket-board.service` for a system unit and
    `0::/user.slice/user-<uid>.slice/user@<uid>.service/app.slice/<unit>` for one
    of the owner's, and the last segment is the unit either way. Readable
    without privilege, which matters because the caller here IS root and can
    read everything -- the exclusion has to hold for the one reader that sees
    every process.
    """
    try:
        raw = (entry / "cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in raw.splitlines():
        path = line.rpartition(":")[2]
        segment = path.rstrip("/").rpartition("/")[2]
        if segment.endswith(".service") or segment.endswith(".scope"):
            return segment
    return ""


def residual_project_processes(
    config: ProjectConfig,
    *,
    proc_root: Path | None = None,
    exclude: Iterable[int] = (),
) -> list[ResidualProcess]:
    """Processes still carrying this project's identity, by environment.

    The marker is `TICKET_BOARD_PROJECT`, read from the ENVIRONMENT rather than
    from argv. That is the whole point: a child that outlived its pane -- or
    that exec'd away from the wrapper that started it -- keeps its inherited
    environment, while the argv marker is gone the moment it execs, which is the
    mistake SYRD-169 was. An exact match on the slug, because `atlas` must not
    answer for `atlas-staging`.

    This command's own process and every one of its ancestors are excluded:
    running `switchyard stop` from inside a role pane must not report, or
    terminate, the shell that is running it.
    """
    from scripts import team_launcher as launcher

    root = Path(proc_root) if proc_root is not None else Path("/proc")
    skip = set(exclude) | launcher._process_ancestry(os.getpid(), proc_root=root)
    residual: list[ResidualProcess] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return residual
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in skip:
            continue
        environment = launcher._process_environ(entry)
        if environment.get("TICKET_BOARD_PROJECT", "") != config.project:
            continue
        # A role WORKLOAD, positively: a pane and everything it started carry a
        # caller role, and a managed service does not. The board unit sets
        # `TICKET_BOARD_PROJECT` too -- on this host
        # `testing-ticket-board.service` literally does -- so the project marker
        # alone selects the services this stop is supposed to stop through their
        # own managers (SYRD-193 review).
        if not environment.get("TICKET_BOARD_CALLER_ROLE", "").strip():
            continue
        # And belt-and-braces from the kernel's own record of unit membership,
        # so a service that ever gained a caller role in its environment is
        # still out of scope.
        if launcher.process_systemd_unit(entry) in launcher.managed_unit_names(config):
            continue
        try:
            uid = entry.stat().st_uid
            command = (entry / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ").strip()
        except OSError:
            continue
        residual.append(launcher.ResidualProcess(pid, uid, command or f"pid {pid}"))
    return residual


def contain_residual_project_processes(
    config: ProjectConfig,
    *,
    proc_root: Path | None = None,
    signaller: Callable[[int, int], None] = os.kill,
    print_func: Callable[[str], None] = print,
) -> list[str]:
    """Terminate what escaped the panes, and report precisely what would not go.

    A suspension that leaves a role's work running is not a suspension: the
    tenant looks stopped and is still writing. Everything signalled here carries
    this project's own identity in its environment, so nothing belonging to
    another tenant -- or to the desktop account's own unrelated work -- is in
    scope even when they share a uid.
    """
    from scripts import team_launcher as launcher

    problems: list[str] = []
    for process in launcher.residual_project_processes(config, proc_root=proc_root):
        try:
            signaller(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            problems.append(
                f"{config.project} process {process.pid} (uid {process.uid}) survived its pane and "
                f"this process may not stop it: {process.command[:120]}"
            )
            continue
        except OSError as exc:
            problems.append(f"could not stop {config.project} process {process.pid}: {exc}")
            continue
        print_func(f"stopped escaped {config.project} process: {process.pid} ({process.command[:80]})")
    return problems
