#!/usr/bin/env python3
"""SYRD-339: launch_project's P3 phase (runners, owner delegation and paths), against the launcher.

P3's ten statements moved into `scripts/launch_phases.py` as
`_launch_runners_and_paths`, which returns a frozen `LaunchSetup`;
`launch_project` calls it at P3's old position and unpacks the eight values the
rest of the launch reads. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher exports both names, the very same objects whichever module is
  imported first, and calls the phase by its own name.
- **Seams (rule 24).** Every launcher facility the phase uses -- the current
  user, the owner's process and git runners, the owned roots, the default
  pane-state directory, the layout path and the window title -- is read through
  the launcher when it runs; nothing the phase binds is read there (rule 27).
- **The phase is unchanged:** each owner and anchor branch, the explicit
  overrides, the layout-owner rule, the objects handed on and the order of every
  lookup.
- **The launch is wired as before:** the board preflight still refuses before
  the phase runs, and the phase's runner is the one the next phase uses.

Every lookup is this test's own fake, patched on the launcher and recorded; no
account, path, git, provider, GUI, board, socket or tmux is touched.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
EXPORTED = ("LaunchSetup", "_launch_runners_and_paths")
#: The phase's launcher lookups, each called once at most, through the launcher.
SEAMS = ("current_user_name", "_owner_process_runner", "_owner_project_git_runner", "_control_repository_owned_roots",
         "default_pane_state_dir_for_user", "default_layout_output_path", "project_window_title")
OUTPUTS = ("worktree_runner", "role_process_runner", "delegate_role_sessions_to_owner", "effective_pane_state_dir",
           "output_path", "window_title", "should_assign_layout_owner", "pane_script_path")
OWNER = "syrd339-owner"
CONFIG_PATH = Path("/nonexistent/syrd339/p339.json")
SCRIPT = Path("/nonexistent/syrd339/checkout/team-launcher")


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


def caller_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the phase must not run anything: {argv}")


class Lookups:
    """The launcher facilities P3 uses, answering with objects this test owns, in one call log."""

    def __init__(self, current: str = "syrd339-launcher", *, owner_runner_error: Exception | None = None) -> None:
        self.current = current
        self.owner_runner_error = owner_runner_error
        self.log: list[tuple] = []
        self.owner_runner = object()
        self.git_runner = object()
        self.roots = ("/nonexistent/syrd339/owned",)
        self.pane_state = Path("/nonexistent/syrd339/pane-state")
        self.layout = Path("/nonexistent/syrd339/layout.json")

    def names(self) -> dict[str, object]:
        def owner_process_runner(*, owner_user: str, runner: object) -> object:
            self.log.append(("owner-runner", owner_user, runner))
            if self.owner_runner_error:
                raise self.owner_runner_error
            return self.owner_runner

        def owner_project_git_runner(*, owner_user: str, project_dir: object, owned_roots: object, runner: object):
            self.log.append(("git-runner", owner_user, project_dir, owned_roots, runner))
            return self.git_runner

        return dict(
            current_user_name=lambda: self.log.append(("current",)) or self.current,
            _owner_process_runner=owner_process_runner,
            _owner_project_git_runner=owner_project_git_runner,
            _control_repository_owned_roots=lambda config: self.log.append(("roots",)) or self.roots,
            default_pane_state_dir_for_user=lambda user, *, project: self.log.append(("pane-state", user, project))
            or self.pane_state,
            default_layout_output_path=lambda config, *, config_path: self.log.append(("layout", config_path))
            or self.layout,
            project_window_title=lambda config: self.log.append(("title",)) or "P339 title",
        )

    def kinds(self) -> list[str]:
        return [entry[0] for entry in self.log]


def config(owner: str | None = OWNER, *, repository: object = None, pane_launcher: object = None) -> SimpleNamespace:
    return SimpleNamespace(project="p339", run_as_user=owner, repository=repository, pane_launcher=pane_launcher)


def setup(cfg: SimpleNamespace, lookups: Lookups, **overrides: object):
    from scripts import launch_phases, team_launcher

    arguments = dict(config_path=CONFIG_PATH, runner=caller_runner, pane_state_dir=None, layout_output=None,
                     assign_layout_owner=None, script_path=SCRIPT)
    arguments.update(overrides)
    with patched(team_launcher, **lookups.names()):
        return launch_phases._launch_runners_and_paths(cfg, **arguments)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.launch_phases as m; "
        "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module, the launcher least of all: "
                                         f"{result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.launch_phases", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.launch_phases")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.launch_phases as p; "
            f"print(all(getattr(t, n) is getattr(p, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: both names are the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_call_site_the_seams_and_the_phases_own_names() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(launcher_tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_launch_runners_and_paths"]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
          "launch_project calls the phase at one site, by the launcher's own (patchable) name")
    module = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top, only when the phase runs")
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_launch_runners_and_paths")
    for name in SEAMS:
        uses = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(uses) == 1 and isinstance(uses[0].func, ast.Attribute) and isinstance(uses[0].func.value, ast.Name)
              and uses[0].func.value.id == "launcher", f"the phase calls {name} at its one site, through the launcher")
    bare = sorted({n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in SEAMS})
    check(bare == [], f"and never past it: {bare}")
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound and through == [],
          f"nothing the phase binds itself is read as the launcher's, nor shadows it: {through}")


def test_the_result_is_a_frozen_record_of_exactly_the_outputs() -> None:
    from scripts.launch_phases import LaunchSetup

    check(tuple(field.name for field in dataclasses.fields(LaunchSetup)) == OUTPUTS,
          f"LaunchSetup holds the eight outputs, in the order the phase assigns them: "
          f"{[f.name for f in dataclasses.fields(LaunchSetup)]}")
    result = setup(config(None), Lookups())
    try:
        result.window_title = "changed"
        refused = False
    except dataclasses.FrozenInstanceError:
        refused = True
    check(refused, "and it cannot be changed once made")


def test_no_owner_keeps_the_callers_runner_and_asks_nobody() -> None:
    lookups = Lookups()
    result = setup(config(None), lookups)
    check(result.worktree_runner is caller_runner and result.role_process_runner is caller_runner
          and result.delegate_role_sessions_to_owner is False,
          f"no owner: both runners are the caller's own, and nothing is delegated: {result}")
    check(lookups.kinds() == ["pane-state", "layout", "title"] and lookups.log[0] == ("pane-state", None, "p339"),
          f"the current user is not even asked; the defaults and title are, in order: {lookups.log}")
    check(result.effective_pane_state_dir is lookups.pane_state and result.output_path is lookups.layout
          and result.window_title == "P339 title" and result.should_assign_layout_owner is True
          and result.pane_script_path is SCRIPT,
          f"the default paths and title handed on as made, the layout owned when no output was given: {result}")


def test_a_caller_who_is_the_owner_is_not_delegated_to() -> None:
    lookups = Lookups(current=OWNER)
    result = setup(config(OWNER, repository=Path("/nonexistent/syrd339/repo")), lookups)
    check(result.delegate_role_sessions_to_owner is False and result.worktree_runner is caller_runner
          and result.role_process_runner is caller_runner and lookups.kinds() == ["current", "pane-state", "layout",
                                                                                   "title"],
          f"the caller already is the owner: nothing is built for it: {lookups.log}")


def test_another_owner_gets_its_own_runners_anchored_where_it_works() -> None:
    repo, pane = Path("/nonexistent/syrd339/repo"), Path("/nonexistent/syrd339/pane-launcher")
    for anchor_config, anchor in ((config(repository=repo, pane_launcher=pane), repo),
                                  (config(repository=None, pane_launcher=pane), pane)):
        lookups = Lookups()
        result = setup(anchor_config, lookups)
        check(result.delegate_role_sessions_to_owner is True and result.role_process_runner is lookups.owner_runner
              and result.worktree_runner is lookups.git_runner,
              f"roles run through the owner's process runner, worktrees through its git runner: {result}")
        check(lookups.log[:4] == [("current",), ("owner-runner", OWNER, caller_runner), ("roots",),
                                  ("git-runner", OWNER, anchor, lookups.roots, caller_runner)],
              f"asked in order, built from the caller's runner, the git runner anchored at {anchor}: {lookups.log}")
    lookups = Lookups()
    result = setup(config(repository=None, pane_launcher=None), lookups)
    check(result.role_process_runner is lookups.owner_runner and result.worktree_runner is caller_runner
          and "git-runner" not in lookups.kinds() and "roots" not in lookups.kinds(),
          f"with nothing to anchor it, worktree work keeps the caller's runner: {lookups.log}")


def test_explicit_paths_and_the_layout_owner_rule() -> None:
    given_state, given_output = Path("/nonexistent/syrd339/given-state"), Path("/nonexistent/syrd339/given.json")
    lookups = Lookups()
    result = setup(config(None), lookups, pane_state_dir=given_state, layout_output=given_output)
    check(result.effective_pane_state_dir is given_state and result.output_path is given_output
          and lookups.kinds() == ["title"],
          f"explicit paths are used as given, and their defaults are never asked: {lookups.log}")
    rules = [((None, None), True), ((None, given_output), False), ((True, given_output), True),
             ((False, None), False), ((True, None), True)]
    for (assign, output), expected in rules:
        answer = setup(config(None), Lookups(), assign_layout_owner=assign, layout_output=output)
        check(answer.should_assign_layout_owner is expected,
              f"assign_layout_owner={assign!r}, layout_output={output!r}: the layout is owned {expected}")
    launcher = Path("/nonexistent/syrd339/configured-pane-launcher")
    check(setup(config(None, pane_launcher=launcher), Lookups()).pane_script_path is launcher,
          "a configured pane launcher is the pane script, the same object")


def test_a_failing_owner_runner_stops_the_phase() -> None:
    refusal = PermissionError("syrd339: no such account")
    lookups = Lookups(owner_runner_error=refusal)
    try:
        setup(config(repository=Path("/nonexistent/syrd339/repo")), lookups)
        raised = None
    except PermissionError as exc:
        raised = exc
    check(raised is refusal and lookups.kinds() == ["current", "owner-runner"],
          f"the builder's failure reaches the caller, and nothing after it is asked: {raised!r} {lookups.log}")


class Stop(Exception):
    """Raised by a patched next step, carrying what it was handed."""


def test_the_launch_runs_the_phase_where_it_was_and_hands_its_values_on() -> None:
    from scripts import team_launcher
    from scripts.launch_phases import LaunchSetup

    asked: list[dict] = []
    sentinel = LaunchSetup(worktree_runner=object(), role_process_runner=object(),
                           delegate_role_sessions_to_owner=False, effective_pane_state_dir=Path("/n/s"),
                           output_path=Path("/n/o"), window_title="T", should_assign_layout_owner=False,
                           pane_script_path=Path("/n/p"))

    def phase(cfg: object, **kwargs: object) -> LaunchSetup:
        asked.append({"config": cfg, **kwargs})
        return sentinel

    def verify(cfg: object, *, script_path: object, runner: object) -> object:
        raise Stop(runner)

    cfg = SimpleNamespace(project="p339", role_state_isolation=False)
    given = dict(pane_state_dir=Path("/n/given-state"), layout_output=Path("/n/given.json"), assign_layout_owner=True)
    with patched(team_launcher, _launch_runners_and_paths=phase,
                 upgrade_generated_project_layout=lambda c, **k: SimpleNamespace(changed=False, message=""),
                 _verify_pane_launcher_path=verify):
        try:
            team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                         runner=caller_runner, **given)
            handed = None
        except Stop as stop:
            handed = stop.args[0]
    check(asked == [dict(config=cfg, config_path=CONFIG_PATH, runner=caller_runner, script_path=SCRIPT, **given)],
          f"the launch hands the phase its own config and arguments, through the launcher's name: {asked}")
    check(handed is sentinel.worktree_runner,
          "and the next phase checks the pane launcher with the worktree runner the phase returned")


def test_a_refused_board_preflight_still_comes_before_the_phase() -> None:
    from scripts import team_launcher

    printed: list[str] = []

    def phase(*args: object, **kwargs: object) -> object:
        raise AssertionError("the phase ran before the board preflight refused")

    with patched(team_launcher, _launch_runners_and_paths=phase,
                 process_authority_board_compatibility=lambda c: (False, "syrd339: old board")):
        result = team_launcher.launch_project(SimpleNamespace(project="p339", role_state_isolation=True),
                                              config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                              runner=caller_runner, print_func=printed.append)
    check(result == 1 and printed and "syrd339: old board" in printed[0],
          f"the launch is refused before the phase is asked anything: {result} {printed}")


def test_a_dry_run_carries_the_phases_paths_title_and_layout_owner_into_the_layout_and_plan() -> None:
    # The dry run reaches the layout (P6) without starting anything, so it shows
    # where four of the phase's values land; worktree_runner is followed above.
    # role_process_runner, delegate_role_sessions_to_owner and
    # effective_pane_state_dir are first read when workers start, which no fake
    # here reaches: their wiring is the AST proof's (equiv_phase step 4).
    import contextlib
    import io
    import json

    from scripts import team_launcher
    from scripts.launch_phases import LaunchSetup

    sentinel = LaunchSetup(worktree_runner=object(), role_process_runner=object(),
                           delegate_role_sessions_to_owner=False, effective_pane_state_dir=Path("/n/state"),
                           output_path=Path("/n/syrd339-layout.json"), window_title="SYRD339 window",
                           should_assign_layout_owner=True, pane_script_path=Path("/n/syrd339-pane"))
    laid_out: list[dict] = []
    owned: list[tuple] = []
    cfg = SimpleNamespace(project="p339", role_state_isolation=False, run_as_user="syrd339-owner", repository=None,
                          roles=[])
    printed = io.StringIO()
    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: sentinel,
                 materialize_layout=lambda c, **k: laid_out.append(k),
                 ensure_layout_output_owner=lambda c, path, *, runner: owned.append((path, runner))):
        with contextlib.redirect_stdout(printed):
            result = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                                  runner=caller_runner, dry_run=True)
    plan = json.loads(printed.getvalue())
    check(result == 0 and len(laid_out) == 1 and laid_out[0]["output_path"] is sentinel.output_path
          and laid_out[0]["script_path"] is sentinel.pane_script_path,
          f"the layout is written to the phase's output path, with its pane script: {laid_out}")
    check(owned == [(sentinel.output_path, caller_runner)],
          f"the phase's layout-owner flag decides that the output is handed to its owner: {owned}")
    check(plan["window_title"] == "SYRD339 window" and plan["layout"] == "/n/syrd339-layout.json",
          f"and the plan carries the phase's title and layout path: {plan}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_call_site_the_seams_and_the_phases_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"launch_phases_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
