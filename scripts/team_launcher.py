"""JSON-driven project team launcher for tmux-backed CLI panes."""

from __future__ import annotations

import argparse
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
    ProjectBoardProvision,
    ROLE_RE,
    build_plan,
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
}
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
DEFAULT_PANE_BASE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


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
DEFAULT_VIEWER_SESSION = "viewer"
DEFAULT_VIEWER_COLUMNS = 240
DEFAULT_VIEWER_ROWS = 80
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
CLAUDE_PROJECTS_DIR_NAME = ".claude/projects"
CODEX_SESSIONS_DIR_NAME = ".codex/sessions"
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
    live_commands: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class ProjectConfig:
    project: str
    ticket_prefix: str
    layout: Path
    session_dir: Path
    board_url: str
    board_socket: str
    run_as_user: str
    pane_launcher: Path | None
    repository: Path | None
    control_repository: Path | None
    worktree_base: Path | None
    worktree_remote: str
    worktree_branch: str
    roles: list[RoleConfig]


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
    current_release: Path | None
    current_sha: str
    target_sha: str
    deploy_ref: str
    source_repo: Path
    resolve_error: str = ""

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
        return bool(self.pane_state_source or self.runtime_hook_source or self.unverified_resume_session_id)

    @property
    def runtime_hook_reported(self) -> bool:
        return bool(self.runtime_hook_source)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
    configured = _env_first(USER_BIN_ENV, LEGACY_USER_BIN_ENV)
    if configured:
        return str(Path(configured).expanduser())
    owner_home = home_dir_for_user(user_name) if user_name.strip() else None
    if owner_home is not None:
        return str(owner_home / "bin")
    return str(Path.home() / "bin")


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


def konsole_launch_args(layout_path: Path, *, gui_user: str | None = None) -> list[str]:
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
    return [
        "env",
        "QT_QPA_PLATFORM=wayland",
        f"WAYLAND_DISPLAY={wayland_display}",
        "konsole",
        "--separate",
        "--layout",
        str(layout_path),
    ]


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
        return LAYOUT_MODE_SEPARATE
    return LAYOUT_MODE_SEPARATE if _desktop_is_kde(desktop) else LAYOUT_MODE_VIEWER


def visible_roles_for_viewer(config: ProjectConfig) -> list[RoleConfig]:
    return sorted(
        (role for role in config.roles if not role.detached and role.slot is not None),
        key=lambda role: (role.slot if role.slot is not None else 0, role.role),
    )


def tmux_set_status_off_args(role: RoleConfig) -> list[str]:
    return ["tmux", "set-option", "-t", role.tmux_session, "status", "off"]


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
    viewer_session: str = DEFAULT_VIEWER_SESSION,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    if not roles:
        print("team-launcher: no visible roles for viewer layout", file=sys.stderr)
        return 1
    if runner(["tmux", "has-session", "-t", viewer_session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        kill_proc = runner(["tmux", "kill-session", "-t", viewer_session])
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
    for role in rest:
        proc = runner(tmux_viewer_split_window_args(viewer_session, role))
        if proc.returncode != 0:
            return int(proc.returncode)
    for args in (
        tmux_viewer_select_layout_args(viewer_session),
        tmux_viewer_set_status_args(viewer_session),
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
    gui_user: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    args = konsole_launch_args(layout_path, gui_user=gui_user)
    if args[:2] == ["sh", "-lc"] or runner is not subprocess.run:
        return runner(args).returncode
    try:
        with tempfile.NamedTemporaryFile(
            mode="ab",
            prefix=f"{project}-team-launcher-konsole.",
            suffix=".log",
            delete=False,
        ) as handle:
            log_path = Path(handle.name)
            _make_konsole_log_readable(log_path)
            proc = subprocess.Popen(
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


def _default_role_cli_pairs(
    implementer_roles: Sequence[str],
    *,
    include_designer: bool,
    include_audit: bool = True,
) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    if include_designer:
        pairs.append(("designer", NEW_PROJECT_ROLE_CLI_DEFAULTS["designer"]))
    pairs.append(("director", NEW_PROJECT_ROLE_CLI_DEFAULTS["director"]))
    if include_audit:
        pairs.append(("audit", NEW_PROJECT_ROLE_CLI_DEFAULTS["audit"]))
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
) -> tuple[tuple[str, str], ...]:
    defaults = _default_role_cli_pairs(
        implementer_roles,
        include_designer=include_designer,
        include_audit=include_audit,
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
    role_clis = _artifact_role_cli_pairs(
        project_raw.get("role_clis"),
        path=artifact_path,
        implementer_roles=implementer_roles,
        include_designer=include_designer_raw,
        include_audit=include_audit_raw,
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
        role_clis=role_clis,
        include_designer=include_designer_raw,
        include_audit=include_audit_raw,
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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    temp_path.replace(path)


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
        live_commands=_string_list(raw.get("live_commands"), field="live_commands", role=role),
        env={str(key): str(value) for key, value in env_raw.items()},
    )


def _role_board_env(config: ProjectConfig, role: RoleConfig, session_role_map: dict[str, str]) -> dict[str, str]:
    return {
        "TICKET_BOARD_PROJECT": config.project,
        "TICKET_BOARD_TICKET_PREFIX": config.ticket_prefix,
        "TICKET_BOARD_URL": config.board_url,
        "TICKET_BOARD_SOCKET": config.board_socket,
        "TICKET_BOARD_CALLER_ROLE": role.role,
        "TICKET_BOARD_CALLER_ROLE_MAP": json.dumps(session_role_map, sort_keys=True, separators=(",", ":")),
    }


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
        ticket_prefix=ticket_prefix,
        layout=layout,
        session_dir=session_dir,
        board_url=board_url,
        board_socket=board_socket,
        run_as_user=run_as_user,
        pane_launcher=pane_launcher,
        repository=repository,
        control_repository=control_repository,
        worktree_base=worktree_base,
        worktree_remote=worktree_remote,
        worktree_branch=worktree_branch,
        roles=roles,
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
    parts = [part for part in path_value.split(":") if part]
    parts = [part for part in parts if part != directory]
    return ":".join([directory, *parts])


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
                session_id=session_id_for_role(role, config.session_dir),
                superseded_session_id=superseded_session_id_for_role(
                    role,
                    config.session_dir,
                    changed_since_ns=fallback_changed_since_ns,
                ),
                unverified_resume_session_id=unverified_resume_session_id_for_role(
                    role,
                    config.session_dir,
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
    print_func: Callable[[str], None] = print,
) -> list[LaunchSessionRecordStatus]:
    selected_roles = tuple(roles or config.roles)
    print_func(
        f"switchyard: checking session records for {len(selected_roles)} pane(s) "
        f"(waiting up to {timeout_seconds:g}s)"
    )
    statuses = launch_session_record_statuses(
        config,
        selected_roles,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        fallback_changed_since_ns=fallback_changed_since_ns,
        pane_state_dir=pane_state_dir,
        pane_state_updated_since=pane_state_updated_since,
    )
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
    env["PATH"] = _prepend_path(env.get("PATH") or default_pane_base_path(bin_user), default_user_bin(bin_user))
    return ["env", *_env_unset_prefix(PANE_TARGET_ENV_KEYS), *_env_prefix(env), *command]


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
    if prefer_resume:
        preflight_ok, preflight_message = _resume_preflight_allows_attempt(role, session_id, session_dir=session_dir)
        if not preflight_ok:
            print(preflight_message, file=sys.stderr)
            prefer_resume = False
    if not prefer_resume:
        clear_session_record_for_role(role, session_dir)
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
        clear_unverified_resume_for_role(role, session_dir)
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    resume_status = _resume_launch_status(role, runner=runner)
    if resume_status == RESUME_LAUNCH_VERIFIED and (post_start_verifier is None or post_start_verifier()):
        clear_unverified_resume_for_role(role, session_dir)
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    if resume_status == RESUME_LAUNCH_TIMEOUT:
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
    clear_unverified_resume_for_role(role, session_dir)
    seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=f"{seed_source}.fallback")
    print(
        f"team-launcher: started fresh session for {role.role} after resume fallback",
        file=sys.stderr,
    )
    return 0


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
                run_as_user=config.run_as_user,
            )
            leaf["WorkingDirectory"] = role.workdir
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
    launcher_path = (config.pane_launcher or script_path).expanduser().resolve(strict=False)
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


def _is_generated_project_layout_template(config: ProjectConfig, *, config_path: Path) -> bool:
    config_file = config_path.expanduser().resolve(strict=False)
    layout_path = config.layout.expanduser().resolve(strict=False)
    provision_dir = config_file.parent
    return (
        provision_dir.name == "provision"
        and layout_path.parent == provision_dir
        and layout_path.name == f"{config.project}-konsole-layout.json"
    )


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
    legacy_layouts = (
        _legacy_new_project_stacked_layout_payload(role_count),
        _legacy_new_project_column_major_layout_payload(role_count),
    )
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
        if existing_layout not in legacy_layouts:
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
    if session_dir_upgrade is not None:
        raw_config = _load_json(config_path)
        raw_config["session_dir"] = str(session_dir_upgrade)
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


def _tenant_board_root_from_config(config: ProjectConfig) -> Path | None:
    if config.pane_launcher is None:
        return None
    launcher = config.pane_launcher.expanduser()
    for parent in launcher.parents:
        if parent.name == "current":
            return parent.parent
    return None


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


def git_deploy_ref_ls_remote_args(source_repo: Path, deploy_ref: str) -> list[str] | None:
    if deploy_ref.startswith("refs/") or "/" not in deploy_ref:
        return None
    remote, branch = deploy_ref.split("/", 1)
    if not remote or not branch:
        return None
    return ["git", "-C", str(source_repo), "ls-remote", remote, f"refs/heads/{branch}"]


def git_deploy_ref_rev_parse_args(source_repo: Path, deploy_ref: str) -> list[str]:
    return ["git", "-C", str(source_repo), "rev-parse", "--verify", f"{deploy_ref}^{{commit}}"]


def _resolve_deploy_ref_readonly(
    source_repo: Path,
    deploy_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[str, str]:
    checkout_runner, owner_error = _launcher_checkout_runner(source_repo, runner=runner)
    if checkout_runner is None:
        return "", owner_error or f"cannot determine owner for {source_repo}"
    ls_remote_args = git_deploy_ref_ls_remote_args(source_repo, deploy_ref)
    if ls_remote_args is not None:
        remote_proc = checkout_runner(
            ls_remote_args,
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
    rev_parse_proc = checkout_runner(
        git_deploy_ref_rev_parse_args(source_repo, deploy_ref),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if rev_parse_proc.returncode != 0:
        return "", _proc_failure_reason(rev_parse_proc, f"git rev-parse exited {rev_parse_proc.returncode}")
    return str(rev_parse_proc.stdout or "").strip(), ""


def tenant_release_status(
    config: ProjectConfig,
    *,
    source_repo: Path | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> TenantReleaseStatus | None:
    board_root = _tenant_board_root_from_config(config)
    if board_root is None:
        return None
    resolved_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    current_release, current_sha = _current_tenant_release(board_root)
    target_sha, resolve_error = _resolve_deploy_ref_readonly(resolved_source_repo, deploy_ref, runner=runner)
    return TenantReleaseStatus(
        board_root=board_root,
        current_release=current_release,
        current_sha=current_sha,
        target_sha=target_sha,
        deploy_ref=deploy_ref,
        source_repo=resolved_source_repo,
        resolve_error=resolve_error,
    )


def tenant_release_deploy_command(status: TenantReleaseStatus, project: str) -> str:
    service_script = status.source_repo / "scripts" / "ticket-board-service.sh"
    return _quote_command(
        [
            "sudo",
            "env",
            f"TICKET_BOARD_PROJECT={project}",
            f"SOURCE_REPO={status.source_repo}",
            f"BOARD_ROOT={status.board_root}",
            f"DEPLOY_REF={status.deploy_ref}",
            str(service_script),
            "deploy",
        ]
    )


def _format_release_sha(sha: str) -> str:
    return sha if sha else "(none)"


def _format_release_path(path: Path | None) -> str:
    return str(path) if path is not None else "(none)"


def report_tenant_release_upgrade(
    config: ProjectConfig,
    *,
    source_repo: Path | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> None:
    status = tenant_release_status(config, source_repo=source_repo, deploy_ref=deploy_ref, runner=runner)
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
    if status.unchanged:
        print_func(f"switchyard: {config.project} deployed board release unchanged; no release deploy needed")
    else:
        print_func("switchyard: privileged release update command for Eric:")
        print_func(f"  {tenant_release_deploy_command(status, config.project)}")
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
        if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            updated_roles.append(role)
            continue
        live_model = live_model_for_role(role, session_dir=config.session_dir, runner=runner)
        if not live_model:
            updated_roles.append(role)
            continue
        live_cli = live_cli_for_role(role, runner=runner)
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
    print_func: Callable[[str], None] = print,
) -> int:
    if mode == "start":
        mode = "attach-or-start"
    if mode not in {"attach", "attach-or-start", "reload"}:
        raise SystemExit(f"unknown launch mode: {mode}")
    worktree_runner = runner
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
    should_assign_layout_owner = layout_output is None if assign_layout_owner is None else assign_layout_owner
    pane_script_path = config.pane_launcher or script_path
    if not dry_run:
        upgrade_result = upgrade_generated_project_layout(config, config_path=config_path, runner=runner)
        if upgrade_result.changed:
            print_func(upgrade_result.message)
            config = load_project_config(config.project, config_path)
    if not dry_run:
        pane_script_path = _verify_pane_launcher_path(config, script_path=script_path, runner=worktree_runner)
    failed_roles: dict[str, str] = {}
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
        ensure_configured_runtime_user(config, runner=runner)
        ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        ensure_generated_project_pane_hooks(
            config,
            config_path=config_path,
            script_path=pane_script_path,
            pane_state_dir=effective_pane_state_dir,
            runner=runner,
        )
        seed_default_session_dir_from_legacy_sources(config.session_dir)
        if mode == "attach-or-start":
            failed_roles = ensure_project_worktrees(config, refresh=True, runner=worktree_runner).failed_roles
            if config.control_repository is not None and set(failed_roles) == {role.role for role in config.roles}:
                reason = next(iter(failed_roles.values()), "unknown error")
                print(f"team-launcher: failed to prepare control repository for {config.project}: {reason}", file=sys.stderr)
                return 1
        elif mode == "reload":
            fetch_project_worktree_ref(config, runner=worktree_runner)
            config = sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
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
                        run_as_user=config.run_as_user,
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
            plan["viewer_session"] = DEFAULT_VIEWER_SESSION
            plan["viewer_roles"] = [role.role for role in visible_roles_for_viewer(config)]
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
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
                    pane_state_dir=effective_pane_state_dir,
                    force_reload=force_reload,
                    skip_launcher_check=True,
                    allow_stale_launcher=allow_stale_launcher,
                    run_as_user=config.run_as_user,
                )
            ).returncode
        else:
            result = run_detached_role(
                role,
                mode=mode,
                session_dir=config.session_dir,
                pane_state_dir=effective_pane_state_dir,
                force_reload=force_reload,
                bin_user=config.run_as_user,
                runner=role_process_runner,
            )
        if result != 0:
            return result
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
                        pane_state_dir=effective_pane_state_dir,
                        force_reload=force_reload,
                        skip_launcher_check=True,
                        allow_stale_launcher=allow_stale_launcher,
                        no_attach=True,
                        run_as_user=config.run_as_user,
                    )
                ).returncode
            else:
                result = ensure_visible_role_session_for_viewer(
                    role,
                    mode=mode,
                    session_dir=config.session_dir,
                    pane_state_dir=effective_pane_state_dir,
                    force_reload=force_reload,
                    bin_user=config.run_as_user,
                    runner=role_process_runner,
                )
            if result != 0:
                return result
        ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        launch_result = launch_tmux_viewer_session(
            [role for role in viewer_roles if role.role not in failed_roles],
            runner=runner,
        )
    else:
        ensure_owner_state_dirs(config, pane_state_dir=effective_pane_state_dir, runner=runner)
        launch_result = launch_konsole_window(output_path, project=config.project, runner=runner)
    if launch_result != 0:
        return launch_result
    if report_session_records:
        report_launch_session_records(
            config,
            timeout_seconds=session_record_timeout,
            poll_seconds=session_record_poll,
            fallback_changed_since_ns=launch_started_ns,
            pane_state_dir=effective_pane_state_dir,
            pane_state_updated_since=launch_started_at,
            print_func=print_func,
        )
    return 0


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
    for _attempt in range(SWITCHYARD_PROMPT_MAX_ATTEMPTS):
        raw_roles = _prompt_text("Implementer roles (comma-separated)", input_func=input_func)
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
        implementer_roles=tuple(implementer_roles or DEFAULT_PROJECT_IMPLEMENTER_ROLES),
        role_clis=_default_role_cli_pairs(
            implementer_roles or DEFAULT_PROJECT_IMPLEMENTER_ROLES,
            include_designer=True,
            include_audit=True,
        ),
        include_designer=True,
        include_audit=True,
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


def _new_project_layout_payload(role_count: int) -> dict[str, Any]:
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
    column_count = math.ceil(math.sqrt(role_count))
    rows: list[dict[str, Any]] = []
    for start in range(0, role_count, column_count):
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
    if len(rows) == 1:
        return rows[0]
    return {
        "Orientation": "Vertical",
        "Widgets": rows,
    }


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
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
) -> dict[str, Any]:
    layout_name = f"{plan.project}-konsole-layout.json"
    role_defs = _dedupe_role_defs(
        role_clis
        or _default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
        )
    )
    worktree_base = _new_project_worktree_base(plan.project, plan.owner_user)
    control_repository = _new_project_control_repository(plan.project, plan.owner_user)
    roles = [
        {
            "cli": [cli],
            **(
                {
                    "env": _new_project_role_env(
                        role,
                        plan,
                        project_name=project_name,
                        artifact_path=artifact_path,
                        design_document=design_document,
                        director_onboarding=director_onboarding,
                    )
                }
            ),
            "live_commands": [cli],
            "role": role,
            "slot": index,
            "target": f"{plan.project}-{role}:0.0",
            "tmux_session": f"{plan.project}-{role}",
            "workdir": str(worktree_base / role),
            "yolo": True,
        }
        for index, (role, cli) in enumerate(role_defs)
    ]
    return {
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
        "pane_launcher": str(Path(plan.board_current) / "scripts" / TEAM_LAUNCHER_NAME),
        "roles": roles,
    }


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
    env: dict[str, str] = {}
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
    return env


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
    remote: str = "origin",
    default_branch: str = "main",
    worktree_policy: str = "shared",
) -> Path:
    role_defs = _dedupe_role_defs(
        role_clis
        or _default_role_cli_pairs(
            implementer_roles,
            include_designer=include_designer,
            include_audit=include_audit,
        )
    )
    role_count = len(role_defs)
    config_path = output_dir / f"{plan.project}.json"
    layout_path = output_dir / f"{plan.project}-konsole-layout.json"
    layout_path.write_text(
        json.dumps(_new_project_layout_payload(role_count), indent=2, sort_keys=True) + "\n",
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
                remote=remote,
                default_branch=default_branch,
                worktree_policy=worktree_policy,
            ),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
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


def precheck_new_project(
    plan: ProjectBoardProvision,
    *,
    source_repo: Path,
    repository: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    port_in_use: Callable[[int], bool] = _tcp_port_in_use,
    socket_exists: Callable[[Path], bool] = _path_exists,
    require_owner_user: bool = True,
    require_repository: bool = True,
) -> None:
    errors: list[str] = []
    if require_owner_user and uid_for_user(plan.owner_user) is None:
        errors.append(f"target user {plan.owner_user!r} does not exist")
    if require_repository and not _path_exists(repository):
        errors.append(f"project repository {repository} does not exist")
    if not _path_exists(source_repo):
        errors.append(f"deploy checkout {source_repo} does not exist")
    else:
        status = _git_status_porcelain(source_repo, runner=runner)
        if status.strip():
            errors.append(f"deploy checkout {source_repo} has uncommitted changes")
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
            errors.append(
                f"project {plan.project!r} is already provisioned "
                f"(database {plan.database} has {table_count} ticket_board tables, {plan.board_unit} is installed).\n"
                f"  to launch it:      switchyard {plan.project}\n"
                "  to start over:     tear it down first (no teardown command exists yet)"
            )
    if errors:
        raise SystemExit("team-launcher: new project precheck failed:\n- " + "\n- ".join(errors))


def new_project_command(
    project: str,
    *,
    from_artifact: Path | None = None,
    owner_user: str | None = None,
    port: int | None = None,
    database: str | None = None,
    source_repo: Path | None = None,
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
        board_service_traversal = True
    if worktree_policy not in WORKTREE_POLICIES:
        raise SystemExit(f"team-launcher: worktree policy must be one of {sorted(WORKTREE_POLICIES)}")
    plan = build_plan(
        project=project,
        owner_user=effective_owner,
        port=port,
        database=database,
        source_repo=effective_source_repo,
        ticket_prefix=ticket_prefix,
        implementer_roles=implementer_roles,
        board_service_traversal=board_service_traversal,
        include_designer=include_designer,
        include_audit=include_audit,
    )
    precheck_new_project(
        plan,
        source_repo=effective_source_repo,
        repository=effective_repository,
        runner=runner,
        port_in_use=port_in_use,
        socket_exists=socket_exists,
        require_owner_user=execute if require_owner_user is None else require_owner_user,
    )
    artifact_dir = (output_dir or _new_project_artifact_dir(plan.project)).expanduser().resolve(strict=False)
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
        remote=remote,
        default_branch=default_branch,
        worktree_policy=worktree_policy,
    )
    commands_path = artifact_dir / "operator-commands.sh"
    if not execute:
        print(f"team-launcher: dry-run for {plan.project}; artifacts in {artifact_dir}")
        print(f"team-launcher: launcher config {config_path}")
        print("team-launcher: execution plan:")
        print(f"  cd {shlex.quote(str(artifact_dir))}")
        print("  sudo -v")
        print(f"  bash {shlex.quote(str(commands_path.name))}")
        print()
        print(commands_path.read_text(encoding="utf-8"), end="")
        return 0
    print(f"team-launcher: provisioning {plan.project}; artifacts in {artifact_dir}")
    sudo_result = runner(["sudo", "-v"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if sudo_result.returncode != 0:
        stderr = str(getattr(sudo_result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise SystemExit(f"team-launcher: sudo authentication failed{detail}")
    result = runner(["bash", str(commands_path)], cwd=str(artifact_dir))
    if result.returncode != 0:
        raise SystemExit(f"team-launcher: provisioning failed with exit status {result.returncode}")
    print(f"team-launcher: provisioned {plan.project}; launcher config {config_path}")
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
    error: str = ""

    @property
    def panes_display(self) -> str:
        if self.panes_up is None or self.panes_total is None:
            return "?/?"
        return f"{self.panes_up}/{self.panes_total}"


@dataclass(frozen=True)
class OwnerUserProvisionResult:
    created: bool
    linger_enabled: bool


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
class FirstRunAuthReport:
    unauthenticated_roles: dict[str, list[str]]
    untrusted_roles: list[tuple[str, str, str]]
    stale_codex_hook_trust: list[CodexHookTrustMismatch] = field(default_factory=list)
    missing_cli_roles: dict[str, list[str]] = field(default_factory=dict)
    model_validation_failures: list[ModelValidationFailure] = field(default_factory=list)
    owner_user: str = ""

    @property
    def has_warnings(self) -> bool:
        return bool(
            self.unauthenticated_roles
            or self.untrusted_roles
            or self.stale_codex_hook_trust
            or self.missing_cli_roles
            or self.model_validation_failures
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
FIRST_RUN_TRUST_CLIS = frozenset({"agy", "claude"})
MODEL_VALIDATION_PROMPT = "Reply with exactly: model-ok"


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
) -> None:
    existing = _existing_owner_user(owner_user)
    if existing is None:
        return
    detail = f"{existing.name} (uid {existing.uid}, home {existing.home})"
    print_func(f"warning: switchyard: owner user {detail} already exists")
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
) -> None:
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
        role_clis=_dedupe_role_cli_pairs(
            role_clis
            or _default_role_cli_pairs(
                implementer_roles,
                include_designer=include_designer,
                include_audit=include_audit,
            )
        ),
        include_designer=include_designer,
        include_audit=include_audit,
        push_policy="director-main-only",
        gates=dict(PROJECT_DESIGN_DEFAULT_GATES),
        capability_grants={
            **PROJECT_DESIGN_DEFAULT_CAPABILITY_GRANTS,
            "shell": owner_shell,
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
    args = _owner_command_args(owner_user, command)
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
    cwd: Path,
    command: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> subprocess.CompletedProcess[Any]:
    args = _owner_command_args(owner_user, command)
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
        command=["sh", "-lc", f"command -v {shlex.quote(binary)}"],
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
    effective_owner = (owner_user or config.run_as_user or current_user_name()).strip()
    if not effective_owner:
        return FirstRunAuthReport({}, [])
    effective_home = owner_home or _owner_home_for_auth(effective_owner)
    roles_by_cli = _roles_by_first_cli(config)
    unauthenticated: dict[str, list[str]] = {}
    missing_cli_roles: dict[str, list[str]] = {}

    for cli, roles in roles_by_cli.items():
        if cli not in FIRST_RUN_AUTH_STATUS_COMMANDS:
            continue
        auth_status = _cli_auth_status(cli, owner_user=effective_owner, owner_home=effective_home, runner=runner)
        if auth_status == "authenticated":
            continue
        if auth_status == "not_installed":
            names = ", ".join(_role_names(roles))
            print_func(
                f"switchyard: first-run {cli} not installed for owner user {effective_owner}; "
                f"install {cli} for owner user {effective_owner}; affected roles: {names}"
            )
            missing_cli_roles[cli] = _role_names(roles)
            continue
        login_command = FIRST_RUN_AUTH_LOGIN_COMMANDS[cli]
        names = ", ".join(_role_names(roles))
        print_func(
            f"switchyard: first-run {cli} login required for {names}; "
            f"running {shlex.join(login_command)} as {effective_owner}"
        )
        _run_owner_cli_interactive(
            owner_user=effective_owner,
            cwd=effective_home,
            command=login_command,
            runner=runner,
        )
        auth_status = _cli_auth_status(cli, owner_user=effective_owner, owner_home=effective_home, runner=runner)
        if auth_status == "not_installed":
            missing_cli_roles[cli] = _role_names(roles)
        elif auth_status != "authenticated":
            unauthenticated[cli] = _role_names(roles)

    untrusted: list[tuple[str, str, str]] = []
    for role in config.roles:
        if not role.detached:
            continue
        cli = _role_cli_name(role)
        if cli not in FIRST_RUN_TRUST_CLIS:
            continue
        workdir = Path(role.workdir)
        if _workdir_is_trusted(cli, owner_home=effective_home, workdir=workdir):
            continue
        command = _first_run_trust_command(role)
        print_func(
            f"switchyard: first-run folder trust required for {cli} role {role.role} "
            f"at {workdir}; this is not account login. Accept the folder trust prompt, then exit the CLI"
        )
        _run_owner_cli_interactive(
            owner_user=effective_owner,
            cwd=workdir,
            command=command,
            runner=runner,
        )
        if not _workdir_is_trusted(cli, owner_home=effective_home, workdir=workdir):
            untrusted.append((cli, role.role, str(workdir)))

    stale_codex_hook_trust = stale_codex_hook_trust_for_roles(config.roles, owner_home=effective_home)
    if stale_codex_hook_trust:
        print_func(f"switchyard: first-run {_format_codex_hook_trust_report(stale_codex_hook_trust, owner_user=effective_owner)}")

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
        stale_codex_hook_trust,
        missing_cli_roles,
        model_validation_failures,
        effective_owner if missing_cli_roles or stale_codex_hook_trust else "",
    )


def report_first_run_auth_warnings(
    report: FirstRunAuthReport,
    *,
    print_func: Callable[[str], None] = print,
) -> None:
    owner_detail = f" for owner user {report.owner_user}" if report.owner_user else ""
    for cli, roles in report.missing_cli_roles.items():
        print_func(
            f"warning: switchyard: {cli} is not installed{owner_detail}; "
            f"install {cli}{owner_detail}; affected roles: {', '.join(roles)}"
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


def _owner_project_install_args(owner_user: str, project_dir: Path, *, shell: str = "fish") -> list[list[str]]:
    shell_path = shell if "/" in shell else (shutil.which(shell) or f"/usr/bin/{shell}")
    return [
        ["id", "-u", owner_user],
        ["useradd", "-m", "-s", shell_path, owner_user],
        ["install", "-d", "-m", "0755", "-o", owner_user, "-g", owner_user, str(project_dir)],
    ]


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
    id_args, useradd_args, install_args = _owner_project_install_args(owner_user, project_dir, shell=shell)
    id_result = runner(id_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    created_user = False
    linger_enabled = False
    if id_result.returncode != 0:
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
    return OwnerUserProvisionResult(created=created_user, linger_enabled=linger_enabled)


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
        "config_path": str(status.config_path),
        "error": status.error,
    }


def switchyard_status_command(
    *,
    config_dir: Path | None = None,
    registry_dir: Path | None = None,
    json_output: bool = False,
    process_commands: Sequence[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    statuses = switchyard_project_statuses(
        config_dir=config_dir,
        registry_dir=registry_dir,
        process_commands=process_commands,
        runner=runner,
    )
    if json_output:
        print_func(json.dumps({"projects": [_switchyard_project_status_payload(status) for status in statuses]}, indent=2))
        return 0
    rows = [("NAME", "SLUG", "STATE", "PANES")]
    rows.extend((status.name, status.slug, status.state, status.panes_display) for status in statuses)
    widths = [max(len(str(row[index])) for row in rows) for index in range(4)]
    for row in rows:
        print_func(
            f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]:<{widths[2]}}  {row[3]}"
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
    output_dir: Path | None = None,
    port: int | None = None,
    database: str | None = None,
    role_clis: Sequence[tuple[str, str]] | None = None,
    yes: bool = False,
    allow_existing_owner_user: bool = False,
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
            role_clis if role_clis is not None else _prompt_switchyard_role_choices(input_func=input_func)
        )
        _require_new_project_roles(selected_role_clis)
        selected_implementer_roles = tuple(
            role for role, _cli in selected_role_clis if role not in NEW_PROJECT_RESERVED_ROLE_NAMES
        )
        if not selected_implementer_roles:
            raise SystemExit("switchyard: at least one implementer role is required")
        include_designer = any(role == "designer" for role, _cli in selected_role_clis)
        include_audit = any(role == "audit" for role, _cli in selected_role_clis)

    artifact_path = (from_artifact or (_switchyard_dir(project_dir) / f"{resolved_slug}.project.json")).expanduser().resolve(strict=False)
    design_document = project_dir / SWITCHYARD_DESIGN_FILE_NAME
    director_onboarding = _switchyard_dir(project_dir) / SWITCHYARD_DIRECTOR_ONBOARDING_FILE_NAME
    _precheck_project_path_before_mutating(owner_user, project_dir)
    effective_source_repo = (source_repo or _repo_root()).expanduser().resolve(strict=False)
    precheck_artifact = load_project_design_artifact(from_artifact, expected_project=resolved_slug) if from_artifact else None
    worktree_branch = precheck_artifact.default_branch if precheck_artifact else "main"
    precheck_plan = build_plan(
        project=resolved_slug,
        owner_user=owner_user,
        port=port,
        database=database,
        source_repo=effective_source_repo,
        ticket_prefix=precheck_artifact.ticket_prefix if precheck_artifact else None,
        implementer_roles=precheck_artifact.implementer_roles if precheck_artifact else selected_implementer_roles,
        include_designer=precheck_artifact.include_designer if precheck_artifact else include_designer,
        include_audit=precheck_artifact.include_audit if precheck_artifact else include_audit,
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
        require_owner_user=False,
        require_repository=False,
    )
    owner_result = _ensure_owner_user_and_project_dir(
        owner_user,
        project_dir,
        runner=runner,
        shell=owner_shell,
    )
    if owner_result.created:
        print_func(f"switchyard: created user {owner_user} with shell {owner_shell}; linger enabled")
    else:
        print_func(f"switchyard: using existing user {owner_user} (not modifying)")
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
        )
        _chown_switchyard_project_files(owner_user=owner_user, project_dir=project_dir, runner=runner)
        if include_designer and design_document.exists():
            _chown_project_file(owner_user=owner_user, path=design_document, runner=runner)
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
    result = new_project_command(
        resolved_slug,
        from_artifact=artifact_path,
        source_repo=source_repo,
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
    if first_run_auth_report.model_validation_failures:
        report_first_run_auth_warnings(first_run_auth_report, print_func=print_func)
        return 1
    launch_runner = _owner_project_git_runner(
        owner_user=owner_user,
        project_dir=project_dir,
        owned_roots=_control_repository_owned_roots(config),
        runner=runner,
    )
    launch_started_at = time.time()
    launch_started_ns = time.time_ns()
    launch_result = launch_project(
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


def upgrade_project_command(
    config: ProjectConfig,
    *,
    config_path: Path,
    dry_run: bool = False,
    source_repo: Path | None = None,
    deploy_ref: str = DEFAULT_TENANT_RELEASE_DEPLOY_REF,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    result = upgrade_generated_project_layout(config, config_path=config_path, dry_run=dry_run, runner=runner)
    print_func(result.message)
    if result.changed:
        config = load_project_config(config.project, config_path)
    report_tenant_release_upgrade(
        config,
        source_repo=source_repo,
        deploy_ref=deploy_ref,
        runner=runner,
        print_func=print_func,
    )
    return 0


def stop_project(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    print_func: Callable[[str], None] = print,
) -> int:
    role_runner = runner
    if config.run_as_user and current_user_name() != config.run_as_user:
        role_runner = _owner_process_runner(owner_user=config.run_as_user, runner=runner)
    exit_code = 0
    for role in config.roles:
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
        choices=["start", "attach", "reload", "stop", "design", "new", "provision-runtime", "deploy-launcher", "upgrade", "pane"],
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
    parser.add_argument("--owner-user", help="new project owner Unix user (default: <project>-agent)")
    parser.add_argument("--port", type=int, help="new project board port; omitted means deterministic allocation")
    parser.add_argument("--database", help="new project PostgreSQL database; omitted means <project>_ticket_board")
    parser.add_argument("--source-repo", type=Path, help="source checkout to deploy for new project provisioning")
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
    parser.add_argument("--clean-launcher", action="store_true", help="run git clean -fdx after updating --launcher-repo")
    parser.add_argument("--force", action="store_true", help="allow reload to kill/relaunch even if live command validation fails")
    parser.add_argument("--allow-stale-launcher", action="store_true", help="emergency override: warn but proceed when the launcher checkout is provably stale")
    parser.add_argument("--no-launcher-self-deploy", action="store_true", help="restore refuse-only behavior instead of automatically fast-forwarding a stale launcher checkout")
    parser.add_argument("--skip-launcher-check", action="store_true", help=argparse.SUPPRESS)
    return parser


def _build_switchyard_new_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard new", description="Create a Switchyard project.")
    parser.add_argument("--slug", help="project slug; prompted when omitted")
    parser.add_argument("--agent-name", help="owner user; prompted default is <slug>-agent when omitted")
    parser.add_argument("--project-name", help="human project name; prompted when omitted")
    parser.add_argument("--project-path", type=Path, help="project working directory; prompted when omitted")
    parser.add_argument("--from", dest="from_artifact", type=Path, help="project artifact emitted by the design session")
    parser.add_argument("--source-repo", type=Path, help="switchyard source checkout to deploy")
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
        "--layout",
        choices=sorted(LAYOUT_MODE_CHOICES),
        default=LAYOUT_MODE_AUTO,
        help="window layout mode: auto detects the invoking desktop, separate keeps the KDE/Konsole path, viewer forces the tmux viewer",
    )
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
    parser.add_argument("--source-repo", type=Path, help="source checkout to export into the tenant board release")
    return parser


def _build_switchyard_stop_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard stop", description="Stop a Switchyard project's tmux pane sessions.")
    parser.add_argument("project", nargs="+", help="project name or slug")
    return parser


def _build_switchyard_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="switchyard status", description="List registered Switchyard projects and pane liveness.")
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable project status")
    return parser


def switchyard_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return switchyard_menu_command()
    if argv[0].casefold() == "new":
        args = _build_switchyard_new_parser().parse_args(argv[1:])
        return switchyard_new_command(
            slug=args.slug,
            agent_name=args.agent_name,
            project_name=args.project_name,
            project_path=args.project_path,
            from_artifact=args.from_artifact,
            source_repo=args.source_repo,
            output_dir=args.output_dir,
            port=args.port,
            database=args.database,
            yes=args.yes,
            allow_existing_owner_user=args.allow_existing_owner_user,
            layout_mode=args.layout,
            git_init=not args.no_git_init,
        )
    if argv[0].casefold() == "register":
        args = _build_switchyard_register_parser().parse_args(argv[1:])
        return switchyard_register_command(args.config_path)
    if argv[0].casefold() == "upgrade":
        args = _build_switchyard_upgrade_parser().parse_args(argv[1:])
        entry = _resolve_switchyard_project(args.project)
        config = load_project_config(entry.slug, entry.config_path)
        return upgrade_project_command(
            config,
            config_path=entry.config_path,
            dry_run=args.dry_run,
            source_repo=args.source_repo,
            deploy_ref=args.deploy_ref,
        )
    if argv[0].casefold() == "stop":
        args = _build_switchyard_stop_parser().parse_args(argv[1:])
        project = " ".join(args.project)
        entry = _resolve_switchyard_project(project)
        config = load_project_config(entry.slug, entry.config_path)
        return stop_project(config)
    if argv[0].casefold() == "status":
        args = _build_switchyard_status_parser().parse_args(argv[1:])
        return switchyard_status_command(json_output=args.json)
    if argv[0].casefold() == "validate-models":
        if len(argv) < 2:
            raise SystemExit("switchyard validate-models requires <project>")
        project = " ".join(argv[1:])
        return switchyard_validate_models_command(project)
    selection = " ".join(argv)
    entry = _resolve_switchyard_project(selection)
    config = load_project_config(entry.slug, entry.config_path)
    # Model validation intentionally runs only for `switchyard new` and the
    # explicit validate-models command. It performs provider API calls, so a
    # routine team start should not depend on provider availability.
    first_run_auth_report = run_switchyard_launch_first_run_auth(config)
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
            port=args.port,
            database=args.database,
            source_repo=args.source_repo,
            repository=args.repository,
            output_dir=args.new_output_dir,
            execute=args.execute,
            dry_run=args.dry_run,
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
            deploy_ref=args.deploy_ref,
        )
    if args.command == "stop":
        return stop_project(config)
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
        role = _role_by_name(config, args.role)
        pane_state_dir = args.pane_state_dir or default_pane_state_dir_for_user(config.run_as_user, project=config.project)
        if config.run_as_user and current_user_name() != config.run_as_user:
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
                    run_as_user=config.run_as_user,
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
                session_dir=config.session_dir,
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
                session_dir=config.session_dir,
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=config.run_as_user,
            )
        if role.detached:
            return run_detached_role(
                role,
                mode=args.pane_mode,
                session_dir=config.session_dir,
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
                bin_user=config.run_as_user,
            )
        return run_role_pane(
            role,
            mode=args.pane_mode,
            session_dir=config.session_dir,
            pane_state_dir=pane_state_dir,
            force_reload=args.force,
            bin_user=config.run_as_user,
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
