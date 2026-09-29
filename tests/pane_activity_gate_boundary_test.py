#!/usr/bin/env python3
"""SYRD-475: the pane activity gate, against notify_listener it came out of.

The twenty-five -- `PaneActivityGate`, which decides whether a role's pane may
be typed into now; the evidence it reads and nothing else does (the process
table and child-work sample, the working-timer probe, the composer snapshot of
a captured pane); its tuning constants; and the five constants its defaults
bind when the class is defined (`DEFAULT_PROJECT`, `ROLE_TO_TARGET` and three
timing defaults) -- moved unchanged into
`scripts/ticket_board/pane_activity_gate.py`. `notify_listener` re-exports all
of them and keeps its own imports. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first, in both import
  forms: the tests' `scripts.ticket_board` and the installed entry point's
  `ticket_board`. The module alone loads only its package. Every default binds
  the object it bound before -- `subprocess.run`, `read_process_table`,
  `time.monotonic`, the role-to-pane map -- when the class is defined.
- **Seams (rule 24):** everything the moved code reads of `notify_listener`
  when it runs -- the hook-state store, the traces, the composer snapshot type,
  the logger, and each other -- is read through it, so a patch there reaches
  the gate; `role_runtime` and `role_pane_entry` build their gate from
  `notify_listener` when they run. Uses are counted across the modules.
- **The behaviour is the baseline's:** the evidence helpers on idle, working,
  Codex, typed and prompt panes, process trees and a fake `/proc`, and twelve
  gate scenarios through twelve of its public methods, with six names rebound on
  `notify_listener`. `GOLDEN` below was produced by running the BASELINE
  module's own definitions over the very cases embedded here (`gold475.py`),
  not typed; it is byte-identical under `env -i`, in a normal role pane, with
  another HOME, USER, COLUMNS, TMPDIR, project and pane-state directory, under
  umask 077 and under several hash seeds.
- **The entry point answers as before:** the installed
  `ticket-board-notify-listener --help` and its offline pane-state authority
  check, and the same check through the package, with no host path looked at.

No real home, tenant, account, /etc, /var or /opt path is read or written, tmux
is never run and the real /proc is never read: runners, the process table and
the clocks are stand-ins, and every directory is made under /tmp. Spawns, every
exec, signals, account and group lookups and socket connections are refused for
each case.
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# notify_listener first, as the listener does: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import notify_listener as t  # noqa: E402,I001
from scripts.ticket_board import pane_activity_gate as m  # noqa: E402

CHECKS = 0
MOVED = ('DEFAULT_BUSY_REQUEUE_SECONDS', 'DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS', 'IDLE_TURN_END_SOURCES', 'TRUSTED_IDLE_SOURCES', 'DEFAULT_ROLE_RUNTIMES', 'DEFAULT_DIRECTOR_COMPOSER_HOME_X', 'DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS', 'DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS', 'DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS', 'DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS', 'DEFAULT_CHILD_WORK_CPU_TICKS', 'MIN_RECOVERABLE_HOOK_EPOCH_SECONDS', 'DEFAULT_PROJECT', 'ROLE_TO_TARGET', 'PERMISSION_PROMPT_BLOCK_SOURCES', 'STALE_PRIOR_TURN_CHILD_WORK', 'WorkingProbe', 'ChildWorkSample', 'ChildWorkMemory', '_boot_time_epoch_seconds', 'read_process_table', 'descendant_work_sample', 'pane_content_digest', 'composer_snapshot_from_pane_text', 'PaneActivityGate')
#: Measured on the baseline notify_listener: each moved function's and method's call-time reads of its globals.
SEAMS = {
    'read_process_table': {'_boot_time_epoch_seconds': 1},
    'descendant_work_sample': {'ChildWorkSample': 1},
    'composer_snapshot_from_pane_text': {'ComposerSnapshot': 3},
    'PaneActivityGate.__init__': {'DEFAULT_ROLE_RUNTIMES': 1, 'PaneHookStateStore': 1, 'ROLE_TO_TARGET': 1},
    'PaneActivityGate._record_trace': {'WORK_EVIDENCE_REASONS': 1},
    'PaneActivityGate.submission_witnessed': {'TURN_START_EVENTS': 1},
    'PaneActivityGate.turn_end_idle_since_by_role': {'IDLE_TURN_END_SOURCES': 1},
    'PaneActivityGate._role_for_target': {'DEFAULT_PROJECT': 1},
    'PaneActivityGate._captured_working_timer_probe': {'WorkingProbe': 2, 'pane_content_digest': 1},
    'PaneActivityGate._working_timer_probe_trace': {'ActivityTrace': 4},
    'PaneActivityGate.composer_snapshot': {'ComposerSnapshot': 1, 'composer_snapshot_from_pane_text': 1},
    'PaneActivityGate._director_startup_hold_trace': {'ActivityTrace': 2},
    'PaneActivityGate._child_work_sample': {'ChildWorkSample': 2, 'descendant_work_sample': 1},
    'PaneActivityGate.child_work_trace': {'ActivityTrace': 6, 'ChildWorkMemory': 1, 'PRIOR_TURN_CHILD_WORK': 1, 'STALE_PRIOR_TURN_CHILD_WORK': 1},
    'PaneActivityGate._trusted_idle_source_trace': {'TRUSTED_IDLE_SOURCES': 1},
    'PaneActivityGate._idle_cursor_trace': {'ActivityTrace': 3},
    'PaneActivityGate._untrusted_idle_source_trace': {'ActivityTrace': 2, 'MIN_RECOVERABLE_HOOK_EPOCH_SECONDS': 1, 'TRUSTED_IDLE_SOURCES': 1},
    'PaneActivityGate._stale_codex_busy_trace': {'ActivityTrace': 3, 'LOGGER': 1, 'MIN_RECOVERABLE_HOOK_EPOCH_SECONDS': 1},
    'PaneActivityGate._anti_clobber_trace': {'ActivityTrace': 4},
    'PaneActivityGate._full_activity_trace': {'ActivityTrace': 2},
    'PaneActivityGate.permission_prompt_waits': {'PERMISSION_PROMPT_BLOCK_SOURCES': 1},
}
#: Every moved function and method, as `name` or `Class.method`.
FUNCTIONS = ('_boot_time_epoch_seconds', 'read_process_table', 'descendant_work_sample', 'pane_content_digest', 'composer_snapshot_from_pane_text', 'PaneActivityGate.__init__', 'PaneActivityGate._record_trace', 'PaneActivityGate.last_trace', 'PaneActivityGate.submission_witnessed', 'PaneActivityGate.missing_hook_targets', 'PaneActivityGate._idle_hook_states_by_role', 'PaneActivityGate.eligibility_busy', 'PaneActivityGate._confirmed_idle_since', 'PaneActivityGate._forget_confirmed_idle', 'PaneActivityGate.idle_since_by_role', 'PaneActivityGate.turn_end_idle_since_by_role', 'PaneActivityGate._tmux_session_for_target', 'PaneActivityGate._role_for_target', 'PaneActivityGate._runtime_for_source', 'PaneActivityGate._expected_runtime_for_target', 'PaneActivityGate._captured_working_timer_probe', 'PaneActivityGate._working_timer_probe_trace', 'PaneActivityGate.composer_snapshot', 'PaneActivityGate._working_timer_trace', 'PaneActivityGate._working_timer_idle_probe_trace', 'PaneActivityGate._target_cursor_state', 'PaneActivityGate._reset_director_startup_hold', 'PaneActivityGate._director_startup_hold_trace', 'PaneActivityGate._pane_pid', 'PaneActivityGate._child_work_sample', 'PaneActivityGate._survivors_advanced', 'PaneActivityGate._survivors_that_advanced', 'PaneActivityGate._prior_turn_children', 'PaneActivityGate.child_work_trace', 'PaneActivityGate._trusted_idle_source_trace', 'PaneActivityGate._idle_cursor_trace', 'PaneActivityGate._untrusted_idle_source_trace', 'PaneActivityGate._stale_codex_busy_trace', 'PaneActivityGate.anti_clobber_trace', 'PaneActivityGate.pre_send_anti_clobber_trace', 'PaneActivityGate._anti_clobber_trace', 'PaneActivityGate.anti_clobber_busy', 'PaneActivityGate.pre_send_anti_clobber_busy', 'PaneActivityGate._full_activity_trace', 'PaneActivityGate.permission_prompt_waits', 'PaneActivityGate.is_busy', 'PaneActivityGate.pre_send_busy', 'PaneActivityGate.is_working')
#: Measured on the baseline: every notify_listener definition outside the twenty-five that names them, and how often.
DISPATCH = {'target_for_transition': {'ROLE_TO_TARGET': 7}, 'TicketBoardNotifyListener': {'DEFAULT_BUSY_REQUEUE_SECONDS': 1, 'DEFAULT_PROJECT': 1, 'PaneActivityGate': 2, 'ROLE_TO_TARGET': 2}, '_build_parser': {'DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS': 1, 'DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS': 1}, 'main': {'PaneActivityGate': 1, 'ROLE_TO_TARGET': 1}}
#: Measured on the baseline, by AST: every production module that imports them from notify_listener, and how often.
READERS = {'scripts/role_pane_entry.py': {'import PaneActivityGate': 1}, 'scripts/role_runtime.py': {'import PaneActivityGate': 1}}
#: The constants and the defaults, as the baseline wrote them. Measured on the baseline, not typed.
CONSTANT_TEXT = {
    'DEFAULT_BUSY_REQUEUE_SECONDS': (None, '1.0'),
    'DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS': (None, '15 * 60.0'),
    'IDLE_TURN_END_SOURCES': (None, "frozenset({'claude.Stop', 'codex.Stop', 'gemini.AfterAgent', 'gemini.PostInvocation', 'gemini.Stop', 'hermes.post_llm_call', 'hermes.on_session_end', 'hermes.on_session_finalize'})"),
    'TRUSTED_IDLE_SOURCES': (None, "IDLE_TURN_END_SOURCES | frozenset({'claude.Notification.idle_prompt', 'listener.stale_codex_busy_recovery'})"),
    'DEFAULT_ROLE_RUNTIMES': (None, "{'director': 'claude', 'main': 'codex', 'app': 'codex', 'perf': 'codex', 'research': 'claude', 'ops': 'codex', 'audit': 'claude', 'inspector': 'gemini'}"),
    'DEFAULT_DIRECTOR_COMPOSER_HOME_X': (None, '2'),
    'DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS': (None, '0.0'),
    'DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS': (None, '1.2'),
    'DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS': (None, '120.0'),
    'DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS': (None, '0.6'),
    'DEFAULT_CHILD_WORK_CPU_TICKS': (None, '2'),
    'MIN_RECOVERABLE_HOOK_EPOCH_SECONDS': (None, '1700000000.0'),
    'DEFAULT_PROJECT': (None, "os.environ.get('TICKET_BOARD_PROJECT', '').strip() or os.environ.get('PGU_TICKET_BOARD_PROJECT', '').strip() or 'pgu'"),
    'ROLE_TO_TARGET': (None, "{role: f'{DEFAULT_PROJECT}-{role}:0.0' for role in ('director', 'main', 'app', 'perf', 'research', 'ops', 'audit', 'inspector')}"),
    'PERMISSION_PROMPT_BLOCK_SOURCES': (None, "frozenset({'claude.Notification.permission_prompt', 'codex.PermissionRequest'})"),
    'STALE_PRIOR_TURN_CHILD_WORK': (None, "'stale_prior_turn_child_work'"),
}
DEFAULTS = {
    '_boot_time_epoch_seconds': ["Path('/proc')"],
    'read_process_table': ["Path('/proc')"],
    'descendant_work_sample': [],
    'pane_content_digest': [],
    'composer_snapshot_from_pane_text': [],
    'PaneActivityGate.__init__': ['None', "ROLE_TO_TARGET['director']", 'subprocess.run', 'subprocess.run', 'subprocess.run', 'None', 'read_process_table', 'DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS', 'DEFAULT_CHILD_WORK_CPU_TICKS', 'DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS', 'DEFAULT_BUSY_REQUEUE_SECONDS', 'DEFAULT_DIRECTOR_COMPOSER_HOME_X', 'DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS', 'DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS', 'DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS', 'None', 'time.monotonic', 'time.time', 'time.sleep'],
    'PaneActivityGate._record_trace': [],
    'PaneActivityGate.last_trace': [],
    'PaneActivityGate.submission_witnessed': [],
    'PaneActivityGate.missing_hook_targets': ['None'],
    'PaneActivityGate._idle_hook_states_by_role': ['None'],
    'PaneActivityGate.eligibility_busy': [],
    'PaneActivityGate._confirmed_idle_since': [],
    'PaneActivityGate._forget_confirmed_idle': [],
    'PaneActivityGate.idle_since_by_role': ['None'],
    'PaneActivityGate.turn_end_idle_since_by_role': ['None'],
    'PaneActivityGate._tmux_session_for_target': [],
    'PaneActivityGate._role_for_target': [],
    'PaneActivityGate._runtime_for_source': [],
    'PaneActivityGate._expected_runtime_for_target': [],
    'PaneActivityGate._captured_working_timer_probe': [],
    'PaneActivityGate._working_timer_probe_trace': [],
    'PaneActivityGate.composer_snapshot': [],
    'PaneActivityGate._working_timer_trace': ['None'],
    'PaneActivityGate._working_timer_idle_probe_trace': [],
    'PaneActivityGate._target_cursor_state': [],
    'PaneActivityGate._reset_director_startup_hold': ['True'],
    'PaneActivityGate._director_startup_hold_trace': [],
    'PaneActivityGate._pane_pid': [],
    'PaneActivityGate._child_work_sample': [],
    'PaneActivityGate._survivors_advanced': [],
    'PaneActivityGate._survivors_that_advanced': [],
    'PaneActivityGate._prior_turn_children': [],
    'PaneActivityGate.child_work_trace': ['None'],
    'PaneActivityGate._trusted_idle_source_trace': [],
    'PaneActivityGate._idle_cursor_trace': ['False'],
    'PaneActivityGate._untrusted_idle_source_trace': [],
    'PaneActivityGate._stale_codex_busy_trace': [],
    'PaneActivityGate.anti_clobber_trace': [],
    'PaneActivityGate.pre_send_anti_clobber_trace': [],
    'PaneActivityGate._anti_clobber_trace': [],
    'PaneActivityGate.anti_clobber_busy': [],
    'PaneActivityGate.pre_send_anti_clobber_busy': [],
    'PaneActivityGate._full_activity_trace': [],
    'PaneActivityGate.permission_prompt_waits': [],
    'PaneActivityGate.is_busy': [],
    'PaneActivityGate.pre_send_busy': [],
    'PaneActivityGate.is_working': [],
}
#: The BASELINE's own behaviour for the cases below (`gold475.py`, run on the baseline notify_listener under the guard).
GOLDEN = {
    'composer: empty': {'result': {'type': 'ComposerSnapshot', 'value': {'__type__': 'ComposerSnapshot', 'available': False, 'marker_found': False, 'active': False, 'content_sha256': '', 'content_length': 0, 'error': 'empty_capture'}}, 'calls': {}},
    'composer: idle': {'result': {'type': 'ComposerSnapshot', 'value': {'__type__': 'ComposerSnapshot', 'available': True, 'marker_found': False, 'active': False, 'content_sha256': '', 'content_length': 0, 'error': ''}}, 'calls': {}},
    'composer: working': {'result': {'type': 'ComposerSnapshot', 'value': {'__type__': 'ComposerSnapshot', 'available': True, 'marker_found': False, 'active': False, 'content_sha256': '', 'content_length': 0, 'error': ''}}, 'calls': {}},
    'composer: codex': {'result': {'type': 'ComposerSnapshot', 'value': {'__type__': 'ComposerSnapshot', 'available': True, 'marker_found': False, 'active': False, 'content_sha256': '', 'content_length': 0, 'error': ''}}, 'calls': {}},
    'composer: typed': {'result': {'type': 'ComposerSnapshot', 'value': {'__type__': 'ComposerSnapshot', 'available': True, 'marker_found': False, 'active': False, 'content_sha256': '', 'content_length': 0, 'error': ''}}, 'calls': {}},
    'composer: prompt': {'result': {'type': 'ComposerSnapshot', 'value': {'__type__': 'ComposerSnapshot', 'available': True, 'marker_found': False, 'active': False, 'content_sha256': '', 'content_length': 0, 'error': ''}}, 'calls': {}},
    'digest: empty': {'result': {'type': 'str', 'value': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'}, 'calls': {}},
    'digest: idle': {'result': {'type': 'str', 'value': '0dc2ed0b5f60d002bd329a529a18838cd4a8ddca285a61b893a6162747831803'}, 'calls': {}},
    'digest: working': {'result': {'type': 'str', 'value': '60db00760ea372289758fd010846a034debc0e639e89938ebd2564caa2f6bd2b'}, 'calls': {}},
    'digest: codex': {'result': {'type': 'str', 'value': '631fd71161ac9bdf9abf12a6498cf3cb8ba5e7ed77719115fa8e9243166932a6'}, 'calls': {}},
    'digest: typed': {'result': {'type': 'str', 'value': 'ddc2b1f07e84a54d6a063df5d36838f520d400d235221bb339ef0807187ed7bb'}, 'calls': {}},
    'digest: prompt': {'result': {'type': 'str', 'value': '09bf4e9faa06e4ea56667235110c8b234a8916fe926139a3179d79f7703d3ba8'}, 'calls': {}},
    'descendants: a tree': {'result': {'type': 'ChildWorkSample', 'value': {'__type__': 'ChildWorkSample', 'observed': True, 'pids': [101, 102, 103, 104], 'cpu_ticks': 31, 'detached': [102, 103], 'starts': [[101, 2.0], [102, 3.0], [103, 4.0], [104, 6.0]], 'cpu_by_pid': [[101, 7], [102, 11], [103, 13], [104, 0]]}}, 'calls': {}},
    'descendants: a leaf': {'result': {'type': 'ChildWorkSample', 'value': {'__type__': 'ChildWorkSample', 'observed': True, 'pids': [], 'cpu_ticks': 0, 'detached': [], 'starts': [], 'cpu_by_pid': []}}, 'calls': {}},
    'descendants: unknown pane': {'result': {'type': 'ChildWorkSample', 'value': {'__type__': 'ChildWorkSample', 'observed': True, 'pids': [], 'cpu_ticks': 0, 'detached': [], 'starts': [], 'cpu_by_pid': []}}, 'calls': {}},
    'descendants: empty table': {'result': {'type': 'ChildWorkSample', 'value': {'__type__': 'ChildWorkSample', 'observed': True, 'pids': [], 'cpu_ticks': 0, 'detached': [], 'starts': [], 'cpu_by_pid': []}}, 'calls': {}},
    'boot time: synthetic': {'result': {'type': 'float', 'value': 1700000000.0}, 'calls': {}},
    'boot time: missing': {'result': {'type': 'float', 'value': 0.0}, 'calls': {}},
    'process table: synthetic': {'result': {'type': 'tuple', 'value': [[102, 101, 11, 102, 1700000003.0], [101, 100, 7, 100, 1700000002.0], [100, 1, 5, 100, 1700000001.0]]}, 'calls': {}},
    'process table: missing': {'result': {'type': 'tuple', 'value': []}, 'calls': {}},
    'constants': {'result': {'type': 'dict', 'value': {'DEFAULT_BUSY_REQUEUE_SECONDS': 1.0, 'DEFAULT_CHILD_WORK_CPU_TICKS': 2, 'DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS': 0.6, 'DEFAULT_DIRECTOR_COMPOSER_HOME_X': 2, 'DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS': 900.0, 'DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS': 1.2, 'DEFAULT_PROJECT': 'PROJECT', 'DEFAULT_ROLE_RUNTIMES': {'app': 'codex', 'audit': 'claude', 'director': 'claude', 'inspector': 'gemini', 'main': 'codex', 'ops': 'codex', 'perf': 'codex', 'research': 'claude'}, 'DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS': 120.0, 'DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS': 0.0, 'IDLE_TURN_END_SOURCES': ['claude.Stop', 'codex.Stop', 'gemini.AfterAgent', 'gemini.PostInvocation', 'gemini.Stop', 'hermes.on_session_end', 'hermes.on_session_finalize', 'hermes.post_llm_call'], 'MIN_RECOVERABLE_HOOK_EPOCH_SECONDS': 1700000000.0, 'PERMISSION_PROMPT_BLOCK_SOURCES': ['claude.Notification.permission_prompt', 'codex.PermissionRequest'], 'ROLE_TO_TARGET': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'STALE_PRIOR_TURN_CHILD_WORK': 'stale_prior_turn_child_work', 'TRUSTED_IDLE_SOURCES': ['claude.Notification.idle_prompt', 'claude.Stop', 'codex.Stop', 'gemini.AfterAgent', 'gemini.PostInvocation', 'gemini.Stop', 'hermes.on_session_end', 'hermes.on_session_finalize', 'hermes.post_llm_call', 'listener.stale_codex_busy_recovery']}}, 'calls': {}},
    'gate: idle after a turn': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16}},
    'gate: busy hook': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': True, 'main: is_busy': True, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'hook_busy', 'region_digest': ''}, 'main: is_working': True, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_busy': True, 'main: submission_witnessed': True, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 4}},
    'gate: blocked on a permission': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': True, 'main: is_busy': True, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'hook_blocked', 'region_digest': ''}, 'main: is_working': True, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_busy': True, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 4}},
    'gate: a permission prompt waiting': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': True, 'main: is_busy': True, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'hook_blocked', 'region_digest': ''}, 'main: is_working': True, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_busy': True, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {'main': '2025-06-15T15:05:40+00:00'}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 4}},
    'gate: no hook state': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': True, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'no_hook_state', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': True, 'main: is_busy': True, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'no_hook_state', 'region_digest': ''}, 'main: is_working': True, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'no_hook_state', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': True, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'no_hook_state', 'region_digest': ''}, 'main: pre_send_busy': True, 'main: submission_witnessed': None, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 4}},
    'gate: codex working': {'result': {'type': 'dict', 'value': {'audit: anti_clobber_busy': False, 'audit: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'audit: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'audit: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'audit: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'audit: eligibility_busy': True, 'audit: is_busy': True, 'audit: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'hook_busy', 'region_digest': ''}, 'audit: is_working': True, 'audit: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'audit: pre_send_anti_clobber_busy': False, 'audit: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'audit: pre_send_busy': True, 'audit: submission_witnessed': True, 'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 4}},
    'gate: codex idle but a timer': {'result': {'type': 'dict', 'value': {'audit: anti_clobber_busy': False, 'audit: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'audit: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'audit: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'audit: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'audit: eligibility_busy': False, 'audit: is_busy': False, 'audit: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'audit: is_working': False, 'audit: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'audit: pre_send_anti_clobber_busy': False, 'audit: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'audit: pre_send_busy': False, 'audit: submission_witnessed': False, 'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16}},
    'gate: director typing': {'result': {'type': 'dict', 'value': {'director: anti_clobber_busy': True, 'director: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'human_composing', 'region_digest': ''}, 'director: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'director: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'director: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'director: eligibility_busy': True, 'director: is_busy': True, 'director: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'human_composing', 'region_digest': ''}, 'director: is_working': True, 'director: last_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'human_composing', 'region_digest': ''}, 'director: pre_send_anti_clobber_busy': True, 'director: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'human_composing', 'region_digest': ''}, 'director: pre_send_busy': True, 'director: submission_witnessed': False, 'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-director:0.0', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 4}},
    'gate: director idle': {'result': {'type': 'dict', 'value': {'director: anti_clobber_busy': False, 'director: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'director: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'director: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'director: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'director: eligibility_busy': False, 'director: is_busy': True, 'director: is_busy trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'startup_latch_unestablished', 'region_digest': ''}, 'director: is_working': False, 'director: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'director: pre_send_anti_clobber_busy': False, 'director: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'director: pre_send_busy': False, 'director: submission_witnessed': False, 'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-director:0.0', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-director:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-director:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16}},
    'gate: pane gone': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': False, 'content_length': 0, 'content_sha256': '', 'error': 'empty_capture', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16}},
    'gate: children at work': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16}},
    'gate: an untrusted idle source': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 14}},
    'gate: a stale codex busy hook': {'result': {'type': 'dict', 'value': {'audit: anti_clobber_busy': False, 'audit: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'audit: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'audit: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'audit: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'audit: eligibility_busy': False, 'audit: is_busy': False, 'audit: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_codex_busy_recovered', 'region_digest': ''}, 'audit: is_working': False, 'audit: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'audit: pre_send_anti_clobber_busy': False, 'audit: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'audit: pre_send_busy': False, 'audit: submission_witnessed': False, 'idle_since_by_role': {'audit': '2025-06-15T15:06:42+00:00'}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-audit:0.0', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-audit:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-audit:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 14, 'pane_content_digest': 4}},
    'gate: an unconfigured pane': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'p-x-main:0.0: anti_clobber_busy': False, 'p-x-main:0.0: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'p-x-main:0.0: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'p-x-main:0.0: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'p-x-main:0.0: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'p-x-main:0.0: eligibility_busy': False, 'p-x-main:0.0: is_busy': False, 'p-x-main:0.0: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'p-x-main:0.0: is_working': False, 'p-x-main:0.0: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'p-x-main:0.0: pre_send_anti_clobber_busy': False, 'p-x-main:0.0: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'p-x-main:0.0: pre_send_busy': False, 'p-x-main:0.0: submission_witnessed': False, 'permission_prompt_waits': {}, 'role runtimes': {'main': 'codex'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 12}},
    'gate: a pane named apart from its role': {'result': {'type': 'dict', 'value': {'custom-pane:0.0: anti_clobber_busy': False, 'custom-pane:0.0: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'foreign_runtime_working_timer_idle', 'region_digest': ''}, 'custom-pane:0.0: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'custom-pane:0.0: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'custom-pane:0.0: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'custom-pane:0.0: eligibility_busy': False, 'custom-pane:0.0: is_busy': False, 'custom-pane:0.0: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'foreign_runtime_working_timer_idle', 'region_digest': ''}, 'custom-pane:0.0: is_working': False, 'custom-pane:0.0: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'custom-pane:0.0: pre_send_anti_clobber_busy': False, 'custom-pane:0.0: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'custom-pane:0.0: pre_send_busy': False, 'custom-pane:0.0: submission_witnessed': False, 'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'main': 'codex'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'custom-pane:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}', 'tmux display-message -p -t custom-pane:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux capture-pane -p -J -t custom-pane:0.0', 'tmux display-message -p -t custom-pane:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16, 'pane_content_digest': 40}},
    'rebound on notify_listener: TRUSTED_IDLE_SOURCES': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'working_timer_idle', 'region_digest': '0dc2ed0b5f60d002bd329a529a18838cd4a8ddca285a61b893a6162747831803'}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': True, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'working_timer_idle', 'region_digest': '0dc2ed0b5f60d002bd329a529a18838cd4a8ddca285a61b893a6162747831803'}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': True, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: pre_send_busy': True, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16, 'pane_content_digest': 40}},
    'rebound on notify_listener: DEFAULT_ROLE_RUNTIMES': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'foreign_runtime_working_timer_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'foreign_runtime_working_timer_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'main': 'gemini'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 16, 'pane_content_digest': 40}},
    'rebound on notify_listener: composer_snapshot_from_pane_text': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': False, 'content_length': 0, 'content_sha256': '', 'error': 'rebound', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text rebound': 1, 'descendant_work_sample': 16}},
    'rebound on notify_listener: descendant_work_sample': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {'main': '2025-06-15T15:05:40+00:00'}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': None, 'main: child_work_trace after new work': None, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {'main': '2025-06-15T15:05:40+00:00'}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample rebound': 8}},
    'rebound on notify_listener: DEFAULT_PROJECT': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'missing_hook_targets': ['PROJECT-app:0.0', 'PROJECT-audit:0.0', 'PROJECT-director:0.0', 'PROJECT-inspector:0.0', 'PROJECT-main:0.0', 'PROJECT-ops:0.0', 'PROJECT-perf:0.0', 'PROJECT-research:0.0'], 'p-x-main:0.0: anti_clobber_busy': False, 'p-x-main:0.0: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'foreign_runtime_working_timer_idle', 'region_digest': ''}, 'p-x-main:0.0: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'p-x-main:0.0: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'p-x-main:0.0: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'p-x-main:0.0: eligibility_busy': False, 'p-x-main:0.0: is_busy': False, 'p-x-main:0.0: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'foreign_runtime_working_timer_idle', 'region_digest': ''}, 'p-x-main:0.0: is_working': False, 'p-x-main:0.0: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'p-x-main:0.0: pre_send_anti_clobber_busy': False, 'p-x-main:0.0: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'p-x-main:0.0: pre_send_busy': False, 'p-x-main:0.0: submission_witnessed': False, 'permission_prompt_waits': {}, 'role runtimes': {'main': 'codex'}, 'role targets': {'app': 'PROJECT-app:0.0', 'audit': 'PROJECT-audit:0.0', 'director': 'PROJECT-director:0.0', 'inspector': 'PROJECT-inspector:0.0', 'main': 'PROJECT-main:0.0', 'ops': 'PROJECT-ops:0.0', 'perf': 'PROJECT-perf:0.0', 'research': 'PROJECT-research:0.0'}, 'tmux calls': ['tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t p-x-main:0.0', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}', 'tmux display-message -p -t p-x-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 12, 'pane_content_digest': 32}},
    'rebound on notify_listener: ROLE_TO_TARGET': {'result': {'type': 'dict', 'value': {'idle_since_by_role': {}, 'main: anti_clobber_busy': False, 'main: anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: child_work_trace': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: child_work_trace after new work': {'__type__': 'ActivityTrace', 'busy': True, 'reason': 'pane_child_work', 'region_digest': ''}, 'main: composer_snapshot': {'__type__': 'ComposerSnapshot', 'active': False, 'available': True, 'content_length': 0, 'content_sha256': '', 'error': '', 'marker_found': False}, 'main: eligibility_busy': False, 'main: is_busy': False, 'main: is_busy trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'hook_idle', 'region_digest': ''}, 'main: is_working': False, 'main: last_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_anti_clobber_busy': False, 'main: pre_send_anti_clobber_trace': {'__type__': 'ActivityTrace', 'busy': False, 'reason': 'stale_prior_turn_child_work', 'region_digest': ''}, 'main: pre_send_busy': False, 'main: submission_witnessed': False, 'missing_hook_targets': ['q475-director:0.0', 'q475-main:0.0'], 'permission_prompt_waits': {}, 'role runtimes': {'audit': 'codex', 'director': 'claude', 'main': 'claude'}, 'role targets': {'director': 'q475-director:0.0', 'main': 'q475-main:0.0'}, 'tmux calls': ['tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{cursor_x} #{cursor_y} #{pane_height}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux capture-pane -p -J -t PROJECT-main:0.0', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}', 'tmux display-message -p -t PROJECT-main:0.0 #{pane_pid}'], 'turn_end_idle_since_by_role': {}}}, 'calls': {'composer_snapshot_from_pane_text': 1, 'descendant_work_sample': 12}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: The module's imports at load: the standard library only.
MODULE_IMPORTS = ["from __future__ import annotations", "import hashlib", "import os", "import re", "import subprocess", "import time",
                  "from dataclasses import dataclass", "from datetime import datetime, timezone", "from pathlib import Path", "from typing import Any, Callable, Sequence"]
#: The call-time import every function and method that reads notify_listener starts with.
CALL_TIME_IMPORT = "from . import notify_listener as listener"

# --- the cases, shared verbatim with `gold475.py` (which ran them on the baseline) ------------------------------------
# A case drives the pane activity gate or one of the evidence helpers with stand-in tmux runners, a stand-in process
# table, a fake /proc and a hook-state directory made fresh under /tmp, and deterministic clocks, and records the answer
# (or the exception) and how many times it reached each helper on `notify_listener` (recorded there and passed through).
# Nothing runs tmux or reads the real /proc. The project a target names comes from the environment at import, so it is
# recorded as PROJECT; paths under the case directory as ROOT.
IDLE = "some output\n\n╭───╮\n│ > │\n╰───╯\n  ? for shortcuts\n"
WORKING = "doing things\n✻ Working… (12s · esc to interrupt)\n\n> \n"
CODEX = "• Working (3s • esc to interrupt)\n\n› Ask Codex\n"
TYPED = "╭───╮\n│ > half a sentence │\n╰───╯\n"
PROMPT = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n"
TEXTS = {"empty": "", "idle": IDLE, "working": WORKING, "codex": CODEX, "typed": TYPED, "prompt": PROMPT}
TABLE = [(100, 1, 5, 100, 1.0), (101, 100, 7, 100, 2.0), (102, 101, 11, 102, 3.0), (103, 102, 13, 102, 4.0), (200, 1, 17, 200, 5.0), (104, 100, 0, 100, 6.0)]
SCENARIOS = {
    "idle after a turn": ({"main": {"text": IDLE}}, [("main", "idle", "claude.Stop")]),
    "busy hook": ({"main": {"text": WORKING}}, [("main", "busy", "claude.UserPromptSubmit")]),
    "blocked on a permission": ({"main": {"text": PROMPT}}, [("main", "blocked", "claude.Notification")]),
    "a permission prompt waiting": ({"main": {"text": PROMPT}}, [("main", "blocked", "claude.Notification.permission_prompt")]),
    "no hook state": ({"main": {"text": IDLE}}, []),
    "codex working": ({"audit": {"text": CODEX}}, [("audit", "busy", "codex.UserPromptSubmit")]),
    "codex idle but a timer": ({"audit": {"text": CODEX}}, [("audit", "idle", "codex.Stop")]),
    "director typing": ({"director": {"text": TYPED, "cursor": "20 5 40"}}, [("director", "idle", "claude.Stop")]),
    "director idle": ({"director": {"text": IDLE}}, [("director", "idle", "claude.Stop")]),
    "pane gone": ({}, [("main", "idle", "claude.Stop")]),
    "children at work": ({"main": {"text": IDLE, "pid": 101}}, [("main", "idle", "claude.Stop")]),
    "an untrusted idle source": ({"main": {"text": IDLE}}, [("main", "idle", "listener.stale_codex_busy_recovery")]),
    "a stale codex busy hook": ({"audit": {"text": IDLE}}, [("audit", "busy", "codex.UserPromptSubmit")]),
    # A pane outside the configured role map: its role is read from its session name, the project prefix first.
    "an unconfigured pane": ({"p-x-main:0.0": {"text": IDLE}}, [("p-x-main:0.0", "idle", "claude.Stop")], {"main": "codex"}),
    # A pane the role map names, whose session name says nothing of its role: the map, not the name, says whose it is.
    "a pane named apart from its role": ({"custom-pane:0.0": {"text": IDLE}}, [("custom-pane:0.0", "idle", "claude.Stop")], {"main": "codex"}, {"main": "custom-pane:0.0"}),
}
METHODS = ("is_busy", "pre_send_busy", "is_working", "eligibility_busy", "anti_clobber_trace", "pre_send_anti_clobber_trace", "anti_clobber_busy",
           "pre_send_anti_clobber_busy", "last_trace", "composer_snapshot", "submission_witnessed", "child_work_trace")
REBINDS = ("TRUSTED_IDLE_SOURCES", "DEFAULT_ROLE_RUNTIMES", "composer_snapshot_from_pane_text", "descendant_work_sample", "DEFAULT_PROJECT", "ROLE_TO_TARGET")
PASSED = ("composer_snapshot_from_pane_text", "descendant_work_sample", "pane_content_digest")
CASES = {
    **{f"composer: {label}": {"call": "composer_snapshot_from_pane_text", "text": label} for label in TEXTS},
    **{f"digest: {label}": {"call": "pane_content_digest", "text": label} for label in TEXTS},
    "descendants: a tree": {"call": "descendant_work_sample", "pid": 100},
    "descendants: a leaf": {"call": "descendant_work_sample", "pid": 103},
    "descendants: unknown pane": {"call": "descendant_work_sample", "pid": 999},
    "descendants: empty table": {"call": "descendant_work_sample", "pid": 100, "table": []},
    "boot time: synthetic": {"call": "_boot_time_epoch_seconds", "proc": True},
    "boot time: missing": {"call": "_boot_time_epoch_seconds", "proc": False},
    "process table: synthetic": {"call": "read_process_table", "proc": True},
    "process table: missing": {"call": "read_process_table", "proc": False},
    "constants": {"call": None},
    **{f"gate: {label}": {"call": "gate", "scenario": label} for label in SCENARIOS},
    **{f"rebound on notify_listener: {name}": {"call": "gate", "scenario": "an unconfigured pane" if name == "DEFAULT_PROJECT" else "idle after a turn", "rebind": name}
       for name in REBINDS},
}
CONSTANTS = ("DEFAULT_PROJECT", "ROLE_TO_TARGET", "TRUSTED_IDLE_SOURCES", "IDLE_TURN_END_SOURCES", "DEFAULT_ROLE_RUNTIMES", "PERMISSION_PROMPT_BLOCK_SOURCES",
             "STALE_PRIOR_TURN_CHILD_WORK", "MIN_RECOVERABLE_HOOK_EPOCH_SECONDS", "DEFAULT_BUSY_REQUEUE_SECONDS", "DEFAULT_DIRECTOR_COMPOSING_TIMEOUT_SECONDS",
             "DEFAULT_IDLE_WORKING_TIMER_SAMPLE_DELAY_SECONDS", "DEFAULT_WORKING_TIMER_SAMPLE_DELAY_SECONDS", "DEFAULT_STALE_CODEX_BUSY_HOOK_SECONDS",
             "DEFAULT_CHILD_WORK_SAMPLE_DELAY_SECONDS", "DEFAULT_CHILD_WORK_CPU_TICKS", "DEFAULT_DIRECTOR_COMPOSER_HOME_X")


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definitions; `t` is notify_listener, whose evidence helpers are recorded and passed through."""
    import dataclasses, pathlib as _pl, shutil as _sh, subprocess as _sp, tempfile as _tf
    counts = {}
    root = _pl.Path(_tf.mkdtemp(prefix="syrd475-case-", dir="/tmp"))
    project = t.DEFAULT_PROJECT

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"__type__": type(value).__name__, **{k: norm(v) for k, v in dataclasses.asdict(value).items()}}
        if isinstance(value, dict):
            return {norm(str(k)): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(norm(v) for v in value)
        if isinstance(value, str):
            return value.replace(str(root), "ROOT").replace(f"{project}-", "PROJECT-") if project else value.replace(str(root), "ROOT")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return norm(repr(value))

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    saved = {n: getattr(t, n) for n in (*PASSED, *REBINDS)}
    try:
        if spec.get("proc") is not None:
            proc = root / "proc"
            if spec["proc"]:
                proc.mkdir()
                (proc / "stat").write_text("cpu  1 2 3 4\nbtime 1700000000\n")
                (proc / "uptime").write_text("1000.00 900.00\n")
                for pid, ppid, utime, stime, sid, start in ((100, 1, 3, 2, 100, 100), (101, 100, 4, 3, 100, 200), (102, 101, 5, 6, 102, 300)):
                    d = proc / str(pid); d.mkdir()
                    fields = ["S", str(ppid), "0", str(sid), "0", "0", "0", "0", "0", "0", "0", str(utime), str(stime), "0", "0", "20", "0", "1", "0", str(start)]
                    (d / "stat").write_text(f"{pid} (prog {pid}) " + " ".join(fields) + "\n")
                (proc / "self").mkdir()
                (proc / "999").mkdir()
                (proc / "999" / "stat").write_text("garbage\n")
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        call = spec["call"]
        if call is None:
            got = {n: getattr(holder, n) for n in CONSTANTS}
            got["DEFAULT_PROJECT"] = "PROJECT" if got["DEFAULT_PROJECT"] == project else got["DEFAULT_PROJECT"]
        elif call == "composer_snapshot_from_pane_text" or call == "pane_content_digest":
            got = (saved[call] if holder is t else getattr(holder, call))(TEXTS[spec["text"]])
        elif call == "descendant_work_sample":
            got = (saved[call] if holder is t else getattr(holder, call))(spec["pid"], spec.get("table", TABLE))
        elif call in ("_boot_time_epoch_seconds", "read_process_table"):
            got = getattr(holder, call)(root / "proc")
        else:
            panes_by_role, states, *runtimes = SCENARIOS[spec["scenario"]]
            targets = dict(saved["ROLE_TO_TARGET"])
            targets.update({role: role for role in panes_by_role if ":" in role})
            panes = {targets[role]: pane for role, pane in panes_by_role.items()}
            clock = [1_750_000_000.0]
            calls = []

            def tick():
                clock[0] += 1.0
                return clock[0]

            def runner(argv, **kwargs):
                calls.append(" ".join(argv))
                target = argv[argv.index("-t") + 1] if "-t" in argv else ""
                pane = panes.get(target, {})
                if "capture-pane" in argv:
                    return _sp.CompletedProcess(argv, 0 if target in panes else 1, pane.get("text", ""), "" if target in panes else "can't find pane")
                fmt = argv[-1]
                if fmt.startswith("#{cursor_x}"):
                    return _sp.CompletedProcess(argv, 0, pane.get("cursor", "2 23 24") + "\n", "")
                if fmt == "#{pane_pid}":
                    return _sp.CompletedProcess(argv, 0, str(pane.get("pid", 100)) + "\n", "")
                return _sp.CompletedProcess(argv, 0, "0\n", "")

            store = t.PaneHookStateStore(root / "state")
            for role, state, source in states:
                store.write(targets[role], state, source=source, now=clock[0] - (3600 if spec["scenario"] == "a stale codex busy hook" else 60))
            table = list(TABLE)
            if spec.get("rebind"):
                name = spec["rebind"]
                value = {"TRUSTED_IDLE_SOURCES": frozenset(), "DEFAULT_ROLE_RUNTIMES": {"main": "gemini"},
                         "composer_snapshot_from_pane_text": lambda text: note("composer_snapshot_from_pane_text rebound") or t.ComposerSnapshot(False, error="rebound"),
                         "descendant_work_sample": lambda pid, table: note("descendant_work_sample rebound") or t.ChildWorkSample(False),
                         "DEFAULT_PROJECT": "p-x", "ROLE_TO_TARGET": {"director": "q475-director:0.0", "main": "q475-main:0.0"}}[name]
                setattr(t, name, value)
            gate = holder.PaneActivityGate(state_store=store, client_activity_runner=runner, cursor_position_runner=runner, capture_pane_runner=runner,
                                           process_table_reader=lambda: table, monotonic=tick, wall_time=tick, sleeper=lambda s: None,
                                           role_runtimes=None if spec.get("rebind") == "DEFAULT_ROLE_RUNTIMES" else runtimes[0] if runtimes else {"director": "claude", "main": "claude", "audit": "codex"})
            if len(runtimes) > 1:
                gate.role_targets.update(runtimes[1])
            got = {"role targets": gate.role_targets, "role runtimes": gate.role_runtimes}
            for role in sorted(set(panes_by_role) | {r for r, *_ in states}):
                target = targets[role]
                for method in METHODS:
                    args = [target, clock[0] - 120] if method == "submission_witnessed" else [target]
                    try:
                        got[f"{role}: {method}"] = norm(getattr(gate, method)(*args))
                        if method == "is_busy":
                            got[f"{role}: is_busy trace"] = norm(gate.last_trace(target))
                    except AssertionError:
                        raise
                    except BaseException as exc:  # noqa: BLE001
                        got[f"{role}: {method}"] = f"raised {type(exc).__name__}: {norm(str(exc))}"
                table.append((900 + len(table), 101, 50, 101, clock[0]))
                got[f"{role}: child_work_trace after new work"] = norm(gate.child_work_trace(target))
            got["missing_hook_targets"] = gate.missing_hook_targets()
            got["idle_since_by_role"] = gate.idle_since_by_role()
            got["turn_end_idle_since_by_role"] = gate.turn_end_idle_since_by_role()
            got["permission_prompt_waits"] = gate.permission_prompt_waits()
            got["tmux calls"] = calls
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


#: Refuses and records any stat/lstat/open/access under the host's switchyard, /etc, /var and /opt paths, after proving it
#: catches one (a positive control).
HOST_RECORDER = (
    "import builtins, os, pathlib\n"
    "PREFIXES = ('/usr/local/lib/switchyard', '/etc', '/var', '/opt')\n"
    "hits = []\n"
    "def guarded(real, name):\n"
    "    def f(p, *a, **k):\n"
    "        s = os.fsdecode(p) if isinstance(p, (str, bytes, os.PathLike)) else ''\n"
    "        if s.startswith(PREFIXES):\n"
    "            hits.append((name, s)); raise FileNotFoundError(2, 'refused', s)\n"
    "        return real(p, *a, **k)\n"
    "    return f\n"
    "os.stat, os.lstat, builtins.open, os.access = guarded(os.stat, 'stat'), guarded(os.lstat, 'lstat'), guarded(builtins.open, 'open'), guarded(os.access, 'access')\n"
    "pathlib.Path('/opt/switchyard/syrd475-positive-control').is_file()\n"
    "control = len(hits); hits.clear()\n"
)
#: One probe, run in both import forms: a gate on a synthetic idle pane and a typed one, the evidence helpers, and the
#: gate with names rebound on notify_listener -- as a digest, and the host paths looked at.
GATE_PROBE = (
    "import hashlib, json, subprocess, tempfile, pathlib, shutil\n"
    "root = pathlib.Path(tempfile.mkdtemp(prefix='syrd475-probe-', dir='/tmp'))\n"
    "T = nl.ROLE_TO_TARGET\n"
    "texts = {T['main']: 'x\\n\\u256d\\u2500\\u256e\\n\\u2502 > \\u2502\\n\\u2570\\u2500\\u256f\\n', T['director']: '\\u2502 > half \\u2502\\n'}\n"
    "cursor = {T['main']: '2 23 24', T['director']: '20 5 40'}\n"
    "def runner(argv, **kw):\n"
    "    target = argv[argv.index('-t') + 1]\n"
    "    if 'capture-pane' in argv: return subprocess.CompletedProcess(argv, 0, texts.get(target, ''), '')\n"
    "    if argv[-1].startswith('#{cursor_x}'): return subprocess.CompletedProcess(argv, 0, cursor.get(target, '2 23 24') + '\\n', '')\n"
    "    return subprocess.CompletedProcess(argv, 0, '100\\n', '')\n"
    "clock = [1750000000.0]\n"
    "def tick():\n"
    "    clock[0] += 1; return clock[0]\n"
    "def build(runtimes={'main': 'claude', 'director': 'claude'}):\n"
    "    store = nl.PaneHookStateStore(root / 'state')\n"
    "    for role in ('main', 'director'): store.write(T[role], 'idle', source='claude.Stop', now=clock[0] - 60)\n"
    "    return nl.PaneActivityGate(state_store=store, client_activity_runner=runner, cursor_position_runner=runner, capture_pane_runner=runner,\n"
    "                               process_table_reader=lambda: [(100, 1, 5, 100, 1.0), (101, 100, 7, 100, 2.0)], monotonic=tick, wall_time=tick, sleeper=lambda s: None,\n"
    "                               role_runtimes=runtimes)\n"
    "def answers(gate):\n"
    "    return [repr(gate.anti_clobber_trace(T['main'])), repr(gate.anti_clobber_trace(T['director'])), gate.is_busy(T['main']), gate.is_working(T['main']),\n"
    "            repr(gate.composer_snapshot(T['main'])), repr(gate.child_work_trace(T['main'])), repr(gate._child_work_sample(100)), gate.role_runtimes]\n"
    "out = [answers(build()), repr(nl.composer_snapshot_from_pane_text(texts[T['main']])), repr(nl.descendant_work_sample(100, [(100, 1, 5, 100, 1.0), (101, 100, 7, 100, 2.0)]))]\n"
    "for name, value in (('TRUSTED_IDLE_SOURCES', frozenset()), ('composer_snapshot_from_pane_text', lambda text: nl.ComposerSnapshot(False, error='rebound')),\n"
    "                    ('descendant_work_sample', lambda pid, table: nl.ChildWorkSample(False)), ('DEFAULT_ROLE_RUNTIMES', {'main': 'gemini'})):\n"
    "    saved = getattr(nl, name); setattr(nl, name, value)\n"
    "    try: out.append(answers(build(None if name == 'DEFAULT_ROLE_RUNTIMES' else {'main': 'claude', 'director': 'claude'})))\n"
    "    finally: setattr(nl, name, saved)\n"
    "shutil.rmtree(root)\n"
    "text = json.dumps(out).replace(nl.DEFAULT_PROJECT + '-', 'PROJECT-')\n"
    "print(len(out), len(set(json.dumps(o) for o in out)), hashlib.sha256(text.encode()).hexdigest(), 'control', control, 'host paths', len(hits))\n"
)


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.ticket_board.pane_activity_gate as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never notify_listener: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_same_defaults() -> None:
    for order in (("scripts.ticket_board.pane_activity_gate", "scripts.ticket_board.notify_listener"),
                  ("scripts.ticket_board.notify_listener", "scripts.ticket_board.pane_activity_gate"),
                  ("scripts.role_runtime", "scripts.ticket_board.pane_activity_gate")):
        result = python("import importlib, subprocess, time; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.notify_listener as t, scripts.ticket_board.pane_activity_gate as m; "
                        "d = m.PaneActivityGate.__init__.__kwdefaults__; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {MOVED!r} if callable(getattr(m, n))}}), "
                        "d['process_table_reader'] is t.read_process_table, all(d[k] is subprocess.run for k in ('client_activity_runner', 'cursor_position_runner', 'capture_pane_runner')), "
                        "(d['monotonic'], d['wall_time'], d['sleeper']) == (time.monotonic, time.time, time.sleep), d['director_target'] == t.ROLE_TO_TARGET['director'], "
                        "not any(hasattr(m, n) for n in ('listener', 'notify_listener', 'PaneHookStateStore', 'ActivityTrace', 'LOGGER', 'TicketBoardNotifyListener')))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.pane_activity_gate'] True True True True True",
              f"{' then '.join(order)}: one object each, defined here; every default the object it bound before; nothing of notify_listener bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.subprocess is subprocess and m.time is time and m.os is os, "the standard-library names are the module's own, the very objects notify_listener holds")


def test_the_installed_import_form_holds_its_own_one_set_and_reads_through_its_own_listener() -> None:
    # The installed entry point puts scripts/ on the path and imports ticket_board.notify_listener: that package's gate is
    # its own module, re-exported there, and reads that package's notify_listener when it runs.
    result = python(f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                    "import ticket_board.notify_listener as nl, ticket_board.pane_activity_gate as g, scripts.ticket_board.notify_listener as other\n"
                    f"print(all(getattr(nl, n) is getattr(g, n) for n in {MOVED!r}), g.PaneActivityGate.__module__, nl.PaneActivityGate is not other.PaneActivityGate)\n"
                    "saved = nl.composer_snapshot_from_pane_text; nl.composer_snapshot_from_pane_text = lambda text: 'THROUGH ticket_board'\n"
                    "import subprocess\n"
                    "gate = nl.PaneActivityGate(capture_pane_runner=lambda argv, **k: subprocess.CompletedProcess(argv, 0, 'x', ''), state_store=nl.PaneHookStateStore('/tmp'))\n"
                    "print(gate.composer_snapshot('p:0.0')); nl.composer_snapshot_from_pane_text = saved")
    check(result.returncode == 0 and result.stdout.splitlines() == ["True ticket_board.pane_activity_gate True", "THROUGH ticket_board"],
          f"the installed form: its own objects, and its gate reads its own notify_listener when it runs: {result.stdout}{result.stderr[-600:]}")


def test_both_import_forms_answer_alike_and_look_at_no_host_path() -> None:
    package = python(HOST_RECORDER + "import scripts.ticket_board.notify_listener as nl\n" + GATE_PROBE)
    installed = python(HOST_RECORDER + f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\nimport ticket_board.notify_listener as nl\n" + GATE_PROBE)
    check(package.returncode == installed.returncode == 0 and package.stdout == installed.stdout and package.stdout.startswith("7 7 ")
          and package.stdout.strip().endswith(" control 1 host paths 0"),
          f"the package and the installed form give the same seven answers, every rebind a different one, with no host path looked at (the recorder's "
          f"positive control caught): {package.stdout}{package.stderr[-400:]} | {installed.stdout}{installed.stderr[-400:]}")


def test_the_readers_build_their_gate_from_notify_listener_when_they_run() -> None:
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
    result = python("import scripts.ticket_board.notify_listener as t, scripts.ticket_board.pane_activity_gate as m\n"
                    "saved = t.PaneActivityGate; t.PaneActivityGate = 'PATCHED'\n"
                    "seen = __import__('scripts.ticket_board.notify_listener', fromlist=['PaneActivityGate']).PaneActivityGate\n"
                    "t.PaneActivityGate = saved\n"
                    "print(seen, __import__('scripts.ticket_board.notify_listener', fromlist=['PaneActivityGate']).PaneActivityGate is m.PaneActivityGate)")
    check(result.stdout.strip() == "PATCHED True", f"a patch on notify_listener is what a call-time import sees; restored, it is the module's own: {result.stdout}{result.stderr[-400:]}")


def test_the_seams_read_through_notify_listener_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "pane_activity_gate.py").read_text(encoding="utf-8"))
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
            check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and own == [CALL_TIME_IMPORT],
                  f"{label}: notify_listener imported first thing (after its docstring), and nothing else: {own}")
        else:
            check(own == [], f"{label}: reads nothing of notify_listener and imports nothing: {own}")
        skip = set()
        for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [v for v in (node.args.vararg, node.args.kwarg) if v]:
            if a.annotation is not None:
                skip |= {id(y) for y in ast.walk(a.annotation)}
        for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None] + ([node.returns] if node.returns is not None else []):
            skip |= {id(y) for y in ast.walk(d)}
        skip |= {id(y) for x in ast.walk(node) if isinstance(x, ast.AnnAssign) for y in ast.walk(x.annotation)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and id(x) not in skip
                       and (x.id in expected or x.id in MOVED or x.id in ("PaneHookStateStore", "ActivityTrace", "ComposerSnapshot", "LOGGER"))})
        check(bare == [], f"{label}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == MODULE_IMPORTS and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try))], f"the standard library only, at load: {top}")
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
    check(names == list(MOVED), f"the twenty-five, in notify_listener's order, and nothing else: {names}")


def test_notify_listener_reexports_them_and_names_them_as_before() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "notify_listener.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "pane_activity_gate" and n.level == 1]
    check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the twenty-five, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else n.targets[0].id for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.ClassDef)) or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name))}
    check(not defined & set(MOVED), f"notify_listener defines none of them: {sorted(defined & set(MOVED))}")
    check({"Sequence", "hashlib"} <= {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names},
          "notify_listener's own imports are left as they were")
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
    # authority check against synthetic assignments (no process ids, so nothing under /proc is read) with a directory that
    # serves both roles and one that serves none; then the same check through the package's main().
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd475-entry-")).resolve()
    try:
        (base / "ok").mkdir(); (base / "empty").mkdir()
        (base / "assignments.json").write_text(json.dumps({"assignments": {"main": {"actual_target": "p475-main:0.0"}, "audit": {"actual_target": "p475-audit:0.0"}}}))
        for target in ("p475-main:0.0", "p475-audit:0.0"):
            (base / "ok" / (target.replace(":", "_") + ".json")).write_text(json.dumps({"target": target, "state": "idle", "updated_at": 1750000000.0, "source": "claude.Stop"}))
        wrapper = ROOT / "scripts" / "ticket-board-notify-listener"

        def entry(*argv: str) -> subprocess.CompletedProcess[str]:
            return python(f"import os, runpy, sys; os.environ['COLUMNS'] = '500'; sys.path.insert(0, {str(ROOT / 'scripts')!r}); sys.argv = [{str(wrapper)!r}, *{list(argv)!r}]\n"
                          f"runpy.run_path({str(wrapper)!r}, run_name='__main__')")

        verify = ["--verify-pane-state-authority", "--assignments-json", str(base / "assignments.json"), "--pane-state-dir"]
        helped, ok, empty = entry("--help"), entry(*verify, str(base / "ok")), entry(*verify, str(base / "empty"))
        through = python(f"import os; os.environ['COLUMNS'] = '500'\nimport scripts.ticket_board.notify_listener as nl\nraise SystemExit(nl.main({verify + [str(base / 'ok')]!r}))")
        check(helped.returncode == 0 and helped.stdout.startswith("usage: ticket-board-notify-listener")
              and all(flag in helped.stdout for flag in ("--director-composing-timeout-seconds", "--idle-working-timer-sample-delay-seconds", "--verify-pane-state-authority")),
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

    check(value("composer: empty")["error"] == "empty_capture" and value("composer: idle")["available"] is True
          and value("digest: idle") != value("digest: working") and len(value("digest: empty")) == 64,
          "an empty capture has no composer, a captured pane has one; the digest is the pane text's sha256")
    tree = value("descendants: a tree")
    check(tree["observed"] is True and tree["pids"] == [101, 102, 103, 104] and tree["cpu_ticks"] == 7 + 11 + 13 + 0 and tree["detached"] == [102, 103]
          and tree["cpu_by_pid"] == [[101, 7], [102, 11], [103, 13], [104, 0]]
          and all(value(f"descendants: {k}")["pids"] == [] and value(f"descendants: {k}")["observed"] is True for k in ("a leaf", "unknown pane", "empty table")),
          "the child-work sample is every descendant of the pane, its CPU in total and per pid, and those in a session of their own; a leaf has none")
    table = value("process table: synthetic")
    check(sorted(row[0] for row in table) == [100, 101, 102] and [100, 1, 5, 100, 1700000001.0] in table and [102, 101, 11, 102, 1700000003.0] in table
          and value("process table: missing") == [] and value("boot time: synthetic") == 1700000000.0 and value("boot time: missing") == 0.0,
          "the process table is (pid, parent, user+system ticks, session, boot time + start) for every readable pid under the given /proc, a malformed "
          "one skipped, and nothing when it is missing")
    consts = value("constants")
    check(consts["DEFAULT_PROJECT"] == "PROJECT" and consts["ROLE_TO_TARGET"]["director"] == "PROJECT-director:0.0" and set(consts["IDLE_TURN_END_SOURCES"]) <= set(consts["TRUSTED_IDLE_SOURCES"]),
          "every role's pane is named for the project; every turn-end source is a trusted idle source")
    gate = {label[len("gate: "):]: value(label) for label in GOLDEN if label.startswith("gate: ")}
    check(gate["idle after a turn"]["main: is_busy"] is False and gate["busy hook"]["main: is_busy"] is True and gate["no hook state"]["main: anti_clobber_trace"]["reason"] == "no_hook_state"
          and gate["director typing"]["director: anti_clobber_trace"]["reason"] == "human_composing" and gate["blocked on a permission"]["main: is_busy"] is True,
          "an idle pane after a turn may be typed into; a busy hook, a missing hook, a person typing and a permission prompt may not")
    check(list(gate["a permission prompt waiting"]["permission_prompt_waits"]) == ["main"] and gate["blocked on a permission"]["permission_prompt_waits"] == {},
          "a pane blocked by a permission-prompt source is reported waiting since its hook wrote it; a pane blocked by another source is not")
    check(gate["blocked on a permission"]["main: is_busy trace"]["reason"] == "hook_blocked" and gate["busy hook"]["main: is_busy trace"]["reason"] == "hook_busy",
          "a blocked hook is recorded as blocked, a busy one as busy")
    check(gate["a pane named apart from its role"]["custom-pane:0.0: anti_clobber_trace"]["reason"] == "foreign_runtime_working_timer_idle",
          "the role map, not the session name, says whose a pane is: a Claude hook in the pane the map gives a Codex role is a foreign runtime")
    check(all(g["main: child_work_trace after new work"]["busy"] for g in gate.values() if "main: child_work_trace after new work" in g),
          "work a turn starts in the pane's children keeps it busy")
    check(all(calls(f"gate: {s}").get("composer_snapshot_from_pane_text", 0) >= 1 and calls(f"gate: {s}").get("descendant_work_sample", 0) >= 1 for s in gate),
          "the gate reaches the composer snapshot and the child-work sample on notify_listener, in every scenario")
    for name in ("TRUSTED_IDLE_SOURCES", "DEFAULT_ROLE_RUNTIMES", "composer_snapshot_from_pane_text", "descendant_work_sample", "DEFAULT_PROJECT", "ROLE_TO_TARGET"):
        base = value("gate: an unconfigured pane" if name == "DEFAULT_PROJECT" else "gate: idle after a turn")
        got = value(f"rebound on notify_listener: {name}")
        check(got != base, f"{name}, rebound on notify_listener, changes what the gate answers: read there when it runs")


def test_every_seam_is_reached() -> None:
    names = {name for reads in SEAMS.values() for name in reads}
    check(set(PASSED) <= REACHED and set(PASSED) <= names and {f"{n} rebound" for n in ("composer_snapshot_from_pane_text", "descendant_work_sample")} <= REACHED
          and all(hasattr(t, n) for n in names), f"a recorder on notify_listener reached every evidence helper the gate reads: missing {sorted(set(PASSED) - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_the_same_defaults",
             "test_the_installed_import_form_holds_its_own_one_set_and_reads_through_its_own_listener", "test_both_import_forms_answer_alike_and_look_at_no_host_path",
             "test_the_readers_build_their_gate_from_notify_listener_when_they_run", "test_the_seams_read_through_notify_listener_and_nothing_bound",
             "test_notify_listener_reexports_them_and_names_them_as_before", "test_the_entry_point_answers_as_before")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"pane_activity_gate_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
