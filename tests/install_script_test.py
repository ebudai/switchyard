#!/usr/bin/env python3
"""Regression checks for the one-command ./install wrapper.

install-switchyard marker for host-automation suite discovery.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install"
CLIS = ("claude", "codex", "agy", "hermes")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _env(tmpdir: Path) -> dict[str, str]:
    log = tmpdir / "commands.log"
    prereqs = tmpdir / "fake-prereqs"
    release = tmpdir / "fake-install-switchyard"
    bin_dir = tmpdir / "bin"
    bin_dir.mkdir()
    _write_executable(
        prereqs,
        "#!/usr/bin/env bash\nprintf 'prereqs:%s\\n' \"$*\" >>\"$SWITCHYARD_TEST_LOG\"\nprintf 'fake prereqs\\n'\n",
    )
    _write_executable(
        release,
        "#!/usr/bin/env bash\nprintf 'release:%s\\n' \"$*\" >>\"$SWITCHYARD_TEST_LOG\"\nprintf 'fake release install\\n'\n",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "SWITCHYARD_INSTALL_TEST_ASSUME_ROOT": "1",
        "SWITCHYARD_INSTALL_FORCE_MISSING_CLIS": "1",
        "SWITCHYARD_INSTALL_PREREQS_SCRIPT": str(prereqs),
        "SWITCHYARD_INSTALL_SWITCHYARD_SCRIPT": str(release),
        "SWITCHYARD_SHARED_INSTALL_ROOT": str(tmpdir / "opt" / "switchyard"),
        "SWITCHYARD_INSTALL_PATH": str(tmpdir / "usr" / "local" / "bin" / "switchyard"),
        "SWITCHYARD_TEST_LOG": str(log),
    }
    for cli in CLIS:
        env[f"SWITCHYARD_{cli.upper()}_INSTALL_COMMAND"] = (
            f"printf {cli} >> {tmpdir / f'{cli}.installed'}"
        )
    return env


def _run_install(
    tmpdir: Path,
    *args: str,
    input_text: str | None = None,
    assume_tty: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = _env(tmpdir)
    if assume_tty:
        env["SWITCHYARD_INSTALL_ASSUME_TTY"] = "1"
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )


def _run_install_dry_run_as_invoking_user(tmpdir: Path) -> subprocess.CompletedProcess[str]:
    env = _env(tmpdir)
    env.pop("SWITCHYARD_INSTALL_TEST_ASSUME_ROOT", None)
    env["SWITCHYARD_INSTALL_ORIGINAL_USER"] = "alice"
    env["SWITCHYARD_INSTALL_ORIGINAL_HOME"] = str(tmpdir / "home" / "alice")
    return subprocess.run(
        [str(SCRIPT), "--dry-run"],
        cwd=ROOT,
        env=env,
        input="n\n",
        text=True,
        capture_output=True,
        check=True,
    )


def test_yes_installs_every_missing_cli_and_prints_final_auth_step_last() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, "--yes")
        output = proc.stdout

        for cli in CLIS:
            assert (tmpdir / f"{cli}.installed").read_text(encoding="utf-8") == cli
            assert f"Install {cli}?" in output
        assert output.count("[Y/n] y") == len(CLIS)
        assert "+ sh -lc printf\\ claude\\ \\>\\>" in output
        assert "fake prereqs" in output
        assert "fake release install" in output
        assert output.rstrip().splitlines()[-6:] == [
            "NEXT STEP",
            "Sign in to each installed agent CLI:",
            "  claude   -> run `claude`",
            "  codex    -> run `codex`",
            "  agy      -> run `agy`",
            "  hermes   -> run `hermes login`",
        ]
        assert (tmpdir / "commands.log").read_text(encoding="utf-8").splitlines() == [
            "prereqs:--skip-cli",
            "release:--apply",
        ]


def test_cli_selection_installs_selected_cli_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, "--cli", "codex", input_text="y\n", assume_tty=True)
        output = proc.stdout

        assert not (tmpdir / "claude.installed").exists()
        assert (tmpdir / "codex.installed").read_text(encoding="utf-8") == "codex"
        assert not (tmpdir / "agy.installed").exists()
        assert not (tmpdir / "hermes.installed").exists()
        assert "Install claude?" not in output
        assert "Install codex?" in output
        assert output.rstrip().splitlines()[-2:] == ["NEXT STEP", "Run `codex` and sign in."]


def test_multiple_installed_clis_final_step_lists_choices_instead_of_first_cli() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, input_text="y\ny\nn\nn\n", assume_tty=True)
        output = proc.stdout

        assert (tmpdir / "claude.installed").read_text(encoding="utf-8") == "claude"
        assert (tmpdir / "codex.installed").read_text(encoding="utf-8") == "codex"
        assert not (tmpdir / "agy.installed").exists()
        assert not (tmpdir / "hermes.installed").exists()
        assert output.rstrip().splitlines()[-4:] == [
            "NEXT STEP",
            "Sign in to each installed agent CLI:",
            "  claude   -> run `claude`",
            "  codex    -> run `codex`",
        ]


def test_cli_selection_final_step_names_selected_cli_even_with_earlier_cli_present() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        env = _env(tmpdir)
        env.pop("SWITCHYARD_INSTALL_FORCE_MISSING_CLIS", None)
        _write_executable(tmpdir / "bin" / "claude", "#!/usr/bin/env bash\nexit 0\n")
        env["PATH"] = f"{tmpdir / 'bin'}{os.pathsep}/bin"
        env["SWITCHYARD_INSTALL_ASSUME_TTY"] = "1"

        proc = subprocess.run(
            [str(SCRIPT), "--cli", "hermes"],
            cwd=ROOT,
            env=env,
            input="y\n",
            text=True,
            capture_output=True,
            check=True,
        )
        output = proc.stdout

        assert "Install claude?" not in output
        assert "Install hermes?" in output
        assert (tmpdir / "hermes.installed").read_text(encoding="utf-8") == "hermes"
        assert output.rstrip().splitlines()[-2:] == ["NEXT STEP", "Run `hermes login` and sign in."]


def test_dry_run_renders_real_cli_privilege_wrapper() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install_dry_run_as_invoking_user(tmpdir)
        output = proc.stdout

        for cli in CLIS:
            assert f"Install {cli}?" in output
        assert "would ask: [Y/n]" in output
        assert "sudo -u alice -H sh -lc" in output
        assert f"printf\\ claude\\ \\>\\>\\ {tmpdir / 'claude.installed'}" in output
        assert not list(tmpdir.glob("*.installed"))


def test_no_cli_skips_prompts_and_finishes_with_manual_cli_action() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, "--no-cli")
        output = proc.stdout

        assert "Agent CLI installation skipped by --no-cli." in output
        assert "Install claude?" not in output
        assert not list(tmpdir.glob("*.installed"))
        assert output.rstrip().splitlines()[-2:] == [
            "NEXT STEP",
            "CLI installation was skipped; install one agent CLI, then run it and sign in.",
        ]


def test_non_tty_without_yes_skips_cli_instead_of_blocking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir)
        output = proc.stdout

        assert "Agent CLI installation skipped: stdin/stdout is not a TTY." in output
        assert (
            "Re-run sudo ./install --yes --cli claude to install only Claude, "
            "run sudo ./install --yes to install all four CLI installers without prompting"
        ) in output
        assert "Install claude?" not in output
        assert not list(tmpdir.glob("*.installed"))
        assert output.rstrip().splitlines()[-2:] == [
            "NEXT STEP",
            "CLI installation was skipped; re-run `sudo ./install --yes --cli claude`, or install one agent CLI manually and sign in.",
        ]


def test_dry_run_prints_every_step_without_prompting_or_mutating() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, "--dry-run", input_text="n\nn\nn\nn\n", assume_tty=True)
        output = proc.stdout

        assert "DRY RUN: no changes will be made." in output
        assert "fake prereqs" in output
        assert "fake release install" in output
        for cli in CLIS:
            assert f"Install {cli}?" in output
            assert f"printf\\ {cli}\\ \\>\\>\\ {tmpdir / f'{cli}.installed'}" in output
        assert output.count("would ask: [Y/n]") == len(CLIS)
        assert "\n[Y/n]\n" not in output
        assert "NEXT STEP" not in output
        assert not list(tmpdir.glob("*.installed"))
        assert (tmpdir / "commands.log").read_text(encoding="utf-8").splitlines() == [
            "prereqs:--dry-run --skip-cli",
            "release:--print-commands",
        ]


def test_interactive_prompts_all_clis_and_accepts_invalid_retry_and_eof_default_yes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, input_text="n\nmaybe\ny\n", assume_tty=True)
        output = proc.stdout

        assert not (tmpdir / "claude.installed").exists()
        assert (tmpdir / "codex.installed").read_text(encoding="utf-8") == "codex"
        assert (tmpdir / "agy.installed").read_text(encoding="utf-8") == "agy"
        assert (tmpdir / "hermes.installed").read_text(encoding="utf-8") == "hermes"
        assert "Please answer y or n." in output
        assert output.rstrip().splitlines()[-5:] == [
            "NEXT STEP",
            "Sign in to each installed agent CLI:",
            "  codex    -> run `codex`",
            "  agy      -> run `agy`",
            "  hermes   -> run `hermes login`",
        ]


def test_declining_every_cli_still_succeeds_and_installs_switchyard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        proc = _run_install(tmpdir, input_text="n\nn\nn\nn\n", assume_tty=True)
        output = proc.stdout

        assert not list(tmpdir.glob("*.installed"))
        assert "fake release install" in output
        assert output.rstrip().splitlines()[-2:] == [
            "NEXT STEP",
            "No CLI was installed; install one agent CLI, then run it and sign in.",
        ]


def test_already_installed_cli_is_not_prompted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        env = _env(tmpdir)
        env.pop("SWITCHYARD_INSTALL_FORCE_MISSING_CLIS", None)
        _write_executable(tmpdir / "bin" / "claude", "#!/usr/bin/env bash\nexit 0\n")
        env["SWITCHYARD_INSTALL_ASSUME_TTY"] = "1"

        proc = subprocess.run(
            [str(SCRIPT), "--cli", "claude"],
            cwd=ROOT,
            env=env,
            input="",
            text=True,
            capture_output=True,
            check=True,
        )
        output = proc.stdout

        assert "claude is already installed; skipping installer." in output
        assert "Install claude?" not in output
        assert "Install codex?" not in output
        assert "Install agy?" not in output
        assert "Install hermes?" not in output
        assert output.rstrip().splitlines()[-2:] == ["NEXT STEP", "Run `claude` and sign in."]


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
            [str(SCRIPT), "--yes", "--no-cli"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        sudo_args = log.read_text(encoding="utf-8")
        assert "--yes --no-cli" in sudo_args


if __name__ == "__main__":
    test_yes_installs_every_missing_cli_and_prints_final_auth_step_last()
    test_cli_selection_installs_selected_cli_only()
    test_multiple_installed_clis_final_step_lists_choices_instead_of_first_cli()
    test_cli_selection_final_step_names_selected_cli_even_with_earlier_cli_present()
    test_dry_run_renders_real_cli_privilege_wrapper()
    test_no_cli_skips_prompts_and_finishes_with_manual_cli_action()
    test_non_tty_without_yes_skips_cli_instead_of_blocking()
    test_dry_run_prints_every_step_without_prompting_or_mutating()
    test_interactive_prompts_all_clis_and_accepts_invalid_retry_and_eof_default_yes()
    test_declining_every_cli_still_succeeds_and_installs_switchyard()
    test_already_installed_cli_is_not_prompted()
    test_non_root_self_elevation_preserves_original_flags()
    print("install_script_test: ok")
