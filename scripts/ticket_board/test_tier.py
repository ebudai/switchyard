"""A ticket's test tier (SYRD-836): the Director's declared instruction, `partial` or `full`, with an optional reason.

Which tier of a project's tests a ticket needs is decided by the Director, per ticket. It is kept with the ticket's
other Director-only workflow settings, as two keys of its workflow_flags (`test_tier`, `test_tier_reason`), and is
written only through the Director's gate write (set_declared_flags, pgu1050): the same authority check, API and
history. Each change is a Director comment and a field-audit row. The tier is shown in the ticket's text read and
JSON, and in its transition notices, so whoever runs the tests sees it without reading the body.

Nothing here enforces it: the board never runs or verifies tests. Audit's sign-off says what it ran.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Callable

TIERS = ("partial", "full")
MAX_REASON = 300
#: The notices that hand a ticket to a role: a stage it entered, or a ticket assigned to it.
HANDOFF_KINDS = frozenset({"transition"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def patch(tier: str | None, reason: str = "") -> dict[str, Any]:
    """The gate-write patch that sets `tier` with an optional one-line `reason`, or clears it when `tier` is None."""
    reason = str(reason or "").strip()
    if tier is None:
        if reason:
            raise ValueError("a cleared test tier takes no reason")
        return {"test_tier": None}
    if tier not in TIERS:
        raise ValueError(f"a test tier is partial or full, not {tier!r}")
    if len(reason) > MAX_REASON or _CONTROL.search(reason):
        raise ValueError(f"a test tier reason is one line of at most {MAX_REASON} characters")
    return {"test_tier": tier, **({"test_tier_reason": reason} if reason else {})}


def fields(flags: Any) -> dict[str, str]:
    """The ticket's `test_tier` and `test_tier_reason` for its JSON: "" when the Director set none."""
    flags = flags if isinstance(flags, dict) else {}
    tier = flags.get("test_tier") if flags.get("test_tier") in TIERS else ""
    return {"test_tier": tier, "test_tier_reason": str(flags.get("test_tier_reason") or "") if tier else ""}


def handoff_line(tier: dict[str, Any] | None) -> str:
    """The sentence a hand-off carries: "Test tier: full (reason)." -- or "" for a ticket with none."""
    if not isinstance(tier, dict) or tier.get("tier") not in TIERS:
        return ""
    reason = _CONTROL.sub(" ", str(tier.get("reason") or "")).strip()
    return f"Test tier: {tier['tier']}" + (f" ({reason})" if reason else "") + "."


def handoff(conn: Any, ticket_id: str, kind: str, message: str) -> str:
    """A transition notice as delivered: the ticket's test tier said after it, read when it is sent."""
    if kind not in HANDOFF_KINDS:
        return message
    try:
        result = conn.execute("SELECT ticket_board.ticket_test_tier(%s)", (ticket_id,))
        row = result.fetchone() if result is not None and hasattr(result, "fetchone") else None
    except Exception:  # the notice itself is never held back by its tier line
        return message
    value = (next(iter(row.values())) if isinstance(row, dict) else row[0]) if row else None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return message
    line = handoff_line(value)
    if not line:
        return message
    return f"{message.rstrip()}{' ' if message.rstrip().endswith(('.', '!', '?')) else '. '}{line}"


def add_create_arguments(create: argparse.ArgumentParser, free_text: Callable[..., Any]) -> None:
    """create-ticket's --test-tier and --test-tier-reason (Director only: the board refuses anyone else)."""
    create.add_argument("--test-tier", choices=TIERS, default=None,
                        help="the test tier this ticket needs, a Director instruction to whoever runs its tests")
    free_text(create, "--test-tier-reason", default="", help="why, in one line (with --test-tier)")


def add_parser(subparsers: Any, free_text: Callable[..., Any]) -> None:
    """set-test-tier <ticket> --tier partial|full [--reason TEXT] | --clear: the Director changes it later."""
    parser = subparsers.add_parser("set-test-tier", help="set or clear a ticket's test tier (Director only)")
    parser.add_argument("ticket_id")
    value = parser.add_mutually_exclusive_group(required=True)
    value.add_argument("--tier", choices=TIERS, help="the test tier this ticket needs")
    value.add_argument("--clear", action="store_true", help="remove the ticket's test tier")
    free_text(parser, "--reason", default="", help="why, in one line (with --tier)")


def _refused(exc: ValueError) -> Exception:
    """The CLI's own refusal, printed without a traceback, before anything is sent."""
    try:
        from . import write_client
    except ImportError:  # pragma: no cover - the CLI run outside the package
        import write_client  # type: ignore[no-redef]
    return write_client.TicketBoardWriteError(str(exc))


def create_fields(args: argparse.Namespace) -> dict[str, str]:
    """The create request's test tier fields, or none. A reason without a tier is refused before anything is sent."""
    try:
        if args.test_tier is None:
            if str(args.test_tier_reason or "").strip():
                raise ValueError("--test-tier-reason needs --test-tier")
            return {}
        patch(args.test_tier, args.test_tier_reason)
    except ValueError as exc:
        raise _refused(exc) from None
    return {"test_tier": args.test_tier, "test_tier_reason": str(args.test_tier_reason or "").strip()}


def set_patch(args: argparse.Namespace) -> dict[str, Any]:
    """set-test-tier's gate-write patch."""
    try:
        return patch(None if args.clear else args.tier, args.reason)
    except ValueError as exc:
        raise _refused(exc) from None


def create_patch(payload: dict[str, Any], caller_role: str | None) -> dict[str, Any] | None:
    """The board's check of a create request's tier: the Director's alone, and a valid patch; None when absent."""
    tier = payload.get("test_tier")
    reason = payload.get("test_tier_reason", "")
    if tier in (None, ""):
        if str(reason or "").strip():
            raise ValueError("a test tier reason needs a test tier")
        return None
    if caller_role != "director":
        raise PermissionError("the test tier can only be set by director")
    if not isinstance(tier, str) or not isinstance(reason, str):
        raise ValueError("a test tier and its reason are text")
    return patch(tier, reason)
