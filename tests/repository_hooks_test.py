#!/usr/bin/env python3
"""Regression coverage for Switchyard repository policy installation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.repository_hooks import (
    MANAGED_MARKER,
    install_new_registry_entries,
    install_project_config,
    install_repository_policy,
    registry_snapshot,
)


def run(*args: str, cwd: Path | None = None, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = {**os.environ}
    if env:
        command_env.update(env)
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True, env=command_env)


def seed_repository(path: Path) -> None:
    run("git", "init", "-b", "main", str(path))
    run("git", "-C", str(path), "config", "user.name", "Test")
    run("git", "-C", str(path), "config", "user.email", "test@example.com")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    run("git", "-C", str(path), "add", "README.md")
    run("git", "-C", str(path), "commit", "-m", "seed")


def main() -> int:
    os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
    with tempfile.TemporaryDirectory(prefix="repository-hooks.") as raw_temp:
        temp = Path(raw_temp)
        remote = temp / "remote.git"
        seed = temp / "seed"
        client = temp / "client"
        worktree = temp / "worktree"
        run("git", "init", "--bare", str(remote))
        seed_repository(seed)
        run("git", "-C", str(seed), "remote", "add", "origin", str(remote))
        run("git", "-C", str(seed), "push", "origin", "main")
        run("git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
        run("git", "clone", str(remote), str(client))
        run("git", "-C", str(client), "config", "user.name", "Test")
        run("git", "-C", str(client), "config", "user.email", "test@example.com")
        run("git", "-C", str(client), "worktree", "add", "-b", "feature/worktree-warning", str(worktree))

        # A caller-global hooksPath must not redirect managed repository hooks.
        global_hooks = temp / "global-hooks"
        global_hooks.mkdir()
        global_config = temp / "global.gitconfig"
        run("git", "config", "--file", str(global_config), "core.hooksPath", str(global_hooks))
        os.environ["GIT_CONFIG_GLOBAL"] = str(global_config)
        hooks = Path(run("git", "-C", str(client), "rev-parse", "--git-common-dir").stdout.strip()) / "hooks"
        if not hooks.is_absolute():
            hooks = client / hooks
        existing_precommit = hooks / "pre-commit"
        existing_precommit.write_text("#!/bin/sh\necho upstream-precommit >&2\nexit 0\n", encoding="utf-8")
        existing_precommit.chmod(0o755)
        remote_update = remote / "hooks" / "update"
        update_marker = temp / "upstream-update-ran"
        upstream_update_text = (
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = refs/heads/main ] && [ \"${PGU_ALLOW_MAIN_PUSH:-}\" != director ]; then\n"
            "  echo legacy-main-guard-rejected >&2\n"
            "  exit 1\n"
            "fi\n"
            f"echo ran >>{update_marker}\n"
            "exit 0\n"
        )
        remote_update.write_text(upstream_update_text, encoding="utf-8")
        remote_update.chmod(0o755)

        install_repository_policy(client, source_root=REPO_ROOT, project="demo", main_guard=False)
        install_repository_policy(remote, source_root=REPO_ROOT, project="demo", local_warning=False)
        # Reinstallation is idempotent and does not recursively wrap managed hooks.
        install_repository_policy(client, source_root=REPO_ROOT, project="demo", main_guard=False)
        install_repository_policy(remote, source_root=REPO_ROOT, project="demo", local_warning=False)
        assert MANAGED_MARKER in existing_precommit.read_text(encoding="utf-8")
        assert not any(global_hooks.iterdir())
        assert "safe.directory" not in global_config.read_text(encoding="utf-8")
        assert run("git", "-C", str(client), "config", "--local", "--get", "core.hooksPath").stdout.strip() == str(hooks.resolve())
        assert run("git", "-C", str(remote), "config", "--local", "--get", "core.hooksPath").stdout.strip() == str((remote / "hooks").resolve())
        assert (hooks / "pre-commit.switchyard-upstream").exists()
        preserved_update = remote / "hooks" / "update.switchyard-upstream"
        assert preserved_update.read_text(encoding="utf-8") == upstream_update_text
        assert not (remote / "hooks" / "update.switchyard-upstream.switchyard-upstream").exists()

        oversized = worktree / "oversized.py"
        oversized.write_text("".join(f"line_{index}\n" for index in range(1251)), encoding="utf-8")
        run("git", "-C", str(worktree), "add", "oversized.py")
        committed = run("git", "-C", str(worktree), "commit", "-m", "oversized from worktree")
        assert "upstream-precommit" in committed.stderr
        assert "warning: oversized.py is 1251 lines (soft limit 1250)" in committed.stderr

        feature_push = run("git", "-C", str(worktree), "push", "origin", "HEAD:refs/heads/feature/worktree-warning")
        assert feature_push.returncode == 0 and update_marker.exists()
        blocked = run("git", "-C", str(worktree), "push", "origin", "HEAD:main", check=False)
        assert blocked.returncode != 0
        assert "refs/heads/main is director-only" in blocked.stderr
        allowed = run(
            "git", "-C", str(worktree), "push", "origin", "HEAD:main",
            env={"ALLOW_MAIN_PUSH": "director"},
        )
        assert allowed.returncode == 0

        # Generated project configs install both owner-checkout and shared-control policy.
        owner_repo = temp / "owner"
        control_repo = temp / "control.git"
        seed_repository(owner_repo)
        run("git", "clone", "--bare", str(owner_repo), str(control_repo))
        config = temp / "demo.json"
        config.write_text(json.dumps({
            "project": "demo",
            "repository": str(owner_repo),
            "control_repository": str(control_repo),
        }), encoding="utf-8")
        installed = install_project_config(config, source_root=REPO_ROOT)
        assert len(installed) == 4
        assert all(path.exists() for path in installed)

        registry = temp / "registry"
        registry.mkdir()
        argv = ["new", "--registry-dir", str(registry)]
        before = registry_snapshot(argv)
        (registry / "demo.json").write_text(json.dumps({"config_path": str(config)}), encoding="utf-8")
        assert len(install_new_registry_entries(argv, before, source_root=REPO_ROOT)) == 4

    print("repository_hooks_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
