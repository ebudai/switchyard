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
EXPORTED = ("LaunchSetup", "_launch_runners_and_paths", "LaunchPreparation", "_prepare_launch")
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
    from scripts.launch_phases import LaunchPreparation

    def inert_p5(c: object, **k: object) -> LaunchPreparation:
        # P5 is inert in a dry run; standing it in keeps any real preparation code out of this case.
        return LaunchPreparation(exit_code=None, config=c, failed_roles={}, running_roles=[], reconcile_home=None,
                                 unreconciled_roles=set())

    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: sentinel, _prepare_launch=inert_p5,
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


#: P5's launcher lookups, each read once, through the launcher (measured).
P5_SEAMS = ("LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV", "NO_LAUNCHER_SELF_DEPLOY_ENV",
            "_drop_roles_with_stale_provider_runtime", "_env_truthy_any", "_owner_home_for_auth",
            "_prepare_project_worktrees_for_launch", "_running_project_roles",
            "current_user_name", "ensure_configured_runtime_user", "ensure_generated_project_board_skill",
            "ensure_generated_project_pane_hooks", "ensure_launcher_checkout_current", "ensure_owner_state_dirs",
            "fetch_project_worktree_ref", "prepare_project_desktop", "role_isolation_gaps",
            "seed_default_session_dir_from_legacy_sources", "sync_reload_config_to_live_sessions")
P5_OUTPUTS = ("exit_code", "config", "failed_roles", "running_roles", "reconcile_home", "unreconciled_roles")
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
        self.worktree_result = SimpleNamespace(failed_roles=dict(self.failed))
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
            _prepare_project_worktrees_for_launch=lambda cfg, *, running_roles, runner: L.append(
                ("worktrees", [r.role for r in running_roles], runner)) or self.worktree_result,
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
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
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
          and entries["worktrees"] == ("worktrees", ["alpha"], worktree_runner),
          f"state, hooks and skill from the phase's paths; worktrees through the worktree runner: {prep.log}")
    check(result.exit_code is None and result.config is cfg and result.failed_roles is prep.worktree_result.failed_roles
          and result.running_roles is prep.kept and result.reconcile_home is OWNER_HOME
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
    import contextlib
    import io

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
    printed = io.StringIO()
    with patched(team_launcher, _launch_runners_and_paths=lambda c, **k: setup,
                 _prepare_launch=lambda c, **k: going_on,
                 materialize_layout=lambda c, **k: laid_out.append((c, k["failed_roles"]))), \
            contextlib.redirect_stdout(printed):
        result = team_launcher.launch_project(cfg, config_path=CONFIG_PATH, mode="attach", script_path=SCRIPT,
                                              runner=caller_runner, dry_run=True, assign_layout_owner=False)
    check(result == 0 and laid_out == [(prepared, going_on.failed_roles)]
          and laid_out[0][1] is going_on.failed_roles,
          f"going on, the layout is written from the prepared config and failures, the same objects: {laid_out}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_call_site_the_seams_and_the_phases_own_names",
             "test_the_preparation_reads_every_launcher_lookup_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"launch_phases_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
