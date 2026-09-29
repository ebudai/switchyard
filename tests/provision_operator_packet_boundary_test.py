#!/usr/bin/env python3
"""SYRD-474: the operator packet renderer, against project_provision it came out of.

The seven -- `render_operator_commands`, which renders the root-run packet for a
plan, `PACKET_PROVISION_DIR` and `packet_companion` (a file beside the packet,
addressed from it; SYRD-149), and the commands only the packet writes
(`postgres_sql_file_command`, `service_user_command`, `peer_auth_command`,
`owned_directory_command`) -- moved unchanged into
`scripts/ticket_board/provision_operator_packet.py`; `project_provision`
re-exports them, in both branches of its import block, and keeps the shared
helpers they read and its own imports. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first. The module
  alone loads only its package.
- **Seams (rule 24):** everything they read of `project_provision` -- each
  other, the plan-path and repository helpers, the quoting, the boundary
  markers and every earlier slice's commands -- is read through it when they
  run, with the direct-script fallback; `write_artifacts` and `main`, which
  stay, render the packet by the name `project_provision` holds, so a patch
  there is what they use. Callers are counted across the re-exported modules.
- **The behaviour is the baseline's:** the packet for eleven synthetic plans
  and without linger, every helper, and the packet with thirteen names rebound
  on `project_provision`. `GOLDEN` below was produced by running the BASELINE
  module's own definitions over the very cases embedded here (`gold474.py`),
  not typed; it is byte-identical under `env -i`, in a normal role pane, with
  another HOME, USER, COLUMNS and TMPDIR and a leaked board Python, shared
  Python and tenant-control root, under umask 077 and under several hash seeds.
- **The direct script answers what the package answers** and looks at no host
  path (a recorder with a positive control), and the default, shaped, lean and
  roles packets are byte-identical through both.

No real home, tenant, account, /etc, /var or /opt path is read or written:
plans are synthetic with the board Python pinned, and nothing is run. Spawns,
every exec, signals, account and group lookups and socket connections are
refused for each case.
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
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_operator_packet as m  # noqa: E402

CHECKS = 0
MOVED = ('PACKET_PROVISION_DIR', 'packet_companion', 'postgres_sql_file_command', 'service_user_command', 'peer_auth_command', 'owned_directory_command', 'render_operator_commands')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'packet_companion': {'PACKET_PROVISION_DIR': 1, 'shell_quote': 1},
    'postgres_sql_file_command': {'packet_companion': 1, 'shell_quote': 2},
    'service_user_command': {'shell_quote': 1},
    'peer_auth_command': {'DEFAULT_PG_IDENT_MAP': 1, 'shell_quote': 5},
    'owned_directory_command': {'_dedupe': 1, 'shell_quote': 3},
    'render_operator_commands': {'REPOSITORY_BOUNDARY_BEGIN': 1, 'REPOSITORY_BOUNDARY_END': 1, 'TENANT_CONTROL_ROOT': 1, 'WRITABLE_REPOSITORY_COPY_MODE': 1, 'commit_store_read_commands': 1, 'owned_ancestor_dirs': 4, 'owned_directory_command': 3, 'owner_github_identity_commands': 1, 'packet_companion': 6, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'readable_system_unit_path': 1, 'repository_copy_confinement_commands': 1, 'repository_group_commands': 1, 'repository_group_name': 1, 'role_accounts_command': 1, 'role_control_sudoers': 1, 'role_runtime_command': 1, 'role_worktree_access_commands': 1, 'roles_group_name': 1, 'service_user_command': 1, 'shell_quote': 37, 'socket_group_retirement_commands': 1, 'system_unit_proof_commands': 1, 'tenant_control_grant_name': 1, 'tenant_control_grant_path': 1, 'tenant_control_repository': 1, 'tenant_source_confinement_commands': 1, 'tenant_worktree_base': 1, 'tenant_worktree_confinement_commands': 1},
}
#: Measured on the baseline: every project_provision definition outside the seven that names them, and how often.
DISPATCH = {'write_artifacts': {'render_operator_commands': 1}, 'main': {'render_operator_commands': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {}
#: The BASELINE's own behaviour for the cases below (`gold474.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'packet: default': {'result': {'type': 'str', 'value': {'chars': 23808, 'lines': 345, 'sha256': 'ad855ddfa75a1a21', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: shaped': {'result': {'type': 'str', 'value': {'chars': 23808, 'lines': 345, 'sha256': 'ad855ddfa75a1a21', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: lean': {'result': {'type': 'str', 'value': {'chars': 23808, 'lines': 345, 'sha256': 'ad855ddfa75a1a21', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: no traversal': {'result': {'type': 'str', 'value': {'chars': 23095, 'lines': 335, 'sha256': '727f1d3a58737221', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: control user': {'result': {'type': 'str', 'value': {'chars': 24227, 'lines': 349, 'sha256': 'd7f80e51b1690c8a', 'marked': {'companion': 7, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 20, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 7, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: named key': {'result': {'type': 'str', 'value': {'chars': 23906, 'lines': 351, 'sha256': '597e856008c8fe84', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: project repository': {'result': {'type': 'str', 'value': {'chars': 23689, 'lines': 345, 'sha256': '5b5bdfaa06bce82c', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: two commit stores': {'result': {'type': 'str', 'value': {'chars': 22621, 'lines': 331, 'sha256': 'abc4926fe881d810', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 13, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: stores under the home': {'result': {'type': 'str', 'value': {'chars': 23099, 'lines': 339, 'sha256': '97cf3ccf1e8a43ab', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 15, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: pgu': {'result': {'type': 'str', 'value': {'chars': 22301, 'lines': 332, 'sha256': '2c7a51c919eff79f', 'marked': {'companion': 4, 'psql': 3, 'service user': 1, 'peer auth': 1, 'owned dirs': 14, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 4, 'peer_auth_command': 1, 'postgres_sql_file_command': 3, 'service_user_command': 1}},
    'packet: roles': {'result': {'type': 'str', 'value': {'chars': 30328, 'lines': 428, 'sha256': '34a8648ed8e68d3c', 'marked': {'companion': 6, 'psql': 4, 'service user': 7, 'peer auth': 1, 'owned dirs': 34, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 6, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: roles and worktrees': {'result': {'type': 'str', 'value': {'chars': 30380, 'lines': 426, 'sha256': '056170d8a81c99d6', 'marked': {'companion': 8, 'psql': 4, 'service user': 7, 'peer auth': 1, 'owned dirs': 34, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 8, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: no linger': {'result': {'type': 'str', 'value': {'chars': 23998, 'lines': 345, 'sha256': '0dd46381b904c2bf', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'packet: roles, no linger': {'result': {'type': 'str', 'value': {'chars': 30570, 'lines': 426, 'sha256': 'ea83d2a4fa79e2e8', 'marked': {'companion': 8, 'psql': 4, 'service user': 7, 'peer auth': 1, 'owned dirs': 34, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 8, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'constant': {'result': {'type': 'str', 'value': '"$provision_dir"'}, 'calls': {}},
    "companion: 'a.conf'": {'result': {'type': 'str', 'value': '"$provision_dir"/\'a.conf\''}, 'calls': {}},
    'companion: "it\'s.sql"': {'result': {'type': 'str', 'value': '"$provision_dir"/\'it\'"\'"\'s.sql\''}, 'calls': {}},
    "companion: 'sub/dir.sql'": {'result': {'type': 'str', 'value': '"$provision_dir"/\'sub/dir.sql\''}, 'calls': {}},
    "companion: '/abs.sql'": {'result': {'raised': 'SystemExit', 'message': 'a packet companion is a file name beside the packet, not a path: /abs.sql'}, 'calls': {}},
    "companion: ''": {'result': {'type': 'str', 'value': '"$provision_dir"/\'\''}, 'calls': {}},
    'psql: plain': {'result': {'type': 'str', 'value': "sudo cat '/p474/x.sql' | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f -"}, 'calls': {}},
    'psql: url': {'result': {'type': 'str', 'value': "sudo cat '/p474/x.sql' | sudo -u postgres psql -X -v ON_ERROR_STOP=1 'postgresql:///p474?host=/run/x' -f -"}, 'calls': {}},
    'psql: companion': {'result': {'type': 'str', 'value': 'sudo cat "$provision_dir"/\'x.sql\' | sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f -'}, 'calls': {'packet_companion': 1}},
    'psql: companion path refused': {'result': {'raised': 'SystemExit', 'message': 'a packet companion is a file name beside the packet, not a path: /p474/x.sql'}, 'calls': {'packet_companion': 1}},
    "service user: 'ticket_board_service'": {'result': {'type': 'str', 'value': {'chars': 534, 'lines': 10, 'sha256': '0ce6bf1075f43ce2', 'marked': {'companion': 0, 'psql': 0, 'service user': 1, 'peer auth': 0, 'owned dirs': 0, 'boundary begin': 0}}}, 'calls': {}},
    'service user: "o\'brien"': {'result': {'type': 'str', 'value': {'chars': 516, 'lines': 10, 'sha256': 'ea7edd6631c013a8', 'marked': {'companion': 0, 'psql': 0, 'service user': 1, 'peer auth': 0, 'owned dirs': 0, 'boundary begin': 0}}}, 'calls': {}},
    "service user: ''": {'result': {'type': 'str', 'value': {'chars': 494, 'lines': 10, 'sha256': '7a56f6bb8b9d1038', 'marked': {'companion': 0, 'psql': 0, 'service user': 1, 'peer auth': 0, 'owned dirs': 0, 'boundary begin': 0}}}, 'calls': {}},
    'peer auth': {'result': {'type': 'str', 'value': "sudo env PG_DATABASE='p474_ticket_board' PG_IDENT_MAP='pgu_ticket_board_service' SERVICE_USER='boardsvc' SERVICE_ROLE='ticket_board_service' '/p474/source/scripts/ticket-board-boardsvc-setup.sh' --apply-peer-auth"}, 'calls': {}},
    'peer auth: pgu': {'result': {'type': 'str', 'value': "sudo env PG_DATABASE='pgu' PG_IDENT_MAP='pgu_ticket_board_service' SERVICE_USER='boardsvc' SERVICE_ROLE='ticket_board_service' '/p474/source/scripts/ticket-board-boardsvc-setup.sh' --apply-peer-auth"}, 'calls': {}},
    'owned dirs: none': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'owned dirs: duplicates': {'result': {'type': 'str', 'value': "sudo install -d -m 0755 -o 'p474-owner' -g 'p474-owner' '/p474/a' '/p474/b'"}, 'calls': {}},
    'owned dirs: mode': {'result': {'type': 'str', 'value': 'sudo install -d -m 0700 -o \'p474-owner\' -g \'p474-owner\' \'/p474/it\'"\'"\'s\''}, 'calls': {}},
    'rebound on project_provision: shell_quote': {'result': {'type': 'str', 'value': {'chars': 30304, 'lines': 428, 'sha256': 'a4a2a37ef2c21c88', 'marked': {'companion': 6, 'psql': 4, 'service user': 7, 'peer auth': 1, 'owned dirs': 34, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 6, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1, 'shell_quote rebound': 246}},
    'rebound on project_provision: owned_directory_command': {'result': {'type': 'str', 'value': {'chars': 23542, 'lines': 345, 'sha256': '95ae30a177969772', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 16, 'boundary begin': 1}}}, 'calls': {'owned_directory_command rebound': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: packet_companion': {'result': {'type': 'str', 'value': {'chars': 23768, 'lines': 345, 'sha256': 'f09ba3e20addc98e', 'marked': {'companion': 0, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion rebound': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: postgres_sql_file_command': {'result': {'type': 'str', 'value': {'chars': 23416, 'lines': 345, 'sha256': 'd0924cb3f4030592', 'marked': {'companion': 3, 'psql': 0, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 3, 'peer_auth_command': 1, 'postgres_sql_file_command rebound': 4, 'service_user_command': 1}},
    'rebound on project_provision: service_user_command': {'result': {'type': 'str', 'value': {'chars': 23320, 'lines': 336, 'sha256': 'cbdc10ea12087b43', 'marked': {'companion': 5, 'psql': 4, 'service user': 0, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command rebound': 1}},
    'rebound on project_provision: peer_auth_command': {'result': {'type': 'str', 'value': {'chars': 23605, 'lines': 345, 'sha256': '76288a748014991e', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 0, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command rebound': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: role_accounts_command': {'result': {'type': 'str', 'value': {'chars': 28880, 'lines': 399, 'sha256': '5ac4463f6c98d38f', 'marked': {'companion': 6, 'psql': 4, 'service user': 4, 'peer auth': 1, 'owned dirs': 31, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 6, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'role_accounts_command rebound': 1, 'service_user_command': 1}},
    'rebound on project_provision: REPOSITORY_BOUNDARY_BEGIN': {'result': {'type': 'str', 'value': {'chars': 23782, 'lines': 345, 'sha256': '1b2a0783dc613bc0', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 0}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: TENANT_CONTROL_ROOT': {'result': {'type': 'str', 'value': {'chars': 23567, 'lines': 349, 'sha256': '001137cdef727dfc', 'marked': {'companion': 7, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 20, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 7, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: tenant_worktree_base': {'result': {'type': 'str', 'value': {'chars': 23379, 'lines': 338, 'sha256': 'da98ea152806f5ef', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 18, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1, 'tenant_worktree_base rebound': 1}},
    'rebound on project_provision: PACKET_PROVISION_DIR': {'result': {'type': 'str', 'value': {'chars': 23783, 'lines': 345, 'sha256': '5cf006f89dd5b078', 'marked': {'companion': 0, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: DEFAULT_PG_IDENT_MAP': {'result': {'type': 'str', 'value': {'chars': 23792, 'lines': 345, 'sha256': '6a5b42fbc057a3c6', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
    'rebound on project_provision: _dedupe': {'result': {'type': 'str', 'value': {'chars': 23829, 'lines': 345, 'sha256': 'a332ef75ea34f336', 'marked': {'companion': 5, 'psql': 4, 'service user': 1, 'peer auth': 1, 'owned dirs': 19, 'boundary begin': 1}}}, 'calls': {'_dedupe rebound': 3, 'owned_directory_command': 3, 'packet_companion': 5, 'peer_auth_command': 1, 'postgres_sql_file_command': 4, 'service_user_command': 1}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = {'default': 10, 'shaped': 10, 'lean': 10, 'roles': 10}

# --- the cases, shared verbatim with `gold474.py` (which ran them on the baseline) ------------------------------------
# A case renders the operator packet for a synthetic plan, or calls one helper the packet alone uses, and records the
# answer or the exact exception, and how many times it called each of the packet's helpers on `project_provision`
# (recorded there and passed through). A packet is recorded by its length, lines and digest, and by how many of its
# lines each helper wrote. Plans are synthetic (/p474 paths) with the board Python pinned; nothing is run or written. A
# case may rebind a `project_provision` name to show it is read there when the packet renders.
PLANS = {
    "default": {},
    "shaped": {"implementer_roles": ["main", "perf"], "audit_roles": ["audit", "inspector"], "vcs_close_role": "ops"},
    "lean": {"include_designer": False, "include_audit": False},
    "no traversal": {"board_service_traversal": False},
    "control user": {"control_user": "p474-human"},
    "named key": {"owner_github_key_name": "id_p474", "owner_github_host_alias": "github-p474"},
    "project repository": {"project_repository": "/p474/project"},
    "two commit stores": {"commit_git_dir": "/p474/a.git:/p474/b.git: "},
    "stores under the home": {"commit_git_dir": "/p474/home/a.git:/p474/home/b.git:/p474/elsewhere.git"},
    "pgu": {"project": "pgu", "port": 34475},
    "roles": {"ROLES": True},
    "roles and worktrees": {"ROLES": True, "role_worktrees": (("main", "/p474/wt/main"),), "control_user": "p474-human"},
}
REBINDS = {
    "shell_quote": ("roles", lambda v: f"<{v}>"),
    "owned_directory_command": ("default", lambda plan, dirs, *, mode="0755": f"OWNED[{mode}:{len(dirs)}]"),
    "packet_companion": ("default", lambda name: f"COMPANION[{name}]"),
    "postgres_sql_file_command": ("default", lambda sql_file, *, database_url="", companion=False: f"PSQL[{sql_file}:{bool(database_url)}:{companion}]"),
    "service_user_command": ("default", lambda service_user: f"SERVICE USER[{service_user}]"),
    "peer_auth_command": ("default", lambda plan: "PEER AUTH"),
    "role_accounts_command": ("roles", lambda plan: "ROLE ACCOUNTS"),
    "REPOSITORY_BOUNDARY_BEGIN": ("default", "# >>> p474"),
    "TENANT_CONTROL_ROOT": ("control user", "/p474/control"),
    "tenant_worktree_base": ("default", lambda plan: "/p474/WORKTREES"),
    "PACKET_PROVISION_DIR": ("default", '"$p474_dir"'),
    "DEFAULT_PG_IDENT_MAP": ("default", "p474_map"),
    "_dedupe": ("default", lambda values: tuple(values)),
}
CASES = {
    **{f"packet: {label}": {"call": "render_operator_commands", "plan": label} for label in PLANS},
    "packet: no linger": {"call": "render_operator_commands", "plan": "default", "kw": {"enable_owner_linger": False}},
    "packet: roles, no linger": {"call": "render_operator_commands", "plan": "roles and worktrees", "kw": {"enable_owner_linger": False}},
    "constant": {"call": None},
    **{f"companion: {name!r}": {"call": "packet_companion", "args": [name]} for name in ("a.conf", "it's.sql", "sub/dir.sql", "/abs.sql", "")},
    "psql: plain": {"call": "postgres_sql_file_command", "args": ["/p474/x.sql"]},
    "psql: url": {"call": "postgres_sql_file_command", "args": ["/p474/x.sql"], "kw": {"database_url": "postgresql:///p474?host=/run/x"}},
    "psql: companion": {"call": "postgres_sql_file_command", "args": ["x.sql"], "kw": {"companion": True}},
    "psql: companion path refused": {"call": "postgres_sql_file_command", "args": ["/p474/x.sql"], "kw": {"companion": True}},
    **{f"service user: {user!r}": {"call": "service_user_command", "args": [user]} for user in ("ticket_board_service", "o'brien", "")},
    "peer auth": {"call": "peer_auth_command", "plan": "default"},
    "peer auth: pgu": {"call": "peer_auth_command", "plan": "pgu"},
    "owned dirs: none": {"call": "owned_directory_command", "plan": "default", "args": [[]]},
    "owned dirs: duplicates": {"call": "owned_directory_command", "plan": "default", "args": [["/p474/a", "/p474/b", "/p474/a"]]},
    "owned dirs: mode": {"call": "owned_directory_command", "plan": "default", "args": [["/p474/it's"]], "kw": {"mode": "0700"}},
    **{f"rebound on project_provision: {name}": {"call": "render_operator_commands", "plan": plan, "rebind": name} for name, (plan, _) in REBINDS.items()},
}
FUNCTIONS = ("packet_companion", "postgres_sql_file_command", "service_user_command", "peer_auth_command", "owned_directory_command", "render_operator_commands")
PASSED = ("packet_companion", "postgres_sql_file_command", "service_user_command", "peer_auth_command", "owned_directory_command")
MARKERS = {"companion": '"$provision_dir"/', "psql": "| sudo -u postgres psql -X -v ON_ERROR_STOP=1", "service user": "if ! getent passwd ",
           "peer auth": " --apply-peer-auth", "owned dirs": "sudo install -d -m ", "boundary begin": "# >>> switchyard repository boundary"}


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definition; `t` is project_provision, whose packet helpers are recorded and passed through."""
    import dataclasses, hashlib, os as _os, pathlib as _pl
    counts = {}

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, str):
            if len(value) <= 400:
                return value
            lines = value.splitlines()
            return {"chars": len(value), "lines": len(lines), "sha256": hashlib.sha256(value.encode()).hexdigest()[:16],
                    "marked": {k: sum(m in line for line in lines) for k, m in MARKERS.items()}}
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    ENV = ("TICKET_BOARD_PYTHON", "SWITCHYARD_SHARED_PYTHON", "SWITCHYARD_TENANT_CONTROL_ROOT")
    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *REBINDS, "build_plan")}
    saved_env = {n: _os.environ.get(n) for n in ENV}
    try:
        for n in ENV:
            _os.environ.pop(n, None)
        _os.environ["TICKET_BOARD_PYTHON"] = "/p474/python"
        plan = None
        if spec.get("plan"):
            kw = dict(PLANS[spec["plan"]])
            roles = kw.pop("ROLES", False)
            worktrees = kw.pop("role_worktrees", ())
            control = kw.pop("control_user", "") if roles else None
            base = {"project": "p474", "owner_user": "p474-owner", "owner_home": _pl.Path("/p474/home"), "port": 34474, "source_repo": _pl.Path("/p474/source")}
            if "commit_git_dir" in kw:
                kw["commit_git_dir"] = kw["commit_git_dir"].replace(":", _os.pathsep)
            if "project_repository" in kw:
                kw["project_repository"] = _pl.Path(kw["project_repository"])
            plan = saved["build_plan"](**{**base, **kw})
            if roles:
                plan = dataclasses.replace(plan, role_accounts=(("director", "p474-director"), ("main", "p474-main"), ("audit", "p474-audit")), roles_group="p474-roles",
                                           role_worktrees=worktrees, **({"control_user": control} if control else {}))
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        if spec.get("rebind"):
            name = spec["rebind"]
            value = REBINDS[name][1]
            setattr(t, name, (lambda value, name: lambda *a, **k: note(f"{name} rebound") or value(*a, **k))(value, name) if callable(value) else value)
        if spec["call"] is None:
            got = saved["PACKET_PROVISION_DIR"]
        else:
            fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
            args = ([plan] if plan is not None else []) + list(spec.get("args", []))
            try:
                got = fn(*args, **spec.get("kw", {}))
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        for n, v in saved_env.items():
            _os.environ.pop(n, None)
            if v is not None:
                _os.environ[n] = v
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


#: What the six functions read on project_provision when they run beyond each other: the shared helpers it keeps and
#: re-exports. None of them may be read bare in the module.
KEPT = ("shell_quote", "_dedupe", "DEFAULT_PG_IDENT_MAP", "tenant_worktree_base", "tenant_control_repository", "repository_group_name",
        "repository_group_commands", "WRITABLE_REPOSITORY_COPY_MODE", "TENANT_CONTROL_ROOT", "REPOSITORY_BOUNDARY_BEGIN", "REPOSITORY_BOUNDARY_END")
#: The one constant, as the baseline wrote it: (annotation, value). Measured on the baseline, not typed.
CONSTANT_TEXT = {'PACKET_PROVISION_DIR': (None, '\'"$provision_dir"\'')}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them: literals only, nothing bound from project_provision.
DEFAULTS = {'packet_companion': [], 'postgres_sql_file_command': ["''", 'False'], 'service_user_command': [], 'peer_auth_command': [], 'owned_directory_command': ["'0755'"], 'render_operator_commands': ['True']}
#: The module's imports at load: the standard library only.
MODULE_IMPORTS = ["from __future__ import annotations", "import os", "from typing import Sequence"]
#: The packets rendered through the direct script and the package, the board Python pinned.
ROLE_ACCOUNTS = (("director", "p474-director"), ("main", "p474-main"))
PACKET_VARIANTS = {
    "default": ([], False),
    "shaped": (["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"], False),
    "lean": (["--no-include-designer", "--no-include-audit"], False),
    "roles": ([], True),
}
#: Refuses and records any stat/lstat/open/access under the host's switchyard, /etc, /var and /opt paths, after proving it
#: catches one (a positive control).
HOST_RECORDER = (
    "import builtins, os, pathlib\n"
    "PREFIXES = ('/usr/local/lib/switchyard', '/etc', '/var', '/opt')\n"
    "hits = []\n"
    "def guarded(real, name):\n"
    "    def f(p, *a, **k):\n"
    "        s = os.fsdecode(p) if isinstance(p, (str, bytes, os.PathLike)) else ''\n"
    "        if s.startswith(PREFIXES):\n"
    "            hits.append((name, s)); raise FileNotFoundError(2, 'refused', s)\n"
    "        return real(p, *a, **k)\n"
    "    return f\n"
    "os.stat, os.lstat, builtins.open, os.access = guarded(os.stat, 'stat'), guarded(os.lstat, 'lstat'), guarded(builtins.open, 'open'), guarded(os.access, 'access')\n"
    "pathlib.Path('/opt/switchyard/syrd474-positive-control').is_file()\n"
    "control = len(hits); hits.clear()\n"
)
#: One probe, run in the package and as the direct script: packets for synthetic plans, each helper, and the packet with
#: names rebound on every copy of project_provision -- as a digest, and the host paths looked at.
PACKET_PROBE = (
    "import dataclasses, hashlib, json, os, pathlib\n"
    "os.environ['TICKET_BOARD_PYTHON'] = '/p474/python'; os.environ.pop('SWITCHYARD_SHARED_PYTHON', None); os.environ.pop('SWITCHYARD_TENANT_CONTROL_ROOT', None)\n"
    "P = pathlib.Path\n"
    "plan = g['build_plan'](project='p474', owner_user='p474-owner', owner_home=P('/p474/home'), port=34474, source_repo=P('/p474/source'), control_user='p474-human')\n"
    "roles = dataclasses.replace(plan, role_accounts=(('main', 'p474-main'),), roles_group='p474-roles')\n"
    "out = [g['render_operator_commands'](plan), g['render_operator_commands'](roles, enable_owner_linger=False), g['packet_companion']('a.sql'),\n"
    "       g['postgres_sql_file_command']('a.sql', companion=True), g['service_user_command']('svc'), g['peer_auth_command'](plan),\n"
    "       g['owned_directory_command'](plan, ['/p474/a', '/p474/a'])]\n"
    "for name, value in (('shell_quote', lambda v: f'<{v}>'), ('packet_companion', lambda name: f'C[{name}]'), ('owned_directory_command', lambda plan, dirs, *, mode='0755': 'OWNED'),\n"
    "                    ('TENANT_CONTROL_ROOT', '/p474/control'), ('REPOSITORY_BOUNDARY_BEGIN', '# >>> p474'), ('role_accounts_command', lambda plan: 'ROLE ACCOUNTS'),\n"
    "                    ('tenant_worktree_base', lambda plan: '/p474/WT')):\n"
    "    saved = [(h, h[name]) for h in HOLDERS]\n"
    "    for h, _ in saved: h[name] = value\n"
    "    out.append(g['render_operator_commands'](roles))\n"
    "    for h, v in saved: h[name] = v\n"
    "print(len(out), len(set(out)), hashlib.sha256(json.dumps(out).encode()).hexdigest(), 'control', control, 'host paths', len(hits))\n"
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
    result = python("import sys, scripts.ticket_board.provision_operator_packet as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_operator_packet", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_operator_packet")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_operator_packet as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_operator_packet'] True",
              f"{' then '.join(order)}: one object each, defined here; nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os, "the standard-library name is the module's own, the very object project_provision holds")


def test_the_packet_writers_render_through_project_provision() -> None:
    # write_artifacts and main's --render stay in project_provision and name the renderer there, when they run: a patch
    # there is what both use, and the module's own renderer otherwise.
    import shutil
    import tempfile
    work = Path(tempfile.mkdtemp(prefix="syrd474-writers-")).resolve()
    try:
        argv = ["--project", "p474", "--owner-user", "p474-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source", "--port", "34474"]
        result = python("import os; os.environ['TICKET_BOARD_PYTHON'] = '/p474/python'\n"
                        "import contextlib, io, scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_operator_packet as m\n"
                        "saved = t.render_operator_commands\n"
                        "t.render_operator_commands = lambda plan, **k: 'PATCHED ' + str(sorted(k)) + '\\n'\n"
                        f"t.main({argv!r} + ['--output-dir', {str(work / 'out')!r}])\n"
                        f"patched_file = open({str(work / 'out' / 'operator-commands.sh')!r}).read()\n"
                        "buf = io.StringIO()\n"
                        "with contextlib.redirect_stdout(buf):\n"
                        f"    t.main({argv!r} + ['--render', 'commands'])\n"
                        "t.render_operator_commands = saved\n"
                        "own = io.StringIO()\n"
                        "with contextlib.redirect_stdout(own):\n"
                        f"    t.main({argv!r} + ['--render', 'commands'])\n"
                        f"plan = t.build_plan(project='p474', owner_user='p474-agent', owner_home=__import__('pathlib').Path({str(work / 'home')!r}), port=34474, "
                        f"source_repo=__import__('pathlib').Path({str(work / 'source')!r}))\n"
                        "print(repr(patched_file), repr(buf.getvalue()), own.getvalue() == m.render_operator_commands(plan) and len(own.getvalue()) > 1000)")
        lines = result.stdout.strip().splitlines()
        check(result.returncode == 0 and lines[0] == f"wrote {work / 'out'}" and lines[-1] == "\"PATCHED ['enable_owner_linger']\\n\" 'PATCHED []\\n' True",
              f"write_artifacts and --render use the renderer project_provision holds when they run; restored, it is the module's own: "
              f"{result.stdout}{result.stderr[-600:]}")
    finally:
        shutil.rmtree(work)


def test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it -- the copy they read through. Both runs record any host
    # path they look at; the rebinds cover every copy.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(HOST_RECORDER + f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd474_script__'); "
                       "m = sys.modules['provision_operator_packet']; import project_provision\n"
                       "HOLDERS = [g, vars(project_provision), g['main'].__globals__]\n"
                       f"print(all(g[n] is getattr(m, n) and getattr(project_provision, n) is getattr(m, n) for n in {MOVED!r}))\n" + PACKET_PROBE)
    as_package = python(HOST_RECORDER + "import scripts.ticket_board.project_provision as pp; g = vars(pp); HOLDERS = [g]\n" + PACKET_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("14 14 ")
          and lines[1].endswith(" control 1 host paths 0"),
          f"as a direct script: both copies of project_provision hold the module's own objects, and all fourteen answers -- every rebind a "
          f"different packet -- are the package's, byte for byte, with no host path looked at (the recorder's positive control caught): "
          f"{as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_operator_packet.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its siblings included, read through it exactly as often as before: {through}")
        first = 1 if ast.get_docstring(node) is not None else 0
        own = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and len(own) == 2,
              f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback, and nothing else: {own}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT or x.id in MOVED)
                       and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == MODULE_IMPORTS and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the constant is the baseline's: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the seven, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_keeps_what_they_read() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_operator_packet" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_operator_packet" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the seven, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # A name stays reachable on project_provision: defined there, or re-exported there, unaliased.
    exported = {a.name for n in ast.walk(guard) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 for a in n.names
                if a.asname is None and n.module != "provision_operator_packet"}
    check(not defined & set(MOVED) and set(KEPT) <= defined | exported, "project_provision defines none of them, and keeps what they read, its own or re-exported")
    check("os" in {a.asname or a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}, "project_provision's own os import is left as it was")
    # The definitions that name them are counted wherever they live -- project_provision, or a later slice's module, whose
    # read through project_provision (provision.X) counts as the name -- so this check survives the next slice.
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_operator_packet"]
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
    check(READERS == {}, f"no production module imports them from project_provision: {READERS}")


def test_the_direct_script_and_the_package_render_the_same_packets() -> None:
    # Four synthetic provisioning packets, each rendered by `project_provision.py` run as a script (its import fallback
    # taken) and through the package entry, in a test-owned directory, the board Python pinned.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd474-packet-")).resolve()
    try:
        for variant, (extra, roles) in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p474", "--owner-user", "p474-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34474", "--output-dir", f"{work}/out", *extra]
                given = f"dataclasses.replace(build(*a, **k), role_accounts={ROLE_ACCOUNTS!r}, roles_group='p474-roles')" if roles else "build(*a, **k)"
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                pin = "import os; os.environ['TICKET_BOARD_PYTHON'] = '/p474/python'; "
                if mode == "script":
                    probe = (pin + f"import dataclasses, runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd474_script'); main = g['main']; build = main.__globals__['build_plan']; "
                             f"main.__globals__['build_plan'] = lambda *a, **k: {given}; raise SystemExit(main({argv!r}))")
                else:
                    probe = (pin + "import dataclasses, scripts.ticket_board.project_provision as pp; build = pp.build_plan; "
                             f"pp.build_plan = lambda *a, **k: {given}; raise SystemExit(pp.main({argv!r}))")
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES[variant],
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            commands = outputs["package"][2].get("operator-commands.sh", b"").decode()
            check(commands.count('"$provision_dir"/') >= 4 and commands.startswith("#!"), f"{variant}: the packet addresses its companions from its own directory")
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
    def value(label):
        return GOLDEN[label]["result"].get("value")

    def calls(label):
        return GOLDEN[label]["calls"]

    packets = {label[len("packet: "):]: value(label) for label in GOLDEN if label.startswith("packet: ")}
    check(all(p["marked"]["companion"] == calls(f"packet: {k}")["packet_companion"] and p["marked"]["psql"] == calls(f"packet: {k}")["postgres_sql_file_command"]
              and p["marked"]["peer auth"] == calls(f"packet: {k}")["peer_auth_command"] == 1 and calls(f"packet: {k}")["service_user_command"] == 1
              and calls(f"packet: {k}")["owned_directory_command"] == 3 and p["marked"]["boundary begin"] == 1 for k, p in packets.items()),
          "every packet names its companions through packet_companion, runs its SQL through postgres_sql_file_command, applies peer auth once, "
          "creates the service user once, writes three owned-directory steps and one boundary fence -- each helper read on project_provision")
    check(packets["shaped"] == packets["lean"] == packets["default"] and packets["no linger"] != packets["default"]
          and packets["roles, no linger"] != packets["roles and worktrees"],
          "the workflow's roles and stages do not reach the packet; the owner's linger does")
    check(packets["control user"]["marked"]["companion"] == packets["default"]["marked"]["companion"] + 2
          and packets["roles"]["lines"] > packets["default"]["lines"] and packets["pgu"]["marked"]["psql"] == packets["default"]["marked"]["psql"] - 1
          and packets["two commit stores"]["sha256"] != packets["default"]["sha256"]
          and packets["stores under the home"]["sha256"] not in (packets["default"]["sha256"], packets["two commit stores"]["sha256"]),
          "a control user adds its two grant files, role accounts add their steps, pgu has no separate database step, and every commit store is granted")
    check(value("constant") == '"$provision_dir"' and value("companion: 'a.conf'") == '"$provision_dir"/\'a.conf\''
          and GOLDEN["companion: '/abs.sql'"]["result"]["raised"] == "SystemExit" and "not a path" in GOLDEN["companion: '/abs.sql'"]["result"]["message"]
          and GOLDEN["psql: companion path refused"]["result"]["raised"] == "SystemExit",
          "a companion is a quoted file name beside the packet, addressed from its own directory; a path is refused")
    check(value("psql: plain").startswith("sudo cat '/p474/x.sql' | sudo -u postgres psql") and value("psql: plain").endswith(" -f -")
          and "'postgresql:///p474?host=/run/x' -f -" in value("psql: url") and value("psql: companion").startswith('sudo cat "$provision_dir"/')
          and calls("psql: companion") == {"packet_companion": 1},
          "the SQL is fed to psql as postgres on stdin, the database URL quoted before -f -, a companion addressed through packet_companion")
    check(all(value(f"service user: {u!r}")["marked"]["service user"] == 1 and value(f"service user: {u!r}")["lines"] == 10 for u in ("ticket_board_service", "o'brien", "")),
          "the service user is created only if missing, with a non-login shell, whatever its name")
    check(value("peer auth").startswith("sudo env PG_DATABASE='p474_ticket_board' ") and value("peer auth").endswith(" --apply-peer-auth")
          and value("owned dirs: none") == "" and value("owned dirs: duplicates").count("'/p474/a'") == 1 and " -m 0700 " in value("owned dirs: mode"),
          "peer auth runs the setup script with the plan's names; owned directories are deduplicated, none is no command, and the mode is honoured")
    for name, (plan, _) in REBINDS.items():
        got = value(f"rebound on project_provision: {name}")
        reached = calls(f"rebound on project_provision: {name}")
        check(got != packets[plan] and (not callable(REBINDS[name][1]) or reached.get(f"{name} rebound", 0) > 0),
              f"{name}, rebound on project_provision, changes the packet (and is called, if a function): read there when the packet renders")


def test_every_seam_is_reached() -> None:
    # Every packet helper the six read on project_provision is a recorder there; every other seam is either rebound by its
    # own case above or an earlier slice's command the packet itself renders, reachable on project_provision.
    names = {name for reads in SEAMS.values() for name in reads}
    others = names - set(PASSED) - set(REBINDS)
    check(set(PASSED) <= REACHED and set(PASSED) <= names and all(hasattr(t, n) for n in others),
          f"a recorder on project_provision reached every packet helper, and every other seam is project_provision's: missing {sorted(set(PASSED) - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_the_packet_writers_render_through_project_provision", "test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_keeps_what_they_read",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_operator_packet_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
