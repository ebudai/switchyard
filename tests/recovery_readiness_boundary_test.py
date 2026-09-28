#!/usr/bin/env python3
"""SYRD-365: recovery readiness and the repository boundary, against the launcher they came out of.

`recovery_readiness_problems`, `_acl_entries` and `repository_boundary_problems`
moved into `scripts/recovery_readiness.py` unchanged. This pins what makes that
safe:

- **No eager cycle.** At its top the module imports only the standard library
  and the registration timeout from `provider_runtime_state` -- the launcher's
  very object -- which loads nothing of the launcher's. The provisioning path
  helpers are still imported inside the function that uses them.
- **One set of objects.** The launcher re-exports every name, so
  resume-provision and `repository_boundary_repair` reach them.
- **Seams (rule 24).** The packet probe, the liveness checks, the registration
  wait, the uid and registry readers and the ACL reader are read from the
  launcher when they run. Nothing they bind is read through it (rule 27).
- **The behaviour is unchanged:** every readiness problem, in the same order
  with the same words; the ACL reader's argv and filtering; every way the
  repository boundary is still open, with the same call counts.

Every facility is this test's own fixture or fake. The registry and the
boundary directories are temporary directories this test creates and modes;
`getfacl` never runs (the ACL reader is a recording fake), the group lookup is
a fake, and every launcher seam that could reach tmux, /proc, a board or the
passwd database is refused unless a test stands one in.
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
MOVED = ("recovery_readiness_problems", "_acl_entries", "repository_boundary_problems")
OWN = ("grp", "stat", "subprocess", "Path", "Any", "Callable", "Sequence", "RUNTIME_REGISTRATION_TIMEOUT_SECONDS")
SEAMS = {
    "recovery_readiness_problems": {"_load_json": 1, "privileged_packet_completion": 1, "repository_boundary_problems": 1,
                                    "owner_tmux_targets": 1, "read_runtime_assignment_details": 1, "uid_for_user": 1,
                                    "pane_liveness": 1, "await_runtime_registration": 1},
    "repository_boundary_problems": {"_acl_entries": 3},
}
SLUG = "p365"


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
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is judged, not a crash
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


#: Every launcher facility the readiness check could reach for real, refused.
LIVE = dict(
    _load_json=refuse("the registry reader"),
    privileged_packet_completion=refuse("the packet probe"),
    repository_boundary_problems=refuse("the boundary check"),
    owner_tmux_targets=refuse("the owner's tmux server"),
    read_runtime_assignment_details=refuse("the board's runtime assignments"),
    uid_for_user=refuse("the passwd uid lookup"),
    pane_liveness=refuse("/proc"),
    await_runtime_registration=refuse("the runtime-registration wait"),
    _acl_entries=refuse("getfacl"),
)
#: The same, for tests of the boundary check itself, which must stay the real one.
BOUNDARY_LIVE = {name: fake for name, fake in LIVE.items() if name != "repository_boundary_problems"}


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_the_runtime_state_leaf_at_import() -> None:
    result = python("import sys, scripts.recovery_readiness as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.account_drop', 'scripts.provider_runtime_state']",
          f"it loads only provider_runtime_state and its leaf, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.recovery_readiness", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.recovery_readiness"),
                  ("scripts.repository_boundary_repair", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.recovery_readiness as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_local_import_and_the_defaults() -> None:
    module = ast.parse((ROOT / "scripts" / "recovery_readiness.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    check([f.name for f in functions] == list(MOVED), f"the three, in baseline order: {[f.name for f in functions]}")
    every = {name for reads in SEAMS.values() for name in reads}
    for f in functions:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every})
        check(through == SEAMS.get(f.name, {}) and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")
    locals_ = [ast.unparse(n) for f in functions for n in ast.walk(f) if isinstance(n, ast.ImportFrom) and n.module != "scripts"]
    check(locals_ == ["from scripts.ticket_board.project_provision import roles_group_name, tenant_control_repository, tenant_worktree_base"],
          f"the provisioning path helpers still imported where they are used: {locals_}")

    from scripts import provider_runtime_state, recovery_readiness as m, team_launcher

    k = m.recovery_readiness_problems.__kwdefaults__
    check(k["runtime_wait_seconds"] is provider_runtime_state.RUNTIME_REGISTRATION_TIMEOUT_SECONDS is team_launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS
          and k["print_func"] is print and k["runner"] is subprocess.run
          and all(k[name] is None for name in ("process_commands", "pane_liveness_states", "session_statuses", "completion",
                                               "registration", "runtime_wait_roles"))
          and m.repository_boundary_problems.__kwdefaults__ == {"runner": subprocess.run},
          "every default is the object it was: the shared timeout, print, subprocess.run, None")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)}
    # The two interleaved definitions did not come here. SYRD-366 moved the finish
    # step on to scripts/resume_provision_command.py, which the launcher re-exports.
    finish_exports = {a.name for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.resume_provision_command"
                      for a in n.names}
    # SYRD-437 moved the board-workflow reader to scripts/board_workflow_readers.py; the launcher re-exports it from
    # there, unaliased, so it stays the launcher's name: defined here (the baseline), or re-exported from exactly that module.
    board_workflow = {a.name for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.board_workflow_readers"
                      for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and "read_board_declared_workflow" in defined | board_workflow
          and "_finish_provision_after_packet" in defined | finish_exports
          and not {"_finish_provision_after_packet", "read_board_declared_workflow"} & {f.name for f in functions},
          f"the launcher defines none of them, and still has the two interleaved definitions: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.recovery_readiness"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all three: {exported}")
    repair = ast.parse((ROOT / "scripts" / "repository_boundary_repair.py").read_text(encoding="utf-8"))
    reads = [n for n in ast.walk(repair) if isinstance(n, ast.Attribute) and n.attr == "repository_boundary_problems"]
    bare = [n for n in ast.walk(repair) if isinstance(n, ast.Name) and n.id == "repository_boundary_problems"]
    check(len(reads) == 1 and isinstance(reads[0].value, ast.Name) and reads[0].value.id == "launcher" and not bare,
          "repository_boundary_repair still reads the boundary check through the launcher")


# --- readiness ----------------------------------------------------------------------------------------------------------


PLAN = SimpleNamespace(project=SLUG)
ROLES = [SimpleNamespace(role="director"), SimpleNamespace(role="main"), SimpleNamespace(role="ops")]
CONFIG = SimpleNamespace(run_as_user="syrd365-owner", roles=ROLES)
DONE = SimpleNamespace(problems=())
QUIET = SimpleNamespace(exited=(), missing=(), problem="")


def readiness(tmp: Path, *, registry: object = "good", seams: dict | None = None, **kwargs: object):
    from scripts import team_launcher as t

    config_path = tmp / "config.json"
    registry_path = tmp / "registry" / f"{SLUG}.json"
    if registry is not None:
        registry_path.parent.mkdir(exist_ok=True)
        registry_path.write_text("{}")
    loaded = {"slug": SLUG, "config_path": str(config_path)} if registry == "good" else registry
    base = dict(LIVE, _load_json=lambda path: loaded, repository_boundary_problems=lambda plan, *, runner: [])
    kwargs.setdefault("completion", DONE)
    kwargs.setdefault("registration", QUIET)
    kwargs.setdefault("pane_liveness_states", [])
    with patched(t, **{**base, **(seams or {})}):
        return judged(t.recovery_readiness_problems, PLAN, CONFIG, config_path, registry_path=registry_path, **kwargs)


def test_the_registry_entry_is_read_back() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        check(readiness(root) == [], "registered, with this slug and this configuration: nothing")
        registry_path = root / "registry" / f"{SLUG}.json"
        registry_path.unlink()
        got = readiness(root, registry=None, seams={"_load_json": refuse("a registry file that is not there")})
        check(got == [f"{SLUG} is not registered at {registry_path}"], f"no entry, and nothing read: {got}")
        check(readiness(root, registry=["not", "a", "dict"]) == [f"{SLUG} is not registered at {registry_path}"], "an entry that is not a dict")
        got = readiness(root, registry={"slug": "other", "config_path": "/elsewhere.json"})
        check(got == [f"{registry_path} registers slug 'other', not '{SLUG}'",
                      f"{registry_path} points at '/elsewhere.json' rather than the verified configuration {root / 'config.json'}"],
              f"a wrong slug, then a wrong configuration, each named: {got}")
        got = readiness(root, registry={})
        check(got == [f"{registry_path} registers slug None, not '{SLUG}'",
                      f"{registry_path} points at '' rather than the verified configuration {root / 'config.json'}"], f"an empty entry: {got}")


def test_the_packet_and_the_boundary_are_read_back() -> None:
    runner = refuse("the runner itself")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        check(readiness(root, completion=SimpleNamespace(problems=("the board unit is not installed",)))
              == ["the board unit is not installed"], "a supplied completion's problems")

        class Falsy:
            problems = ("never read",)

            def __bool__(self) -> bool:
                return False

        probed: list = []
        got = readiness(root, completion=Falsy(), runner=runner,
                        seams={"privileged_packet_completion": lambda plan, *, runner: probed.append((plan, runner)) or SimpleNamespace(problems=("probed",))})
        check(got == ["probed"] and probed == [(PLAN, runner)], f"no truthy completion: the packet is probed with the runner: {got} {probed}")
        asked: list = []
        got = readiness(root, runner=runner, seams={"repository_boundary_problems": lambda plan, *, runner: asked.append(runner) or ["x is open", "y is open"]})
        check(got == [f"x is open -- repair it with `pkexec switchyard repair-boundary {SLUG} --apply`",
                      f"y is open -- repair it with `pkexec switchyard repair-boundary {SLUG} --apply`"] and asked == [runner],
              f"every boundary objection, with how to repair it: {got}")


def test_liveness_is_supplied_or_read_from_tmux_assignments_and_uid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        states = [SimpleNamespace(role="director", live=True, why="up"), SimpleNamespace(role="main", live=False, why="gone"),
                  SimpleNamespace(role="ops", live=False, why="reused")]
        got = readiness(root, pane_liveness_states=states)
        check(got == ["main has no running pane: gone", "ops has no running pane: reused"], f"supplied states, dead panes in order: {got}")
        log: list = []
        runner = refuse("the runner itself")
        seams = {
            "owner_tmux_targets": lambda config, *, runner: log.append(("tmux", config, runner)) or ({"t"}, "tmux could not be asked"),
            "read_runtime_assignment_details": lambda config: log.append(("assignments", config)) or ({"main": {}}, "assignments unreadable"),
            "uid_for_user": lambda name: log.append(("uid", name)) or 1365,
            "pane_liveness": lambda config, role, **kw: log.append(("pane", role.role, kw)) or SimpleNamespace(role=role.role, live=role.role != "ops", why="read"),
        }
        got = readiness(root, pane_liveness_states=None, runner=runner, seams=seams)
        check(got == ["tmux could not be asked", "assignments unreadable", "ops has no running pane: read"],
              f"the tmux problem, the assignment problem, then the dead pane: {got}")
        kw = {"tmux_targets": {"t"}, "assignments": {"main": {}}, "owner_uid": 1365}
        check(log == [("tmux", CONFIG, runner), ("assignments", CONFIG), ("uid", "syrd365-owner"),
                      ("pane", "director", kw), ("pane", "main", kw), ("pane", "ops", kw)],
              f"asked in that order, every role with the same evidence: {log}")


def test_registration_is_waited_for_and_reported_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        got = readiness(root, registration=SimpleNamespace(exited=("ops",), missing=("main", "director"), problem="the board said no"),
                        runtime_wait_seconds=12.5)
        check(got == ["ops has no running session, so it will not register a runtime",
                      "main did not register a runtime with the board within 12.5s",
                      "director did not register a runtime with the board within 12.5s", "the board said no"],
              f"exited, then missing with the deadline, then the wait's own problem: {got}")
        waited: list = []
        printer = refuse("the print function, which is only handed on")

        def wait(config, roles, *, alive, timeout_seconds, print_func):
            waited.append((config, [r.role for r in roles], [alive(r) for r in ROLES], timeout_seconds, print_func))
            return QUIET

        states = [SimpleNamespace(role="director", live=True, why=""), SimpleNamespace(role="main", live=False, why="x")]
        readiness(root, registration=None, pane_liveness_states=states, runtime_wait_seconds=7.0, print_func=printer,
                  seams={"await_runtime_registration": wait})
        check(waited == [(CONFIG, ["director", "main", "ops"], [True, False, False], 7.0, printer)],
              f"no registration: the wait, for every role, alive only where a pane is live, with the deadline and printer: {waited}")
        waited.clear()
        readiness(root, registration=None, runtime_wait_roles=[ROLES[2]], seams={"await_runtime_registration": wait})
        check(waited[0][1] == ["ops"] and waited[0][3] == 90.0, f"the named roles, and the shared default deadline: {waited}")
        waited.clear()
        readiness(root, registration=None, runtime_wait_roles=[], seams={"await_runtime_registration": wait})
        check(waited[0][1] == ["director", "main", "ops"], f"an empty list means every role: {waited}")


def test_session_records_only_add_what_the_wait_did_not_already_say() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        statuses = [SimpleNamespace(role="director", found=True), SimpleNamespace(role="main", found=False)]
        got = readiness(root, session_statuses=statuses)
        check(got == ["main has not registered a runtime session with the board", "ops has not registered a runtime session with the board"],
              f"every role without a found record: {got}")
        got = readiness(root, session_statuses=statuses, registration=SimpleNamespace(exited=("main",), missing=("ops",), problem=""))
        check(got == ["main has no running session, so it will not register a runtime",
                      "ops did not register a runtime with the board within 90s"],
              f"a role already reported exited or missing is not reported twice: {got}")
        check(readiness(root, process_commands=["anything at all"]) == [], "process_commands is accepted and not read")


def test_every_problem_in_one_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        got = readiness(root, registry={"slug": SLUG, "config_path": "/x"}, completion=SimpleNamespace(problems=("packet",)),
                        pane_liveness_states=[SimpleNamespace(role="main", live=False, why="dead")],
                        registration=SimpleNamespace(exited=("ops",), missing=(), problem="wait"),
                        session_statuses=[], seams={"repository_boundary_problems": lambda plan, *, runner: ["open"]})
        check([line.split(" ")[0] for line in got] == [str(root / "registry" / f"{SLUG}.json"), "packet", "open", "main", "ops", "wait",
                                                       "director", "main"],
              f"registry, packet, boundary, panes, exited, the wait, then session records: {got}")


# --- the ACL reader ------------------------------------------------------------------------------------------------------


def test_the_acl_reader() -> None:
    from scripts import team_launcher as t

    calls: list = []

    def runner_of(answer):
        return lambda argv, **kwargs: calls.append((argv, kwargs)) or answer

    got = judged(t._acl_entries, Path("/fixture/base"), runner=runner_of(
        subprocess.CompletedProcess([], 0, "# file: fixture/base\nuser::rwx\n\ngroup::r-x\ndefault:other::---\n", "")))
    check(got == ["user::rwx", "group::r-x", "default:other::---"]
          and calls == [(["getfacl", "-cpn", "/fixture/base"], {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "text": True})],
          f"the exact argv, comments and blank lines dropped: {got} {calls}")
    check(judged(t._acl_entries, Path("/x"), runner=runner_of(subprocess.CompletedProcess([], 1, "user::rwx\n", ""))) == []
          and judged(t._acl_entries, Path("/x"), runner=runner_of(SimpleNamespace(stdout="user::rwx\n"))) == []
          and judged(t._acl_entries, Path("/x"), runner=runner_of(SimpleNamespace(returncode=0, stdout=None))) == [],
          "a failure, a missing return code, or no output: nothing")


# --- the repository boundary ----------------------------------------------------------------------------------------------


class Boundary:
    """A tenant's worktree base and control repository, under a temporary directory."""

    def __init__(self, tmp: Path, *, gid: int | None = 4242) -> None:
        self.base, self.control = tmp / "base", tmp / "control"
        self.gid, self.acl, self.asked = gid, {}, []
        self.plan = SimpleNamespace(project=SLUG, owner_home=str(tmp), role_worktrees=())

    def seams(self):
        from scripts.ticket_board import project_provision

        def getgrnam(name: str):
            self.asked.append(("group", name))
            if self.gid is None:
                raise KeyError(name)
            return SimpleNamespace(gr_gid=self.gid)

        def acl(path: Path, *, runner):
            self.asked.append(("acl", path))
            return list(self.acl.get(path, []))

        provision = patched(project_provision, tenant_worktree_base=lambda plan: str(self.base),
                            tenant_control_repository=lambda plan: str(self.control), roles_group_name=lambda project: f"{project}-roles")
        return provision, dict(BOUNDARY_LIVE, _acl_entries=acl), SimpleNamespace(getgrnam=getgrnam)

    def problems(self):
        from scripts import recovery_readiness as m, team_launcher as t

        provision, launcher_seams, grp = self.seams()
        with provision, patched(t, **launcher_seams), patched(m, grp=grp):
            return judged(t.repository_boundary_problems, self.plan, runner=refuse("the runner itself"))


def test_a_plan_that_names_no_tenant_is_not_read_as_closed() -> None:
    from scripts import team_launcher as t

    with patched(t, **BOUNDARY_LIVE):
        check(judged(t.repository_boundary_problems, SimpleNamespace(project=SLUG)) == [
            f"{SLUG}'s repository boundary was not checked: the plan it was asked about names no owner_home, no role_worktrees"],
              "every missing field named, and nothing else asked")
        check(judged(t.repository_boundary_problems, SimpleNamespace(owner_home="/h", role_worktrees=())) == [
            "this project's repository boundary was not checked: the plan it was asked about names no project"], "no project at all")


def test_a_closed_boundary_and_every_way_it_is_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fx = Boundary(root)
        check(fx.problems() == [] and fx.asked == [("group", f"{SLUG}-roles")], "neither surface exists: nothing, only the group asked")
        fx.base.mkdir(mode=0o750)
        fx.base.chmod(0o750)
        fx.control.mkdir()
        fx.acl = {fx.base: ["default:other::---"]}
        fx.asked.clear()
        check(fx.problems() == [] and fx.asked == [("group", f"{SLUG}-roles"), ("acl", fx.base), ("acl", fx.base), ("acl", fx.control)],
              f"closed: the base read for inheritance and for the group, the control repository for the group: {fx.asked}")
        fx.base.chmod(0o755)
        fx.acl = {fx.base: ["default:other::r-x", "default:other::--x", f"group:{fx.gid}:r-x"], fx.control: [f"default:group:{fx.gid}:rwx"]}
        for name, mode in (("d", 0o755), ("a", 0o705), ("closed", 0o750), ("c", 0o701), ("b", 0o777), ("e", 0o704)):
            (fx.base / name).mkdir(mode=mode)
            (fx.base / name).chmod(mode)
        (fx.base / "file").write_text("not a worktree")
        (fx.base / "file").chmod(0o777)
        got = fx.problems()
        check(got == [
            f"{fx.base} is mode 0755, which anybody on this host can enter",
            f"{fx.base} passes --x, r-x to everything created under it, so new worktrees are readable beyond the tenant",
            f"5 worktree(s) under {fx.base} are world-readable: a, b, c and 2 more",
            f"{fx.base} still grants {SLUG}-roles, which is the board socket's group",
            f"{fx.control} still grants {SLUG}-roles -- the group the board service is in"], f"every opening, in order: {got}")
        for name in ("d", "e"):
            (fx.base / name).chmod(0o750)
        fx.acl = {fx.base: []}
        got = fx.problems()
        check(got == [f"{fx.base} is mode 0755, which anybody on this host can enter",
                      f"{fx.base} grants no inherited closure, so every worktree created under it from now on will be world-readable",
                      f"3 worktree(s) under {fx.base} are world-readable: a, b, c"], f"no inherited entry; exactly three, with no count: {got}")


def test_a_group_this_host_does_not_have_grants_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = Boundary(Path(tmp), gid=None)
        fx.base.mkdir(mode=0o750)
        fx.base.chmod(0o750)
        fx.control.mkdir()
        fx.acl = {fx.base: ["default:other::---", "group:4242:rwx"], fx.control: ["group:4242:rwx"]}
        check(fx.problems() == [] and fx.asked == [("group", f"{SLUG}-roles"), ("acl", fx.base)],
              f"no gid: no stale entry to find, so neither surface is read for one: {fx.asked}")
        fx.gid = 4243
        fx.asked.clear()
        check(fx.problems() == [], f"entries for another gid are not this group's: {fx.asked}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_only_the_runtime_state_leaf_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_local_import_and_the_defaults")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"recovery_readiness_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
