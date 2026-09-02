#!/usr/bin/env python3
"""Regression checks for scripts/install-switchyard-prereqs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-switchyard-prereqs"
APT_BEFORE = "python3 postgresql postgresql-client tmux konsole git curl python3-pip acl"
APT_AFTER = "python3 postgresql postgresql-client tmux git curl python3-pip acl"
PACMAN_BEFORE = "python postgresql tmux konsole git curl python-pip acl"
PACMAN_AFTER = "python postgresql tmux git curl python-pip acl"


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
    assert "Refreshing apt package index with apt-get update before installing packages." in output
    assert "+ sudo apt-get update" in output
    assert f"+ sudo apt-get install -y {APT_AFTER}" in output
    assert APT_BEFORE not in output
    assert "KDE/separate layout users also need Konsole: sudo apt-get install -y konsole" in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "claude, codex, agy, or hermes" in output
    assert "Next step: run scripts/install-switchyard to print the privileged install block." in output


def test_pacman_commands() -> None:
    output = run_dry("pacman")
    assert "Not refreshing pacman package databases automatically; run sudo pacman -Syu first on Arch-family systems." in output
    assert f"+ sudo pacman -S --needed {PACMAN_AFTER}" in output
    assert PACMAN_BEFORE not in output
    assert "KDE/separate layout users also need Konsole: sudo pacman -S --needed konsole" in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "claude, codex, agy, or hermes" in output
    assert "Next step: run scripts/install-switchyard to print the privileged install block." in output


def test_only_konsole_left_the_base_package_sets() -> None:
    assert APT_BEFORE.split() == [
        "python3",
        "postgresql",
        "postgresql-client",
        "tmux",
        "konsole",
        "git",
        "curl",
        "python3-pip",
        "acl",
    ]
    assert APT_AFTER.split() == [
        "python3",
        "postgresql",
        "postgresql-client",
        "tmux",
        "git",
        "curl",
        "python3-pip",
        "acl",
    ]
    assert PACMAN_BEFORE.split() == ["python", "postgresql", "tmux", "konsole", "git", "curl", "python-pip", "acl"]
    assert PACMAN_AFTER.split() == ["python", "postgresql", "tmux", "git", "curl", "python-pip", "acl"]
    assert set(APT_BEFORE.split()) - set(APT_AFTER.split()) == {"konsole"}
    assert set(PACMAN_BEFORE.split()) - set(PACMAN_AFTER.split()) == {"konsole"}


if __name__ == "__main__":
    test_apt_commands()
    test_pacman_commands()
    test_only_konsole_left_the_base_package_sets()
    print("install_switchyard_prereqs_test: ok")
