#!/usr/bin/env python3
"""SYRD-337: the pane-launcher preflight, against the launcher it came out of.

`_verify_pane_launcher_path` moved into `scripts/pane_launcher_preflight.py`
unchanged. `pane_window_program` did not: four modules read it through the
launcher, and the moved check reads it there too. This pins what makes that
safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher still exports the very same function, whichever module is
  imported first, and `launch_project` calls it by the launcher's own name, so
  a suite's patch of it still reaches the launch.
- **Seam (rule 24).** The inert window's path is the launcher's
  `pane_window_program`, looked up when the check runs; nothing the function
  binds is read through the launcher (rule 27).
- **The check is unchanged:** no configured launcher means the caller's own
  path, untouched and unprobed; otherwise the launcher, then the inert window
  beside it, must be executable, each asked of the caller's runner, and the
  first refusal stops the launch with its exact message.

The runner is this test's own table; no executable is probed and nothing runs.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
NAME = "_verify_pane_launcher_path"
LAUNCHER = Path("/nonexistent/syrd337/release/switchyard-pane")
WINDOW = Path("/nonexistent/syrd337/release/fake-inert-window")
SCRIPT = Path("/nonexistent/syrd337/checkout/team-launcher")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


#: What launch_project reads through the launcher (SYRD-429).
LAUNCH_READS = ("_launch_runners_and_paths", "_prepare_launch", "_write_layout_and_plan", "_start_workers_and_present", "_report_launch",
                "process_authority_board_compatibility", "migrate_declarative_director_onboarding", "upgrade_generated_project_layout",
                "prepare_project_desktop", "_verify_pane_launcher_path", "WorkerStartup", "load_project_config")


def launcher_with_launch_project() -> ast.Module:
    """The launcher as launch_project's call sites see it: the launcher's own definitions, and launch_project.

    SYRD-429 moved launch_project to scripts/project_launch.py, where it reads each phase and check through the launcher
    when it runs. Checked first, on the source as it is: the launcher no longer defines it, re-exports it unaliased, and
    main and switchyard_main still call it by that name; its first statement is the call-time launcher import; every one
    of its launcher reads goes through the launcher, none bare. Only then is that import dropped, each `launcher.X` read
    as `X`, and the command appended to the launcher's body, so the call sites and positions below are the command's
    own. Before the move (the baseline) it is the launcher as it stands.
    """
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    moved = ROOT / "scripts" / "project_launch.py"
    if not moved.exists():
        return launcher
    command = next(n for n in ast.parse(moved.read_text(encoding="utf-8")).body if isinstance(n, ast.FunctionDef) and n.name == "launch_project")
    check(not any(isinstance(n, ast.FunctionDef) and n.name == "launch_project" for n in launcher.body), "the launcher no longer defines launch_project")
    check(any(isinstance(n, ast.ImportFrom) and n.module == "scripts.project_launch"
              and any(a.name == "launch_project" and a.asname is None for a in n.names) for n in launcher.body),
          "the launcher re-exports it, unaliased")
    dispatch = {n.name: sum(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "launch_project" for c in ast.walk(n))
                for n in launcher_body(ROOT, launcher) if isinstance(n, ast.FunctionDef) and n.name in ("main", "switchyard_main")}
    past = [n for n in ast.walk(launcher) if isinstance(n, ast.Attribute) and n.attr == "launch_project"]
    check(dispatch == {"main": 1, "switchyard_main": 2} and past == [],
          f"main and switchyard_main still call it by the launcher's name, as often as before, and nothing reaches past it: {dispatch}")
    check(ast.unparse(command.body[0]) == "from scripts import team_launcher as launcher",
          f"the command imports the launcher first thing, when it runs: {ast.unparse(command.body[0])}")
    bare = sorted({n.id for n in ast.walk(command) if isinstance(n, ast.Name) and n.id in LAUNCH_READS})
    through = sorted({n.attr for n in ast.walk(command)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher"})
    check(bare == [] and through == sorted(LAUNCH_READS), f"every launch_project read goes through the launcher, none bare: {bare} {through}")
    del command.body[0]

    class AsLauncherGlobal(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            self.generic_visit(node)
            if isinstance(node.value, ast.Name) and node.value.id == "launcher":
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return node

    AsLauncherGlobal().visit(command)
    launcher.body.append(command)
    return launcher


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def verify(config: SimpleNamespace, executable: dict[str, bool], *, script_path: Path = SCRIPT):
    """Run the check against a runner that answers `test -x` from a table, with the window lookup patched."""
    from scripts import pane_launcher_preflight, team_launcher

    ran: list[list[str]] = []
    asked: list[Path] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        check(kwargs == {} and argv[:2] == ["test", "-x"] and len(argv) == 3,
              f"the runner is only asked `test -x <path>`: {argv} {kwargs}")
        ran.append(list(argv))
        # A path the table does not name answers executable, so a probe this
        # case did not expect shows up in what ran rather than as a crash.
        return subprocess.CompletedProcess(argv, 0 if executable.get(argv[2], True) else 1, "", "")

    saved = team_launcher.pane_window_program
    team_launcher.pane_window_program = lambda path: asked.append(path) or WINDOW
    try:
        try:
            result: object = pane_launcher_preflight._verify_pane_launcher_path(
                config, script_path=script_path, runner=runner)
        except SystemExit as exc:
            result = f"SystemExit: {exc}"
    finally:
        team_launcher.pane_window_program = saved
    return result, ran, asked


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.pane_launcher_preflight as m; "
        "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module, the launcher least of all: "
                                         f"{result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.pane_launcher_preflight", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.pane_launcher_preflight")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.pane_launcher_preflight as p; "
            f"print(t.{NAME} is p.{NAME}, t.pane_window_program.__module__, hasattr(p, 'pane_window_program'))"
        )
        check(result.stdout.strip() == "True scripts.team_launcher False",
              f"{' then '.join(order)}: the check is the launcher's too, and the window lookup stayed: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_call_site_the_seam_and_the_functions_own_names() -> None:
    launcher_tree = launcher_with_launch_project()
    calls = [n for n in ast.walk(launcher_tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == NAME]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
          "launch_project calls it at its one baseline site, by the launcher's own (patchable) name")
    module = ast.parse((ROOT / "scripts" / "pane_launcher_preflight.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top, only when the check runs")
    lookups = [n for n in ast.walk(module) if isinstance(n, ast.Call)
               and getattr(n.func, "id", getattr(n.func, "attr", "")) == "pane_window_program"]
    check(len(lookups) == 1 and isinstance(lookups[0].func, ast.Attribute)
          and isinstance(lookups[0].func.value, ast.Name) and lookups[0].func.value.id == "launcher",
          "the inert window is looked up at its one site, through the launcher")
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == NAME)
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound and through == [],
          f"nothing the check binds itself is read as the launcher's, nor shadows it: {through}")


def test_no_configured_launcher_answers_the_callers_own_path() -> None:
    result, ran, asked = verify(SimpleNamespace(pane_launcher=None, run_as_user="syrd337-owner"), {})
    check(result is SCRIPT and ran == [] and asked == [],
          f"the caller's own script path, the same object, with nothing probed: {result!r} {ran} {asked}")


def test_a_configured_launcher_and_its_window_both_executable() -> None:
    config = SimpleNamespace(pane_launcher=LAUNCHER, run_as_user="syrd337-owner")
    result, ran, asked = verify(config, {str(LAUNCHER): True, str(WINDOW): True})
    check(result is LAUNCHER, f"the configured launcher itself is the answer: {result!r}")
    check(ran == [["test", "-x", str(LAUNCHER)], ["test", "-x", str(WINDOW)]] and asked == [LAUNCHER],
          f"the launcher is probed, then the window the launcher's lookup names beside it: {ran} {asked}")


def test_a_launcher_that_cannot_run_stops_before_the_window() -> None:
    for owner, suffix in (("syrd337-owner", " by syrd337-owner"), (None, ""), ("", "")):
        result, ran, asked = verify(SimpleNamespace(pane_launcher=LAUNCHER, run_as_user=owner),
                                    {str(LAUNCHER): False, str(WINDOW): True})
        check(result == f"SystemExit: team-launcher: configured pane_launcher {LAUNCHER} is not "
                        f"readable/executable{suffix}",
              f"refused, naming the account ({owner!r}) when there is one: {result!r}")
        check(ran == [["test", "-x", str(LAUNCHER)]] and asked == [],
              f"and nothing else is probed or looked up: {ran} {asked}")


def test_a_release_without_the_inert_window_is_refused() -> None:
    result, ran, _ = verify(SimpleNamespace(pane_launcher=LAUNCHER, run_as_user="syrd337-owner"),
                            {str(LAUNCHER): True, str(WINDOW): False})
    check(result == (f"SystemExit: team-launcher: {WINDOW} is missing or not executable; this release cannot open "
                     "panes that stay inert when they detach. Upgrade the shared release before starting."),
          f"no fallback to a shell: the launch stops, saying why: {result!r}")
    check(len(ran) == 2, f"after both probes: {ran}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_call_site_the_seam_and_the_functions_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"pane_launcher_preflight_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
