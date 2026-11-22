"""What the board reads from git, checks and records around the plan gate (SYRD-843).

The database keeps the plan gate's rules (pgu1052) but cannot read git. This module is the service's half:

* `before_transition`, run before a declared transition, refuses before anything changes:
  - a **submission** out of Implementation carrying a commit is measured against its merge-base with main.
    A plan ticket's candidate changes the plan file and, if needed, the decision log, and nothing else, and its plan
    file holds a valid index (`plan_gate.parse_plan_index`), which is recorded for that commit. Any other ticket's
    candidate that changes the plan file is recorded as a finding -- a comment for Audit, a notice for the Director
    -- and so is any candidate, whatever it changes, at which the plan file is over the project's size limit.
    Findings refuse nothing;
  - **closing a plan ticket** reads the index at the commit it is closed on and records it; the database then makes
    that commit the approved plan, and refuses the close when the index was not read;
  - a **documentation commit** that changes the plan file is refused: the plan changes only through a plan ticket.
    The Librarian may move a misplaced ruling into the decision log, never into the plan.
* `perform` carries the Director's `set_ticket_deliverable` and `request_scope_change` to the database.
"""

from __future__ import annotations

from typing import Any

from . import commit_cache
from .plan_gate import FLAG, PlanIndexError, parse_plan_index, policy

OPERATIONS = frozenset({"set_ticket_deliverable", "request_scope_change"})
REASON_MAX_CHARS = 4000


def _git_text(app: Any, commit: str, path: str) -> str | None:
    """`path` at `commit` in the trusted commit cache, or None when it is not there."""
    return commit_cache._first_answer(app.commit_git_dirs, ["cat-file", "blob", f"{commit}:{path}"])


def _merge_base(app: Any, commit: str) -> str | None:
    from . import file_size_policy, size_review

    try:
        reader = size_review._reader(app)
        return file_size_policy.merge_base(reader, size_review._main_commit(reader), commit) or None
    except Exception:  # an unreadable cache measures nothing; the submission's other checks still run
        return None


def _index_at(app: Any, commit: str, plan: dict[str, Any], *, ticket_id: str) -> dict[str, Any]:
    """The plan's index at `commit`, recorded for it; ValueError when the plan is missing or its index is not valid."""
    text = _git_text(app, commit, plan["file"])
    if text is None:
        raise ValueError(f"{ticket_id}: {plan['file']} is not in {commit[:12]}, so the board cannot read the plan's "
                         "index there")
    try:
        index = parse_plan_index(text)
    except PlanIndexError as exc:
        raise ValueError(f"{ticket_id}: the plan at {commit[:12]} has no valid index: {exc}") from None
    import json

    with app._pg_connect() as conn:
        with conn.transaction():
            conn.execute("SELECT ticket_board.record_plan_index(%s, %s, %s::jsonb, %s)",
                         (commit, plan["file"], json.dumps(index), len(text.splitlines())))
    return index


def _note(app: Any, ticket_id: str, commit: str, kind: str, detail: str) -> None:
    with app._pg_connect() as conn:
        with conn.transaction():
            conn.execute("SELECT ticket_board.note_plan_finding(%s, %s, %s, %s)", (ticket_id, commit, kind, detail))


def _commit(app: Any, raw: Any) -> str:
    value = str(raw or "").strip()
    return app._validate_commit_hash(value).lower() if value else ""


def before_transition(app: Any, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> None:
    """Refuse a move the plan does not allow; record what the plan gate keeps."""
    cfg = app.workflow_configuration()
    plan = policy(cfg)
    if plan is None:
        return
    ticket = app.get_ticket(ticket_id)
    stages = {s["name"]: s for s in cfg["stages"]}
    move = next((t for t in cfg["transitions"] if t["action"] == action and t["from"] == ticket.get("state")), None)
    if move is None:
        return
    flags = ticket.get("workflow_flags") or {}
    planned = flags.get(FLAG) is True
    if action == "documentation_sign_off":
        old, new = str(ticket.get("commit_hash") or "").lower(), _commit(app, payload.get("commit_hash"))
        entries = commit_cache.changed_entries(app.commit_git_dirs, old, new) if old and new else None
        if entries and plan["file"] in {path for _o, _n, _s, path in entries}:
            raise ValueError(f"{new[:12]} changes {plan['file']}, and the plan changes only through a plan ticket: "
                             "kick the ticket back with \"plan changes go through a plan ticket\". A ruling found in "
                             f"the work may go to the decision log ({plan['decision_log']}), never into the plan")
        return
    source, target = stages.get(move["from"], {}), stages.get(move["to"], {})
    if planned and target.get("terminal") and move["require_commit"]:
        closing = _commit(app, payload.get("commit_hash")) or str(ticket.get("commit_hash") or "").lower()
        if not closing:
            raise ValueError(f"{ticket_id} is a plan ticket: it is closed on the commit that carries the plan")
        _index_at(app, closing, plan, ticket_id=ticket_id)
        return
    if source.get("kind") != "implementation" or not (move["require_commit"] or payload.get("commit_hash")):
        return
    candidate = _commit(app, payload.get("commit_hash"))
    if not candidate:
        return
    # The size alarm, on any candidate: the plan file as it stands at it, changed by it or not.
    text = _git_text(app, candidate, plan["file"])
    lines = len(text.splitlines()) if text is not None else 0
    if lines > plan["max_lines"]:
        _note(app, ticket_id, candidate, "plan_size",
              f"the plan file {plan['file']} is {lines} lines at {candidate[:12]}, over this project's limit of "
              f"{plan['max_lines']}: detail only the current and next milestone, and move history, status and "
              f"rulings out of it ({plan['decision_log']}, the board, backlog tickets).")
    base = _merge_base(app, candidate)
    entries = commit_cache.changed_entries(app.commit_git_dirs, base, candidate) if base else None
    if entries is None:
        if planned:
            raise ValueError(f"the board cannot read what {candidate[:12]} changes, so it cannot tell that this plan "
                             "ticket changes only the plan and the decision log; nothing was moved")
        return
    paths = {path for _o, _n, _s, path in entries}
    allowed = {plan["file"], plan["decision_log"]}
    if planned:
        outside = sorted(paths - allowed)
        if outside or not paths:
            raise ValueError(f"{candidate[:12]} is a plan ticket's candidate, which changes {plan['file']} and, where a "
                             f"ruling belongs on record, {plan['decision_log']}, and nothing else; it changes "
                             f"{', '.join(outside) or 'nothing'}")
        if plan["file"] in paths or _git_text(app, candidate, plan["file"]) is not None:
            _index_at(app, candidate, plan, ticket_id=ticket_id)
    elif plan["file"] in paths:
        _note(app, ticket_id, candidate, "plan_file_touched",
              f"{candidate[:12]} changes the plan file {plan['file']}, but {ticket_id} is not a plan ticket: plan "
              "changes go through a plan ticket (request-scope-change). Audit: review it as such.")


def _text(payload: dict[str, Any], key: str, limit: int = REASON_MAX_CHARS) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} is text")
    if len(value.strip()) > limit:
        raise ValueError(f"{key} is limited to {limit} characters")
    return value.strip()


def perform(app: Any, operation: str, payload: dict[str, Any], *, caller_role: str,
            ticket_id: str | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS or ticket_id is None:
        raise ValueError(f"{operation} is for one ticket")
    known = {"set_ticket_deliverable": {"deliverable", "unplanned_reason"}, "request_scope_change": {"reason"}}
    unknown = set(payload) - known[operation]
    if unknown:
        raise ValueError(f"{operation} does not take {', '.join(sorted(unknown))}")
    if operation == "set_ticket_deliverable":
        statement = "SELECT ticket_board.set_ticket_deliverable(%s, %s, %s) AS result"
        parameters: tuple = (ticket_id, _text(payload, "deliverable", 64), _text(payload, "unplanned_reason", 2000))
    else:
        statement = "SELECT ticket_board.request_scope_change(%s, %s) AS result"
        parameters = (ticket_id, _text(payload, "reason"))
    with app._pg_connect() as conn:
        with conn.transaction():
            app._pg_set_caller_role(conn, caller_role)
            row = conn.execute(statement, parameters).fetchone()
    result = row["result"] if isinstance(row, dict) else row[0]
    return {"ticket_id": ticket_id, "plan": result}
