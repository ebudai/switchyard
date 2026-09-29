#!/usr/bin/env python3
"""SYRD-374: `switchyard new`'s tail -- the role panes -- against the command it came out of.

The fourteen statements from `stages.begin("role panes")` to the command's
last `return 0` -- with the SYRD-249 staging rationale above them -- moved
into `_launch_new_project_panes` in `scripts/new_project_phases.py`, and the
command now returns that phase's answer. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first.
- **Seams (rule 24)**, as often as the command read them, the clock included;
  nothing bound; `Path` the module's own.
- **The launcher's own file.** The launch still names the launcher script
  beside `scripts/team_launcher.py` -- `launcher.__file__`, never this
  module's -- and reads it only when a launch actually happens.
- **The three answers:** staging problems are the command's 1 before any clock
  or window; a failed launch's own status is returned as itself, compared once;
  a finished tail is 0, the stages finished first.
- **The behaviour is unchanged:** the clocks and what they stamp, every launch
  argument, the deferred launch (no window, no announcement, no records), the
  layout, the warnings, the pane-state directory, and the designer instruction.
- **The caller** returns the phase's answer as itself, having handed it P4's
  replacements and the runners unchanged.

Every facility is this test's own fake, the clock included. No window, pane,
tmux server or Konsole is opened; the command is exercised only with its
earlier phases stood in.
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
#: Measured on the baseline command's fourteen P5 statements (`__file__` read as the launcher's).
SEAMS = {"LAYOUT_MODE_VIEWER": 1, "TEAM_LAUNCHER_NAME": 1, "__file__": 1, "_owner_state_layout_output_path": 1,
         "announce_new_project_presentation": 1, "default_pane_state_dir_for_user": 1, "ensure_staged_role_tooling": 1,
         "launch_project": 1, "report_first_run_auth_warnings": 1, "report_launch_session_records": 1, "resolve_layout_mode": 1,
         "time": 2}
PARAMETERS = ("home_base", "pane_state_dir", "session_record_timeout", "session_record_poll", "layout_mode", "layout_environ",
              "konsole_process_launcher", "print_func")
FROM_P0 = ("include_designer", "owner_user", "resolved_slug", "runner", "stages")
FROM_P3 = ("config_path",)
FROM_P4 = ("config", "first_run_auth_report", "launch_deferred", "launch_runner")
OWNER = "syrd374-agent"
VIEWER_DESIGN = "switchyard: maximize the designer pane during design with Ctrl+a z; press it again to restore"
NATIVE_DESIGN = "switchyard: maximize the designer pane during design with Konsole Ctrl+Shift+E; restore it when done"
NO_DESIGN = "switchyard: design phase skipped; no designer pane configured"


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


class Tripwire:
    """A launcher `__file__` that must not be read: turning it into a path fails the test."""

    def __fspath__(self) -> str:
        raise AssertionError("the launcher's file was read without a launch")


class Status:
    """A launch status other than an int, counting how often it is compared."""

    def __init__(self, zero: bool, truth: bool) -> None:
        self.zero, self.truth, self.asked = zero, truth, 0

    def __ne__(self, other: object) -> bool:
        self.asked += 1
        return not (self.zero and other == 0)

    def __eq__(self, other: object) -> bool:
        self.asked += 1
        return self.zero and other == 0

    def __bool__(self) -> bool:
        return self.truth

    __hash__ = object.__hash__


class Stages:
    def __init__(self, log: list) -> None:
        self.log = log

    def begin(self, name: str, **kwargs: object) -> None:
        self.log.append(("begin", name))

    def finish(self) -> None:
        self.log.append(("finish",))


class World:
    """One run's fakes, recording in order; `fail` names the step that raises."""

    def __init__(self, tmp: Path, *, problems: list | None = None, status: object = 0, mode: str = "native",
                 fail: str | None = None, launcher_file: object = None) -> None:
        self.tmp, self.problems, self.status, self.mode, self.fail, self.log = tmp, problems or [], status, mode, fail, []
        self.launcher_file = launcher_file if launcher_file is not None else str(tmp / "installed" / "scripts" / "team_launcher.py")
        self.layout, self.default_state = tmp / "state" / "layout.json", tmp / "state" / "default-panes"

    def step(self, name: str, *detail: object) -> None:
        self.log.append((name, *detail))
        if self.fail == name:
            raise SystemExit(f"refused at {name}")

    def clock(self) -> SimpleNamespace:
        return SimpleNamespace(time=lambda: self.step("time") or 374.25, time_ns=lambda: self.step("time_ns") or 374_250_000_000)

    def seams(self) -> dict:
        L = self
        return {
            "ensure_staged_role_tooling": lambda config, *, runner, print_func: L.step("staging", config, runner) or L.problems,
            "time": L.clock(),
            "launch_project": lambda config, **kw: L.step("launch", config, kw) or L.status,
            "_owner_state_layout_output_path": lambda slug, *, owner_home: L.step("layout-output", slug, owner_home) or L.layout,
            "resolve_layout_mode": lambda mode, *, environ, runner: L.step("resolve", mode, environ, runner) or L.mode,
            "announce_new_project_presentation": lambda slug, *, resolved_layout_mode, print_func: L.step("announce", slug, resolved_layout_mode),
            "report_first_run_auth_warnings": lambda report, *, print_func: L.step("warnings", report),
            "report_launch_session_records": lambda config, **kw: L.step("records", config, kw),
            "default_pane_state_dir_for_user": lambda user, *, project: L.step("default-state", user, project) or L.default_state,
            "__file__": L.launcher_file,
        }

    def names(self) -> list:
        return [entry[0] for entry in self.log]


def inputs(tmp: Path, world: World, said: list, **overrides: object) -> dict:
    values = dict(home_base=tmp / "homes", pane_state_dir=tmp / "panes", session_record_timeout=37.4, session_record_poll=0.374,
                  layout_mode="auto", layout_environ={"XDG_SESSION_TYPE": "wayland"}, konsole_process_launcher=lambda *a, **k: None,
                  print_func=said.append, include_designer=True, owner_user=OWNER, resolved_slug="s374",
                  runner=lambda *a, **k: None, stages=Stages(world.log), config_path=tmp / "provision" / "s374.json",
                  config=SimpleNamespace(project="s374", run_as_user=OWNER), first_run_auth_report=SimpleNamespace(name="report"),
                  launch_deferred=False, launch_runner=SimpleNamespace(name="launch runner"))
    values.update(overrides)
    return values


def run(world: World, values: dict) -> object:
    with patched(t, **world.seams()):
        return judged(m._launch_new_project_panes, **values)


def module_tree() -> ast.Module:
    return ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))


def phase_def() -> ast.FunctionDef:
    return next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_launch_new_project_panes")


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
    names = ("_launch_new_project_panes", "_run_new_project_sign_in", "_prepare_new_project_board", "_prepare_new_project_accounts",
             "_check_new_project_preflight", "_resolve_new_project_choices")
    for order in (("scripts.new_project_phases", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_phases")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_phases as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {names!r}), t.Path is m.Path)")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_and_nothing_bound_read_through_the_launcher() -> None:
    fn = phase_def()
    check(ast.unparse(fn.body[0]) == "from scripts import team_launcher as launcher", "the launcher imported first, when it runs")
    through: dict[str, int] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "launcher":
            through[node.attr] = through.get(node.attr, 0) + 1
    check(through == SEAMS, f"each launcher name read through it exactly as often as the command read it: {through}")
    bare = sorted({n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in SEAMS})
    check(bare == [], f"and none of them read past it -- no bare `__file__` above all: {bare}")
    bound = {a.arg for a in fn.args.kwonlyargs} | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    check(not bound & set(through), f"nothing the phase binds is read through the launcher: {bound & set(through)}")
    check("Path" not in through and m.Path is Path, "`Path` is the module's own, the standard library's")
    branch = next((n for n in ast.walk(fn) if isinstance(n, ast.IfExp) and ast.unparse(n.test) == "launch_deferred"), None)
    check(branch is not None and ast.unparse(branch.body) == "0" and "launcher.__file__" in ast.unparse(branch.orelse),
          "the launcher's file is read only inside the launch that actually happens")
    check(ast.unparse(fn.body[1]) == "stages.begin('role panes')" and ast.unparse(fn.body[-2:][0]) == "stages.finish()"
          and ast.unparse(fn.body[-1]) == "return 0", "the stage first; finished, then 0, last")
    source = ast.get_source_segment((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"), fn)
    check("# Before any window opens:" in source.split("stages.begin(")[0], "the SYRD-249 rationale above the stage, inside the phase")


def test_the_interface_and_the_answers() -> None:
    fn = phase_def()
    args = fn.args
    check(not args.args and not args.posonlyargs and not args.vararg and not args.kwarg
          and tuple(a.arg for a in args.kwonlyargs) == PARAMETERS + FROM_P0 + FROM_P3 + FROM_P4 and all(d is None for d in args.kw_defaults),
          f"keyword-only: the command's parameters, P0's, P3's and P4's values; no defaults: {[a.arg for a in args.kwonlyargs]}")
    theirs = {a.arg: ast.unparse(a.annotation) for a in command_def().args.kwonlyargs}
    for cls in ("NewProjectChoices", "NewProjectBoard", "NewProjectSignIn"):
        node = next(n for n in module_tree().body if isinstance(n, ast.ClassDef) and n.name == cls)
        theirs.update({s.target.id: ast.unparse(s.annotation) for s in node.body if isinstance(s, ast.AnnAssign)})
    check(all(ast.unparse(a.annotation) == theirs[a.arg] for a in args.kwonlyargs), "each annotated as where it comes from")
    check(ast.unparse(fn.returns) == "int", "the command's own answer")
    returns = sorted(ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Return))
    check(returns == ["return 0", "return 1", "return launch_result"], f"exactly the tail's own three answers: {returns}")


def test_the_command_returns_it() -> None:
    body = command_def().body
    check(ast.unparse(body[-2]) == "launch_runner = new_project_sign_in.launch_runner", "right after P4's read-backs")
    tail = body[-1]
    check(isinstance(tail, ast.Return) and isinstance(tail.value, ast.Call) and ast.unparse(tail.value.func) == "_launch_new_project_panes"
          and not tail.value.args and [(k.arg, ast.unparse(k.value)) for k in tail.value.keywords]
          == [(n, n) for n in PARAMETERS + FROM_P0 + FROM_P3 + FROM_P4],
          "the command's last statement returns the tail, called by the launcher's name with the command's own values")


# --- behaviour -------------------------------------------------------------------------------------------------------


def test_staging_problems_are_the_commands_1_before_any_clock_or_window() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp, problems=["tooling is not staged", "the bundle is stale"], launcher_file=Tripwire()), []
        values = inputs(tmp, world, said)
        got = run(world, values)
        check(got == 1 and type(got) is int, f"the command's 1: {got!r}")
        check(world.names() == ["begin", "staging"] and world.log[1][1] is values["config"] and world.log[1][2] is values["runner"],
              f"staging checked for the configuration with the plain runner, then nothing: {world.names()}")
        check(said == ["switchyard: tooling is not staged", "switchyard: the bundle is stale",
                       "switchyard: not opening s374's windows. Everything else it needs was created and nothing was removed; "
                       "the tenant is startable once its tooling is staged."], f"what is said: {said}")


def test_a_launch_with_every_argument() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        values = inputs(tmp, world, said)
        got = run(world, values)
        check(got == 0 and type(got) is int, f"a finished tail answers 0: {got!r}")
        check(world.names() == ["begin", "staging", "time", "time_ns", "layout-output", "launch", "resolve", "announce", "warnings",
                                "records", "finish"], f"the order: {world.names()}")
        check(world.log[4] == ("layout-output", "s374", tmp / "homes" / OWNER), "the layout written under the owner's home")
        config, kw = world.log[5][1:]
        script = Path(world.launcher_file).resolve().with_name(t.TEAM_LAUNCHER_NAME)
        check(config is values["config"] and kw == dict(config_path=values["config_path"], mode="start", script_path=script,
                                                         layout_output=world.layout, assign_layout_owner=True,
                                                         pane_state_dir=values["pane_state_dir"], runner=values["launch_runner"],
                                                         layout_mode="auto", layout_environ=values["layout_environ"],
                                                         konsole_process_launcher=values["konsole_process_launcher"]),
              f"launched with every argument as before: {kw}")
        check(kw["script_path"].parent == (tmp / "installed" / "scripts").resolve() and kw["script_path"].parent != Path(m.__file__).resolve().parent,
              "the launcher script beside the LAUNCHER's own file, not beside this module")
        check(kw["runner"] is values["launch_runner"] and kw["layout_environ"] is values["layout_environ"]
              and kw["konsole_process_launcher"] is values["konsole_process_launcher"], "the very objects")
        check(world.log[6] == ("resolve", "auto", values["layout_environ"], values["runner"]), "the layout resolved with the plain runner")
        check(world.log[7] == ("announce", "s374", "native") and world.log[8] == ("warnings", values["first_run_auth_report"]),
              "announced, then the sign-in's warnings")
        records = world.log[9][2]
        check(world.log[9][1] is values["config"] and records == dict(timeout_seconds=37.4, poll_seconds=0.374,
                                                                     fallback_changed_since_ns=374_250_000_000,
                                                                     pane_state_dir=tmp / "panes", pane_state_updated_since=374.25,
                                                                     print_func=said.append),
              f"the session records awaited since the clocks' stamps: {records}")
        check("default-state" not in world.names() and said[-1] == NATIVE_DESIGN, "the caller's pane-state directory; the native instruction")


def test_the_pane_state_directory_falls_back_to_the_users() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp), []
        run(world, inputs(tmp, world, said, pane_state_dir=None))
        check(("default-state", OWNER, "s374") in world.log and world.log[-2][2]["pane_state_dir"] == world.default_state,
              "the run-as user's default for the project, when the caller gave none")
        check(world.log[5][2]["pane_state_dir"] is None, "while the launch itself is given the caller's own None")


def test_a_deferred_launch_opens_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
        tmp = Path(tmp)
        world, said = World(tmp, launcher_file=Tripwire()), []
        got = run(world, inputs(tmp, world, said, launch_deferred=True))
        check(got == 0, f"a deferred launch still finishes with 0: {got!r}")
        check(world.names() == ["begin", "staging", "time", "time_ns", "resolve", "warnings", "finish"],
              f"no launch, no layout, no announcement, no records -- and the launcher's file never read: {world.names()}")


def test_a_failed_launch_is_answered_with_its_own_status() -> None:
    for status in (1, 2, Status(zero=False, truth=False), Status(zero=False, truth=True)):
        with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, status=status), []
            got = run(world, inputs(tmp, world, said))
            check(got is status, f"{status!r} comes back as itself: {got!r}")
            check(world.names()[-1] == "launch" and not said, f"and nothing after it, not even the finish: {world.names()}")
            if isinstance(status, Status):
                check(status.asked == 1, f"compared once: {status.asked}")


def test_a_status_equal_to_zero_goes_on_and_is_asked_once() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
        tmp = Path(tmp)
        status = Status(zero=True, truth=True)
        world, said = World(tmp, status=status), []
        got = run(world, inputs(tmp, world, said))
        check(got == 0 and world.names()[-1] == "finish" and status.asked == 1, f"goes on, compared once: {got!r} {status.asked}")


def test_the_designer_instruction() -> None:
    for mode, designer, expected in ((t.LAYOUT_MODE_VIEWER, True, VIEWER_DESIGN), (t.LAYOUT_MODE_VIEWER, False, NO_DESIGN),
                                     ("native", True, NATIVE_DESIGN), ("native", False, NO_DESIGN)):
        with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, mode=mode), []
            run(world, inputs(tmp, world, said, include_designer=designer))
            check(said == [expected] and world.log[7] == ("announce", "s374", mode), f"{mode}, designer {designer}: {said}")


def test_every_refusal_stops_what_follows() -> None:
    order = ["begin", "staging", "time", "time_ns", "layout-output", "launch", "resolve", "announce", "warnings", "records", "finish"]
    for step in order[1:-1]:
        with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
            tmp = Path(tmp)
            world, said = World(tmp, fail=step), []
            got = run(world, inputs(tmp, world, said))
            check(isinstance(got, SystemExit) and got.code == f"refused at {step}" and world.names() == order[:order.index(step) + 1]
                  and not said, f"refused at {step}, and nothing after it: {got!r} {world.names()}")


# --- the caller ------------------------------------------------------------------------------------------------------


def earlier(tmp: Path, log: list) -> tuple:
    p0 = m.NewProjectChoices(**{f.name: object() for f in dataclasses.fields(m.NewProjectChoices)})
    p0 = dataclasses.replace(p0, stages=Stages(log), owner_user=OWNER, resolved_slug="s374", include_designer=False)
    p1 = m.NewProjectPreflight(**{f.name: object() for f in dataclasses.fields(m.NewProjectPreflight)})
    p2 = m.NewProjectAccounts(provision_dir=tmp / "provision")
    p3 = m.NewProjectBoard(config=object(), config_path=tmp / "provision" / "s374.json")
    p4 = m.NewProjectSignIn(config=SimpleNamespace(project="s374", run_as_user=OWNER), first_run_auth_report=object(),
                            launch_deferred=False, launch_runner=object())
    return p0, p1, p2, p3, p4


def before_p5(p0, p1, p2, p3, p4) -> dict:
    return dict(_resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                _prepare_new_project_accounts=lambda **kw: p2, _prepare_new_project_board=lambda **kw: p3,
                _run_new_project_sign_in=lambda **kw: p4)


def test_the_command_returns_the_tails_own_answer() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2, p3, p4 = earlier(tmp, log)
        answer = SimpleNamespace(name="whatever the tail answered")
        handed: list = []
        given = {name: object() for name in PARAMETERS}
        with patched(t, **before_p5(p0, p1, p2, p3, p4), _launch_new_project_panes=lambda **kw: handed.append(kw) or answer):
            got = judged(t.switchyard_new_command, **given)
        check(got is answer, f"the command's answer is the tail's, itself: {got!r}")
        check(len(handed) == 1 and tuple(handed[0]) == PARAMETERS + FROM_P0 + FROM_P3 + FROM_P4, f"P5 called once, with its 18 values: {handed}")
        wrong = ([n for n in PARAMETERS if handed[0][n] is not given[n]] + [n for n in FROM_P0 if handed[0][n] is not getattr(p0, n)]
                 + [n for n in FROM_P3 if handed[0][n] is not getattr(p3, n)] + [n for n in FROM_P4 if handed[0][n] is not getattr(p4, n)])
        check(wrong == [], f"each the very value: the command's own, P0's, P3's or P4's (P4's config, not P3's): {wrong}")


def test_the_real_tail_through_the_command() -> None:
    for status in (0, 3):
        with tempfile.TemporaryDirectory(prefix="syrd374.") as tmp:
            tmp = Path(tmp)
            log: list = []
            p0, p1, p2, p3, p4 = earlier(tmp, log)
            world = World(tmp, status=status)
            world.log = log
            with patched(t, **before_p5(p0, p1, p2, p3, p4), **world.seams()):
                got = judged(t.switchyard_new_command, home_base=tmp / "homes", print_func=lambda line: None)
            check(got == status and type(got) is int, f"the command answers the tail's {status}: {got!r}")
            launch = next(e for e in log if e[0] == "launch")
            check(launch[1] is p4.config and launch[2]["runner"] is p4.launch_runner and next(e for e in log if e[0] == "staging")[2] is p0.runner,
                  "launched for P4's configuration with its launch runner; staged with P0's plain runner")
            check((log[-1] == ("finish",)) is (status == 0), f"finished only when the launch succeeded: {[e[0] for e in log]}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_nothing_bound_read_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"new_project_panes_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
