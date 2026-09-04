#!/usr/bin/env python3
"""The commit pre-flight must say WHERE it looked (PGU-907).

`submit-to-audit` resolves the commit hash by running git in the current
working directory. When the commit is real but lives in another checkout it
used to report "unknown commit_hash", which is a claim git cannot support: it
cannot tell a wrong hash from a right hash in a different repository. The
message sent the reader off to check a sha that was fine.

Every path in these tests is created by the test. Nothing asserts against this
repository's own location or name -- that is the shape that made PGU-908's
assertion fail for anyone whose checkout was named after a CLI.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.write_client import TicketBoardWriteClient, TicketBoardWriteError


@contextlib.contextmanager
def pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def make_repo_with_origin(root: Path, name: str) -> tuple[Path, str]:
    """A checkout whose commit is present on its own origin."""
    bare = root / f"{name}.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = root / name
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(work)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "test@example.invalid"], check=True)
    (work / "file.txt").write_text("content\n", encoding="utf-8")
    git(work, "add", "file.txt")
    git(work, "commit", "-q", "-m", "commit")
    # A feature branch, not main: this machine installs a pre-push hook that
    # blocks direct pushes to origin/main, and it applies to any repository --
    # including a throwaway one in /tmp. The origin check only needs the commit
    # to be reachable from some refs/remotes/origin ref.
    git(work, "push", "-q", "origin", "HEAD:refs/heads/feature/pgu-907-fixture")
    git(work, "fetch", "-q", "origin")
    return work, git(work, "rev-parse", "HEAD")


def make_unrelated_repo(root: Path, name: str) -> Path:
    work = root / name
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    return work


def check(client: TicketBoardWriteClient, commit: str) -> str | None:
    """Run the pre-flight; return the error message, or None when it passes."""
    try:
        client._require_commit_pushed_to_origin(commit, operation="submit_to_audit")
    except TicketBoardWriteError as exc:
        return str(exc)
    return None


def run_checks(root: Path) -> None:
    client = TicketBoardWriteClient("http://127.0.0.1:1", "research")
    holder, real_commit = make_repo_with_origin(root, "holds-the-commit")
    elsewhere = make_unrelated_repo(root, "somewhere-else")

    # ACCEPTANCE 1 -- a real commit, resolved from an unrelated checkout.
    with pushd(elsewhere):
        message = check(client, real_commit)
    assert message is not None, "a commit absent from the cwd repo was accepted"
    assert "unknown commit_hash" not in message, message
    # Names the repository it actually searched -- a path this test created.
    assert str(elsewhere.resolve()) in message, message
    # ...and explains the coupling, so the remedy is in the message.
    assert "current working directory" in message, message
    assert "different checkout" in message, message
    # It does not claim to know which of the two readings applies.
    assert "or correct the hash" in message, message

    # ACCEPTANCE 2 -- a genuinely nonexistent sha still fails, and says so.
    with pushd(holder):
        bogus = check(client, "f" * 40)
    assert bogus is not None, "a nonexistent commit was accepted"
    assert "was not found" in bogus, bogus
    assert str(holder.resolve()) in bogus, bogus

    # ACCEPTANCE 3 -- from the checkout that holds it, nothing changed.
    with pushd(holder):
        assert check(client, real_commit) is None, "a valid pushed commit was rejected"

    # A commit that exists locally but is not on origin still fails on the
    # origin rule, not the resolution rule. The policy is untouched.
    (holder / "second.txt").write_text("more\n", encoding="utf-8")
    git(holder, "add", "second.txt")
    git(holder, "commit", "-q", "-m", "local only")
    local_only = git(holder, "rev-parse", "HEAD")
    with pushd(holder):
        unpushed = check(client, local_only)
    assert unpushed is not None, "an unpushed commit was accepted"
    assert "is not pushed to origin" in unpushed, unpushed

    # Not a git checkout at all: say that, rather than blaming the hash.
    outside = root / "not-a-repo"
    outside.mkdir()
    with pushd(outside):
        message = check(client, real_commit)
    assert message is not None
    assert "not a git checkout" in message, message
    assert str(outside.resolve()) in message, message


def main() -> int:
    if not shutil.which("git"):
        print("ticket_board_commit_repo_message_test: skipped - missing git")
        return 0
    env_home = tempfile.mkdtemp(prefix="ticket-board-commit-repo-home.")
    try:
        with tempfile.TemporaryDirectory(prefix="ticket-board-commit-repo.") as tmp:
            run_checks(Path(tmp).resolve())
    finally:
        shutil.rmtree(env_home, ignore_errors=True)
    print("ticket_board_commit_repo_message_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
