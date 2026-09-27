#!/usr/bin/env python3
"""SYRD-387: writing the tenant's copy of a generated file, against the launcher it came out of.

`publish_tenant_artifact` moved unchanged into `scripts/tenant_artifact_publish.py`,
and the launcher re-exports it. The no-follow walk it uses stays in
`scripts/no_follow_records.py`. This pins what makes that safe:

- **No cycle, one object**, whichever module is imported first; the module alone
  loads nothing of Switchyard's; `os`, `pwd` and `Path` are its own, the same
  singletons; `ProjectConfig` is an annotation only.
- **The seam (rule 24):** the walk is read through the launcher when it runs,
  so a patch on the launcher reaches it, which this test shows.
- **The behaviour is unchanged:** the walk from the anchor (or `/`) to the
  directory, and its refusal; 0755 for a shell script and 0644 otherwise; a
  staged `.name.new` opened relative to the directory's descriptor with
  `O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW`; write, then the mode; only root with a
  configured account looks it up, a missing account means no chown, a found one
  gets it; the file's descriptor always closed, then the rename relative to the
  directory, then the directory's descriptor, always; an OSError anywhere is
  `(False, message)`, anything else propagates; success is `(True, "")`.

Everything real lives in an owned temporary directory. `os.fchown` is always a
recorder here, `os.geteuid` and `pwd.getpwnam` stand-ins -- no real account is
looked up and no ownership changes -- and every file operation is recorded and
refused outside the owned directory.
"""

from __future__ import annotations

import ast
import errno
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

from scripts import tenant_artifact_publish as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts.no_follow_records import _walk_no_follow as REAL_WALK  # noqa: E402

CHECKS = 0
MOVED = ("publish_tenant_artifact",)
#: Measured on the baseline launcher: the moved body's call-time reads of launcher globals.
SEAMS = {
    'publish_tenant_artifact': {'_walk_no_follow': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
REAL_OPEN, REAL_CLOSE = os.open, os.close
ENTRY = SimpleNamespace(pw_uid=4387, pw_gid=4388)


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


def config(user: str | None = "p387-owner") -> SimpleNamespace:
    return SimpleNamespace(project="p387", run_as_user=user)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.tenant_artifact_publish as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_object() -> None:
    for order in (("scripts.tenant_artifact_publish", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_artifact_publish")):
        result = python("import importlib, inspect, os, pwd; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_artifact_publish as m; "
                        "print(t.publish_tenant_artifact is m.publish_tenant_artifact, "
                        "all(p.default is inspect.Parameter.empty for p in inspect.signature(m.publish_tenant_artifact).parameters.values()), "
                        "m.pwd is pwd is t.pwd and m.os is os and not hasattr(m, 'ProjectConfig') and not hasattr(m, '_walk_no_follow'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seam_reads_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "tenant_artifact_publish.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "publish_tenant_artifact")
    through: dict[str, int] = {}
    for x in ast.walk(node):
        if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
            through[x.attr] = through.get(x.attr, 0) + 1
    check(through == SEAMS["publish_tenant_artifact"], f"the walk read through the launcher exactly as often as before: {through}")
    imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
    check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[1]) == imports[0],
          f"the launcher imported once, first thing after the docstring: {imports}")
    check(not [x for x in ast.walk(node) if isinstance(x, ast.Name) and x.id == "_walk_no_follow"], "the walk is never read past the launcher")
    check([ast.unparse(x) for x in ast.walk(node) if isinstance(x, ast.Attribute) and ast.unparse(x).startswith("pwd.")] == ["pwd.getpwnam"],
          "pwd is the module's own, read bare at its one site")
    annotation = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args)] if part is not None for y in ast.walk(part)}
    types = [x for x in ast.walk(node) if isinstance(x, ast.Name) and x.id == "ProjectConfig"]
    check(types and all(id(x) in annotation for x in types), "ProjectConfig only in the annotation")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import pwd", "from pathlib import Path", "from typing import TYPE_CHECKING"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")


def test_the_launcher_reexports_it() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_artifact_publish"]
    check(len(imports) == 1 and [a.name for a in imports[0].names] == ["publish_tenant_artifact"] and imports[0].names[0].asname is None,
          "one explicit import of exactly the publisher, unaliased")
    check("publish_tenant_artifact" not in {getattr(n, "name", None) for n in tree.body}, "the launcher does not define it itself")


# --- the recorded write --------------------------------------------------------------------------------------------


class Host:
    """The publisher's file operations, recorded in order with the descriptors named, confined to owned temp."""

    def __init__(self, owned: Path, directory: Path, *, refuse: str = "", euid: int = 0, lookup=ENTRY, **fail: BaseException) -> None:
        self.owned, self.directory, self.refusal, self.euid, self.lookup, self.fail = owned, directory, refuse, euid, lookup, fail
        self.log: list = []
        self.names: dict[int, str] = {}

    def __enter__(self) -> "Host":
        real = {n: getattr(os, n) for n in ("write", "fchmod", "rename")}

        def step(name, *rest):
            self.log.append((name, *rest))
            if name in self.fail:
                raise self.fail[name]

        def walk(anchor, relative):
            self.log.append(("walk", anchor, relative))
            if self.refusal:
                return -1, self.refusal
            fd = REAL_OPEN(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            self.names[fd] = "DIR"
            return fd, ""

        def open_(path, flags, mode=0o777, *, dir_fd=None):
            step("open", path, flags, mode, self.names.get(dir_fd, dir_fd))
            assert self.names.get(dir_fd) == "DIR", "the staged file is opened relative to the directory's descriptor"
            fd = REAL_OPEN(path, flags, mode, dir_fd=dir_fd)
            self.names[fd] = "FILE"
            return fd

        def write(fd, body):
            step("write", self.names.get(fd), bytes(body))
            return real["write"](fd, body)

        def fchmod(fd, mode):
            step("fchmod", self.names.get(fd), mode)
            return real["fchmod"](fd, mode)

        def fchown(fd, uid, gid):
            step("fchown", self.names.get(fd), uid, gid)

        def getpwnam(user):
            self.log.append(("getpwnam", user))
            if isinstance(self.lookup, BaseException):
                raise self.lookup
            return self.lookup

        def close(fd):
            self.log.append(("close", self.names.get(fd, fd)))
            return REAL_CLOSE(fd)

        def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            step("rename", src, dst, self.names.get(src_dir_fd), self.names.get(dst_dir_fd))
            return real["rename"](src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        self.p = [patched(os, open=open_, write=write, fchmod=fchmod, fchown=fchown, close=close, rename=rename,
                          geteuid=lambda: self.log.append(("geteuid",)) or self.euid),
                  patched(pwd, getpwnam=getpwnam),
                  patched(t, _walk_no_follow=seam("_walk_no_follow", walk))]
        for p in self.p:
            p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self.p):
            p.__exit__(*exc)


def publish(owned: Path, name: str = "run.sh", body: bytes = b"#!/bin/sh\n", *, user: str | None = "p387-owner", **host):
    directory = owned / "tenant" / "provision"
    directory.mkdir(parents=True, exist_ok=True)
    with Host(owned, directory, **host) as h:
        answer = judged(m.publish_tenant_artifact, config(user), directory, name, body)
    return answer, h.log, directory


def test_a_refused_walk_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        answer, log, directory = publish(owned, refuse="ancestor tenant is a symlink")
        check(answer == (False, f"switchyard: refusing to write {directory / 'run.sh'}: ancestor tenant is a symlink")
              and log == [("walk", Path("/"), Path(str(directory).lstrip("/")) / "run.sh")],
              f"walked from / to the file's directory, refused, and nothing opened or closed: {answer} {log}")
        with Host(owned, directory, refuse="missing") as h:
            answer = m.publish_tenant_artifact(config(), Path("relative/provision"), "x.json", b"{}")
        check(answer == (False, "switchyard: refusing to write relative/provision/x.json: missing")
              and h.log == [("walk", Path("/"), Path("relative/provision/x.json"))], f"a relative directory walks from /: {h.log}")


def test_the_file_is_staged_beside_its_name_and_renamed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        answer, log, directory = publish(owned)
        check(answer == (True, ""), f"success: {answer}")
        check(log == [("walk", Path("/"), Path(str(directory).lstrip("/")) / "run.sh"),
                      ("open", ".run.sh.new", FLAGS, 0o755, "DIR"), ("write", "FILE", b"#!/bin/sh\n"), ("fchmod", "FILE", 0o755),
                      ("geteuid",), ("getpwnam", "p387-owner"), ("fchown", "FILE", 4387, 4388), ("close", "FILE"),
                      ("rename", ".run.sh.new", "run.sh", "DIR", "DIR"), ("close", "DIR")],
              f"a shell script: staged 0755 relative to the directory, written, its mode, root looks the owner up and gives it, closed, renamed, the directory closed: {log}")
        check((directory / "run.sh").read_bytes() == b"#!/bin/sh\n" and (directory / "run.sh").stat().st_mode & 0o777 == 0o755
              and sorted(p.name for p in directory.iterdir()) == ["run.sh"], "the bytes at 0755, no staged file left")
        answer, log, directory = publish(owned, "plan.json", b"{}")
        check(("open", ".plan.json.new", FLAGS, 0o644, "DIR") in log and ("fchmod", "FILE", 0o644) in log
              and (directory / "plan.json").stat().st_mode & 0o777 == 0o644, "anything else: 0644")
        answer, log, _ = publish(owned, euid=1387)
        check(answer == (True, "") and ("getpwnam", "p387-owner") not in log and not [e for e in log if e[0] == "fchown"],
              f"not root: no lookup, no chown: {log}")
        answer, log, _ = publish(owned, user=None)
        check(answer == (True, "") and not [e for e in log if e[0] in ("getpwnam", "fchown")], "root with no account configured: no lookup")
        answer, log, _ = publish(owned, user="")
        check(answer == (True, "") and not [e for e in log if e[0] in ("getpwnam", "fchown")], "an empty account is no account")
        answer, log, _ = publish(owned, lookup=KeyError("p387-owner"))
        check(answer == (True, "") and ("getpwnam", "p387-owner") in log and not [e for e in log if e[0] == "fchown"],
              f"an account passwd does not know: looked up, not chowned, still written: {log}")


def test_failures_close_both_descriptors_and_report() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        for step in ("open", "write", "fchmod", "fchown", "rename"):
            failure = OSError(errno.EIO, f"{step} failed")
            answer, log, directory = publish(owned, **{step: failure})
            closes = [e[1] for e in log if e[0] == "close"]
            check(answer == (False, f"switchyard: could not write {directory / 'run.sh'}: {failure}"),
                  f"a failing {step}: reported as not written: {answer!r}")
            check(closes == (["DIR"] if step == "open" else ["FILE", "DIR"]) and log[-1] == ("close", "DIR")
                  and (step == "rename" or not [e for e in log if e[0] == "rename"]),
                  f"a failing {step}: the file's descriptor closed first, the directory's last, and no rename after a failure: {log}")
            check(not (directory / "run.sh").exists(), f"a failing {step}: nothing at the name")
            for leftover in directory.iterdir():
                leftover.unlink()
        strange = ValueError("not an OSError")
        answer, log, directory = publish(owned, write=strange)
        check(answer is strange and [e[1] for e in log if e[0] == "close"] == ["FILE", "DIR"],
              f"anything else propagates, with both descriptors still closed in order: {log}")


def test_end_to_end_a_link_at_the_name_is_replaced_not_followed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        directory = owned / "tenant" / "provision"
        directory.mkdir(parents=True)
        referent = owned / "elsewhere.txt"
        referent.write_bytes(b"not the tenant's")
        (directory / "plan.json").symlink_to(referent)
        chowned: list = []
        walked: list = []
        looked: list = []

        def no_lookup(user):
            # Not root, so no account is ever looked up here; a real one must never be attempted either.
            looked.append(user)
            raise AssertionError(f"a real account lookup was attempted for {user}")

        with patched(os, geteuid=lambda: 1387, fchown=lambda *a: chowned.append(a)), patched(pwd, getpwnam=no_lookup), \
                patched(t, _walk_no_follow=lambda anchor, relative: walked.append((anchor, relative)) or REAL_WALK(anchor, relative)):
            answer = m.publish_tenant_artifact(config(), directory, "plan.json", b"{}")
        check(answer == (True, "") and not (directory / "plan.json").is_symlink() and (directory / "plan.json").read_bytes() == b"{}"
              and referent.read_bytes() == b"not the tenant's" and chowned == [] and len(walked) == 1,
              "with the real walk: the link at the name is replaced by the file, its referent untouched")
        check(looked == [], f"not root: no account looked up: {looked}")
        (owned / "real").mkdir()
        (owned / "linked").symlink_to(owned / "real")
        with patched(os, geteuid=lambda: 1387, fchown=lambda *a: chowned.append(a)), patched(pwd, getpwnam=no_lookup):
            answer = m.publish_tenant_artifact(config(), owned / "linked", "x.json", b"{}")
        check(answer[0] is False and answer[1].startswith(f"switchyard: refusing to write {owned / 'linked' / 'x.json'}: ")
              and list((owned / "real").iterdir()) == [], f"with the real walk: a symlinked directory is refused and nothing written through it: {answer}")
        check(looked == [] and chowned == [], f"refused before anything: no lookup, no chown: {looked} {chowned}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_object",
             "test_the_seam_reads_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_it")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"tenant_artifact_publish_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
