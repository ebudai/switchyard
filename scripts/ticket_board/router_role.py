"""The Router role (SYRD-599): choosing which owner gets work already admitted.

The Director routes a ticket from triage into Implementation assigned to
`router`. Router reads it once and makes exactly one of two writes:

* `router_assign` -- the same ticket, same stage, assigned to an active owner
  of the Implementation stage. The board's existing serial-focus and
  assigned-queue rules then decide when that owner works it.
* `router_return` -- back to `analysis` for the Director, with a reason that
  becomes the Director's handoff.

Neither is a generic Director operation, and Router holds none of those. Its
one capability is `router_assign`; its one declared move is `router_return`.
The database refuses everything else independently (`router_assign`,
`router_return`, `require_router_change` and `validate_router_declaration` in
schema.sql); this module asks the same questions first so a refusal names the
problem before a connection is opened, and so the server's capability check
never has to learn about Router.

Router owns no stage. Owning the Implementation stage is what makes a role an
implementer, so Router never reserves a serial slot and any number of tickets
can wait on it; `router_holds` is the one place a role that does not own the
Implementation stage may still be its assignee.
"""

from __future__ import annotations

from typing import Any, Callable

from .role_kinds import IMPLEMENTER, has_kind, scope_flag

ROUTER_ROLE = "router"
ROUTER_KIND = "router"
ASSIGN = "router_assign"
RETURN = "router_return"
KICK_BACK = "implementer_kick_back"
TRIAGE_STAGE = "analysis"
REASON_MAX_CHARS = 2000
OPERATIONS = frozenset({ASSIGN, RETURN})
_PAYLOAD_KEYS = {ASSIGN: {"assignee", "revision"}, RETURN: {"reason", "revision"}}


def router_role(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    """The document's router role, active or not, or None."""
    return next((r for r in (cfg or {}).get("roles", []) if r.get("kind") == ROUTER_KIND), None)


def implementation_stage(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    return next((s for s in (cfg or {}).get("stages", []) if s.get("kind") == "implementation"), None)


def router_holds(cfg: dict[str, Any] | None, stage: str | None, assignee: str | None) -> bool:
    """ticket_board.router_holds: the active router, in the Implementation stage (any stage if None)."""
    role = router_role(cfg)
    if not role or not role.get("active") or role.get("name") != str(assignee or "").strip().lower():
        return False
    impl = implementation_stage(cfg)
    return stage is None or (impl is not None and impl.get("name") == stage)


def assignable_roles(cfg: dict[str, Any] | None) -> list[str]:
    """ticket_board.router_assignable_roles: the active Implementation owners that do implementation work."""
    impl = implementation_stage(cfg) or {}
    roles = {r["name"]: r for r in (cfg or {}).get("roles", [])}
    return sorted(o for o in impl.get("owners", [])
                  if roles.get(o, {}).get("active") and has_kind(roles[o], IMPLEMENTER))


def out_of_scope(cfg: dict[str, Any] | None, ticket: dict[str, Any], role: str) -> str | None:
    """Why Router may not give `ticket` to `role` (SYRD-678): its routing_scope's flag is not set; else None."""
    flag = scope_flag(next((r for r in (cfg or {}).get("roles", []) if r.get("name") == role), None))
    # A declared flag is in workflow_flags; a builtin gate (needs_inspection, ...) is the ticket's own field.
    if flag is None or (ticket.get("workflow_flags") or {}).get(flag, ticket.get(flag)) is True:
        return None
    return (f"router gives {role} only work flagged {flag}, and {ticket.get('id')} is not; choose another owner, "
            f"or return it to the Director, who may flag it or assign {role} directly")


def validate_router(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """The document's half of ticket_board.validate_router_declaration, rule for rule."""
    roles, transitions, stages = cfg["roles"], cfg["transitions"], cfg["stages"]
    need(all(r["name"] == ROUTER_ROLE for r in roles if r["kind"] == ROUTER_KIND),
         "invalid router role: only the role named router may be of kind router")
    need(all(r["kind"] == ROUTER_KIND for r in roles if ASSIGN in r["capabilities"]),
         "invalid router role: router_assign belongs to the router role alone")
    router = router_role(cfg)
    if router is None:
        need(not any(t["action"] == RETURN for t in transitions),
             "invalid router role: router_return needs the router role")
        return
    impl = implementation_stage(cfg) or {}
    need(sorted(router["capabilities"]) == [ASSIGN], "invalid router role: its capabilities are exactly router_assign")
    need(router.get("serial") is not True, "invalid router role: router never holds a serial slot")
    need(not any(ROUTER_ROLE in s["owners"] or (s["notify"]["kind"] == "fixed_role" and s["notify"]["role"] == ROUTER_ROLE)
                 for s in stages), "invalid router role: router owns no stage")
    need(not any((ROUTER_ROLE in t["actors"] or t.get("relays_decision_of") == ROUTER_ROLE) and t["action"] != RETURN
                 for t in transitions), "invalid router role: router_return is its only move")
    need(all(t["actors"] == [ROUTER_ROLE] and t["from"] == impl.get("name") and t["to"] == TRIAGE_STAGE
             and t["primitive"] == "move" and t["owner_scoped"] and t["require_reason"] and not t["require_commit"]
             and not t.get("allow_no_code") and t["clear_signoffs"] == [] and t.get("relays_decision_of") is None
             for t in transitions if t["action"] == RETURN),
         "invalid router role: router_return moves Implementation to analysis, router alone, owner-scoped, "
         "with a reason, and changes nothing else")
    if not router["active"]:
        return
    need(router.get("target") is not None,
         "invalid router role: an active router needs a runtime and pane target to be told about work")
    need(impl.get("notify", {}).get("kind") in {"assignee", "stage_owner_fallback"},
         "invalid router role: Implementation must notify its assignee for router to be told")
    triage = next((s for s in stages if s["name"] == TRIAGE_STAGE), None)
    need(triage is not None and not triage["terminal"] and triage["owners"] == ["director"]
         and triage["notify"]["kind"] in {"assignee", "stage_owner_fallback"},
         "invalid router role: router returns work to analysis, which must be the director's alone and announced")
    need(sum(t["action"] == RETURN for t in transitions) == 1, "invalid router role: an active router needs its router_return")
    need(any(t["action"] == KICK_BACK and t["from"] == impl.get("name") and t["to"] == TRIAGE_STAGE
             and t["primitive"] == "move" and t["owner_scoped"] and t["require_reason"]
             and sorted(t["actors"]) == sorted(impl.get("owners", []))
             for t in transitions),
         "invalid router role: every Implementation owner needs implementer_kick_back to analysis, "
         "owner-scoped with a reason, to hand back a misrouted ticket")


def _arguments(operation: str, payload: dict[str, Any]) -> tuple[str, str]:
    keys = _PAYLOAD_KEYS[operation]
    if set(payload) != keys or not all(isinstance(payload[k], str) for k in keys):
        raise ValueError(f"{operation} takes exactly {', '.join(sorted(keys))}, as text")
    value = payload["assignee" if operation == ASSIGN else "reason"].strip()
    if operation == RETURN and not value:
        raise ValueError("router_return requires a non-empty reason")
    if operation == RETURN and len(value) > REASON_MAX_CHARS:
        raise ValueError(f"router_return reason is limited to {REASON_MAX_CHARS} characters")
    return (value.lower() if operation == ASSIGN else value), payload["revision"].strip()


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None) -> dict[str, Any]:
    """Check what this layer can, then let the database decide and record it."""
    if not ticket_id:
        raise ValueError(f"{operation} acts on one ticket, named in the path")
    value, revision = _arguments(operation, payload)
    cfg = app.workflow_configuration()
    if not router_holds(cfg, None, caller_role):
        raise PermissionError(f"{caller_role} cannot call {operation}: only the active router role may")
    ticket = app.get_ticket(ticket_id)
    if not router_holds(cfg, ticket.get("state"), ticket.get("assignee")) or ticket.get("assignee") != caller_role:
        raise PermissionError(f"{ticket_id} is not {caller_role} work: it is {ticket.get('state')} / {ticket.get('assignee')}")
    if operation == ASSIGN and value not in assignable_roles(cfg):
        raise PermissionError(f"router may assign only to an active owner of Implementation "
                              f"({', '.join(assignable_roles(cfg))}), not {value or '<empty>'}")
    refusal = out_of_scope(cfg, ticket, value) if operation == ASSIGN else None
    if refusal:
        raise PermissionError(refusal)
    if operation == ASSIGN and cfg.get("scheduling"):
        held = (app.serial_reservations() or {}).get(value)
        if held and held.get("ticket") != ticket_id:
            raise PermissionError(f"{value} already has {held['ticket']} active; on this pull-scheduled board Router "
                                  f"cannot queue work behind it: choose an owner with no active ticket, or return "
                                  f"{ticket_id} to the Director")
    if revision != str((ticket.get("routing") or {}).get("revision") or ""):
        raise ValueError(f"stale revision for {ticket_id}: the ticket changed after router read it; read it again")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            conn.execute(f"SELECT ticket_board.{operation}(%s, %s, %s)", (ticket_id, value, revision))
    return {"ticket": app.get_ticket(ticket_id)}
