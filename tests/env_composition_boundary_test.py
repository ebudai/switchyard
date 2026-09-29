#!/usr/bin/env python3
"""SYRD-460: the launcher's environment and PATH composition, against the launcher it came out of.

The eight -- `_owner_home_bin_dirs`, `_env_prefix`, `_env_unset_prefix`,
`_prepend_path`, `_prepend_paths`, `TERMINAL_PRESENTATION_ENV_KEYS`,
`_terminal_presentation_env` and `_pane_identity_scrubbed_env` -- moved
unchanged into `scripts/env_composition.py`; the launcher re-exports them all
and keeps the pane identity keys, the bin-directory variables and
`default_user_bin_dirs`. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher, and uses only the
  standard library.
- **Seams (rule 24):** what they read of the launcher -- `_env_first`, the
  bin-directory variables, the pane identity keys and the two moved siblings --
  is read through it as often as before, so a patch there reaches them.
- **Readers:** no launcher definition names them, and every production module
  reads them through the launcher, as often as before.
- **The behaviour is the baseline's,** values only: the bin directories, PATH
  order, deduplication and empty entries, the `KEY=value` order with the pane
  target first, the `-u` order, the non-blank terminal variables, and a new
  environment without the identity keys, its source untouched. `GOLDEN` below
  was produced by running the BASELINE launcher's own definitions over the very
  cases embedded here (`gold460.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with stray terminal, pane and bin
  variables.

Nothing is run and no account is looked up: each case replaces the whole process
environment with a synthetic one and restores it afterwards. Spawns, every exec,
signals, account and group lookups and socket connections are refused for each
case.
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
from scripts import env_composition as m  # noqa: E402

CHECKS = 0
MOVED = ('_owner_home_bin_dirs', '_env_prefix', '_env_unset_prefix', '_prepend_path', '_prepend_paths', 'TERMINAL_PRESENTATION_ENV_KEYS', '_terminal_presentation_env', '_pane_identity_scrubbed_env')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_owner_home_bin_dirs': {'LEGACY_USER_BIN_ENV': 1, 'USER_BIN_ENV': 1, '_env_first': 1},
    '_prepend_path': {'_prepend_paths': 1},
    '_terminal_presentation_env': {'TERMINAL_PRESENTATION_ENV_KEYS': 1},
    '_pane_identity_scrubbed_env': {'PROBE_IDENTITY_ENV_KEYS': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the eight that names them, and how often.
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/first_run_auth.py': {'launcher._env_prefix': 1, 'launcher._env_unset_prefix': 1, 'launcher._pane_identity_scrubbed_env': 1}, 'scripts/owner_commands.py': {'launcher._owner_home_bin_dirs': 1, 'launcher._prepend_paths': 1, 'launcher._terminal_presentation_env': 1}, 'scripts/provider_auth_status.py': {'launcher._pane_identity_scrubbed_env': 2}, 'scripts/provider_session.py': {'launcher._pane_identity_scrubbed_env': 1}, 'scripts/role_command.py': {'launcher._env_prefix': 1, 'launcher._env_unset_prefix': 1, 'launcher._prepend_paths': 1}, 'scripts/tenant_release_report.py': {'launcher._owner_home_bin_dirs': 1, 'launcher._prepend_paths': 1}}
#: The BASELINE's own behaviour for the cases below (`gold460.py`, run on the baseline launcher under the guard).
GOLDEN = {
    "bin: the home's two": {'result': {'type': 'list', 'value': ['/home/p460/bin', '/home/p460/.local/bin']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: the configured directory': {'result': {'type': 'list', 'value': ['/opt/p460/bin']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: the legacy directory': {'result': {'type': 'list', 'value': ['/opt/p460/legacy']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: both, the current one wins': {'result': {'type': 'list', 'value': ['/opt/p460/bin']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: a configured directory under ~': {'result': {'type': 'list', 'value': ['/home/p460-caller/tools']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: a blank configured directory': {'result': {'type': 'list', 'value': ['/home/p460/bin', '/home/p460/.local/bin']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: a home under ~': {'result': {'type': 'list', 'value': ['/home/p460-caller/owner/bin', '/home/p460-caller/owner/.local/bin']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: the lookup rebound on the launcher': {'result': {'type': 'list', 'value': ['/p460/rebound']}, 'calls': [['_env_first', ['TEAM_LAUNCHER_BIN_DIR', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'bin: the variable renamed on the launcher': {'result': {'type': 'list', 'value': ['/p460/renamed']}, 'calls': [['_env_first', ['P460_BIN', 'PGU_TEAM_LAUNCHER_BIN_DIR'], {}]]},
    'prefix: the pane target first, then sorted': {'result': {'type': 'list', 'value': ['TICKET_BOARD_PANE_TARGET=syrd:1.0', 'A=1', 'Z=9', 'a=0', 'b=2']}, 'calls': []},
    'prefix: no pane target': {'result': {'type': 'list', 'value': ['HOME=/home/p460', 'LANG=C.UTF-8', 'PATH=/usr/bin']}, 'calls': []},
    'prefix: values kept as they are': {'result': {'type': 'list', 'value': ['EMPTY=', 'EQ=x=y', 'P460=a b \'c\' "d" $e']}, 'calls': []},
    'prefix: nothing': {'result': {'type': 'list', 'value': []}, 'calls': []},
    'unset: order kept, repeats kept': {'result': {'type': 'list', 'value': ['-u', 'Z', '-u', 'A', '-u', 'Z']}, 'calls': []},
    'unset: a tuple': {'result': {'type': 'list', 'value': ['-u', 'DISPLAY', '-u', 'WAYLAND_DISPLAY']}, 'calls': []},
    'unset: nothing': {'result': {'type': 'list', 'value': []}, 'calls': []},
    'path: one new directory': {'result': {'type': 'str', 'value': '/opt/p460/bin:/usr/bin:/bin'}, 'calls': [['_prepend_paths', ['/usr/bin:/bin', ['/opt/p460/bin']], {}]]},
    'path: one already there': {'result': {'type': 'str', 'value': '/opt/p460/bin:/usr/bin:/bin'}, 'calls': [['_prepend_paths', ['/usr/bin:/opt/p460/bin:/bin:/opt/p460/bin', ['/opt/p460/bin']], {}]]},
    'path: the single form through the launcher': {'result': {'type': 'str', 'value': 'REBOUND:/opt/p460/bin'}, 'calls': [['_prepend_paths', ['/usr/bin', ['/opt/p460/bin']], {}]]},
    'paths: several, in order': {'result': {'type': 'str', 'value': '/p460/a:/p460/b:/usr/bin:/bin'}, 'calls': []},
    'paths: duplicates and empty entries': {'result': {'type': 'str', 'value': '/p460/a:/p460/b:/usr/bin:/bin'}, 'calls': []},
    'paths: an empty PATH': {'result': {'type': 'str', 'value': '/p460/a'}, 'calls': []},
    'paths: no directories': {'result': {'type': 'str', 'value': '/usr/bin:/bin'}, 'calls': []},
    'terminal: from the environment': {'result': {'type': 'list', 'value': ['TERM=xterm-256color', 'TERM_PROGRAM=p460 term']}, 'calls': [], 'environ after': {'COLORTERM': '  ', 'OTHER': 'x', 'TERM': 'xterm-256color', 'TERM_PROGRAM': 'p460 term'}},
    'terminal: nothing set': {'result': {'type': 'list', 'value': []}, 'calls': [], 'environ after': {}},
    'terminal: from a mapping': {'result': {'type': 'list', 'value': ['TERM=screen', 'TERM_PROGRAM_VERSION=4.6']}, 'calls': [], 'environ after': {'TERM': 'ignored'}, 'mapping unchanged': True},
    'terminal: an empty mapping': {'result': {'type': 'list', 'value': []}, 'calls': [], 'environ after': {'TERM': 'ignored'}, 'mapping unchanged': True},
    'terminal: the keys rebound on the launcher': {'result': {'type': 'list', 'value': ['P460=1', 'TERM=screen']}, 'calls': [], 'environ after': {}, 'mapping unchanged': True},
    'scrub: the environment': {'result': {'type': 'dict', 'value': {'HOME': '/home/p460', 'KEEP': '1'}, 'a new dict': True}, 'calls': [], 'environ after': {'HOME': '/home/p460', 'KEEP': '1', 'PGU_PANE_TARGET': 's:0.1', 'TICKET_BOARD_CALLER_ROLE': 'main', 'TICKET_BOARD_PANE_TARGET': 's:0.2', 'TMUX': '/tmp/p460,1,0', 'TMUX_PANE': '%1'}},
    'scrub: a mapping': {'result': {'type': 'dict', 'value': {'PATH': '/usr/bin'}, 'a new dict': True}, 'calls': [], 'environ after': {'TMUX': 'kept-in-environ'}, 'mapping unchanged': True},
    'scrub: nothing to remove': {'result': {'type': 'dict', 'value': {'PATH': '/usr/bin'}, 'a new dict': True}, 'calls': [], 'environ after': {}, 'mapping unchanged': True},
    'scrub: the keys rebound on the launcher': {'result': {'type': 'dict', 'value': {'PATH': '/usr/bin', 'TMUX': 'x'}, 'a new dict': True}, 'calls': [], 'environ after': {}, 'mapping unchanged': True},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold460.py` (which ran them on the baseline) ------------------------------------
# A case composes one value with one of the eight and records the answer or the exact exception, in order every call it
# made of the launcher, and -- for the two that read an environment -- that environment afterwards and whether the
# answer is a new object. Nothing is run and no account is looked up. For each case the whole process environment is
# replaced by the case's synthetic one and restored afterwards; the launcher's `_env_first` and `_prepend_paths` are
# recorded there and passed through, and a case may rebind a launcher name to show it is read when the function runs.
CASES = {
    "bin: the home's two": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"]},
    "bin: the configured directory": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"], "environ": {"USER_BIN_ENV": "/opt/p460/bin"}},
    "bin: the legacy directory": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"], "environ": {"LEGACY_USER_BIN_ENV": "/opt/p460/legacy"}},
    "bin: both, the current one wins": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"],
                                        "environ": {"USER_BIN_ENV": "/opt/p460/bin", "LEGACY_USER_BIN_ENV": "/opt/p460/legacy"}},
    "bin: a configured directory under ~": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"], "environ": {"USER_BIN_ENV": "~/tools", "HOME": "/home/p460-caller"}},
    "bin: a blank configured directory": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"], "environ": {"USER_BIN_ENV": "   ", "LEGACY_USER_BIN_ENV": ""}},
    "bin: a home under ~": {"call": "_owner_home_bin_dirs", "args": ["PATH:~/owner"], "environ": {"HOME": "/home/p460-caller"}},
    "bin: the lookup rebound on the launcher": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"], "rebind": {"_env_first": "/p460/rebound"}},
    "bin: the variable renamed on the launcher": {"call": "_owner_home_bin_dirs", "args": ["PATH:/home/p460"], "environ": {"P460_BIN": "/p460/renamed"},
                                                  "rebind": {"USER_BIN_ENV": "P460_BIN"}},
    "prefix: the pane target first, then sorted": {"call": "_env_prefix", "args": [{"b": "2", "TICKET_BOARD_PANE_TARGET": "syrd:1.0", "A": "1", "a": "0", "Z": "9"}]},
    "prefix: no pane target": {"call": "_env_prefix", "args": [{"HOME": "/home/p460", "PATH": "/usr/bin", "LANG": "C.UTF-8"}]},
    "prefix: values kept as they are": {"call": "_env_prefix", "args": [{"P460": "a b 'c' \"d\" $e", "EQ": "x=y", "EMPTY": ""}]},
    "prefix: nothing": {"call": "_env_prefix", "args": [{}]},
    "unset: order kept, repeats kept": {"call": "_env_unset_prefix", "args": [["Z", "A", "Z"]]},
    "unset: a tuple": {"call": "_env_unset_prefix", "args": [("DISPLAY", "WAYLAND_DISPLAY")]},
    "unset: nothing": {"call": "_env_unset_prefix", "args": [[]]},
    "path: one new directory": {"call": "_prepend_path", "args": ["/usr/bin:/bin", "/opt/p460/bin"]},
    "path: one already there": {"call": "_prepend_path", "args": ["/usr/bin:/opt/p460/bin:/bin:/opt/p460/bin", "/opt/p460/bin"]},
    "path: the single form through the launcher": {"call": "_prepend_path", "args": ["/usr/bin", "/opt/p460/bin"], "rebind": {"_prepend_paths": "REBOUND"}},
    "paths: several, in order": {"call": "_prepend_paths", "args": ["/usr/bin:/bin", ["/p460/a", "/p460/b"]]},
    "paths: duplicates and empty entries": {"call": "_prepend_paths", "args": ["::/p460/b:/usr/bin::/p460/a:/bin:", ["/p460/a", "", "/p460/b", "/p460/a"]]},
    "paths: an empty PATH": {"call": "_prepend_paths", "args": ["", ["/p460/a"]]},
    "paths: no directories": {"call": "_prepend_paths", "args": ["/usr/bin::/bin", []]},
    "terminal: from the environment": {"call": "_terminal_presentation_env", "args": [],
                                       "environ": {"TERM": "xterm-256color", "COLORTERM": "  ", "TERM_PROGRAM": "p460 term", "OTHER": "x"}},
    "terminal: nothing set": {"call": "_terminal_presentation_env", "args": []},
    "terminal: from a mapping": {"call": "_terminal_presentation_env", "args": [{"TERM_PROGRAM_VERSION": "4.6", "TERM": "screen", "COLORTERM": None}],
                                 "environ": {"TERM": "ignored"}},
    "terminal: an empty mapping": {"call": "_terminal_presentation_env", "args": [{}], "environ": {"TERM": "ignored"}},
    "terminal: the keys rebound on the launcher": {"call": "_terminal_presentation_env", "args": [{"TERM": "screen", "P460": "1", "COLORTERM": "truecolor"}],
                                                   "rebind": {"TERMINAL_PRESENTATION_ENV_KEYS": ("P460", "TERM")}},
    "scrub: the environment": {"call": "_pane_identity_scrubbed_env", "args": [],
                               "environ": {"TMUX": "/tmp/p460,1,0", "TMUX_PANE": "%1", "TICKET_BOARD_CALLER_ROLE": "main", "PGU_PANE_TARGET": "s:0.1",
                                           "TICKET_BOARD_PANE_TARGET": "s:0.2", "HOME": "/home/p460", "KEEP": "1"}},
    "scrub: a mapping": {"call": "_pane_identity_scrubbed_env", "args": [{"TMUX": "x", "TICKET_BOARD_PANE_SESSION_ID": "7", "PATH": "/usr/bin"}],
                         "environ": {"TMUX": "kept-in-environ"}},
    "scrub: nothing to remove": {"call": "_pane_identity_scrubbed_env", "args": [{"PATH": "/usr/bin"}]},
    "scrub: the keys rebound on the launcher": {"call": "_pane_identity_scrubbed_env", "args": [{"TMUX": "x", "P460": "y", "PATH": "/usr/bin"}],
                                                "rebind": {"PROBE_IDENTITY_ENV_KEYS": ("P460",)}},
}
FUNCTIONS = ("_owner_home_bin_dirs", "_env_prefix", "_env_unset_prefix", "_prepend_path", "_prepend_paths", "_terminal_presentation_env", "_pane_identity_scrubbed_env")
PASSED = ("_env_first", "_prepend_paths")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition, in a synthetic process environment; nothing run, no account looked up."""
    import os
    from pathlib import Path as _P
    calls: list = []

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        if isinstance(value, _P):
            return "PATH " + str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, "USER_BIN_ENV", "PROBE_IDENTITY_ENV_KEYS", "TERMINAL_PRESENTATION_ENV_KEYS")}
    saved_env = dict(os.environ)
    names = {"USER_BIN_ENV": t.USER_BIN_ENV, "LEGACY_USER_BIN_ENV": t.LEGACY_USER_BIN_ENV}
    try:
        os.environ.clear()
        for k, v in spec.get("environ", {}).items():
            os.environ[names.get(k, k)] = v
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name, *a, **k) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "_env_first":
                t._env_first = lambda *keys: note("_env_first", *keys) or value
            elif name == "_prepend_paths":
                t._prepend_paths = lambda path_value, directories: note("_prepend_paths", path_value, directories) or f"{value}:{':'.join(directories)}"
            else:
                setattr(t, name, value)
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        args = [_P(a[5:]) if isinstance(a, str) and a.startswith("PATH:") else a for a in spec["args"]]
        source = args[0] if args and isinstance(args[0], dict) else None
        before = dict(source) if source is not None else None
        try:
            got = fn(*args)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        else:
            result = {"type": type(got).__name__, "value": norm(got)}
            if spec["call"] == "_pane_identity_scrubbed_env":
                result["a new dict"] = got is not source and got is not os.environ
        out = {"result": result, "calls": calls}
        if spec["call"] in ("_terminal_presentation_env", "_pane_identity_scrubbed_env"):
            out["environ after"] = norm(dict(os.environ))
            if source is not None:
                out["mapping unchanged"] = source == before
        return out
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        for n, v in saved.items():
            setattr(t, n, v)
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


#: What the Director kept on the launcher, beside the eight.
KEPT = ("PROBE_IDENTITY_ENV_KEYS", "USER_BIN_ENV", "LEGACY_USER_BIN_ENV", "default_user_bin_dirs")
#: The one constant among the eight, and its measured value.
KEYS = ("TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION")


def top_name(n: ast.AST) -> str | None:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
    return None


def annotation_ids(tree: ast.AST) -> set[int]:
    """Every node inside an annotation, or inside a TYPE_CHECKING block: names there are not read when the code runs."""
    out: set[int] = set()
    for x in ast.walk(tree):
        parts = []
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = [x.returns, *(a.annotation for a in x.args.posonlyargs + x.args.args + x.args.kwonlyargs + [v for v in (x.args.vararg, x.args.kwarg) if v])]
        elif isinstance(x, ast.AnnAssign):
            parts = [x.annotation]
        elif isinstance(x, ast.If) and "TYPE_CHECKING" in ast.unparse(x.test):
            parts = [x]
        for part in parts:
            if part is not None:
                out |= {id(y) for y in ast.walk(part)}
    return out


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.env_composition as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.env_composition", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.env_composition")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.env_composition as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), m.TERMINAL_PRESENTATION_ENV_KEYS == {KEYS!r}, "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', '_env_first', 'PANE_TARGET_ENV_KEYS', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.env_composition'] True True",
              f"{' then '.join(order)}: one object each, defined here; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    import typing
    check(m.os is os and m.Path is pathlib.Path and m.Mapping is typing.Mapping and m.Sequence is typing.Sequence
          and t.os is m.os and t.Path is m.Path and t.Mapping is m.Mapping and t.Sequence is m.Sequence,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "env_composition.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, its sibling included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check((imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0]) if expected else imports == [],
              f"{name}: the launcher imported first thing (after its docstring) when it reads one, and nothing else imported: {imports}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import os", "from pathlib import Path", "from typing import Mapping, Sequence"]
          and not [n for n in tree.body if isinstance(n, ast.If)], f"the standard library only: {top}")
    const = next(n for n in tree.body if top_name(n) == "TERMINAL_PRESENTATION_ENV_KEYS")
    check(ast.literal_eval(const.value) == KEYS, "the terminal keys are the same literal tuple, in order, evaluated at definition time without reading anything")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {"_owner_home_bin_dirs": [], "_env_prefix": [], "_env_unset_prefix": [], "_prepend_path": [], "_prepend_paths": [],
                       "_terminal_presentation_env": ["None"], "_pane_identity_scrubbed_env": ["None"]},
          f"the only defaults are source=None, literals: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the eight, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.env_composition"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the eight, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | set(KEPT) | set(DISPATCH) <= defined | exported,
          "the launcher defines none of them, and keeps the pane identity keys, the bin-directory variables, default_user_bin_dirs and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"the launcher's own definitions name them exactly as often as before -- none: {uses}")
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
        skip = annotation_ids(source)
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED and id(x) not in skip})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads them through the launcher, as often as before: {got} {bare}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


#: The owner prefix the baseline builds: an expected argv prefix, compared and never run.
OWNER_PREFIX = ["sudo", "-u"]


def test_the_rules_hold_in_the_measured_record() -> None:
    def value(label):
        return GOLDEN[label]["result"]["value"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    check(value("bin: the home's two") == value("bin: a blank configured directory") == ["/home/p460/bin", "/home/p460/.local/bin"]
          and value("bin: the configured directory") == value("bin: both, the current one wins") == ["/opt/p460/bin"]
          and value("bin: the legacy directory") == ["/opt/p460/legacy"] and value("bin: a configured directory under ~") == ["/home/p460-caller/tools"]
          and value("bin: a home under ~") == ["/home/p460-caller/owner/bin", "/home/p460-caller/owner/.local/bin"]
          and GOLDEN["bin: the home's two"]["calls"] == [["_env_first", ["TEAM_LAUNCHER_BIN_DIR", "PGU_TEAM_LAUNCHER_BIN_DIR"], {}]],
          "the owner's bin directories: a configured one (current, then legacy; ~ expanded), else bin and .local/bin under the home")
    check(value("bin: the lookup rebound on the launcher") == ["/p460/rebound"] and value("bin: the variable renamed on the launcher") == ["/p460/renamed"],
          "the variable lookup and the variable names are the launcher's, read when it runs")
    check(value("prefix: the pane target first, then sorted") == ["TICKET_BOARD_PANE_TARGET=syrd:1.0", "A=1", "Z=9", "a=0", "b=2"]
          and value("prefix: no pane target") == ["HOME=/home/p460", "LANG=C.UTF-8", "PATH=/usr/bin"]
          and value("prefix: values kept as they are") == ["EMPTY=", "EQ=x=y", "P460=a b 'c' \"d\" $e"] and value("prefix: nothing") == [],
          "env arguments: the pane target first, then every other key in code-point order, values unquoted and unchanged")
    check(value("unset: order kept, repeats kept") == ["-u", "Z", "-u", "A", "-u", "Z"] and value("unset: a tuple") == ["-u", "DISPLAY", "-u", "WAYLAND_DISPLAY"]
          and value("unset: nothing") == [],
          "unset arguments: -u and the key, in the order given, nothing sorted or merged")
    check(value("path: one new directory") == value("path: one already there") == "/opt/p460/bin:/usr/bin:/bin"
          and value("paths: several, in order") == value("paths: duplicates and empty entries") == "/p460/a:/p460/b:/usr/bin:/bin"
          and value("paths: an empty PATH") == "/p460/a" and value("paths: no directories") == "/usr/bin:/bin"
          and value("path: the single form through the launcher") == "REBOUND:/opt/p460/bin" and calls("path: one new directory") == ["_prepend_paths"],
          "PATH: the directories first, in the order given, every other copy of them dropped, empty entries dropped; one directory through the launcher's list form")
    check(value("terminal: from the environment") == ["TERM=xterm-256color", "TERM_PROGRAM=p460 term"] and value("terminal: nothing set") == []
          and value("terminal: from a mapping") == ["TERM=screen", "TERM_PROGRAM_VERSION=4.6"] and value("terminal: an empty mapping") == []
          and value("terminal: the keys rebound on the launcher") == ["P460=1", "TERM=screen"]
          and all(GOLDEN[k]["mapping unchanged"] for k in GOLDEN if "mapping unchanged" in GOLDEN[k]),
          "terminal variables: the launcher's keys in their order, blank or missing ones skipped, from the environment or a given mapping, which is untouched")
    scrubbed = GOLDEN["scrub: the environment"]
    check(scrubbed["result"]["value"] == {"HOME": "/home/p460", "KEEP": "1"} and scrubbed["environ after"]["TMUX"] == "/tmp/p460,1,0"
          and value("scrub: a mapping") == {"PATH": "/usr/bin"} and GOLDEN["scrub: a mapping"]["environ after"] == {"TMUX": "kept-in-environ"}
          and value("scrub: the keys rebound on the launcher") == {"PATH": "/usr/bin", "TMUX": "x"}
          and all(GOLDEN[k]["result"]["a new dict"] for k in GOLDEN if k.startswith("scrub:")),
          "scrubbing: a new dict without the launcher's identity keys (read when it runs); the environment and the mapping are untouched")


def test_every_launcher_seam_is_reached() -> None:
    # The two functions the eight read on the launcher are recorders there; the constants are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name))}
    check(constants == {"USER_BIN_ENV", "LEGACY_USER_BIN_ENV", "TERMINAL_PRESENTATION_ENV_KEYS", "PROBE_IDENTITY_ENV_KEYS"} and names - constants <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"env_composition_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
