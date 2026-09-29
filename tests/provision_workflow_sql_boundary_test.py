#!/usr/bin/env python3
"""SYRD-466: the provisioning workflow SQL renderers, against project_provision they came out of.

The six -- `render_workflow_sql` (a tenant's workflow seed SQL), the role
constraint it installs (`render_project_role_constraint_sql`), the role-change
migrations `render_add_role_sql` and `render_vcs_close_role_sql`, and the two
row renderers they share -- moved unchanged into
`scripts/ticket_board/provision_workflow_sql.py`; `project_provision`
re-exports them all, in both branches of its import block, and keeps the SQL
primitives, `_validate_role`, `UNVERIFIED_DECLARED_WORKFLOW` and (re-exported
from SYRD-465's module) the projection. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first, and
  `team_launcher`'s public imports of the two role-change renderers reach the
  same ones. The module alone loads only its package.
- **Seams (rule 24):** what they read of `project_provision` -- each other, the
  projection, `sql_literal`, `sql_text_array`, `_validate_role` and
  `UNVERIFIED_DECLARED_WORKFLOW` -- is read through it when they run, with the
  direct-script fallback, so a patch there reaches them.
- **Readers:** `write_artifacts` and `main` name `render_workflow_sql` as
  before, and `team_launcher` imports the role-change renderers from
  `project_provision`, as often as before.
- **The behaviour is the baseline's:** every renderer for default, shaped,
  lean, pgu, declared and unverified plans built by `build_plan` for a
  synthetic owner, the role-change migrations and their refusals. `GOLDEN`
  below was produced by running the BASELINE module's own definitions over the
  very cases embedded here (`gold466.py`), not typed; it is byte-identical
  under `env -i`, in a normal role pane, with another HOME, USER and COLUMNS,
  under umask 077, under several hash seeds and with another TMPDIR and
  locale.
- **The direct script renders what the package renders:** every renderer, and
  the default, shaped and lean packets, byte for byte.

No real home, tenant or account is read or written: plans name a synthetic
owner, the workflow comes from the repository's own schema.sql, and the
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
from scripts.ticket_board import provision_workflow_sql as m  # noqa: E402

CHECKS = 0
MOVED = ('render_project_role_constraint_sql', 'render_workflow_sql', '_workflow_stage_rows_sql', '_workflow_transition_rows_sql', 'render_add_role_sql', 'render_vcs_close_role_sql')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'render_project_role_constraint_sql': {'project_workflow_state_names': 1, 'sql_literal': 6},
    'render_workflow_sql': {'UNVERIFIED_DECLARED_WORKFLOW': 1, 'project_workflow_stages': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 8, 'sql_text_array': 2},
    '_workflow_stage_rows_sql': {'sql_literal': 5, 'sql_text_array': 1},
    '_workflow_transition_rows_sql': {'sql_literal': 3, 'sql_text_array': 1},
    'render_add_role_sql': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1},
    'render_vcs_close_role_sql': {'_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1},
}
#: Measured on the baseline: every project_provision definition outside the six that names them, and how often.
DISPATCH = {'write_artifacts': {'render_workflow_sql': 1}, 'main': {'render_workflow_sql': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/team_launcher.py': {'import render_add_role_sql': 1, 'import render_vcs_close_role_sql': 1}}
#: The BASELINE's own behaviour for the cases below (`gold466.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'workflow sql: default': {'result': {'type': 'str', 'value': {'text': 8276, 'sha256': 'b12683e035481d0a', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 150, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 19, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 224, 'sql_text_array': 46}},
    'workflow sql: shaped': {'result': {'type': 'str', 'value': {'text': 8610, 'sha256': '45bb3f4bc7361176', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 152, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'workflow sql: lean': {'result': {'type': 'str', 'value': {'text': 6122, 'sha256': '3d3519d1611c22a7', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 127, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 0, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 113, 'sql_text_array': 23}},
    'workflow sql: pgu': {'result': {'type': 'str', 'value': '-- pgu keeps the full workflow seeded by schema.sql.\n-- No per-project workflow override is applied.\n'}, 'calls': {}},
    'workflow sql: declared': {'result': {'type': 'str', 'value': 'BEGIN;\nSET LOCAL ROLE ticket_board_service;\nSELECT set_config(\'ticket_board.project\',\'p466\',true);\nSELECT set_config(\'ticket_board.caller_role\',\'director\',true);\nSELECT ticket_board.apply_declared_workflow(\'{"note": "it\'\'s declared", "stages": ["draft", "done"]}\'::jsonb);\nCOMMIT;\n'}, 'calls': {}},
    'workflow sql: unverified': {'result': {'type': 'str', 'value': {'text': 818, 'sha256': 'e2e287e642e49be3', 'head': ['-- p466 declares its own workflow, and root holds no verified copy of it.', '--', '-- Root will not seed a workflow it cannot vouch for, and it will not seed the'], 'lines': 14, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 0, "'audit'": 0, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 1, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {}},
    'workflow sql: two close roles': {'result': {'type': 'str', 'value': {'text': 8618, 'sha256': '5e3784cd35b33905', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 152, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 253, 'sql_text_array': 48}},
    'workflow sql: a stranger closing': {'result': {'type': 'str', 'value': {'text': 8620, 'sha256': 'cf82afaac01635ea', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 152, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'workflow sql: a given schema': {'result': {'type': 'str', 'value': {'text': 8610, 'sha256': '45bb3f4bc7361176', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 152, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'workflow sql: a synthetic schema': {'result': {'type': 'str', 'value': {'text': 5478, 'sha256': 'cc4706affb5d3ace', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 116, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 12, "'inspector'": 6, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 101, 'sql_text_array': 12}},
    'add role main: a synthetic schema': {'result': {'type': 'str', 'value': {'text': 4743, 'sha256': '4f71be3d256c885b', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 131, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 12, "'inspector'": 6, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 101, 'sql_text_array': 12}},
    'vcs close: a synthetic schema': {'result': {'type': 'str', 'value': {'text': 4902, 'sha256': 'b1bdcfe24a75f994', 'head': ['-- Configure VCS close role ops for the existing project workflow for p466.', '-- Run after the launcher plan has been updated with operation_allowed_roles mark_done=ops.', 'BEGIN;'], 'lines': 137, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 12, "'inspector'": 6, "'research'": 0, "'mark_done'": 2, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 101, 'sql_text_array': 12}},
    'constraint: default': {'result': {'type': 'str', 'value': {'text': 1919, 'sha256': '8d86a4fa06104a13', 'head': ['', 'ALTER TABLE ticket_board.workflow_stages', '    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;'], 'lines': 40, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 5, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 1, 'project_workflow_state_names': 1, 'sql_literal': 37}},
    'constraint: shaped': {'result': {'type': 'str', 'value': {'text': 2025, 'sha256': 'bab770aa8638db73', 'head': ['', 'ALTER TABLE ticket_board.workflow_stages', '    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;'], 'lines': 40, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 2, "'audit'": 5, "'inspector'": 3, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 1, 'project_workflow_state_names': 1, 'sql_literal': 49}},
    'constraint: lean': {'result': {'type': 'str', 'value': {'text': 1794, 'sha256': 'b85044cb6f0eae88', 'head': ['', 'ALTER TABLE ticket_board.workflow_stages', '    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;'], 'lines': 40, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 0, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 1, 'project_workflow_state_names': 1, 'sql_literal': 25}},
    'constraint: pgu': {'result': {'type': 'str', 'value': {'text': 2089, 'sha256': 'a9d22744e069371a', 'head': ['', 'ALTER TABLE ticket_board.workflow_stages', '    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;'], 'lines': 40, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 5, "'inspector'": 3, "'research'": 4, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 1, 'project_workflow_state_names': 1, 'sql_literal': 54}},
    'stage rows: default': {'result': {'type': 'str', 'value': {'text': 817, 'sha256': 'eda49290088df790', 'head': ["    ('draft', 'Draft', 0, ARRAY['designer']::text[], NULL, NULL, NULL, false),", "    ('analysis', 'Triage', 1, ARRAY['director']::text[], NULL, NULL, NULL, false),", "    ('in_progress', 'Implementation', 2, ARRAY['main', 'app']::text[], NULL, NULL, NULL, false),"], 'lines': 9, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 0, "'audit'": 2, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'sql_literal': 34, 'sql_text_array': 9}},
    'stage rows: shaped': {'result': {'type': 'str', 'value': {'text': 909, 'sha256': 'e034d2ab1da1bea7', 'head': ["    ('draft', 'Draft', 0, ARRAY['designer']::text[], NULL, NULL, NULL, false),", "    ('analysis', 'Triage', 1, ARRAY['director']::text[], NULL, NULL, NULL, false),", "    ('in_progress', 'Implementation', 2, ARRAY['main', 'app', 'perf']::text[], NULL, NULL, NULL, false),"], 'lines': 10, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 1, "'audit'": 2, "'inspector'": 1, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'sql_literal': 39, 'sql_text_array': 10}},
    'stage rows: none': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'transition rows: default': {'result': {'type': 'str', 'value': {'text': 3175, 'sha256': '95f1dee5b0158f6e', 'head': ["    ('draft', 'analysis', 'release_draft', ARRAY['designer', 'director', 'user']::text[], false, false),", "    ('draft', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),", "    ('analysis', 'in_progress', 'route', ARRAY['director']::text[], false, false),"], 'lines': 37, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 0, "'audit'": 12, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'sql_literal': 153, 'sql_text_array': 37}},
    'transition rows: shaped': {'result': {'type': 'str', 'value': {'text': 3311, 'sha256': '1ca1e297b085aafe', 'head': ["    ('draft', 'analysis', 'release_draft', ARRAY['designer', 'director', 'user']::text[], false, false),", "    ('draft', 'cancelled', 'cancel', ARRAY['director']::text[], false, false),", "    ('analysis', 'in_progress', 'route', ARRAY['director']::text[], false, false),"], 'lines': 38, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 2, "'audit'": 12, "'inspector'": 3, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'sql_literal': 164, 'sql_text_array': 38}},
    'transition rows: none': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'add role main: default': {'result': {'type': 'str', 'value': {'text': 7541, 'sha256': '187727628f3483fe', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 165, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 19, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 224, 'sql_text_array': 46}},
    'add role main: shaped': {'result': {'type': 'str', 'value': {'text': 7875, 'sha256': '6a26ca18e9b0b769', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 167, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'add role main: lean': {'result': {'type': 'str', 'value': {'text': 5387, 'sha256': '43907e81a68e9260', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 142, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 0, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 113, 'sql_text_array': 23}},
    'add role main: declared': {'result': {'type': 'str', 'value': {'text': 7541, 'sha256': '187727628f3483fe', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 165, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 19, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 224, 'sql_text_array': 46}},
    'add role main: unverified': {'result': {'type': 'str', 'value': {'text': 7541, 'sha256': '187727628f3483fe', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 165, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 19, "'inspector'": 0, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 224, 'sql_text_array': 46}},
    'add role inspector: shaped': {'result': {'type': 'str', 'value': {'text': 7876, 'sha256': 'eafc9dd69c6f8752', 'head': ['-- Add auditor role inspector to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 167, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'add role  Main  normalized: shaped': {'result': {'type': 'str', 'value': {'text': 7875, 'sha256': '6a26ca18e9b0b769', 'head': ['-- Add implementer role main to the existing project workflow for p466.', '-- Run after the launcher config has been updated with the expanded role set.', 'BEGIN;'], 'lines': 167, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 1, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_validate_role': 1, '_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'add role: a role the plan lacks': {'result': {'raised': 'SystemExit', 'message': 'incremental add-role SQL requires the role in plan.implementer_roles or plan.audit_roles'}, 'calls': {'_validate_role': 1}},
    'add role: an invalid name': {'result': {'raised': 'SystemExit', 'message': 'role names must match ^[a-z][a-z0-9_-]{0,63}$'}, 'calls': {'_validate_role': 1}},
    'add role: the pgu seed': {'result': {'raised': 'SystemExit', 'message': 'incremental add-role SQL is only for provisioned project workflows'}, 'calls': {'_validate_role': 1}},
    'vcs close: shaped': {'result': {'type': 'str', 'value': {'text': 8034, 'sha256': '3ffb2d4e4de4b58d', 'head': ['-- Configure VCS close role ops for the existing project workflow for p466.', '-- Run after the launcher plan has been updated with operation_allowed_roles mark_done=ops.', 'BEGIN;'], 'lines': 173, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 5, "'audit'": 19, "'inspector'": 7, "'research'": 0, "'mark_done'": 2, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 252, 'sql_text_array': 48}},
    'vcs close: no close role': {'result': {'raised': 'SystemExit', 'message': 'incremental VCS close-role SQL requires exactly one configured mark_done role'}, 'calls': {}},
    'vcs close: two close roles': {'result': {'raised': 'SystemExit', 'message': 'incremental VCS close-role SQL requires exactly one configured mark_done role'}, 'calls': {}},
    'vcs close: a close role that is not a project role': {'result': {'raised': 'SystemExit', 'message': '--vcs-close-role must be one of the configured project roles'}, 'calls': {}},
    'vcs close: the pgu seed': {'result': {'raised': 'SystemExit', 'message': 'incremental VCS close-role SQL is only for provisioned project workflows'}, 'calls': {}},
    'workflow sql: the unverified marker rebound on project_provision': {'result': {'type': 'str', 'value': {'text': 818, 'sha256': 'e2e287e642e49be3', 'head': ['-- p466 declares its own workflow, and root holds no verified copy of it.', '--', '-- Root will not seed a workflow it cannot vouch for, and it will not seed the'], 'lines': 14, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 0, "'audit'": 0, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 1, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {}},
    'workflow sql: the literal quoting rebound on project_provision': {'result': {'type': 'str', 'value': {'text': 8037, 'sha256': '8b37b9e1811df42e', 'head': ['-- Seed the default project workflow for p466.', '-- Run after schema.sql/migrations and before rbac.sql on a newly provisioned board.', '--'], 'lines': 150, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 0, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 224, 'P466ARR': 0}}, 'calls': {'project_workflow_stages': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal rebound': 224, 'sql_text_array': 46}},
    'stage rows: the array quoting rebound on project_provision': {'result': {'type': 'str', 'value': {'text': 679, 'sha256': '980730a1e31037c5', 'head': ["    ('draft', 'Draft', 0, P466ARR, NULL, NULL, NULL, false),", "    ('analysis', 'Triage', 1, P466ARR, NULL, NULL, NULL, false),", "    ('in_progress', 'Implementation', 2, P466ARR, NULL, NULL, NULL, false),"], 'lines': 9, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 0, "'vcs'": 0, "'audit'": 1, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 9}}, 'calls': {'sql_literal': 26, 'sql_text_array rebound': 9}},
    'constraint: the state names rebound on project_provision': {'result': {'type': 'str', 'value': {'text': 1751, 'sha256': '1420420236521f44', 'head': ['', 'ALTER TABLE ticket_board.workflow_stages', '    DROP CONSTRAINT IF EXISTS workflow_stages_name_check;'], 'lines': 40, 'BEGIN;': 0, 'COMMIT;': 0, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 0, 'INSERT INTO ticket_board.workflow_transitions': 0, 'DELETE FROM ticket_board.workflow_transitions': 0, 'workflow_stages_name_check': 2, "'vcs'": 0, "'audit'": 3, "'inspector'": 0, "'research'": 0, "'mark_done'": 0, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'project_workflow_state_names rebound': 1, 'sql_literal': 23}},
    'add role: the role check rebound on project_provision': {'result': {'raised': 'SystemExit', 'message': 'incremental add-role SQL requires the role in plan.implementer_roles or plan.audit_roles'}, 'calls': {'_validate_role rebound': 1}},
    'vcs close: the projection rebound on project_provision': {'result': {'type': 'str', 'value': {'text': 4552, 'sha256': '4d7068861be56e95', 'head': ['-- Configure VCS close role ops for the existing project workflow for p466.', '-- Run after the launcher plan has been updated with operation_allowed_roles mark_done=ops.', 'BEGIN;'], 'lines': 135, 'BEGIN;': 1, 'COMMIT;': 1, 'apply_declared_workflow': 0, 'INSERT INTO ticket_board.workflow_stages': 1, 'INSERT INTO ticket_board.workflow_transitions': 1, 'DELETE FROM ticket_board.workflow_transitions': 1, 'workflow_stages_name_check': 2, "'vcs'": 2, "'audit'": 3, "'inspector'": 3, "'research'": 0, "'mark_done'": 2, 'declares its own workflow': 0, 'P466LIT': 0, 'P466ARR': 0}}, 'calls': {'_workflow_stage_rows_sql': 1, '_workflow_transition_rows_sql': 1, 'project_workflow_stages rebound': 3, 'project_workflow_state_names': 1, 'project_workflow_transitions': 1, 'render_project_role_constraint_sql': 1, 'sql_literal': 80, 'sql_text_array': 10}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = 10

# --- the cases, shared verbatim with `gold466.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the six renderers and records the answer or the exact exception, and how many times it called each
# of `project_provision`'s helpers (recorded there and passed through). Plans are built by `build_plan` for a synthetic
# owner under /p466; the stages and transitions come from the repository's own schema.sql. A rendering is recorded by its
# length, digest, first lines and how often it carries each of FEATURES -- nothing is run. A case may rebind a
# `project_provision` name to show it is read there when the renderer runs.
FEATURES = ("BEGIN;", "COMMIT;", "apply_declared_workflow", "INSERT INTO ticket_board.workflow_stages", "INSERT INTO ticket_board.workflow_transitions",
            "DELETE FROM ticket_board.workflow_transitions", "workflow_stages_name_check", "'vcs'", "'audit'", "'inspector'", "'research'",
            "'mark_done'", "declares its own workflow", "P466LIT", "P466ARR")
# A small synthetic schema.sql, the same text SYRD-465's cases use, for a renderer given its own schema text.
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
PLANS = {
    "default": {},
    "shaped": {"implementer_roles": ["app", "main", "perf"], "audit_roles": ["audit", "inspector"], "vcs_close_role": "ops"},
    "lean": {"include_designer": False, "include_audit": False},
    "pgu": {"project": "pgu"},
    "declared": {"declared": {"stages": ["draft", "done"], "note": "it's declared"}},
    "unverified": {"unverified": True},
    "two close roles": {"close": ("ops", "main")},
    "a stranger closing": {"close": ("stranger",)},
}
CASES = {
    **{f"workflow sql: {p}": {"call": "render_workflow_sql", "plan": p} for p in PLANS},
    "workflow sql: a given schema": {"call": "render_workflow_sql", "plan": "shaped", "schema": True},
    "workflow sql: a synthetic schema": {"call": "render_workflow_sql", "plan": "shaped", "schema": "synthetic"},
    "add role main: a synthetic schema": {"call": "render_add_role_sql", "plan": "shaped", "role": "main", "schema": "synthetic"},
    "vcs close: a synthetic schema": {"call": "render_vcs_close_role_sql", "plan": "shaped", "schema": "synthetic"},
    **{f"constraint: {p}": {"call": "render_project_role_constraint_sql", "plan": p} for p in ("default", "shaped", "lean", "pgu")},
    **{f"stage rows: {p}": {"call": "_workflow_stage_rows_sql", "rows": "stages", "plan": p} for p in ("default", "shaped")},
    "stage rows: none": {"call": "_workflow_stage_rows_sql", "rows": "empty"},
    **{f"transition rows: {p}": {"call": "_workflow_transition_rows_sql", "rows": "transitions", "plan": p} for p in ("default", "shaped")},
    "transition rows: none": {"call": "_workflow_transition_rows_sql", "rows": "empty"},
    **{f"add role main: {p}": {"call": "render_add_role_sql", "plan": p, "role": "main"} for p in ("default", "shaped", "lean", "declared", "unverified")},
    "add role inspector: shaped": {"call": "render_add_role_sql", "plan": "shaped", "role": "inspector"},
    "add role  Main  normalized: shaped": {"call": "render_add_role_sql", "plan": "shaped", "role": " Main "},
    "add role: a role the plan lacks": {"call": "render_add_role_sql", "plan": "default", "role": "research"},
    "add role: an invalid name": {"call": "render_add_role_sql", "plan": "default", "role": "Bad Role"},
    "add role: the pgu seed": {"call": "render_add_role_sql", "plan": "pgu", "role": "main"},
    "vcs close: shaped": {"call": "render_vcs_close_role_sql", "plan": "shaped"},
    "vcs close: no close role": {"call": "render_vcs_close_role_sql", "plan": "default"},
    "vcs close: two close roles": {"call": "render_vcs_close_role_sql", "plan": "two close roles"},
    "vcs close: a close role that is not a project role": {"call": "render_vcs_close_role_sql", "plan": "a stranger closing"},
    "vcs close: the pgu seed": {"call": "render_vcs_close_role_sql", "plan": "pgu"},
    "workflow sql: the unverified marker rebound on project_provision": {"call": "render_workflow_sql", "plan": "unverified-custom", "rebind": {"UNVERIFIED_DECLARED_WORKFLOW": "p466-custom"}},
    "workflow sql: the literal quoting rebound on project_provision": {"call": "render_workflow_sql", "plan": "default", "rebind": {"sql_literal": True}},
    "stage rows: the array quoting rebound on project_provision": {"call": "_workflow_stage_rows_sql", "rows": "stages", "plan": "default", "rebind": {"sql_text_array": True}},
    "constraint: the state names rebound on project_provision": {"call": "render_project_role_constraint_sql", "plan": "default", "rebind": {"project_workflow_state_names": True}},
    "add role: the role check rebound on project_provision": {"call": "render_add_role_sql", "plan": "default", "role": "main", "rebind": {"_validate_role": True}},
    "vcs close: the projection rebound on project_provision": {"call": "render_vcs_close_role_sql", "plan": "shaped", "rebind": {"project_workflow_stages": True}},
}
FUNCTIONS = ("render_project_role_constraint_sql", "render_workflow_sql", "_workflow_stage_rows_sql", "_workflow_transition_rows_sql", "render_add_role_sql",
             "render_vcs_close_role_sql")
PASSED = ("sql_literal", "sql_text_array", "_validate_role", "project_workflow_stages", "project_workflow_transitions", "project_workflow_state_names",
          "render_project_role_constraint_sql", "_workflow_stage_rows_sql", "_workflow_transition_rows_sql")
REBINDABLE = ("UNVERIFIED_DECLARED_WORKFLOW",)


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import dataclasses, hashlib
    from pathlib import Path as _P
    counts: dict = {}

    def norm(value):
        if isinstance(value, str) and len(value) > 300:
            lines = value.split("\n")
            return {"text": len(value), "sha256": hashlib.sha256(value.encode()).hexdigest()[:16], "head": lines[:3], "lines": len(lines),
                    **{f: value.count(f) for f in FEATURES}}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, (str, bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "build_plan", "_schema_sql_text")}

    def plan_for(name):
        base = {"project": "p466", "owner_user": "p466-agent", "owner_home": _P("/p466/home"), "port": 34466}
        if name in ("two close roles", "a stranger closing"):
            plan = saved["build_plan"](**{**base, **PLANS["shaped"]})
            ops = tuple((op, PLANS[name]["close"] if op == "mark_done" else roles) for op, roles in plan.operation_allowed_roles)
            return dataclasses.replace(plan, operation_allowed_roles=ops)
        if name in ("declared", "unverified", "unverified-custom"):
            plan = saved["build_plan"](**base)
            if name == "declared":
                return dataclasses.replace(plan, workflow=PLANS["declared"]["declared"])
            return dataclasses.replace(plan, workflow=None, workflow_seed=saved["UNVERIFIED_DECLARED_WORKFLOW"] if name == "unverified" else "p466-custom")
        return saved["build_plan"](**{**base, **PLANS[name]})

    try:
        args, kwargs = [], {}
        if "plan" in spec:
            plan = plan_for(spec["plan"])
            args = [plan]
            if spec.get("schema"):
                kwargs["schema_sql"] = SCHEMA if spec["schema"] == "synthetic" else saved["_schema_sql_text"]()
        if "rows" in spec:
            if spec["rows"] == "empty":
                args = [()]
            elif spec["rows"] == "stages":
                args = [t.project_workflow_stages(plan)]
            else:
                args = [t.project_workflow_transitions(plan)]
        if "role" in spec:
            args = [plan, spec["role"]]
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "sql_literal":
                t.sql_literal = lambda value: note("sql_literal rebound") or "'P466LIT'"
            elif name == "sql_text_array":
                t.sql_text_array = lambda values: note("sql_text_array rebound") or "P466ARR"
            elif name == "project_workflow_state_names":
                t.project_workflow_state_names = lambda plan: note("project_workflow_state_names rebound") or ("draft", "p466_state")
            elif name == "_validate_role":
                t._validate_role = lambda role: note("_validate_role rebound") or "research"
            elif name == "project_workflow_stages":
                t.project_workflow_stages = lambda plan, *, schema_sql=None: note("project_workflow_stages rebound") or saved["project_workflow_stages"](plan, schema_sql=schema_sql)[:3]
            else:
                setattr(t, name, value)
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        try:
            got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        else:
            result = {"type": type(got).__name__, "value": norm(got)}
        return {"result": result, "calls": dict(sorted(counts.items()))}
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


#: What the renderers read on project_provision when they run (its own, or re-exported there by an earlier slice), and its callers of them.
KEPT = ("sql_literal", "sql_text_array", "_validate_role", "UNVERIFIED_DECLARED_WORKFLOW", "project_workflow_stages", "project_workflow_transitions",
        "project_workflow_state_names", "write_artifacts", "main")
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them: None for every schema text, nothing else.
DEFAULTS = {"render_project_role_constraint_sql": [], "render_workflow_sql": ["None"], "_workflow_stage_rows_sql": [], "_workflow_transition_rows_sql": [],
            "render_add_role_sql": ["None"], "render_vcs_close_role_sql": ["None"]}
#: The packets rendered through the direct script and the package: the default plan, and two whose workflow the plan shapes.
PACKET_VARIANTS = {
    "default": [],
    "shaped": ["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"],
    "lean": ["--no-include-designer", "--no-include-audit"],
}
#: One probe, run in the package and as the direct script: every renderer for a default and a shaped plan, as a digest.
RENDER_PROBE = (
    "import hashlib, json, pathlib\n"
    "plan = lambda **k: g['build_plan'](project='p466', owner_user='p466-agent', owner_home=pathlib.Path('/p466/home'), port=34466, **k)\n"
    "plans = [plan(), plan(implementer_roles=['app', 'main', 'perf'], audit_roles=['audit', 'inspector'], vcs_close_role='ops')]\n"
    "out = []\n"
    "for p in plans:\n"
    "    out += [g['render_workflow_sql'](p), g['render_project_role_constraint_sql'](p), g['render_add_role_sql'](p, 'main'),\n"
    "            g['_workflow_stage_rows_sql'](g['project_workflow_stages'](p)), g['_workflow_transition_rows_sql'](g['project_workflow_transitions'](p))]\n"
    "out.append(g['render_vcs_close_role_sql'](plans[1]))\n"
    "print(len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest())\n"
)


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
    result = python("import sys, scripts.ticket_board.provision_workflow_sql as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_team_launcher_reaches_them() -> None:
    for order in (("scripts.ticket_board.provision_workflow_sql", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_workflow_sql"),
                  ("scripts.team_launcher", "scripts.ticket_board.provision_workflow_sql")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_workflow_sql as m, scripts.team_launcher as tl; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {MOVED!r}}}), "
                        "tl.render_add_role_sql is m.render_add_role_sql and tl.render_vcs_close_role_sql is m.render_vcs_close_role_sql, "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_workflow_sql'] True True",
              f"{' then '.join(order)}: one object each, defined here; team_launcher's public imports reach the same ones; "
              f"nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    import json as _json
    import typing
    check(m.json is _json and m.Sequence is typing.Sequence and t.Sequence is m.Sequence,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_direct_script_renders_what_the_package_renders() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the renderers'
    # fallback loads project_provision a second time beside it. Every renderer must give the package's bytes.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd466_script__'); "
                       "m = sys.modules['provision_workflow_sql']; "
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + RENDER_PROBE)
    as_package = python("import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + RENDER_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("11 "),
          f"as a direct script: the script holds the module's own renderers, and all eleven renderings are the package's, byte for byte: "
          f"{as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_workflow_sql.py").read_text(encoding="utf-8"))
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
        check(len(imports) == 2 and ast.unparse(node.body[first]) == CALL_TIME_IMPORT,
              f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback, and nothing else imported")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT) and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import json", "from typing import Sequence"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.Assign, ast.AnnAssign, ast.ClassDef))], f"the standard library only, at load, and nothing but functions: {top}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: None for every schema text, nothing else: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the six, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_workflow_sql" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_workflow_sql" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the six, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # A name stays reachable on project_provision: defined there, or re-exported there, unaliased, by an earlier slice.
    exported = {a.name for n in ast.walk(guard) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1
                and n.module != "provision_workflow_sql" for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and set(KEPT) <= defined | exported, "project_provision defines none of them, and keeps what they read and their callers, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
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
    base = Path(tempfile.mkdtemp(prefix="syrd466-packet-")).resolve()
    try:
        workflows = {}
        for variant, extra in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p466", "--owner-user", "p466-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34466", "--output-dir", f"{work}/out", *extra]
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                if mode == "script":
                    probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd466_script'); raise SystemExit(g['main']({argv!r}))")
                else:
                    probe = f"import scripts.ticket_board.project_provision as pp; raise SystemExit(pp.main({argv!r}))"
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES,
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            workflows[variant] = outputs["package"][2]["p466-workflow.sql"].decode("utf-8")
        check(len(set(workflows.values())) == 3 and "'vcs'" in workflows["shaped"] and "'vcs'" not in workflows["default"]
              and "'audit'" in workflows["default"] and "'audit'" not in workflows["lean"],
              "the plan shapes the rendered workflow: a VCS stage when a close role is named, no audit stage without auditors")
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

    def refused(label, text):
        return result(label).get("raised") == "SystemExit" and text in result(label)["message"]

    head = lambda label: value(label)["head"][0]
    check(head("workflow sql: default") == "-- Seed the default project workflow for p466." and value("workflow sql: default")["INSERT INTO ticket_board.workflow_stages"] == 1
          and calls("workflow sql: default")["render_project_role_constraint_sql"] == 1 and calls("workflow sql: default")["project_workflow_transitions"] == 1,
          "a tenant's workflow SQL: its projected stages and transitions seeded, with its role constraint")
    check(value("workflow sql: shaped")["'vcs'"] > 0 and value("workflow sql: default")["'vcs'"] == 0 and value("workflow sql: lean")["'audit'"] == 0
          and value("workflow sql: default")["'audit'"] > 0 and value("workflow sql: a given schema") == value("workflow sql: shaped")
          and all(value(f"{k}: a synthetic schema")["lines"] < value(k2)["lines"] for k, k2 in (("workflow sql", "workflow sql: shaped"), ("add role main", "add role main: shaped"), ("vcs close", "vcs close: shaped"))),
          "the plan shapes it: a VCS stage with a close role, no audit without auditors; a given schema text used by every renderer")
    check(value("workflow sql: pgu").startswith("-- pgu keeps the full workflow seeded by schema.sql.") and calls("workflow sql: pgu") == {}
          and "apply_declared_workflow" in value("workflow sql: declared") and "it''s declared" in value("workflow sql: declared") and calls("workflow sql: declared") == {}
          and head("workflow sql: unverified") == "-- p466 declares its own workflow, and root holds no verified copy of it." and calls("workflow sql: unverified") == {},
          "pgu seeds nothing more; a declared workflow is applied as its quoted JSON document; an unverified declaration seeds nothing -- none of them projected")
    check(head("workflow sql: the unverified marker rebound on project_provision") == head("workflow sql: unverified")
          and value("workflow sql: the literal quoting rebound on project_provision")["P466LIT"] == calls("workflow sql: the literal quoting rebound on project_provision")["sql_literal rebound"] > 0
          and value("stage rows: the array quoting rebound on project_provision")["P466ARR"] == 9,
          "the unverified marker, the literal quoting and the array quoting are read through project_provision when they run")
    check(all(value(f"constraint: {p}")["lines"] == 40 for p in ("default", "shaped", "lean", "pgu")) and value("constraint: shaped")["'vcs'"] > 0
          and value("constraint: lean")["'audit'"] == 0 and value("constraint: pgu")["'research'"] > 0
          and value("constraint: the state names rebound on project_provision") != value("constraint: default"),
          "the role constraint: from the plan's projected state names (read through project_provision), the same shape for every plan")
    check(head("stage rows: default").startswith("    ('draft', 'Draft', 0, ARRAY['designer']::text[], NULL, NULL, NULL, false)")
          and value("stage rows: default")["lines"] == 9 and value("stage rows: shaped")["lines"] == 10 and value("stage rows: none") == ""
          and value("transition rows: default")["lines"] == 37 and value("transition rows: shaped")["lines"] == 38 and value("transition rows: none") == "",
          "row renderers: one line per stage or transition, nothing for none")
    check(head("add role main: default") == "-- Add implementer role main to the existing project workflow for p466."
          and head("add role inspector: shaped") == "-- Add auditor role inspector to the existing project workflow for p466."
          and value("add role  Main  normalized: shaped") == value("add role main: shaped")
          and value("add role main: declared") == value("add role main: unverified") == value("add role main: default")
          and refused("add role: a role the plan lacks", "requires the role in plan.implementer_roles or plan.audit_roles")
          and refused("add role: an invalid name", "role names must match") and refused("add role: the pgu seed", "only for provisioned project workflows")
          and refused("add role: the role check rebound on project_provision", "requires the role in plan.implementer_roles"),
          "adding a role: an implementer or an auditor already in the plan, its name checked and normalized through project_provision; pgu refused")
    check(head("vcs close: shaped") == "-- Configure VCS close role ops for the existing project workflow for p466." and value("vcs close: shaped")["'vcs'"] > 0
          and refused("vcs close: no close role", "requires exactly one configured mark_done role") and refused("vcs close: the pgu seed", "only for provisioned project workflows")
          and refused("vcs close: two close roles", "requires exactly one configured mark_done role")
          and refused("vcs close: a close role that is not a project role", "must be one of the configured project roles")
          and value("vcs close: the projection rebound on project_provision")["lines"] < value("vcs close: shaped")["lines"],
          "the VCS close migration: only with exactly one close role that is a project role, never for pgu; the projection read through project_provision")


def test_every_seam_is_reached() -> None:
    # Every function the six read on project_provision is a recorder there; the unverified marker is rebound by its own case.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions == {"UNVERIFIED_DECLARED_WORKFLOW"}, f"every other seam is the rebindable marker: {sorted(names - functions)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_team_launcher_reaches_them",
             "test_the_direct_script_renders_what_the_package_renders", "test_the_seams_read_through_project_provision_and_nothing_bound",
             "test_project_provision_reexports_them_and_its_readers_reach_them_there", "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_workflow_sql_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
