#!/usr/bin/env python3
"""SYRD-291: the project-worktrees module's boundary with the launcher it came out of.

A project's worktrees and control repository moved into
`scripts/project_worktrees.py` unchanged, with their git argv builders. This
pins what makes that safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- Launcher facilities the moved code uses are looked up on `team_launcher`
  when it runs, so a patch there reaches it. That includes
  `_control_repository_owner_home`, which is defined here but patched on the
  launcher by four suites.
- SYRD-333 appended `_prepare_project_worktrees_for_launch`, which decides
  which roles' worktrees a launch prepares. It calls `ensure_project_worktrees`
  (rebound by the suites) and builds `WorktreeProvisionResult` through the
  launcher even though both are defined here, and takes `dataclasses.replace`
  as this module's own import -- the very object the launcher holds.

That the builders here run only through `run_owner_correct_git` is
`team_launcher_git_ownership_lint_test`'s job, which now scans this module too.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


#: Every moved name callers and suites reach as `team_launcher.<name>`, fixed
#: here rather than read back from the launcher, so that dropping one is noticed.
EXPORTED = (
    'CONTROL_REPOSITORY_EMPTY',
    'CONTROL_REPOSITORY_MISSING',
    'CONTROL_REPOSITORY_OCCUPIED',
    'CONTROL_REPOSITORY_READY',
    'CONTROL_REPOSITORY_UNREADABLE',
    'WorktreeProvisionResult',
    '_config_git_owner_rules',
    '_control_repository_boundary_error',
    '_control_repository_owner_home',
    '_prepare_project_worktrees_for_launch',
    'chown_control_repository_args',
    'control_repository_refspec',
    'control_repository_state',
    'ensure_control_repository',
    'ensure_control_role_worktrees',
    'ensure_project_worktrees',
    'fetch_project_worktree_ref',
    'git_checkout_shared_ref_args',
    'git_clean_role_worktree_args',
    'git_clean_shared_checkout_args',
    'git_clean_shared_checkout_dry_run_args',
    'git_clone_control_repository_args',
    'git_control_fetch_refspec_args',
    'git_control_remote_rename_args',
    'git_control_worktree_add_args',
    'git_fetch_control_ref_args',
    'git_fetch_worktree_ref_args',
    'git_role_worktree_check_args',
    'git_role_worktree_reset_args',
    'git_role_worktree_status_porcelain_args',
    'git_shared_checkout_check_args',
    'git_shared_checkout_status_porcelain_args',
    'mkdir_p_args',
    'repair_control_repository_ownership',
    'report_kept_worktrees',  # SYRD-555: the launch's summary of worktrees kept, replacing the warn-only check
    'warn_before_shared_checkout_refresh',
)


def exported() -> tuple[str, ...]:
    return EXPORTED


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_imports_without_the_launcher() -> None:
    result = python("import sys, scripts.project_worktrees; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    names = exported()
    for order in (("scripts.project_worktrees", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.project_worktrees")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.project_worktrees as w; "
            f"print(all(getattr(t, n) is getattr(w, n) for n in {json.dumps(names)}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_launcher_patches_reach_the_moved_code() -> None:
    from scripts import project_worktrees, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd291-seam.") as raw:
        owner_home = Path(raw) / "owner-home"
        control = owner_home / ".local" / "state" / "switchyard" / "projects" / "porter" / "control"
        config = SimpleNamespace(control_repository=control, run_as_user="syrd291-no-such-account",
                                 repository=Path(raw) / "repo", worktree_remote="origin", worktree_branch="main")
        unpatched = project_worktrees._control_repository_boundary_error(config, require_existing_user=True)
        check(unpatched == "target user 'syrd291-no-such-account' does not exist",
              f"unpatched, an account this host lacks is refused: {unpatched!r}")
        asked: list[str] = []
        saved = (team_launcher._control_repository_owner_home, team_launcher.worktree_ref)
        team_launcher._control_repository_owner_home = lambda cfg: asked.append("owner-home") or owner_home
        team_launcher.worktree_ref = lambda cfg: asked.append("ref") or "upstream/release"
        try:
            patched = project_worktrees._control_repository_boundary_error(config, require_existing_user=True)
            args = project_worktrees.git_checkout_shared_ref_args(config)
        finally:
            team_launcher._control_repository_owner_home, team_launcher.worktree_ref = saved
    check(patched is None, f"the boundary check used the launcher's patched owner home: {patched!r}")
    check(args[-1] == "upstream/release", f"the builder used the launcher's patched worktree ref: {args}")
    check(asked == ["owner-home", "ref"], f"each was asked of the launcher: {asked}")


def test_a_launch_preparation_reaches_its_seams_through_the_launcher() -> None:
    # Named to run first: a seam taken past the launcher must be caught before
    # any behaviour check runs the real code it reached.
    from scripts import project_worktrees, team_launcher

    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(launcher_tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_prepare_project_worktrees_for_launch"]
    # SYRD-340 moved that site, in launch_project's P5, to launch_phases, which
    # calls it through the launcher.
    phases = ast.parse((ROOT / "scripts" / "launch_phases.py").read_text(encoding="utf-8"))
    phase_calls = [n for n in ast.walk(phases) if isinstance(n, ast.Call)
                   and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_prepare_project_worktrees_for_launch"]
    check(len(calls) + len(phase_calls) == 1 and all(isinstance(n.func, ast.Name) for n in calls)
          and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "launcher" for n in phase_calls),
          "the launch calls it at its one baseline site: through the launcher from launch_phases")
    module = ast.parse((ROOT / "scripts" / "project_worktrees.py").read_text(encoding="utf-8"))
    function = next(n for n in module.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_prepare_project_worktrees_for_launch")
    for name, count in {"ensure_project_worktrees": 3, "WorktreeProvisionResult": 2}.items():
        uses = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(uses) == count and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                                         and n.func.value.id == "launcher" for n in uses),
              f"it calls {name} at its {count} baseline sites, each through the launcher")
    # The body only: the return annotation names the class but is never
    # evaluated (the module defers annotations).
    bare = sorted({n.id for statement in function.body for n in ast.walk(statement) if isinstance(n, ast.Name)
                   and isinstance(n.ctx, ast.Load) and n.id in ("ensure_project_worktrees", "WorktreeProvisionResult")})
    check(bare == [], f"and never past it: {bare}")
    bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
    bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
    check("launcher" not in bound and through == [],
          f"nothing it binds itself is read as the launcher's, nor shadows it: {through}")
    check(project_worktrees.replace is team_launcher.replace is dataclasses.replace,
          "replace is this module's own import, the very object the launcher holds")


@dataclasses.dataclass(frozen=True)
class PrepConfig:
    project: str
    control_repository: Path | None
    roles: list


PREP_ROLES = [SimpleNamespace(role=name) for name in ("alpha", "beta", "gamma")]


def prepare(cfg: PrepConfig, running: list, *, failed: dict[str, str] | None = None,
            error: Exception | None = None, result_class: type | None = None):
    """Run the preparation against a recording ensure_project_worktrees patched on the launcher."""
    from scripts import project_worktrees, team_launcher

    asked: list[tuple] = []
    runner = lambda argv, **kwargs: None  # noqa: E731 - handed on, never called here

    def ensure(config: object, *, refresh: bool, runner: object, discard: frozenset = frozenset()):  # SYRD-555
        asked.append((config, refresh, runner))
        if error:
            raise error
        return team_launcher.WorktreeProvisionResult(dict(failed or {}))

    saved = (team_launcher.ensure_project_worktrees, team_launcher.WorktreeProvisionResult)
    team_launcher.ensure_project_worktrees = ensure
    if result_class:
        team_launcher.WorktreeProvisionResult = result_class
    try:
        result = project_worktrees._prepare_project_worktrees_for_launch(cfg, running_roles=running, runner=runner)
    finally:
        team_launcher.ensure_project_worktrees, team_launcher.WorktreeProvisionResult = saved
    return result, asked, runner


def test_launch_preparation_with_nothing_running_prepares_every_role_refreshed() -> None:
    cfg = PrepConfig("p333", Path("/nonexistent/syrd333/control"), list(PREP_ROLES))
    result, asked, runner = prepare(cfg, [], failed={"beta": "busy"})
    check(len(asked) == 1 and asked[0][0] is cfg and asked[0][1] is True and asked[0][2] is runner,
          f"the caller's own config, refreshed, with the caller's runner: {asked}")
    check(result.failed_roles == {"beta": "busy"}, f"and its result as prepared: {result.failed_roles}")


def test_launch_preparation_with_a_control_repository_prepares_only_stopped_roles() -> None:
    cfg = PrepConfig("p333", Path("/nonexistent/syrd333/control"), list(PREP_ROLES))
    result, asked, runner = prepare(cfg, [PREP_ROLES[1]], failed={"alpha": "a", "beta": "b", "gamma": "c"})
    check(len(asked) == 1 and asked[0][1] is True and asked[0][2] is runner,
          f"one preparation, refreshed, with the caller's runner: {asked}")
    prepared = asked[0][0]
    check(prepared is not cfg and prepared.roles == [PREP_ROLES[0], PREP_ROLES[2]]
          and all(a is b for a, b in zip(prepared.roles, (PREP_ROLES[0], PREP_ROLES[2])))
          and prepared.project == cfg.project and prepared.control_repository is cfg.control_repository
          and cfg.roles == PREP_ROLES,
          f"a copy of the config holding only the stopped roles, in config order; the caller's is untouched: "
          f"{prepared}")
    check(result.failed_roles == {"alpha": "a", "gamma": "c"},
          f"and a running role's failure is never reported: {result.failed_roles}")


def test_launch_preparation_with_every_role_running_prepares_nothing() -> None:
    from scripts import team_launcher

    cfg = PrepConfig("p333", Path("/nonexistent/syrd333/control"), list(PREP_ROLES))
    result, asked, _ = prepare(cfg, list(PREP_ROLES), error=AssertionError("nothing is prepared"))
    check(asked == [] and type(result) is team_launcher.WorktreeProvisionResult and result.failed_roles == {},
          f"with a control repository and every role running, nothing is prepared and nothing failed: {asked}")


def test_launch_preparation_without_a_control_repository_prepares_every_role_unrefreshed() -> None:
    cfg = PrepConfig("p333", None, list(PREP_ROLES))
    result, asked, runner = prepare(cfg, [PREP_ROLES[0]], failed={"alpha": "a", "gamma": "c"})
    check(len(asked) == 1 and asked[0][0] is cfg and asked[0][1] is False and asked[0][2] is runner,
          f"the caller's own config, not refreshed, with the caller's runner: {asked}")
    check(result.failed_roles == {"gamma": "c"}, f"and a running role's failure is dropped: {result.failed_roles}")


def test_launch_preparation_builds_its_result_and_propagates_through_the_launcher() -> None:
    from scripts import team_launcher

    class Recorded(team_launcher.WorktreeProvisionResult):
        pass

    cfg = PrepConfig("p333", None, list(PREP_ROLES))
    result, _, _ = prepare(cfg, [PREP_ROLES[0]], result_class=Recorded)
    check(type(result) is Recorded, f"the result is built by the launcher's WorktreeProvisionResult: {type(result)}")
    refusal = PermissionError("syrd333: not the owner")
    try:
        prepare(cfg, [PREP_ROLES[0]], error=refusal)
        raised = None
    except PermissionError as exc:
        raised = exc
    check(raised is refusal, f"a preparation's failure reaches the caller: {raised!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"project_worktrees_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
