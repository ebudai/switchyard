"""The phone bridge's message and its first transport, Pushover (SYRD-714).

A transport is anything with `name` and `send(message, credential) -> Outcome`; a later self-hosted ntfy adapter
implements the same two. The bridge decides what to send and when; a transport only delivers and says how it went:

- `sent`, with the provider's receipt, only for HTTP 200 whose body is exactly a success: `"status": 1` and a request
  id of the provider's own form. A 200 without that is `dead`, never retried: it may have been delivered.
- `retry` for what may pass: no answer within the timeout, a connection that failed, HTTP 429 and 5xx.
- `dead` for what will not: any other 4xx (a bad credential, a refused message) and anything unexpected, a
  redirect included.

Nothing a transport says carries the credential: every detail is cut short and has both secrets, and anything
shaped like one, replaced, before it reaches a log, the outbox or a terminal. The credential travels only in the
POST body, over HTTPS to the fixed endpoint, never in a URL, a header or a process argument.

`message_for` builds the text from the event alone: project, ticket, a short title, the kind and a fixed action
summary, nothing else of the ticket. Untrusted text has control, format and bidirectional characters removed and
whitespace collapsed; Pushover is sent `html=0`, so markup stays literal.
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, NamedTuple

from scripts.ticket_board.phone_text import ad_hoc_message, clean_text

ENDPOINT = "https://api.pushover.net/1/messages.json"
TIMEOUT_SECONDS = 10.0
MAX_ANSWER_BYTES = 64 << 10
#: An application token or user key, as Pushover issues them.
CREDENTIAL = re.compile(r"[A-Za-z0-9]{30}")
RECEIPT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: How long the provider keeps an undelivered message: an old one is no longer worth a person's attention.
TTL_SECONDS = 3 * 86400
TITLE_LIMIT, BODY_LIMIT, TEXT_LIMIT = 100, 400, 80

LABELS = {
    "user_review": ("needs your review", "waiting for you in User Review"),
    "operator_blocker": ("waiting on you", "blocked until operator:{detail} acts"),
    "operation_failed": ("host operation failed", "privileged action {detail} did not succeed; see the ticket"),
    "project_outage": ("board is down", "the board has not answered for {detail} minutes"),
    "test": ("test notification", "a test from switchyard-user-notify; nothing needs you"),
}


class Outcome(NamedTuple):
    kind: str  # sent, retry or dead
    detail: str
    receipt: str = ""


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str = ""
    url_title: str = ""
    priority: int = 0


def redact(text: object, secrets: tuple[str, ...] = ()) -> str:
    said = str(text or "")
    for secret in secrets:
        if secret:
            said = said.replace(secret, "[redacted]")
    return CREDENTIAL.sub("[redacted]", said)


def message_for(event: dict[str, Any], *, link: str = "", urgent: bool = False) -> Message:
    kind = event["kind"]
    if kind == "director_ad_hoc":  # the Director's own words, checked by the board and the bridge, attributed
        title, body = ad_hoc_message(clean_text(event.get("project"), 32), event.get("title", ""),
                                     event.get("message", ""), event.get("ticket", ""))
        return Message(title=title, body=body, url=link, url_title=f"Open {event['ticket']}" if link else "",
                       priority=1 if urgent else 0)
    label, action = LABELS[kind]
    action = action.format(detail=event.get("detail") or "?")
    project = clean_text(event.get("project"), 32)
    ticket = clean_text(event.get("ticket"), 24)
    title = clean_text(f"{project}: {ticket + ' ' if ticket else ''}{label}", TITLE_LIMIT)
    headline = clean_text(event.get("title"), TEXT_LIMIT)
    body = clean_text(f"{ticket} {headline} -- {action}" if ticket else action, BODY_LIMIT)
    return Message(title=title, body=body, url=link, url_title=f"Open {ticket or project}" if link else "",
                   priority=1 if kind == "project_outage" else 0)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # the 3xx is answered as an HTTPError: the endpoint is fixed, nothing is followed


def default_post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "switchyard-user-notify"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_ANSWER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read(MAX_ANSWER_BYTES + 1)


class Pushover:
    name = "pushover"

    def __init__(self, *, post: Callable[[str, bytes, float], tuple[int, bytes]] = default_post,
                 endpoint: str = ENDPOINT, timeout: float = TIMEOUT_SECONDS) -> None:
        self.post, self.endpoint, self.timeout = post, endpoint, timeout

    def send(self, message: Message, credential: dict[str, str]) -> Outcome:
        token, user = credential.get("app_token", ""), credential.get("user_key", "")
        secrets = (token, user)
        if not (CREDENTIAL.fullmatch(token) and CREDENTIAL.fullmatch(user)):
            return Outcome("dead", "the Pushover credential is not an application token and user key")
        fields = {"token": token, "user": user, "title": message.title, "message": message.body,
                  "priority": str(message.priority), "ttl": str(TTL_SECONDS), "html": "0"}
        if message.url:
            fields.update(url=message.url, url_title=message.url_title)
        try:
            status, answer = self.post(self.endpoint, urllib.parse.urlencode(fields).encode("utf-8"), self.timeout)
        except (TimeoutError, socket.timeout):
            return Outcome("retry", f"no answer from Pushover within {self.timeout:g}s")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return Outcome("retry", redact(f"Pushover unreachable: {clean_text(reason, 120)}", secrets))
        return self.classify(status, answer, secrets)

    @staticmethod
    def classify(status: int, answer: bytes, secrets: tuple[str, ...] = ()) -> Outcome:
        document: Any = None
        if len(answer) <= MAX_ANSWER_BYTES:
            try:
                document = json.loads(answer.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                document = None
        errors = document.get("errors") if isinstance(document, dict) else None
        said = redact(clean_text("; ".join(str(e) for e in errors) if isinstance(errors, list) else "", 200), secrets)
        if status == 200:
            receipt = document.get("request") if isinstance(document, dict) else None
            if (isinstance(document, dict) and type(document.get("status")) is int and document["status"] == 1
                    and isinstance(receipt, str) and RECEIPT.fullmatch(receipt)):
                return Outcome("sent", "delivered to Pushover", receipt)
            return Outcome("dead", "Pushover answered 200 without a valid receipt; not retried, it may have arrived")
        if status == 429 or 500 <= status <= 599:
            return Outcome("retry", f"Pushover answered HTTP {status}" + (f": {said}" if said else ""))
        return Outcome("dead", f"Pushover refused the message: HTTP {status}" + (f": {said}" if said else ""))
