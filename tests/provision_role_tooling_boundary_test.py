#!/usr/bin/env python3
"""SYRD-467: the provisioning role tooling staging and system-unit proofs, against project_provision they came out of.

The seventeen -- the shared staging directory (`tenant_control_root`,
`role_tooling_staging_dir`, `TENANT_CONTROL_ROOT_ENV`), what is staged there
(`ROLE_STAGED_EXECUTABLES`, `RETIRED_STAGED_EXECUTABLES`, the companion modules
from `entry_point_module_dependencies` and its two helpers, the Git template
with `GIT_TEMPLATE_DIR_NAME`), the commands that stage it, the check of what was
staged (`staged_role_tooling_problems`, `_release_marker_commit`) and the
system-unit proofs -- moved unchanged into
`scripts/ticket_board/provision_role_tooling.py`; `project_provision`
re-exports them all, in both branches of its import block, and keeps
`TENANT_CONTROL_ROOT`, `shell_quote`, the release and skills names,
`privileged_install` and the callers. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first, and
  `team_launcher`'s four imports reach the same ones. The module alone loads
  only its package. `entry_point_module_dependencies`' default is the very
  `ROLE_STAGED_EXECUTABLES`, bound at definition time.
- **Seams (rule 24):** what they read of `project_provision` -- each other,
  `shell_quote`, `TENANT_CONTROL_ROOT`, the marker and skills names,
  `privileged_install`, and its `__file__`, whose directory the dependencies
  are scanned from -- is read through it when they run, with the direct-script
  fallback, so a patch there reaches them.
- **Readers:** `role_account_commands`, `role_runtime_command` and
  `render_operator_commands` name them as before, and every production module
  imports them from `project_provision`, as often as before.
- **The behaviour is the baseline's:** the control-root override, the staging
  and proof commands, the imports and dependencies of a synthetic scripts tree,
  the release marker, the Git template, and the staged-bundle check against a
  synthetic release and staging directory, complete and with each defect it
  reports. `GOLDEN` below was produced by running the BASELINE module's own
  definitions over the very cases embedded here (`gold467.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME, USER
  and COLUMNS, under umask 077, under several hash seeds and with another
  TMPDIR, locale and control root in the environment.
- **The direct script stages what the package stages,** and the default,
  shaped and lean packets are byte-identical through both.

No real home, tenant, account or staging directory is read or written: every
path is synthetic or in a test-owned directory, and the staging builders only
return shell text. Spawns, every exec, signals, account and group lookups and
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

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_role_tooling as m  # noqa: E402

CHECKS = 0
MOVED = ('RETIRED_STAGED_EXECUTABLES', 'ROLE_STAGED_EXECUTABLES', '_script_sibling_modules', '_imported_names', 'entry_point_module_dependencies', 'role_tooling_staging_dir', 'TENANT_CONTROL_ROOT_ENV', 'tenant_control_root', 'readable_system_unit_path', 'system_unit_proof_commands', 'system_unit_proof_chain', '_release_marker_commit', 'staged_role_tooling_problems', 'role_tooling_staging_commands', 'GIT_TEMPLATE_DIR_NAME', 'git_template_files', 'git_template_staging_commands')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'entry_point_module_dependencies': {'__file__': 1, '_imported_names': 1, '_script_sibling_modules': 1},
    'role_tooling_staging_dir': {'tenant_control_root': 1},
    'tenant_control_root': {'TENANT_CONTROL_ROOT': 1, 'TENANT_CONTROL_ROOT_ENV': 1},
    'readable_system_unit_path': {'role_tooling_staging_dir': 1},
    'system_unit_proof_commands': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 1, 'shell_quote': 3},
    'system_unit_proof_chain': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 1, 'shell_quote': 2},
    '_release_marker_commit': {'RELEASE_MARKER_NAME': 1},
    'staged_role_tooling_problems': {'GIT_TEMPLATE_DIR_NAME': 2, 'ROLE_STAGED_EXECUTABLES': 1, 'SKILLS_DIR_NAME': 2, '_release_marker_commit': 2, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'role_tooling_staging_dir': 1},
    'role_tooling_staging_commands': {'RELEASE_MARKER_NAME': 2, 'RETIRED_STAGED_EXECUTABLES': 1, 'ROLE_STAGED_EXECUTABLES': 1, 'SKILLS_DIR_NAME': 5, 'entry_point_module_dependencies': 1, 'git_template_staging_commands': 1, 'privileged_install': 2, 'role_tooling_staging_dir': 1, 'shell_quote': 20},
    'git_template_staging_commands': {'GIT_TEMPLATE_DIR_NAME': 1, 'git_template_files': 1, 'shell_quote': 6},
}
#: Measured on the baseline: every project_provision definition outside the seventeen that names them, and how often.
DISPATCH = {'role_runtime_command': {'role_tooling_staging_commands': 1}, 'role_account_commands': {'role_tooling_staging_commands': 1}, 'render_operator_commands': {'system_unit_proof_commands': 1, 'readable_system_unit_path': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/board_services.py': {'import readable_system_unit_path': 1}, 'scripts/project_config_json.py': {'import GIT_TEMPLATE_DIR_NAME': 1}, 'scripts/role_account_migration.py': {'import role_tooling_staging_commands': 1}, 'scripts/switchyard-install-authority': {'import role_tooling_staging_dir': 2}, 'scripts/team_launcher.py': {'import ROLE_STAGED_EXECUTABLES': 1, 'import role_tooling_staging_commands': 1, 'import role_tooling_staging_dir': 1, 'import staged_role_tooling_problems': 1}, 'scripts/tenant_release_report.py': {'import system_unit_proof_chain': 1, 'import readable_system_unit_path': 1}}
#: The BASELINE's own behaviour for the cases below (`gold467.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'control root: default': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard'}, 'calls': {}},
    'control root: from the environment': {'result': {'type': 'str', 'value': '/p467/control'}, 'calls': {}},
    'control root: a blank environment': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard'}, 'calls': {}},
    'control root: the default rebound on project_provision': {'result': {'type': 'str', 'value': '/p467/rebound'}, 'calls': {}},
    'control root: the variable rebound on project_provision': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard'}, 'calls': {}},
    'staging dir: default': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard/p467'}, 'calls': {'tenant_control_root': 1}},
    'staging dir: from the environment': {'result': {'type': 'str', 'value': '/p467/control/p467'}, 'calls': {'tenant_control_root': 1}},
    'staging dir: a given root': {'result': {'type': 'str', 'value': '/p467/given/p467'}, 'calls': {}},
    'unit path: default': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard/p467/p467-ticket-board.service'}, 'calls': {'role_tooling_staging_dir': 1, 'tenant_control_root': 1}},
    'unit path: a given root': {'result': {'type': 'str', 'value': '/p467/given/p467/p467-ticket-board.service'}, 'calls': {'role_tooling_staging_dir': 1}},
    'unit path: the staging dir rebound on project_provision': {'result': {'type': 'str', 'value': '/p467/rebound/p467/p467-ticket-board.service'}, 'calls': {'role_tooling_staging_dir rebound': 1}},
    'unit proof commands': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p467'", 'if [ -f "$unit" ]; then', '    sudo install -m 0444 -o root -g root "$unit" \'/usr/local/lib/switchyard/p467/p467-ticket-board.service\'', 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/p467-ticket-board.service'", 'fi']}, 'calls': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 2, 'shell_quote': 3, 'tenant_control_root': 2}},
    'unit proof commands: a staging root': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p467/stage/p467'", 'if [ -f /p467/src.service ]; then', "    sudo install -m 0444 -o root -g root /p467/src.service '/p467/stage/p467/p467-ticket-board.service'", 'else', "    sudo rm -f '/p467/stage/p467/p467-ticket-board.service'", 'fi']}, 'calls': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 2, 'shell_quote': 3}},
    'unit proof commands: the quoting rebound on project_provision': {'result': {'type': 'list', 'value': ['sudo install -d -m 0755 -o root -g root </p467/stage/p467>', 'if [ -f /p467/src.service ]; then', '    sudo install -m 0444 -o root -g root /p467/src.service </p467/stage/p467/p467-ticket-board.service>', 'else', '    sudo rm -f </p467/stage/p467/p467-ticket-board.service>', 'fi']}, 'calls': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 2, 'shell_quote rebound': 3}},
    'unit proof chain': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p467'", "sudo install -m 0444 -o root -g root /p467/src.service '/usr/local/lib/switchyard/p467/p467-ticket-board.service'"]}, 'calls': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 2, 'shell_quote': 2, 'tenant_control_root': 2}},
    'unit proof chain: a staging root': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p467/stage/p467'", "sudo install -m 0444 -o root -g root /p467/src.service '/p467/stage/p467/p467-ticket-board.service'"]}, 'calls': {'readable_system_unit_path': 1, 'role_tooling_staging_dir': 2, 'shell_quote': 2}},
    'sibling modules': {'result': {'type': 'dict', 'value': {'helper_one': 'PATH TMP/scripts/helper_one.py', 'helper_three': 'PATH TMP/scripts/helper_three.py', 'helper_two': 'PATH TMP/scripts/helper_two.py', 'unrelated': 'PATH TMP/scripts/unrelated.py'}}, 'calls': {}},
    'imported names: every form': {'result': {'type': 'set', 'value': ['helper_one', 'helper_two', 'os']}, 'calls': {}},
    'imported names: relative and plain': {'result': {'type': 'set', 'value': ['json', 'pkg']}, 'calls': {}},
    'imported names: not Python': {'result': {'type': 'set', 'value': []}, 'calls': {}},
    'dependencies: transitive, in a synthetic tree': {'result': {'type': 'tuple', 'value': ['helper_one', 'helper_three', 'helper_two']}, 'calls': {'_imported_names': 5, '_script_sibling_modules': 1}},
    'dependencies: an entry point that is absent': {'result': {'type': 'tuple', 'value': []}, 'calls': {'_imported_names': 1, '_script_sibling_modules': 1}},
    'dependencies: none': {'result': {'type': 'tuple', 'value': []}, 'calls': {'_script_sibling_modules': 1}},
    'dependencies: the staged executables, from the scripts beside project_provision': {'result': {'type': 'tuple', 'value': ['board_skill_cli']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1}},
    "dependencies: the scan root follows project_provision's __file__": {'result': {'type': 'tuple', 'value': ['helper_one', 'helper_three', 'helper_two']}, 'calls': {'_imported_names': 4, '_script_sibling_modules': 1}},
    'dependencies: the default list is bound at definition, so a rebinding does not reach it': {'result': {'type': 'tuple', 'value': ['board_skill_cli']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1}},
    'release marker: none': {'result': {'type': 'tuple', 'value': ['', 'TMP/.switchyard-release.json does not exist']}, 'calls': {}},
    'release marker: a commit': {'result': {'type': 'tuple', 'value': ['p467c', '']}, 'calls': {}},
    'release marker: no commit': {'result': {'type': 'tuple', 'value': ['', 'TMP/.switchyard-release.json names no commit']}, 'calls': {}},
    'release marker: unreadable': {'result': {'type': 'tuple', 'value': ['', 'TMP/.switchyard-release.json could not be read: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)']}, 'calls': {}},
    'release marker: the name rebound on project_provision': {'result': {'type': 'tuple', 'value': ['p467c', '']}, 'calls': {}},
    'git template files': {'result': {'type': 'list', 'value': [['/p467/release/scripts/git_template_pre_commit', 'hooks/pre-commit', '0755'], ['/p467/release/scripts/warn_file_size_limit.py', 'hooks/warn-file-size-limit.py', '0755'], ['/p467/release/scripts/report_file_size_limit.py', 'hooks/report_file_size_limit.py', '0755'], ['/p467/release/scripts/warn_worktree_count.py', 'hooks/warn-worktree-count.py', '0755']]}, 'calls': {}},
    'git template staging': {'result': {'type': 'list', 'value': ["sudo rm -rf '/p467/stage/p467/git-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/p467/stage/p467/git-template' '/p467/stage/p467/git-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/p467/stage/p467/git-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/p467/stage/p467/git-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/p467/stage/p467/git-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/p467/stage/p467/git-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'git_template_files': 1, 'shell_quote': 12}},
    'git template staging: the directory name rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo rm -rf '/p467/stage/p467/p467-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/p467/stage/p467/p467-template' '/p467/stage/p467/p467-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/p467/stage/p467/p467-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/p467/stage/p467/p467-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/p467/stage/p467/p467-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/p467/stage/p467/p467-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'git_template_files': 1, 'shell_quote': 12}},
    'staging commands': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p467/stage/p467'", '# The bounded privileged action boundary: one root-owned helper, the', '# package it imports, and the polkit catalogue that binds them (SYRD-112).', '# Installed when the selected release carries it, removed when it does', '# not, so selecting an older release stays possible.', 'if [ -f /p467/release/scripts/ticket_board/privileged_helper.py ]; then', '    sudo install -d -m 0755 -o root -g root /p467/stage', '    sudo install -m 0755 -o root -g root /p467/release/scripts/ticket_board/privileged_helper.py /p467/stage/switchyard-privileged-helper', '    sudo rm -rf /p467/stage/ticket_board', '    sudo cp -a /p467/release/scripts/ticket_board /p467/stage/ticket_board', '    sudo chown -R root:root /p467/stage/ticket_board', '    sudo chmod -R a+rX,go-w /p467/stage/ticket_board', '    sudo install -d -m 0755 -o root -g root /p467/stage/polkit-actions', 'TEXT 5282 chars sha256:f54fa5e87d3b5625', 'else', '    sudo rm -f /p467/stage/switchyard-privileged-helper /p467/stage/polkit-actions/org.switchyard.privileged.policy', '    sudo rm -rf /p467/stage/ticket_board', 'fi', "if [ -f '/p467/release/scripts/ticket-board-pane-idle-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-pane-idle-hook' '/p467/stage/p467/ticket-board-pane-idle-hook'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-pane-idle-hook'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-install-pane-hooks' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-install-pane-hooks' '/p467/stage/p467/ticket-board-install-pane-hooks'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-install-pane-hooks'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-claude-permission-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-claude-permission-hook' '/p467/stage/p467/ticket-board-claude-permission-hook'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-claude-permission-hook'", 'fi', "if [ -f '/p467/release/scripts/switchyard-board-skill' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-board-skill' '/p467/stage/p467/switchyard-board-skill'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-board-skill'", 'fi', "if [ -f '/p467/release/scripts/switchyard-publish-candidate' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-publish-candidate' '/p467/stage/p467/switchyard-publish-candidate'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-publish-candidate'", 'fi', "if [ -f '/p467/release/scripts/switchyard-request-publication' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-request-publication' '/p467/stage/p467/switchyard-request-publication'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-request-publication'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-register-runtime' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-register-runtime' '/p467/stage/p467/ticket-board-register-runtime'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-register-runtime'", 'fi', "if [ -f '/p467/release/scripts/switchyard-tenant-control' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-tenant-control' '/p467/stage/p467/switchyard-tenant-control'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-tenant-control'", 'fi', "if [ -f '/p467/release/scripts/switchyard-display-attach' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-display-attach' '/p467/stage/p467/switchyard-display-attach'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-display-attach'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-write' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-write' '/p467/stage/p467/ticket-board-write'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-write'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-read' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-read' '/p467/stage/p467/ticket-board-read'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-read'", 'fi', "if [ -f '/p467/release/scripts/directorctl' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/directorctl' '/p467/stage/p467/directorctl'", 'else', "    sudo rm -f '/p467/stage/p467/directorctl'", 'fi', "sudo rm -f '/p467/stage/p467/switchyard-publish-ref'", "sudo rm -f '/p467/stage/p467/switchyard-publish'", "sudo rm -f '/p467/stage/p467/switchyard-integrate-main'", "sudo rm -f '/p467/stage/p467/switchyard-integrate'", "sudo rm -f '/p467/stage/p467/switchyard_publication_authority.py'", "if [ -f '/p467/release/scripts/board_skill_cli.py' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/scripts/board_skill_cli.py' '/p467/stage/p467/board_skill_cli.py'", 'else', "    sudo rm -f '/p467/stage/p467/board_skill_cli.py'", 'fi', "sudo rm -rf '/p467/stage/p467/ticket_board'", "sudo cp -a '/p467/release/scripts/ticket_board' '/p467/stage/p467/ticket_board'", "sudo chown -R root:root '/p467/stage/p467/ticket_board'", "sudo chmod -R a+rX '/p467/stage/p467/ticket_board'", "sudo rm -rf '/p467/stage/p467/skills'", "sudo cp -a '/p467/release/skills' '/p467/stage/p467/skills'", "sudo chown -R root:root '/p467/stage/p467/skills'", "sudo chmod -R a+rX '/p467/stage/p467/skills'", "if [ -f '/p467/release/.switchyard-release.json' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/.switchyard-release.json' '/p467/stage/p467/.switchyard-release.json'", 'else', "    sudo rm -f '/p467/stage/p467/.switchyard-release.json'", 'fi', "sudo rm -rf '/p467/stage/p467/git-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/p467/stage/p467/git-template' '/p467/stage/p467/git-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/p467/stage/p467/git-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/p467/stage/p467/git-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/p467/stage/p467/git-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/p467/stage/p467/git-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'git_template_staging_commands': 1, 'role_tooling_staging_dir': 1, 'shell_quote': 84}},
    'staging commands: the default staging root': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p467'", '# The bounded privileged action boundary: one root-owned helper, the', '# package it imports, and the polkit catalogue that binds them (SYRD-112).', '# Installed when the selected release carries it, removed when it does', '# not, so selecting an older release stays possible.', 'if [ -f /p467/release/scripts/ticket_board/privileged_helper.py ]; then', '    sudo install -d -m 0755 -o root -g root /usr/local/lib/switchyard', '    sudo install -m 0755 -o root -g root /p467/release/scripts/ticket_board/privileged_helper.py /usr/local/lib/switchyard/switchyard-privileged-helper', '    sudo rm -rf /usr/local/lib/switchyard/ticket_board', '    sudo cp -a /p467/release/scripts/ticket_board /usr/local/lib/switchyard/ticket_board', '    sudo chown -R root:root /usr/local/lib/switchyard/ticket_board', '    sudo chmod -R a+rX,go-w /usr/local/lib/switchyard/ticket_board', '    sudo install -d -m 0755 -o root -g root /usr/share/polkit-1/actions', 'TEXT 5381 chars sha256:3a72231d2b656583', 'else', '    sudo rm -f /usr/local/lib/switchyard/switchyard-privileged-helper /usr/share/polkit-1/actions/org.switchyard.privileged.policy', '    sudo rm -rf /usr/local/lib/switchyard/ticket_board', 'fi', "if [ -f '/p467/release/scripts/ticket-board-pane-idle-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-pane-idle-hook' '/usr/local/lib/switchyard/p467/ticket-board-pane-idle-hook'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/ticket-board-pane-idle-hook'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-install-pane-hooks' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-install-pane-hooks' '/usr/local/lib/switchyard/p467/ticket-board-install-pane-hooks'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/ticket-board-install-pane-hooks'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-claude-permission-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-claude-permission-hook' '/usr/local/lib/switchyard/p467/ticket-board-claude-permission-hook'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/ticket-board-claude-permission-hook'", 'fi', "if [ -f '/p467/release/scripts/switchyard-board-skill' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-board-skill' '/usr/local/lib/switchyard/p467/switchyard-board-skill'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-board-skill'", 'fi', "if [ -f '/p467/release/scripts/switchyard-publish-candidate' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-publish-candidate' '/usr/local/lib/switchyard/p467/switchyard-publish-candidate'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-publish-candidate'", 'fi', "if [ -f '/p467/release/scripts/switchyard-request-publication' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-request-publication' '/usr/local/lib/switchyard/p467/switchyard-request-publication'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-request-publication'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-register-runtime' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-register-runtime' '/usr/local/lib/switchyard/p467/ticket-board-register-runtime'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/ticket-board-register-runtime'", 'fi', "if [ -f '/p467/release/scripts/switchyard-tenant-control' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-tenant-control' '/usr/local/lib/switchyard/p467/switchyard-tenant-control'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-tenant-control'", 'fi', "if [ -f '/p467/release/scripts/switchyard-display-attach' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-display-attach' '/usr/local/lib/switchyard/p467/switchyard-display-attach'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-display-attach'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-write' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-write' '/usr/local/lib/switchyard/p467/ticket-board-write'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/ticket-board-write'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-read' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-read' '/usr/local/lib/switchyard/p467/ticket-board-read'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/ticket-board-read'", 'fi', "if [ -f '/p467/release/scripts/directorctl' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/directorctl' '/usr/local/lib/switchyard/p467/directorctl'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/directorctl'", 'fi', "sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-publish-ref'", "sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-publish'", "sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-integrate-main'", "sudo rm -f '/usr/local/lib/switchyard/p467/switchyard-integrate'", "sudo rm -f '/usr/local/lib/switchyard/p467/switchyard_publication_authority.py'", "if [ -f '/p467/release/scripts/board_skill_cli.py' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/scripts/board_skill_cli.py' '/usr/local/lib/switchyard/p467/board_skill_cli.py'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/board_skill_cli.py'", 'fi', "sudo rm -rf '/usr/local/lib/switchyard/p467/ticket_board'", "sudo cp -a '/p467/release/scripts/ticket_board' '/usr/local/lib/switchyard/p467/ticket_board'", "sudo chown -R root:root '/usr/local/lib/switchyard/p467/ticket_board'", "sudo chmod -R a+rX '/usr/local/lib/switchyard/p467/ticket_board'", "sudo rm -rf '/usr/local/lib/switchyard/p467/skills'", "sudo cp -a '/p467/release/skills' '/usr/local/lib/switchyard/p467/skills'", "sudo chown -R root:root '/usr/local/lib/switchyard/p467/skills'", "sudo chmod -R a+rX '/usr/local/lib/switchyard/p467/skills'", "if [ -f '/p467/release/.switchyard-release.json' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/.switchyard-release.json' '/usr/local/lib/switchyard/p467/.switchyard-release.json'", 'else', "    sudo rm -f '/usr/local/lib/switchyard/p467/.switchyard-release.json'", 'fi', "sudo rm -rf '/usr/local/lib/switchyard/p467/git-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p467/git-template' '/usr/local/lib/switchyard/p467/git-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/usr/local/lib/switchyard/p467/git-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/usr/local/lib/switchyard/p467/git-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/usr/local/lib/switchyard/p467/git-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/usr/local/lib/switchyard/p467/git-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'git_template_staging_commands': 1, 'role_tooling_staging_dir': 1, 'shell_quote': 84, 'tenant_control_root': 1}},
    'staging commands: the lists rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p467/stage/p467'", '# The bounded privileged action boundary: one root-owned helper, the', '# package it imports, and the polkit catalogue that binds them (SYRD-112).', '# Installed when the selected release carries it, removed when it does', '# not, so selecting an older release stays possible.', 'if [ -f /p467/release/scripts/ticket_board/privileged_helper.py ]; then', '    sudo install -d -m 0755 -o root -g root /p467/stage', '    sudo install -m 0755 -o root -g root /p467/release/scripts/ticket_board/privileged_helper.py /p467/stage/switchyard-privileged-helper', '    sudo rm -rf /p467/stage/ticket_board', '    sudo cp -a /p467/release/scripts/ticket_board /p467/stage/ticket_board', '    sudo chown -R root:root /p467/stage/ticket_board', '    sudo chmod -R a+rX,go-w /p467/stage/ticket_board', '    sudo install -d -m 0755 -o root -g root /p467/stage/polkit-actions', 'TEXT 5282 chars sha256:f54fa5e87d3b5625', 'else', '    sudo rm -f /p467/stage/switchyard-privileged-helper /p467/stage/polkit-actions/org.switchyard.privileged.policy', '    sudo rm -rf /p467/stage/ticket_board', 'fi', "if [ -f '/p467/release/scripts/p467-tool' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/p467-tool' '/p467/stage/p467/p467-tool'", 'else', "    sudo rm -f '/p467/stage/p467/p467-tool'", 'fi', "sudo rm -f '/p467/stage/p467/p467-old'", "if [ -f '/p467/release/scripts/board_skill_cli.py' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/scripts/board_skill_cli.py' '/p467/stage/p467/board_skill_cli.py'", 'else', "    sudo rm -f '/p467/stage/p467/board_skill_cli.py'", 'fi', "sudo rm -rf '/p467/stage/p467/ticket_board'", "sudo cp -a '/p467/release/scripts/ticket_board' '/p467/stage/p467/ticket_board'", "sudo chown -R root:root '/p467/stage/p467/ticket_board'", "sudo chmod -R a+rX '/p467/stage/p467/ticket_board'", "sudo rm -rf '/p467/stage/p467/skills'", "sudo cp -a '/p467/release/skills' '/p467/stage/p467/skills'", "sudo chown -R root:root '/p467/stage/p467/skills'", "sudo chmod -R a+rX '/p467/stage/p467/skills'", "if [ -f '/p467/release/.switchyard-release.json' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/.switchyard-release.json' '/p467/stage/p467/.switchyard-release.json'", 'else', "    sudo rm -f '/p467/stage/p467/.switchyard-release.json'", 'fi', "sudo rm -rf '/p467/stage/p467/git-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/p467/stage/p467/git-template' '/p467/stage/p467/git-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/p467/stage/p467/git-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/p467/stage/p467/git-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/p467/stage/p467/git-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/p467/stage/p467/git-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'git_template_staging_commands': 1, 'role_tooling_staging_dir': 1, 'shell_quote': 36}},
    'staging commands: the skills and marker names rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p467/stage/p467'", '# The bounded privileged action boundary: one root-owned helper, the', '# package it imports, and the polkit catalogue that binds them (SYRD-112).', '# Installed when the selected release carries it, removed when it does', '# not, so selecting an older release stays possible.', 'if [ -f /p467/release/scripts/ticket_board/privileged_helper.py ]; then', '    sudo install -d -m 0755 -o root -g root /p467/stage', '    sudo install -m 0755 -o root -g root /p467/release/scripts/ticket_board/privileged_helper.py /p467/stage/switchyard-privileged-helper', '    sudo rm -rf /p467/stage/ticket_board', '    sudo cp -a /p467/release/scripts/ticket_board /p467/stage/ticket_board', '    sudo chown -R root:root /p467/stage/ticket_board', '    sudo chmod -R a+rX,go-w /p467/stage/ticket_board', '    sudo install -d -m 0755 -o root -g root /p467/stage/polkit-actions', 'TEXT 5282 chars sha256:f54fa5e87d3b5625', 'else', '    sudo rm -f /p467/stage/switchyard-privileged-helper /p467/stage/polkit-actions/org.switchyard.privileged.policy', '    sudo rm -rf /p467/stage/ticket_board', 'fi', "if [ -f '/p467/release/scripts/ticket-board-pane-idle-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-pane-idle-hook' '/p467/stage/p467/ticket-board-pane-idle-hook'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-pane-idle-hook'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-install-pane-hooks' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-install-pane-hooks' '/p467/stage/p467/ticket-board-install-pane-hooks'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-install-pane-hooks'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-claude-permission-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-claude-permission-hook' '/p467/stage/p467/ticket-board-claude-permission-hook'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-claude-permission-hook'", 'fi', "if [ -f '/p467/release/scripts/switchyard-board-skill' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-board-skill' '/p467/stage/p467/switchyard-board-skill'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-board-skill'", 'fi', "if [ -f '/p467/release/scripts/switchyard-publish-candidate' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-publish-candidate' '/p467/stage/p467/switchyard-publish-candidate'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-publish-candidate'", 'fi', "if [ -f '/p467/release/scripts/switchyard-request-publication' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-request-publication' '/p467/stage/p467/switchyard-request-publication'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-request-publication'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-register-runtime' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-register-runtime' '/p467/stage/p467/ticket-board-register-runtime'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-register-runtime'", 'fi', "if [ -f '/p467/release/scripts/switchyard-tenant-control' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-tenant-control' '/p467/stage/p467/switchyard-tenant-control'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-tenant-control'", 'fi', "if [ -f '/p467/release/scripts/switchyard-display-attach' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-display-attach' '/p467/stage/p467/switchyard-display-attach'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-display-attach'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-write' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-write' '/p467/stage/p467/ticket-board-write'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-write'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-read' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-read' '/p467/stage/p467/ticket-board-read'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-read'", 'fi', "if [ -f '/p467/release/scripts/directorctl' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/directorctl' '/p467/stage/p467/directorctl'", 'else', "    sudo rm -f '/p467/stage/p467/directorctl'", 'fi', "sudo rm -f '/p467/stage/p467/switchyard-publish-ref'", "sudo rm -f '/p467/stage/p467/switchyard-publish'", "sudo rm -f '/p467/stage/p467/switchyard-integrate-main'", "sudo rm -f '/p467/stage/p467/switchyard-integrate'", "sudo rm -f '/p467/stage/p467/switchyard_publication_authority.py'", "if [ -f '/p467/release/scripts/board_skill_cli.py' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/scripts/board_skill_cli.py' '/p467/stage/p467/board_skill_cli.py'", 'else', "    sudo rm -f '/p467/stage/p467/board_skill_cli.py'", 'fi', "sudo rm -rf '/p467/stage/p467/ticket_board'", "sudo cp -a '/p467/release/scripts/ticket_board' '/p467/stage/p467/ticket_board'", "sudo chown -R root:root '/p467/stage/p467/ticket_board'", "sudo chmod -R a+rX '/p467/stage/p467/ticket_board'", "sudo rm -rf '/p467/stage/p467/p467-skills'", "sudo cp -a '/p467/release/p467-skills' '/p467/stage/p467/p467-skills'", "sudo chown -R root:root '/p467/stage/p467/p467-skills'", "sudo chmod -R a+rX '/p467/stage/p467/p467-skills'", "if [ -f '/p467/release/p467-marker.json' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/p467-marker.json' '/p467/stage/p467/p467-marker.json'", 'else', "    sudo rm -f '/p467/stage/p467/p467-marker.json'", 'fi', "sudo rm -rf '/p467/stage/p467/git-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/p467/stage/p467/git-template' '/p467/stage/p467/git-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/p467/stage/p467/git-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/p467/stage/p467/git-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/p467/stage/p467/git-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/p467/stage/p467/git-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'git_template_staging_commands': 1, 'role_tooling_staging_dir': 1, 'shell_quote': 84}},
    'staging commands: the privileged installer rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o root -g root '/p467/stage/p467'", '# p467 privileged install', "if [ -f '/p467/release/scripts/ticket-board-pane-idle-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-pane-idle-hook' '/p467/stage/p467/ticket-board-pane-idle-hook'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-pane-idle-hook'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-install-pane-hooks' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-install-pane-hooks' '/p467/stage/p467/ticket-board-install-pane-hooks'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-install-pane-hooks'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-claude-permission-hook' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-claude-permission-hook' '/p467/stage/p467/ticket-board-claude-permission-hook'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-claude-permission-hook'", 'fi', "if [ -f '/p467/release/scripts/switchyard-board-skill' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-board-skill' '/p467/stage/p467/switchyard-board-skill'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-board-skill'", 'fi', "if [ -f '/p467/release/scripts/switchyard-publish-candidate' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-publish-candidate' '/p467/stage/p467/switchyard-publish-candidate'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-publish-candidate'", 'fi', "if [ -f '/p467/release/scripts/switchyard-request-publication' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-request-publication' '/p467/stage/p467/switchyard-request-publication'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-request-publication'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-register-runtime' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-register-runtime' '/p467/stage/p467/ticket-board-register-runtime'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-register-runtime'", 'fi', "if [ -f '/p467/release/scripts/switchyard-tenant-control' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-tenant-control' '/p467/stage/p467/switchyard-tenant-control'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-tenant-control'", 'fi', "if [ -f '/p467/release/scripts/switchyard-display-attach' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/switchyard-display-attach' '/p467/stage/p467/switchyard-display-attach'", 'else', "    sudo rm -f '/p467/stage/p467/switchyard-display-attach'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-write' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-write' '/p467/stage/p467/ticket-board-write'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-write'", 'fi', "if [ -f '/p467/release/scripts/ticket-board-read' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/ticket-board-read' '/p467/stage/p467/ticket-board-read'", 'else', "    sudo rm -f '/p467/stage/p467/ticket-board-read'", 'fi', "if [ -f '/p467/release/scripts/directorctl' ]; then", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/directorctl' '/p467/stage/p467/directorctl'", 'else', "    sudo rm -f '/p467/stage/p467/directorctl'", 'fi', "sudo rm -f '/p467/stage/p467/switchyard-publish-ref'", "sudo rm -f '/p467/stage/p467/switchyard-publish'", "sudo rm -f '/p467/stage/p467/switchyard-integrate-main'", "sudo rm -f '/p467/stage/p467/switchyard-integrate'", "sudo rm -f '/p467/stage/p467/switchyard_publication_authority.py'", "if [ -f '/p467/release/scripts/board_skill_cli.py' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/scripts/board_skill_cli.py' '/p467/stage/p467/board_skill_cli.py'", 'else', "    sudo rm -f '/p467/stage/p467/board_skill_cli.py'", 'fi', "sudo rm -rf '/p467/stage/p467/ticket_board'", "sudo cp -a '/p467/release/scripts/ticket_board' '/p467/stage/p467/ticket_board'", "sudo chown -R root:root '/p467/stage/p467/ticket_board'", "sudo chmod -R a+rX '/p467/stage/p467/ticket_board'", "sudo rm -rf '/p467/stage/p467/skills'", "sudo cp -a '/p467/release/skills' '/p467/stage/p467/skills'", "sudo chown -R root:root '/p467/stage/p467/skills'", "sudo chmod -R a+rX '/p467/stage/p467/skills'", "if [ -f '/p467/release/.switchyard-release.json' ]; then", "    sudo install -m 0644 -o root -g root '/p467/release/.switchyard-release.json' '/p467/stage/p467/.switchyard-release.json'", 'else', "    sudo rm -f '/p467/stage/p467/.switchyard-release.json'", 'fi', "sudo rm -rf '/p467/stage/p467/git-template'", "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then", "    sudo install -d -m 0755 -o root -g root '/p467/stage/p467/git-template' '/p467/stage/p467/git-template/hooks'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/git_template_pre_commit' '/p467/stage/p467/git-template/hooks/pre-commit'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_file_size_limit.py' '/p467/stage/p467/git-template/hooks/warn-file-size-limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/report_file_size_limit.py' '/p467/stage/p467/git-template/hooks/report_file_size_limit.py'", "    sudo install -m 0755 -o root -g root '/p467/release/scripts/warn_worktree_count.py' '/p467/stage/p467/git-template/hooks/warn-worktree-count.py'", 'fi']}, 'calls': {'_imported_names': 13, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'git_template_staging_commands': 1, 'privileged_install rebound': 2, 'role_tooling_staging_dir': 1, 'shell_quote': 84}},
    'staged problems: nothing staged': {'result': {'type': 'list', 'value': ['ticket-board-write is not staged at TMP/stage/ticket-board-write', 'the companion module helper_one is not staged at TMP/stage/helper_one.py', 'the ticket_board package is not staged at TMP/stage/ticket_board', 'the canonical skills tree is not staged at TMP/stage/skills', 'the staged bundle names release TMP/stage/.switchyard-release.json does not exist and this one is p467c']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: an empty staging dir': {'result': {'type': 'list', 'value': ['ticket-board-write is not staged at TMP/stage/ticket-board-write', 'the companion module helper_one is not staged at TMP/stage/helper_one.py', 'the ticket_board package is not staged at TMP/stage/ticket_board', 'the canonical skills tree is not staged at TMP/stage/skills', 'the staged bundle names release TMP/stage/.switchyard-release.json does not exist and this one is p467c']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: complete': {'result': {'type': 'list', 'value': []}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: a group-writable executable': {'result': {'type': 'list', 'value': ['TMP/stage/ticket-board-write is group- or world-writable']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: an executable that is not executable': {'result': {'type': 'list', 'value': ['TMP/stage/ticket-board-write is not executable']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: a symlinked companion module': {'result': {'type': 'list', 'value': ['TMP/stage/helper_one.py is not a regular file']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: a stray executable': {'result': {'type': 'list', 'value': ['TMP/stage/directorctl is staged but directorctl is not in this release; the staged bundle is not the one this release would install']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: a stray Git template': {'result': {'type': 'list', 'value': ['TMP/stage/git-template is staged but the Git template is not in this release; the staged bundle is not the one this release would install']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    "staged problems: another release's marker": {'result': {'type': 'list', 'value': ['the staged bundle names release p467other and this one is p467c']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: an empty skills tree': {'result': {'type': 'list', 'value': ['TMP/stage/skills is empty']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: a release without a marker': {'result': {'type': 'list', 'value': ['the staged bundle names release p467c, which this source does not (TMP/rel/.switchyard-release.json does not exist)']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: another owner expected': {'result': {'type': 'list', 'value': ['TMP/stage/ticket-board-write is owned by uid OWN rather than by uid OTHER', 'TMP/stage/helper_one.py is owned by uid OWN rather than by uid OTHER']}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1}},
    'staged problems: the default staging dir, through project_provision': {'result': {'type': 'list', 'value': []}, 'calls': {'_imported_names': 13, '_release_marker_commit': 2, '_script_sibling_modules': 1, 'entry_point_module_dependencies': 1, 'git_template_files': 1, 'role_tooling_staging_dir rebound': 1}},
    'constants': {'result': {'type': 'list', 'value': [['ticket-board-pane-idle-hook', 'ticket-board-install-pane-hooks', 'ticket-board-claude-permission-hook', 'switchyard-board-skill', 'switchyard-publish-candidate', 'switchyard-request-publication', 'ticket-board-register-runtime', 'switchyard-tenant-control', 'switchyard-display-attach', 'ticket-board-write', 'ticket-board-read', 'directorctl'], ['switchyard-publish-ref', 'switchyard-publish', 'switchyard-integrate-main', 'switchyard-integrate', 'switchyard_publication_authority.py'], 'git-template', 'SWITCHYARD_TENANT_CONTROL_ROOT']}, 'calls': {}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = 10

# --- the cases, shared verbatim with `gold467.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the thirteen and records the answer or the exact exception, and how many times it called each of
# `project_provision`'s helpers (recorded there and passed through). Projects, release roots and staging roots are
# synthetic (/p467/...); a case that must read files gets a synthetic tree in a test-owned directory (TMP). Nothing is
# run -- the staging builders only return shell text. A case may rebind a `project_provision` name, its own __file__
# included, to show it is read there when the function runs.
TREE = {
    "scripts/tool-a": "#!/usr/bin/env python3\nimport os\nfrom helper_one import thing\nimport helper_two as h2\n",
    "scripts/tool-b": "#!/usr/bin/env python3\nfrom . import nothing\nfrom .rel_mod import thing\nimport json\nimport pkg.sub as s\n",
    "scripts/helper_one.py": "import helper_three\nfrom os import path\n",
    "scripts/helper_two.py": "x = 1\n",
    "scripts/helper_three.py": "import helper_one\n",
    "scripts/unrelated.py": "import helper_two\n",
    "scripts/ticket_board/project_provision.py": "# the rebound __file__ points here\n",
}
CASES = {
    "control root: default": {"call": "tenant_control_root", "env": None},
    "control root: from the environment": {"call": "tenant_control_root", "env": "/p467/control"},
    "control root: a blank environment": {"call": "tenant_control_root", "env": "   "},
    "control root: the default rebound on project_provision": {"call": "tenant_control_root", "env": None, "rebind": {"TENANT_CONTROL_ROOT": "/p467/rebound"}},
    "control root: the variable rebound on project_provision": {"call": "tenant_control_root", "env": "/p467/control", "rebind": {"TENANT_CONTROL_ROOT_ENV": "P467_UNSET"}},
    "staging dir: default": {"call": "role_tooling_staging_dir", "args": ["p467"], "env": None},
    "staging dir: from the environment": {"call": "role_tooling_staging_dir", "args": ["p467"], "env": "/p467/control"},
    "staging dir: a given root": {"call": "role_tooling_staging_dir", "args": ["p467"], "kwargs": {"root": "/p467/given"}},
    "unit path: default": {"call": "readable_system_unit_path", "args": ["p467"], "env": None},
    "unit path: a given root": {"call": "readable_system_unit_path", "args": ["p467"], "kwargs": {"root": "/p467/given"}},
    "unit path: the staging dir rebound on project_provision": {"call": "readable_system_unit_path", "args": ["p467"], "rebind": {"role_tooling_staging_dir": True}},
    "unit proof commands": {"call": "system_unit_proof_commands", "args": ["p467", '"$unit"'], "env": None},
    "unit proof commands: a staging root": {"call": "system_unit_proof_commands", "args": ["p467", "/p467/src.service"], "kwargs": {"staging_root": "/p467/stage"}},
    "unit proof commands: the quoting rebound on project_provision": {"call": "system_unit_proof_commands", "args": ["p467", "/p467/src.service"], "kwargs": {"staging_root": "/p467/stage"},
                                                                      "rebind": {"shell_quote": True}},
    "unit proof chain": {"call": "system_unit_proof_chain", "args": ["p467", "/p467/src.service"], "env": None},
    "unit proof chain: a staging root": {"call": "system_unit_proof_chain", "args": ["p467", "/p467/src.service"], "kwargs": {"staging_root": "/p467/stage"}},
    "sibling modules": {"call": "_script_sibling_modules", "tree": True, "args": ["TMP/scripts"]},
    "imported names: every form": {"call": "_imported_names", "tree": True, "args": ["TMP/scripts/tool-a"]},
    "imported names: relative and plain": {"call": "_imported_names", "tree": True, "args": ["TMP/scripts/tool-b"]},
    "imported names: not Python": {"call": "_imported_names", "tree": "broken", "args": ["TMP/scripts/tool-a"]},
    "dependencies: transitive, in a synthetic tree": {"call": "entry_point_module_dependencies", "tree": True, "args": [["tool-a", "tool-b"]], "kwargs": {"source_root": "TMP/scripts"}},
    "dependencies: an entry point that is absent": {"call": "entry_point_module_dependencies", "tree": True, "args": [["tool-missing"]], "kwargs": {"source_root": "TMP/scripts"}},
    "dependencies: none": {"call": "entry_point_module_dependencies", "tree": True, "args": [[]], "kwargs": {"source_root": "TMP/scripts"}},
    "dependencies: the staged executables, from the scripts beside project_provision": {"call": "entry_point_module_dependencies"},
    "dependencies: the scan root follows project_provision's __file__": {"call": "entry_point_module_dependencies", "tree": True, "args": [["tool-a"]], "rebind": {"__file__": True}},
    "dependencies: the default list is bound at definition, so a rebinding does not reach it": {"call": "entry_point_module_dependencies", "rebind": {"ROLE_STAGED_EXECUTABLES": ("tool-a",)}},
    "release marker: none": {"call": "_release_marker_commit", "tree": True, "args": ["TMP"]},
    "release marker: a commit": {"call": "_release_marker_commit", "tree": True, "marker": '{"commit": " p467c ", "source_ref": "p467"}', "args": ["TMP"]},
    "release marker: no commit": {"call": "_release_marker_commit", "tree": True, "marker": '{"source_ref": "p467"}', "args": ["TMP"]},
    "release marker: unreadable": {"call": "_release_marker_commit", "tree": True, "marker": "{not json", "args": ["TMP"]},
    "release marker: the name rebound on project_provision": {"call": "_release_marker_commit", "tree": True, "marker": '{"commit": "p467c"}', "args": ["TMP"],
                                                              "rebind": {"RELEASE_MARKER_NAME": "p467-marker.json"}},
    "git template files": {"call": "git_template_files", "args": ["/p467/release"]},
    "git template staging": {"call": "git_template_staging_commands", "args": ["/p467/release", "/p467/stage/p467"]},
    "git template staging: the directory name rebound on project_provision": {"call": "git_template_staging_commands", "args": ["/p467/release", "/p467/stage/p467"],
                                                                              "rebind": {"GIT_TEMPLATE_DIR_NAME": "p467-template"}},
    "staging commands": {"call": "role_tooling_staging_commands", "args": ["p467", "/p467/release"], "kwargs": {"staging_root": "/p467/stage"}},
    "staging commands: the default staging root": {"call": "role_tooling_staging_commands", "args": ["p467", "/p467/release"], "env": None},
    "staging commands: the lists rebound on project_provision": {"call": "role_tooling_staging_commands", "args": ["p467", "/p467/release"], "kwargs": {"staging_root": "/p467/stage"},
                                                                 "rebind": {"ROLE_STAGED_EXECUTABLES": ("p467-tool",), "RETIRED_STAGED_EXECUTABLES": ("p467-old",)}},
    "staging commands: the skills and marker names rebound on project_provision": {"call": "role_tooling_staging_commands", "args": ["p467", "/p467/release"],
                                                                                    "kwargs": {"staging_root": "/p467/stage"},
                                                                                    "rebind": {"SKILLS_DIR_NAME": "p467-skills", "RELEASE_MARKER_NAME": "p467-marker.json"}},
    "staging commands: the privileged installer rebound on project_provision": {"call": "role_tooling_staging_commands", "args": ["p467", "/p467/release"],
                                                                                 "kwargs": {"staging_root": "/p467/stage"}, "rebind": {"privileged_install": True}},
    "staged problems: nothing staged": {"call": "staged_role_tooling_problems", "stage": "absent"},
    "staged problems: an empty staging dir": {"call": "staged_role_tooling_problems", "stage": "empty"},
    **{f"staged problems: {k}": {"call": "staged_role_tooling_problems", "stage": k} for k in (
        "complete", "a group-writable executable", "an executable that is not executable", "a symlinked companion module", "a stray executable",
        "a stray Git template", "another release's marker", "an empty skills tree", "a release without a marker")},
    "staged problems: another owner expected": {"call": "staged_role_tooling_problems", "stage": "complete", "kwargs": {"expect_uid": "OTHER"}},
    "staged problems: the default staging dir, through project_provision": {"call": "staged_role_tooling_problems", "stage": "complete", "default_staging": True,
                                                                            "rebind": {"role_tooling_staging_dir": True}},
    "constants": {"call": None},
}
FUNCTIONS = ("_script_sibling_modules", "_imported_names", "entry_point_module_dependencies", "role_tooling_staging_dir", "tenant_control_root", "readable_system_unit_path",
             "system_unit_proof_commands", "system_unit_proof_chain", "_release_marker_commit", "staged_role_tooling_problems", "role_tooling_staging_commands",
             "git_template_files", "git_template_staging_commands")
PASSED = ("_script_sibling_modules", "_imported_names", "entry_point_module_dependencies", "role_tooling_staging_dir", "tenant_control_root", "readable_system_unit_path",
          "_release_marker_commit", "git_template_files", "git_template_staging_commands", "shell_quote")
REBINDABLE = ("TENANT_CONTROL_ROOT", "TENANT_CONTROL_ROOT_ENV", "ROLE_STAGED_EXECUTABLES", "RETIRED_STAGED_EXECUTABLES", "GIT_TEMPLATE_DIR_NAME", "RELEASE_MARKER_NAME",
              "SKILLS_DIR_NAME", "privileged_install", "__file__")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import hashlib, os as _os, shutil, tempfile, types
    from pathlib import Path as _P
    counts: dict = {}
    tmp = _P(tempfile.mkdtemp(prefix="syrd467-")).resolve()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(norm(v) for v in value)
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            value = value.replace(str(tmp), "TMP").replace(str(_P(saved["__file__"]).resolve().parents[1]), "SCRIPTS")
            value = value.replace(f"uid {_os.getuid() + 1}", "uid OTHER").replace(f"uid {_os.getuid()}", "uid OWN")
            return value if len(value) <= 400 else f"TEXT {len(value)} chars sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE)}
    env_saved = _os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT")
    try:
        if spec.get("tree"):
            for rel, text in TREE.items():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_text(text, encoding="utf-8")
            (tmp / "release").mkdir()
            if spec["tree"] == "broken":
                (tmp / "scripts" / "tool-a").write_text("def (:\n", encoding="utf-8")
            if spec["tree"] == "staged-empty":
                (tmp / "stage" / "p467").mkdir(parents=True)
        if "stage" in spec:
            # A synthetic release carrying one staged executable and the companion module it imports, and a staging
            # directory made from it -- complete, or with the one defect the case names.
            rel, stage, kind = tmp / "rel", tmp / "stage", spec["stage"]
            (rel / "scripts").mkdir(parents=True)
            (rel / "scripts" / "ticket-board-write").write_text("#!/usr/bin/env python3\nimport helper_one\n", encoding="utf-8")
            (rel / "scripts" / "helper_one.py").write_text("x = 1\n", encoding="utf-8")
            if kind != "a release without a marker":
                (rel / saved["RELEASE_MARKER_NAME"]).write_text('{"commit": "p467c"}', encoding="utf-8")
            if kind != "absent":
                stage.mkdir()
            if kind not in ("absent", "empty"):
                for rel_path, text, mode in (("ticket-board-write", "#!/bin/sh\n", 0o755), ("helper_one.py", "x = 1\n", 0o644),
                                             ("ticket_board/__init__.py", "", 0o644), (f"{saved['SKILLS_DIR_NAME']}/s.md", "s\n", 0o644)):
                    (stage / rel_path).parent.mkdir(parents=True, exist_ok=True)
                    (stage / rel_path).write_text(text, encoding="utf-8")
                    _os.chmod(stage / rel_path, mode)
                (stage / saved["RELEASE_MARKER_NAME"]).write_text('{"commit": "p467c"}' if kind != "another release's marker" else '{"commit": "p467other"}', encoding="utf-8")
                if kind == "a group-writable executable":
                    _os.chmod(stage / "ticket-board-write", 0o775)
                if kind == "an executable that is not executable":
                    _os.chmod(stage / "ticket-board-write", 0o644)
                if kind == "a symlinked companion module":
                    (stage / "helper_one.py").unlink()
                    (stage / "helper_one.py").symlink_to(rel / "scripts" / "helper_one.py")
                if kind == "a stray executable":
                    (stage / "directorctl").write_text("#!/bin/sh\n", encoding="utf-8")
                if kind == "a stray Git template":
                    (stage / saved["GIT_TEMPLATE_DIR_NAME"]).mkdir()
                if kind == "an empty skills tree":
                    (stage / saved["SKILLS_DIR_NAME"] / "s.md").unlink()
            args = ["p467", str(rel)]
            if not spec.get("default_staging"):
                spec = {**spec, "kwargs": {"staging_root": str(stage), **spec.get("kwargs", {})}}
        if "marker" in spec:
            (tmp / spec.get("rebind", {}).get("RELEASE_MARKER_NAME", saved["RELEASE_MARKER_NAME"])).write_text(spec["marker"], encoding="utf-8")
        if "env" in spec:
            if spec["env"] is None:
                _os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
            else:
                _os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = spec["env"]
        sub = lambda v: (_P(v.replace("TMP", str(tmp))) if v.startswith("TMP") else v) if isinstance(v, str) else (tuple(v) if isinstance(v, list) else v)
        args = [sub(a) for a in spec.get("args", [])] if "stage" not in spec else args
        kwargs = {k: (_os.getuid() + 1 if v == "OTHER" else sub(v)) for k, v in spec.get("kwargs", {}).items()}
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "shell_quote":
                t.shell_quote = lambda value: note("shell_quote rebound") or "<" + str(value) + ">"
            elif name == "role_tooling_staging_dir" and spec.get("default_staging"):
                t.role_tooling_staging_dir = lambda project, *, root=None: note("role_tooling_staging_dir rebound") or str(tmp / "stage")
            elif name == "role_tooling_staging_dir":
                t.role_tooling_staging_dir = lambda project, *, root=None: note("role_tooling_staging_dir rebound") or f"/p467/rebound/{project}"
            elif name == "privileged_install":
                t.privileged_install = types.SimpleNamespace(
                    roots_for=lambda staging_root: note("privileged_install rebound") or ("/p467/boundary", "/p467/policy"),
                    install_commands=lambda *a, **k: note("privileged_install rebound") or ["# p467 privileged install"])
            elif name == "__file__":
                t.__file__ = str(tmp / "scripts" / "ticket_board" / "project_provision.py")
            else:
                setattr(t, name, value)
        if spec["call"] is None:
            got = [saved["ROLE_STAGED_EXECUTABLES"], saved["RETIRED_STAGED_EXECUTABLES"], saved["GIT_TEMPLATE_DIR_NAME"], saved["TENANT_CONTROL_ROOT_ENV"]]
        else:
            fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
            try:
                got = fn(*args, **kwargs)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        if env_saved is None:
            _os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
        else:
            _os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = env_saved
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


#: What the thirteen read on project_provision when they run (its own, or imported there), and its callers of them.
KEPT = ("shell_quote", "TENANT_CONTROL_ROOT", "RELEASE_MARKER_NAME", "SKILLS_DIR_NAME", "privileged_install", "role_account_commands", "role_runtime_command",
        "render_operator_commands")
#: The four constants, as the baseline wrote them: (annotation, value).
CONSTANT_TEXT = {
    "RETIRED_STAGED_EXECUTABLES": ("tuple[str, ...]", "('switchyard-publish-ref', 'switchyard-publish', 'switchyard-integrate-main', 'switchyard-integrate', "
                                                      "'switchyard_publication_authority.py')"),
    "ROLE_STAGED_EXECUTABLES": ("tuple[str, ...]", "('ticket-board-pane-idle-hook', 'ticket-board-install-pane-hooks', 'ticket-board-claude-permission-hook', "
                                                   "'switchyard-board-skill', 'switchyard-publish-candidate', 'switchyard-request-publication', "
                                                   "'ticket-board-register-runtime', 'switchyard-tenant-control', 'switchyard-display-attach', 'ticket-board-write', "
                                                   "'ticket-board-read', 'directorctl')"),
    "TENANT_CONTROL_ROOT_ENV": (None, "'SWITCHYARD_TENANT_CONTROL_ROOT'"),
    "GIT_TEMPLATE_DIR_NAME": (None, "'git-template'"),
}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The imports two of them make of their own, as the baseline wrote them.
OWN_IMPORTS = {
    "tenant_control_root": ["import os as _os"],
    "git_template_files": ["try:\n    from scripts.repository_hooks import PRE_COMMIT_HELPERS, TEMPLATE_PRE_COMMIT\n"
                           "except ImportError:\n    from repository_hooks import PRE_COMMIT_HELPERS, TEMPLATE_PRE_COMMIT"],
}
#: The defaults, as the baseline wrote them: the staged executables bound at definition time, else None or nothing.
DEFAULTS = {"_script_sibling_modules": [], "_imported_names": [], "entry_point_module_dependencies": ["ROLE_STAGED_EXECUTABLES", "None"],
            "role_tooling_staging_dir": ["None"], "tenant_control_root": [], "readable_system_unit_path": ["None"], "system_unit_proof_commands": ["None"],
            "system_unit_proof_chain": ["None"], "_release_marker_commit": [], "staged_role_tooling_problems": ["None", "None"],
            "role_tooling_staging_commands": ["None"], "git_template_files": [], "git_template_staging_commands": []}
#: The packets rendered through the direct script and the package: the default plan, and two whose roles the plan shapes.
PACKET_VARIANTS = {
    "default": [],
    "shaped": ["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"],
    "lean": ["--no-include-designer", "--no-include-audit"],
}
#: One probe, run in the package and as the direct script: the staging and proof commands, the dependencies from the scripts
#: beside project_provision, and the Git template, for a synthetic project -- as a digest.
TOOLING_PROBE = (
    "import hashlib, json, os\n"
    "os.environ.pop('SWITCHYARD_TENANT_CONTROL_ROOT', None)\n"
    "out = [g['role_tooling_staging_commands']('p467', '/p467/release', staging_root='/p467/stage'), g['role_tooling_staging_commands']('p467', '/p467/release'),\n"
    "       g['system_unit_proof_commands']('p467', '\"$u\"'), g['system_unit_proof_chain']('p467', '/p467/u.service'), g['readable_system_unit_path']('p467'),\n"
    "       g['entry_point_module_dependencies'](), g['git_template_files']('/p467/release'), g['git_template_staging_commands']('/p467/release', '/p467/stage/p467'),\n"
    "       g['tenant_control_root'](), g['role_tooling_staging_dir']('p467')]\n"
    "print(len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest())\n"
)


def top_name(n: ast.AST) -> str | None:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        return n.target.id
    return None


def annotation_ids(tree: ast.AST) -> set[int]:
    """Every node inside an annotation: names there are not read when the code runs."""
    out: set[int] = set()
    for x in ast.walk(tree):
        parts = []
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = [x.returns, *(a.annotation for a in x.args.posonlyargs + x.args.args + x.args.kwonlyargs + [v for v in (x.args.vararg, x.args.kwarg) if v])]
        elif isinstance(x, ast.AnnAssign):
            parts = [x.annotation]
        for part in parts:
            if part is not None:
                out |= {id(y) for y in ast.walk(part)}
    return out


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.ticket_board.provision_role_tooling as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_team_launcher_reaches_them() -> None:
    for order in (("scripts.ticket_board.provision_role_tooling", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_role_tooling"),
                  ("scripts.team_launcher", "scripts.ticket_board.provision_role_tooling")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_role_tooling as m, scripts.team_launcher as tl; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        "all(getattr(tl, n) is getattr(m, n) for n in ('ROLE_STAGED_EXECUTABLES', 'role_tooling_staging_commands', 'role_tooling_staging_dir', "
                        "'staged_role_tooling_problems')), "
                        "inspect.signature(m.entry_point_module_dependencies).parameters['entry_points'].default is m.ROLE_STAGED_EXECUTABLES is t.ROLE_STAGED_EXECUTABLES, "
                        "m.entry_point_module_dependencies() == m.entry_point_module_dependencies(source_root=__import__('pathlib').Path(t.__file__).resolve().parents[1]), "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_role_tooling'] True True True True",
              f"{' then '.join(order)}: one object each, defined here; team_launcher's four imports reach the same ones; the default is the very staged list; "
              f"the dependencies are scanned beside project_provision's own file; nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    import ast as _ast
    import json as _json
    import os as _os
    import stat as _stat
    import typing
    check(m.ast is _ast and m.json is _json and m.os is _os and m.stat is _stat and m.Path is Path and m.Sequence is typing.Sequence and t.Path is m.Path,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_direct_script_stages_what_the_package_stages() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it, whose __file__ is still project_provision.py.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd467_script__'); "
                       "m = sys.modules['provision_role_tooling']; "
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + TOOLING_PROBE)
    as_package = python("import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + TOOLING_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("10 "),
          f"as a direct script: the script holds the module's own objects, and all ten answers are the package's, byte for byte: "
          f"{as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_role_tooling.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its sibling and its own __file__ included, read through it exactly as often as before: {through}")
        first = 1 if ast.get_docstring(node) is not None else 0
        own = [ast.unparse(x) for x in node.body if isinstance(x, (ast.Import, ast.ImportFrom)) or (isinstance(x, ast.Try) and ast.unparse(x) != CALL_TIME_IMPORT
                                                                                                  and any(isinstance(y, (ast.Import, ast.ImportFrom)) for y in x.body))]
        if expected:
            check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and own == OWN_IMPORTS.get(name, []),
                  f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback; its own imports the baseline's: {own}")
        else:
            check(own == OWN_IMPORTS.get(name, []) and not any(isinstance(x, ast.Try) and ast.unparse(x) == CALL_TIME_IMPORT for x in node.body),
                  f"{name}: reads nothing of project_provision; its own imports the baseline's: {own}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT) and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import ast", "import json", "import os", "import stat", "from pathlib import Path", "from typing import Sequence"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the four constants are the baseline's, annotations included: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's -- the staged list bound at definition, else None: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the seventeen, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_role_tooling" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_role_tooling" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the seventeen, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # What they read on project_provision is defined there, or imported there, unaliased, in its import block.
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_role_tooling")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read and their callers")
    # The definitions that name them are counted wherever they now live -- project_provision, or a later slice's module,
    # whose read through project_provision (provision.X) counts as the name (SYRD-470: their role-account callers moved on).
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_role_tooling"]
    uses: dict = {}
    for fn in [*tree.body, *(n for later_tree in later for n in later_tree.body)]:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                name = x.id if isinstance(x, ast.Name) else x.attr if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision" else None
                if name in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(name, 0)
                    uses[fn.name][name] += 1
    check(uses == DISPATCH, f"project_provision's own definitions name them exactly as often as before, by the re-exported names: {uses}")
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom, ast.Try)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(loose == [], f"and nothing reads them at module level: {loose}")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("project_provision"):
                for a in x.names:
                    if a.name in MOVED:
                        got[f"import {a.name}"] = got.get(f"import {a.name}", 0) + 1
        check(got == counts, f"{path} still imports them from project_provision, as often as before: {got}")


def test_the_direct_script_and_the_package_render_the_same_packets() -> None:
    # Three synthetic provisioning packets, each rendered by `project_provision.py` run as a script (its import fallback
    # taken) and through the package entry, in a test-owned directory; their operator commands stage the role tooling.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd467-packet-")).resolve()
    try:
        for variant, extra in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p467", "--owner-user", "p467-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34467", "--output-dir", f"{work}/out", *extra]
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                if mode == "script":
                    probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd467_script'); raise SystemExit(g['main']({argv!r}))")
                else:
                    probe = f"import scripts.ticket_board.project_provision as pp; raise SystemExit(pp.main({argv!r}))"
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES,
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            text = outputs["package"][2]["operator-commands.sh"].decode("utf-8")
            check(all(name in text for name in ("ticket-board-write", "git-template", "p467-ticket-board.service")),
                  f"{variant}: the operator commands stage the role tooling, the Git template and the readable system unit")
    finally:
        shutil.rmtree(base)


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

    def value(label):
        return result(label)["value"]

    def calls(label):
        return GOLDEN[label]["calls"]

    problems = lambda kind: value(f"staged problems: {kind}")
    check(value("control root: default") == value("control root: a blank environment") == "/usr/local/lib/switchyard"
          and value("control root: from the environment") == "/p467/control" and value("control root: the default rebound on project_provision") == "/p467/rebound"
          and value("control root: the variable rebound on project_provision") == "/usr/local/lib/switchyard",
          "the control root: SWITCHYARD_TENANT_CONTROL_ROOT when set and not blank, else TENANT_CONTROL_ROOT -- both names read through project_provision")
    check(value("staging dir: default") == "/usr/local/lib/switchyard/p467" and value("staging dir: from the environment") == "/p467/control/p467"
          and value("staging dir: a given root") == "/p467/given/p467" and value("unit path: default") == "/usr/local/lib/switchyard/p467/p467-ticket-board.service"
          and value("unit path: the staging dir rebound on project_provision") == "/p467/rebound/p467/p467-ticket-board.service",
          "the staging directory is the project's under the control root, and the readable unit sits in it")
    proof, chain = value("unit proof commands: a staging root"), value("unit proof chain: a staging root")
    check(proof[1] == "if [ -f /p467/src.service ]; then" and proof[3] == "else" and proof[4].startswith("    sudo rm -f ") and proof[-1] == "fi"
          and "install -m 0444 -o root -g root /p467/src.service '/p467/stage/p467/p467-ticket-board.service'" in proof[2]
          and chain == [proof[0], proof[2].strip()] and value("unit proof commands: the quoting rebound on project_provision")[0].endswith("</p467/stage/p467>"),
          "the unit proof: installed root-owned and read-only when the source exists, removed when it does not; the chain installs it outright")
    check(value("imported names: every form") == ["helper_one", "helper_two", "os"] and value("imported names: relative and plain") == ["json", "pkg"]
          and value("imported names: not Python") == [] and sorted(value("sibling modules")) == ["helper_one", "helper_three", "helper_two", "unrelated"],
          "imports: absolute modules only, by their top-level package, nothing for a file that does not parse; siblings are the directory's .py files")
    check(value("dependencies: transitive, in a synthetic tree") == ["helper_one", "helper_three", "helper_two"] and value("dependencies: an entry point that is absent") == []
          and value("dependencies: none") == [] and "unrelated" not in value("dependencies: transitive, in a synthetic tree")
          and value("dependencies: the scan root follows project_provision's __file__") == ["helper_one", "helper_three", "helper_two"]
          and value("dependencies: the staged executables, from the scripts beside project_provision") == value("dependencies: the default list is bound at definition, so a rebinding does not reach it"),
          "dependencies: the sibling modules the entry points import, transitively and sorted, scanned beside project_provision's __file__; the default list bound at definition")
    check(value("release marker: none")[0] == "" and value("release marker: none")[1].endswith("does not exist") and value("release marker: a commit") == ["p467c", ""]
          and value("release marker: no commit")[1].endswith("names no commit") and "could not be read" in value("release marker: unreadable")[1]
          and value("release marker: the name rebound on project_provision") == ["p467c", ""],
          "the release marker: its commit stripped, or why there is none -- the file name read through project_provision")
    template = value("git template staging")
    check(value("git template files")[0] == ["/p467/release/scripts/git_template_pre_commit", "hooks/pre-commit", "0755"]
          and template[0] == "sudo rm -rf '/p467/stage/p467/git-template'" and template[1] == "if [ -f '/p467/release/scripts/git_template_pre_commit' ]; then"
          and value("git template staging: the directory name rebound on project_provision")[0] == "sudo rm -rf '/p467/stage/p467/p467-template'",
          "the Git template: replaced whole, and only when the release carries it")
    staging = value("staging commands")
    check(staging[0] == "sudo install -d -m 0755 -o root -g root '/p467/stage/p467'" and value("staging commands: the default staging root")[0].endswith("'/usr/local/lib/switchyard/p467'")
          and "# p467 privileged install" in value("staging commands: the privileged installer rebound on project_provision")
          and value("staging commands: the lists rebound on project_provision") != staging and value("staging commands: the skills and marker names rebound on project_provision") != staging,
          "staging: the project's directory root-owned, the privileged boundary, the executables and their modules; every list and name read through project_provision")
    check(problems("complete") == [] and problems("the default staging dir, through project_provision") == []
          and problems("a group-writable executable") == ["TMP/stage/ticket-board-write is group- or world-writable"]
          and problems("an executable that is not executable") == ["TMP/stage/ticket-board-write is not executable"]
          and problems("a symlinked companion module") == ["TMP/stage/helper_one.py is not a regular file"]
          and problems("a stray executable")[0].startswith("TMP/stage/directorctl is staged but directorctl is not in this release")
          and problems("a stray Git template")[0].startswith("TMP/stage/git-template is staged but the Git template is not in this release")
          and problems("another release's marker") == ["the staged bundle names release p467other and this one is p467c"]
          and problems("an empty skills tree") == ["TMP/stage/skills is empty"]
          and problems("a release without a marker")[0].startswith("the staged bundle names release p467c, which this source does not")
          and problems("another owner expected") == ["TMP/stage/ticket-board-write is owned by uid OWN rather than by uid OTHER",
                                                     "TMP/stage/helper_one.py is owned by uid OWN rather than by uid OTHER"]
          and len(problems("nothing staged")) == len(problems("an empty staging dir")) == 5,
          "the staged bundle: every file this release stages present, regular, owned, not group-writable, executable where it runs; nothing it lacks; the same release")


def test_every_seam_is_reached() -> None:
    # Every function the thirteen read on project_provision is a recorder there; the constants, names, the privileged installer
    # and __file__ are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names | {"_script_sibling_modules", "_imported_names"} and functions <= REACHED,
          f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions <= set(REBINDABLE), f"every other seam is a rebindable name: {sorted(names - functions - set(REBINDABLE))}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_team_launcher_reaches_them",
             "test_the_direct_script_stages_what_the_package_stages", "test_the_seams_read_through_project_provision_and_nothing_bound",
             "test_project_provision_reexports_them_and_its_readers_reach_them_there", "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_role_tooling_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
