#!/usr/bin/env python3
"""SYRD-448: the interrupted role-state restore, against the launcher it came out of.

`restore_interrupted_role_state` -- giving a role its state back after an
interrupted repatriation, and finishing the provider-state records the broken
store could not write -- moved unchanged into `scripts/role_state_restore.py`;
the launcher re-exports it. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher; its defaults --
  `subprocess.run` and the builtin `print` -- are the same objects.
- **Seams (rule 24):** everything it reads when it runs -- the owner's home,
  the current user, the interrupted-role scan, the ownership repair, the
  finisher and a role's CLI name -- is read through the launcher as often as
  before, so a patch there reaches it: every case below stands them in there.
- **Reader:** `upgrade_phases.py` reads it through the launcher when it runs.
- **The behaviour is the baseline's:** nothing or several roles captured; the
  repair or the finisher refusing; a dry run, per role; the owner's home as
  given, the owner's, the caller's or the process's; every option passed on; a
  failing scan. `GOLDEN` below was produced by running the BASELINE launcher's
  own definition over the very cases embedded here (`gold448.py`), not typed;
  it is byte-identical under `env -i`, in a normal role pane, with another
  HOME, USER and COLUMNS, under umask 077 and under several hash seeds.

Nothing touches real role state or an account home: every such step is a
stand-in, and `$HOME` points at a test-owned directory for the `Path.home()`
fallback. Spawns, every exec, signals, account and group lookups and socket
connections are refused for each case.
"""

from __future__ import annotations

import ast
import grp
import json
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import role_state_restore as m  # noqa: E402

CHECKS = 0
MOVED = ('restore_interrupted_role_state',)
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'restore_interrupted_role_state': {'_finish_interrupted_provider_state': 1, '_interrupted_provider_state_roles': 1, '_role_cli_name': 1, 'current_user_name': 1, 'home_dir_for_user': 1, 'repair_role_state_ownership': 1},
}
#: Measured on the baseline launcher: every launcher definition outside it that names it, and how often (none).
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/upgrade_phases.py': {'launcher.restore_interrupted_role_state': 1}}
#: The BASELINE's own behaviour for the cases below (`gold448.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'nothing captured, the repair succeeds': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', []], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'nothing captured, the repair refuses': {'result': False, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership refused']},
    'roles captured, finished': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', [['NS main', 41, 3], ['NS app', 42, 0]]], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'roles captured, the finisher refuses': {'result': False, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', [['NS main', 41, 3]]], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'roles captured, the repair refuses': {'result': False, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership refused']},
    'a dry run with roles captured': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['_role_cli_name', ['NS main'], {}], ['_role_cli_name', ['NS app'], {}], ['repair_role_state_ownership', ['NS config'], {'dry_run': True, 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['switchyard: would finish the provider-state record main could not write, so its live codex (pid 41) would not be restarted; nothing written', 'switchyard: would finish the provider-state record app could not write, so its live  (pid 42) would not be restarted; nothing written', 'syrd448 ownership repaired']},
    'a dry run, nothing captured': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': True, 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'a dry run, the repair refuses': {'result': False, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['_role_cli_name', ['NS main'], {}], ['repair_role_state_ownership', ['NS config'], {'dry_run': True, 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['switchyard: would finish the provider-state record main could not write, so its live codex (pid 41) would not be restarted; nothing written', 'syrd448 ownership refused']},
    "the project's own owner": {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', []], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'the caller when the project names none': {'result': True, 'type': 'bool', 'calls': [['current_user_name', [], {}], ['home_dir_for_user', ['p448-caller'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', []], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'no home for the owner: the process home': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/process-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', []], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/process-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'an explicit owner home': {'result': True, 'type': 'bool', 'calls': [['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/explicit-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', [['NS main', 41, 3]]], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/explicit-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'every option passed on': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'STAND-IN RUNNER', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN PRINT'}], ['_finish_interrupted_provider_state', ['NS config', [['NS main', 41, 3]]], {'runner': 'STAND-IN RUNNER', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN PRINT'}]], 'printed': []},
    'the default printer': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['_role_cli_name', ['NS main'], {}], ['repair_role_state_ownership', ['NS config'], {'dry_run': True, 'print_func': 'the builtin print'}]], 'printed': ['switchyard: would finish the provider-state record main could not write, so its live codex (pid 41) would not be restarted; nothing written', 'syrd448 ownership repaired', '']},
    'the scan fails': {'result': {'raised': 'OSError', 'message': '[Errno 5] syrd448 the provider-state store cannot be read'}, 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}]], 'printed': []},
    'the finisher answers something else': {'result': 'partial', 'type': 'str', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['repair_role_state_ownership', ['NS config'], {'dry_run': False, 'print_func': 'STAND-IN <lambda>'}], ['_finish_interrupted_provider_state', ['NS config', []], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home', 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['syrd448 ownership repaired']},
    'the CLI name rebound on the launcher': {'result': True, 'type': 'bool', 'calls': [['home_dir_for_user', ['p448-owner'], {}], ['_interrupted_provider_state_roles', ['NS config'], {'runner': 'the real subprocess.run', 'owner_home': 'PATH TMP/owner-home'}], ['_role_cli_name', ['NS main'], {}], ['repair_role_state_ownership', ['NS config'], {'dry_run': True, 'print_func': 'STAND-IN <lambda>'}]], 'printed': ['switchyard: would finish the provider-state record main could not write, so its live syrd448-cli (pid 41) would not be restarted; nothing written', 'syrd448 ownership repaired']},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold448.py` (which ran them on the baseline) ------------------------------------
# A case restores a synthetic project's interrupted role state. Every step that would touch real role state or an
# account home is a stand-in on the launcher: the owner-home lookup, the current user, the interrupted-role scan, the
# ownership repair and the finisher. A role's CLI name is the launcher's own, recorded there and passed through. `$HOME`
# points at a test-owned directory for the `Path.home()` fallback, so no real home is read. Recorded, in order: every
# call through the launcher with its arguments, everything printed, and the answer, or the exact exception.
ROLE_A = {"role": "main", "cli": ["/usr/bin/codex", "--x"]}
ROLE_B = {"role": "app", "cli": []}
CASES = {
    "nothing captured, the repair succeeds": {"captured": [], "repair": True, "finish": True},
    "nothing captured, the repair refuses": {"captured": [], "repair": False, "finish": True},
    "roles captured, finished": {"captured": [[ROLE_A, 41, 3], [ROLE_B, 42, 0]], "repair": True, "finish": True},
    "roles captured, the finisher refuses": {"captured": [[ROLE_A, 41, 3]], "repair": True, "finish": False},
    "roles captured, the repair refuses": {"captured": [[ROLE_A, 41, 3]], "repair": False, "finish": True},
    "a dry run with roles captured": {"captured": [[ROLE_A, 41, 3], [ROLE_B, 42, 0]], "repair": True, "finish": True, "kw": {"dry_run": True}},
    "a dry run, nothing captured": {"captured": [], "repair": True, "finish": True, "kw": {"dry_run": True}},
    "a dry run, the repair refuses": {"captured": [[ROLE_A, 41, 3]], "repair": False, "finish": True, "kw": {"dry_run": True}},
    "the project's own owner": {"run_as_user": "p448-owner", "captured": [], "repair": True, "finish": True},
    "the caller when the project names none": {"run_as_user": "", "captured": [], "repair": True, "finish": True},
    "no home for the owner: the process home": {"run_as_user": "p448-owner", "home": None, "captured": [], "repair": True, "finish": True},
    "an explicit owner home": {"captured": [[ROLE_A, 41, 3]], "repair": True, "finish": True, "kw": {"owner_home": "EXPLICIT"}},
    "every option passed on": {"captured": [[ROLE_A, 41, 3]], "repair": True, "finish": True, "kw": {"runner": "RUNNER", "print_func": "PRINT"}},
    "the default printer": {"captured": [[ROLE_A, 41, 3]], "repair": True, "finish": True, "kw": {"dry_run": True}, "default_print": True},
    "the scan fails": {"captured": "raise", "repair": True, "finish": True},
    "the finisher answers something else": {"captured": [], "repair": True, "finish": "partial"},
    "the CLI name rebound on the launcher": {"captured": [[ROLE_A, 41, 3]], "repair": True, "finish": True, "kw": {"dry_run": True}, "launcher": {"_role_cli_name": "CLI"}},
}
FUNCTIONS = ("restore_interrupted_role_state",)


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; every role-state and account step a stand-in on `t`, the launcher."""
    import contextlib, io, os, shutil, subprocess, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    printed: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd448-")).resolve()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(str(tmp), "TMP")
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"NS {getattr(value, 'label', getattr(value, 'role', '?'))}"
        if callable(value) and "run_case.<locals>" in getattr(value, "__qualname__", ""):
            return f"STAND-IN {value.__name__}"
        if getattr(value, "__module__", "") == "subprocess" and getattr(value, "__name__", "") == "run":
            return "the real subprocess.run"
        if value is print:
            return "the builtin print"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    names = [*FUNCTIONS, "home_dir_for_user", "current_user_name", "_interrupted_provider_state_roles", "repair_role_state_ownership",
             "_finish_interrupted_provider_state", "_role_cli_name"]
    saved = {n: getattr(t, n) for n in names}
    saved_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(tmp / "process-home")
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        captured = [(SimpleNamespace(role=r["role"], cli=list(r["cli"])), pid, gen) for r, pid, gen in spec["captured"]] if spec["captured"] != "raise" else None

        def scan(config, **kwargs):
            note("_interrupted_provider_state_roles", config, **kwargs)
            if captured is None:
                raise OSError(5, "syrd448 the provider-state store cannot be read")
            return list(captured)

        def repair(config, **kwargs):
            note("repair_role_state_ownership", config, **kwargs)
            kwargs["print_func"]("syrd448 ownership repaired" if spec["repair"] else "syrd448 ownership refused")
            return spec["repair"]

        def finish(config, got, **kwargs):
            note("_finish_interrupted_provider_state", config, got, **kwargs)
            return spec["finish"]

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            f.__name__ = name
            return f

        def RUNNER(*a, **k): return None
        def PRINT(*a, **k): return None
        home = spec.get("home", "OWNER")
        t.home_dir_for_user = lambda user: note("home_dir_for_user", user) or (tmp / "owner-home" if home == "OWNER" else None)
        t.current_user_name = lambda: note("current_user_name") or "p448-caller"
        t._interrupted_provider_state_roles = scan
        t.repair_role_state_ownership = repair
        t._finish_interrupted_provider_state = finish
        t._role_cli_name = passthrough("_role_cli_name")
        stand = {"CLI": lambda role: note("_role_cli_name", role) or "syrd448-cli"}
        for k, v in spec.get("launcher", {}).items():
            setattr(t, k, stand[v])
        config = SimpleNamespace(label="config", run_as_user=spec.get("run_as_user", "p448-owner"))
        kw = {k: {"RUNNER": RUNNER, "PRINT": PRINT, "EXPLICIT": tmp / "explicit-home"}.get(v, v) for k, v in spec.get("kw", {}).items()}
        if not spec.get("default_print") and "print_func" not in kw:
            kw["print_func"] = lambda s: printed.append(norm(s))
        try:
            if spec.get("default_print"):
                shown = io.StringIO()
                with contextlib.redirect_stdout(shown):
                    got = under["restore_interrupted_role_state"](config, **kw)
                printed.extend(norm(shown.getvalue()).split("\n"))
            else:
                got = under["restore_interrupted_role_state"](config, **kw)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": calls, "printed": printed}
        return {"result": norm(got), "type": type(got).__name__, "calls": calls, "printed": printed}
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        for n, v in saved.items():
            setattr(t, n, v)
        shutil.rmtree(tmp)
# ----------------------------------------------------------------------------------------------------------------------


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


EXECS = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")


class contained:
    """Spawns, every exec, signals, connections and real account/group lookups refused."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill"), system=refuse("os.system"), **{name: refuse(f"os.{name}") for name in EXECS}),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(grp, getgrgid=refuse("grp.getgrgid"), getgrnam=refuse("grp.getgrnam")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(holder: object, spec: dict) -> dict:
    with contained():
        return json.loads(json.dumps(run_case(t, holder, spec, REACHED)))


def test_the_guard_itself_refuses_a_spawn_an_exec_a_lookup_and_a_connection() -> None:
    attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0), lambda: os.system("true"),
                lambda: pwd.getpwuid(0), lambda: grp.getgrgid(0), lambda: socket.socket().connect(("127.0.0.1", 9)),
                *(lambda name=name: getattr(os, name)("true", ["true"]) for name in EXECS)]
    for attempt in attempts:
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, every exec, a signal, an account or group lookup and a connection")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.role_state_restore as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.role_state_restore", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.role_state_restore")):
        result = python("import builtins, importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.role_state_restore as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "[(k, v.default is subprocess.run if k == 'runner' else v.default is builtins.print if k == 'print_func' else v.default) "
                        "for k, v in inspect.signature(m.restore_interrupted_role_state).parameters.items() if v.default is not inspect.Parameter.empty], "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'ProjectConfig', 'home_dir_for_user', 'repair_role_state_ownership', "
                        "'_interrupted_provider_state_roles', '_finish_interrupted_provider_state')))")
        check(result.stdout.strip() == "True [('dry_run', False), ('runner', True), ('print_func', True), ('owner_home', None)] True",
              f"{' then '.join(order)}: one object; the defaults the same objects; nothing of the launcher or its imports bound at load: {result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.subprocess is subprocess and m.Path is Path and m.Any is typing.Any and m.Callable is typing.Callable,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "role_state_restore.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, siblings included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == (["from scripts import team_launcher as launcher"] if expected else [])
              and (not expected or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and nothing else imported: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from pathlib import Path", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, and the config type for annotations only: {top} {tc}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the one function, and nothing else: {names}")


def test_the_launcher_reexports_it_and_its_reader_reaches_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.role_state_restore"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the function, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    # Annotated constants count too: the install table is `AGENT_CLI_INSTALL_COMMANDS: dict[str, str] = {...}`.
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"ProjectConfig", "repatriate_role_runtime_state", "_role_cli_name"} <= defined | exported,
          "the launcher defines none of them, and keeps its caller and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition calls it, as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.Attribute) and x.attr in MOVED and isinstance(x.value, ast.Name) and x.value.id in ("launcher", "team_launcher"):
                got[ast.unparse(x)] = got.get(ast.unparse(x), 0) + 1
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads them through the launcher, as often as before: {got} {bare}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def steps(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    full = ["home_dir_for_user", "_interrupted_provider_state_roles", "repair_role_state_ownership", "_finish_interrupted_provider_state"]
    check(steps("roles captured, finished") == full and result("roles captured, finished") is True
          and GOLDEN["roles captured, finished"]["calls"][1][2] == {"runner": "the real subprocess.run", "owner_home": "PATH TMP/owner-home"}
          and GOLDEN["roles captured, finished"]["calls"][3][1][1] == [["NS main", 41, 3], ["NS app", 42, 0]],
          "the owner's home, the interrupted roles, the ownership repair, then those roles finished, in that order")
    check(result("nothing captured, the repair refuses") is False and "_finish_interrupted_provider_state" not in steps("nothing captured, the repair refuses")
          and result("roles captured, the repair refuses") is False and "_finish_interrupted_provider_state" not in steps("roles captured, the repair refuses"),
          "a refused ownership repair answers False, and nothing is finished")
    check(result("roles captured, the finisher refuses") is False and result("the finisher answers something else") == "partial",
          "otherwise the answer is the finisher's, as it is")
    dry = GOLDEN["a dry run with roles captured"]
    check(result("a dry run with roles captured") is True and "_finish_interrupted_provider_state" not in steps("a dry run with roles captured")
          and dry["printed"][:2] == ["switchyard: would finish the provider-state record main could not write, so its live codex (pid 41) would not be restarted; nothing written",
                                    "switchyard: would finish the provider-state record app could not write, so its live  (pid 42) would not be restarted; nothing written"]
          and result("a dry run, the repair refuses") is False and GOLDEN["a dry run, nothing captured"]["printed"] == ["syrd448 ownership repaired"],
          "a dry run says what it would finish, per role, then repairs in dry-run mode and answers the repair's result without finishing")
    check(GOLDEN["the caller when the project names none"]["calls"][:2] == [["current_user_name", [], {}], ["home_dir_for_user", ["p448-caller"], {}]]
          and GOLDEN["no home for the owner: the process home"]["calls"][1][2]["owner_home"] == "PATH TMP/process-home"
          and "home_dir_for_user" not in steps("an explicit owner home")
          and GOLDEN["an explicit owner home"]["calls"][0][2]["owner_home"] == "PATH TMP/explicit-home",
          "the owner's home: as given, else the project owner's (the caller's when it names none), else the process's own home")
    check(result("the scan fails") == {"raised": "OSError", "message": "[Errno 5] syrd448 the provider-state store cannot be read"}
          and steps("the scan fails") == ["home_dir_for_user", "_interrupted_provider_state_roles"],
          "a scan that fails raises, before anything is repaired")
    check(GOLDEN["every option passed on"]["calls"][1][2]["runner"] == "STAND-IN RUNNER"
          and GOLDEN["every option passed on"]["calls"][2][2]["print_func"] == "STAND-IN PRINT"
          and GOLDEN["the default printer"]["printed"][0].startswith("switchyard: would finish the provider-state record main")
          and "syrd448-cli" in GOLDEN["the CLI name rebound on the launcher"]["printed"][0],
          "the runner and printer are passed on; the default printer is print; the role's CLI name is the launcher's when it runs")


def test_every_launcher_seam_is_reached() -> None:
    # Every name the function reads on the launcher is a recorder or stand-in there.
    names = {name for reads in SEAMS.values() for name in reads}
    check(names <= REACHED, f"a recorder on the launcher reached every function: missing {sorted(names - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it_and_its_reader_reaches_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"role_state_restore_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
