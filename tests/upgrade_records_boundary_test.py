#!/usr/bin/env python3
"""SYRD-376: the upgrade journal, its phases and root's pinned-source record, against the launcher they came out of.

Twenty-four top-level definitions -- the phase table and its owners,
`RoleAccountCutover`, root's journal and the tenant's projection of it, the
unprivileged observation, root's record of the pinned release and the phase
reports -- moved unchanged into `scripts/upgrade_records.py`, and the launcher
re-exports them. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's.
- **Definition-time bindings are the same objects:** the `dataclass` decorator,
  and `UPGRADE_PHASE_OWNERS` built from `UPGRADE_PHASES`.
- **Seams (rule 24):** every launcher name these bodies read, and every name
  defined here that another definition here reads when it runs, is read through
  the launcher as often as before -- a patch on the launcher reaches each of
  them, which this test shows for all of them.
- **The trust boundaries are unchanged:** root's record versus the tenant's
  observation, the pinned source's refusals and read-back, the staged no-follow
  writers and their order, and the projection that republishes root's journal
  and drops answered observations.

Every boundary is this test's own: files live in owned temporary directories,
and the effective uid, the ownership calls, the privileged directory, the
publisher, the caller's account and the workflow lookups are stand-ins. No
real account, ownership or privilege changes.
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import upgrade_records as m  # noqa: E402

CHECKS = 0
MOVED = ("UPGRADE_JOURNAL_SCHEMA", "UPGRADE_PHASES", "UPGRADE_PHASE_OWNERS", "RoleAccountCutover", "upgrade_journal_path",
         "UPGRADE_JOURNAL_OBSERVATIONS", "_read_journal_file", "read_upgrade_journal", "UPGRADE_SOURCE_SCHEMA",
         "privileged_upgrade_source_path", "_write_privileged_json", "record_upgrade_source", "read_upgrade_source",
         "upgrade_source_unavailable_reason", "resolve_pinned_upgrade_source", "record_upgrade_phase",
         "publish_tenant_journal_projection", "_record_upgrade_observation", "upgrade_phase_observation", "upgrade_phase_state",
         "director_phase_required", "record_release_phase_from_status", "upgrade_phase_report", "outstanding_release_phase_report")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    '_read_journal_file': {'UPGRADE_JOURNAL_OBSERVATIONS': 1, 'UPGRADE_JOURNAL_SCHEMA': 2},
    'read_upgrade_journal': {'_read_journal_file': 2, 'privileged_upgrade_journal_path': 1, 'upgrade_journal_path': 1},
    'privileged_upgrade_source_path': {'privileged_provision_dir': 1, 'switchyard_privileged_provision_root': 1},
    '_write_privileged_json': {'ensure_privileged_provision_dir': 1, 'privileged_artifact_mode': 1},
    'record_upgrade_source': {'UPGRADE_SOURCE_SCHEMA': 1, '_write_privileged_json': 1, 'privileged_upgrade_source_path': 1, 'read_upgrade_source': 1, 'resolved_source_selection': 1},
    'read_upgrade_source': {'UPGRADE_SOURCE_SCHEMA': 1, 'privileged_upgrade_source_path': 1},
    'upgrade_source_unavailable_reason': {'current_user_name': 1, 'privileged_upgrade_source_path': 1},
    'resolve_pinned_upgrade_source': {'DEFAULT_TENANT_RELEASE_DEPLOY_REF': 2, 'read_upgrade_source': 1},
    'record_upgrade_phase': {'UPGRADE_PHASE_OWNERS': 1, '_record_upgrade_observation': 1, 'ensure_privileged_provision_dir': 1, 'privileged_artifact_mode': 1, 'privileged_upgrade_journal_path': 1, 'publish_tenant_journal_projection': 1, 'read_upgrade_journal': 1},
    'publish_tenant_journal_projection': {'UPGRADE_JOURNAL_OBSERVATIONS': 2, 'UPGRADE_JOURNAL_SCHEMA': 1, 'publish_tenant_artifact': 1, 'read_upgrade_journal': 1, 'upgrade_journal_path': 1},
    '_record_upgrade_observation': {'UPGRADE_JOURNAL_OBSERVATIONS': 1, 'current_user_name': 1, 'read_upgrade_journal': 1, 'upgrade_journal_path': 1},
    'upgrade_phase_observation': {'UPGRADE_JOURNAL_OBSERVATIONS': 1},
    'director_phase_required': {'declared_workflow_presence': 1},
    'record_release_phase_from_status': {'_format_release_sha': 1, 'record_upgrade_phase': 1},
    'upgrade_phase_report': {'UPGRADE_PHASES': 1, 'declared_workflow_presence': 1, 'director_phase_required': 1, 'upgrade_phase_state': 1},
    'outstanding_release_phase_report': {'upgrade_phase_state': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
CONFIG = SimpleNamespace(project="p376")


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


def euid(value: int) -> patched:
    return patched(os, geteuid=lambda: value)


class Owner:
    """`os.fchown`/`os.chown` stand-ins: record the attempt and fail, as they would for anyone but root."""

    def __init__(self, log: list) -> None:
        self.log = log

    def fchown(self, fd: int, uid: int, gid: int) -> None:
        self.log.append(("fchown", uid, gid))
        raise PermissionError(errno.EPERM, "not root (stand-in)")

    def chown(self, path: object, uid: int, gid: int) -> None:
        self.log.append(("chown", str(path), uid, gid))
        raise PermissionError(errno.EPERM, "not root (stand-in)")


class Recorded:
    """Wrap the real descriptor calls and `Path.replace`, recording their order; everything stays on owned files."""

    def __init__(self, write_error: OSError | None = None) -> None:
        self.log: list = []
        self.write_error = write_error
        self.owner = Owner(self.log)
        self.real = {name: getattr(os, name) for name in ("open", "write", "fchmod", "close")}
        self.real_replace = Path.replace

    def open(self, path, flags, mode=0o777, *a, **k):
        self.log.append(("open", Path(path).name, flags, mode))
        return self.real["open"](path, flags, mode, *a, **k)

    def write(self, fd, data):
        self.log.append(("write", data))
        if self.write_error is not None:
            raise self.write_error
        return self.real["write"](fd, data)

    def fchmod(self, fd, mode):
        self.log.append(("fchmod", mode))
        return self.real["fchmod"](fd, mode)

    def close(self, fd):
        self.log.append(("close",))
        return self.real["close"](fd)

    def __enter__(self) -> "Recorded":
        recorder = self

        def replace(path_self, target):
            recorder.log.append(("replace", Path(path_self).name, Path(target).name))
            return recorder.real_replace(path_self, target)
        self.patches = [patched(os, open=self.open, write=self.write, fchmod=self.fchmod, close=self.close,
                                fchown=self.owner.fchown, chown=self.owner.chown),
                        patched(Path, replace=replace)]
        for p in self.patches:
            p.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self.patches):
            p.__exit__(*exc)

    def names(self) -> list[str]:
        return [entry[0] for entry in self.log]


NO_FOLLOW_CREATE = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW


def recent_utc(stamp: str) -> bool:
    at = datetime.fromisoformat(stamp)
    return at.tzinfo is not None and at.utcoffset() == timezone.utc.utcoffset(None) and abs((datetime.now(timezone.utc) - at).total_seconds()) < 60


def module_def(name: str) -> ast.AST:
    tree = ast.parse((ROOT / "scripts" / "upgrade_records.py").read_text(encoding="utf-8"))
    found = [n for n in tree.body if getattr(n, "name", None) == name
             or isinstance(n, (ast.Assign, ast.AnnAssign)) and ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target) == name]
    check(len(found) == 1, f"{name} is defined once in the module: {len(found)}")
    return found[0]


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.upgrade_records as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_bindings() -> None:
    for order in (("scripts.upgrade_records", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.upgrade_records")):
        result = python("import importlib, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.upgrade_records as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "m.dataclass is t.dataclass is dataclasses.dataclass, "
                        "m.UPGRADE_PHASE_OWNERS == {n: o for n, o, _ in m.UPGRADE_PHASES}, "
                        "dataclasses.is_dataclass(t.RoleAccountCutover) and t.RoleAccountCutover.__module__ == 'scripts.upgrade_records')")
        check(result.stdout.strip() == "True True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.json is json and m.sys is sys and m.Path is Path and m.datetime is datetime and m.timezone is timezone,
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
            check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs (after its docstring): {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's, imports nothing: {imports}")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected})
        check(bare == [], f"{name}: none of them read past it: {bare}")
        bound = {a.arg for f in ast.walk(node) if isinstance(f, ast.FunctionDef) for a in f.args.args + f.args.kwonlyargs}
        bound |= {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
        bound |= {x.name for x in ast.walk(node) if isinstance(x, ast.ExceptHandler) and x.name}
        check(not bound & set(through), f"{name}: nothing it binds itself is read through the launcher: {bound & set(through)}")
    tree = ast.parse((ROOT / "scripts" / "upgrade_records.py").read_text(encoding="utf-8"))
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("scripts" in line for line in top), f"nothing of Switchyard's is imported at the top: {top}")
    order = [n.name if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target)
             for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    check(order == list(MOVED), f"the twenty-four in the launcher's order, the phases before their owners: {order}")
    owners = module_def("UPGRADE_PHASE_OWNERS")
    check(ast.unparse(owners.value) == "{name: owner for name, owner, _detail in UPGRADE_PHASES}",
          "the owners are built once, when the module loads, from the phase table just above")


def test_the_launcher_reexports_the_twenty_four() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.upgrade_records"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED)
          and all(a.asname is None for a in imports[0].names), "one explicit import of exactly the twenty-four, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                                                               for x in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)
                                                               if isinstance(x, ast.Name)}
    check(not defined & set(MOVED), f"and the launcher defines none of them itself: {defined & set(MOVED)}")


# --- behaviour ------------------------------------------------------------------------------------------------------


def test_the_phase_table_and_the_cutover_record() -> None:
    check([phase for phase, _owner, _detail in m.UPGRADE_PHASES] == ["artifacts", "accounts", "identities", "release", "director"]
          and m.UPGRADE_PHASE_OWNERS == {"artifacts": "root", "accounts": "operator", "identities": "root", "release": "operator",
                                         "director": "director"}, "the phases in order, and who owns each")
    check(m.UPGRADE_JOURNAL_SCHEMA == "switchyard.upgrade-journal.v1" and m.UPGRADE_SOURCE_SCHEMA == "switchyard.upgrade-source.v1"
          and m.UPGRADE_JOURNAL_OBSERVATIONS == "observations", "the schemas and the observations key")
    fields = dataclasses.fields(m.RoleAccountCutover)
    check([f.name for f in fields] == ["state", "declared", "missing_accounts", "unowned_worktrees", "credential_gaps", "misidentified_roles"]
          and fields[-1].default == () and all(f.default is dataclasses.MISSING for f in fields[:-1]),
          "the fields in order; only the misidentified roles default, to an empty tuple")
    cut = m.RoleAccountCutover("partial", (("main", "p376-main"),), ("p376-main",), ("tree not owned",), ("no key",), ("main runs as x",))
    check(isinstance(judged(setattr, cut, "state", "complete"), dataclasses.FrozenInstanceError), "frozen")
    five = judged(m.RoleAccountCutover, "complete", (), (), (), ())
    check(isinstance(five, m.RoleAccountCutover) and five.misidentified_roles == () and five.problems == [],
          f"built without the misidentified roles, as the probe does: {five!r}")
    check(cut.is_partial and not cut.is_complete and five.is_complete, "the two states")
    legacy = judged(m.RoleAccountCutover, "legacy", (), (), (), ())
    check(not legacy.is_partial and not legacy.is_complete, "any other state is neither")
    check(cut.problems == ["account p376-main does not exist", "tree not owned", "no key", "main runs as x"],
          f"the problems, in order: {cut.problems}")


def test_the_journal_is_read_from_root_or_the_tenant() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        config_path = base / "p376.json"
        check(m.upgrade_journal_path(CONFIG, config_path=config_path) == base / "p376-upgrade.json", "the tenant copy sits beside the config")
        trusted = base / "root" / "upgrade.json"
        trusted.parent.mkdir()
        trusted.write_text(json.dumps({"schema": m.UPGRADE_JOURNAL_SCHEMA, "project": "p376", "phases": {"release": {"state": "done"}}}))
        tenant = base / "p376-upgrade.json"
        tenant.write_text(json.dumps({"schema": "old", "phases": {"release": {"state": "done"}}}))
        with patched(t, privileged_upgrade_journal_path=seam("privileged_upgrade_journal_path", lambda config: trusted)):
            got = m.read_upgrade_journal(CONFIG, config_path=config_path, trusted=True)
            check(got == {"schema": m.UPGRADE_JOURNAL_SCHEMA, "project": "p376", "phases": {"release": {"state": "done"}}, "observations": {}},
                  f"trusted: root's copy, observations defaulted: {got}")
            got = m.read_upgrade_journal(CONFIG, config_path=config_path)
            check(got == {"schema": m.UPGRADE_JOURNAL_SCHEMA, "project": "p376", "phases": {}, "observations": {}},
                  f"untrusted: the tenant copy, a stale schema reset rather than believed: {got}")
            with patched(t, upgrade_journal_path=seam("upgrade_journal_path", lambda config, *, config_path: trusted),
                         _read_journal_file=seam("_read_journal_file", lambda path, project: {"read": path.name, "project": project})):
                check(m.read_upgrade_journal(CONFIG, config_path=config_path) == {"read": "upgrade.json", "project": "p376"},
                      "the launcher's path and reader decide")
        tenant.write_text("{ not json")
        check(m._read_journal_file(tenant, "p376")["phases"] == {} and m._read_journal_file(base / "absent", "p376")["schema"] == m.UPGRADE_JOURNAL_SCHEMA,
              "unreadable or absent: a fresh record")
        tenant.write_text(json.dumps({"schema": "syrd376.schema", "phases": {"x": 1}}))
        with patched(t, UPGRADE_JOURNAL_SCHEMA="syrd376.schema", UPGRADE_JOURNAL_OBSERVATIONS="syrd376-notes"):
            got = m._read_journal_file(tenant, "p376")
            check(got == {"schema": "syrd376.schema", "phases": {"x": 1}, "syrd376-notes": {}}, f"the launcher's schema and key decide: {got}")
        REACHED.update({"UPGRADE_JOURNAL_SCHEMA", "UPGRADE_JOURNAL_OBSERVATIONS"})


def test_the_pinned_source_lives_in_roots_directory() -> None:
    calls: list = []
    with patched(t, switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root", lambda: Path("/nonexistent/syrd376")),
                 privileged_provision_dir=seam("privileged_provision_dir", lambda project, *, root: calls.append((project, root)) or root / project)):
        check(m.privileged_upgrade_source_path(CONFIG) == Path("/nonexistent/syrd376/p376/upgrade-source.json")
              and calls == [("p376", Path("/nonexistent/syrd376"))], f"root's directory, root's name: {calls}")


def test_the_privileged_writer_is_staged_no_follow_and_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "priv" / "record.json"
        made: list = []
        with patched(t, ensure_privileged_provision_dir=seam("ensure_privileged_provision_dir", lambda d: made.append(d) or d.mkdir(exist_ok=True)),
                     privileged_artifact_mode=seam("privileged_artifact_mode", lambda name: 0o640)), Recorded() as rec:
            problem = m._write_privileged_json(target, {"b": 1, "a": [2]})
        check(problem == "" and made == [target.parent], f"written, its directory ensured: {problem!r}")
        check(target.read_text() == '{\n  "a": [\n    2\n  ],\n  "b": 1\n}\n' and stat.S_IMODE(target.stat().st_mode) == 0o640
              and not (target.parent / ".record.json.new").exists(), "sorted, indented, newline-ended, at the launcher's mode, staged away")
        check(rec.names() == ["open", "write", "fchmod", "fchown", "close", "replace"]
              and rec.log[0] == ("open", ".record.json.new", NO_FOLLOW_CREATE, 0o600) and rec.log[3] == ("fchown", 0, 0)
              and rec.log[5] == ("replace", ".record.json.new", "record.json"),
              f"a no-follow create at 0600, then write, mode, a best-effort root owner, close, and only then replace: {rec.log}")
        elsewhere = Path(tmp) / "elsewhere"
        elsewhere.write_text("untouched")
        (target.parent / ".record.json.new").symlink_to(elsewhere)
        with patched(t, ensure_privileged_provision_dir=lambda d: None, privileged_artifact_mode=lambda name: 0o600), Recorded() as rec:
            problem = m._write_privileged_json(target, {"c": 3})
        check("Too many levels of symbolic links" in problem and elsewhere.read_text() == "untouched" and "write" not in rec.names(),
              f"a planted link is never followed: {problem!r}")
        (target.parent / ".record.json.new").unlink()
        with patched(t, ensure_privileged_provision_dir=lambda d: None, privileged_artifact_mode=lambda name: 0o600), \
                Recorded(write_error=OSError(errno.ENOSPC, "No space left on device")) as rec:
            problem = m._write_privileged_json(target, {"c": 3})
        check(problem == "[Errno 28] No space left on device" and rec.names() == ["open", "write", "close"]
              and json.loads(target.read_text()) == {"a": [2], "b": 1}, f"a failed write is closed, never replaced, and said: {rec.log}")
        with patched(t, ensure_privileged_provision_dir=lambda d: (_ for _ in ()).throw(PermissionError(errno.EACCES, "Permission denied", str(d)))):
            problem = m._write_privileged_json(target, {"c": 3})
        check(problem.startswith("[Errno 13] Permission denied"), f"an unusable directory is the answer: {problem!r}")


def test_the_pinned_release_is_recorded_only_by_root_and_read_back() -> None:
    kwargs = dict(source_repo=Path("/nonexistent/src"), commit_git_dir=" /nonexistent/cache ", deploy_ref="syrd376/ref")
    with patched(t, _write_privileged_json=refuse("the privileged write")):
        with euid(1006):
            check(m.record_upgrade_source(CONFIG, **kwargs) == [], "unprivileged: nothing to record, and not a failure")
        with euid(0):
            check(m.record_upgrade_source(CONFIG, **kwargs, dry_run=True) == [], "a dry run records nothing")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "priv" / "upgrade-source.json"
        with euid(0), patched(t, privileged_upgrade_source_path=seam("privileged_upgrade_source_path", lambda config: path),
                              resolved_source_selection=seam("resolved_source_selection", lambda repo: f"resolved:{repo}"),
                              ensure_privileged_provision_dir=lambda d: d.mkdir(exist_ok=True),
                              privileged_artifact_mode=lambda name: 0o600), Recorded():
            check(m.record_upgrade_source(CONFIG, **kwargs) == [], "root writes it and it reads back")
        written = json.loads(path.read_text())
        check({k: v for k, v in written.items() if k != "at"} == {"schema": m.UPGRADE_SOURCE_SCHEMA, "project": "p376",
                                                                 "source_repo": "resolved:/nonexistent/src",
                                                                 "commit_git_dir": "/nonexistent/cache", "deploy_ref": "syrd376/ref"}
              and recent_utc(written["at"]), f"the selection resolved, the cache trimmed, stamped in UTC: {written}")
        with euid(0), patched(t, privileged_upgrade_source_path=lambda config: path, resolved_source_selection=lambda repo: str(repo),
                              _write_privileged_json=seam("_write_privileged_json", lambda p, payload: "disk full")):
            check(m.record_upgrade_source(CONFIG, **kwargs) == [f"could not record the pinned release at {path}: disk full"],
                  "a write that failed is a refusal, in its own words")
        with euid(0), patched(t, privileged_upgrade_source_path=lambda config: path, resolved_source_selection=lambda repo: str(repo),
                              _write_privileged_json=lambda p, payload: "",
                              read_upgrade_source=seam("read_upgrade_source", lambda config: {"deploy_ref": "other"})):
            check(m.record_upgrade_source(CONFIG, **kwargs) == [f"the pinned release at {path} did not read back as it was written"],
                  "a record that would be refused later is refused now")
        with euid(0), patched(t, privileged_upgrade_source_path=lambda config: path, resolved_source_selection=lambda repo: str(repo),
                              _write_privileged_json=lambda p, payload: "", UPGRADE_SOURCE_SCHEMA="syrd376.source",
                              read_upgrade_source=lambda config: {"source_repo": "/nonexistent/src", "commit_git_dir": "/nonexistent/cache",
                                                                  "deploy_ref": "syrd376/ref"}):
            captured: list = []
            with patched(t, _write_privileged_json=lambda p, payload: captured.append(payload) or ""):
                m.record_upgrade_source(CONFIG, **kwargs)
            check(captured and captured[0]["schema"] == "syrd376.source", "the launcher's schema is the one written")
        REACHED.add("UPGRADE_SOURCE_SCHEMA")


def test_only_roots_own_pin_record_is_believed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upgrade-source.json"
        good = {"schema": m.UPGRADE_SOURCE_SCHEMA, "project": "p376", "source_repo": " /s ", "commit_git_dir": None, "deploy_ref": "r"}

        def reads(payload: object, mode: int = 0o600, **extra: object) -> dict:
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
            os.chmod(path, mode)
            with patched(t, privileged_upgrade_source_path=lambda config: path), patched(os, **extra):
                return m.read_upgrade_source(CONFIG)
        check(reads(good) == {"source_repo": "/s", "commit_git_dir": "", "deploy_ref": "r"}, "root's own record, trimmed")
        check(reads(good, 0o620) == {} and reads(good, 0o602) == {}, "group- or world-writable: refused")
        check(reads(good, getuid=lambda: os.stat(path).st_uid + 1 if path.exists() else 1) == {}, "a file somebody else owns: refused")
        check(reads({**good, "schema": "other"}) == {} and reads({**good, "project": "other"}) == {} and reads([good]) == {}
              and reads("{") == {}, "another schema, project, shape, or no JSON at all: refused")
        path.unlink()
        with patched(t, privileged_upgrade_source_path=lambda config: path, current_user_name=lambda: "syrd376-me"):
            check(m.read_upgrade_source(CONFIG) == {}, "absent: nothing")
            check(m.upgrade_source_unavailable_reason(CONFIG) == f"root recorded no pinned release at {path}", "absent, said so")
            path.write_text("{}")
            check(m.upgrade_source_unavailable_reason(CONFIG) == f"root's pinned-release record {path} was refused as not root's own",
                  "present but refused, said so")

        class Unreadable:
            def stat(self):
                raise PermissionError(errno.EACCES, "Permission denied")

            def __str__(self) -> str:
                return "/nonexistent/syrd376/upgrade-source.json"
        with patched(t, privileged_upgrade_source_path=seam("privileged_upgrade_source_path", lambda config: Unreadable()),
                     current_user_name=seam("current_user_name", lambda: "syrd376-me")):
            check(m.upgrade_source_unavailable_reason(CONFIG)
                  == "root's pinned-release record /nonexistent/syrd376/upgrade-source.json is not readable by syrd376-me (Permission denied)",
                  "unreadable, by whom, and why")


def test_an_unpinned_invocation_is_filled_from_roots_record() -> None:
    record = {"source_repo": "/rec/src", "commit_git_dir": "/rec/cache", "deploy_ref": "rec/ref"}
    with patched(t, DEFAULT_TENANT_RELEASE_DEPLOY_REF="syrd376/default"):
        with patched(t, read_upgrade_source=seam("read_upgrade_source", lambda config: {})):
            check(m.resolve_pinned_upgrade_source(CONFIG, source_repo=None, commit_git_dir=None, deploy_ref=None)
                  == (None, None, "syrd376/default", ""), "no record: the launcher's default ref, nothing used")
        with patched(t, read_upgrade_source=lambda config: record):
            check(m.resolve_pinned_upgrade_source(CONFIG, source_repo=None, commit_git_dir=None, deploy_ref=None)
                  == (Path("/rec/src"), "/rec/cache", "rec/ref", "source /rec/src; commit cache /rec/cache; deploy ref rec/ref"),
                  "everything unpinned comes from the record, and says so")
            check(m.resolve_pinned_upgrade_source(CONFIG, source_repo=Path("/mine"), commit_git_dir="/c", deploy_ref="origin/main")
                  == (Path("/mine"), "/c", "origin/main", ""), "an explicit argument always wins")
            check(m.resolve_pinned_upgrade_source(CONFIG, source_repo=None, commit_git_dir=None, deploy_ref="")
                  == (Path("/rec/src"), "/rec/cache", "syrd376/default", "source /rec/src; commit cache /rec/cache"),
                  "an empty ref was given, so it is not filled; it falls to the default")
        with patched(t, read_upgrade_source=lambda config: {"source_repo": "", "commit_git_dir": "", "deploy_ref": ""}):
            check(m.resolve_pinned_upgrade_source(CONFIG, source_repo=None, commit_git_dir=None, deploy_ref=None)
                  == (None, None, "syrd376/default", ""), "an empty record fills nothing")
    REACHED.add("DEFAULT_TENANT_RELEASE_DEPLOY_REF")


def test_an_unprivileged_phase_is_only_an_observation() -> None:
    noted: list = []
    with patched(t, _record_upgrade_observation=seam("_record_upgrade_observation", lambda config, **kw: noted.append(kw)),
                 read_upgrade_journal=refuse("root's journal"), publish_tenant_journal_projection=refuse("the projection")):
        with euid(0):
            m.record_upgrade_phase(CONFIG, config_path=Path("/nonexistent/c.json"), phase="release", state="done", dry_run=True)
        check(noted == [], "a dry run records nothing at all")
        with euid(1006):
            m.record_upgrade_phase(CONFIG, config_path=Path("/nonexistent/c.json"), phase="release", state="done", detail="saw it")
    check(noted == [{"config_path": Path("/nonexistent/c.json"), "phase": "release", "state": "done", "detail": "saw it"}],
          f"not root: an observation, never a phase: {noted}")


def test_root_records_the_phase_and_republishes_the_tenant_copy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        journal_path = Path(tmp) / "priv" / "upgrade.json"
        trusted = {"schema": m.UPGRADE_JOURNAL_SCHEMA, "project": "old", "phases": {"accounts": {"state": "done"}}, "observations": {}}
        projected: list = []
        stand_ins = dict(
            read_upgrade_journal=seam("read_upgrade_journal", lambda config, *, config_path, trusted=False: trusted and dict(trusted_copy)),
            privileged_upgrade_journal_path=lambda config: journal_path,
            ensure_privileged_provision_dir=lambda d: d.mkdir(exist_ok=True),
            privileged_artifact_mode=lambda name: 0o640,
            publish_tenant_journal_projection=seam("publish_tenant_journal_projection",
                                                   lambda config, *, config_path, trusted: projected.append(trusted)),
            UPGRADE_PHASE_OWNERS={"release": "syrd376-operator"},
            _record_upgrade_observation=refuse("an observation"),
        )
        trusted_copy = {**trusted, "phases": dict(trusted["phases"])}
        with euid(0), patched(t, **stand_ins), Recorded() as rec:
            m.record_upgrade_phase(CONFIG, config_path=Path(tmp) / "p376.json", phase="release", state="ready", detail="d")
        written = json.loads(journal_path.read_text())
        entry = written["phases"]["release"]
        check(written["project"] == "p376" and written["phases"]["accounts"] == {"state": "done"}
              and {k: v for k, v in entry.items() if k != "at"} == {"state": "ready", "owner": "syrd376-operator", "detail": "d"}
              and recent_utc(entry["at"]), f"root's journal gains the phase, owned per the launcher's table, stamped in UTC: {written}")
        check(stat.S_IMODE(journal_path.stat().st_mode) == 0o640 and journal_path.read_text().endswith("}\n"), "at the launcher's mode")
        check(rec.names() == ["open", "write", "fchmod", "fchown", "close", "replace"]
              and rec.log[0] == ("open", ".upgrade.json.new", NO_FOLLOW_CREATE, 0o600) and rec.log[3] == ("fchown", 0, 0),
              f"staged no-follow at 0600, written, moded, a best-effort owner, closed, then replaced: {rec.log}")
        check(projected == [written], "and the tenant copy is republished from exactly what root recorded")
        REACHED.update({"UPGRADE_PHASE_OWNERS", "privileged_upgrade_journal_path", "ensure_privileged_provision_dir", "privileged_artifact_mode"})
        # The phase writer's owner attempt covers the directory too; with fchown allowed, chown(parent) is reached.
        owner_log: list = []
        with euid(0), patched(t, **stand_ins), patched(os, fchown=lambda fd, u, g: owner_log.append(("fchown", u, g)),
                                                       chown=lambda p, u, g: owner_log.append(("chown", str(p), u, g))):
            m.record_upgrade_phase(CONFIG, config_path=Path(tmp) / "p376.json", phase="release", state="ready")
        check(owner_log == [("fchown", 0, 0), ("chown", str(journal_path.parent), 0, 0)], f"the file, then its directory, to root: {owner_log}")
        projected.clear()
        err = io.StringIO()
        with euid(0), patched(t, **stand_ins), Recorded(write_error=OSError(errno.EIO, "Input/output error")) as rec, redirect_stderr(err):
            m.record_upgrade_phase(CONFIG, config_path=Path(tmp) / "p376.json", phase="release", state="done")
        check(err.getvalue() == "switchyard: could not record release for p376: [Errno 5] Input/output error\n"
              and rec.names() == ["open", "write", "close"] and len(projected) == 1 and projected[0]["phases"]["release"]["state"] == "done",
              f"a failed write is said, closed, not replaced -- and the projection still follows: {err.getvalue()!r} {rec.log}")


def test_the_tenant_copy_is_root_s_journal_plus_unanswered_notes() -> None:
    published: list = []
    existing = {"observations": {"release": {"state": "done"}, "director": {"state": "seen"}}}
    trusted = {"phases": {"release": {"state": "ready"}}}
    with patched(t, read_upgrade_journal=lambda config, *, config_path, trusted=False: refuse("root's copy")() if trusted else existing,
                 upgrade_journal_path=lambda config, *, config_path: config_path.with_name("syrd376-copy.json"),
                 publish_tenant_artifact=seam("publish_tenant_artifact",
                                              lambda config, directory, name, body: published.append((directory, name, body)) or (True, ""))):
        m.publish_tenant_journal_projection(CONFIG, config_path=Path("/nonexistent/t/p376.json"), trusted=trusted)
    directory, name, body = published[0]
    payload = json.loads(body)
    check(directory == Path("/nonexistent/t") and name == "syrd376-copy.json" and body.endswith(b"}\n"), "published beside the config, by name")
    check(payload == {"schema": m.UPGRADE_JOURNAL_SCHEMA, "project": "p376", "phases": {"release": {"state": "ready"}},
                      "observations": {"director": {"state": "seen"}}, "phases_written_by": "root",
                      "note": ("phases are a copy of the root-owned journal and are the only phase record anything reads; "
                               "observations are what unprivileged commands saw and decide nothing")},
          f"root's phases verbatim; the answered observation dropped: {payload}")
    err = io.StringIO()
    with patched(t, read_upgrade_journal=lambda config, *, config_path, trusted=False: {},
                 publish_tenant_artifact=lambda *a: (False, "syrd376: could not publish")), redirect_stderr(err):
        m.publish_tenant_journal_projection(CONFIG, config_path=Path("/nonexistent/t/p376.json"), trusted={})
    check(err.getvalue() == "syrd376: could not publish\n", f"a refused publication is said: {err.getvalue()!r}")


def test_an_observation_touches_only_the_tenant_copy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "p376-upgrade.json"
        journal = {"schema": m.UPGRADE_JOURNAL_SCHEMA, "project": "old", "phases": {"release": {"state": "ready"}}}
        with patched(t, read_upgrade_journal=lambda config, *, config_path, trusted=False: refuse("root's copy")() if trusted else dict(journal),
                     upgrade_journal_path=lambda config, *, config_path: copy,
                     current_user_name=lambda: "syrd376-director"):
            m._record_upgrade_observation(CONFIG, config_path=Path(tmp) / "p376.json", phase="release", state="done", detail="saw")
        written = json.loads(copy.read_text())
        note = (written.get("observations") or {}).get("release") or {}
        check(written["phases"] == {"release": {"state": "ready"}} and written["project"] == "p376"
              and {k: v for k, v in note.items() if k != "at"} == {"state": "done", "observed_by": "syrd376-director", "detail": "saw"}
              and recent_utc(note["at"]) and not (Path(tmp) / ".p376-upgrade.json.new").exists(),
              f"phases untouched; the note says who saw what, when: {written}")
        err = io.StringIO()
        with patched(t, read_upgrade_journal=lambda config, *, config_path, trusted=False: {},
                     upgrade_journal_path=lambda config, *, config_path: Path(tmp) / "missing" / "copy.json",
                     current_user_name=lambda: "x"), redirect_stderr(err):
            m._record_upgrade_observation(CONFIG, config_path=Path(tmp) / "p376.json", phase="release", state="done", detail="")
        check(err.getvalue().startswith("switchyard: could not note the release observation for p376: [Errno 2]"), f"{err.getvalue()!r}")
    journal = {"phases": {"release": {"state": "done"}, "odd": "text"}, "observations": {"release": {"state": "seen"}, "odd": 3},
               "syrd376-notes": {"release": {"state": "patched"}}}
    check(m.upgrade_phase_state(journal, "release") == "done" and m.upgrade_phase_state(journal, "odd") == ""
          and m.upgrade_phase_state({}, "release") == "", "a phase's state, or nothing")
    check(m.upgrade_phase_observation(journal, "release") == {"state": "seen"} and m.upgrade_phase_observation(journal, "odd") == {},
          "an observation, or nothing")
    with patched(t, UPGRADE_JOURNAL_OBSERVATIONS="syrd376-notes"):
        check(m.upgrade_phase_observation(journal, "release") == {"state": "patched"}, "the launcher's key decides")


def presence(**kw: object) -> SimpleNamespace:
    base = dict(non_declarative_by_design=False, declared_somewhere=False, legacy_without_workflow=False, config_unreadable=None)
    return SimpleNamespace(**{**base, **kw})


def test_the_director_phase_is_required_unless_every_source_agrees_there_is_none() -> None:
    cp = Path("/nonexistent/c.json")
    check(not m.director_phase_required(CONFIG, config_path=cp, presence=presence()), "nothing declared anywhere: not required")
    check(not m.director_phase_required(CONFIG, config_path=cp, presence=presence(non_declarative_by_design=True)), "non-declarative by design")
    check(m.director_phase_required(CONFIG, config_path=cp, presence=presence(non_declarative_by_design=True, declared_somewhere=True)),
          "declared somewhere wins over the design")
    check(m.director_phase_required(CONFIG, config_path=cp, presence=presence(legacy_without_workflow=True))
          and m.director_phase_required(CONFIG, config_path=cp, presence=presence(config_unreadable="broken")),
          "a legacy tenant, or an unreadable config: required")
    asked: list = []
    with patched(t, declared_workflow_presence=seam("declared_workflow_presence",
                                                    lambda config, *, config_path: asked.append(config_path) or presence(declared_somewhere=True))):
        check(m.director_phase_required(CONFIG, config_path=cp) and asked == [cp], "without a presence, the launcher asks")


def test_the_release_phase_from_what_is_deployed() -> None:
    recorded: list = []
    with patched(t, record_upgrade_phase=seam("record_upgrade_phase", lambda config, **kw: recorded.append(kw)),
                 _format_release_sha=seam("_format_release_sha", lambda sha: f"<{sha}>")):
        check(m.record_release_phase_from_status(CONFIG, config_path=Path("/c"), status=None) is False, "no reading: not done")
        check(judged(m.record_release_phase_from_status, CONFIG, config_path=Path("/c"),
                     status=SimpleNamespace(unchanged=False, current_sha="def", deploy_ref="ref")) is False
              and m.record_release_phase_from_status(CONFIG, config_path=Path("/c"), dry_run=True,
                                                     status=SimpleNamespace(unchanged=True, current_sha="abc", deploy_ref="ref")) is True,
              "changed: not done; unchanged: done")
    check(recorded == [dict(config_path=Path("/c"), phase="release", state="ready", detail="", dry_run=False)] * 2
          + [dict(config_path=Path("/c"), phase="release", state="done", dry_run=True,
                  detail="deployed release <abc> already matches ref; the identities transaction switched it")],
          f"ready with nothing to say, or done with why: {recorded}")


def test_the_phase_report() -> None:
    cut = lambda state: m.RoleAccountCutover(state, (), (), (), (), ())  # noqa: E731
    journal = {"phases": {"artifacts": {"state": "done"}, "release": {"state": "ready"}, "director": {"state": "done"}}}
    asked: list = []
    required: list = []

    def report(state, *, legacy=False, need=True, **kw):
        asked.clear(); required.clear()
        with patched(t, declared_workflow_presence=seam("declared_workflow_presence",
                                                        lambda config, *, config_path: asked.append(1) or presence(legacy_without_workflow=legacy)),
                     director_phase_required=seam("director_phase_required",
                                                  lambda config, *, config_path, presence: required.append(presence) or need)):
            return m.upgrade_phase_report(CONFIG, config_path=Path("/c"), cutover=cut(state), journal=journal, **kw)
    lines = report("partial", desktop_policy={"mode": "headless"})
    check(lines == ["switchyard: p376 upgrade phases",
                    f"  {'artifacts':<11} {'root':<8} {'done':<12} regenerate generated artifacts that are safe while the current roles run",
                    f"  {'accounts':<11} {'operator':<8} {'pending':<12} repatriate legacy resumable state without deleting accounts",
                    f"  {'identities':<11} {'root':<8} {'pending':<12} verify worktrees and role-local state belong to the project account",
                    f"  {'release':<11} {'operator':<8} {'ready':<12} deploy the board release that enforces process-bound authority",
                    f"  {'director':<11} {'director':<8} {'done':<12} migrate the declarative director onboarding through the director's own board authority",
                    f"  {'desktop':<11} {'operator':<8} {'ready':<12} role launches use the headless policy recorded for p376"],
          f"every phase, its owner and state, then the desktop: {lines}")
    check(len(asked) == 1 and len(required) == 1, "the board is asked once, for the director's row only")
    check(report("complete")[2].split()[2] == "done", "a complete cutover closes the accounts phase")
    check(report("partial", need=False)[5].split()[2:4] == ["not", "required"], "a director phase nobody needs")
    legacy = report("partial", legacy=True)[5]
    check(legacy.split()[2] == "pending" and legacy.endswith("Run `switchyard migrate-workflow p376` to see what would be installed")
          and "the board is running no declared workflow" in legacy, f"a legacy board is pending, and says what to run: {legacy}")
    dry = report("partial", desktop_policy={"mode": "wayland"}, dry_run=True)[-1]
    check(dry.endswith("role launches would use the wayland policy supplied for p376; a dry run records nothing"), dry)
    for policy in (None, {"mode": "x11"}):
        missing = report("partial", desktop_policy=policy)[-1]
        check(missing == f"  {'desktop':<11} {'operator':<8} {'missing':<12} choose one with `switchyard upgrade p376 --desktop-policy "
                         "headless|FILE` before any role is started", missing)
    with patched(t, UPGRADE_PHASES=(("only", "syrd376", "patched table"),),
                 upgrade_phase_state=seam("upgrade_phase_state", lambda journal, phase: "syrd376-state")):
        lines = m.upgrade_phase_report(CONFIG, config_path=Path("/c"), cutover=cut("partial"), journal={})
    check(lines[1] == f"  {'only':<11} {'syrd376':<8} {'syrd376-state':<12} patched table", f"the launcher's table and state decide: {lines}")
    REACHED.add("UPGRADE_PHASES")


def test_the_outstanding_release_phase() -> None:
    done = m.outstanding_release_phase_report(CONFIG, config_path=Path("/c"), journal={"phases": {"release": {"state": "done"}}})
    check(done == ["switchyard: p376's release phase is closed; artifacts are prepared and the board is deployed."], f"{done}")
    check(m.outstanding_release_phase_report(CONFIG, config_path=Path("/c"), journal={"phases": {"release": {"state": "not required"}}}) == [],
          "not required: nothing owed")
    owed = m.outstanding_release_phase_report(CONFIG, config_path=Path("/c"), journal={})
    check(len(owed) == 2 and "Its release phase is pending and is an operator's" in owed[0]
          and "`pkexec switchyard release-status p376 --close`" in owed[0] and owed[1].startswith("switchyard: `switchyard release-status p376`"),
          f"pending: what exit 0 means, and the command that closes it: {owed}")
    with patched(t, upgrade_phase_state=seam("upgrade_phase_state", lambda journal, phase: "syrd376-ready")):
        check("Its release phase is syrd376-ready" in m.outstanding_release_phase_report(CONFIG, config_path=Path("/c"), journal={})[0],
              "the launcher's reader decides")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, "
                               f"extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_bindings",
             "test_the_seams_read_through_the_launcher_and_nothing_bound",
             "test_the_launcher_reexports_the_twenty_four")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"upgrade_records_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
