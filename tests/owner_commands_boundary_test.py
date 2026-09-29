#!/usr/bin/env python3
"""SYRD-459: the owner-command and user-manager helpers, against the launcher they came out of.

The four -- `_owner_command_args`, `_owner_command_env_args`,
`_tenant_owner_home` and `_owner_user_systemctl` -- moved unchanged into
`scripts/owner_commands.py`; the launcher re-exports them all and keeps
`current_user_name` and `capture_installed_units`. This pins what makes that
safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher, and imports the
  config type only for annotations.
- **Seams (rule 24):** everything the four read of the launcher -- each other,
  the current user, the PATH helpers, the presentation variables, the plan
  record and the account home lookup -- is read through it as often as before,
  so a patch there reaches them.
- **Readers:** the launcher's own caller and every production module read them
  through the launcher, as often as before.
- **The behaviour is the baseline's,** argv only: the sudo prefix, the env
  HOME, PATH and presentation variables, the recorded-then-account-then-/home
  owner home, and the `systemctl --user` script with its exported runtime
  directory and bus, daemon-reload order and quoting. `GOLDEN` below was
  produced by running the BASELINE launcher's own definitions over the very
  cases embedded here (`gold459.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with stray terminal and bin
  variables.

Nothing is run: the four only build argv. The current user, the account home
lookup and the plan record are stand-ins on the launcher, with synthetic users,
homes and units; no user manager, service or tenant is touched. Spawns, every
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
from scripts import owner_commands as m  # noqa: E402

CHECKS = 0
MOVED = ('_owner_command_args', '_owner_command_env_args', '_tenant_owner_home', '_owner_user_systemctl')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_owner_command_args': {'current_user_name': 1},
    '_owner_command_env_args': {'DEFAULT_PANE_BASE_PATH': 1, '_owner_command_args': 1, '_owner_home_bin_dirs': 1, '_prepend_paths': 1, '_terminal_presentation_env': 1},
    '_tenant_owner_home': {'_plan_data_from_config': 1, 'current_user_name': 1, 'home_dir_for_user': 1},
    '_owner_user_systemctl': {'_owner_command_env_args': 1, '_tenant_owner_home': 1, 'current_user_name': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the four that names them, and how often.
DISPATCH = {'_owner_catalog_args': {'_owner_command_env_args': 1}}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/board_services.py': {'launcher._owner_user_systemctl': 1, 'launcher._tenant_owner_home': 1}, 'scripts/first_run_auth.py': {'launcher._owner_command_env_args': 2}, 'scripts/github_identity.py': {'launcher._owner_command_env_args': 1}, 'scripts/project_desktop.py': {'launcher._owner_command_args': 1}, 'scripts/provider_auth_status.py': {'launcher._owner_command_env_args': 1}, 'scripts/provider_session.py': {'launcher._owner_command_env_args': 1}, 'scripts/role_plan_prompt.py': {'launcher._owner_command_env_args': 1}, 'scripts/upgrade_phases.py': {'launcher._tenant_owner_home': 1}}
#: The BASELINE's own behaviour for the cases below (`gold459.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'command: the caller is the owner': {'result': {'type': 'list', 'value': ['claude', '--resume']}, 'calls': [['current_user_name', [], {}]]},
    'command: another owner': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'claude', '--resume']}, 'calls': [['current_user_name', [], {}]]},
    'command: a tuple command': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'codex', 'login']}, 'calls': [['current_user_name', [], {}]]},
    'command: a tuple command, the caller': {'result': {'type': 'list', 'value': ['codex', 'login']}, 'calls': [['current_user_name', [], {}]]},
    'command: no owner, no caller name': {'result': {'type': 'list', 'value': ['id']}, 'calls': [['current_user_name', [], {}]]},
    'command: no owner, a caller': {'result': {'type': 'list', 'value': ['sudo', '-u', '', 'id']}, 'calls': [['current_user_name', [], {}]]},
    'env: another owner': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: the caller is the owner': {'result': {'type': 'list', 'value': ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: presentation variables': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'TERM=xterm-256color', 'COLORTERM=truecolor', 'TERM_PROGRAM_VERSION=3.2', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'TERM=xterm-256color', 'COLORTERM=truecolor', 'TERM_PROGRAM_VERSION=3.2', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: a configured bin directory already on the path': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/usr/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: the legacy bin variable': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/opt/p459/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/opt/p459/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/opt/p459/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: a home with a space': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/srv/p459 homes/agent', 'PATH=/srv/p459 homes/agent/bin:/srv/p459 homes/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude', 'a b']}, 'calls': [['_owner_home_bin_dirs', ['PATH /srv/p459 homes/agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/srv/p459 homes/agent/bin', '/srv/p459 homes/agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/srv/p459 homes/agent', 'PATH=/srv/p459 homes/agent/bin:/srv/p459 homes/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude', 'a b']], {}], ['current_user_name', [], {}]]},
    'env: no command': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin']], {}], ['current_user_name', [], {}]]},
    'env: the default path rebound on the launcher': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/p459/sbin:/p459/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/p459/sbin:/p459/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/p459/sbin:/p459/bin', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: the sibling rebound on the launcher': {'result': {'type': 'list', 'value': ['AS', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']], {}]]},
    'env: the presentation helper rebound on the launcher': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'P459=1', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'P459=1', 'claude']], {}], ['current_user_name', [], {}]]},
    'env: the bin helper rebound on the launcher': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/p459/own/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']}, 'calls': [['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/p459/own/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/p459/own/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'claude']], {}], ['current_user_name', [], {}]]},
    'home: recorded in the plan': {'result': {'type': 'PosixPath', 'value': 'PATH /srv/p459/home'}, 'calls': [['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}]]},
    'home: recorded with padding': {'result': {'type': 'PosixPath', 'value': 'PATH /srv/p459/home'}, 'calls': [['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}]]},
    'home: recorded blank': {'result': {'type': 'PosixPath', 'value': 'PATH /home/p459-agent'}, 'calls': [['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}], ['home_dir_for_user', ['p459-agent'], {}]]},
    'home: recorded as null': {'result': {'type': 'PosixPath', 'value': 'PATH /home/p459-agent'}, 'calls': [['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}], ['home_dir_for_user', ['p459-agent'], {}]]},
    'home: not in the plan': {'result': {'type': 'PosixPath', 'value': 'PATH /home/p459-agent'}, 'calls': [['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}], ['home_dir_for_user', ['p459-agent'], {}]]},
    'home: no configuration path': {'result': {'type': 'PosixPath', 'value': 'PATH /home/p459-agent'}, 'calls': [['home_dir_for_user', ['p459-agent'], {}]]},
    "home: no owner, the caller's": {'result': {'type': 'PosixPath', 'value': 'PATH /home/switchyard459'}, 'calls': [['current_user_name', [], {}], ['home_dir_for_user', ['switchyard459'], {}]]},
    'home: no account home': {'result': {'type': 'PosixPath', 'value': 'PATH /home/p459-agent'}, 'calls': [['home_dir_for_user', ['p459-agent'], {}]]},
    'home: the plan reader fails': {'result': {'raised': 'PermissionError', 'message': "[Errno 13] Permission denied: '/nonexistent/syrd459/p459/plan.json'"}, 'calls': [['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}]]},
    'systemctl: start': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459-listener.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459-listener.service']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459-listener.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: restart': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart p459-listener.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart p459-listener.service']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart p459-listener.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: stop': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user stop p459-listener.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user stop p459-listener.service']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user stop p459-listener.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: a capitalised Start': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user Start p459.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user Start p459.service']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user Start p459.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: a question of the manager': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user is-system-running']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user is-system-running']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user is-system-running']], {}], ['current_user_name', [], {}]]},
    'systemctl: a unit needing quotes': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart \'p459 listener\'"\'"\'s.service\'']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart \'p459 listener\'"\'"\'s.service\'']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart \'p459 listener\'"\'"\'s.service\'']], {}], ['current_user_name', [], {}]]},
    'systemctl: the caller is the owner': {'result': {'type': 'list', 'value': ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user status p459.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user status p459.service']], {}], ['_owner_home_bin_dirs', ['PATH /home/p459-agent'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/p459-agent/bin', '/home/p459-agent/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/home/p459-agent', 'PATH=/home/p459-agent/bin:/home/p459-agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user status p459.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: no owner, the caller': {'result': {'type': 'list', 'value': ['env', 'HOME=/home/switchyard459', 'PATH=/home/switchyard459/bin:/home/switchyard459/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459.service']}, 'calls': [['current_user_name', [], {}], ['_tenant_owner_home', ["CONFIG run_as_user=''", None], {}], ['current_user_name', [], {}], ['home_dir_for_user', ['switchyard459'], {}], ['_owner_command_env_args', ['switchyard459', 'PATH /home/switchyard459', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459.service']], {}], ['_owner_home_bin_dirs', ['PATH /home/switchyard459'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/home/switchyard459/bin', '/home/switchyard459/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['switchyard459', ['env', 'HOME=/home/switchyard459', 'PATH=/home/switchyard459/bin:/home/switchyard459/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: the recorded home': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/srv/p459/home', 'PATH=/srv/p459/home/bin:/srv/p459/home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}], ['_plan_data_from_config', ["CONFIG run_as_user='p459-agent'", 'PATH /nonexistent/syrd459/p459/config.json'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /srv/p459/home', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459.service']], {}], ['_owner_home_bin_dirs', ['PATH /srv/p459/home'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/srv/p459/home/bin', '/srv/p459/home/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/srv/p459/home', 'PATH=/srv/p459/home/bin:/srv/p459/home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user start p459.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: the home helper rebound on the launcher': {'result': {'type': 'list', 'value': ['sudo', '-u', 'p459-agent', 'env', 'HOME=/p459/rebound-home', 'PATH=/p459/rebound-home/bin:/p459/rebound-home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user stop p459.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /p459/rebound-home', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user stop p459.service']], {}], ['_owner_home_bin_dirs', ['PATH /p459/rebound-home'], {}], ['_prepend_paths', ['/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', ['/p459/rebound-home/bin', '/p459/rebound-home/.local/bin']], {}], ['_terminal_presentation_env', [], {}], ['_owner_command_args', ['p459-agent', ['env', 'HOME=/p459/rebound-home', 'PATH=/p459/rebound-home/bin:/p459/rebound-home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user stop p459.service']], {}], ['current_user_name', [], {}]]},
    'systemctl: the env helper rebound on the launcher': {'result': {'type': 'list', 'value': ['ENV', 'p459-agent', '/home/p459-agent', 'sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart p459.service']}, 'calls': [['_tenant_owner_home', ["CONFIG run_as_user='p459-agent'", None], {}], ['home_dir_for_user', ['p459-agent'], {}], ['_owner_command_env_args', ['p459-agent', 'PATH /home/p459-agent', ['sh', '-c', 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; systemctl --user daemon-reload && systemctl --user restart p459.service']], {}]]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold459.py` (which ran them on the baseline) ------------------------------------
# A case builds one argv (or one home) with one of the four and records the answer or the exact exception, and, in
# order, every call it made of the launcher. Nothing is run: the four only build argv. The current user, the account
# home lookup and the tenant's plan record are stand-ins on the launcher, answering the case's synthetic values; the
# PATH and presentation helpers are the launcher's own, recorded there and passed through. The environment variables
# they read (the user bin directory and the terminal presentation keys) are set per case and restored afterwards.
CASES = {
    "command: the caller is the owner": {"call": "_owner_command_args", "caller": "p459-agent", "args": ["p459-agent", ["claude", "--resume"]]},
    "command: another owner": {"call": "_owner_command_args", "caller": "switchyard459", "args": ["p459-agent", ["claude", "--resume"]]},
    "command: a tuple command": {"call": "_owner_command_args", "caller": "switchyard459", "args": ["p459-agent", ("codex", "login")], "tuple": True},
    "command: a tuple command, the caller": {"call": "_owner_command_args", "caller": "p459-agent", "args": ["p459-agent", ("codex", "login")], "tuple": True},
    "command: no owner, no caller name": {"call": "_owner_command_args", "caller": "", "args": ["", ["id"]]},
    "command: no owner, a caller": {"call": "_owner_command_args", "caller": "switchyard459", "args": ["", ["id"]]},
    "env: another owner": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]]},
    "env: the caller is the owner": {"call": "_owner_command_env_args", "caller": "p459-agent", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]]},
    "env: presentation variables": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                    "environ": {"TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "  ", "TERM_PROGRAM_VERSION": "3.2"}},
    "env: a configured bin directory already on the path": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                                           "environ": {"USER_BIN_ENV": "/usr/bin"}},
    "env: the legacy bin variable": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                     "environ": {"LEGACY_USER_BIN_ENV": "/opt/p459/bin"}},
    "env: a home with a space": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/srv/p459 homes/agent", ["claude", "a b"]]},
    "env: no command": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", []]},
    "env: the default path rebound on the launcher": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                                      "rebind": {"DEFAULT_PANE_BASE_PATH": "/p459/sbin:/p459/bin"}},
    "env: the sibling rebound on the launcher": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                                 "rebind": {"_owner_command_args": "AS"}},
    "env: the presentation helper rebound on the launcher": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                                             "rebind": {"_terminal_presentation_env": ["P459=1"]}},
    "env: the bin helper rebound on the launcher": {"call": "_owner_command_env_args", "caller": "switchyard459", "args": ["p459-agent", "HOME:/home/p459-agent", ["claude"]],
                                                    "rebind": {"_owner_home_bin_dirs": ["/p459/own/bin"]}},
    "home: recorded in the plan": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": True,
                                   "plan": {"owner_home": "/srv/p459/home"}, "account_home": "/home/p459-agent"},
    "home: recorded with padding": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": True,
                                    "plan": {"owner_home": "  /srv/p459/home  "}, "account_home": "/home/p459-agent"},
    "home: recorded blank": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": True,
                             "plan": {"owner_home": "   "}, "account_home": "/home/p459-agent"},
    "home: recorded as null": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": True,
                               "plan": {"owner_home": None}, "account_home": "/home/p459-agent"},
    "home: not in the plan": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": True,
                              "plan": {"project": "p459"}, "account_home": "/home/p459-agent"},
    "home: no configuration path": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": False,
                                    "plan": {"owner_home": "/srv/p459/home"}, "account_home": "/home/p459-agent"},
    "home: no owner, the caller's": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "", "config_path": False, "account_home": "/home/switchyard459"},
    "home: no account home": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": False, "account_home": None},
    "home: the plan reader fails": {"call": "_tenant_owner_home", "caller": "switchyard459", "owner": "p459-agent", "config_path": True,
                                    "plan": "RAISE", "account_home": "/home/p459-agent"},
    "systemctl: start": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "start", "unit": "p459-listener.service",
                         "account_home": "/home/p459-agent"},
    "systemctl: restart": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "restart", "unit": "p459-listener.service",
                           "account_home": "/home/p459-agent"},
    "systemctl: stop": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "stop", "unit": "p459-listener.service",
                        "account_home": "/home/p459-agent"},
    "systemctl: a capitalised Start": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "Start", "unit": "p459.service",
                                                  "account_home": "/home/p459-agent"},
    "systemctl: a question of the manager": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "is-system-running", "unit": "",
                                             "account_home": "/home/p459-agent"},
    "systemctl: a unit needing quotes": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "restart", "unit": "p459 listener's.service",
                                         "account_home": "/home/p459-agent"},
    "systemctl: the caller is the owner": {"call": "_owner_user_systemctl", "caller": "p459-agent", "owner": "p459-agent", "action": "status", "unit": "p459.service",
                                           "account_home": "/home/p459-agent"},
    "systemctl: no owner, the caller": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "", "action": "start", "unit": "p459.service",
                                        "account_home": "/home/switchyard459"},
    "systemctl: the recorded home": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "start", "unit": "p459.service",
                                     "config_path": True, "plan": {"owner_home": "/srv/p459/home"}, "account_home": "/home/p459-agent"},
    "systemctl: the home helper rebound on the launcher": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "stop", "unit": "p459.service",
                                                           "account_home": "/home/p459-agent", "rebind": {"_tenant_owner_home": "/p459/rebound-home"}},
    "systemctl: the env helper rebound on the launcher": {"call": "_owner_user_systemctl", "caller": "switchyard459", "owner": "p459-agent", "action": "restart", "unit": "p459.service",
                                                          "account_home": "/home/p459-agent", "rebind": {"_owner_command_env_args": "ENV"}},
}
FUNCTIONS = ("_owner_command_args", "_owner_command_env_args", "_tenant_owner_home", "_owner_user_systemctl")
PASSED = ("_owner_command_args", "_owner_command_env_args", "_tenant_owner_home", "_prepend_paths", "_owner_home_bin_dirs", "_terminal_presentation_env")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition; the current user, account home and plan record stood in on the launcher."""
    import os
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value)
        if isinstance(value, SimpleNamespace):
            return f"CONFIG run_as_user={value.run_as_user!r}"
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    env_keys = (t.USER_BIN_ENV, t.LEGACY_USER_BIN_ENV, *t.TERMINAL_PRESENTATION_ENV_KEYS)
    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, "current_user_name", "home_dir_for_user", "_plan_data_from_config", "DEFAULT_PANE_BASE_PATH")}
    saved_env = {k: os.environ.get(k) for k in env_keys}
    try:
        for k in env_keys:
            os.environ.pop(k, None)
        for k, v in spec.get("environ", {}).items():
            os.environ[{"USER_BIN_ENV": t.USER_BIN_ENV, "LEGACY_USER_BIN_ENV": t.LEGACY_USER_BIN_ENV}.get(k, k)] = v
        t.current_user_name = lambda: note("current_user_name") or spec["caller"]

        def account_home(user):
            note("home_dir_for_user", user)
            return None if spec.get("account_home") is None else _P(spec["account_home"])
        t.home_dir_for_user = account_home

        def plan(config, config_path):
            note("_plan_data_from_config", config, config_path)
            if spec.get("plan") == "RAISE":
                raise OSError(13, "Permission denied", str(config_path.parent / "plan.json"))
            return dict(spec.get("plan") or {})
        t._plan_data_from_config = plan
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name, *a, **k) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "DEFAULT_PANE_BASE_PATH":
                t.DEFAULT_PANE_BASE_PATH = value
            elif name == "_owner_command_args":
                t._owner_command_args = lambda owner, command: note("_owner_command_args", owner, command) or ["AS", owner, *command]
            elif name == "_owner_command_env_args":
                t._owner_command_env_args = lambda owner, home, command: note("_owner_command_env_args", owner, home, command) or ["ENV", owner, str(home), *command]
            elif name == "_tenant_owner_home":
                t._tenant_owner_home = lambda config, config_path: note("_tenant_owner_home", config, config_path) or _P(value)
            else:
                setattr(t, name, (lambda name, value: lambda *a, **k: note(name, *a, **k) or list(value))(name, value))
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        config = SimpleNamespace(run_as_user=spec.get("owner", ""), project="p459")
        config_path = _P("/nonexistent/syrd459/p459/config.json") if spec.get("config_path") else None
        if spec["call"] in ("_owner_command_args", "_owner_command_env_args"):
            args = [_P(a[5:]) if isinstance(a, str) and a.startswith("HOME:") else a for a in spec["args"]]
            if spec.get("tuple"):
                args[-1] = tuple(args[-1])
            kwargs = {}
        elif spec["call"] == "_tenant_owner_home":
            args, kwargs = [config, config_path], {}
        else:
            args, kwargs = [config, spec["action"], spec["unit"]], ({"config_path": config_path} if config_path else {})
        try:
            got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        else:
            result = {"type": type(got).__name__, "value": norm(got)}
        return {"result": result, "calls": calls}
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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


#: What the Director kept on the launcher, beside the four.
KEPT = ("current_user_name", "capture_installed_units")


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
    result = python("import sys, scripts.owner_commands as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.owner_commands", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.owner_commands")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.owner_commands as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"[getattr(m, n).__module__ for n in {MOVED!r}], "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'ProjectConfig', 'home_dir_for_user', 'DEFAULT_PANE_BASE_PATH', "
                        f"'_prepend_paths', '_owner_home_bin_dirs', '_terminal_presentation_env', '_plan_data_from_config', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.owner_commands', 'scripts.owner_commands', 'scripts.owner_commands', 'scripts.owner_commands'] True",
              f"{' then '.join(order)}: one object each, defined here; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    import shlex as _shlex
    import typing
    check(m.shlex is _shlex and m.Path is pathlib.Path and m.Sequence is typing.Sequence and m.TYPE_CHECKING is False
          and t.shlex is m.shlex and t.Path is m.Path and t.Sequence is m.Sequence,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "owner_commands.py").read_text(encoding="utf-8"))
    for name in MOVED:
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
    check(top == ["from __future__ import annotations", "import shlex", "from pathlib import Path", "from typing import TYPE_CHECKING, Sequence"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.project_identity import ProjectConfig"],
          f"the standard library, and the config type for annotations only: {top} {tc}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {"_owner_command_args": [], "_owner_command_env_args": [], "_tenant_owner_home": [], "_owner_user_systemctl": ["None"]},
          f"the only default is config_path=None, a literal: {defaults}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the four, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.owner_commands"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the four, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | set(KEPT) | set(DISPATCH) <= defined | exported,
          "the launcher defines none of them, and keeps current_user_name, capture_installed_units, their launcher caller and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"the launcher's own definitions name them exactly as often as before, by the re-exported names: {uses}")
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
    def value(label):
        return GOLDEN[label]["result"]["value"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    BASE = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    HOME = "/home/p459-agent"
    ENV = ["env", f"HOME={HOME}", f"PATH={HOME}/bin:{HOME}/.local/bin:{BASE}"]
    PRELUDE = 'runtime="/run/user/$(id -u)"; export XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus"; '
    check(value("command: the caller is the owner") == ["claude", "--resume"] and value("command: another owner") == [*OWNER_PREFIX, "p459-agent", "claude", "--resume"]
          and value("command: a tuple command") == [*OWNER_PREFIX, "p459-agent", "codex", "login"] and value("command: a tuple command, the caller") == ["codex", "login"]
          and all(GOLDEN[k]["result"]["type"] == "list" for k in GOLDEN if k.startswith(("command:", "env:", "systemctl:"))),
          "a command runs as it is when the caller is the owner, else behind sudo -u <owner>; always a new list")
    check(value("command: no owner, no caller name") == ["id"] and value("command: no owner, a caller") == [*OWNER_PREFIX, "", "id"]
          and all(calls(k) == ["current_user_name"] for k in GOLDEN if k.startswith("command:")),
          "the owner is compared with the launcher's current user, asked once when it runs; an empty owner is not special (pinned)")
    check(value("env: another owner") == [*OWNER_PREFIX, "p459-agent", *ENV, "claude"] and value("env: the caller is the owner") == [*ENV, "claude"]
          and value("env: no command") == [*OWNER_PREFIX, "p459-agent", *ENV],
          "env: HOME, then a PATH of the owner's bin and .local/bin ahead of the default pane path, then the command, all through the owner prefix")
    check(value("env: presentation variables")[6:10] == ["TERM=xterm-256color", "COLORTERM=truecolor", "TERM_PROGRAM_VERSION=3.2", "claude"]
          and value("env: a configured bin directory already on the path")[5] == "PATH=/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin:/bin"
          and value("env: the legacy bin variable")[5] == f"PATH=/opt/p459/bin:{BASE}"
          and value("env: a home with a space")[4:] == ["HOME=/srv/p459 homes/agent", f"PATH=/srv/p459 homes/agent/bin:/srv/p459 homes/agent/.local/bin:{BASE}", "claude", "a b"],
          "the non-blank presentation variables in order; a configured bin directory replaces the home's and moves to the front; nothing is quoted")
    check(value("env: the default path rebound on the launcher")[5] == f"PATH={HOME}/bin:{HOME}/.local/bin:/p459/sbin:/p459/bin"
          and value("env: the sibling rebound on the launcher")[:2] == ["AS", "p459-agent"]
          and value("env: the presentation helper rebound on the launcher")[6] == "P459=1"
          and value("env: the bin helper rebound on the launcher")[5] == f"PATH=/p459/own/bin:{BASE}"
          and calls("env: another owner") == ["_owner_home_bin_dirs", "_prepend_paths", "_terminal_presentation_env", "_owner_command_args", "current_user_name"],
          "the default path, the bin directories, the presentation variables and the owner prefix are the launcher's, read when it runs, in that order")
    check(value("home: recorded in the plan") == value("home: recorded with padding") == "PATH /srv/p459/home"
          and value("home: recorded blank") == value("home: recorded as null") == value("home: not in the plan") == value("home: no configuration path") == f"PATH {HOME}"
          and value("home: no owner, the caller's") == "PATH /home/switchyard459" and value("home: no account home") == f"PATH {HOME}"
          and calls("home: recorded in the plan") == ["_plan_data_from_config"] and calls("home: no configuration path") == ["home_dir_for_user"]
          and GOLDEN["home: the plan reader fails"]["result"]["raised"] == "PermissionError",
          "the owner home: the plan's recorded one (stripped) when a configuration path is given, else the account's, else /home/<owner>; a plan read error propagates")
    script = lambda label: value(label)[-1]
    check(script("systemctl: start") == PRELUDE + "systemctl --user daemon-reload && systemctl --user start p459-listener.service"
          and script("systemctl: restart") == PRELUDE + "systemctl --user daemon-reload && systemctl --user restart p459-listener.service"
          and script("systemctl: stop") == PRELUDE + "systemctl --user stop p459-listener.service"
          and script("systemctl: a capitalised Start") == PRELUDE + "systemctl --user Start p459.service"
          and script("systemctl: a question of the manager") == PRELUDE + "systemctl --user is-system-running"
          and script("systemctl: a unit needing quotes") == PRELUDE + "systemctl --user daemon-reload && systemctl --user restart 'p459 listener'\"'\"'s.service'"
          and value("systemctl: start")[:-1] == [*OWNER_PREFIX, "p459-agent", *ENV, "sh", "-c"],
          "the user manager: runtime directory and bus exported, daemon-reload first only for start and restart, the unit shell-quoted, all under sh -c")
    check(value("systemctl: the caller is the owner")[0] == "env" and value("systemctl: no owner, the caller")[1] == "HOME=/home/switchyard459"
          and value("systemctl: the recorded home")[4] == "HOME=/srv/p459/home"
          and value("systemctl: the home helper rebound on the launcher")[4] == "HOME=/p459/rebound-home"
          and value("systemctl: the env helper rebound on the launcher")[:3] == ["ENV", "p459-agent", HOME],
          "the owner and home through the launcher's helpers, read when it runs")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the four read on the launcher is a recorder there; the default path is rebound by its own case.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name))}
    check(constants == {"DEFAULT_PANE_BASE_PATH"} and names - constants <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"owner_commands_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
