#!/usr/bin/env python3
"""Guard schema.sql function edits against missing same-commit migrations."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = "scripts/ticket_board/schema.sql"
MIGRATIONS_PREFIX = "scripts/ticket_board/migrations/"
FUNCTION_START_RE = re.compile(
    r"(?is)\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
    r"((?:[A-Za-z_][A-Za-z0-9_$]*\.)?[A-Za-z_][A-Za-z0-9_$]*)\s*\("
)
FUNCTION_BODY_RE = re.compile(r"(?is)\bAS\s+(\$[A-Za-z_0-9]*\$)")
MIGRATION_FUNCTION_RE = re.compile(
    r"(?is)\bCREATE\s+OR\s+REPLACE\s+FUNCTION\s+"
    r"((?:[A-Za-z_][A-Za-z0-9_$]*\.)?[A-Za-z_][A-Za-z0-9_$]*)\s*\("
)


@dataclass(frozen=True)
class GuardResult:
    commit: str
    changed_functions: set[str]
    migrated_functions: set[str]
    activated_stale_functions: set[str]
    missing_functions: set[str]


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def git_text(*args: str) -> str:
    return run_git(*args).stdout


def git_show(commit: str, path: str) -> str:
    proc = run_git("show", f"{commit}:{path}", check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def first_parent(commit: str) -> str | None:
    proc = run_git("rev-parse", f"{commit}^", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def normalize_sql_body(body: str) -> str:
    return " ".join(body.split())


def extract_schema_function_bodies(sql: str) -> dict[str, str]:
    starts = list(FUNCTION_START_RE.finditer(sql))
    functions: dict[str, str] = {}
    for index, match in enumerate(starts):
        block_end = starts[index + 1].start() if index + 1 < len(starts) else len(sql)
        block = sql[match.start() : block_end]
        body_match = FUNCTION_BODY_RE.search(block)
        if body_match is None:
            raise AssertionError(f"function has no dollar-quoted body: {match.group(1)}")
        delimiter = body_match.group(1)
        body_start = body_match.end()
        body_end = block.find(delimiter, body_start)
        if body_end == -1:
            raise AssertionError(f"unterminated function body: {match.group(1)}")
        functions[match.group(1).lower()] = normalize_sql_body(block[body_start:body_end])
    return functions


def changed_schema_functions(commit: str) -> set[str]:
    parent = first_parent(commit)
    if parent is None:
        return set()
    before = extract_schema_function_bodies(git_show(parent, SCHEMA_PATH))
    after = extract_schema_function_bodies(git_show(commit, SCHEMA_PATH))
    changed: set[str] = set()
    for name in before.keys() | after.keys():
        if before.get(name) != after.get(name):
            changed.add(name)
    return changed


def added_migration_paths(commit: str) -> list[str]:
    parent = first_parent(commit)
    if parent is None:
        return []
    output = git_text("diff", "--name-status", parent, commit, "--", f"{MIGRATIONS_PREFIX}*.sql")
    paths: list[str] = []
    for line in output.splitlines():
        status, _, path = line.partition("\t")
        if status == "A" and path.startswith(MIGRATIONS_PREFIX) and path.endswith(".sql"):
            paths.append(path)
    return paths


def functions_replaced_by_added_migrations(commit: str) -> set[str]:
    names: set[str] = set()
    for path in added_migration_paths(commit):
        migration_sql = git_show(commit, path)
        names.update(match.group(1).lower() for match in MIGRATION_FUNCTION_RE.finditer(migration_sql))
    return names


def latest_migration_function_bodies(commit: str) -> dict[str, str]:
    output = git_text("ls-tree", "-r", "--name-only", commit, "--", MIGRATIONS_PREFIX)
    latest: dict[str, str] = {}
    for path in sorted(line.strip() for line in output.splitlines() if line.strip().endswith(".sql")):
        latest.update(extract_schema_function_bodies(git_show(commit, path)))
    return latest


def added_migrations_change_assignee_domain(commit: str) -> bool:
    migration_sql = "\n".join(git_show(commit, path).lower() for path in added_migration_paths(commit))
    return (
        "tickets_assignee_check" in migration_sql
        or "ticket_valid_assignee" in migration_sql
        or "owner_roles" in migration_sql
    )


def activated_stale_schema_functions(commit: str, migrated_functions: set[str]) -> set[str]:
    if not added_migrations_change_assignee_domain(commit):
        return set()

    schema_functions = extract_schema_function_bodies(git_show(commit, SCHEMA_PATH))
    latest_migration_functions = latest_migration_function_bodies(commit)
    stale: set[str] = set()
    for name, schema_body in schema_functions.items():
        migrated_body = latest_migration_functions.get(name)
        if migrated_body is None or schema_body == migrated_body:
            continue
        if name in migrated_functions:
            continue
        if "ticket_board.ticket_valid_assignee(assignee)" in schema_body:
            stale.add(name)
    return stale


def check_commit(commit: str) -> GuardResult:
    full_commit = git_text("rev-parse", commit).strip()
    changed = changed_schema_functions(full_commit)
    migrated = functions_replaced_by_added_migrations(full_commit)
    activated_stale = activated_stale_schema_functions(full_commit, migrated)
    return GuardResult(
        commit=full_commit,
        changed_functions=changed,
        migrated_functions=migrated,
        activated_stale_functions=activated_stale,
        missing_functions=(changed - migrated) | activated_stale,
    )


def default_commits_to_check() -> list[str]:
    proc = run_git("rev-list", "--reverse", "origin/main..HEAD", check=False)
    if proc.returncode == 0:
        commits = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if commits:
            return commits
    return ["HEAD"]


def assert_commit_passes(commit: str) -> None:
    result = check_commit(commit)
    assert not result.missing_functions, (
        f"{commit} changed schema.sql functions without same-commit added migrations: "
        + ", ".join(sorted(result.missing_functions))
    )


def test_pgu649_known_defect_is_caught() -> None:
    result = check_commit("97b852f")
    # PGU-649 did not edit route/force_move directly; it made the already-stale
    # live definitions observable by adding user to the assignee domain.
    assert "ticket_board.route" in result.activated_stale_functions, result
    assert "ticket_board.force_move" in result.activated_stale_functions, result
    assert "ticket_board.route" in result.missing_functions, result
    assert "ticket_board.force_move" in result.missing_functions, result


def test_pgu599_direct_schema_function_change_without_migration_is_caught() -> None:
    result = check_commit("608ae70")
    assert "ticket_board.route" in result.changed_functions, result
    assert "ticket_board.force_move" in result.changed_functions, result
    assert "ticket_board.route" in result.missing_functions, result
    assert "ticket_board.force_move" in result.missing_functions, result


def test_pgu652_same_commit_migration_satisfies_guard() -> None:
    result = check_commit("856fd15")
    assert "ticket_board.route" in result.migrated_functions, result
    assert "ticket_board.force_move" in result.migrated_functions, result
    assert not result.missing_functions, result


def test_current_branch_schema_function_changes_have_added_migrations() -> None:
    for commit in default_commits_to_check():
        assert_commit_passes(commit)


def main() -> int:
    test_pgu649_known_defect_is_caught()
    test_pgu599_direct_schema_function_change_without_migration_is_caught()
    test_pgu652_same_commit_migration_satisfies_guard()
    test_current_branch_schema_function_changes_have_added_migrations()
    print("ticket_board_schema_function_migration_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
