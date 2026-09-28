#!/usr/bin/env python3
"""SYRD-399: the root-owned role tooling staging and refresh, against the launcher they came out of.

`refresh_staged_role_tooling`, `STAGED_TOOLING_OWNER_UID`,
`ensure_staged_role_tooling` and `_staged_tooling_dir` moved unchanged into
`scripts/staged_role_tooling.py`, and the launcher re-exports them. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the shipped defaults are the
  ones bound when each function is defined -- expect root
  (`STAGED_TOOLING_OWNER_UID`), ask the real `os.geteuid`, run with
  `subprocess.run`, say with `print` -- and the launcher's own launch checks,
  which stay, bind the same owner.
- **Seams (rule 24):** the launch-problem check, the provisioning renderer,
  verifier and staging path, and each sibling -- the gate's refresh and staged
  directory included -- are read through the launcher as often as before, so a
  patch there reaches each of them, which this test shows.
- **The behaviour is the baseline's:** a healthy bundle is left alone; a
  hostile one refused and never replaced; an incomplete one reported to
  anyone but root, and restaged by root from the release it was staged from,
  saying so first; a failed command or a repair that does not verify reported;
  and every command, capture, bound and message of the refresh -- `GOLDEN`
  below was produced by running the BASELINE launcher's own functions with the
  same stand-ins (`gold399.py`), not typed. That includes the baseline's
  `AttributeError` for a runner result with no `returncode`, preserved here and
  reported on the ticket.

Nothing is staged: every path is `/nonexistent`, the runner, the euid getter
and the provisioning helpers are stand-ins, the helpers' own module refuses,
and the real `subprocess.run`/`Popen` are refused.
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
from scripts import staged_role_tooling as m  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

CHECKS = 0
MOVED = ("refresh_staged_role_tooling", "STAGED_TOOLING_OWNER_UID", "ensure_staged_role_tooling", "_staged_tooling_dir")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'refresh_staged_role_tooling': {'_staged_tooling_dir': 2, 'role_tooling_staging_commands': 1, 'staged_role_tooling_problems': 1},
    'ensure_staged_role_tooling': {'_staged_tooling_dir': 1, 'refresh_staged_role_tooling': 1, 'staged_bundle_launch_problems': 1},
    '_staged_tooling_dir': {'role_tooling_staging_dir': 1},
}
#: The BASELINE's own behaviour for the cases below (`gold399.py`, run on the baseline launcher under the guard).
GOLDEN = {'ensure:healthy': {'answer': [],
                    'said': [],
                    'calls': [['launch problems?',
                               'p399',
                               {'release_root': '/nonexistent/syrd399/releases/abc',
                                'root': '/nonexistent/syrd399/staging root',
                                'install_root': '/nonexistent/syrd399/opt',
                                'expect_uid': 0}]]},
 'ensure:hostile': {'answer': ['b.sh is writable by uid 1006', "p399's staged tooling is not something a launch may replace"],
                    'said': [],
                    'calls': [['launch problems?',
                               'p399',
                               {'release_root': '', 'root': '/nonexistent/syrd399/staging root', 'install_root': None, 'expect_uid': 0}]]},
 'ensure:absent, not root': {'answer': ['a.sh is missing',
                                        'b.sh is missing',
                                        "p399's panes would open on tooling that is not staged, and repairing it is root's: `switchyard "
                                        "start p399` from the operator's own account repairs this before it crosses, and `sudo switchyard "
                                        'upgrade p399` restages it'],
                             'said': [],
                             'calls': [['launch problems?',
                                        'p399',
                                        {'release_root': '', 'root': None, 'install_root': None, 'expect_uid': 0}],
                                       ['euid']]},
 'ensure:absent, root, repaired': {'answer': [],
                                   'said': ["switchyard: p399's staged role tooling in /nonexistent/syrd399/staged/p399 is incomplete; "
                                            'restaging it from /nonexistent/syrd399/releases/pinned',
                                            'switchyard: staged p399 role tooling in /nonexistent/syrd399/staged/p399 from '
                                            '/nonexistent/syrd399/releases/pinned'],
                                   'calls': [['launch problems?',
                                              'p399',
                                              {'release_root': '/nonexistent/syrd399/releases/abc',
                                               'root': '/nonexistent/syrd399/staging root',
                                               'install_root': None,
                                               'expect_uid': 0}],
                                             ['euid'],
                                             ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                                             ['say'],
                                             ['render',
                                              'p399',
                                              '/nonexistent/syrd399/releases/pinned',
                                              '/nonexistent/syrd399/staging root'],
                                             ['run',
                                              ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                              {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                             ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                                             ['verify', 'p399', '/nonexistent/syrd399/releases/pinned', '/nonexistent/syrd399/staged/p399'],
                                             ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                                             ['say']]},
 'ensure:absent, root, the command fails': {'answer': ["could not stage p399's role tooling from /nonexistent/syrd399/releases/pinned "
                                                       '(exit 2): '
                                                       'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
                                                       "p399's panes would open on tooling that is not there: `switchyard start p399` from "
                                                       "the operator's own account repairs this before it crosses, and `sudo switchyard "
                                                       'upgrade p399` restages it'],
                                            'said': ["switchyard: p399's staged role tooling in /nonexistent/syrd399/staged/p399 is "
                                                     'incomplete; restaging it from /nonexistent/syrd399/releases/pinned'],
                                            'calls': [['launch problems?',
                                                       'p399',
                                                       {'release_root': '', 'root': None, 'install_root': None, 'expect_uid': 0}],
                                                      ['euid'],
                                                      ['dir', 'p399', None],
                                                      ['say'],
                                                      ['render', 'p399', '/nonexistent/syrd399/releases/pinned', None],
                                                      ['run',
                                                       ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                                       {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'ensure:absent, root, the repair does not verify': {'answer': ['a.sh still missing',
                                                                "p399's panes would open on tooling that is not there: `switchyard start "
                                                                "p399` from the operator's own account repairs this before it crosses, and "
                                                                '`sudo switchyard upgrade p399` restages it'],
                                                     'said': ["switchyard: p399's staged role tooling in /nonexistent/syrd399/staged/p399 "
                                                              'is incomplete; restaging it from /nonexistent/syrd399/releases/pinned'],
                                                     'calls': [['launch problems?',
                                                                'p399',
                                                                {'release_root': '',
                                                                 'root': '/nonexistent/syrd399/staging root',
                                                                 'install_root': None,
                                                                 'expect_uid': 0}],
                                                               ['euid'],
                                                               ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                                                               ['say'],
                                                               ['render',
                                                                'p399',
                                                                '/nonexistent/syrd399/releases/pinned',
                                                                '/nonexistent/syrd399/staging root'],
                                                               ['run',
                                                                ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                                                {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                                               ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                                                               ['verify',
                                                                'p399',
                                                                '/nonexistent/syrd399/releases/pinned',
                                                                '/nonexistent/syrd399/staged/p399']]},
 'ensure:absent, root, an explicit owner': {'answer': [],
                                            'said': ["switchyard: p399's staged role tooling in /nonexistent/syrd399/staged/p399 is "
                                                     'incomplete; restaging it from /nonexistent/syrd399/releases/pinned',
                                                     'switchyard: staged p399 role tooling in /nonexistent/syrd399/staged/p399 from '
                                                     '/nonexistent/syrd399/releases/pinned'],
                                            'calls': [['launch problems?',
                                                       'p399',
                                                       {'release_root': '',
                                                        'root': None,
                                                        'install_root': '/nonexistent/syrd399/opt',
                                                        'expect_uid': 1006}],
                                                      ['euid'],
                                                      ['dir', 'p399', None],
                                                      ['say'],
                                                      ['render', 'p399', '/nonexistent/syrd399/releases/pinned', None],
                                                      ['run',
                                                       ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                                       {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                                      ['dir', 'p399', None],
                                                      ['verify',
                                                       'p399',
                                                       '/nonexistent/syrd399/releases/pinned',
                                                       '/nonexistent/syrd399/staged/p399'],
                                                      ['dir', 'p399', None],
                                                      ['say']]},
 'refresh:staged': {'answer': [],
                    'said': ['switchyard: staged p399 role tooling in /nonexistent/syrd399/staged/p399 from '
                             '/nonexistent/syrd399/releases/abc'],
                    'calls': [['render', 'p399', '/nonexistent/syrd399/releases/abc', '/nonexistent/syrd399/staging root'],
                              ['run',
                               ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                               {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                              ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                              ['verify', 'p399', '/nonexistent/syrd399/releases/abc', '/nonexistent/syrd399/staged/p399'],
                              ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                              ['say']]},
 'refresh:no staging root': {'answer': [],
                             'said': ['switchyard: staged p399 role tooling in /nonexistent/syrd399/staged/p399 from '
                                      '/nonexistent/syrd399/releases/abc'],
                             'calls': [['render', 'p399', '/nonexistent/syrd399/releases/abc', None],
                                       ['run',
                                        ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                        {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                       ['dir', 'p399', None],
                                       ['verify', 'p399', '/nonexistent/syrd399/releases/abc', '/nonexistent/syrd399/staged/p399'],
                                       ['dir', 'p399', None],
                                       ['say']]},
 'refresh:a long failure': {'answer': ["could not stage p399's role tooling from /nonexistent/syrd399/releases/abc (exit 2): "
                                       'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'],
                            'said': [],
                            'calls': [['render', 'p399', '/nonexistent/syrd399/releases/abc', None],
                                      ['run',
                                       ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                       {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'refresh:an empty failure': {'answer': ["could not stage p399's role tooling from /nonexistent/syrd399/releases/abc (exit 1): no output"],
                              'said': [],
                              'calls': [['render', 'p399', '/nonexistent/syrd399/releases/abc', None],
                                        ['run',
                                         ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                         {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'refresh:no returncode': {'answer': {'raised': 'AttributeError("\'types.SimpleNamespace\' object has no attribute \'returncode\'")'},
                           'said': [],
                           'calls': [['render', 'p399', '/nonexistent/syrd399/releases/abc', None],
                                     ['run',
                                      ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                      {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'refresh:does not verify': {'answer': ['a.sh mode 0644, not 0755', 'b missing'],
                             'said': [],
                             'calls': [['render', 'p399', '/nonexistent/syrd399/releases/abc', '/nonexistent/syrd399/staging root'],
                                       ['run',
                                        ['sh', '-c', 'set -eu\ninstall -d a\ninstall -m 0755 b c'],
                                        {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}],
                                       ['dir', 'p399', '/nonexistent/syrd399/staging root'],
                                       ['verify', 'p399', '/nonexistent/syrd399/releases/abc', '/nonexistent/syrd399/staged/p399']]},
 'dir': ['/nonexistent/syrd399/default/p399', '/nonexistent/syrd399/nonexistent/syrd399/staging root/p399']}
#: Every seam a stand-in or a patch on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
CONFIG = SimpleNamespace(project="p399")
RELEASE = Path("/nonexistent/syrd399/releases/abc")
STAGING = Path("/nonexistent/syrd399/staging root")
INSTALL = Path("/nonexistent/syrd399/opt")
PINNED = "/nonexistent/syrd399/releases/pinned"
ANSWERS = {"ok": SimpleNamespace(returncode=0, stderr=""), "long": SimpleNamespace(returncode=2, stderr="  " + "e" * 500 + "\n"),
           "empty": SimpleNamespace(returncode=1, stderr=" \n"), "no returncode": SimpleNamespace(stderr="x")}


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


class contained:
    """No real process, and the provisioning helpers refuse where the launcher imports them from."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(project_provision, role_tooling_staging_commands=refuse("project_provision.role_tooling_staging_commands"),
                              staged_role_tooling_problems=refuse("project_provision.staged_role_tooling_problems"),
                              role_tooling_staging_dir=refuse("project_provision.role_tooling_staging_dir"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(kind: str, *, launch=((), (), ""), answer="ok", verify=(), euid=0, **kw) -> dict:
    log: list = []
    said: list = []
    runner = lambda argv, **k: log.append(["run", argv, {a: norm(b) for a, b in k.items()}]) or ANSWERS[answer]  # noqa: E731
    with contained(), patched(t, staged_bundle_launch_problems=seam("staged_bundle_launch_problems", lambda project, **k: log.append(
                                  ["launch problems?", project, {a: norm(b) for a, b in k.items()}]) or (list(launch[0]), list(launch[1]), launch[2])),
                              role_tooling_staging_commands=seam("role_tooling_staging_commands", lambda project, release, *, staging_root=None: log.append(
                                  ["render", project, release, norm(staging_root)]) or ["install -d a", "install -m 0755 b c"]),
                              staged_role_tooling_problems=seam("staged_role_tooling_problems", lambda project, release, *, staging_root: log.append(
                                  ["verify", project, release, norm(staging_root)]) or list(verify)),
                              role_tooling_staging_dir=seam("role_tooling_staging_dir", lambda project, *, root=None: log.append(
                                  ["dir", project, norm(root)]) or f"/nonexistent/syrd399/staged/{project}")):
        try:
            if kind == "ensure":
                got = m.ensure_staged_role_tooling(CONFIG, euid_getter=lambda: log.append(["euid"]) or euid, runner=runner,
                                                   print_func=lambda line: said.append(line) or log.append(["say"]), **kw)
            else:
                got = m.refresh_staged_role_tooling(CONFIG, runner=runner, print_func=lambda line: said.append(line) or log.append(["say"]), **kw)
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the answer, whatever it raises (the baseline raises for a result with no returncode)
            got = {"raised": repr(exc)}
    return json.loads(json.dumps({"answer": got, "said": said, "calls": log}))


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.staged_role_tooling as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.staged_role_tooling", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.staged_role_tooling")):
        result = python("import importlib, inspect, os, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.staged_role_tooling as m; "
                        "e = inspect.signature(m.ensure_staged_role_tooling).parameters; r = inspect.signature(m.refresh_staged_role_tooling).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "e['expect_uid'].default is m.STAGED_TOOLING_OWNER_UID == 0 and e['euid_getter'].default is os.geteuid "
                        "and e['runner'].default is r['runner'].default is subprocess.run and e['print_func'].default is r['print_func'].default is print, "
                        "inspect.signature(t.staged_bundle_launch_problems).parameters['expect_uid'].default is t.STAGED_TOOLING_OWNER_UID "
                        "and inspect.signature(t.ensure_staged_role_bundle_before_crossing).parameters['expect_uid'].default is t.STAGED_TOOLING_OWNER_UID "
                        "and not hasattr(m, 'staged_bundle_launch_problems') and not hasattr(m, 'ProjectConfig'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")
    parameters = inspect.signature(m.ensure_staged_role_tooling).parameters
    check(parameters["expect_uid"].default == 0 and parameters["euid_getter"].default is os.geteuid,
          "the shipped gate expects root and asks the real euid; the overrides are parameters, for fixtures")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "staged_role_tooling.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name
                    or (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == name))
        if not isinstance(node, ast.FunctionDef):
            check(ast.unparse(node) == "STAGED_TOOLING_OWNER_UID = 0", f"the owner is root: {ast.unparse(node)}")
            continue
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported once, first thing when it runs: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *node.args.defaults,
                                   *[d for d in node.args.kw_defaults if d]] if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    ensure = next(n for n in tree.body if getattr(n, "name", None) == "ensure_staged_role_tooling")
    check([ast.unparse(d) for d in ensure.args.kw_defaults if d is not None][3:5] == ["STAGED_TOOLING_OWNER_UID", "os.geteuid"],
          "the gate's owner and euid defaults are bound when it is defined, not read through the launcher")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [getattr(n, "name", None) or ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(order == list(MOVED), f"the four in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_the_four_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.staged_role_tooling"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the four, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition -- the launch checks that stay bind the owner as their default")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"running_launcher_release", "process_uid", "staged_bundle_launch_problems",
                                        "ensure_staged_role_bundle_before_crossing", "resume_tenant"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and callers, its own or re-exported")
    resume = next(n for n in tree.body if getattr(n, "name", None) == "resume_tenant")
    check([ast.unparse(x.func) for x in ast.walk(resume) if isinstance(x, ast.Call) and ast.unparse(x.func).endswith("ensure_staged_role_tooling")]
          == ["ensure_staged_role_tooling"], "resume_tenant still calls the gate by its launcher global")
    reads = {}
    for consumer in ("new_project_phases", "pane_hooks", "release_rollback", "upgrade_phases"):
        module = ast.parse((ROOT / "scripts" / f"{consumer}.py").read_text(encoding="utf-8"))
        reads[consumer] = sorted(ast.unparse(x) for x in ast.walk(module) if isinstance(x, ast.Attribute) and x.attr in MOVED)
        check(not [x for x in ast.walk(module) if isinstance(x, ast.ImportFrom) and x.module == "scripts.staged_role_tooling"],
              f"{consumer} does not bypass the launcher")
    check(reads == {"new_project_phases": ["launcher.ensure_staged_role_tooling"], "pane_hooks": ["launcher._staged_tooling_dir"],
                    "release_rollback": ["launcher._staged_tooling_dir"],
                    "upgrade_phases": ["launcher._staged_tooling_dir", "launcher._staged_tooling_dir", "launcher.refresh_staged_role_tooling"]},
          f"the production readers read them through the launcher: {reads}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_print_and_call_of_the_gate_is_the_baselines() -> None:
    cases = {
        "healthy": dict(launch=((), (), PINNED), release_root=RELEASE, staging_root=STAGING, install_root=INSTALL),
        "hostile": dict(launch=(["a.sh is missing"], ["b.sh is writable by uid 1006"], PINNED), staging_root=STAGING),
        "absent, not root": dict(launch=(["a.sh is missing", "b.sh is missing"], (), PINNED), euid=1006),
        "absent, root, repaired": dict(launch=(["a.sh is missing"], (), PINNED), release_root=RELEASE, staging_root=STAGING),
        "absent, root, the command fails": dict(launch=(["a.sh is missing"], (), PINNED), answer="long"),
        "absent, root, the repair does not verify": dict(launch=(["a.sh is missing"], (), PINNED), verify=["a.sh still missing"], staging_root=STAGING),
        "absent, root, an explicit owner": dict(launch=(["a.sh is missing"], (), PINNED), expect_uid=1006, install_root=INSTALL),
    }
    check(sorted(f"ensure:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("ensure:")), "every measured case is asserted")
    for label, kwargs in cases.items():
        got = run("ensure", **kwargs)
        check(got == GOLDEN[f"ensure:{label}"], f"{label}: the baseline's answer, prints and calls, in order: {got}")


def test_every_answer_print_and_call_of_the_refresh_is_the_baselines() -> None:
    cases = {
        "staged": dict(release_root=RELEASE, staging_root=STAGING),
        "no staging root": dict(release_root=RELEASE),
        "a long failure": dict(release_root=RELEASE, answer="long"),
        "an empty failure": dict(release_root=RELEASE, answer="empty"),
        "no returncode": dict(release_root=RELEASE, answer="no returncode"),
        "does not verify": dict(release_root=RELEASE, staging_root=STAGING, verify=["a.sh mode 0644, not 0755", "b missing"]),
    }
    check(sorted(f"refresh:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("refresh:")), "every measured case is asserted")
    for label, kwargs in cases.items():
        got = run("refresh", **kwargs)
        check(got == GOLDEN[f"refresh:{label}"], f"{label}: the baseline's answer, prints and calls, in order: {got}")


def test_the_staged_directory_is_the_launchers_staging_path() -> None:
    with contained(), patched(t, role_tooling_staging_dir=seam("role_tooling_staging_dir", lambda project, *, root=None: f"/nonexistent/syrd399/{root or 'default'}/{project}")):
        got = [str(m._staged_tooling_dir(CONFIG, None)), str(m._staged_tooling_dir(CONFIG, STAGING))]
    check(got == GOLDEN["dir"], f"the baseline's paths: {got}")


def test_the_gate_uses_the_launcher_facing_refresh_and_directory() -> None:
    log: list = []
    with contained(), patched(t, staged_bundle_launch_problems=lambda project, **k: (["a.sh is missing"], [], PINNED),
                              _staged_tooling_dir=seam("_staged_tooling_dir", lambda config, root: log.append(("dir", root)) or Path("/nonexistent/syrd399/patched")),
                              refresh_staged_role_tooling=seam("refresh_staged_role_tooling", lambda config, **kw: log.append(("refresh", kw["release_root"])) or [])):
        said: list = []
        answer = m.ensure_staged_role_tooling(CONFIG, staging_root=STAGING, euid_getter=lambda: 0, runner=refuse("the runner"), print_func=said.append)
    check(answer == [] and log == [("dir", STAGING), ("refresh", PINNED)] and "/nonexistent/syrd399/patched" in said[0],
          f"a patch on the launcher's refresh and staged directory reaches the gate: {log} {said}")


def test_nothing_runs_for_a_hostile_bundle_or_a_non_root_caller() -> None:
    for launch, euid in ((([], ["b.sh is writable"], PINNED), 0), ((["a.sh is missing"], [], PINNED), 1006)):
        with contained(), patched(t, staged_bundle_launch_problems=lambda project, **k: launch,
                                  refresh_staged_role_tooling=refuse("the refresh"), _staged_tooling_dir=refuse("the staged directory")):
            answer = m.ensure_staged_role_tooling(CONFIG, euid_getter=lambda: euid, runner=refuse("the runner"), print_func=refuse("a message"))
        check(answer and answer[-1].startswith("p399's"), f"reported, and nothing run or said: {answer}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_four_above_every_reader")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"staged_role_tooling_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
