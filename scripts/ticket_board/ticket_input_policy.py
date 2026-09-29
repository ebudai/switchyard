"""Ticket input validation and blocker reference policy.

Functions take the board's ticket prefix explicitly. TicketBoardApp keeps
database-backed blocker state checks, caller roles, workflow decisions and
ticket writes.
"""

from __future__ import annotations

import re
from typing import Any

TICKET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
# SYRD-270: a blocker on another board, `<project>:<PREFIX>-<n>`. It never
# resolves by itself; only release_external_blocker removes it.
# SYRD-273: or a person, `operator:<name>`; `operator` is never a project.
EXTERNAL_BLOCKER_PATTERN = re.compile(r"^((?!operator:)[a-z][a-z0-9_]*:[A-Z][A-Z0-9]*-[0-9]+|operator:[a-z][a-z0-9_]*)$")


def valid_ticket_id(ticket_id: str) -> bool:
    return bool(TICKET_ID_PATTERN.fullmatch(str(ticket_id).strip().upper()))


def normalize_blocker_ref(raw: str) -> str:
    """A local id upper case; a qualified reference as `project:PREFIX-N` (as ticket_board.normalize_blocker_ref)."""
    value = str(raw).strip()
    if ":" in value and value.split(":", 1)[0].strip().lower() == "operator":
        return value.lower()
    if ":" in value:
        project, _, ticket = value.partition(":")
        return f"{project.strip().lower()}:{ticket.strip().upper()}"
    return value.upper()


def is_external_blocker(ref: str) -> bool:
    return bool(EXTERNAL_BLOCKER_PATTERN.fullmatch(str(ref)))


def require_text(raw: Any, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return raw


def require_body(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError("body must be a string")
    return raw


def require_plain_string(raw: Any, field: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    return raw


def validate_comments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("comments must be a list")
    comments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("comment entries must be objects")
        comments.append(
            {
                "who": require_text(item.get("who"), "comment.who"),
                "text": require_text(item.get("text"), "comment.text"),
                "ts": require_text(item.get("ts"), "comment.ts"),
                "urgent": bool(item.get("urgent", False)),
            }
        )
    return comments


def validate_blocked_by(raw: Any, ticket_id: str, ticket_prefix: str) -> list[str]:
    if raw in (None, "", "null"):
        return []
    if not isinstance(raw, list):
        raise ValueError("blocked_by must be a list of ticket IDs")
    blocked_by: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("blocked_by entries must be strings")
        blocker_id = normalize_blocker_ref(item)
        if not blocker_id:
            raise ValueError("blocked_by entries must not be empty")
        if not (valid_ticket_id(blocker_id) or is_external_blocker(blocker_id)):
            raise ValueError(f"invalid blocked_by ticket id: {item}")
        if is_external_blocker(blocker_id) and blocker_id.split(":", 1)[1].rsplit("-", 1)[0] == ticket_prefix:
            local_id = blocker_id.split(":", 1)[1]
            raise ValueError(
                f"external blocker {blocker_id} names a ticket on this board; block on {local_id} instead"
            )
        if blocker_id == ticket_id:
            raise ValueError("ticket cannot be blocked_by itself")
        if blocker_id not in blocked_by:
            blocked_by.append(blocker_id)
    return blocked_by


def validate_blockers(raw: Any, ticket_id: str, ticket_prefix: str) -> list[dict[str, Any]]:
    if raw in (None, "", "null"):
        return []
    if not isinstance(raw, list):
        raise ValueError("blockers must be a list")
    blockers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("blockers entries must be objects")
        blocker_id = str(item.get("id", "")).strip().upper()
        normalized_id = validate_blocked_by([blocker_id], ticket_id, ticket_prefix)[0]
        if normalized_id in seen:
            continue
        seen.add(normalized_id)
        blockers.append({"id": normalized_id, "resolved": bool(item.get("resolved"))})
    return blockers


def enforce_blocked_reason_rule(blocked_by: list[str], blocked_reason: str) -> None:
    if blocked_by and not blocked_reason.strip():
        raise ValueError("blocked_reason must be non-empty when blocked_by is set")
