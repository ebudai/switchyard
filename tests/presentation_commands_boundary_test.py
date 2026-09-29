#!/usr/bin/env python3
"""SYRD-412: the presentation attach, present and recovery commands, against the launcher they came out of.

`switchyard_attach_command`, `switchyard_present_command` and
`switchyard_recover_display_command` moved unchanged into
`scripts/presentation_commands.py`, and the launcher re-exports all three.
This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; `runner=subprocess.run`,
  `print_func=print` and `environ=None` are the defaults bound when each is
  defined; `ProjectConfig` is an annotation only; each command still imports
  the presentation controller when it runs.
- **Seams (rule 24):** the recovery reads both parsers, the project resolver,
  the configuration loader, the tenant-control grant, the current user and the
  present command through the launcher, as often as before -- every case below
  runs with them standing in (or wrapped) there.
- **Dispatch:** `switchyard_main` still dispatches all three by its own
  globals.
- **Behaviour is the baseline's:** attach and every present action with their
  reports; the recovery's authority order -- resolve, load across the bridge,
  then only the Director (either role variable) or the operator the bridge
  authenticates recovers, and anyone else is refused with the registered route
  or the missing grant, before anything is presented. `GOLDEN` below was
  produced by running the BASELINE launcher's own commands over the very cases
  embedded here (`gold412.py`), not typed.

Nothing touches a display, a grant or a user: every controller step stands in,
and the real `subprocess.run`/`Popen`, `os.kill`, the account lookups and
socket connections are refused for each case.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import presentation_commands as m  # noqa: E402
from scripts import presentation_controller  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ("switchyard_attach_command", "switchyard_present_command", "switchyard_recover_display_command")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'switchyard_recover_display_command': {'_build_switchyard_present_parser': 1, '_build_switchyard_recover_display_parser': 1, '_load_switchyard_project_config_for_command': 1, '_resolve_switchyard_project': 1, '_tenant_control_grant': 1, 'current_user_name': 1, 'switchyard_present_command': 1},
}
#: Measured on the baseline launcher: every launcher function that calls one of them, and by what name.
DISPATCH = {'switchyard_main': {'switchyard_attach_command': 1, 'switchyard_present_command': 1, 'switchyard_recover_display_command': 1}}
#: The BASELINE's own behaviour for the cases below (`gold412.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'attach a role': {'answer': 0, 'said': ['attach worker json=False'], 'stdout': '', 'calls': [['attach_role_command', ['CONFIG'], {'json_output': False, 'print_func': 'SAY', 'role_name': 'worker', 'runner': 'RUNNER'}], ['say']]},
    'attach, JSON listing': {'answer': 3, 'said': ['attach None json=True'], 'stdout': '', 'calls': [['attach_role_command', ['CONFIG'], {'json_output': True, 'print_func': 'SAY', 'role_name': None, 'runner': 'RUNNER'}], ['say']]},
    'attach, the defaults': {'answer': 0, 'said': [], 'stdout': 'attach worker json=False\n', 'calls': [['attach_role_command', ['CONFIG'], {'json_output': False, 'print_func': 'print', 'role_name': 'worker', 'runner': 'subprocess.run'}]]},
    'present list': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present list, JSON': {'answer': 0, 'said': ['report 1 json=True'], 'stdout': '', 'calls': [['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, True, 'SAY'], ['say']]},
    'present show': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'show', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'worker', 'runner': 'RUNNER', 'slot': 2}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present swap': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'swap', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': 3, 'role_name': None, 'runner': 'RUNNER', 'slot': 1}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present hide': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'hide', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': None, 'runner': 'RUNNER', 'slot': 4}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present focus': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'focus', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': None, 'runner': 'RUNNER', 'slot': 1}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present restore': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'restore', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': None, 'runner': 'RUNNER', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present restore a layout': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'restore', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'wide', 'other_slot': None, 'role_name': None, 'runner': 'RUNNER', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present bootstrap': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'bootstrap', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'separate', 'other_slot': None, 'role_name': None, 'runner': 'RUNNER', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present bootstrap the viewer': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'bootstrap', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'viewer', 'other_slot': None, 'role_name': None, 'runner': 'RUNNER', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present recover': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'worker', 'runner': 'RUNNER', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present list, the defaults': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'present show, a namespace asking for JSON': {'answer': 0, 'said': ['report 1 json=False'], 'stdout': '', 'calls': [['presentation_action', ['CONFIG'], {'action': 'show', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'worker', 'runner': 'RUNNER', 'slot': 2}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'RUNNER'}], ['print_presentation_report', {'n': 1}, False, 'SAY'], ['say']]},
    'present show, the defaults': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['presentation_action', ['CONFIG'], {'action': 'show', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'worker', 'runner': 'subprocess.run', 'slot': 2}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'recover as the Director': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['_build_switchyard_present_parser', [], {}], ['switchyard_present_command', ['CONFIG'], {'args': {'namespace': {'action': 'recover', 'project': 'p412', 'role': 'director'}}, 'config_path': 'PATH /nonexistent/syrd412/p412.json'}], ['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'director', 'runner': 'subprocess.run', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'recover as the Director, the legacy variable': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['_build_switchyard_present_parser', [], {}], ['switchyard_present_command', ['CONFIG'], {'args': {'namespace': {'action': 'recover', 'project': 'p412', 'role': 'director'}}, 'config_path': 'PATH /nonexistent/syrd412/p412.json'}], ['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'director', 'runner': 'subprocess.run', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'recover, the first variable wins': {'answer': {'raised': 'SystemExit', 'message': "switchyard: recover-display reaches p412's Director through its tenant-control bridge, as the desktop operator the project is registered to. syrd412-caller did not arrive over that bridge, so nothing authorizes the recovery here: p412 has no tenant-control grant, so no operator is registered for it"}, 'said': [], 'stdout': '', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['operator_recovery_caller', ['CONFIG', {'PGU_TICKET_BOARD_CALLER_ROLE': 'director', 'TICKET_BOARD_CALLER_ROLE': 'worker'}], {}], ['_tenant_control_grant', ['p412'], {}], ['current_user_name', [], {}]]},
    'recover as the operator': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['operator_recovery_caller', ['CONFIG', {'SYRD412_OPERATOR': 'yes'}], {}], ['_build_switchyard_present_parser', [], {}], ['switchyard_present_command', ['CONFIG'], {'args': {'namespace': {'action': 'recover', 'project': 'p412', 'role': 'director'}}, 'config_path': 'PATH /nonexistent/syrd412/p412.json'}], ['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'director', 'runner': 'subprocess.run', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    "recover as the operator, a worker's role set": {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['operator_recovery_caller', ['CONFIG', {'SYRD412_OPERATOR': 'yes', 'TICKET_BOARD_CALLER_ROLE': 'worker'}], {}], ['_build_switchyard_present_parser', [], {}], ['switchyard_present_command', ['CONFIG'], {'args': {'namespace': {'action': 'recover', 'project': 'p412', 'role': 'director'}}, 'config_path': 'PATH /nonexistent/syrd412/p412.json'}], ['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'director', 'runner': 'subprocess.run', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'recover, a worker refused': {'answer': {'raised': 'SystemExit', 'message': "switchyard: recover-display reaches p412's Director through its tenant-control bridge, as the desktop operator the project is registered to. syrd412-caller did not arrive over that bridge, so nothing authorizes the recovery here: it is registered to syrd412-operator; run this from syrd412-operator's desktop session"}, 'said': [], 'stdout': '', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['operator_recovery_caller', ['CONFIG', {'TICKET_BOARD_CALLER_ROLE': 'worker'}], {}], ['_tenant_control_grant', ['p412'], {}], ['current_user_name', [], {}]]},
    'recover, refused without a grant': {'answer': {'raised': 'SystemExit', 'message': "switchyard: recover-display reaches p412's Director through its tenant-control bridge, as the desktop operator the project is registered to. syrd412-caller did not arrive over that bridge, so nothing authorizes the recovery here: p412 has no tenant-control grant, so no operator is registered for it"}, 'said': [], 'stdout': '', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['operator_recovery_caller', ['CONFIG', {}], {}], ['_tenant_control_grant', ['p412'], {}], ['current_user_name', [], {}]]},
    'recover, a display name typed': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['P412 Project'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['_build_switchyard_present_parser', [], {}], ['switchyard_present_command', ['CONFIG'], {'args': {'namespace': {'action': 'recover', 'project': 'p412', 'role': 'director'}}, 'config_path': 'PATH /nonexistent/syrd412/p412.json'}], ['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'director', 'runner': 'subprocess.run', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'recover, the process environment': {'answer': 0, 'said': [], 'stdout': 'report 1 json=False\n', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['_build_switchyard_present_parser', [], {}], ['switchyard_present_command', ['CONFIG'], {'args': {'namespace': {'action': 'recover', 'project': 'p412', 'role': 'director'}}, 'config_path': 'PATH /nonexistent/syrd412/p412.json'}], ['presentation_action', ['CONFIG'], {'action': 'recover', 'config_path': 'PATH /nonexistent/syrd412/p412.json', 'layout': 'default', 'other_slot': None, 'role_name': 'director', 'runner': 'subprocess.run', 'slot': None}], ['presentation_report', ['CONFIG'], {'config_path': 'PATH /nonexistent/syrd412/p412.json', 'runner': 'subprocess.run'}], ['print_presentation_report', {'n': 1}, False, 'print']]},
    'recover, the process environment refused': {'answer': {'raised': 'SystemExit', 'message': "switchyard: recover-display reaches p412's Director through its tenant-control bridge, as the desktop operator the project is registered to. syrd412-caller did not arrive over that bridge, so nothing authorizes the recovery here: it is registered to syrd412-operator; run this from syrd412-operator's desktop session"}, 'said': [], 'stdout': '', 'calls': [['_build_switchyard_recover_display_parser', [], {}], ['_resolve_switchyard_project', ['p412'], {}], ['_load_switchyard_project_config_for_command', ['ENTRY', ['recover-display', 'p412']], {}], ['operator_recovery_caller', ['CONFIG', {}], {}], ['_tenant_control_grant', ['p412'], {}], ['current_user_name', [], {}]]},
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold412.py` (which ran them on the baseline) ------------------------------------
CASES = {
    "attach a role": {"kind": "attach", "argv": ["p412", "worker"], "code": 0},
    "attach, JSON listing": {"kind": "attach", "argv": ["p412", "--json"], "code": 3},
    "attach, the defaults": {"kind": "attach", "argv": ["p412", "worker"], "code": 0, "defaults": True},
    "present list": {"kind": "present", "argv": ["p412", "list"]},
    "present list, JSON": {"kind": "present", "argv": ["p412", "list", "--json"]},
    "present show": {"kind": "present", "argv": ["p412", "show", "worker", "--slot", "2"]},
    "present swap": {"kind": "present", "argv": ["p412", "swap", "1", "3"]},
    "present hide": {"kind": "present", "argv": ["p412", "hide", "--slot", "4"]},
    "present focus": {"kind": "present", "argv": ["p412", "focus", "--slot", "1"]},
    "present restore": {"kind": "present", "argv": ["p412", "restore"]},
    "present restore a layout": {"kind": "present", "argv": ["p412", "restore", "--layout", "wide"]},
    "present bootstrap": {"kind": "present", "argv": ["p412", "bootstrap"]},
    "present bootstrap the viewer": {"kind": "present", "argv": ["p412", "bootstrap", "--layout", "viewer"]},
    "present recover": {"kind": "present", "argv": ["p412", "recover", "worker"]},
    "present list, the defaults": {"kind": "present", "argv": ["p412", "list"], "defaults": True},
    # A caller-built namespace: an action's report is never JSON, whatever the namespace carries.
    "present show, a namespace asking for JSON": {"kind": "present", "argv": ["p412", "show", "worker", "--slot", "2"], "extra": {"json": True}},
    "present show, the defaults": {"kind": "present", "argv": ["p412", "show", "worker", "--slot", "2"], "defaults": True},
    "recover as the Director": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": {"TICKET_BOARD_CALLER_ROLE": "director"}},
    "recover as the Director, the legacy variable": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": {"PGU_TICKET_BOARD_CALLER_ROLE": " Director "}},
    "recover, the first variable wins": {"kind": "recover", "argv": ["recover-display", "p412"],
                                         "environ": {"TICKET_BOARD_CALLER_ROLE": "worker", "PGU_TICKET_BOARD_CALLER_ROLE": "director"}},
    "recover as the operator": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": {"SYRD412_OPERATOR": "yes"}, "operator": True},
    # The operator whose own role variable names a worker still recovers the Director, never that role.
    "recover as the operator, a worker's role set": {"kind": "recover", "argv": ["recover-display", "p412"],
                                                    "environ": {"SYRD412_OPERATOR": "yes", "TICKET_BOARD_CALLER_ROLE": "worker"}, "operator": True},
    "recover, a worker refused": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": {"TICKET_BOARD_CALLER_ROLE": "worker"},
                                  "grant": {"authorized_user": "syrd412-operator"}},
    "recover, refused without a grant": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": {}, "grant": {}},
    "recover, a display name typed": {"kind": "recover", "argv": ["recover-display", "P412", "Project"], "environ": {"TICKET_BOARD_CALLER_ROLE": "director"}},
    "recover, the process environment": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": None, "process": {"TICKET_BOARD_CALLER_ROLE": "director"}},
    "recover, the process environment refused": {"kind": "recover", "argv": ["recover-display", "p412"], "environ": None, "process": {},
                                                 "grant": {"authorized_user": "syrd412-operator"}},
}
_RUN, _PRINT, _ENV = subprocess.run, print, os.environ


def run_case(t: object, holder: object, controller: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s command: the launcher's seams on `t`, the presentation controller's steps on `controller`."""
    calls: list = []
    said: list = []
    config = SimpleNamespace(project="p412")
    entry = SimpleNamespace(slug="p412", name="P412 Project", config_path=Path("/nonexistent/syrd412/p412.json"))

    def runner(*a, **k):
        raise AssertionError("the runner is only passed on")

    def say(line):
        said.append(line)
        calls.append(["say"])

    def norm(value):
        if isinstance(value, argparse.Namespace):
            return {"namespace": norm(dict(sorted(vars(value).items())))}
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict) or value is _ENV:
            return {str(k): norm(v) for k, v in sorted(dict(value).items()) if value is not _ENV or str(k).startswith(("TICKET_BOARD", "PGU_TICKET", "SYRD412"))}
        for obj, name in ((config, "CONFIG"), (entry, "ENTRY"), (runner, "RUNNER"), (say, "SAY"), (_RUN, "subprocess.run"), (_PRINT, "print")):
            if value is obj:
                return name
        if isinstance(value, Path):
            return f"PATH {value}"
        if callable(value):
            return "ANOTHER CALLABLE"
        return value

    def record(name, answer):
        def call(*args, **kwargs):
            reached.add(name)
            calls.append([name, norm(list(args)), norm(dict(sorted(kwargs.items())))])
            return answer(*args, **kwargs)
        return call

    def printed(report, *, json_output, print_func=_PRINT):
        calls.append(["print_presentation_report", norm(report), json_output, norm(print_func)])
        print_func(f"report {report['n']} json={json_output}")

    def attached(cfg, *, role_name, json_output, runner, print_func):
        print_func(f"attach {role_name} json={json_output}")
        return spec.get("code", 0)

    reports = iter(range(1, 10))
    real_present = t.switchyard_present_command
    on_launcher = {"_resolve_switchyard_project": record("_resolve_switchyard_project", lambda selection: entry),
                   "_load_switchyard_project_config_for_command": record("_load_switchyard_project_config_for_command", lambda e, argv: config),
                   "_tenant_control_grant": record("_tenant_control_grant", lambda project, **k: dict(spec.get("grant", {}))),
                   "current_user_name": record("current_user_name", lambda: "syrd412-caller"),
                   "switchyard_present_command": record("switchyard_present_command", real_present),
                   "_build_switchyard_recover_display_parser": record("_build_switchyard_recover_display_parser", t._build_switchyard_recover_display_parser),
                   "_build_switchyard_present_parser": record("_build_switchyard_present_parser", t._build_switchyard_present_parser)}
    on_controller = {"attach_role_command": record("attach_role_command", attached),
                     "presentation_report": record("presentation_report", lambda cfg, **k: {"n": next(reports)}),
                     "print_presentation_report": printed,
                     "presentation_action": record("presentation_action", lambda cfg, **k: None),
                     "operator_recovery_caller": record("operator_recovery_caller", lambda cfg, env: spec.get("operator", False))}
    saved = [(o, {n: getattr(o, n) for n in d}) for o, d in ((t, on_launcher), (controller, on_controller))]
    kind = spec["kind"]
    # The command under test is taken before any stand-in goes in (the launcher's attribute is wrapped below).
    command = {n: getattr(holder, n) for n in ("switchyard_attach_command", "switchyard_present_command", "switchyard_recover_display_command")}
    # Built before any stand-in goes in, from the launcher's own parsers.
    args = (t._build_switchyard_attach_parser() if kind == "attach" else t._build_switchyard_present_parser()).parse_args(spec["argv"]) if kind != "recover" else None
    for key, value in spec.get("extra", {}).items():
        setattr(args, key, value)
    for o, d in ((t, on_launcher), (controller, on_controller)):
        for n, f in d.items():
            setattr(o, n, f)
    # Every variable of the recorded families leaves the process environment for the case, whatever the running pane
    # carries (SYRD-412 Audit: its TICKET_BOARD_URL, TICKET_BOARD_PANE_* and the like leaked into the golden record);
    # only the case's own pinned values are set, and all are restored afterwards.
    process = {k: _ENV.pop(k) for k in list(_ENV) if k.startswith(("TICKET_BOARD", "PGU_TICKET", "SYRD412"))}
    _ENV.update(spec.get("process", {}))
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            if kind == "attach":
                got = (command["switchyard_attach_command"](config, args=args) if spec.get("defaults")
                       else command["switchyard_attach_command"](config, args=args, runner=runner, print_func=say))
            elif kind == "present":
                got = (command["switchyard_present_command"](config, config_path=entry.config_path, args=args) if spec.get("defaults")
                       else command["switchyard_present_command"](config, config_path=entry.config_path, args=args, runner=runner, print_func=say))
            elif spec["environ"] is None:
                got = command["switchyard_recover_display_command"](spec["argv"])
            else:
                got = command["switchyard_recover_display_command"](spec["argv"], environ=dict(spec["environ"]))
        answer = norm(got)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        answer = {"raised": type(exc).__name__, "message": str(exc)}
    finally:
        for k in spec.get("process", {}):
            _ENV.pop(k, None)
        _ENV.update(process)
        for o, d in saved:
            for n, f in d.items():
                setattr(o, n, f)
    return {"answer": answer, "said": said, "stdout": stdout.getvalue(), "calls": calls}
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
    """Spawns, signals, account lookups and connections refused."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill")), patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(spec: dict) -> dict:
    with contained():
        return json.loads(json.dumps(run_case(t, m, presentation_controller, spec, REACHED)))


def test_the_guard_itself_refuses_a_spawn_a_signal_a_lookup_and_a_connection() -> None:
    for attempt in (lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0),
                    lambda: pwd.getpwuid(0), lambda: socket.socket().connect(("127.0.0.1", 9))):
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, a signal, an account lookup and a connection")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.presentation_commands as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.presentation_commands", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.presentation_commands")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.presentation_commands as m; "
                        "a = inspect.signature(m.switchyard_attach_command).parameters; p = inspect.signature(m.switchyard_present_command).parameters; "
                        "r = inspect.signature(m.switchyard_recover_display_command).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "a['runner'].default is subprocess.run and a['print_func'].default is print and p['runner'].default is subprocess.run "
                        "and p['print_func'].default is print and r['environ'].default is None "
                        "and list(a) == ['config', 'args', 'runner', 'print_func'] and list(p) == ['config', 'config_path', 'args', 'runner', 'print_func'] "
                        "and list(r) == ['argv', 'environ'], "
                        "not hasattr(m, 'ProjectConfig') and not hasattr(m, 'launcher') and not hasattr(m, 'presentation_controller'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.subprocess is subprocess and m.argparse is argparse and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_the_controller_imported_inside() -> None:
    tree = ast.parse((ROOT / "scripts" / "presentation_commands.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        wanted = (["from scripts import team_launcher as launcher"] if expected else []) + ["from scripts import presentation_controller"]
        check(imports == wanted and [ast.unparse(s) for s in node.body[first:first + len(wanted)]] == wanted,
              f"{name}: the launcher (where it reads one), then the controller, first thing when it runs: {imports}")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    recover = next(n for n in tree.body if getattr(n, "name", None) == "switchyard_recover_display_command")
    order = [ast.unparse(c.func).replace("launcher.", "") for c in sorted((c for c in ast.walk(recover) if isinstance(c, ast.Call)), key=lambda c: (c.lineno, c.col_offset))]
    wanted = ["_build_switchyard_recover_display_parser", "_resolve_switchyard_project", "_load_switchyard_project_config_for_command",
              "presentation_controller.operator_recovery_caller", "_tenant_control_grant", "_build_switchyard_present_parser", "switchyard_present_command"]
    check([c for c in order if c in wanted] == wanted, f"the recovery resolves, crosses, decides, then presents -- in that order: {order}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import argparse", "import os", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    check([n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))] == list(MOVED), "the three in the launcher's order, and nothing else")


def test_the_launcher_reexports_the_three_and_dispatches_through_them() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.presentation_commands"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_owner_catalog_args", "switchyard_help_text", "ensure_staged_role_bundle_before_crossing",
                                        "_switchyard_exec_through_tenant_control", "switchyard_main"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and its dispatcher, its own or re-exported")
    calls: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, ast.FunctionDef):
            for x in ast.walk(fn):
                if isinstance(x, ast.Call) and ast.unparse(x.func).split(".")[-1] in MOVED:
                    calls.setdefault(fn.name, {}).setdefault(ast.unparse(x.func), 0)
                    calls[fn.name][ast.unparse(x.func)] += 1
    check(calls == DISPATCH, f"the dispatcher runs all three by its own globals, as often as before: {calls}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_command_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        got = run(spec)
        check(got == GOLDEN[label], f"{label}: the baseline's answer, every line and every call in order: {got}")


def test_a_refused_recovery_presents_nothing() -> None:
    for label, spec in CASES.items():
        if spec["kind"] == "recover" and isinstance(GOLDEN[label]["answer"], dict):
            steps = [c[0] for c in GOLDEN[label]["calls"]]
            check("switchyard_present_command" not in steps and "presentation_action" not in steps and "_tenant_control_grant" in steps,
                  f"{label}: refused after reading the grant, and nothing was presented: {steps}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(expected <= REACHED, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_the_controller_imported_inside", "test_the_launcher_reexports_the_three_and_dispatches_through_them")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"presentation_commands_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
