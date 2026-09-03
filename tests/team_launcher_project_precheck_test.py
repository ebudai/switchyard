#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

import re

from team_launcher_test_helpers import *

def _write_exported_switchyard_release(source_repo: Path, *, commit: str = REMOTE_HEAD) -> None:
    source_repo.mkdir(parents=True, exist_ok=True)
    (source_repo / team_launcher.SWITCHYARD_RELEASE_MARKER_NAME).write_text(
        json.dumps({"commit": commit}) + "\n",
        encoding="utf-8",
    )
    (source_repo / "switchyard").write_text("#!/bin/sh\n", encoding="utf-8")
    scripts_dir = source_repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / team_launcher.TEAM_LAUNCHER_NAME).write_text("#!/bin/sh\n", encoding="utf-8")
    (scripts_dir / "ticket-board-service.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (scripts_dir / "ticket-board-boardsvc-setup.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")


def test_project_artifact_rejects_stage_configuration() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-design.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        source_repo = tmp_path / "source-repo"
        project_repo.mkdir()
        source_repo.mkdir()
        artifact_path = tmp_path / "bad.project.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "schema": "switchyard.project.v1",
                    "design_document": "bad-design.md",
                    "project": {
                        "slug": "bad",
                        "ticket_prefix": "BAD",
                        "owner_user": team_launcher.current_user_name(),
                        "repository": str(project_repo),
                        "stages": ["analysis", "implementation"],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            new_project_command(
                "bad",
                from_artifact=artifact_path,
                source_repo=source_repo,
                output_dir=tmp_path / "out",
                runner=FakeRunner(),
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected artifact stage-shaping rejection")
        except SystemExit as exc:
            message = str(exc)

    assert "must not preconfigure stages or roles" in message
    assert "project.stages" in message

def test_new_project_requires_project_repository() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "source-repo",
                output_dir=output_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected missing repository failure")
        except SystemExit as exc:
            message = str(exc)

    assert "--repository" in message
    assert not output_dir.exists()
    assert not runner.calls

def test_new_project_precheck_fails_when_project_repository_is_absent() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=tmp_path / "source-repo",
                repository=tmp_path / "missing-project-repo",
                output_dir=output_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected absent repository precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "project repository" in message
    assert "does not exist" in message
    assert not output_dir.exists()
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)

def test_new_project_precheck_fails_before_sudo_when_repo_is_dirty() -> None:
    current_user = team_launcher.current_user_name()

    class DirtyRepoRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout=" M scripts/team_launcher.py\n")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = DirtyRepoRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected dirty repo precheck failure")
        except SystemExit as exc:
            message = str(exc)
        assert not output_dir.exists()

    assert "deploy checkout" in message
    assert "uncommitted changes" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)

def test_new_project_accepts_exported_switchyard_release_without_git_status() -> None:
    current_user = team_launcher.current_user_name()

    class NoGitStatusRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                raise AssertionError("exported releases must not run git status")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-release-source.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "release"
        _write_exported_switchyard_release(source_repo)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = NoGitStatusRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        assert (output_dir / "plan.json").exists()

    assert not any(call[:1] == ["sudo"] for call in runner.calls)

def test_switchyard_new_accepts_exported_switchyard_release_without_git_status() -> None:
    class ReleaseInstallRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                raise AssertionError("switchyard new must not dirty-check exported releases")
            if args[:2] == ["id", "-u"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"] and args[-1]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-release-source.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        project_dir = tmp_path / "home" / "otto-agent" / "Projects" / "porter"
        output_dir = tmp_path / "out"
        runner = ReleaseInstallRunner()

        assert (
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=project_dir,
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
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            == 0
        )
        assert (output_dir / "plan.json").exists()


def test_switchyard_new_creates_missing_board_service_user_before_owner_mutation() -> None:
    class MissingServiceUserRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["getent", "passwd", "boardsvc"]:
                return subprocess.CompletedProcess(args, 2)
            if len(args) >= 2 and Path(args[-2]).name == "ticket-board-boardsvc-setup.sh" and args[-1] == "--apply-peer-auth":
                return subprocess.CompletedProcess(args, 0)
            if args == ["id", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args == ["loginctl", "enable-linger", "otto-agent"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            if args == ["sudo", "-v"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["bash"]:
                commands = Path(args[-1]).read_text(encoding="utf-8")
                assert "if ! getent passwd 'boardsvc' >/dev/null 2>&1; then" in commands
                assert "sudo useradd -r -M -d /nonexistent -s \"$service_shell\" 'boardsvc'" in commands
                assert commands.index("if ! getent passwd 'boardsvc'") < commands.index("setfacl -R -m u:boardsvc:rx")
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-no-boardsvc.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        project_dir = tmp_path / "home" / "otto-agent" / "Projects" / "porter"
        output_dir = tmp_path / "out"
        runner = MissingServiceUserRunner()

        assert (
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=project_dir,
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
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            == 0
        )

    service_useradd = [
        "useradd",
        "-r",
        "-M",
        "-d",
        "/nonexistent",
        "-s",
        team_launcher._non_login_shell_path(),
        "boardsvc",
    ]
    owner_useradd = next(call for call in runner.calls if call[:1] == ["useradd"] and call[-1] == "otto-agent")
    peer_auth_call = next(call for call in runner.calls if call[-1:] == ["--apply-peer-auth"])
    assert service_useradd in runner.calls
    assert runner.calls.index(service_useradd) < runner.calls.index(["id", "-u", "otto-agent"])
    assert runner.calls.index(peer_auth_call) < runner.calls.index(["id", "-u", "otto-agent"])
    assert runner.calls.index(service_useradd) < runner.calls.index(owner_useradd)


def test_switchyard_new_installs_board_peer_auth_before_owner_mutation() -> None:
    class PeerAuthRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.calls_with_kwargs: list[tuple[list[str], dict[str, object]]] = []

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls_with_kwargs.append((args, dict(kwargs)))
            self.calls.append(args)
            if args == ["getent", "passwd", "boardsvc"]:
                return subprocess.CompletedProcess(args, 0)
            if args[-1:] == ["--apply-peer-auth"]:
                return subprocess.CompletedProcess(args, 0)
            if args == ["id", "-u", "otto-agent"]:
                return subprocess.CompletedProcess(args, 1)
            if args[:1] == ["useradd"]:
                return subprocess.CompletedProcess(args, 0)
            if args == ["loginctl", "enable-linger", "otto-agent"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["install"]:
                target = Path(args[-1])
                if str(target).startswith(tempfile.gettempdir()):
                    target.mkdir(parents=True, exist_ok=True)
                return subprocess.CompletedProcess(args, 0)
            if args == ["sudo", "-v"]:
                return subprocess.CompletedProcess(args, 0)
            if args[:1] == ["bash"]:
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-peer-auth.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        runner = PeerAuthRunner()

        assert (
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=tmp_path / "home" / "otto-agent" / "Projects" / "porter",
                source_repo=source_repo,
                output_dir=tmp_path / "out",
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
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            == 0
        )

    setup_script = str(source_repo / "scripts" / "ticket-board-boardsvc-setup.sh")
    peer_auth_call = [setup_script, "--apply-peer-auth"]
    owner_lookup = ["id", "-u", "otto-agent"]
    assert peer_auth_call in runner.calls
    assert runner.calls.index(peer_auth_call) < runner.calls.index(owner_lookup)
    kwargs = next(kwargs for call, kwargs in runner.calls_with_kwargs if call == peer_auth_call)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["PG_DATABASE"] == "porter_ticket_board"
    assert env["PG_IDENT_MAP"] == "pgu_ticket_board_service"
    assert env["SERVICE_USER"] == "boardsvc"
    assert env["SERVICE_ROLE"] == "ticket_board_service"


def test_switchyard_new_missing_board_service_user_failure_names_remedy_before_owner_mutation() -> None:
    class FailingServiceUserRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["getent", "passwd", "boardsvc"]:
                return subprocess.CompletedProcess(args, 2)
            if args[:1] == ["useradd"] and args[-1] == "boardsvc":
                return subprocess.CompletedProcess(args, 1)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-boardsvc-fail.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        project_dir = tmp_path / "home" / "otto-agent" / "Projects" / "porter"
        runner = FailingServiceUserRunner()

        try:
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=project_dir,
                source_repo=source_repo,
                output_dir=tmp_path / "out",
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
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            raise AssertionError("expected board service user creation failure")
        except SystemExit as exc:
            message = str(exc)

    assert "failed to create board service user 'boardsvc'" in message
    assert "scripts/ticket-board-boardsvc-setup.sh --apply" in message
    assert not any(call == ["id", "-u", "otto-agent"] for call in runner.calls)
    assert not any(call[:1] == ["useradd"] and call[-1] == "otto-agent" for call in runner.calls)


def test_switchyard_new_peer_auth_failure_names_remedy_before_owner_mutation() -> None:
    class FailingPeerAuthRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["getent", "passwd", "boardsvc"]:
                return subprocess.CompletedProcess(args, 0)
            if args[-1:] == ["--apply-peer-auth"]:
                return subprocess.CompletedProcess(args, 1, stderr="pg_hba.conf is not writable")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-peer-auth-fail.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        runner = FailingPeerAuthRunner()

        try:
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=tmp_path / "home" / "otto-agent" / "Projects" / "porter",
                source_repo=source_repo,
                output_dir=tmp_path / "out",
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
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            raise AssertionError("expected peer-auth setup failure")
        except SystemExit as exc:
            message = str(exc)

    assert "failed to install PostgreSQL peer-auth mapping" in message
    assert "OS user 'boardsvc'" in message
    assert "database role 'ticket_board_service'" in message
    assert "database 'porter_ticket_board'" in message
    assert "scripts/ticket-board-boardsvc-setup.sh --apply" in message
    assert "pg_hba.conf is not writable" in message
    assert not any(call == ["id", "-u", "otto-agent"] for call in runner.calls)
    assert not any(call[:1] == ["useradd"] and call[-1] == "otto-agent" for call in runner.calls)


def test_switchyard_new_missing_peer_auth_helper_names_remedy_before_owner_mutation() -> None:
    class MissingPeerAuthHelperRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args == ["getent", "passwd", "boardsvc"]:
                return subprocess.CompletedProcess(args, 0)
            if args[-1:] == ["--apply-peer-auth"]:
                raise FileNotFoundError(args[0])
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-new-peer-auth-missing.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "opt" / "switchyard" / "releases" / REMOTE_HEAD
        _write_exported_switchyard_release(source_repo)
        runner = MissingPeerAuthHelperRunner()

        try:
            switchyard_new_command(
                slug="porter",
                agent_name="otto-agent",
                project_name="Porter",
                project_path=tmp_path / "home" / "otto-agent" / "Projects" / "porter",
                source_repo=source_repo,
                output_dir=tmp_path / "out",
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
                konsole_process_launcher=RecordingProcessLauncher(),
            )
            raise AssertionError("expected missing peer-auth helper failure")
        except SystemExit as exc:
            message = str(exc)

    assert "failed to install PostgreSQL peer-auth mapping" in message
    assert "setup helper" in message
    assert "ticket-board-boardsvc-setup.sh" in message
    assert "scripts/ticket-board-boardsvc-setup.sh --apply" in message
    assert not any(call == ["id", "-u", "otto-agent"] for call in runner.calls)
    assert not any(call[:1] == ["useradd"] and call[-1] == "otto-agent" for call in runner.calls)

def test_new_project_dirty_git_checkout_still_fails_precheck() -> None:
    current_user = team_launcher.current_user_name()

    class GitAndPrecheckRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:1] == ["git"]:
                return subprocess.run(args, **kwargs)
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-dirty-source.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "source-repo"
        source_repo.mkdir()
        _run_git(["git", "init", "-b", "main"], cwd=source_repo)
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=source_repo)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=source_repo)
        tracked = source_repo / "tracked.txt"
        tracked.write_text("clean\n", encoding="utf-8")
        _run_git(["git", "add", "tracked.txt"], cwd=source_repo)
        _run_git(["git", "commit", "-m", "initial"], cwd=source_repo)
        tracked.write_text("dirty\n", encoding="utf-8")
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = GitAndPrecheckRunner()

        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected dirty checkout precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "deploy checkout" in message
    assert "uncommitted changes" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)

def test_new_project_rejects_source_that_is_neither_checkout_nor_release() -> None:
    current_user = team_launcher.current_user_name()

    class NotRepoRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(
                    args,
                    128,
                    stderr="fatal: not a git repository (or any of the parent directories): .git\n",
                )
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-arbitrary-source.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "not-switchyard"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = NotRepoRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected arbitrary source precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "neither a git checkout nor a Switchyard release" in message
    assert "expected a clean Switchyard source checkout" in message
    assert "fatal: not a git repository" not in message

def test_new_project_precheck_ignores_untracked_checkout_junk() -> None:
    current_user = team_launcher.current_user_name()

    class UntrackedOnlyRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                assert args[-1] == "--untracked-files=no"
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="",
                )
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        (source_repo / ".claude").mkdir()
        (source_repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        (source_repo / "scripts" / "__pycache__").mkdir(parents=True)
        (source_repo / "scripts" / "ticket_board" / "__pycache__").mkdir(parents=True)
        runner = UntrackedOnlyRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )
        assert any(
            call[:3] == ["git", "-C", str(source_repo)] and call[3:] == ["status", "--porcelain", "--untracked-files=no"]
            for call in runner.calls
        )

def test_git_status_porcelain_ignores_untracked_junk_but_reports_tracked_edits() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-status.") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _run_git(["git", "init", "-b", "main"], cwd=repo)
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=repo)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=repo)
        tracked = repo / "tracked.txt"
        tracked.write_text("clean\n", encoding="utf-8")
        _run_git(["git", "add", "tracked.txt"], cwd=repo)
        _run_git(["git", "commit", "-m", "initial"], cwd=repo)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        (repo / "scripts" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "ticket_board" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "__pycache__" / "team_launcher.cpython-313.pyc").write_bytes(b"pyc\n")
        (repo / "scripts" / "ticket_board" / "__pycache__" / "project_provision.cpython-313.pyc").write_bytes(
            b"pyc\n"
        )

        plain_status = _run_git(["git", "status", "--porcelain"], cwd=repo).stdout
        clean_status = team_launcher._git_status_porcelain(repo, runner=subprocess.run)
        tracked.write_text("dirty\n", encoding="utf-8")
        dirty_status = team_launcher._git_status_porcelain(repo, runner=subprocess.run)

    assert "?? .claude/" in plain_status
    assert "?? scripts/" in plain_status
    assert clean_status == ""
    assert dirty_status == " M tracked.txt"

def test_gitignore_hides_launcher_generated_junk() -> None:
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-gitignore.") as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _run_git(["git", "init", "-b", "main"], cwd=repo)
        _run_git(["git", "config", "user.email", "agent@example.invalid"], cwd=repo)
        _run_git(["git", "config", "user.name", "PGU Agent"], cwd=repo)
        (repo / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")
        _run_git(["git", "add", ".gitignore"], cwd=repo)
        _run_git(["git", "commit", "-m", "initial"], cwd=repo)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        (repo / "scripts" / "__pycache__").mkdir(parents=True)
        (repo / "scripts" / "__pycache__" / "team_launcher.cpython-313.pyc").write_bytes(b"pyc\n")

        status = _run_git(["git", "status", "--porcelain"], cwd=repo).stdout

    assert status == ""

def test_database_exists_uses_postgres_os_user_when_running_as_root() -> None:
    calls: list[list[str]] = []
    original_geteuid = team_launcher.os.geteuid

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="1\n")

    try:
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        assert team_launcher._database_exists("porter_board", runner=runner) is True
    finally:
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [
        [
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-XAt",
            "postgresql:///postgres?host=/var/run/postgresql",
            "-c",
            "SELECT 1 FROM pg_database WHERE datname = 'porter_board'",
        ]
    ]
    assert "user=postgres" not in calls[0][5]

def test_database_exists_keeps_non_root_psql_path_unchanged() -> None:
    calls: list[list[str]] = []
    original_geteuid = team_launcher.os.geteuid

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="")

    try:
        team_launcher.os.geteuid = lambda: 1001  # type: ignore[method-assign]
        assert team_launcher._database_exists("porter_board", runner=runner) is False
    finally:
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [
        [
            "psql",
            "-XAt",
            "postgresql:///postgres?host=/var/run/postgresql",
            "-c",
            "SELECT 1 FROM pg_database WHERE datname = 'porter_board'",
        ]
    ]

def test_ticket_board_table_count_uses_target_database_and_postgres_os_user_when_root() -> None:
    calls: list[list[str]] = []
    original_geteuid = team_launcher.os.geteuid

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="12\n")

    try:
        team_launcher.os.geteuid = lambda: 0  # type: ignore[method-assign]
        assert team_launcher._ticket_board_table_count("porter_ticket_board", runner=runner) == 12
    finally:
        team_launcher.os.geteuid = original_geteuid  # type: ignore[method-assign]

    assert calls == [
        [
            "sudo",
            "-u",
            "postgres",
            "psql",
            "-XAt",
            "postgresql:///porter_ticket_board?host=/var/run/postgresql",
            "-c",
            "SELECT count(*)::int FROM pg_catalog.pg_tables WHERE schemaname = 'ticket_board'",
        ]
    ]

def test_system_unit_successful_empty_listing_means_unit_absent() -> None:
    original_path_exists = team_launcher._path_exists

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == ["systemctl", "list-unit-files", "--no-legend", "porter-ticket-board.service"]
        return subprocess.CompletedProcess(args, 0, stdout="")

    try:
        team_launcher._path_exists = lambda _path: True  # type: ignore[assignment]
        assert team_launcher._system_unit_file_exists("porter-ticket-board.service", runner=runner) is False
    finally:
        team_launcher._path_exists = original_path_exists  # type: ignore[assignment]

def test_new_project_execute_warms_sudo_once_then_runs_generated_script() -> None:
    current_user = team_launcher.current_user_name()

    class RecordingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.call_kwargs: list[dict[str, object]] = []

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.call_kwargs.append(dict(kwargs))
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = RecordingRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            == 0
        )

    sudo_index = next(index for index, call in enumerate(runner.calls) if call == ["sudo", "-v"])
    bash_index = next(index for index, call in enumerate(runner.calls) if call[:1] == ["bash"])
    assert sudo_index < bash_index
    assert runner.calls[bash_index] == ["bash", str(output_dir / "operator-commands.sh")]
    assert runner.call_kwargs[bash_index]["cwd"] == str(output_dir)

def test_new_project_rerun_rejects_partially_provisioned_unregistered_project_before_mutating() -> None:
    current_user = team_launcher.current_user_name()

    class FullyProvisionedRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service enabled\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///postgres?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="1\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///porter_ticket_board?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="12\n")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = FullyProvisionedRunner()

        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                execute=True,
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: True,
            )
            raise AssertionError("expected partially provisioned precheck failure")
        except SystemExit as exc:
            message = str(exc)

        assert not (tmp_path / "out").exists()

    assert "project 'porter' is partially provisioned but not registered" in message
    assert "database porter_ticket_board has 12 ticket_board tables" in message
    assert "porter-ticket-board.service is installed" in message
    assert "no usable launch entry exists" in message
    assert "to inspect recovery: switchyard teardown porter --dry-run" in message
    assert "to launch it:      switchyard porter" not in message
    assert "to start over:" not in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)


def test_precheck_registered_provisioned_project_names_working_launch_command() -> None:
    class FullyProvisionedRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service enabled\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///postgres?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="1\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///porter_ticket_board?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="12\n")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new-registered.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        config_path = tmp_path / "porter.json"
        config_path.write_text(
            json.dumps(
                {
                    "project": "porter",
                    "project_name": "Porter",
                    "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "workdir": str(project_repo)}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (registry_dir / "porter.json").write_text(
            json.dumps(
                {
                    "schema": team_launcher.SWITCHYARD_REGISTRY_SCHEMA,
                    "slug": "porter",
                    "name": "Porter",
                    "config_path": str(config_path),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        runner = FullyProvisionedRunner()
        plan = team_launcher.build_plan(project="porter", owner_user=team_launcher.current_user_name())

        try:
            team_launcher.precheck_new_project(
                plan,
                source_repo=source_repo,
                repository=project_repo,
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: True,
                config_dir=tmp_path / "configs",
                registry_dir=registry_dir,
                require_owner_user=False,
            )
            raise AssertionError("expected provisioned project precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "project 'porter' is already provisioned" in message
    assert "database porter_ticket_board has 12 ticket_board tables" in message
    assert "porter-ticket-board.service is installed" in message
    assert "to launch it:      switchyard porter" in message
    assert "to start over:     switchyard teardown porter --dry-run" in message
    assert "partially provisioned" not in message


class TeardownRunner(FakeRunner):
    def __init__(
        self,
        *,
        database_exists: bool = True,
        ticket_count: int = 0,
        fail_label: str = "",
        fail_ticket_count: bool = False,
    ) -> None:
        super().__init__()
        self.database_exists = database_exists
        self.ticket_count = ticket_count
        self.fail_label = fail_label
        self.fail_ticket_count = fail_ticket_count

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///postgres?host=/var/run/postgresql":
            return subprocess.CompletedProcess(args, 0, stdout="1\n" if self.database_exists else "\n")
        if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///porter_ticket_board?host=/var/run/postgresql":
            if self.fail_ticket_count:
                return subprocess.CompletedProcess(args, 1, stderr="permission denied\n")
            return subprocess.CompletedProcess(args, 0, stdout=f"{self.ticket_count}\n")
        command_display = shlex.join(args)
        if self.fail_label and self.fail_label in command_display:
            return subprocess.CompletedProcess(args, 1, stderr="simulated failure\n")
        return subprocess.CompletedProcess(args, 0)


def _mutating_teardown_calls(runner: TeardownRunner) -> list[list[str]]:
    return [
        call
        for call in runner.calls
        if call[:2] != ["psql", "-XAt"]
    ]


def _teardown_action_lines(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if re.match(r"^  [0-9]+\\. ", line) or line.startswith("     command: ")
    ]


def test_switchyard_teardown_dry_run_lists_partial_project_artifacts_without_mutating() -> None:
    runner = TeardownRunner(ticket_count=0)
    lines: list[str] = []

    assert (
        team_launcher.switchyard_teardown_command(
            "porter",
            dry_run=True,
            runner=runner,
            print_func=lines.append,
        )
        == 0
    )

    output = "\n".join(lines)
    assert "switchyard: teardown plan for porter" in output
    assert "switchyard: owner user: porter-agent" in output
    assert "remove system board unit /etc/systemd/system/porter-ticket-board.service" in output
    assert "remove tmpfiles config /etc/tmpfiles.d/porter-ticket-board.conf" in output
    assert "remove polkit rule /etc/polkit-1/rules.d/49-porter-ticket-board-deploy.rules" in output
    assert "drop PostgreSQL database porter_ticket_board" in output
    assert "remove board release root /home/porter-agent/porter-ticketboard-live" in output
    assert "remove switchyard registry entry /etc/switchyard/projects/porter.json" in output
    assert "owner user account porter-agent" in output
    assert "owner home directory /home/porter-agent" in output
    assert "project checkout /home/porter-agent/Projects/porter" in output
    assert "dry-run only; no changes made" in output
    assert _mutating_teardown_calls(runner) == []


def test_switchyard_teardown_refuses_nonempty_board_without_explicit_flag() -> None:
    runner = TeardownRunner(ticket_count=2)
    lines: list[str] = []

    try:
        team_launcher.switchyard_teardown_command(
            "porter",
            confirm="porter",
            runner=runner,
            print_func=lines.append,
        )
        raise AssertionError("expected non-empty board refusal")
    except SystemExit as exc:
        message = str(exc)

    assert "refusing to drop non-empty board database porter_ticket_board with 2 ticket(s)" in message
    assert "--drop-nonempty-board" in message
    assert "non-empty board guard: destructive run requires --drop-nonempty-board" in "\n".join(lines)
    assert _mutating_teardown_calls(runner) == []


def test_switchyard_teardown_refuses_unknown_ticket_count_without_explicit_flag() -> None:
    runner = TeardownRunner(fail_ticket_count=True)
    lines: list[str] = []

    try:
        team_launcher.switchyard_teardown_command(
            "porter",
            confirm="porter",
            runner=runner,
            print_func=lines.append,
        )
        raise AssertionError("expected unknown ticket-count refusal")
    except SystemExit as exc:
        message = str(exc)

    assert "cannot determine whether board database porter_ticket_board contains tickets" in message
    assert "--drop-nonempty-board" in message
    assert "board ticket count: unknown" in "\n".join(lines)
    assert "board-count guard: destructive run requires --drop-nonempty-board" in "\n".join(lines)
    assert _mutating_teardown_calls(runner) == []


def test_switchyard_teardown_requires_project_name_confirmation() -> None:
    runner = TeardownRunner(ticket_count=0)

    try:
        team_launcher.switchyard_teardown_command(
            "porter",
            confirm="wrong",
            runner=runner,
            print_func=lambda _line: None,
        )
        raise AssertionError("expected confirmation failure")
    except SystemExit as exc:
        message = str(exc)

    assert "teardown confirmation failed" in message
    assert _mutating_teardown_calls(runner) == []


def test_switchyard_teardown_destructive_path_runs_guarded_actions_and_preserves_owner_home() -> None:
    dry_runner = TeardownRunner(ticket_count=1)
    dry_lines: list[str] = []
    assert (
        team_launcher.switchyard_teardown_command(
            "porter",
            dry_run=True,
            drop_nonempty_board=True,
            runner=dry_runner,
            print_func=dry_lines.append,
        )
        == 0
    )

    runner = TeardownRunner(ticket_count=1)
    lines: list[str] = []

    assert (
        team_launcher.switchyard_teardown_command(
            "porter",
            confirm="porter",
            drop_nonempty_board=True,
            runner=runner,
            print_func=lines.append,
        )
        == 0
    )

    mutating = _mutating_teardown_calls(runner)
    joined = "\n".join(shlex.join(call) for call in mutating)
    assert "systemctl disable --now porter-ticket-board.service" in joined
    assert "systemctl --user disable --now porter-ticket-board-notify-listener.service" in joined
    assert "rm -f /etc/systemd/system/porter-ticket-board.service" in joined
    assert "rm -f /etc/tmpfiles.d/porter-ticket-board.conf" in joined
    assert "rm -f /etc/polkit-1/rules.d/49-porter-ticket-board-deploy.rules" in joined
    assert 'DROP DATABASE IF EXISTS "porter_ticket_board" WITH (FORCE);' in joined
    assert "rm -rf -- /home/porter-agent/porter-ticketboard-live" in joined
    assert "rm -f /etc/switchyard/projects/porter.json" in joined
    assert ["rm", "-rf", "--", "/home/porter-agent"] not in mutating
    assert ["userdel", "porter-agent"] not in mutating
    assert "switchyard: teardown complete for porter" in "\n".join(lines)
    assert _teardown_action_lines(dry_lines) == _teardown_action_lines(lines)


def test_switchyard_teardown_clears_partial_state_so_new_precheck_accepts_slug() -> None:
    class StatefulTeardownRunner(TeardownRunner):
        def __init__(self) -> None:
            super().__init__(database_exists=True, ticket_count=0)
            self.unit_exists = True

        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                self.calls.append(args)
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="porter-ticket-board.service enabled\n" if self.unit_exists else "",
                )
            if args[:2] == ["bash", "-lc"] and "systemctl disable --now porter-ticket-board.service" in args[2]:
                self.unit_exists = False
            if args[:2] == ["sudo", "-u"] and "DROP DATABASE IF EXISTS" in " ".join(args):
                self.database_exists = False
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                self.calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="")
            return super().__call__(args, **kwargs)

    with tempfile.TemporaryDirectory(prefix="pgu-switchyard-teardown-retry.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = StatefulTeardownRunner()

        assert (
            team_launcher.switchyard_teardown_command(
                "porter",
                confirm="porter",
                runner=runner,
                print_func=lambda _line: None,
            )
            == 0
        )
        plan = team_launcher.build_plan(project="porter", owner_user=team_launcher.current_user_name())
        team_launcher.precheck_new_project(
            plan,
            source_repo=source_repo,
            repository=project_repo,
            runner=runner,
            port_in_use=lambda _port: False,
            socket_exists=lambda _path: False,
            require_owner_user=False,
        )


def test_switchyard_teardown_partial_failure_reports_completed_and_remaining_actions() -> None:
    runner = TeardownRunner(ticket_count=0, fail_label="/etc/systemd/system/porter-ticket-board.service")

    try:
        team_launcher.switchyard_teardown_command(
            "porter",
            confirm="porter",
            runner=runner,
            print_func=lambda _line: None,
        )
        raise AssertionError("expected partial teardown failure")
    except SystemExit as exc:
        message = str(exc)

    assert "teardown failed while trying to remove system board unit /etc/systemd/system/porter-ticket-board.service" in message
    assert "completed before failure:" in message
    assert "stop and disable system board service porter-ticket-board.service" in message
    assert "stop and disable owner notify-listener service porter-ticket-board-notify-listener.service" in message
    assert "remaining after failure:" in message
    assert "drop PostgreSQL database porter_ticket_board" in message
    joined = "\n".join(shlex.join(call) for call in _mutating_teardown_calls(runner))
    assert 'DROP DATABASE IF EXISTS "porter_ticket_board" WITH (FORCE);' not in joined


def test_switchyard_main_dispatches_teardown_command() -> None:
    original = team_launcher.switchyard_teardown_command
    calls: list[dict[str, object]] = []

    def fake_teardown(project: str, **kwargs: object) -> int:
        calls.append({"project": project, **kwargs})
        return 0

    try:
        team_launcher.switchyard_teardown_command = fake_teardown
        assert (
            team_launcher.switchyard_main(
                [
                    "teardown",
                    "porter",
                    "--dry-run",
                    "--drop-nonempty-board",
                    "--remove-owner-home",
                    "--confirm",
                    "porter",
                ]
            )
            == 0
        )
    finally:
        team_launcher.switchyard_teardown_command = original

    assert calls
    assert calls[0]["project"] == "porter"
    assert calls[0]["dry_run"] is True
    assert calls[0]["drop_nonempty_board"] is True
    assert calls[0]["remove_owner_home"] is True
    assert calls[0]["confirm"] == "porter"
    assert "teardown" in team_launcher.switchyard_help_text()


def test_new_project_rerun_allows_installed_unit_with_empty_database_recovery_path() -> None:
    current_user = team_launcher.current_user_name()

    class EmptyDatabaseRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service enabled\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///postgres?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="1\n")
            if args[:2] == ["psql", "-XAt"] and args[2] == "postgresql:///porter_ticket_board?host=/var/run/postgresql":
                return subprocess.CompletedProcess(args, 0, stdout="0\n")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = EmptyDatabaseRunner()

        assert (
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=tmp_path / "out",
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: True,
            )
            == 0
        )

def test_new_project_rejects_installed_unit_without_database() -> None:
    current_user = team_launcher.current_user_name()

    class ExistingUnitNoDatabaseRunner(FakeRunner):
        def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            self.calls.append(args)
            if args[:3] == ["systemctl", "list-unit-files", "--no-legend"]:
                return subprocess.CompletedProcess(args, 0, stdout="porter-ticket-board.service disabled\n")
            if args[:2] == ["psql", "-XAt"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            if len(args) >= 5 and args[:4] == ["git", "-C", args[2], "status"]:
                return subprocess.CompletedProcess(args, 0, stdout="")
            return subprocess.CompletedProcess(args, 0)

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = ExistingUnitNoDatabaseRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: False,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected unit-without-database precheck failure")
        except SystemExit as exc:
            message = str(exc)

    assert "porter-ticket-board.service is installed" in message
    assert "database 'porter_ticket_board' does not exist" in message
    assert not output_dir.exists()
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)

def test_new_project_rejects_port_collision_before_mutating() -> None:
    current_user = team_launcher.current_user_name()
    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-new.") as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "out"
        source_repo = tmp_path / "repo"
        source_repo.mkdir()
        project_repo = tmp_path / "project-repo"
        project_repo.mkdir()
        runner = FakeRunner()
        try:
            new_project_command(
                "porter",
                owner_user=current_user,
                source_repo=source_repo,
                repository=project_repo,
                output_dir=output_dir,
                execute=True,
                runner=runner,
                port_in_use=lambda _port: True,
                socket_exists=lambda _path: False,
            )
            raise AssertionError("expected port collision precheck failure")
        except SystemExit as exc:
            message = str(exc)
        assert not output_dir.exists()

    assert "port 23682 is already in use" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)

def main() -> int:
    run_team_launcher_tests(globals(), first=())
    print("team_launcher_project_precheck_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
