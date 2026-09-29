#!/usr/bin/env python3
"""SYRD-479: pane-state authority and hook storage, against notify_listener they came out of.

The thirteen -- the hook-state store and its record (`PaneHookStateStore`,
`PaneHookState`, `DEFAULT_PANE_STATE_DIR`), the turn-start carry
(`hook_turn_started_at`, `TURN_START_EVENTS`), the authority report
(`PaneStateAuthority`, `pane_state_authority`), runtime-assignment liveness
(`registered_pane_targets`, `live_pane_targets`, `assignment_process_gone`,
`_pid_exists`) and the offline check (`load_runtime_assignments`,
`verify_pane_state_authority`) -- moved unchanged into
`scripts/ticket_board/pane_state.py`. `notify_listener` re-exports all of them
and keeps its own imports, `PROC_ROOT` and `read_process` among them: the moved
code reads those through it. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first, in both import
  forms: the tests' `scripts.ticket_board` and the installed entry point's
  `ticket_board`. The module alone loads only its package and `peer_identity`,
  whose `PROC_ROOT` two defaults bind when they are defined; every default is
  the object it was before.
- **Seams (rule 24):** everything the moved code reads of `notify_listener`
  when it runs -- each other, `read_process`, and `PROC_ROOT` where a body
  compares against it -- is read through it, so a patch there reaches it;
  `role_runtime` and `role_pane_entry` build their store from
  `notify_listener` when they run. Uses are counted across the modules.
- **The behaviour is the baseline's:** the turn-start carry, the store writing
  and reading and refusing, seven malformed records, the authority report in
  every shape, liveness on a fake `/proc`, live and registered targets, the
  offline check, and nine names rebound on `notify_listener`. `GOLDEN` below was
  produced by running the BASELINE module's own definitions over the very cases
  embedded here (`gold479.py`), not typed; it is byte-identical under `env -i`,
  in a normal role pane, with another HOME, USER, COLUMNS, TMPDIR, project and
  pane-state directory, under umask 077 and under several hash seeds.
- **The entry point answers as before:** the installed
  `ticket-board-notify-listener --help` and its offline pane-state authority
  check, and the same check through the package.

No real home, tenant, account, /etc, /var, /opt or /proc path is read or
written -- a recorder with a positive control for each, through stat, open and
pathlib alike, shows it -- no process is signalled (`pid_exists` is a
stand-in), and nothing reaches the board (assignments come from a file). Spawns,
every exec, signals, account and group lookups and socket connections are
refused for each case.
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

# notify_listener first, as the listener does: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import notify_listener as t  # noqa: E402,I001
from scripts.ticket_board import pane_state as m  # noqa: E402

CHECKS = 0
MOVED = ('DEFAULT_PANE_STATE_DIR', 'PaneHookState', 'TURN_START_EVENTS', 'hook_turn_started_at', 'PaneStateAuthority', 'pane_state_authority', '_pid_exists', 'assignment_process_gone', 'live_pane_targets', 'registered_pane_targets', 'PaneHookStateStore', 'load_runtime_assignments', 'verify_pane_state_authority')
#: Measured on the baseline notify_listener: each moved function's and method's call-time reads of its globals.
SEAMS = {
    'hook_turn_started_at': {'TURN_START_EVENTS': 1},
    'pane_state_authority': {'PaneStateAuthority': 1},
    'assignment_process_gone': {'PROC_ROOT': 1, 'read_process': 1},
    'live_pane_targets': {'assignment_process_gone': 1},
    'PaneHookStateStore.read': {'PaneHookState': 1},
    'PaneHookStateStore.write': {'hook_turn_started_at': 1},
    'verify_pane_state_authority': {'PaneHookStateStore': 1, 'live_pane_targets': 1, 'load_runtime_assignments': 1, 'pane_state_authority': 1},
}
#: Every moved function and method, as `name` or `Class.method`.
FUNCTIONS = ('hook_turn_started_at', 'PaneStateAuthority.ok', 'PaneStateAuthority.describe', 'PaneStateAuthority._describe_registered', 'pane_state_authority', '_pid_exists', 'assignment_process_gone', 'live_pane_targets', 'registered_pane_targets', 'PaneHookStateStore.__init__', 'PaneHookStateStore._target_path', 'PaneHookStateStore.read', 'PaneHookStateStore.write', 'load_runtime_assignments', 'verify_pane_state_authority')
#: Measured on the baseline: every notify_listener definition outside the thirteen that names them, and how often.
DISPATCH = {'_build_parser': {'DEFAULT_PANE_STATE_DIR': 2}, 'main': {'PaneHookStateStore': 1, 'pane_state_authority': 1, 'verify_pane_state_authority': 1}}
#: Measured on the baseline, by AST: every production module that imports them from notify_listener, and how often.
READERS = {'scripts/role_pane_entry.py': {'import PaneHookStateStore': 1}, 'scripts/role_runtime.py': {'import PaneHookStateStore': 1}}
#: The constants and the defaults, as the baseline wrote them. Measured on the baseline, not typed.
CONSTANT_TEXT = {
    'DEFAULT_PANE_STATE_DIR': (None, "Path(os.environ['TICKET_BOARD_PANE_STATE_DIR']).expanduser() if os.environ.get('TICKET_BOARD_PANE_STATE_DIR') else Path(os.environ['PGU_TICKET_BOARD_PANE_STATE_DIR']).expanduser() if os.environ.get('PGU_TICKET_BOARD_PANE_STATE_DIR') else Path(f'/run/user/{os.getuid()}/pgu-ticket-board/pane-state')"),
    'TURN_START_EVENTS': (None, "frozenset({'UserPromptSubmit', 'PreInvocation', 'pre_llm_call'})"),
}
DEFAULTS = {
    'hook_turn_started_at': [],
    'PaneStateAuthority.ok': [],
    'PaneStateAuthority.describe': [],
    'PaneStateAuthority._describe_registered': [],
    'pane_state_authority': ['()'],
    '_pid_exists': [],
    'assignment_process_gone': ['PROC_ROOT', '_pid_exists'],
    'live_pane_targets': ['PROC_ROOT'],
    'registered_pane_targets': [],
    'PaneHookStateStore.__init__': ['DEFAULT_PANE_STATE_DIR'],
    'PaneHookStateStore._target_path': [],
    'PaneHookStateStore.read': [],
    'PaneHookStateStore.write': ["''", 'None'],
    'load_runtime_assignments': [],
    'verify_pane_state_authority': [],
}
#: The BASELINE's own behaviour for the cases below (`gold479.py`, run on the baseline notify_listener under the guard).
GOLDEN = {
    'turn start: a busy turn start': {'result': {'type': 'float', 'value': 100.0}, 'calls': {}},
    'turn start: a gemini start': {'result': {'type': 'float', 'value': 100.0}, 'calls': {}},
    'turn start: busy but not a start': {'result': {'type': 'float', 'value': 5.0}, 'calls': {}},
    'turn start: carried through idle': {'result': {'type': 'float', 'value': 7.5}, 'calls': {}},
    'turn start: an unreadable carried value': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'turn start: nothing before': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'turn start: no source': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'store: the directory and the default': {'result': {'type': 'list', 'value': ['ROOT/state', True]}, 'calls': {}},
    'store: writes and reads': {'result': {'type': 'list', 'value': [{'__type__': 'PaneHookState', 'target': 'p-main:0.0', 'state': 'idle', 'updated_at': 20.0, 'source': 'claude.Stop', 'turn_started_at': 10.0}, {'__type__': 'PaneHookState', 'target': 'p-audit:0.0', 'state': 'blocked', 'updated_at': 30.0, 'source': 'codex.PermissionRequest', 'turn_started_at': None}, None, 'we_ird_target_1.2.json', ['p-audit_0.0.json', 'p-main_0.0.json', 'we_ird_target_1.2.json'], '{"source": "claude.UserPromptSubmit", "state": "busy", "target": "p-case:0.0", "turn_started_at": 55.0, "updated_at": 55.0}\n', 'refused: pane hook state must be idle, busy, or blocked']}, 'calls': {'hook_turn_started_at': 2}},
    'store: a home-relative directory': {'result': {'type': 'PosixPath', 'value': 'ROOT/pane-state'}, 'calls': {}},
    'store: a record with not json': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'store: a record with not an object': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'store: a record with no state': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'store: a record with an unknown state': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'store: a record with no time': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'store: a record with a bad turn start': {'result': {'type': 'PaneHookState', 'value': {'__type__': 'PaneHookState', 'target': 'q-main:0.0', 'state': 'idle', 'updated_at': 1.0, 'source': '', 'turn_started_at': None}}, 'calls': {}},
    'store: a record with no target': {'result': {'type': 'PaneHookState', 'value': {'__type__': 'PaneHookState', 'target': 'q-main:0.0', 'state': 'idle', 'updated_at': 3.5, 'source': '', 'turn_started_at': None}}, 'calls': {}},
    'store: a record with a target of its own': {'result': {'type': 'PaneHookState', 'value': {'__type__': 'PaneHookState', 'target': 'elsewhere:1.0', 'state': 'idle', 'updated_at': 1.0, 'source': '', 'turn_started_at': None}}, 'calls': {}},
    'authority: every role served': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/state', 'registered': ['p-main:0.0', 'p-audit:0.0'], 'with_state': ['p-main:0.0', 'p-audit:0.0'], 'without_state': [], 'stale': []}, True, 'pane-state authority: ROOT/state holds hook state for 2 of 2 registered roles']}, 'calls': {}},
    'authority: some roles served': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/state', 'registered': ['p-main:0.0', 'p-perf:0.0'], 'with_state': ['p-main:0.0'], 'without_state': ['p-perf:0.0'], 'stale': []}, True, 'pane-state authority: ROOT/state holds hook state for 1 of 2 registered roles; no state yet for p-perf:0.0']}, 'calls': {}},
    'authority: none served': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/state', 'registered': ['x-main:0.0'], 'with_state': [], 'without_state': ['x-main:0.0'], 'stale': []}, False, 'pane-state authority: ROOT/state holds hook state for none of the 1 registered roles (x-main:0.0). The listener and the pane hooks are reading and writing different directories, so every pane reads as busy and nothing is delivered.']}, 'calls': {}},
    'authority: no roles': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/state', 'registered': [], 'with_state': [], 'without_state': [], 'stale': []}, True, 'pane-state authority: no registered roles; nothing to serve from ROOT/state']}, 'calls': {}},
    'authority: no live roles, some stale': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/state', 'registered': [], 'with_state': [], 'without_state': [], 'stale': [['p-old:0.0', 'pid 9 no longer exists']]}, True, 'pane-state authority: no live registered roles; nothing to serve from ROOT/state; 1 stale assignment(s) not counted, their process is gone: p-old:0.0 (pid 9 no longer exists)']}, 'calls': {}},
    'authority: served and stale': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/state', 'registered': ['p-main:0.0'], 'with_state': ['p-main:0.0'], 'without_state': [], 'stale': [['p-old:0.0', 'pid 9 no longer exists']]}, True, 'pane-state authority: ROOT/state holds hook state for 1 of 1 registered roles; 1 stale assignment(s) not counted, their process is gone: p-old:0.0 (pid 9 no longer exists)']}, 'calls': {}},
    'authority: a missing directory': {'result': {'type': 'list', 'value': [{'__type__': 'PaneStateAuthority', 'state_dir': 'ROOT/nowhere', 'registered': ['p-main:0.0'], 'with_state': [], 'without_state': ['p-main:0.0'], 'stale': []}, False, 'pane-state authority: ROOT/nowhere holds hook state for none of the 1 registered roles (p-main:0.0). The listener and the pane hooks are reading and writing different directories, so every pane reads as busy and nothing is delivered.']}, 'calls': {}},
    'gone: not a record': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {}},
    'gone: no process': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {}},
    'gone: pid 1': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {}},
    'gone: a bad pid': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {}},
    'gone: alive, same start': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {'read_process': 1}},
    'gone: alive, no start recorded': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {'read_process': 1}},
    'gone: reused': {'result': {'type': 'list', 'value': ['pid 4242 was reused: started at 777, not 776', []]}, 'calls': {'read_process': 1}},
    'gone: a bad start': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {'read_process': 1}},
    'gone: unreadable but present': {'result': {'type': 'list', 'value': ['', []]}, 'calls': {'read_process': 1}},
    'gone: gone': {'result': {'type': 'list', 'value': ['pid 9999 no longer exists', []]}, 'calls': {'read_process': 1}},
    'pid exists: no such process': {'result': {'type': 'list', 'value': [False, [[4242, 0]]]}, 'calls': {}},
    'pid exists: not permitted': {'result': {'type': 'list', 'value': [True, [[4242, 0]]]}, 'calls': {}},
    'pid exists: another error': {'result': {'type': 'list', 'value': [None, [[4242, 0]]]}, 'calls': {}},
    'pid exists: present': {'result': {'type': 'list', 'value': [True, [[4242, 0]]]}, 'calls': {}},
    'live targets': {'result': {'type': 'tuple', 'value': [['p-main:0.0', 'p-audit:0.0'], [['p-perf:0.0', 'pid 9999 no longer exists']]]}, 'calls': {'assignment_process_gone': 4, 'read_process': 3}},
    'live targets: not a payload': {'result': {'type': 'tuple', 'value': [[], []]}, 'calls': {}},
    'registered targets': {'result': {'type': 'tuple', 'value': ['p-main:0.0', 'p-perf:0.0', 'p-audit:0.0']}, 'calls': {}},
    'registered targets: none': {'result': {'type': 'tuple', 'value': []}, 'calls': {}},
    'load: from a file': {'result': {'type': 'dict', 'value': {'assignments': {'audit': {'actual_target': 'p-audit:0.0'}, 'main': {'actual_target': 'p-main:0.0'}}}}, 'calls': {}},
    'load: nothing to read': {'result': {'raised': 'SystemExit', 'message': 'ticket notify listener: --verify-pane-state-authority needs --board-url or TICKET_BOARD_URL'}, 'calls': {}},
    'verify: served': {'result': {'type': 'list', 'value': [0, 'pane-state authority: ROOT/state holds hook state for 2 of 2 registered roles\n', '']}, 'calls': {'assignment_process_gone': 2, 'live_pane_targets': 1, 'load_runtime_assignments': 1, 'pane_state_authority': 1}},
    'verify: not served': {'result': {'type': 'list', 'value': [1, 'pane-state authority: ROOT/nowhere holds hook state for none of the 2 registered roles (p-main:0.0, p-audit:0.0). The listener and the pane hooks are reading and writing different directories, so every pane reads as busy and nothing is delivered.\n', 'the listener would defer every notification while looking healthy; point TICKET_BOARD_PANE_STATE_DIR at the directory the installed pane hooks write to\n']}, 'calls': {'assignment_process_gone': 2, 'live_pane_targets': 1, 'load_runtime_assignments': 1, 'pane_state_authority': 1}},
    'constants': {'result': {'type': 'list', 'value': [['PreInvocation', 'UserPromptSubmit', 'pre_llm_call'], True]}, 'calls': {}},
    'rebound on notify_listener: TURN_START_EVENTS': {'result': {'type': 'float', 'value': 1.0}, 'calls': {}},
    'rebound on notify_listener: PaneHookState': {'result': {'type': 'tuple', 'value': ['REBOUND', 'idle']}, 'calls': {}},
    'rebound on notify_listener: hook_turn_started_at': {'result': {'type': 'list', 'value': ['p-perf_0.0.json', {'__type__': 'PaneHookState', 'target': 'p-perf:0.0', 'state': 'idle', 'updated_at': 60.0, 'source': '', 'turn_started_at': 4242.0}]}, 'calls': {'hook_turn_started_at rebound': 1}},
    'rebound on notify_listener: PaneStateAuthority': {'result': {'type': 'tuple', 'value': ['REBOUND', ['p-main:0.0']]}, 'calls': {}},
    'rebound on notify_listener: read_process': {'result': {'type': 'str', 'value': 'pid 9999 was reused: started at 1, not 2'}, 'calls': {'read_process rebound': 1}},
    'rebound on notify_listener: PROC_ROOT': {'result': {'type': 'list', 'value': ['', [9999]]}, 'calls': {'read_process': 1}},
    'rebound on notify_listener: assignment_process_gone': {'result': {'type': 'tuple', 'value': [[], [['p-main:0.0', 'REBOUND'], ['p-perf:0.0', 'REBOUND'], ['p-audit:0.0', 'REBOUND']]]}, 'calls': {'assignment_process_gone rebound': 4}},
    'rebound on notify_listener: load_runtime_assignments': {'result': {'type': 'list', 'value': [0, 'pane-state authority: no registered roles; nothing to serve from ROOT/state\n', '']}, 'calls': {'live_pane_targets': 1, 'load_runtime_assignments rebound': 1, 'pane_state_authority': 1}},
    'rebound on notify_listener: pane_state_authority': {'result': {'type': 'list', 'value': [1, 'REBOUND\n', 'the listener would defer every notification while looking healthy; point TICKET_BOARD_PANE_STATE_DIR at the directory the installed pane hooks write to\n']}, 'calls': {'assignment_process_gone': 2, 'live_pane_targets': 1, 'load_runtime_assignments': 1, 'pane_state_authority rebound': 1}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package and peer_identity, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board', 'scripts.ticket_board.peer_identity']
#: The module's imports at load: the standard library, and the one name two defaults bind.
MODULE_IMPORTS = ["from __future__ import annotations", "import argparse", "import json", "import os", "import sys", "import time", "from dataclasses import dataclass",
                  "from pathlib import Path", "from typing import Any, Callable, Iterable", "from .peer_identity import PROC_ROOT"]
#: The call-time import every function and method that reads notify_listener starts with.
CALL_TIME_IMPORT = "from . import notify_listener as listener"

# --- the cases, shared verbatim with `gold479.py` (which ran them on the baseline) ------------------------------------
# A case drives hook storage, the authority report, runtime-assignment liveness or the offline check on a directory and a
# fake /proc made fresh under /tmp, and records the answer (or the exception) and how many times it reached each helper on
# `notify_listener` (recorded there and passed through). No real /proc entry is read -- every liveness case passes the
# fake proc_root -- no process is signalled (pid_exists is a stand-in; the one case that runs `_pid_exists` itself hands
# it an `os` whose kill only records), and nothing reaches the board. Paths under the case directory are recorded as ROOT.
TURN_STARTS = {
    "a busy turn start": (None, "busy", "claude.UserPromptSubmit"),
    "a gemini start": ({"turn_started_at": 5}, "busy", "agy.PreInvocation"),
    "busy but not a start": ({"turn_started_at": 5}, "busy", "claude.Notification"),
    "carried through idle": ({"turn_started_at": "7.5"}, "idle", "claude.Stop"),
    "an unreadable carried value": ({"turn_started_at": "soon"}, "idle", "claude.Stop"),
    "nothing before": (None, "idle", "claude.Stop"),
    "no source": (None, "busy", ""),
}
MALFORMED = {"not json": "{", "not an object": "[1, 2]", "no state": '{"updated_at": 1}', "an unknown state": '{"state": "asleep", "updated_at": 1}',
             "no time": '{"state": "idle"}', "a bad turn start": '{"state": "idle", "updated_at": 1, "turn_started_at": "x"}',
             "no target": '{"state": "IDLE", "updated_at": "3.5", "source": null}',
             "a target of its own": '{"target": "elsewhere:1.0", "state": "idle", "updated_at": 1}'}
AUTHORITY = {"every role served": (["p-main:0.0", "p-audit:0.0"], ()), "some roles served": (["p-main:0.0", "p-perf:0.0", "p-main:0.0", ""], ()),
             "none served": (["x-main:0.0"], ()), "no roles": ([], ()), "no live roles, some stale": ([], [("p-old:0.0", "pid 9 no longer exists")]),
             "served and stale": (["p-main:0.0"], [("p-old:0.0", "pid 9 no longer exists")])}
GONE = {"not a record": "x", "no process": {"actual_target": "p-main:0.0"}, "pid 1": {"process_pid": 1}, "a bad pid": {"process_pid": "x"},
        "alive, same start": {"process_pid": 4242, "process_start_time": 777}, "alive, no start recorded": {"process_pid": 4242},
        "reused": {"process_pid": 4242, "process_start_time": 776}, "a bad start": {"process_pid": 4242, "process_start_time": "later"},
        "unreadable but present": {"process_pid": 4444}, "gone": {"process_pid": 9999}}
PID_EXISTS = {"no such process": ProcessLookupError, "not permitted": PermissionError, "another error": OSError, "present": None}
PAYLOAD = {"assignments": {"main": {"actual_target": "p-main:0.0", "process_pid": 4242, "process_start_time": 777}, "perf": {"actual_target": "p-perf:0.0", "process_pid": 9999},
                           "audit": {"actual_target": "p-audit:0.0"}, "dup": {"actual_target": "p-main:0.0", "process_pid": 9999}, "blank": {"actual_target": " "}, "bad": "x"}}
REBINDS = ("TURN_START_EVENTS", "PaneHookState", "hook_turn_started_at", "PaneStateAuthority", "read_process", "PROC_ROOT", "assignment_process_gone",
           "load_runtime_assignments", "pane_state_authority")
PASSED = ("hook_turn_started_at", "assignment_process_gone", "read_process", "live_pane_targets", "load_runtime_assignments", "pane_state_authority")
CASES = {
    **{f"turn start: {label}": {"call": "turn start", "key": label} for label in TURN_STARTS},
    "store: the directory and the default": {"call": "store directory"},
    "store: writes and reads": {"call": "store sequence"},
    "store: a home-relative directory": {"call": "store home"},
    **{f"store: a record with {label}": {"call": "store malformed", "key": label} for label in MALFORMED},
    **{f"authority: {label}": {"call": "authority", "key": label} for label in AUTHORITY},
    "authority: a missing directory": {"call": "authority", "key": "missing"},
    **{f"gone: {label}": {"call": "gone", "key": label} for label in GONE},
    **{f"pid exists: {label}": {"call": "pid exists", "key": label} for label in PID_EXISTS},
    "live targets": {"call": "live", "payload": PAYLOAD},
    "live targets: not a payload": {"call": "live", "payload": ["x"]},
    "registered targets": {"call": "registered", "payload": PAYLOAD},
    "registered targets: none": {"call": "registered", "payload": {"assignments": []}},
    "load: from a file": {"call": "load", "file": True},
    "load: nothing to read": {"call": "load", "file": False},
    "verify: served": {"call": "verify", "served": True},
    "verify: not served": {"call": "verify", "served": False},
    "constants": {"call": None},
    **{f"rebound on notify_listener: {name}": {"call": "rebound", "key": name} for name in REBINDS},
}


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definitions; `t` is notify_listener, whose helpers are recorded and passed through."""
    import argparse as _ap, contextlib as _cl, dataclasses, io as _io, json as _json, os as _os, pathlib as _pl, shutil as _sh, tempfile as _tf, types as _types
    counts = {}
    root = _pl.Path(_tf.mkdtemp(prefix="syrd479-case-", dir="/tmp"))

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"__type__": type(value).__name__, **{k: norm(v) for k, v in dataclasses.asdict(value).items()}}
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(norm(v) for v in value)
        if isinstance(value, _pl.PurePath):
            return norm(str(value))
        if isinstance(value, str):
            return value.replace(str(root), "ROOT")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return norm(repr(value))

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    saved = {n: getattr(t, n) for n in (*PASSED, *REBINDS)}
    fn = lambda name: saved[name] if holder is t and name in saved else getattr(holder, name)
    seen = []
    pid_exists = lambda pid: seen.append(pid) or {5555: False, 6666: True}.get(pid)
    try:
        proc = root / "proc"
        for pid, start in ((4242, 777), (4343, 888)):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "stat").write_text(f"{pid} (claude) S 1 " + " ".join(["0"] * 17) + f" {start}\n")
        (proc / "4444").mkdir()
        (root / "assignments.json").write_text(_json.dumps({"assignments": {"main": {"actual_target": "p-main:0.0"}, "audit": {"actual_target": "p-audit:0.0"}}}))
        store = fn("PaneHookStateStore")(root / "state")
        store.write("p-main:0.0", "busy", source="claude.UserPromptSubmit", now=10.0)
        store.write("p-main:0.0", "idle", source="claude.Stop", now=20.0)
        store.write("p-audit:0.0", "blocked", source="codex.PermissionRequest", now=30.0)
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))

        def verify(assignments_json, state_dir):
            out, err = _io.StringIO(), _io.StringIO()
            with _cl.redirect_stdout(out), _cl.redirect_stderr(err):
                rc = fn("verify_pane_state_authority")(_ap.Namespace(board_url="", assignments_json=assignments_json, pane_state_dir=str(state_dir)))
            return [rc, out.getvalue(), err.getvalue()]

        call, key = spec["call"], spec.get("key")
        if call is None:
            got = [holder.TURN_START_EVENTS, isinstance(holder.DEFAULT_PANE_STATE_DIR, _pl.Path) and holder.DEFAULT_PANE_STATE_DIR.is_absolute()]
        elif call == "turn start":
            previous, state, source = TURN_STARTS[key]
            got = fn("hook_turn_started_at")(previous, state, source, 100.0)
        elif call == "store directory":
            got = [store.state_dir, fn("PaneHookStateStore")().state_dir == holder.DEFAULT_PANE_STATE_DIR]
        elif call == "store sequence":
            got = [store.read("p-main:0.0"), store.read("p-audit:0.0"), store.read("p-none:0.0"),
                   store.write("we/ird target:1.2", "idle", now=50.0).name, sorted(p.name for p in (root / "state").iterdir())]
            got.append((root / "state" / store.write("p-case:0.0", " Busy ", source="claude.UserPromptSubmit", now=55.0).name).read_text())
            try:
                store.write("p-main:0.0", "asleep", now=60.0)
            except ValueError as exc:
                got.append(f"refused: {exc}")
        elif call == "store home":
            home = _os.environ.get("HOME")
            _os.environ["HOME"] = str(root)
            try:
                got = fn("PaneHookStateStore")("~/pane-state").state_dir
            finally:
                if home is None:
                    _os.environ.pop("HOME", None)
                else:
                    _os.environ["HOME"] = home
        elif call == "store malformed":
            (root / "state" / "q-main_0.0.json").write_text(MALFORMED[key])
            got = store.read("q-main:0.0")
        elif call == "authority":
            if key == "missing":
                a = fn("pane_state_authority")(["p-main:0.0"], fn("PaneHookStateStore")(root / "nowhere"))
            else:
                targets, stale = AUTHORITY[key]
                a = fn("pane_state_authority")(targets, store, stale=stale)
            got = [a, a.ok, a.describe()]
        elif call == "gone":
            got = [fn("assignment_process_gone")(GONE[key], proc_root=proc, pid_exists=pid_exists), seen]
        elif call == "pid exists":
            check = fn("_pid_exists")
            real_os = check.__globals__["os"]

            def kill(pid, sig):
                seen.append([pid, sig])
                if PID_EXISTS[key] is not None:
                    raise PID_EXISTS[key]()

            check.__globals__["os"] = _types.SimpleNamespace(kill=kill)
            try:
                got = [check(4242), seen]
            finally:
                check.__globals__["os"] = real_os
        elif call == "live":
            got = fn("live_pane_targets")(spec["payload"], proc_root=proc)
        elif call == "registered":
            got = fn("registered_pane_targets")(spec["payload"])
        elif call == "load":
            got = fn("load_runtime_assignments")(board_url="", assignments_json=str(root / "assignments.json") if spec["file"] else "")
        elif call == "verify":
            got = verify(str(root / "assignments.json"), root / ("state" if spec["served"] else "nowhere"))
        else:
            value, act = {
                "TURN_START_EVENTS": (frozenset({"Notification"}), lambda: fn("hook_turn_started_at")(None, "busy", "claude.Notification", 1.0)),
                "PaneHookState": (lambda **k: ("REBOUND", k["state"]), lambda: store.read("p-main:0.0")),
                "hook_turn_started_at": (lambda *a: note("hook_turn_started_at rebound") or 4242.0, lambda: [store.write("p-perf:0.0", "idle", now=60.0).name, store.read("p-perf:0.0")]),
                "PaneStateAuthority": (lambda **k: ("REBOUND", k["registered"]), lambda: fn("pane_state_authority")(["p-main:0.0"], store)),
                "read_process": (lambda pid, proc_root: note("read_process rebound") or _types.SimpleNamespace(start_time=1),
                                 lambda: fn("assignment_process_gone")({"process_pid": 9999, "process_start_time": 2}, proc_root=proc, pid_exists=pid_exists)),
                "PROC_ROOT": (proc, lambda: [fn("assignment_process_gone")({"process_pid": 9999}, proc_root=proc, pid_exists=pid_exists), seen]),
                "assignment_process_gone": (lambda a, proc_root: note("assignment_process_gone rebound") or "REBOUND", lambda: fn("live_pane_targets")(PAYLOAD, proc_root=proc)),
                "load_runtime_assignments": (lambda **k: note("load_runtime_assignments rebound") or {"assignments": {}}, lambda: verify("", root / "state")),
                "pane_state_authority": (lambda live, store, stale=(): note("pane_state_authority rebound") or _types.SimpleNamespace(ok=False, describe=lambda: "REBOUND"),
                                         lambda: verify(str(root / "assignments.json"), root / "state")),
            }[key]
            setattr(t, key, value)
            got = act()
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        _sh.rmtree(root)
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


#: Refuses and records any stat, lstat, open (builtin, io and os), access, listdir or scandir under the host's switchyard,
#: /etc, /var, /opt and /proc paths, after proving it catches one of each kind it matters for (a positive control).
HOST_RECORDER = (
    "import builtins, io, os, pathlib\n"
    "PREFIXES = ('/usr/local/lib/switchyard', '/etc', '/var', '/opt', '/proc')\n"
    "hits = []\n"
    "def guarded(real, name):\n"
    "    def f(p, *a, **k):\n"
    "        s = os.fsdecode(p) if isinstance(p, (str, bytes, os.PathLike)) else ''\n"
    "        if s.startswith(PREFIXES):\n"
    "            hits.append((name, s)); raise FileNotFoundError(2, 'refused', s)\n"
    "        return real(p, *a, **k)\n"
    "    return f\n"
    "os.stat, os.lstat, builtins.open, os.access = guarded(os.stat, 'stat'), guarded(os.lstat, 'lstat'), guarded(builtins.open, 'open'), guarded(os.access, 'access')\n"
    "io.open, os.open, os.listdir, os.scandir = guarded(io.open, 'io.open'), guarded(os.open, 'os.open'), guarded(os.listdir, 'listdir'), guarded(os.scandir, 'scandir')\n"
    "pathlib.Path('/opt/switchyard/syrd479-positive-control').is_file()\n"
    "try: pathlib.Path('/proc/self/stat').read_text()\n"
    "except FileNotFoundError: pass\n"
    "try: list(pathlib.Path('/proc').iterdir())\n"
    "except FileNotFoundError: pass\n"
    "control = len(hits); hits.clear()\n"
)
#: One probe, run in both import forms: the store, the authority report, liveness on a fake /proc and the offline check,
#: with names rebound on notify_listener -- as a digest, and the host paths looked at.
STATE_PROBE = (
    "import argparse, contextlib, hashlib, io, json, pathlib, shutil, tempfile, types\n"
    "root = pathlib.Path(tempfile.mkdtemp(prefix='syrd479-probe-', dir='/tmp'))\n"
    "proc = root / 'proc'; (proc / '4242').mkdir(parents=True)\n"
    "(proc / '4242' / 'stat').write_text('4242 (claude) S 1 ' + ' '.join(['0'] * 17) + ' 777\\n')\n"
    "store = nl.PaneHookStateStore(root / 'state')\n"
    "store.write('p-main:0.0', 'busy', source='claude.UserPromptSubmit', now=10.0); store.write('p-main:0.0', 'idle', source='claude.Stop', now=20.0)\n"
    "# No process id: the offline check reads liveness under the real PROC_ROOT, which nothing here may touch.\n"
    "(root / 'a.json').write_text(json.dumps({'assignments': {'main': {'actual_target': 'p-main:0.0'}}}))\n"
    "pid = lambda p: False\n"
    "def answers():\n"
    "    out = io.StringIO()\n"
    "    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):\n"
    "        rc = nl.verify_pane_state_authority(argparse.Namespace(board_url='', assignments_json=str(root / 'a.json'), pane_state_dir=str(root / 'state')))\n"
    "    return [repr(store.read('p-main:0.0')), nl.pane_state_authority(['p-main:0.0', 'p-x:0.0'], store).describe(),\n"
    "            nl.assignment_process_gone({'process_pid': 4242, 'process_start_time': 1}, proc_root=proc, pid_exists=pid),\n"
    "            nl.live_pane_targets({'assignments': {'m': {'actual_target': 'p-main:0.0', 'process_pid': 9}}}, proc_root=proc), rc, out.getvalue()]\n"
    "out = [answers()]\n"
    "for name, value in (('read_process', lambda p, proc_root: types.SimpleNamespace(start_time=1)), ('PaneHookState', lambda **k: ('R', k['state'])),\n"
    "                    ('assignment_process_gone', lambda a, **k: 'R'), ('pane_state_authority', lambda l, s, stale=(): types.SimpleNamespace(ok=True, describe=lambda: 'R'))):\n"
    "    saved = getattr(nl, name); setattr(nl, name, value)\n"
    "    try: out.append(answers())\n"
    "    finally: setattr(nl, name, saved)\n"
    "shutil.rmtree(root)\n"
    "text = json.dumps(out).replace(str(root), 'ROOT')\n"
    "print(len(out), len(set(json.dumps(o).replace(str(root), 'ROOT') for o in out)), hashlib.sha256(text.encode()).hexdigest(), 'control', control, 'host paths', len(hits))\n"
)


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.ticket_board.pane_state as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package and peer_identity, never notify_listener: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_same_defaults() -> None:
    for order in (("scripts.ticket_board.pane_state", "scripts.ticket_board.notify_listener"),
                  ("scripts.ticket_board.notify_listener", "scripts.ticket_board.pane_state"),
                  ("scripts.ticket_board.pane_activity_gate", "scripts.ticket_board.pane_state"),
                  ("scripts.role_runtime", "scripts.ticket_board.pane_state")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.notify_listener as t, scripts.ticket_board.pane_state as m, scripts.ticket_board.peer_identity as pi; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {MOVED!r} if callable(getattr(m, n))}}), "
                        "m.PaneHookStateStore.__init__.__defaults__[0] is m.DEFAULT_PANE_STATE_DIR, "
                        "m.assignment_process_gone.__kwdefaults__['proc_root'] is pi.PROC_ROOT is t.PROC_ROOT, m.assignment_process_gone.__kwdefaults__['pid_exists'] is m._pid_exists, "
                        "m.live_pane_targets.__kwdefaults__['proc_root'] is pi.PROC_ROOT, t.read_process is pi.read_process, "
                        "not any(hasattr(m, n) for n in ('listener', 'notify_listener', 'read_process', 'TicketBoardNotifyListener', 'PaneActivityGate', 'LOGGER')))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.pane_state'] True True True True True True",
              f"{' then '.join(order)}: one object each, defined here; every default the object it bound before; nothing of notify_listener bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.json is json, "the standard-library names are the module's own, the very objects notify_listener holds")


def test_the_installed_import_form_holds_its_own_one_set_and_reads_through_its_own_listener() -> None:
    result = python(f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                    "import ticket_board.notify_listener as nl, ticket_board.pane_state as p, scripts.ticket_board.notify_listener as other\n"
                    f"print(all(getattr(nl, n) is getattr(p, n) for n in {MOVED!r}), p.PaneHookStateStore.__module__, nl.PaneHookStateStore is not other.PaneHookStateStore)\n"
                    "saved = nl.PaneStateAuthority; nl.PaneStateAuthority = lambda **k: 'THROUGH ticket_board'\n"
                    "print(nl.pane_state_authority([], nl.PaneHookStateStore('/tmp/syrd479-nowhere'))); nl.PaneStateAuthority = saved")
    check(result.returncode == 0 and result.stdout.splitlines() == ["True ticket_board.pane_state True", "THROUGH ticket_board"],
          f"the installed form: its own objects, and its code reads its own notify_listener when it runs: {result.stdout}{result.stderr[-600:]}")


def test_both_import_forms_answer_alike_and_look_at_no_host_path() -> None:
    package = python(HOST_RECORDER + "import scripts.ticket_board.notify_listener as nl\n" + STATE_PROBE)
    installed = python(HOST_RECORDER + f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\nimport ticket_board.notify_listener as nl\n" + STATE_PROBE)
    check(package.returncode == installed.returncode == 0 and package.stdout == installed.stdout and package.stdout.startswith("5 5 ")
          and package.stdout.strip().endswith(" control 3 host paths 0"),
          f"the package and the installed form give the same five answers, every rebind a different one, with no host path or /proc entry looked at (all "
          f"three positive controls caught): {package.stdout}{package.stderr[-400:]} | {installed.stdout}{installed.stderr[-400:]}")


def test_the_readers_build_their_store_from_notify_listener_when_they_run() -> None:
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        imports = [x for x in ast.walk(source) if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("notify_listener")]
        inside = {id(x) for fn in ast.walk(source) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn)}
        got: dict[str, int] = {}
        for x in imports:
            for a in x.names:
                if a.name in MOVED:
                    got[f"import {a.name}"] = got.get(f"import {a.name}", 0) + 1
        check(got == counts and all(id(x) in inside for x in imports), f"{path} imports them from notify_listener, inside its functions, as often as before: {got}")
    gate = ast.parse((ROOT / "scripts" / "ticket_board" / "pane_activity_gate.py").read_text(encoding="utf-8"))
    through = sorted({x.attr for x in ast.walk(gate) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "listener" and x.attr in MOVED})
    check(through == ["PaneHookStateStore", "TURN_START_EVENTS"], f"the pane activity gate reads the store and the turn-start events through notify_listener: {through}")


def test_the_seams_read_through_notify_listener_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "pane_state.py").read_text(encoding="utf-8"))
    by_name = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for label in FUNCTIONS:
        owner, _, method = label.partition(".")
        node = next(f for f in by_name[owner].body if isinstance(f, ast.FunctionDef) and f.name == method) if method else by_name[owner]
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "listener":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(label, {})
        check(through == expected, f"{label}: each notify_listener name, its siblings included, read through it exactly as often as before: {through}")
        first = 1 if ast.get_docstring(node) is not None else 0
        own = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and own[0] == CALL_TIME_IMPORT and own.count(CALL_TIME_IMPORT) == 1,
                  f"{label}: notify_listener imported first thing (after its docstring), once: {own}")
        else:
            check(CALL_TIME_IMPORT not in own, f"{label}: reads nothing of notify_listener: {own}")
        skip = set()
        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [v for v in (node.args.vararg, node.args.kwarg) if v]:
            if a.annotation is not None:
                skip |= {id(y) for y in ast.walk(a.annotation)}
        for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None] + ([node.returns] if node.returns is not None else []):
            skip |= {id(y) for y in ast.walk(d)}
        skip |= {id(y) for x in ast.walk(node) if isinstance(x, ast.AnnAssign) for y in ast.walk(x.annotation)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and id(x) not in skip
                       and (x.id in expected or x.id in MOVED or x.id in ("read_process", "PROC_ROOT"))})
        check(bare == [], f"{label}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == MODULE_IMPORTS and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try))], f"the standard library and PROC_ROOT only, at load: {top}")
    consts = {n.targets[0].id if isinstance(n, ast.Assign) else n.target.id: (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value))
              for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == {k: tuple(v) for k, v in CONSTANT_TEXT.items()}, f"the constants are the baseline's: {sorted(set(consts) ^ set(CONSTANT_TEXT))}")
    defaults = {}
    for n in tree.body:
        for label, f in ([(n.name, n)] if isinstance(n, ast.FunctionDef) else [(f"{n.name}.{f.name}", f) for f in n.body if isinstance(f, ast.FunctionDef)] if isinstance(n, ast.ClassDef) else []):
            defaults[label] = [ast.unparse(d) for d in f.args.defaults + [d for d in f.args.kw_defaults if d is not None]]
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {sorted(k for k in DEFAULTS if defaults.get(k) != DEFAULTS[k])}")
    names = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else n.targets[0].id if isinstance(n, ast.Assign) else n.target.id
             for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the thirteen, in notify_listener's order, and nothing else: {names}")
    source = (ROOT / "scripts" / "ticket_board" / "pane_state.py").read_text(encoding="utf-8")
    check("(SYRD-264)" not in source and "#: Hook events that mean a turn STARTED in the pane" in source,
          "TURN_START_EVENTS came with its own comment, and without the SYRD-264 block run into it above")


def test_notify_listener_reexports_them_and_names_them_as_before() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "notify_listener.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "pane_state" and n.level == 1]
    check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the thirteen, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else n.targets[0].id for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef)) or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name))}
    check(not defined & set(MOVED), f"notify_listener defines none of them: {sorted(defined & set(MOVED))}")
    check({"PROC_ROOT", "read_process", "Iterable", "Path", "Sequence", "hashlib"} <= {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names},
          "notify_listener's own imports are left as they were, PROC_ROOT and read_process -- read through it -- among them")
    source = (ROOT / "scripts" / "ticket_board" / "notify_listener.py").read_text(encoding="utf-8")
    check("#: dead-lettered as a missing pane (SYRD-264).\n#: How long a sent notice has to show up as a turn" in source,
          "the SYRD-264 comment block stays in notify_listener, where the baseline had it, now run into the next constant's comment")
    uses: dict = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(n):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(n.name, {}).setdefault(x.id, 0)
                    uses[n.name][x.id] += 1
    check(uses == DISPATCH, f"notify_listener's own definitions name them exactly as often as before, by the re-exported names: {uses}")
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(loose == [], f"and nothing reads them at module level: {loose}")


def test_the_entry_point_answers_as_before() -> None:
    # The installed wrapper, run as a program (runpy, in a guarded python child): --help, and the offline pane-state
    # authority check -- the moved code -- against synthetic assignments (no process ids, so no /proc entry is read) with
    # a directory that serves both roles and one that serves none; then the same check through the package's main().
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd479-entry-")).resolve()
    try:
        (base / "ok").mkdir(); (base / "empty").mkdir()
        (base / "assignments.json").write_text(json.dumps({"assignments": {"main": {"actual_target": "p479-main:0.0"}, "audit": {"actual_target": "p479-audit:0.0"}}}))
        for target in ("p479-main:0.0", "p479-audit:0.0"):
            (base / "ok" / (target.replace(":", "_") + ".json")).write_text(json.dumps({"target": target, "state": "idle", "updated_at": 1750000000.0, "source": "claude.Stop"}))
        wrapper = ROOT / "scripts" / "ticket-board-notify-listener"

        def entry(*argv: str) -> subprocess.CompletedProcess[str]:
            return python(f"import os, runpy, sys; os.environ['COLUMNS'] = '500'; sys.path.insert(0, {str(ROOT / 'scripts')!r}); sys.argv = [{str(wrapper)!r}, *{list(argv)!r}]\n"
                          f"runpy.run_path({str(wrapper)!r}, run_name='__main__')")

        verify = ["--verify-pane-state-authority", "--assignments-json", str(base / "assignments.json"), "--pane-state-dir"]
        helped, ok, empty = entry("--help"), entry(*verify, str(base / "ok")), entry(*verify, str(base / "empty"))
        through = python(f"import os; os.environ['COLUMNS'] = '500'\nimport scripts.ticket_board.notify_listener as nl\nraise SystemExit(nl.main({verify + [str(base / 'ok')]!r}))")
        check(helped.returncode == 0 and helped.stdout.startswith("usage: ticket-board-notify-listener")
              and all(flag in helped.stdout for flag in ("--pane-state-dir", "--verify-pane-state-authority", "--assignments-json")),
              f"the installed --help: {helped.stdout[-300:]}{helped.stderr[-300:]}")
        check(ok.returncode == 0 and ok.stdout.strip() == f"pane-state authority: {base / 'ok'} holds hook state for 2 of 2 registered roles" and through.returncode == 0
              and through.stdout == ok.stdout,
              f"the offline check passes for a directory that serves both roles, through the wrapper and the package alike: {ok.stdout}{ok.stderr[-300:]} | {through.stdout}")
        check(empty.returncode == 1 and "holds hook state for none of the 2 registered roles" in empty.stdout and "would defer every notification" in empty.stderr,
              f"and refuses one that serves none: {empty.stdout}{empty.stderr[-300:]}")
    finally:
        shutil.rmtree(base)


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {json.dumps(got)[:600]}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def value(label):
        return GOLDEN[label]["result"].get("value")

    def calls(label):
        return GOLDEN[label]["calls"]

    turn = {k[len("turn start: "):]: value(k) for k in GOLDEN if k.startswith("turn start: ")}
    check(turn["a busy turn start"] == 100.0 and turn["a gemini start"] == 100.0 and turn["busy but not a start"] == 5.0 and turn["carried through idle"] == 7.5
          and turn["an unreadable carried value"] is None and turn["nothing before"] is None and turn["no source"] is None,
          "a busy write from a turn-start event dates the turn; every other write carries the last start, or none")
    written = value("store: writes and reads")
    check(written[0]["turn_started_at"] == 10.0 and written[0]["state"] == "idle" and written[1]["state"] == "blocked" and written[2] is None
          and written[3] == "we_ird_target_1.2.json" and '"state": "busy"' in written[-2] and written[-1].startswith("refused: pane hook state must be idle, busy, or blocked")
          and value("store: a home-relative directory") == "ROOT/pane-state",
          "the store keeps the latest state with the turn's start carried, writes it normalised, makes a target a safe file name, expands a home-relative directory, and refuses any other state")
    malformed = {k: value(k) for k in GOLDEN if k.startswith("store: a record with ")}
    check(all(v is None for k, v in malformed.items() if "no target" not in k and "bad turn start" not in k and "of its own" not in k)
          and value("store: a record with a bad turn start")["turn_started_at"] is None and value("store: a record with no target")["target"] == "q-main:0.0"
          and value("store: a record with a target of its own")["target"] == "elsewhere:1.0",
          "a record that is not json, not an object, or has no state, an unknown state or no time is no record; a bad turn start is dropped, a missing target named, a recorded one kept")
    auth = {k[len("authority: "):]: value(k) for k in GOLDEN if k.startswith("authority: ")}
    check(auth["every role served"][1] is True and auth["some roles served"][0]["registered"] == ["p-main:0.0", "p-perf:0.0"]
          and auth["none served"][1] is False and auth["no roles"][1] is True and "stale assignment" in auth["served and stale"][2] and auth["a missing directory"][1] is False,
          "the authority is served when any registered role has state, and says which do not; stale assignments are named and not counted")
    gone = {k[len("gone: "):]: value(k)[0] for k in GOLDEN if k.startswith("gone: ")}
    check(gone["reused"] == "pid 4242 was reused: started at 777, not 776" and gone["gone"] == "pid 9999 no longer exists"
          and all(gone[k] == "" for k in ("not a record", "no process", "pid 1", "a bad pid", "alive, same start", "alive, no start recorded", "a bad start", "unreadable but present")),
          "only positive proof counts: a reused pid or a pid with no process is gone; everything it cannot see is live")
    exists = {k[len("pid exists: "):]: value(k) for k in GOLDEN if k.startswith("pid exists: ")}
    check(exists == {"no such process": [False, [[4242, 0]]], "not permitted": [True, [[4242, 0]]], "another error": [None, [[4242, 0]]], "present": [True, [[4242, 0]]]},
          "existence asks kill(pid, 0) only: no such process is gone, not permitted is present, any other error is unknown")
    check(value("live targets") == [["p-main:0.0", "p-audit:0.0"], [["p-perf:0.0", "pid 9999 no longer exists"]]] and value("registered targets") == ["p-main:0.0", "p-perf:0.0", "p-audit:0.0"],
          "live targets drop the provably gone and name them; a target both live and stale counts as live")
    check(value("verify: served")[0] == 0 and value("verify: not served")[0] == 1 and "would defer every notification" in value("verify: not served")[2]
          and GOLDEN["load: nothing to read"]["result"]["raised"] == "SystemExit",
          "the offline check passes a directory that serves the live roles and refuses one that serves none; with nothing to read it stops")
    rebound = {k[len("rebound on notify_listener: "):]: v for k, v in GOLDEN.items() if k.startswith("rebound on notify_listener: ")}
    check(rebound["read_process"]["result"]["value"] == "pid 9999 was reused: started at 1, not 2" and rebound["PROC_ROOT"]["result"]["value"] == ["", [9999]]
          and rebound["TURN_START_EVENTS"]["result"]["value"] == 1.0 and rebound["PaneHookState"]["result"]["value"] == ["REBOUND", "idle"]
          and rebound["PaneStateAuthority"]["result"]["value"] == ["REBOUND", ["p-main:0.0"]]
          and all(rebound[n]["calls"].get(f"{n} rebound") for n in ("hook_turn_started_at", "assignment_process_gone", "load_runtime_assignments", "pane_state_authority")),
          "each of the nine, rebound on notify_listener, is what the moved code uses when it runs")


def test_every_seam_is_reached() -> None:
    names = {name for reads in SEAMS.values() for name in reads}
    check(set(PASSED) - {"live_pane_targets"} <= names and set(PASSED) <= REACHED and names <= set(REBINDS) | set(PASSED) | {"PaneHookStateStore", "live_pane_targets"},
          f"a recorder or a rebind on notify_listener reached every seam the moved code reads: missing {sorted(set(PASSED) - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_the_same_defaults",
             "test_the_installed_import_form_holds_its_own_one_set_and_reads_through_its_own_listener", "test_both_import_forms_answer_alike_and_look_at_no_host_path",
             "test_the_readers_build_their_store_from_notify_listener_when_they_run", "test_the_seams_read_through_notify_listener_and_nothing_bound",
             "test_notify_listener_reexports_them_and_names_them_as_before", "test_the_entry_point_answers_as_before")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"pane_state_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
