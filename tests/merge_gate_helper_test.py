#!/usr/bin/env python3
"""Regression tests for the merge-gate helper."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "merge-gate-helper"


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, check=check, text=True, capture_output=True)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=repo, check=check)


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def write_fixture(repo: Path, *, value: int = 1, extra: str = "", test_value: int | None = None) -> None:
    test_value = value if test_value is None else test_value
    package = repo / "scripts" / "ticket_board"
    package.mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sample.py").write_text(
        f"def value():\n    return {value}\n{extra}",
        encoding="utf-8",
    )  # pragma: no cover - fixture string construction, not test logic.
    (repo / "tests" / "ticket_board_sample_test.py").write_text(
        "from scripts.ticket_board.sample import value\n\n"
        "def main():\n"  # pragma: no cover - fixture source line.
        f"    assert value() == {test_value}\n"
        "    print('ticket_board_sample_test: ok')\n"  # pragma: no cover - fixture source line.
        "    return 0\n\n"  # pragma: no cover - fixture source line.
        "if __name__ == '__main__':\n"  # pragma: no cover - fixture source line.
        "    raise SystemExit(main())\n",  # pragma: no cover - fixture source line.
        encoding="utf-8",
    )  # pragma: no cover - fixture string construction, not test logic.


def make_repo(tmp: Path) -> tuple[Path, Path]:
    seed = tmp / "seed"
    origin = tmp / "origin.git"
    repo = tmp / "repo"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    git(seed, "config", "user.name", "PGU Test")
    git(seed, "config", "user.email", "pgu-test@example.invalid")
    write_fixture(seed)
    commit_all(seed, "initial")
    git(tmp, "clone", "--bare", str(seed), str(origin))
    git(tmp, "clone", str(origin), str(repo))
    git(repo, "config", "user.name", "PGU Test")
    git(repo, "config", "user.email", "pgu-test@example.invalid")
    return origin, repo


def run_gate(repo: Path, branch: str) -> tuple[int, dict[str, object]]:
    proc = run([str(HELPER), "--repo", str(repo), "--json", branch], check=False)
    assert proc.stdout.strip(), proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def create_branch(repo: Path, name: str) -> None:
    git(repo, "checkout", "-b", name, "origin/main")


def assert_repo_identity_unchanged(repo: Path) -> None:
    assert git(repo, "config", "--get", "user.name").stdout.strip() == "PGU Test"
    assert git(repo, "config", "--get", "user.email").stdout.strip() == "pgu-test@example.invalid"


def test_ready_to_push_when_changed_line_is_executed() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-ready.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/ready")
        write_fixture(repo, value=2)
        commit_all(repo, "covered change")
        code, payload = run_gate(repo, "feature/ready")
        assert code == 0, payload
        assert payload["verdict"] == "READY-TO-PUSH", payload
        assert_repo_identity_unchanged(repo)


def test_gate_does_not_mutate_caller_repo_identity() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-identity.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/identity")
        write_fixture(repo, value=2)
        commit_all(repo, "covered change")
        code, payload = run_gate(repo, "feature/identity")
        assert code == 0, payload
        assert payload["verdict"] == "READY-TO-PUSH", payload
        assert_repo_identity_unchanged(repo)


def test_clean_merge_test_failure_is_not_ready() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-test-fail.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/fail")
        write_fixture(repo, value=2, test_value=1)
        commit_all(repo, "break test")
        code, payload = run_gate(repo, "feature/fail")
        assert code == 3, payload
        assert payload["verdict"] == "TEST-FAIL", payload
        assert payload["failed_tests"], payload


def test_uncovered_changed_line_is_sent_back() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-uncovered.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/uncovered")
        write_fixture(repo, value=1, extra="\ndef unused():\n    return 3\n")
        commit_all(repo, "add uncovered function")
        code, payload = run_gate(repo, "feature/uncovered")
        assert code == 4, payload
        assert payload["verdict"] == "SEND-BACK-UNCOVERED", payload
        assert any(item["text"] == "return 3" for item in payload["uncovered"]), payload


def test_explicit_no_cover_pragma_exempts_changed_line() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-pragma.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/pragma")
        write_fixture(repo, value=1, extra="\ndef unused():\n    return 3  # pragma: no cover\n")
        commit_all(repo, "add exempt function")
        code, payload = run_gate(repo, "feature/pragma")
        assert code == 0, payload
        assert payload["verdict"] == "READY-TO-PUSH", payload


def test_conflicting_branch_reports_conflict() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-conflict.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/conflict")
        write_fixture(repo, value=2)
        commit_all(repo, "branch change")
        git(repo, "checkout", "main")
        write_fixture(repo, value=3)
        commit_all(repo, "main change")
        git(repo, "push", "origin", "main")
        code, payload = run_gate(repo, "feature/conflict")
        assert code == 2, payload
        assert payload["verdict"] == "CONFLICT", payload
        assert "scripts/ticket_board/sample.py" in payload["conflict_files"], payload


def test_non_python_change_reports_coverage_not_available() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-non-python.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/docs")
        (repo / "README.md").write_text("docs\n", encoding="utf-8")
        commit_all(repo, "docs change")
        code, payload = run_gate(repo, "feature/docs")
        assert code == 5, payload
        assert payload["verdict"] == "COVERAGE-NOT-AVAILABLE", payload
        assert payload["coverage_not_available"] == ["README.md"], payload


def test_python_change_with_only_shell_test_reports_coverage_not_available() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-shell-only.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        create_branch(repo, "feature/shell-only")
        test_path = repo / "tests" / "ticket_board_sample_test.py"
        test_path.unlink()
        (repo / "tests" / "ticket_board_sample_test.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        write_fixture(repo, value=2)
        test_path.unlink()
        commit_all(repo, "covered only by shell")
        code, payload = run_gate(repo, "feature/shell-only")
        assert code == 5, payload
        assert payload["verdict"] == "COVERAGE-NOT-AVAILABLE", payload
        assert payload["coverage_not_available"] == ["tests/ticket_board_sample_test.sh", "scripts/ticket_board/sample.py"], payload


def test_infers_team_launcher_test() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-launcher.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        (repo / "scripts" / "team_launcher.py").write_text("def value():\n    return 1\n", encoding="utf-8")
        (repo / "tests" / "team_launcher_test.py").write_text(
            "from scripts.team_launcher import value\n"
            "assert value() == 2\n",  # pragma: no cover - fixture source line.
            encoding="utf-8",
        )  # pragma: no cover - fixture string construction, not test logic.
        commit_all(repo, "add launcher")
        git(repo, "push", "origin", "main")
        create_branch(repo, "feature/launcher")
        (repo / "scripts" / "team_launcher.py").write_text("def value():\n    return 2\n", encoding="utf-8")
        commit_all(repo, "change launcher")
        code, payload = run_gate(repo, "feature/launcher")
        assert code == 0, payload
        assert payload["tests"] == ["tests/team_launcher_test.py"], payload


def test_infers_directorctl_tests() -> None:
    with tempfile.TemporaryDirectory(prefix="merge-gate-directorctl.") as tmpdir:
        _origin, repo = make_repo(Path(tmpdir))
        (repo / "scripts" / "directorctl").write_text("#!/usr/bin/env python3\ndef value():\n    return 1\n", encoding="utf-8")
        (repo / "tests" / "directorctl_smoke_test.py").write_text(
            "import runpy\n"
            "ns = runpy.run_path('scripts/directorctl')\n"  # pragma: no cover - fixture source line.
            "assert ns['value']() == 2\n",  # pragma: no cover - fixture source line.
            encoding="utf-8",
        )  # pragma: no cover - fixture string construction, not test logic.
        commit_all(repo, "add directorctl")
        git(repo, "push", "origin", "main")
        create_branch(repo, "feature/directorctl")
        (repo / "scripts" / "directorctl").write_text("#!/usr/bin/env python3\ndef value():\n    return 2\n", encoding="utf-8")
        commit_all(repo, "change directorctl")
        code, payload = run_gate(repo, "feature/directorctl")
        assert code == 0, payload
        assert payload["tests"] == ["tests/directorctl_smoke_test.py"], payload


def main() -> int:
    test_ready_to_push_when_changed_line_is_executed()
    test_gate_does_not_mutate_caller_repo_identity()
    test_clean_merge_test_failure_is_not_ready()
    test_uncovered_changed_line_is_sent_back()
    test_explicit_no_cover_pragma_exempts_changed_line()
    test_conflicting_branch_reports_conflict()
    test_non_python_change_reports_coverage_not_available()
    test_python_change_with_only_shell_test_reports_coverage_not_available()
    test_infers_team_launcher_test()
    test_infers_directorctl_tests()
    print("merge_gate_helper_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
