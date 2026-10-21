"""The User's UAT instructions and questions, through the board's extension route (SYRD-629).

The Director prepares, for each ticket that will reach the User, three short
parts -- what to do, what they should see, and what to choose then -- as
current ticket data. The board refuses to put a ticket in User Review without
them, shows them at the top of the ticket, and lets the User ask the Director a
question from there without deciding anything. The database checks every
authority and keeps every rule; this module only carries the request.
"""

from __future__ import annotations

from typing import Any

OPERATIONS = frozenset({"set_uat_instructions", "ask_director"})
#: What the User does after trying the steps, when the Director does not say otherwise.
STANDARD_THEN_CHOOSE = ("Accept if that happened. If it did not, or you need help, use Send to Director "
                        "with a short message saying what you saw.")
_FIELDS = {"set_uat_instructions": {"do_this", "you_should_see", "then_choose", "reconfirm"},
           "ask_director": {"question"}}


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    if operation == "ask_director" and caller_role != "user":
        # Admitted by add_comment, which every role holds on a declared board; the question is the User's alone.
        raise PermissionError(f"{caller_role} cannot call ask_director")
    unknown = set(payload) - _FIELDS[operation]
    if unknown:
        raise ValueError(f"{operation} does not take {', '.join(sorted(unknown))}")
    if operation == "set_uat_instructions":
        reconfirm = payload.get("reconfirm", False)
        if type(reconfirm) is not bool:
            raise ValueError("reconfirm is true or false")
        statement = "SELECT ticket_board.set_uat_instructions(%s, %s, %s, %s, %s) AS result"
        parameters: tuple = (ticket_id, _text(payload.get("do_this")), _text(payload.get("you_should_see")),
                             _text(payload.get("then_choose")) or ("" if reconfirm else STANDARD_THEN_CHOOSE),
                             reconfirm)
    else:
        statement = "SELECT ticket_board.ask_director(%s, %s) AS result"
        parameters = (ticket_id, _text(payload.get("question")))
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(statement, parameters).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return {"ticket_id": ticket_id, "uat": result}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("UAT text is a string")
    return value
