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

That the builders here run only through `run_owner_correct_git` is
`team_launcher_git_ownership_lint_test`'s job, which now scans this module too.
"""

from __future__ import annotations

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
    'chown_control_repository_args',
    'control_repository_refspec',
    'control_repository_state',
    'ensure_control_repository',
    'ensure_control_role_worktrees',
    'ensure_project_worktrees',
    'fetch_project_worktree_ref',
    'git_checkout_shared_ref_args',
    'git_clean_role_worktree_args',
    'git_clean_role_worktree_dry_run_args',
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
    'warn_before_role_worktree_refresh',
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


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"project_worktrees_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
