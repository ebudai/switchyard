"""Read-only git in the board's trusted commit cache, as the board's own account (SYRD-571).

The board runs as its service account and the cache belongs to the tenant,
reachable through one named, read-only ACL. Git's repository discovery
refuses a repository another account owns ("dubious ownership"), and the size
review ran `git -C <cache>`, so every scan of every candidate failed. A
`CacheReader` is what the size review measures through instead.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


#: The only git the size review runs in the cache: reads of objects
#: and refs. Nothing here writes, fetches or checks anything out.
CACHE_READS = frozenset({"rev-parse", "merge-base", "cat-file", "diff", "ls-tree"})


@dataclass(frozen=True)
class CacheReader:
    """Read-only git in one configured cache.

    The repository is named exactly -- its git directory with `--git-dir`,
    never discovered -- and that one path is `safe.directory` on the command
    line, never in any config file. The cache's config is its owner's to
    write, so nothing configured there may run a program in the board or
    change what it reads: a diff must say --no-ext-diff --no-textconv, replace
    refs are ignored, a missing object is never fetched from a promisor remote
    (whose upload-pack program the config names), the global config (the
    board's HOME is the owner's) is not read, and no inherited GIT_* variable
    redirects any of it.
    """

    git_dir: Path

    def command_prefix(self) -> list[str]:
        return [
            "git", "-c", f"safe.directory={self.git_dir}",
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "protocol.ext.allow=never", "-c", "credential.helper=",
            f"--git-dir={self.git_dir}", "--no-replace-objects",
        ]

    @staticmethod
    def environment() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_NO_LAZY_FETCH="1")
        return env

    def git_command(self, args: Sequence[str]) -> tuple[list[str], dict[str, str]]:
        if not args or args[0] not in CACHE_READS:
            raise ValueError(f"not a read of the trusted commit cache: git {' '.join(args[:1])}")
        if args[0] == "diff" and not {"--no-ext-diff", "--no-textconv"} <= set(args):
            raise ValueError("a diff in the trusted commit cache must not run the cache's diff programs")
        return [*self.command_prefix(), *args], self.environment()


def cache_reader(commit_git_dir: Path) -> CacheReader:
    """The reader for one configured cache; ValueError says why there is none.

    A bare repository is its own git directory; a checkout's is its `.git`
    directory. Either, if it is a symlink, could point the board at another
    account's repository, so it is refused, as is a linked checkout's `.git`
    file. Whether the board can open it is asked of its own account, and a
    refusal is a PermissionError, which access grants repair.
    """
    path = Path(commit_git_dir)
    try:
        git_dir = path
        if stat.S_ISDIR(os.lstat(path).st_mode):
            try:
                os.lstat(path / ".git")
                git_dir = path / ".git"
            except FileNotFoundError:
                pass
        mode = os.lstat(git_dir).st_mode
    except FileNotFoundError:
        raise ValueError(f"the trusted commit cache {path} does not exist") from None
    except PermissionError as exc:
        raise PermissionError(f"the board cannot read its trusted commit cache {path}: {exc.strerror}") from None
    if stat.S_ISLNK(mode):
        raise ValueError(f"the trusted commit cache {git_dir} is a symlink; the board reads only the repository it names")
    if not stat.S_ISDIR(mode):
        raise ValueError(f"the trusted commit cache {git_dir} is not a git directory")
    for needed in (git_dir, git_dir / "objects", git_dir / "refs"):
        if needed != git_dir and not needed.is_dir():
            raise ValueError(f"the trusted commit cache {git_dir} is not a git repository: it has no {needed.name}")
        if not os.access(needed, os.R_OK | os.X_OK):
            raise PermissionError(f"the board cannot read its trusted commit cache {git_dir}: "
                                  f"no read access to {needed}")
    return CacheReader(git_dir)
