#!/usr/bin/env python3
"""SYRD-433: the Switchyard project registration, against the launcher it came out of.

`_registered_project_collision`, `_check_switchyard_registration_available`,
`_register_switchyard_project` and `switchyard_register_command` -- checking a
project config can be registered, refusing a collision, and recording its
registry entry -- moved unchanged into `scripts/switchyard_registration.py`;
the launcher re-exports all four. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; the one
  definition-time default is `print`.
- **Seams (rule 24):** every name they read -- each other, the registry
  directory, schema and agent-CLI key, the JSON reader, the config loader, the
  slug check, the registry listing and the configured-CLI reader -- is read
  through the launcher as often as before, so a patch there reaches it: every
  case below records them there, stands in the registry and the loader, and
  rebinds the schema and key there to see the effect.
- **Callers:** `switchyard_main` dispatches `register` to the launcher's name,
  and `new_project_phases.py` and `resume_provision_command.py` read the check
  and the registration there.
- **The behaviour is the baseline's:** slug and name collisions whatever their
  case, a config's own earlier registration, every registration answer and
  refusal, what the entry records and the modes it is written with, and the
  command directly and through `switchyard_main`. `GOLDEN` below was produced
  by running the BASELINE launcher's own definitions over the very cases
  embedded here (`gold433.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME and USER and under umask
  077.

No real registry is written: the registry directory is always inside a
test-owned tree. Spawns, every exec, signals, account and group lookups and
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
from scripts import switchyard_registration as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ('_registered_project_collision', '_check_switchyard_registration_available', '_register_switchyard_project', 'switchyard_register_command')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_registered_project_collision': {'_switchyard_entries': 1},
    '_check_switchyard_registration_available': {'_registered_project_collision': 1},
    '_register_switchyard_project': {'SWITCHYARD_REGISTRY_AGENT_CLIS_KEY': 1, 'SWITCHYARD_REGISTRY_SCHEMA': 1, '_check_switchyard_registration_available': 1, '_configured_agent_clis': 1, '_load_json': 1, '_validate_project_slug': 1, 'load_project_config': 1, 'switchyard_registry_dir': 1},
    'switchyard_register_command': {'_register_switchyard_project': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the four that names them, and how often.
DISPATCH = {'switchyard_main': {'switchyard_register_command': 1}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/new_project_phases.py': ['launcher._check_switchyard_registration_available', 'launcher._register_switchyard_project'], 'scripts/resume_provision_command.py': ['launcher._register_switchyard_project']}
#: The BASELINE's own behaviour for the cases below (`gold433.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'collision: none': {'result': '', 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'collision: the slug, in another case': {'result': "switchyard: project slug 'P433_B' is already registered to 'Beta 433' at TMP/configs/b.json", 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'collision: the name, in another case': {'result': "switchyard: project name 'ALPHA 433' is already registered as 'p433_a' at TMP/configs/a.json", 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'collision: the slug checked before the name': {'result': "switchyard: project name 'Alpha 433' is already registered as 'p433_a' at TMP/configs/a.json", 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'collision: the skipped config is its own': {'result': '', 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'collision: nothing registered': {'result': '', 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'collision: one entry matching both, the slug named': {'result': "switchyard: project slug 'p433_a' is already registered to 'Alpha 433' at TMP/configs/a.json", 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    "collision: the skip resolves the entry's path too": {'result': '', 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'check: available': {'result': None, 'calls': [['_registered_project_collision', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/reg', 'skip_config_path': None, 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'check: refused': {'result': {'raised': 'SystemExit', 'message': "switchyard: project slug 'p433_a' is already registered to 'Alpha 433' at TMP/configs/a.json"}, 'calls': [['_registered_project_collision', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/reg', 'skip_config_path': None, 'slug': 'p433_a'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/reg'}]], 'printed': [], 'files': {}, 'modes the writer set': {}},
    'register: a new project': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433", "project_name": "P 433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "P 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: the slug from the file name': {'result': 'PATH TMP/root/registry/stem433.json', 'calls': [['_load_json', ['PATH TMP/configs/stem433.json'], {}], ['_validate_project_slug', ['stem433'], {}], ['load_project_config', ['stem433', 'PATH TMP/configs/stem433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'Named 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/stem433.json', 'slug': 'stem433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'Named 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/stem433.json', 'slug': 'stem433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/stem433.json': ['per umask', '{"project_name": "Named 433"}'], 'TMP/root/registry/stem433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/stem433.json",\n  "name": "Named 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "stem433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: the name from the legacy name key': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'Legacy 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'Legacy 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433", "name": "Legacy 433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "Legacy 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: the name defaults to the slug': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "p433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: a blank name defaults to the slug': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433", "project_name": "   "}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "p433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: the slug normalized': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['P433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "  P433  ", "project_name": "P 433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "P 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: an explicit registry directory': {'result': 'PATH TMP/explicit/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/explicit/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/explicit/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/explicit/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/explicit/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "p433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/explicit/registry': '0o755', 'TMP/explicit': '0o755'}},
    'register: a config path with dot segments': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "p433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: an empty slug': {'result': {'raised': 'SystemExit', 'message': 'switchyard: cannot register TMP/configs/p433.json: project slug is empty'}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "   "}']}, 'modes the writer set': {}},
    'register: an invalid slug': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$'}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['bad slug!'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "bad slug!"}']}, 'modes the writer set': {}},
    'register: a slug that collides': {'result': {'raised': 'SystemExit', 'message': "switchyard: project slug 'p433_a' is already registered to 'Alpha 433' at TMP/configs/a.json"}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['P433_A'], {}], ['load_project_config', ['p433_a', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'Other', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433_a'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'Other', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433_a'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "P433_A", "project_name": "Other"}']}, 'modes the writer set': {}},
    'register: a name that collides': {'result': {'raised': 'SystemExit', 'message': "switchyard: project name 'beta 433' is already registered as 'p433_b' at TMP/configs/b.json"}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'beta 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'beta 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433", "project_name": "beta 433"}']}, 'modes the writer set': {}},
    'register: its own earlier registration is not a collision': {'result': 'PATH TMP/root/registry/p433_a.json', 'calls': [['_load_json', ['PATH TMP/configs/a.json'], {}], ['_validate_project_slug', ['p433_a'], {}], ['load_project_config', ['p433_a', 'PATH TMP/configs/a.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': 'PATH TMP/configs', 'name': 'Alpha 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/a.json', 'slug': 'p433_a'}], ['_registered_project_collision', [], {'config_dir': 'PATH TMP/configs', 'name': 'Alpha 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/a.json', 'slug': 'p433_a'}], ['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/a.json': ['per umask', '{"project": "p433_a", "project_name": "Alpha 433"}'], 'TMP/root/registry/p433_a.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/a.json",\n  "name": "Alpha 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433_a"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'register: a registry entry already there': {'result': {'raised': 'SystemExit', 'message': 'switchyard: registry entry TMP/root/registry/p433.json already exists; refusing to overwrite'}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry/p433.json': ['per umask', '{"already": true}\n']}, 'modes the writer set': {}},
    'register: a registry that cannot be written': {'result': {'raised': 'SystemExit', 'message': "switchyard: failed to register project 'p433' in TMP/root/registry: [Errno 17] File exists: 'TMP/root/registry'"}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry': ['per umask', 'a file where the registry directory goes\n']}, 'modes the writer set': {}},
    'register: a config that is not a JSON object': {'result': {'raised': 'SystemExit', 'message': 'TMP/configs/p433.json must contain a JSON object'}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '["not", "an", "object"]']}, 'modes the writer set': {}},
    'register: the config loader refuses': {'result': {'raised': 'SystemExit', 'message': 'syrd433: the config loader refuses'}, 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}']}, 'modes the writer set': {}},
    'register: the schema and CLI key rebound on the launcher': {'result': 'PATH TMP/root/registry/p433.json', 'calls': [['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "config_path": "TMP/configs/p433.json",\n  "name": "p433",\n  "schema": "syrd433.schema",\n  "slug": "p433",\n  "syrd433_clis": [\n    "codex",\n    "claude"\n  ]\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'switchyard_register_command': {'result': 0, 'calls': [['_register_switchyard_project', ['PATH TMP/configs/p433.json'], {'config_dir': None, 'registry_dir': None}], ['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': ['switchyard: registered TMP/configs/p433.json at TMP/root/registry/p433.json'], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433", "project_name": "P 433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "P 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'switchyard_register_command, refused': {'result': {'raised': 'SystemExit', 'message': 'switchyard: registry entry TMP/root/registry/p433.json already exists; refusing to overwrite'}, 'calls': [['_register_switchyard_project', ['PATH TMP/configs/p433.json'], {'config_dir': None, 'registry_dir': None}], ['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}]], 'printed': [], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry/p433.json': ['per umask', '{"already": true}\n']}, 'modes the writer set': {}},
    'switchyard_register_command, a config path with dot segments': {'result': 0, 'calls': [['_register_switchyard_project', ['PATH TMP/configs/sub/../p433.json'], {'config_dir': None, 'registry_dir': None}], ['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'p433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': ['switchyard: registered TMP/configs/p433.json at TMP/root/registry/p433.json'], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "p433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
    'switchyard register, through switchyard_main': {'result': 0, 'calls': [['report_installed_release_version', [], {}], ['switchyard_register_command', ['PATH TMP/configs/p433.json'], {}], ['_register_switchyard_project', ['PATH TMP/configs/p433.json'], {'config_dir': None, 'registry_dir': None}], ['_load_json', ['PATH TMP/configs/p433.json'], {}], ['_validate_project_slug', ['p433'], {}], ['load_project_config', ['p433', 'PATH TMP/configs/p433.json'], {}], ['switchyard_registry_dir', [], {}], ['_check_switchyard_registration_available', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_registered_project_collision', [], {'config_dir': None, 'name': 'P 433', 'registry_dir': 'PATH TMP/root/registry', 'skip_config_path': 'PATH TMP/configs/p433.json', 'slug': 'p433'}], ['_switchyard_entries', [], {'config_dir': None, 'registry_dir': 'PATH TMP/root/registry'}], ['_configured_agent_clis', ['NAMESPACE config'], {}]], 'printed': ['switchyard: registered TMP/configs/p433.json at TMP/root/registry/p433.json'], 'files': {'TMP/configs/p433.json': ['per umask', '{"project": "p433", "project_name": "P 433"}'], 'TMP/root/registry/p433.json': ['0o644', '{\n  "agent_clis": [\n    "codex",\n    "claude"\n  ],\n  "config_path": "TMP/configs/p433.json",\n  "name": "P 433",\n  "schema": "switchyard.project-registry.v1",\n  "slug": "p433"\n}\n']}, 'modes the writer set': {'TMP/root/registry': '0o755', 'TMP/root': '0o755'}},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold433.py` (which ran them on the baseline) ------------------------------------
# A case checks for a collision, registers a synthetic project config, or runs `switchyard register` (directly or
# through `switchyard_main`), in a fresh test-owned tree. Every name the four read on the launcher is a recorder there:
# the registry directory, the existing registrations, the config loader and the configured-CLI reader answer from the
# case; the JSON reader and the slug check record the call and run the real thing. No real registry is written: the
# registry directory is always inside the tree. Recorded, in order: every call with its arguments, everything printed,
# the result or the exact refusal, and every file left in the tree with the mode the code sets.
REG = [["p433_a", "Alpha 433", "@/configs/a.json"], ["p433_b", "Beta 433", "@/configs/b.json"]]
REGISTER = {
    "a new project": {"config": {"project": "p433", "project_name": "P 433"}},
    "the slug from the file name": {"config": {"project_name": "Named 433"}, "file": "stem433.json"},
    "the name from the legacy name key": {"config": {"project": "p433", "name": "Legacy 433"}},
    "the name defaults to the slug": {"config": {"project": "p433"}},
    "a blank name defaults to the slug": {"config": {"project": "p433", "project_name": "   "}},
    "the slug normalized": {"config": {"project": "  P433  ", "project_name": "P 433"}},
    "an explicit registry directory": {"config": {"project": "p433"}, "registry": "@/explicit/registry"},
    "a config path with dot segments": {"config": {"project": "p433"}, "path": "configs/./sub/../p433.json"},
    "an empty slug": {"config": {"project": "   "}},
    "an invalid slug": {"config": {"project": "bad slug!"}},
    "a slug that collides": {"config": {"project": "P433_A", "project_name": "Other"}, "entries": REG},
    "a name that collides": {"config": {"project": "p433", "project_name": "beta 433"}, "entries": REG},
    "its own earlier registration is not a collision": {"config": {"project": "p433_a", "project_name": "Alpha 433"}, "file": "a.json",
                                                         "entries": REG, "configs_dir": True},
    "a registry entry already there": {"config": {"project": "p433"}, "existing": True},
    "a registry that cannot be written": {"config": {"project": "p433"}, "unwritable": True},
    "a config that is not a JSON object": {"config": ["not", "an", "object"]},
    "the config loader refuses": {"config": {"project": "p433"}, "load": "refuse"},
    "the schema and CLI key rebound on the launcher": {"config": {"project": "p433"},
                                                       "launcher": {"SWITCHYARD_REGISTRY_SCHEMA": "syrd433.schema", "SWITCHYARD_REGISTRY_AGENT_CLIS_KEY": "syrd433_clis"}},
}
CASES = {
    "collision: none": {"call": "collision", "slug": "p433", "name": "P 433", "entries": REG},
    "collision: the slug, in another case": {"call": "collision", "slug": "P433_B", "name": "Other", "entries": REG},
    "collision: the name, in another case": {"call": "collision", "slug": "p433", "name": "ALPHA 433", "entries": REG},
    "collision: the slug checked before the name": {"call": "collision", "slug": "p433_b", "name": "Alpha 433", "entries": REG},
    "collision: the skipped config is its own": {"call": "collision", "slug": "p433_a", "name": "Alpha 433", "entries": REG, "skip": "@/configs/./a.json"},
    "collision: nothing registered": {"call": "collision", "slug": "p433", "name": "P 433"},
    "collision: one entry matching both, the slug named": {"call": "collision", "slug": "p433_a", "name": "alpha 433", "entries": REG},
    "collision: the skip resolves the entry's path too": {"call": "collision", "slug": "p433_a", "name": "Alpha 433", "skip": "@/configs/a.json",
                                                         "entries": [["p433_a", "Alpha 433", "@/configs/sub/../a.json"]]},
    "check: available": {"call": "check", "slug": "p433", "name": "P 433", "entries": REG},
    "check: refused": {"call": "check", "slug": "p433_a", "name": "P 433", "entries": REG},
    **{f"register: {k}": {"call": "register", **v} for k, v in REGISTER.items()},
    "switchyard_register_command": {"call": "command", "config": {"project": "p433", "project_name": "P 433"}},
    "switchyard_register_command, refused": {"call": "command", "config": {"project": "p433"}, "existing": True},
    "switchyard_register_command, a config path with dot segments": {"call": "command", "config": {"project": "p433"}, "path": "configs/sub/../p433.json"},
    "switchyard register, through switchyard_main": {"call": "main", "config": {"project": "p433", "project_name": "P 433"}},
}
FUNCTIONS = ("_registered_project_collision", "_check_switchyard_registration_available", "_register_switchyard_project", "switchyard_register_command")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions, in a fresh test-owned tree; every name they read a recorder on `t`."""
    import contextlib, json, shutil, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    printed: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd433-")).resolve()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
        if isinstance(value, _P):
            return "PATH " + norm(str(value))
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"NAMESPACE {getattr(value, 'label', '?')}"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    place = lambda v: (tmp / v[2:]) if isinstance(v, str) and v.startswith("@/") else v
    names = [*FUNCTIONS, "switchyard_registry_dir", "SWITCHYARD_REGISTRY_SCHEMA", "SWITCHYARD_REGISTRY_AGENT_CLIS_KEY", "_load_json",
             "load_project_config", "_validate_project_slug", "_switchyard_entries", "_configured_agent_clis", "report_installed_release_version"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        default_registry = tmp / "root" / "registry"
        (tmp / "root").mkdir()
        (tmp / "root").chmod(0o700)  # so the writer's own chmod of the registry's parent shows
        (tmp / "configs").mkdir()
        config_path = tmp / "configs" / spec.get("file", "p433.json")
        if "config" in spec:
            config_path.write_text(json.dumps(spec["config"]), encoding="utf-8")
        if "path" in spec:
            (tmp / "configs" / "sub").mkdir()
            config_path = _P(str(tmp) + "/" + spec["path"])  # handed over unresolved, as a caller might
        registry = place(spec["registry"]) if "registry" in spec else None
        if registry is not None:
            registry.parent.mkdir(parents=True)
        if spec.get("existing"):
            default_registry.mkdir()
            (default_registry / "p433.json").write_text("{\"already\": true}\n", encoding="utf-8")
        if spec.get("unwritable"):
            default_registry.write_text("a file where the registry directory goes\n", encoding="utf-8")

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            return f

        def entries(*, config_dir, registry_dir):
            note("_switchyard_entries", config_dir=config_dir, registry_dir=registry_dir)
            return [SimpleNamespace(slug=s, name=n, config_path=place(p)) for s, n, p in spec.get("entries", [])]

        def load(project, path):
            note("load_project_config", project, path)
            if spec.get("load") == "refuse":
                raise SystemExit("syrd433: the config loader refuses")
            return SimpleNamespace(label="config")

        on_launcher = {n: passthrough(n) for n in (*FUNCTIONS, "_load_json", "_validate_project_slug")}
        on_launcher.update(
            switchyard_registry_dir=lambda: note("switchyard_registry_dir") or default_registry,
            _switchyard_entries=entries,
            load_project_config=load,
            _configured_agent_clis=lambda config: note("_configured_agent_clis", config) or ["codex", "claude"],
            report_installed_release_version=lambda: note("report_installed_release_version"),
        )
        on_launcher.update(spec.get("launcher", {}))
        for n, f in on_launcher.items():
            setattr(t, n, f)
        call = spec["call"]
        kw = {"config_dir": tmp / "configs"} if spec.get("configs_dir") else {}
        try:
            with contextlib.redirect_stdout(SimpleNamespace(write=lambda s: printed.append(norm(s)) if s.strip() else None, flush=lambda: None)):
                if call == "collision":
                    got = under["_registered_project_collision"](slug=spec["slug"], name=spec["name"], registry_dir=tmp / "reg",
                                                                 **({"skip_config_path": place(spec["skip"])} if "skip" in spec else {}))
                elif call == "check":
                    got = under["_check_switchyard_registration_available"](slug=spec["slug"], name=spec["name"], registry_dir=tmp / "reg")
                elif call == "register":
                    got = under["_register_switchyard_project"](config_path, **kw, **({"registry_dir": registry} if registry is not None else {}))
                elif call == "command":
                    got = under["switchyard_register_command"](config_path, print_func=lambda line: printed.append(norm(line)))
                else:
                    got = t.switchyard_main(["register", str(config_path)])
            result = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        # The writer sets the modes of exactly what a successful registration returns: the entry, its directory and that
        # directory's parent. Everything else here the fixture made, under whatever umask the run has.
        written = got if isinstance(result, str) and result.startswith("PATH ") else (
            (default_registry / "p433.json") if result == 0 else None)
        files = {}
        for path in sorted(tmp.rglob("*")):
            if path.is_file():
                files[norm(str(path))] = [oct(path.stat().st_mode & 0o777) if path == written else "per umask", norm(path.read_text(encoding="utf-8"))]
        modes = {norm(str(d)): oct(d.stat().st_mode & 0o777) for d in ((written.parent, written.parent.parent) if written else ())}
        return {"result": result, "calls": calls, "printed": printed, "files": files, "modes the writer set": modes}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
        for d in sorted(tmp.rglob("*"), reverse=True):
            if d.is_dir():
                d.chmod(0o700)
        tmp.chmod(0o700)
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


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.switchyard_registration as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.switchyard_registration", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.switchyard_registration")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.switchyard_registration as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}' for n in " + repr(MOVED) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty and v.default is not None), "
                        "inspect.signature(m.switchyard_register_command).parameters['print_func'].default is print, "
                        "not hasattr(m, 'launcher'))")
        check(result.stdout.strip() == "True ['switchyard_register_command.print_func'] True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.json is json and m.Path is Path and m.Callable is typing.Callable,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "switchyard_registration.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, siblings included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        nested: dict = {}  # the baseline had no nested import
        check(imports[:1] == (["from scripts import team_launcher as launcher"] if expected else [])
              and sorted(imports[1:] if expected else imports) == sorted(nested.get(name, []))
              and (not expected or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and otherwise only the baseline's own nested import: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import json", "from pathlib import Path", "from typing import Callable"] and tc == [],
          f"the standard library only, and no TYPE_CHECKING block: {top} {tc}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the four in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_four_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.switchyard_registration"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the four, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"switchyard_main", "_switchyard_entries", "_build_switchyard_register_parser", "switchyard_registry_dir"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"switchyard_main calls the command by its launcher global, exactly as often as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads the check and the registration through the launcher: {got}")


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

    check(result("collision: the slug, in another case") == "switchyard: project slug 'P433_B' is already registered to 'Beta 433' at TMP/configs/b.json"
          and result("collision: the name, in another case") == "switchyard: project name 'ALPHA 433' is already registered as 'p433_a' at TMP/configs/a.json"
          and result("collision: none") == result("collision: the skipped config is its own") == result("collision: nothing registered") == "",
          "a slug or name collides whatever its case, word for word; the config being registered is not its own collision")
    check(result("collision: the slug checked before the name").startswith("switchyard: project name 'Alpha 433'")
          and result("check: available") is None and result("check: refused")["raised"] == "SystemExit",
          "the registrations are checked in order, the first matching one answers; the check refuses with the collision")
    new = GOLDEN["register: a new project"]
    entry = json.loads(new["files"]["TMP/root/registry/p433.json"][1])
    check(result("register: a new project") == "PATH TMP/root/registry/p433.json" and new["files"]["TMP/root/registry/p433.json"][0] == "0o644"
          and new["modes the writer set"] == {"TMP/root/registry": "0o755", "TMP/root": "0o755"}
          and entry == {"agent_clis": ["codex", "claude"], "config_path": "TMP/configs/p433.json", "name": "P 433",
                        "schema": "switchyard.project-registry.v1", "slug": "p433"},
          "the entry records the schema, slug, name, config path and configured CLIs, world-readable, in a world-readable directory")
    order = seams("register: a new project")
    check(order.index("_load_json") < order.index("_validate_project_slug") < order.index("load_project_config") < order.index("_check_switchyard_registration_available")
          and "_configured_agent_clis" in order,
          f"read, slug checked, config loaded, collisions checked, then written: {order}")
    check(result("register: the slug from the file name") == "PATH TMP/root/registry/stem433.json"
          and json.loads(GOLDEN["register: the name defaults to the slug"]["files"]["TMP/root/registry/p433.json"][1])["name"] == "p433"
          and json.loads(GOLDEN["register: the name from the legacy name key"]["files"]["TMP/root/registry/p433.json"][1])["name"] == "Legacy 433",
          "the slug from the project key or the file name; the name from project_name, the legacy name key, or the slug")
    check(result("register: an empty slug")["message"] == "switchyard: cannot register TMP/configs/p433.json: project slug is empty"
          and result("register: a registry entry already there")["message"].endswith("already exists; refusing to overwrite")
          and result("register: a registry that cannot be written")["message"].startswith("switchyard: failed to register project 'p433' in TMP/root/registry: ")
          and result("register: its own earlier registration is not a collision") == "PATH TMP/root/registry/p433_a.json",
          "each refusal, word for word; nothing is overwritten; re-registering a config is not refused as a collision with itself")
    rebound = json.loads(GOLDEN["register: the schema and CLI key rebound on the launcher"]["files"]["TMP/root/registry/p433.json"][1])
    check(rebound["schema"] == "syrd433.schema" and "syrd433_clis" in rebound, "the schema and CLI key are the launcher's when it runs")
    check(result("switchyard_register_command") == 0 and GOLDEN["switchyard_register_command"]["printed"]
          == ["switchyard: registered TMP/configs/p433.json at TMP/root/registry/p433.json"]
          and seams("switchyard register, through switchyard_main")[:2] == ["report_installed_release_version", "switchyard_register_command"],
          "the command says what it registered where; switchyard_main dispatches register to the launcher's name")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the four read is a recorder on the launcher (the four themselves included); the schema and the
    # CLI key are rebound there by their own case, and change what is written.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name))}
    check(names - constants <= REACHED, f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(constants == rebound == {"SWITCHYARD_REGISTRY_SCHEMA", "SWITCHYARD_REGISTRY_AGENT_CLIS_KEY"}, f"and every constant rebound: {sorted(constants)} {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_four_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"switchyard_registration_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
