#!/usr/bin/env python3
"""SYRD-367: teardown planning and the command, against the launcher they came out of.

`_port_from_board_url`, `_teardown_project_context`,
`_ticket_board_existing_ticket_count`, `_bash_action`, `_drop_database_command`,
`TEARDOWN_PROTECTED_USERS`, `TEARDOWN_MINIMUM_OWNER_UID`,
`owner_removal_refusal`, `_owner_removal_actions`, `owner_removal_residue`,
`_switchyard_teardown_actions`, `_print_teardown_plan`,
`_confirm_teardown_project`, `_run_teardown_actions`,
`switchyard_teardown_command`, `SwitchyardTeardownAction` and
`SwitchyardTeardownPlan` moved into `scripts/project_teardown.py` unchanged, and
`uid_for_user` moved unchanged into the `scripts/host_accounts.py` leaf. This
pins what makes that safe:

- **No cycle.** The module imports only the standard library and
  `uid_for_user` from the `host_accounts` leaf at its top, and the leaf is still
  a leaf.
- **One set of objects.** The launcher re-exports every name -- the classes and
  `uid_for_user` included -- whichever module is imported first, so `main` and
  `switchyard_main` dispatch to the same command.
- **The eager default.** `owner_removal_refusal`'s `uid_lookup` default is the
  very `uid_for_user` the launcher has, bound when the module loads; and the
  suites' `team_launcher.pwd.getpwnam` patches still reach it.
- **Seams (rule 24).** Every launcher facility, every moved helper and constant
  another moved definition reads, and the two classes' construction are read
  from the launcher when they run. Nothing they bind is read through it.
- **The behaviour is unchanged:** context resolution and its refusal, the
  board-count SQL, every owner-protection rule, every rendered action in order,
  the plan printout, the guards, dry-run, confirmation, failure detail and the
  residue read-back.

Every facility is this test's own fake: every runner records rather than runs,
and every registry, config, plan, uid, approval, prompt and caller lookup is
refused unless a test stands one in. Actions are rendered and read as strings;
none is ever executed. Paths are temporary directories.
"""

from __future__ import annotations

import ast
import dataclasses
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
MOVED = ("_port_from_board_url", "_teardown_project_context", "_ticket_board_existing_ticket_count", "_bash_action", "_drop_database_command",
         "TEARDOWN_PROTECTED_USERS", "TEARDOWN_MINIMUM_OWNER_UID", "owner_removal_refusal", "_owner_removal_actions", "owner_removal_residue",
         "_switchyard_teardown_actions", "_print_teardown_plan", "_confirm_teardown_project", "_run_teardown_actions", "switchyard_teardown_command",
         "SwitchyardTeardownAction", "SwitchyardTeardownPlan")
OWN = ("os", "re", "shlex", "subprocess", "dataclass", "Path", "Any", "Callable", "Mapping", "Sequence", "uid_for_user")
SEAMS = {
    "_teardown_project_context": {"_validate_project_slug": 1, "switchyard_registry_dir": 1, "_usable_switchyard_entry_for_project": 1,
                                  "_default_new_project_owner": 3, "build_plan": 2, "commit_git_dir_env_for_project": 1, "load_project_config": 1,
                                  "_project_dir_from_generated_config_path": 1, "_project_board_provision_from_json": 1, "_port_from_board_url": 1,
                                  "_repo_root": 1},
    "_drop_database_command": {"sql_identifier": 1},
    "owner_removal_refusal": {"TEARDOWN_PROTECTED_USERS": 1, "current_user_name": 1, "read_host_desktop_approval": 1, "TEARDOWN_MINIMUM_OWNER_UID": 2},
    "_owner_removal_actions": {"SwitchyardTeardownAction": 4, "_bash_action": 4},
    "_switchyard_teardown_actions": {"SwitchyardTeardownAction": 14, "_bash_action": 2, "tenant_control_helper_path": 2, "_drop_database_command": 1,
                                     "_owner_removal_actions": 1},
    "_confirm_teardown_project": {"_read_prompt": 1},
    "switchyard_teardown_command": {"_teardown_project_context": 1, "_ticket_board_existing_ticket_count": 1, "SwitchyardTeardownPlan": 1,
                                    "_switchyard_teardown_actions": 1, "owner_removal_refusal": 1, "_print_teardown_plan": 1,
                                    "_confirm_teardown_project": 1, "_run_teardown_actions": 1, "owner_removal_residue": 1},
}
SLUG = "p367"


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


#: Every launcher facility that could reach a host for real, refused.
LIVE = {name: refuse(name) for name in (
    "_validate_project_slug", "switchyard_registry_dir", "_usable_switchyard_entry_for_project", "_default_new_project_owner", "build_plan",
    "commit_git_dir_env_for_project", "load_project_config", "_project_dir_from_generated_config_path", "_project_board_provision_from_json",
    "_repo_root", "current_user_name", "read_host_desktop_approval", "_read_prompt")}
#: Pure renderers the actions use, fixed so the rendered commands are this test's.
RENDER = dict(sql_identifier=lambda name: f'"{name}"', tenant_control_helper_path=lambda project: f"/usr/local/libexec/switchyard-control-{project}")


def recorder(answers: list, calls: list):
    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        answer = answers.pop(0) if answers else subprocess.CompletedProcess(argv, 0, "", "")
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return run


PLAN = SimpleNamespace(project=SLUG, owner_user="syrd367-owner", listener_unit="p367-notify.service", board_unit="p367-ticket-board.service",
                       board_root="/srv/p367/live", tmpfiles_name="p367.conf", tenant_control_sudoers_name="47-p367-control",
                       polkit_name="50-p367.rules", database="p367_board")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_the_host_accounts_leaf_at_import() -> None:
    result = python("import sys, scripts.project_teardown as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.host_accounts']",
          f"it loads only the host_accounts leaf, never the launcher: {result.stdout}{result.stderr[-400:]}")
    result = python("import sys, scripts.host_accounts as h; print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != h.__name__))")
    check(result.stdout.strip() == "[]", f"and host_accounts is still a leaf: {result.stdout}{result.stderr[-300:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.project_teardown", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_teardown"),
                  ("scripts.host_accounts", "scripts.project_teardown", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_teardown as m, scripts.host_accounts as h; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}),"
                        " t.uid_for_user is h.uid_for_user and m.owner_removal_refusal.__kwdefaults__['uid_lookup'] is h.uid_for_user)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_classes_the_constants_and_the_leaf() -> None:
    module = ast.parse((ROOT / "scripts" / "project_teardown.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    names = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else n.targets[0].id for n in module.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the seventeen, in baseline order: {names}")
    every = {name for reads in SEAMS.values() for name in reads}
    for f in [n for n in module.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        annotations = set()
        for x in ast.walk(f):
            if isinstance(x, ast.arg) and x.annotation: annotations |= {id(y) for y in ast.walk(x.annotation)}
            if isinstance(x, ast.FunctionDef) and x.returns: annotations |= {id(y) for y in ast.walk(x.returns)}
            if isinstance(x, ast.AnnAssign): annotations |= {id(y) for y in ast.walk(x.annotation)}
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every and id(n) not in annotations})
        check(through == SEAMS.get(f.name, {}) and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")

    from scripts import host_accounts, project_teardown as m, team_launcher

    check(m.TEARDOWN_PROTECTED_USERS == frozenset({"root", "nobody"}) and m.TEARDOWN_MINIMUM_OWNER_UID == 1000
          and m.TEARDOWN_PROTECTED_USERS is team_launcher.TEARDOWN_PROTECTED_USERS, "the constants unchanged, one object")
    for cls in (m.SwitchyardTeardownAction, m.SwitchyardTeardownPlan):
        check(cls.__dataclass_params__.frozen and cls is getattr(team_launcher, cls.__name__), f"{cls.__name__}: frozen, one object")
    check([f.name for f in dataclasses.fields(m.SwitchyardTeardownPlan)] == ["project", "owner_user", "owner_home", "project_checkout",
                                                                           "ticket_count", "registered_healthy", "registry_path", "actions"],
          "the plan's fields in order")
    check(m.owner_removal_refusal.__kwdefaults__ == {"caller": "", "uid_lookup": host_accounts.uid_for_user, "desktop_approval": None}
          and m.switchyard_teardown_command.__kwdefaults__["home_base"] == Path("/home")
          and m.switchyard_teardown_command.__kwdefaults__["runner"] is subprocess.run
          and m.switchyard_teardown_command.__kwdefaults__["input_func"] is input and m.switchyard_teardown_command.__kwdefaults__["print_func"] is print
          and m.owner_removal_residue.__kwdefaults__ == {"runner": subprocess.run},
          "every default is the object it was, the uid lookup the leaf's")
    leaf = ast.parse((ROOT / "scripts" / "host_accounts.py").read_text(encoding="utf-8"))
    check([n.name for n in leaf.body if isinstance(n, ast.FunctionDef)] == ["home_dir_for_user", "local_account_exists", "uid_for_user"]
          and host_accounts.pwd is team_launcher.pwd,
          "the leaf holds exactly its two lookups and uid_for_user, reading the launcher's very pwd module")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))} | {
        t.id for n in launcher.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    check(not defined & (set(MOVED) | {"uid_for_user"})
          and {"_recorded_owner_home", "_plan_migration_reference", "_project_board_provision_from_json",
               "_project_dir_from_generated_config_path"} <= defined,
          f"the launcher defines none of them, and keeps the shared plan parser chain and checkout helper: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_teardown"]
    leaf_import = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.host_accounts"]
    check(exported == [sorted(MOVED)] and leaf_import == [["home_dir_for_user", "local_account_exists", "uid_for_user"]],
          f"one explicit re-export of all seventeen, and uid_for_user from the leaf: {exported} {leaf_import}")


def test_the_launchers_pwd_patches_still_reach_the_moved_uid_lookup() -> None:
    from scripts import host_accounts, team_launcher as t

    saved = t.pwd.getpwnam
    t.pwd.getpwnam = lambda name: SimpleNamespace(pw_uid=4367) if name == "someone" else (_ for _ in ()).throw(KeyError(name))
    try:
        check(t.uid_for_user("someone") == 4367 and host_accounts.uid_for_user("nobody-here") is None,
              "a team_launcher.pwd.getpwnam patch reaches the moved lookup, and a missing account is None")
    finally:
        t.pwd.getpwnam = saved


# --- context and the board count ----------------------------------------------------------------------------------------


def test_the_board_url_port() -> None:
    from scripts import team_launcher as t

    for url, port in (("http://127.0.0.1:8367/", 8367), ("https://localhost:443", 443), (" http://localhost:1/x ", 1),
                      ("http://127.0.0.1:65536/", None), ("http://127.0.0.1:0/", None), ("http://example.com:80/", None),
                      ("http://127.0.0.1/", None), ("ftp://127.0.0.1:21/", None), ("http://127.0.0.1:123456/", None)):
        check(judged(t._port_from_board_url, url) == port, f"{url!r} -> {port}")


def test_the_teardown_context() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        built: list = []
        seams = dict(LIVE, _validate_project_slug=lambda slug: slug, switchyard_registry_dir=lambda: root / "registry",
                     _default_new_project_owner=lambda slug: f"{slug}-agent", commit_git_dir_env_for_project=lambda **kw: f"git:{kw['project']}",
                     build_plan=lambda **kw: built.append(kw) or SimpleNamespace(**kw))
        with patched(t, **{**seams, "_usable_switchyard_entry_for_project": lambda slug, **kw: (None, ["syrd.json: config unreadable"])}):
            refused = judged(t._teardown_project_context, SLUG, owner_user=None, config_dir=None, registry_dir=None, home_base=root)
        check(isinstance(refused, SystemExit) and str(refused) == (
            f"switchyard: refusing to infer teardown artifacts for registered project '{SLUG}' because its launch entry cannot be loaded:\n"
            "  - syrd.json: config unreadable\nswitchyard: rerun teardown with permissions that can read the project config, or repair the registry entry first"),
              f"a registered but unloadable entry is refused, not guessed at: {refused!r}")
        with patched(t, **{**seams, "_usable_switchyard_entry_for_project": lambda slug, **kw: (None, [])}):
            got = judged(t._teardown_project_context, SLUG, owner_user=None, config_dir=None, registry_dir=root / "named", home_base=root)
        home = root / f"{SLUG}-agent"
        check(got[1:] == (home / "Projects" / SLUG, root / "named" / f"{SLUG}.json", False) and built[-1] == dict(
            project=SLUG, project_name=SLUG, owner_user=f"{SLUG}-agent", board_root=home / f"{SLUG}-ticketboard-live",
            commit_git_dir=f"git:{SLUG}", asset_dir=home / ".claude" / f"{SLUG}-tickets-assets", frame_dir=home / ".claude" / f"{SLUG}-ticket-frames"),
              f"no entry: the default owner's conventional plan, not registered: {got[1:]} {built[-1]}")
        with patched(t, **{**seams, "_usable_switchyard_entry_for_project": lambda slug, **kw: (None, [])}):
            got = judged(t._teardown_project_context, SLUG, owner_user="named-owner", config_dir=None, registry_dir=None, home_base=root)
        check(got[2] == root / "registry" / f"{SLUG}.json" and built[-1]["owner_user"] == "named-owner", "a named owner and the host registry")

        provision = root / "home" / "Projects" / SLUG / ".switchyard" / "provision"
        provision.mkdir(parents=True)
        entry = SimpleNamespace(slug=SLUG, config_path=provision / f"{SLUG}.json")
        config = SimpleNamespace(repository=Path("/declared/repo"), run_as_user="cfg-owner", project_name="P367", board_url="http://127.0.0.1:8367/",
                                 ticket_prefix="PTR")
        parsed = SimpleNamespace(project=SLUG, from_json=True)
        loaded: list = []
        live = dict(seams, _usable_switchyard_entry_for_project=lambda slug, **kw: (entry, []),
                    load_project_config=lambda slug, path: loaded.append((slug, path)) or config, _repo_root=lambda: Path("/fixture/repo"),
                    _project_dir_from_generated_config_path=lambda path: Path("/structural/checkout"),
                    _project_board_provision_from_json=lambda path: loaded.append(("plan", path)) or parsed)
        with patched(t, **live):
            got = judged(t._teardown_project_context, SLUG, owner_user=None, config_dir=None, registry_dir=None, home_base=root)
        check(got[0].__dict__ == {"project": SLUG, "owner_user": "cfg-owner", "project_name": "P367", "port": 8367,
                                  "source_repo": Path("/fixture/repo"), "ticket_prefix": "PTR"} and got[1:] == (Path("/structural/checkout"), root / "registry" / f"{SLUG}.json", True),
              f"registered, no plan.json: rebuilt from the configuration; the checkout structural: {got}")
        with patched(t, **live):
            got = judged(t._teardown_project_context, SLUG, owner_user="named-owner", config_dir=None, registry_dir=None, home_base=root)
        check(got[0].owner_user == "named-owner", f"a named owner is preferred to the configured one: {got[0]}")
        (provision / "plan.json").write_text("{}")
        with patched(t, **live):
            got = judged(t._teardown_project_context, SLUG, owner_user="x", config_dir=None, registry_dir=None, home_base=root)
        check(got[0] is parsed and loaded[-1] == ("plan", provision / "plan.json"), "a plan.json beside the configuration is parsed, not rebuilt")
        with patched(t, **dict(live, _project_dir_from_generated_config_path=lambda path: None)):
            got = judged(t._teardown_project_context, SLUG, owner_user=None, config_dir=None, registry_dir=None, home_base=root)
        check(got[1] == Path("/declared/repo"), "no structural checkout: the configured repository")
        with patched(t, **dict(live, _project_dir_from_generated_config_path=lambda path: None)):
            config.repository = None
            got = judged(t._teardown_project_context, SLUG, owner_user=None, config_dir=None, registry_dir=None, home_base=root)
        check(got[1] == root / "cfg-owner" / "Projects" / SLUG, "neither: the owner's conventional checkout")


def test_the_board_count_sql() -> None:
    from scripts import project_teardown as m, team_launcher as t

    exists = ["psql", "-XAt", "postgresql:///postgres?host=/var/run/postgresql", "-c", "SELECT 1 FROM pg_database WHERE datname = 'it''s'"]
    count_sql = ("SELECT CASE WHEN to_regclass('ticket_board.tickets') IS NULL THEN 0 ELSE (SELECT count(*)::int FROM ticket_board.tickets) END")
    count = ["psql", "-XAt", "postgresql:///it's?host=/var/run/postgresql", "-c", count_sql]
    piped = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    fake_os = SimpleNamespace(geteuid=lambda: 1000, environ=os.environ)
    for answers, want, argvs in (
            ([done(0, "1\n"), done(0, " 7 \n")], 7, [exists, count]),
            ([done(0, "\n")], 0, [exists]),
            ([done(2)], None, [exists]),
            ([OSError("no psql")], None, [exists]),
            ([done(0, "1"), done(1)], None, [exists, count]),
            ([done(0, "1"), OSError("gone")], None, [exists, count]),
            ([done(0, "1"), done(0, "")], 0, [exists, count]),
            ([done(0, "1"), done(0, "many")], None, [exists, count])):
        calls: list = []
        with patched(m, os=fake_os):
            got = judged(t._ticket_board_existing_ticket_count, "it's", runner=recorder(list(answers), calls))
        check(got == want and [c[0] for c in calls] == argvs and all(c[1] == piped for c in calls), f"{answers}: {got}; {calls}")
    calls = []
    with patched(m, os=SimpleNamespace(geteuid=lambda: 0, environ=os.environ)):
        judged(t._ticket_board_existing_ticket_count, "it's", runner=recorder([done(0, "1"), done(0, "3")], calls))
    check([c[0] for c in calls] == [["sudo", "-u", "postgres", *exists], ["sudo", "-u", "postgres", *count]], f"as root: through postgres: {calls}")


def done(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- the owner --------------------------------------------------------------------------------------------------------


def test_who_may_never_be_removed() -> None:
    from scripts import project_teardown as m, team_launcher as t

    asked: list = []

    def refusal(owner, *, caller="", uid=1500, approval=None, sudo_user=None, current="operator-login"):
        env = dict(os.environ)
        env.pop("SUDO_USER", None)
        if sudo_user is not None:
            env["SUDO_USER"] = sudo_user
        with patched(t, **dict(LIVE, current_user_name=lambda: current,
                               read_host_desktop_approval=lambda: asked.append("approval") or (approval if approval is not None else {}))), \
                patched(m, os=SimpleNamespace(environ=env, geteuid=os.geteuid)):
            return judged(t.owner_removal_refusal, owner, caller=caller, uid_lookup=lambda name: asked.append(("uid", name)) or uid)

    check(refusal("") == "the teardown plan names no owner account, so there is nothing to remove"
          and refusal("   ") == "the teardown plan names no owner account, so there is nothing to remove", "no owner")
    for system in ("root", "nobody"):
        check(refusal(system) == f"{system} is a system account and is never removed by a tenant teardown", system)
    check(refusal("operator-login") == "operator-login is the account running this teardown; removing it would end the session running the removal",
          "the caller")
    check(refusal("typed-it", caller="typed-it") == "typed-it is the account running this teardown; removing it would end the session running the removal",
          "the named caller")
    check(refusal("typist", sudo_user=" typist ", current="root") ==
          "typist is the account that invoked this teardown; removing it would end the session running the removal", "whoever ran sudo")
    check(refusal("desk", approval={"gui_user": " desk "}) ==
          "desk is this host's approved desktop operator; a tenant teardown does not remove the person running the desktop", "the desktop operator")
    check(refusal("tenant", approval=None) == "" , "an ordinary tenant account")
    with patched(t, **dict(LIVE, current_user_name=lambda: "x", read_host_desktop_approval=lambda: (_ for _ in ()).throw(OSError("unreadable")))):
        check(judged(t.owner_removal_refusal, "tenant", uid_lookup=lambda name: 1500) == "", "an unreadable approval record protects nobody")
    check(refusal("svc", uid=999) == "svc has uid 999, below the 1000 an ordinary tenant account is given; this plan is aimed at a system account",
          "a system uid")
    check(refusal("fresh", uid=None) == "" and refusal("edge", uid=1000) == "", "an unknown uid, or exactly the minimum")
    asked.clear()
    refusal("tenant", approval={}, caller="c")
    check(asked == ["approval", ("uid", "tenant")], f"the approval is read when asked, then the uid: {asked}")


def test_the_owner_removal_steps_and_the_read_back() -> None:
    from scripts import team_launcher as t

    actions = judged(t._owner_removal_actions, "o'wner")
    q = "'o'\"'\"'wner'"
    check([a.label for a in actions] == ["disable linger for owner o'wner", "stop the systemd user manager and session for o'wner",
                                         "wait for o'wner's remaining processes to exit", "remove owner user account o'wner"]
          and all(a.command[:2] == ("bash", "-lc") and len(a.command) == 3 for a in actions), f"four bash steps, in order: {actions}")
    check(actions[0].command[2] == f"if id -u {q} >/dev/null 2>&1; then loginctl disable-linger {q} 2>/dev/null || true; "
                                   "else echo 'account already absent; nothing to unlinger'; fi", "linger, tolerating its absence")
    check(f'systemctl stop "user@${{owner_uid}}.service"' in actions[1].command[2] and f"loginctl terminate-user {q}" in actions[1].command[2]
          and actions[1].command[2].index("user@") < actions[1].command[2].index("terminate-user") < actions[1].command[2].index("user-runtime-dir@"),
          "the manager, then the session, then the runtime dir")
    check("for _ in $(seq 1 50); do" in actions[2].command[2] and "sleep 0.2" in actions[2].command[2] and "exit 1" in actions[2].command[2],
          "a bounded wait that fails loudly")
    check(actions[3].command[2] == f"if id -u {q} >/dev/null 2>&1; then userdel {q}; else echo 'account already absent'; fi", "then userdel")
    calls: list = []
    got = judged(t.owner_removal_residue, "o'wner", runner=recorder([done(0, "o'wner:x:1500:1500::/home/o:/bin/bash\nsecond\n"), done(0, " Yes \n"),
                                                                   done(0, "\n".join(f"{i} proc{i}" for i in range(8)) + "\n")], calls))
    check(got == ["the account still exists: o'wner:x:1500:1500::/home/o:/bin/bash", "linger is still enabled for o'wner",
                  "8 process(es) still running as o'wner: 0 proc0, 1 proc1, 2 proc2, 3 proc3, 4 proc4, 5 proc5 ..."],
          f"every residue, the first six processes and a count: {got}")
    quiet = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "text": True}
    check([c[0] for c in calls] == [["bash", "-lc", f"getent passwd {q} || true"], ["bash", "-lc", f"loginctl show-user {q} -p Linger --value 2>/dev/null || true"],
                                   ["bash", "-lc", f"ps -o pid=,comm= -u {q} 2>/dev/null || true"]] and all(c[1] == quiet for c in calls),
          f"read back with three quiet queries: {calls}")
    check(judged(t.owner_removal_residue, "gone", runner=recorder([done(0, ""), done(0, "no"), done(0, "2 a\n")], [])) ==
          ["1 process(es) still running as gone: 2 a"], "no account, no linger, one process: no ellipsis")
    check(judged(t.owner_removal_residue, "gone", runner=recorder([done(0), done(0), done(0)], [])) == [], "nothing left: empty")


# --- the plan ---------------------------------------------------------------------------------------------------------


def actions_for(**kw):
    from scripts import team_launcher as t

    with patched(t, **dict(LIVE, **RENDER)):
        return judged(t._switchyard_teardown_actions, PLAN, registry_path=Path("/fixture/registry/p367.json"), home_base=Path("/home"), **kw)


def test_every_teardown_action_in_order() -> None:
    from scripts import team_launcher as t

    base = actions_for(remove_owner_home=False, remove_owner_user=False)
    check([a.label for a in base] == [
        "stop and disable system board service p367-ticket-board.service",
        "stop and disable owner notify-listener service p367-notify.service",
        "remove owner notify-listener unit /home/syrd367-owner/.config/systemd/user/p367-notify.service",
        "remove system board unit /etc/systemd/system/p367-ticket-board.service",
        "remove tmpfiles config /etc/tmpfiles.d/p367.conf",
        "remove tenant control grant /etc/sudoers.d/47-p367-control",
        "remove tenant control data /usr/local/lib/switchyard/p367/control-grant.json",
        "remove tenant control helper /usr/local/libexec/switchyard-control-p367",
        "remove polkit rule /etc/polkit-1/rules.d/50-p367.rules",
        "reload systemd manager configuration",
        "drop PostgreSQL database p367_board",
        "remove board release root /srv/p367/live",
        "remove switchyard registry entry /fixture/registry/p367.json"], f"thirteen actions, in order: {[a.label for a in base]}")
    commands = [a.command for a in base]
    check(commands[2:9] == [("rm", "-f", "/home/syrd367-owner/.config/systemd/user/p367-notify.service"), ("rm", "-f", "/etc/systemd/system/p367-ticket-board.service"),
                           ("rm", "-f", "/etc/tmpfiles.d/p367.conf"), ("rm", "-f", "/etc/sudoers.d/47-p367-control"),
                           ("rm", "-f", "/usr/local/lib/switchyard/p367/control-grant.json"), ("rm", "-f", "/usr/local/libexec/switchyard-control-p367"),
                           ("rm", "-f", "/etc/polkit-1/rules.d/50-p367.rules")]
          and commands[9] == ("systemctl", "daemon-reload") and commands[11] == ("rm", "-rf", "--", "/srv/p367/live")
          and commands[12] == ("rm", "-f", "/fixture/registry/p367.json"), f"the exact removals: {commands}")
    check(commands[10] == ("sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "postgresql:///postgres?host=/var/run/postgresql", "-c",
                           'DROP DATABASE IF EXISTS "p367_board" WITH (FORCE);'), f"the database drop: {commands[10]}")
    check(commands[0] == ("bash", "-lc", "if systemctl list-unit-files --no-legend p367-ticket-board.service | grep -q .; then "
                                         "systemctl disable --now p367-ticket-board.service; fi")
          and "systemctl --user disable --now p367-notify.service || true" in commands[1][2] and "sudo -u syrd367-owner env" in commands[1][2],
          "the board service and the owner's listener, each only if present")
    full = actions_for(remove_owner_home=True, remove_owner_user=True)
    check([a.label for a in full[13:]] == ["remove owner home directory /home/syrd367-owner", "disable linger for owner syrd367-owner",
                                           "stop the systemd user manager and session for syrd367-owner", "wait for syrd367-owner's remaining processes to exit",
                                           "remove owner user account syrd367-owner"]
          and full[13].command == ("rm", "-rf", "--", "/home/syrd367-owner") and isinstance(full, tuple),
          "the home, then the four owner steps, only when asked")
    check(base[0].command_display == "bash -lc 'if systemctl list-unit-files --no-legend p367-ticket-board.service | grep -q .; then "
                                     "systemctl disable --now p367-ticket-board.service; fi'", "the display is shlex.join")
    check(isinstance(judged(setattr, base[0], "label", "x"), dataclasses.FrozenInstanceError), "an action is frozen")


def plan_of(**kw):
    from scripts import team_launcher as t

    fields = dict(project=SLUG, owner_user="o", owner_home=Path("/home/o"), project_checkout=Path("/home/o/Projects/p367"), ticket_count=0,
                  registered_healthy=False, registry_path=Path("/r/p367.json"),
                  actions=(t.SwitchyardTeardownAction("stop it", ("systemctl", "stop", "x y")),))
    fields.update(kw)
    return t.SwitchyardTeardownPlan(**fields)


def printout(plan, **kw):
    from scripts import team_launcher as t

    said: list = []
    args = dict(dry_run=False, drop_nonempty_board=False, destroy_registered_tenant=False, remove_owner_home=False, remove_owner_user=False)
    args.update(kw)
    t._print_teardown_plan(plan, print_func=said.append, **args)
    return said


def test_the_plan_printout_and_its_guards() -> None:
    said = printout(plan_of())
    check(said == ["switchyard: teardown plan for p367", "switchyard: owner user: o", "switchyard: owner home: /home/o",
                   "switchyard: project checkout: /home/o/Projects/p367", "switchyard: board ticket count: 0", "switchyard: registered and launchable: no",
                   "switchyard: preserved by default:", "  - owner user account o", "  - owner home directory /home/o", "  - project checkout /home/o/Projects/p367",
                   "switchyard: actions:", "  1. stop it", "     command: systemctl stop 'x y'"], f"the whole plan: {said}")
    said = printout(plan_of(registered_healthy=True, ticket_count=None), dry_run=True, remove_owner_home=True, remove_owner_user=True)
    check("switchyard: live-tenant guard: destructive run requires --destroy-registered-tenant" in said
          and "switchyard: board-count guard: destructive run requires --drop-nonempty-board" in said and said[-1] == "switchyard: dry-run only; no changes made"
          and "  - owner user account o" not in said and "  - owner home directory /home/o" not in said and "switchyard: board ticket count: unknown" in said,
          f"both guards, nothing preserved, dry run: {said}")
    said = printout(plan_of(ticket_count=4))
    check("switchyard: non-empty board guard: destructive run requires --drop-nonempty-board" in said, "a non-empty board")
    said = printout(plan_of(ticket_count=4, registered_healthy=True), drop_nonempty_board=True, destroy_registered_tenant=True)
    check(not any("guard" in line for line in said), "every guard answered: none shown")


def test_confirmation_and_a_failing_action() -> None:
    from scripts import team_launcher as t

    with patched(t, **dict(LIVE, _read_prompt=refuse("the prompt"))):
        check(judged(t._confirm_teardown_project, " p367 ", confirmation="p367", input_func=refuse("input")) is None, "a supplied confirmation")
        bad = judged(t._confirm_teardown_project, "p367", confirmation="yes", input_func=refuse("input"))
    check(isinstance(bad, SystemExit) and str(bad) == "switchyard: teardown confirmation failed; expected 'p367'", f"a wrong confirmation: {bad!r}")
    typed: list = []
    with patched(t, **dict(LIVE, _read_prompt=lambda prompt, *, input_func: typed.append(prompt) or " p367 ")):
        check(judged(t._confirm_teardown_project, "p367", confirmation=None, input_func=refuse("input")) is None
              and typed == ["Type p367 to tear down this project: "], f"asked, stripped: {typed}")
    actions = [t.SwitchyardTeardownAction(f"step {i}", ("true", str(i))) for i in range(4)]
    calls: list = []
    said: list = []
    failed = judged(t._run_teardown_actions, actions, runner=recorder([done(0), done(0), done(3, "out", " the reason \n")], calls), print_func=said.append)
    check(isinstance(failed, SystemExit) and str(failed) == "\n".join([
        "switchyard: teardown failed while trying to step 2: the reason", "switchyard: completed before failure:", "  - step 0", "  - step 1",
        "switchyard: remaining after failure:", "  - step 2", "  - step 3"]), f"what completed and what remains: {failed}")
    check(said == ["switchyard: running: step 0", "switchyard: running: step 1", "switchyard: running: step 2"]
          and [c[0] for c in calls] == [["true", "0"], ["true", "1"], ["true", "2"]]
          and calls[0][1] == {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}, f"run in order as lists, and stopped: {calls}")
    failed = judged(t._run_teardown_actions, actions[:1], runner=recorder([done(9)], []), print_func=lambda line: None)
    check(str(failed) == "switchyard: teardown failed while trying to step 0: exit status 9\nswitchyard: completed before failure:\n  - (none)\n"
                         "switchyard: remaining after failure:\n  - step 0", f"nothing completed, no output: {failed}")
    failed = judged(t._run_teardown_actions, actions[:1], runner=recorder([done(9, "only stdout\n")], []), print_func=lambda line: None)
    check("step 0: only stdout" in str(failed), "stdout when there is no stderr")


# --- the command -------------------------------------------------------------------------------------------------------


class Teardown:
    def __init__(self) -> None:
        self.log, self.said = [], []
        self.count, self.healthy, self.refusal, self.residue = 0, False, "", []

    def seams(self, **extra: object) -> dict:
        from scripts import team_launcher as t

        log = self.log
        names = dict(LIVE, **RENDER)
        names.update(
            _teardown_project_context=lambda project, **kw: log.append(("context", project, kw)) or (PLAN, Path("/checkout"), Path("/r/p367.json"), self.healthy),
            _ticket_board_existing_ticket_count=lambda database, *, runner: log.append(("count", database)) or self.count,
            owner_removal_refusal=lambda owner: log.append(("refusal", owner)) or self.refusal,
            _confirm_teardown_project=lambda project, *, confirmation, input_func: log.append(("confirm", project, confirmation)),
            _run_teardown_actions=lambda actions, *, runner, print_func: log.append(("run", len(actions))),
            owner_removal_residue=lambda owner, *, runner: log.append(("residue", owner)) or list(self.residue),
        )
        names.update(extra)
        return names

    def run(self, **kw):
        from scripts import team_launcher as t

        with patched(t, **self.seams()):
            return judged(t.switchyard_teardown_command, SLUG, print_func=self.said.append, runner=refuse("the runner itself"), **kw)

    def kinds(self):
        return [e[0] for e in self.log]


def test_the_command_guards_dry_run_confirmation_and_residue() -> None:
    fx = Teardown()
    check(fx.run(dry_run=True, remove_owner_user=True, owner_user="named") == 0 and fx.kinds() == ["context", "count", "refusal"]
          and fx.log[0][2] == {"owner_user": "named", "config_dir": None, "registry_dir": None, "home_base": Path("/home")}
          and fx.said[-1] == "switchyard: dry-run only; no changes made", f"a dry run renders and asks, and runs nothing: {fx.log}")
    fx = Teardown(); fx.refusal = "o is the account running this teardown"
    got = fx.run(dry_run=True, remove_owner_user=True)
    check(got == 0 and "switchyard: owner removal refused: o is the account running this teardown" in fx.said, "a dry run shows the refusal")
    fx = Teardown(); fx.refusal = "nope"
    got = fx.run(remove_owner_user=True, destroy_registered_tenant=True, drop_nonempty_board=True)
    check(isinstance(got, SystemExit) and str(got) == "switchyard: refusing to remove the owner account: nope" and "run" not in fx.kinds(),
          f"a real run refuses first: {got!r}")
    fx = Teardown(); fx.healthy = True
    got = fx.run()
    check(isinstance(got, SystemExit) and str(got) == ("switchyard: refusing to tear down registered launchable tenant 'p367'; pass --destroy-registered-tenant "
                                                       "to confirm removing its board unit, database, release root, and registry entry")
          and "refusal" not in fx.kinds(), f"a live tenant needs its own flag, and the owner is not asked when not removed: {got!r}")
    fx = Teardown(); fx.count = None
    got = fx.run()
    check(isinstance(got, SystemExit) and str(got) == ("switchyard: cannot determine whether board database p367_board contains tickets; "
                                                       "pass --drop-nonempty-board to confirm dropping it anyway"), f"an unknown count: {got!r}")
    fx = Teardown(); fx.count = 3
    got = fx.run()
    check(isinstance(got, SystemExit) and str(got) == ("switchyard: refusing to drop non-empty board database p367_board with 3 ticket(s); "
                                                       "pass --drop-nonempty-board to confirm that data loss"), f"a non-empty board: {got!r}")
    fx = Teardown(); fx.count = 3
    check(fx.run(drop_nonempty_board=True, confirm="p367") == 0 and fx.kinds()[-2:] == ["confirm", "run"] and fx.log[-2] == ("confirm", SLUG, "p367")
          and fx.log[-1] == ("run", 13) and fx.said[-1] == "switchyard: teardown complete for p367",
          f"confirmed, then the thirteen actions run, and done: {fx.log}")
    fx = Teardown(); fx.residue = ["the account still exists: o:x:1500"]
    got = fx.run(remove_owner_user=True, remove_owner_home=True, confirm="p367")
    check(isinstance(got, SystemExit) and str(got) == ("switchyard: teardown did NOT remove syrd367-owner; p367 is not safe to recreate under the same slug.\n"
                                                       "  - the account still exists: o:x:1500") and fx.log[-2] == ("run", 18) and fx.kinds()[-1] == "residue",
          f"the account is read back after the actions: {got!r}")
    fx = Teardown()
    fx.run(dry_run=True, home_base=Path("/fixture/homes"))
    check(fx.log[0][2]["home_base"] == Path("/fixture/homes") and "switchyard: owner home: /fixture/homes/syrd367-owner" in fx.said
          and any("/fixture/homes/syrd367-owner/.config/systemd/user/p367-notify.service" in line for line in fx.said),
          f"another home base is the one planned for: {fx.said[:4]}")
    fx = Teardown()
    check(fx.run(remove_owner_user=True, confirm="p367") == 0 and fx.said[-2:] == ["switchyard: owner account syrd367-owner removed; no residue",
                                                                                    "switchyard: teardown complete for p367"], f"no residue: {fx.said[-2:]}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_only_the_host_accounts_leaf_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_classes_the_constants_and_the_leaf")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"project_teardown_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
