#!/usr/bin/env python3
"""SYRD-445: the launch-time owner CLI checks, against the launcher they came out of.

`_format_missing_cli_launch_failure`, `stop_before_launch_for_missing_owner_clis`
and `run_switchyard_launch_first_run_auth` -- first-run sign-in before a launch,
and refusing to launch when a configured CLI is missing -- moved unchanged into
`scripts/launch_owner_clis.py`; the launcher re-exports all three. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; the
  defaults -- `subprocess.run` and the builtin `print` -- are the same objects.
- **Seams (rule 24):** everything they read when they run -- each other, the
  current user, the first-run phase and its report type, the owner's home for
  auth, the install-command table and the owner reminder -- is read through the
  launcher as often as before, so a patch there reaches it: every case below
  records or stands them in there, and rebinds each there to see the effect.
- **Callers:** `switchyard_main` and `switchyard_validate_models_command` call
  them by the launcher's names; `new_project_phases.py` and
  `workflow_launcher.py` read them through the launcher when they run.
- **The behaviour is the baseline's:** the missing-CLI failure text -- one or
  several CLIs, no owner, an unknown CLI, empty lists; the stop, printing it
  once; and first-run auth for the project's owner, the caller, or nobody, with
  every option passed on and the phase's refusal raised. `GOLDEN` below was
  produced by running the BASELINE launcher's own definitions over the very
  cases embedded here (`gold445.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077 and under several hash seeds. The vendor install commands are
  recorded by name, so this file holds no installer string.

Nothing reaches a provider: the first-run phase, the owner's home lookup and
the current user are stand-ins; nobody signs in and no pane is launched.
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
from scripts import launch_owner_clis as m  # noqa: E402

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
MOVED = ('_format_missing_cli_launch_failure', 'stop_before_launch_for_missing_owner_clis', 'run_switchyard_launch_first_run_auth')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_format_missing_cli_launch_failure': {'AGENT_CLI_INSTALL_COMMANDS': 1, '_owner_user_cli_reminder': 1},
    'stop_before_launch_for_missing_owner_clis': {'_format_missing_cli_launch_failure': 1},
    'run_switchyard_launch_first_run_auth': {'FirstRunAuthReport': 1, '_owner_home_for_auth': 1, 'current_user_name': 1, 'run_first_run_auth_phase': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the three that names them, and how often.
DISPATCH = {'switchyard_validate_models_command': {'run_switchyard_launch_first_run_auth': 1}, 'switchyard_main': {'run_switchyard_launch_first_run_auth': 1, 'stop_before_launch_for_missing_owner_clis': 1}}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/new_project_phases.py': {'launcher.stop_before_launch_for_missing_owner_clis': 1}, 'scripts/workflow_launcher.py': {'launcher.run_switchyard_launch_first_run_auth': 1}}
#: The BASELINE's own behaviour for the cases below (`gold445.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'format: one missing CLI': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: claude (roles: main). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   claude  <install command for claude>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'format: two missing CLIs, several roles': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: codex (roles: main, app); claude (roles: director). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   codex   <install command for codex>\nswitchyard:   claude  <install command for claude>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'format: no owner': {'result': "switchyard: cannot launch panes because required CLI(s) are missing: claude (roles: main). Install the missing CLI(s) and rerun switchyard.\nswitchyard: panes run as the project's owner user, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   claude  <install command for claude>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', [''], {}]], 'printed': []},
    'format: a CLI with no install command': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: some-future-cli (roles: main); codex (roles: app). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   some-future-cli  see that vendor's own installation documentation\nswitchyard:   codex            <install command for codex>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'format: nothing missing': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: . Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'format: a role list that is empty': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: agy (roles: ). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   agy  <install command for agy>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'format: the install table rebound on the launcher': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: claude (roles: main). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   claude  syrd445 install claude\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'format: the reminder rebound on the launcher': {'result': "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: claude (roles: main). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nsyrd445 reminder for p445-agent\nswitchyard:   claude  <install command for claude>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", 'type': 'str', 'calls': [['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': []},
    'stop: nothing missing': {'result': False, 'type': 'bool', 'calls': [], 'printed': []},
    'stop: one missing': {'result': True, 'type': 'bool', 'calls': [['_format_missing_cli_launch_failure', [{'dataclass': 'FirstRunAuthReport', 'unauthenticated_roles': {}, 'untrusted_roles': [], 'stale_codex_hook_trust': [], 'missing_cli_roles': {'codex': ['main']}, 'unknown_model_roles': [], 'model_validation_failures': [], 'owner_user': 'p445-agent', 'owner_shell_issue': None, 'github_identity': None, 'authenticated_now': {}, 'incomplete_provider_setup': []}], {}], ['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': ["switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: codex (roles: main). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.\nswitchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.\nswitchyard:   codex  <install command for codex>\nswitchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch."]},
    'stop: the default printer': {'result': True, 'type': 'bool', 'calls': [['_format_missing_cli_launch_failure', [{'dataclass': 'FirstRunAuthReport', 'unauthenticated_roles': {}, 'untrusted_roles': [], 'stale_codex_hook_trust': [], 'missing_cli_roles': {'codex': ['main']}, 'unknown_model_roles': [], 'model_validation_failures': [], 'owner_user': 'p445-agent', 'owner_shell_issue': None, 'github_identity': None, 'authenticated_now': {}, 'incomplete_provider_setup': []}], {}], ['_owner_user_cli_reminder', ['p445-agent'], {}]], 'printed': ['switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: codex (roles: main). Install the missing CLI(s) for owner user p445-agent and rerun switchyard.', 'switchyard: panes run as owner user p445-agent, which does not inherit a CLI installed only for the user running switchyard. Install it host-wide once -- or, if you already have a private copy, let switchyard promote that executable to a root-owned host-wide copy when it offers, which every later project reuses.', 'switchyard:   codex  <install command for codex>', "switchyard: switchyard never fetches or runs a vendor's installer, so these commands are yours to run. It can promote an executable you already have to a host-wide copy; that offer is made before launch.", '']},
    'stop: the formatter rebound on the launcher': {'result': True, 'type': 'bool', 'calls': [['_format_missing_cli_launch_failure', [{'dataclass': 'FirstRunAuthReport', 'unauthenticated_roles': {}, 'untrusted_roles': [], 'stale_codex_hook_trust': [], 'missing_cli_roles': {'codex': ['main']}, 'unknown_model_roles': [], 'model_validation_failures': [], 'owner_user': 'p445-agent', 'owner_shell_issue': None, 'github_identity': None, 'authenticated_now': {}, 'incomplete_provider_setup': []}], {}]], 'printed': ['syrd445 formatted']},
    "auth: the project's own owner": {'result': "NS the phase's report", 'type': 'SimpleNamespace', 'calls': [['_owner_home_for_auth', ['p445-owner'], {}], ['run_first_run_auth_phase', ['NS config'], {'owner_user': 'p445-owner', 'owner_home': '/nonexistent/syrd445/home/p445-owner', 'validate_models': False, 'runner': 'the real subprocess.run', 'foreground_runner': None, 'print_func': 'the builtin print'}]], 'printed': []},
    'auth: the owner padded': {'result': "NS the phase's report", 'type': 'SimpleNamespace', 'calls': [['_owner_home_for_auth', ['p445-owner'], {}], ['run_first_run_auth_phase', ['NS config'], {'owner_user': 'p445-owner', 'owner_home': '/nonexistent/syrd445/home/p445-owner', 'validate_models': False, 'runner': 'the real subprocess.run', 'foreground_runner': None, 'print_func': 'the builtin print'}]], 'printed': []},
    'auth: the caller when the project names none': {'result': "NS the phase's report", 'type': 'SimpleNamespace', 'calls': [['current_user_name', [], {}], ['_owner_home_for_auth', ['p445-caller'], {}], ['run_first_run_auth_phase', ['NS config'], {'owner_user': 'p445-caller', 'owner_home': '/nonexistent/syrd445/home/p445-caller', 'validate_models': False, 'runner': 'the real subprocess.run', 'foreground_runner': None, 'print_func': 'the builtin print'}]], 'printed': []},
    'auth: no owner at all': {'result': {'dataclass': 'FirstRunAuthReport', 'unauthenticated_roles': {}, 'untrusted_roles': [], 'stale_codex_hook_trust': [], 'missing_cli_roles': {}, 'unknown_model_roles': [], 'model_validation_failures': [], 'owner_user': '', 'owner_shell_issue': None, 'github_identity': None, 'authenticated_now': {}, 'incomplete_provider_setup': []}, 'type': 'FirstRunAuthReport', 'calls': [['current_user_name', [], {}], ['FirstRunAuthReport', [{}, []], {}]], 'printed': []},
    'auth: every option passed on': {'result': "NS the phase's report", 'type': 'SimpleNamespace', 'calls': [['_owner_home_for_auth', ['p445-owner'], {}], ['run_first_run_auth_phase', ['NS config'], {'owner_user': 'p445-owner', 'owner_home': '/nonexistent/syrd445/home/p445-owner', 'validate_models': True, 'runner': 'STAND-IN RUNNER', 'foreground_runner': 'STAND-IN FOREGROUND', 'print_func': 'STAND-IN PRINT'}]], 'printed': []},
    'auth: the phase refuses': {'result': {'raised': 'SystemExit', 'message': 'switchyard: syrd445 the first-run phase refuses'}, 'calls': [['_owner_home_for_auth', ['p445-owner'], {}], ['run_first_run_auth_phase', ['NS config'], {'owner_user': 'p445-owner', 'owner_home': '/nonexistent/syrd445/home/p445-owner', 'validate_models': False, 'runner': 'the real subprocess.run', 'foreground_runner': None, 'print_func': 'the builtin print'}]], 'printed': []},
    'auth: the defaults': {'result': "NS the phase's report", 'type': 'SimpleNamespace', 'calls': [['_owner_home_for_auth', ['p445-owner'], {}], ['run_first_run_auth_phase', ['NS config'], {'owner_user': 'p445-owner', 'owner_home': '/nonexistent/syrd445/home/p445-owner', 'validate_models': False, 'runner': 'the real subprocess.run', 'foreground_runner': None, 'print_func': 'the builtin print'}]], 'printed': []},
    'auth: the report type rebound on the launcher': {'result': 'NS a rebound report', 'type': 'SimpleNamespace', 'calls': [['current_user_name', [], {}], ['FirstRunAuthReport', [{}, []], {}]], 'printed': []},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold445.py` (which ran them on the baseline) ------------------------------------
# A case asks the launch-time owner CLI checks about a synthetic project. The first-run phase, the owner's home lookup
# and the current user are stand-ins on the launcher, so no provider is started, nobody signs in and no pane is launched;
# the owner reminder and the report type are the launcher's own, recorded there and passed through; the install-command
# table may be rebound there. The vendor install commands are recorded by name, never copied into the record, so this
# file holds no installer string. Recorded, in order: every call through the launcher with its arguments, everything
# printed, and the answer, or the exact exception.
FORMAT = {
    "one missing CLI": {"missing": {"claude": ["main"]}, "owner": "p445-agent"},
    "two missing CLIs, several roles": {"missing": {"codex": ["main", "app"], "claude": ["director"]}, "owner": "p445-agent"},
    "no owner": {"missing": {"claude": ["main"]}, "owner": ""},
    "a CLI with no install command": {"missing": {"some-future-cli": ["main"], "codex": ["app"]}, "owner": "p445-agent"},
    "nothing missing": {"missing": {}, "owner": "p445-agent"},
    "a role list that is empty": {"missing": {"agy": []}, "owner": "p445-agent"},
    "the install table rebound on the launcher": {"missing": {"claude": ["main"]}, "owner": "p445-agent", "launcher": {"AGENT_CLI_INSTALL_COMMANDS": {"claude": "syrd445 install claude"}}},
    "the reminder rebound on the launcher": {"missing": {"claude": ["main"]}, "owner": "p445-agent", "launcher": {"_owner_user_cli_reminder": "REMINDER"}},
}
STOP = {
    "nothing missing": {"missing": {}, "owner": "p445-agent"},
    "one missing": {"missing": {"codex": ["main"]}, "owner": "p445-agent"},
    "the default printer": {"missing": {"codex": ["main"]}, "owner": "p445-agent", "default_print": True},
    "the formatter rebound on the launcher": {"missing": {"codex": ["main"]}, "owner": "p445-agent", "launcher": {"_format_missing_cli_launch_failure": "FORMAT"}},
}
AUTH = {
    "the project's own owner": {"run_as_user": "p445-owner", "current": "p445-caller"},
    "the owner padded": {"run_as_user": "  p445-owner  ", "current": "p445-caller"},
    "the caller when the project names none": {"run_as_user": "", "current": "p445-caller"},
    "no owner at all": {"run_as_user": "", "current": "   "},
    "every option passed on": {"run_as_user": "p445-owner", "current": "p445-caller", "kw": {"validate_models": True, "runner": "RUNNER", "foreground_runner": "FOREGROUND", "print_func": "PRINT"}},
    "the phase refuses": {"run_as_user": "p445-owner", "current": "p445-caller", "phase": "refuse"},
    "the defaults": {"run_as_user": "p445-owner", "current": "p445-caller", "defaults": True},
    "the report type rebound on the launcher": {"run_as_user": "", "current": "", "launcher": {"FirstRunAuthReport": "REPORT"}},
}
CASES = {
    **{f"format: {k}": {"call": "format", **v} for k, v in FORMAT.items()},
    **{f"stop: {k}": {"call": "stop", **v} for k, v in STOP.items()},
    **{f"auth: {k}": {"call": "auth", **v} for k, v in AUTH.items()},
}
FUNCTIONS = ("_format_missing_cli_launch_failure", "stop_before_launch_for_missing_owner_clis", "run_switchyard_launch_first_run_auth")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions; every host-facing name a stand-in on `t`, the launcher."""
    import contextlib, dataclasses, io, subprocess
    from types import SimpleNamespace
    calls: list = []
    printed: list = []
    table = dict(t.AGENT_CLI_INSTALL_COMMANDS)

    def scrub(text):
        for cli, command in table.items():
            text = text.replace(command, f"<install command for {cli}>")
        return text

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, str):
            return scrub(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, SimpleNamespace):
            return f"NS {getattr(value, 'label', '?')}"
        if dataclasses.is_dataclass(value):
            return {"dataclass": type(value).__qualname__, **{f.name: norm(getattr(value, f.name)) for f in dataclasses.fields(value)}}
        if callable(value) and "run_case.<locals>" in getattr(value, "__qualname__", ""):
            return f"STAND-IN {value.__name__}"
        if getattr(value, "__module__", "") == "subprocess" and getattr(value, "__name__", "") == "run":
            return "the real subprocess.run"
        if value is print:
            return "the builtin print"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    names = [*FUNCTIONS, "AGENT_CLI_INSTALL_COMMANDS", "_owner_user_cli_reminder", "current_user_name", "run_first_run_auth_phase", "_owner_home_for_auth", "FirstRunAuthReport"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            f.__name__ = name
            return f

        def phase(config, **kwargs):
            note("run_first_run_auth_phase", config, **kwargs)
            if spec.get("phase") == "refuse":
                raise SystemExit("switchyard: syrd445 the first-run phase refuses")
            return SimpleNamespace(label="the phase's report", missing_cli_roles={})

        def RUNNER(*a, **k): return None
        def FOREGROUND(*a, **k): return None
        def PRINT(*a, **k): return None
        stand = {"REMINDER": lambda owner="": note("_owner_user_cli_reminder", owner) or f"syrd445 reminder for {owner}",
                 "FORMAT": lambda report: note("_format_missing_cli_launch_failure", report) or "syrd445 formatted",
                 "REPORT": lambda *a, **k: note("FirstRunAuthReport", *a, **k) or SimpleNamespace(label="a rebound report")}
        t._owner_user_cli_reminder = passthrough("_owner_user_cli_reminder")
        t._format_missing_cli_launch_failure = passthrough("_format_missing_cli_launch_failure")
        t.FirstRunAuthReport = passthrough("FirstRunAuthReport")
        t.run_first_run_auth_phase = phase
        t._owner_home_for_auth = lambda owner: note("_owner_home_for_auth", owner) or f"/nonexistent/syrd445/home/{owner}"
        t.current_user_name = lambda: note("current_user_name") or spec.get("current", "")
        for k, v in spec.get("launcher", {}).items():
            setattr(t, k, stand[v] if isinstance(v, str) else v)
        if "launcher" in spec and "AGENT_CLI_INSTALL_COMMANDS" in spec["launcher"]:
            table = dict(saved["AGENT_CLI_INSTALL_COMMANDS"])
        call = spec["call"]
        try:
            if call in ("format", "stop"):
                report = saved["FirstRunAuthReport"](unauthenticated_roles={}, untrusted_roles=[], missing_cli_roles=dict(spec["missing"]), owner_user=spec["owner"])
                if call == "format":
                    got = under["_format_missing_cli_launch_failure"](report)
                elif spec.get("default_print"):
                    shown = io.StringIO()
                    with contextlib.redirect_stdout(shown):
                        got = under["stop_before_launch_for_missing_owner_clis"](report)
                    printed.extend(norm(shown.getvalue()).split("\n"))
                else:
                    got = under["stop_before_launch_for_missing_owner_clis"](report, print_func=lambda s: printed.append(norm(s)))
            else:
                config = SimpleNamespace(label="config", run_as_user=spec["run_as_user"])
                kw = {k: {"RUNNER": RUNNER, "FOREGROUND": FOREGROUND, "PRINT": PRINT}.get(v, v) for k, v in spec.get("kw", {}).items()}
                got = under["run_switchyard_launch_first_run_auth"](config, **kw)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": norm(str(exc))}, "calls": calls, "printed": printed}
        return {"result": norm(got), "type": type(got).__name__, "calls": calls, "printed": printed}
    finally:
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


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.launch_owner_clis as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.launch_owner_clis", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.launch_owner_clis")):
        result = python("import builtins, importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.launch_owner_clis as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty), "
                        "[p['print_func'].default is builtins.print for p in (inspect.signature(m.stop_before_launch_for_missing_owner_clis).parameters, "
                        "inspect.signature(m.run_switchyard_launch_first_run_auth).parameters)], "
                        "[(p['runner'].default is subprocess.run, p['foreground_runner'].default, p['validate_models'].default) "
                        "for p in [inspect.signature(m.run_switchyard_launch_first_run_auth).parameters]], "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'ProjectConfig', 'FirstRunAuthReport', 'AGENT_CLI_INSTALL_COMMANDS', 'run_first_run_auth_phase')))")
        check(result.stdout.strip() == "True ['run_switchyard_launch_first_run_auth.foreground_runner', 'run_switchyard_launch_first_run_auth.print_func', "
              "'run_switchyard_launch_first_run_auth.runner', 'run_switchyard_launch_first_run_auth.validate_models', 'stop_before_launch_for_missing_owner_clis.print_func'] "
              "[True, True] [(True, None, False)] True",
              f"{' then '.join(order)}: one set of objects; the defaults the same objects; nothing of the launcher, the phase or the table bound at load: "
              f"{result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.subprocess is subprocess and m.Any is typing.Any and m.Callable is typing.Callable,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "launch_owner_clis.py").read_text(encoding="utf-8"))
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
        check(imports == (["from scripts import team_launcher as launcher"] if expected else [])
              and (not expected or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and nothing else imported: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.first_run_auth import FirstRunAuthReport\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, and the report and config types for annotations only: {top} {tc}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the three in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_three_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.launch_owner_clis"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    # Annotated constants count too: the install table is `AGENT_CLI_INSTALL_COMMANDS: dict[str, str] = {...}`.
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"ProjectConfig", "switchyard_main", "switchyard_validate_models_command", "report_first_run_auth_warnings"} <= defined | exported,
          "the launcher defines none of them, and keeps its caller and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in launcher_body(ROOT, tree):
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"switchyard_main and switchyard_validate_models_command call them by their launcher globals, exactly as often as before: {uses}")
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

    def steps(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    one = result("format: one missing CLI").split("\n")
    check(one[0] == "switchyard: cannot launch panes because required CLI(s) are missing for owner user p445-agent: claude (roles: main). "
                    "Install the missing CLI(s) for owner user p445-agent and rerun switchyard."
          and one[1].startswith("switchyard: panes run as owner user p445-agent,") and one[2] == "switchyard:   claude  <install command for claude>"
          and one[3].startswith("switchyard: switchyard never fetches or runs a vendor's installer") and len(one) == 4,
          "the failure: what is missing for whom, the owner reminder, each CLI's own install command, and that switchyard never runs it")
    two = result("format: two missing CLIs, several roles").split("\n")
    check("codex (roles: main, app); claude (roles: director)" in two[0]
          and two[2:4] == ["switchyard:   codex   <install command for codex>", "switchyard:   claude  <install command for claude>"],
          "every missing CLI, in the report's order, its roles joined, the names padded to one column")
    check(result("format: no owner").startswith("switchyard: cannot launch panes because required CLI(s) are missing: claude (roles: main). Install the missing CLI(s) and rerun")
          and "switchyard:   some-future-cli  see that vendor's own installation documentation" in result("format: a CLI with no install command"),
          "no owner, no owner detail; a CLI with no known command points to its vendor's documentation")
    check("syrd445 install claude" in result("format: the install table rebound on the launcher")
          and "syrd445 reminder for p445-agent" in result("format: the reminder rebound on the launcher")
          and all(steps(k) == ["_owner_user_cli_reminder"] for k in GOLDEN if k.startswith("format: ")),
          "the table and the reminder are the launcher's when it runs, the reminder asked once")
    check(result("stop: nothing missing") is False and steps("stop: nothing missing") == []
          and result("stop: one missing") is True and steps("stop: one missing") == ["_format_missing_cli_launch_failure", "_owner_user_cli_reminder"]
          and GOLDEN["stop: one missing"]["printed"] == [result("format: one missing CLI").replace("claude", "codex")]
          and GOLDEN["stop: the default printer"]["printed"][:4] == GOLDEN["stop: one missing"]["printed"][0].split("\n")
          and GOLDEN["stop: the formatter rebound on the launcher"]["printed"] == ["syrd445 formatted"],
          "stop: nothing missing, go on; otherwise print the launcher's failure text once, to print by default, and stop")
    phase = GOLDEN["auth: the project's own owner"]["calls"]
    check(phase == [["_owner_home_for_auth", ["p445-owner"], {}],
                    ["run_first_run_auth_phase", ["NS config"], {"owner_user": "p445-owner", "owner_home": "/nonexistent/syrd445/home/p445-owner", "validate_models": False,
                                                                 "runner": "the real subprocess.run", "foreground_runner": None, "print_func": "the builtin print"}]]
          and result("auth: the project's own owner") == "NS the phase's report",
          "auth: the owner's home, then the first-run phase with every option, its report returned as is")
    check(GOLDEN["auth: the owner padded"]["calls"] == phase and steps("auth: the caller when the project names none")[0] == "current_user_name"
          and GOLDEN["auth: the caller when the project names none"]["calls"][2][2]["owner_user"] == "p445-caller",
          "the project's owner, trimmed, else the caller")
    check(steps("auth: no owner at all") == ["current_user_name", "FirstRunAuthReport"] and result("auth: no owner at all")["missing_cli_roles"] == {}
          and result("auth: the report type rebound on the launcher") == "NS a rebound report",
          "no owner at all: an empty report of the launcher's report type, and no phase run")
    check(GOLDEN["auth: every option passed on"]["calls"][1][2] == {"owner_user": "p445-owner", "owner_home": "/nonexistent/syrd445/home/p445-owner", "validate_models": True,
                                                                     "runner": "STAND-IN RUNNER", "foreground_runner": "STAND-IN FOREGROUND", "print_func": "STAND-IN PRINT"}
          and result("auth: the phase refuses") == {"raised": "SystemExit", "message": "switchyard: syrd445 the first-run phase refuses"},
          "every option passed on as given; the phase's refusal raised, not swallowed")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the three read on the launcher is a recorder or stand-in there; the table is rebound there by its own case.
    names = {name for reads in SEAMS.values() for name in reads} - {"AGENT_CLI_INSTALL_COMMANDS"}
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(names <= REACHED and "AGENT_CLI_INSTALL_COMMANDS" in rebound, f"a recorder on the launcher reached every function: missing {sorted(names - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_three_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"launch_owner_clis_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
