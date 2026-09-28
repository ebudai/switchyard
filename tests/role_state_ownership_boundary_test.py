#!/usr/bin/env python3
"""SYRD-392: a tenant's role-state ownership inspection and repair, against the launcher it came out of.

Nine definitions -- the depth limit, the no-follow root opener and its
root-placed-link rule, the quiet close, the tree walk, the store roots, the
read-only ownership findings, the owner-name lookup and the repair -- moved
unchanged into `scripts/role_state_ownership.py`, and the launcher re-exports
them. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the `print_func` default is bound
  when the function is defined; `ProjectConfig` is an annotation only.
- **Seams (rule 24):** the current user, each role's session directory, and
  every name defined here that another definition here reads when it runs are
  read through the launcher as often as before, so a patch or rebind on the
  launcher reaches each of them, which this test shows for all of them.
- **The behaviour is unchanged:** each root component opened from its
  parent's descriptor without following a link, unless root placed it in a
  directory only root can write; a tree visited in sorted order, never through
  a symlink, no deeper than the limit, with a chown that acts through the
  descriptor; every descriptor closed on every path; the read-only findings,
  each path once; the repair's refusal first, root only, dry run, the chown of
  each wrong-owner path through its descriptor, the recheck, and every message.

Everything real is an owned temporary tree. `os.chown` and `os.fchown` are
always recorders, `pwd` lookups and `os.geteuid` are stand-ins, and root-owned
files are simulated by an `os.lstat`/`os.fstat` stand-in -- no ownership
changes and no account is looked up.
"""

from __future__ import annotations

import ast
import errno
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import role_state_ownership as m  # noqa: E402
from scripts import session_paths  # noqa: E402
from scripts import team_launcher as t  # noqa: E402

CHECKS = 0
MOVED = ("STATE_TREE_MAX_DEPTH", "_open_tenant_state_root", "_root_placed_link", "_close_quietly", "_walk_tenant_state_tree",
         "role_state_roots", "role_state_ownership_problems", "_uid_owner_name", "repair_role_state_ownership")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    # SYRD-393 closes the walked-from descriptor on each of the four early returns, through the launcher: 2 + 4.
    '_open_tenant_state_root': {'_close_quietly': 6, '_root_placed_link': 1},
    '_walk_tenant_state_tree': {'STATE_TREE_MAX_DEPTH': 2, '_open_tenant_state_root': 1},
    'role_state_roots': {'role_session_dir': 1},
    'role_state_ownership_problems': {'_uid_owner_name': 1, '_walk_tenant_state_tree': 1, 'current_user_name': 1, 'role_state_roots': 1},
    'repair_role_state_ownership': {'_walk_tenant_state_tree': 1, 'current_user_name': 1, 'role_state_ownership_problems': 2, 'role_state_roots': 1},
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
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is an answer to compare
        return exc


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


def seam(name: str, function):
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


class Host:
    """Descriptors opened and closed, chowns recorded (never real), and chosen stats for chosen names."""

    def __init__(self, *, root_links: dict | None = None, holder_mode: int = 0o755, scandir_fails: set | None = None,
                 lstat_fails: set | None = None, link_uid: int = 0, holder_uid: int = 0, lstat_as_dir: set | None = None,
                 close_fails: int = 0) -> None:
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.chowned: list = []
        self.root_links, self.holder_mode = root_links or {}, holder_mode
        self.scandir_fails, self.lstat_fails = scandir_fails or set(), lstat_fails or set()
        self.real = {n: getattr(os, n) for n in ("open", "close", "lstat", "fstat", "scandir")}
        self.holders: set[int] = set()
        self.live: set[int] = set()
        self.link_uid, self.holder_uid, self.lstat_as_dir = link_uid, holder_uid, lstat_as_dir or set()
        self.double_closed: list[int] = []
        self.close_fails = close_fails  # the Nth close releases the descriptor and then reports EIO, as Linux does

    def __enter__(self) -> "Host":
        def open_(path, flags, mode=0o777, *, dir_fd=None):
            fd = self.real["open"](path, flags, mode, dir_fd=dir_fd)
            self.opened.append(fd)
            self.live.add(fd)
            return fd

        def close(fd):
            self.closed.append(fd)
            if fd not in self.live:
                self.double_closed.append(fd)
            self.live.discard(fd)
            answer = self.real["close"](fd)
            if self.close_fails and len(self.closed) == self.close_fails:
                raise OSError(errno.EIO, "Input/output error")
            return answer

        def lstat(path, *, dir_fd=None):
            if str(path) in self.lstat_fails:
                raise PermissionError(errno.EACCES, "Permission denied")
            info = self.real["lstat"](path, dir_fd=dir_fd)
            if str(path) in self.root_links and dir_fd is not None:
                self.holders.add(dir_fd)
                fields = list(info[:10]); fields[4] = self.link_uid
                return os.stat_result(fields)
            if str(path) in self.lstat_as_dir:
                # A directory at lstat time, swapped for a link before it is opened.
                fields = list(info[:10]); fields[0] = stat.S_IFDIR | 0o755
                return os.stat_result(fields)
            return info

        def fstat(fd):
            info = self.real["fstat"](fd)
            if fd in self.holders:
                fields = list(info[:10]); fields[4] = self.holder_uid; fields[0] = stat.S_IFDIR | self.holder_mode
                return os.stat_result(fields)
            return info

        def scandir(fd):
            listed = self.real["scandir"](fd)
            names = [e.name for e in listed]
            if set(names) & self.scandir_fails:
                raise PermissionError(errno.EACCES, "Permission denied")
            return self.real["scandir"](fd)

        self.p = patched(os, open=open_, close=close, lstat=lstat, fstat=fstat, scandir=scandir,
                         chown=lambda *a, **k: self.chowned.append(("chown", a, k)),
                         fchown=lambda *a, **k: self.chowned.append(("fchown", a, k)))
        self.p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.p.__exit__(*exc)

    def nothing_left_open(self) -> bool:
        """SYRD-393: an early-return refusal closes the descriptor it was walking from -- nothing is left open and
        nothing is closed twice. (SYRD-392 pinned the baseline's one leaked descriptor here.) Anything left is
        closed so the test itself leaks nothing."""
        left = sorted(self.live)
        for fd in left:
            self.real["close"](fd)
        self.live.clear()
        return left == [] and self.double_closed == []

    def leaked(self, keep: int = -1) -> list:
        """Descriptors opened here and still open (tracked live: the kernel reuses numbers), and any closed twice."""
        return sorted(self.live - {keep}) + [f"closed twice: {fd}" for fd in self.double_closed]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.role_state_ownership as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.role_state_ownership", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.role_state_ownership")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.role_state_ownership as m; "
                        "p = inspect.signature(m.repair_role_state_ownership).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "p['print_func'].default is print and p['dry_run'].default is False and m.STATE_TREE_MAX_DEPTH == 64, "
                        "not hasattr(m, 'ProjectConfig') and not hasattr(m, 'current_user_name'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.pwd is pwd and m.stat is stat and m.errno is errno and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "role_state_ownership.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name
                    or (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == name))
        if not isinstance(node, ast.FunctionDef):
            continue
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
            check(imports == [], f"{name}: reads nothing of the launcher's")
        skip = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs), *f.args.defaults, *[d for d in f.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import errno", "import os", "import pwd", "import stat", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Callable"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [getattr(n, "name", None) or ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(order == list(MOVED), f"the nine in the launcher's order: {order}")


def test_the_launcher_reexports_the_nine() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.role_state_ownership"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the nine, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED) and "restore_interrupted_role_state" in defined, "the launcher defines none of them, and keeps its caller")


# --- the root ------------------------------------------------------------------------------------------------------


def test_a_store_root_is_opened_without_following_the_tenants_links() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "a" / "b").mkdir(parents=True)
        with Host() as h:
            fd, problem = m._open_tenant_state_root(base / "a" / "b")
            check(fd >= 0 and problem == "" and os.path.samestat(os.fstat(fd), os.stat(base / "a" / "b")) and h.leaked(keep=fd) == [],
                  f"a real directory: its descriptor, every other one closed: {h.leaked(keep=fd)}")
            os.close(fd)
        with Host() as h:
            check(m._open_tenant_state_root(base / "a" / "missing" / "c") == (-1, "") and h.nothing_left_open(),
                  "a missing component: simply not there, and nothing left open")
        (base / "a" / "tenant-link").symlink_to(base / "a" / "b")
        with Host() as h:
            answer = m._open_tenant_state_root(base / "a" / "tenant-link" / "x")
            check(answer == (-1, f"tenant-link is a symlink that root did not place (it belongs to uid {ME}, in a directory owned by uid {ME}), "
                                 "and a root-run chown does not follow one") and h.nothing_left_open(),
                  f"a link the tenant could have placed is refused: {answer}")
        with Host(root_links={"tenant-link"}) as h:
            fd, problem = m._open_tenant_state_root(base / "a" / "tenant-link")
            check(fd >= 0 and problem == "" and os.path.samestat(os.fstat(fd), os.stat(base / "a" / "b")) and h.leaked(keep=fd) == [],
                  "a link root placed in a directory only root can write is followed")
            os.close(fd)
        for link_uid, holder_uid in ((0, ME), (ME, 0)):
            with Host(root_links={"tenant-link"}, link_uid=link_uid, holder_uid=holder_uid) as h:
                answer = m._open_tenant_state_root(base / "a" / "tenant-link")
            check(answer == (-1, f"tenant-link is a symlink that root did not place (it belongs to uid {link_uid}, in a directory owned by uid {holder_uid}), "
                                 "and a root-run chown does not follow one") and h.nothing_left_open(),
                  f"link uid {link_uid} in a holder of uid {holder_uid}: both must be root's: {answer}")
        with Host(root_links={"tenant-link"}, holder_mode=0o775) as h:
            answer = m._open_tenant_state_root(base / "a" / "tenant-link")
            check(answer[0] == -1 and "is a symlink that root did not place (it belongs to uid 0, in a directory owned by uid 0)" in answer[1]
                  and h.nothing_left_open(), f"root's link in a directory others can write is refused: {answer}")
        closed: list = []
        failure = RuntimeError("the link check failed")
        with Host() as h, patched(t, _root_placed_link=seam("_root_placed_link", lambda component, *, dir_fd: (_ for _ in ()).throw(failure)),
                                  _close_quietly=seam("_close_quietly", lambda fd: closed.append(fd) or m._close_quietly(fd))):
            answer = judged(m._open_tenant_state_root, base / "a" / "tenant-link")
        check(answer is failure and len(closed) == 1 and h.leaked() == [],
              f"an error while deciding about a link propagates, the open descriptor closed through the launcher's quiet close: {closed} {h.leaked()}")
        with Host() as h, patched(t, _root_placed_link=seam("_root_placed_link", lambda component, *, dir_fd: "SYRD392-REFUSED")):
            check(m._open_tenant_state_root(base / "a" / "tenant-link") == (-1, "SYRD392-REFUSED") and h.nothing_left_open(), "the link rule is the launcher's")
        (base / "locked").mkdir()
        (base / "a" / "locked-link").symlink_to(base / "locked")
        os.chmod(base / "locked", 0)
        try:
            with Host(root_links={"locked-link"}) as h:
                answer = m._open_tenant_state_root(base / "a" / "locked-link")
        finally:
            os.chmod(base / "locked", 0o700)
        check(answer == (-1, f"{base / 'a' / 'locked-link'} cannot be opened (Permission denied)") and h.nothing_left_open(),
              f"a link root placed whose target still cannot be opened: refused, and nothing left open: {answer}")
        with Host(close_fails=1) as h:
            answer = m._open_tenant_state_root(base / "a" / "b")
        check(answer == (-1, f"{Path(base.parts[0]) / base.parts[1]} cannot be opened (Input/output error)") and h.nothing_left_open(),
              f"closing a parent fails: the child is closed instead, nothing twice, nothing left: {answer} {h.double_closed}")
        with Host() as h:
            m._close_quietly(-1)
            check(h.closed == [], "a negative descriptor is not closed")
        (base / "a" / "file").write_text("")
        with Host() as h:
            check(m._open_tenant_state_root(base / "a" / "file" / "x") == (-1, "file is not a directory") and h.nothing_left_open(), "a file where a directory should be")
        (base / "shut").mkdir()
        (base / "shut" / "x").mkdir()
        os.chmod(base / "shut", 0)
        try:
            with Host() as h:
                answer = m._open_tenant_state_root(base / "shut" / "x")
        finally:
            os.chmod(base / "shut", 0o700)
        check(answer == (-1, f"{base / 'shut'} cannot be opened (Permission denied)") and h.nothing_left_open(),
              f"a component that cannot be opened: {answer}")


# --- the tree ------------------------------------------------------------------------------------------------------


def tree(base: Path) -> Path:
    root = base / "store"
    (root / "b" / "d").mkdir(parents=True)
    (root / "a").write_text("")
    (base / "elsewhere").mkdir()
    (base / "elsewhere" / "secret").write_text("")
    (root / "c").symlink_to(base / "elsewhere")
    return root


def test_the_tree_is_visited_in_order_without_following_any_link() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = tree(Path(tmp))
        seen: list = []

        def act(path, info, chown):
            seen.append((path.relative_to(root.parent), stat.S_ISLNK(info.st_mode)))
            chown(4392, 4393)
            return "wrong" if path.name == "a" else ""

        with Host() as h, patched(t, _open_tenant_state_root=seam("_open_tenant_state_root", m._open_tenant_state_root)):
            findings, refusals = m._walk_tenant_state_tree(root, act)
        check(seen == [(Path("store"), False), (Path("store/a"), False), (Path("store/b"), False), (Path("store/b/d"), False), (Path("store/c"), True)],
              f"the root, then each entry sorted, a directory before its later siblings, a link as itself and never entered: {seen}")
        check(findings == [(root / "a", "wrong")] and refusals == [], f"what act reports: {findings}")
        check(h.chowned[0] == ("fchown", (h.chowned[0][1][0], 4392, 4393), {})
              and all(c[0] == "chown" and c[1][1:] == (4392, 4393) and c[2]["follow_symlinks"] is False and "dir_fd" in c[2] for c in h.chowned[1:])
              and [c[1][0] for c in h.chowned[1:]] == ["a", "b", "d", "c"],
              f"the root through its descriptor, each entry by name relative to its directory, never following: {h.chowned}")
        check(h.leaked() == [], f"every descriptor closed: {h.leaked()}")
        with Host() as h, patched(t, STATE_TREE_MAX_DEPTH=1):
            findings, refusals = m._walk_tenant_state_tree(root, lambda *a: "")
        check(refusals == [(root / "b", "is nested deeper than 1 directories")] and h.leaked() == [], f"the depth limit is the launcher's: {refusals}")
        REACHED.add("STATE_TREE_MAX_DEPTH")
        with Host(scandir_fails={"d"}) as h:
            answer = judged(m._walk_tenant_state_tree, root, lambda *a: "")
            findings = answer[0] if isinstance(answer, tuple) else answer
        check(findings == [(root / "b", "cannot be listed (Permission denied)")] and h.leaked() == [], f"a directory that cannot be listed: {findings}")
        with Host(lstat_fails={"a"}) as h:
            answer = judged(m._walk_tenant_state_tree, root, lambda *a: "")
            findings = answer[0] if isinstance(answer, tuple) else answer
        check(findings == [(root / "a", "cannot be inspected (Permission denied)")] and h.leaked() == [], f"an entry that cannot be inspected: {findings}")
        os.chmod(root / "b", 0)
        try:
            with Host() as h:
                answer = judged(m._walk_tenant_state_tree, root, lambda *a: "")
                findings = answer[0] if isinstance(answer, tuple) else answer
        finally:
            os.chmod(root / "b", 0o700)
        check(findings == [(root / "b", "cannot be opened (Permission denied)")] and h.leaked() == [], f"a directory that cannot be opened: {findings}")
        with Host() as h:
            check(m._walk_tenant_state_tree(root.parent / "absent", refuse("act")) == ([], []) and h.nothing_left_open(),
                  "no store: nothing, and nothing left open")
        with Host() as h:
            answer = judged(m._walk_tenant_state_tree, root / "c", refuse("act"))
            check(answer == ([], [(root / "c", f"c is a symlink that root did not place (it belongs to uid {ME}, in a directory owned by uid {ME}), "
                                               "and a root-run chown does not follow one")]) and h.nothing_left_open(), f"a store that is a tenant link is refused: {answer}")
        seen_race: list = []
        with Host(lstat_as_dir={"c"}) as h:
            answer = judged(m._walk_tenant_state_tree, root, lambda path, *a: seen_race.append(path.name) or "")
            findings = answer[0] if isinstance(answer, tuple) else answer
        check(findings == [(root / "c", "cannot be opened (Not a directory)")] and "secret" not in seen_race and h.leaked() == [],
              f"a directory swapped for a link after it was inspected is not entered: {findings} {seen_race}")
        raising = RuntimeError("act failed")
        with Host() as h:
            check(judged(m._walk_tenant_state_tree, root, lambda path, *a: (_ for _ in ()).throw(raising) if path.name == "d" else "") is raising
                  and h.leaked() == [], f"an error from act propagates with every descriptor closed: {h.leaked()}")


# --- roots and findings --------------------------------------------------------------------------------------------


def config(base: Path, owner: str | None = "p392-owner", roles=("main", "audit")) -> SimpleNamespace:
    return SimpleNamespace(project="p392", run_as_user=owner, session_dir=Path("~/p392-sessions"),
                           roles=[SimpleNamespace(role=r) for r in roles])


def test_the_stores_roots() -> None:
    with tempfile.TemporaryDirectory() as tmp, patched(os, environ={**os.environ, "HOME": tmp}), \
            patched(session_paths, role_session_dir=refuse("session_paths' own role_session_dir")):
        home = Path(tmp)
        dirs = {"main": Path("~/p392-sessions/main"), "audit": Path("~/p392-sessions")}
        with patched(t, role_session_dir=seam("role_session_dir", lambda cfg, role: dirs[role.role])):
            check(m.role_state_roots(config(home)) == [home / "p392-sessions", home / "p392-sessions" / "main"],
                  "the session store first, then each role's, expanded, a repeat dropped")


def test_what_is_not_the_owners() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = base / "store"
        (store / "main").mkdir(parents=True)
        (store / "main" / "state.json").write_text("{}")
        names = {OTHER: "p392-owner", ME: "p392-me"}
        with patched(t, role_state_roots=seam("role_state_roots", lambda cfg: [store, store / "main"]),
                     role_session_dir=refuse("the roots computed past the launcher's role_state_roots"),
                     current_user_name=seam("current_user_name", lambda: "p392-owner"),
                     _uid_owner_name=seam("_uid_owner_name", lambda uid: names.get(uid, ""))), \
                patched(pwd, getpwnam=lambda user: SimpleNamespace(pw_uid=OTHER, pw_gid=OTHER) if user == "p392-owner" else (_ for _ in ()).throw(KeyError(user))), \
                Host() as h:
            found, refused = m.role_state_ownership_problems(config(base))
            check(found == [(store, "is owned by p392-me, not p392-owner"), (store / "main", "is owned by p392-me, not p392-owner"),
                            (store / "main" / "state.json", "is owned by p392-me, not p392-owner")] and refused == [],
                  f"every path that is not the owner's, each once although the second root repeats them: {found}")
            check(m.role_state_ownership_problems(config(base, owner=None))[0] == found, "no account configured: the current user")
            check(judged(m.role_state_ownership_problems, config(base, owner="p392-nobody")) == ([], []), "an owner passwd does not know: nothing to say")
            names.clear()
            check(m.role_state_ownership_problems(config(base))[0][0] == (store, f"is owned by {ME}, not p392-owner"), "an unnamed uid by number")
        (base / "linked").symlink_to(store)
        with patched(t, role_state_roots=lambda cfg: [base / "linked", store / "main"], current_user_name=lambda: "p392-owner",
                     _uid_owner_name=lambda uid: ""), \
                patched(pwd, getpwnam=lambda user: SimpleNamespace(pw_uid=ME, pw_gid=ME)), Host() as h:
            found, refused = m.role_state_ownership_problems(config(base))
            check(h.nothing_left_open(), "the findings leave nothing open when a root is refused")
        check(found == [] and refused == [(base / "linked", f"linked is a symlink that root did not place (it belongs to uid {ME}, in a directory owned by uid {ME}), "
                                                           "and a root-run chown does not follow one")],
              f"a store root that is a tenant link is refused, and the rest is still read: {refused}")
        with patched(pwd, getpwuid=lambda uid: SimpleNamespace(pw_name="p392-named") if uid == ME else (_ for _ in ()).throw(KeyError(uid))):
            check((judged(m._uid_owner_name, ME), judged(m._uid_owner_name, OTHER)) == ("p392-named", ""), "a uid's name, or nothing")


# --- the repair ----------------------------------------------------------------------------------------------------


def test_the_repair_refuses_first_is_roots_and_checks_its_work() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        store = base / "store"
        (store / "main").mkdir(parents=True)
        cfg = config(base)
        said: list = []
        answers: list = []

        def problems(c):
            return answers.pop(0)

        wrong = [(store / "main", "is owned by root, not p392-owner")]

        def repair(*, euid=0, dry_run=False, lookup=True, chown_error=None, walk=None, sequence=(), mine=False):
            said.clear(); answers[:] = list(sequence)
            chowned: list = []
            real_walk = m._walk_tenant_state_tree

            def walk_(root, act):
                if walk is not None:
                    return walk
                def recording(path, info, chown):
                    def chown_(uid, gid):
                        chowned.append((path, uid, gid))
                        if chown_error:
                            raise chown_error
                    return act(path, info, chown_)
                return real_walk(root, recording)

            with patched(t, role_state_ownership_problems=seam("role_state_ownership_problems", problems),
                         role_state_roots=lambda c: [store], _walk_tenant_state_tree=seam("_walk_tenant_state_tree", walk_),
                         current_user_name=lambda: "p392-owner"), \
                    patched(os, geteuid=lambda: euid, chown=refuse("a real chown"), fchown=refuse("a real fchown")), \
                    patched(pwd, getpwnam=(lambda user: SimpleNamespace(pw_uid=ME if mine else OTHER, pw_gid=OTHER + 1)) if lookup else (lambda user: (_ for _ in ()).throw(KeyError(user)))):
                answer = judged(m.repair_role_state_ownership, cfg, dry_run=dry_run, print_func=said.append)
            return answer, list(said), chowned

        answer, out, chowned = repair(sequence=[([], [(store / "main", "is a symlink")])])
        check(answer is False and chowned == [] and out == [
            f"switchyard: refusing to touch p392's role state: {store / 'main'} is a symlink. No ownership was changed. "
            "Make it a real directory under p392-owner's state store, then run `sudo switchyard upgrade p392` again."],
              f"a refusal first, whoever asks, and nothing changed: {out}")
        answer, out, chowned = repair(euid=1392, sequence=[([], [(store / "main", "is a symlink")])])
        check(answer is False and out[0].startswith(f"switchyard: refusing to touch p392's role state: {store / 'main'} is a symlink."),
              f"the refusal comes before the root check, for anyone: {out}")
        answer, out, chowned = repair(euid=1392, sequence=[([], [])])
        check(answer is True and out == [] and chowned == [], "nothing wrong: nothing said")
        answer, out, chowned = repair(euid=1392, sequence=[(wrong, [])])
        check(answer is False and chowned == [] and out == [
            f"switchyard: p392's role state is not all p392-owner's (1 path(s), first: {store / 'main'} is owned by root, not p392-owner), "
            "and only root can give it back. Run `sudo switchyard upgrade p392`."], f"not root: the command that can: {out}")
        answer, out, chowned = repair(dry_run=True, sequence=[(wrong, [])])
        check(answer is True and chowned == [] and out == [
            f"switchyard: would give p392-owner back 1 path(s) of p392's role state, starting with {store / 'main'} (is owned by root, not p392-owner); nothing written"],
              f"a dry run says what it would do: {out}")
        answer, out, chowned = repair(lookup=False, sequence=[(wrong, [])])
        check(answer is False and out == ["switchyard: p392-owner is not a local account; p392's role state was left alone"], f"an unknown account: {out}")
        answer, out, chowned = repair(sequence=[(wrong, []), ([], [])])
        check(answer is True and chowned == [(store, OTHER, OTHER + 1), (store / "main", OTHER, OTHER + 1)] and out == [
            "switchyard: gave p392-owner back 1 path(s) of p392's role state, so its roles can record what their runtimes were started against"],
              f"root: each path not the owner's given back through its descriptor, then rechecked: {chowned} {out}")
        answer, out, chowned = repair(sequence=[(wrong, []), ([], [])], mine=True)
        check(answer is True and chowned == [], f"a path already the owner's is not chowned: {chowned}")
        answer, out, chowned = repair(sequence=[(wrong, []), ([], [])], chown_error=PermissionError(errno.EPERM, "Operation not permitted"))
        check(answer is False and out == [f"switchyard: could not give {store} back to p392-owner: Operation not permitted"], f"a failed chown stops it: {out}")
        answer, out, chowned = repair(sequence=[(wrong, [])], walk=([], [(store, "is a symlink")]))
        check(answer is False and out == [f"switchyard: could not walk {store}: is a symlink"], f"a walk refused mid-repair: {out}")
        answer, out, chowned = repair(sequence=[(wrong, []), ([], [(store, "cannot be opened")])])
        check(answer is False and out == [f"switchyard: p392's role state could not be re-read after the repair: {store} cannot be opened"], f"the recheck refused: {out}")
        answer, out, chowned = repair(sequence=[(wrong, []), (wrong + wrong, [])])
        check(answer is False and out == [f"switchyard: p392's role state still has 2 path(s) that are not p392-owner's, starting with {store / 'main'}"],
              f"the recheck still wrong: {out}")
        with patched(t, current_user_name=seam("current_user_name", lambda: "p392-current"), role_state_ownership_problems=lambda c: ([], [(store, "x")])):
            said.clear()
            m.repair_role_state_ownership(config(base, owner=None), print_func=said.append)
        check("under p392-current's state store" in said[0], "no account configured: the current user")
        with patched(t, role_state_roots=seam("role_state_roots", lambda c: [store]), role_state_ownership_problems=lambda c: answers.pop(0),
                     _walk_tenant_state_tree=lambda root, act: ([], [])), patched(os, geteuid=lambda: 0), \
                patched(pwd, getpwnam=lambda user: SimpleNamespace(pw_uid=OTHER, pw_gid=OTHER)):
            answers[:] = [(wrong, []), ([], [])]
            check(m.repair_role_state_ownership(cfg, print_func=lambda line: None) is True, "the roots walked are the launcher's")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_nine")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"role_state_ownership_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
