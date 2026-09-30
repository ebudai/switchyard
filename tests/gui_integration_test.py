#!/usr/bin/env python3
"""The opt-in real-GUI boundary, proved without ever starting a real GUI program (SYRD-338).

Every program started here is this test's own stub, written into its own
temporary directory: it records that it ran and exits, or sleeps to stand for a
window left open. Nothing reaches a display, a session bus or a host Konsole.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import gui_integration  # noqa: E402

CHECKS = 0
SUITES = ("legacy_presentation_launch_test.py", "team_launcher_presentation_titles_test.py")
#: What reaches a real Konsole in those suites.
REAL_GUI_HELPERS = {"_run_konsole", "_konsole_reading", "_konsole_reading_once", "_konsole_titles"}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class _Opted:
    """SWITCHYARD_GUI_INTEGRATION as a case says, restored afterwards."""

    def __init__(self, value: str | None) -> None:
        self.value = value

    def __enter__(self):
        self.saved = os.environ.get(gui_integration.OPT_IN)
        if self.value is None:
            os.environ.pop(gui_integration.OPT_IN, None)
        else:
            os.environ[gui_integration.OPT_IN] = self.value
        return self

    def __exit__(self, *_exc):
        if self.saved is None:
            os.environ.pop(gui_integration.OPT_IN, None)
        else:
            os.environ[gui_integration.OPT_IN] = self.saved


class _StubPath:
    """A directory of this test's own stubs, first on PATH, restored afterwards."""

    def __init__(self, tmp: Path, **scripts: str) -> None:
        self.dir = tmp / "stubs"
        self.dir.mkdir()
        self.record = tmp / "ran.txt"
        for name, body in scripts.items():
            stub = self.dir / name
            stub.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {self.record}\n{body}\n", encoding="utf-8")
            stub.chmod(0o755)

    def __enter__(self):
        self.saved = os.environ.get("PATH")
        os.environ["PATH"] = f"{self.dir}:{self.saved or '/usr/bin:/bin'}"
        return self

    def __exit__(self, *_exc):
        if self.saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.saved

    def ran(self) -> list[str]:
        return self.record.read_text(encoding="utf-8").splitlines() if self.record.exists() else []


def test_the_program_that_runs_is_the_one_resolved_on_the_child_s_own_path() -> None:
    """The bypass: a stub first on PATH passed the gate while a pinned PATH ran the real Konsole."""
    with tempfile.TemporaryDirectory(prefix="syrd338-path.") as raw:
        tmp = Path(raw)
        with _StubPath(tmp, konsole="exit 0") as stubs, _Opted("1"):
            env = gui_integration.child_environment(tmp / "sandbox")
            check(env["PATH"] == os.environ["PATH"], "the child is given the caller's PATH, not a pinned one")
            resolved = gui_integration.resolve("konsole", env)
            check(resolved == str(stubs.dir / "konsole"), f"konsole resolves to the stub on the child's PATH: {resolved}")
            # What the old launch would have run instead: whatever /usr/bin:/bin holds.
            pinned = shutil.which("konsole", path="/usr/bin:/bin")
            check(pinned != resolved, f"a pinned PATH resolves somewhere else entirely: {pinned}")
            proc = gui_integration.launch(["konsole", "--nofork", "--layout", str(tmp / "layout.json")], env)
            proc.wait(timeout=30)
            check(stubs.ran() == [f"{stubs.dir / 'konsole'} --nofork --layout {tmp / 'layout.json'}"], stubs.ran())
            check(proc.args[0] == resolved, f"the argv started is the resolved stub: {proc.args}")


def test_nothing_starts_without_the_opt_in() -> None:
    started: list[object] = []
    real_popen = subprocess.Popen

    class Recording:
        def __init__(self, *args, **kwargs):
            started.append(args)
            raise AssertionError("a process was started")

    with tempfile.TemporaryDirectory(prefix="syrd338-refuse.") as raw:
        subprocess.Popen = Recording  # type: ignore[misc]
        try:
            with _Opted(None):
                try:
                    gui_integration.launch(["konsole", "--version"], {"PATH": "/usr/bin:/bin"})
                    refused = ""
                except gui_integration.GuiNotPermitted as exc:
                    refused = str(exc)
            with _Opted("yes"):  # only "1" opts in
                try:
                    gui_integration.launch(["konsole"], {"PATH": "/usr/bin:/bin"})
                    loose = ""
                except gui_integration.GuiNotPermitted as exc:
                    loose = str(exc)
        finally:
            subprocess.Popen = real_popen  # type: ignore[misc]
    check("opt-in" in refused and gui_integration.OPT_IN in refused, refused)
    check("opt-in" in loose, f"an opt-in that is not exactly 1 is not one: {loose!r}")
    check(started == [], f"and nothing reached Popen: {started}")


def test_a_case_that_did_not_run_says_so_and_is_not_counted_as_a_pass() -> None:
    ran: list[str] = []
    gui_integration.NOT_RUN.clear()

    @gui_integration.gui_case("konsole")
    def test_a_real_window() -> None:
        ran.append("body")

    with _Opted(None):
        result = test_a_real_window()
    check(result is None and ran == [], f"its body did not run: {ran}")
    check(gui_integration.NOT_RUN == ["test_a_real_window"], gui_integration.NOT_RUN)
    line = gui_integration.summary()
    check("1 real-GUI integration case(s) NOT RUN" in line and "test_a_real_window" in line
          and gui_integration.OPT_IN in line, line)
    gui_integration.NOT_RUN.clear()
    check(gui_integration.summary() == "", "and a run that skipped nothing says nothing")


def test_opted_in_a_missing_program_fails_the_case() -> None:
    @gui_integration.gui_case("syrd338-no-such-program-anywhere")
    def test_needs_it() -> None:
        raise AssertionError("must not run")

    with _Opted("1"):
        try:
            test_needs_it()
            failed = ""
        except AssertionError as exc:
            failed = str(exc)
    check("opted in, but syrd338-no-such-program-anywhere not on PATH" in failed,
          f"an opted-in case with its program missing did not fail: {failed!r}")


def test_an_empty_reading_is_a_failure_not_a_pass() -> None:
    for reading in ([], ["", ""]):
        try:
            gui_integration.require_reading("split titles", reading)
            failed = False
        except AssertionError:
            failed = True
        check(failed, f"{reading!r} counted as a reading")
    gui_integration.require_reading("split titles", ["Designer", ""])


def test_a_gui_process_left_running_fails_its_case_and_is_stopped() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd338-leak.") as raw:
        tmp = Path(raw)
        started: list[subprocess.Popen] = []
        with _StubPath(tmp, konsole="exec sleep 60"), _Opted("1"):
            @gui_integration.gui_case("konsole")
            def test_forgets_its_window() -> None:
                started.append(gui_integration.launch(["konsole"], gui_integration.child_environment(tmp / "sb")))

            try:
                test_forgets_its_window()
                failed = ""
            except AssertionError as exc:
                failed = str(exc)
        check("left 1 GUI process(es) running" in failed, f"a GUI process left running did not fail its case: {failed!r}")
        deadline = time.monotonic() + 10
        while started and started[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        check(started and started[0].poll() is not None, "and the process was stopped")


def test_every_case_that_reaches_a_real_konsole_is_opt_in() -> None:
    """Static: a future case reaching a real Konsole cannot quietly skip the opt-in."""
    for suite in SUITES:
        tree = ast.parse((ROOT / "tests" / suite).read_text(encoding="utf-8"))
        cases = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
        reaching = [
            case for case in cases
            if any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) in REAL_GUI_HELPERS
                   for n in ast.walk(case))
        ]
        check(reaching, f"{suite} still has real-GUI cases to check")
        for case in reaching:
            marked = any(
                isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == "gui_case"
                for dec in case.decorator_list
            )
            check(marked, f"{suite}::{case.name} reaches a real Konsole without @gui_integration.gui_case")
            # An early return is how a refused stub read as a pass: none is left.
            bare = [n.lineno for n in ast.walk(case) if isinstance(n, ast.Return) and n.value is None]
            check(bare == [], f"{suite}::{case.name} can still return early, passing having read nothing: lines {bare}")
            reads = any(isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) in
                        ("_konsole_reading", "_konsole_titles") for n in ast.walk(case))
            if reads:
                required = any(isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "require_reading"
                               for n in ast.walk(case))
                check(required, f"{suite}::{case.name} reads Konsole without requiring the reading be non-empty")
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in REAL_GUI_HELPERS]
        for helper in helpers:
            direct = [n for n in ast.walk(helper) if isinstance(n, ast.Call)
                      and getattr(n.func, "attr", "") == "Popen"]
            check(direct == [], f"{suite}::{helper.name} starts a process past gui_integration.launch")
        check('"PATH": "/usr/bin:/bin"' not in (ROOT / "tests" / suite).read_text(encoding="utf-8"),
              f"{suite} no longer pins a child PATH")


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"gui_integration_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
