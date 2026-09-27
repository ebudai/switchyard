#!/usr/bin/env python3
"""SYRD-357: the shared role-plan data and board-unit helpers, against the launcher they came out of.

Seven functions -- `_configured_implementer_roles`, `_configured_audit_roles`,
`_regenerated_control_user`, `_loaded_plan_field`, `_owner_home_from_plan_data`,
`_commit_git_dir_from_plan_data`, `_install_and_restart_board_unit` -- moved into
`scripts/project_role_plan_support.py` unchanged. `switchyard add-role` and
`switchyard set-vcs-close-role` read them through the launcher, and are
byte-identical. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- **One set of objects.** The launcher re-exports every name, the very same
  objects whichever module is imported first.
- **Seams (rule 24).** Every launcher facility they use, and each helper here
  another helper calls, is read from the launcher when it runs. The consumers'
  patches on the launcher therefore still reach them. Nothing they bind,
  comprehension variables included, is read through the launcher (rule 27).
- **The behaviour is unchanged:**
  - role lists from the recorded plan, normalised and each once in order, or
    from the configuration, with the new role added only if absent;
  - a recorded field used unless missing, `None` or empty;
  - the owner-home and cache fallbacks computed before the recorded value is
    consulted;
  - the controller from the installed grant, for the invoking human and the
    recorded (or configured) owner, never from the document;
  - the board unit installed, systemd reloaded and the board restarted,
    stopping at the first failure.

Every facility is this test's own recording fake, installed on the launcher
before anything runs. No passwd, grant, home, unit, SQL or service is touched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("_configured_implementer_roles", "_configured_audit_roles", "_regenerated_control_user", "_loaded_plan_field",
         "_owner_home_from_plan_data", "_commit_git_dir_from_plan_data", "_install_and_restart_board_unit")
OWN = ("subprocess", "Path", "Any", "Callable")
#: Per function, the launcher names it reads when it runs and how often (measured on the SYRD-357 baseline).
SEAMS = {
    "_configured_implementer_roles": {"NEW_PROJECT_RESERVED_ROLE_NAMES": 1},
    "_configured_audit_roles": {"_dedupe_role_names": 1},
    "_regenerated_control_user": {"_loaded_plan_field": 1, "current_user_name": 1, "resolve_control_user": 1, "invoking_human": 1},
    "_loaded_plan_field": {},
    "_owner_home_from_plan_data": {"_loaded_plan_field": 1, "_owner_home_for_auth": 1, "current_user_name": 1},
    "_commit_git_dir_from_plan_data": {"_loaded_plan_field": 1, "commit_git_dir_env_for_project": 1, "_owner_home_from_plan_data": 1},
    "_install_and_restart_board_unit": {},
}


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


def config(*roles: str, run_as_user: str = "syrd357-owner") -> SimpleNamespace:
    return SimpleNamespace(project="p357", run_as_user=run_as_user, roles=[SimpleNamespace(role=r) for r in roles])


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.project_role_plan_support as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.project_role_plan_support", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.project_role_plan_support"),
                  ("scripts.project_role_add", "scripts.project_vcs_close_role", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_role_plan_support as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: one set of objects: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_modules_own_names_and_the_consumers() -> None:
    module = ast.parse((ROOT / "scripts" / "project_role_plan_support.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    check(list(functions) == list(MOVED), f"exactly the moved functions, in baseline order: {list(functions)}")
    every = {name for seams in SEAMS.values() for name in seams}
    for name, seams in SEAMS.items():
        function = functions[name]
        through: dict[str, int] = {}
        for n in ast.walk(function):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        bare = sorted({n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every})
        bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        check(through == seams and not bare and not (bound | set(OWN)) & set(through),
              f"{name} reads {seams} through the launcher, none bare, nothing it binds: {through} {bare}")
    for consumer, count in (("project_role_add.py", 12), ("project_vcs_close_role.py", 12)):
        tree = ast.parse((ROOT / "scripts" / consumer).read_text(encoding="utf-8"))
        reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                 and n.value.id == "launcher" and n.attr in MOVED]
        bare = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in MOVED]
        check(len(reads) == count and not bare, f"{consumer} still reads them through the launcher: {len(reads)}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    order = [n.module for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module in (
        "scripts.project_role_plan_support", "scripts.project_role_add", "scripts.project_vcs_close_role")]
    check(order == ["scripts.project_role_plan_support", "scripts.project_role_add", "scripts.project_vcs_close_role"]
          and not {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)} & set(MOVED),
          f"the launcher defines none of them and imports them before their consumers: {order}")


# --- role lists -----------------------------------------------------------------------------------------------------


def test_implementer_roles_recorded_or_configured() -> None:
    from scripts import project_role_plan_support as m, team_launcher

    with patched(team_launcher, NEW_PROJECT_RESERVED_ROLE_NAMES=frozenset({"director", "designer"})):
        check(m._configured_implementer_roles(config("x"), plan_data={"implementer_roles": [" Main ", "ops", "MAIN", "", "  ", 7]})
              == ("main", "ops", "7"), "recorded: stripped, lower-cased, blank dropped, each once in order, non-strings as text")
        check(m._configured_implementer_roles(config("director", "main", "designer", "ops", "main"))
              == ("main", "ops"), "no recorded list: the configured roles, reserved ones filtered, each once")
        check(m._configured_implementer_roles(config("main"), plan_data={"implementer_roles": "main,ops"}) == ("main",),
              "a malformed recorded list is ignored for the configuration")
        check(m._configured_implementer_roles(config("main"), plan_data=None, extra_role="perf") == ("main", "perf")
              and m._configured_implementer_roles(config("main"), extra_role="main") == ("main",)
              and m._configured_implementer_roles(config("main"), extra_role="") == ("main",),
              "the new role appended only when named and absent")
        check(m._configured_implementer_roles(config("main"), plan_data={"implementer_roles": []}) == (),
              "an empty recorded list is a recorded list")


def test_audit_roles_recorded_or_configured() -> None:
    from scripts import project_role_plan_support as m, team_launcher

    asked: list = []
    with patched(team_launcher, _dedupe_role_names=lambda roles: asked.append(roles) or tuple(dict.fromkeys(roles))):
        check(m._configured_audit_roles(config("main"), plan_data={"audit_roles": [" Audit ", "sec", "AUDIT", ""]}) == ("audit", "sec")
              and asked == [("audit", "sec", "audit")], f"recorded: normalised, then the launcher's de-duplicator: {asked}")
        check(m._configured_audit_roles(config("main", "audit")) == ("audit",) and m._configured_audit_roles(config("main")) == (),
              "no recorded list: audit only when the configuration has it")
        check(m._configured_audit_roles(config("main"), plan_data={"audit_roles": "audit"}) == (), "a malformed list is ignored")
        check(m._configured_audit_roles(config("audit"), extra_role="sec") == ("audit", "sec")
              and m._configured_audit_roles(config("audit"), extra_role="audit") == ("audit",), "the new auditor only if absent")


# --- recorded fields and provenance ---------------------------------------------------------------------------------


def test_a_recorded_field_is_used_unless_missing_none_or_empty() -> None:
    from scripts import project_role_plan_support as m

    data = {"none": None, "empty": "", "false": False, "zero": 0, "list": [], "value": "v"}
    answers = {key: m._loaded_plan_field(data, key, "DEFAULT") for key in ("missing", "none", "empty", "false", "zero", "list", "value")}
    check(answers == {"missing": "DEFAULT", "none": "DEFAULT", "empty": "DEFAULT", "false": False, "zero": 0, "list": [], "value": "v"},
          f"False, 0 and [] are values: {answers}")


def provenance(log: list) -> dict:
    return dict(
        current_user_name=lambda: log.append(("current",)) or "syrd357-me",
        invoking_human=lambda: log.append(("human",)) or "syrd357-human",
        resolve_control_user=lambda project, *, invoking_user, owner_user: log.append(("grant", project, invoking_user, owner_user)) or "ctl",
        _owner_home_for_auth=lambda user: log.append(("home", user)) or Path(f"/nonexistent/syrd357/{user}"),
        commit_git_dir_env_for_project=lambda *, project, owner_home: log.append(("cache", project, owner_home)) or "/cache/env",
    )


def test_the_controller_comes_from_the_installed_grant() -> None:
    from scripts import project_role_plan_support as m, team_launcher

    log: list = []
    with patched(team_launcher, **provenance(log)):
        check(m._regenerated_control_user(config(), {"owner_user": "rec-owner", "control_user": "tenant-says"}) == "ctl"
              and log == [("human",), ("grant", "p357", "syrd357-human", "rec-owner")],
              f"the grant, for the invoking human and the recorded owner; the document's controller never read: {log}")
        log.clear()
        m._regenerated_control_user(config(), {})
        check(log[-1] == ("grant", "p357", "syrd357-human", "syrd357-owner"), "no recorded owner: the configured one")
        log.clear()
        m._regenerated_control_user(config(run_as_user=""), {"owner_user": ""})
        check(log[-1] == ("grant", "p357", "syrd357-human", "syrd357-me"), "no owner anywhere: the current user")


def test_owner_home_and_cache_fallbacks_are_computed_first() -> None:
    from scripts import project_role_plan_support as m, team_launcher

    log: list = []
    with patched(team_launcher, **provenance(log)):
        home = m._owner_home_from_plan_data(config(), {"owner_home": "/recorded/home"})
        check(home == Path("/recorded/home") and log == [("home", "syrd357-owner")],
              f"a recorded home wins, but the fallback was computed anyway: {log}")
        log.clear()
        check(m._owner_home_from_plan_data(config(run_as_user=""), {"owner_home": ""}) == Path("/nonexistent/syrd357/syrd357-me")
              and log == [("current",), ("home", "syrd357-me")], f"no recorded home: the current user's: {log}")
        log.clear()
        cache = m._commit_git_dir_from_plan_data(config(), {"commit_git_dir": 42, "owner_home": "/rec"})
        check(cache == "42" and log == [("home", "syrd357-owner"), ("cache", "p357", Path("/rec"))],
              f"a recorded cache wins as text, and the fallback was computed for the recorded home first: {log}")
        log.clear()
        check(m._commit_git_dir_from_plan_data(config(), {}) == "/cache/env"
              and log[-1] == ("cache", "p357", Path("/nonexistent/syrd357/syrd357-owner")), f"no recorded cache: the owner's: {log}")


# --- the board unit -------------------------------------------------------------------------------------------------


def test_the_board_unit_install_reload_restart_stopping_at_the_first_failure() -> None:
    from scripts import project_role_plan_support as m

    plan = SimpleNamespace(board_unit="p357-ticket-board.service")
    expected = [["sudo", "install", "-m", "0644", "/p/unit.service", "/etc/systemd/system/p357-ticket-board.service"],
                ["sudo", "systemctl", "daemon-reload"], ["sudo", "systemctl", "restart", "p357-ticket-board.service"]]
    messages = ["team-launcher: failed to install updated board unit p357-ticket-board.service",
                "team-launcher: failed to reload systemd after updating the board unit",
                "team-launcher: failed to restart p357-ticket-board.service"]
    for failing in (None, 0, 1, 2):
        calls: list = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 1 if len(calls) - 1 == failing else 0, "", "")

        try:
            result = m._install_and_restart_board_unit(plan, board_unit_path=Path("/p/unit.service"), runner=runner); raised = None
        except SystemExit as exc:
            result, raised = None, str(exc)
        stop = 3 if failing is None else failing + 1
        check([c[0] for c in calls] == expected[:stop] and all(c[1] == {} for c in calls)
              and raised == (None if failing is None else messages[failing]) and result is None,
              f"failing at {failing}: the steps up to it, its own message, nothing after: {raised}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_modules_own_names_and_the_consumers")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"project_role_plan_support_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
