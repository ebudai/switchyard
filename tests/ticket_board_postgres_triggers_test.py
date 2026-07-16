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
    assignee: str = "app",
    implementation: str = "",
    parent_id: str = "",
    audit_signoff: bool = False,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
    manually_controlled: bool = False,
    parked: bool = False,
) -> None:
    bools = {
        "audit_signoff": "true" if audit_signoff else "false",
        "needs_inspection": "true" if needs_inspection else "false",
        "inspector_signoff": "true" if inspector_signoff else "false",
        "needs_eric_signoff": "true" if needs_eric_signoff else "false",
        "eric_signoff": "true" if eric_signoff else "false",
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
    audit_signoff, needs_inspection, inspector_signoff, needs_eric_signoff, eric_signoff, commit_hash,
    commit_exempt, manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '{parent_id}', '{implementation}',
    {bools['audit_signoff']}, {bools['needs_inspection']}, {bools['inspector_signoff']}, {bools['needs_eric_signoff']}, {bools['eric_signoff']}, '{commit_hash}',
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

            insert_ticket(conninfo, "PGU-1", title="Workflow", assignee="app")
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
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-11';")
            backlog_analysis = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-11';").stdout.strip()
            assert backlog_analysis == "in_progress", backlog_analysis
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '111111a' WHERE id = 'PGU-11';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-11';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-11';")

            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = '', state = 'in_progress' WHERE id = 'PGU-1';")
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

            insert_ticket(conninfo, "PGU-2", title="Parallel implementation", assignee="app", state="analysis", implementation="queued")
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

            insert_ticket(conninfo, "PGU-45120", title="Park releases current", assignee="research", state="analysis", implementation="active")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-45120';")
            insert_ticket(conninfo, "PGU-45121", title="Queued behind parked ticket", assignee="research", state="analysis", implementation="queued")
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
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4512121' WHERE id = 'PGU-45121';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-45121';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-45121';")

            insert_ticket(
                conninfo,
                "PGU-3",
                title="Linked child",
                assignee="app",
                state="analysis",
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
                assignee="ops",
                state="analysis",
                implementation="rendered",
                needs_inspection=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-30';")
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
                state="analysis",
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
                state="analysis",
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
                assignee="ops",
                state="audit",
                implementation="already submitted",
                commit_hash="3737373",
            )
            assert_error(
                conninfo,
                """
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', 'ops', false);
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
                assignee="ops",
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
                assignee="ops",
                state="inspection",
                implementation="rendered",
                needs_inspection=True,
                inspector_signoff=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-32';",
                "inspector kickback requires",
            )
            psql(
                conninfo,
                """
BEGIN;
INSERT INTO ticket_board.ticket_comments (ticket_id, position, who, ts_text, text, source_json)
VALUES (
    'PGU-32',
    COALESCE((SELECT max(position) + 1 FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-32'), 0),
    'inspector',
    '2026-07-10T00:10:00+00:00',
    'Frame has banding.',
    '{"who":"inspector","ts":"2026-07-10T00:10:00+00:00","text":"Frame has banding."}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-32';
COMMIT;
""",
            )
            inspect_kicked = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'inspector_signoff', inspector_signoff, 'audit_signoff', audit_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-32';
""",
                ).stdout
            )
            assert inspect_kicked == {"state": "in_progress", "inspector_signoff": False, "audit_signoff": False}, inspect_kicked
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
                "target_role": "ops",
                "message": "PGU-32 -- Inspection kickback kicked back to you: Frame has banding.",
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
            assert targeted_inspector_kickback == {"state": "in_progress", "assignee": "perf"}, targeted_inspector_kickback
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
            assert reopened == {"state": "analysis", "assignee": "audit", "audit_signoff": False, "commit_hash": ""}, reopened

            insert_ticket(
                conninfo,
                "PGU-40",
                title="Director kickback to implementation",
                assignee="director",
                state="director_review",
                implementation="done",
                audit_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops' WHERE id = 'PGU-40';")
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
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'unassigned' WHERE id = 'PGU-41';",
                "in_progress tickets require an implementer assignee",
            )
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
                    "in_progress tickets require an implementer assignee",
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
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops', implementation = '' WHERE id = 'PGU-42';")
            kicked_back_empty_implementation = psql(conninfo, "SELECT state || ':' || assignee || ':' || implementation FROM ticket_board.tickets WHERE id = 'PGU-42';").stdout.strip()
            assert kicked_back_empty_implementation == "in_progress:ops:", kicked_back_empty_implementation
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '4242424' WHERE id = 'PGU-42';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-42';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-42';")

            insert_ticket(conninfo, "PGU-6", title="Kickback", assignee="audit", state="audit", implementation="done")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-6';")
            kickback_assignee = psql(conninfo, "SELECT assignee FROM ticket_board.tickets WHERE id = 'PGU-6';").stdout.strip()
            assert kickback_assignee == "unassigned"

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
                title="Auto Eric",
                assignee="audit",
                state="audit",
                implementation="done",
                needs_eric_signoff=True,
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
            assert auto_eric == {"state": "eric_review", "assignee": "director", "audit_signoff": True}, auto_eric
            psql(conninfo, "UPDATE ticket_board.tickets SET eric_signoff = true WHERE id = 'PGU-8';")
            eric_done = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('state', state, 'assignee', assignee, 'eric_signoff', eric_signoff)::text
FROM ticket_board.tickets
WHERE id = 'PGU-8';
""",
                ).stdout
            )
            assert eric_done == {"state": "director_review", "assignee": "director", "eric_signoff": True}, eric_done

            insert_ticket(
                conninfo,
                "PGU-80",
                title="Eric review to inspection",
                assignee="director",
                state="eric_review",
                implementation="visual pass",
                audit_signoff=True,
                inspector_signoff=True,
                needs_eric_signoff=True,
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
                title="Eric review reinspection RPC chain",
                assignee="ops",
                state="analysis",
                implementation="visual pass",
                needs_eric_signoff=True,
                needs_inspection=True,
            )
            service_call(conninfo, "ops", "SELECT ticket_board.start_work('PGU-84');")
            service_call(conninfo, "ops", "SELECT ticket_board.submit_to_audit('PGU-84', 'abcdef4');")
            service_call(conninfo, "inspector", "SELECT ticket_board.inspector_sign_off('PGU-84');")
            service_call(conninfo, "audit", "SELECT ticket_board.audit_sign_off('PGU-84', 'Audit verified.');")
            service_call(conninfo, "director", "SELECT ticket_board.route('PGU-84', 'eric_review', 'director');")
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
            service_call(conninfo, "inspector", "SELECT ticket_board.start_task('PGU-85', 'Starting utility work.');")
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
                "state": "analysis",
                "parked": False,
                "comment": "Starting utility work.",
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

            insert_ticket(conninfo, "PGU-86", title="Direct done still blocked", assignee="inspector", state="analysis")
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
SELECT set_config('ticket_board.caller_role', 'inspector', false);
SELECT ticket_board.complete_task('PGU-84', 'Trying to skip audit.');
""",
                "complete_task requires a backlog or analysis ticket",
            )
            service_call(conninfo, "audit", "SELECT ticket_board.audit_sign_off('PGU-84', 'Audit verified after reinspection.');")
            psql(conninfo, "UPDATE ticket_board.tickets SET eric_signoff = true, state = 'director_review' WHERE id = 'PGU-84';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-84';")

            insert_ticket(
                conninfo,
                "PGU-81",
                title="Eric review inspection guard",
                assignee="director",
                state="eric_review",
                implementation="visual pass",
                audit_signoff=True,
                needs_eric_signoff=True,
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
                needs_eric_signoff=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'eric_review' WHERE id = 'PGU-82';",
                "illegal state transition: inspection -> eric_review",
            )

            insert_ticket(
                conninfo,
                "PGU-83",
                title="Eric review direct done unchanged",
                assignee="director",
                state="eric_review",
                implementation="visual pass",
                audit_signoff=True,
                needs_eric_signoff=True,
                eric_signoff=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'done', commit_exempt = true WHERE id = 'PGU-83';",
                "illegal state transition: eric_review -> done",
            )

            insert_ticket(
                conninfo,
                "PGU-9",
                title="Reaudit",
                assignee="director",
                state="eric_review",
                implementation="done",
                audit_signoff=True,
                needs_eric_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET needs_eric_signoff = false WHERE id = 'PGU-9';")
            reaudit = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'state', state,
    'audit_signoff', audit_signoff,
    'needs_eric_signoff', needs_eric_signoff,
    'eric_signoff', eric_signoff
)::text
FROM ticket_board.tickets
WHERE id = 'PGU-9';
""",
                ).stdout
            )
            assert reaudit == {
                "state": "audit",
                "audit_signoff": False,
                "needs_eric_signoff": False,
                "eric_signoff": False,
            }, reaudit

            insert_ticket(
                conninfo,
                "PGU-50",
                title="Auto implementation on insert",
                assignee="ops",
                state="analysis",
                implementation="Ready.",
            )
            auto_implementation_insert = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-50';").stdout.strip()
            assert auto_implementation_insert == "in_progress", auto_implementation_insert
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '5050505' WHERE id = 'PGU-50';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-50';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-50';")

            insert_ticket(
                conninfo,
                "PGU-51",
                title="Half specced",
                assignee="ops",
                state="analysis",
                implementation="",
            )
            half_specced = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-51';").stdout.strip()
            assert half_specced == "analysis", half_specced
            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = 'Ready now.' WHERE id = 'PGU-51';")
            auto_implementation_update = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-51';").stdout.strip()
            assert auto_implementation_update == "in_progress", auto_implementation_update
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '5151515' WHERE id = 'PGU-51';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-51';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-51';")
            insert_ticket(
                conninfo,
                "PGU-510",
                title="Non-implementer auto advance blocked",
                assignee="audit",
                state="analysis",
                implementation="Ready but no implementer.",
            )
            non_implementer_auto_state = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-510';").stdout.strip()
            assert non_implementer_auto_state == "analysis", non_implementer_auto_state

            insert_ticket(
                conninfo,
                "PGU-52",
                title="Manual analysis",
                assignee="ops",
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
                assignee="ops",
                state="analysis",
                implementation="",
                manually_controlled=True,
            )
            insert_ticket(conninfo, "PGU-540", title="Open blocker", assignee="ops", state="analysis")
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
SELECT set_config('ticket_board.notification_source_role', 'eric', false);
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_eric_signoff, eric_signoff, commit_hash,
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
    audit_signoff, needs_inspection, inspector_signoff, needs_eric_signoff, eric_signoff, commit_hash,
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
            insert_ticket(conninfo, "PGU-29710", title="Direct implementation insert", state="in_progress", assignee="ops")
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
                    "old_state": None,
                    "new_state": "in_progress",
                }
            ], direct_implementation_queue
            invalid_direct_source = ticket_source("PGU-29711", "Invalid direct implementation insert", "in_progress", "director")
            assert_error(
                conninfo,
                f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_eric_signoff, eric_signoff, commit_hash,
    commit_exempt, manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    'PGU-29711', 'Invalid direct implementation insert', '', 'in_progress', 'director', '', '',
    false, false, false, false, false, '',
    false, false, false, '2026-07-10T00:00:00+00:00',
    '2026-07-10T00:00:00+00:00', '{invalid_direct_source}'::jsonb
);
""",
                "in_progress tickets require an implementer assignee",
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET assignee = 'director' WHERE id = 'PGU-29710';",
                "in_progress tickets require an implementer assignee",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '2971010' WHERE id = 'PGU-29710';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-29710';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-29710';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-30900", title="Blocking dependency", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-30901", title="Blocked transition", state="analysis", assignee="ops", implementation="")
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
            assert blocked_transition_state == "analysis", blocked_transition_state
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
            unblocked_transition_queue = json.loads(
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
WHERE ticket_id = 'PGU-30901';
""",
                ).stdout
            )
            assert unblocked_transition_queue == [
                {
                    "target_role": "director",
                    "message": "New ticket for you: PGU-30901 -- Blocked transition",
                    "old_state": None,
                    "new_state": "analysis",
                }
            ], unblocked_transition_queue
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '3090101' WHERE id = 'PGU-30901' AND state = 'in_progress';")
            psql(conninfo, "UPDATE ticket_board.tickets SET audit_signoff = true, state = 'director_review' WHERE id = 'PGU-30901' AND state = 'audit';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'done' WHERE id = 'PGU-30901' AND state = 'director_review';")

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-30910", title="Still unresolved blocker", state="analysis", assignee="unassigned")
            insert_ticket(conninfo, "PGU-30911", title="Blocked backward move", state="in_progress", assignee="research")
            insert_ticket(conninfo, "PGU-30912", title="Blocked park move", state="analysis", assignee="ops", implementation="")
            insert_ticket(conninfo, "PGU-30913", title="Blocked cancel move", state="analysis", assignee="ops", implementation="")
            psql(
                conninfo,
                """
INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position)
VALUES
    ('PGU-30911', 'PGU-30910', 0),
    ('PGU-30912', 'PGU-30910', 0),
    ('PGU-30913', 'PGU-30910', 0);
UPDATE ticket_board.tickets SET state = 'analysis' WHERE id = 'PGU-30911';
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
            insert_ticket(conninfo, "PGU-29703", title="Auto advance insert notify", state="analysis", assignee="main", implementation="ready")
            auto_advance_insert_queue = json.loads(
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
WHERE ticket_id = 'PGU-29703';
""",
                ).stdout
            )
            assert auto_advance_insert_queue == [
                {
                    "target_role": "main",
                    "message": "New ticket for you: PGU-29703 -- Auto advance insert notify",
                    "old_state": "analysis",
                    "new_state": "in_progress",
                }
            ], auto_advance_insert_queue
            auto_advance_state = psql(conninfo, "SELECT state FROM ticket_board.tickets WHERE id = 'PGU-29703';").stdout.strip()
            assert auto_advance_state == "in_progress", auto_advance_state
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'audit', commit_hash = '2970303' WHERE id = 'PGU-29703';")
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
                "eric_review": "PGU-29706",
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
WHERE ticket_id IN ('PGU-29705', 'PGU-29706', 'PGU-29707', 'PGU-29708');
""",
            ).stdout.strip()
            assert non_transition_insert_count == "0"
            non_transition_targets = psql(
                conninfo,
                """
SELECT count(*)
FROM (VALUES ('backlog'), ('eric_review'), ('done'), ('cancelled')) AS states(state)
WHERE ticket_board.transition_target_role(states.state, 'ops') IS NOT NULL;
""",
            ).stdout.strip()
            assert non_transition_targets == "0"

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
                "eric",
                "SELECT ticket_board.create_ticket('Eric-created analysis', 'Body', 'analysis');",
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
WHERE payload->>'title' = 'Eric-created analysis';
""",
                ).stdout
            )
            assert eric_created_analysis_queue == [
                {
                    "target_role": "director",
                    "title": "Eric-created analysis",
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
            insert_ticket(conninfo, "PGU-20", title="Durable notify", state="analysis", assignee="main", implementation="")
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
            assert queued[0]["payload"]["old_state"] == "analysis", queued
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
                state="analysis",
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
            assert "WARNING:  wedged-pane nudge backstop fired for PGU-20 targeting director (" in nudge_proc.stderr
            assert "s since transition)" in nudge_proc.stderr
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
            assert nudge_state == {"last_nudged": True, "nudge_count": 1}, nudge_state
            nudge_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(kind || ':' || target_role, count)::text
FROM (
    SELECT payload->>'kind' AS kind, target_role, count(*) AS count
    FROM ticket_board.ticket_notification_queue
    WHERE ticket_id = 'PGU-20' AND kind = 'nudge'
    GROUP BY payload->>'kind', target_role
) q;
""",
                ).stdout
            )
            assert nudge_queue == {"nudge:director": 1}, nudge_queue
            deduped_nudges = psql(
                conninfo,
                "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '20 minutes', interval '5 minutes', 3);",
            ).stdout.strip()
            assert int(deduped_nudges) >= 1, deduped_nudges
            deduped_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(kind || ':' || target_role, count)::text
FROM (
    SELECT payload->>'kind' AS kind, target_role, count(*) AS count
    FROM ticket_board.ticket_notification_queue
    WHERE ticket_id = 'PGU-20' AND kind = 'nudge'
    GROUP BY payload->>'kind', target_role
) q;
""",
                ).stdout
            )
            assert deduped_queue == {"nudge:director": 1}, deduped_queue
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
    'PGU-253', 'Fresh implementation nudge guard', '', 'analysis', 'ops', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-253',
        'title', 'Fresh implementation nudge guard',
        'body', '',
        'state', 'analysis',
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
    'PGU-254', 'Stale implementation nudge', '', 'analysis', 'research', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-254',
        'title', 'Stale implementation nudge',
        'body', '',
        'state', 'analysis',
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
            assert in_progress_nudges == "1", in_progress_nudges
            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-2850", title="Open blocker for implementation", assignee="ops", state="analysis")
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
            assert cleared_implementation_nudge == {"queued": 1, "last_nudged": True, "nudge_count": 1}, cleared_implementation_nudge
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.tickets
SET commit_exempt = true,
    manually_controlled = true,
    state = 'done'
WHERE id NOT IN ('PGU-21', 'PGU-57')
  AND state IN ('in_progress', 'inspection', 'audit', 'eric_review', 'director_review')
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
    'PGU-2821', 'Deferred delivery should not nudge', '', 'analysis', 'app', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2821',
        'title', 'Deferred delivery should not nudge',
        'body', '',
        'state', 'analysis',
        'assignee', 'app',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
),
(
    'PGU-2822', 'Failed delivery should not nudge', '', 'analysis', 'perf', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2822',
        'title', 'Failed delivery should not nudge',
        'body', '',
        'state', 'analysis',
        'assignee', 'perf',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
),
(
    'PGU-2823', 'Delivered stale ticket should nudge', '', 'analysis', 'main', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2823',
        'title', 'Delivered stale ticket should nudge',
        'body', '',
        'state', 'analysis',
        'assignee', 'main',
        'comments', '[]'::jsonb,
        'created', '2026-07-11T00:00:00+00:00',
        'updated', '2026-07-11T00:00:00+00:00'
    )
),
(
    'PGU-2824', 'Live backoff queue should not nudge', '', 'analysis', 'research', 'Ready.',
    '2026-07-11T00:00:00+00:00', '2026-07-11T00:00:00+00:00',
    jsonb_build_object(
        'id', 'PGU-2824',
        'title', 'Live backoff queue should not nudge',
        'body', '',
        'state', 'analysis',
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
                "PGU-2823": {"queued": 1, "last_nudged": True, "nudge_count": 1},
                "PGU-2824": {"queued": 0, "last_nudged": False, "nudge_count": 0},
            }, delivery_suppression
            psql(
                conninfo,
                """
UPDATE ticket_board.tickets
SET state = 'audit',
    commit_hash = '2828282'
WHERE id IN ('PGU-253', 'PGU-254', 'PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824')
  AND state = 'in_progress';
UPDATE ticket_board.tickets
SET audit_signoff = true,
    state = 'director_review'
WHERE id IN ('PGU-253', 'PGU-254', 'PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824')
  AND state = 'audit';
UPDATE ticket_board.tickets
SET state = 'done'
WHERE id IN ('PGU-253', 'PGU-254', 'PGU-2821', 'PGU-2822', 'PGU-2823', 'PGU-2824')
  AND state = 'director_review';
UPDATE ticket_board.tickets
SET inspector_signoff = true,
    state = 'audit',
    commit_hash = CASE WHEN commit_hash = '' THEN '7777777' ELSE commit_hash END
WHERE state = 'inspection'
  AND id <> 'PGU-21'
  AND NOT manually_controlled
  AND NOT ticket_board.ticket_has_unresolved_blockers(id);
UPDATE ticket_board.tickets
SET state = 'audit',
    commit_hash = CASE WHEN commit_hash = '' THEN '7777777' ELSE commit_hash END
WHERE state = 'in_progress'
  AND id <> 'PGU-21'
  AND NOT manually_controlled
  AND NOT ticket_board.ticket_has_unresolved_blockers(id);
UPDATE ticket_board.tickets
SET audit_signoff = true,
    state = 'eric_review'
WHERE state = 'audit'
  AND id <> 'PGU-21'
  AND NOT manually_controlled
  AND needs_eric_signoff
  AND NOT ticket_board.ticket_has_unresolved_blockers(id);
UPDATE ticket_board.tickets
SET audit_signoff = true,
    state = 'director_review'
WHERE state = 'audit'
  AND id <> 'PGU-21'
  AND NOT manually_controlled
  AND NOT needs_eric_signoff
  AND NOT ticket_board.ticket_has_unresolved_blockers(id);
UPDATE ticket_board.tickets
SET audit_signoff = true,
    eric_signoff = true,
    state = 'director_review'
WHERE state = 'eric_review'
  AND id <> 'PGU-21'
  AND NOT manually_controlled
  AND NOT ticket_board.ticket_has_unresolved_blockers(id);
UPDATE ticket_board.tickets
SET commit_exempt = true,
    state = 'done'
WHERE state = 'director_review'
  AND id <> 'PGU-21'
  AND NOT manually_controlled
  AND NOT ticket_board.ticket_has_unresolved_blockers(id);
UPDATE ticket_board.tickets
SET manually_controlled = true
WHERE state IN ('in_progress', 'inspection', 'audit', 'eric_review', 'director_review')
  AND id <> 'PGU-21'
  AND ticket_board.ticket_has_unresolved_blockers(id);
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
    'PGU-3084', 'Repeated inspection stall escalates', '', 'inspection', 'main', 'Working.',
    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3084', 'title', 'Repeated inspection stall escalates', 'body', '', 'state', 'inspection', 'assignee', 'main', 'comments', '[]'::jsonb, 'created', '2026-07-12T00:00:00+00:00', 'updated', '2026-07-12T00:00:00+00:00')
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
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_stall_nudges(jsonb, timestamptz, interval, interval, integer) TO ticket_board_listener;
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
            resumed_after_role_activity = json.loads(
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
            assert resumed_after_role_activity == {
                "awaiting_role": "",
                "awaiting_since_cleared": True,
                "queued": {"nudge:director": 1},
                "last_nudged": True,
                "nudge_count": 1,
            }, resumed_after_role_activity
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
            wedged_backstop = json.loads(
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
            assert wedged_backstop == {
                "state": "in_progress",
                "queued": 1,
                "last_nudged": True,
                "nudge_count": 1,
            }, wedged_backstop
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
                "message": "PRIORITY PGU-21 -- Inspection notify appears stuck for perf; check/reassign",
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
    'ticket_comments_notification_activity'
)
  AND NOT tgisinternal;
""",
            ).stdout.strip()
            assert trigger_count == "7"
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_postgres_triggers_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
