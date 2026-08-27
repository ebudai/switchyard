#!/usr/bin/env python3
"""Source lint for owner-correct team-launcher git subprocesses."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests

TEAM_LAUNCHER = ROOT / "scripts" / "team_launcher.py"
GIT_CHOKEPOINT = "run_owner_correct_git"


@dataclass(frozen=True)
class GitOwnershipLintViolation:
    path: Path
    line: int
    detail: str


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_git_args_builder(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node.func)
    return name.startswith("git_") and name.endswith("_args")


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def git_owner_chokepoint_violations(source: str, path: Path) -> list[GitOwnershipLintViolation]:
    tree = ast.parse(source, filename=str(path))
    parents = _parent_map(tree)
    violations: list[GitOwnershipLintViolation] = []
    for node in ast.walk(tree):
        if not _is_git_args_builder(node):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Call) and _call_name(parent.func) == GIT_CHOKEPOINT:
            continue
        violations.append(
            GitOwnershipLintViolation(
                path=path,
                line=node.lineno,
                detail=f"git argv builder must be executed through {GIT_CHOKEPOINT}()",
            )
        )
    return violations


def lint_team_launcher_git_calls(path: Path = TEAM_LAUNCHER) -> list[GitOwnershipLintViolation]:
    # Deliberately limited to team_launcher.py: it contains every production
    # git_*_args builder and call site today; tests use the builders as expected
    # argv fixtures.
    return git_owner_chokepoint_violations(path.read_text(encoding="utf-8"), path)


def _format_violations(violations: list[GitOwnershipLintViolation]) -> str:
    return "\n".join(f"{violation.path}:{violation.line}: {violation.detail}" for violation in violations)


def test_raw_runner_git_args_call_is_reported() -> None:
    source = "def fetch(runner, config):\n    return runner(git_fetch_worktree_ref_args(config))\n"
    violations = git_owner_chokepoint_violations(source, Path("bad.py"))
    assert len(violations) == 1
    assert GIT_CHOKEPOINT in violations[0].detail


def test_owner_chokepoint_git_args_call_is_allowed() -> None:
    source = "def fetch(runner, config):\n    return run_owner_correct_git(git_fetch_worktree_ref_args(config), runner=runner)\n"
    assert git_owner_chokepoint_violations(source, Path("good.py")) == []


def test_keyword_git_args_call_is_reported() -> None:
    source = "def fetch(runner, config):\n    return runner(args=git_fetch_worktree_ref_args(config))\n"
    violations = git_owner_chokepoint_violations(source, Path("bad.py"))
    assert len(violations) == 1


def test_sudo_list_starred_git_args_call_is_reported() -> None:
    source = "def fetch(runner, user, config):\n    return runner(['sudo', '-u', user, *git_fetch_worktree_ref_args(config)])\n"
    violations = git_owner_chokepoint_violations(source, Path("bad.py"))
    assert len(violations) == 1


def test_sudo_list_concat_git_args_call_is_reported() -> None:
    source = "def fetch(runner, config):\n    return runner(['sudo'] + git_fetch_worktree_ref_args(config))\n"
    violations = git_owner_chokepoint_violations(source, Path("bad.py"))
    assert len(violations) == 1


def test_assigned_git_args_builder_is_reported() -> None:
    source = "def fetch(runner, config):\n    argv = git_fetch_worktree_ref_args(config)\n    return runner(argv)\n"
    violations = git_owner_chokepoint_violations(source, Path("bad.py"))
    assert len(violations) == 1


def test_returned_git_args_builder_is_reported() -> None:
    source = "def build(config):\n    return git_fetch_worktree_ref_args(config)\n"
    violations = git_owner_chokepoint_violations(source, Path("bad.py"))
    assert len(violations) == 1


def test_literal_git_command_is_outside_builder_lint_scope() -> None:
    source = "def status(runner, path):\n    return runner(['git', '-C', path, 'status'])\n"
    assert git_owner_chokepoint_violations(source, Path("literal.py")) == []


def test_team_launcher_git_calls_use_owner_chokepoint() -> None:
    violations = lint_team_launcher_git_calls()
    assert violations == [], _format_violations(violations)


def main() -> int:
    run_module_tests(globals())
    print("team_launcher_git_ownership_lint_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
