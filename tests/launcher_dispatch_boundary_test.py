#!/usr/bin/env python3
"""SYRD-452: the team-launcher command dispatch, against the launcher it came out of.

`main` -- parsing the team-launcher command line and handing each command to its
own function -- and `_reject_removed_commands`, which it runs first, moved
unchanged into `scripts/launcher_dispatch.py`; the launcher re-exports both.
This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher; the parameters
  and defaults are the same.
- **Seams (rule 24):** everything they read when they run -- the parser, every
  command function, the config resolution and loader, the role, account,
  desktop, checkout and session helpers, the removed-command table and each
  other -- is read through the launcher as often as before, so a patch there
  reaches them: every case below records them there. Every branch is where the
  baseline had it, in the same order.
- **Entry points:** the launcher's own `if __name__ == "__main__"` block and
  `scripts/team-launcher` still run the re-exported `main`; both are run here,
  for `--help` and a removed command.
- **The behaviour is the baseline's:** removed commands, help and a bad
  command; design and new; provision-runtime with and without a config; the
  resolution or the loader refusing; upgrade, add-role, set-vcs-close-role,
  stop, teardown and deploy-launcher; every pane mode and refusal, the re-exec
  as the role's own account among them; and the launch, with every option.
  `GOLDEN` below was produced by running the BASELINE launcher's own
  definitions over the very cases embedded here (`gold452.py`), not typed; it
  is byte-identical under `env -i`, in a normal role pane, with another HOME,
  USER and COLUMNS, under umask 077 and under several hash seeds.

No command runs: every command `main` can hand off to, and every host-facing
step on the way, is a recorder on the launcher, and `subprocess.run` is one
too. Every path is in a test-owned temporary tree. Spawns, every exec,
signals, account and group lookups and socket connections are refused for
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import launcher_dispatch as m  # noqa: E402

CHECKS = 0
MOVED = ('_reject_removed_commands', 'main')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, its sibling included.
SEAMS = {
    '_reject_removed_commands': {'BOOTSTRAP_REPLACEMENT_COMMAND': 1, 'REMOVED_CONFIG_FREE_COMMANDS': 1},
    'main': {'DEFAULT_CONFIG_DIR': 1, '_build_parser': 1, '_reject_removed_commands': 1, '_resolve_launcher_project_config': 2, '_role_by_name': 1, 'add_project_role_command': 1, 'attach_role_to_slot': 1, 'current_user_name': 1, 'default_pane_state_dir_for_user': 1, 'deploy_launcher_checkout': 1, 'design_project_command': 1, 'detach_role_from_slot': 1, 'ensure_configured_runtime_user': 1, 'ensure_launcher_checkout_current': 1, 'ensure_visible_role_session_for_viewer': 1, 'launch_project': 1, 'load_project_config': 2, 'new_project_command': 1, 'pane_command_args': 1, 'prepare_project_desktop': 1, 'provision_runtime_command': 1, 'role_run_as_user': 1, 'role_session_dir': 4, 'run_detached_role': 1, 'run_role_pane': 1, 'seed_default_session_dir_from_legacy_sources': 1, 'set_project_vcs_close_role_command': 1, 'stop_project': 1, 'switchyard_teardown_command': 1, 'upgrade_project_command': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the two that names them, and how often (none).
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often (none;
#: scripts/team-launcher imports main from the launcher when it runs, checked below).
READERS = {}
#: Measured on the baseline: main's branch tests, in order, and its (returns, raises, trys, ifs).
BRANCHES = ["args.command == 'design'", "args.command == 'new'", "args.command == 'provision-runtime' and args.config is None", "args.command == 'provision-runtime'", "args.command == 'upgrade'", "args.command == 'add-role'", 'not args.pane_mode or args.role', "args.command == 'set-vcs-close-role'", 'not args.pane_mode or args.role', "args.command == 'stop'", "args.command == 'teardown'", "args.command == 'deploy-launcher'", "args.command != 'pane' and (args.pane_mode or args.role)", "args.command == 'pane'", 'not args.pane_mode or not args.role', "args.pane_mode not in {'start', 'attach', 'attach-or-start', 'reload', 'attach-role', 'detach-role'}", "args.pane_mode not in {'attach', 'detach-role'}", 'pane_user and current_user_name() != pane_user', 'not args.skip_launcher_check', "args.pane_mode == 'attach-role'", 'args.slot is None', "args.pane_mode == 'detach-role'", 'args.no_attach and (not role.detached)', 'role.detached']
SHAPE = [16, 6, 1, 24]
#: The BASELINE's own behaviour for the cases below (`gold452.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'removed: bootstrap': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: bootstrap has been removed; use `switchyard new` to create a launchable project config', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'bootstrap']], {}]], 'stdout': [], 'stderr': []},
    'removed: bootstrap, blank project': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: bootstrap has been removed; use `switchyard new` to create a launchable project config', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [[' ', 'bootstrap']], {}]], 'stdout': [], 'stderr': []},
    'removed: the table and replacement rebound on the launcher': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: gone has been removed; use `switchyard make p452` to create a launchable project config', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'gone']], {}]], 'stdout': [], 'stderr': []},
    'removed: a blank project, the rebound replacement naming it': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: gone has been removed; use `switchyard make <project>` to create a launchable project config', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [[' ', 'gone']], {}]], 'stdout': [], 'stderr': []},
    'not removed: bootstrap as the first word': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['bootstrap']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['bootstrap'], {'explicit_config': None}], ['load_project_config', ['bootstrap', 'PATH TMP/registry/bootstrap.json'], {}], ['launch_project', ['CONFIG'], {'config_path': 'PATH TMP/registry/bootstrap.json', 'mode': 'start', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'dry_run': False, 'layout_output': None, 'pane_state_dir': None, 'force_reload': False, 'allow_stale_launcher': False, 'no_launcher_self_deploy': False, 'report_session_records': True, 'layout_mode': 'auto'}]], 'stdout': [], 'stderr': []},
    'argv from sys.argv': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'stop', '--config', 'TMP/cfg/p452.json']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': 'PATH TMP/cfg/p452.json'}], ['load_project_config', ['p452', 'PATH TMP/cfg/p452.json'], {}], ['stop_project', ['CONFIG'], {}]], 'stdout': [], 'stderr': []},
    'help': {'result': {'raised': 'SystemExit', 'message': '0', 'code_type': 'int'}, 'calls': [['_reject_removed_commands', [['--help']], {}], ['_build_parser', [], {}]], 'stdout': ['usage: team-launcher [-h] [--slot SLOT] [--config CONFIG] [--layout-output LAYOUT_OUTPUT]', '                     [--layout {auto,separate,viewer}] [--script-path SCRIPT_PATH]', '                     [--pane-state-dir PANE_STATE_DIR] [--dry-run] [--confirm CONFIRM]'], 'stderr': []},
    'an unknown command': {'result': {'raised': 'SystemExit', 'message': '2', 'code_type': 'int'}, 'calls': [['_reject_removed_commands', [['p452', 'launch']], {}], ['_build_parser', [], {}]], 'stdout': [], 'stderr': ['                     [pane_mode] [role]', "team-launcher: error: argument command: invalid choice: 'launch' (choose from 'start', 'attach', 'reload', 'stop', 'design', 'new', 'provision-runtime', 'deploy-launcher', 'upgrade', 'add-role', 'set-vcs-close-role', 'pane', 'teardown')"]},
    'design': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'design', '--design-output-dir', 'TMP/design', '--project-artifact', 'TMP/a.json', '--design-document', 'TMP/d.md', '--design-title', 'T', '--design-body', 'B', '--repository', 'TMP/repo', '--remote', 'up', '--default-branch', 'trunk', '--worktree-policy', 'isolated', '--owner-user', 'p452-agent', '--ticket-prefix', 'PX', '--push-policy', 'anyone', '--audit-signoff', '--no-needs-inspection', '--needs-user-signoff', '--board-service-traversal', '--supplementary-group', 'g1', '--supplementary-group', 'g2', '--linger', '--owner-shell', '/bin/sh']], {}], ['_build_parser', [], {}], ['design_project_command', ['p452'], {'output_dir': 'PATH TMP/design', 'artifact_path': 'PATH TMP/a.json', 'design_document': 'PATH TMP/d.md', 'design_title': 'T', 'design_body': 'B', 'repository': 'PATH TMP/repo', 'remote': 'up', 'default_branch': 'trunk', 'worktree_policy': 'isolated', 'owner_user': 'p452-agent', 'ticket_prefix': 'PX', 'push_policy': 'anyone', 'audit_signoff': True, 'needs_inspection': False, 'needs_user_signoff': True, 'board_service_traversal': True, 'supplementary_groups': ['g1', 'g2'], 'linger': True, 'owner_shell': '/bin/sh'}]], 'stdout': [], 'stderr': []},
    'design, the defaults': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'design']], {}], ['_build_parser', [], {}], ['design_project_command', ['p452'], {'output_dir': None, 'artifact_path': None, 'design_document': None, 'design_title': None, 'design_body': None, 'repository': None, 'remote': None, 'default_branch': None, 'worktree_policy': None, 'owner_user': None, 'ticket_prefix': None, 'push_policy': None, 'audit_signoff': None, 'needs_inspection': None, 'needs_user_signoff': None, 'board_service_traversal': None, 'supplementary_groups': None, 'linger': None, 'owner_shell': None}]], 'stdout': [], 'stderr': []},
    'new': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'new', '--from', 'TMP/a.json', '--owner-user', 'p452-agent', '--desktop-policy', 'headless', '--port', '8452', '--database', 'db452', '--source-repo', 'TMP/src', '--workflow-config', 'TMP/wf.json', '--commit-git-dir', 'TMP/git', '--repository', 'TMP/repo', '--new-output-dir', 'TMP/out', '--execute', '--dry-run', '--upstream-report-url', 'http://up', '--upstream-report-token-file', 'TMP/tok']], {}], ['_build_parser', [], {}], ['new_project_command', ['p452'], {'from_artifact': 'PATH TMP/a.json', 'owner_user': 'p452-agent', 'desktop_policy': 'PATH headless', 'port': 8452, 'database': 'db452', 'source_repo': 'PATH TMP/src', 'workflow_config': 'PATH TMP/wf.json', 'commit_git_dir': 'TMP/git', 'repository': 'PATH TMP/repo', 'output_dir': 'PATH TMP/out', 'execute': True, 'dry_run': True, 'upstream_report_url': 'http://up', 'upstream_report_token_file': 'TMP/tok'}]], 'stdout': [], 'stderr': []},
    'new, the defaults': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'new']], {}], ['_build_parser', [], {}], ['new_project_command', ['p452'], {'from_artifact': None, 'owner_user': None, 'desktop_policy': None, 'port': None, 'database': None, 'source_repo': None, 'workflow_config': None, 'commit_git_dir': None, 'repository': None, 'output_dir': None, 'execute': False, 'dry_run': False, 'upstream_report_url': '', 'upstream_report_token_file': ''}]], 'stdout': [], 'stderr': []},
    'provision-runtime, resolved, the config present': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'provision-runtime', '--runtime-user', 'rt']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['provision_runtime_command', ['rt', 'CONFIG'], {}]], 'stdout': [], 'stderr': []},
    'provision-runtime, resolved, no config file': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'provision-runtime']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['provision_runtime_command', [None, None], {}]], 'stdout': [], 'stderr': []},
    'provision-runtime, the resolution refuses': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'provision-runtime', '--runtime-user', 'rt']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['provision_runtime_command', ['rt', None], {}]], 'stdout': [], 'stderr': []},
    'provision-runtime, the resolution refuses, the default file present': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'provision-runtime']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/default-configs/p452.json'], {}], ['provision_runtime_command', [None, 'CONFIG'], {}]], 'stdout': [], 'stderr': []},
    'provision-runtime, an explicit config': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'provision-runtime', '--config', 'TMP/cfg/p452.json']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': 'PATH TMP/cfg/p452.json'}], ['load_project_config', ['p452', 'PATH TMP/cfg/p452.json'], {}], ['provision_runtime_command', [None, 'CONFIG'], {}]], 'stdout': [], 'stderr': []},
    'the resolution refuses a command': {'result': {'raised': 'SystemExit', 'message': "switchyard: no registered project 'p452' (syrd452)", 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'stop']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}]], 'stdout': [], 'stderr': []},
    'the loader refuses': {'result': {'raised': 'SystemExit', 'message': 'TMP/cfg/p452.json must define a non-empty roles list', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'stop', '--config', 'TMP/cfg/p452.json']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': 'PATH TMP/cfg/p452.json'}], ['load_project_config', ['p452', 'PATH TMP/cfg/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'upgrade': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'upgrade', '--config', 'TMP/cfg/p452.json', '--dry-run', '--source-repo', 'TMP/src', '--commit-git-dir', 'TMP/git', '--deploy-ref', 'v1', '--desktop-policy', 'TMP/pol.json', '--upstream-report-url', 'http://up', '--upstream-report-token-file', 'TMP/tok']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': 'PATH TMP/cfg/p452.json'}], ['load_project_config', ['p452', 'PATH TMP/cfg/p452.json'], {}], ['upgrade_project_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p452.json', 'dry_run': True, 'source_repo': 'PATH TMP/src', 'commit_git_dir': 'TMP/git', 'deploy_ref': 'v1', 'desktop_policy': 'PATH TMP/pol.json', 'publish_remote': '', 'upstream_report_url': 'http://up', 'upstream_report_token_file': 'TMP/tok'}]], 'stdout': [], 'stderr': []},
    'upgrade, the defaults': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'upgrade']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['upgrade_project_command', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'dry_run': False, 'source_repo': None, 'commit_git_dir': None, 'deploy_ref': None, 'desktop_policy': None, 'publish_remote': '', 'upstream_report_url': '', 'upstream_report_token_file': ''}]], 'stdout': [], 'stderr': []},
    'add-role': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'add-role', 'qa', '--config', 'TMP/cfg/p452.json', '--cli', 'claude', '--audit', '--detached', '--slot', '3', '--relayout', '--no-attach', '--pane-state-dir', 'TMP/ps']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': 'PATH TMP/cfg/p452.json'}], ['load_project_config', ['p452', 'PATH TMP/cfg/p452.json'], {}], ['add_project_role_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p452.json', 'role_name': 'qa', 'cli': 'claude', 'audit_role': True, 'detached': True, 'slot': 3, 'relayout': True, 'start': False, 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'pane_state_dir': 'PATH TMP/ps'}]], 'stdout': [], 'stderr': []},
    'add-role, the defaults': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'add-role', 'qa']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['add_project_role_command', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'role_name': 'qa', 'cli': 'codex', 'audit_role': False, 'detached': False, 'slot': None, 'relayout': False, 'start': True, 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'pane_state_dir': None}]], 'stdout': [], 'stderr': []},
    'add-role without a role': {'result': {'raised': 'SystemExit', 'message': 'add-role requires exactly one <role> argument', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'add-role']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'add-role with two words': {'result': {'raised': 'SystemExit', 'message': 'add-role requires exactly one <role> argument', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'add-role', 'qa', 'extra']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'set-vcs-close-role': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'set-vcs-close-role', 'ops']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['set_project_vcs_close_role_command', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'role_name': 'ops', 'runner': 'SUBPROCESS.RUN'}]], 'stdout': [], 'stderr': []},
    'set-vcs-close-role without a role': {'result': {'raised': 'SystemExit', 'message': 'set-vcs-close-role requires exactly one <role> argument', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'set-vcs-close-role']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'stop': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'stop']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['stop_project', ['CONFIG'], {}]], 'stdout': [], 'stderr': []},
    'teardown': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'teardown', '--dry-run', '--confirm', 'p452', '--drop-nonempty-board', '--destroy-registered-tenant', '--remove-owner-home', '--remove-owner-user', '--owner-user', 'p452-agent']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['switchyard_teardown_command', ['p452'], {'dry_run': True, 'confirm': 'p452', 'drop_nonempty_board': True, 'destroy_registered_tenant': True, 'remove_owner_home': True, 'remove_owner_user': True, 'owner_user': 'p452-agent'}]], 'stdout': [], 'stderr': []},
    'teardown, the defaults': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'teardown']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['switchyard_teardown_command', ['p452'], {'dry_run': False, 'confirm': None, 'drop_nonempty_board': False, 'destroy_registered_tenant': False, 'remove_owner_home': False, 'remove_owner_user': False, 'owner_user': None}]], 'stdout': [], 'stderr': []},
    'teardown, the resolved slug': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'teardown']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452-registered', 'PATH TMP/registry/p452.json'], {}], ['switchyard_teardown_command', ['p452-registered'], {'dry_run': False, 'confirm': None, 'drop_nonempty_board': False, 'destroy_registered_tenant': False, 'remove_owner_home': False, 'remove_owner_user': False, 'owner_user': None}]], 'stdout': [], 'stderr': []},
    'deploy-launcher': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'deploy-launcher', '--launcher-repo', 'TMP/launcher', '--clean-launcher']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['deploy_launcher_checkout', ['CONFIG'], {'launcher_repo': 'PATH TMP/launcher', 'clean': True}]], 'stdout': [], 'stderr': []},
    'stop with extra pane arguments': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'stop', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['stop_project', ['CONFIG'], {}]], 'stdout': [], 'stderr': []},
    'start with a role': {'result': {'raised': 'SystemExit', 'message': 'start does not accept extra pane arguments', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'start', 'main', 'extra']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'pane without a role': {'result': {'raised': 'SystemExit', 'message': 'pane mode requires <start|attach|attach-or-start|reload|attach-role|detach-role> and <role>', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'pane without a mode': {'result': {'raised': 'SystemExit', 'message': 'pane mode requires <start|attach|attach-or-start|reload|attach-role|detach-role> and <role>', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'pane']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    'pane with an unknown mode': {'result': {'raised': 'SystemExit', 'message': 'unknown pane mode: restart', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'pane', 'restart', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}]], 'stdout': [], 'stderr': []},
    "pane start: the role's own pane": {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane attach: no desktop preparation': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'attach', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['_role_by_name', ['CONFIG', 'main'], {}], ['role_run_as_user', ['CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'attach', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane reload, forced, a pane state dir given': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'reload', 'main', '--force', '--pane-state-dir', 'TMP/ps']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'reload', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/ps', 'force_reload': True, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane attach-or-start: a detached role': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'attach-or-start', 'bg']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'bg'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role bg'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role bg'], {}], ['run_detached_role', ['role bg'], {'mode': 'attach-or-start', 'session_dir': 'PATH TMP/sessions/bg', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane start --no-attach: the viewer session': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main', '--no-attach']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['ensure_visible_role_session_for_viewer', ['role main'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane start --no-attach, a detached role': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'bg', '--no-attach']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'bg'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role bg'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role bg'], {}], ['run_detached_role', ['role bg'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/bg', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane: an unknown role': {'result': {'raised': 'SystemExit', 'message': "unknown role 'nobody' in project p452", 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'nobody']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'nobody'], {}]], 'stdout': [], 'stderr': []},
    'pane: another account re-execs': {'result': 7, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main', '--slot', '2', '--force', '--skip-launcher-check', '--allow-stale-launcher', '--no-attach', '--script-path', 'TMP/tl']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', ['p452-main'], {'project': 'p452'}], ['current_user_name', [], {}], ['pane_command_args', ['p452', 'role main'], {'config_path': 'PATH TMP/registry/p452.json', 'mode': 'start', 'script_path': 'PATH TMP/tl', 'slot': 2, 'pane_state_dir': 'PATH TMP/pane-state/p452-main', 'force_reload': True, 'skip_launcher_check': True, 'allow_stale_launcher': True, 'no_attach': True, 'run_as_user': 'p452-main'}], ['subprocess.run', [['PANE-ARGV', 'p452', 'main']], {}]], 'stdout': [], 'stderr': []},
    'pane: another account re-execs, the defaults': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'attach', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['_role_by_name', ['CONFIG', 'main'], {}], ['role_run_as_user', ['CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', ['p452-main'], {'project': 'p452'}], ['current_user_name', [], {}], ['pane_command_args', ['p452', 'role main'], {'config_path': 'PATH TMP/registry/p452.json', 'mode': 'attach', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'slot': None, 'pane_state_dir': 'PATH TMP/pane-state/p452-main', 'force_reload': False, 'skip_launcher_check': False, 'allow_stale_launcher': False, 'no_attach': False, 'run_as_user': 'p452-main'}], ['subprocess.run', [['PANE-ARGV', 'p452', 'main']], {}]], 'stdout': [], 'stderr': []},
    "pane: the account is the caller's": {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', ['p452-main'], {'project': 'p452'}], ['current_user_name', [], {}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/p452-main', 'force_reload': False, 'bin_user': 'p452-main'}]], 'stdout': [], 'stderr': []},
    'pane: no account': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane: the launcher check skipped': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main', '--skip-launcher-check']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane: a stale launcher allowed': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main', '--allow-stale-launcher']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': True}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['run_role_pane', ['role main'], {'mode': 'start', 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody', 'force_reload': False, 'bin_user': ''}]], 'stdout': [], 'stderr': []},
    'pane: the checkout check refuses': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: the launcher checkout is stale (syrd452)', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'pane', 'start', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}]], 'stdout': [], 'stderr': []},
    'pane attach-role': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'attach-role', 'main', '--slot', '4']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['role_session_dir', ['PREPARED CONFIG', 'role main'], {}], ['attach_role_to_slot', ['PREPARED CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'role_name': 'main', 'slot': 4, 'session_dir': 'PATH TMP/sessions/main', 'pane_state_dir': 'PATH TMP/pane-state/nobody'}]], 'stdout': [], 'stderr': []},
    'pane attach-role without a slot': {'result': {'raised': 'SystemExit', 'message': 'pane attach-role requires --slot', 'code_type': 'str'}, 'calls': [['_reject_removed_commands', [['p452', 'pane', 'attach-role', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['prepare_project_desktop', ['CONFIG'], {}], ['_role_by_name', ['PREPARED CONFIG', 'main'], {}], ['role_run_as_user', ['PREPARED CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['PREPARED CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['PREPARED CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}]], 'stdout': [], 'stderr': []},
    'pane detach-role': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'pane', 'detach-role', 'main']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['_role_by_name', ['CONFIG', 'main'], {}], ['role_run_as_user', ['CONFIG', 'role main'], {}], ['default_pane_state_dir_for_user', [''], {'project': 'p452'}], ['ensure_launcher_checkout_current', ['CONFIG'], {'runner': 'SUBPROCESS.RUN', 'auto_deploy': False, 'allow_stale': False}], ['ensure_configured_runtime_user', ['CONFIG'], {}], ['seed_default_session_dir_from_legacy_sources', ['PATH TMP/sessions'], {}], ['detach_role_from_slot', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'role_name': 'main'}]], 'stdout': [], 'stderr': []},
    'start': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['launch_project', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'mode': 'start', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'dry_run': False, 'layout_output': None, 'pane_state_dir': None, 'force_reload': False, 'allow_stale_launcher': False, 'no_launcher_self_deploy': False, 'report_session_records': True, 'layout_mode': 'auto'}]], 'stdout': [], 'stderr': []},
    'start, every option': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'start', '--config', 'TMP/cfg/p452.json', '--script-path', 'TMP/tl', '--dry-run', '--layout-output', 'TMP/lay.json', '--pane-state-dir', 'TMP/ps', '--force', '--allow-stale-launcher', '--no-launcher-self-deploy', '--layout', 'viewer']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': 'PATH TMP/cfg/p452.json'}], ['load_project_config', ['p452', 'PATH TMP/cfg/p452.json'], {}], ['launch_project', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p452.json', 'mode': 'start', 'script_path': 'PATH TMP/tl', 'dry_run': True, 'layout_output': 'PATH TMP/lay.json', 'pane_state_dir': 'PATH TMP/ps', 'force_reload': True, 'allow_stale_launcher': True, 'no_launcher_self_deploy': True, 'report_session_records': True, 'layout_mode': 'viewer'}]], 'stdout': [], 'stderr': []},
    'attach': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'attach']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['launch_project', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'mode': 'attach', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'dry_run': False, 'layout_output': None, 'pane_state_dir': None, 'force_reload': False, 'allow_stale_launcher': False, 'no_launcher_self_deploy': False, 'report_session_records': True, 'layout_mode': 'auto'}]], 'stdout': [], 'stderr': []},
    'reload': {'result': 0, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'reload', '--layout', 'separate']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['launch_project', ['CONFIG'], {'config_path': 'PATH TMP/registry/p452.json', 'mode': 'reload', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'dry_run': False, 'layout_output': None, 'pane_state_dir': None, 'force_reload': False, 'allow_stale_launcher': False, 'no_launcher_self_deploy': False, 'report_session_records': True, 'layout_mode': 'separate'}]], 'stdout': [], 'stderr': []},
    "a command's own answer passed back": {'result': 5, 'type': 'int', 'calls': [['_reject_removed_commands', [['p452', 'stop']], {}], ['_build_parser', [], {}], ['_resolve_launcher_project_config', ['p452'], {'explicit_config': None}], ['load_project_config', ['p452', 'PATH TMP/registry/p452.json'], {}], ['stop_project', ['CONFIG'], {}]], 'stdout': [], 'stderr': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold452.py` (which ran them on the baseline) ------------------------------------
# A case runs `main` on an argv (or on `sys.argv`, when it passes none) and records, in order, every step it took and the
# answer: the command's return code, or the exact SystemExit. Every command it can hand off to, and every host-facing
# step on the way -- the config resolution and loader, the desktop, the role's account and the caller's, the pane state
# and session directories, the launcher checkout, the runtime user, the legacy-session seeding, the pane command line
# and `subprocess.run` itself -- is a recorder standing in on the launcher (on `subprocess`, for the run), so no command
# path runs. The parser and the role lookup are the launcher's own, recorded there and passed through; the config is a
# synthetic one, and every path is in a test-owned temporary tree.
CFG = ("--config", "@/cfg/p452.json")
CASES = {
    "removed: bootstrap": {"argv": ["p452", "bootstrap"]},
    "removed: bootstrap, blank project": {"argv": [" ", "bootstrap"]},
    "removed: the table and replacement rebound on the launcher": {"argv": ["p452", "gone"], "removed": ["gone"], "replacement": "switchyard make <project>"},
    "removed: a blank project, the rebound replacement naming it": {"argv": [" ", "gone"], "removed": ["gone"], "replacement": "switchyard make <project>"},
    "not removed: bootstrap as the first word": {"argv": ["bootstrap"]},
    "argv from sys.argv": {"argv": None, "sys_argv": ["team-launcher", "p452", "stop", *CFG]},
    "help": {"argv": ["--help"]},
    "an unknown command": {"argv": ["p452", "launch"]},
    "design": {"argv": ["p452", "design", "--design-output-dir", "@/design", "--project-artifact", "@/a.json", "--design-document", "@/d.md",
                        "--design-title", "T", "--design-body", "B", "--repository", "@/repo", "--remote", "up", "--default-branch", "trunk",
                        "--worktree-policy", "isolated", "--owner-user", "p452-agent", "--ticket-prefix", "PX", "--push-policy", "anyone",
                        "--audit-signoff", "--no-needs-inspection", "--needs-user-signoff", "--board-service-traversal",
                        "--supplementary-group", "g1", "--supplementary-group", "g2", "--linger", "--owner-shell", "/bin/sh"]},
    "design, the defaults": {"argv": ["p452", "design"]},
    "new": {"argv": ["p452", "new", "--from", "@/a.json", "--owner-user", "p452-agent", "--desktop-policy", "headless", "--port", "8452",
                     "--database", "db452", "--source-repo", "@/src", "--workflow-config", "@/wf.json", "--commit-git-dir", "@/git",
                     "--repository", "@/repo", "--new-output-dir", "@/out", "--execute", "--dry-run",
                     "--upstream-report-url", "http://up", "--upstream-report-token-file", "@/tok"]},
    "new, the defaults": {"argv": ["p452", "new"]},
    "provision-runtime, resolved, the config present": {"argv": ["p452", "provision-runtime", "--runtime-user", "rt"], "config_file": True},
    "provision-runtime, resolved, no config file": {"argv": ["p452", "provision-runtime"]},
    "provision-runtime, the resolution refuses": {"argv": ["p452", "provision-runtime", "--runtime-user", "rt"], "resolve_refuses": True},
    "provision-runtime, the resolution refuses, the default file present": {"argv": ["p452", "provision-runtime"], "resolve_refuses": True, "default_file": True},
    "provision-runtime, an explicit config": {"argv": ["p452", "provision-runtime", *CFG], "config_file": True},
    "the resolution refuses a command": {"argv": ["p452", "stop"], "resolve_refuses": True},
    "the loader refuses": {"argv": ["p452", "stop", *CFG], "load_refuses": True},
    "upgrade": {"argv": ["p452", "upgrade", *CFG, "--dry-run", "--source-repo", "@/src", "--commit-git-dir", "@/git", "--deploy-ref", "v1",
                         "--desktop-policy", "@/pol.json", "--upstream-report-url", "http://up", "--upstream-report-token-file", "@/tok"]},
    "upgrade, the defaults": {"argv": ["p452", "upgrade"]},
    "add-role": {"argv": ["p452", "add-role", "qa", *CFG, "--cli", "claude", "--audit", "--detached", "--slot", "3", "--relayout",
                          "--no-attach", "--pane-state-dir", "@/ps"]},
    "add-role, the defaults": {"argv": ["p452", "add-role", "qa"]},
    "add-role without a role": {"argv": ["p452", "add-role"]},
    "add-role with two words": {"argv": ["p452", "add-role", "qa", "extra"]},
    "set-vcs-close-role": {"argv": ["p452", "set-vcs-close-role", "ops"]},
    "set-vcs-close-role without a role": {"argv": ["p452", "set-vcs-close-role"]},
    "stop": {"argv": ["p452", "stop"]},
    "teardown": {"argv": ["p452", "teardown", "--dry-run", "--confirm", "p452", "--drop-nonempty-board", "--destroy-registered-tenant",
                          "--remove-owner-home", "--remove-owner-user", "--owner-user", "p452-agent"]},
    "teardown, the defaults": {"argv": ["p452", "teardown"]},
    "teardown, the resolved slug": {"argv": ["p452", "teardown"], "slug": "p452-registered"},
    "deploy-launcher": {"argv": ["p452", "deploy-launcher", "--launcher-repo", "@/launcher", "--clean-launcher"]},
    "stop with extra pane arguments": {"argv": ["p452", "stop", "main"]},
    "start with a role": {"argv": ["p452", "start", "main", "extra"]},
    "pane without a role": {"argv": ["p452", "pane", "start"]},
    "pane without a mode": {"argv": ["p452", "pane"]},
    "pane with an unknown mode": {"argv": ["p452", "pane", "restart", "main"]},
    "pane start: the role's own pane": {"argv": ["p452", "pane", "start", "main"]},
    "pane attach: no desktop preparation": {"argv": ["p452", "pane", "attach", "main"]},
    "pane reload, forced, a pane state dir given": {"argv": ["p452", "pane", "reload", "main", "--force", "--pane-state-dir", "@/ps"]},
    "pane attach-or-start: a detached role": {"argv": ["p452", "pane", "attach-or-start", "bg"]},
    "pane start --no-attach: the viewer session": {"argv": ["p452", "pane", "start", "main", "--no-attach"]},
    "pane start --no-attach, a detached role": {"argv": ["p452", "pane", "start", "bg", "--no-attach"]},
    "pane: an unknown role": {"argv": ["p452", "pane", "start", "nobody"]},
    "pane: another account re-execs": {"argv": ["p452", "pane", "start", "main", "--slot", "2", "--force", "--skip-launcher-check",
                                                "--allow-stale-launcher", "--no-attach", "--script-path", "@/tl"], "pane_user": "p452-main", "me": "p452-agent", "rc": 7},
    "pane: another account re-execs, the defaults": {"argv": ["p452", "pane", "attach", "main"], "pane_user": "p452-main", "me": "p452-agent"},
    "pane: the account is the caller's": {"argv": ["p452", "pane", "start", "main"], "pane_user": "p452-main", "me": "p452-main"},
    "pane: no account": {"argv": ["p452", "pane", "start", "main"], "pane_user": "", "me": "p452-agent"},
    "pane: the launcher check skipped": {"argv": ["p452", "pane", "start", "main", "--skip-launcher-check"]},
    "pane: a stale launcher allowed": {"argv": ["p452", "pane", "start", "main", "--allow-stale-launcher"]},
    "pane: the checkout check refuses": {"argv": ["p452", "pane", "start", "main"], "checkout_refuses": True},
    "pane attach-role": {"argv": ["p452", "pane", "attach-role", "main", "--slot", "4"]},
    "pane attach-role without a slot": {"argv": ["p452", "pane", "attach-role", "main"]},
    "pane detach-role": {"argv": ["p452", "pane", "detach-role", "main"]},
    "start": {"argv": ["p452"]},
    "start, every option": {"argv": ["p452", "start", *CFG, "--script-path", "@/tl", "--dry-run", "--layout-output", "@/lay.json", "--pane-state-dir", "@/ps",
                                     "--force", "--allow-stale-launcher", "--no-launcher-self-deploy", "--layout", "viewer"]},
    "attach": {"argv": ["p452", "attach"]},
    "reload": {"argv": ["p452", "reload", "--layout", "separate"]},
    "a command's own answer passed back": {"argv": ["p452", "stop"], "rc": 5},
}
FUNCTIONS = ("_reject_removed_commands", "main")
COMMANDS = ("design_project_command", "new_project_command", "provision_runtime_command", "upgrade_project_command", "add_project_role_command",
            "set_project_vcs_close_role_command", "stop_project", "switchyard_teardown_command", "deploy_launcher_checkout", "attach_role_to_slot",
            "detach_role_from_slot", "ensure_visible_role_session_for_viewer", "run_detached_role", "run_role_pane", "launch_project")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s dispatcher; every command and host-facing step a recorder on `t` (and on subprocess)."""
    import contextlib, io, os, shutil, subprocess as _subprocess, sys as _sys, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd452-")).resolve()
    launcher_dir = str(_P(t.__file__).resolve().parent)
    ROLES = [SimpleNamespace(role="main", detached=False, slot=0, tag="role main"), SimpleNamespace(role="bg", detached=True, slot=None, tag="role bg")]
    config = SimpleNamespace(project="p452", roles=ROLES, session_dir=tmp / "sessions", tag="CONFIG")
    prepared = SimpleNamespace(project="p452", roles=ROLES, session_dir=tmp / "sessions", tag="PREPARED CONFIG")

    def norm(value):
        if isinstance(value, SimpleNamespace) and hasattr(value, "tag"):
            return value.tag
        if value is run_recorder:
            return "SUBPROCESS.RUN"
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(str(tmp), "TMP").replace(launcher_dir, "LAUNCHER_DIR")
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP").replace(launcher_dir, "LAUNCHER_DIR")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def command(name):
        def f(*args, **kwargs):
            note(name, *args, **kwargs)
            return spec.get("rc", 0)
        return f

    def resolve(project, *, explicit_config=None):
        note("_resolve_launcher_project_config", project, explicit_config=explicit_config)
        if spec.get("resolve_refuses") and explicit_config is None:
            raise SystemExit(f"switchyard: no registered project {project!r} (syrd452)")
        return SimpleNamespace(config_path=explicit_config or tmp / "registry" / f"{project}.json", slug=spec.get("slug", project))

    def load(project, path):
        note("load_project_config", project, path)
        if spec.get("load_refuses"):
            raise SystemExit(f"{path} must define a non-empty roles list")
        return config

    def checkout(config_, **kwargs):
        note("ensure_launcher_checkout_current", config_, **kwargs)
        if spec.get("checkout_refuses"):
            raise SystemExit("team-launcher: the launcher checkout is stale (syrd452)")

    def run_recorder(argv, *args, **kwargs):
        note("subprocess.run", argv, *args, **kwargs)
        return SimpleNamespace(returncode=spec.get("rc", 0))

    def passthrough(name):
        def f(*args, **kwargs):
            note(name, *args, **kwargs)
            return saved[name](*args, **kwargs)
        return f

    passed = ("_reject_removed_commands", "_build_parser", "_role_by_name")
    stood = (*COMMANDS, "_resolve_launcher_project_config", "load_project_config", "prepare_project_desktop", "role_run_as_user", "current_user_name",
             "default_pane_state_dir_for_user", "ensure_launcher_checkout_current", "ensure_configured_runtime_user",
             "seed_default_session_dir_from_legacy_sources", "role_session_dir", "pane_command_args", "DEFAULT_CONFIG_DIR",
             "REMOVED_CONFIG_FREE_COMMANDS", "BOOTSTRAP_REPLACEMENT_COMMAND")
    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *passed, *stood)}
    saved_run, saved_argv, saved_columns = _subprocess.run, _sys.argv, os.environ.get("COLUMNS")
    try:
        os.environ["COLUMNS"] = "100"  # argparse wraps its usage and errors to the terminal's width
        for n in passed:
            setattr(t, n, passthrough(n))
        for n in COMMANDS:
            setattr(t, n, command(n))
        t._resolve_launcher_project_config = resolve
        t.load_project_config = load
        t.prepare_project_desktop = lambda config_: note("prepare_project_desktop", config_) or prepared
        t.role_run_as_user = lambda config_, role: note("role_run_as_user", config_, role) or spec.get("pane_user", "")
        t.current_user_name = lambda: note("current_user_name") or spec.get("me", "p452-agent")
        t.default_pane_state_dir_for_user = lambda user, *, project: note("default_pane_state_dir_for_user", user, project=project) or tmp / "pane-state" / (user or "nobody")
        t.ensure_launcher_checkout_current = checkout
        t.ensure_configured_runtime_user = lambda config_: note("ensure_configured_runtime_user", config_)
        t.seed_default_session_dir_from_legacy_sources = lambda session_dir: note("seed_default_session_dir_from_legacy_sources", session_dir)
        t.role_session_dir = lambda config_, role: note("role_session_dir", config_, role) or tmp / "sessions" / role.role
        t.pane_command_args = lambda project, role, **kwargs: note("pane_command_args", project, role, **kwargs) or ["PANE-ARGV", project, role.role]
        t.DEFAULT_CONFIG_DIR = tmp / "default-configs"
        if "removed" in spec:
            t.REMOVED_CONFIG_FREE_COMMANDS = frozenset(spec["removed"])
            t.BOOTSTRAP_REPLACEMENT_COMMAND = spec["replacement"]
        _subprocess.run = run_recorder
        (tmp / "registry").mkdir()
        (tmp / "default-configs").mkdir()
        if spec.get("config_file"):
            (tmp / "cfg").mkdir(); (tmp / "cfg" / "p452.json").write_text("{}")
            (tmp / "registry" / "p452.json").write_text("{}")
        if spec.get("default_file"):
            (tmp / "default-configs" / "p452.json").write_text("{}")
        dispatch = getattr(holder, "main")
        argv = None if spec["argv"] is None else [a.replace("@", str(tmp)) if a.startswith("@/") else a for a in spec["argv"]]
        # argparse names itself after sys.argv[0]: always the script's own name, as when it runs as team-launcher.
        _sys.argv = [a.replace("@", str(tmp)) if a.startswith("@/") else a for a in spec.get("sys_argv", ["team-launcher"])]
        shown, said = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(said):
                got = dispatch(argv) if argv is not None else dispatch()
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            code = exc.code if isinstance(exc, SystemExit) else None
            return {"result": {"raised": type(exc).__name__, "message": norm(str(code) if isinstance(exc, SystemExit) else str(exc)),
                               "code_type": type(code).__name__}, "calls": calls,
                    "stdout": norm(shown.getvalue()).splitlines()[:3], "stderr": norm(said.getvalue()).splitlines()[-2:]}
        return {"result": got, "type": type(got).__name__, "calls": calls, "stdout": norm(shown.getvalue()).splitlines()[:3],
                "stderr": norm(said.getvalue()).splitlines()[-2:]}
    finally:
        _subprocess.run, _sys.argv = saved_run, saved_argv
        if saved_columns is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = saved_columns
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
    result = python("import sys, scripts.launcher_dispatch as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.launcher_dispatch", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.launcher_dispatch")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.launcher_dispatch as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"[[(k, v.kind.name, v.default) for k, v in inspect.signature(getattr(m, n)).parameters.items()] for n in {MOVED!r}], "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', *{sorted({x for v in SEAMS.values() for x in v} - set(MOVED))!r})))")
        check(result.stdout.strip() == "True [[('argv', 'POSITIONAL_OR_KEYWORD', <class 'inspect._empty'>)], "
                                       "[('argv', 'POSITIONAL_OR_KEYWORD', None)]] True",
              f"{' then '.join(order)}: one object each; the same parameters and defaults; nothing of the launcher or its imports bound at load: {result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.subprocess is subprocess and m.sys is sys and m.Sequence is typing.Sequence and t.subprocess is m.subprocess and t.sys is m.sys,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "launcher_dispatch.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, its sibling included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported first thing, and nothing else imported: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *node.args.defaults]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    main = next(n for n in tree.body if getattr(n, "name", None) == "main")
    branches = [ast.unparse(n.test) for n in sorted((n for n in ast.walk(main) if isinstance(n, ast.If)), key=lambda n: (n.lineno, n.col_offset))]
    check([b.replace("launcher.", "") for b in branches] == BRANCHES,
          f"main: every branch, in the baseline's order (the only launcher read among them is current_user_name): {branches}")
    shape = [sum(isinstance(x, k) for x in ast.walk(main)) for k in (ast.Return, ast.Raise, ast.Try, ast.If)]
    check(shape == SHAPE, f"main: its returns, refusals, try (the provision-runtime fallback) and branches, as many as in the baseline: {shape}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import subprocess", "import sys", "from typing import Sequence"] and not [n for n in tree.body if isinstance(n, ast.If)],
          f"the standard library only, nothing for annotations: {top}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the two functions, in the baseline's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_both_entry_points_reach_main_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.launcher_dispatch"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"switchyard_main", "REMOVED_CONFIG_FREE_COMMANDS", "BOOTSTRAP_REPLACEMENT_COMMAND"} <= defined | exported,
          "the launcher defines neither, and keeps switchyard_main and every name they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition names either, as before: {uses}")
    # The launcher also calls other modules' own `main` (board_skill_cli.main and the like); only a read through the
    # dispatch module, or through the launcher itself, would bypass its names.
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED
                  and isinstance(x.value, ast.Name) and x.value.id in ("launcher_dispatch", "team_launcher", "launcher"))
    check(past == [], f"and nothing in the launcher reaches past its names: {past}")
    # The launcher's own entry point, last thing in the file, calls the re-exported main exactly as before.
    check(ast.unparse(tree.body[-1]) == "if __name__ == '__main__':\n    raise SystemExit(main(sys.argv[1:]))",
          f"python scripts/team_launcher.py still runs main on its arguments: {ast.unparse(tree.body[-1])}")
    wrapper = ast.parse((ROOT / "scripts" / "team-launcher").read_text(encoding="utf-8"))
    run = next(n for n in wrapper.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    check([ast.unparse(x) for x in run.body] == ["from scripts.team_launcher import main", "return main(argv)"]
          and ast.unparse(wrapper.body[-1]) == "if __name__ == '__main__':\n    raise SystemExit(run(sys.argv[1:]))",
          "scripts/team-launcher imports main from the launcher when it runs, so it reaches the re-exported function (or one rebound there)")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.Attribute) and x.attr in MOVED and isinstance(x.value, ast.Name) and x.value.id in ("launcher", "team_launcher"):
                got[ast.unparse(x)] = got.get(ast.unparse(x), 0) + 1
        check(dict(sorted(got.items())) == counts, f"{path} still reads them through the launcher, as often as before: {got}")


def test_the_real_entry_points_run_main_on_their_arguments() -> None:
    # Both real entry points, each run as __main__ in a Python of its own: `--help` returns before any config is read,
    # and a removed command is refused before the parser runs -- nothing else runs, and nothing is written.
    for entry in ("scripts/team-launcher", "scripts/team_launcher.py"):
        answers = []
        for argv in (["--help"], ["p452", "bootstrap"]):
            answers.append(python(f"import os, runpy, sys; os.environ['COLUMNS'] = '100'; sys.argv = [{entry!r}, *{argv!r}]; "
                                  f"runpy.run_path({entry!r}, run_name='__main__')"))
        helped, removed = answers
        check(helped.returncode == 0 and helped.stdout.startswith("usage: ") and "{start,attach,reload,stop,design,new" in helped.stdout
              and removed.returncode == 1 and removed.stdout == ""
              and removed.stderr == "team-launcher: bootstrap has been removed; use `switchyard new` to create a launchable project config\n",
              f"{entry}: --help and a removed command answer as before: {helped.returncode} {helped.stderr[-300:]} {removed.returncode} {removed.stderr[-300:]}")


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
        return [c[0] for c in GOLDEN[label]["calls"] if c[0] not in ("_reject_removed_commands", "_build_parser")]

    def call(label, seam):
        return next((c for c in GOLDEN[label]["calls"] if c[0] == seam), None)

    def refusal(label):
        r = result(label)
        return r["message"] if isinstance(r, dict) and r.get("raised") == "SystemExit" and r.get("code_type") == "str" else None

    R = "TMP/registry/p452.json"
    # Refused before anything is parsed or read.
    check(refusal("removed: bootstrap") == "team-launcher: bootstrap has been removed; use `switchyard new` to create a launchable project config"
          and refusal("removed: bootstrap, blank project") == refusal("removed: bootstrap")
          and refusal("removed: the table and replacement rebound on the launcher") == "team-launcher: gone has been removed; use `switchyard make p452` to create a launchable project config"
          and refusal("removed: a blank project, the rebound replacement naming it") == "team-launcher: gone has been removed; use `switchyard make <project>` to create a launchable project config"
          and [c[0] for c in GOLDEN["removed: bootstrap"]["calls"]] == ["_reject_removed_commands"]
          and steps("not removed: bootstrap as the first word") == ["_resolve_launcher_project_config", "load_project_config", "launch_project"],
          "a removed command is refused first, naming its replacement (both read on the launcher when it runs); only as the second word")
    check(GOLDEN["argv from sys.argv"]["calls"][0] == ["_reject_removed_commands", [["p452", "stop", "--config", "TMP/cfg/p452.json"]], {}]
          and result("help") == {"raised": "SystemExit", "message": "0", "code_type": "int"} and GOLDEN["help"]["stdout"][0].startswith("usage: team-launcher ")
          and result("an unknown command")["message"] == "2" and "invalid choice: 'launch'" in GOLDEN["an unknown command"]["stderr"][-1],
          "no argv means sys.argv[1:]; the parser answers help and a bad command itself, before anything is read")
    # Design and new: before any config is resolved or read.
    check(steps("design") == ["design_project_command"] and steps("new") == ["new_project_command"]
          and call("design", "design_project_command")[2]["supplementary_groups"] == ["g1", "g2"]
          and call("design", "design_project_command")[2]["needs_inspection"] is False
          and call("new, the defaults", "new_project_command")[2]["upstream_report_url"] == "" and call("new, the defaults", "new_project_command")[2]["upstream_report_token_file"] == "",
          "design and new hand every option on before any config is resolved; new's report URL and token file default to empty")
    # provision-runtime: the one command that may run without a config.
    check(call("provision-runtime, resolved, no config file", "provision_runtime_command") == ["provision_runtime_command", [None, None], {}]
          and call("provision-runtime, resolved, the config present", "provision_runtime_command")[1] == ["rt", "CONFIG"]
          and call("provision-runtime, the resolution refuses", "provision_runtime_command")[1] == ["rt", None]
          and call("provision-runtime, the resolution refuses, the default file present", "load_project_config")[1] == ["p452", "PATH TMP/default-configs/p452.json"]
          and refusal("the resolution refuses a command") == "switchyard: no registered project 'p452' (syrd452)",
          "provision-runtime falls back to the default config path when resolution refuses, and reads a config only if one exists; every other command stops at the refusal")
    check(refusal("the loader refuses") == "TMP/cfg/p452.json must define a non-empty roles list"
          and call("stop", "_resolve_launcher_project_config")[2] == {"explicit_config": None}
          and call("upgrade", "_resolve_launcher_project_config")[2] == {"explicit_config": "PATH TMP/cfg/p452.json"}
          and call("teardown, the resolved slug", "load_project_config")[1] == ["p452-registered", "PATH " + R]
          and call("teardown, the resolved slug", "switchyard_teardown_command")[1] == ["p452-registered"],
          "every other command resolves the project (an explicit --config wins) and loads its config under the resolved slug")
    u = call("upgrade, the defaults", "upgrade_project_command")[2]
    check(u["config_path"] == "PATH " + R and (u["publish_remote"], u["upstream_report_url"], u["upstream_report_token_file"]) == ("", "", "")
          and call("upgrade", "upgrade_project_command")[2]["deploy_ref"] == "v1",
          "upgrade gets the config, its path and every option; the publish remote and report settings default to empty")
    a = call("add-role, the defaults", "add_project_role_command")[2]
    check((a["role_name"], a["cli"], a["start"], a["script_path"]) == ("qa", "codex", True, "PATH LAUNCHER_DIR/team-launcher")
          and call("add-role", "add_project_role_command")[2]["start"] is False
          and refusal("add-role without a role") == refusal("add-role with two words") == "add-role requires exactly one <role> argument"
          and refusal("set-vcs-close-role without a role") == "set-vcs-close-role requires exactly one <role> argument"
          and call("set-vcs-close-role", "set_project_vcs_close_role_command")[2] == {"config_path": "PATH " + R, "role_name": "ops", "runner": "SUBPROCESS.RUN"},
          "add-role and set-vcs-close-role take exactly one role word; --no-attach means do not start; the runner is subprocess.run")
    check(steps("stop") == ["_resolve_launcher_project_config", "load_project_config", "stop_project"]
          and call("deploy-launcher", "deploy_launcher_checkout")[2] == {"launcher_repo": "PATH TMP/launcher", "clean": True}
          and result("a command's own answer passed back") == 5,
          "stop, teardown and deploy-launcher each get the loaded config, and every command's own answer is main's")
    # The extra-argument refusal comes after stop, teardown and deploy-launcher (the baseline's order, kept).
    check(steps("stop with extra pane arguments")[-1] == "stop_project"
          and refusal("start with a role") == "start does not accept extra pane arguments",
          "extra pane words are refused for the launch commands, after the commands that ignore them (the baseline's order)")
    # Pane.
    check(refusal("pane without a role") == refusal("pane without a mode") == "pane mode requires <start|attach|attach-or-start|reload|attach-role|detach-role> and <role>"
          and refusal("pane with an unknown mode") == "unknown pane mode: restart"
          and refusal("pane: an unknown role") == "unknown role 'nobody' in project p452",
          "a pane needs a known mode and a role of the project")
    check("prepare_project_desktop" not in steps("pane attach: no desktop preparation") and "prepare_project_desktop" not in steps("pane detach-role")
          and call("pane start: the role's own pane", "_role_by_name")[1][0] == "PREPARED CONFIG",
          "every pane mode but attach and detach-role prepares the desktop first, and the role is looked up in the prepared config")
    r = call("pane: another account re-execs", "pane_command_args")
    check(steps("pane: another account re-execs")[-2:] == ["pane_command_args", "subprocess.run"] and result("pane: another account re-execs") == 7
          and r[2]["run_as_user"] == "p452-main" and r[2]["pane_state_dir"] == "PATH TMP/pane-state/p452-main" and r[2]["slot"] == 2
          and "ensure_launcher_checkout_current" not in steps("pane: another account re-execs")
          and "subprocess.run" not in steps("pane: the account is the caller's") and "current_user_name" not in steps("pane: no account"),
          "a role running as another account re-execs as that account, with every option, and answers its return code; nothing else runs here")
    check(call("pane reload, forced, a pane state dir given", "run_role_pane")[2]["pane_state_dir"] == "PATH TMP/ps"
          and "default_pane_state_dir_for_user" not in steps("pane reload, forced, a pane state dir given")
          and "ensure_launcher_checkout_current" not in steps("pane: the launcher check skipped")
          and call("pane: a stale launcher allowed", "ensure_launcher_checkout_current")[2] == {"runner": "SUBPROCESS.RUN", "auto_deploy": False, "allow_stale": True}
          and refusal("pane: the checkout check refuses") == "team-launcher: the launcher checkout is stale (syrd452)"
          and steps("pane: the checkout check refuses")[-1] == "ensure_launcher_checkout_current",
          "the launcher checkout is checked (never auto-deployed) unless skipped, before the runtime user and the session seeding")
    check(refusal("pane attach-role without a slot") == "pane attach-role requires --slot"
          and call("pane attach-role", "attach_role_to_slot")[2]["slot"] == 4
          and steps("pane detach-role")[-1] == "detach_role_from_slot"
          and steps("pane start --no-attach: the viewer session")[-1] == "ensure_visible_role_session_for_viewer"
          and steps("pane start --no-attach, a detached role")[-1] == "run_detached_role"
          and steps("pane attach-or-start: a detached role")[-1] == "run_detached_role"
          and steps("pane start: the role's own pane")[-1] == "run_role_pane",
          "attach-role (with a slot), detach-role, the viewer session for a visible role, a detached role, else the role's own pane")
    # The launch.
    s = call("start, every option", "launch_project")[2]
    check(call("start", "launch_project")[2]["mode"] == "start" and call("attach", "launch_project")[2]["mode"] == "attach"
          and call("reload", "launch_project")[2]["layout_mode"] == "separate" and call("start", "launch_project")[2]["report_session_records"] is True
          and (s["dry_run"], s["force_reload"], s["allow_stale_launcher"], s["no_launcher_self_deploy"], s["layout_mode"]) == (True, True, True, True, "viewer"),
          "otherwise the launch, in the command's mode, reporting session records, with every option")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the two read on the launcher is a recorder there; the removed-command table and replacement are
    # rebound there by one case, and change the answer.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name))}
    check(constants == {"REMOVED_CONFIG_FREE_COMMANDS", "BOOTSTRAP_REPLACEMENT_COMMAND", "DEFAULT_CONFIG_DIR"} and names - constants <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_both_entry_points_reach_main_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"launcher_dispatch_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
