#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


DEFAULT_TOTAL_THRESHOLD = 300
DEFAULT_RECLAIMABLE_THRESHOLD = 100
DEFAULT_RATE_LIMIT_DAYS = 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Warn when a repository accumulates too many git worktrees."
    )
    parser.add_argument("--repo-root", default="", help="Repository root to inspect.")
    parser.add_argument(
        "--total-threshold",
        type=int,
        default=int(os.environ.get("SWITCHYARD_WORKTREE_TOTAL_WARNING_THRESHOLD", DEFAULT_TOTAL_THRESHOLD)),
        help=f"Warn when total worktrees exceed this count (default: {DEFAULT_TOTAL_THRESHOLD}).",
    )
    parser.add_argument(
        "--reclaimable-threshold",
        type=int,
        default=int(
            os.environ.get("SWITCHYARD_WORKTREE_RECLAIMABLE_WARNING_THRESHOLD", DEFAULT_RECLAIMABLE_THRESHOLD)
        ),
        help=f"Warn when reclaimable worktrees exceed this count (default: {DEFAULT_RECLAIMABLE_THRESHOLD}).",
    )
    parser.add_argument(
        "--rate-limit-days",
        type=int,
        default=int(os.environ.get("SWITCHYARD_WORKTREE_WARNING_RATE_LIMIT_DAYS", DEFAULT_RATE_LIMIT_DAYS)),
        help=f"Warn at most once per repository for this many days (default: {DEFAULT_RATE_LIMIT_DAYS}).",
    )
    parser.add_argument(
        "--stamp-path",
        default=os.environ.get("SWITCHYARD_WORKTREE_WARNING_STAMP", ""),
        help="Path to the warning stamp file (default: git common hooks dir).",
    )
    parser.add_argument(
        "--cleanup-command",
        default=os.environ.get("SWITCHYARD_WORKTREE_CLEANUP_COMMAND", ""),
        help="Optional cleanup dry-run command. The helper appends --repo <repo> --json.",
    )
    return parser.parse_args(argv)


def run_git(repo_root: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if repo_root is not None:
        command.extend(["-C", str(repo_root)])
    command.extend(args)
    return subprocess.run(command, check=True, capture_output=True, text=True)


def resolve_repo_root(explicit: str) -> Path:
    if explicit:
        return Path(explicit).resolve()
    proc = run_git(None, "rev-parse", "--show-toplevel")
    return Path(proc.stdout.strip()).resolve()


def git_common_dir(repo_root: Path) -> Path:
    raw = run_git(repo_root, "rev-parse", "--git-common-dir").stdout.strip()
    path = Path(raw)
    return (path if path.is_absolute() else repo_root / path).resolve(strict=False)


def count_worktrees(repo_root: Path) -> int:
    output = run_git(repo_root, "worktree", "list", "--porcelain").stdout
    return sum(1 for line in output.splitlines() if line.startswith("worktree "))


def default_stamp_path(repo_root: Path) -> Path:
    return git_common_dir(repo_root) / "hooks" / ".switchyard-worktree-warning-stamp.json"


def warning_key(today: date | None = None) -> str:
    return (today or date.today()).isoformat()


def warning_recent(stamp_path: Path, *, today: date, rate_limit_days: int) -> bool:
    try:
        payload = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    try:
        last_warning = date.fromisoformat(str(payload.get("last_warning_date", "")))
    except ValueError:
        return False
    return last_warning + timedelta(days=rate_limit_days) > today


def write_warning_stamp(stamp_path: Path, *, key: str, total: int, reclaimable: int | None) -> None:
    payload = {
        "last_warning_date": key,
        "total_worktrees": total,
        "reclaimable_worktrees": reclaimable,
    }
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def cleanup_command(repo_root: Path, explicit: str) -> list[str]:
    if explicit:
        return [*shlex.split(explicit), "--repo", str(repo_root), "--json"]
    cleanup = repo_root / "scripts" / "ticket_board" / "worktree_cleanup.py"
    return [sys.executable, str(cleanup), "--repo", str(repo_root), "--json"]


def run_cleanup_analysis(repo_root: Path, explicit_command: str) -> dict[str, Any] | None:
    command = cleanup_command(repo_root, explicit_command)
    proc = subprocess.run(command, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        print(
            f"warning: worktree cleanup analysis failed; run manually: {' '.join(shlex.quote(part) for part in command)}",
            file=sys.stderr,
        )
        if proc.stderr.strip():
            print(f"warning: cleanup stderr: {proc.stderr.strip()}", file=sys.stderr)
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"warning: worktree cleanup analysis returned invalid JSON: {exc}", file=sys.stderr)
        return None


def emit_warning(
    *,
    repo_root: Path,
    total: int,
    total_threshold: int,
    reclaimable_threshold: int,
    cleanup_payload: dict[str, Any] | None,
    rate_limit_days: int,
) -> None:
    reclaimable = None
    dirty = None
    if cleanup_payload is not None:
        reclaimable = int(cleanup_payload.get("remove_candidates", 0) or 0)
        skipped = cleanup_payload.get("skipped", {})
        if isinstance(skipped, dict):
            dirty = int(skipped.get("dirty", 0) or 0)

    print(
        f"warning: repository has {total} git worktrees (soft limit {total_threshold}); "
        "the worktree directory is getting unwieldy.",
        file=sys.stderr,
    )
    if reclaimable is not None:
        if reclaimable > reclaimable_threshold:
            print(
                f"warning: cleanup planner found {reclaimable} reclaimable clean merged worktrees "
                f"(soft limit {reclaimable_threshold}); cleanup may have stopped working.",
                file=sys.stderr,
            )
        else:
            print(
                f"warning: cleanup planner found {reclaimable} reclaimable clean merged worktrees "
                f"(soft limit {reclaimable_threshold}).",
                file=sys.stderr,
            )
    if dirty is not None and dirty:
        print(
            f"warning: cleanup planner skipped {dirty} dirty worktrees; those are protected as unfinished work.",
            file=sys.stderr,
        )
    print(
        "warning: to inspect or clean up, run: "
        f"{sys.executable} {repo_root / 'scripts' / 'ticket_board' / 'worktree_cleanup.py'} "
        f"--repo {repo_root} --execute",
        file=sys.stderr,
    )
    print(
        f"warning: this worktree-count warning is rate-limited to once every {rate_limit_days} day(s) per repository.",
        file=sys.stderr,
    )


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
    except ValueError as exc:
        print(f"warning: invalid worktree warning configuration: {exc}", file=sys.stderr)
        return 0
    if args.total_threshold <= 0 or args.reclaimable_threshold < 0 or args.rate_limit_days < 0:
        print("warning: invalid worktree warning thresholds; skipping", file=sys.stderr)
        return 0

    try:
        repo_root = resolve_repo_root(args.repo_root)
        total = count_worktrees(repo_root)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: unable to inspect git worktree count: {exc}", file=sys.stderr)
        return 0

    if total <= args.total_threshold:
        return 0

    stamp = Path(args.stamp_path).expanduser() if args.stamp_path else default_stamp_path(repo_root)
    today = date.today()
    key = warning_key(today)
    if args.rate_limit_days and warning_recent(stamp, today=today, rate_limit_days=args.rate_limit_days):
        return 0

    cleanup_payload = run_cleanup_analysis(repo_root, args.cleanup_command)
    emit_warning(
        repo_root=repo_root,
        total=total,
        total_threshold=args.total_threshold,
        reclaimable_threshold=args.reclaimable_threshold,
        cleanup_payload=cleanup_payload,
        rate_limit_days=args.rate_limit_days,
    )
    reclaimable = None
    if cleanup_payload is not None:
        reclaimable = int(cleanup_payload.get("remove_candidates", 0) or 0)
    write_warning_stamp(stamp, key=key, total=total, reclaimable=reclaimable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
