#!/usr/bin/env python3
"""Non-PGU API creates use the durable queue without a direct pane send."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.server import (
    CALLER_ROLE_HEADER,
    REPORT_TOKEN_HEADER,
    WRITE_TOKEN_HEADER,
    TicketBoardServer,
)


class RecordingNotifier:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        self.created.append(ticket)

    def close(self) -> None:
        return


def run(args: list[str], *, input_text: str | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        check=True,
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def conninfo(socket_dir: Path, port: int, database: str, user: str = "postgres") -> str:
    return f"host={socket_dir} port={port} dbname={database} user={user}"


def psql(connection: str, sql: str) -> str:
    return run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", connection], input_text=sql).stdout.strip()


def post_json(url: str, payload: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 201, response.status
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise AssertionError(exc.read().decode("utf-8")) from exc


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-tenant-director-notify.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        frames = root / "frames"
        assets = root / "assets"
        socket_dir.mkdir()
        frames.mkdir()
        assets.mkdir()
        port = free_port()
        database = "orbit_director_notify_test"
        admin_connection = conninfo(socket_dir, port, database)
        run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            run(
                ["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"],
                capture=False,
            )
            run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", database])
            psql(admin_connection, (ROOT / "scripts/ticket_board/schema.sql").read_text(encoding="utf-8"))
            psql(
                admin_connection,
                "CREATE ROLE ticket_board_service LOGIN; CREATE ROLE ticket_board_listener LOGIN;",
            )
            psql(admin_connection, (ROOT / "scripts/ticket_board/rbac.sql").read_text(encoding="utf-8"))
            service_connection = conninfo(socket_dir, port, database, "ticket_board_service")
            app = TicketBoardApp(
                frames,
                assets,
                commit_git_dir=ROOT / ".git",
                database_url=service_connection,
                project="orbit",
                project_name="Orbit",
                ticket_prefix="ORB",
            )
            notifier = RecordingNotifier()
            report_token = "orbit-report-token"
            server = TicketBoardServer(
                ("127.0.0.1", 0),
                app,
                director_notifier=notifier,
                report_token=report_token,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_port}/api/tickets/actions"
            try:
                created = post_json(
                    f"{base_url}/create_ticket",
                    {"title": "Orbit web create", "body": "Created through the web API."},
                    {CALLER_ROLE_HEADER: "director", WRITE_TOKEN_HEADER: server.write_token},
                )["ticket"]
                reported = post_json(
                    f"{base_url}/file_report",
                    {
                        "title": "Orbit tenant report",
                        "body": "Created through the tenant report API.",
                        "origin_project": "peer",
                        "external_source_ref": "peer:PEER-99",
                    },
                    {REPORT_TOKEN_HEADER: report_token},
                )["ticket"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            created_id = str(created["id"])
            reported_id = str(reported["id"])
            assert created_id.startswith("ORB-"), created
            assert reported_id.startswith("ORB-"), reported
            queued = json.loads(
                psql(
                    admin_connection,
                    f"""
SELECT jsonb_agg(jsonb_build_object(
    'ticket_id', ticket_id,
    'target_role', target_role,
    'message', message
) ORDER BY id)::text
FROM ticket_board.ticket_notification_queue
WHERE ticket_id IN ('{created_id}', '{reported_id}');
""",
                )
            )
            assert queued == [
                {
                    "ticket_id": created_id,
                    "target_role": "director",
                    "message": f"New ticket for you: {created_id} -- Orbit web create",
                },
                {
                    "ticket_id": reported_id,
                    "target_role": "director",
                    "message": f"New ticket for you: {reported_id} -- Orbit tenant report",
                },
            ], queued
            assert notifier.created == [], notifier.created
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

    print("ticket_board_tenant_director_notification_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
