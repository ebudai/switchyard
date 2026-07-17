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
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import frontend_script_core  # noqa: E402
from scripts.ticket_board.app import STATES  # noqa: E402


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"

EXPECTED_STATE_RANK_CASE = """        WHEN 'draft' THEN 0
        WHEN 'backlog' THEN 1
        WHEN 'analysis' THEN 2
        WHEN 'in_progress' THEN 3
        WHEN 'inspection' THEN 4
        WHEN 'audit' THEN 5
        WHEN 'eric_review' THEN 6
        WHEN 'director_review' THEN 7
        WHEN 'done' THEN 8
        WHEN 'cancelled' THEN 9"""

EXPECTED_TRANSITION_TARGET_ROLE_CASE = """        WHEN p_state = 'analysis' THEN 'director'
        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')
        WHEN p_state = 'inspection' THEN 'inspector'
        WHEN p_state = 'audit' THEN 'audit'
        WHEN p_state = 'eric_review' THEN 'director'
        WHEN p_state = 'director_review' THEN 'director'"""

EXPECTED_TRANSITION_WHITELIST = """            (OLD.state = 'backlog' AND NEW.state IN ('analysis', 'in_progress', 'cancelled')) OR
            (OLD.state = 'draft' AND NEW.state IN ('analysis', 'cancelled')) OR
            (OLD.state = 'analysis' AND NEW.state IN ('in_progress', 'backlog', 'cancelled')) OR
            (OLD.state = 'in_progress' AND NEW.state IN ('inspection', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'inspection' AND NEW.state IN ('audit', 'in_progress', 'backlog', 'cancelled')) OR
            (OLD.state = 'audit' AND NEW.state IN ('eric_review', 'director_review', 'in_progress', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'eric_review' AND NEW.state IN ('inspection', 'director_review', 'audit', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'director_review' AND NEW.state IN ('done', 'in_progress', 'analysis', 'backlog', 'cancelled')) OR
            (OLD.state = 'done' AND NEW.state IN ('analysis', 'backlog')) OR
            (OLD.state = 'cancelled' AND NEW.state IN ('analysis', 'backlog'))"""

EXPECTED_ACTION_ROLES = {
    "audit_kick_back": ["audit"],
    "audit_sign_off": ["audit"],
    "cancel": ["director"],
    "defer": ["director"],
    "eric_reopen": ["eric"],
    "eric_sign_off": ["eric"],
    "inspector_kick_back": ["inspector"],
    "inspector_sign_off": ["inspector"],
    "mark_done": ["director"],
    "release_draft": ["director", "eric"],
    "request_commit_exempt": ["main", "app", "ops", "perf", "research"],
    "route": ["director"],
    "start_task": ["director", "main", "app", "ops", "perf", "research", "audit", "inspector"],
    "start_work": ["main", "app", "ops", "perf", "research"],
    "submit_to_audit": ["main", "app", "ops", "perf", "research"],
    "submit_to_inspection": ["main", "app", "ops", "perf", "research"],
}


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


def frontend_columns_literal() -> str:
    match = re.search(r"    const COLUMNS = \[\n(?P<body>.*?)\n    \];", frontend_script_core.SCRIPT_CORE, re.S)
    if not match:
        raise AssertionError("could not locate frontend COLUMNS literal")
    return match.group("body")


def compile_columns(stages: list[dict[str, object]]) -> str:
    return "\n".join(f"      {{ key: '{stage['name']}', label: '{stage['display_label']}' }}," for stage in stages)


def compile_state_rank(stages: list[dict[str, object]]) -> str:
    return "\n".join(f"        WHEN '{stage['name']}' THEN {stage['rank']}" for stage in stages)


def compile_transition_target_role(stages: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for stage in stages:
        owners = stage["owner_roles"]
        if stage["name"] == "in_progress":
            lines.append("        WHEN p_state = 'in_progress' THEN NULLIF(p_assignee, 'unassigned')")
        elif len(owners) == 1:
            lines.append(f"        WHEN p_state = '{stage['name']}' THEN '{owners[0]}'")
    return "\n".join(lines)


def compile_transition_whitelist(stages: list[dict[str, object]], transitions: list[dict[str, object]]) -> str:
    stage_rank = {str(stage["name"]): int(stage["rank"]) for stage in stages}
    stage_order = ["backlog", "draft", "analysis", "in_progress", "inspection", "audit", "eric_review", "director_review", "done", "cancelled"]
    transition_order = {
        "backlog": ["analysis", "in_progress", "cancelled"],
        "draft": ["analysis", "cancelled"],
        "analysis": ["in_progress", "backlog", "cancelled"],
        "in_progress": ["inspection", "audit", "analysis", "backlog", "cancelled"],
        "inspection": ["audit", "in_progress", "backlog", "cancelled"],
        "audit": ["eric_review", "director_review", "in_progress", "analysis", "backlog", "cancelled"],
        "eric_review": ["inspection", "director_review", "audit", "analysis", "backlog", "cancelled"],
        "director_review": ["done", "in_progress", "analysis", "backlog", "cancelled"],
        "done": ["analysis", "backlog"],
        "cancelled": ["analysis", "backlog"],
    }
    grouped: dict[str, set[str]] = defaultdict(set)
    for transition in transitions:
        grouped[str(transition["from_stage"])].add(str(transition["to_stage"]))
    assert set(grouped) == set(stage_order), grouped
    assert set(stage_order) == set(stage_rank), stage_rank
    lines = []
    for from_stage in stage_order:
        actual_to = grouped[from_stage]
        expected_to = transition_order[from_stage]
        assert actual_to == set(expected_to), (from_stage, actual_to, expected_to)
        quoted = ", ".join(f"'{to_stage}'" for to_stage in expected_to)
        suffix = " OR" if from_stage != stage_order[-1] else ""
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
        'allowed_roles', allowed_roles
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

            assert tuple(stage["name"] for stage in stages) == STATES
            assert compile_columns(stages) == frontend_columns_literal()
            assert compile_state_rank(stages) == EXPECTED_STATE_RANK_CASE
            assert compile_transition_target_role(stages) == EXPECTED_TRANSITION_TARGET_ROLE_CASE
            assert compile_transition_whitelist(stages, transitions) == EXPECTED_TRANSITION_WHITELIST
            assert compile_action_roles(transitions) == EXPECTED_ACTION_ROLES

            terminal_states = {stage["name"] for stage in stages if stage["is_terminal"]}
            assert terminal_states == {"done", "cancelled"}
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False, capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
