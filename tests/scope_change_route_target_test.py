"""SYRD-888: a scope change opens its plan ticket when the Director's move out of triage has more than one target.

On sapi (release e71574b6, declared revision 43) `request-scope-change SAPI-159` was refused with "unknown or ambiguous
workflow action; specify target" and rolled back whole: sapi declares `route` from analysis to in_progress and to
user_review, and request_scope_change performed the move into Implementation by its action name alone.

schema.sql's copy of request_scope_change is the one the newest migration installs (pgu1062), so a fresh board and an
upgraded board run the same function. On a real board -- built fresh from this tree, and upgraded from the parent
tree through this tree's migrations -- whose workflow
is the shipped one (as the board suites use it) with the plan gate (owner: a new Designer) and a second Director `route` out of analysis, to
user_review, declared before the one to in_progress:

* the workflow really is ambiguous by name: two `route` moves leave analysis;
* request_scope_change succeeds, its plan ticket lands in Implementation (in_progress) with the plan owner, flagged
  as a plan change, and the waiting ticket is blocked on it;
* a second scope change extends that plan ticket.
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

from documentation_gate_board_test import example, row, short  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster  # noqa: E402
from schema_function_drift import assert_no_drift, owning_migration  # noqa: E402
from scripts.ticket_board import plan_gate as pg  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

CHECKS = 0
MIGRATION = "scripts/ticket_board/migrations/pgu1062_syrd888_scope_change_route_target.sql"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def parent_tree(into: Path) -> Path:
    """The tree this change was made on: the parent of the commit adding its migration, or HEAD while uncommitted."""
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--", MIGRATION],
                            check=True, capture_output=True, text=True).stdout.split()
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^" if adding else "HEAD", "scripts"],
                             check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    check(not (into / MIGRATION).exists(), "the parent tree has no SYRD-888 migration")
    return into


def two_route_document() -> dict:
    """sapi's shape: the plan gate owned by a Designer, and the Director's route out of analysis to two stages."""
    doc = pg.with_plan_gate(example(), designer={"runtime": "claude"})
    transitions = doc["transitions"]
    at = next(i for i, t in enumerate(transitions) if t["from"] == "analysis" and t["action"] == "route")
    transitions.insert(at, {**copy.deepcopy(transitions[at]), "to": "user_review"})
    validate(doc)
    return doc


def scenario(board: Board, name: str) -> None:
    owner = board.sql("SELECT ticket_board.plan_policy()->>'owner';")
    check(owner == "designer", f"{name}: the plan owner is the Designer: {owner}")
    routes = board.sql("SELECT string_agg(x->>'to', ',' ORDER BY ord) FROM jsonb_array_elements("
                       "ticket_board.declared_workflow()->'transitions') WITH ORDINALITY AS a(x, ord) "
                       "WHERE x->>'from' = 'analysis' AND x->>'action' = 'route';")
    check(routes == "user_review,in_progress", f"{name}: route leaves analysis for two stages: {routes}")

    t = "PGU-959"
    fixture.seed_postgres_ticket(board.admin, t, title="Export to CSV", state="analysis", assignee="director")
    said = short(board.act(t, "request_scope_change", "director", reason="The plan has no export deliverable."))
    check(said == "ok", f"{name}: the scope change is accepted: {said}")
    plan = board.sql("SELECT string_agg(id, ',' ORDER BY id) FROM ticket_board.tickets "
                     "WHERE workflow_flags->>'plan_change' = 'true';")
    check(plan and "," not in plan, f"{name}: one plan ticket opens: {plan!r}")
    check(board.where(plan) == ["in_progress", "designer"] and row(board, plan)["flags"].get("plan_change") is True,
          f"{name}: in Implementation, for the plan owner: {board.where(plan)}")
    blockers = board.sql(f"SELECT string_agg(blocker_ticket_id, ',') FROM ticket_board.ticket_blockers "
                         f"WHERE ticket_id = '{t}';")
    check(blockers == plan, f"{name}: the ticket waits on the plan ticket: {blockers!r}")

    other = "PGU-957"
    fixture.seed_postgres_ticket(board.admin, other, title="Import", state="analysis", assignee="director")
    said = short(board.act(other, "request_scope_change", "director", reason="And an import."))
    blockers = board.sql(f"SELECT string_agg(blocker_ticket_id, ',') FROM ticket_board.ticket_blockers "
                         f"WHERE ticket_id = '{other}';")
    check(said == "ok" and blockers == plan, f"{name}: a second scope change extends it: {said} {blockers!r}")


def same_function_fresh_and_upgraded() -> None:
    owner = owning_migration("request_scope_change").name
    check(owner == Path(MIGRATION).name, f"the newest migration defining request_scope_change is SYRD-888's: {owner}")
    try:
        assert_no_drift("request_scope_change")
        drift = ""
    except AssertionError as exc:
        drift = str(exc)
    check(not drift, f"schema.sql's request_scope_change is pgu1062's: {drift}")


def main() -> int:
    same_function_fresh_and_upgraded()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    doc = two_route_document()
    with temporary_cluster(prefix="syrd888-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd888-tree.") as tmp:
        for name, before in (("fresh", None), ("upgraded", parent_tree(Path(tmp) / "parent"))):
            board = Board(cluster, name, doc, before=before)
            try:
                scenario(board, name)
            finally:
                board.close()
    print(f"scope_change_route_target_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
