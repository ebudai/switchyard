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
