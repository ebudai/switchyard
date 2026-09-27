#!/usr/bin/env python3
"""SYRD-384: rendering and installing root's privileged artifacts, against the launcher they came out of.

`render_privileged_artifacts` and `install_privileged_artifacts` moved
unchanged into `scripts/privileged_artifacts.py`, and the launcher re-exports
them. The artifact writer and names, the provision root and directory, the
directory repair and the mode selector stay elsewhere. This pins what makes
that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the defaults are the baseline's
  literals, and `ProjectBoardProvision` is an annotation only.
- **Seams (rule 24):** every launcher facility read when a body runs is read
  through the launcher as often as before, so a patch on the launcher reaches
  each of them, which this test shows for all of them.
- **The behaviour is unchanged:** a temporary directory with the project's
  prefix, closed to root before anything is written and gone afterwards; the
  owner-linger flag passed on; only the privileged names and the plan, as
  written; the explicit or derived root, the target and its repair before any
  write; per artifact a no-follow `.name.new` opened 0600, then write, chown to
  root, the helper's mode, close and rename, in that order; errors propagated
  with the descriptor closed and nothing renamed.

Everything real lives in an owned temporary directory. `os.fchown` is always a
recorder here, never the host call; every other file operation the two make is
recorded and refused outside the owned directory.
"""

from __future__ import annotations

import ast
import errno
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import privileged_artifacts as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402

CHECKS = 0
MOVED = ("render_privileged_artifacts", "install_privileged_artifacts")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals.
SEAMS = {
    'render_privileged_artifacts': {'privileged_artifact_names': 1, 'write_artifacts': 1},
    'install_privileged_artifacts': {'ensure_privileged_provision_dir': 1, 'privileged_artifact_mode': 1, 'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
PROJECT = "p384"
PLAN = SimpleNamespace(project=PROJECT)
FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW


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


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


def module_def(name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / "scripts" / "privileged_artifacts.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.privileged_artifacts as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.privileged_artifacts", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.privileged_artifacts")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.privileged_artifacts as m; "
                        "r = inspect.signature(m.render_privileged_artifacts).parameters; "
                        "i = inspect.signature(m.install_privileged_artifacts).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "r['enable_owner_linger'].default is False and r['enable_owner_linger'].kind is inspect.Parameter.KEYWORD_ONLY "
                        "and i['privileged_root'].default is None and i['privileged_root'].kind is inspect.Parameter.KEYWORD_ONLY, "
                        "not hasattr(m, 'ProjectBoardProvision') and not hasattr(m, 'write_artifacts'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.tempfile is tempfile and m.Path is Path, "the standard-library names are the module's own")


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
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported once, first thing when it runs: {imports}")
        annotation = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs)]
                      if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected
                       and id(x) not in annotation})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        plan = [x for x in ast.walk(node) if isinstance(x, ast.Name) and x.id == "ProjectBoardProvision"]
        check(plan and all(id(x) in annotation for x in plan), f"{name}: ProjectBoardProvision only in annotations")
        bound = {a.arg for a in node.args.args + node.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        check(not bound & set(through), f"{name}: nothing it binds itself is read through the launcher")
    tree = ast.parse((ROOT / "scripts" / "privileged_artifacts.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import tempfile", "from pathlib import Path", "from typing import TYPE_CHECKING"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectBoardProvision"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    check([n.name for n in tree.body if isinstance(n, ast.FunctionDef)] == list(MOVED), "the two in the launcher's order")


def test_the_launcher_reexports_the_two() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.privileged_artifacts"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the two, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body}
    check(not defined & set(MOVED) and "PRIVILEGED_PROVISION_ROOT_ENV" in {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)},
          "the launcher defines neither itself, and keeps the override's variable that sat above them")


# --- rendering -----------------------------------------------------------------------------------------------------


def render(owned: Path, *, names=("a.service", "missing.sh", "dir.sh", "z.sql"), writer_error: BaseException | None = None, **kw):
    """render_privileged_artifacts with the writer, the names, chmod and the temporary directory recorded."""
    log: list = []
    real_td, real_chmod = tempfile.TemporaryDirectory, os.chmod

    def temporary(**kwargs):
        log.append(("tempdir", kwargs))
        made = real_td(dir=owned, **kwargs)
        log.append(("staged", Path(made.name)))
        return made

    def chmod(path, mode, *args, **kwargs):
        assert Path(path).is_relative_to(owned), f"chmod outside the owned directory: {path}"
        log.append(("chmod", Path(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    def write(plan, staged, *, enable_owner_linger):
        log.append(("write", plan, staged, enable_owner_linger, os.stat(staged).st_mode & 0o777))
        (staged / "a.service").write_bytes(b"[Unit]\n")
        (staged / "z.sql").write_bytes(b"select 1;\n")
        (staged / "plan.json").write_bytes(b'{"project": "p384"}')
        (staged / "tenant-only.txt").write_bytes(b"not root's")
        (staged / "dir.sh").mkdir()
        if writer_error is not None:
            raise writer_error

    with patched(tempfile, TemporaryDirectory=temporary), patched(os, chmod=chmod), \
            patched(t, write_artifacts=seam("write_artifacts", write),
                    privileged_artifact_names=seam("privileged_artifact_names", lambda plan: log.append(("names", plan)) or names)):
        return judged(m.render_privileged_artifacts, PLAN, **kw), log


def test_rendering_happens_where_only_root_can_reach() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        rendered, log = render(owned)
        staged = log[1][1]
        check(log[0] == ("tempdir", {"prefix": f"switchyard-{PROJECT}-privileged."}), f"root's own temporary directory, named for the project: {log[0]}")
        check([e[0] for e in log] == ["tempdir", "staged", "chmod", "write", "names"] and log[2] == ("chmod", staged, 0o700),
              f"closed to root before anything is written, then the names asked: {[e[0] for e in log]}")
        check(log[3] == ("write", PLAN, staged, False, 0o700), f"the plan written there, owner linger off by default: {log[3]}")
        check(isinstance(rendered, dict) and list(rendered) == ["a.service", "z.sql", "plan.json"]
              and rendered == {"a.service": b"[Unit]\n", "z.sql": b"select 1;\n", "plan.json": b'{"project": "p384"}'},
              f"the privileged names that are files, in their order, then the plan, byte for byte: {rendered}")
        check(not staged.exists() and list(owned.iterdir()) == [], "the temporary directory is gone afterwards")
        rendered, log = render(owned, enable_owner_linger=True)
        check(log[3][3] is True, "owner linger passed on")
        failure = RuntimeError("the writer failed")
        rendered, log = render(owned, writer_error=failure)
        check(rendered is failure and not log[1][1].exists() and list(owned.iterdir()) == [],
              f"a writer's failure propagates, and the temporary directory is still removed: {rendered!r}")
        rendered, log = render(owned, names=())
        check(rendered == {"plan.json": b'{"project": "p384"}'}, f"with no privileged names, only the plan: {rendered}")


# --- installation --------------------------------------------------------------------------------------------------


class Host:
    """The installer's file operations, recorded in order, confined to one owned directory."""

    def __init__(self, owned: Path, **fail: BaseException) -> None:
        self.owned, self.fail = owned, fail
        self.log: list = []
        self.fds: dict[int, str] = {}

    def inside(self, path) -> None:
        assert Path(path).is_relative_to(self.owned), f"refused outside the owned directory: {path}"

    def __enter__(self) -> "Host":
        real = {n: getattr(os, n) for n in ("open", "write", "fchmod", "close")}
        real_replace = Path.replace

        def open_(path, flags, mode=0o777, *, dir_fd=None):
            self.inside(path)
            self.log.append(("open", Path(path).name, flags, mode))
            fd = real["open"](path, flags, mode, dir_fd=dir_fd)
            self.fds[fd] = Path(path).name
            return fd

        def write(fd, body):
            self.log.append(("write", self.fds.get(fd), bytes(body)))
            if "write" in self.fail:
                raise self.fail["write"]
            return real["write"](fd, body)

        def fchown(fd, uid, gid):
            self.log.append(("fchown", self.fds.get(fd), uid, gid))
            if "fchown" in self.fail:
                raise self.fail["fchown"]

        def fchmod(fd, mode):
            self.log.append(("fchmod", self.fds.get(fd), mode))
            if "fchmod" in self.fail:
                raise self.fail["fchmod"]
            return real["fchmod"](fd, mode)

        def close(fd):
            self.log.append(("close", self.fds.get(fd)))
            return real["close"](fd)

        def replace(path, target):
            self.inside(path); self.inside(target)
            self.log.append(("replace", Path(path).name, Path(target).name))
            return real_replace(path, target)

        self.p = [patched(os, open=open_, write=write, fchown=fchown, fchmod=fchmod, close=close), patched(Path, replace=replace)]
        for p in self.p:
            p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self.p):
            p.__exit__(*exc)


def install(owned: Path, rendered: dict, *, derived: Path | None = None, **kw):
    """install_privileged_artifacts over an owned tree; returns (answer, host, calls)."""
    calls: list = []

    def provision_dir(project, *, root):
        calls.append(("dir", project, root))
        return root / "tenants" / project

    def ensure(target, *, root):
        calls.append(("ensure", target, root))
        target.mkdir(parents=True, exist_ok=True)
        return []

    with Host(owned, **{k: kw.pop(k) for k in ("write", "fchown", "fchmod") if k in kw}) as host, \
            patched(t, switchyard_privileged_provision_root=(seam("switchyard_privileged_provision_root", lambda: derived)
                                                             if derived is not None else refuse("the provision root")),
                    privileged_provision_dir=seam("privileged_provision_dir", provision_dir),
                    ensure_privileged_provision_dir=seam("ensure_privileged_provision_dir",
                                                         lambda target, *, root: host.log.append(("ensure",)) or ensure(target, root=root)),
                    privileged_artifact_mode=seam("privileged_artifact_mode", lambda name: 0o700 if name.endswith(".sh") else 0o640)):
        return judged(m.install_privileged_artifacts, PLAN, rendered, **kw), host, calls


def test_each_artifact_is_written_beside_its_target_and_renamed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        root = owned / "etc"
        rendered = {"run.sh": b"#!/bin/sh\n", "plan.json": b"{}"}
        answer, host, calls = install(owned, rendered, derived=root)
        target = root / "tenants" / PROJECT
        check(answer == target, f"the installed directory is returned: {answer!r}")
        check(calls == [("dir", PROJECT, root), ("ensure", target, root)], f"the derived root, the project's directory, repaired under that root: {calls}")
        per = lambda name, mode: [("open", f".{name}.new", FLAGS, 0o600), ("write", f".{name}.new", rendered[name]),
                                  ("fchown", f".{name}.new", 0, 0), ("fchmod", f".{name}.new", mode), ("close", f".{name}.new"),
                                  ("replace", f".{name}.new", name)]
        check(host.log == [("ensure",), *per("run.sh", 0o700), *per("plan.json", 0o640)],
              f"repaired first; then per artifact, in the rendered order: no-follow open 0600, write, root, the helper's mode, close, rename: {host.log}")
        check(sorted(p.name for p in target.iterdir()) == ["plan.json", "run.sh"]
              and (target / "run.sh").read_bytes() == b"#!/bin/sh\n" and (target / "run.sh").stat().st_mode & 0o777 == 0o700
              and (target / "plan.json").stat().st_mode & 0o777 == 0o640,
              "the bytes, at the helper's modes, and no staged file left behind")
        check(sorted(str(p.relative_to(owned)) for p in owned.rglob("*")) == ["etc", "etc/tenants", f"etc/tenants/{PROJECT}",
                                                                               f"etc/tenants/{PROJECT}/plan.json", f"etc/tenants/{PROJECT}/run.sh"],
              "nothing written outside the target")
        explicit = owned / "explicit"
        answer, host, calls = install(owned, {"plan.json": b"{}"}, privileged_root=explicit)
        check(answer == explicit / "tenants" / PROJECT and calls == [("dir", PROJECT, explicit), ("ensure", explicit / "tenants" / PROJECT, explicit)],
              f"an explicit root is used as given, and the provision root is not asked: {calls}")
        answer, host, calls = install(owned, {}, derived=root)
        check(answer == target and host.log == [("ensure",)], "nothing rendered: the directory is still repaired, and nothing is written")


def test_failures_close_the_descriptor_rename_nothing_and_propagate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        root = owned / "etc"
        target = root / "tenants" / PROJECT
        target.mkdir(parents=True)
        (owned / "elsewhere").write_bytes(b"not root's")
        (target / ".plan.json.new").symlink_to(owned / "elsewhere")
        answer, host, calls = install(owned, {"plan.json": b"{}"}, derived=root)
        check(isinstance(answer, OSError) and answer.errno == errno.ELOOP and (owned / "elsewhere").read_bytes() == b"not root's"
              and not (target / "plan.json").exists() and [e[0] for e in host.log] == ["ensure", "open"],
              f"a link planted at the staged name is refused, not followed: {answer!r} {host.log}")
        (target / ".plan.json.new").unlink()
        for step in ("write", "fchown", "fchmod"):
            failure = PermissionError(errno.EPERM, f"{step} refused")
            answer, host, calls = install(owned, {"plan.json": b"{}", "run.sh": b"x"}, derived=root, **{step: failure})
            steps = [e[0] for e in host.log]
            check(answer is failure and steps[-1] == "close" and "replace" not in steps and steps.count("open") == 1
                  and not (target / "plan.json").exists(),
                  f"a failing {step}: the descriptor closed, nothing renamed, nothing after it, the error propagated: {steps}")
            (target / ".plan.json.new").unlink()


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_two")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"privileged_artifacts_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
