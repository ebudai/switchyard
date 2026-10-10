"""Claim admitted work for eligible idle implementers, under a pull policy (SYRD-539).

On every listener pass -- which is what makes pickup follow an Audit approval,
a worker coming up, newly admitted work and a restart alike, with no state of
its own to lose -- each implementer that may claim is judged from what the
board can actually see:

* a registered provider process that is still the live one (the listener
  only keeps targets whose registered pid and start time are running);
* hook state that says idle -- not stopped on a prompt, not unknown;
* no activity the gate's probes see, and no running background work
  (SYRD-538).

An eligible worker is given its next ticket by `claim_ready_ticket`, the same
atomic function a worker calls for itself. The claim's ordinary assignment
notice is the only message anyone gets: a successful pickup tells the
Director nothing. What the Director is told, once per waiting ticket, is when
claimable work has waited past the policy's `idle_alert_seconds` while
implementers sit idle -- with each one's reason, so unknown readiness is
visible as unknown rather than reported as idle.

Who is asked is the declared claimant pool (SYRD-573), not the claim
transition's actor list: every active, ephemeral implementer that owns the
implementation stage (`workflow_config.pull_claimant_pool`, the rule the
board applies to every claim). Of those, only a worker whose registered
provider process is running is a claimant now; a stopped one is neither
claimed for nor called idle, since starting workers is the operator's, never
the board's.
"""

from __future__ import annotations

import json
from typing import Any

from . import session_context
from .workflow_config import pull_claimant_pool


#: Why a pool member is not running, as `why_not_eligible` says it: such a
#: worker is not capacity, so it is never reported as idle.
NOT_RUNNING = frozenset({"no registered provider process", "registered provider process is gone"})


def why_not_eligible(gate: Any, role: str) -> str:
    """"" when the role can be given work now; otherwise what the board sees instead."""
    from .peer_identity import session_is_live

    target = gate.role_targets.get(role)
    identity = gate.role_identities.get(role)
    if target is None or identity is None:
        # Without a registration a static target is only a name.
        return "no registered provider process"
    if not session_is_live(identity, proc_root=gate.proc_root):
        return "registered provider process is gone"
    state = gate.state_store.read(target)
    if state is None:
        return "no hook state: readiness unknown"
    if state.state == "blocked":
        return "stopped on a prompt"
    if state.state != "idle":
        return "working"
    if gate.eligibility_busy(target):
        return "working"
    if role in gate.background_work_roles([role]):
        return "background work running"
    return ""


def _scalar(row: Any) -> Any:
    if row is None:
        return None
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def run(listener: Any, conn: Any) -> int:
    """One pickup pass; returns how many tickets were claimed."""
    policy = _scalar(conn.execute("SELECT ticket_board.declared_scheduling()").fetchone())
    if isinstance(policy, str):
        policy = json.loads(policy)
    if not isinstance(policy, dict) or not policy:
        return 0  # no pull policy: nothing here runs
    gate = getattr(listener.activity_gate, "__self__", None)
    if gate is None or not hasattr(gate, "background_work_roles"):
        # Without the gate there is nothing to judge eligibility by, and a
        # claim made blind would hand work to a worker nobody checked.
        listener.logger.warning("Pull pickup skipped: no pane activity gate to judge eligibility")
        return 0
    claimed = 0
    idle: dict[str, str] = {}
    for role in pull_claimant_pool(listener.workflow or {}):
        reason = why_not_eligible(gate, role)
        if reason:
            if reason not in NOT_RUNNING:
                idle[role] = reason
            continue
        try:
            result = _scalar(conn.execute("SELECT ticket_board.claim_ready_ticket(%s)", (role,)).fetchone())
        except Exception as exc:  # logged, never fatal to the pass
            listener.logger.warning("Pull claim for %s failed: %s", role, exc)
            continue
        if isinstance(result, str):
            result = json.loads(result)
        if result and result.get("claimed"):
            claimed += 1
            listener.logger.info("Pull pickup: %s claimed %s", role, result.get("ticket"))
        elif result and result.get("reason") == "nothing ready":
            idle[role] = "idle, nothing claimable"
    session_context.passes(listener, conn, gate)  # SYRD-540: bind, confirm and restart-repair conversations
    try:
        conn.execute("SELECT ticket_board.notify_pull_idle_capacity(%s::jsonb, clock_timestamp())",
                     (json.dumps(idle, sort_keys=True),))
    except Exception as exc:  # logged, never fatal to the pass
        listener.logger.warning("Pull idle-capacity check failed: %s", exc)
    return claimed
