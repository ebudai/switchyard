#!/usr/bin/env python3
"""SYRD-316: presentation reconnect, attach check and hand-back, against the launcher they came out of.

Reconnecting the presentation after a cutover, asking whether anyone is looking
at it, and handing its window back to the bridge's caller moved into
`scripts/presentation_reconnect.py` unchanged; the layout modes moved into the
leaf `scripts/layout_modes.py`. This pins what makes that safe:

- **The imports point one way:** the leaf imports nothing of Switchyard's; the
  module imports only the leaf; neither imports the launcher at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Rule 6.** The hand-back's `layout` default is the leaf's
  `LAYOUT_MODE_SEPARATE`, which is the launcher's.
- **Rule 21.** Every reference the cutover and the presentation controller
  make to these names goes through the launcher.
- **The patched entry points are still reached:** `launch_project` calls the
  hand-back by the launcher's name, and nothing here calls one past it.
- **What they do is unchanged and asks the launcher when it runs:** the
  wrappers ask the presentation controller and never let a failure out; the
  hand-back writes to the descriptor the bridge names, built from the
  launcher's pane program, titles and schema as patched.

No window, display, tmux or process is touched: the controller is patched,
the descriptor is a pipe this test owns, and paths are temporary.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = {
    "scripts.layout_modes": ('LAYOUT_MODE_AUTO', 'LAYOUT_MODE_CHOICES', 'LAYOUT_MODE_SEPARATE', 'LAYOUT_MODE_VIEWER'),
    "scripts.presentation_reconnect": (
        'hand_presentation_back_to_the_caller', 'PRESENTATION_HANDOFF_FD_ENV', 'presentation_is_attached',
        'presentation_slot_titles', 'reconnect_presentation', 'render_presentation_handoff',
    ),
}
READ_ELSEWHERE = {
    "role_identity_cutover": ("reconnect_presentation", "presentation_is_attached"),
    "presentation_controller": ("PRESENTATION_HANDOFF_FD_ENV", "render_presentation_handoff",
                                "presentation_slot_titles", "LAYOUT_MODE_VIEWER", "LAYOUT_MODE_SEPARATE"),
}
PATCHED_ENTRY_POINTS = ("reconnect_presentation", "presentation_is_attached", "hand_presentation_back_to_the_caller")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def loaded_after(module: str) -> list[str]:
    result = python(
        f"import sys, {module}; "
        f"print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != {module!r}))"
    )
    check(result.returncode == 0, f"{module} imports on its own: {result.stderr[-600:]}")
    return eval(result.stdout.strip())


class patched:
    """Rebind attributes of one module for one block, as the suites do."""

    def __init__(self, module: object, **values: object) -> None:
        self.module = module
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.module, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.module, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.module, name, value)


def test_the_imports_point_one_way() -> None:
    check(loaded_after("scripts.layout_modes") == [], "the layout leaf loads nothing of Switchyard's")
    check(loaded_after("scripts.presentation_reconnect") == ["scripts.layout_modes"],
          "and the module loads only the leaf, never the launcher or the presentation controller")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for module, names in EXPORTED.items():
        for order in ((module, "scripts.team_launcher"), ("scripts.team_launcher", module)):
            result = python(
                "import importlib; "
                f"[importlib.import_module(m) for m in {order!r}]; "
                f"import scripts.team_launcher as t, {module} as m; "
                f"print(all(getattr(t, n) is getattr(m, n) for n in {names!r}))"
            )
            check(result.stdout.strip() == "True",
                  f"{' then '.join(order)}: {module}'s names are the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_layout_default_is_the_launchers_object() -> None:
    from scripts import layout_modes, presentation_reconnect, team_launcher

    default = inspect.signature(presentation_reconnect.hand_presentation_back_to_the_caller).parameters["layout"].default
    check(default is layout_modes.LAYOUT_MODE_SEPARATE is team_launcher.LAYOUT_MODE_SEPARATE,
          "the hand-back defaults to the one LAYOUT_MODE_SEPARATE the leaf and the launcher share")
    check(layout_modes.LAYOUT_MODE_CHOICES == {"auto", "separate", "viewer"}, "and the modes are unchanged")


def test_every_reader_reaches_its_names_through_the_launcher() -> None:
    for module, names in READ_ELSEWHERE.items():
        tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        for name in names:
            check(any(name in exported for exported in EXPORTED.values()), f"{name} is exported")
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")


def test_the_patched_entry_points_are_called_by_the_launchers_name() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(launcher_tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", ""))
             == "hand_presentation_back_to_the_caller"]
    check(len(calls) == 2 and all(isinstance(n.func, ast.Name) for n in calls),
          "launch_project hands the window back at its two baseline sites, by the launcher's patchable name")
    moved = ast.parse((ROOT / "scripts" / "presentation_reconnect.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in PATCHED_ENTRY_POINTS})
    check(bare == [], f"and nothing here calls an entry point past that patch: {bare}")


def test_the_wrappers_ask_the_controller_and_never_raise() -> None:
    from scripts import presentation_controller, presentation_reconnect

    config, path = SimpleNamespace(project="p316"), Path("/nonexistent/syrd-316/config.json")
    asked: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("a disabled presentation must not be reconnected or inspected")

    def failing(*args: object, **kwargs: object) -> object:
        asked.append("asked")
        raise RuntimeError("syrd-316 slot gone")

    with patched(presentation_controller, presentation_enabled=lambda config, config_path: False,
                 reconnect_display_slots=refuse, presentation_window_attached=refuse):
        disabled = (presentation_reconnect.reconnect_presentation(config, config_path=path, runner=refuse),
                    presentation_reconnect.presentation_is_attached(config, config_path=path, runner=refuse))
    check(disabled == ([], False), f"no presentation configured: nothing to reconnect, and not attached: {disabled}")

    with patched(presentation_controller, presentation_enabled=lambda config, config_path: True,
                 reconnect_display_slots=failing, presentation_window_attached=failing):
        failed = (presentation_reconnect.reconnect_presentation(config, config_path=path, runner=refuse),
                  presentation_reconnect.presentation_is_attached(config, config_path=path, runner=refuse))
    check(failed == (["syrd-316 slot gone"], False) and asked == ["asked", "asked"],
          f"a failure is reported, never raised, and an unreadable window is not attached: {failed}")

    with patched(presentation_controller, presentation_enabled=lambda config, config_path: True,
                 reconnect_display_slots=lambda config, config_path, runner: asked.append("reconnected"),
                 presentation_window_attached=lambda config, config_path, runner: True):
        ok = (presentation_reconnect.reconnect_presentation(config, config_path=path, runner=refuse),
              presentation_reconnect.presentation_is_attached(config, config_path=path, runner=refuse))
    check(ok == ([], True) and asked[-1] == "reconnected", f"and a working one reconnects and reports attached: {ok}")


def test_slot_titles_ask_the_launcher_when_they_run() -> None:
    from scripts import presentation_reconnect, team_launcher

    roles = [SimpleNamespace(role="main", detached=False, slot=1), SimpleNamespace(role="bg", detached=True, slot=0)]
    with patched(team_launcher, project_window_title=lambda config: "P316",
                 pane_split_title=lambda config, role: f"P316 {role.role}"):
        titles = presentation_reconnect.presentation_slot_titles(SimpleNamespace(roles=roles), 3)
    check(titles == ["P316", "P316 main", "P316"],
          f"an occupied slot takes the launcher's pane title, an empty or detached one the window's: {titles}")


def test_the_hand_back_writes_to_the_bridges_descriptor() -> None:
    from scripts import presentation_reconnect, team_launcher

    env = presentation_reconnect.PRESENTATION_HANDOFF_FD_ENV
    check(env == "SWITCHYARD_PRESENTATION_HANDOFF_FD", f"the bridge's variable is unchanged: {env}")
    config = SimpleNamespace(project="p316", roles=[])
    said: list[str] = []
    saved = os.environ.pop(env, None)
    try:
        check(presentation_reconnect.hand_presentation_back_to_the_caller(
            config, slot_count=2, slot_titles=["a", "b"], window_title="W", print_func=said.append) is False
              and said == [], "without a descriptor from the bridge nothing is handed back")
        with tempfile.TemporaryDirectory(prefix="syrd316.") as raw:
            launcher_path = Path(raw) / "bin" / "team-launcher"
            read_end, write_end = os.pipe()
            os.environ[env] = str(write_end)
            try:
                with patched(team_launcher, switchyard_pane_launcher_for=lambda config: launcher_path,
                             project_window_title=lambda config: "P316 window",
                             pane_split_title=lambda config, role: "unused"):
                    handed = presentation_reconnect.hand_presentation_back_to_the_caller(
                        config, slot_count=2, print_func=said.append)
                with os.fdopen(read_end, encoding="utf-8") as handle:
                    payload = json.loads(handle.read())
            finally:
                os.environ.pop(env, None)
            expected_program = team_launcher.pane_window_program(launcher_path)
    finally:
        if saved is not None:
            os.environ[env] = saved
    check(handed is True, "with one it is handed back")
    check(payload == {"schema": team_launcher.PRESENTATION_HANDOFF_SCHEMA, "project": "p316", "layout": "separate",
                      "slot_count": 2, "pane_program": str(expected_program),
                      "slot_titles": ["P316 window", "P316 window"], "window_title": "P316 window"},
          f"built from the launcher's pane program, titles and schema, as patched: {payload!r}")
    check(said == ["switchyard: p316's panes are up; its window opens in the session that ran this, "
                   "which owns the screen."], f"and it says so: {said!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"presentation_reconnect_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
