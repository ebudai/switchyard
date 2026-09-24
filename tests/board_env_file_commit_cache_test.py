#!/usr/bin/env python3
"""An owner's environment file cannot move the board off its managed cache.

Live syrd after the 2026-09-24 restart: the generated unit said

    EnvironmentFile=-/home/switchyard-agent/.config/syrd/ticket-board.env
    Environment=TICKET_BOARD_COMMIT_GIT_DIR=/home/switchyard-agent/syrd-source-cache.git

and the file, written by an older release and kept across every upgrade because
it also holds the report secret, still said
`TICKET_BOARD_COMMIT_GIT_DIR=/data/git/switchyard.git`. systemd lets the file
win, so the board verified commits against an eric-owned cache nothing
refreshes, and every newly published candidate was refused as an unknown
commit (SYRD-251).

The unit here is the one provisioning renders, and the board it starts is the
real one, given the environment systemd documents it would get.
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests  # noqa: E402

SERVICE_SCRIPT = ROOT / "scripts" / "ticket-board-service.sh"
PROJECT = "syrdenvt"
#: Stands in for the report token. It must reach the board and must never be
#: printed by anything that inspects the file.
SECRET = "report-token-" + uuid.uuid4().hex


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"},
    ).stdout.strip()


class Tenant:
    """A provisioned tenant's board, in a sandbox, with a stale env file."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd251."))
        self.home = self.tmp / "home"
        self.home.mkdir()
        # The project's public remote, the managed cache the board should use
        # (as the owner's is: a clone that fetches from it), and the stale one
        # the old env file names, which nothing refreshes.
        self.public = self.tmp / "public.git"
        work = self.tmp / "work"
        git("init", "-q", "-b", "main", str(work))
        (work / "a").write_text("a\n", encoding="utf-8")
        git("add", "a", cwd=work)
        git("commit", "-qm", "first", cwd=work)
        git("clone", "-q", "--bare", str(work), str(self.public))
        self.work = work
        git("remote", "add", "origin", str(self.public), cwd=work)
        self.managed = self.tmp / "managed-cache.git"
        git("clone", "-q", "--bare", str(self.public), str(self.managed))
        git("--git-dir", str(self.managed), "config", "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*")
        self.stale = self.tmp / "stale-cache.git"
        git("init", "-q", "--bare", str(self.stale))
        self.port = free_port()
        board_root = self.tmp / "board"
        board_root.mkdir()
        (board_root / "current").symlink_to(ROOT)
        from scripts.ticket_board.project_provision import build_plan, render_board_unit

        previous = os.environ.get("TICKET_BOARD_PYTHON")
        os.environ["TICKET_BOARD_PYTHON"] = sys.executable
        try:
            self.plan = build_plan(
                project=PROJECT, owner_user=getpass.getuser(), owner_home=self.home,
                port=self.port, board_root=board_root, commit_git_dir=str(self.managed),
                asset_dir=self.tmp / "assets", frame_dir=self.tmp / "frames",
            )
            self.unit = render_board_unit(self.plan)
        finally:
            if previous is None:
                os.environ.pop("TICKET_BOARD_PYTHON", None)
            else:
                os.environ["TICKET_BOARD_PYTHON"] = previous
        self.unit_path = self.tmp / f"{PROJECT}-ticket-board.service"
        self.unit_path.write_text(self.unit, encoding="utf-8")
        self.env_file = self.home / ".config" / PROJECT / "ticket-board.env"
        self.env_file.parent.mkdir(parents=True)
        self.env_file.write_text(
            f"TICKET_BOARD_TENANT_REPORT_TOKEN={SECRET}\n"
            f"TICKET_BOARD_COMMIT_GIT_DIR={self.stale}\n",
            encoding="utf-8",
        )
        self.env_file.chmod(0o600)
        self.env_bytes = self.env_file.read_bytes()
        self.log = self.tmp / "board.log"
        self.started = False

    def directives(self, key: str) -> list[str]:
        return [
            line.split("=", 1)[1]
            for line in self.unit.splitlines()
            if line.split("=", 1)[0] == key and "=" in line
        ]

    def start_as_systemd_would(self) -> None:
        """The unit's ExecStart, with the environment systemd would give it.

        systemd.exec(5): "Settings from these files override settings made with
        Environment=". That is the precedence that broke syrd, so it is applied
        here exactly: every `Environment=` in unit order, then every
        `EnvironmentFile=` over the top. The suite runs isolated from the user
        manager on purpose, so this composes what the manager would; the same
        unit was also run once under a real `systemd-run --user` (SYRD-251).
        """
        exec_start = self.directives("ExecStart")
        assert len(exec_start) == 1, exec_start
        environment = {"PATH": "/usr/bin:/bin"}
        for value in self.directives("Environment"):
            for assignment in shlex.split(value):
                key, _, setting = assignment.partition("=")
                environment[key] = setting
        files = self.directives("EnvironmentFile")
        # The file is the one the unit names, not one this test chose.
        assert files == [f"-{self.env_file}"], files
        for name in files:
            for line in Path(name.lstrip("-")).read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    key, _, setting = line.partition("=")
                    environment[key.strip()] = setting.strip().strip('"')
        (cwd,) = self.directives("WorkingDirectory")
        self.log_handle = self.log.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            shlex.split(exec_start[0]), cwd=cwd, env=environment,
            stdout=self.log_handle, stderr=subprocess.STDOUT,
        )
        self.started = True

    def client_config(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/client-config", timeout=1.0
                ) as response:
                    return json.load(response)
            except OSError as exc:
                last = str(exc)
                time.sleep(0.2)
        log = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        raise AssertionError(f"the board never answered: {last}\n{log}")

    def close(self) -> None:
        if self.started:
            self.process.terminate()
            self.process.wait(timeout=30)
            self.log_handle.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_a_stale_env_file_cannot_move_the_running_board_off_the_managed_cache() -> None:
    tenant = Tenant()
    try:
        tenant.start_as_systemd_would()
        config = tenant.client_config()
        assert config.get("commit_repositories") == [str(tenant.managed)], (
            config.get("commit_repositories"), str(tenant.managed), str(tenant.stale))
        # The file is still the service's: the secret beside the stale line
        # reached the board, and nothing rewrote the file to get here.
        log = tenant.log.read_text(encoding="utf-8")
        assert "tenant report endpoint: enabled" in log, log
        assert tenant.env_file.read_bytes() == tenant.env_bytes
    finally:
        tenant.close()


def test_a_newly_published_commit_verifies_with_no_manual_fetch() -> None:
    """What the ticket is for: the next candidate is not an unknown commit."""
    from scripts.ticket_board.app import TicketBoardApp

    tenant = Tenant()
    try:
        tenant.start_as_systemd_would()
        reported = tenant.client_config()["commit_repositories"]
        # Published after the board started, to the public remote only.
        (tenant.work / "b").write_text("b\n", encoding="utf-8")
        git("add", "b", cwd=tenant.work)
        git("commit", "-qm", "candidate", cwd=tenant.work)
        git("push", "-q", "origin", "main", cwd=tenant.work)
        candidate = git("rev-parse", "HEAD", cwd=tenant.work)
        assert subprocess.run(
            ["git", "--git-dir", str(tenant.managed), "cat-file", "-e", f"{candidate}^{{commit}}"],
            capture_output=True,
        ).returncode != 0, "the cache already had it, so nothing would be proven"

        # The repositories the running board reported, verified the way it
        # verifies a submission.
        app = TicketBoardApp(frame_dir=tenant.tmp / "f", asset_dir=tenant.tmp / "a",
                             commit_git_dir=os.pathsep.join(reported), database_url="")
        assert app._validate_commit_hash(candidate) == candidate

        # And verification is not relaxed: the cache the stale file named still
        # refuses it, and so does the managed one for a commit nobody published.
        stale = TicketBoardApp(frame_dir=tenant.tmp / "f", asset_dir=tenant.tmp / "a",
                               commit_git_dir=str(tenant.stale), database_url="")
        for board, value in ((stale, candidate), (app, "0" * 40)):
            try:
                board._validate_commit_hash(value)
            except ValueError as exc:
                assert "unknown commit_hash" in str(exc), exc
            else:
                raise AssertionError(f"{value} verified against {board.commit_git_dirs}")
    finally:
        tenant.close()


class WrongCacheBoard:
    """The real board, started the way a pre-fix unit started it."""

    def __init__(self, tenant: Tenant, *, commit_git_dir_arg: bool) -> None:
        argv = [sys.executable, str(ROOT / "scripts" / "ticket-board.py"),
                "--host", "127.0.0.1", "--port", str(tenant.port), "--unix-socket", "",
                "--frames", str(tenant.tmp / "frames"), "--assets", str(tenant.tmp / "assets")]
        if commit_git_dir_arg:
            argv += ["--commit-git-dir", str(tenant.managed)]
        self.process = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "TICKET_BOARD_COMMIT_GIT_DIR": str(tenant.stale),
                 "TICKET_BOARD_DATABASE_URL": ""},
        )
        tenant.client_config()

    def close(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=30)


def deploy_check(tenant: Tenant, scope: str = "system") -> subprocess.CompletedProcess[str]:
    """The deploy's own post-restart check, sourced from the shipped script."""
    stub = tenant.tmp / "stub-bin"
    stub.mkdir(exist_ok=True)
    # Only the fragment lookup is stubbed, so the check reads the unit this
    # test rendered instead of whatever the host's system manager has.
    (stub / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub / "systemctl").chmod(0o755)
    env = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "HOME": str(tenant.home),
        "TICKET_BOARD_PROJECT": PROJECT,
        "TICKET_BOARD_OWNER_HOME": str(tenant.home),
        "TICKET_BOARD_COMMIT_GIT_DIR": str(tenant.managed),
        "TICKET_BOARD_SYSTEM_UNIT_PATH": str(tenant.unit_path),
        "BOARD_PORT": str(tenant.port),
        "BOARD_SMOKE_TIMEOUT_SECONDS": "3",
    }
    return subprocess.run(
        ["bash", "-c", f'source {shlex.quote(str(SERVICE_SCRIPT))} && verify_live_commit_repositories "$1"',
         "check", scope],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_deploy_restart_refuses_a_board_the_env_file_moved() -> None:
    tenant = Tenant()
    board = WrongCacheBoard(tenant, commit_git_dir_arg=False)
    try:
        result = deploy_check(tenant)
        assert result.returncode != 0, (result.stdout, result.stderr)
        said = result.stdout + result.stderr
        assert str(tenant.stale) in said and str(tenant.managed) in said, said
        assert str(tenant.env_file) in said, said
        # It points at the file. It never reads it out.
        assert SECRET not in said, said
    finally:
        board.close()
        tenant.close()


def test_deploy_restart_accepts_a_board_on_the_managed_cache() -> None:
    tenant = Tenant()
    board = WrongCacheBoard(tenant, commit_git_dir_arg=True)
    try:
        result = deploy_check(tenant)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert f"verifies commits against {tenant.managed}" in result.stdout, result.stdout
    finally:
        board.close()
        tenant.close()


def test_deploy_restart_runs_the_check_and_rolls_back_on_it() -> None:
    body = SERVICE_SCRIPT.read_text(encoding="utf-8")
    start = body.index("deploy_restart_service() {")
    function = body[start:body.index("\n}\n", start)]
    check = function.index('verify_live_commit_repositories "$scope"')
    # After the restart it inspects, and before the listener is released on it.
    assert function.index("restart_live_service") < check, function
    assert check < function.index("start_listener_after_upgrade\n    if ! verify_listener"), function
    after = function[check:check + 200]
    assert "rollback_live_service" in after and "exit 1" in after, after


def test_the_user_scope_unit_also_passes_the_cache_on_the_command_line() -> None:
    """The deploy script's own renderer has the same EnvironmentFile= line."""
    tmp = Path(tempfile.mkdtemp(prefix="syrd251-user."))
    try:
        rendered = subprocess.run(
            [str(SERVICE_SCRIPT), "render-unit"], capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp), "TICKET_BOARD_PROJECT": PROJECT,
                 "TICKET_BOARD_OWNER_HOME": str(tmp),
                 "TICKET_BOARD_COMMIT_GIT_DIR": str(tmp / "managed.git")},
        )
        assert rendered.returncode == 0, rendered.stderr
        exec_start = [line for line in rendered.stdout.splitlines() if line.startswith("ExecStart=")]
        assert len(exec_start) == 1, rendered.stdout
        words = shlex.split(exec_start[0][len("ExecStart="):])
        assert words[words.index("--commit-git-dir") + 1] == str(tmp / "managed.git"), words
        assert "EnvironmentFile=" in rendered.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_publisher_still_finds_the_cache_it_refreshes() -> None:
    """The Environment= line stays: it is how publication finds the cache."""
    tenant = Tenant()
    try:
        from scripts import switchyard_publication_authority as authority

        previous = os.environ.get(authority.BOARD_UNIT_TEST_DIR_ENV)
        os.environ[authority.BOARD_UNIT_TEST_DIR_ENV] = str(tenant.tmp)
        tenant.unit_path.chmod(0o644)
        try:
            unit = authority.board_unit(PROJECT)
        finally:
            if previous is None:
                os.environ.pop(authority.BOARD_UNIT_TEST_DIR_ENV, None)
            else:
                os.environ[authority.BOARD_UNIT_TEST_DIR_ENV] = previous
        assert unit["commit_cache"] == str(tenant.managed), unit
        assert unit["board_url"] == f"http://127.0.0.1:{tenant.port}", unit
    finally:
        tenant.close()


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"board_env_file_commit_cache_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
