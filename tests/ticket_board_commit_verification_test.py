#!/usr/bin/env python3
"""Regression test: server-side ticket commit hashes resolve in the project repo."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.commit_repos import commit_git_dir_env_for_project  # noqa: E402


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return proc.stdout.strip()


def git_bare(args: list[str], git_dir: Path) -> str:
    proc = subprocess.run(["git", f"--git-dir={git_dir}", *args], check=True, text=True, capture_output=True)
    return proc.stdout.strip()


def make_repo(path: Path, name: str) -> str:
    path.mkdir()
    git(["init"], path)
    git(["config", "user.name", "Test"], path)
    git(["config", "user.email", "test@example.com"], path)
    (path / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    git(["add", f"{name}.txt"], path)
    git(["commit", "-m", f"Seed {name}"], path)
    return git(["rev-parse", "HEAD"], path)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ticket-board-commit-verification.") as tmpdir:
        root = Path(tmpdir)
        project_repo = root / "project-repo"
        other_repo = root / "other-tenant-repo"
        project_commit = make_repo(project_repo, "project")
        other_commit = make_repo(other_repo, "other")

        data_dir = root / "pgdata"
        socket_dir = root / "socket"
        frames = root / "frames"
        assets = root / "assets"
        socket_dir.mkdir()
        frames.mkdir()
        assets.mkdir()
        port = t.free_port()
        dbname = "pgu_commit_verification_test"
        admin_conn = t.conninfo(socket_dir, port, dbname)
        t.run(["initdb", "-D", str(data_dir), "-A", "trust", "--no-locale", "--username=postgres"])
        try:
            t.run(["pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "-w", "start"], capture=False)
            t.run(["createdb", "-h", str(socket_dir), "-p", str(port), "-U", "postgres", dbname])
            t.psql(admin_conn, t.SCHEMA_PATH.read_text(encoding="utf-8"))
            t.create_roles(admin_conn)
            t.psql(admin_conn, t.RBAC_PATH.read_text(encoding="utf-8"))
            for ticket_id, assignee in (
                ("PGU-87101", "app"),
                ("PGU-87102", "ops"),
                ("PGU-87103", "perf"),
                ("PGU-87105", "main"),
            ):
                t.seed_postgres_ticket(
                    admin_conn,
                    ticket_id,
                    title=ticket_id,
                    state="in_progress",
                    assignee=assignee,
                    implementation="Ready.",
                )
            t.seed_postgres_ticket(
                admin_conn,
                "PGU-87104",
                title="Mark done rejects fake commit",
                state="director_review",
                assignee="director",
                implementation="Ready.",
                audit_signoff=True,
            )

            app = t.TicketBoardApp(
                frames,
                assets,
                commit_git_dir=project_repo,
                database_url=t.conninfo(socket_dir, port, dbname, t.SERVICE_ROLE),
            )
            server = t.TicketBoardServer(
                ("127.0.0.1", 0),
                app,
                director_notifier=t.QuietNotifier(),
                report_token=t.TEST_REPORT_TOKEN,
            )
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                fake_commit = "f" * 40
                fake_submit = t.post_json(
                    base_url,
                    "/api/tickets/PGU-87101/actions/submit_to_audit",
                    {"commit_hash": fake_commit},
                    caller="app",
                    expect=400,
                )
                assert f"unknown commit_hash: {fake_commit}" in str(fake_submit), fake_submit

                other_submit = t.post_json(
                    base_url,
                    "/api/tickets/PGU-87102/actions/submit_to_audit",
                    {"commit_hash": other_commit},
                    caller="ops",
                    expect=400,
                )
                assert f"unknown commit_hash: {other_commit}" in str(other_submit), other_submit

                fake_done = t.post_json(
                    base_url,
                    "/api/tickets/PGU-87104/actions/mark_done",
                    {"commit_hash": fake_commit},
                    caller="director",
                    expect=400,
                )
                assert f"unknown commit_hash: {fake_commit}" in str(fake_done), fake_done

                accepted = t.post_json(
                    base_url,
                    "/api/tickets/PGU-87103/actions/submit_to_audit",
                    {"commit_hash": project_commit[:12]},
                    caller="perf",
                )
                assert accepted["ticket"]["commit_hash"] == project_commit, accepted
                assert (
                    t.psql(admin_conn, "SELECT state || ':' || commit_hash FROM ticket_board.tickets WHERE id = 'PGU-87101';")
                    == "in_progress:"
                )
                assert (
                    t.psql(admin_conn, "SELECT state || ':' || commit_hash FROM ticket_board.tickets WHERE id = 'PGU-87102';")
                    == "in_progress:"
                )
                assert (
                    t.psql(admin_conn, "SELECT state || ':' || commit_hash FROM ticket_board.tickets WHERE id = 'PGU-87104';")
                    == "director_review:"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            missing_repo = root / "missing-project-repo.git"
            missing_app = t.TicketBoardApp(
                frames,
                assets,
                commit_git_dir=missing_repo,
                database_url=t.conninfo(socket_dir, port, dbname, t.SERVICE_ROLE),
            )
            missing_server = t.TicketBoardServer(
                ("127.0.0.1", 0),
                missing_app,
                director_notifier=t.QuietNotifier(),
                report_token=t.TEST_REPORT_TOKEN,
            )
            t.TEST_WRITE_TOKEN = missing_server.write_token
            missing_thread = threading.Thread(target=missing_server.serve_forever, daemon=True)
            missing_thread.start()
            try:
                missing_url = f"http://127.0.0.1:{missing_server.server_port}"
                missing_submit = t.post_json(
                    missing_url,
                    "/api/tickets/PGU-87105/actions/submit_to_audit",
                    {"commit_hash": project_commit},
                    caller="main",
                    expect=400,
                )
                assert f"commit_hash verification repository not found: {missing_repo}" in str(missing_submit), missing_submit
                assert (
                    t.psql(admin_conn, "SELECT state || ':' || commit_hash FROM ticket_board.tickets WHERE id = 'PGU-87105';")
                    == "in_progress:"
                )
            finally:
                missing_server.shutdown()
                missing_server.server_close()
                missing_thread.join(timeout=2)

            switchyard_repo = Path("/data/git/switchyard.git")
            pgu_repo = Path("/data/git/pgu.git")
            if switchyard_repo.exists() and pgu_repo.exists():
                pgu_commit_repos = commit_git_dir_env_for_project(project="pgu")
                assert pgu_commit_repos == "/data/git/switchyard.git:/data/git/pgu.git", pgu_commit_repos
                switchyard_commit = git_bare(["rev-parse", "refs/heads/main"], switchyard_repo)
                pgu_commit = git_bare(["rev-parse", "refs/heads/main"], pgu_repo)
                pgu_split_app = t.TicketBoardApp(
                    frames,
                    assets,
                    commit_git_dir=pgu_commit_repos,
                    database_url=t.conninfo(socket_dir, port, dbname, t.SERVICE_ROLE),
                )
                assert pgu_split_app._validate_commit_hash(switchyard_commit[:12]) == switchyard_commit
                assert pgu_split_app._validate_commit_hash(pgu_commit[:12]) == pgu_commit
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    print("ticket_board_commit_verification_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
