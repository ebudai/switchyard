"""A role that does more than one kind of work, and Router's scope for it (SYRD-678).

A role's `kind` is its identity: what it is presented as, which onboarding it
gets, which pane it runs in, which worker pool it belongs to. It stays singular.
What a role may *do* is its kind plus any `additional_kinds` it declares, and
that is what the board asks wherever it means "may hold Implementation work":
owning an implementation stage, being remembered as an implementer, being
Router's choice, claiming pulled work.

Only one combination is admitted: a draft role (a designer) that also
implements. It keeps owning Draft and releasing drafts as a designer, and can
be assigned Implementation work and submit it to Audit like any implementer;
SYRD-673 returns Audit's findings to it because it produced the revision.

`routing_scope` narrows Router, not the Director: Router may give the role only
tickets whose named gate flag the Director has set (a designer takes design
work only). The Director's own assignment is not limited by it.

Every rule here is also the database's (`ticket_board.role_has_kind`,
`validate_declared_workflow`, `router_assign`); this module asks first so a
document is refused before it is sent.
"""

from __future__ import annotations

from typing import Any, Callable

IMPLEMENTER = "implementer"
#: The combinations a role may declare: its kind -> the further kinds it may also do.
ADDITIONAL_KINDS = {"draft": frozenset({IMPLEMENTER})}


def role_kinds(role: dict[str, Any]) -> set[str]:
    """What the role may do: its kind, plus the additional kinds it declares.

    Total over any input: the stage checks ask this before `validate_role_kinds` has judged the declaration, so a
    malformed one (a dict, a list, null among the kinds) adds nothing here and is refused there, by name.
    """
    extra = role.get("additional_kinds")
    return {str(role.get("kind") or ""), *(k for k in (extra if isinstance(extra, list) else []) if isinstance(k, str))}


def has_kind(role: dict[str, Any] | None, kind: str) -> bool:
    """ticket_board.role_has_kind: whether `role` does `kind` work, by its kind or an additional one."""
    return role is not None and kind in role_kinds(role)


def scope_flag(role: dict[str, Any] | None) -> str | None:
    """The gate flag a ticket must carry for Router to give it to `role`, or None when Router may give it anything."""
    scope = (role or {}).get("routing_scope")
    flag = scope.get("flag") if isinstance(scope, dict) else None
    return flag if isinstance(flag, str) else None


def validate_role_kinds(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """`additional_kinds` and `routing_scope`, when a role declares them; absent changes nothing."""
    flags = cfg.get("flags") or {}
    for role in cfg["roles"]:
        name = role["name"]
        if "additional_kinds" in role:
            extra = role["additional_kinds"]
            need(isinstance(extra, list) and bool(extra) and all(isinstance(k, str) for k in extra)
                 and len(set(extra)) == len(extra),
                 f"additional_kinds must be a non-empty list of distinct kinds: {name}")
            allowed = ADDITIONAL_KINDS.get(role["kind"], frozenset())
            need(set(extra) <= allowed,
                 f"additional_kinds: a {role['kind']} role may also be {', '.join(sorted(allowed)) or 'nothing else'}, "
                 f"not {', '.join(sorted(set(extra) - allowed))}: {name}")
        if "routing_scope" in role:
            scope = role["routing_scope"]
            need(isinstance(scope, dict) and set(scope) == {"flag"} and isinstance(scope.get("flag"), str),
                 f"routing_scope must be {{\"flag\": <gate flag>}}: {name}")
            need(scope["flag"] in flags and flags[scope["flag"]]["kind"] == "gate",
                 f"routing_scope names an undeclared gate flag {scope['flag']!r}: {name}")
            need(has_kind(role, IMPLEMENTER),
                 f"routing_scope limits what Router gives an Implementation owner; {name} does no implementation work")
        if cfg.get("scheduling") is not None and role.get("ephemeral") is True and has_kind(role, IMPLEMENTER):
            need("routing_scope" not in role,
                 f"{name} has a routing_scope and would claim pulled work of any kind; make it non-ephemeral "
                 "or drop the scope")
