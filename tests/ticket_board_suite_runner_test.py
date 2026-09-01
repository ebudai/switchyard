#!/usr/bin/env python3
"""Regression tests for the ticket-board suite runner."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import stat
import subprocess
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


def test_discovers_board_adjacent_suites_by_content_property() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-adjacent-discovery.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_a_test.py").write_text("board_url = 'covered by namespace'\n", encoding="utf-8")
        (tests / "directorctl_ticket_create_test.py").write_text("'--board-url'\n", encoding="utf-8")
        (tests / "report_launch_crash_test.py").write_text("TICKET_BOARD_SOCKET = '/tmp/socket'\n", encoding="utf-8")
        (tests / "team_launcher_test.py").write_text("board_url = 'http://127.0.0.1:8770'\n", encoding="utf-8")
        (tests / "otto_milestone2_runbook_test.py").write_text(
            "from scripts.ticket_board.write_client import TicketBoardWriteClient\n",
            encoding="utf-8",
        )
        (tests / "attachment_crop_browser_test.py").write_text(
            "from tests.ticket_board_ui_harness import BoardHarness\n",
            encoding="utf-8",
        )
        (tests / "unrelated_test.py").write_text("print('not a board client')\n", encoding="utf-8")

        assert runner.discover_board_adjacent_suites(root) == [
            "tests/attachment_crop_browser_test.py",
            "tests/directorctl_ticket_create_test.py",
            "tests/otto_milestone2_runbook_test.py",
            "tests/report_launch_crash_test.py",
            "tests/team_launcher_test.py",
        ]


def test_discovers_host_automation_suites_by_content_property() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-host-discovery.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "repository_policy_test.py").write_text(
            "from scripts.repository_hooks import install_main_guard\n",
            encoding="utf-8",
        )
        (tests / "rollout_test.py").write_text("'install-all-repository-hooks'\n", encoding="utf-8")
        (tests / "launcher_test.sh").write_text("install-switchyard --apply\n", encoding="utf-8")
        (tests / "board_overlap_test.py").write_text(
            "from scripts.repository_hooks import install_main_guard\nboard_url = 'http://example'\n",
            encoding="utf-8",
        )
        (tests / "unrelated_test.py").write_text("print('unrelated')\n", encoding="utf-8")

        assert runner.discover_host_automation_suites(root) == [
            "tests/launcher_test.sh",
            "tests/repository_policy_test.py",
            "tests/rollout_test.py",
        ]


def test_discovers_inline_ticket_board_browser_suites_by_content_property() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-browser-discovery.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_a_test.py").write_text(
            "from scripts.ticket_board.server import TicketBoardServer\n"
            "from playwright.sync_api import sync_playwright\n",
            encoding="utf-8",
        )
        (tests / "inline_browser_test.py").write_text(
            "from scripts.ticket_board.server import TicketBoardServer\n"
            "from playwright.sync_api import sync_playwright\n",
            encoding="utf-8",
        )
        (tests / "inline_browser_alt_test.py").write_text(
            "import scripts.ticket_board.frontend\n"
            "browser = playwright.chromium.launch(headless=True)\n",
            encoding="utf-8",
        )
        (tests / "inline_frontend_only_test.py").write_text(
            "from scripts.ticket_board.frontend import HTML\n",
            encoding="utf-8",
        )
        (tests / "harness_browser_test.py").write_text(
            "from scripts.ticket_board.frontend import HTML\n"
            "from tests.ticket_board_ui_harness import BoardHarness, load_playwright\n",
            encoding="utf-8",
        )
        (tests / "unrelated_browser_test.py").write_text(
            "from playwright.sync_api import sync_playwright\n",
            encoding="utf-8",
        )

        assert runner.discover_ticket_board_browser_suites(root) == [
            "tests/inline_browser_alt_test.py",
            "tests/inline_browser_test.py",
        ]


def test_discovers_inline_ticket_board_frontend_suites_by_content_property() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-frontend-discovery.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_a_test.py").write_text(
            "from scripts.ticket_board.frontend import HTML\n",
            encoding="utf-8",
        )
        (tests / "inline_frontend_test.py").write_text(
            "from scripts.ticket_board.frontend import HTML\n",
            encoding="utf-8",
        )
        (tests / "inline_browser_test.py").write_text(
            "from scripts.ticket_board.server import TicketBoardServer\n"
            "from playwright.sync_api import sync_playwright\n",
            encoding="utf-8",
        )
        (tests / "harness_frontend_test.py").write_text(
            "from tests.ticket_board_ui_harness import BoardHarness\n",
            encoding="utf-8",
        )

        assert runner.discover_ticket_board_frontend_suites(root) == [
            "tests/inline_frontend_test.py",
        ]


def test_adjacent_group_runs_only_board_adjacent_suites() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-adjacent-run.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_fail_if_run_test.py").write_text(
            "raise AssertionError('ticket_board group should not run')\n",
            encoding="utf-8",
        )
        (tests / "adjacent_pass_test.py").write_text(
            "board_url = 'http://127.0.0.1:1'\nprint('adjacent_pass_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "browser_harness_pass_test.py").write_text(
            "marker = 'ticket_board_ui_harness'\nprint('browser_harness_pass_test: ok')\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        results = runner.run_suite(root=root, groups=(runner.BOARD_ADJACENT_GROUP,), out=output)

        assert [(result.group, result.path, result.status) for result in results] == [
            (runner.BOARD_ADJACENT_GROUP, "tests/adjacent_pass_test.py", "PASS"),
            (runner.BOARD_ADJACENT_GROUP, "tests/browser_harness_pass_test.py", "PASS"),
        ]
        rendered = output.getvalue()
        assert "group: board_adjacent" in rendered
        assert "PASS [board_adjacent] tests/adjacent_pass_test.py" in rendered
        assert "PASS [board_adjacent] tests/browser_harness_pass_test.py" in rendered
        assert "ticket_board_fail_if_run_test" not in rendered


def test_browser_group_runs_only_inline_ticket_board_browser_suites() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-browser-run.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "inline_browser_pass_test.py").write_text(
            "marker = 'from scripts.ticket_board.server import TicketBoardServer'\n"
            "browser = 'playwright.chromium.launch(headless=True)'\n"
            "print('inline_browser_pass_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "inline_frontend_fail_if_run_test.py").write_text(
            "marker = 'from scripts.ticket_board.frontend import HTML'\n"
            "raise AssertionError('non-browser importer should not run in browser group')\n",
            encoding="utf-8",
        )
        (tests / "harness_fail_if_run_test.py").write_text(
            "marker = 'ticket_board_ui_harness'\n"
            "browser = 'playwright.chromium.launch(headless=True)'\n"
            "raise AssertionError('adjacent harness browser should not be duplicated')\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        results = runner.run_suite(
            root=root,
            groups=(runner.TICKET_BOARD_BROWSER_GROUP,),
            out=output,
            skip_reasons={},
        )

        assert [(result.group, result.path, result.status) for result in results] == [
            (runner.TICKET_BOARD_BROWSER_GROUP, "tests/inline_browser_pass_test.py", "PASS"),
        ]
        rendered = output.getvalue()
        assert "group: ticket_board_browser" in rendered
        assert "PASS [ticket_board_browser] tests/inline_browser_pass_test.py" in rendered
        assert "inline_frontend_fail_if_run_test" not in rendered
        assert "harness_fail_if_run_test" not in rendered


def test_frontend_group_runs_only_inline_ticket_board_non_browser_suites() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-frontend-run.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "inline_frontend_pass_test.py").write_text(
            "marker = 'from scripts.ticket_board.frontend import HTML'\n"
            "print('inline_frontend_pass_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "inline_browser_fail_if_run_test.py").write_text(
            "marker = 'from scripts.ticket_board.server import TicketBoardServer'\n"
            "browser = 'load_playwright()'\n"
            "raise AssertionError('browser importer should not run in frontend group')\n",
            encoding="utf-8",
        )
        (tests / "harness_fail_if_run_test.py").write_text(
            "marker = 'ticket_board_ui_harness'\n"
            "raise AssertionError('adjacent harness test should not be duplicated')\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        results = runner.run_suite(root=root, groups=(runner.TICKET_BOARD_FRONTEND_GROUP,), out=output)

        assert [(result.group, result.path, result.status) for result in results] == [
            (runner.TICKET_BOARD_FRONTEND_GROUP, "tests/inline_frontend_pass_test.py", "PASS"),
        ]
        rendered = output.getvalue()
        assert "group: ticket_board_frontend" in rendered
        assert "PASS [ticket_board_frontend] tests/inline_frontend_pass_test.py" in rendered
        assert "inline_browser_fail_if_run_test" not in rendered
        assert "harness_fail_if_run_test" not in rendered


def test_all_group_includes_inline_ticket_board_importer_suites() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-all-browser.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "ticket_board_core_test.py").write_text("print('ticket_board_core_test: ok')\n", encoding="utf-8")
        (tests / "adjacent_test.py").write_text(
            "board_url = 'http://127.0.0.1:1'\nprint('adjacent_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "inline_browser_test.py").write_text(
            "marker = 'from scripts.ticket_board.server import TicketBoardServer'\n"
            "browser = 'load_playwright()'\n"
            "print('inline_browser_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "inline_frontend_test.py").write_text(
            "marker = 'from scripts.ticket_board.frontend import HTML'\n"
            "print('inline_frontend_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "host_test.py").write_text(
            "marker = 'scripts.repository_hooks'\nprint('host_test: ok')\n",
            encoding="utf-8",
        )

        suites = runner.discover_suites_by_group(root, runner.normalize_groups("all"))

        assert suites == {
            runner.TICKET_BOARD_GROUP: ["tests/ticket_board_core_test.py"],
            runner.BOARD_ADJACENT_GROUP: ["tests/adjacent_test.py"],
            runner.HOST_AUTOMATION_GROUP: ["tests/host_test.py"],
            runner.TICKET_BOARD_FRONTEND_GROUP: ["tests/inline_frontend_test.py"],
            runner.TICKET_BOARD_BROWSER_GROUP: ["tests/inline_browser_test.py"],
        }


def test_runner_reports_reduced_coverage_without_skipping_suite() -> None:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-reduced.") as tmpdir:
        root = Path(tmpdir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "host_reduced_test.py").write_text(
            "marker = 'scripts.repository_hooks'\n"
            "print('COVERAGE REDUCED: root-only branch not run')\n"
            "print('host_reduced_test: ok')\n",
            encoding="utf-8",
        )

        output = io.StringIO()
        results = runner.run_suite(root=root, groups=(runner.HOST_AUTOMATION_GROUP,), out=output)

        assert len(results) == 1
        assert results[0].status == "PASS"
        assert results[0].reason == "root-only branch not run"
        assert "COVERAGE REDUCED: root-only branch not run" in output.getvalue()


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
        assert "FAIL [ticket_board] tests/ticket_board_fail_test.py" in rendered
        assert "PASS [ticket_board] tests/ticket_board_pass_test.py" in rendered
        assert "PASS [ticket_board] tests/ticket_board_shell_test.sh" in rendered


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
        assert "SKIP [ticket_board] tests/ticket_board_browser_test.py" in output.getvalue()
        assert "missing fixture dependency" in output.getvalue()


def test_runner_skips_every_browser_suite_when_playwright_is_unavailable() -> None:
    with tempfile.TemporaryDirectory(prefix="ticket-board-suite-playwright-missing.") as tmpdir:
        root = Path(tmpdir) / "repo"
        tests = root / "tests"
        tests.mkdir(parents=True)
        playwright_import_marker = "from " + "playwright.sync_api import sync_playwright"
        load_playwright_marker = "load_" + "playwright()"
        (tests / "ticket_board_browser_test.py").write_text(
            f"marker = {playwright_import_marker!r}\n"
            "raise AssertionError('ticket_board browser suite should be skipped')\n",
            encoding="utf-8",
        )
        (tests / "ticket_board_core_test.py").write_text(
            "print('ticket_board_core_test: ok')\n",
            encoding="utf-8",
        )
        (tests / "adjacent_browser_test.py").write_text(
            f"marker = 'ticket_board_ui_harness ' + {load_playwright_marker!r}\n"
            "raise AssertionError('adjacent browser suite should be skipped')\n",
            encoding="utf-8",
        )
        (tests / "inline_browser_test.py").write_text(
            f"marker = 'from scripts.ticket_board.server import TicketBoardServer ' + {load_playwright_marker!r}\n"
            "raise AssertionError('inline browser suite should be skipped')\n",
            encoding="utf-8",
        )
        (tests / "inline_frontend_test.py").write_text(
            "marker = 'from scripts.ticket_board.frontend import HTML'\n"
            "print('inline_frontend_test: ok')\n",
            encoding="utf-8",
        )
        blocker = Path(tmpdir) / "hide_playwright"
        blocker.mkdir()
        (blocker / "sitecustomize.py").write_text(
            "import builtins\n"
            "_real_import = builtins.__import__\n"
            "def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
            "    if name == 'playwright' or name.startswith('playwright.'):\n"
            "        raise ModuleNotFoundError(\"No module named 'playwright'\")\n"
            "    return _real_import(name, globals, locals, fromlist, level)\n"
            "builtins.__import__ = guarded_import\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(blocker), *(part for part in env.get("PYTHONPATH", "").split(os.pathsep) if part)]
        )
        command = [
            sys.executable,
            str(ROOT / "scripts" / "ticket-board-test-suite"),
            "--root",
            str(root),
            "--group",
            "all",
        ]

        proc = subprocess.run(command, text=True, capture_output=True, env=env, check=False)

        output = proc.stdout + proc.stderr
        assert proc.returncode == 0
        assert "PASS [ticket_board] tests/ticket_board_core_test.py" in output
        assert "PASS [ticket_board_frontend] tests/inline_frontend_test.py" in output
        assert "SKIP [ticket_board] tests/ticket_board_browser_test.py" in output
        assert "SKIP [board_adjacent] tests/adjacent_browser_test.py" in output
        assert "SKIP [ticket_board_browser] tests/inline_browser_test.py" in output
        assert "missing Playwright Python package" in output
        assert "summary: 2 passed, 3 skipped, 0 failed" in output
        assert "skipped suites:" in output
        assert "should be skipped" not in output

        fail_proc = subprocess.run(
            [*command, "--fail-on-skip"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        fail_output = fail_proc.stdout + fail_proc.stderr
        assert fail_proc.returncode == 1
        assert "summary: 2 passed, 3 skipped, 0 failed" in fail_output
        assert "skipped suites:" in fail_output


def main() -> int:
    test_discovers_python_and_shell_ticket_board_suites_only()
    test_discovers_board_adjacent_suites_by_content_property()
    test_discovers_host_automation_suites_by_content_property()
    test_discovers_inline_ticket_board_browser_suites_by_content_property()
    test_discovers_inline_ticket_board_frontend_suites_by_content_property()
    test_adjacent_group_runs_only_board_adjacent_suites()
    test_browser_group_runs_only_inline_ticket_board_browser_suites()
    test_frontend_group_runs_only_inline_ticket_board_non_browser_suites()
    test_all_group_includes_inline_ticket_board_importer_suites()
    test_runner_reports_reduced_coverage_without_skipping_suite()
    test_runner_reports_failures_without_stopping_and_strips_live_env()
    test_runner_names_skipped_suites_with_reasons()
    test_runner_skips_every_browser_suite_when_playwright_is_unavailable()
    print("ticket_board_suite_runner_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
