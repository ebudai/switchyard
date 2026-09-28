#!/usr/bin/env python3
"""SYRD-421: the new-project precheck, against the launcher it came out of.

The fourteen definitions that decide whether a new project may be created --
the precheck, its deploy-source, unit, PostgreSQL, database and board probes,
and the two probes it binds as defaults -- moved unchanged into
`scripts/new_project_precheck.py`; the launcher re-exports all fourteen. This
pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads only `scripts.polkit_readiness`, where its polkit default
  comes from; the definition-time defaults -- the two probes, `subprocess.run`
  and the polkit check -- are the objects the launcher bound; the plan type is
  imported under TYPE_CHECKING only.
- **Seams (rule 24):** every name the fourteen read -- each other, the
  launcher's config and registry directories, unit helpers, registry lookup,
  launcher name and its own file, the release marker readers, the git status,
  the unit renderer and the account lookup -- is read through the launcher as
  often as before, so a patch there reaches it: every case below stands in for
  the host-facing ones there, and rebinds the launcher name and the postgres
  unit to see the effect.
- **Callers:** `new_project_command` calls the precheck by its launcher global,
  and `new_project_phases.py` reads it through the launcher.
- **The behaviour is the baseline's:** every reason, in order, and every
  combination that stops a new project, the deploy-source rules, the unit
  listing and its fallback, the PostgreSQL remedy for each state, the database
  and board probes as the owner and as root, and every refusal's text.
  `GOLDEN` below was produced by running the BASELINE launcher's own functions
  over the very cases embedded here (`gold421.py`), not typed; it is
  byte-identical whether generated under `env -i` or in a normal role pane.

No live service, database, account or socket is touched: the runner, the
account lookup, the port, socket and polkit checks and every host path stand in,
recorded. Spawns, every exec, signals, account and group lookups and socket
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
from scripts import new_project_precheck as m  # noqa: E402

CHECKS = 0
MOVED = ('_path_exists', '_tcp_port_in_use', '_looks_like_switchyard_release_tree', '_switchyard_release_source_error', '_precheck_deploy_source', '_system_unit_file_exists', 'POSTGRES_SERVICE_UNIT', 'POSTGRES_ADMIN_SOCKET_DIR', 'postgres_cluster_script', 'postgres_availability_remedy', '_database_exists', '_ticket_board_table_count', '_installed_unit_is_this_plans', 'precheck_new_project')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_looks_like_switchyard_release_tree': {'TEAM_LAUNCHER_NAME': 1},
    '_switchyard_release_source_error': {'SWITCHYARD_RELEASE_MARKER_NAME': 3, '_looks_like_switchyard_release_tree': 2, '_read_switchyard_release_marker': 1, 'shared_switchyard_release_for_path': 1},
    '_precheck_deploy_source': {'SWITCHYARD_RELEASE_MARKER_NAME': 1, '_git_status_porcelain': 1, '_path_exists': 1, '_switchyard_release_source_error': 1},
    '_system_unit_file_exists': {'_path_exists': 2},
    'postgres_cluster_script': {'__file__': 1},
    'postgres_availability_remedy': {'POSTGRES_ADMIN_SOCKET_DIR': 2, 'POSTGRES_SERVICE_UNIT': 7, '_system_unit_file_exists': 1, '_system_unit_is_active': 1, 'postgres_cluster_script': 1},
    '_database_exists': {'postgres_availability_remedy': 2},
    '_installed_unit_is_this_plans': {'_installed_unit_path': 1, 'render_board_unit': 1},
    'precheck_new_project': {'DEFAULT_CONFIG_DIR': 1, '_database_exists': 1, '_installed_unit_is_this_plans': 1, '_path_exists': 1, '_precheck_deploy_source': 1, '_system_unit_file_exists': 1, '_ticket_board_table_count': 1, '_usable_switchyard_entry_for_project': 1, 'switchyard_registry_dir': 1, 'uid_for_user': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the fourteen that names one of them, and how often.
DISPATCH = {'new_project_command': {'_path_exists': 1, '_tcp_port_in_use': 1, 'precheck_new_project': 1}, 'switchyard_new_command': {'_path_exists': 1, '_tcp_port_in_use': 1}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/new_project_phases.py': ['launcher.precheck_new_project']}
#: The BASELINE's own behaviour for the cases below (`gold421.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'precheck: a clean first run': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: polkit not ready': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- polkit rule missing\n- agent absent'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: no such owner': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- target user 'syrd421-owner' does not exist"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: no such owner, not required': {'answer': None, 'calls': [['polkit_problems', [], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: no repository': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- project repository TMP/repo does not exist'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', False], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: no repository, not required': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: everything wrong at once': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- polkit rule missing\n- target user 'syrd421-owner' does not exist\n- project repository TMP/repo does not exist\n- deploy source TMP/nowhere does not exist\n- database 'p421_board' already exists but p421-board.service is not installed\n- socket TMP/sock/board.sock already exists but p421-board.service is not installed\n- port 24210 is already in use but p421-board.service is not installed"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', False], ['_precheck_deploy_source', ['PATH TMP/nowhere'], {}], ['_path_exists', 'PATH TMP/nowhere', 'probed', False], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: the database is there but not the unit': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- database 'p421_board' already exists but p421-board.service is not installed"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: the socket is there but not the unit': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- socket TMP/sock/board.sock already exists but p421-board.service is not installed'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: the port is live but not the unit': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- port 24210 is already in use but p421-board.service is not installed'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: all three, no unit': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- database 'p421_board' already exists but p421-board.service is not installed\n- socket TMP/sock/board.sock already exists but p421-board.service is not installed\n- port 24210 is already in use but p421-board.service is not installed"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    "precheck: the unit is this plan's, no database": {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_installed_unit_is_this_plans', ['PLAN'], {}], ['_installed_unit_path', ['p421-board.service'], {}], ['render_board_unit', [], {}]]},
    'precheck: another unit, no database': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- p421-board.service is installed but database 'p421_board' does not exist, and the installed unit is not the one this provisioning would install.\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_installed_unit_is_this_plans', ['PLAN'], {}], ['_installed_unit_path', ['p421-board.service'], {}], ['render_board_unit', [], {}]]},
    'precheck: an unreadable unit, no database': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- p421-board.service is installed but database 'p421_board' does not exist, and the installed unit is not the one this provisioning would install.\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_installed_unit_is_this_plans', ['PLAN'], {}], ['_installed_unit_path', ['p421-board.service'], {}]]},
    'precheck: unit and an empty database': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'precheck: already provisioned': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- project 'p421' is already provisioned (database p421_board has 12 ticket_board tables, p421-board.service is installed).\n  to launch it:      switchyard p421\n  to start over:     switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['_usable_switchyard_entry_for_project', ['p421'], {'config_dir': None, 'registry_dir': None}]]},
    'precheck: partially provisioned, a broken entry': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- project 'p421' is partially provisioned but not registered (database p421_board has 12 ticket_board tables, p421-board.service is installed, but no usable launch entry exists in TMP/config or TMP/registry).\n  unusable launch entry: TMP/config/p421.json: unreadable\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['_usable_switchyard_entry_for_project', ['p421'], {'config_dir': None, 'registry_dir': None}], ['switchyard_registry_dir', [], {}]]},
    'precheck: partially provisioned, no entry': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- project 'p421' is partially provisioned but not registered (database p421_board has 3 ticket_board tables, p421-board.service is installed, but no usable launch entry exists in TMP/config or TMP/registry).\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['_usable_switchyard_entry_for_project', ['p421'], {'config_dir': None, 'registry_dir': None}], ['switchyard_registry_dir', [], {}]]},
    'precheck: exactly one board table': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- project 'p421' is partially provisioned but not registered (database p421_board has 1 ticket_board tables, p421-board.service is installed, but no usable launch entry exists in TMP/config or TMP/registry).\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['_usable_switchyard_entry_for_project', ['p421'], {'config_dir': None, 'registry_dir': None}], ['switchyard_registry_dir', [], {}]]},
    'precheck: the database probe answers something other than 1': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- socket TMP/sock/board.sock already exists but p421-board.service is not installed'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: partially provisioned, explicit directories': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- project 'p421' is partially provisioned but not registered (database p421_board has 3 ticket_board tables, p421-board.service is installed, but no usable launch entry exists in TMP/cfg or TMP/reg).\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['_usable_switchyard_entry_for_project', ['p421'], {'config_dir': 'PATH TMP/cfg', 'registry_dir': 'PATH TMP/reg'}]]},
    'precheck: as root: the probes run as postgres': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['sudo', '-u', 'postgres', 'psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['sudo', '-u', 'postgres', 'psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'precheck: the unit listing fails, the file is there': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_path_exists', 'PATH /etc/systemd/system/p421-board.service', 'stood in', True], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_installed_unit_is_this_plans', ['PLAN'], {}], ['_installed_unit_path', ['p421-board.service'], {}], ['render_board_unit', [], {}]]},
    'precheck: the unit listing fails, no file': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_path_exists', 'PATH /etc/systemd/system/p421-board.service', 'stood in', False], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: systemctl cannot run, the file is there': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- p421-board.service is installed but database 'p421_board' does not exist, and the installed unit is not the one this provisioning would install.\n  to inspect recovery: switchyard teardown p421 --dry-run"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_path_exists', 'PATH /etc/systemd/system/p421-board.service', 'stood in', True], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_installed_unit_is_this_plans', ['PLAN'], {}], ['_installed_unit_path', ['p421-board.service'], {}], ['render_board_unit', [], {}]]},
    'precheck: the unit listing says zero units': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: new project precheck failed:\n- database 'p421_board' already exists but p421-board.service is not installed"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck: the database probe fails, postgres not installed': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot verify PostgreSQL database availability: psql: error: connection to server on socket failed\nteam-launcher: this host has no postgresql.service, so no PostgreSQL server is installed.\n  install the host packages first: sudo scripts/install-switchyard-prereqs\nteam-launcher: nothing was created; re-run `switchyard new` once that answers.'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['postgres_availability_remedy', [], {}], ['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'precheck: the database probe fails, postgres inactive': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot verify PostgreSQL database availability: psql: error: connection to server on socket failed\nteam-launcher: postgresql.service is installed but not running, so nothing is serving /var/run/postgresql.\n  initialize the cluster if this host has none, then start the service: sudo REPO/scripts/ensure-postgres-cluster\n  it is idempotent, never re-initializes an existing cluster, and verifies the same socket this check uses.\nteam-launcher: nothing was created; re-run `switchyard new` once that answers.'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['postgres_availability_remedy', [], {}], ['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_system_unit_is_active', ['postgresql.service'], {}]]},
    'precheck: the database probe fails, postgres active': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot verify PostgreSQL database availability: psql: error: connection to server on socket failed\nteam-launcher: postgresql.service is active, but the admin connection over /var/run/postgresql did not answer.\n  read why: sudo systemctl status postgresql.service --no-pager\n  and: sudo journalctl -u postgresql.service -n 50 --no-pager\n  then re-verify the socket: sudo REPO/scripts/ensure-postgres-cluster\nteam-launcher: nothing was created; re-run `switchyard new` once that answers.'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['postgres_availability_remedy', [], {}], ['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_system_unit_is_active', ['postgresql.service'], {}]]},
    'precheck: the database probe fails silently': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot verify PostgreSQL database availability\nteam-launcher: postgresql.service is active, but the admin connection over /var/run/postgresql did not answer.\n  read why: sudo systemctl status postgresql.service --no-pager\n  and: sudo journalctl -u postgresql.service -n 50 --no-pager\n  then re-verify the socket: sudo REPO/scripts/ensure-postgres-cluster\nteam-launcher: nothing was created; re-run `switchyard new` once that answers.'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['postgres_availability_remedy', [], {}], ['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_system_unit_is_active', ['postgresql.service'], {}]]},
    'precheck: psql cannot run': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: cannot verify PostgreSQL database availability: [Errno 2] No such file or directory: 'psql'\nteam-launcher: postgresql.service is installed but not running, so nothing is serving /var/run/postgresql.\n  initialize the cluster if this host has none, then start the service: sudo REPO/scripts/ensure-postgres-cluster\n  it is idempotent, never re-initializes an existing cluster, and verifies the same socket this check uses.\nteam-launcher: nothing was created; re-run `switchyard new` once that answers."}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['postgres_availability_remedy', [], {}], ['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_system_unit_is_active', ['postgresql.service'], {}]]},
    'precheck: the table count fails': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: cannot inspect PostgreSQL database 'p421_board': permission denied for database"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'precheck: the table count cannot run': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: cannot inspect PostgreSQL database 'p421_board': [Errno 2] No such file or directory: 'psql'"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'precheck: the table count is not a number': {'answer': {'raised': 'SystemExit', 'message': "team-launcher: cannot parse ticket_board table count for database 'p421_board': 'junk'"}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}], ['_ticket_board_table_count', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///p421_board?host=/var/run/postgresql', '-c', "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'precheck: a database name with a quote': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ["p421'x"], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421''x'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: missing': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy source TMP/nowhere does not exist'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/nowhere'], {}], ['_path_exists', 'PATH TMP/nowhere', 'probed', False], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: git-dirty': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy checkout TMP/src has uncommitted changes'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: not-git': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy source TMP/src is neither a git checkout nor a Switchyard release; expected a clean Switchyard source checkout, or an exported release with .switchyard-release.json'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: git-broken': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: TMP/src is not a git checkout'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/src'], {}], ['_path_exists', 'PATH TMP/src', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/src'], {}], ['_read_switchyard_release_marker', ['PATH TMP/src'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/src'], {}], ['_git_status_porcelain', ['PATH TMP/src'], {}]]},
    'precheck source: release-ok': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/rel'], {}], ['_path_exists', 'PATH TMP/rel', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/rel'], {}], ['_read_switchyard_release_marker', ['PATH TMP/rel'], {}], ['_looks_like_switchyard_release_tree', ['PATH TMP/rel'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: release-bad-marker': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy source TMP/rel has an invalid .switchyard-release.json: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/rel'], {}], ['_path_exists', 'PATH TMP/rel', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/rel'], {}], ['_read_switchyard_release_marker', ['PATH TMP/rel'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: release-marker-no-files': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy source TMP/rel has .switchyard-release.json, but is missing the expected exported launcher files'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/rel'], {}], ['_path_exists', 'PATH TMP/rel', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/rel'], {}], ['_read_switchyard_release_marker', ['PATH TMP/rel'], {}], ['_looks_like_switchyard_release_tree', ['PATH TMP/rel'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: shared-commit': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/opt/releases/r1/sub'], {}], ['_path_exists', 'PATH TMP/opt/releases/r1/sub', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/opt/releases/r1/sub'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/sub'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/opt/releases/r1/sub'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/sub'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: shared-error': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy source TMP/opt/releases/r1/sub has an invalid shared release marker at TMP/opt/releases/r1: Expecting value: line 1 column 12 (char 11)'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/opt/releases/r1/sub'], {}], ['_path_exists', 'PATH TMP/opt/releases/r1/sub', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/opt/releases/r1/sub'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/sub'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/opt/releases/r1/sub'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/sub'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: shared-nomarker-tree': {'answer': None, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/opt/current'], {}], ['_path_exists', 'PATH TMP/opt/current', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/opt/current'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/opt/current'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}], ['_looks_like_switchyard_release_tree', ['PATH TMP/opt/current'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'precheck source: shared-nomarker-bare': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: new project precheck failed:\n- deploy source TMP/opt/current is under the Switchyard shared install, but is missing .switchyard-release.json and the expected exported launcher files'}, 'calls': [['polkit_problems', [], {}], ['uid_for_user', ['syrd421-owner'], {}], ['_path_exists', 'PATH TMP/repo', 'probed', True], ['_precheck_deploy_source', ['PATH TMP/opt/current'], {}], ['_path_exists', 'PATH TMP/opt/current', 'probed', True], ['_switchyard_release_source_error', ['PATH TMP/opt/current'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}], ['shared_switchyard_release_for_path', ['PATH TMP/opt/current'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}], ['_looks_like_switchyard_release_tree', ['PATH TMP/opt/current'], {}], ['_system_unit_file_exists', ['p421-board.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'p421-board.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_database_exists', ['p421_board'], {}], ['runner', [['psql', '-XAt', 'postgresql:///postgres?host=/var/run/postgresql', '-c', "SELECT 1 FROM pg_database WHERE datname = 'p421_board'"]], {'stderr': -1, 'stdout': -1, 'text': True}], ['socket_exists', ['PATH TMP/sock/board.sock'], {}], ['port_in_use', [24210], {}]]},
    'helper: remedy: postgres not installed': {'answer': 'this host has no postgresql.service, so no PostgreSQL server is installed.\n  install the host packages first: sudo scripts/install-switchyard-prereqs', 'calls': [['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'helper: remedy: inactive': {'answer': 'postgresql.service is installed but not running, so nothing is serving /var/run/postgresql.\n  initialize the cluster if this host has none, then start the service: sudo REPO/scripts/ensure-postgres-cluster\n  it is idempotent, never re-initializes an existing cluster, and verifies the same socket this check uses.', 'calls': [['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_system_unit_is_active', ['postgresql.service'], {}]]},
    'helper: remedy: active': {'answer': 'postgresql.service is active, but the admin connection over /var/run/postgresql did not answer.\n  read why: sudo systemctl status postgresql.service --no-pager\n  and: sudo journalctl -u postgresql.service -n 50 --no-pager\n  then re-verify the socket: sudo REPO/scripts/ensure-postgres-cluster', 'calls': [['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['postgresql.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'postgresql.service']], {'stderr': -1, 'stdout': -1, 'text': True}], ['_system_unit_is_active', ['postgresql.service'], {}]]},
    'helper: the cluster script beside the launcher': {'answer': 'PATH REPO/scripts/ensure-postgres-cluster', 'calls': []},
    'helper: a release tree: complete': {'answer': True, 'calls': []},
    'helper: a release tree: no service script': {'answer': False, 'calls': []},
    'helper: a release tree: the launcher name rebound': {'answer': True, 'calls': []},
    'helper: the postgres unit rebound on the launcher': {'answer': 'this host has no syrd421-pg.service, so no PostgreSQL server is installed.\n  install the host packages first: sudo scripts/install-switchyard-prereqs', 'calls': [['postgres_cluster_script', [], {}], ['_system_unit_file_exists', ['syrd421-pg.service'], {}], ['runner', [['systemctl', 'list-unit-files', '--no-legend', 'syrd421-pg.service']], {'stderr': -1, 'stdout': -1, 'text': True}]]},
    'helper: the path probe on a missing path': {'answer': False, 'calls': []},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold421.py` (which ran them on the baseline) ------------------------------------
# A case runs the precheck (or one of its helpers) against a synthetic plan and a test-owned tree. The release-tree and
# marker checks are the launcher's own, run in the tree with the shared install root pointed into it. Everything that
# would reach the host is a recorder answering from the case: the runner (matched on the distinguishing argument --
# `list-unit-files`, `pg_database`, `pg_tables` -- never on a program name), the git status, the account lookup, the
# unit activity and path, the unit renderer, the registry lookup and directories, the port, socket and polkit checks,
# and `_path_exists` for any path outside the tree. The answer is None or the exact refusal, plus every call in order.
CLEAN = {"call": "precheck", "polkit": [], "uid": 1001, "repo": True, "source": "git-clean", "unit": "none", "db": "no", "socket": False, "port": False}
PRECHECK = {
    "a clean first run": {},
    "polkit not ready": {"polkit": ["polkit rule missing", "agent absent"]},
    "no such owner": {"uid": None},
    "no such owner, not required": {"uid": None, "require_owner_user": False},
    "no repository": {"repo": False},
    "no repository, not required": {"repo": False, "require_repository": False},
    "everything wrong at once": {"polkit": ["polkit rule missing"], "uid": None, "repo": False, "source": "missing", "db": "yes", "socket": True, "port": True},
    "the database is there but not the unit": {"db": "yes"},
    "the socket is there but not the unit": {"socket": True},
    "the port is live but not the unit": {"port": True},
    "all three, no unit": {"db": "yes", "socket": True, "port": True},
    "the unit is this plan's, no database": {"unit": "listed", "installed_unit": "match"},
    "another unit, no database": {"unit": "listed", "installed_unit": "other"},
    "an unreadable unit, no database": {"unit": "listed", "installed_unit": "missing"},
    "unit and an empty database": {"unit": "listed", "db": "yes", "tables": 0},
    "already provisioned": {"unit": "listed", "db": "yes", "tables": 12, "entry": "yes"},
    "partially provisioned, a broken entry": {"unit": "listed", "db": "yes", "tables": 12, "entry": "none-broken"},
    "partially provisioned, no entry": {"unit": "listed", "db": "yes", "tables": 3, "entry": "none"},
    "exactly one board table": {"unit": "listed", "db": "yes", "tables": 1, "entry": "none"},
    "the database probe answers something other than 1": {"db": "other", "socket": True},
    "partially provisioned, explicit directories": {"unit": "listed", "db": "yes", "tables": 3, "entry": "none", "dirs": True},
    "as root: the probes run as postgres": {"unit": "listed", "db": "yes", "tables": 0, "root": True},
    "the unit listing fails, the file is there": {"unit": "rc1-file", "db": "no", "installed_unit": "match"},
    "the unit listing fails, no file": {"unit": "rc1-nofile"},
    "systemctl cannot run, the file is there": {"unit": "oserror-file", "installed_unit": "other"},
    "the unit listing says zero units": {"unit": "zero", "db": "yes"},
    "the database probe fails, postgres not installed": {"db": "rc", "pg_unit": "absent"},
    "the database probe fails, postgres inactive": {"db": "rc", "pg_unit": "inactive"},
    "the database probe fails, postgres active": {"db": "rc", "pg_unit": "active"},
    "the database probe fails silently": {"db": "rc-noerr", "pg_unit": "active"},
    "psql cannot run": {"db": "oserror", "pg_unit": "inactive"},
    "the table count fails": {"unit": "listed", "db": "yes", "tables": "rc"},
    "the table count cannot run": {"unit": "listed", "db": "yes", "tables": "oserror"},
    "the table count is not a number": {"unit": "listed", "db": "yes", "tables": "junk"},
    "a database name with a quote": {"database": "p421'x"},
}
SOURCE = {f"source: {s}": {"source": s} for s in ("missing", "git-dirty", "not-git", "git-broken", "release-ok", "release-bad-marker", "release-marker-no-files",
                                                  "shared-commit", "shared-error", "shared-nomarker-tree", "shared-nomarker-bare")}
HELPERS = {
    "remedy: postgres not installed": {"call": "remedy", "pg_unit": "absent"},
    "remedy: inactive": {"call": "remedy", "pg_unit": "inactive"},
    "remedy: active": {"call": "remedy", "pg_unit": "active"},
    "the cluster script beside the launcher": {"call": "script"},
    "a release tree: complete": {"call": "tree", "files": ["switchyard", "scripts/team-launcher", "scripts/ticket-board-service.sh"]},
    "a release tree: no service script": {"call": "tree", "files": ["switchyard", "scripts/team-launcher"]},
    "a release tree: the launcher name rebound": {"call": "tree", "files": ["switchyard", "scripts/syrd421-launcher", "scripts/ticket-board-service.sh"],
                                                  "launcher": {"TEAM_LAUNCHER_NAME": "syrd421-launcher"}},
    "the postgres unit rebound on the launcher": {"call": "remedy", "pg_unit": "absent", "launcher": {"POSTGRES_SERVICE_UNIT": "syrd421-pg.service"}},
    "the path probe on a missing path": {"call": "path"},
}
CASES = {**{f"precheck: {k}": {**CLEAN, **v} for k, v in PRECHECK.items()}, **{f"precheck {k}": {**CLEAN, **v} for k, v in SOURCE.items()},
         **{f"helper: {k}": v for k, v in HELPERS.items()}}
FUNCTIONS = ("_path_exists", "_tcp_port_in_use", "_looks_like_switchyard_release_tree", "_switchyard_release_source_error", "_precheck_deploy_source",
             "_system_unit_file_exists", "postgres_cluster_script", "postgres_availability_remedy", "_database_exists", "_ticket_board_table_count",
             "_installed_unit_is_this_plans", "precheck_new_project")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s function, in a fresh test-owned tree; the host-facing seams stand in on `t`, recorded."""
    import shutil, tempfile
    from types import SimpleNamespace
    calls: list = []
    tmp = Path(tempfile.mkdtemp(prefix="syrd421-")).resolve()
    repo = Path(t.__file__).resolve().parent.parent

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        if isinstance(value, SimpleNamespace):
            return "PLAN"
        if isinstance(value, Path):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP").replace(str(repo), "REPO")
        if callable(value):
            return "CALLABLE"
        return value

    def note(name, *args, **kwargs):
        reached.add(name)
        calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items())))])

    def runner(argv, **kwargs):
        note("runner", list(argv), **kwargs)
        if "list-unit-files" in argv:
            unit = argv[-1]
            if unit == t.POSTGRES_SERVICE_UNIT:
                return SimpleNamespace(returncode=0, stdout="" if spec.get("pg_unit") == "absent" else f"{unit} enabled\n", stderr="")
            shape = spec.get("unit")
            if shape == "oserror-file":
                raise OSError(2, "No such file or directory: 'systemctl'")
            if shape in ("rc1-file", "rc1-nofile"):
                return SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus")
            return SimpleNamespace(returncode=0, stdout={"listed": f"{unit} enabled\n", "zero": "0 unit files listed.\n"}.get(shape, ""), stderr="")
        if any("pg_database" in str(a) for a in argv):
            db = spec.get("db")
            if db == "oserror":
                raise OSError(2, "No such file or directory: 'psql'")
            if db == "rc":
                return SimpleNamespace(returncode=2, stdout="", stderr="psql: error: connection to server on socket failed")
            if db == "rc-noerr":
                return SimpleNamespace(returncode=2, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout={"yes": "1\n", "other": "t\n"}.get(db, "\n"), stderr="")
        if any("pg_tables" in str(a) for a in argv):
            tables = spec.get("tables", 0)
            if tables == "oserror":
                raise OSError(2, "No such file or directory: 'psql'")
            if tables == "rc":
                return SimpleNamespace(returncode=1, stdout="", stderr="permission denied for database")
            return SimpleNamespace(returncode=0, stdout=f"{tables}\n", stderr="")
        raise AssertionError(f"the runner was asked something no case expects: {argv}")

    def git_status(path, *, runner):
        note("_git_status_porcelain", path)
        shape = spec.get("source")
        if shape in ("not-git", "git-broken"):
            raise SystemExit(f"team-launcher: {path} is not a git checkout")
        return " M scripts/x.py\n" if shape == "git-dirty" else ""

    real_path_exists = t._path_exists

    def path_exists(path):
        # Inside the tree, the launcher's own probe; outside it (the host's /etc/systemd/system), the case answers.
        inside = str(path).startswith(str(tmp) + "/")
        answer = real_path_exists(path) if inside else (spec.get("unit") in ("rc1-file", "oserror-file"))
        calls.append(["_path_exists", norm(path), "probed" if inside else "stood in", answer])
        reached.add("_path_exists")
        return answer

    names = [*FUNCTIONS, "_read_switchyard_release_marker", "shared_switchyard_release_for_path", "uid_for_user", "_git_status_porcelain", "_system_unit_is_active", "_installed_unit_path", "render_board_unit",
             "_usable_switchyard_entry_for_project", "switchyard_registry_dir", "DEFAULT_CONFIG_DIR", "TEAM_LAUNCHER_NAME", "POSTGRES_SERVICE_UNIT"]
    saved = {n: getattr(t, n) for n in names}
    saved_env = os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT")
    saved_geteuid = os.geteuid
    try:
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(tmp / "opt")
        tree = ["switchyard", "scripts/team-launcher", "scripts/ticket-board-service.sh"]
        source = {"missing": tmp / "nowhere", "release-ok": tmp / "rel", "release-bad-marker": tmp / "rel", "release-marker-no-files": tmp / "rel",
                  "shared-commit": tmp / "opt" / "releases" / "r1" / "sub", "shared-error": tmp / "opt" / "releases" / "r1" / "sub",
                  "shared-nomarker-tree": tmp / "opt" / "current", "shared-nomarker-bare": tmp / "opt" / "current"}.get(spec.get("source"), tmp / "src")
        shape = spec.get("source")
        if shape != "missing":
            source.mkdir(parents=True)
        if shape in ("release-ok", "release-bad-marker", "shared-nomarker-tree"):
            for rel in tree:
                (source / rel).parent.mkdir(parents=True, exist_ok=True)
                (source / rel).write_text("", encoding="utf-8")
        if shape in ("release-ok", "release-marker-no-files"):
            (source / ".switchyard-release.json").write_text('{"commit": "%s"}' % ("4" * 40), encoding="utf-8")
        if shape == "release-bad-marker":
            (source / ".switchyard-release.json").write_text("{", encoding="utf-8")
        if shape == "shared-commit":
            (source.parent / ".switchyard-release.json").write_text('{"commit": "%s"}' % ("5" * 40), encoding="utf-8")
        if shape == "shared-error":
            (source.parent / ".switchyard-release.json").write_text('{"commit": ', encoding="utf-8")
        if shape == "git-broken":
            (source / ".git").mkdir()
        for rel in spec.get("files", []):
            (tmp / "tree" / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / "tree" / rel).write_text("", encoding="utf-8")
        if spec.get("repo"):
            (tmp / "repo").mkdir()
        unit_file = tmp / "units" / "p421-board.service"
        if spec.get("installed_unit") in ("match", "other"):
            unit_file.parent.mkdir(parents=True)
            unit_file.write_text("[Unit]\nDescription=p421 board\n" if spec["installed_unit"] == "match" else "[Unit]\nDescription=someone else\n", encoding="utf-8")
        plan = SimpleNamespace(project="p421", owner_user="syrd421-owner", board_unit="p421-board.service", database=spec.get("database", "p421_board"),
                               socket_path=str(tmp / "sock" / "board.sock"), port=24210)
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        on_launcher = {
            "_path_exists": path_exists,
            "uid_for_user": lambda user: note("uid_for_user", user) or spec.get("uid"),
            "_git_status_porcelain": git_status,
            "_system_unit_is_active": lambda unit, *, runner: note("_system_unit_is_active", unit) or spec.get("pg_unit") == "active",
            "_installed_unit_path": lambda unit: note("_installed_unit_path", unit) or tmp / "units" / unit,
            "render_board_unit": lambda p: note("render_board_unit") or "[Unit]\nDescription=p421 board\n",
            "_usable_switchyard_entry_for_project": lambda project, *, config_dir, registry_dir: note("_usable_switchyard_entry_for_project", project, config_dir=config_dir,
                                                                                                        registry_dir=registry_dir) or (
                ("ENTRY" if spec.get("entry") == "yes" else None, ["TMP/config/p421.json: unreadable"] if spec.get("entry") == "none-broken" else [])),
            "switchyard_registry_dir": lambda: note("switchyard_registry_dir") or tmp / "registry",
            "DEFAULT_CONFIG_DIR": tmp / "config",
        }
        for n in ("_read_switchyard_release_marker", "shared_switchyard_release_for_path"):
            def reader(*args, _n=n, **kwargs):
                note(_n, *args, **kwargs)
                return saved[_n](*args, **kwargs)
            on_launcher[n] = reader
        for n in ("_looks_like_switchyard_release_tree", "_switchyard_release_source_error", "_precheck_deploy_source", "_system_unit_file_exists",
                  "postgres_cluster_script", "postgres_availability_remedy", "_database_exists", "_ticket_board_table_count", "_installed_unit_is_this_plans"):
            def sibling(*args, _n=n, **kwargs):
                note(_n, *args, **{k: v for k, v in kwargs.items() if k != "runner"})
                return saved[_n](*args, **kwargs)
            on_launcher[n] = sibling
        on_launcher.update(spec.get("launcher", {}))
        for n, f in on_launcher.items():
            setattr(t, n, f)
        if spec.get("root"):
            os.geteuid = lambda: 0
        call = spec["call"]
        try:
            if call == "precheck":
                kwargs = {"runner": runner, "port_in_use": lambda port: note("port_in_use", port) or spec.get("port", False),
                          "socket_exists": lambda path: note("socket_exists", path) or spec.get("socket", False),
                          "polkit_problems": lambda *, runner: note("polkit_problems") or list(spec.get("polkit", []))}
                for key in ("require_owner_user", "require_repository"):
                    if key in spec:
                        kwargs[key] = spec[key]
                if spec.get("dirs"):
                    kwargs.update(config_dir=tmp / "cfg", registry_dir=tmp / "reg")
                got = under["precheck_new_project"](plan, source_repo=source, repository=tmp / "repo", **kwargs)
            elif call == "remedy":
                got = under["postgres_availability_remedy"](runner=runner)
            elif call == "script":
                got = under["postgres_cluster_script"]()
            elif call == "tree":
                got = under["_looks_like_switchyard_release_tree"](tmp / "tree")
            else:
                got = under["_path_exists"](tmp / "absent")
            answer = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            answer = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"answer": answer, "calls": calls}
    finally:
        os.geteuid = saved_geteuid
        for n, f in saved.items():
            setattr(t, n, f)
        if saved_env is None:
            os.environ.pop("SWITCHYARD_SHARED_INSTALL_ROOT", None)
        else:
            os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = saved_env
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


def launcher_reads(fn: ast.AST, *, moved: bool) -> list[str]:
    """The names of MOVED a definition reads as launcher globals.

    In the launcher, a bare name is one. In a module a caller moved to (SYRD-426 moved new_project_command to
    scripts/new_project_command.py), the reads are `launcher.X` in the body and the names bound as definition-time
    defaults -- a bare name in the body there would bypass the launcher, and is not counted.
    """
    if not moved:
        return [x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and x.id in MOVED]
    defaults = [d for f in ast.walk(fn) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                for d in [*f.args.defaults, *[k for k in f.args.kw_defaults if k is not None]]]
    return ([x.attr for x in ast.walk(fn) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
             and x.value.id == "launcher" and x.attr in MOVED]
            + [x.id for d in defaults for x in ast.walk(d) if isinstance(x, ast.Name) and x.id in MOVED])


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_the_polkit_check_at_import() -> None:
    result = python("import sys, scripts.new_project_precheck as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.polkit_readiness']",
          f"it imports on its own, loading only the module its polkit default comes from and never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.new_project_precheck", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_precheck")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_precheck as m, scripts.polkit_readiness as pr; "
                        "p = inspect.signature(m.precheck_new_project).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "p['port_in_use'].default is m._tcp_port_in_use is t._tcp_port_in_use and p['socket_exists'].default is m._path_exists is t._path_exists "
                        "and p['polkit_problems'].default is pr.polkit_readiness_problems is t.polkit_readiness_problems and p['runner'].default is subprocess.run "
                        "and inspect.signature(m.postgres_availability_remedy).parameters['runner'].default is subprocess.run, "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'ProjectBoardProvision'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.socket is socket and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")
    check(m.POSTGRES_SERVICE_UNIT == "postgresql.service" and m.POSTGRES_ADMIN_SOCKET_DIR == "/var/run/postgresql", "the two constants, unchanged")
    check(m.postgres_cluster_script() == Path(t.__file__).resolve().parent / "ensure-postgres-cluster", "the cluster script is still beside the launcher")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "new_project_precheck.py").read_text(encoding="utf-8"))
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
        check(imports == (["from scripts import team_launcher as launcher"] if expected else []) and (not expected or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and nothing otherwise: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import socket", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable", "from scripts.polkit_readiness import polkit_readiness_problems"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.ticket_board.project_provision import ProjectBoardProvision"],
          f"the standard library, the polkit default's own module, and the plan type under TYPE_CHECKING: {top} {tc}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the fourteen in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_fourteen_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.new_project_precheck"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the fourteen, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_new_project_layout_payload", "_usable_switchyard_entry_for_project", "new_project_command", "switchyard_new_command",
                                        "DEFAULT_CONFIG_DIR", "switchyard_registry_dir", "TEAM_LAUNCHER_NAME", "_system_unit_is_active", "_installed_unit_path",
                                        "SWITCHYARD_RELEASE_MARKER_NAME", "_git_status_porcelain", "_read_switchyard_release_marker", "polkit_readiness_problems",
                                        "render_board_unit", "shared_switchyard_release_for_path", "uid_for_user"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    # SYRD-425 moved switchyard_new_command, which reads two of the fourteen as its defaults, to
    # scripts/switchyard_new_command.py, and SYRD-426 moved new_project_command, which reads three, to
    # scripts/new_project_command.py; their reads are still counted, where they are defined, through the launcher.
    moved_defs = []
    for moved_file in ("switchyard_new_command.py", "new_project_command.py"):
        moved_path = ROOT / "scripts" / moved_file
        moved_defs += [(fn, True) for fn in (ast.parse(moved_path.read_text(encoding="utf-8")).body if moved_path.exists() else [])]
    for fn, moved in [(fn, False) for fn in tree.body] + moved_defs:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for name in launcher_reads(fn, moved=moved):
                uses.setdefault(fn.name, {}).setdefault(name, 0)
                uses[fn.name][name] += 1
    check(uses == DISPATCH, f"the launcher's own callers read them as launcher globals, exactly as often as before: {uses}")
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(loose == [], f"and nothing at module level reads them: {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads the precheck through the launcher: {got}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def answer(label):
        return GOLDEN[label]["answer"]

    check(answer("precheck: a clean first run") is None, "a clean host passes")
    everything = answer("precheck: everything wrong at once")["message"].split("\n- ")
    check([line.split(" ")[0] for line in everything[1:5]] == ["polkit", "target", "project", "deploy"],
          f"every reason at once, in order: polkit, owner, repository, deploy source, then the board: {everything}")
    check(answer("precheck: the unit is this plan's, no database") is None and "not the one this provisioning would install" in answer("precheck: another unit, no database")["message"],
          "a half-done provisioning of this very plan may be finished, anyone else's is refused")
    for label in ("postgres not installed", "postgres inactive", "postgres active"):
        check("nothing was created" in answer(f"precheck: the database probe fails, {label}")["message"], f"{label}: the remedy is named and nothing was created")
    calls = GOLDEN["precheck: as root: the probes run as postgres"]["calls"]
    check(any(c[0] == "runner" and c[1][0][1:4] == ["-u", "postgres", "psql"] for c in calls), "as root, the database probes run as the postgres user")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads if callable(getattr(t, name, None)) and not isinstance(getattr(t, name), type)}
    check(expected <= REACHED, f"a recorder on the launcher reached every seam: missing {sorted(expected - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_the_polkit_check_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_fourteen_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"new_project_precheck_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
