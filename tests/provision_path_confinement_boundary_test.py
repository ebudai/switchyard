#!/usr/bin/env python3
"""SYRD-464: the provisioning path confinement, against project_provision it came out of.

The sixteen -- the containment checks (`_is_within`,
`_refuse_prefix_coincidence`, `_interior_directories`), the confinement
commands for the owner home, source tree, commit store, repository copies,
worktrees, retired socket group, role worktrees and the director's control
directory, their four modes (`TENANT_SOURCE_MODE`, `INHERITED_WORKTREE_CLOSURE`,
`REPOSITORY_COPY_MODE`, `INTERIOR_DIRECTORY_MODE`) and `owned_ancestor_dirs` --
moved unchanged into `scripts/ticket_board/provision_path_confinement.py`;
`project_provision` re-exports them all, in both branches of its import block,
and keeps the refusal (`PathContainmentError`), its normal-form check
(`_refuse_unnormalized`), the quoting and their callers. This pins what makes
that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads only its package, never `project_provision`. The refusal and its
  check are still `project_provision`'s own.
- **Seams (rule 24):** what they read of `project_provision` -- each other,
  `shell_quote`, `_refuse_unnormalized`, `PathContainmentError` and the four
  modes -- is read through it when they run, with the direct-script fallback,
  so a patch there reaches them.
- **Readers:** `render_operator_commands` and `role_account_commands` name them
  as before, and every production module imports them from `project_provision`,
  as often as before.
- **The behaviour is the baseline's** over synthetic POSIX paths: containment
  by components, prefix coincidences refused, unnormalized and relative paths
  refused, interior directories, and every confinement command and its
  quoting. `GOLDEN` below was produced by running the BASELINE module's own
  definitions over the very cases embedded here (`gold464.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME, USER
  and COLUMNS, under umask 077, under several hash seeds and with another TMPDIR
  and locale.
- **The direct script and the package render the same packet,** byte for
  byte, for a synthetic owner; and **a refusal keeps its class, message and
  exit status** -- only the class name an uncaught refusal prints from the
  direct script is qualified by the fallback module (`REFUSAL_PRINTED`).

No real home, tenant or account is read or written: every path is synthetic,
and the packets are rendered in a test-owned directory. Spawns, every exec,
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

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_path_confinement as m  # noqa: E402

CHECKS = 0
MOVED = ('_is_within', '_refuse_prefix_coincidence', '_interior_directories', 'owner_home_traversal_commands', 'TENANT_SOURCE_MODE', 'INHERITED_WORKTREE_CLOSURE', 'tenant_source_confinement_commands', 'REPOSITORY_COPY_MODE', 'commit_store_read_commands', 'INTERIOR_DIRECTORY_MODE', 'repository_copy_confinement_commands', 'tenant_worktree_confinement_commands', 'socket_group_retirement_commands', 'role_worktree_access_commands', 'director_control_access_commands', 'owned_ancestor_dirs')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    '_refuse_prefix_coincidence': {'PathContainmentError': 1, '_is_within': 1, '_refuse_unnormalized': 2},
    '_interior_directories': {'_is_within': 1, '_refuse_prefix_coincidence': 1},
    'owner_home_traversal_commands': {'_refuse_unnormalized': 1, 'shell_quote': 1},
    'tenant_source_confinement_commands': {'TENANT_SOURCE_MODE': 1, '_refuse_prefix_coincidence': 1, '_refuse_unnormalized': 2, 'owned_ancestor_dirs': 1, 'shell_quote': 2},
    'commit_store_read_commands': {'_interior_directories': 1, '_is_within': 1, '_refuse_prefix_coincidence': 1, '_refuse_unnormalized': 2, 'owner_home_traversal_commands': 1, 'shell_quote': 2},
    'repository_copy_confinement_commands': {'INTERIOR_DIRECTORY_MODE': 1, 'REPOSITORY_COPY_MODE': 1, '_interior_directories': 1, '_is_within': 1, '_refuse_prefix_coincidence': 1, '_refuse_unnormalized': 2, 'shell_quote': 2},
    'tenant_worktree_confinement_commands': {'INHERITED_WORKTREE_CLOSURE': 1, 'TENANT_SOURCE_MODE': 2, '_is_within': 1, '_refuse_prefix_coincidence': 1, '_refuse_unnormalized': 3, 'owned_ancestor_dirs': 1, 'shell_quote': 7},
    'socket_group_retirement_commands': {'_is_within': 1, '_refuse_unnormalized': 2, 'shell_quote': 3},
    'role_worktree_access_commands': {'_interior_directories': 2, '_is_within': 2, '_refuse_unnormalized': 3, 'owner_home_traversal_commands': 1, 'shell_quote': 5},
    'director_control_access_commands': {'_interior_directories': 1, '_is_within': 2, '_refuse_prefix_coincidence': 1, '_refuse_unnormalized': 3, 'owner_home_traversal_commands': 1, 'shell_quote': 4},
}
#: Measured on the baseline: every project_provision definition outside the sixteen that names them, and how often.
DISPATCH = {'role_account_commands': {'role_worktree_access_commands': 1}, 'render_operator_commands': {'owned_ancestor_dirs': 4, 'repository_copy_confinement_commands': 1, 'tenant_worktree_confinement_commands': 1, 'socket_group_retirement_commands': 1, 'role_worktree_access_commands': 1, 'tenant_source_confinement_commands': 1, 'commit_store_read_commands': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/owner_preparation.py': {'import TENANT_SOURCE_MODE': 1, 'import owned_ancestor_dirs': 1}, 'scripts/role_account_migration.py': {'import role_worktree_access_commands': 1, 'import commit_store_read_commands': 1, 'import repository_copy_confinement_commands': 1, 'import director_control_access_commands': 1, 'import tenant_source_confinement_commands': 1}}
#: The BASELINE's own behaviour for the cases below (`gold464.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'within: a child': {'result': {'type': 'bool', 'value': True}, 'calls': []},
    'within: itself': {'result': {'type': 'bool', 'value': True}, 'calls': []},
    'within: a prefix that coincides': {'result': {'type': 'bool', 'value': False}, 'calls': []},
    'within: a relative candidate': {'result': {'type': 'bool', 'value': False}, 'calls': []},
    'within: trailing slashes': {'result': {'type': 'bool', 'value': True}, 'calls': []},
    'within: an empty root': {'result': {'type': 'bool', 'value': False}, 'calls': []},
    'within: the root of the tree': {'result': {'type': 'bool', 'value': True}, 'calls': []},
    'prefix: inside': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/a'], {}]]},
    'prefix: elsewhere': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/other'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/other'], {}]]},
    'prefix: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex/a is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/a'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex/a'], {}]]},
    'prefix: a trailing-slash root does not share the prefix': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['_refuse_unnormalized', ['/p464/home/'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home2'], {'what': 'the granted path'}], ['_is_within', ['/p464/home/', '/p464/home2'], {}]]},
    'prefix: no root': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['_refuse_unnormalized', [''], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex'], {'what': 'the granted path'}]]},
    'prefix: no candidate': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', [''], {'what': 'the granted path'}]]},
    'prefix: an unnormalized root': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home /p464/home/../home is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home/../home'], {'what': 'the owner home'}]]},
    'prefix: a relative candidate': {'result': {'raised': 'PathContainmentError', 'message': 'the granted path home/a is not an absolute path; refusing to grant access to it'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['home/a'], {'what': 'the granted path'}]]},
    'prefix: the refusal class rebound on project_provision': {'result': {'raised': 'P464Refusal', 'message': '/p464/homex is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex'], {}]]},
    'prefix: the containment test rebound on project_provision': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex'], {'what': 'the granted path'}], ['_is_within rebound', ['/p464/home', '/p464/homex'], {}]]},
    'interior: a deep leaf': {'result': {'type': 'list', 'value': ['/p464/home/a', '/p464/home/a/b']}, 'calls': [['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/a/b/c'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a/b/c'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/a/b/c'], {}], ['_is_within', ['/p464/home', '/p464/home/a/b/c'], {}]]},
    'interior: a direct child': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/a'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/a'], {}], ['_is_within', ['/p464/home', '/p464/home/a'], {}]]},
    'interior: the root itself': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_prefix_coincidence', ['/p464/home', '/p464/home'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home'], {}], ['_is_within', ['/p464/home', '/p464/home'], {}]]},
    'interior: elsewhere': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_prefix_coincidence', ['/p464/home', '/p464/other/a/b'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/other/a/b'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/other/a/b'], {}], ['_is_within', ['/p464/home', '/p464/other/a/b'], {}]]},
    'interior: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex/a/b is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_prefix_coincidence', ['/p464/home', '/p464/homex/a/b'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/a/b'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex/a/b'], {}]]},
    'traversal: the owner home': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-svc:--x '/p464/home'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'traversal: a home that needs quoting': {'result': {'type': 'list', 'value': ['sudo setfacl -m g:p464-roles:--x \'/p464/it\'"\'"\'s home\'']}, 'calls': [['_refuse_unnormalized', ["/p464/it's home"], {'what': 'the owner home'}]]},
    'traversal: no home': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-svc:--x ''"]}, 'calls': [['_refuse_unnormalized', [''], {'what': 'the owner home'}]]},
    'traversal: an unnormalized home': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home /p464/home/.. is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home/..'], {'what': 'the owner home'}]]},
    'traversal: a relative home': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home home is not an absolute path; refusing to grant access to it'}, 'calls': [['_refuse_unnormalized', ['home'], {'what': 'the owner home'}]]},
    'traversal: the quoting rebound on project_provision': {'result': {'type': 'list', 'value': ['sudo setfacl -m u:p464-svc:--x </p464/home>']}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'source: a checkout two levels down': {'result': {'type': 'list', 'value': ["sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/Projects'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/Projects/p464'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/Projects/p464'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/Projects/p464'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/Projects/p464'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/Projects/p464'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/Projects/p464'], {'include_target': True}]]},
    'source: the checkout is the home': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home'], {'include_target': True}]]},
    'source: a checkout outside the home': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/p464'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/p464'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/p464'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/p464'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/srv/p464'], {'include_target': True}]]},
    'source: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex/p464 is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/p464'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/homex/p464'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/p464'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex/p464'], {}]]},
    'source: an unnormalized checkout': {'result': {'raised': 'PathContainmentError', 'message': 'the project checkout /p464/home/a/../b is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a/../b'], {'what': 'the project checkout'}]]},
    'source: an owner that needs quoting': {'result': {'type': 'list', 'value': ["sudo install -d -m 0750 -o 'p464 agent' -g 'p464 agent' '/p464/home/p'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/p'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/p'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/p'], {'include_target': True}]]},
    'source: the mode rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0464 -o 'p464-agent' -g 'p464-agent' '/p464/home/p'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/p'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/p'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/p'], {'include_target': True}]]},
    'source: the ancestors rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/rebound'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p'], {'what': 'the project checkout'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/p'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/p'], {}], ['owned_ancestor_dirs rebound', ['/p464/home', '/p464/home/p'], {}]]},
    'store: a store three levels down': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-svc:--x '/p464/home'", "sudo setfacl -m u:p464-svc:--x '/p464/home/git'", "sudo setfacl -m u:p464-svc:--x '/p464/home/git/p464'", "sudo setfacl -R -m u:p464-svc:rX '/p464/home/git/p464/store.git'", "sudo setfacl -R -m d:u:p464-svc:rX '/p464/home/git/p464/store.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/p464/store.git'], {'what': 'the commit store'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/p464/store.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/p464/store.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/p464/store.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/p464/store.git'], {}], ['owner_home_traversal_commands', ['/p464/home', 'u:p464-svc'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_interior_directories', ['/p464/home', '/p464/home/git/p464/store.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/p464/store.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/p464/store.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/p464/store.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/p464/store.git'], {}]]},
    'store: a direct child': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-svc:--x '/p464/home'", "sudo setfacl -R -m u:p464-svc:rX '/p464/home/store.git'", "sudo setfacl -R -m d:u:p464-svc:rX '/p464/home/store.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/store.git'], {'what': 'the commit store'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/store.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/store.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/store.git'], {}], ['_is_within', ['/p464/home', '/p464/home/store.git'], {}], ['owner_home_traversal_commands', ['/p464/home', 'u:p464-svc'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_interior_directories', ['/p464/home', '/p464/home/store.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/store.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/store.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/store.git'], {}], ['_is_within', ['/p464/home', '/p464/home/store.git'], {}]]},
    'store: the home itself': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/'], {'what': 'the commit store'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/'], {}], ['_is_within', ['/p464/home', '/p464/home/'], {}]]},
    'store: outside the home': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/store.git'], {'what': 'the commit store'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/store.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/store.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/store.git'], {}], ['_is_within', ['/p464/home', '/p464/srv/store.git'], {}]]},
    'store: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/home2/store.git is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home2/store.git'], {'what': 'the commit store'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home2/store.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home2/store.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home2/store.git'], {}]]},
    'store: an unnormalized store': {'result': {'raised': 'PathContainmentError', 'message': 'the commit store /p464/home/x/../store.git is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/x/../store.git'], {'what': 'the commit store'}]]},
    'store: the traversal rebound on project_provision': {'result': {'type': 'list', 'value': ['# p464 traversal', "sudo setfacl -m u:p464-svc:--x '/p464/home/g'", "sudo setfacl -R -m u:p464-svc:rX '/p464/home/g/s.git'", "sudo setfacl -R -m d:u:p464-svc:rX '/p464/home/g/s.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/g/s.git'], {'what': 'the commit store'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/g/s.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/g/s.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/g/s.git'], {}], ['_is_within', ['/p464/home', '/p464/home/g/s.git'], {}], ['owner_home_traversal_commands rebound', ['/p464/home', 'u:p464-svc'], {}], ['_interior_directories', ['/p464/home', '/p464/home/g/s.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/g/s.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/g/s.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/g/s.git'], {}], ['_is_within', ['/p464/home', '/p464/home/g/s.git'], {}]]},
    'copies: inside, outside, empty and shared parents': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o 'p464-agent' -g 'p464-agent' '/p464/home/git'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/git/a.git'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/git/c.git'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/d.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/srv/b.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/b.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/b.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/b.git'], {}], ['_is_within', ['/p464/home', '/p464/srv/b.git'], {}], ['_refuse_unnormalized', ['/p464/home/git/c.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/c.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/c.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/c.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/c.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/git/c.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/c.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/c.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/c.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/c.git'], {}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home/d.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/d.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/d.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/d.git'], {}], ['_is_within', ['/p464/home', '/p464/home/d.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/d.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/d.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/d.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/d.git'], {}], ['_is_within', ['/p464/home', '/p464/home/d.git'], {}]]},
    'copies: a mode given': {'result': {'type': 'list', 'value': ["sudo install -d -m 0755 -o 'p464-agent' -g 'p464-agent' '/p464/home/git'", "sudo install -d -m 0700 -o 'p464-agent' -g 'p464-agent' '/p464/home/git/a.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}]]},
    'copies: the home itself skipped': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/'], {}], ['_is_within', ['/p464/home', '/p464/home/'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home'], {}], ['_is_within', ['/p464/home', '/p464/home'], {}]]},
    'copies: none': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'copies: an unnormalized copy': {'result': {'raised': 'PathContainmentError', 'message': 'a repository copy /p464/home/../x.git is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/a.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/a.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/a.git'], {}], ['_refuse_unnormalized', ['/p464/home/../x.git'], {'what': 'a repository copy'}]]},
    'copies: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex/a.git is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/a.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/homex/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex/a.git'], {}]]},
    'copies: an unnormalized home': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home home is not an absolute path; refusing to grant access to it'}, 'calls': [['_refuse_unnormalized', ['home'], {'what': 'the owner home'}]]},
    'copies: the modes rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0462 -o 'p464-agent' -g 'p464-agent' '/p464/home/git'", "sudo install -d -m 0461 -o 'p464-agent' -g 'p464-agent' '/p464/home/git/a.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'a repository copy'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_interior_directories', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/git/a.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/git/a.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}], ['_is_within', ['/p464/home', '/p464/home/git/a.git'], {}]]},
    'worktrees: a base with worktrees inside, outside, empty and repeated': {'result': {'type': 'list', 'value': ["sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt/p464'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt/p464/main'", "sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt/p464/audit'", "if [ -d '/p464/home/wt/p464' ]; then", "    sudo find '/p464/home/wt/p464' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +", "    sudo setfacl -m 'd:u::rwx,d:g::r-x,d:o::---' '/p464/home/wt/p464'", 'fi']}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/p464'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt/p464'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/p464'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt/p464'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/wt/p464'], {'include_target': True}], ['_refuse_unnormalized', ['/p464/home/wt/p464/main'], {'what': 'a role worktree'}], ['_is_within', ['/p464/home/wt/p464', '/p464/home/wt/p464/main'], {}], ['_refuse_unnormalized', ['/p464/srv/wt'], {'what': 'a role worktree'}], ['_is_within', ['/p464/home/wt/p464', '/p464/srv/wt'], {}], ['_refuse_unnormalized', ['/p464/home/wt/p464/main'], {'what': 'a role worktree'}], ['_is_within', ['/p464/home/wt/p464', '/p464/home/wt/p464/main'], {}], ['_refuse_unnormalized', ['/p464/home/wt/p464/audit'], {'what': 'a role worktree'}], ['_is_within', ['/p464/home/wt/p464', '/p464/home/wt/p464/audit'], {}]]},
    'worktrees: a base with none': {'result': {'type': 'list', 'value': ["sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt'", "if [ -d '/p464/home/wt' ]; then", "    sudo find '/p464/home/wt' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +", "    sudo setfacl -m 'd:u::rwx,d:g::r-x,d:o::---' '/p464/home/wt'", 'fi']}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/wt'], {'include_target': True}]]},
    'worktrees: the base is the home': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home'], {'include_target': True}]]},
    'worktrees: a base outside the home': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/wt'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/wt'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/srv/wt'], {'include_target': True}]]},
    'worktrees: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex/wt is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/wt'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/homex/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex/wt'], {}]]},
    'worktrees: an unnormalized worktree': {'result': {'raised': 'PathContainmentError', 'message': 'a role worktree /p464/home/wt/../x is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/wt'], {'include_target': True}], ['_refuse_unnormalized', ['/p464/home/wt/../x'], {'what': 'a role worktree'}]]},
    'worktrees: the closure and mode rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -d -m 0464 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt'", "sudo install -d -m 0464 -o 'p464-agent' -g 'p464-agent' '/p464/home/wt/a'", "if [ -d '/p464/home/wt' ]; then", "    sudo find '/p464/home/wt' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +", "    sudo setfacl -m 'd:u::rwx' '/p464/home/wt'", 'fi']}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the worktree base'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['owned_ancestor_dirs', ['/p464/home', '/p464/home/wt'], {'include_target': True}], ['_refuse_unnormalized', ['/p464/home/wt/a'], {'what': 'a role worktree'}], ['_is_within', ['/p464/home/wt', '/p464/home/wt/a'], {}]]},
    'retire: both surfaces inside the home': {'result': {'type': 'list', 'value': ["if getent group 'p464-sock' >/dev/null 2>&1; then", "    sudo setfacl -x d:g:p464-sock '/p464/home/wt'", "    sudo setfacl -x g:p464-sock '/p464/home/wt'", "    sudo setfacl -R -x d:g:p464-sock '/p464/home/ctl'", "    sudo setfacl -R -x g:p464-sock '/p464/home/ctl'", 'fi']}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'a repository surface'}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_unnormalized', ['/p464/home/ctl'], {'what': 'a repository surface'}], ['_is_within', ['/p464/home', '/p464/home/ctl'], {}]]},
    'retire: the control repository outside': {'result': {'type': 'list', 'value': ["if getent group 'p464-sock' >/dev/null 2>&1; then", "    sudo setfacl -x d:g:p464-sock '/p464/home/wt'", "    sudo setfacl -x g:p464-sock '/p464/home/wt'", 'fi']}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'a repository surface'}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_unnormalized', ['/p464/srv/ctl'], {'what': 'a repository surface'}], ['_is_within', ['/p464/home', '/p464/srv/ctl'], {}]]},
    'retire: both outside': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/wt'], {'what': 'a repository surface'}], ['_is_within', ['/p464/home', '/p464/srv/wt'], {}]]},
    'retire: no group': {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    "retire: the owner's own group": {'result': {'type': 'list', 'value': []}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'retire: an unnormalized surface': {'result': {'raised': 'PathContainmentError', 'message': 'a repository surface /p464/home/wt/.. is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/..'], {'what': 'a repository surface'}]]},
    'retire: an unnormalized home': {'result': {'raised': 'PathContainmentError', 'message': 'the owner home /p464/home/../h is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home/../h'], {'what': 'the owner home'}]]},
    'roles: both inside, with retired groups': {'result': {'type': 'list', 'value': ["sudo setfacl -m g:p464-repo:--x '/p464/home'", "sudo setfacl -m g:p464-repo:--x '/p464/home/wt'", "sudo setfacl -m g:p464-repo:--x '/p464/home/ctl'", "sudo setfacl -m g:p464-repo:--x '/p464/home/wt/p464'", "sudo setfacl -R -m g:p464-repo:rwX '/p464/home/ctl/p464.git'", "sudo setfacl -R -m d:g:p464-repo:rwX '/p464/home/ctl/p464.git'", "sudo setfacl -R -x d:g:p464-old '/p464/home/ctl/p464.git'", "sudo setfacl -R -x g:p464-old '/p464/home/ctl/p464.git'", "sudo setfacl -R -x d:g:p464-older '/p464/home/ctl/p464.git'", "sudo setfacl -R -x g:p464-older '/p464/home/ctl/p464.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/p464'], {'what': 'the worktree base'}], ['_refuse_unnormalized', ['/p464/home/ctl/p464.git'], {'what': 'the control repository'}], ['_interior_directories', ['/p464/home', '/p464/home/wt/p464'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt/p464'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/p464'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt/p464'], {}], ['_is_within', ['/p464/home', '/p464/home/wt/p464'], {}], ['_interior_directories', ['/p464/home', '/p464/home/ctl/p464.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/ctl/p464.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/ctl/p464.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/ctl/p464.git'], {}], ['_is_within', ['/p464/home', '/p464/home/ctl/p464.git'], {}], ['_is_within', ['/p464/home', '/p464/home/wt/p464'], {}], ['owner_home_traversal_commands', ['/p464/home', 'g:p464-repo'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'roles: a base directly in the home': {'result': {'type': 'list', 'value': ["sudo setfacl -m g:p464-repo:--x '/p464/home'", "sudo setfacl -m g:p464-repo:--x '/p464/home/wt'", "sudo setfacl -R -m g:p464-repo:rwX '/p464/home/wt/ctl.git'", "sudo setfacl -R -m d:g:p464-repo:rwX '/p464/home/wt/ctl.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the worktree base'}], ['_refuse_unnormalized', ['/p464/home/wt/ctl.git'], {'what': 'the control repository'}], ['_interior_directories', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['_interior_directories', ['/p464/home', '/p464/home/wt/ctl.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/wt/ctl.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/ctl.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/wt/ctl.git'], {}], ['_is_within', ['/p464/home', '/p464/home/wt/ctl.git'], {}], ['_is_within', ['/p464/home', '/p464/home/wt'], {}], ['owner_home_traversal_commands', ['/p464/home', 'g:p464-repo'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'roles: both outside the home': {'result': {'type': 'list', 'value': ["sudo setfacl -m g:p464-repo:--x '/p464/srv/wt'", "sudo setfacl -R -m g:p464-repo:rwX '/p464/srv/ctl.git'", "sudo setfacl -R -m d:g:p464-repo:rwX '/p464/srv/ctl.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/wt'], {'what': 'the worktree base'}], ['_refuse_unnormalized', ['/p464/srv/ctl.git'], {'what': 'the control repository'}], ['_interior_directories', ['/p464/home', '/p464/srv/wt'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/wt'], {}], ['_is_within', ['/p464/home', '/p464/srv/wt'], {}], ['_interior_directories', ['/p464/home', '/p464/srv/ctl.git'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/ctl.git'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/ctl.git'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/ctl.git'], {}], ['_is_within', ['/p464/home', '/p464/srv/ctl.git'], {}], ['_is_within', ['/p464/home', '/p464/srv/wt'], {}], ['_is_within', ['/p464/home', '/p464/srv/ctl.git'], {}]]},
    'roles: an unnormalized control repository': {'result': {'raised': 'PathContainmentError', 'message': 'the control repository /p464/home/ctl/../x is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt'], {'what': 'the worktree base'}], ['_refuse_unnormalized', ['/p464/home/ctl/../x'], {'what': 'the control repository'}]]},
    'roles: a coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex/wt is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/wt'], {'what': 'the worktree base'}], ['_refuse_unnormalized', ['/p464/home/ctl'], {'what': 'the control repository'}], ['_interior_directories', ['/p464/home', '/p464/homex/wt'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/homex/wt'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex/wt'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex/wt'], {}]]},
    'roles: the interior rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo setfacl -m g:p464-repo:--x '/p464/home'", "sudo setfacl -m g:p464-repo:--x '/p464/interior'", "sudo setfacl -m g:p464-repo:--x '/p464/home/wt/p'", "sudo setfacl -R -m g:p464-repo:rwX '/p464/home/c/p.git'", "sudo setfacl -R -m d:g:p464-repo:rwX '/p464/home/c/p.git'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/wt/p'], {'what': 'the worktree base'}], ['_refuse_unnormalized', ['/p464/home/c/p.git'], {'what': 'the control repository'}], ['_interior_directories rebound', ['/p464/home', '/p464/home/wt/p'], {}], ['_interior_directories rebound', ['/p464/home', '/p464/home/c/p.git'], {}], ['_is_within', ['/p464/home', '/p464/home/wt/p'], {}], ['owner_home_traversal_commands', ['/p464/home', 'g:p464-repo'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}]]},
    'director: provisioning and project inside': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-director:--x '/p464/home'", "sudo setfacl -m u:p464-director:--x '/p464/home/.local'", "sudo setfacl -m u:p464-director:--x '/p464/home/.local/state'", "sudo setfacl -m u:p464-director:--x '/p464/home/.local/state/p464'", "sudo setfacl -R -m u:p464-director:rwX '/p464/home/.local/state/p464/provision'", "sudo setfacl -R -m d:u:p464-director:rwX '/p464/home/.local/state/p464/provision'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/.local/state/p464/provision'], {'what': 'the provisioning directory'}], ['_refuse_unnormalized', ['/p464/home/.local/state/p464'], {'what': 'the project directory'}], ['_interior_directories', ['/p464/home', '/p464/home/.local/state/p464/provision'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/.local/state/p464/provision'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/.local/state/p464/provision'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/.local/state/p464/provision'], {}], ['_is_within', ['/p464/home', '/p464/home/.local/state/p464/provision'], {}], ['_is_within', ['/p464/home', '/p464/home/.local/state/p464/provision'], {}], ['owner_home_traversal_commands', ['/p464/home', 'u:p464-director'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/.local/state/p464'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/.local/state/p464'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/.local/state/p464'], {}], ['_is_within', ['/p464/home', '/p464/home/.local/state/p464'], {}]]},
    'director: a project directly in the home': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-director:--x '/p464/home'", "sudo setfacl -m u:p464-director:--x '/p464/home/p'", "sudo setfacl -m u:p464-director:--x '/p464/home/q'", "sudo setfacl -R -m u:p464-director:rwX '/p464/home/p/prov'", "sudo setfacl -R -m d:u:p464-director:rwX '/p464/home/p/prov'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the provisioning directory'}], ['_refuse_unnormalized', ['/p464/home/q'], {'what': 'the project directory'}], ['_interior_directories', ['/p464/home', '/p464/home/p/prov'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/p/prov'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['owner_home_traversal_commands', ['/p464/home', 'u:p464-director'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/q'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/q'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/q'], {}], ['_is_within', ['/p464/home', '/p464/home/q'], {}]]},
    'director: no project': {'result': {'type': 'list', 'value': ["sudo setfacl -m u:p464-director:--x '/p464/home'", "sudo setfacl -m u:p464-director:--x '/p464/home/p'", "sudo setfacl -R -m u:p464-director:rwX '/p464/home/p/prov'", "sudo setfacl -R -m d:u:p464-director:rwX '/p464/home/p/prov'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the provisioning directory'}], ['_refuse_unnormalized', [''], {'what': 'the project directory'}], ['_interior_directories', ['/p464/home', '/p464/home/p/prov'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/p/prov'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['owner_home_traversal_commands', ['/p464/home', 'u:p464-director'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_prefix_coincidence', ['/p464/home', ''], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', [''], {'what': 'the granted path'}]]},
    'director: both outside the home': {'result': {'type': 'list', 'value': ["sudo setfacl -R -m u:p464-director:rwX '/p464/srv/prov'", "sudo setfacl -R -m d:u:p464-director:rwX '/p464/srv/prov'"]}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/prov'], {'what': 'the provisioning directory'}], ['_refuse_unnormalized', ['/p464/srv'], {'what': 'the project directory'}], ['_interior_directories', ['/p464/home', '/p464/srv/prov'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv/prov'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv/prov'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv/prov'], {}], ['_is_within', ['/p464/home', '/p464/srv/prov'], {}], ['_is_within', ['/p464/home', '/p464/srv/prov'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/srv'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/srv'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/srv'], {}], ['_is_within', ['/p464/home', '/p464/srv'], {}]]},
    'director: a project coincidence refused': {'result': {'raised': 'PathContainmentError', 'message': '/p464/homex is not inside /p464/home; refusing to grant access to a path that only shares its prefix'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the provisioning directory'}], ['_refuse_unnormalized', ['/p464/homex'], {'what': 'the project directory'}], ['_interior_directories', ['/p464/home', '/p464/home/p/prov'], {}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/home/p/prov'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['_is_within', ['/p464/home', '/p464/home/p/prov'], {}], ['owner_home_traversal_commands', ['/p464/home', 'u:p464-director'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_prefix_coincidence', ['/p464/home', '/p464/homex'], {}], ['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/homex'], {'what': 'the granted path'}], ['_is_within', ['/p464/home', '/p464/homex'], {}]]},
    'director: an unnormalized provisioning directory': {'result': {'raised': 'PathContainmentError', 'message': 'the provisioning directory /p464/home/p/../prov is not in normal form; refusing to grant access to a path that walks back out of itself'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/../prov'], {'what': 'the provisioning directory'}]]},
    'director: a relative project': {'result': {'raised': 'PathContainmentError', 'message': 'the project directory p is not an absolute path; refusing to grant access to it'}, 'calls': [['_refuse_unnormalized', ['/p464/home'], {'what': 'the owner home'}], ['_refuse_unnormalized', ['/p464/home/p/prov'], {'what': 'the provisioning directory'}], ['_refuse_unnormalized', ['p'], {'what': 'the project directory'}]]},
    'ancestors: including the target': {'result': {'type': 'tuple', 'value': ['/p464/home/a', '/p464/home/a/b', '/p464/home/a/b/c']}, 'calls': []},
    'ancestors: excluding the target': {'result': {'type': 'tuple', 'value': ['/p464/home/a', '/p464/home/a/b']}, 'calls': []},
    'ancestors: a direct child, excluded': {'result': {'type': 'tuple', 'value': []}, 'calls': []},
    'ancestors: the home itself': {'result': {'type': 'tuple', 'value': []}, 'calls': []},
    'ancestors: outside the home': {'result': {'type': 'tuple', 'value': []}, 'calls': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts the synthetic packet has.
PACKET_FILES = 10

# --- the cases, shared verbatim with `gold464.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the twelve and records the answer or the exact exception and, in order, every call it made of
# `project_provision`'s helpers (recorded there and passed through). Every path is a synthetic POSIX string under
# /p464: the checks and command builders only compare paths and return shell text, so nothing is created, read or run.
# A case may rebind a `project_provision` name -- a constant, the quoting, the refusal class or a helper -- to show it is
# read there when the function runs.
H = "/p464/home"
CASES = {
    "within: a child": {"call": "_is_within", "args": [H, H + "/a/b"]},
    "within: itself": {"call": "_is_within", "args": [H, H]},
    "within: a prefix that coincides": {"call": "_is_within", "args": [H, H + "x/a"]},
    "within: a relative candidate": {"call": "_is_within", "args": [H, "p464/home/a"]},
    "within: trailing slashes": {"call": "_is_within", "args": [H + "/", H + "/a/"]},
    "within: an empty root": {"call": "_is_within", "args": ["", H]},
    "within: the root of the tree": {"call": "_is_within", "args": ["/", H]},
    "prefix: inside": {"call": "_refuse_prefix_coincidence", "args": [H, H + "/a"]},
    "prefix: elsewhere": {"call": "_refuse_prefix_coincidence", "args": [H, "/p464/other"]},
    "prefix: a coincidence refused": {"call": "_refuse_prefix_coincidence", "args": [H, H + "x/a"]},
    "prefix: a trailing-slash root does not share the prefix": {"call": "_refuse_prefix_coincidence", "args": [H + "/", H + "2"]},
    "prefix: no root": {"call": "_refuse_prefix_coincidence", "args": ["", H + "x"]},
    "prefix: no candidate": {"call": "_refuse_prefix_coincidence", "args": [H, ""]},
    "prefix: an unnormalized root": {"call": "_refuse_prefix_coincidence", "args": [H + "/../home", H + "/a"]},
    "prefix: a relative candidate": {"call": "_refuse_prefix_coincidence", "args": [H, "home/a"]},
    "prefix: the refusal class rebound on project_provision": {"call": "_refuse_prefix_coincidence", "args": [H, H + "x"], "rebind": {"PathContainmentError": True}},
    "prefix: the containment test rebound on project_provision": {"call": "_refuse_prefix_coincidence", "args": [H, H + "x"], "rebind": {"_is_within": True}},
    "interior: a deep leaf": {"call": "_interior_directories", "args": [H, H + "/a/b/c"]},
    "interior: a direct child": {"call": "_interior_directories", "args": [H, H + "/a"]},
    "interior: the root itself": {"call": "_interior_directories", "args": [H, H]},
    "interior: elsewhere": {"call": "_interior_directories", "args": [H, "/p464/other/a/b"]},
    "interior: a coincidence refused": {"call": "_interior_directories", "args": [H, H + "x/a/b"]},
    "traversal: the owner home": {"call": "owner_home_traversal_commands", "args": [H, "u:p464-svc"]},
    "traversal: a home that needs quoting": {"call": "owner_home_traversal_commands", "args": ["/p464/it's home", "g:p464-roles"]},
    "traversal: no home": {"call": "owner_home_traversal_commands", "args": ["", "u:p464-svc"]},
    "traversal: an unnormalized home": {"call": "owner_home_traversal_commands", "args": [H + "/..", "u:p464-svc"]},
    "traversal: a relative home": {"call": "owner_home_traversal_commands", "args": ["home", "u:p464-svc"]},
    "traversal: the quoting rebound on project_provision": {"call": "owner_home_traversal_commands", "args": [H, "u:p464-svc"], "rebind": {"shell_quote": True}},
    "source: a checkout two levels down": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": H + "/Projects/p464"}},
    "source: the checkout is the home": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": H}},
    "source: a checkout outside the home": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": "/p464/srv/p464"}},
    "source: a coincidence refused": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": H + "x/p464"}},
    "source: an unnormalized checkout": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": H + "/a/../b"}},
    "source: an owner that needs quoting": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464 agent", "owner_home": H, "checkout": H + "/p"}},
    "source: the mode rebound on project_provision": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": H + "/p"},
                                                      "rebind": {"TENANT_SOURCE_MODE": "0464"}},
    "source: the ancestors rebound on project_provision": {"call": "tenant_source_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "checkout": H + "/p"},
                                                           "rebind": {"owned_ancestor_dirs": True}},
    "store: a store three levels down": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": H + "/git/p464/store.git"}},
    "store: a direct child": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": H + "/store.git"}},
    "store: the home itself": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": H + "/"}},
    "store: outside the home": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": "/p464/srv/store.git"}},
    "store: a coincidence refused": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": H + "2/store.git"}},
    "store: an unnormalized store": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": H + "/x/../store.git"}},
    "store: the traversal rebound on project_provision": {"call": "commit_store_read_commands", "kwargs": {"owner_home": H, "service_user": "p464-svc", "commit_git_dir": H + "/g/s.git"},
                                                          "rebind": {"owner_home_traversal_commands": True}},
    "copies: inside, outside, empty and shared parents": {"call": "repository_copy_confinement_commands",
                                                          "kwargs": {"owner_user": "p464-agent", "owner_home": H,
                                                                     "repositories": [H + "/git/a.git", "", "/p464/srv/b.git", H + "/git/c.git", H + "/git/a.git", H + "/d.git"]}},
    "copies: a mode given": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "repositories": [H + "/git/a.git"], "mode": "0700"}},
    "copies: the home itself skipped": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "repositories": [H + "/", H]}},
    "copies: none": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "repositories": []}},
    "copies: an unnormalized copy": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "repositories": [H + "/a.git", H + "/../x.git"]}},
    "copies: a coincidence refused": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "repositories": [H + "x/a.git"]}},
    "copies: an unnormalized home": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": "home", "repositories": []}},
    "copies: the modes rebound on project_provision": {"call": "repository_copy_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "repositories": [H + "/git/a.git"]},
                                                       "rebind": {"REPOSITORY_COPY_MODE": "0461", "INTERIOR_DIRECTORY_MODE": "0462"}},
    "worktrees: a base with worktrees inside, outside, empty and repeated": {"call": "tenant_worktree_confinement_commands",
                                                                           "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": H + "/wt/p464",
                                                                                      "worktrees": [H + "/wt/p464/main", "", "/p464/srv/wt", H + "/wt/p464/main", H + "/wt/p464/audit"]}},
    "worktrees: a base with none": {"call": "tenant_worktree_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": H + "/wt"}},
    "worktrees: the base is the home": {"call": "tenant_worktree_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": H, "worktrees": [H + "/a"]}},
    "worktrees: a base outside the home": {"call": "tenant_worktree_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": "/p464/srv/wt"}},
    "worktrees: a coincidence refused": {"call": "tenant_worktree_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": H + "x/wt"}},
    "worktrees: an unnormalized worktree": {"call": "tenant_worktree_confinement_commands", "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": H + "/wt",
                                                                                                    "worktrees": [H + "/wt/../x"]}},
    "worktrees: the closure and mode rebound on project_provision": {"call": "tenant_worktree_confinement_commands",
                                                                     "kwargs": {"owner_user": "p464-agent", "owner_home": H, "worktree_base": H + "/wt", "worktrees": [H + "/wt/a"]},
                                                                     "rebind": {"INHERITED_WORKTREE_CLOSURE": "d:u::rwx", "TENANT_SOURCE_MODE": "0464"}},
    "retire: both surfaces inside the home": {"call": "socket_group_retirement_commands",
                                              "kwargs": {"owner_user": "p464-agent", "owner_home": H, "socket_group": " p464-sock ", "worktree_base": H + "/wt", "control_repository": H + "/ctl"}},
    "retire: the control repository outside": {"call": "socket_group_retirement_commands",
                                               "kwargs": {"owner_user": "p464-agent", "owner_home": H, "socket_group": "p464-sock", "worktree_base": H + "/wt", "control_repository": "/p464/srv/ctl"}},
    "retire: both outside": {"call": "socket_group_retirement_commands",
                             "kwargs": {"owner_user": "p464-agent", "owner_home": H, "socket_group": "p464-sock", "worktree_base": "/p464/srv/wt", "control_repository": ""}},
    "retire: no group": {"call": "socket_group_retirement_commands",
                         "kwargs": {"owner_user": "p464-agent", "owner_home": H, "socket_group": "  ", "worktree_base": H + "/wt", "control_repository": H + "/ctl"}},
    "retire: the owner's own group": {"call": "socket_group_retirement_commands",
                                      "kwargs": {"owner_user": "p464-agent", "owner_home": H, "socket_group": "p464-agent", "worktree_base": H + "/wt", "control_repository": H + "/ctl"}},
    "retire: an unnormalized surface": {"call": "socket_group_retirement_commands",
                                        "kwargs": {"owner_user": "p464-agent", "owner_home": H, "socket_group": "p464-sock", "worktree_base": H + "/wt/..", "control_repository": H + "/ctl"}},
    "retire: an unnormalized home": {"call": "socket_group_retirement_commands",
                                     "kwargs": {"owner_user": "p464-agent", "owner_home": H + "/../h", "socket_group": "", "worktree_base": "", "control_repository": ""}},
    "roles: both inside, with retired groups": {"call": "role_worktree_access_commands",
                                                "kwargs": {"owner_home": H, "repository_group": "p464-repo", "worktree_base": H + "/wt/p464", "control_repository": H + "/ctl/p464.git",
                                                           "retired_groups": ["p464-old", "", "p464-repo", "p464-older"]}},
    "roles: a base directly in the home": {"call": "role_worktree_access_commands",
                                           "kwargs": {"owner_home": H, "repository_group": "p464-repo", "worktree_base": H + "/wt", "control_repository": H + "/wt/ctl.git"}},
    "roles: both outside the home": {"call": "role_worktree_access_commands",
                                     "kwargs": {"owner_home": H, "repository_group": "p464-repo", "worktree_base": "/p464/srv/wt", "control_repository": "/p464/srv/ctl.git"}},
    "roles: an unnormalized control repository": {"call": "role_worktree_access_commands",
                                                  "kwargs": {"owner_home": H, "repository_group": "p464-repo", "worktree_base": H + "/wt", "control_repository": H + "/ctl/../x"}},
    "roles: a coincidence refused": {"call": "role_worktree_access_commands",
                                     "kwargs": {"owner_home": H, "repository_group": "p464-repo", "worktree_base": H + "x/wt", "control_repository": H + "/ctl"}},
    "roles: the interior rebound on project_provision": {"call": "role_worktree_access_commands",
                                                         "kwargs": {"owner_home": H, "repository_group": "p464-repo", "worktree_base": H + "/wt/p", "control_repository": H + "/c/p.git"},
                                                         "rebind": {"_interior_directories": True}},
    "director: provisioning and project inside": {"call": "director_control_access_commands",
                                                  "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": H + "/.local/state/p464/provision",
                                                             "project_dir": H + "/.local/state/p464"}},
    "director: a project directly in the home": {"call": "director_control_access_commands",
                                                 "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": H + "/p/prov", "project_dir": H + "/q"}},
    "director: no project": {"call": "director_control_access_commands",
                             "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": H + "/p/prov", "project_dir": ""}},
    "director: both outside the home": {"call": "director_control_access_commands",
                                        "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": "/p464/srv/prov", "project_dir": "/p464/srv"}},
    "director: a project coincidence refused": {"call": "director_control_access_commands",
                                                "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": H + "/p/prov", "project_dir": H + "x"}},
    "director: an unnormalized provisioning directory": {"call": "director_control_access_commands",
                                                         "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": H + "/p/../prov", "project_dir": H + "/p"}},
    "director: a relative project": {"call": "director_control_access_commands",
                                     "kwargs": {"owner_home": H, "director_account": "p464-director", "provision_dir": H + "/p/prov", "project_dir": "p"}},
    "ancestors: including the target": {"call": "owned_ancestor_dirs", "args": [H, H + "/a/b/c"], "kwargs": {"include_target": True}},
    "ancestors: excluding the target": {"call": "owned_ancestor_dirs", "args": [H, H + "/a/b/c"], "kwargs": {"include_target": False}},
    "ancestors: a direct child, excluded": {"call": "owned_ancestor_dirs", "args": [H, H + "/a"], "kwargs": {"include_target": False}},
    "ancestors: the home itself": {"call": "owned_ancestor_dirs", "args": [H + "/", H], "kwargs": {"include_target": True}},
    "ancestors: outside the home": {"call": "owned_ancestor_dirs", "args": [H, "/p464/srv/a"], "kwargs": {"include_target": True}},
}
FUNCTIONS = ("_is_within", "_refuse_prefix_coincidence", "_interior_directories", "owner_home_traversal_commands", "tenant_source_confinement_commands",
             "commit_store_read_commands", "repository_copy_confinement_commands", "tenant_worktree_confinement_commands", "socket_group_retirement_commands",
             "role_worktree_access_commands", "director_control_access_commands", "owned_ancestor_dirs")
PASSED = ("_refuse_unnormalized", "_is_within", "_refuse_prefix_coincidence", "_interior_directories", "owner_home_traversal_commands", "owned_ancestor_dirs")
REBINDABLE = ("TENANT_SOURCE_MODE", "INHERITED_WORKTREE_CLOSURE", "REPOSITORY_COPY_MODE", "INTERIOR_DIRECTORY_MODE", "shell_quote", "PathContainmentError")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    calls: list = []

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, (str, bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE)}
    try:
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name, *a, **k) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "_is_within":
                t._is_within = lambda root, candidate: note("_is_within rebound", root, candidate) or True
            elif name == "owned_ancestor_dirs":
                t.owned_ancestor_dirs = lambda owner_home, target, *, include_target: note("owned_ancestor_dirs rebound", owner_home, target) or ("/p464/rebound",)
            elif name == "owner_home_traversal_commands":
                t.owner_home_traversal_commands = lambda owner_home, principal: note("owner_home_traversal_commands rebound", owner_home, principal) or ["# p464 traversal"]
            elif name == "_interior_directories":
                t._interior_directories = lambda root, leaf: note("_interior_directories rebound", root, leaf) or ["/p464/interior"]
            elif name == "shell_quote":
                t.shell_quote = lambda value: "<" + str(value) + ">"
            elif name == "PathContainmentError":
                class P464Refusal(ValueError):
                    pass
                t.PathContainmentError = P464Refusal
            else:
                setattr(t, name, value)
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        try:
            got = fn(*spec.get("args", []), **spec.get("kwargs", {}))
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": str(exc)}
        else:
            result = {"type": type(got).__name__, "value": norm(got)}
        return {"result": result, "calls": calls}
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


#: What project_provision keeps and the moved code reads there when it runs, and its two callers of them.
KEPT = ("shell_quote", "_refuse_unnormalized", "PathContainmentError", "render_operator_commands", "role_account_commands")
#: The four constants, as the baseline wrote them.
CONSTANT_TEXT = {
    "TENANT_SOURCE_MODE": "'0750'",
    "INHERITED_WORKTREE_CLOSURE": "'d:u::rwx,d:g::r-x,d:o::---'",
    "REPOSITORY_COPY_MODE": "'0750'",
    "INTERIOR_DIRECTORY_MODE": "'0755'",
}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The refusal keeps its home: raised by the moved code, it is project_provision's own class.
REFUSAL_MODULE = "scripts.ticket_board.project_provision"
#: What an uncaught refusal prints on its last traceback line, measured on the candidate for an unnormalized
#: `--commit-git-dir`. Through the package the class is `REFUSAL_MODULE`'s, as on the baseline. Run as a direct script,
#: the moved code reaches project_provision through the fallback import -- a second module object beside `__main__` --
#: so the printed name is qualified `project_provision.`; the baseline printed it bare. The message and the exit status
#: are the same in both modes and on the baseline, and nothing reads the printed class name.
#: The moved confinement as the packet's operator commands carry it for the synthetic owner (HOME is its home): the worktree
#: base closed and its inherited closure, the commit store granted read-only. Expected text, compared and never run.
PACKET_CONFINEMENT = ("sudo install -d -m 0750 -o 'p464-agent' -g 'p464-agent' 'HOME/p464-worktrees'",
                      "    sudo setfacl -m 'd:u::rwx,d:g::r-x,d:o::---' 'HOME/p464-worktrees'",
                      "sudo setfacl -R -m d:u:boardsvc:rX 'HOME/.local/state/switchyard/projects/p464/control.git'")
REFUSAL_PRINTED = {"package": "scripts.ticket_board.project_provision.PathContainmentError", "script": "project_provision.PathContainmentError"}


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
    result = python("import sys, scripts.ticket_board.provision_path_confinement as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_same_defaults() -> None:
    for order in (("scripts.ticket_board.provision_path_confinement", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_path_confinement")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_path_confinement as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        "t.PathContainmentError.__module__, t._refuse_unnormalized.__module__, "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == f"True ['scripts.ticket_board.provision_path_confinement'] {REFUSAL_MODULE} {REFUSAL_MODULE} True",
              f"{' then '.join(order)}: one object each, defined here; the refusal and its normal-form check still project_provision's own; "
              f"nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    import typing
    check(m.PurePosixPath is pathlib.PurePosixPath and m.Sequence is typing.Sequence and t.PurePosixPath is m.PurePosixPath and t.Sequence is m.Sequence,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_path_confinement.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "from pathlib import PurePosixPath", "from typing import Sequence"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try))], f"the standard library only, at load: {top}")
    consts = {top_name(n): ast.unparse(n.value) for n in tree.body if isinstance(n, ast.Assign)}
    check(consts == CONSTANT_TEXT, f"the four constants are the baseline's literals: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {name: {"repository_copy_confinement_commands": ["''"], "tenant_worktree_confinement_commands": ["()"],
                              "role_worktree_access_commands": ["()"]}.get(name, []) for name in FUNCTIONS},
          f"the defaults are the baseline's literals, none of them a name: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the sixteen, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_path_confinement" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_path_confinement" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the sixteen, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    # A name stays reachable on project_provision: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in ast.walk(guard) if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and set(KEPT) <= defined | exported, "project_provision defines none of them, and keeps the refusal, its normal-form check, the quoting and their callers, its own or re-exported")
    # The definitions that name them are counted wherever they now live -- project_provision, or a later slice's module,
    # whose read through project_provision (provision.X) counts as the name (SYRD-470: their role-account callers moved on).
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_path_confinement"]
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


def synthetic_owner(work: Path) -> list[str]:
    """A synthetic owner home and source checkout in a test-owned directory; the provisioning arguments that name them."""
    (work / "home").mkdir(parents=True)
    (work / "source").mkdir()
    return ["--project", "p464", "--owner-user", "p464-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
            "--port", "34464", "--output-dir", f"{work}/out"]


def test_the_direct_script_and_the_package_render_the_same_packet() -> None:
    # One synthetic provisioning packet, rendered by `project_provision.py` run as a script (its import fallback taken) and
    # through the package entry, in a test-owned directory.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd464-packet-")).resolve()
    try:
        outputs = {}
        for mode in ("script", "package"):
            work = base / "run"
            shutil.rmtree(work, ignore_errors=True)
            argv = synthetic_owner(work)
            script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
            if mode == "script":
                probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                         f"g = runpy.run_path({str(script)!r}, run_name='syrd464_script'); raise SystemExit(g['main']({argv!r}))")
            else:
                probe = f"import scripts.ticket_board.project_provision as pp; raise SystemExit(pp.main({argv!r}))"
            result = python(probe)
            files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
            outputs[mode] = (result.returncode, result.stdout, files)
        check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
              and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES,
              f"the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
        text = outputs["package"][2]["operator-commands.sh"].decode("utf-8")
        lines = text.splitlines()
        check(all(line.replace("HOME", f"{base}/run/home") in lines for line in PACKET_CONFINEMENT),
              "the operator commands carry the moved confinement: the worktree base closed and its inherited closure, the commit store granted read-only")
    finally:
        shutil.rmtree(base)


def test_a_refusal_keeps_its_class_message_and_exit_status() -> None:
    # In process, through the package: the moved code raises project_provision's own refusal class, unchanged.
    for fn, kwargs in ((m.repository_copy_confinement_commands, {"owner_user": "p464-agent", "owner_home": "/p464/home", "repositories": ["/p464/home/../x.git"]}),
                       (m.tenant_source_confinement_commands, {"owner_user": "p464-agent", "owner_home": "/p464/home", "checkout": "/p464/homex/p"})):
        try:
            with contained():
                fn(**kwargs)
        except ValueError as exc:
            raised = exc
        else:
            raised = None
        check(type(raised) is t.PathContainmentError and type(raised).__module__ == REFUSAL_MODULE,
              f"{fn.__name__}: refuses with project_provision's own PathContainmentError: {type(raised)}")
    # From the CLI: the direct script and the installed wrapper, each run as Python runs a program -- as `__main__`, its own
    # directory first on the path, its arguments in argv -- refusing an unnormalized commit store.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd464-refusal-")).resolve()
    try:
        printed = {}
        for mode, program in (("script", ROOT / "scripts" / "ticket_board" / "project_provision.py"), ("package", ROOT / "scripts" / "ticket-board-provision-project")):
            work = base / mode
            argv = [*synthetic_owner(work), "--commit-git-dir", f"{work}/home/x/../store.git"]
            result = python(f"import runpy, sys; sys.argv = [{str(program)!r}, *{argv!r}]; sys.path[0] = {str(program.parent)!r}; "
                            f"runpy.run_path({str(program)!r}, run_name='__main__')")
            last = result.stderr.rstrip("\n").rsplit("\n", 1)[-1]
            name, _, message = last.partition(": ")
            printed[mode] = (result.returncode, name, message.replace(str(work), "WORK"), result.stdout)
        expected = ("a repository copy WORK/home/x/../store.git is not in normal form; refusing to grant access to a path that walks back out of itself")
        check(all(printed[mode][0] == 1 and printed[mode][2] == expected and printed[mode][3] == "" for mode in printed),
              f"both modes exit 1 with the same refusal message and nothing on stdout: {printed}")
        check({mode: printed[mode][1] for mode in printed} == REFUSAL_PRINTED,
              f"the printed class: project_provision's through the package; qualified by the fallback module as a direct script: {printed}")
    finally:
        shutil.rmtree(base)


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


#: The ACL command the baseline builds: expected text, compared and never run.
ACL = "sudo setfacl"


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def value(label):
        return result(label)["value"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    def refused(label, text):
        return result(label).get("raised") == "PathContainmentError" and text in result(label)["message"]

    Q = "'/p464/home"
    check([value(k) for k in GOLDEN if k.startswith("within:")] == [True, True, False, False, True, False, True],
          "containment is by path components: a child, itself, trailing slashes and the tree's root are inside; a coinciding prefix, a relative path and an empty root are not")
    check(value("prefix: inside") is None and value("prefix: elsewhere") is None and value("prefix: no root") is None and value("prefix: no candidate") is None
          and refused("prefix: a coincidence refused", "only shares its prefix") and value("prefix: a trailing-slash root does not share the prefix") is None
          and refused("prefix: an unnormalized root", "not in normal form") and refused("prefix: a relative candidate", "not an absolute path")
          and result("prefix: the refusal class rebound on project_provision")["raised"] == "P464Refusal"
          and value("prefix: the containment test rebound on project_provision") is None,
          "a coinciding prefix is refused, with project_provision's refusal class; anything inside, elsewhere or missing passes; both paths must be absolute and normal")
    check(value("interior: a deep leaf") == ["/p464/home/a", "/p464/home/a/b"]
          and value("interior: a direct child") == value("interior: the root itself") == value("interior: elsewhere") == []
          and refused("interior: a coincidence refused", "only shares its prefix"),
          "the interior directories: strictly between the root and the leaf, none outside it, a coincidence refused")
    check(value("traversal: the owner home") == [f"{ACL} -m u:p464-svc:--x {Q}'"]
          and value("traversal: a home that needs quoting") == [f"{ACL} -m g:p464-roles:--x '/p464/it'\"'\"'s home'"]
          and refused("traversal: an unnormalized home", "not in normal form") and refused("traversal: a relative home", "not an absolute path")
          and value("traversal: the quoting rebound on project_provision") == [f"{ACL} -m u:p464-svc:--x </p464/home>"],
          "traversal: one execute-only ACL on the home for the principal, quoted through project_provision")
    install = lambda mode, path, owner="'p464-agent'": f"sudo install -d -m {mode} -o {owner} -g {owner} '{path}'"
    check(value("source: a checkout two levels down") == [install("0750", "/p464/home/Projects"), install("0750", "/p464/home/Projects/p464")]
          and value("source: the checkout is the home") == value("source: a checkout outside the home") == []
          and refused("source: a coincidence refused", "only shares its prefix") and refused("source: an unnormalized checkout", "the project checkout")
          and value("source: an owner that needs quoting") == [install("0750", "/p464/home/p", "'p464 agent'")]
          and value("source: the mode rebound on project_provision") == [install("0464", "/p464/home/p")]
          and value("source: the ancestors rebound on project_provision") == [install("0750", "/p464/rebound")],
          "the source tree: every directory from the home down to the checkout, owned by the owner at TENANT_SOURCE_MODE; nothing outside the home")
    store = value("store: a store three levels down")
    check(store == [f"{ACL} -m u:p464-svc:--x {Q}'", f"{ACL} -m u:p464-svc:--x {Q}/git'", f"{ACL} -m u:p464-svc:--x {Q}/git/p464'",
                    f"{ACL} -R -m u:p464-svc:rX {Q}/git/p464/store.git'", f"{ACL} -R -m d:u:p464-svc:rX {Q}/git/p464/store.git'"]
          and len(value("store: a direct child")) == 3 and value("store: the home itself") == value("store: outside the home") == []
          and refused("store: a coincidence refused", "only shares its prefix") and refused("store: an unnormalized store", "the commit store")
          and value("store: the traversal rebound on project_provision")[0] == "# p464 traversal",
          "the commit store: traversal to it, then read-only (rX) for the service, recursive and default; nothing for the home itself or outside it")
    check(value("copies: inside, outside, empty and shared parents") == [install("0755", "/p464/home/git"), install("0750", "/p464/home/git/a.git"),
                                                                         install("0750", "/p464/home/git/c.git"), install("0750", "/p464/home/d.git")]
          and value("copies: a mode given")[1] == install("0700", "/p464/home/git/a.git") and value("copies: the home itself skipped") == value("copies: none") == []
          and refused("copies: an unnormalized copy", "a repository copy") and refused("copies: a coincidence refused", "only shares its prefix")
          and refused("copies: an unnormalized home", "the owner home")
          and value("copies: the modes rebound on project_provision") == [install("0462", "/p464/home/git"), install("0461", "/p464/home/git/a.git")],
          "repository copies: interiors at INTERIOR_DIRECTORY_MODE, each copy at REPOSITORY_COPY_MODE or the mode given, once each; empty, outside and the home skipped")
    wt = value("worktrees: a base with worktrees inside, outside, empty and repeated")
    check(wt == [install("0750", "/p464/home/wt"), install("0750", "/p464/home/wt/p464"), install("0750", "/p464/home/wt/p464/main"),
                 install("0750", "/p464/home/wt/p464/audit"), "if [ -d '/p464/home/wt/p464' ]; then",
                 "    sudo find '/p464/home/wt/p464' -mindepth 1 -maxdepth 1 -type d -exec chmod o-rwx {} +",
                 f"    {ACL} -m 'd:u::rwx,d:g::r-x,d:o::---' '/p464/home/wt/p464'", "fi"]
          and value("worktrees: the base is the home") == value("worktrees: a base outside the home") == []
          and refused("worktrees: a coincidence refused", "only shares its prefix") and refused("worktrees: an unnormalized worktree", "a role worktree")
          and f"    {ACL} -m 'd:u::rwx' '/p464/home/wt'" in value("worktrees: the closure and mode rebound on project_provision")
          and value("worktrees: the closure and mode rebound on project_provision")[0] == install("0464", "/p464/home/wt"),
          "worktrees: the base and each worktree under it closed to others, existing worktrees closed and the inherited closure set on the base")
    check(value("retire: both surfaces inside the home") == ["if getent group 'p464-sock' >/dev/null 2>&1; then", f"    {ACL} -x d:g:p464-sock {Q}/wt'",
                                                             f"    {ACL} -x g:p464-sock {Q}/wt'", f"    {ACL} -R -x d:g:p464-sock {Q}/ctl'",
                                                             f"    {ACL} -R -x g:p464-sock {Q}/ctl'", "fi"]
          and len(value("retire: the control repository outside")) == 4
          and value("retire: both outside") == value("retire: no group") == value("retire: the owner's own group") == []
          and refused("retire: an unnormalized surface", "a repository surface") and refused("retire: an unnormalized home", "the owner home"),
          "retiring the socket group: its ACLs removed from surfaces inside the home only if the group exists; nothing for no group or the owner's own")
    roles = value("roles: both inside, with retired groups")
    check(roles[:6] == [f"{ACL} -m g:p464-repo:--x {Q}'", f"{ACL} -m g:p464-repo:--x {Q}/wt'", f"{ACL} -m g:p464-repo:--x {Q}/ctl'",
                        f"{ACL} -m g:p464-repo:--x {Q}/wt/p464'", f"{ACL} -R -m g:p464-repo:rwX {Q}/ctl/p464.git'",
                        f"{ACL} -R -m d:g:p464-repo:rwX {Q}/ctl/p464.git'"]
          and roles[6:] == [f"{ACL} -R -x d:g:{g} {Q}/ctl/p464.git'" if i % 2 == 0 else f"{ACL} -R -x g:{g} {Q}/ctl/p464.git'"
                            for g in ("p464-old", "p464-older") for i in (0, 1)]
          and not any("/p464/home" in c for c in value("roles: both outside the home"))
          and refused("roles: an unnormalized control repository", "the control repository") and refused("roles: a coincidence refused", "only shares its prefix")
          and f"{ACL} -m g:p464-repo:--x '/p464/interior'" in value("roles: the interior rebound on project_provision"),
          "role worktrees: traversal to the base and the control repository, read-write on the repository, retired groups removed except the current one")
    director = value("director: provisioning and project inside")
    check(director[0] == f"{ACL} -m u:p464-director:--x {Q}'" and director[-2:] == [f"{ACL} -R -m u:p464-director:rwX {Q}/.local/state/p464/provision'",
                                                                                       f"{ACL} -R -m d:u:p464-director:rwX {Q}/.local/state/p464/provision'"]
          and director.count(f"{ACL} -m u:p464-director:--x {Q}/.local/state/p464'") == 1
          and f"{ACL} -m u:p464-director:--x {Q}/q'" in value("director: a project directly in the home")
          and len(value("director: no project")) == 4 and len(value("director: both outside the home")) == 2
          and refused("director: a project coincidence refused", "only shares its prefix") and refused("director: an unnormalized provisioning directory", "the provisioning directory")
          and refused("director: a relative project", "the project directory"),
          "the director: traversal to the provisioning directory and the project, read-write on the provisioning directory; a project granted once")
    check(value("ancestors: including the target") == ["/p464/home/a", "/p464/home/a/b", "/p464/home/a/b/c"] and value("ancestors: excluding the target") == ["/p464/home/a", "/p464/home/a/b"]
          and value("ancestors: a direct child, excluded") == value("ancestors: the home itself") == value("ancestors: outside the home") == [],
          "owned ancestors: each directory below the home down to the target (or its parent); none for the home itself or outside it")
    check(calls("prefix: the containment test rebound on project_provision")[-1] == "_is_within rebound"
          and "owned_ancestor_dirs rebound" in calls("source: the ancestors rebound on project_provision")
          and "owner_home_traversal_commands rebound" in calls("store: the traversal rebound on project_provision")
          and calls("roles: the interior rebound on project_provision").count("_interior_directories rebound") == 2,
          "every rebound helper was the one called: each is read on project_provision when the function runs")


def test_every_seam_is_reached() -> None:
    # Every helper the twelve read on project_provision is a recorder there; the constants, the quoting and the refusal class are
    # rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = {"_refuse_unnormalized", "_is_within", "_refuse_prefix_coincidence", "_interior_directories", "owner_home_traversal_commands", "owned_ancestor_dirs"}
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions == {"shell_quote", "PathContainmentError", *CONSTANT_TEXT}, f"every other seam is a rebound name: {sorted(names - functions)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_the_same_defaults",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packet", "test_a_refusal_keeps_its_class_message_and_exit_status")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_path_confinement_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
