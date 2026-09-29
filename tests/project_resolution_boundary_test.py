#!/usr/bin/env python3
"""SYRD-454: project discovery and resolution, against the launcher they came out of.

The seven -- the launcher configs and registration records, merged; the
partial-provision record and the hint it gives; and the resolution of a
selection, with the slugs a name derives -- moved unchanged into
`scripts/project_resolution.py`; the launcher re-exports them all. This pins
what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher; every default is
  the same (None).
- **Seams (rule 24):** everything they read when they run -- each other, the
  config and registry directories and schema, the JSON reader, the slug
  validator and derivations, the entry type and the privileged baseline plan
  path -- is read through the launcher as often as before, so a patch there
  reaches them: every case below records them there.
- **The partial-provision record is never followed:** `os.stat` with
  `follow_symlinks=False`, and a regular file only.
- **Readers:** the launcher's own callers name them as before, and every
  production module that reads them does so through the launcher.
- **The behaviour is the baseline's:** configs and registration records kept or
  skipped, with the warnings; the two merged; a partial-provision record as a
  file, a symlink or a directory; the hint; and resolution by slug, name and
  derived slugs, with every refusal. `GOLDEN` below was produced by running the
  BASELINE launcher's own definitions over the very cases embedded here
  (`gold454.py`), not typed; it is byte-identical under `env -i`, in a normal
  role pane, with another HOME, USER and COLUMNS, under umask 077, under
  several hash seeds and with a stray SWITCHYARD_REGISTRY_DIR.

Every config and registration record is synthetic, in a test-owned temporary
tree that is also `$HOME`; the directories and the record path are stand-ins,
so no real tenant record is read. Spawns, every exec, signals, account and
group lookups and socket connections are refused for each case.
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
from scripts import project_resolution as m  # noqa: E402

CHECKS = 0
MOVED = ('_project_name_selector_slugs', '_project_entries', '_registry_project_entries', '_switchyard_entries', 'partial_provision_record', '_resume_provision_hint', '_resolve_switchyard_project')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_project_name_selector_slugs': {'_legacy_dash_slug_from_project_name': 1, '_slug_from_project_name': 1},
    '_project_entries': {'DEFAULT_CONFIG_DIR': 1, 'SwitchyardProjectEntry': 1, '_load_json': 1, '_validate_project_slug': 1},
    '_registry_project_entries': {'SWITCHYARD_REGISTRY_SCHEMA': 1, 'SwitchyardProjectEntry': 1, '_load_json': 1, '_validate_project_slug': 1, 'switchyard_registry_dir': 1},
    '_switchyard_entries': {'_project_entries': 1, '_registry_project_entries': 1},
    'partial_provision_record': {'privileged_baseline_plan_path': 1},
    '_resume_provision_hint': {'partial_provision_record': 1},
    '_resolve_switchyard_project': {'_project_name_selector_slugs': 1, '_resume_provision_hint': 1, '_switchyard_entries': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the seven that names them, and how often.
DISPATCH = {'_resolve_launcher_project_config': {'_resolve_switchyard_project': 1}, '_usable_switchyard_entry_for_project': {'_switchyard_entries': 1}, 'switchyard_menu_command': {'_switchyard_entries': 1}, 'switchyard_validate_models_command': {'_resolve_switchyard_project': 1}}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/desktop_presentation.py': {'launcher._resolve_switchyard_project': 1}, 'scripts/onboarding_readiness.py': {'launcher._switchyard_entries': 1}, 'scripts/presentation_commands.py': {'launcher._resolve_switchyard_project': 1}, 'scripts/project_status.py': {'launcher._resolve_switchyard_project': 1, 'launcher._switchyard_entries': 1}, 'scripts/repository_boundary_repair.py': {'launcher.partial_provision_record': 1}, 'scripts/resume_provision_command.py': {'launcher.partial_provision_record': 1}, 'scripts/switchyard_dispatch.py': {'launcher._resolve_switchyard_project': 18}, 'scripts/switchyard_registration.py': {'launcher._switchyard_entries': 1}, 'scripts/upstream_report.py': {'launcher._registry_project_entries': 1}, 'scripts/worker_pool_command.py': {'launcher._resolve_switchyard_project': 1}, 'scripts/workflow_adoption.py': {'launcher._resolve_switchyard_project': 1, 'launcher.partial_provision_record': 1}}
#: The BASELINE's own behaviour for the cases below (`gold454.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'selector slugs: a name': {'result': {'set': ['my-project-name', 'my_project_name']}, 'type': 'set', 'calls': [['_slug_from_project_name', ['My Project Name'], {}], ['_validate_project_slug', ['my_project_name'], {}], ['_legacy_dash_slug_from_project_name', ['My Project Name'], {}]], 'stderr': []},
    'selector slugs: one word': {'result': {'set': ['a']}, 'type': 'set', 'calls': [['_slug_from_project_name', ['a'], {}], ['_validate_project_slug', ['a'], {}], ['_legacy_dash_slug_from_project_name', ['a'], {}]], 'stderr': []},
    'selector slugs: nothing derivable': {'result': {'set': []}, 'type': 'set', 'calls': [['_slug_from_project_name', ['!!!'], {}], ['_legacy_dash_slug_from_project_name', ['!!!'], {}]], 'stderr': []},
    'selector slugs: punctuation': {'result': {'set': ['project-454-ltd', 'project_454_ltd']}, 'type': 'set', 'calls': [['_slug_from_project_name', ['Project 454, Ltd.'], {}], ['_validate_project_slug', ['project_454_ltd'], {}], ['_legacy_dash_slug_from_project_name', ['Project 454, Ltd.'], {}]], 'stderr': []},
    'selector slugs: casefolded, not only lower-cased': {'result': {'set': ['stra_e_454', 'strasse-454']}, 'type': 'set', 'calls': [['_slug_from_project_name', ['Straße 454'], {}], ['_validate_project_slug', ['stra_e_454'], {}], ['_legacy_dash_slug_from_project_name', ['Straße 454'], {}]], 'stderr': []},
    'configs: the given directory': {'result': [{'entry': {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}}, {'entry': {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}}, {'entry': {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}}, {'entry': {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}}], 'type': 'list', 'calls': [['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'configs: the default directory': {'result': [{'entry': {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/default-configs/alpha.json'}}, {'entry': {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/default-configs/beta.json'}}, {'entry': {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/default-configs/blank.json'}}, {'entry': {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/default-configs/p454.json'}}], 'type': 'list', 'calls': [['_load_json', ['PATH TMP/default-configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/default-configs/alpha.json'}], ['_load_json', ['PATH TMP/default-configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/default-configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/default-configs/beta.json'}], ['_load_json', ['PATH TMP/default-configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/default-configs/blank.json'}], ['_load_json', ['PATH TMP/default-configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/default-configs/noroles.json'], {}], ['_load_json', ['PATH TMP/default-configs/notjson.json'], {}], ['_load_json', ['PATH TMP/default-configs/notobject.json'], {}], ['_load_json', ['PATH TMP/default-configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/default-configs/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/default-configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'configs: a directory that is not there': {'result': [], 'type': 'list', 'calls': [], 'stderr': []},
    'configs: a file, not a directory': {'result': [], 'type': 'list', 'calls': [], 'stderr': []},
    'registry: the given directory': {'result': [{'entry': {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}}, {'entry': {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}}, {'entry': {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}}], 'type': 'list', 'calls': [['_load_json', ['PATH TMP/registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/registry/notjson.json'], {}], ['_load_json', ['PATH TMP/registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'registry: the default directory': {'result': [{'entry': {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}}, {'entry': {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}}, {'entry': {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}}], 'type': 'list', 'calls': [['switchyard_registry_dir', [], {}], ['_load_json', ['PATH TMP/default-registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/default-registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/default-registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/default-registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/default-registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/default-registry/notjson.json'], {}], ['_load_json', ['PATH TMP/default-registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/default-registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/default-registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/default-registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'registry: a directory that is not there': {'result': [], 'type': 'list', 'calls': [], 'stderr': []},
    'entries: both, a config first for the same slug': {'result': [{'entry': {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}}, {'entry': {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}}, {'entry': {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}}, {'entry': {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}}, {'entry': {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}}, {'entry': {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}}], 'type': 'list', 'calls': [['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_load_json', ['PATH TMP/registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/registry/notjson.json'], {}], ['_load_json', ['PATH TMP/registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'entries: the defaults': {'result': [{'entry': {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/default-configs/alpha.json'}}, {'entry': {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/default-configs/beta.json'}}, {'entry': {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/default-configs/blank.json'}}, {'entry': {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}}, {'entry': {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}}, {'entry': {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/default-configs/p454.json'}}], 'type': 'list', 'calls': [['_project_entries', [None], {}], ['_load_json', ['PATH TMP/default-configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/default-configs/alpha.json'}], ['_load_json', ['PATH TMP/default-configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/default-configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/default-configs/beta.json'}], ['_load_json', ['PATH TMP/default-configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/default-configs/blank.json'}], ['_load_json', ['PATH TMP/default-configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/default-configs/noroles.json'], {}], ['_load_json', ['PATH TMP/default-configs/notjson.json'], {}], ['_load_json', ['PATH TMP/default-configs/notobject.json'], {}], ['_load_json', ['PATH TMP/default-configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/default-configs/p454.json'}], ['_registry_project_entries', [None], {}], ['switchyard_registry_dir', [], {}], ['_load_json', ['PATH TMP/default-registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/default-registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/default-registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/default-registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/default-registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/default-registry/notjson.json'], {}], ['_load_json', ['PATH TMP/default-registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/default-registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/default-configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/default-registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/default-registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'entries: none': {'result': [], 'type': 'list', 'calls': [['_project_entries', ['PATH TMP/missing'], {}], ['_registry_project_entries', ['PATH TMP/missing'], {}]], 'stderr': []},
    'entries: sorted by name, then slug': {'result': [{'entry': {'slug': 'mm', 'name': 'aardvark', 'config_path': 'PATH TMP/configs/mm.json'}}, {'entry': {'slug': 'zz', 'name': 'Aardvark', 'config_path': 'PATH TMP/configs/zz.json'}}, {'entry': {'slug': 'aa', 'name': 'Zebra', 'config_path': 'PATH TMP/configs/aa.json'}}], 'type': 'list', 'calls': [['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/aa.json'], {}], ['_validate_project_slug', ['aa'], {}], ['SwitchyardProjectEntry', [], {'slug': 'aa', 'name': 'Zebra', 'config_path': 'PATH TMP/configs/aa.json'}], ['_load_json', ['PATH TMP/configs/mm.json'], {}], ['_validate_project_slug', ['mm'], {}], ['SwitchyardProjectEntry', [], {'slug': 'mm', 'name': 'aardvark', 'config_path': 'PATH TMP/configs/mm.json'}], ['_load_json', ['PATH TMP/configs/zz.json'], {}], ['_validate_project_slug', ['zz'], {}], ['SwitchyardProjectEntry', [], {'slug': 'zz', 'name': 'Aardvark', 'config_path': 'PATH TMP/configs/zz.json'}], ['_registry_project_entries', ['PATH TMP/missing'], {}]], 'stderr': []},
    'partial: no record': {'result': None, 'type': 'NoneType', 'calls': [['privileged_baseline_plan_path', ['p454'], {}]], 'stderr': []},
    'partial: a record': {'result': 'PATH TMP/privileged/p454/plan.json', 'type': 'PosixPath', 'calls': [['privileged_baseline_plan_path', ['p454'], {}]], 'stderr': []},
    'partial: a symlink': {'result': None, 'type': 'NoneType', 'calls': [['privileged_baseline_plan_path', ['p454'], {}]], 'stderr': []},
    'partial: a directory': {'result': None, 'type': 'NoneType', 'calls': [['privileged_baseline_plan_path', ['p454'], {}]], 'stderr': []},
    'hint: no record': {'result': '', 'type': 'str', 'calls': [['partial_provision_record', ['p454'], {}], ['privileged_baseline_plan_path', ['p454'], {}]], 'stderr': []},
    'hint: a record': {'result': "switchyard: 'p454' is not registered, but root holds a provisioning record for it: its `switchyard new` stopped before registration. Resume it with `sudo switchyard resume-provision p454`.", 'type': 'str', 'calls': [['partial_provision_record', ['p454'], {}], ['privileged_baseline_plan_path', ['p454'], {}]], 'stderr': []},
    'resolve: by slug': {'result': {'entry': {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_load_json', ['PATH TMP/registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/registry/notjson.json'], {}], ['_load_json', ['PATH TMP/registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: by name, any case, spaced': {'result': {'entry': {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_load_json', ['PATH TMP/registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/registry/notjson.json'], {}], ['_load_json', ['PATH TMP/registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: a registered tenant': {'result': {'entry': {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_load_json', ['PATH TMP/registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/registry/notjson.json'], {}], ['_load_json', ['PATH TMP/registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: the defaults': {'result': {'entry': {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/default-configs/p454.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': None, 'registry_dir': None}], ['_project_entries', [None], {}], ['_load_json', ['PATH TMP/default-configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/default-configs/alpha.json'}], ['_load_json', ['PATH TMP/default-configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/default-configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/default-configs/beta.json'}], ['_load_json', ['PATH TMP/default-configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/default-configs/blank.json'}], ['_load_json', ['PATH TMP/default-configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/default-configs/noroles.json'], {}], ['_load_json', ['PATH TMP/default-configs/notjson.json'], {}], ['_load_json', ['PATH TMP/default-configs/notobject.json'], {}], ['_load_json', ['PATH TMP/default-configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/default-configs/p454.json'}], ['_registry_project_entries', [None], {}], ['switchyard_registry_dir', [], {}], ['_load_json', ['PATH TMP/default-registry/badslug.json'], {}], ['_validate_project_slug', ['Bad!'], {}], ['_load_json', ['PATH TMP/default-registry/gamma.json'], {}], ['_validate_project_slug', ['gamma'], {}], ['SwitchyardProjectEntry', [], {'slug': 'gamma', 'name': 'Gamma Works', 'config_path': 'PATH TMP/tenants/gamma.json'}], ['_load_json', ['PATH TMP/default-registry/home.json'], {}], ['_validate_project_slug', ['homed'], {}], ['SwitchyardProjectEntry', [], {'slug': 'homed', 'name': 'homed', 'config_path': 'PATH TMP/home/homed.json'}], ['_load_json', ['PATH TMP/default-registry/noconfig.json'], {}], ['_validate_project_slug', ['noconfig'], {}], ['_load_json', ['PATH TMP/default-registry/noslug.json'], {}], ['_validate_project_slug', [''], {}], ['_load_json', ['PATH TMP/default-registry/notjson.json'], {}], ['_load_json', ['PATH TMP/default-registry/oldschema.json'], {}], ['_load_json', ['PATH TMP/default-registry/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Registered 454', 'config_path': 'PATH TMP/tenants/p454.json'}]], 'stderr': ['warning: switchyard: skipping TMP/default-configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/default-registry/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$', 'warning: switchyard: skipping TMP/default-registry/noslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: empty': {'result': {'raised': 'SystemExit', 'message': 'switchyard: project name cannot be empty'}, 'calls': [], 'stderr': []},
    'resolve: ambiguous': {'result': {'raised': 'SystemExit', 'message': "switchyard: project selector 'two' is ambiguous"}, 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/one.json'], {}], ['_validate_project_slug', ['one'], {}], ['SwitchyardProjectEntry', [], {'slug': 'one', 'name': 'two', 'config_path': 'PATH TMP/configs/one.json'}], ['_load_json', ['PATH TMP/configs/two.json'], {}], ['_validate_project_slug', ['two'], {}], ['SwitchyardProjectEntry', [], {'slug': 'two', 'name': 'two', 'config_path': 'PATH TMP/configs/two.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}]], 'stderr': []},
    'resolve: by a slug the name derives': {'result': {'entry': {'slug': 'x1', 'name': 'Derived Name 454', 'config_path': 'PATH TMP/configs/x1.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/x1.json'], {}], ['_validate_project_slug', ['x1'], {}], ['SwitchyardProjectEntry', [], {'slug': 'x1', 'name': 'Derived Name 454', 'config_path': 'PATH TMP/configs/x1.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Derived Name 454'], {}], ['_slug_from_project_name', ['Derived Name 454'], {}], ['_validate_project_slug', ['derived_name_454'], {}], ['_legacy_dash_slug_from_project_name', ['Derived Name 454'], {}]], 'stderr': []},
    'resolve: by the legacy dashed slug the name derives': {'result': {'entry': {'slug': 'x1', 'name': 'Derived Name 454', 'config_path': 'PATH TMP/configs/x1.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/x1.json'], {}], ['_validate_project_slug', ['x1'], {}], ['SwitchyardProjectEntry', [], {'slug': 'x1', 'name': 'Derived Name 454', 'config_path': 'PATH TMP/configs/x1.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Derived Name 454'], {}], ['_slug_from_project_name', ['Derived Name 454'], {}], ['_validate_project_slug', ['derived_name_454'], {}], ['_legacy_dash_slug_from_project_name', ['Derived Name 454'], {}]], 'stderr': []},
    'resolve: by a derived slug, casefolded': {'result': {'entry': {'slug': 's1', 'name': 'Straße 454', 'config_path': 'PATH TMP/configs/s1.json'}}, 'type': 'SwitchyardProjectEntry', 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/s1.json'], {}], ['_validate_project_slug', ['s1'], {}], ['SwitchyardProjectEntry', [], {'slug': 's1', 'name': 'Straße 454', 'config_path': 'PATH TMP/configs/s1.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Straße 454'], {}], ['_slug_from_project_name', ['Straße 454'], {}], ['_validate_project_slug', ['stra_e_454'], {}], ['_legacy_dash_slug_from_project_name', ['Straße 454'], {}]], 'stderr': []},
    'resolve: a first word that is a project': {'result': {'raised': 'SystemExit', 'message': "switchyard: 'alpha' is a project; did you mean `switchyard alpha`? A bare project name starts or attaches it."}, 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Alpha Team'], {}], ['_slug_from_project_name', ['Alpha Team'], {}], ['_validate_project_slug', ['alpha_team'], {}], ['_legacy_dash_slug_from_project_name', ['Alpha Team'], {}], ['_project_name_selector_slugs', ['beta'], {}], ['_slug_from_project_name', ['beta'], {}], ['_validate_project_slug', ['beta'], {}], ['_legacy_dash_slug_from_project_name', ['beta'], {}], ['_project_name_selector_slugs', ['blank'], {}], ['_slug_from_project_name', ['blank'], {}], ['_validate_project_slug', ['blank'], {}], ['_legacy_dash_slug_from_project_name', ['blank'], {}], ['_project_name_selector_slugs', ['Project 454'], {}], ['_slug_from_project_name', ['Project 454'], {}], ['_validate_project_slug', ['project_454'], {}], ['_legacy_dash_slug_from_project_name', ['Project 454'], {}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: a first word that is no project': {'result': {'raised': 'SystemExit', 'message': "switchyard: unknown project 'nobody here'"}, 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Alpha Team'], {}], ['_slug_from_project_name', ['Alpha Team'], {}], ['_validate_project_slug', ['alpha_team'], {}], ['_legacy_dash_slug_from_project_name', ['Alpha Team'], {}], ['_project_name_selector_slugs', ['beta'], {}], ['_slug_from_project_name', ['beta'], {}], ['_validate_project_slug', ['beta'], {}], ['_legacy_dash_slug_from_project_name', ['beta'], {}], ['_project_name_selector_slugs', ['blank'], {}], ['_slug_from_project_name', ['blank'], {}], ['_validate_project_slug', ['blank'], {}], ['_legacy_dash_slug_from_project_name', ['blank'], {}], ['_project_name_selector_slugs', ['Project 454'], {}], ['_slug_from_project_name', ['Project 454'], {}], ['_validate_project_slug', ['project_454'], {}], ['_legacy_dash_slug_from_project_name', ['Project 454'], {}], ['_resume_provision_hint', ['nobody here'], {}], ['partial_provision_record', ['nobody here'], {}], ['privileged_baseline_plan_path', ['nobody here'], {}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: unknown, with a provisioning record': {'result': {'raised': 'SystemExit', 'message': "switchyard: 'p999' is not registered, but root holds a provisioning record for it: its `switchyard new` stopped before registration. Resume it with `sudo switchyard resume-provision p999`."}, 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Alpha Team'], {}], ['_slug_from_project_name', ['Alpha Team'], {}], ['_validate_project_slug', ['alpha_team'], {}], ['_legacy_dash_slug_from_project_name', ['Alpha Team'], {}], ['_project_name_selector_slugs', ['beta'], {}], ['_slug_from_project_name', ['beta'], {}], ['_validate_project_slug', ['beta'], {}], ['_legacy_dash_slug_from_project_name', ['beta'], {}], ['_project_name_selector_slugs', ['blank'], {}], ['_slug_from_project_name', ['blank'], {}], ['_validate_project_slug', ['blank'], {}], ['_legacy_dash_slug_from_project_name', ['blank'], {}], ['_project_name_selector_slugs', ['Project 454'], {}], ['_slug_from_project_name', ['Project 454'], {}], ['_validate_project_slug', ['project_454'], {}], ['_legacy_dash_slug_from_project_name', ['Project 454'], {}], ['_resume_provision_hint', ['p999'], {}], ['partial_provision_record', ['p999'], {}], ['privileged_baseline_plan_path', ['p999'], {}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
    'resolve: unknown': {'result': {'raised': 'SystemExit', 'message': "switchyard: unknown project 'p999'"}, 'calls': [['_switchyard_entries', [], {'config_dir': 'PATH TMP/configs', 'registry_dir': 'PATH TMP/registry'}], ['_project_entries', ['PATH TMP/configs'], {}], ['_load_json', ['PATH TMP/configs/alpha.json'], {}], ['_validate_project_slug', ['alpha'], {}], ['SwitchyardProjectEntry', [], {'slug': 'alpha', 'name': 'Alpha Team', 'config_path': 'PATH TMP/configs/alpha.json'}], ['_load_json', ['PATH TMP/configs/badslug.json'], {}], ['_validate_project_slug', ['Bad Slug!'], {}], ['_load_json', ['PATH TMP/configs/beta.json'], {}], ['_validate_project_slug', ['beta'], {}], ['SwitchyardProjectEntry', [], {'slug': 'beta', 'name': 'beta', 'config_path': 'PATH TMP/configs/beta.json'}], ['_load_json', ['PATH TMP/configs/blank.json'], {}], ['_validate_project_slug', ['blank'], {}], ['SwitchyardProjectEntry', [], {'slug': 'blank', 'name': 'blank', 'config_path': 'PATH TMP/configs/blank.json'}], ['_load_json', ['PATH TMP/configs/emptyroles.json'], {}], ['_load_json', ['PATH TMP/configs/noroles.json'], {}], ['_load_json', ['PATH TMP/configs/notjson.json'], {}], ['_load_json', ['PATH TMP/configs/notobject.json'], {}], ['_load_json', ['PATH TMP/configs/p454.json'], {}], ['_validate_project_slug', ['p454'], {}], ['SwitchyardProjectEntry', [], {'slug': 'p454', 'name': 'Project 454', 'config_path': 'PATH TMP/configs/p454.json'}], ['_registry_project_entries', ['PATH TMP/registry'], {}], ['_project_name_selector_slugs', ['Alpha Team'], {}], ['_slug_from_project_name', ['Alpha Team'], {}], ['_validate_project_slug', ['alpha_team'], {}], ['_legacy_dash_slug_from_project_name', ['Alpha Team'], {}], ['_project_name_selector_slugs', ['beta'], {}], ['_slug_from_project_name', ['beta'], {}], ['_validate_project_slug', ['beta'], {}], ['_legacy_dash_slug_from_project_name', ['beta'], {}], ['_project_name_selector_slugs', ['blank'], {}], ['_slug_from_project_name', ['blank'], {}], ['_validate_project_slug', ['blank'], {}], ['_legacy_dash_slug_from_project_name', ['blank'], {}], ['_project_name_selector_slugs', ['Project 454'], {}], ['_slug_from_project_name', ['Project 454'], {}], ['_validate_project_slug', ['project_454'], {}], ['_legacy_dash_slug_from_project_name', ['Project 454'], {}], ['_resume_provision_hint', ['p999'], {}], ['partial_provision_record', ['p999'], {}], ['privileged_baseline_plan_path', ['p999'], {}]], 'stderr': ['warning: switchyard: skipping TMP/configs/badslug.json: switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$']},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold454.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the seven on synthetic launcher configs and registration records in a fresh test-owned tree, which
# is also `$HOME`, and records the answer (entries as their fields, paths relative to the tree), what it wrote to
# stderr, and, in order, every call it made on the launcher. The config directory, the registry directory and the
# privileged baseline plan path are stand-ins on the launcher pointing into the tree, so no real tenant record, registry
# or privileged provisioning record is read. The JSON reader, the slug validator and derivations and the entry type are
# the launcher's own, recorded there and passed through.
REG = "switchyard.project-registry.v1"
ROLES = [{"role": "main"}]
CONFIGS = {
    "p454.json": {"project": "p454", "project_name": "Project 454", "roles": ROLES},
    "alpha.json": {"project": "alpha", "name": "Alpha Team", "roles": ROLES},
    "beta.json": {"roles": ROLES},
    "blank.json": {"project": "blank", "project_name": "   ", "roles": ROLES},
    "noroles.json": {"project": "noroles"},
    "emptyroles.json": {"project": "emptyroles", "roles": []},
    "badslug.json": {"project": "Bad Slug!", "roles": ROLES},
    "notjson.json": "{not json",
    "notobject.json": "[1, 2]",
    "notes.txt": {"project": "notes", "roles": ROLES},
}
REGISTRY = {
    "gamma.json": {"schema": REG, "slug": "gamma", "name": "Gamma Works", "config_path": "@/tenants/gamma.json"},
    "p454.json": {"schema": REG, "slug": "p454", "name": "Registered 454", "config_path": "@/tenants/p454.json"},
    "home.json": {"schema": REG, "slug": "homed", "config_path": "~/homed.json"},
    "oldschema.json": {"schema": "switchyard.project-registry.v0", "slug": "old", "config_path": "@/old.json"},
    "noslug.json": {"schema": REG, "name": "No Slug", "config_path": "@/x.json"},
    "badslug.json": {"schema": REG, "slug": "Bad!", "config_path": "@/x.json"},
    "noconfig.json": {"schema": REG, "slug": "noconfig", "config_path": "  "},
    "notjson.json": "{",
}
AMBIGUOUS = {"one.json": {"project": "one", "project_name": "two", "roles": ROLES}, "two.json": {"project": "two", "roles": ROLES}}
DERIVED = {"x1.json": {"project": "x1", "project_name": "Derived Name 454", "roles": ROLES}}
STRASSE = {"s1.json": {"project": "s1", "project_name": "Straße 454", "roles": ROLES}}
SORTED = {"zz.json": {"project": "zz", "project_name": "Aardvark", "roles": ROLES}, "aa.json": {"project": "aa", "project_name": "Zebra", "roles": ROLES},
          "mm.json": {"project": "mm", "project_name": "aardvark", "roles": ROLES}}
CASES = {
    "selector slugs: a name": {"call": "_project_name_selector_slugs", "args": ["My Project Name"]},
    "selector slugs: one word": {"call": "_project_name_selector_slugs", "args": ["a"]},
    "selector slugs: nothing derivable": {"call": "_project_name_selector_slugs", "args": ["!!!"]},
    "selector slugs: punctuation": {"call": "_project_name_selector_slugs", "args": ["Project 454, Ltd."]},
    "selector slugs: casefolded, not only lower-cased": {"call": "_project_name_selector_slugs", "args": ["Straße 454"]},
    "configs: the given directory": {"call": "_project_entries", "configs": CONFIGS, "args": ["@/configs"]},
    "configs: the default directory": {"call": "_project_entries", "default_configs": CONFIGS, "args": []},
    "configs: a directory that is not there": {"call": "_project_entries", "args": ["@/missing"]},
    "configs: a file, not a directory": {"call": "_project_entries", "configs": CONFIGS, "args": ["@/configs/p454.json"]},
    "registry: the given directory": {"call": "_registry_project_entries", "registry": REGISTRY, "args": ["@/registry"]},
    "registry: the default directory": {"call": "_registry_project_entries", "default_registry": REGISTRY, "args": []},
    "registry: a directory that is not there": {"call": "_registry_project_entries", "args": ["@/missing"]},
    "entries: both, a config first for the same slug": {"call": "_switchyard_entries", "configs": CONFIGS, "registry": REGISTRY,
                                                        "kwargs": {"config_dir": "@/configs", "registry_dir": "@/registry"}},
    "entries: the defaults": {"call": "_switchyard_entries", "default_configs": CONFIGS, "default_registry": REGISTRY, "kwargs": {}},
    "entries: none": {"call": "_switchyard_entries", "kwargs": {"config_dir": "@/missing", "registry_dir": "@/missing"}},
    "entries: sorted by name, then slug": {"call": "_switchyard_entries", "configs": SORTED, "kwargs": {"config_dir": "@/configs", "registry_dir": "@/missing"}},
    "partial: no record": {"call": "partial_provision_record", "args": ["p454"]},
    "partial: a record": {"call": "partial_provision_record", "args": ["p454"], "record": "file"},
    "partial: a symlink": {"call": "partial_provision_record", "args": ["p454"], "record": "symlink"},
    "partial: a directory": {"call": "partial_provision_record", "args": ["p454"], "record": "directory"},
    "hint: no record": {"call": "_resume_provision_hint", "args": ["p454"]},
    "hint: a record": {"call": "_resume_provision_hint", "args": ["p454"], "record": "file"},
    "resolve: by slug": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "registry": REGISTRY, "args": ["alpha"]},
    "resolve: by name, any case, spaced": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "registry": REGISTRY, "args": ["  alpha team "]},
    "resolve: a registered tenant": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "registry": REGISTRY, "args": ["Gamma Works"]},
    "resolve: the defaults": {"call": "_resolve_switchyard_project", "default_configs": CONFIGS, "default_registry": REGISTRY, "args": ["p454"], "defaults": True},
    "resolve: empty": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "args": ["   "]},
    "resolve: ambiguous": {"call": "_resolve_switchyard_project", "configs": AMBIGUOUS, "args": ["two"]},
    "resolve: by a slug the name derives": {"call": "_resolve_switchyard_project", "configs": DERIVED, "args": ["derived_name_454"]},
    "resolve: by the legacy dashed slug the name derives": {"call": "_resolve_switchyard_project", "configs": DERIVED, "args": ["derived-name-454"]},
    "resolve: by a derived slug, casefolded": {"call": "_resolve_switchyard_project", "configs": STRASSE, "args": ["strasse-454"]},
    "resolve: a first word that is a project": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "args": ["alpha start"]},
    "resolve: a first word that is no project": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "args": ["nobody here"]},
    "resolve: unknown, with a provisioning record": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "args": [" p999 "], "record": "file", "record_slug": "p999"},
    "resolve: unknown": {"call": "_resolve_switchyard_project", "configs": CONFIGS, "args": ["p999"]},
}
FUNCTIONS = ("_project_name_selector_slugs", "_project_entries", "_registry_project_entries", "_switchyard_entries", "partial_provision_record",
             "_resume_provision_hint", "_resolve_switchyard_project")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition, in a fresh test-owned tree; the directories and the record path stand-ins on `t`."""
    import contextlib, dataclasses, io, json, os, shutil, tempfile
    from pathlib import Path as _P
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd454-")).resolve()

    def norm(value):
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"entry": {f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, set):
            return {"set": sorted(norm(v) for v in value)}
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(str(tmp), "TMP")
        if isinstance(value, str):
            return value.replace(str(tmp), "TMP")
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def passthrough(name):
        def f(*args, **kwargs):
            note(name, *args, **kwargs)
            return saved[name](*args, **kwargs)
        return f

    def write(directory, files):
        directory.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            text = body if isinstance(body, str) else json.dumps(body)
            (directory / name).write_text(text.replace("@/", str(tmp) + "/"), encoding="utf-8")

    passed = ("_load_json", "_validate_project_slug", "_slug_from_project_name", "_legacy_dash_slug_from_project_name", "SwitchyardProjectEntry",
              *FUNCTIONS)
    stood = ("switchyard_registry_dir", "privileged_baseline_plan_path", "DEFAULT_CONFIG_DIR")
    saved = {n: getattr(t, n) for n in (*passed, *stood)}
    saved_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(tmp / "home")
        write(tmp / "configs", spec.get("configs", {}))
        write(tmp / "default-configs", spec.get("default_configs", {}))
        write(tmp / "registry", spec.get("registry", {}))
        write(tmp / "default-registry", spec.get("default_registry", {}))
        (tmp / "configs" / "subdir.json").mkdir()
        slug = spec.get("record_slug", "p454")
        record = tmp / "privileged" / slug / "plan.json"
        record.parent.mkdir(parents=True)
        if spec.get("record") == "file":
            record.write_text("{}")
        elif spec.get("record") == "symlink":
            (tmp / "elsewhere.json").write_text("{}")
            record.symlink_to(tmp / "elsewhere.json")
        elif spec.get("record") == "directory":
            record.mkdir()
        for n in passed:
            setattr(t, n, passthrough(n))
        t.DEFAULT_CONFIG_DIR = tmp / "default-configs"
        t.switchyard_registry_dir = lambda: note("switchyard_registry_dir") or tmp / "default-registry"
        t.privileged_baseline_plan_path = lambda project: note("privileged_baseline_plan_path", project) or tmp / "privileged" / project / "plan.json"
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        args = [_P(a.replace("@/", str(tmp) + "/")) if isinstance(a, str) and a.startswith("@/") else a for a in spec.get("args", [])]
        # A resolution reads the case's own directories, unless the case is about the defaults.
        given = spec.get("kwargs", {"config_dir": "@/configs", "registry_dir": "@/registry"} if spec["call"] == "_resolve_switchyard_project" and not spec.get("defaults") else {})
        kwargs = {k: _P(v.replace("@/", str(tmp) + "/")) for k, v in given.items()}
        said = io.StringIO()
        try:
            with contextlib.redirect_stderr(said):
                got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": calls, "stderr": norm(said.getvalue()).splitlines()}
        return {"result": norm(got), "type": type(got).__name__, "calls": calls, "stderr": norm(said.getvalue()).splitlines()}
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        for n, v in saved.items():
            setattr(t, n, v)
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
    result = python("import sys, scripts.project_resolution as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.project_resolution", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.project_resolution")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.project_resolution as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"[[v.default for v in inspect.signature(getattr(m, n)).parameters.values() if v.default is not inspect.Parameter.empty] for n in {MOVED!r}], "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'SwitchyardProjectEntry', 'DEFAULT_CONFIG_DIR', 'switchyard_registry_dir', "
                        "'privileged_baseline_plan_path', '_load_json', '_validate_project_slug')))")
        check(result.stdout.strip() == "True [[], [None], [None], [None, None], [], [], [None, None]] True",
              f"{' then '.join(order)}: one object each; every default None, as before; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import json as _json
    import pathlib
    import stat as _stat
    check(m.json is _json and m.os is os and m.stat is _stat and m.sys is sys and m.Path is pathlib.Path and m.TYPE_CHECKING is False
          and t.json is m.json and t.os is m.os and t.sys is m.sys and t.Path is m.Path,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "project_resolution.py").read_text(encoding="utf-8"))
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
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported first thing (after its docstring), and nothing else imported: {imports}")
        skip = {id(y) for x in ast.walk(node) for part in ([x.returns, *(a.annotation for a in x.args.args + x.args.kwonlyargs)] if isinstance(x, ast.FunctionDef)
                                                           else [x.annotation] if isinstance(x, ast.AnnAssign) else [])
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    # The partial-provision record is recognised without following anything: a regular file, stat'ed with no follow.
    partial = next(n for n in tree.body if getattr(n, "name", None) == "partial_provision_record")
    stats = [ast.unparse(x) for x in ast.walk(partial) if isinstance(x, ast.Call) and ast.unparse(x.func) in ("os.stat", "stat.S_ISREG")]
    check(stats == ["os.stat(baseline, follow_symlinks=False)", "stat.S_ISREG(info.st_mode)"],
          f"partial_provision_record: os.stat with follow_symlinks=False, and a regular file only: {stats}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import json", "import os", "import stat", "import sys", "from pathlib import Path", "from typing import TYPE_CHECKING"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import SwitchyardProjectEntry"],
          f"the standard library, and the entry type for annotations only: {top} {tc}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the seven, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.project_resolution"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the seven, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"_resolve_launcher_project_config", *DISPATCH} <= defined | exported,
          "the launcher defines none of them, and keeps its callers, _resolve_launcher_project_config and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"the launcher's own callers name them exactly as often as before, by the re-exported names: {uses}")
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
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads them through the launcher, as often as before: {got} {bare}")


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

    def slugs(label):
        return [e["entry"]["slug"] for e in result(label)]

    def steps(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    def refusal(label):
        r = result(label)
        return r["message"] if isinstance(r, dict) and r.get("raised") == "SystemExit" else None

    BAD = "switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$"
    check(result("selector slugs: a name") == {"set": ["my-project-name", "my_project_name"]} and result("selector slugs: nothing derivable") == {"set": []}
          and result("selector slugs: one word") == {"set": ["a"]} and result("selector slugs: casefolded, not only lower-cased") == {"set": ["stra_e_454", "strasse-454"]},
          "a name's selector slugs are the underscore and legacy dashed slugs it derives, casefolded; one that cannot be derived is skipped")
    check(slugs("configs: the given directory") == ["alpha", "beta", "blank", "p454"]
          and [e["entry"]["name"] for e in result("configs: the given directory")] == ["Alpha Team", "beta", "blank", "Project 454"]
          and GOLDEN["configs: the given directory"]["stderr"] == [f"warning: switchyard: skipping TMP/configs/badslug.json: {BAD}"]
          and result("configs: the default directory")[0]["entry"]["config_path"] == "PATH TMP/default-configs/alpha.json"
          and result("configs: a directory that is not there") == [] and result("configs: a file, not a directory") == [],
          "launcher configs: only .json files with a non-empty roles list; the slug from project or the file name, the name from project_name, "
          "name or the slug; a bad slug skipped with a warning; unreadable ones and a missing directory skipped silently")
    check(slugs("registry: the given directory") == ["gamma", "homed", "p454"]
          and [e["entry"]["config_path"] for e in result("registry: the given directory")] == ["PATH TMP/tenants/gamma.json", "PATH TMP/home/homed.json", "PATH TMP/tenants/p454.json"]
          and GOLDEN["registry: the given directory"]["stderr"] == [f"warning: switchyard: skipping TMP/registry/badslug.json: {BAD}",
                                                                    f"warning: switchyard: skipping TMP/registry/noslug.json: {BAD}"]
          and "switchyard_registry_dir" in steps("registry: the default directory") and "switchyard_registry_dir" not in steps("registry: the given directory")
          and result("registry: a directory that is not there") == [],
          "registration records: only the exact schema, a valid slug (a missing or bad one skipped with a warning) and a config path (its ~ expanded); the default directory only when none is given")
    both = result("entries: both, a config first for the same slug")
    check([e["entry"]["slug"] for e in both] == ["alpha", "beta", "blank", "gamma", "homed", "p454"]
          and next(e for e in both if e["entry"]["slug"] == "p454")["entry"]["config_path"] == "PATH TMP/configs/p454.json"
          and result("entries: none") == [] and [e["entry"]["slug"] for e in result("entries: sorted by name, then slug")] == ["mm", "zz", "aa"],
          "the two merged, the launcher config kept over a registration record of the same slug, sorted by name then slug")
    check(result("partial: no record") is None and result("partial: a record") == "PATH TMP/privileged/p454/plan.json"
          and result("partial: a symlink") is None and result("partial: a directory") is None
          and steps("partial: a record") == ["privileged_baseline_plan_path"],
          "a partial provision is root's baseline plan, a regular file, never followed through a symlink")
    check(result("hint: no record") == "" and result("hint: a record").startswith("switchyard: 'p454' is not registered, but root holds a provisioning record for it")
          and result("hint: a record").endswith("Resume it with `sudo switchyard resume-provision p454`."),
          "the resume hint names the one supported way back, and only when root holds the record")
    check(result("resolve: by slug")["entry"]["slug"] == "alpha" and result("resolve: by name, any case, spaced")["entry"]["slug"] == "alpha"
          and result("resolve: a registered tenant")["entry"]["config_path"] == "PATH TMP/tenants/gamma.json"
          and result("resolve: the defaults")["entry"]["config_path"] == "PATH TMP/default-configs/p454.json"
          and result("resolve: by a slug the name derives")["entry"]["slug"] == result("resolve: by the legacy dashed slug the name derives")["entry"]["slug"] == "x1"
          and result("resolve: by a derived slug, casefolded")["entry"]["slug"] == "s1",
          "a selection resolves by slug or name, any case, stripped; else by a slug the name derives")
    check(refusal("resolve: empty") == "switchyard: project name cannot be empty"
          and refusal("resolve: ambiguous") == "switchyard: project selector 'two' is ambiguous"
          and refusal("resolve: a first word that is a project") == "switchyard: 'alpha' is a project; did you mean `switchyard alpha`? A bare project name starts or attaches it."
          and refusal("resolve: a first word that is no project") == "switchyard: unknown project 'nobody here'"
          and refusal("resolve: unknown, with a provisioning record").startswith("switchyard: 'p999' is not registered, but root holds a provisioning record")
          and refusal("resolve: unknown") == "switchyard: unknown project 'p999'"
          and "_resume_provision_hint" in steps("resolve: unknown") and "_resume_provision_hint" not in steps("resolve: by slug"),
          "refusals, word for word: empty, ambiguous, a first word that is a project, the resume hint, unknown")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the seven read on the launcher, the seven themselves included, is a recorder there; the config
    # directory and the registry schema are rebound or read there.
    names = {name for reads in SEAMS.values() for name in reads}
    constants = {name for name in names if not callable(getattr(t, name)) or isinstance(getattr(t, name), (str, Path))}
    check(constants == {"DEFAULT_CONFIG_DIR", "SWITCHYARD_REGISTRY_SCHEMA"} and names - constants <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - constants - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"project_resolution_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
