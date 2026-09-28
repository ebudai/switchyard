#!/usr/bin/env python3
"""SYRD-398: the declared-workflow presence record and reader, against the launcher they came out of.

`NON_DECLARATIVE_WORKFLOW_SEED`, the frozen `DeclaredWorkflowPresence` record
and `declared_workflow_presence` moved unchanged into
`scripts/workflow_presence.py`, and the launcher re-exports them. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the record is the baseline's --
  frozen, the same fields, defaults, order and properties -- and its
  decorator is bound when it is defined.
- **Seams (rule 24):** the JSON loader, root's recorded workflow, the board's
  reader, the seed and the record are read through the launcher as often as
  before, so a patch there reaches each of them, which this test shows.
- **The behaviour is the baseline's:** for each combination of config
  (declares, no key, null, missing, bad JSON, a directory, a loader that exits),
  root record (with and without a note), plan (declares, absent, unreadable, the
  pgu-full seed, another seed, a loader that exits), board (a document, with or
  without a note, running none, unreachable, nothing and no reason) and reader
  (given or default), every
  field, every property and the order the sources are asked in --
  `GOLDEN` below was produced by the BASELINE launcher's own reader with the
  same stand-ins (`gold398.py`), not typed.

Everything real is an owned temporary directory; root's record and the board
are stand-ins; nothing reaches a board, a database or a tenant.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import privileged_provision_records  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts import workflow_presence as m  # noqa: E402

CHECKS = 0
MOVED = ("NON_DECLARATIVE_WORKFLOW_SEED", "DeclaredWorkflowPresence", "declared_workflow_presence")
#: Measured on the baseline launcher: the reader's call-time reads of launcher globals, the moved seed and record included.
SEAMS = {
    'declared_workflow_presence': {'DeclaredWorkflowPresence': 1, 'NON_DECLARATIVE_WORKFLOW_SEED': 1, '_load_json': 2, 'read_board_declared_workflow': 1, 'recorded_declared_workflow': 1},
}
#: The BASELINE's own answers for the cases below (`gold398.py`, run on the baseline launcher under the guard).
GOLDEN = {'declares|declares|root records|a document|default reader': {'type': 'DeclaredWorkflowPresence',
                                                              'fields': {'project': 'p398',
                                                                         'config_declares': True,
                                                                         'config_unreadable': '',
                                                                         'root_records': True,
                                                                         'plan_declares': True,
                                                                         'board_document': True,
                                                                         'board_problem': '',
                                                                         'non_declarative_by_design': False},
                                                              'declared_somewhere': True,
                                                              'board_runs_none': False,
                                                              'legacy_without_workflow': False,
                                                              'calls': [['load', 'p398.json'],
                                                                        ['recorded', 'p398'],
                                                                        ['load', 'plan.json'],
                                                                        ['default board', 'p398']]},
 'no workflow key|absent|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                    'fields': {'project': 'p398',
                                                                               'config_declares': False,
                                                                               'config_unreadable': '',
                                                                               'root_records': False,
                                                                               'plan_declares': False,
                                                                               'board_document': False,
                                                                               'board_problem': 'the board is running no declared workflow',
                                                                               'non_declarative_by_design': False},
                                                                    'declared_somewhere': False,
                                                                    'board_runs_none': True,
                                                                    'legacy_without_workflow': True,
                                                                    'calls': [['load', 'p398.json'],
                                                                              ['recorded', 'p398'],
                                                                              ['load', 'plan.json'],
                                                                              ['default board', 'p398']]},
 'no workflow key|absent|no root record|unreachable|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                      'fields': {'project': 'p398',
                                                                                 'config_declares': False,
                                                                                 'config_unreadable': '',
                                                                                 'root_records': False,
                                                                                 'plan_declares': False,
                                                                                 'board_document': False,
                                                                                 'board_problem': "the board's workflow could not be read: "
                                                                                                  '[Errno 111] Connection refused',
                                                                                 'non_declarative_by_design': False},
                                                                      'declared_somewhere': False,
                                                                      'board_runs_none': False,
                                                                      'legacy_without_workflow': False,
                                                                      'calls': [['load', 'p398.json'],
                                                                                ['recorded', 'p398'],
                                                                                ['load', 'plan.json'],
                                                                                ['default board', 'p398']]},
 'a null workflow|absent|no root record|runs none|given reader': {'type': 'DeclaredWorkflowPresence',
                                                                  'fields': {'project': 'p398',
                                                                             'config_declares': False,
                                                                             'config_unreadable': '',
                                                                             'root_records': False,
                                                                             'plan_declares': False,
                                                                             'board_document': False,
                                                                             'board_problem': 'the board is running no declared workflow',
                                                                             'non_declarative_by_design': False},
                                                                  'declared_somewhere': False,
                                                                  'board_runs_none': True,
                                                                  'legacy_without_workflow': True,
                                                                  'calls': [['load', 'p398.json'],
                                                                            ['recorded', 'p398'],
                                                                            ['load', 'plan.json'],
                                                                            ['given board', 'p398']]},
 'missing|absent|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                            'fields': {'project': 'p398',
                                                                       'config_declares': False,
                                                                       'config_unreadable': '<tmp>/p398/p398.json could not be read '
                                                                                            '([Errno 2] No such file or directory: '
                                                                                            "'<tmp>/p398/p398.json')",
                                                                       'root_records': False,
                                                                       'plan_declares': False,
                                                                       'board_document': False,
                                                                       'board_problem': 'the board is running no declared workflow',
                                                                       'non_declarative_by_design': False},
                                                            'declared_somewhere': False,
                                                            'board_runs_none': True,
                                                            'legacy_without_workflow': True,
                                                            'calls': [['load', 'p398.json'],
                                                                      ['recorded', 'p398'],
                                                                      ['load', 'plan.json'],
                                                                      ['default board', 'p398']]},
 'bad json|absent|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                             'fields': {'project': 'p398',
                                                                        'config_declares': False,
                                                                        'config_unreadable': '<tmp>/p398/p398.json could not be read '
                                                                                             '(Expecting property name enclosed in double '
                                                                                             'quotes: line 1 column 2 (char 1))',
                                                                        'root_records': False,
                                                                        'plan_declares': False,
                                                                        'board_document': False,
                                                                        'board_problem': 'the board is running no declared workflow',
                                                                        'non_declarative_by_design': False},
                                                             'declared_somewhere': False,
                                                             'board_runs_none': True,
                                                             'legacy_without_workflow': True,
                                                             'calls': [['load', 'p398.json'],
                                                                       ['recorded', 'p398'],
                                                                       ['load', 'plan.json'],
                                                                       ['default board', 'p398']]},
 'a directory|declares|no root record|unreachable|given reader': {'type': 'DeclaredWorkflowPresence',
                                                                  'fields': {'project': 'p398',
                                                                             'config_declares': False,
                                                                             'config_unreadable': '<tmp>/p398/p398.json could not be read '
                                                                                                  '([Errno 21] Is a directory: '
                                                                                                  "'<tmp>/p398/p398.json')",
                                                                             'root_records': False,
                                                                             'plan_declares': True,
                                                                             'board_document': False,
                                                                             'board_problem': "the board's workflow could not be read: "
                                                                                              '[Errno 111] Connection refused',
                                                                             'non_declarative_by_design': False},
                                                                  'declared_somewhere': True,
                                                                  'board_runs_none': False,
                                                                  'legacy_without_workflow': False,
                                                                  'calls': [['load', 'p398.json'],
                                                                            ['recorded', 'p398'],
                                                                            ['load', 'plan.json'],
                                                                            ['given board', 'p398']]},
 'no workflow key|declares|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                      'fields': {'project': 'p398',
                                                                                 'config_declares': False,
                                                                                 'config_unreadable': '',
                                                                                 'root_records': False,
                                                                                 'plan_declares': True,
                                                                                 'board_document': False,
                                                                                 'board_problem': 'the board is running no declared '
                                                                                                  'workflow',
                                                                                 'non_declarative_by_design': False},
                                                                      'declared_somewhere': True,
                                                                      'board_runs_none': True,
                                                                      'legacy_without_workflow': True,
                                                                      'calls': [['load', 'p398.json'],
                                                                                ['recorded', 'p398'],
                                                                                ['load', 'plan.json'],
                                                                                ['default board', 'p398']]},
 'no workflow key|unreadable|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                        'fields': {'project': 'p398',
                                                                                   'config_declares': False,
                                                                                   'config_unreadable': '',
                                                                                   'root_records': False,
                                                                                   'plan_declares': False,
                                                                                   'board_document': False,
                                                                                   'board_problem': 'the board is running no declared '
                                                                                                    'workflow',
                                                                                   'non_declarative_by_design': False},
                                                                        'declared_somewhere': False,
                                                                        'board_runs_none': True,
                                                                        'legacy_without_workflow': True,
                                                                        'calls': [['load', 'p398.json'],
                                                                                  ['recorded', 'p398'],
                                                                                  ['load', 'plan.json'],
                                                                                  ['default board', 'p398']]},
 'no workflow key|pgu-full seed|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                           'fields': {'project': 'p398',
                                                                                      'config_declares': False,
                                                                                      'config_unreadable': '',
                                                                                      'root_records': False,
                                                                                      'plan_declares': False,
                                                                                      'board_document': False,
                                                                                      'board_problem': 'the board is running no declared '
                                                                                                       'workflow',
                                                                                      'non_declarative_by_design': True},
                                                                           'declared_somewhere': False,
                                                                           'board_runs_none': True,
                                                                           'legacy_without_workflow': False,
                                                                           'calls': [['load', 'p398.json'],
                                                                                     ['recorded', 'p398'],
                                                                                     ['load', 'plan.json'],
                                                                                     ['default board', 'p398']]},
 'no workflow key|another seed|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                          'fields': {'project': 'p398',
                                                                                     'config_declares': False,
                                                                                     'config_unreadable': '',
                                                                                     'root_records': False,
                                                                                     'plan_declares': False,
                                                                                     'board_document': False,
                                                                                     'board_problem': 'the board is running no declared '
                                                                                                      'workflow',
                                                                                     'non_declarative_by_design': False},
                                                                          'declared_somewhere': False,
                                                                          'board_runs_none': True,
                                                                          'legacy_without_workflow': True,
                                                                          'calls': [['load', 'p398.json'],
                                                                                    ['recorded', 'p398'],
                                                                                    ['load', 'plan.json'],
                                                                                    ['default board', 'p398']]},
 'no workflow key|pgu-full seed and a workflow|no root record|a document|given reader': {'type': 'DeclaredWorkflowPresence',
                                                                                         'fields': {'project': 'p398',
                                                                                                    'config_declares': False,
                                                                                                    'config_unreadable': '',
                                                                                                    'root_records': False,
                                                                                                    'plan_declares': True,
                                                                                                    'board_document': True,
                                                                                                    'board_problem': '',
                                                                                                    'non_declarative_by_design': True},
                                                                                         'declared_somewhere': True,
                                                                                         'board_runs_none': False,
                                                                                         'legacy_without_workflow': False,
                                                                                         'calls': [['load', 'p398.json'],
                                                                                                   ['recorded', 'p398'],
                                                                                                   ['load', 'plan.json'],
                                                                                                   ['given board', 'p398']]},
 'missing|pgu-full seed|no root record|unreachable|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                     'fields': {'project': 'p398',
                                                                                'config_declares': False,
                                                                                'config_unreadable': '<tmp>/p398/p398.json could not be '
                                                                                                     'read ([Errno 2] No such file or '
                                                                                                     "directory: '<tmp>/p398/p398.json')",
                                                                                'root_records': False,
                                                                                'plan_declares': False,
                                                                                'board_document': False,
                                                                                'board_problem': "the board's workflow could not be read: "
                                                                                                 '[Errno 111] Connection refused',
                                                                                'non_declarative_by_design': True},
                                                                     'declared_somewhere': False,
                                                                     'board_runs_none': False,
                                                                     'legacy_without_workflow': False,
                                                                     'calls': [['load', 'p398.json'],
                                                                               ['recorded', 'p398'],
                                                                               ['load', 'plan.json'],
                                                                               ['default board', 'p398']]},
 'declares|absent|root records|runs none|given reader': {'type': 'DeclaredWorkflowPresence',
                                                         'fields': {'project': 'p398',
                                                                    'config_declares': True,
                                                                    'config_unreadable': '',
                                                                    'root_records': True,
                                                                    'plan_declares': False,
                                                                    'board_document': False,
                                                                    'board_problem': 'the board is running no declared workflow',
                                                                    'non_declarative_by_design': False},
                                                         'declared_somewhere': True,
                                                         'board_runs_none': True,
                                                         'legacy_without_workflow': True,
                                                         'calls': [['load', 'p398.json'],
                                                                   ['recorded', 'p398'],
                                                                   ['load', 'plan.json'],
                                                                   ['given board', 'p398']]},
 'no workflow key|absent|root records|unreachable|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                    'fields': {'project': 'p398',
                                                                               'config_declares': False,
                                                                               'config_unreadable': '',
                                                                               'root_records': True,
                                                                               'plan_declares': False,
                                                                               'board_document': False,
                                                                               'board_problem': "the board's workflow could not be read: "
                                                                                                '[Errno 111] Connection refused',
                                                                               'non_declarative_by_design': False},
                                                                    'declared_somewhere': True,
                                                                    'board_runs_none': False,
                                                                    'legacy_without_workflow': False,
                                                                    'calls': [['load', 'p398.json'],
                                                                              ['recorded', 'p398'],
                                                                              ['load', 'plan.json'],
                                                                              ['default board', 'p398']]},
 'the loader exits|absent|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                     'fields': {'project': 'p398',
                                                                                'config_declares': False,
                                                                                'config_unreadable': '<tmp>/p398/p398.json could not be '
                                                                                                     'read (switchyard: p398.json is not a '
                                                                                                     'config)',
                                                                                'root_records': False,
                                                                                'plan_declares': False,
                                                                                'board_document': False,
                                                                                'board_problem': 'the board is running no declared '
                                                                                                 'workflow',
                                                                                'non_declarative_by_design': False},
                                                                     'declared_somewhere': False,
                                                                     'board_runs_none': True,
                                                                     'legacy_without_workflow': True,
                                                                     'calls': [['load', 'p398.json'],
                                                                               ['recorded', 'p398'],
                                                                               ['load', 'plan.json'],
                                                                               ['default board', 'p398']]},
 'no workflow key|the loader exits|no root record|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                              'fields': {'project': 'p398',
                                                                                         'config_declares': False,
                                                                                         'config_unreadable': '',
                                                                                         'root_records': False,
                                                                                         'plan_declares': False,
                                                                                         'board_document': False,
                                                                                         'board_problem': 'the board is running no '
                                                                                                          'declared workflow',
                                                                                         'non_declarative_by_design': False},
                                                                              'declared_somewhere': False,
                                                                              'board_runs_none': True,
                                                                              'legacy_without_workflow': True,
                                                                              'calls': [['load', 'p398.json'],
                                                                                        ['recorded', 'p398'],
                                                                                        ['load', 'plan.json'],
                                                                                        ['default board', 'p398']]},
 'no workflow key|absent|root records, with a note|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                               'fields': {'project': 'p398',
                                                                                          'config_declares': False,
                                                                                          'config_unreadable': '',
                                                                                          'root_records': True,
                                                                                          'plan_declares': False,
                                                                                          'board_document': False,
                                                                                          'board_problem': 'the board is running no '
                                                                                                           'declared workflow',
                                                                                          'non_declarative_by_design': False},
                                                                               'declared_somewhere': True,
                                                                               'board_runs_none': True,
                                                                               'legacy_without_workflow': True,
                                                                               'calls': [['load', 'p398.json'],
                                                                                         ['recorded', 'p398'],
                                                                                         ['load', 'plan.json'],
                                                                                         ['default board', 'p398']]},
 'no workflow key|absent|no record, no reason|runs none|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                          'fields': {'project': 'p398',
                                                                                     'config_declares': False,
                                                                                     'config_unreadable': '',
                                                                                     'root_records': False,
                                                                                     'plan_declares': False,
                                                                                     'board_document': False,
                                                                                     'board_problem': 'the board is running no declared '
                                                                                                      'workflow',
                                                                                     'non_declarative_by_design': False},
                                                                          'declared_somewhere': False,
                                                                          'board_runs_none': True,
                                                                          'legacy_without_workflow': True,
                                                                          'calls': [['load', 'p398.json'],
                                                                                    ['recorded', 'p398'],
                                                                                    ['load', 'plan.json'],
                                                                                    ['default board', 'p398']]},
 'no workflow key|absent|no root record|a document, with a note|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                                  'fields': {'project': 'p398',
                                                                                             'config_declares': False,
                                                                                             'config_unreadable': '',
                                                                                             'root_records': False,
                                                                                             'plan_declares': False,
                                                                                             'board_document': True,
                                                                                             'board_problem': 'the board is running no '
                                                                                                              'declared workflow (a cached '
                                                                                                              'note)',
                                                                                             'non_declarative_by_design': False},
                                                                                  'declared_somewhere': True,
                                                                                  'board_runs_none': False,
                                                                                  'legacy_without_workflow': False,
                                                                                  'calls': [['load', 'p398.json'],
                                                                                            ['recorded', 'p398'],
                                                                                            ['load', 'plan.json'],
                                                                                            ['default board', 'p398']]},
 'no workflow key|absent|no root record|nothing and no reason|default reader': {'type': 'DeclaredWorkflowPresence',
                                                                                'fields': {'project': 'p398',
                                                                                           'config_declares': False,
                                                                                           'config_unreadable': '',
                                                                                           'root_records': False,
                                                                                           'plan_declares': False,
                                                                                           'board_document': False,
                                                                                           'board_problem': '',
                                                                                           'non_declarative_by_design': False},
                                                                                'declared_somewhere': False,
                                                                                'board_runs_none': False,
                                                                                'legacy_without_workflow': False,
                                                                                'calls': [['load', 'p398.json'],
                                                                                          ['recorded', 'p398'],
                                                                                          ['load', 'plan.json'],
                                                                                          ['default board', 'p398']]},
 '_class': {'fields': [['project', 'MISSING'],
                       ['config_declares', 'False'],
                       ['config_unreadable', "''"],
                       ['root_records', 'False'],
                       ['plan_declares', 'False'],
                       ['board_document', 'False'],
                       ['board_problem', "''"],
                       ['non_declarative_by_design', 'False']],
            'frozen': True,
            'seed': 'pgu-full',
            'properties': ['board_runs_none', 'declared_somewhere', 'legacy_without_workflow']}}
#: Every seam a stand-in or a patch on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
CONFIGS = {"declares": {"workflow": {"stages": ["x"]}}, "no workflow key": {"project": "p398"}, "a null workflow": {"workflow": None},
           "missing": None, "bad json": "{not json", "a directory": "DIR", "the loader exits": "EXIT"}
PLANS = {"declares": {"workflow": {"stages": ["x"]}}, "absent": None, "unreadable": "{not json", "pgu-full seed": {"workflow_seed": "pgu-full"},
         "another seed": {"workflow_seed": "custom"}, "pgu-full seed and a workflow": {"workflow": {"s": 1}, "workflow_seed": "pgu-full"}, "the loader exits": "EXIT"}
RECORDS = {"root records": ({"stages": []}, ""), "no root record": (None, "root holds none"), "root records, with a note": ({"stages": []}, "recorded with a warning"),
           "no record, no reason": (None, "")}
BOARDS = {"a document": ({"stages": []}, ""), "runs none": (None, "the board is running no declared workflow"),
          "unreachable": (None, "the board's workflow could not be read: [Errno 111] Connection refused"),
          "a document, with a note": ({"stages": []}, "the board is running no declared workflow (a cached note)"), "nothing and no reason": (None, "")}


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


def seam(name: str, function):
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


def no_process() -> patched:
    return patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen"))


def write(path: Path, value: object) -> None:
    if value is None:
        return
    if value == "DIR":
        path.mkdir()
        return
    if value == "EXIT":
        path.write_text("EXIT")
        return
    path.write_text(value if isinstance(value, str) else json.dumps(value))


def presence(config: str, plan: str, recorded: str, board: str, override: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "p398"
        base.mkdir()
        cfg = base / "p398.json"
        write(cfg, CONFIGS[config])
        write(base / "plan.json", PLANS[plan])
        log: list = []
        real_load = t._load_json

        def load(p):
            # A loader that exits: SystemExit is one of the three things an unreadable source is allowed to raise.
            log.append(["load", Path(p).name])
            if Path(p).is_file() and Path(p).read_text() == "EXIT":
                raise SystemExit(f"switchyard: {Path(p).name} is not a config")
            return real_load(p)

        given = lambda c: log.append(["given board", c.project]) or BOARDS[board]  # noqa: E731
        # Root's own record, where the launcher imports it from, refuses: a reader that went past the launcher fails here.
        with no_process(), patched(privileged_provision_records, recorded_declared_workflow=refuse("privileged_provision_records.recorded_declared_workflow")), \
                patched(t, _load_json=seam("_load_json", load),
                        recorded_declared_workflow=seam("recorded_declared_workflow", lambda project: log.append(["recorded", project]) or RECORDS[recorded]),
                        read_board_declared_workflow=seam("read_board_declared_workflow",
                                                          lambda c: log.append(["default board", c.project]) or BOARDS[board])):
            try:
                p = m.declared_workflow_presence(SimpleNamespace(project="p398"), config_path=cfg, board_reader=given if override else None)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- whatever a mutant raises (SystemExit included) is an answer to compare
                return {"raised": repr(exc).replace(tmp, "<tmp>"), "calls": log}
        fields = {k: (v.replace(tmp, "<tmp>") if isinstance(v, str) else v) for k, v in dataclasses.asdict(p).items()}
        return {"type": type(p).__name__, "fields": fields, "declared_somewhere": p.declared_somewhere, "board_runs_none": p.board_runs_none,
                "legacy_without_workflow": p.legacy_without_workflow, "calls": log}


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.workflow_presence as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_record() -> None:
    for order in (("scripts.workflow_presence", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.workflow_presence")):
        result = python("import importlib, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.workflow_presence as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "m.DeclaredWorkflowPresence.__module__ == 'scripts.workflow_presence' and m.dataclass is dataclasses.dataclass, "
                        "not hasattr(m, '_load_json') and not hasattr(m, 'read_board_declared_workflow') and not hasattr(m, 'ProjectConfig'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    record = GOLDEN["_class"]
    fields = [[f.name, "MISSING" if f.default is dataclasses.MISSING else repr(f.default)] for f in dataclasses.fields(m.DeclaredWorkflowPresence)]
    check(fields == record["fields"] and m.DeclaredWorkflowPresence.__dataclass_params__.frozen is record["frozen"] is True
          and sorted(n for n, v in vars(m.DeclaredWorkflowPresence).items() if isinstance(v, property)) == record["properties"]
          and m.NON_DECLARATIVE_WORKFLOW_SEED == record["seed"],
          f"the record is the baseline's: frozen, its fields, defaults and order, its three properties; the seed: {fields}")
    frozen = m.DeclaredWorkflowPresence(project="p398")
    try:
        frozen.project = "other"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        refused = True
    else:
        refused = False
    check(refused, "and it cannot be changed once made")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "workflow_presence.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if getattr(n, "name", None) == "declared_workflow_presence")
    through: dict[str, int] = {}
    for x in ast.walk(node):
        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
            through[x.attr] = through.get(x.attr, 0) + 1
    expected = SEAMS["declared_workflow_presence"]
    check(through == expected, f"each launcher name read through it exactly as often as before: {through}")
    imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
    check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[1]) == imports[0],
          f"the launcher imported once, first thing when it runs: {imports}")
    skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *node.args.defaults,
                               *[d for d in node.args.kw_defaults if d]] if part is not None for y in ast.walk(part)}
    bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
    check(bare == [], f"none of them read past it: {bare}")
    record = next(n for n in tree.body if getattr(n, "name", None) == "DeclaredWorkflowPresence")
    check([ast.unparse(d) for d in record.decorator_list] == ["dataclass(frozen=True)"]
          and not [x for x in ast.walk(record) if isinstance(x, ast.Name) and x.id == "launcher"],
          "the record: frozen by the decorator bound when it is defined, and reading nothing of the launcher's")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "from dataclasses import dataclass", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Callable"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [getattr(n, "name", None) or ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(order == list(MOVED), f"the three in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_the_three_above_every_consumer() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.workflow_presence"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED)
          and {"SWITCHYARD_RELEASE_MARKER_NAME", "DEFAULT_SESSION_DIR", "resolved_source_selection", "_open_board_url"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours, its own or re-exported")
    reads = {}
    for consumer in ("release_alignment", "upgrade_records", "project_onboarding"):
        module = ast.parse((ROOT / "scripts" / f"{consumer}.py").read_text(encoding="utf-8"))
        through = sorted(ast.unparse(x) for x in ast.walk(module) if isinstance(x, ast.Attribute) and x.attr in MOVED)
        typed = sorted(a.name for x in ast.walk(module) if isinstance(x, ast.ImportFrom) and x.module == "scripts.team_launcher"
                       for a in x.names if a.name in MOVED)
        direct = [ast.unparse(x) for x in ast.walk(module) if isinstance(x, ast.ImportFrom) and x.module == "scripts.workflow_presence"]
        reads[consumer] = (through, typed, direct)
    check(reads == {"release_alignment": (["launcher.declared_workflow_presence"], [], []),
                    "upgrade_records": (["launcher.declared_workflow_presence", "launcher.declared_workflow_presence"], ["DeclaredWorkflowPresence"], []),
                    "project_onboarding": ([], ["DeclaredWorkflowPresence"], [])},
          f"the consumers read the reader through the launcher, and name the record from it: {reads}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_combination_is_the_baselines() -> None:
    cases = [k for k in GOLDEN if k != "_class"]
    for key in cases:
        config, plan, root, board, reader = key.split("|")
        got = presence(config, plan, root, board, reader == "given reader")
        check(got == GOLDEN[key], f"{key}: the baseline's fields, properties and order of sources: {got}")


def test_the_distinctions_the_record_exists_for() -> None:
    unreadable = presence("bad json", "absent", "no root record", "unreachable", False)
    check(unreadable["fields"]["config_unreadable"].startswith("<tmp>/p398/p398.json could not be read (")
          and not unreadable["declared_somewhere"] and not unreadable["legacy_without_workflow"],
          f"an unreadable config names itself and is not taken for a tenant that needs nothing: {unreadable['fields']}")
    unreachable = presence("no workflow key", "absent", "no root record", "unreachable", False)
    check(not unreachable["board_runs_none"] and not unreachable["legacy_without_workflow"], "an unreachable board is unknown, not legacy")
    legacy = presence("missing", "absent", "no root record", "runs none", False)
    check(legacy["legacy_without_workflow"] and not legacy["declared_somewhere"],
          "no local config, no plan, no record, and a board running none: legacy -- never an exempt tenant")
    exempt = presence("missing", "pgu-full seed", "no root record", "runs none", False)
    check(exempt["fields"]["non_declarative_by_design"] and not exempt["legacy_without_workflow"], "the pgu-full seed is exempt, through its flag")


def test_the_seed_and_the_record_are_the_launchers_when_the_reader_runs() -> None:
    with patched(t, NON_DECLARATIVE_WORKFLOW_SEED="custom"):
        got = presence("no workflow key", "another seed", "no root record", "runs none", False)
    check(got["fields"]["non_declarative_by_design"], f"the seed is read through the launcher: {got['fields']}")
    REACHED.add("NON_DECLARATIVE_WORKFLOW_SEED")
    made: list = []

    class Recording(m.DeclaredWorkflowPresence):
        def __init__(self, **fields: object) -> None:
            made.append(fields)
            super().__init__(**fields)

    with patched(t, DeclaredWorkflowPresence=Recording):
        got = presence("declares", "absent", "no root record", "a document", False)
    check(got["type"] == "Recording" and len(made) == 1 and made[0]["project"] == "p398", "the record is made through the launcher's name")
    REACHED.add("DeclaredWorkflowPresence")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in or patch on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_record",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_three_above_every_consumer")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"workflow_presence_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
