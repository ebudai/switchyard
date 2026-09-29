#!/usr/bin/env python3
"""SYRD-473: the root-executable trust checks, against project_provision they came out of.

The two -- `untrusted_root_executable_reasons`, why root must not execute a
file, and `acl_write_grants`, the access control entries that let somebody else
write it (SYRD-62) -- moved unchanged into
`scripts/ticket_board/provision_root_trust.py`; `project_provision` re-exports
them, in both branches of its import block, and keeps its own imports as they
were. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first, and the one
  `team_launcher` holds from its import at load. The module alone loads only
  its package. The shipped runner is `subprocess.run` itself, bound when each
  function is defined, and it still asks `getfacl` exactly as before.
- **Seams (rule 24):** what the trust check reads of `project_provision` -- its
  sibling `acl_write_grants` -- is read through it when it runs, with the
  direct-script fallback, so a patch there reaches it; `trusted_bootstrap` and
  `trusted_upgrade_release` import the check from `project_provision` when they
  run, so a patch there is what they get. Callers are counted across the
  re-exported modules.
- **The behaviour is the baseline's:** every kind of access control entry, a
  list that cannot be read, and on synthetic trees every step's owner, mode,
  kind and symlink, paths not in normal form, boundaries at, above and beside
  the file, and grants on a directory. `GOLDEN` below was produced by running
  the BASELINE module's own definitions over the very cases embedded here
  (`gold473.py`), not typed; it is byte-identical under `env -i`, in a normal
  role pane, with another HOME, USER, COLUMNS and TMPDIR, under umask 077 and
  under several hash seeds.
- **The direct script answers what the package answers** and looks at no host
  path (a recorder with a positive control), and the default, shaped, lean and
  roles packets are byte-identical through both.

No real home, tenant, account, /etc, /var or /opt path is read or written, and
getfacl is never run: every case is given a stand-in runner, and the shipped
default reaches a Popen stand-in. Trees are made under /tmp; the one case whose
boundary is not an ancestor walks on to /tmp and /, as the baseline's does.
Spawns, every exec, signals, account and group lookups and socket connections
are refused for each case.
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
from scripts.ticket_board import provision_root_trust as m  # noqa: E402

CHECKS = 0
MOVED = ('acl_write_grants', 'untrusted_root_executable_reasons')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'untrusted_root_executable_reasons': {'acl_write_grants': 1},
}
#: Measured on the baseline: every project_provision definition outside the two that names them, and how often.
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/switchyard-install-authority': {'import untrusted_root_executable_reasons': 2}, 'scripts/team_launcher.py': {'import untrusted_root_executable_reasons': 1}, 'scripts/trusted_bootstrap.py': {'import untrusted_root_executable_reasons': 1}, 'scripts/trusted_upgrade_release.py': {'import untrusted_root_executable_reasons': 1}}
#: The BASELINE's own behaviour for the cases below (`gold473.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'acl: clean': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: named user': {'result': {'type': 'tuple', 'value': [['user:ctl:rwx'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: named group': {'result': {'type': 'tuple', 'value': [['group:g473:rw-'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: named without write': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: mask and base entries writable': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: default other': {'result': {'type': 'tuple', 'value': [['default:other::rw-'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: default other without write': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: default named': {'result': {'type': 'tuple', 'value': [['default:user:ctl:rwx'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: comments and effective': {'result': {'type': 'tuple', 'value': [['user:bob:rwx'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: short fields': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: an unknown kind': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: an empty qualifier on a named kind': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: write in any position': {'result': {'type': 'tuple', 'value': [['user:a:-w-', 'group:b:--w'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: nonzero with stderr': {'result': {'type': 'tuple', 'value': [[], 'acl tool: /p473/f: No such file']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: nonzero with blank stderr': {'result': {'type': 'tuple', 'value': [[], 'getfacl failed']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: nonzero without stderr': {'result': {'type': 'tuple', 'value': [[], 'getfacl failed']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: stdout None': {'result': {'type': 'tuple', 'value': [[], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: a runner that raises OSError': {'result': {'type': 'tuple', 'value': [[], "[Errno 2] No such file or directory: 'getfacl'"]}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: a result without attributes': {'result': {'type': 'tuple', 'value': [[], 'getfacl failed']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: a runner that raises something else': {'result': {'raised': 'ValueError', 'message': 'not an OSError'}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/f'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    'acl: a string path': {'result': {'type': 'tuple', 'value': [['user:ctl:rwx'], '']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', '/p473/s'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {}},
    "trust: every step root's": {'result': {'type': 'list', 'value': []}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: the default owner': {'result': {'type': 'list', 'value': []}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: another owner': {'result': {'type': 'list', 'value': ['ROOT/ok/bin/tool is owned by uid OWN rather than by uid OTHER', 'ROOT/ok/bin is owned by uid OWN rather than by uid OTHER', 'ROOT/ok is owned by uid OWN rather than by uid OTHER', 'ROOT is owned by uid OWN rather than by uid OTHER']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: a group-writable file': {'result': {'type': 'list', 'value': ['ROOT/gw/tool is group- or world-writable (mode 0775)']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/gw/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/gw'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: a world-writable directory': {'result': {'type': 'list', 'value': ['ROOT/ww is group- or world-writable (mode 0757)']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ww/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ww'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: a directory where the file should be': {'result': {'type': 'list', 'value': ['ROOT/isdir/tool is not a regular file']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/isdir/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/isdir'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: a missing file': {'result': {'type': 'list', 'value': ["ROOT/ok/bin/absent cannot be inspected: [Errno 2] No such file or directory: 'ROOT/ok/bin/absent'"]}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: under a regular file': {'result': {'type': 'list', 'value': ["ROOT/plain/tool cannot be inspected: [Errno 20] Not a directory: 'ROOT/plain/tool'", 'ROOT/plain is not a directory']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/plain'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 2}},
    'trust: a symlinked file': {'result': {'type': 'list', 'value': ['ROOT/ok/bin/link is reached through a symlink and resolves to ROOT/ok/bin/tool', 'ROOT/ok/bin/link is a symlink']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: through a symlinked directory': {'result': {'type': 'list', 'value': ['ROOT/linkdir/bin/tool is reached through a symlink and resolves to ROOT/ok/bin/tool', 'ROOT/linkdir is a symlink']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/linkdir/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/linkdir/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: a relative path': {'result': {'type': 'list', 'value': ['ok/bin/tool is not an absolute path in normal form']}, 'runner': [], 'calls': {}},
    'trust: a path with ..': {'result': {'type': 'list', 'value': ['ROOT/ok/../ok/bin/tool is not an absolute path in normal form']}, 'runner': [], 'calls': {}},
    'trust: the boundary is the file': {'result': {'type': 'list', 'value': []}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 1}},
    'trust: the boundary is a parent': {'result': {'type': 'list', 'value': []}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 3}},
    'trust: a boundary that is not an ancestor': {'result': {'type': 'list', 'value': ['ROOT/ok/bin/tool is not under ROOT/gw', '/tmp is owned by uid 0 rather than by uid OWN', '/tmp is group- or world-writable (mode 1777)', '/ is owned by uid 0 rather than by uid OWN']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ABOVE'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', '/'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 6}},
    'trust: a string path': {'result': {'type': 'list', 'value': []}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: a grant on the directory': {'result': {'type': 'list', 'value': ['ROOT/ok/bin grants write through user:ctl:rwx', 'ROOT/ok/bin grants write through default:other::rw-']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: an unreadable list': {'result': {'type': 'list', 'value': ['ROOT/ok/bin/tool access control list could not be read: no acl support']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: a runner that raises OSError': {'result': {'type': 'list', 'value': ["ROOT/ok/bin/tool access control list could not be read: [Errno 2] No such file or directory: 'getfacl'", "ROOT/ok/bin access control list could not be read: [Errno 2] No such file or directory: 'getfacl'", "ROOT/ok access control list could not be read: [Errno 2] No such file or directory: 'getfacl'", "ROOT access control list could not be read: [Errno 2] No such file or directory: 'getfacl'"]}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin/tool'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok/bin'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ok'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 4}},
    'trust: every fault at once': {'result': {'type': 'list', 'value': ['ROOT/ww/gwlink is reached through a symlink and resolves to ROOT/gw/tool', 'ROOT/ww/gwlink is a symlink', 'ROOT/ww is owned by uid OWN rather than by uid OTHER', 'ROOT/ww is group- or world-writable (mode 0757)', 'ROOT/ww grants write through group:g473:rwx', 'ROOT is owned by uid OWN rather than by uid OTHER']}, 'runner': [[['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT/ww'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]], [['getfacl', '-p', '--omit-header', '--absolute-names', 'ROOT'], [['stderr', '-1'], ['stdout', '-1'], ['text', 'True']]]], 'calls': {'acl_write_grants': 2}},
    'trust: acl_write_grants rebound on project_provision': {'result': {'type': 'list', 'value': ['ROOT/ok/bin/tool grants write through user:rebound:1', 'ROOT/ok/bin grants write through user:rebound:2', 'ROOT/ok grants write through user:rebound:3', 'ROOT grants write through user:rebound:4']}, 'runner': [], 'calls': {'acl_write_grants rebound': 4}},
    'trust: an unreadable answer from the rebound acl_write_grants': {'result': {'type': 'list', 'value': ['ROOT/ok/bin/tool access control list could not be read: rebound ROOT/ok/bin/tool', 'ROOT/ok/bin access control list could not be read: rebound ROOT/ok/bin', 'ROOT/ok access control list could not be read: rebound ROOT/ok', 'ROOT access control list could not be read: rebound ROOT']}, 'runner': [], 'calls': {'acl_write_grants rebound': 4}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = {'default': 10, 'shaped': 10, 'lean': 10, 'roles': 10}

# --- the cases, shared verbatim with `gold473.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the two with a stand-in ACL-tool runner -- never the shipped default, which would run the real
# one -- and records the answer or the exact exception, every call the runner got, and how many times the trust check
# called `acl_write_grants` on `project_provision` (recorded there and passed through). A trust case gets a fresh synthetic
# tree under /tmp, bounded there; the walk goes above it only where a case asks what an unrelated boundary does, and
# those steps are recorded as ABOVE. Paths and uids are normalized: ROOT, uid OWN (this process's), uid OTHER.
ACLS = {
    "clean": (0, "user::rwx\ngroup::r-x\nother::r-x\n", ""),
    "named user": (0, "user::rwx\nuser:ctl:rwx\ngroup::r-x\nmask::rwx\nother::r-x\n", ""),
    "named group": (0, "group:g473:rw-\n", ""),
    "named without write": (0, "user:ctl:r-x\ngroup:g473:r--\n", ""),
    "mask and base entries writable": (0, "mask::rwx\nother::rwx\ngroup::rwx\nuser::rwx\n", ""),
    "default other": (0, "default:other::rw-\n", ""),
    "default other without write": (0, "default:other::r-x\n", ""),
    "default named": (0, "default:user:ctl:rwx\ndefault:group::rwx\ndefault:mask::rwx\n", ""),
    "comments and effective": (0, "# file: x\n# owner: root\nuser:bob:rwx\t#effective:r-x\n\n   \n", ""),
    "short fields": (0, "user:rw\ndefault:user\ndefault\n:\n", ""),
    "an unknown kind": (0, "flag:x:rwx\n", ""),
    "an empty qualifier on a named kind": (0, "user::rwx\ngroup::rwx\n", ""),
    "write in any position": (0, "user:a:-w-\ngroup:b:--w\n", ""),
    "nonzero with stderr": (1, "user:x:rwx\n", "  acl tool: /p473/f: No such file  \n"),
    "nonzero with blank stderr": (2, "", "  \n"),
    "nonzero without stderr": (2, "", ""),
    "stdout None": (0, None, ""),
    "a runner that raises OSError": "raise",
    "a result without attributes": "bare",
    "a runner that raises something else": "valueerror",
}
TREES = {
    "every step root's": ("ok/bin/tool", {"owner_uid": "OWN"}),
    "the default owner": ("ok/bin/tool", {}),
    "another owner": ("ok/bin/tool", {"owner_uid": "OTHER"}),
    "a group-writable file": ("gw/tool", {"owner_uid": "OWN"}),
    "a world-writable directory": ("ww/tool", {"owner_uid": "OWN"}),
    "a directory where the file should be": ("isdir/tool", {"owner_uid": "OWN"}),
    "a missing file": ("ok/bin/absent", {"owner_uid": "OWN"}),
    "under a regular file": ("plain/tool", {"owner_uid": "OWN"}),
    "a symlinked file": ("ok/bin/link", {"owner_uid": "OWN"}),
    "through a symlinked directory": ("linkdir/bin/tool", {"owner_uid": "OWN"}),
    "a relative path": ("RELATIVE", {"owner_uid": "OWN"}),
    "a path with ..": ("ok/../ok/bin/tool", {"owner_uid": "OWN"}),
    "the boundary is the file": ("ok/bin/tool", {"owner_uid": "OWN", "boundary": "ok/bin/tool"}),
    "the boundary is a parent": ("ok/bin/tool", {"owner_uid": "OWN", "boundary": "ok"}),
    "a boundary that is not an ancestor": ("ok/bin/tool", {"owner_uid": "OWN", "boundary": "gw"}),
    "a string path": ("STRING ok/bin/tool", {"owner_uid": "OWN"}),
    "a grant on the directory": ("ok/bin/tool", {"owner_uid": "OWN", "acl": {"ok/bin": (0, "user::rwx\nuser:ctl:rwx\ndefault:other::rw-\n", "")}}),
    "an unreadable list": ("ok/bin/tool", {"owner_uid": "OWN", "acl": {"ok/bin/tool": (1, "", "no acl support")}}),
    "a runner that raises OSError": ("ok/bin/tool", {"owner_uid": "OWN", "acl": "raise"}),
    "every fault at once": ("ww/gwlink", {"owner_uid": "OTHER", "acl": {"ww": (0, "group:g473:rwx\n", ""), "ww/gwlink": (3, "", "")}}),
}
CASES = {
    **{f"acl: {label}": {"call": "acl_write_grants", "acl": answer} for label, answer in ACLS.items()},
    "acl: a string path": {"call": "acl_write_grants", "acl": ACLS["named user"], "path": "/p473/s"},
    **{f"trust: {label}": {"call": "untrusted_root_executable_reasons", "path": path, "kw": kw} for label, (path, kw) in TREES.items()},
    "trust: acl_write_grants rebound on project_provision": {"call": "untrusted_root_executable_reasons", "path": "ok/bin/tool", "kw": {"owner_uid": "OWN"},
                                                             "rebind": "acl_write_grants"},
    "trust: an unreadable answer from the rebound acl_write_grants": {"call": "untrusted_root_executable_reasons", "path": "ok/bin/tool",
                                                                       "kw": {"owner_uid": "OWN"}, "rebind": "unreadable"},
}
FUNCTIONS = ("acl_write_grants", "untrusted_root_executable_reasons")
PASSED = ("acl_write_grants",)


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definition; `t` is project_provision, whose acl_write_grants is recorded and passed through."""
    import os as _os, pathlib as _pl, shutil as _sh, tempfile as _tf
    counts, calls = {}, []
    root = _pl.Path(_tf.mkdtemp(prefix="syrd473-case-", dir="/tmp"))
    uid = _os.getuid()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, str):
            return value.replace(str(root), "ROOT").replace(f"uid {uid + 1}", "uid OTHER").replace(f"uid {uid}", "uid OWN")
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return norm(repr(value))

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    class Proc:
        def __init__(self, answer):
            self.returncode, self.stdout, self.stderr = answer

    def runner(answer):
        def run(argv, **kwargs):
            where = str(argv[-1])
            inside = where == str(root) or where.startswith(str(root) + "/") or not where.startswith("/tmp")
            calls.append([argv[:-1] + [where if inside else "ABOVE"], sorted((k, repr(v)) for k, v in kwargs.items())])
            got = answer.get(where[len(str(root)) + 1:], ACLS["clean"]) if isinstance(answer, dict) else answer
            if got == "raise":
                raise FileNotFoundError(2, "No such file or directory", argv[0])
            if got == "valueerror":
                raise ValueError("not an OSError")
            if got == "bare":
                return object()
            return Proc(got)
        return run

    saved = {n: getattr(t, n) for n in FUNCTIONS}
    try:
        _os.chmod(root, 0o755)
        for rel, kind, mode in (("ok", "d", 0o755), ("ok/bin", "d", 0o755), ("ok/bin/tool", "f", 0o755), ("gw", "d", 0o755), ("gw/tool", "f", 0o775),
                                ("ww", "d", 0o757), ("ww/tool", "f", 0o755), ("isdir", "d", 0o755), ("isdir/tool", "d", 0o755), ("plain", "f", 0o755)):
            p = root / rel
            p.mkdir() if kind == "d" else p.write_text("#!/bin/sh\n")
            _os.chmod(p, mode)
        (root / "ok/bin/link").symlink_to(root / "ok/bin/tool")
        (root / "linkdir").symlink_to(root / "ok")
        (root / "ww/gwlink").symlink_to(root / "gw/tool")
        t.acl_write_grants = lambda *a, **k: note("acl_write_grants") or saved["acl_write_grants"](*a, **k)
        if spec.get("rebind") == "acl_write_grants":
            t.acl_write_grants = lambda path, *, runner: note("acl_write_grants rebound") or ([f"user:rebound:{counts['acl_write_grants rebound']}"], "")
        elif spec.get("rebind") == "unreadable":
            t.acl_write_grants = lambda path, *, runner: note("acl_write_grants rebound") or ([], f"rebound {path}")
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        if spec["call"] == "acl_write_grants":
            args, kwargs = [spec.get("path", _pl.Path("/p473/f"))], {"runner": runner(spec["acl"])}
        else:
            path = spec["path"]
            args = [_pl.Path("ok/bin/tool") if path == "RELATIVE" else str(root / path.split(" ", 1)[1]) if path.startswith("STRING ") else root / path]
            kw = dict(spec["kw"])
            kwargs = {"runner": runner(kw.pop("acl", {})), "boundary": root / kw.pop("boundary") if "boundary" in kw else root}
            if "owner_uid" in kw:
                kwargs["owner_uid"] = uid if kw["owner_uid"] == "OWN" else uid + 1
        try:
            got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "runner": norm(calls), "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "runner": norm(calls), "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        _sh.rmtree(root)
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


#: What the trust check reads on project_provision when it runs: only its sibling -- nothing project_provision keeps.
KEPT = ()
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them: the shipped runner is subprocess.run, bound when the function is defined.
DEFAULTS = {"acl_write_grants": ["subprocess.run"], "untrusted_root_executable_reasons": ["None", "None", "subprocess.run"]}
#: The module's imports at load: the standard library only.
MODULE_IMPORTS = ["from __future__ import annotations", "import os", "import stat", "import subprocess", "from pathlib import Path", "from typing import Callable"]
#: These import the trust check from project_provision inside a function, when it runs; their boundary tests patch it there.
CALL_TIME_READERS = {"scripts/trusted_bootstrap.py": ("untrusted_root_executable_reasons",),
                     "scripts/trusted_upgrade_release.py": ("untrusted_root_executable_reasons",)}
#: These import it from project_provision at load, and so hold the module's own object.
LOAD_TIME_READERS = ("scripts.team_launcher",)
#: The packets rendered through the direct script and the package, the board Python pinned.
ROLE_ACCOUNTS = (("director", "p473-director"), ("main", "p473-main"))
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
    "pathlib.Path('/opt/switchyard/syrd473-positive-control').is_file()\n"
    "control = len(hits); hits.clear()\n"
)
#: A getfacl stand-in for subprocess.run to reach: subprocess.run looks Popen up when it runs, so the shipped default
#: runner -- subprocess.run itself -- runs whole and nothing is spawned. It answers by the path it is asked about.
FAKE_POPEN = (
    "import subprocess\n"
    "popened = []\n"
    "class FakePopen:\n"
    "    def __init__(self, argv, **kwargs):\n"
    "        popened.append((list(argv), sorted(kwargs)))\n"
    "        self.args, self.returncode = argv, None\n"
    "        self.answer = ANSWERS.get(argv[-1].rsplit('/', 1)[-1], (0, 'user::rwx\\ngroup::r-x\\nother::r-x\\n', ''))\n"
    "    def __enter__(self): return self\n"
    "    def __exit__(self, *exc): return False\n"
    "    def communicate(self, input=None, timeout=None):\n"
    "        self.returncode = self.answer[0]; return self.answer[1], self.answer[2]\n"
    "    def poll(self): return self.returncode\n"
    "    def wait(self, timeout=None): return self.returncode\n"
    "    def kill(self): pass\n"
    "subprocess.Popen = FakePopen\n"
)
#: A synthetic tree under /tmp: a root-owned-by-us chain ROOT/ok/bin/tool, and a group-writable one ROOT/gw/tool.
TREE = (
    "import os, pathlib, tempfile\n"
    "root = pathlib.Path(tempfile.mkdtemp(prefix='syrd473-tree-', dir='/tmp')); os.chmod(root, 0o755)\n"
    "for rel, kind, mode in (('ok', 'd', 0o755), ('ok/bin', 'd', 0o755), ('ok/bin/tool', 'f', 0o755), ('gw', 'd', 0o755), ('gw/tool', 'f', 0o775)):\n"
    "    p = root / rel; p.mkdir() if kind == 'd' else p.write_text('#!/bin/sh\\n'); os.chmod(p, mode)\n"
    "(root / 'ok/bin/link').symlink_to(root / 'ok/bin/tool')\n"
    "uid = os.getuid()\n"
    "def norm(x): return str(x).replace(str(root), 'ROOT').replace(f'uid {uid}', 'uid OWN')\n"
)
#: One probe, run in the package and as the direct script: both checks on a synthetic tree with a stand-in runner, and
#: the check with acl_write_grants rebound on project_provision (every copy of it) -- as a digest, and the host paths looked at.
TRUST_PROBE = (
    "import hashlib, json, shutil\n"
    "def runner(argv, **kwargs):\n"
    "    import types; name = argv[-1].rsplit('/', 1)[-1]\n"
    "    return types.SimpleNamespace(returncode=0, stdout={'bin': 'user:ctl:rwx\\n', 'f': 'default:other::rw-\\n'}.get(name, ''), stderr='')\n"
    "out = [g['acl_write_grants'](pathlib.Path('/p473/f'), runner=runner)]\n"
    "for path in ('ok/bin/tool', 'gw/tool', 'ok/bin/link', 'ok/bin/absent'):\n"
    "    out.append(norm(g['untrusted_root_executable_reasons'](root / path, boundary=root, owner_uid=uid, runner=runner)))\n"
    "out.append(norm(g['untrusted_root_executable_reasons'](root / 'ok/bin/tool', boundary=root, runner=runner)))\n"
    "saved = [(h, h['acl_write_grants']) for h in HOLDERS]\n"
    "for h, _ in saved: h['acl_write_grants'] = lambda path, *, runner: ([f'user:rebound:{norm(path)}'], '')\n"
    "out.append(norm(g['untrusted_root_executable_reasons'](root / 'ok/bin/tool', boundary=root, owner_uid=uid, runner=runner)))\n"
    "for h, v in saved: h['acl_write_grants'] = v\n"
    "shutil.rmtree(root)\n"
    "print(len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest(), 'control', control, 'host paths', len(hits))\n"
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
    result = python("import sys, scripts.ticket_board.provision_root_trust as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_root_trust", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_root_trust"),
                  (*LOAD_TIME_READERS, "scripts.ticket_board.provision_root_trust")):
        result = python("import importlib, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_root_trust as m; "
                        f"readers = [importlib.import_module(n) for n in {LOAD_TIME_READERS!r}]; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        "all(r.untrusted_root_executable_reasons is m.untrusted_root_executable_reasons for r in readers), "
                        f"all(getattr(m, n).__kwdefaults__['runner'] is subprocess.run for n in {FUNCTIONS!r}), "
                        "not any(hasattr(m, n) for n in ('provision', 'project_provision', 'PathContainmentError', 'shell_quote')))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_root_trust'] True True True",
              f"{' then '.join(order)}: one object each, defined here and held by the readers that import it at load; the shipped runner is "
              f"subprocess.run itself; nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    check(all(getattr(m, n) is getattr(t, n) for n in ("os", "stat", "subprocess", "Path", "Callable")),
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_a_patch_on_project_provision_reaches_the_call_time_readers() -> None:
    # trusted_bootstrap and trusted_upgrade_release import the trust check from project_provision inside a function, when
    # it runs: a patch there (as their boundary tests make) is what they get, and the module's own object otherwise.
    for path, names in CALL_TIME_READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        imports = [x for x in ast.walk(source) if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("project_provision") and set(names) <= {a.name for a in x.names}]
        inside = {id(x) for fn in ast.walk(source) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn)}
        check(imports and all(id(x) in inside for x in imports), f"{path} imports {names} from project_provision inside its functions, when they run")
    result = python("import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_root_trust as m\n"
                    "name = 'untrusted_root_executable_reasons'\n"
                    "saved = getattr(t, name); setattr(t, name, lambda *a, **k: 'PATCHED')\n"
                    "seen = getattr(__import__('scripts.ticket_board.project_provision', fromlist=[name]), name)\n"
                    "setattr(t, name, saved)\n"
                    "restored = getattr(__import__('scripts.ticket_board.project_provision', fromlist=[name]), name)\n"
                    "print(seen('x'), restored is m.untrusted_root_executable_reasons)")
    check(result.stdout.strip() == "PATCHED True",
          f"a patch on project_provision is what a call-time import sees; restored, it is the module's own: {result.stdout}{result.stderr[-400:]}")


def test_the_shipped_default_runner_asks_the_acl_tool() -> None:
    # No runner given: subprocess.run, bound when the function was defined, runs whole against a Popen stand-in -- the
    # argv, the pipes and text mode it asks for, and what it makes of the answer, through the module and project_provision.
    for holder in ("scripts.ticket_board.provision_root_trust", "scripts.ticket_board.project_provision"):
        result = python("ANSWERS = {'f': (0, 'user::rwx\\nuser:ctl:rwx\\n', ''), 'g': (1, '', ' acl tool: g: denied \\n'), 'bin': (0, 'group:g473:rw-\\n', '')}\n"
                        + FAKE_POPEN + TREE + f"import importlib; h = importlib.import_module({holder!r})\n"
                        "print(h.acl_write_grants(pathlib.Path('/p473/f')), h.acl_write_grants(pathlib.Path('/p473/g')), popened[:2])\n"
                        "del popened[:]\n"
                        "print(norm(h.untrusted_root_executable_reasons(root / 'ok/bin/tool', boundary=root, owner_uid=uid)), norm([p[0][-1] for p in popened]))\n"
                        "import shutil; shutil.rmtree(root)")
        lines = result.stdout.splitlines()
        # The tool is the one the baseline asked for (GOLDEN, measured), with the same flags.
        tool = GOLDEN["acl: clean"]["runner"][0][0][0]
        asked = [[tool, "-p", "--omit-header", "--absolute-names", path] for path in ("/p473/f", "/p473/g")]
        check(result.returncode == 0 and len(lines) == 2
              and lines[0] == (f"(['user:ctl:rwx'], '') ([], 'acl tool: g: denied') "
                               f"[({asked[0]!r}, ['stderr', 'stdout', 'text']), ({asked[1]!r}, ['stderr', 'stdout', 'text'])]")
              and lines[1] == "['ROOT/ok/bin grants write through group:g473:rw-'] ['ROOT/ok/bin/tool', 'ROOT/ok/bin', 'ROOT/ok', 'ROOT']",
              f"{holder}: the shipped default asks the ACL tool for each step, with pipes and text, and reads its answer: {result.stdout}{result.stderr[-600:]}")


def test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the trust check's
    # fallback loads project_provision a second time beside it -- the copy it reads acl_write_grants through. Both runs
    # record any host path they look at.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(HOST_RECORDER + TREE + f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd473_script__'); "
                       "m = sys.modules['provision_root_trust']; import project_provision\n"
                       "HOLDERS = [g, vars(project_provision), g['main'].__globals__]\n"
                       f"print(all(g[n] is getattr(m, n) and getattr(project_provision, n) is getattr(m, n) for n in {MOVED!r}))\n" + TRUST_PROBE)
    as_package = python(HOST_RECORDER + TREE + "import scripts.ticket_board.project_provision as pp; g = vars(pp); HOLDERS = [g]\n" + TRUST_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("7 ")
          and lines[1].endswith(" control 1 host paths 0"),
          f"as a direct script: both copies of project_provision hold the module's own objects, and all seven answers are the package's, "
          f"byte for byte, with no host path looked at (the recorder's positive control caught): {as_script.stdout}{as_script.stderr[-400:]} | "
          f"{as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_root_trust.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "provision":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each project_provision name, its sibling included, read through it exactly as often as before: {through}")
        first = 1 if ast.get_docstring(node) is not None else 0
        own = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            check(ast.unparse(node.body[first]) == CALL_TIME_IMPORT and len(own) == 2,
                  f"{name}: project_provision imported first thing (after its docstring), with the direct-script fallback, and nothing else: {own}")
        else:
            check(own == [], f"{name}: reads nothing of project_provision and imports nothing: {own}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and (x.id in expected or x.id in KEPT) and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == MODULE_IMPORTS and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef, ast.Assign, ast.AnnAssign))],
          f"the standard library only, at load, and no constant: {top}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the two, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_root_trust" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_root_trust" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the two, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_root_trust")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read, its own or re-exported")
    check({"os", "stat", "subprocess", "Path", "Callable"} <= {a.asname or a.name for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names},
          "project_provision's own standard-library imports are left as they were")
    # The definitions that name them are counted wherever they live -- project_provision, or a later slice's module, whose
    # read through project_provision (provision.X) counts as the name -- so this check survives the next slice.
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_root_trust"]
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
    # Four synthetic provisioning packets, each rendered by `project_provision.py` run as a script (its import fallback
    # taken) and through the package entry, in a test-owned directory, the board Python pinned.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd473-packet-")).resolve()
    try:
        for variant, (extra, roles) in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p473", "--owner-user", "p473-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34473", "--output-dir", f"{work}/out", *extra]
                given = f"dataclasses.replace(build(*a, **k), role_accounts={ROLE_ACCOUNTS!r}, roles_group='p473-roles')" if roles else "build(*a, **k)"
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                pin = "import os; os.environ['TICKET_BOARD_PYTHON'] = '/p473/python'; "
                if mode == "script":
                    probe = (pin + f"import dataclasses, runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd473_script'); main = g['main']; build = main.__globals__['build_plan']; "
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

    def runner(label):
        return GOLDEN[label]["runner"]

    tool = runner("acl: clean")[0][0][0]
    check(all(runner(f"acl: {label}") == [[[tool, "-p", "--omit-header", "--absolute-names", "/p473/f"], [["stderr", "-1"], ["stdout", "-1"], ["text", "True"]]]]
              for label in ACLS) and runner("acl: a string path")[0][0][1:] == ["-p", "--omit-header", "--absolute-names", "/p473/s"]
          and all(c[0][0] == tool for v in GOLDEN.values() for c in v["runner"]),
          "acl_write_grants asks the ACL tool once, for the path, with its three flags, pipes and text -- one tool throughout")
    granted = {label: value(f"acl: {label}")[0] for label in ACLS if "value" in GOLDEN[f"acl: {label}"]["result"]}
    check(granted["named user"] == ["user:ctl:rwx"] and granted["named group"] == ["group:g473:rw-"] and granted["default other"] == ["default:other::rw-"]
          and granted["default named"] == ["default:user:ctl:rwx"] and granted["comments and effective"] == ["user:bob:rwx"]
          and granted["write in any position"] == ["user:a:-w-", "group:b:--w"]
          and all(granted[label] == [] for label in ("clean", "named without write", "mask and base entries writable", "default other without write",
                                                     "short fields", "an unknown kind", "an empty qualifier on a named kind", "stdout None")),
          "a grant is a named user or group entry, or a default other entry, carrying w -- never the mask or a base entry, which the mode bits are")
    failed = f"{tool} failed"
    check(value("acl: nonzero with stderr") == [[], ACLS["nonzero with stderr"][2].strip()] and value("acl: nonzero with blank stderr") == [[], failed]
          and value("acl: nonzero without stderr") == [[], failed] and value("acl: a result without attributes") == [[], failed]
          and value("acl: a runner that raises OSError") == [[], f"[Errno 2] No such file or directory: '{tool}'"]
          and GOLDEN["acl: a runner that raises something else"]["result"] == {"raised": "ValueError", "message": "not an OSError"},
          "a list that cannot be read says why, and no grant: the tool's stderr, stripped, else that it failed; an OSError is the reason; anything else is raised")
    check(value("trust: every step root's") == [] and value("trust: the default owner") == [] and value("trust: the boundary is the file") == []
          and value("trust: the boundary is a parent") == [] and value("trust: a string path") == []
          and [len(runner(f"trust: {label}")) for label in ("every step root's", "the boundary is the file", "the boundary is a parent")] == [4, 1, 3]
          and calls("trust: every step root's") == {"acl_write_grants": 4},
          "every step up to the boundary is checked, the boundary included, through acl_write_grants on project_provision -- and nothing above it")
    check(value("trust: another owner") == [f"{p} is owned by uid OWN rather than by uid OTHER" for p in ("ROOT/ok/bin/tool", "ROOT/ok/bin", "ROOT/ok", "ROOT")]
          and value("trust: a group-writable file") == ["ROOT/gw/tool is group- or world-writable (mode 0775)"]
          and value("trust: a world-writable directory") == ["ROOT/ww is group- or world-writable (mode 0757)"]
          and value("trust: a directory where the file should be") == ["ROOT/isdir/tool is not a regular file"]
          and value("trust: under a regular file")[1:] == ["ROOT/plain is not a directory"],
          "an owner other than the entitled one, a group- or world-writable mode, and the wrong kind of step are each a reason")
    check(value("trust: a symlinked file") == ["ROOT/ok/bin/link is reached through a symlink and resolves to ROOT/ok/bin/tool", "ROOT/ok/bin/link is a symlink"]
          and value("trust: through a symlinked directory") == ["ROOT/linkdir/bin/tool is reached through a symlink and resolves to ROOT/ok/bin/tool",
                                                                 "ROOT/linkdir is a symlink"]
          and value("trust: a missing file")[0].startswith("ROOT/ok/bin/absent cannot be inspected: [Errno 2]"),
          "a symlink anywhere on the way is a reason, and a step that cannot be inspected is one")
    check(value("trust: a relative path") == ["ok/bin/tool is not an absolute path in normal form"]
          and value("trust: a path with ..") == ["ROOT/ok/../ok/bin/tool is not an absolute path in normal form"]
          and runner("trust: a relative path") == runner("trust: a path with ..") == [],
          "a path not in normal form is refused before anything is looked at")
    beyond = value("trust: a boundary that is not an ancestor")
    check(beyond[0] == "ROOT/ok/bin/tool is not under ROOT/gw" and len(runner("trust: a boundary that is not an ancestor")) == 6
          and [c[0][-1] for c in runner("trust: a boundary that is not an ancestor")][-2:] == ["ABOVE", "/"],
          "a boundary that is not an ancestor is a reason, and the whole path to / is checked instead")
    check(value("trust: a grant on the directory") == ["ROOT/ok/bin grants write through user:ctl:rwx", "ROOT/ok/bin grants write through default:other::rw-"]
          and value("trust: an unreadable list") == ["ROOT/ok/bin/tool access control list could not be read: no acl support"]
          and len(value("trust: a runner that raises OSError")) == 4,
          "a write grant on any step is a reason, and so is a list that could not be read")
    check(value("trust: acl_write_grants rebound on project_provision") == [f"{p} grants write through user:rebound:{i}" for i, p in
                                                                             enumerate(("ROOT/ok/bin/tool", "ROOT/ok/bin", "ROOT/ok", "ROOT"), 1)]
          and calls("trust: acl_write_grants rebound on project_provision") == {"acl_write_grants rebound": 4}
          and value("trust: an unreadable answer from the rebound acl_write_grants")[0] == "ROOT/ok/bin/tool access control list could not be read: rebound ROOT/ok/bin/tool",
          "acl_write_grants is read through project_provision when the check runs: a patch there is what it gets")
    every = value("trust: every fault at once")
    check(every == ["ROOT/ww/gwlink is reached through a symlink and resolves to ROOT/gw/tool", "ROOT/ww/gwlink is a symlink",
                    "ROOT/ww is owned by uid OWN rather than by uid OTHER", "ROOT/ww is group- or world-writable (mode 0757)",
                    "ROOT/ww grants write through group:g473:rwx", "ROOT is owned by uid OWN rather than by uid OTHER"]
          and [c[0][-1] for c in runner("trust: every fault at once")] == ["ROOT/ww", "ROOT"],
          f"every fault on every step is reported, in path order; a symlinked step is reported and not looked into further: {every}")


def test_every_seam_is_reached() -> None:
    # The one function the trust check reads on project_provision is a recorder there, and a rebinding of its own.
    names = {name for reads in SEAMS.values() for name in reads}
    check(names == set(PASSED) and set(PASSED) <= REACHED and "acl_write_grants rebound" in REACHED,
          f"a recorder on project_provision reached every seam: missing {sorted(set(PASSED) - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_a_patch_on_project_provision_reaches_the_call_time_readers", "test_the_shipped_default_runner_asks_the_acl_tool",
             "test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_root_trust_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
