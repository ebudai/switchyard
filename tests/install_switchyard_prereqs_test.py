#!/usr/bin/env python3
"""Regression checks for scripts/install-switchyard-prereqs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-switchyard-prereqs"
APT_BEFORE = "python3 postgresql postgresql-client tmux konsole git curl python3-pip acl"
APT_AFTER = "python3 postgresql postgresql-client tmux git curl python3-venv python3-pil acl"
PACMAN_BEFORE = "python postgresql tmux konsole git curl python-pip acl"
PACMAN_AFTER = "python postgresql tmux git curl python-pip python-pillow acl"
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
        "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1",
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


AGENT_CLI_NAMES = ("claude", "codex", "agy", "hermes")
VENDOR_INSTALLER_MARKERS = ("install.sh", "| bash", "| sh")


def _without_filesystem_paths(text: str) -> str:
    """Drop absolute-path tokens so only prose and command names remain.

    The prereq installer echoes every command it would run, and those commands
    embed this repository's own location. Matching CLI names against the raw
    output therefore asserted something about the reader's directory name
    rather than about the installer -- a checkout at ~/wt-codex-fix failed with
    "prereqs output still mentions codex", blaming the installer for a string
    the test put there itself. That was PGU-908.

    Paths are the only part of the output the repository location can reach, so
    dropping them is enough. URLs survive, because a vendor installer URL does
    not start with a slash -- and it is exactly what these assertions are for.
    """
    kept = []
    for token in text.split():
        if token.lstrip("+'\"(").startswith("/"):
            continue
        kept.append(token)
    return " ".join(kept)


def assert_no_agent_cli_output(output: str) -> None:
    """The prereq installer says nothing about agent CLIs any more.

    Installing them is the user's job; naming a vendor installer here is how
    the previous behaviour started. Checked against what the installer prints,
    with filesystem paths removed -- see _without_filesystem_paths.
    """
    prose = _without_filesystem_paths(output).lower()
    for cli in AGENT_CLI_NAMES:
        assert cli not in prose, f"prereqs output still mentions {cli}"
    for marker in VENDOR_INSTALLER_MARKERS:
        assert marker not in prose, f"prereqs output still shows a vendor installer: {marker}"


def assert_prereqs_script_never_names_an_agent_cli() -> None:
    """The same guard, against the script itself rather than one dry run.

    This is the stronger half and it has no environment in it at all. The
    script's own text covers branches a dry run never reaches -- the apt path
    when the host has pacman, and vice versa -- and it cannot be perturbed by
    where the repository happens to live.
    """
    source = SCRIPT.read_text(encoding="utf-8").lower()
    for cli in AGENT_CLI_NAMES:
        assert cli not in source, f"install-switchyard-prereqs mentions {cli}"
    for marker in VENDOR_INSTALLER_MARKERS:
        assert marker not in source, f"install-switchyard-prereqs carries a vendor installer: {marker}"


def test_prereqs_script_source_names_no_agent_cli() -> None:
    assert_prereqs_script_never_names_an_agent_cli()


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
    assert "Installing psycopg into the shared Switchyard Python venv at /opt/switchyard/venv." in output
    assert (
        "Using a system-site-packages venv avoids Debian/Ubuntu PEP 668 restrictions "
        "while keeping distro Pillow visible."
    ) in output
    assert "+ sudo install -d -m 0755 /opt/switchyard" in output
    assert "+ sudo python3 -m venv --system-site-packages /opt/switchyard/venv" in output
    assert "+ sudo /opt/switchyard/venv/bin/python -m pip install psycopg\\>=3.3\\,\\<4" in output
    assert "Verifying ticket board runtime dependencies under the shared Switchyard Python venv." in output
    assert "+ sudo /opt/switchyard/venv/bin/python -c import\\ psycopg\\,\\ PIL\\;\\ print" in output
    assert "Verifying the ticket board entry point loads under the shared Switchyard Python venv." in output
    assert f"+ sudo /opt/switchyard/venv/bin/python {ROOT / 'scripts' / 'ticket-board.py'} --help" in output
    assert "pip install --user" not in output
    assert "--break-system-packages" not in output
    assert_no_agent_cli_output(output)
    assert "Next step: run scripts/install-switchyard" not in output


def test_pacman_commands() -> None:
    output = run_dry("pacman")
    assert "Not refreshing pacman package databases automatically; run sudo pacman -Syu first on Arch-family systems." in output
    assert f"+ sudo pacman -S --needed {PACMAN_AFTER}" in output
    pacman_install_line = next(line for line in output.splitlines() if line.startswith("+ sudo pacman -S --needed "))
    pacman_packages = set(pacman_install_line.removeprefix("+ sudo pacman -S --needed ").split())
    assert pacman_packages.isdisjoint(AGENT_CLI_PACKAGE_NAMES)
    assert PACMAN_BEFORE not in output
    assert "KDE/separate layout users also need Konsole: sudo pacman -S --needed konsole" in output
    assert "Installing psycopg for the invoking user with pip on Arch-family systems." in output
    assert "+ python3 -m pip install --user psycopg\\>=3.3\\,\\<4" in output
    assert "/opt/switchyard/venv" not in output
    assert_no_agent_cli_output(output)
    assert "Next step: run scripts/install-switchyard" not in output


def test_pacman_pip_deescalates_to_invoking_user_when_run_as_root() -> None:
    env = {
        **os.environ,
        "SWITCHYARD_PREREQS_ASSUME_MANAGER": "pacman",
        "SWITCHYARD_SUDO_BIN": "sudo",
        "SWITCHYARD_PYTHON_BIN": "python3",
        "SWITCHYARD_INSTALL_ORIGINAL_USER": "alice",
        "SWITCHYARD_PREREQS_TEST_ASSUME_ROOT": "1",
    }
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    output = proc.stdout

    assert "Installing psycopg for the invoking user with pip on Arch-family systems." in output
    assert "+ sudo -u alice -H sh -lc python3\\ -m\\ pip\\ install\\ --user\\ " in output
    assert "psycopg" in output
    assert "+ python3 -m pip install --user" not in output


def test_fresh_machine_docs_match_manual_agent_cli_policy() -> None:
    docs = (ROOT / "docs" / "fresh-machine-install.md").read_text(encoding="utf-8")
    normalized_docs = " ".join(docs.split())
    apt_python_section = docs.split("On Arch-family systems, the current system-user pip path remains:")[0]
    assert "sudo apt-get install python3 postgresql postgresql-client tmux git curl python3-venv python3-pil acl" in docs
    assert "sudo pacman -S python postgresql tmux git curl python-pip python-pillow acl" in docs
    assert "python3 -m pip install --user 'psycopg>=3.3,<4'" not in apt_python_section
    assert "sudo python3 -m venv --system-site-packages /opt/switchyard/venv" in docs
    assert "sudo /opt/switchyard/venv/bin/python -m pip install 'psycopg>=3.3,<4'" in docs
    assert "sudo /opt/switchyard/venv/bin/python -c 'import psycopg, PIL; print(psycopg.__version__, PIL.__version__)'" in docs
    assert "sudo /opt/switchyard/venv/bin/python scripts/ticket-board.py --help" in docs
    assert "`python3-psycopg` exists but is `3.1.17-2`" in docs
    assert "does not satisfy Switchyard's `psycopg>=3.3,<4` pin" in normalized_docs
    assert "sudo ./install" in docs

    # The CLIs are the user's responsibility, and the docs must say so rather
    # than describe an installer that offers to do it.
    assert "Switchyard does not install, download, or upgrade the agent CLIs." in normalized_docs
    assert "Switchyard supports four agent CLIs and installs none of them." in normalized_docs
    assert "Switchyard never runs the commands below." in normalized_docs
    assert "for the user that will own the project" in normalized_docs
    assert "`switchyard new` detects the CLIs that are present" in normalized_docs
    for removed in (
        "can run the vendor-documented agent CLI installers",
        "asks before running each missing CLI",
        "Pass `--cli <name>`",
        "--no-cli",
        "--skip-cli",
    ):
        assert removed not in normalized_docs, removed

    # Vendor provenance stays, as documentation the reader verifies.
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


def test_readme_states_the_user_owns_agent_cli_installation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split())

    assert "Switchyard does not install the agent CLIs." in normalized
    assert "for the user that will own the project" in normalized
    assert "walks their sign-in when you create a project" in normalized
    assert "--cli" not in readme
    assert "--no-cli" not in readme


def test_prereqs_rejects_the_removed_cli_flags() -> None:
    for flag in ("--skip-cli", "--no-cli"):
        proc = subprocess.run(
            [str(SCRIPT), "--dry-run", flag],
            cwd=ROOT,
            env={**os.environ, "SWITCHYARD_PREREQS_ASSUME_MANAGER": "apt"},
            text=True,
            capture_output=True,
        )
        assert proc.returncode != 0, flag
        assert f"unknown argument: {flag}" in proc.stderr


def test_package_sets_match_the_platform_python_strategy() -> None:
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
        "python3-venv",
        "python3-pil",
        "acl",
    ]
    assert PACMAN_BEFORE.split() == ["python", "postgresql", "tmux", "konsole", "git", "curl", "python-pip", "acl"]
    assert PACMAN_AFTER.split() == [
        "python",
        "postgresql",
        "tmux",
        "git",
        "curl",
        "python-pip",
        "python-pillow",
        "acl",
    ]
    assert set(APT_BEFORE.split()) - set(APT_AFTER.split()) == {"konsole", "python3-pip"}
    assert set(APT_AFTER.split()) - set(APT_BEFORE.split()) == {"python3-venv", "python3-pil"}
    assert set(PACMAN_BEFORE.split()) - set(PACMAN_AFTER.split()) == {"konsole"}
    assert set(PACMAN_AFTER.split()) - set(PACMAN_BEFORE.split()) == {"python-pillow"}


def test_system_site_venv_runs_ticket_board_entrypoint() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        venv = Path(tmpdir) / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        import_proc = subprocess.run(
            [
                str(venv / "bin" / "python"),
                "-c",
                "import psycopg, PIL; print(psycopg.__version__, PIL.__version__)",
            ],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        entrypoint_proc = subprocess.run(
            [str(venv / "bin" / "python"), str(ROOT / "scripts" / "ticket-board.py"), "--help"],
            cwd=ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    assert import_proc.stdout.strip()
    assert "usage:" in entrypoint_proc.stdout


if __name__ == "__main__":
    test_prereqs_script_source_names_no_agent_cli()
    test_apt_commands()
    test_pacman_commands()
    test_pacman_pip_deescalates_to_invoking_user_when_run_as_root()
    test_fresh_machine_docs_match_manual_agent_cli_policy()
    test_readme_states_the_user_owns_agent_cli_installation()
    test_prereqs_rejects_the_removed_cli_flags()
    test_package_sets_match_the_platform_python_strategy()
    test_system_site_venv_runs_ticket_board_entrypoint()
    print("install_switchyard_prereqs_test: ok")
