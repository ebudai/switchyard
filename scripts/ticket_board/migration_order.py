"""The order board migrations run in, by number rather than by spelling (SYRD-617).

`ticket-board-migrate` applies every migration a board has not recorded, in
this order, and a fresh board applies all of them in it. The number in the
name is therefore the only thing that makes a fresh install apply migrations
in the order an upgraded board did. Text order put `pgu1000_...` before
`pgu367_...`, so the namespace was exhausted at `pgu999`.

A migration's name is `pgu<number>_<description>.sql`, or `<number>_...` for
the eighteen files from before the `pgu` prefix. Both spellings share one
number line. The number is written without leading zeros: `pgu0999` would be
read as 999 but spelled as something else, so it is refused as ambiguous.
Everything else is refused too, before anything is applied, rather than placed
somewhere. A file is never placed by guessing what it meant.

One number belongs to one file. A few numbers below 920 were used twice or
more before that was enforced. Those files are history that boards have
already recorded by name, so they keep their place, ordered among themselves
byte by byte, exactly as text order placed them. No new file may join them.

This module imports only the standard library: the runner runs it with
`python3 -I`, as root on some hosts, from the release it is migrating.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

NAME = re.compile(r"(?:pgu)?(0|[1-9][0-9]*)_[a-z0-9_]+\.sql", re.ASCII)
#: The general shape before the number is checked for a canonical spelling, so
#: a leading zero is reported as what it is.
SHAPE = re.compile(r"(?:pgu)?[0-9]+_[a-z0-9_]+\.sql", re.ASCII)
#: Every number used by more than one migration before uniqueness was enforced,
#: with exactly the files that share it.
SHARED_NUMBERS: dict[int, frozenset[str]] = {
    458: frozenset({"pgu458_pgu340_director_force_move.sql", "pgu458_submit_to_inspection.sql"}),
    521: frozenset({"pgu521_workflow_transition_shadow_mode.sql", "pgu521_zz_shadow_log_read_grants.sql"}),
    755: frozenset({"pgu755_file_bug_assignee.sql", "pgu755_file_bug_blockers.sql"}),
    759: frozenset({"pgu759_needs_audit_gate.sql", "pgu759_round2_needs_audit_guards.sql",
                    "pgu759_round4_audit_skip_to_dat.sql", "pgu759_round5_dat_gate_skip_transition.sql"}),
    776: frozenset({"pgu776_dismiss_notification_by_key.sql", "pgu776_notification_dead_letter.sql"}),
    812: frozenset({"pgu812_multi_audit_stage_entry_assignee.sql", "pgu812_multi_audit_transition_target.sql"}),
}


class MigrationOrderError(ValueError):
    """A migrations directory whose order cannot be decided; nothing in it may be applied."""


def number(name: str) -> int:
    """The migration number in `name`; MigrationOrderError when the name is not a migration's."""
    match = NAME.fullmatch(name)
    if match:
        return int(match.group(1))
    if SHAPE.fullmatch(name):
        raise MigrationOrderError(f"{name}: the number has a leading zero, so it is ambiguous; "
                                  "write it without one")
    raise MigrationOrderError(f"{name}: a migration must be named pgu<number>_<description>.sql "
                              "(lowercase letters, digits and underscores)")


def key(name: str) -> tuple[int, bytes]:
    """Sort key: the number, then (for the historical shared numbers only) the name's bytes."""
    return number(name), name.encode("ascii")


def ordered(names: Iterable[str]) -> list[str]:
    """`names` in the order they are applied; every problem is reported at once, and nothing is ordered."""
    problems: list[str] = []
    by_number: dict[int, list[str]] = {}
    for name in names:
        try:
            by_number.setdefault(number(name), []).append(name)
        except MigrationOrderError as exc:
            problems.append(str(exc))
    for value, sharing in sorted(by_number.items()):
        if len(sharing) > 1 and not set(sharing) <= SHARED_NUMBERS.get(value, frozenset()):
            problems.append(f"migration number {value} is used by more than one file: "
                            + ", ".join(sorted(sharing)) + "; give each its own number")
    if problems:
        raise MigrationOrderError("; ".join(problems))
    return sorted((name for sharing in by_number.values() for name in sharing), key=key)


def ordered_files(directory: Path) -> list[Path]:
    """The migrations in `directory`, in order. An entry named *.sql that is not a regular file is refused."""
    entries = sorted(entry for entry in Path(directory).iterdir() if entry.name.endswith(".sql"))
    odd = [entry.name for entry in entries if entry.is_symlink() or not entry.is_file()]
    if odd:
        raise MigrationOrderError("not a regular file: " + ", ".join(odd))
    return [Path(directory) / name for name in ordered(entry.name for entry in entries)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: migration_order.py <migrations directory>", file=sys.stderr)
        return 2
    try:
        files = ordered_files(Path(argv[1]))
    except (MigrationOrderError, OSError) as exc:
        print(f"refusing to order {argv[1]}: {exc}", file=sys.stderr)
        return 1
    for path in files:
        print(path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
