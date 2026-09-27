#!/usr/bin/env python3
"""SYRD-382: root-controlled, no-follow record reading, against the launcher it came out of.

Six definitions -- whose files count as root's, the root-control check, the
no-follow walk, the plan reader and the tenant-document readers -- moved
unchanged into `scripts/no_follow_records.py`, and the launcher re-exports
them. `PlanDocument` and the override's variable stay in the launcher. This
pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's.
- **Seams (rule 24):** the override's variable, the result class and every
  sibling read when a body runs are read through the launcher as often as
  before, so a patch on the launcher reaches each of them, which this test
  shows for all of them. The publication boundary's root-control check is still
  imported inside the one function that uses it.
- **The behaviour is unchanged:** every walk and gate refusal and its message,
  in order; missing versus unusable; every descriptor closed on every path;
  65536-byte reads; decode errors that never quote the bytes; the exact
  `PlanDocument`; the tenant reader's entitlement by directory owner.

Everything real lives in owned temporary directories and is owned by this
test's uid; other owners come from an `os.fstat` stand-in that reports a chosen
uid for named entries. No ownership, permission outside owned temp, or account
lookup is performed.
"""

from __future__ import annotations

import ast
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import no_follow_records as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts.ticket_board import publication_boundary  # noqa: E402

CHECKS = 0
MOVED = ("expected_privileged_uid", "root_controlled_problems_for", "read_plan_no_follow", "_directory_owner_no_follow",
         "read_tenant_document_no_follow", "_walk_no_follow")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'expected_privileged_uid': {'PRIVILEGED_PROVISION_ROOT_ENV': 1},
    'root_controlled_problems_for': {'PRIVILEGED_PROVISION_ROOT_ENV': 1},
    'read_plan_no_follow': {'PlanDocument': 1, '_walk_no_follow': 1, 'expected_privileged_uid': 1},
    '_directory_owner_no_follow': {'_walk_no_follow': 1},
    'read_tenant_document_no_follow': {'_directory_owner_no_follow': 1, 'read_plan_no_follow': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
ME = os.getuid()
OTHER = ME + 1
ENV = t.PRIVILEGED_PROVISION_ROOT_ENV


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


class environ:
    """Set or clear one variable for one block."""

    def __init__(self, name: str, value: str | None) -> None:
        self.name, self.value = name, value

    def __enter__(self) -> None:
        self.saved = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value

    def __exit__(self, *exc: object) -> None:
        if self.saved is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.saved


def seam(name: str, function):
    """A launcher stand-in that records, when it is called, that the launcher's name was reached."""
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


class Descriptors:
    """Track every descriptor opened and closed, count reads, and report chosen owners for named entries."""

    def __init__(self, uid_of: dict[str, int] | None = None) -> None:
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.names: dict[int, str] = {}
        self.reads: list[int] = []
        self.uid_of = uid_of or {}
        self.real = {name: getattr(os, name) for name in ("open", "close", "fstat", "read")}

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        fd = self.real["open"](path, flags, mode, dir_fd=dir_fd)
        self.opened.append(fd)
        self.names[fd] = Path(path).name or str(path)
        return fd

    def close(self, fd):
        self.closed.append(fd)
        return self.real["close"](fd)

    def fstat(self, fd):
        info = self.real["fstat"](fd)
        uid = self.uid_of.get(self.names.get(fd, ""))
        if uid is None:
            return info
        fields = list(info[:10])
        fields[4] = uid
        return os.stat_result(fields)

    def read(self, fd, size):
        self.reads.append(size)
        return self.real["read"](fd, size)

    def all_closed(self) -> bool:
        return sorted(self.opened) == sorted(self.closed)

    def __enter__(self) -> "Descriptors":
        self.p = patched(os, open=self.open, close=self.close, fstat=self.fstat, read=self.read)
        self.p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.p.__exit__(*exc)
        for fd in set(self.opened) - set(self.closed):
            self.real["close"](fd)


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "no_follow_records.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.no_follow_records as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.no_follow_records", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.no_follow_records")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.no_follow_records as m; "
                        "p = inspect.signature(m.read_plan_no_follow).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "p['require_owner_uids'].default is None and p['require_single_link'].default is False and p['require_not_shared_writable'].default is False "
                        "and p['require_root_owned'].default is inspect.Parameter.empty, "
                        "not hasattr(m, 'PlanDocument') and t.PlanDocument.__module__ == 'scripts.team_launcher')")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.stat is stat and m.errno is errno and m.json is json and m.Path is Path, "the standard-library names are the module's own")


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
        local = ["from scripts.ticket_board.publication_boundary import root_controlled_problems"] if name == "root_controlled_problems_for" else []
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(imports == ["from scripts import team_launcher as launcher", *local] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs; its local import kept: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's: {imports}")
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
    tree = ast.parse((ROOT / "scripts" / "no_follow_records.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("scripts" in line for line in top), f"nothing of Switchyard's is imported at the top: {top}")
    check([n.name for n in tree.body if isinstance(n, ast.FunctionDef)] == list(MOVED), "the six in the launcher's order")


def test_the_launcher_reexports_the_six_and_keeps_its_class_and_variable() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.no_follow_records"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the six, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED) and {"PlanDocument", "PRIVILEGED_PROVISION_ROOT_ENV"} <= defined,
          "the launcher defines none of the six, and still defines PlanDocument and the override's variable")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_whose_files_count_as_roots() -> None:
    with environ(ENV, None), environ("SYRD382_ROOT", None):
        check(m.expected_privileged_uid() == 0, "on a host: root's")
        with environ(ENV, "  "):
            check(m.expected_privileged_uid() == 0, "a blank override is no override")
        with environ(ENV, "/tmp/syrd382"):
            check(m.expected_privileged_uid() == ME, "overridden: the caller's own, the documented seam")
        with patched(t, PRIVILEGED_PROVISION_ROOT_ENV="SYRD382_ROOT"), environ("SYRD382_ROOT", "/tmp/x"):
            check(m.expected_privileged_uid() == ME, "the launcher's variable decides")
        asked: list = []
        with patched(publication_boundary, root_controlled_problems=lambda path, *, base: asked.append((path, base)) or ["p"]):
            check(judged(m.root_controlled_problems_for, "/etc/x") == ["p"] and asked == [("/etc/x", "/")], "no override: the whole path from /")
            with environ(ENV, " /tmp/syrd382 "):
                m.root_controlled_problems_for("/tmp/syrd382/x")
            with patched(t, PRIVILEGED_PROVISION_ROOT_ENV="SYRD382_ROOT"), environ("SYRD382_ROOT", "/tmp/y"):
                m.root_controlled_problems_for("/tmp/y/z")
        check(asked[1:] == [("/tmp/syrd382/x", "/tmp/syrd382"), ("/tmp/y/z", "/tmp/y")], f"overridden: from the override, stripped: {asked}")
    REACHED.add("PRIVILEGED_PROVISION_ROOT_ENV")


def walk(base: Path, relative: Path, **kw):
    with Descriptors(**kw) as d:
        fd, problem = m._walk_no_follow(base, relative)
        name = d.names.get(fd, "")
        if fd >= 0:
            os.close(fd)
        return fd >= 0, name, problem, d.all_closed()


def test_the_walk_follows_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a" / "b").mkdir(parents=True)
        check(walk(root, Path("a/b/leaf")) == (True, "b", "", True), "the leaf's parent, opened component by component, every other fd closed")
        check(walk(root / "absent", Path("a/leaf"))[:3] == (False, "", "missing"), "a missing base is missing")
        (root / "file").write_text("")
        ok, _n, problem, closed = walk(root / "file", Path("a/leaf"))
        check(not ok and problem == f"cannot open {root / 'file'} (Not a directory)" and closed, f"an unusable base: {problem}")
        (root / "base").symlink_to(root / "a")
        ok, _n, problem, closed = walk(root / "base", Path("b/leaf"))
        check(not ok and problem == f"cannot open {root / 'base'} (Not a directory)" and closed,
              f"a symlinked base is not followed: {problem}")
        (root / "a" / "link").symlink_to(root / "a" / "b")
        check(walk(root, Path("a/link/leaf"))[1:] == ("", "ancestor link is a symlink", True), "a symlinked ancestor is named as one")
        (root / "a" / "plain").write_text("")
        check(walk(root, Path("a/plain/leaf"))[1:] == ("", "ancestor plain is not a directory", True), "a file where a directory should be")
        check(walk(root, Path("a/none/leaf"))[1:] == ("", "missing", True), "a missing ancestor is absent, not unsafe")
        (root / "a" / "shut").mkdir()
        (root / "a" / "shut" / "in").mkdir()
        os.chmod(root / "a" / "shut", 0)
        try:
            ok, _n, problem, closed = walk(root, Path("a/shut/in/leaf"))
        finally:
            os.chmod(root / "a" / "shut", 0o700)
        check(not ok and problem == "ancestor shut is unusable (Permission denied)" and closed, f"an ancestor it cannot open is unusable: {problem}")


def read(path: Path, *, uid_of: dict[str, int] | None = None, **kw):
    with Descriptors(uid_of) as d, patched(t, _walk_no_follow=seam("_walk_no_follow", m._walk_no_follow),
                                           expected_privileged_uid=seam("expected_privileged_uid", m.expected_privileged_uid),
                                           PlanDocument=seam("PlanDocument", t.PlanDocument)):
        answer = judged(m.read_plan_no_follow, path, **kw)
        document, problem = answer if isinstance(answer, tuple) else (None, answer)
        return document, problem, d.all_closed(), list(d.reads)


def test_the_plan_reader_refuses_in_order() -> None:
    with tempfile.TemporaryDirectory() as tmp, environ(ENV, None):
        root = Path(tmp)
        plan = root / "etc" / "plan.json"
        plan.parent.mkdir()
        plan.write_text(json.dumps({"owner_user": "p382"}))
        os.chmod(plan, 0o644)
        document, problem, closed, reads = read(plan, require_root_owned=False, uid_of={"plan.json": OTHER})
        info = os.stat(plan)
        check(info.st_gid != OTHER, "the reported owner differs from the group, so a swap shows")
        check(problem == "" and closed and isinstance(document, t.PlanDocument)
              and (document.path, document.data, document.raw, document.uid, document.gid, document.mode)
              == (plan, {"owner_user": "p382"}, plan.read_bytes(), OTHER, info.st_gid, 0o644),
              f"the exact PlanDocument, every fd closed: {document!r} {problem}")
        check(read(root / "etc" / "absent.json", require_root_owned=False)[1:3] == (f"{root / 'etc' / 'absent.json'} does not exist", True), "missing")
        check(read(root / "no" / "plan.json", require_root_owned=False)[1:3] == (f"{root / 'no' / 'plan.json'}: missing", True), "a missing parent")
        (root / "etc" / "link.json").symlink_to(plan)
        check(read(root / "etc" / "link.json", require_root_owned=False)[1:3] == (f"{root / 'etc' / 'link.json'} is a symlink, so it is not a plan this will read or write", True),
              "a symlinked leaf")
        (root / "etc" / "dir.json").mkdir()
        check(read(root / "etc" / "dir.json", require_root_owned=False)[1:3] == (f"{root / 'etc' / 'dir.json'} is not a regular file", True), "not a file")
        shut = root / "etc" / "shut.json"
        shut.write_text("{}")
        os.chmod(shut, 0)
        try:
            check(read(shut, require_root_owned=False)[1:3] == (f"{shut} could not be opened (Permission denied)", True), "unopenable")
        finally:
            os.chmod(shut, 0o600)
        os.chmod(plan, 0o664)
        check(read(plan, require_root_owned=False, require_not_shared_writable=True)[1:3]
              == (f"{plan} is mode 0664, which anybody in its group or beyond can write", True), "shared-writable, when asked")
        check(read(plan, require_root_owned=False)[1] == "", "shared-writable is fine when not asked")
        os.chmod(plan, 0o644)
        os.link(plan, root / "etc" / "second")
        check(read(plan, require_root_owned=False, require_single_link=True)[1:3]
              == (f"{plan} has 2 links, so it is another file under a second name rather than this project's own document", True), "a second link")
        check(read(plan, require_root_owned=False)[1] == "", "a second link is fine when not asked")
        (root / "etc" / "second").unlink()
        check(read(plan, require_root_owned=False, require_owner_uids=[0, OTHER])[1:3]
              == (f"{plan} is owned by uid {ME} rather than by uid 0, {OTHER}, so it is not a document this project's owner or root wrote", True),
              "an owner outside the entitled uids")
        os.chmod(plan, 0o646)
        check(read(plan, require_root_owned=False, require_owner_uids=[ME])[1] == f"{plan} is mode 0646, which anybody in its group or beyond can write",
              "an entitled owner's file anybody can rewrite")
        os.chmod(plan, 0o644)
        check(read(plan, require_root_owned=True)[1:3] == (f"{plan} is owned by uid {ME} rather than by uid 0", True), "not root's")
        check(read(plan, require_root_owned=True, uid_of={"plan.json": 0})[1] == "", "root's own")
        with environ(ENV, str(root)):
            check(read(plan, require_root_owned=True)[1] == "", "overridden: the caller's own counts as root's")
            os.chmod(plan, 0o624)
            check(read(plan, require_root_owned=True)[1] == f"{plan} is mode 0624, which anybody in its group or beyond can write", "root's, but group-writable")
            os.chmod(plan, 0o644)


def test_the_plan_reader_decodes_without_leaking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plan = Path(tmp) / "plan.json"
        big = {"pad": "x" * 200_000}
        plan.write_text(json.dumps(big))
        document, problem, closed, reads = read(plan, require_root_owned=False)
        check(problem == "" and document.data == big and reads[:4] == [65536] * 4 and len(reads) == 5 and closed,
              f"read in 65536-byte chunks until the end: {reads}")
        plan.write_bytes(b"\xffSECRET-BYTES\xfe")
        document, problem, closed, _ = read(plan, require_root_owned=False)
        check(document is None and problem == f"{plan} is not readable as a plan document: it is not UTF-8 text" and "SECRET" not in problem and closed,
              f"never what it choked on: {problem}")
        plan.write_text('{"secret": ')
        check(read(plan, require_root_owned=False)[1] == f"{plan} is not readable as a plan document: it is not JSON (line 1, column 12)", "where, never what")
        plan.write_text("[1, 2]")
        check(read(plan, require_root_owned=False)[1] == f"{plan} is not a plan document", "not a mapping")


def test_the_tenant_documents() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "p382"
        home.mkdir()
        doc = home / "plan.json"
        doc.write_text(json.dumps({"a": 1}))
        os.chmod(home, 0o755); os.chmod(doc, 0o644)

        read_back: list = []

        def reading(*args: object, **kwargs: object):
            answer = m.read_plan_no_follow(*args, **kwargs)
            read_back.append(answer[0])
            return answer

        def tenant(uid_of=None):
            with Descriptors(uid_of) as d, patched(t, _directory_owner_no_follow=seam("_directory_owner_no_follow", m._directory_owner_no_follow),
                                                   read_plan_no_follow=seam("read_plan_no_follow", reading)):
                result = m.read_tenant_document_no_follow(doc, what="the plan")
                return result, d.all_closed()
        (document, problem), closed = tenant()
        check(document == {"a": 1} and problem == "" and closed, "the owner's own document in the owner's directory")
        (document, problem), _ = tenant({"plan.json": OTHER})
        check(document is None and problem == f"refusing to read the plan at {doc}: {doc} is owned by uid {OTHER} rather than by uid 0, {ME}, "
                                              "so it is not a document this project's owner or root wrote", f"another account's file in the owner's directory: {problem}")
        (document, problem), _ = tenant({"plan.json": OTHER, "p382": 0})
        check(document == {"a": 1}, "in a root-owned directory, root placed what is there, whoever now owns it")
        os.chmod(home, 0o775)
        try:
            (document, problem), _ = tenant()
        finally:
            os.chmod(home, 0o755)
        check(document is None and problem == f"refusing to read the plan at {doc}: {home} is mode 0775, so anybody in its group or beyond can replace what is in it",
              f"a directory others can write: {problem}")
        os.link(doc, home / "second")
        (document, problem), _ = tenant()
        check(document is None and problem.endswith("has 2 links, so it is another file under a second name rather than this project's own document"),
              f"a second link: {problem}")
        (home / "second").unlink()
        os.chmod(doc, 0o664)
        (document, problem), _ = tenant({"p382": 0})
        check(document is None and problem.endswith("is mode 0664, which anybody in its group or beyond can write"), f"a file others can write: {problem}")
        os.chmod(doc, 0o644)
        (document, problem), _ = tenant()
        # Seven reads above; the one in a directory others can write is refused before any plan is read.
        check(len(read_back) == 6, f"every tenant read went through the launcher's plan reader: {len(read_back)}")
        check(type(document) is dict and document == read_back[-1].data and document is not read_back[-1].data,
              "a plain dict, a copy of the document's data")
        with Descriptors() as d:
            info, problem = m._directory_owner_no_follow(Path(tmp) / "absent")
            check(info is None and problem == f"{Path(tmp) / 'absent'}: missing" and d.all_closed(), "a missing directory")
            walked: list = []
            with patched(t, _walk_no_follow=lambda *a: walked.append(a) or m._walk_no_follow(*a)):
                info, problem = m._directory_owner_no_follow(home)
            check(walked == [(Path("/"), Path(str(home).lstrip("/")) / "_")], f"walked through the launcher from the anchor: {walked}")
            check(info is not None and stat.S_ISDIR(info.st_mode) and problem == "" and d.all_closed(), "a directory's own stat, its fd closed")
        missing = Path(tmp) / "absent" / "plan.json"
        with patched(t, _directory_owner_no_follow=m._directory_owner_no_follow, read_plan_no_follow=m.read_plan_no_follow):
            check(m.read_tenant_document_no_follow(missing, what="the config") == (None, f"refusing to read the config at {missing}: {Path(tmp) / 'absent'}: missing"),
                  "its directory missing")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_six_and_keeps_its_class_and_variable")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"no_follow_records_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
