#!/usr/bin/env python3
"""SYRD-414: command configuration loading and the owner/root/tenant-control crossing, against the launcher they came out of.

The eleven definitions that decide how a per-project `switchyard` command
loads its configuration and crosses to the owner or root moved unchanged into
`scripts/command_crossing.py`, and the launcher re-exports all of them. This
pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads only the three leaf modules its definition-time defaults
  come from; those defaults -- the staged tooling owner, the helper repair,
  the caller-aware `which`, `subprocess.run`, `os.execvp`, `input`, `print` --
  are the very objects the launcher bound; the annotation types are imported
  under TYPE_CHECKING only.
- **Seams (rule 24):** every name the eleven call -- each other included -- is
  read through the launcher as often as before, so a patch there reaches it:
  every case below runs with the launcher's seams standing in and every
  sibling wrapped there.
- **Callers:** `switchyard_main` loads through the launcher's name, as often
  as before, and `presentation_commands.py` reads
  `launcher._load_switchyard_project_config_for_command`.
- **The authority decision is the baseline's:** the path-owner and configured
  owner checks, root, the role account's own unprivileged commands, the
  permission-error crossing (non-root only), the grant and its operator
  checked before any repair or prompt, absent staged tooling repaired and
  hostile tooling refused, promotion offered before a start, the bridge argv,
  the desktop halves, sudo without and with a prompt, every refusal and exit.
  `GOLDEN` below was produced by running the BASELINE launcher's own
  functions over the very cases embedded here (`gold414.py`), not typed; it
  is byte-identical whether generated under `env -i` or in a normal role pane.

Nothing crosses: every effect is a recorder -- the sudo check, the bridge, the
exec, the helper and bundle repairs, the desktop halves, the promotion -- and
the real `subprocess.run`/`Popen`, every `os.exec*`, `os.kill` and socket
connections are refused for each case, as are real account and group lookups.
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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import command_crossing as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ('_switchyard_command_display', '_switchyard_user_can_prompt_for_sudo', 'ensure_staged_role_bundle_before_crossing', '_switchyard_exec_through_tenant_control', '_switchyard_exec_with_root', '_configured_role_account_caller', '_switchyard_command_is_unprivileged', '_switchyard_cross_account', '_require_switchyard_owner_hint_or_root', '_require_switchyard_project_owner_or_root', '_load_switchyard_project_config_for_command')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'ensure_staged_role_bundle_before_crossing': {'repair_tenant_control_helper': 1, 'staged_bundle_launch_problems': 2},
    '_switchyard_exec_through_tenant_control': {'TENANT_CONTROL_ROOT': 1, 'close_desktop_presentation': 1, 'complete_desktop_presentation': 1, 'current_user_name': 1, 'ensure_staged_role_bundle_before_crossing': 1},
    '_switchyard_exec_with_root': {'_switchyard_command_display': 1, '_switchyard_user_can_prompt_for_sudo': 1},
    '_configured_role_account_caller': {'current_user_name': 1},
    '_switchyard_command_is_unprivileged': {'SWITCHYARD_UNPRIVILEGED_COMMANDS': 1},
    '_switchyard_cross_account': {'_switchyard_exec_through_tenant_control': 1, '_switchyard_exec_with_root': 1, '_tenant_control_grant': 1, '_tenant_control_operation': 1, 'offer_host_wide_promotion_before_launch': 1},
    '_require_switchyard_owner_hint_or_root': {'_project_config_path_owner_user': 1, '_switchyard_command_is_unprivileged': 1, '_switchyard_cross_account': 1, 'current_user_name': 1},
    '_require_switchyard_project_owner_or_root': {'_configured_role_account_caller': 1, '_switchyard_command_is_unprivileged': 1, '_switchyard_cross_account': 1, 'current_user_name': 1},
    '_load_switchyard_project_config_for_command': {'_require_switchyard_owner_hint_or_root': 1, '_require_switchyard_project_owner_or_root': 1, '_switchyard_exec_with_root': 1, 'load_project_config': 1},
}
#: Measured on the baseline launcher: every launcher function outside the eleven that calls one of them, and by what name.
DISPATCH = {'switchyard_main': {'_load_switchyard_project_config_for_command': 16}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'presentation_commands': ['launcher._load_switchyard_project_config_for_command']}
#: The BASELINE's own behaviour for the cases below (`gold414.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'load: the owner, a privileged command': {'answer': 'CONFIG', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}], ['_require_switchyard_project_owner_or_root', ['CONFIG', ['upgrade', 'p414']], {}], ['current_user_name', [], {}]]},
    'load: root': {'answer': 'CONFIG', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}], ['_require_switchyard_project_owner_or_root', ['CONFIG', ['upgrade', 'p414']], {}], ['current_user_name', [], {}]]},
    'load: no path owner, no configured owner': {'answer': 'CONFIG', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}], ['_require_switchyard_project_owner_or_root', ['CONFIG', ['upgrade', 'p414']], {}]]},
    'load: a role account, its own unprivileged command': {'answer': 'CONFIG', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['status', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['status', 'p414']], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}], ['_require_switchyard_project_owner_or_root', ['CONFIG', ['status', 'p414']], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['status', 'p414']], {}], ['_configured_role_account_caller', ['CONFIG'], {}], ['current_user_name', [], {}]]},
    'load: a role account, unprivileged but unreadable config': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['status', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['status', 'p414']], {}], ['_switchyard_cross_account', ['p414', ['status', 'p414']], {}], ['_tenant_control_grant', ['p414'], {}], ['_switchyard_exec_with_root', [['status', 'p414']], {}], ['_switchyard_command_display', [['status', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'status', 'p414']]]},
    'load: a role account, a privileged command': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['upgrade', 'p414']], {}], ['_switchyard_cross_account', ['p414', ['upgrade', 'p414']], {}], ['_tenant_control_grant', ['p414'], {}], ['_switchyard_exec_with_root', [['upgrade', 'p414']], {}], ['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'upgrade', 'p414']]]},
    'load: an unconfigured account, unprivileged, readable': {'answer': {'raised': 'SystemExit', 'message': 'switchyard: command requires root: switchyard status\nswitchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator'}, 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['status', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}], ['_require_switchyard_project_owner_or_root', ['CONFIG', ['status', 'p414']], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['status', 'p414']], {}], ['_configured_role_account_caller', ['CONFIG'], {}], ['current_user_name', [], {}], ['_switchyard_cross_account', ['p414', ['status', 'p414']], {}], ['_tenant_control_grant', ['p414'], {}], ['_switchyard_exec_with_root', [['status', 'p414']], {}], ['_switchyard_command_display', [['status', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}]]},
    'load: the operator, a granted stop': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['stop', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['stop', 'p414']], {}], ['_switchyard_cross_account', ['p414', ['stop', 'p414']], {}], ['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['stop', 'p414'], 'p414'], {}], ['_switchyard_exec_through_tenant_control', ['p414', 'stop'], {'ensure_helper': 'HELPER', 'grant': {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'print_func': 'SAY', 'runner': 'RUNNER'}], ['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'stop'], {}], ['close_desktop_presentation', ['p414'], {'caller': 'syrd414-operator'}]]},
    'load: a stranger, no grant, no sudo': {'answer': {'raised': 'SystemExit', 'message': 'switchyard: command requires root: switchyard upgrade\nswitchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator'}, 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['upgrade', 'p414']], {}], ['_switchyard_cross_account', ['p414', ['upgrade', 'p414']], {}], ['_tenant_control_grant', ['p414'], {}], ['_switchyard_exec_with_root', [['upgrade', 'p414']], {}], ['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}]]},
    'load: a stranger, no grant, a prompting sudoer': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['_switchyard_command_is_unprivileged', [['upgrade', 'p414']], {}], ['_switchyard_cross_account', ['p414', ['upgrade', 'p414']], {}], ['_tenant_control_grant', ['p414'], {}], ['_switchyard_exec_with_root', [['upgrade', 'p414']], {}], ['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}], ['exec', 'syrd414-escalate', ['syrd414-escalate', 'switchyard', 'upgrade', 'p414']]]},
    'load: the configuration refused to a user': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}], ['_switchyard_exec_with_root', [['upgrade', 'p414']], {}], ['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'upgrade', 'p414']]]},
    'load: the configuration refused to root': {'answer': {'raised': 'PermissionError', 'message': "[Errno 13] Permission denied: '/nonexistent/syrd414/p414.json'"}, 'said': [], 'calls': [['_require_switchyard_owner_hint_or_root', ['ENTRY', ['upgrade', 'p414']], {}], ['_project_config_path_owner_user', ['PATH /nonexistent/syrd414/p414.json'], {}], ['current_user_name', [], {}], ['load_project_config', ['p414', 'PATH /nonexistent/syrd414/p414.json'], {}]]},
    'cross: no grant': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_switchyard_exec_with_root', [['upgrade', 'p414']], {}], ['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'upgrade', 'p414']]]},
    'cross: a grant, no bridge operation': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['upgrade', 'p414'], 'p414'], {}], ['_switchyard_exec_with_root', [['upgrade', 'p414']], {}], ['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'upgrade', 'p414']]]},
    'cross: a grant, stop': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['stop', 'p414'], 'p414'], {}], ['_switchyard_exec_through_tenant_control', ['p414', 'stop'], {'ensure_helper': 'HELPER', 'grant': {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'print_func': 'SAY', 'runner': 'RUNNER'}], ['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'stop'], {}], ['close_desktop_presentation', ['p414'], {'caller': 'syrd414-operator'}]]},
    'cross: a grant, start, interactive': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['start', 'p414'], 'p414'], {}], ['offer_host_wide_promotion_before_launch', ['p414'], {'input_func': 'ANOTHER CALLABLE', 'interactive': True, 'policy': '', 'print_func': 'SAY', 'promoter': 'ANOTHER CALLABLE', 'runner': 'RUNNER', 'sources': {'claude': 'local'}, 'which': 'ANOTHER CALLABLE'}], ['_switchyard_exec_through_tenant_control', ['p414', 'start'], {'ensure_helper': 'HELPER', 'grant': {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'print_func': 'SAY', 'runner': 'RUNNER'}], ['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}], ['complete_desktop_presentation', ['p414'], {'caller': 'syrd414-operator', 'runner': 'RUNNER'}]]},
    'cross: a grant, start, not a terminal': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['start', 'p414'], 'p414'], {}], ['offer_host_wide_promotion_before_launch', ['p414'], {'input_func': 'ANOTHER CALLABLE', 'interactive': False, 'policy': '', 'print_func': 'SAY', 'promoter': 'ANOTHER CALLABLE', 'runner': 'RUNNER', 'sources': {'claude': 'local'}, 'which': 'ANOTHER CALLABLE'}], ['_switchyard_exec_through_tenant_control', ['p414', 'start'], {'ensure_helper': 'HELPER', 'grant': {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'print_func': 'SAY', 'runner': 'RUNNER'}], ['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}], ['complete_desktop_presentation', ['p414'], {'caller': 'syrd414-operator', 'runner': 'RUNNER'}]]},
    'cross: a grant, stop, a bridge that returns': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['stop', 'p414'], 'p414'], {}], ['_switchyard_exec_through_tenant_control', ['p414', 'stop'], {'ensure_helper': 'HELPER', 'grant': {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'print_func': 'SAY', 'runner': 'RUNNER'}], ['_switchyard_exec_with_root', [['stop', 'p414']], {}], ['_switchyard_command_display', [['stop', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'stop', 'p414']]]},
    'cross: a grant, start, interactive given': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['_tenant_control_grant', ['p414'], {}], ['_tenant_control_operation', [['start', 'p414'], 'p414'], {}], ['offer_host_wide_promotion_before_launch', ['p414'], {'input_func': 'ANOTHER CALLABLE', 'interactive': False, 'policy': 'promote-local', 'print_func': 'SAY', 'promoter': 'ANOTHER CALLABLE', 'runner': 'RUNNER', 'sources': {'claude': 'local'}, 'which': 'ANOTHER CALLABLE'}], ['_switchyard_exec_through_tenant_control', ['p414', 'start'], {'ensure_helper': 'HELPER', 'grant': {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'print_func': 'SAY', 'runner': 'RUNNER'}], ['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}], ['complete_desktop_presentation', ['p414'], {'caller': 'syrd414-operator', 'runner': 'RUNNER'}]]},
    'bridge: a caller the grant does not name': {'answer': {'raised': 'SystemExit', 'message': 'switchyard: syrd414-intruder may not control p414; it is registered to syrd414-operator\nswitchyard: ask that user, or run this as the project owner or an operator'}, 'said': [], 'calls': [['current_user_name', [], {}]]},
    'bridge: stop, the bridge answers 0': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'stop'], {}], ['close_desktop_presentation', ['p414'], {'caller': 'syrd414-operator'}]]},
    'bridge: stop, the window will not close': {'answer': {'raised': 'SystemExit', 'message': '4'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'stop'], {}], ['close_desktop_presentation', ['p414'], {'caller': 'syrd414-operator'}]]},
    'bridge: recover-display': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': ["switchyard: p414's Director display was recovered"], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'recover-display'], {}], ['say']]},
    'bridge: start, completed': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}], ['complete_desktop_presentation', ['p414'], {'caller': 'syrd414-operator', 'runner': 'RUNNER'}]]},
    'bridge: start, completion fails': {'answer': {'raised': 'SystemExit', 'message': '6'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}], ['complete_desktop_presentation', ['p414'], {'caller': 'syrd414-operator', 'runner': 'RUNNER'}]]},
    'bridge: the bridge refuses': {'answer': {'raised': 'SystemExit', 'message': '3'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}]]},
    'bridge: the bridge answers nothing': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['syrd414-escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'start'], {}], ['complete_desktop_presentation', ['p414'], {'caller': 'syrd414-operator', 'runner': 'RUNNER'}]]},
    'bridge: the staged bundle is hostile': {'answer': {'raised': 'SystemExit', 'message': "switchyard: p414's staged tooling is not root's to replace: b.sh is writable by uid 1006"}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}]]},
    'bridge: a custom sudo, stop': {'answer': {'raised': 'SystemExit', 'message': '0'}, 'said': [], 'calls': [['current_user_name', [], {}], ['ensure_helper', 'p414', {'authorized_user': 'syrd414-operator', 'owner': 'syrd414-owner'}, 'RUNNER', 'SAY'], ['ensure_staged_role_bundle_before_crossing', ['p414'], {'print_func': 'SAY', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': None}], ['runner', ['/opt/syrd414/bin/escalate', '-n', '/nonexistent/syrd414/tenant-control/p414/switchyard-tenant-control', 'p414', 'stop'], {}], ['close_desktop_presentation', ['p414'], {'caller': 'syrd414-operator'}]]},
    'bundle: healthy': {'answer': '', 'said': [], 'calls': [['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': 'PATH /nonexistent/syrd414/staging'}]]},
    'bundle: hostile': {'answer': "p414's staged tooling is not root's to replace: b.sh is writable by uid 1006; c.sh is a symlink", 'said': [], 'calls': [['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': 'PATH /nonexistent/syrd414/staging'}]]},
    'bundle: absent, repaired': {'answer': '', 'said': ['switchyard: p414 is missing 2 of its staged role tooling; restaging the bundle from /nonexistent/syrd414/releases/current before continuing'], 'calls': [['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': 'PATH /nonexistent/syrd414/staging'}], ['say'], ['repair_tenant_control_helper', ['p414'], {'print_func': 'SAY', 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging'}]]},
    'bundle: absent, the repair fails': {'answer': 'the recorded privileged command failed', 'said': ['switchyard: p414 is missing 1 of its staged role tooling; restaging the bundle from /nonexistent/syrd414/releases/current before continuing'], 'calls': [['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': 'PATH /nonexistent/syrd414/staging'}], ['say'], ['repair_tenant_control_helper', ['p414'], {'print_func': 'SAY', 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging', 'runner': 'RUNNER'}]]},
    'bundle: absent, still absent after repair': {'answer': "p414's staged role tooling is still incomplete after restaging: a.sh; b.sh; c.sh; d.sh", 'said': ['switchyard: p414 is missing 5 of its staged role tooling; restaging the bundle from /nonexistent/syrd414/releases/current before continuing'], 'calls': [['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '', 'root': 'PATH /nonexistent/syrd414/staging'}], ['say'], ['repair_tenant_control_helper', ['p414'], {'print_func': 'SAY', 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging'}]]},
    'bundle: absent, the release named': {'answer': '', 'said': ['switchyard: p414 is missing 1 of its staged role tooling; restaging the bundle from /nonexistent/syrd414/releases/current before continuing'], 'calls': [['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '/nonexistent/syrd414/releases/abc', 'root': 'PATH /nonexistent/syrd414/staging'}], ['say'], ['repair_tenant_control_helper', ['p414'], {'print_func': 'SAY', 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging', 'runner': 'RUNNER'}], ['staged_bundle_launch_problems', ['p414'], {'expect_uid': 0, 'release_root': '/nonexistent/syrd414/releases/current', 'root': 'PATH /nonexistent/syrd414/staging'}]]},
    'root: sudo without a password': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['exec', 'syrd414-escalate', ['syrd414-escalate', '-n', 'switchyard', 'upgrade', 'p414']]]},
    'root: a sudoer at a terminal': {'answer': 'EXEC (does not return)', 'said': [], 'calls': [['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}], ['exec', 'syrd414-escalate', ['syrd414-escalate', 'switchyard', 'upgrade', 'p414']]]},
    'root: a terminal, no sudo group': {'answer': {'raised': 'SystemExit', 'message': 'switchyard: command requires root: switchyard upgrade\nswitchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator'}, 'said': [], 'calls': [['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}]]},
    'root: a sudoer, no terminal': {'answer': {'raised': 'SystemExit', 'message': 'switchyard: command requires root: switchyard upgrade\nswitchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator'}, 'said': [], 'calls': [['_switchyard_command_display', [['upgrade', 'p414']], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}]]},
    'root: no arguments': {'answer': {'raised': 'SystemExit', 'message': 'switchyard: command requires root: switchyard\nswitchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator'}, 'said': [], 'calls': [['_switchyard_command_display', [[]], {}], ['runner', ['syrd414-escalate', '-n', '-v'], {'stderr': -3, 'stdout': -3}], ['_switchyard_user_can_prompt_for_sudo', [], {}]]},
    'prompt: no ttys': {'answer': False, 'said': [], 'calls': []},
    'prompt: stdin only': {'answer': False, 'said': [], 'calls': []},
    'prompt: stderr only': {'answer': False, 'said': [], 'calls': []},
    'prompt: stdin only, a sudoer': {'answer': False, 'said': [], 'calls': []},
    'prompt: stderr only, a sudoer': {'answer': False, 'said': [], 'calls': []},
    'prompt: both, wheel': {'answer': True, 'said': [], 'calls': []},
    'prompt: both, users': {'answer': False, 'said': [], 'calls': []},
    'prompt: both, the account unknown': {'answer': False, 'said': [], 'calls': []},
    'prompt: both, one group unknown': {'answer': True, 'said': [], 'calls': []},
    'small: display, none': {'answer': 'switchyard', 'said': [], 'calls': []},
    'small: display, a verb': {'answer': 'switchyard stop', 'said': [], 'calls': []},
    'small: unprivileged, status': {'answer': True, 'said': [], 'calls': []},
    'small: unprivileged, upgrade': {'answer': False, 'said': [], 'calls': []},
    'small: unprivileged, none': {'answer': False, 'said': [], 'calls': []},
    'small: role account, the worker': {'answer': 'worker', 'said': [], 'calls': [['current_user_name', [], {}]]},
    'small: role account, the owner': {'answer': '', 'said': [], 'calls': [['current_user_name', [], {}]]},
    'small: role account, a stranger': {'answer': '', 'said': [], 'calls': [['current_user_name', [], {}]]},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold414.py` (which ran them on the baseline) ------------------------------------
# A case pins who is asking (caller, euid, ttys, groups), what the launcher's seams answer, and what the injected
# effects answer (the non-interactive sudo check, the bridge, the desktop halves, the helper and bundle repairs).
UNPRIV = "status"          # a verb in SWITCHYARD_UNPRIVILEGED_COMMANDS (checked by the test)
PRIV = "upgrade"           # one that is not
BRIDGE_UNIX = {"stop": "stop", "start": "start", "recover-display": "recover-display"}
LOADER = {
    "the owner, a privileged command": {"call": "load", "argv": [PRIV, "p414"], "caller": "syrd414-owner", "path_owner": "syrd414-owner"},
    "root": {"call": "load", "argv": [PRIV, "p414"], "caller": "root", "euid": 0},
    "no path owner, no configured owner": {"call": "load", "argv": [PRIV, "p414"], "caller": "syrd414-other", "path_owner": "", "config_owner": ""},
    "a role account, its own unprivileged command": {"call": "load", "argv": [UNPRIV, "p414"], "caller": "syrd414-worker", "readable": True},
    "a role account, unprivileged but unreadable config": {"call": "load", "argv": [UNPRIV, "p414"], "caller": "syrd414-worker", "readable": False, "sudo_ok": True},
    "a role account, a privileged command": {"call": "load", "argv": [PRIV, "p414"], "caller": "syrd414-worker", "readable": True, "sudo_ok": True},
    "an unconfigured account, unprivileged, readable": {"call": "load", "argv": [UNPRIV, "p414"], "caller": "syrd414-stranger", "readable": True, "path_owner": "", "sudo_ok": False},
    "the operator, a granted stop": {"call": "load", "argv": ["stop", "p414"], "caller": "syrd414-operator", "grant": True, "operation": "stop", "bridge": 0},
    "a stranger, no grant, no sudo": {"call": "load", "argv": [PRIV, "p414"], "caller": "syrd414-stranger"},
    "a stranger, no grant, a prompting sudoer": {"call": "load", "argv": [PRIV, "p414"], "caller": "syrd414-stranger", "tty": True, "groups": ["wheel"]},
    "the configuration refused to a user": {"call": "load", "argv": [PRIV, "p414"], "caller": "syrd414-owner", "path_owner": "syrd414-owner", "load_denied": True, "sudo_ok": True},
    "the configuration refused to root": {"call": "load", "argv": [PRIV, "p414"], "caller": "root", "euid": 0, "load_denied": True},
}
CROSS = {
    "no grant": {"call": "cross", "argv": [PRIV, "p414"], "caller": "syrd414-operator", "sudo_ok": True},
    "a grant, no bridge operation": {"call": "cross", "argv": [PRIV, "p414"], "caller": "syrd414-operator", "grant": True, "operation": "", "sudo_ok": True},
    "a grant, stop": {"call": "cross", "argv": ["stop", "p414"], "caller": "syrd414-operator", "grant": True, "operation": "stop", "bridge": 0},
    "a grant, start, interactive": {"call": "cross", "argv": ["start", "p414"], "caller": "syrd414-operator", "grant": True, "operation": "start", "bridge": 0, "tty": True},
    "a grant, start, not a terminal": {"call": "cross", "argv": ["start", "p414"], "caller": "syrd414-operator", "grant": True, "operation": "start", "bridge": 0},
    # A bridge that returns (as the suites that patch it make it do): the cross falls through to root.
    "a grant, stop, a bridge that returns": {"call": "cross", "argv": ["stop", "p414"], "caller": "syrd414-operator", "grant": True, "operation": "stop",
                                             "bridge_returns": True, "sudo_ok": True},
    "a grant, start, interactive given": {"call": "cross", "argv": ["start", "p414"], "caller": "syrd414-operator", "grant": True, "operation": "start", "bridge": 0, "interactive": False, "policy": "promote-local"},
}
BRIDGE = {
    "a caller the grant does not name": {"call": "bridge", "operation": "stop", "caller": "syrd414-intruder"},
    "stop, the bridge answers 0": {"call": "bridge", "operation": "stop", "caller": "syrd414-operator", "bridge": 0, "close": 0},
    "stop, the window will not close": {"call": "bridge", "operation": "stop", "caller": "syrd414-operator", "bridge": 0, "close": 4},
    "recover-display": {"call": "bridge", "operation": "recover-display", "caller": "syrd414-operator", "bridge": 0},
    "start, completed": {"call": "bridge", "operation": "start", "caller": "syrd414-operator", "bridge": 0, "complete": 0},
    "start, completion fails": {"call": "bridge", "operation": "start", "caller": "syrd414-operator", "bridge": 0, "complete": 6},
    "the bridge refuses": {"call": "bridge", "operation": "start", "caller": "syrd414-operator", "bridge": 3},
    "the bridge answers nothing": {"call": "bridge", "operation": "start", "caller": "syrd414-operator", "bridge": None},
    "the staged bundle is hostile": {"call": "bridge", "operation": "start", "caller": "syrd414-operator", "bundle": [([], ["b.sh is writable by uid 1006"])]},
    "a custom sudo, stop": {"call": "bridge", "operation": "stop", "caller": "syrd414-operator", "bridge": 0, "close": 0, "sudo_bin": "/opt/syrd414/bin/escalate"},
}
BUNDLE = {
    "healthy": {"call": "bundle", "bundle": [([], [])]},
    "hostile": {"call": "bundle", "bundle": [(["a.sh"], ["b.sh is writable by uid 1006", "c.sh is a symlink"])]},
    "absent, repaired": {"call": "bundle", "bundle": [(["a.sh", "b.sh"], []), ([], [])]},
    "absent, the repair fails": {"call": "bundle", "bundle": [(["a.sh"], [])], "repair": "the recorded privileged command failed"},
    "absent, still absent after repair": {"call": "bundle", "bundle": [(["a.sh", "b.sh", "c.sh", "d.sh", "e.sh"], []), (["a.sh", "b.sh", "c.sh", "d.sh", "e.sh"], [])]},
    "absent, the release named": {"call": "bundle", "bundle": [(["a.sh"], []), ([], [])], "release_root": "/nonexistent/syrd414/releases/abc"},
}
ROOT_CASES = {
    "sudo without a password": {"call": "root", "argv": [PRIV, "p414"], "sudo_ok": True},
    "a sudoer at a terminal": {"call": "root", "argv": [PRIV, "p414"], "tty": True, "groups": ["admin"]},
    "a terminal, no sudo group": {"call": "root", "argv": [PRIV, "p414"], "tty": True, "groups": ["users"]},
    "a sudoer, no terminal": {"call": "root", "argv": [PRIV, "p414"], "groups": ["sudo"]},
    "no arguments": {"call": "root", "argv": []},
}
PROMPT = {
    "no ttys": {"call": "prompt"}, "stdin only": {"call": "prompt", "stdin_tty": True}, "stderr only": {"call": "prompt", "stderr_tty": True},
    "stdin only, a sudoer": {"call": "prompt", "stdin_tty": True, "groups": ["wheel"]}, "stderr only, a sudoer": {"call": "prompt", "stderr_tty": True, "groups": ["wheel"]},
    "both, wheel": {"call": "prompt", "tty": True, "groups": ["wheel"]}, "both, users": {"call": "prompt", "tty": True, "groups": ["users"]},
    "both, the account unknown": {"call": "prompt", "tty": True, "no_account": True},
    "both, one group unknown": {"call": "prompt", "tty": True, "groups": ["sudo"], "unknown_gid": True},
}
SMALL = {
    "display, none": {"call": "display", "argv": []}, "display, a verb": {"call": "display", "argv": ["stop", "p414"]},
    "unprivileged, status": {"call": "unpriv", "argv": ["STATUS", "p414"]}, "unprivileged, upgrade": {"call": "unpriv", "argv": [PRIV]}, "unprivileged, none": {"call": "unpriv", "argv": []},
    "role account, the worker": {"call": "role", "caller": "syrd414-worker"}, "role account, the owner": {"call": "role", "caller": "syrd414-owner"},
    "role account, a stranger": {"call": "role", "caller": "syrd414-stranger"},
}
CASES = {f"{group}: {label}": spec for group, table in (("load", LOADER), ("cross", CROSS), ("bridge", BRIDGE), ("bundle", BUNDLE), ("root", ROOT_CASES),
                                                       ("prompt", PROMPT), ("small", SMALL)) for label, spec in table.items()}
SEAM_NAMES = ["current_user_name", "load_project_config", "_project_config_path_owner_user", "staged_bundle_launch_problems", "_tenant_control_grant",
              "_tenant_control_operation", "repair_tenant_control_helper", "close_desktop_presentation", "complete_desktop_presentation",
              "offer_host_wide_promotion_before_launch", "TENANT_CONTROL_ROOT"]
_REAL_EXECVP = os.execvp


class Stopped(Exception):
    """What a recorded exec raises: an exec never returns."""


class Tty:
    def __init__(self, answer: bool) -> None:
        self.answer = answer

    def isatty(self) -> bool:
        return self.answer


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s function; every launcher seam stands in on `t`, every sibling is wrapped there with recorded effects."""
    calls: list = []
    said: list = []
    config = SimpleNamespace(project="p414", run_as_user=spec.get("config_owner", "syrd414-owner"),
                             roles=[SimpleNamespace(role="director", run_as_user="syrd414-owner"), SimpleNamespace(role="worker", run_as_user="syrd414-worker"),
                                    SimpleNamespace(role="audit", run_as_user="")])
    entry = SimpleNamespace(slug="p414", name="P414", config_path=Path("/nonexistent/syrd414/p414.json"))
    grant = {"authorized_user": "syrd414-operator", "owner": "syrd414-owner"} if spec.get("grant", spec["call"] == "bridge") else {}
    bundle = list(spec.get("bundle", [([], [])]))

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        for obj, name in ((config, "CONFIG"), (entry, "ENTRY"), (say, "SAY"), (runner, "RUNNER"), (exec_func, "EXEC"), (helper, "HELPER")):
            if value is obj:
                return name
        if isinstance(value, Path):
            return f"PATH {value}"
        if callable(value):
            return "ANOTHER CALLABLE"
        return value

    def say(line):
        said.append(line)
        calls.append(["say"])

    def runner(argv, **kwargs):
        calls.append(["runner", norm(list(argv)), norm(dict(sorted(kwargs.items())))])
        if list(argv[1:]) == ["-n", "-v"]:
            return SimpleNamespace(returncode=0 if spec.get("sudo_ok") else 1)
        return SimpleNamespace(returncode=spec.get("bridge", 0)) if spec.get("bridge", 0) is not None else SimpleNamespace(returncode=None)

    def exec_func(program, argv):
        calls.append(["exec", norm(program), norm(list(argv))])
        raise Stopped()

    def helper(project, *, grant, runner, print_func):
        calls.append(["ensure_helper", project, norm(grant), norm(runner), norm(print_func)])

    def record(name, answer):
        def call(*args, **kwargs):
            reached.add(name)
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items())))])
            return answer(*args, **kwargs)
        return call

    def load(slug, path):
        if spec.get("load_denied"):
            raise PermissionError(13, "Permission denied", str(path))
        return config

    real = {n: getattr(t, n) for n in ("_switchyard_exec_with_root", "_switchyard_cross_account", "_switchyard_exec_through_tenant_control",
                                         "ensure_staged_role_bundle_before_crossing", "_switchyard_command_display", "_switchyard_user_can_prompt_for_sudo",
                                         "_configured_role_account_caller", "_switchyard_command_is_unprivileged", "_require_switchyard_owner_hint_or_root",
                                         "_require_switchyard_project_owner_or_root", "_load_switchyard_project_config_for_command")}
    # The function under test, taken before any stand-in goes in (the launcher's attributes are wrapped below).
    under = {n: getattr(holder, n) for n in real}

    def sibling(name, inject=None):
        """A sibling as the others call it through the launcher: recorded as called, then run with every effect a recorder."""
        def call(*args, **kwargs):
            reached.add(name)
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items())))])
            return real[name](*args, **{**(inject or {}), **kwargs})
        return call

    effects = {"runner": runner, "print_func": say}
    on_launcher = {
        "current_user_name": record("current_user_name", lambda: spec.get("caller", "syrd414-owner")),
        "load_project_config": record("load_project_config", load),
        "_project_config_path_owner_user": record("_project_config_path_owner_user", lambda path: spec.get("path_owner", "syrd414-owner")),
        "staged_bundle_launch_problems": record("staged_bundle_launch_problems", lambda project, **k: (*bundle.pop(0), "/nonexistent/syrd414/releases/current") if bundle else ([], [], "?")),
        "_tenant_control_grant": record("_tenant_control_grant", lambda project, **k: dict(grant)),
        "_tenant_control_operation": record("_tenant_control_operation", lambda argv, project: spec.get("operation", "")),
        "repair_tenant_control_helper": record("repair_tenant_control_helper", lambda project, **k: spec.get("repair", "")),
        "close_desktop_presentation": record("close_desktop_presentation", lambda project, **k: spec.get("close", 0)),
        "complete_desktop_presentation": record("complete_desktop_presentation", lambda project, **k: spec.get("complete", 0)),
        "offer_host_wide_promotion_before_launch": record("offer_host_wide_promotion_before_launch", lambda project, **k: None),
        "TENANT_CONTROL_ROOT": Path("/nonexistent/syrd414/tenant-control"),
        "_switchyard_exec_with_root": sibling("_switchyard_exec_with_root", {"runner": runner, "exec_func": exec_func}),
        "_switchyard_cross_account": sibling("_switchyard_cross_account", {**effects, "which": lambda *a, **k: None, "input_func": lambda prompt: "n",
                                                                          "promoter": lambda *a, **k: None, "ensure_helper": helper}),
        "_switchyard_exec_through_tenant_control": (record("_switchyard_exec_through_tenant_control", lambda *a, **k: None) if spec.get("bridge_returns")
                                                    else sibling("_switchyard_exec_through_tenant_control", {**effects, "exec_func": exec_func, "ensure_helper": helper})),
        "ensure_staged_role_bundle_before_crossing": sibling("ensure_staged_role_bundle_before_crossing", effects),
        "_switchyard_command_display": sibling("_switchyard_command_display"),
        "_switchyard_user_can_prompt_for_sudo": sibling("_switchyard_user_can_prompt_for_sudo"),
        "_configured_role_account_caller": sibling("_configured_role_account_caller"),
        "_switchyard_command_is_unprivileged": sibling("_switchyard_command_is_unprivileged"),
        "_require_switchyard_owner_hint_or_root": sibling("_require_switchyard_owner_hint_or_root"),
        "_require_switchyard_project_owner_or_root": sibling("_require_switchyard_project_owner_or_root"),
    }
    groups = spec.get("groups", [])
    user = SimpleNamespace(pw_name="syrd414-caller", pw_gid=4140)

    def getpwuid(uid):
        if spec.get("no_account"):
            raise KeyError(uid)
        return user

    def getgrgid(gid):
        index = gid - 4140
        if spec.get("unknown_gid") and index == 0:
            raise KeyError(gid)
        names = ["syrd414-primary", *groups]
        return SimpleNamespace(gr_name=names[index] if 0 <= index < len(names) else f"g{gid}")

    pinned = [(os, {"geteuid": lambda: spec.get("euid", 4141), "access": lambda path, mode: spec.get("readable", False),
                    "getgroups": lambda: [4140 + i for i in range(1, len(groups) + 1)]}),
              (sys, {"stdin": Tty(spec.get("stdin_tty", spec.get("tty", False))), "stderr": Tty(spec.get("stderr_tty", spec.get("tty", False))), "argv": ["switchyard"]}),
              (pwd, {"getpwuid": getpwuid}), (grp, {"getgrgid": getgrgid})]
    saved = [(o, {n: getattr(o, n) for n in d}) for o, d in [(t, on_launcher), *pinned]]
    saved_env = os.environ.get("SWITCHYARD_SUDO_BIN")
    for o, d in [(t, on_launcher), *pinned]:
        for n, f in d.items():
            setattr(o, n, f)
    os.environ["SWITCHYARD_SUDO_BIN"] = spec.get("sudo_bin", "syrd414-escalate")
    call = spec["call"]
    try:
        if call == "load":
            got = under["_load_switchyard_project_config_for_command"](entry, spec["argv"])
        elif call == "cross":
            got = under["_switchyard_cross_account"]("p414", spec["argv"], agent_cli_policy=spec.get("policy", ""), agent_cli_sources={"claude": "local"},
                                                      interactive=spec.get("interactive"), which=lambda *a, **k: None, input_func=lambda prompt: "n",
                                                      print_func=say, promoter=lambda *a, **k: None, runner=runner, ensure_helper=helper)
        elif call == "bridge":
            got = under["_switchyard_exec_through_tenant_control"]("p414", spec["operation"], grant=grant, runner=runner, exec_func=exec_func,
                                                                    print_func=say, ensure_helper=helper)
        elif call == "bundle":
            got = under["ensure_staged_role_bundle_before_crossing"]("p414", release_root=spec.get("release_root", ""), root=Path("/nonexistent/syrd414/staging"),
                                                                      expect_uid=0, runner=runner, print_func=say)
        elif call == "root":
            got = under["_switchyard_exec_with_root"](spec["argv"], runner=runner, exec_func=exec_func)
        elif call == "prompt":
            got = under["_switchyard_user_can_prompt_for_sudo"]()
        elif call == "display":
            got = under["_switchyard_command_display"](spec["argv"])
        elif call == "unpriv":
            got = under["_switchyard_command_is_unprivileged"](spec["argv"])
        else:
            got = under["_configured_role_account_caller"](config)
        answer = norm(got)
    except AssertionError:
        raise
    except Stopped:
        answer = "EXEC (does not return)"
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        answer = {"raised": type(exc).__name__, "message": str(exc)}
    finally:
        for o, d in saved:
            for n, f in d.items():
                setattr(o, n, f)
        if saved_env is None:
            os.environ.pop("SWITCHYARD_SUDO_BIN", None)
        else:
            os.environ["SWITCHYARD_SUDO_BIN"] = saved_env
    return {"answer": answer, "said": said, "calls": calls}
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
                      patched(os, kill=refuse("os.kill"), **{name: refuse(f"os.{name}") for name in EXECS}),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(grp, getgrgid=refuse("grp.getgrgid"), getgrnam=refuse("grp.getgrnam")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(spec: dict) -> dict:
    with contained():
        return json.loads(json.dumps(run_case(t, m, spec, REACHED)))


def test_the_guard_itself_refuses_a_spawn_an_exec_a_lookup_and_a_connection() -> None:
    attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0),
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


def test_the_module_loads_only_its_three_leaf_modules_at_import() -> None:
    result = python("import sys, scripts.command_crossing as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.agent_cli_discovery', 'scripts.staged_role_tooling', 'scripts.tenant_control_helper']",
          f"it imports on its own, loading only the leaves its defaults come from and never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.command_crossing", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.command_crossing")):
        result = python("import importlib, inspect, os, subprocess, builtins; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.command_crossing as m; "
                        "d = lambda f, n: inspect.signature(f).parameters[n].default; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "d(m.ensure_staged_role_bundle_before_crossing, 'expect_uid') is t.STAGED_TOOLING_OWNER_UID "
                        "and d(m._switchyard_exec_through_tenant_control, 'ensure_helper') is t.ensure_tenant_control_helper "
                        "and d(m._switchyard_cross_account, 'which') is t.caller_aware_which and d(m._switchyard_cross_account, 'input_func') is builtins.input "
                        "and d(m._switchyard_exec_with_root, 'exec_func') is os.execvp and d(m._switchyard_exec_through_tenant_control, 'exec_func') is os.execvp "
                        "and all(d(f, 'runner') is subprocess.run for f in (m.ensure_staged_role_bundle_before_crossing, m._switchyard_exec_through_tenant_control, "
                        "m._switchyard_exec_with_root, m._switchyard_cross_account)) "
                        "and all(d(f, 'print_func') is print for f in (m.ensure_staged_role_bundle_before_crossing, m._switchyard_exec_through_tenant_control, m._switchyard_cross_account)), "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'ProjectConfig') and not hasattr(m, 'SwitchyardProjectEntry') and not hasattr(m, 'AgentCliAvailability'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.sys is sys and m.pwd is pwd and m.grp is grp and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "command_crossing.py").read_text(encoding="utf-8"))
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
        check(imports == (["from scripts import team_launcher as launcher"] if expected else []) and (not imports or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and nothing otherwise: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import grp", "import os", "import pwd", "import subprocess", "import sys", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence", "from scripts.agent_cli_discovery import caller_aware_which",
                  "from scripts.staged_role_tooling import STAGED_TOOLING_OWNER_UID", "from scripts.tenant_control_helper import ensure_tenant_control_helper"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.agent_cli_discovery import AgentCliAvailability\n    from scripts.team_launcher import ProjectConfig, SwitchyardProjectEntry"],
          f"the standard library, the three leaf defaults, and the annotation types under TYPE_CHECKING: {top} {tc}")
    check([n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))] == list(MOVED), "the eleven in the launcher's order, and nothing else")


def test_the_launcher_reexports_the_eleven_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.command_crossing"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the eleven, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"switchyard_help_text", "switchyard_invocation_requires_root", "STAGED_TOOLING_ABSENT_MARKER", "STAGED_TOOLING_HOSTILE_MARKERS",
                                        "tenant_pinned_release_root", "director_readable_pinned_release", "staged_bundle_launch_problems",
                                        "_project_config_path_owner_user", "report_installed_release_version", "switchyard_main"} <= defined | exported,
          "the launcher defines none of them, and keeps every definition between them and its dispatcher, its own or re-exported")
    calls: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, ast.FunctionDef):
            for x in ast.walk(fn):
                if isinstance(x, ast.Call) and ast.unparse(x.func).split(".")[-1] in MOVED:
                    calls.setdefault(fn.name, {}).setdefault(ast.unparse(x.func), 0)
                    calls[fn.name][ast.unparse(x.func)] += 1
    check(calls == DISPATCH, f"the dispatcher loads through the launcher's own name, as often as before: {calls}")
    for module, uses in READERS.items():
        source = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        got = [ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED)]
        check(got == uses, f"{module} still reads them through the launcher: {got}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_crossing_decision_is_the_baselines() -> None:
    check(UNPRIV in t.SWITCHYARD_UNPRIVILEGED_COMMANDS and PRIV not in t.SWITCHYARD_UNPRIVILEGED_COMMANDS, "the two verbs the cases use still classify as they did")
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        got = run(spec)
        check(got == GOLDEN[label], f"{label}: the baseline's answer, every line and every call in order: {got}")


def test_the_boundaries_hold_in_the_measured_record() -> None:
    refused = GOLDEN["bridge: a caller the grant does not name"]["calls"]
    check([c[0] for c in refused] == ["current_user_name"], f"an operator the grant does not name is refused before any repair or prompt: {refused}")
    hostile = [c[0] for c in GOLDEN["bridge: the staged bundle is hostile"]["calls"]]
    check("runner" not in hostile and "repair_tenant_control_helper" not in hostile, f"hostile staged tooling is refused, never repaired or crossed: {hostile}")
    root = [c[0] for c in GOLDEN["load: the configuration refused to root"]["calls"]]
    check("_switchyard_exec_with_root" not in root and GOLDEN["load: the configuration refused to root"]["answer"]["raised"] == "PermissionError",
          f"root is never re-executed through sudo on a refused read: {root}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads} - {"TENANT_CONTROL_ROOT", "SWITCHYARD_UNPRIVILEGED_COMMANDS"}
    check(expected <= REACHED, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_its_three_leaf_modules_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_eleven_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"command_crossing_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
