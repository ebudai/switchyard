#!/usr/bin/env python3
"""SYRD-356: `switchyard set-vcs-close-role`, against the launcher it came out of.

`set_project_vcs_close_role_command` and its three helpers
(`_project_plan_for_vcs_close_role`, `_write_vcs_close_role_artifacts`,
`_apply_vcs_close_role_board_sql`) moved into `scripts/project_vcs_close_role.py`
unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- **One set of objects.** The launcher re-exports every name, the very same
  objects whichever module is imported first. The CLI and the suites' helper
  import reach the same command. Its `runner` and `print_func` defaults are
  `subprocess.run` and `print` themselves.
- **Seams (rule 24).** Every launcher facility these use -- the seven shared
  with `project_role_add` among them -- and every helper here the command calls
  is read from the launcher when it runs. Nothing they bind, comprehension
  variables included, is read through the launcher (rule 27).
- **The behaviour is unchanged:**
  - the plan regenerated from the recorded plan data, with the named existing
    role as the one allowed to mark work done;
  - pgu, a malformed or unknown role, and a declared workflow refused;
  - the rendered plan's own mark_done role used;
  - the SQL checked before any write;
  - the plan, unit and SQL written beside the configuration for the owner;
  - the SQL applied as postgres, and only after it succeeds the unit installed
    and restarted;
  - the three paths said.

Every facility is this test's own recording fake, installed on the launcher
before anything runs. Files are written only under temporary directories this
test creates. No SQL, service, unit or account is touched.
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
MOVED = ("_project_plan_for_vcs_close_role", "_write_vcs_close_role_artifacts", "_apply_vcs_close_role_board_sql",
         "set_project_vcs_close_role_command")
OWN = ("subprocess", "Path", "Any", "Callable")
SHARED = ("_configured_implementer_roles", "_configured_audit_roles", "_regenerated_control_user", "_loaded_plan_field",
          "_owner_home_from_plan_data", "_commit_git_dir_from_plan_data", "_install_and_restart_board_unit")
PROJECT = "p356"


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


def config(*roles: str, project: str = PROJECT, **extra) -> SimpleNamespace:
    return SimpleNamespace(project=project, project_name="P356", run_as_user="syrd356-owner", ticket_prefix="SY",
                           roles=[SimpleNamespace(role=r) for r in roles], **extra)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.project_vcs_close_role as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.project_vcs_close_role", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.project_vcs_close_role")):
        result = python("import importlib, subprocess, builtins; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_vcs_close_role as m; "
                        "k = m.set_project_vcs_close_role_command.__kwdefaults__; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}), "
                        "k['runner'] is subprocess.run and k['print_func'] is builtins.print)")
        check(result.stdout.strip() == "True True True",
              f"{' then '.join(order)}: one set of objects, the defaults the very objects: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_modules_own_names_and_the_callers() -> None:
    module = ast.parse((ROOT / "scripts" / "project_vcs_close_role.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    check(list(functions) == list(MOVED), f"exactly the moved functions, in baseline order: {list(functions)}")
    through: dict[str, int] = {}
    for name, function in functions.items():
        bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        reads = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                 and n.value.id == "launcher"}
        check(not (bound | set(OWN)) & reads, f"{name}: nothing it binds, nor an own import, is read as the launcher's")
        for n in ast.walk(function):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
    bare = sorted({n.id for n in ast.walk(module) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in set(SHARED) | set(MOVED)})
    check(sum(through.values()) == 29 and all(through.get(n) for n in SHARED) and through.get("_loaded_plan_field") == 6
          and not bare, f"29 call-time reads, the seven shared facilities among them, none bare: {through} {bare}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    # SYRD-357 moved the shared facilities into project_role_plan_support; the launcher re-exports them,
    # so each is still bound on the launcher, where this module reads (and suites patch) it.
    bound = {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)}
    bound |= {(a.asname or a.name) for n in launcher.body if isinstance(n, ast.ImportFrom) for a in n.names}
    check(not {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)} & set(MOVED) and bound >= set(SHARED),
          "the launcher defines none of them, and keeps every shared facility bound")
    dispatch = [n for n in ast.walk(launcher) if isinstance(n, ast.Call) and ast.unparse(n.func) == "set_project_vcs_close_role_command"]
    check(len(dispatch) == 2, "`main` and `switchyard_main` still call it by the launcher's own name")


# --- the plan -------------------------------------------------------------------------------------------------------


def plan_names(built: list, *, plan_data: dict | None = None, config_workflow: bool = False) -> dict:
    data = plan_data if plan_data is not None else {"owner_user": "rec-owner", "port": 8356, "asset_dir": "/a", "board_root": "/b"}
    return dict(
        _plan_data_from_config=lambda config, path: dict(data),
        _load_json=lambda path: {"workflow": {"x": 1}} if config_workflow else {},
        _configured_audit_roles=lambda config, *, plan_data: ("audit",),
        _configured_implementer_roles=lambda config, *, plan_data: ("main", "ops"),
        _loaded_plan_field=lambda d, key, default: d.get(key, default),
        _owner_home_from_plan_data=lambda config, d: Path("/home/rec-owner"),
        _regenerated_control_user=lambda config, d: "syrd356-ctl",
        _repo_root=lambda: Path("/nonexistent/syrd356/repo"),
        _commit_git_dir_from_plan_data=lambda config, d: "cache",
        _tenant_board_root_from_config=lambda config: Path("/tenant-board"),
        current_user_name=lambda: "syrd356-me",
        build_plan=lambda **kw: built.append(kw) or "PLAN",
    )


def test_the_plan_is_regenerated_with_the_named_role_closing() -> None:
    from scripts import project_vcs_close_role as m, team_launcher

    built: list[dict] = []
    with patched(team_launcher, **plan_names(built)):
        try:
            plan = m._project_plan_for_vcs_close_role(config("main", "ops", "designer"), config_path=Path("/c.json"), role_name=" OPS ")
        except SystemExit as exc:
            plan = f"refused: {exc}"
    check(plan == "PLAN" and built == [dict(
        project=PROJECT, project_name="P356", owner_user="rec-owner", owner_home=Path("/home/rec-owner"), control_user="syrd356-ctl",
        port=8356, database=None, source_repo=Path("/nonexistent/syrd356/repo"), commit_git_dir="cache", ticket_prefix="SY",
        board_root=Path("/b"), asset_dir=Path("/a"), frame_dir=None, implementer_roles=("main", "ops"), include_designer=True,
        include_audit=True, audit_roles=("audit",), board_service_traversal=True, vcs_close_role="ops")],
          f"recorded values, the configured roles, and the normalised role as the closer: {built}")
    built.clear()
    with patched(team_launcher, **plan_names(built, plan_data={})):
        m._project_plan_for_vcs_close_role(config("main"), config_path=Path("/c.json"), role_name="main")
    check(built[0]["owner_user"] == "syrd356-owner" and built[0]["board_root"] == Path("/tenant-board")
          and built[0]["asset_dir"] is None and built[0]["include_designer"] is False and built[0]["port"] is None,
          f"without recorded values: the configured owner and the tenant's board root: {built[0]}")
    built.clear()
    with patched(team_launcher, **plan_names(built, plan_data={})):
        m._project_plan_for_vcs_close_role(SimpleNamespace(**{**vars(config("main")), "run_as_user": ""}),
                                           config_path=Path("/c.json"), role_name="main")
    check(built[0]["owner_user"] == "syrd356-me", "no owner anywhere: the current user")


def test_the_plan_refusals_before_anything_is_built() -> None:
    from scripts import project_vcs_close_role as m, team_launcher

    cases = [
        (config("main"), "Bad Role!", {}, False, "team-launcher: VCS close role must match ^[a-z][a-z0-9_-]{0,63}$"),
        (config("main", project="pgu"), "main", {}, False,
         "team-launcher: pgu uses the full built-in workflow; set-vcs-close-role is only for provisioned projects"),
        (config("main"), "ops", {}, False, "team-launcher: VCS close role 'ops' does not exist in project p356"),
        (config("main"), "main", {"workflow": {"x": 1}}, False, "use ticket-board-workflow apply to update configured roles and stages"),
        (config("main"), "main", {}, True, "use ticket-board-workflow apply to update configured roles and stages"),
    ]
    for cfg, role, data, config_workflow, expected in cases:
        built: list = []
        with patched(team_launcher, **plan_names(built, plan_data=data, config_workflow=config_workflow)):
            try:
                m._project_plan_for_vcs_close_role(cfg, config_path=Path("/c.json"), role_name=role); raised = None
            except SystemExit as exc:
                raised = str(exc)
        check(raised == expected and built == [], f"{role!r}: {raised}")


# --- artifacts and SQL ----------------------------------------------------------------------------------------------


def test_the_artifacts_beside_the_configuration_for_the_owner() -> None:
    from scripts import project_vcs_close_role as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd356-artifacts.") as raw:
        owned: list[tuple] = []
        plan = SimpleNamespace(board_unit="p356-ticket-board.service", project=PROJECT, marker=1)
        cfg = config("main")
        with patched(team_launcher, render_board_unit=lambda p: "UNIT", render_vcs_close_role_sql=lambda p: "CLOSE SQL",
                     ensure_owner_file=lambda c, path, *, runner: owned.append((c, path, runner))):
            paths = m._write_vcs_close_role_artifacts(plan, role_name="ops", provision_dir=Path(raw), runner="R", config=cfg)
        expected = (Path(raw) / "plan.json", Path(raw) / "p356-ticket-board.service", Path(raw) / "p356-vcs-close-role.sql")
        check(paths == expected and [o[1] for o in owned] == list(expected) and all(o[0] is cfg and o[2] == "R" for o in owned)
              and json.loads(expected[0].read_text()) == {"board_unit": "p356-ticket-board.service", "project": PROJECT, "marker": 1}
              and expected[1].read_text() == "UNIT" and expected[2].read_text() == "CLOSE SQL",
              f"the plan, unit and close-role SQL written, each given to the owner in order: {paths}")


def test_the_sql_as_postgres_and_its_refusal() -> None:
    from scripts import project_vcs_close_role as m, team_launcher

    calls: list = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, len(calls) - 1, "", "denied")

    plan = SimpleNamespace(admin_database_url="postgresql:///admin", database="p356db")
    with patched(team_launcher, render_vcs_close_role_sql=lambda p: "CLOSE SQL",
                 _proc_failure_reason=lambda result, default: f"{default}: {result.stderr}"):
        m._apply_vcs_close_role_board_sql(plan, role_name="ops", runner=runner)
        try:
            m._apply_vcs_close_role_board_sql(plan, role_name="ops", runner=runner); raised = None
        except SystemExit as exc:
            raised = str(exc)
    check(calls[0][0] == ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "postgresql:///admin", "-f", "-"]
          and calls[0][1] == dict(input="CLOSE SQL", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
          and raised == "team-launcher: failed to configure VCS close role ops in board database p356db: psql failed with exit 1: denied",
          f"psql as postgres with ON_ERROR_STOP on stdin; a failure names the role and database: {raised}")


# --- the command ----------------------------------------------------------------------------------------------------


class Command:
    """The command's launcher steps, answering from this test's objects, into one log."""

    def __init__(self, *, preflight_error: str = "", sql_error: str = "", unit_error: str = "") -> None:
        self.preflight_error, self.sql_error, self.unit_error = preflight_error, sql_error, unit_error
        self.plan = SimpleNamespace(operation_allowed_roles=[["create", ["main"]], ["mark_done", ["ops-rendered", "x"]]], name="plan")
        self.log: list[tuple] = []
        self.said: list[str] = []

    def names(self) -> dict:
        L = self.log

        def fail(message):
            raise SystemExit(message)

        return dict(
            _project_plan_for_vcs_close_role=lambda c, *, config_path, role_name: L.append(("plan", c, config_path, role_name)) or self.plan,
            render_vcs_close_role_sql=lambda plan: L.append(("preflight", plan)) or (fail(self.preflight_error) if self.preflight_error else "SQL"),
            _write_vcs_close_role_artifacts=lambda plan, **kw: L.append(("artifacts", plan, kw))
                or (Path("/p/plan.json"), Path("/p/unit.service"), Path("/p/close.sql")),
            _apply_vcs_close_role_board_sql=lambda plan, *, role_name, runner: L.append(("sql", plan, role_name, runner))
                or (fail(self.sql_error) if self.sql_error else None),
            _install_and_restart_board_unit=lambda plan, *, board_unit_path, runner: L.append(("unit", plan, board_unit_path, runner))
                or (fail(self.unit_error) if self.unit_error else None),
        )

    def run(self, cfg=None, **kw):
        from scripts import project_vcs_close_role as m, team_launcher

        self.config = cfg or config("main", "ops")
        with patched(team_launcher, **self.names()):
            return m.set_project_vcs_close_role_command(self.config, config_path=Path("/nonexistent/syrd356/provision/p356.json"),
                                                        role_name="Ops", runner="R", print_func=self.said.append, **kw)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def test_the_command_in_order() -> None:
    fx = Command()
    code = fx.run()
    check(code == 0 and type(code) is int and fx.kinds() == ["plan", "preflight", "artifacts", "sql", "unit"],
          f"plan, SQL checked, artifacts, SQL applied, then the unit: {fx.kinds()}")
    check(fx.log[0] == ("plan", fx.config, Path("/nonexistent/syrd356/provision/p356.json"), "Ops") and fx.log[1][1] is fx.plan
          and fx.log[2][2] == dict(role_name="ops-rendered", provision_dir=Path("/nonexistent/syrd356/provision"), runner="R", config=fx.config)
          and fx.log[3][2:] == ("ops-rendered", "R") and fx.log[4][2:] == (Path("/p/unit.service"), "R"),
          f"the role the rendered plan closes with, beside the configuration, the unit that was written: {fx.log}")
    check(fx.said == ["team-launcher: set VCS close role for p356 to ops-rendered; updated /p/plan.json, /p/unit.service, and /p/close.sql"],
          f"said: {fx.said}")


def test_each_failure_stops_where_it_happens() -> None:
    for kwargs, last in ((dict(preflight_error="syrd356 bad sql"), "preflight"), (dict(sql_error="syrd356 psql refused"), "sql"),
                         (dict(unit_error="syrd356 restart failed"), "unit")):
        fx = Command(**kwargs)
        try:
            fx.run(); raised = None
        except SystemExit as exc:
            raised = str(exc)
        check(raised == next(iter(kwargs.values())) and fx.kinds()[-1] == last and fx.said == [],
              f"{last}: it propagates, nothing after it runs, nothing is said: {fx.kinds()}")
    fx = Command()
    fx.plan.operation_allowed_roles = [["create", ["main"]]]
    try:
        fx.run(); raised = None
    except KeyError as exc:
        raised = exc
    check(isinstance(raised, KeyError) and fx.kinds() == ["plan"], "a plan without a closer is an error before anything else")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_the_modules_own_names_and_the_callers")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"project_vcs_close_role_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
