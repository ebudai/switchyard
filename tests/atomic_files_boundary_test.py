#!/usr/bin/env python3
"""SYRD-457: the launcher-owned atomic JSON writers, against the launcher they came out of.

The three -- `_write_json_atomic`, `_ensure_private_dir` and
`_write_private_json_atomic` -- moved unchanged into `scripts/atomic_files.py`;
the launcher re-exports them all and keeps `ensure_owner_file` and
`chown_owner_file_args`. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module
  alone loads no other Switchyard module, never the launcher, and uses only the
  standard library, the very objects the launcher holds.
- **Seam (rule 24):** the private writer reads its directory helper through
  the launcher, as often as before, so a patch there reaches it.
- **Readers:** no launcher definition names them; every production module
  reads them through the launcher, as often as before.
- **The behaviour is the baseline's,** in a synthetic file tree: the JSON bytes,
  the temporary file beside the target, the parents made, each file and
  directory mode, the owner lookup, chown and chmod on the open temporary file
  and their order, an unknown owner, a payload that cannot be written (the
  target untouched; the private writer's temporary file left, as before), a
  symlinked or dangling target, and every best-effort chmod. `GOLDEN` below was
  produced by running the BASELINE launcher's own definitions over the very
  cases embedded here (`gold457.py`), not typed; it is byte-identical under
  `env -i`, in a normal role pane, with another HOME, USER and COLUMNS, under
  umask 077, under several hash seeds and with another TMPDIR.

Ownership is never changed: the effective uid is stood in, the account lookup
answers a synthetic account and the chown is a recorder. No real tenant path is
written. Spawns, every exec, signals, account and group lookups and socket
connections are refused for each case.
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
from scripts import atomic_files as m  # noqa: E402

CHECKS = 0
MOVED = ('_write_json_atomic', '_ensure_private_dir', '_write_private_json_atomic')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_write_private_json_atomic': {'_ensure_private_dir': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the three that names them, and how often.
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
# SYRD-558: role_runtime's tenant-projection writer moved to scripts/runtime_projection.py.
READERS = {'scripts/agy_credential.py': {'launcher._write_json_atomic': 1}, 'scripts/desktop_approval.py': {'launcher._write_json_atomic': 1}, 'scripts/desktop_layout_writer.py': {'launcher._write_private_json_atomic': 1}, 'scripts/generated_layout_upgrade.py': {'launcher._write_json_atomic': 1}, 'scripts/legacy_presentation.py': {'launcher._write_json_atomic': 1}, 'scripts/live_role_runtime.py': {'launcher._write_json_atomic': 1}, 'scripts/model_validation.py': {'launcher._write_json_atomic': 1}, 'scripts/new_project_artifacts.py': {'launcher._write_json_atomic': 3}, 'scripts/new_project_command.py': {'launcher._write_json_atomic': 1}, 'scripts/new_project_phases.py': {'launcher._write_json_atomic': 1}, 'scripts/new_project_support.py': {'launcher._write_json_atomic': 1}, 'scripts/presentation_controller.py': {'team_launcher._write_private_json_atomic': 1}, 'scripts/project_design_command.py': {'launcher._write_json_atomic': 1}, 'scripts/project_desktop.py': {'launcher._write_json_atomic': 2}, 'scripts/project_role_add.py': {'launcher._write_json_atomic': 3}, 'scripts/project_vcs_close_role.py': {'launcher._write_json_atomic': 1}, 'scripts/provider_resume.py': {'launcher._ensure_private_dir': 1}, 'scripts/provider_runtime_state.py': {'launcher._write_json_atomic': 1}, 'scripts/release_rollback.py': {'launcher._write_private_json_atomic': 1}, 'scripts/role_identity_cutover.py': {'launcher._write_json_atomic': 1, 'launcher._write_private_json_atomic': 1}, 'scripts/role_runtime.py': {'team_launcher._write_private_json_atomic': 1}, 'scripts/role_sessions.py': {'launcher._write_private_json_atomic': 1}, 'scripts/role_visibility.py': {'launcher._write_json_atomic': 1}, 'scripts/session_paths.py': {'launcher._write_json_atomic': 1}, 'scripts/session_records.py': {'launcher._ensure_private_dir': 2, 'launcher._write_private_json_atomic': 1}, 'scripts/tenant_release_root.py': {'launcher._write_private_json_atomic': 1}, 'scripts/upstream_report.py': {'launcher._write_json_atomic': 1}}
#: The BASELINE's own behaviour for the cases below (`gold457.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'json: a new file': {'result': None, 'tree': [['state.json', 'file', '0o600', '{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1,\n  "\\u00e9": "\\u00fc"\n}\n']], 'calls': []},
    'json: a missing parent made': {'result': None, 'tree': [['deep', 'dir', '0o755'], ['deep/er', 'dir', '0o755'], ['deep/er/state.json', 'file', '0o600', '{\n  "x": 1\n}\n']], 'calls': []},
    'json: an existing file replaced, its mode not kept': {'result': None, 'tree': [['state.json', 'file', '0o600', '{\n  "x": 2\n}\n']], 'calls': []},
    'json: an owner, not root, a new file': {'result': None, 'tree': [['state.json', 'file', '0o600', '{\n  "x": 3\n}\n']], 'calls': [['os.fchmod', ['0o600'], {}]]},
    'json: an owner, not root, the existing mode kept': {'result': None, 'tree': [['state.json', 'file', '0o640', '{\n  "x": 4\n}\n']], 'calls': [['os.fchmod', ['0o640'], {}]]},
    'json: an owner, root': {'result': None, 'tree': [['state.json', 'file', '0o600', '{\n  "x": 5\n}\n']], 'calls': [['pwd.getpwnam', ['p457-agent'], {}], ['os.fchown', [1457, 1458], {}], ['os.fchmod', ['0o600'], {}]]},
    'json: an owner, root, the existing mode kept': {'result': None, 'tree': [['state.json', 'file', '0o660', '{\n  "x": 6\n}\n']], 'calls': [['pwd.getpwnam', ['p457-agent'], {}], ['os.fchown', [1457, 1458], {}], ['os.fchmod', ['0o660'], {}]]},
    'json: root without an owner': {'result': None, 'tree': [['state.json', 'file', '0o600', '{\n  "x": 7\n}\n']], 'calls': []},
    'json: an owner that does not exist, root': {'result': {'raised': 'KeyError', 'message': '"getpwnam(): name not found: \'nobody457\'"'}, 'tree': [], 'calls': [['pwd.getpwnam', ['nobody457'], {}]]},
    'json: a payload that cannot be written': {'result': {'raised': 'TypeError', 'message': 'Object of type object is not JSON serializable'}, 'tree': [['state.json', 'file', '0o644', '{"old": true}\n']], 'calls': []},
    'json: a payload that cannot be written, an owner': {'result': {'raised': 'TypeError', 'message': 'Object of type object is not JSON serializable'}, 'tree': [], 'calls': [['pwd.getpwnam', ['p457-agent'], {}]]},
    'json: the target a symlink': {'result': None, 'tree': [['real.json', 'file', '0o640', '{"real": true}\n'], ['state.json', 'file', '0o640', '{\n  "x": 9\n}\n']], 'calls': [['os.fchmod', ['0o640'], {}]]},
    'json: the target a dangling symlink': {'result': None, 'tree': [['state.json', 'file', '0o600', '{\n  "x": 10\n}\n']], 'calls': []},
    'private dir: new and nested': {'result': None, 'tree': [['priv', 'dir', '0o755'], ['priv/nested', 'dir', '0o700']], 'calls': []},
    'private dir: an existing open directory': {'result': None, 'tree': [['open', 'dir', '0o700']], 'calls': []},
    'private dir: its mode cannot be set': {'result': None, 'tree': [['stuck', 'dir', '0o755']], 'calls': [['os.chmod refused', ['TMP/stuck', '0o700'], {}]]},
    'private json: a new file': {'result': None, 'tree': [['priv', 'dir', '0o700'], ['priv/state.json', 'file', '0o600', '{\n  "a": 2,\n  "b": 1\n}\n']], 'calls': [['_ensure_private_dir', ['PATH TMP/priv'], {}]]},
    'private json: an existing file and open directory tightened': {'result': None, 'tree': [['open', 'dir', '0o700'], ['open/state.json', 'file', '0o600', '{\n  "x": 11\n}\n']], 'calls': [['_ensure_private_dir', ['PATH TMP/open'], {}]]},
    'private json: modes that cannot be set': {'result': None, 'tree': [['stuck', 'dir', '0o755'], ['stuck/state.json', 'file', '0o600', '{\n  "x": 12\n}\n']], 'calls': [['_ensure_private_dir', ['PATH TMP/stuck'], {}], ['os.chmod refused', ['TMP/stuck', '0o700'], {}], ['os.chmod refused', ['TMP/stuck/.state.json.RANDOM.tmp', '0o600'], {}], ['os.chmod refused', ['TMP/stuck/state.json', '0o600'], {}]]},
    'private json: a payload that cannot be written': {'result': {'raised': 'TypeError', 'message': 'Object of type object is not JSON serializable'}, 'tree': [['priv', 'dir', '0o700'], ['priv/.state.json.RANDOM.tmp', 'file', '0o600', '{\n  "a": 1,\n  "b": ']], 'calls': [['_ensure_private_dir', ['PATH TMP/priv'], {}]]},
    'private json: the directory helper rebound on the launcher': {'result': None, 'tree': [['priv', 'dir', '0o755'], ['priv/state.json', 'file', '0o600', '{\n  "x": 13\n}\n']], 'calls': [['_ensure_private_dir', ['PATH TMP/priv'], {}]]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold457.py` (which ran them on the baseline) ------------------------------------
# A case writes JSON with one of the three into a synthetic file tree in a fresh test-owned directory (umask 022), then
# records the answer or the exact exception, every file and directory left in the tree with its mode (a temporary
# file's random part masked), the published bytes, whether the target is a link, and, in order, every ownership call.
# Ownership is never changed: `os.geteuid` answers the case's uid, `pwd.getpwnam` a synthetic account, and `os.fchown`
# is a recorder; `os.fchmod` is recorded and then applied, so the mode is real. The private-directory helper is the
# launcher's own, recorded there and passed through. No real tenant path is written.
CASES = {
    "json: a new file": {"call": "_write_json_atomic", "payload": {"b": 1, "a": [1, 2], "é": "ü"}},
    "json: a missing parent made": {"call": "_write_json_atomic", "target": "deep/er/state.json", "payload": {"x": 1}},
    "json: an existing file replaced, its mode not kept": {"call": "_write_json_atomic", "existing": 0o644, "payload": {"x": 2}},
    "json: an owner, not root, a new file": {"call": "_write_json_atomic", "owner": "p457-agent", "uid": 1457, "payload": {"x": 3}},
    "json: an owner, not root, the existing mode kept": {"call": "_write_json_atomic", "owner": "p457-agent", "uid": 1457, "existing": 0o640, "payload": {"x": 4}},
    "json: an owner, root": {"call": "_write_json_atomic", "owner": "p457-agent", "uid": 0, "payload": {"x": 5}},
    "json: an owner, root, the existing mode kept": {"call": "_write_json_atomic", "owner": "p457-agent", "uid": 0, "existing": 0o660, "payload": {"x": 6}},
    "json: root without an owner": {"call": "_write_json_atomic", "uid": 0, "payload": {"x": 7}},
    "json: an owner that does not exist, root": {"call": "_write_json_atomic", "owner": "nobody457", "uid": 0, "payload": {"x": 8}},
    "json: a payload that cannot be written": {"call": "_write_json_atomic", "existing": 0o644, "payload": "UNSERIALIZABLE"},
    "json: a payload that cannot be written, an owner": {"call": "_write_json_atomic", "owner": "p457-agent", "uid": 0, "payload": "UNSERIALIZABLE"},
    "json: the target a symlink": {"call": "_write_json_atomic", "symlink": True, "owner": "p457-agent", "uid": 1457, "payload": {"x": 9}},
    "json: the target a dangling symlink": {"call": "_write_json_atomic", "symlink": "dangling", "payload": {"x": 10}},
    "private dir: new and nested": {"call": "_ensure_private_dir", "target": "priv/nested"},
    "private dir: an existing open directory": {"call": "_ensure_private_dir", "target": "open", "existing_dir": 0o755},
    "private dir: its mode cannot be set": {"call": "_ensure_private_dir", "target": "stuck", "existing_dir": 0o755, "chmod_fails": "stuck"},
    "private json: a new file": {"call": "_write_private_json_atomic", "target": "priv/state.json", "payload": {"b": 1, "a": 2}},
    "private json: an existing file and open directory tightened": {"call": "_write_private_json_atomic", "target": "open/state.json", "existing_dir": 0o755,
                                                                     "existing": 0o644, "payload": {"x": 11}},
    "private json: modes that cannot be set": {"call": "_write_private_json_atomic", "target": "stuck/state.json", "existing_dir": 0o755, "chmod_fails": "all",
                                               "payload": {"x": 12}},
    "private json: a payload that cannot be written": {"call": "_write_private_json_atomic", "target": "priv/state.json", "payload": "UNSERIALIZABLE"},
    "private json: the directory helper rebound on the launcher": {"call": "_write_private_json_atomic", "target": "priv/state.json", "payload": {"x": 13},
                                                                    "helper": True},
}
FUNCTIONS = ("_write_json_atomic", "_ensure_private_dir", "_write_private_json_atomic")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definition, in a fresh test-owned tree; ownership calls stood in on os and pwd."""
    import os, pwd, re, shutil, stat as _stat, tempfile
    from pathlib import Path as _P
    from types import SimpleNamespace
    calls: list = []
    tmp = _P(tempfile.mkdtemp(prefix="syrd457-")).resolve()

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        if isinstance(value, _P):
            return "PATH " + str(value).replace(str(tmp), "TMP")
        if isinstance(value, str):
            return re.sub(r"\.([\w.-]+?)\.[A-Za-z0-9_]{6,}\.tmp", r".\1.RANDOM.tmp", value.replace(str(tmp), "TMP"))
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return repr(value)

    def note(seam, /, *args, **kwargs):
        reached.add(seam)
        calls.append([seam, norm(list(args)), norm(dict(kwargs))])

    def tree():
        out = []
        for p in sorted(tmp.rglob("*")):
            info = os.lstat(p)
            kind = "link" if _stat.S_ISLNK(info.st_mode) else "dir" if _stat.S_ISDIR(info.st_mode) else "file"
            entry = [norm(str(p.relative_to(tmp))), kind, oct(_stat.S_IMODE(info.st_mode)) if kind != "link" else "-"]
            if kind == "file":
                entry.append(p.read_text(encoding="utf-8"))
            if kind == "link":
                entry.append(norm(os.readlink(p)))
            out.append(entry)
        return out

    saved = {n: getattr(t, n) for n in FUNCTIONS}
    saved_os = {n: getattr(os, n) for n in ("geteuid", "fchown", "fchmod", "chmod")}
    saved_getpwnam, saved_umask = pwd.getpwnam, os.umask(0o022)
    try:
        target = tmp / spec.get("target", "state.json")
        if "existing_dir" in spec:
            target.parent.mkdir(parents=True, exist_ok=True) if spec["call"] != "_ensure_private_dir" else target.mkdir(parents=True)
            (target if spec["call"] == "_ensure_private_dir" else target.parent).chmod(spec["existing_dir"])
        if "existing" in spec:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{"old": true}\n', encoding="utf-8")
            target.chmod(spec["existing"])
        if spec.get("symlink") is True:
            (tmp / "real.json").write_text('{"real": true}\n', encoding="utf-8")
            (tmp / "real.json").chmod(0o640)
            target.symlink_to(tmp / "real.json")
        elif spec.get("symlink") == "dangling":
            target.symlink_to(tmp / "nowhere.json")
        uid = spec.get("uid", 1457)
        os.geteuid = lambda: uid
        real_fchmod = saved_os["fchmod"]
        os.fchown = lambda fd, owner_uid, owner_gid: note("os.fchown", owner_uid, owner_gid)
        os.fchmod = lambda fd, mode: note("os.fchmod", oct(mode)) or real_fchmod(fd, mode)

        def getpwnam(name):
            note("pwd.getpwnam", name)
            if name != "p457-agent":
                raise KeyError(f"getpwnam(): name not found: {name!r}")
            return SimpleNamespace(pw_uid=1457, pw_gid=1458, pw_name=name)
        pwd.getpwnam = getpwnam
        failing = spec.get("chmod_fails")
        if failing:
            def chmod(path, mode, *args, **kwargs):
                if failing == "all" or str(path).endswith(failing):
                    note("os.chmod refused", str(path), oct(mode))
                    raise PermissionError(1, "Operation not permitted", str(path))
                return saved_os["chmod"](path, mode, *args, **kwargs)
            os.chmod = chmod
        if spec.get("helper"):
            t._ensure_private_dir = lambda path: note("_ensure_private_dir", path) or (path.mkdir(parents=True, exist_ok=True))
        else:
            t._ensure_private_dir = lambda path: note("_ensure_private_dir", path) or saved["_ensure_private_dir"](path)
        payload = spec.get("payload")
        if payload == "UNSERIALIZABLE":
            payload = {"a": 1, "b": object()}
        fn = saved[spec["call"]] if holder is t else getattr(holder, spec["call"])
        args = [target] if spec["call"] == "_ensure_private_dir" else [target, payload]
        kwargs = {"owner_user": spec["owner"]} if "owner" in spec else {}
        try:
            got = fn(*args, **kwargs)
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            result = {"raised": type(exc).__name__, "message": norm(str(exc))}
        else:
            result = norm(got)
        return {"result": result, "tree": tree(), "calls": calls}
    finally:
        os.umask(saved_umask)
        for n, v in saved_os.items():
            setattr(os, n, v)
        pwd.getpwnam = saved_getpwnam
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


#: What the Director kept on the launcher, beside the three.
KEPT = ("ensure_owner_file", "chown_owner_file_args")


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
    result = python("import sys, scripts.atomic_files as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.atomic_files", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.atomic_files")):
        result = python("import importlib; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.atomic_files as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"[getattr(m, n).__module__ for n in {MOVED!r}], "
                        f"not any(hasattr(m, n) for n in ('launcher', 'team_launcher', *{KEPT!r})))")
        check(result.stdout.strip() == "True ['scripts.atomic_files', 'scripts.atomic_files', 'scripts.atomic_files'] True",
              f"{' then '.join(order)}: one object each, defined here; nothing of the launcher bound at load: {result.stdout}{result.stderr[-600:]}")
    import pathlib
    import stat as _stat
    import tempfile as _tempfile
    import typing
    check(m.json is json and m.os is os and m.pwd is pwd and m.stat is _stat and m.tempfile is _tempfile and m.Path is pathlib.Path
          and m.Any is typing.Any and t.json is m.json and t.os is m.os and t.pwd is m.pwd and t.stat is m.stat and t.tempfile is m.tempfile
          and t.Path is m.Path,
          "the standard-library names are the module's own, the very objects the launcher holds")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "atomic_files.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, its sibling included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        check((imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[0]) == imports[0]) if expected else imports == [],
              f"{name}: the launcher imported first thing when it reads one, and nothing else imported: {imports}")
        skip = annotation_ids(node)
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import json", "import os", "import pwd", "import stat", "import tempfile",
                  "from pathlib import Path", "from typing import Any"] and not [n for n in tree.body if isinstance(n, ast.If)],
          f"the standard library only: {top}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(names == list(MOVED), f"the three, in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_them_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.atomic_files"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the three, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | set(KEPT) | set(DISPATCH) <= defined | exported,
          "the launcher defines none of them, and keeps ensure_owner_file, chown_owner_file_args and every seam they read, its own or re-exported")
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


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    def tree(label):
        return GOLDEN[label]["tree"]

    def calls(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    TYPE = {"raised": "TypeError", "message": "Object of type object is not JSON serializable"}
    check(tree("json: a new file") == [["state.json", "file", "0o600", '{\n  "a": [\n    1,\n    2\n  ],\n  "b": 1,\n  "\\u00e9": "\\u00fc"\n}\n']]
          and all(result(k) is None for k in GOLDEN if not isinstance(result(k), dict)),
          "JSON indented by two, keys sorted, non-ASCII escaped, one closing newline; the writers answer nothing")
    check(tree("json: a missing parent made")[:2] == [["deep", "dir", "0o755"], ["deep/er", "dir", "0o755"]]
          and tree("json: an existing file replaced, its mode not kept") == [["state.json", "file", "0o600", '{\n  "x": 2\n}\n']]
          and calls("json: a new file") == calls("json: an existing file replaced, its mode not kept") == calls("json: root without an owner") == [],
          "without an owner: the parents made, the temporary file's 0600 published, no owner or mode call, even as root")
    check(calls("json: an owner, not root, a new file") == ["os.fchmod"] and GOLDEN["json: an owner, not root, a new file"]["calls"][0][1] == ["0o600"]
          and tree("json: an owner, not root, the existing mode kept")[0][2] == "0o640"
          and GOLDEN["json: an owner, not root, the existing mode kept"]["calls"][0][1] == ["0o640"],
          "an owner, not root: no lookup and no chown; the existing file's mode (else 0600) set on the open temporary file")
    check(GOLDEN["json: an owner, root"]["calls"] == [["pwd.getpwnam", ["p457-agent"], {}], ["os.fchown", [1457, 1458], {}], ["os.fchmod", ["0o600"], {}]]
          and tree("json: an owner, root, the existing mode kept")[0][2] == "0o660" and calls("json: an owner, root, the existing mode kept")[-1] == "os.fchmod",
          "an owner, as root: looked up first, then the owner and then the mode set on the open temporary file, before it is published")
    check(result("json: an owner that does not exist, root")["raised"] == "KeyError" and tree("json: an owner that does not exist, root") == []
          and calls("json: an owner that does not exist, root") == ["pwd.getpwnam"],
          "an unknown owner, as root, is refused before anything is written")
    check(result("json: a payload that cannot be written") == TYPE == result("json: a payload that cannot be written, an owner")
          and tree("json: a payload that cannot be written") == [["state.json", "file", "0o644", '{"old": true}\n']]
          and tree("json: a payload that cannot be written, an owner") == [] and calls("json: a payload that cannot be written, an owner") == ["pwd.getpwnam"],
          "a payload that cannot be written leaves the target as it was, and no temporary file; no owner or mode is set")
    check(tree("json: the target a symlink") == [["real.json", "file", "0o640", '{"real": true}\n'], ["state.json", "file", "0o640", '{\n  "x": 9\n}\n']]
          and tree("json: the target a dangling symlink") == [["state.json", "file", "0o600", '{\n  "x": 10\n}\n']],
          "a symlinked target is replaced by a regular file, its link target untouched; an owner's mode is read through the link")
    check(tree("private dir: new and nested") == [["priv", "dir", "0o755"], ["priv/nested", "dir", "0o700"]]
          and tree("private dir: an existing open directory") == [["open", "dir", "0o700"]]
          and result("private dir: its mode cannot be set") is None and tree("private dir: its mode cannot be set") == [["stuck", "dir", "0o755"]],
          "the private directory: made with its parents, then 0700 itself, an existing one tightened; a refused chmod is ignored")
    check(tree("private json: a new file") == [["priv", "dir", "0o700"], ["priv/state.json", "file", "0o600", '{\n  "a": 2,\n  "b": 1\n}\n']]
          and GOLDEN["private json: a new file"]["calls"] == [["_ensure_private_dir", ["PATH TMP/priv"], {}]]
          and tree("private json: an existing file and open directory tightened") == [["open", "dir", "0o700"], ["open/state.json", "file", "0o600", '{\n  "x": 11\n}\n']],
          "the private writer: its directory through the launcher's helper, the same JSON, a 0600 file")
    check(result("private json: modes that cannot be set") is None and calls("private json: modes that cannot be set")
          == ["_ensure_private_dir", "os.chmod refused", "os.chmod refused", "os.chmod refused"]
          and [c[1][0] for c in GOLDEN["private json: modes that cannot be set"]["calls"][1:]] == ["TMP/stuck", "TMP/stuck/.state.json.RANDOM.tmp", "TMP/stuck/state.json"],
          "every mode it sets -- the directory, the temporary file, the target -- is best effort, in that order")
    check(result("private json: a payload that cannot be written") == TYPE
          and [e[0] for e in tree("private json: a payload that cannot be written")] == ["priv", "priv/.state.json.RANDOM.tmp"],
          "the baseline's private writer leaves its temporary file when the payload cannot be written (pinned, not changed)")
    check(tree("private json: the directory helper rebound on the launcher")[0] == ["priv", "dir", "0o755"],
          "the directory helper is the launcher's, read when it runs: rebound there, the directory is not tightened")


def test_every_launcher_seam_is_reached() -> None:
    # The one name the three read on the launcher, the private-directory helper, is a recorder there.
    names = {name for reads in SEAMS.values() for name in reads}
    check(names == {"_ensure_private_dir"} and names <= REACHED, f"a recorder on the launcher reached every function: missing {sorted(names - REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_them_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"atomic_files_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
