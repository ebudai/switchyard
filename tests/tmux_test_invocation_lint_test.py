#!/usr/bin/env python3
"""Source lint for tmux invocations in tests."""

from __future__ import annotations

import ast
import sys
import tempfile
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
    return sorted(path for path in tests_dir.rglob("*.py") if path.is_file())


def _subprocess_call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Attribute):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id != "subprocess":
        return None
    if node.attr not in SUBPROCESS_TMUX_CALLS:
        return None
    return node.attr


@dataclass(frozen=True)
class TmuxArgvShape:
    starts_with_tmux: bool
    has_explicit_socket: bool


def _literal_string_constants(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        values: list[str] = []
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                values.append(element.value)
        return values
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_literal_string_constants(node.left), *_literal_string_constants(node.right)]
    return []


def _tmux_argv_shape(node: ast.AST) -> TmuxArgvShape | None:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _tmux_argv_shape(node.left)
        if left is None:
            return None
        return TmuxArgvShape(
            starts_with_tmux=left.starts_with_tmux,
            has_explicit_socket=left.has_explicit_socket or "-L" in _literal_string_constants(node.right),
        )
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    first = node.elts[0]
    if not isinstance(first, ast.Constant) or first.value != "tmux":
        return None
    return TmuxArgvShape(
        starts_with_tmux=True,
        has_explicit_socket="-L" in _literal_string_constants(node),
    )


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _is_tmux_argv_builder(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    parent = parents.get(node)
    return (
        isinstance(parent, (ast.Return, ast.Assign, ast.AnnAssign))
        and getattr(parent, "value", None) is node
    )


def tmux_subprocess_violations(source: str, path: Path) -> list[TmuxLintViolation]:
    tree = ast.parse(source, filename=str(path))
    violations: list[TmuxLintViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        call_name = _subprocess_call_name(node.func)
        if call_name is None:
            continue
        argv = _tmux_argv_shape(node.args[0])
        if argv is None or not argv.starts_with_tmux:
            continue
        if argv.has_explicit_socket:
            continue
        violations.append(
            TmuxLintViolation(
                path=path,
                line=node.lineno,
                detail=f"subprocess.{call_name} tmux argv lacks explicit -L socket",
            )
        )
    return violations


def tmux_argv_builder_violations(source: str, path: Path) -> list[TmuxLintViolation]:
    tree = ast.parse(source, filename=str(path))
    parents = _parent_map(tree)
    violations: list[TmuxLintViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple, ast.BinOp)):
            continue
        if not _is_tmux_argv_builder(node, parents):
            continue
        argv = _tmux_argv_shape(node)
        if argv is None or not argv.starts_with_tmux or argv.has_explicit_socket:
            continue
        violations.append(
            TmuxLintViolation(
                path=path,
                line=node.lineno,
                detail="tmux argv builder lacks explicit -L socket",
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
        violations.extend(tmux_argv_builder_violations(source, path))
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


def test_subprocess_tmux_with_starred_args_requires_socket() -> None:
    source = "import subprocess\nsubprocess.run(['tmux', *args])\n"
    violations = tmux_subprocess_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "lacks explicit -L" in violations[0].detail


def test_subprocess_tmux_list_concat_requires_socket() -> None:
    source = "import subprocess\nsubprocess.run(['tmux'] + args)\n"
    violations = tmux_subprocess_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "lacks explicit -L" in violations[0].detail


def test_returned_tmux_argv_builder_requires_socket() -> None:
    source = "def tmux_args(args):\n    return ['tmux', *args]\n"
    violations = tmux_argv_builder_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "builder lacks explicit -L" in violations[0].detail


def test_assigned_tmux_argv_builder_requires_socket() -> None:
    source = "def run(args):\n    isolated_args = ['tmux', *args]\n"
    violations = tmux_argv_builder_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "builder lacks explicit -L" in violations[0].detail


def test_tmux_argv_builder_with_socket_is_allowed() -> None:
    source = "def tmux_args(server, args):\n    return ['tmux', '-L', server, *args]\n"
    assert tmux_argv_builder_violations(source, Path("good_test.py")) == []


def test_assertion_tmux_literals_are_not_builder_violations() -> None:
    source = "def test_calls():\n    assert runner.calls[0] == ['tmux', 'has-session', '-t', 'pgu-ops']\n"
    assert tmux_argv_builder_violations(source, Path("good_test.py")) == []


def test_forbidden_server_shutdown_is_reported() -> None:
    source = f"command = ['tmux', '{FORBIDDEN_TMUX_COMMAND}']\n"
    violations = forbidden_server_shutdown_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "server shutdown" in violations[0].detail


def test_lint_scans_nested_tests_and_helpers() -> None:
    with tempfile.TemporaryDirectory(prefix="tmux-lint-scan.") as tmp:
        tmp_path = Path(tmp)
        nested = tmp_path / "nested"
        nested.mkdir()
        helper = tmp_path / "helper.py"
        helper.write_text("def tmux_args(args):\n    return ['tmux', *args]\n", encoding="utf-8")
        nested_test = nested / "x_test.py"
        nested_test.write_text("import subprocess\nsubprocess.run(['tmux'] + args)\n", encoding="utf-8")

        violations = lint_test_sources(tmp_path)
        paths = {violation.path for violation in violations}
        assert helper in paths
        assert nested_test in paths


def test_tests_do_not_use_unisolated_tmux() -> None:
    violations = lint_test_sources()
    assert violations == [], _format_violations(violations)


def main() -> int:
    run_module_tests(globals())
    print("tmux_test_invocation_lint_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
