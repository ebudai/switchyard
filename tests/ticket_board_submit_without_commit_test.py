#!/usr/bin/env python3
"""A no-code ticket reaches audit without borrowing a hash (PGU-913).

submit_to_audit already accepted an empty hash when commit_exempt was set; an
implementer just could not set it. The only path on offer, request_commit_exempt,
throws the ticket back to analysis and unassigns it -- right for "I cannot do
this", wrong for "this is done and there is no diff". So three tickets reached
audit carrying someone else's commit.

The tests hold both halves of the tradeoff: the exemption must be reachable, and
it must not become a quiet way around the commit requirement. Every accepting
case asserts the CLAIM is recorded and attributed, not merely that the ticket
moved.
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
REAL_COMMIT = "0123456789abcdef0123456789abcdef01234567"


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
        raise AssertionError(f"SQL unexpectedly succeeded:\n{sql}")
    return proc


def as_role(conninfo: str, caller: str, sql: str, *, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    return psql(
        conninfo,
        f"SELECT set_config('ticket_board.caller_role', '{caller}', false);\n{sql}",
        expect_ok=expect_ok,
    )


def insert_ticket(conninfo: str, ticket_id: str, *, assignee: str, commit_exempt: bool = False) -> None:
    source = json.dumps(
        {
            "id": ticket_id,
            "title": "No-code fixture",
            "body": "",
            "state": "in_progress",
            "assignee": assignee,
            "comments": [],
            "created": "2026-09-04T00:00:00+00:00",
            "updated": "2026-09-04T00:00:00+00:00",
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
    '{ticket_id}', 'No-code fixture', '', 'in_progress', '{assignee}', '', '',
    false, true, false, false, false, false, '', {str(commit_exempt).lower()},
    false, false, '2026-09-04T00:00:00+00:00', '2026-09-04T00:00:00+00:00', '{source}'::jsonb
);
""",
    )


def ticket_row(conninfo: str, ticket_id: str) -> tuple[str, str, bool]:
    out = psql(
        conninfo,
        f"SELECT state || '|' || coalesce(commit_hash,'') || '|' || commit_exempt::text "
        f"FROM ticket_board.tickets WHERE id = '{ticket_id}';",
    ).stdout.strip()
    state, commit_hash, exempt = out.split("|")
    return state, commit_hash, exempt == "true"


def comments(conninfo: str, ticket_id: str) -> list[tuple[str, str]]:
    out = psql(
        conninfo,
        f"SELECT who || '|' || text FROM ticket_board.ticket_comments "
        f"WHERE ticket_id = '{ticket_id}' ORDER BY position;",
    ).stdout
    return [tuple(line.split("|", 1)) for line in out.splitlines() if line.strip()]


def missing_prerequisite() -> str:
    for tool in ("initdb", "pg_ctl", "createdb", "psql"):
        if not shutil.which(tool):
            return f"missing {tool}; install PostgreSQL client and server binaries"
    return ""


def run_checks(svc: str, admin: str) -> None:
    # ACCEPTANCE 1 -- a no-code ticket reaches audit with no hash at all.
    insert_ticket(admin, "PGU-1", assignee="research")
    as_role(
        svc,
        "research",
        "SELECT ticket_board.submit_to_audit_without_commit('PGU-1', 'Spike; the deliverable is the finding.');",
    )
    state, commit_hash, exempt = ticket_row(admin, "PGU-1")
    assert state == "audit", state
    assert commit_hash == "", f"a hash was invented: {commit_hash!r}"
    assert exempt is True

    # ACCEPTANCE 2 -- audit sees an explicit claim, attributed, not an absence.
    claim = [c for c in comments(admin, "PGU-1") if "no commit" in c[1]]
    assert len(claim) == 1, comments(admin, "PGU-1")
    who, text = claim[0]
    assert who == "research", claim
    assert text == "Submitted to audit with no commit: Spike; the deliverable is the finding.", claim

    # ACCEPTANCE 3 -- the ordinary path still refuses an empty hash. Demonstrated,
    # because the whole risk of this change is making that refusal skippable.
    insert_ticket(admin, "PGU-2", assignee="app")
    refused = as_role(svc, "app", "SELECT ticket_board.submit_to_audit('PGU-2', '');", expect_ok=False)
    assert "commit_hash must be a 7-40 character hex commit" in (refused.stderr + refused.stdout), refused.stderr
    assert ticket_row(admin, "PGU-2")[0] == "in_progress"

    # ...and the exemption cannot be claimed without saying why.
    no_reason = as_role(
        svc, "app", "SELECT ticket_board.submit_to_audit_without_commit('PGU-2', '   ');", expect_ok=False
    )
    assert "requires a non-empty reason" in (no_reason.stderr + no_reason.stdout), no_reason.stderr
    assert ticket_row(admin, "PGU-2")[0] == "in_progress"

    # A refused submit must not leave the exemption behind on a ticket that
    # never moved -- the whole call rolls back, including commit_exempt.
    assert ticket_row(admin, "PGU-2")[2] is False

    # A real commit still submits normally, and stays unexempt.
    as_role(svc, "app", f"SELECT ticket_board.submit_to_audit('PGU-2', '{REAL_COMMIT}');")
    state, commit_hash, exempt = ticket_row(admin, "PGU-2")
    assert (state, commit_hash, exempt) == ("audit", REAL_COMMIT, False), (state, commit_hash, exempt)

    # A role that does not own the ticket cannot claim the exemption on it.
    insert_ticket(admin, "PGU-3", assignee="ops")
    wrong_role = as_role(
        svc, "perf", "SELECT ticket_board.submit_to_audit_without_commit('PGU-3', 'not mine');", expect_ok=False
    )
    assert "cannot call" in (wrong_role.stderr + wrong_role.stdout), wrong_role.stderr
    assert ticket_row(admin, "PGU-3")[0] == "in_progress"

    # ACCEPTANCE 4 -- a ticket already exempt is unaffected: it still submits
    # through the ordinary path with an empty hash, as it did before.
    # A fresh role: ops already holds PGU-3 in_progress and the serial-focus
    # rule would demote a second one to backlog.
    insert_ticket(admin, "PGU-4", assignee="main", commit_exempt=True)
    as_role(svc, "main", "SELECT ticket_board.submit_to_audit('PGU-4', '');")
    state, commit_hash, exempt = ticket_row(admin, "PGU-4")
    assert (state, commit_hash, exempt) == ("audit", "", True), (state, commit_hash, exempt)
    assert not [c for c in comments(admin, "PGU-4") if "no commit" in c[1]]


def main() -> int:
    reason = missing_prerequisite()
    if reason:
        print(f"ticket_board_submit_without_commit_test: skipped - {reason}")
        return 0

    with tempfile.TemporaryDirectory(prefix="ticket-board-no-commit.") as tmp:
        root = Path(tmp)
        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        socket_dir.mkdir()
        port = free_port()
        dbname = "pgu_no_commit_test"
        admin = f"host={socket_dir} port={port} dbname={dbname}"
        svc = f"{admin} user=ticket_board_service"

        subprocess.run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale"], check=True, capture_output=True)
        try:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(["createdb", "-h", str(socket_dir), "-p", str(port), dbname], check=True, capture_output=True)
            psql(admin, "CREATE ROLE ticket_board_listener LOGIN;")
            psql(admin, "CREATE ROLE ticket_board_service LOGIN;")
            psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
            psql(
                admin,
                """
GRANT USAGE ON SCHEMA ticket_board TO ticket_board_service;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ticket_board TO ticket_board_service;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ticket_board TO ticket_board_service;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ticket_board TO ticket_board_service;
""",
            )
            run_checks(svc, admin)
        finally:
            subprocess.run(["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"], check=False)

    print("ticket_board_submit_without_commit_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
