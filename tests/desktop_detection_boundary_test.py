#!/usr/bin/env python3
"""SYRD-320: whose desktop and which layout, against the launcher they came out of.

The desktop-account and layout-mode detection moved into
`scripts/desktop_detection.py` unchanged. This pins what makes that safe:

- **The imports point one way:** the module imports only the layout-mode
  leaf, never the launcher at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Rule 21.** Every reference the presentation controller, the GUI window
  launch and the agy credential seeding make to these names goes through the
  launcher.
- **Rule 24.** The suites rebind `detected_invoking_desktop`,
  `presentation_gui_user` and `default_gui_user` on the launcher. The
  launcher calls them by its own names, and `resolve_layout_mode` asks the
  launcher's `detected_invoking_desktop` when it runs, as rebound.
- **What it decides is unchanged:** a recorded policy's account wins and root
  never does; the environment is consulted in its order; a desktop is read
  from the environment, then from `loginctl` through the runner given; `auto`
  is `separate` on KDE and `viewer` elsewhere.

No account, session or `loginctl` is touched: the environment is patched or
injected, the current user and `_env_first` are patched on the launcher, and
the runner is a fake.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'GUI_USER_ENV', 'LEGACY_GUI_USER_ENV', 'default_gui_user', '_desktop_from_loginctl_output', '_desktop_is_kde',
    'detected_invoking_desktop', 'pinned_presentation_gui_user', 'presentation_gui_user', 'resolve_layout_mode',
)
READ_ELSEWHERE = {
    "presentation_controller": ("presentation_gui_user",),
    "gui_window_launch": ("default_gui_user", "GUI_USER_ENV"),
    "agy_credential": ("default_gui_user",),
}
PATCHED_SEAMS = ("detected_invoking_desktop", "presentation_gui_user", "default_gui_user")
#: The baseline call sites: replace_presentation_window_command (default_gui_user,
#: twice), legacy_presentation_refusal (presentation_gui_user), launch_project
#: (resolve_layout_mode, twice) and switchyard_new_command (once).
LAUNCHER_CALLS = {"default_gui_user": 2, "presentation_gui_user": 1, "resolve_layout_mode": 3}
#: Modules those launcher callers moved to (SYRD-328 moved legacy_presentation_refusal,
#: SYRD-329 replace_presentation_window_command),
#: whose calls must still go through the launcher; the totals are the baseline's.
#: SYRD-341 moved launch_project's P6 (the dry run's layout-mode resolution) to launch_phases.
#: SYRD-374 moved `switchyard new`'s tail, with its `resolve_layout_mode` call, into new_project_phases.
MOVED_CALLERS = ("legacy_presentation.py", "presentation_window_replacement.py", "launch_phases.py", "new_project_phases.py")
ACCOUNT_VARIABLES = ("TEAM_LAUNCHER_GUI_USER", "PGU_TEAM_LAUNCHER_GUI_USER", "SUDO_USER",
                     "SWITCHYARD_TENANT_CONTROL_CALLER")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


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


class environment:
    """Exactly these account variables for one block, and none of the others."""

    def __init__(self, **values: str) -> None:
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: os.environ.get(name) for name in ACCOUNT_VARIABLES}
        for name in ACCOUNT_VARIABLES:
            os.environ.pop(name, None)
        os.environ.update(self.values)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_the_imports_point_one_way() -> None:
    result = python(
        "import sys, scripts.desktop_detection; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.desktop_detection'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "['scripts.layout_modes']",
          f"and loads only the layout-mode leaf, never the launcher: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.desktop_detection", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.desktop_detection")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.desktop_detection as d, scripts.layout_modes as l; "
            f"print(all(getattr(t, n) is getattr(d, n) for n in {EXPORTED!r}), "
            "d.LAYOUT_MODE_VIEWER is l.LAYOUT_MODE_VIEWER is t.LAYOUT_MODE_VIEWER)"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_every_reader_reaches_its_names_through_the_launcher() -> None:
    for module, names in READ_ELSEWHERE.items():
        tree = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        for name in names:
            check(name in EXPORTED, f"{name} is exported")
            uses = [n for n in ast.walk(tree) if (isinstance(n, ast.Attribute) and n.attr == name)
                    or (isinstance(n, ast.Name) and n.id == name)]
            through = [n for n in uses if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id in ("launcher", "team_launcher")]
            check(uses and through == uses, f"{module} reads {name} through the launcher, and only there")


def test_the_patched_seams_are_reached_through_the_launcher() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    moved_trees = [ast.parse((ROOT / "scripts" / name).read_text(encoding="utf-8")) for name in MOVED_CALLERS]
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        moved_calls = [n for tree in moved_trees for n in ast.walk(tree)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(moved_calls) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id in ("launcher", "team_launcher") for n in moved_calls),
              f"{name} is called at its {count} baseline sites, by the launcher's own name there and through "
              "the launcher where a caller moved")
    moved = ast.parse((ROOT / "scripts" / "desktop_detection.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in PATCHED_SEAMS + ("_env_first", "current_user_name")})
    check(bare == [], f"and nothing here reaches a rebound name past the launcher: {bare}")


def test_whose_desktop_a_projects_window_belongs_to() -> None:
    from scripts import desktop_detection, team_launcher

    def config(policy: object = None, owner: str = "syrd320-owner") -> SimpleNamespace:
        return SimpleNamespace(desktop_access=policy, run_as_user=owner)

    wayland = {"mode": "wayland", "gui_user": "syrd320-pinned"}
    with patched(team_launcher, current_user_name=lambda: "syrd320-me"):
        with environment(TEAM_LAUNCHER_GUI_USER="syrd320-configured", SUDO_USER="syrd320-sudo"):
            pinned = desktop_detection.presentation_gui_user(config(wayland))
            configured = desktop_detection.presentation_gui_user(config())
            not_wayland = desktop_detection.presentation_gui_user(config({"mode": "x11", "gui_user": "x"}))
        with environment(TEAM_LAUNCHER_GUI_USER="root", PGU_TEAM_LAUNCHER_GUI_USER="syrd320-legacy"):
            legacy = desktop_detection.presentation_gui_user(config())
        with environment(SUDO_USER="root", SWITCHYARD_TENANT_CONTROL_CALLER="syrd320-bridged"):
            bridged = desktop_detection.presentation_gui_user(config())
        with environment(SUDO_USER="root"):
            owner = desktop_detection.presentation_gui_user(config())
            me = desktop_detection.presentation_gui_user(config(owner=""))
            root_policy = desktop_detection.presentation_gui_user(config({"mode": "wayland", "gui_user": "root"}))
    check(pinned == "syrd320-pinned", f"a recorded Wayland policy's account wins over the environment: {pinned}")
    check(configured == "syrd320-configured" and not_wayland == "syrd320-configured",
          f"then the configured account, and only a Wayland policy pins: {configured} {not_wayland}")
    check(legacy == "syrd320-legacy" and bridged == "syrd320-bridged",
          f"root in the environment is skipped for the next name in order: {legacy} {bridged}")
    check(owner == "syrd320-owner" and me == "syrd320-me" and root_policy == "syrd320-owner",
          f"then the owner, then the launcher's current user; a policy naming root pins nothing: "
          f"{owner} {me} {root_policy}")


def test_the_default_desktop_account_reads_the_launchers_environment() -> None:
    from scripts import desktop_detection, team_launcher

    asked: list[tuple[str, ...]] = []
    answers = {"TEAM_LAUNCHER_GUI_USER": "", "SUDO_USER": "", "SWITCHYARD_TENANT_CONTROL_CALLER": "syrd320-bridged"}

    def env_first(*names: str) -> str:
        asked.append(names)
        return answers.get(names[0], "")

    with patched(team_launcher, _env_first=env_first, current_user_name=lambda: "syrd320-me"):
        bridged = desktop_detection.default_gui_user()
        answers["SWITCHYARD_TENANT_CONTROL_CALLER"] = ""
        me = desktop_detection.default_gui_user()
        answers["SUDO_USER"] = "syrd320-sudo"
        sudo = desktop_detection.default_gui_user()
    check(asked[:3] == [("TEAM_LAUNCHER_GUI_USER", "PGU_TEAM_LAUNCHER_GUI_USER"), ("SUDO_USER",),
                        ("SWITCHYARD_TENANT_CONTROL_CALLER",)],
          f"the configured names, SUDO_USER and the bridge are asked of the launcher's _env_first in order: {asked}")
    check((bridged, me, sudo) == ("syrd320-bridged", "syrd320-me", "syrd320-sudo"),
          f"and the first answer wins, the current user last: {bridged} {me} {sudo}")


def test_the_invoking_desktop_is_read_without_touching_a_session() -> None:
    from scripts import desktop_detection

    ran: list[list[str]] = []

    def loginctl(display: str, desktop: str, code: int = 0):
        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran.append(list(args))
            out = display if args[1] == "show-user" else desktop
            return subprocess.CompletedProcess(args, code, out, "")
        return run

    def no_runner(args: list[str], **kwargs: object) -> object:
        raise AssertionError(f"the environment answered; loginctl must not run: {args!r}")

    from_env = desktop_detection.detected_invoking_desktop(environ={"XDG_CURRENT_DESKTOP": " GNOME "}, runner=no_runner)
    kde_flag = desktop_detection.detected_invoking_desktop(environ={"KDE_FULL_SESSION": "true"}, runner=no_runner)
    nobody = desktop_detection.detected_invoking_desktop(environ={}, runner=no_runner)
    session = desktop_detection.detected_invoking_desktop(
        environ={"SUDO_USER": "syrd320-human", "USER": "syrd320-owner"},
        runner=loginctl("c7\n", "Desktop=KDE\n"))
    bridged = desktop_detection.detected_invoking_desktop(
        environ={"SWITCHYARD_TENANT_CONTROL_CALLER": "syrd320-bridged", "USER": "syrd320-owner"},
        runner=loginctl("", "Desktop=KDE\n"))
    failed = desktop_detection.detected_invoking_desktop(environ={"USER": "syrd320-owner"},
                                                          runner=loginctl("c9\n", "", code=1))
    check(from_env == "GNOME" and kde_flag == "KDE" and nobody == "",
          f"the environment answers first, and no user means no desktop: {from_env} {kde_flag} {nobody!r}")
    check(session == "KDE" and ran[:2] == [
        ["loginctl", "show-user", "syrd320-human", "-p", "Display", "--value"],
        ["loginctl", "show-session", "c7", "-p", "Desktop"]],
          f"otherwise the invoking human's session, not the owner's, through the given runner: {ran[:2]}")
    check(bridged == "" and ran[2] == ["loginctl", "show-user", "syrd320-bridged", "-p", "Display", "--value"]
          and failed == "", f"a bridged caller is asked about; no display or a failure is no desktop: {ran[2:]}")
    check(desktop_detection._desktop_from_loginctl_output("Desktop=KDE\nOther=x\nplasma\n") == "KDE:plasma",
          "the loginctl answer keeps only desktop values")


def test_the_layout_mode_asks_the_launchers_desktop_when_it_runs() -> None:
    from scripts import desktop_detection, team_launcher

    def no_runner(args: list[str], **kwargs: object) -> object:
        raise AssertionError(f"a patched desktop must not reach loginctl: {args!r}")

    with patched(team_launcher, detected_invoking_desktop=lambda **kwargs: "X-Cinnamon"):
        cinnamon = desktop_detection.resolve_layout_mode("auto", environ={"XDG_CURRENT_DESKTOP": "KDE"},
                                                         runner=no_runner)
    with patched(team_launcher, detected_invoking_desktop=lambda **kwargs: "ubuntu:KDE"):
        kde = desktop_detection.resolve_layout_mode("auto", runner=no_runner)
    with patched(team_launcher, detected_invoking_desktop=lambda **kwargs: ""):
        headless = desktop_detection.resolve_layout_mode("auto", runner=no_runner)
    explicit = desktop_detection.resolve_layout_mode("separate", runner=no_runner)
    try:
        desktop_detection.resolve_layout_mode("tiled", runner=no_runner)
        refused = ""
    except SystemExit as exc:
        refused = str(exc)
    check(cinnamon == "viewer", "the launcher's rebound detected_invoking_desktop decides auto, not the environ")
    check(kde == "separate" and headless == "viewer" and explicit == "separate",
          f"KDE in any position is separate, no desktop is viewer, an explicit mode stands: {kde} {headless}")
    check(refused == "unknown layout mode: tiled", f"an unknown mode is refused: {refused!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"desktop_detection_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
