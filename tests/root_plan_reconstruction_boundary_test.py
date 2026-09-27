#!/usr/bin/env python3
"""SYRD-360: rebuilding a tenant's plan from root's record, against the launcher it came out of.

`_validated_role_names`, `REGENERATED_PLAN_FIELDS`, `_regenerated_field_divergence`,
`plan_workflow_from_root`, `_resume_source_release` and `_resume_plan_from_record`
moved into `scripts/root_plan_reconstruction.py` unchanged. This pins what makes
that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top. The plan
  builder and the unverified-workflow marker are still imported inside the
  functions that use them.
- **One set of objects.** The launcher re-exports every name, the very same
  objects whichever module is imported first -- the field tuple included.
  Resume-provision and the modules that read them through the launcher reach
  the same objects.
- **Seams (rule 24).** Every launcher facility these use, and every name here
  another definition here reads, is read from the launcher when it runs.
  Nothing they bind -- the local `build_plan`, the `except` binding -- is read
  through it (rule 27).
- **The behaviour is unchanged:**
  - only a list of process role names is accepted, and objections are
    recorded in order;
  - the regenerated fields are compared in their order, skipping what is
    skipped and empty values;
  - root's recorded workflow is preferred, then root's plan record's document,
    an unusable record refused, and a declared workflow nobody can vouch for
    marked;
  - a release must resolve, look like Switchyard, and be root-controlled;
  - a rebuild uses the kernel-checked owner and root's recorded values, and
    reports a workflow problem ahead of any divergence.

Every facility is this test's own fixture or fake, installed before anything
runs. Release directories are temporary directories this test creates. No
root record, grant, account or tenant is touched.
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

CHECKS = 0
MOVED = ("_validated_role_names", "REGENERATED_PLAN_FIELDS", "_regenerated_field_divergence", "plan_workflow_from_root",
         "_resume_source_release", "_resume_plan_from_record")
OWN = ("replace", "Path", "Any", "Mapping", "Sequence")
SEAMS = {"ROLE_RE": 1, "NON_PROCESS_ROLES": 1, "REGENERATED_PLAN_FIELDS": 1, "recorded_declared_workflow": 1,
         "switchyard_shared_install_root": 1, "root_controlled_problems_for": 1, "_default_board_service_user": 1,
         "_validated_role_names": 2, "plan_workflow_from_root": 1, "_regenerated_field_divergence": 1}
FIELDS = ("owner_user", "control_user", "owner_home", "service_user", "service_role", "listener_role", "database", "commit_git_dir",
          "board_root", "board_current", "asset_dir", "frame_dir", "board_log", "listener_log", "socket_path", "runtime_directory",
          "board_unit", "canary_unit", "listener_unit", "tmpfiles_name", "polkit_name", "role_control_sudoers_name",
          "tenant_control_sudoers_name")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


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


@dataclasses.dataclass(frozen=True)
class Plan:
    project: str = "p360"
    owner_user: str = "syrd360-owner"
    board_unit: str = "p360-ticket-board.service"
    commit_git_dir: str = "/regen/cache"
    workflow: object = None
    workflow_seed: object = None


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.root_plan_reconstruction as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.root_plan_reconstruction", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.root_plan_reconstruction"),
                  ("scripts.pane_rebind", "scripts.workflow_adoption", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.root_plan_reconstruction as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_local_imports_and_the_field_tuple() -> None:
    module = ast.parse((ROOT / "scripts" / "root_plan_reconstruction.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.target.id for n in module.body if isinstance(n, (ast.FunctionDef, ast.AnnAssign))]
    check(names == list(MOVED), f"the six, in baseline order: {names}")
    through: dict[str, int] = {}
    for n in ast.walk(module):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
            through[n.attr] = through.get(n.attr, 0) + 1
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    bare = sorted({n.id for f in functions for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in SEAMS})
    check(through == SEAMS and not bare, f"eleven call-time reads, none bare: {through} {bare}")
    locals_ = sorted(ast.unparse(n) for f in functions for n in ast.walk(f) if isinstance(n, ast.ImportFrom) and n.module != "scripts")
    check(locals_ == ["from scripts.ticket_board.project_provision import UNVERIFIED_DECLARED_WORKFLOW",
                      "from scripts.ticket_board.project_provision import build_plan"]
          and "build_plan" not in through, f"both local imports kept, build_plan never the launcher's: {locals_}")
    from scripts import root_plan_reconstruction as m
    check(m.REGENERATED_PLAN_FIELDS == FIELDS, f"the exact field tuple, in order: {m.REGENERATED_PLAN_FIELDS}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)} | {n.target.id for n in launcher.body if isinstance(n, ast.AnnAssign)}
    check(not defined & set(MOVED), f"the launcher defines none of them: {defined & set(MOVED)}")
    for consumer, used in (("pane_rebind.py", ("_resume_source_release", "_resume_plan_from_record")),
                           ("workflow_adoption.py", ("_resume_source_release", "_resume_plan_from_record")),
                           ("repository_boundary_repair.py", ("_resume_source_release", "_resume_plan_from_record")),
                           ("privileged_runtime_plan.py", ("_validated_role_names", "_regenerated_field_divergence", "plan_workflow_from_root"))):
        tree = ast.parse((ROOT / "scripts" / consumer).read_text(encoding="utf-8"))
        reads = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.attr in used}
        bare = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in used]
        check(reads == set(used) and not bare, f"{consumer} still reads them through the launcher")


# --- validation and divergence --------------------------------------------------------------------------------------


def test_role_names_are_validated_in_order() -> None:
    from scripts import root_plan_reconstruction as m

    objections: list[str] = []
    check(m._validated_role_names(None, "f", objections) == () and m._validated_role_names("", "f", objections) == () and objections == [],
          "absent or empty: nothing, and no objection")
    check(m._validated_role_names("main", "implementer_roles", objections) == () and objections == ["implementer_roles is not a list"],
          "not a list: an objection")
    objections.clear()
    check(m._validated_role_names([" Main ", "user", "Bad Role", "ops", "unassigned"], "audit_roles", objections) == ("main", "ops")
          and objections == ["audit_roles entry 'user' is not a role name", "audit_roles entry 'Bad Role' is not a role name",
                             "audit_roles entry 'unassigned' is not a role name"],
          f"normalised names kept; malformed and non-process names objected to, in order: {objections}")


def test_divergence_in_field_order_with_skips_and_empties() -> None:
    from scripts import root_plan_reconstruction as m

    plan = Plan()
    data = {"board_unit": "other.service", "owner_user": "someone", "commit_git_dir": "/recorded", "database": "", "control_user": None,
            "listener_unit": "  "}
    check(m._regenerated_field_divergence(plan, data) == ["owner_user: provisioned 'someone', regenerated 'syrd360-owner'",
                                                          "commit_git_dir: provisioned '/recorded', regenerated '/regen/cache'",
                                                          "board_unit: provisioned 'other.service', regenerated 'p360-ticket-board.service'"],
          "each changed field, in the tuple's order; empty or missing recorded values ignored")
    check(m._regenerated_field_divergence(plan, data, skip=("commit_git_dir", "owner_user")) == [
        "board_unit: provisioned 'other.service', regenerated 'p360-ticket-board.service'"], "skipped fields are not judged")
    check(m._regenerated_field_divergence(plan, {"owner_user": " syrd360-owner "}) == [], "a recorded value equal once stripped agrees")
    from scripts import team_launcher
    with patched(team_launcher, REGENERATED_PLAN_FIELDS=("board_unit",)):
        check(m._regenerated_field_divergence(plan, data) == ["board_unit: provisioned 'other.service', regenerated 'p360-ticket-board.service'"],
              "the tuple is read through the launcher when it runs")


# --- the workflow ---------------------------------------------------------------------------------------------------


def test_every_workflow_record_branch() -> None:
    from scripts import root_plan_reconstruction as m, team_launcher
    from scripts.ticket_board import project_provision

    plan = Plan()
    cases = [
        ((({"stages": ["r"]}, "")), dict(), (dataclasses.replace(plan, workflow={"stages": ["r"]}), "")),
        (((None, "root's workflow record for p360 could not be read: denied")), dict(root_plan_document={"x": 1}, declares_workflow=True),
         (plan, "root's workflow record for p360 could not be read: denied")),
        (((None, "root holds no recorded workflow for p360")), dict(root_plan_document={"doc": 1}), (dataclasses.replace(plan, workflow={"doc": 1}), "")),
        (((None, "root holds no recorded workflow for p360")), dict(), (plan, "")),
        (((None, "root holds no recorded workflow for p360")), dict(declares_workflow=True),
         (dataclasses.replace(plan, workflow=None, workflow_seed=project_provision.UNVERIFIED_DECLARED_WORKFLOW), "")),
    ]
    for answer, kwargs, expected in cases:
        asked: list = []
        with patched(team_launcher, recorded_declared_workflow=lambda project: asked.append(project) or answer):
            got = m.plan_workflow_from_root(plan, **kwargs)
        check(got == expected and asked == ["p360"], f"{answer} {kwargs}: {got}")
    source = {"doc": 1}
    with patched(team_launcher, recorded_declared_workflow=lambda project: (None, "root holds no recorded workflow for p360")):
        got, _ = m.plan_workflow_from_root(plan, root_plan_document=source)
    check(got.workflow == source and got.workflow is not source, "root's plan document is adopted as a copy")


# --- the release ----------------------------------------------------------------------------------------------------


def test_the_release_must_be_a_root_controlled_switchyard_release() -> None:
    from scripts import root_plan_reconstruction as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd360-release.") as raw:
        root = Path(raw)
        release = root / "releases" / "r1"
        (release / "scripts").mkdir(parents=True)
        (root / "current").symlink_to(release)
        walked: list = []
        with patched(team_launcher, switchyard_shared_install_root=lambda: root,
                     root_controlled_problems_for=lambda path: walked.append(path) or []):
            selected, problem = m._resume_source_release(None)
            check(selected == release.resolve() and problem == f"{release.resolve()} does not look like a Switchyard release"
                  and walked == [], f"the shared current release, resolved; no launcher: refused before the walk: {problem}")
            (release / "scripts" / "team_launcher.py").write_text("", encoding="utf-8")
            check(m._resume_source_release(None) == (release.resolve(), "") and walked == [str(release.resolve())],
                  "a Switchyard release root controls is accepted, walked by its resolved path")
            missing = root / "missing"
            selected, problem = m._resume_source_release(missing)
            check(selected == missing and problem.startswith(f"{missing} is not a release directory on this host ("),
                  f"a named release that does not resolve: {problem}")
            home_named = Path("~") / "syrd360-no-such-release"
            selected, problem = m._resume_source_release(home_named)
            check(selected == home_named.expanduser() and "is not a release directory" in problem, "a named path is expanded first")
        with patched(team_launcher, switchyard_shared_install_root=lambda: root,
                     root_controlled_problems_for=lambda path: ["group-writable", "owned by 1000"]):
            check(m._resume_source_release(release) == (release.resolve(), f"{release.resolve()} is not root-controlled, so it is not an audited "
                                                                            "release; group-writable; owned by 1000"),
                  "a release root does not control is refused, with every reason")


# --- the rebuild ----------------------------------------------------------------------------------------------------


def test_the_rebuild_uses_roots_facts_and_reports_a_workflow_problem_first() -> None:
    from scripts import root_plan_reconstruction as m, team_launcher
    from scripts.ticket_board import project_provision

    built: list[dict] = []
    log: list = []
    identity = SimpleNamespace(owner_user="syrd360-kernel-owner", owner_home=Path("/nonexistent/syrd360/home"))
    recorded = {"project": "p360", "project_name": "", "owner_user": "tenant-says", "port": "8360", "database": "p360db",
                "service_user": "", "commit_git_dir": "", "implementer_roles": ["Main", "user"], "ticket_prefix": "SY",
                "board_service_traversal": 0, "control_user": None, "audit_roles": "audit", "workflow": {"doc": 1}}
    names = dict(
        _default_board_service_user=lambda: log.append("service") or "syrd360-board",
        plan_workflow_from_root=lambda plan, *, root_plan_document, declares_workflow: log.append(("workflow", root_plan_document, declares_workflow))
            or (dataclasses.replace(plan, workflow="W"), "syrd360: workflow record unusable"),
        _regenerated_field_divergence=lambda plan, data: log.append(("divergence", plan.workflow, data is recorded)) or ["port: 1 vs 2"],
    )
    with patched(team_launcher, **names), patched(project_provision, build_plan=lambda **kw: built.append(kw) or Plan()):
        plan, divergence = m._resume_plan_from_record(SimpleNamespace(data=recorded), identity, source_repo=Path("/rel"))
    check(built == [dict(project="p360", project_name=None, owner_user="syrd360-kernel-owner", owner_home=Path("/nonexistent/syrd360/home"),
                         port=8360, database="p360db", service_user="syrd360-board", source_repo=Path("/rel"), commit_git_dir=None,
                         implementer_roles=("main",), ticket_prefix="SY", board_service_traversal=False, control_user="",
                         audit_roles=None)],
          f"the kernel-checked owner, root's recorded values converted, validated roles, the default service user: {built}")
    check(log == ["service", ("workflow", {"doc": 1}, True), ("divergence", "W", True)] and plan.workflow == "W"
          and divergence == ["syrd360: workflow record unusable", "port: 1 vs 2"],
          f"root's workflow, then the divergence of the plan it produced, the workflow problem first: {log} {divergence}")
    built.clear(); log.clear()
    names["plan_workflow_from_root"] = lambda plan, *, root_plan_document, declares_workflow: log.append(("workflow", root_plan_document, declares_workflow)) or (plan, "")
    names["_regenerated_field_divergence"] = lambda plan, data: []
    bare = {"project": "p360", "port": "  ", "service_user": "svc", "workflow": {}}
    with patched(team_launcher, **names), patched(project_provision, build_plan=lambda **kw: built.append(kw) or Plan()):
        plan, divergence = m._resume_plan_from_record(SimpleNamespace(data=bare), identity, source_repo=Path("/rel"))
    check(built[0]["port"] is None and built[0]["service_user"] == "svc" and built[0]["implementer_roles"] is None
          and built[0]["board_service_traversal"] is True and log == [("workflow", None, True)] and divergence == [],
          f"a blank port is no port; a recorded service user wins; traversal on unless recorded; an empty recorded workflow is declared but not a document: {built[0]} {log}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_local_imports_and_the_field_tuple")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"root_plan_reconstruction_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
