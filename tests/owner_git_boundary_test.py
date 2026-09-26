#!/usr/bin/env python3
"""SYRD-293: the owner-correct git module's boundary with the launcher it came out of.

`run_owner_correct_git`, `GitOwnerRule` and the project git helpers moved into
`scripts/owner_git.py` unchanged. This pins what makes that safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- **The chokepoint seam stays on the launcher.** A patch of
  `team_launcher.run_owner_correct_git` reaches owner_git's own helpers, as it
  reaches the other git modules.
- **The chokepoint's dependencies stay on the launcher.** A patch of
  `team_launcher.current_user_name` decides whether the chokepoint runs git
  directly or as the owner.
- `team_launcher._normalized_path` still means the launcher's LATER
  definition, which returns a `Path`. The launcher defines that name twice, and
  moving either copy would have changed that.

Nothing runs git: the runners record what they are given.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

#: Every moved name the launcher still exports, fixed here so dropping one is noticed.
EXPORTED = (
    '_path_owner_user',
    'GitOwnerRule',
    '_path_is_under',
    '_git_target_path_from_args',
    '_git_owner_for_target',
    '_git_owner_failure',
    'run_owner_correct_git',
    '_git_status_porcelain',
    '_run_owner_git',
    '_ensure_project_git_repository',
    '_require_existing_project_git_repository',
    '_commit_project_git_changes',
    '_owner_project_git_runner',
)


def attempt(action):
    """Run one step and hand back what it returned or raised, so a check reports it."""
    try:
        return action(), None
    except BaseException as exc:  # noqa: BLE001 -- what escaped is the finding
        return None, exc


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_imports_without_the_launcher() -> None:
    result = python("import sys, scripts.owner_git; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.owner_git", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.owner_git")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.owner_git as o; "
            f"print(all(getattr(t, n) is getattr(o, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_the_launchers_normalized_path_is_still_its_later_definition() -> None:
    def normalized():
        from scripts import team_launcher

        return team_launcher._normalized_path(Path("/tmp/syrd293/../syrd293"))

    value, escaped = attempt(normalized)
    check(isinstance(value, Path) and value == Path("/tmp/syrd293"),
          f"_normalized_path returns the later definition's Path, as before: {value!r} {escaped!r}")


def test_a_chokepoint_patch_on_the_launcher_reaches_owner_gits_own_helpers() -> None:
    imported, escaped = attempt(lambda: __import__("scripts.owner_git").owner_git)
    check(escaped is None, f"owner_git imports in this process: {escaped!r}")
    from scripts import owner_git, team_launcher

    seen: list[list[str]] = []

    def patched(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args, 0, " M tracked.txt\n", "")

    saved = team_launcher.run_owner_correct_git
    team_launcher.run_owner_correct_git = patched
    try:
        status, escaped = attempt(lambda: owner_git._git_status_porcelain(Path("/nowhere/repo"), runner=None))
    finally:
        team_launcher.run_owner_correct_git = saved
    check(status == " M tracked.txt",
          f"the helper's answer came from the launcher's patched chokepoint: {status!r} {escaped!r}")
    check(seen == [["git", "-C", "/nowhere/repo", "status", "--porcelain", "--untracked-files=no"]],
          f"with the helper's own git command: {seen}")


def test_the_chokepoint_asks_the_launcher_who_is_running() -> None:
    from scripts import owner_git, team_launcher

    with tempfile.TemporaryDirectory(prefix="syrd293-owner.") as raw:
        repo = Path(raw) / "repo"
        repo.mkdir()
        rules = [owner_git.GitOwnerRule(root=Path(raw), owner_user="repo-owner")]
        ran: list[list[str]] = []
        runner = lambda args, **kwargs: ran.append(list(args)) or subprocess.CompletedProcess(args, 0, "", "")
        saved = team_launcher.current_user_name
        escaped = []
        try:
            for who in ("repo-owner", "someone-else"):
                team_launcher.current_user_name = lambda who=who: who
                escaped.append(attempt(lambda: owner_git.run_owner_correct_git(
                    ["git", "-C", str(repo), "status"], runner=runner, owner_rules=rules))[1])
        finally:
            team_launcher.current_user_name = saved
    check(ran == [["git", "-C", str(repo), "status"], ["sudo", "-u", "repo-owner", "git", "-C", str(repo), "status"]],
          "run directly as the owner, and through sudo -u for anyone else, as the launcher says who is running: "
          f"{ran} {[e for e in escaped if e]!r}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"owner_git_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
