"""finish-upgrade's dry run: apply's checks, in apply's order, with nothing written.

`_finish_upgrade_preview` is what `switchyard finish-upgrade --dry-run` does
for one tenant: it asks the handed-off workflow install to describe itself,
names the director phase, reads the role-account cutover, the owner's release
root and the release step apply would take, and ends with apply's own verdict --
0 only when every check apply makes would pass.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-408). The launcher
imports this module and re-exports the name; `scripts/director_upgrade.py`
still reads it through the launcher when it runs. Everything it calls -- the
workflow install, the current user, the cutover, the release root, the release
report and its verdict, and the checkout it falls back to -- is read through the
launcher at call time, so a suite that rebinds one there still intercepts it.
`ProjectConfig` is an annotation only. This module imports `team_launcher` only
inside the function, when it runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def _finish_upgrade_preview(
    config: ProjectConfig,
    *,
    config_path: Path,
    director: str,
    source_repo: Path | None,
    commit_git_dir: str | None,
    deploy_ref: str | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> int:
    """finish-upgrade's steps in apply's order: every read made, every write described.

    Its verdict is apply's verdict. On MEFP the dry run stopped at its first
    line and exited 0, and apply then made the one-way workflow write before
    failing on a release it could not resolve -- a read-only check the dry run
    never reached (SYRD-254). The release step here is the same function apply
    runs, so the two cannot disagree about it.
    """
    from scripts import team_launcher as launcher

    stops: list[str] = []

    def verdict() -> int:
        if stops:
            print_func(
                f"switchyard: dry run: apply would not complete for {config.project}: "
                + "; ".join(stops)
                + ". Nothing was written."
            )
            return 1
        print_func(
            f"switchyard: dry run: every check apply makes for {config.project} passes. "
            "Nothing was written."
        )
        return 0

    installed = launcher.install_handed_off_workflow(
        config, config_path=config_path, caller_role=director, dry_run=True, print_func=print_func
    )
    if installed is False:
        stops.append("the handed-off workflow would be refused")
    print_func(
        f"switchyard: would migrate {config.project}'s declarative director onboarding as "
        f"{launcher.current_user_name()}, then record the director phase from what the board serves"
    )
    cutover = launcher.role_account_cutover(config, runner=runner)
    if not cutover.is_complete:
        print_func(
            f"switchyard: apply would stop after the director phase: {config.project}'s "
            "per-role accounts are not in place yet, so no release step follows"
        )
        return verdict()
    root_problems = launcher.owner_release_root_problems(config)
    if root_problems:
        for problem in root_problems:
            print_func(f"switchyard: {problem}")
        stops.append("the owner's release root needs repair first")
        return verdict()
    print_func(f"switchyard: the release step apply would report for {config.project}:")
    release_status = launcher.report_tenant_release_upgrade(
        config,
        config_path=config_path,
        source_repo=(source_repo or launcher._repo_root()).expanduser().resolve(strict=False),
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
        print_func=lambda line: print_func(f"  {line}"),
    )
    blocked = launcher.release_update_blocked(release_status)
    if blocked:
        stops.append(f"its release phase would not complete: {blocked}")
    return verdict()
