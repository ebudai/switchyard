"""A role's current work, delivered to a provider that has since been replaced: told to its replacement, once (SYRD-589).

A ticket's notice is typed into one provider process's conversation. When the
role's pane is restored, reloaded or started fresh, that conversation is gone,
but the board still holds the send as the ticket's delivery -- so it showed the
work as delivered while the new provider sat at its startup prompt with no
ticket in view (MEFP: six restored workers, notices sent three days earlier).

On each pass the listener asks the gate which roles' providers are idle and
have begun no turn since they started (`PaneActivityGate.turnless_since`), and
hands the board those start times. The board queues each such role's current
ticket again, in the repeat wording, when its notice for this stage was sent
before the start and nothing for it is queued now; once per provider, keyed by
the start (`ticket_board.redeliver_replaced_work`). The queued notice goes out
through the ordinary delivery path and its gates, like any other.

A provider that has begun a turn is never asked about: whoever started that
turn -- the Director's manual delivery, a person at the pane -- gave it work
the board cannot see, and a second copy of the ticket would be a duplicate
pickup.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def turnless_roles(gate: Any) -> dict[str, str]:
    """Each role whose provider is idle and has begun no turn since it started, with when it started."""
    roles: dict[str, str] = {}
    for role, target in sorted(gate.role_targets.items()):
        started = gate.turnless_since(target)
        if started is not None:
            roles[role] = datetime.fromtimestamp(started, timezone.utc).isoformat()
    return roles


def run(listener: Any, conn: Any) -> int:
    """One pass; returns how many notices were queued again. Never stops the listener."""
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "turnless_since"):
        return 0
    try:
        roles = turnless_roles(gate)
        if not roles:
            return 0
        row = conn.execute("SELECT ticket_board.redeliver_replaced_work(%s::jsonb)",
                           (json.dumps(roles, sort_keys=True),)).fetchone()
        queued = int((next(iter(row.values())) if isinstance(row, dict) else row[0]) or 0) if row else 0
    except Exception as exc:  # logged; the notices this pass delivers matter more
        listener.logger.warning("Replaced-provider redelivery pass failed: %s", exc)
        return 0
    if queued:
        listener.logger.info("Queued %s notice(s) again for providers replaced since delivery: %s", queued, roles)
    return queued
