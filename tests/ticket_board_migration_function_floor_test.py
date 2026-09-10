#!/usr/bin/env python3
"""SYRD-100: a migration may not depend on something only a fresh install has.

A live board is created once from schema.sql and then only ever migrated. So a
function added to schema.sql after that board was installed reaches it only if a
migration installs it too. `control_capabilities()` was added on 2026-09-09 with
no migration, and `pgu930_syrd93_publication_requests.sql` calls it from
`publication_control_role()`. Every fresh-install test passed. The live board,
which has been upgraded rather than rebuilt, refused the first publication
request any role ever made:

    function ticket_board.control_capabilities() does not exist
    CONTEXT: PL/pgSQL function publication_control_role() line 5
             PL/pgSQL function request_publication(text,text,text,text) line 74

The neighbouring commit-diff guard would have caught the commit that did it --
it flags 7848339 -- but it only ever looks at commits on the current branch, so
once such a commit is on main nothing re-examines it. This looks at the tree.

It lives in its own file because that guard's suite pins commit ids that are not
present in every clone, so it does not run everywhere; a guard that does not run
is not a guard.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from ticket_board_schema_function_migration_test import (
    MIGRATION_FUNCTION_RE,
    MIGRATIONS_PREFIX,
    SCHEMA_PATH,
    extract_schema_function_bodies,
)

#: Any `ticket_board.<name>(` in a migration. Narrowed to schema.sql's function
#: names before it is used, so a table in an INSERT is never mistaken for a call.
CALL_RE = re.compile(r"(?i)\bticket_board\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def migration_paths() -> list[str]:
    """Every migration, in the order the runner applies them: a filename sort."""
    directory = ROOT / MIGRATIONS_PREFIX
    return sorted(f"{MIGRATIONS_PREFIX}{path.name}" for path in directory.glob("*.sql"))


def bare(names: object) -> set[str]:
    return {str(name).split(".")[-1] for name in names}


def functions_created_by_migrations() -> set[str]:
    created: set[str] = set()
    for path in migration_paths():
        created |= bare(MIGRATION_FUNCTION_RE.findall((ROOT / path).read_text(encoding="utf-8")))
    return created


def schema_function_names(sql: str) -> set[str]:
    return bare(extract_schema_function_bodies(sql))


#: The schema-only functions migrations already call, and may keep calling.
#:
#: Each is demonstrably present on every deployed board: migrations have called
#: them for months and no board has ever failed on one. That is a fact about
#: what is deployed, which no check of this repository can derive -- the first
#: migration landed before two of these were even added to schema.sql, so "older
#: than the series" is not the property that makes them safe. So it is written
#: down once, here, rather than inferred from history that does not support it.
#:
#: This is a ratchet, not a list to add to. A new name here means a migration has
#: taken a dependency an upgraded board will not have, which is what took the
#: live board's publication path down. Install the function in a migration.
HISTORICAL_SCHEMA_ONLY_CALLS = frozenset(
    {
        "current_actor_role",
        "record_notification_trace",
        "require_ticket_board_listener",
        "transition_notification_payload",
    }
)


def migrations_calling_unmigrated_schema_functions() -> dict[str, list[str]]:
    schema_functions = schema_function_names((ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    at_risk = schema_functions - functions_created_by_migrations()
    at_risk -= HISTORICAL_SCHEMA_ONLY_CALLS
    offenders: dict[str, list[str]] = {}
    for path in migration_paths():
        text = (ROOT / path).read_text(encoding="utf-8")
        own = bare(MIGRATION_FUNCTION_RE.findall(text))
        missing = sorted((set(CALL_RE.findall(text)) - own) & at_risk)
        if missing:
            offenders[path] = missing
    return offenders


def test_no_migration_depends_on_a_function_only_a_fresh_install_has() -> None:
    offenders = migrations_calling_unmigrated_schema_functions()
    assert not offenders, (
        "these migrations call functions that only a fresh schema.sql install has, so an "
        f"upgraded board fails on them: {offenders}"
    )


def test_the_ratchet_names_only_functions_migrations_really_call() -> None:
    """No dead entries: an exception nothing needs is one nobody reviews."""
    schema_functions = schema_function_names((ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    unmigrated = schema_functions - functions_created_by_migrations()
    called: set[str] = set()
    for path in migration_paths():
        text = (ROOT / path).read_text(encoding="utf-8")
        own = bare(MIGRATION_FUNCTION_RE.findall(text))
        called |= (set(CALL_RE.findall(text)) - own) & unmigrated
    assert HISTORICAL_SCHEMA_ONLY_CALLS <= called, (
        "listed as historical, but no migration calls them: "
        f"{sorted(HISTORICAL_SCHEMA_ONLY_CALLS - called)}"
    )
    # And the one this ticket is about is not among them any more: a migration
    # installs it, so it is not an exception at all.
    assert "control_capabilities" not in called, (
        "control_capabilities is still reachable only from a fresh install"
    )


def test_the_publication_function_now_arrives_by_migration() -> None:
    """What makes the check above pass, named so a reader can find it."""
    schema_functions = schema_function_names((ROOT / SCHEMA_PATH).read_text(encoding="utf-8"))
    assert "control_capabilities" in schema_functions
    assert "control_capabilities" in functions_created_by_migrations(), (
        "without a migration installing it, the live board cannot record a publication request"
    )
    # And exactly one migration installs it, with schema.sql's own body.
    installers = [
        path
        for path in migration_paths()
        if "control_capabilities" in bare(MIGRATION_FUNCTION_RE.findall((ROOT / path).read_text(encoding="utf-8")))
    ]
    assert len(installers) == 1, installers
    schema_body = extract_schema_function_bodies(
        (ROOT / SCHEMA_PATH).read_text(encoding="utf-8")
    )["ticket_board.control_capabilities"]
    migration_body = extract_schema_function_bodies(
        (ROOT / installers[0]).read_text(encoding="utf-8")
    )["ticket_board.control_capabilities"]
    assert schema_body == migration_body, (schema_body, migration_body)


def main() -> int:
    test_no_migration_depends_on_a_function_only_a_fresh_install_has()
    test_the_ratchet_names_only_functions_migrations_really_call()
    test_the_publication_function_now_arrives_by_migration()
    print("ticket_board_migration_function_floor_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
