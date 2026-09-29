#!/usr/bin/env python3
"""SYRD-352: `switchyard finish-upgrade`, the director's half of an upgrade, against the launcher it came out of.

`finish_upgrade_command` moved into `scripts/director_upgrade.py` unchanged.
This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- The launcher re-exports the command, the very same object whichever module is
  imported first, and `switchyard finish-upgrade` dispatches to it.
- **Seams (rule 24).** Every launcher facility the command uses -- nineteen
  names, `_finish_upgrade_preview` among them -- is read from the launcher when
  it runs, because suites patch them there. `os` and `subprocess` are the
  module's own, the same module objects, so `team_launcher.os.geteuid` patches
  and the `subprocess.run` default are unchanged; `print` is the builtin.
  Nothing the command binds itself is read through the launcher (rule 27).
- **The behaviour is unchanged:** root refused before anything is read; the
  pinned release reported, or, with nothing named or pinned, the release root
  staged for the roles; the control role and the account the board authorizes
  checked before a dry run's preview, whose answer is returned as the very
  object; the handed-off workflow installed (a refusal answering 1, a real
  install reloading), the onboarding migrated and its OBSERVED state journalled
  from a fresh config; an incomplete cutover answering 0 and the owner's release
  root problems 1 before any deploy instruction; the release reported for the
  normalised source, observed, its divergence said, and a blocked release said
  after that with the installed-workflow reminder, answering 1.

Every facility is this test's own recording fake and the effective uid is a
patched answer: no board, journal, workflow, release, account, key or home is
touched.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
#: The launcher's names the command reads when it runs, and how many times (measured on the SYRD-352 baseline).
SEAMS = {"resolve_pinned_upgrade_source": 1, "upgrade_source_unavailable_reason": 1, "director_readable_pinned_release": 1,
         "control_role_name": 1, "current_user_name": 2, "_role_by_name": 1, "_finish_upgrade_preview": 1,
         "install_handed_off_workflow": 1, "load_project_config": 2, "migrate_declarative_director_onboarding": 1,
         "director_onboarding_state": 1, "record_upgrade_phase": 1, "role_account_cutover": 1,
         "owner_release_root_problems": 1, "report_tenant_release_upgrade": 1, "_repo_root": 1,
         "record_release_phase_from_status": 1, "director_release_divergence_report": 1, "release_update_blocked": 1}
PROJECT = "p352"
CONFIG_PATH = Path("/nonexistent/syrd352/p352.json")
OWNER = "syrd352-owner"
DIRECTOR_ACCOUNT = "syrd352-director"
REPO = Path("/nonexistent/syrd352/repo")
#: What the resolver answers: a `..` in the source, so normalising it changes something.
RESOLVED_SOURCE = Path("/nonexistent/syrd352/checkouts/../resolved")
NORMALISED_SOURCE = Path("/nonexistent/syrd352/resolved")
STAGED = Path("/nonexistent/syrd352/releases/../staged")


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


class Code(int):
    """An exit code with an identity, so an answer can be shown to be the very object returned."""


def refusing_runner(argv, **kwargs):
    raise AssertionError(f"finish-upgrade runs nothing itself; its runner is only handed on: {argv}")


class Finish:
    """finish-upgrade's launcher facilities and the effective uid, answering from this test's objects, into one log."""

    def __init__(self, *, euid: int = 1000, pinned: str = "", staged: bool = False, director: str = "director",
                 account: str = DIRECTOR_ACCOUNT, role: bool = True, caller: str = DIRECTOR_ACCOUNT,
                 preview: object = None, installed: object = None, state: str = "done", reason: str = "",
                 complete: bool = True, root_problems: list | None = None, divergence: list | None = None,
                 blocked: str = "", error: Exception | None = None) -> None:
        self.euid, self.pinned, self.staged = euid, pinned, staged
        self.director, self.account, self.role, self.caller = director, account, role, caller
        self.preview = Code(7) if preview is None else preview
        self.installed, self.state, self.reason, self.complete = installed, state, reason, complete
        self.root_problems, self.divergence = root_problems or [], divergence or []
        self.blocked, self.error = blocked, error
        self.reloads = [SimpleNamespace(project=PROJECT, run_as_user=OWNER, name=f"reload-{i}") for i in range(3)]
        self.status = SimpleNamespace(name="syrd352-status")
        self.log: list[tuple] = []

    def names(self) -> dict[str, object]:
        L = self.log
        reloads = iter(self.reloads)

        def resolve(config, *, source_repo, commit_git_dir, deploy_ref):
            L.append(("resolve", dict(source_repo=source_repo, commit_git_dir=commit_git_dir, deploy_ref=deploy_ref)))
            return RESOLVED_SOURCE, "syrd352-cache", "syrd352-ref", self.pinned

        def install(config, *, config_path, caller_role, print_func):
            L.append(("install", config, caller_role))
            if self.error:
                raise self.error
            return self.installed

        return dict(
            resolve_pinned_upgrade_source=resolve,
            upgrade_source_unavailable_reason=lambda config: L.append(("unavailable?",)) or "root's record is unreadable",
            director_readable_pinned_release=lambda project: L.append(("staged?", project))
                or ((STAGED, "syrd352-staged-commit", "") if self.staged else (None, "", "nothing is staged")),
            control_role_name=lambda config, *, config_path: L.append(("control?", config_path))
                or (self.director, "no control role is declared"),
            current_user_name=lambda: L.append(("caller?",)) or self.caller,
            _role_by_name=lambda config, name: L.append(("role?", name))
                or (SimpleNamespace(run_as_user=self.account) if self.role else None),
            _finish_upgrade_preview=lambda config, **kw: L.append(("preview", config, kw)) or self.preview,
            install_handed_off_workflow=install,
            load_project_config=lambda project, path: L.append(("reload", project, path)) or next(reloads),
            migrate_declarative_director_onboarding=lambda config, *, config_path, print_func:
                L.append(("onboarding", config)) or False,
            director_onboarding_state=lambda config, *, config_path: L.append(("state?", config))
                or (self.state, self.reason),
            record_upgrade_phase=lambda config, **kw: L.append(("journal", config, kw)),
            role_account_cutover=lambda config, *, runner: L.append(("cutover?", config, runner))
                or SimpleNamespace(is_complete=self.complete),
            owner_release_root_problems=lambda config: L.append(("root-problems?", config)) or list(self.root_problems),
            report_tenant_release_upgrade=lambda config, **kw: L.append(("report", config, kw)) or self.status,
            _repo_root=lambda: L.append(("repo-root",)) or REPO,
            record_release_phase_from_status=lambda config, *, config_path, status: L.append(("observe", config, status)),
            director_release_divergence_report=lambda config, *, config_path: L.append(("divergence?", config))
                or list(self.divergence),
            release_update_blocked=lambda status: L.append(("blocked?", status)) or self.blocked,
        )

    def geteuid(self) -> int:
        self.log.append(("euid?",))
        return self.euid

    def kinds(self) -> list[str]:
        return [e[0] for e in self.log]


def finish(fin: Finish, *, dry_run: bool = False, source_repo: object = None, commit_git_dir: object = None,
           deploy_ref: object = None, run_as_user: str = OWNER):
    from scripts import director_upgrade, team_launcher

    said: list[str] = []
    config = SimpleNamespace(project=PROJECT, run_as_user=run_as_user, name="incoming")
    with patched(team_launcher, **fin.names()), patched(os, geteuid=fin.geteuid):
        result = director_upgrade.finish_upgrade_command(
            config, config_path=CONFIG_PATH, dry_run=dry_run, source_repo=source_repo, commit_git_dir=commit_git_dir,
            deploy_ref=deploy_ref, runner=refusing_runner, print_func=said.append)
    return result, said, config


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.director_upgrade as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module: {result.stdout!r}")


def test_either_import_order_gives_one_object_and_the_same_defaults() -> None:
    for order in (("scripts.director_upgrade", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.director_upgrade")):
        result = python("import importlib, os, subprocess, builtins; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.director_upgrade as d; "
                        "k = d.finish_upgrade_command.__kwdefaults__; "
                        "print(t.finish_upgrade_command is d.finish_upgrade_command, d.os is os, "
                        "k['runner'] is subprocess.run, k['print_func'] is builtins.print, "
                        "t.finish_upgrade_command.__module__)")
        check(result.stdout.strip() == "True True True True scripts.director_upgrade",
              f"{' then '.join(order)}: the launcher's command is the module's, os the one module, the defaults the "
              f"very objects: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_modules_own_names_and_the_dispatch() -> None:
    module = ast.parse((ROOT / "scripts" / "director_upgrade.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "finish_upgrade_command")
    for name, count in SEAMS.items():
        through = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == name
                   and isinstance(n.value, ast.Name) and n.value.id == "launcher"]
        bare = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == name]
        check(len(through) == count and not bare, f"finish-upgrade reads {name} at its {count} site(s), through the launcher")
    through_all = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                          and isinstance(n.value, ast.Name) and n.value.id == "launcher"})
    check(through_all == sorted(SEAMS), f"and nothing else through it: {sorted(set(through_all) - set(SEAMS))}")
    geteuid = [n for n in ast.walk(function) if isinstance(n, ast.Attribute) and n.attr == "geteuid"]
    check(len(geteuid) == 1 and ast.unparse(geteuid[0]) == "os.geteuid", "the uid is asked of the module's own os, once")
    bound = {a.arg for a in ast.walk(function) if isinstance(a, ast.arg)}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    check(not bound & set(through_all), f"nothing the command binds is read as the launcher's: {bound & set(through_all)}")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    main = next(n for n in launcher_body(ROOT, launcher) if isinstance(n, ast.FunctionDef) and n.name == "switchyard_main")
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call) and ast.unparse(n.func) == "finish_upgrade_command"]
    check(len(calls) == 1 and not any(isinstance(n, ast.FunctionDef) and n.name == "finish_upgrade_command"
                                      for n in launcher.body),
          "`switchyard finish-upgrade` dispatches by the launcher's name, and the launcher defines it no more")


def test_the_cli_dispatches_to_the_moved_command() -> None:
    from scripts import team_launcher

    seen: list[tuple] = []
    config = SimpleNamespace(project=PROJECT)
    entry = SimpleNamespace(config_path=CONFIG_PATH, slug=PROJECT)
    with patched(team_launcher, finish_upgrade_command=lambda c, **kw: seen.append(("finish", c, kw)) or Code(9),
                 report_installed_release_version=lambda *a, **k: seen.append(("version",)),
                 _resolve_switchyard_project=lambda project: seen.append(("resolve", project)) or entry,
                 _load_switchyard_project_config_for_command=lambda e, argv: seen.append(("load", e, list(argv))) or config):
        code = team_launcher.switchyard_main(["finish-upgrade", PROJECT, "--dry-run", "--deploy-ref", "v352"])
    check([e[0] for e in seen] == ["version", "resolve", "load", "finish"] and seen[1] == ("resolve", PROJECT)
          and seen[3][1] is config and seen[3][2] == dict(config_path=CONFIG_PATH, dry_run=True, source_repo=None,
                                                          commit_git_dir=None, deploy_ref="v352")
          and isinstance(code, Code) and int(code) == 9,
          f"the CLI reaches the launcher's (patchable) name with the parsed arguments and returns its answer: {seen} {code!r}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_root_is_refused_before_anything_is_read() -> None:
    fin = Finish(euid=0)
    result, said, _ = finish(fin, deploy_ref="v352")
    check(result == 1 and type(result) is int and fin.kinds() == ["euid?"] and said == [
        "switchyard: finish-upgrade makes a director-authority board write and must not run as root. Run it from the "
        "director's own session."], f"root: 1, and no pin, board or account read: {fin.kinds()} {said}")


def test_a_pinned_release_is_reported_and_nothing_is_staged_for() -> None:
    fin = Finish(pinned="syrd352-pin")
    result, said, _ = finish(fin)
    check(fin.kinds()[:2] == ["euid?", "resolve"] and fin.log[1] == ("resolve", dict(source_repo=None, commit_git_dir=None, deploy_ref=None))
          and "staged?" not in fin.kinds() and "unavailable?" not in fin.kinds()
          and said[0] == f"switchyard: reporting the release {PROJECT} was pinned to: syrd352-pin",
          f"the pin is reported and the staged release never asked for: {fin.kinds()} {said[:1]}")
    report = next(e for e in fin.log if e[0] == "report")
    check(report[2] == dict(config_path=CONFIG_PATH, source_repo=NORMALISED_SOURCE, commit_git_dir="syrd352-cache",
                            deploy_ref="syrd352-ref", runner=refusing_runner, print_func=said.append)
          and result == 0 and type(result) is int,
          f"the release is reported for the resolved source normalised, with the resolved cache and ref: {report[2]}")


def test_a_named_source_or_ref_skips_the_staged_fallback_but_a_cache_does_not() -> None:
    for given in (dict(source_repo=Path("/nonexistent/syrd352/given")), dict(deploy_ref="v352")):
        fin = Finish(staged=True)
        _, said, _ = finish(fin, **given)
        check("staged?" not in fin.kinds() and not any("staged" in line or "pinned" in line for line in said),
              f"{given}: named, so nothing is said or staged in its place: {fin.kinds()}")
    fin = Finish(staged=True)
    _, said, _ = finish(fin, commit_git_dir="syrd352-given-cache")
    resolved = next((e for e in fin.log if e[0] == "resolve"), ("resolve", {}))
    check(resolved[1].get("commit_git_dir") == "syrd352-given-cache" and "staged?" in fin.kinds(),
          f"a cache alone names no release, so the staged one is looked for: {fin.kinds()}")


def test_unnamed_and_unpinned_reports_the_staged_release_root() -> None:
    fin = Finish(staged=True)
    result, said, _ = finish(fin)
    check(fin.kinds()[1:4] == ["resolve", "unavailable?", "staged?"] and fin.log[3] == ("staged?", PROJECT)
          and said[0] == ("switchyard: root's record is unreadable; reporting the release root staged for p352's roles "
                          f"instead: syrd352-staged-commit ({STAGED})"),
          f"why the pin is unreadable, then the staged release said: {fin.kinds()} {said[:1]}")
    report = next(e for e in fin.log if e[0] == "report")
    check(report[2]["source_repo"] == STAGED.resolve(strict=False) and report[2]["deploy_ref"] == "syrd352-staged-commit"
          and report[2]["commit_git_dir"] == "syrd352-cache" and result == 0,
          f"the staged root (normalised) and its commit are what is reported; the cache is the resolver's: {report[2]}")
    fin = Finish(staged=False)
    result, said, _ = finish(fin)
    check(said[0] == ("switchyard: no pinned release is readable for p352: root's record is unreadable, and nothing is "
                      "staged. Resolving syrd352-ref instead; pass --deploy-ref <commit> --source-repo <installed "
                      "release> to report a specific release.")
          and next(e for e in fin.log if e[0] == "report")[2]["source_repo"] == NORMALISED_SOURCE and result == 0,
          f"nothing staged: said, and the resolver's own source and ref are reported: {said[:1]}")


def test_the_control_role_and_its_account_are_checked_before_anything_is_written() -> None:
    fin = Finish(director="")
    result, said, _ = finish(fin, dry_run=True)
    check(result == 1 and fin.kinds()[-2:] == ["control?", "caller?"] and said[-1] == (
        "switchyard: cannot establish which role controls p352: no control role is declared. Refusing rather than "
        "guessing which account may make this write."), f"no control role: refused, even for a dry run: {fin.kinds()}")
    fin = Finish(caller="syrd352-someone")
    result, said, _ = finish(fin, dry_run=True)
    check(result == 1 and fin.kinds()[-1] == "role?" and fin.log[-1] == ("role?", "director") and said[-1] == (
        "switchyard: finish-upgrade makes p352's director-authority board write, which the board authorizes for "
        "syrd352-director. This process is syrd352-someone."), f"the wrong account is told, not written for: {said}")
    for caller, account, role, run_as in ((OWNER, DIRECTOR_ACCOUNT, True, f"  {OWNER} "),
                                          ("syrd352-someone", "", True, OWNER), ("syrd352-someone", "x", False, OWNER)):
        fin = Finish(caller=caller, account=account, role=role)
        result, _, _ = finish(fin, dry_run=True, run_as_user=run_as)
        check(result is fin.preview, f"caller {caller}, account {account!r}, role {role}: allowed through to the preview")


def test_a_dry_run_returns_the_preview_itself_and_writes_nothing() -> None:
    for preview in (Code(0), Code(1), Code(7)):
        fin = Finish(pinned="syrd352-pin", preview=preview)
        result, said, config = finish(fin, dry_run=True)
        check(result is preview and fin.kinds()[-1] == "preview", f"the preview's own answer {int(preview)}, nothing after")
        check(fin.log[-1][1] is config and fin.log[-1][2] == dict(
            config_path=CONFIG_PATH, director="director", source_repo=RESOLVED_SOURCE, commit_git_dir="syrd352-cache",
            deploy_ref="syrd352-ref", runner=refusing_runner, print_func=said.append),
              f"previewed for the resolved pin, unnormalised, as the director: {fin.log[-1][2]}")
        check(not {"install", "onboarding", "journal", "report", "observe"} & set(fin.kinds()), "no write is made")


def test_the_handed_off_workflow_refuses_reloads_or_is_absent() -> None:
    fin = Finish(installed=False)
    result, _, config = finish(fin)
    check(result == 1 and type(result) is int and fin.kinds()[-1] == "install" and fin.log[-1][1] is config
          and fin.log[-1][2] == "director", f"a refused install answers 1, installed as the director: {fin.kinds()}")
    fin = Finish(installed=None)
    finish(fin)
    at = fin.kinds().index("install")
    check(fin.kinds()[at + 1:at + 3] == ["onboarding", "reload"] and fin.log[at + 1][1].name == "incoming",
          f"nothing handed over: no reload before the onboarding: {fin.kinds()}")
    fin = Finish(installed="syrd352-installed")
    finish(fin)
    at = fin.kinds().index("install")
    check(fin.kinds()[at + 1:at + 4] == ["reload", "onboarding", "reload"] and fin.log[at + 1] == ("reload", PROJECT, CONFIG_PATH)
          and fin.log[at + 2][1] is fin.reloads[0],
          f"a real install reloads, and the onboarding migrates the reloaded config: {fin.kinds()}")


def test_the_observed_onboarding_state_is_journalled_from_the_fresh_config() -> None:
    for state, reason, journalled, code in (("done", "", "done", 0), ("not required", "legacy", "not required", 0),
                                            ("pending", "the board is too old", "pending", 1),
                                            ("syrd352-odd", "odd", "pending", 1)):
        fin = Finish(state=state, reason=reason)
        result, said, _ = finish(fin)
        fresh = fin.reloads[0]
        journal = next(e for e in fin.log if e[0] == "journal")
        detail = reason or f"completed by {DIRECTOR_ACCOUNT}"
        check(next(e for e in fin.log if e[0] == "state?")[1] is fresh and journal[1] is fresh and journal[2] == dict(
            config_path=CONFIG_PATH, phase="director", state=journalled, detail=detail),
              f"{state}: the fresh config's observed state journalled as {journalled}: {journal}")
        check(result == code and type(result) is int, f"{state}: answers {code}")
        if code:
            check(fin.kinds()[-1] == "journal" and said[-1] == (
                f"switchyard: p352's director migration has not landed: {reason}. The release activation stays withheld."),
                  f"{state}: withheld after journalling: {said[-1:]}")


def test_an_incomplete_cutover_or_root_problems_withhold_the_release() -> None:
    fin = Finish(complete=False)
    result, said, _ = finish(fin)
    check(result == 0 and type(result) is int and fin.kinds()[-1] == "cutover?" and fin.log[-1][1] is fin.reloads[0]
          and fin.log[-1][2] is refusing_runner and said[-1] == (
              "switchyard: p352's per-role accounts are not in place yet, so the release deploy instruction is still "
              "withheld."), f"an incomplete cutover: 0, and nothing reported: {fin.kinds()}")
    fin = Finish(root_problems=["syrd352: root a", "syrd352: root b"])
    result, said, _ = finish(fin)
    check(result == 1 and fin.kinds()[-1] == "root-problems?" and said[-3:] == [
        "switchyard: syrd352: root a", "switchyard: syrd352: root b",
        "switchyard: withholding p352's release deploy sequence until that is repaired. Nothing was deployed, and no "
        "listener needs stopping."], f"root problems: each said, then withheld with 1: {said[-3:]}")


def test_the_release_is_reported_observed_and_its_divergence_said_before_blocking() -> None:
    fin = Finish(divergence=["syrd352: diverged a", "syrd352: diverged b"])
    result, said, _ = finish(fin)
    check(fin.kinds()[-4:] == ["report", "observe", "divergence?", "blocked?"],
          f"reported, observed, divergence, then the blocked check: {fin.kinds()}")
    check(fin.log[-3][2] is fin.status and fin.log[-1][1] is fin.status and said[-2:] == ["syrd352: diverged a",
                                                                                         "syrd352: diverged b"]
          and result == 0, f"the status observed and checked is the one reported; divergence said as given: {said}")
    fin = Finish()
    finish(fin, source_repo=None, deploy_ref=None)
    check("repo-root" not in fin.kinds(), "a resolved source needs no checkout root")


def test_the_checkout_root_stands_in_for_no_source() -> None:
    from scripts import team_launcher

    fin = Finish()
    names = fin.names()
    names["resolve_pinned_upgrade_source"] = lambda config, **kw: fin.log.append(("resolve", kw)) or (None, None, "r", "pin")
    said: list[str] = []
    with patched(team_launcher, **names), patched(os, geteuid=fin.geteuid):
        from scripts import director_upgrade
        director_upgrade.finish_upgrade_command(SimpleNamespace(project=PROJECT, run_as_user=OWNER), config_path=CONFIG_PATH,
                                                runner=refusing_runner, print_func=said.append)
    report = next(e for e in fin.log if e[0] == "report")
    check("repo-root" in fin.kinds() and report[2]["source_repo"] == REPO.resolve(strict=False)
          and report[2]["commit_git_dir"] is None, f"no source: the checkout root, normalised: {report[2]}")


def test_a_blocked_release_answers_one_with_the_installed_reminder() -> None:
    for installed, reminder in ((None, False), ("syrd352-installed", True)):
        fin = Finish(blocked="the board did not move", installed=installed, divergence=["syrd352: diverged"])
        result, said, _ = finish(fin)
        expected = ["syrd352: diverged",
                    "switchyard: p352's release phase did not complete: the board did not move. Nothing after it is claimed."]
        if reminder:
            expected.append("switchyard: p352's handed-off workflow is installed and stays installed; only the release "
                            "phase is outstanding.")
        check(result == 1 and type(result) is int and said[-len(expected):] == expected
              and "journal" in fin.kinds() and fin.kinds().index("install") < fin.kinds().index("blocked?"),
              f"installed {installed!r}: divergence, then blocked, then the reminder only if installed; the writes stay: "
              f"{said[-3:]}")


def test_an_error_reaches_the_caller() -> None:
    boom = OSError("syrd352: the install raised")
    fin = Finish(error=boom)
    try:
        finish(fin); raised = None
    except OSError as exc:
        raised = exc
    check(raised is boom and fin.kinds()[-1] == "install", f"not caught, nothing after it: {fin.kinds()}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_object_and_the_same_defaults",
             "test_the_seams_the_modules_own_names_and_the_dispatch")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"finish_upgrade_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
