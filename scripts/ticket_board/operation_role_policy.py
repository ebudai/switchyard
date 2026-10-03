"""The legacy, pre-declarative table of which roles may call each operation.

Built once at import from the board unit's environment
(TICKET_BOARD_IMPLEMENTER_ROLES, TICKET_BOARD_DRAFT_ROLES,
TICKET_BOARD_OPERATION_ALLOWED_ROLES) and the app's caller roles. The handler
decides admission: a declared workflow's capabilities first, this table only
for a board that declares none.
"""

from __future__ import annotations

import json
import os
import re
from typing import Mapping

from .app import CALLER_ROLES as APP_CALLER_ROLES

CALLER_ROLES = set(APP_CALLER_ROLES)
DEFAULT_IMPLEMENTER_ROLES = ("main", "app", "ops", "perf", "research")


def _role_set_from_env(name: str, default: tuple[str, ...]) -> set[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set(default)
    roles = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return roles or set(default)


IMPLEMENTER_ROLES = _role_set_from_env("TICKET_BOARD_IMPLEMENTER_ROLES", DEFAULT_IMPLEMENTER_ROLES)
DRAFT_ROLES = _role_set_from_env("TICKET_BOARD_DRAFT_ROLES", ())
TASK_ROLES = IMPLEMENTER_ROLES | {"director", "audit", "inspector"}
DEFAULT_OPERATION_ALLOWED_ROLES = {
    "create_ticket": {"director", "user"},
    "file_bug": IMPLEMENTER_ROLES | {"audit"},
    "release_draft": DRAFT_ROLES | {"director", "user"},
    "route": {"director"},
    "reassign": {"director"},
    "force_move": {"director"},
    "director_edit": {"director"},
    "override_move": {"director"},
    "start_work": IMPLEMENTER_ROLES,
    "submit_to_inspection": IMPLEMENTER_ROLES,
    "submit_to_audit": IMPLEMENTER_ROLES,
    "submit_to_audit_without_commit": IMPLEMENTER_ROLES,
    "implementer_kick_back": IMPLEMENTER_ROLES,
    "request_commit_exempt": IMPLEMENTER_ROLES,
    "start_task": TASK_ROLES,
    "complete_task": TASK_ROLES,
    "await_role": CALLER_ROLES - {"user"},
    # The same permission as await_role, because it IS await_role plus the
    # sentence that explains it (SYRD-133).
    "request_dependency": CALLER_ROLES - {"user"},
    # Bounded recovery for a ticket whose owner left it behind. Control
    # authority is what actually admits it; this is the pre-declarative table
    # (SYRD-133).
    "recover_stalled_ticket": {"director"},
    "clear_awaiting_role": CALLER_ROLES - {"user"},
    "inspector_sign_off": {"inspector"},
    "inspector_kick_back": {"inspector"},
    "audit_sign_off": {"audit"},
    "audit_kick_back": {"audit"},
    "director_dat_sign_off": {"director"},
    "director_dat_kick_back": {"director"},
    "user_sign_off": {"user"},
    "user_reopen": {"user"},
    "mark_done": {"director"},
    "defer": {"director"},
    "cancel": {"director"},
    "set_manually_controlled": {"director"},
    "set_blockers": {"director"},
    # SYRD-270: the other half of an external blocker, so the same authority.
    "release_external_blocker": {"director"},
    # SYRD-537: deferring optional reminders is less than holding the ticket.
    "snooze_reminders": {"director"},
    "clear_reminder_snooze": {"director"},
    # SYRD-541: the integrator's bounded size decisions.
    "approve_size_exception": {"director"},
    "enable_size_review": {"director"},
    "measure_size": {"director"},
    "add_comment": CALLER_ROLES,
    "edit_fields": CALLER_ROLES,
    "crop_attachment": {"director", "user"},
    "merge": {"director"},
    "dismiss_notification": {"director"},
}

#: The two operations that exist to move a ticket the declared workflow will
#: not. A document may not grant them as capabilities -- that would be declaring
#: its own bypass -- so the database admits them by control-role identity
#: instead (SYRD-49, SYRD-78). This layer has to ask the same question, or it
#: refuses an operation it advertises and the database would accept (SYRD-180).
CONTROL_OVERRIDE_OPERATIONS = {"force_move", "override_move"}

#: Operations that are COMPOSITIONS of declarable capabilities. A declared
#: workflow cannot grant these by name: they are deliberately absent from
#: `workflow_config.CAPABILITIES`, so no document can name one and none ever
#: has. Checking the operation name here therefore refused them on every
#: declared board, whatever the document said -- which is how SYRD-133's
#: documented equivalence for `request_dependency` came to hold on legacy
#: boards only, and how SYRD-193's App ended a turn with a Director question
#: and no durable wait (SYRD-194).
#:
#: Admission requires ALL of the parts, and that broadens nothing: a role
#: holding the set can already reach the identical end state by calling the
#: parts in sequence. A role holding only one half is still refused, and the
#: database re-checks each part under its own capability regardless.
COMPOSED_OPERATION_CAPABILITIES = {
    # "The same permission as await_role, because it IS await_role plus the
    # sentence that explains it" -- the comment on OPERATION_ALLOWED_ROLES
    # below, which the legacy table honours and the declarative path did not.
    # `add_comment` is named too because the sentence is half of what it does.
    "request_dependency": frozenset({"add_comment", "await_role"}),
    # Releasing a blocker is setting the ticket's blockers (SYRD-270); the
    # database checks set_blockers itself.
    "release_external_blocker": frozenset({"set_blockers"}),
    # A reminder snooze silences a subset of what holding a ticket silences, for
    # a bounded time, so it takes the hold's authority; the database checks
    # set_manually_controlled itself (SYRD-537).
    "snooze_reminders": frozenset({"set_manually_controlled"}),
    "clear_reminder_snooze": frozenset({"set_manually_controlled"}),
    # Approving growth into main, or turning the review on, is the integrator's
    # decision: the capability that closes reviewed work into main (SYRD-541).
    "approve_size_exception": frozenset({"merge"}),
    "enable_size_review": frozenset({"merge"}),
    "measure_size": frozenset({"merge"}),
}

#: SYRD-93: publication is admitted by declared capability, never by role name.
#: This map is the legacy, pre-declarative admission table, so these two
#: operations are deliberately absent from it: a board with no declared workflow
#: has no capability to check, and admitting by name would be the reusable-role
#: contract broken at the first boundary a caller reaches.
PUBLICATION_OPERATIONS = frozenset({"request_publication", "resolve_publication"})


def _copy_operation_role_map(source: Mapping[str, set[str]]) -> dict[str, set[str]]:
    return {operation: set(roles) for operation, roles in source.items()}


def _operation_roles_from_value(value: object, *, operation: str) -> set[str]:
    if isinstance(value, str):
        roles = {item.strip().lower() for item in value.split(",") if item.strip()}
    elif isinstance(value, list):
        roles = set()
        for item in value:
            if not isinstance(item, str):
                raise RuntimeError(f"operation role config for {operation} must contain only role strings")
            normalized = item.strip().lower()
            if normalized:
                roles.add(normalized)
    else:
        raise RuntimeError(f"operation role config for {operation} must be a comma-list or string list")
    unknown_roles = roles - CALLER_ROLES
    if unknown_roles:
        raise RuntimeError(f"operation role config for {operation} references unknown caller roles: {sorted(unknown_roles)}")
    return roles


def _operation_role_map_from_env(name: str, default: Mapping[str, set[str]]) -> dict[str, set[str]]:
    roles_by_operation = _copy_operation_role_map(default)
    raw = os.environ.get(name, "").strip()
    if not raw:
        return roles_by_operation
    if raw.startswith("{"):
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{name} must be a JSON object or operation=roles list")
        entries = decoded.items()
    else:
        parsed: list[tuple[str, str]] = []
        for chunk in re.split(r"[;\n]+", raw):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                raise RuntimeError(f"{name} entry must be operation=role,role: {chunk}")
            operation, roles = chunk.split("=", 1)
            parsed.append((operation.strip(), roles.strip()))
        entries = parsed
    for operation, value in entries:
        if not isinstance(operation, str):
            raise RuntimeError(f"{name} operation names must be strings")
        normalized_operation = operation.strip()
        if normalized_operation not in roles_by_operation:
            raise RuntimeError(f"{name} references unknown operation: {normalized_operation}")
        roles_by_operation[normalized_operation] = _operation_roles_from_value(value, operation=normalized_operation)
    return roles_by_operation


OPERATION_ALLOWED_ROLES = _operation_role_map_from_env(
    "TICKET_BOARD_OPERATION_ALLOWED_ROLES",
    DEFAULT_OPERATION_ALLOWED_ROLES,
)
