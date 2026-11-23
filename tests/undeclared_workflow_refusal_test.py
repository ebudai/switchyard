#!/usr/bin/env python3
"""SYRD-860: the workflow composers on a board with no declared workflow, and outside a role pane.

A tenant provisioned before `switchyard new` declared a workflow keeps its workflow as table rows, and every
composer refused it with "no workflow configuration is active for this board", naming no way forward. Run outside
a role pane, `research-stage enable --dry-run` ended in a traceback (`ValueError: caller_role must be non-empty`).

Through `workflow_manage.main`, the command every composer runs, with the board's `/api/workflow` stood in:
1. on a board that declares none, each composer refuses in one line, naming the adopt step for this project
   (`switchyard adopt-workflow <project>`, then `migrate-workflow`, then the Director's `finish-upgrade`), taken
   from the launcher config's name;
2. on a board that declares one -- the workflow `router_onboarding.declared_by_default` gives a new tenant --
   with no caller role, it refuses in one line, saying to run it from the Director's pane, and never reaches the
   write client.
"""

from __future__ import annotations

import live_boundary  # noqa: E402

live_boundary.isolate()  # no live home, pane store or runtime directory, here or in a child (SYRD-819)

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import router_onboarding, team_launcher, workflow_manage  # noqa: E402

CHECKS = 0
#: Each composer `switchyard <command> enable` runs, with what it needs besides a document.
COMPOSERS = (("enable-research-stage", ["--role", "main"]), ("enable-plan-gate", ["--owner", "main"]),
             ("enable-router", ["--runtime", "claude"]), ("enable-documentation-gate", []),
             ("enable-publisher", []), ("add-design-stage", []),
             ("set-role-prompt", ["--role", "main", "--prompt", "Hello."]))


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def run(argv: list[str], workflow: dict) -> tuple[object, str]:
    """`workflow_manage.main(argv)` against a board whose `/api/workflow` answers `workflow`: (exit, everything said)."""
    saved = workflow_manage.urllib.request.urlopen
    workflow_manage.urllib.request.urlopen = lambda url, *a, **k: Answer(json.dumps(workflow).encode())
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(said):
            try:
                code = workflow_manage.main(argv)
            except SystemExit as exc:
                code = exc.code
    except Exception as exc:  # a traceback is the failure this suite is about
        code = f"raised {type(exc).__name__}: {exc}"
    finally:
        workflow_manage.urllib.request.urlopen = saved
    return code, said.getvalue()


def test_role_shapes(tmp: Path) -> None:
    """Every role shape `new` offers composes and validates -- Audit's crash was an inspector with no designer: the
    shipped inspector's template is the designer, which such a tenant does not run."""
    for implementers in (("main",), ("main", "app", "ops")):
        for audit in ((), ("audit",), ("audit", "inspector")):
            for designer in (False, True):
                label = f"implementers {implementers}, audit {audit or 'none'}, designer {designer}"
                plan = team_launcher.build_plan(project="porter", owner_user=team_launcher.current_user_name(),
                                                source_repo=tmp / "source", implementer_roles=implementers,
                                                include_designer=designer, include_audit=bool(audit),
                                                audit_roles=audit or ("audit",))
                panes = team_launcher._default_role_cli_pairs(implementers, include_designer=designer,
                                                              include_audit=bool(audit), audit_roles=audit)
                try:
                    declared = router_onboarding.declared_by_default(plan, role_clis=panes, repository=tmp / "repo",
                                                                     print_func=lambda _line: None).workflow
                except BaseException as exc:  # a refusal or a crash is the failure
                    declared, why = None, f"{type(exc).__name__}: {exc}"
                else:
                    why = ""
                names = {role["name"] for role in (declared or {}).get("roles", [])}
                check(declared and all(role.get("template_role") in (None, *names) for role in declared["roles"]),
                      f"{label}: declares a workflow whose every template role it runs: {why}")


def main() -> int:
    os.environ.pop("TICKET_BOARD_CALLER_ROLE", None)
    os.environ.pop("PGU_TICKET_BOARD_CALLER_ROLE", None)
    with tempfile.TemporaryDirectory(prefix="syrd860-") as name:
        tmp = Path(name)
        (tmp / "source").mkdir()
        (tmp / "repo").mkdir()
        (tmp / "out").mkdir()
        plan = team_launcher.build_plan(project="porter", owner_user=team_launcher.current_user_name(),
                                        source_repo=tmp / "source", implementer_roles=("app", "main"))
        config = team_launcher.write_new_project_launcher_artifacts(
            plan, tmp / "out", repository=tmp / "repo", implementer_roles=("app", "main"), print_func=lambda _l: None)
        check(config.name == "porter.json", f"the launcher config is named for its project: {config}")
        board = ["--board-url", "http://127.0.0.1:1", "--config", str(config), "--dry-run"]

        # 1. No declared workflow: one line, naming the adopt step for this project.
        for operation, extra in COMPOSERS:
            code, said = run([operation, *board, *extra], {"revision": 0, "document": None})
            check(code == 2 and "Traceback" not in said and "has no declared workflow" in said
                  and "`switchyard adopt-workflow porter`" in said
                  and "`pkexec switchyard migrate-workflow porter --apply` hands it to the Director" in said
                  and "`switchyard finish-upgrade porter`, run from the Director's pane, installs it" in said,
                  f"{operation} on an undeclared board names the step that gives it one: {code} {said[-500:]}")

        # 2. A declared workflow -- a new tenant's -- and no role pane: one line, never the write client.
        said: list[str] = []
        declared = router_onboarding.declared_by_default(
            plan, role_clis=team_launcher._default_role_cli_pairs(("app", "main"), include_designer=True,
                                                                  include_audit=True),
            repository=tmp / "repo", print_func=said.append)
        check(declared.workflow and plan.workflow is None
              and any("starts with a declared workflow, the one its plan describes." in line for line in said),
              f"a new tenant's plan declares the workflow it describes: {said}")
        check(router_onboarding.declared_by_default(declared, role_clis=(), repository=tmp / "repo") is declared,
              "and one that already declares a workflow keeps it")
        for operation, extra in COMPOSERS[:2]:
            code, said = run([operation, *board, *extra], {"revision": 1, "document": declared.workflow})
            check(code == 2 and "Traceback" not in said and "caller_role must be non-empty" not in said
                  and "run it from the Director's pane" in said,
                  f"{operation} outside a role pane refuses in one line: {code} {said[-500:]}")
        test_role_shapes(tmp)
    print(f"undeclared_workflow_refusal_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
