"""JSON-driven project team launcher for tmux-backed CLI panes."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "team-launcher"
DEFAULT_SESSION_DIR = Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions")


@dataclass(frozen=True)
class RoleConfig:
    role: str
    slot: int | None
    tmux_session: str
    target: str
    workdir: str
    cli: list[str]
    model: str
    model_arg: str
    resume_flag: str
    env: dict[str, str]


@dataclass(frozen=True)
class ProjectConfig:
    project: str
    layout: Path
    session_dir: Path
    roles: list[RoleConfig]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _expand_path(value: str, *, base: Path) -> Path:
    expanded = os.path.expandvars(value)
    path = Path(expanded).expanduser()
    return path if path.is_absolute() else base / path


def _load_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return parsed


def _role_from_json(project: str, raw: dict[str, Any]) -> RoleConfig:
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
    return RoleConfig(
        role=role,
        slot=slot,
        tmux_session=tmux_session,
        target=str(raw.get("target") or f"{tmux_session}:0.0").strip(),
        workdir=str(raw.get("workdir") or os.getcwd()),
        cli=cli,
        model=str(raw.get("model") or "").strip(),
        model_arg=str(raw.get("model_arg") or "--model").strip(),
        resume_flag=str(raw.get("resume_flag") or "--resume").strip(),
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
    roles = [_role_from_json(project, raw) for raw in roles_raw if isinstance(raw, dict)]
    if len(roles) != len(roles_raw):
        raise SystemExit(f"{path} roles must all be JSON objects")
    slots = [role.slot for role in roles if role.slot is not None]
    if len(set(slots)) != len(slots):
        raise SystemExit(f"{path} contains duplicate role slot assignments")
    return ProjectConfig(project=config_project, layout=layout, session_dir=session_dir, roles=roles)


def _quote_command(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _env_prefix(env: dict[str, str]) -> list[str]:
    return [f"{key}={value}" for key, value in sorted(env.items())]


def session_file_name(target: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    return f"{safe}.json"


def session_id_for_role(role: RoleConfig, session_dir: Path) -> str:
    path = session_dir / session_file_name(role.target)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    if str(parsed.get("target") or "") != role.target:
        return ""
    return str(parsed.get("session_id") or "").strip()


def cli_command_for_role(role: RoleConfig, *, session_dir: Path, resume: bool = False) -> list[str]:
    command = list(role.cli)
    if role.model:
        command.extend([role.model_arg, role.model])
    if resume:
        session_id = session_id_for_role(role, session_dir)
        if session_id:
            command.extend([role.resume_flag, session_id])
    env = {"PGU_PANE_TARGET": role.target, **role.env}
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


def run_role_pane(
    role: RoleConfig,
    *,
    mode: str,
    session_dir: Path,
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
            kill_proc = runner(tmux_kill_session_args(role))
            if kill_proc.returncode != 0:
                return int(kill_proc.returncode)
        start_proc = runner(tmux_new_session_args(role, session_dir=session_dir, resume=True))
        if start_proc.returncode != 0:
            return int(start_proc.returncode)
        return runner(tmux_attach_args(role)).returncode
    if mode in {"start", "attach-or-start"}:
        if not exists:
            start_proc = runner(tmux_new_session_args(role, session_dir=session_dir, resume=False))
            if start_proc.returncode != 0:
                return int(start_proc.returncode)
        return runner(tmux_attach_args(role)).returncode
    raise SystemExit(f"unknown pane mode: {mode}")


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


def pane_command(project: str, role: RoleConfig, *, config_path: Path, mode: str, script_path: Path) -> str:
    args = [
        str(script_path),
        project,
        "pane",
        mode,
        role.role,
        "--config",
        str(config_path),
    ]
    return _quote_command(args)


def materialize_layout(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    output_path: Path,
) -> Path:
    layout = json.loads(config.layout.read_text(encoding="utf-8"))
    leaves = _layout_leaves(layout)
    for role in config.roles:
        if role.slot is None:
            continue
        if role.slot < 0 or role.slot >= len(leaves):
            raise SystemExit(f"role {role.role} slot {role.slot} is outside layout leaf count {len(leaves)}")
        leaf = leaves[role.slot]
        leaf["Command"] = pane_command(config.project, role, config_path=config_path, mode=mode, script_path=script_path)
        leaf["WorkingDirectory"] = role.workdir
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(layout, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def launch_project(
    config: ProjectConfig,
    *,
    config_path: Path,
    mode: str,
    script_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    dry_run: bool = False,
    layout_output: Path | None = None,
) -> int:
    if mode == "start":
        mode = "attach-or-start"
    if mode not in {"attach", "attach-or-start", "reload"}:
        raise SystemExit(f"unknown launch mode: {mode}")
    output_path = layout_output or Path(tempfile.gettempdir()) / f"{config.project}-team-layout.json"
    materialize_layout(config, config_path=config_path, mode=mode, script_path=script_path, output_path=output_path)
    plan = {
        "project": config.project,
        "mode": mode,
        "layout": str(output_path),
        "roles": [
            {
                "role": role.role,
                "slot": role.slot,
                "tmux_session": role.tmux_session,
                "target": role.target,
                "command": pane_command(config.project, role, config_path=config_path, mode=mode, script_path=script_path),
            }
            for role in config.roles
        ],
    }
    if dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    return runner(["konsole", "--layout", str(output_path)]).returncode


def write_template(project: str, output: Path) -> int:
    layout_name = f"{project}-konsole-layout.json"
    payload = {
        "project": project,
        "layout": layout_name,
        "session_dir": str(DEFAULT_SESSION_DIR),
        "roles": [
            {
                "role": "ops",
                "slot": 0,
                "tmux_session": f"{project}-ops",
                "target": f"{project}-ops:0.0",
                "workdir": str(Path.home() / "Projects" / project),
                "cli": ["codex"],
                "model": "gpt-5",
                "model_arg": "--model",
                "resume_flag": "--resume",
            }
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output}")
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
        choices=["start", "attach", "reload", "bootstrap", "pane"],
        help="start is idempotent attach-or-start; reload restarts CLIs with tracked resume ids",
    )
    parser.add_argument("pane_mode", nargs="?", choices=["start", "attach", "attach-or-start", "reload"])
    parser.add_argument("role", nargs="?")
    parser.add_argument("--config", type=Path, help="project launcher config JSON")
    parser.add_argument("--layout-output", type=Path, help="write generated Konsole layout here")
    parser.add_argument("--script-path", type=Path, default=Path(__file__).resolve().with_name("team-launcher"))
    parser.add_argument("--dry-run", action="store_true", help="print launch plan without starting Konsole")
    parser.add_argument("--template-output", type=Path, help="bootstrap output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "bootstrap":
        output = args.template_output or DEFAULT_CONFIG_DIR / f"{args.project}.json"
        return write_template(args.project, output)
    config_path = args.config or DEFAULT_CONFIG_DIR / f"{args.project}.json"
    config = load_project_config(args.project, config_path)
    if args.command == "pane":
        if not args.pane_mode or not args.role:
            raise SystemExit("pane mode requires <start|attach|attach-or-start|reload> and <role>")
        role = _role_by_name(config, args.role)
        return run_role_pane(role, mode=args.pane_mode, session_dir=config.session_dir)
    return launch_project(
        config,
        config_path=config_path,
        mode=args.command,
        script_path=args.script_path,
        dry_run=args.dry_run,
        layout_output=args.layout_output,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
