"""Role policy an older plan derives from the roles it records (SYRD-543).

`operation_allowed_roles` (2026-08-29) and `audit_roles` (2026-08-31) joined the
provision plan a week before `PLAN_BASELINE_FIELDS` was frozen, and that set
listed them, so a real plan written before them -- otto's, measured 2026-10-04 --
could be neither upgraded nor torn down. Neither names the tenant: provisioning
computes both from the implementer and audit roles the plan already records. A
migration therefore takes them from a plan built with the document's OWN
recorded roles, and only when that plan reproduces those recorded role lists;
anything provisioning would not have produced for them stays unresolved, with
the reason, rather than being given a policy it never had.

Imports nothing from `project_provision` when it loads: that module imports
this one, and reaches back only when a function runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

#: Derived from the plan's recorded roles, never identity.
PLAN_DERIVED_ROLE_FIELDS = frozenset({"audit_roles", "operation_allowed_roles"})
#: The recorded role lists a derived role field must be reproducible from.
PLAN_RECORDED_ROLE_FIELDS = ("implementer_roles", "draft_roles", "caller_roles", "assignee_roles")
NO_REFERENCE = "no reference plan could be built for it (the plan records no usable project, owner_user or owner_home)"


def _provision() -> Any:
    try:
        from . import project_provision
    except ImportError:  # pragma: no cover - supports direct script execution
        import project_provision  # type: ignore[no-redef]
    return project_provision


def role_reference(raw: Mapping[str, object], reference: Any) -> tuple[Any, str]:
    """A plan built with the document's own recorded roles, or None and why not.

    Built by `build_plan` from the reference's identity and the document's
    implementer roles, with a designer and an auditor exactly when the document
    records them, then held to the document: every recorded role list must come
    out as the same set of roles, or nothing is derived from it.
    """
    if reference is None:
        return None, NO_REFERENCE
    recorded: dict[str, tuple[str, ...]] = {}
    for name in PLAN_RECORDED_ROLE_FIELDS:
        value = raw.get(name)
        if not isinstance(value, (list, tuple)) or not all(isinstance(role, str) for role in value):
            return None, f"the plan records no usable {name} to derive it from"
        recorded[name] = tuple(value)
    try:
        built = _provision().build_plan(
            project=reference.project,
            project_name=reference.project_name,
            owner_user=reference.owner_user,
            owner_home=Path(reference.owner_home),
            ticket_prefix=reference.ticket_prefix,
            database=reference.database,
            port=reference.port,
            implementer_roles=recorded["implementer_roles"],
            include_designer=bool(recorded["draft_roles"]),
            include_audit="audit" in recorded["caller_roles"],
        )
    except SystemExit as exc:
        return None, f"provisioning refuses the plan's recorded roles ({exc})"
    differ = [name for name in PLAN_RECORDED_ROLE_FIELDS if set(getattr(built, name)) != set(recorded[name])]
    if differ:
        return None, (
            f"the plan's recorded {', '.join(differ)} are not what provisioning gives for its implementer and "
            "audit roles, so its audit and operation roles cannot be derived from them"
        )
    return built, ""


def unresolved_plan_field_reason(name: str, raw: Mapping[str, object], reference: Any) -> str:
    """Why `migrate_plan_document` left `name` unresolved: the real reason, for the refusal to say."""
    if name in _provision().PLAN_BASELINE_FIELDS:
        return "it records this tenant's own identity or provenance, which is never derived; re-provision the project"
    if name in PLAN_DERIVED_ROLE_FIELDS:
        return role_reference(raw, reference)[1]
    if reference is None:
        return NO_REFERENCE + "; re-provision the project"
    return "it could not be completed from the reference plan"
