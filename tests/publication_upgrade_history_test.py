#!/usr/bin/env python3
"""SYRD-93: a real tenant's upgrade, run by the real runner, in order.

Two things go wrong when a migration is written beside a moving main. It can
reuse a number another branch allocated, and then a fresh install applies the
two in a different order than an upgraded tenant did. And it can re-create a
function from a body that predates a migration the tenant already ran, silently
rolling that release back with no error anywhere.

So this drives the shipped runner over a tenant that stops at the release before
this migration, and asserts that everything the releases before it guaranteed is
still true afterwards -- and again when the same upgrade is applied twice.
"""

from __future__ import annotations

import copy
import json
import os
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
from temporary_cluster import temporary_cluster

MIGRATIONS = ROOT / "scripts/ticket_board/migrations"
MINE = MIGRATIONS / "pgu930_syrd93_publication_requests.sql"
PREVIOUS = "pgu929_syrd92_director_defer_backlog.sql"
CANONICAL = json.loads((ROOT / "examples/workflows/inspection.json").read_text())


def migration_numbers() -> dict[int, list[str]]:
    numbers: dict[int, list[str]] = {}
    for path in sorted(MIGRATIONS.glob("pgu*.sql")):
        numbers.setdefault(int(path.name[3:].split("_", 1)[0]), []).append(path.name)
    return numbers


def test_the_number_is_unused_and_sorts_after_everything_before_it() -> None:
    """The defect this guards against happened: two branches both took pgu929.

    The runner applies every file it has not recorded, in filename order, so a
    number is not decoration -- it is the only thing that makes a fresh install
    apply these in the order an upgraded tenant did.
    """
    numbers = migration_numbers()
    mine = int(MINE.name[3:].split("_", 1)[0])
    assert numbers[mine] == [MINE.name], numbers[mine]
    assert mine > max(n for n in numbers if n < mine)
    for number, names in numbers.items():
        if number >= 920:
            assert len(names) == 1, (number, names)


def test_every_function_it_re_creates_matches_the_schema_it_ships_with() -> None:
    """A body older than the schema beside it is a rollback nobody is told about."""
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    migration = MINE.read_text()

    def bodies(text: str, name: str) -> list[str]:
        marker = f"CREATE OR REPLACE FUNCTION ticket_board.{name}("
        starts = [i for i in range(len(text)) if text.startswith(marker, i)]
        return [text[start : text.index("\n$$;", start) + 4] for start in starts]

    created = sorted({
        line.split("ticket_board.")[1].split("(")[0]
        for line in migration.splitlines()
        if line.startswith("CREATE OR REPLACE FUNCTION ticket_board.")
    })
    assert created, migration[:200]
    for name in created:
        in_schema = bodies(schema, name)
        assert in_schema, f"{name} is created by the migration but not by schema.sql"
        assert bodies(migration, name)[-1] == in_schema[-1], name

    # Named outright, so the check above cannot pass vacuously: this migration
    # re-creates the validator, and the validator carries the floors of the two
    # releases before it as well as this one's vocabulary.
    validator = bodies(migration, "validate_declared_workflow")[-1]
    assert "'resolve_publication'" in validator                       # SYRD-93
    assert "'director_edit'" in validator                             # SYRD-83
    assert "director must keep its control capabilities" in validator  # SYRD-82
    assert "stage where deferred work can wait" in validator          # SYRD-92
    assert "deferred work must have an ordinary way back" in validator


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(ROOT), text=True, capture_output=True, check=True
    ).stdout


def schema_at(migration_name: str) -> str:
    """schema.sql as it stood in the release that added `migration_name`.

    Asked of the PREVIOUS migration rather than this one, so it describes a
    tenant that stopped at the last released schema -- which is the tenant this
    upgrade has to work for, and which does not depend on this branch's own
    commits existing yet.
    """
    adding = git(
        "log", "--format=%H", "--diff-filter=A", "--",
        f"scripts/ticket_board/migrations/{migration_name}",
    ).strip().splitlines()
    assert adding, migration_name
    return git("show", f"{adding[-1]}:scripts/ticket_board/schema.sql")


def run_migration_runner(cluster, db: str) -> str:
    proc = subprocess.run(
        [str(ROOT / "scripts/ticket-board-migrate")],
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TICKET_BOARD_ADMIN_DATABASE_URL": t.conninfo(cluster.socket_dir, cluster.port, db),
        },
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stderr


def pre_record_migrations(admin: str, *, applying: set[str]) -> None:
    names = [path.name for path in sorted(MIGRATIONS.glob("*.sql")) if path.name not in applying]
    assert applying <= {path.name for path in MIGRATIONS.glob("*.sql")}
    values = ",".join(f"('{name}')" for name in names)
    t.psql(
        admin,
        "CREATE TABLE IF NOT EXISTS ticket_board.schema_migrations ("
        "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());\n"
        f"INSERT INTO ticket_board.schema_migrations(name) VALUES {values} ON CONFLICT DO NOTHING;",
    )


def before_this_release(document: dict) -> dict:
    """The document as a tenant at the previous release could have had it."""
    document = copy.deepcopy(document)
    for role in document["roles"]:
        role["capabilities"] = [
            c for c in role["capabilities"]
            if c not in ("request_publication", "resolve_publication")
        ]
    return document


def seed_stored_document(admin: str, document: dict) -> None:
    payload = json.dumps(document).replace("'", "''")
    t.psql(
        admin,
        f"""
        INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('seed', '{payload}'::jsonb);
        INSERT INTO ticket_board.workflow_configuration(singleton, revision, document)
             VALUES (true, (SELECT max(revision) FROM ticket_board.workflow_revisions), '{payload}'::jsonb)
        ON CONFLICT (singleton) DO UPDATE SET revision=EXCLUDED.revision, document=EXCLUDED.document;
        INSERT INTO ticket_board.workflow_roles(name, definition)
        SELECT x->>'name', x FROM jsonb_array_elements('{payload}'::jsonb->'roles') x
        ON CONFLICT (name) DO UPDATE SET definition=EXCLUDED.definition;
        """,
    )


def capabilities(admin: str, role: str) -> list[str]:
    return json.loads(t.psql(
        admin,
        "SELECT coalesce(definition->'capabilities', '[]'::jsonb) "
        f"FROM ticket_board.workflow_roles WHERE name = '{role}';",
    ))


def installed(admin: str, function: str) -> str:
    return t.psql(
        admin,
        "SELECT string_agg(p.prosrc, E'\\n') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE n.nspname = 'ticket_board' AND p.proname = '{function}';",
    )


def assert_every_floor_survives(admin: str, label: str) -> None:
    validator = installed(admin, "validate_declared_workflow")
    floor = installed(admin, "director_control_capabilities")
    guard = installed(admin, "enforce_declared_ticket_update")
    # SYRD-82: the control floor, and SYRD-83's edit in it.
    assert "director must keep its control capabilities" in validator, label
    assert "'director_edit'" in floor, label
    assert "director_edit_target" in guard, label
    # SYRD-92: somewhere to put work down, and a way back.
    for rule in (
        "workflow must keep a stage where deferred work can wait",
        "director must be able to defer work out of every active stage",
        "deferred work must have an ordinary way back",
    ):
        assert rule in validator, (label, rule)
    assert "declared_parking_stage" in guard, label
    # SYRD-93: the vocabulary and the floor this migration adds.
    assert "'request_publication'" in validator, label
    assert "'resolve_publication'" in floor, label
    # And the validator actually enforces the whole of it, not just mentions it.
    stripped = before_this_release(CANONICAL)
    stripped["project"] = "cerulean"
    payload = json.dumps(stripped).replace("'", "''")
    try:
        t.psql(admin, f"SELECT ticket_board.validate_declared_workflow('{payload}'::jsonb);")
    except AssertionError as exc:
        assert "resolve_publication" in str(exc), (label, exc)
    else:
        raise AssertionError(f"{label}: the floor accepted a director without resolve_publication")


def main() -> int:
    for test in (
        test_the_number_is_unused_and_sorts_after_everything_before_it,
        test_every_function_it_re_creates_matches_the_schema_it_ships_with,
    ):
        test()

    with temporary_cluster(prefix="publication-upgrade-history-", shutdown="immediate") as cluster:
        # A tenant that stopped at the release before this one: its schema
        # predates this migration, and it has recorded everything up to and
        # including the migration that shipped with that schema.
        db = "syrd93_ordered_tail"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, schema_at(PREVIOUS))
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        document = before_this_release(CANONICAL)
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        seed_stored_document(admin, document)
        pre_record_migrations(admin, applying={MINE.name})
        assert "request_publication" not in capabilities(admin, "ops")

        applied = run_migration_runner(cluster, db)
        assert f"apply {MINE.name}" in applied, applied
        # Only this one: the tenant already ran the release before it.
        assert f"apply {PREVIOUS}" not in applied, applied
        t.psql(admin, t.RBAC_PATH.read_text())

        # The upgrade landed, on the roles it belongs to.
        assert "request_publication" in capabilities(admin, "ops"), capabilities(admin, "ops")
        assert "resolve_publication" in capabilities(admin, "director"), capabilities(admin, "director")
        assert "request_publication" not in capabilities(admin, "audit")
        assert_every_floor_survives(admin, "ordered-tail")

        # Applying the same upgrade again changes nothing: not the document, not
        # the revision, not the recorded history.
        revision = t.psql(admin, "SELECT revision::text FROM ticket_board.workflow_configuration;")
        recorded = t.psql(admin, "SELECT count(*)::text FROM ticket_board.schema_migrations;")
        run_migration_runner(cluster, db)
        t.psql(admin, "BEGIN;\n" + MINE.read_text() + "\nCOMMIT;")
        assert t.psql(admin, "SELECT revision::text FROM ticket_board.workflow_configuration;") == revision
        assert t.psql(admin, "SELECT count(*)::text FROM ticket_board.schema_migrations;") == recorded
        assert capabilities(admin, "director").count("resolve_publication") == 1
        assert_every_floor_survives(admin, "reapplied")

        # And a tenant already carrying both halves is not rewritten at all: a
        # revision nobody asked for reads as somebody editing the workflow.
        fresh = "syrd93_healthy"
        fresh_admin = t.conninfo(cluster.socket_dir, cluster.port, fresh)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", fresh])
        t.psql(fresh_admin, t.SCHEMA_PATH.read_text())
        healthy = copy.deepcopy(CANONICAL)
        healthy["project"] = "cerulean"
        healthy.setdefault("reassign", {})
        healthy.setdefault("remove_stages", [])
        seed_stored_document(fresh_admin, healthy)
        before = t.psql(fresh_admin, "SELECT revision::text FROM ticket_board.workflow_configuration;")
        t.psql(fresh_admin, "BEGIN;\n" + MINE.read_text() + "\nCOMMIT;")
        assert t.psql(fresh_admin, "SELECT revision::text FROM ticket_board.workflow_configuration;") == before
    print("publication_upgrade_history_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
