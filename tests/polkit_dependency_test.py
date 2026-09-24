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


class _Answers:
    """A runner that answers apt-cache and pkcheck as a given host would."""

    def __init__(self, *, apt_has_pkexec: bool = True, pkcheck: int | str = 2) -> None:
        self.apt_has_pkexec = apt_has_pkexec
        self.pkcheck = pkcheck
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["apt-cache", "show"]:
            return subprocess.CompletedProcess(args, 0 if self.apt_has_pkexec else 100)
        if Path(args[0]).name == "pkcheck":
            if self.pkcheck == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))
            stderr = "" if self.pkcheck in (0, 1, 2, 3) else (
                "Error checking for authorization org.freedesktop.policykit.exec: "
                "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown: The name "
                "org.freedesktop.PolicyKit1 was not provided by any .service files"
            )
            return subprocess.CompletedProcess(args, int(self.pkcheck), "", stderr)
        return subprocess.CompletedProcess(args, 0)


ARCH = {"pacman": "/usr/bin/pacman", "pkexec": "/usr/bin/pkexec", "pkcheck": "/usr/bin/pkcheck"}
DEBIAN = {"apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec", "pkcheck": "/usr/bin/pkcheck"}


def _run_remedy(command: str) -> list[str]:
    """Execute the printed remedy, verbatim, and report what reached the package manager."""
    import shlex

    argv = shlex.split(command)
    assert argv and argv[0] == "sudo", argv
    assert not any(ch in command for ch in "()"), f"prose in the remedy: {command}"
    with tempfile.TemporaryDirectory(prefix="syrd261-remedy.") as tmp:
        bin_dir = Path(tmp)
        log = bin_dir / "called"
        (bin_dir / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
        for manager in ("pacman", "apt-get"):
            (bin_dir / manager).write_text(
                f'#!/bin/sh\nprintf "%s\\n" "{manager}" "$@" > {log}\n', encoding="utf-8"
            )
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        subprocess.run(
            ["bash", "-c", command], check=True,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, capture_output=True, text=True,
        )
        return log.read_text(encoding="utf-8").split()


def test_readiness_names_what_is_missing_and_the_command_that_fixes_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd261-polkit.") as tmp:
        rules = Path(tmp) / "polkit-1" / "rules.d"
        arch = {"pacman": "/usr/bin/pacman"}
        problems = team_launcher.polkit_readiness_problems(rules_dir=rules, which=arch.get, runner=_Answers())
        assert len(problems) == 1, problems
        assert str(rules) in problems[0] and "pkexec is not on PATH" in problems[0], problems
        assert problems[0].endswith("\n    sudo pacman -S --needed polkit"), problems

        rules.mkdir(parents=True)
        assert team_launcher.polkit_readiness_problems(rules_dir=rules, which=ARCH.get, runner=_Answers()) == []


def test_every_printed_install_command_runs_and_installs_polkit() -> None:
    """Acceptance: an exact, copyable command -- so it is run, verbatim."""
    cases = (
        (ARCH, _Answers(), ["pacman", "-S", "--needed", "polkit"]),
        (DEBIAN, _Answers(apt_has_pkexec=True), ["apt-get", "install", "-y", "polkitd", "pkexec"]),
        (DEBIAN, _Answers(apt_has_pkexec=False), ["apt-get", "install", "-y", "policykit-1"]),
    )
    for tools, answers, expected in cases:
        command = team_launcher.polkit_install_command(which=tools.get, runner=answers)
        assert _run_remedy(command) == expected, (command, expected)


def test_the_apt_remedy_asks_the_archive_exactly_as_the_installer_does() -> None:
    answers = _Answers(apt_has_pkexec=False)
    team_launcher.polkit_install_command(which=DEBIAN.get, runner=answers)
    assert ["apt-cache", "show", "--no-all-versions", "pkexec"] in answers.calls, answers.calls
    installer = PREREQS.read_text(encoding="utf-8")
    assert "apt-cache show --no-all-versions pkexec" in installer


def test_a_host_with_no_known_manager_gets_no_fake_command() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd261-none.") as tmp:
        problems = team_launcher.polkit_readiness_problems(
            rules_dir=Path(tmp) / "missing", which={}.get, runner=_Answers()
        )
    assert "Install your distribution's polkit package" in problems[0], problems
    assert "sudo " not in problems[0], problems


def test_any_answer_from_the_authority_is_a_working_service() -> None:
    """Not authorized, or needing authentication, is still polkitd answering."""
    for code in (0, 1, 2, 3):
        assert team_launcher.polkit_service_problem(which=ARCH.get, runner=_Answers(pkcheck=code)) == "", code


def test_a_broken_service_is_refused_with_the_command_that_restarts_it() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd261-broken.") as tmp:
        rules = Path(tmp) / "rules.d"
        rules.mkdir()
        for answer in (127, "timeout"):
            problems = team_launcher.polkit_readiness_problems(
                rules_dir=rules, which=ARCH.get, runner=_Answers(pkcheck=answer)
            )
            assert len(problems) == 1, (answer, problems)
            assert "polkit is installed but not working" in problems[0], problems
            assert problems[0].endswith(
                "\n    sudo systemctl unmask polkit.service && sudo systemctl restart polkit.service"
            ), problems
        broken = team_launcher.polkit_readiness_problems(
            rules_dir=rules, which=ARCH.get, runner=_Answers(pkcheck=127)
        )[0]
        assert "was not provided by any .service files" in broken, broken
        no_client = {k: v for k, v in ARCH.items() if k != "pkcheck"}
        problems = team_launcher.polkit_readiness_problems(rules_dir=rules, which=no_client.get, runner=_Answers())
        assert "pkcheck" in problems[0], problems


def test_the_restart_remedy_runs_and_unmasks_before_it_restarts() -> None:
    """A masked unit is one way to be present and unusable; restart alone refuses it."""
    import shlex

    command = team_launcher.POLKIT_RESTART_COMMAND
    assert not any(ch in command for ch in "()"), command
    with tempfile.TemporaryDirectory(prefix="syrd261-restart.") as tmp:
        bin_dir = Path(tmp)
        log = bin_dir / "called"
        (bin_dir / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
        (bin_dir / "systemctl").write_text(f'#!/bin/sh\necho "$@" >> {log}\n', encoding="utf-8")
        for tool in bin_dir.iterdir():
            tool.chmod(0o755)
        subprocess.run(["bash", "-c", command], check=True, env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
        assert log.read_text(encoding="utf-8").splitlines() == [
            "unmask polkit.service", "restart polkit.service",
        ]
    assert shlex.split(command)[0] == "sudo"


def test_this_hosts_real_polkit_answers() -> None:
    """The shipped default, once, against a live authority: no stub in the way."""
    import shutil as _shutil

    if _shutil.which("pkcheck") is None or not team_launcher.POLKIT_RULES_DIR.is_dir():
        return
    assert team_launcher.polkit_service_problem() == ""


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


def test_switchyard_new_refuses_a_broken_polkit_before_creating_anything() -> None:
    class BrokenPolkit(FakeRunner):
        def __call__(self, args, **kwargs):
            if args and Path(str(args[0])).name == "pkcheck":
                self.calls.append(list(args))
                return subprocess.CompletedProcess(args, 127, "", "Error checking for authorization: no authority")
            return super().__call__(args, **kwargs)

    runner = BrokenPolkit()
    original = team_launcher.POLKIT_RULES_DIR
    with tempfile.TemporaryDirectory(prefix="syrd261-new-broken.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        output_dir = tmp_path / "out"
        rules = tmp_path / "polkit-1" / "rules.d"
        rules.mkdir(parents=True)
        team_launcher.POLKIT_RULES_DIR = rules
        try:
            switchyard_new_command(
                desktop_policy=Path("headless"), slug="porter", agent_name="otto-agent",
                project_name="Porter", project_path=tmp_path / "home" / "otto-agent" / "Projects" / "porter",
                source_repo=source_repo, output_dir=output_dir, role_clis=LEGACY_SWITCHYARD_ROLE_CLIS,
                yes=True, allow_existing_owner_user=True, home_base=tmp_path / "home",
                euid_getter=lambda: 0, runner=runner, input_func=lambda _prompt: "",
                port_in_use=lambda _port: False, socket_exists=lambda _path: False,
                session_record_timeout=0, registry_dir=tmp_path / "registry",
            )
        except SystemExit as exc:
            said = str(exc)
        else:
            raise AssertionError("a host whose polkit cannot answer was provisioned")
        finally:
            team_launcher.POLKIT_RULES_DIR = original
        assert not output_dir.exists()
    import shutil as _shutil

    if _shutil.which("pkexec") is None or _shutil.which("pkcheck") is None:
        return  # this host has no polkit at all; the presence refusal covers it
    assert "polkit is installed but not working" in said, said
    assert any(Path(str(call[0])).name == "pkcheck" for call in runner.calls), runner.calls
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
