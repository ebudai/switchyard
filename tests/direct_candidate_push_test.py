#!/usr/bin/env python3
"""SYRD-123: an implementer publishes by pushing, and submits the public commit.

Every role in a project runs as one Unix account, and the User has restored that
account's ordinary write access to the project's GitHub repository, so the
Director publication hop is gone: an implementer pushes its own candidate and
submits the exact commit that is now public. This drives that end to end with
the real programs -- the real push wrapper, the real write client, the real
board -- against a real remote repository, one worktree per role, and no
privileged helper anywhere in it.

What is asserted is not only that the happy path works but that the old one is
absent: no publication request is recorded, the Director is not made to wait,
and nothing in the path needs sudo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

PROJECT = "cerulean"
PUBLISH = ROOT / "scripts" / "switchyard-publish-candidate"
IMPLEMENTERS = ("main", "app", "ops")


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if check:
        assert done.returncode == 0, (args, done.stderr or done.stdout)
    return done.stdout.strip()


def publish(worktree: Path, role: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PUBLISH), "--role", role, *args],
        cwd=str(worktree),
        text=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def commit_in(worktree: Path, name: str, role: str) -> str:
    (worktree / f"{name}.txt").write_text(f"{name} by {role}\n", encoding="utf-8")
    git("add", f"{name}.txt", cwd=worktree)
    git("commit", "-qm", f"{role}: {name}", cwd=worktree)
    return git("rev-parse", "HEAD", cwd=worktree)


def remote_ref(remote: Path, ref: str) -> str:
    listed = git("--git-dir", str(remote), "for-each-ref", "--format=%(objectname)", f"refs/heads/{ref}")
    return listed.strip()


def publication_rows(admin: str) -> list[dict[str, Any]]:
    raw = t.psql(admin, """
SELECT coalesce(jsonb_agg(jsonb_build_object('id', id, 'ticket', ticket_id, 'state', state)
    ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.publication_requests;
""")
    return json.loads(raw)


def awaiting(admin: str, ticket: str) -> str:
    return t.psql(admin, f"""
SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = '{ticket}';
""")


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="syrd123-direct-push-", shutdown="immediate") as cluster:
        dbname = "syrd123_direct_push"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory(prefix="syrd123-") as tmpdir:
            root = Path(tmpdir)
            # The project's repository, as GitHub is to a real tenant: one
            # remote every role can read and write, reached with no credential
            # here because the account already holds one on a real host.
            remote = root / "project.git"
            git("init", "--bare", "-q", "-b", "main", str(remote))
            seed = root / "seed"
            git("init", "-q", "-b", "main", str(seed))
            git("config", "user.email", "seed@example.invalid", cwd=seed)
            git("config", "user.name", "Seed", cwd=seed)
            commit_in(seed, "readme", "seed")
            git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=seed)

            # The board's own copy, which starts knowing nothing about any
            # candidate: it learns them by fetching the project's remote, which
            # is what makes a submitted commit a public commit.
            cache = root / "board-cache.git"
            git("init", "--bare", "-q", str(cache))
            git("--git-dir", str(cache), "remote", "add", "origin", str(remote))

            worktrees: dict[str, Path] = {}
            for role in (*IMPLEMENTERS, "audit"):
                path = root / f"{role}-worktree"
                git("clone", "-q", str(remote), str(path))
                git("config", "user.email", f"{role}@example.invalid", cwd=path)
                git("config", "user.name", role, cwd=path)
                worktrees[role] = path
                # Every role's checkout reaches the project repository as
                # `origin`, which is the whole of the credential story now.
                assert git("remote", "get-url", "origin", cwd=path) == str(remote)
            checks += 1

            (root / "frames").mkdir()
            (root / "assets").mkdir()
            app = TicketBoardApp(
                root / "frames", root / "assets",
                project=PROJECT, ticket_prefix="PGU",
                commit_git_dir=str(cache),
                database_url=service,
            )
            cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

            import threading
            server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            write_client = str(ROOT / "scripts" / "ticket-board-write")
            board_env = {
                **os.environ,
                "TICKET_BOARD_WRITE_TOKEN": server.write_token,
                "TICKET_BOARD_SOCKET": "",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            }

            try:
                published: dict[str, str] = {}
                for index, role in enumerate(IMPLEMENTERS, start=1):
                    ticket = f"PGU-{index}"
                    t.seed_postgres_ticket(
                        admin, ticket, title=f"{role} work", state="in_progress", assignee=role
                    )
                    worktree = worktrees[role]
                    git("checkout", "-qb", f"feature/{ticket.lower()}", cwd=worktree)
                    commit = commit_in(worktree, f"{ticket.lower()}-change", role)

                    # THE PUBLISH: one push, by the implementer, with no
                    # Director, no sudo and no board write.
                    done = publish(worktree, role, ticket)
                    assert done.returncode == 0, done.stdout + done.stderr
                    ref = f"roles/{role}/{ticket.lower()}"
                    assert remote_ref(remote, ref) == commit, (role, done.stdout)
                    published[role] = commit
                    assert "sudo" not in (done.stdout + done.stderr).lower(), done.stdout

                    # Nothing was asked of the Director: no request, no wait.
                    assert publication_rows(admin) == [], publication_rows(admin)
                    assert awaiting(admin, ticket) == "", awaiting(admin, ticket)

                    # THE SUBMISSION: the exact public commit, through the real
                    # client, from the role's own worktree. The board has never
                    # seen this commit and fetches its own copy to check it.
                    submitted = subprocess.run(
                        [sys.executable, write_client, "--board-url", base,
                         "submit-to-audit", ticket, "--commit", commit],
                        cwd=str(worktree), text=True, capture_output=True,
                        env={**board_env, "TICKET_BOARD_CALLER_ROLE": role},
                    )
                    assert submitted.returncode == 0, submitted.stdout + submitted.stderr
                    after = app.get_ticket(ticket)
                    assert after["commit_hash"] == commit, after
                    assert after["state"] != "in_progress", after
                    assert publication_rows(admin) == [], publication_rows(admin)
                checks += 1

                # AUDIT fetches the exact commit into its own separate worktree
                # and reads the work there.
                auditor = worktrees["audit"]
                for role, commit in published.items():
                    git("fetch", "-q", "origin", commit, cwd=auditor)
                    assert git("rev-parse", f"{commit}^{{commit}}", cwd=auditor) == commit
                    git("checkout", "-q", "--detach", commit, cwd=auditor)
                    assert (auditor / f"pgu-{IMPLEMENTERS.index(role) + 1}-change.txt").exists()
                git("checkout", "-q", "--detach", "origin/main", cwd=auditor)
                checks += 1

                # A PUSH THAT FAILS stays the implementer's to retry: nothing is
                # recorded anywhere, the remote is untouched, and re-running it
                # after the cause is fixed is the ordinary response.
                worktree = worktrees["main"]
                t.seed_postgres_ticket(
                    admin, "PGU-9", title="Refused then retried", state="in_progress", assignee="main"
                )
                git("checkout", "-qb", "feature/pgu-9", cwd=worktree)
                retried_commit = commit_in(worktree, "pgu-9-change", "main")
                hook = remote / "hooks" / "pre-receive"
                hook.parent.mkdir(parents=True, exist_ok=True)
                hook.write_text("#!/bin/sh\necho 'remote refuses this' >&2\nexit 1\n", encoding="utf-8")
                hook.chmod(0o755)
                refused = publish(worktree, "main", "PGU-9")
                assert refused.returncode != 0, refused.stdout
                assert "unchanged" in (refused.stdout + refused.stderr), refused.stderr
                assert remote_ref(remote, "roles/main/pgu-9") == "", "nothing may have landed"
                assert publication_rows(admin) == [], publication_rows(admin)
                assert app.get_ticket("PGU-9")["commit_hash"] == "", app.get_ticket("PGU-9")
                hook.unlink()
                again = publish(worktree, "main", "PGU-9")
                assert again.returncode == 0, again.stdout + again.stderr
                assert remote_ref(remote, "roles/main/pgu-9") == retried_commit
                checks += 1

                # A REBUILT CANDIDATE replaces exactly what it replaces. An
                # ordinary push of divergent work is refused and says so; the
                # replacement is leased against what the remote holds now.
                git("reset", "-q", "--hard", "HEAD~1", cwd=worktree)
                rebuilt = commit_in(worktree, "pgu-9-rebuilt", "main")
                divergent = publish(worktree, "main", "PGU-9")
                assert divergent.returncode != 0, divergent.stdout
                assert "--replace" in (divergent.stdout + divergent.stderr), divergent.stderr
                assert remote_ref(remote, "roles/main/pgu-9") == retried_commit
                replaced = publish(worktree, "main", "PGU-9", "--replace")
                assert replaced.returncode == 0, replaced.stdout + replaced.stderr
                assert remote_ref(remote, "roles/main/pgu-9") == rebuilt
                checks += 1

                # INTEGRATION BRANCHES are not somewhere a candidate push can
                # land by mistyping a flag.
                for ref in ("main", "release", "trunk"):
                    refused = publish(worktree, "main", "PGU-9", "--ref", ref)
                    assert refused.returncode != 0, (ref, refused.stdout)
                    assert "integration branch" in (refused.stdout + refused.stderr), refused.stderr
                assert remote_ref(remote, "main") == git(
                    "--git-dir", str(remote), "rev-parse", "refs/heads/main"
                )
                # Nor is another role's namespace.
                elsewhere = publish(worktree, "main", "PGU-9", "--ref", "roles/app/sneaky")
                assert elsewhere.returncode != 0, elsewhere.stdout
                assert "namespace" in (elsewhere.stdout + elsewhere.stderr), elsewhere.stderr
                checks += 1

                # AND THE OLD PATH IS GONE from the happy path: nothing above
                # created a publication request, asked the Director for a
                # verdict, or left anyone waiting on one.
                assert publication_rows(admin) == [], publication_rows(admin)
                queued = t.psql(admin, """
SELECT coalesce(string_agg(DISTINCT kind, ','), '') FROM ticket_board.ticket_notification_queue;
""")
                assert "publication" not in queued, queued
                for ticket in ("PGU-1", "PGU-2", "PGU-3", "PGU-9"):
                    assert awaiting(admin, ticket) == "", (ticket, awaiting(admin, ticket))
                checks += 1
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    print(f"direct_candidate_push_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
