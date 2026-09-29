#!/usr/bin/env python3
"""SYRD-465: the provisioning workflow seed parsing and tenant projection, against project_provision it came out of.

The twenty-two -- the seed classes `WorkflowStageSeed` and
`WorkflowTransitionSeed`; the reading of schema.sql's `workflow_stages` and
`workflow_transitions` INSERT rows (`schema_workflow_stages`,
`schema_workflow_transitions` and their SQL literal parsers, with
`SCHEMA_SQL_PATH`); and the tenant projection (`project_workflow_stages`,
`project_workflow_transitions`, `project_workflow_state_names`, their owner,
allowed-role and rank helpers, and the policy `TENANT_WORKFLOW_EXCLUDED_STAGES`
/ `TENANT_WORKFLOW_EXCLUDED_ACTIONS`) -- moved unchanged into
`scripts/ticket_board/provision_workflow_projection.py`; `project_provision`
re-exports them all, in both branches of its import block, and keeps the SQL
rendered from them, the workflow record, `_dedupe` and
`DEFAULT_IMPLEMENTER_ROLES`. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first, and
  `frontend_script_core` (which imports the seed class at load) reaches the
  same one. The module alone loads only its package. `SCHEMA_SQL_PATH` is the
  same schema.sql beside `project_provision`.
- **The known effect of moving the classes:** the seed classes' `__module__` is
  now this module's (`SEED_CLASS_MODULE`). Each is still one class object, in
  the package and in the direct script -- its fallback included.
- **Seams (rule 24):** what they read of `project_provision` -- each other, the
  constants, the seed classes, `_dedupe` and `DEFAULT_IMPLEMENTER_ROLES` -- is
  read through it when they run, with the direct-script fallback, so a patch
  there reaches them.
- **Readers:** the SQL renderers name them as before, and every production
  module imports them from `project_provision`, as often as before.
- **The behaviour is the baseline's:** the SQL literal parsers and their
  refusals over synthetic text, the real schema.sql's eleven stages and
  fifty-nine transitions, and the projection for default, shaped, lean,
  implementers-without-main and pgu plans built by `build_plan` for a synthetic
  owner. `GOLDEN` below was produced by running the BASELINE module's own
  definitions over the very cases embedded here (`gold465.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME, USER
  and COLUMNS, under umask 077, under several hash seeds and with another TMPDIR
  and locale.
- **The direct script and the package render the same packets,** byte for
  byte, for the default plan and for two whose workflow the plan shapes.

No real home, tenant or account is read or written: plans name a synthetic
owner, schema text is synthetic or the repository's own schema.sql, and the
packets are rendered in a test-owned directory. Spawns, every exec, signals,
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

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_workflow_projection as m  # noqa: E402

CHECKS = 0
MOVED = ('SCHEMA_SQL_PATH', 'TENANT_WORKFLOW_EXCLUDED_STAGES', 'TENANT_WORKFLOW_EXCLUDED_ACTIONS', 'WorkflowStageSeed', 'WorkflowTransitionSeed', '_schema_sql_text', '_insert_values_block', '_split_sql_tuple_rows', '_split_sql_fields', '_parse_sql_string', '_parse_sql_nullable_string', '_parse_sql_bool', '_parse_sql_text_array', 'schema_workflow_stages', 'schema_workflow_transitions', '_project_implementation_owner_roles', '_project_workflow_owner_roles', '_project_workflow_allowed_roles', '_rank_project_stages', 'project_workflow_stages', 'project_workflow_transitions', 'project_workflow_state_names')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    '_schema_sql_text': {'SCHEMA_SQL_PATH': 1},
    '_parse_sql_nullable_string': {'_parse_sql_string': 1},
    '_parse_sql_text_array': {'_parse_sql_string': 1, '_split_sql_fields': 1},
    'schema_workflow_stages': {'WorkflowStageSeed': 1, '_insert_values_block': 1, '_parse_sql_bool': 1, '_parse_sql_nullable_string': 3, '_parse_sql_string': 2, '_parse_sql_text_array': 1, '_schema_sql_text': 1, '_split_sql_fields': 1, '_split_sql_tuple_rows': 1},
    'schema_workflow_transitions': {'WorkflowTransitionSeed': 1, '_insert_values_block': 1, '_parse_sql_bool': 2, '_parse_sql_string': 3, '_parse_sql_text_array': 1, '_schema_sql_text': 1, '_split_sql_fields': 1, '_split_sql_tuple_rows': 1},
    '_project_workflow_owner_roles': {'_project_implementation_owner_roles': 1},
    '_project_workflow_allowed_roles': {'DEFAULT_IMPLEMENTER_ROLES': 1, '_dedupe': 1},
    '_rank_project_stages': {'WorkflowStageSeed': 2},
    'project_workflow_stages': {'TENANT_WORKFLOW_EXCLUDED_STAGES': 1, 'WorkflowStageSeed': 2, '_project_workflow_owner_roles': 1, '_rank_project_stages': 1, 'schema_workflow_stages': 1},
    'project_workflow_transitions': {'TENANT_WORKFLOW_EXCLUDED_ACTIONS': 1, 'WorkflowTransitionSeed': 3, '_dedupe': 1, '_project_workflow_allowed_roles': 1, 'project_workflow_stages': 1, 'schema_workflow_transitions': 2},
    'project_workflow_state_names': {'project_workflow_stages': 1},
}
#: Measured on the baseline: every project_provision definition outside the twenty-two that names them, and how often.
DISPATCH = {'render_project_role_constraint_sql': {'project_workflow_state_names': 1}, 'render_workflow_sql': {'project_workflow_stages': 1, 'project_workflow_transitions': 1}, '_workflow_stage_rows_sql': {'WorkflowStageSeed': 1}, '_workflow_transition_rows_sql': {'WorkflowTransitionSeed': 1}, 'render_add_role_sql': {'project_workflow_stages': 1, 'project_workflow_transitions': 1}, 'render_vcs_close_role_sql': {'project_workflow_stages': 1, 'project_workflow_transitions': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/ticket_board/frontend_script_core.py': {'import WorkflowStageSeed': 1, 'import schema_workflow_stages': 1}, 'scripts/workflow_adoption.py': {'import project_workflow_stages': 1, 'import project_workflow_transitions': 1}}
#: The BASELINE's own behaviour for the cases below (`gold465.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'text: given': {'result': {'type': 'str', 'value': '-- given'}, 'calls': {}},
    'text: the schema beside the module': {'result': {'type': 'str', 'value': 'TEXT 505202 chars sha256:04153a12f30be3b7'}, 'calls': {}},
    'text: the path rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 1385 chars sha256:61ca0f4b0848b714'}, 'calls': {}},
    'values: stages': {'result': {'type': 'str', 'value': 'TEXT 443 chars sha256:def5066d7b21a15d'}, 'calls': {}},
    'values: transitions': {'result': {'type': 'str', 'value': 'TEXT 525 chars sha256:39d2b16c113784d9'}, 'calls': {}},
    'values: a missing table': {'result': {'raised': 'ValueError', 'message': 'could not find ticket_board.workflow_nothing seed INSERT in schema.sql'}, 'calls': {}},
    'values: a table name that is not a pattern': {'result': {'raised': 'ValueError', 'message': 'could not find ticket_board.workflow.stages seed INSERT in schema.sql'}, 'calls': {}},
    'values: no ON CONFLICT': {'result': {'raised': 'ValueError', 'message': 'could not find ticket_board.workflow_stages seed INSERT in schema.sql'}, 'calls': {}},
    'rows: two rows': {'result': {'type': 'tuple', 'value': ["'a', 1", "'b' , 2"]}, 'calls': {}},
    'rows: parentheses and doubled quotes inside strings': {'result': {'type': 'tuple', 'value': ["'a (b)', 'it''s)'", "'c'"]}, 'calls': {}},
    'rows: nested parentheses kept': {'result': {'type': 'tuple', 'value': ['f(x), 2']}, 'calls': {}},
    'rows: none': {'result': {'type': 'tuple', 'value': []}, 'calls': {}},
    'rows: unterminated': {'result': {'raised': 'ValueError', 'message': 'unterminated SQL values block'}, 'calls': {}},
    'rows: an unterminated quote': {'result': {'raised': 'ValueError', 'message': 'unterminated SQL values block'}, 'calls': {}},
    'rows: a stray close': {'result': {'raised': 'ValueError', 'message': 'unterminated SQL values block'}, 'calls': {}},
    'fields: plain': {'result': {'type': 'tuple', 'value': ["'a'", '1', 'NULL']}, 'calls': {}},
    'fields: commas in strings and arrays': {'result': {'type': 'tuple', 'value': ["'a,b'", "ARRAY['x', 'y']::text[]", "'it''s, fine'"]}, 'calls': {}},
    'fields: an empty row': {'result': {'type': 'tuple', 'value': ['']}, 'calls': {}},
    'fields: a trailing comma': {'result': {'type': 'tuple', 'value': ["'a'", '']}, 'calls': {}},
    'string: plain': {'result': {'type': 'str', 'value': 'In progress'}, 'calls': {}},
    'string: a doubled quote': {'result': {'type': 'str', 'value': "it's"}, 'calls': {}},
    'string: empty': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'string: not a literal': {'result': {'raised': 'ValueError', 'message': "expected SQL string literal, got 'NULL'"}, 'calls': {}},
    'string: half a literal': {'result': {'raised': 'ValueError', 'message': 'expected SQL string literal, got "\'open"'}, 'calls': {}},
    'nullable: NULL': {'result': {'type': 'NoneType', 'value': None}, 'calls': {}},
    'nullable: a string': {'result': {'type': 'str', 'value': 'x'}, 'calls': {'_parse_sql_string': 1}},
    'nullable: neither': {'result': {'raised': 'ValueError', 'message': "expected SQL string literal, got 'x'"}, 'calls': {'_parse_sql_string': 1}},
    'bool: true': {'result': {'type': 'bool', 'value': True}, 'calls': {}},
    'bool: false': {'result': {'type': 'bool', 'value': False}, 'calls': {}},
    'bool: neither': {'result': {'raised': 'ValueError', 'message': "expected SQL boolean, got 'yes'"}, 'calls': {}},
    'array: two': {'result': {'type': 'tuple', 'value': ['main', 'app']}, 'calls': {'_parse_sql_string': 2, '_split_sql_fields': 1}},
    'array: empty': {'result': {'type': 'tuple', 'value': []}, 'calls': {}},
    'array: a comma inside a string': {'result': {'type': 'tuple', 'value': ['a,b', "it's"]}, 'calls': {'_parse_sql_string': 2, '_split_sql_fields': 1}},
    'array: not an array': {'result': {'raised': 'ValueError', 'message': 'expected SQL text array, got "\'main\'"'}, 'calls': {}},
    'array: another type': {'result': {'raised': 'ValueError', 'message': 'expected SQL text array, got "ARRAY[\'a\']::int[]"'}, 'calls': {}},
    'stages: synthetic': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 4, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 18, '_parse_sql_text_array': 5, '_schema_sql_text': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1}},
    'stages: the real schema.sql': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'backlog', 'display_label': 'Backlog', 'rank': 1, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'analysis', 'display_label': 'Triage', 'rank': 2, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'Implementation', 'rank': 3, 'owner_roles': ['main', 'app', 'ops', 'perf', 'research'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'inspection', 'display_label': 'Inspection', 'rank': 4, 'owner_roles': ['inspector'], 'entry_gate_field': 'needs_inspection', 'gate_skip_to': 'audit', 'exit_signoff_field': 'inspector_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': 'Audit', 'rank': 5, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'dat', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'dat', 'display_label': 'DAT', 'rank': 6, 'owner_roles': ['director'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'user_review', 'display_label': 'UAT', 'rank': 7, 'owner_roles': ['user'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'user_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Final Sign-Off', 'rank': 8, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 9, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}, {'class': 'WorkflowStageSeed', 'name': 'cancelled', 'display_label': 'Cancelled', 'rank': 10, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1}},
    'stages: a short row': {'result': {'raised': 'ValueError', 'message': "workflow_stages seed row has 3 fields, expected 8: 'a', 'A', 0"}, 'calls': {'_insert_values_block': 1, '_schema_sql_text': 1, '_split_sql_fields': 1, '_split_sql_tuple_rows': 1}},
    'stages: no insert': {'result': {'raised': 'ValueError', 'message': 'could not find ticket_board.workflow_stages seed INSERT in schema.sql'}, 'calls': {'_insert_values_block': 1, '_schema_sql_text': 1}},
    'stages: the text reader rebound on project_provision': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 4, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 18, '_parse_sql_text_array': 5, '_schema_sql_text rebound': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1}},
    'stages: the seed class rebound on project_provision': {'result': {'type': 'tuple', 'value': [{'class': 'P465WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'P465WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'P465WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'P465WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'P465WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 4, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 18, '_parse_sql_text_array': 5, '_schema_sql_text': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1}},
    'transitions: synthetic': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'in_progress', 'action_name': 'release_draft', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['main', 'app', 'ops'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 12, '_parse_sql_string': 26, '_parse_sql_text_array': 6, '_schema_sql_text': 1, '_split_sql_fields': 12, '_split_sql_tuple_rows': 1}},
    'transitions: the seed class rebound on project_provision': {'result': {'type': 'tuple', 'value': [{'class': 'P465WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'in_progress', 'action_name': 'release_draft', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': True}, {'class': 'P465WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['main', 'app', 'ops'], 'owner_scoped': True, 'director_override': False}, {'class': 'P465WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'P465WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'P465WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': True}, {'class': 'P465WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 12, '_parse_sql_string': 26, '_parse_sql_text_array': 6, '_schema_sql_text': 1, '_split_sql_fields': 12, '_split_sql_tuple_rows': 1}},
    'transitions: the real schema.sql': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'analysis', 'action_name': 'release_draft', 'allowed_roles': ['director', 'user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'analysis', 'action_name': 'start_task', 'allowed_roles': ['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector'], 'owner_scoped': True, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'user_review', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'inspection', 'action_name': 'submit_to_inspection', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit_without_commit', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'request_commit_exempt', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'implementer_kick_back', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'audit', 'action_name': 'inspector_sign_off', 'allowed_roles': ['inspector'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'in_progress', 'action_name': 'inspector_kick_back', 'allowed_roles': ['inspector'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'dat', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'director_review', 'action_name': 'entry_gate_skip', 'allowed_roles': [], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'user_review', 'action_name': 'director_dat_sign_off', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'inspection', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'director_review', 'action_name': 'user_sign_off', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 118, '_parse_sql_string': 271, '_parse_sql_text_array': 59, '_schema_sql_text': 1, '_split_sql_fields': 117, '_split_sql_tuple_rows': 1}},
    'transitions: a long row': {'result': {'raised': 'ValueError', 'message': "workflow_transitions seed row has 7 fields, expected 6: 'a', 'b', 'c', ARRAY[]::text[], true, false, true"}, 'calls': {'_insert_values_block': 1, '_schema_sql_text': 1, '_split_sql_fields': 1, '_split_sql_tuple_rows': 1}},
    'implementers: main first': {'result': {'type': 'tuple', 'value': ['main', 'app', 'perf']}, 'calls': {}},
    'implementers: without main': {'result': {'type': 'tuple', 'value': ['app', 'perf']}, 'calls': {}},
    'owners: draft': {'result': {'type': 'tuple', 'value': ['designer']}, 'calls': {}},
    'owners: in progress': {'result': {'type': 'tuple', 'value': ['main', 'app', 'perf']}, 'calls': {'_project_implementation_owner_roles': 1}},
    'owners: audit': {'result': {'type': 'tuple', 'value': ['audit', 'inspector']}, 'calls': {}},
    'owners: another stage': {'result': {'type': 'tuple', 'value': ['director']}, 'calls': {}},
    'owners: the full pgu seed': {'result': {'type': 'tuple', 'value': ['main', 'app', 'ops', 'perf', 'research']}, 'calls': {}},
    'allowed: implementers and audit replaced once': {'result': {'type': 'tuple', 'value': ['director', 'app', 'main', 'perf', 'audit', 'inspector']}, 'calls': {'_dedupe': 1}},
    'allowed: no audit roles': {'result': {'type': 'tuple', 'value': ['app', 'main']}, 'calls': {'_dedupe': 1}},
    'allowed: the full pgu seed': {'result': {'type': 'tuple', 'value': ['main', 'audit', 'main']}, 'calls': {}},
    'allowed: the implementer default rebound on project_provision': {'result': {'type': 'tuple', 'value': ['director', 'app', 'main', 'perf']}, 'calls': {'_dedupe': 1}},
    'allowed: the dedupe rebound on project_provision': {'result': {'type': 'tuple', 'value': ['rebound']}, 'calls': {'_dedupe rebound': 1}},
    'allowed: implementers inserted once, seen with the dedupe passing everything': {'result': {'type': 'tuple', 'value': ['director', 'app', 'main', 'perf', 'audit', 'inspector']}, 'calls': {'_dedupe passing': 1}},
    'rank: synthetic stages, no vcs': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 4, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {}},
    'rank: with a vcs stage': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'vcs', 'display_label': 'VCS', 'rank': 4, 'owner_roles': ['ops'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 5, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {}},
    'names: default': {'result': {'type': 'tuple', 'value': ['draft', 'analysis', 'in_progress', 'audit', 'dat', 'user_review', 'director_review', 'done', 'cancelled']}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'project_workflow_stages': 1, 'schema_workflow_stages': 1}},
    'names: lean': {'result': {'type': 'tuple', 'value': ['draft', 'analysis', 'in_progress', 'director_review', 'done', 'cancelled']}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 6, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'project_workflow_stages': 1, 'schema_workflow_stages': 1}},
    'names: the excluded stages rebound on project_provision': {'result': {'type': 'tuple', 'value': ['draft', 'analysis', 'in_progress', 'audit', 'user_review', 'director_review', 'done', 'cancelled']}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 8, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'project_workflow_stages': 1, 'schema_workflow_stages': 1}},
    'project stages: default': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'analysis', 'display_label': 'Triage', 'rank': 1, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'Implementation', 'rank': 2, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': 'Audit', 'rank': 3, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'dat', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'dat', 'display_label': 'DAT', 'rank': 4, 'owner_roles': ['director'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'user_review', 'display_label': 'UAT', 'rank': 5, 'owner_roles': ['user'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'user_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Final Sign-Off', 'rank': 6, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 9, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}, {'class': 'WorkflowStageSeed', 'name': 'cancelled', 'display_label': 'Cancelled', 'rank': 10, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: shaped': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'analysis', 'display_label': 'Triage', 'rank': 1, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'Implementation', 'rank': 2, 'owner_roles': ['main', 'app', 'perf'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': 'Audit', 'rank': 3, 'owner_roles': ['audit', 'inspector'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'dat', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'dat', 'display_label': 'DAT', 'rank': 4, 'owner_roles': ['director'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'user_review', 'display_label': 'UAT', 'rank': 5, 'owner_roles': ['user'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'user_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Final Sign-Off', 'rank': 6, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'vcs', 'display_label': 'VCS', 'rank': 7, 'owner_roles': ['ops'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 10, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}, {'class': 'WorkflowStageSeed', 'name': 'cancelled', 'display_label': 'Cancelled', 'rank': 11, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: lean': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'analysis', 'display_label': 'Triage', 'rank': 1, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'Implementation', 'rank': 2, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Final Sign-Off', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 9, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}, {'class': 'WorkflowStageSeed', 'name': 'cancelled', 'display_label': 'Cancelled', 'rank': 10, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 6, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: no main': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'analysis', 'display_label': 'Triage', 'rank': 1, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'Implementation', 'rank': 2, 'owner_roles': ['app', 'perf'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': 'Audit', 'rank': 3, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'dat', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'dat', 'display_label': 'DAT', 'rank': 4, 'owner_roles': ['director'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'user_review', 'display_label': 'UAT', 'rank': 5, 'owner_roles': ['user'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'user_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Final Sign-Off', 'rank': 6, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 9, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}, {'class': 'WorkflowStageSeed', 'name': 'cancelled', 'display_label': 'Cancelled', 'rank': 10, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: pgu': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'backlog', 'display_label': 'Backlog', 'rank': 1, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'analysis', 'display_label': 'Triage', 'rank': 2, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'Implementation', 'rank': 3, 'owner_roles': ['main', 'app', 'ops', 'perf', 'research'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'inspection', 'display_label': 'Inspection', 'rank': 4, 'owner_roles': ['inspector'], 'entry_gate_field': 'needs_inspection', 'gate_skip_to': 'audit', 'exit_signoff_field': 'inspector_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': 'Audit', 'rank': 5, 'owner_roles': ['audit'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'dat', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'dat', 'display_label': 'DAT', 'rank': 6, 'owner_roles': ['director'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'user_review', 'display_label': 'UAT', 'rank': 7, 'owner_roles': ['user'], 'entry_gate_field': 'needs_user_signoff', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'user_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Final Sign-Off', 'rank': 8, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 9, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}, {'class': 'WorkflowStageSeed', 'name': 'cancelled', 'display_label': 'Cancelled', 'rank': 10, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 11, '_parse_sql_nullable_string': 33, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_schema_sql_text': 1, '_split_sql_fields': 18, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: synthetic schema, shaped': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app', 'perf'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit', 'inspector'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'vcs', 'display_label': 'VCS', 'rank': 4, 'owner_roles': ['ops'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 5, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 18, '_parse_sql_text_array': 5, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 5, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: synthetic schema, lean': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 2, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 4, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 18, '_parse_sql_text_array': 5, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 4, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: a gate into an excluded stage cleared': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 2, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 4, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 19, '_parse_sql_text_array': 5, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 4, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project stages: a gate into a kept stage kept': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowStageSeed', 'name': 'draft', 'display_label': 'Draft', 'rank': 0, 'owner_roles': ['designer'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'in_progress', 'display_label': 'In (progress)', 'rank': 1, 'owner_roles': ['main', 'app', 'perf'], 'entry_gate_field': None, 'gate_skip_to': 'audit', 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'audit', 'display_label': "Audit, it's", 'rank': 2, 'owner_roles': ['audit', 'inspector'], 'entry_gate_field': 'needs_audit', 'gate_skip_to': 'director_review', 'exit_signoff_field': 'audit_signoff', 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'director_review', 'display_label': 'Director', 'rank': 3, 'owner_roles': ['director'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'vcs', 'display_label': 'VCS', 'rank': 4, 'owner_roles': ['ops'], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': False}, {'class': 'WorkflowStageSeed', 'name': 'done', 'display_label': 'Done', 'rank': 5, 'owner_roles': [], 'entry_gate_field': None, 'gate_skip_to': None, 'exit_signoff_field': None, 'is_terminal': True}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 5, '_parse_sql_nullable_string': 15, '_parse_sql_string': 19, '_parse_sql_text_array': 5, '_project_implementation_owner_roles': 1, '_project_workflow_owner_roles': 5, '_rank_project_stages': 1, '_schema_sql_text': 1, '_split_sql_fields': 9, '_split_sql_tuple_rows': 1, 'schema_workflow_stages': 1}},
    'project transitions: default': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'analysis', 'action_name': 'release_draft', 'allowed_roles': ['designer', 'director', 'user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['app', 'main'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'user_review', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'main'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit_without_commit', 'allowed_roles': ['app', 'main'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'implementer_kick_back', 'allowed_roles': ['app', 'main'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'dat', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'director_review', 'action_name': 'entry_gate_skip', 'allowed_roles': [], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'user_review', 'action_name': 'director_dat_sign_off', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'director_review', 'action_name': 'user_sign_off', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_dedupe': 38, '_insert_values_block': 2, '_parse_sql_bool': 129, '_parse_sql_nullable_string': 33, '_parse_sql_string': 315, '_parse_sql_text_array': 70, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 37, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 135, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
    'project transitions: shaped': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'analysis', 'action_name': 'release_draft', 'allowed_roles': ['designer', 'director', 'user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['app', 'main', 'perf'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'user_review', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'main', 'perf'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit_without_commit', 'allowed_roles': ['app', 'main', 'perf'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'implementer_kick_back', 'allowed_roles': ['app', 'main', 'perf'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'dat', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit', 'inspector'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit', 'inspector'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit', 'inspector'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'director_review', 'action_name': 'entry_gate_skip', 'allowed_roles': [], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'user_review', 'action_name': 'director_dat_sign_off', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'director_review', 'action_name': 'user_sign_off', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'vcs', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'vcs', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['ops'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_dedupe': 37, '_insert_values_block': 2, '_parse_sql_bool': 129, '_parse_sql_nullable_string': 33, '_parse_sql_string': 315, '_parse_sql_text_array': 70, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 36, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 135, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
    'project transitions: lean': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'analysis', 'action_name': 'release_draft', 'allowed_roles': ['director', 'user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['app', 'main'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'director_review', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'main'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'implementer_kick_back', 'allowed_roles': ['app', 'main'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_dedupe': 18, '_insert_values_block': 2, '_parse_sql_bool': 129, '_parse_sql_nullable_string': 33, '_parse_sql_string': 315, '_parse_sql_text_array': 70, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 17, '_project_workflow_owner_roles': 6, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 135, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
    'project transitions: no main': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'analysis', 'action_name': 'release_draft', 'allowed_roles': ['director', 'user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['app', 'perf'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'user_review', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'perf'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit_without_commit', 'allowed_roles': ['app', 'perf'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'implementer_kick_back', 'allowed_roles': ['app', 'perf'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'dat', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'director_review', 'action_name': 'entry_gate_skip', 'allowed_roles': [], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'user_review', 'action_name': 'director_dat_sign_off', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'director_review', 'action_name': 'user_sign_off', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_dedupe': 38, '_insert_values_block': 2, '_parse_sql_bool': 129, '_parse_sql_nullable_string': 33, '_parse_sql_string': 315, '_parse_sql_text_array': 70, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 37, '_project_workflow_owner_roles': 9, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 135, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
    'project transitions: pgu': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'analysis', 'action_name': 'release_draft', 'allowed_roles': ['director', 'user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'analysis', 'action_name': 'start_task', 'allowed_roles': ['director', 'main', 'app', 'ops', 'perf', 'research', 'audit', 'inspector'], 'owner_scoped': True, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'backlog', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'in_progress', 'action_name': 'start_work', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'user_review', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'analysis', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'inspection', 'action_name': 'submit_to_inspection', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit_without_commit', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'request_commit_exempt', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'implementer_kick_back', 'allowed_roles': ['main', 'app', 'ops', 'perf', 'research'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'audit', 'action_name': 'inspector_sign_off', 'allowed_roles': ['inspector'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'in_progress', 'action_name': 'inspector_kick_back', 'allowed_roles': ['inspector'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'inspection', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'dat', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'director_review', 'action_name': 'entry_gate_skip', 'allowed_roles': [], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'user_review', 'action_name': 'director_dat_sign_off', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'director_dat_kick_back', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'dat', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'inspection', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'director_review', 'action_name': 'user_sign_off', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'audit', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'user_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'in_progress', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'cancelled', 'action_name': 'cancel', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'user_reopen', 'allowed_roles': ['user'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'done', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'analysis', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'cancelled', 'to_stage': 'backlog', 'action_name': 'defer', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_insert_values_block': 1, '_parse_sql_bool': 118, '_parse_sql_string': 271, '_parse_sql_text_array': 59, '_schema_sql_text': 1, '_split_sql_fields': 117, '_split_sql_tuple_rows': 1, 'schema_workflow_transitions': 1}},
    'project transitions: synthetic schema, shaped': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'in_progress', 'action_name': 'release_draft', 'allowed_roles': ['designer', 'director'], 'owner_scoped': False, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'main', 'perf'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit', 'inspector'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'in_progress', 'action_name': 'audit_kick_back', 'allowed_roles': ['audit', 'inspector'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'vcs', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'vcs', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['ops'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_dedupe': 5, '_insert_values_block': 2, '_parse_sql_bool': 17, '_parse_sql_nullable_string': 15, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 4, '_project_workflow_owner_roles': 5, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 21, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
    'project transitions: synthetic schema, lean': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'in_progress', 'action_name': 'release_draft', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'director_review', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'main'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': True}]}, 'calls': {'_dedupe': 4, '_insert_values_block': 2, '_parse_sql_bool': 17, '_parse_sql_nullable_string': 15, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 3, '_project_workflow_owner_roles': 4, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 21, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
    'project transitions: the excluded actions rebound on project_provision': {'result': {'type': 'tuple', 'value': [{'class': 'WorkflowTransitionSeed', 'from_stage': 'draft', 'to_stage': 'in_progress', 'action_name': 'release_draft', 'allowed_roles': ['designer', 'director'], 'owner_scoped': False, 'director_override': True}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'in_progress', 'to_stage': 'audit', 'action_name': 'submit_to_audit', 'allowed_roles': ['app', 'main', 'perf'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'audit', 'to_stage': 'director_review', 'action_name': 'audit_sign_off', 'allowed_roles': ['audit', 'inspector'], 'owner_scoped': True, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'director_review', 'to_stage': 'vcs', 'action_name': 'route', 'allowed_roles': ['director'], 'owner_scoped': False, 'director_override': False}, {'class': 'WorkflowTransitionSeed', 'from_stage': 'vcs', 'to_stage': 'done', 'action_name': 'mark_done', 'allowed_roles': ['ops'], 'owner_scoped': False, 'director_override': False}]}, 'calls': {'_dedupe': 4, '_insert_values_block': 2, '_parse_sql_bool': 17, '_parse_sql_nullable_string': 15, '_parse_sql_string': 44, '_parse_sql_text_array': 11, '_project_implementation_owner_roles': 1, '_project_workflow_allowed_roles': 3, '_project_workflow_owner_roles': 5, '_rank_project_stages': 1, '_schema_sql_text': 2, '_split_sql_fields': 21, '_split_sql_tuple_rows': 2, 'project_workflow_stages': 1, 'schema_workflow_stages': 1, 'schema_workflow_transitions': 1}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = 10

# --- the cases, shared verbatim with `gold465.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the moved functions and records the answer or the exact exception, and how many times it called
# each of `project_provision`'s helpers (recorded there and passed through). The parser gets synthetic schema.sql text,
# or the real schema.sql beside the module; the projection gets plans built by `build_plan` for a synthetic owner under
# /p465 -- nothing is created, read (but schema.sql) or run. A case may rebind a `project_provision` name to show it is
# read there when the function runs.
STAGE_ROW = "('{0}', '{1}', {2}, ARRAY[{3}]::text[], {4}, {5}, {6}, {7})"
TRANSITION_ROW = "('{0}', '{1}', '{2}', ARRAY[{3}]::text[], {4}, {5})"
SCHEMA = ("-- synthetic schema.sql\nINSERT INTO ticket_board.workflow_stages (name, display_label, rank, owner_roles, entry_gate_field, gate_skip_to,"
          " exit_signoff_field, is_terminal)\nVALUES\n    " + ",\n    ".join([
              STAGE_ROW.format("draft", "Draft", 0, "'designer'", "NULL", "NULL", "NULL", "false"),
              STAGE_ROW.format("in_progress", "In (progress)", 1, "'main', 'app'", "NULL", "NULL", "NULL", "false"),
              STAGE_ROW.format("audit", "Audit, it''s", 2, "'audit'", "'needs_audit'", "'director_review'", "'audit_signoff'", "false"),
              STAGE_ROW.format("director_review", "Director", 3, "'director'", "NULL", "NULL", "NULL", "FALSE"),
              STAGE_ROW.format("done", "Done", 4, "", "NULL", "NULL", "NULL", "TRUE")]) +
          "\nON CONFLICT (name) DO NOTHING;\n\nINSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles, owner_scoped,"
          " director_override)\nVALUES\n    " + ",\n    ".join([
              TRANSITION_ROW.format("draft", "in_progress", "release_draft", "'director'", "false", "true"),
              TRANSITION_ROW.format("in_progress", "audit", "submit_to_audit", "'main', 'app', 'ops'", "true", "false"),
              TRANSITION_ROW.format("audit", "director_review", "audit_sign_off", "'audit'", "false", "false"),
              TRANSITION_ROW.format("audit", "in_progress", "audit_kick_back", "'audit'", "false", "false"),
              TRANSITION_ROW.format("director_review", "done", "mark_done", "'director'", "false", "true"),
              TRANSITION_ROW.format("in_progress", "backlog", "defer", "'director'", "false", "false")]) +
          "\nON CONFLICT (from_stage, to_stage, action_name) DO NOTHING;\n")
# The same, with the implementation stage's gate skipping to audit: a stage a tenant without auditors does not have.
SCHEMA_SKIP = SCHEMA.replace("('in_progress', 'In (progress)', 1, ARRAY['main', 'app']::text[], NULL, NULL,",
                             "('in_progress', 'In (progress)', 1, ARRAY['main', 'app']::text[], NULL, 'audit',")
assert SCHEMA_SKIP != SCHEMA
PLANS = {
    "default": {},
    "shaped": {"implementer_roles": ["app", "main", "perf"], "audit_roles": ["audit", "inspector"], "vcs_close_role": "ops"},
    "lean": {"include_designer": False, "include_audit": False},
    "no main": {"implementer_roles": ["app", "perf"], "include_designer": False},
    "pgu": {"project": "pgu"},
}
CASES = {
    "text: given": {"call": "_schema_sql_text", "args": ["-- given"]},
    "text: the schema beside the module": {"call": "_schema_sql_text", "args": [None]},
    "text: the path rebound on project_provision": {"call": "_schema_sql_text", "args": [None], "rebind": {"SCHEMA_SQL_PATH": "SYNTHETIC"}},
    "values: stages": {"call": "_insert_values_block", "args": ["SCHEMA", "workflow_stages"]},
    "values: transitions": {"call": "_insert_values_block", "args": ["SCHEMA", "workflow_transitions"]},
    "values: a missing table": {"call": "_insert_values_block", "args": ["SCHEMA", "workflow_nothing"]},
    "values: a table name that is not a pattern": {"call": "_insert_values_block", "args": ["SCHEMA", "workflow.stages"]},
    "values: no ON CONFLICT": {"call": "_insert_values_block", "args": ["INSERT INTO ticket_board.workflow_stages (a) VALUES ('x');\n", "workflow_stages"]},
    "rows: two rows": {"call": "_split_sql_tuple_rows", "args": ["('a', 1), ( 'b' , 2 )"]},
    "rows: parentheses and doubled quotes inside strings": {"call": "_split_sql_tuple_rows", "args": ["('a (b)', 'it''s)'), ('c')"]},
    "rows: nested parentheses kept": {"call": "_split_sql_tuple_rows", "args": ["(f(x), 2)"]},
    "rows: none": {"call": "_split_sql_tuple_rows", "args": ["  "]},
    "rows: unterminated": {"call": "_split_sql_tuple_rows", "args": ["('a', 1"]},
    "rows: an unterminated quote": {"call": "_split_sql_tuple_rows", "args": ["('a, 1)"]},
    "rows: a stray close": {"call": "_split_sql_tuple_rows", "args": ["('a'))"]},
    "fields: plain": {"call": "_split_sql_fields", "args": ["'a', 1, NULL"]},
    "fields: commas in strings and arrays": {"call": "_split_sql_fields", "args": ["'a,b', ARRAY['x', 'y']::text[], 'it''s, fine'"]},
    "fields: an empty row": {"call": "_split_sql_fields", "args": [""]},
    "fields: a trailing comma": {"call": "_split_sql_fields", "args": ["'a',"]},
    "string: plain": {"call": "_parse_sql_string", "args": ["  'In progress' "]},
    "string: a doubled quote": {"call": "_parse_sql_string", "args": ["'it''s'"]},
    "string: empty": {"call": "_parse_sql_string", "args": ["''"]},
    "string: not a literal": {"call": "_parse_sql_string", "args": ["NULL"]},
    "string: half a literal": {"call": "_parse_sql_string", "args": ["'open"]},
    "nullable: NULL": {"call": "_parse_sql_nullable_string", "args": [" null "]},
    "nullable: a string": {"call": "_parse_sql_nullable_string", "args": ["'x'"]},
    "nullable: neither": {"call": "_parse_sql_nullable_string", "args": ["x"]},
    "bool: true": {"call": "_parse_sql_bool", "args": [" TRUE "]},
    "bool: false": {"call": "_parse_sql_bool", "args": ["false"]},
    "bool: neither": {"call": "_parse_sql_bool", "args": ["yes"]},
    "array: two": {"call": "_parse_sql_text_array", "args": ["ARRAY['main', 'app']::text[]"]},
    "array: empty": {"call": "_parse_sql_text_array", "args": ["ARRAY[]::text[]"]},
    "array: a comma inside a string": {"call": "_parse_sql_text_array", "args": ["ARRAY['a,b', 'it''s']::text[]"]},
    "array: not an array": {"call": "_parse_sql_text_array", "args": ["'main'"]},
    "array: another type": {"call": "_parse_sql_text_array", "args": ["ARRAY['a']::int[]"]},
    "stages: synthetic": {"call": "schema_workflow_stages", "args": ["SCHEMA"]},
    "stages: the real schema.sql": {"call": "schema_workflow_stages", "args": []},
    "stages: a short row": {"call": "schema_workflow_stages", "args": ["INSERT INTO ticket_board.workflow_stages (x) VALUES ('a', 'A', 0)\nON CONFLICT DO NOTHING;"]},
    "stages: no insert": {"call": "schema_workflow_stages", "args": ["-- empty"]},
    "stages: the text reader rebound on project_provision": {"call": "schema_workflow_stages", "args": [], "rebind": {"_schema_sql_text": True}},
    "stages: the seed class rebound on project_provision": {"call": "schema_workflow_stages", "args": ["SCHEMA"], "rebind": {"WorkflowStageSeed": True}},
    "transitions: synthetic": {"call": "schema_workflow_transitions", "args": ["SCHEMA"]},
    "transitions: the seed class rebound on project_provision": {"call": "schema_workflow_transitions", "args": ["SCHEMA"], "rebind": {"WorkflowTransitionSeed": True}},
    "transitions: the real schema.sql": {"call": "schema_workflow_transitions", "args": []},
    "transitions: a long row": {"call": "schema_workflow_transitions",
                                "args": ["INSERT INTO ticket_board.workflow_transitions (x) VALUES ('a', 'b', 'c', ARRAY[]::text[], true, false, true)\nON CONFLICT DO NOTHING;"]},
    "implementers: main first": {"call": "_project_implementation_owner_roles", "plan": "shaped"},
    "implementers: without main": {"call": "_project_implementation_owner_roles", "plan": "no main"},
    "owners: draft": {"call": "_project_workflow_owner_roles", "stage": "draft", "plan": "shaped"},
    "owners: in progress": {"call": "_project_workflow_owner_roles", "stage": "in_progress", "plan": "shaped"},
    "owners: audit": {"call": "_project_workflow_owner_roles", "stage": "audit", "plan": "shaped"},
    "owners: another stage": {"call": "_project_workflow_owner_roles", "stage": "director_review", "plan": "shaped"},
    "owners: the full pgu seed": {"call": "_project_workflow_owner_roles", "stage": "in_progress", "plan": "pgu"},
    "allowed: implementers and audit replaced once": {"call": "_project_workflow_allowed_roles", "roles": ["director", "main", "app", "audit", "ops", "director"], "plan": "shaped"},
    "allowed: no audit roles": {"call": "_project_workflow_allowed_roles", "roles": ["audit", "research"], "plan": "lean"},
    "allowed: the full pgu seed": {"call": "_project_workflow_allowed_roles", "roles": ["main", "audit", "main"], "plan": "pgu"},
    "allowed: the implementer default rebound on project_provision": {"call": "_project_workflow_allowed_roles", "roles": ["director", "custom"], "plan": "shaped",
                                                                      "rebind": {"DEFAULT_IMPLEMENTER_ROLES": ("custom",)}},
    "allowed: the dedupe rebound on project_provision": {"call": "_project_workflow_allowed_roles", "roles": ["director", "main"], "plan": "shaped", "rebind": {"_dedupe": True}},
    "allowed: implementers inserted once, seen with the dedupe passing everything": {"call": "_project_workflow_allowed_roles", "roles": ["director", "main", "app", "ops", "audit"],
                                                                                     "plan": "shaped", "rebind": {"_dedupe passing": True}},
    "rank: synthetic stages, no vcs": {"call": "_rank_project_stages", "stages": "synthetic"},
    "rank: with a vcs stage": {"call": "_rank_project_stages", "stages": "synthetic+vcs"},
    "names: default": {"call": "project_workflow_state_names", "plan": "default"},
    "names: lean": {"call": "project_workflow_state_names", "plan": "lean"},
    "names: the excluded stages rebound on project_provision": {"call": "project_workflow_state_names", "plan": "default",
                                                                "rebind": {"TENANT_WORKFLOW_EXCLUDED_STAGES": frozenset({"backlog", "inspection", "dat"})}},
    **{f"project stages: {p}": {"call": "project_workflow_stages", "plan": p} for p in ("default", "shaped", "lean", "no main", "pgu")},
    "project stages: synthetic schema, shaped": {"call": "project_workflow_stages", "plan": "shaped", "schema": True},
    "project stages: synthetic schema, lean": {"call": "project_workflow_stages", "plan": "lean", "schema": True},
    "project stages: a gate into an excluded stage cleared": {"call": "project_workflow_stages", "plan": "lean", "schema": "skip"},
    "project stages: a gate into a kept stage kept": {"call": "project_workflow_stages", "plan": "shaped", "schema": "skip"},
    **{f"project transitions: {p}": {"call": "project_workflow_transitions", "plan": p} for p in ("default", "shaped", "lean", "no main", "pgu")},
    "project transitions: synthetic schema, shaped": {"call": "project_workflow_transitions", "plan": "shaped", "schema": True},
    "project transitions: synthetic schema, lean": {"call": "project_workflow_transitions", "plan": "lean", "schema": True},
    "project transitions: the excluded actions rebound on project_provision": {"call": "project_workflow_transitions", "plan": "shaped", "schema": True,
                                                                              "rebind": {"TENANT_WORKFLOW_EXCLUDED_ACTIONS": frozenset({"audit_kick_back"})}},
}
FUNCTIONS = ("_schema_sql_text", "_insert_values_block", "_split_sql_tuple_rows", "_split_sql_fields", "_parse_sql_string", "_parse_sql_nullable_string",
             "_parse_sql_bool", "_parse_sql_text_array", "schema_workflow_stages", "schema_workflow_transitions", "_project_implementation_owner_roles",
             "_project_workflow_owner_roles", "_project_workflow_allowed_roles", "_rank_project_stages", "project_workflow_stages", "project_workflow_transitions",
             "project_workflow_state_names")
PASSED = ("_dedupe", "_schema_sql_text", "_insert_values_block", "_split_sql_tuple_rows", "_split_sql_fields", "_parse_sql_string", "_parse_sql_nullable_string",
          "_parse_sql_bool", "_parse_sql_text_array", "schema_workflow_stages", "schema_workflow_transitions", "_project_implementation_owner_roles",
          "_project_workflow_owner_roles", "_project_workflow_allowed_roles", "_rank_project_stages", "project_workflow_stages")
REBINDABLE = ("SCHEMA_SQL_PATH", "TENANT_WORKFLOW_EXCLUDED_STAGES", "TENANT_WORKFLOW_EXCLUDED_ACTIONS", "DEFAULT_IMPLEMENTER_ROLES", "WorkflowStageSeed",
              "WorkflowTransitionSeed")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import dataclasses, hashlib, shutil, tempfile
    from pathlib import Path as _P
    counts: dict = {}

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"class": type(value).__name__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted(norm(v) for v in value)
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, str) and len(value) > 400:
            return f"TEXT {len(value)} chars sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
        if isinstance(value, (str, bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    def plan_for(name):
        kwargs = {"project": "p465", "owner_user": "p465-agent", "owner_home": _P("/p465/home"), "port": 34465, **PLANS[name]}
        return saved["build_plan"](**kwargs)

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "build_plan")}
    tmp = _P(tempfile.mkdtemp(prefix="syrd465-")).resolve()
    try:
        args = [SCHEMA if a == "SCHEMA" else a for a in spec.get("args", [])]
        kwargs = {}
        if "plan" in spec:
            plan = plan_for(spec["plan"])
            if "stage" in spec:
                stage = next(s for s in saved["schema_workflow_stages"]() if s.name == spec["stage"])
                args = [stage, plan]
            elif "roles" in spec:
                args = [tuple(spec["roles"]), plan]
            else:
                args = [plan]
            if spec.get("schema"):
                kwargs["schema_sql"] = SCHEMA_SKIP if spec["schema"] == "skip" else SCHEMA
        if "stages" in spec:
            stages = list(saved["schema_workflow_stages"](SCHEMA))
            if spec["stages"] == "synthetic+vcs":
                stages.insert(-1, saved["WorkflowStageSeed"]("vcs", "VCS", 9, ("ops",), None, None, None, False))
            args = [tuple(stages)]
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "SCHEMA_SQL_PATH":
                (tmp / "schema.sql").write_text(SCHEMA, encoding="utf-8")
                t.SCHEMA_SQL_PATH = tmp / "schema.sql"
            elif name == "_schema_sql_text":
                t._schema_sql_text = lambda schema_sql=None: note("_schema_sql_text rebound") or SCHEMA
            elif name in ("WorkflowStageSeed", "WorkflowTransitionSeed"):
                setattr(t, name, type("P465" + name, (saved[name],), {}))
            elif name == "_dedupe":
                t._dedupe = lambda values: note("_dedupe rebound") or ("rebound",)
            elif name == "_dedupe passing":
                t._dedupe = lambda values: note("_dedupe passing") or tuple(values)
            else:
                setattr(t, name, value)
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        try:
            got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc).replace(str(tmp), "TMP"))}
        else:
            result = {"type": type(got).__name__, "value": norm(got)}
        return {"result": result, "calls": dict(sorted(counts.items()))}
    finally:
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


#: What project_provision keeps that the moved code reads there when it runs, and the definitions there that name the moved ones.
KEPT = ("_dedupe", "DEFAULT_IMPLEMENTER_ROLES", "render_project_role_constraint_sql", "render_workflow_sql", "_workflow_stage_rows_sql",
        "_workflow_transition_rows_sql", "render_add_role_sql", "render_vcs_close_role_sql")
#: The three constants, as the baseline wrote them.
CONSTANT_TEXT = {
    "SCHEMA_SQL_PATH": "Path(__file__).with_name('schema.sql')",
    "TENANT_WORKFLOW_EXCLUDED_STAGES": "frozenset({'backlog', 'inspection'})",
    "TENANT_WORKFLOW_EXCLUDED_ACTIONS": "frozenset({'defer', 'request_commit_exempt', 'start_task', 'submit_to_inspection'})",
}
#: The two seed classes, their decorator and fields in order, as the baseline wrote them.
CLASS_FIELDS = {
    "WorkflowStageSeed": ["name", "display_label", "rank", "owner_roles", "entry_gate_field", "gate_skip_to", "exit_signoff_field", "is_terminal"],
    "WorkflowTransitionSeed": ["from_stage", "to_stage", "action_name", "allowed_roles", "owner_scoped", "director_override"],
}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The known effect of moving the seed classes' definitions (approved, not masked): their __module__ is now this module's.
#: Each is still one class object, reached the same way through project_provision and frontend_script_core.
SEED_CLASS_MODULE = "scripts.ticket_board.provision_workflow_projection"
#: The packets rendered through the direct script and the package: the default plan, and two whose workflow the plan shapes.
PACKET_VARIANTS = {
    "default": [],
    "shaped": ["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"],
    "lean": ["--no-include-designer", "--no-include-audit"],
}


def top_name(n: ast.AST) -> str | None:
    if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
        return n.name
    if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
        return n.targets[0].id
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
    result = python("import sys, scripts.ticket_board.provision_workflow_projection as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_same_schema() -> None:
    for order in (("scripts.ticket_board.provision_workflow_projection", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_workflow_projection"),
                  ("scripts.ticket_board.frontend_script_core", "scripts.ticket_board.provision_workflow_projection")):
        result = python("import importlib, pathlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_workflow_projection as m, "
                        "scripts.ticket_board.frontend_script_core as f; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS + tuple(CLASS_FIELDS)!r}}}), "
                        "f.WorkflowStageSeed is m.WorkflowStageSeed and f.schema_workflow_stages is m.schema_workflow_stages, "
                        "m.SCHEMA_SQL_PATH == pathlib.Path(t.__file__).with_name('schema.sql') and m.SCHEMA_SQL_PATH.is_file(), "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == f"True ['{SEED_CLASS_MODULE}'] True True True",
              f"{' then '.join(order)}: one object each, defined here (the seed classes' module included); frontend_script_core reaches the same class; "
              f"the same schema.sql beside project_provision; nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    import dataclasses
    import pathlib
    import re as _re
    import typing
    check(m.dataclass is dataclasses.dataclass and m.Path is pathlib.Path and m.re is _re and m.Sequence is typing.Sequence
          and t.dataclass is m.dataclass and t.Path is m.Path and t.Sequence is m.Sequence,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_direct_script_reads_the_same_schema_through_one_seed_class() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the moved functions'
    # fallback loads project_provision a second time beside it. Both, and the script itself, must hold the one seed class.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    result = python(f"import runpy, sys, pathlib; sys.path.insert(0, {str(script.parent)!r}); "
                    f"g = runpy.run_path({str(script)!r}, run_name='__syrd465_script__'); "
                    "plan = g['build_plan'](project='p465', owner_user='p465-agent', owner_home=pathlib.Path('/p465/home'), port=34465); "
                    "names = [s.name for s in g['project_workflow_stages'](plan)]; "
                    "m = sys.modules['provision_workflow_projection']; second = sys.modules['project_provision']; "
                    "print(g['WorkflowStageSeed'] is m.WorkflowStageSeed is second.WorkflowStageSeed, "
                    "g['WorkflowTransitionSeed'] is m.WorkflowTransitionSeed is second.WorkflowTransitionSeed, "
                    f"g['SCHEMA_SQL_PATH'] == pathlib.Path({str(script)!r}).with_name('schema.sql'), m.WorkflowStageSeed.__module__, len(names))")
    check(result.stdout.strip() == "True True True provision_workflow_projection 9",
          f"as a direct script: one seed class each (in the script, the module and the fallback's project_provision), the same schema.sql, "
          f"the default tenant's nine stages: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_workflow_projection.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its sibling included, read through it exactly as often as before: {through}")
        imports = [x for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        if expected:
            check(len(imports) == 2 and ast.unparse(node.body[first]) == CALL_TIME_IMPORT,
                  f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback, and nothing else imported")
        else:
            check(imports == [], f"{name}: reads nothing of project_provision and imports nothing")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT) and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import re", "from dataclasses import dataclass", "from pathlib import Path", "from typing import Sequence"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try))], f"the standard library only, at load: {top}")
    consts = {top_name(n): ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)}
    check(consts == CONSTANT_TEXT, f"the three constants are the baseline's expressions: {consts}")
    classes = {n.name: ([ast.unparse(d) for d in n.decorator_list], [ast.unparse(x.target) for x in n.body if isinstance(x, ast.AnnAssign)],
                        [x for x in n.body if isinstance(x, ast.AnnAssign) and x.value is not None])
               for n in tree.body if isinstance(n, ast.ClassDef)}
    check({k: (d, f) for k, (d, f, _) in classes.items()} == {k: (["dataclass(frozen=True)"], v) for k, v in CLASS_FIELDS.items()}
          and not any(v for _, _, v in classes.values()), f"the seed classes: frozen dataclasses, their fields in order, no defaults: {classes}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {name: (["None"] if name in ("_schema_sql_text", "schema_workflow_stages", "schema_workflow_transitions", "project_workflow_stages",
                                                   "project_workflow_transitions") else []) for name in FUNCTIONS},
          f"the defaults are the baseline's: None for every schema text, nothing else: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the twenty-two, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_workflow_projection" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_workflow_projection" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the twenty-two, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # A name stays reachable on project_provision: defined there, or -- once a later slice moves it on -- re-exported there,
    # unaliased; and the definitions that name them are counted wherever they now live, a later slice's read through
    # project_provision (provision.X) counting as the name.
    exported = {a.name for n in ast.walk(guard) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and set(KEPT) <= defined | exported, "project_provision defines none of them, and keeps what they read and the SQL renderers that name them, its own or re-exported")
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_workflow_projection"]
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
    # Three synthetic provisioning packets -- the default plan and two whose workflow the plan shapes -- each rendered by
    # `project_provision.py` run as a script (its import fallback taken) and through the package entry, in a test-owned directory.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd465-packet-")).resolve()
    try:
        workflows = {}
        for variant, extra in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p465", "--owner-user", "p465-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34465", "--output-dir", f"{work}/out", *extra]
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                if mode == "script":
                    probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd465_script'); raise SystemExit(g['main']({argv!r}))")
                else:
                    probe = f"import scripts.ticket_board.project_provision as pp; raise SystemExit(pp.main({argv!r}))"
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES,
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            workflows[variant] = outputs["package"][2]["p465-workflow.sql"].decode("utf-8")
        check(len(set(workflows.values())) == 3 and "'vcs'" in workflows["shaped"] and "'vcs'" not in workflows["default"]
              and "'audit'" in workflows["default"] and "'audit'" not in workflows["lean"],
              "the plan shapes the projected workflow: a VCS stage when a close role is named, no audit stage without auditors")
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

    def raises(label, text):
        return result(label).get("raised") == "ValueError" and text in result(label)["message"]

    names = lambda label: [s["name"] for s in value(label)]
    actions = lambda label: [f"{s['from_stage']}>{s['to_stage']}:{s['action_name']}" for s in value(label)]
    check(value("text: given") == "-- given" and value("text: the schema beside the module").startswith("TEXT ")
          and value("text: the path rebound on project_provision") != value("text: the schema beside the module"),
          "the schema text: the one given, else schema.sql read through project_provision's SCHEMA_SQL_PATH when it runs")
    check(raises("values: a missing table", "could not find ticket_board.workflow_nothing seed INSERT") and raises("values: a table name that is not a pattern", "workflow.stages")
          and raises("values: no ON CONFLICT", "could not find"),
          "a seed INSERT is found by its table name, taken literally, up to its ON CONFLICT line; anything else is refused")
    check(value("rows: two rows") == ["'a', 1", "'b' , 2"] and value("rows: parentheses and doubled quotes inside strings") == ["'a (b)', 'it''s)'", "'c'"]
          and value("rows: nested parentheses kept") == ["f(x), 2"] and value("rows: none") == []
          and all(raises(k, "unterminated SQL values block") for k in ("rows: unterminated", "rows: an unterminated quote", "rows: a stray close")),
          "rows: split at top-level parentheses, quotes (and doubled quotes) respected, nesting kept, an unbalanced block refused")
    check(value("fields: plain") == ["'a'", "1", "NULL"] and value("fields: commas in strings and arrays") == ["'a,b'", "ARRAY['x', 'y']::text[]", "'it''s, fine'"]
          and value("fields: an empty row") == [""] and value("fields: a trailing comma") == ["'a'", ""],
          "fields: split at top-level commas, outside quotes and brackets")
    check(value("string: plain") == "In progress" and value("string: a doubled quote") == "it's" and value("string: empty") == ""
          and raises("string: not a literal", "expected SQL string literal") and raises("string: half a literal", "expected SQL string literal")
          and value("nullable: NULL") is None and value("nullable: a string") == "x" and raises("nullable: neither", "expected SQL string literal")
          and value("bool: true") is True and value("bool: false") is False and raises("bool: neither", "expected SQL boolean"),
          "literals: quoted strings unquoted and unescaped, NULL in any case, true/false in any case; anything else refused")
    check(value("array: two") == ["main", "app"] and value("array: empty") == [] and value("array: a comma inside a string") == ["a,b", "it's"]
          and raises("array: not an array", "expected SQL text array") and raises("array: another type", "expected SQL text array"),
          "text arrays: ARRAY[...]::text[] only, each element a string literal")
    synthetic = value("stages: synthetic")
    check(names("stages: synthetic") == ["draft", "in_progress", "audit", "director_review", "done"]
          and synthetic[1]["display_label"] == "In (progress)" and synthetic[2]["display_label"] == "Audit, it's"
          and synthetic[2]["entry_gate_field"] == "needs_audit" and synthetic[2]["gate_skip_to"] == "director_review" and synthetic[0]["entry_gate_field"] is None
          and synthetic[4]["owner_roles"] == [] and synthetic[4]["is_terminal"] is True and synthetic[3]["is_terminal"] is False
          and len(value("stages: the real schema.sql")) == 11 and names("stages: the text reader rebound on project_provision") == names("stages: synthetic")
          and raises("stages: a short row", "has 3 fields, expected 8") and raises("stages: no insert", "could not find"),
          "stages: eight fields per row, in order; the real schema.sql's eleven; the text read through project_provision")
    check(actions("transitions: synthetic")[-1] == "in_progress>backlog:defer" and len(value("transitions: the real schema.sql")) == 59
          and value("transitions: synthetic")[1]["allowed_roles"] == ["main", "app", "ops"] and value("transitions: synthetic")[0]["director_override"] is True
          and raises("transitions: a long row", "has 7 fields, expected 6")
          and {x["class"] for x in value("stages: the seed class rebound on project_provision")} == {"P465WorkflowStageSeed"}
          and {x["class"] for x in value("transitions: the seed class rebound on project_provision")} == {"P465WorkflowTransitionSeed"},
          "transitions: six fields per row, in order; the real schema.sql's fifty-nine; each seed class read through project_provision")
    check(value("implementers: main first") == ["main", "app", "perf"] and value("implementers: without main") == ["app", "perf"]
          and value("owners: draft") == ["designer"] and value("owners: in progress") == ["main", "app", "perf"] and value("owners: audit") == ["audit", "inspector"]
          and value("owners: another stage") == ["director"] and value("owners: the full pgu seed") == ["main", "app", "ops", "perf", "research"],
          "stage owners: draft, implementation (main first) and audit from the plan; the seed's own otherwise, and always for the pgu seed")
    check(value("allowed: implementers and audit replaced once") == ["director", "app", "main", "perf", "audit", "inspector"]
          and value("allowed: no audit roles") == ["app", "main"] and value("allowed: the full pgu seed") == ["main", "audit", "main"]
          and value("allowed: the implementer default rebound on project_provision") == ["director", "app", "main", "perf"]
          and value("allowed: the dedupe rebound on project_provision") == ["rebound"]
          and value("allowed: implementers inserted once, seen with the dedupe passing everything") == ["director", "app", "main", "perf", "audit", "inspector"],
          "allowed roles: the default implementers become the plan's once, audit the plan's auditors, deduplicated through project_provision; the pgu seed untouched")
    check([s["rank"] for s in value("rank: synthetic stages, no vcs")] == [0, 1, 2, 3, 4] and [s["rank"] for s in value("rank: with a vcs stage")] == [0, 1, 2, 3, 4, 5],
          "ranks: consecutive for working stages; a terminal stage keeps its seed rank, one later when a vcs stage is present")
    check(names("project stages: default") == ["draft", "analysis", "in_progress", "audit", "dat", "user_review", "director_review", "done", "cancelled"]
          and names("project stages: shaped") == ["draft", "analysis", "in_progress", "audit", "dat", "user_review", "director_review", "vcs", "done", "cancelled"]
          and names("project stages: lean") == ["draft", "analysis", "in_progress", "director_review", "done", "cancelled"]
          and names("project stages: pgu") == names("stages: the real schema.sql")
          and value("names: default") == names("project stages: default") and value("names: lean") == names("project stages: lean")
          and "dat" not in value("names: the excluded stages rebound on project_provision")
          and [s["gate_skip_to"] for s in value("project stages: a gate into an excluded stage cleared") if s["name"] == "in_progress"] == [None]
          and [s["gate_skip_to"] for s in value("project stages: a gate into a kept stage kept") if s["name"] == "in_progress"] == ["audit"],
          "tenant stages: backlog and inspection excluded (read through project_provision), audit, dat and user review too without auditors, "
          "a VCS stage before done with a close role, a gate into an excluded stage cleared; the pgu seed whole")
    check(len(value("project transitions: pgu")) == 59 and len(value("project transitions: default")) == 37 and len(value("project transitions: lean")) == 17
          and "in_progress>director_review:submit_to_audit" in actions("project transitions: lean")
          and actions("project transitions: synthetic schema, shaped")[-2:] == ["director_review>vcs:route", "vcs>done:mark_done"]
          and "director_review>done:mark_done" not in actions("project transitions: synthetic schema, shaped")
          and not any(":defer" in a for a in actions("project transitions: default"))
          and "audit>in_progress:audit_kick_back" not in actions("project transitions: the excluded actions rebound on project_provision")
          and "audit>in_progress:audit_kick_back" in actions("project transitions: synthetic schema, shaped"),
          "tenant transitions: excluded actions dropped (read through project_provision), submit-to-audit rerouted without auditors, "
          "mark-done moved behind the VCS stage; the pgu seed whole")


def test_every_seam_is_reached() -> None:
    # Every function the moved code reads on project_provision is a recorder there; the constants are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names | {"project_workflow_stages"} and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions == {"SCHEMA_SQL_PATH", "TENANT_WORKFLOW_EXCLUDED_STAGES", "TENANT_WORKFLOW_EXCLUDED_ACTIONS", "DEFAULT_IMPLEMENTER_ROLES",
                                "WorkflowStageSeed", "WorkflowTransitionSeed"},
          f"every other seam is a rebindable name: {sorted(names - functions)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_the_same_schema",
             "test_the_direct_script_reads_the_same_schema_through_one_seed_class", "test_the_seams_read_through_project_provision_and_nothing_bound",
             "test_project_provision_reexports_them_and_its_readers_reach_them_there", "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_workflow_projection_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
