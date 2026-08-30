"""Safe cleanup planning for accumulated git worktrees."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    head: str = ""
    branch_ref: str = ""

    @property
    def branch(self) -> str:
        if self.branch_ref.startswith("refs/heads/"):
            return self.branch_ref.removeprefix("refs/heads/")
        return self.branch_ref


@dataclass(frozen=True)
class WorktreeDecision:
    entry: WorktreeEntry
    action: str
    reason: str
    dirty_status: str = ""


@dataclass(frozen=True)
class CleanupPlan:
    decisions: tuple[WorktreeDecision, ...]

    def candidates(self) -> list[WorktreeDecision]:
        return [decision for decision in self.decisions if decision.action == "remove"]

    def summary(self) -> dict[str, Any]:
        skipped = Counter(decision.reason for decision in self.decisions if decision.action != "remove")
        return {
            "total": len(self.decisions),
            "remove_candidates": len(self.candidates()),
            "skipped": dict(sorted(skipped.items())),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "decisions": [
                {
                    "path": str(decision.entry.path),
                    "head": decision.entry.head,
                    "branch": decision.entry.branch,
                    "action": decision.action,
                    "reason": decision.reason,
                    "dirty_status": decision.dirty_status,
                }
                for decision in self.decisions
            ],
        }


StatusFunc = Callable[[Path], str]
MergedFunc = Callable[[str], bool | None]
Runner = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]


def run_command(args: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


def parse_worktree_porcelain(text: str) -> list[WorktreeEntry]:
    entries: list[WorktreeEntry] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(_entry_from_block(current))
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        entries.append(_entry_from_block(current))
    return entries


def _entry_from_block(block: dict[str, str]) -> WorktreeEntry:
    path = block.get("worktree")
    if not path:
        raise ValueError(f"worktree porcelain block lacks path: {block!r}")
    return WorktreeEntry(path=Path(path), head=block.get("HEAD", ""), branch_ref=block.get("branch", ""))


def read_registered_worktrees(repo: Path, *, runner: Runner = run_command) -> list[WorktreeEntry]:
    proc = runner(["git", "-C", str(repo), "worktree", "list", "--porcelain"], None)
    if proc.returncode != 0:
        raise SystemExit(f"git worktree list failed: {proc.stderr.strip()}")
    return parse_worktree_porcelain(proc.stdout)


def prune_missing_worktrees(repo: Path, *, runner: Runner = run_command) -> None:
    proc = runner(["git", "-C", str(repo), "worktree", "prune"], None)
    if proc.returncode != 0:
        raise SystemExit(f"git worktree prune failed: {proc.stderr.strip()}")


def tmux_active_pane_paths(*, runner: Runner = run_command) -> set[Path]:
    proc = runner(["tmux", "list-panes", "-a", "-F", "#{pane_current_path}"], None)
    if proc.returncode != 0:
        raise SystemExit(f"tmux pane inspection failed: {proc.stderr.strip()}")
    return {_normalize_path(Path(line)) for line in proc.stdout.splitlines() if line.strip()}


def git_status_porcelain(path: Path, *, runner: Runner = run_command) -> str:
    proc = runner(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"], None)
    if proc.returncode != 0:
        return f"STATUS-FAILED: {proc.stderr.strip()}"
    return proc.stdout.strip()


def head_merged_to(head: str, *, repo: Path, base_ref: str, runner: Runner = run_command) -> bool | None:
    proc = runner(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, base_ref], None)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def plan_cleanup(
    entries: Iterable[WorktreeEntry],
    *,
    repo: Path,
    base_ref: str = "origin/main",
    active_pane_paths: Iterable[Path] = (),
    shared_checkout: Path | None = None,
    protected_paths: Iterable[Path] = (),
    protected_prefixes: Iterable[Path] = (),
    status_func: StatusFunc | None = None,
    merged_func: MergedFunc | None = None,
) -> CleanupPlan:
    entries = list(entries)
    if not entries:
        return CleanupPlan(())
    shared = _normalize_path(shared_checkout or entries[0].path)
    protected_exact = {_normalize_path(path) for path in protected_paths}
    protected_exact.add(shared)
    protected_exact.add(_normalize_path(Path.cwd()))
    active = {_normalize_path(path) for path in active_pane_paths}
    prefixes = tuple(_normalize_path(path) for path in protected_prefixes)
    status = status_func or (lambda path: git_status_porcelain(path))
    merged = merged_func or (lambda head: head_merged_to(head, repo=repo, base_ref=base_ref))

    decisions: list[WorktreeDecision] = []
    for entry in entries:
        path = _normalize_path(entry.path)
        normalized_entry = WorktreeEntry(path=path, head=entry.head, branch_ref=entry.branch_ref)
        if path in protected_exact:
            decisions.append(WorktreeDecision(normalized_entry, "skip", "protected"))
            continue
        if any(_is_relative_to(path, prefix) for prefix in prefixes):
            decisions.append(WorktreeDecision(normalized_entry, "skip", "protected-prefix"))
            continue
        if any(_is_relative_to(active_path, path) for active_path in active):
            decisions.append(WorktreeDecision(normalized_entry, "skip", "running-pane"))
            continue
        if not path.exists():
            decisions.append(WorktreeDecision(normalized_entry, "skip", "missing-after-prune"))
            continue

        dirty_status = status(path)
        if dirty_status:
            decisions.append(WorktreeDecision(normalized_entry, "skip", "dirty", dirty_status=dirty_status))
            continue
        if not entry.head:
            decisions.append(WorktreeDecision(normalized_entry, "skip", "unknown-head"))
            continue
        merge_state = merged(entry.head)
        if merge_state is True:
            decisions.append(WorktreeDecision(normalized_entry, "remove", "merged-clean"))
        elif merge_state is False:
            decisions.append(WorktreeDecision(normalized_entry, "skip", "unmerged"))
        else:
            decisions.append(WorktreeDecision(normalized_entry, "skip", "merge-status-unknown"))
    return CleanupPlan(tuple(decisions))


def execute_plan(
    plan: CleanupPlan,
    *,
    repo: Path,
    delete_local_branches: bool = True,
    runner: Runner = run_command,
) -> dict[str, Any]:
    removed: list[str] = []
    failed: list[dict[str, str]] = []
    branch_deleted: list[str] = []
    branch_delete_failed: list[dict[str, str]] = []
    for decision in plan.candidates():
        path = decision.entry.path
        proc = runner(["git", "-C", str(repo), "worktree", "remove", str(path)], None)
        if proc.returncode != 0:
            failed.append({"path": str(path), "stderr": proc.stderr.strip()})
            continue
        removed.append(str(path))
        branch = decision.entry.branch
        if delete_local_branches and branch:
            branch_proc = runner(["git", "-C", str(repo), "branch", "-d", branch], None)
            if branch_proc.returncode == 0:
                branch_deleted.append(branch)
            else:
                branch_delete_failed.append({"branch": branch, "stderr": branch_proc.stderr.strip()})
    return {
        "removed": removed,
        "failed": failed,
        "local_branches_deleted": branch_deleted,
        "local_branch_delete_failed": branch_delete_failed,
    }


def default_protected_prefixes() -> list[Path]:
    return [Path.home() / ".claude" / "worktrees" / "pgu-team"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely remove stale clean merged PGU git worktrees.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository to inspect.")
    parser.add_argument("--base-ref", default="origin/main", help="Reference that candidate heads must be merged into.")
    parser.add_argument("--execute", action="store_true", help="Actually remove candidate worktrees. Default is dry-run.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--keep-local-branches", action="store_true", help="Do not delete local branches after removing worktrees.")
    parser.add_argument("--prune", action="store_true", help="Run git worktree prune before planning. Implied by --execute.")
    parser.add_argument("--protected-path", action="append", type=Path, default=[], help="Exact path to protect.")
    parser.add_argument("--protected-prefix", action="append", type=Path, default=[], help="Path prefix to protect.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    if args.execute or args.prune:
        prune_missing_worktrees(repo)
    entries = read_registered_worktrees(repo)
    active_paths = tmux_active_pane_paths()
    plan = plan_cleanup(
        entries,
        repo=repo,
        base_ref=args.base_ref,
        active_pane_paths=active_paths,
        protected_paths=args.protected_path,
        protected_prefixes=[*default_protected_prefixes(), *args.protected_prefix],
    )
    payload = plan.to_payload()
    payload["mode"] = "execute" if args.execute else "dry-run"
    payload["active_pane_paths"] = sorted(str(path) for path in active_paths)
    if args.execute:
        payload["execution"] = execute_plan(
            plan,
            repo=repo,
            delete_local_branches=not args.keep_local_branches,
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"mode: {payload['mode']}")
        print(json.dumps(payload["skipped"], indent=2, sort_keys=True))
        print(f"remove candidates: {payload['remove_candidates']}")
        if args.execute:
            execution = payload["execution"]
            print(f"removed: {len(execution['removed'])}")
            print(f"failed: {len(execution['failed'])}")
            print(f"local branches deleted: {len(execution['local_branches_deleted'])}")
    return 0


def _normalize_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_relative_to(path: Path, prefix: Path) -> bool:
    try:
        path.relative_to(prefix)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
