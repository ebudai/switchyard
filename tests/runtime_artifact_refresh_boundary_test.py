#!/usr/bin/env python3
"""SYRD-354: refreshing a tenant's generated runtime artifacts, against the launcher it came out of.

`refresh_generated_project_runtime_artifacts` and its four private helpers
(`_plan_replacements`, `_tenant_copy_is_current`, `installed_controller`,
`_path_containment_error`) moved into `scripts/runtime_artifact_refresh.py`
unchanged. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher re-exports every name, the very same objects whichever module is
  imported first, so the upgrade's artifacts phase and the role-identity cutover
  -- which call it through the launcher -- reach the same command. Its `runner`
  and `print_func` defaults are `subprocess.run` and `print` themselves, and
  every result is the launcher's own `LauncherUpgradeResult`.
- **Seams (rule 24).** Every launcher facility it uses, and each helper here the
  command calls, is read from the launcher when it runs. The nested
  current-identities helper reads it through the command's own call-time import.
  Nothing the command binds is read through the launcher (rule 27).
- **The behaviour is unchanged,** the trust boundary first:
  - the tenant's plan is read before anything decides anything, from a path made
    absolute but never resolved;
  - the tenant's document as found is what root judges;
  - the controller comes from the installed grant;
  - a tampered plan's containment refusal is named by field;
  - a legacy root-owned directory is returned on root-controlled evidence only,
    and never in a dry run;
  - a linked tenant copy is never current;
  - a non-root run reports and stops;
  - as root, the baseline, the authorized projection, replacements and checkout
    come first, then the privileged comparison; privacy is repaired even when
    nothing else changed, and never in a dry run; an install failure is reported
    with the previous copy untouched.

Every facility is this test's own recording fake, installed on the launcher
before the command runs, and the effective uid is a patched answer. Tenant
copies are files in a temporary directory this test creates. No plan, grant,
unit, root directory, chown, install or publication is real.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("_plan_replacements", "_tenant_copy_is_current", "installed_controller", "_path_containment_error",
         "refresh_generated_project_runtime_artifacts")
#: Per function, the launcher names it reads when it runs and how often (measured on the SYRD-354 baseline).
SEAMS = {
    "_plan_replacements": {}, "_tenant_copy_is_current": {}, "_path_containment_error": {},
    "installed_controller": {"resolve_control_user": 1},
    "refresh_generated_project_runtime_artifacts": {
        "LauncherUpgradeResult": 13, "read_tenant_document_no_follow": 1, "_plan_replacements": 1,
        "_project_board_provision_from_json": 1, "plan_for_current_identities": 1, "installed_controller": 2,
        "render_privileged_artifacts": 2, "plan_with_tenant_checkout": 2, "_path_containment_error": 1,
        "privileged_baseline_plan_path": 1, "_provision_owner": 1, "legacy_owner_from_host_records": 1,
        "repair_legacy_provision_ownership": 1, "_tenant_copy_is_current": 1, "publish_tenant_artifact": 1,
        "privileged_provision_dir": 1, "switchyard_privileged_provision_root": 1, "_privileged_baseline_plan": 1,
        "authoritative_refresh_plan": 1, "privileged_provision_privacy_problems": 1,
        "ensure_privileged_provision_dir": 1, "close_privileged_artifacts": 1, "install_privileged_artifacts": 1},
}
OWN = ("os", "subprocess", "replace", "Path", "Any", "Callable")
PROJECT = "p354"
OWNER = "syrd354-owner"
ROOT_OWNED = f"is owned by root and so does not identify a tenant"


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


@dataclasses.dataclass(frozen=True)
class Plan:
    """The fields of a provisioning plan the refresh reads and replaces."""

    project: str = PROJECT
    owner_user: str = OWNER
    owner_home: str = f"/home/{OWNER}"
    control_user: str = ""
    source_repo: str = "/nonexistent/syrd354/plan-source"
    commit_git_dir: str = "plan-cache"
    name: str = "tenant"
    marks: tuple = ()


def mark(plan: Plan, label: str) -> Plan:
    return dataclasses.replace(plan, marks=(*plan.marks, label))


class Refresh:
    """The refresh's launcher facilities and effective uid, answering from this test's objects, into one log."""

    def __init__(self, raw: str, *, euid: int = 1000, document: object = "default", read_problem: str = "",
                 parse_error: str = "", migrated: tuple = (), containment: str = "", baseline_stored: bool = False,
                 dir_owner_problem: str = "", owner: str = OWNER, owner_problem: str = "", repair_problem: str = "",
                 publish_problem: str = "", target_exists: bool = True, baseline: object = "baseline",
                 baseline_reason: str = "syrd354: no baseline", added: tuple = (), refused: tuple = (),
                 privacy: tuple = (), ensured: tuple = (), closed: tuple = (), install_error: Exception | None = None,
                 tenant_files: dict | None = None, rendered: dict | None = None, privileged: dict | None = None) -> None:
        self.base = Path(raw)
        self.provision = self.base / "checkout" / ".switchyard" / "provision"
        self.provision.mkdir(parents=True)
        self.config_path = self.provision / f"{PROJECT}.json"
        self.target = self.base / "root" / PROJECT
        if target_exists:
            self.target.mkdir(parents=True)
        self.euid = euid
        self.document = {"port": 8354, "role_accounts": [["design", "p354-design"]]} if document == "default" else document
        self.read_problem, self.parse_error, self.migrated = read_problem, parse_error, migrated
        self.containment, self.baseline_stored = containment, baseline_stored
        if baseline_stored:
            (self.base / "stored-plan.json").write_text("{}", encoding="utf-8")
        self.dir_owner_problem, self.owner, self.owner_problem = dir_owner_problem, owner, owner_problem
        self.repair_problem, self.publish_problem = repair_problem, publish_problem
        self.baseline = Plan(name="baseline") if baseline == "baseline" else baseline
        self.baseline_reason, self.added, self.refused = baseline_reason, list(added), list(refused)
        self.privacy, self.ensured, self.closed, self.install_error = list(privacy), list(ensured), list(closed), install_error
        self.rendered = rendered if rendered is not None else {"plan.json": b"tenant plan", "layout.json": b"tenant layout"}
        self.privileged = privileged if privileged is not None else {"unit": b"root unit", "grant": b"root grant"}
        for name, body in (tenant_files or {}).items():
            (self.provision / name).write_bytes(body) if isinstance(body, bytes) else (self.provision / name).symlink_to(body)
        self.log: list[tuple] = []
        self.said: list[str] = []

    def names(self) -> dict[str, object]:
        from scripts.ticket_board.project_provision import PathContainmentError
        L = self.log

        def read(path, *, what):
            L.append(("read", path, what))
            if self.read_problem:
                return None, self.read_problem.format(path=path)
            return self.document, ""

        def parse(path, *, migrated, supplied, document):
            L.append(("parse", path, dict(supplied), document))
            if self.parse_error:
                raise SystemExit(self.parse_error)
            migrated.extend(self.migrated)
            return Plan()

        def render(plan, **kw):
            L.append(("render", plan, kw))
            if self.containment and kw.get("enable_owner_linger") is False:
                raise PathContainmentError(self.containment)
            return dict(self.rendered) if kw.get("enable_owner_linger") is False else dict(self.privileged)

        def install(plan, rendered):
            L.append(("install", plan, rendered))
            if self.install_error:
                raise self.install_error
            return self.target

        def baseline_plan(config, provision_dir, tenant_data, **kw):
            L.append(("baseline", provision_dir, tenant_data, kw))
            return (self.baseline, "") if self.baseline is not None else (None, self.baseline_reason)

        return dict(
            read_tenant_document_no_follow=read,
            _project_board_provision_from_json=parse,
            render_privileged_artifacts=render,
            plan_with_tenant_checkout=lambda plan, *, config_path: L.append(("checkout", config_path)) or mark(plan, "checkout"),
            plan_for_current_identities=lambda plan, config: L.append(("identities", config)) or mark(plan, "identities"),
            resolve_control_user=lambda project, *, owner_user: L.append(("grant", project, owner_user)) or f"ctl-of-{owner_user}",
            privileged_baseline_plan_path=lambda project: L.append(("baseline-path?", project))
                or (self.base / ("stored-plan.json" if self.baseline_stored else "no-plan.json")),
            _provision_owner=lambda directory: L.append(("dir-owner?", directory)) or ("", self.dir_owner_problem),
            legacy_owner_from_host_records=lambda project, directory: L.append(("legacy-owner", project, directory))
                or (self.owner if not self.owner_problem else "", ["unit says so", "registry says so"], self.owner_problem),
            repair_legacy_provision_ownership=lambda directory, owner, names, *, dry_run:
                L.append(("repair", directory, owner, list(names), dry_run))
                or (([] if self.repair_problem else [f"{directory}/ -> {owner}"]), self.repair_problem),
            publish_tenant_artifact=lambda config, directory, name, body: L.append(("publish", name, body))
                or ((False, self.publish_problem) if self.publish_problem else (True, "")),
            privileged_provision_dir=lambda project, *, root: L.append(("target", project, root)) or self.target,
            switchyard_privileged_provision_root=lambda: L.append(("root?",)) or self.base / "root",
            _privileged_baseline_plan=baseline_plan,
            authoritative_refresh_plan=lambda baseline, tenant_data: L.append(("project", baseline, tenant_data))
                or (mark(baseline, "projected"), list(self.added), list(self.refused)),
            privileged_provision_privacy_problems=lambda project: L.append(("privacy?", project)) or list(self.privacy),
            ensure_privileged_provision_dir=lambda target: L.append(("ensure", target)) or list(self.ensured),
            close_privileged_artifacts=lambda target, rendered: L.append(("close", target, sorted(rendered))) or list(self.closed),
            install_privileged_artifacts=install,
        )

    def geteuid(self) -> int:
        self.log.append(("euid?",))
        return self.euid

    def run(self, *, dry_run: bool = False, source_repo: object = None, commit_git_dir: object = None,
            config_path: Path | None = None):
        from scripts import runtime_artifact_refresh as m, team_launcher

        config = SimpleNamespace(project=PROJECT, name="config")
        self.config = config
        with patched(team_launcher, **self.names()), patched(os, geteuid=self.geteuid):
            return m.refresh_generated_project_runtime_artifacts(
                config, config_path=config_path or self.config_path, dry_run=dry_run, source_repo=source_repo,
                commit_git_dir=commit_git_dir, runner=refusing_runner, print_func=self.said.append)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]

    def entry(self, kind: str, nth: int = 0) -> tuple:
        return [e for e in self.log if e[0] == kind][nth]


def refusing_runner(argv, **kwargs):
    raise AssertionError(f"the refresh runs nothing itself: {argv}")


def result_of(result) -> tuple:
    from scripts import team_launcher
    assert type(result) is team_launcher.LauncherUpgradeResult, type(result)
    return tuple(dataclasses.astuple(result)) if dataclasses.is_dataclass(result) else tuple(result)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.runtime_artifact_refresh as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects_and_defaults() -> None:
    for order in (("scripts.runtime_artifact_refresh", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.runtime_artifact_refresh")):
        result = python("import importlib, subprocess, builtins; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.runtime_artifact_refresh as m; "
                        "k = m.refresh_generated_project_runtime_artifacts.__kwdefaults__; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        f"all(getattr(t, n) is getattr(m, n) for n in {OWN!r}), "
                        "k['runner'] is subprocess.run, k['print_func'] is builtins.print)")
        check(result.stdout.strip() == "True True True True",
              f"{' then '.join(order)}: every name the launcher's, every own import the same object, the defaults "
              f"the very objects: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_modules_own_names_and_the_callers() -> None:
    module = ast.parse((ROOT / "scripts" / "runtime_artifact_refresh.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    check(list(functions) == list(MOVED), f"exactly the moved functions, in baseline order: {list(functions)}")
    every = {name for seams in SEAMS.values() for name in seams}
    for name, seams in SEAMS.items():
        function = functions[name]
        through: dict[str, int] = {}
        for n in ast.walk(function):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        annotations = {id(x) for a in ast.walk(function) if isinstance(a, ast.arg) and a.annotation for x in ast.walk(a.annotation)}
        annotations |= {id(x) for f in ast.walk(function) if isinstance(f, ast.FunctionDef) and f.returns for x in ast.walk(f.returns)}
        bare = sorted({n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id in every and id(n) not in annotations})
        check(through == seams and not bare, f"{name} reads {seams} through the launcher, none bare: {through} {bare}")
        bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        bound |= {h.name for h in ast.walk(function) if isinstance(h, ast.ExceptHandler) and h.name}
        bound |= {f.name for f in ast.walk(function) if isinstance(f, ast.FunctionDef) and f is not function}
        bound |= {(a.asname or a.name) for i in ast.walk(function) if isinstance(i, ast.ImportFrom) for a in i.names}
        check(not bound & set(through) and not set(OWN) & set(through),
              f"{name}: nothing it binds, nor a standard-library name, is read as the launcher's")
    nested = [n for n in ast.walk(functions["refresh_generated_project_runtime_artifacts"])
              if isinstance(n, ast.FunctionDef) and n.name == "_for_current_identities"]
    check(len(nested) == 1 and ast.unparse(nested[0].args) == "plan: ProjectBoardProvision"
          and ast.unparse(nested[0].returns) == "ProjectBoardProvision",
          "the nested helper's annotations are the plain (string) names, never the launcher's")
    local = [ast.unparse(n) for n in ast.walk(functions["_path_containment_error"]) if isinstance(n, ast.ImportFrom)]
    check(local == ["from scripts.ticket_board.project_provision import PathContainmentError"], f"the local import stays: {local}")
    for path, alias, count in (("upgrade_phases.py", "launcher", 1), ("role_identity_cutover.py", "team_launcher", 2)):
        tree = ast.parse((ROOT / "scripts" / path).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "refresh_generated_project_runtime_artifacts"]
        check(len(calls) == count and all(isinstance(c.func.value, ast.Name) and c.func.value.id == alias for c in calls),
              f"{path} still reaches the refresh through the launcher at its {count} site(s)")


# --- the document and the tenant plan --------------------------------------------------------------------------------


def test_the_document_is_read_first_unresolved_and_refused_before_anything() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd354-doc.") as raw:
        fx = Refresh(raw, read_problem="{path} does not exist")
        check(result_of(fx.run()) == (False, f"switchyard: {PROJECT} has no generated runtime plan; leaving artifacts unchanged")
              and fx.kinds() == ["read"] and fx.log[0][1] == fx.provision / "plan.json"
              and fx.log[0][2] == f"{PROJECT}'s generated runtime plan",
              f"no plan: nothing else is asked: {fx.log}")
    with tempfile.TemporaryDirectory(prefix="syrd354-doc.") as raw:
        fx = Refresh(raw, read_problem="{path} is a symbolic link")
        check(result_of(fx.run()) == (False, f"switchyard: {fx.provision / 'plan.json'} is a symbolic link. Nothing was changed.")
              and fx.kinds() == ["read"], "a bad document is refused, nothing changed")
    with tempfile.TemporaryDirectory(prefix="syrd354-doc.") as raw:
        fx = Refresh(raw, read_problem="x")
        link = Path(raw) / "linked-checkout"
        link.symlink_to(fx.provision)
        fx.run(config_path=link / f"{PROJECT}.json")
        check(fx.log[0][1] == link / "plan.json", f"made absolute, never resolved through the link: {fx.log[0][1]}")
        cwd = os.getcwd()
        try:
            os.chdir(raw)
            fx.log.clear()
            fx.run(config_path=Path("linked-checkout") / f"{PROJECT}.json")
        finally:
            os.chdir(cwd)
        check(fx.log[0][1] == link / "plan.json", f"a relative path is made absolute from here: {fx.log[0][1]}")


def test_operator_values_and_the_plan_parse() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd354-parse.") as raw:
        fx = Refresh(raw, parse_error="syrd354 missing owner_home")
        result = fx.run(source_repo=Path("/nonexistent/syrd354/src/../src"), commit_git_dir=" cache ")
        parse = fx.entry("parse")
        check(parse[2] == {"source_repo": "/nonexistent/syrd354/src", "commit_git_dir": "cache"} and parse[3] is fx.document
              and parse[1] == fx.provision / "plan.json",
              f"the operator's values, normalised and stripped, and the document as read: {parse}")
        check(result_of(result) == (False, f"switchyard: {PROJECT} runtime plan is incomplete and cannot be refreshed "
                                           "automatically: syrd354 missing owner_home") and fx.kinds() == ["read", "parse"],
              "an incomplete plan refuses")
    with tempfile.TemporaryDirectory(prefix="syrd354-parse.") as raw:
        fx = Refresh(raw)
        try:
            fx.run(commit_git_dir="  "); raised = None
        except SystemExit as exc:
            raised = str(exc)
        check(raised == "switchyard: --commit-git-dir must not be empty" and fx.kinds() == ["read"],
              "an empty cache is the operator's error, raised before the plan is parsed")
        fx = Refresh(tempfile.mkdtemp(prefix="syrd354-parse.", dir=raw))
        fx.run()
        check(fx.entry("parse")[2] == {}, "no operator values: none supplied")


def test_the_tenant_render_and_its_containment_refusal() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd354-render.") as raw:
        fx = Refresh(raw, migrated=("zeta", "alpha"))
        result = fx.run(source_repo=Path("/nonexistent/syrd354/op"))
        render = fx.entry("render")
        check(render[2] == {"enable_owner_linger": False}
              and render[1] == Plan(control_user=f"ctl-of-{OWNER}", source_repo="/nonexistent/syrd354/op",
                                    marks=("checkout", "identities"))
              and fx.entry("checkout")[1] == fx.config_path and fx.entry("identities")[1] is fx.config
              and fx.entry("grant") == ("grant", PROJECT, OWNER),
              f"the tenant's plan, its controller from the grant, the operator's source, its checkout, rendered for "
              f"the identities running, without linger: {render}")
        check(result_of(result)[1].endswith(f"; migrated {PROJECT} plan fields added since it was provisioned: alpha, zeta"),
              f"the migration is said, sorted: {result_of(result)[1]}")
    with tempfile.TemporaryDirectory(prefix="syrd354-render.") as raw:
        fx = Refresh(raw, containment="//syrd354-worktrees is not inside /")
        check(result_of(fx.run()) == (False, (
            f"switchyard: cannot establish a root-owned baseline for {PROJECT} from {fx.provision / 'plan.json'}: "
            f"owner_home '/home/{OWNER}' cannot contain the paths this plan derives from it: //syrd354-worktrees is "
            "not inside /. Re-provision the project so root generates its own."))
              and "publish" not in fx.kinds() and "euid?" not in fx.kinds(),
              "a tampered owner_home is refused by field, before any copy or root decision")


# --- a legacy root-owned provision directory ----------------------------------------------------------------------


def test_a_legacy_root_owned_directory_only_as_root_without_a_baseline() -> None:
    for euid, stored, problem, asked in ((1000, False, ROOT_OWNED, False), (0, True, ROOT_OWNED, False),
                                         (0, False, "owned by syrd354-owner", False), (0, False, f"{'x'} {ROOT_OWNED}", True)):
        with tempfile.TemporaryDirectory(prefix="syrd354-legacy.") as raw:
            fx = Refresh(raw, euid=euid, baseline_stored=stored, dir_owner_problem=problem)
            fx.run()
            check(("legacy-owner" in fx.kinds()) == asked, f"euid {euid}, stored {stored}, {problem!r}: asked {asked}")
    with tempfile.TemporaryDirectory(prefix="syrd354-legacy.") as raw:
        fx = Refresh(raw, euid=0, dir_owner_problem=ROOT_OWNED, owner_problem="its board unit is missing")
        check(result_of(fx.run()) == (False, (
            f"switchyard: {PROJECT}'s provision directory {fx.provision} is owned by root, and its owner cannot be "
            "established from root-controlled host records: its board unit is missing. Nothing was changed; the "
            "tenant is left as it is.")) and "repair" not in fx.kinds() and "publish" not in fx.kinds(),
              "no established owner: refused, nothing repaired or published")
    with tempfile.TemporaryDirectory(prefix="syrd354-legacy.") as raw:
        fx = Refresh(raw, euid=0, dir_owner_problem=ROOT_OWNED, repair_problem="refusing to repair: a link")
        check(result_of(fx.run()) == (False, "switchyard: refusing to repair: a link. Nothing was changed.")
              and "publish" not in fx.kinds(), "a repair refusal stops before any copy")
    for dry_run, verb in ((True, "would return"), (False, "returned")):
        with tempfile.TemporaryDirectory(prefix="syrd354-legacy.") as raw:
            fx = Refresh(raw, euid=0, dir_owner_problem=ROOT_OWNED, baseline=None, baseline_reason="syrd354: none yet")
            result = fx.run(dry_run=dry_run)
            repair = fx.entry("repair")
            note = (f"; {verb} {PROJECT}'s legacy root-owned provision directory to {OWNER}, established by: unit says "
                    f"so; registry says so. Entries: {fx.provision}/ -> {OWNER}")
            check(repair == ("repair", fx.provision, OWNER, ["plan.json", "layout.json", f"{PROJECT}.json"], dry_run)
                  and fx.entry("baseline")[3]["established_owner"] == OWNER
                  and result_of(result)[1] == "syrd354: none yet" + note,
                  f"dry {dry_run}: the rendered files and the config repaired, the owner handed to the baseline, and "
                  f"the note said even on the baseline's refusal: {result_of(result)}")
            check(fx.kinds().index("repair") < fx.kinds().index("target") and ("publish" not in fx.kinds() or
                  fx.kinds().index("repair") < fx.kinds().index("publish")),
                  f"the repair comes before anything is published or decided as root: {fx.kinds()}")

    with tempfile.TemporaryDirectory(prefix="syrd354-legacy.") as raw:
        fx = Refresh(raw, euid=0, dir_owner_problem=ROOT_OWNED)
        result = fx.run(dry_run=True)
        check(result_of(result)[1].endswith(
            f"; would return {PROJECT}'s legacy root-owned provision directory to {OWNER}, established by: unit says so; "
            f"registry says so. Entries: {fx.provision}/ -> {OWNER}"),
              f"the ownership note is carried into every later outcome too: {result_of(result)[1]}")

# --- the tenant's copies and a non-root run ------------------------------------------------------------------------


def test_tenant_copies_current_changed_linked_and_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd354-copies.") as raw:
        other = Path(raw) / "elsewhere"
        other.write_bytes(b"tenant layout")
        fx = Refresh(raw, tenant_files={"plan.json": b"tenant plan", "layout.json": other})
        result = fx.run()
        check([e[1:] for e in fx.log if e[0] == "publish"] == [("layout.json", b"tenant layout")]
              and result_of(result) == (True, f"switchyard: refreshed {PROJECT} generated runtime artifacts: layout.json"),
              f"a current copy is left; a link with the right bytes is not current, and is republished: {fx.log}")
    with tempfile.TemporaryDirectory(prefix="syrd354-copies.") as raw:
        fx = Refresh(raw)
        result = fx.run(dry_run=True)
        check("publish" not in fx.kinds() and result_of(result) == (
            False, f"switchyard: {PROJECT} generated runtime artifacts can be refreshed: layout.json, plan.json"),
              f"a dry run publishes nothing and says what could be refreshed: {result_of(result)}")
    with tempfile.TemporaryDirectory(prefix="syrd354-copies.") as raw:
        fx = Refresh(raw, publish_problem="switchyard: refusing to write layout.json: a link")
        check(result_of(fx.run()) == (False, "switchyard: refusing to write layout.json: a link")
              and [e[1] for e in fx.log if e[0] == "publish"] == ["layout.json"],
              "the first refused publication stops the refresh")
    with tempfile.TemporaryDirectory(prefix="syrd354-copies.") as raw:
        fx = Refresh(raw, tenant_files={"plan.json": b"tenant plan", "layout.json": b"tenant layout"}, target_exists=False)
        result = fx.run()
        check(result_of(result) == (False, f"switchyard: {PROJECT} generated runtime artifacts are already current; run "
                                           f"`switchyard upgrade {PROJECT}` as root to stage the units, grants and SQL root installs")
              and fx.entry("target") == ("target", PROJECT, fx.base / "root") and "baseline" not in fx.kinds(),
              f"not root: current, and told how root stages, and nothing root decides is asked: {result_of(result)}")


# --- as root --------------------------------------------------------------------------------------------------------


def test_root_judges_the_document_as_found_and_projects_only_what_is_authorized() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd354-root.") as raw:
        fx = Refresh(raw, euid=0, added=("design",), refused=("ops=root",))
        result = fx.run(source_repo=Path("/nonexistent/syrd354/op"), commit_git_dir="op-cache")
        baseline = fx.entry("baseline")
        check(baseline[2] == fx.document and baseline[2] is not fx.document and baseline[1] == fx.provision
              and baseline[3] == dict(source_repo=Path("/nonexistent/syrd354/op"), operator_commit_git_dir=True,
                                      supplied={"source_repo": "/nonexistent/syrd354/op", "commit_git_dir": "op-cache"},
                                      established_owner=""),
              f"the baseline judges a copy of the document as it was read, with the operator's values: {baseline}")
        check(fx.entry("project")[2] is baseline[2], "and the projection judges that same captured document")
        check(fx.said == [f"switchyard: ignoring {PROJECT} plan entry ops=root: an unprivileged workflow projection may "
                          "only add a role under that role's own canonical account"], f"each refused entry said: {fx.said}")
        root_render = fx.entry("render", 1)
        check(root_render[2] == {} and root_render[1] == Plan(
            name="baseline", control_user=f"ctl-of-{OWNER}", source_repo="/nonexistent/syrd354/op", commit_git_dir="op-cache",
            marks=("projected", "checkout", "identities")),
              f"the projected baseline, its controller from the grant, the operator's values, its checkout, for the "
              f"identities running: {root_render}")
        check(result_of(result) == (True, (
            f"switchyard: refreshed {PROJECT} generated runtime artifacts; tenant copies: layout.json, plan.json; root "
            f"installs grant, unit from {fx.target}; role accounts added from the workflow projection: design")),
              f"everything said, with the added role: {result_of(result)}")
        check(fx.kinds()[-4:] == ["privacy?", "ensure", "close", "install"], f"privacy repaired, then installed: {fx.kinds()}")
    with tempfile.TemporaryDirectory(prefix="syrd354-root.") as raw:
        fx = Refresh(raw, euid=0)
        fx.run()
        check(fx.entry("baseline")[3]["operator_commit_git_dir"] is False, "no cache given: none exempt")


def test_root_results_for_each_outcome() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, baseline=None, baseline_reason="syrd354: no baseline")
        check(result_of(fx.run()) == (True, "syrd354: no baseline"), "a baseline refusal answers whether copies changed")
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, baseline=None, tenant_files={"plan.json": b"tenant plan", "layout.json": b"tenant layout"})
        check(result_of(fx.run()) == (False, "syrd354: no baseline"), "and nothing changed: False")
    current = {"plan.json": b"tenant plan", "layout.json": b"tenant layout"}
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, tenant_files=current, ensured=("mode 0700 on root",), closed=("closed 1 artifact(s)",))
        (fx.target / "unit").write_bytes(b"root unit")
        (fx.target / "grant").write_bytes(b"root grant")
        check(result_of(fx.run()) == (True, f"switchyard: {PROJECT} generated runtime artifacts are already current; "
                                            "mode 0700 on root; closed 1 artifact(s)")
              and "install" not in fx.kinds() and fx.entry("close")[2] == ["grant", "unit"],
              "privacy is repaired even when no content changed, and that alone is a change")
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, tenant_files=current)
        (fx.target / "unit").write_bytes(b"root unit")
        (fx.target / "grant").write_bytes(b"root grant")
        check(result_of(fx.run()) == (False, f"switchyard: {PROJECT} generated runtime artifacts are already current"),
              "nothing to repair and nothing changed: False")
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, tenant_files=current, privacy=("mode 0755", "owner 1000"), migrated=("m",))
        (fx.target / "unit").write_bytes(b"old unit")
        (fx.target / "grant").write_bytes(b"root grant")
        result = fx.run(dry_run=True)
        check(result_of(result) == (False, f"switchyard: {PROJECT} generated runtime artifacts can be refreshed: unit; "
                                           f"would repair: mode 0755; would repair: owner 1000; migrated {PROJECT} plan "
                                           "fields added since it was provisioned: m")
              and not {"ensure", "close", "install", "publish"} & set(fx.kinds()),
              f"a dry run repairs, installs and publishes nothing, and says what it would: {result_of(result)}")
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, install_error=OSError("syrd354 disk full"))
        check(result_of(fx.run()) == (True, f"switchyard: could not stage {PROJECT} privileged artifacts under {fx.target}: "
                                            "syrd354 disk full. Nothing privileged was installed; the previous root-owned "
                                            "copy is unchanged."),
              "an install failure is reported with whether the tenant copies changed")
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, tenant_files=current, install_error=OSError("x"))
        check(result_of(fx.run())[0] is False, "and False when they did not")
    with tempfile.TemporaryDirectory(prefix="syrd354-out.") as raw:
        fx = Refresh(raw, euid=0, target_exists=False)
        fx.run()
        check(not {"ensure", "close"} & set(fx.kinds()) and "install" in fx.kinds(),
              "no root directory yet: nothing to repair, the install creates it")


def test_an_error_reaches_the_caller() -> None:
    boom = RuntimeError("syrd354: the installer raised")
    with tempfile.TemporaryDirectory(prefix="syrd354-err.") as raw:
        fx = Refresh(raw, euid=0, install_error=boom)
        try:
            fx.run(); raised = None
        except RuntimeError as exc:
            raised = exc
        check(raised is boom and fx.kinds()[-1] == "install", f"anything but OSError is not caught: {fx.kinds()}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects_and_defaults",
             "test_the_seams_the_modules_own_names_and_the_callers")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"runtime_artifact_refresh_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
