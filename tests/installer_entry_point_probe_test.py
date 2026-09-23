#!/usr/bin/env python3
"""A healthy install does not print a page of argparse help.

Zorin install of release a974471: the prereqs installer announced

    Verifying the ticket board entry point loads under the shared Switchyard
    Python venv.

and then printed the entry point's entire `--help` -- usage line, every option
-- before carrying on into a successful install. Nothing was wrong, but 29
lines of argparse in a privileged transcript read exactly like a command
rejected for a wrong parameter, and buried the diagnostics either side of it
(SYRD-215).

The probe still runs and still proves the same thing. These cases drive the
real installer with a stubbed interpreter on PATH, so what is asserted is what
an operator sees: quiet on success, the entry point's own error on failure, and
`--help` still printing help when a human asks for it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests  # noqa: E402

PREREQS = ROOT / "scripts" / "install-switchyard-prereqs"
ENTRY_POINT = ROOT / "scripts" / "ticket-board.py"

#: What the entry point prints for `--help`, and what an install must not.
HELP_MARKERS = ("usage: ticket-board.py", "show this help message and exit", "--unix-socket")
#: What a real failed load looks like: the interpreter's own words.
TRACEBACK = "ModuleNotFoundError: No module named 'psycopg'"

#: Everything the pacman branch reaches for before the probe. None of it is
#: what this ticket is about, so all of it succeeds quietly.
STUBS = {
    "sudo": """#!/bin/bash
while (($#)); do case "$1" in -n|-H) ;; -u) shift ;; --) shift; break ;; *) break ;; esac; shift; done
exec "$@"
""",
    "runuser": """#!/bin/bash
while (($#)); do case "$1" in -u) shift ;; --) shift; break ;; *) break ;; esac; shift; done
exec "$@"
""",
    "pacman": """#!/bin/bash
exit 0
""",
    "systemctl": """#!/bin/bash
[[ "$1" == "show" ]] && printf 'PGROOT=%s\\n' "$PROBE_STATE/pgroot"
exit 0
""",
    "psql": """#!/bin/bash
echo 1
exit 0
""",
}


class Installer:
    """The real prereqs installer, with a stubbed interpreter and host tools."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd215."))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.log = self.tmp / "probe.log"

    def interpreter(self, *, loads: bool) -> Path:
        """An interpreter that either loads the entry point, or does not.

        Its success output is the real thing: `--help` prints the help, which
        is exactly what used to end up in the transcript.
        """
        path = self.tmp / "fake-python"
        if loads:
            body = f"""#!/bin/bash
printf '%s\\n' "$*" >> {self.log}
if [[ "$*" == *--help* ]]; then
  echo 'usage: ticket-board.py [-h] [--database DATABASE] [--unix-socket UNIX_SOCKET]'
  echo ''
  echo 'options:'
  echo '  -h, --help            show this help message and exit'
  echo '  --unix-socket UNIX_SOCKET  path to the board socket'
  exit 0
fi
echo '3.3.1 11.0.0'
exit 0
"""
        else:
            body = f"""#!/bin/bash
printf '%s\\n' "$*" >> {self.log}
if [[ "$*" == *--help* ]]; then
  echo 'Traceback (most recent call last):' >&2
  echo '  File "ticket-board.py", line 12, in <module>' >&2
  echo "{TRACEBACK}" >&2
  exit 1
fi
echo '3.3.1 11.0.0'
exit 0
"""
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def run(self, *, loads: bool = True, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
        python = self.interpreter(loads=loads)
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "PROBE_STATE": str(self.state),
            "SWITCHYARD_PREREQS_ASSUME_MANAGER": "pacman",
            "SWITCHYARD_SUDO_BIN": "sudo",
            "SWITCHYARD_PREREQS_FORCE_SUDO_PREFIX": "1",
            # The interpreter the generated services would use, which is the
            # one the pacman branch verifies.
            "SWITCHYARD_SERVICE_FALLBACK_PYTHON": str(python),
            "SWITCHYARD_PYTHON_VENV": str(self.tmp / "no-venv"),
            "SWITCHYARD_PG_WAIT_SECONDS": "1",
        }
        args = ["--dry-run"] if dry_run else []
        return subprocess.run(
            [str(PREREQS), *args], env=env, capture_output=True, text=True, timeout=180
        )

    @property
    def probes(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_a_successful_install_prints_no_argparse_help() -> None:
    installer = Installer()
    try:
        result = installer.run()
        assert result.returncode == 0, (result.stdout, result.stderr)
        printed = result.stdout + result.stderr
        for marker in HELP_MARKERS:
            assert marker not in printed, (marker, printed)
        # It still ran, and still asked the same question.
        assert any("--help" in probe for probe in installer.probes), installer.probes
        # And said so once.
        success = [line for line in result.stdout.splitlines()
                   if line.startswith("Ticket board entry point loads with ")]
        assert len(success) == 1, result.stdout
    finally:
        installer.close()


def test_the_explanatory_line_and_the_command_are_still_shown() -> None:
    """Quiet is not silent: an operator can still see what was run."""
    installer = Installer()
    try:
        result = installer.run()
        assert "Verifying the ticket board entry point loads with" in result.stdout, result.stdout
        ran = [line for line in result.stdout.splitlines()
               if line.startswith("+ ") and "ticket-board.py" in line]
        assert len(ran) == 1 and ran[0].endswith("--help"), result.stdout
    finally:
        installer.close()


def test_a_failed_load_shows_the_real_error_and_fails() -> None:
    installer = Installer()
    try:
        result = installer.run(loads=False)
        assert result.returncode != 0, (result.stdout, result.stderr)
        assert TRACEBACK in result.stderr, result.stderr
        assert "Traceback (most recent call last):" in result.stderr, result.stderr
        assert "the ticket board entry point did not load" in result.stderr, result.stderr
        # The interpreter that failed is named, so a two-interpreter host says
        # which one this was.
        assert "fake-python" in result.stderr, result.stderr
        assert "Ticket board entry point loads with" not in result.stdout, result.stdout
    finally:
        installer.close()


def test_a_dry_run_still_describes_the_probe_and_runs_nothing() -> None:
    installer = Installer()
    try:
        result = installer.run(dry_run=True)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert any("ticket-board.py" in line and line.startswith("+ ")
                   for line in result.stdout.splitlines()), result.stdout
        assert installer.probes == [], installer.probes
        for marker in HELP_MARKERS:
            assert marker not in result.stdout, marker
    finally:
        installer.close()


def test_the_entry_point_still_prints_help_when_a_human_asks() -> None:
    """The output was never the problem; printing it unasked was (SYRD-215)."""
    result = subprocess.run(
        [sys.executable, str(ENTRY_POINT), "--help"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    for marker in HELP_MARKERS:
        assert marker in result.stdout, (marker, result.stdout[:400])


def test_the_installer_never_pipes_the_probe_into_the_transcript_again() -> None:
    """The shape, not only the behaviour: no bare probe left echoing its output."""
    body = PREREQS.read_text(encoding="utf-8")
    running = [
        line.strip() for line in body.splitlines()
        if "ticket-board.py" in line and line.strip().startswith(("run_cmd", "run_root_cmd"))
    ]
    assert running == [], running
    assert "verify_ticket_board_entry_point" in body


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"installer_entry_point_probe_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
