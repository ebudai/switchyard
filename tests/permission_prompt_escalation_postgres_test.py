#!/usr/bin/env python3
"""SYRD-234: the board half of telling the Director about a stopped pane.

The listener decides *that* a pane is waiting; this is the generator that turns
it into something the Director is handed. It runs against a real cluster
because a generator that is never executed is a generator nobody has read: a
typo in its body would first be seen by a tenant.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from temporary_cluster import temporary_cluster  # noqa: E402

SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC_PATH = ROOT / "scripts" / "ticket_board" / "rbac.sql"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def run(args: list[str]) -> None:
    subprocess.run(args, text=True, capture_output=True, check=True)


def psql(conninfo: str, sql: str) -> str:
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conninfo],
        input=sql, text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def ticket_source(ticket_id: str, title: str, state: str, assignee: str) -> str:
    payload = {
        "id": ticket_id, "title": title, "body": "", "state": state,
        "assignee": assignee, "comments": [],
        "created": "2026-09-22T00:00:00+00:00", "updated": "2026-09-22T00:00:00+00:00",
    }
    return json.dumps(payload, sort_keys=True).replace("'", "''")


def create_pane_roles(conninfo: str) -> None:
    psql(conninfo, "\n".join(
        f'CREATE ROLE {name} LOGIN;'
        for name in ("director", '"user"', "ops", "app", "audit", "inspector", "perf", "research", "main")
    ))


def waits(conninfo: str, blocked: dict[str, str], *, grace: str = "2 minutes") -> str:
    payload = json.dumps(blocked, sort_keys=True).replace("'", "''")
    return psql(conninfo, (
        "SELECT ticket_board.notify_permission_prompt_waits("
        f"'{payload}'::jsonb, clock_timestamp(), interval '{grace}');"
    ))


def main() -> int:
    with temporary_cluster(prefix="syrd234-permission-prompt.") as cluster:
        admin = cluster.conninfo("syrd234_permission_prompt")
        listener = cluster.conninfo("syrd234_permission_prompt", user="ticket_board_listener")
        run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port),
             "-U", "postgres", "syrd234_permission_prompt"])
        psql(admin, SCHEMA_PATH.read_text(encoding="utf-8"))
        create_pane_roles(admin)
        psql(admin, RBAC_PATH.read_text(encoding="utf-8"))
        psql(admin, f"""
INSERT INTO ticket_board.tickets (
    id, title, body, state, assignee, implementation, created_text, updated_text, source_json
) VALUES (
    'SYRD-900', 'Stopped pane', '', 'backlog', 'main', 'Ready.',
    '2026-09-22T00:00:00+00:00', '2026-09-22T00:00:00+00:00',
    '{ticket_source("SYRD-900", "Stopped pane", "backlog", "main")}'::jsonb
);
UPDATE ticket_board.tickets SET state = 'in_progress' WHERE id = 'SYRD-900';
DELETE FROM ticket_board.ticket_notification_queue;
""")

        # Waiting since well before the grace: the Director is owed this.
        stopped_since = psql(admin, "SELECT (clock_timestamp() - interval '10 minutes')::text;")
        enqueued = waits(listener, {"main": stopped_since})
        check(enqueued == "1", f"one escalation for the stopped pane: {enqueued}")

        row = psql(admin, """
SELECT kind || '|' || target_role || '|' || ticket_id || '|' || dedupe_key
FROM ticket_board.ticket_notification_queue
WHERE dedupe_key LIKE 'permission-prompt:%';
""")
        kind, target, ticket, dedupe = row.split("|")
        check(kind == "escalation", f"queued as an escalation, a kind the board already routes: {kind}")
        check(target == "director", f"and it goes to the Director: {target}")
        check(ticket == "SYRD-900", f"about the ticket the stopped role owns: {ticket}")
        check(dedupe.startswith("permission-prompt:main:"), f"keyed per role and episode: {dedupe}")

        message = psql(admin, """
SELECT message FROM ticket_board.ticket_notification_queue
WHERE dedupe_key LIKE 'permission-prompt:%';
""")
        check("main" in message, f"the message names the role: {message}")
        check(
            "permission prompt" in message and "cannot answer it itself" in message,
            f"and says what is wrong rather than that it is idle: {message}",
        )

        # The same episode, seen again on the next pass: told once.
        again = waits(listener, {"main": stopped_since})
        check(again == "0", f"the same wait is not reported twice: {again}")
        total = psql(admin, """
SELECT count(*) FROM ticket_board.ticket_notification_queue
WHERE dedupe_key LIKE 'permission-prompt:%';
""")
        check(total == "1", f"still one row: {total}")

        # `prune_notification_trace` runs on a cron and deletes the trace rows
        # this dedupes on. The queued row outlives them, and is the only thing
        # left that remembers the Director was already told -- so the queue
        # check is not redundant with the trace check, it is what survives a
        # prune.
        psql(admin, "DELETE FROM ticket_board.notification_trace;")
        after_prune = waits(listener, {"main": stopped_since})
        check(after_prune == "0", f"a pruned trace does not resurrect the escalation: {after_prune}")

        # A prompt answered and raised again later is a new episode.
        later = psql(admin, "SELECT (clock_timestamp() - interval '9 minutes')::text;")
        fresh = waits(listener, {"main": later})
        check(fresh == "1", f"a later episode is its own news: {fresh}")

        # Inside the grace: a prompt a passing human answers is not an escalation.
        psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        recent = psql(admin, "SELECT (clock_timestamp() - interval '5 seconds')::text;")
        quiet = waits(listener, {"main": recent})
        check(quiet == "0", f"nothing before the grace expires: {quiet}")

        # Garbage in the map is not a reason to escalate, or to fail the pass.
        rubbish = waits(listener, {"main": "not an instant"})
        check(rubbish == "0", f"an unparseable instant says nothing: {rubbish}")
        empty = waits(listener, {})
        check(empty == "0", f"an empty map says nothing: {empty}")

        # A role with no ticket of its own has nothing to escalate about.
        orphan = waits(listener, {"audit": stopped_since})
        check(orphan == "0", f"a role owning no ticket is not escalated: {orphan}")

    print(f"permission_prompt_escalation_postgres_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
