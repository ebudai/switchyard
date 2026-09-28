#!/usr/bin/env python3
"""SYRD-429: the project launch command, against the launcher it came out of.

`launch_project` -- starting, attaching or reloading a project's panes through
the existing launch phases -- moved unchanged into `scripts/project_launch.py`;
the launcher re-exports it. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads only the two modules its defaults come from, never the launcher;
  the definition-time defaults -- the session-record timeout and poll, the
  layout mode, `subprocess.run` and `print` -- are the objects the launcher
  bound, and a rebinding on the launcher does not reach them, as before.
- **Seams (rule 24):** every name it reads -- the five phases, the worker
  record, the config loader and the board-authority, onboarding, layout-upgrade,
  desktop and pane-launcher checks -- is read through the launcher as often as
  before, so a patch there reaches it: every case below stands them in there,
  recorded, and rebinds the worker record there to see the effect.
- **Callers:** `main` and `switchyard_main` call the launcher's name, and
  `new_project_phases.py` and `resume_provision_command.py` read it there.
- **The behaviour is the baseline's:** the mode normalization and refusal, the
  authority refusal, the reload migration and config reload, dry runs, the
  attach that leaves the desktop alone, the layout upgrade, every stop point,
  the refusals the checks raise, every option passed through, the defaults, and
  start/reload/attach through `main` and `switchyard_main`. `GOLDEN` below was
  produced by running the BASELINE launcher's own function over the very cases
  embedded here (`gold429.py`), not typed; it is byte-identical whether
  generated under `env -i` or in a normal role pane.

Nothing is launched: no pane, window, desktop, service, provider, board or
project is touched. Spawns, every exec, signals, account and group lookups and
socket connections are refused for each case.
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
from scripts import project_launch as m  # noqa: E402

CHECKS = 0
MOVED = ('launch_project',)
#: Measured on the baseline launcher: the moved body's call-time reads of launcher globals.
SEAMS = {
    'launch_project': {'WorkerStartup': 1, '_launch_runners_and_paths': 1, '_prepare_launch': 1, '_report_launch': 1, '_start_workers_and_present': 1, '_verify_pane_launcher_path': 1, '_write_layout_and_plan': 1, 'load_project_config': 2, 'migrate_declarative_director_onboarding': 1, 'prepare_project_desktop': 1, 'process_authority_board_compatibility': 1, 'upgrade_generated_project_layout': 1},
}
#: Measured on the baseline launcher: every launcher definition that names it, and how often.
DISPATCH = {'switchyard_main': {'launch_project': 2}, 'main': {'launch_project': 1}}
#: Measured on the baseline: every production module that reads it, and how.
READERS = {'scripts/new_project_phases.py': ['launcher.launch_project'], 'scripts/resume_provision_command.py': ['launcher.launch_project']}
#: The BASELINE's own behaviour for the cases below (`gold429.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'launch: start: every step, in order': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: attach-or-start, spelled out': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: attach: the desktop is not prepared': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE given'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE given'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: reload: the onboarding migrated, the config reloaded': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['migrate_declarative_director_onboarding', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'print_func': 'PRINT'}], ['load_project_config', ['p429', 'PATH TMP/p429.json'], {}], ['_launch_runners_and_paths', ['NAMESPACE loaded 1'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE loaded 1'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE loaded 1'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'reload', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'reload', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: reload: nothing to migrate': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['migrate_declarative_director_onboarding', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'print_func': 'PRINT'}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'reload', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'reload', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: a reload dry run': {'result': 0, 'calls': [['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE given'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'reload', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'reload', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: a start dry run': {'result': 0, 'calls': [['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE given'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: a mode nobody offers': {'result': {'raised': 'SystemExit', 'message': 'unknown launch mode: restart'}, 'calls': [], 'printed': []},
    'launch: a stop is not a launch mode': {'result': {'raised': 'SystemExit', 'message': 'unknown launch mode: stop'}, 'calls': [], 'printed': []},
    'launch: the running board lacks process authority': {'result': 1, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}]], 'printed': ['team-launcher: refusing to launch p429 before changing local state: its running board does not provide project-account process authority (syrd429: an old board)']},
    'launch: the running board lacks it, on a reload': {'result': 1, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}]], 'printed': ['team-launcher: refusing to launch p429 before changing local state: its running board does not provide project-account process authority (syrd429: an old board)']},
    'launch: roles not isolated: no authority check': {'result': 0, 'calls': [['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: the generated layout upgraded': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['load_project_config', ['p429', 'PATH TMP/p429.json'], {}], ['prepare_project_desktop', ['NAMESPACE loaded 1'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': ['syrd429: the layout was upgraded']},
    'launch: the layout upgraded on an attach': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['load_project_config', ['p429', 'PATH TMP/p429.json'], {}], ['_verify_pane_launcher_path', ['NAMESPACE loaded 1'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE loaded 1'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': ['syrd429: the layout was upgraded']},
    'launch: the preparation stops the launch': {'result': 3, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}]], 'printed': []},
    'launch: the preparation stops a dry run': {'result': 0, 'calls': [['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE given'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}]], 'printed': []},
    'launch: the layout stops the launch': {'result': 4, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['migrate_declarative_director_onboarding', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'print_func': 'PRINT'}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'reload', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}]], 'printed': []},
    'launch: the layout stops with zero': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}]], 'printed': []},
    'launch: the workers stop the launch': {'result': 5, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}]], 'printed': []},
    'launch: the workers answer zero': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}]], 'printed': []},
    'launch: the report fails': {'result': 7, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: the pane launcher is refused': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: syrd429 pane launcher missing'}, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}]], 'printed': []},
    'launch: the desktop is refused': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: syrd429 desktop refused'}, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}]], 'printed': []},
    'launch: every option given': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['migrate_declarative_director_onboarding', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'print_func': 'PRINT'}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': True, 'config_path': 'PATH TMP/p429.json', 'layout_output': 'PATH TMP/layout.kdl', 'pane_state_dir': 'PATH TMP/pane-state', 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'reload', 'no_launcher_self_deploy': True, 'owner_home': 'PATH TMP/home/p429', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': True, 'layout_environ': {'SYRD': '429'}, 'layout_mode': 'separate', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': 'PATH TMP/pane-state', 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': True, 'konsole_process_launcher': 'KONSOLE', 'layout_environ': {'SYRD': '429'}, 'layout_mode': 'separate', 'layout_output': 'PATH TMP/layout.kdl', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'reload', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.25, 'session_record_timeout': 4.5, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: every option given, a dry run': {'result': 0, 'calls': [['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': True, 'config_path': 'PATH TMP/p429.json', 'layout_output': 'PATH TMP/layout.kdl', 'pane_state_dir': 'PATH TMP/pane-state', 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE given'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach', 'no_launcher_self_deploy': True, 'owner_home': 'PATH TMP/home/p429', 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'failed_roles': ['app'], 'force_reload': True, 'layout_environ': {'SYRD': '429'}, 'layout_mode': 'separate', 'mode': 'attach', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'pane_state_dir': 'PATH TMP/pane-state', 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': True, 'konsole_process_launcher': 'KONSOLE', 'layout_environ': {'SYRD': '429'}, 'layout_mode': 'separate', 'layout_output': 'PATH TMP/layout.kdl', 'mode': 'attach', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.25, 'session_record_timeout': 4.5, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: the defaults': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'launch: the worker record rebound on the launcher': {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'RUNNER'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'RUNNER'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'runner': 'RUNNER', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'RUNNER', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'PRINT', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'RUNNER', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'PRINT', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    "launch: the defaults' sources rebound on the launcher": {'result': 0, 'calls': [['process_authority_board_compatibility', ['NAMESPACE given'], {}], ['_launch_runners_and_paths', ['NAMESPACE given'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH TMP/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE given'], {'config_path': 'PATH TMP/p429.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE given'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': False, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'team-launcher start, through main': {'result': 0, 'calls': [['_resolve_launcher_project_config', ['p429'], {'explicit_config': 'PATH TMP/p429.json'}], ['load_project_config', ['p429', 'PATH TMP/p429.json'], {}], ['launch_project', ['NAMESPACE loaded 1'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'force_reload': False, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'start', 'no_launcher_self_deploy': False, 'pane_state_dir': None, 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE loaded 1'], {}], ['_launch_runners_and_paths', ['NAMESPACE loaded 1'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE loaded 1'], {'config_path': 'PATH TMP/p429.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE loaded 1'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'team-launcher reload, through main, every option': {'result': 0, 'calls': [['_resolve_launcher_project_config', ['p429'], {'explicit_config': 'PATH TMP/p429.json'}], ['load_project_config', ['p429', 'PATH TMP/p429.json'], {}], ['launch_project', ['NAMESPACE loaded 1'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'force_reload': True, 'layout_mode': 'separate', 'layout_output': 'PATH TMP/layout.kdl', 'mode': 'reload', 'no_launcher_self_deploy': True, 'pane_state_dir': 'PATH TMP/pane-state', 'report_session_records': True, 'script_path': 'PATH TMP/team-launcher'}], ['_launch_runners_and_paths', ['NAMESPACE loaded 1'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/p429.json', 'layout_output': 'PATH TMP/layout.kdl', 'pane_state_dir': 'PATH TMP/pane-state', 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH TMP/team-launcher'}], ['_prepare_launch', ['NAMESPACE loaded 1'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'reload', 'no_launcher_self_deploy': True, 'owner_home': None, 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'dry_run': True, 'failed_roles': ['app'], 'force_reload': True, 'layout_environ': None, 'layout_mode': 'separate', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'pane_state_dir': 'PATH TMP/pane-state', 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': True, 'config_path': 'PATH TMP/p429.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': True, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'separate', 'layout_output': 'PATH TMP/layout.kdl', 'mode': 'reload', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/setup-pane', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/p429.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'reload', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'team-launcher attach, through main, refused': {'result': 1, 'calls': [['_resolve_launcher_project_config', ['p429'], {'explicit_config': 'PATH TMP/p429.json'}], ['load_project_config', ['p429', 'PATH TMP/p429.json'], {}], ['launch_project', ['NAMESPACE loaded 1'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/p429.json', 'dry_run': False, 'force_reload': False, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach', 'no_launcher_self_deploy': False, 'pane_state_dir': None, 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE loaded 1'], {}]], 'printed': [['stdout', 'team-launcher: refusing to launch p429 before changing local state: its running board does not provide project-account process authority (syrd429: an old board)']]},
    'switchyard start, through switchyard_main': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['start', 'p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}], ['prepare_project_desktop', ['NAMESPACE switchyard'], {}], ['launch_project', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'mode': 'start', 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE desktop-prepared'], {}], ['_launch_runners_and_paths', ['NAMESPACE desktop-prepared'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/registered.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE desktop-prepared'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'switchyard start, the launch fails': {'result': 6, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['start', 'p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}], ['prepare_project_desktop', ['NAMESPACE switchyard'], {}], ['launch_project', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'mode': 'start', 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE desktop-prepared'], {}], ['_launch_runners_and_paths', ['NAMESPACE desktop-prepared'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/registered.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE desktop-prepared'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'switchyard start, a resume problem': {'result': 1, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['start', 'p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}]], 'printed': [['stdout', 'switchyard: syrd429: a unit would not start']]},
    'switchyard <project>, through switchyard_main': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}], ['prepare_project_desktop', ['NAMESPACE switchyard'], {}], ['run_switchyard_launch_first_run_auth', ['NAMESPACE desktop-prepared'], {}], ['stop_before_launch_for_missing_owner_clis', ['NAMESPACE auth report'], {}], ['stop_before_launch_for_unauthenticated_providers', ['NAMESPACE auth report'], {}], ['stop_before_launch_for_unknown_models', ['NAMESPACE auth report'], {'project': 'p429'}], ['launch_project', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'mode': 'start', 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE desktop-prepared'], {}], ['_launch_runners_and_paths', ['NAMESPACE desktop-prepared'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/registered.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE desktop-prepared'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}], ['report_first_run_auth_warnings', ['NAMESPACE auth report'], {}]], 'printed': []},
    'switchyard <project>, the launch fails': {'result': 8, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}], ['prepare_project_desktop', ['NAMESPACE switchyard'], {}], ['run_switchyard_launch_first_run_auth', ['NAMESPACE desktop-prepared'], {}], ['stop_before_launch_for_missing_owner_clis', ['NAMESPACE auth report'], {}], ['stop_before_launch_for_unauthenticated_providers', ['NAMESPACE auth report'], {}], ['stop_before_launch_for_unknown_models', ['NAMESPACE auth report'], {'project': 'p429'}], ['launch_project', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'mode': 'start', 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE desktop-prepared'], {}], ['_launch_runners_and_paths', ['NAMESPACE desktop-prepared'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/registered.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE desktop-prepared'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}], ['_start_workers_and_present', ['NAMESPACE prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'delegate_role_sessions_to_owner': True, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'force_reload': False, 'konsole_process_launcher': None, 'layout_environ': None, 'layout_mode': 'auto', 'layout_output': None, 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'role_process_runner': 'ROLE-RUNNER', 'runner': 'CALLABLE subprocess.run', 'window_title': 'P 429'}], ['_report_launch', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'effective_pane_state_dir': 'PATH TMP/effective-state', 'failed_roles': ['app'], 'launch_started_at': 1429.5, 'launch_started_ns': 1429, 'mode': 'attach-or-start', 'print_func': 'print', 'reconcile_home': 'PATH TMP/reconcile', 'report_session_records': True, 'resolved_layout_mode': 'viewer', 'running_roles': "{'main'}", 'session_record_poll': 0.2, 'session_record_timeout': 10.0, 'unreconciled_roles': ['audit'], 'worker_start_exit_code': 0}]], 'printed': []},
    'switchyard <project>, missing owner CLIs': {'result': 1, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}], ['prepare_project_desktop', ['NAMESPACE switchyard'], {}], ['run_switchyard_launch_first_run_auth', ['NAMESPACE desktop-prepared'], {}], ['stop_before_launch_for_missing_owner_clis', ['NAMESPACE auth report'], {}]], 'printed': []},
    'switchyard <project>, the main launch stops on the layout': {'result': 2, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p429'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['p429']], {}], ['resume_tenant', ['NAMESPACE switchyard'], {'config_path': 'PATH TMP/registered.json'}], ['prepare_project_desktop', ['NAMESPACE switchyard'], {}], ['run_switchyard_launch_first_run_auth', ['NAMESPACE desktop-prepared'], {}], ['stop_before_launch_for_missing_owner_clis', ['NAMESPACE auth report'], {}], ['stop_before_launch_for_unauthenticated_providers', ['NAMESPACE auth report'], {}], ['stop_before_launch_for_unknown_models', ['NAMESPACE auth report'], {'project': 'p429'}], ['launch_project', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'mode': 'start', 'report_session_records': True, 'script_path': 'PATH REPO/scripts/team-launcher'}], ['process_authority_board_compatibility', ['NAMESPACE desktop-prepared'], {}], ['_launch_runners_and_paths', ['NAMESPACE desktop-prepared'], {'assign_layout_owner': None, 'config_path': 'PATH TMP/registered.json', 'layout_output': None, 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['upgrade_generated_project_layout', ['NAMESPACE desktop-prepared'], {'config_path': 'PATH TMP/registered.json', 'runner': 'CALLABLE subprocess.run'}], ['prepare_project_desktop', ['NAMESPACE desktop-prepared'], {'runner': 'CALLABLE subprocess.run'}], ['_verify_pane_launcher_path', ['NAMESPACE desktop-prepared'], {'runner': 'WORKTREE-RUNNER', 'script_path': 'PATH REPO/scripts/team-launcher'}], ['_prepare_launch', ['NAMESPACE desktop-prepared'], {'allow_stale_launcher': False, 'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'effective_pane_state_dir': 'PATH TMP/effective-state', 'mode': 'attach-or-start', 'no_launcher_self_deploy': False, 'owner_home': None, 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'print_func': 'print', 'runner': 'CALLABLE subprocess.run', 'worktree_runner': 'WORKTREE-RUNNER'}], ['_write_layout_and_plan', ['NAMESPACE prepared'], {'config_path': 'PATH TMP/registered.json', 'dry_run': False, 'failed_roles': ['app'], 'force_reload': False, 'layout_environ': None, 'layout_mode': 'auto', 'mode': 'attach-or-start', 'output_path': 'PATH TMP/out.kdl', 'pane_script_path': 'PATH TMP/verified-pane-launcher', 'pane_state_dir': None, 'runner': 'CALLABLE subprocess.run', 'should_assign_layout_owner': False, 'window_title': 'P 429'}]], 'printed': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- the two modules its defaults come from.
DEFAULT_MODULES_LOADED = ['scripts.layout_modes', 'scripts.session_records']

# --- the cases, shared verbatim with `gold429.py` (which ran them on the baseline) ------------------------------------
# A case runs `launch_project` (directly, through `main`, or through `switchyard_main`) with a synthetic config. Every
# step it takes -- the board-authority check, the onboarding migration, the config loader, each launch phase, the layout
# upgrade, the desktop and the pane-launcher checks -- and every step its two dispatchers take before it, is a stand-in
# on the launcher answering from the case, recorded. Nothing is started: no pane, window, desktop, service, provider or
# project. Recorded, in order: every call with its arguments, everything printed, and the result or the exact refusal.
FULL = {"layout_output": "@/layout.kdl", "assign_layout_owner": True, "pane_state_dir": "@/pane-state", "force_reload": True,
        "allow_stale_launcher": True, "no_launcher_self_deploy": True, "report_session_records": True, "owner_home": "@/home/p429",
        "session_record_timeout": 4.5, "session_record_poll": 0.25, "layout_mode": "separate", "layout_environ": {"SYRD": "429"},
        "konsole_process_launcher": "KONSOLE"}
LAUNCH = {
    "start: every step, in order": {"mode": "start"},
    "attach-or-start, spelled out": {"mode": "attach-or-start"},
    "attach: the desktop is not prepared": {"mode": "attach"},
    "reload: the onboarding migrated, the config reloaded": {"mode": "reload", "migrated": True},
    "reload: nothing to migrate": {"mode": "reload", "migrated": False},
    "a reload dry run": {"mode": "reload", "kwargs": {"dry_run": True}},
    "a start dry run": {"mode": "start", "kwargs": {"dry_run": True}},
    "a mode nobody offers": {"mode": "restart"},
    "a stop is not a launch mode": {"mode": "stop"},
    "the running board lacks process authority": {"mode": "start", "authority": (False, "syrd429: an old board")},
    "the running board lacks it, on a reload": {"mode": "reload", "authority": (False, "syrd429: an old board")},
    "roles not isolated: no authority check": {"mode": "start", "isolated": False, "authority": (False, "never asked")},
    "the generated layout upgraded": {"mode": "start", "upgraded": "syrd429: the layout was upgraded"},
    "the layout upgraded on an attach": {"mode": "attach", "upgraded": "syrd429: the layout was upgraded"},
    "the preparation stops the launch": {"mode": "start", "prepare_exit": 3},
    "the preparation stops a dry run": {"mode": "start", "prepare_exit": 0, "kwargs": {"dry_run": True}},
    "the layout stops the launch": {"mode": "reload", "layout_exit": 4},
    "the layout stops with zero": {"mode": "start", "layout_exit": 0},
    "the workers stop the launch": {"mode": "start", "workers": 5},
    "the workers answer zero": {"mode": "start", "workers": 0},
    "the report fails": {"mode": "start", "report": 7},
    "the pane launcher is refused": {"mode": "start", "verify": "refuse"},
    "the desktop is refused": {"mode": "start", "desktop": "refuse"},
    "every option given": {"mode": "reload", "kwargs": FULL},
    "every option given, a dry run": {"mode": "attach", "kwargs": {**FULL, "dry_run": True}},
    "the defaults": {"mode": "start", "no_print": True},
    "the worker record rebound on the launcher": {"mode": "start", "launcher": {"WorkerStartup": "STAND-IN"}},
    "the defaults' sources rebound on the launcher": {"mode": "start", "no_print": True,
                                                     "launcher": {"LAYOUT_MODE_AUTO": "syrd429-mode", "LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS": 429.0,
                                                                  "LAUNCH_SESSION_RECORD_POLL_SECONDS": 4.29}},
}
CASES = {**{f"launch: {k}": {"call": "launch", **v} for k, v in LAUNCH.items()},
         "team-launcher start, through main": {"call": "main", "argv": ["p429", "start", "--config", "@/p429.json"]},
         "team-launcher reload, through main, every option": {"call": "main", "argv": ["p429", "reload", "--config", "@/p429.json", "--dry-run",
                                                                                       "--layout-output", "@/layout.kdl", "--pane-state-dir", "@/pane-state",
                                                                                       "--force", "--allow-stale-launcher", "--no-launcher-self-deploy",
                                                                                       "--layout", "separate", "--script-path", "@/team-launcher"]},
         "team-launcher attach, through main, refused": {"call": "main", "argv": ["p429", "attach", "--config", "@/p429.json"],
                                                         "authority": (False, "syrd429: an old board")},
         "switchyard start, through switchyard_main": {"call": "switchyard", "argv": ["start", "p429"]},
         "switchyard start, the launch fails": {"call": "switchyard", "argv": ["start", "p429"], "report": 6},
         "switchyard start, a resume problem": {"call": "switchyard", "argv": ["start", "p429"], "resume": ["syrd429: a unit would not start"]},
         "switchyard <project>, through switchyard_main": {"call": "switchyard", "argv": ["p429"]},
         "switchyard <project>, the launch fails": {"call": "switchyard", "argv": ["p429"], "report": 8},
         "switchyard <project>, missing owner CLIs": {"call": "switchyard", "argv": ["p429"], "stop_before": "missing"},
         "switchyard <project>, the main launch stops on the layout": {"call": "switchyard", "argv": ["p429"], "layout_exit": 2}}
FUNCTIONS = ("launch_project",)


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s function; every step it and its dispatchers take a recorder on `t`, the launcher."""
    import contextlib, dataclasses, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    printed: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd429-")).resolve()
    repo = _P(t.__file__).resolve().parent.parent

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP").replace(str(repo), "REPO")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"NAMESPACE {getattr(value, 'label', '?')}"
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {type(value).__name__: {f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if value is runner:
            return "RUNNER"
        if value is show:
            return "PRINT"
        if value is konsole:
            return "KONSOLE"
        if value is print:
            return "print"
        if callable(value):
            return f"CALLABLE {getattr(value, '__module__', '?')}.{getattr(value, '__qualname__', '?')}"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def show(line):
        printed.append(norm(line))

    def runner(*args, **kwargs):
        raise AssertionError(f"nothing is run in a launch case: {args}")

    def konsole(*args, **kwargs):
        raise AssertionError(f"no window is opened in a launch case: {args}")

    place = lambda v: (tmp / v[2:]) if isinstance(v, str) and v.startswith("@/") else (konsole if v == "KONSOLE" else v)
    config = lambda label: SimpleNamespace(label=label, project="p429", role_state_isolation=spec.get("isolated", True))
    names = [*FUNCTIONS, "process_authority_board_compatibility", "migrate_declarative_director_onboarding", "load_project_config",
             "_launch_runners_and_paths", "upgrade_generated_project_layout", "prepare_project_desktop", "_verify_pane_launcher_path",
             "_prepare_launch", "_write_layout_and_plan", "_start_workers_and_present", "_report_launch", "WorkerStartup",
             "LAYOUT_MODE_AUTO", "LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS", "LAUNCH_SESSION_RECORD_POLL_SECONDS",
             "_resolve_launcher_project_config", "report_installed_release_version", "_resolve_switchyard_project",
             "_load_switchyard_project_config_for_command", "resume_tenant", "run_switchyard_launch_first_run_auth",
             "stop_before_launch_for_missing_owner_clis", "stop_before_launch_for_unauthenticated_providers",
             "stop_before_launch_for_unknown_models", "report_first_run_auth_warnings"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}

        @dataclasses.dataclass(frozen=True)
        class StandInStartup:
            worker_start_exit_code: int
            launch_started_at: float
            launch_started_ns: int
            resolved_layout_mode: str

        def authority(cfg):
            note("process_authority_board_compatibility", cfg)
            return spec.get("authority", (True, ""))

        def migrate(cfg, *, config_path, print_func):
            note("migrate_declarative_director_onboarding", cfg, config_path=config_path, print_func=print_func)
            return spec.get("migrated", False)

        def load(project, path):
            note("load_project_config", project, path)
            return config(f"loaded {len([c for c in calls if c[0] == 'load_project_config'])}")

        def runners_and_paths(cfg, **kwargs):
            note("_launch_runners_and_paths", cfg, **kwargs)
            return SimpleNamespace(worktree_runner="WORKTREE-RUNNER", role_process_runner="ROLE-RUNNER", delegate_role_sessions_to_owner=True,
                                   effective_pane_state_dir=tmp / "effective-state", output_path=tmp / "out.kdl", window_title="P 429",
                                   should_assign_layout_owner=False, pane_script_path=tmp / "setup-pane")

        def upgrade(cfg, *, config_path, runner):
            note("upgrade_generated_project_layout", cfg, config_path=config_path, runner=runner)
            return SimpleNamespace(changed="upgraded" in spec, message=spec.get("upgraded", ""))

        def desktop(cfg, **kwargs):
            note("prepare_project_desktop", cfg, **kwargs)
            if spec.get("desktop") == "refuse":
                raise SystemExit("team-launcher: syrd429 desktop refused")
            return config("desktop-prepared")

        def verify(cfg, *, script_path, runner):
            note("_verify_pane_launcher_path", cfg, script_path=script_path, runner=runner)
            if spec.get("verify") == "refuse":
                raise SystemExit("team-launcher: syrd429 pane launcher missing")
            return tmp / "verified-pane-launcher"

        def prepare(cfg, **kwargs):
            note("_prepare_launch", cfg, **kwargs)
            return SimpleNamespace(exit_code=spec.get("prepare_exit"), config=config("prepared"), failed_roles=["app"], running_roles={"main"},
                                   reconcile_home=tmp / "reconcile", unreconciled_roles=("audit",))

        def layout(cfg, **kwargs):
            note("_write_layout_and_plan", cfg, **kwargs)
            return spec.get("layout_exit")

        def workers(cfg, **kwargs):
            note("_start_workers_and_present", cfg, **kwargs)
            if "workers" in spec:
                return spec["workers"]
            record = StandInStartup if spec.get("launcher", {}).get("WorkerStartup") == "STAND-IN" else saved["WorkerStartup"]
            return record(worker_start_exit_code=0, launch_started_at=1429.5, launch_started_ns=1429, resolved_layout_mode="viewer")

        def report(cfg, **kwargs):
            note("_report_launch", cfg, **kwargs)
            return spec.get("report", 0)

        def resume(cfg, *, config_path):
            note("resume_tenant", cfg, config_path=config_path)
            return spec.get("resume", [])

        def stop_before(kind):
            return lambda report, **kwargs: note(f"stop_before_launch_for_{kind}", report, **kwargs) or spec.get("stop_before") == kind.split("_")[0]

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            return f
        on_launcher = {
            "process_authority_board_compatibility": authority,
            "migrate_declarative_director_onboarding": migrate,
            "load_project_config": load,
            "_launch_runners_and_paths": runners_and_paths,
            "upgrade_generated_project_layout": upgrade,
            "prepare_project_desktop": desktop,
            "_verify_pane_launcher_path": verify,
            "_prepare_launch": prepare,
            "_write_layout_and_plan": layout,
            "_start_workers_and_present": workers,
            "_report_launch": report,
            "launch_project": passthrough("launch_project"),
            "_resolve_launcher_project_config": lambda project, *, explicit_config=None: note(
                "_resolve_launcher_project_config", project, explicit_config=explicit_config) or SimpleNamespace(
                    label="resolved", config_path=tmp / "p429.json", slug="p429"),
            "report_installed_release_version": lambda: note("report_installed_release_version"),
            "_resolve_switchyard_project": lambda selection: note("_resolve_switchyard_project", selection) or SimpleNamespace(
                label="entry", config_path=tmp / "registered.json"),
            "_load_switchyard_project_config_for_command": lambda entry, argv: note(
                "_load_switchyard_project_config_for_command", entry, argv) or config("switchyard"),
            "resume_tenant": resume,
            "run_switchyard_launch_first_run_auth": lambda cfg: note("run_switchyard_launch_first_run_auth", cfg) or SimpleNamespace(label="auth report"),
            "stop_before_launch_for_missing_owner_clis": stop_before("missing_owner_clis"),
            "stop_before_launch_for_unauthenticated_providers": stop_before("unauthenticated_providers"),
            "stop_before_launch_for_unknown_models": stop_before("unknown_models"),
            "report_first_run_auth_warnings": lambda rep: note("report_first_run_auth_warnings", rep),
        }
        on_launcher.update({k: (StandInStartup if v == "STAND-IN" else v) for k, v in spec.get("launcher", {}).items()})
        for n, f in on_launcher.items():
            setattr(t, n, f)
        call = spec["call"]
        try:
            with contextlib.redirect_stdout(SimpleNamespace(write=lambda s: printed.append(["stdout", norm(s)]) if s.strip() else None, flush=lambda: None)):
                if call == "launch":
                    kwargs = {k: place(v) for k, v in spec.get("kwargs", {}).items()}
                    if not spec.get("no_print"):  # "the defaults" cases leave the runner and the printer to the definition
                        kwargs.update(runner=runner, print_func=show)
                    got = under["launch_project"](config("given"), config_path=tmp / "p429.json", mode=spec["mode"],
                                                  script_path=tmp / "team-launcher", **kwargs)
                elif call == "main":
                    got = t.main([str(place(a)) for a in spec["argv"]])
                else:
                    got = t.switchyard_main(list(spec["argv"]))
            result = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"result": result, "calls": calls, "printed": printed}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
        import shutil
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
    result = python("import sys, scripts.project_launch as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only the two modules its defaults come from, never the launcher: "
          f"{result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_object_and_the_launchers_defaults() -> None:
    for order in (("scripts.project_launch", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_launch")):
        result = python("import builtins, importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_launch as m; "
                        "p = inspect.signature(m.launch_project).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "p['runner'].default is subprocess.run and p['print_func'].default is builtins.print "
                        "and p['session_record_timeout'].default is t.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS "
                        "and p['session_record_poll'].default is t.LAUNCH_SESSION_RECORD_POLL_SECONDS "
                        "and p['layout_mode'].default is t.LAYOUT_MODE_AUTO, "
                        "sorted(n for n, v in p.items() if v.default is not inspect.Parameter.empty and v.default not in (None, False)), "
                        "not hasattr(m, 'launcher'))")
        check(result.stdout.strip() == "True True ['layout_mode', 'print_func', 'runner', 'session_record_poll', 'session_record_timeout'] True",
              f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    from scripts import layout_modes, session_records
    check(m.LAYOUT_MODE_AUTO is layout_modes.LAYOUT_MODE_AUTO is t.LAYOUT_MODE_AUTO
          and m.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS is session_records.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS is t.LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS
          and m.LAUNCH_SESSION_RECORD_POLL_SECONDS is session_records.LAUNCH_SESSION_RECORD_POLL_SECONDS is t.LAUNCH_SESSION_RECORD_POLL_SECONDS,
          "the defaults come from the modules the launcher imports them from")
    check(m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "project_launch.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
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
              f"{name}: the launcher imported first thing, and nothing else nested (the baseline had no nested import): {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from pathlib import Path", "from typing import TYPE_CHECKING, Any, Callable",
                  "from scripts.layout_modes import LAYOUT_MODE_AUTO",
                  "from scripts.session_records import LAUNCH_SESSION_RECORD_POLL_SECONDS, LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, the defaults' own modules, and the config type for annotations only: {top} {tc}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the command, and nothing else: {names}")


def test_the_launcher_reexports_it_and_its_callers_reach_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_launch"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the command, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read it")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads}
    check(not defined & set(MOVED) and seams | {"main", "switchyard_main", "_build_parser", "ProjectConfig", "LAYOUT_MODE_AUTO",
                                                  "LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS", "LAUNCH_SESSION_RECORD_POLL_SECONDS",
                                                  "resume_tenant", "run_switchyard_launch_first_run_auth"} <= defined | exported,
          "the launcher defines none of it, and keeps its neighbours and every seam and default it reads, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"main and switchyard_main call it by its launcher global, exactly as often as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's name, or reads it at module level: {past} {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads it through the launcher: {got}")


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

    def seams(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    def kw(label, seam):
        return next(c[2] for c in GOLDEN[label]["calls"] if c[0] == seam)

    PHASES = ["_launch_runners_and_paths", "upgrade_generated_project_layout", "prepare_project_desktop", "_verify_pane_launcher_path",
              "_prepare_launch", "_write_layout_and_plan", "_start_workers_and_present", "_report_launch"]
    check(seams("launch: start: every step, in order") == ["process_authority_board_compatibility", *PHASES]
          and result("launch: start: every step, in order") == 0,
          "the authority check, then runners and paths, the layout upgrade, the desktop, the pane launcher, and the phases in order")
    check(result("launch: a mode nobody offers") == {"raised": "SystemExit", "message": "unknown launch mode: restart"}
          and seams("launch: a mode nobody offers") == [] and kw("launch: attach-or-start, spelled out", "_prepare_launch")["mode"]
          == kw("launch: start: every step, in order", "_prepare_launch")["mode"] == "attach-or-start",
          "start means attach-or-start; an unknown mode is refused before anything runs")
    check(result("launch: the running board lacks process authority") == 1
          and GOLDEN["launch: the running board lacks process authority"]["printed"]
          == ["team-launcher: refusing to launch p429 before changing local state: its running board does not provide project-account "
              "process authority (syrd429: an old board)"]
          and seams("launch: the running board lacks process authority") == ["process_authority_board_compatibility"]
          and "process_authority_board_compatibility" not in seams("launch: roles not isolated: no authority check"),
          "an isolated project on a board without process authority is refused, word for word, before any local state changes")
    check(seams("launch: reload: the onboarding migrated, the config reloaded")[1:3] == ["migrate_declarative_director_onboarding", "load_project_config"]
          and "load_project_config" not in seams("launch: reload: nothing to migrate")
          and GOLDEN["launch: reload: the onboarding migrated, the config reloaded"]["calls"][3][1] == ["NAMESPACE loaded 1"],
          "a reload migrates the onboarding first, and launches from the reloaded config when it changed")
    check(seams("launch: a reload dry run") == seams("launch: a start dry run") == ["_launch_runners_and_paths", *PHASES[4:]],
          "a dry run checks, migrates, upgrades, prepares and verifies nothing")
    check("prepare_project_desktop" not in seams("launch: attach: the desktop is not prepared"), "an attach leaves the desktop alone")
    check(GOLDEN["launch: the generated layout upgraded"]["printed"] == ["syrd429: the layout was upgraded"]
          and seams("launch: the generated layout upgraded")[3] == "load_project_config", "an upgraded layout is said, and the config reloaded")
    check([result(f"launch: {k}") for k in ("the preparation stops the launch", "the layout stops the launch", "the workers stop the launch", "the report fails")]
          == [3, 4, 5, 7] and seams("launch: the preparation stops the launch")[-1] == "_prepare_launch"
          and seams("launch: the layout stops with zero")[-1] == "_write_layout_and_plan"
          and seams("launch: the workers answer zero")[-1] == "_start_workers_and_present",
          "each phase's stop is the answer, and nothing runs after it")
    check(kw("launch: every option given", "_start_workers_and_present")["konsole_process_launcher"] == "KONSOLE"
          and kw("launch: every option given", "_report_launch")["session_record_timeout"] == 4.5
          and kw("launch: every option given", "_write_layout_and_plan")["layout_environ"] == {"SYRD": "429"},
          "every option reaches the phase that uses it")
    rebound = "launch: the defaults' sources rebound on the launcher"
    check(kw(rebound, "_write_layout_and_plan")["layout_mode"] == kw("launch: the defaults", "_write_layout_and_plan")["layout_mode"] == "auto"
          and kw(rebound, "_report_launch")["session_record_timeout"] == 10.0 and kw(rebound, "_report_launch")["session_record_poll"] == 0.2
          and kw(rebound, "_report_launch")["print_func"] == "print",
          "the defaults are bound when the command is defined: a rebinding on the launcher does not reach them")
    check(result("launch: the worker record rebound on the launcher") == 0 and seams("launch: the worker record rebound on the launcher")[-1] == "_report_launch",
          "the worker record is the launcher's at call time")
    main_start = seams("team-launcher start, through main")
    check(main_start[:3] == ["_resolve_launcher_project_config", "load_project_config", "launch_project"]
          and kw("team-launcher start, through main", "launch_project")["script_path"] == "PATH REPO/scripts/team-launcher"
          and kw("team-launcher start, through main", "launch_project")["report_session_records"] is True,
          "main starts a project through the launcher's name, the pane launcher beside the launcher")
    check(seams("switchyard start, through switchyard_main")[3:6] == ["resume_tenant", "prepare_project_desktop", "launch_project"]
          and result("switchyard start, the launch fails") == 6 and "launch_project" not in seams("switchyard start, a resume problem"),
          "switchyard start resumes, prepares the desktop, then launches; a resume problem stops it")
    bare = "switchyard <project>, through switchyard_main"
    check(seams(bare)[-1] == "report_first_run_auth_warnings" and "report_first_run_auth_warnings" not in seams("switchyard <project>, the launch fails")
          and result("switchyard <project>, the launch fails") == 8 and "launch_project" not in seams("switchyard <project>, missing owner CLIs"),
          "the bare project launch checks the first run first, and reports its warnings only after a launch that succeeded")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the command reads is a recorder on the launcher (the command itself included, through its callers);
    # the worker record, a class, is rebound there by its own case, which changes the answer.
    names = {name for reads in SEAMS.values() for name in reads}
    record = {"WorkerStartup"}
    check(names - record <= REACHED and "launch_project" in REACHED, f"a recorder on the launcher reached every function: missing {sorted(names - record - REACHED)}")
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(rebound == record | {"LAYOUT_MODE_AUTO", "LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS", "LAUNCH_SESSION_RECORD_POLL_SECONDS"},
          f"and the record and the defaults' sources rebound: {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_object_and_the_launchers_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it_and_its_callers_reach_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"project_launch_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
