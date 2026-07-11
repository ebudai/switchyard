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
    needs_eric_signoff: bool = False,
    eric_signoff: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
    manually_controlled: bool = False,
) -> None:
    bools = {
        "audit_signoff": "true" if audit_signoff else "false",
        "needs_eric_signoff": "true" if needs_eric_signoff else "false",
        "eric_signoff": "true" if eric_signoff else "false",
        "commit_exempt": "true" if commit_exempt else "false",
        "manually_controlled": "true" if manually_controlled else "false",
    }
    source = ticket_source(ticket_id, title, state, assignee)
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_eric_signoff, eric_signoff, commit_hash,
    commit_exempt, manually_controlled, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '{parent_id}', '{implementation}',
    {bools['audit_signoff']}, {bools['needs_eric_signoff']}, {bools['eric_signoff']}, '{commit_hash}',
    {bools['commit_exempt']}, {bools['manually_controlled']}, '2026-07-10T00:00:00+00:00',
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
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(conninfo, "GRANT USAGE ON SCHEMA ticket_board TO ticket_board_listener;")

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

            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-1';",
                "illegal state transition: analysis -> in_progress",
            )
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'ready' WHERE id = 'PGU-1';",
                "implementation must be non-empty",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET implementation = 'ship it', state = 'ready' WHERE id = 'PGU-1';")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-1';")

            insert_ticket(conninfo, "PGU-2", title="Conflict", assignee="app", state="ready", implementation="queued")
            assert_error(
                conninfo,
                "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-2';",
                "app already has an in-progress ticket PGU-1",
            )

            insert_ticket(
                conninfo,
                "PGU-3",
                title="Linked child",
                assignee="app",
                state="ready",
                implementation="same cluster",
                parent_id="PGU-1",
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'PGU-3';")

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
            assert reopened == {"state": "analysis", "audit_signoff": False, "commit_hash": ""}, reopened

            insert_ticket(
                conninfo,
                "PGU-40",
                title="Director kickback to ready",
                assignee="director",
                state="director_review",
                implementation="done",
                audit_signoff=True,
            )
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'ready', assignee = 'ops' WHERE id = 'PGU-40';")
            director_ready = json.loads(
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
            assert director_ready == {
                "state": "ready",
                "assignee": "ops",
                "audit_signoff": False,
                "commit_hash": "",
            }, director_ready

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
                "UPDATE ticket_board.tickets SET state = 'ready', assignee = 'unassigned' WHERE id = 'PGU-41';",
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
                "UPDATE ticket_board.tickets SET state = 'ready', assignee = 'ops', implementation = '' WHERE id = 'PGU-42';",
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

            insert_ticket(conninfo, "PGU-20", title="Durable notify", state="analysis", assignee="ops", implementation="ready")
            psql(conninfo, "UPDATE ticket_board.tickets SET state = 'ready' WHERE id = 'PGU-20';")
            pending = json.loads(
                psql(
                    listener_conninfo,
                    """
SELECT jsonb_agg(payload ORDER BY ticket_id)::text
FROM ticket_board.pending_transition_notifications()
WHERE ticket_id = 'PGU-20';
""",
                ).stdout
            )
            assert pending[0]["id"] == "PGU-20", pending
            assert pending[0]["old_state"] == "analysis", pending
            assert pending[0]["new_state"] == "ready", pending
            assert pending[0]["assignee"] == "ops", pending
            psql(listener_conninfo, "SELECT ticket_board.mark_transition_notified('PGU-20');")
            pending_after_mark = psql(
                listener_conninfo,
                "SELECT count(*) FROM ticket_board.pending_transition_notifications() WHERE ticket_id = 'PGU-20';",
            ).stdout.strip()
            assert pending_after_mark == "0"

            nudges = psql(
                conninfo,
                "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '10 minutes', interval '5 minutes', 3);",
            ).stdout.strip()
            assert int(nudges) >= 1, nudges
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
    'ticket_comments_notification_activity'
)
  AND NOT tgisinternal;
""",
            ).stdout.strip()
            assert trigger_count == "5"
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_postgres_triggers_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
