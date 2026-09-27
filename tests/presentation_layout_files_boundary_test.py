#!/usr/bin/env python3
"""SYRD-319: the presentation layout file, against the launcher it came out of.

Where the layout file goes, who owns it and what it says moved into
`scripts/presentation_layout_files.py` unchanged. This pins what makes that
safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every name the launcher and the modules that read through it reach as
  `team_launcher.<name>` is still there and is the very same object,
  whichever module is imported first.
- **Rule 21.** Every reference the presentation controller, role runtime, the
  desktop half, the layout writer and presentation reconnect make to these
  names goes through the launcher.
- **Rule 24.** The suites rebind `materialize_layout` and
  `default_layout_output_path`, and our own boundary tests rebind
  `desktop_state_dir` and `pane_split_title`, all on the launcher. The
  launcher calls them by its own names, and nothing here calls one past it:
  a rebinding reaches the moved callers as it reached the baseline's.
- **What it does is unchanged:** the layout's titles and inert commands, the
  six-pane limit, where a desktop account's copy goes and where it may not,
  and the owner repair's argv.

No terminal, account or ownership is touched: the launcher's lookups are
patched, the runner is a recorder, and every path is temporary.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = (
    'chown_layout_output_args', 'default_layout_output_path', 'desktop_layout_destination_problem',
    'desktop_presentation_layout_path', 'desktop_state_dir', 'ensure_layout_output_owner', 'inert_pane_command',
    'materialize_layout', 'pane_split_title', 'role_display_name',
)
READ_ELSEWHERE = {
    "presentation_controller": ("inert_pane_command", "default_layout_output_path",
                                "desktop_presentation_layout_path", "ensure_layout_output_owner"),
    "role_runtime": ("default_layout_output_path",),
    "desktop_presentation": ("desktop_state_dir",),
    "desktop_layout_writer": ("desktop_layout_destination_problem",),
    "presentation_reconnect": ("pane_split_title",),
}
PATCHED_SEAMS = ("materialize_layout", "default_layout_output_path", "desktop_state_dir", "pane_split_title")
#: The call sites of the seams outside this module: launch_project,
#: replace_presentation_window_command and _desktop_state_root_problem in the
#: launcher, and presentation_window_processes and desktop_presentation_windows,
#: which SYRD-321 moved to presentation_windows, where they still call through
#: the launcher.
LAUNCHER_CALLS = {"materialize_layout": 2, "default_layout_output_path": 3, "desktop_state_dir": 2}
#: Modules the launcher's callers moved to, whose calls must go through it.
MOVED_CALLERS = ("presentation_windows.py",)


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


def _role(role: str, slot: int | None, **extra: object) -> SimpleNamespace:
    return SimpleNamespace(role=role, slot=slot, detached=extra.pop("detached", False),
                           presentation_label=extra.pop("presentation_label", ""), workdir=f"/nonexistent/{role}",
                           **extra)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.presentation_layout_files; "
        "print(sorted(m for m in sys.modules if m.startswith('scripts.') and m != 'scripts.presentation_layout_files'))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module, the launcher least of all: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.presentation_layout_files", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.presentation_layout_files")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.presentation_layout_files as p; "
            f"print(all(getattr(t, n) is getattr(p, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
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
    moved = ast.parse((ROOT / "scripts" / "presentation_layout_files.py").read_text(encoding="utf-8"))
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in PATCHED_SEAMS})
    check(bare == [], f"and nothing here calls a rebound seam past the launcher: {bare}")


def test_the_layout_is_written_from_the_launchers_lookups() -> None:
    from scripts import presentation_layout_files, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd319.") as raw:
        root = Path(raw)
        template = root / "template.json"
        template.write_text(json.dumps({"Orientation": "Horizontal",
                                        "Widgets": [{"leaf": 0}, {"leaf": 1}, {"leaf": 2}]}), encoding="utf-8")
        config = SimpleNamespace(project="p319", layout=template, roles=[
            _role("main", 0), _role("review", 2, presentation_label="Reviewer"), _role("bg", 1, detached=True),
            _role("broken", 1)])
        commands: list[tuple[object, ...]] = []
        with patched(team_launcher,
                     _layout_leaves=lambda layout: layout["Widgets"],
                     project_window_title=lambda config: "P319 window",
                     pane_window_program=lambda script: Path("/opt/syrd319/switchyard-pane-window"),
                     pane_command_args=lambda project, role, **kwargs: (
                         commands.append((project, role.role, kwargs["run_as_user"], kwargs["skip_launcher_check"]))
                         or ["attach", role.role]),
                     role_run_as_user=lambda config, role: f"{role.role}-account",
                     failed_role_command=lambda role, reason, *, window_title: f"failed {role.role}: {reason}",
                     _konsole_command=lambda argv: " ".join(argv),
                     pane_split_title=lambda config, role: f"patched {role.role}"):
            output = root / "out" / "p319-team-layout.json"
            written = presentation_layout_files.materialize_layout(
                config, config_path=root / "p319.json", mode="attach", script_path=root / "team-launcher",
                output_path=output, failed_roles={"broken": "no CLI"})
            layout = json.loads(output.read_text(encoding="utf-8"))
        too_many = SimpleNamespace(project="p319", layout=template,
                                   roles=[_role(f"r{i}", i) for i in range(team_launcher.MAX_VISIBLE_PANES_PER_WINDOW + 1)])
        try:
            presentation_layout_files.materialize_layout(
                too_many, config_path=root / "p319.json", mode="attach", script_path=root / "team-launcher",
                output_path=root / "never.json")
            refused = ""
        except SystemExit as exc:
            refused = str(exc)
        never = (root / "never.json").exists()
    check(written == output, "the layout is written where it was asked to go")
    main, broken, review = layout["Widgets"]
    check(main == {"leaf": 0, "Command": "/opt/syrd319/switchyard-pane-window --window-title P319 window --title "
                   "patched main attach main", "WorkingDirectory": "/nonexistent/main", "Title": "patched main"},
          f"a role's leaf is its inert command, titled by the launcher's patched pane_split_title: {main}")
    check(broken == {"leaf": 1, "Command": "failed broken: no CLI", "WorkingDirectory": str(Path.home()),
                     "Title": "patched broken"}, f"a failed role gets the launcher's failure command: {broken}")
    check(review["Title"] == "patched review", f"every leaf's title comes from the launcher's name: {review}")
    check(commands == [("p319", "main", "main-account", True), ("p319", "review", "review-account", True)],
          f"each visible role's command is asked of the launcher, as its own account: {commands}")
    check(refused.startswith("team-launcher: p319 has 7 visible roles; at most 6 panes") and not never,
          f"more visible roles than one window holds is refused before anything is written: {refused!r}")


def test_the_titles_read_as_a_person_reads_them() -> None:
    from scripts import presentation_layout_files, team_launcher

    config = SimpleNamespace(project="p319")
    with patched(team_launcher, project_window_title=lambda config: "P319 window"):
        titles = [presentation_layout_files.pane_split_title(config, role) for role in (
            _role("main", 0), _role("review", 1, presentation_label="Reviewer"), _role("", 2))]
        command = presentation_layout_files.inert_pane_command(Path("/opt/p"), ["a b", "c"], title="T",
                                                               window_title=" ")
    check(titles == ["Main", "Reviewer", "P319 window"],
          f"the slug capitalised, a label when the tenant gives one, else the window's: {titles}")
    check(command == team_launcher._konsole_command(["/opt/p", "--title", "T", "a b", "c"]),
          f"an inert command carries only the titles it was given, quoted for Konsole: {command!r}")


def test_where_a_desktop_accounts_copy_goes_and_may_not_go() -> None:
    from scripts import presentation_layout_files, team_launcher

    owned = SimpleNamespace(project="p319", run_as_user="syrd319-owner", repository=None)
    config_path = Path("/nonexistent/syrd319-owner/config/p319.json")
    with patched(team_launcher, _gui_home=lambda user: f"/nonexistent/home/{user}",
                 _owner_state_layout_output_path=lambda project, *, owner_home: owner_home / "state" / f"{project}.json",
                 current_user_name=lambda: "syrd319-me"):
        tenant = presentation_layout_files.default_layout_output_path(owned, config_path=config_path)
        same = presentation_layout_files.desktop_presentation_layout_path(owned, config_path=config_path,
                                                                          gui_user="syrd319-owner")
        other = presentation_layout_files.desktop_presentation_layout_path(owned, config_path=config_path,
                                                                           gui_user="syrd319-desk")
        with patched(team_launcher, desktop_state_dir=lambda project, user: Path(f"/nonexistent/patched/{user}")):
            rebound = presentation_layout_files.desktop_presentation_layout_path(owned, config_path=config_path,
                                                                                 gui_user="syrd319-desk")
            refused_rebound = presentation_layout_files.desktop_layout_destination_problem(
                other, gui_user="syrd319-desk", project="p319")
        state = presentation_layout_files.desktop_state_dir("p319", "syrd319-desk")
        ok = presentation_layout_files.desktop_layout_destination_problem(other, gui_user="syrd319-desk",
                                                                          project="p319")
        elsewhere = presentation_layout_files.desktop_layout_destination_problem(
            state / ".." / "p319-presentation-layout.json", gui_user="syrd319-desk", project="p319")
        unnamed = presentation_layout_files.desktop_layout_destination_problem(other, gui_user="syrd319-desk",
                                                                               project="")
    check(tenant == Path("/nonexistent/syrd319-owner/state/p319.json"),
          f"an unknown owner's home is found in the config path, through the launcher's state path: {tenant}")
    check(same == tenant.with_name("p319-presentation-layout.json"),
          f"the owner's own window reads the tenant's copy: {same}")
    check(state == Path("/nonexistent/home/syrd319-desk/.local/state/switchyard/projects/p319")
          and other == state / "p319-presentation-layout.json",
          f"another desktop account's copy goes in its own state directory, from the launcher's home: {other}")
    check(rebound == Path("/nonexistent/patched/syrd319-desk/p319-presentation-layout.json"),
          f"a rebound desktop_state_dir reaches the layout path: {rebound}")
    check(ok == "" and refused_rebound.startswith(f"{other} is not in syrd319-desk's own state directory"),
          f"the one place it may cross to is the launcher's state dir, as rebound: {refused_rebound!r}")
    check(elsewhere.endswith(f"({state})") and "is not in syrd319-desk's own state directory" in elsewhere,
          f"a path that climbs out of it is refused: {elsewhere!r}")
    check(unnamed == "no project was named for a layout that has to cross into another account",
          f"and so is one with no project: {unnamed!r}")


def test_the_owner_repair_runs_only_across_accounts() -> None:
    from scripts import presentation_layout_files, team_launcher

    ran: list[list[str]] = []

    def runner(returncode: int):
        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            ran.append(list(args))
            return subprocess.CompletedProcess(args, returncode, "", "syrd319 denied" if returncode else "")
        return run

    output = Path("/nonexistent/syrd319/layouts/p319.json")
    owned = SimpleNamespace(project="p319", run_as_user="syrd319-owner")
    with patched(team_launcher, current_user_name=lambda: "syrd319-owner"):
        presentation_layout_files.ensure_layout_output_owner(owned, output, runner=runner(0))
    check(ran == [], "the owner running its own launch changes no ownership")
    with patched(team_launcher, current_user_name=lambda: "syrd319-other",
                 _proc_failure_reason=lambda result, default: f"{default}: {result.stderr}"):
        presentation_layout_files.ensure_layout_output_owner(owned, output, runner=runner(0))
        try:
            presentation_layout_files.ensure_layout_output_owner(owned, output, runner=runner(1))
            failed = ""
        except SystemExit as exc:
            failed = str(exc)
    expected = ["chown", "-R", "syrd319-owner:syrd319-owner", "/nonexistent/syrd319/layouts"]
    check(ran == [expected, expected], f"another account hands the layout directory to the owner: {ran}")
    check(failed == ("team-launcher: failed to assign layout output /nonexistent/syrd319/layouts to syrd319-owner: "
                     "chown failed with exit 1: syrd319 denied"), f"and a failure says why: {failed!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"presentation_layout_files_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
