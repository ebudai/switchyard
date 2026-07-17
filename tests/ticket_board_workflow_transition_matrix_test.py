#!/usr/bin/env python3
"""Phase-3 guard: workflow transition config matches the hardcoded engine."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import CALLER_ROLES  # noqa: E402
from tests.ticket_board_workflow_config_equivalence_test import (  # noqa: E402
    SCHEMA_PATH,
    ActionOwnership,
    action_ownership_from_db,
    action_roles_from_db,
    conninfo,
    function_def,
    free_port,
    psql,
    run,
)


def load_stages(conn: str) -> list[str]:
    return json.loads(
        psql(
            conn,
            """
SELECT jsonb_agg(name ORDER BY rank)::text
FROM ticket_board.workflow_stages;
""",
        )
    )


def load_transitions(conn: str) -> list[dict[str, object]]:
    return json.loads(
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


def transition_verdict(conn: str, function_name: str, from_stage: str, to_stage: str) -> bool:
    result = psql(
        conn,
        f"""
SELECT ticket_board.{function_name}('{from_stage}', '{to_stage}');
""",
    )
    return result == "t"


def build_transition_indexes(
    transitions: list[dict[str, object]],
) -> tuple[dict[tuple[str, str], list[dict[str, object]]], dict[str, list[str]], dict[str, bool]]:
    by_pair: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    config_action_roles: dict[str, list[str]] = {}
    config_action_owner_scopes: dict[str, bool] = {}
    for transition in transitions:
        key = (str(transition["from_stage"]), str(transition["to_stage"]))
        by_pair[key].append(transition)
        action = str(transition["action_name"])
        roles = [str(role) for role in transition["allowed_roles"]]
        existing = config_action_roles.setdefault(action, roles)
        if existing != roles:
            raise AssertionError(f"conflicting config roles for {action}: {existing} != {roles}")
        scoped = bool(transition["owner_scoped"])
        existing_scope = config_action_owner_scopes.setdefault(action, scoped)
        if existing_scope != scoped:
            raise AssertionError(f"conflicting owner_scoped values for {action}: {existing_scope} != {scoped}")
    return by_pair, config_action_roles, config_action_owner_scopes


def assert_matrix_matches(conn: str) -> int:
    stages = load_stages(conn)
    transitions = load_transitions(conn)
    transitions_by_pair, config_action_roles, config_action_owner_scopes = build_transition_indexes(transitions)
    db_action_roles = action_roles_from_db(conn)
    db_action_ownership = action_ownership_from_db(conn)
    roles = list(CALLER_ROLES)
    checked = 0

    for action, config_roles in sorted(config_action_roles.items()):
        db_roles = db_action_roles.get(action)
        if db_roles != config_roles:
            raise AssertionError(f"role drift for action {action}: config={config_roles} hardcoded={db_roles}")
        expected_ownership = db_action_ownership.get(action, ActionOwnership(False, False))
        expected_owner_scoped = expected_ownership.owner_scoped
        config_owner_scoped = config_action_owner_scopes[action]
        if config_owner_scoped != expected_owner_scoped:
            raise AssertionError(
                f"owner-scope drift for action {action}: config={config_owner_scoped} "
                f"hardcoded={expected_owner_scoped}"
            )
        for role in roles:
            for actor_is_assignee in (False, True):
                config_action_allowed = role in config_roles and (
                    not config_owner_scoped
                    or actor_is_assignee
                    or role == "director"
                )
                hardcoded_action_allowed = role in db_roles and (
                    not expected_ownership.owner_scoped
                    or actor_is_assignee
                    or (expected_ownership.director_override and role == "director")
                )
                if config_action_allowed != hardcoded_action_allowed:
                    raise AssertionError(
                        f"action RBAC drift for {action} as {role} "
                        f"(actor_is_assignee={actor_is_assignee}): "
                        f"config={config_action_allowed} hardcoded={hardcoded_action_allowed}"
                    )

    for from_stage in stages:
        for to_stage in stages:
            hardcoded_pair_allowed = transition_verdict(
                conn,
                "workflow_transition_allowed_hardcoded",
                from_stage,
                to_stage,
            )
            config_pair_allowed = transition_verdict(
                conn,
                "workflow_transition_allowed_config",
                from_stage,
                to_stage,
            )
            if hardcoded_pair_allowed != config_pair_allowed:
                raise AssertionError(
                    f"transition pair drift for {from_stage}->{to_stage}: "
                    f"config={config_pair_allowed} hardcoded={hardcoded_pair_allowed}"
                )

            pair_transitions = transitions_by_pair.get((from_stage, to_stage), [])
            for role in roles:
                for actor_is_assignee in (False, True):
                    config_role_allowed = any(
                        role in transition["allowed_roles"]
                        and (
                            not transition["owner_scoped"]
                            or actor_is_assignee
                            or role == "director"
                        )
                        for transition in pair_transitions
                    )
                    hardcoded_role_allowed = any(
                        role in db_action_roles.get(str(transition["action_name"]), [])
                        and (
                            not db_action_ownership.get(
                                str(transition["action_name"]),
                                ActionOwnership(False, False),
                            ).owner_scoped
                            or actor_is_assignee
                            or (
                                db_action_ownership.get(
                                    str(transition["action_name"]),
                                    ActionOwnership(False, False),
                                ).director_override
                                and role == "director"
                            )
                        )
                        for transition in pair_transitions
                    )
                    if hardcoded_pair_allowed:
                        if config_role_allowed != hardcoded_role_allowed:
                            raise AssertionError(
                                f"role verdict drift for {from_stage}->{to_stage} as {role} "
                                f"(actor_is_assignee={actor_is_assignee}): "
                                f"config={config_role_allowed} hardcoded={hardcoded_role_allowed}"
                            )
                    elif config_role_allowed or hardcoded_role_allowed:
                        raise AssertionError(
                            f"disallowed transition has role allowance for {from_stage}->{to_stage} as {role} "
                            f"(actor_is_assignee={actor_is_assignee}): "
                            f"config={config_role_allowed} hardcoded={hardcoded_role_allowed}"
                        )
                    checked += 1

    return checked


def assert_shadow_logs_drift(conn: str) -> None:
    psql(
        conn,
        """
SELECT ticket_board.log_workflow_transition_shadow_mismatch(
    'PGU-MATRIX',
    'backlog',
    'analysis',
    'director',
    true,
    false
);
""",
    )
    row = json.loads(
        psql(
            conn,
            """
SELECT jsonb_build_object(
    'ticket_id', ticket_id,
    'from_state', from_state,
    'to_state', to_state,
    'actor', actor,
    'hardcoded_allowed', hardcoded_allowed,
    'config_allowed', config_allowed
)::text
FROM ticket_board.workflow_transition_shadow_log
WHERE ticket_id = 'PGU-MATRIX';
""",
        )
    )
    assert row == {
        "ticket_id": "PGU-MATRIX",
        "from_state": "backlog",
        "to_state": "analysis",
        "actor": "director",
        "hardcoded_allowed": True,
        "config_allowed": False,
    }


def assert_rbac_shadow_logs_drift(conn: str) -> None:
    psql(
        conn,
        """
SELECT ticket_board.log_workflow_transition_rbac_shadow_mismatch(
    'PGU-MATRIX-RBAC',
    'submit_to_inspection',
    'app',
    'ops',
    true
);
""",
    )
    row = json.loads(
        psql(
            conn,
            """
SELECT jsonb_build_object(
    'ticket_id', ticket_id,
    'action_name', action_name,
    'actor', actor,
    'ticket_assignee', ticket_assignee,
    'hardcoded_allowed', hardcoded_allowed,
    'config_allowed', config_allowed,
    'owner_scoped', owner_scoped
)::text
FROM ticket_board.workflow_transition_rbac_shadow_log
WHERE ticket_id = 'PGU-MATRIX-RBAC';
""",
        )
    )
    assert row == {
        "ticket_id": "PGU-MATRIX-RBAC",
        "action_name": "submit_to_inspection",
        "actor": "app",
        "ticket_assignee": "ops",
        "hardcoded_allowed": True,
        "config_allowed": False,
        "owner_scoped": True,
    }


def assert_config_authoritative_blocks_runtime_transition(conn: str) -> None:
    psql(
        conn,
        """
INSERT INTO ticket_board.tickets (
    id,
    title,
    body,
    state,
    assignee,
    created_text,
    updated_text,
    created_at,
    updated_at,
    source_json
) VALUES (
    'PGU-52199',
    'Config authoritative mutation proof',
    '',
    'backlog',
    'unassigned',
    '2026-07-17T00:00:00+00:00',
    '2026-07-17T00:00:00+00:00',
    clock_timestamp(),
    clock_timestamp(),
    '{"id":"PGU-52199","title":"Config authoritative mutation proof","body":"","state":"backlog","assignee":"unassigned","comments":[],"created":"2026-07-17T00:00:00+00:00","updated":"2026-07-17T00:00:00+00:00"}'::jsonb
);

DELETE FROM ticket_board.workflow_transitions
WHERE from_stage = 'backlog'
  AND to_stage = 'analysis';
""",
    )
    failed = False
    try:
        psql(
            conn,
            """
UPDATE ticket_board.tickets
SET state = 'analysis'
WHERE id = 'PGU-52199';
""",
        )
    except subprocess.CalledProcessError as exc:
        failed = "illegal state transition: backlog -> analysis" in exc.stderr
    assert failed, "removing a config transition must make that runtime transition illegal"


def assert_owner_scope_matrix_catches_drift(conn: str) -> None:
    psql(
        conn,
        """
UPDATE ticket_board.workflow_transitions
SET owner_scoped = false
WHERE action_name = 'submit_to_inspection';
""",
    )
    failed = False
    try:
        assert_matrix_matches(conn)
    except AssertionError as exc:
        failed = "owner-scope drift for action submit_to_inspection" in str(exc)
    assert failed, "removing owner scoping from submit_to_inspection must fail the matrix"


def assert_owner_scope_extraction_catches_function_body_drift(conn: str) -> None:
    original = function_def(conn, "ticket_board.submit_to_audit(text,text)")
    mutated = original.replace(
        "ticket_commit_exempt boolean;\n    ticket_last_rejected_commit text;",
        "ticket_commit_exempt boolean;\n    ticket_last_rejected_commit text;\n    ticket_assignee text;",
    ).replace(
        "SELECT tickets.commit_exempt, tickets.last_rejected_commit\n"
        "    INTO ticket_commit_exempt, ticket_last_rejected_commit",
        "SELECT tickets.commit_exempt, tickets.last_rejected_commit, tickets.assignee\n"
        "    INTO ticket_commit_exempt, ticket_last_rejected_commit, ticket_assignee",
    ).replace(
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'ticket not found: %', id;\n"
        "    END IF;",
        "    IF NOT FOUND THEN\n"
        "        RAISE EXCEPTION 'ticket not found: %', id;\n"
        "    END IF;\n"
        "    IF ticket_assignee <> actor THEN\n"
        "        RAISE EXCEPTION '% cannot call submit_to_audit for ticket assigned to %', actor, ticket_assignee\n"
        "            USING ERRCODE = '42501';\n"
        "    END IF;",
        1,
    )
    if mutated == original or "ticket_assignee <> actor" not in mutated:
        raise AssertionError("submit_to_audit ownership mutation did not apply")

    psql(conn, mutated)
    failed = False
    try:
        assert_matrix_matches(conn)
    except AssertionError as exc:
        failed = "owner-scope drift for action submit_to_audit" in str(exc)
    finally:
        psql(conn, original)
    assert failed, "adding an ownership check to submit_to_audit must fail the matrix"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-workflow-matrix.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_workflow_matrix_test"
        admin_conn = conninfo(socket_dir, port, dbname)

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            checked = assert_matrix_matches(admin_conn)
            assert checked == len(load_stages(admin_conn)) * len(load_stages(admin_conn)) * len(CALLER_ROLES) * 2
            assert_shadow_logs_drift(admin_conn)
            assert_rbac_shadow_logs_drift(admin_conn)
            assert_owner_scope_matrix_catches_drift(admin_conn)
            psql(
                admin_conn,
                """
UPDATE ticket_board.workflow_transitions
SET owner_scoped = true
WHERE action_name = 'submit_to_inspection';
""",
            )
            assert_owner_scope_extraction_catches_function_body_drift(admin_conn)
            assert_config_authoritative_blocks_runtime_transition(admin_conn)
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False, capture_output=True)

    print(f"ticket_board_workflow_transition_matrix_test: ok ({checked} triples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
