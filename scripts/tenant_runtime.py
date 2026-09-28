"""What a tenant's runtime is, in the vocabulary `status` and `list` report it in.

- `TENANT_RUNTIME_STATES` is that vocabulary: running, presentation-closed,
  partially-stopped, suspended and unregistered.
- `TenantRuntime` records what is actually up for one tenant -- its board, its
  listener, its sessions, its window, any process that escaped its panes, and
  what could not be proved -- and names the state that makes it.
- `describe_tenant_runtime` renders that as one line a person can act on.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-404), in their original
order. The launcher imports this module and re-exports every name, so every
suite that reads these there reaches the same objects. None of them reads
anything of the launcher's. The record's decorator is bound when it is
defined, as it was, from this module's own import, the same object. This
module never imports `team_launcher`.
"""

from __future__ import annotations

from dataclasses import dataclass


#: What a tenant's runtime can be, as `status` and `list` report it. A
#: suspension is reversible and keeps every byte of restart state; a teardown
#: removes provisioned state and is a different verb (SYRD-193).
TENANT_RUNTIME_STATES = (
    "running",              # board, listener, sessions and a window
    "presentation-closed",  # the window is gone, everything else still runs
    "partially-stopped",    # some of it is down and some is not
    "suspended",            # nothing runs, everything is preserved
    "unregistered",         # no registration: torn down, or never provisioned
)


@dataclass(frozen=True)
class TenantRuntime:
    """What is actually up for one tenant, each part asked of its own manager."""

    project: str
    board_active: bool
    listener_active: bool
    live_sessions: tuple[str, ...]
    presentation_open: bool
    residual: tuple[int, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        running = [self.board_active, self.listener_active, bool(self.live_sessions)]
        if all(running) and self.presentation_open:
            return "running"
        if all(running) and not self.presentation_open:
            return "presentation-closed"
        if not any(running) and not self.presentation_open and not self.residual:
            return "suspended"
        return "partially-stopped"


def describe_tenant_runtime(runtime: TenantRuntime) -> str:
    """One line a person can act on, naming what is still up."""
    parts = [
        f"board {'up' if runtime.board_active else 'down'}",
        f"listener {'up' if runtime.listener_active else 'down'}",
        f"sessions {len(runtime.live_sessions)}",
        f"window {'open' if runtime.presentation_open else 'closed'}",
    ]
    if runtime.residual:
        parts.append(f"escaped processes {len(runtime.residual)}")
    line = f"{runtime.project}: {runtime.state} ({', '.join(parts)})"
    if runtime.unknown:
        line += " -- not proved: " + "; ".join(runtime.unknown)
    return line
