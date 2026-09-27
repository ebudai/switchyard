#!/usr/bin/env python3
"""SYRD-380: tenant release root preparation, against the launcher it came out of.

Six definitions -- the writable-entry constants, the repair record's path, the
no-follow opener, the repair and the owner's read-only check -- moved unchanged
into `scripts/tenant_release_root.py`, and the launcher re-exports them. The
trusted owner identity stays in the launcher and is read through it. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's.
- **Seams (rule 24):** every launcher name these bodies read -- the trusted
  owner identity, the account lookups, the no-follow walk and the privileged
  record writers included -- and every sibling read when a body runs, is read
  through the launcher as often as before, so a patch on the launcher reaches
  each of them, which this test shows for all of them.
- **The behaviour is unchanged:** refusals for a symlink, a wrong type, another
  link or another account; every descriptor closed on every path; the trusted
  and unattributed paths; dry run, nothing needed, non-root; the record written
  before any ownership or mode change; and the owner-only read-only check.

Every ownership and permission operation here is this test's own recorder --
`os.fchown` and `os.fchmod` are never called for real -- and the effective uid,
the trusted identity, the account and home lookups, root's baseline, the walk
and the record writer are explicit stand-ins. The entries are real files in
owned temporary directories, owned by this test's own uid.
"""

from __future__ import annotations

import ast
import builtins
import errno
import inspect
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

from scripts import team_launcher as t  # noqa: E402
from scripts import tenant_release_root as m  # noqa: E402

CHECKS = 0
MOVED = ("RELEASE_ROOT_WRITABLE_DIRS", "RELEASE_ROOT_WRITABLE_FILES", "release_root_repair_record_path", "_open_release_root_entries",
         "prepare_tenant_release_root", "owner_release_root_problems")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'release_root_repair_record_path': {'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
    '_open_release_root_entries': {'RELEASE_ROOT_WRITABLE_DIRS': 1, 'RELEASE_ROOT_WRITABLE_FILES': 1, '_walk_no_follow': 1},
    'prepare_tenant_release_root': {'_open_release_root_entries': 1, '_write_private_json_atomic': 1, 'ensure_privileged_provision_dir': 1, 'home_dir_for_user': 1, 'privileged_baseline_plan_path': 2, 'read_plan_no_follow': 1, 'release_root_repair_record_path': 1, 'trusted_owner_identity': 1, 'uid_for_user': 1},
    'owner_release_root_problems': {'RELEASE_ROOT_WRITABLE_FILES': 1, 'current_user_name': 1, 'home_dir_for_user': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
ME = os.getuid()
OTHER = ME + 1


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


class Descriptors:
    """Track every descriptor the code under test opens (through the walk or `os.open`) and closes."""

    def __init__(self, uid_of: dict[str, int] | None = None) -> None:
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.names: dict[int, str] = {}
        self.walks = 0
        self.uid_of = uid_of or {}
        self.real_open, self.real_close, self.real_fstat = os.open, os.close, os.fstat

    def walk(self, anchor: Path, relative: Path):
        """The launcher's no-follow walk, stood in: open the board root the relative path names, or say why not."""
        self.walks += 1
        target = Path(anchor) / relative.parent
        if not os.path.lexists(target):
            return -1, "missing"
        if os.path.islink(target):
            return -1, "a symbolic link"
        fd = self.real_open(target, os.O_RDONLY | os.O_CLOEXEC)
        self.opened.append(fd)
        self.names[fd] = target.name
        return fd, ""

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        fd = self.real_open(path, flags, mode, dir_fd=dir_fd)
        self.opened.append(fd)
        self.names[fd] = Path(path).name
        return fd

    def fstat(self, fd):
        """The kernel's answer, with the owner this fixture says an entry has -- no real ownership is needed."""
        info = self.real_fstat(fd)
        uid = self.uid_of.get(self.names.get(fd, ""))
        if uid is None:
            return info
        fields = list(info[:10])
        fields[4] = uid
        return os.stat_result(fields)

    def close(self, fd):
        self.closed.append(fd)
        return self.real_close(fd)

    def all_closed(self) -> bool:
        return sorted(self.opened) == sorted(self.closed)

    def __enter__(self) -> "Descriptors":
        self.p = patched(os, open=self.open, close=self.close, fstat=self.fstat)
        self.p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.p.__exit__(*exc)
        for fd in set(self.opened) - set(self.closed):
            self.real_close(fd)


def board_in(tmp: str, *, releases: str | None = "dir", sha: str | None = "file", home: str = "home") -> Path:
    board = Path(tmp) / home / "p380-ticketboard-live"
    board.mkdir(parents=True)
    if releases == "dir":
        (board / "releases").mkdir()
    elif releases == "file":
        (board / "releases").write_text("")
    elif releases == "link":
        (Path(tmp) / "elsewhere").mkdir(exist_ok=True)
        (board / "releases").symlink_to(Path(tmp) / "elsewhere")
    if sha == "file":
        (board / "system-unit.sha256").write_text("x")
    elif sha == "hardlink":
        (board / "system-unit.sha256").write_text("x")
        os.link(board / "system-unit.sha256", Path(tmp) / "second-link")
    elif sha == "dir":
        (board / "system-unit.sha256").mkdir()
    elif sha == "link":
        (board / "system-unit.sha256").symlink_to(Path(tmp) / "nowhere")
    elif sha == "fifo":
        os.mkfifo(board / "system-unit.sha256")
    return board


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "tenant_release_root.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name
             or isinstance(n, ast.Assign) and [ast.unparse(x) for x in n.targets] == [name]]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.tenant_release_root as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.tenant_release_root", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_release_root")):
        result = python("import importlib, inspect, builtins; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_release_root as m; "
                        "d = inspect.signature(m.prepare_tenant_release_root).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "d['dry_run'].default is False and d['print_func'].default is builtins.print, "
                        "not hasattr(m, 'trusted_owner_identity') and t.trusted_owner_identity.__module__ == 'scripts.team_launcher')")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.RELEASE_ROOT_WRITABLE_DIRS == ("releases",) and m.RELEASE_ROOT_WRITABLE_FILES == ("system-unit.sha256",),
          "exactly the entries a deploy writes")
    check(m.os is os and m.stat is stat and m.errno is errno and m.Path is Path, "the standard-library names are the module's own")


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
            check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs: {imports}")
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
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.FunctionDef) and x is not node}
        check(not bound & set(through), f"{name}: nothing it binds itself, its nested closure included, is read through the launcher")
    tree = ast.parse((ROOT / "scripts" / "tenant_release_root.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("scripts" in line for line in top), f"nothing of Switchyard's is imported at the top: {top}")
    order = [n.name if isinstance(n, ast.FunctionDef) else ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(order == list(MOVED), f"the six in the launcher's order: {order}")


def test_the_launcher_reexports_the_six_and_keeps_the_trusted_identity() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_release_root"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the six, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED) and {"TrustedOwnerIdentity", "trusted_owner_identity"} <= defined,
          "the launcher defines none of the six, and still defines the trusted owner identity")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_record_lives_in_roots_directory() -> None:
    calls: list = []
    with patched(t, switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: Path("/nonexistent/syrd380")),
                 privileged_provision_dir=seam("privileged_provision_dir", lambda project, *, root: calls.append((project, root)) or root / project)):
        check(m.release_root_repair_record_path("p380") == Path("/nonexistent/syrd380/p380/release-root-repair.json")
              and calls == [("p380", Path("/nonexistent/syrd380"))], f"root's directory, root's name: {calls}")


def opened(board: Path, *, allowed: set[int] | None = None, uid_of: dict[str, int] | None = None, **walk):
    with Descriptors(uid_of) as d, patched(t, _walk_no_follow=seam("_walk_no_follow", walk.get("walk") or d.walk)):
        entries, problem = m._open_release_root_entries(board, allowed if allowed is not None else {0, ME}, "p380-owner", "p380")
        names = [Path(path).name for path, *_ in entries]
        bits = [b for *_, b in entries]
        fds = [fd for _p, fd, *_ in entries]
        for fd in fds:
            os.close(fd)
        closed = d.all_closed()
    return names, bits, problem, closed


def test_the_entries_are_opened_following_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = Path(tmp) / "home" / "p380-ticketboard-live"
        check(opened(board)[:3] == ([], [], ""), "no board root yet: nothing to prepare, and no problem")
        seen: list = []
        with patched(t, _walk_no_follow=lambda anchor, relative: seen.append((anchor, relative)) or (-1, "a symbolic link")):
            check(m._open_release_root_entries(board, {0, ME}, "o", "p380") == ([], f"{board} cannot be walked safely: a symbolic link")
                  and seen == [(Path("/"), Path(str(board).lstrip("/")) / "_")], f"walked from the root anchor, following nothing: {seen}")
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        names, bits, problem, closed = opened(board)
        check((names, bits, problem, closed) == (["p380-ticketboard-live", "releases", "system-unit.sha256"], [0o700, 0o700, 0o600], "", True),
              f"the board root, then releases/, then the sha file, with the bits the owner needs: {names} {bits} {problem}")
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp, releases=None, sha=None)
        check(opened(board)[:3] == (["p380-ticketboard-live"], [0o700], ""), "missing entries are simply not there yet")
    for kind, expect in (("releases=link", "is a symbolic link; refusing to repair p380's release root"),
                         ("releases=file", "is a symbolic link; refusing to repair p380's release root"),
                         ("sha=link", "is a symbolic link; refusing to repair p380's release root"),
                         ("sha=hardlink", "is not a regular file with one link"),
                         ("sha=dir", "is not a regular file with one link"),
                         ("sha=fifo", "is not a regular file with one link")):
        with tempfile.TemporaryDirectory() as tmp:
            key, value = kind.split("=")
            board = board_in(tmp, **{key: value})
            names, _bits, problem, closed = opened(board)
            entry = "releases" if key == "releases" else "system-unit.sha256"
            check(names == [] and problem == f"{board / entry} {expect}" and closed, f"{kind}: refused, every descriptor closed: {problem!r} {closed}")
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        names, _bits, problem, closed = opened(board, allowed={0, OTHER})
        check(names == [] and problem == f"{board} is owned by uid {ME}, neither p380-owner nor root, so it cannot be attributed to this tenant" and closed,
              f"a board root of another account: {problem}")
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        names, _bits, problem, closed = opened(board, uid_of={"releases": OTHER})
        check(names == [] and problem == f"{board / 'releases'} is owned by uid {OTHER}, neither p380-owner nor root, so it cannot be attributed to this tenant"
              and closed, f"an entry of another account, under a root the owner holds: {problem}")
        names, _bits, problem, closed = opened(board, uid_of={"releases": 0, "p380-ticketboard-live": 0})
        check(problem == "" and names == ["p380-ticketboard-live", "releases", "system-unit.sha256"], f"root's own legacy entries are accepted: {problem}")
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        (board / "file-root").write_text("")
        names, _bits, problem, closed = opened(board / "file-root")
        check(names == [] and problem.startswith(f"{board / 'file-root'} is owned by uid {ME}") and closed, "a board root that is not a directory")


def identity(*, trusted: bool, home: Path, problems: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(trusted=trusted, owner_user="p380-owner", owner_uid=ME, owner_gid=4380, owner_home=str(home), problems=problems)


def prepared(tmp: str, board: Path, *, trusted: bool = True, euid: int = 0, dry_run: bool = False, baseline: object = "board",
             owner_uid: int = ME, fail_at: str = "", baseline_on_disk: bool = True, uid_of: dict[str, int] | None = None,
             baseline_path_error: OSError | None = None):
    """Run the repair with every account, privilege, ownership and mode operation stood in; return what happened."""
    log: list = []
    said: list[str] = []
    home = board.parent
    plan_path = Path(tmp) / "etc" / "p380" / "plan.json"
    if baseline_on_disk:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text("{}")
    else:
        plan_path.unlink(missing_ok=True)
    base = SimpleNamespace(data={"board_root": str(board) if baseline == "board" else baseline}) if baseline is not None else None

    def fchown(fd, uid, gid):
        log.append(("fchown", uid, gid))
        if fail_at == "fchown":
            raise PermissionError(errno.EPERM, "Operation not permitted")

    def fchmod(fd, mode):
        log.append(("fchmod", oct(mode)))
    stand_ins = dict(
        trusted_owner_identity=seam("trusted_owner_identity", lambda project: identity(trusted=trusted, home=home, problems=("root has no baseline for p380",))),
        read_plan_no_follow=seam("read_plan_no_follow", lambda path, *, require_root_owned: log.append(("baseline", path.name, require_root_owned)) or (base, "unreadable")),
        privileged_baseline_plan_path=seam("privileged_baseline_plan_path", lambda project: Unreadable(baseline_path_error) if baseline_path_error else plan_path),
        uid_for_user=seam("uid_for_user", lambda user: owner_uid if user == "p380-owner" else None),
        home_dir_for_user=seam("home_dir_for_user", lambda user: home if user == "p380-owner" else None),
        release_root_repair_record_path=seam("release_root_repair_record_path", lambda project: Path(tmp) / "priv" / "release-root-repair.json"),
        ensure_privileged_provision_dir=seam("ensure_privileged_provision_dir", lambda d: log.append(("ensure", d.name))),
        _write_private_json_atomic=seam("_write_private_json_atomic", lambda path, record: log.append(("record", path.name, record))),
        _open_release_root_entries=seam("_open_release_root_entries", m._open_release_root_entries),
    )
    with Descriptors(uid_of) as d, patched(t, _walk_no_follow=d.walk, **stand_ins), patched(os, geteuid=lambda: euid, fchown=fchown, fchmod=fchmod):
        result = judged(m.prepare_tenant_release_root, SimpleNamespace(project="p380", run_as_user="p380-owner"), dry_run=dry_run, print_func=said.append)
        closed = d.all_closed()
    log.append(("walks", d.walks))
    return result, said, log, closed


class Unreadable:
    """A baseline path whose lstat fails for a reason other than absence."""

    def __init__(self, error: OSError) -> None:
        self.error = error

    def lstat(self):
        raise self.error

    @property
    def name(self) -> str:
        return "plan.json"


def test_a_board_root_that_needs_nothing_is_ready() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        result, said, log, closed = prepared(tmp, board)
        check(result == [] and said == [] and [e[0] for e in log] == ["baseline", "walks"] and log[0][2] is True and closed,
              f"owned and writable already: nothing changed, root's baseline read root-owned only: {log}")
        REACHED.update({"trusted_owner_identity", "read_plan_no_follow", "privileged_baseline_plan_path"})
        (board / "releases" / "r1").mkdir()
        (board / "current").symlink_to("releases/r1")
        (board / "canary.env").write_text("")
        result, said, log, closed = prepared(tmp, board)
        check(result == [] and said == [] and closed, f"the activation link and canary.env are the deploy's to rename, never opened or repaired: {result}")


def test_root_repairs_after_recording_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        os.chmod(board / "releases", 0o555)
        os.chmod(board / "system-unit.sha256", 0o400)
        try:
            result, said, log, closed = prepared(tmp, board)
        finally:
            os.chmod(board / "releases", 0o755)
        record = next((entry for entry in log if entry[0] == "record"), ("record", "", {}))[2]
        check(result == [] and [e[0] for e in log] == ["baseline", "ensure", "record", "fchown", "fchmod", "fchown", "fchmod", "walks"] and closed,
              f"recorded before anything changes, then owner and mode per entry, every descriptor closed: {log}")
        check(log[3] == ("fchown", ME, 4380) and log[4] == ("fchmod", oct(0o755)) and log[6] == ("fchmod", oct(0o600)),
              f"the trusted owner's uid and gid, and exactly the bits the owner needs added: {log}")
        check(record.get("schema") == "switchyard.release-root-repair.v1" and record.get("owner") == "p380-owner"
              and [e["path"] for e in record.get("entries", [])] == [str(board / "releases"), str(board / "system-unit.sha256")]
              and record["entries"][0]["mode_before"] == "555" and record["entries"][0]["mode_after"] == "755" and record["entries"][1]["uid_before"] == ME,
              f"what it was about to do, entry by entry: {record}")
        check(said == [f"switchyard: gave {board / 'releases'} to p380-owner so it can publish a release",
                       f"switchyard: gave {board / 'system-unit.sha256'} to p380-owner so it can publish a release",
                       f"switchyard: recorded that repair in {Path(tmp) / 'priv' / 'release-root-repair.json'}"], f"{said}")
        REACHED.update({"ensure_privileged_provision_dir", "_write_private_json_atomic", "release_root_repair_record_path"})


def test_a_legacy_root_owned_entry_is_given_to_the_owner() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        result, said, log, closed = prepared(tmp, board, uid_of={"releases": 0})
        check(result == [] and [e[0] for e in log] == ["baseline", "ensure", "record", "fchown", "fchmod", "walks"]
              and log[3] == ("fchown", ME, 4380) and log[4] == ("fchmod", oct(0o755)) and closed,
              f"a root-owned releases/ with the right mode is still given to the owner, and only its owner changes: {log}")


def test_repairs_that_cannot_or_must_not_happen() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        os.chmod(board / "releases", 0o555)
        try:
            result, said, log, closed = prepared(tmp, board, dry_run=True)
            check(result == [] and said == [f"switchyard: would give {board / 'releases'} to p380-owner (now uid {ME}, mode 555 -> 755) so the owner can publish a release"]
                  and "fchown" not in [e[0] for e in log] and "record" not in [e[0] for e in log] and closed, f"a dry run describes and writes nothing: {said} {log}")
            result, said, log, closed = prepared(tmp, board, euid=1006)
            check(result == [f"{board} holds entries p380-owner cannot write; repairing them is root's -- run `sudo switchyard upgrade p380`"]
                  and "record" not in [e[0] for e in log] and closed, f"not root: said, nothing written: {result}")
            result, said, log, closed = prepared(tmp, board, fail_at="fchown")
            check(result == ["could not repair p380's release root: [Errno 1] Operation not permitted"]
                  and [e[0] for e in log] == ["baseline", "ensure", "record", "fchown", "walks"] and closed,
                  f"a repair that stops halfway was recorded first, and every descriptor is still closed: {result} {log}")
            result, said, log, closed = prepared(tmp, board, baseline=None)
            check(result == ["p380's board root cannot be established from root's baseline: unreadable"] and closed, f"{result}")
            result, said, log, closed = prepared(tmp, board, baseline=str(Path(tmp) / "elsewhere"))
            check(result == [f"root's baseline names {Path(tmp) / 'elsewhere'} for p380, not {board}; a release root elsewhere is not one this repair will touch"],
                  f"a board root other than <home>/<project>-ticketboard-live: {result}")
        finally:
            os.chmod(board / "releases", 0o755)


def test_an_unattributed_owner_is_only_inspected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        result, said, log, closed = prepared(tmp, board, trusted=False)
        check(result == [] and "baseline" not in [e[0] for e in log] and closed, f"nothing needed: ready, without root's baseline: {log}")
        REACHED.update({"uid_for_user", "home_dir_for_user"})
        os.chmod(board / "releases", 0o555)
        try:
            result, said, log, closed = prepared(tmp, board, trusted=False)
            check(result == [f"p380's owner must be given {board / 'releases'} before it can publish a release, and root will not attribute them: "
                             "root has no baseline for p380"] and "fchown" not in [e[0] for e in log] and closed, f"needed: refused, never repaired: {result}")
            result, said, log, closed = prepared(tmp, board, trusted=False, dry_run=True)
            check(result[0].startswith("p380's owner must be given") and said == [], "a dry run with root's baseline already staged still refuses")
            result, said, log, closed = prepared(tmp, board, trusted=False, dry_run=True, baseline_on_disk=False)
            check(result == [] and said == [f"switchyard: would give {board / 'releases'} to p380-owner (now uid {ME}, mode 555 -> 755) once root's baseline is staged"],
                  f"a dry run before the baseline is staged predicts the real run instead of a stop: {said}")
        finally:
            os.chmod(board / "releases", 0o755)
        result, said, log, closed = prepared(tmp, board, trusted=False, owner_uid=None)
        check(result == [] and log == [("walks", 0)], f"no such account: nothing is even walked: {log}")
        os.chmod(board / "releases", 0o555)
        try:
            result, said, log, closed = prepared(tmp, board, trusted=False, dry_run=True, baseline_path_error=PermissionError(errno.EACCES, "Permission denied"))
            check(isinstance(result, list) and len(result) == 1 and result[0].startswith("p380's owner must be given") and said == [],
                  f"a baseline that cannot be looked at is not an absent one: the dry run still refuses: {result}")
            result, said, log, closed = prepared(tmp, board, trusted=False, baseline_on_disk=False)
            check(isinstance(result, list) and len(result) == 1 and result[0].startswith("p380's owner must be given") and said == [],
                  f"a real run with no baseline refuses; only a dry run predicts: {result}")
        finally:
            os.chmod(board / "releases", 0o755)


def test_the_owner_asks_read_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp)
        home = board.parent
        config = SimpleNamespace(project="p380", run_as_user="p380-owner")
        base = dict(home_dir_for_user=seam("home_dir_for_user", lambda user: home if user == "p380-owner" else None))
        with patched(t, current_user_name=seam("current_user_name", lambda: "somebody-else"), **base), patched(os, access=refuse("an access check")):
            check(m.owner_release_root_problems(config) == [], "asked by anyone but the owner: nothing to say")
        with patched(t, current_user_name=lambda: "p380-owner", home_dir_for_user=lambda user: None), patched(os, access=refuse("an access check")):
            check(judged(m.owner_release_root_problems, config) == [], "no home: nothing to say")
        with patched(t, current_user_name=lambda: "p380-owner", **base), patched(os, access=lambda path, mode: True):
            check(m.owner_release_root_problems(config) == [], "all writable: nothing to say")
        asked: list = []
        with patched(t, current_user_name=lambda: "p380-owner", **base), \
                patched(os, access=lambda path, mode: asked.append((Path(path).name, mode)) or Path(path).name == "p380-ticketboard-live"):
            problems = m.owner_release_root_problems(config)
        check(problems == [f"p380-owner cannot write {board / 'releases'}, {board / 'system-unit.sha256'}, so its deploy could not publish a release; "
                           "`sudo switchyard upgrade p380` repairs a legacy release root"]
              and asked == [("p380-ticketboard-live", os.W_OK), ("releases", os.W_OK), ("system-unit.sha256", os.W_OK)], f"{problems} {asked}")
        with patched(t, current_user_name=lambda: "p380-owner", RELEASE_ROOT_WRITABLE_FILES=("syrd380.extra",), **base), patched(os, access=lambda path, mode: False):
            check(m.owner_release_root_problems(config) == [f"p380-owner cannot write {board}, {board / 'releases'}, so its deploy could not publish a release; "
                                                            "`sudo switchyard upgrade p380` repairs a legacy release root"],
                  "only entries that exist, and the launcher's list of files decides which")
        REACHED.add("RELEASE_ROOT_WRITABLE_FILES")
    with tempfile.TemporaryDirectory() as tmp:
        board = board_in(tmp, releases=None)
        (board / "syrd380-dir").mkdir()
        with patched(t, RELEASE_ROOT_WRITABLE_DIRS=("syrd380-dir",)):
            with Descriptors() as d, patched(t, _walk_no_follow=d.walk):
                entries, problem = m._open_release_root_entries(board, {0, ME}, "o", "p380")
                names = [Path(path).name for path, *_ in entries]
                for _p, fd, *_ in entries:
                    os.close(fd)
        check(names == ["p380-ticketboard-live", "syrd380-dir", "system-unit.sha256"] and problem == "", f"the launcher's list of directories decides: {names}")
        REACHED.add("RELEASE_ROOT_WRITABLE_DIRS")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_six_and_keeps_the_trusted_identity")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"tenant_release_root_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
