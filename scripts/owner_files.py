"""How the launcher hands a generated file to the project owner, and reads who owns a path.

`chown_owner_file_args` is the `chown <owner>:<owner> <path>` argv for a
tenant's owner (refused without one), and `ensure_owner_file` runs it through
the given runner only when the caller is root and not already the owner,
reporting a failure with the command's own error text. `_chown_project_file`
runs the same argv for a named owner. `_path_owner_label` and `_path_owner_ids`
read a path's owner without following a final symlink (`lstat`), as a
`user:group` label and as ids.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-461). The launcher
imports this module and re-exports all five, so every module that reads them
through the launcher still reaches the launcher's names. What they read of the
launcher -- the current user, the error-text helper and each other -- is read
through it when they run, so a patch there still reaches them. This module
imports `team_launcher` only inside the function that needs it, when it runs.
"""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.project_identity import ProjectConfig


def _path_owner_label(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        return f"unreadable ({exc})"
    try:
        user = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        user = f"uid {info.st_uid}"
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = f"gid {info.st_gid}"
    return f"{user}:{group}"


def _path_owner_ids(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_uid, info.st_gid


def chown_owner_file_args(config: ProjectConfig, path: Path) -> list[str]:
    if not config.run_as_user:
        raise ValueError("file ownership repair requires run_as_user")
    return ["chown", f"{config.run_as_user}:{config.run_as_user}", str(path)]


def ensure_owner_file(
    config: ProjectConfig,
    path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    from scripts import team_launcher as launcher

    if not config.run_as_user or launcher.current_user_name() == config.run_as_user or os.geteuid() != 0:
        return
    result = runner(launcher.chown_owner_file_args(config, path))
    if result.returncode != 0:
        reason = launcher._proc_failure_reason(result, f"chown failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to assign generated file {path} to {config.run_as_user}: {reason}")


def _chown_project_file(
    *,
    owner_user: str,
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    result = runner(["chown", f"{owner_user}:{owner_user}", str(path)])
    if result.returncode != 0:
        raise SystemExit(f"switchyard: failed to assign {path} to {owner_user}")
