#!/usr/bin/env python3
"""SYRD-400: shared-release activation and the host boundary install, against the launcher they came out of.

`install_host_privileged_boundary` and `switchyard_install_shared_release_command`
moved unchanged into `scripts/host_boundary_install.py`, and the launcher
re-exports both. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the `runner` and `print_func`
  defaults are bound when each function is defined.
- **Seams (rule 24):** the command reads the boundary installer through the
  launcher when it runs, so a patch there reaches it, which this test shows;
  the catalogue, the activation and the install commands are still imported
  inside each function, where they were.
- **The behaviour is the baseline's:** both arguments; an invalid commit;
  anyone but root; a dry run of either; activation then the boundary, from
  the ACTIVATED release, installed or failing with the repair line; a rollback,
  with no boundary; a failed activation or rollback; a result with no release
  root; and every command, capture, bound and message of the installer --
  `GOLDEN` below was produced by running the BASELINE launcher's own functions
  with the same stand-ins (`gold400.py`), not typed. That includes the
  baseline's `AttributeError` for a runner result with no `returncode`,
  preserved here (reported in SYRD-399).

Nothing is activated or installed: every path is `/nonexistent`, `os.geteuid`
and every helper are stand-ins on their own modules, and the real
`subprocess.run`/`Popen` are refused.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import host_boundary_install as m  # noqa: E402
from scripts.ticket_board import privileged_actions, privileged_install  # noqa: E402
from scripts.ticket_board import shared_release_activation as sra  # noqa: E402

CHECKS = 0
MOVED = ("install_host_privileged_boundary", "switchyard_install_shared_release_command")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals.
SEAMS = {
    'switchyard_install_shared_release_command': {'install_host_privileged_boundary': 1},
}
#: Measured on the baseline launcher: the Switchyard imports each body makes when it runs.
LOCAL_IMPORTS = {
    'install_host_privileged_boundary': ['from scripts.ticket_board import privileged_install'],
    'switchyard_install_shared_release_command': ['from scripts.ticket_board import privileged_actions, shared_release_activation'],
}
#: The BASELINE's own behaviour for the cases below (`gold400.py`, run on the baseline launcher under the guard).
GOLDEN = {'command:an invalid commit': {'code': 2,
                               'said': ['switchyard: commit must be a full content-addressed sha, not HEAD'],
                               'calls': [['action', 'install-shared-release'], ['validate', {'commit': 'HEAD'}], ['say']]},
 'command:both arguments': {'code': 2, 'said': ['switchyard: give either --commit or --rollback, not both'], 'calls': [['say']]},
 'command:not root': {'code': 1,
                      'said': ['switchyard: install-shared-release changes /opt/switchyard/current and must run as root. It is reached '
                               'through `switchyard privileged-action <project> install-shared-release commit=<sha>`, which asks polkit '
                               'for it'],
                      'calls': [['action', 'install-shared-release'],
                                ['validate', {'commit': 'cccccccccccccccccccccccccccccccccccccccc'}],
                                ['euid'],
                                ['say']]},
 'command:a dry run': {'code': 0,
                       'said': ['switchyard: current points at /nonexistent/syrd400/releases/now',
                                'switchyard: would activate cccccccccccccccccccccccccccccccccccccccc'],
                       'calls': [['action', 'install-shared-release'],
                                 ['validate', {'commit': 'cccccccccccccccccccccccccccccccccccccccc'}],
                                 ['euid'],
                                 ['recorded rollback'],
                                 ['read pointer'],
                                 ['say'],
                                 ['say']]},
 'command:a dry run of a rollback': {'code': 0,
                                     'said': ['switchyard: current points at /nonexistent/syrd400/releases/now',
                                              'switchyard: would activate the recorded previous target /nonexistent/syrd400/releases/prev'],
                                     'calls': [['euid'], ['recorded rollback'], ['read pointer'], ['say'], ['say']]},
 'command:activated, boundary installed': {'code': 0,
                                           'said': ['switchyard: cccccccccccccccccccccccccccccccccccccccc is current'],
                                           'calls': [['action', 'install-shared-release'],
                                                     ['validate', {'commit': ' cccccccccccccccccccccccccccccccccccccccc '}],
                                                     ['euid'],
                                                     ['activate', ['cccccccccccccccccccccccccccccccccccccccc'], ['print_func']],
                                                     ['say'],
                                                     ['boundary',
                                                      '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                                      {'staging_root': '/nonexistent/syrd400/sandbox', 'print_func': 'CALLABLE'}]]},
 'command:activated, boundary fails': {'code': 1,
                                       'said': ['switchyard: cccccccccccccccccccccccccccccccccccccccc is current',
                                                'switchyard: the helper is missing',
                                                'switchyard: polkit is stale',
                                                "switchyard: cccccccccccccccccccccccccccccccccccccccc is current, but this host's "
                                                'privileged boundary was not installed from it; run this again to repair it'],
                                       'calls': [['action', 'install-shared-release'],
                                                 ['validate', {'commit': 'cccccccccccccccccccccccccccccccccccccccc'}],
                                                 ['euid'],
                                                 ['activate', ['cccccccccccccccccccccccccccccccccccccccc'], ['print_func']],
                                                 ['say'],
                                                 ['boundary',
                                                  '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                                  {'staging_root': None, 'print_func': 'CALLABLE'}],
                                                 ['say'],
                                                 ['say'],
                                                 ['say']]},
 'command:rolled back': {'code': 0, 'said': ['switchyard: rolled back'], 'calls': [['euid'], ['rollback', [], ['print_func']], ['say']]},
 'command:a rollback whose result is not marked rolled back': {'code': 0,
                                                               'said': ['switchyard: cccccccccccccccccccccccccccccccccccccccc is current'],
                                                               'calls': [['euid'],
                                                                         ['rollback', [], ['print_func']],
                                                                         ['say'],
                                                                         ['boundary',
                                                                          '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                                                          {'staging_root': None, 'print_func': 'CALLABLE'}]]},
 'command:activation fails': {'code': 1,
                              'said': ['switchyard: the release is not installed'],
                              'calls': [['action', 'install-shared-release'],
                                        ['validate', {'commit': 'cccccccccccccccccccccccccccccccccccccccc'}],
                                        ['euid'],
                                        ['activate', ['cccccccccccccccccccccccccccccccccccccccc'], ['print_func']],
                                        ['say']]},
 'command:rollback fails': {'code': 1,
                            'said': ['switchyard: the release is not installed'],
                            'calls': [['euid'], ['rollback', [], ['print_func']], ['say']]},
 'command:no release root': {'code': 0,
                             'said': ['switchyard: nothing to point at'],
                             'calls': [['action', 'install-shared-release'],
                                       ['validate', {'commit': 'cccccccccccccccccccccccccccccccccccccccc'}],
                                       ['euid'],
                                       ['activate', ['cccccccccccccccccccccccccccccccccccccccc'], ['print_func']],
                                       ['say']]},
 'installer:a dry run': {'answer': [],
                         'said': ["switchyard: would install this host's privileged boundary from "
                                  '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc'],
                         'calls': [['roots', '/nonexistent/syrd400/sandbox'], ['say']]},
 'installer:installed': {'answer': [],
                         'said': ["switchyard: installed this host's privileged boundary from "
                                  '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc'],
                         'calls': [['roots', '/nonexistent/syrd400/sandbox'],
                                   ['commands',
                                    '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                    '/nonexistent/syrd400/boundary',
                                    '/nonexistent/syrd400/policy',
                                    ''],
                                   ['run', ['sh', '-euc', 'install -d x\ncp a b'], {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                   ['verify', '/nonexistent/syrd400/boundary', '/nonexistent/syrd400/policy'],
                                   ['say']]},
 'installer:a long failure': {'answer': ['the privileged boundary could not be installed from '
                                         '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc (exit 3): '
                                         'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'],
                              'said': [],
                              'calls': [['roots', None],
                                        ['commands',
                                         '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                         '/nonexistent/syrd400/boundary',
                                         '/nonexistent/syrd400/policy',
                                         ''],
                                        ['run',
                                         ['sh', '-euc', 'install -d x\ncp a b'],
                                         {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'installer:an empty failure': {'answer': ['the privileged boundary could not be installed from '
                                           '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc (exit 1): no output'],
                                'said': [],
                                'calls': [['roots', None],
                                          ['commands',
                                           '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                           '/nonexistent/syrd400/boundary',
                                           '/nonexistent/syrd400/policy',
                                           ''],
                                          ['run',
                                           ['sh', '-euc', 'install -d x\ncp a b'],
                                           {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'installer:no returncode': {'answer': {'raised': 'AttributeError("\'types.SimpleNamespace\' object has no attribute \'returncode\'")'},
                             'said': [],
                             'calls': [['roots', None],
                                       ['commands',
                                        '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                        '/nonexistent/syrd400/boundary',
                                        '/nonexistent/syrd400/policy',
                                        ''],
                                       ['run',
                                        ['sh', '-euc', 'install -d x\ncp a b'],
                                        {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'installer:does not verify': {'answer': ['the polkit file is stale'],
                               'said': [],
                               'calls': [['roots', None],
                                         ['commands',
                                          '/nonexistent/syrd400/releases/cccccccccccccccccccccccccccccccccccccccc',
                                          '/nonexistent/syrd400/boundary',
                                          '/nonexistent/syrd400/policy',
                                          ''],
                                         ['run',
                                          ['sh', '-euc', 'install -d x\ncp a b'],
                                          {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                         ['verify', '/nonexistent/syrd400/boundary', '/nonexistent/syrd400/policy']]}}
REACHED: set[str] = set()
C = "c" * 40
REL = "/nonexistent/syrd400/releases/" + C
SANDBOX = Path("/nonexistent/syrd400/sandbox")
ANSWERS = {"ok": SimpleNamespace(returncode=0, stderr=""), "long": SimpleNamespace(returncode=3, stderr="  " + "e" * 600 + "\n"),
           "empty": SimpleNamespace(returncode=1, stderr=None), "no returncode": SimpleNamespace(stderr="x")}


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


def seam(name: str, function):
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


def norm(value: object) -> object:
    if value is subprocess.PIPE:
        return "PIPE"
    if isinstance(value, Path):
        return str(value)
    if callable(value):
        return "CALLABLE"
    return value


def no_process() -> patched:
    return patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen"))


def command(commit: str, *, euid: int = 0, activation: str = "activated", boundary=(), **kw) -> dict:
    log: list = []
    said: list = []

    def validate(values):
        log.append(["validate", values])
        if values["commit"] == "HEAD":
            raise privileged_actions.ArgumentError("commit must be a full content-addressed sha, not HEAD")
        return {"commit": values["commit"].strip()}

    result = {"activated": SimpleNamespace(commit=C, release_root=REL, rolled_back=False, describe=lambda: f"{C} is current"),
              "rolled back": SimpleNamespace(commit="b" * 40, release_root="/nonexistent/syrd400/releases/old", rolled_back=True, describe=lambda: "rolled back"),
              "no release root": SimpleNamespace(commit=C, release_root="", describe=lambda: "nothing to point at")}

    def act(kind):
        def run(*a, **k):
            log.append([kind, list(a), sorted(k)])
            if activation == "fails":
                raise sra.ActivationFailed("the release is not installed")
            return result[activation]
        return run

    with no_process(), patched(privileged_actions, action_for=lambda name: log.append(["action", name]) or SimpleNamespace(validate=validate)), \
            patched(sra, activate=act("activate"), rollback=act("rollback"),
                    recorded_rollback=lambda: log.append(["recorded rollback"]) or {"previous_target": "/nonexistent/syrd400/releases/prev"},
                    read_pointer=lambda: log.append(["read pointer"]) or "/nonexistent/syrd400/releases/now"), \
            patched(os, geteuid=lambda: log.append(["euid"]) or euid), \
            patched(t, install_host_privileged_boundary=seam("install_host_privileged_boundary",
                                                             lambda root, **k: log.append(["boundary", str(root), {a: norm(b) for a, b in k.items()}]) or list(boundary))):
        try:
            code = m.switchyard_install_shared_release_command(commit, print_func=lambda line: said.append(line) or log.append(["say"]), **kw)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- whatever a mutant raises is an answer to compare
            code = repr(exc)
    return json.loads(json.dumps({"code": code, "said": said, "calls": log}))


def installer(*, dry_run=False, answer="ok", verify=(), staging_root=None) -> dict:
    log: list = []
    said: list = []
    with no_process(), patched(privileged_install,
                               roots_for=lambda root: log.append(["roots", norm(root)]) or (Path("/nonexistent/syrd400/boundary"), Path("/nonexistent/syrd400/policy")),
                               install_commands=lambda release, *, root, policy_dir, sudo: log.append(["commands", release, norm(root), norm(policy_dir), sudo])
                               or ["# a comment", "", "install -d x", "  # indented", "cp a b"],
                               verify_installation=lambda root, policy: log.append(["verify", norm(root), norm(policy)]) or list(verify)):
        try:
            got = m.install_host_privileged_boundary(Path(REL), dry_run=dry_run, staging_root=staging_root,
                                                     runner=lambda argv, **k: log.append(["run", argv, {a: norm(b) for a, b in k.items()}]) or ANSWERS[answer],
                                                     print_func=lambda line: said.append(line) or log.append(["say"]))
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the answer, whatever it raises (the baseline raises for a result with no returncode)
            got = {"raised": repr(exc)}
    return json.loads(json.dumps({"answer": got, "said": said, "calls": log}))


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.host_boundary_install as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.host_boundary_install", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.host_boundary_install")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.host_boundary_install as m; "
                        "i = inspect.signature(m.install_host_privileged_boundary).parameters; c = inspect.signature(m.switchyard_install_shared_release_command).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "i['runner'].default is subprocess.run and i['print_func'].default is print and c['print_func'].default is print "
                        "and i['dry_run'].default is False and c['rollback'].default is False and c['boundary_root'].default is None, "
                        "not hasattr(m, 'privileged_install') and not hasattr(m, 'shared_release_activation'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "host_boundary_install.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        if expected:
            check(imports == ["from scripts import team_launcher as launcher", *LOCAL_IMPORTS[name]]
                  and [ast.unparse(s) for s in node.body[first:first + 2]] == imports,
                  f"{name}: the launcher imported first thing when it runs, then its own helpers as before: {imports}")
        else:
            check(imports == LOCAL_IMPORTS[name] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: reads nothing of the launcher's, and imports its helper first thing when it runs: {imports}")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import os", "import subprocess", "from pathlib import Path", "from typing import Any, Callable"]
          and not [n for n in tree.body if isinstance(n, ast.If)], f"only the standard library at the top: {top}")
    order = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(order == list(MOVED), f"the two in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_both_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.host_boundary_install"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"role_control_accounts", "running_launcher_release", "_build_switchyard_install_shared_release_parser",
                                        "_build_switchyard_privileged_action_parser", "switchyard_main"} <= defined | exported,
          "the launcher defines neither, and keeps its neighbours and the dispatch, its own or re-exported")
    main = next(n for n in tree.body if getattr(n, "name", None) == "switchyard_main")
    check([ast.unparse(x.func) for x in ast.walk(main) if isinstance(x, ast.Call) and ast.unparse(x.func).endswith("switchyard_install_shared_release_command")]
          == ["switchyard_install_shared_release_command"], "switchyard_main dispatches the command through its launcher global")
    pin = ast.parse((ROOT / "scripts" / "trusted_upgrade_release.py").read_text(encoding="utf-8"))
    reads = [ast.unparse(x) for x in ast.walk(pin) if isinstance(x, ast.Attribute) and x.attr in MOVED]
    check(reads == ["launcher.install_host_privileged_boundary"], f"the recovered-pin check installs the boundary through the launcher: {reads}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_code_print_and_call_of_the_command_is_the_baselines() -> None:
    cases = {
        "an invalid commit": ("HEAD", {}),
        "both arguments": (C, dict(rollback=True)),
        "not root": (C, dict(euid=1006)),
        "a dry run": (C, dict(dry_run=True)),
        "a dry run of a rollback": ("", dict(rollback=True, dry_run=True)),
        "activated, boundary installed": (" " + C + " ", dict(boundary_root=SANDBOX)),
        "activated, boundary fails": (C, dict(boundary=["the helper is missing", "polkit is stale"])),
        "rolled back": ("", dict(rollback=True, activation="rolled back")),
        "a rollback whose result is not marked rolled back": ("", dict(rollback=True)),
        "activation fails": (C, dict(activation="fails")),
        "rollback fails": ("", dict(rollback=True, activation="fails")),
        "no release root": (C, dict(activation="no release root")),
    }
    check(sorted(f"command:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("command:")), "every measured case is asserted")
    for label, (commit, kwargs) in cases.items():
        got = command(commit, **kwargs)
        check(got == GOLDEN[f"command:{label}"], f"{label}: the baseline's code, prints and calls, in order: {got}")


def test_every_answer_print_and_call_of_the_installer_is_the_baselines() -> None:
    cases = {
        "a dry run": dict(dry_run=True, staging_root=SANDBOX),
        "installed": dict(staging_root=SANDBOX),
        "a long failure": dict(answer="long"),
        "an empty failure": dict(answer="empty"),
        "no returncode": dict(answer="no returncode"),
        "does not verify": dict(verify=["the polkit file is stale"]),
    }
    check(sorted(f"installer:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("installer:")), "every measured case is asserted")
    for label, kwargs in cases.items():
        got = installer(**kwargs)
        check(got == GOLDEN[f"installer:{label}"], f"{label}: the baseline's answer, prints and calls, in order: {got}")


def test_the_boundary_comes_from_the_activated_release_and_only_after_it() -> None:
    got = command(C, boundary_root=SANDBOX)
    kinds = [c[0] for c in got["calls"]]
    boundary = next(c for c in got["calls"] if c[0] == "boundary")
    check(kinds.index("activate") < kinds.index("boundary") and boundary[1] == REL and boundary[2]["staging_root"] == str(SANDBOX),
          f"installed from the release just activated, into the given root, after activating it: {got['calls']}")
    for commit, kwargs in (("", dict(rollback=True, activation="rolled back")), (C, dict(activation="fails")), (C, dict(euid=1006)), (C, dict(dry_run=True))):
        got = command(commit, **kwargs)
        check(not any(c[0] == "boundary" for c in got["calls"]), f"no boundary for {kwargs}: {got['calls']}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_both_above_every_reader")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"host_boundary_install_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
