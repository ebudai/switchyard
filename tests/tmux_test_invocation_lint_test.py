#!/usr/bin/env python3
"""Source lint for tmux invocations in tests."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from standalone_test_runner import run_module_tests

TESTS_DIR = ROOT / "tests"
SUBPROCESS_TMUX_CALLS = frozenset({"call", "check_call", "check_output", "Popen", "run"})
FORBIDDEN_TMUX_COMMAND = "kill" "-server"


@dataclass(frozen=True)
class TmuxLintViolation:
    path: Path
    line: int
    detail: str


def _test_source_paths(tests_dir: Path = TESTS_DIR) -> list[Path]:
    return sorted(path for path in tests_dir.glob("*_test.py") if path.is_file())


def _subprocess_call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Attribute):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id != "subprocess":
        return None
    if node.attr not in SUBPROCESS_TMUX_CALLS:
        return None
    return node.attr


def _literal_argv(node: ast.AST) -> list[str] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values: list[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.append(element.value)
    return values


def tmux_subprocess_violations(source: str, path: Path) -> list[TmuxLintViolation]:
    tree = ast.parse(source, filename=str(path))
    violations: list[TmuxLintViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        call_name = _subprocess_call_name(node.func)
        if call_name is None:
            continue
        argv = _literal_argv(node.args[0])
        if not argv or argv[0] != "tmux":
            continue
        if "-L" in argv:
            continue
        violations.append(
            TmuxLintViolation(
                path=path,
                line=node.lineno,
                detail=f"subprocess.{call_name} tmux argv lacks explicit -L socket",
            )
        )
    return violations


def forbidden_server_shutdown_violations(source: str, path: Path) -> list[TmuxLintViolation]:
    violations: list[TmuxLintViolation] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if FORBIDDEN_TMUX_COMMAND in line:
            violations.append(
                TmuxLintViolation(
                    path=path,
                    line=line_number,
                    detail="tests must not invoke the tmux server shutdown command",
                )
            )
    return violations


def lint_test_sources(tests_dir: Path = TESTS_DIR) -> list[TmuxLintViolation]:
    violations: list[TmuxLintViolation] = []
    for path in _test_source_paths(tests_dir):
        source = path.read_text(encoding="utf-8")
        violations.extend(tmux_subprocess_violations(source, path))
        violations.extend(forbidden_server_shutdown_violations(source, path))
    return violations


def _format_violations(violations: list[TmuxLintViolation]) -> str:
    return "\n".join(f"{violation.path}:{violation.line}: {violation.detail}" for violation in violations)


def test_subprocess_tmux_without_socket_is_reported() -> None:
    source = "import subprocess\nsubprocess.run(['tmux', 'new-session', '-d'])\n"
    violations = tmux_subprocess_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "lacks explicit -L" in violations[0].detail


def test_subprocess_tmux_with_socket_is_allowed() -> None:
    source = "import subprocess\nsubprocess.run(['tmux', '-L', 'scratch', 'new-session', '-d'])\n"
    assert tmux_subprocess_violations(source, Path("good_test.py")) == []


def test_forbidden_server_shutdown_is_reported() -> None:
    source = f"command = ['tmux', '{FORBIDDEN_TMUX_COMMAND}']\n"
    violations = forbidden_server_shutdown_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "server shutdown" in violations[0].detail


def test_tests_do_not_use_unisolated_tmux() -> None:
    violations = lint_test_sources()
    assert violations == [], _format_violations(violations)


def main() -> int:
    run_module_tests(globals())
    print("tmux_test_invocation_lint_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
