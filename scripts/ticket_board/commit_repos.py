"""Commit-repository resolution for ticket-board audit hashes."""

from __future__ import annotations

import os
from pathlib import Path


LEGACY_PROJECT_COMMIT_REPOS: dict[str, tuple[Path, ...]] = {
    "pgu": (Path("/data/git/switchyard.git"), Path("/data/git/pgu.git")),
    "mefp": (Path("/data/git/stellaris-fixpatch.git"),),
    "otto": (Path("/data/git/otto_scheduler.git"),),
}


def _parse_git_dir_list(raw: str) -> tuple[Path, ...]:
    return tuple(Path(item) for item in raw.split(os.pathsep) if item.strip())


def explicit_commit_git_dirs(environ: dict[str, str] | os._Environ[str] = os.environ) -> tuple[Path, ...]:
    explicit = environ.get("TICKET_BOARD_COMMIT_GIT_DIR", "").strip() or environ.get(
        "PGU_TICKET_BOARD_COMMIT_GIT_DIR", ""
    ).strip()
    return _parse_git_dir_list(explicit) if explicit else ()


def default_commit_git_dirs(project: str, *, owner_home: Path | str | None = None) -> tuple[Path, ...]:
    project = str(project or "").strip().lower() or "pgu"
    if project in LEGACY_PROJECT_COMMIT_REPOS:
        return LEGACY_PROJECT_COMMIT_REPOS[project]
    home = Path(owner_home) if owner_home is not None else Path.home()
    return (home / ".local" / "state" / "switchyard" / "projects" / project / "control.git",)


def format_commit_git_dirs(paths: tuple[Path, ...]) -> str:
    return os.pathsep.join(str(path) for path in paths)


def commit_git_dirs_for_project(
    environ: dict[str, str] | os._Environ[str] = os.environ,
    *,
    project: str | None = None,
    owner_home: Path | str | None = None,
) -> tuple[Path, ...]:
    explicit = explicit_commit_git_dirs(environ)
    if explicit:
        return explicit
    resolved_project = project or environ.get("TICKET_BOARD_PROJECT", "").strip() or environ.get(
        "PGU_TICKET_BOARD_PROJECT", ""
    ).strip() or "pgu"
    resolved_home = owner_home if owner_home is not None else environ.get("HOME", "").strip() or None
    return default_commit_git_dirs(resolved_project, owner_home=resolved_home)


def commit_git_dir_env_for_project(
    environ: dict[str, str] | os._Environ[str] = os.environ,
    *,
    project: str | None = None,
    owner_home: Path | str | None = None,
) -> str:
    return format_commit_git_dirs(commit_git_dirs_for_project(environ, project=project, owner_home=owner_home))
