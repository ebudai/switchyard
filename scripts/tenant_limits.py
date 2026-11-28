"""A tenant's role panes never inherit the launching user's real-time scheduling (SYRD-895).

On 2026-11-27 an ocstab stress run in its ops pane ran 33 threads as SCHED_RR priority 1 at nice -4, without asking
for either. The host runs with real-time throttling off (`kernel.sched_rt_runtime_us` equal to its period, a CachyOS
default the User keeps), so they took every CPU. Every tenant's board, Postgres and listener starved, and so did the
User's desktop, until the Director demoted the threads by hand.

The panes run as `ocstab-agent`, which no limits rule grants anything, yet they had `Max realtime priority 99` and
`Max nice priority 31`. Those are the User's, from `/etc/security/limits.d/20-audio.conf`: `@audio - rtprio 99`
and `nice -11`. A process inherits its parent's resource limits; sudo, the tenant-control bridge and `runuser` cross
to the tenant account without lowering them (pam_limits sets only what a rule names for the target, and no rule names
these for a tenant); and a tenant's tmux server keeps whatever the process that started it had, for every pane it
ever opens. So any tenant process could claim real-time scheduling and starve the host, every other tenant included.

`drop_inherited_scheduling()` lowers this process's own RLIMIT_RTPRIO and RLIMIT_NICE to 0:0 -- PID 1's, so no
real-time priority and no nice below 0 -- raises a negative nice to 0, and leaves a real-time scheduling policy. Any
process may lower its own limits and priority without privilege.

A pane inherits from its tmux server, and the server from whichever process started it, so the drop has to happen in
every process that may start a tenant's server, not in one caller (Audit, round 1: break-glass started the server
first, and every role pane opened on it afterwards had 99/31). It is called:
- at the entry of `scripts/switchyard` and `scripts/team-launcher`, before anything runs: every command, every path;
- by the owner-account runners (`process_inspection.role_process_runner_for` and `_owner_process_runner`), for any
  caller in a longer-lived process that runs tmux as the project -- break-glass's start among them;
- at the top of `role_sessions._start_role_session`, as before.

`process_scheduling(pid)` reads what a running process may claim, from /proc, for the host readiness check
(`host_diagnostics`), which names a tenant's tmux server still running with the launching user's limits: a server
started before this change keeps them until every role of its tenant stops.
"""

from __future__ import annotations

import os
import re
import resource
from pathlib import Path

#: The limits a tenant process gets, soft and hard: PID 1's own on a stock host.
TENANT_RTPRIO = (0, 0)
TENANT_NICE = (0, 0)
_LIMIT_LINE = re.compile(r"^Max (realtime priority|nice priority)\s+(\S+)\s+(\S+)", re.MULTILINE)


def drop_inherited_scheduling() -> list[str]:
    """Lower this process's real-time and nice limits to a tenant's, its nice to at least 0, and its scheduling
    policy to SCHED_OTHER when it is a real-time one. Returns what changed, in words; never raises."""
    changed = []
    for name, limit, wanted in (("RLIMIT_RTPRIO", resource.RLIMIT_RTPRIO, TENANT_RTPRIO),
                                ("RLIMIT_NICE", resource.RLIMIT_NICE, TENANT_NICE)):
        try:
            current = resource.getrlimit(limit)
            if current != wanted:
                resource.setrlimit(limit, wanted)
                changed.append(f"{name} {current[0]}:{current[1]} -> {wanted[0]}:{wanted[1]}")
        except (OSError, ValueError):
            pass
    try:
        nice = os.getpriority(os.PRIO_PROCESS, 0)
        if nice < 0:
            os.setpriority(os.PRIO_PROCESS, 0, 0)
            changed.append(f"nice {nice} -> 0")
    except OSError:
        pass
    try:
        policy = os.sched_getscheduler(0)
        if policy in (os.SCHED_FIFO, os.SCHED_RR):
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
            changed.append("a real-time scheduling policy -> SCHED_OTHER")
    except (OSError, AttributeError):
        pass
    return changed


def _value(raw: str) -> int:
    return 1 << 62 if raw == "unlimited" else int(raw)


def process_scheduling(pid: int, proc: Path = Path("/proc")) -> dict[str, tuple[int, int]] | None:
    """{"rtprio": (soft, hard), "nice": (soft, hard)} from /proc/<pid>/limits, or None when it cannot be read."""
    try:
        text = (proc / str(pid) / "limits").read_text()
        found = {m.group(1): (_value(m.group(2)), _value(m.group(3))) for m in _LIMIT_LINE.finditer(text)}
    except (OSError, ValueError):
        return None
    if set(found) != {"realtime priority", "nice priority"}:
        return None
    return {"rtprio": found["realtime priority"], "nice": found["nice priority"]}


def too_permissive(limits: dict[str, tuple[int, int]]) -> list[str]:
    """What a process with these limits may claim that a tenant process must not: any real-time priority, or a nice
    below 0 (RLIMIT_NICE above 20 lets it lower its nice to 20 minus the limit; 0, a tenant's, lets it lower nothing)."""
    problems = []
    rtprio, nice = limits["rtprio"], limits["nice"]
    if max(rtprio) > 0:
        problems.append(f"real-time priority up to {max(rtprio)} (RLIMIT_RTPRIO {rtprio[0]}:{rtprio[1]})")
    if max(nice) > 20:
        problems.append(f"nice down to {20 - max(nice)} (RLIMIT_NICE {nice[0]}:{nice[1]})")
    return problems
