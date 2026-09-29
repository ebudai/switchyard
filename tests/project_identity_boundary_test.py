#!/usr/bin/env python3
"""SYRD-456: project identity and config primitives, against the launcher they came out of.

The seven -- the three config types (`RoleConfig`, `ProjectConfig`,
`SwitchyardProjectEntry`), the slug rules (`_validate_project_slug`,
`_slug_from_project_name`, `_legacy_dash_slug_from_project_name`) and
`switchyard_registry_dir` -- moved unchanged into `scripts/project_identity.py`;
the launcher re-exports them all. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher. Each class is one
  object, now defined in the new module, so every annotation, construction and
  comparison reaches the same class.
- **The config types are unchanged:** frozen, the same fields in the same
  order with the same types and literal defaults; defining them reads nothing.
- **Seams (rule 24):** what the four functions read when they run -- the slug
  pattern, the registry directory and its variable, and the slug validator --
  is read through the launcher as often as before, so a patch there reaches it.
- **Readers:** the launcher's own definitions name them as before, and every
  production module reads them through the launcher (or names the config types
  only in annotations, under TYPE_CHECKING).
- **The behaviour is the baseline's:** good and bad slugs; both derivations over
  plain, accented, sharp-s, punctuation-only, non-Latin and over-long names; the
  registry directory from the environment or the default; and each type's
  fields, defaults, frozenness, equality and refusals. `GOLDEN` below was
  produced by running the BASELINE launcher's own definitions over the very
  cases embedded here (`gold456.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with a stray registry variable.

No real registry or tenant record is read: the registry variable is set per
case and the default directory is a test-owned one. Spawns, every exec,
signals, account and group lookups and socket connections are refused for each
case.
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
from scripts import project_identity as m  # noqa: E402

CHECKS = 0
MOVED = ('switchyard_registry_dir', 'RoleConfig', 'ProjectConfig', 'SwitchyardProjectEntry', '_slug_from_project_name', '_legacy_dash_slug_from_project_name', '_validate_project_slug')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'switchyard_registry_dir': {'DEFAULT_SWITCHYARD_REGISTRY_DIR': 1, 'SWITCHYARD_REGISTRY_DIR_ENV': 1},
    '_slug_from_project_name': {'_validate_project_slug': 1},
    '_validate_project_slug': {'PROJECT_SLUG_RE': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the seven that names them, and how often.
DISPATCH = {'project_window_title': {'ProjectConfig': 1}, 'visible_roles_for_viewer': {'RoleConfig': 1, 'ProjectConfig': 1}, '_resolve_launcher_project_config': {'SwitchyardProjectEntry': 2, 'switchyard_registry_dir': 1}, 'role_run_as_user': {'ProjectConfig': 1, 'RoleConfig': 1}, 'worktree_ref': {'ProjectConfig': 1}, '_running_project_roles': {'RoleConfig': 2, 'ProjectConfig': 1}, 'switchyard_pane_launcher_for': {'ProjectConfig': 1}, 'chown_owner_file_args': {'ProjectConfig': 1}, 'ensure_owner_file': {'ProjectConfig': 1}, '_is_generated_project_layout_template': {'ProjectConfig': 1}, '_tenant_board_root_from_config': {'ProjectConfig': 1}, '_tenant_board_root_from_config_or_plan': {'ProjectConfig': 1}, '_usable_switchyard_entry_for_project': {'SwitchyardProjectEntry': 1}, '_control_repository_owned_roots': {'ProjectConfig': 1}, '_role_cli_name': {'RoleConfig': 1}, 'role_isolation_gaps': {'ProjectConfig': 1}, 'role_control_accounts': {'ProjectConfig': 1}, 'privileged_upgrade_journal_path': {'ProjectConfig': 1}, '_role_accounts_ready': {'ProjectConfig': 1}, '_tenant_owner_home': {'ProjectConfig': 1}, '_owner_user_systemctl': {'ProjectConfig': 1}, 'capture_installed_units': {'ProjectConfig': 1}, '_plan_data_from_config': {'ProjectConfig': 1}, '_layout_slot_count': {'ProjectConfig': 1}, '_role_by_name': {'RoleConfig': 1, 'ProjectConfig': 1}}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/agent_cli_promotion.py': {'launcher.switchyard_registry_dir': 2}, 'scripts/new_project_phases.py': {'launcher._slug_from_project_name': 1, 'launcher._validate_project_slug': 1}, 'scripts/new_project_precheck.py': {'launcher.switchyard_registry_dir': 1}, 'scripts/new_project_support.py': {'launcher._slug_from_project_name': 1}, 'scripts/pane_rebind.py': {'launcher._validate_project_slug': 1}, 'scripts/presentation_controller.py': {'team_launcher.ProjectConfig': 56, 'team_launcher.RoleConfig': 9}, 'scripts/privileged_runtime_plan.py': {'launcher.switchyard_registry_dir': 1}, 'scripts/project_config_json.py': {'launcher.RoleConfig': 1}, 'scripts/project_config_loader.py': {'launcher.ProjectConfig': 1, 'launcher._validate_project_slug': 2}, 'scripts/project_design_artifact.py': {'launcher._validate_project_slug': 1}, 'scripts/project_design_command.py': {'launcher._validate_project_slug': 1}, 'scripts/project_resolution.py': {'launcher.SwitchyardProjectEntry': 2, 'launcher._legacy_dash_slug_from_project_name': 1, 'launcher._slug_from_project_name': 1, 'launcher._validate_project_slug': 2, 'launcher.switchyard_registry_dir': 1}, 'scripts/project_teardown.py': {'launcher._validate_project_slug': 1, 'launcher.switchyard_registry_dir': 1}, 'scripts/repository_boundary_repair.py': {'launcher._validate_project_slug': 1}, 'scripts/resume_provision_command.py': {'launcher._validate_project_slug': 1, 'launcher.switchyard_registry_dir': 2}, 'scripts/role_runtime.py': {'team_launcher.ProjectConfig': 11, 'team_launcher.RoleConfig': 7}, 'scripts/switchyard_registration.py': {'launcher._validate_project_slug': 1, 'launcher.switchyard_registry_dir': 1}, 'scripts/tenant_config_records.py': {'launcher.switchyard_registry_dir': 1}, 'scripts/workflow_adoption.py': {'launcher._validate_project_slug': 2}}
#: The BASELINE's own behaviour for the cases below (`gold456.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'validate: a good slug': {'result': 'p456', 'type': 'str', 'calls': []},
    'validate: stripped and lower-cased': {'result': 'p456_team', 'type': 'str', 'calls': []},
    'validate: forty characters': {'result': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'type': 'str', 'calls': []},
    'validate: forty-one characters': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$'}, 'calls': []},
    'validate: a dash': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$'}, 'calls': []},
    'validate: a leading underscore': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$'}, 'calls': []},
    'validate: empty': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$'}, 'calls': []},
    'validate: the pattern rebound on the launcher': {'result': 'p-456', 'type': 'str', 'calls': []},
    'slug: a plain name': {'result': 'my_project_456', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['my_project_456']}, {}]]},
    'slug: accents dropped': {'result': 'cafe_deja_vu', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['cafe_deja_vu']}, {}]]},
    'slug: a sharp s': {'result': 'stra_e_456', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['stra_e_456']}, {}]]},
    'slug: punctuation runs': {'result': 'a_b_c', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['a_b_c']}, {}]]},
    'slug: over forty characters': {'result': 'an_extremely_long_project_name_that_runs', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['an_extremely_long_project_name_that_runs']}, {}]]},
    'slug: an underscore at the cut': {'result': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa']}, {}]]},
    'slug: nothing usable': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug cannot be empty'}, 'calls': []},
    'slug: non-Latin only': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug cannot be empty'}, 'calls': []},
    'slug: a leading digit': {'result': '456_project', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['456_project']}, {}]]},
    'slug: the validator rebound on the launcher': {'result': 'validated:my_project', 'type': 'str', 'calls': [['_validate_project_slug', {'list': ['my_project']}, {}]]},
    'legacy: a plain name': {'result': 'my-project-456', 'type': 'str', 'calls': []},
    'legacy: a sharp s kept': {'result': 'straße-456', 'type': 'str', 'calls': []},
    'legacy: punctuation runs': {'result': 'a-b-c', 'type': 'str', 'calls': []},
    'legacy: long, not cut': {'result': 'an-extremely-long-project-name-that-runs-well-past-forty-characters', 'type': 'str', 'calls': []},
    'legacy: nothing usable': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug cannot be empty'}, 'calls': []},
    'registry: the default': {'result': 'PATH TMP/default-registry', 'type': 'PosixPath', 'calls': []},
    'registry: the environment': {'result': 'PATH TMP/registry', 'type': 'PosixPath', 'calls': []},
    'registry: the environment, home-relative and spaced': {'result': 'PATH TMP/home/reg', 'type': 'PosixPath', 'calls': []},
    'registry: the environment blank': {'result': 'PATH TMP/default-registry', 'type': 'PosixPath', 'calls': []},
    'registry: the variable renamed on the launcher': {'result': 'PATH TMP/registry', 'type': 'PosixPath', 'calls': []},
    'type: RoleConfig': {'result': {'dataclass': 'RoleConfig', 'role': 'main', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': '', 'unset_env': {'tuple': []}, 'ephemeral': False, 'presentation_label': ''}, 'fields': [['role', 'str', 'REQUIRED', ''], ['slot', 'int | None', 'REQUIRED', ''], ['detached', 'bool', 'REQUIRED', ''], ['tmux_session', 'str', 'REQUIRED', ''], ['target', 'str', 'REQUIRED', ''], ['workdir', 'str', 'REQUIRED', ''], ['cli', 'list[str]', 'REQUIRED', ''], ['model', 'str', 'REQUIRED', ''], ['model_arg', 'str', 'REQUIRED', ''], ['effort', 'str', 'REQUIRED', ''], ['yolo', 'bool', 'REQUIRED', ''], ['extra_args', 'list[str]', 'REQUIRED', ''], ['resume_mode', 'str', 'REQUIRED', ''], ['resume_flag', 'str', 'REQUIRED', ''], ['resume_subcommand', 'str', 'REQUIRED', ''], ['fresh_session_per_ticket', 'bool', 'REQUIRED', ''], ['live_commands', 'list[str]', 'REQUIRED', ''], ['env', 'dict[str, str]', 'REQUIRED', ''], ['run_as_user', 'str', "''", ''], ['unset_env', 'tuple[str, ...]', '()', ''], ['ephemeral', 'bool', 'False', ''], ['presentation_label', 'str', "''", '']], 'frozen': True, 'equal_to_a_twin': True, 'hashable': 'unhashable fields', 'replaced': {'dataclass': 'RoleConfig', 'role': 'p456-replaced', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': '', 'unset_env': {'tuple': []}, 'ephemeral': False, 'presentation_label': ''}, 'repr_starts': 'RoleConfig', 'calls': []},
    'type: RoleConfig, every default given': {'result': {'dataclass': 'RoleConfig', 'role': 'main', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': 'u', 'unset_env': {'tuple': ['A']}, 'ephemeral': True, 'presentation_label': 'L'}, 'fields': [['role', 'str', 'REQUIRED', ''], ['slot', 'int | None', 'REQUIRED', ''], ['detached', 'bool', 'REQUIRED', ''], ['tmux_session', 'str', 'REQUIRED', ''], ['target', 'str', 'REQUIRED', ''], ['workdir', 'str', 'REQUIRED', ''], ['cli', 'list[str]', 'REQUIRED', ''], ['model', 'str', 'REQUIRED', ''], ['model_arg', 'str', 'REQUIRED', ''], ['effort', 'str', 'REQUIRED', ''], ['yolo', 'bool', 'REQUIRED', ''], ['extra_args', 'list[str]', 'REQUIRED', ''], ['resume_mode', 'str', 'REQUIRED', ''], ['resume_flag', 'str', 'REQUIRED', ''], ['resume_subcommand', 'str', 'REQUIRED', ''], ['fresh_session_per_ticket', 'bool', 'REQUIRED', ''], ['live_commands', 'list[str]', 'REQUIRED', ''], ['env', 'dict[str, str]', 'REQUIRED', ''], ['run_as_user', 'str', "''", ''], ['unset_env', 'tuple[str, ...]', '()', ''], ['ephemeral', 'bool', 'False', ''], ['presentation_label', 'str', "''", '']], 'frozen': True, 'equal_to_a_twin': True, 'hashable': 'unhashable fields', 'replaced': {'dataclass': 'RoleConfig', 'role': 'p456-replaced', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': 'u', 'unset_env': {'tuple': ['A']}, 'ephemeral': True, 'presentation_label': 'L'}, 'repr_starts': 'RoleConfig', 'calls': []},
    'type: RoleConfig, a field missing': {'result': {'raised': 'TypeError', 'message': "RoleConfig.__init__() missing 1 required positional argument: 'env'"}, 'calls': []},
    'type: ProjectConfig': {'result': {'dataclass': 'ProjectConfig', 'project': 'p456', 'project_name': 'P 456', 'ticket_prefix': 'P456', 'layout': 'PATH TMP/lay.json', 'session_dir': 'PATH TMP/sess', 'board_url': 'http://b', 'board_socket': '/run/x.sock', 'upstream_report_url': '', 'upstream_report_token_file': '', 'run_as_user': '', 'pane_launcher': None, 'repository': None, 'control_repository': None, 'worktree_base': None, 'worktree_remote': 'origin', 'worktree_branch': 'main', 'roles': {'list': [{'dataclass': 'RoleConfig', 'role': 'main', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': '', 'unset_env': {'tuple': []}, 'ephemeral': False, 'presentation_label': ''}]}, 'desktop_access': None, 'role_state_isolation': False, 'worker_pool': None}, 'fields': [['project', 'str', 'REQUIRED', ''], ['project_name', 'str', 'REQUIRED', ''], ['ticket_prefix', 'str', 'REQUIRED', ''], ['layout', 'Path', 'REQUIRED', ''], ['session_dir', 'Path', 'REQUIRED', ''], ['board_url', 'str', 'REQUIRED', ''], ['board_socket', 'str', 'REQUIRED', ''], ['upstream_report_url', 'str', 'REQUIRED', ''], ['upstream_report_token_file', 'str', 'REQUIRED', ''], ['run_as_user', 'str', 'REQUIRED', ''], ['pane_launcher', 'Path | None', 'REQUIRED', ''], ['repository', 'Path | None', 'REQUIRED', ''], ['control_repository', 'Path | None', 'REQUIRED', ''], ['worktree_base', 'Path | None', 'REQUIRED', ''], ['worktree_remote', 'str', 'REQUIRED', ''], ['worktree_branch', 'str', 'REQUIRED', ''], ['roles', 'list[RoleConfig]', 'REQUIRED', ''], ['desktop_access', 'dict[str, Any] | None', 'None', ''], ['role_state_isolation', 'bool', 'False', ''], ['worker_pool', "'WorkerPool | None'", 'None', '']], 'frozen': True, 'equal_to_a_twin': True, 'hashable': 'unhashable fields', 'replaced': {'dataclass': 'ProjectConfig', 'project': 'p456-replaced', 'project_name': 'P 456', 'ticket_prefix': 'P456', 'layout': 'PATH TMP/lay.json', 'session_dir': 'PATH TMP/sess', 'board_url': 'http://b', 'board_socket': '/run/x.sock', 'upstream_report_url': '', 'upstream_report_token_file': '', 'run_as_user': '', 'pane_launcher': None, 'repository': None, 'control_repository': None, 'worktree_base': None, 'worktree_remote': 'origin', 'worktree_branch': 'main', 'roles': {'list': [{'dataclass': 'RoleConfig', 'role': 'main', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': '', 'unset_env': {'tuple': []}, 'ephemeral': False, 'presentation_label': ''}]}, 'desktop_access': None, 'role_state_isolation': False, 'worker_pool': None}, 'repr_starts': 'ProjectConfig', 'calls': []},
    'type: ProjectConfig, every default given': {'result': {'dataclass': 'ProjectConfig', 'project': 'p456', 'project_name': 'P 456', 'ticket_prefix': 'P456', 'layout': 'PATH TMP/lay.json', 'session_dir': 'PATH TMP/sess', 'board_url': 'http://b', 'board_socket': '/run/x.sock', 'upstream_report_url': '', 'upstream_report_token_file': '', 'run_as_user': '', 'pane_launcher': None, 'repository': None, 'control_repository': None, 'worktree_base': None, 'worktree_remote': 'origin', 'worktree_branch': 'main', 'roles': {'list': [{'dataclass': 'RoleConfig', 'role': 'main', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': '', 'unset_env': {'tuple': []}, 'ephemeral': False, 'presentation_label': ''}]}, 'desktop_access': {'x': 1}, 'role_state_isolation': True, 'worker_pool': 'POOL'}, 'fields': [['project', 'str', 'REQUIRED', ''], ['project_name', 'str', 'REQUIRED', ''], ['ticket_prefix', 'str', 'REQUIRED', ''], ['layout', 'Path', 'REQUIRED', ''], ['session_dir', 'Path', 'REQUIRED', ''], ['board_url', 'str', 'REQUIRED', ''], ['board_socket', 'str', 'REQUIRED', ''], ['upstream_report_url', 'str', 'REQUIRED', ''], ['upstream_report_token_file', 'str', 'REQUIRED', ''], ['run_as_user', 'str', 'REQUIRED', ''], ['pane_launcher', 'Path | None', 'REQUIRED', ''], ['repository', 'Path | None', 'REQUIRED', ''], ['control_repository', 'Path | None', 'REQUIRED', ''], ['worktree_base', 'Path | None', 'REQUIRED', ''], ['worktree_remote', 'str', 'REQUIRED', ''], ['worktree_branch', 'str', 'REQUIRED', ''], ['roles', 'list[RoleConfig]', 'REQUIRED', ''], ['desktop_access', 'dict[str, Any] | None', 'None', ''], ['role_state_isolation', 'bool', 'False', ''], ['worker_pool', "'WorkerPool | None'", 'None', '']], 'frozen': True, 'equal_to_a_twin': True, 'hashable': 'unhashable fields', 'replaced': {'dataclass': 'ProjectConfig', 'project': 'p456-replaced', 'project_name': 'P 456', 'ticket_prefix': 'P456', 'layout': 'PATH TMP/lay.json', 'session_dir': 'PATH TMP/sess', 'board_url': 'http://b', 'board_socket': '/run/x.sock', 'upstream_report_url': '', 'upstream_report_token_file': '', 'run_as_user': '', 'pane_launcher': None, 'repository': None, 'control_repository': None, 'worktree_base': None, 'worktree_remote': 'origin', 'worktree_branch': 'main', 'roles': {'list': [{'dataclass': 'RoleConfig', 'role': 'main', 'slot': 0, 'detached': False, 'tmux_session': 'p456-main', 'target': 'p456-main:0.0', 'workdir': '/nonexistent/syrd456/w', 'cli': {'list': ['codex']}, 'model': '', 'model_arg': '--model', 'effort': '', 'yolo': False, 'extra_args': {'list': []}, 'resume_mode': 'flag', 'resume_flag': '--resume', 'resume_subcommand': 'resume', 'fresh_session_per_ticket': False, 'live_commands': {'list': []}, 'env': {}, 'run_as_user': '', 'unset_env': {'tuple': []}, 'ephemeral': False, 'presentation_label': ''}]}, 'desktop_access': {'x': 1}, 'role_state_isolation': True, 'worker_pool': 'POOL'}, 'repr_starts': 'ProjectConfig', 'calls': []},
    'type: SwitchyardProjectEntry': {'result': {'dataclass': 'SwitchyardProjectEntry', 'slug': 'p456', 'name': 'P 456', 'config_path': 'PATH TMP/p456.json'}, 'fields': [['slug', 'str', 'REQUIRED', ''], ['name', 'str', 'REQUIRED', ''], ['config_path', 'Path', 'REQUIRED', '']], 'frozen': True, 'equal_to_a_twin': True, 'hashable': True, 'replaced': {'dataclass': 'SwitchyardProjectEntry', 'slug': 'p456-replaced', 'name': 'P 456', 'config_path': 'PATH TMP/p456.json'}, 'repr_starts': 'SwitchyardProjectEntry', 'calls': []},
    'type: SwitchyardProjectEntry, an unknown field': {'result': {'raised': 'TypeError', 'message': "SwitchyardProjectEntry.__init__() got an unexpected keyword argument 'extra'"}, 'calls': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold456.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the four functions on a synthetic name, slug or registry setting, or builds one of the three
# config types from the holder, and records the answer, or the exact refusal, and, in order, every call it made on the
# launcher. The registry directory's environment variable is set per case, and the default registry directory is
# rebound on the launcher into a test-owned tree, which is also `$HOME`; no real registry or tenant record is read. The
# slug pattern and the slug validator are the launcher's own, recorded there and passed through.
LONG = "An Extremely Long Project Name That Runs Well Past Forty Characters"
ROLE = {"role": "main", "slot": 0, "detached": False, "tmux_session": "p456-main", "target": "p456-main:0.0", "workdir": "/nonexistent/syrd456/w",
        "cli": ["codex"], "model": "", "model_arg": "--model", "effort": "", "yolo": False, "extra_args": [], "resume_mode": "flag",
        "resume_flag": "--resume", "resume_subcommand": "resume", "fresh_session_per_ticket": False, "live_commands": [], "env": {}}
PROJECT = {"project": "p456", "project_name": "P 456", "ticket_prefix": "P456", "layout": "@/lay.json", "session_dir": "@/sess", "board_url": "http://b",
           "board_socket": "/run/x.sock", "upstream_report_url": "", "upstream_report_token_file": "", "run_as_user": "", "pane_launcher": None,
           "repository": None, "control_repository": None, "worktree_base": None, "worktree_remote": "origin", "worktree_branch": "main", "roles": "@ROLES"}
CASES = {
    "validate: a good slug": {"call": "_validate_project_slug", "args": ["p456"]},
    "validate: stripped and lower-cased": {"call": "_validate_project_slug", "args": ["  P456_Team  "]},
    "validate: forty characters": {"call": "_validate_project_slug", "args": ["a" * 40]},
    "validate: forty-one characters": {"call": "_validate_project_slug", "args": ["a" * 41]},
    "validate: a dash": {"call": "_validate_project_slug", "args": ["p-456"]},
    "validate: a leading underscore": {"call": "_validate_project_slug", "args": ["_p456"]},
    "validate: empty": {"call": "_validate_project_slug", "args": ["   "]},
    "validate: the pattern rebound on the launcher": {"call": "_validate_project_slug", "args": ["p-456"], "pattern": "^[a-z0-9-]+$"},
    "slug: a plain name": {"call": "_slug_from_project_name", "args": ["My Project 456"]},
    "slug: accents dropped": {"call": "_slug_from_project_name", "args": ["Café Déjà Vu"]},
    "slug: a sharp s": {"call": "_slug_from_project_name", "args": ["Straße 456"]},
    "slug: punctuation runs": {"call": "_slug_from_project_name", "args": ["  A -- B!! C  "]},
    "slug: over forty characters": {"call": "_slug_from_project_name", "args": [LONG]},
    "slug: an underscore at the cut": {"call": "_slug_from_project_name", "args": ["a" * 39 + " b"]},
    "slug: nothing usable": {"call": "_slug_from_project_name", "args": ["!!!"]},
    "slug: non-Latin only": {"call": "_slug_from_project_name", "args": ["Ωμέγα"]},
    "slug: a leading digit": {"call": "_slug_from_project_name", "args": ["456 Project"]},
    "slug: the validator rebound on the launcher": {"call": "_slug_from_project_name", "args": ["My Project"], "validator": True},
    "legacy: a plain name": {"call": "_legacy_dash_slug_from_project_name", "args": ["My Project 456"]},
    "legacy: a sharp s kept": {"call": "_legacy_dash_slug_from_project_name", "args": ["Straße 456"]},
    "legacy: punctuation runs": {"call": "_legacy_dash_slug_from_project_name", "args": ["  A -- B!! C  "]},
    "legacy: long, not cut": {"call": "_legacy_dash_slug_from_project_name", "args": [LONG]},
    "legacy: nothing usable": {"call": "_legacy_dash_slug_from_project_name", "args": ["!!!"]},
    "registry: the default": {"call": "switchyard_registry_dir", "args": []},
    "registry: the environment": {"call": "switchyard_registry_dir", "args": [], "env": "@/registry"},
    "registry: the environment, home-relative and spaced": {"call": "switchyard_registry_dir", "args": [], "env": "  ~/reg  "},
    "registry: the environment blank": {"call": "switchyard_registry_dir", "args": [], "env": "   "},
    "registry: the variable renamed on the launcher": {"call": "switchyard_registry_dir", "args": [], "env": "@/registry", "env_name": "SYRD456_REGISTRY"},
    "type: RoleConfig": {"type": "RoleConfig", "build": ROLE},
    "type: RoleConfig, every default given": {"type": "RoleConfig", "build": {**ROLE, "run_as_user": "u", "unset_env": ("A",), "ephemeral": True, "presentation_label": "L"}},
    "type: RoleConfig, a field missing": {"type": "RoleConfig", "build": {k: v for k, v in ROLE.items() if k != "env"}},
    "type: ProjectConfig": {"type": "ProjectConfig", "build": PROJECT},
    "type: ProjectConfig, every default given": {"type": "ProjectConfig", "build": {**PROJECT, "desktop_access": {"x": 1}, "role_state_isolation": True, "worker_pool": "POOL"}},
    "type: SwitchyardProjectEntry": {"type": "SwitchyardProjectEntry", "build": {"slug": "p456", "name": "P 456", "config_path": "@/p456.json"}},
    "type: SwitchyardProjectEntry, an unknown field": {"type": "SwitchyardProjectEntry", "build": {"slug": "p456", "name": "P", "config_path": "@/c", "extra": 1}},
}
FUNCTIONS = ("switchyard_registry_dir", "RoleConfig", "ProjectConfig", "SwitchyardProjectEntry", "_slug_from_project_name", "_legacy_dash_slug_from_project_name",
             "_validate_project_slug")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions; the registry setting and the default directory into a test-owned tree."""
    import dataclasses, os, re, shutil, tempfile
    from pathlib import Path as _P
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd456-")).resolve()

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"dataclass": type(value).__qualname__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if isinstance(value, (list, tuple)):
            return {"tuple" if isinstance(value, tuple) else "list": [norm(v) for v in value]}
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(str(tmp), "TMP")
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return repr(value).replace(str(tmp), "TMP")

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def place(value):
        if value == "@ROLES":
            return [t.RoleConfig(**ROLE)]
        if isinstance(value, str) and value.startswith("@/"):
            return _P(value.replace("@/", str(tmp) + "/"))
        return value

    saved = {n: getattr(t, n) for n in ("_validate_project_slug", "PROJECT_SLUG_RE", "DEFAULT_SWITCHYARD_REGISTRY_DIR", "SWITCHYARD_REGISTRY_DIR_ENV")}
    names = ("SWITCHYARD_PROJECT_REGISTRY_DIR", "SYRD456_REGISTRY", "HOME")
    saved_env = {k: os.environ.get(k) for k in names}
    try:
        for k in names:
            os.environ.pop(k, None)
        os.environ["HOME"] = str(tmp / "home")
        t.DEFAULT_SWITCHYARD_REGISTRY_DIR = tmp / "default-registry"
        if "env_name" in spec:
            t.SWITCHYARD_REGISTRY_DIR_ENV = spec["env_name"]
        if "env" in spec:
            os.environ[spec.get("env_name", "SWITCHYARD_PROJECT_REGISTRY_DIR")] = spec["env"].replace("@/", str(tmp) + "/")
        if "pattern" in spec:
            t.PROJECT_SLUG_RE = re.compile(spec["pattern"])
        if spec.get("validator"):
            t._validate_project_slug = lambda value: note("_validate_project_slug", value) or f"validated:{value}"
        else:
            t._validate_project_slug = lambda value: note("_validate_project_slug", value) or saved["_validate_project_slug"](value)
        try:
            if "type" in spec:
                cls = getattr(holder, spec["type"])
                built = cls(**{k: place(v) for k, v in spec["build"].items()})
                try:
                    setattr(built, dataclasses.fields(cls)[0].name, "changed")
                    frozen = False
                except dataclasses.FrozenInstanceError:
                    frozen = True
                fields = [[f.name, str(f.type), repr(f.default) if f.default is not dataclasses.MISSING else "REQUIRED",
                           "factory" if f.default_factory is not dataclasses.MISSING else ""] for f in dataclasses.fields(cls)]
                again = cls(**{k: place(v) for k, v in spec["build"].items()})
                first = dataclasses.fields(cls)[0].name
                changed = dataclasses.replace(built, **{first: "p456-replaced" if isinstance(getattr(built, first), str) else getattr(built, first)})
                return {"result": norm(built), "fields": fields, "frozen": frozen, "equal_to_a_twin": built == again, "hashable": isinstance(hash(built), int) if
                        all(not isinstance(getattr(built, f.name), (list, dict)) for f in dataclasses.fields(cls)) else "unhashable fields",
                        "replaced": norm(changed), "repr_starts": repr(built).split("(")[0], "calls": calls}
            # The call itself is the definition's own, on either holder; what it calls on the launcher is what is recorded.
            fn = saved[spec["call"]] if holder is t and spec["call"] in saved else getattr(holder, spec["call"])
            got = fn(*[place(a) for a in spec["args"]])
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": calls}
        return {"result": norm(got), "type": type(got).__name__, "calls": calls}
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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


CLASSES = ("RoleConfig", "ProjectConfig", "SwitchyardProjectEntry")


def annotation_ids(tree: ast.AST) -> set[int]:
    """Every node inside an annotation, or inside a TYPE_CHECKING block: names there are not read when the code runs."""
    out: set[int] = set()
    for x in ast.walk(tree):
        parts = []
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = [x.returns, *(a.annotation for a in x.args.posonlyargs + x.args.args + x.args.kwonlyargs + [v for v in (x.args.vararg, x.args.kwarg) if v])]
        elif isinstance(x, ast.AnnAssign):
            parts = [x.annotation]
        elif isinstance(x, ast.If) and "TYPE_CHECKING" in ast.unparse(x.test):
            parts = [x]
        for part in parts:
            if part is not None:
                out |= {id(y) for y in ast.walk(part)}
    return out


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.project_identity as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.project_identity", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_identity")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_identity as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"[getattr(m, n).__module__ for n in {CLASSES!r}], "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'PROJECT_SLUG_RE', 'DEFAULT_SWITCHYARD_REGISTRY_DIR', "
                        "'SWITCHYARD_REGISTRY_DIR_ENV', 'WorkerPool')))")
        check(result.stdout.strip() == "True ['scripts.project_identity', 'scripts.project_identity', 'scripts.project_identity'] True",
              f"{' then '.join(order)}: one object each, the three classes defined here; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import dataclasses as _dataclasses
    import pathlib
    import typing
    import unicodedata as _unicodedata
    check(m.os is os and m.unicodedata is _unicodedata and m.dataclass is _dataclasses.dataclass and m.Path is pathlib.Path and m.Any is typing.Any
          and m.TYPE_CHECKING is False and t.os is m.os and t.Path is m.Path and t.dataclass is m.dataclass,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "project_identity.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, its sibling included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if isinstance(node, ast.ClassDef):
            check(imports == [] and expected == {} and [ast.unparse(d) for d in node.decorator_list] == ["dataclass(frozen=True)"],
                  f"{name}: a frozen dataclass that reads nothing of the launcher, at definition time or later: {imports}")
            continue
        check((imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[0]) == imports[0]) if expected else imports == [],
              f"{name}: the launcher imported first thing when it reads one, and nothing else imported: {imports}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    # The class defaults are literals: defining the classes reads nothing.
    defaults = {n.name: [ast.unparse(s.value) for s in n.body if isinstance(s, ast.AnnAssign) and s.value is not None]
                for n in tree.body if isinstance(n, ast.ClassDef)}
    check(defaults == {"RoleConfig": ["''", "()", "False", "''"], "ProjectConfig": ["None", "False", "None"], "SwitchyardProjectEntry": []},
          f"every field default a literal, as before: {defaults}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import unicodedata", "from dataclasses import dataclass", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.worker_pool_command import WorkerPool"],
          f"the standard library, and the worker pool type for annotations only: {top} {tc}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the seven, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_identity"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the seven, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"_resolve_launcher_project_config", *DISPATCH} <= defined | exported,
          "the launcher defines none of them, and keeps _resolve_launcher_project_config, their users and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"the launcher's own definitions name them exactly as often as before, by the re-exported names: {len(uses)} definitions")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.Attribute) and x.attr in MOVED and isinstance(x.value, ast.Name) and x.value.id in ("launcher", "team_launcher"):
                got[ast.unparse(x)] = got.get(ast.unparse(x), 0) + 1
        # The config types are also named bare in annotations, imported under TYPE_CHECKING; that is not a read.
        skip = annotation_ids(source)
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED and id(x) not in skip})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads them through the launcher, as often as before: {got} {bare}")


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

    def refusal(label):
        r = result(label)
        return r["message"] if isinstance(r, dict) and r.get("raised") == "SystemExit" else None

    BAD = "switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$"
    EMPTY = "switchyard: project slug cannot be empty"
    check(result("validate: a good slug") == "p456" and result("validate: stripped and lower-cased") == "p456_team"
          and result("validate: forty characters") == "a" * 40
          and refusal("validate: forty-one characters") == refusal("validate: a dash") == refusal("validate: a leading underscore") == refusal("validate: empty") == BAD
          and result("validate: the pattern rebound on the launcher") == "p-456",
          "a slug is stripped and lower-cased, then must match the launcher's pattern, read when it runs")
    check(result("slug: a plain name") == "my_project_456" and result("slug: accents dropped") == "cafe_deja_vu" and result("slug: a sharp s") == "stra_e_456"
          and result("slug: punctuation runs") == "a_b_c" and result("slug: over forty characters") == "an_extremely_long_project_name_that_runs"
          and result("slug: an underscore at the cut") == "a" * 39 and result("slug: a leading digit") == "456_project"
          and refusal("slug: nothing usable") == refusal("slug: non-Latin only") == EMPTY
          and GOLDEN["slug: nothing usable"]["calls"] == []
          and result("slug: the validator rebound on the launcher") == "validated:my_project"
          and GOLDEN["slug: a plain name"]["calls"] == [["_validate_project_slug", {"list": ["my_project_456"]}, {}]],
          "a name's slug: accents dropped, other characters to single underscores, cut to forty without a trailing underscore, "
          "refused when empty, then validated through the launcher's name")
    check(result("legacy: a plain name") == "my-project-456" and result("legacy: a sharp s kept") == "straße-456" and result("legacy: punctuation runs") == "a-b-c"
          and result("legacy: long, not cut") == "an-extremely-long-project-name-that-runs-well-past-forty-characters"
          and refusal("legacy: nothing usable") == EMPTY and all(GOLDEN[k]["calls"] == [] for k in GOLDEN if k.startswith("legacy:")),
          "the legacy dashed slug keeps any letter, joins with single dashes, is never cut and never validated")
    check(result("registry: the default") == "PATH TMP/default-registry" and result("registry: the environment") == "PATH TMP/registry"
          and result("registry: the environment, home-relative and spaced") == "PATH TMP/home/reg"
          and result("registry: the environment blank") == "PATH TMP/default-registry"
          and result("registry: the variable renamed on the launcher") == "PATH TMP/registry",
          "the registry directory: the environment variable, stripped and ~-expanded, else the default, both read on the launcher when it runs")
    role = GOLDEN["type: RoleConfig"]
    check(role["frozen"] is True and role["equal_to_a_twin"] is True and role["repr_starts"] == "RoleConfig"
          and [f[0] for f in role["fields"]][-4:] == ["run_as_user", "unset_env", "ephemeral", "presentation_label"]
          and [f[2] for f in role["fields"]][-4:] == ["''", "()", "False", "''"] and sum(f[2] == "REQUIRED" for f in role["fields"]) == 18
          and result("type: RoleConfig, a field missing")["raised"] == "TypeError",
          "RoleConfig: frozen, equal by value, 18 required fields then four defaulted ones, in order")
    project = GOLDEN["type: ProjectConfig"]
    check(project["frozen"] is True and [f[0] for f in project["fields"]][-3:] == ["desktop_access", "role_state_isolation", "worker_pool"]
          and [f[2] for f in project["fields"]][-3:] == ["None", "False", "None"] and project["fields"][-1][1] == "'WorkerPool | None'"
          and sum(f[2] == "REQUIRED" for f in project["fields"]) == 17,
          "ProjectConfig: frozen, 17 required fields then three defaulted ones, the worker pool annotated as a string")
    entry = GOLDEN["type: SwitchyardProjectEntry"]
    check(entry["frozen"] is True and entry["hashable"] is True and [f[0] for f in entry["fields"]] == ["slug", "name", "config_path"]
          and result("type: SwitchyardProjectEntry, an unknown field")["raised"] == "TypeError",
          "SwitchyardProjectEntry: frozen, hashable, three required fields")


def test_every_launcher_seam_is_reached() -> None:
    # The one function the four call on the launcher, the slug validator, is a recorder there; the pattern and the
    # registry settings are rebound there by their own cases, and change the answer.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name)) or name == "PROJECT_SLUG_RE"}
    check(constants == {"PROJECT_SLUG_RE", "DEFAULT_SWITCHYARD_REGISTRY_DIR", "SWITCHYARD_REGISTRY_DIR_ENV"} and names - constants <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"project_identity_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
