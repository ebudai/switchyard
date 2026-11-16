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
PACMAN_AFTER = "python postgresql tmux git curl python-pillow python-psycopg acl"
AGENT_CLI_PACKAGE_NAMES = {
    "claude",
    "claude-code",
    "codex",
    "openai-codex",
    "agy",
    "antigravity-cli",
    "hermes",
}


def executed_commands(output: str) -> list[str]:
    """Only the lines the installer would actually run, not its explanatory prose."""
    return [line for line in output.splitlines() if line.startswith("+ ")]


STAND_INS = Path(tempfile.mkdtemp(prefix="syrd828-stand-ins."))


def stand_in(name: str, body: str) -> str:
    """An executable in this suite's own directory: never the host's package manager or PostgreSQL."""
    path = STAND_INS / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def host_with(*, installed: tuple[str, ...] = (), server: str = "current") -> dict[str, str]:
    """A host whose package database holds `installed` (everything else is missing) and whose running PostgreSQL
    server is `server`: "current", or "replaced" (older than its installed binary)."""
    tag = "-".join(installed) or "fresh"
    listed = " ".join(installed)
    pacman = stand_in(f"pacman-{tag}", f"""[ "$1" = -T ] || {{ echo "unmodelled pacman $*" >&2; exit 99; }}
shift; missing=0
for p in "$@"; do case " {listed} " in *" $p "*) ;; *) echo "$p"; missing=1;; esac; done
[ $missing = 1 ] && exit 127 || exit 0
""")
    dpkg = stand_in(f"dpkg-query-{tag}", f"""p="$(eval echo \\${{$#}})"
case " {listed} " in *" $p "*) printf installed; exit 0;; *) exit 1;; esac
""")
    check = stand_in(f"pg-check-{server}", "echo 'the running PostgreSQL server (pid 42) started before its binary "
                     "/usr/bin/postgres was replaced'; exit 1\n" if server == "replaced" else "exit 0\n")
    return {"SWITCHYARD_PACMAN_BIN": pacman, "SWITCHYARD_DPKG_QUERY_BIN": dpkg, "SWITCHYARD_POSTGRES_REPLACED_CHECK": check}


def run_dry(manager: str, *, python_venv: str = "/nonexistent/switchyard-venv", host: dict[str, str] | None = None,
            check: bool = True) -> str:
    env = {
        **os.environ,
        **(host if host is not None else host_with()),
        "SWITCHYARD_PREREQS_ASSUME_MANAGER": manager,
        "SWITCHYARD_SUDO_BIN": "sudo",
        "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1",
        "SWITCHYARD_PYTHON_BIN": "python3",
        # Pinned so these assertions describe the installer, not whether this
        # particular host happens to have a shared venv already.
        "SWITCHYARD_PYTHON_VENV": python_venv,
    }
    proc = subprocess.run(
        [str(SCRIPT), "--dry-run"],
        cwd=ROOT,
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )
    return proc.stdout if check else proc.stdout + proc.stderr + f"\nexit {proc.returncode}"


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
    output = run_dry("apt", python_venv="/opt/switchyard/venv")
    assert "Refreshing apt package index with apt-get update before installing packages." in output
    assert "+ sudo apt-get update" in output
    assert f"+ sudo apt-get install -y --no-upgrade {APT_AFTER}" in output
    apt_install_line = next(line for line in output.splitlines() if line.startswith("+ sudo apt-get install -y "))
    apt_packages = set(apt_install_line.removeprefix("+ sudo apt-get install -y --no-upgrade ").split())
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
    assert "python-psycopg" in pacman_packages
    assert pacman_packages.isdisjoint(AGENT_CLI_PACKAGE_NAMES)
    assert PACMAN_BEFORE not in output
    assert "KDE/separate layout users also need Konsole: sudo pacman -S --needed konsole" in output
    assert "Installing psycopg from the distribution package python-psycopg" in output
    # Verification runs under the interpreter generated services select, which with no
    # shared venv present is /usr/bin/python3 -- see ticket-board-service.sh.
    assert "Verifying ticket board runtime dependencies with /usr/bin/python3, the interpreter generated services use." in output
    assert "+ /usr/bin/python3 -c import\\ psycopg\\,\\ PIL\\;\\ print" in output
    assert "Verifying the ticket board entry point loads with /usr/bin/python3." in output
    assert f"+ /usr/bin/python3 {ROOT / 'scripts' / 'ticket-board.py'} --help" in output
    # The Arch path builds no venv and runs no pip at all.
    assert "/opt/switchyard/venv" not in output
    assert not any("pip" in c for c in executed_commands(output)), executed_commands(output)
    assert_no_agent_cli_output(output)
    assert "Next step: run scripts/install-switchyard" not in output


def test_pacman_verification_follows_the_shared_venv_when_one_exists() -> None:
    """The rule is the service's rule, not a hardcoded interpreter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        venv = Path(tmpdir) / "venv"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        output = run_dry("pacman", python_venv=str(venv))

    assert f"Verifying ticket board runtime dependencies with {python}, the interpreter generated services use." in output
    assert f"+ {python} -c" in output
    assert "/usr/bin/python3 -c" not in output


def test_no_supported_path_uses_user_pip_or_break_system_packages() -> None:
    """The failure this ticket exists to remove, pinned against the script itself.

    Checked against the source rather than one dry run, so it covers the branch this
    host does not take.
    """
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--user" not in source, "install-switchyard-prereqs still has a user pip path"
    assert "--break-system-packages" not in source
    for manager in ("apt", "pacman"):
        output = run_dry(manager, python_venv="/opt/switchyard/venv" if manager == "apt" else "/nonexistent/switchyard-venv")
        assert not any("--user" in c for c in executed_commands(output)), manager
        assert "--break-system-packages" not in output, manager


def test_pacman_path_installs_nothing_as_the_invoking_user_when_run_as_root() -> None:
    """Nothing de-escalates to the original user any more, because nothing pips.

    The root wrapper still passes SWITCHYARD_INSTALL_ORIGINAL_USER; this pins that the
    prereq script no longer acts on it, rather than leaving that silently untested.
    """
    env = {
        **os.environ,
        "SWITCHYARD_PREREQS_ASSUME_MANAGER": "pacman",
        "SWITCHYARD_SUDO_BIN": "sudo",
        "SWITCHYARD_PYTHON_BIN": "python3",
        "SWITCHYARD_PYTHON_VENV": "/nonexistent/switchyard-venv",
        "SWITCHYARD_INSTALL_ORIGINAL_USER": "alice",
        "SWITCHYARD_PREREQS_TEST_ASSUME_ROOT": "1",
        **host_with(),
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

    assert "sudo -u alice" not in output
    assert not any("pip" in c for c in executed_commands(output)), executed_commands(output)
    # SYRD-235: already root, pacman runs directly, as apt does. The second sudo
    # this used to add put pacman's confirmation on a nested pty (Arch's default
    # use_pty) that no keystroke reached, so a fresh `./install` hung there.
    assert f"+ pacman -S --needed {PACMAN_AFTER}" in output, output
    assert not [c for c in executed_commands(output) if c.startswith("+ sudo ")], executed_commands(output)
    assert "+ /usr/bin/python3 -c" in output


def test_pacman_still_refuses_to_refresh_databases_itself() -> None:
    """The no-pacman-Sy policy survives this change."""
    output = run_dry("pacman")
    assert "run sudo pacman -Syu first" in output
    for call in ("pacman -Sy", "pacman -Syu", "pacman -Syyu"):
        assert f"+ sudo {call}" not in output
    source = SCRIPT.read_text(encoding="utf-8")
    assert "pacman -Sy" not in source.replace("pacman -Syu first", "")


def test_arch_system_python_satisfies_the_pin_it_replaces_pip_with() -> None:
    """The premise: the packaged Psycopg really does satisfy psycopg>=3.3,<4.

    Skipped where the interpreter has no psycopg, so this does not fail on a host that
    has not run the installer.
    """
    probe = subprocess.run(
        ["/usr/bin/python3", "-c", "import psycopg; print(psycopg.__version__)"],
        text=True,
        capture_output=True,
    )
    if probe.returncode != 0:
        return
    major, minor = (int(part) for part in probe.stdout.strip().split(".")[:2])
    assert (3, 3) <= (major, minor) < (4, 0), probe.stdout


def test_fresh_machine_docs_match_manual_agent_cli_policy() -> None:
    docs = (ROOT / "docs" / "fresh-machine-install.md").read_text(encoding="utf-8")
    normalized_docs = " ".join(docs.split())
    apt_python_section = docs.split("Arch-family systems need no venv and no pip.")[0]
    assert "sudo apt-get install python3 postgresql postgresql-client tmux git curl python3-venv python3-pil acl" in docs
    assert "sudo pacman -S python postgresql tmux git curl python-pillow python-psycopg acl" in docs
    # Prose explaining that PEP 668 refuses `pip install --user` is correct and stays;
    # what must be gone is any runnable command form of it.
    assert "-m pip install --user" not in docs, "fresh-machine docs still prescribe a user pip command"
    for line in docs.splitlines():
        stripped = line.strip().removeprefix("sudo ")
        assert not stripped.startswith("pip install --user"), line
    assert "python-psycopg" in apt_python_section or "python-psycopg" in docs
    assert "Arch-family systems need no venv and no pip." in normalized_docs
    assert "/usr/bin/python3 scripts/ticket-board.py --help" in docs
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
    assert "Switchyard supports five agent CLIs and installs none of them." in normalized_docs
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
        "python-pillow",
        "python-psycopg",
        "acl",
    ]
    assert set(APT_BEFORE.split()) - set(APT_AFTER.split()) == {"konsole", "python3-pip"}
    assert set(APT_AFTER.split()) - set(APT_BEFORE.split()) == {"python3-venv", "python3-pil"}
    # python-pip goes with the pip call it existed for, mirroring python3-pip on apt.
    assert set(PACMAN_BEFORE.split()) - set(PACMAN_AFTER.split()) == {"konsole", "python-pip"}
    assert set(PACMAN_AFTER.split()) - set(PACMAN_BEFORE.split()) == {"python-pillow", "python-psycopg"}


def install_line(output: str, prefix: str) -> list[str]:
    lines = [line for line in executed_commands(output) if line.startswith(prefix)]
    return lines[0].removeprefix(prefix).split() if lines else []


def test_an_installed_package_is_never_upgraded_by_pacman() -> None:
    """SYRD-828: `pacman -S --needed` upgraded an installed, outdated postgresql under every running board."""
    output = run_dry("pacman", host=host_with(installed=("python", "postgresql", "tmux")))
    installing = install_line(output, "+ sudo pacman -S --needed ")
    assert installing == ["git", "curl", "python-pillow", "python-psycopg", "acl", "polkit"], (installing, output)
    assert ("Already installed, left as they are (this installer never upgrades a package; host upgrades are yours: "
            "sudo pacman -Syu first): python postgresql tmux") in output, output
    assert "Installing the missing packages: git curl python-pillow python-psycopg acl polkit" in output, output
    assert not [c for c in executed_commands(output) if "pacman -Sy" in c or "pacman -Su" in c], executed_commands(output)

    everything = run_dry("pacman", host=host_with(
        installed=("python", "postgresql", "tmux", "git", "curl", "python-pillow", "python-psycopg", "acl", "polkit"),
        server="replaced"), check=False)
    assert everything.rstrip().endswith("exit 0"), everything
    assert install_line(everything, "+ sudo pacman -S") == [], executed_commands(everything)
    assert "Nothing to install: every package is already present." in everything
    # Nothing installed, so nothing moved: the running server is not this installer's to report (status does).
    assert "systemctl restart postgresql" not in everything, everything


def test_an_installed_package_is_never_upgraded_by_apt() -> None:
    output = run_dry("apt", python_venv="/opt/switchyard/venv", host=host_with(installed=("postgresql", "postgresql-client")))
    installing = install_line(output, "+ sudo apt-get install -y --no-upgrade ")
    assert installing and "postgresql" not in installing and "postgresql-client" not in installing, (installing, output)
    assert {"python3", "tmux", "git", "curl", "python3-venv", "python3-pil", "acl"} <= set(installing), installing
    assert ("Already installed, left as they are (this installer never upgrades a package; host upgrades are yours: "
            "sudo apt-get upgrade): postgresql postgresql-client") in output, output
    assert not [c for c in executed_commands(output) if "apt-get upgrade" in c or "dist-upgrade" in c]


def run_real(manager: str, *, host: dict[str, str]) -> tuple[int, str, list[str]]:
    """The installer for real, but every privileged command goes to a stand-in sudo that records it and refuses
    anything other than the package install, so nothing reaches this host. (exit code, output, sudo's commands)."""
    log = STAND_INS / f"sudo-{manager}-{len(list(STAND_INS.iterdir()))}.log"
    sudo = stand_in(log.name + ".sh", f"""echo "$*" >> {log}
case "$1" in pacman|apt-get) exit 0;; *) exit 1;; esac
""")
    proc = subprocess.run([str(SCRIPT)], cwd=ROOT, text=True, capture_output=True, env={
        **os.environ, **host, "SWITCHYARD_PREREQS_ASSUME_MANAGER": manager, "SWITCHYARD_SUDO_BIN": sudo,
        "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1", "SWITCHYARD_PYTHON_BIN": "python3",
        "SWITCHYARD_PYTHON_VENV": "/nonexistent/switchyard-venv"})
    return proc.returncode, proc.stdout + proc.stderr, log.read_text().splitlines() if log.exists() else []


def test_a_running_server_left_stale_stops_the_install() -> None:
    """Installing a missing package can still move PostgreSQL (a dependency pulled at a newer version). The server is
    shared, so the installer never restarts it: it stops, naming the restart, before anything relies on it."""
    for manager in ("pacman", "apt"):
        code, said, sudo = run_real(manager, host=host_with(installed=("python",), server="replaced"))
        assert code == 1, (manager, code, said)
        assert "started before its binary /usr/bin/postgres was replaced" in said, (manager, said)
        assert "older than the PostgreSQL now installed" in said, (manager, said)
        assert "does not restart it. Restart it yourself: sudo systemctl restart postgresql" in said, (manager, said)
        assert "Ensuring the local PostgreSQL cluster" not in said, (manager, said)
        assert sudo and all(line.split()[0] in ("pacman", "apt-get") for line in sudo), (manager, sudo)
    quiet = run_dry("pacman", host=host_with(installed=("python",), server="current"))
    assert "Ensuring the local PostgreSQL cluster" in quiet and "restart postgresql" not in quiet, quiet


def test_a_dry_run_reports_a_stale_server_and_goes_on() -> None:
    """Audit, SYRD-828: a dry run installs nothing, so it cannot have made the server stale, and it must not die. It
    says what a real run would do."""
    for manager in ("pacman", "apt"):
        said = run_dry(manager, python_venv="/opt/switchyard/venv", host=host_with(installed=("python",), server="replaced"),
                       check=False)
        assert said.rstrip().endswith("exit 0"), (manager, said)
        assert "now installed" not in said, (manager, said)
        assert "Dry run: a real run would stop here" in said and "sudo systemctl restart postgresql" in said, (manager, said)
        assert "Ensuring the local PostgreSQL cluster" in said, (manager, said)


def test_a_check_that_cannot_run_is_said_and_the_install_goes_on() -> None:
    """Audit, SYRD-828: a host where the check itself fails -- no systemctl in a container, python3 not usable yet --
    must not be stopped by it; only a stale server stops an install."""
    for code in (2, 127):
        host = {**host_with(installed=("python",)),
                "SWITCHYARD_POSTGRES_REPLACED_CHECK": stand_in(f"pg-check-broken-{code}", f"echo 'cannot ask'; exit {code}\n")}
        said = run_dry("pacman", host=host, check=False)
        assert said.rstrip().endswith("exit 0"), (code, said)
        assert "Could not check whether the running PostgreSQL server needs a restart (cannot ask)" in said, (code, said)
        assert "restart postgresql" not in said and "would stop" not in said, (code, said)
        assert "Ensuring the local PostgreSQL cluster" in said, (code, said)


def test_the_default_check_is_postgres_health() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"$PYTHON_BIN" -I "$SCRIPT_DIR/postgres_health.py" --replaced' in source
    usage = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "postgres_health.py"), "--bogus"],
                           capture_output=True, text=True)
    assert usage.returncode == 2 and "--replaced" in usage.stderr, usage


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
    test_pacman_verification_follows_the_shared_venv_when_one_exists()
    test_no_supported_path_uses_user_pip_or_break_system_packages()
    test_pacman_path_installs_nothing_as_the_invoking_user_when_run_as_root()
    test_pacman_still_refuses_to_refresh_databases_itself()
    test_arch_system_python_satisfies_the_pin_it_replaces_pip_with()
    test_fresh_machine_docs_match_manual_agent_cli_policy()
    test_readme_states_the_user_owns_agent_cli_installation()
    test_prereqs_rejects_the_removed_cli_flags()
    test_package_sets_match_the_platform_python_strategy()
    test_system_site_venv_runs_ticket_board_entrypoint()
    test_an_installed_package_is_never_upgraded_by_pacman()
    test_an_installed_package_is_never_upgraded_by_apt()
    test_a_running_server_left_stale_stops_the_install()
    test_a_dry_run_reports_a_stale_server_and_goes_on()
    test_a_check_that_cannot_run_is_said_and_the_install_goes_on()
    test_the_default_check_is_postgres_health()
    print("install_switchyard_prereqs_test: ok")
