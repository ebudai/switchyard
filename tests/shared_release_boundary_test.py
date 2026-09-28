#!/usr/bin/env python3
"""SYRD-416: the shared-release identity, against the launcher it came out of.

The four constants, the release record and the eight functions that name the
shared install, read a release's marker, map a path or the running launcher to
an installed release and report the version moved unchanged into
`scripts/shared_release.py`, and the launcher re-exports all thirteen. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module; the class is the launcher's
  very class, frozen as before; the definition-time default `runner` is
  `subprocess.run`, and the annotations resolve to the same types.
- **Seams (rule 24):** every name the eight read -- each other, the constants,
  the class, the launcher's name, repository root, path check and its own
  `__file__` -- is read through the launcher as often as before, so a patch
  there reaches it: cases below rebind each on the launcher and see the effect.
- **Readers:** the fourteen production modules still read them through the
  launcher, and the launcher's own seven callers read them as launcher globals,
  exactly as often as before.
- **The behaviour is the baseline's:** marker parsing (absent, unreadable,
  malformed, not an object, not UTF-8, without a commit, a commit), the walk
  from a path to its release (nearest marker, stop at the root, the bare
  `current`/`releases` fallback, symlinks resolved, explicit/environment/default
  roots), the paths, the version text, the running launcher's own checkout, and
  the version notice's order, default stream and nonfatal failure. `GOLDEN`
  below was produced by running the BASELINE launcher's own functions over the
  very cases embedded here (`gold416.py`), not typed; it is byte-identical
  whether generated under `env -i` or in a normal role pane.

Every case builds its own test-owned tree and the shared root is always inside
it: the real /opt/switchyard is never read. The version notice is a recorder.
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import shared_release as m  # noqa: E402

CHECKS = 0
MOVED = ('SWITCHYARD_NAME', 'DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT', 'SWITCHYARD_RELEASE_MARKER_NAME', 'SWITCHYARD_VERSION', 'SharedSwitchyardRelease', 'switchyard_shared_install_root', 'switchyard_shared_target', 'switchyard_shared_pane_launcher', '_read_switchyard_release_marker', 'shared_switchyard_release_for_path', 'switchyard_version_text', 'running_launcher_release', 'report_installed_release_version')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'switchyard_shared_install_root': {'DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT': 1},
    'switchyard_shared_target': {'SWITCHYARD_NAME': 1, 'switchyard_shared_install_root': 1},
    'switchyard_shared_pane_launcher': {'TEAM_LAUNCHER_NAME': 1, 'switchyard_shared_install_root': 1},
    '_read_switchyard_release_marker': {'SWITCHYARD_RELEASE_MARKER_NAME': 1, 'SharedSwitchyardRelease': 3},
    'shared_switchyard_release_for_path': {'SharedSwitchyardRelease': 1, '_path_is_under': 4, '_read_switchyard_release_marker': 1, 'switchyard_shared_install_root': 1},
    'switchyard_version_text': {'SWITCHYARD_VERSION': 1, '_read_switchyard_release_marker': 1, '_repo_root': 1},
    'running_launcher_release': {'__file__': 1, 'shared_switchyard_release_for_path': 1},
    'report_installed_release_version': {'_repo_root': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the thirteen that names one of them, and how often.
DISPATCH = {'_new_project_launcher_config_payload': {'switchyard_shared_pane_launcher': 1}, '_precheck_deploy_source': {'SWITCHYARD_RELEASE_MARKER_NAME': 1}, '_rollout_recorder_path': {'switchyard_shared_install_root': 1}, '_switchyard_release_source_error': {'SWITCHYARD_RELEASE_MARKER_NAME': 3, '_read_switchyard_release_marker': 1, 'shared_switchyard_release_for_path': 1}, '_tenant_board_root_from_config': {'shared_switchyard_release_for_path': 1}, 'switchyard_main': {'report_installed_release_version': 1, 'switchyard_version_text': 1}, 'upgrade_generated_project_config': {'switchyard_shared_pane_launcher': 1}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/agent_cli_promotion.py': ['launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root'], 'scripts/launcher_checkout.py': ['launcher.shared_switchyard_release_for_path', 'launcher.shared_switchyard_release_for_path'], 'scripts/project_onboarding.py': ['launcher._read_switchyard_release_marker', 'launcher._read_switchyard_release_marker', 'launcher.shared_switchyard_release_for_path', 'launcher.shared_switchyard_release_for_path'], 'scripts/project_status.py': ['launcher.shared_switchyard_release_for_path'], 'scripts/release_alignment.py': ['launcher._read_switchyard_release_marker', 'launcher.switchyard_shared_install_root'], 'scripts/release_rollback.py': ['launcher.SWITCHYARD_RELEASE_MARKER_NAME', 'launcher.shared_switchyard_release_for_path', 'launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root'], 'scripts/role_account_migration.py': ['launcher.switchyard_shared_install_root'], 'scripts/root_plan_reconstruction.py': ['launcher.switchyard_shared_install_root'], 'scripts/staged_launch_checks.py': ['launcher.SWITCHYARD_RELEASE_MARKER_NAME', 'launcher._read_switchyard_release_marker', 'launcher._read_switchyard_release_marker', 'launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root'], 'scripts/tenant_control_helper.py': ['launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root'], 'scripts/tenant_release_report.py': ['launcher.SWITCHYARD_RELEASE_MARKER_NAME', 'launcher.switchyard_shared_install_root'], 'scripts/tenant_release_target.py': ['launcher.shared_switchyard_release_for_path', 'launcher.shared_switchyard_release_for_path'], 'scripts/ticket_board/publication_boundary.py': ['SWITCHYARD_RELEASE_MARKER_NAME', 'SWITCHYARD_RELEASE_MARKER_NAME', 'SWITCHYARD_RELEASE_MARKER_NAME'], 'scripts/trusted_bootstrap.py': ['launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT', 'launcher.running_launcher_release', 'launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root'], 'scripts/trusted_upgrade_release.py': ['launcher.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT', 'launcher.running_launcher_release', 'launcher.switchyard_shared_install_root', 'launcher.switchyard_shared_install_root']}
#: The BASELINE's own behaviour for the cases below (`gold416.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'marker: absent': {'answer': None, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a valid commit, stripped': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a non-string commit': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '1234', 'marker_error': ''}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a null commit': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '', 'marker_error': 'TMP/d/.switchyard-release.json has no commit'}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a blank commit': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '', 'marker_error': 'TMP/d/.switchyard-release.json has no commit'}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: no commit key': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '', 'marker_error': 'TMP/d/.switchyard-release.json has no commit'}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: empty file': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '', 'marker_error': 'Expecting value: line 1 column 1 (char 0)'}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: malformed JSON': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '', 'marker_error': 'Expecting value: line 1 column 12 (char 11)'}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a JSON list': {'answer': {'raised': 'AttributeError', 'message': "'list' object has no attribute 'get'"}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: JSON null': {'answer': {'raised': 'AttributeError', 'message': "'NoneType' object has no attribute 'get'"}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: not UTF-8': {'answer': {'raised': 'UnicodeDecodeError', 'message': "'utf-8' codec can't decode byte 0xff in position 12: invalid start byte"}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a directory in its place': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '', 'marker_error': "[Errno 21] Is a directory: 'TMP/d/.switchyard-release.json'"}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: a dangling symlink in its place': {'answer': None, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'marker: the marker name rebound on the launcher': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/d', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: outside the root': {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/elsewhere/v.txt', 'PATH TMP/opt'], {}, False]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: the root itself': {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt/current'], {}, False], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt/releases'], {}, False]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a marked release, a file inside': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts/x.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a marked release, its directory': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r1', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r1', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: current, unmarked': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/current/scripts/y.py', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/current/scripts/y.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/current/scripts/y.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current/scripts/y.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/current/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current/scripts'], {}, None], ['_path_is_under', ['PATH TMP/opt/current', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/current/scripts/y.py', 'PATH TMP/opt/current'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: current itself': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/current', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/current', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/current', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/current', 'PATH TMP/opt/current'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: an unmarked release': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r2/scripts/z.py', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r2/scripts/z.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r2/scripts/z.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r2/scripts/z.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r2/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r2/scripts'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r2', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r2'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/releases/r2/scripts/z.py', 'PATH TMP/opt/current'], {}, False], ['_path_is_under', ['PATH TMP/opt/releases/r2/scripts/z.py', 'PATH TMP/opt/releases'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: releases itself': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/releases', 'PATH TMP/opt/current'], {}, False], ['_path_is_under', ['PATH TMP/opt/releases', 'PATH TMP/opt/releases'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: under the root, elsewhere': {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/other/w.txt'], {}, None], ['_path_is_under', ['PATH TMP/opt/other', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/other'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt/current'], {}, False], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt/releases'], {}, False]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a path that does not exist, under current': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/current/missing/deeper', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/current/missing/deeper', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/current/missing/deeper', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current/missing/deeper'], {}, None], ['_path_is_under', ['PATH TMP/opt/current/missing', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current/missing'], {}, None], ['_path_is_under', ['PATH TMP/opt/current', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/current/missing/deeper', 'PATH TMP/opt/current'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: the nearer marker wins': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1/scripts', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts/x.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1/scripts', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a malformed marker stops the walk': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1/scripts', 'marker_commit': '', 'marker_error': 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts/x.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1/scripts', 'marker_commit': '', 'marker_error': 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a marker at the root counts': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/other/w.txt'], {}, None], ['_path_is_under', ['PATH TMP/opt/other', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/other'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a marker above the root does not': {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/other/w.txt'], {}, None], ['_path_is_under', ['PATH TMP/opt/other', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/other'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt/current'], {}, False], ['_path_is_under', ['PATH TMP/opt/other/w.txt', 'PATH TMP/opt/releases'], {}, False]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a symlink from outside into a release': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts/x.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts/x.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a symlink inside pointing outside': {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/elsewhere/v.txt', 'PATH TMP/opt'], {}, False]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: an explicit install root': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/alt/releases/r9', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}, 'calls': [['_path_is_under', ['PATH TMP/alt/releases/r9', 'PATH TMP/alt'], {}, True], ['_path_is_under', ['PATH TMP/alt/releases/r9', 'PATH TMP/alt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/alt/releases/r9'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/alt/releases/r9', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    "for_path: an explicit install root, the environment's release outside it": {'answer': None, 'calls': [['_path_is_under', ['PATH TMP/opt/releases/r1', 'PATH TMP/alt'], {}, False]], 'notices': [], 'printed': [], 'stderr': ''},
    "for_path: the environment's root with ~": {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/home/opt2/current/a', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/home/opt2'], ['_path_is_under', ['PATH TMP/home/opt2/current/a', 'PATH TMP/home/opt2'], {}, True], ['_path_is_under', ['PATH TMP/home/opt2/current/a', 'PATH TMP/home/opt2'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/home/opt2/current/a'], {}, None], ['_path_is_under', ['PATH TMP/home/opt2/current', 'PATH TMP/home/opt2'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/home/opt2/current'], {}, None], ['_path_is_under', ['PATH TMP/home/opt2', 'PATH TMP/home/opt2'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/home/opt2'], {}, None], ['_path_is_under', ['PATH TMP/home', 'PATH TMP/home/opt2'], {}, False], ['_path_is_under', ['PATH TMP/home/opt2/current/a', 'PATH TMP/home/opt2/current'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    "for_path: no environment: the launcher's default, rebound into the tree": {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/deflt/releases/r3', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/deflt'], ['_path_is_under', ['PATH TMP/deflt/releases/r3', 'PATH TMP/deflt'], {}, True], ['_path_is_under', ['PATH TMP/deflt/releases/r3', 'PATH TMP/deflt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/deflt/releases/r3'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/deflt/releases/r3', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: the install root rebound on the launcher': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/alt/current/q', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/alt'], ['_path_is_under', ['PATH TMP/alt/current/q', 'PATH TMP/alt'], {}, True], ['_path_is_under', ['PATH TMP/alt/current/q', 'PATH TMP/alt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/alt/current/q'], {}, None], ['_path_is_under', ['PATH TMP/alt/current', 'PATH TMP/alt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/alt/current'], {}, None], ['_path_is_under', ['PATH TMP/alt', 'PATH TMP/alt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/alt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/alt'], {}, False], ['_path_is_under', ['PATH TMP/alt/current/q', 'PATH TMP/alt/current'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'for_path: a relative path': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/current/scripts/y.py', 'marker_commit': '', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/current/scripts/y.py', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/current/scripts/y.py', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current/scripts/y.py'], {}, None], ['_path_is_under', ['PATH TMP/opt/current/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current/scripts'], {}, None], ['_path_is_under', ['PATH TMP/opt/current', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/current'], {}, None], ['_path_is_under', ['PATH TMP/opt', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt'], {}, None], ['_path_is_under', ['PATH TMP', 'PATH TMP/opt'], {}, False], ['_path_is_under', ['PATH TMP/opt/current/scripts/y.py', 'PATH TMP/opt/current'], {}, True]], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: install root: the environment': {'answer': 'PATH TMP/opt', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: install root: the environment with ~': {'answer': 'PATH TMP/home/opt2', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: install root: an empty environment': {'answer': 'PATH .', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: install root: no environment, the real default': {'answer': 'PATH /opt/switchyard', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: install root: the default rebound on the launcher': {'answer': 'PATH TMP/deflt', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: target: default root': {'answer': 'PATH TMP/opt/current/switchyard', 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt']], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: target: a root': {'answer': 'PATH TMP/alt/current/switchyard', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: target: the name rebound': {'answer': 'PATH TMP/opt/current/syrd416-name', 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt']], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: target: the install root rebound': {'answer': 'PATH TMP/alt/current/switchyard', 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/alt']], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: pane launcher: default root': {'answer': 'PATH TMP/opt/current/scripts/team-launcher', 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt']], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: pane launcher: a root': {'answer': 'PATH TMP/alt/current/scripts/team-launcher', 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'paths: pane launcher: the name rebound': {'answer': 'PATH TMP/opt/current/scripts/syrd416-launcher', 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt']], 'notices': [], 'printed': [], 'stderr': ''},
    'version: a repo with a commit': {'answer': 'switchyard 1111111111111111111111111111111111111111', 'calls': [['_read_switchyard_release_marker', ['PATH TMP/repo'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/repo', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'version: a repo without a marker': {'answer': 'switchyard dev', 'calls': [['_read_switchyard_release_marker', ['PATH TMP/repo'], {}, None]], 'notices': [], 'printed': [], 'stderr': ''},
    'version: a repo whose marker has no commit': {'answer': 'switchyard dev', 'calls': [['_read_switchyard_release_marker', ['PATH TMP/repo'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/repo', 'marker_commit': '', 'marker_error': 'TMP/repo/.switchyard-release.json has no commit'}]], 'notices': [], 'printed': [], 'stderr': ''},
    'version: a repo whose marker is malformed': {'answer': 'switchyard dev', 'calls': [['_read_switchyard_release_marker', ['PATH TMP/repo'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/repo', 'marker_commit': '', 'marker_error': 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}]], 'notices': [], 'printed': [], 'stderr': ''},
    "version: the launcher's repo, marked": {'answer': 'switchyard 2222222222222222222222222222222222222222', 'calls': [['_repo_root', [], {}, 'PATH TMP/launcher-repo'], ['_read_switchyard_release_marker', ['PATH TMP/launcher-repo'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/launcher-repo', 'marker_commit': '2222222222222222222222222222222222222222', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    "version: the launcher's repo, unmarked": {'answer': 'switchyard dev', 'calls': [['_repo_root', [], {}, 'PATH TMP/launcher-repo'], ['_read_switchyard_release_marker', ['PATH TMP/launcher-repo'], {}, None]], 'notices': [], 'printed': [], 'stderr': ''},
    'version: the version rebound on the launcher': {'answer': 'switchyard syrd416-v', 'calls': [['_read_switchyard_release_marker', ['PATH TMP/repo'], {}, None]], 'notices': [], 'printed': [], 'stderr': ''},
    'running: a given root': {'answer': {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts', 'PATH TMP/opt'], {}, True], ['_path_is_under', ['PATH TMP/opt/releases/r1/scripts', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1/scripts'], {}, None], ['_path_is_under', ['PATH TMP/opt/releases/r1', 'PATH TMP/opt'], {}, True], ['_read_switchyard_release_marker', ['PATH TMP/opt/releases/r1'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}], ['shared_switchyard_release_for_path', ['PATH TMP/opt/releases/r1/scripts'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/opt/releases/r1', 'marker_commit': '1111111111111111111111111111111111111111', 'marker_error': ''}]], 'notices': [], 'printed': [], 'stderr': ''},
    'running: a given root outside': {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH TMP/elsewhere', 'PATH TMP/opt'], {}, False], ['shared_switchyard_release_for_path', ['PATH TMP/elsewhere'], {}, None]], 'notices': [], 'printed': [], 'stderr': ''},
    "running: the launcher's own checkout (release lookup recorded)": {'answer': 'SYRD416 LOOKUP', 'calls': [['shared_switchyard_release_for_path', ['PATH REPO'], {}, 'SYRD416 LOOKUP']], 'notices': [], 'printed': [], 'stderr': ''},
    "running: the launcher's own checkout, outside the shared root": {'answer': None, 'calls': [['switchyard_shared_install_root', [], {}, 'PATH TMP/opt'], ['_path_is_under', ['PATH REPO', 'PATH TMP/opt'], {}, False], ['shared_switchyard_release_for_path', ['PATH REPO'], {}, None]], 'notices': [], 'printed': [], 'stderr': ''},
    'report: two lines, an injected print': {'answer': ['one', 'two'], 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {'SYRD416': '1'}, 'INJECTED']], 'printed': ['one', 'two'], 'stderr': ''},
    'report: two lines, the default print': {'answer': ['one', 'two'], 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'INJECTED']], 'printed': [], 'stderr': 'one\ntwo\n'},
    'report: no lines': {'answer': [], 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'INJECTED']], 'printed': [], 'stderr': ''},
    "report: the process environment and the launcher's repo": {'answer': ['x'], 'calls': [['_repo_root', [], {}, 'PATH TMP/launcher-repo']], 'notices': [['release_notice_lines', 'PATH TMP/launcher-repo', 'THE PROCESS ENVIRONMENT', 'INJECTED']], 'printed': ['x'], 'stderr': ''},
    'report: the default runner': {'answer': ['x'], 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'subprocess.run']], 'printed': ['x'], 'stderr': ''},
    'report: the notice raises': {'answer': [], 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'INJECTED']], 'printed': [], 'stderr': ''},
    'report: the notice raises OSError': {'answer': [], 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'INJECTED']], 'printed': [], 'stderr': ''},
    'report: the notice exits': {'answer': {'raised': 'SystemExit', 'message': 'syrd416 notice'}, 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'INJECTED']], 'printed': [], 'stderr': ''},
    'report: the print raises': {'answer': {'raised': 'RuntimeError', 'message': 'syrd416 print'}, 'calls': [], 'notices': [['release_notice_lines', 'PATH TMP/repo', {}, 'INJECTED']], 'printed': [], 'stderr': ''},
    'class: active with a commit': {'answer': True, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'class: inactive without one': {'answer': False, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'class: a bare release is not active': {'answer': False, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'class: undeterminable reads a field it does not have': {'answer': {'raised': 'AttributeError', 'message': "'SharedSwitchyardRelease' object has no attribute 'error'"}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'class: frozen': {'answer': {'raised': 'FrozenInstanceError', 'message': "cannot assign to field 'marker_commit'"}, 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'class: equal by value': {'answer': [True, False, True], 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
    'class: its repr': {'answer': "SharedSwitchyardRelease(root=PosixPath('TMP/r'), marker_commit='1111111111111111111111111111111111111111', marker_error='e')", 'calls': [], 'notices': [], 'printed': [], 'stderr': ''},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold416.py` (which ran them on the baseline) ------------------------------------
# A case lays out one test-owned tree -- files, directories and symlinks under TMP -- and calls one of the shared-release
# functions with the shared install root always inside it (through SWITCHYARD_SHARED_INSTALL_ROOT, an explicit root, or
# the launcher's own default rebound into the tree): the real /opt/switchyard is never read. Every launcher name the
# functions read may be rebound on the launcher for a case, the way the suites that patch them do; the version notice
# is a recorder; the launcher's repository root answers inside the tree.
_REAL_RUN = subprocess.run
C1, C2 = "1" * 40, "2" * 40
REL = {"opt/releases/r1/.switchyard-release.json": '{"commit": "%s"}' % C1, "opt/releases/r1/scripts/x.py": "", "opt/current/scripts/y.py": "",
       "opt/releases/r2/scripts/z.py": "", "opt/other/w.txt": "", "elsewhere/v.txt": ""}
MARKER = {
    "absent": {"call": "marker", "tree": {"d/x": ""}, "path": "d"},
    "a valid commit, stripped": {"call": "marker", "tree": {"d/.switchyard-release.json": '{"commit": "  %s  "}' % C1}, "path": "d"},
    "a non-string commit": {"call": "marker", "tree": {"d/.switchyard-release.json": '{"commit": 1234}'}, "path": "d"},
    "a null commit": {"call": "marker", "tree": {"d/.switchyard-release.json": '{"commit": null}'}, "path": "d"},
    "a blank commit": {"call": "marker", "tree": {"d/.switchyard-release.json": '{"commit": "   "}'}, "path": "d"},
    "no commit key": {"call": "marker", "tree": {"d/.switchyard-release.json": '{"source_ref": "x"}'}, "path": "d"},
    "empty file": {"call": "marker", "tree": {"d/.switchyard-release.json": ""}, "path": "d"},
    "malformed JSON": {"call": "marker", "tree": {"d/.switchyard-release.json": '{"commit": '}, "path": "d"},
    "a JSON list": {"call": "marker", "tree": {"d/.switchyard-release.json": '["commit"]'}, "path": "d"},
    "JSON null": {"call": "marker", "tree": {"d/.switchyard-release.json": "null"}, "path": "d"},
    "not UTF-8": {"call": "marker", "tree": {"d/.switchyard-release.json": b'{"commit": "\xff"}'}, "path": "d"},
    "a directory in its place": {"call": "marker", "tree": {"d/.switchyard-release.json/inner": ""}, "path": "d"},
    "a dangling symlink in its place": {"call": "marker", "tree": {"d/.switchyard-release.json": ("link", "nowhere")}, "path": "d"},
    "the marker name rebound on the launcher": {"call": "marker", "tree": {"d/.syrd416-marker.json": '{"commit": "%s"}' % C2, "d/.switchyard-release.json": '{"commit": "%s"}' % C1},
                                                "path": "d", "launcher": {"SWITCHYARD_RELEASE_MARKER_NAME": ".syrd416-marker.json"}},
}
FOR_PATH = {
    "outside the root": {"call": "for_path", "tree": REL, "path": "elsewhere/v.txt"},
    "the root itself": {"call": "for_path", "tree": REL, "path": "opt"},
    "a marked release, a file inside": {"call": "for_path", "tree": REL, "path": "opt/releases/r1/scripts/x.py"},
    "a marked release, its directory": {"call": "for_path", "tree": REL, "path": "opt/releases/r1"},
    "current, unmarked": {"call": "for_path", "tree": REL, "path": "opt/current/scripts/y.py"},
    "current itself": {"call": "for_path", "tree": REL, "path": "opt/current"},
    "an unmarked release": {"call": "for_path", "tree": REL, "path": "opt/releases/r2/scripts/z.py"},
    "releases itself": {"call": "for_path", "tree": REL, "path": "opt/releases"},
    "under the root, elsewhere": {"call": "for_path", "tree": REL, "path": "opt/other/w.txt"},
    "a path that does not exist, under current": {"call": "for_path", "tree": REL, "path": "opt/current/missing/deeper"},
    "the nearer marker wins": {"call": "for_path", "tree": {**REL, "opt/releases/r1/scripts/.switchyard-release.json": '{"commit": "%s"}' % C2}, "path": "opt/releases/r1/scripts/x.py"},
    "a malformed marker stops the walk": {"call": "for_path", "tree": {**REL, "opt/releases/r1/scripts/.switchyard-release.json": "{"}, "path": "opt/releases/r1/scripts/x.py"},
    "a marker at the root counts": {"call": "for_path", "tree": {**REL, "opt/.switchyard-release.json": '{"commit": "%s"}' % C2}, "path": "opt/other/w.txt"},
    "a marker above the root does not": {"call": "for_path", "tree": {**REL, ".switchyard-release.json": '{"commit": "%s"}' % C2}, "path": "opt/other/w.txt"},
    "a symlink from outside into a release": {"call": "for_path", "tree": {**REL, "elsewhere/in": ("link", "../opt/releases/r1/scripts")}, "path": "elsewhere/in/x.py"},
    "a symlink inside pointing outside": {"call": "for_path", "tree": {**REL, "opt/current/out": ("link", "../../elsewhere")}, "path": "opt/current/out/v.txt"},
    "an explicit install root": {"call": "for_path", "tree": {**REL, "alt/releases/r9/.switchyard-release.json": '{"commit": "%s"}' % C2}, "path": "alt/releases/r9", "install_root": "alt"},
    "an explicit install root, the environment's release outside it": {"call": "for_path", "tree": REL, "path": "opt/releases/r1", "install_root": "alt"},
    "the environment's root with ~": {"call": "for_path", "tree": {"home/opt2/current/a": ""}, "path": "home/opt2/current/a", "env": "~/opt2"},
    "no environment: the launcher's default, rebound into the tree": {"call": "for_path", "tree": {"deflt/releases/r3/.switchyard-release.json": '{"commit": "%s"}' % C2},
                                                                     "path": "deflt/releases/r3", "env": None, "launcher": {"DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT": "deflt"}},
    "the install root rebound on the launcher": {"call": "for_path", "tree": {**REL, "alt/current/q": ""}, "path": "alt/current/q",
                                                 "launcher": {"switchyard_shared_install_root": "alt"}},
    "a relative path": {"call": "for_path", "tree": REL, "path": "opt/current/scripts/y.py", "relative": True},
}
PATHS = {
    "install root: the environment": {"call": "install_root", "env": "{TMP}/opt"},
    "install root: the environment with ~": {"call": "install_root", "env": "~/opt2"},
    "install root: an empty environment": {"call": "install_root", "env": ""},
    "install root: no environment, the real default": {"call": "install_root", "env": None},
    "install root: the default rebound on the launcher": {"call": "install_root", "env": None, "launcher": {"DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT": "deflt"}},
    "target: default root": {"call": "target"},
    "target: a root": {"call": "target", "root": "alt"},
    "target: the name rebound": {"call": "target", "launcher": {"SWITCHYARD_NAME": "syrd416-name"}},
    "target: the install root rebound": {"call": "target", "launcher": {"switchyard_shared_install_root": "alt"}},
    "pane launcher: default root": {"call": "pane_launcher"},
    "pane launcher: a root": {"call": "pane_launcher", "root": "alt"},
    "pane launcher: the name rebound": {"call": "pane_launcher", "launcher": {"TEAM_LAUNCHER_NAME": "syrd416-launcher"}},
}
VERSION = {
    "a repo with a commit": {"call": "version", "tree": {"repo/.switchyard-release.json": '{"commit": "%s"}' % C1}, "repo_root": "repo"},
    "a repo without a marker": {"call": "version", "tree": {"repo/x": ""}, "repo_root": "repo"},
    "a repo whose marker has no commit": {"call": "version", "tree": {"repo/.switchyard-release.json": "{}"}, "repo_root": "repo"},
    "a repo whose marker is malformed": {"call": "version", "tree": {"repo/.switchyard-release.json": "{"}, "repo_root": "repo"},
    "the launcher's repo, marked": {"call": "version", "tree": {"launcher-repo/.switchyard-release.json": '{"commit": "%s"}' % C2}},
    "the launcher's repo, unmarked": {"call": "version", "tree": {"launcher-repo/x": ""}},
    "the version rebound on the launcher": {"call": "version", "tree": {"repo/x": ""}, "repo_root": "repo", "launcher": {"SWITCHYARD_VERSION": "syrd416-v"}},
}
RUNNING = {
    "a given root": {"call": "running", "tree": REL, "root": "opt/releases/r1/scripts"},
    "a given root outside": {"call": "running", "tree": REL, "root": "elsewhere"},
    "the launcher's own checkout (release lookup recorded)": {"call": "running", "tree": REL, "stand_in_lookup": True},
    "the launcher's own checkout, outside the shared root": {"call": "running", "tree": REL},
}
REPORT = {
    "two lines, an injected print": {"call": "report", "lines": ["one", "two"], "print": True, "runner": True, "environ": {"SYRD416": "1"}, "root": "repo"},
    "two lines, the default print": {"call": "report", "lines": ["one", "two"], "runner": True, "environ": {}, "root": "repo"},
    "no lines": {"call": "report", "lines": [], "print": True, "runner": True, "environ": {}, "root": "repo"},
    "the process environment and the launcher's repo": {"call": "report", "lines": ["x"], "print": True, "runner": True},
    "the default runner": {"call": "report", "lines": ["x"], "print": True, "environ": {}, "root": "repo"},
    "the notice raises": {"call": "report", "raises": "RuntimeError", "print": True, "runner": True, "environ": {}, "root": "repo"},
    "the notice raises OSError": {"call": "report", "raises": "OSError", "print": True, "runner": True, "environ": {}, "root": "repo"},
    "the notice exits": {"call": "report", "raises": "SystemExit", "print": True, "runner": True, "environ": {}, "root": "repo"},
    "the print raises": {"call": "report", "lines": ["one", "two"], "print": "raises", "runner": True, "environ": {}, "root": "repo"},
}
CLASS = {
    "active with a commit": {"call": "class", "fields": {"marker_commit": C1}, "attr": "active"},
    "inactive without one": {"call": "class", "fields": {"marker_error": "e"}, "attr": "active"},
    "a bare release is not active": {"call": "class", "fields": {}, "attr": "active"},
    "undeterminable reads a field it does not have": {"call": "class", "fields": {"marker_error": "e"}, "attr": "undeterminable"},
    "frozen": {"call": "class", "fields": {}, "attr": "set"},
    "equal by value": {"call": "class", "fields": {"marker_commit": C1}, "attr": "eq"},
    "its repr": {"call": "class", "fields": {"marker_commit": C1, "marker_error": "e"}, "attr": "repr"},
}
CASES = {f"{group}: {label}": spec for group, table in (("marker", MARKER), ("for_path", FOR_PATH), ("paths", PATHS), ("version", VERSION),
                                                       ("running", RUNNING), ("report", REPORT), ("class", CLASS)) for label, spec in table.items()}
FUNCTIONS = ("switchyard_shared_install_root", "switchyard_shared_target", "switchyard_shared_pane_launcher", "_read_switchyard_release_marker",
             "shared_switchyard_release_for_path", "switchyard_version_text", "running_launcher_release", "report_installed_release_version")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s function, in a fresh test-owned tree; the launcher's seams are recorded (or rebound) on `t`."""
    import dataclasses, io, shutil, tempfile
    from scripts import version_notice
    calls: list = []
    tmp = Path(tempfile.mkdtemp(prefix="syrd416-")).resolve()
    repo = Path(t.__file__).resolve().parent.parent

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"dataclass": type(value).__name__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if isinstance(value, Path):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP").replace(str(repo), "REPO")
        return value

    def record(name, real):
        def call(*args, **kwargs):
            reached.add(name)
            try:
                answer = real(*args, **kwargs)
            except Exception as exc:
                calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items()))), {"raised": type(exc).__name__}])
                raise
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items()))), norm(answer)])
            return answer
        return call

    saved_env = {k: os.environ.get(k) for k in ("SWITCHYARD_SHARED_INSTALL_ROOT", "HOME")}
    saved_cwd = os.getcwd()
    saved_stderr = sys.stderr
    names = [*FUNCTIONS, "_path_is_under", "_repo_root", "SWITCHYARD_NAME", "TEAM_LAUNCHER_NAME", "SWITCHYARD_VERSION", "SWITCHYARD_RELEASE_MARKER_NAME",
             "DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT"]
    saved = {n: getattr(t, n) for n in names}
    saved_notice = version_notice.release_notice_lines
    try:
        for rel, content in spec.get("tree", {}).items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, tuple):
                os.symlink(content[1], p)
            elif isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        real = dict(saved)
        on_launcher = {n: record(n, real[n]) for n in FUNCTIONS}
        on_launcher["_path_is_under"] = record("_path_is_under", real["_path_is_under"])
        on_launcher["_repo_root"] = record("_repo_root", lambda: tmp / "launcher-repo")
        if spec.get("stand_in_lookup"):
            on_launcher["shared_switchyard_release_for_path"] = record("shared_switchyard_release_for_path", lambda path, **k: "SYRD416 LOOKUP")
        for n, v in spec.get("launcher", {}).items():
            if n == "switchyard_shared_install_root":
                on_launcher[n] = record(n, lambda v=v: tmp / v)
            elif n == "DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT":
                on_launcher[n] = tmp / v
            else:
                on_launcher[n] = v
        for n, f in on_launcher.items():
            setattr(t, n, f)
        # The shared root is always inside the tree unless the case names it: the real /opt/switchyard is never read.
        env = spec.get("env", "{TMP}/opt")
        os.environ["HOME"] = str(tmp / "home")
        if env is None:
            os.environ.pop("SWITCHYARD_SHARED_INSTALL_ROOT", None)
        else:
            os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = env.replace("{TMP}", str(tmp))
        notices = []

        def notice(root, *, environ, runner):
            reached.add("release_notice_lines")
            notices.append(["release_notice_lines", norm(root), "THE PROCESS ENVIRONMENT" if environ == dict(os.environ) and "SYRD416" not in environ else norm(environ),
                            "subprocess.run" if runner is _REAL_RUN else "INJECTED" if runner is injected_runner else "ANOTHER"])
            if spec.get("raises"):
                raise {"RuntimeError": RuntimeError, "OSError": OSError, "SystemExit": SystemExit}[spec["raises"]]("syrd416 notice")
            return list(spec.get("lines", []))

        def injected_runner(*a, **k):
            raise AssertionError("the notice runner is recorded, never run")

        version_notice.release_notice_lines = notice
        printed = []

        def printer(line):
            if spec.get("print") == "raises":
                raise RuntimeError("syrd416 print")
            printed.append(line)

        call = spec["call"]
        captured = io.StringIO()
        try:
            if call == "marker":
                got = under["_read_switchyard_release_marker"](tmp / spec["path"])
            elif call == "for_path":
                kwargs = {"install_root": tmp / spec["install_root"]} if "install_root" in spec else {}
                if spec.get("relative"):
                    os.chdir(tmp)
                    got = under["shared_switchyard_release_for_path"](Path(spec["path"]), **kwargs)
                else:
                    got = under["shared_switchyard_release_for_path"](tmp / spec["path"], **kwargs)
            elif call == "install_root":
                got = under["switchyard_shared_install_root"]()
            elif call == "target":
                got = under["switchyard_shared_target"](*([tmp / spec["root"]] if "root" in spec else []))
            elif call == "pane_launcher":
                got = under["switchyard_shared_pane_launcher"](*([tmp / spec["root"]] if "root" in spec else []))
            elif call == "version":
                got = under["switchyard_version_text"](*([tmp / spec["repo_root"]] if "repo_root" in spec else []))
            elif call == "running":
                got = under["running_launcher_release"](*([tmp / spec["root"]] if "root" in spec else []))
            elif call == "report":
                kwargs = {}
                if "root" in spec:
                    kwargs["root"] = tmp / spec["root"]
                if "environ" in spec:
                    kwargs["environ"] = dict(spec["environ"])
                if spec.get("runner"):
                    kwargs["runner"] = injected_runner
                if spec.get("print"):
                    kwargs["print_func"] = printer
                sys.stderr = captured
                try:
                    got = under["report_installed_release_version"](**kwargs)
                finally:
                    sys.stderr = saved_stderr
            else:
                cls = t.SharedSwitchyardRelease
                obj = cls(root=tmp / "r", **spec["fields"])
                attr = spec["attr"]
                if attr == "set":
                    obj.marker_commit = "x"
                    got = "ASSIGNED"
                elif attr == "eq":
                    got = [obj == cls(root=tmp / "r", **spec["fields"]), obj == cls(root=tmp / "other", **spec["fields"]), hash(obj) == hash(cls(root=tmp / "r", **spec["fields"]))]
                elif attr == "repr":
                    got = repr(obj)
                else:
                    got = getattr(obj, attr)
            answer = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            answer = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"answer": answer, "calls": calls, "notices": notices, "printed": printed, "stderr": norm(captured.getvalue())}
    finally:
        sys.stderr = saved_stderr
        os.chdir(saved_cwd)
        version_notice.release_notice_lines = saved_notice
        for n, f in saved.items():
            setattr(t, n, f)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp)
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


def test_the_module_loads_no_other_switchyard_module_at_import() -> None:
    result = python("import sys, scripts.shared_release as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own, loading no other Switchyard module and never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.shared_release", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.shared_release")):
        result = python("import importlib, inspect, subprocess, typing, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.shared_release as m; "
                        "p = inspect.signature(m.report_installed_release_version).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "p['runner'].default is subprocess.run and p['print_func'].default is None and p['root'].default is None and p['environ'].default is None "
                        "and m.SharedSwitchyardRelease.__dataclass_params__.frozen and [f.name for f in dataclasses.fields(m.SharedSwitchyardRelease)] == ['root', 'marker_commit', 'marker_error'] "
                        "and typing.get_type_hints(m.running_launcher_release)['return'] == typing.Optional[t.SharedSwitchyardRelease], "
                        "not hasattr(m, 'launcher') and not hasattr(m, '_repo_root') and not hasattr(m, 'TEAM_LAUNCHER_NAME') and not hasattr(m, '_path_is_under'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.sys is sys and m.subprocess is subprocess and m.json is json and m.Path is Path, "the standard-library names are the module's own")
    check(m.SWITCHYARD_NAME == "switchyard" and m.DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT == Path("/opt/switchyard")
          and m.SWITCHYARD_RELEASE_MARKER_NAME == ".switchyard-release.json" and m.SWITCHYARD_VERSION == "dev", "the four constants, unchanged")
    check(t.SharedSwitchyardRelease is m.SharedSwitchyardRelease and t.SharedSwitchyardRelease.__qualname__ == "SharedSwitchyardRelease"
          and sorted(k for k, v in vars(m.SharedSwitchyardRelease).items() if isinstance(v, property)) == ["active", "undeterminable"],
          "one class, the same properties")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "shared_release.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        nested = ["from scripts.version_notice import release_notice_lines"] if name == "report_installed_release_version" else []
        check(imports == ["from scripts import team_launcher as launcher", *nested] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported first thing, and the notice still imported when it runs: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import json", "import os", "import subprocess", "import sys", "from dataclasses import dataclass",
                  "from pathlib import Path", "from typing import Any, Callable"],
          f"the standard library only: {top}")
    names = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the thirteen in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_thirteen_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.shared_release"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the thirteen, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"SWITCHYARD_REGISTRY_AGENT_CLIS_KEY", "TEAM_LAUNCHER_NAME", "DEFAULT_PANE_BASE_PATH", "DEFAULT_SESSION_DIR",
                                        "MAX_VISIBLE_PANES_PER_WINDOW", "SWITCHYARD_COMMANDS", "ProjectDesignArtifact", "_repo_root", "_allocated_board_port",
                                        "role_control_accounts", "_project_config_path_owner_user", "switchyard_main", "_path_is_under"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    # A caller a later slice moved on is re-exported by the launcher, unaliased, from a scripts module, and reads them
    # there through the launcher, exactly as often (SYRD-417 moved upgrade_generated_project_config).
    sources = {a.name: n.module for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
               for a in n.names if a.asname is None}
    for caller in sorted(set(DISPATCH) - set(uses)):
        if caller not in sources:
            continue
        moved_to = ast.parse((ROOT / (sources[caller].replace(".", "/") + ".py")).read_text(encoding="utf-8"))
        node = next((n for n in moved_to.body if getattr(n, "name", None) == caller), None)
        for x in ast.walk(node) if node is not None else ():
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher" and x.attr in MOVED:
                uses.setdefault(caller, {}).setdefault(x.attr, 0)
                uses[caller][x.attr] += 1
    check(uses == DISPATCH, f"the launcher's own callers read them as launcher globals, exactly as often as before: {uses}")
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(loose == [], f"and nothing at module level reads them: {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads them as it did: {got}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def answer(label):
        return GOLDEN[label]["answer"]

    check(answer("marker: absent") is None and answer("marker: a blank commit")["marker_error"].endswith("has no commit")
          and answer("marker: a JSON list")["raised"] == "AttributeError" and answer("marker: not UTF-8")["raised"] == "UnicodeDecodeError",
          "absent is None, a blank commit is an error, and what the baseline raises it still raises")
    check(answer("for_path: outside the root") is None and answer("for_path: a marker above the root does not") is None
          and answer("for_path: the nearer marker wins")["marker_commit"] == C2 and answer("for_path: an unmarked release")["marker_commit"] == "",
          "the walk stops at the root, the nearest marker wins, and an unmarked release is bare")
    lookup = GOLDEN["running: the launcher's own checkout (release lookup recorded)"]["calls"]
    check(lookup == [["shared_switchyard_release_for_path", ["PATH REPO"], {}, "SYRD416 LOOKUP"]], f"the running launcher is the launcher's own checkout: {lookup}")
    check(answer("report: the notice raises") == [] and GOLDEN["report: the notice raises"]["printed"] == [] and answer("report: the notice exits")["raised"] == "SystemExit"
          and GOLDEN["report: two lines, the default print"]["stderr"] == "one\ntwo\n", "the notice never fails the command, and prints to stderr by default")
    check(answer("class: undeterminable reads a field it does not have")["raised"] == "AttributeError", "the odd property is kept as it was")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads if callable(getattr(t, name, None)) and not isinstance(getattr(t, name), type)}
    check(expected <= REACHED, f"a recorder on the launcher reached every seam: missing {sorted(expected - REACHED)}")


STRUCTURE = ("test_the_module_loads_no_other_switchyard_module_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_thirteen_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"shared_release_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
