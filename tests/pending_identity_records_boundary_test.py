#!/usr/bin/env python3
"""SYRD-385: root's pending-identity records, against the launcher they came out of.

The schema, the record's path, its writer and reader, and the per-role answer
moved unchanged into `scripts/pending_identity_records.py`, and the launcher
re-exports them. The current user, the canonical identities, the provision
root and directory, the directory repair, the mode selector and the home lookup
stay elsewhere. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the schema is the baseline's
  literal and the config types are annotations only.
- **Seams (rule 24):** every launcher facility, sibling and the schema read when
  a body runs is read through the launcher as often as before, so a patch on the
  launcher reaches each of them, which this test shows for all of them.
- **The behaviour is unchanged:** the record's place; the identities computed
  first and returned as they are, written only by root; the payload and its
  exact bytes; the repair before anything is opened; per record a no-follow
  `.name.new` opened 0600, then write, the helper's mode, a tolerated chown to
  root, close and rename; a failure reported on stderr and the identities still
  returned; every read fallback, filter and coercion; the per-role answer.

Everything real lives in an owned temporary directory. `os.fchown` is always a
recorder, `os.geteuid` a stand-in, and every other file operation the writer
makes is recorded and refused outside the owned directory.
"""

from __future__ import annotations

import ast
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pending_identity_records as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402

CHECKS = 0
MOVED = ("PENDING_IDENTITIES_SCHEMA", "pending_identities_path", "write_pending_identities", "read_pending_identities",
         "pending_identity_for")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'pending_identities_path': {'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
    'write_pending_identities': {'PENDING_IDENTITIES_SCHEMA': 1, 'canonical_role_identities': 1, 'current_user_name': 1, 'ensure_privileged_provision_dir': 1, 'pending_identities_path': 1, 'privileged_artifact_mode': 1},
    'read_pending_identities': {'PENDING_IDENTITIES_SCHEMA': 1, 'canonical_role_identities': 3, 'pending_identities_path': 1},
    'pending_identity_for': {'current_user_name': 1, 'home_dir_for_user': 1, 'read_pending_identities': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
PROJECT = "p385"
FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
CANONICAL = {"main": {"account": "p385-main", "home": "/home/p385-main", "worktree": "/w/main"},
             "audit": {"account": "p385-audit", "home": "/home/p385-audit", "worktree": "/w/audit"}}


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


def config(owner: str | None = "p385-owner") -> SimpleNamespace:
    return SimpleNamespace(project=PROJECT, run_as_user=owner)


def module_def(name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / "scripts" / "pending_identity_records.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.pending_identity_records as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_literal() -> None:
    for order in (("scripts.pending_identity_records", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.pending_identity_records")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.pending_identity_records as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "m.PENDING_IDENTITIES_SCHEMA == 'switchyard.pending-identities.v1' and all(p.default is inspect.Parameter.empty "
                        "for f in (m.pending_identities_path, m.write_pending_identities, m.read_pending_identities, m.pending_identity_for) "
                        "for p in inspect.signature(f).parameters.values()), "
                        "not hasattr(m, 'ProjectConfig') and not hasattr(m, 'RoleConfig') and not hasattr(m, 'current_user_name'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.json is json and m.sys is sys and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "pending_identity_records.py").read_text(encoding="utf-8"))
    for name in MOVED:
        if name.isupper():
            assigned = [n for n in tree.body if isinstance(n, ast.Assign) and [ast.unparse(x) for x in n.targets] == [name]]
            check(len(assigned) == 1 and isinstance(assigned[0].value, ast.Constant), f"{name}: one literal assignment")
            continue
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
        types = [x for x in ast.walk(node) if isinstance(x, ast.Name) and x.id in ("ProjectConfig", "RoleConfig")]
        check(types and all(id(x) in annotation for x in types), f"{name}: the config types only in annotations")
        bound = {a.arg for a in node.args.args + node.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.ExceptHandler) and x.name}
        check(not bound & set(through), f"{name}: nothing it binds itself is read through the launcher")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import json", "import os", "import sys", "from pathlib import Path", "from typing import TYPE_CHECKING"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig, RoleConfig"],
          f"only the standard library at the top, and the config types under TYPE_CHECKING: {top} {tc}")
    names = [n.name if isinstance(n, ast.FunctionDef) else ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(names == list(MOVED), f"the five in the launcher's order: {names}")


def test_the_launcher_reexports_the_five() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.pending_identity_records"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the five, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later
    # slice moves it on (SYRD-399) -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_staged_tooling_dir", "process_uid"} <= defined | exported,
          "the launcher defines none of the five, and keeps the neighbours on either side, its own or re-exported")


# --- the record's place --------------------------------------------------------------------------------------------


def test_the_record_lives_in_roots_provision_directory() -> None:
    asked: list = []
    root = Path("/fixture/provision")
    with patched(t, switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: root),
                 privileged_provision_dir=seam("privileged_provision_dir",
                                               lambda project, *, root: asked.append((project, root)) or root / "tenants" / project)):
        check(m.pending_identities_path(config()) == root / "tenants" / PROJECT / "pending-identities.json" and asked == [(PROJECT, root)],
              f"pending-identities.json in the project's directory under root's provision root: {asked}")


# --- writing -------------------------------------------------------------------------------------------------------


class Host:
    """The writer's file operations, recorded in order, confined to one owned directory."""

    def __init__(self, owned: Path, **fail: BaseException) -> None:
        self.owned, self.fail = owned, fail
        self.log: list = []
        self.fds: dict[int, str] = {}

    def inside(self, path) -> None:
        assert Path(path).is_relative_to(self.owned), f"refused outside the owned directory: {path}"

    def __enter__(self) -> "Host":
        real = {n: getattr(os, n) for n in ("open", "write", "fchmod", "close")}
        real_replace = Path.replace

        def step(name, fd, *rest):
            self.log.append((name, self.fds.get(fd), *rest))
            if name in self.fail:
                raise self.fail[name]

        def open_(path, flags, mode=0o777, *, dir_fd=None):
            self.inside(path)
            self.log.append(("open", Path(path).name, flags, mode))
            if "open" in self.fail:
                raise self.fail["open"]
            fd = real["open"](path, flags, mode, dir_fd=dir_fd)
            self.fds[fd] = Path(path).name
            return fd

        def write(fd, body):
            step("write", fd, bytes(body))
            return real["write"](fd, body)

        def fchmod(fd, mode):
            step("fchmod", fd, mode)
            return real["fchmod"](fd, mode)

        def fchown(fd, uid, gid):
            step("fchown", fd, uid, gid)

        def close(fd):
            self.log.append(("close", self.fds.get(fd)))
            return real["close"](fd)

        def replace(path, target):
            self.inside(path); self.inside(target)
            self.log.append(("replace", Path(path).name, Path(target).name))
            return real_replace(path, target)

        self.p = [patched(os, open=open_, write=write, fchmod=fchmod, fchown=fchown, close=close), patched(Path, replace=replace)]
        for p in self.p:
            p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self.p):
            p.__exit__(*exc)


def write(owned: Path, *, euid: int = 0, owner: str | None = "p385-owner", ensure_error: BaseException | None = None, **fail):
    """write_pending_identities with every effect recorded; returns (answer, host, stderr, calls)."""
    calls: list = []
    path = owned / "tenants" / PROJECT / "pending-identities.json"

    def ensure(directory):
        calls.append(("ensure", directory))
        if ensure_error is not None:
            raise ensure_error
        directory.mkdir(parents=True, exist_ok=True)
        return []

    err = io.StringIO()
    with Host(owned, **fail) as host, patched(os, geteuid=lambda: calls.append(("euid",)) or euid), patched(sys, stderr=err), \
            patched(t, canonical_role_identities=seam("canonical_role_identities", lambda c: calls.append(("canonical", c.project)) or CANONICAL),
                    pending_identities_path=seam("pending_identities_path", lambda c: path),
                    current_user_name=seam("current_user_name", lambda: "p385-current"),
                    ensure_privileged_provision_dir=seam("ensure_privileged_provision_dir", lambda d: host.log.append(("ensure",)) or ensure(d)),
                    privileged_artifact_mode=seam("privileged_artifact_mode", lambda name: calls.append(("mode", name)) or 0o640)):
        return judged(m.write_pending_identities, config(owner)), host, err.getvalue(), calls


def test_only_root_writes_and_the_identities_are_returned_either_way() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        answer, host, err, calls = write(owned, euid=1385)
        check(answer is CANONICAL and calls == [("canonical", PROJECT), ("euid",)] and host.log == [] and err == "" and list(owned.iterdir()) == [],
              f"not root: the canonical identities, derived first, returned unchanged, and nothing written: {calls} {host.log}")


def test_root_writes_the_record_beside_itself_and_renames_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        path = owned / "tenants" / PROJECT / "pending-identities.json"
        payload = {"schema": "switchyard.pending-identities.v1", "project": PROJECT, "owner": "p385-owner", "roles": CANONICAL}
        body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        answer, host, err, calls = write(owned)
        staged = ".pending-identities.json.new"
        check(answer is CANONICAL and err == "", f"root: the same identities returned: {err}")
        check(host.log == [("ensure",), ("open", staged, FLAGS, 0o600), ("write", staged, body), ("fchmod", staged, 0o640),
                           ("fchown", staged, 0, 0), ("close", staged), ("replace", staged, "pending-identities.json")],
              f"repaired first; then a no-follow staged file 0600, the bytes, the helper's mode, root, close, rename: {host.log}")
        check(("mode", "pending-identities.json") in calls and path.read_bytes() == body and json.loads(body)["roles"] == CANONICAL
              and sorted(p.name for p in path.parent.iterdir()) == ["pending-identities.json"],
              "the mode asked for the record's own name; sorted, indented JSON with a newline; no staged file left")
        answer, host, err, calls = write(owned, owner=None)
        check(json.loads(path.read_text())["owner"] == "p385-current", "no owner configured: the current user")
        with patched(t, PENDING_IDENTITIES_SCHEMA="syrd385.schema"):
            write(owned)
            check(json.loads(path.read_text())["schema"] == "syrd385.schema", "the schema is the launcher's")
            REACHED.add("PENDING_IDENTITIES_SCHEMA")


def test_a_failed_write_is_reported_and_the_identities_still_returned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        owned = Path(tmp)
        path = owned / "tenants" / PROJECT / "pending-identities.json"
        refused = PermissionError(errno.EPERM, "fchown refused")
        answer, host, err, calls = write(owned, fchown=refused)
        check(answer is CANONICAL and err == "" and [e[0] for e in host.log][-2:] == ["close", "replace"] and path.exists(),
              f"a refused chown is tolerated: the record is still closed and renamed into place: {host.log}")
        path.unlink()
        for step in ("open", "write", "fchmod"):
            failure = OSError(errno.EIO, f"{step} failed")
            answer, host, err, calls = write(owned, **{step: failure})
            steps = [e[0] for e in host.log]
            check(answer is CANONICAL and err == f"switchyard: could not record {PROJECT} pending identities: {failure}\n"
                  and "replace" not in steps and (steps[-1] == "close" if step != "open" else steps[-1] == "open")
                  and not path.exists(),
                  f"a failing {step}: reported on stderr, the descriptor closed, nothing renamed, the identities returned: {steps} {err!r}")
            for leftover in path.parent.iterdir():
                leftover.unlink()
        failure = OSError(errno.EACCES, "ensure failed")
        answer, host, err, calls = write(owned, ensure_error=failure)
        check(answer is CANONICAL and err == f"switchyard: could not record {PROJECT} pending identities: {failure}\n"
              and host.log == [("ensure",)], f"a failing repair: reported, nothing opened: {host.log}")
        strange = ValueError("not an OSError")
        answer, host, err, calls = write(owned, fchown=strange)
        check(answer is strange and [e[0] for e in host.log][-1] == "close", f"only an OSError is tolerated: {answer!r} {host.log}")


# --- reading -------------------------------------------------------------------------------------------------------


def read(path: Path):
    with patched(t, pending_identities_path=seam("pending_identities_path", lambda c: path),
                 canonical_role_identities=seam("canonical_role_identities", lambda c: CANONICAL)):
        return judged(m.read_pending_identities, config())


def test_the_record_is_read_back_or_the_canonical_derivation_used() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pending-identities.json"
        check(read(path) is CANONICAL, "no record: the canonical derivation")
        path.mkdir()
        check(read(path) is CANONICAL, "an unreadable record: the canonical derivation")
        path.rmdir()
        for text, why in (("{not json", "not JSON"), ('{"roles": {}}', "no schema"),
                          ('{"schema": "switchyard.pending-identities.v2", "roles": {}}', "another schema"),
                          ('{"schema": "switchyard.pending-identities.v1", "roles": ["main"]}', "roles not a mapping"),
                          ('{"schema": "switchyard.pending-identities.v1"}', "no roles")):
            path.write_text(text)
            check(read(path) is CANONICAL, f"{why}: the canonical derivation")
        path.write_text("[1, 2]")
        answer = read(path)
        check(isinstance(answer, AttributeError), f"a JSON document that is not an object is not caught (baseline): {answer!r}")
        record = {"schema": "switchyard.pending-identities.v1", "roles": {
            "zeta": {"account": "p385-zeta", "home": "/home/p385-zeta", "worktree": "/w/zeta"},
            "alpha": {"account": 385, "home": None},
            "empty": {"account": "", "home": "/h"},
            "none": {"home": "/h"},
            "listed": ["p385-listed"],
            7: {"account": "p385-seven", "worktree": 0},
        }}
        path.write_text(json.dumps(record))
        answer = read(path)
        check(answer == {"zeta": {"account": "p385-zeta", "home": "/home/p385-zeta", "worktree": "/w/zeta"},
                         "alpha": {"account": "385", "home": "", "worktree": ""},
                         "7": {"account": "p385-seven", "home": "", "worktree": ""}}
              and list(answer) == ["zeta", "alpha", "7"],
              f"only entries naming an account, as strings with empty fields for what is missing, in the record's order: {answer}")
        with patched(t, PENDING_IDENTITIES_SCHEMA="syrd385.schema"):
            check(read(path) is CANONICAL, "the schema compared is the launcher's")


# --- one role ------------------------------------------------------------------------------------------------------


def test_one_roles_account_now_or_the_one_it_is_prepared_for() -> None:
    role = lambda account, name="main": SimpleNamespace(role=name, run_as_user=account, workdir=f"/w/{name}")
    homes: list = []
    pending = {"main": {"account": "p385-main", "home": "/home/p385-main", "worktree": "/w/main"}}

    def answer(cfg, r, home=None):
        with patched(t, current_user_name=seam("current_user_name", lambda: "p385-current"),
                     home_dir_for_user=seam("home_dir_for_user", lambda user: homes.append(user) or home),
                     read_pending_identities=seam("read_pending_identities", lambda c: pending)):
            return judged(m.pending_identity_for, cfg, r)

    check(answer(config(), role("p385-other"), home=Path("/srv/p385-other")) == {"account": "p385-other", "home": "/srv/p385-other", "worktree": "/w/main"}
          and homes == ["p385-other"], f"a role running as its own account: that account, its home and workdir: {homes}")
    check(answer(config(), role("p385-other")) == {"account": "p385-other", "home": "/home/p385-other", "worktree": "/w/main"},
          "no home known: /home/<account>")
    homes.clear()
    check(answer(config(), role("p385-owner")) == pending["main"] and homes == [], "a role running as the owner: the pending record's entry")
    check(answer(config(), role(None)) == pending["main"], "a role with no account of its own: the pending record's entry")
    check(answer(config(None), role("p385-current")) == pending["main"], "no owner configured: the current user is the owner")
    check(answer(config(None), role("p385-owner")) == {"account": "p385-owner", "home": "/home/p385-owner", "worktree": "/w/main"},
          "no owner configured, and a role running as somebody else")
    check(answer(config(), role(None, "absent")) == {}, "a role the record does not name: nothing")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_the_literal",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_five")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"pending_identity_records_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
