#!/usr/bin/env python3
"""SYRD-505: the legacy operation-role table, its import-time environment and the handler that reads it."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import operation_role_policy as policy  # noqa: E402
from scripts.ticket_board import server  # noqa: E402

PUBLIC = (
    "CALLER_ROLES", "DEFAULT_IMPLEMENTER_ROLES", "IMPLEMENTER_ROLES", "DRAFT_ROLES", "TASK_ROLES",
    "DEFAULT_OPERATION_ALLOWED_ROLES", "CONTROL_OVERRIDE_OPERATIONS", "COMPOSED_OPERATION_CAPABILITIES",
    "PUBLICATION_OPERATIONS", "OPERATION_ALLOWED_ROLES",
)
PRIVATE = ("_role_set_from_env", "_copy_operation_role_map", "_operation_roles_from_value", "_operation_role_map_from_env")
DUMP = """
import json, sys
sys.path.insert(0, {root!r})
try:
    from scripts.ticket_board import server as s
except Exception as exc:
    print(json.dumps({{"error": [type(exc).__name__, str(exc)]}}))
else:
    norm = lambda v: sorted(v) if isinstance(v, (set, frozenset)) else v
    print(json.dumps({{
        "callers": norm(s.CALLER_ROLES), "implementers": norm(s.IMPLEMENTER_ROLES), "drafts": norm(s.DRAFT_ROLES),
        "tasks": norm(s.TASK_ROLES), "table": {{op: norm(r) for op, r in s.OPERATION_ALLOWED_ROLES.items()}},
        "defaults_equal": s.OPERATION_ALLOWED_ROLES == s.DEFAULT_OPERATION_ALLOWED_ROLES,
    }}))
"""


def fresh(**env: str) -> dict[str, Any]:
    """Import the server in a new interpreter with exactly this environment."""
    child = subprocess.run(
        [sys.executable, "-c", DUMP.format(root=str(ROOT))],
        capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin", **env},
    )
    return json.loads(child.stdout)


def refusal(operation: Any, kind: type[BaseException] = PermissionError) -> str:
    try:
        operation()
    except kind as exc:
        return str(exc)
    raise AssertionError(f"expected {kind.__name__}")


def test_server_keeps_identical_aliases_and_no_reverse_import() -> None:
    for name in PUBLIC:
        assert getattr(server, name) is getattr(policy, name), name
    for name in PRIVATE:
        assert hasattr(policy, name) and not hasattr(server, name), name
    assert not hasattr(server, "APP_CALLER_ROLES")
    # The live table owns its sets: mutating one must never rewrite the defaults.
    defaults = policy.DEFAULT_OPERATION_ALLOWED_ROLES
    assert all(policy.OPERATION_ALLOWED_ROLES[op] is not defaults[op] for op in defaults)
    tree = ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))
    imported = [node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module != "__future__"]
    assert imported == ["typing", "app"], imported
    assert (policy.CONTROL_OVERRIDE_OPERATIONS, policy.PUBLICATION_OPERATIONS) == (
        {"force_move", "override_move"}, frozenset({"request_publication", "resolve_publication"}),
    )
    assert policy.COMPOSED_OPERATION_CAPABILITIES == {
        "request_dependency": frozenset({"add_comment", "await_role"}),
        "release_external_blocker": frozenset({"set_blockers"}),
        # SYRD-537: a reminder snooze takes the hold's authority.
        "snooze_reminders": frozenset({"set_manually_controlled"}),
        "clear_reminder_snooze": frozenset({"set_manually_controlled"}),
    }


def test_default_environment() -> None:
    got = fresh()
    assert got["callers"] == sorted(("director", "main", "app", "ops", "perf", "audit", "inspector", "research", "user"))
    assert got["implementers"] == ["app", "main", "ops", "perf", "research"] and got["drafts"] == []
    assert got["tasks"] == ["app", "audit", "director", "inspector", "main", "ops", "perf", "research"]
    assert got["defaults_equal"]
    table = got["table"]
    assert table["release_draft"] == ["director", "user"] and table["file_bug"] == ["app", "audit", "main", "ops", "perf", "research"]
    assert table["await_role"] == [r for r in got["callers"] if r != "user"] and table["add_comment"] == got["callers"]
    assert "request_publication" not in table and "resolve_publication" not in table
    # SYRD-537 added snooze_reminders and clear_reminder_snooze, Director only.
    assert table["snooze_reminders"] == ["director"] and table["clear_reminder_snooze"] == ["director"]
    assert len(table) == 41, len(table)


def test_role_environment_is_read_at_import() -> None:
    got = fresh(TICKET_BOARD_IMPLEMENTER_ROLES=" Builder , main ,,", TICKET_BOARD_DRAFT_ROLES="designer,Main")
    assert got["implementers"] == ["builder", "main"] and got["drafts"] == ["designer", "main"]
    assert got["table"]["start_work"] == ["builder", "main"] and got["table"]["release_draft"] == ["designer", "director", "main", "user"]
    assert fresh(TICKET_BOARD_IMPLEMENTER_ROLES=" , ,")["implementers"] == ["app", "main", "ops", "perf", "research"]
    custom = fresh(TICKET_BOARD_CALLER_ROLES="director,designer,builder,audit,user", TICKET_BOARD_IMPLEMENTER_ROLES="builder")
    assert custom["callers"] == ["audit", "builder", "designer", "director", "user"]
    assert custom["table"]["await_role"] == ["audit", "builder", "designer", "director"]


def test_operation_table_overrides_and_refusals() -> None:
    listed = fresh(TICKET_BOARD_OPERATION_ALLOWED_ROLES="mark_done=ops, Director ;route=director;;force_move=ops,director")
    assert listed["table"]["mark_done"] == ["director", "ops"] and listed["table"]["force_move"] == ["director", "ops"]
    assert not listed["defaults_equal"]
    edges = fresh(TICKET_BOARD_OPERATION_ALLOWED_ROLES=";mark_done=ops;\nroute=ops,director;\n")
    assert edges["table"]["mark_done"] == ["ops"] and edges["table"]["route"] == ["director", "ops"], edges
    as_json = fresh(TICKET_BOARD_OPERATION_ALLOWED_ROLES='{"mark_done": ["ops", " Director ", ""], "route": "director,ops"}')
    assert as_json["table"]["mark_done"] == ["director", "ops"] and as_json["table"]["route"] == ["director", "ops"]
    name = "TICKET_BOARD_OPERATION_ALLOWED_ROLES"
    for value, error in [
        ('{"x": 1}', ["RuntimeError", f"{name} references unknown operation: x"]),
        ('["mark_done"]', ["RuntimeError", f'{name} entry must be operation=role,role: ["mark_done"]']),
        ('{"mark_done": ["ops", 3]}', ["RuntimeError", "operation role config for mark_done must contain only role strings"]),
        ('{"mark_done": 3}', ["RuntimeError", "operation role config for mark_done must be a comma-list or string list"]),
        ("teleport=director", ["RuntimeError", f"{name} references unknown operation: teleport"]),
        ("mark_done=wizard", ["RuntimeError", "operation role config for mark_done references unknown caller roles: ['wizard']"]),
        ("mark_done", ["RuntimeError", f"{name} entry must be operation=role,role: mark_done"]),
        ("request_publication=main", ["RuntimeError", f"{name} references unknown operation: request_publication"]),
    ]:
        assert fresh(**{name: value}) == {"error": error}, value
    assert fresh(**{name: '{"mark_done": '})["error"][0] == "JSONDecodeError"


class App:
    def __init__(self, cfg: Any = None, assignee: str = "main") -> None:
        self.cfg, self.assignee = cfg, assignee

    def workflow_configuration(self) -> Any:
        return self.cfg

    def get_ticket(self, ticket_id: str) -> dict[str, str]:
        return {"id": ticket_id, "assignee": self.assignee, "state": "in_progress"}


def admit(app: App, operation: str, role: str, ticket_id: str | None = None) -> None:
    handler = server.TicketBoardHandler.__new__(server.TicketBoardHandler)
    handler.server = types.SimpleNamespace(app=app)
    handler.require_operation_allowed(operation, role, ticket_id)


def test_handler_reads_the_same_legacy_table_objects() -> None:
    table = policy.OPERATION_ALLOWED_ROLES
    saved = set(table["mark_done"])
    try:
        table["mark_done"] = {"ops"}
        admit(App(), "mark_done", "ops")
        assert refusal(lambda: admit(App(), "mark_done", "director")) == "director cannot call mark_done"
    finally:
        table["mark_done"] = saved
    assert server.OPERATION_ALLOWED_ROLES["mark_done"] == saved
    assert refusal(lambda: admit(App(), "no_such_op", "director"), ValueError) == "unknown ticket operation: no_such_op"
    assert refusal(lambda: admit(App(), "request_publication", "director")) == (
        "request_publication requires a declared workflow; this board has none, so there is no capability to admit a caller by"
    )
    saved_start = set(table["start_work"])
    try:
        table["start_work"] = {"main", "ops"}
        admit(App(assignee=" MAIN "), "start_work", "main", "T-1")
        assert refusal(lambda: admit(App(assignee="ops"), "start_work", "main", "T-1")) == (
            "main cannot call start_work for ticket assigned to ops"
        )
        admit(App(assignee="ops"), "start_work", "main")
    finally:
        table["start_work"] = saved_start


def test_declared_workflow_override_and_composed_admission() -> None:
    cfg = {
        "roles": [
            {"name": "director", "active": True, "capabilities": ["merge", "set_blockers", "set_manually_controlled"]},
            {"name": "main", "active": True, "capabilities": ["add_comment", "await_role"]},
            {"name": "app", "active": True, "capabilities": ["add_comment"]},
            {"name": "idle", "active": False, "capabilities": ["add_comment"]},
        ],
        "stages": [], "transitions": [],
    }
    declared = App(cfg)
    for operation, role in [("force_move", "director"), ("override_move", "director"), ("request_dependency", "main"),
                            ("release_external_blocker", "director"), ("add_comment", "app")]:
        admit(declared, operation, role)
    for operation, role in [("force_move", "main"), ("request_dependency", "app"), ("request_dependency", "director"),
                            ("release_external_blocker", "main"), ("request_publication", "main"), ("mark_done", "director")]:
        assert refusal(lambda: admit(declared, operation, role)) == f"{role} cannot call {operation}", (operation, role)
    assert refusal(lambda: admit(declared, "add_comment", "idle")) == "inactive or unknown workflow actor"
    assert refusal(lambda: admit(declared, "add_comment", "ghost")) == "inactive or unknown workflow actor"


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_operation_role_policy_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
