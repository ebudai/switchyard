#!/usr/bin/env python3
"""SYRD-470: the provisioning role accounts, runtime commands and role-control sudoers, against project_provision they came out of.

The sixteen -- who the role accounts are (`NON_PROCESS_ROLES`,
`role_account_name`, `roles_group_name`, `role_account_table`,
`role_accounts_env`, `role_account_home`), the commands that create them and
give each its runtime (`role_accounts_command`, `role_runtime_command`,
`role_runtime_commands`, `role_account_commands`, `ROLE_ACCOUNT_MIGRATION_SUFFIX`,
`role_account_migration_name`), and the role-control rule with its install
commands (`render_role_control_sudoers`, `role_control_sudoers`,
`ROLE_CONTROL_SUDOERS_HEREDOC`, `role_control_sudoers_install_commands`) --
moved unchanged into `scripts/ticket_board/provision_role_accounts.py`;
`project_provision` re-exports them all, in both branches of its import block,
and keeps `shell_quote`, `SHARED_RELEASE_CURRENT` and the repository group, and
re-exports the role tooling, path confinement and tenant control they call.
This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first. The module
  alone loads only its package; `team_launcher`'s two module-level imports are
  its objects, and its `role_account_name` stays its own wrapper.
- **Seams (rule 24):** what they read of `project_provision` -- each other and
  everything above -- is read through it when they run, with the direct-script
  fallback, so a patch there reaches them; `project_role_add` and
  `recovery_readiness` import `role_account_commands` and `roles_group_name`
  from `project_provision` when they run, so a patch there is what they get.
- **Readers:** every production module imports them from `project_provision`,
  as often as before, and `project_provision`'s own callers name them as often.
- **The behaviour is the baseline's:** names, table, homes and role map; the
  account and runtime commands and the control rule for plans with and without
  role accounts, a group and a director; the install commands; adding a role,
  with and without a control repository and a recorded human. `GOLDEN` below
  was produced by running the BASELINE module's own definitions over the very
  cases embedded here (`gold470.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with another TMPDIR, locale, SUDO_UID
  and staging override in the environment.
- **The direct script answers what the package answers,** and the default,
  shaped, lean and roles packets are byte-identical through both; the roles
  packet's role-control sudoers and operator commands name the role accounts.

No real home, tenant, account, grant, /etc or /var path is read or written:
every path is synthetic, plans are given synthetic role sets, and the builders
only return names and shell text. No account is looked up: each case stands in
`pwd.getpwuid` and `os.getuid`, and the tenant-control grant adding a role looks
for lives under a TENANT_CONTROL_ROOT the case creates. Spawns, every exec,
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
from pathlib import Path, PurePosixPath
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# project_provision first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts.ticket_board import project_provision as t  # noqa: E402,I001
from scripts.ticket_board import provision_role_accounts as m  # noqa: E402

CHECKS = 0
MOVED = ('NON_PROCESS_ROLES', 'role_account_name', 'roles_group_name', 'role_account_table', 'role_accounts_env', 'role_accounts_command', 'role_runtime_command', 'ROLE_ACCOUNT_MIGRATION_SUFFIX', 'role_account_migration_name', 'role_runtime_commands', 'role_account_home', 'role_account_commands', 'render_role_control_sudoers', 'role_control_sudoers', 'ROLE_CONTROL_SUDOERS_HEREDOC', 'role_control_sudoers_install_commands')
#: Measured on the baseline project_provision: each moved body's call-time reads of its globals, siblings included.
SEAMS = {
    'role_account_table': {'NON_PROCESS_ROLES': 1, 'role_account_name': 1},
    'role_accounts_command': {'repository_group_commands': 1, 'role_account_home': 1, 'shell_quote': 5},
    'role_runtime_command': {'SHARED_RELEASE_CURRENT': 1, 'role_account_home': 1, 'role_runtime_commands': 1, 'role_tooling_staging_commands': 1},
    'role_account_migration_name': {'ROLE_ACCOUNT_MIGRATION_SUFFIX': 1},
    'role_runtime_commands': {'shell_quote': 11},
    'role_account_home': {'role_account_name': 1},
    'role_account_commands': {'SHARED_RELEASE_CURRENT': 1, 'invoking_human': 1, 'render_role_control_sudoers': 1, 'repository_group_commands': 1, 'repository_group_name': 1, 'resolve_control_user': 1, 'role_account_name': 1, 'role_control_sudoers_install_commands': 1, 'role_runtime_commands': 1, 'role_tooling_staging_commands': 1, 'role_worktree_access_commands': 1, 'roles_group_name': 1, 'shell_quote': 6, 'tenant_control_commands': 1},
    'role_control_sudoers': {'render_role_control_sudoers': 1},
    'role_control_sudoers_install_commands': {'ROLE_CONTROL_SUDOERS_HEREDOC': 1},
}
#: Measured on the baseline: every project_provision definition outside the sixteen that names them, and how often.
DISPATCH = {'render_board_unit': {'role_accounts_env': 1}, 'render_listener_unit': {'role_accounts_env': 1}, 'render_operator_commands': {'role_accounts_command': 1, 'role_control_sudoers': 1, 'role_runtime_command': 1, 'roles_group_name': 1}, 'write_artifacts': {'role_control_sudoers': 1}, 'main': {'role_control_sudoers': 1}}
#: Measured on the baseline, by AST: every production module that imports them from project_provision, and how often.
READERS = {'scripts/project_role_add.py': {'import role_account_commands': 1}, 'scripts/recovery_readiness.py': {'import roles_group_name': 1}, 'scripts/role_account_migration.py': {'import roles_group_name': 2, 'import render_role_control_sudoers': 1, 'import role_control_sudoers_install_commands': 1, 'import role_runtime_commands': 1}, 'scripts/team_launcher.py': {'import NON_PROCESS_ROLES': 2, 'import role_account_migration_name': 1, 'import role_account_name': 1}}
#: The BASELINE's own behaviour for the cases below (`gold470.py`, run on the baseline project_provision under the guard).
GOLDEN = {
    'account name': {'result': {'type': 'str', 'value': 'p470-main'}, 'calls': {}},
    'group name': {'result': {'type': 'str', 'value': 'p470-roles'}, 'calls': {}},
    'migration name': {'result': {'type': 'str', 'value': 'p470-role-accounts.sh'}, 'calls': {}},
    'migration name: the suffix rebound on project_provision': {'result': {'type': 'str', 'value': 'p470.sh'}, 'calls': {}},
    'account table': {'result': {'type': 'tuple', 'value': [['director', 'p470-director'], ['main', 'p470-main'], ['audit', 'p470-audit']]}, 'calls': {'role_account_name': 3}},
    'account table: the non-process roles rebound on project_provision': {'result': {'type': 'tuple', 'value': [['director', 'p470-director'], ['user', 'p470-user']]}, 'calls': {'role_account_name': 2}},
    'account table: the name rebound on project_provision': {'result': {'type': 'tuple', 'value': [['main', 'acct-main']]}, 'calls': {'role_account_name rebound': 1}},
    'account home': {'result': {'type': 'str', 'value': '/home/p470-main'}, 'calls': {'role_account_name': 1}},
    'account home: the name rebound on project_provision': {'result': {'type': 'str', 'value': '/home/acct-main'}, 'calls': {'role_account_name rebound': 1}},
    'accounts env: no role accounts': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'accounts env: role accounts': {'result': {'type': 'str', 'value': 'director=p470-director,main=p470-main,audit=p470-audit'}, 'calls': {}},
    'accounts env: no director': {'result': {'type': 'str', 'value': 'main=p470-main,audit=p470-audit'}, 'calls': {}},
    'accounts env: role accounts, no group': {'result': {'type': 'str', 'value': 'director=p470-director,main=p470-main,audit=p470-audit'}, 'calls': {}},
    'accounts command: no role accounts': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'accounts command: role accounts': {'result': {'type': 'str', 'value': 'TEXT 1461 chars sha256:bf22dc663b1c8d59'}, 'calls': {'repository_group_commands': 1, 'repository_group_name': 1, 'role_account_home': 3, 'role_account_name': 3, 'shell_quote': 14}},
    'accounts command: no director': {'result': {'type': 'str', 'value': 'TEXT 1064 chars sha256:c43485e95a1cef4a'}, 'calls': {'repository_group_commands': 1, 'repository_group_name': 1, 'role_account_home': 2, 'role_account_name': 2, 'shell_quote': 11}},
    'accounts command: role accounts, no group': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'runtime command: no role accounts': {'result': {'type': 'str', 'value': 'TEXT 11775 chars sha256:caf1aa194ef77e49'}, 'calls': {'role_tooling_staging_commands': 1, 'shell_quote': 84}},
    'runtime command: role accounts': {'result': {'type': 'str', 'value': 'TEXT 15685 chars sha256:58e301922a147e0a'}, 'calls': {'role_account_home': 3, 'role_account_name': 3, 'role_runtime_commands': 3, 'role_tooling_staging_commands': 1, 'shell_quote': 120}},
    'runtime command: no director': {'result': {'type': 'str', 'value': 'TEXT 14321 chars sha256:b536132298ff3381'}, 'calls': {'role_account_home': 2, 'role_account_name': 2, 'role_runtime_commands': 2, 'role_tooling_staging_commands': 1, 'shell_quote': 108}},
    'runtime command: role accounts, no group': {'result': {'type': 'str', 'value': 'TEXT 11775 chars sha256:caf1aa194ef77e49'}, 'calls': {'role_tooling_staging_commands': 1, 'shell_quote': 84}},
    'runtime command: the release rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 15305 chars sha256:ef70f94efc2d358a'}, 'calls': {'role_account_home': 3, 'role_account_name': 3, 'role_runtime_commands': 3, 'role_tooling_staging_commands': 1, 'shell_quote': 120}},
    'control sudoers: no role accounts': {'result': {'type': 'str', 'value': ''}, 'calls': {'render_role_control_sudoers': 1}},
    'control sudoers: role accounts': {'result': {'type': 'str', 'value': "# p470: role control interface. Each entry grants one command and\n# nothing else, so a holder can drive another account's tmux server, and gains\n# no other command and no root.\np470-owner ALL=(p470-director,p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-owner) NOPASSWD: /usr/bin/tmux\n"}, 'calls': {'render_role_control_sudoers': 1}},
    'control sudoers: no director': {'result': {'type': 'str', 'value': "# p470: role control interface. Each entry grants one command and\n# nothing else, so a holder can drive another account's tmux server, and gains\n# no other command and no root.\np470-owner ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\n"}, 'calls': {'render_role_control_sudoers': 1}},
    'control sudoers: role accounts, no group': {'result': {'type': 'str', 'value': "# p470: role control interface. Each entry grants one command and\n# nothing else, so a holder can drive another account's tmux server, and gains\n# no other command and no root.\np470-owner ALL=(p470-director,p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-owner) NOPASSWD: /usr/bin/tmux\n"}, 'calls': {'render_role_control_sudoers': 1}},
    'control sudoers: the renderer rebound on project_provision': {'result': {'type': 'str', 'value': "RULE p470 p470-owner [('director', 'p470-director'), ('main', 'p470-main'), ('audit', 'p470-audit')]\n"}, 'calls': {'render_role_control_sudoers rebound': 1}},
    'runtime commands': {'result': {'type': 'list', 'value': ["if ! getent passwd 'p470-main' >/dev/null 2>&1; then", "    sudo useradd -m -d '/home/p470-main' -s /bin/bash 'p470-main'", 'fi', "sudo gpasswd -a 'p470-main' 'p470-roles' >/dev/null", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main'", "sudo loginctl enable-linger 'p470-main' >/dev/null 2>&1 || true", "if [ -d '/p470/home/p470-worktrees/main' ]; then sudo chown -R 'p470-main': '/p470/home/p470-worktrees/main'; fi", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main/.local/bin'", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main/.local/state/p470-ticket-board/pane-sessions'", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main/.config'", "sudo install -m 0755 -o 'p470-main' -g 'p470-main' '/usr/local/lib/switchyard/p470/ticket-board-pane-idle-hook' '/home/p470-main/.local/bin/ticket-board-pane-idle-hook'", "sudo -u 'p470-main' -H env TICKET_BOARD_PROJECT='p470' TICKET_BOARD_PANE_SESSION_DIR='/home/p470-main/.local/state/p470-ticket-board/pane-sessions' '/usr/local/lib/switchyard/p470/ticket-board-install-pane-hooks' install --home '/home/p470-main' --hook-source '/usr/local/lib/switchyard/p470/ticket-board-pane-idle-hook' --bin-path '/home/p470-main/.local/bin/ticket-board-pane-idle-hook'", "sudo -u 'p470-main' -H '/usr/local/lib/switchyard/p470/switchyard-board-skill' install --home '/home/p470-main'"]}, 'calls': {'shell_quote': 13}},
    'runtime commands: no worktree': {'result': {'type': 'list', 'value': ["if ! getent passwd 'p470-main' >/dev/null 2>&1; then", "    sudo useradd -m -d '/home/p470-main' -s /bin/bash 'p470-main'", 'fi', "sudo gpasswd -a 'p470-main' 'p470-roles' >/dev/null", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main'", "sudo loginctl enable-linger 'p470-main' >/dev/null 2>&1 || true", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main/.local/bin'", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main/.local/state/p470-ticket-board/pane-sessions'", "sudo install -d -m 0700 -o 'p470-main' -g 'p470-main' '/home/p470-main/.config'", "sudo install -m 0755 -o 'p470-main' -g 'p470-main' '/usr/local/lib/switchyard/p470/ticket-board-pane-idle-hook' '/home/p470-main/.local/bin/ticket-board-pane-idle-hook'", "sudo -u 'p470-main' -H env TICKET_BOARD_PROJECT='p470' TICKET_BOARD_PANE_SESSION_DIR='/home/p470-main/.local/state/p470-ticket-board/pane-sessions' '/usr/local/lib/switchyard/p470/ticket-board-install-pane-hooks' install --home '/home/p470-main' --hook-source '/usr/local/lib/switchyard/p470/ticket-board-pane-idle-hook' --bin-path '/home/p470-main/.local/bin/ticket-board-pane-idle-hook'", "sudo -u 'p470-main' -H '/usr/local/lib/switchyard/p470/switchyard-board-skill' install --home '/home/p470-main'"]}, 'calls': {'shell_quote': 12}},
    'runtime commands: the quoting rebound on project_provision': {'result': {'type': 'list', 'value': ['if ! getent passwd <p470-main> >/dev/null 2>&1; then', '    sudo useradd -m -d </home/p470-main> -s /bin/bash <p470-main>', 'fi', 'sudo gpasswd -a <p470-main> <p470-roles> >/dev/null', 'sudo install -d -m 0700 -o <p470-main> -g <p470-main> </home/p470-main>', 'sudo loginctl enable-linger <p470-main> >/dev/null 2>&1 || true', 'sudo install -d -m 0700 -o <p470-main> -g <p470-main> </home/p470-main/.local/bin>', 'sudo install -d -m 0700 -o <p470-main> -g <p470-main> </home/p470-main/.local/state/p470-ticket-board/pane-sessions>', 'sudo install -d -m 0700 -o <p470-main> -g <p470-main> </home/p470-main/.config>', 'sudo install -m 0755 -o <p470-main> -g <p470-main> </usr/local/lib/switchyard/p470/ticket-board-pane-idle-hook> </home/p470-main/.local/bin/ticket-board-pane-idle-hook>', 'sudo -u <p470-main> -H env TICKET_BOARD_PROJECT=<p470> TICKET_BOARD_PANE_SESSION_DIR=</home/p470-main/.local/state/p470-ticket-board/pane-sessions> </usr/local/lib/switchyard/p470/ticket-board-install-pane-hooks> install --home </home/p470-main> --hook-source </usr/local/lib/switchyard/p470/ticket-board-pane-idle-hook> --bin-path </home/p470-main/.local/bin/ticket-board-pane-idle-hook>', 'sudo -u <p470-main> -H </usr/local/lib/switchyard/p470/switchyard-board-skill> install --home </home/p470-main>']}, 'calls': {'shell_quote rebound': 12}},
    'render control sudoers: none': {'result': {'type': 'str', 'value': ''}, 'calls': {}},
    'render control sudoers: with a director': {'result': {'type': 'str', 'value': "# p470: role control interface. Each entry grants one command and\n# nothing else, so a holder can drive another account's tmux server, and gains\n# no other command and no root.\np470-owner ALL=(p470-director,p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-owner) NOPASSWD: /usr/bin/tmux\n"}, 'calls': {}},
    'render control sudoers: without a director': {'result': {'type': 'str', 'value': "# p470: role control interface. Each entry grants one command and\n# nothing else, so a holder can drive another account's tmux server, and gains\n# no other command and no root.\np470-owner ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux\n"}, 'calls': {}},
    'render control sudoers: only a director': {'result': {'type': 'str', 'value': "# p470: role control interface. Each entry grants one command and\n# nothing else, so a holder can drive another account's tmux server, and gains\n# no other command and no root.\np470-owner ALL=(p470-director) NOPASSWD: /usr/bin/tmux\np470-director ALL=(p470-owner) NOPASSWD: /usr/bin/tmux\n"}, 'calls': {}},
    'install commands: an empty document': {'result': {'type': 'list', 'value': []}, 'calls': {}},
    'install commands: a blank document': {'result': {'type': 'list', 'value': []}, 'calls': {}},
    'install commands: a document': {'result': {'type': 'list', 'value': ["sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/49-p470-role-control.staged <<'SWITCHYARD_ROLE_CONTROL_SUDOERS'", 'p470-owner ALL=(p470-main) NOPASSWD: /usr/bin/tmux', 'SWITCHYARD_ROLE_CONTROL_SUDOERS', 'sudo visudo -c -f /etc/sudoers.d/49-p470-role-control.staged', 'sudo mv /etc/sudoers.d/49-p470-role-control.staged /etc/sudoers.d/49-p470-role-control']}, 'calls': {}},
    'install commands: the heredoc marker rebound on project_provision': {'result': {'type': 'list', 'value': ["sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/49-p470-role-control.staged <<'P470_MARK'", 'rule', 'P470_MARK', 'sudo visudo -c -f /etc/sudoers.d/49-p470-role-control.staged', 'sudo mv /etc/sudoers.d/49-p470-role-control.staged /etc/sudoers.d/49-p470-role-control']}, 'calls': {}},
    'add role: defaults': {'result': {'type': 'str', 'value': 'TEXT 13980 chars sha256:fac2f806dec3dd8d'}, 'calls': {'invoking_human': 1, 'render_role_control_sudoers': 1, 'resolve_control_user': 1, 'role_account_name': 1, 'role_control_sudoers_install_commands': 1, 'role_runtime_commands': 1, 'role_tooling_staging_commands': 1, 'roles_group_name': 1, 'shell_quote': 102, 'tenant_control_commands': 1}},
    'add role: a control repository and worktree': {'result': {'type': 'str', 'value': 'TEXT 14856 chars sha256:c9374dafcf89e866'}, 'calls': {'invoking_human': 1, 'render_role_control_sudoers': 1, 'repository_group_commands': 1, 'repository_group_name': 2, 'resolve_control_user': 1, 'role_account_name': 1, 'role_control_sudoers_install_commands': 1, 'role_runtime_commands': 1, 'role_tooling_staging_commands': 1, 'role_worktree_access_commands': 1, 'roles_group_name': 1, 'shell_quote': 116, 'tenant_control_commands': 1}},
    'add role: no director': {'result': {'type': 'str', 'value': 'TEXT 13846 chars sha256:b3ef7fb98b00c2f2'}, 'calls': {'invoking_human': 1, 'render_role_control_sudoers': 1, 'resolve_control_user': 1, 'role_account_name': 1, 'role_control_sudoers_install_commands': 1, 'role_runtime_commands': 1, 'role_tooling_staging_commands': 1, 'roles_group_name': 1, 'shell_quote': 102, 'tenant_control_commands': 1}},
    'add role: the helpers rebound on project_provision': {'result': {'type': 'str', 'value': 'TEXT 525 chars sha256:6ed9572f397faf24'}, 'calls': {'invoking_human': 1, 'render_role_control_sudoers rebound': 1, 'resolve_control_user': 1, 'role_account_name': 1, 'role_control_sudoers_install_commands rebound': 1, 'role_runtime_commands rebound': 1, 'role_tooling_staging_commands rebound': 1, 'roles_group_name': 1, 'shell_quote': 6, 'tenant_control_commands rebound': 1}},
    'add role: a recorded human gets the tenant control bridge': {'result': {'type': 'str', 'value': 'TEXT 15264 chars sha256:f2f5348dab25fe01'}, 'calls': {'invoking_human': 1, 'render_role_control_sudoers': 1, 'resolve_control_user': 1, 'role_account_name': 1, 'role_control_sudoers_install_commands': 1, 'role_runtime_commands': 1, 'role_tooling_staging_commands': 1, 'roles_group_name': 1, 'shell_quote': 110, 'tenant_control_commands': 1}},
    'constants': {'result': {'type': 'list', 'value': [['unassigned', 'user'], '-role-accounts.sh', 'SWITCHYARD_ROLE_CONTROL_SUDOERS']}, 'calls': {}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- its package, nothing else.
DEFAULT_MODULES_LOADED = ['scripts.ticket_board']
#: Measured on the baseline: how many artifacts each synthetic packet has.
PACKET_FILES = {'default': 10, 'shaped': 10, 'lean': 10, 'roles': 10}

# --- the cases, shared verbatim with `gold470.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the thirteen and records the answer or the exact exception, and how many times it called each of
# `project_provision`'s helpers (recorded there and passed through). Projects, owners, role sets and plans are synthetic
# (p470...); plans are built by `build_plan` for a synthetic owner and given role accounts with `dataclasses.replace`
# (build_plan always leaves them empty). No account is looked up: `pwd.getpwuid` and `os.getuid` are stood in for each
# case, and the tenant-control grant `role_account_commands` looks for lives under a TENANT_CONTROL_ROOT the case creates.
# SUDO_UID and the staging override SWITCHYARD_TENANT_CONTROL_ROOT are cleared for each case and restored after it.
# A case may rebind a `project_provision` name to show it is read there when the function runs.
ACCOUNTS = (("director", "p470-director"), ("main", "p470-main"), ("audit", "p470-audit"))
ROLE_SETS = {"none": (), "with a director": ACCOUNTS, "without a director": ACCOUNTS[1:], "only a director": ACCOUNTS[:1]}
PLANS = {"no role accounts": {}, "role accounts": {"role_accounts": ACCOUNTS, "roles_group": "p470-roles"},
         "no director": {"role_accounts": ACCOUNTS[1:], "roles_group": "p470-roles"}, "role accounts, no group": {"role_accounts": ACCOUNTS, "roles_group": ""}}
ADD = {"project": "p470", "role": "main", "owner_user": "p470-owner", "service_user": "boardsvc"}
CASES = {
    "account name": {"call": "role_account_name", "args": ["p470", "main"]},
    "group name": {"call": "roles_group_name", "args": ["p470"]},
    "migration name": {"call": "role_account_migration_name", "args": ["p470"]},
    "migration name: the suffix rebound on project_provision": {"call": "role_account_migration_name", "args": ["p470"], "rebind": {"ROLE_ACCOUNT_MIGRATION_SUFFIX": ".sh"}},
    "account table": {"call": "role_account_table", "args": ["p470", ["director", "main", "user", "unassigned", " Main ", "main", "", "AUDIT"]]},
    "account table: the non-process roles rebound on project_provision": {"call": "role_account_table", "args": ["p470", ["director", "main", "user"]],
                                                                         "rebind": {"NON_PROCESS_ROLES": frozenset({"main"})}},
    "account table: the name rebound on project_provision": {"call": "role_account_table", "args": ["p470", ["main"]], "rebind": {"role_account_name": True}},
    "account home": {"call": "role_account_home", "plan": "role accounts", "args": ["main"]},
    "account home: the name rebound on project_provision": {"call": "role_account_home", "plan": "role accounts", "args": ["main"], "rebind": {"role_account_name": True}},
    **{f"accounts env: {p}": {"call": "role_accounts_env", "plan": p} for p in PLANS},
    **{f"accounts command: {p}": {"call": "role_accounts_command", "plan": p} for p in PLANS},
    **{f"runtime command: {p}": {"call": "role_runtime_command", "plan": p} for p in PLANS},
    "runtime command: the release rebound on project_provision": {"call": "role_runtime_command", "plan": "role accounts", "rebind": {"SHARED_RELEASE_CURRENT": "/p470/release"}},
    **{f"control sudoers: {p}": {"call": "role_control_sudoers", "plan": p} for p in PLANS},
    "control sudoers: the renderer rebound on project_provision": {"call": "role_control_sudoers", "plan": "role accounts", "rebind": {"render_role_control_sudoers": True}},
    "runtime commands": {"call": "role_runtime_commands", "kwargs": {"project": "p470", "runtime_directory": "p470-ticket-board", "board_current": "/p470/board/current",
                                                                     "roles_group": "p470-roles", "account": "p470-main", "home": "/home/p470-main",
                                                                     "worktree": "/p470/home/p470-worktrees/main"}},
    "runtime commands: no worktree": {"call": "role_runtime_commands", "kwargs": {"project": "p470", "runtime_directory": "p470-ticket-board", "board_current": "/p470/board/current",
                                                                                 "roles_group": "p470-roles", "account": "p470-main", "home": "/home/p470-main", "worktree": ""}},
    "runtime commands: the quoting rebound on project_provision": {"call": "role_runtime_commands", "rebind": {"shell_quote": True},
                                                                   "kwargs": {"project": "p470", "runtime_directory": "p470-ticket-board", "board_current": "/p470/board/current",
                                                                              "roles_group": "p470-roles", "account": "p470-main", "home": "/home/p470-main", "worktree": ""}},
    **{f"render control sudoers: {r}": {"call": "render_role_control_sudoers", "args": ["p470", "p470-owner", list(ROLE_SETS[r])]} for r in ROLE_SETS},
    "install commands: an empty document": {"call": "role_control_sudoers_install_commands", "args": ["p470", ""]},
    "install commands: a blank document": {"call": "role_control_sudoers_install_commands", "args": ["p470", " \n "]},
    "install commands: a document": {"call": "role_control_sudoers_install_commands", "args": ["p470", "p470-owner ALL=(p470-main) NOPASSWD: /usr/bin/tmux\n"]},
    "install commands: the heredoc marker rebound on project_provision": {"call": "role_control_sudoers_install_commands", "args": ["p470", "rule\n"],
                                                                          "rebind": {"ROLE_CONTROL_SUDOERS_HEREDOC": "P470_MARK"}},
    "add role: defaults": {"call": "role_account_commands", "kwargs": {**ADD, "role_accounts": list(ACCOUNTS)}},
    "add role: a control repository and worktree": {"call": "role_account_commands",
                                                    "kwargs": {**ADD, "role": "perf", "role_accounts": list(ACCOUNTS) + [("perf", "p470-perf")], "runtime_directory": "p470-rt",
                                                               "board_current": "/p470/board/current", "worktree": "/p470/home/p470-worktrees/perf",
                                                               "owner_home": "/p470/home", "control_repository": "/p470/home/p470-control.git"}},
    "add role: no director": {"call": "role_account_commands", "kwargs": {**ADD, "role_accounts": list(ACCOUNTS[1:])}},
    "add role: the helpers rebound on project_provision": {"call": "role_account_commands", "kwargs": {**ADD, "role_accounts": list(ACCOUNTS)},
                                                           "rebind": {"render_role_control_sudoers": True, "role_control_sudoers_install_commands": True,
                                                                      "role_runtime_commands": True, "role_tooling_staging_commands": True, "tenant_control_commands": True}},
    "add role: a recorded human gets the tenant control bridge": {"call": "role_account_commands", "kwargs": {**ADD, "role_accounts": list(ACCOUNTS)}, "sudo_uid": "1470"},
    "constants": {"call": None},
}
FUNCTIONS = ("role_account_name", "roles_group_name", "role_account_table", "role_accounts_env", "role_accounts_command", "role_runtime_command",
             "role_account_migration_name", "role_runtime_commands", "role_account_home", "role_account_commands", "render_role_control_sudoers",
             "role_control_sudoers", "role_control_sudoers_install_commands")
PASSED = ("role_account_name", "roles_group_name", "role_account_home", "role_runtime_commands", "render_role_control_sudoers", "role_control_sudoers_install_commands",
          "shell_quote", "repository_group_commands", "repository_group_name", "role_worktree_access_commands", "role_tooling_staging_commands",
          "tenant_control_commands", "resolve_control_user", "invoking_human")
REBINDABLE = ("NON_PROCESS_ROLES", "ROLE_ACCOUNT_MIGRATION_SUFFIX", "ROLE_CONTROL_SUDOERS_HEREDOC", "SHARED_RELEASE_CURRENT")


def run_case(t, holder, spec, reached):
    """One case against `holder`'s definition; `t` is project_provision, whose helpers are recorded and passed through."""
    import dataclasses as _dc, hashlib, os as _os, pathlib as _pl, pwd as _pwd, shutil as _sh, tempfile as _tf
    counts = {}
    work = _tf.mkdtemp(prefix="syrd470-case.")

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

    def fake_getpwuid(uid):
        if uid != 1470 and uid != 2470:
            raise KeyError(f"getpwuid(): uid not found: {uid}")
        return _pwd.struct_passwd(("alice470" if uid == 1470 else "p470-self", "x", uid, uid, "", "/nonexistent", "/bin/false"))

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, *REBINDABLE, "TENANT_CONTROL_ROOT", "build_plan")}
    ENV = ("SUDO_UID", "SWITCHYARD_TENANT_CONTROL_ROOT")
    saved_env, saved_getpwuid, saved_getuid = {n: _os.environ.get(n) for n in ENV}, _pwd.getpwuid, _os.getuid
    try:
        _pwd.getpwuid = fake_getpwuid
        _os.getuid = lambda: 2470
        for n in ENV:
            _os.environ.pop(n, None)
        if "sudo_uid" in spec:
            _os.environ["SUDO_UID"] = spec["sudo_uid"]
        t.TENANT_CONTROL_ROOT = work
        args, kwargs = list(spec.get("args", [])), dict(spec.get("kwargs", {}))
        if "plan" in spec:
            plan = saved["build_plan"](project="p470", owner_user="p470-owner", owner_home=_pl.Path("/p470/home"), port=34470)
            args = [_dc.replace(plan, **PLANS[spec["plan"]])] + args
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name) or saved[name](*a, **k))(name))
        for name, value in spec.get("rebind", {}).items():
            if name == "shell_quote":
                t.shell_quote = lambda value: note("shell_quote rebound") or "<" + str(value) + ">"
            elif name == "role_account_name":
                t.role_account_name = lambda project, role: note("role_account_name rebound") or f"acct-{role}"
            elif name == "render_role_control_sudoers":
                t.render_role_control_sudoers = lambda project, owner, accounts: note("render_role_control_sudoers rebound") or f"RULE {project} {owner} {list(accounts)}\n"
            elif name == "role_control_sudoers_install_commands":
                t.role_control_sudoers_install_commands = lambda project, document: note("role_control_sudoers_install_commands rebound") or [f"INSTALL {document!r}"]
            elif name == "role_runtime_commands":
                t.role_runtime_commands = lambda **k: note("role_runtime_commands rebound") or [f"RUNTIME {k['account']} {k['runtime_directory']} {k['board_current']} {k['worktree']!r}"]
            elif name == "role_tooling_staging_commands":
                t.role_tooling_staging_commands = lambda project, release, *a, **k: note("role_tooling_staging_commands rebound") or [f"STAGE {project} {release}"]
            elif name == "tenant_control_commands":
                t.tenant_control_commands = lambda project, owner, human: note("tenant_control_commands rebound") or [f"CONTROL {project} {owner} {human!r}"]
            else:
                setattr(t, name, value)
        if spec["call"] is None:
            got = [sorted(saved["NON_PROCESS_ROLES"]), saved["ROLE_ACCOUNT_MIGRATION_SUFFIX"], saved["ROLE_CONTROL_SUDOERS_HEREDOC"]]
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
        _pwd.getpwuid, _os.getuid = saved_getpwuid, saved_getuid
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


#: What the thirteen read on project_provision when they run: its own, or re-exported there by the earlier slices.
KEPT = ("shell_quote", "SHARED_RELEASE_CURRENT", "repository_group_commands", "repository_group_name", "role_worktree_access_commands",
        "role_tooling_staging_commands", "tenant_control_commands", "resolve_control_user", "invoking_human")
#: The three constants, as the baseline wrote them: (annotation, value).
CONSTANT_TEXT = {"NON_PROCESS_ROLES": (None, "frozenset({'user', 'unassigned'})"), "ROLE_ACCOUNT_MIGRATION_SUFFIX": (None, "'-role-accounts.sh'"),
                 "ROLE_CONTROL_SUDOERS_HEREDOC": (None, "'SWITCHYARD_ROLE_CONTROL_SUDOERS'")}
#: The call-time import every function that reads project_provision starts with, with the direct-script fallback.
CALL_TIME_IMPORT = ("try:\n    from . import project_provision as provision\n"
                    "except ImportError:\n    import project_provision as provision")
#: The defaults, as the baseline wrote them.
DEFAULTS = {"role_account_name": [], "roles_group_name": [], "role_account_table": [], "role_accounts_env": [], "role_accounts_command": [], "role_runtime_command": [],
            "role_account_migration_name": [], "role_runtime_commands": [], "role_account_home": [], "role_account_commands": ["''", "''", "''", "''", "''", "''"],
            "render_role_control_sudoers": [], "role_control_sudoers": [], "role_control_sudoers_install_commands": []}
#: team_launcher's module-level imports of them, from project_provision (its role_account_name is its own wrapper).
LAUNCHER_IMPORTS = ("NON_PROCESS_ROLES", "role_account_migration_name")
#: The callers that import a name from project_provision inside a function, when it runs, and that tests patch there.
CALL_TIME_READERS = {"scripts/project_role_add.py": "role_account_commands", "scripts/recovery_readiness.py": "roles_group_name"}
#: The packets rendered through the direct script and the package: the default plan, two the plan shapes, and one given
#: role accounts and a roles group (build_plan always leaves them empty), whose role-control sudoers has content.
ROLE_ACCOUNTS = (("director", "p470-director"), ("main", "p470-main"), ("audit", "p470-audit"))
PACKET_VARIANTS = {
    "default": ([], False),
    "shaped": (["--implementer-role", "main", "--implementer-role", "perf", "--audit-role", "audit", "--audit-role", "inspector", "--vcs-close-role", "ops"], False),
    "lean": (["--no-include-designer", "--no-include-audit"], False),
    "roles": ([], True),
}
#: One probe, run in the package and as the direct script: every name, table, command and rule for a synthetic project,
#: plan and role set -- as a digest. No account is looked up (the probe stands in pwd.getpwuid and os.getuid), and the
#: grant role_account_commands looks for is under a TENANT_CONTROL_ROOT the probe creates, set on every project_provision
#: (the staging directory is built from it too, so its path is read as CONTROL).
ROLE_PROBE = (
    "import dataclasses, hashlib, importlib, json, os, pathlib, pwd, sys, tempfile\n"
    "pwd.getpwuid = lambda uid: pwd.struct_passwd(('p470-self', 'x', uid, uid, '', '/nonexistent', '/bin/false'))\n"
    "os.getuid = lambda: 2470\n"
    "[os.environ.pop(n, None) for n in ('SUDO_UID', 'SWITCHYARD_TENANT_CONTROL_ROOT')]\n"
    "control = tempfile.mkdtemp(prefix='syrd470-probe-')\n"
    "for name in ('project_provision', 'scripts.ticket_board.project_provision'):\n"
    "    if name in sys.modules: sys.modules[name].TENANT_CONTROL_ROOT = control\n"
    "accounts = (('director', 'p470-director'), ('main', 'p470-main'), ('audit', 'p470-audit'))\n"
    "plan = dataclasses.replace(g['build_plan'](project='p470', owner_user='p470-owner', owner_home=pathlib.Path('/p470/home'), port=34470),\n"
    "                           role_accounts=accounts, roles_group='p470-roles')\n"
    "out = [g['role_account_name']('p470', 'main'), g['roles_group_name']('p470'), g['role_account_migration_name']('p470'),\n"
    "       g['role_account_table']('p470', ['director', 'main', 'user']), g['role_account_home'](plan, 'main'), g['role_accounts_env'](plan),\n"
    "       g['role_accounts_command'](plan), g['role_runtime_command'](plan), g['role_control_sudoers'](plan),\n"
    "       g['role_runtime_commands'](project='p470', runtime_directory='p470-rt', board_current='/p470/board', roles_group='p470-roles', account='p470-main', home='/home/p470-main', worktree=''),\n"
    "       g['render_role_control_sudoers']('p470', 'p470-owner', accounts), g['role_control_sudoers_install_commands']('p470', 'rule\\n'),\n"
    "       g['role_account_commands']('p470', 'main', 'p470-owner', 'boardsvc', role_accounts=accounts)]\n"
    "os.rmdir(control)\n"
    "print(len(out), hashlib.sha256(json.dumps(out).replace(control, 'CONTROL').encode()).hexdigest())\n"
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
    result = python("import sys, scripts.ticket_board.provision_role_accounts as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading only its package, never project_provision: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.ticket_board.provision_role_accounts", "scripts.ticket_board.project_provision"),
                  ("scripts.ticket_board.project_provision", "scripts.ticket_board.provision_role_accounts"),
                  ("scripts.team_launcher", "scripts.ticket_board.provision_role_accounts")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_role_accounts as m, scripts.team_launcher as tl; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"sorted({{getattr(m, n).__module__ for n in {FUNCTIONS!r}}}), "
                        f"all(getattr(tl, n) is getattr(m, n) for n in {LAUNCHER_IMPORTS!r}), "
                        "tl.role_account_name is not m.role_account_name and tl.role_account_name('p470', 'main') == m.role_account_name('p470', 'main'), "
                        f"not any(hasattr(m, n) for n in ('provision', 'project_provision', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.ticket_board.provision_role_accounts'] True True True",
              f"{' then '.join(order)}: one object each, defined here; team_launcher's two imports the module's own and its role_account_name its own wrapper; "
              f"nothing of project_provision bound at load: {result.stdout}{result.stderr[-600:]}")
    check(m.PurePosixPath is PurePosixPath and m.Sequence is Sequence and t.PurePosixPath is m.PurePosixPath,
          "the standard-library names are the module's own, the very objects project_provision holds")


def test_a_patch_on_project_provision_reaches_its_callers() -> None:
    # project_role_add and recovery_readiness import role_account_commands and roles_group_name from project_provision inside
    # a function, when it runs: a patch there (as their boundary tests make) is what they get, and the module's own otherwise.
    for path, name in CALL_TIME_READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        imports = [x for x in ast.walk(source) if isinstance(x, ast.ImportFrom) and (x.module or "").endswith("project_provision") and name in {a.name for a in x.names}]
        inside = {id(x) for fn in ast.walk(source) if isinstance(fn, ast.FunctionDef) for x in ast.walk(fn)}
        check(imports and all(id(x) in inside for x in imports), f"{path} imports {name} from project_provision inside its functions, when they run")
    result = python("import scripts.ticket_board.project_provision as t, scripts.ticket_board.provision_role_accounts as m\n"
                    "out = []\n"
                    f"for name in {tuple(CALL_TIME_READERS.values())!r}:\n"
                    "    saved = getattr(t, name); setattr(t, name, lambda *a, **k: 'PATCHED')\n"
                    "    seen = getattr(__import__('scripts.ticket_board.project_provision', fromlist=[name]), name)\n"
                    "    setattr(t, name, saved)\n"
                    "    restored = getattr(__import__('scripts.ticket_board.project_provision', fromlist=[name]), name)\n"
                    "    out.append((seen('p470'), restored is getattr(m, name)))\n"
                    "print(out)")
    check(result.stdout.strip() == "[('PATCHED', True), ('PATCHED', True)]",
          f"a patch on project_provision is what a call-time import sees; restored, it is the module's own: {result.stdout}{result.stderr[-400:]}")


def test_the_direct_script_answers_what_the_package_answers() -> None:
    # project_provision.py run as a script: its import block loads the module under its own name, and the functions'
    # fallback loads project_provision a second time beside it.
    script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
    as_script = python(f"import runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                       f"g = runpy.run_path({str(script)!r}, run_name='__syrd470_script__'); "
                       "m = sys.modules['provision_role_accounts']; import project_provision; "
                       f"print(all(g[n] is getattr(m, n) for n in {MOVED!r}))\n" + ROLE_PROBE)
    as_package = python("import scripts.ticket_board.project_provision as pp; g = vars(pp)\n" + ROLE_PROBE)
    lines = as_script.stdout.split("\n")
    check(as_script.returncode == as_package.returncode == 0 and lines[0] == "True" and lines[1] == as_package.stdout.strip() and lines[1].startswith("13 "),
          f"as a direct script: the script holds the module's own objects, and all thirteen answers are the package's, byte for byte: "
          f"{as_script.stdout}{as_script.stderr[-400:]} | {as_package.stdout}{as_package.stderr[-400:]}")


def test_the_seams_read_through_project_provision_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "provision_role_accounts.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "from pathlib import PurePosixPath", "from typing import Sequence"]
          and not [n for n in tree.body if isinstance(n, (ast.If, ast.Try, ast.ClassDef))], f"the standard library only, at load: {top}")
    consts = {top_name(n): (ast.unparse(n.annotation) if isinstance(n, ast.AnnAssign) else None, ast.unparse(n.value)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))}
    check(consts == CONSTANT_TEXT, f"the three constants are the baseline's: {consts}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == DEFAULTS, f"the defaults are the baseline's: {defaults}")
    names = [top_name(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the sixteen, in project_provision's order, and nothing else: {names}")


def test_project_provision_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "ticket_board" / "project_provision.py").read_text(encoding="utf-8"))
    guard = next(n for n in tree.body if isinstance(n, ast.Try))
    package = [n for n in guard.body if isinstance(n, ast.ImportFrom) and n.module == "provision_role_accounts" and n.level == 1]
    script = [n for h in guard.handlers for n in h.body if isinstance(n, ast.ImportFrom) and n.module == "provision_role_accounts" and n.level == 0]
    for imports in (package, script):
        check(len(imports) == 1 and [a.name for a in imports[0].names] == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
              "one explicit import of exactly the sixteen, unaliased, in both the package and the direct-script branch")
    check(guard.lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))),
          "at the top, above every definition and constant that could read them")
    defined = {top_name(n) for n in tree.body} - {None}
    imported = {a.asname or a.name for n in guard.body if isinstance(n, (ast.Import, ast.ImportFrom)) and not (isinstance(n, ast.ImportFrom) and n.module == "provision_role_accounts")
                for a in n.names}
    check(not defined & set(MOVED) and set(KEPT) <= defined | imported, "project_provision defines none of them, and keeps what they read, its own or re-exported")
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
    base = Path(tempfile.mkdtemp(prefix="syrd470-packet-")).resolve()
    try:
        for variant, (extra, roles) in PACKET_VARIANTS.items():
            outputs = {}
            for mode in ("script", "package"):
                work = base / "run"
                shutil.rmtree(work, ignore_errors=True)
                (work / "home").mkdir(parents=True)
                (work / "source").mkdir()
                argv = ["--project", "p470", "--owner-user", "p470-agent", "--owner-home", f"{work}/home", "--source-repo", f"{work}/source",
                        "--port", "34470", "--output-dir", f"{work}/out", *extra]
                given = f"dataclasses.replace(build(*a, **k), role_accounts={ROLE_ACCOUNTS!r}, roles_group='p470-roles')" if roles else "build(*a, **k)"
                script = ROOT / "scripts" / "ticket_board" / "project_provision.py"
                if mode == "script":
                    probe = (f"import dataclasses, runpy, sys; sys.path.insert(0, {str(script.parent)!r}); "
                             f"g = runpy.run_path({str(script)!r}, run_name='syrd470_script'); main = g['main']; build = main.__globals__['build_plan']; "
                             f"main.__globals__['build_plan'] = lambda *a, **k: {given}; raise SystemExit(main({argv!r}))")
                else:
                    probe = ("import dataclasses, scripts.ticket_board.project_provision as pp; build = pp.build_plan; "
                             f"pp.build_plan = lambda *a, **k: {given}; raise SystemExit(pp.main({argv!r}))")
                result = python(probe)
                files = {str(p.relative_to(work / "out")): p.read_bytes() for p in sorted((work / "out").rglob("*")) if p.is_file()}
                outputs[mode] = (result.returncode, result.stdout, files)
            check(outputs["script"][0] == outputs["package"][0] == 0 and outputs["script"][1] == outputs["package"][1]
                  and outputs["script"][2] == outputs["package"][2] and len(outputs["package"][2]) == PACKET_FILES[variant],
                  f"{variant}: the direct script and the package render the same packet, byte for byte: {sorted(outputs['script'][2])} {outputs['script'][1][-300:]}")
            files = outputs["package"][2]
            rule, commands = files.get("49-p470-role-control", b""), files.get("operator-commands.sh", b"")
            if roles:
                check(b"p470-director ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux" in rule and b"p470-main" in commands and b"p470-roles" in commands,
                      f"{variant}: the role-control sudoers and the operator commands name the role accounts: {rule[:300]!r}")
            else:
                check(rule == b"", f"{variant}: with no role accounts, the role-control sudoers is empty: {rule[:200]!r}")
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

    check(value("account name") == "p470-main" and value("group name") == "p470-roles" and value("migration name") == "p470-role-accounts.sh"
          and value("migration name: the suffix rebound on project_provision") == "p470.sh"
          and value("account home") == "/home/p470-main" and value("account home: the name rebound on project_provision") == "/home/acct-main",
          "an account is <project>-<role> and lives in /home/<account>; the group <project>-roles; the migration <project> plus the suffix read through project_provision")
    check(value("account table") == [["director", "p470-director"], ["main", "p470-main"], ["audit", "p470-audit"]]
          and value("account table: the non-process roles rebound on project_provision") == [["director", "p470-director"], ["user", "p470-user"]]
          and value("account table: the name rebound on project_provision") == [["main", "acct-main"]],
          "the table: each caller role once, stripped and lower-cased, in order, never a non-process role -- both read through project_provision")
    check(value("accounts env: no role accounts") == "" and value("accounts env: role accounts") == "director=p470-director,main=p470-main,audit=p470-audit",
          "the board's role map: role=account, comma-separated, in the plan's order")
    check(value("accounts command: no role accounts") == value("accounts command: role accounts, no group") == ""
          and value("accounts command: role accounts") != value("accounts command: no director")
          and calls("accounts command: role accounts")["role_account_home"] == 3 and calls("accounts command: no director")["role_account_home"] == 2,
          "account creation: nothing without both role accounts and a group, else one home per account")
    check(calls("runtime command: role accounts")["role_runtime_commands"] == 3 and calls("runtime command: no director")["role_runtime_commands"] == 2
          and "role_runtime_commands" not in calls("runtime command: no role accounts")
          and all(calls(f"runtime command: {p}")["role_tooling_staging_commands"] == 1 for p in PLANS)
          and value("runtime command: role accounts, no group") == value("runtime command: no role accounts")
          and value("runtime command: the release rebound on project_provision") != value("runtime command: role accounts"),
          "the runtime: the staged bundle for every tenant, then one runtime per role account when there is a group -- the release read through project_provision")
    rule = value("render control sudoers: with a director").split("\n")
    check(value("render control sudoers: none") == "" and value("control sudoers: no role accounts") == ""
          and rule[3] == "p470-owner ALL=(p470-director,p470-main,p470-audit) NOPASSWD: /usr/bin/tmux"
          and rule[4] == "p470-director ALL=(p470-main,p470-audit) NOPASSWD: /usr/bin/tmux"
          and rule[5] == "p470-director ALL=(p470-owner) NOPASSWD: /usr/bin/tmux" and rule[-1] == ""
          and len(value("render control sudoers: without a director").split("\n")) == 5
          and value("render control sudoers: only a director").split("\n")[3:] == ["p470-owner ALL=(p470-director) NOPASSWD: /usr/bin/tmux",
                                                                                   "p470-director ALL=(p470-owner) NOPASSWD: /usr/bin/tmux", ""]
          and value("control sudoers: role accounts") == value("render control sudoers: with a director")
          and value("control sudoers: the renderer rebound on project_provision").startswith("RULE p470 p470-owner"),
          "the control rule: tmux only -- the owner as every role account, the director as the other role accounts and as the owner; nothing without role accounts")
    install = value("install commands: a document")
    check(value("install commands: an empty document") == value("install commands: a blank document") == []
          and install[0] == "sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/49-p470-role-control.staged <<'SWITCHYARD_ROLE_CONTROL_SUDOERS'"
          and install[1] == "p470-owner ALL=(p470-main) NOPASSWD: /usr/bin/tmux" and install[2] == "SWITCHYARD_ROLE_CONTROL_SUDOERS"
          and install[3] == "sudo visudo -c -f /etc/sudoers.d/49-p470-role-control.staged"
          and install[4] == "sudo mv /etc/sudoers.d/49-p470-role-control.staged /etc/sudoers.d/49-p470-role-control"
          and value("install commands: the heredoc marker rebound on project_provision")[2] == "P470_MARK",
          "the install: the embedded rule staged at 0440 through a heredoc, checked by visudo, then moved in -- the marker read through project_provision")
    check(value("add role: defaults") != value("add role: no director") != value("add role: a control repository and worktree")
          and value("add role: a recorded human gets the tenant control bridge") != value("add role: defaults")
          and calls("add role: defaults")["tenant_control_commands"] == 1 and calls("add role: defaults")["invoking_human"] == 1
          and "role_worktree_access_commands" in calls("add role: a control repository and worktree")
          and "role_worktree_access_commands" not in calls("add role: defaults")
          and {"render_role_control_sudoers rebound", "role_control_sudoers_install_commands rebound", "role_runtime_commands rebound",
               "role_tooling_staging_commands rebound", "tenant_control_commands rebound"} <= set(calls("add role: the helpers rebound on project_provision")),
          "adding a role: the staged bundle, its runtime, repository access when there is a control repository, the refreshed control rule and the "
          "tenant control bridge for the recorded human -- every helper read through project_provision")
    check(value("constants") == [["unassigned", "user"], "-role-accounts.sh", "SWITCHYARD_ROLE_CONTROL_SUDOERS"], "the non-process roles, the migration suffix and the heredoc marker")


def test_every_seam_is_reached() -> None:
    # Every function the thirteen read on project_provision is a recorder there; the constants are rebound by their own cases.
    names = {name for reads in SEAMS.values() for name in reads}
    functions = set(PASSED)
    check(functions <= names and functions <= REACHED, f"a recorder on project_provision reached every function: missing {sorted(functions - REACHED)}")
    check(names - functions <= set(REBINDABLE), f"every other seam is a rebindable name: {sorted(names - functions - set(REBINDABLE))}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects",
             "test_a_patch_on_project_provision_reaches_its_callers", "test_the_direct_script_answers_what_the_package_answers",
             "test_the_seams_read_through_project_provision_and_nothing_bound", "test_project_provision_reexports_them_and_its_readers_reach_them_there",
             "test_the_direct_script_and_the_package_render_the_same_packets")
LAST = ("test_every_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"provision_role_accounts_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
