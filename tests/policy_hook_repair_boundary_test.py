#!/usr/bin/env python3
"""SYRD-449: the repository policy-hook repair, against the launcher it came out of.

`repair_repository_policy_hooks` -- reinstalling a project's managed Git policy
hooks, missing or stale, warning rather than failing -- moved unchanged into
`scripts/policy_hook_repair.py`; the launcher re-exports it. This pins what
makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module -- not the hook installer, never the
  launcher; its defaults are the same, `print` the builtin.
- **Seams (rule 24):** it reads nothing from the launcher, as before. What it
  reads when it runs is `scripts.repository_hooks`, imported inside it exactly
  as before, so a suite that rebinds the installer there still reaches it: every
  case below does.
- **Reader:** `upgrade_phases.py` reads it through the launcher when it runs.
- **The behaviour is the baseline's:** a dry run; nothing, one or two hooks
  reinstalled, as a tuple or a list; ordinary failures warning only; a refusal
  that is not an ordinary exception raised; the config path and source
  repository passed on; the default printer. `GOLDEN` below was produced by
  running the BASELINE launcher's own definition over the very cases embedded
  here (`gold449.py`), not typed; it is byte-identical under `env -i`, in a
  normal role pane, with another HOME, USER and COLUMNS, under umask 077 and
  under several hash seeds.

Nothing is installed: the hook installer is a stand-in, and every path is a
fixed, non-existent one. Spawns, every exec, signals, account and group
lookups and socket connections are refused for each case.
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
from scripts import policy_hook_repair as m  # noqa: E402

CHECKS = 0
MOVED = ('repair_repository_policy_hooks',)
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals (none).
SEAMS = {
}
#: Measured on the baseline launcher: every launcher definition outside it that names it, and how often (none).
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads it through the launcher, and how often.
READERS = {'scripts/upgrade_phases.py': {'launcher.repair_repository_policy_hooks': 1}}
#: The BASELINE's own behaviour for the cases below (`gold449.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'a dry run': {'result': [], 'type': 'tuple', 'calls': [], 'printed': ['switchyard: would reinstall managed Git policy hooks for /nonexistent/syrd449/p449.json']},
    'a dry run, the default printer': {'result': [], 'type': 'tuple', 'calls': [], 'printed': ['switchyard: would reinstall managed Git policy hooks for /nonexistent/syrd449/p449.json', '']},
    'nothing to reinstall': {'result': [], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': []},
    'one hook reinstalled': {'result': ['PATH /nonexistent/syrd449/repo/.git/hooks/pre-commit'], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/repo/.git/hooks/pre-commit']},
    'two hooks reinstalled, as a tuple': {'result': ['PATH /nonexistent/syrd449/a/.git/hooks/pre-commit', 'PATH /nonexistent/syrd449/b/.git/hooks/pre-push'], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/a/.git/hooks/pre-commit', 'switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/b/.git/hooks/pre-push']},
    'the installer answers a list': {'result': ['PATH /nonexistent/syrd449/repo/.git/hooks/pre-commit'], 'type': 'list', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/repo/.git/hooks/pre-commit']},
    'the default printer': {'result': ['PATH /nonexistent/syrd449/repo/.git/hooks/pre-commit'], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/repo/.git/hooks/pre-commit', '']},
    'the installer fails with an OSError': {'result': [], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['warning: switchyard: could not repair Git policy hooks: syrd449 permission denied']},
    'the installer fails with a ValueError': {'result': [], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['warning: switchyard: could not repair Git policy hooks: syrd449 bad config']},
    'the installer fails with a RuntimeError, the default printer': {'result': [], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': ['warning: switchyard: could not repair Git policy hooks: syrd449 git exploded', '']},
    'the installer refuses with SystemExit': {'result': {'raised': 'SystemExit', 'message': 'syrd449 refused'}, 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': []},
    'a relative config path': {'result': [], 'type': 'tuple', 'calls': [['install_project_config', 'PATH relative/p449.json', {'source_root': 'PATH /nonexistent/syrd449/source'}]], 'printed': []},
    'the source repository passed on': {'result': [], 'type': 'tuple', 'calls': [['install_project_config', 'PATH /nonexistent/syrd449/p449.json', {'source_root': 'PATH /nonexistent/syrd449/other-source'}]], 'printed': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold449.py` (which ran them on the baseline) ------------------------------------
# A case repairs a synthetic project's policy hooks. The hook installer is a stand-in on `scripts.repository_hooks`, the
# module the repair imports it from when it runs, so no hook is installed in any repository; every path is a fixed,
# non-existent one. Recorded, in order: every call to the installer with its arguments, everything printed, and the
# answer (value and container type), or the exact exception.
CASES = {
    "a dry run": {"dry_run": True, "installed": ["/nonexistent/syrd449/repo/.git/hooks/pre-commit"]},
    "a dry run, the default printer": {"dry_run": True, "installed": [], "default_print": True},
    "nothing to reinstall": {"installed": []},
    "one hook reinstalled": {"installed": ["/nonexistent/syrd449/repo/.git/hooks/pre-commit"]},
    "two hooks reinstalled, as a tuple": {"installed": ("/nonexistent/syrd449/a/.git/hooks/pre-commit", "/nonexistent/syrd449/b/.git/hooks/pre-push"), "as": "tuple"},
    "the installer answers a list": {"installed": ["/nonexistent/syrd449/repo/.git/hooks/pre-commit"], "as": "list"},
    "the default printer": {"installed": ["/nonexistent/syrd449/repo/.git/hooks/pre-commit"], "default_print": True},
    "the installer fails with an OSError": {"raise": ("OSError", "syrd449 permission denied")},
    "the installer fails with a ValueError": {"raise": ("ValueError", "syrd449 bad config")},
    "the installer fails with a RuntimeError, the default printer": {"raise": ("RuntimeError", "syrd449 git exploded"), "default_print": True},
    "the installer refuses with SystemExit": {"raise": ("SystemExit", "syrd449 refused")},
    "a relative config path": {"config": "relative/p449.json", "installed": []},
    "the source repository passed on": {"source": "/nonexistent/syrd449/other-source", "installed": []},
}
FUNCTIONS = ("repair_repository_policy_hooks",)


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; the hook installer a stand-in on scripts.repository_hooks."""
    import contextlib, io
    from pathlib import Path as _P
    from scripts import repository_hooks
    calls: list = []
    printed: list = []

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, _P):
            return "PATH " + str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    def installer(config_path, **kwargs):
        reached.add("install_project_config")
        calls.append(["install_project_config", norm(config_path), {k: norm(v) for k, v in kwargs.items()}])
        if "raise" in spec:
            kind, message = spec["raise"]
            raise {"OSError": OSError, "ValueError": ValueError, "RuntimeError": RuntimeError, "SystemExit": SystemExit}[kind](message)
        paths = [_P(p) for p in spec["installed"]]
        return tuple(paths) if spec.get("as", "tuple") == "tuple" else paths

    saved = repository_hooks.install_project_config
    try:
        repository_hooks.install_project_config = installer
        repair = getattr(holder, "repair_repository_policy_hooks")
        kw = {"source_repo": _P(spec.get("source", "/nonexistent/syrd449/source"))}
        if spec.get("dry_run"):
            kw["dry_run"] = True
        config = _P(spec.get("config", "/nonexistent/syrd449/p449.json"))
        try:
            if spec.get("default_print"):
                shown = io.StringIO()
                with contextlib.redirect_stdout(shown):
                    got = repair(config, **kw)
                printed.extend(shown.getvalue().split("\n"))
            else:
                got = repair(config, print_func=printed.append, **kw)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": str(exc)}, "calls": calls, "printed": printed}
        return {"result": norm(got), "type": type(got).__name__, "calls": calls, "printed": printed}
    finally:
        repository_hooks.install_project_config = saved
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
    result = python("import sys, scripts.policy_hook_repair as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module -- not the hook installer, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.policy_hook_repair", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.policy_hook_repair")):
        result = python("import builtins, importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.policy_hook_repair as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "[f'{k}={v.default!r}' for k, v in inspect.signature(m.repair_repository_policy_hooks).parameters.items() "
                        "if v.default is not inspect.Parameter.empty and v.default is not builtins.print], "
                        "inspect.signature(m.repair_repository_policy_hooks).parameters['print_func'].default is builtins.print, "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'team_launcher') and not hasattr(m, 'repository_hooks'))")
        check(result.stdout.strip() == "True ['dry_run=False'] True True",
              f"{' then '.join(order)}: one object, the same defaults, the builtin printer, and the hook installer not bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    import typing
    check(m.Path is Path is pathlib.Path and t.Path is m.Path and m.Callable is typing.Callable,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "policy_hook_repair.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through = sorted({x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher"})
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(SEAMS.get(name, {}) == {} and through == []
              and imports == ["from scripts import repository_hooks"]
              and [ast.unparse(x) for x in node.body[first:first + 2]][1] == imports[0],
              f"{name}: reads nothing from the launcher, as before, and keeps its one call-time import of the hook installer, right after the dry run: {through} {imports}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "from pathlib import Path", "from typing import Callable"] and not [n for n in tree.body if isinstance(n, ast.If)],
          f"the standard library only, nothing for annotations: {top}")
    runtime = [ast.unparse(x) for x in ast.walk(tree) if isinstance(x, (ast.Import, ast.ImportFrom)) and "team_launcher" in ast.unparse(x)]
    check(runtime == [], f"the launcher is never imported: {runtime}")
    names = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else ast.unparse(n)[:40] for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the one function, and nothing else: {names}")


def test_the_launcher_reexports_it_and_its_reader_reaches_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.policy_hook_repair"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the repair, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read it")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"restore_interrupted_role_state", "_role_cli_name", "ensure_generated_project_board_skill"} <= defined | exported,
          "the launcher defines none of it, and keeps its neighbours, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition names it, as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's name, or reads it at module level: {past} {loose}")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.Attribute) and x.attr in MOVED and isinstance(x.value, ast.Name) and x.value.id in ("launcher", "team_launcher"):
                got[ast.unparse(x)] = got.get(ast.unparse(x), 0) + 1
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads it through the launcher, as often as before: {got} {bare}")


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

    check(result("a dry run") == [] and GOLDEN["a dry run"]["type"] == "tuple" and steps("a dry run") == []
          and GOLDEN["a dry run"]["printed"] == ["switchyard: would reinstall managed Git policy hooks for /nonexistent/syrd449/p449.json"],
          "a dry run says what it would do, installs nothing, and answers an empty tuple")
    check(GOLDEN["one hook reinstalled"]["calls"] == [["install_project_config", "PATH /nonexistent/syrd449/p449.json", {"source_root": "PATH /nonexistent/syrd449/source"}]]
          and GOLDEN["the source repository passed on"]["calls"][0][2] == {"source_root": "PATH /nonexistent/syrd449/other-source"}
          and GOLDEN["a relative config path"]["calls"][0][1] == "PATH relative/p449.json",
          "the installer is called once, with the config path as given and the source repository as source_root")
    check(GOLDEN["two hooks reinstalled, as a tuple"]["printed"] == ["switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/a/.git/hooks/pre-commit",
                                                                     "switchyard: reinstalled managed Git policy hook /nonexistent/syrd449/b/.git/hooks/pre-push"]
          and GOLDEN["nothing to reinstall"]["printed"] == [],
          "every reinstalled hook is named, in the installer's order; nothing reinstalled, nothing said")
    check(GOLDEN["the installer answers a list"]["type"] == "list" and GOLDEN["two hooks reinstalled, as a tuple"]["type"] == "tuple",
          "the answer is the installer's own, as it is")
    check(all(result(f"the installer fails with {k}") == [] for k in ("an OSError", "a ValueError", "a RuntimeError, the default printer"))
          and GOLDEN["the installer fails with an OSError"]["printed"] == ["warning: switchyard: could not repair Git policy hooks: syrd449 permission denied"],
          "any ordinary failure only warns, and answers an empty tuple: a hook repair never fails an upgrade")
    check(result("the installer refuses with SystemExit") == {"raised": "SystemExit", "message": "syrd449 refused"},
          "a refusal that is not an ordinary exception is not caught (the baseline's behaviour, kept)")
    check(GOLDEN["the default printer"]["printed"][0].startswith("switchyard: reinstalled managed Git policy hook")
          and GOLDEN["a dry run, the default printer"]["printed"][0].startswith("switchyard: would reinstall"),
          "the default printer is print")


def test_every_launcher_seam_is_reached() -> None:
    # It reads nothing from the launcher; what it reaches when it runs is the hook installer, stood in on its own module.
    check(SEAMS == {} and REACHED == {"install_project_config"}, f"no launcher seam, and the installer reached: {sorted(REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it_and_its_reader_reaches_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"policy_hook_repair_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
