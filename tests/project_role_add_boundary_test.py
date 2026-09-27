#!/usr/bin/env python3
"""SYRD-355: `switchyard add-role`, the command and its helpers, against the launcher they came out of.

`add_project_role_command` and its nine helpers moved into
`scripts/project_role_add.py` unchanged. `local_account_exists`, which the
command binds as its `account_exists` default when it is defined, moved to the
leaf `scripts/host_accounts.py`. This pins what makes that safe:

- **No cycle.** Neither module imports the launcher at its top, and
  `host_accounts` imports nothing of Switchyard's.
- **One set of objects.** The launcher re-exports every name, the very same
  objects whichever module is imported first. The default is the leaf's
  `local_account_exists`, which is also the launcher's. The `runner`,
  `print_func` and `input_func` defaults are `subprocess.run`, `print` and
  `input` themselves.
- **Seams (rule 24).** Every launcher facility these use, and every name here
  another definition here calls, is read from the launcher when it runs. So is
  the launcher's own file: the pane script falls back to beside the LAUNCHER,
  never beside this module. Nothing they bind is read through the launcher
  (rule 27).
- **The behaviour is unchanged:**
  - an interactive CLI choice or the codex default;
  - the role and CLI validated, and the plan's SQL checked before anything is
    written;
  - a half-added role recovered without re-appending;
  - the plan artifacts, board SQL and unit restart, then the worktree prepared
    as the owner;
  - the role started, or, without its account, the operator handed the
    commands for the whole role-control set and nothing started;
  - the helpers' slot, layout, payload, artifact and SQL rules;
  - every message.

Every facility is this test's own recording fake, installed on the launcher (or,
for the local import, on its own module) before anything runs. Files are
written only under temporary directories this test creates. No SQL, service,
worktree, account, tmux or pane is touched.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("_is_recognized_generated_project_layout", "_vcs_close_role_from_plan_data", "_project_plan_for_added_role",
         "_next_visible_role_slot", "_add_role_payload", "_update_project_design_artifact_for_role", "_write_added_role_config",
         "_write_updated_project_plan_artifacts", "_apply_add_role_board_sql", "add_project_role_command")
OWN = ("json", "subprocess", "sys", "replace", "Path", "Any", "Callable", "local_account_exists")
PROJECT = "p355"
OWNER = "syrd355-owner"


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
class Role:
    role: str
    detached: bool = False
    slot: int | None = None
    workdir: str = ""
    user: str = ""


@dataclasses.dataclass(frozen=True)
class Config:
    roles: tuple = ()
    project: str = PROJECT
    run_as_user: str = OWNER
    repository: object = None
    pane_launcher: object = None
    worktree_base: object = None
    control_repository: object = None
    layout: object = None
    name: str = "config"


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_modules_load_nothing_of_switchyards_at_import() -> None:
    for module in ("scripts.project_role_add", "scripts.host_accounts"):
        result = python(f"import sys, {module} as m; "
                        f"print(sorted(n for n in sys.modules if n.startswith('scripts.') and n not in (m.__name__, 'scripts.host_accounts')))")
        check(result.returncode == 0 and result.stdout.strip() == "[]",
              f"{module} imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.project_role_add", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_role_add"),
                  ("scripts.host_accounts", "scripts.project_role_add", "scripts.team_launcher")):
        result = python("import importlib, subprocess, builtins; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_role_add as m, scripts.host_accounts as h; "
                        "k = m.add_project_role_command.__kwdefaults__; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}), "
                        "k['account_exists'] is h.local_account_exists is t.local_account_exists, "
                        "k['runner'] is subprocess.run and k['print_func'] is builtins.print and k['input_func'] is builtins.input)")
        check(result.stdout.strip() == "True True True True",
              f"{' then '.join(order)}: one set of objects, one default: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_modules_own_names_and_the_launchers_file() -> None:
    module = ast.parse((ROOT / "scripts" / "project_role_add.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    check(list(functions) == list(MOVED), f"exactly the moved functions, in baseline order: {list(functions)}")
    through_all: dict[str, int] = {}
    for name, function in functions.items():
        bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound |= {h.name for h in ast.walk(function) if isinstance(h, ast.ExceptHandler) and h.name}
        bound |= {(a.asname or a.name) for i in ast.walk(function) if isinstance(i, ast.ImportFrom) for a in i.names}
        through = {n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                   and n.value.id == "launcher"}
        check(not (bound | set(OWN)) & through, f"{name}: nothing it binds, nor an own import, is read as the launcher's")
        for n in ast.walk(function):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through_all[n.attr] = through_all.get(n.attr, 0) + 1
    check(sum(through_all.values()) == 74 and through_all.get("__file__") == 1 and through_all.get("TEAM_LAUNCHER_NAME") == 1,
          f"74 call-time reads, the launcher's own file once: {sum(through_all.values())} {through_all.get('__file__')}")
    bare_file = [n for n in ast.walk(module) if isinstance(n, ast.Name) and n.id == "__file__"]
    check(not bare_file, "this module's own file is never used")
    default = functions["add_project_role_command"].args.kw_defaults[functions["add_project_role_command"].args.kwonlyargs.index(
        next(a for a in functions["add_project_role_command"].args.kwonlyargs if a.arg == "account_exists"))]
    check(ast.unparse(default) == "local_account_exists", "the account default is the bare leaf name, bound when the def runs")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    check(not {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)} & (set(MOVED) | {"local_account_exists"}),
          "the launcher defines none of them any more")
    leaf = ast.parse((ROOT / "scripts" / "host_accounts.py").read_text(encoding="utf-8"))
    check([n.name for n in leaf.body if isinstance(n, ast.FunctionDef)] == ["home_dir_for_user", "local_account_exists"],
          "the leaf holds the two account lookups")


def test_the_leaf_answers_from_the_passwd_database() -> None:
    import pwd
    from scripts.host_accounts import local_account_exists

    asked: list[str] = []

    def getpwnam(name):
        asked.append(name)
        if name == "syrd355-here":
            return SimpleNamespace(pw_name=name)
        raise KeyError(name)

    with patched(pwd, getpwnam=getpwnam):
        answers = [local_account_exists(n) for n in (" syrd355-here ", "syrd355-gone", "", "   ", None)]
    check(answers == [True, False, False, False, False] and asked == ["syrd355-here", "syrd355-gone"],
          f"stripped, blank never asked, a missing account False: {answers} {asked}")


# --- the command ----------------------------------------------------------------------------------------------------


class Command:
    """add_project_role_command's launcher facilities, answering from this test's objects, into one log."""

    def __init__(self, *, roles: tuple = (Role("main", slot=0, workdir="/w/main", user=OWNER),), caller: str = OWNER,
                 worktree_ok: bool = True, sql_error: str = "", pane_rc: int = 0, updated_roles: tuple | None = None,
                 regenerated: bool = False, pane_launcher: object = None) -> None:
        self.config = Config(roles=roles)
        added = updated_roles if updated_roles is not None else (*roles, Role("perf", slot=1, workdir="/w/perf", user=OWNER))
        self.updated = Config(roles=added, name="updated", repository=Path("/nonexistent/syrd355/repo"), pane_launcher=pane_launcher)
        self.caller, self.worktree_ok, self.sql_error, self.pane_rc = caller, worktree_ok, sql_error, pane_rc
        self.regenerated = regenerated
        self.log: list[tuple] = []
        self.said: list[str] = []

    def names(self) -> dict[str, object]:
        L = self.log

        def select_one(field, *, input_func, print_func):
            L.append(("select", field))
            return "claude"

        def plan(config, *, config_path, role_name, audit_role=False):
            L.append(("plan", config.name, role_name, audit_role))
            return SimpleNamespace(name=f"plan-for-{config.name}")

        def apply_sql(plan, *, role_name, runner):
            L.append(("sql", plan.name, role_name, runner))
            if self.sql_error:
                raise SystemExit(self.sql_error)

        def worktrees(config, *, refresh, runner):
            L.append(("worktrees", [r.role for r in config.roles], refresh, runner))
            return SimpleNamespace(ok=self.worktree_ok, failed_roles={"perf": "syrd355 no disk"} if not self.worktree_ok else {})

        return dict(
            _runtime_field=lambda role, *, default: L.append(("field", role, default)) or "FIELD",
            terminal_select=SimpleNamespace(select_one=select_one),
            _validate_new_project_audit_role=lambda name, *, context: L.append(("audit-role?", name, context)) or name.lower(),
            _validate_new_project_implementer_role=lambda name, *, context: L.append(("role?", name, context)) or name.lower(),
            _validate_new_project_cli=lambda cli, *, context: L.append(("cli?", cli, context)) or cli,
            _project_plan_for_added_role=plan,
            render_add_role_sql=lambda plan, role: L.append(("preflight-sql", plan.name, role)) or "SQL",
            _write_added_role_config=lambda config, **kw: L.append(("write-config", kw)) or (self.updated, self.regenerated),
            _write_updated_project_plan_artifacts=lambda plan, **kw: L.append(("artifacts", plan.name, kw["provision_dir"], kw["config"].name))
                or (Path("plan.json"), Path("unit.service"), Path("add.sql")),
            _apply_add_role_board_sql=apply_sql,
            _install_and_restart_board_unit=lambda plan, *, board_unit_path, runner: L.append(("restart", plan.name, board_unit_path)),
            current_user_name=lambda: L.append(("caller?",)) or self.caller,
            _owner_project_git_runner=lambda **kw: L.append(("owner-runner", kw["owner_user"], kw["project_dir"], kw["owned_roots"])) or "OWNER-RUNNER",
            _control_repository_owned_roots=lambda config: L.append(("owned-roots", config.name)) or ("ROOTS",),
            ensure_project_worktrees=worktrees,
            _role_by_name=lambda config, name: next(r for r in config.roles if r.role == name),
            role_run_as_user=lambda config, role: role.user,
            board_service_user=lambda config: "syrd355-board",
            role_control_accounts=lambda config: ("p355-main", "p355-perf"),
            home_dir_for_user=lambda user: Path("/nonexistent/syrd355/home") / user,
            pane_command_args=lambda project, role, **kw: L.append(("pane-args", project, role.role, kw)) or ["PANE", role.role],
            default_pane_state_dir_for_user=lambda user, *, project: Path("/nonexistent/syrd355/state") / user,
        )

    def runner(self, argv, **kwargs):
        self.log.append(("run", list(argv)))
        return subprocess.CompletedProcess(argv, self.pane_rc, "", "")

    def run(self, **kwargs):
        from scripts import project_role_add as m, team_launcher
        from scripts.ticket_board import project_provision

        def account_commands(project, role, owner, board_user, **kw):
            self.log.append(("account-commands", project, role, owner, board_user, kw))
            return "sudo useradd syrd355"

        kwargs.setdefault("config_path", Path("/nonexistent/syrd355/provision/p355.json"))
        kwargs.setdefault("role_name", "Perf")
        kwargs.setdefault("account_exists", lambda account: self.log.append(("account?", account)) or True)
        with patched(team_launcher, **self.names()), patched(project_provision, role_account_commands=account_commands):
            return m.add_project_role_command(self.config, runner=self.runner, print_func=self.said.append, **kwargs)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def test_the_cli_is_chosen_at_a_terminal_or_defaults_to_codex() -> None:
    fx = Command()
    fx.run(interactive=True, start=False, input_func=lambda _p: "x")
    check(fx.kinds()[:3] == ["field", "select", "role?"] and fx.log[0] == ("field", "Perf", "codex")
          and next(e for e in fx.log if e[0] == "cli?")[1] == "claude",
          f"at a terminal, the operator chooses: {fx.log[:4]}")
    fx = Command()
    fx.run(interactive=False, start=False)
    check("select" not in fx.kinds() and next(e for e in fx.log if e[0] == "cli?")[1] == "codex", "no terminal: codex")
    fx = Command()
    fx.run(cli="hermes", interactive=True, start=False)
    check("select" not in fx.kinds() and next(e for e in fx.log if e[0] == "cli?") == ("cli?", "hermes", "CLI for perf"),
          "a named CLI is never asked about, and is validated for the role")
    fx = Command()
    with patched(sys, stdin=SimpleNamespace(isatty=lambda: True)):
        fx.run(start=False, input_func=lambda _p: "x")
    check("select" in fx.kinds(), "with no preference stated, a terminal on stdin decides")


def test_validation_and_the_sql_preflight_precede_every_write() -> None:
    fx = Command()
    fx.run(cli="codex", start=False)
    check(fx.kinds()[:4] == ["role?", "cli?", "plan", "preflight-sql"] and fx.log[2] == ("plan", "config", "perf", False)
          and fx.kinds().index("preflight-sql") < fx.kinds().index("write-config"),
          f"validated, planned and its SQL rendered before the config is written: {fx.kinds()}")
    fx = Command()
    fx.run(cli="codex", audit_role=True, start=False)
    check(fx.log[0] == ("audit-role?", "Perf", "audit role") and fx.log[2] == ("plan", "config", "perf", True),
          "an auditor is validated as one and planned as one")
    fx = Command()
    names = fx.names()
    names["render_add_role_sql"] = lambda plan, role: (_ for _ in ()).throw(SystemExit("syrd355 bad sql"))
    from scripts import project_role_add as m, team_launcher
    with patched(team_launcher, **names):
        try:
            m.add_project_role_command(fx.config, config_path=Path("/nonexistent/c.json"), role_name="perf", cli="codex",
                                       runner=fx.runner, print_func=fx.said.append); raised = None
        except SystemExit as exc:
            raised = str(exc)
    check(raised == "syrd355 bad sql" and "write-config" not in fx.kinds(), "a plan whose SQL cannot render writes nothing")


def test_a_new_role_in_order_as_the_owner_and_started() -> None:
    fx = Command(caller="syrd355-operator")
    code = fx.run(cli="codex", detached=False, slot=1, relayout=True, pane_state_dir=None)
    check(code == 0, "0")
    check(fx.kinds() == ["role?", "cli?", "plan", "preflight-sql", "write-config", "plan", "artifacts", "sql", "restart",
                         "caller?", "owned-roots", "owner-runner", "worktrees", "account?", "pane-args", "run"],
          f"validate, preflight, write, re-plan from the written config, artifacts, SQL, restart, owner, worktree, "
          f"account, pane: {fx.kinds()}")
    write = fx.log[4][1]
    check(write == dict(config_path=Path("/nonexistent/syrd355/provision/p355.json"), role_name="perf", cli="codex",
                        detached=False, slot=1, relayout=True, audit_role=False, runner=fx.runner),
          f"the config written with every choice: {write}")
    check(fx.log[5] == ("plan", "updated", "perf", False) and fx.log[6] == ("artifacts", "plan-for-updated",
                                                                               Path("/nonexistent/syrd355/provision"), "updated")
          and fx.log[7][1:] == ("plan-for-updated", "perf", fx.runner) and fx.log[8][2] == Path("unit.service"),
          f"the plan for the written config, in the provision directory; the unit written is the one restarted: {fx.log[5:9]}")
    check(fx.log[11] == ("owner-runner", OWNER, Path("/nonexistent/syrd355/repo"), ("ROOTS",))
          and fx.log[12] == ("worktrees", ["perf"], True, "OWNER-RUNNER"),
          f"a caller other than the owner prepares the new role's worktree only, as the owner: {fx.log[11:13]}")
    pane = fx.log[14][3]
    check(fx.log[13] == ("account?", OWNER) and pane["run_as_user"] == OWNER and pane["no_attach"] is True
          and pane["skip_launcher_check"] is True and pane["mode"] == "attach-or-start"
          and pane["pane_state_dir"] == Path("/nonexistent/syrd355/state") / OWNER and fx.log[15] == ("run", ["PANE", "perf"]),
          f"started detached from any terminal, as its own account, with its own state dir: {pane}")
    check(fx.said == ["team-launcher: added role perf to p355; started tmux session for the new role; relaunch the project "
                      "window to display newly added visible slots"], f"said: {fx.said}")
    fx = Command(caller=OWNER, regenerated=True)
    fx.run(cli="codex", audit_role=True, start=False)
    check("owner-runner" not in fx.kinds() and next(e for e in fx.log if e[0] == "worktrees")[3] == fx.runner
          and fx.said == ["team-launcher: added auditor role perf to p355; regenerated layout for 2 visible pane(s); relaunch the "
                          "project window to display newly added visible slots"] and "account?" not in fx.kinds(),
          f"the owner itself uses the runner it was given; no start, no account asked: {fx.said}")


def test_a_half_added_role_is_recovered_without_rewriting_the_config() -> None:
    existing = (Role("main", slot=0, user=OWNER), Role("perf", slot=1, workdir="/w/perf", user=OWNER))
    fx = Command(roles=existing)
    fx.run(cli="codex", detached=True)
    check("write-config" not in fx.kinds() and [e[1] for e in fx.log if e[0] == "plan"] == ["config", "config"]
          and fx.said == ["team-launcher: role perf already exists in p355; reapplied board registration"],
          f"no config write, the existing config re-planned and registered, and a detached role says nothing about "
          f"windows: {fx.said}")
    fx = Command(roles=existing)
    fx.run(cli="codex")
    check(fx.said[0].endswith("; started tmux session for the existing role; relaunch the project window to display newly "
                              "added visible slots"), f"a visible recovered role: {fx.said}")


def test_a_missing_account_is_handed_to_the_operator_and_nothing_starts() -> None:
    fx = Command(caller="syrd355-operator")
    fx.run(cli="codex", account_exists=lambda account: fx.log.append(("account?", account)) or False)
    handoff = next((e for e in fx.log if e[0] == "account-commands"), ("none",) * 6)
    check(handoff[1:5] == (PROJECT, "perf", OWNER, "syrd355-board") and handoff[5] == dict(
        role_accounts=("p355-main", "p355-perf"), worktree="/w/perf",
        owner_home=str(Path("/nonexistent/syrd355/home") / OWNER), worktree_base="/w", control_repository=""),
          f"the whole role-control set, the owner's home and the worktree base: {handoff}")
    check("run" not in fx.kinds() and "pane-args" not in fx.kinds(), "nothing started")
    check(fx.said == ["team-launcher: added role perf to p355; its Unix account syrd355-owner does not exist yet, so the role "
                      "was not started. Run these as an operator, then start it:\nsudo useradd syrd355; started tmux session "
                      "for the new role; relaunch the project window to display newly added visible slots"],
          f"the handoff said (and the unchanged 'started' wording kept): {fx.said}")
    fx = Command(updated_roles=(Role("main", slot=0, user=OWNER), Role("perf", slot=1, workdir="/w/perf", user="")))
    fx.run(cli="codex", account_exists=lambda account: (_ for _ in ()).throw(AssertionError("asked")))
    check("run" in fx.kinds(), "no pane user: no account to ask about, and it starts")


def test_errors_stop_where_they_happen() -> None:
    fx = Command(sql_error="team-launcher: failed to register role perf in board database db: denied")
    try:
        fx.run(cli="codex"); raised = None
    except SystemExit as exc:
        raised = str(exc)
    check(raised.endswith("denied") and fx.kinds()[-1] == "sql", f"a refused registration stops before the restart: {fx.kinds()}")
    fx = Command(worktree_ok=False)
    try:
        fx.run(cli="codex"); raised = None
    except SystemExit as exc:
        raised = str(exc)
    check(raised == "team-launcher: failed to prepare worktree for perf: syrd355 no disk" and "pane-args" not in fx.kinds(),
          "a worktree that cannot be prepared stops before the start")
    fx = Command(pane_rc=3)
    try:
        fx.run(cli="codex"); raised = None
    except SystemExit as exc:
        raised = str(exc)
    check(raised == "team-launcher: added perf, but failed to start its pane with exit 3" and fx.said == [],
          "a pane that fails to start is an error, and nothing is said as done")


def test_the_pane_script_falls_back_beside_the_launcher_not_this_module() -> None:
    from scripts import team_launcher

    fx = Command()
    fake = "/nonexistent/syrd355/elsewhere/team_launcher.py"
    with patched(team_launcher, __file__=fake):
        fx.run(cli="codex")
    script = next(e for e in fx.log if e[0] == "pane-args")[3]["script_path"]
    check(script == Path(fake).resolve().with_name("team-launcher")
          and script.parent != (ROOT / "scripts").resolve(),
          f"the launcher's own file decides, read when the command runs: {script}")
    fx = Command()
    fx.run(cli="codex", script_path=Path("/nonexistent/syrd355/given"))
    check(next(e for e in fx.log if e[0] == "pane-args")[3]["script_path"] == Path("/nonexistent/syrd355/given"),
          "a given script path wins over the fallback")
    fx = Command(pane_launcher=Path("/nonexistent/syrd355/project-launcher"))
    fx.run(cli="codex", script_path=Path("/nonexistent/syrd355/given"))
    check(next(e for e in fx.log if e[0] == "pane-args")[3]["script_path"] == Path("/nonexistent/syrd355/project-launcher"),
          "the project's own pane launcher wins over both")


# --- the helpers ----------------------------------------------------------------------------------------------------


def test_slots_payloads_and_the_close_role() -> None:
    from scripts import project_role_add as m

    config = Config(roles=(Role("a", slot=0), Role("b", slot=3), Role("c", detached=True, slot=9), Role("d", slot=None)))
    check(m._next_visible_role_slot(config) == 4 and m._next_visible_role_slot(Config()) == 0,
          "one past the highest visible slot, detached and unslotted roles ignored; 0 for none")
    base = dict(cli=["codex"], live_commands=["codex"], role="perf", target="p355-perf:0.0", tmux_session="p355-perf", yolo=True)
    check(m._add_role_payload(Config(), role_name="perf", cli="codex", detached=False, slot=2) == {**base, "slot": 2},
          "a visible role gets its slot")
    check(m._add_role_payload(Config(worktree_base=Path("/wt"), control_repository=Path("/c"), repository=Path("/r")),
                              role_name="perf", cli="codex", detached=True, slot=None, directorctl="/b/directorctl")
          == {**base, "detached": True, "env": {"TICKET_BOARD_DIRECTORCTL": "/b/directorctl"}, "workdir": "/wt/perf"},
          "a detached role, its directorctl, and its own worktree when the project has a control repository")
    check(m._add_role_payload(Config(repository=Path("/r")), role_name="perf", cli="codex", detached=True, slot=None)["workdir"] == "/r",
          "otherwise the repository")
    try:
        m._add_role_payload(Config(), role_name="perf", cli="codex", detached=False, slot=None); raised = None
    except ValueError as exc:
        raised = str(exc)
    check(raised == "visible role requires slot", "a visible role without a slot is a programming error")
    check(m._vcs_close_role_from_plan_data({"operation_allowed_roles": [["x"], ["mark_done", []], ["open", ["a"]],
                                                                          ("mark_done", ("ops", "main"))]}) == "ops"
          and m._vcs_close_role_from_plan_data({}) is None, "the first role allowed to mark done, from well-formed entries only")


def test_the_design_artifact_records_the_role() -> None:
    from scripts import project_role_add as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd355-artifact.") as raw:
        config_path = Path(raw) / "provision" / "p355.json"
        config_path.parent.mkdir()
        artifact = Path(raw) / "p355.project.json"
        check(m._update_project_design_artifact_for_role(Config(), config_path, role_name="perf", cli="codex") is None,
              "no artifact: nothing")
        artifact.write_text(json.dumps({"project": {"roles": ["Main", " "], "role_clis": "bad"}}), encoding="utf-8")
        written: list[Path] = []
        real = team_launcher._write_json_atomic
        with patched(team_launcher, _write_json_atomic=lambda path, data: written.append(path) or real(path, data)):
            path = m._update_project_design_artifact_for_role(Config(), config_path, role_name="perf", cli="claude", audit_role=True)
        data = json.loads(artifact.read_text(encoding="utf-8"))["project"]
        check(path == artifact and written == [artifact] and data == {"roles": ["Main", " "], "audit_roles": ["perf"],
                                                                     "include_audit": True, "role_clis": {"perf": "claude"}},
              f"an auditor recorded under audit_roles, the CLI map rebuilt, through the launcher's writer: {data}")
        artifact.write_text(json.dumps({"project": "not a dict"}), encoding="utf-8")
        check(m._update_project_design_artifact_for_role(Config(), config_path, role_name="perf", cli="codex") is None,
              "an artifact without a project mapping is left alone")


def test_plan_artifacts_and_board_sql() -> None:
    from scripts import project_role_add as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd355-plan.") as raw:
        owned: list[Path] = []
        plan = SimpleNamespace(board_unit="p355-ticket-board.service", project=PROJECT, x=1)
        with patched(team_launcher, render_board_unit=lambda p: "UNIT", render_add_role_sql=lambda p, role: f"SQL {role}",
                     ensure_owner_file=lambda config, path, *, runner: owned.append(path)):
            paths = m._write_updated_project_plan_artifacts(plan, role_name="perf", provision_dir=Path(raw), runner="R", config=Config())
        check(paths == (Path(raw) / "plan.json", Path(raw) / "p355-ticket-board.service", Path(raw) / "p355-add-role.sql")
              and owned == list(paths) and json.loads(paths[0].read_text()) == {"board_unit": "p355-ticket-board.service",
                                                                                 "project": PROJECT, "x": 1}
              and paths[1].read_text() == "UNIT" and paths[2].read_text() == "SQL perf",
              "the plan, unit and SQL written and each given to the owner")
    calls: list = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, calls and len(calls) - 1, "", "denied")

    plan = SimpleNamespace(admin_database_url="postgresql:///admin", database="p355db")
    with patched(team_launcher, render_add_role_sql=lambda p, role: f"SQL {role}",
                 _proc_failure_reason=lambda result, default: f"{default}: {result.stderr}"):
        m._apply_add_role_board_sql(plan, role_name="perf", runner=runner)
        try:
            m._apply_add_role_board_sql(plan, role_name="perf", runner=runner); raised = None
        except SystemExit as exc:
            raised = str(exc)
    check(calls[0][0] == ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "postgresql:///admin", "-f", "-"]
          and calls[0][1] == dict(input="SQL perf", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
          and raised == "team-launcher: failed to register role perf in board database p355db: psql failed with exit 1: denied",
          f"psql as postgres on stdin; a failure names the database and the reason: {raised}")


def test_the_role_plan_is_built_from_the_recorded_plan() -> None:
    from scripts import project_role_add as m, team_launcher

    built: list[dict] = []
    plan_data = {"owner_user": "rec-owner", "port": 8355, "board_root": "/b", "asset_dir": "/a",
                 "operation_allowed_roles": [["mark_done", ["ops"]]]}
    names = dict(
        _plan_data_from_config=lambda config, path: plan_data, _load_json=lambda path: {},
        _configured_implementer_roles=lambda config, *, plan_data, extra_role: ("main", *([extra_role] if extra_role else [])),
        _configured_audit_roles=lambda config, *, plan_data, extra_role: tuple([extra_role] if extra_role else []),
        _loaded_plan_field=lambda data, key, default: data.get(key, default),
        _owner_home_from_plan_data=lambda config, data: Path("/home/rec-owner"),
        _regenerated_control_user=lambda config, data: "ctl", _repo_root=lambda: Path("/repo"),
        _commit_git_dir_from_plan_data=lambda config, data: "cache", current_user_name=lambda: "me",
        _tenant_board_root_from_config=lambda config: Path("/tenant-board"),
        build_plan=lambda **kw: built.append(kw) or "PLAN",
    )
    config = SimpleNamespace(project=PROJECT, project_name="P355", run_as_user=OWNER, ticket_prefix="SY",
                             roles=[SimpleNamespace(role="designer")])
    with patched(team_launcher, **names):
        check(m._project_plan_for_added_role(config, config_path=Path("/c.json"), role_name="perf") == "PLAN", "the built plan")
        m._project_plan_for_added_role(config, config_path=Path("/c.json"), role_name="perf", audit_role=True)
    check(built[0]["implementer_roles"] == ("main", "perf") and built[0]["audit_roles"] == () and built[0]["include_audit"] is False
          and built[1]["implementer_roles"] == ("main",) and built[1]["audit_roles"] == ("perf",) and built[1]["include_audit"] is True,
          "a role joins the implementers, an auditor the auditors")
    check({k: built[0][k] for k in ("owner_user", "port", "board_root", "asset_dir", "frame_dir", "include_designer",
                                     "vcs_close_role", "ticket_prefix", "source_repo", "database")}
          == dict(owner_user="rec-owner", port=8355, board_root=Path("/b"), asset_dir=Path("/a"), frame_dir=None,
                  include_designer=True, vcs_close_role="ops", ticket_prefix="SY", source_repo=Path("/repo"), database=None),
          f"recorded values win, the rest from the configuration: {built[0]}")
    with patched(team_launcher, **dict(names, _load_json=lambda path: {"workflow": {"x": 1}})):
        try:
            m._project_plan_for_added_role(config, config_path=Path("/c.json"), role_name="perf"); raised = None
        except SystemExit as exc:
            raised = str(exc)
    check(raised == "use ticket-board-workflow apply to update configured roles and stages", "a declared workflow refuses")


def test_a_recognized_generated_layout() -> None:
    from scripts import project_role_add as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd355-layout.") as raw:
        layout = Path(raw) / "layout.json"
        layout.write_text(json.dumps({"known": 2}), encoding="utf-8")
        config = Config(roles=(Role("a", slot=0), Role("b", slot=1), Role("c", detached=True)), layout=layout)
        with patched(team_launcher, _is_generated_project_layout_template=lambda config, *, config_path: True,
                     _known_generated_project_layout_payloads=lambda count: [{"known": count}]):
            check(m._is_recognized_generated_project_layout(config, config_path=Path(raw)), "its payload for 2 visible roles")
            layout.write_text("{not json", encoding="utf-8")
            check(not m._is_recognized_generated_project_layout(config, config_path=Path(raw)), "unreadable: not recognized")
        with patched(team_launcher, _is_generated_project_layout_template=lambda config, *, config_path: False):
            check(not m._is_recognized_generated_project_layout(config, config_path=Path(raw)), "not a template: not recognized")


def test_writing_the_added_role_config() -> None:
    from scripts import project_role_add as m, team_launcher

    def run(config, *, recognized=False, layout_slots=2, raw_roles="list", **kw):
        log: list = []
        with tempfile.TemporaryDirectory(prefix="syrd355-config.") as raw:
            layout = Path(raw) / "layout.json"
            nxt = max([r.slot for r in config.roles if not r.detached and r.slot is not None] or [-1]) + 1
            loaded = Config(roles=(*config.roles, Role("perf", slot=kw.get("slot") if kw.get("slot") is not None else nxt)), layout=layout, name="loaded")
            names = dict(
                _is_recognized_generated_project_layout=lambda c, *, config_path: recognized,
                _layout_slot_count=lambda c: layout_slots,
                _load_json=lambda path: {"roles": []} if raw_roles == "list" else {"roles": "x"},
                _tenant_board_root_from_config_or_plan=lambda c, p: Path("/b"),
                _write_json_atomic=lambda path, data: log.append(("write", data["roles"][-1]["role"], data["roles"][-1].get("slot"))),
                ensure_owner_file=lambda c, path, *, runner: log.append(("own", Path(path).name)),
                load_project_config=lambda project, path: log.append(("load",)) or loaded,
                _new_project_layout_payload=lambda count: {"panes": count},
                _update_project_design_artifact_for_role=lambda c, p, **k: log.append(("artifact", k["audit_role"])) or None,
            )
            with patched(team_launcher, **names):
                try:
                    result = m._write_added_role_config(config, config_path=Path(raw) / "p355.json", role_name="perf", cli="codex",
                                                        runner="R", **kw)
                except SystemExit as exc:
                    result = str(exc)
            regenerated = json.loads(layout.read_text()) if layout.exists() else None
        return result, log, regenerated

    two = Config(roles=(Role("main", slot=0), Role("ops", slot=1)))
    result, log, layout = run(two, detached=False, slot=None, layout_slots=3)
    check(result[1] is False and log == [("write", "perf", 2), ("own", "p355.json"), ("load",), ("artifact", False), ("load",)]
          and layout is None, f"a custom layout with room: the next slot, no relayout: {log}")
    result, log, layout = run(two, detached=False, slot=None, recognized=True)
    check(result[1] is True and layout == {"panes": 3} and ("own", "layout.json") in log,
          f"a recognized generated layout is regenerated for the visible roles: {layout}")
    result, _, _ = run(two, detached=False, slot=5, recognized=True)
    check(result == "team-launcher: generated layouts append new visible roles at slot 2", result)
    result, _, _ = run(two, detached=False, slot=None, relayout=True)
    check(result[1] is True, "--relayout regenerates")
    result, _, _ = run(two, detached=False, slot=4, relayout=True)
    check(result == "team-launcher: --relayout regenerates visible roles contiguously; add perf at slot 2 or omit --slot", result)
    result, _, _ = run(two, detached=False, slot=2, layout_slots=2)
    check(isinstance(result, str) and result.startswith("team-launcher: cannot add perf to visible slot 2; layout ") and result.endswith(
        "has 2 slot(s), so no pane exists for the new role; use --detached to add it headless, or pass --relayout to replace "
        "the existing layout with a generated 3-pane layout"), result)
    result, _, _ = run(two, detached=False, slot=1)
    check(result == "team-launcher: cannot add perf to slot 1; slot is occupied by ops", result)
    result, _, _ = run(Config(roles=tuple(Role(f"r{i}", slot=i) for i in range(6))), detached=False, slot=None)
    check(result.startswith("team-launcher: cannot add perf as visible; at most 6 panes"), result)
    result, _, _ = run(two, detached=True, slot=-1)
    check(result == "team-launcher: add-role slot must be non-negative", result)
    result, _, _ = run(Config(roles=(Role("perf", slot=0),)), detached=True, slot=None)
    check(result == "team-launcher: role 'perf' already exists in project p355", result)
    result, log, _ = run(two, detached=True, slot=None, audit_role=True)
    check(result[1] is False and log[0] == ("write", "perf", None) and ("artifact", True) in log,
          f"a detached auditor: no slot, no layout, recorded as an auditor: {log}")
    result, _, _ = run(two, detached=True, slot=None, raw_roles="bad")
    check(result.endswith("must define a roles list"), result)


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_modules_load_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_the_modules_own_names_and_the_launchers_file")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"project_role_add_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
