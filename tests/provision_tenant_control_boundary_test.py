#!/usr/bin/env python3
"""SYRD-469: the provisioning tenant-control grant and sudoers, against project_provision they came out of.

The thirteen -- the control grant and its paths (`TENANT_CONTROL_GRANT_NAME`,
`tenant_control_grant_path`, `tenant_control_grant_name`,
`tenant_control_grant_document`, `tenant_control_grant`), who is recorded
(`resolve_control_user`, `invoking_human`), the sudoers rule for the two
root-owned helpers (`tenant_control_sudoers_path`, `tenant_control_helper_path`,
`display_attach_helper_path`, `tenant_control_sudoers_document`,
`tenant_control_sudoers`) and the commands that install both
(`tenant_control_commands`) -- moved unchanged into
`scripts/ticket_board/provision_tenant_control.py`; `project_provision`
re-exports them all, in both branches of its import block, and keeps
`shell_quote`, `TENANT_CONTROL_ROOT`, `SHARED_RELEASE_CURRENT`,
`TENANT_CONTROL_LAUNCHER` and the role-control sudoers. This pins what makes
that safe:

- **No cycle, one object**, whichever module is imported first. The module
  alone loads only its package; `team_launcher`'s four imports are its objects.
- **The launcher:** `TENANT_CONTROL_LAUNCHER` stays on `project_provision`,
  bound from `SHARED_RELEASE_CURRENT` when it loads, and the grant document
  reads it there when it runs.
- **Seams (rule 24):** what they read of `project_provision` -- each other,
  `TENANT_CONTROL_ROOT`, `TENANT_CONTROL_GRANT_NAME`, the launcher and
  `shell_quote` -- is read through it when they run, with the direct-script
  fallback, so a patch there reaches them.
- **Readers:** every production module imports them from `project_provision`,
  as often as before, and `project_provision`'s own callers name them as often.
- **The behaviour is the baseline's:** every path; the recorded human with no
  grant and with each kind of grant; the invoking human for each SUDO_UID; the
  commands, grant and sudoers for synthetic humans and plans built by
  `build_plan`. `GOLDEN` below was produced by running the BASELINE module's own
  definitions over the very cases embedded here (`gold469.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME, USER
  and COLUMNS, under umask 077, under several hash seeds and with another
  TMPDIR, locale and SUDO_UID in the environment.
- **The direct script answers what the package answers,** and the default,
  shaped, lean and control packets are byte-identical through both; the control
  packet's grant and sudoers have content.

No real home, tenant, account, grant, /etc or /var path is read or written:
every path is synthetic, grants live in a directory each case creates, and the
builders only return paths, documents and shell text. No account is looked up:
each case stands in `pwd.getpwuid` and `os.getuid`. Spawns, every exec,
signals, real account and group lookups and socket connections are refused for
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

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_tenant_control as m  # noqa: E402

CHECKS = 0
MOVED = ('TENANT_CONTROL_GRANT_NAME', 'tenant_control_grant_path', 'tenant_control_sudoers_path', 'resolve_control_user', 'invoking_human', 'tenant_control_commands', 'tenant_control_helper_path', 'display_attach_helper_path', 'tenant_control_grant_document', 'tenant_control_sudoers_document', 'tenant_control_grant', 'tenant_control_sudoers', 'tenant_control_grant_name')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'tenant_control_grant_path': {'TENANT_CONTROL_GRANT_NAME': 1, 'TENANT_CONTROL_ROOT': 1},
    'resolve_control_user': {'TENANT_CONTROL_GRANT_NAME': 1, 'TENANT_CONTROL_ROOT': 1},
    'tenant_control_commands': {'TENANT_CONTROL_ROOT': 1, 'shell_quote': 8, 'tenant_control_grant_document': 1, 'tenant_control_grant_path': 1, 'tenant_control_sudoers_document': 1, 'tenant_control_sudoers_path': 1},
    'tenant_control_grant_document': {'TENANT_CONTROL_LAUNCHER': 1},
    'tenant_control_sudoers_document': {'display_attach_helper_path': 1, 'tenant_control_helper_path': 1},
    'tenant_control_grant': {'tenant_control_grant_document': 1},
    'tenant_control_sudoers': {'tenant_control_sudoers_document': 1},
}
#: Measured on the baseline: every project_provision definition outside the thirteen that names them, and how often.
DISPATCH = {'role_account_commands': {'invoking_human': 1, 'resolve_control_user': 1, 'tenant_control_commands': 1}, 'render_operator_commands': {'tenant_control_grant_name': 1, 'tenant_control_grant_path': 1}, 'privileged_artifact_names': {'tenant_control_grant_name': 1}, 'write_artifacts': {'tenant_control_grant': 1, 'tenant_control_grant_name': 1, 'tenant_control_sudoers': 1}, 'main': {'tenant_control_grant': 1, 'tenant_control_sudoers': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/display_bridge.py': {'import TENANT_CONTROL_GRANT_NAME': 2, 'import tenant_control_commands': 1, 'import tenant_control_sudoers_document': 1}, 'scripts/role_account_migration.py': {'import tenant_control_commands': 1}, 'scripts/team_launcher.py': {'import display_attach_helper_path': 1, 'import invoking_human': 1, 'import resolve_control_user': 1, 'import tenant_control_helper_path': 1}}
#: The BASELINE's own behaviour for the cases below (`gold469.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'grant path': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard/p469/control-grant.json'}, 'calls': {}},
    'grant path: the root and name rebound on project_provision': {'result': {'type': 'str', 'value': '/p469/control/p469/g.json'}, 'calls': {}},
    'grant name': {'result': {'type': 'str', 'value': 'p469-control-grant.json'}, 'calls': {}},
    'sudoers path': {'result': {'type': 'str', 'value': '/etc/sudoers.d/49-p469-tenant-control'}, 'calls': {}},
    'helper path': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard/p469/switchyard-tenant-control'}, 'calls': {}},
    'helper path: not under a rebound control root': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard/p469/switchyard-tenant-control'}, 'calls': {}},
    'display attach path': {'result': {'type': 'str', 'value': '/usr/local/lib/switchyard/p469/switchyard-display-attach'}, 'calls': {}},
    "control user: no grant, invoked by ''": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "control user: no grant, invoked by 'root'": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "control user: no grant, invoked by 'p469-owner'": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "control user: no grant, invoked by ' p469-owner '": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "control user: no grant, invoked by 'p469-dev'": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "control user: no grant, invoked by 'alice'": {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    "control user: no grant, invoked by '  alice  '": {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    'control user: a root-owned grant': {'result': {'type': 'str', 'value': 'bob'}, 'calls': {}},
    'control user: a grant the caller owns': {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    'control user: a group-writable root grant': {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    'control user: a root grant for another project': {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    'control user: an unreadable root grant': {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    'control user: a root grant naming nobody': {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    'control user: a root grant with a padded name': {'result': {'type': 'str', 'value': 'bob'}, 'calls': {}},
    'control user: a root grant under another name': {'result': {'type': 'str', 'value': 'alice'}, 'calls': {}},
    "control user: no grant, invoked by an owner outside the project's names": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'control user: no grant, invoked by that owner, given padded': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'control user: the default root is TENANT_CONTROL_ROOT, read through project_provision': {'result': {'type': 'str', 'value': 'bob'}, 'calls': {}},
    'control user: the grant name rebound on project_provision': {'result': {'type': 'str', 'value': 'carol'}, 'calls': {}},
    'invoking human: sudo uid of a known account': {'result': {'type': 'str', 'value': 'p469-human'}, 'calls': {}},
    'invoking human: sudo uid 0': {'result': {'type': 'str', 'value': 'p469-self'}, 'calls': {}},
    'invoking human: sudo uid padded': {'result': {'type': 'str', 'value': 'p469-human'}, 'calls': {}},
    'invoking human: sudo uid not a number': {'result': {'type': 'str', 'value': 'p469-self'}, 'calls': {}},
    'invoking human: sudo uid of an unknown account': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'invoking human: no sudo uid': {'result': {'type': 'str', 'value': 'p469-self'}, 'calls': {}},
    'invoking human: the process environment': {'result': {'type': 'str', 'value': 'p469-human'}, 'calls': {}},
    'invoking human: an unknown own uid': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "commands for ''": {'result': {'type': 'list', 'value': []}, 'calls': {}},
    "commands for 'alice'": {'result': {'type': 'list', 'value': ['# Lifecycle control for alice, the human who provisioned p469.', '# Re-runnable: both files are rewritten to exactly these bytes.', "sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p469'", 'printf \'%s\\n\' \'{\n  "authorized_user": "alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}\' | sudo install -m 0644 -o root -g root /dev/stdin \'/usr/local/lib/switchyard/p469/control-grant.json\'', 'TEXT 706 chars sha256:446826f972a749f5', "sudo visudo -c -f '/etc/sudoers.d/49-p469-tenant-control.staged'", "sudo mv '/etc/sudoers.d/49-p469-tenant-control.staged' '/etc/sudoers.d/49-p469-tenant-control'"]}, 'calls': {'display_attach_helper_path': 1, 'shell_quote': 8, 'tenant_control_grant_document': 1, 'tenant_control_grant_path': 1, 'tenant_control_helper_path': 1, 'tenant_control_sudoers_document': 1, 'tenant_control_sudoers_path': 1}},
    'commands for "it\'s alice"': {'result': {'type': 'list', 'value': ["# Lifecycle control for it's alice, the human who provisioned p469.", '# Re-runnable: both files are rewritten to exactly these bytes.', "sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p469'", 'printf \'%s\\n\' \'{\n  "authorized_user": "it\'"\'"\'s alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}\' | sudo install -m 0644 -o root -g root /dev/stdin \'/usr/local/lib/switchyard/p469/control-grant.json\'', 'TEXT 724 chars sha256:7b25d7fe36dc63be', "sudo visudo -c -f '/etc/sudoers.d/49-p469-tenant-control.staged'", "sudo mv '/etc/sudoers.d/49-p469-tenant-control.staged' '/etc/sudoers.d/49-p469-tenant-control'"]}, 'calls': {'display_attach_helper_path': 1, 'shell_quote': 8, 'tenant_control_grant_document': 1, 'tenant_control_grant_path': 1, 'tenant_control_helper_path': 1, 'tenant_control_sudoers_document': 1, 'tenant_control_sudoers_path': 1}},
    'commands: the control root rebound on project_provision': {'result': {'type': 'list', 'value': ['# Lifecycle control for alice, the human who provisioned p469.', '# Re-runnable: both files are rewritten to exactly these bytes.', "sudo install -d -m 0755 -o root -g root '/p469/control/p469'", 'printf \'%s\\n\' \'{\n  "authorized_user": "alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}\' | sudo install -m 0644 -o root -g root /dev/stdin \'/p469/control/p469/control-grant.json\'', 'TEXT 706 chars sha256:446826f972a749f5', "sudo visudo -c -f '/etc/sudoers.d/49-p469-tenant-control.staged'", "sudo mv '/etc/sudoers.d/49-p469-tenant-control.staged' '/etc/sudoers.d/49-p469-tenant-control'"]}, 'calls': {'display_attach_helper_path': 1, 'shell_quote': 8, 'tenant_control_grant_document': 1, 'tenant_control_grant_path': 1, 'tenant_control_helper_path': 1, 'tenant_control_sudoers_document': 1, 'tenant_control_sudoers_path': 1}},
    'commands: the paths and documents rebound on project_provision': {'result': {'type': 'list', 'value': ['# Lifecycle control for alice, the human who provisioned p469.', '# Re-runnable: both files are rewritten to exactly these bytes.', "sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p469'", 'printf \'%s\\n\' \'GRANT [(\'"\'"\'control_user\'"\'"\', \'"\'"\'alice\'"\'"\'), (\'"\'"\'owner_user\'"\'"\', \'"\'"\'p469-owner\'"\'"\'), (\'"\'"\'project\'"\'"\', \'"\'"\'p469\'"\'"\')]\' | sudo install -m 0644 -o root -g root /dev/stdin \'/p469/tenant_control_grant_path/p469\'', "printf '%s\\n' 'SUDOERS p469 alice' | sudo install -m 0440 -o root -g root /dev/stdin '/p469/tenant_control_sudoers_path/p469.staged'", "sudo visudo -c -f '/p469/tenant_control_sudoers_path/p469.staged'", "sudo mv '/p469/tenant_control_sudoers_path/p469.staged' '/p469/tenant_control_sudoers_path/p469'"]}, 'calls': {'shell_quote': 8, 'tenant_control_grant_document rebound': 1, 'tenant_control_grant_path rebound': 1, 'tenant_control_sudoers_document rebound': 1, 'tenant_control_sudoers_path rebound': 1}},
    'commands: the quoting rebound on project_provision': {'result': {'type': 'list', 'value': ['# Lifecycle control for alice, the human who provisioned p469.', '# Re-runnable: both files are rewritten to exactly these bytes.', 'sudo install -d -m 0755 -o root -g root </usr/local/lib/switchyard/p469>', 'printf \'%s\\n\' <{\n  "authorized_user": "alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}> | sudo install -m 0644 -o root -g root /dev/stdin </usr/local/lib/switchyard/p469/control-grant.json>', 'TEXT 706 chars sha256:316f543bdc70a1f8', 'sudo visudo -c -f </etc/sudoers.d/49-p469-tenant-control.staged>', 'sudo mv </etc/sudoers.d/49-p469-tenant-control.staged> </etc/sudoers.d/49-p469-tenant-control>']}, 'calls': {'display_attach_helper_path': 1, 'shell_quote rebound': 8, 'tenant_control_grant_document': 1, 'tenant_control_grant_path': 1, 'tenant_control_helper_path': 1, 'tenant_control_sudoers_document': 1, 'tenant_control_sudoers_path': 1}},
    "grant document for ''": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "grant document for 'alice'": {'result': {'type': 'str', 'value': '{\n  "authorized_user": "alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}'}, 'calls': {}},
    'grant document for "it\'s alice"': {'result': {'type': 'str', 'value': '{\n  "authorized_user": "it\'s alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}'}, 'calls': {}},
    'grant document: the launcher rebound on project_provision': {'result': {'type': 'str', 'value': '{\n  "authorized_user": "alice",\n  "launcher": "/p469/launcher",\n  "owner": "p469-owner",\n  "project": "p469"\n}'}, 'calls': {}},
    'grant document: SHARED_RELEASE_CURRENT rebound after load changes nothing': {'result': {'type': 'str', 'value': '{\n  "authorized_user": "alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}'}, 'calls': {}},
    "sudoers document for ''": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "sudoers document for 'alice'": {'result': {'type': 'str', 'value': 'TEXT 593 chars sha256:86889e1515e5241b'}, 'calls': {'display_attach_helper_path': 1, 'tenant_control_helper_path': 1}},
    'sudoers document for "it\'s alice"': {'result': {'type': 'str', 'value': 'TEXT 603 chars sha256:fb5e9dbeee7bd8c6'}, 'calls': {'display_attach_helper_path': 1, 'tenant_control_helper_path': 1}},
    'sudoers document: the helper paths rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 555 chars sha256:e9d2819a870916b3'}, 'calls': {'display_attach_helper_path rebound': 1, 'tenant_control_helper_path rebound': 1}},
    "plan grant for ''": {'result': {'type': 'str', 'value': ''}, 'calls': {'tenant_control_grant_document': 1}},
    "plan grant for 'alice'": {'result': {'type': 'str', 'value': '{\n  "authorized_user": "alice",\n  "launcher": "/opt/switchyard/current/switchyard",\n  "owner": "p469-owner",\n  "project": "p469"\n}\n'}, 'calls': {'tenant_control_grant_document': 1}},
    "plan sudoers for ''": {'result': {'type': 'str', 'value': ''}, 'calls': {'tenant_control_sudoers_document': 1}},
    "plan sudoers for 'alice'": {'result': {'type': 'str', 'value': 'TEXT 594 chars sha256:652fca3c596a30ef'}, 'calls': {'display_attach_helper_path': 1, 'tenant_control_helper_path': 1, 'tenant_control_sudoers_document': 1}},
    'plan grant: the document rebound on project_provision': {'result': {'type': 'str', 'value': "GRANT [('control_user', 'alice'), ('owner_user', 'p469-owner'), ('project', 'p469')]\n"}, 'calls': {'tenant_control_grant_document rebound': 1}},
    'plan sudoers: the document rebound on project_provision': {'result': {'type': 'str', 'value': 'SUDOERS p469 alice\n'}, 'calls': {'tenant_control_sudoers_document rebound': 1}},
    'constants': {'result': {'type': 'list', 'value': ['/usr/local/lib/switchyard', 'control-grant.json', '/opt/switchyard/current/switchyard', '/opt/switchyard/current']}, 'calls': {}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = {'default': 10, 'shaped': 10, 'lean': 10, 'control': 12}

# --- the cases, shared verbatim with `gold469.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the twelve and records the answer or the exact exception, and how many times it called each of
# `project_provision`'s helpers (recorded there and passed through). Projects, owners, humans and plans are synthetic
# (p469...); plans are built by `build_plan` for a synthetic owner. No account is looked up: `pwd.getpwuid` and
# `os.getuid` are stood in with synthetic answers for each case. Grants live in a directory the case creates; a
# "root-owned" grant is one whose stat the case reports as uid 0 -- only for files the case registered -- so the
# authority branch runs without root and without reading any real grant. A case may set SUDO_UID, and may rebind a
# `project_provision` name to show it is read there when the function runs.
ACCOUNTS = {1469: "p469-human", 2469: "p469-self"}
GOOD = '{"project": "p469", "authorized_user": "bob"}'
GRANTS = {
    "a root-owned grant": ("control-grant.json", GOOD, 0o100644),
    "a grant the caller owns": ("control-grant.json", GOOD, None),
    "a group-writable root grant": ("control-grant.json", GOOD, 0o100664),
    "a root grant for another project": ("control-grant.json", '{"project": "q469", "authorized_user": "bob"}', 0o100644),
    "an unreadable root grant": ("control-grant.json", "{not json", 0o100644),
    "a root grant naming nobody": ("control-grant.json", '{"project": "p469", "authorized_user": "  "}', 0o100644),
    "a root grant with a padded name": ("control-grant.json", '{"project": "p469", "authorized_user": "  bob  "}', 0o100644),
    "a root grant under another name": ("other-grant.json", '{"project": "p469", "authorized_user": "carol"}', 0o100644),
}
CASES = {
    "grant path": {"call": "tenant_control_grant_path", "args": ["p469"]},
    "grant path: the root and name rebound on project_provision": {"call": "tenant_control_grant_path", "args": ["p469"],
                                                                   "rebind": {"TENANT_CONTROL_ROOT": "/p469/control", "TENANT_CONTROL_GRANT_NAME": "g.json"}},
    "grant name": {"call": "tenant_control_grant_name", "args": ["p469"]},
    "sudoers path": {"call": "tenant_control_sudoers_path", "args": ["p469"]},
    "helper path": {"call": "tenant_control_helper_path", "args": ["p469"]},
    "helper path: not under a rebound control root": {"call": "tenant_control_helper_path", "args": ["p469"], "rebind": {"TENANT_CONTROL_ROOT": "/p469/control"}},
    "display attach path": {"call": "display_attach_helper_path", "args": ["p469"]},
    **{f"control user: no grant, invoked by {who!r}": {"call": "resolve_control_user", "grant": None, "kwargs": {"invoking_user": who, "owner_user": "p469-owner"}}
       for who in ("", "root", "p469-owner", " p469-owner ", "p469-dev", "alice", "  alice  ")},
    **{f"control user: {g}": {"call": "resolve_control_user", "grant": g, "kwargs": {"invoking_user": "alice", "owner_user": "p469-owner"}} for g in GRANTS},
    "control user: no grant, invoked by an owner outside the project's names": {"call": "resolve_control_user", "grant": None,
                                                                                  "kwargs": {"invoking_user": "q469owner", "owner_user": "q469owner"}},
    "control user: no grant, invoked by that owner, given padded": {"call": "resolve_control_user", "grant": None,
                                                                    "kwargs": {"invoking_user": "q469owner", "owner_user": "  q469owner  "}},
    "control user: the default root is TENANT_CONTROL_ROOT, read through project_provision": {"call": "resolve_control_user", "grant": "a root-owned grant", "root": "rebind",
                                                                                               "kwargs": {"invoking_user": "alice", "owner_user": "p469-owner"}},
    "control user: the grant name rebound on project_provision": {"call": "resolve_control_user", "grant": "a root grant under another name",
                                                                  "rebind": {"TENANT_CONTROL_GRANT_NAME": "other-grant.json"},
                                                                  "kwargs": {"invoking_user": "alice", "owner_user": "p469-owner"}},
    **{f"invoking human: {label}": {"call": "invoking_human", "args": [env]}
       for label, env in (("sudo uid of a known account", {"SUDO_UID": "1469"}), ("sudo uid 0", {"SUDO_UID": "0"}), ("sudo uid padded", {"SUDO_UID": " 1469 "}),
                          ("sudo uid not a number", {"SUDO_UID": "x1"}), ("sudo uid of an unknown account", {"SUDO_UID": "7469"}), ("no sudo uid", {}))},
    "invoking human: the process environment": {"call": "invoking_human", "sudo_uid": "1469"},
    "invoking human: an unknown own uid": {"call": "invoking_human", "args": [{}], "uid": 9469},
    **{f"commands for {who!r}": {"call": "tenant_control_commands", "args": ["p469", "p469-owner", who]} for who in ("", "alice", "it's alice")},
    "commands: the control root rebound on project_provision": {"call": "tenant_control_commands", "args": ["p469", "p469-owner", "alice"], "rebind": {"TENANT_CONTROL_ROOT": "/p469/control"}},
    "commands: the paths and documents rebound on project_provision": {"call": "tenant_control_commands", "args": ["p469", "p469-owner", "alice"],
                                                                       "rebind": {"tenant_control_grant_path": True, "tenant_control_sudoers_path": True,
                                                                                  "tenant_control_grant_document": True, "tenant_control_sudoers_document": True}},
    "commands: the quoting rebound on project_provision": {"call": "tenant_control_commands", "args": ["p469", "p469-owner", "alice"], "rebind": {"shell_quote": True}},
    **{f"grant document for {who!r}": {"call": "tenant_control_grant_document", "kwargs": {"project": "p469", "owner_user": "p469-owner", "control_user": who}}
       for who in ("", "alice", "it's alice")},
    "grant document: the launcher rebound on project_provision": {"call": "tenant_control_grant_document", "kwargs": {"project": "p469", "owner_user": "p469-owner", "control_user": "alice"},
                                                                  "rebind": {"TENANT_CONTROL_LAUNCHER": "/p469/launcher"}},
    "grant document: SHARED_RELEASE_CURRENT rebound after load changes nothing": {"call": "tenant_control_grant_document",
                                                                                   "kwargs": {"project": "p469", "owner_user": "p469-owner", "control_user": "alice"},
                                                                                   "rebind": {"SHARED_RELEASE_CURRENT": "/p469/release"}},
    **{f"sudoers document for {who!r}": {"call": "tenant_control_sudoers_document", "args": ["p469", who]} for who in ("", "alice", "it's alice")},
    "sudoers document: the helper paths rebound on project_provision": {"call": "tenant_control_sudoers_document", "args": ["p469", "alice"],
                                                                        "rebind": {"tenant_control_helper_path": True, "display_attach_helper_path": True}},
    **{f"plan {what} for {who!r}": {"call": call, "plan": who} for what, call in (("grant", "tenant_control_grant"), ("sudoers", "tenant_control_sudoers")) for who in ("", "alice")},
    "plan grant: the document rebound on project_provision": {"call": "tenant_control_grant", "plan": "alice", "rebind": {"tenant_control_grant_document": True}},
    "plan sudoers: the document rebound on project_provision": {"call": "tenant_control_sudoers", "plan": "alice", "rebind": {"tenant_control_sudoers_document": True}},
    "constants": {"call": None},
}
FUNCTIONS = ("tenant_control_grant_path", "tenant_control_sudoers_path", "resolve_control_user", "invoking_human", "tenant_control_commands", "tenant_control_helper_path",
             "display_attach_helper_path", "tenant_control_grant_document", "tenant_control_sudoers_document", "tenant_control_grant", "tenant_control_sudoers",
             "tenant_control_grant_name")
PASSED = ("tenant_control_grant_path", "tenant_control_sudoers_path", "tenant_control_grant_document", "tenant_control_sudoers_document",
          "tenant_control_helper_path", "display_attach_helper_path", "shell_quote")
REBINDABLE = ("TENANT_CONTROL_ROOT", "TENANT_CONTROL_GRANT_NAME", "TENANT_CONTROL_LAUNCHER")


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import hashlib, os as _os, pathlib as _pl, pwd as _pwd, shutil as _sh, tempfile as _tf
    counts = {}
    work = _tf.mkdtemp(prefix="syrd469-case.")

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, _pl.Path):
            value = "PATH " + str(value)
        if isinstance(value, str):
            value = value.replace(work, "TMP")
            return value if len(value) <= 400 else f"TEXT {len(value)} chars sha256:{hashlib.sha256(value.encode()).hexdigest()[:16]}"
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        return repr(value)

    def note(seam):
        reached.add(seam)
        counts[seam] = counts.get(seam, 0) + 1

    def fake_getpwuid(uid):
        if uid not in ACCOUNTS:
            raise KeyError(f"getpwuid(): uid not found: {uid}")
        return _pwd.struct_passwd((ACCOUNTS[uid], "x", uid, uid, "", "/nonexistent", "/bin/false"))

    root_owned = {}
    real_stat = _pl.Path.stat

    def fake_stat(self, *a, **k):
        info = real_stat(self, *a, **k)
        mode = root_owned.get(str(self))
        if mode is None:
            return info
        fields = list(info); fields[0] = mode; fields[4] = 0
        return _os.stat_result(fields)

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "SHARED_RELEASE_CURRENT", "build_plan")}
    saved_env, saved_getpwuid, saved_getuid = _os.environ.get("SUDO_UID"), _pwd.getpwuid, _os.getuid
    try:
        _pwd.getpwuid = fake_getpwuid
        _os.getuid = lambda: spec.get("uid", 2469)
        _pl.Path.stat = fake_stat
        _os.environ.pop("SUDO_UID", None)
        if "sudo_uid" in spec:
            _os.environ["SUDO_UID"] = spec["sudo_uid"]
        args, kwargs = list(spec.get("args", [])), dict(spec.get("kwargs", {}))
        if "grant" in spec:
            base = _pl.Path(work) / "grants"
            if spec["grant"] is not None:
                name, text, mode = GRANTS[spec["grant"]]
                (base / "p469").mkdir(parents=True)
                (base / "p469" / name).write_text(text, encoding="utf-8")
                if mode is not None:
                    root_owned[str(base / "p469" / name)] = mode
            if spec.get("root") == "rebind":
                t.TENANT_CONTROL_ROOT = str(base)
            else:
                kwargs["root"] = base
            args = ["p469"]
        if "plan" in spec:
            args = [saved["build_plan"](project="p469", owner_user="p469-owner", owner_home=_pl.Path("/p469/home"), port=34469, control_user=spec["plan"])]
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "shell_quote":
                t.shell_quote = lambda value: note("shell_quote rebound") or "<" + str(value) + ">"
            elif name in ("tenant_control_grant_path", "tenant_control_sudoers_path", "tenant_control_helper_path", "display_attach_helper_path"):
                setattr(t, name, (lambda name: lambda project: note(name + " rebound") or f"/p469/{name}/{project}")(name))
            elif name == "tenant_control_grant_document":
                t.tenant_control_grant_document = lambda **k: note("tenant_control_grant_document rebound") or f"GRANT {sorted(k.items())}"
            elif name == "tenant_control_sudoers_document":
                t.tenant_control_sudoers_document = lambda project, control_user: note("tenant_control_sudoers_document rebound") or f"SUDOERS {project} {control_user}"
            else:
                setattr(t, name, value)
        if spec["call"] is None:
            got = [saved["TENANT_CONTROL_ROOT"], saved["TENANT_CONTROL_GRANT_NAME"], saved["TENANT_CONTROL_LAUNCHER"], saved["SHARED_RELEASE_CURRENT"]]
        else:
            fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
            try:
                got = fn(*args, **kwargs)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        _pwd.getpwuid, _os.getuid, _pl.Path.stat = saved_getpwuid, saved_getuid, real_stat
        _os.environ.pop("SUDO_UID", None)
        if saved_env is not None:
            _os.environ["SUDO_UID"] = saved_env
        _sh.rmtree(work)
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


#: What the twelve read on project_provision when they run, all its own. TENANT_CONTROL_LAUNCHER stays there: it is bound
#: from SHARED_RELEASE_CURRENT when project_provision loads, and the grant document reads it through project_provision.
KEPT = ("shell_quote", "TENANT_CONTROL_ROOT", "TENANT_CONTROL_LAUNCHER")
#: The one constant, as the baseline wrote it: (annotation, value).
CONSTANT_TEXT = {"TENANT_CONTROL_GRANT_NAME": (None, "'control-grant.json'")}
#: What project_provision keeps, as the baseline wrote it.
KEPT_TEXT = {"SHARED_RELEASE_CURRENT": "'/opt/switchyard/current'", "TENANT_CONTROL_ROOT": "'/usr/local/lib/switchyard'",
             "TENANT_CONTROL_LAUNCHER": "f'{SHARED_RELEASE_CURRENT}/switchyard'"}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them.
DEFAULTS = {"tenant_control_grant_path": [], "tenant_control_sudoers_path": [], "resolve_control_user": ["''", "''", "None"], "invoking_human": ["None"],
            "tenant_control_commands": [], "tenant_control_helper_path": [], "display_attach_helper_path": [], "tenant_control_grant_document": [],
            "tenant_control_sudoers_document": [], "tenant_control_grant": [], "tenant_control_sudoers": [], "tenant_control_grant_name": []}
#: team_launcher's module-level imports of them, from project_provision.
LAUNCHER_IMPORTS = ("display_attach_helper_path", "invoking_human", "resolve_control_user", "tenant_control_helper_path")
#: The packets rendered through the direct script and the package: the default plan, two the plan shapes, and one with a
#: synthetic control user given to build_plan (the CLI takes none), whose tenant-control grant and sudoers have content.
PACKET_VARIANTS = {
    "default": ([], ""),
    "shaped": (["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"], ""),
    "lean": (["--no-include-designer", "--no-include-audit"], ""),
    "control": ([], "p469-human"),
}
#: The control packet's two files with content.
CONTROL_FILES = ("49-p469-tenant-control", "p469-control-grant.json")
#: One probe, run in the package and as the direct script: every path, document and command for a synthetic project,
#: plan and human, and the recorded human from a synthetic grant directory -- as a digest. No account is looked up: the
#: probe stands in pwd.getpwuid and os.getuid.
CONTROL_PROBE = (
    "import hashlib, json, os, pathlib, pwd, tempfile\n"
    "pwd.getpwuid = lambda uid: pwd.struct_passwd(('p469-human' if uid == 1469 else 'p469-self', 'x', uid, uid, '', '/nonexistent', '/bin/false'))\n"
    "os.getuid = lambda: 2469\n"
    "grants = pathlib.Path(tempfile.mkdtemp(prefix='syrd469-probe-'))\n"
    "plan = g['build_plan'](project='p469', owner_user='p469-owner', owner_home=pathlib.Path('/p469/home'), port=34469, control_user='alice')\n"
    "out = [g['tenant_control_grant_path']('p469'), g['tenant_control_grant_name']('p469'), g['tenant_control_sudoers_path']('p469'),\n"
    "       g['tenant_control_helper_path']('p469'), g['display_attach_helper_path']('p469'), g['tenant_control_commands']('p469', 'p469-owner', 'alice'),\n"
    "       g['tenant_control_grant_document'](project='p469', owner_user='p469-owner', control_user='alice'),\n"
    "       g['tenant_control_sudoers_document']('p469', 'alice'), g['tenant_control_grant'](plan), g['tenant_control_sudoers'](plan),\n"
    "       g['invoking_human']({'SUDO_UID': '1469'}), g['invoking_human']({}),\n"
    "       g['resolve_control_user']('p469', invoking_user='alice', owner_user='p469-owner', root=grants)]\n"
    "grants.rmdir()\n"
    "print(len(out), hashlib.sha256(json.dumps(out).encode()).hexdigest())\n"
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
    result = python("import sys, scripts.ticket_board.provision_tenant_control as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_tenant_control", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_tenant_control"),
                  ("scripts.team_launcher", "scripts.ticket_board.provision_tenant_control")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_tenant_control as m, scripts.team_launcher as tl; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        f"all(getattr(tl, n) is getattr(m, n) for n in {LAUNCHER_IMPORTS!r}), "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', 'SHARED_RELEASE_CURRENT', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_tenant_control'] True True",
              f"{' then '.join(order)}: one object each, defined here; team_launcher's four imports the module's own; nothing of project_provision bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.Path is Path and m.json is json and m.os is os and m.pwd is pwd and t.pwd is m.pwd,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_launcher_stays_on_project_provision_bound_when_it_loads() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    kept = {top_name(n): ast.unparse(n.value) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and top_name(n) in KEPT_TEXT}
    check(kept == KEPT_TEXT, f"project_provision keeps SHARED_RELEASE_CURRENT, TENANT_CONTROL_ROOT and the launcher bound from the release, as written: {kept}")
    check(t.TENANT_CONTROL_LAUNCHER == f"{t.SHARED_RELEASE_CURRENT}/switchyard" == "/opt/switchyard/current/switchyard",
          "the launcher's value is the baseline's")
    result = python("import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_tenant_control as m, json\n"
                    "doc = lambda: json.loads(m.tenant_control_grant_document(project='p469', owner_user='p469-owner', control_user='alice'))['launcher']\n"
                    "before = doc(); t.TENANT_CONTROL_LAUNCHER = '/p469/patched'; patched = doc(); t.SHARED_RELEASE_CURRENT = '/p469/release'; after = doc()\n"
                    "print(before, patched, after)")
    check(result.stdout.strip() == "/opt/switchyard/current/switchyard /p469/patched /p469/patched",
          f"the moved grant document reads the launcher on project_provision when it runs; SHARED_RELEASE_CURRENT is read only when project_provision loads: "
          f"{result.stdout}{result.stderr[-400:]}")


def test_the_direct_script_answers_what_the_package_answers() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd469_script__'); "
                       "m = sys.modules['provision_tenant_control']; "
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + CONTROL_PROBE)
    as_package = python("import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + CONTROL_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("13 "),
          f"as a direct script: the script holds the module's own objects, and all thirteen answers are the package's, byte for byte: "
          f"{as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_tenant_control.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "import json", "import os", "import pwd", "from pathlib import Path"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the one constant is the baseline's: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the thirteen, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_tenant_control" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_tenant_control" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the thirteen, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_tenant_control")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read")
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
    # Four synthetic provisioning packets, each rendered by `project_provision.py` run as a script (its import fallback
    # taken) and through the package entry, in a test-owned directory.
    import shutil
    import tempfile
    base = Path(tempfile.mkdtemp(prefix="syrd469-packet-")).resolve()
    try:
        for variant, (extra, control_user) in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p469", "--owner-user", "p469-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34469", "--output-dir", f"{work}/out", *extra]
                given = repr({"control_user": control_user} if control_user else {})
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                if mode == "script":
                    probe = (f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd469_script'); main = g['main']; build = main.__globals__['build_plan']; "
                             f"main.__globals__['build_plan'] = lambda *a, **k: build(*a, **{{**k, **{given}}}); raise SystemExit(main({argv!r}))")
                else:
                    probe = ("import scripts.ticket_board.project_provision as pp; build = pp.build_plan; "
                             f"pp.build_plan = lambda *a, **k: build(*a, **{{**k, **{given}}}); raise SystemExit(pp.main({argv!r}))")
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES[variant],
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            files = outputs["package"][2]
            if control_user:
                check(all(files.get(name) and control_user.encode() in files[name] for name in CONTROL_FILES),
                      f"{variant}: the tenant-control grant and sudoers are written, naming the control user: {sorted(files)}")
            else:
                check(not any(name in files for name in CONTROL_FILES), f"{variant}: with no control user, no tenant-control grant or sudoers file: {sorted(files)}")
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
        return GOLDEN[label]["result"]["value"]

    def calls(label):
        return GOLDEN[label]["calls"]

    check(value("grant path") == "/usr/local/lib/switchyard/p469/control-grant.json" and value("grant name") == "p469-control-grant.json"
          and value("grant path: the root and name rebound on project_provision") == "/p469/control/p469/g.json"
          and value("sudoers path") == "/etc/sudoers.d/49-p469-tenant-control",
          "the grant under TENANT_CONTROL_ROOT/<project>, both read through project_provision; its provision-directory name; its own sudoers file")
    check(value("helper path") == value("helper path: not under a rebound control root") == "/usr/local/lib/switchyard/p469/switchyard-tenant-control"
          and value("display attach path") == "/usr/local/lib/switchyard/p469/switchyard-display-attach",
          "the two helpers' paths are written out, not built from TENANT_CONTROL_ROOT")
    nobody = [value(f"control user: no grant, invoked by {who!r}") for who in ("", "root", "p469-owner", " p469-owner ", "p469-dev")]
    nobody += [value("control user: no grant, invoked by an owner outside the project's names"), value("control user: no grant, invoked by that owner, given padded")]
    check(nobody == [""] * 7 and value("control user: no grant, invoked by 'alice'") == value("control user: no grant, invoked by '  alice  '") == "alice",
          "with no grant the provisioning human is recorded, stripped -- never nobody, root, the owner or one of the project's own accounts")
    check(value("control user: a root-owned grant") == value("control user: a root grant with a padded name") == "bob"
          and all(value(f"control user: {g}") == "alice" for g in ("a grant the caller owns", "a group-writable root grant", "a root grant for another project",
                                                                     "an unreadable root grant", "a root grant naming nobody", "a root grant under another name")),
          "only a root-owned, not group- or world-writable grant for this project naming someone is an authority; anything else falls back to the caller")
    check(value("control user: the default root is TENANT_CONTROL_ROOT, read through project_provision") == "bob"
          and value("control user: the grant name rebound on project_provision") == "carol",
          "the grant is looked up under TENANT_CONTROL_ROOT by TENANT_CONTROL_GRANT_NAME, both read through project_provision when it runs")
    check(value("invoking human: sudo uid of a known account") == value("invoking human: sudo uid padded") == value("invoking human: the process environment") == "p469-human"
          and value("invoking human: sudo uid 0") == value("invoking human: sudo uid not a number") == value("invoking human: no sudo uid") == "p469-self"
          and value("invoking human: sudo uid of an unknown account") == value("invoking human: an unknown own uid") == "",
          "the human is sudo's own uid when it is a real non-root number, else the process's own; an unknown account is nobody")
    commands = value("commands for 'alice'")
    check(value("commands for ''") == [] and len(commands) == 7 and commands[0] == "# Lifecycle control for alice, the human who provisioned p469."
          and commands[2] == "sudo install -d -m 0755 -o root -g root '/usr/local/lib/switchyard/p469'"
          and commands[-2] == "sudo visudo -c -f '/etc/sudoers.d/49-p469-tenant-control.staged'"
          and commands[-1] == "sudo mv '/etc/sudoers.d/49-p469-tenant-control.staged' '/etc/sudoers.d/49-p469-tenant-control'"
          and value("commands: the control root rebound on project_provision")[2] == "sudo install -d -m 0755 -o root -g root '/p469/control/p469'"
          and value("commands: the quoting rebound on project_provision")[2] == "sudo install -d -m 0755 -o root -g root </usr/local/lib/switchyard/p469>"
          and calls("commands: the paths and documents rebound on project_provision") == {"shell_quote": 8, "tenant_control_grant_document rebound": 1,
                                                                                         "tenant_control_grant_path rebound": 1, "tenant_control_sudoers_document rebound": 1,
                                                                                         "tenant_control_sudoers_path rebound": 1},
          "the commands: nothing without a human; the grant directory, both files at fixed modes, sudoers checked by visudo before it moves in -- every path, document and quote read through project_provision")
    grant = __import__("json").loads(value("grant document for 'alice'"))
    check(value("grant document for ''") == "" and grant == {"authorized_user": "alice", "launcher": "/opt/switchyard/current/switchyard", "owner": "p469-owner", "project": "p469"}
          and __import__("json").loads(value("grant document: the launcher rebound on project_provision"))["launcher"] == "/p469/launcher"
          and value("grant document: SHARED_RELEASE_CURRENT rebound after load changes nothing") == value("grant document for 'alice'"),
          "the grant names the project, owner, human and the launcher -- TENANT_CONTROL_LAUNCHER, read on project_provision when it runs, bound from the release when it loaded")
    sudoers = [value(f"sudoers document for {who!r}") for who in ("alice", "it's alice")]
    check(value("sudoers document for ''") == "" and len(set(sudoers)) == 2
          and value("sudoers document: the helper paths rebound on project_provision") not in sudoers
          and calls("sudoers document for 'alice'") == {"display_attach_helper_path": 1, "tenant_control_helper_path": 1},
          "the sudoers rule names the human and the two helpers, whose paths it reads through project_provision")
    check(value("plan grant for ''") == value("plan sudoers for ''") == ""
          and value("plan grant for 'alice'") == value("grant document for 'alice'") + "\n"
          and value("plan grant: the document rebound on project_provision").startswith("GRANT ") and value("plan sudoers: the document rebound on project_provision") == "SUDOERS p469 alice\n",
          "a plan's grant and sudoers are the documents for its control user, newline-terminated, or nothing")
    check(value("constants") == ["/usr/local/lib/switchyard", "control-grant.json", "/opt/switchyard/current/switchyard", "/opt/switchyard/current"],
          "the control root, the grant name, the launcher and the release it is bound from")


def test_every_seam_is_reached() -> None:
    # Every function the twelve read on project_provision is a recorder there; the constants are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions <= set(REBINDABLE), f"every other seam is a rebindable name: {sorted(names - functions - set(REBINDABLE))}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_the_launcher_stays_on_project_provision_bound_when_it_loads", "test_the_direct_script_answers_what_the_package_answers",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_tenant_control_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
