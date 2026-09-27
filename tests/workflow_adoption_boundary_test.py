#!/usr/bin/env python3
"""SYRD-303: workflow adoption and migration's boundary with the launcher they came out of.

Adoption, migration and the root-vouched workflow handoff moved into
`scripts/workflow_adoption.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's dispatch and `finish-upgrade` path and the suites
  reach as `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **The patched names are still reached.** The suites patch
  `install_handed_off_workflow` and `read_board_workflow_state` on the
  launcher. `finish-upgrade`, in the launcher, calls the former by the
  launcher's own name; the moved code calls either only through the launcher.
- **The handoff is still vouched for by the launcher's root checks.** Reading
  a handoff asks the launcher where root's provision root is and has the
  launcher's no-follow, root-owned reader open it, when it runs.

Nothing here reads or writes a real handoff, board or privileged path: the
launcher facilities the reads go through are patched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    '_build_switchyard_adopt_workflow_parser',
    '_build_switchyard_migrate_workflow_parser',
    'effective_workflow_document',
    'install_handed_off_workflow',
    'plan_workflow_migration',
    'propose_legacy_workflow_adoption',
    'publish_workflow_handoff',
    'read_workflow_handoff',
    'switchyard_adopt_workflow_command',
    'switchyard_migrate_workflow_command',
    'workflow_handoff_path',
    'WorkflowMigration',
    'write_workflow_record',
)

PATCHED_ON_THE_LAUNCHER = ('install_handed_off_workflow', 'read_board_workflow_state')


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.workflow_adoption; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.workflow_adoption'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.workflow_adoption", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.workflow_adoption")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.workflow_adoption as w; "
            f"print(all(getattr(t, n) is getattr(w, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def _loads(path: Path, names: tuple[str, ...]) -> tuple[list[str], list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in names})
    through = sorted({n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                      and n.value.id == "launcher" and n.attr in names})
    return bare, through


def test_the_patched_names_are_reached_where_the_suites_patch_them() -> None:
    bare, through = _loads(ROOT / "scripts" / "workflow_adoption.py", PATCHED_ON_THE_LAUNCHER)
    check(bare == [], f"the moved code reads no patched name past the launcher: {bare}")
    check(through == ["read_board_workflow_state"], f"and reads the board state through it: {through}")
    calls = [
        node for node in ast.walk(ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "install_handed_off_workflow"
    ]
    # SYRD-352 moved `finish_upgrade_command` into director_upgrade, which reads it through the launcher.
    moved = [
        node for node in ast.walk(ast.parse((ROOT / "scripts" / "director_upgrade.py").read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "install_handed_off_workflow"
    ]
    # `finish_upgrade_command` and `_finish_upgrade_preview`, as at the baseline.
    check(len(calls) + len(moved) == 2 and all(isinstance(node.func, ast.Name) for node in calls)
          and all(isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "launcher" for node in moved),
          "finish-upgrade installs a handed-off workflow only by the launcher's own (patchable) name: "
          f"{[ast.unparse(node.func) for node in calls + moved]}")


def test_reading_a_handoff_goes_through_the_launchers_root_checks() -> None:
    from scripts import team_launcher, workflow_adoption

    opened: list[tuple[Path, dict[str, object]]] = []

    def refuse(path: Path, **kwargs: object) -> tuple[None, str]:
        opened.append((path, kwargs))
        return None, f"syrd-303 refused {path}"

    saved = (team_launcher.switchyard_privileged_provision_root, team_launcher.read_plan_no_follow)
    team_launcher.switchyard_privileged_provision_root = lambda: Path("/nonexistent/syrd-303/provision")
    team_launcher.read_plan_no_follow = refuse
    try:
        handoff, problem = workflow_adoption.read_workflow_handoff("syrd303", config_path=Path("/nonexistent/syrd-303.json"))
    finally:
        team_launcher.switchyard_privileged_provision_root, team_launcher.read_plan_no_follow = saved
    expected = Path("/nonexistent/syrd-303/workflow-handoff/syrd303.json")
    check(handoff is None and problem == f"syrd-303 refused {expected}",
          f"the handoff was located by the launcher's provision root and refused by its reader: {problem!r}")
    check(opened == [(expected, {"require_root_owned": True, "require_single_link": True})],
          f"and only as root-owned, single-link: {opened!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"workflow_adoption_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
