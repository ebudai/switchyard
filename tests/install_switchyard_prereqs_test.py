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
AGENT_CLI_PACKAGE_NAMES = {
    "claude",
    "claude-code",
    "codex",
    "openai-codex",
    "agy",
    "antigravity-cli",
    "hermes",
}


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
    apt_install_line = next(line for line in output.splitlines() if line.startswith("+ sudo apt-get install -y "))
    apt_packages = set(apt_install_line.removeprefix("+ sudo apt-get install -y ").split())
    assert apt_packages.isdisjoint(AGENT_CLI_PACKAGE_NAMES)
    assert APT_BEFORE not in output
    assert "KDE/separate layout users also need Konsole: sudo apt-get install -y konsole" in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "Agent CLI setup is manual; this script does not install agent CLIs." in output
    assert "Switchyard needs at least one installed and authenticated agent CLI" in output
    assert "https://claude.ai/install.sh | bash" in output
    assert "Debian/Ubuntu package is claude-code, not claude" in output
    assert "https://chatgpt.com/codex/install.sh | sh" in output
    assert "https://antigravity.google/cli/install.sh | bash" in output
    assert "https://hermes-agent.nousresearch.com/install.sh | bash" in output
    assert "See docs/fresh-machine-install.md for vendor documentation links." in output
    assert "fetch the script URL without piping it and read the script first" in output
    assert "authenticate at least one of: claude, codex, agy, or hermes" in output
    assert "Next step: run scripts/install-switchyard to print the privileged install block." in output


def test_pacman_commands() -> None:
    output = run_dry("pacman")
    assert "Not refreshing pacman package databases automatically; run sudo pacman -Syu first on Arch-family systems." in output
    assert f"+ sudo pacman -S --needed {PACMAN_AFTER}" in output
    pacman_install_line = next(line for line in output.splitlines() if line.startswith("+ sudo pacman -S --needed "))
    pacman_packages = set(pacman_install_line.removeprefix("+ sudo pacman -S --needed ").split())
    assert pacman_packages.isdisjoint(AGENT_CLI_PACKAGE_NAMES)
    assert PACMAN_BEFORE not in output
    assert "KDE/separate layout users also need Konsole: sudo pacman -S --needed konsole" in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "Agent CLI setup is manual; this script does not install agent CLIs." in output
    assert "https://claude.ai/install.sh | bash" in output
    assert "https://chatgpt.com/codex/install.sh | sh" in output
    assert "https://antigravity.google/cli/install.sh | bash" in output
    assert "https://hermes-agent.nousresearch.com/install.sh | bash" in output
    assert "See docs/fresh-machine-install.md for vendor documentation links." in output
    assert "fetch the script URL without piping it and read the script first" in output
    assert "authenticate at least one of: claude, codex, agy, or hermes" in output
    assert "Next step: run scripts/install-switchyard to print the privileged install block." in output


def test_fresh_machine_docs_match_manual_agent_cli_policy() -> None:
    docs = (ROOT / "docs" / "fresh-machine-install.md").read_text(encoding="utf-8")
    normalized_docs = " ".join(docs.split())
    assert "Agent CLI setup is manual." in docs
    assert "does not install any agent CLI" in normalized_docs
    assert "failed or missing agent CLI install must not fail the host package run" in normalized_docs
    assert "Before running any `curl ... | bash` or `curl ... | sh` command" in normalized_docs
    assert "read the script first" in normalized_docs
    assert "`claude-code`, not `claude`" in docs
    assert "https://code.claude.com/docs/en/setup" in docs
    assert "https://chatgpt.com/codex/install.sh" in docs
    assert "https://learn.chatgpt.com/docs/codex/cli" in docs
    assert "https://antigravity.google/cli/install.sh" in docs
    assert "https://antigravity.google/docs/cli/install/" in docs
    assert "https://hermes-agent.nousresearch.com/install.sh" in docs
    assert "https://hermes-agent.nousresearch.com/docs/getting-started/installation" in docs
    assert "complete authentication" in docs


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
    test_fresh_machine_docs_match_manual_agent_cli_policy()
    test_only_konsole_left_the_base_package_sets()
    print("install_switchyard_prereqs_test: ok")
