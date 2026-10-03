"""The board side of a pull policy for workers and readers (SYRD-539).

`claim_next` is a worker taking its own next ticket: the database decides --
the caller's process-bound role, the declared claim transition, one active
ticket per worker, pinned rework first. `status` is the queue as the claim
sees it, for the reservations route and worker-pool status.
"""

from __future__ import annotations

from typing import Any

OPERATIONS = frozenset({"claim_next"})


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
    if operation != "claim_next":
        raise ValueError(f"unknown pull queue operation: {operation}")
    if payload:
        raise ValueError("claim_next takes no arguments: a worker claims only for itself")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.claim_ready_ticket(%s) AS result", (caller_role,)).fetchone()
    return dict(row["result"])


def context_restore(cfg: dict[str, Any] | None) -> dict[str, str]:
    """What happens to each claimant's returned rework (SYRD-540), so the limit is visible before anyone relies on it."""
    from .pull_pickup import _claimants
    from .session_context import RESUME_COMMANDS
    policy = (cfg or {}).get("scheduling")
    if not isinstance(policy, dict):
        return {}
    runtimes = {role["name"]: role.get("runtime") for role in cfg.get("roles", [])}
    return {role: (f"automatic: {runtimes[role]} resumes the ticket's saved conversation, handed over once proven"
                   if runtimes.get(role) in RESUME_COMMANDS else
                   f"not automatic: {runtimes.get(role) or 'no runtime'} has no verified in-session resume, "
                   "so returned rework parks for the Director")
            for role in _claimants(cfg, policy)}


def status(app: Any) -> dict[str, Any]:
    """Ready, waiting-for-author and pulled work; {"enabled": false} without a pull policy or this release's schema."""
    try:
        with app._pg_connect() as conn:
            row = conn.execute("SELECT ticket_board.pull_queue_status() AS status").fetchone()
            document = conn.execute("SELECT document FROM ticket_board.workflow_configuration").fetchone()
    except Exception:  # a board older than this route reports nothing rather than failing the read
        return {"enabled": False}
    result = dict(row["status"])
    if result.get("enabled"):
        result["context_restore"] = context_restore(document["document"] if document else None)
    return result


def reservations(conn: Any) -> dict[str, Any]:
    """Each implementer's held ticket as the routing gate decides it, and the admitted work waiting to be pulled.

    Probed first, never attempted: a failed query would abort the transaction
    applying the workflow, and a board older than these functions has no
    holds or queue to preview.
    """
    present = conn.execute("SELECT to_regprocedure('ticket_board.serial_reservations()') IS NOT NULL AS holds, "
                           "to_regprocedure('ticket_board.pull_queue_status()') IS NOT NULL AS queue").fetchone()
    rows = conn.execute("SELECT implementer, ticket_id FROM ticket_board.serial_reservations()").fetchall() if present["holds"] else []
    status = conn.execute("SELECT ticket_board.pull_queue_status() AS status").fetchone()["status"] if present["queue"] else {}
    return {"holds": {str(row["implementer"]): (str(row["ticket_id"]) if row["ticket_id"] else None) for row in rows},
            "ready": [entry["id"] for entry in (status or {}).get("ready_work", [])]}


def reservation_changes(conn: Any, before: dict[str, Any]) -> dict[str, Any]:
    """What a workflow document does to in-flight work, read in the same transaction that applied it.

    A dry run rolls that transaction back, so this is the preview of enabling,
    disabling or changing a pull policy: whose hold changes, and which admitted
    tickets would be left waiting with nobody pulling them.
    """
    after = reservations(conn)
    holds_before, holds_after = before["holds"], after["holds"]
    return {
        "holds": [{"implementer": role, "before": holds_before.get(role), "after": holds_after.get(role)}
                  for role in sorted(set(holds_before) | set(holds_after)) if holds_before.get(role) != holds_after.get(role)],
        "ready_work_left_unpulled": sorted(set(before["ready"]) - set(after["ready"])) if not after["ready"] else [],
        "context_restore": context_restore(_applied(conn)),
    }


def _applied(conn: Any) -> dict[str, Any] | None:
    row = conn.execute("SELECT document FROM ticket_board.workflow_configuration").fetchone()
    return row["document"] if row else None
