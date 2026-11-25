"""Waiting review work goes to an idle owner of the same review stage (SYRD-745).

A declared transition into a stage that names no owner gives the ticket the stage's first owner. With two owners of
one review stage -- MEFP's audit and audit-2 -- every submission went to the first and waited behind its current
review ("finish current"), while the second sat live, idle and empty, until the Director moved work by hand.

On every listener pass, for each review stage with more than one owner, each owner the listener can give work to
now (`pull_pickup.why_not_eligible`: a registered provider process that is live, hook state idle, nothing running)
is offered the stage's oldest waiting review by `ticket_board.balance_review_stage`. The board decides under its own
rules what may move: only a review waiting behind its reviewer's current ticket, never that current ticket, never
held, blocked or awaited work, never a re-review its reviewer began, and only to an owner holding nothing in the
stage and not provider-held. A moved ticket is recorded on the ticket and handed to its new owner as a reassignment
is; the notice still queued for the old owner no longer names the assignee and is dropped.

SYRD-873: live and idle is not enough. A review whose hand-off to that owner would park -- the owner holds the ticket's
saved conversation from an earlier round and cannot restore it (`session_context.why_not_restorable`: a runtime with no
verified in-session resume, an unconfirmed binding, another runtime), or a restore of it already failed -- is skipped:
the delivery would park it, as on MEFP-1234. Read from durable records (the binding, the failed restore) and the
declared runtime, never from the pane, so the judgement cannot flap. The board itself never moves a review the Director
placed in the stage (`balance_review_stage_skipping`); a parked hand-off is its owner's current ticket, never taken.

SYRD-875: a resubmission lands with the reviewer that kicked it back, and the balancer never moves it from there. When
that reviewer's provider process is not running, the pass gives the return up before the hand-off
(`review_return_fall_back`): the rework goes to the stage's first owner, as a first submission does, the ticket says
why, and it may then be balanced like any other waiting review.
"""

from __future__ import annotations

import json
from typing import Any

from . import session_context
from .pull_pickup import NOT_RUNNING, why_not_eligible

#: SYRD-873's balancer, which takes the tickets to skip; SYRD-745's is the fallback on an older board.
SKIPPING = "ticket_board.balance_review_stage_skipping(text, text, timestamptz, text[])"
PLAIN = "ticket_board.balance_review_stage(text, text, timestamptz)"
#: SYRD-875: the listener's half of a returned rework's fallback; absent on an older board.
FALL_BACK = "ticket_board.review_return_fall_back(text, timestamptz, jsonb)"


def _present(listener: Any, conn: Any) -> str:
    """The balancer this board has: SKIPPING, PLAIN, or '' for none."""
    present = getattr(listener, "_review_balance_present", None)
    if present is None:
        present = ""
        for signature in (SKIPPING, PLAIN):
            row = conn.execute("SELECT to_regprocedure(%s) IS NOT NULL AS present", (signature,)).fetchone()
            if row and (row["present"] if isinstance(row, dict) else row[0]):
                present = signature
                break
        listener._review_balance_present = present
    return present


def _has(listener: Any, conn: Any, signature: str) -> bool:
    known = getattr(listener, "_review_functions_present", None)
    if known is None:
        known = listener._review_functions_present = {}
    if signature not in known:
        row = conn.execute("SELECT to_regprocedure(%s) IS NOT NULL AS present", (signature,)).fetchone()
        known[signature] = bool(row and (row["present"] if isinstance(row, dict) else row[0]))
    return known[signature]


def fall_back(listener: Any, conn: Any, stage: str, eligibility: dict[str, str]) -> list:
    """Give up the return of each rework in `stage` whose reviewer is not running (or that the board finds unavailable)."""
    if not _has(listener, conn, FALL_BACK):
        return []
    not_running = {role: why for role, why in eligibility.items() if why in NOT_RUNNING}
    row = conn.execute("SELECT ticket_board.review_return_fall_back(%s, clock_timestamp(), %s::jsonb)",
                       (stage, json.dumps(not_running, sort_keys=True))).fetchone()
    result = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
    result = json.loads(result) if isinstance(result, (str, bytes)) else result
    for fell in result or []:
        listener.logger.info("Review return: %s goes to %s in %s, not back to %s (%s)", fell.get("ticket"),
                             fell.get("to"), stage, fell.get("from"), fell.get("why"))
    return result or []


def would_park(listener: Any, conn: Any, stage: str, role: str) -> list[str]:
    """The reviews waiting in `stage` with another owner whose hand-off to `role` would park."""
    rows = conn.execute("SELECT id FROM ticket_board.tickets WHERE state = %s AND assignee <> %s ORDER BY ticket_number",
                        (stage, role)).fetchall()
    skipped = []
    for row in rows:
        ticket = session_context._text(row["id"] if isinstance(row, dict) else row[0])
        status = session_context._status(conn, ticket, role)
        if status.get("parked") or (status.get("context") is not None
                                    and session_context.why_not_restorable(listener, role, status["context"])):
            skipped.append(ticket)
    return skipped


def _eligibility(listener: Any, gate: Any, role: str) -> str:
    try:
        return why_not_eligible(gate, role)
    except Exception as exc:  # logged; never fatal to the pass, and never read as eligible
        listener.logger.warning("Review balance could not judge %s: %s", role, exc)
        return f"eligibility unknown: {exc}"


def run(listener: Any, conn: Any) -> int:
    """One pass; how many waiting reviews moved. Never fatal to the pass."""
    gate = getattr(listener.activity_gate, "__self__", None)
    stages = [s for s in ((listener.workflow or {}).get("stages") or []) if s.get("kind") == "review" and len(s.get("owners") or []) > 1]
    balancer = _present(listener, conn) if gate is not None and hasattr(gate, "background_work_roles") and stages else ""
    if not balancer:
        return 0
    from . import work_pause

    if work_pause.project_paused(listener, conn):
        return 0  # SYRD-756: no review moves while work is paused
    moved = 0
    for stage in stages:
        eligibility = {role: _eligibility(listener, gate, role) for role in stage["owners"]}
        try:
            fall_back(listener, conn, stage["name"], eligibility)
        except Exception as exc:  # logged; never fatal to the pass
            listener.logger.warning("Review return fallback in %s failed: %s", stage["name"], exc)
        for role in stage["owners"]:
            try:
                if eligibility[role]:
                    continue
                if balancer == SKIPPING:
                    row = conn.execute("SELECT ticket_board.balance_review_stage_skipping(%s, %s, clock_timestamp(), %s)",
                                       (stage["name"], role, would_park(listener, conn, stage["name"], role))).fetchone()
                else:
                    row = conn.execute("SELECT ticket_board.balance_review_stage(%s, %s, clock_timestamp())",
                                       (stage["name"], role)).fetchone()
            except Exception as exc:  # logged; never fatal to the pass
                listener.logger.warning("Review balance for %s in %s failed: %s", role, stage["name"], exc)
                continue
            result = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
            result = json.loads(result) if isinstance(result, (str, bytes)) else result
            if isinstance(result, dict) and result.get("moved"):
                moved += 1
                listener.logger.info("Review balance: %s moved from %s to idle %s in %s (it waited behind %s)",
                                     result.get("ticket"), result.get("from"), role, stage["name"], result.get("behind"))
    return moved
