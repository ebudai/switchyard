#!/usr/bin/env python3
"""The board's side of a break-glass grant (SYRD-739): one operation, by token, enforced here and in the database.

A provider safety hold (SYRD-672) can leave work that must be isolated now, while the Director that would do it is
held. The operator then starts a temporary role on another provider (`switchyard break-glass`). Its only authority is
a grant: a random token whose sha256 alone the board keeps, for a fixed set of operations, until a deadline by the
database clock, for ONE operation (pgu1040).

`POST /api/break-glass` takes `{token, operation, args}` and nothing else. It is checked before any caller role, so it
needs no pane registration and lends no role's identity:
1. the database claims the grant (`claim_break_glass`): known token, open, not expired, an operation it allows. The
   claim is committed as `operating` before anything is done, so neither a second request nor a crash mid-way can
   reach a second operation. A refused claim ends the grant; `abort` ends it too;
2. the arguments are checked against the operation's fixed shape;
3. the one operation runs through the board's ordinary path (update_ticket, reassign_ticket) as the Director
   operation it is, so every invariant, trigger and notice of that path applies;
4. the grant is finished completed, with the ticket as it now stands, or refused, with the board's reason.

The vocabulary, and what each may do:
- record   -- `add_comment`: one comment on a ticket;
- isolate  -- `set_manually_controlled`: take one ticket out of automation;
- reassign -- `reassign`: one ticket to another role, with a reason;
- unblock  -- `set_blockers`: remove one named blocker from one ticket.

`GET /api/break-glass` shows the newest grants, never a token or its hash.

The client half (`operate`, and this file run as a script) is what the temporary role runs: it reads the token from
the file its start named in TICKET_BOARD_BREAK_GLASS_TOKEN_FILE, and the board address from TICKET_BOARD_URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PATH = "/api/break-glass"
OPERATIONS = ("record", "isolate", "reassign", "unblock")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
TICKET = re.compile(r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}")
ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
BLOCKER = re.compile(r"(?:[a-z][a-z0-9_]{0,31}:)?[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}|operator:[a-z][a-z0-9_]{0,31}")
#: Each operation's argument shape: name -> (pattern or None for free text, longest text).
SHAPES: dict[str, dict[str, tuple[re.Pattern | None, int]]] = {
    "record": {"ticket": (TICKET, 0), "text": (None, 2000)},
    "isolate": {"ticket": (TICKET, 0)},
    "reassign": {"ticket": (TICKET, 0), "assignee": (ROLE, 0), "reason": (None, 500)},
    "unblock": {"ticket": (TICKET, 0), "blocker": (BLOCKER, 0)},
    "abort": {},
}


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def checked_args(operation: str, args: Any) -> dict[str, str]:
    """The arguments, exactly the operation's shape; ValueError naming what is wrong."""
    shape = SHAPES.get(operation)
    if shape is None:
        raise ValueError(f"{operation!r} is not a break-glass operation ({', '.join(OPERATIONS)}, abort)")
    if not isinstance(args, dict) or set(args) != set(shape):
        raise ValueError(f"{operation} takes exactly {', '.join(sorted(shape)) or 'nothing'}")
    out: dict[str, str] = {}
    for name, (pattern, longest) in shape.items():
        value = args[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{operation}: {name} must be a non-empty string")
        value = value.strip()
        if pattern is not None and not pattern.fullmatch(value):
            raise ValueError(f"{operation}: {name} {value!r} is not a {name}")
        if pattern is None and len(value) > longest:
            raise ValueError(f"{operation}: {name} is longer than {longest} characters")
        out[name] = value
    return out


# -- the board ----------------------------------------------------------------------------------------------------------
def _plain(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _perform(app: Any, grant: int, operation: str, args: dict[str, str]) -> dict[str, Any]:
    """The one operation, through the board's ordinary path, as the Director operation it is."""
    ticket = args["ticket"]
    said = f"Break-glass grant {grant}"
    if operation == "record":
        updated = app.update_ticket(ticket, {"comment": {"who": "director", "text": f"{said}: {args['text']}",
                                                         "urgent": False}}, caller_role="director")
    elif operation == "isolate":
        updated = app.update_ticket(ticket, {"manually_controlled": True}, caller_role="director")
    elif operation == "reassign":
        updated = app.reassign_ticket(ticket, args["assignee"], reason=f"{said}: {args['reason']}",
                                      caller_role="director")
    else:  # unblock
        current = app.get_ticket(ticket)
        blockers = [str(b) for b in current.get("blocked_by") or []]
        if args["blocker"] not in blockers:
            raise ValueError(f"{ticket} is not blocked by {args['blocker']}")
        remaining = [b for b in blockers if b != args["blocker"]]
        updated = app.update_ticket(ticket, {"blocked_by": remaining,
                                             "blocked_reason": str(current.get("blocked_reason") or "") if remaining
                                             else ""}, caller_role="director")
    return {"ticket": ticket, "state": updated.get("state"), "assignee": updated.get("assignee"),
            "manually_controlled": updated.get("manually_controlled"), "blocked_by": updated.get("blocked_by")}


def operate_on_board(app: Any, payload: Any) -> dict[str, Any]:
    """POST /api/break-glass: claim, check, perform, finish. Answers the outcome; never raises for a refusal."""
    if not isinstance(payload, dict) or set(payload) - {"token", "operation", "args"}:
        return {"refused": "a break-glass request is {token, operation, args}"}
    token, operation, args = payload.get("token"), payload.get("operation"), payload.get("args") or {}
    if not isinstance(token, str) or not TOKEN.fullmatch(token) or not isinstance(operation, str):
        return {"refused": "a break-glass request needs its grant's token and an operation"}
    with app._pg_connect() as conn:
        with conn.transaction():
            claim = _plain(conn.execute("SELECT ticket_board.claim_break_glass(%s, %s, %s::jsonb) AS claim",
                                        (digest(token), operation, json.dumps(args if isinstance(args, dict) else {})))
                           .fetchone()["claim"])
    if "grant" not in claim or "operation" not in claim:  # refused, or aborted: the grant has ended
        return claim
    grant = int(claim["grant"])
    try:
        result, completed = _perform(app, grant, operation, checked_args(operation, args)), True
    except (ValueError, PermissionError, KeyError, LookupError) as exc:
        result, completed = {"refused": str(exc)[:1000]}, False
    except Exception as exc:  # noqa: BLE001 - the board's own refusal, whatever its type, ends the grant
        result, completed = {"refused": f"{type(exc).__name__}: {str(exc)[:1000]}"}, False
    with app._pg_connect() as conn:
        with conn.transaction():
            conn.execute("SELECT ticket_board.finish_break_glass(%s, %s, %s::jsonb)", (grant, completed,
                                                                                      json.dumps(result)))
    return {"grant": grant, "state": "completed" if completed else "refused", "result": result,
            **({} if completed else {"refused": result["refused"]})}


def handle(handler: Any, payload: Any) -> None:
    """The server's hook: the outcome as JSON, 200 for a completed operation and 409 for a refusal."""
    from http import HTTPStatus

    outcome = operate_on_board(handler.app, payload)
    if outcome.get("state") == "completed":
        handler.events.notify_change(handler.app.store_signature())
    handler.send_json(outcome, HTTPStatus.OK if outcome.get("state") == "completed" or outcome.get("aborted")
                      else HTTPStatus.CONFLICT)


def status(app: Any, _query: dict[str, list[str]]) -> dict[str, Any]:
    """GET /api/break-glass: the newest grants, without tokens."""
    with app._pg_connect() as conn:
        row = conn.execute("SELECT CASE WHEN to_regprocedure('ticket_board.break_glass_status()') IS NULL THEN NULL "
                           "ELSE ticket_board.break_glass_status() END AS grants").fetchone()
    return {"grants": _plain(row["grants"]) or []}


# -- the role's client --------------------------------------------------------------------------------------------------
def operate(board_url: str, token: str, operation: str, args: dict[str, str], *, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(board_url.rstrip("/") + PATH, method="POST",
                                     data=json.dumps({"token": token, "operation": operation, "args": args}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return {"refused": f"HTTP {exc.code}: {body[:300]}"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="break-glass", description="The break-glass role's one board operation. The grant allows exactly one; "
                                        "after it, or a refusal, the role is ended.")
    parser.add_argument("operation", choices=[*OPERATIONS, "abort"])
    parser.add_argument("--ticket")
    parser.add_argument("--text")
    parser.add_argument("--assignee")
    parser.add_argument("--reason")
    parser.add_argument("--blocker")
    args = parser.parse_args(argv)
    token_file = os.environ.get("TICKET_BOARD_BREAK_GLASS_TOKEN_FILE", "")
    board_url = os.environ.get("TICKET_BOARD_URL", "")
    if not token_file or not board_url:
        print("break-glass: TICKET_BOARD_BREAK_GLASS_TOKEN_FILE and TICKET_BOARD_URL are set only in a break-glass "
              "role's session", file=sys.stderr)
        return 2
    given = {name: getattr(args, name) for name in SHAPES[args.operation]}
    missing = [name for name, value in given.items() if value is None]
    if missing:
        print(f"break-glass: {args.operation} needs --{' --'.join(missing)}", file=sys.stderr)
        return 2
    outcome = operate(board_url, Path(token_file).read_text(encoding="ascii").strip(), args.operation, given)
    print(json.dumps(outcome, indent=1, sort_keys=True))
    return 0 if outcome.get("state") == "completed" or outcome.get("aborted") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
