#!/usr/bin/env python3
"""Phase-0 guard: workflow config mirrors the hardcoded board workflow."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import frontend_script_core  # noqa: E402
from scripts.ticket_board.app import STATES, TicketBoardApp  # noqa: E402


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"


@dataclass(frozen=True)
class ActionOwnership:
    owner_scoped: bool
    director_override: bool


def run(args: list[str], *, input_text: str | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    if capture:
        return subprocess.run(args, input=input_text, text=True, capture_output=True, check=True)
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def conninfo(socket_dir: Path, port: int, dbname: str) -> str:
    return f"host={socket_dir} port={port} dbname={dbname} user=postgres"


def psql(conn: str, sql: str) -> str:
    return run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn], input_text=sql).stdout.strip()


def function_def(conn: str, signature: str) -> str:
    return psql(conn, f"SELECT pg_get_functiondef('{signature}'::regprocedure);")


def normalize_sql_body(body: str) -> str:
    return "\n".join(line.rstrip() for line in body.strip("\n").splitlines())


def state_rank_values_from_db(conn: str, stages: list[dict[str, object]]) -> dict[str, int | None]:
    values_sql = ", ".join(f"('{stage['name']}')" for stage in stages)
    rows = json.loads(
        psql(
            conn,
            f"""
SELECT jsonb_object_agg(state_name, ticket_board.state_rank(state_name) ORDER BY state_name)::text
FROM (VALUES {values_sql}) AS states(state_name);
""",
        )
    )
    return {str(key): value for key, value in rows.items()}


def transition_target_role_case_from_db(conn: str) -> str:
    definition = function_def(conn, "ticket_board.transition_target_role(text,text)")
    match = re.search(r"SELECT CASE\n(?P<body>.*?)\n\s+ELSE NULL", definition, re.S)
    if not match:
        raise AssertionError(f"could not extract transition_target_role CASE body:\n{definition}")
    return normalize_sql_body(match.group("body"))


def transition_whitelist_from_db(conn: str) -> list[tuple[str, list[str]]]:
    definition = function_def(conn, "ticket_board.workflow_transition_allowed_hardcoded(text,text)")
    body_match = re.search(r"AS \$[^$]*\$\n(?P<body>.*?)\n\$[^$]*\$", definition, re.S)
    if not body_match:
        raise AssertionError(f"could not extract workflow_transition_allowed_hardcoded body:\n{definition}")
    whitelist: list[tuple[str, list[str]]] = []
    for line in normalize_sql_body(body_match.group("body")).splitlines():
        line_match = re.fullmatch(
            r"\s*(?:SELECT\s+|OR\s+)?\(p_from_state = '([^']+)' AND p_to_state IN \((.*?)\)\);?",
            line,
        )
        if not line_match:
            raise AssertionError(f"could not parse transition whitelist line: {line!r}")
        from_stage = line_match.group(1)
        to_stages = re.findall(r"'([^']+)'", line_match.group(2))
        whitelist.append((from_stage, to_stages))
    return whitelist


def action_roles_from_db(conn: str) -> dict[str, list[str]]:
    rows = json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(pg_get_functiondef(p.oid) ORDER BY p.proname, p.oid)::text
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'ticket_board';
""",
        )
    )
    action_roles: dict[str, list[str]] = {}
    for definition in rows:
        for match in re.finditer(
            r"require_actor\(\s*ARRAY\[(?P<roles>.*?)\]\s*,\s*'(?P<action>[^']+)'\s*\)",
            definition,
            re.S,
        ):
            action = match.group("action")
            roles = re.findall(r"'([^']+)'", match.group("roles"))
            existing = action_roles.setdefault(action, roles)
            if existing != roles:
                raise AssertionError(f"conflicting require_actor roles for {action}: {existing} != {roles}")
    return dict(sorted(action_roles.items()))


def _normalize_sql_fragment(fragment: str) -> str:
    return re.sub(r"\s+", " ", fragment.strip()).lower()


def _split_sql_csv(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "'":
            current.append(char)
            if index + 1 < len(value) and value[index + 1] == "'":
                current.append(value[index + 1])
                index += 2
                continue
            in_quote = not in_quote
        elif not in_quote and char == "(":
            depth += 1
            current.append(char)
        elif not in_quote and char == ")":
            depth = max(depth - 1, 0)
            current.append(char)
        elif not in_quote and depth == 0 and char == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _assignee_expressions(definition: str) -> set[str]:
    expressions = {"(select tickets.assignee", "(select assignee", "tickets.assignee"}
    for match in re.finditer(
        r"SELECT\s+(?P<select>.*?)\s+INTO\s+(?P<into>.*?)\s+FROM\b",
        definition,
        re.S | re.I,
    ):
        select_items = _split_sql_csv(match.group("select"))
        into_items = _split_sql_csv(match.group("into"))
        for select_item, into_item in zip(select_items, into_items, strict=False):
            if re.search(r"\b(?:tickets\.)?assignee\b", select_item, re.I):
                expressions.add(_normalize_sql_fragment(into_item))
                expressions.add(_normalize_sql_fragment(select_item))
    return expressions


def _is_assignee_expression(value: str, assignee_expressions: set[str]) -> bool:
    normalized = _normalize_sql_fragment(value)
    return normalized in assignee_expressions or bool(
        re.search(r"\bselect\b.*\b(?:tickets\.)?assignee\b", normalized)
    )


def _ownership_actor_vars(definition: str) -> set[str]:
    assignee_expressions = _assignee_expressions(definition)
    owner_actor_vars: set[str] = set()
    for if_match in re.finditer(r"\bIF\s+(?P<condition>.*?)\s+THEN\b", definition, re.S | re.I):
        condition = if_match.group("condition")
        for match in re.finditer(
            r"(?P<left>\([^()]*\bSELECT\b.*?\)|\b[\w.]+\b)\s*"
            r"(?:<>|!=|IS\s+DISTINCT\s+FROM)\s*"
            r"(?P<right>\([^()]*\bSELECT\b.*?\)|\b[\w.]+\b)",
            condition,
            re.S | re.I,
        ):
            left = match.group("left")
            right = match.group("right")
            left_normalized = _normalize_sql_fragment(left)
            right_normalized = _normalize_sql_fragment(right)
            if left_normalized in {"actor", "comment_actor"} and _is_assignee_expression(right, assignee_expressions):
                owner_actor_vars.add(left_normalized)
            if right_normalized in {"actor", "comment_actor"} and _is_assignee_expression(left, assignee_expressions):
                owner_actor_vars.add(right_normalized)
    return owner_actor_vars


def action_ownership_from_db(conn: str) -> dict[str, ActionOwnership]:
    rows = json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(pg_get_functiondef(p.oid) ORDER BY p.proname, p.oid)::text
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'ticket_board';
""",
        )
    )
    action_ownership: dict[str, ActionOwnership] = {}
    for definition in rows:
        actions = {
            match.group("action")
            for match in re.finditer(
                r"require_actor\(\s*ARRAY\[.*?\]\s*,\s*'(?P<action>[^']+)'\s*\)",
                definition,
                re.S,
            )
        }
        if not actions:
            continue

        owner_actor_vars = _ownership_actor_vars(definition)
        owner_scoped = bool(owner_actor_vars)
        director_override = any(
            re.search(
                rf"(\b{re.escape(actor_var)}\s*<>\s*'director'|'director'\s*<>\s*{re.escape(actor_var)}\b)",
                definition,
            )
            for actor_var in owner_actor_vars
        )
        ownership = ActionOwnership(owner_scoped=owner_scoped, director_override=director_override)
        for action in actions:
            existing = action_ownership.setdefault(action, ownership)
            if existing != ownership:
                raise AssertionError(f"conflicting ownership checks for {action}: {existing} != {ownership}")
    return dict(sorted(action_ownership.items()))


def assert_frontend_columns_are_dynamic() -> None:
    assert "const COLUMNS = [" not in frontend_script_core.SCRIPT_CORE
    assert "function normalizeBoardColumns(" in frontend_script_core.SCRIPT_CORE
    assert "function boardColumns()" in frontend_script_core.SCRIPT_CORE
    assert "visibleColumns() {\n      return boardColumns().filter" in frontend_script_core.SCRIPT_CORE


def compile_columns(stages: list[dict[str, object]]) -> list[dict[str, str]]:
    return [{"key": str(stage["name"]), "label": str(stage["display_label"])} for stage in stages]


def compile_state_rank(stages: list[dict[str, object]]) -> dict[str, int]:
    return {str(stage["name"]): int(stage["rank"]) for stage in stages}


def compile_transition_target_role(stages: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for stage in stages:
        owners = stage["owner_roles"]
        if stage["name"] == "in_progress":
            lines.append("        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')")
        elif len(owners) == 1:
            lines.append(f"        WHEN p_state = '{stage['name']}' THEN '{owners[0]}'")
    return "\n".join(lines)


def compile_transition_whitelist(
    stages: list[dict[str, object]],
    transitions: list[dict[str, object]],
    db_whitelist: list[tuple[str, list[str]]],
) -> str:
    stage_rank = {str(stage["name"]): int(stage["rank"]) for stage in stages}
    stage_order = [from_stage for from_stage, _to_stages in db_whitelist]
    grouped: dict[str, set[str]] = defaultdict(set)
    for transition in transitions:
        grouped[str(transition["from_stage"])].add(str(transition["to_stage"]))
    assert set(grouped) == set(stage_order), grouped
    assert set(stage_order) == set(stage_rank), stage_rank
    lines = []
    for index, (from_stage, expected_to) in enumerate(db_whitelist):
        actual_to = grouped[from_stage]
        assert actual_to == set(expected_to), (from_stage, actual_to, expected_to)
        quoted = ", ".join(f"'{to_stage}'" for to_stage in expected_to)
        suffix = " OR" if index < len(db_whitelist) - 1 else ""
        lines.append(f"            (OLD.state = '{from_stage}' AND NEW.state IN ({quoted})){suffix}")
    return "\n".join(lines)


def compile_action_roles(transitions: list[dict[str, object]]) -> dict[str, list[str]]:
    action_roles: dict[str, list[str]] = {}
    for transition in transitions:
        action = str(transition["action_name"])
        roles = [str(role) for role in transition["allowed_roles"]]
        existing = action_roles.setdefault(action, roles)
        if existing != roles:
            raise AssertionError(f"conflicting allowed_roles for {action}: {existing} != {roles}")
    return dict(sorted(action_roles.items()))


def compile_owner_scoped_actions(transitions: list[dict[str, object]]) -> set[str]:
    owner_scoped: set[str] = set()
    action_scopes: dict[str, bool] = {}
    for transition in transitions:
        action = str(transition["action_name"])
        scoped = bool(transition["owner_scoped"])
        existing = action_scopes.setdefault(action, scoped)
        if existing != scoped:
            raise AssertionError(f"conflicting owner_scoped values for {action}: {existing} != {scoped}")
        if scoped:
            owner_scoped.add(action)
    return owner_scoped


def load_workflow_config(conn: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    stages = json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(
    jsonb_build_object(
        'name', name,
        'display_label', display_label,
        'rank', rank,
        'owner_roles', owner_roles,
        'entry_gate_field', entry_gate_field,
        'gate_skip_to', gate_skip_to,
        'exit_signoff_field', exit_signoff_field,
        'is_terminal', is_terminal
    )
    ORDER BY rank
)::text
FROM ticket_board.workflow_stages;
""",
        )
    )
    transitions = json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(
    jsonb_build_object(
        'from_stage', from_stage,
        'to_stage', to_stage,
        'action_name', action_name,
        'allowed_roles', allowed_roles,
        'owner_scoped', owner_scoped
    )
    ORDER BY from_stage, to_stage, action_name
)::text
FROM ticket_board.workflow_transitions;
""",
        )
    )
    return stages, transitions


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-workflow-config.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_workflow_config_test"
        admin_conn = conninfo(socket_dir, port, dbname)

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            stages, transitions = load_workflow_config(admin_conn)
            db_transition_whitelist = transition_whitelist_from_db(admin_conn)
            transition_actions = {str(transition["action_name"]) for transition in transitions}
            db_action_roles = action_roles_from_db(admin_conn)
            db_action_ownership = action_ownership_from_db(admin_conn)
            app_columns = TicketBoardApp(database_url=admin_conn).workflow_columns()

            assert tuple(stage["name"] for stage in stages) == STATES
            assert_frontend_columns_are_dynamic()
            assert compile_columns(stages) == app_columns
            assert compile_state_rank(stages) == state_rank_values_from_db(admin_conn, stages)
            assert compile_transition_target_role(stages) == transition_target_role_case_from_db(admin_conn)
            assert compile_transition_whitelist(stages, transitions, db_transition_whitelist) == normalize_sql_body(
                "\n".join(
                    f"            (OLD.state = '{from_stage}' AND NEW.state IN ({', '.join(repr(to_stage) for to_stage in to_stages)}))"
                    f"{' OR' if index < len(db_transition_whitelist) - 1 else ''}"
                    for index, (from_stage, to_stages) in enumerate(db_transition_whitelist)
                )
            )
            assert compile_action_roles(transitions) == {
                action: roles
                for action, roles in db_action_roles.items()
                if action in transition_actions
            }
            assert compile_owner_scoped_actions(transitions) == {
                action
                for action, ownership in db_action_ownership.items()
                if action in transition_actions and ownership.owner_scoped
            }

            terminal_states = {stage["name"] for stage in stages if stage["is_terminal"]}
            assert terminal_states == {"done", "cancelled"}
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False, capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
