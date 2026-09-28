#!/usr/bin/env python3
"""SYRD-397: the trusted upgrade release and the recovered-pin check, against the launcher they came out of.

`_recovered_pin_behind_host`, `resolve_trusted_upgrade_release` and
`_selected_release_commit` moved unchanged into
`scripts/trusted_upgrade_release.py`, and the launcher re-exports them. This
pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the `runner` defaults are bound
  when each function is defined.
- **The file root is asked to trust is the launcher's.** The pin check
  resolves `team_launcher.__file__` when it runs -- never this module's own
  file -- for a checkout, for an installed release reached through
  `current/`, and for a launcher file changed after import; and an untrusted
  answer installs nothing.
- **Seams (rule 24):** the running release, the checkout, the shared install
  root and its default, the host-boundary installer, each sibling and the
  launcher's file are read through the launcher as often as before, so a
  patch or rebind there reaches each of them -- a bare sibling would bypass a
  suite's rebind of the resolver silently. The provisioning and publication
  helpers are still imported inside each function, where they were.
- **The behaviour is the baseline's:** every answer, print and call, in order,
  of the three -- `GOLDEN` below was produced by running the BASELINE
  launcher's own functions with the same stand-ins (`gold397.py`), not typed.

Nothing is executed: every path is `/nonexistent` or an owned temporary
directory, `os.geteuid`, the trust check, the resolver's helpers, the
installer and every runner are stand-ins, and the real `subprocess.run`/`Popen`
are refused.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import trusted_upgrade_release as m  # noqa: E402
from scripts.ticket_board import project_provision, publication_boundary  # noqa: E402

CHECKS = 0
MOVED = ("_recovered_pin_behind_host", "resolve_trusted_upgrade_release", "_selected_release_commit")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings and its own file included.
SEAMS = {
    '_recovered_pin_behind_host': {'DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT': 1, '__file__': 1, '_repo_root': 1, 'install_host_privileged_boundary': 1, 'resolve_trusted_upgrade_release': 1, 'running_launcher_release': 1, 'switchyard_shared_install_root': 1},
    'resolve_trusted_upgrade_release': {'_selected_release_commit': 1, 'switchyard_shared_install_root': 1},
}
#: Measured on the baseline launcher: the Switchyard imports each body makes when it runs, in order.
LOCAL_IMPORTS = {
    '_recovered_pin_behind_host': ['from scripts.ticket_board.project_provision import untrusted_root_executable_reasons'],
    'resolve_trusted_upgrade_release': ['from scripts.ticket_board.publication_boundary import TrustedRelease, materialize_trusted_release, read_release_marker, root_controlled_problems'],
    '_selected_release_commit': [],
}
#: The BASELINE's own behaviour for the cases below (`gold397.py`, run on the baseline launcher under the guard).
GOLDEN = {'pin:no running release': {'answer': None, 'said': [], 'calls': [['running']]},
 'pin:an unmarked running release': {'answer': None, 'said': [], 'calls': [['running']]},
 'pin:pinned level with the host': {'answer': None,
                                    'said': [],
                                    'calls': [['running'],
                                              ['resolve',
                                               '/nonexistent/syrd397/given',
                                               'cccccccccccccccccccccccccccccccccccccccc',
                                               {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}]]},
 'pin:nothing resolves, the ref is the pin': {'answer': 1,
                                              'said': ['switchyard: p397 is pinned to v1.2 from its last upgrade, and this host now runs '
                                                       'cccccccccccccccccccccccccccccccccccccccc. An upgrade given no release keeps the '
                                                       "tenant's pin, and the launcher running it is not that release, so nothing of "
                                                       "p397's was changed.",
                                                       "switchyard: this host's privileged boundary comes from "
                                                       'cccccccccccccccccccccccccccccccccccccccc; as root it is installed by `switchyard '
                                                       'privileged-action p397 upgrade-tenant`, which stops here in the same way',
                                                       'switchyard: to move p397 to cccccccccccccccccccccccccccccccccccccccc, preview it '
                                                       'and then apply it, pinned: `switchyard privileged-action p397 preview-upgrade '
                                                       'commit=cccccccccccccccccccccccccccccccccccccccc`, then `switchyard '
                                                       'privileged-action p397 upgrade-tenant-release '
                                                       'commit=cccccccccccccccccccccccccccccccccccccccc`'],
                                              'calls': [['running'],
                                                        ['repo root'],
                                                        ['resolve',
                                                         '/nonexistent/syrd397/check out',
                                                         'v1.2',
                                                         {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                                        ['say'],
                                                        ['euid'],
                                                        ['say'],
                                                        ['say']]},
 'pin:no pin at all': {'answer': None,
                       'said': [],
                       'calls': [['running'],
                                 ['repo root'],
                                 ['resolve',
                                  '/nonexistent/syrd397/check out',
                                  '',
                                  {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}]]},
 'pin:behind, not root': {'answer': 1,
                          'said': ['switchyard: p397 is pinned to bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb from its last upgrade, and this '
                                   "host now runs cccccccccccccccccccccccccccccccccccccccc. An upgrade given no release keeps the tenant's "
                                   "pin, and the launcher running it is not that release, so nothing of p397's was changed.",
                                   "switchyard: this host's privileged boundary comes from cccccccccccccccccccccccccccccccccccccccc; as "
                                   'root it is installed by `switchyard privileged-action p397 upgrade-tenant`, which stops here in the '
                                   'same way',
                                   'switchyard: to move p397 to cccccccccccccccccccccccccccccccccccccccc, preview it and then apply it, '
                                   'pinned: `switchyard privileged-action p397 preview-upgrade '
                                   'commit=cccccccccccccccccccccccccccccccccccccccc`, then `switchyard privileged-action p397 '
                                   'upgrade-tenant-release commit=cccccccccccccccccccccccccccccccccccccccc`'],
                          'calls': [['running'],
                                    ['repo root'],
                                    ['resolve',
                                     '/nonexistent/syrd397/check out',
                                     '',
                                     {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                    ['say'],
                                    ['euid'],
                                    ['say'],
                                    ['say']]},
 'pin:behind, a dry run': {'answer': 1,
                           'said': ['switchyard: p397 is pinned to bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb from its last upgrade, and '
                                    'this host now runs cccccccccccccccccccccccccccccccccccccccc. An upgrade given no release keeps the '
                                    "tenant's pin, and the launcher running it is not that release, so nothing of p397's was changed.",
                                    'switchyard: to move p397 to cccccccccccccccccccccccccccccccccccccccc, preview it and then apply it, '
                                    'pinned: `switchyard privileged-action p397 preview-upgrade '
                                    'commit=cccccccccccccccccccccccccccccccccccccccc`, then `switchyard privileged-action p397 '
                                    'upgrade-tenant-release commit=cccccccccccccccccccccccccccccccccccccccc`'],
                           'calls': [['running'],
                                     ['repo root'],
                                     ['resolve',
                                      '/nonexistent/syrd397/check out',
                                      'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                      {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                     ['say'],
                                     ['euid'],
                                     ['install',
                                      '/nonexistent/syrd397/releases/current',
                                      {'dry_run': True,
                                       'staging_root': '/nonexistent/syrd397/tooling',
                                       'runner': 'RUNNER',
                                       'print_func': 'PRINT'}],
                                     ['say']]},
 'pin:behind, root, untrusted': {'answer': 1,
                                 'said': ['switchyard: p397 is pinned to bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb from its last upgrade, '
                                          'and this host now runs cccccccccccccccccccccccccccccccccccccccc. An upgrade given no release '
                                          "keeps the tenant's pin, and the launcher running it is not that release, so nothing of p397's "
                                          'was changed.',
                                          "switchyard: this host's privileged boundary was not installed: this is not running from "
                                          'root-owned code (/x is writable by uid 1000)'],
                                 'calls': [['running'],
                                           ['repo root'],
                                           ['resolve',
                                            '/nonexistent/syrd397/check out',
                                            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                            {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                           ['say'],
                                           ['euid'],
                                           ['install root'],
                                           ['trust?', 0],
                                           ['say']]},
 'pin:behind, root, trusted, installed': {'answer': 1,
                                          'said': ['switchyard: p397 is pinned to bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb from its last '
                                                   'upgrade, and this host now runs cccccccccccccccccccccccccccccccccccccccc. An upgrade '
                                                   "given no release keeps the tenant's pin, and the launcher running it is not that "
                                                   "release, so nothing of p397's was changed.",
                                                   'switchyard: to move p397 to cccccccccccccccccccccccccccccccccccccccc, preview it and '
                                                   'then apply it, pinned: `switchyard privileged-action p397 preview-upgrade '
                                                   'commit=cccccccccccccccccccccccccccccccccccccccc`, then `switchyard privileged-action '
                                                   'p397 upgrade-tenant-release commit=cccccccccccccccccccccccccccccccccccccccc`'],
                                          'calls': [['running'],
                                                    ['repo root'],
                                                    ['resolve',
                                                     '/nonexistent/syrd397/check out',
                                                     'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                     {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                                    ['say'],
                                                    ['euid'],
                                                    ['install root'],
                                                    ['trust?', 0],
                                                    ['install',
                                                     '/nonexistent/syrd397/releases/current',
                                                     {'dry_run': False,
                                                      'staging_root': '/nonexistent/syrd397/tooling',
                                                      'runner': 'RUNNER',
                                                      'print_func': 'PRINT'}],
                                                    ['say']]},
 'pin:behind, root, install problems': {'answer': 1,
                                        'said': ['switchyard: p397 is pinned to bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb from its last '
                                                 'upgrade, and this host now runs cccccccccccccccccccccccccccccccccccccccc. An upgrade '
                                                 "given no release keeps the tenant's pin, and the launcher running it is not that "
                                                 "release, so nothing of p397's was changed.",
                                                 'switchyard: the helper is missing',
                                                 'switchyard: polkit is stale'],
                                        'calls': [['running'],
                                                  ['repo root'],
                                                  ['resolve',
                                                   '/nonexistent/syrd397/check out',
                                                   'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                   {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                                  ['say'],
                                                  ['euid'],
                                                  ['install root'],
                                                  ['trust?', 0],
                                                  ['install',
                                                   '/nonexistent/syrd397/releases/current',
                                                   {'dry_run': False,
                                                    'staging_root': '/nonexistent/syrd397/tooling',
                                                    'runner': 'RUNNER',
                                                    'print_func': 'PRINT'}],
                                                  ['say'],
                                                  ['say']]},
 'pin:behind, root, a redirected install root': {'answer': 1,
                                                 'said': ['switchyard: p397 is pinned to bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb from its '
                                                          'last upgrade, and this host now runs cccccccccccccccccccccccccccccccccccccccc. '
                                                          "An upgrade given no release keeps the tenant's pin, and the launcher running it "
                                                          "is not that release, so nothing of p397's was changed.",
                                                          'switchyard: to move p397 to cccccccccccccccccccccccccccccccccccccccc, preview '
                                                          'it and then apply it, pinned: `switchyard privileged-action p397 '
                                                          'preview-upgrade commit=cccccccccccccccccccccccccccccccccccccccc`, then '
                                                          '`switchyard privileged-action p397 upgrade-tenant-release '
                                                          'commit=cccccccccccccccccccccccccccccccccccccccc`'],
                                                 'calls': [['running'],
                                                           ['repo root'],
                                                           ['resolve',
                                                            '/nonexistent/syrd397/check out',
                                                            'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                            {'dry_run': True, 'ref_is_pinned': True, 'runner': 'RUNNER'}],
                                                           ['say'],
                                                           ['euid'],
                                                           ['install root'],
                                                           ['install',
                                                            '/nonexistent/syrd397/releases/current',
                                                            {'dry_run': False,
                                                             'staging_root': '/nonexistent/syrd397/tooling',
                                                             'runner': 'RUNNER',
                                                             'print_func': 'PRINT'}],
                                                           ['say']]},
 'resolve:a marked release root does not control': {'answer': '(None, '
                                                              "['/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb is "
                                                              "an installed release but is not root-controlled, so it will not be used', "
                                                              "'group-writable: /nonexistent'])",
                                                    'calls': [['marker',
                                                               '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
                                                              ['root-controlled?',
                                                               '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                               '/']]},
 'resolve:a marked release root does not control, under an overridden root': {'answer': '(None, '
                                                                                        "['/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "
                                                                                        'is an installed release but is not '
                                                                                        "root-controlled, so it will not be used', 'x'])",
                                                                              'calls': [['marker',
                                                                                         '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
                                                                                        ['root-controlled?',
                                                                                         '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                                                         '/nonexistent/syrd397/over']]},
 'resolve:the marked release asked for': {'answer': "(TrustedRelease(root=PosixPath('/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'), "
                                                    "commit='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', materialized=False), [])",
                                          'calls': [['marker', '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
                                                    ['root-controlled?',
                                                     '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                     '/']]},
 'resolve:a marked release, another exact commit pinned': {'answer': '(None, '
                                                                     "['/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "
                                                                     'is the installed release for '
                                                                     'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, but this upgrade is pinned '
                                                                     'at cccccccccccccccccccccccccccccccccccccccc. Nothing was staged: '
                                                                     'pointing at one release while pinning another installs the older '
                                                                     "tools and reports success.'])",
                                                           'calls': [['marker',
                                                                      '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
                                                                     ['root-controlled?',
                                                                      '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                                      '/']]},
 'resolve:a marked release, a name pinned': {'answer': '(None, ["/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb is '
                                                       'the installed release for bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, and this '
                                                       "upgrade is pinned at 'origin/main', which is a name rather than a commit. It is "
                                                       'not resolved here: the repositories that could resolve it are writable by the '
                                                       'account every role runs as. Pin the exact commit instead."])',
                                             'calls': [['marker', '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
                                                       ['root-controlled?',
                                                        '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                        '/']]},
 'resolve:a marked release, a default ref not pinned': {'answer': "(TrustedRelease(root=PosixPath('/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'), "
                                                                  "commit='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', materialized=False), "
                                                                  '[])',
                                                        'calls': [['marker',
                                                                   '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'],
                                                                  ['root-controlled?',
                                                                   '/nonexistent/syrd397/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                                                                   '/']]},
 'resolve:no marker, no commit': {'answer': "(None, ['could not resolve which commit /nonexistent/syrd397/check out is being installed "
                                            "from, so no release can be verified and nothing privileged can be staged from it'])",
                                  'calls': [['marker', '/nonexistent/syrd397/check out'],
                                            ['select', '/nonexistent/syrd397/check out', 'main', 'RUNNER']]},
 'resolve:no marker, materialized': {'answer': "('MATERIALIZED', [])",
                                     'calls': [['marker', '/nonexistent/syrd397/check out'],
                                               ['select', '/nonexistent/syrd397/check out', 'main', 'RUNNER'],
                                               ['install root'],
                                               ['materialize',
                                                'cccccccccccccccccccccccccccccccccccccccc',
                                                {'source_repo': '/nonexistent/syrd397/check out',
                                                 'install_root': '/nonexistent/syrd397/opt dir',
                                                 'trust_base': '/',
                                                 'runner': 'RUNNER',
                                                 'dry_run': True}]]},
 'resolve:no marker, materialized under an overridden root': {'answer': "('MATERIALIZED', [])",
                                                              'calls': [['marker', '/nonexistent/syrd397/check out'],
                                                                        ['select', '/nonexistent/syrd397/check out', '', 'RUNNER'],
                                                                        ['install root'],
                                                                        ['materialize',
                                                                         'cccccccccccccccccccccccccccccccccccccccc',
                                                                         {'source_repo': '/nonexistent/syrd397/check out',
                                                                          'install_root': '/nonexistent/syrd397/opt dir',
                                                                          'trust_base': '/nonexistent/syrd397/opt dir',
                                                                          'runner': 'RUNNER',
                                                                          'dry_run': True}]]},
 'resolve:no marker, a given install root': {'answer': "('MATERIALIZED', [])",
                                             'calls': [['marker', '/nonexistent/syrd397/check out'],
                                                       ['select',
                                                        '/nonexistent/syrd397/check out',
                                                        'cccccccccccccccccccccccccccccccccccccccc',
                                                        'RUNNER'],
                                                       ['materialize',
                                                        'cccccccccccccccccccccccccccccccccccccccc',
                                                        {'source_repo': '/nonexistent/syrd397/check out',
                                                         'install_root': '/nonexistent/syrd397/given root',
                                                         'trust_base': '/',
                                                         'runner': 'RUNNER',
                                                         'dry_run': True}]]},
 'select:a ref': {'answer': 'cccccccccccccccccccccccccccccccccccccccc',
                  'calls': [[['git', '-C', '/nonexistent/syrd397/check out', 'rev-parse', '--verify', 'main^{commit}'],
                             {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'select:no ref is HEAD': {'answer': 'cccccccccccccccccccccccccccccccccccccccc',
                           'calls': [[['git', '-C', '/nonexistent/syrd397/check out', 'rev-parse', '--verify', 'HEAD^{commit}'],
                                      {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'select:a failure': {'answer': '',
                      'calls': [[['git', '-C', '/nonexistent/syrd397/check out', 'rev-parse', '--verify', 'main^{commit}'],
                                 {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'select:no returncode': {'answer': '',
                          'calls': [[['git', '-C', '/nonexistent/syrd397/check out', 'rev-parse', '--verify', 'main^{commit}'],
                                     {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]},
 'select:no output': {'answer': '',
                      'calls': [[['git', '-C', '/nonexistent/syrd397/check out', 'rev-parse', '--verify', 'main^{commit}'],
                                 {'stdout': 'PIPE', 'stderr': 'PIPE', 'text': True}]]}}
#: Every seam a stand-in or a patch on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
CHECKOUT = Path("/nonexistent/syrd397/check out")
REDIRECTED = Path("/nonexistent/syrd397/opt dir")
CUR, OLD = "c" * 40, "b" * 40
RUNNER = object()


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
    result = python("import sys, scripts.trusted_upgrade_release as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.trusted_upgrade_release", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.trusted_upgrade_release")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.trusted_upgrade_release as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "all(inspect.signature(f).parameters['runner'].default is subprocess.run for f in (m.resolve_trusted_upgrade_release, m._selected_release_commit)), "
                        "not hasattr(m, 'running_launcher_release') and not hasattr(m, 'install_host_privileged_boundary') and m.__file__ != t.__file__)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.re is re and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "trusted_upgrade_release.py").read_text(encoding="utf-8"))
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
            check(imports[0] == "from scripts import team_launcher as launcher" and ast.unparse(node.body[first]) == imports[0]
                  and imports[1:] == LOCAL_IMPORTS[name],
                  f"{name}: the launcher imported first thing when it runs, then its own helpers as before: {imports}")
        else:
            check(imports == LOCAL_IMPORTS[name], f"{name}: reads nothing of the launcher's: {imports}")
        skip = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs), *f.args.defaults, *[d for d in f.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    check(not [x for x in ast.walk(tree) if isinstance(x, ast.Name) and x.id == "__file__"],
          "no bare __file__: the file root is asked to trust is never this module's")
    pin = next(n for n in tree.body if getattr(n, "name", None) == "_recovered_pin_behind_host")
    trust = next((x for x in ast.walk(pin) if isinstance(x, ast.If)
                  and ast.unparse(x.test) == "not dry_run and launcher.switchyard_shared_install_root() == launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT"), None)
    check(trust is not None and ast.unparse(trust.body[0]) == "from scripts.ticket_board.project_provision import untrusted_root_executable_reasons",
          "the trust helper is imported inside the real-install-root test, first thing, when it is reached")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import re", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(order == list(MOVED), f"the three in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_the_three_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.trusted_upgrade_release"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later
    # slice moves it on (SYRD-400) -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"install_host_privileged_boundary", "running_launcher_release"} <= defined | exported,
          "the launcher defines none of them, and keeps the installer and the running release between them, its own or re-exported")
    phases = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    reads = sorted(ast.unparse(x) for x in ast.walk(phases) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    check(reads == ["launcher._recovered_pin_behind_host", "launcher.resolve_trusted_upgrade_release", "launcher.resolve_trusted_upgrade_release"],
          f"the upgrade's phases still read them through the launcher: {reads}")


# --- the recovered pin ---------------------------------------------------------------------------------------------


MARKED = SimpleNamespace(marker_commit=CUR, root=Path("/nonexistent/syrd397/releases/current"))


def pin(*, running, resolved, deploy_ref, source_repo=None, dry_run=False, euid=1000, root=None, untrusted=(), install=(),
        launcher_file: str | None = None, asked: list | None = None) -> dict:
    log: list = []

    def resolve(source, ref, **kw):
        log.append(["resolve", str(source), ref, {k: ("RUNNER" if v is RUNNER else v) for k, v in kw.items()}])
        return (SimpleNamespace(commit=resolved) if resolved else None), ["ignored"]

    def installer(release_root, **kw):
        log.append(["install", str(release_root), {k: ("RUNNER" if v is RUNNER else "PRINT" if callable(v) else str(v) if isinstance(v, Path) else v)
                                                   for k, v in kw.items()}])
        return list(install)

    def trust(path, *, owner_uid):
        log.append(["trust?", owner_uid])
        if asked is not None:
            asked.append(path)
        return list(untrusted)

    said: list = []
    files = {} if launcher_file is None else {"__file__": launcher_file}
    with no_process(), patched(t, running_launcher_release=seam("running_launcher_release", lambda: log.append(["running"]) or running),
                               resolve_trusted_upgrade_release=seam("resolve_trusted_upgrade_release", resolve),
                               _repo_root=seam("_repo_root", lambda: log.append(["repo root"]) or CHECKOUT),
                               switchyard_shared_install_root=seam("switchyard_shared_install_root",
                                                                   lambda: log.append(["install root"]) or (root or t.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT)),
                               install_host_privileged_boundary=seam("install_host_privileged_boundary", installer), **files), \
            patched(os, geteuid=lambda: log.append(["euid"]) or euid), \
            patched(project_provision, untrusted_root_executable_reasons=trust):
        answer = judged(m._recovered_pin_behind_host, SimpleNamespace(project="p397"), source_repo=source_repo, deploy_ref=deploy_ref, dry_run=dry_run,
                        tooling_root=Path("/nonexistent/syrd397/tooling"), runner=RUNNER, print_func=lambda line: said.append(line) or log.append(["say"]))
    return {"answer": normal(answer), "said": said, "calls": normal(log)}


def test_every_answer_print_and_call_of_the_pin_check_is_the_baselines() -> None:
    cases = {
        "no running release": dict(running=None, resolved=OLD, deploy_ref=OLD),
        "an unmarked running release": dict(running=SimpleNamespace(marker_commit="", root=Path("/x")), resolved=OLD, deploy_ref=OLD),
        "pinned level with the host": dict(running=MARKED, resolved=CUR, deploy_ref=CUR, source_repo=Path("/nonexistent/syrd397/given")),
        "nothing resolves, the ref is the pin": dict(running=MARKED, resolved=None, deploy_ref="v1.2"),
        "no pin at all": dict(running=MARKED, resolved=None, deploy_ref=None),
        "behind, not root": dict(running=MARKED, resolved=OLD, deploy_ref=None),
        "behind, a dry run": dict(running=MARKED, resolved=OLD, deploy_ref=OLD, dry_run=True),
        "behind, root, untrusted": dict(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, untrusted=["/x is writable by uid 1000", "second"]),
        "behind, root, trusted, installed": dict(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0),
        "behind, root, install problems": dict(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, install=["the helper is missing", "polkit is stale"]),
        "behind, root, a redirected install root": dict(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, root=REDIRECTED),
    }
    check(sorted(f"pin:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("pin:")), "every measured case is asserted")
    for label, kwargs in cases.items():
        got = pin(**kwargs)
        check(got == GOLDEN[f"pin:{label}"], f"{label}: the baseline's answer, prints and calls, in order: {got}")


def test_the_install_root_default_is_the_launchers_when_the_check_runs() -> None:
    marker = Path("/nonexistent/syrd397/the-real-install-root")
    with patched(t, DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT=marker):
        got = pin(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, root=marker, untrusted=["/x"])
    check(["trust?", 0] in got["calls"] and not any(c[0] == "install" for c in got["calls"]),
          f"an install root equal to the patched default is the real one, and is asked about: {got['calls']}")
    REACHED.add("DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT")


def test_the_file_root_is_asked_to_trust_is_the_launchers_and_nothing_is_installed_before() -> None:
    asked: list = []
    got = pin(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, untrusted=["reason"], asked=asked)
    check(asked == [Path(os.path.realpath(t.__file__))] and asked[0].name == "team_launcher.py" and asked[0] != Path(os.path.realpath(m.__file__)),
          f"a checkout: the launcher's own file, resolved, not this module's: {asked}")
    check(got["answer"] == 1 and not any(c[0] == "install" for c in got["calls"]), f"untrusted: nothing installed: {got['calls']}")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        release = base / "releases" / CUR / "scripts"
        release.mkdir(parents=True)
        (release / "team_launcher.py").write_text("")
        (base / "current").symlink_to(base / "releases" / CUR)
        asked = []
        got = pin(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, launcher_file=str(base / "current" / "scripts" / "team_launcher.py"), asked=asked)
    trust_at = [c[0] for c in got["calls"]].index("trust?")
    install_at = [c[0] for c in got["calls"]].index("install")
    check(asked == [release / "team_launcher.py"] and trust_at < install_at,
          f"an installed release through `current`: the release's file, resolved, asked before anything is installed: {asked} {got['calls']}")
    with tempfile.TemporaryDirectory() as tmp:
        later = Path(tmp).resolve() / "later" / "team_launcher.py"
        asked = []
        pin(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0, launcher_file=str(later), asked=asked)
    check(asked == [later], f"a launcher file changed after import is the one asked about: {asked}")
    REACHED.add("__file__")


def test_the_trust_helper_is_imported_when_the_check_needs_it() -> None:
    got = pin(running=MARKED, resolved=OLD, deploy_ref=OLD, euid=0)
    check(["trust?", 0] in got["calls"], "the helper as project_provision holds it when the check runs")
    with patched(project_provision, untrusted_root_executable_reasons=refuse("the trust check on a dry run")):
        with no_process(), patched(t, running_launcher_release=lambda: MARKED, resolve_trusted_upgrade_release=lambda *a, **k: (SimpleNamespace(commit=OLD), []),
                                   install_host_privileged_boundary=lambda root, **kw: []), patched(os, geteuid=lambda: 0):
            answer = m._recovered_pin_behind_host(SimpleNamespace(project="p397"), source_repo=CHECKOUT, deploy_ref=OLD, dry_run=True,
                                                  tooling_root=None, runner=RUNNER, print_func=lambda line: None)
    check(answer == 1, "a dry run never asks root to trust anything")


# --- the resolver --------------------------------------------------------------------------------------------------


def resolve(*, marker, source, commit, pinned=False, problems=(), env=None, selected="", install_root=None, materialized=("MATERIALIZED", [])) -> dict:
    log: list = []
    environment = {k: v for k, v in os.environ.items() if k != "SWITCHYARD_SHARED_INSTALL_ROOT"}
    if env is not None:
        environment["SWITCHYARD_SHARED_INSTALL_ROOT"] = env
    saved = dict(os.environ)
    os.environ.clear(); os.environ.update(environment)
    try:
        with no_process(), patched(publication_boundary, read_release_marker=lambda p: log.append(["marker", str(p)]) or dict(marker),
                                   root_controlled_problems=lambda p, *, base: log.append(["root-controlled?", p, base]) or list(problems),
                                   materialize_trusted_release=lambda sel, **kw: log.append(
                                       ["materialize", sel, {k: ("RUNNER" if v is RUNNER else str(v) if isinstance(v, Path) else v) for k, v in kw.items()}]) or materialized), \
                patched(t, _selected_release_commit=seam("_selected_release_commit", lambda s, c, *, runner:
                                                         log.append(["select", str(s), c, "RUNNER" if runner is RUNNER else runner]) or selected),
                        switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: log.append(["install root"]) or REDIRECTED)):
            answer = judged(m.resolve_trusted_upgrade_release, source, commit, ref_is_pinned=pinned, install_root=install_root, dry_run=True, runner=RUNNER)
    finally:
        os.environ.clear(); os.environ.update(saved)
    return {"answer": repr(answer), "calls": normal(log)}


def test_every_answer_and_call_of_the_resolver_is_the_baselines() -> None:
    rel = Path("/nonexistent/syrd397/releases/" + OLD)
    cases = {
        "a marked release root does not control": dict(marker={"commit": OLD}, source=rel, commit=OLD, problems=["group-writable: /nonexistent"]),
        "a marked release root does not control, under an overridden root": dict(marker={"commit": OLD}, source=rel, commit=OLD, problems=["x"], env=" /nonexistent/syrd397/over "),
        "the marked release asked for": dict(marker={"commit": " " + OLD + " "}, source=rel, commit=OLD, pinned=True),
        "a marked release, another exact commit pinned": dict(marker={"commit": OLD}, source=rel, commit=CUR, pinned=True),
        "a marked release, a name pinned": dict(marker={"commit": OLD}, source=rel, commit="origin/main", pinned=True),
        "a marked release, a default ref not pinned": dict(marker={"commit": OLD}, source=rel, commit=CUR, pinned=False),
        "no marker, no commit": dict(marker={}, source=CHECKOUT, commit="main", selected=""),
        "no marker, materialized": dict(marker={"commit": ""}, source=CHECKOUT, commit="main", selected=CUR),
        "no marker, materialized under an overridden root": dict(marker={}, source=CHECKOUT, commit="", selected=CUR, env="/nonexistent/syrd397/over"),
        "no marker, a given install root": dict(marker={}, source=CHECKOUT, commit=CUR, selected=CUR, install_root=Path("/nonexistent/syrd397/given root")),
    }
    check(sorted(f"resolve:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("resolve:")), "every measured case is asserted")
    for label, kwargs in cases.items():
        got = resolve(**kwargs)
        check(got == GOLDEN[f"resolve:{label}"], f"{label}: the baseline's answer and calls, in order: {got}")


def test_the_selected_commit_is_asked_of_git_exactly_as_before() -> None:
    cases = {
        "a ref": (" main ", SimpleNamespace(returncode=0, stdout=f"  {CUR}\n")),
        "no ref is HEAD": ("", SimpleNamespace(returncode=0, stdout=CUR)),
        "a failure": ("main", SimpleNamespace(returncode=128, stdout=CUR)),
        "no returncode": ("main", SimpleNamespace(stdout=CUR)),
        "no output": ("main", SimpleNamespace(returncode=0, stdout=None)),
    }
    check(sorted(f"select:{k}" for k in cases) == sorted(k for k in GOLDEN if k.startswith("select:")), "every measured case is asserted")
    for label, (ref, answer) in cases.items():
        calls: list = []

        def runner(argv, **kw):
            calls.append([argv, {k: ("PIPE" if v is subprocess.PIPE else v) for k, v in kw.items()}])
            return answer

        with no_process():
            got = {"answer": judged(m._selected_release_commit, CHECKOUT, ref, runner=runner), "calls": calls}
        check(normal(got) == GOLDEN[f"select:{label}"], f"{label}: the baseline's git argv, capture and answer: {got}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in or patch on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_three_above_every_reader")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"trusted_upgrade_release_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
