#!/usr/bin/env python3
"""Source lint for tmux invocations.

Rules:
- tests/ tmux invocations must use an explicit -L socket for isolation.
- tests/ and scripts/ must never invoke the tmux server-shutdown command.
- scripts/ may use a project's default tmux socket; the -L test-isolation rule
  does not apply there.

Known non-goals: absolute tmux paths such as "/usr/bin/tmux", subprocess
function aliases such as ``from subprocess import run``, tmux argv variables
not built in Return/Assign/AnnAssign, dynamically assembled tmux subcommands,
and shell=True command strings.
"""

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
SCRIPTS_DIR = ROOT / "scripts"
SUBPROCESS_TMUX_CALLS = frozenset({"call", "check_call", "check_output", "Popen", "run"})
FORBIDDEN_TMUX_COMMAND = "kill-server"


@dataclass(frozen=True)
class TmuxLintViolation:
    path: Path
    line: int
    detail: str


@dataclass(frozen=True)
class TmuxLintTree:
    root: Path
    require_explicit_socket: bool


def _python_source_paths(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if _is_python_source_path(path))


def _is_python_source_path(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix == ".py":
        return True
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(200)
    except OSError:
        return False
    return first_line.startswith(b"#!") and b"python" in first_line.lower()


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
    literal_args: tuple[str, ...]


def _literal_string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string_value(node.left)
        right = _literal_string_value(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _literal_string_constants(node: ast.AST) -> list[str]:
    literal_value = _literal_string_value(node)
    if literal_value is not None:
        return [literal_value]
    if isinstance(node, (ast.List, ast.Tuple)):
        values: list[str] = []
        for element in node.elts:
            literal_element = _literal_string_value(element)
            if literal_element is not None:
                values.append(literal_element)
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
            literal_args=(*left.literal_args, *_literal_string_constants(node.right)),
        )
    if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
        return None
    first = node.elts[0]
    if _literal_string_value(first) != "tmux":
        return None
    return TmuxArgvShape(
        starts_with_tmux=True,
        has_explicit_socket="-L" in _literal_string_constants(node),
        literal_args=tuple(_literal_string_constants(node)),
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
    tree = ast.parse(source, filename=str(path))
    violations: list[TmuxLintViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple, ast.BinOp)):
            continue
        argv = _tmux_argv_shape(node)
        if argv is None or not argv.starts_with_tmux:
            continue
        if FORBIDDEN_TMUX_COMMAND in argv.literal_args:
            violations.append(
                TmuxLintViolation(
                    path=path,
                    line=node.lineno,
                    detail="tmux server shutdown command is forbidden in tests/ and scripts/",
                )
            )
    return violations


def lint_tmux_sources(
    trees: tuple[TmuxLintTree, ...] = (
        TmuxLintTree(TESTS_DIR, require_explicit_socket=True),
        TmuxLintTree(SCRIPTS_DIR, require_explicit_socket=False),
    ),
) -> list[TmuxLintViolation]:
    violations: list[TmuxLintViolation] = []
    for tree in trees:
        for path in _python_source_paths(tree.root):
            source = path.read_text(encoding="utf-8")
            if tree.require_explicit_socket:
                violations.extend(tmux_subprocess_violations(source, path))
                violations.extend(tmux_argv_builder_violations(source, path))
            violations.extend(forbidden_server_shutdown_violations(source, path))
    return violations


def lint_test_sources(tests_dir: Path = TESTS_DIR) -> list[TmuxLintViolation]:
    return lint_tmux_sources((TmuxLintTree(tests_dir, require_explicit_socket=True),))


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


def test_forbidden_server_shutdown_matches_resolved_string_literals() -> None:
    source = 'command = ["tmux", "kill" "-server"]\n'
    violations = forbidden_server_shutdown_violations(source, Path("bad_test.py"))
    assert len(violations) == 1
    assert "server shutdown" in violations[0].detail


def test_forbidden_server_shutdown_ignores_non_tmux_literals() -> None:
    source = f'FORBIDDEN_TMUX_COMMAND = "{FORBIDDEN_TMUX_COMMAND}"\n'
    assert forbidden_server_shutdown_violations(source, Path("good_test.py")) == []


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


def test_lint_scans_scripts_for_forbidden_server_shutdown() -> None:
    with tempfile.TemporaryDirectory(prefix="tmux-lint-scripts.") as tmp:
        tmp_path = Path(tmp)
        scripts_dir = tmp_path / "scripts"
        tests_dir = tmp_path / "tests"
        scripts_dir.mkdir()
        tests_dir.mkdir()
        script = scripts_dir / "team_launcher.py"
        script.write_text(f"def tmux_args():\n    return ['tmux', '{FORBIDDEN_TMUX_COMMAND}']\n", encoding="utf-8")

        violations = lint_tmux_sources(
            (
                TmuxLintTree(tests_dir, require_explicit_socket=True),
                TmuxLintTree(scripts_dir, require_explicit_socket=False),
            )
        )

        assert len(violations) == 1
        assert violations[0].path == script
        assert "server shutdown" in violations[0].detail


def test_lint_scans_extensionless_python_scripts_for_forbidden_server_shutdown() -> None:
    with tempfile.TemporaryDirectory(prefix="tmux-lint-scripts.") as tmp:
        tmp_path = Path(tmp)
        scripts_dir = tmp_path / "scripts"
        tests_dir = tmp_path / "tests"
        scripts_dir.mkdir()
        tests_dir.mkdir()
        script = scripts_dir / "ticket-board-verify-resume"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "def tmux_args():\n"
            "    return ['tmux', 'kill' '-server']\n",
            encoding="utf-8",
        )

        violations = lint_tmux_sources(
            (
                TmuxLintTree(tests_dir, require_explicit_socket=True),
                TmuxLintTree(scripts_dir, require_explicit_socket=False),
            )
        )

        assert len(violations) == 1
        assert violations[0].path == script
        assert "server shutdown" in violations[0].detail


def test_scripts_tmux_without_socket_is_allowed() -> None:
    with tempfile.TemporaryDirectory(prefix="tmux-lint-scripts.") as tmp:
        scripts_dir = Path(tmp) / "scripts"
        scripts_dir.mkdir()
        script = scripts_dir / "team_launcher.py"
        script.write_text("def tmux_args(role):\n    return ['tmux', 'kill-session', '-t', role]\n", encoding="utf-8")

        assert lint_tmux_sources((TmuxLintTree(scripts_dir, require_explicit_socket=False),)) == []


def test_tests_do_not_use_unisolated_tmux() -> None:
    violations = lint_tmux_sources()
    assert violations == [], _format_violations(violations)


def main() -> int:
    run_module_tests(globals())
    print("tmux_test_invocation_lint_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
