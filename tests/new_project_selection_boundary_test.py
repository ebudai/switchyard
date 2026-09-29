#!/usr/bin/env python3
"""SYRD-462: the new-project role and runtime selection, against the launcher it came out of.

The eighteen -- five constants (`SUPPORTED_NEW_PROJECT_CLIS`,
`NEW_PROJECT_ROLE_CLI_DEFAULTS`, `SWITCHYARD_PROMPT_MAX_ATTEMPTS`,
`NEW_PROJECT_RESERVED_ROLE_NAMES`, `NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES`),
the three validators, the role/runtime defaults, the line prompts, the runtime
picker and `RoleSelection` -- moved unchanged into
`scripts/new_project_selection.py`; the launcher re-exports them all and keeps
the two path helpers. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher, and imports the
  prompt schema's types only for annotations. `RoleSelection` is one class,
  now defined in the new module.
- **Seams (rule 24):** what they read of the launcher -- each other, the role
  pattern, the runtime catalog, the prompt schema and the terminal picker -- is
  read through it as often as before, so a patch there reaches them.
- **Readers:** no launcher definition names them, and every production module
  reads them through the launcher, as often as before.
- **The behaviour is the baseline's:** every prompt text and its order, the
  yes/no answers and attempt limit, every validation message, the default
  pairs and owner, the runtime choices and field, and `RoleSelection`'s fields,
  defaults, frozenness and equality. `GOLDEN` below was produced by running the
  BASELINE launcher's own definitions over the very cases embedded here
  (`gold462.py`), not typed; it is byte-identical under `env -i`, in a normal
  role pane, with another HOME, USER, COLUMNS and LINES, under umask 077, under
  several hash seeds and with another TERM, width and locale.

Nothing reads a terminal: every prompt goes to an injected input function and
every line to an injected print function (or captured stdout). Spawns, every
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import new_project_selection as m  # noqa: E402

CHECKS = 0
MOVED = ('SUPPORTED_NEW_PROJECT_CLIS', 'NEW_PROJECT_ROLE_CLI_DEFAULTS', 'SWITCHYARD_PROMPT_MAX_ATTEMPTS', 'NEW_PROJECT_RESERVED_ROLE_NAMES', 'NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES', '_validate_new_project_cli', '_validate_new_project_implementer_role', '_validate_new_project_audit_role', '_dedupe_role_names', '_default_role_cli_pairs', '_default_new_project_owner', '_read_prompt', '_prompt_text', '_prompt_bool', '_runtime_choices', '_runtime_field', '_prompt_cli', 'RoleSelection')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_validate_new_project_cli': {'SUPPORTED_NEW_PROJECT_CLIS': 2},
    '_validate_new_project_implementer_role': {'NEW_PROJECT_RESERVED_ROLE_NAMES': 1, 'ROLE_RE': 1},
    '_validate_new_project_audit_role': {'NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES': 1, 'ROLE_RE': 1},
    '_default_role_cli_pairs': {'NEW_PROJECT_ROLE_CLI_DEFAULTS': 3},
    '_prompt_text': {'_read_prompt': 1},
    '_prompt_bool': {'SWITCHYARD_PROMPT_MAX_ATTEMPTS': 1, '_read_prompt': 1},
    '_runtime_choices': {'Choice': 1, 'SUPPORTED_NEW_PROJECT_CLIS': 1, 'runtime_catalog': 1},
    '_runtime_field': {'Field': 1, 'KIND_SINGLE': 1, '_runtime_choices': 1, 'with_existing_value': 1},
    '_prompt_cli': {'_runtime_field': 1, '_validate_new_project_cli': 1, 'terminal_select': 2},
}
#: Measured on the baseline launcher: every launcher definition outside the eighteen that names them, and how often.
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/agent_cli_promotion.py': {'launcher._read_prompt': 1}, 'scripts/agy_credential.py': {'launcher._prompt_bool': 1}, 'scripts/desktop_policy.py': {'launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS': 1, 'launcher._prompt_bool': 1, 'launcher._read_prompt': 1}, 'scripts/new_project_artifacts.py': {'launcher._default_role_cli_pairs': 2}, 'scripts/new_project_command.py': {'launcher._default_new_project_owner': 1, 'launcher._default_role_cli_pairs': 1}, 'scripts/new_project_phases.py': {'launcher.NEW_PROJECT_RESERVED_ROLE_NAMES': 1, 'launcher._prompt_text': 4}, 'scripts/new_project_support.py': {'launcher.NEW_PROJECT_RESERVED_ROLE_NAMES': 1, 'launcher._default_role_cli_pairs': 1, 'launcher._read_prompt': 1, 'launcher._validate_new_project_cli': 1, 'launcher._validate_new_project_implementer_role': 1}, 'scripts/owner_preparation.py': {'launcher._prompt_bool': 1}, 'scripts/project_design_artifact.py': {'launcher.NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES': 1, 'launcher._default_role_cli_pairs': 1, 'launcher._validate_new_project_cli': 1}, 'scripts/project_design_command.py': {'launcher._dedupe_role_names': 1, 'launcher._default_new_project_owner': 1, 'launcher._default_role_cli_pairs': 1, 'launcher._prompt_bool': 5, 'launcher._prompt_text': 10, 'launcher._validate_new_project_audit_role': 1}, 'scripts/project_role_add.py': {'launcher._runtime_field': 1, 'launcher._validate_new_project_audit_role': 1, 'launcher._validate_new_project_cli': 1, 'launcher._validate_new_project_implementer_role': 1}, 'scripts/project_role_plan_support.py': {'launcher.NEW_PROJECT_RESERVED_ROLE_NAMES': 1, 'launcher._dedupe_role_names': 1}, 'scripts/project_role_runtime.py': {'launcher._runtime_field': 1}, 'scripts/project_teardown.py': {'launcher._default_new_project_owner': 3, 'launcher._read_prompt': 1}, 'scripts/role_plan_prompt.py': {'launcher.NEW_PROJECT_ROLE_CLI_DEFAULTS': 1, 'launcher.RoleSelection': 2, 'launcher._prompt_bool': 2, 'launcher._runtime_field': 1, 'launcher._validate_new_project_implementer_role': 1}}
#: The BASELINE's own behaviour for the cases below (`gold462.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'cli: accepted, stripped and lower-cased': {'result': {'type': 'str', 'value': 'codex'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'cli: refused': {'result': {'raised': 'SystemExit', 'message': 'CLI must be one of claude, codex, agy, hermes'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'cli: refused with a context': {'result': {'raised': 'SystemExit', 'message': 'director CLI must be one of claude, codex, agy, hermes'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'cli: the supported set rebound on the launcher': {'result': {'type': 'str', 'value': 'p462'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'implementer: accepted': {'result': {'type': 'str', 'value': 'main_1'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'implementer: reserved': {'result': {'raised': 'SystemExit', 'message': "role 'director' is reserved"}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'implementer: audit is reserved': {'result': {'raised': 'SystemExit', 'message': "role 'audit' is reserved"}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'implementer: a bad name': {'result': {'raised': 'SystemExit', 'message': 'implementer must match ^[a-z][a-z0-9_-]{0,63}$'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'implementer: the pattern rebound on the launcher': {'result': {'type': 'str', 'value': '9bad'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'implementer: the reserved set rebound on the launcher': {'result': {'raised': 'SystemExit', 'message': "role 'worker' is reserved"}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'audit role: audit accepted': {'result': {'type': 'str', 'value': 'audit'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'audit role: reserved': {'result': {'raised': 'SystemExit', 'message': "audit role 'user' is reserved"}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'audit role: a bad name': {'result': {'raised': 'SystemExit', 'message': 'audit role must match ^[a-z][a-z0-9_-]{0,63}$'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'audit role: the reserved set rebound on the launcher': {'result': {'raised': 'SystemExit', 'message': "audit role 'audit' is reserved"}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'dedupe: first occurrence kept, in order': {'result': {'type': 'tuple', 'value': ['b', 'a', 'c']}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'dedupe: nothing': {'result': {'type': 'tuple', 'value': []}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'pairs: designer, director, audit, implementers': {'result': {'type': 'tuple', 'value': [['designer', 'claude'], ['director', 'claude'], ['audit', 'claude'], ['main', 'codex'], ['worker', 'codex']]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'pairs: no designer': {'result': {'type': 'tuple', 'value': [['director', 'claude'], ['audit', 'claude'], ['main', 'codex']]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'pairs: no audit': {'result': {'type': 'tuple', 'value': [['director', 'claude'], ['main', 'codex']]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'pairs: named audit roles': {'result': {'type': 'tuple', 'value': [['designer', 'claude'], ['director', 'claude'], ['audit', 'claude'], ['review', 'claude'], ['main', 'codex']]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'pairs: an empty audit list wins over include_audit': {'result': {'type': 'tuple', 'value': [['director', 'claude'], ['main', 'codex']]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'pairs: the defaults rebound on the launcher': {'result': {'type': 'tuple', 'value': [['designer', 'd462'], ['director', 'r462'], ['audit', 'a462'], ['main', 'codex']]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'owner: the default': {'result': {'type': 'str', 'value': 'p462-agent'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'read: an answer': {'result': {'type': 'str', 'value': 'p462'}, 'prompts': ['Project name: '], 'printed': [], 'stdout': [], 'calls': []},
    'read: no input': {'result': {'raised': 'SystemExit', 'message': 'switchyard: no input available'}, 'prompts': ['Project name: '], 'printed': [], 'stdout': [], 'calls': []},
    'text: the default shown and taken on a blank': {'result': {'type': 'str', 'value': 'p462'}, 'prompts': ['Project name [p462]: '], 'printed': [], 'stdout': [], 'calls': [['_read_prompt', ['Project name [p462]: '], {}]]},
    'text: an answer stripped': {'result': {'type': 'str', 'value': 'other'}, 'prompts': ['Project name [p462]: '], 'printed': [], 'stdout': [], 'calls': [['_read_prompt', ['Project name [p462]: '], {}]]},
    'text: no default': {'result': {'type': 'str', 'value': ''}, 'prompts': ['Owner: '], 'printed': [], 'stdout': [], 'calls': [['_read_prompt', ['Owner: '], {}]]},
    'bool: blank takes the default': {'result': {'type': 'bool', 'value': True}, 'prompts': ['Designer [Y/n]: '], 'printed': [], 'stdout': [], 'calls': [['_read_prompt', ['Designer [Y/n]: '], {}]]},
    'bool: yes': {'result': {'type': 'bool', 'value': True}, 'prompts': ['Designer [y/N]: '], 'printed': [], 'stdout': [], 'calls': [['_read_prompt', ['Designer [y/N]: '], {}]]},
    'bool: zero is no': {'result': {'type': 'bool', 'value': False}, 'prompts': ['Designer [Y/n]: '], 'printed': [], 'stdout': [], 'calls': [['_read_prompt', ['Designer [Y/n]: '], {}]]},
    'bool: an invalid answer, then no': {'result': {'type': 'bool', 'value': False}, 'prompts': ['Designer [Y/n]: ', 'Designer [Y/n]: '], 'printed': [], 'stdout': ['answer yes or no'], 'calls': [['_read_prompt', ['Designer [Y/n]: '], {}], ['_read_prompt', ['Designer [Y/n]: '], {}]]},
    'bool: too many invalid answers': {'result': {'raised': 'SystemExit', 'message': 'switchyard: too many invalid answers for Designer'}, 'prompts': ['Designer [y/N]: ', 'Designer [y/N]: ', 'Designer [y/N]: ', 'Designer [y/N]: ', 'Designer [y/N]: '], 'printed': [], 'stdout': ['answer yes or no', 'answer yes or no', 'answer yes or no', 'answer yes or no', 'answer yes or no'], 'calls': [['_read_prompt', ['Designer [y/N]: '], {}], ['_read_prompt', ['Designer [y/N]: '], {}], ['_read_prompt', ['Designer [y/N]: '], {}], ['_read_prompt', ['Designer [y/N]: '], {}], ['_read_prompt', ['Designer [y/N]: '], {}]]},
    'bool: the attempt limit rebound on the launcher': {'result': {'raised': 'SystemExit', 'message': 'switchyard: too many invalid answers for Designer'}, 'prompts': ['Designer [y/N]: ', 'Designer [y/N]: '], 'printed': [], 'stdout': ['answer yes or no', 'answer yes or no'], 'calls': [['_read_prompt', ['Designer [y/N]: '], {}], ['_read_prompt', ['Designer [y/N]: '], {}]]},
    'runtimes: from the catalog, in supported order': {'result': {'type': 'tuple', 'value': [{'dataclass': 'Choice', 'value': 'claude', 'label': 'Claude Code', 'description': 'takes an --effort flag', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'codex', 'label': 'Codex', 'description': 'takes model_reasoning_effort as config', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'agy', 'label': 'Antigravity', 'description': 'can list its own models; takes no effort level', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'hermes', 'label': 'Hermes', 'description': 'chooses its model in its own picker', 'unlisted': False}]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'runtimes: an unsupported catalog entry, an uncatalogued name': {'result': {'type': 'tuple', 'value': [{'dataclass': 'Choice', 'value': 'codex', 'label': 'Codex', 'description': 'takes model_reasoning_effort as config', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'p462', 'label': '', 'description': '', 'unlisted': False}]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'runtimes: the catalog rebound on the launcher': {'result': {'type': 'tuple', 'value': [{'dataclass': 'Choice', 'value': 'claude', 'label': '', 'description': '', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'codex', 'label': 'P462 Codex', 'description': 'rebound', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'agy', 'label': '', 'description': '', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'hermes', 'label': '', 'description': '', 'unlisted': False}]}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'field: a default': {'result': {'type': 'Field', 'value': {'dataclass': 'Field', 'name': 'runtime', 'kind': 'single', 'title': 'director runtime', 'choices': [{'dataclass': 'Choice', 'value': 'claude', 'label': 'Claude Code', 'description': 'takes an --effort flag', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'codex', 'label': 'Codex', 'description': 'takes model_reasoning_effort as config', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'agy', 'label': 'Antigravity', 'description': 'can list its own models; takes no effort level', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'hermes', 'label': 'Hermes', 'description': 'chooses its model in its own picker', 'unlisted': False}], 'default': 'claude', 'description': '', 'allow_custom': False, 'custom_title': 'Something else', 'required': True, 'validate': None, 'depends_on': [], 'secret': False}}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': [['_runtime_choices', [], {}]]},
    'field: a configured runtime wins': {'result': {'type': 'Field', 'value': {'dataclass': 'Field', 'name': 'runtime', 'kind': 'single', 'title': 'main runtime', 'choices': [{'dataclass': 'Choice', 'value': 'agy', 'label': 'Antigravity', 'description': 'can list its own models; takes no effort level', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'claude', 'label': 'Claude Code', 'description': 'takes an --effort flag', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'codex', 'label': 'Codex', 'description': 'takes model_reasoning_effort as config', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'hermes', 'label': 'Hermes', 'description': 'chooses its model in its own picker', 'unlisted': False}], 'default': 'agy', 'description': '', 'allow_custom': False, 'custom_title': 'Something else', 'required': True, 'validate': None, 'depends_on': [], 'secret': False}}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': [['_runtime_choices', [], {}]]},
    'field: a configured runtime no longer offered': {'result': {'type': 'Field', 'value': {'dataclass': 'Field', 'name': 'runtime', 'kind': 'single', 'title': 'main runtime', 'choices': [{'dataclass': 'Choice', 'value': 'p462cli', 'label': 'p462cli (already configured)', 'description': '', 'unlisted': True}, {'dataclass': 'Choice', 'value': 'claude', 'label': 'Claude Code', 'description': 'takes an --effort flag', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'codex', 'label': 'Codex', 'description': 'takes model_reasoning_effort as config', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'agy', 'label': 'Antigravity', 'description': 'can list its own models; takes no effort level', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'hermes', 'label': 'Hermes', 'description': 'chooses its model in its own picker', 'unlisted': False}], 'default': 'p462cli', 'description': '', 'allow_custom': False, 'custom_title': 'Something else', 'required': True, 'validate': None, 'depends_on': [], 'secret': False}}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': [['_runtime_choices', [], {}]]},
    'cli prompt: Enter takes the default': {'result': {'type': 'str', 'value': 'claude'}, 'prompts': ['director runtime [Claude Code]: '], 'printed': ['director runtime:', '  1) Claude Code -- takes an --effort flag  [default]', '  2) Codex -- takes model_reasoning_effort as config', '  3) Antigravity -- can list its own models; takes no effort level', '  4) Hermes -- chooses its model in its own picker', "  (number, Enter for the default, 'cancel' to stop)"], 'stdout': [], 'calls': [['_validate_new_project_cli', ['Claude'], {'context': 'default CLI for director'}], ['_runtime_field', ['director'], {'default': 'claude'}], ['_runtime_choices', [], {}]]},
    'cli prompt: a number': {'result': {'type': 'str', 'value': 'agy'}, 'prompts': ['main runtime [Codex]: '], 'printed': ['main runtime:', '  1) Claude Code -- takes an --effort flag', '  2) Codex -- takes model_reasoning_effort as config  [default]', '  3) Antigravity -- can list its own models; takes no effort level', '  4) Hermes -- chooses its model in its own picker', "  (number, Enter for the default, 'cancel' to stop)"], 'stdout': [], 'calls': [['_validate_new_project_cli', ['codex'], {'context': 'default CLI for main'}], ['_runtime_field', ['main'], {'default': 'codex'}], ['_runtime_choices', [], {}]]},
    'cli prompt: an unsupported default': {'result': {'raised': 'SystemExit', 'message': 'default CLI for main must be one of claude, codex, agy, hermes'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': [['_validate_new_project_cli', ['gpt'], {'context': 'default CLI for main'}]]},
    'cli prompt: cancelled': {'result': {'raised': 'SystemExit', 'message': 'switchyard: too many invalid answers for main CLI'}, 'prompts': ['main runtime [Codex]: '], 'printed': ['main runtime:', '  1) Claude Code -- takes an --effort flag', '  2) Codex -- takes model_reasoning_effort as config  [default]', '  3) Antigravity -- can list its own models; takes no effort level', '  4) Hermes -- chooses its model in its own picker', "  (number, Enter for the default, 'cancel' to stop)"], 'stdout': [], 'calls': [['_validate_new_project_cli', ['codex'], {'context': 'default CLI for main'}], ['_runtime_field', ['main'], {'default': 'codex'}], ['_runtime_choices', [], {}]]},
    'cli prompt: the picker rebound on the launcher': {'result': {'type': 'str', 'value': 'p462-picked'}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': [['_validate_new_project_cli', ['codex'], {'context': 'default CLI for main'}], ['_runtime_field', ['main'], {'default': 'codex'}], ['_runtime_choices', [], {}], ['terminal_select.select_one', [{'dataclass': 'Field', 'name': 'runtime', 'kind': 'single', 'title': 'main runtime', 'choices': [{'dataclass': 'Choice', 'value': 'claude', 'label': 'Claude Code', 'description': 'takes an --effort flag', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'codex', 'label': 'Codex', 'description': 'takes model_reasoning_effort as config', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'agy', 'label': 'Antigravity', 'description': 'can list its own models; takes no effort level', 'unlisted': False}, {'dataclass': 'Choice', 'value': 'hermes', 'label': 'Hermes', 'description': 'chooses its model in its own picker', 'unlisted': False}], 'default': 'codex', 'description': '', 'allow_custom': False, 'custom_title': 'Something else', 'required': True, 'validate': None, 'depends_on': [], 'secret': False}], {}]]},
    'selection: the fields and defaults': {'result': {'type': 'RoleSelection', 'value': {'dataclass': 'RoleSelection', 'role': 'main', 'cli': 'codex', 'model': '', 'effort': ''}}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'selection: every field': {'result': {'type': 'RoleSelection', 'value': {'dataclass': 'RoleSelection', 'role': 'director', 'cli': 'claude', 'model': 'opus', 'effort': 'high'}}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'selection: a field missing': {'result': {'raised': 'TypeError', 'message': "RoleSelection.__init__() missing 1 required positional argument: 'cli'"}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
    'selection: frozen and equal by value': {'result': {'type': 'RoleSelection', 'value': {'dataclass': 'RoleSelection', 'role': 'main', 'cli': 'codex', 'model': '', 'effort': ''}, 'frozen': True, 'equal to a twin': True, 'hashable': True}, 'prompts': [], 'printed': [], 'stdout': [], 'calls': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold462.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the eighteen and records the answer or the exact exception, every prompt shown to the injected
# input function and every line given to the injected print function (and to stdout), and, in order, every call of the
# launcher's siblings (recorded there and passed through). The real terminal picker and prompt schema run with the
# injected functions, so nothing reads a terminal. A case may rebind a launcher name -- the role pattern, a constant,
# the runtime catalog, the picker -- to show it is read when the function runs.
CASES = {
    "cli: accepted, stripped and lower-cased": {"call": "_validate_new_project_cli", "args": [" Codex "]},
    "cli: refused": {"call": "_validate_new_project_cli", "args": ["gpt"]},
    "cli: refused with a context": {"call": "_validate_new_project_cli", "args": ["gpt"], "kwargs": {"context": "director CLI"}},
    "cli: the supported set rebound on the launcher": {"call": "_validate_new_project_cli", "args": ["p462"], "rebind": {"SUPPORTED_NEW_PROJECT_CLIS": ("p462",)}},
    "implementer: accepted": {"call": "_validate_new_project_implementer_role", "args": [" Main_1 "]},
    "implementer: reserved": {"call": "_validate_new_project_implementer_role", "args": ["Director"]},
    "implementer: audit is reserved": {"call": "_validate_new_project_implementer_role", "args": ["audit"]},
    "implementer: a bad name": {"call": "_validate_new_project_implementer_role", "args": ["9bad"], "kwargs": {"context": "implementer"}},
    "implementer: the pattern rebound on the launcher": {"call": "_validate_new_project_implementer_role", "args": ["9bad"], "rebind": {"ROLE_RE": ".*"}},
    "implementer: the reserved set rebound on the launcher": {"call": "_validate_new_project_implementer_role", "args": ["worker"],
                                                              "rebind": {"NEW_PROJECT_RESERVED_ROLE_NAMES": frozenset({"worker"})}},
    "audit role: audit accepted": {"call": "_validate_new_project_audit_role", "args": ["Audit"]},
    "audit role: reserved": {"call": "_validate_new_project_audit_role", "args": ["user"]},
    "audit role: a bad name": {"call": "_validate_new_project_audit_role", "args": ["-x"]},
    "audit role: the reserved set rebound on the launcher": {"call": "_validate_new_project_audit_role", "args": ["audit"],
                                                             "rebind": {"NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES": frozenset({"audit"})}},
    "dedupe: first occurrence kept, in order": {"call": "_dedupe_role_names", "args": [["b", "a", "b", "c", "a"]]},
    "dedupe: nothing": {"call": "_dedupe_role_names", "args": [[]]},
    "pairs: designer, director, audit, implementers": {"call": "_default_role_cli_pairs", "args": [["main", "worker"]], "kwargs": {"include_designer": True}},
    "pairs: no designer": {"call": "_default_role_cli_pairs", "args": [["main"]], "kwargs": {"include_designer": False}},
    "pairs: no audit": {"call": "_default_role_cli_pairs", "args": [["main"]], "kwargs": {"include_designer": False, "include_audit": False}},
    "pairs: named audit roles": {"call": "_default_role_cli_pairs", "args": [["main"]], "kwargs": {"include_designer": True, "audit_roles": ["audit", "review"]}},
    "pairs: an empty audit list wins over include_audit": {"call": "_default_role_cli_pairs", "args": [["main"]],
                                                           "kwargs": {"include_designer": False, "include_audit": True, "audit_roles": []}},
    "pairs: the defaults rebound on the launcher": {"call": "_default_role_cli_pairs", "args": [["main"]], "kwargs": {"include_designer": True},
                                                    "rebind": {"NEW_PROJECT_ROLE_CLI_DEFAULTS": {"designer": "d462", "director": "r462", "audit": "a462"}}},
    "owner: the default": {"call": "_default_new_project_owner", "args": ["p462"]},
    "read: an answer": {"call": "_read_prompt", "args": ["Project name: "], "inputs": ["p462"]},
    "read: no input": {"call": "_read_prompt", "args": ["Project name: "], "inputs": ["EOF"]},
    "text: the default shown and taken on a blank": {"call": "_prompt_text", "args": ["Project name"], "kwargs": {"default": "p462"}, "inputs": ["  "]},
    "text: an answer stripped": {"call": "_prompt_text", "args": ["Project name"], "kwargs": {"default": "p462"}, "inputs": ["  other  "]},
    "text: no default": {"call": "_prompt_text", "args": ["Owner"], "inputs": [""]},
    "bool: blank takes the default": {"call": "_prompt_bool", "args": ["Designer"], "kwargs": {"default": True}, "inputs": [""]},
    "bool: yes": {"call": "_prompt_bool", "args": ["Designer"], "kwargs": {"default": False}, "inputs": [" Yes "]},
    "bool: zero is no": {"call": "_prompt_bool", "args": ["Designer"], "kwargs": {"default": True}, "inputs": ["0"]},
    "bool: an invalid answer, then no": {"call": "_prompt_bool", "args": ["Designer"], "kwargs": {"default": True}, "inputs": ["maybe", "n"]},
    "bool: too many invalid answers": {"call": "_prompt_bool", "args": ["Designer"], "kwargs": {"default": False}, "inputs": ["x", "x", "x", "x", "x", "x"]},
    "bool: the attempt limit rebound on the launcher": {"call": "_prompt_bool", "args": ["Designer"], "kwargs": {"default": False}, "inputs": ["x", "x", "x"],
                                                        "rebind": {"SWITCHYARD_PROMPT_MAX_ATTEMPTS": 2}},
    "runtimes: from the catalog, in supported order": {"call": "_runtime_choices", "args": []},
    "runtimes: an unsupported catalog entry, an uncatalogued name": {"call": "_runtime_choices", "args": [], "rebind": {"SUPPORTED_NEW_PROJECT_CLIS": ("codex", "p462")}},
    "runtimes: the catalog rebound on the launcher": {"call": "_runtime_choices", "args": [], "rebind": {"runtime_catalog": True}},
    "field: a default": {"call": "_runtime_field", "args": ["director"], "kwargs": {"default": "claude"}},
    "field: a configured runtime wins": {"call": "_runtime_field", "args": ["main"], "kwargs": {"default": "codex", "configured": "agy"}},
    "field: a configured runtime no longer offered": {"call": "_runtime_field", "args": ["main"], "kwargs": {"default": "codex", "configured": "p462cli"}},
    "cli prompt: Enter takes the default": {"call": "_prompt_cli", "args": ["director"], "kwargs": {"default": "Claude"}, "inputs": [""]},
    "cli prompt: a number": {"call": "_prompt_cli", "args": ["main"], "kwargs": {"default": "codex"}, "inputs": ["3"]},
    "cli prompt: an unsupported default": {"call": "_prompt_cli", "args": ["main"], "kwargs": {"default": "gpt"}, "inputs": [""]},
    "cli prompt: cancelled": {"call": "_prompt_cli", "args": ["main"], "kwargs": {"default": "codex"}, "inputs": ["cancel"]},
    "cli prompt: the picker rebound on the launcher": {"call": "_prompt_cli", "args": ["main"], "kwargs": {"default": "codex"}, "rebind": {"terminal_select": True}},
    "selection: the fields and defaults": {"call": "RoleSelection", "args": ["main", "codex"]},
    "selection: every field": {"call": "RoleSelection", "args": ["director", "claude"], "kwargs": {"model": "opus", "effort": "high"}},
    "selection: a field missing": {"call": "RoleSelection", "args": ["main"]},
    "selection: frozen and equal by value": {"call": "RoleSelection", "args": ["main", "codex"], "frozen": True},
}
FUNCTIONS = ("_validate_new_project_cli", "_validate_new_project_implementer_role", "_validate_new_project_audit_role", "_dedupe_role_names",
             "_default_role_cli_pairs", "_default_new_project_owner", "_read_prompt", "_prompt_text", "_prompt_bool", "_runtime_choices",
             "_runtime_field", "_prompt_cli", "RoleSelection")
PASSED = ("_read_prompt", "_runtime_choices", "_runtime_field", "_validate_new_project_cli")
CONSTANTS = ("SUPPORTED_NEW_PROJECT_CLIS", "NEW_PROJECT_ROLE_CLI_DEFAULTS", "SWITCHYARD_PROMPT_MAX_ATTEMPTS", "NEW_PROJECT_RESERVED_ROLE_NAMES",
             "NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition, with injected input and print; nothing reads a terminal."""
    import contextlib, dataclasses, io, re
    from types import SimpleNamespace
    calls: list = []
    prompts: list = []
    printed: list = []

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"dataclass": type(value).__name__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if isinstance(value, (frozenset, set)):
            return sorted(norm(v) for v in value)
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if callable(value):
            return "CALLABLE"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm({k: v for k, v in kwargs.items() if k not in ("input_func", "print_func")})])

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *CONSTANTS, "ROLE_RE", "runtime_catalog", "terminal_select")}
    try:
        answers = list(spec.get("inputs", []))

        def input_func(prompt):
            prompts.append(prompt)
            if not answers:
                raise AssertionError(f"an unexpected prompt: {prompt!r}")
            answer = answers.pop(0)
            if answer == "EOF":
                raise EOFError
            return answer

        def print_func(line):
            printed.append(str(line))
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name, *a, **k) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "ROLE_RE":
                t.ROLE_RE = re.compile(value)
            elif name == "runtime_catalog":
                t.runtime_catalog = SimpleNamespace(RUNTIMES=(t.Choice("codex", "P462 Codex", "rebound"),))
            elif name == "terminal_select":
                def select_one(field, *a, **k):
                    note("terminal_select.select_one", field, *a, **k)
                    return "p462-picked"
                t.terminal_select = SimpleNamespace(select_one=select_one, Cancelled=saved["terminal_select"].Cancelled)
            else:
                setattr(t, name, value)
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        kwargs = dict(spec.get("kwargs", {}))
        if spec["call"] in ("_read_prompt", "_prompt_text", "_prompt_bool", "_prompt_cli"):
            kwargs["input_func"] = input_func
        if spec["call"] == "_prompt_cli":
            kwargs["print_func"] = print_func
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                got = fn(*spec["args"], **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        else:
            result = {"type": type(got).__name__, "value": norm(got)}
            if spec.get("frozen"):
                try:
                    got.role = "changed"
                except dataclasses.FrozenInstanceError:
                    result["frozen"] = True
                else:
                    result["frozen"] = False
                result["equal to a twin"] = got == fn(*spec["args"], **kwargs)
                try:
                    result["hashable"] = hash(got) == hash(fn(*spec["args"], **kwargs))
                except TypeError:
                    result["hashable"] = False
        return {"result": result, "prompts": prompts, "printed": printed, "stdout": out.getvalue().splitlines(), "calls": calls}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
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


#: What the Director kept on the launcher, beside the eighteen: the two path helpers, and what the eighteen read there.
KEPT = ("_new_project_session_dir", "_new_project_worktree_base", "ROLE_RE", "runtime_catalog", "terminal_select", "Choice", "Field", "KIND_SINGLE",
        "with_existing_value")
#: The five constants, as the baseline wrote them.
CONSTANT_TEXT = {
    "SUPPORTED_NEW_PROJECT_CLIS": "('claude', 'codex', 'agy', 'hermes')",
    "NEW_PROJECT_ROLE_CLI_DEFAULTS": "{'designer': 'claude', 'director': 'claude', 'audit': 'claude'}",
    "SWITCHYARD_PROMPT_MAX_ATTEMPTS": "5",
    "NEW_PROJECT_RESERVED_ROLE_NAMES": "frozenset({'designer', 'director', 'audit', 'user', 'unassigned'})",
    "NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES": "frozenset({'designer', 'director', 'user', 'unassigned'})",
}


def top_name(n: ast.AST) -> str | None:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
    return None


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
    result = python("import sys, scripts.new_project_selection as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.new_project_selection", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.new_project_selection")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.new_project_selection as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), m.RoleSelection.__qualname__ == 'RoleSelection', "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.new_project_selection'] True True",
              f"{' then '.join(order)}: one object each, defined here; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import dataclasses
    import typing
    check(m.dataclass is dataclasses.dataclass and m.Callable is typing.Callable and m.Sequence is typing.Sequence and m.TYPE_CHECKING is False
          and t.dataclass is m.dataclass and t.Callable is m.Callable and t.Sequence is m.Sequence,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "new_project_selection.py").read_text(encoding="utf-8"))
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
        check((imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0]) if expected else imports == [],
              f"{name}: the launcher imported first thing (after its docstring) when it reads one, and nothing else imported: {imports}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "from dataclasses import dataclass", "from typing import TYPE_CHECKING, Callable, Sequence"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.ticket_board.prompt_schema import Choice, Field"],
          f"the standard library, and the prompt schema's types for annotations only: {top} {tc}")
    consts = {top_name(n): ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)}
    check(consts == CONSTANT_TEXT, f"the five constants are the baseline's literals, evaluated at definition time without reading anything: {consts}")
    cls = next(n for n in tree.body if top_name(n) == "RoleSelection")
    fields = [(ast.unparse(x.target), ast.unparse(x.annotation), ast.unparse(x.value) if x.value is not None else None) for x in cls.body if isinstance(x, ast.AnnAssign)]
    check([ast.unparse(d) for d in cls.decorator_list] == ["dataclass(frozen=True)"]
          and fields == [("role", "str", None), ("cli", "str", None), ("model", "str", "''"), ("effort", "str", "''")],
          f"RoleSelection: frozen, role and cli required, model and effort defaulting to empty, in that order: {fields}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {"_validate_new_project_cli": ["'CLI'"], "_validate_new_project_implementer_role": ["'role'"],
                       "_validate_new_project_audit_role": ["'audit role'"], "_dedupe_role_names": [], "_default_role_cli_pairs": ["True", "None"],
                       "_default_new_project_owner": [], "_read_prompt": ["input"], "_prompt_text": ["''", "input"], "_prompt_bool": ["input"],
                       "_runtime_choices": [], "_runtime_field": ["''"], "_prompt_cli": ["input", "print"]},
          f"the defaults are the baseline's: literals, and the builtin input and print: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the eighteen, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.new_project_selection"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the eighteen, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | set(KEPT) | set(DISPATCH) <= defined | exported,
          "the launcher defines none of them, and keeps the two path helpers and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"the launcher's own definitions name them exactly as often as before -- none: {uses}")
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


#: The owner prefix the baseline builds: an expected argv prefix, compared and never run.
OWNER_PREFIX = ["sudo", "-u"]


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def value(label):
        return result(label)["value"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    SUPPORTED = "claude, codex, agy, hermes"
    PATTERN = "^[a-z][a-z0-9_-]{0,63}$"
    check(value("cli: accepted, stripped and lower-cased") == "codex" and result("cli: refused")["message"] == f"CLI must be one of {SUPPORTED}"
          and result("cli: refused with a context")["message"] == f"director CLI must be one of {SUPPORTED}"
          and value("cli: the supported set rebound on the launcher") == "p462",
          "a runtime: stripped and lower-cased, one of the launcher's supported set, read when it runs; the refusal names the set in order")
    check(value("implementer: accepted") == "main_1" and result("implementer: reserved")["message"] == "role 'director' is reserved"
          and result("implementer: audit is reserved")["message"] == "role 'audit' is reserved"
          and result("implementer: a bad name")["message"] == f"implementer must match {PATTERN}"
          and value("implementer: the pattern rebound on the launcher") == "9bad"
          and result("implementer: the reserved set rebound on the launcher")["message"] == "role 'worker' is reserved"
          and value("audit role: audit accepted") == "audit" and result("audit role: reserved")["message"] == "audit role 'user' is reserved"
          and result("audit role: a bad name")["message"] == f"audit role must match {PATTERN}"
          and result("audit role: the reserved set rebound on the launcher")["message"] == "audit role 'audit' is reserved",
          "role names: stripped, lower-cased, the launcher's pattern, then its reserved set -- audit reserved for implementers only")
    check(value("dedupe: first occurrence kept, in order") == ["b", "a", "c"] and value("dedupe: nothing") == []
          and all(result(k)["type"] == "tuple" for k in GOLDEN if k.startswith(("dedupe:", "pairs:"))),
          "duplicates dropped, first occurrence kept, as a tuple")
    check(value("pairs: designer, director, audit, implementers") == [["designer", "claude"], ["director", "claude"], ["audit", "claude"], ["main", "codex"], ["worker", "codex"]]
          and value("pairs: no designer") == [["director", "claude"], ["audit", "claude"], ["main", "codex"]]
          and value("pairs: no audit") == value("pairs: an empty audit list wins over include_audit") == [["director", "claude"], ["main", "codex"]]
          and value("pairs: named audit roles")[2:4] == [["audit", "claude"], ["review", "claude"]]
          and value("pairs: the defaults rebound on the launcher")[:3] == [["designer", "d462"], ["director", "r462"], ["audit", "a462"]],
          "default pairs: designer (if asked), director, the audit roles, then each implementer on codex; runtimes from the launcher's defaults")
    check(value("owner: the default") == "p462-agent" and value("read: an answer") == "p462"
          and result("read: no input") == {"raised": "SystemExit", "message": "switchyard: no input available"},
          "the default owner is <project>-agent; a prompt with no input left exits")
    check(GOLDEN["text: the default shown and taken on a blank"]["prompts"] == ["Project name [p462]: "] and value("text: the default shown and taken on a blank") == "p462"
          and value("text: an answer stripped") == "other" and GOLDEN["text: no default"]["prompts"] == ["Owner: "] and value("text: no default") == "",
          "a text prompt shows its default in brackets, strips the answer, and takes the default on a blank")
    check(GOLDEN["bool: blank takes the default"]["prompts"] == ["Designer [Y/n]: "] and value("bool: blank takes the default") is True
          and GOLDEN["bool: yes"]["prompts"] == ["Designer [y/N]: "] and value("bool: yes") is True and value("bool: zero is no") is False
          and value("bool: an invalid answer, then no") is False and GOLDEN["bool: an invalid answer, then no"]["stdout"] == ["answer yes or no"]
          and len(GOLDEN["bool: too many invalid answers"]["prompts"]) == 5
          and result("bool: too many invalid answers")["message"] == "switchyard: too many invalid answers for Designer"
          and len(GOLDEN["bool: the attempt limit rebound on the launcher"]["prompts"]) == 2
          and all(set(calls(k)) == {"_read_prompt"} for k in GOLDEN if k.startswith(("bool:", "text:"))),
          "a yes/no prompt: Y/n or y/N, blank is the default, 'answer yes or no' on stdout, the launcher's attempt limit, each line through its reader")
    runtimes = [c["value"] for c in value("runtimes: from the catalog, in supported order")]
    check(runtimes == ["claude", "codex", "agy", "hermes"]
          and [c["value"] for c in value("runtimes: an unsupported catalog entry, an uncatalogued name")] == ["codex", "p462"]
          and value("runtimes: an unsupported catalog entry, an uncatalogued name")[1]["label"] == ""
          and value("runtimes: the catalog rebound on the launcher")[1]["label"] == "P462 Codex",
          "the runtime choices: the launcher's supported set in order, described from its catalog, a bare choice when uncatalogued")
    field = value("field: a default")
    check(field["name"] == "runtime" and field["kind"] == "single" and field["title"] == "director runtime" and field["default"] == "claude"
          and value("field: a configured runtime wins")["default"] == "agy"
          and value("field: a configured runtime no longer offered")["choices"][0]["value"] == "p462cli"
          and value("field: a configured runtime no longer offered")["choices"][0]["unlisted"] is True,
          "the runtime field: single choice, '<role> runtime', the configured value wins and is kept even when no longer offered")
    check(value("cli prompt: Enter takes the default") == "claude" and value("cli prompt: a number") == "agy"
          and GOLDEN["cli prompt: a number"]["prompts"] == ["main runtime [Codex]: "] and len(GOLDEN["cli prompt: a number"]["printed"]) == 6
          and result("cli prompt: an unsupported default")["message"] == f"default CLI for main must be one of {SUPPORTED}"
          and GOLDEN["cli prompt: an unsupported default"]["prompts"] == []
          and result("cli prompt: cancelled")["message"] == "switchyard: too many invalid answers for main CLI"
          and value("cli prompt: the picker rebound on the launcher") == "p462-picked"
          and calls("cli prompt: a number") == ["_validate_new_project_cli", "_runtime_field", "_runtime_choices"],
          "the runtime prompt: the default validated first, then the picker over the runtime field, both the launcher's; a cancel exits")
    check(value("selection: the fields and defaults") == {"dataclass": "RoleSelection", "role": "main", "cli": "codex", "model": "", "effort": ""}
          and value("selection: every field")["effort"] == "high" and result("selection: a field missing")["raised"] == "TypeError"
          and result("selection: frozen and equal by value")["frozen"] is True and result("selection: frozen and equal by value")["equal to a twin"] is True
          and result("selection: frozen and equal by value")["hashable"] is True,
          "RoleSelection: role and cli required, model and effort empty by default, frozen, equal and hashable by value")


def test_every_launcher_seam_is_reached() -> None:
    # The siblings the eighteen call on the launcher are recorders there; the pattern, constants, catalog, schema and picker are
    # rebound by their own cases or read as they are.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = {"_read_prompt", "_runtime_choices", "_runtime_field", "_validate_new_project_cli"}
    check(functions <= names and functions <= REACHED and "terminal_select.select_one" in REACHED,
          f"a recorder on the launcher reached every sibling: missing {sorted(functions - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"new_project_selection_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
