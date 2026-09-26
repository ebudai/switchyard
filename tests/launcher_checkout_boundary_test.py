#!/usr/bin/env python3
"""SYRD-292: the launcher-checkout module's boundary with the launcher it came out of.

Keeping a launcher checkout current moved into `scripts/launcher_checkout.py`
unchanged, with its git argv builders. This pins what makes that safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- The owner-correct chokepoint and the launcher's own facilities are looked up
  on `team_launcher` when the code runs. A patch there reaches the probe, and
  so does a patch of `_repo_root`, which three suites patch. Driven with
  canned git answers; no git runs.

That the builders here run only through `run_owner_correct_git` is
`team_launcher_git_ownership_lint_test`'s job, which scans this module too.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every moved name the launcher still exports, fixed here so dropping one is noticed.
EXPORTED = (
    'ALLOW_STALE_LAUNCHER_ENV',
    'LEGACY_ALLOW_STALE_LAUNCHER_ENV',
    'LauncherCheckoutProbe',
    'git_fetch_launcher_ref_args',
    'git_launcher_checkout_check_args',
    'git_launcher_head_args',
    'git_launcher_ls_remote_ref_args',
    'git_launcher_commit_exists_args',
    'git_launcher_ahead_behind_args',
    'git_checkout_launcher_branch_args',
    'git_fast_forward_launcher_ref_args',
    'git_launcher_current_branch_args',
    'git_launcher_status_porcelain_args',
    'git_launcher_head_short_args',
    'git_clean_launcher_checkout_args',
    'probe_launcher_checkout',
    'probe_checkout_against_worktree_ref',
    'launcher_checkout_status',
    'ensure_launcher_checkout_current',
    'deploy_launcher_checkout',
    'warn_if_artifact_source_checkout_is_stale',
    '_format_behind_count',
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_imports_without_the_launcher() -> None:
    result = python("import sys, scripts.launcher_checkout; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.launcher_checkout", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.launcher_checkout")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.launcher_checkout as c; "
            f"print(all(getattr(t, n) is getattr(c, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_probe_goes_through_the_launchers_chokepoint_and_repo_root() -> None:
    from scripts import launcher_checkout, team_launcher

    config = SimpleNamespace(worktree_remote="origin", worktree_branch="main")
    asked: list[str] = []

    def chokepoint(args, *, runner, **kwargs):
        verb = args[3]
        asked.append(verb)
        out = {"rev-parse": "aaaa\n", "ls-remote": "bbbb\trefs/heads/main\n", "rev-list": "1\t2\n"}.get(verb, "")
        return subprocess.CompletedProcess(args, 0, out, "")

    saved = (team_launcher.run_owner_correct_git, team_launcher._repo_root)
    team_launcher.run_owner_correct_git = chokepoint
    team_launcher._repo_root = lambda: asked.append("repo-root") or Path("/nowhere/launcher-repo")
    try:
        probe = launcher_checkout.probe_launcher_checkout(config, runner=None)
    finally:
        team_launcher.run_owner_correct_git, team_launcher._repo_root = saved
    check(probe == launcher_checkout.LauncherCheckoutProbe(ahead=1, behind=2),
          f"the probe read its answers from the launcher's patched chokepoint: {probe}")
    check(asked[0] == "repo-root" and asked[1:] == ["rev-parse", "rev-parse", "ls-remote", "cat-file", "rev-list"],
          f"and asked for the repo and each git step through the launcher, in order: {asked}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"launcher_checkout_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
