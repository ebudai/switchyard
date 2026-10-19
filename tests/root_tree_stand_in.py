"""Fixture trees stood in as root's own, for suites that run a root flow as namespace or pretended root (SYRD-630).

As root, the git chokepoint runs git itself only in a tree root alone controls, proven by descriptor from `/`. A
suite's source checkout, deploy checkout or fixture release cannot be one -- under a user namespace `/` is not
root's, and a suite that only sets euid 0 runs as its own account -- though each stands in for an installed release
that is. `roots_own` says so for the trees it names and nothing else: a link in one is still refused, and every
other target is proven as on a host. The proof itself is `owner_git_root_account_test`'s.
"""

from __future__ import annotations

import contextlib
from pathlib import Path


@contextlib.contextmanager
def roots_own(*trees: Path | str | None):
    from scripts import owner_git

    real = owner_git._walk_git_target
    named = [str(tree).rstrip("/") for tree in trees if tree]

    def walk(text: str) -> tuple[str, bool]:
        problem, controlled = real(text)
        if any(text == tree or text.startswith(tree + "/") for tree in named):
            return problem, not problem
        return problem, controlled

    owner_git._walk_git_target = walk
    try:
        yield
    finally:
        owner_git._walk_git_target = real
