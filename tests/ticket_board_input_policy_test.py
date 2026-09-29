#!/usr/bin/env python3
"""SYRD-501: ticket input validation and blocker reference policy, without a database."""

from __future__ import annotations

import ast
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import app as app_module  # noqa: E402
from scripts.ticket_board import ticket_input_policy as policy  # noqa: E402
from scripts.ticket_board.app import STATES, TicketBoardApp  # noqa: E402

PUBLIC = ("TICKET_ID_PATTERN", "EXTERNAL_BLOCKER_PATTERN", "valid_ticket_id", "normalize_blocker_ref", "is_external_blocker")
MOVED_METHODS = (
    "_require_text",
    "_require_body",
    "_require_plain_string",
    "_validate_comments",
    "_validate_blocked_by",
    "_validate_blockers",
    "_enforce_blocked_reason_rule",
)
KEPT_METHODS = ("_validate_blocker_ticket_states", "_validate_state", "_validate_assignee", "_validate_commit_hash")


def refusal(operation: Any) -> str:
    try:
        operation()
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected a refusal")


def test_app_keeps_public_names_and_no_forwarders() -> None:
    for name in PUBLIC:
        assert getattr(app_module, name) is getattr(policy, name), name
    for name in MOVED_METHODS:
        assert not hasattr(TicketBoardApp, name), name
    for name in KEPT_METHODS:
        assert callable(getattr(TicketBoardApp, name)), name
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported == {"__future__", "re", "typing"}, imported


def test_blocker_reference_forms() -> None:
    assert policy.normalize_blocker_ref(" syrd-2 ") == "SYRD-2"
    assert policy.normalize_blocker_ref(" Other : mefp-5 ") == "other:MEFP-5"
    assert policy.normalize_blocker_ref("Operator:Eric") == "operator:eric"
    assert policy.valid_ticket_id(" syrd-2 ") and not policy.valid_ticket_id("2-SYRD")
    assert policy.is_external_blocker("other:MEFP-5") and policy.is_external_blocker("operator:eric")
    assert not policy.is_external_blocker("operator:MEFP-5") and not policy.is_external_blocker("SYRD-2")


def test_blocked_by_normalizes_and_refuses() -> None:
    raw = [" syrd-2 ", "SYRD-2", "Other:mefp-5", "other:MEFP-5", "Operator:Eric"]
    assert policy.validate_blocked_by(raw, "SYRD-9", "SYRD") == ["SYRD-2", "other:MEFP-5", "operator:eric"]
    for empty in (None, "", "null"):
        assert policy.validate_blocked_by(empty, "SYRD-9", "SYRD") == []
    cases = [
        ("SYRD-2", "blocked_by must be a list of ticket IDs"),
        ([7], "blocked_by entries must be strings"),
        ([" "], "blocked_by entries must not be empty"),
        (["bad id"], "invalid blocked_by ticket id: bad id"),
        (["operator:MEFP-5"], "invalid blocked_by ticket id: operator:MEFP-5"),
        (["Syrd:syrd-4"], "external blocker syrd:SYRD-4 names a ticket on this board; block on SYRD-4 instead"),
        (["other:SYRD-4"], "external blocker other:SYRD-4 names a ticket on this board; block on SYRD-4 instead"),
        (["syrd-9"], "ticket cannot be blocked_by itself"),
    ]
    for value, text in cases:
        assert refusal(lambda: policy.validate_blocked_by(value, "SYRD-9", "SYRD")) == text, value
    assert policy.validate_blocked_by(["other:SYRD-4"], "MEFP-1", "MEFP") == ["other:SYRD-4"]


def test_blockers_dedupe_and_refuse() -> None:
    raw = [{"id": " syrd-2 ", "resolved": 1}, {"id": "SYRD-2", "resolved": False}, {"id": "other:MEFP-5"}]
    assert policy.validate_blockers(raw, "SYRD-9", "SYRD") == [
        {"id": "SYRD-2", "resolved": True},
        {"id": "other:MEFP-5", "resolved": False},
    ]
    assert policy.validate_blockers("null", "SYRD-9", "SYRD") == []
    assert refusal(lambda: policy.validate_blockers({"id": "SYRD-2"}, "SYRD-9", "SYRD")) == "blockers must be a list"
    assert refusal(lambda: policy.validate_blockers(["SYRD-2"], "SYRD-9", "SYRD")) == "blockers entries must be objects"
    assert refusal(lambda: policy.validate_blockers([{}], "SYRD-9", "SYRD")) == "blocked_by entries must not be empty"
    assert refusal(lambda: policy.validate_blockers([{"id": "syrd-9"}], "SYRD-9", "SYRD")) == "ticket cannot be blocked_by itself"


def test_comments_text_and_reason_rule() -> None:
    assert policy.validate_comments([{"who": "main", "text": " hi ", "ts": "t", "urgent": "yes", "extra": 1}]) == [
        {"who": "main", "text": " hi ", "ts": "t", "urgent": True}
    ]
    assert refusal(lambda: policy.validate_comments({})) == "comments must be a list"
    assert refusal(lambda: policy.validate_comments(["x"])) == "comment entries must be objects"
    assert refusal(lambda: policy.validate_comments([{"who": "m", "text": " ", "ts": "t"}])) == "comment.text must be a non-empty string"
    assert refusal(lambda: policy.validate_comments([{"who": "m", "text": "x"}])) == "comment.ts must be a non-empty string"
    assert policy.require_text(" x ", "title") == " x "
    assert refusal(lambda: policy.require_text(3, "title")) == "title must be a non-empty string"
    assert policy.require_body(None) == "" and policy.require_body(" b ") == " b "
    assert refusal(lambda: policy.require_body(3)) == "body must be a string"
    assert policy.require_plain_string(None, "note") == ""
    assert refusal(lambda: policy.require_plain_string(1, "note")) == "note must be a string"
    policy.enforce_blocked_reason_rule([], "")
    policy.enforce_blocked_reason_rule(["SYRD-2"], "why")
    assert refusal(lambda: policy.enforce_blocked_reason_rule(["SYRD-2"], " ")) == "blocked_reason must be non-empty when blocked_by is set"


class Result:
    def __init__(self, one: Any = None, many: list[dict[str, str]] | None = None) -> None:
        self.one = one
        self.many = many or []

    def fetchone(self) -> Any:
        return self.one

    def fetchall(self) -> list[dict[str, str]]:
        return self.many


class Conn:
    def __init__(self, log: list[Any], states: dict[str, str]) -> None:
        self.log = log
        self.states = states

    def __enter__(self) -> Conn:
        self.log.append("begin")
        return self

    def __exit__(self, *_args: Any) -> bool:
        self.log.append("end")
        return False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Result:
        if "set_config('ticket_board.caller_role'" in sql:
            self.log.append(("caller", params[0]))
        elif "FROM ticket_board.tickets WHERE id = ANY" in sql:
            self.log.append(("states", list(params[0])))
            return Result(many=[{"id": i, "state": s} for i, s in self.states.items() if i in params[0]])
        elif "ticket_board.create_ticket(" in sql:
            self.log.append(("create", list(params[4]), params[5]))
            return Result(one={"id": "ABC-7"})
        elif "ticket_board.set_blockers(" in sql:
            self.log.append(("set_blockers", params[0], list(params[1]), params[2]))
        elif "ticket_board.release_external_blocker(" in sql:
            self.log.append(("release", params[0], params[1]))
        elif "ticket_board.add_comment(" in sql:
            self.log.append(("comment", params[1]))
        else:
            self.log.append(("sql", " ".join(sql.split())[:40]))
        return Result()


def fake_app(log: list[Any]) -> TicketBoardApp:
    app = TicketBoardApp.__new__(TicketBoardApp)
    app.database_url = ""
    app.ticket_prefix = "ABC"
    app._workflow_states_cache = STATES
    states = {"ABC-2": "in_progress", "ABC-3": "done"}
    app._pg_connect = lambda: Conn(log, states)
    current = {"id": "ABC-9", "blocked_by": ["ABC-2"], "blocked_reason": "waits", "state": "in_progress", "assignee": "main"}
    app._pg_get_ticket = lambda ticket_id, _conn: log.append(("readback", ticket_id)) or dict(current, id=ticket_id)
    return app


def create(app: TicketBoardApp, **extra: Any) -> dict[str, Any]:
    fields: dict[str, Any] = dict(
        title=" New ", body="", screenshot=None, screenshots=None, state="analysis", assignee="main", blocked_by=None,
        parent_id="", implementation="", audit_prompt="", audit_signoff=False, needs_audit=True, needs_inspection=False,
        inspector_signoff=False, needs_user_signoff=False, user_signoff=False, regression=False, comments=[],
        blocked_reason="", commit_hash="", commit_exempt=False,
    )
    fields.update(extra)
    return app.create_ticket_record(**fields)


def test_create_validates_before_connecting_and_checks_states_after_role() -> None:
    log: list[Any] = []
    app = fake_app(log)
    for extra, text in [
        ({"blocked_by": ["ABC-2"], "blocked_reason": " "}, "blocked_reason must be non-empty when blocked_by is set"),
        ({"blocked_by": ["x:ABC-2"], "blocked_reason": "w"}, "external blocker x:ABC-2 names a ticket on this board; block on ABC-2 instead"),
        ({"blocked_by": ["abc-0"], "blocked_reason": "w"}, "ticket cannot be blocked_by itself"),
        ({"comments": [{"who": "", "text": "t", "ts": "t"}]}, "comment.who must be a non-empty string"),
        ({"title": " "}, "title must be a non-empty string"),
    ]:
        assert refusal(lambda: create(app, caller_role="main", **extra)) == text, extra
        assert log == [], (extra, log)
    create(
        app,
        blocked_by=["abc-2", "ABC-2", "x:SYRD-1", "operator:eric"],
        blocked_reason="why",
        caller_role="main",
        comments=[{"who": "audit", "text": "note", "ts": "t"}],
    )
    assert log == [
        "begin",
        ("caller", "main"),
        ("states", ["ABC-2"]),
        ("create", ["ABC-2", "x:SYRD-1", "operator:eric"], "why"),
        ("caller", "audit"),
        ("comment", "note"),
        ("readback", "ABC-7"),
        "end",
    ], log
    for blocker, text in [("ABC-3", "terminal tickets cannot block other tickets: ABC-3 is done"), ("ABC-8", "blocker ticket not found: ABC-8")]:
        log.clear()
        assert refusal(lambda: create(app, blocked_by=[blocker], blocked_reason="w", caller_role="main")) == text
        assert log == ["begin", ("caller", "main"), ("states", [blocker]), "end"], log


def test_update_orders_role_read_validate_states_write() -> None:
    log: list[Any] = []
    app = fake_app(log)
    app._pg_update_ticket("abc-9", {"blocked_by": ["abc-2", "ABC-2", "x:SYRD-1"], "blocked_reason": "r"}, caller_role="main")
    assert log[:5] == ["begin", ("caller", "main"), ("readback", "ABC-9"), ("states", ["ABC-2"]), ("set_blockers", "ABC-9", ["ABC-2", "x:SYRD-1"], "r")], log
    for patch, text in [
        ({"blocked_by": ["abc-9"]}, "ticket cannot be blocked_by itself"),
        ({"blocked_reason": ""}, "blocked_reason must be non-empty when blocked_by is set"),
        ({"blocked_reason": 1}, "blocked_reason must be a string"),
        ({"blocked_by": ["x:abc-4"], "blocked_reason": "r"}, "external blocker x:ABC-4 names a ticket on this board; block on ABC-4 instead"),
    ]:
        log.clear()
        assert refusal(lambda: app._pg_update_ticket("ABC-9", patch, caller_role="main")) == text, patch
        assert log == ["begin", ("caller", "main"), ("readback", "ABC-9"), "end"], (patch, log)


def test_read_revalidates_with_the_app_prefix() -> None:
    app = fake_app([])
    app._active_work_delivery = lambda _row: {}
    row = {
        "id": "ABC-9", "title": "T", "body": None, "assignee": "main", "state": "in_progress",
        "blocked_by": [" abc-2 ", "ABC-2", "x:SYRD-1"], "blockers": json.dumps([{"id": "abc-2", "resolved": 1}, {"id": "ABC-2"}]),
        "parent_id": None, "origin_project": None, "external_source_ref": None, "blocked_reason": "r",
        "queued_for_assignee": None, "queued_behind_ticket": None, "implementation": None, "audit_prompt": None,
        "audit_signoff": 0, "needs_audit": 1, "needs_inspection": 0, "inspector_signoff": 0, "needs_user_signoff": 0,
        "user_signoff": 0, "regression": 0, "manually_controlled": 0, "commit_hash": None, "commit_exempt": 0,
        "workflow_flags": None, "created_text": "c", "updated_text": "u", "active_work_highlight": 0,
        "active_work_owner_role": None, "active_work_notified_at": None, "awaiting_role": None,
        "comments": json.dumps([{"who": "main", "text": "hi", "ts": "t"}]), "screenshots": None,
    }
    ticket = app._pg_row_to_ticket(row)
    assert (ticket["blocked_by"], ticket["blockers"], ticket["body"], ticket["origin_project"]) == (
        ["ABC-2", "x:SYRD-1"], [{"id": "ABC-2", "resolved": True}], "", ""
    )
    assert ticket["comments"] == [{"who": "main", "text": "hi", "ts": "t", "urgent": False}]
    assert refusal(lambda: app._pg_row_to_ticket(dict(row, blocked_by=["x:ABC-1"]))) == (
        "external blocker x:ABC-1 names a ticket on this board; block on ABC-1 instead"
    )
    assert refusal(lambda: app._pg_row_to_ticket(dict(row, title=""))) == "title must be a non-empty string"


def test_reason_verbs_refuse_before_connecting() -> None:
    log: list[Any] = []
    app = fake_app(log)
    assert refusal(lambda: app.implementer_kick_back("ABC-9", " ", caller_role="main")) == "reason must be a non-empty string"
    assert refusal(lambda: app.start_task("ABC-9", 3, caller_role="main")) == "note must be a string"
    assert refusal(lambda: app.dismiss_notification_by_key(ticket_id="", target_role="x", caller_role="main")) == "ticket_id must be a non-empty string"
    assert refusal(lambda: app.file_report(title="r", body="", origin_project=5)) == "origin_project must be a string"
    assert log == []
    app.release_external_blocker("ABC-9", ref=" Other:syrd-5 ", reason="done", caller_role="main")
    assert log == ["begin", ("caller", "main"), ("release", "ABC-9", "other:SYRD-5"), ("readback", "ABC-9"), "end"], log


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_input_policy_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
