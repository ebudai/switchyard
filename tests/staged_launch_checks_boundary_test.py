#!/usr/bin/env python3
"""SYRD-415: the staged-tooling launch checks, against the launcher they came out of.

The two markers and three functions that decide which release a tenant's staged
bundle names, whether the director may believe that marker, and which bundle
problems an ordinary launch may restage or must refuse moved unchanged into
`scripts/staged_launch_checks.py`, and the launcher re-exports all five. This
pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads only `scripts.staged_role_tooling`, where the one
  definition-time default -- the staged tooling owner -- comes from, and it is
  the very object the launcher bound.
- **Seams (rule 24):** every name the three read -- each other and the two
  markers included -- is read through the launcher as often as before, so a
  patch there reaches it: every case below runs with the launcher's seams
  recorded there.
- **Readers:** `command_crossing.py`, `director_upgrade.py` and
  `staged_role_tooling.py` still read them through the launcher, and no other
  launcher definition names them.
- **The pinned-release rules are the baseline's:** an explicit release, else
  the tenant's own installed pinned release, else `current`; the director's
  marker believed only after the root-controlled walk (from the test root, the
  environment's staging root, or "/"), then a 40-hex commit, then an installed
  release; absence restaged, hostile ownership/mode/type refused, drift left
  alone; every message and the return shapes. `GOLDEN` below was produced by
  running the BASELINE launcher's own functions over the very cases embedded
  here (`gold415.py`), not typed; it is byte-identical whether generated under
  `env -i` or in a normal role pane.

Every case builds its own test-owned tree; the release marker is read by the
launcher's own reader and the root-controlled walk is the real one inside that
tree. The host is never read: its staging directory is redirected into the tree
and a walk from "/" stands in, both recorded. Spawns, every exec, signals,
account and group lookups and socket connections are refused for each case.
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
from scripts import staged_launch_checks as m  # noqa: E402
from scripts import staged_role_tooling  # noqa: E402

CHECKS = 0
MOVED = ('STAGED_TOOLING_ABSENT_MARKER', 'STAGED_TOOLING_HOSTILE_MARKERS', 'tenant_pinned_release_root', 'director_readable_pinned_release', 'staged_bundle_launch_problems')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'tenant_pinned_release_root': {'_read_switchyard_release_marker': 1, 'role_tooling_staging_dir': 1, 'switchyard_shared_install_root': 1},
    'director_readable_pinned_release': {'SWITCHYARD_RELEASE_MARKER_NAME': 1, '_read_switchyard_release_marker': 1, 'role_tooling_staging_dir': 1, 'switchyard_shared_install_root': 1},
    'staged_bundle_launch_problems': {'STAGED_TOOLING_ABSENT_MARKER': 1, 'STAGED_TOOLING_HOSTILE_MARKERS': 1, 'role_tooling_staging_dir': 1, 'staged_role_tooling_problems': 1, 'switchyard_shared_install_root': 1, 'tenant_pinned_release_root': 1},
}
#: Measured on the baseline launcher: every launcher function outside the five that calls one of them.
DISPATCH = {}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/command_crossing.py': ['launcher.staged_bundle_launch_problems', 'launcher.staged_bundle_launch_problems'], 'scripts/director_upgrade.py': ['launcher.director_readable_pinned_release'], 'scripts/staged_role_tooling.py': ['launcher.staged_bundle_launch_problems']}
#: The BASELINE's own behaviour for the cases below (`gold415.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'pinned: an old pinned release': {'answer': 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'shape': 'PosixPath', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'pinned: a marker naming an uninstalled release': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'marker_error': ''}]]},
    'pinned: a marker naming a file, not a release': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'pinned: no marker': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None]]},
    'pinned: a marker with no commit': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': '', 'marker_error': 'TMP/staging/p415/.switchyard-release.json has no commit'}]]},
    'pinned: a marker with a blank commit': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': '', 'marker_error': 'TMP/staging/p415/.switchyard-release.json has no commit'}]]},
    'pinned: a marker that is not JSON': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': '', 'marker_error': 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}]]},
    'pinned: a non-hex name, installed': {'answer': 'PATH TMP/opt/releases/release-1', 'shape': 'PosixPath', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'release-1', 'marker_error': ''}]]},
    'pinned: an uppercase commit': {'answer': None, 'shape': 'NoneType', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', 'marker_error': ''}]]},
    'pinned: the shared install root by default': {'answer': 'PATH TMP/shared/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'shape': 'PosixPath', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['switchyard_shared_install_root', [], {}, 'PATH TMP/shared']]},
    "pinned: the host's staging root": {'answer': 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'shape': 'PosixPath', 'calls': [["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['_read_switchyard_release_marker', ['PATH TMP/host/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/host/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    "pinned: the environment's staging root": {'answer': 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'shape': 'PosixPath', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/envstaging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/envstaging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/envstaging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'pinned: an explicit root over the environment': {'answer': 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'shape': 'PosixPath', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'director: an old pinned release, a test-owned root': {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'director: an uppercase commit': {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', 'marker_error': ''}]]},
    'director: a commit with spaces around it': {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'director: a short commit': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json names 'abc1234', not a commit"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'abc1234', 'marker_error': ''}]]},
    'director: a 41-character commit': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json names 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', not a commit"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'director: a 40-character non-hex name': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json names 'gggggggggggggggggggggggggggggggggggggggg', not a commit"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'gggggggggggggggggggggggggggggggggggggggg', 'marker_error': ''}]]},
    'director: no marker': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json is not root's: cannot inspect TMP/staging/p415/.switchyard-release.json: [Errno 2] No such file or directory: 'TMP/staging/p415/.switchyard-release.json'"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', ["cannot inspect TMP/staging/p415/.switchyard-release.json: [Errno 2] No such file or directory: 'TMP/staging/p415/.switchyard-release.json'"]]]},
    'director: a marker with no commit': {'answer': [None, '', 'the staged release marker TMP/staging/p415/.switchyard-release.json names no release: TMP/staging/p415/.switchyard-release.json has no commit'], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': '', 'marker_error': 'TMP/staging/p415/.switchyard-release.json has no commit'}]]},
    'director: a marker that is not JSON': {'answer': [None, '', 'the staged release marker TMP/staging/p415/.switchyard-release.json names no release: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': '', 'marker_error': 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'}]]},
    'director: a group-writable staging root': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json is not root's: TMP/staging is group- or world-writable"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', ['TMP/staging is group- or world-writable']]]},
    'director: a world-writable marker': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json is not root's: TMP/staging/p415/.switchyard-release.json is group- or world-writable"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', ['TMP/staging/p415/.switchyard-release.json is group- or world-writable']]]},
    'director: a group-writable tenant directory': {'answer': [None, '', "the staged release marker TMP/staging/p415/.switchyard-release.json is not root's: TMP/staging/p415 is group- or world-writable"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', ['TMP/staging/p415 is group- or world-writable']]]},
    'director: a missing release': {'answer': [None, '', 'TMP/opt/releases/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb, the release TMP/staging/p415/.switchyard-release.json names, is not installed'], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'marker_error': ''}]]},
    'director: a release that is a file': {'answer': [None, '', 'TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa, the release TMP/staging/p415/.switchyard-release.json names, is not installed'], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'director: the shared install root by default': {'answer': ['PATH TMP/shared/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['switchyard_shared_install_root', [], {}, 'PATH TMP/shared']]},
    "director: the host, not root's": {'answer': [None, '', "the staged release marker TMP/host/p415/.switchyard-release.json is not root's: / is owned by uid 4150 rather than by uid 0; and a second"], 'shape': 'tuple[NoneType, str, str]', 'calls': [["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['root_controlled_problems', 'TMP/host/p415/.switchyard-release.json', 0, '/', 'stood in', ['/ is owned by uid 4150 rather than by uid 0', 'and a second']]]},
    "director: the host, root's, no marker": {'answer': [None, '', 'the staged release marker TMP/host/p415/.switchyard-release.json names no release: it is absent'], 'shape': 'tuple[NoneType, str, str]', 'calls': [["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['root_controlled_problems', 'TMP/host/p415/.switchyard-release.json', 0, '/', 'stood in', []], ['_read_switchyard_release_marker', ['PATH TMP/host/p415'], {}, None]]},
    "director: the host, root's, an old pinned release": {'answer': ['PATH TMP/shared/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['root_controlled_problems', 'TMP/host/p415/.switchyard-release.json', 0, '/', 'stood in', []], ['_read_switchyard_release_marker', ['PATH TMP/host/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/host/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['switchyard_shared_install_root', [], {}, 'PATH TMP/shared']]},
    "director: the environment's staging root": {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/envstaging/p415'], ['root_controlled_problems', 'TMP/envstaging/p415/.switchyard-release.json', 'GETUID', 'TMP/envstaging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/envstaging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/envstaging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    "director: the environment's staging root, padded": {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/envstaging/p415'], ['root_controlled_problems', 'TMP/envstaging/p415/.switchyard-release.json', 'GETUID', 'TMP/envstaging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/envstaging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/envstaging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    "director: the environment's staging root, loosened": {'answer': [None, '', "the staged release marker TMP/envstaging/p415/.switchyard-release.json is not root's: TMP/envstaging/p415 is group- or world-writable"], 'shape': 'tuple[NoneType, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/envstaging/p415'], ['root_controlled_problems', 'TMP/envstaging/p415/.switchyard-release.json', 'GETUID', 'TMP/envstaging', 'walked', ['TMP/envstaging/p415 is group- or world-writable']]]},
    'director: the environment only spaces': {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['root_controlled_problems', 'TMP/host/p415/.switchyard-release.json', 0, '/', 'stood in', []], ['_read_switchyard_release_marker', ['PATH TMP/host/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/host/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'director: an explicit root over the environment': {'answer': ['PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', ''], 'shape': 'tuple[PosixPath, str, str]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['root_controlled_problems', 'TMP/staging/p415/.switchyard-release.json', 'GETUID', 'TMP/staging', 'walked', []], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}]]},
    'bundle: an older complete pinned bundle': {'answer': [[], [], 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: no marker: current': {'answer': [[], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: an uninstalled pin: current': {'answer': [[], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: an uppercase pin: current': {'answer': [[], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: an explicit release over the pin': {'answer': [[], [], 'PATH /nonexistent/syrd415/releases/explicit'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', '/nonexistent/syrd415/releases/explicit'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: an explicit empty release': {'answer': [[], [], 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: the shared current by default': {'answer': [[], [], 'PATH TMP/shared/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': None, 'root': 'PATH TMP/staging'}, None], ['switchyard_shared_install_root', [], {}, 'PATH TMP/shared'], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/shared/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    'bundle: the shared pin by default': {'answer': [[], [], 'PATH TMP/shared/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['switchyard_shared_install_root', [], {}, 'PATH TMP/shared'], ['tenant_pinned_release_root', ['p415'], {'install_root': None, 'root': 'PATH TMP/staging'}, 'PATH TMP/shared/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/shared/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
    "bundle: the host's staging root": {'answer': [[], [], 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['_read_switchyard_release_marker', ['PATH TMP/host/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/host/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': None}, 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ["the host's staging directory, redirected into the tree", '/usr/local/lib/switchyard/p415'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/host/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], {'expect_uid': 0, 'staging_root': 'PATH TMP/host/p415'}, []]]},
    "bundle: the environment's staging root": {'answer': [[], [], 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/envstaging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/envstaging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/envstaging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': None}, 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['role_tooling_staging_dir', ['p415'], {'root': None}, 'TMP/envstaging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], {'expect_uid': 0, 'staging_root': 'PATH TMP/envstaging/p415'}, []]]},
    'bundle: absent only': {'answer': [['b.sh is not staged at TMP/x/b.sh', 'a.sh is not staged at TMP/x/a.sh'], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['b.sh is not staged at TMP/x/b.sh', 'a.sh is not staged at TMP/x/a.sh']]]},
    'bundle: each hostile shape': {'answer': [[], ['c.sh is owned by uid 4150 rather than by uid 0', 'b.sh is group- or world-writable', 'a.sh is not a regular file'], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['c.sh is owned by uid 4150 rather than by uid 0', 'b.sh is group- or world-writable', 'a.sh is not a regular file']]]},
    'bundle: absent and hostile in one problem': {'answer': [['a.sh is not staged at TMP/x/a.sh (writable)'], ['b.sh is writable'], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['a.sh is not staged at TMP/x/a.sh (writable)', 'b.sh is writable']]]},
    'bundle: benign drift': {'answer': [[], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['a.sh differs from the release', 'the marker names an older release', 'c.sh is unexpected', 'd.sh is staged at TMP/x/d.sh from an older release']]]},
    'bundle: a mixed, unordered list': {'answer': [['x.sh is not staged at TMP/x', 'v.sh is not staged at TMP/v'], ['z.sh is not a regular file', 'w.sh is owned by uid 1 rather than by uid 0'], 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, {'dataclass': 'SharedSwitchyardRelease', 'root': 'PATH TMP/staging/p415', 'marker_commit': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'marker_error': ''}], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, 'PATH TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['z.sh is not a regular file', 'y.sh differs from the release', 'x.sh is not staged at TMP/x', 'w.sh is owned by uid 1 rather than by uid 0', 'v.sh is not staged at TMP/v', 'u.sh drifted']]]},
    'bundle: the same problem twice': {'answer': [['b.sh is not staged at TMP/b', 'b.sh is not staged at TMP/b'], ['a.sh is writable', 'a.sh is writable'], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['a.sh is writable', 'a.sh is writable', 'b.sh is not staged at TMP/b', 'b.sh is not staged at TMP/b']]]},
    'bundle: other letter case': {'answer': [[], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, ['a.sh IS NOT STAGED AT TMP/a', 'b.sh is Writable', 'c.sh Owned By Uid 1']]]},
    'bundle: an explicit owner': {'answer': [[], ['a.sh is owned by uid 0 rather than by uid 424242'], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 424242, 'staging_root': 'PATH TMP/staging/p415'}, ['a.sh is owned by uid 0 rather than by uid 424242']]]},
    'bundle: the default owner': {'answer': [[], [], 'PATH TMP/opt/current'], 'shape': 'tuple[list, list, PosixPath]', 'calls': [['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['_read_switchyard_release_marker', ['PATH TMP/staging/p415'], {}, None], ['tenant_pinned_release_root', ['p415'], {'install_root': 'PATH TMP/opt', 'root': 'PATH TMP/staging'}, None], ['role_tooling_staging_dir', ['p415'], {'root': 'PATH TMP/staging'}, 'TMP/staging/p415'], ['staged_role_tooling_problems', ['p415', 'TMP/opt/current'], {'expect_uid': 0, 'staging_root': 'PATH TMP/staging/p415'}, []]]},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold415.py` (which ran them on the baseline) ------------------------------------
# A case lays out one test-owned tree: three staging roots (the one passed as `root`, the one the environment names,
# and a stand-in for the host's), two install roots (the one passed as `install_root`, and a stand-in for the shared
# one), and writes the release marker and the installed releases ONLY where the case says the code must look -- so a
# lookup in the wrong place answers differently. The marker is read by the launcher's own reader, and the root-controlled
# walk is the real one whenever it starts inside the tree; only a walk from "/" (the host) and the staged-tooling
# inspection stand in, recorded.
A = "a" * 40                   # an older pinned release
B = "b" * 40                   # a release nobody installed
PIN = {
    "an old pinned release": {"call": "pinned", "marker": {"commit": A}, "installed": [A, "current"]},
    "a marker naming an uninstalled release": {"call": "pinned", "marker": {"commit": B}, "installed": [A]},
    "a marker naming a file, not a release": {"call": "pinned", "marker": {"commit": A}, "installed_files": [A]},
    "no marker": {"call": "pinned", "marker": None, "installed": [A]},
    "a marker with no commit": {"call": "pinned", "marker": {}, "installed": [A]},
    "a marker with a blank commit": {"call": "pinned", "marker": {"commit": "   "}, "installed": [A]},
    "a marker that is not JSON": {"call": "pinned", "marker": "{not json", "installed": [A]},
    "a non-hex name, installed": {"call": "pinned", "marker": {"commit": "release-1"}, "installed": ["release-1"]},
    # Unlike the director's read, the pin is not lowered: an uppercase marker names no installed (lowercase) release.
    "an uppercase commit": {"call": "pinned", "marker": {"commit": A.upper()}, "installed": [A]},
    "the shared install root by default": {"call": "pinned", "marker": {"commit": A}, "installed": [A], "install": "default"},
    "the host's staging root": {"call": "pinned", "marker": {"commit": A}, "installed": [A], "root": None},
    "the environment's staging root": {"call": "pinned", "marker": {"commit": A}, "installed": [A], "root": None, "env": "{TMP}/envstaging"},
    "an explicit root over the environment": {"call": "pinned", "marker": {"commit": A}, "installed": [A], "env": "{TMP}/envstaging"},
}
DIRECTOR = {
    "an old pinned release, a test-owned root": {"call": "director", "marker": {"commit": A}, "installed": [A, "current"]},
    "an uppercase commit": {"call": "director", "marker": {"commit": A.upper()}, "installed": [A]},
    "a commit with spaces around it": {"call": "director", "marker": {"commit": "  " + A + "  "}, "installed": [A]},
    "a short commit": {"call": "director", "marker": {"commit": "abc1234"}, "installed": ["abc1234"]},
    "a 41-character commit": {"call": "director", "marker": {"commit": A + "a"}, "installed": [A + "a"]},
    "a 40-character non-hex name": {"call": "director", "marker": {"commit": "g" * 40}, "installed": ["g" * 40]},
    "no marker": {"call": "director", "marker": None, "installed": [A]},
    "a marker with no commit": {"call": "director", "marker": {}, "installed": [A]},
    "a marker that is not JSON": {"call": "director", "marker": "{not json", "installed": [A]},
    "a group-writable staging root": {"call": "director", "marker": {"commit": A}, "installed": [A], "loosen": [["staging", 0o775]]},
    "a world-writable marker": {"call": "director", "marker": {"commit": A}, "installed": [A], "loosen": [["staging/p415/.switchyard-release.json", 0o666]]},
    "a group-writable tenant directory": {"call": "director", "marker": {"commit": A}, "installed": [A], "loosen": [["staging/p415", 0o770]]},
    "a missing release": {"call": "director", "marker": {"commit": B}, "installed": [A]},
    "a release that is a file": {"call": "director", "marker": {"commit": A}, "installed_files": [A]},
    "the shared install root by default": {"call": "director", "marker": {"commit": A}, "installed": [A], "install": "default"},
    "the host, not root's": {"call": "director", "marker": {"commit": A}, "installed": [A], "root": None,
                             "root_answer": ["/ is owned by uid 4150 rather than by uid 0", "and a second"]},
    "the host, root's, no marker": {"call": "director", "marker": None, "installed": [A], "root": None},
    "the host, root's, an old pinned release": {"call": "director", "marker": {"commit": A}, "installed": [A], "root": None, "install": "default"},
    "the environment's staging root": {"call": "director", "marker": {"commit": A}, "installed": [A], "root": None, "env": "{TMP}/envstaging"},
    "the environment's staging root, padded": {"call": "director", "marker": {"commit": A}, "installed": [A], "root": None, "env": "  {TMP}/envstaging  "},
    "the environment's staging root, loosened": {"call": "director", "marker": {"commit": A}, "installed": [A], "root": None, "env": "{TMP}/envstaging",
                                                 "loosen": [["envstaging/p415", 0o777]]},
    "the environment only spaces": {"call": "director", "marker": {"commit": A}, "installed": [A], "root": None, "env": "   "},
    "an explicit root over the environment": {"call": "director", "marker": {"commit": A}, "installed": [A], "env": "{TMP}/envstaging"},
}
BUNDLE = {
    "an older complete pinned bundle": {"call": "bundle", "marker": {"commit": A}, "installed": [A, "current"], "problems": []},
    "no marker: current": {"call": "bundle", "marker": None, "installed": [A, "current"], "problems": []},
    "an uninstalled pin: current": {"call": "bundle", "marker": {"commit": B}, "installed": [A, "current"], "problems": []},
    "an uppercase pin: current": {"call": "bundle", "marker": {"commit": A.upper()}, "installed": [A, "current"], "problems": []},
    "an explicit release over the pin": {"call": "bundle", "marker": {"commit": A}, "installed": [A, "current"], "problems": [],
                                         "release_root": "/nonexistent/syrd415/releases/explicit"},
    "an explicit empty release": {"call": "bundle", "marker": {"commit": A}, "installed": [A], "problems": [], "release_root": ""},
    "the shared current by default": {"call": "bundle", "marker": None, "installed": [], "problems": [], "install": "default"},
    "the shared pin by default": {"call": "bundle", "marker": {"commit": A}, "installed": [A], "problems": [], "install": "default"},
    "the host's staging root": {"call": "bundle", "marker": {"commit": A}, "installed": [A], "problems": [], "root": None},
    "the environment's staging root": {"call": "bundle", "marker": {"commit": A}, "installed": [A], "problems": [], "root": None, "env": "{TMP}/envstaging"},
    "absent only": {"call": "bundle", "marker": None, "problems": ["b.sh is not staged at TMP/x/b.sh", "a.sh is not staged at TMP/x/a.sh"]},
    "each hostile shape": {"call": "bundle", "marker": None, "problems": ["c.sh is owned by uid 4150 rather than by uid 0", "b.sh is group- or world-writable",
                                                                      "a.sh is not a regular file"]},
    "absent and hostile in one problem": {"call": "bundle", "marker": None, "problems": ["a.sh is not staged at TMP/x/a.sh (writable)", "b.sh is writable"]},
    "benign drift": {"call": "bundle", "marker": None, "problems": ["a.sh differs from the release", "the marker names an older release", "c.sh is unexpected",
                                                                  "d.sh is staged at TMP/x/d.sh from an older release"]},
    "a mixed, unordered list": {"call": "bundle", "marker": {"commit": A}, "installed": [A], "problems": [
        "z.sh is not a regular file", "y.sh differs from the release", "x.sh is not staged at TMP/x", "w.sh is owned by uid 1 rather than by uid 0",
        "v.sh is not staged at TMP/v", "u.sh drifted"]},
    "the same problem twice": {"call": "bundle", "marker": None, "problems": ["a.sh is writable", "a.sh is writable", "b.sh is not staged at TMP/b", "b.sh is not staged at TMP/b"]},
    "other letter case": {"call": "bundle", "marker": None, "problems": ["a.sh IS NOT STAGED AT TMP/a", "b.sh is Writable", "c.sh Owned By Uid 1"]},
    "an explicit owner": {"call": "bundle", "marker": None, "problems": ["a.sh is owned by uid 0 rather than by uid 424242"], "expect_uid": 424242},
    "the default owner": {"call": "bundle", "marker": None, "problems": []},
}
CASES = {f"{group}: {label}": spec for group, table in (("pinned", PIN), ("director", DIRECTOR), ("bundle", BUNDLE)) for label, spec in table.items()}
SEAM_NAMES = ["role_tooling_staging_dir", "_read_switchyard_release_marker", "switchyard_shared_install_root", "staged_role_tooling_problems",
              "tenant_pinned_release_root"]
MOVED_FUNCTIONS = ("tenant_pinned_release_root", "director_readable_pinned_release", "staged_bundle_launch_problems")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s function, in a fresh test-owned tree; the launcher's seams are recorded on `t`, the host stands in."""
    import dataclasses, shutil, tempfile
    from scripts.ticket_board import publication_boundary as pb
    calls: list = []
    old_umask = os.umask(0o022)
    tmp = Path(tempfile.mkdtemp(prefix="syrd415-"))
    os.chmod(tmp, 0o755)

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items())}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"dataclass": type(value).__name__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if isinstance(value, Path):
            return "PATH " + str(value).replace(str(tmp), "TMP")
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        return value

    def record(name, real):
        def call(*args, **kwargs):
            reached.add(name)
            answer = real(*args, **kwargs)
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items()))), norm(answer)])
            return answer
        return call

    try:
        env = spec.get("env")
        staging = {"root": tmp / "staging", "env": Path(env.replace("{TMP}", str(tmp)).strip()) if env and env.strip() else None, "host": tmp / "host"}
        for directory in (tmp / "staging", tmp / "envstaging", tmp / "host", tmp / "opt" / "releases", tmp / "shared" / "releases"):
            (directory / "p415" if "releases" not in directory.parts else directory).mkdir(parents=True)
        # Where the case says the code must look, and only there.
        effective = staging["root"] if spec.get("root", "given") == "given" else (staging["env"] or staging["host"])
        install = tmp / ("opt" if spec.get("install", "given") == "given" else "shared")
        marker = spec.get("marker")
        if marker is not None:
            (effective / "p415" / ".switchyard-release.json").write_text(marker if isinstance(marker, str) else json.dumps(marker), encoding="utf-8")
        for name in spec.get("installed", []):
            (install / "releases" / name).mkdir()
        for name in spec.get("installed_files", []):
            (install / "releases" / name).write_text("", encoding="utf-8")
        for relative, mode in spec.get("loosen", []):
            os.chmod(tmp / relative, mode)

        real = {n: getattr(t, n) for n in ("role_tooling_staging_dir", "_read_switchyard_release_marker", "tenant_pinned_release_root")}
        # The function under test, taken before any stand-in goes in (the launcher's attributes are recorded below).
        under = {n: getattr(holder, n) for n in MOVED_FUNCTIONS}

        def staging_dir(project, *, root=None):
            answer = real["role_tooling_staging_dir"](project, root=root)
            # The host's own staging root is never read: the answer outside the tree is recorded, then redirected into it.
            if answer.startswith(str(tmp) + "/"):
                return answer
            calls.append(["the host's staging directory, redirected into the tree", answer])
            return str(tmp / "host" / project)

        def inspect(project, release_root, *, staging_root=None, expect_uid=None):
            return list(spec.get("problems", []))

        real_walk = pb.root_controlled_problems

        def walk(declared, *, expect_uid=None, base="/"):
            reached.add("root_controlled_problems")
            walked = base.startswith(str(tmp) + "/")
            answer = real_walk(declared, expect_uid=expect_uid, base=base) if walked else list(spec.get("root_answer", []))
            calls.append(["root_controlled_problems", norm(declared), "GETUID" if expect_uid == os.getuid() and walked else expect_uid, norm(base),
                          "walked" if walked else "stood in", norm(answer)])
            return answer

        on_launcher = {
            "role_tooling_staging_dir": record("role_tooling_staging_dir", staging_dir),
            "_read_switchyard_release_marker": record("_read_switchyard_release_marker", real["_read_switchyard_release_marker"]),
            "switchyard_shared_install_root": record("switchyard_shared_install_root", lambda: tmp / "shared"),
            "staged_role_tooling_problems": record("staged_role_tooling_problems", inspect),
            "tenant_pinned_release_root": record("tenant_pinned_release_root", real["tenant_pinned_release_root"]),
        }
        saved = {n: getattr(t, n) for n in on_launcher}
        saved_walk = pb.root_controlled_problems
        saved_env = os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
        for n, f in on_launcher.items():
            setattr(t, n, f)
        pb.root_controlled_problems = walk
        if env is not None:
            os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = env.replace("{TMP}", str(tmp))
        kwargs = {}
        if spec.get("root", "given") == "given":
            kwargs["root"] = tmp / "staging"
        if spec.get("install", "given") == "given":
            kwargs["install_root"] = tmp / "opt"
        for key in ("release_root", "expect_uid"):
            if key in spec:
                kwargs[key] = spec[key]
        call = spec["call"]
        try:
            if call == "pinned":
                got = under["tenant_pinned_release_root"]("p415", **kwargs)
            elif call == "director":
                got = under["director_readable_pinned_release"]("p415", **kwargs)
            else:
                got = under["staged_bundle_launch_problems"]("p415", **kwargs)
            answer = norm(got)
            shape = type(got).__name__ + ("[" + ", ".join(type(x).__name__ for x in got) + "]" if isinstance(got, tuple) else "")
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            answer, shape = {"raised": type(exc).__name__, "message": norm(str(exc))}, "raised"
        finally:
            for n, f in saved.items():
                setattr(t, n, f)
            pb.root_controlled_problems = saved_walk
            os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
            if saved_env is not None:
                os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = saved_env
        return {"answer": answer, "shape": shape, "calls": calls}
    finally:
        os.umask(old_umask)
        for relative, mode in spec.get("loosen", []):
            os.chmod(tmp / relative, 0o755)
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


def test_the_module_loads_only_the_owner_module_at_import() -> None:
    result = python("import sys, scripts.staged_launch_checks as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.staged_role_tooling']",
          f"it imports on its own, loading only the module its default comes from and never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_default() -> None:
    for order in (("scripts.staged_launch_checks", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.staged_launch_checks")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.staged_launch_checks as m, scripts.staged_role_tooling as s; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "inspect.signature(m.staged_bundle_launch_problems).parameters['expect_uid'].default is s.STAGED_TOOLING_OWNER_UID is t.STAGED_TOOLING_OWNER_UID "
                        "is m.STAGED_TOOLING_OWNER_UID == 0, "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'root_controlled_problems') and not hasattr(s, 'staged_bundle_launch_problems'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.Path is Path and m.re is __import__("re"), "the standard-library names are the module's own")
    check(m.STAGED_TOOLING_ABSENT_MARKER == "is not staged at" and m.STAGED_TOOLING_HOSTILE_MARKERS == ("owned by uid", "writable", "is not a regular file"),
          "the two markers, unchanged")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "staged_launch_checks.py").read_text(encoding="utf-8"))
    for name in MOVED_FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        nested = ["from scripts.ticket_board.publication_boundary import root_controlled_problems"] if name == "director_readable_pinned_release" else []
        check(imports == ["from scripts import team_launcher as launcher", *nested] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported first thing, and the walk still imported when it runs: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import os", "import re", "from pathlib import Path",
                  "from scripts.staged_role_tooling import STAGED_TOOLING_OWNER_UID"],
          f"the standard library and the owner default, nothing else: {top}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the five in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_five_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.staged_launch_checks"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the five, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"switchyard_invocation_requires_root", "_project_config_path_owner_user", "SWITCHYARD_RELEASE_MARKER_NAME",
                                        "switchyard_shared_install_root", "_read_switchyard_release_marker", "role_tooling_staging_dir",
                                        "staged_role_tooling_problems", "STAGED_TOOLING_OWNER_UID"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    calls: dict = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for x in ast.walk(fn):
                if isinstance(x, ast.Call) and ast.unparse(x.func).split(".")[-1] in MOVED:
                    calls.setdefault(fn.name, {}).setdefault(ast.unparse(x.func), 0)
                    calls[fn.name][ast.unparse(x.func)] += 1
    check(calls == DISPATCH, f"no launcher definition calls them, as before: {calls}")
    named = sorted({x.id for fn in tree.body if not isinstance(fn, (ast.Import, ast.ImportFrom)) for x in ast.walk(fn) if isinstance(x, ast.Name) and x.id in MOVED})
    check(named == [], f"nor names them anywhere else: {named}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads them through the launcher: {got}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_launch_check_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def answer(label):
        return GOLDEN[label]["answer"]

    def called(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    check(answer("bundle: an older complete pinned bundle")[2].endswith("/releases/" + A), "an older complete bundle is judged against its own release")
    check(answer("bundle: no marker: current")[2].endswith("/current") and answer("bundle: an uninstalled pin: current")[2].endswith("/current"),
          "current only when no installed release is named")
    check(answer("bundle: an explicit release over the pin")[2] == "PATH /nonexistent/syrd415/releases/explicit", "an explicit release wins over the pin")
    for label in ("director: no marker", "director: a group-writable staging root", "director: the host, not root's"):
        check("_read_switchyard_release_marker" not in called(label) and answer(label)[0] is None, f"{label}: no byte of the marker believed before the walk passes")
    check(all(answer(f"director: {label}")[0] is None for label in ("a short commit", "a 41-character commit", "a 40-character non-hex name", "a missing release")),
          "only a 40-hex commit whose release is installed")
    host = [c for c in GOLDEN["director: the host, not root's"]["calls"] if c[0] == "root_controlled_problems"]
    check(host == [["root_controlled_problems", "TMP/host/p415/.switchyard-release.json", 0, "/", "stood in", ["/ is owned by uid 4150 rather than by uid 0", "and a second"]]],
          f"on a host the walk starts at / and expects root: {host}")
    check(answer("bundle: absent and hostile in one problem") == [["a.sh is not staged at TMP/x/a.sh (writable)"], ["b.sh is writable"], "PATH TMP/opt/current"],
          "a problem is absent or hostile, never both")
    check(answer("bundle: benign drift")[:2] == [[], []], "drift is neither restaged nor refused")
    owner = [c for c in GOLDEN["bundle: the default owner"]["calls"] if c[0] == "staged_role_tooling_problems"]
    check(owner and owner[0][2]["expect_uid"] == 0, f"the default owner is root: {owner}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads} - {"SWITCHYARD_RELEASE_MARKER_NAME", "STAGED_TOOLING_ABSENT_MARKER", "STAGED_TOOLING_HOSTILE_MARKERS"}
    check(expected <= REACHED, f"a recorder on the launcher reached every seam: missing {sorted(expected - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_the_owner_module_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_default",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_five_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"staged_launch_checks_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
