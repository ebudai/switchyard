#!/usr/bin/env python3
"""SYRD-461: the owner file handoff and ownership helpers, against the launcher they came out of.

The five -- `_path_owner_label`, `_path_owner_ids`, `chown_owner_file_args`,
`ensure_owner_file` and `_chown_project_file` -- moved unchanged into
`scripts/owner_files.py`; the launcher re-exports them all and keeps
`_proc_failure_reason` and `_control_repository_owned_roots`. This pins what
makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher, and imports the
  config type only for annotations.
- **Seams (rule 24):** what `ensure_owner_file` reads of the launcher -- the
  current user, the argv helper and the error-text helper -- is read through it
  as often as before, so a patch there reaches it.
- **Readers:** no launcher definition names them, and every production module
  reads them through the launcher, as often as before.
- **The behaviour is the baseline's:** the lstat owner reads (a dangling
  symlink is the link's own owner) and their fallbacks, the chown argv and its
  refusal without an owner, the root-and-not-already-owner condition, the
  runner call, and both refusal texts. `GOLDEN` below was produced by running
  the BASELINE launcher's own definitions over the very cases embedded here
  (`gold461.py`), not typed; it is byte-identical under `env -i`, in a normal
  role pane, with another HOME, USER and COLUMNS, under umask 077, under
  several hash seeds and with another TMPDIR and locale.

Nothing is ever chowned and no account is looked up for real: the account and
group lookups, the effective uid and the current user are stand-ins, and the
runner is a recorder. The owner reads look only at a synthetic tree in a fresh
test-owned directory. Spawns, every exec, signals, account and group lookups
and socket connections are refused for each case.
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
from scripts import owner_files as m  # noqa: E402

CHECKS = 0
MOVED = ('_path_owner_label', '_path_owner_ids', 'chown_owner_file_args', 'ensure_owner_file', '_chown_project_file')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'ensure_owner_file': {'_proc_failure_reason': 1, 'chown_owner_file_args': 1, 'current_user_name': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the five that names them, and how often.
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/generated_layout_upgrade.py': {'launcher.ensure_owner_file': 2}, 'scripts/model_validation.py': {'launcher.ensure_owner_file': 1}, 'scripts/new_project_phases.py': {'launcher._chown_project_file': 1}, 'scripts/presentation_controller.py': {'team_launcher.ensure_owner_file': 2}, 'scripts/project_onboarding.py': {'launcher._chown_project_file': 3}, 'scripts/project_role_add.py': {'launcher.ensure_owner_file': 4}, 'scripts/project_vcs_close_role.py': {'launcher.ensure_owner_file': 1}, 'scripts/project_worktrees.py': {'launcher._path_owner_ids': 2, 'launcher._path_owner_label': 1}, 'scripts/role_runtime.py': {'team_launcher.ensure_owner_file': 1}, 'scripts/role_visibility.py': {'launcher.ensure_owner_file': 1}, 'scripts/workflow_launcher.py': {'launcher.ensure_owner_file': 1}, 'scripts/workflow_manage.py': {'launcher.ensure_owner_file': 1}}
#: The BASELINE's own behaviour for the cases below (`gold461.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'label: a file': {'result': {'type': 'str', 'value': 'p461-user:p461-group'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: a directory': {'result': {'type': 'str', 'value': 'p461-user:p461-group'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: a symlink, not followed': {'result': {'type': 'str', 'value': 'p461-user:p461-group'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: a dangling symlink, not followed': {'result': {'type': 'str', 'value': 'p461-user:p461-group'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: a missing path': {'result': {'type': 'str', 'value': "unreadable ([Errno 2] No such file or directory: 'TMP/missing')"}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: an unknown user': {'result': {'type': 'str', 'value': 'uid UID:p461-group'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: an unknown group': {'result': {'type': 'str', 'value': 'p461-user:gid GID'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: both unknown': {'result': {'type': 'str', 'value': 'uid UID:gid GID'}, 'calls': [['pwd.getpwuid', ['UID'], {}], ['grp.getgrgid', ['GID'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: distinct synthetic ids': {'result': {'type': 'str', 'value': 'p461-owner:p461-owner-group'}, 'calls': [['lstat', [], {}], ['pwd.getpwuid', [1461], {}], ['grp.getgrgid', [2461], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'label: distinct synthetic ids, the group unknown': {'result': {'type': 'str', 'value': 'p461-owner:gid 2461'}, 'calls': [['lstat', [], {}], ['pwd.getpwuid', [1461], {}], ['grp.getgrgid', [2461], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ids: a file': {'result': {'type': 'tuple', 'value': ['UID', 'GID']}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ids: distinct synthetic ids': {'result': {'type': 'tuple', 'value': [1461, 2461]}, 'calls': [['lstat', [], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ids: a dangling symlink, not followed': {'result': {'type': 'tuple', 'value': ['UID', 'GID']}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ids: a missing path': {'result': {'type': 'NoneType', 'value': None}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'args: an owner': {'result': {'type': 'list', 'value': ['chown', 'p461-agent:p461-agent', 'TMP/file']}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'args: a path with a space': {'result': {'type': 'list', 'value': ['chown', 'p461-agent:p461-agent', 'TMP/a dir/state file.json']}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'args: no owner': {'result': {'raised': 'ValueError', 'message': 'file ownership repair requires run_as_user'}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: no owner': {'result': {'type': 'NoneType', 'value': None}, 'calls': [], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: the caller is the owner': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['current_user_name', [], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: not root': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: root, handed over': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}], ['chown_owner_file_args', [{'run_as_user': 'p461-agent', 'project': 'p461'}, 'PATH TMP/file'], {}], ['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: root, the chown fails with stderr': {'result': {'raised': 'SystemExit', 'message': "team-launcher: failed to assign generated file TMP/file to p461-agent: chown: changing ownership of 'x': Operation not permitted"}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}], ['chown_owner_file_args', [{'run_as_user': 'p461-agent', 'project': 'p461'}, 'PATH TMP/file'], {}], ['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}], ['_proc_failure_reason', [{'returncode': 1, 'stdout': '', 'stderr': "chown: changing ownership of 'x':\n  Operation not permitted\n\n"}, 'chown failed with exit 1'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: root, the chown fails with bytes': {'result': {'raised': 'SystemExit', 'message': "team-launcher: failed to assign generated file TMP/file to p461-agent: chown: invalid user: �'p461-agent'"}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}], ['chown_owner_file_args', [{'run_as_user': 'p461-agent', 'project': 'p461'}, 'PATH TMP/file'], {}], ['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}], ['_proc_failure_reason', [{'returncode': 1, 'stdout': '', 'stderr': "BYTES chown: invalid user: �'p461-agent'"}, 'chown failed with exit 1'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: root, the chown fails silently': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: failed to assign generated file TMP/file to p461-agent: chown failed with exit 2'}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}], ['chown_owner_file_args', [{'run_as_user': 'p461-agent', 'project': 'p461'}, 'PATH TMP/file'], {}], ['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}], ['_proc_failure_reason', [{'returncode': 2, 'stdout': '', 'stderr': '  \n'}, 'chown failed with exit 2'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: the argv helper rebound on the launcher': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}], ['chown_owner_file_args', [{'run_as_user': 'p461-agent', 'project': 'p461'}, 'PATH TMP/file'], {}], ['runner', [['P461-CHOWN', 'TMP/file']], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'ensure: the error helper rebound on the launcher': {'result': {'raised': 'SystemExit', 'message': 'team-launcher: failed to assign generated file TMP/file to p461-agent: P461-REASON'}, 'calls': [['current_user_name', [], {}], ['os.geteuid', [], {}], ['chown_owner_file_args', [{'run_as_user': 'p461-agent', 'project': 'p461'}, 'PATH TMP/file'], {}], ['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}], ['_proc_failure_reason', [{'returncode': 3, 'stdout': '', 'stderr': 'boom'}, 'chown failed with exit 3'], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'project file: handed over': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'project file: the chown fails': {'result': {'raised': 'SystemExit', 'message': 'switchyard: failed to assign TMP/file to p461-agent'}, 'calls': [['runner', [['chown', 'p461-agent:p461-agent', 'TMP/file']], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
    'project file: an empty owner': {'result': {'type': 'NoneType', 'value': None}, 'calls': [['runner', [['chown', ':', 'TMP/file']], {}]], 'tree kept': ['dangling', 'dir', 'file', 'link-to-file']},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold461.py` (which ran them on the baseline) ------------------------------------
# A case calls one of the five and records the answer or the exact exception, and, in order, every call it made: the
# account and group lookups, the current user, the effective uid, the runner and the launcher helpers. Nothing is ever
# chowned and no account is looked up for real: `pwd.getpwuid`, `grp.getgrgid` and `os.geteuid` are stand-ins that
# answer synthetic names and uids, the current user is a stand-in on the launcher, and the runner is a recorder that
# returns the case's exit status and stderr. The owner reads look at a synthetic tree in a fresh test-owned directory
# (a file, a directory, a symlink to the file, a dangling symlink); the test's own uid and gid are recorded as UID and
# GID by position, never by value, since they may be equal on one host and not on another. A synthetic path object
# answers `lstat` with distinct ids (1461, 2461) and refuses `stat`, so the uid and gid can never be confused.
CASES = {
    "label: a file": {"call": "_path_owner_label", "path": "file"},
    "label: a directory": {"call": "_path_owner_label", "path": "dir"},
    "label: a symlink, not followed": {"call": "_path_owner_label", "path": "link-to-file"},
    "label: a dangling symlink, not followed": {"call": "_path_owner_label", "path": "dangling"},
    "label: a missing path": {"call": "_path_owner_label", "path": "missing"},
    "label: an unknown user": {"call": "_path_owner_label", "path": "file", "unknown": ["user"]},
    "label: an unknown group": {"call": "_path_owner_label", "path": "file", "unknown": ["group"]},
    "label: both unknown": {"call": "_path_owner_label", "path": "file", "unknown": ["user", "group"]},
    "label: distinct synthetic ids": {"call": "_path_owner_label", "path": "SYNTHETIC"},
    "label: distinct synthetic ids, the group unknown": {"call": "_path_owner_label", "path": "SYNTHETIC", "unknown": ["group"]},
    "ids: a file": {"call": "_path_owner_ids", "path": "file"},
    "ids: distinct synthetic ids": {"call": "_path_owner_ids", "path": "SYNTHETIC"},
    "ids: a dangling symlink, not followed": {"call": "_path_owner_ids", "path": "dangling"},
    "ids: a missing path": {"call": "_path_owner_ids", "path": "missing"},
    "args: an owner": {"call": "chown_owner_file_args", "owner": "p461-agent", "path": "file"},
    "args: a path with a space": {"call": "chown_owner_file_args", "owner": "p461-agent", "path": "a dir/state file.json"},
    "args: no owner": {"call": "chown_owner_file_args", "owner": "", "path": "file"},
    "ensure: no owner": {"call": "ensure_owner_file", "owner": "", "caller": "switchyard461", "euid": 0, "path": "file"},
    "ensure: the caller is the owner": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "p461-agent", "euid": 0, "path": "file"},
    "ensure: not root": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 1461, "path": "file"},
    "ensure: root, handed over": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 0, "path": "file"},
    "ensure: root, the chown fails with stderr": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 0, "path": "file",
                                                  "returncode": 1, "stderr": "chown: changing ownership of 'x':\n  Operation not permitted\n\n"},
    "ensure: root, the chown fails with bytes": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 0, "path": "file",
                                                 "returncode": 1, "stderr": b"chown: invalid user: \xff'p461-agent'"},
    "ensure: root, the chown fails silently": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 0, "path": "file",
                                               "returncode": 2, "stderr": "  \n"},
    "ensure: the argv helper rebound on the launcher": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 0, "path": "file",
                                                        "rebind": {"chown_owner_file_args": True}},
    "ensure: the error helper rebound on the launcher": {"call": "ensure_owner_file", "owner": "p461-agent", "caller": "switchyard461", "euid": 0, "path": "file",
                                                         "returncode": 3, "stderr": "boom", "rebind": {"_proc_failure_reason": True}},
    "project file: handed over": {"call": "_chown_project_file", "owner": "p461-agent", "path": "file"},
    "project file: the chown fails": {"call": "_chown_project_file", "owner": "p461-agent", "path": "file", "returncode": 1, "stderr": "not permitted"},
    "project file: an empty owner": {"call": "_chown_project_file", "owner": "", "path": "file"},
}
FUNCTIONS = ("_path_owner_label", "_path_owner_ids", "chown_owner_file_args", "ensure_owner_file", "_chown_project_file")
PASSED = ("chown_owner_file_args", "_proc_failure_reason")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition, over a synthetic tree; account lookups, uid, current user and runner stood in."""
    import grp, os, pwd, re, shutil, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd461-")).resolve()
    uid, gid = os.getuid(), os.getgid()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(str(tmp), "TMP")
        if isinstance(value, SimpleNamespace):
            return {k: norm(v) for k, v in vars(value).items()}
        if isinstance(value, bytes):
            return "BYTES " + value.decode("utf-8", errors="replace")
        if isinstance(value, (bool, int, float)) or value is None:
            return value
        if isinstance(value, str):
            s = value.replace(str(tmp), "TMP")
            return re.sub(rf"\b(uid|gid) ({uid}|{gid})\b", lambda m: f"{m.group(1)} {'UID' if m.group(1) == 'uid' else 'GID'}", s)
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    saved = {n: getattr(t, n) for n in (*FUNCTIONS, *PASSED, "current_user_name")}
    saved_os = {"geteuid": os.geteuid}
    saved_pwd, saved_grp = pwd.getpwuid, grp.getgrgid
    try:
        (tmp / "file").write_text("x\n", encoding="utf-8")
        (tmp / "dir").mkdir()
        (tmp / "link-to-file").symlink_to(tmp / "file")
        (tmp / "dangling").symlink_to(tmp / "nowhere")
        unknown = spec.get("unknown", [])

        synthetic = spec["path"] == "SYNTHETIC"

        def getpwuid(n):
            note("pwd.getpwuid", n if synthetic else "UID" if n == uid else n)
            names = {1461: "p461-owner"} if synthetic else {uid: "p461-user"}
            if "user" in unknown or n not in names:
                raise KeyError(f"getpwuid(): uid not found: {n}")
            return SimpleNamespace(pw_name=names[n], pw_uid=n)

        def getgrgid(n):
            note("grp.getgrgid", n if synthetic else "GID" if n == gid else n)
            names = {2461: "p461-owner-group"} if synthetic else {gid: "p461-group"}
            if "group" in unknown or n not in names:
                raise KeyError(f"getgrgid(): gid not found: {n}")
            return SimpleNamespace(gr_name=names[n], gr_gid=n)

        class SyntheticPath:
            """A path whose own entry says uid 1461, gid 2461; following it is refused."""

            def lstat(self):
                note("lstat")
                return SimpleNamespace(st_uid=1461, st_gid=2461)

            def stat(self, *args, **kwargs):
                note("stat")
                raise OSError(40, "followed a link it should not have")

            def __str__(self):
                return "SYNTHETIC"
        pwd.getpwuid, grp.getgrgid = getpwuid, getgrgid
        os.geteuid = lambda: note("os.geteuid") or spec.get("euid", 1461)
        t.current_user_name = lambda: note("current_user_name") or spec.get("caller", "switchyard461")
        for name in PASSED:
            setattr(t, name, (lambda name: lambda *a, **k: note(name, *a, **k) or saved[name](*a, **k))(name))
        for name in spec.get("rebind", {}):
            if name == "chown_owner_file_args":
                t.chown_owner_file_args = lambda config, path: note("chown_owner_file_args", config, path) or ["P461-CHOWN", str(path)]
            elif name == "_proc_failure_reason":
                t._proc_failure_reason = lambda proc, fallback: note("_proc_failure_reason", proc, fallback) or "P461-REASON"

        def runner(argv, *args, **kwargs):
            note("runner", argv, *args, **kwargs)
            return SimpleNamespace(returncode=spec.get("returncode", 0), stdout="", stderr=spec.get("stderr", ""))
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        path = SyntheticPath() if synthetic else tmp / spec["path"]
        config = SimpleNamespace(run_as_user=spec.get("owner", ""), project="p461")
        if spec["call"] in ("_path_owner_label", "_path_owner_ids"):
            args, kwargs = [path], {}
        elif spec["call"] == "chown_owner_file_args":
            args, kwargs = [config, path], {}
        elif spec["call"] == "ensure_owner_file":
            args, kwargs = [config, path], {"runner": runner}
        else:
            args, kwargs = [], {"owner_user": spec["owner"], "path": path, "runner": runner}
        try:
            got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        else:
            kind = type(got).__name__
            if spec["call"] == "_path_owner_ids" and got is not None and not synthetic:
                # By position: the test's own uid and gid may be equal on one host and not on another.
                got = ["UID" if got[0] == uid else got[0], "GID" if got[1] == gid else got[1]]
            result = {"type": kind, "value": norm(got)}
        return {"result": result, "calls": calls, "tree kept": sorted(p.name for p in tmp.iterdir())}
    finally:
        os.geteuid = saved_os["geteuid"]
        pwd.getpwuid, grp.getgrgid = saved_pwd, saved_grp
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


#: What the Director kept on the launcher, beside the five.
KEPT = ("_proc_failure_reason", "_control_repository_owned_roots")


def annotation_ids(tree: ast.AST) -> set[int]:
    """Every node inside an annotation, or inside a TYPE_CHECKING block: names there are not read when the code runs."""
    out: set[int] = set()
    for x in ast.walk(tree):
        parts = []
        if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parts = [x.returns, *(a.annotation for a in x.args.posonlyargs + x.args.args + x.args.kwonlyargs + [v for v in (x.args.vararg, x.args.kwarg) if v])]
        elif isinstance(x, ast.AnnAssign):
            parts = [x.annotation]
        elif isinstance(x, ast.If) and "TYPE_CHECKING" in ast.unparse(x.test):
            parts = [x]
        for part in parts:
            if part is not None:
                out |= {id(y) for y in ast.walk(part)}
    return out


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.owner_files as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.owner_files", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.owner_files")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.owner_files as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"[getattr(m, n).__module__ for n in {MOVED!r}], "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'ProjectConfig', 'current_user_name', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.owner_files', 'scripts.owner_files', 'scripts.owner_files', 'scripts.owner_files', 'scripts.owner_files'] True",
              f"{' then '.join(order)}: one object each, defined here; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    import typing
    check(m.grp is grp and m.os is os and m.pwd is pwd and m.subprocess is subprocess and m.Path is pathlib.Path and m.Any is typing.Any
          and m.Callable is typing.Callable and m.TYPE_CHECKING is False
          and t.grp is m.grp and t.os is m.os and t.pwd is m.pwd and t.subprocess is m.subprocess and t.Path is m.Path and t.Callable is m.Callable,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "owner_files.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, its sibling included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check((imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0]) if expected else imports == [],
              f"{name}: the launcher imported first thing (after its docstring) when it reads one, and nothing else imported: {imports}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import grp", "import os", "import pwd", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.project_identity import ProjectConfig"],
          f"the standard library, and the config type for annotations only: {top} {tc}")
    defaults = {n.name: [ast.unparse(d) for d in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]] for n in tree.body if isinstance(n, ast.FunctionDef)}
    check(defaults == {"_path_owner_label": [], "_path_owner_ids": [], "chown_owner_file_args": [], "ensure_owner_file": [], "_chown_project_file": []},
          f"no defaults at all; the runner is keyword-only and required: {defaults}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the five, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.owner_files"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the five, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | set(KEPT) | set(DISPATCH) <= defined | exported,
          "the launcher defines none of them, and keeps _proc_failure_reason, _control_repository_owned_roots and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"the launcher's own definitions name them exactly as often as before -- none: {uses}")
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
        skip = annotation_ids(source)
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED and id(x) not in skip})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads them through the launcher, as often as before: {got} {bare}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


#: The owner prefix the baseline builds: an expected argv prefix, compared and never run.
OWNER_PREFIX = ["sudo", "-u"]


#: The command word the baseline builds: an expected argv head, compared and never run.
CHOWN = "chown"


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def value(label):
        return result(label)["value"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    check(value("label: a file") == value("label: a directory") == value("label: a symlink, not followed") == "p461-user:p461-group"
          and value("label: an unknown user") == "uid UID:p461-group" and value("label: an unknown group") == "p461-user:gid GID"
          and value("label: both unknown") == "uid UID:gid GID"
          and value("label: a missing path") == "unreadable ([Errno 2] No such file or directory: 'TMP/missing')" and calls("label: a missing path") == [],
          "the owner label: user:group by name, uid N or gid N when unknown, 'unreadable (...)' when the path cannot be read")
    check(value("label: a dangling symlink, not followed") == "p461-user:p461-group" and value("ids: a dangling symlink, not followed") == ["UID", "GID"]
          and value("ids: a file") == ["UID", "GID"] and result("ids: a file")["type"] == "tuple" and value("ids: a missing path") is None,
          "both owner reads use lstat: a dangling symlink is the link's own owner, not an error; a missing path is None")
    check(value("label: distinct synthetic ids") == "p461-owner:p461-owner-group" and value("label: distinct synthetic ids, the group unknown") == "p461-owner:gid 2461"
          and value("ids: distinct synthetic ids") == [1461, 2461] and result("ids: distinct synthetic ids")["type"] == "tuple"
          and calls("label: distinct synthetic ids") == ["lstat", "pwd.getpwuid", "grp.getgrgid"] and calls("ids: distinct synthetic ids") == ["lstat"],
          "with distinct ids: the user from st_uid, the group from st_gid, the ids a (uid, gid) tuple, each read once with lstat and never stat")
    check(value("args: an owner") == [CHOWN, "p461-agent:p461-agent", "TMP/file"]
          and value("args: a path with a space") == [CHOWN, "p461-agent:p461-agent", "TMP/a dir/state file.json"]
          and result("args: no owner") == {"raised": "ValueError", "message": "file ownership repair requires run_as_user"},
          "the handoff argv: <owner>:<owner> and the path as one argument; refused without an owner")
    check(calls("ensure: no owner") == [] and calls("ensure: the caller is the owner") == ["current_user_name"]
          and calls("ensure: not root") == ["current_user_name", "os.geteuid"]
          and all(value(k) is None for k in ("ensure: no owner", "ensure: the caller is the owner", "ensure: not root", "ensure: root, handed over"))
          and calls("ensure: root, handed over") == ["current_user_name", "os.geteuid", "chown_owner_file_args", "runner"]
          and GOLDEN["ensure: root, handed over"]["calls"][-1][1] == [[CHOWN, "p461-agent:p461-agent", "TMP/file"]],
          "ensure: nothing without an owner, when the caller is the owner, or when not root; else the launcher's argv through the runner")
    PREFIX = "team-launcher: failed to assign generated file TMP/file to p461-agent: "
    check(result("ensure: root, the chown fails with stderr")["message"] == PREFIX + "chown: changing ownership of 'x': Operation not permitted"
          and result("ensure: root, the chown fails silently")["message"] == PREFIX + "chown failed with exit 2"
          and result("ensure: root, the chown fails with bytes")["raised"] == "SystemExit"
          and result("ensure: the error helper rebound on the launcher")["message"] == PREFIX + "P461-REASON"
          and GOLDEN["ensure: the argv helper rebound on the launcher"]["calls"][-1][1] == [["P461-CHOWN", "TMP/file"]],
          "a failed handoff exits with the launcher's error text (stderr on one line, else the exit status); the argv and error helpers are read through it")
    check(value("project file: handed over") is None and GOLDEN["project file: handed over"]["calls"] == [["runner", [[CHOWN, "p461-agent:p461-agent", "TMP/file"]], {}]]
          and result("project file: the chown fails") == {"raised": "SystemExit", "message": "switchyard: failed to assign TMP/file to p461-agent"}
          and GOLDEN["project file: an empty owner"]["calls"][0][1] == [[CHOWN, ":", "TMP/file"]],
          "a project file: the same argv for the named owner (an empty one not refused, pinned), and a fixed refusal on a non-zero exit")
    check(all(GOLDEN[k]["tree kept"] == ["dangling", "dir", "file", "link-to-file"] for k in GOLDEN),
          "nothing in the synthetic tree was created, removed or renamed")


def test_every_launcher_seam_is_reached() -> None:
    # Every name ensure_owner_file reads on the launcher is a recorder or stand-in there.
    names = {name for reads in SEAMS.values() for name in reads}
    check(names == {"current_user_name", "chown_owner_file_args", "_proc_failure_reason"} and names <= REACHED,
          f"a recorder on the launcher reached every function: missing {sorted(names - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"owner_files_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
