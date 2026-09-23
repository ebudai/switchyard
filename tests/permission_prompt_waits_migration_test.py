#!/usr/bin/env python3
"""An upgraded board gets the function a fresh one is built with.

`ticket_board.notify_permission_prompt_waits` arrived with the permission-wait
escalation and was written into schema.sql, which is what a FRESH board is
built from. No ordered migration created it, so a tenant that arrives by
upgrade replayed the whole tail cleanly and then current rbac.sql tried to
grant a function that was not there:

    ERROR: function ticket_board.notify_permission_prompt_waits(
           jsonb, timestamp with time zone, interval) does not exist

which is exactly where tests/ticket_board_director_defer_backlog_test.py
stopped on pristine main (SYRD-243).

Both histories are built for real, in one temporary cluster: a fresh schema,
and a pgu928-era schema replaying the ordered tail. What is compared is what
PostgreSQL ends up holding -- `pg_get_functiondef` and the listener's EXECUTE
grant -- rather than the text either one was built from.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import ticket_board_write_api_test as t  # noqa: E402
from schema_function_drift import assert_no_drift, owning_migration  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"
MIGRATION = MIGRATIONS / "pgu956_syrd243_permission_prompt_waits.sql"
SCHEMA = ROOT / "scripts" / "ticket_board" / "schema.sql"
RBAC = ROOT / "scripts" / "ticket_board" / "rbac.sql"
#: Where a tenant that predates the function starts from.
LEGACY_FROM = "pgu928_syrd83_director_edit.sql"
FUNCTION = "notify_permission_prompt_waits"
SIGNATURE = "jsonb, timestamp with time zone, interval"

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
    ).stdout


def schema_before(migration_name: str) -> str:
    """schema.sql as it stood before `migration_name` joined the tree."""
    adding = git(
        "log", "--format=%H", "--diff-filter=A", "--",
        f"scripts/ticket_board/migrations/{migration_name}",
    ).strip().splitlines()
    assert adding, migration_name
    return git("show", f"{adding[-1]}^:scripts/ticket_board/schema.sql")


def definition(admin: str) -> str:
    """What the database itself says the function is."""
    return t.psql(
        admin,
        "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE n.nspname = 'ticket_board' AND p.proname = '{FUNCTION}';",
    ).strip()


def listener_may_execute(admin: str) -> str:
    return t.psql(
        admin,
        "SELECT has_function_privilege('ticket_board_listener', "
        f"'ticket_board.{FUNCTION}({SIGNATURE})', 'EXECUTE');",
    ).strip()


def build(cluster, db: str, *, legacy: bool) -> str:
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, schema_before(LEGACY_FROM) if legacy else SCHEMA.read_text(encoding="utf-8"))
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
    if legacy:
        # The ordered tail, in the order the shipped runner applies it: every
        # migration at or after the release this tenant starts from.
        for path in sorted(MIGRATIONS.glob("pgu*.sql")):
            if path.name >= LEGACY_FROM:
                t.psql(admin, path.read_text(encoding="utf-8"))
    # And then the current RBAC pass, which is where the upgrade used to stop.
    t.psql(admin, RBAC.read_text(encoding="utf-8"))
    return admin


def main() -> int:
    with temporary_cluster(prefix="syrd243-", shutdown="immediate") as cluster:
        fresh = build(cluster, "syrd243_fresh", legacy=False)
        legacy = build(cluster, "syrd243_legacy", legacy=True)

        fresh_definition = definition(fresh)
        legacy_definition = definition(legacy)
        check(FUNCTION in fresh_definition, f"a fresh schema has it: {fresh_definition[:120]}")
        check(
            legacy_definition == fresh_definition,
            "the upgraded board holds the same definition as a fresh one:\n"
            f"fresh:\n{fresh_definition}\n\nlegacy:\n{legacy_definition}",
        )
        # The two things the ticket says must not move: who may call it, and
        # what it is called with.
        check(
            "require_ticket_board_listener" in legacy_definition,
            f"the listener-only authority check survives: {legacy_definition}",
        )
        check(
            f"{SIGNATURE.split(', ')[0]}" in legacy_definition and "interval" in legacy_definition,
            f"the callable signature is the one rbac.sql grants: {legacy_definition}",
        )
        for label, admin in (("fresh", fresh), ("legacy", legacy)):
            check(
                listener_may_execute(admin) == "t",
                f"{label}: ticket_board_listener may execute it",
            )

        # Idempotent: the ordered tail is replayed on any clean build, and a
        # migration that cannot run twice breaks the next upgrade rather than
        # this one.
        t.psql(legacy, MIGRATION.read_text(encoding="utf-8"))
        t.psql(legacy, MIGRATION.read_text(encoding="utf-8"))
        check(
            definition(legacy) == fresh_definition,
            "applying the migration twice leaves the same definition",
        )
        check(listener_may_execute(legacy) == "t", "and the grant still holds")

        # The listener calls it by exactly this signature; a board that has it
        # under another one is a board the listener cannot use.
        # Argument TYPES, in order, which is what a call site and a GRANT
        # resolve against; the parameter names are not part of that.
        called = t.psql(
            legacy,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = 'ticket_board' AND p.proname = '{FUNCTION}' "
            "AND pg_catalog.pg_get_function_arguments(p.oid) LIKE '%jsonb%timestamp with time zone%interval%';",
        ).strip()
        check(called == "1", f"exactly one function with the granted signature: {called}")
        overloads = t.psql(
            legacy,
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = 'ticket_board' AND p.proname = '{FUNCTION}';",
        ).strip()
        check(overloads == "1", f"and no second overload the listener could miss: {overloads}")

    # The copy the upgraded board runs, against the copy a fresh board is built
    # from, through the guard this repository already keeps for exactly that: a
    # migration copy that drifts is the one every upgraded tenant runs.
    assert_no_drift(FUNCTION)
    check(owning_migration(FUNCTION) == MIGRATION, owning_migration(FUNCTION).name)
    migration_text = MIGRATION.read_text(encoding="utf-8")
    check(
        "GRANT EXECUTE ON FUNCTION" in migration_text
        and "ticket_board_listener" in migration_text,
        "the migration restates the listener grant",
    )
    # Numbered so the ordered tail reaches it after what it depends on.
    later = [path.name for path in sorted(MIGRATIONS.glob("pgu*.sql")) if path.name > MIGRATION.name]
    check(later == [], f"it is the last ordered migration: {later}")

    # And the rule this ticket is an instance of. rbac.sql grants on a board
    # that may have arrived by upgrade, so every function it names has to be
    # one an upgrade produces: either it was already in the schema every
    # supported history starts from, or a migration installs it. This needs no
    # allowlist -- that IS the invariant -- and it is the check that would have
    # caught notify_permission_prompt_waits when it was written (SYRD-243).
    import re

    granted = sorted(set(re.findall(
        r"GRANT EXECUTE ON FUNCTION\s+ticket_board\.([a-z0-9_]+)\s*\(",
        RBAC.read_text(encoding="utf-8"),
    )))
    check(len(granted) > 60, f"the grants were found at all: {len(granted)}")
    floor = schema_before(LEGACY_FROM)
    installed = "\n".join(path.read_text(encoding="utf-8") for path in MIGRATIONS.glob("*.sql"))
    unreachable = [
        name for name in granted
        if f"FUNCTION ticket_board.{name}(" not in installed
        and f"FUNCTION ticket_board.{name}(" not in floor
    ]
    check(
        unreachable == [],
        "every function rbac.sql grants is one an upgraded board has, from the "
        f"legacy floor or a migration; these are neither: {unreachable}",
    )

    print(f"permission_prompt_waits_migration_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
