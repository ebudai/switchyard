#!/usr/bin/env python3
"""SYRD-480: the listener's reminder generators, owned by `IdleNudges` instead of the listener class.

`TicketBoardNotifyListener` delegates its per-pass reminder generators to one
collaborator in `scripts/ticket_board/idle_nudges.py`. That collaborator owns
what the generators remember between passes: which turn-end boundary and which
present-idle instant each role was already acted on, and when work was last
seen in each pane. It also owns the five timings the generators pass to the
board. This pins what makes that a real decomposition, and a safe one:

- **The state is the collaborator's.** The three maps and five timings live on
  `IdleNudges`, and nothing in the listener names a map. The listener keeps its
  three public passes as delegations, and the timings as properties over the
  collaborator's, so a caller that sets one after construction reaches the
  generator.
- **Its inputs are narrow.** The collaborator reaches the listener only through
  two providers, the activity gate and the role targets, which it calls when a
  pass runs because the listener rebinds both. It also takes the logger, and
  the connection passed into each pass. It owns no transaction: each generator
  is one statement on the listener's autocommit connection, as before.
- **No cycle, one class**, whichever module is imported first, in both import
  forms: the tests' `scripts.ticket_board` and the installed entry point's
  `ticket_board`. The module alone loads only its package. Its one call-time
  read of `notify_listener` (`ActivityTrace` and `WORK_EVIDENCE_REASONS`) goes
  through it, so a patch there reaches it.
- **The behaviour is the baseline's**, driven through the listener's public
  passes in the order `listen_once` runs them. That covers:
  - turn-end dedupe, present idle, work observed, permission-prompt waits;
  - every answer the board can give, and unreadable hook state;
  - the gate and targets rebound, and every timing set after construction;
  - the graces from the environment, and the loop order.

  `GOLDEN` below was produced by running the BASELINE listener over the very
  cases embedded here (`gold480.py`), not typed. It also counts how often each
  of the twelve reminder methods ran, wherever they are defined. It is
  byte-identical under `env -i`, in a normal role pane, with another HOME,
  USER, COLUMNS, TMPDIR, project and grace environment, under umask 077 and
  under several hash seeds.
- **A collaborator alone answers as the listener does**: no listener is needed
  to test the generators.
- **The entry point answers as before:** the installed
  `ticket-board-notify-listener --help` and its offline pane-state authority
  check.

Nothing touches a database, a pane or a process. No real home, tenant,
account, /etc, /var, /opt or /proc path is read or written; a recorder with a
positive control for each, through stat, open and pathlib alike, shows it.
Spawns, every exec, signals, account and group lookups and socket connections
are refused for each case.
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

# notify_listener first, as the listener's entry point does.
from scripts.ticket_board import notify_listener as t  # noqa: E402,I001
from scripts.ticket_board import idle_nudges as m  # noqa: E402

CHECKS = 0
#: What IdleNudges owns: the three maps nothing else in the listener touched, and the five timings the generators read.
OWNED_STATE = ("_seen_turn_end_idle_since_by_role", "_consumed_present_idle_since_by_role", "_work_observed_at_by_role")
TIMINGS = ("idle_stall_grace_seconds", "idle_stall_nudge_cadence_seconds", "idle_stall_escalate_after", "unresolved_turn_grace_seconds",
           "permission_prompt_grace_seconds")
#: The listener's public passes, which it keeps as delegations, in the order listen_once runs them.
PUBLIC = ("process_idle_turn_end_nudges", "process_idle_stall_nudges", "process_serial_focus_queue_wakeups")
#: The BASELINE listener's own behaviour for the cases below (`gold480.py`, run on the baseline under the guard).
GOLDEN = {
    'no gate owner': {'calls': {'_fresh_turn_end_idle_since_by_role': 2, '_idle_since_by_role': 4, '_present_fresh_idle_since_by_role': 2, '_process_permission_prompt_waits': 2, '_process_unresolved_turn_end': 2, '_turn_end_idle_since_by_role': 2, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 2, 'process_idle_turn_end_nudges': 2, 'process_serial_focus_queue_wakeups': 2}, 'result': {'answers': [[0, 0, 1], [0, 0, 1]], 'asked': None, 'calls': [['notify_unresolved_turn_end', ['{}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None], ['notify_unresolved_turn_end', ['{}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'a turn end, deduped, then a new boundary, then none': {'calls': {'_fresh_turn_end_idle_since_by_role': 4, '_idle_since_by_role': 6, '_present_fresh_idle_since_by_role': 2, '_process_permission_prompt_waits': 4, '_process_unresolved_turn_end': 4, '_record_work_observed_from_gate': 6, '_reset_busy_backoff_for_idle_roles': 4, '_turn_end_idle_since_by_role': 4, '_work_observed_at_for_roles': 7, 'process_idle_stall_nudges': 4, 'process_idle_turn_end_nudges': 4, 'process_serial_focus_queue_wakeups': 4}, 'result': {'answers': [[1, 1, 1], [0, 1, 1], [1, 1, 1], [1, 1, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_unresolved_turn_end', ['{}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:05:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:05:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:05:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'a boundary that disappears and comes back': {'calls': {'_fresh_turn_end_idle_since_by_role': 3, '_idle_since_by_role': 4, '_present_fresh_idle_since_by_role': 1, '_process_permission_prompt_waits': 3, '_process_unresolved_turn_end': 3, '_record_work_observed_from_gate': 4, '_turn_end_idle_since_by_role': 3, '_work_observed_at_for_roles': 5, 'process_idle_stall_nudges': 3, 'process_idle_turn_end_nudges': 3, 'process_serial_focus_queue_wakeups': 3}, 'result': {'answers': [[1, 0, 1], [0, 0, 1], [1, 0, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None], ['notify_unresolved_turn_end', ['{}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None], ['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'present idle without a turn end': {'calls': {'_fresh_turn_end_idle_since_by_role': 4, '_idle_since_by_role': 8, '_present_fresh_idle_since_by_role': 4, '_process_permission_prompt_waits': 4, '_process_unresolved_turn_end': 4, '_record_work_observed_from_gate': 8, '_reset_busy_backoff_for_idle_roles': 3, '_turn_end_idle_since_by_role': 4, '_work_observed_at_for_roles': 6, 'process_idle_stall_nudges': 4, 'process_idle_turn_end_nudges': 4, 'process_serial_focus_queue_wakeups': 4}, 'result': {'answers': [[1, 1, 1], [0, 1, 1], [1, 1, 1], [0, 0, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"audit": "not an instant", "director": "2026-01-01T00:00:00+00:00", "main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"audit": "not an instant", "director": "2026-01-01T00:00:00+00:00", "main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_unresolved_turn_end', ['{}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"audit": "not an instant", "director": "2026-01-01T00:00:00+00:00", "main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"audit": "not an instant", "director": "2026-01-01T00:00:00+00:00", "main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:09:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:09:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:09:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:09:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:09:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_unresolved_turn_end', ['{}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'work observed and passed on': {'calls': {'_fresh_turn_end_idle_since_by_role': 2, '_idle_since_by_role': 3, '_present_fresh_idle_since_by_role': 1, '_process_permission_prompt_waits': 2, '_process_unresolved_turn_end': 2, '_record_work_observed_from_gate': 3, '_reset_busy_backoff_for_idle_roles': 2, '_turn_end_idle_since_by_role': 2, '_work_observed_at_for_roles': 4, 'process_idle_stall_nudges': 2, 'process_idle_turn_end_nudges': 2, 'process_serial_focus_queue_wakeups': 2}, 'result': {'answers': [[1, 1, 1], [1, 1, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']], ['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"audit": "2026-01-01T00:05:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['audit', '2026-01-01T00:05:00+00:00']], ['notify_unresolved_turn_end', ['{"audit": "2026-01-01T00:05:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{"main": "NOW"}']], ['notify_serial_focus_queue_wakeups', None], ['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{"main": "NOW"}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{"main": "NOW"}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'a permission-prompt wait': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 2, '_present_fresh_idle_since_by_role': 1, '_process_permission_prompt_waits': 1, '_process_unresolved_turn_end': 1, '_record_work_observed_from_gate': 2, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 1, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[0, 0, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_unresolved_turn_end', ['{}', '600 seconds']], ['notify_permission_prompt_waits', ['{"main": "2026-01-01T00:00:00+00:00"}', '120 seconds']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Told the director about 2 pane(s) stopped on a permission prompt: main'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'the board answers nothing': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[0, 0, 0]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [], 'second_gate_asked': None}},
    'the board answers zero': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_process_permission_prompt_waits': 1, '_process_unresolved_turn_end': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[0, 0, 0]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['notify_permission_prompt_waits', ['{"audit": "2026-01-01T00:00:00+00:00"}', '120 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [], 'second_gate_asked': None}},
    'the board answers text': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[0, 0, 0]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [], 'second_gate_asked': None}},
    'the board answers a dict': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_process_permission_prompt_waits': 1, '_process_unresolved_turn_end': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[3, 3, 3]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['notify_permission_prompt_waits', ['{"audit": "2026-01-01T00:00:00+00:00"}', '120 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 3 idle turn-end ticket nudges'], ['info', 'Enqueued 3 unresolved turn-end Director handoffs'], ['info', 'Told the director about 3 pane(s) stopped on a permission prompt: audit'], ['info', 'Reset busy notification backoff for 3 idle-pane rows'], ['info', 'Enqueued 3 idle-stall ticket nudges'], ['info', 'Enqueued 3 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'the board answers by raising': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[0, 0, 0]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['warning', 'Failed to enqueue idle turn-end reminders: notify_idle_turn_end_nudges unavailable'], ['warning', 'Failed to reset busy notification backoff for idle panes: reset_notification_backoff_for_idle_roles unavailable'], ['warning', 'Failed to enqueue idle-stall nudges: notify_idle_stall_nudges unavailable'], ['warning', 'Failed to enqueue serial-focus queue wake-ups: notify_serial_focus_queue_wakeups unavailable']], 'second_gate_asked': None}},
    'unreadable hook state': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 2, '_present_fresh_idle_since_by_role': 1, '_process_permission_prompt_waits': 1, '_process_unresolved_turn_end': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 1, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[0, 0, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_unresolved_turn_end', ['{}', '600 seconds']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['warning', 'Failed to read pane turn-end idle hook state for reminders: turn-end state unreadable'], ['warning', 'Failed to read pane idle hook state for stall nudges: hook state unreadable'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['warning', 'Failed to read pane idle hook state for stall nudges: hook state unreadable'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': None}},
    'targets and gate rebound after construction': {'calls': {'_fresh_turn_end_idle_since_by_role': 2, '_idle_since_by_role': 4, '_present_fresh_idle_since_by_role': 2, '_process_permission_prompt_waits': 2, '_process_unresolved_turn_end': 2, '_record_work_observed_from_gate': 4, '_reset_busy_backoff_for_idle_roles': 2, '_turn_end_idle_since_by_role': 2, '_work_observed_at_for_roles': 4, 'process_idle_stall_nudges': 2, 'process_idle_turn_end_nudges': 2, 'process_serial_focus_queue_wakeups': 2}, 'result': {'answers': [[1, 1, 1], [1, 1, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'main']], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '45 seconds', '1800 seconds', 2, '{}']], ['notify_serial_focus_queue_wakeups', None], ['notify_idle_turn_end_nudges', ['{"ops": "2026-01-01T00:05:00+00:00"}', '45 seconds', '{"ops": "NOW"}']], ['consume_turn_continuation', ['ops', '2026-01-01T00:05:00+00:00']], ['notify_unresolved_turn_end', ['{"ops": "2026-01-01T00:05:00+00:00"}', '600 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"ops": "2026-01-01T00:05:00+00:00"}']], ['notify_idle_stall_nudges', ['{"ops": "2026-01-01T00:05:00+00:00"}', '45 seconds', '1800 seconds', 2, '{"ops": "NOW"}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'second_gate_asked': [['turn_end'], ['idle', ['ops']], ['idle', ['ops']]]}},
    'timings set after construction': {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_process_permission_prompt_waits': 1, '_process_unresolved_turn_end': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'answers': [[1, 1, 1]], 'asked': [['turn_end'], ['idle', ['audit', 'director', 'main']]], 'calls': [['notify_idle_turn_end_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '12.5 seconds', '{}']], ['consume_turn_continuation', ['main', '2026-01-01T00:00:00+00:00']], ['notify_unresolved_turn_end', ['{"main": "2026-01-01T00:00:00+00:00"}', '42 seconds']], ['notify_permission_prompt_waits', ['{"main": "2026-01-01T00:00:00+00:00"}', '7 seconds']], ['reset_notification_backoff_for_idle_roles', ['{"main": "2026-01-01T00:00:00+00:00"}']], ['notify_idle_stall_nudges', ['{"main": "2026-01-01T00:00:00+00:00"}', '12.5 seconds', '60 seconds', 5, '{}']], ['notify_serial_focus_queue_wakeups', None]], 'log': [['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Told the director about 1 pane(s) stopped on a permission prompt: main'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'read_back': [42, 7, 12.5, 60, 5], 'second_gate_asked': None}},
    'graces from the environment': {'calls': {}, 'result': {'empty': [600, 120, 45.0, 1800.0, 2, 0.0], 'set': [33, 9, 45.0, 1800.0, 2, 0.0], 'unset': [600, 120, 45.0, 1800.0, 2, 0.0]}},
    "the listen loop's order": {'calls': {'_fresh_turn_end_idle_since_by_role': 1, '_idle_since_by_role': 1, '_process_permission_prompt_waits': 1, '_process_unresolved_turn_end': 1, '_record_work_observed_from_gate': 1, '_reset_busy_backoff_for_idle_roles': 1, '_turn_end_idle_since_by_role': 1, '_work_observed_at_for_roles': 2, 'process_idle_stall_nudges': 1, 'process_idle_turn_end_nudges': 1, 'process_serial_focus_queue_wakeups': 1}, 'result': {'delivered': 0, 'log': [['info', 'LISTEN ticket_board_state_transition established'], ['info', 'Emitted 1 due reminder-snooze notices'], ['info', 'Enqueued 1 idle turn-end ticket nudges'], ['info', 'Enqueued 1 unresolved turn-end Director handoffs'], ['info', 'Reset busy notification backoff for 1 idle-pane rows'], ['info', 'Enqueued 1 idle-stall ticket nudges'], ['info', 'Enqueued 1 serial-focus capacity hand-offs']], 'order': ['refresh_workflow', 'process_due_notifications'], 'statements': ['Composed', 'emit_due_reminder_snoozes', 'notify_idle_turn_end_nudges', 'consume_turn_continuation', 'notify_unresolved_turn_end', 'reset_notification_backoff_for_idle_roles', 'notify_idle_stall_nudges', 'notify_serial_focus_queue_wakeups']}},
    # SYRD-537 added the reminder-snooze due pass (its statement and log line) FIRST, before the producers.
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: The module's imports at load: the standard library only.
MODULE_IMPORTS = ['from __future__ import annotations', 'import json', 'from datetime import datetime, timezone', 'from typing import Any, Callable, Mapping']
#: The call-time import the one method that reads notify_listener starts with.
CALL_TIME_IMPORT = "from . import notify_listener as listener"

# --- the cases, shared verbatim with `gold480.py` (which ran them on the baseline) ------------------------------------
# A case builds a listener with a stand-in gate (a real bound method whose `__self__` answers the four questions the
# reminder generators ask) and a connection that records every statement and answers from a script, runs the listener's
# public passes in the order `listen_once` does, and records the statements, the answers, the log, what the gate was
# asked, and how many times each of the twelve reminder methods ran -- wherever they are defined. No database, pane,
# process or /proc is touched. Instants the code takes from the clock are recorded as NOW.
import re as _re

NOW = _re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+\+00:00")
REMINDER_METHODS = ("_reset_busy_backoff_for_idle_roles", "_idle_since_by_role", "_record_work_observed_from_gate", "_work_observed_at_for_roles",
                    "_turn_end_idle_since_by_role", "_fresh_turn_end_idle_since_by_role", "_present_fresh_idle_since_by_role",
                    "_process_permission_prompt_waits", "_process_unresolved_turn_end", "process_idle_turn_end_nudges",
                    "process_serial_focus_queue_wakeups", "process_idle_stall_nudges")
BOARD = ("notify_idle_turn_end_nudges", "notify_unresolved_turn_end", "notify_permission_prompt_waits", "notify_idle_stall_nudges",
         "reset_notification_backoff_for_idle_roles", "notify_serial_focus_queue_wakeups", "consume_turn_continuation")
T0, T1, T2 = "2026-01-01T00:00:00+00:00", "2026-01-01T00:05:00+00:00", "2026-01-01T00:09:00+00:00"
CASES = {
    "no gate owner": {"gate": None, "steps": [{}, {}]},
    "a turn end, deduped, then a new boundary, then none": {"gate": {"turn_end": {"main": T0}, "idle": {"main": T0}},
                                                          "steps": [{}, {}, {"turn_end": {"main": T1}}, {"turn_end": {}}]},
    "a boundary that disappears and comes back": {"gate": {"turn_end": {"main": T0}}, "steps": [{}, {"turn_end": {}}, {"turn_end": {"main": T0}}]},
    "present idle without a turn end": {"gate": {"idle": {"main": T0, "audit": "not an instant", "director": T0}},
                                        "steps": [{}, {}, {"idle": {"main": T2}}, {"idle": {}}]},
    "work observed and passed on": {"gate": {"idle": {"main": T0}, "turn_end": {"audit": T1},
                                             "traces": {"p-main:0.0": [True, "WORK"], "p-audit:0.0": [True, "hook_blocked"], "p-director:0.0": [False, "WORK"]}},
                                    "steps": [{}, {}]},
    "a permission-prompt wait": {"gate": {"waits": {"main": T0}}, "answers": {"notify_permission_prompt_waits": [2]}, "steps": [{}]},
    **{f"the board answers {label}": {"gate": {"turn_end": {"main": T0}, "idle": {"main": T0}, "waits": {"audit": T0}},
                                      "answers": {n: answer for n in BOARD}, "steps": [{}]}
       for label, answer in (("nothing", None), ("zero", [0]), ("text", ["x"]), ("a dict", {"n": 3}), ("by raising", "raise"))},
    "unreadable hook state": {"gate": {"turn_end": {"main": "x"}, "idle": {"main": "x"}, "fail": ["idle", "turn_end"]}, "steps": [{}]},
    "targets and gate rebound after construction": {"gate": {"idle": {"main": T0}},
                                                    "steps": [{}, {"targets": {"ops": "p-ops:0.0"}, "new_gate": {"idle": {"ops": T1}, "traces": {"p-ops:0.0": [True, "WORK"]}}}]},
    "timings set after construction": {"gate": {"turn_end": {"main": T0}, "idle": {"main": T0}, "waits": {"main": T0}},
                                       "kwargs": {"idle_stall_grace_seconds": 45, "idle_stall_nudge_cadence_seconds": 300, "idle_stall_escalate_after": 2},
                                       "set": {"unresolved_turn_grace_seconds": 42, "permission_prompt_grace_seconds": 7, "idle_stall_grace_seconds": 12.5,
                                               "idle_stall_nudge_cadence_seconds": 60, "idle_stall_escalate_after": 5}, "steps": [{}]},
    "graces from the environment": {"env": True},
    "the listen loop's order": {"loop": True},
}


def run_case(t, spec, reached):
    """One case through notify_listener `t`'s listener; the reminder methods are counted where they are defined."""
    import json as _json, os as _os, types as _types
    L = t.TicketBoardNotifyListener
    owner = getattr(t, "IdleNudges", L)
    work = sorted(t.WORK_EVIDENCE_REASONS)[0]
    counts = {}

    class Gate:
        def __init__(self, idle=None, turn_end=None, traces=None, waits=None, fail=()):
            self.idle, self.turn_end, self.waits, self.fail = dict(idle or {}), dict(turn_end or {}), dict(waits or {}), set(fail)
            self.traces = {k: t.ActivityTrace(v[0], work if v[1] == "WORK" else v[1]) for k, v in (traces or {}).items()}
            self.asked = []

        def is_working(self, target):
            return False

        def idle_since_by_role(self, roles):
            self.asked.append(["idle", list(roles)])
            if "idle" in self.fail:
                raise RuntimeError("hook state unreadable")
            return {r: v for r, v in self.idle.items() if r in roles}

        def turn_end_idle_since_by_role(self):
            self.asked.append(["turn_end"])
            if "turn_end" in self.fail:
                raise RuntimeError("turn-end state unreadable")
            return dict(self.turn_end)

        def last_trace(self, target):
            if target in self.traces:
                return self.traces[target]
            raise KeyError(target)

        def permission_prompt_waits(self):
            return dict(self.waits)

    class Conn:
        def __init__(self, answers=None):
            self.calls, self.answers = [], answers or {}

        def execute(self, statement, params=None):
            text = " ".join(str(statement).split())
            name = (_re.search(r"ticket_board\.(\w+)", text) or _re.search(r"(\w+)", text)).group(1)
            self.calls.append([name, list(params) if params is not None else None])
            answer = self.answers.get(name, [1])
            if answer == "raise":
                raise RuntimeError(f"{name} unavailable")
            answer = tuple(answer) if isinstance(answer, list) else answer
            return _types.SimpleNamespace(fetchone=lambda: answer)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Log:
        def __init__(self):
            self.lines = []

        def info(self, msg, *args):
            self.lines.append(["info", msg % args if args else msg])

        def warning(self, msg, *args):
            self.lines.append(["warning", msg % args if args else msg])

        debug = error = info

    def build(gate, kwargs=None):
        log = Log()
        listener = L(conninfo="", sender=lambda a, b: None, activity_gate=gate.is_working if gate else (lambda *a, **k: False), logger=log, **(kwargs or {}))
        listener.role_targets = {"main": "p-main:0.0", "audit": "p-audit:0.0", "director": "p-director:0.0"}
        return listener, log

    def one_pass(listener, conn):
        return [listener.process_idle_turn_end_nudges(conn), listener.process_idle_stall_nudges(conn), listener.process_serial_focus_queue_wakeups(conn)]

    saved = {n: owner.__dict__[n] for n in REMINDER_METHODS}

    def counted(name, fn):
        def wrapper(*a, **k):
            reached.add(name)
            counts[name] = counts.get(name, 0) + 1
            return fn(*a, **k)
        return wrapper

    for n in REMINDER_METHODS:
        setattr(owner, n, counted(n, saved[n]))
    env_keys = ("TICKET_BOARD_UNRESOLVED_TURN_GRACE_SECONDS", "TICKET_BOARD_PERMISSION_PROMPT_GRACE_SECONDS")
    saved_env = {k: _os.environ.get(k) for k in env_keys}
    # The two graces default from the environment when a listener is built; every case starts without them.
    for k in env_keys:
        _os.environ.pop(k, None)
    try:
        if spec.get("env"):
            got = {}
            for label, env in (("unset", {}), ("set", {env_keys[0]: "33", env_keys[1]: "9"}), ("empty", {env_keys[0]: "", env_keys[1]: ""})):
                for k in env_keys:
                    _os.environ.pop(k, None)
                _os.environ.update(env)
                listener, _ = build(None)
                got[label] = [listener.unresolved_turn_grace_seconds, listener.permission_prompt_grace_seconds, listener.idle_stall_grace_seconds,
                              listener.idle_stall_nudge_cadence_seconds, listener.idle_stall_escalate_after, listener.present_idle_freshness_seconds]
        elif spec.get("loop"):
            gate = Gate(turn_end={"main": T0}, idle={"main": T0})
            listener, log = build(gate, {"poll_seconds": 0})
            order = []
            conn = Conn({"claim_due_notification": None})
            listener.connector = lambda *a, **k: conn
            listener.refresh_workflow = lambda c: order.append("refresh_workflow")
            listener.process_due_notifications = lambda c, max_notifications=None: order.append("process_due_notifications") or 0
            listener._wait_for_notification = lambda c: order.append("wait") or False
            listener._log_missing_hook_state = lambda: None
            got = {"delivered": listener.listen_once(max_notifications=5), "order": order, "statements": [c[0] for c in conn.calls], "log": log.lines}
        else:
            gate = Gate(**spec["gate"]) if spec.get("gate") else None
            listener, log = build(gate, spec.get("kwargs"))
            for name, value in spec.get("set", {}).items():
                setattr(listener, name, value)
            conn = Conn(spec.get("answers"))
            passes, extra = [], None
            for step in spec["steps"]:
                if gate is not None:
                    for k in ("idle", "turn_end"):
                        if k in step:
                            setattr(gate, k, dict(step[k]))
                if "targets" in step:
                    listener.role_targets = dict(step["targets"])
                if "new_gate" in step:
                    extra = Gate(**step["new_gate"])
                    listener.activity_gate = extra.is_working
                passes.append(one_pass(listener, conn))
            got = {"answers": passes, "calls": conn.calls, "log": log.lines, "asked": gate.asked if gate else None,
                   "second_gate_asked": extra.asked if extra else None}
            if spec.get("set"):
                got["read_back"] = [getattr(listener, n) for n in spec["set"]]
        return _json.loads(NOW.sub("NOW", _json.dumps({"result": got, "calls": dict(sorted(counts.items()))}, sort_keys=True)))
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        return {"result": {"raised": type(exc).__name__, "message": str(exc)[:300]}, "calls": dict(sorted(counts.items()))}
    finally:
        for n in REMINDER_METHODS:
            setattr(owner, n, saved[n])
        for k, v in saved_env.items():
            _os.environ.pop(k, None)
            if v is not None:
                _os.environ[k] = v
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
    "pathlib.Path('/opt/switchyard/syrd480-positive-control').is_file()\n"
    "try: pathlib.Path('/proc/self/stat').read_text()\n"
    "except FileNotFoundError: pass\n"
    "try: list(pathlib.Path('/proc').iterdir())\n"
    "except FileNotFoundError: pass\n"
    "control = len(hits); hits.clear()\n"
)
#: One probe, run in both import forms: the listener's three passes over a stand-in gate and a recording connection,
#: before and after a new turn boundary and a timing set late -- as a digest, and the host paths looked at.
PASS_PROBE = (
    "import hashlib, json, types\n"
    "class Gate:\n"
    "    def __init__(self): self.turn_end = {'main': '2026-01-01T00:00:00+00:00'}; self.idle = dict(self.turn_end)\n"
    "    def is_working(self, target): return False\n"
    "    def idle_since_by_role(self, roles): return {r: v for r, v in self.idle.items() if r in roles}\n"
    "    def turn_end_idle_since_by_role(self): return dict(self.turn_end)\n"
    "    def last_trace(self, target): return nl.ActivityTrace(True, sorted(nl.WORK_EVIDENCE_REASONS)[0])\n"
    "    def permission_prompt_waits(self): return {'audit': '2026-01-01T00:00:00+00:00'}\n"
    "calls = []\n"
    "class Conn:\n"
    "    def execute(self, s, p=None): calls.append([' '.join(str(s).split())[:60], list(p) if p else None]); return types.SimpleNamespace(fetchone=lambda: (1,))\n"
    "class Log:\n"
    "    def info(self, *a): pass\n"
    "    warning = info\n"
    "g = Gate(); l = nl.TicketBoardNotifyListener(conninfo='', sender=lambda a, b: None, activity_gate=g.is_working, logger=Log())\n"
    "l.role_targets = {'main': 'p-main:0.0', 'audit': 'p-audit:0.0'}\n"
    "c = Conn(); out = []\n"
    "for step in range(3):\n"
    "    if step == 1: g.turn_end = {'main': '2026-01-01T00:05:00+00:00'}; l.unresolved_turn_grace_seconds = 42\n"
    "    out.append([l.process_idle_turn_end_nudges(c), l.process_idle_stall_nudges(c), l.process_serial_focus_queue_wakeups(c)])\n"
    "import re; text = re.sub(r'\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d+\\+00:00', 'NOW', json.dumps([out, calls]))\n"
    "print(len(calls), type(l.idle_nudges).__module__.rsplit('.', 1)[-1], hashlib.sha256(text.encode()).hexdigest(), 'control', control, 'host paths', len(hits))\n"
)


def _tree(name: str) -> ast.Module:
    return ast.parse((ROOT / "scripts" / "ticket_board" / name).read_text(encoding="utf-8"))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def test_the_module_loads_only_its_package() -> None:
    result = python("import sys, scripts.ticket_board.idle_nudges as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never notify_listener: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_class_and_the_listener_holds_one_of_it() -> None:
    for order in (("scripts.ticket_board.idle_nudges", "scripts.ticket_board.notify_listener"),
                  ("scripts.ticket_board.notify_listener", "scripts.ticket_board.idle_nudges")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.notify_listener as t, scripts.ticket_board.idle_nudges as m; "
                        "l = t.TicketBoardNotifyListener(conninfo='', sender=lambda a, b: None, activity_gate=lambda *a, **k: False); "
                        "print(t.IdleNudges is m.IdleNudges, m.IdleNudges.__module__, type(l.idle_nudges) is m.IdleNudges, "
                        "not any(hasattr(m, n) for n in ('listener', 'notify_listener', 'TicketBoardNotifyListener', 'ActivityTrace', 'WORK_EVIDENCE_REASONS', 'LOGGER')))")
        check(result.stdout.strip() == "True scripts.ticket_board.idle_nudges True True",
              f"{' then '.join(order)}: one class, defined here, and the listener holds an instance of it; nothing of notify_listener bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.json is json, "the standard-library names are the module's own")


def test_the_installed_import_form_holds_its_own_class_and_reads_its_own_listener() -> None:
    result = python(f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
                    "import ticket_board.notify_listener as nl, ticket_board.idle_nudges as p, scripts.ticket_board.notify_listener as other\n"
                    "print(nl.IdleNudges is p.IdleNudges, p.IdleNudges.__module__, nl.IdleNudges is not other.IdleNudges)\n"
                    "class Gate:\n"
                    "    def is_working(self, target): return False\n"
                    "    def last_trace(self, target): return nl.ActivityTrace(True, 'THROUGH ticket_board')\n"
                    "saved = nl.WORK_EVIDENCE_REASONS; nl.WORK_EVIDENCE_REASONS = frozenset({'THROUGH ticket_board'})\n"
                    "g = Gate(); n = p.IdleNudges(activity_gate=lambda: g.is_working, role_targets=lambda: {'main': 'p-main:0.0'}, logger=None, idle_stall_grace_seconds=1,\n"
                    "    idle_stall_nudge_cadence_seconds=1, idle_stall_escalate_after=1, unresolved_turn_grace_seconds=1, permission_prompt_grace_seconds=1)\n"
                    "n._record_work_observed_from_gate(['main']); nl.WORK_EVIDENCE_REASONS = saved\n"
                    "print(sorted(n._work_observed_at_by_role))")
    check(result.returncode == 0 and result.stdout.splitlines() == ["True ticket_board.idle_nudges True", "['main']"],
          f"the installed form: its own class, and its one call-time read goes through its own notify_listener: {result.stdout}{result.stderr[-600:]}")


def test_both_import_forms_answer_alike_and_look_at_no_host_path() -> None:
    package = python(HOST_RECORDER + "import scripts.ticket_board.notify_listener as nl\n" + PASS_PROBE)
    installed = python(HOST_RECORDER + f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\nimport ticket_board.notify_listener as nl\n" + PASS_PROBE)
    first = package.stdout.split()
    check(package.returncode == installed.returncode == 0 and package.stdout == installed.stdout and first[:2] != ["0"] and first[1] == "idle_nudges"
          and package.stdout.strip().endswith(" control 3 host paths 0"),
          f"the package and the installed form issue the same statements and give the same answers, with no host path or /proc entry looked at "
          f"(all three positive controls caught): {package.stdout}{package.stderr[-400:]} | {installed.stdout}{installed.stderr[-400:]}")


def test_the_state_and_the_timings_are_the_collaborators() -> None:
    listener_source = (ROOT / "scripts" / "ticket_board" / "notify_listener.py").read_text(encoding="utf-8")
    check(not [n for n in OWNED_STATE if n in listener_source], f"notify_listener names none of the three maps: {[n for n in OWNED_STATE if n in listener_source]}")
    tree = _tree("idle_nudges.py")
    cls = _class(tree, "IdleNudges")
    inside = {id(x) for x in ast.walk(cls)}
    outside = sorted({x.attr for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in OWNED_STATE and id(x) not in inside})
    check(outside == [], f"only IdleNudges' own methods touch the maps: {outside}")
    listener = t.TicketBoardNotifyListener(conninfo="", sender=lambda a, b: None, activity_gate=lambda *a, **k: False)
    check(not set(vars(listener)) & set(OWNED_STATE + TIMINGS) and set(OWNED_STATE + TIMINGS) <= set(vars(listener.idle_nudges)),
          f"a listener instance holds none of them; its collaborator holds all eight: {sorted(set(vars(listener)) & set(OWNED_STATE + TIMINGS))}")
    for name in TIMINGS:
        prop = t.TicketBoardNotifyListener.__dict__.get(name)
        check(isinstance(prop, property) and prop.fset is not None, f"{name} is a property with a setter on the listener")
        before = getattr(listener.idle_nudges, name)
        setattr(listener, name, 97)
        check(listener.idle_nudges.__dict__[name] == 97 and getattr(listener, name) == 97, f"{name}: a write on the listener reaches the collaborator, and reads back")
        setattr(listener.idle_nudges, name, before)
        check(getattr(listener, name) == before, f"{name}: the listener reads the collaborator's value")
    ltree = _tree("notify_listener.py")
    lcls = _class(ltree, "TicketBoardNotifyListener")
    methods = {f.name: f for f in lcls.body if isinstance(f, ast.FunctionDef)}
    moved_private = [n for n in REMINDER_METHODS if n not in PUBLIC]
    check(not set(moved_private) & set(methods), f"the listener defines none of the nine private reminder methods: {sorted(set(moved_private) & set(methods))}")
    for name in PUBLIC:
        body = [ast.unparse(s) for s in methods[name].body]
        check(body == [f"return self.idle_nudges.{name}(conn)"], f"{name} is a delegation, and nothing else: {body}")


def test_the_collaborator_reaches_the_listener_only_through_its_providers() -> None:
    tree = _tree("idle_nudges.py")
    cls = _class(tree, "IdleNudges")
    init = next(f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "__init__")
    check([a.arg for a in init.args.kwonlyargs] == ["activity_gate", "role_targets", "logger", *TIMINGS],
          f"its inputs: the two providers, the logger and the five timings: {[a.arg for a in init.args.kwonlyargs]}")
    names = [f.name for f in cls.body if isinstance(f, ast.FunctionDef)]
    check(names == ["__init__", *[n for n in REMINDER_METHODS if n in names]] and sorted(names[1:]) == sorted(REMINDER_METHODS),
          f"the twelve reminder methods, in the listener's order, and nothing else: {names}")
    listener_attrs = sorted({x.attr for x in ast.walk(cls) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self"
                             and x.attr in ("activity_gate", "role_targets")})
    check(listener_attrs == [], f"no copy of the gate or the targets is kept: {listener_attrs}")
    calls = sorted({ast.unparse(x.func) for x in ast.walk(cls) if isinstance(x, ast.Call) and ast.unparse(x.func) in ("self._activity_gate", "self._role_targets")})
    check(calls == ["self._activity_gate", "self._role_targets"], f"both are asked for when a pass runs: {calls}")
    through = {}
    for f in cls.body:
        if isinstance(f, ast.FunctionDef):
            got = sorted({x.attr for x in ast.walk(f) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "listener"})
            if got:
                through[f.name] = got
                check(ast.unparse(f.body[0]) == CALL_TIME_IMPORT, f"{f.name}: notify_listener imported first thing, when it runs")
    check(through == {"_record_work_observed_from_gate": ["ActivityTrace", "WORK_EVIDENCE_REASONS"]}, f"one method reads notify_listener, for two names: {through}")
    bare = sorted({x.id for x in ast.walk(cls) if isinstance(x, ast.Name) and x.id in ("ActivityTrace", "WORK_EVIDENCE_REASONS")})
    check(bare == [], f"and never past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == MODULE_IMPORTS and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try))], f"the standard library only, at load: {top}")


def test_the_listener_wires_it_with_late_bound_providers() -> None:
    ltree = _tree("notify_listener.py")
    imports = [n for n in ltree.body if isinstance(n, ast.ImportFrom) and n.module == "idle_nudges" and n.level == 1]
    check(len(imports) == 1 and [(a.name, a.asname) for a in imports[0].names] == [("IdleNudges", None)], "notify_listener imports IdleNudges once, unaliased")
    lcls = _class(ltree, "TicketBoardNotifyListener")
    init = next(f for f in lcls.body if isinstance(f, ast.FunctionDef) and f.name == "__init__")
    built = [x for x in ast.walk(init) if isinstance(x, ast.Call) and ast.unparse(x.func) == "IdleNudges"]
    check(len(built) == 1, "the constructor builds one collaborator")
    kw = {k.arg: ast.unparse(k.value) for k in built[0].keywords}
    check(kw.get("activity_gate") == "lambda: self.activity_gate" and kw.get("role_targets") == "lambda: self.role_targets" and kw.get("logger") == "logger"
          and all(kw.get(n) for n in TIMINGS) and set(kw) == {"activity_gate", "role_targets", "logger", *TIMINGS},
          f"the gate and the targets as providers that read the listener when called, the logger and the timings: {sorted(kw)}")
    listener = t.TicketBoardNotifyListener(conninfo="", sender=lambda a, b: None, activity_gate=lambda *a, **k: False)
    gate = lambda *a, **k: True  # noqa: E731
    listener.activity_gate = gate
    listener.role_targets = {"ops": "p-ops:0.0"}
    check(listener.idle_nudges._activity_gate() is gate and listener.idle_nudges._role_targets() == {"ops": "p-ops:0.0"},
          "a gate or targets rebound on the listener are what the collaborator sees next")


def test_the_loop_runs_the_passes_where_it_did() -> None:
    lcls = _class(_tree("notify_listener.py"), "TicketBoardNotifyListener")
    listen = next(f for f in lcls.body if isinstance(f, ast.FunctionDef) and f.name == "listen_once")
    order = [x.func.attr for x in ast.walk(listen) if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
             and isinstance(x.func.value, ast.Name) and x.func.value.id == "self" and x.func.attr in ("refresh_workflow", *PUBLIC, "process_reminder_snooze_due", "process_due_notifications")]
    # SYRD-537: the reminder-snooze due pass runs first, so a due batch's notice
    # is written before any producer can remind about its members.
    check(order == ["refresh_workflow", "process_reminder_snooze_due", *PUBLIC, "process_due_notifications"],
          f"listen_once: the workflow, the snooze deadlines, the three passes, then delivery: {order}")


def test_the_entry_point_answers_as_before() -> None:
    # The installed wrapper, run as a program (runpy, in a guarded python child): --help, and the offline pane-state
    # authority check against synthetic assignments (no process ids, so no /proc entry is read); then the same check
    # through the package's main(). Constructing a listener, which the service does, is covered above.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd480-entry-")).resolve()
    try:
        (base / "ok").mkdir()
        (base / "assignments.json").write_text(json.dumps({"assignments": {"main": {"actual_target": "p480-main:0.0"}}}))
        (base / "ok" / "p480-main_0.0.json").write_text(json.dumps({"target": "p480-main:0.0", "state": "idle", "updated_at": 1750000000.0, "source": "claude.Stop"}))
        wrapper = ROOT / "scripts" / "ticket-board-notify-listener"

        def entry(*argv: str) -> subprocess.CompletedProcess[str]:
            return python(f"import os, runpy, sys; os.environ['COLUMNS'] = '500'; sys.path.insert(0, {str(ROOT / 'scripts')!r}); sys.argv = [{str(wrapper)!r}, *{list(argv)!r}]\n"
                          f"runpy.run_path({str(wrapper)!r}, run_name='__main__')")

        verify = ["--verify-pane-state-authority", "--assignments-json", str(base / "assignments.json"), "--pane-state-dir", str(base / "ok")]
        helped, ok = entry("--help"), entry(*verify)
        through = python(f"import os; os.environ['COLUMNS'] = '500'\nimport scripts.ticket_board.notify_listener as nl\nraise SystemExit(nl.main({verify!r}))")
        check(helped.returncode == 0 and helped.stdout.startswith("usage: ticket-board-notify-listener")
              and all(flag in helped.stdout for flag in ("--pane-state-dir", "--verify-pane-state-authority", "--assignments-json")),
              f"the installed --help: {helped.stdout[-300:]}{helped.stderr[-300:]}")
        check(ok.returncode == 0 and ok.stdout.strip() == f"pane-state authority: {base / 'ok'} holds hook state for 1 of 1 registered roles" and through.returncode == 0
              and through.stdout == ok.stdout,
              f"the offline check, through the wrapper and the package alike: {ok.stdout}{ok.stderr[-300:]} | {through.stdout}")
    finally:
        shutil.rmtree(base)


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        with contained():
            got = json.loads(json.dumps(run_case(t, spec, REACHED)))
        check(got == GOLDEN[label], f"{label}: the baseline's statements, answers, log and reminder-method calls: {json.dumps(got)[:600]}")


def test_a_collaborator_alone_answers_as_the_listener_does() -> None:
    class Gate:
        def __init__(self) -> None:
            self.turn_end = {"main": "2026-01-01T00:00:00+00:00"}
            self.idle = dict(self.turn_end)

        def is_working(self, target: str) -> bool:
            return False

        def idle_since_by_role(self, roles):
            return {r: v for r, v in self.idle.items() if r in roles}

        def turn_end_idle_since_by_role(self):
            return dict(self.turn_end)

        def permission_prompt_waits(self):
            return {}

    class Conn:
        def __init__(self) -> None:
            self.calls: list = []

        def execute(self, statement, params=None):
            self.calls.append([" ".join(str(statement).split())[:60], list(params) if params else None])
            return type("R", (), {"fetchone": staticmethod(lambda: (1,))})()

    class Log:
        def info(self, *a: object) -> None:
            pass

        warning = info

    targets = {"main": "p-main:0.0", "audit": "p-audit:0.0"}
    runs = []
    for alone in (True, False):
        gate, conn = Gate(), Conn()
        if alone:
            passes = t.IdleNudges(activity_gate=lambda: gate.is_working, role_targets=lambda: targets, logger=Log(), idle_stall_grace_seconds=45,
                                  idle_stall_nudge_cadence_seconds=1800, idle_stall_escalate_after=2, unresolved_turn_grace_seconds=600,
                                  permission_prompt_grace_seconds=120)
        else:
            passes = t.TicketBoardNotifyListener(conninfo="", sender=lambda a, b: None, activity_gate=gate.is_working, logger=Log(), idle_stall_grace_seconds=45,
                                                 idle_stall_nudge_cadence_seconds=1800, idle_stall_escalate_after=2)
            passes.role_targets = targets
        with contained(), patched(os, environ={}):
            answers = []
            for step in range(3):
                if step == 2:
                    gate.turn_end = {"main": "2026-01-01T00:05:00+00:00"}
                answers.append([getattr(passes, name)(conn) for name in PUBLIC])
        runs.append((answers, conn.calls))
    check(runs[0] == runs[1] and len(runs[0][1]) > 6, f"no listener is needed: the collaborator alone issues the same statements and answers: {runs[0][0]} {runs[1][0]}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    dedupe = result("a turn end, deduped, then a new boundary, then none")
    check(dedupe["answers"][0][0] == 1 and dedupe["answers"][1][0] == 0 and dedupe["answers"][2][0] == 1,
          "a turn-end boundary is acted on once, then not again until a new one arrives")
    back = result("a boundary that disappears and comes back")
    check([a[0] for a in back["answers"]] == [1, 0, 1], "a boundary that leaves the hook state is forgotten, so the same one coming back is acted on again")
    work = [p for n, p in result("work observed and passed on")["calls"] if n == "notify_idle_stall_nudges"]
    check(work and all(p[-1] == '{"main": "NOW"}' for p in work),
          "work is observed only from a busy pane with a work reason: not a blocked one, not an idle one; and the stall nudge is told when")
    named = [c[0] for c in dedupe["calls"]]
    check(named[:3] == ["notify_idle_turn_end_nudges", "consume_turn_continuation", "notify_unresolved_turn_end"]
          and "reset_notification_backoff_for_idle_roles" in named and named.index("reset_notification_backoff_for_idle_roles") < named.index("notify_idle_stall_nudges"),
          "the reminder, then the continuation lease and the unresolved-turn guard; the busy backoff is reset before the stall nudge")
    timed = result("timings set after construction")
    params = [p for _, p in timed["calls"]]
    check(["{\"main\": \"2026-01-01T00:00:00+00:00\"}", "42 seconds"] in params and ["{\"main\": \"2026-01-01T00:00:00+00:00\"}", "7 seconds"] in params
          and any(p and p[1:4] == ["12.5 seconds", "60 seconds", 5] for p in params) and timed["read_back"] == [42, 7, 12.5, 60, 5],
          "every timing set on the listener after construction is what the board is told, and what reads back")
    graces = result("graces from the environment")
    check(graces["set"][:2] == [33, 9] and graces["unset"][:2] == graces["empty"][:2], "the two graces come from the environment when set, the defaults otherwise")
    loop = result("the listen loop's order")
    check(loop["order"][:2] == ["refresh_workflow", "process_due_notifications"]
          and loop["statements"][1] == "emit_due_reminder_snoozes" and loop["statements"][-1] == "notify_serial_focus_queue_wakeups"
          and loop["statements"].index("notify_idle_turn_end_nudges") < loop["statements"].index("notify_idle_stall_nudges"),
          "the loop refreshes the workflow, runs the three passes in order, then delivers")
    rebound = result("targets and gate rebound after construction")
    check(rebound["second_gate_asked"] and any(a[0] == "idle" and a[1] == ["ops"] for a in rebound["second_gate_asked"]),
          "a gate and targets rebound on the listener are the ones the next pass asks")
    raised = result("the board answers by raising")
    # As the baseline has it: a failed reminder statement returns before the unresolved-turn guard and the
    # permission-prompt generator run on that pass (reported on SYRD-480, not changed by this move).
    check(raised["answers"] == [[0, 0, 0]] and [c[0] for c in raised["calls"]] == ["notify_idle_turn_end_nudges", "reset_notification_backoff_for_idle_roles",
                                                                                 "notify_idle_stall_nudges", "notify_serial_focus_queue_wakeups"]
          and [level for level, _ in raised["log"]] == ["warning"] * 4,
          "a board statement that fails is logged and the pass goes on to the next generator")


def test_every_reminder_method_is_reached() -> None:
    check(set(REMINDER_METHODS) <= REACHED, f"every one of the twelve ran in some case: missing {sorted(set(REMINDER_METHODS) - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_its_package", "test_either_import_order_gives_one_class_and_the_listener_holds_one_of_it",
             "test_the_installed_import_form_holds_its_own_class_and_reads_its_own_listener", "test_both_import_forms_answer_alike_and_look_at_no_host_path",
             "test_the_state_and_the_timings_are_the_collaborators", "test_the_collaborator_reaches_the_listener_only_through_its_providers",
             "test_the_listener_wires_it_with_late_bound_providers", "test_the_loop_runs_the_passes_where_it_did", "test_the_entry_point_answers_as_before")
LAST = ("test_every_reminder_method_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"idle_nudges_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
