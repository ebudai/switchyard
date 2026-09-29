#!/usr/bin/env python3
"""SYRD-471: the board service's host artifacts, against project_provision they came out of.

The twelve -- the three systemd units (`render_board_unit`,
`render_listener_unit`, `render_canary_unit`) and the helpers only they use
(`env_list`, `env_operation_role_map`, `listener_board_url`,
`default_ticket_board_python` with `DEFAULT_SHARED_PYTHON`), the tmpfiles line
(`render_tmpfiles`), the polkit rule (`render_polkit_rule`), the database SQL
(`render_database_sql`) and `tenant_primary_group` -- moved unchanged into
`scripts/ticket_board/provision_board_service.py`; `project_provision`
re-exports them all, in both branches of its import block, and keeps
`systemd_environment` and `sql_identifier`, and re-exports `role_accounts_env`.
This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first. The module
  alone loads only its package; `team_launcher`'s two imports are its objects.
- **Seams (rule 24):** what they read of `project_provision` -- each other, the
  default shared Python and the three names above -- is read through it when
  they run, with the direct-script fallback, so a patch there reaches them;
  the callers are counted in `project_provision` and any later slice's module.
- **Readers:** every production module imports them from `project_provision`,
  as often as before.
- **The behaviour is the baseline's:** the helpers; the board Python for the
  override, an executable, non-executable and missing shared Python and the
  default; the socket group for every kind of owner; every unit, URL, tmpfiles,
  polkit and database rendering for default, role-account, pgu and shaped
  plans. `GOLDEN` below was produced by running the BASELINE module's own
  definitions over the very cases embedded here (`gold471.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME, USER
  and COLUMNS, under umask 077, under several hash seeds and with another
  TMPDIR, locale and board Python in the environment.
- **The direct script answers what the package answers** and looks at no host
  path (a recorder with a positive control), and the default, shaped, lean and
  roles packets are byte-identical through both.

No real home, tenant, account, group, /etc, /var or /opt path is read or
written: every path is synthetic, the board Python is chosen only among files a
case creates (or pinned), and account and group lookups are stood in. Spawns,
every exec, signals, real account and group lookups and socket connections are
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
from scripts.ticket_board import provision_board_service as m  # noqa: E402

CHECKS = 0
MOVED = ('DEFAULT_SHARED_PYTHON', 'env_list', 'env_operation_role_map', 'default_ticket_board_python', 'render_board_unit', 'tenant_primary_group', 'listener_board_url', 'render_listener_unit', 'render_canary_unit', 'render_tmpfiles', 'render_polkit_rule', 'render_database_sql')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'env_operation_role_map': {'env_list': 1},
    'default_ticket_board_python': {'DEFAULT_SHARED_PYTHON': 1},
    'render_board_unit': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'role_accounts_env': 1, 'systemd_environment': 1},
    'render_listener_unit': {'default_ticket_board_python': 1, 'listener_board_url': 1, 'role_accounts_env': 1},
    'render_canary_unit': {'default_ticket_board_python': 1},
    'render_database_sql': {'sql_identifier': 1},
}
#: Measured on the baseline: every project_provision definition outside the twelve that names them, and how often.
DISPATCH = {'write_artifacts': {'render_board_unit': 1, 'render_canary_unit': 1, 'render_database_sql': 1, 'render_listener_unit': 1, 'render_polkit_rule': 1, 'render_tmpfiles': 1}, 'main': {'render_board_unit': 1, 'render_database_sql': 1, 'render_listener_unit': 1, 'render_polkit_rule': 1, 'render_tmpfiles': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/team_launcher.py': {'import render_board_unit': 1, 'import render_canary_unit': 1}}
#: The BASELINE's own behaviour for the cases below (`gold471.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'env list': {'result': {'type': 'str', 'value': 'a,b,c'}, 'calls': {}},
    'env list: empty': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'operation role map': {'result': {'type': 'str', 'value': 'close=director,ops;add=main;none='}, 'calls': {'env_list': 3}},
    'operation role map: the list rebound on project_provision': {'result': {'type': 'str', 'value': 'close=director|ops'}, 'calls': {'env_list rebound': 1}},
    'board python: override': {'result': {'type': 'str', 'value': '/p471/override/python'}, 'calls': {}},
    'board python: an executable shared python': {'result': {'type': 'str', 'value': 'TMP/python-exe'}, 'calls': {}},
    'board python: a shared python that is not executable': {'result': {'type': 'str', 'value': '/usr/bin/python3'}, 'calls': {}},
    'board python: a missing shared python': {'result': {'type': 'str', 'value': '/usr/bin/python3'}, 'calls': {}},
    'board python: the default rebound to an executable': {'result': {'type': 'str', 'value': 'TMP/python-exe'}, 'calls': {}},
    'board python: the default rebound to a missing file': {'result': {'type': 'str', 'value': '/usr/bin/python3'}, 'calls': {}},
    "primary group of ''": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "primary group of '  '": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "primary group of 'p471-owner'": {'result': {'type': 'str', 'value': 'p471-owner'}, 'calls': {}},
    "primary group of ' p471-owner '": {'result': {'type': 'str', 'value': 'p471-owner'}, 'calls': {}},
    "primary group of 'p471-orphan'": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    "primary group of 'p471-stranger'": {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'render_board_unit: default': {'result': {'type': 'str', 'value': 'TEXT 2078 chars sha256:46a0ff41e57ed8bc'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: default': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    'render_listener_unit: default': {'result': {'type': 'str', 'value': 'TEXT 1282 chars sha256:375b764c1733fe34'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: default': {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: default': {'result': {'type': 'str', 'value': 'd /p471/home/.claude/p471-ticket-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: default': {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    'render_database_sql: default': {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:0eb7fbc998038161'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: role accounts': {'result': {'type': 'str', 'value': 'TEXT 2111 chars sha256:a27f78418641b121'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'role_accounts_env': 1, 'systemd_environment': 1}},
    'listener_board_url: role accounts': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    'render_listener_unit: role accounts': {'result': {'type': 'str', 'value': 'TEXT 1316 chars sha256:396e10905ae130d6'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1, 'role_accounts_env': 1}},
    'render_canary_unit: role accounts': {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: role accounts': {'result': {'type': 'str', 'value': 'd /p471/home/.claude/p471-ticket-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: role accounts': {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    'render_database_sql: role accounts': {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:0eb7fbc998038161'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: operation roles': {'result': {'type': 'str', 'value': 'TEXT 2155 chars sha256:9848242b00c1cf32'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 6, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: operation roles': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    'render_listener_unit: operation roles': {'result': {'type': 'str', 'value': 'TEXT 1282 chars sha256:375b764c1733fe34'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: operation roles': {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: operation roles': {'result': {'type': 'str', 'value': 'd /p471/home/.claude/p471-ticket-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: operation roles': {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    'render_database_sql: operation roles': {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:0eb7fbc998038161'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: a frame directory': {'result': {'type': 'str', 'value': 'TEXT 2028 chars sha256:f1a37e35ddd1ecf6'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: a frame directory': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    'render_listener_unit: a frame directory': {'result': {'type': 'str', 'value': 'TEXT 1282 chars sha256:375b764c1733fe34'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: a frame directory': {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: a frame directory': {'result': {'type': 'str', 'value': 'd /p471/frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: a frame directory': {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    'render_database_sql: a frame directory': {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:0eb7fbc998038161'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: a database and roles': {'result': {'type': 'str', 'value': 'TEXT 2056 chars sha256:a22c8237505b4159'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: a database and roles': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    'render_listener_unit: a database and roles': {'result': {'type': 'str', 'value': 'TEXT 1259 chars sha256:aa6ede462f594432'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: a database and roles': {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: a database and roles': {'result': {'type': 'str', 'value': 'd /p471/home/.claude/p471-ticket-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: a database and roles': {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    'render_database_sql: a database and roles': {'result': {'type': 'str', 'value': 'TEXT 720 chars sha256:b406027d23b0625e'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: no roles group': {'result': {'type': 'str', 'value': 'TEXT 1981 chars sha256:ae4bd004c6373456'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: no roles group': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    'render_listener_unit: no roles group': {'result': {'type': 'str', 'value': 'TEXT 1282 chars sha256:375b764c1733fe34'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: no roles group': {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: no roles group': {'result': {'type': 'str', 'value': 'd /p471/home/.claude/p471-ticket-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: no roles group': {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    'render_database_sql: no roles group': {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:0eb7fbc998038161'}, 'calls': {'sql_identifier': 1}},
    "render_board_unit: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'TEXT 2034 chars sha256:f5da258b1f05aae1'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    "listener_board_url: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'http://127.0.0.1:34471'}, 'calls': {}},
    "render_listener_unit: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'TEXT 1282 chars sha256:375b764c1733fe34'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    "render_canary_unit: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'TEXT 855 chars sha256:b47c963a066eba96'}, 'calls': {'default_ticket_board_python': 1}},
    "render_tmpfiles: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'd /tmp/pgu-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    "render_polkit_rule: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'TEXT 1230 chars sha256:99b7c3c4087682f1'}, 'calls': {}},
    "render_database_sql: pgu's frame directory in another project": {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:0eb7fbc998038161'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: pgu': {'result': {'type': 'str', 'value': 'TEXT 2011 chars sha256:79bb26f6e49d4739'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: pgu': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34472'}, 'calls': {}},
    'render_listener_unit: pgu': {'result': {'type': 'str', 'value': 'TEXT 1231 chars sha256:f590b12fcd01145a'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: pgu': {'result': {'type': 'str', 'value': 'TEXT 850 chars sha256:a6461e7cf6c5243f'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: pgu': {'result': {'type': 'str', 'value': 'd /tmp/pgu-frames 1777 root root -\n'}, 'calls': {}},
    'render_polkit_rule: pgu': {'result': {'type': 'str', 'value': 'TEXT 1227 chars sha256:b959d3ba89f4e531'}, 'calls': {}},
    'render_database_sql: pgu': {'result': {'type': 'str', 'value': 'TEXT 752 chars sha256:1f691d27bf601e71'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: pgu with the shared frames': {'result': {'type': 'str', 'value': 'TEXT 2011 chars sha256:79bb26f6e49d4739'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: pgu with the shared frames': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34472'}, 'calls': {}},
    'render_listener_unit: pgu with the shared frames': {'result': {'type': 'str', 'value': 'TEXT 1231 chars sha256:f590b12fcd01145a'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: pgu with the shared frames': {'result': {'type': 'str', 'value': 'TEXT 850 chars sha256:a6461e7cf6c5243f'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: pgu with the shared frames': {'result': {'type': 'str', 'value': 'd /tmp/pgu-frames 1777 root root -\n'}, 'calls': {}},
    'render_polkit_rule: pgu with the shared frames': {'result': {'type': 'str', 'value': 'TEXT 1227 chars sha256:b959d3ba89f4e531'}, 'calls': {}},
    'render_database_sql: pgu with the shared frames': {'result': {'type': 'str', 'value': 'TEXT 752 chars sha256:1f691d27bf601e71'}, 'calls': {'sql_identifier': 1}},
    'render_board_unit: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'TEXT 2013 chars sha256:f1d38c3abd5c6316'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'listener_board_url: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'http://127.0.0.1:34472'}, 'calls': {}},
    'render_listener_unit: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'TEXT 1231 chars sha256:f590b12fcd01145a'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url': 1}},
    'render_canary_unit: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'TEXT 850 chars sha256:a6461e7cf6c5243f'}, 'calls': {'default_ticket_board_python': 1}},
    'render_tmpfiles: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'd /p471/pgu-frames 0775 p471-owner p471-owner -\n'}, 'calls': {}},
    'render_polkit_rule: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'TEXT 1227 chars sha256:b959d3ba89f4e531'}, 'calls': {}},
    'render_database_sql: pgu with its own frame directory': {'result': {'type': 'str', 'value': 'TEXT 752 chars sha256:1f691d27bf601e71'}, 'calls': {'sql_identifier': 1}},
    'board unit: the python rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 2086 chars sha256:2b6b6a797f5359b5'}, 'calls': {'default_ticket_board_python rebound': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment': 1}},
    'board unit: the environment quoting rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 2078 chars sha256:ced9555be8d34250'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'systemd_environment rebound': 1}},
    'board unit: the role map rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 2082 chars sha256:beefcaefb0aa5630'}, 'calls': {'default_ticket_board_python': 1, 'env_list': 4, 'env_operation_role_map': 1, 'role_accounts_env rebound': 1, 'systemd_environment': 1}},
    'listener unit: the board URL rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 1285 chars sha256:2b9e7d693a956830'}, 'calls': {'default_ticket_board_python': 1, 'listener_board_url rebound': 1}},
    'canary unit: the python rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 863 chars sha256:6eb8bcfa3ab4c49a'}, 'calls': {'default_ticket_board_python rebound': 1}},
    'database sql: the identifier quoting rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 810 chars sha256:fdbf02f7ef0a2c01'}, 'calls': {'sql_identifier rebound': 1}},
    'constant': {'result': {'type': 'str', 'value': '/opt/switchyard/venv/bin/python'}, 'calls': {}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = {'default': 10, 'shaped': 10, 'lean': 10, 'roles': 10}

# --- the cases, shared verbatim with `gold471.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the eleven and records the answer or the exact exception, and how many times it called each of
# `project_provision`'s helpers (recorded there and passed through). Projects, owners and plans are synthetic (p471...);
# plans are built by `build_plan` for a synthetic owner and shaped with `dataclasses.replace`. No account or group is
# looked up: `pwd.getpwnam` and `grp.getgrgid` are stood in for each case. TICKET_BOARD_PYTHON and SWITCHYARD_SHARED_PYTHON
# are cleared for each case and set only as it names them; the board Python is chosen only among files the case creates,
# or the default rebound on `project_provision` -- no host path is checked. A case may rebind a `project_provision` name
# to show it is read there when the function runs.
PYTHONS = ("override", "an executable shared python", "a shared python that is not executable", "a missing shared python",
           "the default rebound to an executable", "the default rebound to a missing file")
PLANS = {"default": {}, "role accounts": {"role_accounts": (("director", "p471-director"), ("main", "p471-main")), "roles_group": "p471-roles"},
         "operation roles": {"operation_allowed_roles": (("close", ("director", "ops")), ("add", ("main",)))}, "a frame directory": {"frame_dir": "/p471/frames"},
         "a database and roles": {"database": "p471 db", "service_role": "p471_svc", "listener_role": "p471_lsn"},
         "no roles group": {"roles_group": ""}, "pgu's frame directory in another project": {"frame_dir": "/tmp/pgu-frames"}}
PGU = {"pgu": {}, "pgu with the shared frames": {"frame_dir": "/tmp/pgu-frames"}, "pgu with its own frame directory": {"frame_dir": "/p471/pgu-frames"}}
RENDERERS = ("render_board_unit", "listener_board_url", "render_listener_unit", "render_canary_unit", "render_tmpfiles", "render_polkit_rule", "render_database_sql")
CASES = {
    "env list": {"call": "env_list", "args": [["a", "b", "c"]]},
    "env list: empty": {"call": "env_list", "args": [[]]},
    "operation role map": {"call": "env_operation_role_map", "args": [[["close", ["director", "ops"]], ["add", ["main"]], ["none", []]]]},
    "operation role map: the list rebound on project_provision": {"call": "env_operation_role_map", "args": [[["close", ["director", "ops"]]]], "rebind": {"env_list": True}},
    **{f"board python: {p}": {"call": "default_ticket_board_python", "python": p} for p in PYTHONS},
    **{f"primary group of {who!r}": {"call": "tenant_primary_group", "args": [who]} for who in ("", "  ", "p471-owner", " p471-owner ", "p471-orphan", "p471-stranger")},
    **{f"{what}: {p}": {"call": what, "plan": p} for p in (*PLANS, *PGU) for what in RENDERERS},
    "board unit: the python rebound on project_provision": {"call": "render_board_unit", "plan": "default", "rebind": {"default_ticket_board_python": True}},
    "board unit: the environment quoting rebound on project_provision": {"call": "render_board_unit", "plan": "default", "rebind": {"systemd_environment": True}},
    "board unit: the role map rebound on project_provision": {"call": "render_board_unit", "plan": "role accounts", "rebind": {"role_accounts_env": True}},
    "listener unit: the board URL rebound on project_provision": {"call": "render_listener_unit", "plan": "default", "rebind": {"listener_board_url": True}},
    "canary unit: the python rebound on project_provision": {"call": "render_canary_unit", "plan": "default", "rebind": {"default_ticket_board_python": True}},
    "database sql: the identifier quoting rebound on project_provision": {"call": "render_database_sql", "plan": "default", "rebind": {"sql_identifier": True}},
    "constant": {"call": None},
}
FUNCTIONS = ("env_list", "env_operation_role_map", "default_ticket_board_python", "render_board_unit", "tenant_primary_group", "listener_board_url",
             "render_listener_unit", "render_canary_unit", "render_tmpfiles", "render_polkit_rule", "render_database_sql")
PASSED = ("env_list", "env_operation_role_map", "default_ticket_board_python", "listener_board_url", "systemd_environment", "sql_identifier", "role_accounts_env")
REBINDABLE = ("DEFAULT_SHARED_PYTHON",)


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import dataclasses as _dc, grp as _grp, hashlib, os as _os, pathlib as _pl, pwd as _pwd, shutil as _sh, tempfile as _tf
    counts = {}
    work = _tf.mkdtemp(prefix="syrd471-case.")

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, _pl.PurePath):
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

    users, groups = {"p471-owner": 4710, "p471-orphan": 4719}, {4710: "p471-owner"}

    def getpwnam(name):
        if name not in users:
            raise KeyError(f"getpwnam(): name not found: {name!r}")
        return _pwd.struct_passwd((name, "x", users[name], users[name], "", "/nonexistent", "/bin/false"))

    def getgrgid(gid):
        if gid not in groups:
            raise KeyError(f"getgrgid(): gid not found: {gid}")
        return _grp.struct_group((groups[gid], "x", gid, []))

    ENV = ("TICKET_BOARD_PYTHON", "SWITCHYARD_SHARED_PYTHON")
    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "build_plan")}
    saved_env, saved_lookups = {n: _os.environ.get(n) for n in ENV}, (_pwd.getpwnam, _grp.getgrgid)
    try:
        _pwd.getpwnam, _grp.getgrgid = getpwnam, getgrgid
        for n in ENV:
            _os.environ.pop(n, None)
        exe, plain, missing = f"{work}/python-exe", f"{work}/python-plain", f"{work}/python-missing"
        open(exe, "w").close(); _os.chmod(exe, 0o755)
        open(plain, "w").close(); _os.chmod(plain, 0o644)
        python = spec.get("python")
        if python == "override":
            _os.environ["TICKET_BOARD_PYTHON"] = "/p471/override/python"
        elif python in ("an executable shared python", "a shared python that is not executable", "a missing shared python"):
            _os.environ["SWITCHYARD_SHARED_PYTHON"] = {"an executable shared python": exe, "a shared python that is not executable": plain}.get(python, missing)
        elif python in ("the default rebound to an executable", "the default rebound to a missing file"):
            t.DEFAULT_SHARED_PYTHON = exe if python == "the default rebound to an executable" else missing
        elif spec["call"] != "default_ticket_board_python":
            _os.environ["TICKET_BOARD_PYTHON"] = "/p471/python"
        args = list(spec.get("args", []))
        if "plan" in spec:
            if spec["plan"] in PLANS:
                plan = _dc.replace(saved["build_plan"](project="p471", owner_user="p471-owner", owner_home=_pl.Path("/p471/home"), port=34471), **PLANS[spec["plan"]])
            else:
                plan = _dc.replace(saved["build_plan"](project="pgu", owner_user="p471-owner", owner_home=_pl.Path("/p471/home"), port=34472), **PGU[spec["plan"]])
            args = [plan] + args
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "env_list":
                t.env_list = lambda values: note("env_list rebound") or "|".join(values)
            elif name == "default_ticket_board_python":
                t.default_ticket_board_python = lambda: note("default_ticket_board_python rebound") or "/p471/rebound/python"
            elif name == "systemd_environment":
                t.systemd_environment = lambda name, value: note("systemd_environment rebound") or f"Environment=<{name}={value}>"
            elif name == "role_accounts_env":
                t.role_accounts_env = lambda plan: note("role_accounts_env rebound") or "ROLE-MAP"
            elif name == "listener_board_url":
                t.listener_board_url = lambda plan: note("listener_board_url rebound") or f"http://p471.invalid:{plan.port}"
            elif name == "sql_identifier":
                t.sql_identifier = lambda value: note("sql_identifier rebound") or f"<{value}>"
            else:
                setattr(t, name, value)
        if spec["call"] is None:
            got = saved["DEFAULT_SHARED_PYTHON"]
        else:
            fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
            try:
                got = fn(*args)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
                return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": dict(sorted(counts.items()))}
        return {"result": {"type": type(got).__name__, "value": norm(got)}, "calls": dict(sorted(counts.items()))}
    finally:
        for n, v in saved.items():
            setattr(t, n, v)
        _pwd.getpwnam, _grp.getgrgid = saved_lookups
        for n, v in saved_env.items():
            _os.environ.pop(n, None)
            if v is not None:
                _os.environ[n] = v
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


#: What the eleven read on project_provision when they run: its own, or re-exported there by an earlier slice.
KEPT = ("systemd_environment", "sql_identifier", "role_accounts_env")
#: The one constant, as the baseline wrote it: (annotation, value).
CONSTANT_TEXT = {"DEFAULT_SHARED_PYTHON": (None, "'/opt/switchyard/venv/bin/python'")}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them: none.
DEFAULTS = {name: [] for name in ("env_list", "env_operation_role_map", "default_ticket_board_python", "render_board_unit", "tenant_primary_group",
                                  "listener_board_url", "render_listener_unit", "render_canary_unit", "render_tmpfiles", "render_polkit_rule", "render_database_sql")}
#: team_launcher's module-level imports of them, from project_provision.
LAUNCHER_IMPORTS = ("render_board_unit", "render_canary_unit")
#: The packets rendered through the direct script and the package: the default plan, two the plan shapes, and one given
#: role accounts and a roles group, whose units carry the role map. The board Python is pinned in every run.
ROLE_ACCOUNTS = (("director", "p471-director"), ("main", "p471-main"))
PACKET_VARIANTS = {
    "default": ([], False),
    "shaped": (["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"], False),
    "lean": (["--no-include-designer", "--no-include-audit"], False),
    "roles": ([], True),
}
#: Refuses and records any stat/lstat/open/access under the host's switchyard, /etc, /var and /opt paths, after proving it
#: catches one (a positive control): what the probe below renders must not look at the host.
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
    "pathlib.Path('/opt/switchyard/syrd471-positive-control').is_file()\n"
    "control = len(hits); hits.clear()\n"
)
#: One probe, run in the package and as the direct script: every helper, the board Python among files the probe creates
#: and the default rebound (on every holder, since runpy returns a copy of the script's globals), the primary group with
#: pwd/grp stood in, and every rendering for a synthetic plan -- as a digest, and the host paths it looked at.
BOARD_PROBE = (
    "import dataclasses, grp, hashlib, json, os, pathlib, pwd, sys, tempfile\n"
    "pwd.getpwnam = lambda name: pwd.struct_passwd((name, 'x', 4710, 4710, '', '/nonexistent', '/bin/false'))\n"
    "grp.getgrgid = lambda gid: grp.struct_group(('p471-owner', 'x', gid, []))\n"
    "[os.environ.pop(n, None) for n in ('TICKET_BOARD_PYTHON', 'SWITCHYARD_SHARED_PYTHON')]\n"
    "work = tempfile.mkdtemp(prefix='syrd471-probe-'); exe = work + '/python'; open(exe, 'w').close(); os.chmod(exe, 0o755)\n"
    "holders = [sys.modules[n] for n in ('project_provision', 'scripts.ticket_board.project_provision') if n in sys.modules] + [g['default_ticket_board_python'].__globals__]\n"
    "def rebind(value):\n"
    "    for h in holders:\n"
    "        if isinstance(h, dict): h['DEFAULT_SHARED_PYTHON'] = value\n"
    "        else: h.DEFAULT_SHARED_PYTHON = value\n"
    "rebind(exe); chosen = g['default_ticket_board_python'](); rebind(work + '/missing'); fallback = g['default_ticket_board_python']()\n"
    "os.environ['TICKET_BOARD_PYTHON'] = '/p471/python'\n"
    "plan = dataclasses.replace(g['build_plan'](project='p471', owner_user='p471-owner', owner_home=pathlib.Path('/p471/home'), port=34471),\n"
    "                           role_accounts=(('director', 'p471-director'), ('main', 'p471-main')), roles_group='p471-roles')\n"
    "out = [g['env_list'](['a', 'b']), g['env_operation_role_map']([('close', ['director'])]), chosen.replace(work, 'WORK'), fallback, g['tenant_primary_group']('p471-owner'),\n"
    "       g['listener_board_url'](plan), g['render_board_unit'](plan), g['render_listener_unit'](plan), g['render_canary_unit'](plan),\n"
    "       g['render_tmpfiles'](plan), g['render_polkit_rule'](plan), g['render_database_sql'](plan), g['default_ticket_board_python']()]\n"
    "os.unlink(exe); os.rmdir(work)\n"
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
    result = python("import sys, scripts.ticket_board.provision_board_service as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_board_service", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_board_service"),
                  ("scripts.team_launcher", "scripts.ticket_board.provision_board_service")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_board_service as m, scripts.team_launcher as tl; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        f"all(getattr(tl, n) is getattr(m, n) for n in {LAUNCHER_IMPORTS!r}), "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_board_service'] True True",
              f"{' then '.join(order)}: one object each, defined here; team_launcher's two imports the module's own; nothing of project_provision bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    check(m.Path is Path and m.Sequence is Sequence and m.os is os and m.pwd is pwd and m.grp is grp and t.os is m.os,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it. Both runs record any host path they look at.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(HOST_RECORDER + f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd471_script__'); "
                       "m = sys.modules['provision_board_service']; import project_provision\n"
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + BOARD_PROBE)
    as_package = python(HOST_RECORDER + "import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + BOARD_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("13 ")
          and lines[1].endswith(" control 1 host paths 0"),
          f"as a direct script: the script holds the module's own objects, and all thirteen answers are the package's, byte for byte, "
          f"with no host path looked at (the recorder's positive control caught): {as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_board_service.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "import grp", "import os", "import pwd", "from pathlib import Path", "from typing import Sequence"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the one constant is the baseline's: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the twelve, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_board_service" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_board_service" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the twelve, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_board_service")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read, its own or re-exported")
    # The definitions that name them are counted wherever they live -- project_provision, or a later slice's module, whose
    # read through project_provision (provision.X) counts as the name -- so this check survives the next slice.
    later = [ast.parse((ROOT / "scripts" / "ticket_board" / f"{n.module}.py").read_text(encoding="utf-8")) for n in guard.body
             if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("provision_") and n.level == 1 and n.module != "provision_board_service"]
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
    base = Path(tempfile.mkdtemp(prefix="syrd471-packet-")).resolve()
    try:
        for variant, (extra, roles) in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p471", "--owner-user", "p471-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34471", "--output-dir", f"{work}/out", *extra]
                given = f"dataclasses.replace(build(*a, **k), role_accounts={ROLE_ACCOUNTS!r}, roles_group='p471-roles')" if roles else "build(*a, **k)"
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                pin = "import os; os.environ['TICKET_BOARD_PYTHON'] = '/p471/python'; "
                if mode == "script":
                    probe = (pin + f"import dataclasses, runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd471_script'); main = g['main']; build = main.__globals__['build_plan']; "
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
            files = outputs["package"][2]
            units = [files.get(f"p471-ticket-board{suffix}.service", b"") for suffix in ("", "-notify-listener", "-canary")]
            check(all(b"/p471/python" in unit for unit in units) and b"p471-ticket-board.service" in files.get("49-p471-ticket-board-deploy.rules", b"")
                  and b"CREATE DATABASE" in files.get("p471-database.sql", b"") and bool(files.get("p471-ticket-board.conf")),
                  f"{variant}: the three units run the pinned Python, the polkit rule names the board unit, the database SQL and tmpfiles are written")
            role_map = b"Environment=TICKET_BOARD_ROLE_ACCOUNTS=director=p471-director,main=p471-main" in units[0]
            check(role_map == roles, f"{variant}: the board unit carries the role map exactly when the plan has role accounts")
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

    check(value("env list") == "a,b,c" and value("env list: empty") == "" and value("operation role map") == "close=director,ops;add=main;none="
          and value("operation role map: the list rebound on project_provision") == "close=director|ops",
          "the unit's lists: comma-separated, and operation=roles pairs separated by ';' -- the list read through project_provision")
    check(value("board python: override") == "/p471/override/python" and value("board python: an executable shared python") == "TMP/python-exe"
          and value("board python: a shared python that is not executable") == value("board python: a missing shared python") == "/usr/bin/python3"
          and value("board python: the default rebound to an executable") == "TMP/python-exe" and value("board python: the default rebound to a missing file") == "/usr/bin/python3",
          "the board's Python: TICKET_BOARD_PYTHON, else the shared Python when it is an executable file, else /usr/bin/python3 -- the default read through project_provision")
    check([value(f"primary group of {w!r}") for w in ("", "  ", "p471-owner", " p471-owner ", "p471-orphan", "p471-stranger")] == ["", "", "p471-owner", "p471-owner", "", ""],
          "the socket group: the owner's primary group, or empty for no owner, an unknown owner or an unknown group")
    check(all(value(f"listener_board_url: {p}") == "http://127.0.0.1:34471" for p in PLANS) and value("listener_board_url: pgu") == "http://127.0.0.1:34472",
          "the listener's board: the loopback address the board unit binds, at the plan's port")
    check(value("render_tmpfiles: a frame directory") == "d /p471/frames 0775 p471-owner p471-owner -\n"
          and value("render_tmpfiles: pgu with the shared frames") == "d /tmp/pgu-frames 1777 root root -\n"
          and value("render_tmpfiles: pgu") == value("render_tmpfiles: pgu with the shared frames")
          and value("render_tmpfiles: pgu with its own frame directory") == "d /p471/pgu-frames 0775 p471-owner p471-owner -\n",
          "the frame directory: the owner's, 0775 -- except pgu's shared /tmp/pgu-frames (its default), sticky and root's")
    check(value("render_board_unit: role accounts") != value("render_board_unit: default") and value("render_board_unit: operation roles") != value("render_board_unit: default")
          and calls("render_board_unit: role accounts").get("role_accounts_env") == 1 and "role_accounts_env" not in calls("render_board_unit: default")
          and value("board unit: the python rebound on project_provision") != value("render_board_unit: default")
          and value("board unit: the environment quoting rebound on project_provision") != value("render_board_unit: default")
          and value("board unit: the role map rebound on project_provision") != value("render_board_unit: role accounts"),
          "the board unit: the role map only with role accounts, the operation roles only when there are some -- the Python, quoting and role map read through project_provision")
    check(value("listener unit: the board URL rebound on project_provision") != value("render_listener_unit: default")
          and value("canary unit: the python rebound on project_provision") != value("render_canary_unit: default")
          and calls("render_listener_unit: role accounts").get("role_accounts_env") == 1,
          "the listener and canary units: the board URL, the role map and the Python read through project_provision")
    check(value("render_polkit_rule: default") != value("render_polkit_rule: pgu") and value("render_polkit_rule: default") == value("render_polkit_rule: role accounts"),
          "the polkit rule names the project's own units and owner, and nothing about role accounts")
    check(value("render_database_sql: a database and roles") != value("render_database_sql: default")
          and value("database sql: the identifier quoting rebound on project_provision") != value("render_database_sql: default")
          and calls("render_database_sql: default") == {"sql_identifier": 1},
          "the database SQL names the plan's database and roles -- the identifier quoted through project_provision")
    check(value("constant") == "/opt/switchyard/venv/bin/python", "the shared Python the board runs when it is installed")


def test_every_seam_is_reached() -> None:
    # Every function the eleven read on project_provision is a recorder there; the constant is rebound by its own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions <= set(REBINDABLE), f"every other seam is a rebindable name: {sorted(names - functions - set(REBINDABLE))}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_the_direct_script_answers_what_the_package_answers_and_looks_at_no_host_path",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_board_service_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
