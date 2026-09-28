#!/usr/bin/env python3
"""SYRD-401: a tenant board's authority unit installation, against the launcher it came out of.

`install_board_authority_files` and `install_board_authority` moved unchanged
into `scripts/board_authority_install.py`, and the launcher re-exports both.
This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the `print_func` defaults are
  bound when each function is defined, and `runner` stays required.
- **Seams (rule 24):** root's staged provisioning directory and its root, the
  current user, the units a tenant's authority installs, the board's
  activation and the files half the whole operation calls are read through
  the launcher as often as before, so a patch there reaches each of them,
  which this test shows.
- **The behaviour is the baseline's:** each unit from root's staged copy,
  `install -D -m 0644` with its ownership, output captured; a unit not staged
  or not installed is a problem and the rest carry on; no reload unless every
  unit installed; the reload's own failure; the listener left stopped; and
  the whole operation activating the board -- restarting it -- only after a
  clean install. `GOLDEN` below was produced by running the BASELINE
  launcher's own functions over the same owned staged files with the same
  stand-ins (`gold401.py`), not typed.

Nothing is installed or reloaded: the runner is a recorder, every destination
is `/nonexistent`, the staged units are an owned temporary directory, and the
real `subprocess.run`/`Popen` are refused.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import board_authority_install as m  # noqa: E402

CHECKS = 0
MOVED = ("install_board_authority_files", "install_board_authority")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, the sibling included.
SEAMS = {
    'install_board_authority_files': {'authority_unit_installs': 1, 'current_user_name': 1, 'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
    'install_board_authority': {'activate_board_authority': 1, 'install_board_authority_files': 1},
}
#: The BASELINE's own behaviour for the cases below (`gold401.py`, run on the baseline launcher under the guard).
GOLDEN = {'files:all installed and reloaded': {'answer': [],
                                      'said': ["switchyard: installed p401's generated units and reloaded systemd; p401-owner's listener "
                                               'stays stopped until the roles have verified'],
                                      'calls': [['provision root'],
                                                ['provision dir', 'p401', '<tmp>/provision'],
                                                ['units', 'p401', '/nonexistent/syrd401/p401.json'],
                                                ['run',
                                                 ['install',
                                                  '-D',
                                                  '-m',
                                                  '0644',
                                                  '-o',
                                                  'root',
                                                  '-g',
                                                  'root',
                                                  '<tmp>/provision/p401/p401-ticket-board.service',
                                                  '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                                 [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                ['run',
                                                 ['install',
                                                  '-D',
                                                  '-m',
                                                  '0644',
                                                  '-o',
                                                  'p401-owner',
                                                  '-g',
                                                  'p401-owner',
                                                  '<tmp>/provision/p401/p401-notify.service',
                                                  '/nonexistent/syrd401/user/p401-notify.service'],
                                                 [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                ['run',
                                                 ['install',
                                                  '-D',
                                                  '-m',
                                                  '0644',
                                                  '-o',
                                                  'root',
                                                  '-g',
                                                  'root',
                                                  '<tmp>/provision/p401/p401-canary.service',
                                                  '/nonexistent/syrd401/system/p401-canary.service'],
                                                 [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                ['run', ['systemctl', 'daemon-reload'], [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                ['say']]},
 'files:the current user when none is configured': {'answer': [],
                                                    'said': ["switchyard: installed p401's generated units and reloaded systemd; "
                                                             "p401-current's listener stays stopped until the roles have verified"],
                                                    'calls': [['provision root'],
                                                              ['provision dir', 'p401', '<tmp>/provision'],
                                                              ['current user'],
                                                              ['units', 'p401', None],
                                                              ['run',
                                                               ['install',
                                                                '-D',
                                                                '-m',
                                                                '0644',
                                                                '-o',
                                                                'root',
                                                                '-g',
                                                                'root',
                                                                '<tmp>/provision/p401/p401-ticket-board.service',
                                                                '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                              ['run',
                                                               ['install',
                                                                '-D',
                                                                '-m',
                                                                '0644',
                                                                '-o',
                                                                'p401-owner',
                                                                '-g',
                                                                'p401-owner',
                                                                '<tmp>/provision/p401/p401-notify.service',
                                                                '/nonexistent/syrd401/user/p401-notify.service'],
                                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                              ['run',
                                                               ['install',
                                                                '-D',
                                                                '-m',
                                                                '0644',
                                                                '-o',
                                                                'root',
                                                                '-g',
                                                                'root',
                                                                '<tmp>/provision/p401/p401-canary.service',
                                                                '/nonexistent/syrd401/system/p401-canary.service'],
                                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                              ['run',
                                                               ['systemctl', 'daemon-reload'],
                                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                              ['say']]},
 'files:one unit not staged': {'answer': ["p401-notify.service (the owner's listener) has not been generated under <tmp>/provision/p401"],
                               'said': [],
                               'calls': [['provision root'],
                                         ['provision dir', 'p401', '<tmp>/provision'],
                                         ['units', 'p401', None],
                                         ['run',
                                          ['install',
                                           '-D',
                                           '-m',
                                           '0644',
                                           '-o',
                                           'root',
                                           '-g',
                                           'root',
                                           '<tmp>/provision/p401/p401-ticket-board.service',
                                           '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                          [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                         ['run',
                                          ['install',
                                           '-D',
                                           '-m',
                                           '0644',
                                           '-o',
                                           'root',
                                           '-g',
                                           'root',
                                           '<tmp>/provision/p401/p401-canary.service',
                                           '/nonexistent/syrd401/system/p401-canary.service'],
                                          [['stderr', 'PIPE'], ['stdout', 'PIPE']]]]},
 'files:nothing staged': {'answer': ['p401-ticket-board.service has not been generated under <tmp>/provision/p401',
                                     "p401-notify.service (the owner's listener) has not been generated under <tmp>/provision/p401",
                                     'p401-canary.service has not been generated under <tmp>/provision/p401'],
                          'said': [],
                          'calls': [['provision root'], ['provision dir', 'p401', '<tmp>/provision'], ['units', 'p401', None]]},
 'files:one install fails': {'answer': ['could not install p401-canary.service (exit 1)'],
                             'said': [],
                             'calls': [['provision root'],
                                       ['provision dir', 'p401', '<tmp>/provision'],
                                       ['units', 'p401', None],
                                       ['run',
                                        ['install',
                                         '-D',
                                         '-m',
                                         '0644',
                                         '-o',
                                         'root',
                                         '-g',
                                         'root',
                                         '<tmp>/provision/p401/p401-ticket-board.service',
                                         '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                        [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                       ['run',
                                        ['install',
                                         '-D',
                                         '-m',
                                         '0644',
                                         '-o',
                                         'p401-owner',
                                         '-g',
                                         'p401-owner',
                                         '<tmp>/provision/p401/p401-notify.service',
                                         '/nonexistent/syrd401/user/p401-notify.service'],
                                        [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                       ['run',
                                        ['install',
                                         '-D',
                                         '-m',
                                         '0644',
                                         '-o',
                                         'root',
                                         '-g',
                                         'root',
                                         '<tmp>/provision/p401/p401-canary.service',
                                         '/nonexistent/syrd401/system/p401-canary.service'],
                                        [['stderr', 'PIPE'], ['stdout', 'PIPE']]]]},
 'files:two installs fail': {'answer': ['could not install p401-ticket-board.service (exit 1)',
                                        "could not install p401-notify.service (the owner's listener) (exit 1)"],
                             'said': [],
                             'calls': [['provision root'],
                                       ['provision dir', 'p401', '<tmp>/provision'],
                                       ['units', 'p401', None],
                                       ['run',
                                        ['install',
                                         '-D',
                                         '-m',
                                         '0644',
                                         '-o',
                                         'root',
                                         '-g',
                                         'root',
                                         '<tmp>/provision/p401/p401-ticket-board.service',
                                         '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                        [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                       ['run',
                                        ['install',
                                         '-D',
                                         '-m',
                                         '0644',
                                         '-o',
                                         'p401-owner',
                                         '-g',
                                         'p401-owner',
                                         '<tmp>/provision/p401/p401-notify.service',
                                         '/nonexistent/syrd401/user/p401-notify.service'],
                                        [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                       ['run',
                                        ['install',
                                         '-D',
                                         '-m',
                                         '0644',
                                         '-o',
                                         'root',
                                         '-g',
                                         'root',
                                         '<tmp>/provision/p401/p401-canary.service',
                                         '/nonexistent/syrd401/system/p401-canary.service'],
                                        [['stderr', 'PIPE'], ['stdout', 'PIPE']]]]},
 'files:one missing and one failing': {'answer': ["p401-notify.service (the owner's listener) has not been generated under "
                                                  '<tmp>/provision/p401',
                                                  'could not install p401-canary.service (exit 1)'],
                                       'said': [],
                                       'calls': [['provision root'],
                                                 ['provision dir', 'p401', '<tmp>/provision'],
                                                 ['units', 'p401', None],
                                                 ['run',
                                                  ['install',
                                                   '-D',
                                                   '-m',
                                                   '0644',
                                                   '-o',
                                                   'root',
                                                   '-g',
                                                   'root',
                                                   '<tmp>/provision/p401/p401-ticket-board.service',
                                                   '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                                  [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                 ['run',
                                                  ['install',
                                                   '-D',
                                                   '-m',
                                                   '0644',
                                                   '-o',
                                                   'root',
                                                   '-g',
                                                   'root',
                                                   '<tmp>/provision/p401/p401-canary.service',
                                                   '/nonexistent/syrd401/system/p401-canary.service'],
                                                  [['stderr', 'PIPE'], ['stdout', 'PIPE']]]]},
 'files:the reload fails': {'answer': ['systemctl daemon-reload failed'],
                            'said': [],
                            'calls': [['provision root'],
                                      ['provision dir', 'p401', '<tmp>/provision'],
                                      ['units', 'p401', None],
                                      ['run',
                                       ['install',
                                        '-D',
                                        '-m',
                                        '0644',
                                        '-o',
                                        'root',
                                        '-g',
                                        'root',
                                        '<tmp>/provision/p401/p401-ticket-board.service',
                                        '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                       [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                      ['run',
                                       ['install',
                                        '-D',
                                        '-m',
                                        '0644',
                                        '-o',
                                        'p401-owner',
                                        '-g',
                                        'p401-owner',
                                        '<tmp>/provision/p401/p401-notify.service',
                                        '/nonexistent/syrd401/user/p401-notify.service'],
                                       [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                      ['run',
                                       ['install',
                                        '-D',
                                        '-m',
                                        '0644',
                                        '-o',
                                        'root',
                                        '-g',
                                        'root',
                                        '<tmp>/provision/p401/p401-canary.service',
                                        '/nonexistent/syrd401/system/p401-canary.service'],
                                       [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                      ['run', ['systemctl', 'daemon-reload'], [['stderr', 'PIPE'], ['stdout', 'PIPE']]]]},
 'full:the files fail: nothing activated': {'answer': ["could not install p401-notify.service (the owner's listener) (exit 1)"],
                                            'said': [],
                                            'calls': [['provision root'],
                                                      ['provision dir', 'p401', '<tmp>/provision'],
                                                      ['units', 'p401', None],
                                                      ['run',
                                                       ['install',
                                                        '-D',
                                                        '-m',
                                                        '0644',
                                                        '-o',
                                                        'root',
                                                        '-g',
                                                        'root',
                                                        '<tmp>/provision/p401/p401-ticket-board.service',
                                                        '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                                       [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                      ['run',
                                                       ['install',
                                                        '-D',
                                                        '-m',
                                                        '0644',
                                                        '-o',
                                                        'p401-owner',
                                                        '-g',
                                                        'p401-owner',
                                                        '<tmp>/provision/p401/p401-notify.service',
                                                        '/nonexistent/syrd401/user/p401-notify.service'],
                                                       [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                                      ['run',
                                                       ['install',
                                                        '-D',
                                                        '-m',
                                                        '0644',
                                                        '-o',
                                                        'root',
                                                        '-g',
                                                        'root',
                                                        '<tmp>/provision/p401/p401-canary.service',
                                                        '/nonexistent/syrd401/system/p401-canary.service'],
                                                       [['stderr', 'PIPE'], ['stdout', 'PIPE']]]]},
 'full:installed, then activated': {'answer': [],
                                    'said': ["switchyard: installed p401's generated units and reloaded systemd; p401-owner's listener "
                                             'stays stopped until the roles have verified'],
                                    'calls': [['provision root'],
                                              ['provision dir', 'p401', '<tmp>/provision'],
                                              ['units', 'p401', None],
                                              ['run',
                                               ['install',
                                                '-D',
                                                '-m',
                                                '0644',
                                                '-o',
                                                'root',
                                                '-g',
                                                'root',
                                                '<tmp>/provision/p401/p401-ticket-board.service',
                                                '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['run',
                                               ['install',
                                                '-D',
                                                '-m',
                                                '0644',
                                                '-o',
                                                'p401-owner',
                                                '-g',
                                                'p401-owner',
                                                '<tmp>/provision/p401/p401-notify.service',
                                                '/nonexistent/syrd401/user/p401-notify.service'],
                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['run',
                                               ['install',
                                                '-D',
                                                '-m',
                                                '0644',
                                                '-o',
                                                'root',
                                                '-g',
                                                'root',
                                                '<tmp>/provision/p401/p401-canary.service',
                                                '/nonexistent/syrd401/system/p401-canary.service'],
                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['run', ['systemctl', 'daemon-reload'], [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['say'],
                                              ['activate', 'p401', True]]},
 "full:activation's own problems": {'answer': ['the board did not come back healthy'],
                                    'said': ["switchyard: installed p401's generated units and reloaded systemd; p401-owner's listener "
                                             'stays stopped until the roles have verified'],
                                    'calls': [['provision root'],
                                              ['provision dir', 'p401', '<tmp>/provision'],
                                              ['units', 'p401', None],
                                              ['run',
                                               ['install',
                                                '-D',
                                                '-m',
                                                '0644',
                                                '-o',
                                                'root',
                                                '-g',
                                                'root',
                                                '<tmp>/provision/p401/p401-ticket-board.service',
                                                '/nonexistent/syrd401/system/p401-ticket-board.service'],
                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['run',
                                               ['install',
                                                '-D',
                                                '-m',
                                                '0644',
                                                '-o',
                                                'p401-owner',
                                                '-g',
                                                'p401-owner',
                                                '<tmp>/provision/p401/p401-notify.service',
                                                '/nonexistent/syrd401/user/p401-notify.service'],
                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['run',
                                               ['install',
                                                '-D',
                                                '-m',
                                                '0644',
                                                '-o',
                                                'root',
                                                '-g',
                                                'root',
                                                '<tmp>/provision/p401/p401-canary.service',
                                                '/nonexistent/syrd401/system/p401-canary.service'],
                                               [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['run', ['systemctl', 'daemon-reload'], [['stderr', 'PIPE'], ['stdout', 'PIPE']]],
                                              ['say'],
                                              ['activate', 'p401', True]]},
 'full:nothing staged: nothing activated': {'answer': ['p401-ticket-board.service has not been generated under <tmp>/provision/p401',
                                                       "p401-notify.service (the owner's listener) has not been generated under "
                                                       '<tmp>/provision/p401',
                                                       'p401-canary.service has not been generated under <tmp>/provision/p401'],
                                            'said': [],
                                            'calls': [['provision root'],
                                                      ['provision dir', 'p401', '<tmp>/provision'],
                                                      ['units', 'p401', None]]}}
REACHED: set[str] = set()
UNITS = [("p401-ticket-board.service", "/nonexistent/syrd401/system/p401-ticket-board.service", ["-o", "root", "-g", "root"]),
         ("p401-notify.service (the owner's listener)", "/nonexistent/syrd401/user/p401-notify.service", ["-o", "p401-owner", "-g", "p401-owner"]),
         ("p401-canary.service", "/nonexistent/syrd401/system/p401-canary.service", ["-o", "root", "-g", "root"])]
ALL = ("p401-ticket-board.service", "p401-notify.service", "p401-canary.service")


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


def run(kind: str, *, staged=ALL, failing=(), reload_rc=0, owner="p401-owner", activation=(), config_path=None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        provision = Path(tmp) / "provision"
        (provision / "p401").mkdir(parents=True)
        for name in staged:
            (provision / "p401" / name).write_text("[Unit]\n")
        log: list = []
        said: list = []
        n = lambda v: (str(v).replace(tmp, "<tmp>") if isinstance(v, (str, Path)) else v)  # noqa: E731

        def runner(argv, **k):
            argv = [str(a) for a in argv]
            log.append(["run", [n(a) for a in argv], sorted((a, "PIPE" if b is subprocess.PIPE else b) for a, b in k.items())])
            if argv[1:2] == ["daemon-reload"]:
                return SimpleNamespace(returncode=reload_rc)
            return SimpleNamespace(returncode=1 if any(f in argv[-1] for f in failing) else 0)

        with patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")), \
                patched(t, switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: log.append(["provision root"]) or provision),
                        privileged_provision_dir=seam("privileged_provision_dir", lambda project, *, root: log.append(["provision dir", project, n(root)]) or (Path(root) / project)),
                        current_user_name=seam("current_user_name", lambda: log.append(["current user"]) or "p401-current"),
                        authority_unit_installs=seam("authority_unit_installs", lambda config, *, config_path=None: log.append(["units", config.project, n(config_path)])
                                                     or [(u, Path(d), list(o)) for u, d, o in UNITS]),
                        activate_board_authority=seam("activate_board_authority", lambda config, *, runner, restart, print_func:
                                                      log.append(["activate", config.project, restart]) or list(activation))):
            config = SimpleNamespace(project="p401", run_as_user=owner)
            fn = m.install_board_authority_files if kind == "files" else m.install_board_authority
            try:
                got = [n(p) for p in fn(config, runner=runner, config_path=config_path, print_func=lambda line: said.append(n(line)) or log.append(["say"]))]
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- whatever a mutant raises is an answer to compare
                got = repr(exc)
        return json.loads(json.dumps({"answer": got, "said": said, "calls": log}))


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.board_authority_install as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.board_authority_install", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.board_authority_install")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.board_authority_install as m; "
                        "ps = [inspect.signature(f).parameters for f in (m.install_board_authority_files, m.install_board_authority)]; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "all(p['print_func'].default is print and p['runner'].default is inspect.Parameter.empty and p['config_path'].default is None for p in ps), "
                        "not hasattr(m, 'activate_board_authority') and not hasattr(m, 'ProjectConfig'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "board_authority_install.py").read_text(encoding="utf-8"))
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
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported once, first thing when it runs: {imports}")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from pathlib import Path", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(order == list(MOVED), f"the two in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_both_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.board_authority_install"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"capture_installed_units", "_finish_upgrade_preview"} <= defined | exported,
          "the launcher defines neither, and keeps its neighbours, its own or re-exported")
    cutover = ast.parse((ROOT / "scripts" / "role_identity_cutover.py").read_text(encoding="utf-8"))
    reads = [ast.unparse(x) for x in ast.walk(cutover) if isinstance(x, ast.Attribute) and x.attr in MOVED]
    check(reads == ["team_launcher.install_board_authority_files"], f"the identity cutover installs the units through the launcher: {reads}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_print_and_call_is_the_baselines() -> None:
    cases = {
        "files:all installed and reloaded": ("files", dict(config_path=Path("/nonexistent/syrd401/p401.json"))),
        "files:the current user when none is configured": ("files", dict(owner="")),
        "files:one unit not staged": ("files", dict(staged=("p401-ticket-board.service", "p401-canary.service"))),
        "files:nothing staged": ("files", dict(staged=())),
        "files:one install fails": ("files", dict(failing=("p401-canary.service",))),
        "files:two installs fail": ("files", dict(failing=("p401-ticket-board.service", "p401-notify.service"))),
        "files:one missing and one failing": ("files", dict(staged=("p401-ticket-board.service", "p401-canary.service"), failing=("p401-canary.service",))),
        "files:the reload fails": ("files", dict(reload_rc=1)),
        "full:the files fail: nothing activated": ("full", dict(failing=("p401-notify.service",))),
        "full:installed, then activated": ("full", {}),
        "full:activation's own problems": ("full", dict(activation=["the board did not come back healthy"])),
        "full:nothing staged: nothing activated": ("full", dict(staged=())),
    }
    check(sorted(cases) == sorted(GOLDEN), "every measured case is asserted")
    for label, (kind, kwargs) in cases.items():
        got = run(kind, **kwargs)
        check(got == GOLDEN[label], f"{label}: the baseline's answer, prints and calls, in order: {got}")


def test_no_reload_or_activation_before_every_unit_installed() -> None:
    for kwargs in (dict(failing=("p401-canary.service",)), dict(staged=("p401-ticket-board.service",)), dict(reload_rc=1)):
        got = run("full", **kwargs)
        calls = got["calls"]
        installs = [i for i, c in enumerate(calls) if c[0] == "run" and c[1][0] == "install"]
        reloads = [i for i, c in enumerate(calls) if c[0] == "run" and c[1][1:2] == ["daemon-reload"]]
        check(not any(c[0] == "activate" for c in calls) and (not reloads or (reloads[-1] > max(installs) and not kwargs.get("failing") and kwargs.get("reload_rc"))),
              f"{kwargs}: no activation, and a reload only after every install: {calls}")
    got = run("full")
    kinds = [c[0] if c[0] != "run" else (c[1][1] if c[1][1:2] == ["daemon-reload"] else c[1][0]) for c in got["calls"]]
    check(kinds.index("daemon-reload") > max(i for i, k in enumerate(kinds) if k == "install") and kinds[-1] == "activate"
          and ["activate", "p401", True] in got["calls"], f"install every unit, then reload, then activate with a restart: {kinds}")
    sources = [c[1][-2] for c in got["calls"] if c[0] == "run" and c[1][0] == "install"]
    check(sources == ["<tmp>/provision/p401/p401-ticket-board.service", "<tmp>/provision/p401/p401-notify.service", "<tmp>/provision/p401/p401-canary.service"],
          f"each unit from root's staged provisioning directory: {sources}")


def test_a_patch_of_the_launchers_files_half_reaches_the_whole_operation() -> None:
    asked: list = []
    with patched(t, install_board_authority_files=seam("install_board_authority_files", lambda config, **kw: asked.append(sorted(kw)) or ["from the patch"]),
                 activate_board_authority=refuse("the activation")):
        try:
            answer = m.install_board_authority(SimpleNamespace(project="p401"), runner=refuse("the runner"), print_func=refuse("a message"))
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- a body that went past the launcher answers with whatever it raised
            answer = repr(exc)
    check(answer == ["from the patch"] and asked == [["config_path", "print_func", "runner"]], f"the launcher's files half, and nothing activated: {answer} {asked}")


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
    print(f"board_authority_install_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
