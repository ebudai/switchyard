#!/usr/bin/env python3
"""SYRD-406: a role's visibility update and detach, against the launcher they came out of.

`tmux_detach_clients_args`, `_raw_role_for_update`, `_write_role_visibility`
and `detach_role_from_slot` moved unchanged into `scripts/role_visibility.py`,
and the launcher re-exports all four. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; `detach_role_from_slot`'s
  `runner=subprocess.run` and `print_func=print` are the defaults bound when
  it is defined, and `_write_role_visibility` still takes no default runner;
  `RoleConfig` and `ProjectConfig` are annotations only.
- **Seams (rule 24):** the config reader and writer, the owner repair, the
  config loader, the role lookup, the tmux argv and the moved siblings are read
  through the launcher as often as before, so a patch there reaches each of
  them -- every case below runs with all nine standing in (or wrapped) on the
  launcher.
- **Readers:** `main` dispatches `pane detach-role` by its own global, and
  `scripts/role_pane_entry.py`, which reads `launcher._write_role_visibility`
  when it runs, is byte-identical to the baseline.
- **Behaviour is the baseline's:** the raw-role lookup and its two refusals;
  detached (no slot) or visible (a slot, required); the atomic write, its
  OSError (and only an OSError) reported, then the owner repair with the
  caller's runner, then the reload; an already-detached or unknown role; the
  headless worker: `has-session` first, `detach-client` only for a live
  session, a failed detach reported and still 0, nothing ever killed.
  `GOLDEN` below was produced by running the BASELINE launcher's own
  functions over the very cases embedded here (`gold406.py`), not typed.

Nothing touches tmux, a tenant or a user: every config is an owned temporary
file, every runner a recorder, and the real `subprocess.run`/`Popen`,
`os.kill`, the account lookups, `os.geteuid`/`chown`, and every path outside
the temporary root are refused for each case.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import role_visibility as m  # noqa: E402

CHECKS = 0
MOVED = ("tmux_detach_clients_args", "_raw_role_for_update", "_write_role_visibility", "detach_role_from_slot")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_write_role_visibility': {'_load_json': 1, '_raw_role_for_update': 1, '_write_json_atomic': 1, 'ensure_owner_file': 1, 'load_project_config': 1},
    'detach_role_from_slot': {'_role_by_name': 1, '_write_role_visibility': 1, 'tmux_detach_clients_args': 1, 'tmux_has_session_args': 1},
}
#: Measured on the baseline launcher: every function outside the four that calls one of them, and by what name.
DISPATCH = {'main': {'detach_role_from_slot': 1}}
#: The git blob of `scripts/role_pane_entry.py` at the baseline (read with `git rev-parse`, not typed).
ROLE_PANE_ENTRY_BLOB = '8235fe5ce609937c6c38cd7ffd184d7aa82a10da'
#: The BASELINE's own behaviour for the cases below (`gold406.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'write:detached': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:visible in slot 2': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 2, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': False}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": false,\n      "role": "worker7",\n      "slot": 2,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:visible in slot 0': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 0, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': False}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": false,\n      "role": "worker7",\n      "slot": 0,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:visible without a slot': {'answer': {'raised': 'ValueError', 'message': 'visible role update requires slot'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:a role already without a slot': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'detached': True, 'role': 'worker7', 'slot': None, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'detached': True, 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:a whitespace-padded name': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': '  worker7 ', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': '  worker7 ', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "  worker7 ",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:non-dict entries first': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [['worker7', 7, None, ['worker7'], {'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': ['worker7', 7, None, ['worker7'], {'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    "worker7",\n    7,\n    null,\n    [\n      "worker7"\n    ],\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:an entry whose role is empty': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'role': None}, {'role': ''}, {'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'role': None}, {'role': ''}, {'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "role": null\n    },\n    {\n      "role": ""\n    },\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:two entries with the name': {'answer': 'RELOADED', 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 5, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}, {'cli': ['codex'], 'role': 'worker7', 'slot': 5, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 5,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:the role missing': {'answer': {'raised': 'SystemExit', 'message': "unknown role 'worker7' in launcher config"}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}], 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:roles an object': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [{'worker7': {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}}, 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": {\n    "worker7": {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  }\n}\n', 'left': ['porter.json']},
    'write:roles absent': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [None, 'worker7'], {}]], 'file': '{\n  "project": "porter"\n}\n', 'left': ['porter.json']},
    'write:roles a string': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', ['worker7', 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": "worker7"\n}\n', 'left': ['porter.json']},
    'write:roles a number': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [1, 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": 1\n}\n', 'left': ['porter.json']},
    'write:the config not an object': {'answer': {'raised': 'SystemExit', 'message': '<TMP>/porter.json must contain a JSON object'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}]], 'file': '[1, 2]\n', 'left': ['porter.json']},
    'write:the config not JSON': {'answer': {'raised': 'JSONDecodeError', 'message': 'Expecting property name enclosed in double quotes: line 2 column 1 (char 2)'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}]], 'file': '{\n', 'left': ['porter.json']},
    'write:the write refused': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot update launcher config <TMP>/porter.json: permission denied'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:the disk full': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot update launcher config <TMP>/porter.json: No space left on device'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:a write failure that is not an OSError': {'answer': {'raised': 'RuntimeError', 'message': 'encoder broke'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:the owner repair refuses': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: failed to assign generated file (owner stand-in)'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'write:visible, the owner repair refuses': {'answer': {'raised': 'SystemExit', 'message': 'owner refused'}, 'said': [], 'stdout': '', 'calls': [['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 3, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': False}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'ROLE_RUNNER'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": false,\n      "role": "worker7",\n      "slot": 3,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:already detached': {'answer': 0, 'said': ['team-launcher: role worker7 is already detached'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['say']], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:an unknown role': {'answer': {'raised': 'SystemExit', 'message': "unknown role 'ghost' in project porter"}, 'said': [], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'ghost'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:no live session': {'answer': 0, 'said': ['team-launcher: detached role worker7 from slot 1; tmux session remains headless'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}], ['say'], ['tmux_has_session_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:has-session exits 2': {'answer': 0, 'said': ['team-launcher: detached role worker7 from slot 1; tmux session remains headless'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}], ['say'], ['tmux_has_session_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:a live session, detached': {'answer': 0, 'said': ['team-launcher: detached role worker7 from slot 1; tmux session remains headless'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}], ['say'], ['tmux_has_session_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_detach_clients_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'detach-client', '-s', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:a live session, the detach fails': {'answer': 0, 'said': ['team-launcher: detached role worker7 from slot 1; tmux session remains headless', 'team-launcher: no live tmux client detached for worker7; session remains configured headless'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}], ['say'], ['tmux_has_session_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_detach_clients_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'detach-client', '-s', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['say']], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:a live session, the detach exits 255': {'answer': 0, 'said': ['team-launcher: detached role worker7 from slot 1; tmux session remains headless', 'team-launcher: no live tmux client detached for worker7; session remains configured headless'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}], ['say'], ['tmux_has_session_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_detach_clients_args', ['ROLE worker7 slot=1 detached=False'], {}], ['runner', ['tmux', 'detach-client', '-s', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['say']], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:a role with no slot': {'answer': 0, 'said': ['team-launcher: detached role worker7 from slot None; tmux session remains headless'], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=None detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}], ['load_project_config', ['porter', '<TMP>/porter.json'], {}], ['say'], ['tmux_has_session_args', ['ROLE worker7 slot=None detached=False'], {}], ['runner', ['tmux', 'has-session', '-t', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}], ['tmux_detach_clients_args', ['ROLE worker7 slot=None detached=False'], {}], ['runner', ['tmux', 'detach-client', '-s', 'porter-worker7'], {'stderr': 'DEVNULL', 'stdout': 'DEVNULL'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:the write refused': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot update launcher config <TMP>/porter.json: permission denied'}, 'said': [], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:the owner repair refuses': {'answer': {'raised': 'SystemExit', 'message': 'owner refused'}, 'said': [], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}], ['ensure_owner_file', ['CONFIG', '<TMP>/porter.json'], {'runner': 'RUNNER'}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "detached": true,\n      "role": "worker7",\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:the role missing from the file': {'answer': {'raised': 'SystemExit', 'message': "unknown role 'worker7' in launcher config"}, 'said': [], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'RUNNER', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}], 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:already detached, the defaults': {'answer': 0, 'said': [], 'stdout': 'team-launcher: role worker7 is already detached\n', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:an unknown role, the defaults': {'answer': {'raised': 'SystemExit', 'message': "unknown role 'ghost' in project porter"}, 'said': [], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'ghost'], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'detach:the write refused, the defaults': {'answer': {'raised': 'SystemExit', 'message': 'team-launcher: cannot update launcher config <TMP>/porter.json: permission denied'}, 'said': [], 'stdout': '', 'calls': [['_role_by_name', ['CONFIG', 'worker7'], {}], ['_write_role_visibility', ['CONFIG'], {'config_path': '<TMP>/porter.json', 'detached': True, 'role': 'ROLE worker7 slot=1 detached=False', 'runner': 'subprocess.run', 'slot': None}], ['_load_json', ['<TMP>/porter.json'], {}], ['_raw_role_for_update', [[{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7'}], 'worker7'], {}], ['_write_json_atomic', ['<TMP>/porter.json', {'project': 'porter', 'roles': [{'cli': ['claude'], 'role': 'director', 'slot': 0, 'target': 'porter-director:0.0', 'tmux_session': 'porter-director'}, {'cli': ['codex'], 'role': 'worker7', 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'detached': True}]}], {}]], 'file': '{\n  "project": "porter",\n  "roles": [\n    {\n      "cli": [\n        "claude"\n      ],\n      "role": "director",\n      "slot": 0,\n      "target": "porter-director:0.0",\n      "tmux_session": "porter-director"\n    },\n    {\n      "cli": [\n        "codex"\n      ],\n      "role": "worker7",\n      "slot": 1,\n      "target": "porter-worker7:0.0",\n      "tmux_session": "porter-worker7"\n    }\n  ]\n}\n', 'left': ['porter.json']},
    'raw:a list': {'role': 'worker7', 'slot': 1, 'target': 'porter-worker7:0.0', 'tmux_session': 'porter-worker7', 'cli': ['codex']},
    'raw:an object': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'},
    'raw:null': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'},
    'raw:a string': {'raised': 'SystemExit', 'message': 'team-launcher: launcher config roles must be a JSON list'},
    'argv:detach clients': ['tmux', 'detach-client', '-s', 'porter-worker7'],
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold406.py` (which ran them on the baseline) ------------------------------------
ROWS = [{"role": "director", "slot": 0, "target": "porter-director:0.0", "tmux_session": "porter-director", "cli": ["claude"]},
        {"role": "worker7", "slot": 1, "target": "porter-worker7:0.0", "tmux_session": "porter-worker7", "cli": ["codex"]}]
DIRECTOR, WORKER = ROWS


def rows(*extra: object) -> list:
    return json.loads(json.dumps([*extra, *ROWS]))


WRITE_CASES = {
    "detached": {"detached": True},
    "visible in slot 2": {"detached": False, "slot": 2},
    "visible in slot 0": {"detached": False, "slot": 0},
    "visible without a slot": {"detached": False, "slot": None},
    "a role already without a slot": {"raw": {"project": "porter", "roles": [DIRECTOR, {**WORKER, "slot": None, "detached": True}]}},
    "a whitespace-padded name": {"raw": {"project": "porter", "roles": [DIRECTOR, {**WORKER, "role": "  worker7 "}]}},
    "non-dict entries first": {"raw": {"project": "porter", "roles": ["worker7", 7, None, ["worker7"], *ROWS]}},
    "an entry whose role is empty": {"raw": {"project": "porter", "roles": [{"role": None}, {"role": ""}, *ROWS]}},
    "two entries with the name": {"raw": {"project": "porter", "roles": [*ROWS, {**WORKER, "slot": 5}]}},
    "the role missing": {"raw": {"project": "porter", "roles": [DIRECTOR]}},
    "roles an object": {"raw": {"project": "porter", "roles": {"worker7": WORKER}}},
    "roles absent": {"raw": {"project": "porter"}},
    "roles a string": {"raw": {"project": "porter", "roles": "worker7"}},
    "roles a number": {"raw": {"project": "porter", "roles": 1}},
    "the config not an object": {"text": "[1, 2]\n"},
    "the config not JSON": {"text": "{\n"},
    "the write refused": {"write_error": ("PermissionError", "permission denied")},
    "the disk full": {"write_error": ("OSError", "No space left on device")},
    "a write failure that is not an OSError": {"write_error": ("RuntimeError", "encoder broke")},
    "the owner repair refuses": {"owner_error": "team-launcher: failed to assign generated file (owner stand-in)"},
    "visible, the owner repair refuses": {"detached": False, "slot": 3, "owner_error": "owner refused"},
}
DETACH_CASES = {
    "already detached": {"role_detached": True},
    "an unknown role": {"role_name": "ghost"},
    "no live session": {"has": 1},
    "has-session exits 2": {"has": 2},
    "a live session, detached": {"has": 0, "detach": 0},
    "a live session, the detach fails": {"has": 0, "detach": 1},
    "a live session, the detach exits 255": {"has": 0, "detach": 255},
    "a role with no slot": {"role_slot": None, "has": 0, "detach": 0},
    "the write refused": {"write_error": ("PermissionError", "permission denied")},
    "the owner repair refuses": {"owner_error": "owner refused"},
    "the role missing from the file": {"raw": {"project": "porter", "roles": [DIRECTOR]}},
    "already detached, the defaults": {"role_detached": True, "defaults": True},
    "an unknown role, the defaults": {"role_name": "ghost", "defaults": True},
    # The default runner reaches the update, whose write fails before any process could be started with it.
    "the write refused, the defaults": {"write_error": ("PermissionError", "permission denied"), "defaults": True},
}
ERRORS = {"PermissionError": PermissionError, "OSError": OSError, "RuntimeError": RuntimeError}


class Reloaded:
    """What the reload stand-in answers."""


def ROLE_RUNNER(*args: object, **kwargs: object) -> object:
    raise AssertionError("the owner-repair runner is only passed on")


def run_case(t: object, holder: object, kind: str, spec: dict, tmp: str, reached: set) -> dict:
    """Run one case against `holder`'s function, every seam standing in (or wrapped) on the launcher `t`."""
    root = Path(tmp)
    path = root / "porter.json"
    path.write_text(spec["text"] if "text" in spec else json.dumps(spec.get("raw", {"project": "porter", "roles": rows()}), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    calls: list = []
    said: list = []
    reloaded = Reloaded()

    def norm(value: object) -> object:
        if isinstance(value, Path):
            return str(value).replace(tmp, "<TMP>")
        if isinstance(value, str):
            return value.replace(tmp, "<TMP>")
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if value is _subprocess_devnull:
            return "DEVNULL"
        if value is ROLE_RUNNER:
            return "ROLE_RUNNER"
        if value is runner:
            return "RUNNER"
        if value is _SUBPROCESS_RUN:
            return "subprocess.run"
        if value is config:
            return "CONFIG"
        if isinstance(value, SimpleNamespace) and hasattr(value, "tmux_session"):
            return f"ROLE {value.role} slot={value.slot} detached={value.detached}"
        if callable(value):
            return "ANOTHER CALLABLE"
        return value

    real = {n: getattr(t, n) for n in ("_load_json", "_write_json_atomic", "_raw_role_for_update", "_role_by_name", "tmux_has_session_args",
                                       "tmux_detach_clients_args", "_write_role_visibility")}

    def wrap(name, function):
        def call(*args, **kwargs):
            reached.add(name)
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items())))])
            return function(*args, **kwargs)
        return call

    def write(target, payload, **kwargs):
        if "write_error" in spec:
            kind_, text = spec["write_error"]
            raise ERRORS[kind_](text)
        return real["_write_json_atomic"](target, payload, **kwargs)

    def owner(cfg, target, *, runner):
        if "owner_error" in spec:
            raise SystemExit(spec["owner_error"])

    def runner(argv, **kwargs):
        calls.append(["runner", norm(list(argv)), norm(dict(sorted(kwargs.items())))])
        code = {"has-session": spec.get("has", 0), "detach-client": spec.get("detach", 0)}[argv[1]]
        return SimpleNamespace(returncode=code, stdout="", stderr="")

    role = SimpleNamespace(role="worker7", slot=spec.get("role_slot", 1), detached=spec.get("role_detached", False),
                           target="porter-worker7:0.0", tmux_session="porter-worker7")
    config = SimpleNamespace(project="porter", run_as_user="", roles=[SimpleNamespace(role="director", slot=0, detached=False,
                                                                                    target="porter-director:0.0", tmux_session="porter-director"), role])
    stand = {"_load_json": wrap("_load_json", real["_load_json"]), "_write_json_atomic": wrap("_write_json_atomic", write),
             "ensure_owner_file": wrap("ensure_owner_file", owner), "load_project_config": wrap("load_project_config", lambda project, target: reloaded),
             "_raw_role_for_update": wrap("_raw_role_for_update", real["_raw_role_for_update"]), "_role_by_name": wrap("_role_by_name", real["_role_by_name"]),
             "tmux_has_session_args": wrap("tmux_has_session_args", real["tmux_has_session_args"]),
             "tmux_detach_clients_args": wrap("tmux_detach_clients_args", real["tmux_detach_clients_args"]),
             "_write_role_visibility": wrap("_write_role_visibility", real["_write_role_visibility"])}
    saved = {n: getattr(t, n) for n in stand}
    # The function under test is called as it was before any stand-in went in (the launcher attribute is wrapped).
    unpatched = {"_write_role_visibility": getattr(holder, "_write_role_visibility")}
    for n, f in stand.items():
        setattr(t, n, f)
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            if kind == "write":
                got = unpatched["_write_role_visibility"](config, config_path=path, role=role, detached=spec.get("detached", True),
                                                    slot=spec.get("slot"), runner=ROLE_RUNNER)
            elif spec.get("defaults"):
                got = holder.detach_role_from_slot(config, config_path=path, role_name=spec.get("role_name", "worker7"))
            else:
                got = holder.detach_role_from_slot(config, config_path=path, role_name=spec.get("role_name", "worker7"), runner=runner,
                                                   print_func=lambda line: said.append(norm(line)) or calls.append(["say"]))
        answer = "RELOADED" if got is reloaded else norm(got)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        answer = {"raised": type(exc).__name__, "message": norm(str(exc))}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
    return {"answer": answer, "said": said, "stdout": norm(stdout.getvalue()), "calls": calls,
            "file": norm(path.read_text(encoding="utf-8")), "left": sorted(p.name for p in root.iterdir())}


_subprocess_devnull = subprocess.DEVNULL
# The real object, taken when this is loaded: a guard may later put a refuser at `subprocess.run`.
_SUBPROCESS_RUN = subprocess.run
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


class contained:
    """Spawns, signals, account lookups and ownership changes refused, and every Path touched outside the owned root."""

    def __init__(self, root: object) -> None:
        self.root = str(Path(root).resolve())

    def __enter__(self) -> None:
        allowed = self.root

        def guarded(name):
            real = getattr(Path, name)

            def call(self, *args, **kwargs):
                resolved = os.path.realpath(os.path.abspath(self))
                if resolved != allowed and not resolved.startswith(allowed + os.sep):
                    raise AssertionError(f"touched outside the owned root: {name} {self}")
                return real(self, *args, **kwargs)
            return call
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill"), geteuid=refuse("os.geteuid"), chown=refuse("os.chown"), fchown=refuse("os.fchown")),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(Path, **{name: guarded(name) for name in ("read_text", "write_text", "read_bytes", "replace", "mkdir", "iterdir", "unlink")})]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(kind: str, spec: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd406-test.") as tmp:
        tmp = os.path.realpath(tmp)
        with contained(tmp):
            return json.loads(json.dumps(run_case(t, m, kind, spec, tmp, REACHED)))


def test_the_guard_itself_refuses_a_spawn_a_lookup_and_a_path_outside_the_root() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
        attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0), lambda: os.geteuid(),
                    lambda: pwd.getpwuid(0), lambda: (Path(elsewhere) / "x").write_text("x"), lambda: list(Path(elsewhere).iterdir())]
        for attempt in attempts:
            with contained(tmp):
                try:
                    attempt()
                except AssertionError as exc:
                    refused = " was called: " in str(exc) or "outside the owned root" in str(exc)
                else:
                    refused = False
            check(refused, "the guard refuses a spawn, a signal, an account or ownership call, and a path outside the root")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.role_visibility as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.role_visibility", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.role_visibility")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.role_visibility as m; "
                        "d = inspect.signature(m.detach_role_from_slot).parameters; w = inspect.signature(m._write_role_visibility).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "d['runner'].default is subprocess.run and d['print_func'].default is print "
                        "and w['runner'].default is inspect.Parameter.empty "
                        "and list(d) == ['config', 'config_path', 'role_name', 'runner', 'print_func'] "
                        "and list(w) == ['config', 'config_path', 'role', 'detached', 'slot', 'runner'], "
                        "not hasattr(m, 'ProjectConfig') and not hasattr(m, 'RoleConfig') and not hasattr(m, 'launcher') and not hasattr(m, '_role_by_name'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "role_visibility.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's and imports nothing: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *node.args.defaults,
                                   *[d for d in node.args.kw_defaults if d],
                                   *(x.annotation for x in ast.walk(node) if isinstance(x, ast.AnnAssign))] if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    detach = next(n for n in tree.body if getattr(n, "name", None) == "detach_role_from_slot")
    check([ast.unparse(d) for d in detach.args.kw_defaults if d is not None] == ["subprocess.run", "print"],
          "the runner and print defaults are bound when it is defined")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from pathlib import Path", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig, RoleConfig"],
          f"only the standard library at the top, and the annotations' types under TYPE_CHECKING: {top} {tc}")
    order = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(order == list(MOVED), f"the four in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_the_four_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.role_visibility"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the four, the private-looking ones included, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"role_pane_declaration", "tmux_pane_pid_args", "_layout_slot_count", "_role_by_name", "main"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and its dispatcher, its own or re-exported")
    calls: dict = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for x in ast.walk(fn):
                if isinstance(x, ast.Call) and ast.unparse(x.func).split(".")[-1] in MOVED:
                    calls.setdefault(fn.name, {}).setdefault(ast.unparse(x.func), 0)
                    calls[fn.name][ast.unparse(x.func)] += 1
    check(calls == DISPATCH, f"the launcher's callers reach them by its own globals, as often as before: {calls}")
    data = (ROOT / "scripts" / "role_pane_entry.py").read_bytes()
    check(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == ROLE_PANE_ENTRY_BLOB,
          "role_pane_entry.py, which reads launcher._write_role_visibility when it runs, is byte-identical to the baseline")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_visibility_update_and_detach_is_the_baselines() -> None:
    measured = [f"{kind}:{label}" for kind, cases in (("write", WRITE_CASES), ("detach", DETACH_CASES)) for label in cases]
    check(sorted(measured) == sorted(k for k in GOLDEN if k.startswith(("write:", "detach:"))), "every measured case is asserted, and nothing else")
    for kind, cases in (("write", WRITE_CASES), ("detach", DETACH_CASES)):
        for label, spec in cases.items():
            got = run(kind, spec)
            check(got == GOLDEN[f"{kind}:{label}"], f"{kind} {label}: the baseline's answer, messages, file and every call in order: {got}")


def test_the_raw_role_lookup_and_the_argv_are_the_baselines() -> None:
    for label, raw in {"a list": [dict(r) for r in ROWS], "an object": {"worker7": {}}, "null": None, "a string": "worker7"}.items():
        try:
            got = m._raw_role_for_update(raw, "worker7")
        except SystemExit as exc:
            got = {"raised": "SystemExit", "message": str(exc)}
        check(got == GOLDEN[f"raw:{label}"], f"raw roles {label}: {got}")
    role = SimpleNamespace(role="worker7", tmux_session="porter-worker7", target="porter-worker7:0.0")
    check(m.tmux_detach_clients_args(role) == GOLDEN["argv:detach clients"], "the detach argv")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_four_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"role_visibility_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
