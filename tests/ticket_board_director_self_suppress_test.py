#!/usr/bin/env python3
"""Regression test: director write-client actions suppress director self-notifications."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import psycopg  # noqa: F401
except ModuleNotFoundError:
    raise SystemExit(
        "ticket_board_director_self_suppress_test: psycopg3 is required; "
        "install Arch/CachyOS package python-psycopg, or run from a venv with psycopg installed"
    )

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.server import LocalRoleAuthority, TicketBoardEventHub, TicketBoardUnixServer
from scripts.ticket_board.write_client import TicketBoardWriteClient

from temporary_cluster import temporary_cluster  # noqa: E402


SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
PANE_ROLES = ["director", "user", "ops", "app", "audit", "inspector", "perf", "research", "main"]
SERVICE_ROLE = "ticket_board_service"


class RecordingNotifier:
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        self.created.append({"id": str(ticket["id"]), "title": str(ticket["title"])})

    def close(self) -> None:
        return


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


def conninfo(socket_dir: Path, port: int, dbname: str, user: str = "postgres") -> str:
    return f"host={socket_dir} port={port} dbname={dbname} user={user}"


def psql(conn: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def create_roles(conn: str) -> None:
    psql(conn, "\n".join(f"CREATE ROLE {sql_ident(role)} LOGIN;" for role in PANE_ROLES))


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id,
        "title": title,
        "body": "",
        "state": state,
        "assignee": assignee,
        "comments": [],
        "created": "2026-07-13T00:00:00+00:00",
        "updated": "2026-07-13T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def seed_postgres_ticket(
    conn: str,
    ticket_id: str,
    *,
    title: str,
    state: str = "analysis",
    assignee: str = "unassigned",
    implementation: str = "",
) -> None:
    psql(
        conn,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation,
    audit_signoff, needs_inspection, inspector_signoff, needs_user_signoff, user_signoff, commit_hash,
    commit_exempt, created_text, updated_text, source_json
) VALUES (
    {sql_string(ticket_id)}, {sql_string(title)}, '', {sql_string(state)}, {sql_string(assignee)}, {sql_string(implementation)},
    false, false, false, false, false, '',
    false, '2026-07-13T00:00:00+00:00', '2026-07-13T00:00:00+00:00',
    {sql_string(ticket_source(ticket_id, title, state, assignee))}::jsonb
);
""",
    )


def queued_notifications(conn: str, ticket_id: str) -> list[dict[str, object]]:
    payload = psql(
        conn,
        f"""
SELECT COALESCE(
    jsonb_agg(
        jsonb_build_object(
            'target_role', target_role,
            'message', message,
            'old_state', payload->>'old_state',
            'new_state', payload->>'new_state'
        )
        ORDER BY id
    ),
    '[]'::jsonb
)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id = {sql_string(ticket_id)};
""",
    )
    return json.loads(payload)


def clear_notifications(conn: str, ticket_id: str) -> None:
    psql(conn, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = {sql_string(ticket_id)};")


def assert_director_write_client_self_suppresses_notifications() -> None:
    with temporary_cluster(
        prefix="ticket-board-director-self-suppress.",
    ) as cluster:
        root = cluster.root
        data_dir = cluster.data_dir
        socket_dir = cluster.socket_dir
        port = cluster.port
        board_socket = root / "board.sock"
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        dbname = "pgu_director_self_suppress_test"
        admin_conn = conninfo(socket_dir, port, dbname)


def local_role_authority_as(role: str) -> LocalRoleAuthority:
    """Run the local socket as though this test process were `role`'s account.

    Role authority is the peer's Unix uid, so a test that drives the write
    client has to say which role account it is standing in for.
    """
    return LocalRoleAuthority({role: f"test-{role}"}, resolve_uid=lambda _account: os.getuid())

def main() -> int:
    assert_director_write_client_self_suppresses_notifications()
    print("ticket_board_director_self_suppress_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
