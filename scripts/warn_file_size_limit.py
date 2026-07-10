#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_LINE_LIMIT = 1250
SOURCE_EXTENSIONS = {".cpp", ".cu", ".fish", ".h", ".hpp", ".py", ".sh"}
SKIP_PREFIXES = ("external/", "third_party/")


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


def should_check_path(path: str) -> bool:
    if path.startswith(SKIP_PREFIXES):
        return False
    return Path(path).suffix.lower() in SOURCE_EXTENSIONS


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


def line_count_for_staged_path(repo_root: Path, path: str) -> int | None:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "show", f":{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if proc.returncode != 0:
        return None
    return sum(1 for _ in proc.stdout.splitlines())


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
        if not should_check_path(path):
            continue
        current_lines = line_count_for_staged_path(repo_root, path)
        if current_lines is None or current_lines <= args.line_limit:
            continue
        print(
            f"warning: {path} is {current_lines} lines (soft limit {args.line_limit}) - consider splitting.",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
