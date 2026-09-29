#!/usr/bin/env python3
"""SYRD-432: the Switchyard command table and help, against the launcher they came out of.

The verb tables -- `SWITCHYARD_COMMANDS`, `SWITCHYARD_UNPRIVILEGED_COMMANDS` and
`SWITCHYARD_PRIVILEGED_COMMANDS` (the rest, computed when the module loads) --
the help `switchyard --help` prints (`switchyard_help_text`) and the answer the
installed wrapper asks for before it crosses to root
(`switchyard_invocation_requires_root`) moved unchanged into
`scripts/switchyard_commands.py`; the launcher re-exports all five. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; the
  privileged set is computed from the two tables beside it, the very objects
  the launcher re-exports.
- **Seams (rule 24):** the help reads the verb list, and the root check the
  privileged set and the tenant-control check, through the launcher when they
  run, so a patch there reaches them: every case below records the functions
  and the check there, and rebinds both tables there to see the effect.
- **Callers:** `switchyard_main` calls the launcher's names, and
  `command_crossing.py` reads the unprivileged set there.
- **The behaviour is the baseline's:** the complete tables, the help (directly
  and through `switchyard_main`), every verb's root decision served and not
  served over the tenant-control bridge, help and version flags, a verb's own
  help, case-folding, unknown verbs and bare project names, and the installed
  wrapper's question. `GOLDEN` below was produced by running the BASELINE
  launcher's own definitions over the very cases embedded here (`gold432.py`),
  not typed; it is byte-identical under `env -i`, in a normal role pane and
  with another HOME, USER and COLUMNS.

Nothing touches the host: the tenant-control check and the release notice are
stand-ins, and spawns, every exec, signals, account and group lookups and
socket connections are refused for each case.
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
from scripts import switchyard_commands as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ('SWITCHYARD_COMMANDS', 'SWITCHYARD_UNPRIVILEGED_COMMANDS', 'SWITCHYARD_PRIVILEGED_COMMANDS', 'switchyard_help_text', 'switchyard_invocation_requires_root')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'switchyard_help_text': {'SWITCHYARD_COMMANDS': 1},
    'switchyard_invocation_requires_root': {'SWITCHYARD_PRIVILEGED_COMMANDS': 1, '_tenant_control_can_serve': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the five that names them, and how often.
DISPATCH = {'switchyard_main': {'switchyard_help_text': 1, 'switchyard_invocation_requires_root': 1}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/command_crossing.py': ['launcher.SWITCHYARD_UNPRIVILEGED_COMMANDS']}
#: The BASELINE's own behaviour for the cases below (`gold432.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'the three tables': {'result': {'commands': ['board-skill', 'new', 'register', 'upgrade', 'repair-boundary', 'approve-desktop', 'adopt-workflow', 'migrate-workflow', 'rebind-workflow-panes', 'resume-provision', 'finish-upgrade', 'cutover-roles', 'publication-status', 'rollout-log', 'recover-display', 'privileged-action', 'install-shared-release', 'release-status', 'add-role', 'worker-pool', 'set-owner-identity', 'present', 'attach', 'replace-window', 'set-vcs-close-role', 'set-role-runtime', 'agy-credential', 'seed-role-credentials', 'role-prompt', 'onboarding-readiness', 'stop', 'start', 'teardown', 'status', 'validate-models'], 'privileged': {'SET': ['add-role', 'adopt-workflow', 'agy-credential', 'approve-desktop', 'cutover-roles', 'install-shared-release', 'migrate-workflow', 'new', 'onboarding-readiness', 'publication-status', 'rebind-workflow-panes', 'recover-display', 'register', 'release-status', 'repair-boundary', 'replace-window', 'resume-provision', 'rollout-log', 'seed-role-credentials', 'set-owner-identity', 'set-vcs-close-role', 'start', 'stop', 'teardown', 'upgrade', 'validate-models']}, 'the same objects on the holder': [True, True, True], 'unprivileged': {'SET': ['attach', 'board-skill', 'finish-upgrade', 'present', 'privileged-action', 'role-prompt', 'set-role-runtime', 'status', 'worker-pool']}}, 'calls': [], 'printed': ''},
    'the help': {'result': "Usage:\n  switchyard\n  switchyard <project name or slug>\n  switchyard <command> [options]\n\nCommands:\n  board-skill      install or verify the portable board skill for every agent CLI\n  new              create and provision a new project\n  register         register an existing project config\n  upgrade          update generated project artifacts and report release drift\n  repair-boundary  apply the reviewed repository boundary to a registered tenant\n  approve-desktop  record, show or withdraw this host's standing desktop approval\n  adopt-workflow   record an existing project's declared workflow as root's own copy\n  finish-upgrade   run the director-owned phase of an upgrade from the director's session\n  cutover-roles    legacy compatibility command (new runtimes use the project account)\n  add-role         add an implementer or auditor role, worktree, pane, and board registration\n  worker-pool      plan, apply and run a project's declared pool of interchangeable workers\n  present          map persistent role sessions into stable display slots at runtime\n  attach           attach this terminal to a role's live worker by project and role name\n  replace-window   replace a root-owned presentation window without stopping any worker\n  recover-display  reattach a project's disconnected Director display, as its desktop operator\n  set-vcs-close-role\n                   set which existing project role can mark tickets done\n  set-role-runtime change an existing role's agent runtime and reconnect its panes\n  agy-credential   show, set, or clear this host's agy credential source\n  role-prompt      show, set, or clear a role's onboarding prompt\n  onboarding-readiness\n                   report whether every registered tenant has migrated director onboarding\n  stop             suspend a project: window, sessions, listener and board, reversibly\n  start            resume a suspended project in dependency order\n  teardown         remove project board provisioning artifacts after a dry-run review\n  release-status   compare the shared release, deployed board, live build and both journals\n  status           list registered projects and pane liveness\n  validate-models  check configured role models without starting panes\n\nBare project names start or attach the project. Recognized commands: board-skill, new, register, upgrade, repair-boundary, approve-desktop, adopt-workflow, migrate-workflow, rebind-workflow-panes, resume-provision, finish-upgrade, cutover-roles, publication-status, rollout-log, recover-display, privileged-action, install-shared-release, release-status, add-role, worker-pool, set-owner-identity, present, attach, replace-window, set-vcs-close-role, set-role-runtime, agy-credential, seed-role-credentials, role-prompt, onboarding-readiness, stop, start, teardown, status, validate-models.\n", 'calls': [], 'printed': ''},
    'the help, the verb list rebound on the launcher': {'result': "Usage:\n  switchyard\n  switchyard <project name or slug>\n  switchyard <command> [options]\n\nCommands:\n  board-skill      install or verify the portable board skill for every agent CLI\n  new              create and provision a new project\n  register         register an existing project config\n  upgrade          update generated project artifacts and report release drift\n  repair-boundary  apply the reviewed repository boundary to a registered tenant\n  approve-desktop  record, show or withdraw this host's standing desktop approval\n  adopt-workflow   record an existing project's declared workflow as root's own copy\n  finish-upgrade   run the director-owned phase of an upgrade from the director's session\n  cutover-roles    legacy compatibility command (new runtimes use the project account)\n  add-role         add an implementer or auditor role, worktree, pane, and board registration\n  worker-pool      plan, apply and run a project's declared pool of interchangeable workers\n  present          map persistent role sessions into stable display slots at runtime\n  attach           attach this terminal to a role's live worker by project and role name\n  replace-window   replace a root-owned presentation window without stopping any worker\n  recover-display  reattach a project's disconnected Director display, as its desktop operator\n  set-vcs-close-role\n                   set which existing project role can mark tickets done\n  set-role-runtime change an existing role's agent runtime and reconnect its panes\n  agy-credential   show, set, or clear this host's agy credential source\n  role-prompt      show, set, or clear a role's onboarding prompt\n  onboarding-readiness\n                   report whether every registered tenant has migrated director onboarding\n  stop             suspend a project: window, sessions, listener and board, reversibly\n  start            resume a suspended project in dependency order\n  teardown         remove project board provisioning artifacts after a dry-run review\n  release-status   compare the shared release, deployed board, live build and both journals\n  status           list registered projects and pane liveness\n  validate-models  check configured role models without starting panes\n\nBare project names start or attach the project. Recognized commands: syrd432, verbs.\n", 'calls': [], 'printed': ''},
    'every verb in the table, not served': {'result': {'add-role': [True, False], 'adopt-workflow': [True, False], 'agy-credential': [True, False], 'approve-desktop': [True, False], 'attach': [False, False], 'board-skill': [False, False], 'cutover-roles': [True, False], 'finish-upgrade': [False, False], 'install-shared-release': [True, False], 'migrate-workflow': [True, False], 'new': [True, False], 'onboarding-readiness': [True, False], 'present': [False, False], 'privileged-action': [False, False], 'publication-status': [True, False], 'rebind-workflow-panes': [True, False], 'recover-display': [True, False], 'register': [True, False], 'release-status': [True, False], 'repair-boundary': [True, False], 'replace-window': [True, False], 'resume-provision': [True, False], 'role-prompt': [False, False], 'rollout-log': [True, False], 'seed-role-credentials': [True, False], 'set-owner-identity': [True, False], 'set-role-runtime': [False, False], 'set-vcs-close-role': [True, False], 'start': [True, False], 'status': [False, False], 'stop': [True, False], 'teardown': [True, False], 'upgrade': [True, False], 'validate-models': [True, False], 'worker-pool': [False, False]}, 'calls': [['_tenant_control_can_serve', [['new', 'p432']], {}], ['_tenant_control_can_serve', [['register', 'p432']], {}], ['_tenant_control_can_serve', [['upgrade', 'p432']], {}], ['_tenant_control_can_serve', [['repair-boundary', 'p432']], {}], ['_tenant_control_can_serve', [['approve-desktop', 'p432']], {}], ['_tenant_control_can_serve', [['adopt-workflow', 'p432']], {}], ['_tenant_control_can_serve', [['migrate-workflow', 'p432']], {}], ['_tenant_control_can_serve', [['rebind-workflow-panes', 'p432']], {}], ['_tenant_control_can_serve', [['resume-provision', 'p432']], {}], ['_tenant_control_can_serve', [['cutover-roles', 'p432']], {}], ['_tenant_control_can_serve', [['publication-status', 'p432']], {}], ['_tenant_control_can_serve', [['rollout-log', 'p432']], {}], ['_tenant_control_can_serve', [['recover-display', 'p432']], {}], ['_tenant_control_can_serve', [['install-shared-release', 'p432']], {}], ['_tenant_control_can_serve', [['release-status', 'p432']], {}], ['_tenant_control_can_serve', [['add-role', 'p432']], {}], ['_tenant_control_can_serve', [['set-owner-identity', 'p432']], {}], ['_tenant_control_can_serve', [['replace-window', 'p432']], {}], ['_tenant_control_can_serve', [['set-vcs-close-role', 'p432']], {}], ['_tenant_control_can_serve', [['agy-credential', 'p432']], {}], ['_tenant_control_can_serve', [['seed-role-credentials', 'p432']], {}], ['_tenant_control_can_serve', [['onboarding-readiness', 'p432']], {}], ['_tenant_control_can_serve', [['stop', 'p432']], {}], ['_tenant_control_can_serve', [['start', 'p432']], {}], ['_tenant_control_can_serve', [['teardown', 'p432']], {}], ['_tenant_control_can_serve', [['validate-models', 'p432']], {}]], 'printed': ''},
    'every verb in the table, served over the bridge': {'result': {'add-role': [False, False], 'adopt-workflow': [False, False], 'agy-credential': [False, False], 'approve-desktop': [False, False], 'attach': [False, False], 'board-skill': [False, False], 'cutover-roles': [False, False], 'finish-upgrade': [False, False], 'install-shared-release': [False, False], 'migrate-workflow': [False, False], 'new': [False, False], 'onboarding-readiness': [False, False], 'present': [False, False], 'privileged-action': [False, False], 'publication-status': [False, False], 'rebind-workflow-panes': [False, False], 'recover-display': [False, False], 'register': [False, False], 'release-status': [False, False], 'repair-boundary': [False, False], 'replace-window': [False, False], 'resume-provision': [False, False], 'role-prompt': [False, False], 'rollout-log': [False, False], 'seed-role-credentials': [False, False], 'set-owner-identity': [False, False], 'set-role-runtime': [False, False], 'set-vcs-close-role': [False, False], 'start': [False, False], 'status': [False, False], 'stop': [False, False], 'teardown': [False, False], 'upgrade': [False, False], 'validate-models': [False, False], 'worker-pool': [False, False]}, 'calls': [['_tenant_control_can_serve', [['new', 'p432']], {}], ['_tenant_control_can_serve', [['register', 'p432']], {}], ['_tenant_control_can_serve', [['upgrade', 'p432']], {}], ['_tenant_control_can_serve', [['repair-boundary', 'p432']], {}], ['_tenant_control_can_serve', [['approve-desktop', 'p432']], {}], ['_tenant_control_can_serve', [['adopt-workflow', 'p432']], {}], ['_tenant_control_can_serve', [['migrate-workflow', 'p432']], {}], ['_tenant_control_can_serve', [['rebind-workflow-panes', 'p432']], {}], ['_tenant_control_can_serve', [['resume-provision', 'p432']], {}], ['_tenant_control_can_serve', [['cutover-roles', 'p432']], {}], ['_tenant_control_can_serve', [['publication-status', 'p432']], {}], ['_tenant_control_can_serve', [['rollout-log', 'p432']], {}], ['_tenant_control_can_serve', [['recover-display', 'p432']], {}], ['_tenant_control_can_serve', [['install-shared-release', 'p432']], {}], ['_tenant_control_can_serve', [['release-status', 'p432']], {}], ['_tenant_control_can_serve', [['add-role', 'p432']], {}], ['_tenant_control_can_serve', [['set-owner-identity', 'p432']], {}], ['_tenant_control_can_serve', [['replace-window', 'p432']], {}], ['_tenant_control_can_serve', [['set-vcs-close-role', 'p432']], {}], ['_tenant_control_can_serve', [['agy-credential', 'p432']], {}], ['_tenant_control_can_serve', [['seed-role-credentials', 'p432']], {}], ['_tenant_control_can_serve', [['onboarding-readiness', 'p432']], {}], ['_tenant_control_can_serve', [['stop', 'p432']], {}], ['_tenant_control_can_serve', [['start', 'p432']], {}], ['_tenant_control_can_serve', [['teardown', 'p432']], {}], ['_tenant_control_can_serve', [['validate-models', 'p432']], {}]], 'printed': ''},
    'root: no arguments': {'result': False, 'calls': [], 'printed': ''},
    'root: --help': {'result': False, 'calls': [], 'printed': ''},
    'root: -h': {'result': False, 'calls': [], 'printed': ''},
    'root: help': {'result': False, 'calls': [], 'printed': ''},
    'root: --version': {'result': False, 'calls': [], 'printed': ''},
    'root: version': {'result': False, 'calls': [], 'printed': ''},
    'root: a privileged verb': {'result': True, 'calls': [['_tenant_control_can_serve', [['upgrade', 'p432']], {}]], 'printed': ''},
    'root: a privileged verb, served over the bridge': {'result': False, 'calls': [['_tenant_control_can_serve', [['stop', 'p432']], {}]], 'printed': ''},
    'root: a privileged verb, not served': {'result': True, 'calls': [['_tenant_control_can_serve', [['stop', 'p432']], {}]], 'printed': ''},
    'root: an unprivileged verb': {'result': False, 'calls': [], 'printed': ''},
    'root: an unprivileged verb the bridge would serve': {'result': False, 'calls': [], 'printed': ''},
    "root: a privileged verb's own -h": {'result': False, 'calls': [], 'printed': ''},
    "root: a privileged verb's own --help": {'result': False, 'calls': [], 'printed': ''},
    "root: help as a verb's argument is not a help flag": {'result': True, 'calls': [['_tenant_control_can_serve', [['upgrade', 'help']], {}]], 'printed': ''},
    'root: a verb in capitals': {'result': True, 'calls': [['_tenant_control_can_serve', [['UPGRADE', 'p432']], {}]], 'printed': ''},
    'root: a verb in capitals, served': {'result': False, 'calls': [['_tenant_control_can_serve', [['STOP', 'p432']], {}]], 'printed': ''},
    'root: an unknown verb': {'result': False, 'calls': [], 'printed': ''},
    'root: a bare project name': {'result': False, 'calls': [], 'printed': ''},
    'root: a bare project name with a space': {'result': False, 'calls': [], 'printed': ''},
    'root: the privileged set rebound on the launcher': {'result': True, 'calls': [['_tenant_control_can_serve', [['frobnicate']], {}]], 'printed': ''},
    'root: the privileged set rebound empty': {'result': False, 'calls': [], 'printed': ''},
    'root: -h never crosses, even were it a privileged verb': {'result': False, 'calls': [], 'printed': ''},
    'root: --help never crosses, even were it a privileged verb': {'result': False, 'calls': [], 'printed': ''},
    'root: help never crosses, even were it a privileged verb': {'result': False, 'calls': [], 'printed': ''},
    'root: --version never crosses, even were it a privileged verb': {'result': False, 'calls': [], 'printed': ''},
    'root: version never crosses, even were it a privileged verb': {'result': False, 'calls': [], 'printed': ''},
    'switchyard --help, through switchyard_main': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['switchyard_help_text', [], {}]], 'printed': "Usage:\n  switchyard\n  switchyard <project name or slug>\n  switchyard <command> [options]\n\nCommands:\n  board-skill      install or verify the portable board skill for every agent CLI\n  new              create and provision a new project\n  register         register an existing project config\n  upgrade          update generated project artifacts and report release drift\n  repair-boundary  apply the reviewed repository boundary to a registered tenant\n  approve-desktop  record, show or withdraw this host's standing desktop approval\n  adopt-workflow   record an existing project's declared workflow as root's own copy\n  finish-upgrade   run the director-owned phase of an upgrade from the director's session\n  cutover-roles    legacy compatibility command (new runtimes use the project account)\n  add-role         add an implementer or auditor role, worktree, pane, and board registration\n  worker-pool      plan, apply and run a project's declared pool of interchangeable workers\n  present          map persistent role sessions into stable display slots at runtime\n  attach           attach this terminal to a role's live worker by project and role name\n  replace-window   replace a root-owned presentation window without stopping any worker\n  recover-display  reattach a project's disconnected Director display, as its desktop operator\n  set-vcs-close-role\n                   set which existing project role can mark tickets done\n  set-role-runtime change an existing role's agent runtime and reconnect its panes\n  agy-credential   show, set, or clear this host's agy credential source\n  role-prompt      show, set, or clear a role's onboarding prompt\n  onboarding-readiness\n                   report whether every registered tenant has migrated director onboarding\n  stop             suspend a project: window, sessions, listener and board, reversibly\n  start            resume a suspended project in dependency order\n  teardown         remove project board provisioning artifacts after a dry-run review\n  release-status   compare the shared release, deployed board, live build and both journals\n  status           list registered projects and pane liveness\n  validate-models  check configured role models without starting panes\n\nBare project names start or attach the project. Recognized commands: board-skill, new, register, upgrade, repair-boundary, approve-desktop, adopt-workflow, migrate-workflow, rebind-workflow-panes, resume-provision, finish-upgrade, cutover-roles, publication-status, rollout-log, recover-display, privileged-action, install-shared-release, release-status, add-role, worker-pool, set-owner-identity, present, attach, replace-window, set-vcs-close-role, set-role-runtime, agy-credential, seed-role-credentials, role-prompt, onboarding-readiness, stop, start, teardown, status, validate-models.\n"},
    'switchyard help, through switchyard_main': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['switchyard_help_text', [], {}]], 'printed': "Usage:\n  switchyard\n  switchyard <project name or slug>\n  switchyard <command> [options]\n\nCommands:\n  board-skill      install or verify the portable board skill for every agent CLI\n  new              create and provision a new project\n  register         register an existing project config\n  upgrade          update generated project artifacts and report release drift\n  repair-boundary  apply the reviewed repository boundary to a registered tenant\n  approve-desktop  record, show or withdraw this host's standing desktop approval\n  adopt-workflow   record an existing project's declared workflow as root's own copy\n  finish-upgrade   run the director-owned phase of an upgrade from the director's session\n  cutover-roles    legacy compatibility command (new runtimes use the project account)\n  add-role         add an implementer or auditor role, worktree, pane, and board registration\n  worker-pool      plan, apply and run a project's declared pool of interchangeable workers\n  present          map persistent role sessions into stable display slots at runtime\n  attach           attach this terminal to a role's live worker by project and role name\n  replace-window   replace a root-owned presentation window without stopping any worker\n  recover-display  reattach a project's disconnected Director display, as its desktop operator\n  set-vcs-close-role\n                   set which existing project role can mark tickets done\n  set-role-runtime change an existing role's agent runtime and reconnect its panes\n  agy-credential   show, set, or clear this host's agy credential source\n  role-prompt      show, set, or clear a role's onboarding prompt\n  onboarding-readiness\n                   report whether every registered tenant has migrated director onboarding\n  stop             suspend a project: window, sessions, listener and board, reversibly\n  start            resume a suspended project in dependency order\n  teardown         remove project board provisioning artifacts after a dry-run review\n  release-status   compare the shared release, deployed board, live build and both journals\n  status           list registered projects and pane liveness\n  validate-models  check configured role models without starting panes\n\nBare project names start or attach the project. Recognized commands: board-skill, new, register, upgrade, repair-boundary, approve-desktop, adopt-workflow, migrate-workflow, rebind-workflow-panes, resume-provision, finish-upgrade, cutover-roles, publication-status, rollout-log, recover-display, privileged-action, install-shared-release, release-status, add-role, worker-pool, set-owner-identity, present, attach, replace-window, set-vcs-close-role, set-role-runtime, agy-credential, seed-role-credentials, role-prompt, onboarding-readiness, stop, start, teardown, status, validate-models.\n"},
    "the wrapper's question, a privileged verb": {'result': 0, 'calls': [['switchyard_invocation_requires_root', [['upgrade', 'p432']], {}], ['_tenant_control_can_serve', [['upgrade', 'p432']], {}]], 'printed': 'requires-root\n'},
    "the wrapper's question, served over the bridge": {'result': 0, 'calls': [['switchyard_invocation_requires_root', [['stop', 'p432']], {}], ['_tenant_control_can_serve', [['stop', 'p432']], {}]], 'printed': 'no-root\n'},
    "the wrapper's question, an unprivileged verb": {'result': 0, 'calls': [['switchyard_invocation_requires_root', [['status']], {}]], 'printed': 'no-root\n'},
    "the wrapper's question, nothing asked": {'result': 0, 'calls': [['switchyard_invocation_requires_root', [[]], {}]], 'printed': 'no-root\n'},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold432.py` (which ran them on the baseline) ------------------------------------
# A case reads the verb tables, the help, or the root decision -- directly, or through `switchyard_main` -- with every
# name they read a recorder on the launcher: the tenant-control check (which reads root-owned grants on a real host)
# answers from the case, as does the installed-release notice `switchyard_main` prints first. Recorded, in order: every
# call with its arguments, everything printed, and the result or the exact refusal.
ROOT_CASES = {
    "no arguments": [],
    "--help": ["--help"], "-h": ["-h"], "help": ["help"], "--version": ["--version"], "version": ["version"],
    "a privileged verb": ["upgrade", "p432"],
    "a privileged verb, served over the bridge": {"argv": ["stop", "p432"], "served": True},
    "a privileged verb, not served": {"argv": ["stop", "p432"], "served": False},
    "an unprivileged verb": ["status"],
    "an unprivileged verb the bridge would serve": {"argv": ["attach", "p432", "main"], "served": True},
    "a privileged verb's own -h": ["upgrade", "-h"],
    "a privileged verb's own --help": ["teardown", "--help"],
    "help as a verb's argument is not a help flag": ["upgrade", "help"],
    "a verb in capitals": ["UPGRADE", "p432"],
    "a verb in capitals, served": {"argv": ["STOP", "p432"], "served": True},
    "an unknown verb": ["frobnicate"],
    "a bare project name": ["p432"],
    "a bare project name with a space": ["P 432"],
    "the privileged set rebound on the launcher": {"argv": ["frobnicate"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset({"frobnicate"})}},
    "the privileged set rebound empty": {"argv": ["upgrade", "p432"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset()}},
    "-h never crosses, even were it a privileged verb": {"argv": ["-h"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset({"-h", "--help", "help", "--version", "version"})}},
    "--help never crosses, even were it a privileged verb": {"argv": ["--help"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset({"-h", "--help", "help", "--version", "version"})}},
    "help never crosses, even were it a privileged verb": {"argv": ["help"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset({"-h", "--help", "help", "--version", "version"})}},
    "--version never crosses, even were it a privileged verb": {"argv": ["--version"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset({"-h", "--help", "help", "--version", "version"})}},
    "version never crosses, even were it a privileged verb": {"argv": ["version"], "launcher": {"SWITCHYARD_PRIVILEGED_COMMANDS": frozenset({"-h", "--help", "help", "--version", "version"})}},
}
CASES = {
    "the three tables": {"call": "tables"},
    "the help": {"call": "help"},
    "the help, the verb list rebound on the launcher": {"call": "help", "launcher": {"SWITCHYARD_COMMANDS": ("syrd432", "verbs")}},
    "every verb in the table, not served": {"call": "every", "served": False},
    "every verb in the table, served over the bridge": {"call": "every", "served": True},
    **{f"root: {k}": ({"call": "root", "argv": v} if isinstance(v, list) else {"call": "root", **v}) for k, v in ROOT_CASES.items()},
    "switchyard --help, through switchyard_main": {"call": "main", "argv": ["--help"]},
    "switchyard help, through switchyard_main": {"call": "main", "argv": ["help"]},
    "the wrapper's question, a privileged verb": {"call": "main", "argv": ["--switchyard-wrapper-requires-root", "upgrade", "p432"]},
    "the wrapper's question, served over the bridge": {"call": "main", "argv": ["--switchyard-wrapper-requires-root", "stop", "p432"], "served": True},
    "the wrapper's question, an unprivileged verb": {"call": "main", "argv": ["--switchyard-wrapper-requires-root", "status"]},
    "the wrapper's question, nothing asked": {"call": "main", "argv": ["--switchyard-wrapper-requires-root"]},
}
FUNCTIONS = ("switchyard_help_text", "switchyard_invocation_requires_root")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions; every name they read a recorder on `t`, the launcher."""
    import contextlib
    from types import SimpleNamespace
    calls: list = []
    printed: list = []

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, (set, frozenset)):
            return {"SET": sorted(norm(v) for v in value)}
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    names = [*FUNCTIONS, "SWITCHYARD_COMMANDS", "SWITCHYARD_UNPRIVILEGED_COMMANDS", "SWITCHYARD_PRIVILEGED_COMMANDS",
             "_tenant_control_can_serve", "report_installed_release_version"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            return f

        def can_serve(argv):
            note("_tenant_control_can_serve", list(argv))
            return spec.get("served", False)

        on_launcher = {n: passthrough(n) for n in FUNCTIONS}
        on_launcher.update(_tenant_control_can_serve=can_serve,
                           report_installed_release_version=lambda: note("report_installed_release_version"))
        on_launcher.update(spec.get("launcher", {}))
        for n, f in on_launcher.items():
            setattr(t, n, f)
        call = spec["call"]
        try:
            with contextlib.redirect_stdout(SimpleNamespace(write=lambda s: printed.append(s) if s else None, flush=lambda: None)):
                if call == "tables":
                    got = {"commands": saved["SWITCHYARD_COMMANDS"], "unprivileged": saved["SWITCHYARD_UNPRIVILEGED_COMMANDS"],
                           "privileged": saved["SWITCHYARD_PRIVILEGED_COMMANDS"],
                           "the same objects on the holder": [getattr(holder, n, None) is saved[n] for n in
                                                              ("SWITCHYARD_COMMANDS", "SWITCHYARD_UNPRIVILEGED_COMMANDS", "SWITCHYARD_PRIVILEGED_COMMANDS")]}
                elif call == "help":
                    got = under["switchyard_help_text"]()
                elif call == "every":
                    got = {verb: [under["switchyard_invocation_requires_root"]([verb, "p432"]),
                                  under["switchyard_invocation_requires_root"]([verb, "--help"])] for verb in saved["SWITCHYARD_COMMANDS"]}
                elif call == "root":
                    got = under["switchyard_invocation_requires_root"](list(spec["argv"]))
                else:
                    got = t.switchyard_main(list(spec["argv"]))
            result = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"result": result, "calls": calls, "printed": "".join(printed)}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
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
    result = python("import sys, scripts.switchyard_commands as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.switchyard_commands", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.switchyard_commands")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.switchyard_commands as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty), "
                        "m.SWITCHYARD_PRIVILEGED_COMMANDS == frozenset(c for c in m.SWITCHYARD_COMMANDS if c not in m.SWITCHYARD_UNPRIVILEGED_COMMANDS), "
                        "not hasattr(m, 'launcher'))")
        check(result.stdout.strip() == "True [] True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.Sequence is typing.Sequence, "the one standard-library name is the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "switchyard_commands.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, the verb tables included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported first thing, and nothing else nested (the baseline had no nested import): {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs)]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "from typing import Sequence"] and tc == [],
          f"the standard library only, and no TYPE_CHECKING block: {top} {tc}")
    tables = [n for n in tree.body if isinstance(n, ast.Assign)]
    privileged = next(n for n in tables if n.targets[0].id == "SWITCHYARD_PRIVILEGED_COMMANDS")
    reads = sorted({x.id for x in ast.walk(privileged.value) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)})
    check(reads == ["SWITCHYARD_COMMANDS", "SWITCHYARD_UNPRIVILEGED_COMMANDS", "command", "frozenset"]
          and not any(isinstance(x, ast.Name) and x.id == "launcher" for n in tables for x in ast.walk(n)),
          f"the privileged set is computed when the module loads, from the two tables beside it, never through the launcher: {reads}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the five in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_five_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.switchyard_commands"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the five, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"switchyard_main", "MAX_VISIBLE_PANES_PER_WINDOW", "SUPPORTED_CONFIG_CLI_NAMES",
                                                  "_project_config_path_owner_user"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"switchyard_main calls them by their launcher globals, exactly as often as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads the unprivileged set through the launcher: {got}")


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

    def seams(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    tables = result("the three tables")
    commands, unprivileged, privileged = tables["commands"], tables["unprivileged"]["SET"], tables["privileged"]["SET"]
    check(len(commands) == len(set(commands)) and set(unprivileged) <= set(commands)
          and privileged == sorted(c for c in commands if c not in unprivileged) and tables["the same objects on the holder"] == [True, True, True],
          "every verb once; the privileged set is exactly the verbs not marked unprivileged")
    check(result("the help").endswith(f"Recognized commands: {', '.join(commands)}.\n")
          and result("the help, the verb list rebound on the launcher").endswith("Recognized commands: syrd432, verbs.\n")
          and GOLDEN["switchyard --help, through switchyard_main"]["printed"] == result("the help")
          and seams("switchyard --help, through switchyard_main") == ["report_installed_release_version", "switchyard_help_text"],
          "the help names every verb from the launcher's table when it runs, and switchyard_main prints it after the release notice")
    every, served = result("every verb in the table, not served"), result("every verb in the table, served over the bridge")
    check(every == {verb: [verb in privileged, False] for verb in commands} and served == {verb: [False, False] for verb in commands},
          "a privileged verb needs root unless the bridge serves it; no verb's own help does")
    check(not any(result(f"root: {k}") for k in ("no arguments", "--help", "-h", "help", "--version", "version", "an unprivileged verb",
                                                 "an unknown verb", "a bare project name", "a bare project name with a space",
                                                 "a privileged verb's own -h", "a privileged verb's own --help"))
          and seams("root: an unprivileged verb the bridge would serve") == [],
          "help, version, unprivileged and unknown verbs and bare project names never cross, and never ask the bridge")
    check(result("root: a verb in capitals") is True and result("root: a verb in capitals, served") is False
          and result("root: help as a verb's argument is not a help flag") is True
          and GOLDEN["root: a privileged verb, served over the bridge"]["calls"] == [["_tenant_control_can_serve", [["stop", "p432"]], {}]],
          "the verb is case-folded; only -h and --help after it are help; the bridge is asked with the whole argv")
    check(all(result(f"root: {w} never crosses, even were it a privileged verb") is False and not GOLDEN[f"root: {w} never crosses, even were it a privileged verb"]["calls"]
              for w in ("-h", "--help", "help", "--version", "version")),
          "the help and version words answer before the verb tables are consulted, and never ask the bridge")
    check(result("root: the privileged set rebound on the launcher") is True and result("root: the privileged set rebound empty") is False,
          "the privileged set is the launcher's when the check runs")
    check([GOLDEN[f"the wrapper's question, {k}"]["printed"] for k in ("a privileged verb", "served over the bridge", "an unprivileged verb", "nothing asked")]
          == ["requires-root\n", "no-root\n", "no-root\n", "no-root\n"],
          "the installed wrapper's question is answered through the launcher's name, one word per answer")


def test_every_launcher_seam_is_reached() -> None:
    # Both functions are recorders on the launcher (switchyard_main reaches them there), and so is the tenant-control
    # check; the two verb tables they read are rebound there by their own cases, and change the answer.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name))}
    check((names - constants) | set(FUNCTIONS) <= REACHED, f"a recorder on the launcher reached every function: missing {sorted((names - constants) | set(FUNCTIONS) - REACHED)}")
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(constants == rebound == {"SWITCHYARD_COMMANDS", "SWITCHYARD_PRIVILEGED_COMMANDS"}, f"and every table rebound: {sorted(constants)} {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_five_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"switchyard_commands_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
