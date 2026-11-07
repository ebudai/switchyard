"""Provider quota holds: the read-only status and the Director's clear (SYRD-644).

* `status` is what `/api/provider-holds` and `ticket-board-read holds` show:
  each current hold, and those ended in the last day -- the role, its provider,
  when the evidence was observed, the reset or recheck time, and how to clear
  it. The provider's own error line is shown; credentials and prompts never are.
* `clear_provider_hold` ends a role's hold at once, with a reason.

Authority is the database's (`require_actor`), as for every board operation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

OPERATIONS = frozenset({"clear_provider_hold", "hold_provider_account", "clear_provider_account_hold"})


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if ticket_id is not None:
        raise ValueError(f"{operation} names its role in the request, not a ticket path")
    if operation in ("hold_provider_account", "clear_provider_account_hold"):
        return _account(app, operation, payload, caller_role=caller_role)
    if operation != "clear_provider_hold":
        raise ValueError(f"unknown provider hold operation: {operation}")
    if set(payload) - {"role", "reason"}:
        raise ValueError("clear_provider_hold takes role and reason")
    role, reason = payload.get("role"), payload.get("reason")
    if not isinstance(role, str) or not role.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError("clear_provider_hold needs a role and a reason")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.clear_provider_hold(%s, %s) AS hold", (role.strip(), reason.strip())).fetchone()
    return {"hold": _plain(row["hold"])}


def _account(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
    """SYRD-672: hold a provider account by hand, or clear its hold; the database says who may."""
    if set(payload) - {"runtime", "reason"}:
        raise ValueError(f"{operation} takes runtime and reason")
    runtime, reason = payload.get("runtime"), payload.get("reason")
    if not isinstance(runtime, str) or not runtime.strip() or not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{operation} needs a runtime and a reason")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(f"SELECT ticket_board.{operation}(%s, %s) AS hold", (runtime.strip(), reason.strip())).fetchone()
    return {"hold": _plain(row["hold"])}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items() if key not in ("history",)}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value.isoformat() if isinstance(value, datetime) else value


def status(app: Any) -> dict[str, Any]:
    """The board's provider holds, read-only; empty on a board older than them."""
    with app._pg_connect() as conn:
        present = conn.execute("SELECT to_regprocedure('ticket_board.provider_hold_status(timestamptz)') IS NOT NULL AS present").fetchone()
        if not (present["present"] if isinstance(present, dict) else present[0]):
            return {"holds": []}
        row = conn.execute("SELECT ticket_board.provider_hold_status(clock_timestamp()) AS status").fetchone()
        # SYRD-672: held provider accounts, each with the roles it holds now.
        accounts = conn.execute("SELECT CASE WHEN to_regprocedure('ticket_board.provider_account_hold_status()') IS NULL "
                                "THEN NULL ELSE ticket_board.provider_account_hold_status() END AS accounts").fetchone()
    status = _plain(row["status"])
    found = accounts["accounts"] if isinstance(accounts, dict) else (accounts[0] if accounts else None)
    return {**status, **({"accounts": _plain(found)} if found is not None else {})}


def describe(payload: dict[str, Any]) -> str:
    """`ticket-board-read holds`."""
    lines: list[str] = []
    for h in payload.get("holds") or []:
        if h.get("state") == "ended":
            lines.append(f"{h['role']} ({h['runtime']}): ended {h.get('ended_at')} -- {h.get('end_reason')}")
            continue
        when = f"until {h['deadline']}" + (" (a recheck: the provider gave no reset time)" if h.get("recheck")
                                           else f" (reset {h.get('reset_at')}, plus a grace)")
        state = "held" if h.get("current") else "released, its one fresh attempt pending"
        lines.append(f"{h['role']} ({h['runtime']}): {state} {when}; evidence {h.get('evidence_at')} from {h.get('source')}"
                     + (f", renewed {h['renewals']}x" if h.get("renewals") else ""))
        if h.get("message"):
            lines.append(f"  provider said: {h['message']}")
        if h.get("clear"):
            lines.append(f"  clear: {h['clear']}")
    for h in (payload.get("accounts") or {}).get("held") or []:
        why = (f"{h.get('role')}'s provider refused on safety grounds" + (f" ({h['category']})" if h.get("category") else "")
               if h.get("kind") == "refusal" else f"held by hand by {h.get('began_by')}: {h.get('reason')}")
        lines.append(f"{h['runtime']} account HELD since {h.get('began_at')} (hold {h['hold']}): {why}")
        lines.append(f"  holds: {', '.join(h.get('roles') or []) or 'no role now'}; nothing reaches them until it is cleared"
                     + (f"; {h['refusals']} refusal(s) seen" if h.get("refusals") else ""))
        lines.append(f"  clear, once the account is confirmed safe: ticket-board-write clear-provider-account-hold "
                     f"{h['runtime']} --reason \"...\"")
    return "\n".join(lines) if lines else "No provider quota holds."
