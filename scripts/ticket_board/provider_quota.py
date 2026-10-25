"""What a provider says when an account's usage quota is exhausted, and when it resets (SYRD-644).

Recognised only as measured (tests/fixtures/syrd644_provider_quota_evidence.json):

* **Claude Code** ends such a turn with no Stop hook. It fires `StopFailure`
  instead, whose `error` is `rate_limit`, and writes an API-error record to the
  session transcript carrying `quotaLimits: {status: "rejected", resetsAt,
  rateLimitType}`. A `rate_limit` with no rejected quota -- a throttle, as an
  API key sees it -- is not an exhausted quota, and neither is `overloaded`
  (capacity), an authentication or billing failure, or a server or network
  error: those keep their own handling (`classify_claude`).
* **Codex** has no failure hook and runs no Stop hook for an errored turn. It
  draws `■ You’ve hit your usage limit. ... try again at Oct 25th, 2026 12:33
  AM.` (wrapped at the pane's width; "try again later." when it gives no time)
  at the margin above its empty prompt (`codex_quota`). Its time names no zone:
  it is the Codex process's local time. Under an API key it draws `■ Quota
  exceeded. Check your plan and billing details.`: credit that no clock
  restores, a billing failure as Claude's `billing_error` is, not a quota.

Nothing here decides a hold, keeps state, or trusts a screen's age: the caller
ties what it finds to a turn or a hook event (quota_hold).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

#: Bounds on what is kept of a provider's own error text; a prompt is never read.
MESSAGE_CHARS = 240

CLAUDE_KINDS = {
    "overloaded": "capacity",
    "authentication_failed": "auth",
    "oauth_org_not_allowed": "auth",
    "account_on_hold": "auth",
    "verification_required": "auth",
    "cloud_credential_error": "auth",
    "billing_error": "billing",
    "server_error": "server",
    "unknown": "unknown",
}
_CLAUDE_RESET = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>am|pm)\s*\((?P<zone>[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\)")
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
CODEX_PROMPT = "›"
_CODEX_QUOTA = re.compile(r"^■ You[’']ve hit your usage limit\.")
_CODEX_BILLING = re.compile(r"^■ Quota exceeded\. Check your plan and billing details\.")
_CODEX_RESET = re.compile(r"try again at (?P<mon>[A-Z][a-z]{2}) (?P<day>\d{1,2})(?:st|nd|rd|th), (?P<year>\d{4}) "
                          r"(?P<hour>\d{1,2}):(?P<minute>\d{2}) (?P<ampm>AM|PM)")


def _clock(hour: int, ampm: str) -> int:
    return hour % 12 + (12 if ampm.lower() == "pm" else 0)


def parse_claude_reset(text: str, observed: float) -> float | None:
    """When "resets 10:20pm (America/Toronto)" or "resets Oct 3, 4am (America/Toronto)" means, after `observed`."""
    match = _CLAUDE_RESET.search(text or "")
    if not match:
        return None
    try:
        zone = ZoneInfo(match["zone"])
    except (KeyError, ValueError):
        return None
    seen = datetime.fromtimestamp(observed, zone)
    hour, minute = _clock(int(match["hour"]), match["ampm"]), int(match["minute"] or 0)
    if match["mon"]:
        month = _MONTHS.get(match["mon"])
        if month is None:
            return None
        when = datetime(seen.year, month, int(match["day"]), hour, minute, tzinfo=zone)
        if when < seen - timedelta(days=1):  # "Jan 2" read in late December
            when = when.replace(year=seen.year + 1)
    else:
        when = seen.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= seen:
            when += timedelta(days=1)
    return when.timestamp()


def classify_claude(error: str, quota: dict[str, Any] | None, message: str, *, observed: float) -> dict[str, Any]:
    """A StopFailure's kind, and for an exhausted quota its reset: from the hook's error, the transcript record's
    `quotaLimits` and the provider's own error line, as the pane hook recorded them."""
    quota = quota if isinstance(quota, dict) else {}
    message = " ".join(str(message or "").split())[:MESSAGE_CHARS]
    if error == "rate_limit" and quota.get("status") == "rejected":
        reset = quota.get("resetsAt")
        reset = float(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) else parse_claude_reset(message, observed)
        return {"kind": "quota", "reset_at": reset, "limit": str(quota.get("rateLimitType") or ""), "message": message}
    kind = "rate_limit" if error == "rate_limit" else CLAUDE_KINDS.get(error, "other")
    return {"kind": kind, "reset_at": None, "limit": "", "message": message}


def codex_quota(lines: list[str], cursor_y: int, *, zone: str | None = None) -> dict[str, Any] | None:
    """The usage-limit message ending a Codex screen above its prompt, joined across its wrapped lines; an API key's
    out-of-credit line there is `billing`; None otherwise."""
    if not 0 <= cursor_y < len(lines) or not lines[cursor_y].startswith(CODEX_PROMPT):
        return None
    block: list[str] = []
    for line in reversed(lines[:cursor_y]):
        if not line.strip():
            if block:
                break
            continue
        block.insert(0, line.rstrip())
        if line.startswith("■ ") or line.startswith(CODEX_PROMPT):
            break
    if not block or not (_CODEX_QUOTA.match(block[0]) or _CODEX_BILLING.match(block[0])):
        return None
    message = " ".join(" ".join(block).split())[:MESSAGE_CHARS]
    if _CODEX_BILLING.match(block[0]):
        return {"kind": "billing", "reset_at": None, "limit": "", "message": message}
    reset = None
    match = _CODEX_RESET.search(message)
    if match and match["mon"] in _MONTHS:
        try:
            tz = ZoneInfo(zone) if zone else None
            local = datetime(int(match["year"]), _MONTHS[match["mon"]], int(match["day"]),
                             _clock(int(match["hour"]), match["ampm"]), int(match["minute"]))
            reset = (local.replace(tzinfo=tz) if tz else local.astimezone()).timestamp()
        except (KeyError, ValueError):
            reset = None
    # How many quota lines the screen holds: a newly emitted one adds one; an old one still showing does not.
    count = sum(1 for line in lines if _CODEX_QUOTA.match(line))
    return {"kind": "quota", "reset_at": reset, "limit": "", "message": message, "count": count}


def local_zone() -> str | None:
    """The zone a provider process on this host reports in, unless TZ says otherwise; None is the host's own."""
    return os.environ.get("TZ") or None


def iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).isoformat()
