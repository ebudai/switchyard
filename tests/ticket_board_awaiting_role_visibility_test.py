#!/usr/bin/env python3
"""Regression test: awaiting_role reaches the director queue (PGU-906).

This suite deliberately runs against a real Postgres and the real
``TicketBoardApp`` serializer. The bug it pins was a read path that returned
``""`` forever: ``needs_director()`` consulted ``ticket["awaiting_role"]`` while
``_pg_row_to_ticket`` never put that key on the payload. Any test that hands
``needs_director()`` a hand-built dict passes in both the broken and the fixed
world, so it must be the serializer that produces the ticket here.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"

from scripts.ticket_board.app import TicketBoardApp
from scripts.ticket_board.read_client import director_payload, needs_director


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def psql(conninfo: str, sql: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc


def insert_ticket(conninfo: str, ticket_id: str, *, title: str, state: str, assignee: str) -> None:
    source = json.dumps(
        {
            "id": ticket_id,
            "title": title,
            "body": "",
            "state": state,
            "assignee": assignee,
            "comments": [],
            "created": "2026-09-03T00:00:00+00:00",
            "updated": "2026-09-03T00:00:00+00:00",
        },
        sort_keys=True,
    ).replace("'", "''")
    psql(
        conninfo,
        f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, parent_id, implementation,
    audit_signoff, needs_audit, needs_inspection, inspector_signoff,
    needs_user_signoff, user_signoff, commit_hash, commit_exempt,
    manually_controlled, parked, created_text, updated_text, source_json
) VALUES (
    '{ticket_id}', '{title}', '', '{state}', '{assignee}', '', '',
    false, true, false, false,
    false, false, '', false,
    false, false, '2026-09-03T00:00:00+00:00',
    '2026-09-03T00:00:00+00:00', '{source}'::jsonb
);
""",
    )


def missing_prerequisite() -> str:
    for tool in ("initdb", "pg_ctl", "createdb", "psql"):
        if not shutil.which(tool):
            return f"missing {tool}; install PostgreSQL client and server binaries"
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return "missing psycopg; install psycopg>=3.3 to run board serializer suites"
    return ""


def run_checks(conninfo: str, admin_conninfo: str, repo_root: Path, frames: Path, assets: Path) -> None:
    app = TicketBoardApp(
        frame_dir=frames,
        asset_dir=assets,
        repo_root=repo_root,
        commit_git_dir=str(repo_root / ".git"),
        database_url=conninfo,
    )

    insert_ticket(admin_conninfo, "PGU-1", title="Awaiting the director", state="in_progress", assignee="research")
    insert_ticket(admin_conninfo, "PGU-2", title="Ordinary implementer work", state="in_progress", assignee="research")

    # Baseline: neither ticket is the director's problem yet.
    for ticket_id in ("PGU-1", "PGU-2"):
        ticket = app.get_ticket(ticket_id)
        assert "awaiting_role" in ticket, f"{ticket_id} payload is missing awaiting_role entirely"
        assert ticket["awaiting_role"] == "", ticket["awaiting_role"]
        assert needs_director(ticket) is False

    app.set_awaiting_role("PGU-1", "director", caller_role="research")

    # The flag is in the database...
    stored = psql(
        admin_conninfo,
        "SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-1';",
    ).stdout.strip()
    assert stored == "director", stored

    # ...and, the point of this suite, it survives the real serializer.
    awaiting = app.get_ticket("PGU-1")
    assert awaiting["awaiting_role"] == "director", awaiting.get("awaiting_role")
    assert needs_director(awaiting) is True

    # The untouched ticket must not be swept in with it.
    other = app.get_ticket("PGU-2")
    assert other["awaiting_role"] == ""
    assert needs_director(other) is False

    # End to end through the exact path `ticket-board-read director` uses.
    tickets, _columns = app.list_tickets()
    queue_ids = [str(ticket["id"]) for ticket in director_payload({"tickets": tickets})]
    assert "PGU-1" in queue_ids, queue_ids
    assert "PGU-2" not in queue_ids, queue_ids

    # Clearing it takes the ticket back out of the queue.
    app.clear_awaiting_role("PGU-1", caller_role="research")
    cleared = app.get_ticket("PGU-1")
    assert cleared["awaiting_role"] == ""
    assert needs_director(cleared) is False
    tickets, _columns = app.list_tickets()
    assert "PGU-1" not in [str(ticket["id"]) for ticket in director_payload({"tickets": tickets})]


def main() -> int:
    reason = missing_prerequisite()
    if reason:
        print(f"ticket_board_awaiting_role_visibility_test: skipped - {reason}")
        return 0

    with tempfile.TemporaryDirectory(prefix="ticket-board-awaiting-role.") as tmpdir:
        root = Path(tmpdir)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        frames = root / "frames"
        assets = root / "assets"
        frames.mkdir()
        assets.mkdir()
        port = free_port()
        dbname = "pgu_awaiting_role_test"
        # The board writes as ticket_board_service and the schema enforces it
        # (require_actor rejects any other database role), so the app under test
        # has to connect the same way production does.
        admin_conninfo = f"host={socket_dir} port={port} dbname={dbname}"
        conninfo = f"{admin_conninfo} user=ticket_board_service"

        subprocess.run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"], check=True, capture_output=True, text=True)
        try:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname], check=True, capture_output=True, text=True)
            psql(admin_conninfo, "CREATE ROLE ticket_board_listener LOGIN;")
            psql(admin_conninfo, "CREATE ROLE ticket_board_service LOGIN;")
            psql(admin_conninfo, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(
                admin_conninfo,
                """
GRANT USAGE ON SCHEMA ticket_board TO ticket_board_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ticket_board TO ticket_board_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ticket_board TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.set_awaiting_role(text, text) TO ticket_board_service;
GRANT EXECUTE ON FUNCTION ticket_board.clear_awaiting_role(text) TO ticket_board_service;
""",
            )
            run_checks(conninfo, admin_conninfo, ROOT, frames, assets)
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_awaiting_role_visibility_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
