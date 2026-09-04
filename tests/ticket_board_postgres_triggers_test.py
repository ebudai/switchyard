#!/usr/bin/env python3
"""Regression test: PGU ticket-board Postgres workflow triggers enforce non-timed rules."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"

from scripts.ticket_board.notify_listener import TicketBoardNotifyListener


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


def psql(conninfo: str, sql: str, *, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if expect_ok and proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"SQL unexpectedly succeeded: {sql}")
    return proc


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
    conninfo: str,
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
    parent_id: str = "",
    audit_signoff: bool = False,
    needs_audit: bool = True,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_user_signoff: bool = False,
    user_signoff: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
    manually_controlled: bool = False,
    parked: bool = False,
) -> None:
    bools = {
        "audit_signoff": "true" if audit_signoff else "false",
        "needs_audit": "true" if needs_audit else "false",
        "needs_inspection": "true" if needs_inspection else "false",
        "inspector_signoff": "true" if inspector_signoff else "false",
        "needs_user_signoff": "true" if needs_user_signoff else "false",
        "user_signoff": "true" if user_signoff else "false",
        "commit_exempt": "true" if commit_exempt else "false",
        "manually_controlled": "true" if manually_controlled else "false",
        "parked": "true" if parked else "false",
    }
    source = ticket_source(ticket_id, title, state, assignee)
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_audit, needs_inspection, inspector_signoff, needs_user_signoff, user_signoff, commit_hash,
    commit_exempt, manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '{parent_id}', '{implementation}',
    {bools['audit_signoff']}, {bools['needs_audit']}, {bools['needs_inspection']}, {bools['inspector_signoff']}, {bools['needs_user_signoff']}, {bools['user_signoff']}, '{commit_hash}',
    {bools['commit_exempt']}, {bools['manually_controlled']}, {bools['parked']}, '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00', '{source}'::jsonb
);
""",
    )


def insert_comment(conninfo: str, ticket_id: str, text: str = "Reason recorded.") -> None:
    source = json.dumps(
        {
            "who": "director",
            "text": text,
            "ts": "2026-07-10T00:10:00+00:00",
        },
        sort_keys=True,
    ).replace("'", "''")
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    '{ticket_id}',
    COALESCE((SELECT max(position) + 1 FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket_id}'), 0),
    'director',
    '2026-07-10T00:10:00+00:00',
    '{text}',
    '{source}'::jsonb
);
""",
    )


def assert_error(conninfo: str, sql: str, expected: str) -> None:
    proc = psql(conninfo, sql, expect_ok=False)
    combined = proc.stderr + proc.stdout
    assert expected in combined, combined


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def service_call(conninfo: str, caller_role: str, sql: str, *, notification_source_role: str | None = None) -> None:
    notification_source_sql = ""
    if notification_source_role is not None:
        notification_source_sql = (
            f"SELECT set_config('ticket_board.notification_source_role', "
            f"{sql_string(notification_source_role)}, false);\n"
        )
    psql(
        conninfo,
        f"""
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', {sql_string(caller_role)}, false);
{notification_source_sql}
{sql}
""",
    )


def ticket_status(conninfo: str, ticket_id: str, fields: str) -> dict[str, object]:
    return json.loads(
        psql(
            conninfo,
            f"""
SELECT jsonb_build_object({fields})::text
FROM ticket_board.tickets
WHERE id = {sql_string(ticket_id)};
""",
        ).stdout
    )


def cancel_with_fresh_comment(conninfo: str, ticket_id: str, caller_role: str, text: str) -> None:
    source = json.dumps(
        {
            "who": caller_role,
            "text": text,
            "ts": "2026-07-10T00:40:00+00:00",
        },
        sort_keys=True,
    ).replace("'", "''")
    psql(
        conninfo,
        f"""
SELECT set_config('ticket_board.caller_role', {sql_string(caller_role)}, false);
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    {sql_string(ticket_id)},
    COALESCE((SELECT max(position) + 1 FROM ticket_board.ticket_comments WHERE ticket_id = {sql_string(ticket_id)}), 0),
    {sql_string(caller_role)},
    '2026-07-10T00:40:00+00:00',
    {sql_string(text)},
    '{source}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = {sql_string(ticket_id)};
COMMIT;
""",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-triggers.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_triggers_test"
        conninfo = f"host={socket_dir} port={port} dbname={dbname}"
        listener_conninfo = f"host={socket_dir} port={port} dbname={dbname} user=ticket_board_listener"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname])
            psql(conninfo, "CREATE ROLE ticket_board_listener LOGIN;")
            psql(conninfo, "CREATE ROLE ticket_board_service;")
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(conninfo, "GRANT USAGE ON SCHEMA ticket_board TO ticket_board_service;")
            psql(
                conninfo,
                """
GRANT EXECUTE ON FUNCTION ticket_board.route(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.force_move(text, text, text, boolean) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.start_work(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_inspection(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.start_task(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.complete_task(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.audit_sign_off(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.inspector_sign_off(text) TO ticket_board_service;
""",
            )
            psql(conninfo, "GRANT USAGE ON SCHEMA ticket_board TO ticket_board_listener;")
            psql(conninfo, "GRANT SELECT ON ALL TABLES IN SCHEMA ticket_board TO ticket_board_listener;")
            psql(conninfo, "GRANT EXECUTE ON FUNCTION ticket_board.claim_notification(timestamptz, interval) TO ticket_board_listener;")
            psql(conninfo, "GRANT EXECUTE ON FUNCTION ticket_board.ack_notification(bigint) TO ticket_board_listener;")
            psql(conninfo, "GRANT EXECUTE ON FUNCTION ticket_board.requeue_notification(bigint, interval, text) TO ticket_board_listener;")
            psql(conninfo, "GRANT EXECUTE ON FUNCTION ticket_board.record_notification_trace(text, bigint, text, text, text, text, text, text, jsonb) TO ticket_board_listener;")

            insert_ticket(conninfo, "PGU-1", title="Workflow", assignee="director")
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-1';",
                "cancelling a ticket requires a non-empty comment explaining why",
            )
            insert_comment(conninfo, "PGU-1", "Old unrelated routing comment.")
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-1';",
                "cancelling a ticket requires a non-empty comment explaining why",
            )
            psql(
                conninfo,
                """
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    'PGU-1',
    COALESCE((SELECT max(position) + 1 FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-1'), 0),
    'director',
    '2026-07-10T00:20:00+00:00',
    'Cancelled with a fresh reason.',
    '{"text": "Cancelled with a fresh reason.", "ts": "2026-07-10T00:20:00+00:00", "who": "director"}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-1';
COMMIT;
""",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-1';")

            insert_ticket(conninfo, "PGU-10", title="Backlog cancel", assignee="ops", state="backlog")
            psql(
                conninfo,
                """
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    'PGU-10',
    0,
    'director',
    '2026-07-10T00:25:00+00:00',
    'Cancelled directly from backlog.',
    '{"text": "Cancelled directly from backlog.", "ts": "2026-07-10T00:25:00+00:00", "who": "director"}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-10';
COMMIT;
""",
            )
            backlog_cancelled = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-10';").stdout.strip()
            assert backlog_cancelled == "cancelled", backlog_cancelled

            insert_ticket(conninfo, "PGU-11", title="Backlog analysis", assignee="ops", state="backlog", implementation="Ready.")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-11';")
            backlog_analysis = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-11';").stdout.strip()
            assert backlog_analysis == "in_progress", backlog_analysis
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '111111a' WHERE id = 'PGU-11';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-11';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-11';")

            insert_ticket(conninfo, "PGU-12", title="Unassigned analysis handoff", assignee="unassigned", state="backlog")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-12';")
            unassigned_analysis = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'queue_target', (
        SELECT q.target_role
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = t.id
          AND q.payload->>'new_state' = 'analysis'
        ORDER BY q.id DESC
        LIMIT 1
    )
)::text
FROM ticket_board.tickets t
WHERE t.id = 'PGU-12';
""",
                ).stdout
            )
            assert unassigned_analysis == {"state": "analysis", "assignee": "director", "queue_target": "director"}, unassigned_analysis

            insert_ticket(conninfo, "PGU-82000", title="Misrouted analysis", assignee="ops", state="backlog")
            service_call(conninfo, "director", "SELECT ticket_board.route('PGU-82000', 'in_progress', 'ops');")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', assignee = 'audit', commit_hash = '8200000' WHERE id = 'PGU-82000';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review', assignee = 'director' WHERE id = 'PGU-82000';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-82000';")
            service_call(conninfo, "user", "SELECT ticket_board.user_reopen('PGU-82000', 'Needs another pass.');")
            reopened_for_analysis = ticket_status(conninfo, "PGU-82000", "'state', state, 'assignee', assignee")
            assert reopened_for_analysis == {"state": "analysis", "assignee": "director"}, reopened_for_analysis

            insert_ticket(conninfo, "PGU-82001", title="Exact owner mismatch", assignee="ops", state="backlog")
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.route('PGU-82001', 'analysis', 'ops');
""",
                "assignee ops is not an owner of stage analysis (owners: director)",
            )
            for offset, state in enumerate(("analysis", "in_progress", "inspection", "audit", "user_review", "director_review"), start=1):
                insert_ticket(conninfo, f"PGU-{82010 + offset}", title=f"Unassigned {state}", state=state, assignee="unassigned")
            unassigned_results = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object('state', state, 'assignee', assignee) ORDER BY state)::text
FROM ticket_board.tickets
WHERE id BETWEEN 'PGU-82011' AND 'PGU-82016';
""",
                ).stdout
            )
            assert unassigned_results == [
                {"state": "analysis", "assignee": "unassigned"},
                {"state": "audit", "assignee": "unassigned"},
                {"state": "director_review", "assignee": "unassigned"},
                {"state": "in_progress", "assignee": "unassigned"},
                {"state": "inspection", "assignee": "unassigned"},
                {"state": "user_review", "assignee": "unassigned"},
            ], unassigned_results
            insert_ticket(conninfo, "PGU-82002", title="Backlog unconstrained", assignee="ops", state="backlog")
            insert_ticket(conninfo, "PGU-82003", title="Done unconstrained", assignee="ops", state="done", commit_hash="8200003")

            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = '', assignee = 'app', state = 'in_progress' WHERE id = 'PGU-1';")
            handoff_notice = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-1' AND payload->>'new_state' = 'in_progress'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert handoff_notice == {
                "target_role": "app",
                "message": "New ticket for you: PGU-1 -- Workflow",
                "new_state": "in_progress",
            }, handoff_notice

            insert_ticket(conninfo, "PGU-2", title="Parallel implementation", assignee="app", state="backlog", implementation="queued")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-2';")
            queued_parallel = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked)::text
FROM ticket_board.tickets
WHERE id = 'PGU-2';
""",
                ).stdout
            )
            assert queued_parallel == {"state": "backlog", "assignee": "app", "parked": False}, queued_parallel

            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '1111111' WHERE id = 'PGU-1';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-1';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-1';")
            auto_activated_parallel = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked)::text
FROM ticket_board.tickets
WHERE id = 'PGU-2';
""",
                ).stdout
            )
            assert auto_activated_parallel == {"state": "in_progress", "assignee": "app", "parked": False}, auto_activated_parallel
            auto_activated_notice = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-2' AND payload->>'new_state' = 'in_progress'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert auto_activated_notice == {"target_role": "app", "new_state": "in_progress"}, auto_activated_notice
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '2222222' WHERE id = 'PGU-2';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-2';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-2';")

            insert_ticket(conninfo, "PGU-63800", title="Release current for activation reset", assignee="perf", state="in_progress", implementation="active")
            insert_ticket(conninfo, "PGU-63801", title="Queued activation reset", assignee="perf", state="backlog", implementation="queued")
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '3 hours',
    last_activity_at = clock_timestamp() - interval '3 hours',
    last_nudged_at = clock_timestamp() - interval '2 hours',
    nudge_count = 4
WHERE ticket_id = 'PGU-63801';
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id IN ('PGU-63800', 'PGU-63801');
""",
            )
            service_call(conninfo, "director", "SELECT ticket_board.defer('PGU-63800');")
            queued_activation_clock = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'parked', t.parked,
    'fresh_state_clock', ns.entered_current_state_at > clock_timestamp() - interval '30 seconds',
    'last_nudged_cleared', ns.last_nudged_at IS NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-63801';
""",
                ).stdout
            )
            assert queued_activation_clock == {
                "state": "in_progress",
                "parked": False,
                "fresh_state_clock": True,
                "last_nudged_cleared": True,
                "nudge_count": 0,
            }, queued_activation_clock
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '6380101' WHERE id = 'PGU-63801';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-63801';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-63801';")
            psql(conninfo, "DELETE FROM ticket_board.tickets WHERE id = 'PGU-63800';")

            insert_ticket(conninfo, "PGU-63802", title="Parked active reset", assignee="perf", state="in_progress", implementation="held", parked=True)
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '3 hours',
    last_activity_at = clock_timestamp() - interval '3 hours',
    last_nudged_at = clock_timestamp() - interval '2 hours',
    nudge_count = 5
WHERE ticket_id = 'PGU-63802';
UPDATE ticket_board.tickets SET parked = false WHERE id = 'PGU-63802';
""",
            )
            parked_active_reset = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'parked', t.parked,
    'fresh_state_clock', ns.entered_current_state_at > clock_timestamp() - interval '30 seconds',
    'last_nudged_cleared', ns.last_nudged_at IS NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-63802';
""",
                ).stdout
            )
            assert parked_active_reset == {
                "state": "in_progress",
                "parked": False,
                "fresh_state_clock": True,
                "last_nudged_cleared": True,
                "nudge_count": 0,
            }, parked_active_reset
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '3 hours',
    last_activity_at = clock_timestamp() - interval '3 hours',
    last_nudged_at = clock_timestamp() - interval '2 hours',
    nudge_count = 6
WHERE ticket_id = 'PGU-63802';
UPDATE ticket_board.tickets SET implementation = 'ordinary edit' WHERE id = 'PGU-63802';
""",
            )
            ordinary_active_edit = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state_clock_retained', ns.entered_current_state_at < clock_timestamp() - interval '2 hours',
    'last_nudged_retained', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.ticket_notification_state ns
WHERE ns.ticket_id = 'PGU-63802';
""",
                ).stdout
            )
            assert ordinary_active_edit == {
                "state_clock_retained": True,
                "last_nudged_retained": True,
                "nudge_count": 6,
            }, ordinary_active_edit
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '6380202' WHERE id = 'PGU-63802';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-63802';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-63802';")

            insert_ticket(conninfo, "PGU-63000", title="Current implementation", assignee="ops", state="in_progress", implementation="active")
            insert_ticket(conninfo, "PGU-63001", title="Deferred implementation", assignee="ops", state="backlog", implementation="queued")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id IN ('PGU-63000', 'PGU-63001');")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_notification_queue (
    ticket_id, kind, target_role, message, payload, dedupe_key,
    attempts, next_attempt_at, claimed_at, last_error, created_at, updated_at
) VALUES (
    'PGU-63001',
    'transition',
    'ops',
    'New ticket for you: PGU-63001 -- Deferred implementation',
    jsonb_build_object(
        'kind', 'transition',
        'id', 'PGU-63001',
        'title', 'Deferred implementation',
        'new_state', 'in_progress',
        'assignee', 'ops',
        'target_role', 'ops'
    ),
    'pgu630-finish-current',
    12,
    clock_timestamp() + interval '5 minutes',
    clock_timestamp(),
    'finish current',
    clock_timestamp() - interval '2 hours',
    clock_timestamp()
);
SET ROLE ticket_board_listener;
SELECT ticket_board.requeue_notification(
    (
        SELECT id
        FROM ticket_board.ticket_notification_queue
        WHERE dedupe_key = 'pgu630-finish-current'
    ),
    interval '5 minutes',
    'finish current'
);
SELECT ticket_board.requeue_notification(
    (
        SELECT id
        FROM ticket_board.ticket_notification_queue
        WHERE dedupe_key = 'pgu630-finish-current'
    ),
    interval '5 minutes',
    'finish current'
);
RESET ROLE;
""",
            )
            retry_loud_count = psql(
                conninfo,
                """
SELECT count(*)
FROM ticket_board.notification_trace nt
JOIN ticket_board.ticket_notification_queue q ON q.id = nt.notification_id
WHERE q.dedupe_key = 'pgu630-finish-current'
  AND nt.event = 'retry_loud';
""",
            ).stdout.strip()
            assert retry_loud_count == "1", retry_loud_count
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '6300000' WHERE id = 'PGU-63000';")
            finish_current_reset = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'last_error', last_error,
    'claimed_at_cleared', claimed_at IS NULL,
    'due_now', next_attempt_at <= clock_timestamp()
)::text
FROM ticket_board.ticket_notification_queue
WHERE dedupe_key = 'pgu630-finish-current';
""",
                ).stdout
            )
            assert finish_current_reset == {"last_error": None, "claimed_at_cleared": True, "due_now": True}, finish_current_reset
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE dedupe_key = 'pgu630-finish-current';")
            psql(conninfo, "DELETE FROM ticket_board.tickets WHERE id = 'PGU-63001';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-63000';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-63000';")

            insert_ticket(conninfo, "PGU-45120", title="Park releases current", assignee="research", state="backlog", implementation="active")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-45120';")
            insert_ticket(conninfo, "PGU-45121", title="Queued behind parked ticket", assignee="research", state="backlog", implementation="queued")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-45121';")
            queued_before_park = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked)::text
FROM ticket_board.tickets
WHERE id = 'PGU-45121';
""",
                ).stdout
            )
            assert queued_before_park == {"state": "backlog", "assignee": "research", "parked": False}, queued_before_park
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '20 minutes',
    last_activity_at = clock_timestamp() - interval '20 minutes',
    last_nudged_at = NULL
WHERE ticket_id = 'PGU-45121';
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-45121';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            queued_nudge_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-45121' AND kind IN ('nudge', 'escalation');",
            ).stdout.strip()
            assert queued_nudge_count == "0", queued_nudge_count
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '20 minutes',
    last_activity_at = clock_timestamp() - interval '20 minutes',
    last_nudged_at = clock_timestamp() - interval '10 minutes',
    nudge_count = 3
WHERE ticket_id = 'PGU-45121';
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-45121';
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_stall_nudges(jsonb, timestamptz, interval, interval, integer, jsonb) TO ticket_board_listener;
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('director', (now_at - interval '2 minutes')::text),
    now_at,
    interval '45 seconds',
    interval '5 minutes',
    2
)
FROM params;
RESET ROLE;
""",
            )
            queued_idle_stall_nudge_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-45121' AND kind IN ('nudge', 'escalation');",
            ).stdout.strip()
            assert queued_idle_stall_nudge_count == "0", queued_idle_stall_nudge_count
            service_call(conninfo, "director", "SELECT ticket_board.defer('PGU-45120');")
            park_activated = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(id, jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked) ORDER BY id)::text
FROM ticket_board.tickets
WHERE id IN ('PGU-45120', 'PGU-45121');
""",
                ).stdout
            )
            assert park_activated == {
                "PGU-45120": {"state": "backlog", "assignee": "research", "parked": True},
                "PGU-45121": {"state": "in_progress", "assignee": "research", "parked": False},
            }, park_activated
            activated_clock = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'fresh_state_clock', ns.entered_current_state_at > clock_timestamp() - interval '30 seconds',
    'last_nudged_cleared', ns.last_nudged_at IS NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.ticket_notification_state ns
WHERE ns.ticket_id = 'PGU-45121';
""",
                ).stdout
            )
            assert activated_clock == {
                "fresh_state_clock": True,
                "last_nudged_cleared": True,
                "nudge_count": 0,
            }, activated_clock
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4512121' WHERE id = 'PGU-45121';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-45121';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-45121';")

            insert_ticket(
                conninfo,
                "PGU-3",
                title="Linked child",
                assignee="app",
                state="backlog",
                implementation="same cluster",
                parent_id="PGU-1",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-3';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3333333' WHERE id = 'PGU-3';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-3';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-3';")

            insert_ticket(
                conninfo,
                "PGU-30",
                title="Inspection required",
                assignee="director",
                state="analysis",
                implementation="rendered",
                needs_inspection=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'app' WHERE id = 'PGU-30';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = 'abcdef0' WHERE id = 'PGU-30';")
            inspect_submit = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'inspector_signoff', inspector_signoff, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets
WHERE id = 'PGU-30';
""",
                ).stdout
            )
            assert inspect_submit == {"state": "inspection", "inspector_signoff": False, "commit_hash": "abcdef0"}, inspect_submit
            psql(conninfo, "UPDATE ticket_board.tickets SET inspector_signoff = true, state = 'audit' WHERE id = 'PGU-30';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-30';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-30';")

            insert_ticket(
                conninfo,
                "PGU-35",
                title="Submit to inspection function",
                assignee="main",
                state="backlog",
                implementation="rendered",
                needs_inspection=True,
            )
            service_call(conninfo, "main", "SELECT ticket_board.start_work('PGU-35');")
            service_call(conninfo, "main", "SELECT ticket_board.submit_to_inspection('PGU-35');")
            submitted_to_inspection = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'inspector_signoff', inspector_signoff, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets
WHERE id = 'PGU-35';
""",
                ).stdout
            )
            assert submitted_to_inspection == {
                "state": "inspection",
                "assignee": "inspector",
                "inspector_signoff": False,
                "commit_hash": "",
            }, submitted_to_inspection
            retained_reservation = psql(
                conninfo,
                "SELECT last_implementer_assignee FROM ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-35';",
            ).stdout.strip()
            assert retained_reservation == "main", retained_reservation
            service_call(conninfo, "inspector", "SELECT ticket_board.inspector_kick_back('PGU-35', 'Needs a sharper crop.');")
            service_call(conninfo, "main", "SELECT ticket_board.submit_to_inspection('PGU-35');")
            round_two_inspection = json.loads(
                psql(
                    conninfo,
                    "SELECT jsonb_build_object('state', state, 'assignee', assignee)::text FROM ticket_board.tickets WHERE id = 'PGU-35';",
                ).stdout
            )
            assert round_two_inspection == {"state": "inspection", "assignee": "inspector"}, round_two_inspection
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'audit', false);
SELECT ticket_board.submit_to_inspection('PGU-35');
""",
                "audit cannot call submit_to_inspection",
            )

            insert_ticket(
                conninfo,
                "PGU-36",
                title="Submit to inspection not applicable",
                assignee="app",
                state="backlog",
                implementation="not visual",
                needs_inspection=False,
            )
            service_call(conninfo, "app", "SELECT ticket_board.start_work('PGU-36');")
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'app', false);
SELECT ticket_board.submit_to_inspection('PGU-36');
""",
                "submit_to_inspection requires needs_inspection=true",
            )

            insert_ticket(
                conninfo,
                "PGU-37",
                title="Submit to inspection wrong state",
                assignee="app",
                state="backlog",
                implementation="already submitted",
                commit_hash="3737373",
            )
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'app', false);
SELECT ticket_board.submit_to_inspection('PGU-37');
""",
                "submit_to_inspection requires an in_progress ticket",
            )
            psql(
                conninfo,
                """
BEGIN;
SELECT set_config('ticket_board.force_move', 'on', true);
UPDATE ticket_board.tickets
SET state = 'done',
    assignee = 'director'
WHERE id IN ('PGU-35', 'PGU-36', 'PGU-37');
COMMIT;
""",
            )

            insert_ticket(
                conninfo,
                "PGU-31",
                title="Inspection gate",
                assignee="inspector",
                state="inspection",
                implementation="rendered",
                needs_inspection=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'audit' WHERE id = 'PGU-31';",
                "inspector_signoff must be true",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET inspector_signoff = true, state = 'audit' WHERE id = 'PGU-31';")
            inspect_signed = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'inspector_signoff', inspector_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-31';
""",
                ).stdout
            )
            assert inspect_signed == {"state": "audit", "inspector_signoff": True}, inspect_signed

            insert_ticket(
                conninfo,
                "PGU-32",
                title="Inspection kickback",
                assignee="inspector",
                state="inspection",
                implementation="rendered",
                needs_inspection=True,
                inspector_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'perf' WHERE id = 'PGU-32';")
            inspect_kicked = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'inspector_signoff', inspector_signoff,
    'audit_signoff', audit_signoff,
    'comment_count', (SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-32')
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-32';
""",
                ).stdout
            )
            assert inspect_kicked == {
                "state": "in_progress",
                "inspector_signoff": False,
                "audit_signoff": False,
                "comment_count": 0,
            }, inspect_kicked
            inspect_kick_notice = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-32' AND payload->>'new_state' = 'in_progress'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert inspect_kick_notice == {
                "target_role": "perf",
                "message": "PGU-32 -- Inspection kickback kicked back to you",
                "new_state": "in_progress",
            }, inspect_kick_notice

            insert_ticket(
                conninfo,
                "PGU-45010",
                title="Normal inspector kickback remembers implementer",
                assignee="app",
                state="in_progress",
                implementation="rendered",
                needs_inspection=True,
                inspector_signoff=False,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'inspection', assignee = 'inspector' WHERE id = 'PGU-45010';")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-45010';")
            service_call(
                conninfo,
                "inspector",
                "SELECT ticket_board.inspector_kick_back('PGU-45010', 'Edges need cleanup.');",
            )
            normal_inspector_kickback = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'inspector_signoff', inspector_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-45010';
""",
                ).stdout
            )
            assert normal_inspector_kickback == {
                "state": "in_progress",
                "assignee": "app",
                "inspector_signoff": False,
            }, normal_inspector_kickback
            normal_inspector_notice = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-45010' AND payload->>'new_state' = 'in_progress'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert normal_inspector_notice == {
                "target_role": "app",
                "message": "PGU-45010 -- Normal inspector kickback remembers implementer kicked back to you: Edges need cleanup.",
                "new_state": "in_progress",
            }, normal_inspector_notice

            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'inspection', assignee = 'inspector' WHERE id = 'PGU-45010';")
            service_call(
                conninfo,
                "inspector",
                "SELECT ticket_board.inspector_kick_back('PGU-45010', 'Send to perf.', 'perf');",
            )
            targeted_inspector_kickback = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee)::text
FROM ticket_board.tickets
WHERE id = 'PGU-45010';
""",
                ).stdout
            )
            assert targeted_inspector_kickback == {"state": "backlog", "assignee": "perf"}, targeted_inspector_kickback
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = 'abcdef1' WHERE id = 'PGU-32';")
            inspect_resubmitted = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'inspector_signoff', inspector_signoff, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets
WHERE id = 'PGU-32';
""",
                ).stdout
            )
            assert inspect_resubmitted == {"state": "inspection", "inspector_signoff": False, "commit_hash": "abcdef1"}, inspect_resubmitted
            psql(conninfo, "UPDATE ticket_board.tickets SET inspector_signoff = true, state = 'audit' WHERE id = 'PGU-32';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-32';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-32';")
            psql(conninfo, "UPDATE ticket_board.tickets SET inspector_signoff = true, state = 'audit', commit_hash = '4501010' WHERE id = 'PGU-45010';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-45010';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-45010';")

            insert_ticket(conninfo, "PGU-4", title="Review", assignee="audit", state="audit", implementation="done")
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'director_review' WHERE id = 'PGU-4';",
                "audit_signoff must be true",
            )
            psql(
                conninfo,
                "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-4';",
            )
            insert_ticket(
                conninfo,
                "PGU-73601",
                title="User review cannot silently bypass audit signoff",
                assignee="user",
                state="user_review",
                implementation="done",
                needs_user_signoff=True,
                user_signoff=True,
                audit_signoff=False,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'director_review' WHERE id = 'PGU-73601';",
                "audit_signoff must be true before a ticket can enter director_review",
            )
            psql(
                conninfo,
                "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-73601';",
            )
            insert_ticket(
                conninfo,
                "PGU-75901",
                title="No audit bypasses audit signoff gate",
                assignee="ops",
                state="backlog",
                implementation="done",
                needs_audit=False,
            )
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-75901'; COMMIT;")
            service_call(conninfo, "ops", "SELECT ticket_board.submit_to_audit('PGU-75901', '7590001');")
            no_audit_director_review = ticket_status(
                conninfo,
                "PGU-75901",
                "'state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'needs_audit', needs_audit",
            )
            assert no_audit_director_review == {
                "state": "director_review",
                "assignee": "director",
                "audit_signoff": False,
                "needs_audit": False,
            }, no_audit_director_review
            insert_ticket(
                conninfo,
                "PGU-75902",
                title="Audit still required",
                assignee="main",
                state="in_progress",
                implementation="done",
                needs_audit=True,
            )
            service_call(conninfo, "main", "SELECT ticket_board.submit_to_audit('PGU-75902', '7590002');")
            audit_required_status = ticket_status(
                conninfo,
                "PGU-75902",
                "'state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'needs_audit', needs_audit",
            )
            assert audit_required_status == {
                "state": "audit",
                "assignee": "audit",
                "audit_signoff": False,
                "needs_audit": True,
            }, audit_required_status
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'director_review' WHERE id = 'PGU-75902';",
                "audit_signoff must be true before a ticket can enter director_review",
            )
            psql(
                conninfo,
                "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-75902';",
            )
            insert_ticket(
                conninfo,
                "PGU-75903",
                title="Inspection only",
                assignee="app",
                state="in_progress",
                implementation="done",
                needs_inspection=True,
                needs_audit=False,
            )
            service_call(conninfo, "app", "SELECT ticket_board.submit_to_audit('PGU-75903', '7590003');")
            inspection_only_entry = ticket_status(
                conninfo,
                "PGU-75903",
                "'state', state, 'assignee', assignee, 'inspector_signoff', inspector_signoff, 'audit_signoff', audit_signoff",
            )
            assert inspection_only_entry == {
                "state": "inspection",
                "assignee": "inspector",
                "inspector_signoff": False,
                "audit_signoff": False,
            }, inspection_only_entry
            service_call(conninfo, "inspector", "SELECT ticket_board.inspector_sign_off('PGU-75903');")
            inspection_only_done = ticket_status(
                conninfo,
                "PGU-75903",
                "'state', state, 'assignee', assignee, 'inspector_signoff', inspector_signoff, 'audit_signoff', audit_signoff",
            )
            assert inspection_only_done == {
                "state": "director_review",
                "assignee": "director",
                "inspector_signoff": True,
                "audit_signoff": False,
            }, inspection_only_done
            insert_ticket(
                conninfo,
                "PGU-75904",
                title="Inspection and audit",
                assignee="perf",
                state="in_progress",
                implementation="done",
                needs_inspection=True,
                needs_audit=True,
            )
            service_call(conninfo, "perf", "SELECT ticket_board.submit_to_audit('PGU-75904', '7590004');")
            service_call(conninfo, "inspector", "SELECT ticket_board.inspector_sign_off('PGU-75904');")
            inspection_and_audit = ticket_status(
                conninfo,
                "PGU-75904",
                "'state', state, 'assignee', assignee, 'inspector_signoff', inspector_signoff, 'audit_signoff', audit_signoff",
            )
            assert inspection_and_audit == {
                "state": "audit",
                "assignee": "audit",
                "inspector_signoff": True,
                "audit_signoff": False,
            }, inspection_and_audit
            service_call(conninfo, "audit", "SELECT ticket_board.audit_sign_off('PGU-75904', 'Audit verified.');")
            both_reviewed = ticket_status(
                conninfo,
                "PGU-75904",
                "'state', state, 'assignee', assignee, 'inspector_signoff', inspector_signoff, 'audit_signoff', audit_signoff",
            )
            assert both_reviewed == {
                "state": "director_review",
                "assignee": "director",
                "inspector_signoff": True,
                "audit_signoff": True,
            }, both_reviewed
            psql(
                conninfo,
                """
UPDATE ticket_board.tickets
SET commit_exempt = true,
    state = 'done'
WHERE id IN ('PGU-75901', 'PGU-75902', 'PGU-75903', 'PGU-75904');
""",
            )
            signoff_cases = [
                ("PGU-75910", False, False, "director_review", "director", False, False),
                ("PGU-75911", False, True, "dat", "director", False, False),
                ("PGU-75912", True, False, "audit", "audit", False, False),
                ("PGU-75913", True, True, "audit", "audit", False, False),
            ]
            for ticket_id, needs_audit, needs_user_signoff, expected_state, expected_assignee, expected_audit_signoff, expected_user_signoff in signoff_cases:
                insert_ticket(
                    conninfo,
                    ticket_id,
                    title=f"Audit/UAT matrix {ticket_id}",
                    assignee="ops",
                    state="in_progress",
                    implementation="done",
                    needs_audit=needs_audit,
                    needs_user_signoff=needs_user_signoff,
                )
                psql(conninfo, f"BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops' WHERE id = '{ticket_id}'; COMMIT;")
                service_call(conninfo, "ops", f"SELECT ticket_board.submit_to_audit('{ticket_id}', 'a7591{ticket_id[-2:]}');")
                matrix_status = ticket_status(
                    conninfo,
                    ticket_id,
                    (
                        "'state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, "
                        "'needs_audit', needs_audit, 'needs_user_signoff', needs_user_signoff, "
                        "'user_signoff', user_signoff"
                    ),
                )
                assert matrix_status == {
                    "state": expected_state,
                    "assignee": expected_assignee,
                    "audit_signoff": expected_audit_signoff,
                    "needs_audit": needs_audit,
                    "needs_user_signoff": needs_user_signoff,
                    "user_signoff": expected_user_signoff,
                }, matrix_status
                if needs_audit:
                    service_call(conninfo, "audit", f"SELECT ticket_board.audit_sign_off('{ticket_id}', 'Audit verified.');")
                    after_audit_signoff = ticket_status(
                        conninfo,
                        ticket_id,
                        "'state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'user_signoff', user_signoff",
                    )
                    assert after_audit_signoff == {
                        "state": "dat" if needs_user_signoff else "director_review",
                        "assignee": "director",
                        "audit_signoff": True,
                        "user_signoff": False,
                    }, after_audit_signoff
                if needs_user_signoff:
                    service_call(conninfo, "director", f"SELECT ticket_board.director_dat_sign_off('{ticket_id}', 'DAT accepted.');")
                    uat_status = ticket_status(
                        conninfo,
                        ticket_id,
                        "'state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'user_signoff', user_signoff",
                    )
                    assert uat_status == {
                        "state": "user_review",
                        "assignee": "user",
                        "audit_signoff": needs_audit,
                        "user_signoff": False,
                    }, uat_status
                    service_call(conninfo, "user", f"SELECT ticket_board.user_sign_off('{ticket_id}', 'UAT accepted.');")
                    user_signed_status = ticket_status(
                        conninfo,
                        ticket_id,
                        "'state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'user_signoff', user_signoff",
                    )
                    assert user_signed_status == {
                        "state": "director_review",
                        "assignee": "director",
                        "audit_signoff": needs_audit,
                        "user_signoff": True,
                    }, user_signed_status
                psql(conninfo, f"UPDATE ticket_board.tickets SET commit_exempt = true, state = 'done' WHERE id = '{ticket_id}';")
            psql(
                conninfo,
                """
UPDATE ticket_board.tickets
SET commit_exempt = true,
    state = 'done'
WHERE id IN ('PGU-75910', 'PGU-75911', 'PGU-75912', 'PGU-75913');
""",
            )
            insert_ticket(
                conninfo,
                "PGU-75905",
                title="Inspection cannot skip its own signoff",
                assignee="inspector",
                state="inspection",
                implementation="done",
                needs_inspection=True,
                needs_audit=False,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'audit' WHERE id = 'PGU-75905';",
                "inspector_signoff must be true before a ticket can enter audit from inspection",
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-4';",
                "commit_hash is required",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET commit_exempt = true, state = 'done' WHERE id = 'PGU-4';")
            psql(
                conninfo,
                "UPDATE ticket_board.tickets SET audit_signoff = true, commit_hash = 'abc1234', commit_exempt = false WHERE id = 'PGU-4';",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-4';")
            reopened = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets
WHERE id = 'PGU-4';
""",
                ).stdout
            )
            assert reopened == {"state": "analysis", "assignee": "director", "audit_signoff": False, "commit_hash": ""}, reopened

            insert_ticket(
                conninfo,
                "PGU-40",
                title="Director kickback to implementation",
                assignee="director",
                state="director_review",
                implementation="done",
                audit_signoff=True,
            )
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops', audit_signoff = false WHERE id = 'PGU-40'; COMMIT;")
            director_implementation = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'assignee', assignee,
    'audit_signoff', audit_signoff,
    'commit_hash', commit_hash
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-40';
""",
                ).stdout
            )
            assert director_implementation == {
                "state": "in_progress",
                "assignee": "ops",
                "audit_signoff": False,
                "commit_hash": "",
            }, director_implementation
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4040404' WHERE id = 'PGU-40';")
            director_audit_handoff = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'assignee', assignee
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-40';
""",
                ).stdout
            )
            assert director_audit_handoff == {"state": "audit", "assignee": "audit"}, director_audit_handoff
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-40';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-40';")

            insert_ticket(
                conninfo,
                "PGU-41",
                title="Director kickback missing assignee",
                assignee="director",
                state="director_review",
                implementation="done",
                audit_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'unassigned' WHERE id = 'PGU-41';")
            assert psql(conninfo, "SELECT state || ':' || assignee FROM ticket_board.tickets WHERE id = 'PGU-41';").stdout.strip() == "in_progress:unassigned"
            for invalid_index, invalid_assignee in enumerate(("audit", "inspector", "director", "agent"), start=1):
                invalid_ticket_id = f"PGU-41{invalid_index}"
                insert_ticket(
                    conninfo,
                    invalid_ticket_id,
                    title=f"Director kickback invalid {invalid_assignee}",
                    assignee="director",
                    state="director_review",
                    implementation="done",
                    audit_signoff=True,
                )
                assert_error(
                    conninfo,
                    f"UPDATE ticket_board.tickets SET state = 'in_progress', assignee = '{invalid_assignee}' WHERE id = '{invalid_ticket_id}';",
                    f"assignee {invalid_assignee} is not an owner of stage in_progress (owners: main, app, ops, perf, research)",
                )
            insert_ticket(
                conninfo,
                "PGU-42",
                title="Director kickback empty implementation allowed",
                assignee="director",
                state="director_review",
                implementation="done",
                audit_signoff=True,
            )
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops', implementation = '', audit_signoff = false WHERE id = 'PGU-42'; COMMIT;")
            kicked_back_empty_implementation = psql(conninfo, "SELECT state || ':' || assignee || ':' || implementation FROM ticket_board.tickets WHERE id = 'PGU-42';").stdout.strip()
            assert kicked_back_empty_implementation == "in_progress:ops:", kicked_back_empty_implementation
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4242424' WHERE id = 'PGU-42';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-42';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-42';")

            insert_ticket(conninfo, "PGU-6", title="Kickback", assignee="audit", state="audit", implementation="done")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-6';")
            kickback_assignee = psql(conninfo, "SELECT assignee FROM ticket_board.tickets WHERE id = 'PGU-6';").stdout.strip()
            assert kickback_assignee == "director"

            insert_ticket(conninfo, "PGU-7", title="Auto director", assignee="audit", state="audit", implementation="done")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true WHERE id = 'PGU-7';")
            auto_director = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-7';
""",
                ).stdout
            )
            assert auto_director == {"state": "director_review", "assignee": "director", "audit_signoff": True}, auto_director

            insert_ticket(
                conninfo,
                "PGU-8",
                title="Auto User",
                assignee="audit",
                state="audit",
                implementation="done",
                needs_user_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true WHERE id = 'PGU-8';")
            auto_eric = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-8';
""",
                ).stdout
            )
            assert auto_eric == {"state": "dat", "assignee": "director", "audit_signoff": True}, auto_eric
            auto_eric_notice = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-8' AND kind = 'transition'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert auto_eric_notice == {
                "target_role": "director",
                "message": "PGU-8 -- Auto User ready for Director Acceptance Testing",
                "new_state": "dat",
            }, auto_eric_notice
            service_call(conninfo, "director", "SELECT ticket_board.director_dat_sign_off('PGU-8', 'DAT accepted.');")
            auto_uat = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'audit_signoff', audit_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-8';
""",
                ).stdout
            )
            assert auto_uat == {"state": "user_review", "assignee": "user", "audit_signoff": True}, auto_uat
            auto_uat_notice_count = psql(
                conninfo,
                """
SELECT count(*)::int
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-8'
  AND kind = 'transition'
  AND payload->>'new_state' = 'user_review';
""",
            ).stdout.strip()
            assert auto_uat_notice_count == "0", auto_uat_notice_count
            psql(conninfo, "UPDATE ticket_board.tickets SET user_signoff = true WHERE id = 'PGU-8';")
            eric_done = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'user_signoff', user_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-8';
""",
                ).stdout
            )
            assert eric_done == {"state": "director_review", "assignee": "director", "user_signoff": True}, eric_done

            insert_ticket(
                conninfo,
                "PGU-80",
                title="User review to inspection",
                assignee="user",
                state="user_review",
                implementation="visual pass",
                audit_signoff=True,
                inspector_signoff=True,
                needs_user_signoff=True,
                needs_inspection=True,
                commit_hash="abcdef8",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'inspection' WHERE id = 'PGU-80';")
            eric_to_inspection = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'needs_inspection', needs_inspection,
    'audit_signoff', audit_signoff,
    'inspector_signoff', inspector_signoff,
    'commit_hash', commit_hash
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-80';
""",
                ).stdout
            )
            assert eric_to_inspection == {
                "state": "inspection",
                "needs_inspection": True,
                "audit_signoff": False,
                "inspector_signoff": False,
                "commit_hash": "abcdef8",
            }, eric_to_inspection

            insert_ticket(
                conninfo,
                "PGU-84",
                title="User review reinspection RPC chain",
                assignee="ops",
                state="backlog",
                implementation="visual pass",
                needs_user_signoff=True,
                needs_inspection=True,
            )
            service_call(conninfo, "ops", "SELECT ticket_board.start_work('PGU-84');")
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops' WHERE id = 'PGU-84'; COMMIT;")
            service_call(conninfo, "ops", "SELECT ticket_board.submit_to_audit('PGU-84', 'abcdef4');")
            service_call(conninfo, "inspector", "SELECT ticket_board.inspector_sign_off('PGU-84');")
            service_call(conninfo, "audit", "SELECT ticket_board.audit_sign_off('PGU-84', 'Audit verified.');")
            service_call(conninfo, "director", "SELECT ticket_board.route('PGU-84', 'user_review', 'user');")
            user_review_releases_ops = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'assignee', assignee,
    'current_reserved_is_uat', coalesce(ticket_board.ticket_current_reserved_ticket('ops'), '') = id,
    'reserved_from_ticket', coalesce(ticket_board.ticket_reserved_implementer(id, state, assignee), '<none>')
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-84';
""",
                ).stdout
            )
            assert user_review_releases_ops == {
                "state": "user_review",
                "assignee": "user",
                "current_reserved_is_uat": False,
                "reserved_from_ticket": "<none>",
            }, user_review_releases_ops
            service_call(conninfo, "director", "SELECT ticket_board.route('PGU-84', 'inspection', 'inspector');")
            reinspect_entry = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'audit_signoff', audit_signoff,
    'inspector_signoff', inspector_signoff,
    'commit_hash', commit_hash
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-84';
""",
                ).stdout
            )
            assert reinspect_entry == {
                "state": "inspection",
                "audit_signoff": False,
                "inspector_signoff": False,
                "commit_hash": "abcdef4",
            }, reinspect_entry
            service_call(conninfo, "inspector", "SELECT ticket_board.inspector_sign_off('PGU-84');")
            reinspect_signed = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'audit_signoff', audit_signoff,
    'inspector_signoff', inspector_signoff,
    'commit_hash', commit_hash
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-84';
""",
                ).stdout
            )
            assert reinspect_signed == {
                "state": "audit",
                "audit_signoff": False,
                "inspector_signoff": True,
                "commit_hash": "abcdef4",
            }, reinspect_signed

            insert_ticket(
                conninfo,
                "PGU-85",
                title="Inspector utility task",
                assignee="inspector",
                state="backlog",
                parked=True,
            )
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'inspector', false);
SELECT ticket_board.start_task('PGU-85', 'Starting utility work.');
""",
                "assignee inspector is not an owner of stage analysis (owners: director)",
            )
            utility_started = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'parked', t.parked,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t
WHERE id = 'PGU-85';
""",
                ).stdout
            )
            assert utility_started == {
                "state": "backlog",
                "parked": True,
                "comment": None,
            }, utility_started
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'audit', false);
SELECT ticket_board.complete_task('PGU-85', 'Wrong assignee completion.');
""",
                "audit cannot call complete_task for ticket assigned to inspector",
            )
            service_call(conninfo, "inspector", "SELECT ticket_board.complete_task('PGU-85', 'Inspector task complete.');")
            utility_done = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'commit_exempt', t.commit_exempt,
    'commit_hash', t.commit_hash,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t
WHERE id = 'PGU-85';
""",
                ).stdout
            )
            assert utility_done == {
                "state": "done",
                "commit_exempt": True,
                "commit_hash": "",
                "comment": "Inspector task complete.",
            }, utility_done

            insert_ticket(conninfo, "PGU-86", title="Direct done still blocked", assignee="director", state="analysis")
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET commit_exempt = true, state = 'done' WHERE id = 'PGU-86';",
                "illegal state transition: analysis -> done",
            )
            insert_ticket(conninfo, "PGU-34001", title="Force move to done", assignee="unassigned", state="analysis")
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT ticket_board.route('PGU-34001', 'done', 'director');
""",
                "illegal state transition: analysis -> done",
            )
            service_call(conninfo, "director", "SELECT ticket_board.force_move('PGU-34001', 'done', 'director', true);")
            forced_done = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'commit_hash', t.commit_hash,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t
WHERE t.id = 'PGU-34001';
""",
                ).stdout
            )
            assert forced_done == {
                "state": "done",
                "assignee": "director",
                "commit_hash": "",
                "comment": "Director override: analysis/unassigned -> done/director (notification suppressed)",
            }, forced_done

            insert_ticket(conninfo, "PGU-47401", title="Force move active work", assignee="app", state="backlog", implementation="active")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-47401';")
            insert_ticket(conninfo, "PGU-47402", title="Force move queued work", assignee="unassigned", state="analysis", implementation="queued")
            service_call(conninfo, "director", "SELECT ticket_board.force_move('PGU-47402', 'in_progress', 'app', false);")
            force_move_queued = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'parked', t.parked,
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t
WHERE t.id = 'PGU-47402';
""",
                ).stdout
            )
            assert force_move_queued == {
                "state": "backlog",
                "assignee": "app",
                "parked": False,
                "comment": "Director override: analysis/unassigned -> in_progress/app (queued: implementer already has active work)",
            }, force_move_queued
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4740101' WHERE id = 'PGU-47401';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-47401';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-47401';")
            force_move_auto_activated = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'parked', parked)::text
FROM ticket_board.tickets
WHERE id = 'PGU-47402';
""",
                ).stdout
            )
            assert force_move_auto_activated == {"state": "in_progress", "assignee": "app", "parked": False}, force_move_auto_activated
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4740202' WHERE id = 'PGU-47402';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-47402';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-47402';")

            insert_ticket(conninfo, "PGU-34002", title="Force move notifies", assignee="unassigned", state="analysis")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34002';")
            service_call(conninfo, "director", "SELECT ticket_board.force_move('PGU-34002', 'audit', 'audit', false);")
            assert psql(conninfo, "SELECT count(*)::int FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34002';").stdout.strip() == "1"

            insert_ticket(conninfo, "PGU-34003", title="Force move suppresses", assignee="unassigned", state="analysis")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34003';")
            service_call(conninfo, "director", "SELECT ticket_board.force_move('PGU-34003', 'audit', 'audit', true);")
            assert psql(conninfo, "SELECT count(*)::int FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34003';").stdout.strip() == "0"

            insert_ticket(conninfo, "PGU-34005", title="Force move blocker", assignee="unassigned", state="analysis")
            insert_ticket(conninfo, "PGU-34006", title="Force move blocked target", assignee="unassigned", state="analysis")
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34006';
DELETE FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-34006';
DELETE FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-34006';
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-34006', 'PGU-34005', 0);
""",
            )
            service_call(conninfo, "director", "SELECT ticket_board.force_move('PGU-34006', 'audit', 'audit', false);")
            blocked_override_record = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'assignee', t.assignee,
    'queue_count', (SELECT count(*)::int FROM ticket_board.ticket_notification_queue WHERE ticket_id = t.id),
    'trace_count', (SELECT count(*)::int FROM ticket_board.notification_trace WHERE ticket_id = t.id),
    'comment', (SELECT text FROM ticket_board.ticket_comments WHERE ticket_id = t.id ORDER BY position DESC LIMIT 1)
)::text
FROM ticket_board.tickets t
WHERE t.id = 'PGU-34006';
""",
                ).stdout
            )
            assert blocked_override_record == {
                "state": "audit",
                "assignee": "audit",
                "queue_count": 0,
                "trace_count": 0,
                "comment": "Director override: analysis/unassigned -> audit/audit",
            }, blocked_override_record

            insert_ticket(conninfo, "PGU-34004", title="Force move invalid in-progress assignee", assignee="unassigned", state="analysis")
            service_call(conninfo, "director", "SELECT ticket_board.force_move('PGU-34004', 'in_progress', 'director', true);")
            forced_in_progress = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee)::text
FROM ticket_board.tickets
WHERE id = 'PGU-34004';
""",
                ).stdout
            )
            assert forced_in_progress == {"state": "in_progress", "assignee": "director"}, forced_in_progress

            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'audit', false);
SELECT ticket_board.complete_task('PGU-84', 'Trying to skip audit.');
""",
                "complete_task requires a backlog or analysis ticket",
            )
            service_call(conninfo, "audit", "SELECT ticket_board.audit_sign_off('PGU-84', 'Audit verified after reinspection.');")
            service_call(conninfo, "director", "SELECT ticket_board.director_dat_sign_off('PGU-84', 'DAT accepted after reinspection.');")
            psql(conninfo, "UPDATE ticket_board.tickets SET user_signoff = true, state = 'director_review' WHERE id = 'PGU-84';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-84';")

            insert_ticket(
                conninfo,
                "PGU-81",
                title="User review inspection guard",
                assignee="user",
                state="user_review",
                implementation="visual pass",
                audit_signoff=True,
                needs_user_signoff=True,
                needs_inspection=False,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'inspection' WHERE id = 'PGU-81';",
                "needs_inspection must be true",
            )

            insert_ticket(
                conninfo,
                "PGU-82",
                title="Inspection reverse unchanged",
                assignee="inspector",
                state="inspection",
                implementation="visual pass",
                needs_inspection=True,
                needs_user_signoff=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'user_review' WHERE id = 'PGU-82';",
                "illegal state transition: inspection -> user_review",
            )

            insert_ticket(
                conninfo,
                "PGU-83",
                title="User review direct done unchanged",
                assignee="user",
                state="user_review",
                implementation="visual pass",
                audit_signoff=True,
                needs_user_signoff=True,
                user_signoff=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'done', commit_exempt = true WHERE id = 'PGU-83';",
                "illegal state transition: user_review -> done",
            )

            insert_ticket(
                conninfo,
                "PGU-9",
                title="Reaudit",
                assignee="user",
                state="user_review",
                implementation="done",
                audit_signoff=True,
                needs_user_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET needs_user_signoff = false WHERE id = 'PGU-9';")
            reaudit = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'audit_signoff', audit_signoff,
    'needs_user_signoff', needs_user_signoff,
    'user_signoff', user_signoff
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-9';
""",
                ).stdout
            )
            assert reaudit == {
                "state": "director_review",
                "audit_signoff": True,
                "needs_user_signoff": False,
                "user_signoff": False,
            }, reaudit

            insert_ticket(
                conninfo,
                "PGU-75920",
                title="DAT no longer needs UAT",
                assignee="director",
                state="dat",
                implementation="done",
                audit_signoff=False,
                needs_audit=False,
                needs_user_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET needs_user_signoff = false WHERE id = 'PGU-75920';")
            dat_without_uat = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'audit_signoff', audit_signoff,
    'needs_audit', needs_audit,
    'needs_user_signoff', needs_user_signoff,
    'user_signoff', user_signoff
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-75920';
""",
                ).stdout
            )
            assert dat_without_uat == {
                "state": "director_review",
                "audit_signoff": False,
                "needs_audit": False,
                "needs_user_signoff": False,
                "user_signoff": False,
            }, dat_without_uat

            insert_ticket(
                conninfo,
                "PGU-50",
                title="Analysis stays with owner on insert",
                assignee="director",
                state="analysis",
                implementation="Ready.",
            )
            auto_implementation_insert = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-50';").stdout.strip()
            assert auto_implementation_insert == "analysis", auto_implementation_insert
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'done', assignee = 'director', commit_exempt = true WHERE id = 'PGU-50'; COMMIT;")

            insert_ticket(
                conninfo,
                "PGU-51",
                title="Half specced",
                assignee="director",
                state="analysis",
                implementation="",
            )
            half_specced = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-51';").stdout.strip()
            assert half_specced == "analysis", half_specced
            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = 'Ready now.' WHERE id = 'PGU-51';")
            auto_implementation_update = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-51';").stdout.strip()
            assert auto_implementation_update == "analysis", auto_implementation_update
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'done', assignee = 'director', commit_exempt = true WHERE id = 'PGU-51'; COMMIT;")
            insert_ticket(
                conninfo,
                "PGU-510",
                title="Non-implementer auto advance blocked",
                assignee="director",
                state="analysis",
                implementation="Ready but no implementer.",
            )
            non_implementer_auto_state = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-510';").stdout.strip()
            assert non_implementer_auto_state == "analysis", non_implementer_auto_state

            insert_ticket(
                conninfo,
                "PGU-52",
                title="Manual analysis",
                assignee="director",
                state="analysis",
                implementation="Ready but held.",
                manually_controlled=True,
            )
            manual_analysis = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-52';").stdout.strip()
            assert manual_analysis == "analysis", manual_analysis

            insert_ticket(
                conninfo,
                "PGU-53",
                title="Unassigned analysis",
                assignee="unassigned",
                state="analysis",
                implementation="Ready but unrouted.",
            )
            unassigned_analysis = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-53';").stdout.strip()
            assert unassigned_analysis == "analysis", unassigned_analysis

            insert_ticket(
                conninfo,
                "PGU-54",
                title="Blocked analysis",
                assignee="director",
                state="analysis",
                implementation="",
                manually_controlled=True,
            )
            insert_ticket(conninfo, "PGU-540", title="Open blocker", assignee="unassigned", state="analysis")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-54', 'PGU-540', 0);
UPDATE ticket_board.tickets
SET manually_controlled = false
WHERE id = 'PGU-54';
""",
            )
            blocked_analysis = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-54';").stdout.strip()
            assert blocked_analysis == "analysis", blocked_analysis

            insert_ticket(
                conninfo,
                "PGU-55",
                title="Assigned backlog",
                assignee="audit",
                state="backlog",
                implementation="Parked but assigned.",
            )
            insert_ticket(
                conninfo,
                "PGU-56",
                title="Explicitly deferred backlog",
                assignee="ops",
                state="backlog",
                implementation="Intentionally parked.",
                parked=True,
            )
            insert_ticket(
                conninfo,
                "PGU-57",
                title="Manual implementation",
                assignee="ops",
                state="in_progress",
                implementation="Ready but held.",
                manually_controlled=True,
            )
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops' WHERE id = 'PGU-57'; COMMIT;")

            insert_ticket(
                conninfo,
                "PGU-5",
                title="Manual",
                assignee="unassigned",
                state="analysis",
                manually_controlled=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-5';")
            manual_state = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-5';").stdout.strip()
            assert manual_state == "done"

            notify_function = psql(
                conninfo,
                """
SELECT pg_get_functiondef('ticket_board.notify_ticket_state_transition()'::regprocedure)
  LIKE '%enqueue_transition_notification%';
""",
            ).stdout.strip()
            assert notify_function == "t"
            combined_notify_trigger = psql(
                conninfo,
                """
SELECT count(*)
FROM pg_trigger
WHERE tgrelid = 'ticket_board.tickets'::regclass
  AND tgname = 'tickets_zzz_notify_transition'
  AND NOT tgisinternal
  AND (tgtype & 4) = 4
  AND (tgtype & 16) = 16;
""",
            ).stdout.strip()
            assert combined_notify_trigger == "1"
            notify_helper = psql(
                conninfo,
                """
SELECT pg_get_functiondef('ticket_board.enqueue_transition_notification(text,text,text,text,text,timestamp with time zone,integer,text)'::regprocedure)
  LIKE '%pg_notify%ticket_board_state_transition%';
""",
            ).stdout.strip()
            assert notify_helper == "t"

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-29701", title="Analysis insert notify", state="analysis", assignee="unassigned")
            analysis_insert_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'kind', kind,
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state',
    'payload_kind', payload->>'kind'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-29701';
""",
                ).stdout
            )
            assert analysis_insert_queue == [
                {
                    "kind": "transition",
                    "target_role": "director",
                    "message": "New ticket for you: PGU-29701 -- Analysis insert notify",
                    "old_state": None,
                    "new_state": "analysis",
                    "payload_kind": "transition",
                }
            ], analysis_insert_queue
            listener_sent: list[tuple[str, str]] = []
            listener = TicketBoardNotifyListener(
                conninfo=listener_conninfo,
                sender=lambda target, message: listener_sent.append((target, message)),
                activity_gate=lambda _target: False,
                poll_seconds=0,
            )
            assert listener.listen_once(max_notifications=1) == 1
            assert listener_sent == [
                ("pgu-director:0.0", "New ticket for you: PGU-29701 -- Analysis insert notify")
            ], listener_sent
            analysis_pending_after_listener = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-29701';",
            ).stdout.strip()
            assert analysis_pending_after_listener == "0"

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            web_created_ticket_id = "PGU-37201"
            web_created_source = ticket_source(
                web_created_ticket_id,
                "Human web create notify",
                "analysis",
                "unassigned",
            )
            psql(
                conninfo,
                f"""
SELECT set_config('ticket_board.caller_role', 'director', false);
SELECT set_config('ticket_board.notification_source_role', 'user', false);
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_user_signoff, user_signoff, commit_hash,
    commit_exempt, manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    '{web_created_ticket_id}', 'Human web create notify', '', 'analysis', 'unassigned', '', '',
    false, false, false, false, false, '',
    false, false, false, '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00', '{web_created_source}'::jsonb
);
""",
            )
            web_create_queue = json.loads(
                psql(
                    conninfo,
                    f"""
SELECT jsonb_agg(jsonb_build_object(
    'kind', kind,
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state',
    'payload_kind', payload->>'kind'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = '{web_created_ticket_id}';
""",
                ).stdout
            )
            assert web_create_queue == [
                {
                    "kind": "transition",
                    "target_role": "director",
                    "message": f"New ticket for you: {web_created_ticket_id} -- Human web create notify",
                    "old_state": None,
                    "new_state": "analysis",
                    "payload_kind": "transition",
                }
            ], web_create_queue

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            agent_created_ticket_id = "PGU-37202"
            agent_created_source = ticket_source(
                agent_created_ticket_id,
                "Director self-create suppress",
                "analysis",
                "unassigned",
            )
            psql(
                conninfo,
                f"""
SELECT set_config('ticket_board.caller_role', 'director', false);
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_user_signoff, user_signoff, commit_hash,
    commit_exempt, manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    '{agent_created_ticket_id}', 'Director self-create suppress', '', 'analysis', 'unassigned', '', '',
    false, false, false, false, false, '',
    false, false, false, '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00', '{agent_created_source}'::jsonb
);
""",
            )
            direct_create_self_suppressed = psql(
                conninfo,
                f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{agent_created_ticket_id}';",
            ).stdout.strip()
            assert direct_create_self_suppressed == "0", direct_create_self_suppressed

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-29710", title="Direct implementation insert", state="backlog", assignee="ops")
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops' WHERE id = 'PGU-29710'; COMMIT;")
            direct_implementation_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-29710';
""",
                ).stdout
            )
            assert direct_implementation_queue == [
                {
                    "target_role": "ops",
                    "message": "New ticket for you: PGU-29710 -- Direct implementation insert",
                    "old_state": "backlog",
                    "new_state": "in_progress",
                }
            ], direct_implementation_queue
            invalid_direct_source = ticket_source("PGU-29711", "Invalid direct implementation insert", "in_progress", "director")
            assert_error(
                conninfo,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_user_signoff, user_signoff, commit_hash,
    commit_exempt, manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    'PGU-29711', 'Invalid direct implementation insert', '', 'in_progress', 'director', '', '',
    false, false, false, false, false, '',
    false, false, false, '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00', '{invalid_direct_source}'::jsonb
);
""",
                "assignee director is not an owner of stage in_progress (owners: main, app, ops, perf, research)",
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET assignee = 'director' WHERE id = 'PGU-29710';",
                "assignee director is not an owner of stage in_progress (owners: main, app, ops, perf, research)",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '2971010' WHERE id = 'PGU-29710';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-29710';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-29710';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-30900", title="Blocking dependency", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-30901", title="Blocked transition", state="backlog", assignee="ops", implementation="")
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-30901', 'PGU-30900', 0);
""",
            )
            blocked_transition_error = psql(
                conninfo,
                """
UPDATE ticket_board.tickets
SET implementation = 'ready',
    state = 'in_progress'
WHERE id = 'PGU-30901';
""",
                expect_ok=False,
            ).stderr
            assert "unresolved blocker prevents forward promotion: PGU-30900" in blocked_transition_error, blocked_transition_error
            blocked_transition_state = psql(
                conninfo,
                "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-30901';",
            ).stdout.strip()
            assert blocked_transition_state == "backlog", blocked_transition_state
            blocker_before_resolution = psql(
                conninfo,
                "SELECT resolved::text FROM ticket_board.ticket_blockers WHERE ticket_id = 'PGU-30901' AND blocker_ticket_id = 'PGU-30900';",
            ).stdout.strip()
            assert blocker_before_resolution == "false", blocker_before_resolution
            blocked_transition_queue_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-30901';",
            ).stdout.strip()
            assert blocked_transition_queue_count == "0", blocked_transition_queue_count
            psql(
                conninfo,
                """
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    'PGU-30900',
    0,
    'director',
    '2026-07-10T00:30:00+00:00',
    'Cancelling to clear dependent blocker.',
    '{"text": "Cancelling to clear dependent blocker.", "ts": "2026-07-10T00:30:00+00:00", "who": "director"}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-30900';
COMMIT;
""",
            )
            blocker_after_resolution = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'row_exists', count(*) = 1,
    'resolved', bool_or(resolved)
)::text
FROM ticket_board.ticket_blockers
WHERE ticket_id = 'PGU-30901'
  AND blocker_ticket_id = 'PGU-30900';
""",
                ).stdout
            )
            assert blocker_after_resolution == {"row_exists": True, "resolved": True}, blocker_after_resolution
            unblocked_transition_queue_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-30901';",
            ).stdout.strip()
            assert unblocked_transition_queue_count == "0", unblocked_transition_queue_count
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops' WHERE id = 'PGU-30901'; COMMIT;")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3090101' WHERE id = 'PGU-30901' AND state = 'in_progress';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-30901' AND state = 'audit';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-30901' AND state = 'director_review';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-30910", title="Still unresolved blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-30911", title="Blocked backward move", state="in_progress", assignee="research")
            insert_ticket(conninfo, "PGU-30912", title="Blocked park move", state="backlog", assignee="ops", implementation="")
            insert_ticket(conninfo, "PGU-30913", title="Blocked cancel move", state="backlog", assignee="ops", implementation="")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES
    ('PGU-30911', 'PGU-30910', 0),
    ('PGU-30912', 'PGU-30910', 0),
    ('PGU-30913', 'PGU-30910', 0);
UPDATE ticket_board.tickets SET state = 'analysis', assignee = 'director' WHERE id = 'PGU-30911';
UPDATE ticket_board.tickets SET state = 'backlog' WHERE id = 'PGU-30912';
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    'PGU-30913',
    0,
    'director',
    '2026-07-10T00:35:00+00:00',
    'Cancelling while dependency remains unresolved.',
    '{"text": "Cancelling while dependency remains unresolved.", "ts": "2026-07-10T00:35:00+00:00", "who": "director"}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'cancelled' WHERE id = 'PGU-30913';
COMMIT;
""",
            )
            blocked_non_forward_moves = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(
    jsonb_build_object(
        'id', t.id,
        'state', t.state,
        'resolved', b.resolved
    )
    ORDER BY t.id
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_blockers b ON b.ticket_id = t.id
WHERE t.id IN ('PGU-30911', 'PGU-30912', 'PGU-30913');
""",
                ).stdout
            )
            assert blocked_non_forward_moves == [
                {"id": "PGU-30911", "state": "analysis", "resolved": False},
                {"id": "PGU-30912", "state": "backlog", "resolved": False},
                {"id": "PGU-30913", "state": "cancelled", "resolved": False},
            ], blocked_non_forward_moves

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-29703", title="Auto advance insert notify", state="backlog", assignee="main", implementation="ready")
            auto_advance_insert = ticket_status(
                conninfo,
                "PGU-29703",
                "'state', state, 'queue_count', (SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-29703')",
            )
            assert auto_advance_insert == {"state": "backlog", "queue_count": 0}, auto_advance_insert
            insert_ticket(conninfo, "PGU-47100", title="Create auto activate duplicate", state="analysis", assignee="director")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            psql(
                conninfo,
                """
BEGIN;
SELECT ticket_board.enqueue_transition_notification(
    'PGU-47100',
    'Create auto activate duplicate',
    NULL,
    'analysis',
    'director',
    clock_timestamp(),
    47100,
    ticket_board.transition_message('PGU-47100', 'Create auto activate duplicate', NULL, 'analysis', false)
);
SELECT ticket_board.enqueue_transition_notification(
    'PGU-47100',
    'Create auto activate duplicate',
    'analysis',
    'in_progress',
    'director',
    clock_timestamp(),
    47100,
    ticket_board.transition_message('PGU-47100', 'Create auto activate duplicate', 'analysis', 'in_progress', false)
);
COMMIT;
""",
            )
            coalesced_create_activate_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state',
    'dedupe_prefix', split_part(dedupe_key, ':', 1) || ':' || split_part(dedupe_key, ':', 2)
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-47100';
""",
                ).stdout
            )
            assert coalesced_create_activate_queue == [
                {
                    "target_role": "director",
                    "message": "New ticket for you: PGU-47100 -- Create auto activate duplicate",
                    "old_state": "analysis",
                    "new_state": "in_progress",
                    "dedupe_prefix": "transition:new_ticket",
                }
            ], coalesced_create_activate_queue
            psql(conninfo, "BEGIN; SET LOCAL ticket_board.force_move = 'on'; UPDATE ticket_board.tickets SET state = 'audit', assignee = 'audit', commit_hash = '2970303' WHERE id = 'PGU-29703'; COMMIT;")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-29703';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-29703';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(
                conninfo,
                "PGU-29704",
                title="Manual insert suppress",
                state="analysis",
                assignee="unassigned",
                manually_controlled=True,
            )
            manual_insert_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-29704';",
            ).stdout.strip()
            assert manual_insert_count == "0"

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            non_transition_ids = {
                "backlog": "PGU-29705",
                "done": "PGU-29707",
                "cancelled": "PGU-29708",
            }
            for state, ticket_id in non_transition_ids.items():
                insert_ticket(
                    conninfo,
                    ticket_id,
                    title=f"{state} insert suppress",
                    state=state,
                    assignee="ops",
                )
            non_transition_insert_count = psql(
                conninfo,
                """
SELECT count(*)
FROM ticket_board.ticket_notification_queue
WHERE ticket_id IN ('PGU-29705', 'PGU-29707', 'PGU-29708');
""",
            ).stdout.strip()
            assert non_transition_insert_count == "0"
            non_transition_targets = psql(
                conninfo,
                """
SELECT count(*)
FROM (VALUES ('backlog'), ('done'), ('cancelled')) AS states(state)
WHERE ticket_board.transition_target_role(states.state, 'ops') IS NOT NULL;
""",
            ).stdout.strip()
            assert non_transition_targets == "0"
            user_review_target = psql(
                conninfo,
                "SELECT coalesce(ticket_board.transition_target_role('user_review', 'user'), '<none>');",
            ).stdout.strip()
            assert user_review_target == "<none>"

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.create_ticket('Director self-create analysis', 'Body', 'analysis');",
            )
            director_self_create_queue_count = psql(
                conninfo,
                """
SELECT count(*)
FROM ticket_board.ticket_notification_queue
WHERE payload->>'title' = 'Director self-create analysis'
  AND target_role = 'director';
""",
            ).stdout.strip()
            assert director_self_create_queue_count == "0", director_self_create_queue_count

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            service_call(
                conninfo,
                "user",
                "SELECT ticket_board.create_ticket('User-created analysis', 'Body', 'analysis');",
            )
            eric_created_analysis_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'title', payload->>'title',
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE payload->>'title' = 'User-created analysis';
""",
                ).stdout
            )
            assert eric_created_analysis_queue == [
                {
                    "target_role": "director",
                    "title": "User-created analysis",
                    "old_state": None,
                    "new_state": "analysis",
                }
            ], eric_created_analysis_queue

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-33801", title="Audit kickback notifies implementation", state="audit", assignee="audit", implementation="done")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            service_call(
                conninfo,
                "audit",
                "SELECT ticket_board.audit_kick_back('PGU-33801', 'Needs another implementation pass.', 'main');",
            )
            audit_kickback_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-33801';
""",
                ).stdout
            )
            assert audit_kickback_queue == [
                {
                    "target_role": "main",
                    "message": "PGU-33801 -- Audit kickback notifies implementation kicked back to you",
                    "old_state": "audit",
                    "new_state": "in_progress",
                }
            ], audit_kickback_queue
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3380101' WHERE id = 'PGU-33801';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-33801';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-33801';")

            insert_ticket(
                conninfo,
                "PGU-45091",
                title="Normal audit kickback remembers implementer",
                state="in_progress",
                assignee="main",
                implementation="done",
                commit_hash="4501111",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', assignee = 'audit' WHERE id = 'PGU-45091';")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-45091';")
            service_call(
                conninfo,
                "audit",
                "SELECT ticket_board.audit_kick_back('PGU-45091', 'Needs another implementation pass.');",
            )
            normal_audit_kickback = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'assignee', assignee,
    'commit_hash', commit_hash,
    'last_rejected_commit', last_rejected_commit
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-45091';
""",
                ).stdout
            )
            assert normal_audit_kickback == {
                "state": "in_progress",
                "assignee": "main",
                "commit_hash": "",
                "last_rejected_commit": "4501111",
            }, normal_audit_kickback
            normal_audit_notice = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-45091' AND payload->>'new_state' = 'in_progress'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert normal_audit_notice == {
                "target_role": "main",
                "message": "PGU-45091 -- Normal audit kickback remembers implementer kicked back to you",
                "new_state": "in_progress",
            }, normal_audit_notice
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4509191' WHERE id = 'PGU-45091';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-45091';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-45091';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-33802", title="Director route still notifies main", state="analysis", assignee="unassigned", implementation="")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.route('PGU-33802', 'in_progress', 'main');",
            )
            director_route_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-33802';
""",
                ).stdout
            )
            assert director_route_queue == [
                {
                    "target_role": "main",
                    "message": "New ticket for you: PGU-33802 -- Director route still notifies main",
                    "old_state": "analysis",
                    "new_state": "in_progress",
                }
            ], director_route_queue
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3380202' WHERE id = 'PGU-33802';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-33802';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-33802';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-34210", title="Cross-role blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-34211", title="Cross-role dependent", state="in_progress", assignee="main")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-34211', 'PGU-34210', 0);
""",
            )
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            cancel_with_fresh_comment(conninfo, "PGU-34210", "director", "Cancelling blocker for another role.")
            cross_role_unblock_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-34211';
""",
                ).stdout
            )
            assert cross_role_unblock_queue == [
                {
                    "target_role": "main",
                    "message": "New ticket for you: PGU-34211 -- Cross-role dependent",
                    "old_state": None,
                    "new_state": "in_progress",
                }
            ], cross_role_unblock_queue
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3421111' WHERE id = 'PGU-34211';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-34211';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-34211';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-34220", title="Self-owned blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-34221", title="Self-owned dependent", state="analysis", assignee="unassigned")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-34221', 'PGU-34220', 0);
""",
            )
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            cancel_with_fresh_comment(conninfo, "PGU-34220", "director", "Cancelling blocker for my own dependent.")
            self_owned_unblock_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-34221';
""",
                ).stdout
            )
            assert self_owned_unblock_queue == [
                {
                    "target_role": "director",
                    "message": "New ticket for you: PGU-34221 -- Self-owned dependent",
                    "old_state": None,
                    "new_state": "analysis",
                }
            ], self_owned_unblock_queue

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-34222", title="Self-owned implementation blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-34223", title="Self-owned implementation dependent", state="in_progress", assignee="main")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-34223', 'PGU-34222', 0);
""",
            )
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            cancel_with_fresh_comment(conninfo, "PGU-34222", "main", "Cancelling blocker for my own implementation ticket.")
            self_owned_in_progress_unblock_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'old_state', payload->>'old_state',
    'new_state', payload->>'new_state'
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-34223';
""",
                ).stdout
            )
            assert self_owned_in_progress_unblock_queue == [
                {
                    "target_role": "main",
                    "message": "New ticket for you: PGU-34223 -- Self-owned implementation dependent",
                    "old_state": None,
                    "new_state": "in_progress",
                }
            ], self_owned_in_progress_unblock_queue
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3422323' WHERE id = 'PGU-34223';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-34223';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-34223';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-34230", title="Direct self-suppress route", state="backlog", assignee="unassigned")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.route('PGU-34230', 'analysis', 'unassigned');",
            )
            direct_self_transition_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34230';",
            ).stdout.strip()
            assert direct_self_transition_count == "0", direct_self_transition_count

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-34240", title="Backlog blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-34241", title="Backlog dependent", state="backlog", assignee="unassigned")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-34241', 'PGU-34240', 0);
""",
            )
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            cancel_with_fresh_comment(conninfo, "PGU-34240", "director", "Cancelling blocker for backlog triage.")
            backlog_unblock_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34241';",
            ).stdout.strip()
            assert backlog_unblock_count == "0", backlog_unblock_count

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-34242", title="Assigned backlog blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-34243", title="Assigned backlog dependent", state="backlog", assignee="ops")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-34243', 'PGU-34242', 0);
""",
            )
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            cancel_with_fresh_comment(conninfo, "PGU-34242", "director", "Cancelling blocker for assigned backlog triage.")
            assigned_backlog_unblock_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-34243';",
            ).stdout.strip()
            assert assigned_backlog_unblock_count == "0", assigned_backlog_unblock_count

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-20", title="Durable notify", state="backlog", assignee="main", implementation="")
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-20';")
            psql(conninfo, "DELETE FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-20';")
            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = 'ready', state = 'in_progress' WHERE id = 'PGU-20';")
            queued = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_agg(jsonb_build_object(
    'id', ticket_id,
    'kind', kind,
    'target_role', target_role,
    'message', message,
    'payload', payload
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-20';
""",
                ).stdout
            )
            assert queued[0]["id"] == "PGU-20", queued
            assert queued[0]["kind"] == "transition", queued
            assert queued[0]["target_role"] == "main", queued
            assert queued[0]["message"] == "New ticket for you: PGU-20 -- Durable notify", queued
            assert queued[0]["payload"]["old_state"] == "backlog", queued
            assert queued[0]["payload"]["new_state"] == "in_progress", queued
            claimed = json.loads(
                psql(
                    listener_conninfo,
                    """
SELECT row_to_json(claimed)::text
FROM ticket_board.claim_notification() AS claimed;
""",
                ).stdout
            )
            assert claimed["ticket_id"] == "PGU-20", claimed
            assert claimed["target_role"] == "main", claimed
            psql(listener_conninfo, f"SELECT ticket_board.ack_notification({claimed['notification_id']});")
            pending_after_ack = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-20';",
            ).stdout.strip()
            assert pending_after_ack == "0"
            durable_trace = json.loads(
                psql(
                    listener_conninfo,
                    """
SELECT jsonb_agg(
    jsonb_build_object(
        'event', event,
        'state', ticket_state_at_event,
        'assignee', ticket_assignee_at_event,
        'target_role', target_role,
        'kind', kind
    )
    ORDER BY id
)::text
FROM ticket_board.notification_trace
WHERE ticket_id = 'PGU-20';
""",
                ).stdout
            )
            assert durable_trace == [
                {
                    "event": "enqueue",
                    "state": "in_progress",
                    "assignee": "main",
                    "target_role": "main",
                    "kind": "transition",
                },
                {
                    "event": "claim",
                    "state": "in_progress",
                    "assignee": "main",
                    "target_role": "main",
                    "kind": "transition",
                },
                {
                    "event": "ack",
                    "state": "in_progress",
                    "assignee": "main",
                    "target_role": "main",
                    "kind": "transition",
                },
            ], durable_trace
            transition_marked_at = psql(
                listener_conninfo,
                """
SELECT (last_transition_notified_at IS NOT NULL)::text
FROM ticket_board.ticket_notification_state
WHERE ticket_id = 'PGU-20';
""",
            ).stdout.strip()
            assert transition_marked_at == "true", transition_marked_at

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(
                conninfo,
                "PGU-21",
                title="Inspection notify",
                state="backlog",
                assignee="perf",
                implementation="rendered",
                needs_inspection=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-21';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = 'abcdef1' WHERE id = 'PGU-21';")
            inspection_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'new_state', payload->>'new_state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-21' AND payload->>'new_state' = 'inspection'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert inspection_queue == {
                "target_role": "inspector",
                "message": "PGU-21 -- Inspection notify ready for inspection",
                "new_state": "inspection",
            }, inspection_queue

            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id NOT IN ('PGU-20', 'PGU-21');
""",
            )
            nudge_proc = psql(
                conninfo,
                "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '10 minutes', interval '5 minutes', 3);",
            )
            nudges = nudge_proc.stdout.strip()
            assert int(nudges) >= 1, nudges
            assert "WARNING:  wedged-pane nudge backstop fired for PGU-20 targeting director (" not in nudge_proc.stderr
            nudge_state = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('last_nudged', last_nudged_at IS NOT NULL, 'nudge_count', nudge_count)::text
FROM ticket_board.ticket_notification_state
WHERE ticket_id = 'PGU-20';
""",
                ).stdout
            )
            assert nudge_state == {"last_nudged": False, "nudge_count": 0}, nudge_state
            nudge_queue_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-20' AND kind = 'nudge';",
            ).stdout.strip()
            assert nudge_queue_count == "0", nudge_queue_count
            psql(
                conninfo,
                "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '20 minutes', interval '5 minutes', 3);",
            )
            deduped_queue_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-20' AND kind = 'nudge';",
            ).stdout.strip()
            assert deduped_queue_count == "0", deduped_queue_count
            inspection_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'state', payload->>'state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-21' AND kind = 'nudge' AND target_role = 'director'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert inspection_nudge == {
                "target_role": "director",
                "message": "NUDGE PGU-21 -- Inspection notify ready for inspection",
                "state": "inspection",
            }, inspection_nudge
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '2020202' WHERE id = 'PGU-20';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-20';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-20';")
            psql(conninfo, "UPDATE ticket_board.ticket_notification_state SET last_implementer_assignee = '' WHERE ticket_id = 'PGU-21';")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-253', 'Fresh implementation nudge guard', '', 'backlog', 'ops', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-253',
        'title', 'Fresh implementation nudge guard',
        'body', '',
        'state', 'backlog',
        'assignee', 'ops',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-253';
SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '1 minute', interval '5 minutes', 3);
""",
            )
            fresh_implementation_nudge = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-253' AND kind = 'nudge';",
            ).stdout.strip()
            assert fresh_implementation_nudge == "0", fresh_implementation_nudge
            psql(
                conninfo,
                """
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'PGU-254', 'Stale implementation nudge', '', 'backlog', 'research', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-254',
        'title', 'Stale implementation nudge',
        'body', '',
        'state', 'backlog',
        'assignee', 'research',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-254';
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_activity_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = clock_timestamp() - interval '10 minutes',
    nudge_count = 3
WHERE ticket_id = 'PGU-254';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            in_progress_nudges = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-254' AND kind IN ('nudge', 'escalation');",
            ).stdout.strip()
            assert in_progress_nudges == "0", in_progress_nudges
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-2850", title="Open blocker for implementation", assignee="unassigned", state="analysis")
            insert_ticket(conninfo, "PGU-2852", title="Cleared blocker for implementation", assignee="ops", state="done")
            insert_ticket(conninfo, "PGU-2851", title="Blocked implementation nudge", assignee="app", state="in_progress", implementation="Ready.")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES ('PGU-2851', 'PGU-2850', 0);
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id <> 'PGU-2851';
UPDATE ticket_board.ticket_notification_state
SET last_activity_at = clock_timestamp() - interval '1 hour',
    entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = NULL,
    nudge_count = 0
WHERE ticket_id = 'PGU-2851';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            blocked_implementation_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'queued', (
        SELECT count(*)::int
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = t.id
          AND q.kind IN ('nudge', 'escalation')
    ),
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-2851';
""",
                ).stdout
            )
            assert blocked_implementation_nudge == {"queued": 0, "last_nudged": False, "nudge_count": 0}, blocked_implementation_nudge
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_blockers
SET blocker_ticket_id = 'PGU-2852'
WHERE ticket_id = 'PGU-2851';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            cleared_implementation_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'queued', (
        SELECT count(*)::int
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = t.id
          AND q.kind IN ('nudge', 'escalation')
    ),
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-2851';
""",
                ).stdout
            )
            assert cleared_implementation_nudge == {"queued": 0, "last_nudged": False, "nudge_count": 0}, cleared_implementation_nudge
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.tickets
SET commit_exempt = true,
    manually_controlled = true,
    state = 'done'
WHERE id NOT IN ('PGU-21', 'PGU-57')
              AND state IN ('in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
  AND (
      assignee IN ('app', 'ops')
      OR id IN (
          SELECT ticket_id
          FROM ticket_board.ticket_notification_state
          WHERE last_implementer_assignee IN ('app', 'ops')
      )
  );
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES
(
    'PGU-2821', 'Deferred delivery should not nudge', '', 'backlog', 'app', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2821',
        'title', 'Deferred delivery should not nudge',
        'body', '',
        'state', 'backlog',
        'assignee', 'app',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
),
(
    'PGU-2822', 'Failed delivery should not nudge', '', 'backlog', 'perf', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2822',
        'title', 'Failed delivery should not nudge',
        'body', '',
        'state', 'backlog',
        'assignee', 'perf',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
),
(
    'PGU-2823', 'Delivered stale ticket should nudge', '', 'backlog', 'main', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2823',
        'title', 'Delivered stale ticket should nudge',
        'body', '',
        'state', 'backlog',
        'assignee', 'main',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
),
(
    'PGU-2824', 'Live backoff queue should not nudge', '', 'backlog', 'research', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2824',
        'title', 'Live backoff queue should not nudge',
        'body', '',
        'state', 'backlog',
        'assignee', 'research',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
);
UPDATE ticket_board.tickets
SET state = 'in_progress'
WHERE id IN ('PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824');
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id NOT IN ('PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824');
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_activity_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = NULL,
    nudge_count = 0
WHERE ticket_id IN ('PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824');
INSERT INTO ticket_board.ticket_notification_queue (
    ticket_id,
    kind,
    target_role,
    message,
    payload,
    dedupe_key,
    next_attempt_at,
    last_error
) VALUES (
    'PGU-2824',
    'transition',
    'research',
    'PGU-2824 -- Live backoff queue should not nudge',
    jsonb_build_object(
        'kind', 'transition',
        'id', 'PGU-2824',
        'new_state', 'in_progress',
        'assignee', 'research'
    ),
    'transition:PGU-2824:research',
    clock_timestamp() + interval '30 seconds',
    'pane busy'
);
SELECT ticket_board.record_notification_trace(
    'PGU-2821',
    NULL,
    'app',
    'transition',
    'gate_defer',
    'busy',
    'pane active',
    NULL,
    '{}'::jsonb
);
SELECT ticket_board.record_notification_trace(
    'PGU-2821',
    NULL,
    'app',
    'transition',
    'requeue',
    NULL,
    'pane busy',
    NULL,
    '{}'::jsonb
);
SELECT ticket_board.record_notification_trace(
    'PGU-2822',
    NULL,
    'perf',
    'transition',
    'send_failed',
    NULL,
    'pane unreachable',
    NULL,
    '{}'::jsonb
);
SELECT ticket_board.record_notification_trace(
    'PGU-2822',
    NULL,
    'perf',
    'transition',
    'requeue',
    NULL,
    'pane unreachable',
    NULL,
    '{}'::jsonb
);
SELECT ticket_board.record_notification_trace(
    'PGU-2823',
    NULL,
    'main',
    'transition',
    'send',
    NULL,
    NULL,
    NULL,
    '{}'::jsonb
);
SELECT ticket_board.record_notification_trace(
    'PGU-2823',
    NULL,
    'main',
    'transition',
    'listener_ack',
    NULL,
    NULL,
    NULL,
    '{}'::jsonb
);
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            delivery_suppression = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(ticket_id, row_json ORDER BY ticket_id)::text
FROM (
    SELECT
        t.id AS ticket_id,
        jsonb_build_object(
            'queued', (
                SELECT count(*)::int
                FROM ticket_board.ticket_notification_queue q
                WHERE q.ticket_id = t.id
                  AND q.kind IN ('nudge', 'escalation')
            ),
            'last_nudged', ns.last_nudged_at IS NOT NULL,
            'nudge_count', ns.nudge_count
        ) AS row_json
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.id IN ('PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824')
) s;
""",
                ).stdout
            )
            assert delivery_suppression == {
                "PGU-2821": {"queued": 0, "last_nudged": False, "nudge_count": 0},
                "PGU-2822": {"queued": 0, "last_nudged": False, "nudge_count": 0},
                "PGU-2823": {"queued": 0, "last_nudged": False, "nudge_count": 0},
                "PGU-2824": {"queued": 0, "last_nudged": False, "nudge_count": 0},
            }, delivery_suppression
            psql(
                conninfo,
                """
BEGIN;
SET LOCAL ticket_board.force_move = 'on';
UPDATE ticket_board.tickets
SET state = 'done',
    assignee = 'director',
    commit_exempt = true,
    inspector_signoff = true,
    audit_signoff = true,
    user_signoff = true,
    manually_controlled = true
WHERE state IN ('in_progress', 'inspection', 'audit', 'dat', 'user_review', 'director_review')
  AND id NOT IN ('PGU-21', 'PGU-57');
COMMIT;
""",
            )
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES
(
    'PGU-3081', 'Idle implementation stall', '', 'in_progress', 'research', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3081', 'title', 'Idle implementation stall', 'body', '', 'state', 'in_progress', 'assignee', 'research', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
),
(
    'PGU-3082', 'Busy implementation is not nudged', '', 'in_progress', 'main', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3082', 'title', 'Busy implementation is not nudged', 'body', '', 'state', 'in_progress', 'assignee', 'main', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
),
(
    'PGU-3083', 'Fresh handoff is not nudged', '', 'in_progress', 'perf', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3083', 'title', 'Fresh handoff is not nudged', 'body', '', 'state', 'in_progress', 'assignee', 'perf', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
),
(
    'PGU-3084', 'Repeated inspection stall escalates', '', 'inspection', 'inspector', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3084', 'title', 'Repeated inspection stall escalates', 'body', '', 'state', 'inspection', 'assignee', 'inspector', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
),
(
    'PGU-3085', 'Held director review is not idle-nudged', '', 'director_review', 'director', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3085', 'title', 'Held director review is not idle-nudged', 'body', '', 'state', 'director_review', 'assignee', 'director', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
),
(
    'PGU-4531', 'Awaiting perf is not idle-nudged', '', 'in_progress', 'app', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-4531', 'title', 'Awaiting perf is not idle-nudged', 'body', '', 'state', 'in_progress', 'assignee', 'app', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
),
(
    'PGU-4532', 'Expired awaiting marker nudges again', '', 'audit', 'audit', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
	    jsonb_build_object('id', 'PGU-4532', 'title', 'Expired awaiting marker nudges again', 'body', '', 'state', 'audit', 'assignee', 'audit', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
	);
	BEGIN;
	SET LOCAL ticket_board.force_move = 'on';
	UPDATE ticket_board.tickets
	SET state = 'in_progress',
	    assignee = 'research'
	WHERE id = 'PGU-3081';
	COMMIT;
	UPDATE ticket_board.ticket_notification_state
	SET entered_current_state_at = clock_timestamp() - interval '10 minutes',
	    last_activity_at = clock_timestamp() - interval '10 minutes',
    last_nudged_at = NULL,
    nudge_count = 0
WHERE ticket_id IN ('PGU-3081', 'PGU-3082', 'PGU-3084', 'PGU-3085', 'PGU-4531', 'PGU-4532');
UPDATE ticket_board.ticket_notification_state
SET awaiting_role = 'perf',
    awaiting_since_at = clock_timestamp() - interval '10 minutes'
WHERE ticket_id = 'PGU-4531';
UPDATE ticket_board.ticket_notification_state
SET awaiting_role = 'inspector',
    awaiting_since_at = clock_timestamp() - interval '5 hours'
WHERE ticket_id = 'PGU-4532';
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '20 seconds',
    last_activity_at = clock_timestamp() - interval '20 seconds',
    last_nudged_at = NULL,
    nudge_count = 0
WHERE ticket_id = 'PGU-3083';
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() - interval '6 minutes',
    nudge_count = 2
WHERE ticket_id = 'PGU-3084';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener;
    END IF;
END;
$$;
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_stall_nudges(jsonb, timestamptz, interval, interval, integer, jsonb) TO ticket_board_listener;
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object(
        'research', (now_at - interval '2 minutes')::text,
        'app', (now_at - interval '2 minutes')::text,
        'audit', (now_at - interval '2 minutes')::text,
        'perf', (now_at - interval '2 minutes')::text,
        'inspector', (now_at - interval '2 minutes')::text,
        'director', (now_at - interval '2 minutes')::text
    ),
    now_at,
    interval '45 seconds',
    interval '5 minutes',
    2
)
FROM params;
RESET ROLE;
""",
            )
            idle_stall_nudges = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(ticket_id, row_json ORDER BY ticket_id)::text
FROM (
    SELECT
        t.id AS ticket_id,
        jsonb_build_object(
            'state', t.state,
            'queued', (
                SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)
                FROM (
                    SELECT kind, target_role, count(*)::int AS count
                    FROM ticket_board.ticket_notification_queue q
                    WHERE q.ticket_id = t.id
                      AND q.kind IN ('nudge', 'escalation')
                    GROUP BY kind, target_role
                ) q_counts
            ),
            'last_nudged', ns.last_nudged_at IS NOT NULL,
            'nudge_count', ns.nudge_count
        ) AS row_json
    FROM ticket_board.tickets t
    JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
    WHERE t.id IN ('PGU-3081', 'PGU-3082', 'PGU-3083', 'PGU-3084', 'PGU-3085', 'PGU-4531', 'PGU-4532')
) s;
""",
                ).stdout
            )
            assert idle_stall_nudges == {
                "PGU-3081": {
                    "state": "in_progress",
                    "queued": {"nudge:director": 1},
                    "last_nudged": True,
                    "nudge_count": 1,
                },
                "PGU-3082": {
                    "state": "in_progress",
                    "queued": None,
                    "last_nudged": False,
                    "nudge_count": 0,
                },
                "PGU-3083": {
                    "state": "in_progress",
                    "queued": None,
                    "last_nudged": False,
                    "nudge_count": 0,
                },
                "PGU-3084": {
                    "state": "inspection",
                    "queued": {"escalation:director": 1},
                    "last_nudged": True,
                    "nudge_count": 3,
                },
                "PGU-3085": {
                    "state": "director_review",
                    "queued": None,
                    "last_nudged": False,
                    "nudge_count": 0,
                },
                "PGU-4531": {
                    "state": "in_progress",
                    "queued": None,
                    "last_nudged": False,
                    "nudge_count": 0,
                },
                "PGU-4532": {
                    "state": "audit",
                    "queued": {"nudge:director": 1},
                    "last_nudged": True,
                    "nudge_count": 1,
                },
            }, idle_stall_nudges
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '20 minutes',
    last_activity_at = clock_timestamp() - interval '20 minutes',
    last_nudged_at = clock_timestamp() - interval '10 minutes',
    nudge_count = 2
WHERE ticket_id = 'PGU-3084';
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('inspector', (now_at - interval '2 minutes')::text),
    now_at,
    interval '45 seconds',
    interval '5 minutes',
    2,
    jsonb_build_object('inspector', (now_at - interval '2 minutes')::text)
)
FROM params;
RESET ROLE;
""",
            )
            work_evidence_suppressed_escalation = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'queued', (
        SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)
        FROM (
            SELECT kind, target_role, count(*)::int AS count
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = 'PGU-3084'
              AND q.kind IN ('nudge', 'escalation')
            GROUP BY kind, target_role
        ) q_counts
    ),
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.ticket_notification_state ns
WHERE ns.ticket_id = 'PGU-3084';
""",
                ).stdout
            )
            assert work_evidence_suppressed_escalation == {
                "queued": {"nudge:director": 1},
                "nudge_count": 1,
            }, work_evidence_suppressed_escalation
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id <> 'PGU-4531';
SELECT ticket_board.append_ticket_comment('PGU-4531', 'perf', 'Perf measurement posted.');
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('app', (now_at - interval '2 minutes')::text),
    now_at,
    interval '45 seconds',
    interval '5 minutes',
    2
)
FROM params;
RESET ROLE;
""",
            )
            # PGU-909: perf commenting its measurement no longer clears the flag.
            # Until PGU-909 this asserted the opposite -- a comment by the awaited
            # role cleared awaiting_role and let nudges resume. That convenience is
            # what let a director's "seen, still blocked" silently drop an
            # escalation, so the awaited role now clears explicitly with
            # clear_awaiting_role. The flag holding also holds the suppression,
            # which is the property PGU-453 was really protecting.
            still_awaiting_after_role_comment = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'awaiting_role', ns.awaiting_role,
    'awaiting_since_cleared', ns.awaiting_since_at IS NULL,
    'queued', (
        SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)
        FROM (
            SELECT kind, target_role, count(*)::int AS count
            FROM ticket_board.ticket_notification_queue q
            WHERE q.ticket_id = 'PGU-4531'
              AND q.kind IN ('nudge', 'escalation')
            GROUP BY kind, target_role
        ) q_counts
    ),
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.ticket_notification_state ns
WHERE ns.ticket_id = 'PGU-4531';
""",
                ).stdout
            )
            assert still_awaiting_after_role_comment == {
                "awaiting_role": "perf",
                "awaiting_since_cleared": False,
                "queued": None,
                "last_nudged": False,
                "nudge_count": 0,
            }, still_awaiting_after_role_comment
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id <> 'PGU-3082';
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '1 hour',
    last_activity_at = clock_timestamp() - interval '1 hour',
    last_nudged_at = NULL,
    nudge_count = 0
WHERE ticket_id = 'PGU-3082';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            active_work_backstop = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', t.state,
    'queued', (
        SELECT count(*)::int
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = t.id
          AND q.kind = 'nudge'
          AND q.target_role = 'director'
    ),
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-3082';
""",
                ).stdout
            )
            assert active_work_backstop == {
                "state": "in_progress",
                "queued": 0,
                "last_nudged": False,
                "nudge_count": 0,
            }, active_work_backstop
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.ticket_notification_state
SET last_activity_at = clock_timestamp() - interval '20 minutes',
    entered_current_state_at = clock_timestamp() - interval '20 minutes',
    last_nudged_at = clock_timestamp() - interval '10 minutes',
    nudge_count = 3
WHERE ticket_id = 'PGU-21';
SELECT ticket_board.notify_due_nudges(clock_timestamp(), interval '5 minutes', 3);
""",
            )
            inspection_escalation = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('kind', kind, 'target_role', target_role, 'message', message)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-21'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert inspection_escalation == {
                "kind": "escalation",
                "target_role": "director",
                "message": "PRIORITY PGU-21 -- Inspection notify appears stuck for inspector; check/reassign",
            }, inspection_escalation
            escalation_nudge_copies = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-21' AND kind = 'nudge' AND target_role = 'director';",
            ).stdout.strip()
            assert escalation_nudge_copies == "0", escalation_nudge_copies

            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = clock_timestamp() + interval '1 hour'
WHERE ticket_id NOT IN ('PGU-52', 'PGU-53', 'PGU-54', 'PGU-55', 'PGU-56', 'PGU-57');

UPDATE ticket_board.ticket_notification_state
SET last_nudged_at = NULL,
    entered_current_state_at = clock_timestamp() - interval '10 minutes'
WHERE ticket_id IN ('PGU-52', 'PGU-53', 'PGU-54', 'PGU-55', 'PGU-56', 'PGU-57');
""",
            )
            stale_now = "clock_timestamp()"
            stale_nudges = psql(
                conninfo,
                f"SELECT ticket_board.notify_due_nudges({stale_now}, interval '5 minutes', 3);",
            ).stdout.strip()
            assert int(stale_nudges) >= 1, stale_nudges
            manual_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'manually_controlled', manually_controlled,
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count,
    'queued', (
        SELECT count(*)::int
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = t.id
          AND q.kind IN ('nudge', 'escalation')
    )
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-52';
""",
                ).stdout
            )
            assert manual_nudge == {
                "state": "analysis",
                "manually_controlled": True,
                "last_nudged": False,
                "nudge_count": 0,
                "queued": 0,
            }, manual_nudge
            unassigned_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'last_nudged', ns.last_nudged_at IS NOT NULL, 'nudge_count', ns.nudge_count)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-53';
""",
                ).stdout
            )
            assert unassigned_nudge == {"state": "analysis", "last_nudged": True, "nudge_count": 1}, unassigned_nudge

            stale_nudges_second = psql(
                conninfo,
                f"SELECT ticket_board.notify_due_nudges({stale_now}, interval '5 minutes', 3);",
            ).stdout.strip()
            assert int(stale_nudges_second) >= 0, stale_nudges_second
            blocked_analysis_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'blocked_by_count', count(b.blocker_ticket_id), 'last_nudged', ns.last_nudged_at IS NOT NULL, 'nudge_count', ns.nudge_count)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
LEFT JOIN ticket_board.ticket_blockers b ON b.ticket_id = t.id
WHERE t.id = 'PGU-54'
GROUP BY t.state, ns.last_nudged_at, ns.nudge_count;
""",
                ).stdout
            )
            assert blocked_analysis_nudge == {
                "state": "analysis",
                "blocked_by_count": 1,
                "last_nudged": False,
                "nudge_count": 0,
            }, blocked_analysis_nudge
            insert_ticket(conninfo, "PGU-541", title="Done blocker", assignee="ops", state="done")
            psql(
                conninfo,
                """
UPDATE ticket_board.ticket_blockers
SET blocker_ticket_id = 'PGU-541'
WHERE ticket_id = 'PGU-54';
""",
            )
            stale_nudges_after_unblock = psql(
                conninfo,
                f"SELECT ticket_board.notify_due_nudges({stale_now}, interval '5 minutes', 3);",
            ).stdout.strip()
            assert int(stale_nudges_after_unblock) >= 1, stale_nudges_after_unblock
            unblocked_analysis_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'blocked_by_count', count(b.blocker_ticket_id), 'last_nudged', ns.last_nudged_at IS NOT NULL, 'nudge_count', ns.nudge_count)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
LEFT JOIN ticket_board.ticket_blockers b ON b.ticket_id = t.id
WHERE t.id = 'PGU-54'
GROUP BY t.state, ns.last_nudged_at, ns.nudge_count;
""",
                ).stdout
            )
            assert unblocked_analysis_nudge == {
                "state": "analysis",
                "blocked_by_count": 1,
                "last_nudged": True,
                "nudge_count": 1,
            }, unblocked_analysis_nudge

            backlog_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'last_nudged', ns.last_nudged_at IS NOT NULL, 'nudge_count', ns.nudge_count)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-55';
""",
                ).stdout
            )
            assert backlog_nudge == {"state": "backlog", "last_nudged": True, "nudge_count": 1}, backlog_nudge
            parked_backlog_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'parked', parked,
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-56';
""",
                ).stdout
            )
            assert parked_backlog_nudge == {
                "state": "backlog",
                "parked": True,
                "last_nudged": False,
                "nudge_count": 0,
            }, parked_backlog_nudge
            manual_implementation_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'manually_controlled', manually_controlled,
    'last_nudged', ns.last_nudged_at IS NOT NULL,
    'nudge_count', ns.nudge_count
)::text
FROM ticket_board.tickets t
JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id
WHERE t.id = 'PGU-57';
""",
                ).stdout
            )
            assert manual_implementation_nudge == {
                "state": "in_progress",
                "manually_controlled": True,
                "last_nudged": False,
                "nudge_count": 0,
            }, manual_implementation_nudge

            trigger_count = psql(
                conninfo,
                """
SELECT count(*)
FROM pg_trigger
WHERE tgname IN (
    'tickets_enforce_workflow_insert',
    'tickets_enforce_workflow_update',
    'tickets_notification_state_insert',
    'tickets_notification_state_update',
    'tickets_zz_auto_advance_analysis',
    'tickets_zzz_notify_transition',
    'tickets_zzzzx_reset_finish_current_notifications',
    'ticket_comments_notification_activity'
)
  AND NOT tgisinternal;
""",
            ).stdout.strip()
            assert trigger_count == "8"
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_postgres_triggers_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
