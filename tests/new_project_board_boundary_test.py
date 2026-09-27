#!/usr/bin/env python3
"""SYRD-372: `switchyard new`'s database-and-board phase, against the command it came out of.

The nine statements from `stages.begin("database and board")` through the
first-run worktrees moved into `_prepare_new_project_board` in
`scripts/new_project_phases.py`. Unlike the phases before it, this one has an
early return: when provisioning answers anything but 0, the command returns
that status at once. The phase keeps that statement as it was and so returns
the status itself; otherwise it returns a frozen `NewProjectBoard`, and the
command goes on only when it got one. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first.
- **Seams (rule 24)**, as often as the command read them; nothing bound.
- **The early return:** every failed status comes back out of the command as
  the very object provisioning answered, compared once, and nothing after it
  runs -- no commit, load, desktop, registration or worktrees.
- **The behaviour is unchanged:** the stage, every provisioning argument (the
  original `source_repo`), the commit, the configuration path and load, the
  desktop replacing the configuration, the registration, the worktrees.
- **The caller** hands the phase its own values and the earlier phases'
  objects, reads back `config` and `config_path`, and goes on to P4.

Every facility is this test's own fake. Nothing is provisioned, committed,
registered or run; paths are inside temporary directories this test owns, and
the command is stopped by a fake at P4's first step.
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

CHECKS = 0
#: Measured on the baseline command's nine P3 statements.
SEAMS = {"_commit_project_git_changes": 1, "_prepare_first_run_auth_worktrees": 1, "_register_switchyard_project": 1,
         "load_project_config": 1, "new_project_command": 1, "prepare_project_desktop": 1}
PARAMETERS = ("source_repo", "workflow_config", "commit_git_dir", "port", "database", "home_base", "port_in_use",
              "socket_exists", "registry_dir", "print_func")
FROM_P0 = ("artifact_path", "director_onboarding", "owner_user", "project_dir", "resolved_slug", "runner",
           "selected_role_efforts", "selected_role_models", "stages")
FROM_P2 = ("provision_dir",)
OUTPUTS = ("config", "config_path")
OWNER = "syrd372-agent"


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
        self.log.append(("begin", name))


class World:
    """One run's fakes, recording in order; `fail` names the step that refuses."""

    def __init__(self, *, status: object = 0, fail: str | None = None) -> None:
        self.status, self.fail, self.log = status, fail, []
        self.loaded, self.prepared = SimpleNamespace(name="loaded"), SimpleNamespace(name="prepared")

    def step(self, name: str, *detail: object) -> None:
        self.log.append((name, *detail))
        if self.fail == name:
            raise SystemExit(f"refused at {name}")

    def seams(self) -> dict:
        L = self
        return dict(
            new_project_command=lambda slug, **kw: L.step("provision", slug, kw) or L.status,
            _commit_project_git_changes=lambda **kw: L.step("commit", kw),
            load_project_config=lambda slug, path: L.step("load", slug, path) or L.loaded,
            prepare_project_desktop=lambda config, *, runner: L.step("desktop", config, runner) or L.prepared,
            _register_switchyard_project=lambda path, *, registry_dir: L.step("register", path, registry_dir),
            _prepare_first_run_auth_worktrees=lambda config, *, runner: L.step("worktrees", config, runner),
        )

    def names(self) -> list:
        return [entry[0] for entry in self.log]


def inputs(tmp: Path, world: World, **overrides: object) -> dict:
    values = dict(source_repo=tmp / "original-source", workflow_config=tmp / "workflow.json", commit_git_dir=".git-fixture",
                  port=15472, database="syrd372_db", home_base=tmp / "homes", port_in_use=lambda port: False,
                  socket_exists=lambda path: False, registry_dir=tmp / "registry", print_func=lambda line: None,
                  artifact_path=tmp / "project" / ".switchyard" / "s372.project.json",
                  director_onboarding=tmp / "project" / ".switchyard" / "director.md", owner_user=OWNER,
                  project_dir=tmp / "project", resolved_slug="s372", runner=lambda *a, **k: None,
                  selected_role_efforts={"main": "high"}, selected_role_models={"main": "opus"}, stages=Stages(world.log),
                  provision_dir=tmp / "project" / ".switchyard" / "provision")
    values.update(overrides)
    return values


def run(world: World, values: dict) -> object:
    with patched(t, **world.seams()):
        return judged(m._prepare_new_project_board, **values)


def module_tree() -> ast.Module:
    return ast.parse((ROOT / "scripts" / "new_project_phases.py").read_text(encoding="utf-8"))


def phase_def() -> ast.FunctionDef:
    return next(n for n in module_tree().body if isinstance(n, ast.FunctionDef) and n.name == "_prepare_new_project_board")


def command_def() -> ast.FunctionDef:
    return next(n for n in ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")).body
                if isinstance(n, ast.FunctionDef) and n.name == "switchyard_new_command")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.new_project_phases as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    names = ("_prepare_new_project_board", "NewProjectBoard", "_prepare_new_project_accounts", "_check_new_project_preflight",
             "_resolve_new_project_choices")
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
    check(ast.unparse(fn.body[1]) == "stages.begin('database and board')", "the stage begun first")
    check(ast.unparse(fn.body[3]) == "if result != 0:\n    return result", "the early return, as it was, right after provisioning")


def test_the_interface_and_the_result() -> None:
    fn = phase_def()
    args = fn.args
    check(not args.args and not args.posonlyargs and not args.vararg and not args.kwarg
          and tuple(a.arg for a in args.kwonlyargs) == PARAMETERS + FROM_P0 + FROM_P2 and all(d is None for d in args.kw_defaults),
          f"keyword-only: the command's parameters, P0's values, P2's; no defaults: {[a.arg for a in args.kwonlyargs]}")
    theirs = {a.arg: ast.unparse(a.annotation) for a in command_def().args.kwonlyargs}
    for cls in ("NewProjectChoices", "NewProjectAccounts"):
        node = next(n for n in module_tree().body if isinstance(n, ast.ClassDef) and n.name == cls)
        theirs.update({s.target.id: ast.unparse(s.annotation) for s in node.body if isinstance(s, ast.AnnAssign)})
    check(all(ast.unparse(a.annotation) == theirs[a.arg] for a in args.kwonlyargs), "each annotated as where it comes from")
    check(ast.unparse(fn.returns) == "NewProjectBoard | int", f"a board, or provisioning's status: {ast.unparse(fn.returns)}")
    fields = dataclasses.fields(m.NewProjectBoard)
    check(tuple(f.name for f in fields) == OUTPUTS and all(f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
                                                            for f in fields) and m.NewProjectBoard.__dataclass_params__.frozen,
          "two frozen fields, no defaults")
    returns = sorted(ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Return))
    check(returns == ["return NewProjectBoard(config=config, config_path=config_path)", "return result"],
          f"the status, or the board: {returns}")


def test_the_command_calls_it_and_returns_a_failed_status_itself() -> None:
    body = command_def().body
    at = next(i for i, s in enumerate(body) if isinstance(s, ast.Assign) and ast.unparse(s.targets[0]) == "new_project_board")
    check(ast.unparse(body[at - 1]) == "provision_dir = new_project_accounts.provision_dir", "right after P2's read-back")
    call = body[at].value
    check(ast.unparse(call.func) == "_prepare_new_project_board" and not call.args
          and [(k.arg, ast.unparse(k.value)) for k in call.keywords] == [(n, n) for n in PARAMETERS + FROM_P0 + FROM_P2],
          "called by the launcher's name, with the command's own values")
    check(ast.unparse(body[at + 1]) == "if not isinstance(new_project_board, NewProjectBoard):\n    return new_project_board",
          "a status is returned as it came, without comparing it again")
    check([ast.unparse(s) for s in body[at + 2:at + 4]] == [f"{f} = new_project_board.{f}" for f in OUTPUTS], "both read back")
    check(ast.unparse(body[at + 4]) == "stages.begin('provider sign-in and folder trust', waits_for_you=True)", "then P4 begins")


# --- behaviour -------------------------------------------------------------------------------------------------------


def test_provisioning_its_arguments_and_the_steps_after_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
        tmp = Path(tmp)
        world = World()
        values = inputs(tmp, world)
        got = run(world, values)
        check(isinstance(got, m.NewProjectBoard), f"it goes on: {got!r}")
        check(world.names() == ["begin", "provision", "commit", "load", "desktop", "register", "worktrees"], f"the order: {world.names()}")
        slug, kw = world.log[1][1:]
        check(slug == "s372" and kw == dict(from_artifact=values["artifact_path"], owner_home=tmp / "homes" / OWNER,
                                            source_repo=values["source_repo"], workflow_config=values["workflow_config"],
                                            commit_git_dir=".git-fixture", output_dir=values["provision_dir"],
                                            director_onboarding=values["director_onboarding"], port=15472, database="syrd372_db",
                                            execute=True, runner=values["runner"], port_in_use=values["port_in_use"],
                                            socket_exists=values["socket_exists"], require_owner_user=False,
                                            enable_owner_linger=False, role_models=values["selected_role_models"],
                                            role_efforts=values["selected_role_efforts"], print_func=values["print_func"]),
              f"provisioned with every argument as before: {kw}")
        identity = ("source_repo", "output_dir", "runner", "role_models", "role_efforts", "from_artifact", "port_in_use", "socket_exists")
        check(kw["source_repo"] is values["source_repo"] and kw["output_dir"] is values["provision_dir"]
              and kw["runner"] is values["runner"] and kw["role_models"] is values["selected_role_models"]
              and kw["role_efforts"] is values["selected_role_efforts"], f"the very objects: {identity}")
        check(world.log[2] == ("commit", dict(owner_user=OWNER, project_dir=tmp / "project",
                                              message="Record Switchyard provisioning artifacts", runner=values["runner"])),
              "the artifacts committed as the owner, with the same message")
        path = values["provision_dir"] / "s372.json"
        check(world.log[3] == ("load", "s372", path), "the slug's configuration loaded from the provisioning directory")
        check(world.log[4][1] is world.loaded and world.log[4][2] is values["runner"], "its desktop prepared")
        check(world.log[5] == ("register", path, tmp / "registry"), "registered in the caller's registry")
        check(world.log[6][1] is world.prepared and world.log[6][2] is values["runner"], "the prepared configuration's worktrees")
        check(got.config is world.prepared and got.config_path == path, "and the desktop's configuration is the one returned")


def test_a_failed_provisioning_is_answered_with_its_own_status() -> None:
    for status in (1, 2, -1, None, SimpleNamespace(name="a status that is not an int")):
        with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
            tmp = Path(tmp)
            world = World(status=status)
            got = run(world, inputs(tmp, world))
            check(got is status, f"{status!r} comes back as itself: {got!r}")
            check(world.names() == ["begin", "provision"], f"and nothing runs after it: {world.names()}")


class Zero:
    """Equal to 0 and yet true: a status only `!= 0` reads correctly, counting how often it is asked."""

    def __init__(self) -> None:
        self.asked = 0

    def __ne__(self, other: object) -> bool:
        self.asked += 1
        return other != 0

    def __eq__(self, other: object) -> bool:
        self.asked += 1
        return other == 0

    def __bool__(self) -> bool:
        return True

    __hash__ = object.__hash__


def test_a_status_equal_to_zero_goes_on_and_is_asked_once() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
        tmp = Path(tmp)
        status = Zero()
        world = World(status=status)
        got = run(world, inputs(tmp, world))
        check(isinstance(got, m.NewProjectBoard) and world.names()[-1] == "worktrees", f"provisioning's 0 goes on: {got!r}")
        check(status.asked == 1, f"and the status is compared once, as the command always did: {status.asked}")


def test_every_later_refusal_stops_what_follows() -> None:
    order = ["begin", "provision", "commit", "load", "desktop", "register", "worktrees"]
    for step in order[1:]:
        with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
            tmp = Path(tmp)
            world = World(fail=step)
            got = run(world, inputs(tmp, world))
            check(isinstance(got, SystemExit) and got.code == f"refused at {step}" and world.names() == order[:order.index(step) + 1],
                  f"refused at {step}, and nothing after it: {got!r} {world.names()}")


def test_the_answer_cannot_be_changed() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
        tmp = Path(tmp)
        world = World()
        got = run(world, inputs(tmp, world))
        check(isinstance(judged(setattr, got, "config", None), dataclasses.FrozenInstanceError), "frozen")


# --- the caller ------------------------------------------------------------------------------------------------------


class Fence(Exception):
    """P4 reached: stop there."""


def command_locals(exc: BaseException) -> dict:
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is t.switchyard_new_command.__code__:
            return dict(tb.tb_frame.f_locals)
        tb = tb.tb_next
    raise AssertionError("the command's frame is not on the way to the fence")


def earlier(tmp: Path, log: list) -> tuple:
    p0 = m.NewProjectChoices(**{f.name: object() for f in dataclasses.fields(m.NewProjectChoices)})
    p0 = dataclasses.replace(p0, stages=Stages(log), owner_user=OWNER, project_dir=tmp / "project", resolved_slug="s372",
                             runner=lambda *a, **k: None)
    p1 = m.NewProjectPreflight(**{f.name: object() for f in dataclasses.fields(m.NewProjectPreflight)})
    p2 = m.NewProjectAccounts(provision_dir=tmp / "project" / ".switchyard" / "provision")
    return p0, p1, p2


def p4_fence(seen: list) -> dict:
    def owner_home(user, *, fallback):
        seen.append(("auth-home", user, fallback))
        raise Fence()
    return dict(_owner_home_for_auth=owner_home, run_first_run_auth_phase=refuse("the first-run phase"))


def test_the_command_hands_p3_its_values_and_reads_back_both() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2 = earlier(tmp, log)
        answer = m.NewProjectBoard(config=object(), config_path=object())
        handed: list = []
        seen: list = []
        given = {name: object() for name in PARAMETERS}
        given.update(home_base=tmp / "homes")
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: p2, _prepare_new_project_board=lambda **kw: handed.append(kw) or answer,
                     **p4_fence(seen)):
            got = judged(t.switchyard_new_command, **given)
        check(isinstance(got, Fence), f"stopped at P4: {got!r}")
        check(len(handed) == 1 and tuple(handed[0]) == PARAMETERS + FROM_P0 + FROM_P2, f"P3 called once, with its 20 values: {handed}")
        wrong = ([n for n in PARAMETERS if handed[0][n] is not given[n]] + [n for n in FROM_P0 if handed[0][n] is not getattr(p0, n)]
                 + [n for n in FROM_P2 if handed[0][n] is not getattr(p2, n)])
        check(wrong == [], f"each the very value: the command's own, P0's or P2's: {wrong}")
        local = command_locals(got)
        check(local["config"] is answer.config and local["config_path"] is answer.config_path, "both the command's, the very objects")
        check(local["first_run_runner"] is p0.first_run_runner and local["runner"] is p0.runner, "P0's two runners untouched")
        check(log == [("begin", "provider sign-in and folder trust")] and seen == [("auth-home", OWNER, tmp / "homes" / OWNER)],
              f"then P4 begins: {log} {seen}")


def test_a_failed_provisioning_leaves_the_command_as_itself() -> None:
    status = SimpleNamespace(name="provisioning refused")
    for patched_phase in (True, False):
        with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
            tmp = Path(tmp)
            log: list = []
            p0, p1, p2 = earlier(tmp, log)
            world = World(status=status)
            world.log = log
            seams = dict(_prepare_new_project_board=lambda **kw: status) if patched_phase else world.seams()
            with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                         _prepare_new_project_accounts=lambda **kw: p2, **seams, **p4_fence([])):
                got = judged(t.switchyard_new_command, source_repo=tmp / "s", home_base=tmp / "homes", registry_dir=tmp / "r",
                             print_func=lambda line: None)
            check(got is status, f"the command returns provisioning's status itself ({'phase stood in' if patched_phase else 'real phase'}): {got!r}")
            check(not any(e[0] == "begin" and e[1] != "database and board" for e in log), f"and P4 never begins: {log}")


def test_a_zero_status_is_compared_once_through_the_command() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2 = earlier(tmp, log)
        status = Zero()
        world = World(status=status)
        world.log = log
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: p2, **world.seams(), **p4_fence([])):
            got = judged(t.switchyard_new_command, source_repo=tmp / "s", home_base=tmp / "homes", registry_dir=tmp / "r",
                         print_func=lambda line: None)
        check(isinstance(got, Fence) and status.asked == 1, f"through the whole command, still once, and on to P4: {got!r} {status.asked}")


def test_the_real_phase_between_p2_and_p4() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd372.") as tmp:
        tmp = Path(tmp)
        log: list = []
        p0, p1, p2 = earlier(tmp, log)
        world = World()
        world.log = log
        seen: list = []
        with patched(t, _resolve_new_project_choices=lambda **kw: p0, _check_new_project_preflight=lambda **kw: p1,
                     _prepare_new_project_accounts=lambda **kw: p2, **world.seams(), **p4_fence(seen)):
            got = judged(t.switchyard_new_command, source_repo=tmp / "original", home_base=tmp / "homes",
                         registry_dir=tmp / "registry", print_func=lambda line: None)
        check(isinstance(got, Fence), f"stopped at P4: {got!r}")
        local = command_locals(got)
        check(local["config"] is world.prepared and local["config_path"] == p2.provision_dir / "s372.json",
              "the desktop's configuration and its path are the command's from here on")
        provision = next(e for e in log if e[0] == "provision")[2]
        check(provision["source_repo"] == tmp / "original" and provision["output_dir"] is p2.provision_dir and provision["runner"] is p0.runner,
              "provisioned from the command's own source, into P2's directory, with P0's runner")
        check([e[0] for e in log] == ["begin", "provision", "commit", "load", "desktop", "register", "worktrees", "begin"]
              and log[-1] == ("begin", "provider sign-in and folder trust"), f"P3, then P4's stage: {[e[0] for e in log]}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_and_nothing_bound_read_through_the_launcher")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"new_project_board_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
