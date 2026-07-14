#!/usr/bin/env python3
"""Regression test: ticket-board PostgreSQL single-writer function API grants."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"

PANE_ROLES = ["director", "eric", "ops", "app", "audit", "inspector", "perf", "research", "main"]
EXPECTED_ROLES = PANE_ROLES + ["ticket_board_service", "ticket_board_listener"]
ROLE_SQL_ARRAY = "ARRAY['director','eric','ops','app','audit','inspector','perf','research','main','ticket_board_service','ticket_board_listener']"

WRITE_FUNCTIONS = [
    "ticket_board.create_ticket(text,text)",
    "ticket_board.create_ticket(text,text,text)",
    "ticket_board.create_ticket(text,text,text,text[],text)",
    "ticket_board.file_bug(text,text,text)",
    "ticket_board.route(text,text,text)",
    "ticket_board.start_work(text)",
    "ticket_board.submit_to_audit(text,text)",
    "ticket_board.request_commit_exempt(text,text)",
    "ticket_board.audit_sign_off(text,text)",
    "ticket_board.audit_kick_back(text,text)",
    "ticket_board.inspector_sign_off(text)",
    "ticket_board.inspector_kick_back(text,text)",
    "ticket_board.eric_sign_off(text,text)",
    "ticket_board.eric_reopen(text,text)",
    "ticket_board.mark_done(text,text)",
    "ticket_board.defer(text)",
    "ticket_board.cancel(text,text)",
    "ticket_board.set_manually_controlled(text,boolean)",
    "ticket_board.set_blockers(text,text[],text)",
    "ticket_board.add_comment(text,text)",
    "ticket_board.edit_fields(text,jsonb)",
    "ticket_board.merge(text,text)",
]
LISTENER_FUNCTIONS = [
    "ticket_board.claim_notification(timestamp with time zone,interval)",
    "ticket_board.next_notification_attempt(timestamp with time zone,interval)",
    "ticket_board.finish_current_blocker(text,text,timestamp with time zone,interval)",
    "ticket_board.ack_notification(bigint)",
    "ticket_board.requeue_notification(bigint,interval,text)",
    "ticket_board.record_notification_trace(text,bigint,text,text,text,text,text,text,jsonb)",
    "ticket_board.notify_idle_stall_nudges(jsonb,timestamp with time zone,interval,interval,integer)",
    "ticket_board.notify_idle_turn_end_nudges(jsonb,timestamp with time zone)",
]
PRIVATE_LISTENER_FUNCTIONS = [
    "ticket_board.require_ticket_board_listener(text)",
    "ticket_board.transition_notification_payload(text,text,text,text)",
    "ticket_board.pending_transition_notifications()",
    "ticket_board.mark_transition_notified(text)",
    "ticket_board.enqueue_notification(text,text,text,text,jsonb,text,timestamp with time zone)",
]


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


def create_pane_roles(conn: str, *, exclude: set[str] | None = None) -> None:
    exclude = exclude or set()
    role_sql = "\n".join(
        f"CREATE ROLE {role} LOGIN PASSWORD '{role}_preexisting_password';"
        for role in PANE_ROLES
        if role not in exclude
    )
    if role_sql:
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
    manually_controlled: bool = False,
    parked: bool = False,
) -> None:
    psql(
        conn,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, audit_signoff,
    needs_eric_signoff, eric_signoff, commit_hash, commit_exempt,
    manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    {sql_string(ticket_id)}, {sql_string(title)}, '', {sql_string(state)}, {sql_string(assignee)},
    {sql_string(implementation)}, {str(audit_signoff).lower()}, {str(needs_eric_signoff).lower()},
    {str(eric_signoff).lower()}, {sql_string(commit_hash)}, {str(commit_exempt).lower()},
    {str(manually_controlled).lower()}, {str(parked).lower()}, '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00', '{ticket_source(ticket_id, title, state, assignee)}'::jsonb
);
""",
    )


def assert_permission_denied(conn: str, sql: str) -> None:
    error = psql_error(conn, sql)
    assert "permission denied" in error or "cannot call" in error, error


def assert_function_grants(conn: str) -> None:
    for signature in WRITE_FUNCTIONS:
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
            expected = role == "ticket_board_service"
            if signature == "ticket_board.file_bug(text,text,text)" and role == "audit":
                expected = True
            assert rows[role] == expected, (signature, role, rows)
    for signature in LISTENER_FUNCTIONS:
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
            assert rows[role] == (role == "ticket_board_listener"), (signature, role, rows)
    for signature in PRIVATE_LISTENER_FUNCTIONS:
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
            assert rows[role] is False, (signature, role, rows)


def assert_direct_dml_denied(conn: str, role_conn: dict[str, str]) -> None:
    for role in EXPECTED_ROLES:
        privileges = json.loads(
            psql(
                conn,
                f"""
SELECT jsonb_build_object(
    'tickets_insert', has_table_privilege({sql_string(role)}, 'ticket_board.tickets', 'INSERT'),
    'tickets_update', has_table_privilege({sql_string(role)}, 'ticket_board.tickets', 'UPDATE'),
    'tickets_delete', has_table_privilege({sql_string(role)}, 'ticket_board.tickets', 'DELETE'),
    'comments_insert', has_table_privilege({sql_string(role)}, 'ticket_board.ticket_comments', 'INSERT'),
    'blockers_insert', has_table_privilege({sql_string(role)}, 'ticket_board.ticket_blockers', 'INSERT'),
    'queue_insert', has_table_privilege({sql_string(role)}, 'ticket_board.ticket_notification_queue', 'INSERT'),
    'queue_update', has_table_privilege({sql_string(role)}, 'ticket_board.ticket_notification_queue', 'UPDATE'),
    'queue_delete', has_table_privilege({sql_string(role)}, 'ticket_board.ticket_notification_queue', 'DELETE'),
    'trace_insert', has_table_privilege({sql_string(role)}, 'ticket_board.notification_trace', 'INSERT'),
    'trace_update', has_table_privilege({sql_string(role)}, 'ticket_board.notification_trace', 'UPDATE'),
    'trace_delete', has_table_privilege({sql_string(role)}, 'ticket_board.notification_trace', 'DELETE'),
    'comments_sequence', has_sequence_privilege({sql_string(role)}, 'ticket_board.ticket_comments_id_seq', 'USAGE'),
    'queue_sequence', has_sequence_privilege({sql_string(role)}, 'ticket_board.ticket_notification_queue_id_seq', 'USAGE'),
    'trace_sequence', has_sequence_privilege({sql_string(role)}, 'ticket_board.notification_trace_id_seq', 'USAGE')
)::text;
""",
            )
        )
        assert privileges == {
            "tickets_insert": False,
            "tickets_update": False,
            "tickets_delete": False,
            "comments_insert": False,
            "blockers_insert": False,
            "queue_insert": False,
            "queue_update": False,
            "queue_delete": False,
            "trace_insert": False,
            "trace_update": False,
            "trace_delete": False,
            "comments_sequence": False,
            "queue_sequence": False,
            "trace_sequence": False,
        }, (role, privileges)

        assert_permission_denied(
            role_conn[role],
            """
INSERT INTO ticket_board.tickets (id, title, body, state, assignee, created_text, updated_text, source_json)
VALUES (
    'PGU-999999', 'direct insert', '', 'analysis', 'unassigned',
    '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00',
    '{"id":"PGU-999999","title":"direct insert","body":"","state":"analysis","assignee":"unassigned","comments":[],"created":"2026-07-10T00:00:00+00:00","updated":"2026-07-10T00:00:00+00:00"}'
);
""",
        )
        assert_permission_denied(
            role_conn[role],
            "UPDATE ticket_board.tickets SET title = title WHERE id = 'PGU-1';",
        )
        assert_permission_denied(
            role_conn[role],
            "DELETE FROM ticket_board.tickets WHERE id = 'PGU-1';",
        )
        assert_permission_denied(
            role_conn[role],
            """
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES ('PGU-1', 0, 'x', '2026-07-10T00:00:00+00:00', 'x', '{"who":"x","ts":"2026-07-10T00:00:00+00:00","text":"x"}');
""",
        )
        assert_permission_denied(
            role_conn[role],
            """
INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, payload, dedupe_key)
VALUES ('PGU-1', 'transition', 'ops', 'x', '{"kind":"transition"}', 'direct-dml');
""",
        )


def assert_service_can_execute_every_write_function(admin_conn: str, service_conn: str) -> None:
    created = psql(service_conn, "SELECT ticket_board.create_ticket('Service create', 'Body');")
    assert created.startswith("PGU-"), created
    backlog_created = psql(service_conn, "SELECT ticket_board.create_ticket('Service backlog create', 'Body', 'backlog');")
    backlog_row = json.loads(
        psql(
            admin_conn,
            f"""
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked)::text
FROM ticket_board.tickets
WHERE id = {sql_string(backlog_created)};
""",
        )
    )
    assert backlog_row == {"state": "backlog", "assignee": "unassigned", "parked": True}, backlog_row
    blocked_created = psql(
        service_conn,
        f"SELECT ticket_board.create_ticket('Service blocked create', 'Body', 'analysis', ARRAY[{sql_string(created)}], 'Waiting on source.');",
    )
    blocked_created_row = json.loads(
        psql(
            admin_conn,
            f"""
SELECT jsonb_build_object(
    'blocked_reason', t.blocked_reason,
    'blocked_by', (SELECT jsonb_agg(blocker_ticket_id ORDER BY position) FROM ticket_board.ticket_blockers WHERE ticket_id = t.id)
)::text
FROM ticket_board.tickets t
WHERE id = {sql_string(blocked_created)};
""",
        )
    )
    assert blocked_created_row == {"blocked_reason": "Waiting on source.", "blocked_by": [created]}, blocked_created_row
    assert "invalid create state: in_progress; allowed: analysis, backlog" in psql_error(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.create_ticket('Bad direct create', 'Body', 'in_progress');
""",
    )

    insert_ticket(admin_conn, "PGU-100", title="Source")
    filed = psql(service_conn, "SELECT ticket_board.file_bug('Service bug', 'Body', 'PGU-100');")
    assert psql(admin_conn, f"SELECT parent_id FROM ticket_board.tickets WHERE id = {sql_string(filed)};") == "PGU-100"

    insert_ticket(admin_conn, "PGU-200", title="Route fixture", state="analysis")
    psql(service_conn, "SELECT ticket_board.route('PGU-200', 'backlog', 'ops');")
    assert psql(admin_conn, "SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = 'PGU-200';") == "backlog:ops"

    insert_ticket(admin_conn, "PGU-300", title="Start fixture", state="analysis", assignee="ops", implementation="Ready.")
    psql(service_conn, "SELECT ticket_board.start_work('PGU-300');")
    assert psql(admin_conn, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-300';") == "in_progress"

    insert_ticket(admin_conn, "PGU-400", title="Submit fixture", state="in_progress", assignee="app", implementation="Done.")
    psql(service_conn, "SELECT ticket_board.submit_to_audit('PGU-400', 'abcdef0');")
    submitted = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash, 'last_rejected_commit', last_rejected_commit)::text
FROM ticket_board.tickets WHERE id = 'PGU-400';
""",
        )
    )
    assert submitted == {"state": "audit", "commit_hash": "abcdef0", "last_rejected_commit": None}, submitted

    insert_ticket(
        admin_conn,
        "PGU-401",
        title="Submit exempt fixture",
        state="in_progress",
        assignee="ops",
        implementation="No repo changes.",
        commit_exempt=True,
    )
    psql(service_conn, "SELECT ticket_board.submit_to_audit('PGU-401', '');")
    exempt_submitted = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash, 'commit_exempt', commit_exempt)::text
FROM ticket_board.tickets WHERE id = 'PGU-401';
""",
        )
    )
    assert exempt_submitted == {"state": "audit", "commit_hash": "", "commit_exempt": True}, exempt_submitted

    insert_ticket(admin_conn, "PGU-500", title="Audit signoff", state="audit", assignee="audit", implementation="Done.")
    psql(service_conn, "SELECT ticket_board.audit_sign_off('PGU-500', 'Audit verified.');")
    audit_signed = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'audit_signoff', t.audit_signoff,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-500';
""",
        )
    )
    assert audit_signed == {
        "state": "director_review",
        "assignee": "director",
        "audit_signoff": True,
        "comment": "Audit verified.",
    }, audit_signed
    insert_ticket(admin_conn, "PGU-505", title="Require actor backstop", state="audit", assignee="audit", implementation="Done.")
    assert "role app cannot call audit_sign_off" in psql_error(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'app', false);
SELECT ticket_board.audit_sign_off('PGU-505', 'Nope.');
""",
    )
    insert_ticket(admin_conn, "PGU-506", title="Require actor empty caller role", state="audit", assignee="audit", implementation="Done.")
    psql(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', '', false);
SELECT ticket_board.audit_sign_off('PGU-506', 'Allowed with empty caller role.');
""",
    )
    assert psql(admin_conn, "SELECT state || ':' || assignee || ':' || audit_signoff::text FROM ticket_board.tickets WHERE id = 'PGU-506';") == "director_review:director:true"

    insert_ticket(admin_conn, "PGU-502", title="Audit signoff no comment", state="audit", assignee="audit", implementation="Done.")
    assert "comment text must be non-empty" in psql_error(
        service_conn,
        "SELECT ticket_board.audit_sign_off('PGU-502', '');",
    )

    insert_ticket(
        admin_conn,
        "PGU-501",
        title="Audit kickback",
        state="audit",
        assignee="audit",
        implementation="Done.",
        commit_hash="abcdef1",
    )
    psql(service_conn, "SELECT ticket_board.audit_kick_back('PGU-501', 'Needs changes.');")
    kickback = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'commit_hash', t.commit_hash,
    'last_rejected_commit', t.last_rejected_commit,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-501';
""",
        )
    )
    assert kickback == {
        "state": "analysis",
        "assignee": "unassigned",
        "commit_hash": "",
        "last_rejected_commit": "abcdef1",
        "comment": "Needs changes.",
    }, kickback
    assert "cannot submit: commit abcdef1 was just kicked back with no change -- do the fix and submit a NEW commit." in psql_error(
        service_conn,
        "SELECT ticket_board.submit_to_audit('PGU-501', 'abcdef1');",
    )
    psql(
        admin_conn,
        """
UPDATE ticket_board.tickets
SET state = 'in_progress',
    assignee = 'ops'
WHERE id = 'PGU-501';
""",
    )
    psql(service_conn, "SELECT ticket_board.submit_to_audit('PGU-501', 'abcdef2');")
    resubmitted = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash, 'last_rejected_commit', last_rejected_commit)::text
FROM ticket_board.tickets WHERE id = 'PGU-501';
""",
        )
    )
    assert resubmitted == {"state": "audit", "commit_hash": "abcdef2", "last_rejected_commit": None}, resubmitted

    insert_ticket(
        admin_conn,
        "PGU-504",
        title="Commit exempt resubmit after kickback",
        state="in_progress",
        assignee="ops",
        implementation="No repo changes.",
        commit_exempt=True,
    )
    psql(service_conn, "SELECT ticket_board.submit_to_audit('PGU-504', '');")
    psql(service_conn, "SELECT ticket_board.audit_kick_back('PGU-504', 'Document the change.');")
    psql(
        admin_conn,
        """
UPDATE ticket_board.tickets
SET state = 'in_progress',
    assignee = 'ops'
WHERE id = 'PGU-504';
""",
    )
    psql(service_conn, "SELECT ticket_board.submit_to_audit('PGU-504', '');")
    exempt_resubmitted = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', state,
    'commit_hash', commit_hash,
    'commit_exempt', commit_exempt,
    'last_rejected_commit', last_rejected_commit
)::text
FROM ticket_board.tickets WHERE id = 'PGU-504';
""",
        )
    )
    assert exempt_resubmitted == {
        "state": "audit",
        "commit_hash": "",
        "commit_exempt": True,
        "last_rejected_commit": None,
    }, exempt_resubmitted

    insert_ticket(
        admin_conn,
        "PGU-503",
        title="Inspector kickback",
        state="inspection",
        assignee="inspector",
        implementation="Needs changes.",
        commit_hash="fedcba9",
    )
    psql(service_conn, "SELECT ticket_board.inspector_kick_back('PGU-503', 'Please revise.');")
    inspector_kickback = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'commit_hash', t.commit_hash,
    'last_rejected_commit', t.last_rejected_commit,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-503';
""",
        )
    )
    assert inspector_kickback == {
        "state": "in_progress",
        "commit_hash": "",
        "last_rejected_commit": "fedcba9",
        "comment": "Please revise.",
    }, inspector_kickback

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
    psql(service_conn, "SELECT ticket_board.eric_sign_off('PGU-600', 'Eric approves.');")
    eric_signed = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'eric_signoff', t.eric_signoff,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-600';
""",
        )
    )
    assert eric_signed == {
        "state": "director_review",
        "assignee": "director",
        "eric_signoff": True,
        "comment": "Eric approves.",
    }, eric_signed

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
    psql(service_conn, "SELECT ticket_board.eric_reopen('PGU-601', 'Needs another pass.');")
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
    assert eric_reopened == {"state": "in_progress", "comment": "Needs another pass."}, eric_reopened

    insert_ticket(
        admin_conn,
        "PGU-700",
        title="Done",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
    )
    psql(service_conn, "SELECT ticket_board.mark_done('PGU-700', '123abcd');")
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

    insert_ticket(
        admin_conn,
        "PGU-705",
        title="Done exempt",
        state="director_review",
        assignee="director",
        implementation="No repo changes.",
        audit_signoff=True,
        commit_exempt=True,
    )
    psql(service_conn, "SELECT ticket_board.mark_done('PGU-705', '');")
    exempt_done = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'commit_hash', commit_hash, 'commit_exempt', commit_exempt)::text
FROM ticket_board.tickets WHERE id = 'PGU-705';
""",
        )
    )
    assert exempt_done == {"state": "done", "commit_hash": "", "commit_exempt": True}, exempt_done

    insert_ticket(admin_conn, "PGU-701", title="Defer", state="analysis")
    psql(service_conn, "SELECT ticket_board.defer('PGU-701');")
    deferred = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'parked', parked, 'manually_controlled', manually_controlled)::text
FROM ticket_board.tickets
WHERE id = 'PGU-701';
""",
        )
    )
    assert deferred == {"state": "backlog", "parked": True, "manually_controlled": False}, deferred
    psql(service_conn, "SELECT ticket_board.route('PGU-701', 'analysis', 'ops');")
    undeferred = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked, 'manually_controlled', manually_controlled)::text
FROM ticket_board.tickets
WHERE id = 'PGU-701';
""",
        )
    )
    assert undeferred == {"state": "analysis", "assignee": "ops", "parked": False, "manually_controlled": False}, undeferred

    insert_ticket(admin_conn, "PGU-702", title="Cancel", state="analysis")
    psql(service_conn, "SELECT ticket_board.cancel('PGU-702', 'Cancelled by app policy.');")
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
    assert cancelled == {"state": "cancelled", "comment": "Cancelled by app policy."}, cancelled

    insert_ticket(admin_conn, "PGU-703", title="Manual", state="analysis")
    psql(service_conn, "SELECT ticket_board.set_manually_controlled('PGU-703', true);")
    assert psql(admin_conn, "SELECT manually_controlled FROM ticket_board.tickets WHERE id = 'PGU-703';") == "t"

    insert_ticket(admin_conn, "PGU-704", title="Blockers", state="analysis")
    psql(service_conn, "SELECT ticket_board.set_blockers('PGU-704', ARRAY['PGU-100'], 'Waiting on source.');")
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

    insert_ticket(admin_conn, "PGU-800", title="Comments", state="analysis")
    psql(service_conn, "SELECT ticket_board.add_comment('PGU-800', 'comment from service');")
    assert psql(admin_conn, "SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-800';") == "comment from service"

    insert_ticket(admin_conn, "PGU-810", title="Editable", state="analysis")
    psql(service_conn, "SELECT ticket_board.edit_fields('PGU-810', '{\"title\":\"Edited\"}'::jsonb);")
    assert psql(admin_conn, "SELECT title FROM ticket_board.tickets WHERE id = 'PGU-810';") == "Edited"

    insert_ticket(admin_conn, "PGU-811", title="Commit exempt director gate", state="in_progress", assignee="ops")
    assert "commit_exempt can only be edited by director" in psql_error(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'ops', false);
SELECT ticket_board.edit_fields('PGU-811', '{"commit_exempt":true}'::jsonb);
""",
    )
    psql(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.edit_fields('PGU-811', '{"commit_exempt":true}'::jsonb);
""",
    )
    assert psql(admin_conn, "SELECT commit_exempt FROM ticket_board.tickets WHERE id = 'PGU-811';") == "t"

    insert_ticket(admin_conn, "PGU-820", title="Merge source", state="analysis")
    insert_ticket(admin_conn, "PGU-821", title="Merge target", state="analysis")
    psql(service_conn, "SELECT ticket_board.merge('PGU-820', 'PGU-821');")
    assert psql(admin_conn, "SELECT state || ':' || commit_exempt::text FROM ticket_board.tickets WHERE id = 'PGU-820';") == "done:true"


def assert_audit_direct_file_bug_grant(admin_conn: str, role_conn: dict[str, str]) -> None:
    insert_ticket(admin_conn, "PGU-150", title="Direct audit source")
    filed = psql(role_conn["audit"], "SELECT ticket_board.file_bug('Direct audit bug', 'Body', 'PGU-150');")
    filed_row = json.loads(
        psql(
            admin_conn,
            f"""
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parent_id', parent_id)::text
FROM ticket_board.tickets
WHERE id = {sql_string(filed)};
""",
        )
    )
    assert filed_row == {"state": "analysis", "assignee": "unassigned", "parent_id": "PGU-150"}, filed_row
    assert_permission_denied(role_conn["main"], "SELECT ticket_board.file_bug('Main cannot file bug', 'Body', 'PGU-150');")


def assert_structural_rules_still_apply(admin_conn: str, service_conn: str) -> None:
    assert "source_ticket_id must look like PGU-N" in psql_error(
        service_conn,
        "SELECT ticket_board.file_bug('Bad source', 'Body', 'not-a-ticket');",
    )

    insert_ticket(admin_conn, "PGU-900", title="No cancel reason", state="analysis")
    assert "comment text must be non-empty" in psql_error(
        service_conn,
        "SELECT ticket_board.cancel('PGU-900', '');",
    )
    insert_ticket(admin_conn, "PGU-910", title="Submit still needs commit", state="in_progress", assignee="ops", implementation="Done.")
    assert "commit_hash must be a 7-40 character hex commit" in psql_error(
        service_conn,
        "SELECT ticket_board.submit_to_audit('PGU-910', '');",
    )
    insert_ticket(admin_conn, "PGU-912", title="Request exempt wrong assignee", state="in_progress", assignee="ops", implementation="Done.")
    assert "app cannot call request_commit_exempt for ticket assigned to ops" in psql_error(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'app', false);
SELECT ticket_board.request_commit_exempt('PGU-912', 'No repo change.');
""",
    )
    insert_ticket(admin_conn, "PGU-913", title="Request exempt empty reason", state="in_progress", assignee="ops", implementation="Done.")
    assert "request_commit_exempt requires a non-empty reason" in psql_error(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'ops', false);
SELECT ticket_board.request_commit_exempt('PGU-913', '   ');
""",
    )
    insert_ticket(admin_conn, "PGU-914", title="Request exempt happy path", state="in_progress", assignee="ops", implementation="Done.")
    psql(
        service_conn,
        """
SELECT set_config('ticket_board.caller_role', 'ops', false);
SELECT ticket_board.request_commit_exempt('PGU-914', 'No repo change.');
""",
    )
    requested = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'commit_exempt', t.commit_exempt,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t WHERE id = 'PGU-914';
""",
        )
    )
    assert requested == {
        "state": "analysis",
        "assignee": "unassigned",
        "commit_exempt": False,
        "comment": "Commit exemption requested: No repo change.",
    }, requested
    insert_ticket(
        admin_conn,
        "PGU-911",
        title="Done still needs commit",
        state="director_review",
        assignee="director",
        implementation="Done.",
        audit_signoff=True,
    )
    assert "commit_hash must be a 7-40 character hex commit" in psql_error(
        service_conn,
        "SELECT ticket_board.mark_done('PGU-911', '');",
    )

    insert_ticket(admin_conn, "PGU-901", title="Optional implementation start", state="analysis", assignee="ops", implementation="")
    psql(service_conn, "SELECT ticket_board.start_work('PGU-901');")
    assert psql(admin_conn, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-901';") == "in_progress"

    insert_ticket(admin_conn, "PGU-905", title="Unassigned start still illegal", state="analysis", assignee="unassigned", implementation="")
    assert "assignee must not be unassigned" in psql_error(
        service_conn,
        "SELECT ticket_board.start_work('PGU-905');",
    )

    insert_ticket(admin_conn, "PGU-902", title="Bad route", state="analysis")
    assert "invalid state" in psql_error(
        service_conn,
        "SELECT ticket_board.route('PGU-902', 'bogus', 'ops');",
    )

    insert_ticket(admin_conn, "PGU-903", title="Bad blockers", state="analysis")
    assert "blocked_reason must be non-empty" in psql_error(
        service_conn,
        "SELECT ticket_board.set_blockers('PGU-903', ARRAY['PGU-100'], '');",
    )


def assert_deferred_cancel_resurrect_clears_park_and_notifies(admin_conn: str, service_conn: str) -> None:
    insert_ticket(
        admin_conn,
        "PGU-930",
        title="Deferred lifecycle",
        state="in_progress",
        assignee="ops",
        implementation="Do the thing.",
    )
    psql(admin_conn, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-930';")

    psql(service_conn, "SELECT ticket_board.defer('PGU-930');")
    deferred = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', state,
    'parked', parked,
    'manually_controlled', manually_controlled
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-930';
""",
        )
    )
    assert deferred == {"state": "backlog", "parked": True, "manually_controlled": False}, deferred

    psql(service_conn, "SELECT ticket_board.cancel('PGU-930', 'No longer needed.');")
    cancelled = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', state,
    'parked', parked,
    'manually_controlled', manually_controlled
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-930';
""",
        )
    )
    assert cancelled == {"state": "cancelled", "parked": False, "manually_controlled": False}, cancelled

    psql(admin_conn, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-930';")
    psql(service_conn, "SELECT ticket_board.route('PGU-930', 'analysis', 'ops');")
    resurrected = json.loads(
        psql(
            admin_conn,
            """
SELECT jsonb_build_object(
    'state', state,
    'assignee', assignee,
    'parked', parked,
    'manually_controlled', manually_controlled,
    'can_auto_advance', ticket_board.ticket_can_auto_advance_analysis(
        'analysis',
        assignee,
        implementation,
        manually_controlled,
        id
    ),
    'implementation_notifications', (
        SELECT count(*)::int
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = t.id
          AND q.kind = 'transition'
          AND q.target_role = 'ops'
          AND q.payload->>'new_state' = 'in_progress'
    )
)::text
FROM ticket_board.tickets t
WHERE id = 'PGU-930';
""",
        )
    )
    assert resurrected == {
        "state": "in_progress",
        "assignee": "ops",
        "parked": False,
        "manually_controlled": False,
        "can_auto_advance": True,
        "implementation_notifications": 1,
    }, resurrected


def assert_listener_can_execute_reconcile_functions(admin_conn: str, listener_conn: str, service_conn: str) -> None:
    psql(admin_conn, "DELETE FROM ticket_board.ticket_notification_queue;")
    insert_ticket(admin_conn, "PGU-950", title="Listener queue", state="analysis", assignee="ops", implementation="Ready.")
    psql(admin_conn, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-950';")

    claimed = json.loads(
        psql(
            listener_conn,
            """
SELECT row_to_json(claimed)::text
FROM ticket_board.claim_notification() AS claimed;
""",
        )
    )
    assert claimed["ticket_id"] == "PGU-950", claimed
    assert claimed["target_role"] == "ops", claimed
    assert claimed["message"] == "New ticket for you: PGU-950 -- Listener queue", claimed
    assert claimed["attempts"] == 1, claimed

    psql(listener_conn, f"SELECT ticket_board.requeue_notification({claimed['notification_id']}, interval '0 seconds', 'busy');")
    claimed_again = json.loads(
        psql(
            listener_conn,
            """
SELECT row_to_json(claimed)::text
FROM ticket_board.claim_notification() AS claimed;
""",
        )
    )
    assert claimed_again["notification_id"] == claimed["notification_id"], claimed_again
    assert claimed_again["attempts"] == 2, claimed_again

    psql(listener_conn, f"SELECT ticket_board.ack_notification({claimed_again['notification_id']});")
    after = psql(
        listener_conn,
        "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-950';",
    )
    assert after == "0", after
    trace_events = json.loads(
        psql(
            listener_conn,
            """
SELECT jsonb_agg(event ORDER BY id)::text
FROM ticket_board.notification_trace
WHERE ticket_id = 'PGU-950';
""",
        )
    )
    assert trace_events == ["enqueue", "claim", "requeue", "claim", "ack"], trace_events
    psql(
        listener_conn,
        """
SELECT ticket_board.record_notification_trace(
    'PGU-950',
    NULL,
    'ops',
    'transition',
    'manual-test',
    'idle',
    NULL,
    NULL,
    '{}'::jsonb
);
""",
    )

    assert_permission_denied(service_conn, "SELECT * FROM ticket_board.claim_notification();")
    assert_permission_denied(service_conn, f"SELECT ticket_board.ack_notification({claimed_again['notification_id']});")
    assert_permission_denied(service_conn, f"SELECT ticket_board.requeue_notification({claimed_again['notification_id']}, interval '1 second', 'x');")
    assert_permission_denied(
        service_conn,
        """
SELECT ticket_board.record_notification_trace(
    'PGU-950',
    NULL,
    'ops',
    'transition',
    'service-denied',
    NULL,
    NULL,
    NULL,
    '{}'::jsonb
);
""",
    )


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

            create_pane_roles(admin_conn, exclude={"inspector"})
            missing_inspector_before = psql(admin_conn, "SELECT to_regrole('inspector') IS NULL;")
            assert missing_inspector_before == "t", missing_inspector_before
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
                assert role_rows[role] == {"can_login": True, "password_is_null": role == "inspector"}, (role, role_rows[role])
            assert role_rows["ticket_board_service"] == {"can_login": True, "password_is_null": True}, role_rows
            assert role_rows["ticket_board_listener"] == {"can_login": True, "password_is_null": True}, role_rows

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
            assert_direct_dml_denied(admin_conn, role_conn)
            for role in PANE_ROLES:
                assert_permission_denied(role_conn[role], "SELECT ticket_board.create_ticket('Nope', 'Body');")
                assert_permission_denied(role_conn[role], "SELECT ticket_board.create_ticket('Nope', 'Body', 'backlog');")

            service_conn = role_conn["ticket_board_service"]
            listener_conn = role_conn["ticket_board_listener"]
            assert_service_can_execute_every_write_function(admin_conn, service_conn)
            assert_audit_direct_file_bug_grant(admin_conn, role_conn)
            assert_structural_rules_still_apply(admin_conn, service_conn)
            assert_deferred_cancel_resurrect_clears_park_and_notifies(admin_conn, service_conn)
            assert_listener_can_execute_reconcile_functions(admin_conn, listener_conn, service_conn)
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_rbac_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
