#!/usr/bin/env python3
"""SYRD-453: the switchyard command dispatch, against the launcher it came out of.

`switchyard_main` -- the wrapper's requires-root probe, the release notice, and
every `switchyard` verb handed to its own function, or, for a bare project
name, the ordinary launch -- moved unchanged into
`scripts/switchyard_dispatch.py`; the launcher re-exports it. This pins what
makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher; the parameter
  and default are the same.
- **Seams (rule 24):** everything it reads when it runs -- every command and
  parser, the project resolution and config loading, the tenant, desktop and
  first-run steps, and the launcher's own file and script name -- is read
  through the launcher as often as before, so a patch there reaches it: every
  case below records them there. The four commands it imports when it runs are
  still imported there, in order. Every branch is where the baseline had it.
- **The team-launcher script path:** `add-role`, `start` and the launch name it
  from `launcher.__file__`, the launcher's own file, exactly as before; the
  module's own `__file__` is never read.
- **Entry points:** both `switchyard` wrappers import it from the launcher, and
  both are run here for the requires-root probe their sudo wrapper asks.
- **The behaviour is the baseline's:** the probe, the menu, help and version;
  every verb and its refusals; the unknown-project and unreadable-config
  paths; stop and start with and without problems; and the ordinary launch
  through each first-run stop, a failure and success. `GOLDEN` below was
  produced by running the BASELINE launcher's own definition over the very
  cases embedded here (`gold453.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with a stray TICKET_BOARD_PROJECT.

No command runs: every command it can hand off to, and every host-facing step
on the way, is a recorder on the launcher or on the module it imports it from.
Every path is in a test-owned temporary tree. Spawns, every exec, signals,
account and group lookups and socket connections are refused for each case.
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
from scripts import switchyard_dispatch as m  # noqa: E402

CHECKS = 0
MOVED = ('switchyard_main',)
#: Measured on the baseline launcher: the moved body's call-time reads of launcher globals, its own file among them.
SEAMS = {
    # SYRD-531 adds the deploy-release parser and command.
    'switchyard_main': {'TEAM_LAUNCHER_NAME': 3, 'WORKER_POOL_ACTIONS': 1, '__file__': 3, '_build_switchyard_add_role_parser': 1, '_build_switchyard_adopt_workflow_parser': 1, '_build_switchyard_approve_desktop_parser': 1, '_build_switchyard_attach_parser': 1, '_build_switchyard_cutover_roles_parser': 1, '_build_switchyard_finish_upgrade_parser': 1, '_build_switchyard_install_shared_release_parser': 1, '_build_switchyard_migrate_workflow_parser': 1, '_build_switchyard_new_parser': 1, '_build_switchyard_present_parser': 1, '_build_switchyard_privileged_action_parser': 1, '_build_switchyard_publication_status_parser': 1, '_build_switchyard_rebind_workflow_panes_parser': 1, '_build_switchyard_register_parser': 1, '_build_switchyard_deploy_release_parser': 1, '_build_switchyard_release_status_parser': 1, '_build_switchyard_repair_boundary_parser': 1, '_build_switchyard_replace_window_parser': 1, '_build_switchyard_resume_provision_parser': 1, '_build_switchyard_rollout_log_parser': 1, '_build_switchyard_set_role_runtime_parser': 1, '_build_switchyard_set_vcs_close_role_parser': 1, '_build_switchyard_start_parser': 1, '_build_switchyard_status_parser': 1, '_build_switchyard_stop_parser': 1, '_build_switchyard_teardown_parser': 1, '_build_switchyard_upgrade_parser': 1, '_build_switchyard_worker_pool_parser': 1, '_load_switchyard_project_config_for_command': 16, '_resolve_switchyard_project': 18, 'add_project_role_command': 1, 'clear_owner_github_identity_command': 1, 'finish_upgrade_command': 1, 'launch_project': 2, 'prepare_project_desktop': 2, 'publication_status_command': 1, 'release_rollback_commands': 1, 'replace_presentation_window_command': 1, 'report_first_run_auth_warnings': 1, 'report_installed_release_version': 1, 'resume_tenant': 2, 'rollout_log_command': 1, 'run_switchyard_launch_first_run_auth': 1, 'set_owner_github_identity_command': 1, 'set_project_role_runtime_command': 1, 'set_project_vcs_close_role_command': 1, 'stop_before_launch_for_missing_owner_clis': 1, 'stop_before_launch_for_unauthenticated_providers': 1, 'stop_before_launch_for_unknown_models': 1, 'suspend_tenant': 1, 'switchyard_adopt_workflow_command': 1, 'switchyard_agy_credential_command': 1, 'switchyard_approve_desktop_command': 1, 'switchyard_attach_command': 1, 'switchyard_help_text': 1, 'switchyard_install_shared_release_command': 1, 'switchyard_invocation_requires_root': 1, 'switchyard_menu_command': 1, 'switchyard_migrate_workflow_command': 1, 'switchyard_new_command': 1, 'switchyard_present_command': 1, 'switchyard_rebind_workflow_panes_command': 1, 'switchyard_recover_display_command': 1, 'switchyard_register_command': 1, 'switchyard_deploy_release_command': 1, 'switchyard_release_status_command': 1, 'switchyard_repair_boundary_command': 1, 'switchyard_resume_provision_command': 1, 'switchyard_seed_role_credentials_command': 1, 'switchyard_status_command': 2, 'switchyard_teardown_command': 1, 'switchyard_validate_models_command': 1, 'switchyard_version_text': 1, 'switchyard_worker_pool_command': 1, 'upgrade_project_command': 1},
}
#: Measured on the baseline launcher: every launcher definition outside it that names it, and how often (none).
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads it through the launcher, and how often (none;
#: both switchyard wrappers import it from the launcher, checked below).
READERS = {}
#: Measured on the baseline: switchyard_main's branch tests, in order, and its (returns, raises, trys, ifs).
BRANCHES = ["argv[:1] == ['--switchyard-wrapper-requires-root']", 'not argv', "argv[0] in {'-h', '--help', 'help'}", "argv[0] in {'--version', 'version'}", "argv[0].casefold() == 'new'", "argv[0].casefold() == 'agy-credential'", "args.action == 'set' and (not args.user)", "argv[0].casefold() == 'seed-role-credentials'", "argv[0].casefold() == 'set-owner-identity'", 'args.clear == bool(args.key_name)', 'args.clear', "argv[0].casefold() == 'register'", "argv[0].casefold() == 'repair-boundary'", "argv[0].casefold() == 'approve-desktop'", "argv[0].casefold() == 'rebind-workflow-panes'", 'not sep or not name.strip() or (not value.strip())', 'not sep or not name.strip() or (not value.strip().isdigit())', "argv[0].casefold() == 'migrate-workflow'", "argv[0].casefold() == 'adopt-workflow'", "argv[0].casefold() == 'resume-provision'", "argv[0].casefold() == 'upgrade'", "argv[0].casefold() == 'install-shared-release'", "argv[0].casefold() == 'privileged-action'", 'not sep or not key', 'key in values', "argv[0].casefold() == 'rollout-log'", "argv[0].casefold() == 'publication-status'", "argv[0].casefold() == 'cutover-roles'", "argv[0].casefold() == 'finish-upgrade'", "argv[0].casefold() == 'deploy-release'", "argv[0].casefold() == 'release-status'", "argv[0].casefold() == 'worker-pool'", 'WORKER_POOL_ACTIONS[args.action] and (not args.member.strip())', "argv[0].casefold() == 'add-role'", "argv[0].casefold() == 'board-skill'", "argv[0].casefold() == 'onboarding-readiness'", "argv[0].casefold() in ('role-prompt', 'design-stage')", 'design_stage', 'not design_stage', 'not args.project.strip()', 'design_stage', "args.action != 'show'", 'args.prompt is not None', 'args.prompt_file is not None', "argv[0].casefold() == 'set-role-runtime'", "argv[0].casefold() == 'present'", "argv[0].casefold() == 'attach'", "argv[0].casefold() == 'replace-window'", "argv[0].casefold() == 'set-vcs-close-role'", "argv[0].casefold() == 'recover-display'", "argv[0].casefold() == 'stop'", 'problems', "argv[0].casefold() == 'start'", 'problems', "argv[0].casefold() == 'teardown'", "argv[0].casefold() == 'status'", 'selection', "argv[0].casefold() == 'validate-models'", 'len(argv) < 2', 'stop_before_launch_for_missing_owner_clis(first_run_auth_report)', 'stop_before_launch_for_unauthenticated_providers(first_run_auth_report)', 'stop_before_launch_for_unknown_models(first_run_auth_report, project=config.project)', 'launch_result != 0']
SHAPE = [51, 8, 0, 63]  # SYRD-531: the deploy-release branch and its return; SYRD-567: design-stage in role-prompt's block
#: The BASELINE's own behaviour for the cases below (`gold453.py`, run on the baseline launcher under the guard).
GOLDEN = {
    "the wrapper's requires-root probe, root": {'result': 0, 'type': 'int', 'calls': [['switchyard_invocation_requires_root', [['upgrade', 'p453']], {}]], 'stdout': ['requires-root'], 'stderr': []},
    "the wrapper's requires-root probe, no root": {'result': 0, 'type': 'int', 'calls': [['switchyard_invocation_requires_root', [['status']], {}]], 'stdout': ['no-root'], 'stderr': []},
    'the menu': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_menu_command', [], {}]], 'stdout': [], 'stderr': []},
    'argv from sys.argv': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_status_parser', [], {}], ['switchyard_status_command', [], {'json_output': True}]], 'stdout': [], 'stderr': []},
    'help, -h': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_help_text', [], {}]], 'stdout': ['SWITCHYARD HELP (syrd453)'], 'stderr': []},
    'help, --help': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_help_text', [], {}]], 'stdout': ['SWITCHYARD HELP (syrd453)'], 'stderr': []},
    'help, help': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_help_text', [], {}]], 'stdout': ['SWITCHYARD HELP (syrd453)'], 'stderr': []},
    'version, --version': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_version_text', [], {}]], 'stdout': ['switchyard SYRD453-VERSION'], 'stderr': []},
    'version, version': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_version_text', [], {}]], 'stdout': ['switchyard SYRD453-VERSION'], 'stderr': []},
    'new, every option': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_new_parser', [], {}], ['switchyard_new_command', [], {'slug': 'p453', 'agent_name': 'p453-agent', 'project_name': 'P 453', 'project_path': 'PATH TMP/proj', 'from_artifact': 'PATH TMP/a.json', 'source_repo': 'PATH TMP/src', 'workflow_config': 'PATH TMP/wf.json', 'commit_git_dir': 'TMP/git', 'output_dir': 'PATH TMP/out', 'agent_cli_policy': 'promote-local', 'agent_cli_sources': ['claude=TMP/c', 'codex=TMP/x'], 'port': 8453, 'database': 'db453', 'yes': True, 'desktop_policy': 'PATH TMP/pol.json', 'headless': True, 'desktop_gui_user': 'gui', 'allow_existing_owner_user': True, 'agy_credential_source': 'src-user', 'no_agy_credential': True, 'layout_mode': 'viewer', 'git_init': False}]], 'stdout': [], 'stderr': []},
    'new, the defaults': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_new_parser', [], {}], ['switchyard_new_command', [], {'slug': None, 'agent_name': None, 'project_name': None, 'project_path': None, 'from_artifact': None, 'source_repo': None, 'workflow_config': None, 'commit_git_dir': None, 'output_dir': None, 'agent_cli_policy': '', 'agent_cli_sources': [], 'port': None, 'database': None, 'yes': False, 'desktop_policy': None, 'headless': False, 'desktop_gui_user': '', 'allow_existing_owner_user': False, 'agy_credential_source': None, 'no_agy_credential': False, 'layout_mode': 'auto', 'git_init': True}]], 'stdout': [], 'stderr': []},
    'new, in capitals': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_new_parser', [], {}], ['switchyard_new_command', [], {'slug': None, 'agent_name': None, 'project_name': None, 'project_path': None, 'from_artifact': None, 'source_repo': None, 'workflow_config': None, 'commit_git_dir': None, 'output_dir': None, 'agent_cli_policy': '', 'agent_cli_sources': [], 'port': None, 'database': None, 'yes': False, 'desktop_policy': None, 'headless': False, 'desktop_gui_user': '', 'allow_existing_owner_user': False, 'agy_credential_source': None, 'no_agy_credential': False, 'layout_mode': 'auto', 'git_init': True}]], 'stdout': [], 'stderr': []},
    'new, a bad option': {'result': {'raised': 'SystemExit', 'message': '2', 'code_type': 'int'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_new_parser', [], {}]], 'stdout': [], 'stderr': ['                      [--desktop-gui-user DESKTOP_GUI_USER] [--workflow-config WORKFLOW_CONFIG]', "switchyard new: error: argument --port: invalid int value: 'many'"]},
    'agy-credential show': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_agy_credential_command', ['show'], {'source_user': None}]], 'stdout': [], 'stderr': []},
    'agy-credential set': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_agy_credential_command', ['set'], {'source_user': 'someone'}]], 'stdout': [], 'stderr': []},
    'agy-credential set without a user': {'result': {'raised': 'SystemExit', 'message': 'switchyard: agy-credential set requires a user name', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': []},
    'agy-credential clear': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_agy_credential_command', ['clear'], {'source_user': None}]], 'stdout': [], 'stderr': []},
    'agy-credential, an unknown action': {'result': {'raised': 'SystemExit', 'message': '2', 'code_type': 'int'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': ['usage: switchyard agy-credential [-h] {show,set,clear} [user]', "switchyard agy-credential: error: argument action: invalid choice: 'rotate' (choose from 'show', 'set', 'clear')"]},
    'seed-role-credentials': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['seed-role-credentials', 'p453']], {}], ['switchyard_seed_role_credentials_command', ['CONFIG'], {'role_name': '', 'reseed': False}]], 'stdout': [], 'stderr': []},
    'seed-role-credentials, one role, reseeded': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['seed-role-credentials', 'p453', '--role', 'ops', '--reseed']], {}], ['switchyard_seed_role_credentials_command', ['CONFIG'], {'role_name': 'ops', 'reseed': True}]], 'stdout': [], 'stderr': []},
    'set-owner-identity, a key': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['set-owner-identity', 'p453', '--key-name', 'id_ed25519', '--host-alias', 'gh-p453', '--host', 'forge.example', '--dry-run']], {}], ['set_owner_github_identity_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'key_name': 'id_ed25519', 'host_alias': 'gh-p453', 'host': 'forge.example', 'dry_run': True}]], 'stdout': [], 'stderr': []},
    'set-owner-identity, a key, the defaults': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['set-owner-identity', 'p453', '--key-name', 'id_ed25519']], {}], ['set_owner_github_identity_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'key_name': 'id_ed25519', 'host_alias': '', 'host': 'github.com', 'dry_run': False}]], 'stdout': [], 'stderr': []},
    'set-owner-identity, cleared': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['set-owner-identity', 'p453', '--clear']], {}], ['clear_owner_github_identity_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'dry_run': False}]], 'stdout': [], 'stderr': []},
    'set-owner-identity, both': {'result': {'raised': 'SystemExit', 'message': '2', 'code_type': 'int'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': ['                                     project', 'switchyard set-owner-identity: error: give exactly one of --key-name or --clear']},
    'set-owner-identity, neither': {'result': {'raised': 'SystemExit', 'message': '2', 'code_type': 'int'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': ['                                     project', 'switchyard set-owner-identity: error: give exactly one of --key-name or --clear']},
    'register': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_register_parser', [], {}], ['switchyard_register_command', ['PATH TMP/cfg.json'], {}]], 'stdout': [], 'stderr': []},
    'repair-boundary': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_repair_boundary_parser', [], {}], ['switchyard_repair_boundary_command', ['p453'], {'apply': True}]], 'stdout': [], 'stderr': []},
    'approve-desktop': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_approve_desktop_parser', [], {}], ['switchyard_approve_desktop_command', [], {'gui_user': 'gui', 'reference': 'ticket 1', 'show': True, 'revoke': False}]], 'stdout': [], 'stderr': []},
    'approve-desktop, revoked': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_approve_desktop_parser', [], {}], ['switchyard_approve_desktop_command', [], {'gui_user': '', 'reference': '', 'show': False, 'revoke': True}]], 'stdout': [], 'stderr': []},
    'rebind-workflow-panes': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rebind_workflow_panes_parser', [], {}], ['switchyard_rebind_workflow_panes_command', ['p453'], {'apply': True, 'expect': 'abc', 'runtimes': {'ops': 'codex', 'qa': 'claude'}, 'slots': {'ops': 3, 'qa': 4}, 'config_path': 'PATH TMP/c.json'}]], 'stdout': [], 'stderr': []},
    'rebind-workflow-panes, a runtime without a role': {'result': {'raised': 'SystemExit', 'message': "switchyard: --runtime takes ROLE=RUNTIME, not '=codex'", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rebind_workflow_panes_parser', [], {}]], 'stdout': [], 'stderr': []},
    'rebind-workflow-panes, a runtime without =': {'result': {'raised': 'SystemExit', 'message': "switchyard: --runtime takes ROLE=RUNTIME, not 'ops'", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rebind_workflow_panes_parser', [], {}]], 'stdout': [], 'stderr': []},
    'rebind-workflow-panes, a runtime without a value': {'result': {'raised': 'SystemExit', 'message': "switchyard: --runtime takes ROLE=RUNTIME, not 'ops= '", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rebind_workflow_panes_parser', [], {}]], 'stdout': [], 'stderr': []},
    'rebind-workflow-panes, a slot that is not a number': {'result': {'raised': 'SystemExit', 'message': "switchyard: --slot takes ROLE=SLOT, not 'ops=three'", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rebind_workflow_panes_parser', [], {}]], 'stdout': [], 'stderr': []},
    'migrate-workflow': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_migrate_workflow_parser', [], {}], ['switchyard_migrate_workflow_command', ['p453'], {'apply': True, 'config_path': 'PATH TMP/c.json'}]], 'stdout': [], 'stderr': []},
    'adopt-workflow': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_adopt_workflow_parser', [], {}], ['switchyard_adopt_workflow_command', ['p453'], {'apply': True, 'despite_board': 'because', 'config_path': 'PATH TMP/c.json'}]], 'stdout': [], 'stderr': []},
    'resume-provision': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_resume_provision_parser', [], {}], ['switchyard_resume_provision_command', ['p453'], {'source_repo': 'PATH TMP/src', 'config_path': 'PATH TMP/c.json'}]], 'stdout': [], 'stderr': []},
    'upgrade, every option': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_upgrade_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['upgrade', 'p453', '--dry-run', '--deploy-ref', 'v2', '--source-repo', 'TMP/src', '--commit-git-dir', 'TMP/git', '--desktop-policy', 'TMP/pol.json', '--upstream-report-url', 'http://up', '--upstream-report-token-file', 'TMP/tok', '--publish-remote', 'pub']], {}], ['upgrade_project_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'dry_run': True, 'source_repo': 'PATH TMP/src', 'commit_git_dir': 'TMP/git', 'deploy_ref': 'v2', 'desktop_policy': 'PATH TMP/pol.json', 'publish_remote': 'pub', 'upstream_report_url': 'http://up', 'upstream_report_token_file': 'TMP/tok'}]], 'stdout': [], 'stderr': []},
    'upgrade, the defaults': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_upgrade_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['upgrade', 'p453']], {}], ['upgrade_project_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'dry_run': False, 'source_repo': None, 'commit_git_dir': None, 'deploy_ref': None, 'desktop_policy': None, 'publish_remote': '', 'upstream_report_url': '', 'upstream_report_token_file': ''}]], 'stdout': [], 'stderr': []},
    'install-shared-release': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_install_shared_release_parser', [], {}], ['switchyard_install_shared_release_command', ['abc123'], {'rollback': True, 'dry_run': True}]], 'stdout': [], 'stderr': []},
    'privileged-action': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_privileged_action_parser', [], {}], ['privileged_front_door.privileged_action_command', ['p453', 'restart-board', {'a': '1', 'b': 'x=y'}], {'dry_run': True, 'rollback_commands': 'LAMBDA'}], ['release_rollback_commands', ['p453-rollback'], {}]], 'stdout': [], 'stderr': []},
    'privileged-action, a value without =': {'result': {'raised': 'SystemExit', 'message': "switchyard: expected key=value, got 'a'", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_privileged_action_parser', [], {}]], 'stdout': [], 'stderr': []},
    'privileged-action, an empty key': {'result': {'raised': 'SystemExit', 'message': "switchyard: expected key=value, got '=1'", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_privileged_action_parser', [], {}]], 'stdout': [], 'stderr': []},
    'privileged-action, a key twice': {'result': {'raised': 'SystemExit', 'message': 'switchyard: a was given twice', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_privileged_action_parser', [], {}]], 'stdout': [], 'stderr': []},
    'rollout-log': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rollout_log_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['rollout_log_command', ['p453'], {'attempt': '7', 'output': True}]], 'stdout': [], 'stderr': []},
    'rollout-log, a name that is not the slug': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_rollout_log_parser', [], {}], ['_resolve_switchyard_project', ['P453'], {}], ['rollout_log_command', ['p453'], {'attempt': '', 'output': False}]], 'stdout': [], 'stderr': []},
    'publication-status': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_publication_status_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['publication-status', 'p453', '--verify']], {}], ['publication_status_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'verify': True}]], 'stdout': [], 'stderr': []},
    'cutover-roles, retired': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_cutover_roles_parser', [], {}]], 'stdout': ['switchyard: cutover-roles is retired for p453; run `switchyard upgrade p453` to repatriate resumable state without creating or deleting accounts'], 'stderr': []},
    'finish-upgrade': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_finish_upgrade_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['finish-upgrade', 'p453', '--dry-run', '--deploy-ref', 'v2', '--source-repo', 'TMP/src', '--commit-git-dir', 'TMP/git']], {}], ['finish_upgrade_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'dry_run': True, 'source_repo': 'PATH TMP/src', 'commit_git_dir': 'TMP/git', 'deploy_ref': 'v2'}]], 'stdout': [], 'stderr': []},
    'deploy-release': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_deploy_release_parser', [], {}], ['switchyard_deploy_release_command', ['p453'], {'commit': 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'}]], 'stdout': [], 'stderr': []},
    'release-status': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_release_status_parser', [], {}], ['switchyard_release_status_command', ['p453'], {'close': True}]], 'stdout': [], 'stderr': []},
    'worker-pool list': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_worker_pool_parser', [], {}], ['switchyard_worker_pool_command', ['p453'], {'action': 'list', 'member': '', 'apply_changes': False, 'force': False, 'out': None, 'journal': None}]], 'stdout': [], 'stderr': []},
    'worker-pool restart, a member': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_worker_pool_parser', [], {}], ['switchyard_worker_pool_command', ['p453'], {'action': 'restart', 'member': 'pool-3', 'apply_changes': True, 'force': True, 'out': 'PATH TMP/o', 'journal': 'PATH TMP/j'}]], 'stdout': [], 'stderr': []},
    'worker-pool restart, no member': {'result': {'raised': 'SystemExit', 'message': 'switchyard: worker-pool restart needs the worker it acts on, e.g. `switchyard worker-pool p453 restart <pool>-3`', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_worker_pool_parser', [], {}]], 'stdout': [], 'stderr': []},
    'worker-pool plan, no member needed': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_worker_pool_parser', [], {}], ['switchyard_worker_pool_command', ['p453'], {'action': 'plan', 'member': '', 'apply_changes': False, 'force': False, 'out': None, 'journal': None}]], 'stdout': [], 'stderr': []},
    'add-role': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_add_role_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['add-role', 'p453', 'qa', '--cli', 'claude', '--audit', '--slot', '2', '--detached', '--relayout', '--no-start']], {}], ['add_project_role_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'role_name': 'qa', 'cli': 'claude', 'audit_role': True, 'detached': True, 'slot': 2, 'relayout': True, 'start': False, 'script_path': 'PATH LAUNCHER_DIR/team-launcher'}]], 'stdout': [], 'stderr': []},
    'add-role, the defaults': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_add_role_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['add-role', 'p453', 'qa']], {}], ['add_project_role_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'role_name': 'qa', 'cli': '', 'audit_role': False, 'detached': False, 'slot': None, 'relayout': False, 'start': True, 'script_path': 'PATH LAUNCHER_DIR/team-launcher'}]], 'stdout': [], 'stderr': []},
    'board-skill': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['board_skill_cli.main', [['status', '--json']], {'prog': 'switchyard board-skill'}]], 'stdout': [], 'stderr': []},
    'onboarding-readiness': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['onboarding_readiness.main', [['p453']], {}]], 'stdout': [], 'stderr': []},
    'role-prompt show': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['role-prompt', 'show', 'ops', '--project', 'p453']], {}], ['workflow_manage.main', [['show-role-prompt', '--role', 'ops', '--board-url', 'http://127.0.0.1:8453']], {}]], 'stdout': [], 'stderr': []},
    'role-prompt set, a prompt': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['role-prompt', 'set', 'ops', '--project', 'p453', '--prompt', 'be brief']], {}], ['workflow_manage.main', [['set-role-prompt', '--role', 'ops', '--board-url', 'http://127.0.0.1:8453', '--config', 'TMP/cfg/p453.json', '--prompt', 'be brief']], {}]], 'stdout': [], 'stderr': []},
    'role-prompt set, a prompt file': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['role-prompt', 'set', 'ops', '--project', 'p453', '--prompt-file', 'TMP/prompt.txt']], {}], ['workflow_manage.main', [['set-role-prompt', '--role', 'ops', '--board-url', 'http://127.0.0.1:8453', '--config', 'TMP/cfg/p453.json', '--prompt-file', 'TMP/prompt.txt']], {}]], 'stdout': [], 'stderr': []},
    'role-prompt clear, the project from the pane': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453-pane'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453-pane', ['role-prompt', 'clear', 'ops']], {}], ['workflow_manage.main', [['clear-role-prompt', '--role', 'ops', '--board-url', 'http://127.0.0.1:8453', '--config', 'TMP/cfg/p453-pane.json']], {}]], 'stdout': [], 'stderr': []},
    'role-prompt, no project': {'result': {'raised': 'SystemExit', 'message': 'switchyard: no project selected; pass --project or run where TICKET_BOARD_PROJECT is set', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': []},
    'role-prompt, a blank project': {'result': {'raised': 'SystemExit', 'message': 'switchyard: no project selected; pass --project or run where TICKET_BOARD_PROJECT is set', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': []},
    # SYRD-567: design-stage shares role-prompt's block and forwards add-design-stage.
    'design-stage, a dry run': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['design-stage', '--project', 'p453', '--dry-run']], {}], ['workflow_manage.main', [['add-design-stage', '--board-url', 'http://127.0.0.1:8453', '--config', 'TMP/cfg/p453.json', '--dry-run']], {}]], 'stdout': [], 'stderr': []},
    'design-stage, the project from the pane': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453-pane'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453-pane', ['design-stage']], {}], ['workflow_manage.main', [['add-design-stage', '--board-url', 'http://127.0.0.1:8453', '--config', 'TMP/cfg/p453-pane.json']], {}]], 'stdout': [], 'stderr': []},
    'design-stage, no project': {'result': {'raised': 'SystemExit', 'message': 'switchyard: no project selected; pass --project or run where TICKET_BOARD_PROJECT is set', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': []},
    # SYRD-534: the command is handed --effort, None when it is not given.
    'set-role-runtime': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_set_role_runtime_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['set-role-runtime', 'p453', 'ops', '--cli', 'claude', '--model', 'opus', '--force', '--reason', 'why', '--dry-run']], {}], ['set_project_role_runtime_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'role_name': 'ops', 'runtime': 'claude', 'model': 'opus', 'effort': None, 'force': True, 'reason': 'why', 'dry_run': True}]], 'stdout': [], 'stderr': []},
    'set-role-runtime, an effort': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_set_role_runtime_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['set-role-runtime', 'p453', 'ops', '--cli', 'codex', '--model', 'gpt-5.6-luna', '--effort', 'xhigh', '--dry-run']], {}], ['set_project_role_runtime_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'role_name': 'ops', 'runtime': 'codex', 'model': 'gpt-5.6-luna', 'effort': 'xhigh', 'force': False, 'reason': '', 'dry_run': True}]], 'stdout': [], 'stderr': []},
    'present': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_present_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['present', 'p453', 'list']], {}], ['switchyard_present_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'args': {'Namespace': {'action': 'list', 'json': False, 'project': 'p453'}}}]], 'stdout': [], 'stderr': []},
    'attach': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_attach_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['attach', 'p453', 'ops', '--json']], {}], ['switchyard_attach_command', ['CONFIG'], {'args': {'Namespace': {'json': True, 'project': 'p453', 'role': 'ops'}}}]], 'stdout': [], 'stderr': []},
    'replace-window, a project name in words': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_replace_window_parser', [], {}], ['_resolve_switchyard_project', ['Project 453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY project453', ['replace-window', 'Project', '453']], {}], ['replace_presentation_window_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/project453.json'}]], 'stdout': [], 'stderr': []},
    'set-vcs-close-role': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_set_vcs_close_role_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['set-vcs-close-role', 'p453', 'ops']], {}], ['set_project_vcs_close_role_command', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'role_name': 'ops'}]], 'stdout': [], 'stderr': []},
    'recover-display': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['switchyard_recover_display_command', [['recover-display', 'p453', '--anything']], {}]], 'stdout': [], 'stderr': []},
    'stop': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_stop_parser', [], {}], ['_resolve_switchyard_project', ['Project 453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY project453', ['stop', 'Project', '453']], {}], ['suspend_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/project453.json'}]], 'stdout': ['switchyard: p453 is suspended. Its board database, history, worktrees, credentials, provider state and session records are untouched; `switchyard start p453` brings it back.'], 'stderr': []},
    'stop, partly': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_stop_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['stop', 'p453']], {}], ['suspend_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}]], 'stdout': ['switchyard: the board would not stop', 'switchyard: p453 is partially stopped; nothing was removed and `switchyard start p453` still resumes what is down.'], 'stderr': []},
    'start': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_start_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['start', 'p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['launch_project', ['PREPARED CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'mode': 'start', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'report_session_records': True}]], 'stdout': [], 'stderr': []},
    'start, a resumption problem': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_start_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['start', 'p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}]], 'stdout': ['switchyard: the listener would not start', 'switchyard: the board would not start'], 'stderr': []},
    "start, the launch's own answer": {'result': 3, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_start_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['start', 'p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['launch_project', ['PREPARED CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'mode': 'start', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'report_session_records': True}]], 'stdout': [], 'stderr': []},
    'teardown': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_teardown_parser', [], {}], ['switchyard_teardown_command', ['p453'], {'dry_run': True, 'confirm': 'p453', 'drop_nonempty_board': True, 'destroy_registered_tenant': True, 'remove_owner_home': True, 'remove_owner_user': True, 'owner_user': 'p453-agent'}]], 'stdout': [], 'stderr': []},
    'status, every project': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_status_parser', [], {}], ['switchyard_status_command', [], {'json_output': False}]], 'stdout': [], 'stderr': []},
    'status, one project, JSON': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_status_parser', [], {}], ['_resolve_switchyard_project', ['Project 453'], {}], ['switchyard_status_command', [], {'json_output': True, 'project': 'project453'}]], 'stdout': [], 'stderr': []},
    'validate-models': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['Project 453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY project453', ['validate-models', 'Project', '453']], {}], ['switchyard_validate_models_command', ['Project 453'], {}]], 'stdout': [], 'stderr': []},
    'validate-models without a project': {'result': {'raised': 'SystemExit', 'message': 'switchyard validate-models requires <project>', 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}]], 'stdout': [], 'stderr': []},
    'a verb in mixed case': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_status_parser', [], {}], ['switchyard_status_command', [], {'json_output': False}]], 'stdout': [], 'stderr': []},
    'an unknown project': {'result': {'raised': 'SystemExit', 'message': "switchyard: no project named 'nobody here' (syrd453)", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['nobody here'], {}]], 'stdout': [], 'stderr': []},
    'an unknown project for a verb': {'result': {'raised': 'SystemExit', 'message': "switchyard: no project named 'nobody' (syrd453)", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_stop_parser', [], {}], ['_resolve_switchyard_project', ['nobody'], {}]], 'stdout': [], 'stderr': []},
    'a config that cannot be loaded': {'result': {'raised': 'SystemExit', 'message': "switchyard: p453's configuration cannot be read (syrd453)", 'code_type': 'str'}, 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_upgrade_parser', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['upgrade', 'p453']], {}]], 'stdout': [], 'stderr': []},
    'the ordinary launch': {'result': 0, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['Project 453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY project453', ['Project', '453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/project453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['run_switchyard_launch_first_run_auth', ['PREPARED CONFIG'], {}], ['stop_before_launch_for_missing_owner_clis', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unauthenticated_providers', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unknown_models', ['FIRST-RUN REPORT'], {'project': 'p453'}], ['launch_project', ['PREPARED CONFIG'], {'config_path': 'PATH TMP/cfg/project453.json', 'mode': 'start', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'report_session_records': True}], ['report_first_run_auth_warnings', ['FIRST-RUN REPORT'], {}]], 'stdout': [], 'stderr': []},
    'the ordinary launch, a resumption problem': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}]], 'stdout': ['switchyard: the board would not start'], 'stderr': []},
    'the ordinary launch, missing owner CLIs': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['run_switchyard_launch_first_run_auth', ['PREPARED CONFIG'], {}], ['stop_before_launch_for_missing_owner_clis', ['FIRST-RUN REPORT'], {}]], 'stdout': [], 'stderr': []},
    'the ordinary launch, unauthenticated providers': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['run_switchyard_launch_first_run_auth', ['PREPARED CONFIG'], {}], ['stop_before_launch_for_missing_owner_clis', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unauthenticated_providers', ['FIRST-RUN REPORT'], {}]], 'stdout': [], 'stderr': []},
    'the ordinary launch, unknown models': {'result': 1, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['run_switchyard_launch_first_run_auth', ['PREPARED CONFIG'], {}], ['stop_before_launch_for_missing_owner_clis', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unauthenticated_providers', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unknown_models', ['FIRST-RUN REPORT'], {'project': 'p453'}]], 'stdout': [], 'stderr': []},
    'the ordinary launch fails': {'result': 4, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['p453'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY p453', ['p453']], {}], ['resume_tenant', ['CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json'}], ['prepare_project_desktop', ['CONFIG'], {}], ['run_switchyard_launch_first_run_auth', ['PREPARED CONFIG'], {}], ['stop_before_launch_for_missing_owner_clis', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unauthenticated_providers', ['FIRST-RUN REPORT'], {}], ['stop_before_launch_for_unknown_models', ['FIRST-RUN REPORT'], {'project': 'p453'}], ['launch_project', ['PREPARED CONFIG'], {'config_path': 'PATH TMP/cfg/p453.json', 'mode': 'start', 'script_path': 'PATH LAUNCHER_DIR/team-launcher', 'report_session_records': True}]], 'stdout': [], 'stderr': []},
    "a command's own answer passed back": {'result': 9, 'type': 'int', 'calls': [['report_installed_release_version', [], {}], ['_build_switchyard_register_parser', [], {}], ['switchyard_register_command', ['PATH TMP/cfg.json'], {}]], 'stdout': [], 'stderr': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold453.py` (which ran them on the baseline) ------------------------------------
# A case runs `switchyard_main` on an argv and records, in order, every step it took, what it printed, and the answer:
# the command's return code, or the exact SystemExit. Every command it can hand off to, and every host-facing step on
# the way -- the requires-root probe, the release notice, the menu, help and version text, the project resolution and
# config loading, the tenant suspension and resumption, the desktop, the first-run checks and their reports -- is a
# recorder standing in on the launcher, and the four commands it imports when it runs (the privileged front door,
# board-skill, onboarding-readiness and workflow-manage) are recorders on their own modules; no command path runs. The
# verb parsers are the launcher's own, recorded there and passed through. Every path is in a test-owned temporary tree.
CASES = {
    "the wrapper's requires-root probe, root": {"argv": ["--switchyard-wrapper-requires-root", "upgrade", "p453"], "root": True},
    "the wrapper's requires-root probe, no root": {"argv": ["--switchyard-wrapper-requires-root", "status"]},
    "the menu": {"argv": []},
    "argv from sys.argv": {"argv": None, "sys_argv": ["switchyard", "status", "--json"]},
    "help, -h": {"argv": ["-h"]},
    "help, --help": {"argv": ["--help", "ignored"]},
    "help, help": {"argv": ["help"]},
    "version, --version": {"argv": ["--version"]},
    "version, version": {"argv": ["version"]},
    "new, every option": {"argv": ["new", "--slug", "p453", "--agent-name", "p453-agent", "--project-name", "P 453", "--project-path", "@/proj",
                                   "--from", "@/a.json", "--source-repo", "@/src", "--workflow-config", "@/wf.json", "--commit-git-dir", "@/git",
                                   "--output-dir", "@/out", "--agent-cli-policy", "promote-local", "--agent-cli-source", "claude=@/c",
                                   "--agent-cli-source", "codex=@/x", "--port", "8453", "--database", "db453", "--yes", "--desktop-policy", "@/pol.json",
                                   "--headless", "--desktop-gui-user", "gui", "--allow-existing-owner-user", "--agy-credential-source", "src-user",
                                   "--no-agy-credential", "--layout", "viewer", "--no-git-init"]},
    "new, the defaults": {"argv": ["new"]},
    "new, in capitals": {"argv": ["NEW"]},
    "new, a bad option": {"argv": ["new", "--port", "many"]},
    "agy-credential show": {"argv": ["agy-credential", "show"]},
    "agy-credential set": {"argv": ["agy-credential", "set", "someone"]},
    "agy-credential set without a user": {"argv": ["agy-credential", "set"]},
    "agy-credential clear": {"argv": ["agy-credential", "clear"]},
    "agy-credential, an unknown action": {"argv": ["agy-credential", "rotate"]},
    "seed-role-credentials": {"argv": ["seed-role-credentials", "p453"]},
    "seed-role-credentials, one role, reseeded": {"argv": ["seed-role-credentials", "p453", "--role", "ops", "--reseed"]},
    "set-owner-identity, a key": {"argv": ["set-owner-identity", "p453", "--key-name", "id_ed25519", "--host-alias", "gh-p453", "--host", "forge.example", "--dry-run"]},
    "set-owner-identity, a key, the defaults": {"argv": ["set-owner-identity", "p453", "--key-name", "id_ed25519"]},
    "set-owner-identity, cleared": {"argv": ["set-owner-identity", "p453", "--clear"]},
    "set-owner-identity, both": {"argv": ["set-owner-identity", "p453", "--clear", "--key-name", "k"]},
    "set-owner-identity, neither": {"argv": ["set-owner-identity", "p453"]},
    "register": {"argv": ["register", "@/cfg.json"]},
    "repair-boundary": {"argv": ["repair-boundary", "p453", "--apply"]},
    "approve-desktop": {"argv": ["approve-desktop", "--gui-user", "gui", "--reference", "ticket 1", "--show"]},
    "approve-desktop, revoked": {"argv": ["approve-desktop", "--revoke"]},
    "rebind-workflow-panes": {"argv": ["rebind-workflow-panes", "p453", "--apply", "--expect", "abc", "--runtime", " ops = codex ", "--runtime", "qa=claude",
                                       "--slot", "ops=3", "--slot", " qa = 4 ", "--config", "@/c.json"]},
    "rebind-workflow-panes, a runtime without a role": {"argv": ["rebind-workflow-panes", "p453", "--runtime", "=codex"]},
    "rebind-workflow-panes, a runtime without =": {"argv": ["rebind-workflow-panes", "p453", "--runtime", "ops"]},
    "rebind-workflow-panes, a runtime without a value": {"argv": ["rebind-workflow-panes", "p453", "--runtime", "ops= "]},
    "rebind-workflow-panes, a slot that is not a number": {"argv": ["rebind-workflow-panes", "p453", "--slot", "ops=three"]},
    "migrate-workflow": {"argv": ["migrate-workflow", "p453", "--apply", "--config", "@/c.json"]},
    "adopt-workflow": {"argv": ["adopt-workflow", "p453", "--apply", "--despite-board", "because", "--config", "@/c.json"]},
    "resume-provision": {"argv": ["resume-provision", "p453", "--source-repo", "@/src", "--config", "@/c.json"]},
    "upgrade, every option": {"argv": ["upgrade", "p453", "--dry-run", "--deploy-ref", "v2", "--source-repo", "@/src", "--commit-git-dir", "@/git",
                                       "--desktop-policy", "@/pol.json", "--upstream-report-url", "http://up", "--upstream-report-token-file", "@/tok",
                                       "--publish-remote", "pub"]},
    "upgrade, the defaults": {"argv": ["upgrade", "p453"]},
    "install-shared-release": {"argv": ["install-shared-release", "--commit", "abc123", "--rollback", "--dry-run"]},
    "privileged-action": {"argv": ["privileged-action", "p453", "restart-board", "a=1", "b=x=y", "--dry-run"]},
    "privileged-action, a value without =": {"argv": ["privileged-action", "p453", "restart-board", "a"]},
    "privileged-action, an empty key": {"argv": ["privileged-action", "p453", "restart-board", "=1"]},
    "privileged-action, a key twice": {"argv": ["privileged-action", "p453", "restart-board", "a=1", "a=2"]},
    "rollout-log": {"argv": ["rollout-log", "p453", "--attempt", "7", "--output"]},
    "rollout-log, a name that is not the slug": {"argv": ["rollout-log", "P453"]},
    "publication-status": {"argv": ["publication-status", "p453", "--verify"]},
    "cutover-roles, retired": {"argv": ["cutover-roles", "p453", "--dry-run"]},
    "finish-upgrade": {"argv": ["finish-upgrade", "p453", "--dry-run", "--deploy-ref", "v2", "--source-repo", "@/src", "--commit-git-dir", "@/git"]},
    "deploy-release": {"argv": ["deploy-release", "p453", "--commit", "e" * 40]},
    "release-status": {"argv": ["release-status", "p453", "--close"]},
    "worker-pool list": {"argv": ["worker-pool", "p453", "list"]},
    "worker-pool restart, a member": {"argv": ["worker-pool", "p453", "restart", " pool-3 ", "--apply", "--force", "--out", "@/o", "--journal", "@/j"]},
    "worker-pool restart, no member": {"argv": ["worker-pool", "p453", "restart"]},
    "worker-pool plan, no member needed": {"argv": ["worker-pool", "p453", "plan"]},
    "add-role": {"argv": ["add-role", "p453", "qa", "--cli", "claude", "--audit", "--slot", "2", "--detached", "--relayout", "--no-start"]},
    "add-role, the defaults": {"argv": ["add-role", "p453", "qa"]},
    "board-skill": {"argv": ["board-skill", "status", "--json"]},
    "onboarding-readiness": {"argv": ["onboarding-readiness", "p453"]},
    "role-prompt show": {"argv": ["role-prompt", "show", "ops", "--project", "p453"]},
    "role-prompt set, a prompt": {"argv": ["role-prompt", "set", "ops", "--project", "p453", "--prompt", "be brief"]},
    "role-prompt set, a prompt file": {"argv": ["role-prompt", "set", "ops", "--project", "p453", "--prompt-file", "@/prompt.txt"]},
    "role-prompt clear, the project from the pane": {"argv": ["role-prompt", "clear", "ops"], "env": {"TICKET_BOARD_PROJECT": "p453-pane"}},
    "role-prompt, no project": {"argv": ["role-prompt", "show", "ops"]},
    "role-prompt, a blank project": {"argv": ["role-prompt", "show", "ops", "--project", "  "]},
    "design-stage, a dry run": {"argv": ["design-stage", "--project", "p453", "--dry-run"]},
    "design-stage, the project from the pane": {"argv": ["design-stage"], "env": {"TICKET_BOARD_PROJECT": "p453-pane"}},
    "design-stage, no project": {"argv": ["design-stage"]},
    "set-role-runtime": {"argv": ["set-role-runtime", "p453", "ops", "--cli", "claude", "--model", "opus", "--force", "--reason", "why", "--dry-run"]},
    "set-role-runtime, an effort": {"argv": ["set-role-runtime", "p453", "ops", "--cli", "codex", "--model", "gpt-5.6-luna", "--effort", "xhigh", "--dry-run"]},
    "present": {"argv": ["present", "p453", "list"]},
    "attach": {"argv": ["attach", "p453", "ops", "--json"]},
    "replace-window, a project name in words": {"argv": ["replace-window", "Project", "453"]},
    "set-vcs-close-role": {"argv": ["set-vcs-close-role", "p453", "ops"]},
    "recover-display": {"argv": ["recover-display", "p453", "--anything"]},
    "stop": {"argv": ["stop", "Project", "453"]},
    "stop, partly": {"argv": ["stop", "p453"], "problems": ["the board would not stop"]},
    "start": {"argv": ["start", "p453"]},
    "start, a resumption problem": {"argv": ["start", "p453"], "problems": ["the listener would not start", "the board would not start"]},
    "start, the launch's own answer": {"argv": ["start", "p453"], "rc": 3},
    "teardown": {"argv": ["teardown", "p453", "--dry-run", "--confirm", "p453", "--owner-user", "p453-agent", "--drop-nonempty-board",
                          "--destroy-registered-tenant", "--remove-owner-home", "--remove-owner-user"]},
    "status, every project": {"argv": ["status"]},
    "status, one project, JSON": {"argv": ["status", "Project", "453", "--json"]},
    "validate-models": {"argv": ["validate-models", "Project", "453"]},
    "validate-models without a project": {"argv": ["validate-models"]},
    "a verb in mixed case": {"argv": ["Status"]},
    "an unknown project": {"argv": ["nobody", "here"], "unknown": True},
    "an unknown project for a verb": {"argv": ["stop", "nobody"], "unknown": True},
    "a config that cannot be loaded": {"argv": ["upgrade", "p453"], "unloadable": True},
    "the ordinary launch": {"argv": ["Project", "453"]},
    "the ordinary launch, a resumption problem": {"argv": ["p453"], "problems": ["the board would not start", "never reached"]},
    "the ordinary launch, missing owner CLIs": {"argv": ["p453"], "stop": "stop_before_launch_for_missing_owner_clis"},
    "the ordinary launch, unauthenticated providers": {"argv": ["p453"], "stop": "stop_before_launch_for_unauthenticated_providers"},
    "the ordinary launch, unknown models": {"argv": ["p453"], "stop": "stop_before_launch_for_unknown_models"},
    "the ordinary launch fails": {"argv": ["p453"], "rc": 4},
    "a command's own answer passed back": {"argv": ["register", "@/cfg.json"], "rc": 9},
}
FUNCTIONS = ("switchyard_main",)
SPECIAL = ("_resolve_switchyard_project", "_load_switchyard_project_config_for_command", "resume_tenant", "suspend_tenant", "prepare_project_desktop",
           "run_switchyard_launch_first_run_auth", "stop_before_launch_for_missing_owner_clis", "stop_before_launch_for_unauthenticated_providers",
           "stop_before_launch_for_unknown_models", "switchyard_help_text", "switchyard_version_text", "switchyard_invocation_requires_root",
           "release_rollback_commands")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s dispatcher; every command and host-facing step a recorder on `t` or its own module."""
    import argparse, contextlib, importlib, io, os, shutil, sys as _sys, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd453-")).resolve()
    launcher_dir = str(_P(t.__file__).resolve().parent)
    config = SimpleNamespace(project="p453", board_url="http://127.0.0.1:8453", tag="CONFIG")
    prepared = SimpleNamespace(project="p453", board_url="http://127.0.0.1:8453", tag="PREPARED CONFIG")
    report = SimpleNamespace(tag="FIRST-RUN REPORT")

    def norm(value):
        if isinstance(value, SimpleNamespace) and hasattr(value, "tag"):
            return value.tag
        if isinstance(value, argparse.Namespace):
            return {"Namespace": norm(dict(sorted(vars(value).items())))}
        if callable(value) and getattr(value, "__name__", "") == "<lambda>":
            return "LAMBDA"
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

    def passthrough(name):
        def f(*args, **kwargs):
            note(name, *args, **kwargs)
            return saved[name](*args, **kwargs)
        return f

    def resolve(project, *args, **kwargs):
        note("_resolve_switchyard_project", project, *args, **kwargs)
        if spec.get("unknown"):
            raise SystemExit(f"switchyard: no project named {project!r} (syrd453)")
        slug = project.lower().replace(" ", "")
        return SimpleNamespace(slug=slug, name=project, config_path=tmp / "cfg" / f"{slug}.json", tag=f"ENTRY {slug}")

    def load(entry, argv):
        note("_load_switchyard_project_config_for_command", entry, argv)
        if spec.get("unloadable"):
            raise SystemExit("switchyard: p453's configuration cannot be read (syrd453)")
        return config

    def privileged(project, action, values, *, dry_run, rollback_commands):
        note("privileged_front_door.privileged_action_command", project, action, values, dry_run=dry_run, rollback_commands=rollback_commands)
        return [spec.get("rc", 0), rollback_commands("p453-rollback")][0]

    names = sorted({n for reads in SEAMS.values() for n in reads})
    parsers = [n for n in names if n.startswith("_build_switchyard_")]
    stood = [n for n in names if n not in parsers and callable(getattr(t, n)) and not isinstance(getattr(t, n), type)]
    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *parsers, *stood)}
    nested = [("scripts.ticket_board.privileged_front_door", "privileged_action_command", privileged),
              ("scripts.board_skill_cli", "main", None), ("scripts.onboarding_readiness", "main", None), ("scripts.workflow_manage", "main", None)]
    nested_saved = []
    saved_argv, saved_env = _sys.argv, {k: os.environ.get(k) for k in ("TICKET_BOARD_PROJECT", "COLUMNS")}
    try:
        os.environ["COLUMNS"] = "100"  # argparse wraps its usage and errors to the terminal's width
        os.environ.pop("TICKET_BOARD_PROJECT", None)
        os.environ.update(spec.get("env", {}))
        for n in parsers:
            setattr(t, n, passthrough(n))
        for n in stood:
            setattr(t, n, command(n))
        t._resolve_switchyard_project = resolve
        t._load_switchyard_project_config_for_command = load
        t.resume_tenant = lambda config_, *, config_path: note("resume_tenant", config_, config_path=config_path) or list(spec.get("problems", []))
        t.suspend_tenant = lambda config_, *, config_path: note("suspend_tenant", config_, config_path=config_path) or list(spec.get("problems", []))
        t.prepare_project_desktop = lambda config_: note("prepare_project_desktop", config_) or prepared
        t.run_switchyard_launch_first_run_auth = lambda config_: note("run_switchyard_launch_first_run_auth", config_) or report
        for n in ("stop_before_launch_for_missing_owner_clis", "stop_before_launch_for_unauthenticated_providers", "stop_before_launch_for_unknown_models"):
            setattr(t, n, (lambda name: lambda *a, **k: note(name, *a, **k) or spec.get("stop") == name)(n))
        t.switchyard_help_text = lambda: note("switchyard_help_text") or "SWITCHYARD HELP (syrd453)\n"
        t.switchyard_version_text = lambda: note("switchyard_version_text") or "switchyard SYRD453-VERSION"
        t.switchyard_invocation_requires_root = lambda argv: note("switchyard_invocation_requires_root", argv) or bool(spec.get("root"))
        t.release_rollback_commands = lambda project: note("release_rollback_commands", project) or ["ROLLBACK"]
        for module_name, attr, stand in nested:
            module = importlib.import_module(module_name)
            nested_saved.append((module, attr, getattr(module, attr)))
            label = f"{module_name.rsplit('.', 1)[-1]}.{attr}"
            setattr(module, attr, stand or (lambda label: lambda *a, **k: note(label, *a, **k) or spec.get("rc", 0))(label))
        # argparse names itself after sys.argv[0] where a parser sets no prog: always the wrapper's own name.
        _sys.argv = [a.replace("@", str(tmp)) if a.startswith("@/") else a for a in spec.get("sys_argv", ["switchyard"])]
        dispatch = getattr(holder, "switchyard_main")
        argv = None if spec["argv"] is None else [a.replace("@", str(tmp)) if "@/" in a else a for a in spec["argv"]]
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
                    "stdout": norm(shown.getvalue()).splitlines(), "stderr": norm(said.getvalue()).splitlines()[-2:]}
        return {"result": got, "type": type(got).__name__, "calls": calls, "stdout": norm(shown.getvalue()).splitlines(),
                "stderr": norm(said.getvalue()).splitlines()[-2:]}
    finally:
        _sys.argv = saved_argv
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for module, attr, value in nested_saved:
            setattr(module, attr, value)
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


NESTED = ["from scripts.ticket_board import privileged_front_door", "from scripts import board_skill_cli",
          "from scripts import onboarding_readiness", "from scripts import workflow_manage"]


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.switchyard_dispatch as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.switchyard_dispatch", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.switchyard_dispatch")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.switchyard_dispatch as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "[(k, v.kind.name, v.default) for k, v in inspect.signature(m.switchyard_main).parameters.items()], "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'privileged_front_door', 'board_skill_cli', 'onboarding_readiness', "
                        f"'workflow_manage', *{sorted(n for n in SEAMS['switchyard_main'] if n != '__file__')!r})))")
        check(result.stdout.strip() == "True [('argv', 'POSITIONAL_OR_KEYWORD', None)] True",
              f"{' then '.join(order)}: one object; the same parameter and default; nothing of the launcher, its imports or the nested commands bound at load: {result.stdout}{result.stderr[-600:]}")
    import argparse
    import pathlib
    check(m.argparse is argparse and m.os is os and m.sys is sys and m.Path is pathlib.Path and t.os is m.os and t.sys is m.sys and t.Path is m.Path,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "switchyard_dispatch.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if getattr(n, "name", None) == "switchyard_main")
    through: dict[str, int] = {}
    for x in ast.walk(node):
        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
            through[x.attr] = through.get(x.attr, 0) + 1
    check(through == SEAMS["switchyard_main"], f"each launcher name, __file__ and TEAM_LAUNCHER_NAME among them, read through it exactly as often as before: {through}")
    imports = [ast.unparse(x) for x in sorted((x for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))), key=lambda x: (x.lineno, x.col_offset))]
    check(imports == ["from scripts import team_launcher as launcher", *NESTED] and ast.unparse(node.body[0]) == imports[0],
          f"the launcher imported first thing, and the four nested imports kept where they were, in order: {imports}")
    skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *node.args.defaults]
            if part is not None for y in ast.walk(part)}
    bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)
                   and x.id in SEAMS["switchyard_main"] and id(x) not in skip})
    check(bare == [], f"none of them read past it, the module's own __file__ least of all: {bare}")
    paths = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, ast.Call) and ast.unparse(x.func).endswith(".with_name")]
    check(paths == ["Path(launcher.__file__).resolve().with_name(launcher.TEAM_LAUNCHER_NAME)"] * 3,
          f"the team-launcher script is named from the launcher's own file, at each of its three places (add-role, start, the launch): {paths}")
    branches = [ast.unparse(n.test) for n in sorted((n for n in ast.walk(node) if isinstance(n, ast.If)), key=lambda n: (n.lineno, n.col_offset))]
    check([b.replace("launcher.", "") for b in branches] == BRANCHES, f"every branch, in the baseline's order: {len(branches)}")
    shape = [sum(isinstance(x, k) for x in ast.walk(node)) for k in (ast.Return, ast.Raise, ast.Try, ast.If)]
    check(shape == SHAPE, f"its returns, refusals, trys and branches, as many as in the baseline: {shape}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import argparse", "import os", "import sys", "from pathlib import Path"]
          and not [n for n in tree.body if isinstance(n, ast.If)], f"the standard library only, nothing for annotations: {top}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the one function, and nothing else: {names}")


def test_the_launcher_reexports_it_and_both_wrappers_reach_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.switchyard_dispatch"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the dispatcher, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read it")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED) - {"__file__"}
    check(not defined & set(MOVED) and seams | {"switchyard_menu_command", "switchyard_validate_models_command", "switchyard_pane_launcher_for", "main"}
          <= defined | exported, "the launcher defines it no more, and keeps its two commands, main and every name it reads, its own or re-exported")
    # The fourth use of the expression is switchyard_pane_launcher_for's own, which stays in the launcher, unchanged.
    stays = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "switchyard_pane_launcher_for")
    check([ast.unparse(x) for x in ast.walk(stays) if isinstance(x, ast.Call) and ast.unparse(x.func).endswith(".with_name")]
          == ["Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME)"], "the launcher's own pane-launcher default still names the script from its own file")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition names it, as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's name, or reads it at module level: {past} {loose}")
    for wrapper in ("scripts/switchyard", "switchyard"):
        source = ast.parse((ROOT / wrapper).read_text(encoding="utf-8"))
        check([ast.unparse(n) for n in source.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.team_launcher"]
              == ["from scripts.team_launcher import switchyard_main"],
              f"{wrapper} imports the dispatcher from the launcher, so it runs the re-exported function")


def test_the_real_wrappers_run_the_dispatcher() -> None:
    # Both real `switchyard` wrappers, each run as __main__ in a Python of its own, answer the requires-root probe their
    # sudo wrapper asks -- the dispatcher's first branch, which returns before the release notice or anything else runs.
    for wrapper in ("scripts/switchyard", "switchyard"):
        answers = [python(f"import runpy, sys; sys.argv = [{wrapper!r}, '--switchyard-wrapper-requires-root', *{argv!r}]; "
                          f"runpy.run_path({wrapper!r}, run_name='__main__')") for argv in (["status"], ["upgrade", "p453"])]
        check([(a.returncode, a.stdout, a.stderr) for a in answers] == [(0, "no-root\n", ""), (0, "requires-root\n", "")],
              f"{wrapper}: the dispatcher answers the probe as before: {[(a.returncode, a.stdout, a.stderr[-300:]) for a in answers]}")


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
        return [c[0] for c in GOLDEN[label]["calls"] if not c[0].startswith("_build_switchyard_")]

    def call(label, seam):
        return next((c for c in GOLDEN[label]["calls"] if c[0] == seam), None)

    def refusal(label):
        r = result(label)
        return r["message"] if isinstance(r, dict) and r.get("raised") == "SystemExit" and r.get("code_type") == "str" else None

    def usage_error(label):
        return result(label) == {"raised": "SystemExit", "message": "2", "code_type": "int"}

    C = "PATH TMP/cfg/p453.json"
    # Before anything else.
    check(GOLDEN["the wrapper's requires-root probe, root"]["stdout"] == ["requires-root"] and GOLDEN["the wrapper's requires-root probe, no root"]["stdout"] == ["no-root"]
          and steps("the wrapper's requires-root probe, root") == ["switchyard_invocation_requires_root"]
          and call("the wrapper's requires-root probe, root", "switchyard_invocation_requires_root")[1] == [["upgrade", "p453"]],
          "the wrapper's probe is answered first, from the rest of the command line, before the release notice or anything else")
    check(all(steps(k)[0] == "report_installed_release_version" for k in GOLDEN if "requires-root probe" not in k and steps(k))
          and steps("the menu") == ["report_installed_release_version", "switchyard_menu_command"]
          and all(GOLDEN[k]["stdout"] == ["SWITCHYARD HELP (syrd453)"] for k in ("help, -h", "help, --help", "help, help"))
          and all(GOLDEN[k]["stdout"] == ["switchyard SYRD453-VERSION"] for k in ("version, --version", "version, version"))
          and call("argv from sys.argv", "switchyard_status_command") == ["switchyard_status_command", [], {"json_output": True}],
          "every other command starts with the release notice; no command is the menu; help and version print; no argv means sys.argv[1:]")
    check(steps("new, in capitals")[-1] == "switchyard_new_command" and steps("a verb in mixed case")[-1] == "switchyard_status_command"
          and call("new, the defaults", "switchyard_new_command")[2]["git_init"] is True and call("new, every option", "switchyard_new_command")[2]["git_init"] is False
          and call("new, every option", "switchyard_new_command")[2]["agent_cli_sources"] == ["claude=TMP/c", "codex=TMP/x"]
          and usage_error("new, a bad option"),
          "verbs match in any case; new hands every option on (--no-git-init as git_init=False)")
    check(refusal("agy-credential set without a user") == "switchyard: agy-credential set requires a user name"
          and call("agy-credential set", "switchyard_agy_credential_command") == ["switchyard_agy_credential_command", ["set"], {"source_user": "someone"}]
          and usage_error("agy-credential, an unknown action")
          and call("seed-role-credentials, one role, reseeded", "switchyard_seed_role_credentials_command")[2] == {"role_name": "ops", "reseed": True},
          "credential verbs: a set needs a user; seeding loads the project's config first")
    check(steps("set-owner-identity, cleared")[-1] == "clear_owner_github_identity_command"
          and call("set-owner-identity, a key", "set_owner_github_identity_command")[2] == {"config_path": C, "key_name": "id_ed25519", "host_alias": "gh-p453", "host": "forge.example", "dry_run": True}
          and call("set-owner-identity, a key, the defaults", "set_owner_github_identity_command")[2]["host"] == "github.com"
          and usage_error("set-owner-identity, both") and usage_error("set-owner-identity, neither"),
          "set-owner-identity takes exactly one of a key or --clear")
    r = call("rebind-workflow-panes", "switchyard_rebind_workflow_panes_command")[2]
    check(r["runtimes"] == {"ops": "codex", "qa": "claude"} and r["slots"] == {"ops": 3, "qa": 4} and r["apply"] is True and r["expect"] == "abc"
          and refusal("rebind-workflow-panes, a runtime without a role") == "switchyard: --runtime takes ROLE=RUNTIME, not '=codex'"
          and refusal("rebind-workflow-panes, a runtime without =") == "switchyard: --runtime takes ROLE=RUNTIME, not 'ops'"
          and refusal("rebind-workflow-panes, a runtime without a value") == "switchyard: --runtime takes ROLE=RUNTIME, not 'ops= '"
          and refusal("rebind-workflow-panes, a slot that is not a number") == "switchyard: --slot takes ROLE=SLOT, not 'ops=three'",
          "runtimes and slots are ROLE=VALUE pairs, stripped, slots whole numbers")
    v = call("privileged-action", "privileged_front_door.privileged_action_command")
    check(v[1] == ["p453", "restart-board", {"a": "1", "b": "x=y"}] and v[2] == {"dry_run": True, "rollback_commands": "LAMBDA"}
          and call("privileged-action", "release_rollback_commands") == ["release_rollback_commands", ["p453-rollback"], {}]
          and refusal("privileged-action, a value without =") == "switchyard: expected key=value, got 'a'"
          and refusal("privileged-action, an empty key") == "switchyard: expected key=value, got '=1'"
          and refusal("privileged-action, a key twice") == "switchyard: a was given twice",
          "privileged-action runs the front door imported when it runs, with key=value pairs and a rollback that reads the launcher when called")
    check(steps("rollout-log") == ["report_installed_release_version", "_resolve_switchyard_project", "rollout_log_command"]
          and call("rollout-log", "rollout_log_command") == ["rollout_log_command", ["p453"], {"attempt": "7", "output": True}]
          and call("rollout-log, a name that is not the slug", "rollout_log_command")[1] == ["p453"]
          and result("cutover-roles, retired") == 1 and "cutover-roles is retired for p453" in GOLDEN["cutover-roles, retired"]["stdout"][0],
          "rollout-log reads the journal by the resolved slug, not the name given, without loading the config; cutover-roles is retired")
    check(refusal("worker-pool restart, no member").startswith("switchyard: worker-pool restart needs the worker it acts on")
          and call("worker-pool restart, a member", "switchyard_worker_pool_command")[2]["member"] == "pool-3"
          and steps("worker-pool plan, no member needed")[-1] == "switchyard_worker_pool_command",
          "worker-pool actions that act on a worker need one")
    a = call("add-role, the defaults", "add_project_role_command")[2]
    check(a["script_path"] == "PATH LAUNCHER_DIR/team-launcher" and a["start"] is True and call("add-role", "add_project_role_command")[2]["start"] is False
          and call("start", "launch_project")[2]["script_path"] == "PATH LAUNCHER_DIR/team-launcher"
          and call("the ordinary launch", "launch_project")[2]["script_path"] == "PATH LAUNCHER_DIR/team-launcher",
          "add-role, start and the launch name the team-launcher script beside the launcher")
    check(call("board-skill", "board_skill_cli.main") == ["board_skill_cli.main", [["status", "--json"]], {"prog": "switchyard board-skill"}]
          and call("onboarding-readiness", "onboarding_readiness.main") == ["onboarding_readiness.main", [["p453"]], {}]
          and call("role-prompt show", "workflow_manage.main")[1] == [["show-role-prompt", "--role", "ops", "--board-url", "http://127.0.0.1:8453"]]
          and call("role-prompt set, a prompt", "workflow_manage.main")[1][0][-4:] == ["--config", C[5:], "--prompt", "be brief"]
          and call("role-prompt set, a prompt file", "workflow_manage.main")[1][0][-2:] == ["--prompt-file", "TMP/prompt.txt"]
          and call("role-prompt clear, the project from the pane", "_resolve_switchyard_project")[1] == ["p453-pane"]
          and refusal("role-prompt, no project") == refusal("role-prompt, a blank project")
          == "switchyard: no project selected; pass --project or run where TICKET_BOARD_PROJECT is set",
          "board-skill, onboarding-readiness and role-prompt hand off to the modules they import when they run")
    check(call("replace-window, a project name in words", "_resolve_switchyard_project")[1] == ["Project 453"]
          and call("stop", "_resolve_switchyard_project")[1] == ["Project 453"]
          and call("status, one project, JSON", "switchyard_status_command") == ["switchyard_status_command", [], {"json_output": True, "project": "project453"}]
          and "_load_switchyard_project_config_for_command" not in steps("status, one project, JSON")
          and refusal("validate-models without a project") == "switchyard validate-models requires <project>"
          and call("validate-models", "switchyard_validate_models_command")[1] == ["Project 453"],
          "a project named in words is joined; status resolves but does not load; validate-models loads, then validates by name")
    check(GOLDEN["stop"]["stdout"][0].startswith("switchyard: p453 is suspended.") and result("stop, partly") == 1
          and GOLDEN["stop, partly"]["stdout"][0] == "switchyard: the board would not stop"
          and GOLDEN["stop, partly"]["stdout"][1].startswith("switchyard: p453 is partially stopped; nothing was removed"),
          "stop suspends the whole tenant, and says what it could not stop")
    check(result("start, a resumption problem") == 1 and GOLDEN["start, a resumption problem"]["stdout"] == ["switchyard: the listener would not start", "switchyard: the board would not start"]
          and "prepare_project_desktop" not in steps("start, a resumption problem") and result("start, the launch's own answer") == 3,
          "start resumes the tenant, prints every problem and stops there; otherwise it prepares the desktop and launches")
    check(refusal("an unknown project") == "switchyard: no project named 'nobody here' (syrd453)" and refusal("an unknown project for a verb") == "switchyard: no project named 'nobody' (syrd453)"
          and refusal("a config that cannot be loaded") == "switchyard: p453's configuration cannot be read (syrd453)",
          "an unknown project, or a config that cannot be read, stops the command before anything else")
    order = ["report_installed_release_version", "_resolve_switchyard_project", "_load_switchyard_project_config_for_command", "resume_tenant", "prepare_project_desktop",
             "run_switchyard_launch_first_run_auth", "stop_before_launch_for_missing_owner_clis", "stop_before_launch_for_unauthenticated_providers",
             "stop_before_launch_for_unknown_models", "launch_project", "report_first_run_auth_warnings"]
    check(steps("the ordinary launch") == order and result("the ordinary launch") == 0
          and GOLDEN["the ordinary launch, a resumption problem"]["stdout"] == ["switchyard: the board would not start"]
          and result("the ordinary launch, a resumption problem") == 1
          and steps("the ordinary launch, missing owner CLIs") == order[:7] and steps("the ordinary launch, unauthenticated providers") == order[:8]
          and steps("the ordinary launch, unknown models") == order[:9]
          and result("the ordinary launch fails") == 4 and steps("the ordinary launch fails") == order[:10],
          "the ordinary launch: resume (stopping at its first problem, the baseline's behaviour), prepare, the three first-run stops, launch, then the warnings")
    check(result("a command's own answer passed back") == 9, "every command's own answer is switchyard_main's")


def test_every_launcher_seam_is_reached() -> None:
    # Every function it reads on the launcher is a recorder there; the constants are the launcher's own.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name))}
    check(constants == {"__file__", "TEAM_LAUNCHER_NAME", "WORKER_POOL_ACTIONS"} and names - constants <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it_and_both_wrappers_reach_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"switchyard_dispatch_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
