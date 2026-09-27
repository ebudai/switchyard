"""Owner state directories: giving the project owner the session and pane-state directories it writes.

When a project runs as its own account and the launcher is someone else,
`ensure_owner_state_dirs` creates the session directory and the pane-state
directory as that owner, mode 700, through the caller's runner
(`install_owner_state_dir_args`) -- but only a directory inside the owner's
home or runtime directory (`_owner_state_roots`, `_is_owner_state_path`), so the
launcher never hands the owner a path outside its own state.

The current user, the owner's runtime directory and the failure reason are
read from `scripts/team_launcher.py` when a function runs, as are the calls
between the functions here; the owner's account comes from `pwd`, the same
module the launcher holds.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-332). `team_launcher`
imports this module at its top and still exports every name callers read there.
This module never imports `team_launcher` at its top.
"""

from __future__ import annotations

import pwd
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig

def install_owner_state_dir_args(config: ProjectConfig, path: Path) -> list[str]:
    if not config.run_as_user:
        raise ValueError("state directory ownership setup requires run_as_user")
    return [
        "install",
        "-d",
        "-m",
        "700",
        "-o",
        config.run_as_user,
        "-g",
        config.run_as_user,
        str(path),
    ]


def _owner_state_roots(config: ProjectConfig) -> tuple[Path, ...]:
    from scripts import team_launcher as launcher

    if not config.run_as_user:
        return ()
    try:
        owner = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return ()
    return (
        Path(owner.pw_dir).expanduser().resolve(strict=False),
        launcher.runtime_dir_for_uid(owner.pw_uid).expanduser().resolve(strict=False),
    )


def _is_owner_state_path(config: ProjectConfig, path: Path) -> bool:
    from scripts import team_launcher as launcher

    resolved = path.expanduser().resolve(strict=False)
    return any(resolved == root or resolved.is_relative_to(root) for root in launcher._owner_state_roots(config))


def ensure_owner_state_dirs(
    config: ProjectConfig,
    *,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    if not config.run_as_user or launcher.current_user_name() == config.run_as_user:
        return
    for path in (config.session_dir, pane_state_dir):
        if not launcher._is_owner_state_path(config, path):
            continue
        result = runner(launcher.install_owner_state_dir_args(config, path))
        if result.returncode != 0:
            reason = launcher._proc_failure_reason(result, f"install failed with exit {result.returncode}")
            raise SystemExit(f"team-launcher: failed to assign state directory {path} to {config.run_as_user}: {reason}")
