#!/usr/bin/env python3
"""SYRD-424: the project upgrade command, against the launcher it came out of.

`upgrade_project_command` -- the orchestration behind `team-launcher upgrade`
and `switchyard upgrade`, which runs the six upgrade phases in order and hands
each what the earlier ones settled -- moved unchanged into
`scripts/project_upgrade_command.py`; the launcher re-exports it. This pins
what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads nothing of Switchyard's; its definition-time defaults are the
  very `subprocess.run` and `print` the launcher bound; the config type is
  imported under TYPE_CHECKING only.
- **Seams (rule 24):** the six phases and their four continuation types are
  read through the launcher as often as before, so a patch there reaches them:
  every case below stands each phase in there, recorded, and rebinds a phase
  and a continuation type on the launcher to see the effect.
- **Callers:** `main` and `switchyard_main` dispatch `upgrade` to the
  launcher's name.
- **The behaviour is the baseline's:** the phase order, every value each phase
  hands on and which later phase receives it, each refusal (or a foreign
  answer) returned as it came before anything later runs, errors reaching the
  caller, the defaults forwarded, and both entry points' parsing and dispatch.
  `GOLDEN` below was produced by running the BASELINE launcher's own function
  over the very cases embedded here (`gold424.py`), not typed; it is
  byte-identical whether generated under `env -i` or in a normal role pane.

No phase runs: nothing is pinned, staged, deployed, restarted or migrated, and
no privilege is asked for. Spawns, every exec, signals, account and group
lookups and socket connections are refused for each case.
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
from scripts import project_upgrade_command as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ('upgrade_project_command',)
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'upgrade_project_command': {'UpgradeIdentitiesDone': 1, 'UpgradeSourcePinned': 1, 'UpgradeStateReady': 1, 'UpgradeToolingStaged': 1, '_finish_upgrade': 1, '_pin_upgrade_source': 1, '_recover_upgrade_state': 1, '_refresh_upgrade_artifacts': 1, '_stage_upgrade_tooling': 1, '_upgrade_identities_and_accounts': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the command that names it, and how often.
DISPATCH = {'switchyard_main': {'upgrade_project_command': 1}, 'main': {'upgrade_project_command': 1}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {}
#: The BASELINE's own behaviour for the cases below (`gold424.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'upgrade: every phase goes on': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'upgrade: every phase goes on, applying': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': False, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': False, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'upgrade: only the config path given': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': None, 'deploy_ref': None, 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'source_repo': None, 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '', 'upstream_report_url': ''}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': '', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
    'upgrade: only the config path, the default runner and print': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': None, 'deploy_ref': None, 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'source_repo': None, 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '', 'upstream_report_url': ''}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': '', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
    'upgrade: U1 refuses': {'result': 11, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}]], 'printed': []},
    'upgrade: U2 refuses': {'result': 12, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}]], 'printed': []},
    'upgrade: U4 refuses': {'result': 14, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}]], 'printed': []},
    'upgrade: U5 refuses': {'result': 15, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}]], 'printed': []},
    'upgrade: U6 answers nonzero': {'result': 16, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'upgrade: U1 answers a refusal of zero': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}]], 'printed': []},
    'upgrade: U1 answers something else': {'result': "NAMESPACE _pin_upgrade_source's foreign answer", 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}]], 'printed': []},
    'upgrade: U2 answers something else': {'result': "NAMESPACE _recover_upgrade_state's foreign answer", 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}]], 'printed': []},
    'upgrade: U4 answers something else': {'result': "NAMESPACE _stage_upgrade_tooling's foreign answer", 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}]], 'printed': []},
    'upgrade: U5 answers something else': {'result': "NAMESPACE _upgrade_identities_and_accounts's foreign answer", 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}]], 'printed': []},
    'upgrade: U1 raises': {'result': {'raised': 'RuntimeError', 'message': 'syrd424: _pin_upgrade_source failed'}, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}]], 'printed': []},
    'upgrade: U3 raises': {'result': {'raised': 'RuntimeError', 'message': 'syrd424: _refresh_upgrade_artifacts failed'}, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}]], 'printed': []},
    'upgrade: U6 raises': {'result': {'raised': 'RuntimeError', 'message': 'syrd424: _finish_upgrade failed'}, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'upgrade: the pinned values are empty': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': None, 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': None}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': None, 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': None, 'deploy_ref_chosen': False, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': None, 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': None, 'desktop_choice': None, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': None, 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': None, 'desktop_choice': None, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'upgrade: U3 hands back the same config': {'result': 0, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U2'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'upgrade: a continuation type rebound on the launcher': {'result': {'UpgradeToolingStaged': {'trusted_release_root': 'PATH /nonexistent/syrd424/release-root', 'publication_detail': 'syrd424 detail'}}, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}]], 'printed': []},
    'upgrade: a phase rebound on the launcher': {'result': 42, 'calls': [['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling'}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH /nonexistent/syrd424/policy.json', 'dry_run': True, 'print_func': 'PRINT recorder', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'registry_dir': 'PATH /nonexistent/syrd424/registry', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '/nonexistent/syrd424/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'RUNNER recorder', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'RUNNER recorder', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': 'PATH /nonexistent/syrd424/tooling', 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['the rebound finish', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH /nonexistent/syrd424/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'PRINT recorder', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'RUNNER recorder'}]], 'printed': []},
    'team-launcher upgrade': {'result': 0, 'calls': [['_resolve_launcher_project_config', ['syrd424'], {'explicit_config': 'PATH TMP/syrd424.json'}], ['load_project_config', ['syrd424', 'PATH TMP/syrd424.json'], {}], ['upgrade_project_command', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'publish_remote': '', 'source_repo': 'PATH TMP/src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH TMP/src', 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': '', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
    'team-launcher upgrade, bare': {'result': 0, 'calls': [['_resolve_launcher_project_config', ['syrd424'], {'explicit_config': 'PATH TMP/syrd424.json'}], ['load_project_config', ['syrd424', 'PATH TMP/syrd424.json'], {}], ['upgrade_project_command', ['NAMESPACE config as loaded'], {'commit_git_dir': None, 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': None, 'desktop_policy': None, 'dry_run': False, 'publish_remote': '', 'source_repo': None, 'upstream_report_token_file': '', 'upstream_report_url': ''}], ['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': None, 'deploy_ref': None, 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'source_repo': None, 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '', 'upstream_report_url': ''}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': '', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
    'team-launcher upgrade, U2 refuses': {'result': 3, 'calls': [['_resolve_launcher_project_config', ['syrd424'], {'explicit_config': 'PATH TMP/syrd424.json'}], ['load_project_config', ['syrd424', 'PATH TMP/syrd424.json'], {}], ['upgrade_project_command', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'publish_remote': '', 'source_repo': 'PATH TMP/src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH TMP/src', 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}]], 'printed': []},
    'switchyard upgrade': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['syrd424'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['upgrade', 'syrd424', '--dry-run', '--deploy-ref', 'v424', '--source-repo', 'TMP/src', '--commit-git-dir', 'syrd424-gitdir', '--desktop-policy', 'headless', '--upstream-report-url', 'http://syrd424.invalid/board', '--upstream-report-token-file', 'TMP/report.env', '--publish-remote', 'upstream']], {}], ['upgrade_project_command', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'publish_remote': 'upstream', 'source_repo': 'PATH TMP/src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'publish_remote': 'upstream', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH TMP/src', 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
    'switchyard upgrade, bare': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['syrd424'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['upgrade', 'syrd424']], {}], ['upgrade_project_command', ['NAMESPACE config as loaded'], {'commit_git_dir': None, 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': None, 'desktop_policy': None, 'dry_run': False, 'publish_remote': '', 'source_repo': None, 'upstream_report_token_file': '', 'upstream_report_url': ''}], ['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': None, 'deploy_ref': None, 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'source_repo': None, 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': None, 'dry_run': False, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': '', 'upstream_report_url': ''}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': '', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': '', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': False, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
    'switchyard upgrade, U6 answers nonzero': {'result': 9, 'calls': [['report_installed_release_version', [], {}], ['_resolve_switchyard_project', ['syrd424'], {}], ['_load_switchyard_project_config_for_command', ['NAMESPACE entry', ['upgrade', 'syrd424', '--dry-run', '--deploy-ref', 'v424', '--source-repo', 'TMP/src', '--commit-git-dir', 'syrd424-gitdir', '--desktop-policy', 'headless', '--upstream-report-url', 'http://syrd424.invalid/board', '--upstream-report-token-file', 'TMP/report.env', '--publish-remote', 'upstream']], {}], ['upgrade_project_command', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'publish_remote': 'upstream', 'source_repo': 'PATH TMP/src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_pin_upgrade_source', ['NAMESPACE config as loaded'], {'commit_git_dir': 'syrd424-gitdir', 'deploy_ref': 'v424', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'publish_remote': 'upstream', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH TMP/src', 'tooling_root': None}], ['_recover_upgrade_state', ['NAMESPACE config as loaded'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_policy': 'PATH headless', 'dry_run': True, 'print_func': 'CALLABLE builtins.print', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/pinned-src'}], ['_refresh_upgrade_artifacts', ['NAMESPACE config after U2'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'registry_dir': None, 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'upstream_report_token_file': 'TMP/report.env', 'upstream_report_url': 'http://syrd424.invalid/board'}], ['_stage_upgrade_tooling', ['NAMESPACE config after U3'], {'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'deploy_ref_chosen': True, 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': '', 'publish_remote': 'upstream', 'runner': 'CALLABLE subprocess.run', 'tooling_root': None, 'trusted_release_root': None}], ['_upgrade_identities_and_accounts', ['NAMESPACE config after U3'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'cutover': 'syrd424 cutover', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'publication_detail': 'syrd424 detail', 'publish_remote': 'upstream', 'release_report_config': 'NAMESPACE config after U2', 'runner': 'CALLABLE subprocess.run', 'source_repo': 'PATH /nonexistent/syrd424/state-src', 'tooling_root': None, 'trusted_release_root': 'PATH /nonexistent/syrd424/release-root'}], ['_finish_upgrade', ['NAMESPACE config after U5'], {'commit_git_dir': 'syrd424 pinned gitdir', 'config_path': 'PATH TMP/syrd424.json', 'deploy_ref': 'v424-pinned', 'desktop_choice': 'syrd424 desktop choice', 'dry_run': True, 'effective_source_repo': 'PATH /nonexistent/syrd424/effective-src', 'print_func': 'CALLABLE builtins.print', 'release_report_config': 'NAMESPACE report config after U5', 'runner': 'CALLABLE subprocess.run'}]], 'printed': []},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold424.py` (which ran them on the baseline) ------------------------------------
# A case runs the upgrade's orchestration with every phase a stand-in on the launcher: each records what it is handed
# and answers from the case -- its continuation (built from the real continuation type, with values no other phase
# uses), an int refusal, an error, or an object of the wrong type. Nothing is pinned, staged, deployed, restarted or
# migrated. The two entry points run their real parsers and dispatch, with the project lookup and config load stood in.
# Recorded, in order: every phase call with its arguments, everything printed, and the result or what was raised.
P = "/nonexistent/syrd424"
FULL = {"dry_run": True, "desktop_policy": f"{P}/policy.json", "source_repo": f"{P}/src", "commit_git_dir": "syrd424-gitdir", "deploy_ref": "v424",
        "tooling_root": f"{P}/tooling", "publish_remote": "upstream", "upstream_report_url": "http://syrd424.invalid/board",
        "upstream_report_token_file": f"{P}/report.env", "registry_dir": f"{P}/registry", "runner": "recorder", "print_func": "recorder"}
PHASES = ("_pin_upgrade_source", "_recover_upgrade_state", "_refresh_upgrade_artifacts", "_stage_upgrade_tooling", "_upgrade_identities_and_accounts",
          "_finish_upgrade")
COMMAND = {
    "every phase goes on": {"kwargs": FULL},
    "every phase goes on, applying": {"kwargs": {**FULL, "dry_run": False}},
    "only the config path given": {"kwargs": {}},
    "only the config path, the default runner and print": {"kwargs": {"runner": "default", "print_func": "default"}},
    "U1 refuses": {"kwargs": FULL, "answers": {"_pin_upgrade_source": 11}},
    "U2 refuses": {"kwargs": FULL, "answers": {"_recover_upgrade_state": 12}},
    "U4 refuses": {"kwargs": FULL, "answers": {"_stage_upgrade_tooling": 14}},
    "U5 refuses": {"kwargs": FULL, "answers": {"_upgrade_identities_and_accounts": 15}},
    "U6 answers nonzero": {"kwargs": FULL, "answers": {"_finish_upgrade": 16}},
    "U1 answers a refusal of zero": {"kwargs": FULL, "answers": {"_pin_upgrade_source": 0}},
    "U1 answers something else": {"kwargs": FULL, "answers": {"_pin_upgrade_source": "foreign"}},
    "U2 answers something else": {"kwargs": FULL, "answers": {"_recover_upgrade_state": "foreign"}},
    "U4 answers something else": {"kwargs": FULL, "answers": {"_stage_upgrade_tooling": "foreign"}},
    "U5 answers something else": {"kwargs": FULL, "answers": {"_upgrade_identities_and_accounts": "foreign"}},
    "U1 raises": {"kwargs": FULL, "answers": {"_pin_upgrade_source": "raise"}},
    "U3 raises": {"kwargs": FULL, "answers": {"_refresh_upgrade_artifacts": "raise"}},
    "U6 raises": {"kwargs": FULL, "answers": {"_finish_upgrade": "raise"}},
    "the pinned values are empty": {"kwargs": FULL, "pinned": {"desktop_choice": None, "deploy_ref_chosen": False, "source_repo": None,
                                                              "commit_git_dir": None, "deploy_ref": None}},
    "U3 hands back the same config": {"kwargs": FULL, "answers": {"_refresh_upgrade_artifacts": "same"}},
    "a continuation type rebound on the launcher": {"kwargs": FULL, "rebind": "UpgradeToolingStaged"},
    "a phase rebound on the launcher": {"kwargs": FULL, "rebind": "_finish_upgrade"},
}
MAIN = ["syrd424", "upgrade", "--config", "@/syrd424.json", "--dry-run", "--deploy-ref", "v424", "--source-repo", "@/src", "--commit-git-dir", "syrd424-gitdir",
        "--desktop-policy", "headless", "--upstream-report-url", "http://syrd424.invalid/board", "--upstream-report-token-file", "@/report.env"]
SWITCHYARD = ["upgrade", "syrd424", "--dry-run", "--deploy-ref", "v424", "--source-repo", "@/src", "--commit-git-dir", "syrd424-gitdir",
              "--desktop-policy", "headless", "--upstream-report-url", "http://syrd424.invalid/board", "--upstream-report-token-file", "@/report.env",
              "--publish-remote", "upstream"]
CASES = {**{f"upgrade: {k}": {"call": "command", **v} for k, v in COMMAND.items()},
         "team-launcher upgrade": {"call": "main", "argv": MAIN},
         "team-launcher upgrade, bare": {"call": "main", "argv": ["syrd424", "upgrade", "--config", "@/syrd424.json"]},
         "team-launcher upgrade, U2 refuses": {"call": "main", "argv": MAIN, "answers": {"_recover_upgrade_state": 3}},
         "switchyard upgrade": {"call": "switchyard", "argv": SWITCHYARD},
         "switchyard upgrade, bare": {"call": "switchyard", "argv": ["upgrade", "syrd424"]},
         "switchyard upgrade, U6 answers nonzero": {"call": "switchyard", "argv": SWITCHYARD, "answers": {"_finish_upgrade": 9}}}
FUNCTIONS = ("upgrade_project_command",)


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One run of `holder`'s upgrade (or an entry point), every phase a recording stand-in on `t`."""
    import contextlib, dataclasses, shutil, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    printed: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd424-")).resolve()
    answers = spec.get("answers", {})

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"NAMESPACE {getattr(value, 'label', '?')}"
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {type(value).__name__: {f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if value is recorder_runner:
            return "RUNNER recorder"
        if value is show:
            return "PRINT recorder"
        if callable(value):
            return f"CALLABLE {getattr(value, '__module__', '?')}.{getattr(value, '__qualname__', '?')}"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def recorder_runner(argv, **kwargs):
        raise AssertionError(f"a stand-in phase never runs anything: {argv}")

    def show(line):
        printed.append(["print_func", norm(line)])

    class Stdout:
        def write(self, text):
            if text.strip():
                printed.append(["stdout", norm(text.rstrip("\n"))])
            return len(text)

        def flush(self):
            pass

    names = [*PHASES, "UpgradeSourcePinned", "UpgradeStateReady", "UpgradeToolingStaged", "UpgradeIdentitiesDone", "upgrade_project_command",
             "_resolve_launcher_project_config", "load_project_config", "report_installed_release_version", "_resolve_switchyard_project",
             "_load_switchyard_project_config_for_command"]
    saved = {n: getattr(t, n) for n in names}
    config = SimpleNamespace(label="config as loaded", project="syrd424")
    configs = {k: SimpleNamespace(label=k) for k in ("config after U2", "config after U3", "config after U5", "report config after U5")}
    pinned_values = {"desktop_choice": "syrd424 desktop choice", "deploy_ref_chosen": True, "source_repo": _P(f"{P}/pinned-src"),
                     "commit_git_dir": "syrd424 pinned gitdir", "deploy_ref": "v424-pinned", **spec.get("pinned", {})}
    going = {
        "_pin_upgrade_source": lambda args, kw: saved["UpgradeSourcePinned"](**pinned_values),
        "_recover_upgrade_state": lambda args, kw: saved["UpgradeStateReady"](source_repo=_P(f"{P}/state-src"), effective_source_repo=_P(f"{P}/effective-src"),
                                                                           config=configs["config after U2"], cutover="syrd424 cutover"),
        "_refresh_upgrade_artifacts": lambda args, kw: args[0] if answers.get("_refresh_upgrade_artifacts") == "same" else configs["config after U3"],
        "_stage_upgrade_tooling": lambda args, kw: saved["UpgradeToolingStaged"](trusted_release_root=_P(f"{P}/release-root"), publication_detail="syrd424 detail"),
        "_upgrade_identities_and_accounts": lambda args, kw: saved["UpgradeIdentitiesDone"](config=configs["config after U5"],
                                                                                           release_report_config=configs["report config after U5"]),
        "_finish_upgrade": lambda args, kw: 0,
    }
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        on_launcher = {}
        for n in PHASES:
            def phase(*args, _n=n, **kwargs):
                note(_n, *args, **kwargs)
                answer = answers.get(_n)
                if answer == "raise":
                    raise RuntimeError(f"syrd424: {_n} failed")
                if answer == "foreign":
                    return SimpleNamespace(label=f"{_n}'s foreign answer")
                if isinstance(answer, int) and not isinstance(answer, bool):
                    return answer
                return going[_n](args, kwargs)
            on_launcher[n] = phase

        def dispatched(*args, **kwargs):
            note("upgrade_project_command", *args, **kwargs)
            return saved["upgrade_project_command"](*args, **kwargs)
        on_launcher["upgrade_project_command"] = dispatched
        on_launcher["_resolve_launcher_project_config"] = lambda project, *, explicit_config=None: note(
            "_resolve_launcher_project_config", project, explicit_config=explicit_config) or SimpleNamespace(config_path=tmp / "syrd424.json", slug=project)
        on_launcher["load_project_config"] = lambda project, path: note("load_project_config", project, path) or config
        on_launcher["report_installed_release_version"] = lambda *a, **k: note("report_installed_release_version", *a, **k)
        on_launcher["_resolve_switchyard_project"] = lambda project: note("_resolve_switchyard_project", project) or SimpleNamespace(
            config_path=tmp / "syrd424.json", label="entry")
        on_launcher["_load_switchyard_project_config_for_command"] = lambda entry, argv: note(
            "_load_switchyard_project_config_for_command", entry, list(argv)) or config
        rebind = spec.get("rebind")
        if rebind == "UpgradeToolingStaged":
            on_launcher[rebind] = type("SomeOtherContinuation", (), {})
        elif rebind == "_finish_upgrade":
            on_launcher[rebind] = lambda config, **kwargs: note("the rebound finish", config, **kwargs) or 42
        for n, f in on_launcher.items():
            setattr(t, n, f)
        call = spec["call"]
        place = lambda a: str(tmp) + a[1:] if a.startswith("@/") else a
        try:
            with contextlib.redirect_stdout(Stdout()):
                if call == "command":
                    kwargs = {}
                    for k, v in spec["kwargs"].items():
                        if k == "runner":
                            if v == "recorder":
                                kwargs[k] = recorder_runner
                        elif k == "print_func":
                            if v == "recorder":
                                kwargs[k] = show
                        elif k in ("desktop_policy", "source_repo", "tooling_root", "registry_dir"):
                            kwargs[k] = _P(v)
                        else:
                            kwargs[k] = v
                    got = under["upgrade_project_command"](config, config_path=_P(f"{P}/syrd424.json"), **kwargs)
                elif call == "main":
                    got = t.main([place(a) for a in spec["argv"]])
                else:
                    got = t.switchyard_main([place(a) for a in spec["argv"]])
            result = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"result": result, "calls": calls, "printed": printed}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
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


def test_the_module_loads_no_other_switchyard_module_at_import() -> None:
    result = python("import sys, scripts.project_upgrade_command as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing of Switchyard's, the launcher least of all: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.project_upgrade_command", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_upgrade_command")):
        result = python("import builtins, importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_upgrade_command as m; "
                        "p = inspect.signature(m.upgrade_project_command).parameters; "
                        "print(t.upgrade_project_command is m.upgrade_project_command, "
                        "p['runner'].default is subprocess.run and p['print_func'].default is builtins.print, "
                        "sorted({repr(v.default) for k, v in p.items() if v.default is not inspect.Parameter.empty and k not in ('runner', 'print_func')}), "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'ProjectConfig'))")
        check(result.stdout.strip() == "True True [\"''\", 'False', 'None'] True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "project_upgrade_command.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "import subprocess", "from pathlib import Path", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, and the config type under TYPE_CHECKING: {top} {tc}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the command, and nothing else: {names}")


def test_the_launcher_reexports_it_and_both_dispatchers_reach_it_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_upgrade_command"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the command, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"main", "switchyard_main", "_build_parser", "_build_switchyard_upgrade_parser", "restore_interrupted_role_state",
                                        "_role_accounts_ready", "ProjectConfig", "_resolve_launcher_project_config", "load_project_config",
                                        "_resolve_switchyard_project", "_load_switchyard_project_config_for_command", "_pin_upgrade_source",
                                        "_recover_upgrade_state", "_refresh_upgrade_artifacts", "_stage_upgrade_tooling", "_upgrade_identities_and_accounts",
                                        "_finish_upgrade", "UpgradeSourcePinned", "UpgradeStateReady", "UpgradeToolingStaged",
                                        "UpgradeIdentitiesDone"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"main and switchyard_main dispatch upgrade by its launcher global, exactly as often as before: {uses}")
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(loose == [], f"and nothing at module level reads them: {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads them through the launcher: {got}")


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

    def phases(label):
        return [c[0] for c in GOLDEN[label]["calls"] if c[0] in PHASES]

    def kw(label, phase):
        return next(c[2] for c in GOLDEN[label]["calls"] if c[0] == phase)

    check(phases("upgrade: every phase goes on") == list(PHASES) and result("upgrade: every phase goes on") == 0, "the six phases, in order")
    for label, stop in (("U1 refuses", 1), ("U2 refuses", 2), ("U4 refuses", 4), ("U5 refuses", 5)):
        check(phases(f"upgrade: {label}") == list(PHASES[:stop]) and isinstance(result(f"upgrade: {label}"), int),
              f"{label}: returned as it came, nothing later runs")
    check(result("upgrade: U6 answers nonzero") == 16 and result("upgrade: U1 answers a refusal of zero") == 0
          and phases("upgrade: U1 answers a refusal of zero") == ["_pin_upgrade_source"], "U6's answer is the upgrade's; any non-continuation stops it")
    full = "upgrade: every phase goes on"
    check(kw(full, "_recover_upgrade_state")["source_repo"] == "PATH /nonexistent/syrd424/pinned-src"
          and kw(full, "_recover_upgrade_state")["deploy_ref"] == "v424-pinned", "U2 gets what U1 pinned")
    check(GOLDEN[full]["calls"][2][1] == ["NAMESPACE config after U2"] and GOLDEN[full]["calls"][3][1] == ["NAMESPACE config after U3"]
          and GOLDEN[full]["calls"][5][1] == ["NAMESPACE config after U5"], "each phase gets the config the last one settled")
    check(kw(full, "_upgrade_identities_and_accounts")["release_report_config"] == "NAMESPACE config after U2"
          and kw(full, "_finish_upgrade")["release_report_config"] == "NAMESPACE report config after U5",
          "the report config is the one from before U3, until U5 replaces it")
    check(kw(full, "_stage_upgrade_tooling")["trusted_release_root"] is None and kw(full, "_stage_upgrade_tooling")["publication_detail"] == ""
          and kw(full, "_upgrade_identities_and_accounts")["publication_detail"] == "syrd424 detail",
          "U4 starts from nothing and hands its release root and detail on")
    default = "upgrade: only the config path, the default runner and print"
    check(kw(default, "_pin_upgrade_source")["runner"] == "CALLABLE subprocess.run" and kw(default, "_pin_upgrade_source")["print_func"] == "CALLABLE builtins.print",
          "the defaults are subprocess.run and print, handed on, never called")
    for label in ("team-launcher upgrade", "switchyard upgrade"):
        check(result(label) == 0 and "upgrade_project_command" in [c[0] for c in GOLDEN[label]["calls"]], f"{label}: dispatched to the launcher's name")
    check(result("switchyard upgrade, U6 answers nonzero") == 9 and result("team-launcher upgrade, U2 refuses") == 3, "both entry points return the upgrade's answer")


def test_every_launcher_seam_is_reached() -> None:
    # The six phases are stood in on the launcher, recorded; the four continuation types are classes the upgrade tests
    # with isinstance, so they stay the real ones -- one case rebinds a type on the launcher and one a phase, and
    # each changes the answer.
    names = {name for reads in SEAMS.values() for name in reads}
    types = {name for name in names if isinstance(getattr(t, name), type)}
    check(names - types <= REACHED and len(names - types) == 6, f"a recorder on the launcher reached every phase: missing {sorted(names - types - REACHED)}")
    rebound = {spec.get("rebind") for spec in CASES.values()} - {None}
    check(len(types) == 4 and rebound & types and rebound - types and "the rebound finish" in REACHED,
          f"and a type and a phase rebound there take effect: {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_no_other_switchyard_module_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it_and_both_dispatchers_reach_it_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"project_upgrade_command_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
