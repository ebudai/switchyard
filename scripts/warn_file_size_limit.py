#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from report_file_size_limit import DEFAULT_LINE_LIMIT

# The board's own measurement (SYRD-541): copied beside this helper in a hooks
# directory, found in the package when run from a checkout.
try:
    from file_size_policy import WARN_LINES, in_scope, physical_lines
except ImportError:  # run from scripts/ in a checkout
    from ticket_board.file_size_policy import WARN_LINES, in_scope, physical_lines


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Warn when staged source files exceed the soft line limit."
    )
    parser.add_argument(
        "--repo-root",
        default="",
        help="Repository root to inspect (default: current git toplevel).",
    )
    parser.add_argument(
        "--warn-limit",
        type=int,
        default=WARN_LINES,
        help=f"Warn from this many lines, before the review limit (default: {WARN_LINES}).",
    )
    parser.add_argument(
        "--line-limit",
        type=int,
        default=DEFAULT_LINE_LIMIT,
        help=f"Soft line limit (default: {DEFAULT_LINE_LIMIT}).",
    )
    return parser.parse_args(argv)


def git_output(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def resolve_repo_root(explicit: str) -> Path:
    if explicit:
        return Path(explicit).resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(proc.stdout.strip()).resolve()


def staged_paths(repo_root: Path) -> list[str]:
    output = git_output(
        repo_root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )
    return [entry for entry in output.split("\0") if entry]


def line_count_for_staged_path(repo_root: Path, path: str, *, at: str = "") -> int | None:
    """Physical lines of `path` as staged (the index), or at commit `at`; None if absent or binary.

    The staged blob is what the commit will contain -- never the working tree,
    whose unstaged edits are not being committed.
    """
    proc = subprocess.run(["git", "-C", str(repo_root), "show", f"{at}:{path}"], capture_output=True)
    if proc.returncode != 0:
        return None
    return physical_lines(proc.stdout)


def head_path(repo_root: Path, path: str) -> str:
    """The path this staged file had at HEAD (a rename keeps its identity), or itself."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--name-status", "-M", "-z"],
        capture_output=True,
    )
    fields = proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    for index, status in enumerate(fields):
        if status.startswith("R") and index + 2 < len(fields) and fields[index + 2] == path:
            return fields[index + 1]
    return path


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.line_limit <= 0:
        print("[file-size-warning] line limit must be positive", file=sys.stderr)
        return 0

    try:
        repo_root = resolve_repo_root(args.repo_root)
        paths = staged_paths(repo_root)
    except Exception as exc:  # noqa: BLE001
        print(f"[file-size-warning] unable to inspect staged files: {exc}", file=sys.stderr)
        return 0

    for path in sorted(paths):
        if not in_scope(path):
            continue
        current_lines = line_count_for_staged_path(repo_root, path)
        if current_lines is None or current_lines < min(args.warn_limit, args.line_limit + 1):
            continue
        before = line_count_for_staged_path(repo_root, head_path(repo_root, path), at="HEAD") or 0
        change = f"was {before} at HEAD ({current_lines - before:+d})"
        if current_lines > args.line_limit:
            print(
                f"warning: {path} is {current_lines} lines (soft limit {args.line_limit}) - consider splitting.",
                file=sys.stderr,
            )
            print(
                f"note: {path} {change}; growth past the limit needs a split or a Director exception "
                "before integration.",
                file=sys.stderr,
            )
        else:
            print(
                f"note: {path} is {current_lines} lines, nearing the {args.line_limit}-line review limit; {change}.",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
