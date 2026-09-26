#!/usr/bin/env python3
"""SYRD-304: pane rebind's boundary with the launcher it came out of.

`switchyard rebind-workflow-panes` and its planning moved into
`scripts/pane_rebind.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top; the
  launcher imports it at its top.
- Every name the launcher's dispatch and the suites reach as
  `team_launcher.<name>` is still there and is the very same object, whichever
  module is imported first.
- **The shared seams stay the launcher's.** `read_board_workflow_state` is
  patched by the suites, `role_pane_declaration` and `role_runtime_binding`
  are read through the launcher by `workflow_adoption` and `role_command`,
  `presentation_section_for_roles` is the presentation rule, and
  `uid_for_user` is patched by the privileged prologue's fixtures. The moved
  code reads every one of them only as `launcher.<name>`, when it runs.
- A reconciled presentation section is derived by the launcher's
  presentation rule as it is when the rebind runs.

Nothing here touches a board, a configuration or a privileged path.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    '_build_switchyard_rebind_workflow_panes_parser',
    '_reconciled_presentation_section',
    'root_verified_tenant',
    'switchyard_rebind_workflow_panes_command',
)

LAUNCHER_SEAMS = (
    'presentation_section_for_roles',
    'read_board_workflow_state',
    'role_pane_declaration',
    'role_runtime_binding',
    'uid_for_user',
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.pane_rebind; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.pane_rebind'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.pane_rebind", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.pane_rebind")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.pane_rebind as p; "
            f"print(all(getattr(t, n) is getattr(p, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_shared_seams_are_only_read_through_the_launcher() -> None:
    tree = ast.parse((ROOT / "scripts" / "pane_rebind.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(tree)
                   if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in LAUNCHER_SEAMS})
    through = sorted({n.attr for n in ast.walk(tree)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                      and n.value.id == "launcher" and n.attr in LAUNCHER_SEAMS})
    defined = sorted({n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))} & set(LAUNCHER_SEAMS))
    check(defined == [], f"none of the shared seams moved here: {defined}")
    check(bare == [], f"none is read past the launcher: {bare}")
    check(through == sorted(LAUNCHER_SEAMS), f"and every one is read through it: {through}")


def test_a_reconciled_section_uses_the_launchers_presentation_rule_when_it_runs() -> None:
    from scripts import pane_rebind, team_launcher

    marker = {"slot_count": 3, "layouts": {"default": {"0": "syrd-304-rule"}}}
    text = json.dumps({
        "presentation": {"slot_count": 6, "layouts": {"default": {"0": "main"}}},
        "roles": [{"role": "main", "slot": 0}],
    })
    saved = team_launcher.presentation_section_for_roles
    team_launcher.presentation_section_for_roles = lambda roles: json.loads(json.dumps(marker))
    try:
        reconciled = json.loads(pane_rebind._reconciled_presentation_section(text))
    finally:
        team_launcher.presentation_section_for_roles = saved
    check(reconciled["presentation"] == marker,
          f"the section was re-derived by the launcher's rule as patched: {reconciled['presentation']!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"pane_rebind_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
