#!/usr/bin/env python3
"""SYRD-855: every migration from FIRST_DECLARED on says whether older code runs against it, and the rule reads it.

A rollback keeps the newer release's migrations, so it is allowed across a migration only when that migration was
checked against older code (`scripts/ticket_board/schema_compatibility.py`). A migration added without a verdict is
refused here, so its author decides it; the rule itself refuses an undeclared, unsafe or unmet one by name.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board import migration_order  # noqa: E402
from scripts.ticket_board import schema_compatibility as compat  # noqa: E402

MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def test_every_new_migration_is_declared() -> None:
    names = [path.name for path in MIGRATIONS.glob("*.sql")]
    due = sorted(name for name in names if migration_order.number(name) >= compat.FIRST_DECLARED)
    check(due, "there are migrations to declare")
    undeclared = [name for name in due if name not in compat.DECLARED]
    check(not undeclared, f"every migration from pgu{compat.FIRST_DECLARED} on says whether older code runs against "
                          f"it, in scripts/ticket_board/schema_compatibility.py: {undeclared}")
    stale = sorted(set(compat.DECLARED) - set(names))
    check(not stale, f"every declaration names a migration this tree carries: {stale}")
    for name, verdict in compat.DECLARED.items():
        check(compat.migration_number(name) == migration_order.number(name), f"{name}: one number line")
        check(verdict.reason.strip() and (not verdict.holds_while) == (not verdict.while_text),
              f"{name}: a reason, and a condition comes with its words: {verdict}")


def test_conditions_name_functions_their_migration_or_an_earlier_one_defines() -> None:
    defined: dict[str, int] = {}
    for path in sorted(MIGRATIONS.glob("*.sql"), key=lambda p: migration_order.key(p.name)):
        for function in re.findall(r"CREATE OR REPLACE FUNCTION ticket_board\.(\w+)\(", path.read_text()):
            defined.setdefault(function, migration_order.number(path.name))
    for name, verdict in compat.DECLARED.items():
        for function in re.findall(r"ticket_board\.(\w+)\(", verdict.holds_while):
            check(defined.get(function, 1 << 30) <= compat.migration_number(name),
                  f"{name}: its condition calls {function}, which the database has once this migration is applied")


def test_crossed_is_what_the_database_has_and_the_older_release_does_not() -> None:
    applied = ["schema.sql", "270_ready_to_in_progress.sql", "pgu1050_x.sql", "pgu1052_y.sql", "pgu1051_z.sql"]
    carried = ["270_ready_to_in_progress.sql", "pgu1050_x.sql"]
    check(compat.crossed(applied, carried) == ["pgu1051_z.sql", "pgu1052_y.sql"],
          f"in number order, and schema.sql is not a migration: {compat.crossed(applied, carried)}")
    check(compat.crossed(carried, carried) == [], "nothing newer, nothing crossed")


def test_the_rule_refuses_by_name() -> None:
    research, plan, unconditional = ("pgu1051_syrd842_research_briefs.sql", "pgu1052_syrd843_plan_gate.sql",
                                     "pgu1053_syrd848_documentation_pair.sql")
    asked: list[str] = []

    def holds(answers):
        return lambda expression: (asked.append(expression), answers.get(expression))[-1]

    yes = {compat.RESEARCH_OFF: True, compat.PLAN_GATE_OFF: True}
    check(compat.refusals([research, plan, unconditional], holds=holds(yes)) == [],
          "declared, and every condition holds: no refusal")
    check(compat.RESEARCH_OFF in asked and compat.PLAN_GATE_OFF in asked, f"each condition was asked: {asked}")
    asked.clear()
    check(compat.refusals([unconditional], holds=holds({})) == [] and asked == [],
          "an unconditional verdict asks the database nothing")
    declared_on = compat.refusals([research], holds=holds({compat.RESEARCH_OFF: False}))
    check(len(declared_on) == 1 and research in declared_on[0] and "research stage is not declared, and it is "
          "declared on this board" in declared_on[0], f"a condition that does not hold is said: {declared_on}")
    unread = compat.refusals([plan], holds=holds({}))
    check(len(unread) == 1 and "could not be read" in unread[0], f"an unreadable condition is not proof: {unread}")
    unknown = compat.refusals(["pgu9999_new.sql"], holds=holds(yes))
    check(len(unknown) == 1 and "pgu9999_new.sql is not declared" in unknown[0], f"undeclared is refused: {unknown}")
    saved = dict(compat.DECLARED)
    try:
        compat.DECLARED["pgu9998_drops.sql"] = compat.OlderCode(False, "drops a column older code reads")
        unsafe = compat.refusals(["pgu9998_drops.sql"], holds=holds(yes))
    finally:
        compat.DECLARED.clear()
        compat.DECLARED.update(saved)
    check(len(unsafe) == 1 and "declared unsafe" in unsafe[0] and "drops a column" in unsafe[0],
          f"declared unsafe is refused with its reason: {unsafe}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"schema_compatibility_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
