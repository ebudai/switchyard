#!/usr/bin/env python3
"""polkit is a dependency, a host without it is refused first, and a stranded run finishes.

Found measuring SYRD-248 on a minimal Arch VM. `./install` never installed
polkit, so `switchyard new` created the accounts, the project and the board's
units, then failed installing the board's deploy rule:

    install: cannot create regular file '/etc/polkit-1/rules.d/49-meas-ticket-board-deploy.rules'

and running it again was refused, because a unit was now installed with no
database beside it -- a tenant stranded with no supported way forward
(SYRD-261).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from team_launcher_test_helpers import *  # noqa: F401,F403
from team_launcher_project_precheck_test import _write_exported_switchyard_release

PREREQS = ROOT / "scripts" / "install-switchyard-prereqs"


def _dry_run(manager: str, *, apt_has_pkexec: bool | None = None) -> str:
    """The real installer, dry, with the archive answering as asked."""
    with tempfile.TemporaryDirectory(prefix="syrd261-apt.") as tmp:
        bin_dir = Path(tmp)
        if apt_has_pkexec is not None:
            stub = bin_dir / "apt-cache"
            stub.write_text(
                "#!/bin/sh\n"
                '[ "$1" = show ] && [ "$3" = pkexec ] && exit '
                + ("0" if apt_has_pkexec else "100")
                + "\nexit 1\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
        proc = subprocess.run(
            [str(PREREQS), "--dry-run"],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "SWITCHYARD_PREREQS_ASSUME_MANAGER": manager,
                "SWITCHYARD_SUDO_BIN": "sudo",
                "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1",
                "SWITCHYARD_PYTHON_BIN": "python3",
                "SWITCHYARD_PYTHON_VENV": "/nonexistent/switchyard-venv",
            },
            check=True,
            text=True,
            capture_output=True,
        )
    return proc.stdout


def _install_line(output: str, prefix: str) -> list[str]:
    line = next(line for line in output.splitlines() if line.startswith(prefix))
    return line.removeprefix(prefix).split()


def test_arch_installs_polkit() -> None:
    packages = _install_line(_dry_run("pacman"), "+ sudo pacman -S --needed ")
    assert "polkit" in packages, packages


def test_current_debian_and_ubuntu_install_polkitd_and_pkexec() -> None:
    packages = _install_line(_dry_run("apt", apt_has_pkexec=True), "+ sudo apt-get install -y ")
    assert {"polkitd", "pkexec"} <= set(packages), packages
    assert "policykit-1" not in packages, packages


def test_older_releases_get_the_package_they_actually_have() -> None:
    """Naming polkitd where it does not exist would abort the whole install."""
    packages = _install_line(_dry_run("apt", apt_has_pkexec=False), "+ sudo apt-get install -y ")
    assert "policykit-1" in packages, packages
    assert not {"polkitd", "pkexec"} & set(packages), packages


def test_the_confirmation_prompt_is_not_silently_removed() -> None:
    """--noconfirm is an operator-consent decision, not part of this fix."""
    assert "--noconfirm" not in PREREQS.read_text(encoding="utf-8")
    assert "--noconfirm" not in _dry_run("pacman")


def test_readiness_names_what_is_missing_and_the_command_that_fixes_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd261-polkit.") as tmp:
        rules = Path(tmp) / "polkit-1" / "rules.d"
        arch = {"pacman": "/usr/bin/pacman"}
        problems = team_launcher.polkit_readiness_problems(rules_dir=rules, which=arch.get)
        assert len(problems) == 1, problems
        assert str(rules) in problems[0] and "pkexec is not on PATH" in problems[0], problems
        assert "sudo pacman -S --needed polkit" in problems[0], problems

        rules.mkdir(parents=True)
        present = {"pacman": "/usr/bin/pacman", "pkexec": "/usr/bin/pkexec"}
        assert team_launcher.polkit_readiness_problems(rules_dir=rules, which=present.get) == []

        debian = {"apt-get": "/usr/bin/apt-get"}
        problems = team_launcher.polkit_readiness_problems(rules_dir=rules, which=debian.get)
        assert "pkexec is not on PATH" in problems[0] and str(rules) not in problems[0], problems
        assert "sudo apt-get install -y polkitd pkexec" in problems[0], problems


def test_switchyard_new_refuses_before_creating_anything() -> None:
    """The live failure point came after accounts and units; the refusal is before them."""
    runner = FakeRunner()
    original = team_launcher.POLKIT_RULES_DIR
    with tempfile.TemporaryDirectory(prefix="syrd261-new.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        output_dir = tmp_path / "out"
        team_launcher.POLKIT_RULES_DIR = tmp_path / "no-polkit" / "rules.d"
        try:
            switchyard_new_command(
                desktop_policy=Path("headless"),
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=tmp_path / "home" / "otto-agent" / "Projects" / "porter",
                source_repo=source_repo,
                output_dir=output_dir,
                role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True,
                allow_existing_owner_user=True,
                home_base=tmp_path / "home",
                euid_getter=lambda: 0,
                runner=runner,
                input_func=lambda _prompt: "",
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
                session_record_timeout=0,
                registry_dir=tmp_path / "registry",
            )
        except SystemExit as exc:
            said = str(exc)
        else:
            raise AssertionError("a host without polkit was provisioned")
        finally:
            team_launcher.POLKIT_RULES_DIR = original
        assert not output_dir.exists()
    assert "polkit is not installed" in said, said
    created = [call for call in runner.calls if call[:1] in (["useradd"], ["install"], ["bash"], ["sudo"])]
    assert created == [], created


class _StrandedRunner(FakeRunner):
    """The host after the live failure: the unit is installed, the database is not."""

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
            return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service disabled\n")
        if args[:2] == ["psql", "-XAt"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="")
        return subprocess.CompletedProcess(args, 0)


def _stranded(unit_text_edit=None) -> tuple[int | None, str]:
    """Run the provisioning twice: once to render, once over its own stranded unit."""
    current_user = team_launcher.current_user_name()
    original_units = team_launcher.SYSTEMD_UNIT_DIR
    with tempfile.TemporaryDirectory(prefix="syrd261-resume.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        rendered = tmp_path / "rendered"
        # First, what this provisioning installs.
        new_project_command(
            "porter", owner_user=current_user, source_repo=source_repo, repository=project_repo,
            output_dir=rendered, runner=FakeRunner(), port_in_use=lambda _port: False,
            socket_exists=lambda _path: False,
        )
        unit = (rendered / "porter-ticket-board.service").read_text(encoding="utf-8")
        installed = tmp_path / "systemd"
        installed.mkdir()
        (installed / "porter-ticket-board.service").write_text(
            unit_text_edit(unit) if unit_text_edit else unit, encoding="utf-8"
        )
        team_launcher.SYSTEMD_UNIT_DIR = installed
        try:
            result = new_project_command(
                "porter", owner_user=current_user, source_repo=source_repo, repository=project_repo,
                output_dir=tmp_path / "out", runner=_StrandedRunner(), port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            return result, ""
        except SystemExit as exc:
            return None, str(exc)
        finally:
            team_launcher.SYSTEMD_UNIT_DIR = original_units


def test_a_stranded_provisioning_is_finished_by_running_it_again() -> None:
    result, said = _stranded()
    assert result == 0, said


def test_a_unit_that_is_not_this_provisionings_stays_refused_with_a_way_forward() -> None:
    result, said = _stranded(lambda unit: unit.replace("Restart=on-failure", "Restart=always"))
    assert result is None, "a unit this provisioning did not write was built over"
    assert "porter-ticket-board.service is installed but database 'porter_ticket_board' does not exist" in said, said
    assert "switchyard teardown porter --dry-run" in said, said


def main() -> int:
    from standalone_test_runner import run_module_tests

    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"polkit_dependency_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
