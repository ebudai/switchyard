#!/usr/bin/env python3
"""Split team-launcher regression tests."""

from __future__ import annotations

from team_launcher_test_helpers import *

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

def test_new_project_rerun_rejects_fully_provisioned_project_before_mutating() -> None:
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
            raise AssertionError("expected fully provisioned precheck failure")
        except SystemExit as exc:
            message = str(exc)

        assert not (tmp_path / "out").exists()

    assert "project 'porter' is already provisioned" in message
    assert "database porter_ticket_board has 12 ticket_board tables" in message
    assert "porter-ticket-board.service is installed" in message
    assert "to launch it:      switchyard porter" in message
    assert "to start over:" in message
    assert not any(call[:1] == ["sudo"] for call in runner.calls)
    assert not any(call[:1] == ["bash"] for call in runner.calls)

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
