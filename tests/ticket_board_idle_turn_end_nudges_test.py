#!/usr/bin/env python3
"""Regression test: idle turn-end reminders enqueue on every idle-without-advance."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"


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


def psql(conninfo: str, sql: str) -> str:
    return run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo], input_text=sql).stdout.strip()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-idle-turn-end.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_idle_turn_end_test"
        conninfo = f"host={socket_dir} port={port} dbname={dbname}"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname])
            psql(conninfo, "CREATE ROLE ticket_board_listener LOGIN;")
            psql(conninfo, "CREATE ROLE ticket_board_service;")
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(
                conninfo,
                """
GRANT USAGE ON SCHEMA ticket_board TO ticket_board_listener;
GRANT SELECT ON ALL TABLES IN SCHEMA ticket_board TO ticket_board_listener;
GRANT EXECUTE ON FUNCTION ticket_board.notify_idle_turn_end_nudges(jsonb, timestamptz) TO ticket_board_listener;

INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, audit_signoff, created_text, updated_text, source_json
) VALUES
(
    'PGU-3541', 'Idle implementation reminder', '', 'in_progress', 'ops', 'Working.', false,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3541', 'title', 'Idle implementation reminder', 'body', '', 'state', 'in_progress', 'assignee', 'ops', 'implementation', 'Working.', 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
),
(
    'PGU-3542', 'Idle analysis reminder', '', 'analysis', 'app', '', false,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3542', 'title', 'Idle analysis reminder', 'body', '', 'state', 'analysis', 'assignee', 'app', 'implementation', '', 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
),
(
    'PGU-3543', 'Idle audit reminder', '', 'audit', 'perf', 'Audit.', false,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3543', 'title', 'Idle audit reminder', 'body', '', 'state', 'audit', 'assignee', 'perf', 'implementation', 'Audit.', 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
),
(
    'PGU-3545', 'Idle inspection reminder', '', 'inspection', 'main', 'Inspect.', false,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3545', 'title', 'Idle inspection reminder', 'body', '', 'state', 'inspection', 'assignee', 'main', 'implementation', 'Inspect.', 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
),
(
    'PGU-3546', 'No active owner reminder', '', 'backlog', 'research', 'Parked.', false,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3546', 'title', 'No active owner reminder', 'body', '', 'state', 'backlog', 'assignee', 'research', 'implementation', 'Parked.', 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
),
(
    'PGU-3661', 'Repeat escalation reminder', '', 'in_progress', 'app', 'Working.', false,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3661', 'title', 'Repeat escalation reminder', 'body', '', 'state', 'in_progress', 'assignee', 'app', 'implementation', 'Working.', 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
);

UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '10 minutes',
    last_activity_at = clock_timestamp() - interval '10 minutes'
WHERE ticket_id IN ('PGU-3541', 'PGU-3542', 'PGU-3543', 'PGU-3545', 'PGU-3546', 'PGU-3661');

DELETE FROM ticket_board.ticket_notification_queue;
""",
            )

            first_wave_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object(
        'ops', (now_at - interval '5 seconds')::text,
        'audit', (now_at - interval '5 seconds')::text,
        'inspector', (now_at - interval '5 seconds')::text,
        'research', (now_at - interval '5 seconds')::text
    ),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert first_wave_returned == "3", first_wave_returned
            first_wave_rows = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(ticket_id, row_json ORDER BY ticket_id)::text
FROM (
    SELECT
        t.id AS ticket_id,
        jsonb_build_object(
            'queued', (
                SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)
                FROM (
                    SELECT kind, target_role, count(*)::int AS count
                    FROM ticket_board.ticket_notification_queue q
                    WHERE q.ticket_id = t.id
                    GROUP BY kind, target_role
                ) q_counts
            ),
            'message', (
                SELECT message
                FROM ticket_board.ticket_notification_queue q
                WHERE q.ticket_id = t.id
                ORDER BY id DESC
                LIMIT 1
            )
        ) AS row_json
    FROM ticket_board.tickets t
    WHERE t.id IN ('PGU-3541', 'PGU-3543', 'PGU-3545', 'PGU-3546')
) s;
""",
                )
            )
            assert first_wave_rows == {
                "PGU-3541": {
                    "queued": {"idle_reminder:ops": 1},
                    "message": "PGU-3541 is still in in_progress and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell the director directly what is wrong. Do NOT do nothing.",
                },
                "PGU-3543": {
                    "queued": {"idle_reminder:audit": 1},
                    "message": "PGU-3543 is still in audit and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell the director directly what is wrong. Do NOT do nothing.",
                },
                "PGU-3545": {
                    "queued": {"idle_reminder:inspector": 1},
                    "message": "PGU-3545 is still in inspection and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell the director directly what is wrong. Do NOT do nothing.",
                },
                "PGU-3546": {
                    "queued": None,
                    "message": None,
                },
            }, first_wave_rows
            for ticket_id in ("PGU-3541", "PGU-3543", "PGU-3545"):
                assert "<" not in first_wave_rows[ticket_id]["message"], first_wave_rows
                assert "Eric" not in first_wave_rows[ticket_id]["message"], first_wave_rows

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            analysis_wave_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('director', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert analysis_wave_returned == "1", analysis_wave_returned
            analysis_wave_row = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'state', payload->>'state'
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-3542'
ORDER BY id DESC
LIMIT 1;
""",
                )
            )
            assert analysis_wave_row == {
                "target_role": "director",
                "message": "PGU-3542 is still in analysis and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell Eric directly what is wrong. Do NOT do nothing.",
                "state": "analysis",
            }, analysis_wave_row
            assert "<" not in analysis_wave_row["message"], analysis_wave_row
            assert "tell Eric directly what is wrong" in analysis_wave_row["message"], analysis_wave_row

            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.ticket_notification_queue (
    ticket_id, kind, target_role, message, payload, dedupe_key
) VALUES (
    'PGU-3542',
    'transition',
    'director',
    'PGU-3542 kicked back to you',
    jsonb_build_object('id', 'PGU-3542', 'state', 'analysis', 'target_role', 'director', 'kind', 'transition'),
    'pending-primary:PGU-3542:director'
);
""",
            )
            deferred_wave_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('director', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert deferred_wave_returned == "0", deferred_wave_returned
            deferred_wave_counts = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)::text
FROM (
    SELECT kind, target_role, count(*)::int AS count
    FROM ticket_board.ticket_notification_queue
    WHERE ticket_id = 'PGU-3542'
    GROUP BY kind, target_role
) counts;
""",
                )
            )
            assert deferred_wave_counts == {"transition:director": 1}, deferred_wave_counts

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            delivered_primary_wave_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('director', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert delivered_primary_wave_returned == "1", delivered_primary_wave_returned
            delivered_primary_wave_row = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'target_role', target_role,
    'kind', kind,
    'message', message,
    'state', payload->>'state'
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-3542'
ORDER BY id DESC
LIMIT 1;
""",
                )
            )
            assert delivered_primary_wave_row == {
                "target_role": "director",
                "kind": "idle_reminder",
                "message": "PGU-3542 is still in analysis and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell Eric directly what is wrong. Do NOT do nothing.",
                "state": "analysis",
            }, delivered_primary_wave_row

            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
UPDATE ticket_board.tickets
SET state = 'in_progress',
    assignee = 'main',
    implementation = 'Ready.'
WHERE id = 'PGU-3542';
DELETE FROM ticket_board.ticket_notification_queue;
""",
            )
            advanced_wave_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('main', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert advanced_wave_returned == "1", advanced_wave_returned
            advanced_wave_row = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'target_role', target_role,
    'kind', kind,
    'message', message,
    'state', payload->>'state'
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-3542'
ORDER BY id DESC
LIMIT 1;
""",
                ),
            )
            assert advanced_wave_row == {
                "target_role": "main",
                "kind": "idle_reminder",
                "message": "PGU-3542 is still in in_progress and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell the director directly what is wrong. Do NOT do nothing.",
                "state": "in_progress",
            }, advanced_wave_row
            assert "<" not in advanced_wave_row["message"], advanced_wave_row
            assert "Eric" not in advanced_wave_row["message"], advanced_wave_row

            psql(
                conninfo,
                """
DELETE FROM ticket_board.ticket_notification_queue;
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, audit_signoff, created_text, updated_text, source_json
) VALUES (
    'PGU-3547', 'Idle director review reminder', '', 'director_review', 'director', 'Review.', true,
    '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    jsonb_build_object('id', 'PGU-3547', 'title', 'Idle director review reminder', 'body', '', 'state', 'director_review', 'assignee', 'director', 'implementation', 'Review.', 'audit_signoff', true, 'comments', '[]'::jsonb, 'created', '2026-07-13T00:00:00+00:00', 'updated', '2026-07-13T00:00:00+00:00')
);
UPDATE ticket_board.ticket_notification_state
SET entered_current_state_at = clock_timestamp() - interval '10 minutes',
    last_activity_at = clock_timestamp() - interval '10 minutes'
WHERE ticket_id = 'PGU-3547';
DELETE FROM ticket_board.ticket_notification_queue;
""",
            )
            director_review_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('director', (now_at - interval '5 seconds')::text),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert director_review_returned == "1", director_review_returned
            director_review_row = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'target_role', target_role,
    'message', message,
    'state', payload->>'state'
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = 'PGU-3547'
ORDER BY id DESC
LIMIT 1;
""",
                )
            )
            assert director_review_row == {
                "target_role": "director",
                "message": "PGU-3547 is still in director_review and you haven't advanced it. Advance it now (do the work or hand it off). If you genuinely CANNOT move it forward, tell Eric directly what is wrong. Do NOT do nothing.",
                "state": "director_review",
            }, director_review_row
            assert "<" not in director_review_row["message"], director_review_row
            assert "tell Eric directly what is wrong" in director_review_row["message"], director_review_row

            second_director_review_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
WITH params AS (
    SELECT clock_timestamp() AS now_at
)
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('director', (now_at - interval '4 seconds')::text),
    now_at
)
FROM params;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert second_director_review_returned == "1", second_director_review_returned
            director_review_counts = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)::text
FROM (
    SELECT kind, target_role, count(*)::int AS count
    FROM ticket_board.ticket_notification_queue
    WHERE ticket_id = 'PGU-3547'
      AND kind IN ('idle_reminder', 'escalation')
    GROUP BY kind, target_role
) counts;
""",
                )
            )
            assert director_review_counts == {"idle_reminder:director": 2}, director_review_counts

            psql(conninfo, "DELETE FROM ticket_board.ticket_notification_queue;")
            repeated_wave_returned = psql(
                conninfo,
                """
SET ROLE ticket_board_listener;
SELECT (
    ticket_board.notify_idle_turn_end_nudges(
        jsonb_build_object('app', (clock_timestamp() - interval '5 seconds')::text),
        clock_timestamp()
    )
    +
    ticket_board.notify_idle_turn_end_nudges(
        jsonb_build_object('app', (clock_timestamp() - interval '4 seconds')::text),
        clock_timestamp()
    )
)::text;
RESET ROLE;
""",
            ).splitlines()[-2]
            assert repeated_wave_returned == "2", repeated_wave_returned
            repeated_wave_row = json.loads(
                psql(
                    conninfo,
                    """
SELECT jsonb_build_object(
    'queued', (
        SELECT jsonb_object_agg(kind || ':' || target_role, count ORDER BY kind || ':' || target_role)
        FROM (
            SELECT kind, target_role, count(*)::int AS count
            FROM ticket_board.ticket_notification_queue
            WHERE ticket_id = 'PGU-3661'
            GROUP BY kind, target_role
        ) counts
    ),
    'latest_target_role', (
        SELECT target_role
        FROM ticket_board.ticket_notification_queue
        WHERE ticket_id = 'PGU-3661'
        ORDER BY id DESC
        LIMIT 1
    ),
    'latest_kind', (
        SELECT kind
        FROM ticket_board.ticket_notification_queue
        WHERE ticket_id = 'PGU-3661'
        ORDER BY id DESC
        LIMIT 1
    ),
    'latest_message', (
        SELECT message
        FROM ticket_board.ticket_notification_queue
        WHERE ticket_id = 'PGU-3661'
        ORDER BY id DESC
        LIMIT 1
    )
)::text;
""",
                )
            )
            assert repeated_wave_row == {
                "queued": {
                    "escalation:director": 1,
                    "idle_reminder:app": 1,
                },
                "latest_target_role": "director",
                "latest_kind": "escalation",
                "latest_message": "app was reminded about PGU-3661 (in in_progress) and still hasn't advanced it -- may be stuck.",
            }, repeated_wave_row
        finally:
            run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], capture=False)

    print("ticket_board_idle_turn_end_nudges_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
