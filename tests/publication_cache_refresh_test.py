#!/usr/bin/env python3
"""SYRD-125: a published commit the board could not see yet.

SYRD-123 made an implementer publish by pushing, and left one step to somebody's
memory. On the refreshed SYRD-122 submission GitHub held
roles/app/syrd-122-presentation-titles-63648fb at 63648fbdafd1c..., the board's
own copy of the repository did not have that commit, and `submit-to-audit` was
refused until App fetched the cache by hand. A documented happy path that works
only if you know an undocumented repair is not a happy path.

Publishing already needs the network; submitting should not need it again. So
the push wrapper, having just had the remote confirm the exact ref, brings the
board's repository up to it -- asking the board which repository that is, and
fetching from that repository's own remote so no credential of the role's is
involved and nobody can point the refresh somewhere else.

Real repositories, the real board handler, the real programs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

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


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, text=True, capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    if check:
        assert done.returncode == 0, (args, done.stderr or done.stdout)
    return done.stdout.strip()


def publish(
    worktree: Path, *args: str, board_url: str, path_prefix: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        [sys.executable, str(PUBLISH), "--role", "main", "--board-url", board_url, *args],
        cwd=str(worktree), text=True, capture_output=True, env=env,
    )


#: A git that lets one push land and then moves the ref out from under it, which
#: is the race the read-back after a push exists to catch. Written as a shim on
#: PATH so the program under test runs its own code against a remote that really
#: does change between the two commands.
OVERTAKING_GIT = """#!/usr/bin/env python3
import os, subprocess, sys

argv = sys.argv[1:]
done = subprocess.run(["{real_git}", *argv])
if done.returncode == 0 and "push" in argv and not os.path.exists("{stamp}"):
    open("{stamp}", "w").close()
    subprocess.run([
        "{real_git}", "--git-dir", "{remote}", "update-ref", "refs/heads/{ref}", "{other}",
    ], check=True)
raise SystemExit(done.returncode)
"""


def commit_in(worktree: Path, name: str) -> str:
    (worktree / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    git("add", f"{name}.txt", cwd=worktree)
    git("commit", "-qm", name, cwd=worktree)
    return git("rev-parse", "HEAD", cwd=worktree)


def cache_knows(cache: Path, commit: str) -> bool:
    done = subprocess.run(
        ["git", "--git-dir", str(cache), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True, text=True,
    )
    return done.returncode == 0


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="syrd125-cache-", shutdown="immediate") as cluster:
        dbname = "syrd125_cache"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory(prefix="syrd125-") as tmpdir:
            root = Path(tmpdir)
            remote = root / "project.git"
            git("init", "--bare", "-q", "-b", "main", str(remote))
            seed = root / "seed"
            git("init", "-q", "-b", "main", str(seed))
            git("config", "user.email", "seed@example.invalid", cwd=seed)
            git("config", "user.name", "Seed", cwd=seed)
            commit_in(seed, "readme")
            git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=seed)

            # The board's copy: configured against the project repository, as a
            # tenant's cache is configured against the project's public URL.
            cache = root / "board-cache.git"
            git("init", "--bare", "-q", str(cache))
            git("--git-dir", str(cache), "remote", "add", "origin", str(remote))

            worktree = root / "main-worktree"
            git("clone", "-q", str(remote), str(worktree))
            git("config", "user.email", "main@example.invalid", cwd=worktree)
            git("config", "user.name", "main", cwd=worktree)

            (root / "frames").mkdir()
            (root / "assets").mkdir()
            app = TicketBoardApp(
                root / "frames", root / "assets", project=PROJECT, ticket_prefix="PGU",
                commit_git_dir=str(cache), database_url=service,
            )
            cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            write_client = str(ROOT / "scripts" / "ticket-board-write")
            board_env = {
                **os.environ, "TICKET_BOARD_WRITE_TOKEN": server.write_token,
                "TICKET_BOARD_SOCKET": "", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            }

            try:
                # The board says where it verifies, because nothing else tells a
                # role pane and guessing would refresh a repository nobody reads.
                import urllib.request
                with urllib.request.urlopen(base + "/api/client-config", timeout=5) as response:
                    config = json.loads(response.read().decode("utf-8"))
                assert config["commit_repositories"] == [str(cache)], config
                checks += 1

                # A FRESH CANDIDATE the board has never seen. This is SYRD-122's
                # shape exactly: pushed, public, and absent from the cache.
                t.seed_postgres_ticket(admin, "PGU-1", title="Fresh candidate",
                                       state="in_progress", assignee="main")
                git("checkout", "-qb", "feature/pgu-1", cwd=worktree)
                commit = commit_in(worktree, "pgu-1-change")
                assert not cache_knows(cache, commit), "the cache must start out not knowing it"

                done = publish(worktree, "PGU-1", board_url=base)
                assert done.returncode == 0, done.stdout + done.stderr
                assert cache_knows(cache, commit), done.stdout + done.stderr
                # And under the namespace a published ref belongs in.
                assert git("--git-dir", str(cache), "rev-parse",
                           "refs/remotes/origin/roles/main/pgu-1^{commit}") == commit
                checks += 1

                # SUBMISSION IS THEN A LOCAL QUESTION: nobody fetched anything by
                # hand, and the board accepts the exact public commit.
                submitted = subprocess.run(
                    [sys.executable, write_client, "--board-url", base,
                     "submit-to-audit", "PGU-1", "--commit", commit],
                    cwd=str(worktree), text=True, capture_output=True,
                    env={**board_env, "TICKET_BOARD_CALLER_ROLE": "main"},
                )
                assert submitted.returncode == 0, submitted.stdout + submitted.stderr
                assert app.get_ticket("PGU-1")["commit_hash"] == commit
                checks += 1

                # RETRY: running it again when everything already landed is
                # quiet, still verifies, and leaves the cache correct.
                again = publish(worktree, "PGU-1", board_url=base)
                assert again.returncode == 0, again.stdout + again.stderr
                assert "already at" in again.stdout, again.stdout
                assert cache_knows(cache, commit)
                checks += 1

                # A REFRESH THAT CANNOT RUN is reported and does not pretend.
                # The board names a repository that is not there; publication
                # has happened, so the message has to say both halves.
                t.seed_postgres_ticket(admin, "PGU-2", title="Unreachable cache",
                                       state="in_progress", assignee="main")
                git("checkout", "-qb", "feature/pgu-2", cwd=worktree)
                second = commit_in(worktree, "pgu-2-change")
                moved = root / "board-cache-moved.git"
                cache.rename(moved)
                try:
                    broken = publish(worktree, "PGU-2", board_url=base)
                    assert broken.returncode != 0, broken.stdout
                    text = broken.stdout + broken.stderr
                    assert "IS published" in text, text
                    assert "Re-running this command is safe" in text, text
                    # The push really did land: the failure is about the cache.
                    assert git("--git-dir", str(remote), "rev-parse",
                               "refs/heads/roles/main/pgu-2") == second
                    # And the ticket did not move.
                    assert app.get_ticket("PGU-2")["commit_hash"] == "", app.get_ticket("PGU-2")
                finally:
                    moved.rename(cache)
                # Re-running after the cause is fixed completes it.
                repaired = publish(worktree, "PGU-2", board_url=base)
                assert repaired.returncode == 0, repaired.stdout + repaired.stderr
                assert cache_knows(cache, second)
                checks += 1

                # THE NEGATIVE CASE the ticket names: the push lands and the
                # public ref then does NOT resolve to the commit that was sent.
                # Nothing may be refreshed, because a cache holding a commit the
                # public ref does not carry would be the board believing
                # something untrue -- and this is the case that decides the
                # order of the two steps, not merely that both happen.
                t.seed_postgres_ticket(admin, "PGU-3", title="Overtaken",
                                       state="in_progress", assignee="main")
                git("checkout", "-qb", "feature/pgu-3", cwd=worktree)
                third = commit_in(worktree, "pgu-3-change")
                other = root / "other"
                git("clone", "-q", str(remote), str(other))
                git("config", "user.email", "other@example.invalid", cwd=other)
                git("config", "user.name", "other", cwd=other)
                git("checkout", "-qb", "elsewhere", cwd=other)
                theirs = commit_in(other, "someone-elses-work")
                git("push", "-q", str(remote), f"{theirs}:refs/heads/theirs", cwd=other)

                shim_dir = root / "shim"
                shim_dir.mkdir()
                shim = shim_dir / "git"
                shim.write_text(
                    OVERTAKING_GIT.format(
                        real_git=git("--exec-path") and subprocess.run(
                            ["bash", "-lc", "command -v git"], capture_output=True, text=True,
                        ).stdout.strip(),
                        stamp=str(root / "overtaken.stamp"),
                        remote=str(remote), ref="roles/main/pgu-3", other=theirs,
                    ),
                    encoding="utf-8",
                )
                shim.chmod(0o755)

                overtaken = publish(worktree, "PGU-3", board_url=base, path_prefix=shim_dir)
                assert overtaken.returncode != 0, overtaken.stdout
                text = overtaken.stdout + overtaken.stderr
                assert "rather than the" in text, text
                assert "Do NOT submit this commit" in text, text
                # The ref really was overtaken, and the commit this run pushed
                # never reached the board's copy.
                assert git("--git-dir", str(remote), "rev-parse",
                           "refs/heads/roles/main/pgu-3") == theirs
                assert not cache_knows(cache, third), "an unpublished commit reached the cache"
                checks += 1

                # And the board refuses it too, so the two agree: a commit that
                # is not public is not submittable.
                refused = subprocess.run(
                    [sys.executable, write_client, "--board-url", base,
                     "submit-to-audit", "PGU-3", "--commit", third],
                    cwd=str(worktree), text=True, capture_output=True,
                    env={**board_env, "TICKET_BOARD_CALLER_ROLE": "main"},
                )
                assert refused.returncode != 0, refused.stdout
                assert app.get_ticket("PGU-3")["commit_hash"] == ""
                checks += 1
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    print(f"publication_cache_refresh_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
