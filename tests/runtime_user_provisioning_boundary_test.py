#!/usr/bin/env python3
"""SYRD-438: the runtime-user provisioning, against the launcher it came out of.

`ensure_user_linger_runtime` (with `loginctl_enable_linger_args` and the retry
constants `RUNTIME_READY_ATTEMPTS` and `RUNTIME_READY_POLL_SECONDS`),
`ensure_configured_runtime_user` and `provision_runtime_command` -- making sure a
project's runtime user lingers and its runtime directory is ready -- moved
unchanged into `scripts/runtime_user_provisioning.py`; the launcher re-exports
all six. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module, never the launcher; every
  definition-time default -- the two retry constants, `subprocess.run` and
  `time.sleep` -- is the very object the launcher binds and re-exports.
- **Seams (rule 24):** everything they read when they run -- each other, the
  uid lookup, the current user, the runtime-directory path and the session-path
  check -- is read through the launcher as often as before, so a patch there
  reaches it: every case below stands them in there, and rebinds the linger
  arguments and runtime path there to see the effect.
- **Callers:** `main` calls `ensure_configured_runtime_user` and
  `provision_runtime_command` by the launcher's names, and `launch_phases.py`
  reads the first there.
- **The behaviour is the baseline's:** a runtime ready at once, after polling,
  or never; an empty or unknown user; linger refused with a reason or silently;
  the default readiness check; the configured user only when its sessions live
  under its runtime; `provision-runtime`'s choice of user, directly and through
  `main`. `GOLDEN` below was produced by running the BASELINE launcher's own
  definitions over the very cases embedded here (`gold438.py`), not typed; it is
  byte-identical under `env -i`, in a normal role pane, with another HOME, USER
  and COLUMNS, and under umask 077.

Nothing reaches the host: no `loginctl`, `sudo` or systemd is run, and no
account or runtime directory is created -- the runtime directory is a path in a
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
from scripts import runtime_user_provisioning as m  # noqa: E402

CHECKS = 0
MOVED = ('RUNTIME_READY_ATTEMPTS', 'RUNTIME_READY_POLL_SECONDS', 'loginctl_enable_linger_args', 'ensure_user_linger_runtime', 'ensure_configured_runtime_user', 'provision_runtime_command')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'ensure_user_linger_runtime': {'loginctl_enable_linger_args': 1, 'runtime_dir_for_uid': 1, 'uid_for_user': 1},
    'ensure_configured_runtime_user': {'current_user_name': 1, 'ensure_user_linger_runtime': 1, 'session_dir_uses_user_runtime': 1},
    'provision_runtime_command': {'current_user_name': 2, 'ensure_user_linger_runtime': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the six that names them, and how often.
DISPATCH = {'main': {'provision_runtime_command': 1, 'ensure_configured_runtime_user': 1}}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/launch_phases.py': ['launcher.ensure_configured_runtime_user']}
#: The BASELINE's own behaviour for the cases below (`gold438.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'linger: ready at once': {'result': 'PATH TMP/run-user-4381', 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'linger: ready after two polls': {'result': 'PATH TMP/run-user-4381', 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.1], {}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.1], {}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'linger: never ready': {'result': {'raised': 'SystemExit', 'message': "team-launcher: linger is enabled for 'p438-agent', but TMP/run-user-4381 is still missing; start or restart that user's systemd user manager and retry"}, 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.25], {}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.25], {}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.25], {}]], 'printed': []},
    'linger: no attempts still polls once': {'result': {'raised': 'SystemExit', 'message': "team-launcher: linger is enabled for 'p438-agent', but TMP/run-user-4381 is still missing; start or restart that user's systemd user manager and retry"}, 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.1], {}]], 'printed': []},
    'linger: a user name with spaces around it': {'result': 'PATH TMP/run-user-4381', 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'linger: an empty user': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: cannot provision runtime for an empty user name'}, 'calls': [], 'printed': []},
    'linger: an unknown user': {'result': {'raised': 'SystemExit', 'message': "team-launcher: cannot provision runtime for unknown user 'p438-ghost'"}, 'calls': [['uid_for_user', ['p438-ghost'], {}]], 'printed': []},
    'linger: linger refused, with a reason': {'result': {'raised': 'SystemExit', 'message': "team-launcher: failed to enable linger for 'p438-agent': Access denied; run `sudo loginctl enable-linger p438-agent` and retry"}, 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}]], 'printed': []},
    'linger: linger refused, silently': {'result': {'raised': 'SystemExit', 'message': "team-launcher: failed to enable linger for 'p438-agent'; run `sudo loginctl enable-linger p438-agent` and retry"}, 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}]], 'printed': []},
    'linger: the runtime check left to the default': {'result': 'PATH TMP/run-user-4381', 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}]], 'printed': []},
    'linger: the runtime check left to the default, the directory missing': {'result': {'raised': 'SystemExit', 'message': "team-launcher: linger is enabled for 'p438-agent', but TMP/run-user-4381 is still missing; start or restart that user's systemd user manager and retry"}, 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['sleeper', [0.1], {}], ['sleeper', [0.1], {}]], 'printed': []},
    'linger: the linger arguments and runtime path rebound on the launcher': {'result': 'PATH TMP/elsewhere', 'calls': [['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['syrd438-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/elsewhere'], {}]], 'printed': []},
    "configured: the project's own user, its sessions under the runtime": {'result': 'PATH TMP/run-user-4381', 'calls': [['session_dir_uses_user_runtime', ['PATH TMP/run-user-4381/sessions', 'p438-agent'], {}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['ensure_user_linger_runtime', ['p438-agent'], {'attempts': 50, 'runner': 'STAND-IN', 'runtime_exists': 'STAND-IN', 'sleeper': 'STAND-IN'}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'configured: the current user when the project names none': {'result': 'PATH TMP/run-user-4381', 'calls': [['current_user_name', [], {}], ['session_dir_uses_user_runtime', ['PATH TMP/run-user-4381/sessions', 'p438-current'], {}], ['uid_for_user', ['p438-current'], {}], ['runtime_dir_for_uid', [4381], {}], ['ensure_user_linger_runtime', ['p438-current'], {'attempts': 50, 'runner': 'STAND-IN', 'runtime_exists': 'STAND-IN', 'sleeper': 'STAND-IN'}], ['uid_for_user', ['p438-current'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-current'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-current']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'configured: sessions elsewhere: nothing to provision': {'result': None, 'calls': [['session_dir_uses_user_runtime', ['PATH TMP/sessions', 'p438-agent'], {}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}]], 'printed': []},
    'configured: the session dir is the runtime dir itself': {'result': 'PATH TMP/run-user-4381', 'calls': [['session_dir_uses_user_runtime', ['PATH TMP/run-user-4381', 'p438-agent'], {}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['ensure_user_linger_runtime', ['p438-agent'], {'attempts': 50, 'runner': 'STAND-IN', 'runtime_exists': 'STAND-IN', 'sleeper': 'STAND-IN'}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'configured: no user at all': {'result': None, 'calls': [['current_user_name', [], {}]], 'printed': []},
    'configured: an unknown user': {'result': None, 'calls': [['session_dir_uses_user_runtime', ['PATH TMP/run-user-4381/sessions', 'p438-ghost'], {}], ['uid_for_user', ['p438-ghost'], {}]], 'printed': []},
    'configured: the attempts passed on': {'result': 'PATH TMP/run-user-4381', 'calls': [['session_dir_uses_user_runtime', ['PATH TMP/run-user-4381/sessions', 'p438-agent'], {}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['ensure_user_linger_runtime', ['p438-agent'], {'attempts': 7, 'runner': 'STAND-IN', 'runtime_exists': 'STAND-IN', 'sleeper': 'STAND-IN'}], ['uid_for_user', ['p438-agent'], {}], ['runtime_dir_for_uid', [4381], {}], ['loginctl_enable_linger_args', ['p438-agent'], {}], ['runner', [['loginctl', 'enable-linger', 'p438-agent']], {'stderr': -1, 'stdout': -1, 'text': True}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}], ['sleeper', [0.1], {}], ['runtime_exists', ['PATH TMP/run-user-4381'], {}]], 'printed': []},
    'provision-runtime: the named user': {'result': 0, 'calls': [['ensure_user_linger_runtime', ['p438-named'], {}]], 'printed': ['runtime ready for p438-named: TMP/run-user-4381']},
    "provision-runtime: no name: the project's user": {'result': 0, 'calls': [['ensure_user_linger_runtime', ['p438-agent'], {}]], 'printed': ['runtime ready for p438-agent: TMP/run-user-4381']},
    'provision-runtime: no name, the project names none: the current user': {'result': 0, 'calls': [['current_user_name', [], {}], ['ensure_user_linger_runtime', ['p438-current'], {}]], 'printed': ['runtime ready for p438-current: TMP/run-user-4381']},
    'provision-runtime: no name and no project: the current user': {'result': 0, 'calls': [['current_user_name', [], {}], ['ensure_user_linger_runtime', ['p438-current'], {}]], 'printed': ['runtime ready for p438-current: TMP/run-user-4381']},
    'provision-runtime: the helper refuses': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: syrd438 the helper refuses'}, 'calls': [['ensure_user_linger_runtime', ['p438-named'], {}]], 'printed': []},
    'the linger arguments': {'result': ['loginctl', 'enable-linger', 'p438-agent'], 'calls': [], 'printed': []},
    'team-launcher provision-runtime, through main': {'result': 0, 'calls': [['_resolve_launcher_project_config', ['p438'], {'explicit_config': None}], ['load_project_config', ['p438', 'PATH TMP/p438.json'], {}], ['provision_runtime_command', ['p438-named', 'NAMESPACE config'], {}], ['ensure_user_linger_runtime', ['p438-named'], {}]], 'printed': ['runtime ready for p438-named: TMP/run-user-4381']},
    "team-launcher provision-runtime, through main, the project's user": {'result': 0, 'calls': [['_resolve_launcher_project_config', ['p438'], {'explicit_config': None}], ['load_project_config', ['p438', 'PATH TMP/p438.json'], {}], ['provision_runtime_command', [None, 'NAMESPACE config'], {}], ['ensure_user_linger_runtime', ['p438-agent'], {}]], 'printed': ['runtime ready for p438-agent: TMP/run-user-4381']},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold438.py` (which ran them on the baseline) ------------------------------------
# A case provisions a synthetic runtime user. Nothing reaches the host: the uid lookup, the current user and the
# runtime-directory path are stand-ins on the launcher (the runtime directory is a path in a test-owned tree), the
# runner answers from the case and never runs `loginctl`, `sudo` or systemd, the readiness check and the sleeper are
# recorders, and no account or runtime directory is created on the host. The session-path check runs for real, reading
# the stood-in uid and runtime directory through the launcher. Recorded, in order: every call with its arguments,
# everything printed, and the result or the exact refusal.
LINGER = {
    "ready at once": {"user": "p438-agent"},
    "ready after two polls": {"user": "p438-agent", "ready_after": 2},
    "never ready": {"user": "p438-agent", "ready_after": 99, "kw": {"attempts": 3, "poll_seconds": 0.25}},
    "no attempts still polls once": {"user": "p438-agent", "ready_after": 99, "kw": {"attempts": 0}},
    "a user name with spaces around it": {"user": "  p438-agent  "},
    "an empty user": {"user": "   "},
    "an unknown user": {"user": "p438-ghost"},
    "linger refused, with a reason": {"user": "p438-agent", "linger": (1, "  Access denied  ")},
    "linger refused, silently": {"user": "p438-agent", "linger": (1, "")},
    "the runtime check left to the default": {"user": "p438-agent", "default_exists": True, "dir_exists": True},
    "the runtime check left to the default, the directory missing": {"user": "p438-agent", "default_exists": True, "kw": {"attempts": 2}},
    "the linger arguments and runtime path rebound on the launcher": {"user": "p438-agent", "launcher": {"loginctl_enable_linger_args": "ARGS", "runtime_dir_for_uid": "RUNDIR"}},
}
CONFIGURED = {
    "the project's own user, its sessions under the runtime": {"run_as_user": "p438-agent", "session": "RUNTIME/sessions"},
    "the current user when the project names none": {"run_as_user": "", "session": "RUNTIME/sessions"},
    "sessions elsewhere: nothing to provision": {"run_as_user": "p438-agent", "session": "@/sessions"},
    "the session dir is the runtime dir itself": {"run_as_user": "p438-agent", "session": "RUNTIME"},
    "no user at all": {"run_as_user": "", "current": "", "session": "RUNTIME/sessions"},
    "an unknown user": {"run_as_user": "p438-ghost", "session": "RUNTIME/sessions"},
    "the attempts passed on": {"run_as_user": "p438-agent", "session": "RUNTIME/sessions", "kw": {"attempts": 7}, "ready_after": 1},
}
PROVISION = {
    "the named user": {"user": "  p438-named  ", "config": None},
    "no name: the project's user": {"user": None, "config": "p438-agent"},
    "no name, the project names none: the current user": {"user": "", "config": ""},
    "no name and no project: the current user": {"user": None, "config": None},
    "the helper refuses": {"user": "p438-named", "config": None, "ensure": "refuse"},
}
CASES = {
    **{f"linger: {k}": {"call": "linger", **v} for k, v in LINGER.items()},
    **{f"configured: {k}": {"call": "configured", **v} for k, v in CONFIGURED.items()},
    **{f"provision-runtime: {k}": {"call": "provision", **v} for k, v in PROVISION.items()},
    "the linger arguments": {"call": "args", "user": "p438-agent"},
    "team-launcher provision-runtime, through main": {"call": "main", "argv": ["p438", "provision-runtime", "--runtime-user", "p438-named"]},
    "team-launcher provision-runtime, through main, the project's user": {"call": "main", "argv": ["p438", "provision-runtime"]},
}
FUNCTIONS = ("loginctl_enable_linger_args", "ensure_user_linger_runtime", "ensure_configured_runtime_user", "provision_runtime_command")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions, in a fresh test-owned tree; every host-facing name a stand-in on `t`."""
    import contextlib, shutil, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    printed: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd438-")).resolve()
    runtime = tmp / "run-user-4381"

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
        if callable(value) and "run_case.<locals>" in getattr(value, "__qualname__", ""):
            return "STAND-IN"  # the case's own recorder, whatever module the case text was loaded as
        if callable(value):
            return f"CALLABLE {getattr(value, '__module__', '?')}.{getattr(value, '__qualname__', '?')}"
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    place = lambda v: _P(str(v).replace("RUNTIME", str(runtime)).replace("@", str(tmp))) if isinstance(v, str) else v
    names = [*FUNCTIONS, "uid_for_user", "runtime_dir_for_uid", "current_user_name", "session_dir_uses_user_runtime",
             "_resolve_launcher_project_config", "load_project_config"]
    saved = {n: getattr(t, n) for n in names}
    try:
        under = {n: getattr(holder, n) for n in FUNCTIONS}
        if spec.get("dir_exists"):
            runtime.mkdir()

        def passthrough(name):
            def f(*args, **kwargs):
                note(name, *args, **kwargs)
                return saved[name](*args, **kwargs)
            return f

        def uid(user):
            note("uid_for_user", user)
            return None if user.endswith("ghost") or not user else 4381

        def runner(argv, **kwargs):
            note("runner", list(argv), **kwargs)
            code, err = spec.get("linger", (0, ""))
            return SimpleNamespace(returncode=code, stdout="", stderr=err)

        polls = [0]

        def exists(path):
            note("runtime_exists", path)
            polls[0] += 1
            return polls[0] > spec.get("ready_after", 0)

        def ensure(user, **kwargs):
            note("ensure_user_linger_runtime", user, **kwargs)
            if spec.get("ensure") == "refuse":
                raise SystemExit("team-launcher: syrd438 the helper refuses")
            return runtime

        on_launcher = {n: passthrough(n) for n in (*FUNCTIONS, "session_dir_uses_user_runtime")}
        on_launcher.update(
            uid_for_user=uid,
            runtime_dir_for_uid=lambda u: note("runtime_dir_for_uid", u) or runtime,
            current_user_name=lambda: note("current_user_name") or spec.get("current", "p438-current"),
            _resolve_launcher_project_config=lambda project, explicit_config=None: note("_resolve_launcher_project_config", project, explicit_config=explicit_config)
            or SimpleNamespace(label="resolved", config_path=tmp / "p438.json", slug="p438"),
            load_project_config=lambda project, path: note("load_project_config", project, path)
            or SimpleNamespace(label="config", run_as_user="p438-agent", session_dir=runtime / "sessions"),
        )
        if spec["call"] in ("provision", "main"):
            on_launcher["ensure_user_linger_runtime"] = ensure
        stand = {"ARGS": lambda user: note("loginctl_enable_linger_args", user) or ["syrd438-linger", user],
                 "RUNDIR": lambda u: note("runtime_dir_for_uid", u) or tmp / "elsewhere"}
        on_launcher.update({k: stand[v] for k, v in spec.get("launcher", {}).items()})
        for n, f in on_launcher.items():
            setattr(t, n, f)
        call = spec["call"]
        sleeper = lambda seconds: note("sleeper", seconds)
        try:
            with contextlib.redirect_stdout(SimpleNamespace(write=lambda s: printed.append(norm(s)) if s.strip() else None, flush=lambda: None)):
                if call == "args":
                    got = under["loginctl_enable_linger_args"](spec["user"])
                elif call == "linger":
                    kw = dict(spec.get("kw", {}))
                    if not spec.get("default_exists"):
                        kw["runtime_exists"] = exists
                    got = under["ensure_user_linger_runtime"](spec["user"], runner=runner, sleeper=sleeper, **kw)
                elif call == "configured":
                    config = SimpleNamespace(label="config", run_as_user=spec["run_as_user"], session_dir=place(spec["session"]))
                    got = under["ensure_configured_runtime_user"](config, runner=runner, runtime_exists=exists, sleeper=sleeper, **spec.get("kw", {}))
                elif call == "provision":
                    config = None if spec["config"] is None else SimpleNamespace(label="config", run_as_user=spec["config"])
                    got = under["provision_runtime_command"](spec["user"], config)
                else:
                    (tmp / "p438.json").write_text("{}", encoding="utf-8")
                    got = t.main(list(spec["argv"]))
            result = norm(got)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        return {"result": result, "calls": calls, "printed": printed}
    finally:
        for n, f in saved.items():
            setattr(t, n, f)
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
    result = python("import sys, scripts.runtime_user_provisioning as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.runtime_user_provisioning", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.runtime_user_provisioning")):
        result = python("import importlib, inspect, subprocess, time; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.runtime_user_provisioning as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty and v.default is not None), "
                        "[p['runner'].default is subprocess.run and p['sleeper'].default is time.sleep and p['attempts'].default is t.RUNTIME_READY_ATTEMPTS "
                        "for p in (inspect.signature(m.ensure_user_linger_runtime).parameters, inspect.signature(m.ensure_configured_runtime_user).parameters)] "
                        "+ [inspect.signature(m.ensure_user_linger_runtime).parameters['poll_seconds'].default is t.RUNTIME_READY_POLL_SECONDS], "
                        "not hasattr(m, 'launcher'))")
        check(result.stdout.strip() == "True ['ensure_configured_runtime_user.attempts', 'ensure_configured_runtime_user.runner', 'ensure_configured_runtime_user.sleeper', "
              "'ensure_user_linger_runtime.attempts', 'ensure_user_linger_runtime.poll_seconds', 'ensure_user_linger_runtime.runner', 'ensure_user_linger_runtime.sleeper'] "
              "[True, True, True] True", f"{' then '.join(order)}: every default the object it was: {result.stdout}{result.stderr[-600:]}")
    import time
    import typing
    check(m.subprocess is subprocess and m.time is time and m.Path is Path and m.Callable is typing.Callable,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "runtime_user_provisioning.py").read_text(encoding="utf-8"))
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
    check(top == ["from __future__ import annotations", "import subprocess", "import time", "from pathlib import Path", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, and the config type for annotations only: {top} {tc}")
    thresholds = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)}
    check(thresholds == {"RUNTIME_READY_ATTEMPTS": 50, "RUNTIME_READY_POLL_SECONDS": 0.1} and m.RUNTIME_READY_ATTEMPTS is t.RUNTIME_READY_ATTEMPTS,
          f"the two retry constants are plain literals, bound as defaults when the functions are defined, as before: {thresholds}")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the two retry constants and the four functions in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_six_and_its_callers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.runtime_user_provisioning"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the six, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"main", "default_user_bin", "role_isolation_gaps", "DETACHED_SESSION_STABILITY_SECONDS", "NO_LAUNCHER_SELF_DEPLOY_ENV"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"main calls two of them by their launcher globals, exactly as often as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads the runtime check through the launcher: {got}")


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

    def kw(label, seam):
        return next(c for c in GOLDEN[label]["calls"] if c[0] == seam)

    check(result("the linger arguments") == ["loginctl", "enable-linger", "p438-agent"]
          and kw("linger: ready at once", "runner")[1] == [["loginctl", "enable-linger", "p438-agent"]]
          and kw("linger: ready at once", "runner")[2] == {"stderr": -1, "stdout": -1, "text": True},
          "linger is enabled with `loginctl enable-linger <user>`, its output captured as text")
    check(result("linger: ready at once") == "PATH TMP/run-user-4381" and steps("linger: ready after two polls").count("sleeper") == 2
          and all(c[1] == [0.1] for c in GOLDEN["linger: ready after two polls"]["calls"] if c[0] == "sleeper"),
          "the runtime directory is returned once it exists, polled every 0.1 seconds by default")
    never = GOLDEN["linger: never ready"]
    check(steps("linger: never ready").count("runtime_exists") == 3 and all(c[1] == [0.25] for c in never["calls"] if c[0] == "sleeper")
          and never["result"]["message"] == "team-launcher: linger is enabled for 'p438-agent', but TMP/run-user-4381 is still missing; "
          "start or restart that user's systemd user manager and retry"
          and steps("linger: no attempts still polls once").count("runtime_exists") == 1,
          "it polls the given number of times (at least once), then refuses, saying what to do")
    check(result("linger: an empty user")["message"] == "team-launcher: cannot provision runtime for an empty user name" and steps("linger: an empty user") == []
          and result("linger: an unknown user")["message"] == "team-launcher: cannot provision runtime for unknown user 'p438-ghost'"
          and "runner" not in steps("linger: an unknown user")
          and result("linger: linger refused, with a reason")["message"] == "team-launcher: failed to enable linger for 'p438-agent': Access denied; "
          "run `sudo loginctl enable-linger p438-agent` and retry"
          and result("linger: linger refused, silently")["message"] == "team-launcher: failed to enable linger for 'p438-agent'; run `sudo loginctl enable-linger p438-agent` and retry"
          and kw("linger: a user name with spaces around it", "uid_for_user")[1] == ["p438-agent"],
          "each refusal, word for word; the user name trimmed; nothing is run for an empty or unknown user")
    check(result("linger: the runtime check left to the default") == "PATH TMP/run-user-4381"
          and steps("linger: the runtime check left to the default, the directory missing").count("sleeper") == 2
          and result("linger: the linger arguments and runtime path rebound on the launcher") == "PATH TMP/elsewhere"
          and kw("linger: the linger arguments and runtime path rebound on the launcher", "runner")[1] == [["syrd438-linger", "p438-agent"]],
          "the default readiness check is the directory's existence; the linger arguments and runtime path are the launcher's when it runs")
    check(result("configured: sessions elsewhere: nothing to provision") is None and result("configured: no user at all") is None
          and result("configured: an unknown user") is None and result("configured: the session dir is the runtime dir itself") == "PATH TMP/run-user-4381"
          and steps("configured: the current user when the project names none")[0] == "current_user_name"
          and kw("configured: the attempts passed on", "ensure_user_linger_runtime")[2]["attempts"] == 7,
          "the configured runtime user is provisioned only when the session directory lives under that user's runtime")
    check([GOLDEN[f"provision-runtime: {k}"]["printed"] for k in ("the named user", "no name: the project's user", "no name and no project: the current user")]
          == [["runtime ready for p438-named: TMP/run-user-4381"], ["runtime ready for p438-agent: TMP/run-user-4381"], ["runtime ready for p438-current: TMP/run-user-4381"]]
          and result("provision-runtime: the named user") == 0 and kw("provision-runtime: the named user", "ensure_user_linger_runtime")[1] == ["p438-named"],
          "provision-runtime: the named user (trimmed), else the project's, else the current user; says where the runtime is")
    check(steps("team-launcher provision-runtime, through main")[:4] == ["_resolve_launcher_project_config", "load_project_config", "provision_runtime_command", "ensure_user_linger_runtime"]
          and GOLDEN["team-launcher provision-runtime, through main, the project's user"]["printed"] == ["runtime ready for p438-agent: TMP/run-user-4381"],
          "main dispatches provision-runtime to the launcher's name with the resolved project's config")


def test_every_launcher_seam_is_reached() -> None:
    # Every function the four read is a recorder or stand-in on the launcher (the four themselves included), and the
    # linger arguments and runtime path are rebound there by their own case.
    names = {name for reads in SEAMS.values() for name in reads}
    check(names <= REACHED, f"a recorder on the launcher reached every function: missing {sorted(names - REACHED)}")
    rebound = {name for spec in CASES.values() for name in spec.get("launcher", {})}
    check(rebound == {"loginctl_enable_linger_args", "runtime_dir_for_uid"}, f"and the linger arguments and runtime path rebound: {sorted(rebound)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_six_and_its_callers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"runtime_user_provisioning_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
