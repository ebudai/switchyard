#!/usr/bin/env python3
"""SYRD-396: the stale-launcher check and the trusted bootstrap commands, against the launcher they came out of.

Seven definitions -- the two rollout labels, the installed-recorder lookup,
the recorded install and install-boundary commands, the bootstrap renderer
and the stale-launcher check -- moved unchanged into
`scripts/trusted_bootstrap.py`, and the launcher re-exports them. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the install label default is
  bound when the function is defined, to the object the launcher exports.
- **The file root is asked to trust is the launcher's.** The check resolves
  `team_launcher.__file__` when it runs -- never this module's own file --
  for a checkout and for an installed release reached through a symlink.
- **Seams (rule 24):** the shared install root and its default, the release
  this process runs from, every name defined here that another definition
  here reads, and the launcher's file are read through the launcher as often
  as before, so a patch there reaches each of them, which this test shows.
  The trust check's helper is still imported inside the check when it runs,
  and only after the root and install-root test.
- **The rendering is the baseline's, byte for byte:** every command, quote and
  chain order of the upgrade, first-install and guidance branches, the
  recorded install and boundary commands and their refusal, and every answer
  and call order of the stale-launcher check. `GOLDEN` below was produced by
  running the BASELINE launcher's own functions with the same stand-ins
  (`gold396.py`), not typed.

Nothing is executed: every path is `/nonexistent` or an owned temporary
directory, `os.geteuid` and the trust check are stand-ins, and the real
`subprocess.run`/`Popen` are refused.
"""

from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import trusted_bootstrap as m  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

CHECKS = 0
MOVED = ("INSTALL_ROLLOUT_LABEL", "INSTALL_BOUNDARY_ROLLOUT_LABEL", "installed_rollout_recorder", "recorded_install_command",
         "install_boundary_command", "trusted_bootstrap_commands", "stale_launcher_problems")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings and its own file included.
SEAMS = {
    'installed_rollout_recorder': {'switchyard_shared_install_root': 1},
    'install_boundary_command': {'INSTALL_BOUNDARY_ROLLOUT_LABEL': 1},
    'trusted_bootstrap_commands': {'install_boundary_command': 1, 'installed_rollout_recorder': 1, 'recorded_install_command': 1, 'switchyard_shared_install_root': 1},
    'stale_launcher_problems': {'DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT': 1, '__file__': 1, 'running_launcher_release': 1, 'switchyard_shared_install_root': 1, 'trusted_bootstrap_commands': 2},
}
#: The BASELINE's own output for the inputs below (`gold396.py`, run on the baseline launcher under the guard).
GOLDEN = {'first_install': ["env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' update-ref "
                   'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567 0123456789abcdef0123456789abcdef01234567',
                   "env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' bundle create '/nonexistent/syrd396/check "
                   "out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
                   'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
                   '# First install on this host: /nonexistent/syrd396/opt dir/current does not exist yet, so there is no',
                   '# root-owned switchyard-record-rollout to record the next four lines with. They run',
                   '# unrecorded -- deliberately, rather than executing the recorder out of an unverified',
                   '# checkout -- and the line after them records the boundary against what landed.',
                   "sudo install -d -m 0755 -o root -g root '/nonexistent/syrd396/opt dir/bootstrap'",
                   "sudo env GIT_NO_REPLACE_OBJECTS=1 git init -q '/nonexistent/syrd396/opt dir/bootstrap/src'",
                   "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' -c fetch.fsckObjects=true fetch "
                   "--no-tags '/nonexistent/syrd396/check out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
                   'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567:refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
                   "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' checkout -q --detach "
                   '0123456789abcdef0123456789abcdef01234567',
                   "sudo env GIT_NO_REPLACE_OBJECTS=1 SWITCHYARD_SOURCE_REPO='/nonexistent/syrd396/opt dir/bootstrap/src' "
                   "SWITCHYARD_SOURCE_REF=0123456789abcdef0123456789abcdef01234567 '/nonexistent/syrd396/opt "
                   "dir/current/scripts/install-switchyard' --apply",
                   "sudo '/nonexistent/syrd396/opt dir/current/scripts/switchyard-record-rollout' p396 --target-commit "
                   '0123456789abcdef0123456789abcdef01234567 --label install-boundary -- bash -c \'test "$(readlink -f '
                   '\'"\'"\'/nonexistent/syrd396/opt dir/current\'"\'"\')" = \'"\'"\'/nonexistent/syrd396/opt '
                   'dir/releases/0123456789abcdef0123456789abcdef01234567\'"\'"\'\'',
                   "sudo switchyard upgrade p396 --source-repo '/nonexistent/syrd396/opt "
                   "dir/releases/0123456789abcdef0123456789abcdef01234567' --deploy-ref 0123456789abcdef0123456789abcdef01234567 "
                   '--publish-remote git@example:o/p396.git'],
 'guidance_no_project': ["env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' update-ref "
                         'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567 0123456789abcdef0123456789abcdef01234567',
                         "env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' bundle create '/nonexistent/syrd396/check "
                         "out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
                         'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
                         "sudo install -d -m 0755 -o root -g root '/nonexistent/syrd396/opt dir/bootstrap'",
                         "sudo env GIT_NO_REPLACE_OBJECTS=1 git init -q '/nonexistent/syrd396/opt dir/bootstrap/src'",
                         "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' -c fetch.fsckObjects=true "
                         "fetch --no-tags '/nonexistent/syrd396/check "
                         "out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
                         'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567:refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
                         "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' checkout -q --detach "
                         '0123456789abcdef0123456789abcdef01234567',
                         "sudo env GIT_NO_REPLACE_OBJECTS=1 SWITCHYARD_SOURCE_REPO='/nonexistent/syrd396/opt dir/bootstrap/src' "
                         "SWITCHYARD_SOURCE_REF=0123456789abcdef0123456789abcdef01234567 '/nonexistent/syrd396/opt "
                         "dir/current/scripts/install-switchyard' --apply",
                         "sudo switchyard upgrade '<project>' --source-repo '/nonexistent/syrd396/opt "
                         "dir/releases/0123456789abcdef0123456789abcdef01234567' --deploy-ref 0123456789abcdef0123456789abcdef01234567 "
                         "--publish-remote '<url>'"],
 'guidance_no_commit': ["env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' update-ref refs/switchyard/bootstrap- ''",
                        "env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' bundle create '/nonexistent/syrd396/check "
                        "out/.switchyard-bootstrap-.bundle' refs/switchyard/bootstrap-",
                        "sudo install -d -m 0755 -o root -g root '/nonexistent/syrd396/opt dir/bootstrap'",
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 git init -q '/nonexistent/syrd396/opt dir/bootstrap/src'",
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' -c fetch.fsckObjects=true "
                        "fetch --no-tags '/nonexistent/syrd396/check out/.switchyard-bootstrap-.bundle' "
                        'refs/switchyard/bootstrap-:refs/switchyard/bootstrap-',
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' checkout -q --detach ''",
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 SWITCHYARD_SOURCE_REPO='/nonexistent/syrd396/opt dir/bootstrap/src' "
                        "SWITCHYARD_SOURCE_REF='' '/nonexistent/syrd396/opt dir/current/scripts/install-switchyard' --apply",
                        "sudo switchyard upgrade p396 --source-repo '/nonexistent/syrd396/opt dir/releases' --deploy-ref '' "
                        "--publish-remote '<url>'"],
 'upgrade': ["env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' update-ref "
             'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567 0123456789abcdef0123456789abcdef01234567',
             "env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' bundle create '/nonexistent/syrd396/check "
             "out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
             'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
             "sudo '/nonexistent/syrd396/opt dir/current/scripts/switchyard-record-rollout' p396 --target-commit "
             "0123456789abcdef0123456789abcdef01234567 --label install -- bash -c 'set -euo pipefail\n"
             'sudo install -d -m 0755 -o root -g root \'"\'"\'/nonexistent/syrd396/opt dir/bootstrap\'"\'"\'\n'
             'sudo env GIT_NO_REPLACE_OBJECTS=1 git init -q \'"\'"\'/nonexistent/syrd396/opt dir/bootstrap/src\'"\'"\'\n'
             'sudo env GIT_NO_REPLACE_OBJECTS=1 git -C \'"\'"\'/nonexistent/syrd396/opt dir/bootstrap/src\'"\'"\' -c '
             'fetch.fsckObjects=true fetch --no-tags \'"\'"\'/nonexistent/syrd396/check '
             'out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle\'"\'"\' '
             'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567:refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567\n'
             'sudo env GIT_NO_REPLACE_OBJECTS=1 git -C \'"\'"\'/nonexistent/syrd396/opt dir/bootstrap/src\'"\'"\' checkout -q --detach '
             '0123456789abcdef0123456789abcdef01234567\n'
             'sudo env GIT_NO_REPLACE_OBJECTS=1 SWITCHYARD_SOURCE_REPO=\'"\'"\'/nonexistent/syrd396/opt dir/bootstrap/src\'"\'"\' '
             'SWITCHYARD_SOURCE_REF=0123456789abcdef0123456789abcdef01234567 \'"\'"\'/nonexistent/syrd396/opt '
             'dir/current/scripts/install-switchyard\'"\'"\' --apply\'',
             "sudo switchyard upgrade p396 --source-repo '/nonexistent/syrd396/opt dir/releases/0123456789abcdef0123456789abcdef01234567' "
             '--deploy-ref 0123456789abcdef0123456789abcdef01234567 --publish-remote git@example:o/p396.git'],
 'upgrade_no_project': ["env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' update-ref "
                        'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567 0123456789abcdef0123456789abcdef01234567',
                        "env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/check out' bundle create '/nonexistent/syrd396/check "
                        "out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
                        'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
                        "sudo install -d -m 0755 -o root -g root '/nonexistent/syrd396/opt dir/bootstrap'",
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 git init -q '/nonexistent/syrd396/opt dir/bootstrap/src'",
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' -c fetch.fsckObjects=true "
                        "fetch --no-tags '/nonexistent/syrd396/check "
                        "out/.switchyard-bootstrap-0123456789abcdef0123456789abcdef01234567.bundle' "
                        'refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567:refs/switchyard/bootstrap-0123456789abcdef0123456789abcdef01234567',
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 git -C '/nonexistent/syrd396/opt dir/bootstrap/src' checkout -q --detach "
                        '0123456789abcdef0123456789abcdef01234567',
                        "sudo env GIT_NO_REPLACE_OBJECTS=1 SWITCHYARD_SOURCE_REPO='/nonexistent/syrd396/opt dir/bootstrap/src' "
                        "SWITCHYARD_SOURCE_REF=0123456789abcdef0123456789abcdef01234567 '/nonexistent/syrd396/opt "
                        "dir/current/scripts/install-switchyard' --apply",
                        "sudo switchyard upgrade '<project>' --source-repo '/nonexistent/syrd396/opt "
                        "dir/releases/0123456789abcdef0123456789abcdef01234567' --deploy-ref 0123456789abcdef0123456789abcdef01234567 "
                        "--publish-remote '<url>'"],
 'recorded_default': "sudo '/nonexistent/syrd396/opt dir/current/scripts/switchyard-record-rollout' p396 --target-commit "
                     "0123456789abcdef0123456789abcdef01234567 --label install -- bash -c 'set -euo pipefail\n"
                     'install -d \'"\'"\'/nonexistent/a b\'"\'"\'\n'
                     'echo "$x" && true\'',
 'recorded_no_label': "sudo '/nonexistent/syrd396/opt dir/current/scripts/switchyard-record-rollout' p396 --target-commit "
                      "0123456789abcdef0123456789abcdef01234567 -- bash -c 'set -euo pipefail\n"
                      'install -d \'"\'"\'/nonexistent/a b\'"\'"\'\n'
                      'echo "$x" && true\'',
 'recorded_no_commit': "sudo '/nonexistent/syrd396/opt dir/current/scripts/switchyard-record-rollout' p396 --label install -- bash -c 'set "
                       '-euo pipefail\n'
                       'install -d \'"\'"\'/nonexistent/a b\'"\'"\'\n'
                       'echo "$x" && true\'',
 'recorded_nested_refusal': 'refusing to wrap a chain that already records itself: nested attempts make the journal describe one install '
                            'twice',
 'boundary': "sudo '/nonexistent/syrd396/opt dir/current/scripts/switchyard-record-rollout' p396 --target-commit "
             '0123456789abcdef0123456789abcdef01234567 --label install-boundary -- bash -c \'test "$(readlink -f '
             '\'"\'"\'/nonexistent/syrd396/opt dir/current\'"\'"\')" = \'"\'"\'/nonexistent/syrd396/opt '
             'dir/releases/0123456789abcdef0123456789abcdef01234567\'"\'"\'\'',
 'labels': ['install', 'install-boundary'],
 'stale:checkout, not root': {'answer': [], 'calls': ['running', 'euid']},
 'stale:same release, not root': {'answer': [], 'calls': ['running', 'euid']},
 'stale:another release, not root': {'answer': ['this command is running from installed release ffffffffffffffffffffffffffffffffffffffff, '
                                                'but the upgrade selected 0123456789abcdef0123456789abcdef01234567. The public '
                                                '`switchyard` wrapper dispatches privileged commands to whatever is installed, so it is '
                                                'not running the release you pinned and nothing privileged was staged.',
                                                'install that release first, with root-owned code only:',
                                                "  L1 'q'",
                                                '  L2',
                                                'then re-run this command.'],
                                     'calls': ['running',
                                               'euid',
                                               ['bootstrap',
                                                '/nonexistent/syrd396/check out',
                                                '0123456789abcdef0123456789abcdef01234567',
                                                'p396',
                                                'git@example:o/p396.git']]},
 'stale:an unmarked release, not root': {'answer': ['this command is running from installed release an unmarked release at '
                                                    '/nonexistent/syrd396/releases/old, but the upgrade selected '
                                                    '0123456789abcdef0123456789abcdef01234567. The public `switchyard` wrapper dispatches '
                                                    'privileged commands to whatever is installed, so it is not running the release you '
                                                    'pinned and nothing privileged was staged.',
                                                    'install that release first, with root-owned code only:',
                                                    "  L1 'q'",
                                                    '  L2',
                                                    'then re-run this command.'],
                                         'calls': ['running',
                                                   'euid',
                                                   ['bootstrap',
                                                    '/nonexistent/syrd396/check out',
                                                    '0123456789abcdef0123456789abcdef01234567',
                                                    'p396',
                                                    'git@example:o/p396.git']]},
 'stale:root, a redirected install root': {'answer': ['this command is running from installed release '
                                                      'ffffffffffffffffffffffffffffffffffffffff, but the upgrade selected '
                                                      '0123456789abcdef0123456789abcdef01234567. The public `switchyard` wrapper '
                                                      'dispatches privileged commands to whatever is installed, so it is not running the '
                                                      'release you pinned and nothing privileged was staged.',
                                                      'install that release first, with root-owned code only:',
                                                      "  L1 'q'",
                                                      '  L2',
                                                      'then re-run this command.'],
                                           'calls': ['running',
                                                     'euid',
                                                     'install root',
                                                     ['bootstrap',
                                                      '/nonexistent/syrd396/check out',
                                                      '0123456789abcdef0123456789abcdef01234567',
                                                      'p396',
                                                      'git@example:o/p396.git']]},
 'stale:root, untrusted': {'answer': ['this command is running as root out of a path root does not control: /x is writable by uid 1000',
                                      'nothing privileged was staged. Install the release you want with root-owned code only, and run the '
                                      'upgrade from that:',
                                      "  L1 'q'",
                                      '  L2'],
                           'calls': ['running',
                                     'euid',
                                     'install root',
                                     ['trust?', 0],
                                     ['bootstrap',
                                      '/nonexistent/syrd396/check out',
                                      '0123456789abcdef0123456789abcdef01234567',
                                      'p396',
                                      'git@example:o/p396.git']]},
 'stale:root, trusted, the same release': {'answer': [], 'calls': ['running', 'euid', 'install root', ['trust?', 0]]},
 'stale:root, trusted, another release': {'answer': ['this command is running from installed release '
                                                     'ffffffffffffffffffffffffffffffffffffffff, but the upgrade selected '
                                                     '0123456789abcdef0123456789abcdef01234567. The public `switchyard` wrapper dispatches '
                                                     'privileged commands to whatever is installed, so it is not running the release you '
                                                     'pinned and nothing privileged was staged.',
                                                     'install that release first, with root-owned code only:',
                                                     "  L1 'q'",
                                                     '  L2',
                                                     'then re-run this command.'],
                                          'calls': ['running',
                                                    'euid',
                                                    'install root',
                                                    ['trust?', 0],
                                                    ['bootstrap',
                                                     '/nonexistent/syrd396/check out',
                                                     '0123456789abcdef0123456789abcdef01234567',
                                                     'p396',
                                                     'git@example:o/p396.git']]},
 'stale:root, trusted, a checkout': {'answer': [], 'calls': ['running', 'euid', 'install root', ['trust?', 0]]}}
#: Every seam a stand-in or a patch on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
ROOTDIR = Path("/nonexistent/syrd396/opt dir")
REPO = Path("/nonexistent/syrd396/check out")
COMMIT = "0123456789abcdef0123456789abcdef01234567"
RECORDER = ROOTDIR / "current" / "scripts" / "switchyard-record-rollout"
REMOTE = "git@example:o/p396.git"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is an answer to compare
        return exc


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


def normal(value: object) -> object:
    """As the golden file holds it: tuples are lists. Anything else (an exception a mutant raised) is its repr."""
    try:
        return json.loads(json.dumps(value))
    except TypeError:
        return repr(value)


def no_process() -> patched:
    return patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen"))


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.trusted_bootstrap as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_default() -> None:
    for order in (("scripts.trusted_bootstrap", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.trusted_bootstrap")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.trusted_bootstrap as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "inspect.signature(m.recorded_install_command).parameters['label'].default is t.INSTALL_ROLLOUT_LABEL, "
                        "not hasattr(m, 'running_launcher_release') and not hasattr(m, 'switchyard_shared_install_root') "
                        "and not hasattr(m, 'select') and m.__file__ != t.__file__)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.shlex is shlex and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "trusted_bootstrap.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name
                    or (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == name))
        if not isinstance(node, ast.FunctionDef):
            continue
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        launcher_imports = [i for i in imports if i == "from scripts import team_launcher as launcher"]
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(len(launcher_imports) == 1 and ast.unparse(node.body[first]) == launcher_imports[0],
                  f"{name}: the launcher imported once, first thing when it runs: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's and imports nothing: {imports}")
        skip = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs), *f.args.defaults, *[d for d in f.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    check(not [x for x in ast.walk(tree) if isinstance(x, ast.Name) and x.id == "__file__"],
          "no bare __file__: the file root is asked to trust is never this module's")
    stale = next(n for n in tree.body if getattr(n, "name", None) == "stale_launcher_problems")
    guard = next((s for s in stale.body if isinstance(s, ast.If) and ast.unparse(s.test)
                  == "os.geteuid() == 0 and launcher.switchyard_shared_install_root() == launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT"), None)
    check(guard is not None, "the trust check is asked only of root, under the real install root")
    local = [ast.unparse(x) for x in ast.walk(stale) if isinstance(x, ast.ImportFrom) and x.module != "scripts"]
    check(local == ["from scripts.ticket_board.project_provision import untrusted_root_executable_reasons"]
          and isinstance(guard.body[0], ast.ImportFrom) and ast.unparse(guard.body[0]) == local[0],
          f"the trust check's helper is imported inside the root test, when it is reached: {local}")
    label = next(n for n in tree.body if getattr(n, "name", None) == "recorded_install_command").args.kw_defaults[-1]
    check(ast.unparse(label) == "INSTALL_ROLLOUT_LABEL", f"the label default is the module's constant, bound when defined: {ast.unparse(label)}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import shlex", "from pathlib import Path", "from typing import TYPE_CHECKING"]
          and tc == ["if TYPE_CHECKING:\n    from typing import Sequence"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [getattr(n, "name", None) or ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(order == list(MOVED), f"the seven in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_the_seven_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.trusted_bootstrap"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the seven, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED) and {"running_launcher_release", "resolve_trusted_upgrade_release"} <= defined,
          "the launcher defines none of them, and keeps its neighbours")
    phases = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    reads = [x for x in ast.walk(phases) if isinstance(x, ast.Attribute) and x.attr in MOVED]
    check(len(reads) == 1 and ast.unparse(reads[0]) == "launcher.stale_launcher_problems",
          f"the upgrade's tooling phase still reads the check through the launcher: {[ast.unparse(x) for x in reads]}")


# --- the recorder and the recorded commands -----------------------------------------------------------------------


def test_only_an_installed_recorder_is_one_root_records_with() -> None:
    with tempfile.TemporaryDirectory() as tmp, no_process():
        root = Path(tmp) / "opt dir"
        with patched(t, switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: root)):
            check(m.installed_rollout_recorder() is None, "no installed release: nothing to record with")
            scripts = root / "current" / "scripts"
            (scripts / "switchyard-record-rollout").mkdir(parents=True)
            check(m.installed_rollout_recorder() is None, "a directory at the name is not a recorder")
            (scripts / "switchyard-record-rollout").rmdir()
            (scripts / "switchyard-record-rollout").write_text("#!/bin/sh\n")
            check(m.installed_rollout_recorder() == scripts / "switchyard-record-rollout",
                  "the installed release's own recorder, under the install root the launcher names")
        check(str(ROOT / "scripts" / "switchyard-record-rollout") not in str(m.installed_rollout_recorder()),
              "never the checkout's copy")


def test_the_recorded_commands_are_the_baselines() -> None:
    lines = ["install -d '/nonexistent/a b'", "echo \"$x\" && true"]
    check(m.recorded_install_command(lines, project="p396", commit=COMMIT, recorder=RECORDER) == GOLDEN["recorded_default"]
          and m.recorded_install_command(lines, project="p396", commit=COMMIT, recorder=RECORDER, label="") == GOLDEN["recorded_no_label"]
          and m.recorded_install_command(lines, project="p396", commit="", recorder=RECORDER) == GOLDEN["recorded_no_commit"],
          "the whole chain as one recorded attempt, labelled `install` by default, commit and label only when given")
    refused = judged(m.recorded_install_command, ["sudo /x/switchyard-record-rollout p -- bash -c true"], project="p396", commit=COMMIT, recorder=RECORDER)
    check(isinstance(refused, ValueError) and str(refused) == GOLDEN["recorded_nested_refusal"], f"a chain that records itself is refused: {refused!r}")
    with patched(t, INSTALL_BOUNDARY_ROLLOUT_LABEL="syrd396-boundary-label"):
        relabelled = m.install_boundary_command(project="p396", commit=COMMIT, recorder=RECORDER, release_root=ROOTDIR / "releases" / COMMIT,
                                                pointer=ROOTDIR / "current")
    check("--label syrd396-boundary-label --" in relabelled, "the boundary's label is read through the launcher when it runs")
    REACHED.add("INSTALL_BOUNDARY_ROLLOUT_LABEL")
    check(m.install_boundary_command(project="p396", commit=COMMIT, recorder=RECORDER, release_root=ROOTDIR / "releases" / COMMIT,
                                     pointer=ROOTDIR / "current") == GOLDEN["boundary"],
          "a verification of what landed, recorded by the installed recorder")
    check([m.INSTALL_ROLLOUT_LABEL, m.INSTALL_BOUNDARY_ROLLOUT_LABEL] == GOLDEN["labels"], "the journal's two labels")


# --- the bootstrap -------------------------------------------------------------------------------------------------


def bootstrap(recorder: Path | None, *args: object, **kwargs: object) -> list[str]:
    calls: list = []

    def recording(name, real):
        return seam(name, lambda *a, **k: calls.append(name) or real(*a, **k))

    with no_process(), patched(t, switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: ROOTDIR),
                               installed_rollout_recorder=seam("installed_rollout_recorder", lambda: recorder),
                               recorded_install_command=recording("recorded_install_command", m.recorded_install_command),
                               install_boundary_command=recording("install_boundary_command", m.install_boundary_command)):
        lines = m.trusted_bootstrap_commands(*args, **kwargs)
    bootstrap.calls = calls
    return lines


def test_an_upgrade_records_the_install_as_one_attempt() -> None:
    lines = bootstrap(RECORDER, REPO, COMMIT, project="p396", publish_remote=REMOTE)
    check(lines == GOLDEN["upgrade"] and bootstrap.calls == ["recorded_install_command"],
          f"the operator's bundle, the whole privileged chain recorded as `install` by root's recorder, then the reselection: {bootstrap.calls}")
    check(bootstrap(RECORDER, REPO, COMMIT) == GOLDEN["upgrade_no_project"] and bootstrap.calls == [],
          "without a project there is nothing to name in a record, whatever root holds")


def test_a_first_install_says_it_is_unrecorded_and_records_the_boundary_after() -> None:
    lines = bootstrap(None, REPO, COMMIT, project="p396", publish_remote=REMOTE)
    check(lines == GOLDEN["first_install"] and bootstrap.calls == ["install_boundary_command"],
          f"the gap stated, the privileged lines unrecorded, then the boundary recorded by the recorder they install: {bootstrap.calls}")


def test_guidance_names_no_host_or_release() -> None:
    check(bootstrap(None, REPO, COMMIT) == GOLDEN["guidance_no_project"] and bootstrap.calls == []
          and bootstrap(None, REPO, "", project="p396") == GOLDEN["guidance_no_commit"] and bootstrap.calls == [],
          "without a project or a commit, the lines as guidance, recorded by nothing")


# --- the stale-launcher check --------------------------------------------------------------------------------------


REL = SimpleNamespace(commit=COMMIT)
MARKED = SimpleNamespace(marker_commit="f" * 40, root=Path("/nonexistent/syrd396/releases/old"))
UNMARKED = SimpleNamespace(marker_commit="", root=Path("/nonexistent/syrd396/releases/old"))
SAME = SimpleNamespace(marker_commit=COMMIT, root=Path("/nonexistent/syrd396/releases/same"))


def stale(*, euid: int, root: Path, running: object, untrusted: list[str], launcher_file: str | None = None) -> tuple[object, list, list]:
    log: list = []
    asked: list = []
    files = {} if launcher_file is None else {"__file__": launcher_file}
    with no_process(), patched(t, running_launcher_release=seam("running_launcher_release", lambda: log.append("running") or running),
                               switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: log.append("install root") or root),
                               trusted_bootstrap_commands=seam("trusted_bootstrap_commands", lambda source_repo, commit, *, project="", publish_remote="":
                                                               log.append(("bootstrap", str(source_repo), commit, project, publish_remote)) or ["L1 'q'", "L2"]),
                               **files), \
            patched(os, geteuid=lambda: log.append("euid") or euid), \
            patched(project_provision, untrusted_root_executable_reasons=lambda path, *, owner_uid:
                    log.append(("trust?", owner_uid)) or asked.append(path) or list(untrusted)):
        answer = judged(m.stale_launcher_problems, REL, source_repo=REPO, project="p396", publish_remote=REMOTE)
    return answer, normal(log), asked


def test_every_answer_and_call_order_of_the_check_is_the_baselines() -> None:
    default = t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT
    cases = {
        "checkout, not root": dict(euid=1000, root=default, running=None, untrusted=["never asked"]),
        "same release, not root": dict(euid=1000, root=default, running=SAME, untrusted=["never asked"]),
        "another release, not root": dict(euid=1000, root=default, running=MARKED, untrusted=["never asked"]),
        "an unmarked release, not root": dict(euid=1000, root=default, running=UNMARKED, untrusted=["never asked"]),
        "root, a redirected install root": dict(euid=0, root=ROOTDIR, running=MARKED, untrusted=["never asked"]),
        "root, untrusted": dict(euid=0, root=default, running=MARKED, untrusted=["/x is writable by uid 1000", "second reason"]),
        "root, trusted, the same release": dict(euid=0, root=default, running=SAME, untrusted=[]),
        "root, trusted, another release": dict(euid=0, root=default, running=MARKED, untrusted=[]),
        "root, trusted, a checkout": dict(euid=0, root=default, running=None, untrusted=[]),
    }
    check(sorted(f"stale:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("stale:")), "every measured case is asserted")
    for label, kwargs in cases.items():
        answer, log, _asked = stale(**kwargs)
        want = GOLDEN[f"stale:{label}"]
        check(normal(answer) == want["answer"] and log == want["calls"],
              f"{label}: the baseline's answer, consulting the same things in the same order: {answer} {log}")


def test_the_install_root_default_is_the_launchers_when_the_check_runs() -> None:
    marker = Path("/nonexistent/syrd396/the-real-install-root")
    with patched(t, DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT=marker):
        answer, log, _asked = stale(euid=0, root=marker, running=None, untrusted=["/x is writable"])
    check(["trust?", 0] in log and normal(answer)[0].endswith("/x is writable"),
          f"the default is read through the launcher: an install root equal to the patched default is the real one: {log}")
    REACHED.add("DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT")


def test_the_file_root_is_asked_to_trust_is_the_launchers() -> None:
    _answer, log, asked = stale(euid=0, root=t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT, running=None, untrusted=["reason"])
    check(asked == [Path(os.path.realpath(t.__file__))] and asked[0].name == "team_launcher.py" and asked[0] != Path(os.path.realpath(m.__file__))
          and ["trust?", 0] in log,
          f"a checkout: the launcher's own file, resolved, against uid 0 -- not this module's: {asked}")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        release = base / "releases" / COMMIT / "scripts"
        release.mkdir(parents=True)
        (release / "team_launcher.py").write_text("")
        (base / "current").symlink_to(base / "releases" / COMMIT)
        installed = str(base / "current" / "scripts" / "team_launcher.py")
        _answer, log, asked = stale(euid=0, root=t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT, running=None, untrusted=[], launcher_file=installed)
    check(asked == [release / "team_launcher.py"] and ["trust?", 0] in log,
          f"an installed release reached through `current`: the release's file, resolved when the check runs: {asked}")
    REACHED.add("__file__")


def test_the_launcher_file_is_read_when_the_check_runs_not_when_this_module_loaded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        later = Path(tmp).resolve() / "later" / "team_launcher.py"
        _answer, _log, asked = stale(euid=0, root=t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT, running=None, untrusted=[], launcher_file=str(later))
    check(asked == [later], f"a launcher file changed after import is the one asked about: {asked}")


def test_the_trust_helper_is_imported_when_the_check_needs_it() -> None:
    marker: list = []
    with no_process(), patched(t, running_launcher_release=lambda: None,
                               switchyard_shared_install_root=lambda: t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT), \
            patched(os, geteuid=lambda: 0), \
            patched(project_provision, untrusted_root_executable_reasons=lambda path, *, owner_uid: marker.append(owner_uid) or []):
        answer = m.stale_launcher_problems(REL, source_repo=REPO)
    check(answer == [] and marker == [0], f"the helper as project_provision holds it when the check runs: {marker}")
    with no_process(), patched(t, running_launcher_release=lambda: None, switchyard_shared_install_root=lambda: t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT), \
            patched(os, geteuid=lambda: 1000), \
            patched(project_provision, untrusted_root_executable_reasons=refuse("the trust check for a caller who is not root")):
        check(m.stale_launcher_problems(REL, source_repo=REPO) == [], "not root: never asked")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in or patch on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_default",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_seven_above_every_reader")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"trusted_bootstrap_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
