#!/usr/bin/env python3
"""SYRD-366: resume-provision's checkout binder, finish step and command, against the launcher they came out of.

`plan_with_tenant_checkout`, `_finish_provision_after_packet` and
`switchyard_resume_provision_command` moved into
`scripts/resume_provision_command.py` unchanged. This pins what makes that safe:

- **No eager cycle.** At its top the module imports only the standard library
  and the registration timeout from `provider_runtime_state` -- the launcher's
  very object -- which loads nothing of the launcher's.
- **One set of objects.** The launcher re-exports every name, so
  `switchyard_main`, `runtime_artifact_refresh` and every suite reach them.
- **Seams (rule 24).** Every launcher facility these use, and each moved name
  another moved definition calls, is read from the launcher when it runs -- the
  launcher's own `__file__` included, so the default launcher script is still
  the one beside the launcher that is running. Nothing they bind is read
  through it (rule 27).
- **The behaviour is unchanged:** the structural checkout, the finish step's
  phases, refusals, order and idempotence, and the command's trust, source,
  record, registered-versus-rendered and packet paths.

Every facility is this test's own fake, read through the launcher. No root
record is read, nothing is registered, installed, launched or waited for, and
no uid, identity, release, desktop or process is consulted: every real seam is
refused unless a test stands one in. Paths are temporary directories.
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
MOVED = ("plan_with_tenant_checkout", "_finish_provision_after_packet", "switchyard_resume_provision_command")
OWN = ("os", "subprocess", "replace", "Path", "Any", "Callable", "Sequence", "RUNTIME_REGISTRATION_TIMEOUT_SECONDS")
SEAMS = {
    "plan_with_tenant_checkout": {"_project_dir_from_generated_config_path": 1},
    "_finish_provision_after_packet": {n: 1 for n in (
        "uid_for_user", "verified_tenant_config", "record_tenant_config_path", "switchyard_registry_dir", "_load_json",
        "_register_switchyard_project", "install_recovered_desktop_access", "launch_project", "__file__", "TEAM_LAUNCHER_NAME",
        "_owner_state_layout_output_path", "recovery_readiness_problems")},
    "switchyard_resume_provision_command": {n: 1 for n in (
        "_validate_project_slug", "switchyard_registry_dir", "privileged_baseline_plan_path", "partial_provision_record",
        "read_plan_no_follow", "trusted_owner_identity", "_resume_source_release", "_resume_plan_from_record", "verified_tenant_config",
        "uid_for_user", "plan_with_tenant_checkout", "ensure_privileged_provision_dir", "render_privileged_artifacts",
        "install_privileged_artifacts", "privileged_packet_completion", "_finish_provision_after_packet")},
}
SLUG = "p366"


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
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is judged, not a crash
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


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


#: Every launcher facility these could reach for real, refused. The moved names
#: stay real unless a test stands one in.
LIVE = {name: refuse(name) for reads in SEAMS.values() for name in reads
        if name not in ("__file__", "TEAM_LAUNCHER_NAME") and name not in MOVED}


@dataclasses.dataclass(frozen=True)
class Plan:
    project: str = SLUG
    owner_user: str = "syrd366-owner"
    owner_home: str = "/fixture/home/syrd366-owner"
    project_repository: str = ""


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_the_runtime_state_leaf_at_import() -> None:
    result = python("import sys, scripts.resume_provision_command as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.account_drop', 'scripts.provider_runtime_state']",
          f"it loads only provider_runtime_state and its leaf, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.resume_provision_command", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.resume_provision_command"),
                  ("scripts.runtime_artifact_refresh", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.resume_provision_command as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_file_origin_and_the_defaults() -> None:
    module = ast.parse((ROOT / "scripts" / "resume_provision_command.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    check([f.name for f in functions] == list(MOVED), f"the three, in baseline order: {[f.name for f in functions]}")
    every = {name for reads in SEAMS.values() for name in reads}
    for f in functions:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every})
        check(through == SEAMS[f.name] and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")
    check(not [n for n in ast.walk(module) if isinstance(n, ast.Name) and n.id == "__file__"],
          "this module's own __file__ is never read: the default launcher script is found beside the launcher")

    from scripts import provider_runtime_state, resume_provision_command as m, team_launcher

    f, c = m._finish_provision_after_packet.__kwdefaults__, m.switchyard_resume_provision_command.__kwdefaults__
    T = provider_runtime_state.RUNTIME_REGISTRATION_TIMEOUT_SECONDS
    check(f["runtime_wait_seconds"] is T is team_launcher.RUNTIME_REGISTRATION_TIMEOUT_SECONDS and c["runtime_wait_seconds"] is T
          and c["euid_getter"] is os.geteuid and f["runner"] is subprocess.run and c["runner"] is subprocess.run
          and f["print_func"] is print and c["print_func"] is print and f["start_roles"] is True and c["start_roles"] is True
          and c["enable_owner_linger"] is True and m.plan_with_tenant_checkout.__kwdefaults__ is None,
          "every default is the object it was: the shared timeout, os.geteuid, subprocess.run, print")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)}
    check(not defined & set(MOVED) and "read_board_declared_workflow" in defined,
          f"the launcher defines none of them, and keeps the interleaved board-workflow reader: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.resume_provision_command"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all three: {exported}")
    refresh = ast.parse((ROOT / "scripts" / "runtime_artifact_refresh.py").read_text(encoding="utf-8"))
    reads = [n for n in ast.walk(refresh) if isinstance(n, ast.Attribute) and n.attr == "plan_with_tenant_checkout"]
    bare = [n for n in ast.walk(refresh) if isinstance(n, ast.Name) and n.id == "plan_with_tenant_checkout"]
    check(reads and all(isinstance(r.value, ast.Name) and r.value.id == "launcher" for r in reads) and not bare,
          "runtime_artifact_refresh still reads the checkout binder through the launcher")


# --- the checkout binder ------------------------------------------------------------------------------------------------


def test_the_checkout_is_structural_and_the_plan_kept_when_nothing_changes() -> None:
    from scripts import team_launcher as t

    plan = Plan()
    asked: list = []
    with patched(t, _project_dir_from_generated_config_path=refuse("the structural derivation")):
        check(judged(t.plan_with_tenant_checkout, plan, config_path=None) is plan, "no configuration: the very same plan")
    with patched(t, _project_dir_from_generated_config_path=lambda path: asked.append(path) or None):
        check(judged(t.plan_with_tenant_checkout, plan, config_path=Path("/tmp/loose.json")) is plan
              and asked == [Path("/tmp/loose.json")], "not a generated configuration's path: the very same plan")
    with patched(t, _project_dir_from_generated_config_path=lambda path: Path("/fixture/home/Projects/p366")):
        same = Plan(project_repository="/fixture/home/Projects/p366")
        check(judged(t.plan_with_tenant_checkout, same, config_path=Path("/c.json")) is same, "already recorded: the very same plan")
        got = judged(t.plan_with_tenant_checkout, Plan(project_repository="/declared/by/the/tenant"), config_path=Path("/c.json"))
        check(got == Plan(project_repository="/fixture/home/Projects/p366"),
              f"otherwise only the checkout changes, and it is the structural one: {got}")


# --- the finish step ----------------------------------------------------------------------------------------------------


class Finish:
    """Every launcher seam the finish step reads, as fakes sharing one ordered log."""

    def __init__(self, tmp: Path) -> None:
        self.tmp, self.log, self.said = tmp, [], []
        self.registry = tmp / "registry"
        self.registry.mkdir(parents=True)
        self.verified = tmp / "home" / "Projects" / SLUG / ".switchyard" / "provision" / f"{SLUG}.json"
        self.config = SimpleNamespace(name="checked config", roles=[1, 2, 3])
        self.configured = SimpleNamespace(name="configured config", roles=[1, 2])
        self.verify = (self.verified, self.config, [])
        self.entry = None
        self.register_error = None
        self.desktop = (self.configured, True)
        self.launched = 0
        self.remaining: list[str] = []

    def seams(self, **extra: object) -> dict:
        log = self.log

        def register(verified, *, config_dir, registry_dir):
            log.append(("register", verified, config_dir, registry_dir))
            if self.register_error:
                raise SystemExit(self.register_error)
            return self.registry / f"{SLUG}.json"

        names = dict(
            LIVE,
            uid_for_user=lambda user: log.append(("uid", user)) or 1366,
            verified_tenant_config=lambda plan, slug, *, explicit, owner_uid: log.append(("verify", slug, explicit, owner_uid)) or self.verify,
            record_tenant_config_path=lambda slug, verified: log.append(("record", slug, verified)),
            switchyard_registry_dir=lambda: log.append(("registry-dir",)) or self.registry,
            _load_json=lambda path: log.append(("load", path)) or self.entry,
            _register_switchyard_project=register,
            install_recovered_desktop_access=lambda plan, config, verified, **kw: log.append(("desktop", config.name, verified, kw)) or self.desktop,
            _owner_state_layout_output_path=lambda slug, *, owner_home: log.append(("layout", slug, owner_home)) or Path("/fixture/layout.json"),
            launch_project=lambda config, **kw: log.append(("launch", config.name, kw)) or self.launched,
            recovery_readiness_problems=lambda plan, config, verified, **kw: log.append(("ready", config.name, verified, kw)) or list(self.remaining),
        )
        names.update(extra)
        return names

    def run(self, **kwargs):
        from scripts import team_launcher as t

        kwargs.setdefault("registry_dir", self.registry)
        kwargs.setdefault("config_dir", self.tmp / "configs")
        kwargs.setdefault("config_path", None)
        kwargs.setdefault("completion", SimpleNamespace(problems=()))
        seams = kwargs.pop("seams", {})
        with patched(t, **self.seams(**seams)):
            return judged(t._finish_provision_after_packet, SLUG, Plan(), print_func=self.said.append, **kwargs)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def test_the_finish_step_refuses_an_unverified_configuration_before_anything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = Finish(Path(tmp))
        fx.verify = (None, None, ["owned by 1000", "mode 0666"])
        check(fx.run(config_path=Path("/named.json")) == 1 and fx.kinds() == ["uid", "verify"]
              and fx.log[1] == ("verify", SLUG, Path("/named.json"), 1366),
              f"the owner's uid, then the verification with the named path; nothing recorded or registered: {fx.log}")
        check(fx.said == ["switchyard: owned by 1000", "switchyard: mode 0666",
                          f"switchyard: refusing to register {SLUG} from a configuration root has not verified. Nothing was changed. "
                          f"If its checkout is not where it was generated, name the configuration: "
                          f"`sudo switchyard resume-provision {SLUG} --config <path>`."], f"every objection, then the refusal: {fx.said}")
        fx = Finish(Path(tmp) / "b")
        fx.verify = (fx.verified, None, [])
        check(fx.run() == 1 and fx.kinds() == ["uid", "verify"], "a path without a configuration is refused too")


def test_the_finish_step_registers_once_and_refuses_a_different_registration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = Finish(Path(tmp))
        check(fx.run(start_roles=False) == 0, f"registered and ready: {fx.said}")
        check(fx.kinds() == ["uid", "verify", "record", "register", "ready"]
              and fx.log[3] == ("register", fx.verified, Path(tmp) / "configs", fx.registry),
              f"verified, recorded, registered with the dirs it was given, then read back: {fx.log}")
        check(fx.said == [f"switchyard: registered {SLUG} at {fx.registry / f'{SLUG}.json'} from {fx.verified}",
                          f"switchyard: {SLUG} is registered at {fx.registry / f'{SLUG}.json'}, its board and listener are running, "
                          "and all 3 configured role(s) have live sessions registered with the board."], f"{fx.said}")
        (fx.registry / f"{SLUG}.json").write_text("{}")
        fx.log.clear(); fx.said.clear()
        fx.entry = {"config_path": str(fx.verified)}
        check(fx.run(start_roles=False) == 0 and "register" not in fx.kinds()
              and fx.said[0] == f"switchyard: {SLUG} is already registered at {fx.registry / f'{SLUG}.json'}",
              f"already registered at this configuration: not registered again: {fx.said}")
        fx.log.clear(); fx.said.clear()
        fx.entry = {"config_path": "/somewhere/else.json"}
        check(fx.run() == 1 and fx.kinds() == ["uid", "verify", "record", "load"]
              and fx.said == [f"switchyard: {SLUG} is already registered at {fx.registry / f'{SLUG}.json'}, pointing at "
                              f"'/somewhere/else.json' rather than the configuration root verified ({fx.verified}). Which of those is "
                              "this project is not this command's to decide; nothing was changed."],
              f"registered elsewhere: refused before any role starts: {fx.said}")
        fx.log.clear(); fx.said.clear()
        fx.entry = None
        check(fx.run(start_roles=False) == 1 and fx.said[-1].startswith(f"switchyard: {SLUG} is already registered at")
              and "rather than the configuration root verified" in fx.said[-1] and "''" in fx.said[-1],
              f"an unreadable entry points nowhere, and is refused: {fx.said}")
        (fx.registry / f"{SLUG}.json").unlink()
        fx.log.clear(); fx.said.clear()
        fx.register_error = "registry is not writable"
        check(fx.run() == 1 and fx.said == ["switchyard: registry is not writable"] and fx.kinds()[-1] == "register",
              f"a registration that refuses is reported, and nothing starts: {fx.said}")
        fx.log.clear(); fx.said.clear(); fx.register_error = None
        fx.run(start_roles=False, registry_dir=None)
        check(("registry-dir",) in fx.log, "no registry named: the host's")


def test_desktop_access_then_the_launch_then_readiness() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        fx = Finish(Path(tmp))
        runner, installer = refuse("the runner itself"), refuse("the installer itself")
        release, approval = Path("/fixture/release"), Path("/fixture/approval.json")
        fx.remaining = []
        got = fx.run(source_release=release, desktop_approval_path=approval, desktop_installer=installer, runner=runner,
                     launcher_script=Path("/fixture/team-launcher"), process_commands=["dead"], pane_liveness_states=["s"],
                     session_statuses=["r"], registration="w", runtime_wait_seconds=5.0)
        check(got == 0 and fx.kinds() == ["uid", "verify", "record", "register", "desktop", "layout", "launch", "ready"],
              f"desktop access before the launch, the launch before readiness: {fx.kinds()}")
        desktop = fx.log[4]
        check(desktop[1:3] == ("checked config", fx.verified) and desktop[3] == {"source_release": release, "approval_path": approval,
              "installer": installer, "runner": runner, "print_func": fx.said.append},
              f"the recovered desktop access, with everything it was given: {desktop}")
        launch = fx.log[6]
        check(launch[1] == "configured config" and launch[2] == {"config_path": fx.verified, "mode": "start", "script_path": Path("/fixture/team-launcher"),
              "runner": runner, "layout_output": Path("/fixture/layout.json"), "assign_layout_owner": True, "print_func": fx.said.append}
              and fx.log[5] == ("layout", SLUG, Path(Plan().owner_home)),
              f"the configured config is launched, start mode, the owner's layout: {launch}")
        ready = fx.log[7]
        check(ready[1:3] == ("configured config", fx.verified) and ready[3] == {
            "registry_path": fx.registry / f"{SLUG}.json", "runner": runner, "process_commands": ["dead"], "pane_liveness_states": ["s"],
            "session_statuses": ["r"], "completion": SimpleNamespace(problems=()), "registration": "w", "runtime_wait_seconds": 5.0,
            "print_func": fx.said.append}, f"readiness is asked about everything it was given: {ready}")
        check(fx.said[-1].endswith("and all 2 configured role(s) have live sessions registered with the board."),
              f"the roles counted are the configured ones: {fx.said[-1]}")

        fx.log.clear(); fx.said.clear()
        # Reached through a symlinked directory, as an installed layout can be:
        # the default is beside the RESOLVED launcher file.
        (Path(tmp) / "real").mkdir()
        (Path(tmp) / "link").symlink_to(Path(tmp) / "real")
        with patched(t, __file__=str(Path(tmp) / "link" / "team_launcher.py")):
            fx.run()
        launched = [e for e in fx.log if e[0] == "launch"][0][2]["script_path"]
        check(launched == Path(tmp).resolve() / "real" / "team-launcher",
              f"no launcher script named: the one beside the LAUNCHER's own resolved file, read when it runs: {launched}")
        fx.log.clear(); fx.said.clear()
        fx.run()
        launched = [e for e in fx.log if e[0] == "launch"][0][2]["script_path"]
        check(launched == (ROOT / "scripts" / "team_launcher.py").resolve().with_name("team-launcher"),
              f"and by default, the script beside this launcher: {launched}")

        for desktop_answer in ((None, True), (fx.configured, False)):
            fx.log.clear(); fx.said.clear(); fx.desktop = desktop_answer
            check(fx.run() == 1 and fx.kinds()[-1] == "desktop" and fx.said[-1] == (
                f"switchyard: {SLUG} is registered, but its roles were not started: the desktop access they need is not "
                f"installed. Address what is named above and run `sudo switchyard resume-provision {SLUG}` again -- the "
                "phases already done are not repeated."), f"{desktop_answer}: no launch without desktop access: {fx.said}")
        fx.log.clear(); fx.said.clear(); fx.desktop = (fx.configured, True); fx.launched = 2
        check(fx.run() == 1 and fx.kinds()[-1] == "launch" and fx.said[-1] == (
            f"switchyard: {SLUG} is registered, but starting its roles did not succeed. Fix what the launcher named above and "
            f"run `sudo switchyard resume-provision {SLUG}` again -- the phases already done are not repeated."),
              f"a failed launch: no readiness claimed: {fx.said}")
        fx.log.clear(); fx.said.clear(); fx.launched = 0; fx.remaining = ["ops has no running pane", "the wait failed"]
        check(fx.run() == 1 and fx.said[-3:] == [
            f"switchyard: {SLUG} is not finished: ops has no running pane", f"switchyard: {SLUG} is not finished: the wait failed",
            f"switchyard: run `sudo switchyard resume-provision {SLUG}` again once that is addressed -- resuming is the supported "
            "retry, and it continues from wherever this stopped."], f"what is left, in order, and how to continue: {fx.said}")
        fx.log.clear(); fx.said.clear(); fx.remaining = []
        check(fx.run(start_roles=False) == 0 and "desktop" not in fx.kinds() and "launch" not in fx.kinds()
              and [e for e in fx.log if e[0] == "ready"][0][1] == "checked config",
              "roles not started: no desktop access, no launch, and readiness asked about the checked configuration")


# --- the command --------------------------------------------------------------------------------------------------------


class Command:
    """Every launcher seam the command reads, as fakes sharing one ordered log."""

    def __init__(self, tmp: Path) -> None:
        self.tmp, self.log, self.said = tmp, [], []
        self.registry = tmp / "registry"
        self.registry.mkdir(parents=True)
        self.baseline = tmp / "etc" / SLUG / "plan.json"
        self.record = SimpleNamespace(slug=SLUG)
        self.document = SimpleNamespace(data={"project": SLUG, "source_repo": "/fixture/releases/old"})
        self.read_problem = ""
        self.identity = SimpleNamespace(trusted=True, problems=())
        self.release = (Path("/fixture/releases/new"), "")
        self.plan, self.divergence = Plan(), []
        self.verified_config = None
        self.repairs: list[str] = []
        self.completion = SimpleNamespace(done=True, problems=())
        self.finished = 0

    def seams(self, **extra: object) -> dict:
        log = self.log
        names = dict(
            LIVE,
            _validate_project_slug=lambda slug: log.append(("validate", slug)) or slug,
            switchyard_registry_dir=lambda: log.append(("registry-dir",)) or self.registry,
            privileged_baseline_plan_path=lambda slug: log.append(("baseline", slug)) or self.baseline,
            partial_provision_record=lambda slug: log.append(("record?", slug)) or self.record,
            read_plan_no_follow=lambda path, **kw: log.append(("read", path, kw)) or (None if self.read_problem else self.document, self.read_problem),
            trusted_owner_identity=lambda slug: log.append(("identity", slug)) or self.identity,
            _resume_source_release=lambda source: log.append(("release", source)) or self.release,
            _resume_plan_from_record=lambda document, identity, *, source_repo: log.append(("rebuild", source_repo)) or (self.plan, list(self.divergence)),
            uid_for_user=lambda user: log.append(("uid", user)) or 1366,
            verified_tenant_config=lambda plan, slug, *, explicit, owner_uid: log.append(("verify", explicit, owner_uid)) or (self.verified_config, None, []),
            plan_with_tenant_checkout=lambda plan, *, config_path: log.append(("checkout", config_path)) or dataclasses.replace(plan, project_repository="/bound"),
            ensure_privileged_provision_dir=lambda path: log.append(("repair", path)) or list(self.repairs),
            render_privileged_artifacts=lambda plan, *, enable_owner_linger: log.append(("render", plan.project_repository, enable_owner_linger)) or "RENDERED",
            install_privileged_artifacts=lambda plan, rendered: log.append(("install", rendered)) or self.tmp / "installed",
            privileged_packet_completion=lambda plan, *, runner: log.append(("probe", runner)) or self.completion,
            _finish_provision_after_packet=lambda slug, plan, **kw: log.append(("finish", slug, plan.project_repository, kw)) or self.finished,
        )
        names.update(extra)
        return names

    def run(self, *, euid: int = 0, **kwargs):
        from scripts import team_launcher as t

        kwargs.setdefault("registry_dir", self.registry)
        seams = kwargs.pop("seams", {})
        with patched(t, **self.seams(**seams)):
            return judged(t.switchyard_resume_provision_command, SLUG, euid_getter=lambda: self.log.append(("euid",)) or euid,
                          print_func=self.said.append, **kwargs)

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def test_the_command_refuses_before_it_reads_as_root() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        fx = Command(Path(tmp))
        with patched(t, **fx.seams(_validate_project_slug=lambda slug: (_ for _ in ()).throw(SystemExit(f"bad slug {slug}")))):
            bad = judged(t.switchyard_resume_provision_command, "Bad Slug!", euid_getter=refuse("the euid"), print_func=fx.said.append)
        check(isinstance(bad, SystemExit) and str(bad) == "bad slug Bad Slug!" and fx.said == [], f"the slug is validated first: {bad!r}")
        fx.record = None
        check(fx.run() == 1 and fx.kinds() == ["validate", "baseline", "record?"] and fx.said == [
            f"switchyard: root holds no provisioning record for {SLUG} at {fx.baseline}, so there is nothing to resume. A project that "
            "was never provisioned is started with `sudo switchyard new`."], f"no record, no registry: nothing to resume: {fx.said}")
        (fx.registry / f"{SLUG}.json").write_text("{}")
        fx.log.clear(); fx.said.clear()
        check(fx.run() == 1 and fx.said == [
            f"switchyard: {SLUG} is registered at {fx.registry / f'{SLUG}.json'} and root holds no provisioning record to resume from; "
            f"use `switchyard upgrade {SLUG}` instead. Nothing was changed."], f"no record but registered: upgrade instead: {fx.said}")
        (fx.registry / f"{SLUG}.json").unlink()
        fx.record = SimpleNamespace()
        fx.log.clear(); fx.said.clear()
        check(fx.run(euid=1000, source_repo=Path("/fixture/src")) == 1 and fx.kinds() == ["validate", "baseline", "record?", "euid"]
              and fx.said == [f"switchyard: resuming {SLUG} reads root's own provisioning record and rewrites root's artifacts. "
                              f"Run: sudo switchyard resume-provision {SLUG} --source-repo /fixture/src"],
              f"not root: told how, before any root-only read: {fx.log}")
        fx.log.clear(); fx.said.clear()
        fx.run(euid=1000)
        check(fx.said == [f"switchyard: resuming {SLUG} reads root's own provisioning record and rewrites root's artifacts. "
                          f"Run: sudo switchyard resume-provision {SLUG}"], f"and without a source repo, none named: {fx.said}")
        fx.log.clear(); fx.said.clear()
        fx.run(registry_dir=None, euid=1000)
        check(("registry-dir",) in fx.log, "no registry named: the host's")


def test_the_command_trusts_only_roots_record_the_kernel_and_a_verified_release() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = Command(Path(tmp))
        fx.read_problem = "plan.json is a symbolic link"
        check(fx.run() == 1 and fx.log[-1] == ("read", fx.baseline, {"require_root_owned": True})
              and fx.said == ["switchyard: plan.json is a symbolic link",
                              f"switchyard: root holds no usable provisioning record for {SLUG}, so there is nothing to resume from. Nothing was changed."],
              f"root's record, read no-follow and root-owned, or nothing: {fx.said}")
        for data, shown in (({"project": "other"}, "'other'"), ({}, "''"), ({"project": "  "}, "''")):
            fx = Command(Path(tmp) / f"project-{len(shown)}-{len(data)}-{data.get('project', '').strip() or 'none'}")
            fx.document = SimpleNamespace(data=data)
            check(fx.run() == 1 and fx.said == [f"switchyard: {fx.baseline} records project {shown}, not '{SLUG}'. Which one it belongs "
                                                "to is not this command's to decide; nothing was changed."] and "identity" not in fx.kinds(),
                  f"{data}: another project's record is refused: {fx.said}")
        fx = Command(Path(tmp) / "id")
        fx.identity = SimpleNamespace(trusted=False, problems=("uid 1000 is not the owner",))
        check(fx.run() == 1 and fx.said == ["switchyard: uid 1000 is not the owner",
                                           f"switchyard: refusing to resume {SLUG}: root cannot establish whose installation this is. Nothing was changed."]
              and "release" not in fx.kinds(), f"an owner the kernel does not vouch for: {fx.said}")
        fx.log.clear(); fx.said.clear()
        fx.identity = SimpleNamespace(trusted=True, problems=())
        fx.release = (None, "not a root-controlled release")
        check(fx.run(source_repo=Path("/named")) == 1 and ("release", Path("/named")) in fx.log and fx.said == [
            "switchyard: not a root-controlled release", f"switchyard: refusing to resume {SLUG} from an unverified release. Nothing was changed."],
              f"an unverified release: {fx.said}")
        fx.log.clear(); fx.said.clear()
        fx.release = (Path("/fixture/releases/new"), "")
        fx.divergence = ["owner_user: provisioned 'a', regenerated 'b'"]
        check(fx.run() == 1 and ("rebuild", Path("/fixture/releases/new")) in fx.log and fx.said == [
            "switchyard: owner_user: provisioned 'a', regenerated 'b'",
            f"switchyard: refusing to resume {SLUG}: rebuilding it would change what root installs. Nothing was changed."]
              and "render" not in fx.kinds(), f"a rebuild that would change what root installs: {fx.said}")


def test_the_command_rebuilds_or_leaves_artifacts_then_reads_the_packet() -> None:
    runner = refuse("the runner itself")
    with tempfile.TemporaryDirectory() as tmp:
        fx = Command(Path(tmp))
        fx.verified_config = Path("/fixture/home/Projects/p366/.switchyard/provision/p366.json")
        got = fx.run(runner=runner, config_path=Path("/named.json"), enable_owner_linger=False)
        check(got == 0 and fx.kinds() == ["validate", "baseline", "record?", "euid", "read", "identity", "release", "rebuild", "uid", "verify",
                                          "checkout", "render", "install", "probe", "finish"], f"every phase, in order: {fx.kinds()}")
        check(("verify", Path("/named.json"), 1366) in fx.log and ("checkout", fx.verified_config) in fx.log
              and ("render", "/bound", False) in fx.log and ("install", "RENDERED") in fx.log and ("probe", runner) in fx.log,
              f"the checkout bound from the verified configuration before rendering: {fx.log}")
        check(fx.said == [f"switchyard: {SLUG} was provisioned from /fixture/releases/old; its artifacts are rebuilt from /fixture/releases/new",
                          f"switchyard: regenerated {SLUG}'s root-owned artifacts in {fx.tmp / 'installed'}"], f"{fx.said}")
        finish = [e for e in fx.log if e[0] == "finish"][0]
        check(finish[1:3] == (SLUG, "/bound") and finish[3] == {
            "registry_dir": fx.registry, "config_dir": None, "config_path": Path("/named.json"), "completion": fx.completion,
            "source_release": Path("/fixture/releases/new"), "desktop_approval_path": None, "desktop_installer": None, "runner": runner,
            "launcher_script": None, "start_roles": True, "process_commands": None, "pane_liveness_states": None, "session_statuses": None,
            "registration": None, "runtime_wait_seconds": 90.0, "print_func": fx.said.append},
              f"the finish step is handed everything: {finish[3]}")

        fx.log.clear(); fx.said.clear()
        fx.verified_config = None
        fx.document = SimpleNamespace(data={"project": SLUG, "source_repo": "/fixture/releases/new"})
        fx.run()
        check("checkout" not in fx.kinds() and ("render", "", True) in fx.log
              and fx.said == [f"switchyard: regenerated {SLUG}'s root-owned artifacts in {fx.tmp / 'installed'}"],
              f"no verified configuration: no checkout recorded; the same release is not announced: {fx.said}")

        (fx.registry / f"{SLUG}.json").write_text("{}")
        fx.log.clear(); fx.said.clear()
        fx.repairs = ["tightened plan.json to 0600"]
        fx.run()
        check("render" not in fx.kinds() and "install" not in fx.kinds() and ("repair", fx.baseline.parent) in fx.log
              and fx.said == [f"switchyard: {SLUG} is registered at {fx.registry / f'{SLUG}.json'}; its root-owned artifacts are left as "
                              f"they are (`switchyard upgrade {SLUG}` refreshes them).", "switchyard: tightened plan.json to 0600"],
              f"already registered: artifacts left alone, only the directory repaired: {fx.said}")
        finish = [e for e in fx.log if e[0] == "finish"][0]
        check(finish[3]["completion"] is fx.completion, "and the finish step still runs")

        fx.log.clear(); fx.said.clear()
        read: list = []
        fx.completion = SimpleNamespace(done=False, problems=("the board unit is not installed", "the board does not answer"))
        got = fx.run(completion_reader=lambda plan: read.append(plan) or fx.completion)
        check(got == 1 and "probe" not in fx.kinds() and "finish" not in fx.kinds() and len(read) == 1 and fx.said[-4:] == [
            f"switchyard: {SLUG} has not finished its privileged packet: the board unit is not installed",
            f"switchyard: {SLUG} has not finished its privileged packet: the board does not answer",
            f"switchyard: run {fx.baseline.parent}/operator-commands.sh through the ordinary operator path to finish those phases. Every "
            "phase in it is re-runnable, so the work already done is left alone.",
            f"switchyard: then run `sudo switchyard resume-provision {SLUG}` again -- it continues from there, registers the project and "
            "starts its roles."], f"a supplied reader, an unfinished packet, and the ordinary operator path: {fx.said}")
        fx.log.clear(); fx.said.clear()
        fx.completion = SimpleNamespace(done=True, problems=())
        fx.finished = 7
        check(fx.run() == 7, "the finish step's answer is the command's")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_only_the_runtime_state_leaf_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_file_origin_and_the_defaults")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"resume_provision_command_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
