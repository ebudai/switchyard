#!/usr/bin/env python3
"""Regression test: PGU ticket-board Postgres workflow triggers enforce non-timed rules."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"


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


def service_call(conninfo: str, caller_role: str, sql: str) -> None:
    psql(
        conninfo,
        f"""
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', {sql_string(caller_role)}, false);
{sql}
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
GRANT EXECUTE ON FUNCTION ticket_board.start_work(text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.submit_to_audit(text, text) TO ticket_board_service;
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

            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-1';",
                "implementation must be non-empty",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = 'ship it', state = 'in_progress' WHERE id = 'PGU-1';")
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
            parallel_implementation_count = psql(
                conninfo,
                "SELECT count(*) FROM ticket_board.tickets WHERE state = 'in_progress' AND assignee = 'app';",
            ).stdout.strip()
            assert parallel_implementation_count == "2", parallel_implementation_count

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
SELECT jsonb_build_object('state', state, 'audit_signoff', audit_signoff, 'commit_hash', commit_hash)::text
FROM ticket_board.tickets
WHERE id = 'PGU-4';
""",
                ).stdout
            )
            assert reopened == {"state": "in_progress", "audit_signoff": False, "commit_hash": ""}, reopened

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
                "assignee must not be unassigned",
            )
            insert_ticket(
                conninfo,
                "PGU-42",
                title="Director kickback missing implementation",
                assignee="director",
                state="director_review",
                implementation="done",
                audit_signoff=True,
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'in_progress', assignee = 'ops', implementation = '' WHERE id = 'PGU-42';",
                "implementation must be non-empty",
            )

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
                assignee="ops",
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
  LIKE '%pg_notify%ticket_board_state_transition%';
""",
            ).stdout.strip()
            assert notify_function == "t"

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            insert_ticket(conninfo, "PGU-20", title="Durable notify", state="analysis", assignee="ops", implementation="ready")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-20';")
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
            assert queued[0]["target_role"] == "ops", queued
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
            assert claimed["target_role"] == "ops", claimed
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
                    "assignee": "ops",
                    "target_role": "ops",
                    "kind": "transition",
                },
                {
                    "event": "claim",
                    "state": "in_progress",
                    "assignee": "ops",
                    "target_role": "ops",
                    "kind": "transition",
                },
                {
                    "event": "ack",
                    "state": "in_progress",
                    "assignee": "ops",
                    "target_role": "ops",
                    "kind": "transition",
                },
            ], durable_trace

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
            assert "WARNING:  primary notification path was late/undelivered for PGU-20 targeting ops (" in nudge_proc.stderr
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
SELECT jsonb_object_agg(target_role, count)::text
FROM (
    SELECT target_role, count(*) AS count
    FROM ticket_board.ticket_notification_queue
    WHERE ticket_id = 'PGU-20' AND kind = 'nudge'
    GROUP BY target_role
) q;
""",
                ).stdout
            )
            assert nudge_queue == {"director": 1, "ops": 1}, nudge_queue
            director_analysis = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'kind', payload->>'kind',
    'target_role', target_role,
    'original_target_role', payload->>'original_target_role',
    'message', message
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-20' AND kind = 'nudge' AND target_role = 'director'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert director_analysis["kind"] == "nudge_analysis", director_analysis
            assert director_analysis["target_role"] == "director", director_analysis
            assert director_analysis["original_target_role"] == "ops", director_analysis
            assert director_analysis["message"].startswith(
                "NUDGE-ANALYSIS: PGU-20 (target=ops, state=in_progress, "
            ), director_analysis
            assert "s since transition" in director_analysis["message"], director_analysis
            assert "prior_nudges=0" in director_analysis["message"], director_analysis
            deduped_nudges = psql(
                conninfo,
                "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '20 minutes', interval '5 minutes', 3);",
            ).stdout.strip()
            assert int(deduped_nudges) >= 1, deduped_nudges
            deduped_queue = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(target_role, count)::text
FROM (
    SELECT target_role, count(*) AS count
    FROM ticket_board.ticket_notification_queue
    WHERE ticket_id = 'PGU-20' AND kind = 'nudge'
    GROUP BY target_role
) q;
""",
                ).stdout
            )
            assert deduped_queue == {"director": 1, "ops": 1}, deduped_queue
            inspection_nudge = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object('target_role', target_role, 'message', message, 'state', payload->>'state')::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-21' AND kind = 'nudge' AND target_role = 'inspector'
ORDER BY id DESC
LIMIT 1;
""",
                ).stdout
            )
            assert inspection_nudge == {
                "target_role": "inspector",
                "message": "NUDGE PGU-21 -- Inspection notify ready for inspection",
                "state": "inspection",
            }, inspection_nudge
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
            assert cleared_implementation_nudge == {"queued": 2, "last_nudged": True, "nudge_count": 1}, cleared_implementation_nudge
            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
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
                "PGU-2823": {"queued": 2, "last_nudged": True, "nudge_count": 1},
                "PGU-2824": {"queued": 0, "last_nudged": False, "nudge_count": 0},
            }, delivery_suppression
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
            assert int(stale_nudges_second) >= 1, stale_nudges_second
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
    'tickets_enforce_workflow_update',
    'tickets_notify_state_transition',
    'tickets_notification_state_insert',
    'tickets_notification_state_update',
    'tickets_zz_auto_advance_analysis',
    'ticket_comments_notification_activity'
)
  AND NOT tgisinternal;
""",
            ).stdout.strip()
            assert trigger_count == "6"
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_postgres_triggers_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
