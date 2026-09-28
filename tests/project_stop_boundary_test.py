#!/usr/bin/env python3
"""SYRD-407: stopping a project's sessions, against the launcher it came out of.

`stop_project` moved unchanged into `scripts/project_stop.py`, and the launcher
re-exports it. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads nothing of Switchyard's; `runner=subprocess.run` and
  `print_func=print` are the defaults bound when it is defined; `ProjectConfig`
  is an annotation only; the presentation controller is still imported inside
  the function, after the viewer step, when it runs.
- **Seams (rule 24):** the viewer session name, the current user, the owner
  runner, the failure reason, the two tmux argv builders and the role-session
  stop are read through the launcher as often as before, so a patch there
  reaches each of them -- every case below runs with all seven standing in (or
  wrapped) on the launcher, and their `role_sessions` definitions refuse to be
  called directly.
- **Callers:** `main`'s `stop` still calls it by its own global, and
  `scripts/tenant_suspension.py` still reads `launcher.stop_project`.
- **Behaviour is the baseline's:** the viewer probed and killed as the project
  owner (through `sudo -u <owner>` only when the owner is set and is not this
  user), with its output captured exactly as before; then the presentation and
  every role session on the caller's runner, each attempted whatever failed
  before; the first nonzero exit is the answer. `GOLDEN` below was produced by
  running the BASELINE launcher's own function over the very cases embedded
  here (`gold407.py`), not typed.

Nothing touches tmux, a tenant, a user or a service: every runner is a
recorder, and the real `subprocess.run`/`Popen`, `os.kill`, the account
lookups, `os.geteuid` and socket connections are refused for each case.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import project_stop as m  # noqa: E402
from scripts import presentation_controller, role_sessions  # noqa: E402

CHECKS = 0
MOVED = ("stop_project",)
#: Measured on the baseline launcher: the body's call-time reads of launcher globals.
SEAMS = {
    'stop_project': {'_owner_process_runner': 1, '_proc_failure_reason': 1, 'current_user_name': 1, 'stop_role_sessions': 1, 'tmux_has_session_by_name_args': 1, 'tmux_kill_session_by_name_args': 1, 'viewer_session_for_project': 1},
}
#: Measured on the baseline: every launcher function that calls it, and by what name.
DISPATCH = {'main': {'stop_project': 1}}
#: The BASELINE's own behaviour for the cases below (`gold407.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'no owner, viewer absent': {'answer': 0, 'said': ['already stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'no owner, viewer live and stopped': {'answer': 0, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'the owner is this user': {'answer': 0, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'a different owner, viewer absent': {'answer': 0, 'said': ['already stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'a different owner, viewer live and stopped': {'answer': 0, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'has-session exits 2': {'answer': 0, 'said': ['already stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'the kill fails, with stderr': {'answer': 3, 'said': ['failed to stop viewer: porter-viewer: no server running on /tmp/tmux-0/default error connecting', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 3, 'stderr': 'no server running on /tmp/tmux-0/default\n\n  error connecting  \n'}, 'tmux kill-session failed with exit 3'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'the kill fails, stderr blank': {'answer': 4, 'said': ['failed to stop viewer: porter-viewer: tmux kill-session failed with exit 4', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 4, 'stderr': '  \n'}, 'tmux kill-session failed with exit 4'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'the kill fails, stderr absent': {'answer': 5, 'said': ['failed to stop viewer: porter-viewer: tmux kill-session failed with exit 5', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 5, 'stderr': None}, 'tmux kill-session failed with exit 5'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'no current user': {'answer': 0, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'the defaults, a different owner': {'answer': 0, 'said': [], 'stdout': 'stopped viewer: porter-viewer\npresentation stand-in: stopping\nrole stand-in: stopping every role\n', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'subprocess.run'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['owner runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['owner runner', ['tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['runner', ['tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['stop_presentation', ['CONFIG'], {'print_func': 'print', 'runner': 'subprocess.run'}], ['stop_role_sessions', ['CONFIG'], {'print_func': 'print', 'runner': 'subprocess.run'}]]},
    'the defaults, every step failing': {'answer': 6, 'said': [], 'stdout': 'failed to stop viewer: porter-viewer: denied\npresentation stand-in: stopping\nrole stand-in: stopping every role\n', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'subprocess.run'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['owner runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['runner', ['tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['owner runner', ['tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['runner', ['tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 6, 'stderr': 'denied\n'}, 'tmux kill-session failed with exit 6'], {}], ['stop_presentation', ['CONFIG'], {'print_func': 'print', 'runner': 'subprocess.run'}], ['stop_role_sessions', ['CONFIG'], {'print_func': 'print', 'runner': 'subprocess.run'}]]},
    'viewer ok, presentation 0, roles 0': {'answer': 0, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer ok, presentation 0, roles 7': {'answer': 7, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer ok, presentation 5, roles 0': {'answer': 5, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer ok, presentation 5, roles 7': {'answer': 5, 'said': ['stopped viewer: porter-viewer', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer fails, presentation 0, roles 0': {'answer': 3, 'said': ['failed to stop viewer: porter-viewer: kill refused', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 3, 'stderr': 'kill refused\n'}, 'tmux kill-session failed with exit 3'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer fails, presentation 0, roles 7': {'answer': 3, 'said': ['failed to stop viewer: porter-viewer: kill refused', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 3, 'stderr': 'kill refused\n'}, 'tmux kill-session failed with exit 3'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer fails, presentation 5, roles 0': {'answer': 3, 'said': ['failed to stop viewer: porter-viewer: kill refused', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 3, 'stderr': 'kill refused\n'}, 'tmux kill-session failed with exit 3'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
    'viewer fails, presentation 5, roles 7': {'answer': 3, 'said': ['failed to stop viewer: porter-viewer: kill refused', 'presentation stand-in: stopping', 'role stand-in: stopping every role'], 'stdout': '', 'calls': [['current_user_name', [], {}], ['_owner_process_runner', [], {'owner_user': 'syrd407-owner', 'runner': 'RUNNER'}], ['viewer_session_for_project', ['porter'], {}], ['tmux_has_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'has-session', '-t', 'porter-viewer'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_kill_session_by_name_args', ['porter-viewer'], {}], ['runner', ['sudo', '-u', 'syrd407-owner', '-H', 'tmux', 'kill-session', '-t', 'porter-viewer'], {'stderr': 'PIPE', 'stdout': 'DEVNULL', 'text': True}], ['_proc_failure_reason', [{'returncode': 3, 'stderr': 'kill refused\n'}, 'tmux kill-session failed with exit 3'], {}], ['say'], ['stop_presentation', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say'], ['stop_role_sessions', ['CONFIG'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['say']]},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold407.py` (which ran them on the baseline) ------------------------------------
# me: who the process runs as; owner: config.run_as_user; has/kill: the viewer's has-session/kill-session exits;
# stderr: what a failed kill says; presentation/roles: what those two steps answer; defaults: nothing but the config.
CASES = {
    "no owner, viewer absent": {"owner": "", "has": 1},
    "no owner, viewer live and stopped": {"owner": "", "has": 0, "kill": 0},
    "the owner is this user": {"owner": "syrd407-me", "has": 0, "kill": 0},
    "a different owner, viewer absent": {"has": 1},
    "a different owner, viewer live and stopped": {"has": 0, "kill": 0},
    "has-session exits 2": {"has": 2},
    "the kill fails, with stderr": {"has": 0, "kill": 3, "stderr": "no server running on /tmp/tmux-0/default\n\n  error connecting  \n"},
    "the kill fails, stderr blank": {"has": 0, "kill": 4, "stderr": "  \n"},
    "the kill fails, stderr absent": {"has": 0, "kill": 5, "stderr": None},
    "no current user": {"me": "", "has": 0, "kill": 0},
    "the defaults, a different owner": {"defaults": True, "has": 0, "kill": 0},
    "the defaults, every step failing": {"defaults": True, "has": 0, "kill": 6, "stderr": "denied\n", "presentation": 5, "roles": 7},
}
for viewer_fails in (False, True):
    for presentation in (0, 5):
        for roles in (0, 7):
            CASES[f"viewer {'fails' if viewer_fails else 'ok'}, presentation {presentation}, roles {roles}"] = {
                "has": 0, "kill": 3 if viewer_fails else 0, "stderr": "kill refused\n", "presentation": presentation, "roles": roles}
_SUBPROCESS_RUN, _PRINT, _DEVNULL, _PIPE = subprocess.run, print, subprocess.DEVNULL, subprocess.PIPE


def run_case(t: object, holder: object, controller: object, spec: dict, reached: set) -> dict:
    """Run one case against `holder.stop_project`, every launcher seam standing in (or wrapped) on `t`, the presentation stop on `controller`."""
    calls: list = []
    said: list = []
    config = SimpleNamespace(project="porter", run_as_user=spec.get("owner", "syrd407-owner"), roles=["unused by the stand-ins"])

    def runner(argv, **kwargs):
        calls.append(["runner", norm(list(argv)), norm(dict(sorted(kwargs.items())))])
        verb = next(a for a in argv if a in ("has-session", "kill-session"))
        code = {"has-session": spec.get("has", 0), "kill-session": spec.get("kill", 0)}[verb]
        return SimpleNamespace(returncode=code, stdout="", stderr=spec.get("stderr", ""))

    def owner_runner(argv, **kwargs):
        calls.append(["owner runner", norm(list(argv)), norm(dict(sorted(kwargs.items())))])
        return runner(argv, **kwargs)

    def say(line):
        said.append(line)
        calls.append(["say"])

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        for obj, name in ((config, "CONFIG"), (runner, "RUNNER"), (owner_runner, "OWNER RUNNER"), (say, "SAY"), (_SUBPROCESS_RUN, "subprocess.run"),
                          (_PRINT, "print"), (_DEVNULL, "DEVNULL"), (_PIPE, "PIPE")):
            if value is obj:
                return name
        if isinstance(value, SimpleNamespace):
            return {"returncode": value.returncode, "stderr": value.stderr}
        if callable(value):
            return "ANOTHER CALLABLE" if getattr(value, "__name__", "") != "wrapped" else "THE OWNER WRAPPER"
        return value

    real = {n: getattr(t, n) for n in ("viewer_session_for_project", "_owner_process_runner", "_proc_failure_reason",
                                       "tmux_has_session_by_name_args", "tmux_kill_session_by_name_args")}

    def wrap(name, function):
        def call(*args, **kwargs):
            reached.add(name)
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items())))])
            return function(*args, **kwargs)
        return call

    def owner(*, owner_user, runner):
        # With the defaults the caller's runner is the real subprocess.run: answer a recorder instead of wrapping it.
        return owner_runner if spec.get("defaults") else real["_owner_process_runner"](owner_user=owner_user, runner=runner)

    def presentation(cfg, *, runner, print_func):
        calls.append(["stop_presentation", norm([cfg]), norm({"print_func": print_func, "runner": runner})])
        print_func("presentation stand-in: stopping")
        return spec.get("presentation", 0)

    def roles(cfg, *, runner, print_func):
        print_func("role stand-in: stopping every role")
        return spec.get("roles", 0)

    stand = {"current_user_name": wrap("current_user_name", lambda: spec.get("me", "syrd407-me")),
             "_owner_process_runner": wrap("_owner_process_runner", owner),
             "stop_role_sessions": wrap("stop_role_sessions", roles),
             **{n: wrap(n, f) for n, f in real.items() if n != "_owner_process_runner"}}
    saved = {n: getattr(t, n) for n in stand}
    saved_presentation = controller.stop_presentation
    for n, f in stand.items():
        setattr(t, n, f)
    controller.stop_presentation = presentation
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            got = holder.stop_project(config) if spec.get("defaults") else holder.stop_project(config, runner=runner, print_func=say)
        answer = norm(got)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        answer = {"raised": type(exc).__name__, "message": str(exc)}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
        controller.stop_presentation = saved_presentation
    return {"answer": answer, "said": said, "stdout": stdout.getvalue(), "calls": calls}
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


HOMES = {role_sessions: ("stop_role_sessions", "tmux_has_session_by_name_args", "tmux_kill_session_by_name_args")}


class contained:
    """Spawns, signals, account lookups and connections refused, and each role-session step's own definition."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill"), geteuid=refuse("os.geteuid")),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex")),
                      *(patched(home, **{name: refuse(f"{home.__name__}.{name}") for name in names}) for home, names in HOMES.items())]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(spec: dict) -> dict:
    with contained():
        return run_case(t, m, presentation_controller, spec, REACHED)


def test_the_guard_itself_refuses_a_spawn_a_lookup_and_each_step_past_the_launcher() -> None:
    attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0), lambda: os.geteuid(),
                lambda: pwd.getpwuid(0), lambda: socket.socket().connect(("127.0.0.1", 9)),
                *(lambda home=home, name=name: getattr(home, name)("porter-viewer") for home, names in HOMES.items() for name in names)]
    for attempt in attempts:
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, a signal, an account lookup, a connection, and each role-session step's own definition")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.project_stop as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_object_and_the_defaults() -> None:
    for order in (("scripts.project_stop", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_stop")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_stop as m; "
                        "s = inspect.signature(m.stop_project).parameters; "
                        "print(t.stop_project is m.stop_project, "
                        "s['runner'].default is subprocess.run and s['print_func'].default is print and list(s) == ['config', 'runner', 'print_func'], "
                        "not hasattr(m, 'ProjectConfig') and not hasattr(m, 'launcher') and not hasattr(m, 'presentation_controller'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.subprocess is subprocess, "the standard-library name is the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "project_stop.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if getattr(n, "name", None) == "stop_project")
    through: dict[str, int] = {}
    for x in ast.walk(node):
        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
            through[x.attr] = through.get(x.attr, 0) + 1
    expected = SEAMS["stop_project"]
    check(through == expected, f"each launcher name read through it exactly as often as before: {through}")
    imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
    check(imports == ["from scripts import team_launcher as launcher", "from scripts import presentation_controller"]
          and ast.unparse(node.body[0]) == imports[0], f"the launcher imported first thing when it runs, and the controller inside too: {imports}")
    at = [i for i, s in enumerate(node.body) if ast.unparse(s) == imports[1]]
    viewer = [i for i, s in enumerate(node.body) if isinstance(s, ast.If) and ast.unparse(s.test) == "viewer_exists"]
    check(len(at) == 1 and len(viewer) == 1 and at[0] == viewer[0] + 1, "the controller is imported right after the viewer step, as before")
    skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
            if part is not None for y in ast.walk(part)}
    bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
    check(bare == [], f"none of them read past it: {bare}")
    check([ast.unparse(d) for d in node.args.kw_defaults if d is not None] == ["subprocess.run", "print"], "the runner and print defaults are bound when it is defined")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    check([n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))] == list(MOVED), "the one function, and nothing else")


def test_the_launcher_reexports_it_and_its_callers_reach_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_stop"]
    check(len(imports) == 1 and [(a.name, a.asname) for a in imports[0].names] == [("stop_project", None)], "one explicit import of it, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read it")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_plan_data_from_config", "_layout_slot_count", "main"} <= defined | exported,
          "the launcher does not define it, and keeps its neighbours and its dispatcher, its own or re-exported")
    calls: dict = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for x in ast.walk(fn):
                if isinstance(x, ast.Call) and ast.unparse(x.func).split(".")[-1] in MOVED:
                    calls.setdefault(fn.name, {}).setdefault(ast.unparse(x.func), 0)
                    calls[fn.name][ast.unparse(x.func)] += 1
    check(calls == DISPATCH, f"the launcher's callers reach it by its own global, as often as before: {calls}")
    suspension = ast.parse((ROOT / "scripts" / "tenant_suspension.py").read_text(encoding="utf-8"))
    uses = [ast.unparse(x) for x in ast.walk(suspension) if (isinstance(x, ast.Attribute) and x.attr == "stop_project")
            or (isinstance(x, ast.Name) and x.id == "stop_project")]
    check(uses == ["launcher.stop_project"], f"the suspension still reads it through the launcher, when it runs: {uses}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_stop_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        got = run(spec)
        check(got == GOLDEN[label], f"{label}: the baseline's answer, messages and every call in order: {got}")


def test_every_step_is_attempted_and_no_role_is_killed_here() -> None:
    for label, got in GOLDEN.items():
        steps = [c[0] for c in got["calls"] if c[0] in ("stop_presentation", "stop_role_sessions")]
        check(steps == ["stop_presentation", "stop_role_sessions"], f"{label}: the presentation, then every role, whatever failed before: {steps}")
        kills = [c[1] for c in got["calls"] if c[0] in ("runner", "owner runner") and "kill-session" in c[1]]
        check(all(k[-1] == "porter-viewer" for k in kills), f"{label}: the only session killed here is the viewer: {kills}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_object_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it_and_its_callers_reach_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"project_stop_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
