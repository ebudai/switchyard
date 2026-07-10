#!/usr/bin/env python3
"""Regression test: ticket-board PostgreSQL Model B function API grants."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"

PANE_ROLES = ["director", "eric", "ops", "app", "audit", "perf", "research", "main"]
IMPLEMENTERS = ["main", "app", "ops", "perf", "research"]
EXPECTED_ROLES = PANE_ROLES + ["ticket_board_service"]
ROLE_SQL_ARRAY = "ARRAY['director','eric','ops','app','audit','perf','research','main','ticket_board_service']"

FUNCTION_GRANTS = {
    "ticket_board.create_ticket(text,text)": {"director", "eric"},
    "ticket_board.file_bug(text,text,text)": set(IMPLEMENTERS),
    "ticket_board.route(text,text,text)": {"director"},
    "ticket_board.start_work(text)": set(IMPLEMENTERS),
    "ticket_board.submit_to_audit(text,text)": set(IMPLEMENTERS),
    "ticket_board.audit_sign_off(text)": {"audit"},
    "ticket_board.audit_kick_back(text,text)": {"audit"},
    "ticket_board.eric_sign_off(text)": {"eric"},
    "ticket_board.eric_reopen(text,text)": {"eric"},
    "ticket_board.mark_done(text,text)": {"director"},
    "ticket_board.defer(text)": {"director"},
    "ticket_board.cancel(text,text)": {"director"},
    "ticket_board.set_manually_controlled(text,boolean)": {"director"},
    "ticket_board.set_blockers(text,text[],text)": {"director"},
    "ticket_board.add_comment(text,text)": set(PANE_ROLES),
}


def run(args: list[str], *, capture: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
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


def conninfo(socket_dir: Path, port: int, dbname: str, user: str = "postgres") -> str:
    return f"host={socket_dir} port={port} dbname={dbname} user={user}"


def psql(conn: str, sql: str) -> str:
    return run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn], input_text=sql).stdout.strip()


def psql_error(conn: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        raise AssertionError(f"SQL unexpectedly succeeded for {conn}: {sql}")
    return proc.stderr + proc.stdout


def create_pane_roles(conn: str) -> None:
    role_sql = "\n".join(
        f"CREATE ROLE {role} LOGIN PASSWORD '{role}_preexisting_password';"
        for role in PANE_ROLES
    )
    psql(conn, role_sql)


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "comments": [],
        "created": "2026-07-10T00:00:00+00:00",
        "updated": "2026-07-10T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def insert_ticket(
    conn: str,
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
    audit_signoff: bool = False,
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
) -> None:
    psql(
        conn,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, audit_signoff,
    needs_eric_signoff, eric_signoff, commit_hash, commit_exempt,
    created_text, updated_text, source_json
) VALUES (
    {sql_string(ticket_id)}, {sql_string(title)}, '', {sql_string(state)}, {sql_string(assignee)},
    {sql_string(implementation)}, {str(audit_signoff).lower()}, {str(needs_eric_signoff).lower()},
    {str(eric_signoff).lower()}, {sql_string(commit_hash)}, {str(commit_exempt).lower()},
    '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
    '{ticket_source(ticket_id, title, state, assignee)}'::jsonb
);
""",
    )


def assert_permission_denied(conn: str, sql: str) -> None:
    error = psql_error(conn, sql)
    assert "permission denied" in error or "cannot call" in error, error


def assert_function_grants(conn: str) -> None:
    for signature, allowed_roles in FUNCTION_GRANTS.items():
        rows = json.loads(
            psql(
                conn,
                f"""
SELECT jsonb_object_agg(role_name, has_function_privilege(role_name, {sql_string(signature)}, 'EXECUTE'))::text
FROM unnest({ROLE_SQL_ARRAY}) AS role_name;
""",
            )
        )
        for role in EXPECTED_ROLES:
            assert rows[role] == (role in allowed_roles), (signature, role, rows)


def assert_direct_dml_denied(conn: str) -> None:
    for role in EXPECTED_ROLES:
        privileges = json.loads(
            psql(
                conn,
                f"""
SELECT jsonb_build_object(
    'insert', has_table_privilege({sql_string(role)}, 'ticket_board.tickets', 'INSERT'),
    'update', has_table_privilege({sql_string(role)}, 'ticket_board.tickets', 'UPDATE'),
    'delete', has_table_privilege({sql_string(role)}, 'ticket_board.tickets', 'DELETE'),
    'comment_insert', has_table_privilege({sql_string(role)}, 'ticket_board.ticket_comments', 'INSERT')
)::text;
""",
            )
        )
        assert privileges == {
            "insert": False,
            "update": False,
            "delete": False,
            "comment_insert": False,
        }, (role, privileges)

        assert_permission_denied(
            conn.replace("user=postgres", f"user={role}"),
            "UPDATE ticket_board.tickets SET title = title WHERE id = 'PGU-1';",
        )


def exercise_function_api(admin_conn: str, role_conn: dict[str, str]) -> None:
    created_by_director = psql(role_conn["director"], "SELECT ticket_board.create_ticket('Director create', 'Body');")
    created_by_eric = psql(role_conn["eric"], "SELECT ticket_board.create_ticket('Eric create', 'Body');")
    assert created_by_director.startswith("PGU-"), created_by_director
    assert created_by_eric.startswith("PGU-"), created_by_eric
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.create_ticket('Nope', 'Body');")

    insert_ticket(admin_conn, "PGU-100", title="Source")
    for role in IMPLEMENTERS:
        filed = psql(role_conn[role], f"SELECT ticket_board.file_bug('{role} bug', 'Body', 'PGU-100');")
        parent_id = psql(admin_conn, f"SELECT parent_id FROM ticket_board.tickets WHERE id = {sql_string(filed)};")
        assert parent_id == "PGU-100", (role, filed, parent_id)
    assert "source_ticket_id ticket not found" in psql_error(
        role_conn["ops"],
        "SELECT ticket_board.file_bug('Missing source', 'Body', 'PGU-99999');",
    )
    assert_permission_denied(role_conn["audit"], "SELECT ticket_board.file_bug('Nope', 'Body', 'PGU-100');")

    insert_ticket(admin_conn, "PGU-200", title="Route fixture", state="analysis")
    psql(role_conn["director"], "SELECT ticket_board.route('PGU-200', 'backlog', 'ops');")
    routed = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'assignee', assignee)::text
FROM ticket_board.tickets WHERE id = 'PGU-200';
""",
        )
    )
    assert routed == {"state": "backlog", "assignee": "ops"}, routed
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.route('PGU-200', 'analysis', 'ops');")

    for role in IMPLEMENTERS:
        ticket_id = f"PGU-{300 + IMPLEMENTERS.index(role)}"
        insert_ticket(
            admin_conn,
            ticket_id,
            title=f"{role} start",
            state="ready",
            assignee=role,
            implementation="Ready.",
        )
        psql(role_conn[role], f"SELECT ticket_board.start_work('{ticket_id}');")
        state = psql(admin_conn, f"SELECT state FROM ticket_board.tickets WHERE id = '{ticket_id}';")
        assert state == "in_progress", (role, ticket_id, state)
    assert_permission_denied(role_conn["audit"], "SELECT ticket_board.start_work('PGU-300');")

    for role in IMPLEMENTERS:
        ticket_id = f"PGU-{400 + IMPLEMENTERS.index(role)}"
        insert_ticket(
            admin_conn,
            ticket_id,
            title=f"{role} submit",
            state="in_progress",
            assignee=role,
            implementation="In progress.",
        )
        psql(role_conn[role], f"SELECT ticket_board.submit_to_audit('{ticket_id}', 'abcdef{IMPLEMENTERS.index(role)}');")
        submitted = json.loads(
            psql(
                admin_conn,
                f"""
SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets WHERE id = '{ticket_id}';
""",
            )
        )
        assert submitted == {"state": "audit", "commit_hash": f"abcdef{IMPLEMENTERS.index(role)}"}, submitted
    assert_permission_denied(role_conn["director"], "SELECT ticket_board.submit_to_audit('PGU-400', 'abc1234');")

    insert_ticket(admin_conn, "PGU-500", title="Audit signoff", state="audit", assignee="audit", implementation="Done.")
    psql(role_conn["audit"], "SELECT ticket_board.audit_sign_off('PGU-500');")
    audit_signed = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff)::text
FROM ticket_board.tickets WHERE id = 'PGU-500';
""",
        )
    )
    assert audit_signed == {"state": "director_review", "assignee": "director", "audit_signoff": True}, audit_signed
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.audit_sign_off('PGU-500');")

    insert_ticket(admin_conn, "PGU-501", title="Audit kickback", state="audit", assignee="audit", implementation="Done.")
    psql(role_conn["audit"], "SELECT ticket_board.audit_kick_back('PGU-501', 'Needs changes.');")
    kickback = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-501';
""",
        )
    )
    assert kickback == {"state": "analysis", "assignee": "unassigned", "comment": "Needs changes."}, kickback
    assert_permission_denied(role_conn["director"], "SELECT ticket_board.audit_kick_back('PGU-501', 'Nope');")

    insert_ticket(
        admin_conn,
        "PGU-600",
        title="Eric signoff",
        state="eric_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
        needs_eric_signoff=True,
    )
    psql(role_conn["eric"], "SELECT ticket_board.eric_sign_off('PGU-600');")
    eric_signed = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'eric_signoff', eric_signoff)::text
FROM ticket_board.tickets WHERE id = 'PGU-600';
""",
        )
    )
    assert eric_signed == {"state": "director_review", "assignee": "director", "eric_signoff": True}, eric_signed
    assert_permission_denied(role_conn["audit"], "SELECT ticket_board.eric_sign_off('PGU-600');")

    insert_ticket(
        admin_conn,
        "PGU-601",
        title="Eric reopen",
        state="eric_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
        needs_eric_signoff=True,
    )
    psql(role_conn["eric"], "SELECT ticket_board.eric_reopen('PGU-601', 'Needs another pass.');")
    eric_reopened = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-601';
""",
        )
    )
    assert eric_reopened == {"state": "analysis", "comment": "Needs another pass."}, eric_reopened
    assert_permission_denied(role_conn["audit"], "SELECT ticket_board.eric_reopen('PGU-601', 'Nope');")

    insert_ticket(
        admin_conn,
        "PGU-700",
        title="Done",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
    )
    psql(role_conn["director"], "SELECT ticket_board.mark_done('PGU-700', '123abcd');")
    done = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets WHERE id = 'PGU-700';
""",
        )
    )
    assert done == {"state": "done", "commit_hash": "123abcd"}, done
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.mark_done('PGU-700', '123abcd');")

    insert_ticket(admin_conn, "PGU-701", title="Defer", state="analysis")
    psql(role_conn["director"], "SELECT ticket_board.defer('PGU-701');")
    assert psql(admin_conn, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-701';") == "backlog"
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.defer('PGU-701');")

    insert_ticket(admin_conn, "PGU-702", title="Cancel", state="analysis")
    psql(role_conn["director"], "SELECT ticket_board.cancel('PGU-702', 'Cancelled by director.');")
    cancelled = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-702';
""",
        )
    )
    assert cancelled == {"state": "cancelled", "comment": "Cancelled by director."}, cancelled
    assert_permission_denied(role_conn["audit"], "SELECT ticket_board.cancel('PGU-702', 'Nope');")

    insert_ticket(admin_conn, "PGU-703", title="Manual", state="analysis")
    psql(role_conn["director"], "SELECT ticket_board.set_manually_controlled('PGU-703', true);")
    assert psql(admin_conn, "SELECT manually_controlled FROM ticket_board.tickets WHERE id = 'PGU-703';") == "t"
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.set_manually_controlled('PGU-703', false);")

    insert_ticket(admin_conn, "PGU-704", title="Blockers", state="analysis")
    psql(role_conn["director"], "SELECT ticket_board.set_blockers('PGU-704', ARRAY['PGU-100'], 'Waiting on source.');")
    blockers = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'blocked_reason', t.blocked_reason,
    'blocked_by', (SELECT jsonb_agg(blocker_ticket_id ORDER BY position) FROM ticket_board.ticket_blockers WHERE ticket_id = t.id)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-704';
""",
        )
    )
    assert blockers == {"blocked_reason": "Waiting on source.", "blocked_by": ["PGU-100"]}, blockers
    assert_permission_denied(role_conn["ops"], "SELECT ticket_board.set_blockers('PGU-704', ARRAY['PGU-100'], 'Nope');")

    insert_ticket(admin_conn, "PGU-800", title="Comments", state="analysis")
    for role in PANE_ROLES:
        psql(role_conn[role], f"SELECT ticket_board.add_comment('PGU-800', 'comment from {role}');")
    comment_count = psql(admin_conn, "SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-800';")
    assert comment_count == str(len(PANE_ROLES)), comment_count
    assert_permission_denied(role_conn["ticket_board_service"], "SELECT ticket_board.add_comment('PGU-800', 'Nope');")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-rbac.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_rbac_test"
        admin_conn = conninfo(socket_dir, port, dbname)

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))

            missing_role_error = psql_error(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            assert 'role "director" does not exist' in missing_role_error, missing_role_error
            service_after_failed_rbac = psql(admin_conn, "SELECT to_regrole('ticket_board_service') IS NULL;")
            assert service_after_failed_rbac == "t", service_after_failed_rbac

            create_pane_roles(admin_conn)
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))

            role_rows = json.loads(
                psql(
                    admin_conn,
                    f"""
SELECT jsonb_object_agg(rolname, jsonb_build_object('can_login', rolcanlogin, 'password_is_null', rolpassword IS NULL))::text
FROM pg_authid
WHERE rolname = ANY({ROLE_SQL_ARRAY});
""",
                )
            )
            assert sorted(role_rows) == sorted(EXPECTED_ROLES), role_rows
            for role in PANE_ROLES:
                assert role_rows[role] == {"can_login": True, "password_is_null": False}, (role, role_rows[role])
            assert role_rows["ticket_board_service"] == {"can_login": True, "password_is_null": True}, role_rows

            schema_usage = json.loads(
                psql(
                    admin_conn,
                    f"""
SELECT jsonb_object_agg(role_name, has_schema_privilege(role_name, 'ticket_board', 'USAGE'))::text
FROM unnest({ROLE_SQL_ARRAY}) AS role_name;
""",
                )
            )
            assert all(schema_usage.values()), schema_usage

            select_privileges = json.loads(
                psql(
                    admin_conn,
                    f"""
SELECT jsonb_object_agg(role_name, has_table_privilege(role_name, 'ticket_board.tickets', 'SELECT'))::text
FROM unnest({ROLE_SQL_ARRAY}) AS role_name;
""",
                )
            )
            assert all(select_privileges.values()), select_privileges

            assert_function_grants(admin_conn)

            role_conn = {role: conninfo(socket_dir, port, dbname, role) for role in EXPECTED_ROLES}
            insert_ticket(admin_conn, "PGU-1", title="Direct DML fixture")
            assert_direct_dml_denied(admin_conn)
            exercise_function_api(admin_conn, role_conn)
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_rbac_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
