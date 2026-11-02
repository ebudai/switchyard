"""Whether a Hermes pane may be cleared into a new ticket (SYRD-693).

`/new now` empties a Hermes conversation, but Hermes puts its memory
(`memories/MEMORY.md`, `USER.md`) into every new session's prompt, and its
`session_search` tool reads every earlier session. So an ephemeral Hermes role
starts a ticket on nothing from the last one only if its Hermes was started
with those off. Switchyard writes them off into the role's own `config.yaml`
(`scripts.hermes_role_config.close_ticket_boundary`), marked with
`TICKET_BOUNDARY_MARK`. Hermes reads that file once, when it starts: a `/clear`
keeps the same agent, and nothing it does rereads its config.

A workflow apply can make a running role ephemeral without restarting it. So
before the board clears a Hermes pane into a ticket, it checks what the pane's
own process started from:
- the HERMES_HOME in the pane's start command (`#{pane_start_command}`);
- that home's `config.yaml`, which must carry the mark;
- and that file being older than the pane's process, so the process read it.

Anything else holds the hand-off, with the reason, until the role is restarted.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable

TICKET_BOUNDARY_MARK = (
    "# switchyard-ticket-boundary: closed (SYRD-693): Hermes memory, user profile, memory provider and "
    "session search are off, so nothing reaches a ticket from an earlier conversation."
)
#: How far into config.yaml the mark may be: after the file's own marks.
MARK_LINES = 4
HERMES_BOUNDARY_OPEN_ERROR = "hermes_ticket_boundary_open"
#: /proc's boot time is whole seconds, and the config is written just before its pane starts.
CLOCK_SLACK_SECONDS = 2.0
REMEDY = "restart the role so its Hermes starts on its prepared home"


def _process_start(pid: int, proc: Path) -> float | None:
    """When process `pid` started, in seconds since the epoch, from /proc; None when it cannot be read."""
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="ascii")
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])  # field 22, starttime, after the command's parentheses
        boot = next(int(line.split()[1]) for line in (proc / "stat").read_text(encoding="ascii").splitlines()
                    if line.startswith("btime "))
    except (OSError, ValueError, IndexError, StopIteration):
        return None
    return boot + ticks / os.sysconf("SC_CLK_TCK")


def start_command_home(command: str) -> str:
    """The HERMES_HOME a pane's start command gives its CLI, or "": the launcher's `env ... HERMES_HOME=...`."""
    try:
        words = shlex.split(command)
        if len(words) == 1:  # tmux quotes a command given as one shell string, as the launcher gives it, whole
            words = shlex.split(words[0])
    except ValueError:
        return ""
    homes = [word.split("=", 1)[1] for word in words if word.startswith("HERMES_HOME=")]
    return homes[-1] if homes and os.path.isabs(homes[-1]) else ""


def open_reason(target: str, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
                proc: Path = Path("/proc")) -> str:
    """"" when the Hermes in `target` started with its ticket boundary closed; otherwise why it may not be cleared."""
    try:
        shown = runner(["tmux", "display-message", "-p", "-t", target, "#{pane_pid}\t#{pane_start_command}"],
                       text=True, capture_output=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"tmux did not say what {target} runs ({exc})"
    pid_text, _, command = (shown.stdout or "").rstrip("\n").partition("\t")
    if shown.returncode != 0 or not pid_text.isdigit():
        return f"tmux did not say what {target} runs ({(shown.stderr or '').strip()[:200]})"
    home = start_command_home(command)
    if not home:
        return f"{target}'s start command names no HERMES_HOME, so what its Hermes loaded is unknown"
    config = Path(home) / "config.yaml"
    try:
        with open(config, encoding="utf-8", errors="replace") as stream:
            head = [stream.readline().rstrip("\n") for _ in range(MARK_LINES)]
        written = os.stat(config).st_mtime
    except OSError as exc:
        return f"{config} cannot be read ({exc.strerror or exc})"
    if TICKET_BOUNDARY_MARK not in head:
        return f"{config} leaves Hermes's memory and session search on; {REMEDY}"
    started = _process_start(int(pid_text), proc)
    if started is None:
        return f"when {target}'s process started cannot be read"
    if written > started + CLOCK_SLACK_SECONDS:
        return f"{config} was written after {target}'s Hermes started, which has not read it; {REMEDY}"
    return ""
