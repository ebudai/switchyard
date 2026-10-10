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

SYRD-340 added P5, pre-launch preparation (`_prepare_launch`, returning a frozen
`LaunchPreparation` whose `exit_code` is None to go on or the code the launch
returns at once). Its cases below pin every branch, the order of every step, the
runner each is given, the objects handed back, both refusals with their exact
output, and the launch returning the phase's code before the layout is written.

SYRD-341 added P6, layout, plan and dry run (`_write_layout_and_plan`, answering
None to go on or 0 after a dry run's plan). Its cases pin the layout's
arguments (the caller's own pane-state directory, not the effective one), the
owner hand-off, every plan field and which lookups each role needs, the dry
run's layout-mode resolution and viewer additions, the exact output, and the
launch stopping at 0 before any worker starts.

SYRD-342 added P7 and P8 together, worker start and presentation
(`_start_workers_and_present`): they share the nested failure recorder, which
keeps the first failing exit and writes each failure into the caller's own
`failed_roles`. A code the launch stops on is returned as the very object it
was; going on returns a frozen `WorkerStartup`. Its cases pin every branch --
detached and visible starts, delegated and direct, with and without runtime
presentation; the viewer, runtime-separate, handed-back, refused and Konsole
windows; the skips, the stderr text and the first failure kept -- and the
launch: stopping before the layout mode exists, P9 fenced from a stop, and P9
seeing the four values and the same failures dict.

SYRD-344 added P9, the launch's report and records (`_report_launch`, the last
six statements, ending in the launch's own return). Its cases pin the unsafe
window report, the attach announcement's filter, order and wording, which roles
get provider-state records, the session report's arguments, the code coming
back as the very object, and `launch_project` returning P9's answer.

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

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
EXPORTED = ("LaunchSetup", "_launch_runners_and_paths", "LaunchPreparation", "_prepare_launch",
            "_write_layout_and_plan", "WorkerStartup", "_start_workers_and_present", "_report_launch")
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


#: What launch_project reads through the launcher (SYRD-429).
LAUNCH_READS = ("_launch_runners_and_paths", "_prepare_launch", "_write_layout_and_plan", "_start_workers_and_present", "_report_launch",
                "process_authority_board_compatibility", "migrate_declarative_director_onboarding", "upgrade_generated_project_layout",
                "prepare_project_desktop", "_verify_pane_launcher_path", "WorkerStartup", "load_project_config", "report_kept_worktrees")


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


def fence_workers():
    """P7's first step, fenced: a case that should stop before workers start fails here, clearly,
    instead of running real worker code if a mutant carries the launch past its phase."""
    from scripts import presentation_controller

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the launch went past its phase into starting workers")
    return patched(presentation_controller, presentation_enabled=refuse)


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
    launcher_tree = launcher_with_launch_project()
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


def test_a_dry_run_carries_the_phases_paths_title_and_layout_owner_into_the_next_phase() -> None:
    # P3's values that P6 (layout, plan and dry run) reads are handed to it as the same objects; P6 is stood
    # in, as P5 is, so no real phase code runs here, and P7 is fenced. P6's own cases are below.
    # role_process_runner, delegate_role_sessions_to_owner and effective_pane_state_dir are first read when
    # workers start, which no fake here reaches: their wiring is the AST proof's (equiv_phase step 4).
    from scripts import team_launcher
    from scripts.launch_phases import LaunchPreparation, LaunchSetup

    sentinel = LaunchSetup(worktree_runner=object(), role_process_runner=object(),
                           delegate_role_sessions_to_owner=False, effective_pane_state_dir=Path("/n/state"),
                           output_path=Path("/n/syrd339-layout.json"), window_title="SYRD339 window",
                           should_assign_layout_owner=True, pane_script_path=Path("/n/syrd339-pane"))
    handed: list[dict] = []
    cfg = SimpleNamespace(project="p339", role_state_isolation=False)

    def inert_p5(c: object, **k: object) -> LaunchPreparation:
        return LaunchPreparation(exit_code=None, config=c, failed_roles={}, running_roles=[], reconcile_home=None,
                                 unreconciled_roles=set())

    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: sentinel, _prepare_launch=inert_p5,
                 _write_layout_and_plan=lambda c, **k: handed.append(k) or 0), fence_workers():
        result = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                              runner=caller_runner, dry_run=True)
    check(result == 0 and len(handed) == 1, f"the dry run ends with P6's 0: {result!r}")
    got = handed[0]
    check(got["output_path"] is sentinel.output_path and got["pane_script_path"] is sentinel.pane_script_path
          and got["should_assign_layout_owner"] is True and got["window_title"] == "SYRD339 window",
          f"P6 is handed P3's output path, pane script, layout-owner flag and title: {got}")

#: P5's launcher lookups, each read once, through the launcher (measured).
P5_SEAMS = ("LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV", "NO_LAUNCHER_SELF_DEPLOY_ENV",
            "_drop_roles_with_stale_provider_runtime", "_env_truthy_any", "_owner_home_for_auth",
            "_prepare_project_worktrees_for_launch", "_running_project_roles",
            "current_user_name", "ensure_configured_runtime_user", "ensure_generated_project_board_skill",
            "ensure_generated_project_pane_hooks", "ensure_launcher_checkout_current", "ensure_owner_state_dirs",
            "fetch_project_worktree_ref", "prepare_project_desktop", "role_isolation_gaps",
            "seed_default_session_dir_from_legacy_sources", "sync_reload_config_to_live_sessions")
P5_OUTPUTS = ("exit_code", "config", "failed_roles", "running_roles", "reconcile_home", "unreconciled_roles", "kept_worktrees")
PANES = Path("/nonexistent/syrd340/pane-state")
PANE_SCRIPT = Path("/nonexistent/syrd340/pane")
OWNER_HOME = Path("/nonexistent/syrd340/owner-home")


def worktree_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the preparation must not run anything itself: {argv}")


class Preparation:
    """P5's launcher lookups, answering from objects this test owns, into one ordered log."""

    def __init__(self, *, env_flag: bool = False, running: tuple[str, ...] = (), failed: dict | None = None,
                 gaps: list[str] | None = None, current: str = "syrd340-launcher", step_error: Exception | None = None):
        self.env_flag, self.running, self.failed, self.gaps = env_flag, running, failed or {}, gaps or []
        self.current, self.step_error = current, step_error
        self.log: list[tuple] = []
        self.kept: list = []
        self.unreconciled = {"syrd340-unreconciled"}
        self.worktree_result = SimpleNamespace(failed_roles=dict(self.failed), kept_roles={})  # SYRD-555: kept, not failed
        self.synced = SimpleNamespace(project="p340-synced")
        self.desktop = SimpleNamespace(project="p340-desktop")
        self.auth_home = Path("/nonexistent/syrd340/auth-home")

    def names(self, config: SimpleNamespace) -> dict[str, object]:
        L = self.log

        def running_roles(cfg, *, runner):
            L.append(("running", runner))
            return [r for r in cfg.roles if r.role in self.running]

        def drop(cfg, running, *, owner_home, runner, print_func):
            L.append(("drop", [r.role for r in running], owner_home, runner, print_func))
            self.kept = list(running)
            return self.kept, self.unreconciled

        def runtime_user(cfg, *, runner):
            L.append(("runtime-user", runner))
            if self.step_error:
                raise self.step_error

        return dict(
            NO_LAUNCHER_SELF_DEPLOY_ENV="SYRD340_NO_DEPLOY",
            LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV="SYRD340_LEGACY_NO_DEPLOY",
            _env_truthy_any=lambda *names: L.append(("env", names)) or self.env_flag,
            ensure_launcher_checkout_current=lambda cfg, *, runner, auto_deploy, allow_stale: L.append(
                ("checkout", runner, auto_deploy, allow_stale)),
            _running_project_roles=running_roles,
            current_user_name=lambda: L.append(("current",)) or self.current,
            _owner_home_for_auth=lambda user: L.append(("auth-home", user)) or self.auth_home,
            _drop_roles_with_stale_provider_runtime=drop,
            ensure_configured_runtime_user=runtime_user,
            ensure_owner_state_dirs=lambda cfg, *, pane_state_dir, runner: L.append(
                ("owner-dirs", pane_state_dir, runner)),
            ensure_generated_project_pane_hooks=lambda cfg, *, config_path, script_path, pane_state_dir, runner:
            L.append(("hooks", config_path, script_path, pane_state_dir, runner)),
            ensure_generated_project_board_skill=lambda cfg, *, config_path, script_path, runner, print_func: L.append(
                ("board-skill", config_path, script_path, runner, print_func)),
            seed_default_session_dir_from_legacy_sources=lambda session_dir: L.append(("seed", session_dir)),
            _prepare_project_worktrees_for_launch=lambda cfg, *, running_roles, runner, discard: L.append(
                ("worktrees", [r.role for r in running_roles], runner, discard)) or self.worktree_result,
            fetch_project_worktree_ref=lambda cfg, *, runner: L.append(("fetch", runner)),
            sync_reload_config_to_live_sessions=lambda cfg, *, config_path, runner: L.append(("sync", cfg, runner))
            or self.synced,
            prepare_project_desktop=lambda cfg, *, runner: L.append(("desktop", cfg, runner)) or self.desktop,
            role_isolation_gaps=lambda cfg: L.append(("gaps", cfg)) or list(self.gaps),
        )

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def p5_config(*, control: bool = True, owner: str | None = "syrd340-owner") -> SimpleNamespace:
    return SimpleNamespace(project="p340", run_as_user=owner,
                           roles=[SimpleNamespace(role=n) for n in ("alpha", "beta")],
                           control_repository=Path("/nonexistent/syrd340/control") if control else None,
                           session_dir=Path("/nonexistent/syrd340/sessions"))


def prepare(prep: Preparation, cfg: SimpleNamespace, *, mode: str = "attach-or-start", dry_run: bool = False,
            owner_home: object = OWNER_HOME, no_deploy: bool = False, printed: list | None = None):
    import contextlib
    import io

    from scripts import launch_phases, team_launcher

    printed = printed if printed is not None else []
    stderr = io.StringIO()
    with patched(team_launcher, **prep.names(cfg)), contextlib.redirect_stderr(stderr):
        result = launch_phases._prepare_launch(
            cfg, allow_stale_launcher="syrd340-allow-stale", config_path=CONFIG_PATH, dry_run=dry_run,
            effective_pane_state_dir=PANES, mode=mode, no_launcher_self_deploy=no_deploy, owner_home=owner_home,
            pane_script_path=PANE_SCRIPT, print_func=printed.append, runner=caller_runner,
            worktree_runner=worktree_runner)
    return result, stderr.getvalue(), printed


def test_the_preparation_reads_every_launcher_lookup_through_the_launcher() -> None:
    module = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_prepare_launch")
    for name in P5_SEAMS:
        uses = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(uses) == 1 and isinstance(uses[0].value, ast.Name) and uses[0].value.id == "launcher" and not bare,
              f"P5 reads {name} at its one site, through the launcher")
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound and through == [], f"nothing P5 binds is read as the launcher's: {through}")
    import sys as _sys

    from scripts import launch_phases
    check(launch_phases.sys is _sys, "sys is the module's own import, the one module object")
    launcher_tree = launcher_with_launch_project()
    calls = [n for n in ast.walk(launcher_tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_prepare_launch"]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
          "launch_project calls P5 at one site, by the launcher's own (patchable) name")
    from scripts.launch_phases import LaunchPreparation
    check(tuple(f.name for f in dataclasses.fields(LaunchPreparation)) == P5_OUTPUTS
          and LaunchPreparation.__dataclass_params__.frozen,
          "LaunchPreparation is frozen, exit_code first, then the five outputs in the order P5 assigns them")


def test_p5_a_dry_run_prepares_nothing() -> None:
    prep = Preparation(); cfg = p5_config()
    result, err, printed = prepare(prep, cfg, dry_run=True)
    check(prep.log == [] and err == "" and printed == [], f"a dry run asks nothing and says nothing: {prep.log}")
    check(result.exit_code is None and result.config is cfg and result.failed_roles == {} and result.running_roles == []
          and result.reconcile_home is None and result.unreconciled_roles == set(),
          f"it goes on, with the caller's config and empty preparation: {result}")


def test_p5_attach_or_start_prepares_in_order() -> None:
    prep = Preparation(running=("alpha",), failed={"beta": "busy"}); cfg = p5_config(control=False)
    result, err, printed = prepare(prep, cfg)
    check(prep.kinds() == ["env", "checkout", "running", "drop", "runtime-user", "owner-dirs", "hooks", "board-skill",
                           "seed", "worktrees", "gaps"],
          f"the checkout, the running roles and their stale runtimes, then the account, state, hooks, skill, seed, "
          f"worktrees and the isolation check: {prep.kinds()}")
    entries = dict((e[0], e) for e in prep.log)
    check(entries["checkout"] == ("checkout", worktree_runner, True, "syrd340-allow-stale")
          and entries["env"] == ("env", ("SYRD340_NO_DEPLOY", "SYRD340_LEGACY_NO_DEPLOY")),
          f"the checkout runs through the worktree runner, self-deploying unless the launcher's flags say not: "
          f"{prep.log[:2]}")
    check(entries["running"] == ("running", caller_runner)
          and entries["drop"][1:4] == (["alpha"], OWNER_HOME, caller_runner) and "auth-home" not in prep.kinds(),
          f"running roles through the caller's runner, reconciled against the given owner home: {entries['drop']}")
    check(entries["owner-dirs"] == ("owner-dirs", PANES, caller_runner)
          and entries["hooks"] == ("hooks", CONFIG_PATH, PANE_SCRIPT, PANES, caller_runner)
          and entries["board-skill"][1:4] == (CONFIG_PATH, PANE_SCRIPT, caller_runner)
          and entries["seed"] == ("seed", cfg.session_dir)
          and entries["worktrees"] == ("worktrees", ["alpha"], worktree_runner, frozenset()),  # SYRD-555: no discard unless named
          f"state, hooks and skill from the phase's paths; worktrees through the worktree runner: {prep.log}")
    check(result.exit_code is None and result.config is cfg and result.failed_roles is prep.worktree_result.failed_roles
          and result.kept_worktrees is prep.worktree_result.kept_roles and result.running_roles is prep.kept and result.reconcile_home is OWNER_HOME
          and result.unreconciled_roles is prep.unreconciled and err == "" and printed == [],
          f"it goes on, handing back the very objects the steps made: {result}")


def test_p5_the_reconcile_home_is_looked_up_when_not_given() -> None:
    prep = Preparation(); prepare(prep, p5_config(), owner_home=None)
    check(("auth-home", "syrd340-owner") in prep.log and "current" not in prep.kinds(),
          f"the owner's own auth home, without asking who is running: {prep.log}")
    prep = Preparation(current="syrd340-me"); result, _, _ = prepare(prep, p5_config(owner=None), owner_home=None)
    check(prep.kinds()[:4] == ["env", "checkout", "running", "current"] and ("auth-home", "syrd340-me") in prep.log
          and result.reconcile_home is prep.auth_home, f"with no owner, the current user's: {prep.log}")


def test_p5_the_self_deploy_switches() -> None:
    prep = Preparation(); prepare(prep, p5_config(), no_deploy=True)
    check(prep.log[0][:3] == ("checkout", worktree_runner, False) and "env" not in prep.kinds(),
          f"an explicit no-self-deploy wins without reading the environment: {prep.log[:2]}")
    prep = Preparation(env_flag=True); prepare(prep, p5_config())
    check(prep.log[1][:3] == ("checkout", worktree_runner, False), f"so does the environment's: {prep.log[:2]}")


def test_p5_reload_and_attach() -> None:
    prep = Preparation(); cfg = p5_config()
    result, _, _ = prepare(prep, cfg, mode="reload")
    check(prep.kinds() == ["env", "checkout", "runtime-user", "owner-dirs", "hooks", "board-skill", "seed", "fetch",
                           "sync", "desktop", "gaps"],
          f"a reload fetches, syncs and prepares the desktop, and looks at no running role: {prep.kinds()}")
    check(prep.log[7] == ("fetch", worktree_runner) and prep.log[8] == ("sync", cfg, caller_runner)
          and prep.log[9] == ("desktop", prep.synced, caller_runner) and prep.log[10] == ("gaps", prep.desktop)
          and result.config is prep.desktop and result.exit_code is None,
          f"the synced config is the one prepared, and the prepared one is handed on: {prep.log[7:]}")
    prep = Preparation(); result, _, _ = prepare(prep, cfg, mode="attach")
    check(prep.kinds() == ["env", "checkout", "runtime-user", "owner-dirs", "hooks", "board-skill", "seed", "gaps"]
          and result.config is cfg, f"a plain attach prepares no worktree and no reload: {prep.kinds()}")


def test_p5_a_control_repository_that_cannot_be_prepared_stops_the_launch() -> None:
    prep = Preparation(running=("alpha",), failed={"beta": "clone refused"})
    result, err, printed = prepare(prep, p5_config(control=True))
    check(result.exit_code == 1 and "gaps" not in prep.kinds() and printed == []
          and err == "team-launcher: failed to prepare control repository for p340: clone refused\n",
          f"every stopped role failed, so the launch stops, saying why on stderr: {result.exit_code} {err!r}")
    prep = Preparation(failed={"alpha": "busy"})
    result, err, _ = prepare(prep, p5_config(control=True))
    check(result.exit_code is None and err == "" and "gaps" in prep.kinds()
          and result.failed_roles == {"alpha": "busy"}, f"a partial failure goes on: {result}")
    prep = Preparation(failed={"alpha": "a", "beta": "b"})
    result, err, _ = prepare(prep, p5_config(control=False))
    check(result.exit_code is None and err == "", "without a control repository, even a complete failure goes on")


def test_p5_isolation_gaps_stop_the_launch() -> None:
    prep = Preparation(gaps=["alpha is still dedicated", "beta too"])
    result, err, printed = prepare(prep, p5_config())
    check(result.exit_code == 1 and err == "" and printed == [
        "team-launcher: refusing to launch p340; its resumable state is not ready for project-account runtime:\n  "
        "alpha is still dedicated\n  beta too\nRun `sudo switchyard upgrade p340` after every live role is at a "
        "resumable checkpoint. The migration leaves dedicated accounts intact."],
          f"the launch stops, saying why through print_func: {printed}")


def test_p5_a_failing_step_stops_the_phase() -> None:
    refusal = PermissionError("syrd340: runtime user")
    prep = Preparation(step_error=refusal)
    try:
        prepare(prep, p5_config()); raised = None
    except PermissionError as exc:
        raised = exc
    check(raised is refusal and prep.kinds()[-1] == "runtime-user",
          f"the step's failure reaches the caller, and nothing after it runs: {prep.kinds()}")


def test_the_launch_returns_p5s_code_before_the_layout_and_hands_its_values_on() -> None:
    from scripts import team_launcher
    from scripts.launch_phases import LaunchPreparation

    order: list[str] = []
    asked: list[dict] = []
    stopped = LaunchPreparation(exit_code=7, config=None, failed_roles={}, running_roles=[], reconcile_home=None,
                                unreconciled_roles=set())

    def phase(cfg: object, **kwargs: object) -> LaunchPreparation:
        order.append("prepare"); asked.append(kwargs)
        return stopped

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("the layout was written after P5 stopped the launch")

    from scripts.launch_phases import LaunchSetup
    setup = LaunchSetup(worktree_runner=worktree_runner, role_process_runner=caller_runner,
                        delegate_role_sessions_to_owner=False, effective_pane_state_dir=PANES,
                        output_path=Path("/nonexistent/syrd340/layout.json"), window_title="P340",
                        should_assign_layout_owner=False, pane_script_path=SCRIPT)
    cfg = SimpleNamespace(project="p340", role_state_isolation=False, pane_launcher=None, run_as_user="syrd340-owner",
                          repository=None, roles=[])
    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: setup, _prepare_launch=phase,
                 materialize_layout=never,
                 upgrade_generated_project_layout=lambda c, **k: order.append("upgrade")
                 or SimpleNamespace(changed=False),
                 _verify_pane_launcher_path=lambda c, **k: order.append("verify") or PANE_SCRIPT):
        result = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                              runner=caller_runner)
    check(result == 7 and order == ["upgrade", "verify", "prepare"],
          f"P4's checks run first, then P5, whose code the launch returns before the layout: {result} {order}")
    check(asked[0]["pane_script_path"] is PANE_SCRIPT and asked[0]["dry_run"] is False and asked[0]["mode"] == "attach",
          f"P5 is handed the pane script P4 verified, and the launch's own arguments: {asked[0]}")
    prepared = SimpleNamespace(project="p340-prepared", run_as_user="syrd340-owner", repository=None, roles=[])
    going_on = LaunchPreparation(exit_code=None, config=prepared, failed_roles={"x": "y"}, running_roles=[],
                                 reconcile_home=None, unreconciled_roles=set())
    laid_out: list[tuple] = []
    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: setup,
                 _prepare_launch=lambda c, **k: going_on,
                 _write_layout_and_plan=lambda c, **k: laid_out.append((c, k["failed_roles"])) or 0), fence_workers():
        result = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                              runner=caller_runner, dry_run=True, assign_layout_owner=False)
    check(result == 0 and laid_out == [(prepared, going_on.failed_roles)]
          and laid_out[0][1] is going_on.failed_roles,
          f"going on, the next phase is handed the prepared config and failures, the same objects: {laid_out}")


#: P6's launcher lookups and the number of times each is read, through the launcher (measured).
P6_SEAMS = {"materialize_layout": 1, "ensure_layout_output_owner": 1, "current_user_name": 1, "worktree_ref": 1,
            "failed_role_command": 1, "pane_command": 1, "role_run_as_user": 1, "resolve_layout_mode": 1,
            "viewer_session_for_project": 1, "visible_roles_for_viewer": 1, "LAYOUT_MODE_AUTO": 1,
            "LAYOUT_MODE_SEPARATE": 1, "LAYOUT_MODE_VIEWER": 2}
FAKE_HOME = Path("/nonexistent/syrd341/home")


class fake_home:
    """Path.home answering FAKE_HOME for one block, restored exactly: no real account home is read."""

    def __enter__(self) -> None:
        self.saved = Path.__dict__["home"]
        Path.home = classmethod(lambda cls: FAKE_HOME)

    def __exit__(self, *exc: object) -> None:
        Path.home = self.saved


class Layout:
    """P6's launcher lookups, answering from objects this test owns, into one ordered log."""

    def __init__(self, *, current: str = "syrd341-me", resolved: str = "SYRD341_SEPARATE",
                 error: Exception | None = None):
        self.current, self.resolved, self.error = current, resolved, error
        self.log: list[tuple] = []

    def names(self) -> dict[str, object]:
        L = self.log

        def materialize(cfg, **kwargs):
            L.append(("layout", cfg, kwargs))
            if self.error:
                raise self.error

        return dict(
            materialize_layout=materialize,
            ensure_layout_output_owner=lambda cfg, path, *, runner: L.append(("owner", cfg, path, runner)),
            current_user_name=lambda: L.append(("current",)) or self.current,
            worktree_ref=lambda cfg: L.append(("worktree-ref",)) or "syrd341/ref",
            failed_role_command=lambda role, reason: L.append(("failed-command", role.role, reason))
            or f"FAILED {role.role}: {reason}",
            pane_command=lambda project, role, **kwargs: L.append(("pane-command", role.role, kwargs))
            or f"PANE {role.role}",
            role_run_as_user=lambda cfg, role: L.append(("run-as", role.role)) or f"acct-{role.role}",
            resolve_layout_mode=lambda mode, *, environ, runner: L.append(("resolve", mode, environ, runner))
            or self.resolved,
            viewer_session_for_project=lambda project: L.append(("viewer-session", project)) or f"view-{project}",
            visible_roles_for_viewer=lambda cfg: L.append(("viewer-roles",))
            or [r for r in cfg.roles if not r.detached],
            LAYOUT_MODE_AUTO="SYRD341_AUTO", LAYOUT_MODE_SEPARATE="SYRD341_SEPARATE",
            LAYOUT_MODE_VIEWER="SYRD341_VIEWER",
        )

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def p6_config(*, owner: str | None = "syrd341-owner", repository: object = None) -> SimpleNamespace:
    def role(name, slot, detached=False):
        return SimpleNamespace(role=name, slot=slot, detached=detached, tmux_session=f"p341-{name}",
                               target=f"p341-{name}:0.0", workdir=f"/nonexistent/syrd341/{name}")
    return SimpleNamespace(project="p341", run_as_user=owner, repository=repository,
                           roles=[role("zeta", 0), role("alpha", 1), role("bg", None, detached=True)])


def layout(lay: Layout, cfg: SimpleNamespace, *, dry_run: bool, layout_mode: str = "SYRD341_AUTO",
           layout_environ: object = None, failed: dict | None = None, assign: bool = True):
    import contextlib
    import io

    from scripts import launch_phases, team_launcher

    out = io.StringIO()
    kwargs = dict(config_path=CONFIG_PATH, dry_run=dry_run, failed_roles=failed if failed is not None else {},
                  force_reload="syrd341-force", layout_environ=layout_environ, layout_mode=layout_mode, mode="attach",
                  output_path=Path("/nonexistent/syrd341/layout.json"), pane_script_path=PANE_SCRIPT,
                  pane_state_dir=Path("/nonexistent/syrd341/caller-pane-state"), runner=caller_runner,
                  should_assign_layout_owner=assign, window_title="P341 window")
    with patched(team_launcher, **lay.names()), fake_home(), contextlib.redirect_stdout(out):
        result = launch_phases._write_layout_and_plan(cfg, **kwargs)
    return result, out.getvalue(), kwargs


def test_the_layout_phase_reads_its_lookups_through_the_launcher() -> None:
    import json as _json

    from scripts import launch_phases
    module = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_write_layout_and_plan")
    for name, count in P6_SEAMS.items():
        uses = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(uses) == count and all(isinstance(n.value, ast.Name) and n.value.id == "launcher" for n in uses)
              and not bare, f"P6 reads {name} at its {count} site(s), through the launcher")
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound and through == [], f"nothing P6 binds is read as the launcher's: {through}")
    check(launch_phases.json is _json and launch_phases.Path is Path,
          "json and Path are the module's own, the very objects the launcher holds")
    launcher_tree = launcher_with_launch_project()
    calls = [n for n in ast.walk(launcher_tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_write_layout_and_plan"]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
          "launch_project calls P6 at one site, by the launcher's own (patchable) name")


def test_p6_a_launch_writes_the_layout_hands_it_over_and_goes_on() -> None:
    lay = Layout(); cfg = p6_config()
    result, out, kwargs = layout(lay, cfg, dry_run=False, failed={"alpha": "busy"})
    check(result is None and out == "", f"a launch goes on, printing nothing: {result!r} {out!r}")
    first = lay.log[0]
    check(first[0] == "layout" and first[1] is cfg and first[2] == dict(
        config_path=CONFIG_PATH, mode="attach", script_path=PANE_SCRIPT, output_path=kwargs["output_path"],
        pane_state_dir=kwargs["pane_state_dir"], force_reload="syrd341-force", failed_roles=kwargs["failed_roles"])
          and first[2]["failed_roles"] is kwargs["failed_roles"],
          f"the layout first, with the caller's own pane-state directory and failures: {first}")
    check(lay.log[1] == ("owner", cfg, kwargs["output_path"], caller_runner),
          f"then handed to its owner through the caller's runner: {lay.log[1]}")
    # zeta, visible: its account is looked up as an argument, then its pane command; alpha, failed: its command.
    check(lay.kinds()[2:] == ["run-as", "pane-command", "failed-command"] and "resolve" not in lay.kinds()
          and "current" not in lay.kinds() and "worktree-ref" not in lay.kinds(),
          f"the plan is still built -- a failed role's command, a visible role's pane command -- but nothing it "
          f"does not need: {lay.kinds()}")
    lay = Layout(); layout(lay, cfg, dry_run=False, assign=False)
    check("owner" not in lay.kinds(), "and not handed over when the phase is told not to")


def test_p6_a_dry_run_prints_the_plan_and_answers_zero() -> None:
    import json as _json

    lay = Layout(); cfg = p6_config(owner=None, repository=Path("/nonexistent/syrd341/repo"))
    result, out, kwargs = layout(lay, cfg, dry_run=True, failed={"alpha": "busy"})
    try:
        plan = _json.loads(out)
    except ValueError:
        plan = None
    check(result == 0 and plan is not None and out == _json.dumps(plan, indent=2, sort_keys=True) + "\n",
          f"a dry run prints the plan on stdout as sorted, two-space JSON and answers 0: {result!r} {out[:60]!r}")
    check(plan["project"] == "p341" and plan["window_title"] == "P341 window" and plan["mode"] == "attach"
          and plan["layout"] == "/nonexistent/syrd341/layout.json" and plan["run_as_user"] == "syrd341-me"
          and plan["worktree_ref"] == "syrd341/ref" and "layout_mode" not in plan,
          f"its fields, the current user standing in for a missing owner: {plan}")
    check([r["role"] for r in plan["roles"]] == ["zeta", "alpha"]
          and [r["role"] for r in plan["detached_roles"]] == ["bg"],
          f"visible roles in config order, detached ones apart: {plan['roles']}")
    zeta, alpha = plan["roles"]
    check(zeta["command"] == "PANE zeta" and zeta["workdir"] == "/nonexistent/syrd341/zeta"
          and zeta["worktree_error"] == ""
          and alpha["command"] == "FAILED alpha: busy" and alpha["workdir"] == str(FAKE_HOME)
          and alpha["worktree_error"] == "busy",
          f"a failed role runs its failure command from the home directory, with its error: {alpha}")
    pane = next(e for e in lay.log if e[0] == "pane-command")
    check(pane[2] == dict(config_path=CONFIG_PATH, mode="attach", script_path=PANE_SCRIPT,
                          pane_state_dir=kwargs["pane_state_dir"], force_reload="syrd341-force",
                          skip_launcher_check=True, run_as_user="acct-zeta"),
          f"the pane command for a visible role: {pane}")
    check("resolve" not in lay.kinds() and lay.kinds()[0] == "layout",
          f"with the default mode and no environment the layout mode is not resolved, and the layout is still "
          f"written first: "
          f"{lay.kinds()}")


def test_p6_a_dry_run_resolves_an_explicit_mode_and_adds_the_viewer() -> None:
    import json as _json

    lay = Layout(resolved="SYRD341_VIEWER"); cfg = p6_config()
    result, out, _ = layout(lay, cfg, dry_run=True, layout_mode="viewer", layout_environ={"X": "1"})
    plan = _json.loads(out)
    check(result == 0 and ("resolve", "viewer", {"X": "1"}, caller_runner) in lay.log,
          f"an explicit mode is resolved with the caller's environment and runner: {lay.log}")
    check(plan.get("layout_mode") == "SYRD341_VIEWER" and plan.get("viewer_session") == "view-p341"
          and plan.get("viewer_roles") == ["zeta", "alpha"] and "current" not in lay.kinds(),
          f"a viewer plan names the viewer session and its roles: {plan}")
    lay = Layout(); layout(lay, cfg, dry_run=True, layout_environ={"Y": "2"})
    check(any(e[0] == "resolve" and e[1] == "SYRD341_AUTO" for e in lay.log),
          "the default mode is resolved too when an environment is given")


def test_p6_a_failing_layout_stops_the_phase() -> None:
    refusal = PermissionError("syrd341: layout")
    lay = Layout(error=refusal)
    try:
        layout(lay, p6_config(), dry_run=True); raised = None
    except PermissionError as exc:
        raised = exc
    check(raised is refusal and lay.kinds() == ["layout"],
          f"the failure reaches the caller, nothing after it: {lay.kinds()}")


def test_the_launch_stops_at_p6s_exit_before_any_worker_starts() -> None:
    from scripts import presentation_controller, team_launcher
    from scripts.launch_phases import LaunchPreparation, LaunchSetup

    setup = LaunchSetup(worktree_runner=worktree_runner, role_process_runner=caller_runner,
                        delegate_role_sessions_to_owner=False, effective_pane_state_dir=PANES,
                        output_path=Path("/nonexistent/syrd341/layout.json"), window_title="P341",
                        should_assign_layout_owner=True, pane_script_path=SCRIPT)
    prepared = SimpleNamespace(project="p341")
    going_on = LaunchPreparation(exit_code=None, config=prepared, failed_roles={}, running_roles=[],
                                 reconcile_home=None, unreconciled_roles=set())
    asked: list[tuple] = []

    def workers_start(*a: object, **k: object) -> object:
        raise Stop("workers")

    cfg = SimpleNamespace(project="p341", role_state_isolation=False, pane_launcher=None)
    common = dict(_launch_runners_and_paths=lambda c, **k: setup,
                  upgrade_generated_project_layout=lambda c, **k: SimpleNamespace(changed=False),
                  _verify_pane_launcher_path=lambda c, **k: SCRIPT)
    for answer, want in ((0, 0), (None, "workers")):
        asked.clear()
        with patched(team_launcher, **common, _prepare_launch=lambda c, **k: going_on,
                     _write_layout_and_plan=lambda c, **k: asked.append((c, k)) or answer,
                     _start_workers_and_present=workers_start), \
                patched(presentation_controller, presentation_enabled=workers_start):
            try:
                got = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                                   runner=caller_runner, pane_state_dir=Path("/n/given"))
            except Stop as stop:
                got = stop.args[0]
        outcome = "returns 0 before any worker" if want == 0 else "goes on to the workers"
        check(got == want, f"P6 answering {answer!r}: the launch {outcome}: {got!r}")
    check(asked[0][0] is prepared and asked[0][1]["pane_state_dir"] == Path("/n/given")
          and asked[0][1]["output_path"] is setup.output_path and asked[0][1]["window_title"] == "P341",
          f"P6 is handed P5's config, the caller's pane-state directory and P3's paths: {asked[0][1]}")
    stopped = LaunchPreparation(exit_code=3, config=None, failed_roles={}, running_roles=[], reconcile_home=None,
                                unreconciled_roles=set())

    def never(*a: object, **k: object) -> None:
        raise AssertionError("P6 ran after P5 stopped the launch")

    with patched(team_launcher, **common, _prepare_launch=lambda c, **k: stopped, _write_layout_and_plan=never,
                 _start_workers_and_present=never), fence_workers():
        got = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                           runner=caller_runner)
    check(got == 3, f"P5's exit comes before P6: {got!r}")


#: P7+P8's launcher lookups and the number of times each is read, through the launcher (measured on the
#: SYRD-342 baseline: 46 reads of 21 names).
P78_SEAMS = {"pane_command_args": 3, "role_pane_state_dir": 6, "role_run_as_user": 6, "run_detached_role": 1,
             "role_session_dir": 3, "role_process_runner_for": 3, "resolve_layout_mode": 1, "LAYOUT_MODE_VIEWER": 3,
             "visible_roles_for_viewer": 3, "ensure_visible_role_session_for_viewer": 2, "ensure_owner_state_dirs": 2,
             "RUNTIME_REGISTRATION_TIMEOUT_SECONDS": 2, "launch_tmux_viewer_session": 1,
             "viewer_session_for_project": 1, "running_through_tenant_control": 2,
             "hand_presentation_back_to_the_caller": 2, "project_window_title": 1, "display_attach_helper_path": 1,
             "LAYOUT_MODE_SEPARATE": 1, "legacy_presentation_refusal": 1, "launch_konsole_window": 1}
P78_OUTPUTS = ("worker_start_exit_code", "launch_started_at", "launch_started_ns", "resolved_layout_mode")
EFFECTIVE = Path("/nonexistent/syrd342/effective-pane-state")
OUTPUT = Path("/nonexistent/syrd342/out/layout.json")
GIVEN_LAYOUT = Path("/nonexistent/syrd342/given/layout.json")
WAIT = 342.5


class Code(int):
    """An exit code with an identity, so a stop can be shown to hand back the very object it got."""


def viewer_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"the role process runner is only handed on, never run here: {argv}")


def konsole_launcher(*args: object, **kwargs: object) -> None:
    raise AssertionError("the Konsole process launcher is only handed on, never run here")


class Workers:
    """P7+P8's launcher lookups, the presentation controller and the clock, answering from objects this test
    owns, into one ordered log. `codes` answers each role's start; a role not named starts with 0."""

    def __init__(self, *, runtime: bool = False, resolved: str = "SYRD342_SEPARATE", codes: dict | None = None,
                 tenant: bool = False, handback: bool = True, refusal: str = "", konsole: int = 0,
                 viewer: int = 0, presentation: int = 0, error: Exception | None = None):
        self.runtime, self.resolved, self.codes = runtime, resolved, codes or {}
        self.tenant, self.handback, self.refusal = tenant, handback, refusal
        self.konsole, self.viewer, self.presentation, self.error = konsole, viewer, presentation, error
        self.log: list[tuple] = []
        self.runners: dict[str, object] = {}

    def start(self, kind: str, role: SimpleNamespace, detail: object) -> object:
        self.log.append((kind, role.role, detail))
        if self.error and kind == "detached":
            raise self.error
        return self.codes.get(role.role, 0)

    def caller_runner(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        check(kwargs == {}, f"a delegated start is run as argv alone: {kwargs}")
        code = self.start("delegated", SimpleNamespace(role=argv[1]), list(argv))
        return subprocess.CompletedProcess(argv, code, "", "")

    def names(self) -> dict[str, object]:
        L = self.log

        def process_runner_for(cfg, role, *, runner):
            L.append(("role-runner", role.role, runner))
            return self.runners.setdefault(role.role, object())

        return dict(
            pane_command_args=lambda project, role, **kw: L.append(("pane-args", role.role, kw))
            or ["PANE", role.role],
            role_pane_state_dir=lambda cfg, role, effective: Path(f"{effective}/{role.role}"),
            role_run_as_user=lambda cfg, role: f"acct-{role.role}",
            run_detached_role=lambda role, **kw: self.start("detached", role, kw),
            role_session_dir=lambda cfg, role: Path(f"/nonexistent/syrd342/sessions/{role.role}"),
            role_process_runner_for=process_runner_for,
            resolve_layout_mode=lambda mode, *, environ, runner: L.append(("resolve", mode, environ, runner))
            or self.resolved,
            LAYOUT_MODE_VIEWER="SYRD342_VIEWER", LAYOUT_MODE_SEPARATE="SYRD342_SEPARATE",
            RUNTIME_REGISTRATION_TIMEOUT_SECONDS=WAIT,
            visible_roles_for_viewer=lambda cfg: L.append(("viewer-roles",))
            or [r for r in cfg.roles if not r.detached],
            ensure_visible_role_session_for_viewer=lambda role, **kw: self.start("visible", role, kw),
            ensure_owner_state_dirs=lambda cfg, *, pane_state_dir, runner: L.append(("owner-dirs", pane_state_dir,
                                                                                      runner)),
            launch_tmux_viewer_session=lambda roles, **kw: L.append(("viewer", [r.role for r in roles], kw))
            or self.viewer,
            viewer_session_for_project=lambda project: f"view-{project}",
            running_through_tenant_control=lambda: L.append(("tenant?",)) or self.tenant,
            hand_presentation_back_to_the_caller=lambda cfg, **kw: L.append(("handback", kw)) or self.handback,
            project_window_title=lambda cfg: "P342 title",
            display_attach_helper_path=lambda project: f"/nonexistent/syrd342/{project}-attach",
            legacy_presentation_refusal=lambda cfg, *, output_path: L.append(("refusal?", output_path))
            or self.refusal,
            launch_konsole_window=lambda path, **kw: L.append(("konsole", path, kw)) or self.konsole,
        )

    def controller(self) -> dict[str, object]:
        def enabled(cfg, *, config_path):
            self.log.append(("runtime?", config_path))
            return self.runtime

        def launch(cfg, **kw):
            self.log.append(("presentation", kw))
            return self.presentation
        return dict(presentation_enabled=enabled, launch_presentation=launch)

    def clock(self) -> SimpleNamespace:
        return SimpleNamespace(time=lambda: self.log.append(("time",)) or 342.25,
                               time_ns=lambda: self.log.append(("time_ns",)) or 342_250_000_000)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def p78_config(*roles: tuple[str, bool]) -> SimpleNamespace:
    return SimpleNamespace(project="p342", roles=[SimpleNamespace(role=name, detached=detached)
                                                  for name, detached in roles])


def workers(w: Workers, cfg: SimpleNamespace, *, delegate: bool = False, failed: dict | None = None,
            window_title: str = "P342 window", layout_output: object = OUTPUT):
    import contextlib
    import io

    from scripts import launch_phases, presentation_controller, team_launcher

    said: list[str] = []
    err = io.StringIO()
    kwargs = dict(allow_stale_launcher="syrd342-stale", config_path=CONFIG_PATH,
                  delegate_role_sessions_to_owner=delegate, effective_pane_state_dir=EFFECTIVE,
                  failed_roles=failed if failed is not None else {}, force_reload="syrd342-force",
                  konsole_process_launcher=konsole_launcher, layout_environ={"SYRD342": "env"},
                  layout_mode="SYRD342_AUTO", layout_output=layout_output, mode="attach", output_path=OUTPUT,
                  pane_script_path=PANE_SCRIPT, print_func=said.append, role_process_runner=viewer_runner,
                  runner=w.caller_runner, window_title=window_title)
    with patched(team_launcher, **w.names()), patched(presentation_controller, **w.controller()), \
            patched(launch_phases, time=w.clock()), contextlib.redirect_stderr(err):
        result = launch_phases._start_workers_and_present(cfg, **kwargs)
    return result, err.getvalue(), said, kwargs


def pane_args(role: str, *, no_attach: bool = False) -> dict[str, object]:
    args = dict(config_path=CONFIG_PATH, mode="attach", script_path=PANE_SCRIPT,
                pane_state_dir=Path(f"{EFFECTIVE}/{role}"), force_reload="syrd342-force",
                skip_launcher_check=True, allow_stale_launcher="syrd342-stale")
    if no_attach:
        args["no_attach"] = True
    args["run_as_user"] = f"acct-{role}"
    return args


def direct_args(w: Workers, role: str) -> dict[str, object]:
    return dict(mode="attach", session_dir=Path(f"/nonexistent/syrd342/sessions/{role}"),
                pane_state_dir=Path(f"{EFFECTIVE}/{role}"), force_reload="syrd342-force", bin_user=f"acct-{role}",
                runner=w.runners[role])


def test_the_worker_phase_reads_its_lookups_through_the_launcher() -> None:
    import sys as _sys
    import time as _time

    from scripts import launch_phases
    module = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_start_workers_and_present")
    for name, count in P78_SEAMS.items():
        uses = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(uses) == count and all(isinstance(n.value, ast.Name) and n.value.id == "launcher" for n in uses)
              and not bare, f"P7+P8 read {name} at its {count} site(s), through the launcher")
    imports = [n for n in function.body if isinstance(n, ast.ImportFrom)]
    check([(n.module, [(a.name, a.asname) for a in n.names]) for n in imports]
          == [("scripts", [("team_launcher", "launcher")]), ("scripts", [("presentation_controller", None)])],
          "the launcher, then the presentation controller, are the phase's own imports, in that order")
    controller = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == "presentation_controller"]
    calls = sorted(n.func.attr for n in ast.walk(function) if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                   and n.func.value.id == "presentation_controller")
    check(controller == [] and calls == ["launch_presentation", "launch_presentation", "presentation_enabled"],
          f"the controller is the local import, never the launcher's, and is called through it: {calls}")
    nested = [n for n in function.body if isinstance(n, ast.FunctionDef)]
    check([n.name for n in nested] == ["record_worker_start_failure"]
          and isinstance(nested[0].body[0], ast.Nonlocal) and nested[0].body[0].names == ["worker_start_exit_code"],
          "the failure recorder stays nested, holding the phase's own exit code")
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {"presentation_controller", "record_worker_start_failure", "reason"}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound - {"launcher"} and through == [],
          f"nothing P7+P8 bind, nor the recorder's own names, is read as the launcher's: {through}")
    check(launch_phases.time is _time and launch_phases.sys is _sys and launch_phases.Path is Path,
          "time, sys and Path are the module's own, the very objects the launcher holds")
    launcher_tree = launcher_with_launch_project()
    sites = [n for n in ast.walk(launcher_tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_start_workers_and_present"]
    check(len(sites) == 1 and isinstance(sites[0].func, ast.Name),
          "launch_project calls P7+P8 at one site, by the launcher's own (patchable) name")
    launch = next(n for n in launcher_tree.body if isinstance(n, ast.FunctionDef) and n.name == "launch_project")
    at = next(i for i, n in enumerate(launch.body) if isinstance(n, ast.Assign) and n.value is sites[0])
    dispatch, *unpack = launch.body[at + 1:at + 1 + 1 + len(P78_OUTPUTS)]
    check(ast.unparse(dispatch) == "if not isinstance(worker_startup, WorkerStartup):\n    return worker_startup"
          and [ast.unparse(n) for n in unpack] == [f"{name} = worker_startup.{name}" for name in P78_OUTPUTS],
          "a stop is returned right after the call, before any of the four values is read, then each is unpacked")


def test_the_worker_result_is_a_frozen_record_of_exactly_the_four_outputs() -> None:
    from scripts.launch_phases import WorkerStartup

    check(tuple(f.name for f in dataclasses.fields(WorkerStartup)) == P78_OUTPUTS
          and all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
                  for f in dataclasses.fields(WorkerStartup)),
          f"WorkerStartup holds the four outputs, in the order P7+P8 assign them, with no defaults: "
          f"{[f.name for f in dataclasses.fields(WorkerStartup)]}")
    result, _, _, _ = workers(Workers(), p78_config())
    try:
        result.worker_start_exit_code = 9
        refused = False
    except dataclasses.FrozenInstanceError:
        refused = True
    check(refused, "and it cannot be changed once made")


def test_p78_detached_roles_start_direct_then_the_window_opens() -> None:
    w = Workers(); cfg = p78_config(("zeta", False), ("bg", True), ("bg2", True))
    result, err, said, kwargs = workers(w, cfg)
    check(w.kinds()[:3] == ["runtime?", "time", "time_ns"] and w.log[0] == ("runtime?", CONFIG_PATH),
          f"runtime presentation is asked first, then the clock, time before time_ns: {w.kinds()}")
    starts = [e for e in w.log if e[0] in ("detached", "delegated", "visible")]
    check([(e[0], e[1]) for e in starts] == [("detached", "bg"), ("detached", "bg2")],
          f"only the detached roles start here, in config order: {starts}")
    check(("role-runner", "bg", w.caller_runner) in w.log and starts[0][2] == direct_args(w, "bg")
          and starts[1][2] == direct_args(w, "bg2"),
          f"each directly, as its own account, with its own runner made from the caller's: {starts[0][2]}")
    check(w.kinds()[w.kinds().index("time_ns") + 1:] == ["role-runner", "detached", "role-runner", "detached",
                                                         "resolve", "owner-dirs", "tenant?", "refusal?", "konsole"],
          f"then the layout mode, the owner's directories, and with no runtime, no bridge and no refusal, Konsole: "
          f"{w.kinds()}")
    check(("resolve", "SYRD342_AUTO", {"SYRD342": "env"}, w.caller_runner) in w.log
          and ("owner-dirs", EFFECTIVE, w.caller_runner) in w.log
          and ("refusal?", OUTPUT) in w.log
          and w.log[-1] == ("konsole", OUTPUT, dict(project="p342", window_title="P342 window",
                                                   runner=w.caller_runner, process_launcher=konsole_launcher)),
          f"with the caller's layout mode, environment, runner and Konsole launcher: {w.log[-1]}")
    from scripts.launch_phases import WorkerStartup
    check(result == WorkerStartup(worker_start_exit_code=0, launch_started_at=342.25,
                                  launch_started_ns=342_250_000_000, resolved_layout_mode="SYRD342_SEPARATE")
          and err == "" and said == [],
          f"going on, the four values, and nothing said: {result!r} {err!r} {said}")


def test_p78_delegated_detached_roles_run_as_pane_commands() -> None:
    w = Workers(); cfg = p78_config(("bg", True))
    result, _, _, _ = workers(w, cfg, delegate=True)
    check(("pane-args", "bg", pane_args("bg")) in w.log and ("delegated", "bg", ["PANE", "bg"]) in w.log
          and "detached" not in w.kinds() and "role-runner" not in w.kinds(),
          f"a delegated start is the pane command, run on the caller's runner, never a direct start: {w.log}")
    check(result.worker_start_exit_code == 0, f"and it goes on: {result!r}")


def test_p78_a_failed_detached_start_without_runtime_stops_before_the_layout_mode() -> None:
    code = Code(5)
    for delegate in (False, True):
        w = Workers(codes={"bg": code}); failed: dict = {}
        cfg = p78_config(("zeta", False), ("bg", True), ("bg2", True))
        result, err, said, _ = workers(w, cfg, delegate=delegate, failed=failed)
        check(result is code,
              f"the launch stops on the start's own code ({'delegated' if delegate else 'direct'}): {result!r}")
        check([e[1] for e in w.log if e[0] in ("detached", "delegated")] == ["bg"],
              f"only the detached role before it was started, never a visible one: {w.log}")
        check("resolve" not in w.kinds() and w.kinds()[-1] in ("detached", "delegated"),
              f"at once: no later role, no layout mode, no window: {w.kinds()}")
        check(failed == {} and err == "" and said == [], f"and nothing is recorded or said: {failed} {err!r}")


def test_p78_with_runtime_failures_are_recorded_and_the_first_is_kept() -> None:
    import io as _io
    w = Workers(runtime=True, codes={"bg": 5.0, "bg2": 7, "zeta": 6})
    failed: dict = {"old": "kept"}
    cfg = p78_config(("zeta", False), ("bg", True), ("bg2", True))
    result, err, said, kwargs = workers(w, cfg, failed=failed)
    check(result.worker_start_exit_code == 5 and type(result.worker_start_exit_code) is int,
          f"the first failing exit is kept, as an int: {result!r}")
    check(failed == {"old": "kept", "bg": "pane start failed with exit 5.0", "bg2": "pane start failed with exit 7",
                     "zeta": "pane start failed with exit 6"},
          f"each failure is written into the caller's own dict, in its own words: {failed}")
    check(err == ("team-launcher: pane start failed with exit 5.0 for bg; leaving presentation recovery status\n"
                  "team-launcher: pane start failed with exit 7 for bg2; leaving presentation recovery status\n"
                  "team-launcher: pane start failed with exit 6 for zeta; leaving presentation recovery status\n"),
          f"and said on stderr: {err!r}")
    presentation = next(e for e in w.log if e[0] == "presentation")
    check(presentation[1] == dict(config_path=CONFIG_PATH, layout="SYRD342_SEPARATE",
                                  state_path=OUTPUT.with_name("presentation.json"), runner=w.caller_runner,
                                  process_launcher=konsole_launcher, assignment_wait_seconds=WAIT,
                                  print_func=kwargs["print_func"],
                                  unstarted=("old", "bg", "bg2", "zeta")),
          f"a separate runtime presentation, told which roles never started: {presentation[1]}")
    check(w.kinds()[w.kinds().index("resolve"):] == ["resolve", "owner-dirs", "viewer-roles", "role-runner",
                                                     "visible", "presentation"]
          and "tenant?" not in w.kinds() and "konsole" not in w.kinds(),
          f"its visible roles are started first, then the presentation, and no other window: {w.kinds()}")


def test_p78_skips_say_why_and_start_nothing() -> None:
    w = Workers(resolved="SYRD342_VIEWER")
    cfg = p78_config(("zeta", False), ("alpha", False), ("bg", True))
    result, err, said, _ = workers(w, cfg, failed={"bg": "stale", "zeta": "busy"})
    check(err == "skipping detached role bg: stale\nskipping visible role zeta: busy\n",
          f"a failed detached or viewer role is skipped, saying why: {err!r}")
    check([(e[0], e[1]) for e in w.log if e[0] in ("detached", "visible")] == [("visible", "alpha")]
          and ("viewer", ["alpha"], dict(viewer_session="view-p342", window_title="P342 window",
                                         runner=viewer_runner)) in w.log,
          f"only alpha starts, and the viewer holds only it, on the role process runner: {w.log}")
    w = Workers(runtime=True)
    workers(w, p78_config(("zeta", False)), failed={"zeta": "busy"})
    check("visible" not in w.kinds() and "presentation" in w.kinds(),
          "a separate runtime start skips a failed role silently, and still presents")


def test_p78_the_viewer_starts_its_roles_then_opens_one_session() -> None:
    for delegate in (False, True):
        w = Workers(resolved="SYRD342_VIEWER")
        cfg = p78_config(("zeta", False), ("alpha", False), ("bg", True))
        result, err, said, _ = workers(w, cfg, delegate=delegate)
        if delegate:
            check(("pane-args", "zeta", pane_args("zeta", no_attach=True)) in w.log
                  and ("pane-args", "bg", pane_args("bg")) in w.log and "visible" not in w.kinds(),
                  f"delegated viewer roles are pane commands that do not attach: {w.log}")
        else:
            visible = [e for e in w.log if e[0] == "visible"]
            check([e[1] for e in visible] == ["zeta", "alpha"] and visible[0][2] == direct_args(w, "zeta"),
                  f"direct viewer roles start as their own accounts, in order: {visible}")
        after = w.kinds()[w.kinds().index("resolve"):]
        check(after[:2] == ["resolve", "viewer-roles"] and after[-3:] == ["owner-dirs", "viewer", "tenant?"],
              f"the roles, then the owner's directories, the viewer, and the bridge asked: {after}")
        check(("owner-dirs", EFFECTIVE, w.caller_runner) in w.log,
              f"the owner's directories are made in the effective pane-state directory, on the caller's runner: "
              f"{w.log}")
        check(result.resolved_layout_mode == "SYRD342_VIEWER" and said == [] and "handback" not in w.kinds(),
              f"goes on, with nothing handed back outside the bridge: {result!r}")


def test_p78_a_failed_viewer_role_without_runtime_stops() -> None:
    code = Code(6)
    w = Workers(resolved="SYRD342_VIEWER", codes={"zeta": code}); failed: dict = {}
    result, err, said, _ = workers(w, p78_config(("zeta", False), ("alpha", False)), failed=failed)
    check(result is code and w.kinds()[-1] == "visible" and "owner-dirs" not in w.kinds() and failed == {},
          f"the launch stops on that role's own code, at once: {result!r} {w.kinds()}")


def test_p78_a_viewer_with_runtime_presentation() -> None:
    w = Workers(runtime=True, resolved="SYRD342_VIEWER", codes={"bg": 5, "zeta": 6})
    failed: dict = {}
    result, err, said, kwargs = workers(w, p78_config(("zeta", False), ("alpha", False), ("bg", True)),
                                        failed=failed, layout_output=None)
    check(list(failed) == ["bg", "zeta"], f"both failures are written into the caller's own dict: {failed}")
    presentation = next(e for e in w.log if e[0] == "presentation")
    check(presentation[1] == dict(config_path=CONFIG_PATH, layout="SYRD342_VIEWER", state_path=None,
                                  runner=w.caller_runner, assignment_wait_seconds=WAIT,
                                  print_func=kwargs["print_func"], unstarted=("bg", "zeta")),
          f"the viewer is the controller's, with no state path when no layout output was given: {presentation}")
    check(result.worker_start_exit_code == 5 and list(failed) == ["bg", "zeta"] and "viewer" not in w.kinds()
          and w.kinds()[-3:] == ["owner-dirs", "presentation", "tenant?"],
          f"the first failure, detached then visible, is kept, and no tmux viewer opens: {result!r} {w.kinds()}")


def test_p78_the_viewers_empty_and_all_failed_answers() -> None:
    w = Workers(resolved="SYRD342_VIEWER")
    result, _, _, _ = workers(w, p78_config(("bg", True)))
    check(result.worker_start_exit_code == 0 and "viewer" not in w.kinds() and "tenant?" in w.kinds(),
          f"no visible role: nothing to open, and it goes on as a success: {result!r} {w.kinds()}")
    w = Workers(resolved="SYRD342_VIEWER")
    result, err, _, _ = workers(w, p78_config(("zeta", False)), failed={"zeta": "busy"})
    check(result == 1 and "viewer" not in w.kinds() and "tenant?" not in w.kinds(),
          f"every visible role failed: the launch stops with 1 and nothing is handed back: {result!r} {w.kinds()}")
    w = Workers(resolved="SYRD342_VIEWER", viewer=4)
    result, _, _, _ = workers(w, p78_config(("zeta", False)))
    check(result == 4 and "tenant?" not in w.kinds(), f"a viewer that fails stops the launch with its code: {result}")


def test_p78_a_bridged_viewer_is_handed_back_as_one_tab() -> None:
    for title, handback in (("P342 window", True), ("", False)):
        w = Workers(resolved="SYRD342_VIEWER", tenant=True, handback=handback)
        result, _, said, kwargs = workers(w, p78_config(("zeta", False), ("alpha", False)), window_title=title)
        hb = next(e for e in w.log if e[0] == "handback")
        check(hb[1] == dict(slot_count=1, window_title=title, layout="SYRD342_VIEWER",
                            slot_titles=[title or "P342 title"], pane_program=Path("/nonexistent/syrd342/p342-attach"),
                            print_func=kwargs["print_func"]),
              f"one tab, titled by the window or the project, running the attach helper: {hb}")
        warning = ("warning: switchyard: p342's panes are up, but this invocation was given no way to hand its "
                   "window back, so none will open. Run it again from the session that owns the screen")
        check(said == ([] if handback else [warning]) and result.worker_start_exit_code == 0,
              f"a failed hand-back is said, as a warning, and the launch goes on: {said}")


def test_p78_separate_windows_bridged_refused_or_konsole() -> None:
    w = Workers(tenant=True, handback=True)
    result, _, said, kwargs = workers(w, p78_config(("zeta", False), ("alpha", False), ("bg", True)))
    hb = next(e for e in w.log if e[0] == "handback")
    check(hb[1] == dict(slot_count=2, window_title="P342 window", print_func=kwargs["print_func"])
          and "refusal?" not in w.kinds() and "konsole" not in w.kinds() and result.worker_start_exit_code == 0,
          f"bridged: handed back, one slot per visible role, and nothing opened here: {hb} {w.kinds()}")
    w = Workers(tenant=True, handback=False)
    workers(w, p78_config(("zeta", False)))
    check(w.kinds()[-3:] == ["handback", "refusal?", "konsole"], f"a failed hand-back falls through: {w.kinds()}")
    w = Workers(refusal="syrd342: refused")
    result, _, said, _ = workers(w, p78_config(("zeta", False)))
    check(result == 1 and said == ["syrd342: refused"] and "konsole" not in w.kinds() and "handback" not in w.kinds(),
          f"a legacy refusal is said, and stops the launch with 1: {result!r} {said}")
    w = Workers(konsole=3)
    result, _, _, _ = workers(w, p78_config(("zeta", False)))
    check(result == 3, f"Konsole's own failure stops the launch with its code: {result!r}")
    w = Workers(runtime=True, presentation=9)
    result, _, _, _ = workers(w, p78_config(("zeta", False)))
    check(result == 9, f"and so does the runtime presentation's: {result!r}")


def test_p78_an_error_reaches_the_caller_with_nothing_after() -> None:
    boom = OSError("syrd342: start")
    w = Workers(error=boom); failed: dict = {}
    try:
        workers(w, p78_config(("bg", True), ("bg2", True)), failed=failed); raised = None
    except OSError as exc:
        raised = exc
    check(w.kinds()[3:5] == ["role-runner", "detached"],
          f"not delegated, the first detached role is started directly: {w.kinds()}")
    check(raised is boom and w.kinds()[-1] == "detached" and failed == {},
          f"a start that raises is not caught or recorded: {w.kinds()}")


def launch_through_p78(answer: object, *, mode: str = "attach", running: list | None = None,
                       failed: dict | None = None, mutate: dict | None = None, report: bool = False):
    """launch_project with P3, P5 and P6 stood in, P7+P8 answering `answer`, and P9's first lookup recorded."""
    from scripts import team_launcher
    from scripts.launch_phases import LaunchPreparation, LaunchSetup

    setup = LaunchSetup(worktree_runner=worktree_runner, role_process_runner=viewer_runner,
                        delegate_role_sessions_to_owner="syrd342-delegate", effective_pane_state_dir=EFFECTIVE,
                        output_path=OUTPUT, window_title="P342 window", should_assign_layout_owner=True,
                        pane_script_path=PANE_SCRIPT)
    prepared = SimpleNamespace(project="p342", roles=[])
    failures = failed if failed is not None else {}
    going_on = LaunchPreparation(exit_code=None, config=prepared, failed_roles=failures,
                                 running_roles=running or [], reconcile_home=None, unreconciled_roles=set())
    asked: list[tuple] = []
    seen: list[tuple] = []

    def phase(c: object, **k: object) -> object:
        asked.append((c, k))
        if mutate:
            k["failed_roles"].update(mutate)
        return answer

    def p9(c: object, *, config_path: object) -> list:
        seen.append(("p9", c))
        return []
    said: list[str] = []
    cfg = SimpleNamespace(project="p342", role_state_isolation=False, pane_launcher=None)
    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: setup,
                 upgrade_generated_project_layout=lambda c, **k: SimpleNamespace(changed=False),
                 _verify_pane_launcher_path=lambda c, **k: PANE_SCRIPT,
                 prepare_project_desktop=lambda c, **k: c,
                 _prepare_launch=lambda c, **k: going_on, _write_layout_and_plan=lambda c, **k: None,
                 _start_workers_and_present=phase, unsafe_root_presentation_windows=p9,
                 report_launch_session_records=lambda c, **k: seen.append(("records", k))), fence_workers():
        got = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode=mode, script_path=SCRIPT,
                                           runner=caller_runner, layout_output=GIVEN_LAYOUT, layout_mode="SYRD342_AUTO",
                                           layout_environ={"SYRD342": "env"}, konsole_process_launcher=konsole_launcher,
                                           print_func=said.append, report_session_records=report)
    return got, asked, seen, said, setup, prepared, failures


def test_the_launch_returns_a_p78_stop_before_p9() -> None:
    code = Code(7)
    got, asked, seen, said, setup, prepared, failures = launch_through_p78(code)
    check(got is code and seen == [] and said == [],
          f"a stop is returned as the very object P7+P8 gave, and P9 never runs: {got!r} {seen}")
    check(asked[0][0] is prepared and asked[0][1] == dict(
        allow_stale_launcher=False, config_path=CONFIG_PATH, delegate_role_sessions_to_owner="syrd342-delegate",
        effective_pane_state_dir=EFFECTIVE, failed_roles=failures, force_reload=False,
        konsole_process_launcher=konsole_launcher, layout_environ={"SYRD342": "env"}, layout_mode="SYRD342_AUTO",
        layout_output=GIVEN_LAYOUT, mode="attach", output_path=OUTPUT, pane_script_path=PANE_SCRIPT,
        print_func=asked[0][1]["print_func"], role_process_runner=viewer_runner, runner=caller_runner,
        window_title="P342 window") and asked[0][1]["failed_roles"] is failures,
          f"P7+P8 get P5's config and its failures dict itself, P3's runners and paths, and the caller's own "
          f"arguments: {asked[0][1]}")


def test_the_launch_hands_p78s_four_values_and_its_failures_to_p9() -> None:
    from scripts.launch_phases import WorkerStartup
    zeta = SimpleNamespace(role="zeta", detached=False)
    going = WorkerStartup(worker_start_exit_code=4, launch_started_at=342.25, launch_started_ns=342_250_000_000,
                          resolved_layout_mode="SYRD342_SEPARATE")
    got, _, seen, said, _, prepared, _ = launch_through_p78(going, mode="attach-or-start", running=[zeta],
                                                            report=True)
    records = next(e for e in seen if e[0] == "records")[1]
    check(got == 4 and seen[0] == ("p9", prepared) and records["fallback_changed_since_ns"] == 342_250_000_000
          and records["pane_state_updated_since"] == 342.25 and records["pane_state_dir"] == EFFECTIVE,
          f"going on, P9 runs and returns P7+P8's exit code, with their clock: {got!r} {records}")
    check(len(said) == 1 and said[0].startswith("switchyard: opened a new window attached to running pane: zeta"),
          f"P9 reads the separate layout mode P7+P8 resolved: {said}")
    viewer = dataclasses.replace(going, resolved_layout_mode="SYRD342_VIEWER")
    from scripts import team_launcher
    with patched(team_launcher, LAYOUT_MODE_VIEWER="SYRD342_VIEWER"):
        _, _, _, said, _, _, _ = launch_through_p78(viewer, mode="attach-or-start", running=[zeta])
    check(said == [], f"and a viewer mode keeps P9 quiet: {said}")
    _, _, _, said, _, _, failures = launch_through_p78(going, mode="attach-or-start", running=[zeta],
                                                       mutate={"zeta": "pane start failed with exit 6"})
    check(said == [] and failures == {"zeta": "pane start failed with exit 6"},
          f"a failure P7+P8 recorded is in the very dict P9 reads: {said} {failures}")


#: P9's launcher lookups, each read once, through the launcher (measured on the SYRD-344 baseline).
P9_SEAMS = ("unsafe_root_presentation_windows", "unsafe_presentation_report", "LAYOUT_MODE_VIEWER", "_role_cli_name",
            "record_provider_state_generation", "provider_state_generation", "report_launch_session_records")
P9_HOME = Path("/nonexistent/syrd344/owner-home")
P9_PANES = Path("/nonexistent/syrd344/effective-pane-state")


class Reports:
    """P9's launcher lookups, answering from objects this test owns, into one ordered log."""

    def __init__(self, *, unsafe: list | None = None, error: Exception | None = None):
        self.unsafe, self.error = unsafe or [], error
        self.log: list[tuple] = []

    def names(self) -> dict[str, object]:
        L = self.log

        def session_records(cfg, **kw):
            L.append(("records", kw))
            if self.error:
                raise self.error

        return dict(
            unsafe_root_presentation_windows=lambda cfg, *, config_path: L.append(("unsafe?", config_path))
            or self.unsafe,
            unsafe_presentation_report=lambda cfg, windows: L.append(("unsafe-report", windows)) or "SYRD344 UNSAFE",
            LAYOUT_MODE_VIEWER="SYRD344_VIEWER",
            _role_cli_name=lambda role: L.append(("cli", role.role)) or role.cli,
            provider_state_generation=lambda cli, *, owner_home: L.append(("generation", cli, owner_home))
            or f"gen-{cli}",
            record_provider_state_generation=lambda cfg, role, generation: L.append(("record", role.role, generation)),
            report_launch_session_records=session_records,
        )

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def p9_role(name: str, *, detached: bool = False, cli: str = "codex") -> SimpleNamespace:
    return SimpleNamespace(role=name, detached=detached, cli=cli)


def report(r: Reports, *, mode: str = "attach-or-start", layout: str = "SYRD344_SEPARATE", running: list | None = None,
           roles: list | None = None, failed: dict | None = None, unreconciled: set | None = None,
           home: object = P9_HOME, records: bool = False, code: object = None):
    from scripts import launch_phases, team_launcher

    said: list[str] = []
    cfg = SimpleNamespace(project="p344", roles=roles if roles is not None else [])
    kwargs = dict(config_path=CONFIG_PATH, effective_pane_state_dir=P9_PANES,
                  failed_roles=failed if failed is not None else {}, launch_started_at=344.25,
                  launch_started_ns=344_250_000_000, mode=mode, print_func=said.append, reconcile_home=home,
                  report_session_records=records, resolved_layout_mode=layout,
                  running_roles=running if running is not None else [], session_record_poll=0.344,
                  session_record_timeout=34.4, unreconciled_roles=unreconciled if unreconciled is not None else set(),
                  worker_start_exit_code=code if code is not None else Code(0))
    with patched(team_launcher, **r.names()):
        result = launch_phases._report_launch(cfg, **kwargs)
    return result, said, kwargs, cfg


def test_the_report_phase_reads_its_lookups_through_the_launcher() -> None:
    module = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_report_launch")
    for name in P9_SEAMS:
        uses = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(uses) == 1 and isinstance(uses[0].value, ast.Name) and uses[0].value.id == "launcher" and not bare,
              f"P9 reads {name} at its one site, through the launcher")
    lens = [n for n in ast.walk(function) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "len"]
    check(len(lens) == 1 and isinstance(lens[0].func, ast.Name), "len stays the builtin")
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound - {"launcher"} and through == [], f"nothing P9 binds is read as the launcher's: {through}")
    check(isinstance(function.body[-1], ast.Return) and ast.unparse(function.body[-1]) == "return worker_start_exit_code",
          "the phase ends in the launch's own return, unchanged")
    launcher_tree = launcher_with_launch_project()
    launch = next(n for n in launcher_tree.body if isinstance(n, ast.FunctionDef) and n.name == "launch_project")
    sites = [n for n in ast.walk(launcher_tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_report_launch"]
    check(len(sites) == 1 and isinstance(sites[0].func, ast.Name) and launch.body[-3].value is sites[0] and ast.unparse(launch.body[-2]).startswith(
          "report_kept_worktrees(config, launch_preparation.kept_worktrees") and ast.unparse(launch.body[-1]) == "return exit_code",
          "launch_project returns P9's answer, called by the launcher's own name, after the worktrees it kept (SYRD-555)")


def test_p9_an_unsafe_window_is_reported_and_nothing_is_announced() -> None:
    r = Reports(unsafe=["syrd344-window"])
    code = Code(3)
    result, said, _, _ = report(r, running=[p9_role("zeta")], code=code)
    check(r.log[:2] == [("unsafe?", CONFIG_PATH), ("unsafe-report", ["syrd344-window"])]
          and said == ["SYRD344 UNSAFE"],
          f"the unsafe windows are looked for first and their report said, and no attachment announced: {said}")
    check(result is code, f"the launch's own code comes back, the very object: {result!r}")


def test_p9_the_announcement_names_the_attached_visible_panes_in_order() -> None:
    running = [p9_role("zeta"), p9_role("bg", detached=True), p9_role("alpha"), p9_role("old")]
    r = Reports()
    _, said, _, _ = report(r, running=running, failed={"old": "busy"})
    check(said == ["switchyard: opened a new window attached to running panes: zeta, alpha; "
                   "the previous window may be closed if no longer needed"],
          f"visible, started roles in running order, plural, detached and failed ones left out: {said}")
    _, said, _, _ = report(Reports(), running=[p9_role("zeta"), p9_role("bg", detached=True)])
    check(said == ["switchyard: opened a new window attached to running pane: zeta; "
                   "the previous window may be closed if no longer needed"], f"one pane, singular: {said}")
    for label, kwargs in (("no running role", dict(running=[])),
                          ("only detached or failed ones", dict(running=[p9_role("bg", detached=True), p9_role("old")],
                                                                failed={"old": "busy"})),
                          ("a viewer", dict(running=[p9_role("zeta")], layout="SYRD344_VIEWER")),
                          ("a plain attach", dict(running=[p9_role("zeta")], mode="attach"))):
        _, said, _, _ = report(Reports(), **kwargs)
        check(said == [], f"{label}: nothing is announced: {said}")


def test_p9_provider_state_is_recorded_for_each_role_that_came_up() -> None:
    roles = [p9_role("zeta"), p9_role("old"), p9_role("stale"), p9_role("bare", cli=""), p9_role("alpha", cli="claude")]
    r = Reports()
    report(r, roles=roles, failed={"old": "busy"}, unreconciled={"stale"})
    check([e for e in r.log if e[0] in ("cli", "generation", "record")] == [
        ("cli", "zeta"), ("generation", "codex", P9_HOME), ("record", "zeta", "gen-codex"),
        ("cli", "bare"),
        ("cli", "alpha"), ("generation", "claude", P9_HOME), ("record", "alpha", "gen-claude")],
          f"in config order, against the reconcile home; failed, unreconciled and CLI-less roles keep their records: "
          f"{r.log}")
    for label, kwargs in (("no reconcile home", dict(home=None)), ("a plain attach", dict(mode="attach"))):
        r = Reports()
        report(r, roles=roles, **kwargs)
        check(not {"cli", "generation", "record"} & set(r.kinds()), f"{label}: nothing is recorded: {r.kinds()}")


def test_p9_the_session_report_gets_the_launchs_clock_and_directory() -> None:
    running = [p9_role("zeta")]
    for mode, attached in (("attach-or-start", running), ("attach", ())):
        r = Reports(); code = Code(4)
        result, said, kwargs, _ = report(r, mode=mode, running=running, records=True, code=code)
        got = next(e[1] for e in r.log if e[0] == "records")
        check(got == dict(timeout_seconds=34.4, poll_seconds=0.344, fallback_changed_since_ns=344_250_000_000,
                          pane_state_dir=P9_PANES, pane_state_updated_since=344.25, attached_roles=attached,
                          print_func=kwargs["print_func"])
              and (got["attached_roles"] is running if mode == "attach-or-start" else True),
              f"{mode}: the caller's timeout and poll, the launch's clock, the effective directory, and the attached "
              f"roles: {got}")
        check(r.kinds()[-1] == "records" and result is code, f"reported last, and the code still comes back: {result!r}")
    r = Reports(); code = Code(5)
    result, _, _, _ = report(r, running=running, records=False, code=code)
    check("records" not in r.kinds() and result is code, f"not asked for, no report, and the same code: {r.kinds()}")


def test_p9_an_error_reaches_the_caller() -> None:
    boom = OSError("syrd344: records")
    r = Reports(error=boom)
    try:
        report(r, records=True); raised = None
    except OSError as exc:
        raised = exc
    check("records" in r.kinds(), f"asked for, the session report is made: {r.kinds()}")
    check(raised is boom and r.kinds()[-1] == "records", f"a failing report is not swallowed: {r.kinds()}")


def test_the_launch_returns_p9s_answer_with_the_state_before_it() -> None:
    from scripts import team_launcher
    from scripts.launch_phases import WorkerStartup
    going = WorkerStartup(worker_start_exit_code=Code(6), launch_started_at=344.25,
                          launch_started_ns=344_250_000_000, resolved_layout_mode="SYRD344_SEPARATE")
    answer = Code(8)
    asked: list[tuple] = []
    with patched(team_launcher, _report_launch=lambda c, **k: asked.append((c, k)) or answer):
        got, p78, _, _, setup, prepared, failures = launch_through_p78(going, mode="attach-or-start", report=True)
    check(got is answer and len(asked) == 1 and asked[0][0] is prepared,
          f"launch_project returns P9's answer itself, P9 given P5's config: {got!r}")
    k = asked[0][1]
    check(k == dict(config_path=CONFIG_PATH, effective_pane_state_dir=EFFECTIVE, failed_roles=failures,
                    launch_started_at=344.25, launch_started_ns=344_250_000_000, mode="attach-or-start",
                    print_func=k["print_func"], reconcile_home=None, report_session_records=True,
                    resolved_layout_mode="SYRD344_SEPARATE", running_roles=[],
                    session_record_poll=team_launcher.LAUNCH_SESSION_RECORD_POLL_SECONDS,
                    session_record_timeout=team_launcher.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
                    unreconciled_roles=set(), worker_start_exit_code=going.worker_start_exit_code)
          and k["failed_roles"] is failures and k["worker_start_exit_code"] is going.worker_start_exit_code
          and k["failed_roles"] is p78[0][1]["failed_roles"],
          f"P9 gets P7+P8's four values and the very failures dict they wrote into: {k}")
    asked.clear()
    stop = Code(7)
    with patched(team_launcher, _report_launch=lambda c, **k: asked.append((c, k)) or answer):
        got, _, _, _, _, _, _ = launch_through_p78(stop)
    check(got is stop and asked == [], f"a stop in P7+P8 never reaches P9: {got!r} {asked}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_call_site_the_seams_and_the_phases_own_names",
             "test_the_preparation_reads_every_launcher_lookup_through_the_launcher",
             "test_the_layout_phase_reads_its_lookups_through_the_launcher",
             "test_the_worker_phase_reads_its_lookups_through_the_launcher",
             "test_the_report_phase_reads_its_lookups_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"launch_phases_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
