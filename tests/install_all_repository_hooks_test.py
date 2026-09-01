#!/usr/bin/env python3
"""The privileged rollout covers bare, shared, and registered repositories."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})


def init_repo(path: Path, *, bare: bool = False) -> None:
    command = ["git", "init"]
    if bare:
        command.append("--bare")
    else:
        command.extend(["-b", "main"])
    command.append(str(path))
    run(*command)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="install-all-hooks.") as raw_temp:
        temp = Path(raw_temp)
        bare_root = temp / "bare"
        registry = temp / "registry"
        shared = temp / "shared"
        owner = temp / "owner"
        control = temp / "control.git"
        bare_root.mkdir()
        registry.mkdir()
        init_repo(bare_root / "legacy.git", bare=True)
        init_repo(bare_root / "pgu.git", bare=True)
        init_repo(bare_root / "switchyard.git", bare=True)
        init_repo(shared)
        init_repo(owner)
        init_repo(control, bare=True)
        config = temp / "tenant.json"
        config.write_text(json.dumps({
            "project": "tenant",
            "repository": str(owner),
            "control_repository": str(control),
        }), encoding="utf-8")
        (registry / "tenant.json").write_text(json.dumps({"config_path": str(config)}), encoding="utf-8")
        environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "SWITCHYARD_HOOK_INSTALL_ALLOW_NON_ROOT": "1",
            "SWITCHYARD_HOOK_SOURCE_ROOT": str(ROOT),
            "SWITCHYARD_BARE_REPO_ROOT": str(bare_root),
            "SWITCHYARD_PROJECT_REGISTRY_DIR": str(registry),
            "SWITCHYARD_SHARED_CHECKOUTS": str(shared),
        }
        installed = subprocess.run(
            [str(ROOT / "scripts" / "install-all-repository-hooks.sh")],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert f"NO MAIN GUARD (no Switchyard workflow): {bare_root / 'legacy.git'}" in installed.stdout
        assert f"MAIN GUARD (platform workflow): {bare_root / 'pgu.git'}" in installed.stdout
        assert "FILE-SIZE WARNING ONLY:" in installed.stdout
        assert "REGISTERED WORKFLOW (warning + main guard):" in installed.stdout
        assert not (bare_root / "legacy.git" / "hooks" / "update").exists()
        assert (bare_root / "pgu.git" / "hooks" / "update").exists()
        assert (bare_root / "switchyard.git" / "hooks" / "update").exists()
        assert (shared / ".git" / "hooks" / "pre-commit").exists()
        assert not (shared / ".git" / "hooks" / "update").exists()
        assert (owner / ".git" / "hooks" / "pre-commit").exists()
        assert (owner / ".git" / "hooks" / "update").exists()
        assert (control / "hooks" / "pre-commit").exists()
        assert (control / "hooks" / "update").exists()

        default_environment = dict(environment)
        default_environment.pop("SWITCHYARD_SHARED_CHECKOUTS")
        dry_run_without_shared_default = subprocess.run(
            [str(ROOT / "scripts" / "install-all-repository-hooks.sh"), "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
            env=default_environment,
        )
        assert "FILE-SIZE WARNING ONLY:" not in dry_run_without_shared_default.stdout
        assert str(shared) not in dry_run_without_shared_default.stdout
        assert "/home/eric/Projects/pgu" not in dry_run_without_shared_default.stdout
        assert "/home/agent/Projects/pgu" not in dry_run_without_shared_default.stdout
        assert "--project-config" in dry_run_without_shared_default.stdout

        dry_run = subprocess.run(
            [str(ROOT / "scripts" / "install-all-repository-hooks.sh"), "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert f"NO MAIN GUARD (no Switchyard workflow): {bare_root / 'legacy.git'}" in dry_run.stdout
        assert "--server-only" in dry_run.stdout
        assert "--local-only" in dry_run.stdout
        assert "--project-config" in dry_run.stdout

    print("install_all_repository_hooks_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
