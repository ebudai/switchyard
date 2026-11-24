"""Which applied migrations an older release's board and listener can run against (SYRD-855).

A rollback of one tenant to an earlier release keeps every migration the newer release applied: migrations run
forward only, and reversing one would lose what was written under it. So the older board and listener run against a
newer schema, and that is safe only for a migration someone has checked against older code. This table records
that check, one entry per migration from `FIRST_DECLARED` on; `tests/schema_compatibility_test.py` refuses a new
migration that is not in it, so whoever writes a migration decides it.

- **Compatible** means: code from before this migration runs against the schema after it. It holds when the
  migration only adds objects older code never names (tables, functions, triggers) and keeps the signature of every
  function it replaces. A trigger it adds must not refuse an update older code makes.
- **holds_while** is a condition, as one SQL boolean expression over the tenant's own database, when compatibility
  depends on the tenant's declared workflow: typically a stage or gate the older code does not know, which the
  migration's triggers act on only when it is declared. Root evaluates it at the rollback, as the cluster's own
  account. Something that cannot be evaluated is not proof.
- **Not compatible**, or **not declared**, refuses the rollback across it, by name, and changes nothing.

The older release's own `rbac.sql` runs at the rollback (it is part of every deploy): it takes EXECUTE on the
newer functions away from the board roles, which the older code never calls. A later deploy of the newer release
grants them again.

Standard library only: root's launcher reads this, and nothing here imports another Switchyard module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

#: Every migration numbered from here on is declared in `DECLARED`. Older migrations are not, so a rollback across
#: one of them is refused: nobody checked it.
FIRST_DECLARED = 1051

_NAME = re.compile(r"(?:pgu)?(0|[1-9][0-9]*)_[a-z0-9_]+\.sql", re.ASCII)

RESEARCH_OFF = "NOT ticket_board.research_declared()"
PLAN_GATE_OFF = "ticket_board.plan_policy() IS NULL"


@dataclass(frozen=True)
class OlderCode:
    """One migration's verdict for code from before it."""

    compatible: bool
    reason: str
    #: A SQL boolean expression that must be true on the tenant's database, or "" when the verdict holds always.
    holds_while: str = ""
    #: What `holds_while` says, in words, for a refusal.
    while_text: str = ""


DECLARED: dict[str, OlderCode] = {
    "pgu1051_syrd842_research_briefs.sql": OlderCode(
        True, "adds the research-brief table, its functions and two tickets triggers, which act only on a board "
        "that declares the research stage", RESEARCH_OFF, "the research stage is not declared"),
    "pgu1052_syrd843_plan_gate.sql": OlderCode(
        True, "adds the plan tables, their functions and the tickets and workflow triggers, which act only on a "
        "board that declares the plan gate", PLAN_GATE_OFF, "the plan gate is not declared"),
    "pgu1053_syrd848_documentation_pair.sql": OlderCode(
        True, "adds the documentation-layer table, its read, and an AFTER UPDATE trigger that writes only that "
        "table; an older Publisher publishes the implementer commit alone, as it did"),
    "pgu1054_syrd849_findings_publication.sql": OlderCode(
        True, "replaces publisher_finish, publication_missing_signoffs and enforce_publisher_window with the same "
        "signatures, adding branches for research findings only", RESEARCH_OFF, "the research stage is not declared"),
    "pgu1055_syrd852_finding_mark.sql": OlderCode(
        True, "replaces the research-round trigger function, which acts only on a board that declares the research "
        "stage", RESEARCH_OFF, "the research stage is not declared"),
    "pgu1056_syrd864_close_requires_commit.sql": OlderCode(
        True, "replaces validate_declared_workflow with the same signature, only adding a refusal of a move into done "
        "without require_commit; older code's composers never write such a move, and older code calls the function "
        "only to apply a document, so it refuses nothing older code produces"),
    "pgu1057_syrd868_no_code_reasons.sql": OlderCode(
        True, "adds the no-code reason table and an AFTER UPDATE trigger on tickets that writes only that table; "
        "older code never reads it"),
    "pgu1058_syrd870_release_notice_message.sql": OlderCode(
        True, "adds a nullable message column to release_notices and replaces note_release_available with the same "
        "signature, storing the message as well; older code reads neither"),
    "pgu1059_syrd873_review_placement.sql": OlderCode(
        True, "adds the review-placement and fresh-review tables, a trigger writing only the first, and a balancer "
        "variant; replaces balance_review_stage, resolve_ticket_context_restore, request_fresh_ticket_context and "
        "board_stall_runnable with the same signatures. Older code's balancer call now also leaves a review the "
        "Director placed where it is, and its other calls behave as before"),
}


def migration_number(name: str) -> int | None:
    """The number in a migration's file name, or None for anything else (`schema.sql`)."""
    match = _NAME.fullmatch(name)
    return int(match.group(1)) if match else None


def crossed(applied: Iterable[str], carried: Iterable[str]) -> list[str]:
    """The migrations the database has applied that the older release does not carry, in number order."""
    known = set(carried)
    newer = [name for name in set(applied) if name not in known and migration_number(name) is not None]
    return sorted(newer, key=lambda name: (migration_number(name), name))


def refusals(newer: Iterable[str], *, holds: Callable[[str], bool | None]) -> list[str]:
    """Why the older release cannot run against each of `newer`; empty when it can against all of them.

    `holds` evaluates a `holds_while` expression on the tenant's database: True, False, or None when it could not
    be evaluated.
    """
    problems: list[str] = []
    for name in newer:
        verdict = DECLARED.get(name)
        if verdict is None:
            problems.append(f"{name} is not declared safe for older code (schema_compatibility), so nobody has "
                            "checked the older board against it")
        elif not verdict.compatible:
            problems.append(f"{name} is declared unsafe for older code: {verdict.reason}")
        elif verdict.holds_while:
            answer = holds(verdict.holds_while)
            if answer is None:
                problems.append(f"{name} is safe for older code only while {verdict.while_text}, and that could "
                                "not be read from the database")
            elif not answer:
                problems.append(f"{name} is safe for older code only while {verdict.while_text}, and it is "
                                "declared on this board")
    return problems
