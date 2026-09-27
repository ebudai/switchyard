#!/usr/bin/env python3
"""SYRD-332: owner state directories, against the launcher they came out of.

Giving a project's owner the session and pane-state directories it writes
moved into `scripts/owner_state_dirs.py` unchanged. This pins what makes that
safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every moved name is still the launcher's, the very same object whichever
  module is imported first, and `pwd` is the very module the launcher holds --
  so the suites' `team_launcher.pwd.getpwnam = ...` reaches it. The launcher
  calls `ensure_owner_state_dirs` by its own name at its three sites.
- **Seams (rule 24).** The current user, the owner's runtime directory, the
  failure reason and the calls between the four functions are read through the
  launcher when they run; nothing is read past it.
- **The boundary is unchanged:** `install -d -m 700 -o <owner> -g <owner>`,
  through the caller's runner, for the session directory and then the pane-state
  directory, each only when it lies inside the owner's home or runtime
  directory; nothing when there is no owner or the caller already is it; a
  failed install stops the launch with the path, the owner and the reason.

The passwd lookup is this test's own fake (no real account or home is looked
up), the runner records, and every path is under a temporary root that is never
written.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = ("install_owner_state_dir_args", "_owner_state_roots", "_is_owner_state_path", "ensure_owner_state_dirs")
#: Measured on the baseline: the launcher's own call sites (all in launch_project).
#: SYRD-340 moved one of them, in launch_project's P5, to launch_phases, which
#: calls it through the launcher; the total is unchanged.
LAUNCHER_CALLS = {"ensure_owner_state_dirs": 3}
#: Measured on the baseline: the moved code's calls, each through the launcher.
MOVED_CALLS = {"current_user_name": 1, "runtime_dir_for_uid": 1, "_proc_failure_reason": 1,
               "_owner_state_roots": 1, "_is_owner_state_path": 1, "install_owner_state_dir_args": 1}
OWNER, UID = "syrd332-owner", 43332


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind attributes of one module for one block, as the suites do."""

    def __init__(self, module: object, **values: object) -> None:
        self.module = module
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.module, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.module, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.module, name, value)


class Accounts:
    """A passwd with one account, whose home and runtime directory are under a temporary root."""

    def __init__(self, root: Path, *, current: str = "syrd332-launcher") -> None:
        self.home = root / "home" / OWNER
        self.runtime = root / "run" / "user" / str(UID)
        self.current = current
        self.asked: list[object] = []

    def getpwnam(self, name: str) -> SimpleNamespace:
        self.asked.append(("passwd", name))
        if name != OWNER:
            raise KeyError(name)
        return SimpleNamespace(pw_name=OWNER, pw_uid=UID, pw_dir=str(self.home))

    def launcher_names(self) -> dict[str, object]:
        return dict(current_user_name=lambda: self.asked.append("current") or self.current,
                    runtime_dir_for_uid=lambda uid: self.asked.append(("runtime", uid)) or self.runtime,
                    _proc_failure_reason=lambda result, default: f"reason({result.returncode}; {default})")


def accounts(root: Path, **kwargs: object):
    """Install the fake passwd the way the suites do -- through the launcher's `pwd` -- and its seams."""
    from scripts import team_launcher

    fake = Accounts(root, **kwargs)

    class Installed:
        def __enter__(self) -> Accounts:
            self.saved = team_launcher.pwd.getpwnam
            team_launcher.pwd.getpwnam = fake.getpwnam
            self.seams = patched(team_launcher, **fake.launcher_names())
            self.seams.__enter__()
            return fake

        def __exit__(self, *exc: object) -> None:
            self.seams.__exit__(*exc)
            team_launcher.pwd.getpwnam = self.saved
    return Installed()


def config(root: Path, owner: str | None = OWNER, session: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(project="p332", run_as_user=owner,
                           session_dir=session if session is not None else root / "home" / OWNER / "sessions")


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.owner_state_dirs as m; "
        "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module, the launcher least of all: "
                                         f"{result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.owner_state_dirs", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.owner_state_dirs")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import pwd, scripts.team_launcher as t, scripts.owner_state_dirs as o; "
            f"print(all(getattr(t, n) is getattr(o, n) for n in {EXPORTED!r}), o.pwd is t.pwd is pwd)"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too, and so is pwd: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_calls_the_seams_and_the_functions_own_names() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        phases = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
        phase_calls = [n for n in ast.walk(phases)
                       if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) + len(phase_calls) == count and all(isinstance(n.func, ast.Name) for n in calls)
              and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                      and n.func.value.id == "launcher" for n in phase_calls),
              f"{name} is called at its {count} baseline sites: by the launcher's own name there, "
              "through the launcher from launch_phases")
    moved = ast.parse((ROOT / "scripts" / "owner_state_dirs.py").read_text(encoding="utf-8"))
    top = [n for n in moved.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("team_launcher" in ast.dump(n) for n in top),
          "the launcher is never imported at the module's top, only when a function runs")
    for name, count in MOVED_CALLS.items():
        calls = [n for n in ast.walk(moved)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == count and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                                          and n.func.value.id == "launcher" for n in calls),
              f"the moved code calls {name} at its {count} baseline site, through the launcher")
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in MOVED_CALLS})
    check(bare == [], f"no launcher name is read past it: {bare}")
    lookups = [n for n in ast.walk(moved) if isinstance(n, ast.Attribute) and n.attr == "getpwnam"]
    check(len(lookups) == 1 and isinstance(lookups[0].value, ast.Name) and lookups[0].value.id == "pwd",
          "the account is looked up in pwd, the one module the launcher holds")
    for function in (n for n in moved.body if isinstance(n, ast.FunctionDef)):
        bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                          and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
        check(through == [], f"{function.name}: nothing it binds itself is read as the launcher's: {through}")


def test_the_install_argv() -> None:
    from scripts import owner_state_dirs

    path = Path("/nonexistent/syrd332/state dir")
    check(owner_state_dirs.install_owner_state_dir_args(SimpleNamespace(run_as_user=OWNER), path)
          == ["install", "-d", "-m", "700", "-o", OWNER, "-g", OWNER, "/nonexistent/syrd332/state dir"],
          "the directory is created mode 700, owned by the owner and its group, the path one argument")
    for owner in (None, ""):
        try:
            owner_state_dirs.install_owner_state_dir_args(SimpleNamespace(run_as_user=owner), path)
            refused = ""
        except ValueError as exc:
            refused = str(exc)
        check(refused == "state directory ownership setup requires run_as_user",
              f"with no owner ({owner!r}) there is no argv to build: {refused!r}")


def test_the_owners_roots() -> None:
    from scripts import owner_state_dirs

    with tempfile.TemporaryDirectory(prefix="syrd332.") as raw:
        root = Path(raw)
        with accounts(root) as fake:
            none = owner_state_dirs._owner_state_roots(config(root, owner=None))
            check(none == () and fake.asked == [], f"no owner: no roots, and no account is looked up: {fake.asked}")
            unknown = owner_state_dirs._owner_state_roots(config(root, owner="syrd332-nobody"))
            check(unknown == () and fake.asked == [("passwd", "syrd332-nobody")],
                  f"an account passwd does not know: no roots, and no runtime directory asked: {fake.asked}")
            fake.asked.clear()
            roots = owner_state_dirs._owner_state_roots(config(root))
        check(roots == (fake.home.resolve(strict=False), fake.runtime.resolve(strict=False))
              and fake.asked == [("passwd", OWNER), ("runtime", UID)],
              f"the owner's home from passwd, then its runtime directory for its uid, resolved: {roots} {fake.asked}")


def test_what_counts_as_inside_the_owners_state() -> None:
    from scripts import owner_state_dirs

    with tempfile.TemporaryDirectory(prefix="syrd332.") as raw:
        root = Path(raw)
        with accounts(root) as fake:
            inside = lambda path: owner_state_dirs._is_owner_state_path(config(root), path)  # noqa: E731
            cases = {
                "the home itself": (fake.home, True),
                "under the home": (fake.home / "state" / "sessions", True),
                "under the runtime directory": (fake.runtime / "panes", True),
                "a sibling sharing the prefix": (fake.home.with_name(OWNER + "2") / "sessions", False),
                "escaping the home with ..": (fake.home / ".." / "someone-else" / "sessions", False),
                "the home's parent": (fake.home.parent, False),
                "outside both": (root / "elsewhere", False),
            }
            for what, (path, expected) in cases.items():
                check(inside(path) is expected, f"{what}: {path} -> {not expected}")
            check(owner_state_dirs._is_owner_state_path(config(root, owner=None), fake.home) is False,
                  "with no owner, nothing is the owner's")


def run(root: Path, cfg: SimpleNamespace, pane: Path, *, current: str = "syrd332-launcher", code: int = 0):
    from scripts import owner_state_dirs

    ran: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        check(kwargs == {}, f"the runner is handed the argv alone: {kwargs}")
        ran.append(list(argv))
        return subprocess.CompletedProcess(argv, code, "", "")

    with accounts(root, current=current) as fake:
        try:
            owner_state_dirs.ensure_owner_state_dirs(cfg, pane_state_dir=pane, runner=runner)
            stopped = None
        except SystemExit as exc:
            stopped = str(exc)
    return ran, stopped, fake


def test_nothing_is_done_without_a_different_owner() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd332.") as raw:
        root = Path(raw)
        pane = root / "home" / OWNER / "panes"
        ran, stopped, fake = run(root, config(root, owner=None), pane)
        check(ran == [] and stopped is None and fake.asked == [],
              f"no owner: nothing runs, and the current user is not even asked: {fake.asked}")
        ran, stopped, fake = run(root, config(root), pane, current=OWNER)
        check(ran == [] and stopped is None and fake.asked == ["current"],
              f"the caller already is the owner: nothing runs and no account is looked up: {fake.asked}")


def test_only_the_owners_directories_are_given_in_order() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd332.") as raw:
        root = Path(raw)
        home = root / "home" / OWNER
        ran, stopped, _ = run(root, config(root, session=home / "sessions"), root / "run" / "user" / str(UID) / "p")
        check(stopped is None and ran == [
            ["install", "-d", "-m", "700", "-o", OWNER, "-g", OWNER, str(home / "sessions")],
            ["install", "-d", "-m", "700", "-o", OWNER, "-g", OWNER, str(root / "run" / "user" / str(UID) / "p")]],
              f"the session directory, then the pane-state directory, each through the caller's runner: {ran}")
        ran, stopped, _ = run(root, config(root, session=root / "shared" / "sessions"), home / "panes")
        check(ran == [["install", "-d", "-m", "700", "-o", OWNER, "-g", OWNER, str(home / "panes")]],
              f"a directory outside the owner's state is never handed to it: {ran}")
        ran, stopped, _ = run(root, config(root, session=root / "shared"), root / "shared" / "panes")
        check(ran == [] and stopped is None, f"and with neither inside, nothing runs: {ran}")


def test_the_launch_stops_on_a_failed_install() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd332.") as raw:
        root = Path(raw)
        home = root / "home" / OWNER
        ran, stopped, _ = run(root, config(root, session=home / "sessions"), home / "panes", code=5)
    check(len(ran) == 1 and stopped == (
        f"team-launcher: failed to assign state directory {home / 'sessions'} to {OWNER}: "
        "reason(5; install failed with exit 5)"),
          f"the first failure stops it, naming the path, the owner and the launcher's reason: {ran} {stopped!r}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_calls_the_seams_and_the_functions_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"owner_state_dirs_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
