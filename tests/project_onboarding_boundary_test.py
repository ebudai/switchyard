#!/usr/bin/env python3
"""SYRD-289: the onboarding module's boundary with the launcher it came out of.

Onboarding documents, director onboarding and the generated board skill moved
into `scripts/project_onboarding.py` unchanged. This pins what makes that safe:

- The module does not import the launcher at its top: the launcher imports it.
- Every name callers reached as `team_launcher.<name>` is still there and is
  the very same object, whichever module is imported first.
- Launcher facilities the moved code uses are looked up on `team_launcher`
  when it runs, so a patch there reaches it. That includes names the launcher
  itself only imports, such as `home_dir_for_user`, which the suites patch on
  the launcher.
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

EXPORTED = (
    "BOARD_SKILL_INSTALLER_NAME", "BOARD_SKILL_NAME", "ONBOARDING_SNAPSHOT_RE",
    "SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME", "SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME",
    "SWITCHYARD_ONBOARDING_DOC_NAMES", "DirectorSeedResult", "_install_switchyard_onboarding_docs",
    "_stamp_onboarding_doc", "_switchyard_source_commit", "_write_switchyard_onboarding_files",
    "board_skill_installer_path", "director_onboarding_seed_text", "director_onboarding_state",
    "ensure_generated_project_board_skill", "install_generated_project_board_skill_args",
    "migrate_declarative_director_onboarding", "seed_director_onboarding", "upgrade_switchyard_onboarding_docs",
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def test_the_module_imports_without_the_launcher() -> None:
    result = python("import sys, scripts.project_onboarding; print('scripts.team_launcher' in sys.modules)")
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "False", f"and does not pull the launcher in: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.project_onboarding", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.project_onboarding")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.project_onboarding as o; "
            f"print(all(getattr(t, n) is getattr(o, n) for n in {EXPORTED!r}))"
        )
        check(result.stdout.strip() == "True",
              f"{' then '.join(order)}: every moved name is the launcher's too: {result.stdout}{result.stderr[-600:]}")


def test_launcher_patches_reach_the_moved_code() -> None:
    from scripts import project_onboarding, team_launcher

    asked: list[str] = []
    names = ("home_dir_for_user", "current_user_name", "_read_switchyard_release_marker",
             "shared_switchyard_release_for_path", "run_owner_correct_git")
    saved = {name: getattr(team_launcher, name) for name in names}
    team_launcher.home_dir_for_user = lambda user: asked.append(f"home:{user}") or Path("/homes") / user
    team_launcher.current_user_name = lambda: asked.append("whoami") or "someone-else"
    team_launcher._read_switchyard_release_marker = lambda path: asked.append("marker") or None
    team_launcher.shared_switchyard_release_for_path = lambda path: asked.append("shared") or None
    team_launcher.run_owner_correct_git = lambda args, **kwargs: asked.append(f"git:{args[-1]}") or \
        subprocess.CompletedProcess(args, 0, "c0ffee\n", "")
    try:
        args = project_onboarding.install_generated_project_board_skill_args(
            SimpleNamespace(run_as_user="owner-x"), installer=Path("/opt/installer"))
        commit = project_onboarding._switchyard_source_commit(Path("/nowhere"), runner=None)
    finally:
        for name, value in saved.items():
            setattr(team_launcher, name, value)
    check(args == ["sudo", "-u", "owner-x", "-H", "/opt/installer", "install", "--home", "/homes/owner-x"],
          f"the board skill install used the launcher's patched home and user: {args}")
    check(commit == "c0ffee", f"the source commit came through the launcher's patched git chokepoint: {commit}")
    check(asked == ["home:owner-x", "whoami", "marker", "shared", "git:HEAD"],
          f"each facility was asked of the launcher, in order: {asked}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"project_onboarding_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
