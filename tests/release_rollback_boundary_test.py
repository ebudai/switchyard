#!/usr/bin/env python3
"""SYRD-378: root's rollback note and the pinned publication remote, against the launcher they came out of.

Seven definitions -- the rollback schema, its path, the note written before an
upgrade replaces anything, the commands that return to it, and the publication
remote's record, writer and restore -- moved unchanged into
`scripts/release_rollback.py`, and the launcher re-exports them. This pins what
makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's.
- **Definition-time bindings are the same objects:** the only defaults are
  plain values and the builtin `print`.
- **Seams (rule 24):** every launcher name these bodies read -- its release
  marker name included -- and every sibling read when a body runs, is read
  through the launcher as often as before, so a patch on the launcher reaches
  each of them, which this test shows for all of them. The publication
  boundary's path helper is still imported inside the two functions that use
  it, and is never read through the launcher.
- **The behaviour is unchanged:** a retry keeps the note taken when the host
  was whole, the pointer and staged-marker fallbacks, the publication
  validation and refusal order, the staged no-follow writer and its close
  before replace, the restore, and the rollback commands' guards and quoting.

Every boundary is this test's own: the shared release, the staged tooling and
root's directories live in owned temporary directories; the effective uid,
`os.readlink`, the private writer and every launcher facility are stand-ins.
Nothing on the host is read or changed.
"""

from __future__ import annotations

import ast
import errno
import inspect
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import release_rollback as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts.ticket_board import publication_boundary  # noqa: E402

CHECKS = 0
MOVED = ("RELEASE_ROLLBACK_SCHEMA", "release_rollback_path", "record_release_rollback", "record_publication_remote",
         "_write_publication_remote", "restore_publication_remote", "release_rollback_commands")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'release_rollback_path': {'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
    'record_release_rollback': {'RELEASE_ROLLBACK_SCHEMA': 1, 'SWITCHYARD_RELEASE_MARKER_NAME': 1, '_staged_tooling_dir': 1, '_write_private_json_atomic': 1, 'ensure_privileged_provision_dir': 1, 'privileged_artifact_mode': 1, 'release_rollback_path': 1, 'shared_switchyard_release_for_path': 1, 'switchyard_shared_install_root': 1,
                                # SYRD-528: the tenant's deployed board build, read through the launcher.
                                '_tenant_board_root_from_config_or_plan': 1, '_current_tenant_release': 1},
    'record_publication_remote': {'_write_publication_remote': 1, 'switchyard_privileged_provision_root': 1},
    '_write_publication_remote': {'ensure_privileged_provision_dir': 1},
    'restore_publication_remote': {'_write_publication_remote': 1, 'switchyard_privileged_provision_root': 1},
    # SYRD-528: the way back is the tenant's own release, named only when root holds it.
    'release_rollback_commands': {'RELEASE_ROLLBACK_SCHEMA': 1, 'release_rollback_path': 1, 'switchyard_shared_install_root': 1,
                                  '_read_switchyard_release_marker': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
CONFIG = SimpleNamespace(project="p378")
LOCAL_IMPORT = "from scripts.ticket_board.publication_boundary import publish_remote_registration_path"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is an answer to compare
        return exc


class patched:
    """Rebind attributes of one object for one block, as the suites do."""

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
    """A launcher stand-in that records, when it is called, that the launcher's name was reached."""
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


def raising(exc: BaseException):
    def raised(*args: object, **kwargs: object) -> object:
        raise exc
    return raised


def euid(value: int) -> patched:
    return patched(os, geteuid=lambda: value)


def printed(function, *args: object, **kwargs: object) -> tuple[object, list[str]]:
    said: list[str] = []
    return judged(function, *args, print_func=said.append, **kwargs), said


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "release_rollback.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name
             or isinstance(n, ast.Assign) and [ast.unparse(x) for x in n.targets] == [name]]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.release_rollback as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.release_rollback", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.release_rollback")):
        result = python("import importlib, inspect, builtins; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.release_rollback as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "all(inspect.signature(getattr(m, n)).parameters['print_func'].default is builtins.print "
                        "for n in ('record_release_rollback', 'record_publication_remote')))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    sig = inspect.signature(m.record_release_rollback).parameters
    check(sig["staging_root"].default is None and sig["dry_run"].default is False and "release" in sig
          and inspect.signature(m.release_rollback_commands).parameters["publish_remote"].default == "", "the defaults are plain values")
    check(m.os is os and m.json is json and m.shlex is shlex and m.Path is Path and m.datetime is datetime,
          "the standard-library names are the module's own, the same objects")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    for name in MOVED:
        node = module_def(name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            local = [LOCAL_IMPORT] if name in ("record_publication_remote", "restore_publication_remote") else []
            check(imports == ["from scripts import team_launcher as launcher", *local] and ast.unparse(node.body[first]) == imports[0]
                  and [ast.unparse(x) for x in node.body[first + 1:first + 1 + len(local)]] == local,
                  f"{name}: the launcher imported once, first thing when it runs; its own local import kept right after: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's, imports nothing: {imports}")
        annotation = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                      for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs)] if part is not None
                      for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected
                       and id(x) not in annotation})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        bound = {a.arg for f in ast.walk(node) if isinstance(f, ast.FunctionDef) for a in f.args.args + f.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.ExceptHandler) and x.name}
        bound |= {a.asname or a.name for x in ast.walk(node) if isinstance(x, ast.ImportFrom) and x.module != "scripts" for a in x.names}
        check(not bound & set(through), f"{name}: nothing it binds itself, its local import included, is read through the launcher")
    tree = ast.parse((ROOT / "scripts" / "release_rollback.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("scripts" in line for line in top), f"nothing of Switchyard's is imported at the top: {top}")
    order = [n.name if isinstance(n, ast.FunctionDef) else ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(order == list(MOVED), f"the seven in the launcher's order: {order}")


def test_the_launcher_reexports_the_seven() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.release_rollback"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the seven, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED), f"the launcher defines none of them itself: {defined & set(MOVED)}")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_note_lives_in_roots_directory() -> None:
    calls: list = []
    with patched(t, switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: Path("/nonexistent/syrd378")),
                 privileged_provision_dir=seam("privileged_provision_dir", lambda project, *, root: calls.append((project, root)) or root / project)):
        check(m.release_rollback_path("p378") == Path("/nonexistent/syrd378/p378/release-rollback.json")
              and calls == [("p378", Path("/nonexistent/syrd378"))], f"root's directory, root's name: {calls}")
    check(m.RELEASE_ROLLBACK_SCHEMA == "switchyard.release-rollback.v1", "the schema")


class Host:
    """An owned stand-in host: a shared install root with a current pointer, staged tooling, and root's directory."""

    def __init__(self, tmp: str, *, pointer: str | None = "releases/old", staged: str | None = None) -> None:
        self.base = Path(tmp)
        self.opt = self.base / "opt"
        (self.opt / "releases" / "old").mkdir(parents=True)
        if pointer is not None:
            (self.opt / "current").symlink_to(self.opt / pointer)
        self.staged = self.base / "staged"
        self.staged.mkdir()
        if staged is not None:
            (self.staged / ".syrd378-marker").write_text(staged)
        self.note = self.base / "priv" / "p378" / "release-rollback.json"
        self.writes: list = []

    def write(self, path: Path, payload: dict) -> None:
        self.writes.append(path)
        path.write_text(json.dumps(payload))

    def stand_ins(self, **over: object) -> dict:
        values = dict(
            switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: self.opt),
            shared_switchyard_release_for_path=seam("shared_switchyard_release_for_path",
                                                    lambda path: SimpleNamespace(marker_commit=f"commit-of-{Path(path).name}")),
            _staged_tooling_dir=seam("_staged_tooling_dir", lambda config, staging_root: self.staged),
            SWITCHYARD_RELEASE_MARKER_NAME=".syrd378-marker",
            release_rollback_path=seam("release_rollback_path", lambda project: self.note),
            ensure_privileged_provision_dir=seam("ensure_privileged_provision_dir", lambda d: d.mkdir(parents=True, exist_ok=True)),
            _write_private_json_atomic=seam("_write_private_json_atomic", self.write),
            privileged_artifact_mode=seam("privileged_artifact_mode", lambda name: 0o640),
            RELEASE_ROLLBACK_SCHEMA="syrd378.rollback",
            _tenant_board_root_from_config_or_plan=seam("_tenant_board_root_from_config_or_plan", lambda config: self.base / "board"),
            _current_tenant_release=seam("_current_tenant_release", lambda root: (root / "releases" / "b1", "board-old")),
        )
        values.update(over)
        return values


def note_of(host: "Host") -> dict:
    """The note as written, or nothing -- so a missing note fails a check rather than crashing it."""
    try:
        return json.loads(host.note.read_text())
    except (OSError, ValueError):
        return {}


def test_the_way_back_is_written_before_anything_is_replaced() -> None:
    with tempfile.TemporaryDirectory() as tmp, patched(t, **{name: refuse(name) for name in ("switchyard_shared_install_root", "release_rollback_path")}):
        check(printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"), dry_run=True) == ([], []), "a dry run writes nothing")
    with tempfile.TemporaryDirectory() as tmp:
        host = Host(tmp, staged=json.dumps({"commit": "staged-old"}))
        with patched(t, **host.stand_ins()):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"), staging_root=Path("/nonexistent/s"))
        note = json.loads(host.note.read_text())
        check(result == [] and {k: v for k, v in note.items() if k != "recorded_at"} == {
            "schema": "syrd378.rollback", "project": "p378", "upgrading_to": "new", "previous_release_root": str(host.opt / "releases" / "old"),
            "previous_release_commit": "commit-of-old", "previous_staged_commit": "staged-old",
            "previous_board_commit": "board-old"}, f"what the host was whole on: {note}")
        at = datetime.fromisoformat(note["recorded_at"])
        check(at.utcoffset() == timezone.utc.utcoffset(None), "stamped in UTC")
        check(stat.S_IMODE(host.note.stat().st_mode) == 0o640 and said == [f"switchyard: recorded the way back for p378 in {host.note}: commit-of-old"],
              f"moded as the launcher says, and said: {said}")
        REACHED.update({"SWITCHYARD_RELEASE_MARKER_NAME", "RELEASE_ROLLBACK_SCHEMA"})
        with patched(t, **host.stand_ins(_write_private_json_atomic=refuse("a second note"))):
            check(printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new")) == ([], []),
                  "a retry of the same upgrade keeps the note taken when the host was whole")
        with patched(t, **host.stand_ins(shared_switchyard_release_for_path=lambda path: None)):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="newer"))
        check(result == [] and json.loads(host.note.read_text())["upgrading_to"] == "newer"
              and said[0].endswith(str(host.opt / "releases" / "old")), f"a different upgrade replaces it; no marker: the root is named: {said}")
        host.note.write_text("{ not json")
        with patched(t, **host.stand_ins()):
            check(printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="newer"))[0] == []
                  and json.loads(host.note.read_text())["upgrading_to"] == "newer", "an unreadable note is replaced")
    with tempfile.TemporaryDirectory() as tmp:
        host = Host(tmp, pointer=None, staged="{ not json")
        with patched(t, **host.stand_ins(shared_switchyard_release_for_path=refuse("a release lookup without a pointer"))):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"))
        note = note_of(host)
        check(result == [] and note.get("previous_release_root") == "" and note.get("previous_release_commit") == "" and note.get("previous_staged_commit") == ""
              and said == [f"switchyard: recorded the way back for p378 in {host.note}: no previous release"],
              f"no pointer and an unreadable staged marker: nothing to name, said so: {note} {said}")
    with tempfile.TemporaryDirectory() as tmp:
        host = Host(tmp, pointer=None)
        (host.opt / "current").mkdir()
        with patched(t, **host.stand_ins(shared_switchyard_release_for_path=refuse("a release lookup for a pointer that is no link"))):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"))
        check(result == [] and note_of(host).get("previous_release_root") == "",
              f"a current that is a real directory, not a link, names no previous release and is no error: {result}")
    with tempfile.TemporaryDirectory() as tmp:
        host = Host(tmp, staged=json.dumps({"other": 1}))
        with patched(t, **host.stand_ins(release_rollback_path=lambda project: host.note)), \
                patched(os, readlink=raising(PermissionError(errno.EACCES, "Permission denied"))):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"))
        check(result == [f"could not read the current release pointer {host.opt / 'current'}: [Errno 13] Permission denied"] and said == []
              and not host.note.exists(), f"an unreadable pointer stops it before anything is written: {result}")
        with patched(t, **host.stand_ins(ensure_privileged_provision_dir=raising(PermissionError(errno.EACCES, "Permission denied")))):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"))
        check(result == ["could not record the rollback for p378: [Errno 13] Permission denied"] and said == [], f"{result}")
        with patched(t, **host.stand_ins(_write_private_json_atomic=lambda path, payload: None)):
            result, said = printed(m.record_release_rollback, CONFIG, release=SimpleNamespace(commit="new"))
        check(isinstance(result, list) and len(result) == 1 and result[0].startswith("could not record the rollback for p378: [Errno 2]") and said == [],
              f"a note that could not be moded is a failure: {result}")


def publication_root(tmp: str) -> Path:
    return Path(tmp) / "priv"


def remote_path(tmp: str) -> Path:
    return publication_boundary.publish_remote_registration_path("p378", publication_root(tmp))


def test_the_publication_remote_is_validated_before_anything_is_read() -> None:
    with patched(t, switchyard_privileged_provision_root=refuse("the provision root"), _write_publication_remote=refuse("the writer")):
        for bad in ("", "   ", "git@host:a b", "x" * 1025, "git@host:\x07repo"):
            result, said = printed(m.record_publication_remote, "p378", bad)
            check(result == (False, "", [f"{bad!r} is not a single git remote, so it cannot be recorded as p378's publication remote"]) and said == [],
                  f"{bad!r}: refused before the path is known: {result}")


def test_the_publication_remote_is_recorded_only_by_root_and_only_when_it_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = remote_path(tmp)
        written: list = []
        base = dict(switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: publication_root(tmp)),
                    _write_publication_remote=seam("_write_publication_remote", lambda p, value: written.append((p, value)) or ""))
        with patched(t, **base), euid(1006):
            check(printed(m.record_publication_remote, "p378", " git@host:repo ") ==
                  ((False, "", ["recording p378's publication remote is root's; run the upgrade with sudo"]), []), "not root: refused")
            check(printed(m.record_publication_remote, "p378", "git@host:repo", dry_run=True) ==
                  ((False, "", []), [f"switchyard: would record p378's publication remote as git@host:repo in {path}"]), "a dry run says what it would do")
            check(printed(m.record_publication_remote, "p378", "x" * 1024)[0] ==
                  (False, "", ["recording p378's publication remote is root's; run the upgrade with sudo"]), "1024 characters is still a remote")
        path.parent.mkdir(parents=True)
        path.write_text("git@host:old\n")
        with patched(t, **base), euid(0):
            check(printed(m.record_publication_remote, "p378", "git@host:old") ==
                  ((False, "git@host:old", []), ["switchyard: p378's publication remote is already recorded as git@host:old"]) and written == [],
                  "unchanged: left as it is")
            check(printed(m.record_publication_remote, "p378", "git@host:repo", dry_run=True)[1] ==
                  [f"switchyard: would record p378's publication remote as git@host:repo in {path} (replacing git@host:old)"], "a dry run names what it replaces")
            check(printed(m.record_publication_remote, "p378", " git@host:repo ") ==
                  ((True, "git@host:old", []), [f"switchyard: recorded p378's publication remote as git@host:repo in {path}"])
                  and written == [(path, "git@host:repo")], f"root records the trimmed value and says what it replaced: {written}")
        with patched(t, **{**base, "_write_publication_remote": lambda p, value: "syrd378: no room"}), euid(0):
            check(printed(m.record_publication_remote, "p378", "git@host:repo") == ((False, "git@host:old", ["syrd378: no room"]), []),
                  "a writer's problem is the answer")
        with patched(t, **base), euid(0):
            os.chmod(path, 0)
            try:
                result, _ = printed(m.record_publication_remote, "p378", "git@host:repo")
            finally:
                os.chmod(path, 0o600)
            check(result == (False, "", [f"{path} cannot be read (Permission denied)"]), f"an unreadable record: {result}")
            path.unlink(); path.symlink_to(Path(tmp) / "elsewhere")
            check(printed(m.record_publication_remote, "p378", "git@host:repo")[0] ==
                  (False, "", [f"{path} is not a regular file, so p378's publication remote cannot be recorded there"]), "a link is refused")
            path.unlink(); path.mkdir()
            check(printed(m.record_publication_remote, "p378", "git@host:repo")[0] ==
                  (False, "", [f"{path} is not a regular file, so p378's publication remote cannot be recorded there"]), "so is a directory")


class Recorded:
    """Wrap the real descriptor calls and `os.replace`, recording their order; everything stays on owned files."""

    def __init__(self) -> None:
        self.log: list = []
        self.real = {name: getattr(os, name) for name in ("open", "write", "close", "replace")}

    def __enter__(self) -> "Recorded":
        r = self
        self.patch = patched(os, open=lambda path, flags, mode=0o777: r.log.append(("open", Path(path).name, flags, mode)) or r.real["open"](path, flags, mode),
                             write=lambda fd, data: r.log.append(("write", data)) or r.real["write"](fd, data),
                             close=lambda fd: r.log.append(("close",)) or r.real["close"](fd),
                             replace=lambda a, b: r.log.append(("replace", Path(a).name, Path(b).name)) or r.real["replace"](a, b))
        self.patch.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.patch.__exit__(*exc)


def test_the_remote_is_written_staged_no_follow_and_closed_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p378" / "publish-remote"
        made: list = []
        with patched(t, ensure_privileged_provision_dir=lambda d: made.append(d) or d.mkdir(exist_ok=True)), Recorded() as rec:
            problem = m._write_publication_remote(path, "git@host:repo")
        check(problem == "" and path.read_text() == "git@host:repo\n" and made == [path.parent] and stat.S_IMODE(path.stat().st_mode) == 0o600,
              "written with a newline, private, its directory ensured")
        check(rec.log == [("open", ".publish-remote.new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600),
                          ("write", b"git@host:repo\n"), ("close",), ("replace", ".publish-remote.new", "publish-remote")],
              f"a no-follow create, the write, the close, and only then the replace: {rec.log}")
        (path.parent / ".publish-remote.new").symlink_to(Path(tmp) / "elsewhere")
        with patched(t, ensure_privileged_provision_dir=lambda d: None):
            problem = m._write_publication_remote(path, "git@host:other")
        check(problem.startswith(f"could not record the publication remote at {path}: [Errno 40]") and not (Path(tmp) / "elsewhere").exists()
              and path.read_text() == "git@host:repo\n", f"a planted link is never followed: {problem}")
        with patched(t, ensure_privileged_provision_dir=raising(PermissionError(errno.EACCES, "Permission denied"))):
            check(m._write_publication_remote(path, "x") == f"could not record the publication remote at {path}: [Errno 13] Permission denied",
                  "an unusable directory is the answer")


def test_the_pin_is_put_back_as_it_was() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = remote_path(tmp)
        written: list = []
        with patched(t, switchyard_privileged_provision_root=lambda: publication_root(tmp),
                     _write_publication_remote=lambda p, value: written.append((p, value)) or "syrd378-answer"):
            check(m.restore_publication_remote("p378", "git@host:old") == "syrd378-answer" and written == [(path, "git@host:old")],
                  "a previous value is written back, and the writer's answer returned")
            check(m.restore_publication_remote("p378", "") == "", "nothing to remove: fine")
            path.parent.mkdir(parents=True); path.write_text("git@host:new\n")
            check(m.restore_publication_remote("p378", "") == "" and not path.exists(), "no previous value: the new pin is removed")
            path.mkdir()
            answer = m.restore_publication_remote("p378", "")
            check(answer.startswith(f"could not remove {path}: [Errno 21]"), f"a removal that fails is said: {answer}")


def test_the_way_back_as_commands() -> None:
    old, new, host = "a" * 40, "c" * 40, "b" * 40
    with tempfile.TemporaryDirectory() as tmp:
        note = Path(tmp) / "release-rollback.json"
        opt = Path(tmp) / "opt dir"
        held: dict[str, str] = {}
        with patched(t, release_rollback_path=lambda project: note,
                     switchyard_shared_install_root=seam("switchyard_shared_install_root", lambda: opt),
                     _read_switchyard_release_marker=seam(
                         "_read_switchyard_release_marker",
                         lambda path: SimpleNamespace(marker_commit=held[path.name]) if path.name in held else None),
                     RELEASE_ROLLBACK_SCHEMA="syrd378.rollback"):
            check(m.release_rollback_commands("p378") == [], "no note: nothing to say")
            for bad in ("{", json.dumps({"schema": "other", "previous_staged_commit": old})):
                note.write_text(bad)
                check(m.release_rollback_commands("p378") == [], f"{bad}: nothing to say")

            def way_back(**record) -> list[str]:
                note.write_text(json.dumps({"schema": "syrd378.rollback", "upgrading_to": new, **record}))
                return m.release_rollback_commands("my proj")

            held[old] = old
            lines = way_back(previous_release_commit=host, previous_staged_commit=old, previous_board_commit="board-old")
            action = f"switchyard privileged-action 'my proj' select-shared-release commit={old}"
            check([l for l in lines if not l.startswith("#")] == [f"{action} --dry-run", action],
                  f"the tenant's own release, through the admin action, dry run first, the project quoted: {lines}")
            check(not any("ln -sfn" in l or "/current" in l for l in lines) and any(f"({host})" in l and "did not move it" in l for l in lines),
                  f"never the host's pointer, which is named as not part of it: {lines}")
            check(any("it was on board-old" in l and "not reversed" in l for l in lines), f"what the board goes back to, said plainly: {lines}")
            check([l for l in way_back(previous_staged_commit="b0" * 20) if not l.startswith("#")] == [],
                  "a release root does not hold: no command")
            held["e" * 40] = "f" * 40
            check([l for l in way_back(previous_staged_commit="e" * 40) if not l.startswith("#")] == [],
                  "a release whose marker names another commit: no command")
            check(any("no way back can be named" in l for l in way_back(previous_staged_commit="")), "nothing recorded: said so")
            check(any("nothing to go back to" in l for l in way_back(previous_staged_commit=new)), "already on it: said so")
            check(m.release_rollback_commands("p378", publish_remote="git@host:a b") == m.release_rollback_commands("p378"),
                  "the remote a caller passes changes nothing: the admin action keeps the recorded one")
    REACHED.update({"RELEASE_ROLLBACK_SCHEMA"})


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_seven")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"release_rollback_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
