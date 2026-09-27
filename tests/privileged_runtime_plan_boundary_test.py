#!/usr/bin/env python3
"""SYRD-353: root's runtime baseline and plan helpers, against the launcher they came out of.

Thirteen definitions moved into `scripts/privileged_runtime_plan.py` unchanged:
the owner evidence (`_provision_owner`, `SYSTEMD_SYSTEM_UNIT_DIR`,
`_root_controlled_record`, `_unit_environment`,
`legacy_owner_from_host_records`), the no-follow ownership repair, the baseline
(`reconstruct_privileged_baseline`, `_privileged_baseline_plan`), the current
identities (`plan_for_current_identities`, `_tenant_runs_on_project_account`),
the projection (`authoritative_refresh_plan`) and the privacy pair
(`privileged_provision_privacy_problems`, `close_privileged_artifacts`). This
pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher re-exports every name, the very same objects whichever module is
  imported first, and `refresh_generated_project_runtime_artifacts` still calls
  them by those names.
- **Seams (rule 24).** Every launcher facility they use, and every name here
  another definition here calls, is read from the launcher when it runs: suites
  set `team_launcher.SYSTEMD_SYSTEM_UNIT_DIR` to a sandbox and patch
  `team_launcher._provision_owner`, and both still reach the moved code. The
  standard-library modules are the module's own, the same objects, so
  `pwd`/`os` patches reach it too. Nothing the code binds itself is read through
  the launcher (rule 27).
- **The behaviour is unchanged,** the trust boundary first. The owner comes from
  the directory's owner, or for a root-owned legacy directory from root-owned
  host records that must agree. Ownership is repaired without following a link
  and never in a dry run. The baseline is root's own copy, or one regenerated
  from root's facts to which the tenant's document adds only validated values,
  with the operator's cache exempt from the divergence refusal. The projection
  adds only canonical accounts for new process roles. A project-account tenant
  renders no per-role table. Privacy is asked of the filesystem, and only named
  regular files are closed.

Every path is under a temporary directory this test creates. The passwd
database, the effective owner of a directory and every chown are this test's own
answers. Nothing is chowned, and nothing outside the test's directories is
chmodded or written. The only host file read is `/etc/passwd`, read-only, as a
root-owned record.
"""

from __future__ import annotations

import dataclasses
import ast
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

CHECKS = 0
MOVED = ("_provision_owner", "SYSTEMD_SYSTEM_UNIT_DIR", "_root_controlled_record", "_unit_environment",
         "legacy_owner_from_host_records", "repair_legacy_provision_ownership", "reconstruct_privileged_baseline",
         "_privileged_baseline_plan", "plan_for_current_identities", "_tenant_runs_on_project_account",
         "authoritative_refresh_plan", "close_privileged_artifacts", "privileged_provision_privacy_problems")
#: Per function, the launcher names it reads when it runs and how often (measured on the SYRD-353 baseline).
SEAMS = {
    "_provision_owner": {}, "_root_controlled_record": {}, "_unit_environment": {}, "_tenant_runs_on_project_account": {},
    "legacy_owner_from_host_records": {"SYSTEMD_SYSTEM_UNIT_DIR": 1, "_root_controlled_record": 2, "_unit_environment": 1,
                                       "switchyard_registry_dir": 1},
    "repair_legacy_provision_ownership": {"_walk_no_follow": 1},
    "reconstruct_privileged_baseline": {"build_plan": 2, "_validated_role_names": 3, "_regenerated_field_divergence": 1,
                                        "_new_project_worktree_base": 1, "plan_workflow_from_root": 1, "_provision_owner": 1,
                                        "resolve_control_user": 1, "ROLE_RE": 1, "_repo_root": 1},
    "_privileged_baseline_plan": {"privileged_baseline_plan_path": 1, "reconstruct_privileged_baseline": 1,
                                  "_project_board_provision_from_json": 1},
    "plan_for_current_identities": {"_tenant_runs_on_project_account": 1},
    "authoritative_refresh_plan": {"_new_project_worktree_base": 1, "role_account_name": 1, "NON_PROCESS_ROLES": 1, "ROLE_RE": 1},
    "close_privileged_artifacts": {"privileged_artifact_mode": 1},
    "privileged_provision_privacy_problems": {"privileged_provision_dir": 1, "expected_privileged_uid": 1,
                                              "switchyard_privileged_provision_root": 1},
}
OWN = ("errno", "json", "os", "pwd", "shlex", "stat", "replace", "Path", "Any", "Iterable", "Mapping", "Sequence")
PROJECT = "p353"
OWNER = "syrd353-owner"
ME = os.getuid()


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


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


def account(name: str, uid: int, home: Path) -> SimpleNamespace:
    return SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=uid, pw_dir=str(home))


def owned_as(uid: int, *paths: Path):
    """os.stat/os.fstat that report `uid` as the owner of these paths and nothing else."""
    real_stat, real_fstat = os.stat, os.fstat
    inodes = {real_stat(p, follow_symlinks=False).st_ino for p in paths}

    def restamp(info):
        if info.st_ino not in inodes:
            return info
        fields = list(info)
        fields[4] = uid
        return os.stat_result(fields)

    return dict(stat=lambda path, *a, **k: restamp(real_stat(path, *a, **k)),
                fstat=lambda fd: restamp(real_fstat(fd)))


@dataclasses.dataclass(frozen=True)
class Plan:
    """The fields of a provisioning plan these functions read and replace."""

    project: str = PROJECT
    owner_user: str = OWNER
    role_accounts: tuple = ()
    role_worktrees: tuple = ()
    workflow: object = None
    name: str = "plan"


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.privileged_runtime_plan as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.privileged_runtime_plan", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.privileged_runtime_plan")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.privileged_runtime_plan as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: every moved name is the launcher's too, every own import the same object: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_modules_own_names_and_the_refresh_callers() -> None:
    module = ast.parse((ROOT / "scripts" / "privileged_runtime_plan.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    check(sorted(functions) == sorted(SEAMS), f"exactly the moved functions: {sorted(functions)}")
    every = {name for seams in SEAMS.values() for name in seams}
    for name, seams in SEAMS.items():
        function = functions[name]
        through = {}
        for n in ast.walk(function):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        bare = sorted({n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id in every})
        check(through == seams and not bare, f"{name} reads {seams} through the launcher, none bare: {through} {bare}")
        bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound |= {h.name for h in ast.walk(function) if isinstance(h, ast.ExceptHandler) and h.name}
        check(not bound & set(through) and not set(OWN) & set(through),
              f"{name}: nothing it binds, nor a standard-library name, is read as the launcher's")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    # SYRD-354 moved the refresh into runtime_artifact_refresh, which reads them through the launcher.
    refresher = ast.parse((ROOT / "scripts" / "runtime_artifact_refresh.py").read_text(encoding="utf-8"))
    refresh = next(n for n in refresher.body if isinstance(n, ast.FunctionDef)
                   and n.name == "refresh_generated_project_runtime_artifacts")
    called = sorted({n.func.attr for n in ast.walk(refresh) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and isinstance(n.func.value, ast.Name) and n.func.value.id == "launcher" and n.func.attr in MOVED})
    bare = sorted({n.id for n in ast.walk(refresh) if isinstance(n, ast.Name) and n.id in MOVED})
    check(called == sorted(["_provision_owner", "legacy_owner_from_host_records", "repair_legacy_provision_ownership",
                            "_privileged_baseline_plan", "plan_for_current_identities", "authoritative_refresh_plan",
                            "privileged_provision_privacy_problems", "close_privileged_artifacts"]) and not bare,
          f"the refresh command still calls them through the launcher's own (patchable) names: {called} {bare}")
    defined = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    defined |= {t.id for n in launcher.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)}
    check(not defined & set(MOVED), f"and the launcher defines none of them any more: {defined & set(MOVED)}")


def test_the_suites_patches_reach_the_moved_code() -> None:
    from scripts import privileged_runtime_plan as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd353-seam.") as raw:
        units = Path(raw) / "units"
        units.mkdir()
        read: list[Path] = []
        with patched(team_launcher, SYSTEMD_SYSTEM_UNIT_DIR=units,
                     _root_controlled_record=lambda path: read.append(path) or (None, "syrd353 stop")):
            owner, _, problem = m.legacy_owner_from_host_records(PROJECT, Path(raw) / "provision")
        check(read == [units / f"{PROJECT}-ticket-board.service"] and owner == "" and problem.endswith("syrd353 stop"),
              f"a sandboxed unit directory set on the launcher is the one read: {read}")
    with patched(team_launcher, _provision_owner=lambda d: ("", "syrd353: the launcher's owner lookup")):
        plan, problem = m.reconstruct_privileged_baseline(PROJECT, Path("/nonexistent/syrd353"), {})
    check(plan is None and problem == "syrd353: the launcher's owner lookup",
          "the owner lookup patched on the launcher is the one reconstruction asks")


# --- owner evidence ------------------------------------------------------------------------------------------------


def test_the_provision_owner_is_the_directorys_owner() -> None:
    from scripts import privileged_runtime_plan as m

    with tempfile.TemporaryDirectory(prefix="syrd353-owner.") as raw:
        directory = Path(raw)
        with patched(pwd, getpwuid=lambda uid: account("syrd353-me", uid, directory) if uid == ME else pwd.getpwuid(uid)):
            check(m._provision_owner(directory) == ("syrd353-me", ""), "the owner's account name")
        with patched(pwd, getpwuid=lambda uid: (_ for _ in ()).throw(KeyError(uid))):
            check(m._provision_owner(directory) == ("", f"{directory} is owned by uid {ME}, which is not a local account"),
                  "a uid with no account is refused")
        with patched(os, **owned_as(0, directory)):
            check(m._provision_owner(directory) == ("", f"{directory} is owned by root and so does not identify a tenant"),
                  "a root-owned directory identifies nobody")
        link = directory / "link"
        link.symlink_to(directory)
        with patched(pwd, getpwuid=lambda uid: account("syrd353-me", uid, directory)), patched(os, **owned_as(0, directory)):
            check(m._provision_owner(link) == ("syrd353-me", ""),
                  "the link itself is inspected, not followed to the root-owned directory it names")
        missing = directory / "missing"
        owner, problem = m._provision_owner(missing)
        check(owner == "" and problem.startswith(f"cannot inspect {missing} ("), f"a missing directory: {problem}")


def test_a_root_controlled_record_is_root_owned_regular_and_closed() -> None:
    from scripts import privileged_runtime_plan as m

    body, problem = m._root_controlled_record(Path("/etc/passwd"))
    check(problem == "" and body == Path("/etc/passwd").read_bytes(), "a root-owned 0644 file is read whole")
    with tempfile.TemporaryDirectory(prefix="syrd353-record.") as raw:
        mine = Path(raw) / "unit"
        mine.write_text("[Service]\n", encoding="utf-8")
        check(m._root_controlled_record(mine) == (None, f"{mine} is not owned by root"), "a file an account owns")
        link = Path(raw) / "link"
        link.symlink_to("/etc/passwd")
        body, problem = m._root_controlled_record(link)
        check(body is None and problem.startswith(f"{link} cannot be read (") and "link" in problem.lower(),
              f"a link at the record is not followed: {problem}")
        check(m._root_controlled_record(Path(raw)) == (None, f"{raw} is not a regular file"), "a directory is no record")
        mine.chmod(0o666)
        with patched(os, **owned_as(0, mine)):
            check(m._root_controlled_record(mine) == (None, f"{mine} is writable by accounts other than root"),
                  "a root-owned file others can write is no record")


def test_a_units_environment_is_every_assignment_in_order() -> None:
    from scripts import privileged_runtime_plan as m

    found = m._unit_environment('[Service]\nEnvironment=HOME=/h "A=b c" NOVALUE\n  Environment=HOME=/i\nEnvironmentFile=x\n')
    check(found == {"HOME": ["/h", "/i"], "A": ["b c"]}, f"quoted, repeated, and only `Environment=` lines: {found}")


class HostRecords:
    """A legacy tenant's root-controlled records, as this test serves them through the launcher's reader."""

    def __init__(self, raw: str, *, unit: str | None = None, registry: object = "ok", board_uid: int | None = None) -> None:
        self.base = Path(raw)
        self.home = self.base / "home" / OWNER
        self.provision = self.home / "checkout" / ".switchyard" / "provision"
        self.provision.mkdir(parents=True)
        self.units, self.registry = self.base / "units", self.base / "registry"
        self.unit = (f"[Service]\nEnvironment=TICKET_BOARD_TENANT_USER={OWNER}\nEnvironment=HOME={self.home}\n"
                     if unit is None else unit)
        config_path = self.provision / f"{PROJECT}.json"
        self.record = ({"slug": PROJECT, "config_path": str(config_path)} if registry == "ok" else registry)
        if board_uid is not None:
            (self.home / f"{PROJECT}-ticketboard-live").mkdir()
        self.board_uid = board_uid
        self.passwd = [account(OWNER, ME, self.home), account("syrd353-other", ME + 1, self.base / "other")]

    def read(self, path: Path):
        import json
        if path == self.units / f"{PROJECT}-ticket-board.service":
            return self.unit.encode(), ""
        if path == self.registry / f"{PROJECT}.json":
            return (json.dumps(self.record).encode() if isinstance(self.record, dict) else self.record), ""
        return None, f"syrd353 has no record {path}"

    def owner(self):
        from scripts import privileged_runtime_plan as m, team_launcher

        names = {a.pw_name: a for a in self.passwd}
        board = self.home / f"{PROJECT}-ticketboard-live"
        stats = owned_as(self.board_uid, board) if self.board_uid is not None and self.board_uid != ME else {}
        with patched(team_launcher, SYSTEMD_SYSTEM_UNIT_DIR=self.units, _root_controlled_record=self.read), \
                patched(pwd, getpwall=lambda: list(self.passwd),
                        getpwnam=lambda n: names[n] if n in names else (_ for _ in ()).throw(KeyError(n))), \
                patched(os, **stats):
            return m.legacy_owner_from_host_records(PROJECT, self.provision, registry_dir=self.registry)


def test_a_legacy_owner_is_established_from_agreeing_root_records() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd353-legacy.") as raw:
        records = HostRecords(raw, board_uid=ME)
        owner, evidence, problem = records.owner()
        unit = records.units / f"{PROJECT}-ticket-board.service"
        check(owner == OWNER and problem == "" and evidence == [
            f"{unit} HOME -> {OWNER}", f"{unit} TICKET_BOARD_TENANT_USER -> {OWNER}",
            f"{records.registry / (PROJECT + '.json')} registers {records.provision / (PROJECT + '.json')}, inside {OWNER}'s home",
            f"{records.home / (PROJECT + '-ticketboard-live')} is owned by {OWNER}"],
              f"the unit's user and home, the registry and the board root all say {OWNER}: {evidence} {problem}")


def test_each_legacy_owner_refusal() -> None:
    cases = [
        (dict(unit="[Service]\nEnvironment=TICKET_BOARD_TENANT_USER=a TICKET_BOARD_TENANT_USER=b\n"), "names more than one tenant user"),
        (dict(unit="[Service]\nEnvironment=HOME=/a HOME=/b\n"), "sets more than one HOME"),
        (dict(unit="[Service]\nEnvironment=HOME=/nonexistent/syrd353/nobody\n"), "which is the home of no account"),
        (dict(unit="[Service]\nEnvironment=LANG=C\n"), "names neither a tenant user nor a HOME"),
        (dict(unit=f"[Service]\nEnvironment=TICKET_BOARD_TENANT_USER=syrd353-other HOME={{home}}\n"), "is contradictory"),
        (dict(unit="[Service]\nEnvironment=TICKET_BOARD_TENANT_USER=syrd353-ghost\n"), "is not a local account"),
        (dict(registry=b"not json"), "is not a registry record"),
        (dict(registry={"slug": "other", "config_path": "/x/p.json"}), f"does not register {PROJECT} at an absolute path"),
        (dict(registry={"slug": PROJECT, "config_path": "/nonexistent/syrd353/elsewhere/p353.json"}), "registers /nonexistent/syrd353/elsewhere, not"),
        (dict(board_uid=ME + 1), f"is owned by uid {ME + 1}, not by {OWNER}"),
    ]
    for kwargs, expected in cases:
        with tempfile.TemporaryDirectory(prefix="syrd353-refuse.") as raw:
            if "unit" in kwargs:
                kwargs = dict(kwargs, unit=kwargs["unit"].replace("{home}", str(Path(raw) / "home" / OWNER)))
            records = HostRecords(raw, **kwargs)
            owner, _, problem = records.owner()
            check(owner == "" and expected in problem, f"{expected}: refused, no owner: {problem}")
    with tempfile.TemporaryDirectory(prefix="syrd353-refuse.") as raw:
        records = HostRecords(raw, unit=f"[Service]\nEnvironment=HOME={Path(raw) / 'home' / OWNER}\n")
        records.passwd.append(account("syrd353-twin", ME + 2, records.home))
        problem = records.owner()[2]
        check(problem.endswith(f"which is the home of 2 accounts ({OWNER}, syrd353-twin)"), f"a shared HOME names nobody: {problem}")
    with tempfile.TemporaryDirectory(prefix="syrd353-refuse.") as raw:
        records = HostRecords(raw)
        outside = Path(raw) / "elsewhere" / "provision"
        outside.mkdir(parents=True)
        records.provision = outside
        records.record = {"slug": PROJECT, "config_path": str(outside / f"{PROJECT}.json")}
        problem = records.owner()[2]
        check(problem.endswith(f"which is not inside {OWNER}'s home {records.home.resolve()}"),
              f"a registration outside the owner's home: {problem}")
    with tempfile.TemporaryDirectory(prefix="syrd353-refuse.") as raw:
        records = HostRecords(raw)
        records.passwd[0] = account(OWNER, 0, records.home)
        check(records.owner()[2].endswith("names root as the tenant owner"), "root is never a tenant owner")
    with tempfile.TemporaryDirectory(prefix="syrd353-refuse.") as raw:
        records = HostRecords(raw, registry={"slug": PROJECT, "config_path": str(Path(raw) / "home" / OWNER / "p353.json")})
        records.provision = records.home
        check("which is not inside" not in records.owner()[2], "a registration at the home itself is inside it")
    with tempfile.TemporaryDirectory(prefix="syrd353-refuse.") as raw:
        records = HostRecords(raw)
        records.unit = None
        records.read = lambda path: (None, "syrd353 unreadable")
        owner, evidence, problem = records.owner()
        check(owner == "" and evidence == [] and problem == "its board unit is not a root-controlled record: syrd353 unreadable",
              f"no unit record: refused first: {problem}")


# --- ownership repair ----------------------------------------------------------------------------------------------


def repair(directory: Path, names, *, dry_run: bool, root_owned: tuple = ()):
    from scripts import privileged_runtime_plan as m

    chowned: list[tuple] = []
    fake = account(OWNER, 4353, directory)
    with patched(pwd, getpwnam=lambda n: fake if n == OWNER else (_ for _ in ()).throw(KeyError(n))), \
            patched(os, fchown=lambda fd, uid, gid: chowned.append((os.readlink(f"/proc/self/fd/{fd}"), uid, gid)),
                    **(owned_as(0, *root_owned) if root_owned else {})):
        result = m.repair_legacy_provision_ownership(directory, OWNER, names, dry_run=dry_run)
    return result, chowned


def test_ownership_repair_is_bounded_and_never_follows() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd353-repair.") as raw:
        directory = Path(raw) / "provision"
        directory.mkdir()
        for name in ("plan.json", "layout.json"):
            (directory / name).write_text("{}", encoding="utf-8")
        (directory / "operator.sh").write_text("", encoding="utf-8")
        (changes, problem), chowned = repair(directory, ["plan.json"], dry_run=False)
        check(changes == [] and problem == f"{directory} is not root-owned; nothing to repair" and chowned == [],
              "a directory root does not own is left alone")
        (changes, problem), chowned = repair(directory, ["layout.json", "plan.json", "plan.json", "absent.json"], dry_run=True,
                                             root_owned=(directory,))
        check(problem == "" and changes == [f"{directory}/ -> {OWNER}", f"{directory / 'layout.json'} -> {OWNER}",
                                            f"{directory / 'plan.json'} -> {OWNER}"] and chowned == [],
              f"a dry run lists the directory and each named file once, sorted, missing ones skipped, and chowns nothing: {changes}")
        (changes, problem), chowned = repair(directory, ["plan.json", "layout.json"], dry_run=False, root_owned=(directory,))
        check(problem == "" and chowned == [(str(directory), 4353, 4353), (str(directory / "layout.json"), 4353, 4353),
                                             (str(directory / "plan.json"), 4353, 4353)],
              f"applied: the directory, then only the named files, to the owner's uid and gid: {chowned}")
        (directory / "linked").symlink_to(directory / "plan.json")
        os.link(directory / "layout.json", directory / "hard")
        for names, expected in ((["linked"], f"refusing to repair {directory / 'linked'}: it is a symbolic link"),
                                (["layout.json"], f"refusing to repair {directory / 'layout.json'}: not a regular file with one link"),
                                (["a/b"], f"refusing to repair {directory}: 'a/b' is not a file name"),
                                ([".."], f"refusing to repair {directory}: '..' is not a file name")):
            (changes, problem), chowned = repair(directory, names, dry_run=False, root_owned=(directory,))
            check(changes == [] and problem == expected and chowned == [], f"{names}: {problem}")
        link = Path(raw) / "provision-link"
        link.symlink_to(directory)
        (changes, problem), chowned = repair(link, ["plan.json"], dry_run=False, root_owned=(directory,))
        check(changes == [] and problem.startswith(f"refusing to repair {link}: ") and chowned == [],
              f"a linked provision directory is refused by the no-follow walk: {problem}")


# --- the baseline --------------------------------------------------------------------------------------------------


class Baseline:
    """The launcher facilities reconstruction uses, answering from this test's objects, into one log."""

    def __init__(self, *, diverged: list | None = None, workflow_problem: str = "", build_error: str = "") -> None:
        self.diverged, self.workflow_problem, self.build_error = diverged or [], workflow_problem, build_error
        self.log: list[tuple] = []

    def names(self) -> dict[str, object]:
        L = self.log

        def build(**kw):
            L.append(("build", kw))
            if self.build_error and "port" in kw:
                raise SystemExit(self.build_error)
            return Plan(role_accounts=(("main", "p353-main"), ("ops", "p353-ops")), name="built" if "port" in kw else "reference")

        return dict(
            build_plan=build,
            resolve_control_user=lambda project, *, owner_user: L.append(("control", project, owner_user)) or "syrd353-ctl",
            _repo_root=lambda: L.append(("repo-root",)) or Path("/nonexistent/syrd353/repo"),
            _regenerated_field_divergence=lambda plan, data, *, skip=(): L.append(("diverged?", plan, data, skip)) or list(self.diverged),
            plan_workflow_from_root=lambda plan, *, declares_workflow: L.append(("workflow", plan, declares_workflow))
                or (dataclasses.replace(plan, name="with-workflow"), self.workflow_problem),
        )


def reconstruct(base: Baseline, data: dict, *, established_owner: str = "", home: object = "/nonexistent/syrd353/home",
                **kw):
    from scripts import privileged_runtime_plan as m, team_launcher

    entry = account(OWNER, 4353, Path(str(home)))
    with patched(team_launcher, **base.names(), _provision_owner=lambda d: base.log.append(("owner?", d)) or (OWNER, "")), \
            patched(pwd, getpwnam=lambda n: entry if n == OWNER else (_ for _ in ()).throw(KeyError(n))):
        return m.reconstruct_privileged_baseline(PROJECT, Path("/nonexistent/syrd353/provision"), data,
                                                 established_owner=established_owner, **kw)


def test_reconstruction_takes_only_validated_values_from_the_tenant() -> None:
    base = Baseline()
    data = {"port": 8353, "board_service_traversal": False, "implementer_roles": ["Main"], "audit_roles": ["audit"],
            "draft_roles": ["design"], "vcs_close_role": "OPS", "project_name": " P353 ", "ticket_prefix": " SY ",
            "owner_user": "attacker", "owner_home": "/attacker", "role_accounts": [["main", "root"]], "workflow": {"x": 1}}
    plan, problem = reconstruct(base, data, source_repo=Path("/nonexistent/syrd353/src/../src"))
    builds = [e[1] for e in base.log if e[0] == "build"]
    check(problem == "" and plan.name == "with-workflow" and base.log[0] == ("owner?", Path("/nonexistent/syrd353/provision")),
          f"the owner from the directory, then a plan: {base.log[:2]} {problem}")
    check(builds[0] == dict(project=PROJECT, owner_user=OWNER, owner_home=Path("/nonexistent/syrd353/home"),
                            control_user="syrd353-ctl", source_repo=Path("/nonexistent/syrd353/src")),
          f"the reference plan is built from root's facts and the normalised source: {builds[0]}")
    check(builds[1] == dict(project=PROJECT, project_name="P353", owner_user=OWNER, owner_home=Path("/nonexistent/syrd353/home"),
                            port=8353, source_repo=Path("/nonexistent/syrd353/src"), ticket_prefix="SY",
                            implementer_roles=("main",), include_designer=True, include_audit=True, audit_roles=("audit",),
                            board_service_traversal=False, vcs_close_role="ops"),
          f"the tenant adds only the validated display name, port, prefix, traversal and role names: {builds[1]}")
    workflow = next(e for e in base.log if e[0] == "workflow")
    check(workflow[1].name == "built" and workflow[1].role_worktrees == plan.role_worktrees == (
        ("main", f"/home/{OWNER}/{PROJECT}-worktrees/main"), ("ops", f"/home/{OWNER}/{PROJECT}-worktrees/ops"))
          and workflow[2] is True,
          "a worktree per regenerated role account under the owner's worktree base, then root's workflow")
    check(next(e for e in base.log if e[0] == "diverged?")[3] == (), "no operator cache: every regenerated field is judged")


def test_reconstruction_refusals_and_the_operator_cache_exception() -> None:
    base = Baseline()
    _, problem = reconstruct(base, {"port": True, "board_service_traversal": "yes", "vcs_close_role": "Bad Role!",
                                    "implementer_roles": "main"})
    check(problem == (f"switchyard: cannot establish a root-owned baseline for {PROJECT} from "
                      f"/nonexistent/syrd353/provision/plan.json: port True is not a usable unprivileged port; "
                      "board_service_traversal 'yes' is not a boolean; implementer_roles is not a list; "
                      "vcs_close_role 'bad role!' is not a role name. Re-provision the project so root generates its own.")
          and [e[0] for e in base.log].count("build") == 1, f"every objection, and no plan built from them: {problem}")
    for port in (80, 65536, "8353", None):
        _, problem = reconstruct(Baseline(), {"port": port})
        check("is not a usable unprivileged port" in problem, f"port {port!r} refused")
    _, problem = reconstruct(Baseline(build_error="syrd353 bad plan"), {"port": 8353})
    check(problem == (f"switchyard: cannot establish a root-owned baseline for {PROJECT}: syrd353 bad plan. "
                      "Re-provision the project so root generates its own."), f"a plan the builder refuses: {problem}")
    base = Baseline(diverged=["commit_git_dir: provisioned 'a', regenerated 'b'", "port: x"])
    _, problem = reconstruct(base, {"port": 8353}, operator_commit_git_dir=True)
    check(next(e for e in base.log if e[0] == "diverged?")[3] == ("commit_git_dir",) and problem.endswith(
        "\n  commit_git_dir: provisioned 'a', regenerated 'b'\n  port: x\nRe-provision the project so root generates its own baseline.")
          and "would change what runs:" in problem, f"the operator's cache is exempt; any divergence refuses: {problem}")
    _, problem = reconstruct(Baseline(workflow_problem="syrd353 no workflow"), {"port": 8353})
    check(problem == "switchyard: syrd353 no workflow", "a workflow root cannot vouch for refuses")
    base = Baseline()
    plan, problem = reconstruct(base, {"port": 8353}, established_owner=OWNER)
    check(problem == "" and "owner?" not in [e[0] for e in base.log], "an established owner is used as given")
    _, problem = reconstruct(Baseline(), {}, established_owner="syrd353-ghost")
    check(problem == "syrd353-ghost has no passwd entry, so its home cannot be established", problem)
    _, problem = reconstruct(Baseline(), {}, home="relative/home")
    check(problem == f"the passwd entry for {OWNER} has no absolute home", problem)
    base = Baseline()
    reconstruct(base, {"port": 8353})
    check(next(e for e in base.log if e[0] == "workflow")[2] is False, "a tenant that declares no workflow says so")


def test_roots_own_baseline_wins_and_is_judged_as_first_read() -> None:
    from scripts import privileged_runtime_plan as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd353-base.") as raw:
        stored = Path(raw) / "plan.json"
        stored.write_text("{}", encoding="utf-8")
        log: list[tuple] = []
        config = SimpleNamespace(project=PROJECT)
        tenant_data = {"port": 8353}

        def load(path, *, supplied):
            log.append(("load", path, supplied))
            return Plan(name="stored")

        def rebuild(project, provision_dir, data, **kw):
            log.append(("reconstruct", project, provision_dir, data, kw))
            return Plan(name="rebuilt"), ""

        names = dict(privileged_baseline_plan_path=lambda project: log.append(("path?", project)) or stored,
                     _project_board_provision_from_json=load, reconstruct_privileged_baseline=rebuild)
        with patched(team_launcher, **names):
            plan, problem = m._privileged_baseline_plan(config, Path(raw), tenant_data, supplied={"commit_git_dir": "c"})
        check(plan.name == "stored" and problem == "" and log == [("path?", PROJECT), ("load", stored, {"commit_git_dir": "c"})],
              f"root's stored copy, with what the operator supplied: {log}")

        def refuse(path, *, supplied):
            raise SystemExit("syrd353 corrupt")

        with patched(team_launcher, **dict(names, _project_board_provision_from_json=refuse)):
            plan, problem = m._privileged_baseline_plan(config, Path(raw), tenant_data)
        check(plan is None and problem == f"switchyard: {PROJECT} root-owned runtime baseline {stored} is unusable: syrd353 corrupt",
              f"a corrupt stored copy refuses rather than reconstructing: {problem}")
        stored.unlink()
        log.clear()
        with patched(team_launcher, **names):
            plan, problem = m._privileged_baseline_plan(config, Path(raw), tenant_data, source_repo=Path("/s"),
                                                         operator_commit_git_dir=True, established_owner=OWNER)
        check(plan.name == "rebuilt" and log[-1][:3] == ("reconstruct", PROJECT, Path(raw)) and log[-1][3] is tenant_data
              and log[-1][4] == dict(source_repo=Path("/s"), operator_commit_git_dir=True, established_owner=OWNER),
              f"no stored copy: reconstructed, through the launcher, from the document as first read: {log[-1]}")


# --- identities and the projection --------------------------------------------------------------------------------


def test_a_project_account_tenant_renders_no_per_role_table() -> None:
    from scripts import privileged_runtime_plan as m

    role = lambda account: SimpleNamespace(run_as_user=account)
    crossed = SimpleNamespace(role_state_isolation=True, roles=[role(OWNER), role(""), role(f" {OWNER} ")], run_as_user=f" {OWNER}")
    check(m._tenant_runs_on_project_account(crossed), "flagged, and every role on the project account")
    for label, config in (("not flagged", SimpleNamespace(role_state_isolation=False, roles=crossed.roles, run_as_user=OWNER)),
                          ("no roles", SimpleNamespace(role_state_isolation=True, roles=[], run_as_user=OWNER)),
                          ("no owner", SimpleNamespace(role_state_isolation=True, roles=crossed.roles, run_as_user=" ")),
                          ("a role elsewhere", SimpleNamespace(role_state_isolation=True, roles=[role(OWNER), role("p353-ops")], run_as_user=OWNER))):
        check(not m._tenant_runs_on_project_account(config), f"{label}: not crossed")
    plan = Plan(role_accounts=(("main", "p353-main"),))
    check(m.plan_for_current_identities(plan, crossed) == dataclasses.replace(plan, role_accounts=()), "crossed: the table emptied")
    check(m.plan_for_current_identities(plan, SimpleNamespace(role_state_isolation=False, roles=[], run_as_user="")) is plan,
          "not crossed: the plan itself")
    empty = Plan()
    check(m.plan_for_current_identities(empty, crossed) is empty, "an empty table is returned as it is")


def test_the_projection_adds_only_canonical_accounts_for_new_process_roles() -> None:
    from scripts import privileged_runtime_plan as m, team_launcher

    base = Plan(role_accounts=(("main", "p353-main"),), role_worktrees=(("main", "/w/main"),))
    entries = [["main", "p353-main"], ["main", "root"], ["design", team_launcher.role_account_name(PROJECT, "design")],
               ["audit", "root"], ["Bad Role", team_launcher.role_account_name(PROJECT, "bad role")],
               ["user", team_launcher.role_account_name(PROJECT, "user")], "main", ["a", "b", "c"]]
    plan, added, refused = m.authoritative_refresh_plan(base, {"role_accounts": entries})
    check(added == ["design"] and plan.role_accounts == (("main", "p353-main"), ("design", team_launcher.role_account_name(PROJECT, "design")))
          and plan.role_worktrees == (("main", "/w/main"), ("design", f"/home/{OWNER}/{PROJECT}-worktrees/design")),
          f"one new canonical process role, with its worktree: {plan}")
    check(refused == ["main=root", "audit=root", f"Bad Role={team_launcher.role_account_name(PROJECT, 'bad role')}",
                      f"user={team_launcher.role_account_name(PROJECT, 'user')}", "'main'", "['a', 'b', 'c']"],
          f"a changed known account, a non-canonical one, a bad name, a non-process role and malformed entries: {refused}")
    for data in ({}, {"role_accounts": "main"}, {"role_accounts": [["main", "p353-main"]]}):
        plan, added, refused = m.authoritative_refresh_plan(base, data)
        check(plan is base and added == [], f"{data}: nothing crosses, the baseline itself")
    unworked = Plan(role_accounts=(("main", "p353-main"),))
    plan, _, _ = m.authoritative_refresh_plan(unworked, {"role_accounts": [["design", team_launcher.role_account_name(PROJECT, "design")]]})
    check(plan.role_worktrees == (), "no worktrees recorded: none invented")


# --- privacy -------------------------------------------------------------------------------------------------------


def test_privacy_is_asked_of_the_filesystem() -> None:
    from scripts import privileged_runtime_plan as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd353-privacy.") as raw:
        root = Path(raw)
        target = root / PROJECT
        check(m.privileged_provision_privacy_problems(PROJECT, root=root) == [], "nothing there, nothing given away")
        target.mkdir(mode=0o700)
        with patched(team_launcher, expected_privileged_uid=lambda: ME):
            check(m.privileged_provision_privacy_problems(PROJECT, root=root) == [], "private and root's (here: the fixture's)")
            target.chmod(0o755)
            check(m.privileged_provision_privacy_problems(PROJECT, root=root) == [
                f"{target} is mode 0755, so {PROJECT}'s plan, operator packet, SQL and publication record are readable beyond root"],
                  "a directory others can read")
        with patched(team_launcher, expected_privileged_uid=lambda: 0):
            check(m.privileged_provision_privacy_problems(PROJECT, root=root)[-1] == f"{target} is owned by uid {ME} rather than by root",
                  "an owner other than root")
        with patched(team_launcher, switchyard_privileged_provision_root=lambda: root, expected_privileged_uid=lambda: ME):
            check(len(m.privileged_provision_privacy_problems(PROJECT)) == 1, "the provision root through the launcher when none is given")
        target.chmod(0o700)
        target.rename(root / "real")
        target.symlink_to(root / "real")
        check(m.privileged_provision_privacy_problems(PROJECT, root=root) == [f"{target} is not a directory root owns"],
              "a link is not a directory root owns")


def test_only_named_regular_artifacts_are_closed() -> None:
    from scripts import privileged_runtime_plan as m, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd353-close.") as raw:
        target = Path(raw)
        for name, mode in (("plan.json", 0o644), ("board.sql", 0o600), ("operator.sh", 0o777)):
            (target / name).write_text("", encoding="utf-8")
            (target / name).chmod(mode)
        (target / "dir").mkdir(mode=0o755)
        (target / "link").symlink_to(target / "operator.sh")
        with patched(team_launcher, privileged_artifact_mode=lambda name: 0o600):
            said = m.close_privileged_artifacts(target, ["plan.json", "board.sql", "link", "dir", "absent"])
            again = m.close_privileged_artifacts(target, ["plan.json", "board.sql"])
        modes = {n: stat.S_IMODE((target / n).lstat().st_mode) for n in ("plan.json", "board.sql", "operator.sh", "dir")}
        check(said == [f"closed 1 artifact(s) in {target}: plan.json"] and again == []
              and modes == {"plan.json": 0o600, "board.sql": 0o600, "operator.sh": 0o777, "dir": 0o755},
              f"only named regular files not already at their mode; links, directories and unnamed files untouched: {said} {modes}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_modules_own_names_and_the_refresh_callers")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"privileged_runtime_plan_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
