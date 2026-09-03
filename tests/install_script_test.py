#!/usr/bin/env python3
"""Regression checks for the one-command ./install wrapper.

install-switchyard marker for host-automation suite discovery.

Switchyard does not install agent CLIs. Several checks below are structural:
they read the installer sources and prove the code *cannot* install a CLI,
rather than observing that one particular run did not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install"
PREREQS = ROOT / "scripts" / "install-switchyard-prereqs"
CLIS = ("claude", "codex", "agy", "hermes")
# Binaries the installer itself shells out to. The sandbox PATH is built from
# exactly this list so that no agent CLI can be resolved during a test run.
SANDBOX_BINARIES = ("bash", "sh", "env", "id", "getent", "cut", "dirname")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _sandbox_path(tmpdir: Path) -> Path:
    """A PATH directory holding the installer's own tools and no agent CLI."""
    bin_dir = tmpdir / "sandbox-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in SANDBOX_BINARIES:
        resolved = shutil.which(name)
        assert resolved, f"test host is missing {name}"
        target = bin_dir / name
        if not target.exists():
            target.symlink_to(resolved)
    return bin_dir


def _env(tmpdir: Path) -> dict[str, str]:
    log = tmpdir / "commands.log"
    prereqs = tmpdir / "fake-prereqs"
    release = tmpdir / "fake-install-switchyard"
    bin_dir = tmpdir / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(
        prereqs,
        "#!/usr/bin/env bash\nprintf 'prereqs:%s\\n' \"$*\" >>\"$SWITCHYARD_TEST_LOG\"\nprintf 'fake prereqs\\n'\n",
    )
    _write_executable(
        release,
        "#!/usr/bin/env bash\nprintf 'release:%s\\n' \"$*\" >>\"$SWITCHYARD_TEST_LOG\"\nprintf 'fake release install\\n'\n",
    )
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "SWITCHYARD_INSTALL_TEST_ASSUME_ROOT": "1",
        "SWITCHYARD_INSTALL_PREREQS_SCRIPT": str(prereqs),
        "SWITCHYARD_INSTALL_SWITCHYARD_SCRIPT": str(release),
        "SWITCHYARD_SHARED_INSTALL_ROOT": str(tmpdir / "opt" / "switchyard"),
        "SWITCHYARD_INSTALL_PATH": str(tmpdir / "usr" / "local" / "bin" / "switchyard"),
        "SWITCHYARD_TEST_LOG": str(log),
    }


def _run_install(
    tmpdir: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        env=_env(tmpdir),
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def _usage_heredoc_bounds(lines: list[str]) -> tuple[int, int]:
    start = next(i for i, line in enumerate(lines) if line.strip() == "cat <<'EOF'")
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "EOF")
    return start, end


def _cli_name_lines(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = []
    heredoc_start, heredoc_end = _usage_heredoc_bounds(lines)
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(cli in lowered for cli in CLIS):
            continue
        hits.append((index, line))
    return hits


def _line_is_inert(path: Path, index: int, line: str) -> bool:
    """True when a line naming a CLI is text, not an executed command."""
    lines = path.read_text(encoding="utf-8").splitlines()
    heredoc_start, heredoc_end = _usage_heredoc_bounds(lines)
    if heredoc_start < index < heredoc_end:
        return True
    stripped = line.strip()
    return stripped.startswith("#") or stripped.startswith("printf ")


def test_installer_sources_cannot_install_an_agent_cli() -> None:
    """Structural proof for both files, not an observation of one run."""
    for path in (SCRIPT, PREREQS):
        text = path.read_text(encoding="utf-8")

        # No vendor installer is named, fetched, or piped to a shell.
        assert "install.sh" not in text
        assert "| bash" not in text
        assert "| sh" not in text
        assert "curl -fsSL" not in text

        # None of the removed CLI-install machinery survives.
        for token in (
            "CLI_INSTALL_COMMANDS",
            "CLI_UNATTENDED_INSTALL_COMMANDS",
            "CLI_AUTH_COMMANDS",
            "CLI_ORDER",
            "install_agent_clis",
            "dry_run_agent_clis",
            "prompt_cli_install",
            "run_cli_install_command",
            "--no-cli",
            "--skip-cli",
        ):
            assert token not in text, f"{path.name} still references {token}"

        # Every surviving mention of a CLI name is inert text.
        for index, line in _cli_name_lines(path):
            assert _line_is_inert(path, index, line), (
                f"{path.name}:{index + 1} names an agent CLI outside a printed "
                f"string or comment: {line!r}"
            )


def test_install_succeeds_on_a_host_with_no_agent_cli_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        env = _env(tmpdir)
        sandbox = _sandbox_path(tmpdir)
        env["PATH"] = str(sandbox)

        for cli in CLIS:
            assert shutil.which(cli, path=str(sandbox)) is None

        proc = subprocess.run(
            [str(SCRIPT)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

        assert proc.returncode == 0, proc.stderr
        assert "fake prereqs" in proc.stdout
        assert "fake release install" in proc.stdout
        assert (tmpdir / "commands.log").read_text(encoding="utf-8").splitlines() == [
            "prereqs:",
            "release:--apply",
        ]


def test_final_next_step_points_at_user_owned_cli_install_and_project_auth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir)

        assert proc.stdout.rstrip().splitlines()[-3:] == [
            "NEXT STEP",
            "Switchyard does not install agent CLIs. Install at least one of claude, codex, agy, or hermes yourself, with that vendor's own installer, for the user that will own the project.",
            "Then run `switchyard new`: it detects which agent CLIs are installed and walks you through signing in to each one before any pane starts.",
        ]


def test_install_never_prompts_even_with_stdin_available() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, input_text="n\nn\nn\nn\n")
        output = proc.stdout

        assert "[Y/n]" not in output
        for cli in CLIS:
            assert f"Install {cli}?" not in output


def test_yes_is_accepted_and_changes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        plain = _run_install(tmpdir, input_text=None).stdout
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        with_yes = _run_install(tmpdir, "--yes", input_text=None).stdout
        short = _run_install(tmpdir, "-y", input_text=None).stdout

    def _scrub(text: str) -> str:
        return "\n".join(line for line in text.splitlines() if "/tmp" not in line)

    assert _scrub(plain) == _scrub(with_yes) == _scrub(short)


def test_removed_cli_flags_are_rejected_rather_than_silently_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for flag in ("--cli", "--cli=claude", "--no-cli", "--skip-cli"):
            proc = _run_install(tmpdir, flag, check=False)
            assert proc.returncode != 0, flag
            assert f"unknown argument: {flag}" in proc.stderr


def test_help_advertises_no_dead_cli_flags() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        output = _run_install(tmpdir, "--help").stdout

        assert "Usage: sudo ./install [--dry-run] [--yes|-y]" in output
        assert "--cli" not in output
        assert "--no-cli" not in output
        assert "--skip-cli" not in output
        assert "Switchyard does not install, download, or upgrade agent CLIs." in output


def test_dry_run_prints_every_step_without_prompting_or_mutating() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, "--dry-run", input_text="n\nn\nn\nn\n")
        output = proc.stdout

        assert "DRY RUN: no changes will be made." in output
        assert "fake prereqs" in output
        assert "fake release install" in output
        assert "[Y/n]" not in output
        assert "NEXT STEP" not in output
        assert not list(tmpdir.glob("*.installed"))
        assert (tmpdir / "commands.log").read_text(encoding="utf-8").splitlines() == [
            "prereqs:--dry-run",
            "release:--print-commands",
        ]


def test_dry_run_as_invoking_user_does_not_require_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        env = _env(tmpdir)
        env.pop("SWITCHYARD_INSTALL_TEST_ASSUME_ROOT", None)
        env["SWITCHYARD_INSTALL_ORIGINAL_USER"] = "alice"
        env["SWITCHYARD_INSTALL_ORIGINAL_HOME"] = str(tmpdir / "home" / "alice")

        proc = subprocess.run(
            [str(SCRIPT), "--dry-run"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        output = proc.stdout

        assert "DRY RUN: no changes will be made." in output
        assert "SWITCHYARD_INSTALL_ORIGINAL_USER=alice" in output
        assert "sudo" not in output


def test_non_root_self_elevation_preserves_original_flags() -> None:
    if os.geteuid() == 0:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        fake_sudo = tmpdir / "sudo"
        log = tmpdir / "sudo.log"
        _write_executable(
            fake_sudo,
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >\"$SWITCHYARD_TEST_LOG\"\n",
        )
        env = {
            **os.environ,
            "SWITCHYARD_SUDO_BIN": str(fake_sudo),
            "SWITCHYARD_TEST_LOG": str(log),
        }
        subprocess.run(
            [str(SCRIPT), "--yes"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        sudo_args = log.read_text(encoding="utf-8")
        assert "--yes" in sudo_args


if __name__ == "__main__":
    test_installer_sources_cannot_install_an_agent_cli()
    test_install_succeeds_on_a_host_with_no_agent_cli_present()
    test_final_next_step_points_at_user_owned_cli_install_and_project_auth()
    test_install_never_prompts_even_with_stdin_available()
    test_yes_is_accepted_and_changes_nothing()
    test_removed_cli_flags_are_rejected_rather_than_silently_ignored()
    test_help_advertises_no_dead_cli_flags()
    test_dry_run_prints_every_step_without_prompting_or_mutating()
    test_dry_run_as_invoking_user_does_not_require_root()
    test_non_root_self_elevation_preserves_original_flags()
    print("install_script_test: ok")
