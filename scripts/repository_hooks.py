#!/usr/bin/env python3
"""Install Switchyard's Git policy hooks without discarding existing hooks."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable


DEFAULT_LINE_LIMIT = 1250
DEFAULT_WORKTREE_TOTAL_LIMIT = 300
DEFAULT_WORKTREE_RECLAIMABLE_LIMIT = 100
MANAGED_MARKER = "# switchyard-managed-repository-hook-v1"
DEFAULT_REGISTRY_DIR = Path("/etc/switchyard/projects")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull}
    repository_path = repository.expanduser().resolve(strict=False)
    return subprocess.run(
        ["git", "-c", f"safe.directory={repository_path}", "-C", str(repository_path), *args],
        check=check,
        capture_output=True,
        text=True,
        env=environment,
    )


def _hooks_path(repository: Path) -> Path:
    """Return and pin the repository's hook path independently of user config."""
    configured = _git(repository, "config", "--local", "--get", "core.hooksPath", check=False)
    if configured.stdout.strip():
        path = Path(_git(repository, "rev-parse", "--git-path", "hooks").stdout.strip())
        return (path if path.is_absolute() else repository / path).resolve(strict=False)

    common = Path(_git(repository, "rev-parse", "--git-common-dir").stdout.strip())
    if not common.is_absolute():
        common = repository / common
    path = (common / "hooks").resolve(strict=False)

    # Root performs the host-wide rollout. Restore the config's repository
    # ownership after git's lock-and-rename update so tenant Git remains writable.
    config = (common / "config").resolve(strict=False)
    config_owner = _owner(config)
    _git(repository, "config", "--local", "core.hooksPath", str(path))
    os.chown(config, *config_owner)
    return path


def _owner(path: Path) -> tuple[int, int]:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    info = current.stat()
    return info.st_uid, info.st_gid


def _atomic_install_text(path: Path, text: str, *, owner: tuple[int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o755)
        os.chown(temporary, *owner)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _install_file(source: Path, destination: Path, *, owner: tuple[int, int]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o755)
        os.chown(temporary, *owner)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _preserve_existing_hook(hook: Path, *, replace_warning_only: bool = False) -> Path:
    upstream = hook.with_name(f"{hook.name}.switchyard-upstream")
    content = hook.read_text(encoding="utf-8", errors="replace") if hook.exists() else ""
    if replace_warning_only and "warn-file-size-limit.py" in content and "exit 0" in content:
        hook.unlink()
    elif hook.exists() and MANAGED_MARKER not in content:
        if upstream.exists():
            raise RuntimeError(f"refusing to overwrite preserved hook {upstream}")
        os.replace(hook, upstream)
    return upstream


#: The helpers the pre-commit hook runs, by the name they have beside it and
#: the release script each is copied from.
PRE_COMMIT_HELPERS = (
    ("warn-file-size-limit.py", "warn_file_size_limit.py"),
    ("report_file_size_limit.py", "report_file_size_limit.py"),
    ("warn-worktree-count.py", "warn_worktree_count.py"),
)

#: The hook a Switchyard Git template carries, as committed in the release.
#: Root stages it with the helpers into each tenant's role tooling, and role
#: panes point GIT_TEMPLATE_DIR at that, so a repository a role clones or
#: creates has the policy before its first commit (SYRD-257).
TEMPLATE_PRE_COMMIT = "git_template_pre_commit"

#: Where the hook finds its helpers and its preserved upstream hook, when
#: those were copied beside it -- which a template's copy always is.
_OWN_DIRECTORY = '"$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"'


def render_pre_commit(*, hook_dir: str, line_limit: int = DEFAULT_LINE_LIMIT) -> str:
    """The managed pre-commit hook, for helpers in `hook_dir` (a bash word).

    One body for both forms, so the installed hook and the template's cannot
    drift: they differ only in whether the directory is written in or found.
    The upstream hook runs first and its status is the hook's; the warnings
    never block.
    """
    return f"""#!/usr/bin/env bash
set -u
{MANAGED_MARKER}

readonly HOOK_DIR={hook_dir}
readonly FILE_SIZE_LIMIT={line_limit}
readonly FILE_SIZE_HELPER="$HOOK_DIR/warn-file-size-limit.py"
readonly WORKTREE_TOTAL_LIMIT={DEFAULT_WORKTREE_TOTAL_LIMIT}
readonly WORKTREE_RECLAIMABLE_LIMIT={DEFAULT_WORKTREE_RECLAIMABLE_LIMIT}
readonly WORKTREE_COUNT_HELPER="$HOOK_DIR/warn-worktree-count.py"
readonly UPSTREAM_HOOK="$HOOK_DIR/pre-commit.switchyard-upstream"

upstream_status=0
if [[ -x "$UPSTREAM_HOOK" ]]; then
    "$UPSTREAM_HOOK" "$@" || upstream_status=$?
fi
if [[ -x "$FILE_SIZE_HELPER" ]]; then
    "$FILE_SIZE_HELPER" --line-limit "$FILE_SIZE_LIMIT" || true
fi
if [[ -x "$WORKTREE_COUNT_HELPER" ]]; then
    "$WORKTREE_COUNT_HELPER" --total-threshold "${{SWITCHYARD_WORKTREE_TOTAL_WARNING_THRESHOLD:-$WORKTREE_TOTAL_LIMIT}}" --reclaimable-threshold "${{SWITCHYARD_WORKTREE_RECLAIMABLE_WARNING_THRESHOLD:-$WORKTREE_RECLAIMABLE_LIMIT}}" || true
fi
exit "$upstream_status"
"""


def render_template_pre_commit() -> str:
    return render_pre_commit(hook_dir=_OWN_DIRECTORY)


def install_local_warning(repository: Path, *, source_root: Path, line_limit: int = DEFAULT_LINE_LIMIT) -> Path:
    if line_limit <= 0:
        raise ValueError("line limit must be positive")
    hooks = _hooks_path(repository)
    owner = _owner(hooks)
    hooks.mkdir(parents=True, exist_ok=True)
    os.chown(hooks, *owner)
    for installed_name, source_name in PRE_COMMIT_HELPERS:
        _install_file(source_root / "scripts" / source_name, hooks / installed_name, owner=owner)
    hook = hooks / "pre-commit"
    upstream = _preserve_existing_hook(hook, replace_warning_only=True)
    if upstream != hooks / "pre-commit.switchyard-upstream":
        raise RuntimeError(f"unexpected preserved hook path {upstream}")
    text = render_pre_commit(hook_dir=shlex.quote(str(hooks)), line_limit=line_limit)
    _atomic_install_text(hook, text, owner=owner)
    return hook


def install_main_guard(repository: Path, *, project: str) -> Path:
    if not PROJECT_RE.fullmatch(project):
        raise ValueError("project must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    hooks = _hooks_path(repository)
    owner = _owner(hooks)
    hooks.mkdir(parents=True, exist_ok=True)
    os.chown(hooks, *owner)
    hook = hooks / "update"
    upstream = _preserve_existing_hook(hook)
    text = f"""#!/usr/bin/env bash
set -u
{MANAGED_MARKER}

readonly UPSTREAM_HOOK={shlex.quote(str(upstream))}
refname="${{1:-}}"

if [[ "$refname" == "refs/heads/main" && "${{ALLOW_MAIN_PUSH:-}}" != "director" && "${{PGU_ALLOW_MAIN_PUSH:-}}" != "director" ]]; then
    cat >&2 <<'MESSAGE'
[{project} main guard] Push rejected: refs/heads/main is director-only.
Push a feature branch and hand it off through the project workflow.
Director override: ALLOW_MAIN_PUSH=director git push origin HEAD:main
MESSAGE
    exit 1
fi

# PGU's preserved July 2026 hook predates the generic override name. Keep its
# own guard effective while translating the generic director authorization.
if [[ "${{ALLOW_MAIN_PUSH:-}}" == "director" && "${{PGU_ALLOW_MAIN_PUSH:-}}" != "director" ]]; then
    export PGU_ALLOW_MAIN_PUSH=director
fi

if [[ -x "$UPSTREAM_HOOK" ]]; then
    exec "$UPSTREAM_HOOK" "$@"
fi
exit 0
"""
    _atomic_install_text(hook, text, owner=owner)
    return hook


def install_repository_policy(
    repository: Path,
    *,
    source_root: Path,
    project: str,
    local_warning: bool = True,
    main_guard: bool = True,
) -> tuple[Path, ...]:
    installed: list[Path] = []
    if local_warning:
        installed.append(install_local_warning(repository, source_root=source_root))
    if main_guard:
        installed.append(install_main_guard(repository, project=project))
    return tuple(installed)


def install_project_config(config_path: Path, *, source_root: Path) -> tuple[Path, ...]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    project = str(config["project"])
    repositories = [Path(str(config["repository"]))]
    control = config.get("control_repository")
    if control:
        repositories.append(Path(str(control)))
    installed: list[Path] = []
    for repository in repositories:
        installed.extend(install_repository_policy(repository, source_root=source_root, project=project))
    return tuple(installed)


def registry_dir_from_argv(argv: list[str]) -> Path:
    for index, value in enumerate(argv):
        if value == "--registry-dir" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if value.startswith("--registry-dir="):
            return Path(value.split("=", 1)[1]).expanduser()
    return DEFAULT_REGISTRY_DIR


def registry_snapshot(argv: list[str]) -> frozenset[Path]:
    directory = registry_dir_from_argv(argv)
    return frozenset(directory.glob("*.json")) if directory.is_dir() else frozenset()


def install_new_registry_entries(
    argv: list[str], before: Iterable[Path], *, source_root: Path
) -> tuple[Path, ...]:
    added = sorted(registry_snapshot(argv) - frozenset(before))
    if len(added) != 1:
        raise RuntimeError(f"expected one newly registered project, found {len(added)}")
    registry = json.loads(added[0].read_text(encoding="utf-8"))
    return install_project_config(Path(str(registry["config_path"])), source_root=source_root)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Switchyard Git policy hooks.")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--project", default="")
    parser.add_argument("--repository", type=Path, action="append", default=[])
    parser.add_argument("--project-config", type=Path, action="append", default=[])
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--server-only", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    if args.local_only and args.server_only:
        raise SystemExit("--local-only and --server-only are mutually exclusive")
    installed: list[Path] = []
    for config in args.project_config:
        installed.extend(install_project_config(config, source_root=args.source_root))
    for repository in args.repository:
        if not args.project:
            raise SystemExit("--project is required with --repository")
        installed.extend(
            install_repository_policy(
                repository,
                source_root=args.source_root,
                project=args.project,
                local_warning=not args.server_only,
                main_guard=not args.local_only,
            )
        )
    if not installed:
        raise SystemExit("at least one --repository or --project-config is required")
    for path in installed:
        print(f"installed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
