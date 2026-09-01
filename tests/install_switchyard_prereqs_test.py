#!/usr/bin/env python3
"""Regression checks for scripts/install-switchyard-prereqs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-switchyard-prereqs"


def run_dry(manager: str) -> str:
    env = {
        **os.environ,
        "SWITCHYARD_PREREQS_ASSUME_MANAGER": manager,
        "SWITCHYARD_SUDO_BIN": "sudo",
        "SWITCHYARD_PYTHON_BIN": "python3",
    }
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return proc.stdout


def test_apt_commands() -> None:
    output = run_dry("apt")
    assert "+ sudo apt-get update" in output
    assert "+ sudo apt-get install -y python3 postgresql postgresql-client tmux konsole git curl python3-pip acl" in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "claude, codex, agy, or hermes" in output


def test_pacman_commands() -> None:
    output = run_dry("pacman")
    assert "+ sudo pacman -S --needed python postgresql tmux konsole git curl python-pip acl" in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "claude, codex, agy, or hermes" in output


if __name__ == "__main__":
    test_apt_commands()
    test_pacman_commands()
    print("install_switchyard_prereqs_test: ok")
