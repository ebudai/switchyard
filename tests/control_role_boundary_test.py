#!/usr/bin/env python3
"""SYRD-440: the control-role resolution, against the launcher it came out of.

`CONTROL_ROLE_CAPABILITIES`, `control_role_name` and `director_role_name` --
which configured role controls a tenant, and why not when none or several can
-- moved unchanged into `scripts/control_role.py`; the launcher re-exports all
three. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; the
  defaults are the same and the capabilities the same frozenset.
- **Seams (rule 24):** everything they read when they run -- each other, the
  capabilities and the launcher's JSON reader -- is read through the launcher
  as often as before, so a patch there reaches it: every case below records
  the reader and the resolver there, and rebinds the capabilities and the
  resolver there to see the effect.
- **Readers:** `scripts/director_upgrade.py` and
  `scripts/role_account_migration.py` still read `control_role_name` through
  the launcher when they run; no launcher definition calls the three.
- **The behaviour is the baseline's:** one controller, the director or another
  role; none (one capability, inactive, unconfigured, unnamed, null or string
  capabilities); several; no usable workflow document, falling back to the
  historical director; a config that is not JSON, missing or unreadable;
  and the director's name alone. `GOLDEN` below was produced by running the
  BASELINE launcher's own definitions over the very cases embedded here
  (`gold440.py`), not typed; it is byte-identical under `env -i`, in a normal
  role pane, with another HOME, USER and COLUMNS, and under umask 077.

Nothing reaches a board or the host: each workflow document is a JSON file in
a test-owned tree. Spawns, every exec, signals, account and group lookups and
socket connections are refused for each case.
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
from scripts import control_role as m  # noqa: E402

CHECKS = 0
MOVED = ('CONTROL_ROLE_CAPABILITIES', 'control_role_name', 'director_role_name')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'control_role_name': {'CONTROL_ROLE_CAPABILITIES': 2, '_load_json': 1},
    'director_role_name': {'control_role_name': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the three that names them, and how often (none).
DISPATCH = {}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/director_upgrade.py': ['launcher.control_role_name'], 'scripts/role_account_migration.py': ['launcher.control_role_name']}
#: The BASELINE's own behaviour for the cases below (`gold440.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'control: no workflow file, a director configured': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': []},
    'control: no workflow file, no director configured': {'result': ['', 'this project configures no director role'], 'type': 'tuple', 'json': '["", "this project configures no director role"]', 'calls': []},
    'control: no workflow file, no roles': {'result': ['', 'this project configures no director role'], 'type': 'tuple', 'json': '["", "this project configures no director role"]', 'calls': []},
    'control: one controller': {'result': ['boss', ''], 'type': 'tuple', 'json': '["boss", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the controller is the director': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a controller with more capabilities': {'result': ['main', ''], 'type': 'tuple', 'json': '["main", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: only one of the two capabilities': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: no role has them': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: several controllers': {'result': ['', "this project's workflow gives the control capabilities to more than one role: director, main"], 'type': 'tuple', 'json': '["", "this project\'s workflow gives the control capabilities to more than one role: director, main"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: several declared, one configured': {'result': ['audit', ''], 'type': 'tuple', 'json': '["audit", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the controller is not configured': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the controller is inactive': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the controller is active by a truthy value': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the controller is inactive by zero': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the controller named twice': {'result': ['', "this project's workflow gives the control capabilities to more than one role: main, main"], 'type': 'tuple', 'json': '["", "this project\'s workflow gives the control capabilities to more than one role: main, main"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a role entry that is not an object': {'result': ['main', ''], 'type': 'tuple', 'json': '["main", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a controller with no name': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a controller with no name, an unnamed role configured': {'result': ['', ''], 'type': 'tuple', 'json': '["", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: capabilities that are null': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: capabilities given as a string': {'result': ['', "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a workflow that is null': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: no workflow key': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a workflow with no roles': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a workflow whose roles are null': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a workflow that is a list': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a config that is not an object': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a config that is not JSON': {'result': {'raised': 'JSONDecodeError', 'message': 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}, 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a config that is empty': {'result': {'raised': 'JSONDecodeError', 'message': 'Expecting value: line 1 column 1 (char 0)'}, 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: a config file that is missing': {'result': {'raised': 'FileNotFoundError', 'message': "[Errno 2] No such file or directory: 'TMP/p440.json'"}, 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the reader refuses': {'result': ['director', ''], 'type': 'tuple', 'json': '["director", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the reader answers null': {'result': ['', 'this project configures no director role'], 'type': 'tuple', 'json': '["", "this project configures no director role"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the reader fails otherwise': {'result': {'raised': 'PermissionError', 'message': "[Errno 13] Permission denied: 'TMP/p440.json'"}, 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the capabilities rebound on the launcher': {'result': ['main', ''], 'type': 'tuple', 'json': '["main", ""]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'control: the capabilities rebound, none match': {'result': ['', "this project's workflow declares no active role with the control capabilities (alpha, claim, zeta)"], 'type': 'tuple', 'json': '["", "this project\'s workflow declares no active role with the control capabilities (alpha, claim, zeta)"]', 'calls': [['_load_json', ['PATH TMP/p440.json'], {}]]},
    'director: a controller': {'result': 'main', 'type': 'str', 'json': '"main"', 'calls': [['control_role_name', ["CONFIG ['audit', 'director', 'main']"], {'config_path': 'PATH TMP/p440.json'}], ['_load_json', ['PATH TMP/p440.json'], {}]]},
    'director: none, with a reason': {'result': '', 'type': 'str', 'json': '""', 'calls': [['control_role_name', ["CONFIG ['main']"], {'config_path': None}]]},
    'director: no workflow file, a director configured': {'result': 'director', 'type': 'str', 'json': '"director"', 'calls': [['control_role_name', ["CONFIG ['audit', 'director', 'main']"], {'config_path': None}]]},
    'director: the resolver rebound on the launcher': {'result': 'syrd440-rebound', 'type': 'str', 'json': '"syrd440-rebound"', 'calls': [['control_role_name', ["CONFIG ['audit', 'director', 'main']"], {'config_path': None}]]},
    'director: the resolver refuses': {'result': {'raised': 'SystemExit', 'message': 'syrd440 the resolver refuses'}, 'calls': [['control_role_name', ["CONFIG ['audit', 'director', 'main']"], {'config_path': 'PATH TMP/p440.json'}]]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold440.py` (which ran them on the baseline) ------------------------------------
# A case asks which configured role controls a synthetic tenant. The tenant's config is a stand-in naming its roles; its
# workflow document, when the case has one, is a JSON file in a test-owned tree, read by the launcher's own JSON reader
# (recorded on the launcher, passed through) -- or the reader is stood in there to answer or refuse. No board is asked.
# Recorded, in order: every call through the launcher with its arguments, and the answer as a value, as the exact JSON
# text and by container type, or the exact exception.
CAPS = ["merge", "set_manually_controlled"]
ROLES = ["director", "main", "audit"]
CONTROL = {
    "no workflow file, a director configured": {"roles": ROLES},
    "no workflow file, no director configured": {"roles": ["main", "audit"]},
    "no workflow file, no roles": {"roles": []},
    "one controller": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "boss", "capabilities": CAPS}, {"name": "main"}]}}, "extra": ["boss"]},
    "the controller is the director": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": CAPS}, {"name": "main", "capabilities": ["merge"]}]}}},
    "a controller with more capabilities": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "main", "capabilities": ["claim", *CAPS]}]}}},
    "only one of the two capabilities": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": ["merge"]}]}}},
    "no role has them": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director"}, {"name": "main", "capabilities": []}]}}},
    "several controllers": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "main", "capabilities": CAPS}, {"name": "director", "capabilities": CAPS}]}}},
    "several declared, one configured": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "ghost", "capabilities": CAPS}, {"name": "audit", "capabilities": CAPS}]}}},
    "the controller is not configured": {"roles": ["main"], "doc": {"workflow": {"roles": [{"name": "director", "capabilities": CAPS}]}}},
    "the controller is inactive": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": CAPS, "active": False}]}}},
    "the controller is active by a truthy value": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": CAPS, "active": 1}]}}},
    "the controller is inactive by zero": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": CAPS, "active": 0}]}}},
    "the controller named twice": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "main", "capabilities": CAPS}, {"name": "main", "capabilities": CAPS}]}}},
    "a role entry that is not an object": {"roles": ROLES, "doc": {"workflow": {"roles": ["director", {"name": "main", "capabilities": CAPS}]}}},
    "a controller with no name": {"roles": ROLES, "doc": {"workflow": {"roles": [{"capabilities": CAPS}]}}},
    "a controller with no name, an unnamed role configured": {"roles": ["", "main"], "doc": {"workflow": {"roles": [{"name": None, "capabilities": CAPS}]}}},
    "capabilities that are null": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": None}]}}},
    "capabilities given as a string": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "director", "capabilities": "merge set_manually_controlled"}]}}},
    "a workflow that is null": {"roles": ROLES, "doc": {"workflow": None}},
    "no workflow key": {"roles": ROLES, "doc": {"project": "p440"}},
    "a workflow with no roles": {"roles": ROLES, "doc": {"workflow": {"roles": []}}},
    "a workflow whose roles are null": {"roles": ROLES, "doc": {"workflow": {"stages": ["backlog"], "roles": None}}},
    "a workflow that is a list": {"roles": ROLES, "doc": {"workflow": [{"name": "main", "capabilities": CAPS}]}},
    "a config that is not an object": {"roles": ROLES, "raw": "[1, 2]"},
    "a config that is not JSON": {"roles": ROLES, "raw": "{not json"},
    "a config that is empty": {"roles": ["main"], "raw": ""},
    "a config file that is missing": {"roles": ROLES, "missing": True},
    "the reader refuses": {"roles": ROLES, "reader": "SystemExit"},
    "the reader answers null": {"roles": ["main"], "reader": "None"},
    "the reader fails otherwise": {"roles": ROLES, "reader": "PermissionError"},
    "the capabilities rebound on the launcher": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "main", "capabilities": ["merge"]}]}},
                                                "launcher": {"CONTROL_ROLE_CAPABILITIES": frozenset({"merge"})}},
    "the capabilities rebound, none match": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "main", "capabilities": CAPS}]}},
                                            "launcher": {"CONTROL_ROLE_CAPABILITIES": frozenset({"claim", "zeta", "alpha"})}},
}
DIRECTOR = {
    "a controller": {"roles": ROLES, "doc": {"workflow": {"roles": [{"name": "main", "capabilities": CAPS}]}}},
    "none, with a reason": {"roles": ["main"]},
    "no workflow file, a director configured": {"roles": ROLES},
    "the resolver rebound on the launcher": {"roles": ROLES, "launcher": {"control_role_name": "RESOLVER"}},
    "the resolver refuses": {"roles": ROLES, "reader": "SystemExit", "launcher": {"control_role_name": "RAISE"}},
}
CASES = {
    **{f"control: {k}": {"call": "control", **v} for k, v in CONTROL.items()},
    **{f"director: {k}": {"call": "director", **v} for k, v in DIRECTOR.items()},
}
FUNCTIONS = ("control_role_name", "director_role_name")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions, in a fresh test-owned tree; the launcher's reader recorded there."""
    import json, shutil, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd440-")).resolve()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"CONFIG {sorted(r.role for r in value.roles)}"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    names = [*FUNCTIONS, "CONTROL_ROLE_CAPABILITIES", "_load_json"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        path = tmp / "p440.json"
        if "doc" in spec:
            path.write_text(json.dumps(spec["doc"]), encoding="utf-8")
        elif "raw" in spec:
            path.write_text(spec["raw"], encoding="utf-8")
        given = path if any(k in spec for k in ("doc", "raw", "missing", "reader")) else None

        def reader(p):
            note("_load_json", p)
            kind = spec.get("reader")
            if kind == "SystemExit":
                raise SystemExit(f"{p} must contain a JSON object")
            if kind == "PermissionError":
                raise PermissionError(13, "Permission denied", str(p))
            if kind == "None":
                return None
            return saved["_load_json"](p)

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            return f

        stand = {"RESOLVER": lambda config, **kw: note("control_role_name", config, **kw) or ("syrd440-rebound", "rebound"),
                 "RAISE": lambda config, **kw: note("control_role_name", config, **kw) or (_ for _ in ()).throw(SystemExit("syrd440 the resolver refuses"))}
        t._load_json = reader
        t.control_role_name = passthrough("control_role_name")
        for k, v in spec.get("launcher", {}).items():
            setattr(t, k, stand[v] if isinstance(v, str) else v)
        config = SimpleNamespace(roles=[SimpleNamespace(role=r) for r in [*spec["roles"], *spec.get("extra", [])]])
        try:
            got = under["control_role_name" if spec["call"] == "control" else "director_role_name"](config, config_path=given)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": calls}
        return {"result": norm(got), "type": type(got).__name__, "json": json.dumps(got), "calls": calls}
    finally:
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
    result = python("import sys, scripts.control_role as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.control_role", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.control_role")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.control_role as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}={v.default!r}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty), "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'team_launcher') and not hasattr(m, 'ProjectConfig') and not hasattr(m, '_load_json'))")
        check(result.stdout.strip() == "True ['control_role_name.config_path=None', 'director_role_name.config_path=None'] True",
              f"{' then '.join(order)}: one set of objects, the same defaults, and nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.Path is Path and m.Mapping is typing.Mapping and t.Mapping is m.Mapping,
          "the standard-library names are the module's own, the very objects the launcher holds")
    check(m.CONTROL_ROLE_CAPABILITIES == frozenset({"set_manually_controlled", "merge"}) and type(m.CONTROL_ROLE_CAPABILITIES) is frozenset,
          "the capabilities are the same frozenset")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "control_role.py").read_text(encoding="utf-8"))
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
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported first thing when it runs, and nothing else imported: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "from pathlib import Path", "from typing import TYPE_CHECKING, Mapping"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, and the config type for annotations only: {top} {tc}")
    consts = {n.targets[0].id: ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)}
    check(consts == {"CONTROL_ROLE_CAPABILITIES": "frozenset({'set_manually_controlled', 'merge'})"},
          f"the capabilities are the baseline's literal, and the only constant: {consts}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the capabilities and the two functions in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_three_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.control_role"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"ProjectConfig", "role_control_accounts", "load_project_config"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition calls them, as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads the control role through the launcher, when it runs: {got}")


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

    none = ["", "this project's workflow declares no active role with the control capabilities (merge, set_manually_controlled)"]
    check(all(GOLDEN[k].get("type") == "tuple" for k in GOLDEN if k.startswith("control: ") and "raised" not in result(k))
          and all(GOLDEN[k].get("type") == "str" for k in GOLDEN if k.startswith("director: ") and "raised" not in result(k)),
          "the resolver answers a (name, reason) tuple; the director's name alone is a string")
    check(result("control: one controller") == ["boss", ""] and result("control: a controller with more capabilities") == ["main", ""]
          and result("control: several declared, one configured") == ["audit", ""] and result("control: a role entry that is not an object") == ["main", ""],
          "the one configured, active role holding both capabilities controls, whatever it is called")
    check(all(result(f"control: {k}") == none for k in ("only one of the two capabilities", "no role has them", "the controller is not configured",
                                                         "the controller is inactive", "the controller is inactive by zero", "a controller with no name",
                                                         "capabilities that are null", "capabilities given as a string")),
          "none: refused, naming the capabilities sorted -- both needed, active, configured, listed")
    check(result("control: several controllers") == ["", "this project's workflow gives the control capabilities to more than one role: director, main"]
          and result("control: the controller named twice") == ["", "this project's workflow gives the control capabilities to more than one role: main, main"],
          "several: refused, naming them sorted, a repeated name counted twice")
    check(all(result(f"control: {k}") == ["director", ""] for k in ("no workflow file, a director configured", "a workflow that is null", "no workflow key",
                                                                     "a workflow with no roles", "a workflow whose roles are null", "a workflow that is a list",
                                                                     "a config that is not an object", "the reader refuses"))
          and result("control: no workflow file, no director configured") == result("control: the reader answers null") == ["", "this project configures no director role"],
          "no usable workflow document -- none, not an object, no roles, or a reader that refuses -- falls back to the historical director")
    check(steps("control: no workflow file, a director configured") == [] and steps("control: one controller") == ["_load_json"]
          and GOLDEN["control: one controller"]["calls"][0][1] == ["PATH TMP/p440.json"],
          "the config is read only when its path is given, through the launcher's JSON reader")
    check([result(f"control: {k}")["raised"] for k in ("a config that is not JSON", "a config that is empty", "a config file that is missing", "the reader fails otherwise")]
          == ["JSONDecodeError", "JSONDecodeError", "FileNotFoundError", "PermissionError"],
          "only the reader's refusal is a fallback; a config that is not JSON or cannot be read raises")
    check(result("control: a controller with no name, an unnamed role configured") == ["", ""],
          "a nameless controller matching an unnamed configured role answers an empty name with no reason (the baseline's behaviour, kept)")
    check(result("control: the capabilities rebound on the launcher") == ["main", ""]
          and result("control: the capabilities rebound, none match")[1].endswith("(alpha, claim, zeta)"),
          "the capabilities are the launcher's when it runs")
    check(result("director: a controller") == "main" and result("director: none, with a reason") == ""
          and result("director: the resolver rebound on the launcher") == "syrd440-rebound"
          and result("director: the resolver refuses") == {"raised": "SystemExit", "message": "syrd440 the resolver refuses"}
          and all(steps(k)[0] == "control_role_name" for k in GOLDEN if k.startswith("director: ")),
          "the director's name is the launcher's resolver's name, its reason dropped, its refusal raised")


def test_every_launcher_seam_is_reached() -> None:
    # Every name the two read on the launcher is a recorder there, or rebound there by its own case.
    names = {name for reads in SEAMS.values() for name in reads}
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(names - {"CONTROL_ROLE_CAPABILITIES"} <= REACHED and "CONTROL_ROLE_CAPABILITIES" in rebound,
          f"a recorder on the launcher reached every function, and the capabilities were rebound there: missing {sorted(names - REACHED - rebound)}")
    check(rebound == {"CONTROL_ROLE_CAPABILITIES", "control_role_name"}, f"and exactly those rebound: {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_three_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"control_role_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
