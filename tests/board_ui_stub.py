"""SYRD-598: the board page's confirmations, in a browser test whose board is an in-memory fake.

The page asks POST /api/ui/delegations what may be done for a ticket (describe), shows the ticket as it is
now (prepare), and then asks for a grant (mint). A grant needs the board's database, which these fakes
stand in for, so here describe and prepare are answered from the fake's own tickets and a mint is refused --
which is how a test sees the confirmation's concrete values, and the page's handling of a refusal, without
a database. Given `perform`, a mint instead returns a token, and the page's action request is taken here:
it must carry exactly that token, and its payload is what `perform` applies to the fake.
tests/board_ui_delegation_browser_test.py runs the whole flow, server and database, on a real board.
"""

from __future__ import annotations

import json
from typing import Any, Callable

REFUSAL = "this in-memory board cannot grant a board UI confirmation"


def field(name: str, kind: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "kind": kind, "label": label, "required": False, **extra}


def blockers_op(ticket: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "set_blockers", "label": "Set blockers", "mode": "delegated", "placement": "blockers",
            "fields": [field("blocked_by", "list", "Blocked by", value=list(ticket.get("blocked_by") or [])),
                       field("blocked_reason", "text", "Blocked reason", value=ticket.get("blocked_reason") or "")]}


def manual_control_op(ticket: dict[str, Any]) -> dict[str, Any]:
    held = bool(ticket.get("manually_controlled"))
    return {"operation": "set_manually_controlled", "mode": "delegated", "placement": "toggle",
            "label": "Release manual control" if held else "Hold under manual control",
            "fields": [field("manually_controlled", "fixed", "Manual control", value=not held)]}


def cancel_op(ticket: dict[str, Any]) -> dict[str, Any]:
    return {"operation": "cancel", "label": "Cancel", "mode": "delegated",
            "fields": [field("reason", "text", "Why", required=True)]}


def stub_board_ui(page: Any, tickets: Callable[[], list[dict[str, Any]]],
                  operations: Callable[[dict[str, Any]], list[dict[str, Any]]],
                  perform: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Answer the page's describe and prepare from `tickets()`; refuse every mint unless `perform` is given.
    Returns what the page sent: each delegation request, and each confirmed action as {"action": ...}."""
    seen: list[dict[str, Any]] = []
    granted: dict[str, tuple[str, str]] = {}

    def act(route: Any) -> None:
        token = route.request.headers.get("x-ticket-board-ui-delegation", "")
        parts = route.request.url.split("/api/tickets/", 1)[1].split("/")
        ticket_id, operation = parts[0], parts[-1]
        if not token:
            route.continue_()  # the User's own action, to the fake's server
            return
        payload = route.request.post_data_json or {}
        seen.append({"action": operation, "ticket_id": ticket_id, "payload": payload,
                     "caller": route.request.headers.get("x-ticket-board-caller-role", "")})
        assert granted.pop(token, None) == (ticket_id, operation), (token, ticket_id, operation)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ticket": perform(ticket_id, operation, payload)}))

    def handle(route: Any) -> None:
        body = route.request.post_data_json or {}
        seen.append(body)
        by_id = {item["id"]: item for item in tickets()}
        source = by_id[body["ticket_id"]]
        if body.get("describe"):
            answer: dict[str, Any] = {"ticket_id": source["id"], "authority": "director", "operations": operations(source)}
        elif body.get("prepare"):
            answer = {"ticket_id": source["id"], "title": source["title"], "state": source["state"],
                      "assignee": source["assignee"], "authority": "director", "ticket_version": "v"}
            target = by_id.get(str((body.get("payload") or {}).get("target_id", "")))
            if target:
                answer["target"] = {"ticket_id": target["id"], "title": target["title"], "state": target["state"]}
        elif perform is None:
            route.fulfill(status=403, content_type="text/plain", body=REFUSAL)
            return
        else:
            token = f"stub-grant-{len(seen)}"
            granted[token] = (source["id"], body["operation"])
            answer = {"token": token, "ticket_id": source["id"], "operation": body["operation"], "authority": "director"}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))

    page.route("**/api/ui/delegations", handle)
    if perform is not None:
        page.route("**/api/tickets/*/actions/*", act)
    return seen
