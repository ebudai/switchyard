"""What a phone notification may say, and how it reads: one set of rules for the board and the host bridge (SYRD-714).

The board previews a Director's ad hoc notification with these rules and the host's bridge renders it with the same
ones, so the preview the Director confirms is the notification that is sent. Pure and dependency-free: the bridge
imports it from root's shared release, the board from its own.

- `clean_text` makes untrusted text one plain line: control, format and bidirectional characters removed, whitespace
  collapsed, cut short.
- `ad_hoc_problem` is why a Director's title, message or reason may not be sent at all: a control character, a URL
  (a link comes only from a ticket of the same board), something shaped like a credential or token, or a reference
  to another board's ticket. Refused, not cleaned: the Director sees what was wrong and says it another way.
- `ad_hoc_message` is the notification's title and body, attributed to `<project> Director`.
"""

from __future__ import annotations

import re
import unicodedata

AD_HOC_TITLE_LIMIT, AD_HOC_MESSAGE_LIMIT, AD_HOC_REASON_LIMIT = 80, 300, 200
#: Anything shaped like a credential: an unbroken run of 20 token characters mixing letters and digits (Pushover keys,
#: board write tokens or a piece of one, digests), a report credential, a private key, or a name that announces a
#: secret.
CREDENTIAL_SHAPES = (
    re.compile(r"(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{20,}"),
    re.compile(r"syrdr1\.", re.IGNORECASE),
    re.compile(r"-----BEGIN"),
    re.compile(r"\b(password|passwd|secret|token|api[_ -]?key|bearer)\b\s*[:=]", re.IGNORECASE),
)
URL_SHAPES = (re.compile(r"\b[a-z][a-z0-9+.-]*://", re.IGNORECASE), re.compile(r"\bwww\.", re.IGNORECASE))
QUALIFIED_TICKET = re.compile(r"\b[a-z][a-z0-9_]*:[A-Z][A-Z0-9]*-[0-9]+\b")
TICKET = re.compile(r"\b([A-Z][A-Z0-9]*)-[0-9]+\b")


def clean_text(text: object, limit: int) -> str:
    """Untrusted text made safe to show in one line: no control, format or bidirectional characters, no newlines."""
    kept = []
    for char in str(text or ""):
        category = unicodedata.category(char)
        kept.append(" " if category[0] == "C" or category in ("Zl", "Zp") else char)  # Cc, Cf incl. bidi, Cs, Co, Cn
    collapsed = " ".join("".join(kept).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit - 1].rstrip() + "…"


def ad_hoc_problem(name: str, text: object, limit: int, prefix: str, *, required: bool = True) -> str:
    """'' when `text` may go in a Director's notification as it is; otherwise what is wrong with it."""
    if not isinstance(text, str):
        return f"{name} must be text"
    if not text.strip():
        return f"{name} is empty" if required else ""
    if len(text) > limit:
        return f"{name} is longer than {limit} characters"
    if any(unicodedata.category(char)[0] == "C" or unicodedata.category(char) in ("Zl", "Zp") for char in text):
        return f"{name} holds a control, format or line-break character"
    if any(shape.search(text) for shape in URL_SHAPES):
        return f"{name} holds a URL: a link comes only from a ticket of this board (--ticket)"
    if any(shape.search(text) for shape in CREDENTIAL_SHAPES):
        return f"{name} holds something shaped like a credential or token"
    if QUALIFIED_TICKET.search(text) or any(found != prefix for found in TICKET.findall(text)):
        return f"{name} names another board's ticket: name only this board's {prefix}-N tickets"
    return ""


def ad_hoc_message(project: str, title: str, message: str, ticket: str = "") -> tuple[str, str]:
    """(title, body) of a Director's ad hoc notification, exactly as the phone shows it."""
    heading = clean_text(f"{project} Director: {title}", 100)
    body = clean_text(f"{message}" + (f" -- {ticket}" if ticket else ""), 400)
    return heading, body
