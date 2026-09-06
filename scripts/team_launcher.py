"""JSON-driven project team launcher for tmux-backed CLI panes."""

from __future__ import annotations

import argparse
import errno
import grp
import hashlib
import json
import math
import os
import pwd
import re
import shutil
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scripts.ticket_board.project_provision import (
    DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    DEFAULT_PG_IDENT_MAP,
    ProjectBoardProvision,
    ROLE_RE,
    build_plan,
    render_add_role_sql,
    render_board_unit,
    render_canary_unit,
    render_vcs_close_role_sql,
    sql_identifier,
    validate_ticket_prefix,
    write_artifacts,
)
from scripts.ticket_board.codex_hook_trust import (
    codex_command_hook_trust_entries as _codex_command_hook_trust_entries,
    codex_hook_current_hash as _codex_hook_current_hash,
    codex_hook_event_key as _codex_hook_event_key,
    codex_hook_timeout as _codex_hook_timeout,
    codex_trusted_hashes as _codex_trusted_hashes,
)
from scripts.ticket_board.commit_repos import commit_git_dir_env_for_project

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "team-launcher"
DEFAULT_SWITCHYARD_REGISTRY_DIR = Path("/etc/switchyard/projects")
SWITCHYARD_REGISTRY_SCHEMA = "switchyard.project-registry.v1"
SWITCHYARD_NAME = "switchyard"
TEAM_LAUNCHER_NAME = "team-launcher"
DEFAULT_LEGACY_RUNTIME_SESSION_DIR = Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions")
GENERIC_DEFAULT_STATE_DIR_NAME = "ticket-board"
LIVE_PGU_STATE_DIR_NAME = "pgu-ticket-board"
REMOVED_CONFIG_FREE_COMMANDS = frozenset({"bootstrap"})
BOOTSTRAP_REPLACEMENT_COMMAND = "switchyard new"
PROJECT_DESIGN_ARTIFACT_SCHEMA = "switchyard.project.v1"
PROJECT_DESIGN_DEFAULT_GATES = {
    "audit_signoff": True,
    "needs_inspection": False,
    "needs_user_signoff": False,
}
PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS = {
    "board_service_traversal": True,
    "supplementary_groups": [],
    "linger": True,
    "shell": "fish",
    # Empty means no credential sharing. A username here grants every role pane of
    # this project the ability to act as that user's Google account in agy; see
    # _seed_agy_credential_for_owner. The origin records how that value was chosen, so a
    # host default is distinguishable from a per-project override or an explicit opt-out.
    "agy_credential_source": "",
    "agy_credential_source_origin": "unset",
}
AGY_SOURCE_ORIGINS = frozenset({"unset", "host_default", "project_override", "opt_out"})
PROJECT_DESIGN_FORBIDDEN_KEYS = frozenset(
    {
        "stages",
        "workflow",
        "workflow_stages",
        "workflow_transitions",
        "extra_implementer_roles",
    }
)
WORKTREE_POLICIES = frozenset({"shared", "isolated"})
SWITCHYARD_PROJECT_DIR_NAME = ".switchyard"
SWITCHYARD_DESIGN_FILE_NAME = "PROJECT_DESIGN.md"
SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME = "DESIGNER_ONBOARDING.md"
SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME = "DIRECTOR_ONBOARDING.md"
SWITCHYARD_ONBOARDING_DOC_NAMES = (
    "README.md",
    "adversarial-collaborative-methodology.md",
    "switchyard-board-guide.md",
    "switchyard-director-guide.md",
)
DEFAULT_PANE_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT = Path("/opt/switchyard")
SWITCHYARD_RELEASE_MARKER_NAME = ".switchyard-release.json"
BOARD_SKILL_NAME = "switchyard-board"
BOARD_SKILL_INSTALLER_NAME = "switchyard-board-skill"


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


DEFAULT_SESSION_DIR = (
    Path(_env_first("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")).expanduser()
    if _env_first("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
    else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / LIVE_PGU_STATE_DIR_NAME
    / "pane-sessions"
)
DEFAULT_INTERIM_SESSION_BACKUP_DIR = Path.home() / ".local" / "state" / "pgu-ticket-board" / "pane-sessions"
DEFAULT_PANE_STATE_DIR = (
    Path(_env_first("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")).expanduser()
    if _env_first("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")
    else Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")
)
USER_BIN_ENV = "TEAM_LAUNCHER_BIN_DIR"
LEGACY_USER_BIN_ENV = "PGU_TEAM_LAUNCHER_BIN_DIR"
GUI_USER_ENV = "TEAM_LAUNCHER_GUI_USER"
LEGACY_GUI_USER_ENV = "PGU_TEAM_LAUNCHER_GUI_USER"
GUI_WAYLAND_ENV = "TEAM_LAUNCHER_WAYLAND_DISPLAY"
LEGACY_GUI_WAYLAND_ENV = "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"
HOST_WAYLAND_ENV = "HOST_WAYLAND_DISPLAY"
LEGACY_HOST_WAYLAND_ENV = "PGU_HOST_WAYLAND_DISPLAY"
LAYOUT_MODE_AUTO = "auto"
LAYOUT_MODE_SEPARATE = "separate"
LAYOUT_MODE_VIEWER = "viewer"
LAYOUT_MODE_CHOICES = frozenset({LAYOUT_MODE_AUTO, LAYOUT_MODE_SEPARATE, LAYOUT_MODE_VIEWER})
DEFAULT_VIEWER_COLUMNS = 240
DEFAULT_VIEWER_ROWS = 80
DEFAULT_TMUX_HISTORY_LIMIT = 200_000
MAX_VISIBLE_PANES_PER_WINDOW = 6
SWITCHYARD_VERSION = "dev"
SWITCHYARD_COMMANDS = (
    "board-skill",
    "new",
    "register",
    "upgrade",
    "add-role",
    "present",
    "set-vcs-close-role",
    "agy-credential",
    "seed-role-credentials",
    "role-prompt",
    "stop",
    "teardown",
    "status",
    "validate-models",
)
# `present` and `board-skill` act on the caller's own runtime -- display slots
# and the caller's CLI skill trees -- so neither needs to escalate. `role-prompt`
# writes the tenant's own configuration through the board's workflow API as the
# invoking user, which is the point: the director sets a role's remit without root
# and without hand-editing a generated artifact.
SWITCHYARD_UNPRIVILEGED_COMMANDS = frozenset({"present", "board-skill", "role-prompt"})
SWITCHYARD_PRIVILEGED_COMMANDS = frozenset(
    command for command in SWITCHYARD_COMMANDS if command not in SWITCHYARD_UNPRIVILEGED_COMMANDS
)
YOLO_ARGS_BY_CLI = {
    "agy": ["--dangerously-skip-permissions"],
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
    "hermes": ["--yolo"],
}
STARTUP_ARGS_BY_CLI = {
    "hermes": ["--accept-hooks", "--pass-session-id"],
}
EFFORT_STYLE_BY_CLI = {
    "agy": None,
    "claude": "flag",
    "codex": "config",
    "hermes": "reasoning",
}
SUPPORTED_CONFIG_CLI_NAMES = ("agy", "claude", "codex", "hermes")


def viewer_session_for_project(project: str) -> str:
    return f"{project}-viewer"


def project_window_title(config: ProjectConfig) -> str:
    return config.project_name.strip() or config.project
KNOWN_LIVE_CLI_NAMES = set(SUPPORTED_CONFIG_CLI_NAMES)
DEFAULT_MODEL_ARG_BY_CLI = {
    "hermes": "-m",
}
DEFAULT_RESUME_MODE_BY_CLI = {
    "agy": "flag",
    "claude": "flag",
    "codex": "subcommand",
    "hermes": "flag",
}
DEFAULT_RESUME_FLAG_BY_CLI = {
    "agy": "--conversation",
    "claude": "--resume",
    "hermes": "--resume",
}
DEFAULT_RESUME_SUBCOMMAND_BY_CLI = {
    "codex": "resume",
}
AGY_CONVERSATION_ROOT = Path.home() / ".gemini" / "antigravity-cli"
AGY_CREDENTIAL_DIR_NAME = ".gemini/antigravity-cli"
AGY_CREDENTIAL_TOKEN_NAME = "antigravity-oauth-token"
# Machine-scoped, so the operator names the account once instead of on every provision.
DEFAULT_AGY_CREDENTIAL_SETTING_PATH = Path("/etc/switchyard/agy-credential.json")
AGY_SOURCE_FROM_HOST = "host_default"
AGY_SOURCE_FROM_OVERRIDE = "project_override"
AGY_SOURCE_FROM_OPT_OUT = "opt_out"
AGY_SOURCE_UNSET = "unset"
CLAUDE_PROJECTS_DIR_NAME = ".claude/projects"
CODEX_SESSIONS_DIR_NAME = ".codex/sessions"
HERMES_HOME_DIR_NAME = ".hermes"
HERMES_SHARED_HOME_ENTRIES = (
    ".env",
    "SOUL.md",
    "auth.json",
    "auth.lock",
    "bin",
    "config.yaml",
    "hooks",
    "models_dev_cache.json",
    "shell-hooks-allowlist.json",
    "shell-hooks-allowlist.json.lock",
    "skills",
)
HERMES_PRIVATE_HOME_ENTRIES = frozenset(
    {
        ".hermes_history",
        ".skills_prompt_snapshot.json",
        ".update_check",
        "audio_cache",
        "cache",
        "cron",
        "image_cache",
        "logs",
        "memories",
        "pairing",
        "sandboxes",
        "sessions",
        "state.db",
        "state.db-shm",
        "state.db-wal",
    }
)
HERMES_SHARED_LOCK_GUARDS = {
    "auth.lock": "auth.json",
    "shell-hooks-allowlist.json.lock": "shell-hooks-allowlist.json",
}
RESUME_STARTUP_TIMEOUT_SECONDS = 1.5
RESUME_STARTUP_POLL_SECONDS = 0.1
DETACHED_SESSION_STABILITY_SECONDS = 2.0
RUNTIME_READY_ATTEMPTS = 50
RUNTIME_READY_POLL_SECONDS = 0.1
LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS = 10.0
LAUNCH_SESSION_RECORD_POLL_SECONDS = 0.2
# Used only by direct report callers that cannot provide pane-state launch outcomes.
LAUNCH_RESUME_FALLBACK_GRACE_SECONDS = 2.0
LAUNCH_RESUME_FALLBACK_FRESHNESS_SKEW_NS = 100_000_000
LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS = 0.1
ALLOW_STALE_LAUNCHER_ENV = "TEAM_LAUNCHER_ALLOW_STALE"
LEGACY_ALLOW_STALE_LAUNCHER_ENV = "PGU_TEAM_LAUNCHER_ALLOW_STALE"
NO_LAUNCHER_SELF_DEPLOY_ENV = "TEAM_LAUNCHER_NO_SELF_DEPLOY"
LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV = "PGU_TEAM_LAUNCHER_NO_SELF_DEPLOY"
DEFAULT_TENANT_RELEASE_DEPLOY_REF = "origin/main"
PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
# A credential-source user name is joined onto a home base to locate a token, so it is
# constrained to a plain useradd-style name: no separators, no traversal.
OWNER_USER_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SUPPORTED_NEW_PROJECT_CLIS = ("claude", "codex", "agy", "hermes")
NEW_PROJECT_ROLE_CLI_DEFAULTS = {
    "designer": "claude",
    "director": "claude",
    "audit": "claude",
}
SWITCHYARD_PROMPT_MAX_ATTEMPTS = 5
NEW_PROJECT_REQUIRED_ROLES = (
    "director",
)
NEW_PROJECT_FIXED_ROLE_NAMES = frozenset({"designer", "director", "audit"})
NEW_PROJECT_RESERVED_ROLE_NAMES = frozenset({"designer", "director", "audit", "user", "unassigned"})
NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES = frozenset({"designer", "director", "user", "unassigned"})
NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES = ("main", "ops")
NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES = (
    ("main", "core/domain implementation and integration"),
    ("ops", "environment, services, tooling, and infrastructure"),
    ("app", "application/UI work"),
    ("research", "investigation, design support, and unknowns"),
    ("perf", "measurement and performance work"),
)


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
    # SYRD-39: the Unix account this role runs as. One account per role is
    # what makes SO_PEERCRED's uid an authoritative role identity on the
    # board socket. Empty falls back to the project account, which is the
    # pre-migration arrangement and cannot separate roles.
    run_as_user: str = ""
    unset_env: tuple[str, ...] = ()


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


@dataclass(frozen=True)
class WorktreeProvisionResult:
    failed_roles: dict[str, str]

    @property
    def ok(self) -> bool:
        return not self.failed_roles


@dataclass(frozen=True)
class LauncherUpgradeResult:
    changed: bool
    message: str


@dataclass(frozen=True)
class TenantReleaseStatus:
    board_root: Path
    owner_user: str
    owner_home: Path
    provisioned_system_unit: Path | None
    commit_git_dir: str
    current_release: Path | None
    current_sha: str
    target_sha: str
    deploy_ref: str
    source_repo: Path
    resolve_error: str = ""
    clone_source_repo: Path | None = None

    @property
    def unchanged(self) -> bool:
        return bool(self.current_sha and self.target_sha and self.current_sha == self.target_sha)


@dataclass(frozen=True)
class ProjectDesignArtifact:
    project: str
    project_name: str
    ticket_prefix: str
    owner_user: str
    repository: Path
    remote: str
    default_branch: str
    worktree_policy: str
    design_document: Path
    implementer_roles: tuple[str, ...]
    audit_roles: tuple[str, ...]
    role_clis: tuple[tuple[str, str], ...]
    include_designer: bool
    include_audit: bool
    push_policy: str
    gates: dict[str, bool]
    capability_grants: dict[str, object]


@dataclass(frozen=True)
class LauncherCheckoutProbe:
    ahead: int = 0
    behind: int = 0
    behind_exact: bool = True
    error: str = ""


@dataclass(frozen=True)
class SharedSwitchyardRelease:
    root: Path
    marker_commit: str = ""
    marker_error: str = ""

    @property
    def active(self) -> bool:
        return bool(self.marker_commit)

    @property
    def undeterminable(self) -> bool:
        return bool(self.error)


@dataclass(frozen=True)
class LaunchSessionRecordStatus:
    role: str
    target: str
    session_id: str
    superseded_session_id: str = ""
    unverified_resume_session_id: str = ""
    pane_state_source: str = ""
    runtime_hook_source: str = ""
    attached_to_running: bool = False

    @property
    def found(self) -> bool:
        return bool(self.session_id)

    @property
    def resume_fallback(self) -> bool:
        return bool(self.superseded_session_id)

    @property
    def unverified_resume(self) -> bool:
        return bool(self.unverified_resume_session_id)

    @property
    def pane_launch_reported(self) -> bool:
        return bool(
            self.attached_to_running
            or self.pane_state_source
            or self.runtime_hook_source
            or self.unverified_resume_session_id
        )

    @property
    def runtime_hook_reported(self) -> bool:
        return bool(self.runtime_hook_source)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def switchyard_shared_install_root() -> Path:
    return Path(os.environ.get("SWITCHYARD_SHARED_INSTALL_ROOT", DEFAULT_SWITCHYARD_SHARED_INSTALL_ROOT)).expanduser()


def switchyard_bare_repo() -> Path | None:
    selected = os.environ.get("SWITCHYARD_BARE_REPO", "").strip()
    if not selected:
        return None
    cache = Path(selected).expanduser()
    if not cache.is_absolute():
        raise SystemExit("switchyard: SWITCHYARD_BARE_REPO must be an absolute source-cache path")
    return cache


def switchyard_shared_target(root: Path | None = None) -> Path:
    return (root or switchyard_shared_install_root()) / "current" / SWITCHYARD_NAME


def switchyard_shared_pane_launcher(root: Path | None = None) -> Path:
    return (root or switchyard_shared_install_root()) / "current" / "scripts" / TEAM_LAUNCHER_NAME


def _read_switchyard_release_marker(path: Path) -> SharedSwitchyardRelease | None:
    marker = path / SWITCHYARD_RELEASE_MARKER_NAME
    if not marker.exists():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SharedSwitchyardRelease(root=path, marker_error=str(exc))
    commit = str(payload.get("commit") or "").strip()
    if not commit:
        return SharedSwitchyardRelease(root=path, marker_error=f"{marker} has no commit")
    return SharedSwitchyardRelease(root=path, marker_commit=commit)


def shared_switchyard_release_for_path(path: Path, *, install_root: Path | None = None) -> SharedSwitchyardRelease | None:
    root = (install_root or switchyard_shared_install_root()).expanduser().resolve(strict=False)
    candidate = path.expanduser().resolve(strict=False)
    if not _path_is_under(candidate, root):
        return None
    for probe in (candidate, *candidate.parents):
        if not _path_is_under(probe, root):
            break
        marker = _read_switchyard_release_marker(probe)
        if marker is not None:
            return marker
    if _path_is_under(candidate, root / "current") or _path_is_under(candidate, root / "releases"):
        return SharedSwitchyardRelease(root=candidate)
    return None


def switchyard_version_text(repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    release = _read_switchyard_release_marker(root)
    if release is not None and release.marker_commit:
        return f"switchyard {release.marker_commit}"
    return f"switchyard {SWITCHYARD_VERSION}"


def _allocated_board_port(project: str, *, base: int = 18_770, span: int = 10_000) -> int:
    if project == "pgu":
        return 8770
    digest = hashlib.blake2s(project.encode("utf-8"), digest_size=2).digest()
    return base + (int.from_bytes(digest, "big") % span)


def _default_board_url(project: str) -> str:
    return f"http://127.0.0.1:{_allocated_board_port(project)}"


def _default_board_socket(project: str) -> str:
    return f"/run/{project}-ticket-board/ticket-board.sock"


def uid_for_user(user_name: str) -> int | None:
    try:
        return int(pwd.getpwnam(user_name).pw_uid)
    except KeyError:
        return None


def current_user_name() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return ""


def runtime_dir_for_uid(uid: int) -> Path:
    return Path(f"/run/user/{uid}")


def home_dir_for_user(user_name: str) -> Path | None:
    user = user_name.strip()
    if not user:
        return None
    try:
        return Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        return Path("/home") / user


def _explicit_session_dir_from_env() -> Path | None:
    value = _env_first("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR")
    return Path(value).expanduser() if value else None


def _explicit_pane_state_dir_from_env() -> Path | None:
    value = _env_first("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")
    return Path(value).expanduser() if value else None


def default_session_dir_for_user(user_name: str) -> Path:
    explicit = _explicit_session_dir_from_env()
    if explicit is not None:
        return explicit
    owner_home = home_dir_for_user(user_name)
    if owner_home is None:
        return DEFAULT_SESSION_DIR
    return owner_home / ".local" / "state" / LIVE_PGU_STATE_DIR_NAME / "pane-sessions"


def default_pane_state_dir_for_user(user_name: str, *, project: str) -> Path:
    explicit = _explicit_pane_state_dir_from_env()
    if explicit is not None:
        return explicit
    user = user_name.strip()
    if not user:
        return DEFAULT_PANE_STATE_DIR
    uid = uid_for_user(user)
    if uid is None:
        return DEFAULT_PANE_STATE_DIR
    return runtime_dir_for_uid(uid) / f"{project}-ticket-board" / "pane-state"


def account_session_dir(account: str, *, project: str) -> Path:
    """Session records for one account of one project.

    Deliberately ignores the ambient TICKET_BOARD_PANE_SESSION_DIR: that names
    the CURRENT pane's project and user, so honouring it when computing another
    role's or another project's path hands a role a directory belonging to
    something else. It is also project-scoped, which the owner-facing helper is
    not -- that one still hardcodes the legacy pgu state directory (SYRD-39).
    """
    home = home_dir_for_user(account)
    if home is None:
        return DEFAULT_SESSION_DIR
    return home / ".local" / "state" / f"{project}-ticket-board" / "pane-sessions"


def shared_pane_state_dir(project: str) -> Path:
    """Where every role of a project records pane activity.

    Role accounts cannot write the owner's XDG runtime directory, and the notify
    listener cannot read theirs, so per-role state under each role's own
    /run/user is written where nobody reads it. This is the deliberate
    aggregation path: the board's runtime directory, group-owned by the
    project's roles group so every role writes it and the listener reads it
    (SYRD-39).
    """
    return Path("/run") / f"{project}-ticket-board" / "pane-state"


def loginctl_enable_linger_args(user_name: str) -> list[str]:
    return ["loginctl", "enable-linger", user_name]


def ensure_user_linger_runtime(
    user_name: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    runtime_exists: Callable[[Path], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = RUNTIME_READY_ATTEMPTS,
    poll_seconds: float = RUNTIME_READY_POLL_SECONDS,
) -> Path:
    user = user_name.strip()
    if not user:
        raise SystemExit("team-launcher: cannot provision runtime for an empty user name")
    uid = uid_for_user(user)
    if uid is None:
        raise SystemExit(f"team-launcher: cannot provision runtime for unknown user {user!r}")
    runtime_dir = runtime_dir_for_uid(uid)
    exists = runtime_exists or (lambda path: path.is_dir())
    result = runner(
        loginctl_enable_linger_args(user),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            f"team-launcher: failed to enable linger for {user!r}{detail}; "
            f"run `sudo loginctl enable-linger {user}` and retry"
        )
    for _ in range(max(1, attempts)):
        if exists(runtime_dir):
            return runtime_dir
        sleeper(poll_seconds)
    raise SystemExit(
        f"team-launcher: linger is enabled for {user!r}, but {runtime_dir} is still missing; "
        "start or restart that user's systemd user manager and retry"
    )


def session_dir_uses_user_runtime(session_dir: Path, user_name: str) -> bool:
    uid = uid_for_user(user_name)
    if uid is None:
        return False
    runtime_dir = runtime_dir_for_uid(uid).resolve(strict=False)
    session_path = session_dir.resolve(strict=False)
    return session_path == runtime_dir or runtime_dir in session_path.parents


def ensure_configured_runtime_user(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    runtime_exists: Callable[[Path], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    attempts: int = RUNTIME_READY_ATTEMPTS,
) -> Path | None:
    user = config.run_as_user or current_user_name()
    if not user or not session_dir_uses_user_runtime(config.session_dir, user):
        return None
    return ensure_user_linger_runtime(
        user,
        runner=runner,
        runtime_exists=runtime_exists,
        sleeper=sleeper,
        attempts=attempts,
    )


def default_user_bin(user_name: str = "") -> str:
    return default_user_bin_dirs(user_name)[0]


def default_user_bin_dirs(user_name: str = "") -> list[str]:
    configured = _env_first(USER_BIN_ENV, LEGACY_USER_BIN_ENV)
    if configured:
        return [str(Path(configured).expanduser())]
    owner_home = home_dir_for_user(user_name) if user_name.strip() else None
    if owner_home is not None:
        return [str(owner_home / "bin"), str(owner_home / ".local" / "bin")]
    home = Path.home()
    return [str(home / "bin"), str(home / ".local" / "bin")]


def _owner_home_bin_dirs(owner_home: Path) -> list[str]:
    configured = _env_first(USER_BIN_ENV, LEGACY_USER_BIN_ENV)
    if configured:
        return [str(Path(configured).expanduser())]
    home = owner_home.expanduser()
    return [str(home / "bin"), str(home / ".local" / "bin")]


def default_pane_base_path(user_name: str = "") -> str:
    user = user_name.strip()
    if user and current_user_name() != user:
        return DEFAULT_PANE_BASE_PATH
    return os.environ.get("PATH", "")


def default_gui_user() -> str:
    configured = _env_first(GUI_USER_ENV, LEGACY_GUI_USER_ENV)
    if configured:
        return configured
    sudo_user = _env_first("SUDO_USER")
    if sudo_user:
        return sudo_user
    return current_user_name()


def normalize_wayland_display(display: str, *, gui_user: str) -> str | None:
    display = display.strip()
    if display.startswith("/"):
        return display
    user = gui_user.strip()
    uid = uid_for_user(user) if user else None
    if uid is None:
        return None
    name = display or "wayland-0"
    return f"/run/user/{uid}/{name}"


def konsole_launch_args(layout_path: Path, *, gui_user: str | None = None, window_title: str = "") -> list[str]:
    user = (gui_user if gui_user is not None else default_gui_user()).strip()
    wayland_display = None
    host_wayland_display = _env_first(HOST_WAYLAND_ENV, LEGACY_HOST_WAYLAND_ENV)
    if host_wayland_display:
        wayland_display = normalize_wayland_display(host_wayland_display, gui_user=user)
    if not wayland_display:
        wayland_name = _env_first(GUI_WAYLAND_ENV, LEGACY_GUI_WAYLAND_ENV) or "wayland-0"
        wayland_display = normalize_wayland_display(wayland_name, gui_user=user)
    if not wayland_display:
        return [
            "sh",
            "-lc",
            "printf '%s\\n' 'team-launcher: no host Wayland display; run from Eric desktop session' >&2; exit 1",
        ]
    args = [
        "env",
        "QT_QPA_PLATFORM=wayland",
        f"WAYLAND_DISPLAY={wayland_display}",
        "konsole",
        "--separate",
    ]
    title = window_title.strip()
    if title:
        args.extend(["--qwindowtitle", title])
    args.extend(
        [
            "--layout",
            str(layout_path),
        ]
    )
    return args


def _desktop_is_kde(desktop: str) -> bool:
    tokens = {
        token.strip().casefold()
        for chunk in desktop.replace(";", ":").split(":")
        for token in [chunk]
        if token.strip()
    }
    return bool(tokens & {"kde", "plasma"})


def _desktop_from_loginctl_output(output: str) -> str:
    values: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            if name != "Desktop":
                continue
            value = value.strip()
            if value:
                values.append(value)
        else:
            values.append(line)
    return ":".join(values)


def detected_invoking_desktop(
    *,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    env = environ if environ is not None else os.environ
    desktop = str(env.get("XDG_CURRENT_DESKTOP", "")).strip()
    if desktop:
        return desktop
    if str(env.get("KDE_FULL_SESSION", "")).strip().casefold() in {"1", "true"}:
        return "KDE"
    user = str(env.get("SUDO_USER") or env.get("USER") or "").strip()
    if not user:
        return ""
    try:
        display_proc = runner(
            ["loginctl", "show-user", user, "-p", "Display", "--value"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return ""
    if display_proc.returncode != 0:
        return ""
    session_id = str(display_proc.stdout or "").strip()
    if not session_id:
        return ""
    try:
        session_proc = runner(
            ["loginctl", "show-session", session_id, "-p", "Desktop"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return ""
    if session_proc.returncode != 0:
        return ""
    return _desktop_from_loginctl_output(str(session_proc.stdout or ""))


def resolve_layout_mode(
    requested: str,
    *,
    environ: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    if requested not in LAYOUT_MODE_CHOICES:
        raise SystemExit(f"unknown layout mode: {requested}")
    if requested != LAYOUT_MODE_AUTO:
        return requested
    desktop = detected_invoking_desktop(environ=environ, runner=runner)
    if not desktop:
        return LAYOUT_MODE_VIEWER
    return LAYOUT_MODE_SEPARATE if _desktop_is_kde(desktop) else LAYOUT_MODE_VIEWER


def visible_roles_for_viewer(config: ProjectConfig) -> list[RoleConfig]:
    return sorted(
        (role for role in config.roles if not role.detached and role.slot is not None),
        key=lambda role: (role.slot if role.slot is not None else 0, role.role),
    )


def tmux_set_status_off_args(role: RoleConfig) -> list[str]:
    return ["tmux", "set-option", "-t", role.tmux_session, "status", "off"]


def tmux_set_mouse_args(session: str) -> list[str]:
    return ["tmux", "set-option", "-t", session, "mouse", "on"]


def tmux_set_history_limit_args(session: str) -> list[str]:
    return ["tmux", "set-option", "-t", session, "history-limit", str(DEFAULT_TMUX_HISTORY_LIMIT)]


def configure_tmux_session_options(
    session: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    for args in (tmux_set_mouse_args(session), tmux_set_history_limit_args(session)):
        proc = runner(args)
        if proc.returncode != 0:
            return int(proc.returncode)
    return 0


def tmux_set_pane_border_status_off_args(role: RoleConfig) -> list[str]:
    return ["tmux", "set-window-option", "-t", f"{role.tmux_session}:0", "pane-border-status", "off"]


def tmux_viewer_new_session_args(viewer_session: str, role: RoleConfig) -> list[str]:
    command = _quote_command(["env", "TMUX=", "tmux", "attach", "-t", role.tmux_session])
    return [
        "tmux",
        "new-session",
        "-d",
        "-x",
        str(DEFAULT_VIEWER_COLUMNS),
        "-y",
        str(DEFAULT_VIEWER_ROWS),
        "-s",
        viewer_session,
        "-c",
        role.workdir,
        command,
    ]


def tmux_viewer_split_window_args(viewer_session: str, role: RoleConfig) -> list[str]:
    command = _quote_command(["env", "TMUX=", "tmux", "attach", "-t", role.tmux_session])
    return ["tmux", "split-window", "-t", f"{viewer_session}:0", "-c", role.workdir, command]


def tmux_viewer_select_layout_args(viewer_session: str) -> list[str]:
    return ["tmux", "select-layout", "-t", f"{viewer_session}:0", "tiled"]


def tmux_viewer_set_status_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "status", "on"]


def tmux_viewer_set_titles_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "set-titles", "on"]


def tmux_viewer_set_titles_string_args(viewer_session: str, title: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "set-titles-string", title]


def tmux_viewer_set_prefix_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-option", "-t", viewer_session, "prefix", "C-a"]


def tmux_viewer_set_border_status_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-window-option", "-t", f"{viewer_session}:0", "pane-border-status", "top"]


def tmux_viewer_set_border_format_args(viewer_session: str) -> list[str]:
    return ["tmux", "set-window-option", "-t", f"{viewer_session}:0", "pane-border-format", " #{@role} "]


def tmux_viewer_set_role_arg(viewer_session: str, pane_index: int, role: str) -> list[str]:
    return ["tmux", "set-option", "-p", "-t", f"{viewer_session}.{pane_index}", "@role", role]


def launch_tmux_viewer_session(
    roles: Sequence[RoleConfig],
    *,
    viewer_session: str,
    window_title: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    if not roles:
        print("team-launcher: no visible roles for viewer layout", file=sys.stderr)
        return 1
    if runner(tmux_has_session_by_name_args(viewer_session), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        kill_proc = runner(tmux_kill_session_by_name_args(viewer_session))
        if kill_proc.returncode != 0:
            return int(kill_proc.returncode)
    for role in roles:
        for args in (tmux_set_status_off_args(role), tmux_set_pane_border_status_off_args(role)):
            proc = runner(args)
            if proc.returncode != 0:
                return int(proc.returncode)
    first, *rest = roles
    proc = runner(tmux_viewer_new_session_args(viewer_session, first))
    if proc.returncode != 0:
        return int(proc.returncode)
    configure_result = configure_tmux_session_options(viewer_session, runner=runner)
    if configure_result != 0:
        return configure_result
    for role in rest:
        proc = runner(tmux_viewer_split_window_args(viewer_session, role))
        if proc.returncode != 0:
            return int(proc.returncode)
    for args in (
        tmux_viewer_select_layout_args(viewer_session),
        tmux_viewer_set_status_args(viewer_session),
        tmux_viewer_set_titles_args(viewer_session),
        tmux_viewer_set_titles_string_args(viewer_session, window_title or viewer_session),
        tmux_viewer_set_prefix_args(viewer_session),
        tmux_viewer_set_border_status_args(viewer_session),
        tmux_viewer_set_border_format_args(viewer_session),
    ):
        proc = runner(args)
        if proc.returncode != 0:
            return int(proc.returncode)
    for index, role in enumerate(roles):
        proc = runner(tmux_viewer_set_role_arg(viewer_session, index, role.role))
        if proc.returncode != 0:
            return int(proc.returncode)
    return 0


def launch_konsole_window(
    layout_path: Path,
    *,
    project: str,
    window_title: str = "",
    gui_user: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    process_launcher: Callable[..., Any] | None = None,
) -> int:
    args = konsole_launch_args(layout_path, gui_user=gui_user, window_title=window_title or project)
    if args[:2] == ["sh", "-lc"]:
        return runner(args).returncode
    launch_process = process_launcher or subprocess.Popen
    try:
        with tempfile.NamedTemporaryFile(
            mode="ab",
            prefix=f"{project}-team-launcher-konsole.",
            suffix=".log",
            delete=False,
        ) as handle:
            log_path = Path(handle.name)
            _make_konsole_log_readable(log_path)
            proc = launch_process(
                args,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    except OSError as exc:
        print(f"team-launcher: failed to launch Konsole: {exc}", file=sys.stderr)
        return 1
    time.sleep(0.2)
    returncode = proc.poll()
    if returncode is not None:
        _print_konsole_early_exit(returncode, log_path)
        return returncode
    print(
        f"team-launcher: started {project} in background (Konsole pid {proc.pid}; log {log_path})"
    )
    return 0


def _make_konsole_log_readable(log_path: Path, *, environ: dict[str, str] | None = None) -> None:
    env = environ if environ is not None else os.environ
    sudo_user = str(env.get("SUDO_USER") or "").strip()
    sudo_uid = _int_env(env.get("SUDO_UID"))
    sudo_gid = _int_env(env.get("SUDO_GID"))
    if sudo_user and (sudo_uid is None or sudo_gid is None):
        try:
            user_info = pwd.getpwnam(sudo_user)
        except KeyError:
            user_info = None
        if user_info is not None:
            sudo_uid = user_info.pw_uid if sudo_uid is None else sudo_uid
            sudo_gid = user_info.pw_gid if sudo_gid is None else sudo_gid
    if sudo_uid is not None and sudo_gid is not None:
        try:
            os.chown(log_path, sudo_uid, sudo_gid)
        except OSError:
            pass
    try:
        log_path.chmod(0o644)
    except OSError:
        pass


def _int_env(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _print_konsole_early_exit(returncode: int, log_path: Path) -> None:
    print(f"team-launcher: Konsole exited immediately with status {returncode}; captured output:", file=sys.stderr)
    try:
        captured = log_path.read_bytes()
    except OSError as exc:
        print(f"team-launcher: could not read Konsole log {log_path}: {exc}", file=sys.stderr)
        return
    if not captured:
        print(f"team-launcher: Konsole log {log_path} was empty", file=sys.stderr)
        return
    text = captured.decode("utf-8", errors="replace").rstrip()
    if text:
        print(text, file=sys.stderr)


def _expand_path(value: str, *, base: Path) -> Path:
    expanded = os.path.expandvars(value)
    path = Path(expanded).expanduser()
    return path if path.is_absolute() else base / path


def _load_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return parsed


def _artifact_forbidden_keys(raw: dict[str, Any]) -> list[str]:
    found = [key for key in raw if key in PROJECT_DESIGN_FORBIDDEN_KEYS]
    project_raw = raw.get("project")
    if isinstance(project_raw, dict):
        found.extend(f"project.{key}" for key in project_raw if key in PROJECT_DESIGN_FORBIDDEN_KEYS)
    return sorted(found)


def _artifact_string(raw: dict[str, Any], key: str, *, path: Path, default: str = "") -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise SystemExit(f"{path} field {key!r} must be a JSON string")
    value = value.strip()
    if not value:
        raise SystemExit(f"{path} field {key!r} must be non-empty")
    return value


def _artifact_optional_string(raw: dict[str, Any], key: str, *, path: Path, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SystemExit(f"{path} field {key!r} must be a JSON string")
    return value.strip() or default


def _artifact_role_list(raw: Any, *, path: Path, field: str, defaults: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(defaults)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise SystemExit(f"{path} field {field!r} must be a JSON string list")
    result: list[str] = []
    for item in raw:
        role = item.strip().lower()
        if role in {"designer", "director", "audit", "user", "unassigned"}:
            raise SystemExit(f"{path} field {field!r} contains reserved role {role!r}")
        if role not in result:
            result.append(role)
    if not result:
        raise SystemExit(f"{path} field {field!r} must contain at least one implementer role")
    return tuple(result)


def _artifact_audit_role_list(raw: Any, *, path: Path, defaults: Sequence[str]) -> tuple[str, ...]:
    if raw is None:
        return tuple(defaults)
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise SystemExit(f"{path} field 'project.audit_roles' must be a JSON string list")
    result: list[str] = []
    for item in raw:
        role = item.strip().lower()
        if not ROLE_RE.fullmatch(role):
            raise SystemExit(f"{path} field 'project.audit_roles' role {role!r} must match ^[a-z][a-z0-9_-]{{0,63}}$")
        if role in NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES:
            raise SystemExit(f"{path} field 'project.audit_roles' contains reserved role {role!r}")
        if role not in result:
            result.append(role)
    return tuple(result)


def _validate_new_project_cli(value: str, *, context: str = "CLI") -> str:
    cli = value.strip().lower()
    if cli not in SUPPORTED_NEW_PROJECT_CLIS:
        raise SystemExit(f"{context} must be one of {', '.join(SUPPORTED_NEW_PROJECT_CLIS)}")
    return cli


def _validate_new_project_implementer_role(value: str, *, context: str = "role") -> str:
    role = value.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit(f"{context} must match ^[a-z][a-z0-9_-]{{0,63}}$")
    if role in NEW_PROJECT_RESERVED_ROLE_NAMES:
        raise SystemExit(f"{context} {role!r} is reserved")
    return role


def _validate_new_project_audit_role(value: str, *, context: str = "audit role") -> str:
    role = value.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit(f"{context} must match ^[a-z][a-z0-9_-]{{0,63}}$")
    if role in NEW_PROJECT_NON_AUDIT_RESERVED_ROLE_NAMES:
        raise SystemExit(f"{context} {role!r} is reserved")
    return role


def _dedupe_role_names(roles: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for role in roles:
        if role not in result:
            result.append(role)
    return tuple(result)


def _default_role_cli_pairs(
    implementer_roles: Sequence[str],
    *,
    include_designer: bool,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    resolved_audit_roles = tuple(audit_roles) if audit_roles is not None else (("audit",) if include_audit else ())
    if include_designer:
        pairs.append(("designer", NEW_PROJECT_ROLE_CLI_DEFAULTS["designer"]))
    pairs.append(("director", NEW_PROJECT_ROLE_CLI_DEFAULTS["director"]))
    pairs.extend((role, NEW_PROJECT_ROLE_CLI_DEFAULTS["audit"]) for role in resolved_audit_roles)
    pairs.extend((role, "codex") for role in implementer_roles)
    return tuple(pairs)


def _dedupe_role_cli_pairs(role_clis: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, cli in role_clis:
        normalized_role = role.strip().lower()
        if normalized_role in NEW_PROJECT_RESERVED_ROLE_NAMES:
            if normalized_role not in NEW_PROJECT_FIXED_ROLE_NAMES:
                raise SystemExit(f"role {normalized_role!r} is reserved")
        else:
            _validate_new_project_implementer_role(normalized_role)
        normalized_cli = _validate_new_project_cli(cli, context=f"CLI for {normalized_role}")
        if normalized_role in seen:
            continue
        seen.add(normalized_role)
        result.append((normalized_role, normalized_cli))
    return tuple(result)


def _role_cli_map(role_clis: Sequence[tuple[str, str]]) -> dict[str, str]:
    return {role: cli for role, cli in role_clis}


def _require_new_project_roles(role_clis: Sequence[tuple[str, str]]) -> None:
    roles = {role for role, _cli in role_clis}
    missing = [role for role in NEW_PROJECT_REQUIRED_ROLES if role not in roles]
    if missing:
        raise SystemExit(f"switchyard: required roles missing: {', '.join(missing)}")


def _artifact_role_cli_pairs(
    raw: Any,
    *,
    path: Path,
    implementer_roles: Sequence[str],
    include_designer: bool,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    defaults = _default_role_cli_pairs(
        implementer_roles,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
    )
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'project.role_clis' must be a JSON object")
    allowed_roles = {role for role, _cli in defaults}
    pairs_by_role = dict(defaults)
    for raw_role, raw_cli in raw.items():
        if not isinstance(raw_role, str) or not isinstance(raw_cli, str):
            raise SystemExit(f"{path} field 'project.role_clis' must map role strings to CLI strings")
        role = raw_role.strip().lower()
        if role not in allowed_roles:
            raise SystemExit(f"{path} field 'project.role_clis' contains unknown role {role!r}")
        pairs_by_role[role] = _validate_new_project_cli(raw_cli, context=f"CLI for {role}")
    return tuple((role, pairs_by_role[role]) for role, _cli in defaults)


def _artifact_bool_mapping(raw: Any, *, path: Path, field: str, defaults: dict[str, bool]) -> dict[str, bool]:
    if raw is None:
        return dict(defaults)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field {field!r} must be a JSON object")
    result = dict(defaults)
    for key, value in raw.items():
        if key not in defaults:
            raise SystemExit(f"{path} field {field!r} contains unknown key {key!r}")
        if not isinstance(value, bool):
            raise SystemExit(f"{path} field {field}.{key} must be a JSON boolean")
        result[key] = value
    return result


def _artifact_capability_grants(raw: Any, *, path: Path) -> dict[str, object]:
    if raw is None:
        return dict(PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS)
    if not isinstance(raw, dict):
        raise SystemExit(f"{path} field 'capability_grants' must be a JSON object")
    result = dict(PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS)
    allowed = set(result)
    for key, value in raw.items():
        if key not in allowed:
            raise SystemExit(f"{path} field 'capability_grants' contains unknown key {key!r}")
        if key == "supplementary_groups":
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise SystemExit(f"{path} field 'capability_grants.supplementary_groups' must be a JSON string list")
            result[key] = [item.strip() for item in value]
        elif key == "shell":
            if not isinstance(value, str) or not value.strip():
                raise SystemExit(f"{path} field 'capability_grants.shell' must be a non-empty JSON string")
            result[key] = value.strip()
        elif key == "agy_credential_source":
            if not isinstance(value, str):
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source' must be a JSON string"
                )
            candidate = value.strip()
            if candidate and not _is_valid_owner_user_name(candidate):
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source' must be a plain Unix "
                    f"user name, not {candidate!r}"
                )
            result[key] = candidate
        elif key == "agy_credential_source_origin":
            if not isinstance(value, str) or value.strip() not in AGY_SOURCE_ORIGINS:
                raise SystemExit(
                    f"{path} field 'capability_grants.agy_credential_source_origin' must be "
                    f"one of {', '.join(sorted(AGY_SOURCE_ORIGINS))}"
                )
            result[key] = value.strip()
        elif not isinstance(value, bool):
            raise SystemExit(f"{path} field 'capability_grants.{key}' must be a JSON boolean")
        else:
            result[key] = value
    return result


def load_project_design_artifact(path: Path, *, expected_project: str | None = None) -> ProjectDesignArtifact:
    artifact_path = path.expanduser().resolve(strict=False)
    raw = _load_json(artifact_path)
    forbidden = _artifact_forbidden_keys(raw)
    if forbidden:
        raise SystemExit(
            f"{artifact_path} must not preconfigure stages or roles for new projects: {', '.join(forbidden)}"
        )
    schema = _artifact_string(raw, "schema", path=artifact_path)
    if schema != PROJECT_DESIGN_ARTIFACT_SCHEMA:
        raise SystemExit(
            f"{artifact_path} schema must be {PROJECT_DESIGN_ARTIFACT_SCHEMA!r}, got {schema!r}"
        )
    project_raw = raw.get("project")
    if not isinstance(project_raw, dict):
        raise SystemExit(f"{artifact_path} field 'project' must be a JSON object")
    forbidden = _artifact_forbidden_keys(project_raw)
    if forbidden:
        raise SystemExit(
            f"{artifact_path} must not preconfigure stages or roles for new projects: {', '.join(forbidden)}"
        )
    slug = _validate_project_slug(_artifact_string(project_raw, "slug", path=artifact_path))
    if expected_project is not None and slug != expected_project:
        raise SystemExit(f"{artifact_path} project slug {slug!r} does not match requested project {expected_project!r}")
    project_name = _artifact_optional_string(project_raw, "name", path=artifact_path, default=slug)
    ticket_prefix = validate_ticket_prefix(_artifact_string(project_raw, "ticket_prefix", path=artifact_path))
    owner_user = _artifact_string(project_raw, "owner_user", path=artifact_path)
    repository = _expand_path(_artifact_string(project_raw, "repository", path=artifact_path), base=artifact_path.parent)
    remote = _artifact_string(project_raw, "remote", path=artifact_path, default="origin")
    default_branch = _artifact_string(project_raw, "default_branch", path=artifact_path, default="main")
    worktree_policy = _artifact_string(project_raw, "worktree_policy", path=artifact_path, default="shared")
    if worktree_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"{artifact_path} field 'project.worktree_policy' must be one of {sorted(WORKTREE_POLICIES)}")
    design_document = _expand_path(_artifact_string(raw, "design_document", path=artifact_path), base=artifact_path.parent)
    implementer_roles = _artifact_role_list(
        project_raw.get("roles", project_raw.get("implementer_roles")),
        path=artifact_path,
        field="project.roles",
        defaults=DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    )
    include_designer_raw = project_raw.get("include_designer", True)
    if not isinstance(include_designer_raw, bool):
        raise SystemExit(f"{artifact_path} field 'project.include_designer' must be a JSON boolean")
    include_audit_raw = project_raw.get("include_audit", True)
    if not isinstance(include_audit_raw, bool):
        raise SystemExit(f"{artifact_path} field 'project.include_audit' must be a JSON boolean")
    audit_roles = _artifact_audit_role_list(
        project_raw.get("audit_roles"),
        path=artifact_path,
        defaults=("audit",) if include_audit_raw else (),
    )
    role_overlap = set(implementer_roles) & set(audit_roles)
    if role_overlap:
        raise SystemExit(
            f"{artifact_path} roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}"
        )
    role_clis = _artifact_role_cli_pairs(
        project_raw.get("role_clis"),
        path=artifact_path,
        implementer_roles=implementer_roles,
        include_designer=include_designer_raw,
        include_audit=bool(audit_roles),
        audit_roles=audit_roles,
    )
    push_policy = _artifact_string(project_raw, "push_policy", path=artifact_path, default="director-main-only")
    gates = _artifact_bool_mapping(project_raw.get("gates"), path=artifact_path, field="project.gates", defaults=PROJECT_DESIGN_DEFAULT_GATES)
    capability_grants = _artifact_capability_grants(project_raw.get("capability_grants"), path=artifact_path)
    return ProjectDesignArtifact(
        project=slug,
        project_name=project_name,
        ticket_prefix=ticket_prefix,
        owner_user=owner_user,
        repository=repository,
        remote=remote,
        default_branch=default_branch,
        worktree_policy=worktree_policy,
        design_document=design_document,
        implementer_roles=implementer_roles,
        audit_roles=audit_roles,
        role_clis=role_clis,
        include_designer=include_designer_raw,
        include_audit=bool(audit_roles),
        push_policy=push_policy,
        gates=gates,
        capability_grants=capability_grants,
    )


def project_design_artifact_payload(artifact: ProjectDesignArtifact) -> dict[str, Any]:
    return {
        "schema": PROJECT_DESIGN_ARTIFACT_SCHEMA,
        "design_document": str(artifact.design_document),
        "project": {
            "slug": artifact.project,
            "name": artifact.project_name,
            "ticket_prefix": artifact.ticket_prefix,
            "owner_user": artifact.owner_user,
            "repository": str(artifact.repository),
            "remote": artifact.remote,
            "default_branch": artifact.default_branch,
            "worktree_policy": artifact.worktree_policy,
            "roles": list(artifact.implementer_roles),
            "audit_roles": list(artifact.audit_roles),
            "include_designer": artifact.include_designer,
            "include_audit": artifact.include_audit,
            "role_clis": _role_cli_map(artifact.role_clis),
            "push_policy": artifact.push_policy,
            "gates": artifact.gates,
            "capability_grants": artifact.capability_grants,
        },
    }


def _resolve_launcher_project_config(
    project: str,
    *,
    explicit_config: Path | None = None,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> SwitchyardProjectEntry:
    if explicit_config is not None:
        return SwitchyardProjectEntry(slug=project, name=project, config_path=explicit_config)
    effective_config_dir = config_dir or DEFAULT_CONFIG_DIR
    effective_registry_dir = registry_dir or DEFAULT_SWITCHYARD_REGISTRY_DIR
    try:
        return _resolve_switchyard_project(
            project,
            config_dir=effective_config_dir,
            registry_dir=effective_registry_dir,
        )
    except SystemExit as exc:
        message = str(exc)
        if message.startswith("switchyard: unknown project"):
            raise SystemExit(
                f"team-launcher: unknown project {project!r}; "
                f"searched config dir {effective_config_dir} and registry dir {effective_registry_dir}"
            ) from exc
        raise


def _write_json_atomic(
    path: Path, payload: dict[str, Any], *, owner_user: str | None = None,
) -> None:
    owner = pwd.getpwnam(owner_user) if owner_user and os.geteuid() == 0 else None
    mode = 0o600
    if owner_user and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode) & 0o777
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            # Publish tenant controls with the correct owner already attached;
            # never leave a root-owned replacement for a later repair step.
            if owner is not None:
                os.fchown(handle.fileno(), owner.pw_uid, owner.pw_gid)
            if owner_user:
                os.fchmod(handle.fileno(), mode)
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_private_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)
    try:
        temp_path.chmod(0o600)
    except OSError:
        pass
    temp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _string_list(value: Any, *, field: str, role: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"role {role} {field} must be a JSON string list")
    return list(value)


def _bool_value(value: Any, *, field: str, role: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise SystemExit(f"role {role} {field} must be a JSON boolean")
    return value


def _role_from_json(project: str, raw: dict[str, Any], *, base: Path, default_workdir: Path | None) -> RoleConfig:
    role = str(raw.get("role") or "").strip()
    if not role:
        raise SystemExit("team launcher role entry missing role")
    tmux_session = str(raw.get("tmux_session") or f"{project}-{role}").strip()
    slot_raw = raw.get("slot")
    slot = None if slot_raw is None else int(slot_raw)
    cli_raw = raw.get("cli")
    if isinstance(cli_raw, str):
        cli = shlex.split(cli_raw)
    elif isinstance(cli_raw, list) and all(isinstance(item, str) for item in cli_raw):
        cli = list(cli_raw)
    else:
        raise SystemExit(f"role {role} must define cli as a string or string list")
    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict):
        raise SystemExit(f"role {role} env must be a JSON object")
    workdir_raw = raw.get("workdir")
    if workdir_raw is None:
        workdir = str(default_workdir or Path.cwd())
    else:
        workdir = str(_expand_path(str(workdir_raw), base=base))
    cli_name = _command_name(cli[0])
    if cli_name not in SUPPORTED_CONFIG_CLI_NAMES:
        raise SystemExit(
            f"role {role} cli {cli_name!r} is not supported; "
            f"supported clis: {', '.join(SUPPORTED_CONFIG_CLI_NAMES)}"
        )
    resume_mode = str(raw.get("resume_mode") or DEFAULT_RESUME_MODE_BY_CLI.get(cli_name, "flag")).strip()
    resume_flag = str(raw.get("resume_flag") or DEFAULT_RESUME_FLAG_BY_CLI.get(cli_name, "--resume")).strip()
    resume_subcommand = str(raw.get("resume_subcommand") or DEFAULT_RESUME_SUBCOMMAND_BY_CLI.get(cli_name, "resume")).strip()
    return RoleConfig(
        role=role,
        slot=slot,
        detached=_bool_value(raw.get("detached"), field="detached", role=role),
        tmux_session=tmux_session,
        target=str(raw.get("target") or f"{tmux_session}:0.0").strip(),
        workdir=workdir,
        cli=cli,
        model=str(raw.get("model") or "").strip(),
        model_arg=str(raw.get("model_arg") or DEFAULT_MODEL_ARG_BY_CLI.get(cli_name, "--model")).strip(),
        effort=str(raw.get("effort") or "").strip(),
        yolo=_bool_value(raw.get("yolo"), field="yolo", role=role),
        extra_args=_string_list(raw.get("extra_args"), field="extra_args", role=role),
        resume_mode=resume_mode,
        resume_flag=resume_flag,
        resume_subcommand=resume_subcommand,
        fresh_session_per_ticket=_bool_value(
            raw.get("fresh_session_per_ticket"),
            field="fresh_session_per_ticket",
            role=role,
        ),
        live_commands=_string_list(raw.get("live_commands"), field="live_commands", role=role),
        env={str(key): str(value) for key, value in env_raw.items()},
        run_as_user=str(raw.get("run_as_user") or "").strip(),
    )


def local_account_exists(account: str) -> bool:
    """Whether a Unix account exists on this host."""
    name = (account or "").strip()
    if not name:
        return False
    try:
        pwd.getpwnam(name)
    except KeyError:
        return False
    return True


def board_service_user(config: ProjectConfig) -> str:
    """The account the board service runs as, for operator command rendering."""
    from scripts.ticket_board.project_provision import DEFAULT_SERVICE_USER

    return DEFAULT_SERVICE_USER


def role_account_name(project: str, role: str) -> str:
    """The Unix account a role runs as. Defined once, in provisioning."""
    from scripts.ticket_board.project_provision import role_account_name as _name

    return _name(project, role)


def role_run_as_user(config: ProjectConfig, role: RoleConfig) -> str:
    """The account a role's session runs as.

    Per-role accounts are what give each role its own tmux server and make the
    board's uid-to-role mapping authoritative. A role without one falls back to
    the project account, which still works but cannot be told apart from the
    other roles on the board socket (SYRD-39).
    """
    return role.run_as_user or config.run_as_user


def role_process_runner_for(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    """A runner that acts as the account owning this role's tmux server.

    With one Unix account per role there is no shared tmux server, so probing,
    stopping, reloading or recovering a role has to address that role's own
    server. Using the project owner's runner for this reports roles stopped
    while their sessions are still alive (SYRD-39).
    """
    account = role_run_as_user(config, role)
    if account and current_user_name() != account:
        return _owner_process_runner(owner_user=account, runner=runner)
    return runner


def role_session_dir(config: ProjectConfig, role: RoleConfig) -> Path:
    """Where this role's CLI keeps its resumable session records.

    A role account cannot write the project owner's state directory, so a role
    with its own account gets its own path. Roles still sharing the project
    account keep the project-wide directory (SYRD-39).
    """
    account = role_run_as_user(config, role)
    if account and account != config.run_as_user:
        return account_session_dir(account, project=config.project)
    return config.session_dir


def role_pane_state_dir(
    config: ProjectConfig, role: RoleConfig, default: Path | None = None
) -> Path:
    """Where this role's pane hooks record busy/idle state.

    An isolated role writes the shared aggregation path, which it can write and
    the owner's listener can read. A role still running as the project account
    keeps whatever the caller chose, so an explicitly supplied directory is not
    silently replaced.
    """
    account = role_run_as_user(config, role)
    if account and account != config.run_as_user:
        return shared_pane_state_dir(config.project)
    if default is not None:
        return default
    return default_pane_state_dir_for_user(config.run_as_user, project=config.project)


def _role_board_env(config: ProjectConfig, role: RoleConfig, session_role_map: dict[str, str]) -> dict[str, str]:
    env = {
        "TICKET_BOARD_PROJECT": config.project,
        "TICKET_BOARD_PROJECT_NAME": config.project_name,
        "TICKET_BOARD_TICKET_PREFIX": config.ticket_prefix,
        "TICKET_BOARD_URL": config.board_url,
        "TICKET_BOARD_SOCKET": config.board_socket,
        "TICKET_BOARD_CALLER_ROLE": role.role,
        "TICKET_BOARD_CALLER_ROLE_MAP": json.dumps(session_role_map, sort_keys=True, separators=(",", ":")),
    }
    if config.run_as_user:
        # directorctl needs the owner's name to reach the display and viewer
        # sessions, which stay in the owner's tmux server (SYRD-39).
        env["SWITCHYARD_PROJECT_OWNER"] = config.run_as_user
    if config.upstream_report_url:
        env["TICKET_BOARD_REPORT_URL"] = config.upstream_report_url
        env["TICKET_BOARD_REPORT_ORIGIN_PROJECT"] = config.project
    if config.upstream_report_token_file:
        env["TICKET_BOARD_TENANT_REPORT_TOKEN_FILE"] = config.upstream_report_token_file
    return env


def _with_project_board_env(config: ProjectConfig, roles: list[RoleConfig]) -> list[RoleConfig]:
    session_role_map = {role.tmux_session: role.role for role in roles}
    return [
        replace(
            role,
            env={
                **role.env,
                **_role_board_env(config, role, session_role_map),
            },
        )
        for role in roles
    ]


def load_project_config(project: str, config_path: Path | None = None) -> ProjectConfig:
    project_slug = _validate_project_slug(project)
    path = config_path or DEFAULT_CONFIG_DIR / f"{project_slug}.json"
    config = _load_json(path)
    config_project = _validate_project_slug(str(config.get("project") or project_slug))
    if config_project != project_slug:
        raise SystemExit(f"config project {config_project!r} does not match requested project {project_slug!r}")
    project_name = str(config.get("project_name") or config.get("name") or config_project).strip() or config_project
    roles_raw = config.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise SystemExit(f"{path} must define a non-empty roles list")
    base = path.parent
    layout = _expand_path(str(config.get("layout") or f"{config_project}-konsole-layout.json"), base=base)
    run_as_user = str(config.get("run_as_user") or "").strip()
    session_dir = _expand_path(str(config.get("session_dir") or str(default_session_dir_for_user(run_as_user))), base=base)
    ticket_prefix = validate_ticket_prefix(str(config.get("ticket_prefix") or config_project))
    board_url = str(config.get("board_url") or _default_board_url(config_project)).strip()
    board_socket = str(config.get("board_socket") or _default_board_socket(config_project)).strip()
    upstream_report_url = str(config.get("upstream_report_url") or config.get("report_board_url") or "").strip()
    inline_report_token_keys = [
        key for key in ("upstream_report_token", "tenant_report_token") if str(config.get(key) or "").strip()
    ]
    if inline_report_token_keys:
        raise SystemExit(
            f"{path} stores an upstream report token value in {', '.join(inline_report_token_keys)}; "
            "use upstream_report_token_file instead"
        )
    upstream_report_token_file = str(
        config.get("upstream_report_token_file") or config.get("tenant_report_token_file") or ""
    ).strip()
    pane_launcher = None
    pane_launcher_raw = config.get("pane_launcher")
    if pane_launcher_raw is not None:
        pane_launcher = _expand_path(str(pane_launcher_raw), base=base)
    repository = None
    repository_raw = config.get("repository")
    if repository_raw is not None:
        repository = _expand_path(str(repository_raw), base=base)
    control_repository = None
    control_repository_raw = config.get("control_repository")
    if control_repository_raw is not None:
        control_repository = _expand_path(str(control_repository_raw), base=base)
    worktree_base = None
    worktree_base_raw = config.get("worktree_base")
    if worktree_base_raw is not None:
        worktree_base = _expand_path(str(worktree_base_raw), base=base)
    if repository is None and worktree_base is not None:
        raise SystemExit(f"{path} must not define worktree_base without repository")
    if control_repository is not None and repository is None:
        raise SystemExit(f"{path} must not define control_repository without repository")
    if control_repository is not None and worktree_base is None:
        raise SystemExit(f"{path} must define worktree_base when control_repository is set")
    worktree_remote = str(config.get("worktree_remote") or "origin").strip()
    worktree_branch = str(config.get("worktree_branch") or "main").strip()
    if not worktree_remote or not worktree_branch:
        raise SystemExit(f"{path} worktree_remote and worktree_branch must be non-empty")
    roles: list[RoleConfig] = []
    for raw in roles_raw:
        if not isinstance(raw, dict):
            continue
        default_workdir = repository
        if control_repository is not None and raw.get("workdir") is None and worktree_base is not None:
            role_name = str(raw.get("role") or "").strip()
            if role_name:
                default_workdir = worktree_base / role_name
        roles.append(_role_from_json(config_project, raw, base=base, default_workdir=default_workdir))
    if len(roles) != len(roles_raw):
        raise SystemExit(f"{path} roles must all be JSON objects")
    slots = [role.slot for role in roles if role.slot is not None]
    if len(set(slots)) != len(slots):
        raise SystemExit(f"{path} contains duplicate role slot assignments")
    detached_with_slots = [role.role for role in roles if role.detached and role.slot is not None]
    if detached_with_slots:
        raise SystemExit(f"{path} detached roles must not define layout slots: {', '.join(detached_with_slots)}")
    visible_without_slots = [role.role for role in roles if not role.detached and role.slot is None]
    if visible_without_slots:
        raise SystemExit(f"{path} visible roles must define layout slots: {', '.join(visible_without_slots)}")
    if repository is not None:
        repo_path = _normalized_path(repository)
    else:
        repo_path = ""
    seen_workdirs: dict[str, str] = {}
    duplicate_non_shared: list[str] = []
    for role in roles:
        normalized_workdir = _normalized_path(Path(role.workdir))
        previous_role = seen_workdirs.setdefault(normalized_workdir, role.role)
        if previous_role != role.role and normalized_workdir != repo_path:
            duplicate_non_shared.append(f"{previous_role}/{role.role}:{normalized_workdir}")
    if duplicate_non_shared:
        raise SystemExit(f"{path} contains duplicate non-shared role workdir assignments: {', '.join(duplicate_non_shared)}")
    parsed_config = ProjectConfig(
        project=config_project,
        project_name=project_name,
        ticket_prefix=ticket_prefix,
        layout=layout,
        session_dir=session_dir,
        board_url=board_url,
        board_socket=board_socket,
        upstream_report_url=upstream_report_url,
        upstream_report_token_file=upstream_report_token_file,
        run_as_user=run_as_user,
        pane_launcher=pane_launcher,
        repository=repository,
        control_repository=control_repository,
        worktree_base=worktree_base,
        worktree_remote=worktree_remote,
        worktree_branch=worktree_branch,
        roles=roles,
        desktop_access=config.get("desktop_access"),
    )
    boundary_error = _control_repository_boundary_error(parsed_config, require_existing_user=False)
    if boundary_error is not None:
        raise SystemExit(f"{path} control_repository {boundary_error}")
    return replace(parsed_config, roles=_with_project_board_env(parsed_config, roles))


def _normalized_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _quote_command(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _env_prefix(env: dict[str, str]) -> list[str]:
    return [
        f"{key}={value}"
        for key, value in sorted(env.items(), key=lambda item: (item[0] != "TICKET_BOARD_PANE_TARGET", item[0]))
    ]


PANE_TARGET_ENV_KEYS = (
    "PGU_PANE_SESSION_ID",
    "TICKET_BOARD_PANE_SESSION_ID",
    "PGU_TICKET_BOARD_PANE_SESSION_DIR",
    "PGU_TICKET_BOARD_PANE_STATE_DIR",
    "TICKET_BOARD_PANE_SESSION_DIR",
    "TICKET_BOARD_PANE_STATE_DIR",
    "TICKET_BOARD_PANE_TARGET",
    "PGU_PANE_TARGET",
    "TICKET_BOARD_CALLER_ROLE",
)
PROBE_IDENTITY_ENV_KEYS = ("TMUX", "TMUX_PANE", *PANE_TARGET_ENV_KEYS)


def _env_unset_prefix(keys: Sequence[str]) -> list[str]:
    result: list[str] = []
    for key in keys:
        result.extend(["-u", key])
    return result


def _prepend_path(path_value: str, directory: str) -> str:
    return _prepend_paths(path_value, [directory])


def _prepend_paths(path_value: str, directories: Sequence[str]) -> str:
    parts = [part for part in path_value.split(":") if part]
    for directory in reversed([item for item in directories if item]):
        parts = [part for part in parts if part != directory]
        parts.insert(0, directory)
    return ":".join(parts)


def session_file_name(target: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    return f"{safe}.json"


def pane_state_file_name(target: str) -> str:
    return session_file_name(target)


def seed_initial_pane_idle_state(
    role: RoleConfig,
    *,
    pane_state_dir: Path,
    source: str,
    now: float | None = None,
) -> Path:
    path = pane_state_dir / pane_state_file_name(role.target)
    payload = {
        "target": role.target,
        "state": "idle",
        "updated_at": time.time() if now is None else now,
        "source": source,
    }
    _write_json_atomic(path, payload)
    return path


def clear_pane_idle_state_for_role(role: RoleConfig, *, pane_state_dir: Path) -> None:
    path = pane_state_dir / pane_state_file_name(role.target)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"team-launcher: failed to clear pane state for {role.role}: {exc}", file=sys.stderr)


def session_id_for_role(role: RoleConfig, session_dir: Path) -> str:
    parsed = _session_record_for_role(role, session_dir)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def _ambient_pane_session_ids(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    result: dict[str, str] = {}
    for key in ("TICKET_BOARD_PANE_SESSION_ID", "PGU_PANE_SESSION_ID"):
        value = str(source.get(key) or "").strip()
        if value:
            result[key] = value
    return result


def _ambient_session_conflict_for_role(
    role: RoleConfig,
    *,
    session_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> str:
    recorded_session_id = session_id_for_role(role, session_dir)
    if not recorded_session_id:
        return ""
    conflicts = [
        f"{key}={value}"
        for key, value in _ambient_pane_session_ids(environ).items()
        if value != recorded_session_id
    ]
    if not conflicts:
        return ""
    return (
        f"team-launcher: refusing to launch {role.role}; ambient pane session id "
        f"{', '.join(conflicts)} does not match recorded session {recorded_session_id} "
        f"for target {role.target}. Strip pane identity env or run from outside a pane."
    )


def superseded_session_id_for_role(
    role: RoleConfig,
    session_dir: Path,
    *,
    changed_since_ns: int | None = None,
) -> str:
    if changed_since_ns is None:
        return ""
    path = session_dir / f"{session_file_name(role.target)}.superseded"
    try:
        stat = path.stat()
    except OSError:
        return ""
    if stat.st_ctime_ns + LAUNCH_RESUME_FALLBACK_FRESHNESS_SKEW_NS < changed_since_ns:
        return ""
    parsed = _session_record_at_path_for_role(role, path)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def unverified_resume_session_id_for_role(
    role: RoleConfig,
    session_dir: Path,
    *,
    changed_since_ns: int | None = None,
) -> str:
    if changed_since_ns is None:
        return ""
    path = session_dir / f"{session_file_name(role.target)}.resume_timeout"
    try:
        stat = path.stat()
    except OSError:
        return ""
    if stat.st_ctime_ns + LAUNCH_RESUME_FALLBACK_FRESHNESS_SKEW_NS < changed_since_ns:
        return ""
    parsed = _session_record_at_path_for_role(role, path)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def pane_launch_outcome_source_for_role(
    role: RoleConfig,
    pane_state_dir: Path | None,
    *,
    updated_since: float | None = None,
) -> str:
    if pane_state_dir is None or updated_since is None:
        return ""
    path = pane_state_dir / pane_state_file_name(role.target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if str(parsed.get("target") or "") != role.target:
        return ""
    source = str(parsed.get("source") or "").strip()
    if not source.startswith("team_launcher."):
        return ""
    try:
        updated_at = float(parsed.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return ""
    if updated_at + LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS < updated_since:
        return ""
    return source


def pane_runtime_hook_source_for_role(
    role: RoleConfig,
    pane_state_dir: Path | None,
    *,
    updated_since: float | None = None,
) -> str:
    if pane_state_dir is None or updated_since is None:
        return ""
    path = pane_state_dir / pane_state_file_name(role.target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if str(parsed.get("target") or "") != role.target:
        return ""
    source = str(parsed.get("source") or "").strip()
    expected_prefix = f"{_command_name(role.cli[0])}." if role.cli else ""
    if not expected_prefix or not source.startswith(expected_prefix):
        return ""
    try:
        updated_at = float(parsed.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return ""
    if updated_at + LAUNCH_PANE_STATE_FRESHNESS_SKEW_SECONDS < updated_since:
        return ""
    return source


def _expects_launch_runtime_hook_probe(role: RoleConfig) -> bool:
    cli_name = _command_name(role.cli[0]) if role.cli else ""
    return cli_name == "codex"


def launch_session_record_statuses(
    config: ProjectConfig,
    roles: Sequence[RoleConfig] | None = None,
    *,
    timeout_seconds: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    poll_seconds: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    fallback_changed_since_ns: int | None = None,
    fallback_grace_seconds: float = LAUNCH_RESUME_FALLBACK_GRACE_SECONDS,
    pane_state_dir: Path | None = None,
    pane_state_updated_since: float | None = None,
) -> list[LaunchSessionRecordStatus]:
    roles = tuple(roles or config.roles)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    pane_outcomes_required = pane_state_dir is not None and pane_state_updated_since is not None
    fallback_deadline = (
        time.monotonic() + min(max(0.0, fallback_grace_seconds), max(0.0, timeout_seconds))
        if fallback_changed_since_ns is not None and not pane_outcomes_required
        else time.monotonic()
    )
    statuses: list[LaunchSessionRecordStatus] = []
    while True:
        statuses = [
            LaunchSessionRecordStatus(
                role=role.role,
                target=role.target,
                # A role records its session under its own account, so reading
                # the project-wide directory reports every isolated role as
                # having no session (SYRD-39).
                session_id=session_id_for_role(role, role_session_dir(config, role)),
                superseded_session_id=superseded_session_id_for_role(
                    role,
                    role_session_dir(config, role),
                    changed_since_ns=fallback_changed_since_ns,
                ),
                unverified_resume_session_id=unverified_resume_session_id_for_role(
                    role,
                    role_session_dir(config, role),
                    changed_since_ns=fallback_changed_since_ns,
                ),
                pane_state_source=pane_launch_outcome_source_for_role(
                    role,
                    pane_state_dir,
                    updated_since=pane_state_updated_since,
                ),
                runtime_hook_source=pane_runtime_hook_source_for_role(
                    role,
                    pane_state_dir,
                    updated_since=pane_state_updated_since,
                ),
            )
            for role in roles
        ]
        now = time.monotonic()
        all_records_found = all(status.found for status in statuses)
        resume_fallback_found = any(status.resume_fallback for status in statuses)
        all_pane_outcomes_reported = not pane_outcomes_required or all(
            status.pane_launch_reported for status in statuses
        )
        all_runtime_hooks_reported = not pane_outcomes_required or all(
            (not _expects_launch_runtime_hook_probe(role)) or status.runtime_hook_reported or status.unverified_resume
            for role, status in zip(roles, statuses, strict=True)
        )
        fallback_grace_elapsed = now >= fallback_deadline
        if (
            now >= deadline
            or (resume_fallback_found and all_pane_outcomes_reported and all_runtime_hooks_reported)
            or (all_records_found and all_pane_outcomes_reported and all_runtime_hooks_reported)
            or (
                all_records_found
                and not pane_outcomes_required
                and fallback_changed_since_ns is not None
                and fallback_grace_elapsed
            )
        ):
            return statuses
        next_deadline = deadline
        if all_records_found and fallback_changed_since_ns is not None and not pane_outcomes_required:
            next_deadline = min(deadline, fallback_deadline)
        sleep_seconds = min(max(0.01, poll_seconds), max(0.0, next_deadline - now))
        if sleep_seconds <= 0:
            return statuses
        time.sleep(sleep_seconds)


def report_launch_session_records(
    config: ProjectConfig,
    roles: Sequence[RoleConfig] | None = None,
    *,
    timeout_seconds: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    poll_seconds: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    fallback_changed_since_ns: int | None = None,
    pane_state_dir: Path | None = None,
    pane_state_updated_since: float | None = None,
    attached_roles: Sequence[RoleConfig] | None = None,
    print_func: Callable[[str], None] = print,
) -> list[LaunchSessionRecordStatus]:
    selected_roles = tuple(roles or config.roles)
    attached_role_names = {role.role for role in attached_roles or ()}
    attached_by_role = {
        role.role: LaunchSessionRecordStatus(
            role=role.role,
            target=role.target,
            session_id=session_id_for_role(role, config.session_dir),
            attached_to_running=True,
        )
        for role in selected_roles
        if role.role in attached_role_names
    }
    probe_roles = tuple(role for role in selected_roles if role.role not in attached_role_names)
    for role in selected_roles:
        if role.role not in attached_by_role:
            continue
        print_func(f"switchyard: attached to running pane for {role.role} ({role.target})")
    probed_statuses: list[LaunchSessionRecordStatus] = []
    if probe_roles:
        print_func(
            f"switchyard: checking session records for {len(probe_roles)} pane(s) "
            f"(waiting up to {timeout_seconds:g}s)"
        )
        probed_statuses = launch_session_record_statuses(
            config,
            probe_roles,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            fallback_changed_since_ns=fallback_changed_since_ns,
            pane_state_dir=pane_state_dir,
            pane_state_updated_since=pane_state_updated_since,
        )
    statuses_by_role = {status.role: status for status in probed_statuses}
    statuses_by_role.update(attached_by_role)
    statuses = [statuses_by_role[role.role] for role in selected_roles if role.role in statuses_by_role]
    roles_by_target = {role.target: role for role in selected_roles}
    codex_runtime_hook_missing_statuses: list[LaunchSessionRecordStatus] = []
    for status in statuses:
        role = roles_by_target.get(status.target)
        codex_runtime_hook_deferred_until_activity = (
            role is not None
            and _expects_launch_runtime_hook_probe(role)
            and status.pane_state_source.startswith("team_launcher.")
            and not status.runtime_hook_reported
            and not status.unverified_resume
            and not status.resume_fallback
        )
        if status.attached_to_running:
            continue
        if status.found:
            print_func(
                f"switchyard: session record found for {status.role} ({status.target}): {status.session_id}"
            )
        elif not codex_runtime_hook_deferred_until_activity:
            print_func(
                f"warning: switchyard: session record missing for {status.role} "
                f"({status.target}) after {timeout_seconds:g}s; continuing"
            )
        if status.unverified_resume:
            print_func(
                f"warning: switchyard: {status.role} ({status.target}) resume for session "
                f"{status.unverified_resume_session_id} was not verified before startup timeout; "
                "left session record intact and did not mark pane idle"
            )
        if status.resume_fallback:
            print_func(
                f"warning: switchyard: {status.role} ({status.target}) fell back to a fresh session; "
                f"superseded session {status.superseded_session_id}"
            )
        if (
            role is not None
            and _expects_launch_runtime_hook_probe(role)
            and not status.runtime_hook_reported
            and not status.unverified_resume
            and not status.resume_fallback
        ):
            codex_runtime_hook_missing_statuses.append(status)
    if codex_runtime_hook_missing_statuses:
        missing = ", ".join(
            f"{status.role} ({status.target})" for status in codex_runtime_hook_missing_statuses
        )
        print_func(
            f"warning: switchyard: codex runtime hook did not report for "
            f"{len(codex_runtime_hook_missing_statuses)} pane(s): {missing}; expected fresh codex.* "
            "pane state. Interactive Codex may defer SessionStart until the first prompt; hook delivery "
            "is unproven until activity writes codex.UserPromptSubmit or codex.Stop."
        )
    return statuses


def clear_session_record_for_role(role: RoleConfig, session_dir: Path) -> bool:
    path = session_dir / session_file_name(role.target)
    superseded_path = path.with_name(f"{path.name}.superseded")
    previous_superseded_path = path.with_name(f"{path.name}.superseded.1")
    timeout_path = path.with_name(f"{path.name}.resume_timeout")
    if not path.exists():
        return False
    try:
        if superseded_path.exists():
            superseded_path.replace(previous_superseded_path)
        timeout_path.unlink(missing_ok=True)
        path.replace(superseded_path)
    except FileNotFoundError:
        return False
    return True


def clear_unverified_resume_for_role(role: RoleConfig, session_dir: Path) -> None:
    path = session_dir / f"{session_file_name(role.target)}.resume_timeout"
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"team-launcher: failed to clear unverified resume marker for {role.role}: {exc}", file=sys.stderr)


def record_unverified_resume_for_role(role: RoleConfig, session_dir: Path, session_id: str) -> None:
    _write_private_json_atomic(
        session_dir / f"{session_file_name(role.target)}.resume_timeout",
        {
            "reason": "resume_startup_timeout",
            "session_id": session_id,
            "target": role.target,
            "updated_at": time.time(),
        },
    )


def _session_record_at_path_for_role(role: RoleConfig, path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("target") or "") != role.target:
        return None
    return parsed


def _session_record_for_role(role: RoleConfig, session_dir: Path) -> dict[str, Any] | None:
    return _session_record_at_path_for_role(role, session_dir / session_file_name(role.target))


def _session_payload_model_for_role(role: RoleConfig, session_dir: Path) -> str:
    session = _session_record_for_role(role, session_dir)
    if session is None:
        return ""
    payload = session.get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model") or "").strip()


def _session_dir_has_records(session_dir: Path) -> bool:
    try:
        return any(path.is_file() and path.suffix == ".json" for path in session_dir.iterdir())
    except OSError:
        return False


def _valid_session_record_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    target = str(payload.get("target") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not target or not session_id:
        return None
    return dict(payload)


def _default_session_seed_dirs(session_dir: Path) -> list[Path]:
    candidates = [DEFAULT_LEGACY_RUNTIME_SESSION_DIR, DEFAULT_INTERIM_SESSION_BACKUP_DIR]
    session_resolved = session_dir.expanduser().resolve(strict=False)
    unique: list[Path] = []
    seen: set[str] = {str(session_resolved)}
    for candidate in candidates:
        resolved = str(candidate.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def seed_session_dir_from_legacy_sources(
    session_dir: Path,
    *,
    candidates: Sequence[Path] | None = None,
) -> list[Path]:
    target_dir = session_dir.expanduser()
    if _session_dir_has_records(target_dir):
        _ensure_private_dir(target_dir)
        return []

    copied: list[Path] = []
    for source_dir in candidates if candidates is not None else _default_session_seed_dirs(target_dir):
        source = source_dir.expanduser()
        try:
            source_records = sorted(path for path in source.iterdir() if path.is_file() and path.suffix == ".json")
        except OSError:
            continue
        for source_path in source_records:
            try:
                payload = json.loads(source_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            valid = _valid_session_record_payload(payload)
            if valid is None:
                continue
            target_path = target_dir / session_file_name(str(valid["target"]))
            if target_path.exists():
                continue
            _write_private_json_atomic(target_path, valid)
            copied.append(target_path)
    if not copied:
        _ensure_private_dir(target_dir)
    return copied


def seed_default_session_dir_from_legacy_sources(session_dir: Path) -> list[Path]:
    if session_dir.expanduser().resolve(strict=False) != DEFAULT_SESSION_DIR.expanduser().resolve(strict=False):
        return []
    return seed_session_dir_from_legacy_sources(session_dir)


def _command_name(value: str) -> str:
    return Path(value.strip()).name


def yolo_args_for_role(role: RoleConfig) -> list[str]:
    if not role.yolo:
        return []
    cli_name = _command_name(role.cli[0])
    flags = YOLO_ARGS_BY_CLI.get(cli_name)
    if flags is None:
        raise SystemExit(f"role {role.role} uses unsupported yolo cli {cli_name!r}")
    return [flag for flag in flags if flag not in role.extra_args]


def startup_args_for_role(role: RoleConfig) -> list[str]:
    cli_name = _command_name(role.cli[0])
    flags = STARTUP_ARGS_BY_CLI.get(cli_name, [])
    return [flag for flag in flags if flag not in role.extra_args]


def effort_args_for_role(role: RoleConfig) -> list[str]:
    if not role.effort:
        return []
    cli_name = _command_name(role.cli[0])
    style = EFFORT_STYLE_BY_CLI.get(cli_name)
    if style == "flag":
        return ["--effort", role.effort]
    if style == "config":
        return ["-c", f"reasoning_effort={role.effort}"]
    if style == "reasoning":
        return ["--reasoning", role.effort]
    if style is None and cli_name in EFFORT_STYLE_BY_CLI:
        return []
    raise SystemExit(f"role {role.role} uses unsupported effort cli {cli_name!r}")


def _resume_args_for_role(role: RoleConfig, session_id: str) -> list[str]:
    if not session_id:
        return []
    if role.resume_mode == "flag":
        return [role.resume_flag, session_id]
    if role.resume_mode == "subcommand":
        return [role.resume_subcommand, session_id]
    raise SystemExit(f"role {role.role} uses unsupported resume_mode {role.resume_mode!r}")


def _uses_agy_conversation_resume(role: RoleConfig) -> bool:
    cli_name = _command_name(role.cli[0]) if role.cli else ""
    return cli_name == "agy" and role.resume_mode == "flag" and role.resume_flag == "--conversation"


def _uses_claude_resume(role: RoleConfig) -> bool:
    cli_name = _command_name(role.cli[0]) if role.cli else ""
    return cli_name == "claude" and role.resume_mode == "flag" and role.resume_flag == "--resume"


def _uses_codex_resume(role: RoleConfig) -> bool:
    cli_name = _command_name(role.cli[0]) if role.cli else ""
    return cli_name == "codex" and role.resume_mode == "subcommand" and role.resume_subcommand == "resume"


def _uses_fresh_session_per_ticket(role: RoleConfig) -> bool:
    return role.fresh_session_per_ticket


def _uses_hermes(role: RoleConfig) -> bool:
    cli_name = _command_name(role.cli[0]) if role.cli else ""
    return cli_name == "hermes"


def agy_conversation_store_exists(session_id: str, *, root: Path | None = None) -> bool:
    if not session_id:
        return False
    root = root or AGY_CONVERSATION_ROOT
    return (root / "conversations" / f"{session_id}.db").is_file() or (root / "brain" / session_id).is_dir()


def _home_from_session_dir(session_dir: Path) -> Path:
    expanded = session_dir.expanduser()
    parts = expanded.parts
    for index in range(len(parts) - 1):
        if parts[index] == ".local" and parts[index + 1] == "state":
            return Path(*parts[:index])
    return Path.home()


def hermes_shared_home_for_session_dir(session_dir: Path) -> Path:
    return _home_from_session_dir(session_dir) / HERMES_HOME_DIR_NAME


def hermes_home_for_role(role: RoleConfig, *, session_dir: Path) -> Path:
    role_home_name = session_file_name(role.target).removesuffix(".json")
    return session_dir.expanduser().parent / "hermes-homes" / role_home_name


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _symlink_shared_hermes_entry(source: Path, destination: Path) -> None:
    if destination.is_symlink():
        try:
            if _same_path(destination.resolve(strict=False), source):
                return
        except OSError:
            pass
        destination.unlink()
    elif destination.exists():
        return
    destination.symlink_to(source)


def prepare_hermes_home_for_role(role: RoleConfig, *, session_dir: Path) -> Path | None:
    if not _uses_hermes(role):
        return None
    hermes_home = hermes_home_for_role(role, session_dir=session_dir)
    _ensure_private_dir(hermes_home)
    shared_home = hermes_shared_home_for_session_dir(session_dir)
    for entry in HERMES_SHARED_HOME_ENTRIES:
        if entry in HERMES_PRIVATE_HOME_ENTRIES:
            continue
        source = shared_home / entry
        # Locks must live with the resource they guard. Hermes derives both
        # of these lock paths from shared JSON files, so role-local locks would
        # leave several panes writing the same JSON without mutual exclusion.
        if not source.exists() and entry in HERMES_SHARED_LOCK_GUARDS:
            guarded = shared_home / HERMES_SHARED_LOCK_GUARDS[entry]
            if guarded.exists():
                shared_home.mkdir(parents=True, exist_ok=True)
                source.touch(mode=0o600, exist_ok=True)
        if not source.exists():
            continue
        _symlink_shared_hermes_entry(source, hermes_home / entry)
    return hermes_home


def hermes_env_for_role(role: RoleConfig, *, session_dir: Path) -> dict[str, str]:
    if not _uses_hermes(role):
        return {}
    return {"HERMES_HOME": str(hermes_home_for_role(role, session_dir=session_dir))}


def _session_record_transcript_path(record: dict[str, Any] | None) -> Path | None:
    if record is None:
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    raw_path = str(payload.get("transcript_path") or "").strip()
    return Path(raw_path).expanduser() if raw_path else None


def _claude_project_dir_for_workdir(workdir: str, *, home: Path) -> Path:
    absolute = str(Path(workdir).expanduser().resolve(strict=False))
    project_key = "-" + absolute.strip("/").replace("/", "-")
    return home / CLAUDE_PROJECTS_DIR_NAME / project_key


def claude_session_store_exists(
    role: RoleConfig,
    session_id: str,
    *,
    session_dir: Path,
    record: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    transcript_path = _session_record_transcript_path(record)
    if transcript_path is not None:
        return transcript_path.is_file(), str(transcript_path)
    store_path = _claude_project_dir_for_workdir(role.workdir, home=_home_from_session_dir(session_dir)) / f"{session_id}.jsonl"
    return store_path.is_file(), str(store_path)


def codex_session_store_exists(
    session_id: str,
    *,
    session_dir: Path,
    record: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    transcript_path = _session_record_transcript_path(record)
    if transcript_path is not None:
        return transcript_path.is_file(), str(transcript_path)
    sessions_root = _home_from_session_dir(session_dir) / CODEX_SESSIONS_DIR_NAME
    if not session_id:
        return False, str(sessions_root)
    try:
        for path in sessions_root.rglob(f"*-{session_id}.jsonl"):
            if path.is_file():
                return True, str(path)
    except OSError:
        pass
    return False, f"{sessions_root}/**/*-{session_id}.jsonl"


def _resume_preflight_allows_attempt(role: RoleConfig, session_id: str, *, session_dir: Path) -> tuple[bool, str]:
    record = _session_record_for_role(role, session_dir)
    if _uses_claude_resume(role):
        found, location = claude_session_store_exists(role, session_id, session_dir=session_dir, record=record)
        if found:
            return True, ""
        return (
            False,
            (
                f"team-launcher: recorded claude session {session_id} for {role.role} "
                f"is not present at {location}; starting fresh instead of passing claude --resume, "
                "which exits when the conversation is missing"
            ),
        )
    if _uses_codex_resume(role):
        found, location = codex_session_store_exists(session_id, session_dir=session_dir, record=record)
        if found:
            return True, ""
        return (
            False,
            (
                f"team-launcher: recorded codex session {session_id} for {role.role} "
                f"is not present at {location}; starting fresh instead of passing codex resume"
            ),
        )
    if not _uses_agy_conversation_resume(role):
        return True, ""
    if agy_conversation_store_exists(session_id):
        return True, ""
    return (
        False,
        (
            f"team-launcher: recorded agy conversation {session_id} for {role.role} "
            "is not present in the local Antigravity store; starting fresh instead of relying "
            "on agy --conversation, which silently falls back when the id is missing"
        ),
    )


def cli_command_for_role(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path | None = None,
    resume: bool = False,
    bin_user: str = "",
) -> list[str]:
    session_id = session_id_for_role(role, session_dir) if resume else ""
    if session_id and _uses_fresh_session_per_ticket(role):
        session_id = ""
    command = [*role.cli, *_resume_args_for_role(role, session_id)]
    if role.model:
        command.extend([role.model_arg, role.model])
    command.extend(effort_args_for_role(role))
    command.extend(yolo_args_for_role(role))
    command.extend(startup_args_for_role(role))
    command.extend(role.extra_args)
    env = {
        **role.env,
        "TICKET_BOARD_PANE_TARGET": role.target,
        "TICKET_BOARD_PANE_SESSION_DIR": str(session_dir.expanduser()),
    }
    env.update(hermes_env_for_role(role, session_dir=session_dir))
    if pane_state_dir is not None:
        env["TICKET_BOARD_PANE_STATE_DIR"] = str(pane_state_dir.expanduser())
    if session_id:
        env["TICKET_BOARD_PANE_SESSION_ID"] = session_id
    if role.target.startswith("pgu-"):
        env.setdefault("PGU_PANE_TARGET", role.target)
        env.setdefault("PGU_TICKET_BOARD_PANE_SESSION_DIR", str(session_dir.expanduser()))
        if pane_state_dir is not None:
            env.setdefault("PGU_TICKET_BOARD_PANE_STATE_DIR", str(pane_state_dir.expanduser()))
        if session_id:
            env.setdefault("PGU_PANE_SESSION_ID", session_id)
    pane_path_dirs = default_user_bin_dirs(bin_user)
    configured_directorctl = env.get("TICKET_BOARD_DIRECTORCTL", "").strip()
    if configured_directorctl:
        directorctl_path = Path(configured_directorctl).expanduser()
        if not directorctl_path.is_absolute():
            raise SystemExit(
                f"team-launcher: TICKET_BOARD_DIRECTORCTL must be absolute for {role.role}: "
                f"{configured_directorctl}"
            )
        pane_path_dirs.insert(0, str(directorctl_path.parent))
    env["PATH"] = _prepend_paths(env.get("PATH") or default_pane_base_path(bin_user), pane_path_dirs)
    return ["env", *_env_unset_prefix((*PANE_TARGET_ENV_KEYS, *role.unset_env)), *_env_prefix(env), *command]


def tmux_new_session_args(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path | None = None,
    resume: bool = False,
    bin_user: str = "",
) -> list[str]:
    shell_command = _quote_command(
        cli_command_for_role(role, session_dir=session_dir, pane_state_dir=pane_state_dir, resume=resume, bin_user=bin_user)
    )
    return ["tmux", "new-session", "-d", "-s", role.tmux_session, "-c", role.workdir, "-n", role.role, shell_command]


def tmux_attach_args(role: RoleConfig) -> list[str]:
    return ["tmux", "attach", "-t", role.tmux_session]


def tmux_has_session_args(role: RoleConfig) -> list[str]:
    return ["tmux", "has-session", "-t", role.tmux_session]


def tmux_kill_session_args(role: RoleConfig) -> list[str]:
    return ["tmux", "kill-session", "-t", role.tmux_session]


def tmux_has_session_by_name_args(session: str) -> list[str]:
    return ["tmux", "has-session", "-t", session]


def tmux_kill_session_by_name_args(session: str) -> list[str]:
    return ["tmux", "kill-session", "-t", session]


def tmux_detach_clients_args(role: RoleConfig) -> list[str]:
    return ["tmux", "detach-client", "-s", role.tmux_session]


def tmux_current_command_args(role: RoleConfig) -> list[str]:
    return ["tmux", "display-message", "-p", "-t", role.target, "#{pane_current_command}"]


def tmux_pane_pid_args(role: RoleConfig) -> list[str]:
    return ["tmux", "display-message", "-p", "-t", role.target, "#{pane_pid}"]


def pane_pid_for_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    proc = runner(tmux_pane_pid_args(role), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return 0
    try:
        return int(str(proc.stdout).strip())
    except ValueError:
        return 0


def worktree_ref(config: ProjectConfig) -> str:
    return f"{config.worktree_remote}/{config.worktree_branch}"


def control_repository_refspec(config: ProjectConfig) -> str:
    return f"+refs/heads/*:refs/remotes/{config.worktree_remote}/*"


def mkdir_p_args(path: Path) -> list[str]:
    return ["mkdir", "-p", str(path)]


def git_fetch_worktree_ref_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "fetch", config.worktree_remote, config.worktree_branch]


def git_clone_control_repository_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return ["git", "clone", "--bare", str(config.repository), str(config.control_repository)]


def git_control_remote_rename_args(config: ProjectConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return ["git", "-C", str(config.control_repository), "remote", "rename", "origin", config.worktree_remote]


def git_control_fetch_refspec_args(config: ProjectConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return [
        "git",
        "-C",
        str(config.control_repository),
        "config",
        "--replace-all",
        f"remote.{config.worktree_remote}.fetch",
        control_repository_refspec(config),
    ]


def git_fetch_control_ref_args(config: ProjectConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return ["git", "-C", str(config.control_repository), "fetch", config.worktree_remote, config.worktree_branch]


def git_control_worktree_add_args(config: ProjectConfig, role: RoleConfig) -> list[str]:
    if config.control_repository is None:
        raise SystemExit("project config does not define control_repository")
    return [
        "git",
        "--git-dir",
        str(config.control_repository),
        "worktree",
        "add",
        "--detach",
        role.workdir,
        worktree_ref(config),
    ]


def git_fetch_launcher_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "fetch", config.worktree_remote, config.worktree_branch]


def git_shared_checkout_check_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "rev-parse", "--is-inside-work-tree"]


def git_launcher_checkout_check_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "--is-inside-work-tree"]


def git_launcher_head_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "HEAD"]


def git_launcher_ls_remote_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "ls-remote", "--exit-code", config.worktree_remote, config.worktree_branch]


def git_launcher_commit_exists_args(launcher_repo: Path, commit: str) -> list[str]:
    return ["git", "-C", str(launcher_repo), "cat-file", "-e", f"{commit}^{{commit}}"]


def git_launcher_ahead_behind_args(
    config: ProjectConfig,
    launcher_repo: Path,
    compare_ref: str | None = None,
) -> list[str]:
    return [
        "git",
        "-C",
        str(launcher_repo),
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{compare_ref or worktree_ref(config)}",
    ]


def git_checkout_shared_ref_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "checkout", "--detach", "--force", worktree_ref(config)]


def git_checkout_launcher_branch_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "checkout", "--force", config.worktree_branch]


def git_fast_forward_launcher_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "merge", "--ff-only", worktree_ref(config)]


def git_launcher_current_branch_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "symbolic-ref", "--quiet", "--short", "HEAD"]


def git_launcher_status_porcelain_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "status", "--porcelain", "--untracked-files=no"]


def git_shared_checkout_status_porcelain_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "status", "--porcelain"]


def git_role_worktree_check_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "rev-parse", "--is-inside-work-tree"]


def git_role_worktree_status_porcelain_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "status", "--porcelain"]


def git_role_worktree_reset_args(config: ProjectConfig, role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "reset", "--hard", worktree_ref(config)]


def git_clean_role_worktree_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "clean", "-fdx"]


def git_clean_role_worktree_dry_run_args(role: RoleConfig) -> list[str]:
    return ["git", "-C", role.workdir, "clean", "-fd", "--dry-run"]


def git_launcher_head_short_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "--short", "HEAD"]


def git_clean_shared_checkout_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "clean", "-fdx"]


def git_clean_shared_checkout_dry_run_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "clean", "-fd", "--dry-run"]


def git_clean_launcher_checkout_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "clean", "-fdx"]


def _proc_failure_reason(proc: subprocess.CompletedProcess[Any], fallback: str) -> str:
    stderr = getattr(proc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return fallback


def _path_owner_user(path: Path) -> tuple[str, str]:
    try:
        info = path.stat()
    except OSError as exc:
        return "", str(exc)
    try:
        return pwd.getpwuid(info.st_uid).pw_name, ""
    except KeyError:
        return "", f"uid {info.st_uid} has no passwd entry"


@dataclass(frozen=True)
class GitOwnerRule:
    root: Path
    owner_user: str


def _normalized_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _path_is_under(path: Path, root: Path) -> bool:
    normalized = _normalized_path(path)
    normalized_root = _normalized_path(root)
    return normalized == normalized_root or normalized.is_relative_to(normalized_root)


def _git_target_path_from_args(args: Sequence[str]) -> Path | None:
    if not args or args[0] != "git":
        return None
    for index, arg in enumerate(args):
        if arg == "-C" and index + 1 < len(args):
            return Path(args[index + 1])
        if arg == "--git-dir" and index + 1 < len(args):
            return Path(args[index + 1])
        if arg.startswith("--git-dir="):
            return Path(arg.split("=", 1)[1])
    if len(args) >= 5 and args[1:3] == ["clone", "--bare"]:
        return Path(args[4])
    return None


def _git_owner_for_target(target: Path, owner_rules: Sequence[GitOwnerRule]) -> tuple[str, str]:
    for rule in owner_rules:
        if rule.owner_user and _path_is_under(target, rule.root):
            return rule.owner_user, ""
    if target.exists():
        return _path_owner_user(target)
    return "", f"{target} does not exist and no owner rule matched"


def _git_owner_failure(args: Sequence[str], detail: str) -> subprocess.CompletedProcess[Any]:
    return subprocess.CompletedProcess(list(args), 125, stderr=f"owner-correct git skipped: {detail}")


def run_owner_correct_git(
    args: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    owner_rules: Sequence[GitOwnerRule] = (),
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    target = _git_target_path_from_args(args)
    if target is None:
        return _git_owner_failure(args, "git command does not declare a target path")
    owner_user, error = _git_owner_for_target(target, owner_rules)
    if not owner_user:
        return _git_owner_failure(args, error or f"cannot determine owner for {target}")
    if owner_user == current_user_name():
        return runner(args, **kwargs)
    return runner(["sudo", "-u", owner_user, *args], **kwargs)


def _owner_correct_git_runner(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    owner_rules: Sequence[GitOwnerRule] = (),
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if args[:1] == ["git"]:
            return run_owner_correct_git(args, runner=runner, owner_rules=owner_rules, **kwargs)
        return runner(args, **kwargs)

    return wrapped


def _config_git_owner_rules(config: ProjectConfig) -> list[GitOwnerRule]:
    owner_user = config.run_as_user or current_user_name()
    if not owner_user:
        return []
    roots: list[Path] = []
    if config.repository is not None:
        roots.append(config.repository)
    roots.extend(_control_repository_owned_roots(config))
    return [GitOwnerRule(root, owner_user) for root in roots]


def _launcher_checkout_runner(
    repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[Callable[..., subprocess.CompletedProcess[Any]] | None, str]:
    owner_user, error = _path_owner_user(repo)
    if not owner_user:
        return None, error or "owner is unknown"
    return _owner_correct_git_runner(runner=runner, owner_rules=[GitOwnerRule(repo, owner_user)]), ""


def _parse_ahead_behind(output: str) -> tuple[int, int] | None:
    parts = output.strip().split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _parse_ls_remote_head(output: str) -> str | None:
    for line in output.splitlines():
        parts = line.strip().split()
        if parts:
            return parts[0]
    return None


def _format_behind_count(count: int, *, exact: bool = True) -> str:
    if exact:
        return f"{count} commit(s)"
    return f"at least {count} commit(s)"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_truthy_any(*names: str) -> bool:
    return any(_env_truthy(name) for name in names)


def probe_launcher_checkout(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherCheckoutProbe:
    repo = launcher_repo or _repo_root()
    check_proc = run_owner_correct_git(
        git_launcher_checkout_check_args(repo),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        return LauncherCheckoutProbe(error=f"launcher path is not a git checkout: {repo}")
    head_proc = run_owner_correct_git(
        git_launcher_head_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if head_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to read launcher checkout HEAD in {repo}: "
                f"{_proc_failure_reason(head_proc, f'exit {head_proc.returncode}')}"
            )
        )
    local_head = str(head_proc.stdout or "").strip()
    remote_proc = run_owner_correct_git(
        git_launcher_ls_remote_ref_args(config, repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if remote_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to inspect launcher deploy ref {worktree_ref(config)} without fetching in {repo}: "
                f"{_proc_failure_reason(remote_proc, f'exit {remote_proc.returncode}')}"
            )
        )
    remote_head = _parse_ls_remote_head(str(remote_proc.stdout or ""))
    if not remote_head:
        return LauncherCheckoutProbe(
            error=f"could not parse launcher deploy ref {worktree_ref(config)} from ls-remote output"
        )
    if local_head == remote_head:
        return LauncherCheckoutProbe(ahead=0, behind=0)
    exists_proc = run_owner_correct_git(
        git_launcher_commit_exists_args(repo, remote_head),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists_proc.returncode != 0:
        # The remote tip is not in the local object database. Avoid fetching in
        # the hot-path status probe; the deploy path will fetch before merging.
        return LauncherCheckoutProbe(ahead=0, behind=1, behind_exact=False)
    count_proc = run_owner_correct_git(
        git_launcher_ahead_behind_args(config, repo, remote_head),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if count_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to compare launcher checkout {repo} with {worktree_ref(config)}: "
                f"{_proc_failure_reason(count_proc, f'exit {count_proc.returncode}')}"
            )
        )
    counts = _parse_ahead_behind(str(count_proc.stdout or ""))
    if counts is None:
        return LauncherCheckoutProbe(
            error=(
                f"could not parse launcher ahead/behind count for {repo}: "
                f"{str(count_proc.stdout or '').strip()!r}"
            )
        )
    ahead, behind = counts
    return LauncherCheckoutProbe(ahead=ahead, behind=behind)


def probe_checkout_against_worktree_ref(
    config: ProjectConfig,
    repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherCheckoutProbe:
    check_proc = run_owner_correct_git(
        git_launcher_checkout_check_args(repo),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        return LauncherCheckoutProbe(error=f"path is not a git checkout: {repo}")
    head_proc = run_owner_correct_git(
        git_launcher_head_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if head_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to read HEAD in {repo}: "
                f"{_proc_failure_reason(head_proc, f'exit {head_proc.returncode}')}"
            )
        )
    local_head = str(head_proc.stdout or "").strip()
    remote_proc = run_owner_correct_git(
        git_launcher_ls_remote_ref_args(config, repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if remote_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to inspect {worktree_ref(config)} without fetching in {repo}: "
                f"{_proc_failure_reason(remote_proc, f'exit {remote_proc.returncode}')}"
            )
        )
    remote_head = _parse_ls_remote_head(str(remote_proc.stdout or ""))
    if not remote_head:
        return LauncherCheckoutProbe(error=f"could not parse {worktree_ref(config)} from ls-remote output")
    if local_head == remote_head:
        return LauncherCheckoutProbe(ahead=0, behind=0)
    exists_proc = run_owner_correct_git(
        git_launcher_commit_exists_args(repo, remote_head),
        runner=runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if exists_proc.returncode != 0:
        return LauncherCheckoutProbe(ahead=0, behind=1, behind_exact=False)
    count_proc = run_owner_correct_git(
        git_launcher_ahead_behind_args(config, repo, remote_head),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if count_proc.returncode != 0:
        return LauncherCheckoutProbe(
            error=(
                f"failed to compare {repo} with {worktree_ref(config)}: "
                f"{_proc_failure_reason(count_proc, f'exit {count_proc.returncode}')}"
            )
        )
    counts = _parse_ahead_behind(str(count_proc.stdout or ""))
    if counts is None:
        return LauncherCheckoutProbe(error=f"could not parse ahead/behind count for {repo}")
    ahead, behind = counts
    return LauncherCheckoutProbe(ahead=ahead, behind=behind)


def launcher_checkout_status(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, int]:
    repo = launcher_repo or _repo_root()
    probe = probe_launcher_checkout(config, launcher_repo=repo, runner=runner)
    if probe.error:
        raise SystemExit(
            f"team-launcher: cannot determine launcher checkout freshness for {repo}: {probe.error}"
        )
    return probe.ahead, probe.behind


def _short_head(repo: Path, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    proc = run_owner_correct_git(
        git_launcher_head_short_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return "unknown"
    return str(proc.stdout or "").strip() or "unknown"


def _auto_fast_forward_launcher_checkout(
    config: ProjectConfig,
    repo: Path,
    *,
    behind: int,
    behind_exact: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> LauncherCheckoutProbe:
    branch_proc = run_owner_correct_git(
        git_launcher_current_branch_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    branch = str(branch_proc.stdout or "").strip()
    if branch_proc.returncode != 0 or branch != config.worktree_branch:
        reason = branch or "detached HEAD"
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"it is {_format_behind_count(behind, exact=behind_exact)} behind {worktree_ref(config)}, "
            "and automatic fast-forward "
            f"requires branch {config.worktree_branch!r} but found {reason!r}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    status_proc = run_owner_correct_git(
        git_launcher_status_porcelain_args(repo),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"could not inspect local changes before automatic fast-forward: "
            f"{_proc_failure_reason(status_proc, f'exit {status_proc.returncode}')}"
        )
    if str(status_proc.stdout or "").strip():
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            "local changes are present, so automatic fast-forward is not safe. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    before = _short_head(repo, runner=runner)
    fetch_proc = run_owner_correct_git(git_fetch_launcher_ref_args(config, repo), runner=runner)
    if fetch_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"automatic fetch of {worktree_ref(config)} failed: "
            f"{_proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    merge_proc = run_owner_correct_git(git_fast_forward_launcher_ref_args(config, repo), runner=runner)
    if merge_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"automatic fast-forward to {worktree_ref(config)} failed: "
            f"{_proc_failure_reason(merge_proc, f'exit {merge_proc.returncode}')}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, or rerun with --allow-stale-launcher in an emergency."
        )
    after = _short_head(repo, runner=runner)
    probe = probe_launcher_checkout(config, launcher_repo=repo, runner=runner)
    if probe.error:
        print(
            f"warning: team-launcher: auto-fast-forwarded launcher checkout {repo} "
            f"from {before} to {after}, but freshness is now undeterminable: {probe.error}; continuing",
            file=sys.stderr,
        )
        return probe
    if probe.behind:
        raise SystemExit(
            f"team-launcher: launcher checkout {repo} is still {probe.behind} commit(s) "
            f"behind {worktree_ref(config)} after automatic fast-forward"
        )
    print(
        f"team-launcher: auto-fast-forwarded launcher checkout {repo} from {before} to {after}; "
        f"was {_format_behind_count(behind, exact=behind_exact)} behind {worktree_ref(config)}",
        file=sys.stderr,
    )
    return probe


def ensure_launcher_checkout_current(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    auto_deploy: bool = False,
    allow_stale: bool = False,
) -> None:
    repo = launcher_repo or _repo_root()
    release = shared_switchyard_release_for_path(repo)
    if release is not None:
        return
    allow_stale = allow_stale or _env_truthy_any(ALLOW_STALE_LAUNCHER_ENV, LEGACY_ALLOW_STALE_LAUNCHER_ENV)
    checkout_runner, owner_error = _launcher_checkout_runner(repo, runner=runner)
    if checkout_runner is None:
        print(
            f"warning: team-launcher: cannot determine launcher checkout owner for {repo}: "
            f"{owner_error}; skipping freshness probe",
            file=sys.stderr,
        )
        return
    probe = probe_launcher_checkout(config, launcher_repo=repo, runner=checkout_runner)
    if probe.error:
        print(
            f"warning: team-launcher: cannot determine launcher checkout freshness for {repo}: "
            f"{probe.error}; continuing",
            file=sys.stderr,
        )
        return
    if probe.behind:
        if allow_stale:
            print(
                f"warning: team-launcher: OVERRIDE proceeding with stale launcher checkout {repo}; "
                f"it is {_format_behind_count(probe.behind, exact=probe.behind_exact)} "
                f"behind {worktree_ref(config)}",
                file=sys.stderr,
            )
            return
        if auto_deploy:
            post_deploy_probe = _auto_fast_forward_launcher_checkout(
                config,
                repo,
                behind=probe.behind,
                behind_exact=probe.behind_exact,
                runner=checkout_runner,
            )
            if post_deploy_probe.error or post_deploy_probe.behind:
                return
            probe = post_deploy_probe
        else:
            raise SystemExit(
                f"team-launcher: refusing to launch from stale checkout {repo}; "
                f"it is {_format_behind_count(probe.behind, exact=probe.behind_exact)} "
                f"behind {worktree_ref(config)}. "
                f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
                "during an approved restart window, rerun with --allow-stale-launcher in an emergency, "
                "or unset --no-launcher-self-deploy for automatic fast-forward."
            )
    if probe.ahead:
        print(
            f"warning: launcher checkout {repo} is {probe.ahead} commit(s) ahead of {worktree_ref(config)}",
            file=sys.stderr,
        )


def deploy_launcher_checkout(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    clean: bool = False,
) -> int:
    repo = launcher_repo or _repo_root()
    checkout_runner, owner_error = _launcher_checkout_runner(repo, runner=runner)
    if checkout_runner is None:
        print(
            f"team-launcher: cannot determine launcher checkout owner for {repo}: "
            f"{owner_error}; not deploying launcher checkout",
            file=sys.stderr,
        )
        return 1
    check_proc = run_owner_correct_git(
        git_launcher_checkout_check_args(repo),
        runner=checkout_runner,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        print(f"team-launcher: launcher path is not a git checkout: {repo}", file=sys.stderr)
        return int(check_proc.returncode)
    fetch_proc = run_owner_correct_git(git_fetch_launcher_ref_args(config, repo), runner=checkout_runner)
    if fetch_proc.returncode != 0:
        print(
            f"team-launcher: failed to fetch {worktree_ref(config)} in {repo}: "
            f"{_proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(fetch_proc.returncode)
    checkout_proc = run_owner_correct_git(git_checkout_launcher_branch_args(config, repo), runner=checkout_runner)
    if checkout_proc.returncode != 0:
        print(
            f"team-launcher: failed to checkout launcher branch {config.worktree_branch} in {repo}: "
            f"{_proc_failure_reason(checkout_proc, f'exit {checkout_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(checkout_proc.returncode)
    merge_proc = run_owner_correct_git(git_fast_forward_launcher_ref_args(config, repo), runner=checkout_runner)
    if merge_proc.returncode != 0:
        print(
            f"team-launcher: failed to fast-forward launcher checkout {repo} to {worktree_ref(config)}: "
            f"{_proc_failure_reason(merge_proc, f'exit {merge_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(merge_proc.returncode)
    if clean:
        clean_proc = run_owner_correct_git(git_clean_launcher_checkout_args(repo), runner=checkout_runner)
        if clean_proc.returncode != 0:
            print(
                f"team-launcher: failed to clean launcher checkout {repo}: "
                f"{_proc_failure_reason(clean_proc, f'exit {clean_proc.returncode}')}",
                file=sys.stderr,
            )
            return int(clean_proc.returncode)
    ahead, behind = launcher_checkout_status(config, launcher_repo=repo, runner=checkout_runner)
    if behind:
        print(
            f"team-launcher: launcher checkout {repo} is still {behind} commit(s) behind {worktree_ref(config)}",
            file=sys.stderr,
        )
        return 1
    print(f"team-launcher: launcher checkout {repo} is current at {worktree_ref(config)}")
    return 0


def _tracked_dirty_paths_from_status(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in output.splitlines():
        if not raw_line:
            continue
        code = raw_line[:2]
        if code in {"??", "!!"}:
            continue
        path = raw_line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            paths.append(path)
    return paths


def _clean_paths_from_dry_run(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        prefix = "Would remove "
        if not line.startswith(prefix):
            continue
        path = line.removeprefix(prefix).strip()
        if path:
            paths.append(path)
    return paths


def warn_before_shared_checkout_refresh(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult | None:
    status_proc = run_owner_correct_git(
        git_shared_checkout_status_porcelain_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.returncode != 0:
        reason = _proc_failure_reason(status_proc, f"status failed with exit {status_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    clean_proc = run_owner_correct_git(
        git_clean_shared_checkout_dry_run_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if clean_proc.returncode != 0:
        reason = _proc_failure_reason(clean_proc, f"clean dry-run failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})

    tracked_paths = _tracked_dirty_paths_from_status(str(status_proc.stdout or ""))
    clean_paths = _clean_paths_from_dry_run(str(clean_proc.stdout or ""))
    if not tracked_paths and not clean_paths:
        return None

    print(
        f"warning: team-launcher will reset managed project checkout {config.repository} to {worktree_ref(config)}",
        file=sys.stderr,
    )
    if tracked_paths:
        print("warning: tracked changes will be discarded:", file=sys.stderr)
        for path in tracked_paths:
            print(f"warning:   {path}", file=sys.stderr)
    if clean_paths:
        print("warning: untracked files will be removed:", file=sys.stderr)
        for path in clean_paths:
            print(f"warning:   {path}", file=sys.stderr)
    return None


def warn_before_role_worktree_refresh(
    config: ProjectConfig,
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult | None:
    status_proc = run_owner_correct_git(
        git_role_worktree_status_porcelain_args(role),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if status_proc.returncode != 0:
        reason = _proc_failure_reason(status_proc, f"status failed with exit {status_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason})
    clean_proc = run_owner_correct_git(
        git_clean_role_worktree_dry_run_args(role),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if clean_proc.returncode != 0:
        reason = _proc_failure_reason(clean_proc, f"clean dry-run failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason})

    tracked_paths = _tracked_dirty_paths_from_status(str(status_proc.stdout or ""))
    clean_paths = _clean_paths_from_dry_run(str(clean_proc.stdout or ""))
    if not tracked_paths and not clean_paths:
        return None

    print(
        f"warning: team-launcher will reset managed role worktree {role.workdir} "
        f"for {role.role} to {worktree_ref(config)}",
        file=sys.stderr,
    )
    if tracked_paths:
        print("warning: tracked changes will be discarded:", file=sys.stderr)
        for path in tracked_paths:
            print(f"warning:   {path}", file=sys.stderr)
    if clean_paths:
        print("warning: untracked files will be removed:", file=sys.stderr)
        for path in clean_paths:
            print(f"warning:   {path}", file=sys.stderr)
    return None


def ensure_control_repository(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    if config.control_repository is None:
        return WorktreeProvisionResult({})
    repair_result = repair_control_repository_ownership(config, runner=runner)
    if not repair_result.ok:
        return repair_result
    mkdir_proc = runner(mkdir_p_args(config.control_repository.parent))
    if mkdir_proc.returncode != 0:
        reason = _proc_failure_reason(mkdir_proc, f"mkdir failed with exit {mkdir_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    if not config.control_repository.exists():
        clone_proc = run_owner_correct_git(
            git_clone_control_repository_args(config),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if clone_proc.returncode != 0:
            reason = _proc_failure_reason(clone_proc, f"clone failed with exit {clone_proc.returncode}")
            return WorktreeProvisionResult({role.role: reason for role in config.roles})
        if config.worktree_remote != "origin":
            rename_proc = run_owner_correct_git(
                git_control_remote_rename_args(config),
                runner=runner,
                owner_rules=_config_git_owner_rules(config),
            )
            if rename_proc.returncode != 0:
                reason = _proc_failure_reason(rename_proc, f"remote rename failed with exit {rename_proc.returncode}")
                return WorktreeProvisionResult({role.role: reason for role in config.roles})
    refspec_proc = run_owner_correct_git(
        git_control_fetch_refspec_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if refspec_proc.returncode != 0:
        reason = _proc_failure_reason(refspec_proc, f"fetch refspec config failed with exit {refspec_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    fetch_proc = run_owner_correct_git(
        git_fetch_control_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if fetch_proc.returncode != 0:
        reason = _proc_failure_reason(fetch_proc, f"fetch failed with exit {fetch_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    return WorktreeProvisionResult({})


def chown_control_repository_args(config: ProjectConfig, path: Path | None = None, *, recursive: bool = True) -> list[str]:
    if config.control_repository is None or not config.run_as_user:
        raise ValueError("control repository ownership repair requires control_repository and run_as_user")
    args = ["chown"]
    if recursive:
        args.append("-R")
    args.extend([f"{config.run_as_user}:{config.run_as_user}", str(path or config.control_repository)])
    return args


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


def _control_repository_managed_root(config: ProjectConfig) -> Path | None:
    owner_home = _control_repository_owner_home(config)
    if owner_home is None:
        return None
    return (owner_home / ".local" / "state" / "switchyard" / "projects").resolve(strict=False)


def _control_repository_owner_home(config: ProjectConfig) -> Path | None:
    if config.control_repository is None or not config.run_as_user:
        return None
    try:
        expected = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return None
    return Path(expected.pw_dir).resolve(strict=False)


def _control_repository_boundary_error(config: ProjectConfig, *, require_existing_user: bool) -> str | None:
    if config.control_repository is None:
        return None
    managed_root = _control_repository_managed_root(config)
    if managed_root is None:
        if not require_existing_user and config.run_as_user:
            # Load-time validation accepts pre-account-creation configs from `new`.
            # The same prefix check still bounds the eventual repair path.
            owner_home = (Path("/home") / config.run_as_user).resolve(strict=False)
            managed_root = (owner_home / ".local" / "state" / "switchyard" / "projects").resolve(strict=False)
        else:
            return f"target user {config.run_as_user!r} does not exist"
    parent = config.control_repository.expanduser().resolve(strict=False).parent
    if parent == managed_root or not parent.is_relative_to(managed_root):
        return f"{parent} is not a switchyard-managed control directory under {managed_root}"
    return None


def _path_owner_ids(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    return info.st_uid, info.st_gid


def _control_repository_owner_mismatch_path(config: ProjectConfig) -> Path | None:
    if config.control_repository is None or not config.run_as_user:
        return None
    try:
        expected = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return config.control_repository.parent if config.control_repository.parent.exists() else None
    control_path = config.control_repository
    if not control_path.exists():
        return None
    stack = [control_path]
    while stack:
        path = stack.pop()
        owner_ids = _path_owner_ids(path)
        if owner_ids is None:
            return path
        if owner_ids != (expected.pw_uid, expected.pw_gid):
            return path
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            try:
                children = list(path.iterdir())
            except OSError:
                return path
            stack.extend(children)
    return None


def _control_repository_repair_paths(config: ProjectConfig) -> tuple[list[tuple[Path, bool]], Path | None]:
    if config.control_repository is None or not config.run_as_user:
        return [], None
    boundary_error = _control_repository_boundary_error(config, require_existing_user=True)
    if boundary_error is not None and not config.control_repository.exists() and not config.control_repository.parent.exists():
        return [], None
    try:
        expected = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return [], config.control_repository.parent if config.control_repository.parent.exists() else None

    parent = config.control_repository.parent
    start = config.control_repository if config.control_repository.exists() else parent
    owner_home = _control_repository_owner_home(config)
    if owner_home is None:
        return [], config.control_repository.parent if config.control_repository.parent.exists() else None
    repair_paths: list[tuple[Path, bool]] = []
    current = start
    while True:
        resolved = current.expanduser().resolve(strict=False)
        if not (resolved == owner_home or resolved.is_relative_to(owner_home)):
            return [], current
        owner_ids = _path_owner_ids(current)
        if owner_ids is not None:
            if owner_ids == (expected.pw_uid, expected.pw_gid):
                break
            repair_paths.append((current, current == config.control_repository))
        if resolved == owner_home:
            break
        current = current.parent

    leaf_mismatch = _control_repository_owner_mismatch_path(config)
    if leaf_mismatch is not None and all(path != config.control_repository for path, _recursive in repair_paths):
        repair_paths.append((config.control_repository, True))
    repair_paths.reverse()
    return repair_paths, repair_paths[0][0] if repair_paths else leaf_mismatch


def repair_control_repository_ownership(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    if config.control_repository is None or not config.run_as_user:
        return WorktreeProvisionResult({})
    repair_paths, mismatch = _control_repository_repair_paths(config)
    if mismatch is None:
        return WorktreeProvisionResult({})
    boundary_error = _control_repository_boundary_error(config, require_existing_user=True)
    if boundary_error is not None:
        message = f"refusing to repair control repository ownership for {config.control_repository}: {boundary_error}"
        return WorktreeProvisionResult({role.role: message for role in config.roles})
    actual_owner = _path_owner_label(mismatch)
    for path, recursive in repair_paths:
        chown_proc = runner(chown_control_repository_args(config, path, recursive=recursive))
        if chown_proc.returncode != 0:
            reason = _proc_failure_reason(chown_proc, f"chown failed with exit {chown_proc.returncode}")
            message = (
                f"failed to repair control repository ownership for {config.control_repository}: {reason}; "
                f"{mismatch} is owned by {actual_owner}, expected {config.run_as_user}:{config.run_as_user}"
            )
            return WorktreeProvisionResult({role.role: message for role in config.roles})
    return WorktreeProvisionResult({})


def ensure_control_role_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    control_result = ensure_control_repository(config, runner=runner)
    if not control_result.ok or not refresh:
        return control_result
    if config.worktree_base is None:
        return WorktreeProvisionResult({role.role: "project config does not define worktree_base" for role in config.roles})
    mkdir_proc = runner(mkdir_p_args(config.worktree_base))
    if mkdir_proc.returncode != 0:
        reason = _proc_failure_reason(mkdir_proc, f"mkdir failed with exit {mkdir_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})

    failed: dict[str, str] = {}
    for role in config.roles:
        role_path = Path(role.workdir)
        if not role_path.exists():
            add_proc = run_owner_correct_git(
                git_control_worktree_add_args(config, role),
                runner=runner,
                owner_rules=_config_git_owner_rules(config),
            )
            if add_proc.returncode != 0:
                failed[role.role] = _proc_failure_reason(add_proc, f"worktree add failed with exit {add_proc.returncode}")
            continue
        check_proc = run_owner_correct_git(
            git_role_worktree_check_args(role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if check_proc.returncode != 0:
            failed[role.role] = _proc_failure_reason(check_proc, f"worktree check failed with exit {check_proc.returncode}")
            continue
        warning_result = warn_before_role_worktree_refresh(config, role, runner=runner)
        if warning_result is not None:
            failed.update(warning_result.failed_roles)
            continue
        reset_proc = run_owner_correct_git(
            git_role_worktree_reset_args(config, role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if reset_proc.returncode != 0:
            failed[role.role] = _proc_failure_reason(reset_proc, f"reset failed with exit {reset_proc.returncode}")
            continue
        clean_proc = run_owner_correct_git(
            git_clean_role_worktree_args(role),
            runner=runner,
            owner_rules=_config_git_owner_rules(config),
        )
        if clean_proc.returncode != 0:
            failed[role.role] = _proc_failure_reason(clean_proc, f"clean failed with exit {clean_proc.returncode}")
    return WorktreeProvisionResult(failed)


def ensure_project_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> WorktreeProvisionResult:
    if config.control_repository is not None:
        return ensure_control_role_worktrees(config, refresh=refresh, runner=runner)
    if config.repository is None:
        return WorktreeProvisionResult({})
    fetch_proc = run_owner_correct_git(
        git_fetch_worktree_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if fetch_proc.returncode != 0:
        reason = _proc_failure_reason(fetch_proc, f"fetch failed with exit {fetch_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    if not refresh:
        return WorktreeProvisionResult({})
    check_proc = run_owner_correct_git(
        git_shared_checkout_check_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        reason = _proc_failure_reason(check_proc, f"repository check failed with exit {check_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    warning_result = warn_before_shared_checkout_refresh(config, runner=runner)
    if warning_result is not None:
        return warning_result
    checkout_proc = run_owner_correct_git(
        git_checkout_shared_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if checkout_proc.returncode != 0:
        reason = _proc_failure_reason(checkout_proc, f"checkout failed with exit {checkout_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    clean_proc = run_owner_correct_git(
        git_clean_shared_checkout_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if clean_proc.returncode != 0:
        reason = _proc_failure_reason(clean_proc, f"clean failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    return WorktreeProvisionResult({})


def fetch_project_worktree_ref(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    if config.control_repository is not None:
        result = ensure_control_repository(config, runner=runner)
        if result.ok:
            return 0
        print(
            f"warning: failed to fetch {worktree_ref(config)} for reload: "
            f"{next(iter(result.failed_roles.values()), 'unknown error')}",
            file=sys.stderr,
        )
        return 1
    if config.repository is None:
        return 0
    fetch_proc = run_owner_correct_git(
        git_fetch_worktree_ref_args(config),
        runner=runner,
        owner_rules=_config_git_owner_rules(config),
    )
    if fetch_proc.returncode != 0:
        print(
            f"warning: failed to fetch {worktree_ref(config)} for reload: "
            f"{_proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}",
            file=sys.stderr,
        )
    return int(fetch_proc.returncode)


def expected_live_commands(role: RoleConfig) -> set[str]:
    configured = role.live_commands or [_command_name(role.cli[0])]
    return {_command_name(command) for command in configured if _command_name(command)}


def _process_snapshot() -> tuple[dict[int, int], dict[int, list[int]], dict[int, set[str]], dict[int, list[str]]]:
    proc = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,comm=,args="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return {}, {}, {}, {}
    parents: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    names: dict[int, set[str]] = {}
    argv_by_pid: dict[int, list[str]] = {}
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        parents[pid] = ppid
        argv: list[str]
        try:
            argv = shlex.split(parts[3])
        except ValueError:
            argv = []
        argv_by_pid[pid] = argv
        command_names = {_command_name(parts[2])}
        command_names.update(_command_name(token) for token in argv if _command_name(token))
        names[pid] = command_names
        children.setdefault(ppid, []).append(pid)
    return parents, children, names, argv_by_pid


def process_tree_command_names(pane_pid: int) -> set[str]:
    if pane_pid <= 0:
        return set()
    _parents_by_pid, children_by_parent, process_names, _argv_by_pid = _process_snapshot()
    stack = [pane_pid]
    seen: set[int] = set()
    names: set[str] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        names.update(process_names.get(pid, set()))
        stack.extend(children_by_parent.get(pid, []))
    return names


def process_tree_argvs(pane_pid: int) -> list[list[str]]:
    if pane_pid <= 0:
        return []
    _parents_by_pid, children_by_parent, _process_names, argv_by_pid = _process_snapshot()
    stack = [pane_pid]
    seen: set[int] = set()
    records: list[list[str]] = []
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        argv = argv_by_pid.get(pid, [])
        if argv:
            records.append(list(argv))
        stack.extend(children_by_parent.get(pid, []))
    return records


def _model_from_argv(argv: Sequence[str], *, model_arg: str) -> str:
    if not model_arg:
        return ""
    for index, token in enumerate(argv):
        if token == model_arg and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if token.startswith(f"{model_arg}="):
            return token.split("=", 1)[1].strip()
    return ""


def process_tree_contains_command(pane_pid: int, expected_commands: set[str]) -> bool:
    return bool(process_tree_command_names(pane_pid) & expected_commands)


def live_cli_for_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    pane_pid = pane_pid_for_role(role, runner=runner)
    if pane_pid <= 0:
        return []
    matches = sorted(name for name in process_tree_command_names(pane_pid) if name in KNOWN_LIVE_CLI_NAMES)
    if len(matches) == 1:
        return [matches[0]]
    configured_cli = _command_name(role.cli[0])
    if configured_cli in matches:
        return [configured_cli]
    return []


def live_model_for_role(
    role: RoleConfig,
    *,
    session_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    pane_pid = pane_pid_for_role(role, runner=runner)
    if pane_pid > 0:
        for argv in process_tree_argvs(pane_pid):
            model = _model_from_argv(argv, model_arg=role.model_arg)
            if model:
                return model
    return _session_payload_model_for_role(role, session_dir)


def live_command_matches_role(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    expected = expected_live_commands(role)
    pane_pid = pane_pid_for_role(role, runner=runner)
    if pane_pid > 0:
        return process_tree_contains_command(pane_pid, expected)

    proc = runner(tmux_current_command_args(role), text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return False
    actual = _command_name(str(proc.stdout).strip())
    return actual in expected


RESUME_LAUNCH_VERIFIED = "verified"
RESUME_LAUNCH_MISSING = "missing"
RESUME_LAUNCH_TIMEOUT = "timeout"


def _resume_launch_status(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    deadline = time.monotonic() + RESUME_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if live_command_matches_role(role, runner=runner):
            return RESUME_LAUNCH_VERIFIED
        if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return RESUME_LAUNCH_MISSING
        time.sleep(RESUME_STARTUP_POLL_SECONDS)
    if live_command_matches_role(role, runner=runner):
        return RESUME_LAUNCH_VERIFIED
    if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return RESUME_LAUNCH_MISSING
    return RESUME_LAUNCH_TIMEOUT


def _resume_launch_verified(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    return _resume_launch_status(role, runner=runner) == RESUME_LAUNCH_VERIFIED


def _detached_launch_verified(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    if not _resume_launch_verified(role, runner=runner):
        return False
    deadline = time.monotonic() + DETACHED_SESSION_STABILITY_SECONDS
    while time.monotonic() < deadline:
        if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
        if not live_command_matches_role(role, runner=runner):
            return False
        time.sleep(RESUME_STARTUP_POLL_SECONDS)
    return (
        runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        and live_command_matches_role(role, runner=runner)
    )


def _start_role_session(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path,
    prefer_resume: bool,
    seed_source: str,
    post_start_verifier: Callable[[], bool] | None = None,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    session_id = session_id_for_role(role, session_dir) if prefer_resume else ""
    if prefer_resume and not session_id:
        print(
            f"team-launcher: no recorded session id for {role.role}; starting fresh session",
            file=sys.stderr,
        )
        prefer_resume = False
    if prefer_resume and _uses_fresh_session_per_ticket(role):
        print(
            f"team-launcher: {role.role} is configured for fresh sessions per ticket; "
            "starting a fresh session instead of inheriting the previous ticket context",
            file=sys.stderr,
        )
        prefer_resume = False
    if prefer_resume:
        preflight_ok, preflight_message = _resume_preflight_allows_attempt(role, session_id, session_dir=session_dir)
        if not preflight_ok:
            print(preflight_message, file=sys.stderr)
            prefer_resume = False
    if not prefer_resume:
        clear_session_record_for_role(role, session_dir)
    prepare_hermes_home_for_role(role, session_dir=session_dir)
    start_proc = runner(
        tmux_new_session_args(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            resume=prefer_resume,
            bin_user=bin_user,
        )
    )
    if start_proc.returncode != 0:
        return int(start_proc.returncode)
    if not prefer_resume:
        if post_start_verifier is not None and not post_start_verifier():
            clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
            print(
                f"team-launcher: role {role.role} did not leave a live {role.tmux_session} session",
                file=sys.stderr,
            )
            return 1
        configure_result = configure_tmux_session_options(role.tmux_session, runner=runner)
        if configure_result != 0:
            return configure_result
        clear_unverified_resume_for_role(role, session_dir)
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    resume_status = _resume_launch_status(role, runner=runner)
    if resume_status == RESUME_LAUNCH_VERIFIED and (post_start_verifier is None or post_start_verifier()):
        configure_result = configure_tmux_session_options(role.tmux_session, runner=runner)
        if configure_result != 0:
            return configure_result
        clear_unverified_resume_for_role(role, session_dir)
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    if resume_status == RESUME_LAUNCH_TIMEOUT:
        configure_result = configure_tmux_session_options(role.tmux_session, runner=runner)
        if configure_result != 0:
            return configure_result
        record_unverified_resume_for_role(role, session_dir, session_id)
        print(
            f"team-launcher: resume for {role.role} using session {session_id} was not verified within "
            f"{RESUME_STARTUP_TIMEOUT_SECONDS:g}s; leaving tmux session and session record intact",
            file=sys.stderr,
        )
        return 0
    print(
        f"team-launcher: resume failed for {role.role} using session {session_id}; falling back to fresh session",
        file=sys.stderr,
    )
    if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        kill_proc = runner(tmux_kill_session_args(role))
        if kill_proc.returncode != 0:
            return int(kill_proc.returncode)
    clear_session_record_for_role(role, session_dir)
    prepare_hermes_home_for_role(role, session_dir=session_dir)
    fresh_proc = runner(
        tmux_new_session_args(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            resume=False,
            bin_user=bin_user,
        )
    )
    if fresh_proc.returncode != 0:
        return int(fresh_proc.returncode)
    if post_start_verifier is not None and not post_start_verifier():
        clear_pane_idle_state_for_role(role, pane_state_dir=pane_state_dir)
        print(
            f"team-launcher: role {role.role} did not leave a live {role.tmux_session} session after resume fallback",
            file=sys.stderr,
        )
        return 1
    configure_result = configure_tmux_session_options(role.tmux_session, runner=runner)
    if configure_result != 0:
        return configure_result
    clear_unverified_resume_for_role(role, session_dir)
    seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=f"{seed_source}.fallback")
    print(
        f"team-launcher: started fresh session for {role.role} after resume fallback",
        file=sys.stderr,
    )
    return 0


def desktop_reload_is_safe(role: RoleConfig, pane_state_dir: Path) -> bool:
    if not role.unset_env:
        return True
    from scripts.ticket_board.notify_listener import PaneActivityGate, PaneHookStateStore
    gate = PaneActivityGate(state_store=PaneHookStateStore(pane_state_dir))
    if gate.is_busy(role.target):
        print(f"switchyard: {role.target} is busy; wait for an idle checkpoint before reloading desktop environment", file=sys.stderr)
        return False
    return True


def run_role_pane(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    force_reload: bool = False,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    exists = runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return runner(tmux_attach_args(role)).returncode
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    if mode == "reload":
        if exists and not desktop_reload_is_safe(role, pane_state_dir):
            return 1
        if exists:
            if not force_reload and not live_command_matches_role(role, runner=runner):
                print(
                    f"refusing to reload {role.tmux_session}: live pane command does not match configured CLI; "
                    "rerun with --force to override",
                    file=sys.stderr,
                )
                return 1
            kill_proc = runner(tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        start_result = _start_role_session(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            prefer_resume=True,
            seed_source="team_launcher.reload",
            bin_user=bin_user,
            runner=runner,
        )
        if start_result != 0:
            return start_result
        return runner(tmux_attach_args(role)).returncode
    if mode in {"start", "attach-or-start"}:
        if not exists:
            start_result = _start_role_session(
                role,
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                prefer_resume=True,
                seed_source="team_launcher.start",
                post_start_verifier=lambda: _resume_launch_verified(role, runner=runner),
                bin_user=bin_user,
                runner=runner,
            )
            if start_result != 0:
                return start_result
        return runner(tmux_attach_args(role)).returncode
    raise SystemExit(f"unknown pane mode: {mode}")


def run_detached_role(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    force_reload: bool = False,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    exists = runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return 0
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    if mode == "reload":
        if exists and not desktop_reload_is_safe(role, pane_state_dir):
            return 1
        if exists:
            if not force_reload and not live_command_matches_role(role, runner=runner):
                print(
                    f"refusing to reload {role.tmux_session}: live pane command does not match configured CLI; "
                    "rerun with --force to override",
                    file=sys.stderr,
                )
                return 1
            kill_proc = runner(tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        return _start_role_session(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            prefer_resume=True,
            seed_source="team_launcher.reload",
            post_start_verifier=lambda: _detached_launch_verified(role, runner=runner),
            bin_user=bin_user,
            runner=runner,
        )
    if mode in {"start", "attach-or-start"}:
        if not exists:
            return _start_role_session(
                role,
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                prefer_resume=True,
                seed_source="team_launcher.start",
                post_start_verifier=lambda: _detached_launch_verified(role, runner=runner),
                bin_user=bin_user,
                runner=runner,
            )
        return 0
    raise SystemExit(f"unknown detached role mode: {mode}")


def ensure_visible_role_session_for_viewer(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    force_reload: bool = False,
    bin_user: str = "",
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    exists = runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return 0
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    if mode == "reload":
        if exists and not desktop_reload_is_safe(role, pane_state_dir):
            return 1
        if exists:
            if not force_reload and not live_command_matches_role(role, runner=runner):
                print(
                    f"refusing to reload {role.tmux_session}: live pane command does not match configured CLI; "
                    "rerun with --force to override",
                    file=sys.stderr,
                )
                return 1
            kill_proc = runner(tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        return _start_role_session(
            role,
            session_dir=session_dir,
            pane_state_dir=pane_state_dir,
            prefer_resume=True,
            seed_source="team_launcher.reload",
            bin_user=bin_user,
            runner=runner,
        )
    if mode in {"start", "attach-or-start"}:
        if not exists:
            return _start_role_session(
                role,
                session_dir=session_dir,
                pane_state_dir=pane_state_dir,
                prefer_resume=True,
                seed_source="team_launcher.start",
                post_start_verifier=lambda: _resume_launch_verified(role, runner=runner),
                bin_user=bin_user,
                runner=runner,
            )
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source="team_launcher.start")
        return 0
    raise SystemExit(f"unknown viewer role mode: {mode}")


def _layout_leaves(node: Any) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []
    if isinstance(node, dict):
        widgets = node.get("Widgets")
        if isinstance(widgets, list):
            for child in widgets:
                leaves.extend(_layout_leaves(child))
        elif "Command" in node:
            leaves.append(node)
    return leaves


def pane_command_args(
    project: str,
    role: RoleConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    slot: int | None = None,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    skip_launcher_check: bool = False,
    allow_stale_launcher: bool = False,
    no_attach: bool = False,
    run_as_user: str = "",
) -> list[str]:
    args = [
        str(script_path),
        project,
        "pane",
        mode,
        role.role,
        "--config",
        str(config_path),
    ]
    if mode == "reload" and force_reload:
        args.append("--force")
    if slot is not None:
        args.extend(["--slot", str(slot)])
    if skip_launcher_check:
        args.append("--skip-launcher-check")
    if allow_stale_launcher:
        args.append("--allow-stale-launcher")
    if no_attach:
        args.append("--no-attach")
    if pane_state_dir is not None:
        args.extend(["--pane-state-dir", str(pane_state_dir)])
    if run_as_user and current_user_name() != run_as_user:
        args = ["sudo", "-u", run_as_user, "-H", *args]
    return args


def pane_command(
    project: str,
    role: RoleConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    slot: int | None = None,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    skip_launcher_check: bool = False,
    allow_stale_launcher: bool = False,
    no_attach: bool = False,
    run_as_user: str = "",
) -> str:
    args = pane_command_args(
        project,
        role,
        config_path=config_path,
        mode=mode,
        script_path=script_path,
        slot=slot,
        pane_state_dir=pane_state_dir,
        force_reload=force_reload,
        skip_launcher_check=skip_launcher_check,
        allow_stale_launcher=allow_stale_launcher,
        no_attach=no_attach,
        run_as_user=run_as_user,
    )
    return _quote_command(args)


def _running_project_roles(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[RoleConfig]:
    """Which roles have a live session, asked of each role's own tmux server.

    Probing them all through the project owner's server cannot see a role that
    runs as its own account, so restart and liveness both misreport (SYRD-39).
    """
    running_roles: list[RoleConfig] = []
    for role in config.roles:
        role_runner = role_process_runner_for(config, role, runner=runner)
        result = role_runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0 and live_command_matches_role(role, runner=role_runner):
            running_roles.append(role)
    return running_roles


def _prepare_project_worktrees_for_launch(
    config: ProjectConfig,
    *,
    running_roles: list[RoleConfig],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> WorktreeProvisionResult:
    if not running_roles:
        return ensure_project_worktrees(config, refresh=True, runner=runner)
    running_role_names = {role.role for role in running_roles}
    if config.control_repository is not None:
        stopped_roles = [role for role in config.roles if role.role not in running_role_names]
        if not stopped_roles:
            return WorktreeProvisionResult({})
        result = ensure_project_worktrees(replace(config, roles=stopped_roles), refresh=True, runner=runner)
    else:
        result = ensure_project_worktrees(config, refresh=False, runner=runner)
    return WorktreeProvisionResult(
        {role: reason for role, reason in result.failed_roles.items() if role not in running_role_names}
    )


def failed_role_command(role: RoleConfig, reason: str) -> str:
    message = f"PGU launcher did not start {role.role}: checkout refresh failed: {reason}"
    return _quote_command(["sh", "-lc", f"printf '%s\\n' {shlex.quote(message)}; sleep 30"])


def materialize_layout(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    output_path: Path,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    failed_roles: dict[str, str] | None = None,
) -> Path:
    failed_roles = failed_roles or {}
    visible_roles = [role.role for role in config.roles if not role.detached]
    if len(visible_roles) > MAX_VISIBLE_PANES_PER_WINDOW:
        raise SystemExit(
            f"team-launcher: {config.project} has {len(visible_roles)} visible roles; at most "
            f"{MAX_VISIBLE_PANES_PER_WINDOW} panes can be visible in one window; detach extra roles or open an "
            "additional director-invoked window"
        )
    layout = json.loads(config.layout.read_text(encoding="utf-8"))
    leaves = _layout_leaves(layout)
    for role in config.roles:
        if role.detached:
            continue
        if role.slot is None:
            continue
        if role.slot < 0 or role.slot >= len(leaves):
            raise SystemExit(f"role {role.role} slot {role.slot} is outside layout leaf count {len(leaves)}")
        leaf = leaves[role.slot]
        if role.role in failed_roles:
            leaf["Command"] = failed_role_command(role, failed_roles[role.role])
            leaf["WorkingDirectory"] = str(Path.home())
        else:
            leaf["Command"] = pane_command(
                config.project,
                role,
                config_path=config_path,
                mode=mode,
                script_path=script_path,
                pane_state_dir=pane_state_dir,
                force_reload=force_reload,
                skip_launcher_check=True,
                run_as_user=role_run_as_user(config, role),
            )
            leaf["WorkingDirectory"] = role.workdir
        leaf["Title"] = project_window_title(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _verify_pane_launcher_path(
    config: ProjectConfig,
    *,
    script_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Path:
    pane_script_path = config.pane_launcher or script_path
    if config.pane_launcher is None:
        return pane_script_path
    result = runner(["test", "-x", str(pane_script_path)])
    if result.returncode != 0:
        owner = f" by {config.run_as_user}" if config.run_as_user else ""
        raise SystemExit(f"team-launcher: configured pane_launcher {pane_script_path} is not readable/executable{owner}")
    return pane_script_path


def _owner_state_layout_output_path(project: str, *, owner_home: Path) -> Path:
    return owner_home / ".local" / "state" / "switchyard" / "projects" / project / f"{project}-team-layout.json"


def default_layout_output_path(config: ProjectConfig, *, config_path: Path) -> Path:
    if config.run_as_user:
        try:
            owner_home = Path(pwd.getpwnam(config.run_as_user).pw_dir)
        except KeyError:
            owner_home = None
            paths = [config_path.expanduser().resolve(strict=False)]
            if config.repository is not None:
                paths.append(config.repository.expanduser().resolve(strict=False))
            for path in paths:
                for candidate in (path, *path.parents):
                    if candidate.name == config.run_as_user:
                        owner_home = candidate
                        break
                if owner_home is not None:
                    break
            if owner_home is None:
                owner_home = Path("/home") / config.run_as_user
        return _owner_state_layout_output_path(config.project, owner_home=owner_home)
    else:
        base_dir = config_path.parent / SWITCHYARD_PROJECT_DIR_NAME / config.project
        return base_dir / f"{config.project}-team-layout.json"


def chown_layout_output_args(config: ProjectConfig, output_path: Path) -> list[str]:
    if not config.run_as_user:
        raise ValueError("layout ownership repair requires run_as_user")
    return ["chown", "-R", f"{config.run_as_user}:{config.run_as_user}", str(output_path.parent)]


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
    if not config.run_as_user or current_user_name() == config.run_as_user or os.geteuid() != 0:
        return
    result = runner(chown_owner_file_args(config, path))
    if result.returncode != 0:
        reason = _proc_failure_reason(result, f"chown failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to assign generated file {path} to {config.run_as_user}: {reason}")


def ensure_layout_output_owner(
    config: ProjectConfig,
    output_path: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    if not config.run_as_user or current_user_name() == config.run_as_user:
        return
    result = runner(chown_layout_output_args(config, output_path))
    if result.returncode != 0:
        reason = _proc_failure_reason(result, f"chown failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to assign layout output {output_path.parent} to {config.run_as_user}: {reason}")


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
    if not config.run_as_user:
        return ()
    try:
        owner = pwd.getpwnam(config.run_as_user)
    except KeyError:
        return ()
    return (
        Path(owner.pw_dir).expanduser().resolve(strict=False),
        runtime_dir_for_uid(owner.pw_uid).expanduser().resolve(strict=False),
    )


def _is_owner_state_path(config: ProjectConfig, path: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    return any(resolved == root or resolved.is_relative_to(root) for root in _owner_state_roots(config))


def ensure_owner_state_dirs(
    config: ProjectConfig,
    *,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    if not config.run_as_user or current_user_name() == config.run_as_user:
        return
    for path in (config.session_dir, pane_state_dir):
        if not _is_owner_state_path(config, path):
            continue
        result = runner(install_owner_state_dir_args(config, path))
        if result.returncode != 0:
            reason = _proc_failure_reason(result, f"install failed with exit {result.returncode}")
            raise SystemExit(f"team-launcher: failed to assign state directory {path} to {config.run_as_user}: {reason}")


def install_generated_project_pane_hooks_args(
    config: ProjectConfig,
    *,
    script_path: Path,
    pane_state_dir: Path,
) -> list[str]:
    if not config.run_as_user:
        raise ValueError("pane hook repair requires run_as_user")
    owner_home = home_dir_for_user(config.run_as_user) or Path("/home") / config.run_as_user
    launcher_path = (config.pane_launcher or script_path).expanduser()
    hook_installer = launcher_path.with_name("ticket-board-install-pane-hooks")
    hook_source = launcher_path.with_name("ticket-board-pane-idle-hook")
    hook_bin = owner_home / ".local" / "bin" / "ticket-board-pane-idle-hook"
    uid = uid_for_user(config.run_as_user)
    if uid is None:
        raise SystemExit(f"team-launcher: cannot install pane hooks for unknown user {config.run_as_user!r}")
    env_args = [
        "env",
        f"XDG_RUNTIME_DIR={runtime_dir_for_uid(uid)}",
        f"TICKET_BOARD_PROJECT={config.project}",
        f"TICKET_BOARD_PANE_STATE_DIR={pane_state_dir}",
        f"TICKET_BOARD_PANE_SESSION_DIR={config.session_dir}",
        str(hook_installer),
        "install",
        "--home",
        str(owner_home),
        "--hook-source",
        str(hook_source),
        "--bin-path",
        str(hook_bin),
    ]
    if current_user_name() == config.run_as_user:
        return env_args
    return ["sudo", "-u", config.run_as_user, "-H", *env_args]


def ensure_generated_project_pane_hooks(
    config: ProjectConfig,
    *,
    config_path: Path,
    script_path: Path,
    pane_state_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    if not config.run_as_user or not _is_generated_project_layout_template(config, config_path=config_path):
        return
    args = install_generated_project_pane_hooks_args(
        config,
        script_path=script_path,
        pane_state_dir=pane_state_dir,
    )
    result = runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        reason = _proc_failure_reason(result, f"installer failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to install pane hooks for {config.project}: {reason}")


def install_generated_project_board_skill_args(
    config: ProjectConfig,
    *,
    installer: Path,
    source_commit: str = "",
    dry_run: bool = False,
) -> list[str]:
    if not config.run_as_user:
        raise ValueError("board skill install requires run_as_user")
    owner_home = home_dir_for_user(config.run_as_user) or Path("/home") / config.run_as_user
    args = [str(installer), "install", "--home", str(owner_home)]
    if source_commit and source_commit != "unknown":
        args.extend(["--source-commit", source_commit])
    if dry_run:
        args.append("--dry-run")
    if current_user_name() == config.run_as_user:
        return args
    return ["sudo", "-u", config.run_as_user, "-H", *args]


def board_skill_installer_path(
    config: ProjectConfig,
    *,
    script_path: Path,
    source_repo: Path | None = None,
) -> Path:
    """The installer that ships beside the launcher this project actually runs."""
    if source_repo is not None:
        return source_repo / "scripts" / BOARD_SKILL_INSTALLER_NAME
    launcher_path = (config.pane_launcher or script_path).expanduser()
    return launcher_path.with_name(BOARD_SKILL_INSTALLER_NAME)


def ensure_generated_project_board_skill(
    config: ProjectConfig,
    *,
    config_path: Path,
    script_path: Path,
    source_repo: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> None:
    """Project the board skill into the owner's CLI skill trees.

    On launch this runs before any role session starts, so a Hermes role home
    created later in the same launch finds a shared skills directory to link.
    """
    if not config.run_as_user or not _is_generated_project_layout_template(config, config_path=config_path):
        return
    installer = board_skill_installer_path(config, script_path=script_path, source_repo=source_repo)
    source_commit = _switchyard_source_commit(installer.parent.parent, runner=runner)
    args = install_generated_project_board_skill_args(
        config,
        installer=installer,
        source_commit=source_commit,
        dry_run=dry_run,
    )
    result = runner(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        # A missing or unwritable skill tree must not stop the team from
        # working; the pane still runs, it just starts without the skill.
        reason = _proc_failure_reason(result, f"installer failed with exit {result.returncode}")
        print_func(
            f"warning: switchyard: could not install the {BOARD_SKILL_NAME} skill for {config.project}: {reason}"
        )
        return
    for line in str(getattr(result, "stdout", "") or "").splitlines():
        # An unchanged copy is the normal case; only report what moved.
        if line.strip() and ": current " not in line:
            print_func(f"switchyard: board skill {line.strip()}")


def _legacy_new_project_stacked_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = [
        {
            "Command": "",
            "SessionRestoreId": index,
            "WorkingDirectory": "",
        }
        for index in range(role_count)
    ]
    if role_count <= 1:
        return leaves[0] if leaves else {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}
    return {
        "Orientation": "Horizontal",
        "Widgets": [
            leaves[0],
            {
                "Orientation": "Vertical",
                "Widgets": leaves[1:],
            },
        ],
    }


def _legacy_new_project_column_major_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    # Historical recognizer: old generated layouts used a single row through 4 roles.
    # Do not read the live layout threshold here or existing layouts stop upgrading.
    if role_count <= 4:
        return _single_row_layout_payload(leaves)
    return _legacy_new_project_sqrt_column_major_layout_payload(role_count)


def _legacy_new_project_sqrt_column_major_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    if role_count <= 1:
        return _single_row_layout_payload(leaves)
    column_count = math.ceil(math.sqrt(role_count))
    row_count = math.ceil(role_count / column_count)
    columns: list[dict[str, Any]] = []
    for start in range(0, role_count, row_count):
        column = leaves[start : start + row_count]
        if len(column) == 1:
            columns.append(column[0])
        else:
            columns.append(
                {
                    "Orientation": "Vertical",
                    "Widgets": column,
                }
            )
    return {
        "Orientation": "Horizontal",
        "Widgets": columns,
    }


def _legacy_new_project_chunked_row_major_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    # Historical recognizer: old generated layouts used a single row through 4 roles.
    # Do not read the live layout threshold here or existing layouts stop upgrading.
    if role_count <= 4:
        return _single_row_layout_payload(leaves)
    column_count = math.ceil(math.sqrt(len(leaves)))
    rows: list[dict[str, Any]] = []
    for start in range(0, len(leaves), column_count):
        row = leaves[start : start + column_count]
        if len(row) == 1:
            rows.append(row[0])
        else:
            rows.append(
                {
                    "Orientation": "Horizontal",
                    "Widgets": row,
                }
            )
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


def _known_generated_project_layout_payloads(role_count: int) -> tuple[dict[str, Any], ...]:
    return (
        _new_project_layout_payload(role_count),
        _legacy_new_project_stacked_layout_payload(role_count),
        _legacy_new_project_column_major_layout_payload(role_count),
        _legacy_new_project_sqrt_column_major_layout_payload(role_count),
        _legacy_new_project_chunked_row_major_layout_payload(role_count),
    )


def _is_generated_project_layout_template(config: ProjectConfig, *, config_path: Path) -> bool:
    config_file = config_path.expanduser().resolve(strict=False)
    layout_path = config.layout.expanduser().resolve(strict=False)
    provision_dir = config_file.parent
    return (
        provision_dir.name == "provision"
        and layout_path.parent == provision_dir
        and layout_path.name == f"{config.project}-konsole-layout.json"
    )


def _is_recognized_generated_project_layout(config: ProjectConfig, *, config_path: Path) -> bool:
    if not _is_generated_project_layout_template(config, config_path=config_path):
        return False
    role_count = sum(1 for role in config.roles if not role.detached)
    try:
        existing_layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return existing_layout in _known_generated_project_layout_payloads(role_count)


def _generated_project_durable_session_dir(config: ProjectConfig) -> Path | None:
    if not config.run_as_user:
        return None
    return Path(_new_project_session_dir(config.project, config.run_as_user))


def upgrade_generated_project_config(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherUpgradeResult:
    if not _is_generated_project_layout_template(config, config_path=config_path):
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: {config.project} layout is hand-maintained or outside a provision directory; leaving it unchanged",
    )
    changed_messages: list[str] = []
    durable_session_dir = _generated_project_durable_session_dir(config)
    session_dir_upgrade: Path | None = None
    shared_pane_launcher = switchyard_shared_pane_launcher()
    pane_launcher_upgrade: Path | None = None
    board_root = _tenant_board_root_from_config_or_plan(config, config_path)
    directorctl_upgrade = str(board_root / "current" / "scripts" / "directorctl") if board_root is not None else ""
    roles_need_directorctl_upgrade = bool(directorctl_upgrade) and any(
        role.env.get("TICKET_BOARD_DIRECTORCTL") != directorctl_upgrade for role in config.roles
    )
    if roles_need_directorctl_upgrade:
        if dry_run:
            changed_messages.append(f"pane directorctl can be pinned to {directorctl_upgrade}")
        else:
            changed_messages.append(f"pinned pane directorctl to {directorctl_upgrade}")
    if (
        config.pane_launcher is not None
        and config.pane_launcher.expanduser().resolve(strict=False)
        != shared_pane_launcher.expanduser().resolve(strict=False)
    ):
        if dry_run:
            changed_messages.append(
                f"pane launcher can be upgraded from {config.pane_launcher} to {shared_pane_launcher}"
            )
        else:
            pane_launcher_upgrade = shared_pane_launcher
            changed_messages.append(f"upgraded pane launcher to {shared_pane_launcher}")
    if (
        durable_session_dir is not None
        and session_dir_uses_user_runtime(config.session_dir, config.run_as_user)
        and config.session_dir.expanduser().resolve(strict=False) != durable_session_dir.expanduser().resolve(strict=False)
    ):
        if dry_run:
            changed_messages.append(
                f"session dir can be upgraded from {config.session_dir} to {durable_session_dir}"
            )
        else:
            session_dir_upgrade = durable_session_dir
            changed_messages.append(f"upgraded session dir to {durable_session_dir}")
    role_count = sum(1 for role in config.roles if not role.detached)
    current_layout = _new_project_layout_payload(role_count)
    known_layouts = _known_generated_project_layout_payloads(role_count)
    try:
        existing_layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except OSError as exc:
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: cannot read generated layout template {config.layout}: {exc}",
        )
    except json.JSONDecodeError as exc:
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: generated layout template {config.layout} is not valid JSON: {exc}",
        )
    if existing_layout != current_layout:
        if existing_layout not in known_layouts:
            if not changed_messages:
                return LauncherUpgradeResult(
                    changed=False,
                    message=f"switchyard: {config.project} layout template differs from the known generated legacy shapes; leaving it unchanged",
                )
            changed_messages.append("layout template differs from the known generated legacy shapes; leaving it unchanged")
        elif dry_run:
            changed_messages.append(f"layout template can be upgraded: {config.layout}")
        else:
            config.layout.write_text(json.dumps(current_layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            ensure_owner_file(config, config.layout, runner=runner)
            changed_messages.append(f"upgraded generated layout template: {config.layout}")
    elif not changed_messages:
        return LauncherUpgradeResult(
            changed=False,
            message=f"switchyard: {config.project} layout template is already current",
        )
    if session_dir_upgrade is not None or pane_launcher_upgrade is not None or roles_need_directorctl_upgrade:
        raw_config = _load_json(config_path)
        if session_dir_upgrade is not None:
            raw_config["session_dir"] = str(session_dir_upgrade)
        if pane_launcher_upgrade is not None:
            raw_config["pane_launcher"] = str(pane_launcher_upgrade)
        if roles_need_directorctl_upgrade:
            for raw_role in raw_config.get("roles", []):
                if not isinstance(raw_role, dict):
                    continue
                raw_env = raw_role.get("env")
                if not isinstance(raw_env, dict):
                    raw_env = {}
                    raw_role["env"] = raw_env
                raw_env["TICKET_BOARD_DIRECTORCTL"] = directorctl_upgrade
        _write_json_atomic(config_path, raw_config)
        ensure_owner_file(config, config_path, runner=runner)
    return LauncherUpgradeResult(
        changed=not dry_run,
        message=f"switchyard: upgraded generated project config for {config.project}: {'; '.join(changed_messages)}",
    )


def upgrade_generated_project_layout(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherUpgradeResult:
    return upgrade_generated_project_config(config, config_path=config_path, dry_run=dry_run, runner=runner)


def refresh_generated_project_runtime_artifacts(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> LauncherUpgradeResult:
    provision_dir = config_path.expanduser().resolve(strict=False).parent
    plan_path = provision_dir / "plan.json"
    if not plan_path.is_file():
        return LauncherUpgradeResult(False, f"switchyard: {config.project} has no generated runtime plan; leaving artifacts unchanged")
    try:
        plan = _project_board_provision_from_json(plan_path)
    except SystemExit as exc:
        return LauncherUpgradeResult(
            False,
            f"switchyard: {config.project} runtime plan is incomplete and cannot be refreshed automatically: {exc}",
        )
    replacements: dict[str, str] = {}
    if source_repo is not None:
        replacements["source_repo"] = str(source_repo.expanduser().resolve(strict=False))
    if commit_git_dir is not None:
        selected_commit_git_dir = commit_git_dir.strip()
        if not selected_commit_git_dir:
            raise SystemExit("switchyard: --commit-git-dir must not be empty")
        replacements["commit_git_dir"] = selected_commit_git_dir
    if replacements:
        plan = replace(plan, **replacements)
    with tempfile.TemporaryDirectory(prefix=f"switchyard-{config.project}-runtime-artifacts.") as tmp:
        staged = Path(tmp)
        write_artifacts(plan, staged, enable_owner_linger=False)
        changed_names = sorted(
            path.name
            for path in staged.iterdir()
            if not (provision_dir / path.name).is_file()
            or (provision_dir / path.name).read_bytes() != path.read_bytes()
        )
        if not changed_names:
            return LauncherUpgradeResult(False, f"switchyard: {config.project} generated runtime artifacts are already current")
        if dry_run:
            return LauncherUpgradeResult(
                False,
                f"switchyard: {config.project} generated runtime artifacts can be refreshed: {', '.join(changed_names)}",
            )
        for name in changed_names:
            target = provision_dir / name
            shutil.copyfile(staged / name, target)
            target.chmod((staged / name).stat().st_mode & 0o777)
            ensure_owner_file(config, target, runner=runner)
    return LauncherUpgradeResult(
        True,
        f"switchyard: refreshed {config.project} generated runtime artifacts: {', '.join(changed_names)}",
    )


def _tenant_board_root_from_config(config: ProjectConfig) -> Path | None:
    if config.pane_launcher is None:
        return None
    launcher = config.pane_launcher.expanduser()
    if shared_switchyard_release_for_path(launcher) is not None:
        return None
    for parent in launcher.parents:
        if parent.name == "current":
            return parent.parent
    return None


def _tenant_board_root_from_config_or_plan(config: ProjectConfig, config_path: Path | None = None) -> Path | None:
    if config_path is not None:
        plan_data = _plan_data_from_config(config, config_path)
        board_root = plan_data.get("board_root")
        if board_root:
            return Path(str(board_root)).expanduser()
    return _tenant_board_root_from_config(config)


def _read_deploy_sha_marker(path: Path) -> str:
    try:
        return (path / ".pgu-deploy-sha").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_tenant_release(board_root: Path) -> tuple[Path | None, str]:
    current_link = board_root / "current"
    if not current_link.exists() and not current_link.is_symlink():
        return None, ""
    current_release = current_link.resolve(strict=False)
    current_sha = _read_deploy_sha_marker(current_link)
    if not current_sha:
        current_sha = _read_deploy_sha_marker(current_release)
    if not current_sha and current_release.name:
        current_sha = current_release.name
    return current_release, current_sha


def _deploy_ref_remote_branch(deploy_ref: str) -> tuple[str, str] | None:
    if deploy_ref.startswith("refs/") or "/" not in deploy_ref:
        return None
    remote, branch = deploy_ref.split("/", 1)
    if not remote or not branch:
        return None
    return remote, branch


def git_deploy_ref_ls_remote_args(source_repo: Path, deploy_ref: str) -> list[str]:
    remote_branch = _deploy_ref_remote_branch(deploy_ref)
    if remote_branch is None:
        raise ValueError(f"deploy ref does not name a remote branch: {deploy_ref}")
    remote, branch = remote_branch
    return ["git", "-C", str(source_repo), "ls-remote", remote, f"refs/heads/{branch}"]


def git_deploy_ref_rev_parse_args(source_repo: Path, deploy_ref: str) -> list[str]:
    return ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{deploy_ref}^{{commit}}"]


def _resolve_deploy_ref_readonly(
    source_repo: Path,
    deploy_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, str]:
    if _deploy_ref_remote_branch(deploy_ref) is not None:
        remote_proc = run_owner_correct_git(
            git_deploy_ref_ls_remote_args(source_repo, deploy_ref),
            runner=runner,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if remote_proc.returncode == 0:
            remote_sha = _parse_ls_remote_head(str(remote_proc.stdout or ""))
            if remote_sha:
                return remote_sha, ""
        else:
            return "", _proc_failure_reason(remote_proc, f"git ls-remote exited {remote_proc.returncode}")
    rev_parse_proc = run_owner_correct_git(
        git_deploy_ref_rev_parse_args(source_repo, deploy_ref),
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if rev_parse_proc.returncode != 0:
        return "", _proc_failure_reason(rev_parse_proc, f"git rev-parse exited {rev_parse_proc.returncode}")
    return str(rev_parse_proc.stdout or "").strip(), ""


def _resolve_deploy_ref_from_bare_repo(
    bare_repo: Path,
    deploy_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, str]:
    remote_branch = _deploy_ref_remote_branch(deploy_ref)
    refs = (
        (f"refs/remotes/{remote_branch[0]}/{remote_branch[1]}",)
        if remote_branch is not None
        else (deploy_ref,)
    )
    errors: list[str] = []
    for ref in refs:
        proc = runner(
            ["git", f"--git-dir={bare_repo}", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode == 0:
            return str(proc.stdout or "").strip(), ""
        errors.append(_proc_failure_reason(proc, f"git rev-parse exited {proc.returncode}"))
    return "", "; ".join(errors)


def tenant_release_status(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> TenantReleaseStatus | None:
    board_root = _tenant_board_root_from_config_or_plan(config, config_path)
    if board_root is None:
        return None
    plan_data = _plan_data_from_config(config, config_path) if config_path is not None else {}
    owner_user = str(plan_data.get("owner_user") or config.run_as_user or current_user_name()).strip()
    if not owner_user:
        raise SystemExit(f"switchyard: cannot determine tenant owner for {config.project}")
    raw_owner_home = str(plan_data.get("owner_home") or "").strip()
    if raw_owner_home:
        owner_home = Path(raw_owner_home).expanduser()
    elif board_root.name == f"{config.project}-ticketboard-live":
        owner_home = board_root.parent
    else:
        raise SystemExit(
            f"switchyard: provision plan for {config.project} is missing owner_home and it cannot be "
            f"derived from board_root {board_root}"
        )
    if not owner_home.is_absolute():
        raise SystemExit(f"switchyard: tenant owner_home must be absolute: {owner_home}")
    provisioned_system_unit = None
    if config_path is not None:
        candidate = (config_path.parent / f"{config.project}-ticket-board.service").resolve(strict=False)
        required_units = (
            candidate,
            candidate.parent / f"{config.project}-ticket-board-canary.service",
            candidate.parent / f"{config.project}-ticket-board-notify-listener.service",
        )
        if all(path.is_file() for path in required_units):
            provisioned_system_unit = candidate
    selected_commit_git_dir = (
        commit_git_dir.strip()
        if commit_git_dir is not None
        else str(plan_data.get("commit_git_dir") or "").strip()
    )
    resolved_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    current_release, current_sha = _current_tenant_release(board_root)
    clone_source_repo: Path | None = None
    if shared_switchyard_release_for_path(resolved_source_repo) is not None:
        selected_cache = switchyard_bare_repo()
        if selected_cache is None:
            target_sha = ""
            resolve_error = (
                "installed shared releases require an explicit source cache in SWITCHYARD_BARE_REPO "
                "or an explicit --source-repo checkout"
            )
        else:
            clone_source_repo = selected_cache.resolve(strict=False)
            target_sha, resolve_error = _resolve_deploy_ref_from_bare_repo(clone_source_repo, deploy_ref, runner=runner)
    else:
        target_sha, resolve_error = _resolve_deploy_ref_readonly(resolved_source_repo, deploy_ref, runner=runner)
    return TenantReleaseStatus(
        board_root=board_root,
        owner_user=owner_user,
        owner_home=owner_home,
        provisioned_system_unit=provisioned_system_unit,
        commit_git_dir=selected_commit_git_dir,
        current_release=current_release,
        current_sha=current_sha,
        target_sha=target_sha,
        deploy_ref=deploy_ref,
        source_repo=resolved_source_repo,
        resolve_error=resolve_error,
        clone_source_repo=clone_source_repo,
    )


def tenant_release_deploy_command(status: TenantReleaseStatus, project: str) -> str:
    deploy_env = [
        f"TICKET_BOARD_OWNER_HOME={status.owner_home}",
        f"TICKET_BOARD_PROJECT={project}",
        f"BOARD_ROOT={status.board_root}",
        f"DEPLOY_REF={status.deploy_ref}",
    ]
    if status.provisioned_system_unit is not None:
        deploy_env.append(f"TICKET_BOARD_PROVISIONED_SYSTEM_UNIT={status.provisioned_system_unit}")
    if status.commit_git_dir:
        deploy_env.append(f"TICKET_BOARD_COMMIT_GIT_DIR={status.commit_git_dir}")
    if status.clone_source_repo is not None:
        marker = json.dumps({"commit": status.target_sha}, separators=(",", ":"))
        script = (
            'tmpdir="$(mktemp -d)"'
            f" && git --git-dir={shlex.quote(str(status.clone_source_repo))} archive {shlex.quote(status.target_sha)}"
            ' | tar -x -C "$tmpdir"'
            f" && printf '%s\\n' {shlex.quote(marker)} >\"$tmpdir/{SWITCHYARD_RELEASE_MARKER_NAME}\""
            " && env "
            + " ".join(shlex.quote(value) for value in deploy_env)
            + ' SOURCE_REPO="$tmpdir" "$tmpdir/scripts/ticket-board-service.sh" deploy-restart'
        )
        return _quote_command(_owner_command_env_args(status.owner_user, status.owner_home, ["sh", "-c", script]))
    service_script = status.source_repo / "scripts" / "ticket-board-service.sh"
    return _quote_command(
        _owner_command_env_args(
            status.owner_user,
            status.owner_home,
            ["env", *deploy_env, f"SOURCE_REPO={status.source_repo}", str(service_script), "deploy-restart"],
        )
    )


def tenant_release_listener_command(status: TenantReleaseStatus, project: str, action: str) -> str:
    if action not in {"stop", "start"}:
        raise ValueError(f"unsupported listener action: {action}")
    listener_unit = f"{project}-ticket-board-notify-listener.service"
    operation = f"systemctl --user {action} {shlex.quote(listener_unit)}"
    if action == "start":
        operation = f"systemctl --user daemon-reload && {operation}"
    script = (
        'runtime="/run/user/$(id -u)"; '
        'XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" '
        + operation
    )
    command = _owner_command_env_args(
        status.owner_user,
        status.owner_home,
        ["sh", "-c", script],
    )
    return _quote_command(command)


def tenant_release_unit_install_command(status: TenantReleaseStatus, project: str) -> str:
    if status.provisioned_system_unit is None:
        return ""
    provision_dir = status.provisioned_system_unit.parent
    board_unit = status.provisioned_system_unit
    canary_unit = provision_dir / f"{project}-ticket-board-canary.service"
    listener_unit = provision_dir / f"{project}-ticket-board-notify-listener.service"
    listener_target = status.owner_home / ".config" / "systemd" / "user" / listener_unit.name
    return " && ".join(
        [
            _quote_command(["sudo", "install", "-m", "0644", str(board_unit), f"/etc/systemd/system/{board_unit.name}"]),
            _quote_command(["sudo", "install", "-m", "0644", str(canary_unit), f"/etc/systemd/system/{canary_unit.name}"]),
            _quote_command(
                [
                    "sudo",
                    "install",
                    "-m",
                    "0644",
                    "-o",
                    status.owner_user,
                    "-g",
                    status.owner_user,
                    str(listener_unit),
                    str(listener_target),
                ]
            ),
            _quote_command(["sudo", "systemctl", "daemon-reload"]),
        ]
    )


def _format_release_sha(sha: str) -> str:
    return sha if sha else "(none)"


def _format_release_path(path: Path | None) -> str:
    return str(path) if path is not None else "(none)"


def report_tenant_release_upgrade(
    config: ProjectConfig,
    *,
    config_path: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> None:
    status = tenant_release_status(
        config,
        config_path=config_path,
        source_repo=source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
    )
    if status is None:
        return
    print_func(
        f"switchyard: {config.project} deployed board release old: "
        f"{_format_release_sha(status.current_sha)} at {_format_release_path(status.current_release)}"
    )
    if status.target_sha:
        print_func(f"switchyard: {config.project} deployed board release new: {status.target_sha} from {status.deploy_ref}")
    else:
        print_func(
            f"switchyard: {config.project} deployed board release new: "
            f"(unresolved {status.deploy_ref}: {status.resolve_error})"
        )
    if not status.target_sha:
        print_func(
            f"switchyard: cannot produce a safe release update for {config.project}: "
            f"{status.resolve_error}"
        )
    elif status.unchanged:
        print_func(f"switchyard: {config.project} deployed board release unchanged; no release deploy needed")
    else:
        if status.provisioned_system_unit is None:
            print_func(
                f"switchyard: cannot produce a safe release update for {config.project}: generated board, "
                "canary, and listener units are incomplete"
            )
            return
        print_func("switchyard: matching-release deployment sequence (keep the listener stopped through migrations):")
        print_func(f"  {tenant_release_listener_command(status, config.project, 'stop')}")
        unit_install = tenant_release_unit_install_command(status, config.project)
        if unit_install:
            print_func(f"  {unit_install}")
        print_func(f"  {tenant_release_deploy_command(status, config.project)}")
        print_func(f"  {tenant_release_listener_command(status, config.project, 'start')}")
        print_func(
            "switchyard: panes must be restarted after the release update to pick up hook installer, "
            "hook binary, or pane launcher changes; this command does not restart panes"
        )


def sync_reload_config_to_live_sessions(
    config: ProjectConfig,
    *,
    config_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> ProjectConfig:
    raw_config = _load_json(config_path)
    raw_roles = raw_config.get("roles")
    if not isinstance(raw_roles, list):
        return config
    role_by_name = {role.role: role for role in config.roles}
    updated_roles: list[RoleConfig] = []
    changed = False
    for raw_role in raw_roles:
        if not isinstance(raw_role, dict):
            continue
        role_name = str(raw_role.get("role") or "").strip()
        role = role_by_name.get(role_name)
        if role is None:
            continue
        # A role's live session is in that role's own tmux server, so reload has
        # to inspect it there or it sees nothing and rewrites the config from a
        # blank reading (SYRD-39).
        role_runner = role_process_runner_for(config, role, runner=runner)
        if role_runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            updated_roles.append(role)
            continue
        live_model = live_model_for_role(role, session_dir=role_session_dir(config, role), runner=role_runner)
        if not live_model:
            updated_roles.append(role)
            continue
        live_cli = live_cli_for_role(role, runner=role_runner)
        next_role = role
        if live_cli and live_cli != role.cli:
            raw_role["cli"] = live_cli
            if not set(live_cli) <= set(role.live_commands):
                raw_role["live_commands"] = live_cli
                next_role = replace(next_role, cli=live_cli, live_commands=live_cli)
            else:
                next_role = replace(next_role, cli=live_cli)
            changed = True
        if live_model != role.model:
            raw_role["model"] = live_model
            next_role = replace(next_role, model=live_model)
            changed = True
        updated_roles.append(next_role)
    if not changed:
        return config
    _write_json_atomic(config_path, raw_config)
    return replace(config, roles=updated_roles)


DESKTOP_ENV_KEYS = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def prepare_project_desktop(config: ProjectConfig, *, install: bool = False,
                            helper: Path | None = None,
                            runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> ProjectConfig:
    from scripts import desktop_access as desktop
    tenant = config.run_as_user or current_user_name()
    try:
        policy = desktop.validate_policy(config.desktop_access, project=config.project, tenant=tenant)
        env: dict[str, str] = {}
        if policy["mode"] == "wayland":
            helper = helper or Path(__file__).resolve().with_name("desktop_access.py")
            if install:
                desktop.install(policy, helper=helper)
            # Invoke the verifier with the tenant's actual uid, never a GUI runtime
            # masquerading as the tenant. The protected GUI receipt is authoritative.
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as stream:
                json.dump(policy, stream)
                stream.flush()
                os.chmod(stream.name, 0o644)
                args = [sys.executable, str(helper), "verify", stream.name]
                result = runner(_owner_command_args(tenant, args), text=True, capture_output=True)
            if result.returncode:
                raise desktop.DesktopAccessError(str(result.stderr).strip())
            env = json.loads(result.stdout)
        roles = [replace(role,
                         env={**{k:v for k,v in role.env.items() if k not in DESKTOP_ENV_KEYS}, **env},
                         unset_env=DESKTOP_ENV_KEYS) for role in config.roles]
        return replace(config, desktop_access=policy, roles=roles)
    except (desktop.DesktopAccessError, OSError, ValueError) as exc:
        raise SystemExit(f"switchyard: desktop setup incomplete before launch: {exc}") from exc


def configure_project_desktop(config: ProjectConfig, *, config_path: Path,
                              policy_path: Path | None = None, dry_run: bool = False,
                              helper: Path | None = None,
                              runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run) -> ProjectConfig:
    from scripts import desktop_access as desktop
    raw = ({"mode": "headless"} if str(policy_path) == "headless" else _load_json(policy_path)) if policy_path else config.desktop_access
    try:
        policy = desktop.validate_policy(raw, project=config.project, tenant=config.run_as_user or current_user_name())
    except desktop.DesktopAccessError as exc:
        raise SystemExit(f"switchyard: {exc}") from exc
    if dry_run:
        print(f"switchyard: desktop policy for {config.project}: {json.dumps(policy, sort_keys=True)}; no access changed")
        return config
    # Complete privileged setup and tenant readiness before persisting the launch
    # choice; failures leave the previous project config intact.
    previous = (desktop.validate_policy(config.desktop_access, project=config.project, tenant=config.run_as_user or current_user_name())
                if config.desktop_access is not None else None)
    if previous and previous.get("mode") == "wayland" and previous != policy:
        desktop.uninstall(previous)
    installed_new = False
    try:
        if policy["mode"] == "wayland":
            desktop.install(policy, helper=helper or Path(__file__).resolve().with_name("desktop_access.py"))
            installed_new = previous != policy
        configured = prepare_project_desktop(replace(config, desktop_access=policy), runner=runner)
        payload = _load_json(config_path)
        payload["desktop_access"] = policy
        owner_user = config.run_as_user or current_user_name()
        _write_json_atomic(config_path, payload, owner_user=owner_user)
        _write_json_atomic(config_path.parent / "desktop-policy.json", policy, owner_user=owner_user)
        return configured
    except (Exception, SystemExit):
        if installed_new:
            desktop.uninstall(policy)
        if previous and previous.get("mode") == "wayland" and previous != policy:
            desktop.install(previous, helper=helper or Path(__file__).resolve().with_name("desktop_access.py"))
        raise


def launch_project(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    dry_run: bool = False,
    layout_output: Path | None = None,
    assign_layout_owner: bool | None = None,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
    allow_stale_launcher: bool = False,
    no_launcher_self_deploy: bool = False,
    report_session_records: bool = False,
    session_record_timeout: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    session_record_poll: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    layout_mode: str = LAYOUT_MODE_AUTO,
    layout_environ: dict[str, str] | None = None,
    konsole_process_launcher: Callable[..., Any] | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    if mode == "start":
        mode = "attach-or-start"
    if mode not in {"attach", "attach-or-start", "reload"}:
        raise SystemExit(f"unknown launch mode: {mode}")
    if mode == "reload" and not dry_run:
        # Reload re-projects the stored document, so an unmigrated tenant would reload
        # into the same missing director onboarding forever. Run the one-time backfill
        # first; it is idempotent and marked, so a migrated tenant pays nothing.
        if migrate_declarative_director_onboarding(
            config, config_path=config_path, print_func=print_func
        ):
            # The migration rewrote the projection on disk, so the config loaded before
            # it is now stale. Every later path -- session sync, detached restarts, the
            # viewer -- reads role env off this object, and would otherwise restart roles
            # without the prompt that was just projected.
            config = load_project_config(config.project, config_path)
    worktree_runner = runner
    # Owner-scoped work (worktrees, the viewer session, layout) uses this.
    # Anything that starts, probes or stops a ROLE selects that role's own
    # account instead, because bin_user only sets PATH and the uid the board
    # sees comes from the runner (SYRD-39).
    role_process_runner = runner
    owner_runner_anchor = config.repository or config.pane_launcher
    delegate_role_sessions_to_owner = bool(config.run_as_user and current_user_name() != config.run_as_user)
    if delegate_role_sessions_to_owner:
        role_process_runner = _owner_process_runner(owner_user=config.run_as_user, runner=runner)
        if owner_runner_anchor is not None:
            worktree_runner = _owner_project_git_runner(
                owner_user=config.run_as_user,
                project_dir=owner_runner_anchor,
                owned_roots=_control_repository_owned_roots(config),
                runner=runner,
            )
    effective_pane_state_dir = pane_state_dir or default_pane_state_dir_for_user(config.run_as_user, project=config.project)
    output_path = layout_output or default_layout_output_path(config, config_path=config_path)
    window_title = project_window_title(config)
    should_assign_layout_owner = layout_output is None if assign_layout_owner is None else assign_layout_owner
    pane_script_path = config.pane_launcher or script_path
    if not dry_run:
        upgrade_result = upgrade_generated_project_layout(config, config_path=config_path, runner=runner)
        if upgrade_result.changed:
            print_func(upgrade_result.message)
            config = load_project_config(config.project, config_path)
    if not dry_run and mode != "attach":
        config = prepare_project_desktop(config, runner=runner)
    if not dry_run:
        pane_script_path = _verify_pane_launcher_path(config, script_path=script_path, runner=worktree_runner)
    failed_roles: dict[str, str] = {}
    running_roles: list[RoleConfig] = []
    if not dry_run:
        ensure_launcher_checkout_current(
            config,
            runner=worktree_runner,
            auto_deploy=not (
                no_launcher_self_deploy
                or _env_truthy_any(NO_LAUNCHER_SELF_DEPLOY_ENV, LEGACY_NO_LAUNCHER_SELF_DEPLOY_ENV)
            ),
            allow_stale=allow_stale_launcher,
        )
        if mode == "attach-or-start":
            running_roles = _running_project_roles(config, runner=runner)
        ensure_configured_runtime_user(config, runner=runner)
        ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        ensure_generated_project_pane_hooks(
            config,
            config_path=config_path,
            script_path=pane_script_path,
            pane_state_dir=effective_pane_state_dir,
            runner=runner,
        )
        ensure_generated_project_board_skill(
            config,
            config_path=config_path,
            script_path=pane_script_path,
            runner=runner,
            print_func=print_func,
        )
        seed_default_session_dir_from_legacy_sources(config.session_dir)
        if mode == "attach-or-start":
            worktree_roles = (
                [role for role in config.roles if role.role not in {running.role for running in running_roles}]
                if running_roles and config.control_repository is not None
                else config.roles
            )
            failed_roles = _prepare_project_worktrees_for_launch(
                config,
                running_roles=running_roles,
                runner=worktree_runner,
            ).failed_roles
            if worktree_roles and config.control_repository is not None and set(failed_roles) == {role.role for role in worktree_roles}:
                reason = next(iter(failed_roles.values()), "unknown error")
                print(f"team-launcher: failed to prepare control repository for {config.project}: {reason}", file=sys.stderr)
                return 1
        elif mode == "reload":
            fetch_project_worktree_ref(config, runner=worktree_runner)
            config = sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
            config = prepare_project_desktop(config, runner=runner)
        # Worktrees are created here, after the operator artifact has already
        # run, so a fresh project reaches this point with trees still owned by
        # the project owner. Say so every launch until the handoff is done,
        # rather than starting roles that cannot write their own trees.
        isolation_gaps = role_isolation_gaps(config)
        if isolation_gaps:
            handoff_path = config_path.with_name(f"{config.project}-role-accounts.sh")
            try:
                handoff_path.write_text(render_role_account_migration(config), encoding="utf-8")
                handoff_path.chmod(0o755)
            except OSError:
                pass
            # Refuse rather than warn. A partially migrated project that starts
            # anyway runs roles under the wrong uid or without credentials, and
            # the board then either denies them or -- worse, before this was
            # closed -- cannot tell them apart at all (SYRD-39).
            print_func(
                "team-launcher: refusing to launch " + config.project
                + "; its roles are not isolated yet, so they cannot be told apart:\n  "
                + "\n  ".join(isolation_gaps)
                + f"\nAn operator must run {handoff_path} (safe to re-run) and then relaunch."
            )
            return 1
    materialize_layout(
        config,
        config_path=config_path,
        mode=mode,
        script_path=pane_script_path,
        output_path=output_path,
        pane_state_dir=pane_state_dir,
        force_reload=force_reload,
        failed_roles=failed_roles,
    )
    if should_assign_layout_owner:
        ensure_layout_output_owner(config, output_path, runner=runner)
    plan = {
        "project": config.project,
        "window_title": window_title,
        "mode": mode,
        "layout": str(output_path),
        "run_as_user": config.run_as_user or current_user_name(),
        "worktree_ref": worktree_ref(config) if config.repository is not None else None,
        "roles": [
            {
                "role": role.role,
                "slot": role.slot,
                "tmux_session": role.tmux_session,
                "target": role.target,
                "workdir": str(Path.home()) if role.role in failed_roles else role.workdir,
                "command": (
                    failed_role_command(role, failed_roles[role.role])
                    if role.role in failed_roles
                    else pane_command(
                        config.project,
                        role,
                        config_path=config_path,
                        mode=mode,
                        script_path=pane_script_path,
                        pane_state_dir=pane_state_dir,
                        force_reload=force_reload,
                        skip_launcher_check=True,
                        run_as_user=role_run_as_user(config, role),
                    )
                ),
                "worktree_error": failed_roles.get(role.role, ""),
            }
            for role in config.roles
            if not role.detached
        ],
        "detached_roles": [
            {
                "role": role.role,
                "tmux_session": role.tmux_session,
                "target": role.target,
                "workdir": role.workdir,
            }
            for role in config.roles
            if role.detached
        ],
    }
    if dry_run:
        resolved_layout_mode = (
            resolve_layout_mode(layout_mode, environ=layout_environ, runner=runner)
            if layout_mode != LAYOUT_MODE_AUTO or layout_environ is not None
            else LAYOUT_MODE_SEPARATE
        )
        if resolved_layout_mode == LAYOUT_MODE_VIEWER:
            plan["layout_mode"] = LAYOUT_MODE_VIEWER
            plan["viewer_session"] = viewer_session_for_project(config.project)
            plan["viewer_roles"] = [role.role for role in visible_roles_for_viewer(config)]
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    from scripts import presentation_controller

    use_runtime_presentation = presentation_controller.presentation_enabled(config, config_path=config_path)
    worker_start_exit_code = 0

    def record_worker_start_failure(role: RoleConfig, result: int) -> None:
        nonlocal worker_start_exit_code
        worker_start_exit_code = worker_start_exit_code or int(result)
        reason = f"pane start failed with exit {result}"
        failed_roles[role.role] = reason
        print(f"team-launcher: {reason} for {role.role}; leaving presentation recovery status", file=sys.stderr)

    launch_started_at = time.time()
    launch_started_ns = time.time_ns()
    for role in config.roles:
        if not role.detached:
            continue
        if role.role in failed_roles:
            print(f"skipping detached role {role.role}: {failed_roles[role.role]}", file=sys.stderr)
            continue
        if delegate_role_sessions_to_owner:
            result = runner(
                pane_command_args(
                    config.project,
                    role,
                    config_path=config_path,
                    mode=mode,
                    script_path=pane_script_path,
                    pane_state_dir=role_pane_state_dir(config, role, effective_pane_state_dir),
                    force_reload=force_reload,
                    skip_launcher_check=True,
                    allow_stale_launcher=allow_stale_launcher,
                    run_as_user=role_run_as_user(config, role),
                )
            ).returncode
        else:
            result = run_detached_role(
                role,
                mode=mode,
                session_dir=role_session_dir(config, role),
                pane_state_dir=role_pane_state_dir(config, role, effective_pane_state_dir),
                force_reload=force_reload,
                bin_user=role_run_as_user(config, role),
                runner=role_process_runner_for(config, role, runner=runner),
            )
        if result != 0:
            if not use_runtime_presentation:
                return result
            record_worker_start_failure(role, result)
    resolved_layout_mode = resolve_layout_mode(layout_mode, environ=layout_environ, runner=runner)
    if resolved_layout_mode == LAYOUT_MODE_VIEWER:
        viewer_roles = visible_roles_for_viewer(config)
        for role in viewer_roles:
            if role.role in failed_roles:
                print(f"skipping visible role {role.role}: {failed_roles[role.role]}", file=sys.stderr)
                continue
            if delegate_role_sessions_to_owner:
                result = runner(
                    pane_command_args(
                        config.project,
                        role,
                        config_path=config_path,
                        mode=mode,
                        script_path=pane_script_path,
                        pane_state_dir=role_pane_state_dir(config, role, effective_pane_state_dir),
                        force_reload=force_reload,
                        skip_launcher_check=True,
                        allow_stale_launcher=allow_stale_launcher,
                        no_attach=True,
                        run_as_user=role_run_as_user(config, role),
                    )
                ).returncode
            else:
                result = ensure_visible_role_session_for_viewer(
                    role,
                    mode=mode,
                    session_dir=role_session_dir(config, role),
                    pane_state_dir=role_pane_state_dir(config, role, effective_pane_state_dir),
                    force_reload=force_reload,
                    bin_user=role_run_as_user(config, role),
                    runner=role_process_runner_for(config, role, runner=runner),
                )
            if result != 0:
                if not use_runtime_presentation:
                    return result
                record_worker_start_failure(role, result)
        ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        launchable_viewer_roles = [role for role in viewer_roles if role.role not in failed_roles]
        if use_runtime_presentation:
            launch_result = presentation_controller.launch_presentation(
                config,
                config_path=config_path,
                layout=LAYOUT_MODE_VIEWER,
                state_path=output_path.with_name("presentation.json") if layout_output is not None else None,
                runner=runner,
            )
        elif launchable_viewer_roles:
            launch_result = launch_tmux_viewer_session(
                launchable_viewer_roles,
                viewer_session=viewer_session_for_project(config.project),
                window_title=window_title,
                runner=role_process_runner,
            )
        elif viewer_roles:
            launch_result = 1
        else:
            launch_result = 0
    else:
        ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        if use_runtime_presentation:
            for role in visible_roles_for_viewer(config):
                if role.role in failed_roles:
                    continue
                if delegate_role_sessions_to_owner:
                    result = runner(
                        pane_command_args(
                            config.project,
                            role,
                            config_path=config_path,
                            mode=mode,
                            script_path=pane_script_path,
                            pane_state_dir=role_pane_state_dir(config, role, effective_pane_state_dir),
                            force_reload=force_reload,
                            skip_launcher_check=True,
                            allow_stale_launcher=allow_stale_launcher,
                            no_attach=True,
                            run_as_user=role_run_as_user(config, role),
                        )
                    ).returncode
                else:
                    result = ensure_visible_role_session_for_viewer(
                        role,
                        mode=mode,
                        session_dir=role_session_dir(config, role),
                        pane_state_dir=role_pane_state_dir(config, role, effective_pane_state_dir),
                        force_reload=force_reload,
                        bin_user=role_run_as_user(config, role),
                        runner=role_process_runner_for(config, role, runner=runner),
                    )
                if result != 0:
                    record_worker_start_failure(role, result)
            launch_result = presentation_controller.launch_presentation(
                config,
                config_path=config_path,
                layout=LAYOUT_MODE_SEPARATE,
                state_path=output_path.with_name("presentation.json") if layout_output is not None else None,
                runner=runner,
                process_launcher=konsole_process_launcher,
            )
        else:
            launch_result = launch_konsole_window(
                output_path,
                project=config.project,
                window_title=window_title,
                runner=runner,
                process_launcher=konsole_process_launcher,
            )
    if launch_result != 0:
        return launch_result
    if mode == "attach-or-start" and resolved_layout_mode != LAYOUT_MODE_VIEWER:
        attached_visible_roles = [role for role in running_roles if not role.detached and role.role not in failed_roles]
        if attached_visible_roles:
            attached_names = ", ".join(role.role for role in attached_visible_roles)
            plural = "pane" if len(attached_visible_roles) == 1 else "panes"
            print_func(
                f"switchyard: opened a new window attached to running {plural}: {attached_names}; "
                "the previous window may be closed if no longer needed"
            )
    if report_session_records:
        report_launch_session_records(
            config,
            timeout_seconds=session_record_timeout,
            poll_seconds=session_record_poll,
            fallback_changed_since_ns=launch_started_ns,
            pane_state_dir=effective_pane_state_dir,
            pane_state_updated_since=launch_started_at,
            attached_roles=running_roles if mode == "attach-or-start" else (),
            print_func=print_func,
        )
    return worker_start_exit_code


def _default_new_project_owner(project: str) -> str:
    return f"{project}-agent"


def _read_prompt(
    prompt: str,
    *,
    input_func: Callable[[str], str] = input,
) -> str:
    try:
        return input_func(prompt)
    except EOFError:
        raise SystemExit("switchyard: no input available") from None


def _prompt_text(
    label: str,
    *,
    default: str = "",
    input_func: Callable[[str], str] = input,
) -> str:
    suffix = f" [{default}]" if default else ""
    value = _read_prompt(f"{label}{suffix}: ", input_func=input_func).strip()
    return value or default


def _prompt_bool(
    label: str,
    *,
    default: bool,
    input_func: Callable[[str], str] = input,
) -> bool:
    default_text = "Y/n" if default else "y/N"
    for _attempt in range(SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw = _read_prompt(f"{label} [{default_text}]: ", input_func=input_func).strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print("answer yes or no")
    raise SystemExit(f"switchyard: too many invalid answers for {label}")


def _prompt_cli(
    role: str,
    *,
    default: str,
    input_func: Callable[[str], str] = input,
) -> str:
    default_cli = _validate_new_project_cli(default, context=f"default CLI for {role}")
    choices = "/".join(SUPPORTED_NEW_PROJECT_CLIS)
    for _attempt in range(SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw = _prompt_text(f"{role} CLI ({choices})", default=default_cli, input_func=input_func)
        try:
            return _validate_new_project_cli(raw, context=f"CLI for {role}")
        except SystemExit as exc:
            print(exc)
    raise SystemExit(f"switchyard: too many invalid answers for {role} CLI")


def _prompt_switchyard_role_choices(
    *,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> tuple[tuple[str, str], ...]:
    include_designer = _prompt_bool("Include designer role", default=True, input_func=input_func)
    include_audit = _prompt_bool("Include audit role", default=True, input_func=input_func)
    role_clis: list[tuple[str, str]] = []
    if include_designer:
        role_clis.append(
            ("designer", _prompt_cli("designer", default=NEW_PROJECT_ROLE_CLI_DEFAULTS["designer"], input_func=input_func))
        )
    role_clis.append(("director", _prompt_cli("director", default=NEW_PROJECT_ROLE_CLI_DEFAULTS["director"], input_func=input_func)))
    if include_audit:
        role_clis.append(("audit", _prompt_cli("audit", default=NEW_PROJECT_ROLE_CLI_DEFAULTS["audit"], input_func=input_func)))
    print_func("Conventional implementer roles:")
    for role, description in NEW_PROJECT_CONVENTIONAL_IMPLEMENTER_ROLES:
        print_func(f"  {role}: {description}")
    for _attempt in range(SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw_roles = _prompt_text(
            "Implementer roles (comma-separated)",
            default=", ".join(NEW_PROJECT_DEFAULT_IMPLEMENTER_ROLES),
            input_func=input_func,
        )
        roles: list[str] = []
        try:
            for item in _comma_list(raw_roles):
                role = _validate_new_project_implementer_role(item, context="implementer role")
                if role not in roles:
                    roles.append(role)
            if not roles:
                raise SystemExit("at least one implementer role is required")
            break
        except SystemExit as exc:
            print(exc)
    else:
        raise SystemExit("switchyard: too many invalid answers for implementer roles")
    for role in roles:
        role_clis.append((role, _prompt_cli(role, default="codex", input_func=input_func)))
    return tuple(role_clis)


def _comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_project_artifact_path(project: str, output_dir: Path) -> Path:
    return output_dir / f"{project}.project.json"


def _default_project_design_document_path(project: str, output_dir: Path) -> Path:
    return output_dir / f"{project}-design.md"


def _project_design_markdown(project: str, *, title: str, body: str) -> str:
    heading = title.strip() or f"{project} design"
    body_text = body.strip() or "TBD."
    return f"# {heading}\n\n{body_text}\n"


def design_project_command(
    project: str,
    *,
    output_dir: Path | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    project_name: str | None = None,
    design_title: str | None = None,
    design_body: str | None = None,
    repository: Path | None = None,
    remote: str | None = None,
    default_branch: str | None = None,
    worktree_policy: str | None = None,
    owner_user: str | None = None,
    ticket_prefix: str | None = None,
    implementer_roles: Sequence[str] | None = None,
    push_policy: str | None = None,
    audit_signoff: bool | None = None,
    audit_roles: Sequence[str] | None = None,
    needs_inspection: bool | None = None,
    needs_user_signoff: bool | None = None,
    board_service_traversal: bool | None = None,
    supplementary_groups: Sequence[str] | None = None,
    linger: bool | None = None,
    owner_shell: str | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> int:
    project_slug = _validate_project_slug(project)
    base_dir = (output_dir or Path.cwd()).expanduser().resolve(strict=False)
    target_artifact = (artifact_path or _default_project_artifact_path(project_slug, base_dir)).expanduser().resolve(strict=False)
    target_design_document = (
        design_document or _default_project_design_document_path(project_slug, target_artifact.parent)
    ).expanduser().resolve(strict=False)

    title = design_title if design_title is not None else _prompt_text("Design document title", default=f"{project_slug} design", input_func=input_func)
    resolved_project_name = (project_name or title or project_slug).strip()
    body = design_body if design_body is not None else _prompt_text("Design summary", input_func=input_func)
    repo = repository or Path(_prompt_text("Code location", input_func=input_func))
    resolved_remote = remote or _prompt_text("Remote", default="origin", input_func=input_func)
    resolved_branch = default_branch or _prompt_text("Default branch", default="main", input_func=input_func)
    resolved_policy = worktree_policy or _prompt_text("Worktree policy (shared/isolated)", default="shared", input_func=input_func)
    if resolved_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"worktree policy must be one of {sorted(WORKTREE_POLICIES)}")
    resolved_owner = _owner_user_verbatim(
        owner_user if owner_user is not None else _prompt_text("Owner user", default=_default_new_project_owner(project_slug), input_func=input_func)
    )
    resolved_prefix = validate_ticket_prefix(
        ticket_prefix or _prompt_text("Ticket prefix", default=project_slug.upper(), input_func=input_func)
    )
    resolved_push_policy = push_policy or _prompt_text("Push policy", default="director-main-only", input_func=input_func)
    resolved_audit_roles = _dedupe_role_names(
        tuple(_validate_new_project_audit_role(role) for role in (audit_roles or ("audit",)))
    )
    resolved_implementer_roles = tuple(implementer_roles or DEFAULT_PROJECT_IMPLEMENTER_ROLES)
    role_overlap = set(resolved_implementer_roles) & set(resolved_audit_roles)
    if role_overlap:
        raise SystemExit(f"roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}")
    gates = {
        "audit_signoff": audit_signoff
        if audit_signoff is not None
        else _prompt_bool("Require audit signoff gate", default=PROJECT_DESIGN_DEFAULT_GATES["audit_signoff"], input_func=input_func),
        "needs_inspection": needs_inspection
        if needs_inspection is not None
        else _prompt_bool("Enable inspection gate by default", default=PROJECT_DESIGN_DEFAULT_GATES["needs_inspection"], input_func=input_func),
        "needs_user_signoff": needs_user_signoff
        if needs_user_signoff is not None
        else _prompt_bool("Enable user signoff gate by default", default=PROJECT_DESIGN_DEFAULT_GATES["needs_user_signoff"], input_func=input_func),
    }
    grants = {
        "board_service_traversal": board_service_traversal
        if board_service_traversal is not None
        else _prompt_bool("Grant board-service traversal into the owner home", default=True, input_func=input_func),
        "supplementary_groups": list(supplementary_groups)
        if supplementary_groups is not None
        else _comma_list(_prompt_text("Supplementary groups (comma-separated, blank for none)", input_func=input_func)),
        "linger": linger
        if linger is not None
        else _prompt_bool("Enable linger for the owner user", default=True, input_func=input_func),
        "shell": owner_shell or _prompt_text("Owner shell", default="fish", input_func=input_func),
    }
    artifact = ProjectDesignArtifact(
        project=project_slug,
        project_name=resolved_project_name,
        ticket_prefix=resolved_prefix,
        owner_user=resolved_owner,
        repository=repo.expanduser().resolve(strict=False),
        remote=resolved_remote,
        default_branch=resolved_branch,
        worktree_policy=resolved_policy,
        design_document=target_design_document,
        implementer_roles=resolved_implementer_roles,
        audit_roles=resolved_audit_roles,
        role_clis=_default_role_cli_pairs(
            resolved_implementer_roles,
            include_designer=True,
            include_audit=True,
            audit_roles=resolved_audit_roles,
        ),
        include_designer=True,
        include_audit=bool(resolved_audit_roles),
        push_policy=resolved_push_policy,
        gates=gates,
        capability_grants=grants,
    )
    target_design_document.parent.mkdir(parents=True, exist_ok=True)
    target_design_document.write_text(_project_design_markdown(project_slug, title=title, body=body), encoding="utf-8")
    _write_json_atomic(target_artifact, project_design_artifact_payload(artifact))
    print_func(f"team-launcher: wrote design document {target_design_document}")
    print_func(f"team-launcher: wrote project artifact {target_artifact}")
    return 0


def _new_project_artifact_dir(project: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{project}-team-launcher-new."))


def _new_project_session_dir(project: str, owner_user: str) -> str:
    owner_home = home_dir_for_user(owner_user) or Path("/home") / owner_user
    return str(owner_home / ".local" / "state" / f"{project}-ticket-board" / "pane-sessions")


def _new_project_control_repository(project: str, owner_user: str) -> Path:
    return Path("/home") / owner_user / ".local" / "state" / "switchyard" / "projects" / project / "control.git"


def _new_project_worktree_base(project: str, owner_user: str) -> Path:
    return Path("/home") / owner_user / f"{project}-worktrees"


NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES = 3
NEW_PROJECT_GRID_PANES_PER_ROW = 3


def _new_project_layout_leaves(role_count: int) -> list[dict[str, Any]]:
    return [
        {
            "Command": "",
            "SessionRestoreId": index,
            "WorkingDirectory": "",
        }
        for index in range(role_count)
    ]


def _single_row_layout_payload(leaves: list[dict[str, Any]]) -> dict[str, Any]:
    if len(leaves) <= 1:
        return leaves[0] if leaves else {"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}
    return {
        "Orientation": "Horizontal",
        "Widgets": leaves,
    }


def _row_major_grid_layout_payload(leaves: list[dict[str, Any]]) -> dict[str, Any]:
    row_count = math.ceil(len(leaves) / NEW_PROJECT_GRID_PANES_PER_ROW)
    base_row_size, extra = divmod(len(leaves), row_count)
    rows: list[dict[str, Any]] = []
    start = 0
    for row_index in range(row_count):
        row_size = base_row_size + (1 if row_index < extra else 0)
        row = leaves[start : start + row_size]
        start += row_size
        if len(row) == 1:
            rows.append(row[0])
        else:
            rows.append(
                {
                    "Orientation": "Horizontal",
                    "Widgets": row,
                }
            )
    if len(rows) == 1:
        return rows[0]
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


def _new_project_layout_payload(role_count: int) -> dict[str, Any]:
    leaves = _new_project_layout_leaves(role_count)
    if role_count <= NEW_PROJECT_SINGLE_ROW_LAYOUT_MAX_ROLES:
        return _single_row_layout_payload(leaves)
    return _row_major_grid_layout_payload(leaves)


def _new_project_launcher_config_payload(
    plan: ProjectBoardProvision,
    *,
    repository: Path,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
) -> dict[str, Any]:
    layout_name = f"{plan.project}-konsole-layout.json"
    role_defs = _dedupe_role_defs(
        role_clis
        or _default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=audit_roles,
        )
    )
    worktree_base = _new_project_worktree_base(plan.project, plan.owner_user)
    control_repository = _new_project_control_repository(plan.project, plan.owner_user)
    roles = []
    for index, (role, cli) in enumerate(role_defs):
        role_payload: dict[str, Any] = {
            "cli": [cli],
            "env": _new_project_role_env(
                role,
                plan,
                project_name=project_name,
                artifact_path=artifact_path,
                design_document=design_document,
                director_onboarding=director_onboarding,
            ),
            "live_commands": [cli],
            "role": role,
            # One Unix account per role: this is what the board resolves a
            # peer's uid against, and it gives each role its own tmux server.
            "run_as_user": role_account_name(plan.project, role),
            "target": f"{plan.project}-{role}:0.0",
            "tmux_session": f"{plan.project}-{role}",
            "workdir": str(worktree_base / role),
            "yolo": True,
        }
        if index < MAX_VISIBLE_PANES_PER_WINDOW:
            role_payload["slot"] = index
        else:
            role_payload["detached"] = True
        roles.append(role_payload)
    payload = {
        "project": plan.project,
        **({"project_name": project_name} if project_name else {}),
        "ticket_prefix": plan.ticket_prefix,
        "layout": layout_name,
        "repository": str(repository),
        "control_repository": str(control_repository),
        "run_as_user": plan.owner_user,
        "worktree_branch": default_branch,
        "worktree_remote": remote,
        "worktree_base": str(worktree_base),
        "board_url": f"http://127.0.0.1:{plan.port}",
        "board_socket": plan.socket_path,
        "session_dir": _new_project_session_dir(plan.project, plan.owner_user),
        "pane_launcher": str(switchyard_shared_pane_launcher()),
        "presentation": {
            "slot_count": min(len(role_defs), MAX_VISIBLE_PANES_PER_WINDOW),
            "layouts": {
                "default": {
                    str(index): role
                    for index, (role, _cli) in enumerate(role_defs[:MAX_VISIBLE_PANES_PER_WINDOW])
                }
            },
        },
        "roles": roles,
    }
    if upstream_report_url:
        payload["upstream_report_url"] = upstream_report_url
    if upstream_report_token_file:
        payload["upstream_report_token_file"] = upstream_report_token_file
    if plan.workflow is not None:
        from scripts.workflow_launcher import project_roles
        payload = project_roles(payload, plan.workflow)
    return payload


def _dedupe_role_defs(role_defs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, cli in role_defs:
        if role in seen:
            continue
        seen.add(role)
        result.append((role, cli))
    return result


def _new_project_role_env(
    role: str,
    plan: ProjectBoardProvision,
    *,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {
        "TICKET_BOARD_DIRECTORCTL": f"{plan.board_current}/scripts/directorctl",
    }
    if role == "designer" and design_document is not None:
        project_dir = design_document.parent
        env.update(
            {
                "SWITCHYARD_PROJECT_SLUG": plan.project,
                "SWITCHYARD_PROJECT_NAME": project_name or plan.project,
                "SWITCHYARD_PROJECT_ARTIFACT": str(artifact_path or (_switchyard_dir(project_dir) / f"{plan.project}.project.json")),
                "SWITCHYARD_PROJECT_DESIGN": str(design_document),
                "SWITCHYARD_DESIGNER_ONBOARDING": str(
                    _switchyard_dir(project_dir) / SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME
                ),
            }
        )
    if role == "director" and director_onboarding is not None and design_document is not None:
        env.update(
            {
                "SWITCHYARD_DIRECTOR_ONBOARDING": str(director_onboarding),
                "SWITCHYARD_PROJECT_DESIGN": str(design_document),
            }
        )
    if role == "director":
        # Generic role data, read by the hook exactly as any other role's prompt is.
        # Seeded here because a project with no declarative workflow document has no
        # document to carry it, and the director's remit must not depend on one: the
        # hook no longer has a director-only branch to fall back to. A declarative
        # project carries the same field in its document, and the projection governs it
        # there, so this value is replaced rather than layered on top.
        seed_root = _director_seed_project_dir(design_document, director_onboarding)
        if seed_root is not None:
            env.setdefault(
                "TICKET_BOARD_ROLE_ONBOARDING_PROMPT",
                director_onboarding_seed_text(seed_root),
            )
    return env


def _director_seed_project_dir(
    design_document: Path | None, director_onboarding: Path | None
) -> Path | None:
    if design_document is not None:
        return design_document.parent
    if director_onboarding is not None:
        parent = director_onboarding.parent
        return parent.parent if parent.name == SWITCHYARD_PROJECT_DIR_NAME else parent
    return None


def write_new_project_launcher_artifacts(
    plan: ProjectBoardProvision,
    output_dir: Path,
    *,
    repository: Path,
    project_name: str | None = None,
    artifact_path: Path | None = None,
    design_document: Path | None = None,
    director_onboarding: Path | None = None,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    print_func: Callable[[str], None] = print,
) -> Path:
    role_defs = _dedupe_role_defs(
        role_clis
        or _default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=audit_roles,
        )
    )
    visible_role_count = min(len(role_defs), MAX_VISIBLE_PANES_PER_WINDOW)
    detached_roles = [role for role, _cli in role_defs[MAX_VISIBLE_PANES_PER_WINDOW:]]
    if detached_roles:
        print_func(
            f"team-launcher: auto-detached roles beyond the {MAX_VISIBLE_PANES_PER_WINDOW}-pane window cap: "
            f"{', '.join(detached_roles)}; use attach-role to surface one later or detach another role first"
        )
    config_path = output_dir / f"{plan.project}.json"
    layout_path = output_dir / f"{plan.project}-konsole-layout.json"
    layout_path.write_text(
        json.dumps(_new_project_layout_payload(visible_role_count), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            _new_project_launcher_config_payload(
                plan,
                repository=repository,
                project_name=project_name,
                artifact_path=artifact_path,
                design_document=design_document,
                director_onboarding=director_onboarding,
                implementer_roles=implementer_roles,
                role_clis=role_defs,
                include_designer=include_designer,
                include_audit=include_audit,
                audit_roles=audit_roles,
                remote=remote,
                default_branch=default_branch,
                worktree_policy=worktree_policy,
                upstream_report_url=upstream_report_url,
                upstream_report_token_file=upstream_report_token_file,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if plan.workflow:
        projected = _load_json(config_path)
        visible_role_count = max(1, 1 + max((role.get("slot", -1) for role in projected["roles"]), default=-1))
        _write_json_atomic(layout_path, _new_project_layout_payload(visible_role_count))
        if artifact_path and artifact_path.exists():
            artifact_data = _load_json(artifact_path)
            artifact_data["workflow"] = plan.workflow
            _write_json_atomic(artifact_path, artifact_data)
    policy_file = output_dir / "desktop-policy.json"
    if policy_file.exists():
        from scripts.desktop_access import validate_policy
        payload = _load_json(config_path)
        payload["desktop_access"] = validate_policy(_load_json(policy_file), project=plan.project, tenant=plan.owner_user)
        _write_json_atomic(config_path, payload)
    return config_path


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _tcp_port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _git_status_porcelain(repo: Path, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> str:
    try:
        result = run_owner_correct_git(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            runner=runner,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot inspect deploy checkout {repo}: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: cannot inspect deploy checkout {repo}{detail}")
    return str(getattr(result, "stdout", "") or "").rstrip("\n")


def _looks_like_switchyard_release_tree(path: Path) -> bool:
    return (
        (path / "switchyard").is_file()
        and (path / "scripts" / TEAM_LAUNCHER_NAME).is_file()
        and (path / "scripts" / "ticket-board-service.sh").is_file()
    )


def _switchyard_release_source_error(source_repo: Path) -> str:
    direct_marker = _read_switchyard_release_marker(source_repo)
    if direct_marker is not None:
        if direct_marker.marker_error:
            return (
                f"deploy source {source_repo} has an invalid {SWITCHYARD_RELEASE_MARKER_NAME}: "
                f"{direct_marker.marker_error}"
            )
        if _looks_like_switchyard_release_tree(source_repo):
            return ""
        return (
            f"deploy source {source_repo} has {SWITCHYARD_RELEASE_MARKER_NAME}, but is missing "
            "the expected exported launcher files"
        )

    shared_release = shared_switchyard_release_for_path(source_repo)
    if shared_release is None:
        return "not-release"
    if shared_release.marker_error:
        return (
            f"deploy source {source_repo} has an invalid shared release marker "
            f"at {shared_release.root}: {shared_release.marker_error}"
        )
    if shared_release.marker_commit or _looks_like_switchyard_release_tree(source_repo):
        return ""
    return (
        f"deploy source {source_repo} is under the Switchyard shared install, but is missing "
        f"{SWITCHYARD_RELEASE_MARKER_NAME} and the expected exported launcher files"
    )


def _precheck_deploy_source(
    source_repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str]:
    if not _path_exists(source_repo):
        return [f"deploy source {source_repo} does not exist"]

    release_error = _switchyard_release_source_error(source_repo)
    if release_error == "":
        return []
    if release_error != "not-release":
        return [release_error]

    has_git_metadata = (source_repo / ".git").exists()
    try:
        status = _git_status_porcelain(source_repo, runner=runner)
    except SystemExit:
        if has_git_metadata:
            raise
        return [
            f"deploy source {source_repo} is neither a git checkout nor a Switchyard release; "
            f"expected a clean Switchyard source checkout, or an exported release with "
            f"{SWITCHYARD_RELEASE_MARKER_NAME}"
        ]
    if status.strip():
        return [f"deploy checkout {source_repo} has uncommitted changes"]
    return []


def _system_unit_file_exists(unit: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    try:
        result = runner(
            ["systemctl", "list-unit-files", "--no-legend", unit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return _path_exists(Path("/etc/systemd/system") / unit)
    if result.returncode != 0:
        return _path_exists(Path("/etc/systemd/system") / unit)
    stdout = str(getattr(result, "stdout", "") or "").strip()
    return bool(stdout and not stdout.startswith("0 unit files listed"))


def _database_exists(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> bool:
    escaped = database.replace("'", "''")
    command = [
        "psql",
        "-XAt",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{escaped}'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot verify PostgreSQL database availability: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: cannot verify PostgreSQL database availability{detail}")
    return str(getattr(result, "stdout", "") or "").strip() == "1"


def _ticket_board_table_count(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> int:
    command = [
        "psql",
        "-XAt",
        f"postgresql:///{database}?host=/var/run/postgresql",
        "-c",
        "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot inspect PostgreSQL database {database!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: cannot inspect PostgreSQL database {database!r}{detail}")
    raw_count = str(getattr(result, "stdout", "") or "").strip()
    try:
        return int(raw_count)
    except ValueError as exc:
        raise SystemExit(f"team-launcher: cannot parse ticket_board table count for database {database!r}: {raw_count!r}") from exc


def _usable_switchyard_entry_for_project(
    project: str,
    *,
    config_dir: Path | None,
    registry_dir: Path | None,
) -> tuple[SwitchyardProjectEntry | None, list[str]]:
    broken_entries: list[str] = []
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        if entry.slug.casefold() != project.casefold():
            continue
        try:
            load_project_config(entry.slug, entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit) as exc:
            broken_entries.append(f"{entry.config_path}: {exc}")
            continue
        return entry, broken_entries
    return None, broken_entries


def _project_board_provision_from_json(path: Path) -> ProjectBoardProvision:
    raw = _load_json(path)
    fields: dict[str, Any] = {}
    for name in ProjectBoardProvision.__dataclass_fields__:
        if name == "project_name" and name not in raw:
            raw_project = str(raw.get("project") or "pgu")
            fields[name] = "PGU" if raw_project == "pgu" else raw_project
            continue
        if name == "workflow" and name not in raw:
            fields[name] = None
            continue
        if name == "owner_home" and name not in raw:
            raw_project = str(raw.get("project") or "").strip()
            raw_board_root = Path(str(raw.get("board_root") or "")).expanduser()
            if raw_project and raw_board_root.name == f"{raw_project}-ticketboard-live":
                fields[name] = str(raw_board_root.parent)
                continue
            raise SystemExit(
                f"switchyard: {path} is missing provision field 'owner_home' and it cannot be "
                "derived from board_root"
            )
        if name == "canary_unit" and name not in raw:
            fields[name] = f"{raw.get('project')}-ticket-board-canary.service"
            continue
        if name not in raw:
            raise SystemExit(f"switchyard: {path} is missing provision field {name!r}")
        fields[name] = raw[name]
    return ProjectBoardProvision(**fields)


def _port_from_board_url(board_url: str) -> int | None:
    match = re.fullmatch(r"https?://(?:127\.0\.0\.1|localhost):([0-9]{1,5})(?:/.*)?", board_url.strip())
    if not match:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _teardown_project_context(
    project: str,
    *,
    owner_user: str | None,
    config_dir: Path | None,
    registry_dir: Path | None,
    home_base: Path,
) -> tuple[ProjectBoardProvision, Path, Path, bool]:
    project_slug = _validate_project_slug(project)
    registry_path = (registry_dir or DEFAULT_SWITCHYARD_REGISTRY_DIR) / f"{project_slug}.json"
    entry, broken_entries = _usable_switchyard_entry_for_project(
        project_slug,
        config_dir=config_dir,
        registry_dir=registry_dir,
    )
    if entry is None:
        if broken_entries:
            detail = "\n  - ".join(broken_entries)
            raise SystemExit(
                f"switchyard: refusing to infer teardown artifacts for registered project {project_slug!r} "
                f"because its launch entry cannot be loaded:\n  - {detail}\n"
                "switchyard: rerun teardown with permissions that can read the project config, or repair the registry entry first"
            )
        resolved_owner = owner_user or _default_new_project_owner(project_slug)
        owner_home = home_base / resolved_owner
        plan = build_plan(
            project=project_slug,
            project_name=project_slug,
            owner_user=resolved_owner,
            board_root=owner_home / f"{project_slug}-ticketboard-live",
            commit_git_dir=commit_git_dir_env_for_project(project=project_slug, owner_home=owner_home),
            asset_dir=owner_home / ".claude" / f"{project_slug}-tickets-assets",
            frame_dir=owner_home / ".claude" / f"{project_slug}-ticket-frames",
        )
        return plan, owner_home / "Projects" / project_slug, registry_path, False

    config = load_project_config(entry.slug, entry.config_path)
    project_checkout = _project_dir_from_generated_config_path(entry.config_path) or config.repository
    if project_checkout is None:
        project_checkout = home_base / (config.run_as_user or owner_user or _default_new_project_owner(entry.slug)) / "Projects" / entry.slug
    plan_path = entry.config_path.parent / "plan.json"
    if plan_path.exists():
        plan = _project_board_provision_from_json(plan_path)
    else:
        resolved_owner = owner_user or config.run_as_user or _default_new_project_owner(entry.slug)
        plan = build_plan(
            project=entry.slug,
            project_name=config.project_name,
            owner_user=resolved_owner,
            port=_port_from_board_url(config.board_url),
            source_repo=_repo_root(),
            ticket_prefix=config.ticket_prefix,
        )
    return plan, project_checkout, registry_path, True


def _ticket_board_existing_ticket_count(database: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]]) -> int | None:
    escaped_database = database.replace("'", "''")
    command = [
        "psql",
        "-XAt",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{escaped_database}'",
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    if str(getattr(result, "stdout", "") or "").strip() != "1":
        return 0

    count_sql = (
        "SELECT CASE WHEN to_regclass('ticket_board.tickets') IS NULL "
        "THEN 0 ELSE (SELECT count(*)::int FROM ticket_board.tickets) END"
    )
    command = [
        "psql",
        "-XAt",
        f"postgresql:///{database}?host=/var/run/postgresql",
        "-c",
        count_sql,
    ]
    if os.geteuid() == 0:
        command = ["sudo", "-u", "postgres", *command]
    try:
        result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    raw_count = str(getattr(result, "stdout", "") or "").strip()
    try:
        return int(raw_count or "0")
    except ValueError:
        return None


def _bash_action(script: str) -> tuple[str, ...]:
    return ("bash", "-lc", script)


def _drop_database_command(database: str) -> tuple[str, ...]:
    return (
        "sudo",
        "-u",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "postgresql:///postgres?host=/var/run/postgresql",
        "-c",
        f"DROP DATABASE IF EXISTS {sql_identifier(database)} WITH (FORCE);",
    )


def _switchyard_teardown_actions(
    plan: ProjectBoardProvision,
    *,
    registry_path: Path,
    home_base: Path,
    remove_owner_home: bool,
    remove_owner_user: bool,
) -> tuple[SwitchyardTeardownAction, ...]:
    owner_home = home_base / plan.owner_user
    listener_unit_path = owner_home / ".config" / "systemd" / "user" / plan.listener_unit
    owner_runtime_script = (
        f"owner_uid=$(id -u {shlex.quote(plan.owner_user)} 2>/dev/null || true); "
        'if [ -n "$owner_uid" ] && [ -S "/run/user/$owner_uid/bus" ]; then '
        f"sudo -u {shlex.quote(plan.owner_user)} env "
        'XDG_RUNTIME_DIR="/run/user/$owner_uid" '
        'DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$owner_uid/bus" '
        f"systemctl --user disable --now {shlex.quote(plan.listener_unit)} || true; "
        "fi"
    )
    board_service_script = (
        f"if systemctl list-unit-files --no-legend {shlex.quote(plan.board_unit)} | grep -q .; then "
        f"systemctl disable --now {shlex.quote(plan.board_unit)}; "
        "fi"
    )
    board_root = Path(plan.board_root)
    actions = [
        SwitchyardTeardownAction(
            f"stop and disable system board service {plan.board_unit}",
            _bash_action(board_service_script),
        ),
        SwitchyardTeardownAction(
            f"stop and disable owner notify-listener service {plan.listener_unit}",
            _bash_action(owner_runtime_script),
        ),
        SwitchyardTeardownAction(
            f"remove owner notify-listener unit {listener_unit_path}",
            ("rm", "-f", str(listener_unit_path)),
        ),
        SwitchyardTeardownAction(
            f"remove system board unit /etc/systemd/system/{plan.board_unit}",
            ("rm", "-f", f"/etc/systemd/system/{plan.board_unit}"),
        ),
        SwitchyardTeardownAction(
            f"remove tmpfiles config /etc/tmpfiles.d/{plan.tmpfiles_name}",
            ("rm", "-f", f"/etc/tmpfiles.d/{plan.tmpfiles_name}"),
        ),
        SwitchyardTeardownAction(
            f"remove polkit rule /etc/polkit-1/rules.d/{plan.polkit_name}",
            ("rm", "-f", f"/etc/polkit-1/rules.d/{plan.polkit_name}"),
        ),
        SwitchyardTeardownAction(
            "reload systemd manager configuration",
            ("systemctl", "daemon-reload"),
        ),
        SwitchyardTeardownAction(
            f"drop PostgreSQL database {plan.database}",
            _drop_database_command(plan.database),
        ),
        SwitchyardTeardownAction(
            f"remove board release root {board_root}",
            ("rm", "-rf", "--", str(board_root)),
        ),
        SwitchyardTeardownAction(
            f"remove switchyard registry entry {registry_path}",
            ("rm", "-f", str(registry_path)),
        ),
    ]
    if remove_owner_home:
        actions.append(
            SwitchyardTeardownAction(
                f"remove owner home directory {owner_home}",
                ("rm", "-rf", "--", str(owner_home)),
            )
        )
    if remove_owner_user:
        actions.append(
            SwitchyardTeardownAction(
                f"remove owner user account {plan.owner_user}",
                ("userdel", plan.owner_user),
            )
        )
    return tuple(actions)


def _print_teardown_plan(
    teardown: SwitchyardTeardownPlan,
    *,
    dry_run: bool,
    drop_nonempty_board: bool,
    destroy_registered_tenant: bool,
    remove_owner_home: bool,
    remove_owner_user: bool,
    print_func: Callable[[str], None],
) -> None:
    print_func(f"switchyard: teardown plan for {teardown.project}")
    print_func(f"switchyard: owner user: {teardown.owner_user}")
    print_func(f"switchyard: owner home: {teardown.owner_home}")
    print_func(f"switchyard: project checkout: {teardown.project_checkout}")
    ticket_count = "unknown" if teardown.ticket_count is None else str(teardown.ticket_count)
    print_func(f"switchyard: board ticket count: {ticket_count}")
    registered_health = "yes" if teardown.registered_healthy else "no"
    print_func(f"switchyard: registered and launchable: {registered_health}")
    if teardown.registered_healthy and not destroy_registered_tenant:
        print_func("switchyard: live-tenant guard: destructive run requires --destroy-registered-tenant")
    if teardown.ticket_count is None and not drop_nonempty_board:
        print_func("switchyard: board-count guard: destructive run requires --drop-nonempty-board")
    elif teardown.ticket_count and not drop_nonempty_board:
        print_func("switchyard: non-empty board guard: destructive run requires --drop-nonempty-board")
    print_func("switchyard: preserved by default:")
    if not remove_owner_user:
        print_func(f"  - owner user account {teardown.owner_user}")
    if not remove_owner_home:
        print_func(f"  - owner home directory {teardown.owner_home}")
        print_func(f"  - project checkout {teardown.project_checkout}")
    print_func("switchyard: actions:")
    for index, action in enumerate(teardown.actions, start=1):
        print_func(f"  {index}. {action.label}")
        print_func(f"     command: {action.command_display}")
    if dry_run:
        print_func("switchyard: dry-run only; no changes made")


def _confirm_teardown_project(
    project: str,
    *,
    confirmation: str | None,
    input_func: Callable[[str], str],
) -> None:
    expected = project.strip()
    answer = confirmation
    if answer is None:
        answer = _read_prompt(f"Type {expected} to tear down this project: ", input_func=input_func).strip()
    if answer != expected:
        raise SystemExit(f"switchyard: teardown confirmation failed; expected {expected!r}")


def _run_teardown_actions(
    actions: Sequence[SwitchyardTeardownAction],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    completed: list[SwitchyardTeardownAction] = []
    for index, action in enumerate(actions):
        print_func(f"switchyard: running: {action.label}")
        result = runner(list(action.command), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            completed.append(action)
            continue
        stderr = str(getattr(result, "stderr", "") or "").strip()
        stdout = str(getattr(result, "stdout", "") or "").strip()
        detail = stderr or stdout or f"exit status {result.returncode}"
        remaining = list(actions[index:])
        message = [
            f"switchyard: teardown failed while trying to {action.label}: {detail}",
            "switchyard: completed before failure:",
        ]
        message.extend(f"  - {item.label}" for item in completed)
        if not completed:
            message.append("  - (none)")
        message.append("switchyard: remaining after failure:")
        message.extend(f"  - {item.label}" for item in remaining)
        raise SystemExit("\n".join(message))


def switchyard_teardown_command(
    project: str,
    *,
    dry_run: bool = False,
    confirm: str | None = None,
    drop_nonempty_board: bool = False,
    destroy_registered_tenant: bool = False,
    remove_owner_home: bool = False,
    remove_owner_user: bool = False,
    owner_user: str | None = None,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    home_base: Path = Path("/home"),
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> int:
    plan, project_checkout, registry_path, registered_healthy = _teardown_project_context(
        project,
        owner_user=owner_user,
        config_dir=config_dir,
        registry_dir=registry_dir,
        home_base=home_base,
    )
    ticket_count = _ticket_board_existing_ticket_count(plan.database, runner=runner)
    teardown = SwitchyardTeardownPlan(
        project=plan.project,
        owner_user=plan.owner_user,
        owner_home=home_base / plan.owner_user,
        project_checkout=project_checkout,
        ticket_count=ticket_count,
        registered_healthy=registered_healthy,
        registry_path=registry_path,
        actions=_switchyard_teardown_actions(
            plan,
            registry_path=registry_path,
            home_base=home_base,
            remove_owner_home=remove_owner_home,
            remove_owner_user=remove_owner_user,
        ),
    )
    _print_teardown_plan(
        teardown,
        dry_run=dry_run,
        drop_nonempty_board=drop_nonempty_board,
        destroy_registered_tenant=destroy_registered_tenant,
        remove_owner_home=remove_owner_home,
        remove_owner_user=remove_owner_user,
        print_func=print_func,
    )
    if dry_run:
        return 0
    if registered_healthy and not destroy_registered_tenant:
        raise SystemExit(
            f"switchyard: refusing to tear down registered launchable tenant {plan.project!r}; "
            "pass --destroy-registered-tenant to confirm removing its board unit, database, release root, and registry entry"
        )
    if ticket_count is None and not drop_nonempty_board:
        raise SystemExit(
            f"switchyard: cannot determine whether board database {plan.database} contains tickets; "
            "pass --drop-nonempty-board to confirm dropping it anyway"
        )
    if ticket_count and not drop_nonempty_board:
        raise SystemExit(
            f"switchyard: refusing to drop non-empty board database {plan.database} "
            f"with {ticket_count} ticket(s); pass --drop-nonempty-board to confirm that data loss"
        )
    _confirm_teardown_project(plan.project, confirmation=confirm, input_func=input_func)
    _run_teardown_actions(teardown.actions, runner=runner, print_func=print_func)
    print_func(f"switchyard: teardown complete for {plan.project}")
    return 0


def precheck_new_project(
    plan: ProjectBoardProvision,
    *,
    source_repo: Path,
    repository: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    require_owner_user: bool = True,
    require_repository: bool = True,
) -> None:
    errors: list[str] = []
    if require_owner_user and uid_for_user(plan.owner_user) is None:
        errors.append(f"target user {plan.owner_user!r} does not exist")
    if require_repository and not _path_exists(repository):
        errors.append(f"project repository {repository} does not exist")
    errors.extend(_precheck_deploy_source(source_repo, runner=runner))
    unit_exists = _system_unit_file_exists(plan.board_unit, runner=runner)
    database_exists = _database_exists(plan.database, runner=runner)
    socket_path = Path(plan.socket_path)
    socket_is_present = socket_exists(socket_path)
    port_is_live = port_in_use(plan.port)
    if not unit_exists:
        if database_exists:
            errors.append(f"database {plan.database!r} already exists but {plan.board_unit} is not installed")
        if socket_is_present:
            errors.append(f"socket {plan.socket_path} already exists but {plan.board_unit} is not installed")
        if port_is_live:
            errors.append(f"port {plan.port} is already in use but {plan.board_unit} is not installed")
    elif not database_exists:
        errors.append(f"{plan.board_unit} is installed but database {plan.database!r} does not exist")
    else:
        table_count = _ticket_board_table_count(plan.database, runner=runner)
        if table_count > 0:
            launch_entry, broken_entries = _usable_switchyard_entry_for_project(
                plan.project,
                config_dir=config_dir,
                registry_dir=registry_dir,
            )
            if launch_entry is not None:
                errors.append(
                    f"project {plan.project!r} is already provisioned "
                    f"(database {plan.database} has {table_count} ticket_board tables, {plan.board_unit} is installed).\n"
                    f"  to launch it:      switchyard {plan.project}\n"
                    f"  to start over:     switchyard teardown {plan.project} --dry-run"
                )
            else:
                effective_config_dir = config_dir or DEFAULT_CONFIG_DIR
                effective_registry_dir = registry_dir or DEFAULT_SWITCHYARD_REGISTRY_DIR
                broken_entry_detail = f"  unusable launch entry: {broken_entries[0]}\n" if broken_entries else ""
                errors.append(
                    f"project {plan.project!r} is partially provisioned but not registered "
                    f"(database {plan.database} has {table_count} ticket_board tables, "
                    f"{plan.board_unit} is installed, but no usable launch entry exists in "
                    f"{effective_config_dir} or {effective_registry_dir}).\n"
                    f"{broken_entry_detail}"
                    f"  to inspect recovery: switchyard teardown {plan.project} --dry-run"
                )
    if errors:
        raise SystemExit("team-launcher: new project precheck failed:\n- " + "\n- ".join(errors))


def new_project_command(
    project: str,
    *,
    from_artifact: Path | None = None,
    owner_user: str | None = None,
    owner_home: Path | None = None,
    desktop_policy: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    source_repo: Path | None = None,
    workflow_config: Path | None = None,
    commit_git_dir: str | None = None,
    repository: Path | None = None,
    output_dir: Path | None = None,
    director_onboarding: Path | None = None,
    execute: bool = False,
    dry_run: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    require_owner_user: bool | None = None,
    enable_owner_linger: bool = True,
    upstream_report_url: str = "",
    upstream_report_token_file: str = "",
    print_func: Callable[[str], None] = print,
) -> int:
    if execute and dry_run:
        raise SystemExit("team-launcher: --execute and --dry-run are mutually exclusive")
    effective_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    design_artifact = load_project_design_artifact(from_artifact, expected_project=project) if from_artifact else None
    if design_artifact is not None:
        if owner_user is not None or repository is not None:
            raise SystemExit("team-launcher: --from owns owner/repository answers; do not also pass --owner-user or --repository")
        effective_owner = design_artifact.owner_user
        effective_repository = design_artifact.repository.expanduser().resolve(strict=False)
        remote = design_artifact.remote
        default_branch = design_artifact.default_branch
        worktree_policy = design_artifact.worktree_policy
        ticket_prefix = design_artifact.ticket_prefix
        project_name = design_artifact.project_name
        design_document = design_artifact.design_document if design_artifact.include_designer else None
        implementer_roles = design_artifact.implementer_roles
        role_clis = design_artifact.role_clis
        include_designer = design_artifact.include_designer
        include_audit = design_artifact.include_audit
        audit_roles = design_artifact.audit_roles
        board_service_traversal = bool(design_artifact.capability_grants.get("board_service_traversal", True))
    else:
        if repository is None:
            raise SystemExit("team-launcher: new project requires --repository for the project's working checkout")
        effective_owner = (owner_user or _default_new_project_owner(project)).strip()
        effective_repository = repository.expanduser().resolve(strict=False)
        remote = "origin"
        default_branch = "main"
        worktree_policy = "shared"
        ticket_prefix = None
        project_name = project
        design_document = None
        implementer_roles = DEFAULT_PROJECT_IMPLEMENTER_ROLES
        role_clis = _default_role_cli_pairs(implementer_roles, include_designer=True, include_audit=True)
        include_designer = True
        include_audit = True
        audit_roles = ("audit",)
        board_service_traversal = True
    if worktree_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"team-launcher: worktree policy must be one of {sorted(WORKTREE_POLICIES)}")
    plan = build_plan(
        project=project,
        project_name=project_name,
        workflow=seed_director_onboarding(
            json.loads(workflow_config.read_text())
            if workflow_config
            else _load_json(from_artifact).get("workflow")
            if from_artifact
            else None,
            effective_repository,
        )[0],
        owner_user=effective_owner,
        owner_home=owner_home,
        port=port,
        database=database,
        source_repo=effective_source_repo,
        commit_git_dir=commit_git_dir,
        ticket_prefix=ticket_prefix,
        implementer_roles=implementer_roles,
        board_service_traversal=board_service_traversal,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
    )
    precheck_new_project(
        plan,
        source_repo=effective_source_repo,
        repository=effective_repository,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        config_dir=None,
        registry_dir=None,
        require_owner_user=execute if require_owner_user is None else require_owner_user,
    )
    artifact_dir = (output_dir or _new_project_artifact_dir(plan.project)).expanduser().resolve(strict=False)
    if desktop_policy is not None:
        from scripts.desktop_access import validate_policy
        selected = {"mode": "headless"} if str(desktop_policy) == "headless" else _load_json(desktop_policy)
        selected = validate_policy(selected, project=plan.project, tenant=plan.owner_user)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(artifact_dir / "desktop-policy.json", selected)
    # The rollout must hand each role the tree it works in, so record those
    # paths on the plan before the operator artifact is rendered (SYRD-39).
    _role_worktree_base = _new_project_worktree_base(plan.project, plan.owner_user)
    plan = replace(
        plan,
        role_worktrees=tuple(
            (role, str(_role_worktree_base / role)) for role, _account in plan.role_accounts
        ),
    )
    write_artifacts(plan, artifact_dir, enable_owner_linger=enable_owner_linger)
    config_path = write_new_project_launcher_artifacts(
        plan,
        artifact_dir,
        repository=effective_repository,
        project_name=project_name,
        artifact_path=from_artifact,
        design_document=design_document,
        director_onboarding=director_onboarding,
        implementer_roles=implementer_roles,
        role_clis=role_clis,
        include_designer=include_designer,
        include_audit=include_audit,
        audit_roles=audit_roles,
        remote=remote,
        default_branch=default_branch,
        worktree_policy=worktree_policy,
        upstream_report_url=upstream_report_url.strip(),
        upstream_report_token_file=upstream_report_token_file.strip(),
        print_func=print_func,
    )
    commands_path = artifact_dir / "operator-commands.sh"
    # The complete role-isolation handoff -- accounts, ownership, runtime,
    # tooling AND credential seeding -- is written beside the other artifacts, so
    # an operator who follows the printed instruction once has everything. It
    # used to be produced only by a later failed start, which meant doing exactly
    # what provisioning said still left the credential gaps open (SYRD-39).
    try:
        handoff_config = load_project_config(plan.project, config_path)
    except SystemExit:
        handoff_config = None
    if handoff_config is not None and role_isolation_gaps(handoff_config):
        handoff_path = config_path.with_name(f"{plan.project}-role-accounts.sh")
        try:
            handoff_path.write_text(render_role_account_migration(handoff_config), encoding="utf-8")
            handoff_path.chmod(0o755)
            print_func(
                f"team-launcher: roles are not isolated yet; run {handoff_path} as an operator "
                "(safe to re-run) before starting them"
            )
        except OSError as exc:
            print_func(f"team-launcher: could not write {handoff_path}: {exc}")
    if not execute:
        print_func(f"team-launcher: dry-run for {plan.project}; artifacts in {artifact_dir}")
        print_func(f"team-launcher: launcher config {config_path}")
        print_func("team-launcher: execution plan:")
        print_func(f"  cd {shlex.quote(str(artifact_dir))}")
        print_func("  sudo -v")
        print_func(f"  bash {shlex.quote(str(commands_path.name))}")
        print_func("")
        print_func(commands_path.read_text(encoding="utf-8").rstrip("\n"))
        return 0
    print_func(f"team-launcher: provisioning {plan.project}; artifacts in {artifact_dir}")
    sudo_result = runner(["sudo", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if sudo_result.returncode != 0:
        stderr = str(getattr(sudo_result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: sudo authentication failed{detail}")
    result = runner(["bash", str(commands_path)], cwd=str(artifact_dir))
    if result.returncode != 0:
        raise SystemExit(f"team-launcher: provisioning failed with exit status {result.returncode}")
    config = load_project_config(plan.project, config_path)
    if config.desktop_access is not None:
        configure_project_desktop(config, config_path=config_path,
            helper=effective_source_repo / "scripts/desktop_access.py", runner=runner)
    from scripts.workflow_launcher import assign_projection_owner
    assign_projection_owner(config, config_path, runner=runner)
    print_func(f"team-launcher: provisioned {plan.project}; launcher config {config_path}")
    return 0


@dataclass(frozen=True)
class SwitchyardProjectEntry:
    slug: str
    name: str
    config_path: Path


@dataclass(frozen=True)
class SwitchyardProjectStatus:
    name: str
    slug: str
    state: str
    panes_up: int | None
    panes_total: int | None
    config_path: Path
    viewer_session: str | None = None
    error: str = ""

    @property
    def panes_display(self) -> str:
        if self.panes_up is None or self.panes_total is None:
            return "?/?"
        return f"{self.panes_up}/{self.panes_total}"

    @property
    def viewer_display(self) -> str:
        return self.viewer_session or "-"


@dataclass(frozen=True)
class SwitchyardTeardownAction:
    label: str
    command: tuple[str, ...]

    @property
    def command_display(self) -> str:
        return shlex.join(self.command)


@dataclass(frozen=True)
class SwitchyardTeardownPlan:
    project: str
    owner_user: str
    owner_home: Path
    project_checkout: Path
    ticket_count: int | None
    registered_healthy: bool
    registry_path: Path
    actions: tuple[SwitchyardTeardownAction, ...]


@dataclass(frozen=True)
class SwitchyardRuntimeCopyStatus:
    project: str
    copy: str
    path: Path
    status: str


@dataclass(frozen=True)
class OwnerUserProvisionResult:
    created: bool
    linger_enabled: bool
    shell_path: str = ""


@dataclass(frozen=True)
class CodexHookTrustMismatch:
    event: str
    hook_key: str
    expected_hash: str
    trusted_hash: str
    affected_roles: tuple[str, ...]


@dataclass(frozen=True)
class ModelValidationFailure:
    role: str
    cli: str
    model: str
    reason: str
    suggestion: str


@dataclass(frozen=True)
class OwnerShellIssue:
    owner_user: str
    shell: str
    remedy: str


@dataclass(frozen=True)
class FirstRunAuthReport:
    unauthenticated_roles: dict[str, list[str]]
    untrusted_roles: list[tuple[str, str, str]]
    stale_codex_hook_trust: list[CodexHookTrustMismatch] = field(default_factory=list)
    missing_cli_roles: dict[str, list[str]] = field(default_factory=dict)
    model_validation_failures: list[ModelValidationFailure] = field(default_factory=list)
    owner_user: str = ""
    owner_shell_issue: OwnerShellIssue | None = None

    @property
    def has_warnings(self) -> bool:
        return bool(
            self.unauthenticated_roles
            or self.untrusted_roles
            or self.stale_codex_hook_trust
            or self.missing_cli_roles
            or self.model_validation_failures
            or self.owner_shell_issue
        )


@dataclass(frozen=True)
class FirstRunAuthLoginStep:
    cli: str
    roles: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class FirstRunFolderTrustStep:
    cli: str
    role: str
    workdir: Path
    command: tuple[str, ...]


@dataclass(frozen=True)
class FirstRunSetupManifest:
    owner_user: str
    login_steps: list[FirstRunAuthLoginStep]
    folder_trust_steps: list[FirstRunFolderTrustStep]
    stale_codex_hook_trust: list[CodexHookTrustMismatch]
    missing_cli_roles: dict[str, list[str]]
    owner_shell_issue: OwnerShellIssue | None = None

    @property
    def has_steps(self) -> bool:
        return bool(
            self.login_steps
            or self.folder_trust_steps
            or self.stale_codex_hook_trust
            or self.missing_cli_roles
            or self.owner_shell_issue
        )


FIRST_RUN_AUTH_STATUS_COMMANDS: dict[str, list[str]] = {
    "agy": ["agy", "models"],
    "claude": ["claude", "auth", "status", "--json"],
    "codex": ["codex", "login", "status"],
    "hermes": ["hermes", "config", "check"],
}
FIRST_RUN_AUTH_LOGIN_COMMANDS: dict[str, list[str]] = {
    "agy": ["agy"],
    "claude": ["claude", "auth", "login"],
    "codex": ["codex", "login"],
    "hermes": ["hermes", "model"],
}
# Vendor install commands, as TEXT for a person to run themselves. Switchyard
# never executes these. PGU-904 removed agent CLI installation deliberately --
# the user owns which version of each CLI they run -- and wiring this table to a
# subprocess would put that straight back. If you are here to "finish the job"
# by making these runnable, that is the thing this table exists to prevent.
#
# These strings WILL drift as vendors change their installers, and that is the
# accepted trade: a stale command in a message a person reads and can correct is
# a far smaller failure than a stale command we run on their machine. Verified
# on 2026-09-03 by fetching each URL without piping it -- every one served a
# shell script for the expected CLI, and the interpreter matches each script's
# shebang (claude, agy and hermes are bash; codex is sh).
#
# docs/fresh-machine-install.md carries the same four commands for a reader who
# has not run anything yet. Update both together; a test asserts they agree.
AGENT_CLI_INSTALL_COMMANDS: dict[str, str] = {
    "agy": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "hermes": "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash",
}
FIRST_RUN_TRUST_CLIS = frozenset({"agy", "claude"})
MODEL_VALIDATION_PROMPT = "Reply with exactly: model-ok"


def _missing_cli_install_clause(cli: str, owner_user: str = "") -> str:
    """How to install one missing CLI, as text the reader runs themselves.

    Names the owner user because installing a CLI only for the human running
    switchyard is the failure people actually hit: panes run as the owner, so a
    CLI on the invoking user's PATH is invisible to them.
    """
    owner = (owner_user or "").strip()
    target = f" for owner user {owner}" if owner else ""
    command = AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
    if not command:
        return f"install {cli}{target} with that vendor's own installer"
    return f"install {cli}{target} with: {command}"


def _owner_user_cli_reminder(owner_user: str = "") -> str:
    owner = (owner_user or "").strip()
    if not owner:
        return (
            "switchyard: panes run as the project's owner user, so a CLI installed only "
            "for the user running switchyard is not found."
        )
    return (
        f"switchyard: install each one for owner user {owner}; panes run as that user, so a CLI "
        "installed only for the user running switchyard is not found."
    )


def _owner_shell_issue(owner_user: str) -> OwnerShellIssue | None:
    try:
        info = pwd.getpwnam(owner_user)
    except KeyError:
        return None
    shell = str(getattr(info, "pw_shell", "") or "").strip()
    if not shell or os.access(shell, os.X_OK):
        return None
    fallback_shell = "/bin/bash" if os.access("/bin/bash", os.X_OK) else "/bin/sh"
    remedy = f"sudo usermod -s {fallback_shell} {shlex.quote(owner_user)}"
    return OwnerShellIssue(owner_user=owner_user, shell=shell, remedy=remedy)


def _slug_from_project_name(name: str) -> str:
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
    return _validate_project_slug(slug)


def _legacy_dash_slug_from_project_name(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    if not slug:
        raise SystemExit("switchyard: project slug cannot be empty")
    return slug


def _project_name_selector_slugs(name: str) -> set[str]:
    selectors: set[str] = set()
    for derive in (_slug_from_project_name, _legacy_dash_slug_from_project_name):
        try:
            selectors.add(derive(name).casefold())
        except SystemExit:
            continue
    return selectors


def _validate_project_slug(value: str) -> str:
    slug = value.strip().lower()
    if not PROJECT_SLUG_RE.fullmatch(slug):
        raise SystemExit("switchyard: project slug must match ^[a-z0-9][a-z0-9_]{0,39}$")
    return slug


def _agent_owner_user(agent_name: str) -> str:
    agent = agent_name.strip()
    if not agent:
        raise SystemExit("switchyard: agent name cannot be empty")
    return agent if agent.endswith("-agent") else f"{agent}-agent"


def _owner_user_verbatim(value: str) -> str:
    owner = value.strip()
    if not owner:
        raise SystemExit("switchyard: owner user cannot be empty")
    return owner


def _is_valid_owner_user_name(value: str) -> bool:
    return bool(OWNER_USER_NAME_RE.fullmatch(value))


@dataclass(frozen=True)
class ExistingOwnerUser:
    name: str
    uid: int
    home: Path


def _existing_owner_user(owner_user: str) -> ExistingOwnerUser | None:
    name = _owner_user_verbatim(owner_user)
    uid = uid_for_user(name)
    if uid is None:
        return None
    home = home_dir_for_user(name) or Path("/home") / name
    return ExistingOwnerUser(name=name, uid=uid, home=home)


def _confirm_existing_owner_user(
    owner_user: str,
    *,
    allow_existing_owner_user: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
    agy_credential_source: str = "",
) -> None:
    existing = _existing_owner_user(owner_user)
    if existing is None:
        return
    detail = f"{existing.name} (uid {existing.uid}, home {existing.home})"
    print_func(f"warning: switchyard: owner user {detail} already exists")
    if agy_credential_source:
        # Reusing an existing account and installing a credential into it are separate
        # things to consent to, so the confirmation has to name both.
        print_func(
            f"warning: switchyard: the agy credential from {agy_credential_source} will be "
            f"installed into {existing.name}, and every role of this project will be able "
            f"to act as the Google account {agy_credential_source} signed in to agy with"
        )
    if allow_existing_owner_user:
        return
    try:
        proceed = _prompt_bool(f"Use existing owner user {detail}", default=False, input_func=input_func)
    except SystemExit as exc:
        if str(exc) == "switchyard: no input available":
            raise SystemExit(
                f"switchyard: owner user {detail} already exists; "
                "pass --allow-existing-owner-user to reuse it"
            ) from None
        raise
    if not proceed:
        raise SystemExit("switchyard: cancelled")


def _project_dir(home_base: Path, owner_user: str, project_name: str) -> Path:
    return home_base / owner_user / "Projects" / _slug_from_project_name(project_name)


def _resolve_project_path(raw_path: Path | str) -> Path:
    return Path(os.path.expandvars(str(raw_path))).expanduser().resolve(strict=False)


def _switchyard_dir(project_dir: Path) -> Path:
    return project_dir / SWITCHYARD_PROJECT_DIR_NAME


from scripts.ticket_board.workflow_config import (  # noqa: E402
    DIRECTOR_ONBOARDING_MIGRATION,
    DIRECTOR_ROLE,
)


def director_onboarding_seed_text(project_dir: Path) -> str:
    """The director's remit, as ordinary role data rather than a special case.

    The hook used to build this at session start for the director alone, searching for
    the onboarding packet and printing paths relative to the pane's cwd. Storing it makes
    the director the same shape as every other role, at the cost of the paths being fixed
    at provisioning rather than rediscovered. It is stored as prompt text rather than as
    an `onboarding` file path because the generic path branch refuses to start when the
    file is missing, where the director previously degraded quietly.
    """
    packet = project_dir / "docs" / "onboarding"
    guide = packet / "switchyard-director-guide.md"
    return (
        f"Your director session just started fresh. Read the onboarding packet first: {packet}. "
        f"Start with {guide}."
    )


@dataclass(frozen=True)
class DirectorSeedResult:
    """What the backfill changed: the marker, the prompt, or neither."""

    marked: bool
    seeded: bool

    @property
    def changed(self) -> bool:
        return self.marked or self.seeded


def seed_director_onboarding(document, project_dir: Path):
    """Give the configured director stored onboarding when it has none.

    Idempotent, and only ever fills a gap: a director that already carries a prompt or a
    remit path is left untouched, as is every other role. The director is located through
    the workflow model's own required-role constant rather than a literal, because a
    document without that role is rejected by validation -- it is structural, not a
    tenant-chosen name.

    Returns the document and whether anything was seeded, so callers can report an
    automatic configuration change rather than making it silently.
    """
    if not document:
        return document, DirectorSeedResult(False, False)
    migrations = document.setdefault("migrations", {})
    if migrations.get(DIRECTOR_ONBOARDING_MIGRATION):
        # Already considered once. A director with nothing stored now means the operator
        # cleared it, not that this tenant predates the feature, so refilling here would
        # make `role-prompt clear director` impossible to persist.
        return document, DirectorSeedResult(False, False)
    seeded = False
    for role in document.get("roles", []):
        if role.get("name") != DIRECTOR_ROLE:
            continue
        if not role.get("onboarding_prompt") and not role.get("onboarding"):
            role["onboarding_prompt"] = director_onboarding_seed_text(project_dir)
            seeded = True
    # Marked whether or not anything was filled: a director that already had its own
    # onboarding is equally "considered". Reported separately from seeding, because a
    # tenant that needs only the marker still needs that marker persisted -- otherwise a
    # later clear would look like a legacy gap and be refilled.
    migrations[DIRECTOR_ONBOARDING_MIGRATION] = True
    return document, DirectorSeedResult(True, seeded)


def _write_switchyard_onboarding_files(
    *,
    project_name: str,
    slug: str,
    owner_user: str,
    project_dir: Path,
    artifact_path: Path,
    design_document: Path,
    director_onboarding: Path,
    include_designer: bool = True,
) -> None:
    switchyard_dir = _switchyard_dir(project_dir)
    switchyard_dir.mkdir(parents=True, exist_ok=True)
    if include_designer:
        designer_onboarding = switchyard_dir / SWITCHYARD_DESIGN_ONBOARDING_FILE_NAME
        designer_onboarding.write_text(
            "\n".join(
                [
                    f"# {project_name} Switchyard Design Session",
                    "",
                    f"Working directory: {project_dir}",
                    f"Project slug: {slug}",
                    f"Owner user: {owner_user}",
                    f"Design document: {design_document}",
                    f"Project artifact: {artifact_path}",
                    "",
                    "Create or refine the design document with the user.",
                    "The board and full pane window already exist; use tickets for follow-up implementation work.",
                    "Do not create workflow stages or workflow transitions in the artifact.",
                    "This project directory is the user-visible checkout and is not reset or cleaned by launch.",
                    "Implementation panes run from managed role worktrees outside this directory.",
                    "The initial git remote named origin points at this local repository as a bootstrap placeholder.",
                    "Replace origin with the project's real remote before expecting fetches to detect upstream changes.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    director_onboarding.write_text(
        "\n".join(
            [
                f"# {project_name} Director Onboarding",
                "",
                f"Project directory: {project_dir}",
                f"Project artifact: {artifact_path}",
                *([f"Design document: {design_document}"] if include_designer else ["Design phase: skipped"]),
                "",
                "The privileged provisioning and full pane window were started by switchyard new.",
                "Use the live board to onboard the team and reshape stages or roles later if the user asks.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _switchyard_source_commit(
    source_repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    direct_marker = _read_switchyard_release_marker(source_repo)
    if direct_marker is not None and direct_marker.marker_commit:
        return direct_marker.marker_commit
    shared_release = shared_switchyard_release_for_path(source_repo)
    if shared_release is not None and shared_release.marker_commit:
        return shared_release.marker_commit
    proc = run_owner_correct_git(
        ["git", "-C", str(source_repo), "rev-parse", "--verify", "HEAD"],
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return "unknown"
    return str(getattr(proc, "stdout", "") or "").strip() or "unknown"


def _stamp_onboarding_doc(*, source_name: str, source_commit: str, body: str) -> str:
    return (
        f"<!-- Switchyard onboarding snapshot: source commit {source_commit}; "
        f"source docs/onboarding/{source_name}. -->\n\n"
        f"{body}"
    )


ONBOARDING_SNAPSHOT_RE = re.compile(
    r"\A<!-- Switchyard onboarding snapshot: source commit (?P<commit>[^;]+); "
    r"source docs/onboarding/(?P<source>.+?)\. -->\n\n",
    re.S,
)


def _parse_onboarding_snapshot(text: str) -> tuple[str, str] | None:
    match = ONBOARDING_SNAPSHOT_RE.match(text)
    if not match:
        return None
    return match.group("commit").strip(), match.group("source").strip()


def _onboarding_history_git_args(
    *,
    source_repo: Path,
    commit_git_dir: str | None,
    source_commit: str,
    current_source_commit: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> list[str] | None:
    direct_release = _read_switchyard_release_marker(source_repo)
    shared_release = shared_switchyard_release_for_path(source_repo)
    immutable_release = bool(
        (direct_release is not None and direct_release.marker_commit)
        or (shared_release is not None and shared_release.marker_commit)
    )
    selected = str(commit_git_dir or "").strip()
    if not immutable_release or not selected:
        return ["git", "-C", str(source_repo)]

    required_commits = tuple(dict.fromkeys((source_commit, current_source_commit)))
    for raw_path in selected.split(os.pathsep):
        if not raw_path.strip():
            continue
        git_dir = Path(raw_path.strip()).expanduser()
        git_args = ["git", f"--git-dir={git_dir}"]
        for commit in required_commits:
            proc = run_owner_correct_git(
                [*git_args, "cat-file", "-e", f"{commit}^{{commit}}"],
                runner=runner,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if proc.returncode != 0:
                break
        else:
            return git_args
    return None


def _read_source_onboarding_doc_at_commit(
    *,
    source_repo: Path,
    git_args: Sequence[str] | None = None,
    source_commit: str,
    source_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str | None:
    if not source_commit or source_commit == "unknown":
        return None
    selected_git_args = list(git_args) if git_args is not None else ["git", "-C", str(source_repo)]
    proc = run_owner_correct_git(
        [*selected_git_args, "show", f"{source_commit}:docs/onboarding/{source_name}"],
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return str(getattr(proc, "stdout", "") or "")


def _source_onboarding_commit_is_ancestor(
    *,
    source_repo: Path,
    git_args: Sequence[str] | None = None,
    source_commit: str,
    current_source_commit: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool | None:
    if (
        not source_commit
        or source_commit == "unknown"
        or not current_source_commit
        or current_source_commit == "unknown"
    ):
        return None
    selected_git_args = list(git_args) if git_args is not None else ["git", "-C", str(source_repo)]
    proc = run_owner_correct_git(
        [
            *selected_git_args,
            "merge-base",
            "--is-ancestor",
            source_commit,
            current_source_commit,
        ],
        runner=runner,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _project_dir_from_generated_config_path(config_path: Path) -> Path | None:
    resolved = config_path.expanduser().resolve(strict=False)
    if resolved.parent.name != "provision" or resolved.parent.parent.name != SWITCHYARD_PROJECT_DIR_NAME:
        return None
    return resolved.parent.parent.parent


def upgrade_switchyard_onboarding_docs(
    *,
    source_repo: Path,
    project_dir: Path,
    owner_user: str,
    commit_git_dir: str | None = None,
    dry_run: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    source_dir = source_repo / "docs" / "onboarding"
    target_dir = project_dir / "docs" / "onboarding"
    if not source_dir.is_dir():
        print_func(
            "warning: switchyard: onboarding docs source "
            f"{source_dir} is missing; skipped upgrading "
            f"{', '.join(SWITCHYARD_ONBOARDING_DOC_NAMES)} in {target_dir}."
        )
        return

    current_source_commit = _switchyard_source_commit(source_repo, runner=runner)
    if not dry_run:
        install = runner(
            [
                "install",
                "-d",
                "-m",
                "0755",
                "-o",
                owner_user,
                "-g",
                owner_user,
                str(target_dir.parent),
                str(target_dir),
            ]
        )
        if install.returncode != 0:
            raise SystemExit(f"switchyard: failed to create onboarding docs directory {target_dir} for {owner_user}")
        if not target_dir.exists():
            target_dir.mkdir(parents=True, exist_ok=True)

    for name in SWITCHYARD_ONBOARDING_DOC_NAMES:
        source_path = source_dir / name
        target_path = target_dir / name
        try:
            current_body = source_path.read_text(encoding="utf-8")
        except OSError:
            print_func(f"switchyard: onboarding doc {name}: skipped, source missing")
            continue
        current_snapshot = _stamp_onboarding_doc(
            source_name=name,
            source_commit=current_source_commit,
            body=current_body,
        )
        if not target_path.exists():
            if not dry_run:
                target_path.write_text(current_snapshot, encoding="utf-8")
                _chown_project_file(owner_user=owner_user, path=target_path, runner=runner)
            print_func(f"switchyard: onboarding doc {name}: {'would install' if dry_run else 'installed'}")
            continue
        try:
            existing = target_path.read_text(encoding="utf-8")
        except OSError as exc:
            print_func(f"switchyard: onboarding doc {name}: skipped, cannot read installed copy: {exc}")
            continue
        provenance = _parse_onboarding_snapshot(existing)
        if provenance is None:
            print_func(f"switchyard: onboarding doc {name}: skipped, no provenance header")
            continue
        source_commit, source_name = provenance
        if source_name != name:
            print_func(f"switchyard: onboarding doc {name}: skipped, provenance names {source_name}")
            continue
        history_git_args = _onboarding_history_git_args(
            source_repo=source_repo,
            commit_git_dir=commit_git_dir,
            source_commit=source_commit,
            current_source_commit=current_source_commit,
            runner=runner,
        )
        if history_git_args is None:
            print_func(f"switchyard: onboarding doc {name}: skipped, cannot verify source commit {source_commit}")
            continue
        old_body = _read_source_onboarding_doc_at_commit(
            source_repo=source_repo,
            git_args=history_git_args,
            source_commit=source_commit,
            source_name=source_name,
            runner=runner,
        )
        if old_body is None:
            print_func(f"switchyard: onboarding doc {name}: skipped, cannot verify source commit {source_commit}")
            continue
        if existing != _stamp_onboarding_doc(source_name=name, source_commit=source_commit, body=old_body):
            print_func(f"switchyard: onboarding doc {name}: skipped, edited since source commit {source_commit}")
            continue
        source_commit_is_ancestor = _source_onboarding_commit_is_ancestor(
            source_repo=source_repo,
            git_args=history_git_args,
            source_commit=source_commit,
            current_source_commit=current_source_commit,
            runner=runner,
        )
        if source_commit_is_ancestor is False:
            print_func(f"switchyard: onboarding doc {name}: skipped, installed copy is newer than this checkout")
            continue
        if source_commit_is_ancestor is None:
            print_func(
                f"switchyard: onboarding doc {name}: skipped, cannot verify ancestry from "
                f"source commit {source_commit} to release commit {current_source_commit}"
            )
            continue
        if not dry_run:
            target_path.write_text(current_snapshot, encoding="utf-8")
            _chown_project_file(owner_user=owner_user, path=target_path, runner=runner)
        print_func(f"switchyard: onboarding doc {name}: {'would refresh' if dry_run else 'refreshed'}")


def _install_switchyard_onboarding_docs(
    *,
    source_repo: Path,
    project_dir: Path,
    owner_user: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    source_dir = source_repo / "docs" / "onboarding"
    target_dir = project_dir / "docs" / "onboarding"
    if not source_dir.is_dir():
        print_func(
            "warning: switchyard: onboarding docs source "
            f"{source_dir} is missing; skipped installing "
            f"{', '.join(SWITCHYARD_ONBOARDING_DOC_NAMES)} into {target_dir}. "
            "Copy them later from docs/onboarding/ in the Switchyard source checkout."
        )
        return

    install = runner(
        [
            "install",
            "-d",
            "-m",
            "0755",
            "-o",
            owner_user,
            "-g",
            owner_user,
            str(target_dir.parent),
            str(target_dir),
        ]
    )
    if install.returncode != 0:
        raise SystemExit(f"switchyard: failed to create onboarding docs directory {target_dir} for {owner_user}")
    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)

    source_commit = _switchyard_source_commit(source_repo, runner=runner)
    copied: list[str] = []
    skipped_existing: list[str] = []
    skipped_missing: list[str] = []
    for name in SWITCHYARD_ONBOARDING_DOC_NAMES:
        source_path = source_dir / name
        target_path = target_dir / name
        if target_path.exists():
            skipped_existing.append(name)
            continue
        try:
            body = source_path.read_text(encoding="utf-8")
        except OSError:
            skipped_missing.append(name)
            continue
        target_path.write_text(
            _stamp_onboarding_doc(source_name=name, source_commit=source_commit, body=body),
            encoding="utf-8",
        )
        _chown_project_file(owner_user=owner_user, path=target_path, runner=runner)
        copied.append(name)

    if copied:
        print_func(f"switchyard: installed onboarding docs into {target_dir}: {', '.join(copied)}")
    if skipped_existing:
        print_func(f"switchyard: skipped existing onboarding docs in {target_dir}: {', '.join(skipped_existing)}")
    if skipped_missing:
        print_func(
            "warning: switchyard: onboarding docs source "
            f"{source_dir} is incomplete; skipped missing {', '.join(skipped_missing)}. "
            "Copy them later from docs/onboarding/ in the Switchyard source checkout."
        )


def _write_initial_switchyard_project_artifact(
    *,
    project_name: str,
    slug: str,
    owner_user: str,
    project_dir: Path,
    artifact_path: Path,
    design_document: Path,
    owner_shell: str,
    implementer_roles: Sequence[str] = DEFAULT_PROJECT_IMPLEMENTER_ROLES,
    role_clis: Sequence[tuple[str, str]] | None = None,
    include_designer: bool = True,
    include_audit: bool = True,
    audit_roles: Sequence[str] | None = None,
    agy_credential_source: str = "",
    agy_credential_source_origin: str = "unset",
) -> None:
    resolved_audit_roles = tuple(audit_roles) if audit_roles is not None else (("audit",) if include_audit else ())
    role_overlap = set(implementer_roles) & set(resolved_audit_roles)
    if role_overlap:
        raise SystemExit(f"roles cannot be both implementers and auditors: {', '.join(sorted(role_overlap))}")
    artifact = ProjectDesignArtifact(
        project=slug,
        project_name=project_name,
        ticket_prefix=validate_ticket_prefix(slug),
        owner_user=owner_user,
        repository=project_dir,
        remote="origin",
        default_branch="main",
        worktree_policy="shared",
        design_document=design_document,
        implementer_roles=tuple(implementer_roles),
        audit_roles=resolved_audit_roles,
        role_clis=_dedupe_role_cli_pairs(
            role_clis
            or _default_role_cli_pairs(
                implementer_roles,
                include_designer=include_designer,
                include_audit=include_audit,
                audit_roles=resolved_audit_roles,
            )
        ),
        include_designer=include_designer,
        include_audit=bool(resolved_audit_roles),
        push_policy="director-main-only",
        gates=dict(PROJECT_DESIGN_DEFAULT_GATES),
        capability_grants={
            **PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS,
            "shell": owner_shell,
            "agy_credential_source": agy_credential_source,
            "agy_credential_source_origin": agy_credential_source_origin,
        },
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if include_designer and not design_document.exists():
        design_document.write_text(_project_design_markdown(slug, title=project_name, body="Design in progress."), encoding="utf-8")
    _write_json_atomic(artifact_path, project_design_artifact_payload(artifact))


def _chown_switchyard_project_files(
    *,
    owner_user: str,
    project_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    result = runner(["chown", "-R", f"{owner_user}:{owner_user}", str(_switchyard_dir(project_dir))])
    if result.returncode != 0:
        raise SystemExit(f"switchyard: failed to assign {_switchyard_dir(project_dir)} to {owner_user}")


def _chown_project_file(
    *,
    owner_user: str,
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    result = runner(["chown", f"{owner_user}:{owner_user}", str(path)])
    if result.returncode != 0:
        raise SystemExit(f"switchyard: failed to assign {path} to {owner_user}")


def _owner_git_args(owner_user: str, project_dir: Path, *git_args: str) -> list[str]:
    return ["sudo", "-u", owner_user, "git", "-C", str(project_dir), *git_args]


def _run_owner_git(
    owner_user: str,
    project_dir: Path,
    *git_args: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> subprocess.CompletedProcess[Any]:
    return run_owner_correct_git(
        ["git", "-C", str(project_dir), *git_args],
        runner=runner,
        owner_rules=[GitOwnerRule(project_dir, owner_user)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _ensure_project_git_repository(
    *,
    owner_user: str,
    project_dir: Path,
    branch: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    if not (project_dir / ".git").exists():
        init = _run_owner_git(owner_user, project_dir, "init", "-b", branch, runner=runner)
        if init.returncode != 0:
            raise SystemExit(
                f"switchyard: failed to initialize git repository in {project_dir}: "
                f"{_proc_failure_reason(init, f'exit {init.returncode}')}"
            )
        remote = _run_owner_git(owner_user, project_dir, "remote", "get-url", "origin", runner=runner)
        if remote.returncode != 0:
            add_remote = _run_owner_git(owner_user, project_dir, "remote", "add", "origin", str(project_dir), runner=runner)
            if add_remote.returncode != 0:
                raise SystemExit(
                    f"switchyard: failed to add local origin remote for {project_dir}: "
                    f"{_proc_failure_reason(add_remote, f'exit {add_remote.returncode}')}"
                )
        created_repository = True
    else:
        check = _run_owner_git(
            owner_user,
            project_dir,
            "rev-parse",
            "--is-inside-work-tree",
            runner=runner,
        )
        if check.returncode != 0:
            raise SystemExit(
                f"switchyard: project path {project_dir} has unusable git metadata: "
                f"{_proc_failure_reason(check, f'exit {check.returncode}')}"
            )
        created_repository = False

    head = _run_owner_git(owner_user, project_dir, "rev-parse", "--verify", "HEAD", runner=runner)
    if head.returncode == 0:
        return created_repository
    if not created_repository:
        raise SystemExit(
            f"switchyard: project path {project_dir} is already a git repository but has no initial commit; "
            "create one yourself, or remove its .git directory and let switchyard initialize it"
        )

    add = _run_owner_git(owner_user, project_dir, "add", ".", runner=runner)
    if add.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to stage initial project files in {project_dir}: "
            f"{_proc_failure_reason(add, f'exit {add.returncode}')}"
        )
    commit = _run_owner_git(
        owner_user,
        project_dir,
        "-c",
        "user.name=Switchyard",
        "-c",
        "user.email=switchyard@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "Initial Switchyard project",
        runner=runner,
    )
    if commit.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to create initial git commit in {project_dir}: "
            f"{_proc_failure_reason(commit, f'exit {commit.returncode}')}"
        )
    return True


def _require_existing_project_git_repository(
    *,
    owner_user: str,
    project_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    check = _run_owner_git(
        owner_user,
        project_dir,
        "rev-parse",
        "--is-inside-work-tree",
        runner=runner,
    )
    if check.returncode != 0:
        raise SystemExit(
            f"switchyard: --no-git-init was set, but project path {project_dir} is not a git repository; "
            "initialize it yourself before running switchyard new"
        )
    head = _run_owner_git(owner_user, project_dir, "rev-parse", "--verify", "HEAD", runner=runner)
    if head.returncode != 0:
        raise SystemExit(
            f"switchyard: --no-git-init was set, but project path {project_dir} has no initial commit; "
            "create one yourself before running switchyard new"
        )


def _commit_project_git_changes(
    *,
    owner_user: str,
    project_dir: Path,
    message: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    status = _run_owner_git(owner_user, project_dir, "status", "--porcelain", runner=runner)
    if status.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to inspect git status in {project_dir}: "
            f"{_proc_failure_reason(status, f'exit {status.returncode}')}"
        )
    if not str(getattr(status, "stdout", "") or "").strip():
        return
    add = _run_owner_git(owner_user, project_dir, "add", ".", runner=runner)
    if add.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to stage project files in {project_dir}: "
            f"{_proc_failure_reason(add, f'exit {add.returncode}')}"
        )
    commit = _run_owner_git(
        owner_user,
        project_dir,
        "-c",
        "user.name=Switchyard",
        "-c",
        "user.email=switchyard@localhost",
        "commit",
        "-m",
        message,
        runner=runner,
    )
    if commit.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to commit project files in {project_dir}: "
            f"{_proc_failure_reason(commit, f'exit {commit.returncode}')}"
        )


def _owner_project_git_runner(
    *,
    owner_user: str,
    project_dir: Path,
    owned_roots: Sequence[Path] = (),
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    normalized_roots = [_normalized_path(project_dir), *(_normalized_path(path) for path in owned_roots)]
    owner_rules = [GitOwnerRule(root, owner_user) for root in normalized_roots]

    def is_owned_path(path: Path) -> bool:
        return any(_path_is_under(path, root) for root in normalized_roots)

    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if len(args) >= 3 and args[:2] == ["mkdir", "-p"] and is_owned_path(Path(args[2])):
            return runner(["sudo", "-u", owner_user, *args], **kwargs)
        if len(args) >= 3 and args[:2] == ["test", "-x"] and is_owned_path(Path(args[2])):
            return runner(["sudo", "-u", owner_user, *args], **kwargs)
        if args[:1] == ["git"]:
            return run_owner_correct_git(args, runner=runner, owner_rules=owner_rules, **kwargs)
        return runner(args, **kwargs)

    return wrapped


def _owner_process_runner(
    *,
    owner_user: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    def wrapped(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        return runner(["sudo", "-u", owner_user, "-H", *args], **kwargs)

    return wrapped


def _control_repository_owned_roots(config: ProjectConfig) -> list[Path]:
    owned_roots: list[Path] = []
    if config.control_repository is not None:
        owned_roots.extend([config.control_repository.parent, config.control_repository])
    if config.worktree_base is not None:
        owned_roots.append(config.worktree_base)
    if config.pane_launcher is not None:
        owned_roots.extend([config.pane_launcher.parent, config.pane_launcher])
    return owned_roots


def _owner_home_for_auth(owner_user: str, fallback: Path | None = None) -> Path:
    try:
        return Path(pwd.getpwnam(owner_user).pw_dir)
    except KeyError:
        return fallback or (Path("/home") / owner_user)


def _owner_command_args(owner_user: str, command: Sequence[str]) -> list[str]:
    if owner_user == current_user_name():
        return list(command)
    return ["sudo", "-u", owner_user, *command]


def _owner_command_env_args(owner_user: str, owner_home: Path, command: Sequence[str]) -> list[str]:
    path = _prepend_paths(DEFAULT_PANE_BASE_PATH, _owner_home_bin_dirs(owner_home))
    return _owner_command_args(owner_user, ["env", f"HOME={owner_home}", f"PATH={path}", *command])


def _pane_identity_scrubbed_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if source is None else source)
    for key in PROBE_IDENTITY_ENV_KEYS:
        env.pop(key, None)
    return env


def _role_cli_name(role: RoleConfig) -> str:
    return _command_name(role.cli[0]) if role.cli else ""


def _roles_by_first_cli(config: ProjectConfig) -> dict[str, list[RoleConfig]]:
    grouped: dict[str, list[RoleConfig]] = {}
    for role in config.roles:
        cli = _role_cli_name(role)
        if cli:
            grouped.setdefault(cli, []).append(role)
    return grouped


def _role_names(roles: Sequence[RoleConfig]) -> list[str]:
    return [role.role for role in roles]


def _run_owner_cli_probe(
    *,
    owner_user: str,
    owner_home: Path,
    command: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> subprocess.CompletedProcess[Any]:
    args = _owner_command_env_args(owner_user, owner_home, command)
    try:
        return runner(
            args,
            cwd=str(owner_home),
            env=_pane_identity_scrubbed_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))


def _run_owner_cli_interactive(
    *,
    owner_user: str,
    owner_home: Path,
    cwd: Path,
    command: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> subprocess.CompletedProcess[Any]:
    args = _owner_command_env_args(owner_user, owner_home, command)
    try:
        return runner(args, cwd=str(cwd), env=_pane_identity_scrubbed_env())
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, stderr=str(exc))


def _cli_auth_probe_passed(cli: str, proc: subprocess.CompletedProcess[Any]) -> bool:
    if proc.returncode != 0:
        return False
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    combined = f"{stdout}\n{stderr}".casefold()
    if cli == "claude":
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return False
        return parsed.get("loggedIn") is True
    if cli == "hermes":
        # PGU-773 measured `hermes auth list` as a false positive: a pooled
        # manual OpenRouter credential appears there while Hermes still reports
        # no resolved API keys and opens `hermes setup`. `config check` reports
        # only environment/config-resolved keys, which is the runnable shape.
        return any(line.strip().startswith("\N{CHECK MARK} ") and "_API_KEY" in line for line in stdout.splitlines())
    if cli in {"agy", "codex"}:
        return "not logged" not in combined and "not authenticated" not in combined
    return True


def _owner_cli_is_installed(
    cli: str,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    command = FIRST_RUN_AUTH_STATUS_COMMANDS.get(cli)
    if command is None or not command:
        return True
    binary = command[0]
    proc = _run_owner_cli_probe(
        owner_user=owner_user,
        owner_home=owner_home,
        command=["sh", "-c", f"command -v {shlex.quote(binary)}"],
        runner=runner,
    )
    return proc.returncode == 0


def _cli_auth_status(
    cli: str,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    command = FIRST_RUN_AUTH_STATUS_COMMANDS.get(cli)
    if command is None:
        return "authenticated"
    proc = _run_owner_cli_probe(owner_user=owner_user, owner_home=owner_home, command=command, runner=runner)
    if _cli_auth_probe_passed(cli, proc):
        return "authenticated"
    if not _owner_cli_is_installed(
        cli,
        owner_user=owner_user,
        owner_home=owner_home,
        runner=runner,
    ):
        return "not_installed"
    return "unauthenticated"


def _model_validation_command(role: RoleConfig) -> list[str] | None:
    cli = _role_cli_name(role)
    if not cli or not role.model:
        return None
    model_args = [role.model_arg, role.model] if role.model_arg else []
    if cli == "codex":
        return [*role.cli, "exec", "--skip-git-repo-check", *model_args, MODEL_VALIDATION_PROMPT]
    if cli in {"agy", "claude"}:
        return [*role.cli, *model_args, "-p", MODEL_VALIDATION_PROMPT]
    if cli == "hermes":
        return [*role.cli, *model_args, "-z", MODEL_VALIDATION_PROMPT]
    return None


def _parse_agy_model_ids(stdout: str) -> list[str]:
    ids: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        model_id = stripped.split(maxsplit=1)[0]
        if model_id:
            ids.append(model_id)
    return list(dict.fromkeys(ids))


def _model_failure_suggestion(
    cli: str,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    if cli != "agy":
        return f"no model list is available for {cli}; validation used a one-shot prompt"
    proc = _run_owner_cli_probe(
        owner_user=owner_user,
        owner_home=owner_home,
        command=["agy", "models"],
        runner=runner,
    )
    if proc.returncode != 0:
        return "agy exposes a model list, but `agy models` failed while building suggestions"
    ids = _parse_agy_model_ids(str(getattr(proc, "stdout", "") or ""))
    if not ids:
        return "agy exposes a model list, but `agy models` returned no ids"
    return f"valid agy models include: {', '.join(ids[:8])}"


def _model_validation_passed(proc: subprocess.CompletedProcess[Any]) -> bool:
    # Codex can echo the prompt text to stderr on model failures; the prompt
    # contains "model-ok", so stderr must never satisfy the sentinel check.
    return proc.returncode == 0 and "model-ok" in str(getattr(proc, "stdout", "") or "")


def validate_role_models(
    roles: Sequence[RoleConfig],
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[ModelValidationFailure]:
    failures: list[ModelValidationFailure] = []
    for role in roles:
        cli = _role_cli_name(role)
        command = _model_validation_command(role)
        if command is None:
            continue
        proc = _run_owner_cli_probe(
            owner_user=owner_user,
            owner_home=owner_home,
            command=command,
            runner=runner,
        )
        if _model_validation_passed(proc):
            continue
        reason = _proc_failure_reason(proc, f"model probe failed with exit {proc.returncode}")
        if proc.returncode == 0:
            reason = "model probe did not confirm model-ok"
        failures.append(
            ModelValidationFailure(
                role=role.role,
                cli=cli,
                model=role.model,
                reason=reason,
                suggestion=_model_failure_suggestion(
                    cli,
                    owner_user=owner_user,
                    owner_home=owner_home,
                    runner=runner,
                ),
            )
        )
    return failures


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_toml_object(path: Path) -> dict[str, Any]:
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _claude_workdir_is_trusted(owner_home: Path, workdir: Path) -> bool:
    config = _read_json_object(owner_home / ".claude.json")
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return False
    for path in _trust_path_candidates(workdir):
        entry = projects.get(path)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def _agy_workdir_is_trusted(owner_home: Path, workdir: Path) -> bool:
    settings = _read_json_object(owner_home / ".gemini" / "antigravity-cli" / "settings.json")
    trusted = settings.get("trustedWorkspaces")
    if not isinstance(trusted, list):
        return False
    candidates = set(_trust_path_candidates(workdir))
    return any(str(path) in candidates for path in trusted)


def _trust_path_candidates(workdir: Path) -> list[str]:
    expanded = workdir.expanduser()
    candidates = [str(expanded), str(expanded.resolve(strict=False))]
    return list(dict.fromkeys(candidates))


def _workdir_is_trusted(cli: str, *, owner_home: Path, workdir: Path) -> bool:
    if cli == "claude":
        return _claude_workdir_is_trusted(owner_home, workdir)
    if cli == "agy":
        return _agy_workdir_is_trusted(owner_home, workdir)
    return True


def _first_run_trust_command(role: RoleConfig) -> list[str]:
    # Trust is directory-scoped, so the minimal interactive CLI is enough to collect it.
    # Detached roles do not have a visible pane where the prompt can appear.
    return list(role.cli)


def stale_codex_hook_trust_for_roles(
    roles: Sequence[RoleConfig],
    *,
    owner_home: Path,
) -> list[CodexHookTrustMismatch]:
    codex_roles = [role for role in roles if _role_cli_name(role) == "codex"]
    if not codex_roles:
        return []
    hook_entries = _codex_command_hook_trust_entries(owner_home)
    if not hook_entries:
        return []
    trusted_hashes = _codex_trusted_hashes(owner_home)
    affected_roles = tuple(_role_names(codex_roles))
    mismatches: list[CodexHookTrustMismatch] = []
    for event_key, hook_key, expected_hash in hook_entries:
        trusted_hash = trusted_hashes.get(hook_key, "")
        if trusted_hash == expected_hash:
            continue
        mismatches.append(
            CodexHookTrustMismatch(
                event=event_key,
                hook_key=hook_key,
                expected_hash=expected_hash,
                trusted_hash=trusted_hash,
                affected_roles=affected_roles,
            )
        )
    return mismatches


def _codex_hook_trust_reason(mismatch: CodexHookTrustMismatch) -> str:
    if mismatch.trusted_hash:
        return "hooks changed, re-approve"
    return "new project, never trusted"


def _codex_hook_trust_affected_roles(mismatches: Sequence[CodexHookTrustMismatch]) -> list[str]:
    roles: list[str] = []
    for mismatch in mismatches:
        for role in mismatch.affected_roles:
            if role not in roles:
                roles.append(role)
    return roles


def _format_codex_hook_trust_report(
    mismatches: Sequence[CodexHookTrustMismatch],
    *,
    owner_user: str,
) -> str:
    count = len(mismatches)
    plural = "" if count == 1 else "s"
    owner = f"owner user {owner_user}" if owner_user else "the project owner user"
    roles = ", ".join(_codex_hook_trust_affected_roles(mismatches)) or "none"
    approvals = "; ".join(f"{mismatch.event} ({_codex_hook_trust_reason(mismatch)})" for mismatch in mismatches)
    return (
        f"codex hook trust needs {count} approval{plural} for {owner}; "
        f"run /hooks once in any Codex pane as that owner. "
        f"This trust is shared by affected Codex roles: {roles}. "
        f"Approvals: {approvals}"
    )


def build_first_run_setup_manifest(
    config: ProjectConfig,
    *,
    owner_user: str,
    owner_home: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> FirstRunSetupManifest:
    roles_by_cli = _roles_by_first_cli(config)
    login_steps: list[FirstRunAuthLoginStep] = []
    missing_cli_roles: dict[str, list[str]] = {}

    for cli, roles in roles_by_cli.items():
        if cli not in FIRST_RUN_AUTH_STATUS_COMMANDS:
            continue
        auth_status = _cli_auth_status(cli, owner_user=owner_user, owner_home=owner_home, runner=runner)
        if auth_status == "authenticated":
            continue
        names = _role_names(roles)
        if auth_status == "not_installed":
            missing_cli_roles[cli] = names
            continue
        login_steps.append(
            FirstRunAuthLoginStep(
                cli=cli,
                roles=tuple(names),
                command=tuple(FIRST_RUN_AUTH_LOGIN_COMMANDS[cli]),
            )
        )

    folder_trust_steps: list[FirstRunFolderTrustStep] = []
    for role in config.roles:
        if not role.detached:
            continue
        cli = _role_cli_name(role)
        if cli not in FIRST_RUN_TRUST_CLIS:
            continue
        workdir = Path(role.workdir)
        if _workdir_is_trusted(cli, owner_home=owner_home, workdir=workdir):
            continue
        folder_trust_steps.append(
            FirstRunFolderTrustStep(
                cli=cli,
                role=role.role,
                workdir=workdir,
                command=tuple(_first_run_trust_command(role)),
            )
        )

    return FirstRunSetupManifest(
        owner_user=owner_user,
        login_steps=login_steps,
        folder_trust_steps=folder_trust_steps,
        stale_codex_hook_trust=stale_codex_hook_trust_for_roles(config.roles, owner_home=owner_home),
        missing_cli_roles=missing_cli_roles,
        owner_shell_issue=_owner_shell_issue(owner_user),
    )


def _format_first_run_setup_manifest(manifest: FirstRunSetupManifest) -> list[str]:
    if not manifest.has_steps:
        return []
    missing_cli_count = len(manifest.missing_cli_roles)
    login_count = len(manifest.login_steps)
    folder_trust_count = len(manifest.folder_trust_steps)
    hook_trust_count = len(manifest.stale_codex_hook_trust)
    owner_shell_issue_count = 1 if manifest.owner_shell_issue else 0
    summary = (
        f"switchyard: first-run setup manifest for owner user {manifest.owner_user}: "
        f"{login_count} login step(s), {folder_trust_count} folder trust step(s), "
        f"{hook_trust_count} codex hook approval(s), {missing_cli_count} missing CLI(s)"
    )
    if owner_shell_issue_count:
        summary = f"{summary}, {owner_shell_issue_count} owner shell issue(s)"
    lines = [
        summary
    ]
    if manifest.owner_shell_issue:
        issue = manifest.owner_shell_issue
        lines.append(
            f"switchyard: owner shell for {issue.owner_user} is not executable: {issue.shell}; "
            f"repair with `{issue.remedy}`"
        )
    for cli, roles in manifest.missing_cli_roles.items():
        lines.append(
            f"switchyard: missing CLI {cli} (affected roles: {', '.join(roles)}): "
            f"{_missing_cli_install_clause(cli, manifest.owner_user)}"
        )
    if manifest.missing_cli_roles:
        lines.append(_owner_user_cli_reminder(manifest.owner_user))
    for step in manifest.login_steps:
        lines.append(
            f"switchyard: login {step.cli}: roles {', '.join(step.roles)}; "
            f"interactive account setup running {shlex.join(step.command)} as {manifest.owner_user}"
        )
    for step in manifest.folder_trust_steps:
        lines.append(
            f"switchyard: folder trust {step.cli}: role {step.role} at {step.workdir}; "
            "recurs per project/workdir even when the owner user is reused; "
            "interactive repository trust today, not account login"
        )
    if manifest.stale_codex_hook_trust:
        lines.append(
            "switchyard: manual security approval: "
            + _format_codex_hook_trust_report(
                manifest.stale_codex_hook_trust,
                owner_user=manifest.owner_user,
            )
        )
    return lines


def print_first_run_setup_manifest(
    manifest: FirstRunSetupManifest,
    *,
    print_func: Callable[[str], None] = print,
) -> None:
    for line in _format_first_run_setup_manifest(manifest):
        print_func(line)


def _prepare_first_run_auth_worktrees(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    if config.repository is None:
        return
    worktree_runner = runner
    owner_runner_anchor = config.repository or config.pane_launcher
    if config.run_as_user and owner_runner_anchor is not None and (
        config.control_repository is not None or config.pane_launcher is not None
    ):
        worktree_runner = _owner_project_git_runner(
            owner_user=config.run_as_user,
            project_dir=owner_runner_anchor,
            owned_roots=_control_repository_owned_roots(config),
            runner=runner,
        )
    ensure_project_worktrees(config, refresh=True, runner=worktree_runner)


def run_first_run_auth_phase(
    config: ProjectConfig,
    *,
    owner_user: str | None = None,
    owner_home: Path | None = None,
    validate_models: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> FirstRunAuthReport:
    if config.desktop_access is not None:
        config = prepare_project_desktop(config, runner=runner)
        desktop_env = {k:v for k,v in config.roles[0].env.items() if k in DESKTOP_ENV_KEYS}
        base_runner = runner
        def runner(args, **kwargs):
            args = list(args)
            if "env" in args:
                position = args.index("env") + 1
                args[position:position] = [*_env_unset_prefix(DESKTOP_ENV_KEYS), *_env_prefix(desktop_env)]
            environment = dict(kwargs.get("env", os.environ))
            for key in DESKTOP_ENV_KEYS:
                environment.pop(key, None)
            environment.update(desktop_env)
            kwargs["env"] = environment
            return base_runner(args, **kwargs)
    effective_owner = (owner_user or config.run_as_user or current_user_name()).strip()
    if not effective_owner:
        return FirstRunAuthReport({}, [])
    effective_home = owner_home or _owner_home_for_auth(effective_owner)
    unauthenticated: dict[str, list[str]] = {}
    missing_cli_roles: dict[str, list[str]] = {}

    manifest = build_first_run_setup_manifest(
        config,
        owner_user=effective_owner,
        owner_home=effective_home,
        runner=runner,
    )
    print_first_run_setup_manifest(manifest, print_func=print_func)
    missing_cli_roles.update(manifest.missing_cli_roles)

    for step in manifest.login_steps:
        login_command = list(step.command)
        _run_owner_cli_interactive(
            owner_user=effective_owner,
            owner_home=effective_home,
            cwd=effective_home,
            command=login_command,
            runner=runner,
        )
        auth_status = _cli_auth_status(step.cli, owner_user=effective_owner, owner_home=effective_home, runner=runner)
        if auth_status == "not_installed":
            missing_cli_roles[step.cli] = list(step.roles)
        elif auth_status != "authenticated":
            unauthenticated[step.cli] = list(step.roles)

    untrusted: list[tuple[str, str, str]] = []
    for step in manifest.folder_trust_steps:
        _run_owner_cli_interactive(
            owner_user=effective_owner,
            owner_home=effective_home,
            cwd=step.workdir,
            command=list(step.command),
            runner=runner,
        )
        if not _workdir_is_trusted(step.cli, owner_home=effective_home, workdir=step.workdir):
            untrusted.append((step.cli, step.role, str(step.workdir)))

    model_validation_failures: list[ModelValidationFailure] = []
    if validate_models:
        skipped_clis = set(unauthenticated) | set(missing_cli_roles)
        model_roles = [role for role in config.roles if _role_cli_name(role) not in skipped_clis]
        model_validation_failures = validate_role_models(
            model_roles,
            owner_user=effective_owner,
            owner_home=effective_home,
            runner=runner,
        )

    return FirstRunAuthReport(
        unauthenticated,
        untrusted,
        manifest.stale_codex_hook_trust,
        missing_cli_roles,
        model_validation_failures,
        owner_user=effective_owner
        if missing_cli_roles or manifest.stale_codex_hook_trust or manifest.owner_shell_issue
        else "",
        owner_shell_issue=manifest.owner_shell_issue,
    )


def report_first_run_auth_warnings(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> None:
    owner_detail = f" for owner user {report.owner_user}" if report.owner_user else ""
    for cli, roles in report.missing_cli_roles.items():
        print_func(
            f"warning: switchyard: {cli} is not installed{owner_detail} "
            f"(affected roles: {', '.join(roles)}); "
            f"{_missing_cli_install_clause(cli, report.owner_user)}"
        )
    if report.owner_shell_issue:
        issue = report.owner_shell_issue
        print_func(
            f"warning: switchyard: owner shell for {issue.owner_user} is not executable: "
            f"{issue.shell}; repair with `{issue.remedy}`"
        )
    for cli, roles in report.unauthenticated_roles.items():
        print_func(
            f"warning: switchyard: {cli} is still unauthenticated; affected roles: {', '.join(roles)}"
        )
    for cli, role, workdir in report.untrusted_roles:
        print_func(f"warning: switchyard: {cli} workspace still untrusted for {role}: {workdir}")
    if report.stale_codex_hook_trust:
        print_func(
            "warning: switchyard: "
            + _format_codex_hook_trust_report(report.stale_codex_hook_trust, owner_user=report.owner_user)
        )
    for failure in report.model_validation_failures:
        print_func(
            f"warning: switchyard: model validation failed for role {failure.role} "
            f"(cli {failure.cli}, model {failure.model}): {failure.reason}; {failure.suggestion}"
        )


def _format_missing_cli_launch_failure(report: FirstRunAuthReport) -> str:
    owner_detail = f" for owner user {report.owner_user}" if report.owner_user else ""
    cli_details = "; ".join(
        f"{cli} (roles: {', '.join(roles)})" for cli, roles in report.missing_cli_roles.items()
    )
    lines = [
        f"switchyard: cannot launch panes because required CLI(s) are missing{owner_detail}: "
        f"{cli_details}. Install the missing CLI(s){owner_detail} and rerun switchyard.",
        _owner_user_cli_reminder(report.owner_user),
    ]
    width = max((len(cli) for cli in report.missing_cli_roles), default=0)
    for cli in report.missing_cli_roles:
        command = AGENT_CLI_INSTALL_COMMANDS.get(cli, "")
        detail = command or "see that vendor's own installation documentation"
        lines.append(f"switchyard:   {cli.ljust(width)}  {detail}")
    lines.append(
        "switchyard: these commands are yours to run; switchyard does not install agent CLIs."
    )
    return "\n".join(lines)


def stop_before_launch_for_missing_owner_clis(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> bool:
    if not report.missing_cli_roles:
        return False
    print_func(_format_missing_cli_launch_failure(report))
    return True


def run_switchyard_launch_first_run_auth(
    config: ProjectConfig,
    *,
    validate_models: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> FirstRunAuthReport:
    owner_user = (config.run_as_user or current_user_name()).strip()
    if not owner_user:
        return FirstRunAuthReport({}, [])
    return run_first_run_auth_phase(
        config,
        owner_user=owner_user,
        owner_home=_owner_home_for_auth(owner_user),
        validate_models=validate_models,
        runner=runner,
        print_func=print_func,
    )


def _resolve_owner_shell_path(shell: str) -> str:
    requested = shell.strip()
    if not requested:
        raise SystemExit("switchyard: owner shell cannot be empty")
    if "/" in requested:
        if os.access(requested, os.X_OK):
            return requested
        raise SystemExit(
            f"switchyard: owner shell {requested!r} is not executable; "
            "install it or choose an installed shell"
        )
    resolved = shutil.which(requested)
    if resolved:
        return resolved
    if requested == PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"]:
        for fallback in ("/bin/bash", "/bin/sh"):
            if os.access(fallback, os.X_OK):
                return fallback
        raise SystemExit(
            "switchyard: default owner shell 'fish' is unavailable and no fallback shell "
            "(/bin/bash or /bin/sh) is executable"
        )
    raise SystemExit(
        f"switchyard: owner shell {requested!r} was not found on PATH; "
        "install it or choose an installed shell"
    )


def _owner_project_install_args(owner_user: str, project_dir: Path, *, shell: str = "fish") -> list[list[str]]:
    shell_path = _resolve_owner_shell_path(shell)
    return [
        ["id", "-u", owner_user],
        ["useradd", "-m", "-s", shell_path, owner_user],
        ["install", "-d", "-m", "0755", "-o", owner_user, "-g", owner_user, str(project_dir)],
    ]


def _non_login_shell_path() -> str:
    for candidate in (shutil.which("nologin"), "/usr/sbin/nologin", "/bin/false"):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit(
        "switchyard: cannot create board service user because neither nologin nor /bin/false "
        "is executable; install util-linux or provide a non-login shell"
    )


def _ensure_board_service_user(
    service_user: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    result = runner(["getent", "passwd", service_user], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        return
    shell_path = _non_login_shell_path()
    create_result = runner(["useradd", "-r", "-M", "-d", "/nonexistent", "-s", shell_path, service_user])
    if create_result.returncode != 0:
        raise SystemExit(
            f"switchyard: failed to create board service user {service_user!r}; "
            "run scripts/ticket-board-boardsvc-setup.sh --apply or create a system account "
            f"with `sudo useradd -r -M -d /nonexistent -s {shell_path} {service_user}`"
        )


def _ensure_board_service_peer_auth(
    plan: ProjectBoardProvision,
    *,
    source_repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    setup_script = source_repo / "scripts" / "ticket-board-boardsvc-setup.sh"
    env = {
        **os.environ,
        "PG_DATABASE": plan.database,
        "PG_IDENT_MAP": DEFAULT_PG_IDENT_MAP,
        "SERVICE_USER": plan.service_user,
        "SERVICE_ROLE": plan.service_role,
    }
    try:
        result = runner([str(setup_script), "--apply-peer-auth"], env=env)
    except OSError as exc:
        raise SystemExit(
            "switchyard: failed to install PostgreSQL peer-auth mapping "
            f"for OS user {plan.service_user!r} to database role {plan.service_role!r} "
            f"on database {plan.database!r}; setup helper {setup_script} is not executable "
            "or is missing. Run scripts/ticket-board-boardsvc-setup.sh --apply or install "
            "the pg_hba/pg_ident mapping manually before provisioning"
        ) from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(
            "switchyard: failed to install PostgreSQL peer-auth mapping "
            f"for OS user {plan.service_user!r} to database role {plan.service_role!r} "
            f"on database {plan.database!r}; run scripts/ticket-board-boardsvc-setup.sh --apply "
            "or install the pg_hba/pg_ident mapping manually before provisioning"
            f"{detail}"
        )


def _enable_owner_linger_args(owner_user: str) -> list[str]:
    return ["loginctl", "enable-linger", owner_user]


def _owner_linger_show_args(owner_user: str) -> list[str]:
    return ["loginctl", "show-user", owner_user, "-p", "Linger", "--value"]


def _owner_linger_is_enabled(
    owner_user: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> bool:
    result = runner(
        _owner_linger_show_args(owner_user),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.returncode == 0 and str(getattr(result, "stdout", "") or "").strip() == "yes"


def _group_ids_for_user(user_name: str) -> set[int]:
    try:
        user = pwd.getpwnam(user_name)
    except KeyError:
        return set()
    gids = {int(user.pw_gid)}
    try:
        import grp
    except ImportError:
        return gids
    for group in grp.getgrall():
        if user_name in group.gr_mem:
            gids.add(int(group.gr_gid))
    return gids


def _existing_project_path_is_usable(project_dir: Path, owner_user: str) -> bool:
    try:
        info = project_dir.stat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    uid = uid_for_user(owner_user)
    if uid is not None and int(info.st_uid) == uid:
        mask = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    elif uid is not None and int(info.st_gid) in _group_ids_for_user(owner_user):
        mask = stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP
    else:
        mask = stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
    return (info.st_mode & mask) == mask


def _precheck_project_path_before_mutating(owner_user: str, project_dir: Path) -> None:
    if not project_dir.exists():
        return
    if not project_dir.is_dir():
        raise SystemExit(f"switchyard: project path {project_dir} already exists but is not a directory")
    if not _existing_project_path_is_usable(project_dir, owner_user):
        raise SystemExit(
            f"switchyard: project path {project_dir} already exists but is not readable and writable "
            f"by {owner_user}; choose a writable path or fix ownership/permissions first"
        )


def _verify_project_path_writable_by_owner(
    owner_user: str,
    project_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    quoted_path = shlex.quote(str(project_dir))
    result = runner(
        [
            "sudo",
            "-u",
            owner_user,
            "sh",
            "-lc",
            f"test -d {quoted_path} && test -r {quoted_path} && test -w {quoted_path} && test -x {quoted_path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"switchyard: project path {project_dir} is not readable and writable by {owner_user}; "
            "choose a writable path or fix ownership/permissions first"
        )


def _ensure_owner_user_and_project_dir(
    owner_user: str,
    project_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    shell: str = "fish",
) -> OwnerUserProvisionResult:
    existed = project_dir.exists()
    _precheck_project_path_before_mutating(owner_user, project_dir)
    id_args = ["id", "-u", owner_user]
    install_args = ["install", "-d", "-m", "0755", "-o", owner_user, "-g", owner_user, str(project_dir)]
    id_result = runner(id_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created_user = False
    linger_enabled = False
    shell_path = ""
    if id_result.returncode != 0:
        _id_args, useradd_args, _install_args = _owner_project_install_args(owner_user, project_dir, shell=shell)
        shell_path = useradd_args[useradd_args.index("-s") + 1]
        useradd_result = runner(useradd_args)
        if useradd_result.returncode != 0:
            raise SystemExit(f"switchyard: failed to create user {owner_user!r}")
        created_user = True
        linger_result = runner(_enable_owner_linger_args(owner_user))
        if linger_result.returncode != 0:
            raise SystemExit(f"switchyard: failed to enable linger for created user {owner_user!r}")
        linger_enabled = True
    elif not _owner_linger_is_enabled(owner_user, runner=runner):
        raise SystemExit(
            f"switchyard: existing user {owner_user!r} does not have linger enabled; "
            "switchyard refuses to modify an existing owner user"
        )
    if not existed:
        install_result = runner(install_args)
        if install_result.returncode != 0:
            raise SystemExit(f"switchyard: failed to create project directory {project_dir}")
        project_dir.mkdir(parents=True, exist_ok=True)
    _verify_project_path_writable_by_owner(owner_user, project_dir, runner=runner)
    return OwnerUserProvisionResult(created=created_user, linger_enabled=linger_enabled, shell_path=shell_path)


def _agy_source_token_path(source_user: str, home_base: Path) -> Path:
    return home_base / source_user / AGY_CREDENTIAL_DIR_NAME / AGY_CREDENTIAL_TOKEN_NAME


def _uid_for_user(user_name: str) -> int | None:
    try:
        return int(pwd.getpwnam(user_name).pw_uid)
    except KeyError:
        return None


def _open_agy_source_token(source_user: str, home_base: Path) -> int:
    """Open the opted-in source token and return a validated read-only fd.

    Opened with O_NOFOLLOW and validated by fstat on the fd rather than by stat on the
    path. Both matter: the source home is writable by an unprivileged account, so a
    path-based check would let that account swap the token for a symlink to any
    root-readable file between the check and a root-run copy. The caller closes the fd.
    """
    source_token = _agy_source_token_path(source_user, home_base)
    # Resolved before the file is touched, and never optional: without a uid to compare
    # against there is no owner boundary at all, and a deleted or mistyped account whose
    # stale home survives would be accepted on the strength of the filename alone.
    expected_uid = _uid_for_user(source_user)
    if expected_uid is None:
        raise SystemExit(
            f"switchyard: agy_credential_source {source_user!r} is not a user on this machine; "
            "seeding copies a token only from an existing account that owns it"
        )
    try:
        fd = os.open(source_token, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SystemExit(
                f"switchyard: {source_token} is a symlink; agy credential seeding copies only a "
                "regular file from the source user's own home and will not follow a link out of it"
            ) from exc
        raise SystemExit(
            f"switchyard: agy credential seeding was requested from {source_user!r}, but "
            f"{source_token} could not be read ({exc.strerror}); sign in to agy as "
            f"{source_user} first, or clear capability_grants.agy_credential_source"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(
                f"switchyard: {source_token} is not a regular file; refusing to seed from it"
            )
        if info.st_uid != expected_uid:
            raise SystemExit(
                f"switchyard: {source_token} is owned by uid {info.st_uid}, not {source_user} "
                f"(uid {expected_uid}); refusing to copy a token that user does not own"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _validate_agy_credential_source(source_user: str, owner_user: str, home_base: Path) -> None:
    """Reject an unusable credential source before provisioning mutates anything.

    Called ahead of the first mutation so a bad source cannot leave a half-provisioned
    project behind: the owner user would then exist, and seeding deliberately refuses to
    touch an existing owner, so the retry could never reach the requested end state.
    """
    if not _is_valid_owner_user_name(source_user):
        raise SystemExit(
            f"switchyard: agy_credential_source {source_user!r} is not a plain Unix user name"
        )
    if source_user == owner_user:
        raise SystemExit(
            f"switchyard: agy_credential_source {source_user!r} is the project owner user itself; "
            "seeding needs a different source account"
        )
    os.close(_open_agy_source_token(source_user, home_base))


AGY_CREDENTIAL_INSTALLED = "installed"
AGY_CREDENTIAL_ABSENT = "absent"
AGY_CREDENTIAL_UNUSABLE = "unusable"


def _agy_credential_state(owner_user: str, home_base: Path) -> str:
    """Whether the owner already holds a usable seeded token.

    The seeding decision is made on this, not on whether this run created the owner: a
    seed that failed after owner creation must still be completable by a retry, and a
    retry that skipped seeding would let provisioning finish and record a credential
    source that was never installed.
    """
    target = home_base / owner_user / AGY_CREDENTIAL_DIR_NAME / AGY_CREDENTIAL_TOKEN_NAME
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return AGY_CREDENTIAL_ABSENT
    except OSError:
        return AGY_CREDENTIAL_UNUSABLE
    try:
        info = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        return AGY_CREDENTIAL_UNUSABLE
    # Ownership is the point of the check, not a refinement of it: a 0600 token owned by
    # root or by any other account is unreadable to the pane owner, so treating it as
    # installed would complete provisioning and record a credential agy still cannot use.
    # Unresolvable owner means no uid to compare, which is not a pass.
    owner_uid = _uid_for_user(owner_user)
    if owner_uid is None or info.st_uid != owner_uid:
        return AGY_CREDENTIAL_UNUSABLE
    return AGY_CREDENTIAL_INSTALLED


def _has_usable_agy_token(user_name: str, home_base: Path) -> bool:
    try:
        os.close(_open_agy_source_token(user_name, home_base))
    except SystemExit:
        return False
    return True


def _resolve_agy_credential_source(
    *,
    override: str | None,
    opt_out: bool,
    artifact_value: str,
    home_base: Path,
    settings_path: Path | None,
    yes: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> tuple[str, str]:
    """Decide the credential source and where the decision came from.

    The invoking human is only ever a *proposal*: it is offered interactively and stored
    only once confirmed. A noninteractive run uses a recorded host setting or an explicit
    override and otherwise seeds nothing, so SUDO_USER is never inferred silently.
    """
    if opt_out:
        return "", AGY_SOURCE_FROM_OPT_OUT
    explicit = (override or "").strip()
    if explicit:
        if not _is_valid_owner_user_name(explicit):
            raise SystemExit(
                f"switchyard: --agy-credential-source must be a plain Unix user name, not "
                f"{explicit!r}"
            )
        return explicit, AGY_SOURCE_FROM_OVERRIDE
    if artifact_value:
        return artifact_value, AGY_SOURCE_FROM_OVERRIDE
    recorded = read_host_agy_credential_source(settings_path)
    if recorded:
        return recorded, AGY_SOURCE_FROM_HOST
    if yes:
        return "", AGY_SOURCE_UNSET
    proposed = default_gui_user()
    if (
        not proposed
        or proposed == "root"
        or not _is_valid_owner_user_name(proposed)
        or _uid_for_user(proposed) is None
        or not _has_usable_agy_token(proposed, home_base)
    ):
        # Nothing worth offering: no invoking human, or that account has no agy token to
        # share. Offering one anyway would train the operator to accept a prompt that
        # cannot work.
        return "", AGY_SOURCE_UNSET
    print_func(
        f"switchyard: no agy credential source is recorded for this host. Seeding one lets "
        f"each new project's panes use agy without their own login, by sharing the Google "
        f"account that user signed in with."
    )
    if not _prompt_bool(
        f"Record {proposed} as this host's agy credential source", default=False, input_func=input_func
    ):
        print_func(
            "switchyard: continuing without an agy credential source; set one later with "
            "`switchyard agy-credential set USER`"
        )
        return "", AGY_SOURCE_UNSET
    path = write_host_agy_credential_source(proposed, settings_path=settings_path, confirmed_by=proposed)
    print_func(f"switchyard: recorded agy credential source {proposed} ({path})")
    return proposed, AGY_SOURCE_FROM_HOST


def _agy_credential_setting_path(settings_path: Path | None = None) -> Path:
    return settings_path or DEFAULT_AGY_CREDENTIAL_SETTING_PATH


def read_host_agy_credential_source(settings_path: Path | None = None) -> str:
    """The account this host seeds agy credentials from, or empty when unset."""
    path = _agy_credential_setting_path(settings_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    source = str(raw.get("source_user") or "").strip()
    if source and not _is_valid_owner_user_name(source):
        raise SystemExit(
            f"switchyard: {path} records source_user {source!r}, which is not a plain Unix "
            "user name; fix or clear it with `switchyard agy-credential clear`"
        )
    return source


def write_host_agy_credential_source(
    source_user: str,
    *,
    settings_path: Path | None = None,
    confirmed_by: str = "",
) -> Path:
    path = _agy_credential_setting_path(settings_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        path,
        {
            "source_user": source_user,
            "confirmed_by": confirmed_by or current_user_name(),
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return path


def switchyard_seed_role_credentials_command(
    config: ProjectConfig,
    *,
    role_name: str = "",
    reseed: bool = False,
    home_base: Path = Path("/home"),
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    """Give each role its own private copy of the credentials its CLI needs.

    Idempotent: a role that already has a credential is left alone unless
    --reseed is passed, which is the deliberate repair/sync path. Nothing here
    changes how the board derives a role -- that stays SO_PEERCRED uid -- and
    nothing is shared between roles: each copy is owned by that role, 0600,
    under a 0700 directory (SYRD-39).
    """
    owner_user = config.run_as_user or current_user_name()
    roles = [role for role in config.roles if not role_name or role.role == role_name]
    if role_name and not roles:
        raise SystemExit(f"switchyard: {config.project} has no role {role_name}")
    seeded = 0
    for role in roles:
        account = role_run_as_user(config, role)
        if not account or account == owner_user:
            print_func(f"switchyard: {role.role} has no Unix account of its own; nothing to seed")
            continue
        cli_name = _command_name(role.cli[0]) if role.cli else ""
        artifacts = role_credential_artifacts(role)
        if not artifacts:
            raise SystemExit(
                f"switchyard: no credential allowlist for {cli_name or 'unknown cli'} used by "
                f"{role.role}; refusing to guess what to copy"
            )
        seeded_for_role = 0
        for artifact in artifacts:
            if seed_role_credential(
                account=account,
                owner_user=owner_user,
                artifact=artifact,
                target=_role_credential_target(config, role, artifact, home_base=home_base),
                home_base=home_base,
                reseed=reseed,
                runner=runner,
                print_func=print_func,
            ):
                seeded += 1
                seeded_for_role += 1
        if not seeded_for_role and all(not artifact.required for artifact in artifacts):
            # Nothing was copied. That is fine when the role already holds one
            # of the alternatives -- this command is idempotent -- and a failure
            # when it holds none, because the owner is then not authenticated
            # for this CLI and the role must not start.
            already_held = any(
                not _credential_state(
                    *_role_credential_target(config, role, artifact, home_base=home_base),
                    account,
                    private=True,
                )
                for artifact in artifacts
            )
            if not already_held:
                raise SystemExit(
                    f"switchyard: {owner_user} has none of "
                    + ", ".join(artifact.relative_path for artifact in artifacts)
                    + f"; authenticate {cli_name} as {owner_user} first"
                )
    print_func(
        f"switchyard: seeded {seeded} credential file(s); every role now acts as the same "
        "model-provider account as the owner, and shares that account's quota, while keeping "
        "its own filesystem and its own board role"
    )
    return 0


def switchyard_agy_credential_command(
    action: str,
    *,
    source_user: str | None = None,
    settings_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    """show / set / clear the host-wide agy credential source."""
    path = _agy_credential_setting_path(settings_path)
    if action == "show":
        current = read_host_agy_credential_source(settings_path)
        if current:
            print_func(f"switchyard: agy credential source: {current} ({path})")
        else:
            print_func(f"switchyard: no agy credential source recorded ({path})")
        return 0
    if action == "set":
        candidate = (source_user or "").strip()
        if not _is_valid_owner_user_name(candidate):
            raise SystemExit(
                f"switchyard: agy credential source must be a plain Unix user name, not "
                f"{candidate!r}"
            )
        if _uid_for_user(candidate) is None:
            raise SystemExit(f"switchyard: {candidate!r} is not a user on this machine")
        write_host_agy_credential_source(candidate, settings_path=settings_path)
        print_func(f"switchyard: agy credential source set to {candidate} ({path})")
        print_func(
            f"switchyard: new projects will seed from {candidate}, so each project's role "
            "panes can act as the Google account it signed in to agy with"
        )
        return 0
    if action == "clear":
        try:
            path.unlink()
        except FileNotFoundError:
            print_func(f"switchyard: no agy credential source recorded ({path})")
            return 0
        print_func(f"switchyard: cleared agy credential source ({path})")
        return 0
    raise SystemExit(f"switchyard: unknown agy-credential action {action!r}")


def _owner_can_traverse(info: os.stat_result, owner_user: str, owner_uid: int) -> bool:
    """Whether owner_user has execute permission on a directory, by the POSIX rule."""
    if info.st_uid == owner_uid:
        return bool(info.st_mode & stat.S_IXUSR)
    if info.st_gid in _group_ids_for_user(owner_user):
        return bool(info.st_mode & stat.S_IXGRP)
    return bool(info.st_mode & stat.S_IXOTH)


def _require_owner_home_traversable(dir_fd: int, owner_user: str, home: Path) -> None:
    """Refuse an owner home the owner cannot enter.

    Only reachability is required here, not ownership: a home is not switchyard's to
    dictate, and one owned by root but world-executable is genuinely fine. A
    Director-approved pre-existing owner may have a home this function did not create, so
    it is proven rather than assumed -- a credential under a home the owner cannot
    traverse is exactly the unreadable-token failure this ticket exists to remove.
    """
    owner_uid = _uid_for_user(owner_user)
    if owner_uid is None:
        raise SystemExit(
            f"switchyard: {owner_user!r} is not a user on this machine, so {home} cannot "
            "be shown to be reachable by it"
        )
    info = os.fstat(dir_fd)
    if not _owner_can_traverse(info, owner_user, owner_uid):
        raise SystemExit(
            f"switchyard: home {home} is owned by uid {info.st_uid} with mode "
            f"{stat.S_IMODE(info.st_mode):04o}, so {owner_user} (uid {owner_uid}) could "
            "not enter it and agy could not read a credential below it; fix the home's "
            "ownership or permissions and rerun"
        )


def _require_owner_traversable(dir_fd: int, owner_user: str, component: str, target_dir: Path) -> None:
    """Refuse a pre-existing component the pane owner cannot enter.

    A directory switchyard did not create carries no guarantee at all: it may be left
    over from a run whose ownership assignment failed, or simply be someone else's. A
    token written beneath one the owner cannot traverse is unreachable to agy, which is
    the very failure this ticket exists to fix, so it is a refusal rather than a warning.
    """
    owner_uid = _uid_for_user(owner_user)
    if owner_uid is None:
        raise SystemExit(
            f"switchyard: {owner_user!r} is not a user on this machine, so {target_dir} "
            "cannot be shown to be reachable by it"
        )
    info = os.fstat(dir_fd)
    if info.st_uid != owner_uid or not info.st_mode & stat.S_IXUSR:
        raise SystemExit(
            f"switchyard: existing directory {component!r} of {target_dir} is owned by uid "
            f"{info.st_uid} with mode {stat.S_IMODE(info.st_mode):04o}, so {owner_user} "
            f"(uid {owner_uid}) could not enter it and agy could not read a credential "
            "below it; remove or fix that directory and rerun"
        )


# Hermes reads provider keys from its own home. The owner's authenticated state
# lives in ~/.hermes; a role reads the same two files from the per-role
# HERMES_HOME the launcher already gives it, so nothing has to be injected into
# the environment and no shell file is ever sourced (SYRD-39).
HERMES_OWNER_CREDENTIAL_DIR = ".hermes"
# Hermes keeps model-provider keys in the SAME .env as SUDO_PASSWORD, messaging
# bot tokens, GitHub tokens, tool credentials and terminal SSH keys -- its own
# terminal tool consumes SUDO_PASSWORD. Copying that file into every role would
# hand each one the owner's login password and external identities, which is the
# opposite of what per-role accounts are for. Only these keys cross, and a new
# file is constructed rather than the source copied (SYRD-39).
HERMES_PROVIDER_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "ARCEEAI_API_KEY",
        "ARCEE_BASE_URL",
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_BASE_URL",
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
        "GLM_API_KEY",
        "GLM_BASE_URL",
        "GMI_API_KEY",
        "GMI_BASE_URL",
        "GOOGLE_API_KEY",
        "HERMES_GEMINI_CLIENT_ID",
        "HERMES_GEMINI_CLIENT_SECRET",
        "HERMES_GEMINI_PROJECT_ID",
        "HERMES_QWEN_BASE_URL",
        "HF_BASE_URL",
        "HF_TOKEN",
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "KIMI_CN_API_KEY",
        "LM_API_KEY",
        "LM_BASE_URL",
        "MINIMAX_API_KEY",
        "MINIMAX_BASE_URL",
        "MINIMAX_CN_API_KEY",
        "MINIMAX_CN_BASE_URL",
        "MISTRAL_API_KEY",
        "NOUS_BASE_URL",
        "NVIDIA_API_KEY",
        "NVIDIA_BASE_URL",
        "OLLAMA_API_KEY",
        "OLLAMA_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENCODE_GO_API_KEY",
        "OPENCODE_GO_BASE_URL",
        "OPENCODE_ZEN_API_KEY",
        "OPENCODE_ZEN_BASE_URL",
        "OPENROUTER_API_KEY",
        "STEPFUN_API_KEY",
        "STEPFUN_BASE_URL",
        "XAI_API_KEY",
        "XAI_BASE_URL",
        "XIAOMI_API_KEY",
        "XIAOMI_BASE_URL",
        "ZAI_API_KEY",
        "Z_AI_API_KEY",
    }
)


@dataclass(frozen=True)
class RoleCredentialArtifact:
    """One file a CLI needs in a role's own home to start authenticated.

    An allowlist, deliberately narrow. Whole CLI homes are never copied: they
    carry conversations, histories, caches, hooks and general settings, and
    sharing those between roles would hand every role the others' work as well
    as their tokens (SYRD-39).
    """

    cli: str
    relative_path: str
    required: bool = True


# What each supported CLI needs, and nothing else. Anything not listed here is
# never copied into a role home.
ROLE_CREDENTIAL_ARTIFACTS: dict[str, tuple[RoleCredentialArtifact, ...]] = {
    "claude": (RoleCredentialArtifact("claude", ".claude/.credentials.json"),),
    "codex": (RoleCredentialArtifact("codex", ".codex/auth.json"),),
    "agy": (
        RoleCredentialArtifact(
            "agy", f"{AGY_CREDENTIAL_DIR_NAME}/{AGY_CREDENTIAL_TOKEN_NAME}"
        ),
    ),
    # Hermes resolves provider keys from the environment, not a token file, and
    # an ambient environment does not survive sudo -u into a separate account.
    # The owner therefore keeps its key in one private file, which is seeded
    # like any other credential and sourced by the role's own shell at start.
    # Targets are inside the role's own HERMES_HOME rather than its home root,
    # so they are resolved per role rather than listed here.
    # Only the filtered .env. auth.json is deliberately NOT seeded: its schema
    # is not something this code can verify is provider-only, and an artifact
    # whose contents cannot be constrained must not cross the boundary.
    "hermes": (RoleCredentialArtifact("hermes", f"{HERMES_OWNER_CREDENTIAL_DIR}/.env"),),
}


def hermes_credential_target(config: ProjectConfig, role: RoleConfig, artifact: RoleCredentialArtifact) -> Path:
    """Where a hermes role reads one credential from.

    Its own HERMES_HOME, which the launcher already points it at, so the file is
    read by hermes itself out of a directory the role owns. Nothing is injected
    into the environment and no shell file is sourced.
    """
    base, relative = _role_credential_target(config, role, artifact)
    return base / relative


def select_hermes_provider_env(text: str) -> tuple[str, list[str], str]:
    """Build a role .env holding only inference-provider authentication.

    Returns (content, omitted_keys, problem). The source is parsed, not copied:
    plain KEY=value grammar stops shell execution but says nothing about WHICH
    secrets cross, and hermes stores SUDO_PASSWORD, messaging tokens, GitHub and
    tool credentials in the same file. Everything outside the provider allowlist
    is dropped, and the caller reports what was left behind so the omission is
    visible rather than silent (SYRD-39).
    """
    kept: list[str] = []
    omitted: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator:
            return "", [], f"line {number} is not KEY=value"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return "", [], f"line {number} does not name a valid environment variable"
        if name in HERMES_PROVIDER_ENV_KEYS:
            kept.append(f"{name}={value.strip()}")
        else:
            omitted.append(name)
    if not kept:
        return "", omitted, "it holds no inference-provider credentials"
    header = (
        "# Generated by switchyard: inference-provider authentication only.\n"
        "# Other entries in the owner's .env are deliberately not shared with roles.\n"
    )
    return header + "\n".join(kept) + "\n", omitted, ""


def role_credential_artifacts(role: RoleConfig) -> tuple[RoleCredentialArtifact, ...]:
    cli_name = _command_name(role.cli[0]) if role.cli else ""
    return ROLE_CREDENTIAL_ARTIFACTS.get(cli_name, ())


def _walk_no_follow(base: Path, relative: Path) -> tuple[int, str]:
    """Open `base/relative`'s parent by walking components with O_NOFOLLOW.

    Returns (dir_fd, problem). lstat on the leaf and its immediate parent was
    not enough: an ancestor several levels up can be a symlink, and the whole
    subtree then belongs to wherever it points (SYRD-39).
    """
    try:
        fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return -1, "missing"
    except OSError as exc:
        return -1, f"cannot open {base} ({exc.strerror})"
    for component in relative.parent.parts:
        parent_fd_for_report = fd
        try:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                # O_DIRECTORY|O_NOFOLLOW reports ENOTDIR for a symlinked
                # directory on Linux and ELOOP elsewhere. Either way it is
                # refused; name it accurately so the manifest is actionable.
                # This is the ancestor case lstat on the leaf could not see.
                try:
                    kind = os.lstat(component, dir_fd=parent_fd_for_report)
                    if stat.S_ISLNK(kind.st_mode):
                        os.close(fd)
                        return -1, f"ancestor {component} is a symlink"
                except OSError:
                    pass
                os.close(fd)
                return -1, f"ancestor {component} is not a directory"
            os.close(fd)
            if exc.errno == errno.ENOENT:
                # Nothing there yet: absent, not unsafe.
                return -1, "missing"
            return -1, f"ancestor {component} is unusable ({exc.strerror})"
        os.close(fd)
        fd = child
    return fd, ""


def _credential_state(
    base: Path,
    relative: Path | str,
    expected_user: str,
    *,
    private: bool,
) -> str:
    """Empty when the credential is safe to rely on, otherwise why it is not.

    Every component from the home downwards is opened with O_NOFOLLOW, so a
    symlinked ancestor is refused rather than silently followed, and the leaf is
    inspected through that anchored descriptor rather than by path.
    """
    relative_path = Path(relative)
    expected_uid = uid_for_user(expected_user)
    if expected_uid is None:
        return f"cannot be checked: {expected_user} is not a local account"
    dir_fd, problem = _walk_no_follow(base, relative_path)
    if problem:
        return problem
    try:
        try:
            info = os.stat(relative_path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            return f"unreadable ({exc.strerror})"
        if stat.S_ISLNK(info.st_mode):
            return "is a symlink; credentials must be private regular files"
        if not stat.S_ISREG(info.st_mode):
            return "is not a regular file"
        if info.st_uid != expected_uid:
            return f"is owned by uid {info.st_uid}, not {expected_user}"
        if info.st_mode & 0o077:
            return f"is readable beyond its owner (mode {oct(stat.S_IMODE(info.st_mode))})"
        if private:
            parent = os.fstat(dir_fd)
            if parent.st_uid != expected_uid:
                return (
                    f"its directory is owned by uid {parent.st_uid}, not {expected_user}"
                )
            if parent.st_mode & 0o077:
                return (
                    "its directory is reachable beyond its owner "
                    f"(mode {oct(stat.S_IMODE(parent.st_mode))})"
                )
    finally:
        os.close(dir_fd)
    return ""


def _artifact_present(home: Path, artifact: RoleCredentialArtifact) -> bool:
    candidate = home / artifact.relative_path
    try:
        return candidate.is_file()
    except OSError:
        return False


def _role_credential_target(
    config: ProjectConfig,
    role: RoleConfig,
    artifact: RoleCredentialArtifact,
    *,
    home_base: Path | None = None,
) -> tuple[Path, Path]:
    """(base, relative) for where a role reads one credential.

    Most CLIs read from a fixed place under the role's home. Hermes reads from
    the per-role HERMES_HOME the launcher already points it at, so its target is
    resolved per role rather than assumed to mirror the owner's layout.
    """
    account = role_run_as_user(config, role)
    role_home = (
        home_base / account if home_base is not None else home_dir_for_user(account) or Path("/home") / account
    )
    if artifact.cli == "hermes":
        # Always expressed relative to the role's home so every component is
        # created and anchored under it, whatever base is in use.
        session_relative = Path(".local/state") / f"{config.project}-ticket-board"
        home_name = session_file_name(role.target).removesuffix(".json")
        return role_home, (
            session_relative
            / "hermes-homes"
            / home_name
            / Path(artifact.relative_path).name
        )
    return role_home, Path(artifact.relative_path)


def role_credential_manifest(config: ProjectConfig) -> list[str]:
    """What still has to be true before each role can start authenticated.

    Fails closed by being explicit: every line names the role, the CLI, and the
    exact artifact that is missing from the role's own home or absent from the
    owner's, rather than letting a role launch and fail at the provider.
    """
    owner_user = config.run_as_user or current_user_name()
    owner_home = home_dir_for_user(owner_user)
    lines: list[str] = []
    for role in config.roles:
        account = role_run_as_user(config, role)
        cli_name = _command_name(role.cli[0]) if role.cli else ""
        if not account or account == config.run_as_user:
            continue
        role_home = home_dir_for_user(account)
        artifacts = role_credential_artifacts(role)
        if not artifacts:
            lines.append(f"{role.role} ({cli_name or 'unknown cli'}): no known credential artifact")
            continue
        optional_group = all(not artifact.required for artifact in artifacts)
        satisfied = False
        pending: list[str] = []
        for artifact in artifacts:
            if role_home is not None:
                target_base, target_relative = _role_credential_target(config, role, artifact)
                target_problem = _credential_state(
                    target_base, target_relative, account, private=True
                )
                if not target_problem:
                    satisfied = True
                    continue
                if target_problem != "missing":
                    # Present but unsafe is worse than absent: say so instead of
                    # treating it as ready.
                    # Present but unsafe is worse than absent, for an
                    # alternative as much as a required artifact.
                    lines.append(
                        f"{role.role} ({cli_name}): {artifact.relative_path} in /home/{account} "
                        f"{target_problem}; reseed it with "
                        f"`switchyard seed-role-credentials {config.project} --role {role.role} --reseed`"
                    )
                    satisfied = True
                    continue
            owner_problem = (
                "missing"
                if owner_home is None
                else _credential_state(owner_home, artifact.relative_path, owner_user, private=False)
            )
            if owner_problem:
                if owner_problem == "missing" and optional_group:
                    # One of several alternatives; only report if none exist.
                    continue
                pending.append(
                    f"{role.role} ({cli_name}): the owner's {artifact.relative_path} "
                    f"{owner_problem}, so it cannot be seeded; authenticate {cli_name} as "
                    f"{owner_user} first"
                )
                continue
            pending.append(
                f"{role.role} ({cli_name}): {artifact.relative_path} not yet seeded into "
                f"/home/{account}"
            )
        if optional_group and satisfied:
            continue
        if optional_group and not pending:
            lines.append(
                f"{role.role} ({cli_name}): the owner has none of "
                + ", ".join(artifact.relative_path for artifact in artifacts)
                + f"; authenticate {cli_name} as {owner_user} first"
            )
            continue
        lines.extend(pending)
    return lines


def _open_role_credential_parent(
    account: str,
    base: Path,
    relative_path: Path | str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    """Open the role-owned directory that will hold one credential.

    Extends the SYRD-28 model: every component is created with mkdirat 0700 and
    opened with openat/O_NOFOLLOW relative to the descriptor above it, so no
    privileged step is ever handed a whole path that a symlink could redirect,
    and a component that already existed is proven to belong to the role rather
    than assumed to (SYRD-39).
    """
    relative = Path(relative_path)
    target_dir = base / relative.parent
    fd = os.open(base.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = _openat_no_follow(fd, base.name, target_dir)
        _require_owner_home_traversable(fd, account, base)
        for component in relative.parent.parts:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise SystemExit(
                    f"switchyard: failed to create {target_dir} for {account} ({exc.strerror})"
                ) from exc
            parent_fd = fd
            child_fd = _openat_no_follow_keep_parent(parent_fd, component, target_dir)
            try:
                if created:
                    os.fchmod(child_fd, 0o700)
                    own = runner(["chown", f"{account}:{account}", f"/proc/{os.getpid()}/fd/{child_fd}"])
                    if own.returncode != 0:
                        raise SystemExit(f"switchyard: failed to assign {target_dir} to {account}")
                else:
                    _require_owner_traversable(child_fd, account, component, target_dir)
            except BaseException:
                os.close(child_fd)
                if created:
                    try:
                        os.rmdir(component, dir_fd=parent_fd)
                    except OSError:
                        pass
                raise
            os.close(parent_fd)
            fd = child_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _try_open_credential_source(base: Path, relative: Path | str) -> tuple[int, str]:
    """Open a credential for reading with every component anchored.

    Returns (fd, problem); fd is -1 when problem is set. Unlike the raising
    variant this lets an optional artifact be absent without aborting.
    """
    relative_path = Path(relative)
    dir_fd, problem = _walk_no_follow(base, relative_path)
    if problem:
        return -1, problem
    try:
        try:
            return os.open(relative_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd), ""
        except FileNotFoundError:
            return -1, "missing"
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return -1, "is a symlink; credentials must be private regular files"
            return -1, f"cannot be opened ({exc.strerror})"
    finally:
        os.close(dir_fd)


def _fd_credential_state(fd: int, expected_user: str) -> str:
    """Validate the OPEN FILE, not a path that may have changed since.

    Validating by path and then reading by path leaves a window in which the
    name can be repointed at another file, and the privileged seeding command
    would read outside the approved artifact. Everything here is decided on the
    descriptor the bytes are actually read from (SYRD-39).
    """
    expected_uid = uid_for_user(expected_user)
    if expected_uid is None:
        return f"cannot be checked: {expected_user} is not a local account"
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if info.st_uid != expected_uid:
        return f"is owned by uid {info.st_uid}, not {expected_user}"
    if info.st_mode & 0o077:
        return f"is readable beyond its owner (mode {oct(stat.S_IMODE(info.st_mode))})"
    return ""


def _read_fd_bytes(fd: int) -> bytes:
    """Read a whole file from an already-open descriptor."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte; os.write may write fewer than requested."""
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])


def seed_role_credential(
    *,
    account: str,
    owner_user: str,
    artifact: RoleCredentialArtifact,
    target: tuple[Path, Path] | None = None,
    home_base: Path = Path("/home"),
    reseed: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Give one role its own private copy of one credential.

    A copy, never a symlink and never a shared directory: the role owns the
    file, refreshes rotate its own copy, and revoking one role's filesystem
    access does not touch another's. Returns False when the role already has it
    and reseed was not requested, which is what makes repair idempotent.
    """
    role_home = home_base / account
    owner_home = home_base / owner_user
    target_base, target_relative = target or (role_home, Path(artifact.relative_path))
    target_name = target_relative.name
    # Open the source ONCE, through anchored components, and decide everything
    # from that descriptor. Validating a path and then reading the same path
    # leaves a window in which the name can be repointed at another file
    # (SYRD-39).
    source_fd, source_problem = _try_open_credential_source(owner_home, artifact.relative_path)
    if source_problem == "missing" and not artifact.required:
        return False
    if source_problem == "missing":
        raise SystemExit(
            f"switchyard: {owner_user} has no {artifact.relative_path} to seed for {account}; "
            f"authenticate {artifact.cli} as {owner_user} first"
        )
    if source_problem:
        raise SystemExit(
            f"switchyard: {owner_user}'s {artifact.relative_path} {source_problem}; refusing to "
            "seed it"
        )
    constructed: bytes | None = None
    try:
        state_problem = _fd_credential_state(source_fd, owner_user)
        if state_problem:
            # Refuse to propagate a credential that is already unsafe, rather
            # than copying it into every role home.
            raise SystemExit(
                f"switchyard: {owner_user}'s {artifact.relative_path} {state_problem}; refusing "
                "to seed it"
            )
        if artifact.cli == "hermes" and target_relative.name == ".env":
            # Filtered from the bytes of the descriptor that was just validated,
            # never from a fresh read of the name.
            content, omitted, problem = select_hermes_provider_env(
                _read_fd_bytes(source_fd).decode("utf-8", errors="replace")
            )
            if problem:
                raise SystemExit(
                    f"switchyard: {owner_user}'s {artifact.relative_path} cannot be seeded "
                    f"({problem})"
                )
            constructed = content.encode("utf-8")
            if omitted:
                # Named, so the omission is visible rather than silent: these are
                # the owner's other secrets and settings, which roles must not
                # receive.
                print_func(
                    f"switchyard: not sharing {len(omitted)} non-provider entr"
                    + ("y" if len(omitted) == 1 else "ies")
                    + f" from {owner_user}'s {artifact.relative_path} with {account}: "
                    + ", ".join(sorted(omitted))
                )
    except BaseException:
        os.close(source_fd)
        raise
    dir_fd = _open_role_credential_parent(
        account, target_base, target_relative, runner=runner
    )
    created_target = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if reseed else os.O_EXCL)
        try:
            target_fd = os.open(target_name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            print_func(
                f"switchyard: {account} already has {artifact.relative_path}; leaving it alone "
                "(pass --reseed to replace it)"
            )
            return False
        except OSError as exc:
            raise SystemExit(
                f"switchyard: failed to seed {artifact.relative_path} for {account} "
                f"({exc.strerror})"
            ) from exc
        created_target = not reseed
        try:
            os.fchmod(target_fd, 0o600)
            if constructed is None:
                os.lseek(source_fd, 0, os.SEEK_SET)
                _copy_fd_contents(source_fd, target_fd)
            else:
                # A newly built file, never the source bytes, written in full.
                _write_all(target_fd, constructed)
            # Ownership is assigned to the open description rather than the
            # path, so nothing can be substituted between copy and chown.
            own = runner(["chown", f"{account}:{account}", f"/proc/{os.getpid()}/fd/{target_fd}"])
            if own.returncode != 0:
                raise SystemExit(
                    f"switchyard: failed to assign {artifact.relative_path} to {account}"
                )
        finally:
            os.close(target_fd)
    except BaseException:
        if created_target:
            try:
                os.unlink(target_name, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(source_fd)
        os.close(dir_fd)
    print_func(f"switchyard: seeded {artifact.relative_path} for {account} from {owner_user}")
    return True


def _open_owner_credential_dir(
    owner_user: str,
    home_base: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> int:
    """Create the owner's private credential directory and return a descriptor for it.

    Two things are being defended against. The owner controls this part of the tree, so
    no privileged command is handed the whole path: install(1) follows a symlink that
    names a directory and would apply mode and ownership to whatever it points at.
    Instead each component is created with mkdirat and opened with openat and O_NOFOLLOW
    relative to the descriptor above it, and everything later uses those descriptors.

    And a component that already exists is proven reachable by the owner rather than
    assumed to be, because a directory switchyard did not just create and chown may be
    left over from a failed attempt or belong to someone else entirely.
    """
    target_dir = home_base / owner_user / AGY_CREDENTIAL_DIR_NAME
    fd = os.open(home_base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # The home is usually created by useradd, but a Director-approved pre-existing
        # owner may bring one switchyard never made, so its reachability is proven here
        # rather than assumed. Everything below it is owner-controlled.
        fd = _openat_no_follow(fd, owner_user, target_dir)
        _require_owner_home_traversable(fd, owner_user, home_base / owner_user)
        for component in Path(AGY_CREDENTIAL_DIR_NAME).parts:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise SystemExit(
                    f"switchyard: failed to create {target_dir} for {owner_user} "
                    f"({exc.strerror})"
                ) from exc
            parent_fd = fd
            child_fd = _openat_no_follow_keep_parent(parent_fd, component, target_dir)
            try:
                if created:
                    os.fchmod(child_fd, 0o700)
                    own = runner(
                        ["chown", f"{owner_user}:{owner_user}", f"/proc/{os.getpid()}/fd/{child_fd}"]
                    )
                    if own.returncode != 0:
                        raise SystemExit(
                            f"switchyard: failed to assign {target_dir} to {owner_user}"
                        )
                else:
                    _require_owner_traversable(child_fd, owner_user, component, target_dir)
            except BaseException:
                os.close(child_fd)
                if created:
                    # Do not leave a directory whose ownership was never established: the
                    # retry would find it existing and, without this, trust it.
                    try:
                        os.rmdir(component, dir_fd=parent_fd)
                    except OSError:
                        pass
                raise
            os.close(parent_fd)
            fd = child_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def _openat_no_follow_keep_parent(parent_fd: int, component: str, target_dir: Path) -> int:
    """Like _openat_no_follow, but the caller keeps ownership of parent_fd."""
    try:
        return os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SystemExit(
                f"switchyard: path component {component!r} of {target_dir} is a symlink "
                "or not a directory; refusing to write a credential through a replaced "
                "path component"
            ) from exc
        raise SystemExit(
            f"switchyard: failed to open {component!r} of {target_dir} ({exc.strerror})"
        ) from exc


def _openat_no_follow(parent_fd: int, component: str, target_dir: Path) -> int:
    """Open one path component relative to parent_fd, refusing to traverse a symlink."""
    try:
        nested = os.open(
            component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SystemExit(
                f"switchyard: path component {component!r} of {target_dir} is a symlink "
                "or not a directory; refusing to write a credential through a replaced "
                "path component"
            ) from exc
        raise SystemExit(
            f"switchyard: failed to open {component!r} of {target_dir} ({exc.strerror})"
        ) from exc
    os.close(parent_fd)
    return nested


def _copy_fd_contents(source_fd: int, target_fd: int) -> None:
    """Copy fd to fd in the kernel, so the token's bytes never enter this process."""
    offset = 0
    while True:
        sent = os.sendfile(target_fd, source_fd, offset, 1 << 20)
        if not sent:
            return
        offset += sent


def _seed_agy_credential_for_owner(
    *,
    owner_user: str,
    source_user: str,
    home_base: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None] = print,
) -> None:
    """Copy one agy OAuth token into a freshly created owner home.

    Opt-in only, and only for a newly created owner: an existing owner user is never
    modified. The source is re-validated here on the fd actually copied, so this does not
    rely on the earlier precheck still being true.
    """
    _validate_agy_credential_source(source_user, owner_user, home_base)
    target_dir = home_base / owner_user / AGY_CREDENTIAL_DIR_NAME
    target_token = target_dir / AGY_CREDENTIAL_TOKEN_NAME
    dir_fd = _open_owner_credential_dir(owner_user, home_base, runner=runner)
    source_fd = _open_agy_source_token(source_user, home_base)
    created_target = False
    try:
        try:
            target_fd = os.open(
                AGY_CREDENTIAL_TOKEN_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError as exc:
            raise SystemExit(
                f"switchyard: {target_token} already exists; refusing to overwrite it"
            ) from exc
        except OSError as exc:
            raise SystemExit(
                f"switchyard: failed to seed agy credential into {target_token} ({exc.strerror})"
            ) from exc
        created_target = True
        try:
            os.fchmod(target_fd, 0o600)
            _copy_fd_contents(source_fd, target_fd)
            # Ownership is assigned to the open description, not to the path. Naming the
            # path again here would hand the owner a second chance to substitute a
            # different object between the copy and the chown.
            own_result = runner(
                [
                    "chown",
                    f"{owner_user}:{owner_user}",
                    f"/proc/{os.getpid()}/fd/{target_fd}",
                ]
            )
            if own_result.returncode != 0:
                raise SystemExit(f"switchyard: failed to assign {target_token} to {owner_user}")
        finally:
            os.close(target_fd)
    except BaseException:
        # Remove only the entry proven to have been created, resolved through the same
        # anchored directory rather than by walking the path again.
        if created_target:
            try:
                os.unlink(AGY_CREDENTIAL_TOKEN_NAME, dir_fd=dir_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(source_fd)
        os.close(dir_fd)
    print_func(f"switchyard: seeded agy credential for {owner_user} from {source_user}")
    print_func(
        f"switchyard: {owner_user} can now act as the Google account that {source_user} signed "
        "in to agy with; every role pane of this project shares that one account"
    )


def _project_entries(config_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in config_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return []
    for path in paths:
        try:
            raw = _load_json(path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        roles_raw = raw.get("roles")
        if not isinstance(roles_raw, list) or not roles_raw:
            continue
        try:
            slug = _validate_project_slug(str(raw.get("project") or path.stem))
        except SystemExit as exc:
            print(f"warning: switchyard: skipping {path}: {exc}", file=sys.stderr)
            continue
        name = str(raw.get("project_name") or raw.get("name") or slug).strip() or slug
        entries.append(SwitchyardProjectEntry(slug=slug, name=name, config_path=path))
    return entries


def _registry_project_entries(registry_dir: Path | None = None) -> list[SwitchyardProjectEntry]:
    registry_dir = registry_dir or DEFAULT_SWITCHYARD_REGISTRY_DIR
    entries: list[SwitchyardProjectEntry] = []
    try:
        paths = sorted(path for path in registry_dir.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return []
    for path in paths:
        try:
            raw = _load_json(path)
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
        if str(raw.get("schema") or "") != SWITCHYARD_REGISTRY_SCHEMA:
            continue
        try:
            slug = _validate_project_slug(str(raw.get("slug") or ""))
        except SystemExit as exc:
            print(f"warning: switchyard: skipping {path}: {exc}", file=sys.stderr)
            continue
        name = str(raw.get("name") or slug).strip() or slug
        config_path_raw = str(raw.get("config_path") or "").strip()
        if not slug or not config_path_raw:
            continue
        entries.append(SwitchyardProjectEntry(slug=slug, name=name, config_path=Path(config_path_raw).expanduser()))
    return entries


def _switchyard_entries(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> list[SwitchyardProjectEntry]:
    entries: dict[str, SwitchyardProjectEntry] = {}
    for entry in [*_project_entries(config_dir), *_registry_project_entries(registry_dir)]:
        key = entry.slug.casefold()
        entries.setdefault(key, entry)
    return sorted(entries.values(), key=lambda entry: (entry.name.casefold(), entry.slug.casefold()))


def _registered_project_collision(
    *,
    slug: str,
    name: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    skip_config_path: Path | None = None,
) -> str:
    skip_resolved = skip_config_path.expanduser().resolve(strict=False) if skip_config_path is not None else None
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        if skip_resolved is not None and entry.config_path.expanduser().resolve(strict=False) == skip_resolved:
            continue
        if entry.slug.casefold() == slug.casefold():
            return (
                f"switchyard: project slug {slug!r} is already registered to "
                f"{entry.name!r} at {entry.config_path}"
            )
        if entry.name.casefold() == name.casefold():
            return (
                f"switchyard: project name {name!r} is already registered as "
                f"{entry.slug!r} at {entry.config_path}"
            )
    return ""


def _check_switchyard_registration_available(
    *,
    slug: str,
    name: str,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    skip_config_path: Path | None = None,
) -> None:
    collision = _registered_project_collision(
        slug=slug,
        name=name,
        config_dir=config_dir,
        registry_dir=registry_dir,
        skip_config_path=skip_config_path,
    )
    if collision:
        raise SystemExit(collision)


def _register_switchyard_project(
    config_path: Path,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> Path:
    resolved_config_path = config_path.expanduser().resolve(strict=False)
    raw = _load_json(resolved_config_path)
    raw_slug = str(raw.get("project") or resolved_config_path.stem).strip()
    if not raw_slug:
        raise SystemExit(f"switchyard: cannot register {resolved_config_path}: project slug is empty")
    slug = _validate_project_slug(raw_slug)
    load_project_config(slug, resolved_config_path)
    name = str(raw.get("project_name") or raw.get("name") or slug).strip() or slug
    registry_dir = registry_dir or DEFAULT_SWITCHYARD_REGISTRY_DIR
    registry_path = registry_dir / f"{slug}.json"
    _check_switchyard_registration_available(
        slug=slug,
        name=name,
        config_dir=config_dir,
        registry_dir=registry_dir,
        skip_config_path=resolved_config_path,
    )
    if registry_path.exists():
        raise SystemExit(f"switchyard: registry entry {registry_path} already exists; refusing to overwrite")
    payload = {
        "schema": SWITCHYARD_REGISTRY_SCHEMA,
        "slug": slug,
        "name": name,
        "config_path": str(resolved_config_path),
    }
    try:
        registry_dir.mkdir(parents=True, exist_ok=True)
        registry_dir.parent.chmod(0o755)
        registry_dir.chmod(0o755)
        registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        registry_path.chmod(0o644)
    except OSError as exc:
        raise SystemExit(f"switchyard: failed to register project {slug!r} in {registry_dir}: {exc}") from exc
    return registry_path


def switchyard_register_command(
    config_path: Path,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    registry_path = _register_switchyard_project(config_path, config_dir=config_dir, registry_dir=registry_dir)
    print_func(f"switchyard: registered {config_path.expanduser().resolve(strict=False)} at {registry_path}")
    return 0


def switchyard_menu_command(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    print_func("new...")
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        suffix = f" ({entry.slug})" if entry.name.casefold() != entry.slug.casefold() else ""
        print_func(f"{entry.name}{suffix}")
    return 0


def _list_process_command_lines(
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[str]:
    proc = runner(
        ["ps", "-eo", "args=", "--no-headers"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        reason = _proc_failure_reason(proc, f"ps failed with exit {proc.returncode}")
        raise SystemExit(f"switchyard: failed to inspect processes: {reason}")
    return [line for line in str(proc.stdout or "").splitlines() if line.strip()]


def _role_has_pane_process(role: RoleConfig, process_commands: Sequence[str]) -> bool:
    target_marker = f"TICKET_BOARD_PANE_TARGET={role.target}"
    legacy_target_marker = f"PGU_PANE_TARGET={role.target}"
    for command in process_commands:
        try:
            first = shlex.split(command)[0] if command.strip() else ""
        except ValueError:
            first = command.strip().split(maxsplit=1)[0] if command.strip() else ""
        if Path(first).name == "tmux":
            continue
        if target_marker in command or legacy_target_marker in command:
            return True
    return False


def _format_checkout_probe_status(probe: LauncherCheckoutProbe) -> str:
    if probe.error:
        return f"unknown: {probe.error}"
    parts: list[str] = []
    if probe.behind:
        parts.append(f"behind {_format_behind_count(probe.behind, exact=probe.behind_exact)}")
    if probe.ahead:
        parts.append(f"ahead {probe.ahead} commit(s)")
    return ", ".join(parts) if parts else "current"


def _runtime_checkout_copy_status(
    config: ProjectConfig,
    *,
    project: str,
    copy: str,
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> SwitchyardRuntimeCopyStatus | None:
    if not path.exists():
        return None
    probe = probe_checkout_against_worktree_ref(config, path, runner=runner)
    return SwitchyardRuntimeCopyStatus(
        project=project,
        copy=copy,
        path=path,
        status=_format_checkout_probe_status(probe),
    )


def _runtime_release_copy_status(
    config: ProjectConfig,
    *,
    source_repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> SwitchyardRuntimeCopyStatus | None:
    status = tenant_release_status(config, source_repo=source_repo, deploy_ref=worktree_ref(config), runner=runner)
    if status is None:
        return None
    if status.resolve_error:
        summary = f"unknown: unresolved {status.deploy_ref}: {status.resolve_error}"
    elif not status.current_sha:
        summary = f"missing release; target {_format_release_sha(status.target_sha)} from {status.deploy_ref}"
    elif status.unchanged:
        summary = f"current at {_format_release_sha(status.current_sha)}"
    else:
        summary = f"stale: {_format_release_sha(status.current_sha)} -> {_format_release_sha(status.target_sha)}"
    return SwitchyardRuntimeCopyStatus(
        project=config.project,
        copy="tenant release",
        path=status.board_root,
        status=summary,
    )


def _runtime_shared_release_status(path: Path) -> SwitchyardRuntimeCopyStatus | None:
    release = shared_switchyard_release_for_path(path)
    if release is None:
        return None
    if release.marker_commit:
        summary = f"shared install at {release.marker_commit}"
    elif release.marker_error:
        summary = f"unknown shared install: {release.marker_error}"
    else:
        summary = "unknown shared install: missing release marker"
    return SwitchyardRuntimeCopyStatus(
        project="switchyard",
        copy="shared release",
        path=release.root,
        status=summary,
    )


def _parse_switchyard_wrapper_target(text: str) -> str:
    for prefix in ("readonly SWITCHYARD_DEFAULT_TARGET=", "readonly SWITCHYARD_TARGET="):
        target = _parse_switchyard_wrapper_target_line(text, prefix)
        if target:
            return target
    return ""


def _parse_switchyard_wrapper_target_line(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        raw_value = line.split("=", 1)[1].strip()
        try:
            parts = shlex.split(raw_value)
        except ValueError:
            return raw_value
        return parts[0] if parts else raw_value
    return ""


def switchyard_installed_wrapper_status(install_path: Path | None = None) -> SwitchyardRuntimeCopyStatus | None:
    path = (
        install_path
        or Path(_env_first("SWITCHYARD_INSTALL_PATH", "PGU_SWITCHYARD_INSTALL_PATH") or "/usr/local/bin/switchyard")
    ).expanduser()
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return SwitchyardRuntimeCopyStatus(
            project="switchyard",
            copy="installed wrapper",
            path=path,
            status=f"unknown: cannot read wrapper: {exc}",
        )
    target = _parse_switchyard_wrapper_target(text)
    if not target:
        return SwitchyardRuntimeCopyStatus(
            project="switchyard",
            copy="installed wrapper",
            path=path,
            status="unknown: not a switchyard trampoline",
        )
    target_path = Path(target).expanduser()
    target_status = "target reachable" if os.access(target_path, os.X_OK) else "target unreachable"
    if "--switchyard-wrapper-requires-root" not in text:
        summary = f"stale: embedded verb classification; {target_status}: {target_path}"
    else:
        summary = f"current: live verb classification; {target_status}: {target_path}"
    return SwitchyardRuntimeCopyStatus(
        project="switchyard",
        copy="installed wrapper",
        path=path,
        status=summary,
    )


def switchyard_runtime_copy_statuses(
    configs: Sequence[ProjectConfig],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    source_repo: Path | None = None,
    switchyard_install_path: Path | None = None,
) -> list[SwitchyardRuntimeCopyStatus]:
    source = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    statuses: list[SwitchyardRuntimeCopyStatus] = []
    seen: set[tuple[str, str, Path]] = set()

    def add(status: SwitchyardRuntimeCopyStatus | None) -> None:
        if status is None:
            return
        key = (status.project, status.copy, status.path.expanduser().resolve(strict=False))
        if key in seen:
            return
        seen.add(key)
        statuses.append(status)

    add(switchyard_installed_wrapper_status(switchyard_install_path))
    shared_status = _runtime_shared_release_status(source)
    if shared_status is not None:
        add(shared_status)
    for config in configs:
        if shared_status is None:
            add(_runtime_checkout_copy_status(config, project=config.project, copy="invoking checkout", path=source, runner=runner))
        if config.repository is not None:
            add(
                _runtime_checkout_copy_status(
                    config,
                    project=config.project,
                    copy="project checkout",
                    path=config.repository,
                    runner=runner,
                )
            )
        for role in config.roles:
            role_path = Path(role.workdir)
            if config.repository is not None and role_path.resolve(strict=False) == config.repository.resolve(strict=False):
                continue
            add(
                _runtime_checkout_copy_status(
                    config,
                    project=config.project,
                    copy=f"{role.role} worktree",
                    path=role_path,
                    runner=runner,
                )
            )
        if shared_status is None:
            add(_runtime_release_copy_status(config, source_repo=source, runner=runner))
    return statuses


def warn_if_artifact_source_checkout_is_stale(
    config: ProjectConfig,
    *,
    source_repo: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    print_func: Callable[[str], None],
) -> None:
    if not source_repo.exists():
        return
    shared_release = shared_switchyard_release_for_path(source_repo)
    if shared_release is not None:
        if shared_release.marker_commit:
            print_func(
                f"switchyard: artifact source is shared release {shared_release.root} "
                f"at {shared_release.marker_commit}"
            )
        elif shared_release.marker_error:
            print_func(
                f"warning: switchyard: cannot determine artifact source shared release "
                f"for {shared_release.root}: {shared_release.marker_error}"
            )
        else:
            print_func(
                f"warning: switchyard: cannot determine artifact source shared release "
                f"for {shared_release.root}: missing release marker"
            )
        return
    probe = probe_checkout_against_worktree_ref(config, source_repo, runner=runner)
    if probe.error:
        print_func(f"warning: switchyard: cannot determine artifact source checkout freshness for {source_repo}: {probe.error}")
        return
    if probe.behind:
        print_func(
            f"warning: switchyard: artifact source checkout {source_repo} is "
            f"{_format_behind_count(probe.behind, exact=probe.behind_exact)} behind {worktree_ref(config)}; "
            "generated files will reflect this checkout, not the remote tip"
        )
    if probe.ahead:
        print_func(
            f"warning: switchyard: artifact source checkout {source_repo} is "
            f"{probe.ahead} commit(s) ahead of {worktree_ref(config)}; generated files may include unmerged changes"
        )


def switchyard_project_statuses(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    process_commands: Sequence[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> list[SwitchyardProjectStatus]:
    commands = list(process_commands) if process_commands is not None else _list_process_command_lines(runner=runner)
    statuses: list[SwitchyardProjectStatus] = []
    for entry in _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir):
        try:
            config = load_project_config(entry.slug, entry.config_path)
        except (OSError, json.JSONDecodeError, SystemExit) as exc:
            statuses.append(
                SwitchyardProjectStatus(
                    name=entry.name,
                    slug=entry.slug,
                    state="unknown",
                    panes_up=None,
                    panes_total=None,
                    config_path=entry.config_path,
                    viewer_session=None,
                    error=str(exc),
                )
            )
            continue
        panes_up = sum(1 for role in config.roles if _role_has_pane_process(role, commands))
        statuses.append(
            SwitchyardProjectStatus(
                name=entry.name,
                slug=entry.slug,
                state="running" if panes_up else "stopped",
                panes_up=panes_up,
                panes_total=len(config.roles),
                config_path=entry.config_path,
                viewer_session=viewer_session_for_project(config.project),
            )
        )
    return statuses


def _switchyard_project_status_payload(status: SwitchyardProjectStatus) -> dict[str, Any]:
    return {
        "name": status.name,
        "slug": status.slug,
        "state": status.state,
        "panes_up": status.panes_up,
        "panes_total": status.panes_total,
        "panes": status.panes_display,
        "viewer_session": status.viewer_session,
        "config_path": str(status.config_path),
        "error": status.error,
    }


def _switchyard_runtime_copy_status_payload(status: SwitchyardRuntimeCopyStatus) -> dict[str, str]:
    return {
        "project": status.project,
        "copy": status.copy,
        "path": str(status.path),
        "status": status.status,
    }


def switchyard_status_command(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    json_output: bool = False,
    process_commands: Sequence[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    source_repo: Path | None = None,
    switchyard_install_path: Path | None = None,
    print_func: Callable[[str], None] = print,
) -> int:
    configs: list[ProjectConfig] = []
    statuses = switchyard_project_statuses(
        config_dir=config_dir,
        registry_dir=registry_dir,
        process_commands=process_commands,
        runner=runner,
    )
    for status in statuses:
        if status.state == "unknown":
            continue
        try:
            configs.append(load_project_config(status.slug, status.config_path))
        except (OSError, json.JSONDecodeError, SystemExit):
            continue
    runtime_statuses = switchyard_runtime_copy_statuses(
        configs,
        runner=runner,
        source_repo=source_repo,
        switchyard_install_path=switchyard_install_path,
    )
    if json_output:
        print_func(
            json.dumps(
                {
                    "projects": [_switchyard_project_status_payload(status) for status in statuses],
                    "runtime_copies": [
                        _switchyard_runtime_copy_status_payload(status) for status in runtime_statuses
                    ],
                },
                indent=2,
            )
        )
        return 0
    rows = [("NAME", "SLUG", "STATE", "PANES", "VIEWER")]
    rows.extend(
        (status.name, status.slug, status.state, status.panes_display, status.viewer_display)
        for status in statuses
    )
    widths = [max(len(str(row[index])) for row in rows) for index in range(5)]
    for row in rows:
        print_func(
            f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  "
            f"{row[3]:<{widths[3]}}  {row[4]}"
        )
    if runtime_statuses:
        print_func("")
        runtime_rows = [("PROJECT", "COPY", "PATH", "STATUS")]
        runtime_rows.extend(
            (status.project, status.copy, str(status.path), status.status) for status in runtime_statuses
        )
        runtime_widths = [max(len(str(row[index])) for row in runtime_rows) for index in range(3)]
        print_func("RUNTIME COPIES")
        for row in runtime_rows:
            print_func(
                f"{row[0]:<{runtime_widths[0]}}  "
                f"{row[1]:<{runtime_widths[1]}}  "
                f"{row[2]:<{runtime_widths[2]}}  {row[3]}"
            )
    return 0


def switchyard_validate_models_command(
    project: str,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    entry = _resolve_switchyard_project(project, config_dir=config_dir, registry_dir=registry_dir)
    config = load_project_config(entry.slug, entry.config_path)
    report = run_switchyard_launch_first_run_auth(
        config,
        validate_models=True,
        runner=runner,
        print_func=print_func,
    )
    report_first_run_auth_warnings(report, print_func=print_func)
    return 1 if report.model_validation_failures else 0


def _resolve_switchyard_project(
    selection: str,
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
) -> SwitchyardProjectEntry:
    wanted = selection.strip().casefold()
    if not wanted:
        raise SystemExit("switchyard: project name cannot be empty")
    entries = _switchyard_entries(config_dir=config_dir, registry_dir=registry_dir)
    exact = [
        entry
        for entry in entries
        if entry.name.casefold() == wanted or entry.slug.casefold() == wanted
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise SystemExit(f"switchyard: project selector {selection!r} is ambiguous")
    fallback = [entry for entry in entries if wanted in _project_name_selector_slugs(entry.name)]
    if len(fallback) == 1:
        return fallback[0]
    words = selection.split()
    if len(words) > 1:
        first = words[0].casefold()
        first_matches = [
            entry
            for entry in entries
            if entry.slug.casefold() == first or entry.name.casefold() == first
        ]
        if len(first_matches) == 1:
            raise SystemExit(
                f"switchyard: {words[0]!r} is a project; did you mean `switchyard {first_matches[0].slug}`? "
                "A bare project name starts or attaches it."
            )
    raise SystemExit(f"switchyard: unknown project {selection!r}")


def _confirm_switchyard_new(
    *,
    slug: str,
    owner_user: str,
    project_name: str,
    project_dir: Path,
    yes: bool,
    input_func: Callable[[str], str],
    print_func: Callable[[str], None],
) -> None:
    print_func(f"switchyard: project name: {project_name}")
    print_func(f"switchyard: slug: {slug}")
    print_func(f"switchyard: owner user: {owner_user}")
    print_func(f"switchyard: project path: {project_dir}")
    if yes:
        return
    answer = _read_prompt("Proceed? [y/N]: ", input_func=input_func).strip().lower()
    if answer not in {"y", "yes"}:
        raise SystemExit("switchyard: cancelled")


def switchyard_new_command(
    *,
    slug: str | None = None,
    agent_name: str | None = None,
    project_name: str | None = None,
    project_path: Path | None = None,
    from_artifact: Path | None = None,
    source_repo: Path | None = None,
    workflow_config: Path | None = None,
    commit_git_dir: str | None = None,
    output_dir: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    role_clis: Sequence[tuple[str, str]] | None = None,
    yes: bool = False,
    desktop_policy: Path | None = None,
    allow_existing_owner_user: bool = False,
    agy_credential_source: str | None = None,
    no_agy_credential: bool = False,
    agy_credential_settings_path: Path | None = None,
    home_base: Path = Path("/home"),
    euid_getter: Callable[[], int] = os.geteuid,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    session_dir_exists: Callable[[Path], bool] | None = None,
    pane_state_dir: Path | None = None,
    session_record_timeout: float = LAUNCH_SESSION_RECORD_TIMEOUT_SECONDS,
    session_record_poll: float = LAUNCH_SESSION_RECORD_POLL_SECONDS,
    layout_mode: str = LAYOUT_MODE_AUTO,
    layout_environ: dict[str, str] | None = None,
    konsole_process_launcher: Callable[..., Any] | None = None,
    git_init: bool = True,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    input_func: Callable[[str], str] = input,
    print_func: Callable[[str], None] = print,
) -> int:
    if from_artifact is not None:
        artifact = load_project_design_artifact(from_artifact)
        resolved_slug = artifact.project
        resolved_project_name = artifact.project_name
        owner_user = artifact.owner_user
        owner_shell = str(artifact.capability_grants.get("shell") or PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"])
        project_dir = _resolve_project_path(project_path or artifact.repository)
        selected_role_clis = artifact.role_clis
        selected_implementer_roles = artifact.implementer_roles
        include_designer = artifact.include_designer
        include_audit = artifact.include_audit
        selected_audit_roles = artifact.audit_roles
    else:
        resolved_project_name = (project_name or _prompt_text("Project name", input_func=input_func)).strip()
        if not resolved_project_name:
            raise SystemExit("switchyard: project name cannot be empty")
        default_slug = _slug_from_project_name(resolved_project_name)
        resolved_slug = _validate_project_slug(slug or _prompt_text("Slug", default=default_slug, input_func=input_func))
        raw_agent = agent_name if agent_name is not None else _prompt_text(
            "Agent user",
            default=_agent_owner_user(resolved_slug),
            input_func=input_func,
        )
        owner_user = _owner_user_verbatim(raw_agent)
        owner_shell = str(PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"])
        default_project_dir = _project_dir(home_base, owner_user, resolved_project_name)
        project_dir = _resolve_project_path(
            project_path
            or _prompt_text("Project path", default=str(default_project_dir), input_func=input_func)
        )
        selected_role_clis = ()
        selected_implementer_roles = ()
        include_designer = True
        include_audit = True
        selected_audit_roles = ("audit",)
    artifact_agy_source = ""
    if from_artifact is not None:
        artifact_agy_source = str(
            artifact.capability_grants.get("agy_credential_source") or ""
        ).strip()
    resolved_agy_credential_source, agy_source_origin = _resolve_agy_credential_source(
        override=agy_credential_source,
        opt_out=no_agy_credential,
        artifact_value=artifact_agy_source,
        home_base=home_base,
        settings_path=agy_credential_settings_path,
        yes=yes,
        input_func=input_func,
        print_func=print_func,
    )
    if desktop_policy is None:
        if yes:
            raise SystemExit("switchyard: --yes does not grant desktop access; provide --desktop-policy FILE (explicit headless or approved Wayland policy)")
        choice = input_func("Desktop policy file, or 'headless' for no clipboard: ").strip()
        if choice == "headless":
            selected_desktop_policy = {"mode": "headless"}
        elif choice:
            selected_desktop_policy = _load_json(Path(choice).expanduser())
        else:
            raise SystemExit("switchyard: choose a desktop policy before provisioning")
    else:
        selected_desktop_policy = {"mode": "headless"} if str(desktop_policy) == "headless" else _load_json(desktop_policy)
    from scripts.desktop_access import validate_policy
    selected_desktop_policy = validate_policy(selected_desktop_policy, project=resolved_slug, tenant=owner_user)
    _check_switchyard_registration_available(
        slug=resolved_slug,
        name=resolved_project_name,
        config_dir=config_dir,
        registry_dir=registry_dir,
    )
    _confirm_existing_owner_user(
        owner_user,
        allow_existing_owner_user=allow_existing_owner_user,
        input_func=input_func,
        print_func=print_func,
        agy_credential_source=resolved_agy_credential_source,
    )
    _confirm_switchyard_new(
        slug=resolved_slug,
        owner_user=owner_user,
        project_name=resolved_project_name,
        project_dir=project_dir,
        yes=yes,
        input_func=input_func,
        print_func=print_func,
    )
    if euid_getter() != 0:
        raise SystemExit("switchyard: new requires sudo; re-run as `sudo ./switchyard new`")
    if from_artifact is None:
        selected_role_clis = _dedupe_role_cli_pairs(
            role_clis if role_clis is not None else _prompt_switchyard_role_choices(input_func=input_func, print_func=print_func)
        )
        _require_new_project_roles(selected_role_clis)
        selected_implementer_roles = tuple(
            role for role, _cli in selected_role_clis if role not in NEW_PROJECT_RESERVED_ROLE_NAMES
        )
        if not selected_implementer_roles:
            raise SystemExit("switchyard: at least one implementer role is required")
        include_designer = any(role == "designer" for role, _cli in selected_role_clis)
        include_audit = any(role == "audit" for role, _cli in selected_role_clis)
        selected_audit_roles = ("audit",) if include_audit else ()

    artifact_path = (from_artifact or (_switchyard_dir(project_dir) / f"{resolved_slug}.project.json")).expanduser().resolve(strict=False)
    design_document = project_dir / SWITCHYARD_DESIGN_FILE_NAME
    director_onboarding = _switchyard_dir(project_dir) / SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME
    _precheck_project_path_before_mutating(owner_user, project_dir)
    effective_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    precheck_artifact = load_project_design_artifact(from_artifact, expected_project=resolved_slug) if from_artifact else None
    worktree_branch = precheck_artifact.default_branch if precheck_artifact else "main"
    precheck_plan = build_plan(
        project=resolved_slug,
        project_name=precheck_artifact.project_name if precheck_artifact else resolved_project_name,
        owner_user=owner_user,
        owner_home=home_base / owner_user,
        port=port,
        database=database,
        source_repo=effective_source_repo,
        commit_git_dir=commit_git_dir,
        ticket_prefix=precheck_artifact.ticket_prefix if precheck_artifact else None,
        implementer_roles=precheck_artifact.implementer_roles if precheck_artifact else selected_implementer_roles,
        include_designer=precheck_artifact.include_designer if precheck_artifact else include_designer,
        include_audit=precheck_artifact.include_audit if precheck_artifact else include_audit,
        audit_roles=precheck_artifact.audit_roles if precheck_artifact else selected_audit_roles,
        board_service_traversal=(
            bool(precheck_artifact.capability_grants.get("board_service_traversal", True))
            if precheck_artifact
            else True
        ),
    )
    precheck_new_project(
        precheck_plan,
        source_repo=effective_source_repo,
        repository=project_dir,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        config_dir=config_dir,
        registry_dir=registry_dir,
        require_owner_user=False,
        require_repository=False,
    )
    if resolved_agy_credential_source:
        _validate_agy_credential_source(resolved_agy_credential_source, owner_user, home_base)
    _ensure_board_service_user(precheck_plan.service_user, runner=runner)
    _ensure_board_service_peer_auth(precheck_plan, source_repo=effective_source_repo, runner=runner)
    owner_result = _ensure_owner_user_and_project_dir(
        owner_user,
        project_dir,
        runner=runner,
        shell=owner_shell,
    )
    if owner_result.created:
        created_shell = owner_result.shell_path or owner_shell
        if owner_shell == PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS["shell"] and Path(created_shell).name != owner_shell:
            print_func(
                f"switchyard: created user {owner_user} with shell {created_shell} "
                f"({owner_shell} unavailable); linger enabled"
            )
        else:
            display_shell = created_shell if "/" in owner_shell else owner_shell
            print_func(f"switchyard: created user {owner_user} with shell {display_shell}; linger enabled")
    else:
        print_func(f"switchyard: using existing user {owner_user} (not modifying)")
    if resolved_agy_credential_source:
        credential_state = _agy_credential_state(owner_user, home_base)
        if credential_state == AGY_CREDENTIAL_INSTALLED:
            print_func(
                f"switchyard: agy credential already present for {owner_user}; leaving it in place"
            )
        elif credential_state == AGY_CREDENTIAL_UNUSABLE:
            raise SystemExit(
                f"switchyard: {home_base / owner_user / AGY_CREDENTIAL_DIR_NAME / AGY_CREDENTIAL_TOKEN_NAME} "
                f"exists but is not a 0600 regular file owned by {owner_user}, so agy could not "
                "read it; remove it and rerun, or clear capability_grants.agy_credential_source"
            )
        else:
            # Not gated on owner_result.created. Reaching here with a pre-existing owner
            # already required consent to reuse that account, and gating on creation is
            # what made a failed seed unrecoverable: the retry would skip and then let
            # provisioning record a credential source that was never installed.
            _seed_agy_credential_for_owner(
                owner_user=owner_user,
                source_user=resolved_agy_credential_source,
                home_base=home_base,
                runner=runner,
                print_func=print_func,
            )
    if from_artifact is None:
        _write_switchyard_onboarding_files(
            project_name=resolved_project_name,
            slug=resolved_slug,
            owner_user=owner_user,
            project_dir=project_dir,
            artifact_path=artifact_path,
            design_document=design_document,
            director_onboarding=director_onboarding,
            include_designer=include_designer,
        )
        _write_initial_switchyard_project_artifact(
            project_name=resolved_project_name,
            slug=resolved_slug,
            owner_user=owner_user,
            project_dir=project_dir,
            artifact_path=artifact_path,
            design_document=design_document,
            owner_shell=owner_shell,
            implementer_roles=selected_implementer_roles,
            role_clis=selected_role_clis,
            include_designer=include_designer,
            include_audit=include_audit,
            audit_roles=selected_audit_roles,
            agy_credential_source=resolved_agy_credential_source,
            agy_credential_source_origin=agy_source_origin,
        )
        _chown_switchyard_project_files(owner_user=owner_user, project_dir=project_dir, runner=runner)
        if include_designer and design_document.exists():
            _chown_project_file(owner_user=owner_user, path=design_document, runner=runner)
        _install_switchyard_onboarding_docs(
            source_repo=effective_source_repo,
            project_dir=project_dir,
            owner_user=owner_user,
            runner=runner,
            print_func=print_func,
        )
        if git_init:
            created_git_repository = _ensure_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                branch=worktree_branch,
                runner=runner,
            )
            if created_git_repository:
                print_func(
                    f"switchyard: initialized git repository in {project_dir} "
                    f"on branch {worktree_branch} with an initial commit"
                )
            else:
                print_func(f"switchyard: using existing git repository in {project_dir} without modifying it")
        else:
            print_func(f"switchyard: skipped project git initialization for {project_dir} (--no-git-init)")
            _require_existing_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                runner=runner,
            )
    else:
        artifact = load_project_design_artifact(artifact_path)
        _write_switchyard_onboarding_files(
            project_name=resolved_project_name,
            slug=resolved_slug,
            owner_user=owner_user,
            project_dir=project_dir,
            artifact_path=artifact_path,
            design_document=artifact.design_document,
            director_onboarding=director_onboarding,
            include_designer=artifact.include_designer,
        )
        _chown_switchyard_project_files(owner_user=owner_user, project_dir=project_dir, runner=runner)
        _install_switchyard_onboarding_docs(
            source_repo=effective_source_repo,
            project_dir=project_dir,
            owner_user=owner_user,
            runner=runner,
            print_func=print_func,
        )
        if git_init:
            created_git_repository = _ensure_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                branch=worktree_branch,
                runner=runner,
            )
            if created_git_repository:
                print_func(
                    f"switchyard: initialized git repository in {project_dir} "
                    f"on branch {worktree_branch} with an initial commit"
                )
            else:
                print_func(f"switchyard: using existing git repository in {project_dir} without modifying it")
        else:
            print_func(f"switchyard: skipped project git initialization for {project_dir} (--no-git-init)")
            _require_existing_project_git_repository(
                owner_user=owner_user,
                project_dir=project_dir,
                runner=runner,
            )

    provision_dir = (output_dir or (_switchyard_dir(project_dir) / "provision")).expanduser().resolve(strict=False)
    provision_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(provision_dir / "desktop-policy.json", selected_desktop_policy)
    result = new_project_command(
        resolved_slug,
        from_artifact=artifact_path,
        owner_home=home_base / owner_user,
        source_repo=source_repo,
        workflow_config=workflow_config,
        commit_git_dir=commit_git_dir,
        output_dir=provision_dir,
        director_onboarding=director_onboarding,
        port=port,
        database=database,
        execute=True,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        require_owner_user=False,
        enable_owner_linger=False,
        print_func=print_func,
    )
    if result != 0:
        return result
    _commit_project_git_changes(
        owner_user=owner_user,
        project_dir=project_dir,
        message="Record Switchyard provisioning artifacts",
        runner=runner,
    )
    config_path = provision_dir / f"{resolved_slug}.json"
    config = load_project_config(resolved_slug, config_path)
    config = prepare_project_desktop(config, runner=runner)
    _register_switchyard_project(config_path, registry_dir=registry_dir)
    _prepare_first_run_auth_worktrees(config, runner=runner)
    first_run_auth_report = run_first_run_auth_phase(
        config,
        owner_user=owner_user,
        owner_home=_owner_home_for_auth(owner_user, fallback=home_base / owner_user),
        validate_models=True,
        runner=runner,
        print_func=print_func,
    )
    if stop_before_launch_for_missing_owner_clis(first_run_auth_report, print_func=print_func):
        return 1
    if first_run_auth_report.model_validation_failures:
        report_first_run_auth_warnings(first_run_auth_report, print_func=print_func)
        return 1
    launch_runner = _owner_project_git_runner(
        owner_user=owner_user,
        project_dir=project_dir,
        owned_roots=_control_repository_owned_roots(config),
        runner=runner,
    )
    # A newly provisioned project declares per-role accounts that the operator
    # has not created yet, so its roles cannot start with their own identities.
    # Provisioning itself succeeded; the launch is deferred rather than failed,
    # and the artifacts say what to run next (SYRD-39).
    pending_isolation = role_isolation_gaps(config)
    launch_deferred = bool(pending_isolation)
    if launch_deferred:
        # The complete handoff -- accounts, ownership, runtime, tooling AND
        # credential seeding -- is written here, not left to a later failed
        # start, so following the printed instruction once is enough to make the
        # next start operable (SYRD-39).
        handoff_path = config_path.with_name(f"{resolved_slug}-role-accounts.sh")
        try:
            handoff_path.write_text(render_role_account_migration(config), encoding="utf-8")
            handoff_path.chmod(0o755)
        except OSError as exc:
            print_func(f"switchyard: could not write {handoff_path}: {exc}")
        print_func(
            f"switchyard: provisioned {resolved_slug}. Its roles are not isolated yet, so they "
            "were not started:\n  " + "\n  ".join(pending_isolation)
            + f"\nRun {handoff_path} as an operator (safe to re-run), then start it with "
            f"`switchyard {resolved_slug}`."
        )
    launch_started_at = time.time()
    launch_started_ns = time.time_ns()
    launch_result = 0 if launch_deferred else launch_project(
        config,
        config_path=config_path,
        mode="start",
        script_path=Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME),
        layout_output=_owner_state_layout_output_path(resolved_slug, owner_home=home_base / owner_user),
        assign_layout_owner=True,
        pane_state_dir=pane_state_dir,
        runner=launch_runner,
        layout_mode=layout_mode,
        layout_environ=layout_environ,
        konsole_process_launcher=konsole_process_launcher,
    )
    if launch_result != 0:
        return launch_result
    print_func(f"switchyard: full pane window started for {resolved_slug}")
    report_first_run_auth_warnings(first_run_auth_report, print_func=print_func)
    report_launch_session_records(
        config,
        timeout_seconds=session_record_timeout,
        poll_seconds=session_record_poll,
        fallback_changed_since_ns=launch_started_ns,
        pane_state_dir=pane_state_dir or default_pane_state_dir_for_user(config.run_as_user, project=config.project),
        pane_state_updated_since=launch_started_at,
        print_func=print_func,
    )
    resolved_layout_mode = resolve_layout_mode(layout_mode, environ=layout_environ, runner=runner)
    if resolved_layout_mode == LAYOUT_MODE_VIEWER:
        if include_designer:
            print_func("switchyard: maximize the designer pane during design with Ctrl+a z; press it again to restore")
        else:
            print_func("switchyard: design phase skipped; no designer pane configured")
    else:
        if include_designer:
            print_func("switchyard: maximize the designer pane during design with Konsole Ctrl+Shift+E; restore it when done")
        else:
            print_func("switchyard: design phase skipped; no designer pane configured")
    return 0


def provision_runtime_command(user_name: str | None, config: ProjectConfig | None = None) -> int:
    user = (user_name or "").strip()
    if not user and config is not None:
        user = config.run_as_user or current_user_name()
    if not user:
        user = current_user_name()
    runtime_dir = ensure_user_linger_runtime(user)
    print(f"runtime ready for {user}: {runtime_dir}")
    return 0


def role_isolation_gaps(config: ProjectConfig) -> list[str]:
    """What still stops each role running under its own Unix identity.

    Checks the things that actually have to be true, not just whether the
    accounts exist: a tenant that applied an account-only rollout still has
    owner-owned worktrees, so it must keep being repaired until every gap is
    closed. Fresh projects hit this too, because their worktrees are created
    after the operator artifact has already run (SYRD-39).
    """
    # Isolation is opt-in per project: a config where no role declares its own
    # account is an unmigrated tenant, which keeps working as before. Once ANY
    # role opts in, the whole project must be complete, because a partial
    # migration is the dangerous state -- some roles isolated, some sharing.
    if not any(
        role_run_as_user(config, role) not in ("", config.run_as_user) for role in config.roles
    ):
        return []
    gaps: list[str] = []
    for role in config.roles:
        account = role_run_as_user(config, role)
        if not account or account == config.run_as_user:
            gaps.append(f"{role.role}: no Unix account of its own")
            continue
        if not local_account_exists(account):
            gaps.append(f"{role.role}: account {account} does not exist")
            continue
        workdir = Path(role.workdir)
        if not workdir.exists():
            continue
        try:
            owner_uid = workdir.stat().st_uid
            account_uid = pwd.getpwnam(account).pw_uid
        except (OSError, KeyError):
            continue
        if owner_uid != account_uid:
            gaps.append(f"{role.role}: worktree {workdir} is not owned by {account}")
    # A role that starts without its CLI credentials fails at the provider
    # instead of at launch, so the manifest is part of the gate rather than a
    # separate check nobody runs (SYRD-39).
    gaps.extend(role_credential_manifest(config))
    return gaps


def upgrade_role_accounts_in_config(config_path: Path, *, dry_run: bool = False) -> tuple[bool, str]:
    """Give every role in an existing config its own Unix account.

    A tenant provisioned before per-role identities has no run_as_user on its
    roles, so every role would keep launching as the shared project owner and
    the board could not tell them apart. Upgrade fills it in from the same
    naming provisioning uses (SYRD-39).
    """
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"team-launcher: cannot read {config_path} to add role accounts: {exc}"
    project = str(payload.get("project") or "").strip()
    roles = payload.get("roles")
    if not project or not isinstance(roles, list):
        return False, "team-launcher: config has no project roles to give Unix accounts"
    added: list[str] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        name = str(role.get("role") or "").strip()
        if not name or str(role.get("run_as_user") or "").strip():
            continue
        role["run_as_user"] = role_account_name(project, name)
        added.append(name)
    if not added:
        return False, "team-launcher: every role already has its own Unix account"
    if dry_run:
        return True, "team-launcher: would give " + ", ".join(sorted(added)) + " their own Unix accounts"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True, "team-launcher: gave " + ", ".join(sorted(added)) + " their own Unix accounts"


def render_role_account_migration(config: ProjectConfig) -> str:
    """Operator commands that move an existing tenant onto per-role accounts.

    Creating the accounts is not enough on its own: a role also has to own the
    working tree and runtime state it uses, or it cannot write them once it
    stops running as the project owner. Every step is guarded or idempotent so
    the artifact is safe to re-run.
    """
    from scripts.ticket_board.project_provision import (
        DEFAULT_SERVICE_USER,
        role_runtime_commands,
        role_tooling_staging_commands,
        roles_group_name,
        shell_quote,
    )

    owner = config.run_as_user or current_user_name()
    group = roles_group_name(config.project)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"# Migrate {config.project} onto one Unix account per role (SYRD-39).",
        "# Safe to re-run: every creating step is guarded.",
        f"if ! getent group {shell_quote(group)} >/dev/null 2>&1; then",
        f"    sudo groupadd -r {shell_quote(group)}",
        "fi",
        f"sudo gpasswd -a {shell_quote(DEFAULT_SERVICE_USER)} {shell_quote(group)} >/dev/null",
        f"sudo gpasswd -a {shell_quote(owner)} {shell_quote(group)} >/dev/null",
    ]
    lines.extend(
        role_tooling_staging_commands(
            config.project, f"/home/{owner}/{config.project}-ticketboard-live/current"
        )
    )
    for role in config.roles:
        account = role_run_as_user(config, role)
        if not account or account == owner:
            continue
        lines.extend(
            role_runtime_commands(
                project=config.project,
                runtime_directory=f"{config.project}-ticket-board",
                board_current=f"/home/{owner}/{config.project}-ticketboard-live/current",
                roles_group=group,
                account=account,
                home=f"/home/{account}",
                worktree=role.workdir,
            )
        )
    lines.append(
        "# Refresh the director control interface for the current role set:"
    )
    lines.append(
        f"switchyard provision {config.project} --render role-control-sudoers "
        f"| sudo install -m 0440 -o root -g root /dev/stdin "
        f"/etc/sudoers.d/49-{config.project}-role-control.staged"
    )
    lines.append(f"sudo visudo -c -f /etc/sudoers.d/49-{config.project}-role-control.staged")
    lines.append(
        f"sudo mv /etc/sudoers.d/49-{config.project}-role-control.staged "
        f"/etc/sudoers.d/49-{config.project}-role-control"
    )
    lines.append(
        "# Give each role its own private copy of the credentials its CLI needs."
    )
    lines.append(
        "# Idempotent: a role that already has one is left alone; --reseed replaces it."
    )
    lines.append(f"sudo switchyard seed-role-credentials {config.project}")
    lines.append(f"sudo systemctl restart {config.project}-ticket-board.service")
    return "\n".join(lines) + "\n"
def migrate_declarative_director_onboarding(
    config: "ProjectConfig",
    *,
    config_path: Path,
    dry_run: bool = False,
    print_func: Callable[[str], None] = print,
) -> bool:
    """Run the shared director-onboarding migration for a declarative tenant.

    Upgrade and recovery call this so an existing project is migrated by ordinary
    maintenance rather than by a manual per-tenant step. It is a no-op for a project
    with no workflow document, and idempotent for one already migrated.
    """
    if not _load_json(config_path).get("workflow"):
        return False
    if dry_run:
        print_func("switchyard: would migrate the director's onboarding prompt if unset")
        return False
    import contextlib
    import io as _io

    from scripts import workflow_manage

    captured = _io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            workflow_manage.main(
                [
                    "migrate-director-onboarding",
                    "--board-url",
                    config.board_url,
                    "--config",
                    str(config_path),
                ]
            )
    except Exception as exc:
        # Deliberately fatal. The hook no longer carries a legacy director source, so
        # deploying it over an unmigrated document would leave that director with no
        # onboarding at all. Failing here stops before the release is deployed, which is
        # recoverable; succeeding and deploying anyway is not.
        raise SystemExit(
            f"switchyard: director onboarding migration failed for {config_path}: {exc}. "
            "Resolve it before deploying, or clear the director's onboarding deliberately "
            "with `switchyard role-prompt clear director`."
        ) from exc
    # Report whether the document actually changed, so callers know when their loaded
    # configuration has gone stale. An already-migrated tenant changes nothing.
    try:
        report = json.loads(captured.getvalue() or "{}")
    except json.JSONDecodeError:
        return True
    if report.get("reason") == "board predates the phase-one schema":
        # Expected during rollout, and not a failure: the upgrade must still report the
        # deployment commands, because deploying phase one is what unblocks the migration.
        print_func(
            "switchyard: the running board predates this release's workflow schema, so "
            "the director onboarding migration was not attempted. Deploy the release "
            "below, then rerun switchyard upgrade to migrate this tenant."
        )
        return False
    if report.get("migrated") is False:
        return False
    print_func(
        f"switchyard: migrated the director's onboarding for {config_path}"
    )
    return True


def upgrade_project_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    desktop_policy: Path | None = None,
    source_repo: Path | None = None,
    commit_git_dir: str | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    effective_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    if desktop_policy is not None or config.desktop_access is not None:
        config = configure_project_desktop(config, config_path=config_path, policy_path=desktop_policy,
            dry_run=dry_run, helper=effective_source_repo / "scripts/desktop_access.py", runner=runner)
    release_report_config = config
    warn_if_artifact_source_checkout_is_stale(
        config,
        source_repo=effective_source_repo,
        runner=runner,
        print_func=print_func,
    )
    result = upgrade_generated_project_layout(config, config_path=config_path, dry_run=dry_run, runner=runner)
    print_func(result.message)
    if result.changed:
        config = load_project_config(config.project, config_path)
    accounts_changed, accounts_message = upgrade_role_accounts_in_config(config_path, dry_run=dry_run)
    print_func(accounts_message)
    if accounts_changed and not dry_run:
        config = load_project_config(config.project, config_path)
    gaps = role_isolation_gaps(config)
    if gaps:
        # Re-emitted while ANY gap remains, not only while an account is
        # missing, so a tenant that applied an account-only rollout is repaired
        # rather than silently left half-migrated.
        migration_path = config_path.with_name(f"{config.project}-role-accounts.sh")
        if not dry_run:
            migration_path.write_text(render_role_account_migration(config), encoding="utf-8")
            migration_path.chmod(0o755)
        print_func(
            "team-launcher: roles are not yet isolated, so the board cannot tell them apart:\n  "
            + "\n  ".join(gaps)
            + f"\nAn operator must run {migration_path} (safe to re-run) before those roles restart."
        )
    runtime_artifacts = refresh_generated_project_runtime_artifacts(
        config,
        config_path=config_path,
        dry_run=dry_run,
        source_repo=effective_source_repo if source_repo is not None else None,
        commit_git_dir=commit_git_dir,
        runner=runner,
    )
    print_func(runtime_artifacts.message)
    project_dir = _project_dir_from_generated_config_path(config_path)
    if project_dir is not None:
        onboarding_commit_git_dir = commit_git_dir
        if onboarding_commit_git_dir is None:
            plan_commit_git_dir = _plan_data_from_config(config, config_path).get("commit_git_dir")
            if isinstance(plan_commit_git_dir, str) and plan_commit_git_dir.strip():
                onboarding_commit_git_dir = plan_commit_git_dir.strip()
        upgrade_switchyard_onboarding_docs(
            source_repo=effective_source_repo,
            project_dir=project_dir,
            owner_user=config.run_as_user or current_user_name(),
            commit_git_dir=onboarding_commit_git_dir,
            dry_run=dry_run,
            runner=runner,
            print_func=print_func,
        )
    ensure_generated_project_board_skill(
        config,
        config_path=config_path,
        script_path=effective_source_repo / "scripts" / "team-launcher",
        source_repo=effective_source_repo,
        dry_run=dry_run,
        runner=runner,
        print_func=print_func,
    )
    # Before the release is reported/deployed, not after: the deployed hook has no
    # legacy director source, so the document must already carry the director's
    # onboarding by the time anyone acts on that report.
    migrate_declarative_director_onboarding(
        config, config_path=config_path, dry_run=dry_run, print_func=print_func
    )
    report_tenant_release_upgrade(
        release_report_config,
        config_path=config_path,
        source_repo=effective_source_repo,
        commit_git_dir=commit_git_dir,
        deploy_ref=deploy_ref,
        runner=runner,
        print_func=print_func,
    )
    return 0


def _configured_implementer_roles(
    config: ProjectConfig,
    *,
    plan_data: dict[str, Any] | None = None,
    extra_role: str | None = None,
) -> tuple[str, ...]:
    roles: list[str] = []
    raw_plan_roles = (plan_data or {}).get("implementer_roles")
    if isinstance(raw_plan_roles, list):
        source_roles = [str(role).strip().lower() for role in raw_plan_roles if str(role).strip()]
    else:
        source_roles = [
            role.role
            for role in config.roles
            if role.role not in NEW_PROJECT_RESERVED_ROLE_NAMES
        ]
    for role in source_roles:
        if role not in roles:
            roles.append(role)
    if extra_role and extra_role not in roles:
        roles.append(extra_role)
    return tuple(roles)


def _configured_audit_roles(
    config: ProjectConfig,
    *,
    plan_data: dict[str, Any] | None = None,
    extra_role: str | None = None,
) -> tuple[str, ...]:
    raw_plan_roles = (plan_data or {}).get("audit_roles")
    if isinstance(raw_plan_roles, list):
        roles = list(_dedupe_role_names(tuple(str(role).strip().lower() for role in raw_plan_roles if str(role).strip())))
    else:
        roles = ["audit"] if any(role.role == "audit" for role in config.roles) else []
    if extra_role and extra_role not in roles:
        roles.append(extra_role)
    return tuple(roles)


def _loaded_plan_field(plan_data: dict[str, Any], key: str, default: Any) -> Any:
    return plan_data[key] if key in plan_data and plan_data[key] not in (None, "") else default


def _plan_data_from_config(config: ProjectConfig, config_path: Path) -> dict[str, Any]:
    plan_path = config_path.parent / "plan.json"
    try:
        raw = _load_json(plan_path)
    except OSError:
        raw = {}
    except SystemExit:
        raw = {}
    if raw:
        return raw
    port = None
    match = re.search(r":([0-9]{1,5})(?:/|$)", config.board_url)
    if match:
        port = int(match.group(1))
    board_root = _tenant_board_root_from_config(config)
    return {
        "project": config.project,
        "owner_user": config.run_as_user or current_user_name(),
        "port": port,
        "database": "pgu" if config.project == "pgu" else f"{config.project}_ticket_board",
        "ticket_prefix": config.ticket_prefix,
        "source_repo": str(_repo_root()),
        "board_root": str(board_root) if board_root else None,
        "asset_dir": None,
        "frame_dir": None,
        "audit_roles": ["audit"] if any(role.role == "audit" for role in config.roles) else [],
        "board_service_traversal": True,
        "operation_allowed_roles": [],
    }


def _owner_home_from_plan_data(config: ProjectConfig, plan_data: dict[str, Any]) -> Path:
    return Path(str(_loaded_plan_field(
        plan_data,
        "owner_home",
        _owner_home_for_auth(config.run_as_user or current_user_name()),
    )))


def _commit_git_dir_from_plan_data(config: ProjectConfig, plan_data: dict[str, Any]) -> str:
    return str(_loaded_plan_field(
        plan_data,
        "commit_git_dir",
        commit_git_dir_env_for_project(
            project=config.project,
            owner_home=_owner_home_from_plan_data(config, plan_data),
        ),
    ))


def _vcs_close_role_from_plan_data(plan_data: dict[str, Any]) -> str | None:
    for item in plan_data.get("operation_allowed_roles") or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        operation, roles = item
        if operation != "mark_done" or not isinstance(roles, (list, tuple)) or not roles:
            continue
        return str(roles[0])
    return None


def _project_plan_for_added_role(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    audit_role: bool = False,
) -> ProjectBoardProvision:
    plan_data = _plan_data_from_config(config, config_path)
    if plan_data.get("workflow") or _load_json(config_path).get("workflow"):
        raise SystemExit("use ticket-board-workflow apply to update configured roles and stages")
    implementer_roles = _configured_implementer_roles(
        config,
        plan_data=plan_data,
        extra_role=None if audit_role else role_name,
    )
    audit_roles = _configured_audit_roles(
        config,
        plan_data=plan_data,
        extra_role=role_name if audit_role else None,
    )
    include_designer = any(role.role == "designer" for role in config.roles)
    return build_plan(
        project=config.project,
        project_name=config.project_name,
        owner_user=str(_loaded_plan_field(plan_data, "owner_user", config.run_as_user or current_user_name())),
        owner_home=_owner_home_from_plan_data(config, plan_data),
        port=_loaded_plan_field(plan_data, "port", None),
        database=_loaded_plan_field(plan_data, "database", None),
        source_repo=Path(str(_loaded_plan_field(plan_data, "source_repo", _repo_root()))),
        commit_git_dir=_commit_git_dir_from_plan_data(config, plan_data),
        ticket_prefix=str(_loaded_plan_field(plan_data, "ticket_prefix", config.ticket_prefix)),
        board_root=(
            Path(str(plan_data["board_root"]))
            if plan_data.get("board_root")
            else _tenant_board_root_from_config(config)
        ),
        asset_dir=Path(str(plan_data["asset_dir"])) if plan_data.get("asset_dir") else None,
        frame_dir=Path(str(plan_data["frame_dir"])) if plan_data.get("frame_dir") else None,
        implementer_roles=implementer_roles,
        include_designer=include_designer,
        include_audit=bool(audit_roles),
        audit_roles=audit_roles,
        board_service_traversal=bool(_loaded_plan_field(plan_data, "board_service_traversal", True)),
        vcs_close_role=_vcs_close_role_from_plan_data(plan_data),
    )


def _project_plan_for_vcs_close_role(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
) -> ProjectBoardProvision:
    role = role_name.strip().lower()
    if not ROLE_RE.fullmatch(role):
        raise SystemExit("team-launcher: VCS close role must match ^[a-z][a-z0-9_-]{0,63}$")
    if config.project == "pgu":
        raise SystemExit("team-launcher: pgu uses the full built-in workflow; set-vcs-close-role is only for provisioned projects")
    configured_roles = {configured.role for configured in config.roles}
    if role not in configured_roles:
        raise SystemExit(f"team-launcher: VCS close role {role!r} does not exist in project {config.project}")
    plan_data = _plan_data_from_config(config, config_path)
    if plan_data.get("workflow") or _load_json(config_path).get("workflow"):
        raise SystemExit("use ticket-board-workflow apply to update configured roles and stages")
    audit_roles = _configured_audit_roles(config, plan_data=plan_data)
    implementer_roles = _configured_implementer_roles(config, plan_data=plan_data)
    include_designer = any(configured.role == "designer" for configured in config.roles)
    return build_plan(
        project=config.project,
        project_name=config.project_name,
        owner_user=str(_loaded_plan_field(plan_data, "owner_user", config.run_as_user or current_user_name())),
        owner_home=_owner_home_from_plan_data(config, plan_data),
        port=_loaded_plan_field(plan_data, "port", None),
        database=_loaded_plan_field(plan_data, "database", None),
        source_repo=Path(str(_loaded_plan_field(plan_data, "source_repo", _repo_root()))),
        commit_git_dir=_commit_git_dir_from_plan_data(config, plan_data),
        ticket_prefix=str(_loaded_plan_field(plan_data, "ticket_prefix", config.ticket_prefix)),
        board_root=(
            Path(str(plan_data["board_root"]))
            if plan_data.get("board_root")
            else _tenant_board_root_from_config(config)
        ),
        asset_dir=Path(str(plan_data["asset_dir"])) if plan_data.get("asset_dir") else None,
        frame_dir=Path(str(plan_data["frame_dir"])) if plan_data.get("frame_dir") else None,
        implementer_roles=implementer_roles,
        include_designer=include_designer,
        include_audit=bool(audit_roles),
        audit_roles=audit_roles,
        board_service_traversal=bool(_loaded_plan_field(plan_data, "board_service_traversal", True)),
        vcs_close_role=role,
    )


def _next_visible_role_slot(config: ProjectConfig) -> int:
    slots = [role.slot for role in config.roles if not role.detached and role.slot is not None]
    return max(slots) + 1 if slots else 0


def _add_role_payload(
    config: ProjectConfig,
    *,
    role_name: str,
    cli: str,
    detached: bool,
    slot: int | None,
    directorctl: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cli": [cli],
        "live_commands": [cli],
        "role": role_name,
        "run_as_user": role_account_name(config.project, role_name),
        "target": f"{config.project}-{role_name}:0.0",
        "tmux_session": f"{config.project}-{role_name}",
        "yolo": True,
    }
    if directorctl:
        payload["env"] = {"TICKET_BOARD_DIRECTORCTL": directorctl}
    if not detached:
        if slot is None:
            raise ValueError("visible role requires slot")
        payload["slot"] = slot
    else:
        payload["detached"] = True
    if config.control_repository is not None and config.worktree_base is not None:
        payload["workdir"] = str(config.worktree_base / role_name)
    elif config.repository is not None:
        payload["workdir"] = str(config.repository)
    return payload


def _update_project_design_artifact_for_role(
    config: ProjectConfig,
    config_path: Path,
    *,
    role_name: str,
    cli: str,
    audit_role: bool = False,
) -> Path | None:
    artifact_path = config_path.parent.parent / f"{config.project}.project.json"
    try:
        artifact = _load_json(artifact_path)
    except OSError:
        return None
    project_data = artifact.get("project")
    if not isinstance(project_data, dict):
        return None
    roles_key = "audit_roles" if audit_role else "roles"
    raw_roles = project_data.get(roles_key)
    if isinstance(raw_roles, list):
        roles = [str(item).strip().lower() for item in raw_roles if str(item).strip()]
    else:
        roles = []
    if role_name not in roles:
        roles.append(role_name)
        project_data[roles_key] = roles
    if audit_role:
        project_data["include_audit"] = True
    raw_role_clis = project_data.get("role_clis")
    if not isinstance(raw_role_clis, dict):
        raw_role_clis = {}
        project_data["role_clis"] = raw_role_clis
    raw_role_clis[role_name] = cli
    _write_json_atomic(artifact_path, artifact)
    return artifact_path


def _write_added_role_config(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    cli: str,
    detached: bool,
    slot: int | None,
    relayout: bool = False,
    audit_role: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> tuple[ProjectConfig, bool]:
    if any(role.role == role_name for role in config.roles):
        raise SystemExit(f"team-launcher: role {role_name!r} already exists in project {config.project}")
    if slot is not None and slot < 0:
        raise SystemExit("team-launcher: add-role slot must be non-negative")
    should_regenerate_layout = False
    if not detached:
        visible_count = sum(1 for role in config.roles if not role.detached)
        if visible_count >= MAX_VISIBLE_PANES_PER_WINDOW:
            raise SystemExit(
                f"team-launcher: cannot add {role_name} as visible; at most {MAX_VISIBLE_PANES_PER_WINDOW} panes "
                "can be visible in one window; add it with --detached or detach another role first"
            )
        if slot is None:
            slot = _next_visible_role_slot(config)
        occupant = next(
            (role for role in config.roles if not role.detached and role.slot == slot),
            None,
        )
        if occupant is not None:
            raise SystemExit(f"team-launcher: cannot add {role_name} to slot {slot}; slot is occupied by {occupant.role}")
        next_slot = _next_visible_role_slot(config)
        recognized_generated_layout = _is_recognized_generated_project_layout(config, config_path=config_path)
        if recognized_generated_layout:
            if slot != next_slot:
                raise SystemExit(f"team-launcher: generated layouts append new visible roles at slot {next_slot}")
            should_regenerate_layout = True
        elif relayout:
            if slot != next_slot:
                raise SystemExit(
                    f"team-launcher: --relayout regenerates visible roles contiguously; "
                    f"add {role_name} at slot {next_slot} or omit --slot"
                )
            should_regenerate_layout = True
        else:
            layout_slot_count = _layout_slot_count(config)
            if slot < layout_slot_count:
                should_regenerate_layout = False
            else:
                generated_count = max(next_slot + 1, visible_count + 1)
                raise SystemExit(
                    f"team-launcher: cannot add {role_name} to visible slot {slot}; layout {config.layout} "
                    f"has {layout_slot_count} slot(s), so no pane exists for the new role; use --detached "
                    f"to add it headless, or pass --relayout to replace the existing layout with a generated "
                    f"{generated_count}-pane layout"
                )
    raw_config = _load_json(config_path)
    raw_roles = raw_config.get("roles")
    if not isinstance(raw_roles, list):
        raise SystemExit(f"{config_path} must define a roles list")
    board_root = _tenant_board_root_from_config_or_plan(config, config_path)
    directorctl = str(board_root / "current" / "scripts" / "directorctl") if board_root is not None else ""
    raw_roles.append(
        _add_role_payload(
            config,
            role_name=role_name,
            cli=cli,
            detached=detached,
            slot=slot,
            directorctl=directorctl,
        )
    )
    _write_json_atomic(config_path, raw_config)
    ensure_owner_file(config, config_path, runner=runner)
    updated_config = load_project_config(config.project, config_path)
    if should_regenerate_layout:
        visible_count = max(
            (role.slot or 0) for role in updated_config.roles if not role.detached
        ) + 1
        updated_config.layout.write_text(
            json.dumps(_new_project_layout_payload(visible_count), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        ensure_owner_file(updated_config, updated_config.layout, runner=runner)
    artifact_path = _update_project_design_artifact_for_role(
        updated_config,
        config_path,
        role_name=role_name,
        cli=cli,
        audit_role=audit_role,
    )
    if artifact_path is not None:
        ensure_owner_file(updated_config, artifact_path, runner=runner)
    return load_project_config(config.project, config_path), should_regenerate_layout


def _write_updated_project_plan_artifacts(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    provision_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config: ProjectConfig,
) -> tuple[Path, Path, Path]:
    plan_path = provision_dir / "plan.json"
    board_unit_path = provision_dir / plan.board_unit
    add_role_sql_path = provision_dir / f"{plan.project}-add-role.sql"
    _write_json_atomic(plan_path, {key: value for key, value in plan.__dict__.items()})
    board_unit_path.write_text(render_board_unit(plan), encoding="utf-8")
    add_role_sql_path.write_text(render_add_role_sql(plan, role_name), encoding="utf-8")
    for path in (plan_path, board_unit_path, add_role_sql_path):
        ensure_owner_file(config, path, runner=runner)
    return plan_path, board_unit_path, add_role_sql_path


def _write_vcs_close_role_artifacts(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    provision_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
    config: ProjectConfig,
) -> tuple[Path, Path, Path]:
    plan_path = provision_dir / "plan.json"
    board_unit_path = provision_dir / plan.board_unit
    workflow_sql_path = provision_dir / f"{plan.project}-vcs-close-role.sql"
    _write_json_atomic(plan_path, {key: value for key, value in plan.__dict__.items()})
    board_unit_path.write_text(render_board_unit(plan), encoding="utf-8")
    workflow_sql_path.write_text(render_vcs_close_role_sql(plan), encoding="utf-8")
    for path in (plan_path, board_unit_path, workflow_sql_path):
        ensure_owner_file(config, path, runner=runner)
    return plan_path, board_unit_path, workflow_sql_path


def _apply_add_role_board_sql(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    sql = render_add_role_sql(plan, role_name)
    result = runner(
        ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", plan.admin_database_url, "-f", "-"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        reason = _proc_failure_reason(result, f"psql failed with exit {result.returncode}")
        raise SystemExit(f"team-launcher: failed to register role {role_name} in board database {plan.database}: {reason}")


def _apply_vcs_close_role_board_sql(
    plan: ProjectBoardProvision,
    *,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    sql = render_vcs_close_role_sql(plan)
    result = runner(
        ["sudo", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", plan.admin_database_url, "-f", "-"],
        input=sql,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        reason = _proc_failure_reason(result, f"psql failed with exit {result.returncode}")
        raise SystemExit(
            f"team-launcher: failed to configure VCS close role {role_name} in board database {plan.database}: {reason}"
        )


def _install_and_restart_board_unit(
    plan: ProjectBoardProvision,
    *,
    board_unit_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> None:
    install = runner(["sudo", "install", "-m", "0644", str(board_unit_path), f"/etc/systemd/system/{plan.board_unit}"])
    if install.returncode != 0:
        raise SystemExit(f"team-launcher: failed to install updated board unit {plan.board_unit}")
    daemon_reload = runner(["sudo", "systemctl", "daemon-reload"])
    if daemon_reload.returncode != 0:
        raise SystemExit("team-launcher: failed to reload systemd after updating the board unit")
    restart = runner(["sudo", "systemctl", "restart", plan.board_unit])
    if restart.returncode != 0:
        raise SystemExit(f"team-launcher: failed to restart {plan.board_unit}")


def set_project_vcs_close_role_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    plan = _project_plan_for_vcs_close_role(config, config_path=config_path, role_name=role_name)
    role = dict(plan.operation_allowed_roles)["mark_done"][0]
    render_vcs_close_role_sql(plan)
    plan_path, board_unit_path, workflow_sql_path = _write_vcs_close_role_artifacts(
        plan,
        role_name=role,
        provision_dir=config_path.parent,
        runner=runner,
        config=config,
    )
    _apply_vcs_close_role_board_sql(plan, role_name=role, runner=runner)
    _install_and_restart_board_unit(plan, board_unit_path=board_unit_path, runner=runner)
    print_func(
        f"team-launcher: set VCS close role for {config.project} to {role}; "
        f"updated {plan_path}, {board_unit_path}, and {workflow_sql_path}"
    )
    return 0


def add_project_role_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    cli: str = "codex",
    detached: bool = False,
    slot: int | None = None,
    relayout: bool = False,
    audit_role: bool = False,
    start: bool = True,
    script_path: Path | None = None,
    pane_state_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
    # A new role can only be started once the Unix account it must run as
    # exists; injectable so tests can state that precondition explicitly.
    account_exists: Callable[[str], bool] = local_account_exists,
) -> int:
    role = (
        _validate_new_project_audit_role(role_name, context="audit role")
        if audit_role
        else _validate_new_project_implementer_role(role_name, context="role")
    )
    cli_name = _validate_new_project_cli(cli, context=f"CLI for {role}")
    preflight_plan = _project_plan_for_added_role(
        config,
        config_path=config_path,
        role_name=role,
        audit_role=audit_role,
    )
    render_add_role_sql(preflight_plan, role)
    existing_role = next((candidate for candidate in config.roles if candidate.role == role), None)
    recovering_existing_role = existing_role is not None
    if recovering_existing_role:
        updated_config = config
        regenerated_layout = False
    else:
        updated_config, regenerated_layout = _write_added_role_config(
            config,
            config_path=config_path,
            role_name=role,
            cli=cli_name,
            detached=detached,
            slot=slot,
            relayout=relayout,
            audit_role=audit_role,
            runner=runner,
        )
    plan = _project_plan_for_added_role(
        updated_config,
        config_path=config_path,
        role_name=role,
        audit_role=audit_role,
    )
    _plan_path, board_unit_path, _sql_path = _write_updated_project_plan_artifacts(
        plan,
        role_name=role,
        provision_dir=config_path.parent,
        runner=runner,
        config=updated_config,
    )
    _apply_add_role_board_sql(plan, role_name=role, runner=runner)
    _install_and_restart_board_unit(plan, board_unit_path=board_unit_path, runner=runner)
    role_runner = runner
    if updated_config.run_as_user and current_user_name() != updated_config.run_as_user:
        role_runner = _owner_project_git_runner(
            owner_user=updated_config.run_as_user,
            project_dir=updated_config.repository or updated_config.pane_launcher or Path("/"),
            owned_roots=_control_repository_owned_roots(updated_config),
            runner=runner,
        )
    worktree_result = ensure_project_worktrees(
        replace(updated_config, roles=[_role_by_name(updated_config, role)]),
        refresh=True,
        runner=role_runner,
    )
    if not worktree_result.ok:
        reason = next(iter(worktree_result.failed_roles.values()), "unknown error")
        raise SystemExit(f"team-launcher: failed to prepare worktree for {role}: {reason}")
    pending_account_commands = ""
    if start:
        pane_role = _role_by_name(updated_config, role)
        pane_user = role_run_as_user(updated_config, pane_role)
        # A role started under the wrong account has the wrong uid, and the
        # board would give it either no authority or another role's. Refuse to
        # start it and hand the operator the commands that create the account,
        # rather than starting something that looks fine and is not (SYRD-39).
        if pane_user and not account_exists(pane_user):
            from scripts.ticket_board.project_provision import role_account_commands

            pending_account_commands = role_account_commands(
                updated_config.project,
                role,
                updated_config.run_as_user or current_user_name(),
                board_service_user(updated_config),
                worktree=pane_role.workdir,
            )
        else:
            pane_script_path = updated_config.pane_launcher or script_path or Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME)
            pane_args = pane_command_args(
                updated_config.project,
                pane_role,
                config_path=config_path,
                mode="attach-or-start",
                script_path=pane_script_path,
                pane_state_dir=pane_state_dir or default_pane_state_dir_for_user(pane_user, project=updated_config.project),
                skip_launcher_check=True,
                no_attach=True,
                run_as_user=pane_user,
            )
            pane_proc = runner(pane_args)
            if pane_proc.returncode != 0:
                raise SystemExit(f"team-launcher: added {role}, but failed to start its pane with exit {pane_proc.returncode}")
    if recovering_existing_role:
        message = f"team-launcher: role {role} already exists in {updated_config.project}; reapplied board registration"
    else:
        role_label = "auditor role" if audit_role else "role"
        message = f"team-launcher: added {role_label} {role} to {updated_config.project}"
    if pending_account_commands:
        message += (
            f"; its Unix account {role_run_as_user(updated_config, _role_by_name(updated_config, role))} "
            "does not exist yet, so the role was not started. Run these as an operator, then start it:\n"
            + pending_account_commands
        )
    if not detached:
        visible_count = sum(1 for candidate in updated_config.roles if not candidate.detached)
        if regenerated_layout:
            message += f"; regenerated layout for {visible_count} visible pane(s)"
        if start:
            if recovering_existing_role:
                message += "; started tmux session for the existing role"
            else:
                message += "; started tmux session for the new role"
        message += "; relaunch the project window to display newly added visible slots"
    print_func(message)
    return 0


def stop_project(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    # The viewer and display sessions belong to the project owner; each role's
    # session lives in that role's own tmux server, so it has to be probed and
    # killed there or stop reports success while the session is still alive
    # (SYRD-39).
    owner_runner = runner
    if config.run_as_user and current_user_name() != config.run_as_user:
        owner_runner = _owner_process_runner(owner_user=config.run_as_user, runner=runner)
    exit_code = 0
    viewer_session = viewer_session_for_project(config.project)
    viewer_exists = owner_runner(
        tmux_has_session_by_name_args(viewer_session),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if viewer_exists:
        result = owner_runner(
            tmux_kill_session_by_name_args(viewer_session),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            reason = _proc_failure_reason(result, f"tmux kill-session failed with exit {result.returncode}")
            print_func(f"failed to stop viewer: {viewer_session}: {reason}")
            exit_code = exit_code or int(result.returncode)
        else:
            print_func(f"stopped viewer: {viewer_session}")
    else:
        print_func(f"already stopped viewer: {viewer_session}")
    from scripts import presentation_controller

    presentation_stop = presentation_controller.stop_presentation(
        config,
        runner=runner,
        print_func=print_func,
    )
    exit_code = exit_code or presentation_stop
    for role in config.roles:
        role_runner = role_process_runner_for(config, role, runner=runner)
        exists = role_runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if not exists:
            print_func(f"already stopped {role.role}: {role.tmux_session}")
            continue
        result = role_runner(tmux_kill_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            reason = _proc_failure_reason(result, f"tmux kill-session failed with exit {result.returncode}")
            print_func(f"failed to stop {role.role}: {role.tmux_session}: {reason}")
            exit_code = exit_code or int(result.returncode)
            continue
        print_func(f"stopped {role.role}: {role.tmux_session}")
    return exit_code


def _layout_slot_count(config: ProjectConfig) -> int:
    try:
        layout = json.loads(config.layout.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot read layout {config.layout}: {exc}") from exc
    return len(_layout_leaves(layout))


def _raw_role_for_update(raw_roles: Any, role_name: str) -> dict[str, Any]:
    if not isinstance(raw_roles, list):
        raise SystemExit("team-launcher: launcher config roles must be a JSON list")
    for raw_role in raw_roles:
        if isinstance(raw_role, dict) and str(raw_role.get("role") or "").strip() == role_name:
            return raw_role
    raise SystemExit(f"unknown role {role_name!r} in launcher config")


def _write_role_visibility(
    config: ProjectConfig,
    *,
    config_path: Path,
    role: RoleConfig,
    detached: bool,
    slot: int | None,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> ProjectConfig:
    raw_config = _load_json(config_path)
    raw_role = _raw_role_for_update(raw_config.get("roles"), role.role)
    if detached:
        raw_role["detached"] = True
        raw_role.pop("slot", None)
    else:
        if slot is None:
            raise ValueError("visible role update requires slot")
        raw_role["detached"] = False
        raw_role["slot"] = slot
    try:
        _write_json_atomic(config_path, raw_config)
    except OSError as exc:
        raise SystemExit(f"team-launcher: cannot update launcher config {config_path}: {exc}") from exc
    ensure_owner_file(config, config_path, runner=runner)
    return load_project_config(config.project, config_path)


def attach_role_to_slot(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    slot: int,
    session_dir: Path,
    pane_state_dir: Path = DEFAULT_PANE_STATE_DIR,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    if slot < 0:
        raise SystemExit("team-launcher: attach-role slot must be non-negative")
    slot_count = _layout_slot_count(config)
    if slot >= slot_count:
        raise SystemExit(
            f"team-launcher: cannot attach {role_name} to slot {slot}; layout {config.layout} has {slot_count} slot(s)"
        )
    role = _role_by_name(config, role_name)
    occupant = next(
        (
            candidate
            for candidate in config.roles
            if candidate.role != role.role and not candidate.detached and candidate.slot == slot
        ),
        None,
    )
    if occupant is not None:
        raise SystemExit(
            f"team-launcher: cannot attach {role.role} to slot {slot}; slot {slot} is occupied by {occupant.role}"
        )
    if not role.detached:
        if role.slot == slot:
            print_func(f"team-launcher: role {role.role} is already attached to slot {slot}")
            return 0
        raise SystemExit(f"team-launcher: role {role.role} is already attached to slot {role.slot}")
    visible_count = sum(1 for candidate in config.roles if not candidate.detached)
    if visible_count >= MAX_VISIBLE_PANES_PER_WINDOW:
        raise SystemExit(
            f"team-launcher: cannot attach {role.role}; at most {MAX_VISIBLE_PANES_PER_WINDOW} panes "
            "can be visible in one window; detach another role first"
        )
    conflict = _ambient_session_conflict_for_role(role, session_dir=session_dir)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1
    exists = runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    session_id = session_id_for_role(role, session_dir)
    if not exists:
        if not session_id:
            print(f"team-launcher: cannot attach {role.role}; no live session or recorded resume id", file=sys.stderr)
            return 1
        preflight_ok, preflight_message = _resume_preflight_allows_attempt(role, session_id, session_dir=session_dir)
        if not preflight_ok:
            print(preflight_message, file=sys.stderr)
            return 1
    updated_config = _write_role_visibility(
        config,
        config_path=config_path,
        role=role,
        detached=False,
        slot=slot,
        runner=runner,
    )
    updated_role = _role_by_name(updated_config, role.role)
    if not exists:
        prepare_hermes_home_for_role(updated_role, session_dir=session_dir)
        start_proc = runner(
            tmux_new_session_args(updated_role, session_dir=session_dir, pane_state_dir=pane_state_dir, resume=True)
        )
        if start_proc.returncode != 0:
            _write_role_visibility(
                updated_config,
                config_path=config_path,
                role=updated_role,
                detached=True,
                slot=None,
                runner=runner,
            )
            return int(start_proc.returncode)
        resume_status = _resume_launch_status(updated_role, runner=runner)
        if resume_status == RESUME_LAUNCH_VERIFIED:
            clear_unverified_resume_for_role(updated_role, session_dir)
            seed_initial_pane_idle_state(updated_role, pane_state_dir=pane_state_dir, source="team_launcher.attach_role")
        else:
            _write_role_visibility(
                updated_config,
                config_path=config_path,
                role=updated_role,
                detached=True,
                slot=None,
                runner=runner,
            )
            print(
                f"team-launcher: resume for {updated_role.role} using session {session_id} was not verified; "
                "leaving tmux session and session record intact",
                file=sys.stderr,
            )
            return 1
    print_func(f"team-launcher: attached role {role.role} to slot {slot}; refusing to relayout other panes")
    return runner(tmux_attach_args(updated_role)).returncode


def detach_role_from_slot(
    config: ProjectConfig,
    *,
    config_path: Path,
    role_name: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    role = _role_by_name(config, role_name)
    if role.detached:
        print_func(f"team-launcher: role {role.role} is already detached")
        return 0
    previous_slot = role.slot
    _write_role_visibility(
        config,
        config_path=config_path,
        role=role,
        detached=True,
        slot=None,
        runner=runner,
    )
    print_func(f"team-launcher: detached role {role.role} from slot {previous_slot}; tmux session remains headless")
    if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        return 0
    detach_proc = runner(tmux_detach_clients_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if detach_proc.returncode != 0:
        print_func(f"team-launcher: no live tmux client detached for {role.role}; session remains configured headless")
    return 0


def _role_by_name(config: ProjectConfig, role_name: str) -> RoleConfig:
    for role in config.roles:
        if role.role == role_name:
            return role
    raise SystemExit(f"unknown role {role_name!r} in project {config.project}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch or reload a JSON-configured project team.")
    parser.add_argument("project", help="project name, for example pgu")
    parser.add_argument(
        "command",
        nargs="?",
        default="start",
        choices=[
            "start",
            "attach",
            "reload",
            "stop",
            "design",
            "new",
            "provision-runtime",
            "deploy-launcher",
            "upgrade",
            "add-role",
            "set-vcs-close-role",
            "pane",
            "teardown",
        ],
        help="start is idempotent attach-or-start (resumes tracked session ids when relaunching a stopped pane); reload force-restarts running CLIs with tracked resume ids",
    )
    parser.add_argument("pane_mode", nargs="?")
    parser.add_argument("role", nargs="?")
    parser.add_argument("--slot", type=int, help="layout slot for `pane attach-role <role>`")
    parser.add_argument("--config", type=Path, help="project launcher config JSON")
    parser.add_argument("--layout-output", type=Path, help="write generated Konsole layout here")
    parser.add_argument(
        "--layout",
        choices=sorted(LAYOUT_MODE_CHOICES),
        default=LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
    parser.add_argument("--script-path", type=Path, default=Path(__file__).resolve().with_name("team-launcher"))
    parser.add_argument("--pane-state-dir", type=Path, help=f"write initial pane idle state here (default: {DEFAULT_PANE_STATE_DIR})")
    parser.add_argument("--no-attach", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="print launch plan without starting Konsole")
    parser.add_argument("--confirm", help="project slug required for destructive teardown")
    parser.add_argument(
        "--drop-nonempty-board",
        action="store_true",
        help="allow teardown to drop a board database that still contains tickets",
    )
    parser.add_argument(
        "--destroy-registered-tenant",
        action="store_true",
        help="allow teardown to remove a registered launchable tenant; off by default",
    )
    parser.add_argument(
        "--remove-owner-home",
        action="store_true",
        help="teardown may remove /home/<owner>; off by default because it can contain user work",
    )
    parser.add_argument("--remove-owner-user", action="store_true", help="teardown may remove the owner Unix account")
    parser.add_argument("--owner-user", help="new project owner Unix user (default: <project>-agent)")
    parser.add_argument("--port", type=int, help="new project board port; omitted means deterministic allocation")
    parser.add_argument("--database", help="new project PostgreSQL database; omitted means <project>_ticket_board")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--repository", type=Path, help="project working checkout opened by generated panes")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design command")
    parser.add_argument("--new-output-dir", type=Path, help="write new-project artifacts here")
    parser.add_argument("--design-output-dir", type=Path, help="write design document and project artifact here")
    parser.add_argument("--project-artifact", type=Path, help="write project artifact here during design")
    parser.add_argument("--design-document", type=Path, help="write project design document here during design")
    parser.add_argument("--design-title", help="project design document title")
    parser.add_argument("--design-body", help="project design document body")
    parser.add_argument("--remote", help="project git remote name for generated launcher config")
    parser.add_argument("--default-branch", help="project default branch for generated launcher config")
    parser.add_argument("--worktree-policy", choices=sorted(WORKTREE_POLICIES), help="shared or isolated role worktrees")
    parser.add_argument("--ticket-prefix", help="ticket id prefix for the new board, e.g. OTTO")
    parser.add_argument("--upstream-report-url", help="board URL where tenant reports should be filed")
    parser.add_argument("--upstream-report-token-file", help="0600 file containing the report-only token for --upstream-report-url")
    parser.add_argument("--push-policy", help="reviewable push policy label recorded in the design artifact")
    parser.add_argument("--audit-signoff", action=argparse.BooleanOptionalAction, default=None, help="record whether audit signoff is a project gate")
    parser.add_argument("--needs-inspection", action=argparse.BooleanOptionalAction, default=None, help="record whether inspection is a project gate")
    parser.add_argument("--needs-user-signoff", action=argparse.BooleanOptionalAction, default=None, help="record whether user signoff is a project gate")
    parser.add_argument("--board-service-traversal", action=argparse.BooleanOptionalAction, default=None, help="record whether boardsvc may traverse the owner home")
    parser.add_argument("--supplementary-group", action="append", dest="supplementary_groups", help="owner-user supplementary group to record; repeat as needed")
    parser.add_argument("--linger", action=argparse.BooleanOptionalAction, default=None, help="record whether linger should be enabled")
    parser.add_argument("--owner-shell", help="owner user's shell to record")
    parser.add_argument("--execute", action="store_true", help="execute new-project provisioning after precheck")
    parser.add_argument("--runtime-user", help="local user whose lingering /run/user/<uid> runtime should be provisioned")
    parser.add_argument("--launcher-repo", type=Path, help="launcher checkout to update or verify (default: this script's repo)")
    parser.add_argument("--deploy-ref", default=DEFAULT_TENANT_RELEASE_DEPLOY_REF, help="board release ref to deploy during upgrade (default: origin/main)")
    parser.add_argument("--cli", dest="add_role_cli", default="codex", help="CLI runtime for `add-role` (default: codex)")
    parser.add_argument("--audit", dest="add_role_audit", action="store_true", help="add the role as an auditor instead of an implementer")
    parser.add_argument("--detached", action="store_true", help="configure `add-role` as headless instead of visible")
    parser.add_argument("--relayout", action="store_true", help="replace the existing layout with a generated layout when adding a visible role")
    parser.add_argument("--clean-launcher", action="store_true", help="run git clean -fdx after updating --launcher-repo")
    parser.add_argument("--force", action="store_true", help="allow reload to kill/relaunch even if live command validation fails")
    parser.add_argument("--allow-stale-launcher", action="store_true", help="emergency override: warn but proceed when the launcher checkout is provably stale")
    parser.add_argument("--no-launcher-self-deploy", action="store_true", help="restore refuse-only behavior instead of automatically fast-forwarding a stale launcher checkout")
    parser.add_argument("--skip-launcher-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--desktop-policy", type=Path, help="approved Wayland JSON policy or headless; installed before launch")
    parser.add_argument("--workflow-config", type=Path, help="declarative roles/stages JSON for the new project")
    return parser


def _build_switchyard_new_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard new", description="Create a Switchyard project.")
    parser.add_argument("--slug", help="project slug; prompted when omitted")
    parser.add_argument("--agent-name", help="owner user; prompted default is <slug>-agent when omitted")
    parser.add_argument("--project-name", help="human project name; prompted when omitted")
    parser.add_argument("--project-path", type=Path, help="project working directory; prompted when omitted")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design session")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="git repository path, or colon-separated paths, used to verify board commit hashes",
    )
    parser.add_argument("--output-dir", type=Path, help="write provisioning artifacts here")
    parser.add_argument("--port", type=int, help="HTTP port; omitted means deterministic allocation")
    parser.add_argument("--database", help="PostgreSQL database; omitted means <slug>_ticket_board")
    parser.add_argument("--yes", action="store_true", help="proceed without the confirmation prompt")
    parser.add_argument(
        "--no-git-init",
        action="store_true",
        help="do not initialize --project-path; it must already be a git repository with an initial commit",
    )
    parser.add_argument(
        "--allow-existing-owner-user",
        action="store_true",
        help="reuse an existing owner user without the existing-user confirmation prompt",
    )
    parser.add_argument(
        "--agy-credential-source",
        metavar="USER",
        help=(
            "override this host's recorded agy credential source for this project; copies USER's "
            "agy OAuth token into the owner user so the project's panes do not need their own "
            "agy login, sharing that one Google account across every role"
        ),
    )
    parser.add_argument(
        "--no-agy-credential",
        action="store_true",
        help="do not seed any agy credential for this project, ignoring this host's recorded source",
    )
    parser.add_argument(
        "--layout",
        choices=sorted(LAYOUT_MODE_CHOICES),
        default=LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
    parser.add_argument("--desktop-policy", type=Path, help="headless, or a JSON file recording scoped Wayland consent; installed before role launch")
    parser.add_argument("--workflow-config", type=Path, help="declarative roles/stages JSON for the new project")
    return parser


def _build_switchyard_register_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard register", description="Register an existing Switchyard project config.")
    parser.add_argument("config_path", type=Path, help="path to the project's generated launcher config JSON")
    return parser


def _build_switchyard_upgrade_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard upgrade", description="Upgrade safe generated artifacts for a Switchyard project.")
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing files")
    parser.add_argument("--deploy-ref", default=DEFAULT_TENANT_RELEASE_DEPLOY_REF, help="board release ref to deploy (default: origin/main)")
    parser.add_argument("--source-repo", type=Path, help="Switchyard source checkout or exported release to deploy")
    parser.add_argument(
        "--commit-git-dir",
        help="replace and persist the git repository path(s) used to verify board commit hashes",
    )
    parser.add_argument("--desktop-policy", type=Path, help="headless, or a JSON file recording scoped Wayland consent; installed before role launch")
    return parser


def _build_switchyard_add_role_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard add-role", description="Add a role to an existing Switchyard project.")
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("role", help="new role name")
    parser.add_argument("--cli", default="codex", help="CLI runtime for the role (default: codex)")
    parser.add_argument("--audit", action="store_true", help="add the role as an auditor instead of an implementer")
    parser.add_argument("--slot", type=int, help="visible layout slot; generated layouts append automatically when omitted")
    parser.add_argument("--detached", action="store_true", help="start the role as a headless tmux session")
    parser.add_argument("--relayout", action="store_true", help="replace the existing layout with a generated layout when adding a visible role")
    parser.add_argument("--no-start", action="store_true", help="register the role without starting its pane")
    return parser


def _build_switchyard_set_vcs_close_role_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard set-vcs-close-role",
        description="Set the role that closes a provisioned project after final sign-off.",
    )
    parser.add_argument("project", help="project name or slug")
    parser.add_argument("role", help="existing project role allowed to mark tickets done")
    return parser


def _build_switchyard_stop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard stop", description="Stop a Switchyard project's tmux pane sessions.")
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_teardown_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard teardown",
        description="Remove Switchyard-created provisioning artifacts for a project.",
    )
    parser.add_argument("project", help="project slug")
    parser.add_argument("--dry-run", action="store_true", help="list exactly what would be removed without changing anything")
    parser.add_argument("--confirm", help="required project slug for the destructive run")
    parser.add_argument("--owner-user", help="owner user for an unregistered partial provision (default: <project>-agent)")
    parser.add_argument(
        "--drop-nonempty-board",
        action="store_true",
        help="allow dropping a board database that still contains tickets",
    )
    parser.add_argument(
        "--destroy-registered-tenant",
        action="store_true",
        help="allow removing a registered launchable tenant; off by default",
    )
    parser.add_argument(
        "--remove-owner-home",
        action="store_true",
        help="also remove /home/<owner>; off by default because it can contain user work",
    )
    parser.add_argument(
        "--remove-owner-user",
        action="store_true",
        help="also remove the owner Unix account; off by default",
    )
    return parser


def _build_switchyard_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard status", description="List registered Switchyard projects and pane liveness.")
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable project status")
    return parser


def _build_switchyard_present_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="switchyard present",
        description="Map persistent project role sessions into stable display slots.",
    )
    parser.add_argument("project", help="registered project name or slug")
    actions = parser.add_subparsers(dest="action", required=True)
    list_parser = actions.add_parser("list", help="show desired and actual display state")
    list_parser.add_argument("--json", action="store_true", help="emit machine-readable presentation state")
    show_parser = actions.add_parser("show", help="move a role into a display slot")
    show_parser.add_argument("role")
    show_parser.add_argument("--slot", type=int, required=True)
    swap_parser = actions.add_parser("swap", help="swap two display slots")
    swap_parser.add_argument("slot_a", type=int)
    swap_parser.add_argument("slot_b", type=int)
    hide_parser = actions.add_parser("hide", help="hide the role in a display slot")
    hide_parser.add_argument("--slot", type=int, required=True)
    focus_parser = actions.add_parser("focus", help="select a display slot in the project viewer")
    focus_parser.add_argument("--slot", type=int, required=True)
    restore_parser = actions.add_parser("restore", help="restore a configured presentation layout")
    restore_parser.add_argument("--layout", default="default")
    bootstrap_parser = actions.add_parser("bootstrap", help="create stable slots and a presentation window")
    bootstrap_parser.add_argument("--layout", choices=(LAYOUT_MODE_VIEWER, LAYOUT_MODE_SEPARATE), default=LAYOUT_MODE_VIEWER)
    recover_parser = actions.add_parser("recover", help="make one bounded start/resume attempt for a role")
    recover_parser.add_argument("role")
    return parser


def switchyard_present_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    args: argparse.Namespace,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    from scripts import presentation_controller

    if args.action == "list":
        report = presentation_controller.presentation_report(
            config,
            config_path=config_path,
            runner=runner,
        )
        presentation_controller.print_presentation_report(report, json_output=args.json, print_func=print_func)
        return 0
    presentation_controller.presentation_action(
        config,
        config_path=config_path,
        action=args.action,
        role_name=getattr(args, "role", None),
        slot=getattr(args, "slot", getattr(args, "slot_a", None)),
        other_slot=getattr(args, "slot_b", None),
        layout=getattr(args, "layout", "default"),
        runner=runner,
    )
    report = presentation_controller.presentation_report(config, config_path=config_path, runner=runner)
    presentation_controller.print_presentation_report(report, json_output=False, print_func=print_func)
    return 0


def switchyard_help_text() -> str:
    commands = ", ".join(SWITCHYARD_COMMANDS)
    return f"""Usage:
  switchyard
  switchyard <project name or slug>
  switchyard <command> [options]

Commands:
  board-skill      install or verify the portable board skill for every agent CLI
  new              create and provision a new project
  register         register an existing project config
  upgrade          update generated project artifacts and report release drift
  add-role         add an implementer or auditor role, worktree, pane, and board registration
  present          map persistent role sessions into stable display slots at runtime
  set-vcs-close-role
                   set which existing project role can mark tickets done
  agy-credential   show, set, or clear this host's agy credential source
  role-prompt      show, set, or clear a role's onboarding prompt
  stop             stop a project's configured tmux pane sessions
  teardown         remove project board provisioning artifacts after a dry-run review
  status           list registered projects and pane liveness
  validate-models  check configured role models without starting panes

Bare project names start or attach the project. Recognized commands: {commands}.
"""


def _switchyard_command_display(argv: Sequence[str]) -> str:
    if not argv:
        return "switchyard"
    return f"switchyard {argv[0]}"


def switchyard_invocation_requires_root(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    command = argv[0].casefold()
    if command in {"-h", "--help", "help", "--version", "version"}:
        return False
    if len(argv) >= 2 and argv[1] in {"-h", "--help"}:
        return False
    return command in SWITCHYARD_PRIVILEGED_COMMANDS


def _switchyard_user_can_prompt_for_sudo() -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    try:
        user = pwd.getpwuid(os.geteuid())
        group_ids = {user.pw_gid, *os.getgroups()}
    except KeyError:
        return False
    group_names: set[str] = set()
    for gid in group_ids:
        try:
            group_names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            continue
    return bool({"sudo", "wheel", "admin"} & group_names)


def _switchyard_exec_with_root(
    argv: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    exec_func: Callable[[str, Sequence[str]], Any] = os.execvp,
) -> None:
    sudo_bin = os.environ.get("SWITCHYARD_SUDO_BIN", "sudo")
    command_display = _switchyard_command_display(argv)
    target_argv = [sys.argv[0], *argv]
    if runner([sudo_bin, "-n", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        exec_func(sudo_bin, [sudo_bin, "-n", *target_argv])
        raise SystemExit(0)
    if _switchyard_user_can_prompt_for_sudo():
        exec_func(sudo_bin, [sudo_bin, *target_argv])
        raise SystemExit(0)
    raise SystemExit(
        f"switchyard: command requires root: {command_display}\n"
        "switchyard: sudo is unavailable for this user or shell; run it as a sudo-capable human or ask an operator"
    )


def _project_config_path_owner_user(path: Path) -> str:
    expanded = path.expanduser()
    parts = expanded.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "home" and parts[2]:
        return parts[2]
    try:
        return pwd.getpwuid(expanded.stat().st_uid).pw_name
    except (KeyError, OSError):
        return ""


def _require_switchyard_owner_hint_or_root(entry: SwitchyardProjectEntry, argv: Sequence[str]) -> None:
    owner = _project_config_path_owner_user(entry.config_path)
    if not owner or current_user_name() == owner or os.geteuid() == 0:
        return
    _switchyard_exec_with_root(argv)


def _require_switchyard_project_owner_or_root(config: ProjectConfig, argv: Sequence[str]) -> None:
    owner = (config.run_as_user or "").strip()
    if not owner or current_user_name() == owner or os.geteuid() == 0:
        return
    _switchyard_exec_with_root(argv)


def _load_switchyard_project_config_for_command(entry: SwitchyardProjectEntry, argv: Sequence[str]) -> ProjectConfig:
    _require_switchyard_owner_hint_or_root(entry, argv)
    try:
        config = load_project_config(entry.slug, entry.config_path)
    except PermissionError:
        if os.geteuid() != 0:
            _switchyard_exec_with_root(argv)
        raise
    _require_switchyard_project_owner_or_root(config, argv)
    return config


def switchyard_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--switchyard-wrapper-requires-root"]:
        print("requires-root" if switchyard_invocation_requires_root(argv[1:]) else "no-root")
        return 0
    if not argv:
        return switchyard_menu_command()
    if argv[0] in {"-h", "--help", "help"}:
        print(switchyard_help_text(), end="")
        return 0
    if argv[0] in {"--version", "version"}:
        print(switchyard_version_text())
        return 0
    if argv[0].casefold() == "new":
        args = _build_switchyard_new_parser().parse_args(argv[1:])
        return switchyard_new_command(
            slug=args.slug,
            agent_name=args.agent_name,
            project_name=args.project_name,
            project_path=args.project_path,
            from_artifact=args.from_artifact,
            source_repo=args.source_repo,
            workflow_config=args.workflow_config,
            commit_git_dir=args.commit_git_dir,
            output_dir=args.output_dir,
            port=args.port,
            database=args.database,
            yes=args.yes,
            desktop_policy=args.desktop_policy,
            allow_existing_owner_user=args.allow_existing_owner_user,
            agy_credential_source=args.agy_credential_source,
            no_agy_credential=args.no_agy_credential,
            layout_mode=args.layout,
            git_init=not args.no_git_init,
        )
    if argv[0].casefold() == "agy-credential":
        parser = argparse.ArgumentParser(prog="switchyard agy-credential")
        parser.add_argument("action", choices=("show", "set", "clear"))
        parser.add_argument("user", nargs="?", help="Unix user to seed agy credentials from (set only)")
        args = parser.parse_args(argv[1:])
        if args.action == "set" and not args.user:
            raise SystemExit("switchyard: agy-credential set requires a user name")
        return switchyard_agy_credential_command(args.action, source_user=args.user)
    if argv[0].casefold() == "seed-role-credentials":
        parser = argparse.ArgumentParser(prog="switchyard seed-role-credentials")
        parser.add_argument("project", help="registered project name or slug")
        parser.add_argument("--role", default="", help="seed only this role")
        parser.add_argument(
            "--reseed",
            action="store_true",
            help="replace credentials a role already has; the deliberate repair path",
        )
        args = parser.parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_seed_role_credentials_command(
            config, role_name=args.role, reseed=args.reseed
        )
    if argv[0].casefold() == "register":
        args = _build_switchyard_register_parser().parse_args(argv[1:])
        return switchyard_register_command(args.config_path)
    if argv[0].casefold() == "upgrade":
        args = _build_switchyard_upgrade_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return upgrade_project_command(
            config,
            config_path=entry.config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
            desktop_policy=args.desktop_policy,
        )
    if argv[0].casefold() == "add-role":
        args = _build_switchyard_add_role_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return add_project_role_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
            cli=args.cli,
            audit_role=args.audit,
            detached=args.detached,
            slot=args.slot,
            relayout=args.relayout,
            start=not args.no_start,
            script_path=Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME),
        )
    if argv[0].casefold() == "board-skill":
        from scripts import board_skill_cli

        return board_skill_cli.main(argv[1:], prog="switchyard board-skill")
    if argv[0].casefold() == "role-prompt":
        parser = argparse.ArgumentParser(
            prog="switchyard role-prompt",
            description=(
                "Show, set, or clear the onboarding prompt a role receives when its next "
                "conversation starts fresh. A running conversation is never interrupted or "
                "rewritten: a changed prompt is used by the next fresh session or an "
                "explicit role restart."
            ),
        )
        parser.add_argument("action", choices=("show", "set", "clear"))
        parser.add_argument("role")
        parser.add_argument(
            "--project",
            default=os.environ.get("TICKET_BOARD_PROJECT", ""),
            help="project name or slug; defaults to TICKET_BOARD_PROJECT in the caller's pane",
        )
        parser.add_argument("--prompt", help="prompt text; use --prompt-file for anything long")
        parser.add_argument(
            "--prompt-file",
            type=Path,
            help="read the prompt from a file, or from stdin when given as -",
        )
        args = parser.parse_args(argv[1:])
        if not args.project.strip():
            raise SystemExit(
                "switchyard: no project selected; pass --project or run where "
                "TICKET_BOARD_PROJECT is set"
            )
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        from scripts import workflow_manage

        forwarded = [
            f"{args.action}-role-prompt",
            "--role",
            args.role,
            "--board-url",
            config.board_url,
        ]
        if args.action != "show":
            forwarded += ["--config", str(entry.config_path)]
        if args.prompt is not None:
            forwarded += ["--prompt", args.prompt]
        if args.prompt_file is not None:
            forwarded += ["--prompt-file", str(args.prompt_file)]
        return workflow_manage.main(forwarded)
    if argv[0].casefold() == "present":
        args = _build_switchyard_present_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_present_command(config, config_path=entry.config_path, args=args)
    if argv[0].casefold() == "set-vcs-close-role":
        args = _build_switchyard_set_vcs_close_role_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return set_project_vcs_close_role_command(
            config,
            config_path=entry.config_path,
            role_name=args.role,
        )
    if argv[0].casefold() == "stop":
        args = _build_switchyard_stop_parser().parse_args(argv[1:])
        project = " ".join(args.project)
        entry = _resolve_switchyard_project(project)
        config = _load_switchyard_project_config_for_command(entry, argv)
        return stop_project(config)
    if argv[0].casefold() == "teardown":
        args = _build_switchyard_teardown_parser().parse_args(argv[1:])
        return switchyard_teardown_command(
            args.project,
            dry_run=args.dry_run,
            confirm=args.confirm,
            drop_nonempty_board=args.drop_nonempty_board,
            destroy_registered_tenant=args.destroy_registered_tenant,
            remove_owner_home=args.remove_owner_home,
            remove_owner_user=args.remove_owner_user,
            owner_user=args.owner_user,
        )
    if argv[0].casefold() == "status":
        args = _build_switchyard_status_parser().parse_args(argv[1:])
        if os.geteuid() != 0:
            _switchyard_exec_with_root(argv)
        return switchyard_status_command(json_output=args.json)
    if argv[0].casefold() == "validate-models":
        if len(argv) < 2:
            raise SystemExit("switchyard validate-models requires <project>")
        project = " ".join(argv[1:])
        entry = _resolve_switchyard_project(project)
        _load_switchyard_project_config_for_command(entry, argv)
        return switchyard_validate_models_command(project)
    selection = " ".join(argv)
    entry = _resolve_switchyard_project(selection)
    config = _load_switchyard_project_config_for_command(entry, argv)
    # Model validation intentionally runs only for `switchyard new` and the
    # explicit validate-models command. It performs provider API calls, so a
    # routine team start should not depend on provider availability.
    config = prepare_project_desktop(config)
    first_run_auth_report = run_switchyard_launch_first_run_auth(config)
    if stop_before_launch_for_missing_owner_clis(first_run_auth_report):
        return 1
    launch_result = launch_project(
        config,
        config_path=entry.config_path,
        mode="start",
        script_path=Path(__file__).resolve().with_name(TEAM_LAUNCHER_NAME),
        report_session_records=True,
    )
    if launch_result != 0:
        return launch_result
    report_first_run_auth_warnings(first_run_auth_report)
    return 0


def _reject_removed_commands(argv: Sequence[str]) -> None:
    if len(argv) > 1 and str(argv[1]).strip() in REMOVED_CONFIG_FREE_COMMANDS:
        project = str(argv[0]).strip() or "<project>"
        replacement = BOOTSTRAP_REPLACEMENT_COMMAND.replace("<project>", project)
        raise SystemExit(
            f"team-launcher: {argv[1]} has been removed; use `{replacement}` to create a launchable project config"
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _reject_removed_commands(argv)
    args = _build_parser().parse_args(argv)
    if args.command == "design":
        return design_project_command(
            args.project,
            output_dir=args.design_output_dir,
            artifact_path=args.project_artifact,
            design_document=args.design_document,
            design_title=args.design_title,
            design_body=args.design_body,
            repository=args.repository,
            remote=args.remote,
            default_branch=args.default_branch,
            worktree_policy=args.worktree_policy,
            owner_user=args.owner_user,
            ticket_prefix=args.ticket_prefix,
            push_policy=args.push_policy,
            audit_signoff=args.audit_signoff,
            needs_inspection=args.needs_inspection,
            needs_user_signoff=args.needs_user_signoff,
            board_service_traversal=args.board_service_traversal,
            supplementary_groups=args.supplementary_groups,
            linger=args.linger,
            owner_shell=args.owner_shell,
        )
    if args.command == "new":
        return new_project_command(
            args.project,
            from_artifact=args.from_artifact,
            owner_user=args.owner_user,
            desktop_policy=args.desktop_policy,
            port=args.port,
            database=args.database,
            source_repo=args.source_repo,
            workflow_config=args.workflow_config,
            commit_git_dir=args.commit_git_dir,
            repository=args.repository,
            output_dir=args.new_output_dir,
            execute=args.execute,
            dry_run=args.dry_run,
            upstream_report_url=args.upstream_report_url or "",
            upstream_report_token_file=args.upstream_report_token_file or "",
        )
    if args.command == "provision-runtime" and args.config is None:
        try:
            resolved = _resolve_launcher_project_config(args.project)
            config_path = resolved.config_path
            config_project = resolved.slug
        except SystemExit:
            config_path = DEFAULT_CONFIG_DIR / f"{args.project}.json"
            config_project = args.project
    else:
        resolved = _resolve_launcher_project_config(args.project, explicit_config=args.config)
        config_path = resolved.config_path
        config_project = resolved.slug
    if args.command == "provision-runtime":
        config = load_project_config(config_project, config_path) if config_path.exists() else None
        return provision_runtime_command(args.runtime_user, config)
    config = load_project_config(config_project, config_path)
    if args.command == "upgrade":
        return upgrade_project_command(
            config,
            config_path=config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            commit_git_dir=args.commit_git_dir,
            deploy_ref=args.deploy_ref,
            desktop_policy=args.desktop_policy,
        )
    if args.command == "add-role":
        if not args.pane_mode or args.role:
            raise SystemExit("add-role requires exactly one <role> argument")
        return add_project_role_command(
            config,
            config_path=config_path,
            role_name=args.pane_mode,
            cli=args.add_role_cli,
            audit_role=args.add_role_audit,
            detached=args.detached,
            slot=args.slot,
            relayout=args.relayout,
            start=not args.no_attach,
            script_path=args.script_path,
            pane_state_dir=args.pane_state_dir,
        )
    if args.command == "set-vcs-close-role":
        if not args.pane_mode or args.role:
            raise SystemExit("set-vcs-close-role requires exactly one <role> argument")
        return set_project_vcs_close_role_command(
            config,
            config_path=config_path,
            role_name=args.pane_mode,
            runner=subprocess.run,
        )
    if args.command == "stop":
        return stop_project(config)
    if args.command == "teardown":
        return switchyard_teardown_command(
            config_project,
            dry_run=args.dry_run,
            confirm=args.confirm,
            drop_nonempty_board=args.drop_nonempty_board,
            destroy_registered_tenant=args.destroy_registered_tenant,
            remove_owner_home=args.remove_owner_home,
            remove_owner_user=args.remove_owner_user,
            owner_user=args.owner_user,
        )
    if args.command == "deploy-launcher":
        return deploy_launcher_checkout(
            config,
            launcher_repo=args.launcher_repo,
            clean=args.clean_launcher,
        )
    if args.command != "pane" and (args.pane_mode or args.role):
        raise SystemExit(f"{args.command} does not accept extra pane arguments")
    if args.command == "pane":
        if not args.pane_mode or not args.role:
            raise SystemExit("pane mode requires <start|attach|attach-or-start|reload|attach-role|detach-role> and <role>")
        if args.pane_mode not in {"start", "attach", "attach-or-start", "reload", "attach-role", "detach-role"}:
            raise SystemExit(f"unknown pane mode: {args.pane_mode}")
        if args.pane_mode not in {"attach", "detach-role"}:
            config = prepare_project_desktop(config)
        role = _role_by_name(config, args.role)
        # Every lifecycle path for this role runs as the role's own account, so
        # its tmux server, pane processes and board writes all carry that uid.
        # Re-execing as the shared project owner here is what previously undid
        # the per-role accounts the layout had already selected (SYRD-39).
        pane_user = role_run_as_user(config, role)
        pane_state_dir = args.pane_state_dir or default_pane_state_dir_for_user(pane_user, project=config.project)
        if pane_user and current_user_name() != pane_user:
            return subprocess.run(
                pane_command_args(
                    config.project,
                    role,
                    config_path=config_path,
                    mode=args.pane_mode,
                    script_path=args.script_path,
                    slot=args.slot,
                    pane_state_dir=pane_state_dir,
                    force_reload=args.force,
                    skip_launcher_check=args.skip_launcher_check,
                    allow_stale_launcher=args.allow_stale_launcher,
                    no_attach=args.no_attach,
                    run_as_user=pane_user,
                )
            ).returncode
        if not args.skip_launcher_check:
            ensure_launcher_checkout_current(
                config,
                runner=subprocess.run,
                auto_deploy=False,
                allow_stale=args.allow_stale_launcher,
            )
        ensure_configured_runtime_user(config)
        seed_default_session_dir_from_legacy_sources(config.session_dir)
        if args.pane_mode == "attach-role":
            if args.slot is None:
                raise SystemExit("pane attach-role requires --slot")
            return attach_role_to_slot(
                config,
                config_path=config_path,
                role_name=role.role,
                slot=args.slot,
                session_dir=role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
            )
        if args.pane_mode == "detach-role":
            return detach_role_from_slot(
                config,
                config_path=config_path,
                role_name=role.role,
            )
        if args.no_attach and not role.detached:
            return ensure_visible_role_session_for_viewer(
                role,
                mode=args.pane_mode,
                session_dir=role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=pane_user,
            )
        if role.detached:
            return run_detached_role(
                role,
                mode=args.pane_mode,
                session_dir=role_session_dir(config, role),
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=pane_user,
            )
        return run_role_pane(
            role,
            mode=args.pane_mode,
            session_dir=role_session_dir(config, role),
            pane_state_dir=pane_state_dir,
            force_reload=args.force,
            bin_user=pane_user,
        )
    return launch_project(
        config,
        config_path=config_path,
        mode=args.command,
        script_path=args.script_path,
        dry_run=args.dry_run,
        layout_output=args.layout_output,
        pane_state_dir=args.pane_state_dir,
        force_reload=args.force,
        allow_stale_launcher=args.allow_stale_launcher,
        no_launcher_self_deploy=args.no_launcher_self_deploy,
        report_session_records=True,
        layout_mode=args.layout,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
