"""The Director's wait on the User, and where a workflow lets one stand (SYRD-662).

SYRD-222 sat in Implementation with `awaiting_role=user`: its implementer had
recorded `request-dependency --role user`, so the ticket never reached User
Review and the User never saw it. A wait on the User is now the Director's
decision, made with `await-user` -- a reason and the action or decision asked
of the User -- and only in a stage the declared workflow lists in its optional
`user_waits`. Everything else that names the User is refused by the database,
which checks every rule; this module only carries the request and checks the
document's shape.
"""

from __future__ import annotations

from typing import Any, Callable

OPERATIONS = frozenset({"await_user"})


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    unknown = set(payload) - {"reason", "request"}
    if unknown:
        raise ValueError(f"{operation} does not take {', '.join(sorted(unknown))}")
    reason, request = payload.get("reason", ""), payload.get("request", "")
    if not isinstance(reason, str) or not isinstance(request, str):
        raise ValueError("await-user's reason and request are text")
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.await_user(%s, %s, %s) AS result",
                               (ticket_id, reason, request)).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return {"ticket_id": ticket_id, "user_wait": result}


def validate_user_waits(cfg: dict[str, Any], need: Callable[[bool, str], None]) -> None:
    """`user_waits`, when declared: distinct stages where the Director may wait on the User.

    Absent is none, and stays absent. Implementation is finished through its
    transitions, User Review is where the User already is, and a terminal or
    unowned stage holds no work to wait, so none of them may be listed.
    """
    if "user_waits" not in cfg:
        return
    waits = cfg["user_waits"]
    need(isinstance(waits, list) and all(isinstance(name, str) for name in waits) and len(set(waits)) == len(waits),
         "user_waits must be a list of distinct stage names")
    need(any(role.get("kind") == "user" and role.get("active") is True for role in cfg["roles"]),
         "user_waits needs an active User to wait on")
    stages = {stage["name"]: stage for stage in cfg["stages"]}
    for name in waits:
        stage = stages.get(name)
        need(stage is not None, f"user_waits names an undeclared stage: {name}")
        need(not stage["terminal"] and stage["kind"] != "implementation" and bool(stage["owners"])
             and not (stage["kind"] == "review" and "user" in stage["owners"]),
             f"user_waits cannot name {name}: work leaves Implementation through its transitions and reaches the "
             "User in User Review, and a terminal or unowned stage holds no work to wait")
