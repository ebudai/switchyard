"""A trusted commit cache, and the two ways a ref gets into one.

The board proves a publication by resolving `refs/remotes/origin/<ref>` in the
tenant's commit cache (SYRD-118), so a test that records a published verdict
has to leave behind what a real publication leaves behind. Both halves live
here rather than in each test, because the difference between them is the whole
rule: `publish` is what the privileged publisher's cache refresh writes after it
has pushed, and `claim` is the local branch `switchyard-request-publication`
creates when the ask is filed, which proves nothing.

`git` here is resolved once, by absolute path, so a test that puts a recording
shim on PATH sees only the calls the board itself makes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

GIT = shutil.which("git") or "/usr/bin/git"


def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        [GIT, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    return done.stdout.strip()


def build_cache(root: Path) -> tuple[Path, Path]:
    """An empty trusted cache, and a work repository with one commit on main."""
    work = root / "publication-work"
    work.mkdir(parents=True)
    git("init", "-q", "-b", "main", str(work))
    git("config", "user.email", "test@example.invalid", cwd=work)
    git("config", "user.name", "Test", cwd=work)
    commit_file(work, "one")
    cache = root / "publication-cache.git"
    git("init", "-q", "--bare", str(cache))
    return cache, work


def commit_file(work: Path, name: str) -> str:
    (work / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    git("add", f"{name}.txt", cwd=work)
    git("commit", "-qm", name, cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


def publish(cache: Path, work: Path, ref: str, commit_hash: str) -> None:
    """What the privileged publisher's cache refresh leaves behind."""
    git("--git-dir", str(cache), "fetch", "-q", str(work),
        f"+{commit_hash}:refs/remotes/origin/{ref}")


def claim(cache: Path, work: Path, ref: str, commit_hash: str) -> None:
    """What filing a publication request leaves behind: the ask, not a push."""
    git("--git-dir", str(cache), "fetch", "-q", str(work), f"+{commit_hash}:refs/heads/{ref}")
