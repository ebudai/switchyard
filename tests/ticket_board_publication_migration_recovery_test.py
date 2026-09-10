#!/usr/bin/env python3
"""SYRD-100: an upgraded board can record a publication request again.

`pgu930_syrd93_publication_requests.sql` creates `publication_control_role()`,
which calls `ticket_board.control_capabilities()`. That function is defined in
schema.sql and, until `pgu932`, by no migration. A board built fresh from
schema.sql has it; a board built by applying migrations does not. Every test of
the publication path built the first kind. The live board is the second, and the
first publication request any role ever made was refused with

    function ticket_board.control_capabilities() does not exist

from inside `request_publication` -- so the implementer half of publication, the
whole point of which is that it needs no credential, could not be used at all.

Driven against a real cluster in the state the live board is in: every migration
applied in order through pgu931, and the function absent. The refusal is
reproduced first, because a repair that is only ever run against a healthy
database proves nothing about the broken one.
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

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from scripts.ticket_board.workflow_config import validate
from temporary_cluster import temporary_cluster

MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"
REPAIR = "pgu932_syrd100_control_capabilities.sql"
COMMIT = "c" * 40
BUNDLE = "/home/switchyard-agent/.local/state/switchyard/publish-outbox/syrd/SYRD-100.bundle"


def migration_files() -> list[Path]:
    """In the order the runner applies them: a plain filename sort."""
    return sorted(MIGRATIONS.glob("*.sql"))


def migrations_from(first: str, *, through: str) -> list[Path]:
    """The tail of the series, in the order the runner applies it.

    The whole series cannot be replayed over the current schema.sql: a migration
    from July is written against the schema of July, and one of them calls a
    `transition_message` signature that no longer exists. Replaying the tail over
    the current schema is what can be done here, and it is the part this ticket
    is about -- the publication migration, the one after it, and the repair.
    """
    return [path for path in migration_files() if first <= path.name <= through]


def function_exists(conn: str) -> bool:
    return t.psql(
        conn,
        "SELECT to_regprocedure('ticket_board.control_capabilities()') IS NOT NULL;",
    ) == "t"


def request_publication(conn: str, *, role: str, ticket: str, ref: str) -> tuple[bool, str]:
    """The real operation, as the role, exactly as the board client calls it."""
    proc = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
        input=(
            f"SELECT set_config('ticket_board.caller_role', '{role}', false);\n"
            f"SELECT (ticket_board.request_publication('{ticket}', '{ref}', "
            f"'{COMMIT}', '{BUNDLE}')).id;\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def recorded_requests(admin: str, ticket: str) -> list[dict]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) "
        f"FROM ticket_board.publication_requests r WHERE ticket_id = '{ticket}';",
    )
    return json.loads(raw)


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="publication-migration-", shutdown="immediate") as cluster:
        sock, port, db = cluster.socket_dir, cluster.port, "syrd_publication_migration"
        admin = t.conninfo(sock, port, db)
        service = t.conninfo(sock, port, db, t.SERVICE_ROLE)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text(encoding="utf-8"))

        # The tail of the series in order, through the migration before the
        # repair. Applying it over a schema that already has these functions is
        # also the reapplication check: every one is re-runnable.
        tail = migrations_from(
            "pgu930_syrd93_publication_requests.sql",
            through="pgu931_syrd99_superseded_reminder_grant.sql",
        )
        assert [path.name for path in tail] == [
            "pgu930_syrd93_publication_requests.sql",
            "pgu931_syrd99_superseded_reminder_grant.sql",
        ], [path.name for path in tail]
        for path in tail:
            t.psql(admin, path.read_text(encoding="utf-8"))
        # And the repair sorts after all of them, so the runner applies it last
        # and never before the publication routing that needs it.
        assert migration_files()[-1].name == REPAIR, migration_files()[-1].name
        checks += 1

        # The live board's state: the publication migration is installed and the
        # function it calls is not. schema.sql created it here, so it is dropped
        # to model a board that was upgraded into this rather than built into it.
        t.psql(admin, "DROP FUNCTION ticket_board.control_capabilities();")
        assert not function_exists(admin)
        assert t.psql(admin, "SELECT to_regprocedure('ticket_board.publication_control_role()') IS NOT NULL;") == "t"
        checks += 1

        # Publication admits by declared capability and nothing else, so the
        # board needs a workflow before any of this is reachable. The example
        # document gives `app` request_publication and the director the control
        # capabilities `publication_control_role()` looks for.
        document = json.dumps(validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text())))
        t.psql(
            service,
            "SELECT set_config('ticket_board.caller_role', 'director', false);\n"
            "SELECT set_config('ticket_board.project', 'cerulean', false);\n"
            "SELECT ticket_board.apply_declared_workflow($$" + document + "$$::jsonb);",
        )
        assert t.psql(admin, "SELECT ticket_board.declared_workflow() IS NOT NULL;") == "t"
        t.seed_postgres_ticket(admin, "SYRD-100", title="Owner identity", state="in_progress", assignee="app")

        # The refusal, reproduced. This is what a role got instead of a request.
        ok, output = request_publication(
            service, role="app", ticket="SYRD-100", ref="roles/app/syrd-100-owner-identity"
        )
        assert not ok, output
        assert "control_capabilities() does not exist" in output, output
        assert "request_publication" in output, output
        assert recorded_requests(admin, "SYRD-100") == [], recorded_requests(admin, "SYRD-100")
        checks += 1

        # The repair, applied the way the runner would apply it.
        t.psql(admin, (MIGRATIONS / REPAIR).read_text(encoding="utf-8"))
        assert function_exists(admin)
        assert t.psql(admin, "SELECT ticket_board.control_capabilities()::text;") == "{set_manually_controlled,merge}"
        checks += 1

        # And the same operation now records a request instead of failing.
        ok, output = request_publication(
            service, role="app", ticket="SYRD-100", ref="roles/app/syrd-100-owner-identity"
        )
        assert ok, output
        requests = recorded_requests(admin, "SYRD-100")
        assert len(requests) == 1, requests
        assert requests[0]["ref"] == "roles/app/syrd-100-owner-identity", requests[0]
        assert requests[0]["commit_hash"] == COMMIT, requests[0]
        assert requests[0]["bundle_path"] == BUNDLE, requests[0]
        checks += 1

        # Applying it again changes nothing, which is what the runner does on
        # every subsequent upgrade.
        t.psql(admin, (MIGRATIONS / REPAIR).read_text(encoding="utf-8"))
        assert function_exists(admin)
        assert t.psql(admin, "SELECT ticket_board.control_capabilities()::text;") == "{set_manually_controlled,merge}"
        assert len(recorded_requests(admin, "SYRD-100")) == 1
        checks += 1

        # Fresh-schema parity: a board built from schema.sql alone has the same
        # function, so the two ways of arriving at a board agree.
        fresh_db = "syrd_publication_fresh"
        fresh = t.conninfo(sock, port, fresh_db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", fresh_db])
        t.psql(fresh, (ROOT / "scripts/ticket_board/schema.sql").read_text(encoding="utf-8"))
        assert function_exists(fresh)
        assert t.psql(fresh, "SELECT ticket_board.control_capabilities()::text;") == (
            t.psql(admin, "SELECT ticket_board.control_capabilities()::text;")
        )
        assert t.psql(fresh, "SELECT prosrc FROM pg_proc WHERE proname = 'control_capabilities';") == (
            t.psql(admin, "SELECT prosrc FROM pg_proc WHERE proname = 'control_capabilities';")
        )
        checks += 1

    print(f"ticket_board_publication_migration_recovery_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
