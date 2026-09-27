#!/usr/bin/env python3
"""SYRD-383: root's provisioning records and their directory, against the launcher they came out of.

Nine definitions -- the provision root, root's baseline plan and workflow
record paths, the recorded workflow reader, the three modes, the artifact mode
selector and the directory repair -- moved unchanged into
`scripts/privileged_provision_records.py`, and the launcher re-exports them.
The override's variable, the default root, the provision-directory builder and
the no-follow readers stay elsewhere. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the modes and the default are the
  baseline's literals.
- **Seams (rule 24):** every launcher facility, sibling and mode read when a
  body runs is read through the launcher as often as before, so a patch on the
  launcher reaches each of them, which this test shows for all of them. The two
  `project_provision` names are still imported inside the functions that use
  them.
- **The behaviour is unchanged:** the stripped, user-expanded override and the
  default object; the path names; a missing record told from an unusable one
  before the no-follow read; the modes; the directory's traversal, creation
  modes, refusals, repairs and best-effort chown.

Everything real lives in an owned temporary directory. `os.chown` is always a
recorder here, never the host call; `Path.mkdir` and `Path.chmod` are wrapped
to refuse anything outside the owned directory; whose files count as root's is
a stand-in. No ownership, permission outside owned temp, or account lookup is
performed.
"""

from __future__ import annotations

import ast
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

from scripts import privileged_provision_records as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

CHECKS = 0
MOVED = ("switchyard_privileged_provision_root", "privileged_baseline_plan_path", "workflow_record_path",
         "recorded_declared_workflow", "PRIVILEGED_PROVISION_DIR_MODE", "PRIVILEGED_ARTIFACT_MODE",
         "PRIVILEGED_EXECUTABLE_ARTIFACT_MODE", "privileged_artifact_mode", "ensure_privileged_provision_dir")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'switchyard_privileged_provision_root': {'DEFAULT_PRIVILEGED_PROVISION_ROOT': 1, 'PRIVILEGED_PROVISION_ROOT_ENV': 1},
    'privileged_baseline_plan_path': {'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
    'workflow_record_path': {'privileged_baseline_plan_path': 1},
    'recorded_declared_workflow': {'read_plan_no_follow': 1, 'workflow_record_path': 1},
    'privileged_artifact_mode': {'PRIVILEGED_ARTIFACT_MODE': 1, 'PRIVILEGED_EXECUTABLE_ARTIFACT_MODE': 1},
    'ensure_privileged_provision_dir': {'PRIVILEGED_PROVISION_DIR_MODE': 2, 'expected_privileged_uid': 1, 'switchyard_privileged_provision_root': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
ME = os.getuid()
OTHER = ME + 1
PROJECT = "p383"


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


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "privileged_provision_records.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.privileged_provision_records as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_literals() -> None:
    for order in (("scripts.privileged_provision_records", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.privileged_provision_records")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.privileged_provision_records as m; "
                        "p = inspect.signature(m.ensure_privileged_provision_dir).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "(m.PRIVILEGED_PROVISION_DIR_MODE, m.PRIVILEGED_ARTIFACT_MODE, m.PRIVILEGED_EXECUTABLE_ARTIFACT_MODE) == (0o700, 0o600, 0o700) "
                        "and p['root'].default is None and p['root'].kind is inspect.Parameter.KEYWORD_ONLY, "
                        "not hasattr(m, 'PRIVILEGED_PROVISION_ROOT_ENV') and not hasattr(m, 'DEFAULT_PRIVILEGED_PROVISION_ROOT'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.stat is stat and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    local_imports = {"workflow_record_path": ["from scripts.ticket_board.project_provision import WORKFLOW_RECORD_NAME"],
                     "recorded_declared_workflow": ["from scripts.ticket_board.project_provision import workflow_record_document"]}
    for name in MOVED:
        if name.isupper():
            tree = ast.parse((ROOT / "scripts" / "privileged_provision_records.py").read_text(encoding="utf-8"))
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
        check(imports == ["from scripts import team_launcher as launcher", *local_imports.get(name, [])]
              and ast.unparse(node.body[first]) == imports[0],
              f"{name}: the launcher imported once, first thing when it runs; its local import kept: {imports}")
        if name in local_imports:
            check(ast.unparse(node.body[first + 1]) == local_imports[name][0], f"{name}: the local import right after it")
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
        check(not bound & set(through), f"{name}: nothing it binds itself, its local imports included, is read through the launcher")
    tree = ast.parse((ROOT / "scripts" / "privileged_provision_records.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import os", "import stat", "from pathlib import Path"],
          f"only the standard library is imported at the top: {top}")
    names = [n.name if isinstance(n, ast.FunctionDef) else ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(names == list(MOVED), f"the nine in the launcher's order: {names}")


def test_the_launcher_reexports_the_nine_and_keeps_its_facilities() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.privileged_provision_records"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the nine, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED) and "PRIVILEGED_PROVISION_ROOT_ENV" in defined and "render_privileged_artifacts" in defined,
          "the launcher defines none of the nine, and still defines the override's variable and the renderer between them")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_provision_root() -> None:
    default = Path("/fixture/default-provision-root")
    with tempfile.TemporaryDirectory() as home, environ("HOME", home), \
            patched(t, DEFAULT_PRIVILEGED_PROVISION_ROOT=default):
        with environ(t.PRIVILEGED_PROVISION_ROOT_ENV, None):
            check(m.switchyard_privileged_provision_root() is default, "unset: the launcher's default, the same object")
        with environ(t.PRIVILEGED_PROVISION_ROOT_ENV, "  "):
            check(m.switchyard_privileged_provision_root() is default, "a blank override is no override")
        REACHED.add("DEFAULT_PRIVILEGED_PROVISION_ROOT")
        with environ(t.PRIVILEGED_PROVISION_ROOT_ENV, " ~/p383-root "):
            check(m.switchyard_privileged_provision_root() == Path(home) / "p383-root", "stripped, then the user's home expanded")
        with environ(t.PRIVILEGED_PROVISION_ROOT_ENV, "/srv/p383"):
            check(m.switchyard_privileged_provision_root() == Path("/srv/p383"), "an absolute override as given")
        with patched(t, PRIVILEGED_PROVISION_ROOT_ENV="SYRD383_ROOT"), environ("SYRD383_ROOT", "/srv/syrd383"):
            check(m.switchyard_privileged_provision_root() == Path("/srv/syrd383"), "the launcher's variable decides")
            REACHED.add("PRIVILEGED_PROVISION_ROOT_ENV")
    check(t.DEFAULT_PRIVILEGED_PROVISION_ROOT is project_provision.DEFAULT_PRIVILEGED_PROVISION_ROOT,
          "the launcher's default is project_provision's own object")


def test_the_baseline_and_workflow_paths() -> None:
    asked: list = []
    root = Path("/fixture/provision")
    with patched(t, switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: root),
                 privileged_provision_dir=seam("privileged_provision_dir",
                                               lambda project, *, root: asked.append((project, root)) or root / "tenants" / project)):
        check(m.privileged_baseline_plan_path(PROJECT) == root / "tenants" / PROJECT / "plan.json" and asked == [(PROJECT, root)],
              f"root's plan in the project's provision directory under the provision root: {asked}")
    plan = Path("/fixture/etc/p383/plan.json")
    with patched(t, privileged_baseline_plan_path=seam("privileged_baseline_plan_path", lambda project: plan if project == PROJECT else None)):
        check(m.workflow_record_path(PROJECT) == plan.with_name(project_provision.WORKFLOW_RECORD_NAME),
              "the workflow record beside it, by project_provision's name")
        with patched(project_provision, WORKFLOW_RECORD_NAME="syrd383-workflow.json"):
            check(m.workflow_record_path(PROJECT) == plan.with_name("syrd383-workflow.json"), "the name is read from project_provision when it runs")


def test_the_recorded_workflow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        record = Path(tmp) / "etc" / PROJECT / "workflow.json"
        record.parent.mkdir(parents=True)
        asked: list = []
        handed: list = []

        def reading(path, *, require_root_owned):
            asked.append((path, require_root_owned))
            return answer

        def read(read_plan=None):
            with patched(t, workflow_record_path=seam("workflow_record_path", lambda project: record if project == PROJECT else None),
                         read_plan_no_follow=seam("read_plan_no_follow", read_plan or reading)), \
                    patched(project_provision, workflow_record_document=lambda data, *, project: handed.append((data, project)) or ("doc", "")):
                return judged(m.recorded_declared_workflow, PROJECT)

        answer = (None, "unexpected")
        check(read() == (None, f"root holds no recorded workflow for {PROJECT}") and asked == [], "missing: absent, and the reader is not asked")
        record.write_text("{}")
        os.chmod(record.parent, 0)
        try:
            try:
                record.lstat()
                expected = None
            except OSError as exc:
                expected = exc
            got = read()
        finally:
            os.chmod(record.parent, 0o700)
        check(isinstance(expected, PermissionError) and got == (None, f"root's workflow record for {PROJECT} could not be read: {expected}") and asked == [],
              f"unusable is a different answer, with its reason, and the reader is not asked: {got}")
        answer = (None, "why not")
        check(read() == (None, "why not") and asked == [(record, True)] and handed == [],
              f"an unsafe record: the no-follow reader's refusal, asked for root's own: {asked}")
        answer = (SimpleNamespace(data={"workflow": 1}), "")
        check(read() == ("doc", "") and handed == [({"workflow": 1}, PROJECT)], f"a readable one is validated as the document: {handed}")
        record.unlink()
        record.symlink_to(record.parent / "elsewhere.json")
        (record.parent / "elsewhere.json").write_text("{}")
        with environ(t.PRIVILEGED_PROVISION_ROOT_ENV, tmp):
            got = read(read_plan=t.read_plan_no_follow)
        check(got == (None, f"{record} is a symlink, so it is not a plan this will read or write"),
              f"the real reader refuses a symlinked record the lstat let through: {got}")
        record.unlink()
        record.write_text('{"not": "a workflow"}')
        os.chmod(record, 0o600)
        with environ(t.PRIVILEGED_PROVISION_ROOT_ENV, tmp), patched(t, workflow_record_path=lambda project: record):
            real = m.recorded_declared_workflow(PROJECT)
        check(real == project_provision.workflow_record_document({"not": "a workflow"}, project=PROJECT),
              f"end to end, project_provision's own verdict on the document: {real}")


def test_the_modes() -> None:
    check((m.PRIVILEGED_PROVISION_DIR_MODE, m.PRIVILEGED_ARTIFACT_MODE, m.PRIVILEGED_EXECUTABLE_ARTIFACT_MODE) == (0o700, 0o600, 0o700),
          "root only: the directory and executables 0700, everything else 0600")
    for name, mode in (("operator-commands.sh", 0o700), ("migrate.sh", 0o700), ("plan.json", 0o600), ("p383-workflow.sql", 0o600),
                       ("run.sh.bak", 0o600), ("sh", 0o600)):
        check(m.privileged_artifact_mode(name) == mode, f"{name}: {m.privileged_artifact_mode(name):o}")
    with patched(t, PRIVILEGED_ARTIFACT_MODE=0o640, PRIVILEGED_EXECUTABLE_ARTIFACT_MODE=0o750):
        check((m.privileged_artifact_mode("plan.json"), m.privileged_artifact_mode("a.sh")) == (0o640, 0o750), "both modes are the launcher's")
        REACHED.update({"PRIVILEGED_ARTIFACT_MODE", "PRIVILEGED_EXECUTABLE_ARTIFACT_MODE"})


class Host:
    """The directory repair's effects, recorded and confined to one owned directory."""

    def __init__(self, owned: Path, *, chown_error: BaseException | None = None) -> None:
        self.owned, self.chown_error = owned, chown_error
        self.chowned: list = []
        self.made: list = []
        self.chmodded: list = []

    def inside(self, path: Path) -> None:
        assert Path(path).is_relative_to(self.owned), f"refused outside the owned directory: {path}"

    def __enter__(self) -> "Host":
        real_mkdir, real_chmod = Path.mkdir, Path.chmod

        def mkdir(path, mode=0o777, parents=False, exist_ok=False):
            self.inside(path); self.made.append((path, mode))
            return real_mkdir(path, mode, parents, exist_ok)

        def chmod(path, mode, *, follow_symlinks=True):
            self.inside(path); self.chmodded.append((path, mode))
            return real_chmod(path, mode, follow_symlinks=follow_symlinks)

        def chown(path, uid, gid, **kwargs):
            self.chowned.append((Path(path), uid, gid))
            if self.chown_error is not None:
                raise self.chown_error

        self.p = [patched(Path, mkdir=mkdir, chmod=chmod), patched(os, chown=chown)]
        for p in self.p:
            p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self.p):
            p.__exit__(*exc)


def ensure(owned: Path, target: Path, *, owner: int = ME, derived: Path | None = None, **kw):
    """ensure_privileged_provision_dir with every effect recorded; returns (answer, host)."""
    with Host(owned, **{k: kw.pop(k) for k in ("chown_error",) if k in kw}) as host, \
            patched(t, expected_privileged_uid=seam("expected_privileged_uid", lambda: owner),
                    switchyard_privileged_provision_root=(seam("switchyard_privileged_provision_root", lambda: derived)
                                                          if derived is not None else refuse("the provision root"))):
        return judged(m.ensure_privileged_provision_dir, target, **kw), host


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_the_directory_is_created_root_only_under_its_base() -> None:
    saved = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            base = owned / "etc"
            target = base / "switchyard" / PROJECT
            (owned / "neighbour").mkdir()
            answer, host = ensure(owned, target, root=base)
            check(answer == [], f"a fresh directory repairs nothing: {answer}")
            check(host.made == [(base, 0o755), (base / "switchyard", 0o755), (target, 0o700)],
                  f"outermost first, each ancestor under the base 0755 and the target root-only: {host.made}")
            check(host.chmodded == [] and (mode_of(base), mode_of(base / "switchyard"), mode_of(target)) == (0o755, 0o755, 0o700),
                  "created with the modes they need, so nothing is changed afterwards")
            check(host.chowned == [(base, 0, 0), (base / "switchyard", 0, 0), (target, 0, 0)],
                  f"each given to root, and nothing above the base is touched: {host.chowned}")
            check(sorted(p.name for p in owned.iterdir()) == ["etc", "neighbour"] and list((owned / "neighbour").iterdir()) == [],
                  "nothing outside the selected path")
            answer, host = ensure(owned, target, root=base)
            check(answer == [] and host.made == [] and host.chmodded == [] and len(host.chowned) == 3, "a second run changes nothing")
            other = owned / "elsewhere" / PROJECT
            other.parent.mkdir()
            answer, host = ensure(owned, other, root=base)
            check(answer == [] and host.made == [(other, 0o700)] and host.chowned == [(other, 0, 0)],
                  f"a target outside the base: only the target itself is walked, none of its ancestors: {host.made} {host.chowned}")
    finally:
        os.umask(saved)


def test_the_base_decides_which_ancestors_are_walked() -> None:
    saved = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            base = owned / "etc"
            target = base / "switchyard" / PROJECT
            answer, host = ensure(owned, target, derived=base)
            check(answer == [] and [c[0] for c in host.chowned] == [base, base / "switchyard", target],
                  f"with no explicit root, the launcher's provision root is the base: {host.chowned}")
            answer, host = ensure(owned, target, root=base / "switchyard")
            check([c[0] for c in host.chowned] == [base / "switchyard", target],
                  f"an explicit root wins over the provision root, which is not asked: {host.chowned}")
    finally:
        os.umask(saved)


def test_refusals_are_loud_and_write_nothing_more() -> None:
    saved = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            base = owned / "etc"
            target = base / "switchyard" / PROJECT
            base.mkdir()
            (owned / "real").mkdir()
            (base / "switchyard").symlink_to(owned / "real")
            answer, host = ensure(owned, target, root=base)
            check(isinstance(answer, SystemExit) and str(answer) == (
                f"switchyard: {base / 'switchyard'} is a symlink, so it is not a directory root will "
                f"publish {PROJECT}'s provisioning artifacts into. Nothing was written."),
                f"a symlinked ancestor: {answer!r}")
            check(host.chowned == [(base, 0, 0)] and host.made == [] and list((owned / "real").iterdir()) == [],
                  f"nothing past the refusal, and nothing through the link: {host.chowned} {host.made}")
            (base / "switchyard").unlink()
            (base / "switchyard").write_text("")
            answer, host = ensure(owned, target, root=base)
            check(isinstance(answer, SystemExit) and str(answer) == (
                f"switchyard: {base / 'switchyard'} is not a directory, so root's provisioning artifacts "
                "have nowhere to go. Nothing was written."), f"a file where a directory should be: {answer!r}")
            (base / "switchyard").unlink()
            answer, host = ensure(owned, target, root=base, owner=OTHER)
            check(isinstance(answer, SystemExit) and str(answer) == (
                f"switchyard: {base} is owned by uid {ME} rather than by root, so "
                "what root publishes there would be its owner's to read and replace. Nothing "
                "was written."), f"a directory that is not root's: {answer!r}")
            check(host.chowned == [] and host.chmodded == [] and not (base / "switchyard").exists(),
                  "refused at the first directory: nothing chowned, chmodded or created below it")
            check(host.chowned == [] and ("expected_privileged_uid" in REACHED), "whose files count as root's is the launcher's answer")
    finally:
        os.umask(saved)


def test_modes_are_repaired_and_only_the_targets_is_reported() -> None:
    saved = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            base = owned / "etc"
            target = base / "switchyard" / PROJECT
            target.mkdir(parents=True)
            os.chmod(base / "switchyard", 0o775)
            os.chmod(target, 0o750)
            answer, host = ensure(owned, target, root=base)
            check(answer == [f"closed {target} to root only (was mode 0750)"],
                  f"the target's closing is reported, with the mode it had: {answer}")
            check(host.chmodded == [(base / "switchyard", 0o755), (target, 0o700)]
                  and (mode_of(base), mode_of(base / "switchyard"), mode_of(target)) == (0o755, 0o755, 0o700),
                  f"an ancestor is put back to 0755 without a report, the target to 0700: {host.chmodded}")
            os.chmod(target, 0o755)
            with patched(t, PRIVILEGED_PROVISION_DIR_MODE=0o750):
                answer, host = ensure(owned, target, root=base)
                check(answer == [f"closed {target} to root only (was mode 0755)"] and host.chmodded == [(target, 0o750)],
                      f"the private mode is the launcher's: {host.chmodded}")
                fresh = base / "switchyard" / "p383b"
                answer, host = ensure(owned, fresh, root=base)
                check(host.made == [(fresh, 0o750)], f"so is the mode a new target is made with: {host.made}")
                REACHED.add("PRIVILEGED_PROVISION_DIR_MODE")
    finally:
        os.umask(saved)


def test_chown_is_attempted_not_relied_on() -> None:
    saved = os.umask(0o022)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            owned = Path(tmp)
            base = owned / "etc"
            target = base / PROJECT
            answer, host = ensure(owned, target, root=base, chown_error=PermissionError(1, "Operation not permitted"))
            check(answer == [] and host.chowned == [(base, 0, 0), (target, 0, 0)] and mode_of(target) == 0o700,
                  f"a refused chown is tolerated for every directory, and the walk goes on: {host.chowned}")
            answer, host = ensure(owned, target, root=base, chown_error=ValueError("not an OSError"))
            check(isinstance(answer, ValueError) and host.chowned == [(base, 0, 0)], f"only an OSError is tolerated: {answer!r}")
    finally:
        os.umask(saved)


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_the_literals",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_nine_and_keeps_its_facilities")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"privileged_provision_records_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
