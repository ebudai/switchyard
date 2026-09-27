#!/usr/bin/env python3
"""SYRD-362: the tenant configuration record and its verifier, against the launcher they came out of.

`TENANT_CONFIG_RECORD_NAME`, `tenant_config_record_path`,
`normalize_tenant_config_mode`, `registered_tenant_config_path`,
`recorded_tenant_config_path`, `record_tenant_config_path`,
`_tenant_config_candidates`, `board_declared_role_names`,
`tenant_config_conflicts` and `verified_tenant_config` moved into
`scripts/tenant_config_records.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports only the standard library at its top.
- **One set of objects.** The launcher re-exports every name, the very same
  objects whichever module is imported first, so resume-provision,
  `pane_rebind` and `workflow_adoption` reach them.
- **Seams (rule 24).** Every launcher facility these use, and every name here
  another definition here reads, is read from the launcher when it runs.
  Nothing they bind -- the nested `disagree`, an `except` binding, a
  comprehension variable -- is read through it (rule 27).
- **The behaviour is unchanged:**
  - the mode is normalised on one no-follow descriptor, owner checked first,
    only group and world write taken off, and the descriptor always closed;
  - the registry pointer and root's record are read root-owned and no-follow,
    and the record is staged, owned by root, moded and replaced in that order;
  - candidates are explicit, then recorded, then registered, then the
    conventional layout;
  - conflicts are named in the same order, and an added role counts only when
    the board corroborates it;
  - the verifier normalises before the strict reader, asks the board only when
    roles are the only disagreement, stays fail-closed, and returns the first
    matching or conflicting candidate.

Every facility is this test's own fixture or fake, installed before anything
runs. The module's `os` is a recording proxy: no real `fchown` ever runs, and
the only files opened or moded are ones this test created in its own
temporary directories. No root record, registry, board, account or tenant is
touched.
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

CHECKS = 0
MOVED = ("TENANT_CONFIG_RECORD_NAME", "tenant_config_record_path", "normalize_tenant_config_mode", "registered_tenant_config_path",
         "recorded_tenant_config_path", "record_tenant_config_path", "_tenant_config_candidates", "board_declared_role_names",
         "tenant_config_conflicts", "verified_tenant_config")
OWN = ("json", "os", "stat", "Path", "Any")
SEAMS = {
    "tenant_config_record_path": {"privileged_baseline_plan_path": 1, "TENANT_CONFIG_RECORD_NAME": 1},
    "registered_tenant_config_path": {"switchyard_registry_dir": 1, "read_plan_no_follow": 1},
    "recorded_tenant_config_path": {"read_plan_no_follow": 1, "tenant_config_record_path": 1},
    "record_tenant_config_path": {"tenant_config_record_path": 1, "ensure_privileged_provision_dir": 1, "privileged_artifact_mode": 1},
    "verified_tenant_config": {"recorded_tenant_config_path": 1, "registered_tenant_config_path": 1, "_tenant_config_candidates": 1,
                               "expected_privileged_uid": 1, "normalize_tenant_config_mode": 1, "read_plan_no_follow": 1,
                               "load_project_config": 1, "tenant_config_conflicts": 2, "read_board_declared_workflow": 1,
                               "board_declared_role_names": 1},
}
SLUG = "p362"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def attempt(call):
    """(result, None) or (None, what it raised) -- anything a mutant raises is judged, not a crash."""
    try:
        return call(), None
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- SystemExit and OSError are answers here
        return None, exc


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    result, raised = attempt(lambda: function(*args, **kwargs))
    return result if raised is None else raised


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


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


class RecordingOs:
    """The module's `os`, recording every descriptor call.

    `open`, `fstat`, `write` and `close` reach the real calls, on this test's own
    files only; `fchown` never does. `fchmod` reaches the real call on this
    test's own file unless told to fail, and `fail` makes any named call raise.
    """

    def __init__(self, *, fail: dict[str, OSError] | None = None, real_fchmod: bool = True) -> None:
        self.calls: list[tuple] = []
        self.fail = fail or {}
        self.real_fchmod = real_fchmod

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    def _step(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    def open(self, path, flags, mode=0o777):
        self._step("open", str(path), flags, mode)
        return os.open(path, flags, mode)

    def fstat(self, descriptor):
        self._step("fstat")
        return os.fstat(descriptor)

    def fchmod(self, descriptor, mode):
        self._step("fchmod", oct(mode))
        if self.real_fchmod:
            os.fchmod(descriptor, mode)

    def fchown(self, descriptor, uid, gid):
        self._step("fchown", uid, gid)

    def write(self, descriptor, data):
        self._step("write", bytes(data))
        return os.write(descriptor, data)

    def close(self, descriptor):
        self._step("close")
        os.close(descriptor)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def document(data: dict) -> SimpleNamespace:
    return SimpleNamespace(data=data)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.tenant_config_records as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.tenant_config_records", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_config_records"),
                  ("scripts.pane_rebind", "scripts.workflow_adoption", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_config_records as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_nested_closure_and_the_defaults() -> None:
    module = ast.parse((ROOT / "scripts" / "tenant_config_records.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    names = [n.name if isinstance(n, ast.FunctionDef) else n.targets[0].id for n in module.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(names == list(MOVED), f"the ten, in baseline order: {names}")
    every = {name for reads in SEAMS.values() for name in reads}
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    for f in functions:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every})
        check(through == SEAMS.get(f.name, {}) and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")
    conflicts = next(f for f in functions if f.name == "tenant_config_conflicts")
    check([n.name for n in ast.walk(conflicts) if isinstance(n, ast.FunctionDef) and n is not conflicts] == ["disagree"],
          "the nested disagree closure is still the conflicts function's own")
    locals_ = [ast.unparse(n) for f in functions for n in ast.walk(f) if isinstance(n, (ast.Import, ast.ImportFrom))
               and ast.unparse(n) != "from scripts import team_launcher as launcher"]
    check(locals_ == [], f"no other function-level import was added: {locals_}")
    from scripts import team_launcher, tenant_config_records as m

    check(m.TENANT_CONFIG_RECORD_NAME == "tenant-config.json" and m.TENANT_CONFIG_RECORD_NAME is team_launcher.TENANT_CONFIG_RECORD_NAME,
          "the record name is one object on both modules")
    check(m.verified_tenant_config.__kwdefaults__ == {"explicit": None, "owner_uid": None, "registry_dir": None,
                                                     "board_reader": None, "corroborate_roles": True}
          and m.tenant_config_conflicts.__kwdefaults__ == {"corroborated_roles": ()}
          and m._tenant_config_candidates.__kwdefaults__ == {"registered": None}
          and m.registered_tenant_config_path.__kwdefaults__ == {"registry_dir": None},
          "every default unchanged: corroboration on, nothing else assumed")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)} | {
        t.id for n in launcher.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    check(not defined & set(MOVED), f"the launcher defines none of them: {defined & set(MOVED)}")
    # The interleaved packet-completion definitions did not come here. SYRD-363
    # moved them on to scripts/packet_completion.py and SYRD-365 moved
    # recovery_readiness_problems to scripts/recovery_readiness.py; the launcher
    # re-exports both.
    own = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    reexported = {a.name for n in launcher.body if isinstance(n, ast.ImportFrom)
                  and n.module in ("scripts.packet_completion", "scripts.recovery_readiness") for a in n.names}
    check({"PacketCompletion", "privileged_packet_completion", "recovery_readiness_problems"} <= own | reexported
          and not {"PacketCompletion", "privileged_packet_completion", "recovery_readiness_problems"} & {
              n.name for n in module.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))},
          "the interleaved packet-completion definitions did not move here, and the launcher still has them")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_config_records"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all ten: {exported}")
    for consumer in ("pane_rebind.py", "workflow_adoption.py"):
        tree = ast.parse((ROOT / "scripts" / consumer).read_text(encoding="utf-8"))
        reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "verified_tenant_config"]
        bare = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "verified_tenant_config"]
        check(len(reads) == 1 and isinstance(reads[0].value, ast.Name) and reads[0].value.id == "launcher" and not bare,
              f"{consumer} still reads the verifier through the launcher")


# --- the mode, on one descriptor --------------------------------------------------------------------------------------


def test_the_mode_is_normalised_on_one_no_follow_descriptor() -> None:
    from scripts import tenant_config_records as m

    me = os.getuid()
    flags = os.O_RDONLY | os.O_NOFOLLOW
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text("{}\n")
        path.chmod(0o666)
        fake = RecordingOs()
        with patched(m, os=fake):
            check(judged(m.normalize_tenant_config_mode, path, permitted_uids=[me]) == [], "tightened: no problem")
        check(stat.S_IMODE(path.stat().st_mode) == 0o644, f"only group and world write came off: {oct(path.stat().st_mode)}")
        check(fake.calls == [("open", str(path), flags, 0o777), ("fstat",), ("fchmod", "0o644"), ("close",)],
              f"open no-follow, fstat, fchmod on that descriptor, close: {fake.calls}")

        path.chmod(0o2775)
        fake = RecordingOs()
        with patched(m, os=fake):
            m.normalize_tenant_config_mode(path, permitted_uids=[me])
        check(stat.S_IMODE(path.stat().st_mode) == 0o2755, f"every other bit kept, setgid included: {oct(path.stat().st_mode)}")

        fake = RecordingOs()
        with patched(m, os=fake):
            check(judged(m.normalize_tenant_config_mode, path, permitted_uids=[me]) == [] and fake.names() == ["open", "fstat", "close"],
                  f"already compliant: not written to at all: {fake.calls}")
        path.chmod(0o660)
        fake = RecordingOs()
        with patched(m, os=fake):
            check(judged(m.normalize_tenant_config_mode, path, permitted_uids=[me + 4242, me + 4243]) == []
                  and fake.names() == ["open", "fstat", "close"] and stat.S_IMODE(path.stat().st_mode) == 0o660,
                  f"somebody else's file is left alone, for the reader to refuse: {fake.calls}")
        fake = RecordingOs(fail={"fchmod": PermissionError(errno.EPERM, "Operation not permitted")})
        with patched(m, os=fake):
            got = judged(m.normalize_tenant_config_mode, path, permitted_uids=[me])
        check(got == [f"{path} is mode 0660 and could not be tightened: Operation not permitted"] and fake.names()[-1] == "close",
              f"a refused fchmod is reported with the mode, and the descriptor still closed: {got} {fake.calls}")
        fake = RecordingOs(fail={"fstat": OSError(errno.EIO, "I/O error")})
        with patched(m, os=fake):
            got = judged(m.normalize_tenant_config_mode, path, permitted_uids=[me])
        check(isinstance(got, OSError) and fake.names() == ["open", "fstat", "close"], f"closed even when fstat raises: {got!r} {fake.calls}")

        directory = Path(tmp) / "adir"
        directory.mkdir()
        fake = RecordingOs()
        with patched(m, os=fake):
            got = judged(m.normalize_tenant_config_mode, directory, permitted_uids=[me])
        check(got == [f"{directory} is not a regular file"] and fake.names() == ["open", "fstat", "close"], f"not a regular file: {got}")
        link = Path(tmp) / "link.json"
        link.symlink_to(path)
        fake = RecordingOs()
        with patched(m, os=fake):
            got = judged(m.normalize_tenant_config_mode, link, permitted_uids=[me])
        check(isinstance(got, list) and len(got) == 1 and got[0].startswith(f"{link} could not be opened to check its mode: ")
              and fake.names() == ["open"] and stat.S_IMODE(path.stat().st_mode) == 0o660,
              f"a symlink is not followed, and nothing is closed that was never opened: {got} {fake.calls}")
        missing = Path(tmp) / "absent.json"
        got = judged(m.normalize_tenant_config_mode, missing, permitted_uids=[me])
        check(got == [f"{missing} could not be opened to check its mode: No such file or directory"], f"the open error's strerror: {got}")


# --- the registry pointer and root's record ----------------------------------------------------------------------------


def test_the_registry_pointer_is_a_root_owned_absolute_path_or_nothing() -> None:
    from scripts import team_launcher as t

    read: list = []

    def reader(answer):
        return lambda path, **kwargs: read.append((path, kwargs)) or answer

    registry = Path("/fixture/registry")
    for answer, expected in (((None, "absent"), None), ((document({}), ""), None), ((document({"config_path": "  "}), ""), None),
                             ((document({"config_path": "relative/p362.json"}), ""), None),
                             ((document({"config_path": " /fixture/home/p362.json "}), ""), Path("/fixture/home/p362.json"))):
        read.clear()
        with patched(t, read_plan_no_follow=reader(answer), switchyard_registry_dir=refuse("the host registry")):
            got = judged(t.registered_tenant_config_path, SLUG, registry_dir=registry)
        check(got == expected and read == [(registry / "p362.json", {"require_root_owned": True})],
              f"{answer[0] and answer[0].data}: {got}; read root-owned, no-follow, from the named registry: {read}")
    read.clear()
    with patched(t, read_plan_no_follow=reader((None, "")), switchyard_registry_dir=lambda: Path("/fixture/default-registry")):
        judged(t.registered_tenant_config_path, SLUG)
    check(read and read[0][0] == Path("/fixture/default-registry/p362.json"), f"no registry named: the host's, read at call time: {read}")


def test_roots_record_is_read_and_written_where_only_root_can() -> None:
    from scripts import team_launcher as t, tenant_config_records as m

    with patched(t, privileged_baseline_plan_path=lambda slug: Path(f"/fixture/etc/{slug}/plan.json")):
        check(judged(t.tenant_config_record_path, SLUG) == Path("/fixture/etc/p362/tenant-config.json"), "beside root's plan record")
        with patched(t, TENANT_CONFIG_RECORD_NAME="seam.json"):
            check(judged(t.tenant_config_record_path, SLUG) == Path("/fixture/etc/p362/seam.json"), "the name is read through the launcher")
    read: list = []
    for data, expected in (({"config_path": " /fixture/c.json "}, Path(" /fixture/c.json ".strip())), ({"config_path": ""}, None), ({}, None)):
        read.clear()
        with patched(t, tenant_config_record_path=lambda slug: Path(f"/fixture/record/{slug}.json"),
                     read_plan_no_follow=lambda path, **kwargs: read.append((path, kwargs)) or (document(data), "")):
            got = judged(t.recorded_tenant_config_path, SLUG)
        check(got == expected and read == [(Path("/fixture/record/p362.json"), {"require_root_owned": True})],
              f"{data}: {got}; root-owned no-follow read of the record: {read}")
    with patched(t, tenant_config_record_path=lambda slug: Path("/fixture/r.json"), read_plan_no_follow=lambda path, **kwargs: (None, "no")):
        check(judged(t.recorded_tenant_config_path, SLUG) is None, "no record: nothing")

    with tempfile.TemporaryDirectory() as tmp:
        record = Path(tmp) / "etc" / "tenant-config.json"
        log: list = []
        seams = dict(tenant_config_record_path=lambda slug: record,
                     ensure_privileged_provision_dir=lambda path: log.append(("ensure", path)) or path.mkdir(parents=True, exist_ok=True),
                     privileged_artifact_mode=lambda name: log.append(("mode-of", name)) or 0o640)
        fake = RecordingOs()
        with patched(t, **seams), patched(m, os=fake):
            got = judged(t.record_tenant_config_path, SLUG, Path("/fixture/home/Projects/p362/config.json"))
        payload = (json.dumps({"config_path": "/fixture/home/Projects/p362/config.json", "project": SLUG}, indent=2, sort_keys=True) + "\n").encode()
        staged = record.with_name(".tenant-config.json.new")
        check(got == record and record.read_bytes() == payload and not staged.exists(), f"the exact payload, replaced into place: {got}")
        check(log == [("ensure", record.parent), ("mode-of", "tenant-config.json")], f"the directory is ensured first, the mode is the artifact's: {log}")
        check(fake.calls == [("open", str(staged), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), ("write", payload),
                             ("fchown", 0, 0), ("fchmod", "0o640"), ("close",)],
              f"staged no-follow 0600, written, owned by root, moded, closed -- in that order: {fake.calls}")
        record.unlink()
        fake = RecordingOs(fail={"fchown": PermissionError(errno.EPERM, "Operation not permitted")})
        with patched(t, **seams), patched(m, os=fake):
            got = judged(t.record_tenant_config_path, SLUG, Path("/fixture/c.json"))
        check(isinstance(got, PermissionError) and fake.names() == ["open", "write", "fchown", "close"] and not record.exists()
              and staged.exists(), f"a refused fchown closes the descriptor and replaces nothing: {got!r} {fake.calls}")


# --- candidates, conflicts, corroboration -------------------------------------------------------------------------------


def plan_for(tmp: Path, **overrides: object) -> SimpleNamespace:
    fields = dict(project=SLUG, project_name="Porter", owner_user="syrd362-owner", owner_home=str(tmp / "home"), ticket_prefix="PTR",
                  socket_path="/fixture/run/p362.sock", port=8362, caller_roles=("director", "main"))
    fields.update(overrides)
    return SimpleNamespace(**fields)


def config_for(tmp: Path, **overrides: object) -> SimpleNamespace:
    fields = dict(project=SLUG, run_as_user="syrd362-owner", ticket_prefix="PTR", board_socket=Path("/fixture/run/p362.sock"),
                  board_url="http://127.0.0.1:8362/", repository=tmp / "home" / "Projects" / "p362", session_dir=None,
                  roles=[SimpleNamespace(role="director"), SimpleNamespace(role="main")])
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_candidates_in_precedence_order() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        plan = plan_for(Path(tmp))
        rec, reg = Path("/fixture/recorded.json"), Path("/fixture/registered.json")
        saved = os.environ.get("HOME")
        os.environ["HOME"] = tmp
        try:
            check(judged(t._tenant_config_candidates, plan, SLUG, explicit=Path("~/x.json"), recorded=rec, registered=reg) == [Path(tmp) / "x.json"],
                  "an explicit path is the whole answer, `~` expanded")
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved
        check(judged(t._tenant_config_candidates, plan, SLUG, explicit=None, recorded=Path("~/r.json"), registered=reg) == [Path("~/r.json")],
              "then root's record, as recorded (not expanded)")
        check(judged(t._tenant_config_candidates, plan, SLUG, explicit=None, recorded=None, registered=Path("~/g.json")) == [Path("~/g.json")],
              "then the registry's pointer, as recorded")
        home = Path(tmp) / "home"
        conventional = lambda name: home / "Projects" / name / ".switchyard" / "provision" / "p362.json"
        check(judged(t._tenant_config_candidates, plan, SLUG, explicit=None, recorded=None) == [conventional("Porter"), conventional("p362")],
              "then the conventional layout: the project name, then the slug")
        check(judged(t._tenant_config_candidates, plan_for(Path(tmp), project_name=SLUG), SLUG, explicit=None, recorded=None) == [conventional("p362")]
              and judged(t._tenant_config_candidates, plan_for(Path(tmp), project_name=""), SLUG, explicit=None, recorded=None) == [conventional("p362")],
              "the same name once, and an empty one skipped")


def test_the_boards_declared_roles() -> None:
    from scripts import team_launcher as t

    check(judged(t.board_declared_role_names, None) == set() and judged(t.board_declared_role_names, ["x"]) == set()
          and judged(t.board_declared_role_names, {"roles": None}) == set(), "no document, not a dict, no roles: none")
    check(judged(t.board_declared_role_names, {"roles": [{"name": " inspector "}, {"name": ""}, {"name": None}, "main", {"other": 1}, {"name": "audit"}]})
          == {"inspector", "audit"}, "named dict roles only, stripped")


def test_conflicts_in_order_and_roles_only_when_corroborated() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        plan = plan_for(Path(tmp))
        home = Path(tmp) / "home"
        check(judged(t.tenant_config_conflicts, plan, config_for(Path(tmp))) == [], "agreeing: none")
        everything = config_for(Path(tmp), project="other", run_as_user=None, ticket_prefix="OTH", board_socket=Path("/x.sock"),
                                board_url="http://127.0.0.1:1/", repository=Path("/elsewhere/repo"), session_dir=Path("/elsewhere/s"),
                                roles=[SimpleNamespace(role="zeta"), SimpleNamespace(role="alpha"), SimpleNamespace(role="main")])
        check(judged(t.tenant_config_conflicts, plan, everything) == [
            "project: the configuration says 'other', root provisioned 'p362'",
            "run_as_user: the configuration says None, root provisioned 'syrd362-owner'",
            "ticket_prefix: the configuration says 'OTH', root provisioned 'PTR'",
            "board_socket: the configuration says '/x.sock', root provisioned '/fixture/run/p362.sock'",
            "board_url: the configuration says 'http://127.0.0.1:1/', root provisioned 'port 8362'",
            f"repository: the configuration points at /elsewhere/repo, which is outside syrd362-owner's home {home}",
            f"session_dir: the configuration points at /elsewhere/s, which is outside syrd362-owner's home {home}",
            "roles: the configuration declares alpha, zeta, which root did not provision for this project and the board's declared "
            "workflow does not name either",
        ], "every disagreement, in order, roles sorted")
        check(judged(t.tenant_config_conflicts, plan, config_for(Path(tmp), repository=home, session_dir=home / "s")) == [],
              "the home itself and paths under it are inside")
        saved = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            check(judged(t.tenant_config_conflicts, plan, config_for(Path(tmp), repository=Path("~/Projects/p362"))) == [],
                  "a `~` path is expanded before it is judged")
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved
        extra = config_for(Path(tmp), roles=[SimpleNamespace(role="main"), SimpleNamespace(role="inspector")])
        check(len(judged(t.tenant_config_conflicts, plan, extra)) == 1
              and judged(t.tenant_config_conflicts, plan, extra, corroborated_roles={"inspector"}) == []
              and judged(t.tenant_config_conflicts, plan, extra, corroborated_roles=("audit",)) != [],
              "an added role counts only when the board names it")


# --- the verifier ------------------------------------------------------------------------------------------------------


class Verifier:
    """Every launcher seam the verifier reads, as fakes sharing one log."""

    def __init__(self, tmp: Path, candidates: list[Path], *, readable=None, configs=None) -> None:
        self.tmp, self.log = tmp, []
        self.candidates = candidates
        self.readable = readable if readable is not None else {c: (document({}), "") for c in candidates}
        self.configs = configs or {}

    def seams(self, **extra: object) -> dict[str, object]:
        log = self.log
        names = dict(
            recorded_tenant_config_path=lambda slug: log.append(("recorded", slug)) or Path("/fixture/rec.json"),
            registered_tenant_config_path=lambda slug, registry_dir=None: log.append(("registered", slug, registry_dir)) or Path("/fixture/reg.json"),
            _tenant_config_candidates=lambda plan, slug, **kw: log.append(("candidates", kw)) or list(self.candidates),
            expected_privileged_uid=lambda: 0,
            normalize_tenant_config_mode=lambda path, permitted_uids: log.append(("normalize", path, list(permitted_uids))) or [],
            read_plan_no_follow=lambda path, **kw: log.append(("read", path, kw)) or self.readable[path],
            load_project_config=self.load,
            read_board_declared_workflow=refuse("the default board reader"),
        )
        names.update(extra)
        return names

    def load(self, slug: str, path: Path):
        self.log.append(("load", path))
        answer = self.configs[path]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def verify(fixture: Verifier, plan, **kwargs: object):
    from scripts import team_launcher as t

    return judged(t.verified_tenant_config, plan, SLUG, **kwargs)


def test_the_verifier_normalises_before_the_strict_read_and_returns_the_first_match() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        plan = plan_for(Path(tmp))
        a, b = Path("/fixture/a.json"), Path("/fixture/b.json")
        good = config_for(Path(tmp))
        fixture = Verifier(Path(tmp), [a, b], configs={a: good, b: refuse("the second candidate")})
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan, explicit=Path("/fixture/x.json"), owner_uid=1362, registry_dir=Path("/fixture/registry"))
        check(got == (a, good, []), f"the first candidate that agrees is the answer: {got}")
        check(fixture.log[:3] == [("recorded", SLUG), ("registered", SLUG, Path("/fixture/registry")),
                                  ("candidates", {"explicit": Path("/fixture/x.json"), "recorded": Path("/fixture/rec.json"),
                                                  "registered": Path("/fixture/reg.json")})],
              f"the record, then the registry, then the candidates from all three: {fixture.log[:3]}")
        check(fixture.log[3:] == [("normalize", a, [0, 1362]), ("read", a, {"require_root_owned": False, "require_owner_uids": [0, 1362]}), ("load", a)],
              f"the mode is normalised BEFORE the strict reader, both with root then the owner: {fixture.log[3:]}")
        fixture.log.clear()
        with patched(t, **fixture.seams(expected_privileged_uid=lambda: 1362)):
            verify(fixture, plan, owner_uid=1362)
        check(fixture.log[3] == ("normalize", a, [1362]), f"one uid when they are the same: {fixture.log[3]}")
        fixture.log.clear()
        with patched(t, **fixture.seams(expected_privileged_uid=lambda: 5)):
            verify(fixture, plan)
        check(fixture.log[3] == ("normalize", a, [5]), f"no owner uid: root's alone: {fixture.log[3]}")
        fixture.log.clear()
        # 9 and 1 share a hash slot, so a set of them iterates 9 first: only a
        # sort puts them in ascending order.
        with patched(t, **fixture.seams(expected_privileged_uid=lambda: 9)):
            verify(fixture, plan, owner_uid=1)
        check(fixture.log[3] == ("normalize", a, [1, 9]) and fixture.log[4][2]["require_owner_uids"] == [1, 9],
              f"the permitted uids are sorted: {fixture.log[3:5]}")


def test_the_verifier_accumulates_what_it_could_not_use() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        plan = plan_for(Path(tmp))
        paths = [Path(f"/fixture/{n}.json") for n in ("unreadable", "exit", "oserror", "json")]
        fixture = Verifier(Path(tmp), paths, readable={paths[0]: (None, "owned by 1000"), **{p: (document({}), "") for p in paths[1:]}},
                           configs={paths[1]: SystemExit("bad config"), paths[2]: OSError("gone"),
                                    paths[3]: json.JSONDecodeError("Expecting value", "", 0)})
        with patched(t, **fixture.seams(normalize_tenant_config_mode=lambda path, permitted_uids: [f"mode of {path.name}"])):
            got = verify(fixture, plan)
        check(got == (None, None, ["mode of unreadable.json", "owned by 1000", "mode of exit.json",
                                   f"{paths[1]} is not a usable launcher configuration: bad config", "mode of oserror.json",
                                   f"{paths[2]} is not a usable launcher configuration: gone", "mode of json.json",
                                   f"{paths[3]} is not a usable launcher configuration: Expecting value: line 1 column 1 (char 0)"]),
              f"every candidate's problems, in order, and nothing registered: {got}")
        fixture = Verifier(Path(tmp), [paths[1]], configs={paths[1]: RuntimeError("not a load failure")})
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan)
        check(isinstance(got, RuntimeError), f"only the three load failures are absorbed: {got!r}")
        with patched(t, **Verifier(Path(tmp), []).seams()):
            check(verify(fixture, plan) == (None, None, []), "no candidates: nothing, and no problems")


def test_the_board_is_asked_only_when_roles_are_all_that_disagree() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        plan = plan_for(Path(tmp))
        a, b = Path("/fixture/a.json"), Path("/fixture/b.json")
        extra = config_for(Path(tmp), roles=[SimpleNamespace(role="main"), SimpleNamespace(role="inspector")])
        asked: list = []
        board = lambda answer: (lambda config: asked.append(config) or answer)
        roles_line = ("roles: the configuration declares inspector, which root did not provision for this project and the board's "
                      "declared workflow does not name either")

        fixture = Verifier(Path(tmp), [a, b], configs={a: extra, b: refuse("the second candidate")})
        with patched(t, **fixture.seams(read_board_declared_workflow=board(({"roles": [{"name": "inspector"}]}, "")))):
            got = verify(fixture, plan)
        check(got == (a, extra, []) and asked == [extra], f"by default the running board corroborates the added role: {got} {asked}")

        asked.clear()
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan, board_reader=board(({"roles": [{"name": "audit"}]}, "")))
        check(got == (None, None, [f"{a} is not the configuration root provisioned for {SLUG}:", roles_line]) and asked == [extra],
              f"a role the board does not name is still refused, and the second candidate never looked at: {got}")
        asked.clear()
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan, board_reader=board((None, "board unreachable")))
        check(got == (None, None, [f"{a} is not the configuration root provisioned for {SLUG}:", roles_line]),
              f"an unreachable board corroborates nothing: {got}")
        asked.clear()
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan, corroborate_roles=False)
        check(got == (None, None, [f"{a} is not the configuration root provisioned for {SLUG}:", roles_line]),
              f"corroboration off and no reader: refused without asking: {got}")
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan, corroborate_roles=False, board_reader=board(({"roles": [{"name": "inspector"}]}, "")))
        check(got == (a, extra, []), f"an explicit reader is used whatever the flag says: {got}")

        asked.clear()
        both = config_for(Path(tmp), ticket_prefix="OTH", roles=extra.roles)
        fixture = Verifier(Path(tmp), [a], configs={a: both})
        with patched(t, **fixture.seams()):
            got = verify(fixture, plan, board_reader=board(({"roles": [{"name": "inspector"}]}, "")))
        check(asked == [] and got == (None, None, [f"{a} is not the configuration root provisioned for {SLUG}:",
                                                   "ticket_prefix: the configuration says 'OTH', root provisioned 'PTR'", roles_line]),
              f"any other disagreement: the board is never asked, so a configuration cannot nominate its own corroborator: {got}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_nested_closure_and_the_defaults")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"tenant_config_records_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
