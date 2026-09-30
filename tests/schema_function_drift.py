#!/usr/bin/env python3
"""Which copy of a board function an upgraded board actually ends up with.

Every migration that changes a function ships a whole copy of it, identical to
the one in `schema.sql` (SYRD-22 and every migration since). Fresh provisioning
installs schema.sql's copy; an upgrade installs the migrations' copies in
filename order, so the body a board runs is the one from the *last* migration
that defines it.

An older migration keeps the body it shipped. Rewriting it would change what a
board that already applied it received, and would misrepresent what that step
did -- so "has schema.sql drifted from its migration?" means the newest
migration defining the function, not all of them.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "scripts" / "ticket_board" / "schema.sql"
MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"


def definition(name: str) -> str:
    """The full `CREATE OR REPLACE FUNCTION ... $$;` text from schema.sql."""
    text = SCHEMA.read_text()
    start = text.index(f"CREATE OR REPLACE FUNCTION ticket_board.{name}(")
    return text[start:text.index("$$;", start) + 3]


def owning_migration(name: str) -> Path:
    """The migration whose copy of `name` an upgraded board is left running."""
    needle = f"CREATE OR REPLACE FUNCTION ticket_board.{name}("
    owners = sorted(p for p in MIGRATIONS.glob("*.sql") if needle in p.read_text())
    assert owners, f"no migration installs ticket_board.{name}"
    return owners[-1]


def assert_no_drift(*names: str) -> None:
    for name in names:
        owner = owning_migration(name)
        assert definition(name) in owner.read_text(), (
            f"ticket_board.{name} in schema.sql differs from the copy in "
            f"{owner.name}, so a fresh board and an upgraded board would not "
            f"run the same function"
        )


def file_before(migration: Path, path: str) -> str:
    """`path` as it stood before `migration` joined the tree.

    Not the merge-base with origin/main: that stops being "before" the moment
    the change it describes is merged, and a reproduction built on it would
    then run against the fix (SYRD-273 found SYRD-270's and SYRD-271's doing
    exactly that). Clone-based: the migration must be committed.
    """
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout

    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(migration.relative_to(ROOT))).split()
    assert adding, f"{migration.name} is not committed; clone-based checks see only commits"
    return git("show", f"{adding[-1]}^:{path}")


def schema_before(migration: Path) -> str:
    """schema.sql as it stood before `migration` joined the tree."""
    return file_before(migration, "scripts/ticket_board/schema.sql")


def rbac_before(migration: Path) -> str:
    """rbac.sql from the same point as `schema_before(migration)`.

    A board "as it shipped" is that schema with that era's grants. Today's
    rbac.sql grants on functions later migrations create, so on a historical
    schema it fails before the board exists (SYRD-526: SYRD-476's
    serial_reservations grant).
    """
    return file_before(migration, "scripts/ticket_board/rbac.sql")


def migrations_from(migration: Path) -> list[Path]:
    """`migration` and every migration after it, in the order the runner applies them.

    An upgraded board runs all of them before current rbac.sql, which may grant
    on what a later one creates (SYRD-270, SYRD-526); applying only the
    migration under test and then current rbac.sql is not an upgrade any board
    takes.
    """
    return [path for path in sorted(MIGRATIONS.glob("*.sql")) if path.name >= migration.name]
