"""JSON-driven project team launcher for tmux-backed CLI panes."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "team-launcher"
DEFAULT_SESSION_DIR = Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions")
DEFAULT_PANE_STATE_DIR = (
    Path(os.environ["PGU_TICKET_BOARD_PANE_STATE_DIR"]).expanduser()
    if os.environ.get("PGU_TICKET_BOARD_PANE_STATE_DIR")
    else Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")
)
USER_BIN_ENV = "PGU_TEAM_LAUNCHER_BIN_DIR"
GUI_USER_ENV = "PGU_TEAM_LAUNCHER_GUI_USER"
GUI_WAYLAND_ENV = "PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY"
HOST_WAYLAND_ENV = "PGU_HOST_WAYLAND_DISPLAY"
KONSOLE_QT_LOGGING_RULES = "qt.qpa.wayland.warning=false"
YOLO_ARGS_BY_CLI = {
    "agy": ["--dangerously-skip-permissions"],
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust"],
    "gemini": ["--yolo"],
}
EFFORT_STYLE_BY_CLI = {
    "agy": None,
    "claude": "flag",
    "codex": "config",
    "gemini": None,
}
KNOWN_LIVE_CLI_NAMES = {"agy", "claude", "codex", "gemini"}
DEFAULT_RESUME_MODE_BY_CLI = {
    "agy": "flag",
    "claude": "flag",
    "codex": "subcommand",
    "gemini": "flag",
}
DEFAULT_RESUME_FLAG_BY_CLI = {
    "agy": "--conversation",
    "claude": "--resume",
    "gemini": "--conversation",
}
DEFAULT_RESUME_SUBCOMMAND_BY_CLI = {
    "codex": "resume",
}
RESUME_STARTUP_TIMEOUT_SECONDS = 1.5
RESUME_STARTUP_POLL_SECONDS = 0.1
RUNTIME_READY_ATTEMPTS = 50
RUNTIME_READY_POLL_SECONDS = 0.1


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
    layout: Path
    session_dir: Path
    run_as_user: str
    repository: Path | None
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def default_user_bin() -> str:
    configured = os.environ.get(USER_BIN_ENV, "").strip()
    if configured:
        return str(Path(configured).expanduser())
    return str(Path.home() / "bin")


def default_gui_user() -> str:
    configured = os.environ.get(GUI_USER_ENV, "").strip()
    if configured:
        return configured
    return current_user_name()


def konsole_launch_args(layout_path: Path, *, gui_user: str | None = None) -> list[str]:
    wayland_display = str(os.environ.get(HOST_WAYLAND_ENV, "")).strip()
    if not wayland_display:
        user = (gui_user if gui_user is not None else default_gui_user()).strip()
        uid = uid_for_user(user) if user else None
        if uid is not None:
            wayland_display = f"/run/user/{uid}/{os.environ.get(GUI_WAYLAND_ENV, 'wayland-0')}"
    if not wayland_display:
        return [
            "sh",
            "-lc",
            "printf '%s\\n' 'team-launcher: no host Wayland display; run from Eric desktop session' >&2; exit 1",
        ]
    return [
        "env",
        "QT_QPA_PLATFORM=wayland",
        f"QT_LOGGING_RULES={KONSOLE_QT_LOGGING_RULES}",
        f"WAYLAND_DISPLAY={wayland_display}",
        "konsole",
        "--layout",
        str(layout_path),
    ]


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
        print(
            f"team-launcher: Konsole exited immediately with status {returncode}; see {log_path}",
            file=sys.stderr,
        )
        return returncode
    print(
        f"team-launcher: started {project} in background (Konsole pid {proc.pid}; log {log_path})"
    )
    return 0


def _expand_path(value: str, *, base: Path) -> Path:
    expanded = os.path.expandvars(value)
    path = Path(expanded).expanduser()
    return path if path.is_absolute() else base / path


def _load_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return parsed


def _config_path_from_launcher_args(argv: Sequence[str]) -> Path | None:
    if not argv:
        return None
    project = str(argv[0]).strip()
    if not project or project.startswith("-"):
        return None
    command = str(argv[1]).strip() if len(argv) > 1 and not str(argv[1]).startswith("-") else "start"
    if command == "bootstrap":
        return None
    for index, arg in enumerate(argv):
        raw = str(arg)
        if raw == "--config" and index + 1 < len(argv):
            return Path(str(argv[index + 1]))
        if raw.startswith("--config="):
            return Path(raw.split("=", 1)[1])
    return DEFAULT_CONFIG_DIR / f"{project}.json"


def configured_run_as_user(argv: Sequence[str]) -> str:
    path = _config_path_from_launcher_args(argv)
    if path is None:
        return ""
    try:
        config = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return ""
    return str(config.get("run_as_user") or "").strip()


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
        model_arg=str(raw.get("model_arg") or "--model").strip(),
        effort=str(raw.get("effort") or "").strip(),
        yolo=_bool_value(raw.get("yolo"), field="yolo", role=role),
        extra_args=_string_list(raw.get("extra_args"), field="extra_args", role=role),
        resume_mode=resume_mode,
        resume_flag=resume_flag,
        resume_subcommand=resume_subcommand,
        live_commands=_string_list(raw.get("live_commands"), field="live_commands", role=role),
        env={str(key): str(value) for key, value in env_raw.items()},
    )


def load_project_config(project: str, config_path: Path | None = None) -> ProjectConfig:
    path = config_path or DEFAULT_CONFIG_DIR / f"{project}.json"
    config = _load_json(path)
    config_project = str(config.get("project") or project).strip()
    if config_project != project:
        raise SystemExit(f"config project {config_project!r} does not match requested project {project!r}")
    roles_raw = config.get("roles")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise SystemExit(f"{path} must define a non-empty roles list")
    base = path.parent
    layout = _expand_path(str(config.get("layout") or f"{project}-konsole-layout.json"), base=base)
    session_dir = _expand_path(str(config.get("session_dir") or str(DEFAULT_SESSION_DIR)), base=base)
    run_as_user = str(config.get("run_as_user") or "").strip()
    repository = None
    repository_raw = config.get("repository")
    if repository_raw is not None:
        repository = _expand_path(str(repository_raw), base=base)
    worktree_base = None
    worktree_base_raw = config.get("worktree_base")
    if worktree_base_raw is not None:
        worktree_base = _expand_path(str(worktree_base_raw), base=base)
    if repository is None and worktree_base is not None:
        raise SystemExit(f"{path} must not define worktree_base without repository")
    worktree_remote = str(config.get("worktree_remote") or "origin").strip()
    worktree_branch = str(config.get("worktree_branch") or "main").strip()
    if not worktree_remote or not worktree_branch:
        raise SystemExit(f"{path} worktree_remote and worktree_branch must be non-empty")
    roles = [
        _role_from_json(
            project,
            raw,
            base=base,
            default_workdir=repository,
        )
        for raw in roles_raw
        if isinstance(raw, dict)
    ]
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
    return ProjectConfig(
        project=config_project,
        layout=layout,
        session_dir=session_dir,
        run_as_user=run_as_user,
        repository=repository,
        worktree_base=worktree_base,
        worktree_remote=worktree_remote,
        worktree_branch=worktree_branch,
        roles=roles,
    )


def _normalized_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _quote_command(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _env_prefix(env: dict[str, str]) -> list[str]:
    return [
        f"{key}={value}"
        for key, value in sorted(env.items(), key=lambda item: (item[0] != "PGU_PANE_TARGET", item[0]))
    ]


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


def session_id_for_role(role: RoleConfig, session_dir: Path) -> str:
    parsed = _session_record_for_role(role, session_dir)
    if parsed is None:
        return ""
    return str(parsed.get("session_id") or "").strip()


def _session_record_for_role(role: RoleConfig, session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / session_file_name(role.target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if str(parsed.get("target") or "") != role.target:
        return None
    return parsed


def _session_payload_model_for_role(role: RoleConfig, session_dir: Path) -> str:
    session = _session_record_for_role(role, session_dir)
    if session is None:
        return ""
    payload = session.get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("model") or "").strip()


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


def effort_args_for_role(role: RoleConfig) -> list[str]:
    if not role.effort:
        return []
    cli_name = _command_name(role.cli[0])
    style = EFFORT_STYLE_BY_CLI.get(cli_name)
    if style == "flag":
        return ["--effort", role.effort]
    if style == "config":
        return ["-c", f"reasoning_effort={role.effort}"]
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


def cli_command_for_role(role: RoleConfig, *, session_dir: Path, resume: bool = False) -> list[str]:
    session_id = session_id_for_role(role, session_dir) if resume else ""
    command = [*role.cli, *_resume_args_for_role(role, session_id)]
    if role.model:
        command.extend([role.model_arg, role.model])
    command.extend(effort_args_for_role(role))
    command.extend(yolo_args_for_role(role))
    command.extend(role.extra_args)
    env = {"PGU_PANE_TARGET": role.target, **role.env}
    env["PATH"] = _prepend_path(env.get("PATH") or os.environ.get("PATH", ""), default_user_bin())
    return ["env", *_env_prefix(env), *command]


def tmux_new_session_args(role: RoleConfig, *, session_dir: Path, resume: bool = False) -> list[str]:
    shell_command = _quote_command(cli_command_for_role(role, session_dir=session_dir, resume=resume))
    return ["tmux", "new-session", "-d", "-s", role.tmux_session, "-c", role.workdir, shell_command]


def tmux_attach_args(role: RoleConfig) -> list[str]:
    return ["tmux", "attach", "-t", role.tmux_session]


def tmux_has_session_args(role: RoleConfig) -> list[str]:
    return ["tmux", "has-session", "-t", role.tmux_session]


def tmux_kill_session_args(role: RoleConfig) -> list[str]:
    return ["tmux", "kill-session", "-t", role.tmux_session]


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


def git_fetch_worktree_ref_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "fetch", config.worktree_remote, config.worktree_branch]


def git_fetch_launcher_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "fetch", config.worktree_remote, config.worktree_branch]


def git_shared_checkout_check_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "rev-parse", "--is-inside-work-tree"]


def git_launcher_checkout_check_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "rev-parse", "--is-inside-work-tree"]


def git_launcher_ahead_behind_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return [
        "git",
        "-C",
        str(launcher_repo),
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{worktree_ref(config)}",
    ]


def git_checkout_shared_ref_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "checkout", "--detach", "--force", worktree_ref(config)]


def git_checkout_launcher_branch_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "checkout", "--force", config.worktree_branch]


def git_fast_forward_launcher_ref_args(config: ProjectConfig, launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "merge", "--ff-only", worktree_ref(config)]


def git_clean_shared_checkout_args(config: ProjectConfig) -> list[str]:
    if config.repository is None:
        raise SystemExit("project config does not define repository")
    return ["git", "-C", str(config.repository), "clean", "-fdx"]


def git_clean_launcher_checkout_args(launcher_repo: Path) -> list[str]:
    return ["git", "-C", str(launcher_repo), "clean", "-fdx"]


def _proc_failure_reason(proc: subprocess.CompletedProcess[Any], fallback: str) -> str:
    stderr = getattr(proc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip().splitlines()[-1]
    return fallback


def _parse_ahead_behind(output: str) -> tuple[int, int] | None:
    parts = output.strip().split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def launcher_checkout_status(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> tuple[int, int]:
    repo = launcher_repo or _repo_root()
    check_proc = runner(
        git_launcher_checkout_check_args(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        raise SystemExit(f"team-launcher: launcher path is not a git checkout: {repo}")
    fetch_proc = runner(git_fetch_launcher_ref_args(config, repo))
    if fetch_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: failed to fetch launcher deploy ref {worktree_ref(config)} in {repo}: "
            f"{_proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}"
        )
    count_proc = runner(
        git_launcher_ahead_behind_args(config, repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if count_proc.returncode != 0:
        raise SystemExit(
            f"team-launcher: failed to compare launcher checkout {repo} with {worktree_ref(config)}: "
            f"{_proc_failure_reason(count_proc, f'exit {count_proc.returncode}')}"
        )
    counts = _parse_ahead_behind(str(count_proc.stdout or ""))
    if counts is None:
        raise SystemExit(
            f"team-launcher: could not parse launcher ahead/behind count for {repo}: "
            f"{str(count_proc.stdout or '').strip()!r}"
        )
    return counts


def ensure_launcher_checkout_current(
    config: ProjectConfig,
    *,
    launcher_repo: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    repo = launcher_repo or _repo_root()
    ahead, behind = launcher_checkout_status(config, launcher_repo=repo, runner=runner)
    if behind:
        raise SystemExit(
            f"team-launcher: refusing to launch from stale checkout {repo}; "
            f"it is {behind} commit(s) behind {worktree_ref(config)}. "
            f"Run `scripts/team-launcher {config.project} deploy-launcher --launcher-repo {repo}` "
            "during an approved restart window, then retry."
        )
    if ahead:
        print(
            f"warning: launcher checkout {repo} is {ahead} commit(s) ahead of {worktree_ref(config)}",
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
    check_proc = runner(
        git_launcher_checkout_check_args(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        print(f"team-launcher: launcher path is not a git checkout: {repo}", file=sys.stderr)
        return int(check_proc.returncode)
    fetch_proc = runner(git_fetch_launcher_ref_args(config, repo))
    if fetch_proc.returncode != 0:
        print(
            f"team-launcher: failed to fetch {worktree_ref(config)} in {repo}: "
            f"{_proc_failure_reason(fetch_proc, f'exit {fetch_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(fetch_proc.returncode)
    checkout_proc = runner(git_checkout_launcher_branch_args(config, repo))
    if checkout_proc.returncode != 0:
        print(
            f"team-launcher: failed to checkout launcher branch {config.worktree_branch} in {repo}: "
            f"{_proc_failure_reason(checkout_proc, f'exit {checkout_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(checkout_proc.returncode)
    merge_proc = runner(git_fast_forward_launcher_ref_args(config, repo))
    if merge_proc.returncode != 0:
        print(
            f"team-launcher: failed to fast-forward launcher checkout {repo} to {worktree_ref(config)}: "
            f"{_proc_failure_reason(merge_proc, f'exit {merge_proc.returncode}')}",
            file=sys.stderr,
        )
        return int(merge_proc.returncode)
    if clean:
        clean_proc = runner(git_clean_launcher_checkout_args(repo))
        if clean_proc.returncode != 0:
            print(
                f"team-launcher: failed to clean launcher checkout {repo}: "
                f"{_proc_failure_reason(clean_proc, f'exit {clean_proc.returncode}')}",
                file=sys.stderr,
            )
            return int(clean_proc.returncode)
    ahead, behind = launcher_checkout_status(config, launcher_repo=repo, runner=runner)
    if behind:
        print(
            f"team-launcher: launcher checkout {repo} is still {behind} commit(s) behind {worktree_ref(config)}",
            file=sys.stderr,
        )
        return 1
    print(f"team-launcher: launcher checkout {repo} is current at {worktree_ref(config)}")
    return 0


def ensure_project_worktrees(
    config: ProjectConfig,
    *,
    refresh: bool,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> WorktreeProvisionResult:
    if config.repository is None:
        return WorktreeProvisionResult({})
    fetch_proc = runner(git_fetch_worktree_ref_args(config))
    if fetch_proc.returncode != 0:
        reason = _proc_failure_reason(fetch_proc, f"fetch failed with exit {fetch_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    if not refresh:
        return WorktreeProvisionResult({})
    check_proc = runner(
        git_shared_checkout_check_args(config),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check_proc.returncode != 0:
        reason = _proc_failure_reason(check_proc, f"repository check failed with exit {check_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    checkout_proc = runner(git_checkout_shared_ref_args(config))
    if checkout_proc.returncode != 0:
        reason = _proc_failure_reason(checkout_proc, f"checkout failed with exit {checkout_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    clean_proc = runner(git_clean_shared_checkout_args(config))
    if clean_proc.returncode != 0:
        reason = _proc_failure_reason(clean_proc, f"clean failed with exit {clean_proc.returncode}")
        return WorktreeProvisionResult({role.role: reason for role in config.roles})
    return WorktreeProvisionResult({})


def fetch_project_worktree_ref(
    config: ProjectConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    if config.repository is None:
        return 0
    fetch_proc = runner(git_fetch_worktree_ref_args(config))
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


def _resume_launch_verified(
    role: RoleConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> bool:
    deadline = time.monotonic() + RESUME_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if live_command_matches_role(role, runner=runner):
            return True
        if runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return False
        time.sleep(RESUME_STARTUP_POLL_SECONDS)
    return live_command_matches_role(role, runner=runner)


def _start_role_session(
    role: RoleConfig,
    *,
    session_dir: Path,
    pane_state_dir: Path,
    prefer_resume: bool,
    seed_source: str,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    session_id = session_id_for_role(role, session_dir) if prefer_resume else ""
    if prefer_resume and not session_id:
        print(
            f"team-launcher: no recorded session id for {role.role}; starting fresh session",
            file=sys.stderr,
        )
        prefer_resume = False
    start_proc = runner(tmux_new_session_args(role, session_dir=session_dir, resume=prefer_resume))
    if start_proc.returncode != 0:
        return int(start_proc.returncode)
    if not prefer_resume:
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    if _resume_launch_verified(role, runner=runner):
        seed_initial_pane_idle_state(role, pane_state_dir=pane_state_dir, source=seed_source)
        return 0
    print(
        f"team-launcher: resume failed for {role.role} using session {session_id}; falling back to fresh session",
        file=sys.stderr,
    )
    kill_proc = runner(tmux_kill_session_args(role))
    if kill_proc.returncode != 0:
        return int(kill_proc.returncode)
    fresh_proc = runner(tmux_new_session_args(role, session_dir=session_dir, resume=False))
    if fresh_proc.returncode != 0:
        return int(fresh_proc.returncode)
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
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    exists = runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return runner(tmux_attach_args(role)).returncode
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
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> int:
    exists = runner(tmux_has_session_args(role), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if mode == "attach":
        if not exists:
            print(f"tmux session {role.tmux_session} does not exist", file=sys.stderr)
            return 1
        return 0
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
                runner=runner,
            )
        return 0
    raise SystemExit(f"unknown detached role mode: {mode}")


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


def pane_command(
    project: str,
    role: RoleConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
) -> str:
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
    if pane_state_dir is not None:
        args.extend(["--pane-state-dir", str(pane_state_dir)])
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
            )
            leaf["WorkingDirectory"] = role.workdir
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


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
    pane_state_dir: Path | None = None,
    force_reload: bool = False,
) -> int:
    if mode == "start":
        mode = "attach-or-start"
    if mode not in {"attach", "attach-or-start", "reload"}:
        raise SystemExit(f"unknown launch mode: {mode}")
    effective_pane_state_dir = pane_state_dir or DEFAULT_PANE_STATE_DIR
    output_path = layout_output or Path(tempfile.gettempdir()) / f"{config.project}-team-layout.json"
    failed_roles: dict[str, str] = {}
    if not dry_run:
        ensure_launcher_checkout_current(config, runner=runner)
        ensure_configured_runtime_user(config, runner=runner)
        if mode == "attach-or-start":
            failed_roles = ensure_project_worktrees(config, refresh=True, runner=runner).failed_roles
        elif mode == "reload":
            fetch_project_worktree_ref(config, runner=runner)
            config = sync_reload_config_to_live_sessions(config, config_path=config_path, runner=runner)
    materialize_layout(
        config,
        config_path=config_path,
        mode=mode,
        script_path=script_path,
        output_path=output_path,
        pane_state_dir=pane_state_dir,
        force_reload=force_reload,
        failed_roles=failed_roles,
    )
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
                        script_path=script_path,
                        pane_state_dir=pane_state_dir,
                        force_reload=force_reload,
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
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    for role in config.roles:
        if not role.detached:
            continue
        if role.role in failed_roles:
            print(f"skipping detached role {role.role}: {failed_roles[role.role]}", file=sys.stderr)
            continue
        result = run_detached_role(
            role,
            mode=mode,
            session_dir=config.session_dir,
            pane_state_dir=effective_pane_state_dir,
            force_reload=force_reload,
            runner=runner,
        )
        if result != 0:
            return result
    return launch_konsole_window(output_path, project=config.project, runner=runner)


def write_template(project: str, output: Path) -> int:
    layout_name = f"{project}-konsole-layout.json"
    payload = {
        "project": project,
        "layout": layout_name,
        "session_dir": str(DEFAULT_SESSION_DIR),
        "roles": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output}")
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
        choices=["start", "attach", "reload", "bootstrap", "provision-runtime", "deploy-launcher", "pane"],
        help="start is idempotent attach-or-start (resumes tracked session ids when relaunching a stopped pane); reload force-restarts running CLIs with tracked resume ids",
    )
    parser.add_argument("pane_mode", nargs="?", choices=["start", "attach", "attach-or-start", "reload"])
    parser.add_argument("role", nargs="?")
    parser.add_argument("--config", type=Path, help="project launcher config JSON")
    parser.add_argument("--layout-output", type=Path, help="write generated Konsole layout here")
    parser.add_argument("--script-path", type=Path, default=Path(__file__).resolve().with_name("team-launcher"))
    parser.add_argument("--pane-state-dir", type=Path, help=f"write initial pane idle state here (default: {DEFAULT_PANE_STATE_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="print launch plan without starting Konsole")
    parser.add_argument("--template-output", type=Path, help="bootstrap output path")
    parser.add_argument("--runtime-user", help="local user whose lingering /run/user/<uid> runtime should be provisioned")
    parser.add_argument("--launcher-repo", type=Path, help="launcher checkout to update or verify (default: this script's repo)")
    parser.add_argument("--clean-launcher", action="store_true", help="run git clean -fdx after updating --launcher-repo")
    parser.add_argument("--force", action="store_true", help="allow reload to kill/relaunch even if live command validation fails")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "bootstrap":
        output = args.template_output or DEFAULT_CONFIG_DIR / f"{args.project}.json"
        return write_template(args.project, output)
    config_path = args.config or DEFAULT_CONFIG_DIR / f"{args.project}.json"
    if args.command == "provision-runtime":
        config = load_project_config(args.project, config_path) if config_path.exists() else None
        return provision_runtime_command(args.runtime_user, config)
    config = load_project_config(args.project, config_path)
    if args.command == "deploy-launcher":
        return deploy_launcher_checkout(
            config,
            launcher_repo=args.launcher_repo,
            clean=args.clean_launcher,
        )
    if args.command == "pane":
        if not args.pane_mode or not args.role:
            raise SystemExit("pane mode requires <start|attach|attach-or-start|reload> and <role>")
        ensure_launcher_checkout_current(config, runner=subprocess.run)
        ensure_configured_runtime_user(config)
        role = _role_by_name(config, args.role)
        pane_state_dir = args.pane_state_dir or DEFAULT_PANE_STATE_DIR
        if role.detached:
            return run_detached_role(
                role,
                mode=args.pane_mode,
                session_dir=config.session_dir,
                pane_state_dir=pane_state_dir,
                force_reload=args.force,
            )
        return run_role_pane(
            role,
            mode=args.pane_mode,
            session_dir=config.session_dir,
            pane_state_dir=pane_state_dir,
            force_reload=args.force,
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
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
