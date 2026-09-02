#!/usr/bin/env python3
"""Regression test: director write-client actions suppress director self-notifications."""

from __future__ import annotations

import json
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
from scripts.ticket_board.server import CallerRegistry, TicketBoardEventHub, TicketBoardUnixServer
from scripts.ticket_board.write_client import TicketBoardWriteClient


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
    with tempfile.TemporaryDirectory(prefix="ticket-board-director-self-suppress.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "postgres"
        board_socket = root / "board.sock"
        frames = root / "frames"
        assets = root / "assets"
        socket_dir.mkdir()
        frames.mkdir()
        assets.mkdir()
        port = free_port()
        dbname = "pgu_director_self_suppress_test"
        admin_conn = conninfo(socket_dir, port, dbname)
        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            psql(admin_conn, SCHEMA_PATH.read_text(encoding="utf-8"))
            create_roles(admin_conn)
            psql(admin_conn, RBAC_PATH.read_text(encoding="utf-8"))
            commit_hash = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).stdout.strip()

            app = TicketBoardApp(
                frames,
                assets,
                commit_git_dir=ROOT,
                database_url=conninfo(socket_dir, port, dbname, SERVICE_ROLE),
            )
            events = TicketBoardEventHub(app)
            notifier = RecordingNotifier()
            server = TicketBoardUnixServer(
                board_socket,
                app,
                events=events,
                director_notifier=notifier,
                caller_registry=CallerRegistry(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = TicketBoardWriteClient(socket_path=str(board_socket), caller_role="director")

                created = client.create_ticket(
                    title="Director self-create analysis",
                    body="Created through the Unix socket write client.",
                )["ticket"]
                created_id = str(created["id"])
                assert created["state"] == "analysis", created
                assert notifier.created == [], notifier.created
                assert queued_notifications(admin_conn, created_id) == [], queued_notifications(admin_conn, created_id)

                seed_postgres_ticket(
                    admin_conn,
                    "PGU-35001",
                    title="Director self-route to analysis",
                    state="in_progress",
                    assignee="ops",
                    implementation="Ready.",
                )
                clear_notifications(admin_conn, "PGU-35001")
                routed_to_director = client.route("PGU-35001", state="analysis", assignee="unassigned")["ticket"]
                assert routed_to_director["state"] == "analysis", routed_to_director
                assert routed_to_director["assignee"] == "director", routed_to_director
                assert queued_notifications(admin_conn, "PGU-35001") == [], queued_notifications(admin_conn, "PGU-35001")

                seed_postgres_ticket(
                    admin_conn,
                    "PGU-35002",
                    title="Director route still notifies app",
                    state="analysis",
                    assignee="unassigned",
                )
                clear_notifications(admin_conn, "PGU-35002")
                routed_to_app = client.route("PGU-35002", state="in_progress", assignee="app")["ticket"]
                assert routed_to_app["state"] == "in_progress", routed_to_app
                assert routed_to_app["assignee"] == "app", routed_to_app
                assert queued_notifications(admin_conn, "PGU-35002") == [
                    {
                        "target_role": "app",
                        "message": "New ticket for you: PGU-35002 -- Director route still notifies app",
                        "old_state": "analysis",
                        "new_state": "in_progress",
                    }
                ], queued_notifications(admin_conn, "PGU-35002")

                seed_postgres_ticket(
                    admin_conn,
                    "PGU-40501",
                    title="Director cancel should notify active ops",
                    state="in_progress",
                    assignee="ops",
                    implementation="Ready.",
                )
                clear_notifications(admin_conn, "PGU-40501")
                cancelled_ops = client.cancel("PGU-40501", reason="Split into follow-up tickets.")["ticket"]
                assert cancelled_ops["state"] == "cancelled", cancelled_ops
                assert queued_notifications(admin_conn, "PGU-40501") == [
                    {
                        "target_role": "ops",
                        "message": "PGU-40501 -- Director cancel should notify active ops cancelled",
                        "old_state": "in_progress",
                        "new_state": "cancelled",
                    }
                ], queued_notifications(admin_conn, "PGU-40501")

                seed_postgres_ticket(
                    admin_conn,
                    "PGU-40502",
                    title="Director cancel analysis self-suppresses",
                    state="analysis",
                    assignee="unassigned",
                )
                clear_notifications(admin_conn, "PGU-40502")
                cancelled_director = client.cancel("PGU-40502", reason="No longer needed.")["ticket"]
                assert cancelled_director["state"] == "cancelled", cancelled_director
                assert queued_notifications(admin_conn, "PGU-40502") == [], queued_notifications(admin_conn, "PGU-40502")

                seed_postgres_ticket(
                    admin_conn,
                    "PGU-40503",
                    title="Director done self-suppresses",
                    state="director_review",
                    assignee="director",
                    implementation="Ready.",
                )
                clear_notifications(admin_conn, "PGU-40503")
                done_director = client.mark_done("PGU-40503", commit_hash=commit_hash)["ticket"]
                assert done_director["state"] == "done", done_director
                assert queued_notifications(admin_conn, "PGU-40503") == [], queued_notifications(admin_conn, "PGU-40503")
            finally:
                server.shutdown()
                server.server_close()
                events.close()
                thread.join(timeout=2)
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def main() -> int:
    assert_director_write_client_self_suppresses_notifications()
    print("ticket_board_director_self_suppress_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
