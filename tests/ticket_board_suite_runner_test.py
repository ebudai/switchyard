#!/usr/bin/env python3
"""Regression tests for the ticket_board_* suite runner."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_runner() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "ticket_board_test_suite_for_test",
        str(ROOT / "scripts" / "ticket-board-test-suite"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_discovers_python_and_shell_ticket_board_suites_only() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-discovery.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_a_test.py").write_text("", encoding="utf-8")
        (tests / "ticket_board_b_test.sh").write_text("", encoding="utf-8")
        (tests / "ticket_board_helper.py").write_text("", encoding="utf-8")
        (tests / "other_board_test.py").write_text("", encoding="utf-8")

        assert runner.discover_ticket_board_suites(root) == [
            "tests/ticket_board_a_test.py",
            "tests/ticket_board_b_test.sh",
        ]


def test_runner_reports_failures_without_stopping_and_strips_live_env() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-run.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_fail_test.py").write_text(
            "import sys\nprint('first failed')\nsys.exit(3)\n",
            encoding="utf-8",
        )
        (tests / "ticket_board_pass_test.py").write_text(
            "import os\n"
            "assert 'TICKET_BOARD_SOCKET' not in os.environ\n"
            "assert 'TICKET_BOARD_DATABASE_URL' not in os.environ\n"
            "print('ticket_board_pass_test: ok')\n",
            encoding="utf-8",
        )
        write_executable(
            tests / "ticket_board_shell_test.sh",
            "#!/usr/bin/env bash\n"
            "if [[ -n \"${TICKET_BOARD_SOCKET:-}\" ]]; then exit 9; fi\n"
            "echo 'ticket_board_shell_test: ok'\n",
        )

        output = io.StringIO()
        results = runner.run_suite(
            root=root,
            out=output,
            env={
                **os.environ,
                "TICKET_BOARD_SOCKET": "/run/pgu-ticket-board/ticket-board.sock",
                "TICKET_BOARD_DATABASE_URL": "postgresql:///pgu",
            },
        )

        statuses = {result.path: result.status for result in results}
        assert statuses == {
            "tests/ticket_board_fail_test.py": "FAIL",
            "tests/ticket_board_pass_test.py": "PASS",
            "tests/ticket_board_shell_test.sh": "PASS",
        }
        rendered = output.getvalue()
        assert "FAIL tests/ticket_board_fail_test.py" in rendered
        assert "PASS tests/ticket_board_pass_test.py" in rendered
        assert "PASS tests/ticket_board_shell_test.sh" in rendered


def test_runner_names_skipped_suites_with_reasons() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-skip.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_browser_test.py").write_text("raise AssertionError('should be skipped')\n", encoding="utf-8")
        (tests / "ticket_board_core_test.py").write_text("print('ticket_board_core_test: ok')\n", encoding="utf-8")

        output = io.StringIO()
        results = runner.run_suite(
            root=root,
            out=output,
            skip_reasons={"tests/ticket_board_browser_test.py": "missing fixture dependency"},
        )

        statuses = {result.path: result.status for result in results}
        assert statuses["tests/ticket_board_browser_test.py"] == "SKIP"
        assert statuses["tests/ticket_board_core_test.py"] == "PASS"
        assert "SKIP tests/ticket_board_browser_test.py" in output.getvalue()
        assert "missing fixture dependency" in output.getvalue()


def main() -> int:
    test_discovers_python_and_shell_ticket_board_suites_only()
    test_runner_reports_failures_without_stopping_and_strips_live_env()
    test_runner_names_skipped_suites_with_reasons()
    print("ticket_board_suite_runner_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
