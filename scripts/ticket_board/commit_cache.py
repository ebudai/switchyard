"""Reads and refreshes of the board's trusted commit cache.

Every function takes the root-configured repository paths explicitly and runs
local git against them. The only network use is refresh_commit_repos fetching
each repository from its own configured origin, bounded by a timeout; no
caller can name a remote. TicketBoardApp keeps commit_hash policy, publication
authority and every database transaction.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Where a publication proves itself (SYRD-118). The privileged publisher writes
#: this namespace into the tenant's trusted commit cache only after it has
#: pushed and read the exact commit back from the public remote, so a ref here
#: is the board's own sight of a completed publication. `refs/heads/<ref>` in
#: the same repository is not: `switchyard-request-publication` creates that
#: locally when the ask is filed, and accepting it would prove only that
#: somebody asked.
PUBLISHED_REF_NAMESPACE = "refs/remotes/origin"
#: How long the board will wait for its own copy of the repository to catch up
#: with a commit somebody has just published. Short enough that a submission
#: does not hang on an unreachable forge, long enough for an ordinary fetch.
COMMIT_REFRESH_TIMEOUT_SECONDS = 30
#: Branch names this will hand to git. The board already refuses anything else
#: when the ask is filed; asked again here so no ref shape can become an
#: argument to the command that is supposed to be reading it.
PUBLISHABLE_REF = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*(?:/[0-9A-Za-z][0-9A-Za-z._-]*)*")


def commit_repo_git_args(commit_git_dir: Path) -> list[str]:
    if (commit_git_dir / ".git").exists():
        return ["git", "-C", str(commit_git_dir)]
    if not commit_git_dir.exists():
        raise ValueError(f"commit_hash verification repository not found: {commit_git_dir}")
    return ["git", f"--git-dir={commit_git_dir}"]


def readable_commit_repos(commit_git_dirs: tuple[Path, ...]) -> bool:
    for commit_git_dir in commit_git_dirs:
        try:
            commit_repo_git_args(commit_git_dir)
        except ValueError:
            continue
        return True
    return False


def cache_ref_commit(commit_git_dirs: tuple[Path, ...], refname: str) -> str:
    candidate = refname.strip()
    if not PUBLISHABLE_REF.fullmatch(candidate) or ".." in candidate:
        return ""
    for commit_git_dir in commit_git_dirs:
        try:
            git_args = commit_repo_git_args(commit_git_dir)
        except ValueError:
            continue
        resolved = subprocess.run(
            [*git_args, "rev-parse", "--verify", "--quiet", "--end-of-options",
             f"{candidate}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            return resolved.stdout.strip().lower()
    return ""


def published_ref_commit(commit_git_dirs: tuple[Path, ...], ref: str) -> str:
    """Resolve one published ref in the tenant's trusted commit cache.

    Local git against the root-configured repositories and nothing else: no
    remote is named, contacted, or taken at its word, so this stays safe to
    re-run and cannot be pointed at a remote of the caller's choosing.
    """
    return cache_ref_commit(commit_git_dirs, f"{PUBLISHED_REF_NAMESPACE}/{ref}")


def published_ref_absence(commit_git_dirs: tuple[Path, ...], ref: str, commit: str) -> str:
    """Why the proof is missing, said precisely enough to act on."""
    local = cache_ref_commit(commit_git_dirs, f"refs/heads/{ref}")
    if local == commit:
        return (
            f"the trusted commit cache has no {PUBLISHED_REF_NAMESPACE}/{ref}. Its local "
            f"refs/heads/{ref} is at {commit[:12]}, but that branch is what filing the "
            "request creates, not evidence that anything reached the remote."
        )
    return f"the trusted commit cache has no {PUBLISHED_REF_NAMESPACE}/{ref}."


def resolve_known_commit(commit_git_dirs: tuple[Path, ...], value: str) -> tuple[str, list[Path]]:
    """The commit as this board's own copies of the repository resolve it."""
    missing_repos: list[Path] = []
    for commit_git_dir in commit_git_dirs:
        try:
            git_args = commit_repo_git_args(commit_git_dir)
        except ValueError:
            missing_repos.append(commit_git_dir)
            continue
        proc = subprocess.run(
            [*git_args, "cat-file", "-e", f"{value}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            continue
        resolved = subprocess.run(
            [*git_args, "rev-parse", "--verify", f"{value}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            return resolved.stdout.strip(), missing_repos
    return "", missing_repos


def refresh_commit_repos(commit_git_dirs: tuple[Path, ...]) -> bool:
    """Fetch each verification repository from its own configured remote.

    Bounded and credential-free by construction: the remote is whatever that
    repository already names -- for a tenant's cache, the project's public
    URL -- and nothing here is told a remote by a caller, so a submission
    cannot point this at a repository of its choosing. A fetch that fails or
    hangs is not an error in itself; it only means the commit stays unknown,
    which the caller is then told plainly.
    """
    refreshed = False
    for commit_git_dir in commit_git_dirs:
        try:
            git_args = commit_repo_git_args(commit_git_dir)
        except ValueError:
            continue
        try:
            fetched = subprocess.run(
                [*git_args, "fetch", "--quiet", "--prune", "origin"],
                capture_output=True,
                text=True,
                check=False,
                timeout=COMMIT_REFRESH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            continue
        refreshed = refreshed or fetched.returncode == 0
    return refreshed


def _first_answer(commit_git_dirs: tuple[Path, ...], args: list[str]) -> str | None:
    """Stdout of `args` from the first configured repository that answers, else None."""
    for commit_git_dir in commit_git_dirs:
        try:
            git_args = commit_repo_git_args(commit_git_dir)
        except ValueError:
            continue
        answer = subprocess.run([*git_args, *args], capture_output=True, text=True, check=False)
        if answer.returncode == 0:
            return answer.stdout
    return None


def commit_parents(commit_git_dirs: tuple[Path, ...], commit: str) -> list[str] | None:
    """The parents of a full commit id in the trusted cache, or None when no repository has it (SYRD-624)."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    listed = _first_answer(commit_git_dirs, ["rev-list", "--parents", "-n", "1", "--end-of-options", commit])
    words = (listed or "").split()
    return [word.lower() for word in words[1:]] if words and words[0].lower() == commit else None


def changed_entries(commit_git_dirs: tuple[Path, ...], old: str, new: str) -> list[tuple[str, str, str, str]] | None:
    """Each change `new` makes against `old`, both full commit ids in the trusted cache, as (old mode, new mode,
    status, path); a rename is its removal and its addition. None when no repository can say (SYRD-624)."""
    if not (re.fullmatch(r"[0-9a-f]{40}", old) and re.fullmatch(r"[0-9a-f]{40}", new)):
        return None
    listed = _first_answer(commit_git_dirs, RAW_DIFF_ARGS + [old, new, "--"])
    return None if listed is None else parse_raw_diff(listed)


#: `git diff` arguments whose output parse_raw_diff reads.
RAW_DIFF_ARGS = ["diff", "--raw", "--no-renames", "--no-abbrev", "-z"]


def parse_raw_diff(listed: str) -> list[tuple[str, str, str, str]]:
    """`git diff RAW_DIFF_ARGS` output as (old mode, new mode, status, path) entries."""
    fields = listed.split("\0")
    entries = []
    for at in range(0, len(fields) - 1, 2):
        if not fields[at]:
            break
        old_mode, new_mode, _old_blob, _new_blob, status = fields[at].lstrip(":").split()
        entries.append((old_mode, new_mode, status, fields[at + 1]))
    return entries
