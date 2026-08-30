#!/usr/bin/env python3
"""Regression tests for safe stale-worktree cleanup planning."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.standalone_test_runner import run_module_tests

import scripts.ticket_board.worktree_cleanup as worktree_cleanup
from scripts.ticket_board.worktree_cleanup import (
    WorktreeEntry,
    default_protected_prefixes,
    execute_plan,
    parse_worktree_porcelain,
    plan_cleanup,
)


def test_parse_worktree_porcelain_keeps_heads_and_branches() -> None:
    entries = parse_worktree_porcelain(
        "worktree /repo\n"
        "HEAD aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/wt\n"
        "HEAD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "branch refs/heads/feature/example\n"
    )

    assert [entry.path for entry in entries] == [Path("/repo"), Path("/repo/wt")]
    assert entries[1].head == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert entries[1].branch == "feature/example"


def test_default_protected_prefixes_include_launcher_team_worktrees() -> None:
    assert Path.home() / ".claude" / "worktrees" / "pgu-team" in default_protected_prefixes()


def test_plan_cleanup_protects_live_dirty_and_unmerged_worktrees() -> None:
    with tempfile.TemporaryDirectory(prefix="worktree-cleanup-plan.") as tmpdir:
        tmp_path = Path(tmpdir)
        shared = tmp_path / "shared"
        exact = tmp_path / "exact-protected"
        team = tmp_path / ".claude" / "worktrees" / "pgu-team" / "ops"
        running = tmp_path / "running"
        dirty = tmp_path / "dirty"
        unmerged = tmp_path / "unmerged"
        clean = tmp_path / "clean"
        for path in (shared, exact, team, running, dirty, unmerged, clean):
            path.mkdir(parents=True)

        entries = [
            WorktreeEntry(shared, head="0" * 40, branch_ref="refs/heads/main"),
            WorktreeEntry(exact, head="7" * 40, branch_ref="refs/heads/exact"),
            WorktreeEntry(team, head="1" * 40, branch_ref="refs/heads/team"),
            WorktreeEntry(running, head="2" * 40, branch_ref="refs/heads/running"),
            WorktreeEntry(dirty, head="3" * 40, branch_ref="refs/heads/dirty"),
            WorktreeEntry(unmerged, head="4" * 40, branch_ref="refs/heads/unmerged"),
            WorktreeEntry(clean, head="5" * 40, branch_ref="refs/heads/clean"),
        ]

        def status(path: Path) -> str:
            return " M file.txt" if path == dirty.resolve() else ""

        def merged(head: str) -> bool:
            return head != "4" * 40

        plan = plan_cleanup(
            entries,
            repo=shared,
            active_pane_paths=[running / "subdir"],
            shared_checkout=shared,
            protected_paths=[exact],
            protected_prefixes=[tmp_path / ".claude" / "worktrees" / "pgu-team"],
            status_func=status,
            merged_func=merged,
        )

        decisions = {decision.entry.branch: decision for decision in plan.decisions}
        assert decisions["main"].reason == "protected"
        assert decisions["exact"].reason == "protected"
        assert decisions["team"].reason == "protected-prefix"
        assert decisions["running"].reason == "running-pane"
        assert decisions["dirty"].reason == "dirty"
        assert decisions["dirty"].dirty_status == " M file.txt"
        assert decisions["unmerged"].reason == "unmerged"
        assert decisions["clean"].action == "remove"
        assert decisions["clean"].reason == "merged-clean"


def test_plan_cleanup_skips_when_merge_status_is_unknown() -> None:
    with tempfile.TemporaryDirectory(prefix="worktree-cleanup-unknown.") as tmpdir:
        tmp_path = Path(tmpdir)
        repo = tmp_path / "repo"
        candidate = tmp_path / "candidate"
        repo.mkdir()
        candidate.mkdir()

        plan = plan_cleanup(
            [WorktreeEntry(candidate, head="6" * 40, branch_ref="refs/heads/unknown")],
            repo=repo,
            active_pane_paths=[],
            shared_checkout=repo,
            status_func=lambda _path: "",
            merged_func=lambda _head: None,
        )

        assert len(plan.decisions) == 1
        assert plan.decisions[0].action == "skip"
        assert plan.decisions[0].reason == "merge-status-unknown"


def test_execute_plan_removes_exact_candidate_paths_and_branches() -> None:
    with tempfile.TemporaryDirectory(prefix="worktree-cleanup-execute.") as tmpdir:
        tmp_path = Path(tmpdir)
        repo = tmp_path / "repo"
        repo.mkdir()
        keep = tmp_path / "keep"
        remove = tmp_path / "remove"
        keep.mkdir()
        remove.mkdir()
        plan = plan_cleanup(
            [
                WorktreeEntry(keep, head="1" * 40, branch_ref="refs/heads/keep"),
                WorktreeEntry(remove, head="2" * 40, branch_ref="refs/heads/remove-me"),
            ],
            repo=repo,
            active_pane_paths=[],
            shared_checkout=repo,
            status_func=lambda _path: "",
            merged_func=lambda head: head == "2" * 40,
        )
        calls: list[list[str]] = []

        def runner(args: list[str] | tuple[str, ...], cwd: Path | None) -> subprocess.CompletedProcess[str]:
            del cwd
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        result = execute_plan(plan, repo=repo, runner=runner)

        assert result["removed"] == [str(remove.resolve())]
        assert result["local_branches_deleted"] == ["remove-me"]
        assert ["git", "-C", str(repo), "worktree", "remove", str(remove.resolve())] in calls
        assert ["git", "-C", str(repo), "branch", "-d", "remove-me"] in calls
        assert not any(str(keep.resolve()) in call for command in calls for call in command)


def test_main_dry_run_does_not_prune_missing_worktrees() -> None:
    calls: list[str] = []
    old_prune = worktree_cleanup.prune_missing_worktrees
    old_read = worktree_cleanup.read_registered_worktrees
    old_tmux = worktree_cleanup.tmux_active_pane_paths
    try:
        worktree_cleanup.prune_missing_worktrees = lambda _repo: calls.append("prune")  # type: ignore[assignment]
        worktree_cleanup.read_registered_worktrees = lambda _repo: []  # type: ignore[assignment]
        worktree_cleanup.tmux_active_pane_paths = lambda: set()  # type: ignore[assignment]
        out = StringIO()
        with redirect_stdout(out):
            assert worktree_cleanup.main(["--repo", "/tmp/nonexistent", "--json"]) == 0
        assert calls == []
    finally:
        worktree_cleanup.prune_missing_worktrees = old_prune  # type: ignore[assignment]
        worktree_cleanup.read_registered_worktrees = old_read  # type: ignore[assignment]
        worktree_cleanup.tmux_active_pane_paths = old_tmux  # type: ignore[assignment]


def main() -> int:
    run_module_tests(globals())
    print("ticket_board_worktree_cleanup_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
