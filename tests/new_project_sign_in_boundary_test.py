#!/usr/bin/env python3
"""SYRD-373: `switchyard new`'s sign-in phase, against the command it came out of.

The eleven statements from `stages.begin("provider sign-in and folder trust")`
through the deferred launch's handoff message -- with the SYRD-246 rationale
above them -- moved into `_run_new_project_sign_in` in
`scripts/new_project_phases.py`. Like P3, it has early exits: three stops, each
the command's own `return 1`, kept as they were. Otherwise it returns a frozen
`NewProjectSignIn`, and the command goes on only when it got one. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first.
- **Seams (rule 24)**, as often as the command read them; nothing bound.
- **The stops**, in their order -- an owner missing a CLI, a provider not
  signed in, a model still unknown after the owner's confirmation -- each the
  command's 1, with nothing after it and nothing of P5.
- **The behaviour is unchanged:** the watched sign-in (the first-run runner as
  the caller passed it, everything else the plain runner), the confirmation
  replacing the configuration and the report, the notice, the launch runner,
  and the deferred launch with its handoff, found or not.
- **The caller** hands the phase its own values and the earlier phases'
  objects, reads back all four answers, and goes on to P5.

Every facility is this test's own fake. No provider is signed in to, no shell
or window is opened, and the command is stopped by a fake at P5's first step.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import new_project_phases as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
#: Measured on the baseline command's eleven P4 statements.
SEAMS = {"_control_repository_owned_roots": 1, "_owner_home_for_auth": 1, "_owner_project_git_runner": 1,
         "confirm_unknown_models_with_owner": 1, "foreground_runner_for": 1, "publish_role_account_migration": 1,
         "report_models_were_not_probed": 1, "role_isolation_gaps": 1, "run_first_run_auth_phase": 1,
         "stop_before_launch_for_missing_owner_clis": 1, "stop_before_launch_for_unauthenticated_providers": 1,
         "stop_before_launch_for_unknown_models": 1}
PARAMETERS = ("home_base", "euid_getter", "input_func", "print_func", "interactive")
FROM_P0 = ("first_run_runner", "owner_user", "project_dir", "resolved_slug", "runner", "stages")
FROM_P3 = ("config", "config_path")
OUTPUTS = ("config", "first_run_auth_report", "launch_deferred", "launch_runner")
STOPS = ("stop_before_launch_for_missing_owner_clis", "stop_before_launch_for_unauthenticated_providers",
         "stop_before_launch_for_unknown_models")
OWNER = "syrd373-agent"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- SystemExit is an answer here, and so is anything a mutant raises
        return exc


class patched:
    """Rebind attributes of one object for one block, as the suites do."""

    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


class Stages:
    def __init__(self, log: list) -> None:
        self.log = log

    def begin(self, name: str, **kwargs: object) -> None:
        self.log.append(("begin", name, kwargs))


class World:
    """One run's fakes, recording in order; `stop` names the stop that answers True, `fail` the step that raises."""

    def __init__(self, *, stop: str | None = None, fail: str | None = None, gaps: list | None = None,
                 handoff: tuple = (Path("/fixture/handoff.sh"), [])) -> None:
        self.stop, self.fail, self.gaps, self.handoff, self.log = stop, fail, gaps or [], handoff, []
        self.auth_home, self.foreground = Path("/fixture/auth-home"), SimpleNamespace(name="foreground")
        self.report, self.confirmed_report = SimpleNamespace(name="report"), SimpleNamespace(name="confirmed report")
        self.confirmed = SimpleNamespace(project="s373-confirmed")
        self.roots, self.launch_runner = ("root-a",), SimpleNamespace(name="launch runner")

    def step(self, name: str, *detail: object) -> None:
        self.log.append((name, *detail))
        if self.fail == name:
            raise SystemExit(f"refused at {name}")

    def seams(self) -> dict:
        L = self

        def stop(label):
            return lambda report, **kw: L.step(label, report, kw) or L.stop == label

        return dict(
            _owner_home_for_auth=lambda owner, *, fallback: L.step("auth-home", owner, fallback) or L.auth_home,
            foreground_runner_for=lambda runner: L.step("foreground", runner) or L.foreground,
            run_first_run_auth_phase=lambda config, **kw: L.step("auth", config, kw) or L.report,
            stop_before_launch_for_missing_owner_clis=stop("missing-clis"),
            stop_before_launch_for_unauthenticated_providers=stop("unauthenticated"),
            confirm_unknown_models_with_owner=lambda config, report, **kw: L.step("confirm", config, report, kw) or (L.confirmed, L.confirmed_report),
            stop_before_launch_for_unknown_models=stop("unknown-models"),
            report_models_were_not_probed=lambda config, *, print_func: L.step("not-probed", config, print_func),
            _control_repository_owned_roots=lambda config: L.step("owned-roots", config) or L.roots,
            _owner_project_git_runner=lambda **kw: L.step("git-runner", kw) or L.launch_runner,
            role_isolation_gaps=lambda config: L.step("isolation", config) or L.gaps,
            publish_role_account_migration=lambda config, **kw: L.step("handoff", config, kw) or L.handoff,
        )

    def names(self) -> list:
        return [entry[0] for entry in self.log]


def inputs(tmp: Path, world: World, said: list, **overrides: object) -> dict:
    values = dict(home_base=tmp / "homes", euid_getter=lambda: 0, input_func=refuse("the terminal"), print_func=said.append,
                  interactive=False, first_run_runner=SimpleNamespace(name="as the caller passed it"), owner_user=OWNER,
                  project_dir=tmp / "project", resolved_slug="s373", runner=lambda *a, **k: None, stages=Stages(world.log),
                  config=SimpleNamespace(project="s373-before"), config_path=tmp / "provision" / "s373.json")
    values.update(overrides)
    return values


def run(world: World, values: dict) -> object:
    with patched(t, **world.seams()):
        return judged(m._run_new_project_sign_in, **values)


def module_tree() -> ast.Module:
    return ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))


def phase_def() -> ast.FunctionDef:
    return next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_run_new_project_sign_in")


#: The six phases and two continuation types `switchyard new` reads through the launcher (SYRD-425).
NEW_COMMAND_READS = ("_resolve_new_project_choices", "_check_new_project_preflight", "_prepare_new_project_accounts",
                     "_prepare_new_project_board", "_run_new_project_sign_in", "_launch_new_project_panes",
                     "NewProjectBoard", "NewProjectSignIn")


def command_def() -> ast.FunctionDef:
    """`switchyard_new_command` as its phases' wiring sees it.

    SYRD-425 moved it to scripts/switchyard_new_command.py, where it reads each phase and continuation type through
    the launcher when it runs. Checked first, on the source as it is: the launcher no longer defines it, re-exports it
    unaliased and still dispatches `new` to that name; its first statement is the call-time launcher import; and every
    phase and continuation is read through the launcher, none bare. Only then is the import dropped and each
    `launcher.X` read as `X`, so the positions and names below are the command's own. Before the move (the baseline)
    it is the launcher's definition as it stands.
    """
    moved = ROOT / "scripts" / "switchyard_new_command.py"
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    tree = ast.parse(moved.read_text(encoding="utf-8")) if moved.exists() else launcher_tree
    command = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command")
    if moved.exists():
        check(not any(isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command" for n in launcher_tree.body),
              "the launcher no longer defines switchyard_new_command")
        check(any(isinstance(n, ast.ImportFrom) and n.module == "scripts.switchyard_new_command"
                  and any(a.name == "switchyard_new_command" and a.asname is None for a in n.names) for n in launcher_tree.body),
              "the launcher re-exports it, unaliased")
        main = next(n for n in launcher_body(ROOT, launcher_tree) if isinstance(n, ast.FunctionDef) and n.name == "switchyard_main")
        check(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "switchyard_new_command" for n in ast.walk(main)),
              "and switchyard_main still dispatches `new` to the launcher's name")
        check(ast.unparse(command.body[0]) == "from scripts import team_launcher as launcher",
              f"the command imports the launcher first thing, when it runs: {ast.unparse(command.body[0])}")
        bare = sorted({n.id for n in ast.walk(command) if isinstance(n, ast.Name) and n.id in NEW_COMMAND_READS})
        through = sorted({n.attr for n in ast.walk(command)
                          if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher"})
        check(bare == [] and through == sorted(NEW_COMMAND_READS),
              f"every phase and continuation is read through the launcher, none bare: {bare} {through}")
        del command.body[0]

        class AsLauncherGlobal(ast.NodeTransformer):
            def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
                self.generic_visit(node)
                if isinstance(node.value, ast.Name) and node.value.id == "launcher":
                    return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
                return node

        AsLauncherGlobal().visit(command)
    return command


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.new_project_phases as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    names = ("_run_new_project_sign_in", "NewProjectSignIn", "_prepare_new_project_board", "_prepare_new_project_accounts",
             "_check_new_project_preflight", "_resolve_new_project_choices")
    for order in (("scripts.new_project_phases", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_phases")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_phases as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {names!r}))")
        check(result.stdout.strip() == "True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_and_nothing_bound_read_through_the_launcher() -> None:
    fn = phase_def()
    check(ast.unparse(fn.body[0]) == "from scripts import team_launcher as launcher", "the launcher imported first, when it runs")
    through: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "launcher":
            through[node.attr] = through.get(node.attr, 0) + 1
    check(through == SEAMS, f"each launcher name read through it exactly as often as the command read it: {through}")
    bare = sorted({n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in SEAMS})
    check(bare == [], f"and none of them read past it: {bare}")
    bound = {a.arg for a in fn.args.kwonlyargs} | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    check(not bound & set(through), f"nothing the phase binds is read through the launcher: {bound & set(through)}")
    check(ast.unparse(fn.body[1]) == "stages.begin('provider sign-in and folder trust', waits_for_you=True)", "the stage begun first")
    stops = [ast.unparse(s) for s in fn.body if isinstance(s, ast.If) and ast.unparse(s.body) == "return 1"]
    check([next(n for n in STOPS if n in s) for s in stops] == list(STOPS) and all(s.endswith("\n    return 1") for s in stops),
          f"the three stops, each the command's own 1, in their order: {stops}")
    source = ast.get_source_segment((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"), fn)
    check("# No live model probe here, deliberately." in source.split("stages.begin(")[0], "the SYRD-246 rationale above the stage, inside the phase")


def test_the_interface_and_the_result() -> None:
    fn = phase_def()
    args = fn.args
    check(not args.args and not args.posonlyargs and not args.vararg and not args.kwarg
          and tuple(a.arg for a in args.kwonlyargs) == PARAMETERS + FROM_P0 + FROM_P3 and all(d is None for d in args.kw_defaults),
          f"keyword-only: the command's parameters, P0's values, P3's; no defaults: {[a.arg for a in args.kwonlyargs]}")
    theirs = {a.arg: ast.unparse(a.annotation) for a in command_def().args.kwonlyargs}
    for cls in ("NewProjectChoices", "NewProjectBoard"):
        node = next(n for n in module_tree().body if isinstance(n, ast.ClassDef) and n.name == cls)
        theirs.update({s.target.id: ast.unparse(s.annotation) for s in node.body if isinstance(s, ast.AnnAssign)})
    check(all(ast.unparse(a.annotation) == theirs[a.arg] for a in args.kwonlyargs), "each annotated as where it comes from")
    check(ast.unparse(fn.returns) == "NewProjectSignIn | int", f"a record, or the command's 1: {ast.unparse(fn.returns)}")
    fields = dataclasses.fields(m.NewProjectSignIn)
    check(tuple(f.name for f in fields) == OUTPUTS and all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
                                                            for f in fields) and m.NewProjectSignIn.__dataclass_params__.frozen,
          "four frozen fields, no defaults")
    returns = sorted(ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Return))
    check(returns == ["return 1"] * 3 + ["return NewProjectSignIn(config=config, first_run_auth_report=first_run_auth_report, "
                                          "launch_deferred=launch_deferred, launch_runner=launch_runner)"], f"three stops and the record: {returns}")


def test_the_command_calls_it_and_returns_its_stops() -> None:
    body = command_def().body
    at = next(i for i, s in enumerate(body) if isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "new_project_sign_in")
    check(ast.unparse(body[at - 1]) == "config_path = new_project_board.config_path", "right after P3's read-backs")
    call = body[at].value
    check(ast.unparse(call.func) == "_run_new_project_sign_in" and not call.args
          and [(k.arg, ast.unparse(k.value)) for k in call.keywords] == [(n, n) for n in PARAMETERS + FROM_P0 + FROM_P3],
          "called by the launcher's name, with the command's own values")
    check(ast.unparse(body[at + 1]) == "if not isinstance(new_project_sign_in, NewProjectSignIn):\n    return new_project_sign_in",
          "a stop is returned as it came")
    check([ast.unparse(s) for s in body[at + 2:at + 6]] == [f"{f} = new_project_sign_in.{f}" for f in OUTPUTS], "all four read back")
    after = body[at + 6]
    if ast.unparse(after) != "stages.begin('role panes')":
        # SYRD-374: P5 is the command's tail phase, returned, and it begins the stage first.
        panes = next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_launch_new_project_panes")
        check(isinstance(after, ast.Return) and isinstance(after.value, ast.Call)
              and ast.unparse(after.value.func) == "_launch_new_project_panes"
              and ast.unparse(panes.body[1]) == "stages.begin('role panes')",
              f"then the P5 phase, returned, which begins it first: {ast.unparse(after)[:80]}")
    else:
        check(True, "then P5 begins")


# --- behaviour -------------------------------------------------------------------------------------------------------


def test_the_confirmation_is_as_interactive_as_the_caller_said() -> None:
    for interactive in (True, False, None):
        with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
            tmp = Path(tmp)
            world, said = World(), []
            run(world, inputs(tmp, world, said, interactive=interactive))
            kw = next(e for e in world.log if e[0] == "confirm")[3]
            check(kw["interactive"] is interactive, f"{interactive!r} passed on as itself: {kw['interactive']!r}")


def test_the_sign_in_and_everything_after_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        world, said = World(), []
        values = inputs(tmp, world, said, interactive=True)
        got = run(world, values)
        check(isinstance(got, m.NewProjectSignIn), f"it goes on: {got!r}")
        check(world.names() == ["begin", "auth-home", "foreground", "auth", "missing-clis", "unauthenticated", "confirm", "unknown-models",
                                "not-probed", "owned-roots", "git-runner", "isolation"], f"the order: {world.names()}")
        check(world.log[0] == ("begin", "provider sign-in and folder trust", {"waits_for_you": True}), "a stage that waits for you")
        check(world.log[1] == ("auth-home", OWNER, tmp / "homes" / OWNER), "the owner's home, falling back under the home base")
        check(world.log[2][1] is values["first_run_runner"], "the watched window chosen from exactly what the caller passed")
        config, kw = world.log[3][1:]
        check(config is values["config"] and kw == dict(owner_user=OWNER, owner_home=world.auth_home, runner=values["runner"],
                                                         foreground_runner=world.foreground, print_func=said.append),
              f"signed in with the plain runner and the foreground one: {kw}")
        check(world.log[4][1] is world.report and world.log[5][1] is world.report, "both stops read the sign-in's report")
        c, r, kw = world.log[6][1:]
        check(c is values["config"] and r is world.report and kw == dict(config_path=values["config_path"], runner=values["runner"],
                                                                        interactive=True, input_func=values["input_func"],
                                                                        print_func=said.append), f"the owner confirms: {kw}")
        check(world.log[7][1] is world.confirmed_report and world.log[7][2] == dict(project="s373-confirmed", print_func=said.append),
              "the unknown-model stop reads the confirmed report, for the confirmed configuration's project")
        check(world.log[8][1] is world.confirmed and world.log[9][1] is world.confirmed, "the notice and the roots from the confirmed configuration")
        check(world.log[10][1] == dict(owner_user=OWNER, project_dir=tmp / "project", owned_roots=world.roots, runner=values["runner"]),
              "the launch runner for the owner's project")
        check(world.log[11][1] is world.confirmed, "isolation of the confirmed configuration")
        check((got.config, got.first_run_auth_report, got.launch_runner) == (world.confirmed, world.confirmed_report, world.launch_runner)
              and got.config is world.confirmed and got.first_run_auth_report is world.confirmed_report and got.launch_deferred is False,
              "the confirmation's configuration and report, the launch runner, not deferred")


def test_each_stop_is_the_commands_1_with_nothing_after() -> None:
    order = ["begin", "auth-home", "foreground", "auth", "missing-clis", "unauthenticated", "confirm", "unknown-models"]
    for stop in ("missing-clis", "unauthenticated", "unknown-models"):
        with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
            tmp = Path(tmp)
            world, said = World(stop=stop), []
            got = run(world, inputs(tmp, world, said))
            check(got == 1 and type(got) is int and world.names() == order[:order.index(stop) + 1],
                  f"{stop}: 1, and nothing after it: {got!r} {world.names()}")


def test_a_deferred_launch_and_its_handoff() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        world, said = World(gaps=["role main has no account", "role audit has no account"]), []
        values = inputs(tmp, world, said)
        got = run(world, values)
        check(got.launch_deferred is True, "deferred while roles are not isolated")
        config, kw = world.log[-1][1:]
        check(world.names()[-1] == "handoff" and config is world.confirmed
              and kw == dict(config_path=values["config_path"], euid_getter=values["euid_getter"], print_func=said.append),
              f"the handoff published for the confirmed configuration: {kw}")
        check(said == ["switchyard: provisioned s373. Its roles are not isolated yet, so they were not started:\n"
                       "  role main has no account\n  role audit has no account\n"
                       "Run /fixture/handoff.sh as an operator (safe to re-run), then start it with `switchyard s373`."],
              f"the next step: {said}")
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        world, said = World(gaps=["role main has no account"], handoff=(None, ["root cannot read it", "no sudo"])), []
        run(world, inputs(tmp, world, said))
        check(said == ["switchyard: provisioned s373. Its roles are not isolated yet, so they were not started:\n"
                       "  role main has no account\n"
                       "Its role-account migration was not published where root can run it, so there is nothing to hand you yet: "
                       "root cannot read it; no sudo"], f"no handoff: the problems instead: {said}")


def test_every_refusal_stops_what_follows() -> None:
    order = ["begin", "auth-home", "foreground", "auth", "missing-clis", "unauthenticated", "confirm", "unknown-models",
             "not-probed", "owned-roots", "git-runner", "isolation", "handoff"]
    for step in order[1:]:
        with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
            tmp = Path(tmp)
            world, said = World(fail=step, gaps=["a gap"]), []
            got = run(world, inputs(tmp, world, said))
            check(isinstance(got, SystemExit) and got.code == f"refused at {step}" and world.names() == order[:order.index(step) + 1]
                  and not said, f"refused at {step}, and nothing after it: {got!r} {world.names()} {said}")


def test_the_answer_cannot_be_changed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        world, said = World(), []
        got = run(world, inputs(tmp, world, said))
        check(isinstance(judged(setattr, got, "launch_deferred", True), dataclasses.FrozenInstanceError), "frozen")


# --- the caller ------------------------------------------------------------------------------------------------------


class Fence(Exception):
    """P5 reached: stop there."""


def command_locals(exc: BaseException) -> dict:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is t.switchyard_new_command.__code__:
            return dict(tb.tb_frame.f_locals)
        tb = tb.tb_next
    raise AssertionError("the command's frame is not on the way to the fence")


def earlier(tmp: Path, log: list) -> tuple:
    p0 = m.NewProjectChoices(**{f.name: object() for f in dataclasses.fields(m.NewProjectChoices)})
    p0 = dataclasses.replace(p0, stages=Stages(log), owner_user=OWNER, project_dir=tmp / "project", resolved_slug="s373")
    p1 = m.NewProjectPreflight(**{f.name: object() for f in dataclasses.fields(m.NewProjectPreflight)})
    p2 = m.NewProjectAccounts(provision_dir=tmp / "provision")
    p3 = m.NewProjectBoard(config=SimpleNamespace(project="s373-before"), config_path=tmp / "provision" / "s373.json")
    return p0, p1, p2, p3


def p5_fence(seen: list) -> dict:
    def staged(config, **kw):
        seen.append(("staging", config, kw))
        raise Fence()
    return dict(ensure_staged_role_tooling=staged, launch_project=refuse("the panes"))


def test_the_command_hands_p4_its_values_and_reads_back_all_four() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2, p3 = earlier(tmp, log)
        answer = m.NewProjectSignIn(config=object(), first_run_auth_report=object(), launch_deferred=object(), launch_runner=object())
        handed: list = []
        seen: list = []
        given = {name: object() for name in PARAMETERS}
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: p2, _prepare_new_project_board=lambda **kw: p3,
                     _run_new_project_sign_in=lambda **kw: handed.append(kw) or answer, **p5_fence(seen)):
            got = judged(t.switchyard_new_command, **given)
        check(isinstance(got, Fence), f"stopped at P5: {got!r}")
        check(len(handed) == 1 and tuple(handed[0]) == PARAMETERS + FROM_P0 + FROM_P3, f"P4 called once, with its 13 values: {handed}")
        wrong = ([n for n in PARAMETERS if handed[0][n] is not given[n]] + [n for n in FROM_P0 if handed[0][n] is not getattr(p0, n)]
                 + [n for n in FROM_P3 if handed[0][n] is not getattr(p3, n)])
        check(wrong == [], f"each the very value: the command's own, P0's or P3's: {wrong}")
        local = command_locals(got)
        wrong = [f for f in OUTPUTS if local.get(f, Fence) is not getattr(answer, f)]
        check(wrong == [], f"all four are the command's, the very objects: {wrong}")
        check(local["first_run_runner"] is p0.first_run_runner and local["runner"] is p0.runner, "P0's two runners untouched")
        check(log == [("begin", "role panes", {})] and seen[0][1] is answer.config, f"then P5 begins, with the confirmed configuration: {log}")


def test_every_stop_leaves_the_command_before_p5() -> None:
    for stop in ("missing-clis", "unauthenticated", "unknown-models"):
        with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
            tmp = Path(tmp)
            log: list = []
            p0, p1, p2, p3 = earlier(tmp, log)
            world = World(stop=stop)
            world.log = log
            with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                         _prepare_new_project_accounts=lambda **kw: p2, _prepare_new_project_board=lambda **kw: p3,
                         **world.seams(), **p5_fence([])):
                got = judged(t.switchyard_new_command, home_base=tmp / "homes", print_func=lambda line: None)
            check(got == 1 and type(got) is int and not any(e[0] == "begin" and e[1] == "role panes" for e in log),
                  f"{stop}: the command returns 1 and P5 never begins: {got!r} {[e[0] for e in log]}")


def test_the_command_returns_the_phases_own_answer() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2, p3 = earlier(tmp, log)
        answer = SimpleNamespace(name="whatever the phase answered")
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: p2, _prepare_new_project_board=lambda **kw: p3,
                     _run_new_project_sign_in=lambda **kw: answer, **p5_fence([])):
            got = judged(t.switchyard_new_command, home_base=tmp / "homes", print_func=lambda line: None)
        check(got is answer and not log, f"the phase's answer, itself, and P5 never begins: {got!r} {log}")


def test_the_real_phase_between_p3_and_p5() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd373.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2, p3 = earlier(tmp, log)
        world = World()
        world.log = log
        seen: list = []
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: p2, _prepare_new_project_board=lambda **kw: p3,
                     **world.seams(), **p5_fence(seen)):
            got = judged(t.switchyard_new_command, home_base=tmp / "homes", print_func=lambda line: None)
        check(isinstance(got, Fence), f"stopped at P5: {got!r}")
        local = command_locals(got)
        check(local["config"] is world.confirmed and local["first_run_auth_report"] is world.confirmed_report
              and local["launch_runner"] is world.launch_runner and local["launch_deferred"] is False,
              "the confirmed configuration and report, the launch runner, not deferred")
        auth = next(e for e in log if e[0] == "auth")
        foreground = next(e for e in log if e[0] == "foreground")
        check(auth[1] is p3.config and auth[2]["runner"] is p0.runner and foreground[1] is p0.first_run_runner,
              "signed in on P3's configuration, the plain runner, and the watched window from the caller's own")
        check(seen[0][1] is world.confirmed and log[-1] == ("begin", "role panes", {}), "and P5 stages the confirmed configuration")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_nothing_bound_read_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"new_project_sign_in_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
