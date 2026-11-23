#!/usr/bin/env python3
"""SYRD-864: a declared document whose move into done does not require the commit is refused when it is applied.

Measured on main 4427c2c2 with a board built the production way: a document whose `mark_done` had
`require_commit: false` was accepted by the board's own path (`apply_workflow`), and a code ticket that is not
`commit_exempt`, carrying no commit, then closed into done with `commit_hash` empty. Nothing on the path refused it:
the executor's "commit required" asks only the move's own `require_commit`, and SYRD-489 only reads a blank hash as
none.

1. The Python validator (`workflow_config.validate`, through `close_commit`) refuses such a document, naming the move;
   the shipped workflow, every composer together, and `switchyard new`'s default document for every role shape it
   offers still validate; a no-code completion into done (`allow_no_code`) and the research stage's
   `close_on_finding` still do.
2. On a board built the production way (schema.sql, migrations, rbac.sql), the database's
   `validate_declared_workflow` refuses the same document with the same words and accepts the good ones, and the
   board's own `apply_workflow` refuses it, changing nothing.
3. With the shipped document applied, a code ticket with no commit is still refused at the close ("commit
   required"), and a `commit_exempt` one -- no code -- still closes by the same `mark_done`.
"""

from __future__ import annotations

import live_boundary  # noqa: E402

live_boundary.isolate()  # no live home, pane store or runtime directory, here or in a child (SYRD-819)

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from router_role_board_test import Board, build_board, fixture, temporary_cluster  # noqa: E402
from scripts import router_onboarding, team_launcher  # noqa: E402
from scripts.ticket_board.documentation_gate import with_documentation_gate  # noqa: E402
from scripts.ticket_board.plan_gate import with_plan_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher  # noqa: E402
from scripts.ticket_board.research_stage import with_research_stage  # noqa: E402
from scripts.ticket_board.router_setup import with_router  # noqa: E402
from scripts.ticket_board import close_commit  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

REFUSAL = "a move into done must require the commit it closes on (require_commit): "
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def shipped() -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


def loose(document: dict) -> dict:
    """`document` with its close requiring no commit: what a hand edit could leave."""
    edited = copy.deepcopy(document)
    for move in edited["transitions"]:
        if move["action"] == "mark_done":
            move["require_commit"] = False
    return edited


def borrowed_name(document: dict) -> dict:
    """`document`'s close renamed close_on_finding, with no commit: what the research exemption must not let through
    on a board that declares no research stage."""
    edited = copy.deepcopy(document)
    for move in edited["transitions"]:
        if move["action"] == "mark_done":
            move.update(action="close_on_finding", require_commit=False)
    return edited


def refusal(document: dict) -> str:
    try:
        validate(copy.deepcopy(document))
    except ValueError as exc:
        return str(exc)
    return ""


def all_composers() -> dict:
    cfg = with_documentation_gate(shipped())
    cfg = with_router(cfg, runtime="claude")
    return with_plan_gate(with_research_stage(with_publisher(cfg), role="app"), owner="main")


def test_python(tmp: Path) -> list[dict]:
    said = refusal(loose(shipped()))
    check(said.startswith(REFUSAL) and "mark_done (director_review -> done)" in said,
          f"Python refuses a close into done without the commit, naming the move: {said!r}")
    try:  # the composers validate as they go, so a refusal of theirs is this check's failure, not a crash
        good, why = [validate(shipped()), validate(all_composers())], ""
    except ValueError as exc:
        good, why = [], str(exc)
    check(good and all(not refusal(document) for document in good),
          f"the shipped workflow and every composer still validate: {why!r}")
    check(any(t["action"] == "close_on_finding" and not t["require_commit"] for t in good[1]["transitions"]),
          "with the research stage, close_on_finding closes a finding without a commit and is accepted")
    # SYRD-865: the exemption is the research stage's alone. With no research stage, a commit-less close named
    # close_on_finding is refused: by the close check itself, and by validate (whose research check also refuses it).
    borrowed = borrowed_name(good[0])
    check(not any(stage["name"] == "research" for stage in borrowed["stages"])
          and close_commit.closes_without_commit(borrowed) == ["close_on_finding (director_review -> done)"]
          and refusal(borrowed),
          f"with no research stage, a close named close_on_finding without a commit is refused: "
          f"{close_commit.closes_without_commit(borrowed)} {refusal(borrowed)!r}")
    defaults = 0
    for implementers in (("main",), ("main", "app", "ops")):
        for audit in ((), ("audit",), ("audit", "inspector")):
            for designer in (False, True):
                plan = team_launcher.build_plan(project="porter", owner_user=team_launcher.current_user_name(),
                                                source_repo=tmp, implementer_roles=implementers,
                                                include_designer=designer, include_audit=bool(audit),
                                                audit_roles=audit or ("audit",))
                panes = team_launcher._default_role_cli_pairs(implementers, include_designer=designer,
                                                              include_audit=bool(audit), audit_roles=audit)
                declared = router_onboarding.declared_by_default(plan, role_clis=panes, repository=tmp,
                                                                 print_func=lambda _line: None).workflow
                defaults += not refusal(declared)
    check(defaults == 12, f"switchyard new's default document validates for every role shape it offers: {defaults}/12")
    # A no-code completion straight into done keeps its own policy, and is not a close that drops a commit.
    no_code = shipped()
    in_progress = next(t for t in no_code["transitions"] if t["from"] == "in_progress")
    no_code["transitions"].append({**in_progress, "to": "done", "action": "finish_without_code", "label": "Finish",
                                   "require_commit": False, "require_reason": True, "allow_no_code": True})
    check(not refusal(no_code), f"a no-code completion into done is accepted: {refusal(no_code)!r}")
    return good


def sql(admin: str, statement: str) -> subprocess.CompletedProcess:
    return subprocess.run(["psql", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", admin], input=statement,
                          capture_output=True, text=True, timeout=120)


def test_database(cluster, good: list[dict]) -> None:
    admin = build_board(cluster, "s864_sql")
    bad = loose(good[0])  # validated by Python first, then edited, so only the database judges it
    asked = sql(admin, f"SELECT ticket_board.validate_declared_workflow($doc${json.dumps(bad)}$doc$::jsonb);")
    check(asked.returncode != 0 and REFUSAL + "mark_done (director_review -> done)" in asked.stderr,
          f"the database's validate_declared_workflow refuses it, naming the move: {asked.stderr[-400:]}")
    # SYRD-865: the database holds the exemption to a board that declares the research stage, too.
    borrowed = borrowed_name(good[0])
    asked = sql(admin, f"SELECT ticket_board.validate_declared_workflow($doc${json.dumps(borrowed)}$doc$::jsonb);")
    check(asked.returncode != 0 and REFUSAL + "close_on_finding (director_review -> done)" in asked.stderr,
          f"with no research stage the database refuses a close named close_on_finding without a commit, naming it: "
          f"{asked.stderr[-400:]}")
    for label, document in (("the shipped workflow", good[0]), ("every composer", good[1])):
        asked = sql(admin, f"SELECT ticket_board.validate_declared_workflow($doc${json.dumps(document)}$doc$::jsonb);")
        check(asked.returncode == 0, f"and accepts {label}: {asked.stderr[-400:]}")


def test_board(cluster, good: list[dict]) -> None:
    board = Board(cluster, "s864_board", shipped())
    try:
        revision = board.sql("SELECT revision FROM ticket_board.workflow_configuration;")
        try:
            board.app.apply_workflow(loose(good[0]), expected_revision=int(revision), dry_run=False,
                                     caller_role="director")
            applied = "applied"
        except Exception as exc:  # noqa: BLE001 - the refusal is the answer
            applied = str(exc)
        check(REFUSAL in applied and board.sql("SELECT revision FROM ticket_board.workflow_configuration;") == revision,
              f"the board's own apply refuses the document and changes nothing: {applied[-300:]}")
        for ticket, exempt in (("PGU-1", False), ("PGU-2", True)):
            fixture.seed_postgres_ticket(board.admin, ticket, title=ticket, state="director_review",
                                         assignee="director", audit_signoff=True, commit_hash="",
                                         commit_exempt=exempt)
        said = board.act("PGU-1", "mark_done", "director")
        check("refused" in said and "commit required" in said["refused"] and board.where("PGU-1")[0] == "director_review",
              f"a code ticket with no commit is refused at the close: {str(said)[:300]}")
        said = board.act("PGU-2", "mark_done", "director")
        check("ok" in said and board.where("PGU-2")[0] == "done",
              f"and a no-code ticket (commit_exempt) still closes by the same mark_done: {str(said)[:300]}")
    finally:
        board.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd864-") as name:
        good = test_python(Path(name))
    with temporary_cluster(prefix="syrd864-", shutdown="immediate") as cluster:
        test_database(cluster, good)
        test_board(cluster, good)
    print(f"close_requires_commit_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
