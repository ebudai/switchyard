#!/usr/bin/env python3
"""SYRD-442: process inspection and the role runners, against the launcher they came out of.

`role_process_runner_for`, `_process_snapshot`, `process_tree_command_names`,
`_owner_process_runner`, `_list_process_command_lines`, `_role_has_pane_process`
and `process_uid` -- reading the host's processes and the runners that read
them as a role or the owner -- moved unchanged into
`scripts/process_inspection.py`; the launcher re-exports all seven. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; both
  runner defaults are `subprocess.run` itself.
- **Seams (rule 24):** everything they read when they run -- each other, the
  current user, the role's account, the command-name and failure-reason
  helpers, the kernel uid reader and `PROC_ROOT` -- is read through the
  launcher as often as before, so a patch there reaches it: every case below
  records or stands them in there, and rebinds each one there to see the
  effect.
- **Readers:** `_running_project_roles` calls `role_process_runner_for` by the
  launcher's name, and eight production modules read the seven through the
  launcher when they run, as often as before.
- **The behaviour is the baseline's:** a `ps` snapshot (unparsable, short and
  non-numeric lines, a failed or empty `ps`); a pane's process tree (a leaf, a
  cycle, no pid); command lines and a failed `ps` refused with its reason; a
  role's pane marker, current or legacy, never on a tmux command; the kernel
  uid, its fallbacks and no pid; the runner for the same account, another
  account (`sudo -u <account> -H`) or none. `GOLDEN` below was produced by
  running the BASELINE launcher's own definitions over the very cases embedded
  here (`gold442.py`), not typed; it is byte-identical under `env -i`, in a
  normal role pane, with another HOME, USER and COLUMNS, under umask 077 and
  under several hash seeds.

Nothing reaches the host: `ps` never runs (`subprocess.run` answers from the
case), every runner is a recorder, and /proc is a synthetic tree in a
test-owned directory. No process is signalled, no sudo is run and no role is
launched. Spawns, every exec, signals, account and group lookups and socket
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
from scripts import process_inspection as m  # noqa: E402

CHECKS = 0
MOVED = ('role_process_runner_for', '_process_snapshot', 'process_tree_command_names', '_owner_process_runner', '_list_process_command_lines', '_role_has_pane_process', 'process_uid')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'role_process_runner_for': {'_owner_process_runner': 1, 'current_user_name': 1, 'role_run_as_user': 1},
    '_process_snapshot': {'_command_name': 3},
    'process_tree_command_names': {'_process_snapshot': 1},
    '_list_process_command_lines': {'_proc_failure_reason': 1},
    'process_uid': {'PROC_ROOT': 1, '_proc_effective_uid': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the seven that names them, and how often.
DISPATCH = {'_running_project_roles': {'role_process_runner_for': 1}}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/launch_phases.py': {'launcher._owner_process_runner': 1, 'launcher.role_process_runner_for': 3}, 'scripts/live_role_runtime.py': {'launcher._process_snapshot': 1, 'launcher.process_tree_command_names': 1, 'launcher.role_process_runner_for': 2}, 'scripts/presentation_controller.py': {'team_launcher._owner_process_runner': 1}, 'scripts/project_stop.py': {'launcher._owner_process_runner': 1}, 'scripts/role_identity_cutover.py': {'launcher._owner_process_runner': 1, 'launcher.process_uid': 1, 'launcher.role_process_runner_for': 4}, 'scripts/role_sessions.py': {'launcher.role_process_runner_for': 1}, 'scripts/tmux_session_argv.py': {'launcher.process_tree_command_names': 1}, 'scripts/worker_pool.py': {'launcher.role_process_runner_for': 2}}
#: The BASELINE's own behaviour for the cases below (`gold442.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'snapshot: a snapshot': {'result': [{'1': 0, '100': 1, '200': 100, '300': 200, '301': 300, '400': 100, '500': 200, '800': 800}, {'0': [1], '1': [100], '100': [200, 400], '200': [300, 500], '300': [301], '800': [800]}, {'1': {'set': ['init', 'splash', 'systemd']}, '100': {'set': ['-d', '-s', 'new-session', 'p442', 'tmux']}, '200': {'set': ['-bash', 'bash']}, '300': {'set': ['--resume', 'a b', 'claude']}, '301': {'set': ['--model', 'codex.js', 'node', 'x']}, '400': {'set': ['sh']}, '500': {'set': ['python3']}, '800': {'set': ['--self', 'loop']}}, {'1': ['/sbin/init', 'splash'], '100': ['tmux', 'new-session', '-d', '-s', 'p442'], '200': ['-bash'], '300': ['/usr/local/bin/claude', '--resume', 'a b'], '301': ['node', '/opt/codex/bin/codex.js', '--model', 'x'], '400': [], '500': ['python3'], '800': ['loop', '--self']}], 'type': 'tuple', 'calls': [['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}], ['_command_name', ['systemd'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['x'], {}], ['_command_name', ['x'], {}], ['_command_name', ['sh'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['--self'], {}], ['_command_name', ['--self'], {}]]},
    'snapshot: a failed ps': {'result': [{}, {}, {}, {}], 'type': 'tuple', 'calls': [['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}]]},
    'snapshot: an empty snapshot': {'result': [{}, {}, {}, {}], 'type': 'tuple', 'calls': [['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}]]},
    'snapshot: the command-name helper rebound on the launcher': {'result': [{'5': 1}, {'1': [5]}, {'5': {'set': ['-L', '/BIN/ZSH', 'ZSH']}}, {'5': ['/bin/zsh', '-l']}], 'type': 'tuple', 'calls': [['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}], ['_command_name', ['zsh'], {}], ['_command_name', ['/bin/zsh'], {}], ['_command_name', ['/bin/zsh'], {}], ['_command_name', ['-l'], {}], ['_command_name', ['-l'], {}]]},
    "tree: a pane's tree": {'result': {'set': ['--model', '--resume', '-bash', '-d', '-s', 'a b', 'bash', 'claude', 'codex.js', 'new-session', 'node', 'p442', 'python3', 'sh', 'tmux', 'x']}, 'type': 'set', 'calls': [['_process_snapshot', [], {}], ['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}], ['_command_name', ['systemd'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['x'], {}], ['_command_name', ['x'], {}], ['_command_name', ['sh'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['--self'], {}], ['_command_name', ['--self'], {}]]},
    'tree: a leaf': {'result': {'set': ['--model', 'codex.js', 'node', 'x']}, 'type': 'set', 'calls': [['_process_snapshot', [], {}], ['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}], ['_command_name', ['systemd'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['x'], {}], ['_command_name', ['x'], {}], ['_command_name', ['sh'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['--self'], {}], ['_command_name', ['--self'], {}]]},
    'tree: a pid not in the snapshot': {'result': {'set': []}, 'type': 'set', 'calls': [['_process_snapshot', [], {}], ['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}], ['_command_name', ['systemd'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['x'], {}], ['_command_name', ['x'], {}], ['_command_name', ['sh'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['--self'], {}], ['_command_name', ['--self'], {}]]},
    'tree: a process that is its own parent': {'result': {'set': ['--self', 'loop']}, 'type': 'set', 'calls': [['_process_snapshot', [], {}], ['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}], ['_command_name', ['systemd'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['/sbin/init'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['splash'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['tmux'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['new-session'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-d'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['-s'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['p442'], {}], ['_command_name', ['bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['-bash'], {}], ['_command_name', ['claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['/usr/local/bin/claude'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['--resume'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['a b'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['node'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['/opt/codex/bin/codex.js'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['--model'], {}], ['_command_name', ['x'], {}], ['_command_name', ['x'], {}], ['_command_name', ['sh'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['python3'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['loop'], {}], ['_command_name', ['--self'], {}], ['_command_name', ['--self'], {}]]},
    'tree: pid zero': {'result': {'set': []}, 'type': 'set', 'calls': []},
    'tree: a negative pid': {'result': {'set': []}, 'type': 'set', 'calls': []},
    'tree: a failed ps': {'result': {'set': []}, 'type': 'set', 'calls': [['_process_snapshot', [], {}], ['subprocess.run', [['ps', '-eo', 'pid=,ppid=,comm=,args=']], {'text': True, 'stdout': -1, 'stderr': -3}]]},
    'tree: the snapshot rebound on the launcher': {'result': {'set': ['a', 'b']}, 'type': 'set', 'calls': [['_process_snapshot', [], {}]]},
    'lines: every command line': {'result': ['a --b', 'c'], 'type': 'list', 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}]]},
    'lines: no output': {'result': [], 'type': 'list', 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}]]},
    'lines: ps fails with stderr': {'result': {'raised': 'SystemExit', 'message': 'switchyard: failed to inspect processes: ps: denied twice'}, 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}], ['_proc_failure_reason', ['NS proc', 'ps failed with exit 2'], {}]]},
    'lines: ps fails silently': {'result': {'raised': 'SystemExit', 'message': 'switchyard: failed to inspect processes: ps failed with exit 3'}, 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}], ['_proc_failure_reason', ['NS proc', 'ps failed with exit 3'], {}]]},
    'lines: ps exits 1': {'result': {'raised': 'SystemExit', 'message': 'switchyard: failed to inspect processes: partial'}, 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}], ['_proc_failure_reason', ['NS proc', 'ps failed with exit 1'], {}]]},
    'lines: ps fails with bytes': {'result': {'raised': 'SystemExit', 'message': 'switchyard: failed to inspect processes: bytes �'}, 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}], ['_proc_failure_reason', ['NS proc', 'ps failed with exit 4'], {}]]},
    'lines: the failure helper rebound on the launcher': {'result': {'raised': 'SystemExit', 'message': 'switchyard: failed to inspect processes: syrd442 reason'}, 'calls': [['runner', [['ps', '-eo', 'args=', '--no-headers']], {'stdout': -1, 'stderr': -1, 'text': True}], ['_proc_failure_reason', ['NS proc', 'ps failed with exit 5'], {}]]},
    'pane: the marker': {'result': True, 'type': 'bool', 'calls': []},
    'pane: the legacy marker': {'result': True, 'type': 'bool', 'calls': []},
    'pane: a tmux command carrying it': {'result': False, 'type': 'bool', 'calls': []},
    'pane: another target': {'result': False, 'type': 'bool', 'calls': []},
    'pane: a prefix of the target': {'result': True, 'type': 'bool', 'calls': []},
    'pane: an unparsable command': {'result': True, 'type': 'bool', 'calls': []},
    'pane: an unparsable tmux command': {'result': False, 'type': 'bool', 'calls': []},
    'pane: blank commands': {'result': False, 'type': 'bool', 'calls': []},
    'pane: no commands': {'result': False, 'type': 'bool', 'calls': []},
    'uid: the effective uid': {'result': 4420, 'type': 'int', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '42'], {}]]},
    'uid: the default root rebound on the launcher': {'result': 4421, 'type': 'int', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '42'], {}]]},
    'uid: a short uid line': {'result': "THE FIXTURE DIRECTORY'S OWNER", 'type': 'str', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '42'], {}]]},
    'uid: an unreadable uid': {'result': "THE FIXTURE DIRECTORY'S OWNER", 'type': 'str', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '42'], {}]]},
    'uid: no status file': {'result': "THE FIXTURE DIRECTORY'S OWNER", 'type': 'str', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '42'], {}]]},
    'uid: no such process': {'result': None, 'type': 'NoneType', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '43'], {}]]},
    'uid: pid zero': {'result': None, 'type': 'NoneType', 'calls': []},
    'uid: a negative pid': {'result': None, 'type': 'NoneType', 'calls': []},
    'uid: the uid reader rebound on the launcher': {'result': 4429, 'type': 'int', 'calls': [['_proc_effective_uid', ['PATH TMP/proc', '42'], {}]]},
    'runner: the same account': {'result': 'the given runner', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}], ['current_user_name', [], {}]]},
    'runner: another account': {'result': 'CALLABLE _owner_process_runner.<locals>.wrapped', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}], ['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'p442-owner', 'runner': 'STAND-IN runner'}], ['runner', [['sudo', '-u', 'p442-owner', '-H', 'tmux', 'ls']], {'text': True}]]},
    'runner: the same account, another caller': {'result': 'the given runner', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}], ['current_user_name', [], {}]]},
    'runner: no account': {'result': 'the given runner', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}]]},
    'runner: the default runner, same account': {'result': 'the real subprocess.run', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}], ['current_user_name', [], {}]]},
    'runner: the default runner, another account': {'result': 'CALLABLE _owner_process_runner.<locals>.wrapped', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}], ['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'p442-owner', 'runner': 'CALLABLE run'}]]},
    'runner: the owner runner rebound on the launcher': {'result': 'syrd442-owner-runner', 'type': 'str', 'calls': [['role_run_as_user', ['NS config', 'NS role'], {}], ['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'p442-owner', 'runner': 'STAND-IN runner'}]]},
    'owner runner: the command wrapped in sudo': {'result': 'NS proc', 'type': 'SimpleNamespace', 'calls': [['runner', [['sudo', '-u', 'p442-owner', '-H', 'tmux', 'list-sessions']], {'text': True, 'check': False}]]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold442.py` (which ran them on the baseline) ------------------------------------
# A case reads a synthetic host. `ps` is never run: `subprocess.run` answers from the case's synthetic `ps` output (the
# snapshot calls it directly), and every runner passed in is a recorder. `/proc` is never read: the kernel uid reader
# reads a synthetic /proc tree in a test-owned directory, and `PROC_ROOT` is rebound on the launcher to it for the
# default case. The current user and the role's account are stand-ins on the launcher; the command-name, failure-reason
# and uid helpers are the launcher's own, recorded there and passed through. No process is signalled, no sudo is run and
# no role is launched. Recorded, in order: every call with its arguments, and the answer (sets sorted and marked), or
# the exact exception.
PS = "\n".join([
    "  1     0 systemd /sbin/init splash",
    " 100    1 tmux    tmux new-session -d -s p442",
    " 200  100 bash    -bash",
    " 300  200 claude  /usr/local/bin/claude --resume 'a b'",
    " 301  300 node    node /opt/codex/bin/codex.js --model x",
    " 400  100 sh      sh -c 'unbalanced",
    " 500  200 python3 python3",
    "  x9  200 bad     not a pid",
    " 600   y1 bad     not a parent",
    " 700  200 short",
    "",
    " 800  800 loop    loop --self",
])
SNAPSHOT = {
    "a snapshot": {"ps": {"code": 0, "stdout": PS}},
    "a failed ps": {"ps": {"code": 1, "stdout": PS}},
    "an empty snapshot": {"ps": {"code": 0, "stdout": ""}},
    "the command-name helper rebound on the launcher": {"ps": {"code": 0, "stdout": " 5 1 zsh /bin/zsh -l"}, "launcher": {"_command_name": "UPPER"}},
}
TREE = {
    "a pane's tree": {"pid": 100, "ps": {"code": 0, "stdout": PS}},
    "a leaf": {"pid": 301, "ps": {"code": 0, "stdout": PS}},
    "a pid not in the snapshot": {"pid": 999, "ps": {"code": 0, "stdout": PS}},
    "a process that is its own parent": {"pid": 800, "ps": {"code": 0, "stdout": PS}},
    "pid zero": {"pid": 0, "ps": {"code": 0, "stdout": PS}},
    "a negative pid": {"pid": -3, "ps": {"code": 0, "stdout": PS}},
    "a failed ps": {"pid": 100, "ps": {"code": 1, "stdout": PS}},
    "the snapshot rebound on the launcher": {"pid": 7, "launcher": {"_process_snapshot": "SNAPSHOT"}},
}
LINES = {
    "every command line": {"runner": {"code": 0, "stdout": "a --b\n\n  \nc\n"}},
    "no output": {"runner": {"code": 0, "stdout": None}},
    "ps fails with stderr": {"runner": {"code": 2, "stdout": "", "stderr": "  ps: denied\n  twice \n"}},
    "ps fails silently": {"runner": {"code": 3, "stdout": "", "stderr": ""}},
    "ps exits 1": {"runner": {"code": 1, "stdout": "listed anyway\n", "stderr": "partial"}},
    "ps fails with bytes": {"runner": {"code": 4, "stdout": "", "stderr": b"bytes \xff"}},
    "the failure helper rebound on the launcher": {"runner": {"code": 5, "stdout": ""}, "launcher": {"_proc_failure_reason": "REASON"}},
}
PANE = {
    "the marker": {"target": "p442:0.1", "commands": ["claude TICKET_BOARD_PANE_TARGET=p442:0.1"]},
    "the legacy marker": {"target": "p442:0.1", "commands": ["env PGU_PANE_TARGET=p442:0.1 codex"]},
    "a tmux command carrying it": {"target": "p442:0.1", "commands": ["tmux new -e TICKET_BOARD_PANE_TARGET=p442:0.1", "/usr/bin/tmux x TICKET_BOARD_PANE_TARGET=p442:0.1"]},
    "another target": {"target": "p442:0.1", "commands": ["claude TICKET_BOARD_PANE_TARGET=p442:0.2"]},
    "a prefix of the target": {"target": "p442:0.1", "commands": ["claude TICKET_BOARD_PANE_TARGET=p442:0.10"]},
    "an unparsable command": {"target": "p442:0.1", "commands": ["sh -c 'x TICKET_BOARD_PANE_TARGET=p442:0.1"]},
    "an unparsable tmux command": {"target": "p442:0.1", "commands": ["tmux 'x TICKET_BOARD_PANE_TARGET=p442:0.1"]},
    "blank commands": {"target": "p442:0.1", "commands": ["", "   "]},
    "no commands": {"target": "p442:0.1", "commands": []},
}
UID = {
    "the effective uid": {"pid": 42, "proc": {"42": "Name:\tx\nUid:\t1000\t4420\t1002\t1003\n"}},
    "the default root rebound on the launcher": {"pid": 42, "proc": {"42": "Uid:\t1\t4421\t3\t4\n"}, "default_root": True},
    "a short uid line": {"pid": 42, "proc": {"42": "Uid:\t1000\n"}},
    "an unreadable uid": {"pid": 42, "proc": {"42": "Uid:\tx\ty\tz\n"}},
    "no status file": {"pid": 42, "proc": {"42": None}},
    "no such process": {"pid": 43, "proc": {"42": "Uid:\t1\t2\t3\t4\n"}},
    "pid zero": {"pid": 0, "proc": {}},
    "a negative pid": {"pid": -1, "proc": {}},
    "the uid reader rebound on the launcher": {"pid": 42, "proc": {}, "launcher": {"_proc_effective_uid": "UID"}},
}
RUNNER = {
    "the same account": {"account": "p442-agent", "current": "p442-agent"},
    "another account": {"account": "p442-owner", "current": "p442-agent", "invoke": ["tmux", "ls"]},
    "the same account, another caller": {"account": "p442-owner", "current": "p442-owner"},
    "no account": {"account": "", "current": "p442-agent"},
    "the default runner, same account": {"account": "p442-agent", "current": "p442-agent", "default": True},
    "the default runner, another account": {"account": "p442-owner", "current": "p442-agent", "default": True},
    "the owner runner rebound on the launcher": {"account": "p442-owner", "current": "p442-agent", "launcher": {"_owner_process_runner": "OWNER"}},
}
CASES = {
    **{f"snapshot: {k}": {"call": "snapshot", **v} for k, v in SNAPSHOT.items()},
    **{f"tree: {k}": {"call": "tree", **v} for k, v in TREE.items()},
    **{f"lines: {k}": {"call": "lines", **v} for k, v in LINES.items()},
    **{f"pane: {k}": {"call": "pane", **v} for k, v in PANE.items()},
    **{f"uid: {k}": {"call": "uid", **v} for k, v in UID.items()},
    **{f"runner: {k}": {"call": "runner", **v} for k, v in RUNNER.items()},
    "owner runner: the command wrapped in sudo": {"call": "owner", "owner": "p442-owner", "invoke": ["tmux", "list-sessions"]},
}
FUNCTIONS = ("role_process_runner_for", "_process_snapshot", "process_tree_command_names", "_owner_process_runner",
             "_list_process_command_lines", "_role_has_pane_process", "process_uid")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions; `ps`, runners and /proc all synthetic, in a fresh test-owned tree."""
    import json, os, shutil, subprocess, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd442-")).resolve()

    def norm(value):
        if isinstance(value, (set, frozenset)):
            return {"set": sorted(norm(v) for v in value)}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, bytes):
            return "BYTES " + repr(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"NS {getattr(value, 'label', '?')}"
        if callable(value) and "run_case.<locals>" in getattr(value, "__qualname__", ""):
            return f"STAND-IN {value.__name__}"
        if callable(value):
            return f"CALLABLE {getattr(value, '__qualname__', '?')}"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    names = [*FUNCTIONS, "current_user_name", "role_run_as_user", "_command_name", "_proc_failure_reason", "_proc_effective_uid", "PROC_ROOT"]
    saved = {n: getattr(t, n) for n in names}
    saved_run = subprocess.run
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            f.__name__ = name
            return f

        def answer(ps):
            return SimpleNamespace(label="proc", returncode=ps["code"], stdout=ps.get("stdout"), stderr=ps.get("stderr"))

        def ps_run(argv, **kwargs):
            note("subprocess.run", list(argv), **kwargs)
            return answer(spec["ps"])

        def runner(argv, **kwargs):
            note("runner", list(argv), **kwargs)
            return answer(spec.get("runner", {"code": 0, "stdout": "ran"}))

        proc = tmp / "proc"
        for pid, status in spec.get("proc", {}).items():
            (proc / pid).mkdir(parents=True)
            if status is not None:
                (proc / pid / "status").write_text(status, encoding="utf-8")
        stand = {"UPPER": lambda value: note("_command_name", value) or value.strip().upper(),
                 "SNAPSHOT": lambda: note("_process_snapshot") or ({}, {7: [8]}, {7: {"a"}, 8: {"b"}}, {}),
                 "REASON": lambda p, fallback: note("_proc_failure_reason", p, fallback) or "syrd442 reason",
                 "UID": lambda root, pid: note("_proc_effective_uid", root, pid) or 4429,
                 "OWNER": lambda *, owner_user, runner: note("_owner_process_runner", owner_user=owner_user, runner=runner) or "syrd442-owner-runner"}
        on_launcher = {n: passthrough(n) for n in (*FUNCTIONS, "_command_name", "_proc_failure_reason", "_proc_effective_uid")}
        on_launcher.update(current_user_name=lambda: note("current_user_name") or spec.get("current", "p442-agent"),
                           role_run_as_user=lambda config, role: note("role_run_as_user", config, role) or spec.get("account", ""),
                           PROC_ROOT=proc if spec.get("default_root") else tmp / "no-default-root")
        on_launcher.update({k: stand[v] for k, v in spec.get("launcher", {}).items()})
        for n, f in on_launcher.items():
            setattr(t, n, f)
        if "ps" in spec:
            subprocess.run = ps_run
        call = spec["call"]
        try:
            if call == "snapshot":
                got = under["_process_snapshot"]()
            elif call == "tree":
                got = under["process_tree_command_names"](spec["pid"])
            elif call == "lines":
                got = under["_list_process_command_lines"](runner=runner)
            elif call == "pane":
                got = under["_role_has_pane_process"](SimpleNamespace(label="role", target=spec["target"]), list(spec["commands"]))
            elif call == "uid":
                got = under["process_uid"](spec["pid"]) if spec.get("default_root") else under["process_uid"](spec["pid"], proc_root=proc)
                # The reader falls back to the process directory's owner, which here is whoever runs the test.
                if (proc / str(spec["pid"])).is_dir() and got == os.stat(proc / str(spec["pid"])).st_uid:
                    got = "THE FIXTURE DIRECTORY'S OWNER"
            elif call == "runner":
                config, role = SimpleNamespace(label="config"), SimpleNamespace(label="role")
                got = under["role_process_runner_for"](config, role) if spec.get("default") else under["role_process_runner_for"](config, role, runner=runner)
                real_run = getattr(got, "__module__", "") == "subprocess" and getattr(got, "__name__", "") == "run"
                shape = "the given runner" if got is runner else "the real subprocess.run" if real_run else norm(got)
                if spec.get("invoke") and callable(got):
                    got(list(spec["invoke"]), text=True)
                got = shape
            else:
                wrapped = under["_owner_process_runner"](owner_user=spec["owner"], runner=runner)
                got = wrapped(list(spec["invoke"]), text=True, check=False)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": calls}
        return {"result": norm(got), "type": type(got).__name__, "calls": calls}
    finally:
        subprocess.run = saved_run
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
    result = python("import sys, scripts.process_inspection as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.process_inspection", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.process_inspection")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.process_inspection as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty), "
                        "[inspect.signature(getattr(m, n)).parameters['runner'].default is subprocess.run for n in ('role_process_runner_for', '_list_process_command_lines')], "
                        "inspect.signature(m.process_uid).parameters['proc_root'].default is None, "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'ProjectConfig', 'RoleConfig', 'PROC_ROOT', '_command_name')))")
        check(result.stdout.strip() == "True ['_list_process_command_lines.runner', 'process_uid.proc_root', 'role_process_runner_for.runner'] [True, True] True True",
              f"{' then '.join(order)}: one set of objects; the runner defaults are subprocess.run itself; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import shlex
    import typing
    check(m.subprocess is subprocess and m.shlex is shlex and m.Path is Path and m.Sequence is typing.Sequence and t.shlex is m.shlex,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "process_inspection.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "import shlex", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable, Sequence"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig, RoleConfig"],
          f"the standard library, and the two config types for annotations only: {top} {tc}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the seven in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_seven_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.process_inspection"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the seven, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"ProjectConfig", "RoleConfig", "_running_project_roles"} <= defined | exported,
          "the launcher defines none of them, and keeps its caller and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"_running_project_roles calls role_process_runner_for by its launcher global, exactly as often as before: {uses}")
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

    snap = GOLDEN["snapshot: a snapshot"]
    check(snap["calls"][0] == ["subprocess.run", [["ps", "-eo", "pid=,ppid=,comm=,args="]], {"stderr": -3, "stdout": -1, "text": True}]
          and snap["type"] == "tuple" and len(snap["result"]) == 4,
          "the snapshot is one `ps -eo pid=,ppid=,comm=,args=` read as text, its errors discarded: parents, children, names and argv by pid")
    parents, children, names, argv = snap["result"]
    check(sorted(parents) == ["1", "100", "200", "300", "301", "400", "500", "800"] and children["100"] == [200, 400]
          and argv["400"] == [] and argv["300"] == ["/usr/local/bin/claude", "--resume", "a b"]
          and names["300"] == {"set": ["--resume", "a b", "claude"]},
          "lines without four fields or with a non-numeric pid or parent are skipped; unparsable arguments read as none; a process is named by its command and every argument's basename")
    check(result("snapshot: a failed ps") == result("snapshot: an empty snapshot") == [{}, {}, {}, {}],
          "a failed or empty `ps` is an empty snapshot, not an error")
    check(result("tree: a pane's tree")["set"][:3] == ["--model", "--resume", "-bash"] and "systemd" not in result("tree: a pane's tree")["set"]
          and result("tree: a leaf") == {"set": ["--model", "codex.js", "node", "x"]} and result("tree: a process that is its own parent") == {"set": ["--self", "loop"]}
          and result("tree: a pid not in the snapshot") == {"set": []},
          "a pane's tree is it and its descendants, each once, never its ancestors")
    check(steps("tree: pid zero") == steps("tree: a negative pid") == [] and result("tree: pid zero") == {"set": []}
          and result("tree: the snapshot rebound on the launcher") == {"set": ["a", "b"]},
          "no pid, no snapshot taken; the snapshot is the launcher's when it runs")
    check(result("lines: every command line") == ["a --b", "c"] and result("lines: no output") == []
          and GOLDEN["lines: every command line"]["calls"][0] == ["runner", [["ps", "-eo", "args=", "--no-headers"]], {"stderr": -1, "stdout": -1, "text": True}],
          "command lines: `ps -eo args= --no-headers` through the runner, blank lines dropped")
    check([result(f"lines: {k}")["message"] for k in ("ps fails with stderr", "ps fails silently", "the failure helper rebound on the launcher")]
          == ["switchyard: failed to inspect processes: ps: denied twice", "switchyard: failed to inspect processes: ps failed with exit 3",
              "switchyard: failed to inspect processes: syrd442 reason"],
          "a failed `ps` is refused, with the launcher's failure reason when it runs")
    check([result(f"pane: {k}") for k in ("the marker", "the legacy marker", "a tmux command carrying it", "another target", "an unparsable command",
                                           "an unparsable tmux command", "blank commands", "no commands")]
          == [True, True, False, False, True, False, False, False],
          "a role has a pane process when a non-tmux command carries its pane marker, current or legacy")
    check(result("pane: a prefix of the target") is True,
          "the marker is matched as a substring, so a longer target that starts with it matches (the baseline's behaviour, kept)")
    check(result("uid: the effective uid") == 4420 and result("uid: the default root rebound on the launcher") == 4421
          and result("uid: no such process") is None and result("uid: pid zero") is None and steps("uid: pid zero") == []
          and all(result(f"uid: {k}") == "THE FIXTURE DIRECTORY'S OWNER" for k in ("a short uid line", "an unreadable uid", "no status file"))
          and result("uid: the uid reader rebound on the launcher") == 4429,
          "the effective uid from the kernel's status, else the process directory's owner; no pid, nothing read; the root and reader are the launcher's")
    check(result("runner: the same account") == result("runner: no account") == "the given runner"
          and result("runner: the default runner, same account") == "the real subprocess.run"
          and steps("runner: no account") == ["role_run_as_user"]
          and result("runner: the owner runner rebound on the launcher") == "syrd442-owner-runner",
          "a role running as the current user, or as no one, gets the runner itself; the default is subprocess.run")
    # The exact argv is pinned by the golden comparison; here the distinguishing arguments, so the screen reads no command.
    wrapped = GOLDEN["runner: another account"]["calls"][3]
    direct = GOLDEN["owner runner: the command wrapped in sudo"]["calls"]
    check(GOLDEN["runner: another account"]["calls"][2] == ["_owner_process_runner", [], {"owner_user": "p442-owner", "runner": "STAND-IN runner"}]
          and wrapped[0] == "runner" and wrapped[1][0][1:4] == ["-u", "p442-owner", "-H"] and wrapped[1][0][5:] == ["ls"] and wrapped[2] == {"text": True}
          and len(direct) == 1 and direct[0][1][0][1:4] == ["-u", "p442-owner", "-H"] and direct[0][1][0][5:] == ["list-sessions"]
          and direct[0][1][0][0] == wrapped[1][0][0] and direct[0][2] == {"check": False, "text": True},
          "another account's runner runs every command as that account: `sudo -u <account> -H`, keywords passed on")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the seven read on the launcher is a recorder or stand-in there; PROC_ROOT is rebound there by its own case.
    names = {name for reads in SEAMS.values() for name in reads} - {"PROC_ROOT"}
    check(names <= REACHED, f"a recorder on the launcher reached every function: missing {sorted(names - REACHED)}")
    check(any(spec.get("default_root") for spec in CASES.values()), "and PROC_ROOT was rebound there to the synthetic /proc")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_seven_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"process_inspection_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
