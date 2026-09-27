#!/usr/bin/env python3
"""SYRD-346: `upgrade_project_command`'s U4, role tooling preview and staging, against the launcher.

U4 -- one `if dry_run: ... elif os.geteuid() == 0: ...` statement -- moved into
`scripts/upgrade_phases.py` as `_stage_upgrade_tooling`. It rewrites the
verified release root and the publication detail only on some branches, so the
caller hands both in and gets them back in a frozen `UpgradeToolingStaged`; each
of its five refusals records the artifacts phase blocked and answers 1. This
pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher exports both names, the very same objects whichever module is
  imported first, and the upgrade calls the phase by the launcher's own name, at
  U4's old position; a refusal is returned before either value is read.
- **Seams (rule 24).** Every launcher facility U4 uses is read through the
  launcher when it runs; `os` is the module's own, the shared module the suites'
  `team_launcher.os.geteuid` patches reach; nothing U4 binds is read through
  the launcher (rule 27).
- **The phase is unchanged:** the dry run's preview and its lazy boundary
  preview; as root, the untrusted migration first, a verified release (a stale
  one refused) before anything is staged, the rollback note before the first
  replacement, staging only from the release, hooks after staging, and the
  publication boundary last, its failure a warning and the detail; every
  refusal's text, journal record and code; and nothing at all for a non-root
  apply -- each carry handed back as the very object it came in as.

Every facility is this test's own recording fake: no release, journal, rollback
note, staged tooling, hook, sudoers rule, account, key, network or root path is
touched, and the effective uid is a patched answer.

SYRD-347 added U5, identity and accounts (`_upgrade_identities_and_accounts`):
the owner's GitHub identity (local, unknown, unresolved, dry, root and not),
pending identities and the artifacts verdict, the role accounts (the migration
published, named, or refused), and the identities transaction with its reload.
`config` and `release_report_config` are carried in and handed back, changed only
after a real transaction; a refusal is the very object U5 returned. Its local
provision and publication imports are patched where they live, never through
the launcher, and no key, account, migration or worker is touched.

SYRD-348 added U6, finish (`_finish_upgrade`, the upgrade's last sixteen
statements, ending in its own returns). Its cases pin the director phase and
instruction, the fresh cutover, the release root as root or owner, the release
reported for the original report config and recorded against the current one,
the trusted journal and reports, unsafe windows, and the blocked line and 1 said
after everything else; the upgrade returns U6's answer, and a U5 stop never
reaches it.
"""

from __future__ import annotations

import ast
import dataclasses
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
EXPORTED = ("UpgradeToolingStaged", "_stage_upgrade_tooling", "UpgradeIdentitiesDone", "_upgrade_identities_and_accounts",
            "_finish_upgrade")
#: U4's launcher lookups and how many times each is read (measured on the SYRD-346 baseline: 16 reads of 9 names).
SEAMS = {"_staged_tooling_dir": 1, "refresh_role_pane_hooks": 2, "resolve_trusted_upgrade_release": 2,
         "remove_tenant_publication_boundary": 2, "remove_untrusted_role_account_migration": 1,
         "stale_launcher_problems": 1, "record_release_rollback": 1, "refresh_staged_role_tooling": 1,
         "record_upgrade_phase": 5}
PROJECT = "p346"
CONFIG_PATH = Path("/nonexistent/syrd346/p346.json")
SOURCE = Path("/nonexistent/syrd346/source")
TOOLING = Path("/nonexistent/syrd346/tooling")
RELEASE_ROOT = Path("/nonexistent/syrd346/releases/r1")
INCOMING_ROOT = Path("/nonexistent/syrd346/incoming-root")


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


def refusing_runner(argv, **kwargs):
    raise AssertionError(f"U4 runs nothing itself; its runner is only handed on: {argv}")


class Upgrade:
    """U4's launcher facilities, answering from objects this test owns, into one ordered log."""

    def __init__(self, *, euid: int = 0, preview: object = "release", preview_problems: list | None = None,
                 legacy: list | None = None, release: object = "release", release_problems: list | None = None,
                 stale: list | None = None, rollback: list | None = None, staging: list | None = None,
                 hooks: list | None = None, publication: list | None = None, preview_removal: list | None = None,
                 error: Exception | None = None) -> None:
        self.release = SimpleNamespace(root=RELEASE_ROOT, name="r1")
        self.euid = euid
        self.preview = self.release if preview == "release" else preview
        self.preview_problems = preview_problems or []
        self.legacy, self.stale, self.rollback = legacy or [], stale or [], rollback or []
        self.resolved = self.release if release == "release" else release
        self.release_problems = release_problems or []
        self.staging, self.hooks, self.publication = staging or [], hooks or [], publication or []
        self.preview_removal = preview_removal or []
        self.error = error
        self.log: list[tuple] = []

    def names(self) -> dict[str, object]:
        L = self.log

        def hooks(config, *, staging_root, runner, print_func, dry_run=None, **extra):
            L.append(("hooks", staging_root, runner, dry_run, extra))
            return [] if dry_run else self.hooks

        def resolve(source, ref, *, runner, ref_is_pinned, **extra):
            L.append(("resolve", source, ref, ref_is_pinned, runner, extra))
            if extra.get("dry_run"):
                return self.preview, self.preview_problems
            return self.resolved, self.release_problems

        def removal(config, *, config_path, dry_run, runner, print_func):
            L.append(("removal", config_path, dry_run, runner))
            return self.preview_removal if dry_run else self.publication

        def staging(config, *, release_root, staging_root, runner, print_func):
            L.append(("staging", release_root, staging_root, runner))
            if self.error:
                raise self.error
            return self.staging

        return dict(
            _staged_tooling_dir=lambda config, root: L.append(("tooling-dir", root)) or Path(f"{root}/{config.project}"),
            refresh_role_pane_hooks=hooks,
            resolve_trusted_upgrade_release=resolve,
            remove_tenant_publication_boundary=removal,
            remove_untrusted_role_account_migration=lambda config, *, config_path, print_func:
                L.append(("legacy", config_path)) or self.legacy,
            stale_launcher_problems=lambda release, *, source_repo, project, publish_remote:
                L.append(("stale", release, source_repo, project, publish_remote)) or self.stale,
            record_release_rollback=lambda config, *, release, staging_root, print_func:
                L.append(("rollback", release, staging_root)) or self.rollback,
            refresh_staged_role_tooling=staging,
            record_upgrade_phase=lambda config, *, config_path, phase, state, detail:
                L.append(("journal", config_path, phase, state, detail)),
        )

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def stage(up: Upgrade, *, dry_run: bool = False, deploy_ref: str | None = "v346", detail: str = "",
          root: object = INCOMING_ROOT):
    from scripts import team_launcher, upgrade_phases

    said: list[str] = []
    config = SimpleNamespace(project=PROJECT)
    asked: list[int] = []

    def geteuid() -> int:
        asked.append(1)
        return up.euid
    kwargs = dict(config_path=CONFIG_PATH, deploy_ref=deploy_ref, deploy_ref_chosen="syrd346-pinned", dry_run=dry_run,
                  effective_source_repo=SOURCE, print_func=said.append, publication_detail=detail,
                  publish_remote="git@example.invalid:p346.git", runner=refusing_runner, tooling_root=TOOLING,
                  trusted_release_root=root)
    with patched(team_launcher, **up.names()), patched(os, geteuid=geteuid):
        result = upgrade_phases._stage_upgrade_tooling(config, **kwargs)
    return result, said, asked


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.upgrade_phases as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.upgrade_phases", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.upgrade_phases")):
        result = python("import importlib, os; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.upgrade_phases as u; "
                        f"print(all(getattr(t, n) is getattr(u, n) for n in {EXPORTED!r}), u.os is os)")
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: both names are the launcher's too, os the one module: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_phases_own_names_and_the_call_site() -> None:
    module = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top")
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_stage_upgrade_tooling")
    for name, count in SEAMS.items():
        through = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name
                   and isinstance(n.value, ast.Name) and n.value.id == "launcher"]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(through) == count and not bare, f"U4 reads {name} at its {count} site(s), through the launcher")
    geteuid = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == "geteuid"]
    check(len(geteuid) == 1 and isinstance(geteuid[0].value, ast.Name) and geteuid[0].value.id == "os",
          "the effective uid is asked of the module's own os, once")
    bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check(through == [], f"nothing U4 binds is read as the launcher's: {through}")
    from scripts.upgrade_phases import UpgradeToolingStaged
    fields = dataclasses.fields(UpgradeToolingStaged)
    check([f.name for f in fields] == ["trusted_release_root", "publication_detail"]
          and all(f.default is dataclasses.MISSING for f in fields)
          and UpgradeToolingStaged.__dataclass_params__.frozen,
          f"the result is frozen, holds the two carried values in the order U4 assigns them, with no defaults: {fields}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    upgrade = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade_project_command")
    at = next((i for i, n in enumerate(upgrade.body) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
               and ast.unparse(n.value.func).endswith("_stage_upgrade_tooling")), None)
    check(at is not None and isinstance(upgrade.body[at].value.func, ast.Name),
          "the upgrade calls U4 by the launcher's own (patchable) name")
    wiring = [ast.unparse(n) for n in upgrade.body[at + 1:at + 4]]
    check(at == 43 and wiring == ["if not isinstance(tooling_staged, UpgradeToolingStaged):\n    return tooling_staged",
                                  "trusted_release_root = tooling_staged.trusted_release_root",
                                  "publication_detail = tooling_staged.publication_detail"],
          f"the upgrade calls U4 at its old position, returns a refusal before reading either value, then takes both: "
          f"{at} {wiring}")
    sites = [n for n in ast.walk(launcher) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_stage_upgrade_tooling"]
    check(len(sites) == 1 and isinstance(sites[0].func, ast.Name), "one call, by the launcher's own (patchable) name")
    keywords = {k.arg: ast.unparse(k.value) for k in sites[0].keywords}
    check(keywords.get("trusted_release_root") == "trusted_release_root"
          and keywords.get("publication_detail") == "publication_detail",
          f"each carry is the caller's current value, same-named: {keywords}")


def run_wiring(answer: object):
    """The upgrade's own call, dispatch and unpacking, compiled from the launcher and run against a stand-in U4."""
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    upgrade = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade_project_command")
    statements = upgrade.body[43:47]
    code = ast.Module(body=[ast.FunctionDef(
        name="wiring", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=n) for n in (
            "config", "config_path", "deploy_ref", "deploy_ref_chosen", "dry_run", "effective_source_repo",
            "print_func", "publication_detail", "publish_remote", "runner", "tooling_root", "trusted_release_root")],
            kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=[*statements, ast.parse("return ('went on', trusted_release_root, publication_detail)").body[0]],
        decorator_list=[], returns=None, type_params=[])], type_ignores=[])
    from scripts.upgrade_phases import UpgradeToolingStaged
    asked: list[dict] = []
    namespace = {"UpgradeToolingStaged": UpgradeToolingStaged,
                 "_stage_upgrade_tooling": lambda config, **k: asked.append(k) or answer}
    exec(compile(ast.fix_missing_locations(code), "upgrade_project_command", "exec"), namespace)
    detail = "syrd346 incoming detail"
    result = namespace["wiring"]("cfg", CONFIG_PATH, "v346", True, False, SOURCE, print, detail, "", refusing_runner,
                                 TOOLING, INCOMING_ROOT)
    return result, asked, detail


def test_the_upgrade_returns_a_refusal_and_unpacks_a_continuation() -> None:
    result, asked, detail = run_wiring(1)
    check(result == 1 and type(result) is int, f"a refusal is returned as it came, before anything after U4: {result!r}")
    check(asked[0]["trusted_release_root"] is INCOMING_ROOT and asked[0]["publication_detail"] is detail
          and asked[0]["tooling_root"] is TOOLING and asked[0]["effective_source_repo"] is SOURCE,
          f"U4 is handed the caller's own values: {asked[0]}")
    from scripts.upgrade_phases import UpgradeToolingStaged
    going = UpgradeToolingStaged(trusted_release_root=RELEASE_ROOT, publication_detail="syrd346 detail")
    result, _, _ = run_wiring(going)
    check(result == ("went on", RELEASE_ROOT, "syrd346 detail"), f"going on, both values are taken: {result!r}")


# --- behaviour --------------------------------------------------------------------------------------------------


def test_a_dry_run_previews_and_stages_nothing() -> None:
    up = Upgrade(preview_removal=["syrd346: the rule stays until the real run"])
    detail = "syrd346 carried detail"
    result, said, asked = stage(up, dry_run=True, detail=detail)
    check(up.log == [("tooling-dir", TOOLING),
                     ("hooks", TOOLING, refusing_runner, True, {}),
                     ("resolve", SOURCE, "v346", "syrd346-pinned", refusing_runner, {"dry_run": True}),
                     ("removal", CONFIG_PATH, True, refusing_runner)] and asked == [],
          f"the preview, in order, with the caller's runner, and the effective uid never asked: {up.log}")
    check(said == [f"switchyard: would stage {PROJECT} role tooling in {TOOLING}/{PROJECT} from {SOURCE}",
                   "warning: switchyard: syrd346: the rule stays until the real run"],
          f"what it would stage, and what the preview could not work out: {said}")
    check(result.trusted_release_root is RELEASE_ROOT and result.publication_detail is detail,
          f"the previewed release's root, and the detail untouched: {result}")
    up = Upgrade(preview=None, preview_problems=["syrd346: no release yet"])
    result, said, _ = stage(up, dry_run=True, deploy_ref=None)
    check("removal" not in up.kinds() and said[-1] == "warning: switchyard: syrd346: no release yet"
          and up.log[2][2] == "",
          f"preview problems are shown instead of the boundary preview, which is never asked; no ref is '': {up.log}")
    check(result.trusted_release_root is INCOMING_ROOT, "no previewed release leaves the caller's root as it was")


def test_a_non_root_apply_does_nothing_here() -> None:
    up = Upgrade(euid=1000)
    detail = "syrd346 carried detail"
    result, said, asked = stage(up, detail=detail)
    check(up.log == [] and said == [] and asked == [1], f"not root: nothing but the uid question: {up.log} {said}")
    check(result.trusted_release_root is INCOMING_ROOT and result.publication_detail is detail,
          f"both carries come back as the very objects they went in as: {result}")


def test_a_root_apply_stages_from_the_verified_release_in_order() -> None:
    up = Upgrade()
    detail = "syrd346 carried detail"
    result, said, _ = stage(up, detail=detail)
    check(up.log == [("legacy", CONFIG_PATH),
                     ("resolve", SOURCE, "v346", "syrd346-pinned", refusing_runner, {}),
                     ("stale", up.release, SOURCE, PROJECT, "git@example.invalid:p346.git"),
                     ("rollback", up.release, TOOLING),
                     ("staging", RELEASE_ROOT, TOOLING, refusing_runner),
                     ("hooks", TOOLING, refusing_runner, None, {}),
                     ("removal", CONFIG_PATH, False, refusing_runner)],
          f"migration, release, staleness, rollback note, staging from the release, hooks, boundary -- in that "
          f"order: {up.log}")
    check(said == [] and result.trusted_release_root is RELEASE_ROOT and result.publication_detail is detail,
          f"the verified root goes on, the detail untouched, nothing said: {result} {said}")


def test_each_refusal_blocks_the_phase_and_answers_one() -> None:
    cases = [
        ("the untrusted migration", dict(legacy=["syrd346: migration left"]), "legacy", "syrd346: migration left",
         "role-account migration in a directory its control role can write."),
        ("no verified release", dict(release=None, release_problems=["syrd346: no release"]), "resolve",
         "syrd346: no release", "none is available."),
        ("a stale release", dict(stale=["syrd346: launcher is stale"]), "stale", "syrd346: launcher is stale",
         "none is available."),
        ("no rollback note", dict(rollback=["syrd346: no note"]), "rollback", "syrd346: no note",
         "would be upgraded with no recorded way back."),
        ("staging failed", dict(staging=["syrd346: staging failed"]), "staging", "syrd346: staging failed",
         "back on tooling this release did not stage."),
        ("hooks failed", dict(hooks=["syrd346: hooks failed"]), "hooks", "syrd346: hooks failed",
         "back without the hooks this release stages for them."),
    ]
    order = ["legacy", "resolve", "stale", "rollback", "staging", "hooks", "removal"]
    for label, kwargs, last_step, problem, ending in cases:
        up = Upgrade(**kwargs)
        try:
            result, said, _ = stage(up)
        except Exception as exc:  # noqa: BLE001 - a refusal that raises instead is the finding
            result, said = f"raised {type(exc).__name__}: {exc}", []
        steps = [k for k in up.kinds() if k != "journal"]
        check(result == 1 and type(result) is int, f"{label}: the phase answers 1: {result!r}")
        check(steps == order[:order.index(last_step) + 1] if last_step != "resolve" else steps == ["legacy", "resolve"],
              f"{label}: nothing after the failing step runs: {steps}")
        check(up.log[-1] == ("journal", CONFIG_PATH, "artifacts", "blocked", problem),
              f"{label}: the artifacts phase is recorded blocked, with the problems: {up.log[-1]}")
        check(said[0] == f"switchyard: {problem}" and said[-1].startswith("switchyard: stopping before any later phase: ")
              and said[-1].endswith(ending), f"{label}: said, then why it stops: {said}")


def test_an_incomplete_boundary_removal_is_a_warning_and_the_detail() -> None:
    up = Upgrade(publication=["syrd346: rule A", "syrd346: rule B"])
    result, said, _ = stage(up, detail="syrd346 carried detail")
    from scripts.upgrade_phases import UpgradeToolingStaged
    check(isinstance(result, UpgradeToolingStaged), f"an incomplete removal does not stop the upgrade: {result!r}")
    check(said == ["warning: switchyard: syrd346: rule A", "warning: switchyard: syrd346: rule B",
                   f"warning: switchyard: {PROJECT}'s publication boundary was NOT fully removed. The rest of this "
                   "upgrade continued."] and "journal" not in up.kinds(),
          f"warned, not refused, and nothing blocked: {said}")
    check(result.publication_detail == "publication boundary not removed: syrd346: rule A; syrd346: rule B"
          and result.trusted_release_root is RELEASE_ROOT, f"the detail says what was left: {result}")


def test_an_error_reaches_the_caller() -> None:
    boom = OSError("syrd346: staging raised")
    up = Upgrade(error=boom)
    try:
        stage(up); raised = None
    except OSError as exc:
        raised = exc
    check(raised is boom and up.kinds()[-1] == "staging" and "journal" not in up.kinds(),
          f"an exception is not caught or recorded: {up.kinds()}")


# --- U5: identity and accounts (SYRD-347) --------------------------------------------------------------------------

#: U5's launcher lookups and how many times each is read (measured on the SYRD-347 baseline: 23 reads of 19 names).
U5_SEAMS = {"_tenant_owner_home": 1, "current_user_name": 1, "_plan_data_from_config": 1,
            "switchyard_privileged_provision_root": 1, "github_identity_status": 1, "github_identity_remedy": 1,
            "write_pending_identities": 1, "record_upgrade_phase": 5, "_role_accounts_ready": 1,
            "trusted_role_account_migration_path": 1, "publish_role_account_migration": 1, "read_upgrade_source": 1,
            "role_account_migration_instruction": 1, "cutover_role_identities_command": 1, "_staged_tooling_dir": 1,
            "load_project_config": 1, "role_account_cutover": 1, "read_upgrade_journal": 1, "upgrade_phase_report": 1}
U5_LOCAL = [("scripts.ticket_board.project_provision", "owner_github_identity_commands"),
            ("scripts.ticket_board.project_provision", "owner_github_key_path"),
            ("scripts.ticket_board.project_provision", "publication_uses_github"),
            ("scripts.ticket_board.project_provision", "resolve_owner_github_identity"),
            ("scripts.ticket_board.publication_boundary", "resolve_pinned_remote")]
OWNER_HOME = Path("/nonexistent/syrd347/owner-home")
MIGRATION = Path("/nonexistent/syrd347/migration.sh")


class Code(int):
    """An exit code with an identity, so a refusal can be shown to hand back the very object it got."""


class Identities:
    """U5's launcher facilities and local imports, answering from objects this test owns, into one ordered log."""

    def __init__(self, *, euid: int = 0, github: object = True, resolved: bool = True, plan: dict | None = None,
                 script: int = 0, remedy: str = "", ready: bool = True, trusted_path: object = MIGRATION,
                 published: tuple = (MIGRATION, []), instruction: tuple = (MIGRATION, []), transaction: object = 0,
                 error: Exception | None = None) -> None:
        self.euid, self.github, self.resolved = euid, github, resolved
        self.plan = plan if plan is not None else {"owner_github_key_name": "id_347", "owner_github_host_alias": "gh-347"}
        self.script, self.remedy, self.ready = script, remedy, ready
        self.trusted_path, self.published, self.instruction = trusted_path, published, instruction
        self.transaction, self.error = transaction, error
        self.reloaded = SimpleNamespace(project=PROJECT, run_as_user="syrd347-owner", name="reloaded")
        self.recalculated = SimpleNamespace(is_complete=True, name="recalculated")
        self.journal = SimpleNamespace(name="journal")
        self.log: list[tuple] = []

    def launcher(self) -> dict[str, object]:
        L = self.log

        def transaction(config, **kw):
            L.append(("transaction", config, kw))
            if self.error:
                raise self.error
            return self.transaction

        return dict(
            _tenant_owner_home=lambda config, config_path: L.append(("owner-home", config_path)) or OWNER_HOME,
            current_user_name=lambda: L.append(("current-user",)) or "syrd347-me",
            _plan_data_from_config=lambda config, config_path: L.append(("plan", config_path)) or dict(self.plan),
            switchyard_privileged_provision_root=lambda: L.append(("provision-root",)) or Path("/nonexistent/syrd347/root"),
            github_identity_status=lambda owner, home, **kw: L.append(("status", owner, home, kw)) or "STATUS",
            github_identity_remedy=lambda status, *, project: L.append(("remedy", status, project)) or self.remedy,
            write_pending_identities=lambda config: L.append(("pending", config)),
            record_upgrade_phase=lambda config, **kw: L.append(("journal", config, kw)),
            _role_accounts_ready=lambda config: L.append(("ready?", config)) or self.ready,
            trusted_role_account_migration_path=lambda config: L.append(("trusted-path", config)) or self.trusted_path,
            publish_role_account_migration=lambda config, **kw: L.append(("publish", config, kw)) or self.published,
            read_upgrade_source=lambda config: L.append(("source", config)) or "SYRD347-SOURCE",
            role_account_migration_instruction=lambda config, *, runner: L.append(("instruction", config, runner))
            or self.instruction,
            cutover_role_identities_command=transaction,
            _staged_tooling_dir=lambda config, root: Path(f"{root}/staged"),
            load_project_config=lambda project, path: L.append(("reload", project, path)) or self.reloaded,
            role_account_cutover=lambda config, *, runner: L.append(("recalculate", config, runner)) or self.recalculated,
            read_upgrade_journal=lambda config, *, config_path, trusted: L.append(("read-journal", config, trusted))
            or self.journal,
            upgrade_phase_report=lambda config, **kw: L.append(("report", config, kw)) or ["SYRD347 REPORT"],
        )

    def provision(self) -> dict[str, object]:
        L = self.log

        def identity(home, *, recorded_key_name, recorded_host_alias):
            L.append(("identity", home, recorded_key_name, recorded_host_alias))
            return SimpleNamespace(resolved=self.resolved, problems=["syrd347: two keys, none named"],
                                   key_name="id_347", host="github.com", host_alias="gh-347", source="the plan")

        def commands(owner, home, **kw):
            L.append(("commands", owner, home, kw))
            return ["syrd347 cmd one", "syrd347 cmd two"]

        return dict(
            publication_uses_github=lambda remote, *, recorded_host_alias:
                L.append(("github?", remote, recorded_host_alias)) or self.github,
            resolve_owner_github_identity=identity,
            owner_github_key_path=lambda home, *, key_name: f"{home}/.ssh/{key_name}",
            owner_github_identity_commands=commands,
        )

    def remote(self):
        def resolve(project, *, registration_root, declared_remote):
            self.log.append(("remote", project, registration_root, declared_remote))
            return "git@github.com:syrd/p347.git", ""
        return resolve

    def runner(self, argv, **kw):
        self.log.append(("run", list(argv), kw))
        return subprocess.CompletedProcess(argv, self.script, "", "syrd347: key refused\n")

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def u5(ids: Identities, *, dry_run: bool = False, complete: bool = False, detail: str = "",
       trusted: object = RELEASE_ROOT, source_repo: object = SOURCE):
    from scripts import team_launcher, upgrade_phases
    from scripts.ticket_board import project_provision, publication_boundary

    said: list[str] = []
    config = SimpleNamespace(project=PROJECT, run_as_user="syrd347-owner", name="incoming")
    report_config = SimpleNamespace(project=PROJECT, name="report")
    kwargs = dict(commit_git_dir="syrd347-commit", config_path=CONFIG_PATH,
                  cutover=SimpleNamespace(is_complete=complete, name="incoming-cutover"), deploy_ref="v347",
                  desktop_choice="syrd347-desktop", dry_run=dry_run, effective_source_repo=SOURCE,
                  print_func=said.append, publication_detail=detail, publish_remote="syrd347-remote",
                  release_report_config=report_config, runner=ids.runner, source_repo=source_repo,
                  tooling_root=TOOLING, trusted_release_root=trusted)
    with patched(team_launcher, **ids.launcher()), patched(project_provision, **ids.provision()), \
            patched(publication_boundary, resolve_pinned_remote=ids.remote()), patched(os, geteuid=lambda: ids.euid):
        result = upgrade_phases._upgrade_identities_and_accounts(config, **kwargs)
    return result, said, config, report_config


def test_u5_reads_its_lookups_through_the_launcher_and_keeps_its_imports_local() -> None:
    module = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                    and n.name == "_upgrade_identities_and_accounts")
    for name, count in U5_SEAMS.items():
        through = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name
                   and isinstance(n.value, ast.Name) and n.value.id == "launcher"]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(through) == count and not bare, f"U5 reads {name} at its {count} site(s), through the launcher")
    local = sorted((n.module, a.name) for n in ast.walk(function) if isinstance(n, ast.ImportFrom)
                   and n.module != "scripts" for a in n.names)
    qualified = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                        and isinstance(n.value, ast.Name) and n.value.id == "launcher"
                        and n.attr in {name for _, name in U5_LOCAL}})
    check(local == U5_LOCAL and qualified == [], f"the provision and publication imports stay U5's own: {local}")
    bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check(through == [], f"nothing U5 binds is read as the launcher's: {through}")
    from scripts.upgrade_phases import UpgradeIdentitiesDone
    fields = dataclasses.fields(UpgradeIdentitiesDone)
    check([f.name for f in fields] == ["config", "release_report_config"] and UpgradeIdentitiesDone.__dataclass_params__.frozen
          and all(f.default is dataclasses.MISSING for f in fields), f"a frozen two-field continuation: {fields}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    upgrade = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade_project_command")
    at = next((i for i, n in enumerate(upgrade.body) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
               and ast.unparse(n.value.func).endswith("_upgrade_identities_and_accounts")), None)
    check(at is not None and isinstance(upgrade.body[at].value.func, ast.Name),
          "the upgrade calls U5 by the launcher's own (patchable) name")
    wiring = [ast.unparse(n) for n in upgrade.body[at + 1:at + 4]]
    check(at == 47 and wiring == ["if not isinstance(identities_done, UpgradeIdentitiesDone):\n    return identities_done",
                                  "config = identities_done.config",
                                  "release_report_config = identities_done.release_report_config"],
          f"at U5's old position, a refusal is returned before either value is read: {at} {wiring}")
    keywords = {k.arg: ast.unparse(k.value) for k in upgrade.body[at].value.keywords}
    check(keywords.get("release_report_config") == "release_report_config" and
          ast.unparse(upgrade.body[at].value.args[0]) == "config",
          f"each carry is the caller's current value: {keywords}")


def test_u5_a_local_tenant_manages_no_github_identity() -> None:
    ids = Identities(github=False, ready=True)
    result, said, config, report_config = u5(ids, complete=True)
    check(ids.kinds()[:6] == ["owner-home", "plan", "provision-root", "remote", "github?", "identity"]
          and ids.log[3] == ("remote", PROJECT, Path("/nonexistent/syrd347/root"), "syrd347-remote")
          and ids.log[4] == ("github?", "git@github.com:syrd/p347.git", "gh-347"),
          f"root's pinned remote decides, from root's registration root, before the plan's identity; an owner in the "
          f"config means the current user is never asked: {ids.log[:6]}")
    check(said[:2] == [f"switchyard: {PROJECT} publishes to git@github.com:syrd/p347.git, not GitHub, so no owner GitHub "
                       "identity is selected, configured or checked",
                       f"switchyard: {PROJECT}'s plan still records a GitHub identity it does not use; clear it with "
                       f"`sudo switchyard set-owner-identity {PROJECT} --clear` (try --dry-run first)"]
          and not {"commands", "run", "status"} & set(ids.kinds()),
          f"a local tenant is told so, pointed at the stale plan, and nothing about GitHub is done: {said}")
    check(result.config is config and result.release_report_config is report_config,
          f"no transaction: both carries come back as the very objects they went in as: {result}")
    ids = Identities(github=False, plan={})
    _, said, _, _ = u5(ids, complete=True)
    check(len([s for s in said if "still records" in s]) == 0, "a plan with no identity gets no remedy")


def test_u5_no_configured_owner_means_the_current_user() -> None:
    from scripts import team_launcher, upgrade_phases
    from scripts.ticket_board import project_provision, publication_boundary
    ids = Identities()
    said: list[str] = []
    config = SimpleNamespace(project=PROJECT, run_as_user=None)
    with patched(team_launcher, **ids.launcher()), patched(project_provision, **ids.provision()), \
            patched(publication_boundary, resolve_pinned_remote=ids.remote()), patched(os, geteuid=lambda: 0):
        upgrade_phases._upgrade_identities_and_accounts(
            config, commit_git_dir=None, config_path=CONFIG_PATH, cutover=SimpleNamespace(is_complete=True),
            deploy_ref=None, desktop_choice=None, dry_run=True, effective_source_repo=SOURCE, print_func=said.append,
            publication_detail="", publish_remote="", release_report_config=config, runner=ids.runner, source_repo=None,
            tooling_root=None, trusted_release_root=None)
    check("current-user" in ids.kinds() and said[0].startswith("switchyard: would keep syrd347-me's GitHub identity"),
          f"with no owner configured, the identity is the current user's: {said[:1]}")


def test_u5_an_unresolved_identity_is_left_untouched() -> None:
    ids = Identities(github=None, resolved=False)
    _, said, _, _ = u5(ids, complete=True)
    check(said[:2] == ["warning: switchyard: syrd347: two keys, none named",
                       f"warning: switchyard: {PROJECT}'s owner GitHub identity was left untouched, so publication "
                       "continues with whatever is already configured."]
          and not {"commands", "run", "status"} & set(ids.kinds()), f"warned, nothing rendered or checked: {said}")


def test_u5_a_dry_run_keeps_and_writes_nothing() -> None:
    ids = Identities(ready=True)
    result, said, config, report_config = u5(ids, dry_run=True, complete=False)
    check(said[0] == f"switchyard: would keep syrd347-owner's GitHub identity on {OWNER_HOME}/.ssh/id_347, from the plan"
          and not {"commands", "run", "status", "pending", "reload", "recalculate"} & set(ids.kinds()),
          f"a dry run says what it would keep and provisions, checks and writes nothing: {said} {ids.kinds()}")
    artifacts = next(e for e in ids.log if e[0] == "journal")
    check(artifacts[2] == dict(config_path=CONFIG_PATH, phase="artifacts", state="done", detail="", dry_run=True),
          f"the artifacts verdict is still recorded, as a dry run: {artifacts}")
    transaction = next(e for e in ids.log if e[0] == "transaction")
    check(transaction[2]["dry_run"] is True and result.config is config and result.release_report_config is report_config,
          f"a dry-run transaction reloads nothing and hands both carries back: {result}")


def test_u5_root_provisions_the_identity_then_reads_it_back() -> None:
    for code, remedy in ((0, ""), (3, "SYRD347 REMEDY")):
        ids = Identities(script=code, remedy=remedy)
        _, said, config, _ = u5(ids, complete=True)
        commands = next(e for e in ids.log if e[0] == "commands")
        check(commands[1:] == ("syrd347-owner", str(OWNER_HOME), dict(key_name="id_347", host="github.com",
                                                                        host_alias="gh-347",
                                                                        comment=f"syrd347-owner switchyard {PROJECT}")),
              f"the selected key, host and alias, with the owner's comment: {commands}")
        run = next(e for e in ids.log if e[0] == "run")
        check(run[1:] == (["sh", "-c", "set -eu\nsyrd347 cmd one\nsyrd347 cmd two"],
                          dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)),
              f"run once, as a set -eu script: {run}")
        check("status" in ids.kinds(),
              f"the script's result does not stop the upgrade; the identity is still read back: {ids.kinds()}")
        status = next(e for e in ids.log if e[0] == "status")
        check(status[1:] == ("syrd347-owner", OWNER_HOME, dict(key_name="id_347", host="github.com", runner=ids.runner))
              and ids.kinds().index("status") > ids.kinds().index("run"),
              f"then read back against the same key and host: {status}")
        if code:
            check(said[0] == "switchyard: could not provision syrd347-owner's GitHub identity (exit 3): syrd347: key refused"
                  and said[1] == "SYRD347 REMEDY", f"a failed script is said, and the upgrade goes on: {said}")
        else:
            check(said[0] == "switchyard: syrd347-owner can publish to GitHub as its own identity (id_347, from the plan)",
                  f"a working identity is reported: {said}")
    ids = Identities(euid=1000)
    u5(ids, complete=True)
    check("run" not in ids.kinds() and "status" in ids.kinds(), "not root: nothing provisioned, still read back")


def test_u5_pending_identities_then_the_artifacts_verdict() -> None:
    ids = Identities(github=False)
    _, _, config, _ = u5(ids, complete=True, detail="publication boundary not removed: x")
    kinds = ids.kinds()
    artifacts = [e for e in ids.log if e[0] == "journal"][0]
    check(kinds.index("pending") < kinds.index("journal") and ids.log[kinds.index("pending")][1] is config,
          f"pending identities are written before the verdict: {kinds}")
    check(artifacts[2] == dict(config_path=CONFIG_PATH, phase="artifacts", state="incomplete",
                               detail="publication boundary not removed: x", dry_run=False),
          f"an incomplete U4 makes the phase incomplete, with its detail: {artifacts}")


def test_u5_missing_accounts_publish_or_name_the_migration() -> None:
    ids = Identities(github=False, ready=False)
    result, said, config, report_config = u5(ids, dry_run=True)
    check(said[-1] == (f"switchyard: {PROJECT}'s roles still share the project account. An operator must run {MIGRATION} "
                       f"(safe to re-run), then rerun `switchyard upgrade {PROJECT}`, which runs whichever phase is next "
                       "in order.") and "publish" not in ids.kinds()
          and ids.log[-1] == ("journal", config, dict(config_path=CONFIG_PATH, phase="accounts", state="pending",
                                                        dry_run=True)),
          f"a dry run names root's trusted copy and records accounts pending: {said[-1]}")
    check(result.config is config and result.release_report_config is report_config, "and goes on with both carries")
    ids = Identities(github=False, ready=False)
    u5(ids)
    publish = next(e for e in ids.log if e[0] == "publish")
    check(publish[2] == dict(config_path=CONFIG_PATH, resume_source="SYRD347-SOURCE", runner=ids.runner,
                             print_func=publish[2]["print_func"]) and ids.kinds().index("source") < ids.kinds().index("publish"),
          f"root publishes it from the upgrade's recorded source: {publish}")
    ids = Identities(github=False, ready=False, published=(None, ["syrd347: not vouched"]))
    result, said, _, _ = u5(ids)
    check(result == 1 and type(result) is int and said[-2:] == [
        "switchyard: syrd347: not vouched",
        f"switchyard: {PROJECT}'s roles still share the project account, and its role-account migration is not an "
        "artifact root can vouch for, so it is not being handed to an operator to run."]
          and ids.log[-1][2] == dict(config_path=CONFIG_PATH, phase="accounts", state="blocked", detail="syrd347: not vouched"),
          f"a migration root cannot vouch for stops the upgrade with 1, journalled blocked: {result!r} {said[-2:]}")
    ids = Identities(github=False, ready=False, euid=1000, instruction=(None, ["syrd347: not published yet"]))
    _, said, _, _ = u5(ids)
    check("publish" not in ids.kinds() and said[-1] == (
        f"switchyard: {PROJECT}'s roles still share the project account. An operator must run `sudo switchyard upgrade "
        f"{PROJECT}`, which publishes the role-account migration where only root can write it: syrd347: not published "
        f"yet, then rerun `switchyard upgrade {PROJECT}`, which runs whichever phase is next in order."),
          f"not root: the command that publishes it, and no root write: {said[-1]}")


def test_u5_ready_accounts_run_the_transaction_and_reload() -> None:
    ids = Identities(github=False)
    result, said, config, report_config = u5(ids)
    transaction = next(e for e in ids.log if e[0] == "transaction")
    check(transaction[1] is config and transaction[2] == dict(
        config_path=CONFIG_PATH, dry_run=False, runner=ids.runner, source_repo=RELEASE_ROOT,
        commit_git_dir="syrd347-commit", deploy_ref="v347", tooling_dir=Path(f"{TOOLING}/staged"),
        print_func=transaction[2]["print_func"]), f"the transaction, against the verified release: {transaction}")
    after = ids.kinds()[ids.kinds().index("transaction"):]
    check(after == ["transaction", "reload", "recalculate"] and ids.log[-1][1] is ids.reloaded,
          f"then the configuration reloaded and the cutover recalculated from it: {after}")
    check(result.config is ids.reloaded and result.release_report_config is ids.reloaded,
          f"both continuation values are the reloaded configuration: {result}")
    check(("journal", config, dict(config_path=CONFIG_PATH, phase="accounts", state="done", dry_run=False)) in ids.log,
          "the accounts phase is recorded done first")
    for trusted, source_repo, want in ((None, SOURCE, SOURCE), (None, None, None), (RELEASE_ROOT, None, RELEASE_ROOT)):
        ids = Identities(github=False)
        u5(ids, trusted=trusted, source_repo=source_repo)
        got = next(e for e in ids.log if e[0] == "transaction")[2]["source_repo"]
        check(got == want, f"the verified release first, else the source only when one was given: {trusted} {source_repo} {got}")


def test_u5_a_failed_transaction_stops_after_reloading() -> None:
    code = Code(4)
    ids = Identities(github=False, transaction=code)
    result, said, config, _ = u5(ids)
    check(result is code, f"the transaction's own code is returned, the very object: {result!r}")
    check(ids.kinds()[-5:] == ["transaction", "reload", "recalculate", "read-journal", "report"],
          f"reloaded and recalculated before stopping, then reported: {ids.kinds()}")
    report = ids.log[-1]
    check(report[1] is ids.reloaded and report[2] == dict(config_path=CONFIG_PATH, cutover=ids.recalculated,
                                                           journal=ids.journal, desktop_policy="syrd347-desktop",
                                                           dry_run=False)
          and ids.log[-2] == ("read-journal", ids.reloaded, True),
          f"the report reads the new configuration, cutover and trusted journal: {report}")
    check(said[-2].startswith(f"switchyard: stopping: {PROJECT}'s identities phase did not complete") and
          said[-1] == "SYRD347 REPORT" and not any(e[0] == "journal" and e[2].get("phase") == "identities" for e in ids.log),
          f"said, reported, and the identities phase never recorded done: {said[-2:]}")
    ids = Identities(github=False, transaction=Code(5))
    result, _, config, _ = u5(ids, dry_run=True)
    check(result == 5 and "reload" not in ids.kinds() and ids.log[-1][1] is config,
          f"a dry-run failure reports against the configuration it had: {ids.kinds()}")


def test_u5_a_complete_cutover_records_identities_done() -> None:
    ids = Identities(github=False)
    result, _, config, report_config = u5(ids, complete=True)
    check("transaction" not in ids.kinds() and ids.log[-1] == ("journal", config, dict(
        config_path=CONFIG_PATH, phase="identities", state="done", dry_run=False))
          and result.config is config and result.release_report_config is report_config,
          f"nothing to cut over: identities recorded done, both carries unchanged: {ids.log[-1]}")


def test_u5_an_error_reaches_the_caller() -> None:
    boom = OSError("syrd347: transaction raised")
    ids = Identities(github=False, error=boom)
    try:
        u5(ids); raised = None
    except OSError as exc:
        raised = exc
    check(raised is boom and ids.kinds()[-1] == "transaction", f"not caught, nothing after it: {ids.kinds()}")


def run_u5_wiring(answer: object):
    """The upgrade's own U5 call, dispatch and unpacking, compiled from the launcher, against a stand-in U5."""
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    upgrade = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade_project_command")
    names = ["config", "commit_git_dir", "config_path", "cutover", "deploy_ref", "desktop_choice", "dry_run",
             "effective_source_repo", "print_func", "publication_detail", "publish_remote", "release_report_config",
             "runner", "source_repo", "tooling_root", "trusted_release_root"]
    code = ast.Module(body=[ast.FunctionDef(
        name="wiring", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=n) for n in names], kwonlyargs=[],
                                          kw_defaults=[], defaults=[]),
        body=[*upgrade.body[47:51], ast.parse("return ('went on', config, release_report_config)").body[0]],
        decorator_list=[], returns=None, type_params=[])], type_ignores=[])
    from scripts.upgrade_phases import UpgradeIdentitiesDone
    asked: list[tuple] = []
    namespace = {"UpgradeIdentitiesDone": UpgradeIdentitiesDone,
                 "_upgrade_identities_and_accounts": lambda config, **k: asked.append((config, k)) or answer}
    exec(compile(ast.fix_missing_locations(code), "upgrade_project_command", "exec"), namespace)
    config, report_config = SimpleNamespace(name="cfg"), SimpleNamespace(name="report")
    result = namespace["wiring"](config, "c", CONFIG_PATH, "cut", "v", "d", False, SOURCE, print, "", "", report_config,
                                 refusing_runner, None, TOOLING, None)
    return result, asked, config, report_config


def test_the_upgrade_returns_u5s_refusal_and_unpacks_its_continuation() -> None:
    code = Code(4)
    result, asked, config, report_config = run_u5_wiring(code)
    check(result is code and asked[0][0] is config and asked[0][1]["release_report_config"] is report_config,
          f"a refusal is returned as the very object, before anything after U5: {result!r}")
    from scripts.upgrade_phases import UpgradeIdentitiesDone
    new = SimpleNamespace(name="new")
    result, _, _, _ = run_u5_wiring(UpgradeIdentitiesDone(config=new, release_report_config=new))
    check(result == ("went on", new, new), f"going on, both values are taken: {result!r}")


# --- U6: finish (SYRD-348) -----------------------------------------------------------------------------------------

#: U6's launcher lookups and how many times each is read (measured on the SYRD-348 baseline: 14 reads of 13 names).
U6_SEAMS = {"director_onboarding_state": 1, "record_upgrade_phase": 2, "role_account_cutover": 1,
            "prepare_tenant_release_root": 1, "owner_release_root_problems": 1, "report_tenant_release_upgrade": 1,
            "record_release_phase_from_status": 1, "release_update_blocked": 1, "read_upgrade_journal": 1,
            "upgrade_phase_report": 1, "outstanding_release_phase_report": 1, "unsafe_root_presentation_windows": 1,
            "unsafe_presentation_report": 1}


class Finish:
    """U6's launcher facilities, answering from objects this test owns, into one ordered log."""

    def __init__(self, *, euid: int = 0, director: tuple = ("done", ""), complete: bool = True,
                 root_problems: list | None = None, deployed: bool = False, blocked: str = "",
                 unsafe: list | None = None, error: Exception | None = None) -> None:
        self.euid, self.director, self.complete = euid, director, complete
        self.root_problems, self.deployed, self.blocked = root_problems or [], deployed, blocked
        self.unsafe, self.error = unsafe or [], error
        self.cutover = SimpleNamespace(is_complete=complete, name="fresh cutover")
        self.status = SimpleNamespace(name="release status")
        self.journal = SimpleNamespace(name="trusted journal")
        self.log: list[tuple] = []

    def names(self) -> dict[str, object]:
        L = self.log

        def report(config, **kw):
            L.append(("release-report", config, kw))
            if self.error:
                raise self.error
            return self.status

        return dict(
            director_onboarding_state=lambda config, *, config_path: L.append(("director", config)) or self.director,
            record_upgrade_phase=lambda config, **kw: L.append(("journal", config, kw)),
            role_account_cutover=lambda config, *, runner: L.append(("cutover", config, runner)) or self.cutover,
            prepare_tenant_release_root=lambda config, *, dry_run, print_func:
                L.append(("prepare-root", config, dry_run)) or self.root_problems,
            owner_release_root_problems=lambda config: L.append(("owner-root", config)) or self.root_problems,
            report_tenant_release_upgrade=report,
            record_release_phase_from_status=lambda config, **kw: L.append(("record-status", config, kw)) or self.deployed,
            release_update_blocked=lambda status: L.append(("blocked?", status)) or self.blocked,
            read_upgrade_journal=lambda config, *, config_path, trusted: L.append(("read-journal", config, trusted))
                or self.journal,
            upgrade_phase_report=lambda config, **kw: L.append(("phase-report", config, kw)) or ["SYRD348 PHASES"],
            outstanding_release_phase_report=lambda config, *, config_path, journal:
                L.append(("outstanding", config, journal)) or ["SYRD348 OUTSTANDING"],
            unsafe_root_presentation_windows=lambda config, *, config_path: L.append(("unsafe?", config)) or self.unsafe,
            unsafe_presentation_report=lambda config, windows: L.append(("unsafe-report", windows)) or "SYRD348 UNSAFE",
        )

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def u6(fin: Finish, *, dry_run: bool = False):
    from scripts import team_launcher, upgrade_phases

    said: list[str] = []
    config = SimpleNamespace(project=PROJECT, name="current")
    report_config = SimpleNamespace(project=PROJECT, name="report")
    with patched(team_launcher, **fin.names()), patched(os, geteuid=lambda: fin.euid):
        result = upgrade_phases._finish_upgrade(
            config, commit_git_dir="syrd348-commit", config_path=CONFIG_PATH, deploy_ref="v348",
            desktop_choice="syrd348-desktop", dry_run=dry_run, effective_source_repo=SOURCE, print_func=said.append,
            release_report_config=report_config, runner=refusing_runner)
    return result, said, config, report_config


def test_u6_reads_its_lookups_through_the_launcher_and_ends_the_upgrade() -> None:
    module = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_finish_upgrade")
    for name, count in U6_SEAMS.items():
        through = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name
                   and isinstance(n.value, ast.Name) and n.value.id == "launcher"]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(through) == count and not bare, f"U6 reads {name} at its {count} site(s), through the launcher")
    bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound | {"os"}})
    check(through == [], f"nothing U6 binds, nor os, is read as the launcher's: {through}")
    check([ast.unparse(n) for n in function.body[-2:]][-1] == "return 0", "the upgrade's own final `return 0` ends U6")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    upgrade = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade_project_command")
    tail = upgrade.body[-1]
    check(len(upgrade.body) == 52 and isinstance(tail, ast.Return) and isinstance(tail.value, ast.Call)
          and isinstance(tail.value.func, ast.Name) and tail.value.func.id == "_finish_upgrade"
          and {k.arg: ast.unparse(k.value) for k in tail.value.keywords}.get("release_report_config") == "release_report_config",
          f"the upgrade ends by returning U6's answer, called by the launcher's own name at U6's old position: "
          f"{ast.unparse(tail)[:80]}")


def test_u6_a_complete_release_as_root_in_order() -> None:
    fin = Finish(deployed=True)
    result, said, config, report_config = u6(fin)
    check(fin.kinds() == ["director", "journal", "cutover", "prepare-root", "release-report", "record-status", "blocked?",
                          "read-journal", "phase-report", "outstanding", "unsafe?"],
          f"director, fresh cutover, root, release report and record, then the reports, then unsafe windows: {fin.kinds()}")
    check(fin.log[1] == ("journal", config, dict(config_path=CONFIG_PATH, phase="director", state="done", detail="",
                                                 dry_run=False)) and fin.log[2][1] is config
          and fin.log[3] == ("prepare-root", config, False),
          f"the director phase recorded, the cutover asked afresh of the current config: {fin.log[:4]}")
    check(fin.log[4][1] is report_config and fin.log[4][2] == dict(
        config_path=CONFIG_PATH, source_repo=SOURCE, commit_git_dir="syrd348-commit", deploy_ref="v348",
        runner=refusing_runner, print_func=fin.log[4][2]["print_func"])
          and fin.log[5] == ("record-status", config, dict(config_path=CONFIG_PATH, status=fin.status, dry_run=False)),
          f"the release is reported for the ORIGINAL report config, recorded against the current one: {fin.log[4:6]}")
    check(fin.log[7] == ("read-journal", config, True)
          and fin.log[8] == ("phase-report", config, dict(config_path=CONFIG_PATH, cutover=fin.cutover,
                                                          journal=fin.journal, desktop_policy="syrd348-desktop",
                                                          dry_run=False)),
          f"the trusted journal and the fresh cutover are what is reported: {fin.log[7:9]}")
    check(result == 0 and type(result) is int and said == ["SYRD348 PHASES", "SYRD348 OUTSTANDING"],
          f"a clean finish answers 0: {result!r} {said}")


def test_u6_the_release_root_as_owner_and_its_problems() -> None:
    fin = Finish(euid=1000)
    u6(fin)
    check("owner-root" in fin.kinds() and "prepare-root" not in fin.kinds(), "not root: the owner's own check")
    fin = Finish(root_problems=["syrd348: no board root"], unsafe=["window"])
    result, said, config, _ = u6(fin)
    check(not {"release-report", "record-status", "blocked?"} & set(fin.kinds())
          and ("journal", config, dict(config_path=CONFIG_PATH, phase="release", state="blocked",
                                       detail="syrd348: no board root", dry_run=False)) in fin.log,
          f"root problems withhold the release and record it blocked: {fin.kinds()}")
    check(result == 1 and said == [
        "switchyard: syrd348: no board root",
        f"switchyard: withholding {PROJECT}'s release deploy sequence: its owner cannot publish a release under the "
        "board root yet. Nothing was deployed, and no listener needs stopping.",
        "SYRD348 PHASES", "SYRD348 OUTSTANDING", "SYRD348 UNSAFE",
        f"switchyard: {PROJECT}'s release phase did not complete: syrd348: no board root. Nothing after it is claimed."],
          f"the reports and unsafe windows still come before the blocked line and the 1: {result!r} {said}")


def test_u6_a_blocked_release_after_everything_else() -> None:
    fin = Finish(blocked="syrd348: listener still up", unsafe=["window"])
    result, said, _, _ = u6(fin)
    check(result == 1 and said[-2:] == ["SYRD348 UNSAFE", f"switchyard: {PROJECT}'s release phase did not complete: "
                                         "syrd348: listener still up. Nothing after it is claimed."],
          f"blocked by its status: said last, after the unsafe report, and 1: {said}")


def test_u6_an_incomplete_cutover_withholds_the_release() -> None:
    fin = Finish(complete=False, director=("pending", ""))
    result, said, _, _ = u6(fin)
    check(not {"prepare-root", "owner-root", "release-report"} & set(fin.kinds()) and result == 0,
          f"no release is attempted and the upgrade still answers 0: {fin.kinds()}")
    check(said[:2] == [
        f"switchyard: withholding the {PROJECT} release deploy instruction until its legacy role state is repatriated to "
        "the project account: the release enforces process-bound authority and must not strand a resumable pane.",
        f"switchyard: once its roles are on their own accounts and the release is deployed, the director runs "
        f"`switchyard finish-upgrade {PROJECT}` from their own session (outstanding); root cannot make that write and "
        "will not pretend to."], f"withheld, and the director told when: {said[:2]}")


def test_u6_the_director_phase_and_instruction() -> None:
    for state, recorded in (("done", "done"), ("not required", "not required"), ("pending", "pending"),
                            ("unknown", "pending"), ("syrd348-other", "pending")):
        fin = Finish(director=(state, "syrd348 reason"))
        _, said, _, _ = u6(fin)
        check(fin.log[1][2]["state"] == recorded and fin.log[1][2]["detail"] == "syrd348 reason",
              f"{state!r} is recorded as {recorded!r}, with its reason: {fin.log[1]}")
        told = [s for s in said if "finish-upgrade" in s]
        check(bool(told) == (state in {"pending", "unknown"}), f"{state!r}: the director is told only when outstanding")
    fin = Finish(director=("pending", "syrd348 reason"), deployed=True)
    _, said, _, _ = u6(fin)
    check(said[0] == (f"switchyard: {PROJECT}'s board release is deployed and no further deploy is needed. The remaining "
                      f"step is the director's: the director runs `switchyard finish-upgrade {PROJECT}` from their own "
                      "session (syrd348 reason); root cannot make that write and will not pretend to."),
          f"deployed: no further deploy is needed: {said[0]}")
    fin = Finish(director=("unknown", ""), deployed=False)
    _, said, _, _ = u6(fin)
    check(said[0].startswith("switchyard: after that deploy, the director runs") and "(outstanding)" in said[0],
          f"not deployed: after that deploy, the reason falling back to 'outstanding': {said[0]}")


def test_u6_a_dry_run_reports_nothing_outstanding() -> None:
    fin = Finish()
    result, said, _, _ = u6(fin, dry_run=True)
    check("outstanding" not in fin.kinds() and ("prepare-root", fin.log[3][1], True) == fin.log[3]
          and fin.log[8][2]["dry_run"] is True and result == 0 and said == ["SYRD348 PHASES"],
          f"a dry run prepares as a dry run and names nothing outstanding: {fin.kinds()} {said}")


def test_u6_an_error_reaches_the_caller() -> None:
    boom = OSError("syrd348: report raised")
    fin = Finish(error=boom)
    try:
        u6(fin); raised = None
    except OSError as exc:
        raised = exc
    check(raised is boom and fin.kinds()[-1] == "release-report", f"not caught, nothing after it: {fin.kinds()}")


def test_the_upgrade_returns_u6s_answer_after_u5() -> None:
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    upgrade = next(n for n in launcher.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade_project_command")
    names = ["config", "commit_git_dir", "config_path", "cutover", "deploy_ref", "desktop_choice", "dry_run",
             "effective_source_repo", "print_func", "publication_detail", "publish_remote", "release_report_config",
             "runner", "source_repo", "tooling_root", "trusted_release_root"]
    code = ast.Module(body=[ast.FunctionDef(
        name="wiring", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=n) for n in names], kwonlyargs=[],
                                          kw_defaults=[], defaults=[]),
        body=upgrade.body[47:52], decorator_list=[], returns=None, type_params=[])], type_ignores=[])
    from scripts.upgrade_phases import UpgradeIdentitiesDone
    for u5_answer in (Code(4), "continue"):
        asked: list[tuple] = []
        final = Code(9)
        reloaded = SimpleNamespace(name="reloaded")
        answer = (UpgradeIdentitiesDone(config=reloaded, release_report_config=reloaded)
                  if u5_answer == "continue" else u5_answer)
        namespace = {"UpgradeIdentitiesDone": UpgradeIdentitiesDone,
                     "_upgrade_identities_and_accounts": lambda config, **k: answer,
                     "_finish_upgrade": lambda config, **k: asked.append((config, k)) or final}
        exec(compile(ast.fix_missing_locations(code), "upgrade_project_command", "exec"), namespace)
        result = namespace["wiring"](SimpleNamespace(name="cfg"), "c", CONFIG_PATH, "cut", "v", "d", False, SOURCE,
                                     print, "", "", SimpleNamespace(name="report"), refusing_runner, None, TOOLING, None)
        if u5_answer == "continue":
            check(result is final and asked[0][0] is reloaded and asked[0][1]["release_report_config"] is reloaded
                  and asked[0][1]["desktop_choice"] == "d",
                  f"going on, U6 gets U5's values and its answer is the upgrade's: {result!r}")
        else:
            check(result is u5_answer and asked == [], f"a U5 stop never reaches U6: {result!r} {asked}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_phases_own_names_and_the_call_site",
             "test_u5_reads_its_lookups_through_the_launcher_and_keeps_its_imports_local",
             "test_u6_reads_its_lookups_through_the_launcher_and_ends_the_upgrade")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"upgrade_phases_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
