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
EXPORTED = ("UpgradeToolingStaged", "_stage_upgrade_tooling")
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


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_phases_own_names_and_the_call_site")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"upgrade_phases_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
