"""Project identity and configuration: the config types, the slug rules and where registrations live.

`RoleConfig` and `ProjectConfig` are the launcher's configuration types and
`SwitchyardProjectEntry` a known project; `_validate_project_slug`,
`_slug_from_project_name` and `_legacy_dash_slug_from_project_name` are the slug
rules; and `switchyard_registry_dir` is where registrations live.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-456). The launcher
imports this module and re-exports every name, so every caller, annotation and
isinstance check still reaches the same objects. The three dataclasses are
defined here, so their `__module__` is this module's; their fields and defaults
are the launcher's, unchanged. Everything the functions read when they run is
read through the launcher, so a suite that rebinds one there still intercepts
it. This module imports `team_launcher` only inside the functions, when they
run.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scripts.worker_pool_command import WorkerPool


def switchyard_registry_dir() -> Path:
    from scripts import team_launcher as launcher

    configured = os.environ.get(launcher.SWITCHYARD_REGISTRY_DIR_ENV, "").strip()
    return Path(configured).expanduser() if configured else launcher.DEFAULT_SWITCHYARD_REGISTRY_DIR


@dataclass(frozen=True)
class RoleConfig:
    role: str
    slot: int | None
    detached: bool
    tmux_session: str
    target: str
    workdir: str
    cli: list[str]
    model: str
    model_arg: str
    effort: str
    yolo: bool
    extra_args: list[str]
    resume_mode: str
    resume_flag: str
    resume_subcommand: str
    fresh_session_per_ticket: bool
    live_commands: list[str]
    env: dict[str, str]
    # Read only for upgrade compatibility. SYRD-69 runs every role as the
    # project account; authority is the registered live process, not this UID.
    run_as_user: str = ""
    unset_env: tuple[str, ...] = ()
    # SYRD-135: declared on the role, projected from the workflow document, and
    # acted on by the notify listener rather than here -- the reset happens at
    # the ticket boundary in a running pane, not at launch. It is carried in the
    # generated config so the two descriptions of a role cannot disagree.
    ephemeral: bool = False
    # SYRD-141: what this role's presentation pane is called. Projected from
    # the workflow document, where the implementer default and any per-role
    # override are decided; empty here means a config generated before that
    # existed, and the role's own name is the answer it had then.
    presentation_label: str = ""


@dataclass(frozen=True)
class ProjectConfig:
    project: str
    project_name: str
    ticket_prefix: str
    layout: Path
    session_dir: Path
    board_url: str
    board_socket: str
    upstream_report_url: str
    upstream_report_token_file: str
    run_as_user: str
    pane_launcher: Path | None
    repository: Path | None
    control_repository: Path | None
    worktree_base: Path | None
    worktree_remote: str
    worktree_branch: str
    roles: list[RoleConfig]
    desktop_access: dict[str, Any] | None = None
    role_state_isolation: bool = False
    #: A pool of interchangeable workers this project may run, declared once
    #: rather than written out as N roles. None means the project has none,
    #: which is every project that has not asked for one (SYRD-37).
    worker_pool: "WorkerPool | None" = None


@dataclass(frozen=True)
class SwitchyardProjectEntry:
    slug: str
    name: str
    config_path: Path


def _slug_from_project_name(name: str) -> str:
    from scripts import team_launcher as launcher

    raw = unicodedata.normalize("NFKD", name.strip())
    pieces: list[str] = []
    for ch in raw:
        if ch.isascii() and ch.isalnum():
            pieces.append(ch.lower())
        elif unicodedata.category(ch).startswith("M"):
            continue
        else:
            pieces.append("_")
    slug = "_".join(part for part in "".join(pieces).split("_") if part)
    if len(slug) > 40:
        slug = slug[:40].rstrip("_")
    if not slug:
        raise SystemExit("switchyard: project slug cannot be empty")
    return launcher._validate_project_slug(slug)


def _legacy_dash_slug_from_project_name(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    if not slug:
        raise SystemExit("switchyard: project slug cannot be empty")
    return slug


def _validate_project_slug(value: str) -> str:
    from scripts import team_launcher as launcher

    slug = value.strip().lower()
    if not launcher.PROJECT_SLUG_RE.fullmatch(slug):
        raise SystemExit("switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$")
    return slug
