#!/usr/bin/env python3
"""SYRD-100 review: packaging output must never be tracked as source.

`switchyard-request-publication` writes its bundle into the publish outbox, and
the trusted bootstrap writes one into the worktree it was run from. Both are
build products of a commit that already exists.

One of them was swept into a source commit by `git add -A`, and the consequences
run further than a large file in a diff: every later handoff bundle records
complete history, so each one embedded the earlier bundle -- the revised pair
were roughly twice the size they should have been -- and installing that release
would have exported a generated bundle into the root-owned release tree.

`.gitignore` stops the ordinary accident. This is the part that fails loudly if
one is committed anyway, whether by a forced add or by a path the ignore rule
does not happen to match.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Anything git is tracking that looks like packaging output rather than source.
PACKAGING_OUTPUT_RE = re.compile(r"(?i)(?:^|/)[^/]*\.bundle$")


def tracked_paths() -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        text=True, capture_output=True, check=True,
    )
    return [path for path in proc.stdout.split("\0") if path]


def test_no_bundle_is_tracked_in_the_worktree() -> None:
    tracked = [path for path in tracked_paths() if PACKAGING_OUTPUT_RE.search(path)]
    assert not tracked, (
        "these are packaging output and must not be tracked as source: " + ", ".join(tracked)
    )


def test_the_ignore_rule_covers_what_the_bootstrap_writes() -> None:
    """The rule is checked against a real name, not read for plausibility."""
    written = ".switchyard-bootstrap-" + "f" * 40 + ".bundle"
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "-q", written],
        capture_output=True,
    )
    assert proc.returncode == 0, (
        f"{written} is what the trusted bootstrap writes into the worktree and git would "
        "not ignore it"
    )


def main() -> int:
    test_no_bundle_is_tracked_in_the_worktree()
    test_the_ignore_rule_covers_what_the_bootstrap_writes()
    print("ticket_board_no_packaging_output_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
