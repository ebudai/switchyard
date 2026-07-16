#!/usr/bin/env python3
"""Regression test: active ticket owners are notified about in-place edits by someone else."""

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


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def ticket_source(ticket_id: str, title: str, body: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": body,
        "state": state,
        "assignee": assignee,
        "comments": [],
        "created": "2026-07-13T00:00:00+00:00",
        "updated": "2026-07-13T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def insert_ticket(
    conninfo: str,
    ticket_id: str,
    *,
    title: str,
    body: str = "",
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
    commit_hash: str = "",
    commit_exempt: bool = False,
) -> None:
    source = ticket_source(ticket_id, title, body, state, assignee)
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, commit_hash, commit_exempt,
    audit_signoff, needs_inspection, inspector_signoff, needs_eric_signoff, eric_signoff,
    created_text, updated_text, source_json
) VALUES (
    {sql_string(ticket_id)},
    {sql_string(title)},
    {sql_string(body)},
    {sql_string(state)},
    {sql_string(assignee)},
    {sql_string(implementation)},
    {sql_string(commit_hash)},
    {'true' if commit_exempt else 'false'},
    false, false, false, false, false,
    '2026-07-13T00:00:00+00:00',
    '2026-07-13T00:00:00+00:00',
    {sql_string(source)}::jsonb
);
""",
    )


def service_call(conninfo: str, caller_role: str, sql: str) -> None:
    psql(
        conninfo,
        f"""
SET ROLE ticket_board_service;
SELECT set_config('ticket_board.caller_role', {sql_string(caller_role)}, false);
{sql}
""",
    )


def clear_notifications(conninfo: str, ticket_id: str) -> None:
    psql(conninfo, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = {sql_string(ticket_id)};")


def queued_notifications(conninfo: str, ticket_id: str) -> list[dict[str, object]]:
    payload = psql(
        conninfo,
        f"""
SELECT COALESCE(
    jsonb_agg(
        jsonb_build_object(
            'kind', kind,
            'target_role', target_role,
            'message', message,
            'state', payload->>'state',
            'assignee', payload->>'assignee',
            'old_state', payload->>'old_state',
            'new_state', payload->>'new_state',
            'change_summary', payload->>'change_summary'
        )
        ORDER BY id
    ),
    '[]'::jsonb
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = {sql_string(ticket_id)};
""",
    ).stdout.strip()
    return json.loads(payload)


def assert_single_ticket_update(
    conninfo: str,
    ticket_id: str,
    *,
    target_role: str,
    state: str,
    assignee: str,
    actor: str,
    summary: str,
    title: str,
) -> None:
    queued = queued_notifications(conninfo, ticket_id)
    assert queued == [
        {
            "kind": "ticket_update",
            "target_role": target_role,
            "message": f"{ticket_id} -- {title} (in {state}, that you own) was changed by {actor}: {summary} -- re-read it before continuing.",
            "state": state,
            "assignee": assignee,
            "old_state": None,
            "new_state": None,
            "change_summary": summary,
        }
    ], queued


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-owner-update-notify.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_owner_update_notify_test"
        conninfo = f"host={socket_dir} port={port} dbname={dbname}"

        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname])
            psql(conninfo, "CREATE ROLE ticket_board_service;")
            psql(conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(conninfo, "GRANT USAGE ON SCHEMA ticket_board TO ticket_board_service;")
            psql(
                conninfo,
                """
GRANT EXECUTE ON FUNCTION ticket_board.route(text, text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.edit_fields(text, jsonb) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_blockers(text, text[], text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.add_comment(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.add_comment(text, text, boolean) TO ticket_board_service;
""",
            )

            insert_ticket(
                conninfo,
                "PGU-35501",
                title="Implementation edit",
                body="Original body.",
                state="in_progress",
                assignee="ops",
                implementation="before",
            )
            clear_notifications(conninfo, "PGU-35501")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.edit_fields('PGU-35501', '{\"implementation\":\"after\"}'::jsonb);",
            )
            assert_single_ticket_update(
                conninfo,
                "PGU-35501",
                target_role="ops",
                state="in_progress",
                assignee="ops",
                actor="director",
                summary="implementation",
                title="Implementation edit",
            )

            clear_notifications(conninfo, "PGU-35501")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.edit_fields('PGU-35501', '{\"body\":\"Body updated by director.\"}'::jsonb);",
            )
            assert_single_ticket_update(
                conninfo,
                "PGU-35501",
                target_role="ops",
                state="in_progress",
                assignee="ops",
                actor="director",
                summary="body",
                title="Implementation edit",
            )

            insert_ticket(
                conninfo,
                "PGU-35502",
                title="Comment edit",
                state="in_progress",
                assignee="app",
            )
            clear_notifications(conninfo, "PGU-35502")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.add_comment('PGU-35502', 'Please re-read this ticket.');",
            )
            assert queued_notifications(conninfo, "PGU-35502") == [], queued_notifications(conninfo, "PGU-35502")
            service_call(
                conninfo,
                "audit",
                "SELECT ticket_board.add_comment('PGU-35502', 'Audit note should not interrupt mid-work.');",
            )
            assert queued_notifications(conninfo, "PGU-35502") == [], queued_notifications(conninfo, "PGU-35502")
            service_call(
                conninfo,
                "audit",
                "SELECT ticket_board.add_comment('PGU-35502', 'Urgent audit note should interrupt mid-work.', true);",
            )
            assert_single_ticket_update(
                conninfo,
                "PGU-35502",
                target_role="app",
                state="in_progress",
                assignee="app",
                actor="audit",
                summary="new comment",
                title="Comment edit",
            )
            clear_notifications(conninfo, "PGU-35502")
            service_call(
                conninfo,
                "inspector",
                "SELECT ticket_board.add_comment('PGU-35502', 'Inspector note should not interrupt mid-work.');",
            )
            assert queued_notifications(conninfo, "PGU-35502") == [], queued_notifications(conninfo, "PGU-35502")

            insert_ticket(
                conninfo,
                "PGU-35503",
                title="Blocked implementation",
                state="in_progress",
                assignee="perf",
            )
            insert_ticket(
                conninfo,
                "PGU-35504",
                title="Blocking ticket",
                state="analysis",
                assignee="unassigned",
            )
            clear_notifications(conninfo, "PGU-35503")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.set_blockers('PGU-35503', ARRAY['PGU-35504'], 'Waiting on blocker.');",
            )
            assert_single_ticket_update(
                conninfo,
                "PGU-35503",
                target_role="perf",
                state="in_progress",
                assignee="perf",
                actor="director",
                summary="blockers",
                title="Blocked implementation",
            )

            insert_ticket(
                conninfo,
                "PGU-35505",
                title="Self-suppress",
                state="in_progress",
                assignee="ops",
                implementation="before",
            )
            clear_notifications(conninfo, "PGU-35505")
            service_call(
                conninfo,
                "ops",
                "SELECT ticket_board.edit_fields('PGU-35505', '{\"implementation\":\"after\"}'::jsonb);",
            )
            assert queued_notifications(conninfo, "PGU-35505") == [], queued_notifications(conninfo, "PGU-35505")
            service_call(
                conninfo,
                "ops",
                "SELECT ticket_board.add_comment('PGU-35505', 'I changed this myself.');",
            )
            assert queued_notifications(conninfo, "PGU-35505") == [], queued_notifications(conninfo, "PGU-35505")

            insert_ticket(conninfo, "PGU-35506", title="Backlog edit", state="backlog", assignee="unassigned")
            insert_ticket(conninfo, "PGU-35507", title="Done edit", state="done", assignee="ops", commit_hash="abcdef1")
            insert_ticket(conninfo, "PGU-35508", title="Cancelled edit", state="cancelled", assignee="ops", commit_exempt=True)
            for ticket_id in ("PGU-35506", "PGU-35507", "PGU-35508"):
                clear_notifications(conninfo, ticket_id)
                service_call(
                    conninfo,
                    "director",
                    f"SELECT ticket_board.edit_fields({sql_string(ticket_id)}, '{{\"implementation\":\"noop change\"}}'::jsonb);",
                )
                assert queued_notifications(conninfo, ticket_id) == [], queued_notifications(conninfo, ticket_id)

            psql(
                conninfo,
                """
SELECT set_config('ticket_board.force_move', 'on', false);
UPDATE ticket_board.tickets
SET state = 'done',
    commit_hash = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
WHERE id IN ('PGU-35501', 'PGU-35502', 'PGU-35503', 'PGU-35505');
""",
            )

            insert_ticket(conninfo, "PGU-35509", title="Transition only", state="analysis", assignee="unassigned")
            clear_notifications(conninfo, "PGU-35509")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.route('PGU-35509', 'in_progress', 'ops');",
            )
            transition_only = queued_notifications(conninfo, "PGU-35509")
            assert transition_only == [
                {
                    "kind": "transition",
                    "target_role": "ops",
                    "message": "New ticket for you: PGU-35509 -- Transition only",
                    "state": None,
                    "assignee": "ops",
                    "old_state": "analysis",
                    "new_state": "in_progress",
                    "change_summary": None,
                }
            ], transition_only

            psql(
                conninfo,
                """
SELECT set_config('ticket_board.force_move', 'on', false);
UPDATE ticket_board.tickets
SET state = 'done',
    commit_hash = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
WHERE id = 'PGU-35509';
""",
            )

            insert_ticket(conninfo, "PGU-35510", title="Reassign active work", state="in_progress", assignee="ops")
            clear_notifications(conninfo, "PGU-35510")
            service_call(
                conninfo,
                "director",
                "SELECT ticket_board.route('PGU-35510', 'in_progress', 'app');",
            )
            assert_single_ticket_update(
                conninfo,
                "PGU-35510",
                target_role="app",
                state="in_progress",
                assignee="app",
                actor="director",
                summary="assignee",
                title="Reassign active work",
            )
        finally:
            run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], capture=False)

    print("ticket_board_owner_update_notifications_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
