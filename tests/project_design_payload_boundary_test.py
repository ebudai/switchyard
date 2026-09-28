#!/usr/bin/env python3
"""SYRD-443: the project design artifact payload, against the launcher it came out of.

`ProjectDesignArtifact`, `_role_cli_map`, `project_design_artifact_payload` and
`_project_design_markdown` -- the design record, the payload it is written as,
its role/CLI map and the starting design document -- moved unchanged into
`scripts/project_design_payload.py`; the launcher re-exports all four. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; the record
  is the same frozen dataclass -- its fields, their order, defaults and flags,
  and hints that resolve -- and only its `__module__` names its new home.
- **Seams (rule 24):** what the payload reads when it runs -- the schema and the
  role/CLI map -- is read through the launcher as often as before, so a patch
  there reaches it: the cases record the map there and rebind both there.
- **Readers:** `new_project_support.py`, `project_design_artifact.py` and
  `project_design_command.py` read the four through the launcher when they run,
  as often as before; `project_design_artifact.py` names the record's type
  there for annotations only.
- **The behaviour is the baseline's:** the exact payload JSON text (key order
  included), optional models, efforts and catalog version, pass-through gates
  and grants, missing and invalid fields; the record's fields, defaults,
  frozenness, equality and refusals; the role/CLI map; the design document.
  `GOLDEN` below was produced by running the BASELINE launcher's own
  definitions over the very cases embedded here (`gold443.py`), not typed; it
  is byte-identical under `env -i`, in a normal role pane, with another HOME,
  USER and COLUMNS, under umask 077 and under several hash seeds.

Nothing is written: every path is a fixed, non-existent one and no project
artifact or document is created. Spawns, every exec, signals, account and
group lookups and socket connections are refused for each case.
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
from scripts import project_design_payload as m  # noqa: E402

CHECKS = 0
MOVED = ('ProjectDesignArtifact', '_role_cli_map', 'project_design_artifact_payload', '_project_design_markdown')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'project_design_artifact_payload': {'PROJECT_DESIGN_ARTIFACT_SCHEMA': 1, '_role_cli_map': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the four that names them, and how often (none).
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/new_project_support.py': {'launcher.ProjectDesignArtifact': 1, 'launcher._project_design_markdown': 1, 'launcher.project_design_artifact_payload': 1}, 'scripts/project_design_artifact.py': {'launcher.ProjectDesignArtifact': 1}, 'scripts/project_design_command.py': {'launcher.ProjectDesignArtifact': 1, 'launcher._project_design_markdown': 1, 'launcher.project_design_artifact_payload': 1}}
#: Measured on the baseline: modules naming one of them for annotations only, under TYPE_CHECKING.
TYPING = {'scripts/project_design_artifact.py': ['ProjectDesignArtifact']}
#: Measured on the baseline launcher: the record's fields, in order.
RECORD_FIELDS = ('project', 'project_name', 'ticket_prefix', 'owner_user', 'repository', 'remote', 'default_branch', 'worktree_policy', 'design_document', 'implementer_roles', 'audit_roles', 'role_clis', 'include_designer', 'include_audit', 'push_policy', 'gates', 'capability_grants', 'role_models', 'role_efforts', 'catalog_version')
#: The BASELINE's own behaviour for the cases below (`gold443.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'payload: a minimal record': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: models, efforts and a catalog version': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'role_models': {'main': 'gpt-x', 'director': 'opus'}, 'role_efforts': {'main': 'high'}, 'catalog_version': 3, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'role_models': {'dict': {'main': 'str', 'director': 'str'}}, 'role_efforts': {'dict': {'main': 'str'}}, 'catalog_version': 'int', 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "role_models": {"main": "gpt-x", "director": "opus"}, "role_efforts": {"main": "high"}, "catalog_version": 3, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: models only': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'role_models': {'main': 'gpt-x'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'role_models': {'dict': {'main': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "role_models": {"main": "gpt-x"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: a negative catalog version': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'catalog_version': -1, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'catalog_version': 'int', 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "catalog_version": -1, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: no roles and no CLIs': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': [], 'audit_roles': [], 'include_designer': False, 'include_audit': False, 'role_clis': {}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': []}, 'audit_roles': {'list': []}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": [], "audit_roles": [], "include_designer": false, "include_audit": false, "role_clis": {}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[]], {}]]},
    'payload: a designer without audit': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': [], 'include_designer': True, 'include_audit': False, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': []}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": [], "include_designer": true, "include_audit": false, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: a role named twice in the CLI pairs': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'main': 'claude'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'main': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"main": "claude"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['main', 'codex'], ['main', 'claude']]], {}]]},
    'payload: relative paths': {'result': {'schema': 'switchyard.project.v1', 'design_document': 'docs/design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': 'repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "docs/design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: empty gates and grants': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'push_policy': 'director-merges', 'gates': {}, 'capability_grants': {}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {}}, 'capability_grants': {'dict': {}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "push_policy": "director-merges", "gates": {}, "capability_grants": {}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: grants that are not JSON': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'set': {'set': ['a']}}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'set': 'frozenset'}}}}}}, 'json': 'not JSON: Object of type frozenset is not JSON serializable', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: the schema rebound on the launcher': {'result': {'schema': 'syrd443.v9', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'director': 'claude', 'main': 'codex', 'app': 'codex', 'audit': 'claude'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'director': 'str', 'main': 'str', 'app': 'str', 'audit': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "syrd443.v9", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"director": "claude", "main": "codex", "app": "codex", "audit": "claude"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: the CLI map rebound on the launcher': {'result': {'schema': 'switchyard.project.v1', 'design_document': '/nonexistent/syrd443/p443-design.md', 'project': {'slug': 'p443', 'name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': '/nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'roles': ['main', 'app'], 'audit_roles': ['audit'], 'include_designer': True, 'include_audit': True, 'role_clis': {'syrd443': 'rebound'}, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}}}, 'shape': {'dict': {'schema': 'str', 'design_document': 'str', 'project': {'dict': {'slug': 'str', 'name': 'str', 'ticket_prefix': 'str', 'owner_user': 'str', 'repository': 'str', 'remote': 'str', 'default_branch': 'str', 'worktree_policy': 'str', 'roles': {'list': ['str', 'str']}, 'audit_roles': {'list': ['str']}, 'include_designer': 'bool', 'include_audit': 'bool', 'role_clis': {'dict': {'syrd443': 'str'}}, 'push_policy': 'str', 'gates': {'dict': {'audit': 'bool', 'inspection': 'bool'}}, 'capability_grants': {'dict': {'agy': 'str', 'network': 'bool'}}}}}}, 'json': '{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443", "name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner", "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role", "roles": ["main", "app"], "audit_roles": ["audit"], "include_designer": true, "include_audit": true, "role_clis": {"syrd443": "rebound"}, "push_policy": "director-merges", "gates": {"audit": true, "inspection": false}, "capability_grants": {"agy": "host_default", "network": false}}}', "gates is the record's": True, "grants are the record's": True, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'payload: a record missing a field': {'result': {'raised': 'AttributeError', 'message': "'types.SimpleNamespace' object has no attribute 'remote'"}, 'calls': []},
    'payload: CLI pairs that are not pairs': {'result': {'raised': 'ValueError', 'message': 'not enough values to unpack (expected 2, got 1)'}, 'calls': [['_role_cli_map', [[['main']]], {}]]},
    'payload: models that are not pairs': {'result': {'raised': 'ValueError', 'message': 'dictionary update sequence element #0 has length 4; 2 is required'}, 'calls': [['_role_cli_map', [[['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']]], {}]]},
    'record: every field': {'result': {'dataclass': 'ProjectDesignArtifact', 'project': 'p443', 'project_name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': 'PATH /nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'design_document': 'PATH /nonexistent/syrd443/p443-design.md', 'implementer_roles': ['main', 'app'], 'audit_roles': ['audit'], 'role_clis': [['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']], 'include_designer': True, 'include_audit': True, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}, 'role_models': [['main', 'm']], 'role_efforts': [['main', 'e']], 'catalog_version': 2}, 'shape': 'ProjectDesignArtifact', 'json': 'not JSON: Object of type ProjectDesignArtifact is not JSON serializable', 'fields': ['project', 'project_name', 'ticket_prefix', 'owner_user', 'repository', 'remote', 'default_branch', 'worktree_policy', 'design_document', 'implementer_roles', 'audit_roles', 'role_clis', 'include_designer', 'include_audit', 'push_policy', 'gates', 'capability_grants', 'role_models', 'role_efforts', 'catalog_version'], 'repr': "ProjectDesignArtifact(project='p443', project_name='Project 443', ticket_prefix='P443', owner_user='syrd443-owner', repository=PosixPath('/nonexistent/syrd443/repo'), remote='origin', default_branch='main', worktree_policy='per-role', design_document=PosixPath('/nonexistent/syrd443/p443-design.md'), implementer_roles=('main', 'app'), audit_roles=('audit',), role_clis=(('director', 'claude'), ('main', 'codex'), ('app', 'codex'), ('audit', 'claude')), include_designer=True, include_audit=True, push_policy='director-merges', gates={'audit': True, 'inspection': False}, capability_grants={'agy': 'host_default', 'network': False}, role_models=(('main', 'm'),), role_efforts=(('main', 'e'),), catalog_version=2)", 'hash': "TypeError: unhashable type: 'dict'", 'frozen': True, 'calls': []},
    'record: the defaults': {'result': {'dataclass': 'ProjectDesignArtifact', 'project': 'p443', 'project_name': 'Project 443', 'ticket_prefix': 'P443', 'owner_user': 'syrd443-owner', 'repository': 'PATH /nonexistent/syrd443/repo', 'remote': 'origin', 'default_branch': 'main', 'worktree_policy': 'per-role', 'design_document': 'PATH /nonexistent/syrd443/p443-design.md', 'implementer_roles': ['main', 'app'], 'audit_roles': ['audit'], 'role_clis': [['director', 'claude'], ['main', 'codex'], ['app', 'codex'], ['audit', 'claude']], 'include_designer': True, 'include_audit': True, 'push_policy': 'director-merges', 'gates': {'audit': True, 'inspection': False}, 'capability_grants': {'agy': 'host_default', 'network': False}, 'role_models': [], 'role_efforts': [], 'catalog_version': 0}, 'shape': 'ProjectDesignArtifact', 'json': 'not JSON: Object of type ProjectDesignArtifact is not JSON serializable', 'fields': ['project', 'project_name', 'ticket_prefix', 'owner_user', 'repository', 'remote', 'default_branch', 'worktree_policy', 'design_document', 'implementer_roles', 'audit_roles', 'role_clis', 'include_designer', 'include_audit', 'push_policy', 'gates', 'capability_grants', 'role_models', 'role_efforts', 'catalog_version'], 'repr': "ProjectDesignArtifact(project='p443', project_name='Project 443', ticket_prefix='P443', owner_user='syrd443-owner', repository=PosixPath('/nonexistent/syrd443/repo'), remote='origin', default_branch='main', worktree_policy='per-role', design_document=PosixPath('/nonexistent/syrd443/p443-design.md'), implementer_roles=('main', 'app'), audit_roles=('audit',), role_clis=(('director', 'claude'), ('main', 'codex'), ('app', 'codex'), ('audit', 'claude')), include_designer=True, include_audit=True, push_policy='director-merges', gates={'audit': True, 'inspection': False}, capability_grants={'agy': 'host_default', 'network': False}, role_models=(), role_efforts=(), catalog_version=0)", 'hash': "TypeError: unhashable type: 'dict'", 'frozen': True, 'calls': []},
    'record: frozen': {'result': {'raised': 'FrozenInstanceError', 'message': "cannot assign to field 'project'"}, 'calls': []},
    'record: a required field missing': {'result': {'raised': 'TypeError', 'message': "ProjectDesignArtifact.__init__() missing 1 required positional argument: 'gates'"}, 'calls': []},
    'record: an unknown field': {'result': {'raised': 'TypeError', 'message': "ProjectDesignArtifact.__init__() got an unexpected keyword argument 'colour'"}, 'calls': []},
    'record: equal when equal': {'result': True, 'shape': 'bool', 'json': 'true', 'calls': []},
    'record: unequal when a default differs': {'result': False, 'shape': 'bool', 'json': 'false', 'calls': []},
    'cli map: pairs': {'result': {'director': 'claude', 'main': 'codex'}, 'shape': {'dict': {'director': 'str', 'main': 'str'}}, 'json': '{"director": "claude", "main": "codex"}', 'calls': []},
    'cli map: a role twice, the last wins': {'result': {'main': 'claude'}, 'shape': {'dict': {'main': 'str'}}, 'json': '{"main": "claude"}', 'calls': []},
    'cli map: no pairs': {'result': {}, 'shape': {'dict': {}}, 'json': '{}', 'calls': []},
    'cli map: a generator': {'result': {'a': 'b', 'c': 'd'}, 'shape': {'dict': {'a': 'str', 'c': 'str'}}, 'json': '{"a": "b", "c": "d"}', 'calls': []},
    'cli map: a pair of three': {'result': {'raised': 'ValueError', 'message': 'too many values to unpack (expected 2, got 3)'}, 'calls': []},
    'markdown: a title and a body': {'result': '# Porter\n\nBuild it.\n', 'shape': 'str', 'json': '"# Porter\\n\\nBuild it.\\n"', 'calls': []},
    'markdown: padded': {'result': '# Porter\n\nBuild it.\n', 'shape': 'str', 'json': '"# Porter\\n\\nBuild it.\\n"', 'calls': []},
    'markdown: a blank title': {'result': '# p443 design\n\nBuild it.\n', 'shape': 'str', 'json': '"# p443 design\\n\\nBuild it.\\n"', 'calls': []},
    'markdown: a blank body': {'result': '# Porter\n\nTBD.\n', 'shape': 'str', 'json': '"# Porter\\n\\nTBD.\\n"', 'calls': []},
    'markdown: both blank': {'result': '# p443 design\n\nTBD.\n', 'shape': 'str', 'json': '"# p443 design\\n\\nTBD.\\n"', 'calls': []},
    'markdown: a multi-line body': {'result': '# T\n\none\n\ntwo\n', 'shape': 'str', 'json': '"# T\\n\\none\\n\\ntwo\\n"', 'calls': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold443.py` (which ran them on the baseline) ------------------------------------
# A case builds a synthetic design record, renders it as the artifact payload or the starting design document, or maps
# role/CLI pairs. Every path is a fixed, non-existent one and nothing is written: no project artifact or document is
# created. The payload's role/CLI map is recorded on the launcher and passed through; the schema may be rebound there.
# Recorded, in order: every call through the launcher with its arguments, and the answer as a value, as the exact JSON
# text (key order included) and by container type, or the exact exception.
BASE = {"project": "p443", "project_name": "Project 443", "ticket_prefix": "P443", "owner_user": "syrd443-owner",
        "repository": "/nonexistent/syrd443/repo", "remote": "origin", "default_branch": "main", "worktree_policy": "per-role",
        "design_document": "/nonexistent/syrd443/p443-design.md", "implementer_roles": ("main", "app"), "audit_roles": ("audit",),
        "role_clis": (("director", "claude"), ("main", "codex"), ("app", "codex"), ("audit", "claude")),
        "include_designer": True, "include_audit": True, "push_policy": "director-merges",
        "gates": {"audit": True, "inspection": False}, "capability_grants": {"agy": "host_default", "network": False}}
PAYLOAD = {
    "a minimal record": {},
    "models, efforts and a catalog version": {"role_models": (("main", "gpt-x"), ("director", "opus")), "role_efforts": (("main", "high"),), "catalog_version": 3},
    "models only": {"role_models": (("main", "gpt-x"),)},
    "a negative catalog version": {"catalog_version": -1},
    "no roles and no CLIs": {"implementer_roles": (), "audit_roles": (), "role_clis": (), "include_designer": False, "include_audit": False},
    "a designer without audit": {"include_designer": True, "include_audit": False, "audit_roles": ()},
    "a role named twice in the CLI pairs": {"role_clis": (("main", "codex"), ("main", "claude"))},
    "relative paths": {"repository": "repo", "design_document": "docs/design.md"},
    "empty gates and grants": {"gates": {}, "capability_grants": {}},
    "grants that are not JSON": {"capability_grants": {"set": frozenset({"a"})}},
    "the schema rebound on the launcher": {"launcher": {"PROJECT_DESIGN_ARTIFACT_SCHEMA": "syrd443.v9"}},
    "the CLI map rebound on the launcher": {"launcher": {"_role_cli_map": "CLIMAP"}},
    "a record missing a field": {"missing": "remote"},
    "CLI pairs that are not pairs": {"role_clis": (("main",),)},
    "models that are not pairs": {"role_models": ("main",)},
}
RECORD = {
    "every field": {},
    "the defaults": {"defaults": True},
    "frozen": {"assign": ("project", "other")},
    "a required field missing": {"drop": "gates"},
    "an unknown field": {"extra": {"colour": "red"}},
    "equal when equal": {"compare": {}},
    "unequal when a default differs": {"compare": {"catalog_version": 1}},
}
CLIMAP = {
    "pairs": [("director", "claude"), ("main", "codex")],
    "a role twice, the last wins": [("main", "codex"), ("main", "claude")],
    "no pairs": [],
    "a generator": "GEN",
    "a pair of three": [("main", "codex", "x")],
}
MARKDOWN = {
    "a title and a body": {"project": "p443", "title": "Porter", "body": "Build it."},
    "padded": {"project": "p443", "title": "  Porter  ", "body": "\n  Build it.\n\n"},
    "a blank title": {"project": "p443", "title": "   ", "body": "Build it."},
    "a blank body": {"project": "p443", "title": "Porter", "body": " \n "},
    "both blank": {"project": "p443", "title": "", "body": ""},
    "a multi-line body": {"project": "p443", "title": "T", "body": "one\n\ntwo\n"},
}
CASES = {
    **{f"payload: {k}": {"call": "payload", **v} for k, v in PAYLOAD.items()},
    **{f"record: {k}": {"call": "record", **v} for k, v in RECORD.items()},
    **{f"cli map: {k}": {"call": "climap", "pairs": v} for k, v in CLIMAP.items()},
    **{f"markdown: {k}": {"call": "markdown", **v} for k, v in MARKDOWN.items()},
}
FUNCTIONS = ("ProjectDesignArtifact", "_role_cli_map", "project_design_artifact_payload", "_project_design_markdown")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions; the payload's role/CLI map recorded on `t`, the launcher."""
    import dataclasses, json
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []

    def norm(value):
        if isinstance(value, (set, frozenset)):
            return {"set": sorted(norm(v) for v in value)}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if dataclasses.is_dataclass(value):
            return {"dataclass": type(value).__qualname__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        return repr(value)

    def shape(value):
        if isinstance(value, dict):
            return {"dict": {str(k): shape(v) for k, v in value.items()}}
        if isinstance(value, (list, tuple)):
            return {type(value).__name__: [shape(v) for v in value]}
        return type(value).__name__

    def hashed(value):
        try:
            return hash(value) == hash(value)
        except TypeError as exc:  # a frozen record holding dicts cannot be hashed
            return f"TypeError: {exc}"

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    names = [*FUNCTIONS, "PROJECT_DESIGN_ARTIFACT_SCHEMA"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            return f

        stand = {"CLIMAP": lambda pairs: note("_role_cli_map", pairs) or {"syrd443": "rebound"}}
        t._role_cli_map = passthrough("_role_cli_map")
        for k, v in spec.get("launcher", {}).items():
            setattr(t, k, stand.get(v, v))

        def fields(**over):
            f = {**BASE, **over}
            f["repository"], f["design_document"] = _P(f["repository"]), _P(f["design_document"])
            return f

        call = spec["call"]
        extra = {}
        try:
            if call == "payload":
                over = {k: v for k, v in spec.items() if k not in ("call", "launcher", "missing")}
                if spec.get("missing"):
                    f = fields(**over)
                    del f[spec["missing"]]
                    record = SimpleNamespace(**f)
                else:
                    record = under["ProjectDesignArtifact"](**fields(**over))
                got = under["project_design_artifact_payload"](record)
                if isinstance(got, dict) and isinstance(got.get("project"), dict):
                    extra = {"gates is the record's": got["project"].get("gates") is getattr(record, "gates", None),
                             "grants are the record's": got["project"].get("capability_grants") is getattr(record, "capability_grants", None)}
            elif call == "record":
                f = fields()
                if spec.get("defaults"):
                    got = under["ProjectDesignArtifact"](**f)
                elif spec.get("assign"):
                    record = under["ProjectDesignArtifact"](**f)
                    setattr(record, *spec["assign"])
                    got = record
                elif spec.get("drop"):
                    del f[spec["drop"]]
                    got = under["ProjectDesignArtifact"](**f)
                elif "extra" in spec:
                    got = under["ProjectDesignArtifact"](**f, **spec["extra"])
                elif "compare" in spec:
                    got = under["ProjectDesignArtifact"](**f) == under["ProjectDesignArtifact"](**f, **spec["compare"])
                else:
                    got = under["ProjectDesignArtifact"](**f, role_models=(("main", "m"),), role_efforts=(("main", "e"),), catalog_version=2)
                if dataclasses.is_dataclass(got):
                    extra = {"fields": [f.name for f in dataclasses.fields(got)], "repr": repr(got), "hash": hashed(got),
                             "frozen": type(got).__dataclass_params__.frozen}
            elif call == "climap":
                pairs = (p for p in [("a", "b"), ("c", "d")]) if spec["pairs"] == "GEN" else list(map(tuple, spec["pairs"]))
                got = under["_role_cli_map"](pairs)
            else:
                got = under["_project_design_markdown"](spec["project"], title=spec["title"], body=spec["body"])
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": str(exc)}, "calls": calls}
        try:
            text = json.dumps(got)
        except TypeError as exc:
            text = f"not JSON: {exc}"
        return {"result": norm(got), "shape": shape(got), "json": text, **extra, "calls": calls}
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


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.project_design_payload as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.project_design_payload", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_design_payload")):
        result = python("import dataclasses, importlib, inspect, typing; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_design_payload as m; "
                        "A = m.ProjectDesignArtifact; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "[f.name for f in dataclasses.fields(A)] == " + repr(list(RECORD_FIELDS)) + ", "
                        "[(f.name, f.default) for f in dataclasses.fields(A) if f.default is not dataclasses.MISSING], "
                        "A.__dataclass_params__.frozen and A.__dataclass_params__.eq and not A.__dataclass_params__.order, "
                        "sorted(typing.get_type_hints(A)) == sorted(" + repr(RECORD_FIELDS) + "), "
                        "[p for n in ('_role_cli_map', 'project_design_artifact_payload', '_project_design_markdown') for p, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty], "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'PROJECT_DESIGN_ARTIFACT_SCHEMA')))")
        check(result.stdout.strip() == "True True [('role_models', ()), ('role_efforts', ()), ('catalog_version', 0)] True True [] True",
              f"{' then '.join(order)}: one set of objects; the record's fields, their order, defaults and flags, and its resolvable hints: "
              f"{result.stdout}{result.stderr[-600:]}")
    import dataclasses as _dataclasses
    import typing
    check(m.dataclass is _dataclasses.dataclass and m.Path is Path and m.Any is typing.Any and m.Sequence is typing.Sequence,
          "the standard-library names are the module's own, the very objects the launcher holds")
    check(m.ProjectDesignArtifact.__module__ == "scripts.project_design_payload" and m.ProjectDesignArtifact.__qualname__ == "ProjectDesignArtifact",
          "the record's class now names this module as its home; its qualified name, and so its repr, is unchanged")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "project_design_payload.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, siblings included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if isinstance(node, ast.ClassDef):
            check(imports == [] and [ast.unparse(d) for d in node.decorator_list] == ["dataclass(frozen=True)"],
                  f"{name}: the standard frozen dataclass, nothing imported: {imports}")
            continue
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == (["from scripts import team_launcher as launcher"] if expected else [])
              and (not expected or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and nothing else imported: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "from dataclasses import dataclass", "from pathlib import Path", "from typing import Any, Sequence"]
          and not [n for n in tree.body if isinstance(n, ast.If)],
          f"the standard library only, nothing for annotations alone: {top}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the four in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_four_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_design_payload"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the four, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"LauncherUpgradeResult", "_default_role_cli_pairs", "_write_json_atomic"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition names them, as before: {uses}")
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
        typing_only = sorted(a.name for n in ast.walk(source) if isinstance(n, ast.If) and ast.unparse(n.test) == "TYPE_CHECKING"
                             for x in ast.walk(n) if isinstance(x, ast.ImportFrom) and x.module == "scripts.team_launcher" for a in x.names if a.name in MOVED)
        eager = [ast.unparse(x) for x in source.body if isinstance(x, ast.ImportFrom) and x.module == "scripts.team_launcher"]
        check(dict(sorted(got.items())) == counts and typing_only == TYPING.get(path, []) and eager == [],
              f"{path} still reads them through the launcher, as often as before, and names the record's type there for annotations only: {got} {typing_only} {eager}")


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

    minimal = result("payload: a minimal record")
    check(list(minimal) == ["schema", "design_document", "project"] and minimal["schema"] == "switchyard.project.v1"
          and list(minimal["project"]) == ["slug", "name", "ticket_prefix", "owner_user", "repository", "remote", "default_branch", "worktree_policy",
                                           "roles", "audit_roles", "include_designer", "include_audit", "role_clis", "push_policy", "gates", "capability_grants"]
          and GOLDEN["payload: a minimal record"]["json"].startswith('{"schema": "switchyard.project.v1", "design_document": "/nonexistent/syrd443/p443-design.md", "project": {"slug": "p443"'),
          "the payload: schema, design document, then the project's fields in a fixed order -- the exact JSON text is pinned")
    full = result("payload: models, efforts and a catalog version")["project"]
    check(list(full)[12:16] == ["role_clis", "role_models", "role_efforts", "catalog_version"]
          and full["role_models"] == {"main": "gpt-x", "director": "opus"} and full["catalog_version"] == 3
          and "role_efforts" not in result("payload: models only")["project"] and result("payload: a negative catalog version")["project"]["catalog_version"] == -1,
          "models, efforts and the catalog version are written only when set (any non-zero version), between the CLIs and the push policy")
    check(minimal["project"]["repository"] == "/nonexistent/syrd443/repo" and GOLDEN["payload: a minimal record"]["shape"]["dict"]["project"]["dict"]["roles"] == {"list": ["str", "str"]}
          and result("payload: relative paths")["design_document"] == "docs/design.md",
          "paths are written as given, as strings; role tuples become lists")
    check(all(GOLDEN[k]["gates is the record's"] and GOLDEN[k]["grants are the record's"] for k in GOLDEN if "gates is the record's" in GOLDEN[k])
          and GOLDEN["payload: grants that are not JSON"]["json"].startswith("not JSON"),
          "gates and grants are the record's own objects, passed through unchecked")
    check(result("payload: a role named twice in the CLI pairs")["project"]["role_clis"] == {"main": "claude"}
          and result("cli map: a role twice, the last wins") == {"main": "claude"} and result("cli map: a generator") == {"a": "b", "c": "d"}
          and result("cli map: a pair of three")["raised"] == "ValueError",
          "the CLI map: the last pair for a role wins; any iterable of pairs; anything else refused")
    check(result("payload: the schema rebound on the launcher")["schema"] == "syrd443.v9"
          and result("payload: the CLI map rebound on the launcher")["project"]["role_clis"] == {"syrd443": "rebound"}
          and all(GOLDEN[k]["calls"] == [["_role_cli_map", GOLDEN[k]["calls"][0][1], {}]] for k in GOLDEN
                  if k.startswith("payload: ") and "raised" not in result(k)),
          "the schema and the CLI map are the launcher's when the payload is built, the map asked exactly once")
    check([result(f"payload: {k}")["raised"] for k in ("a record missing a field", "CLI pairs that are not pairs", "models that are not pairs")]
          == ["AttributeError", "ValueError", "ValueError"],
          "a record missing a field, or pairs that are not pairs, raise")
    check(GOLDEN["record: every field"]["fields"] == list(RECORD_FIELDS) and GOLDEN["record: every field"]["frozen"] is True
          and GOLDEN["record: every field"]["repr"].startswith("ProjectDesignArtifact(project='p443', ")
          and result("record: frozen")["raised"] == "FrozenInstanceError"
          and result("record: a required field missing")["message"] == "ProjectDesignArtifact.__init__() missing 1 required positional argument: 'gates'"
          and result("record: an unknown field")["raised"] == "TypeError"
          and result("record: equal when equal") is True and result("record: unequal when a default differs") is False
          and GOLDEN["record: the defaults"]["result"]["catalog_version"] == 0,
          "the record: its fields in order, frozen, equal by value, its repr by class name, its defaults, and refusals word for word")
    check(GOLDEN["record: every field"]["hash"].startswith("TypeError"),
          "a record holding its gates and grants dicts cannot be hashed (the baseline's behaviour, kept)")
    check([result(f"markdown: {k}") for k in ("a title and a body", "padded", "a blank title", "a blank body", "both blank")]
          == ["# Porter\n\nBuild it.\n", "# Porter\n\nBuild it.\n", "# p443 design\n\nBuild it.\n", "# Porter\n\nTBD.\n", "# p443 design\n\nTBD.\n"],
          "the document: the trimmed title (else '<project> design'), a blank line, the trimmed body (else 'TBD.'), a final newline")


def test_every_launcher_seam_is_reached() -> None:
    # The payload reads the role/CLI map (a recorder on the launcher) and the schema (rebound there by its own case).
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(SEAMS == {"project_design_artifact_payload": {"PROJECT_DESIGN_ARTIFACT_SCHEMA": 1, "_role_cli_map": 1}}
          and "_role_cli_map" in REACHED and rebound == {"PROJECT_DESIGN_ARTIFACT_SCHEMA", "_role_cli_map"},
          f"the map reached through the launcher, and both seams rebound there: {sorted(REACHED)} {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_four_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"project_design_payload_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
