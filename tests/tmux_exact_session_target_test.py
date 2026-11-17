#!/usr/bin/env python3
"""SYRD-832: a role whose session name prefixes another's is asked about, and sent to, as itself.

mefp, 2026-11-17: the roles audit and audit-2 have the sessions mefp-audit and mefp-audit-2. audit-2's started first,
and tmux answered every question about the absent mefp-audit with mefp-audit-2 (it matches a session target by
prefix when the exact name is absent). audit was never started, and restart-role read audit-2's pane as audit's.

On a private tmux server holding only the longer session (x-audit-2; luna-10), with the launcher's own argv builders
and directorctl's own target rule:
* has-session for the shorter one says absent; its pane pid and current command come back empty, never the longer
  session's (display-message -p exits 0 on an absent target, with empty fields);
* a send to it fails and never reaches the longer session's pane;
* once the shorter session is started, each is answered for itself and a send reaches only its own pane.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_socket_cleanup import end_isolated_tmux_server, own_isolated_tmux_server, run_isolated_tmux  # noqa: E402

CHECKS = 0
PAIRS = (("x-audit", "x-audit-2"), ("luna-1", "luna-10"))


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def role(session: str) -> SimpleNamespace:
    return SimpleNamespace(tmux_session=session, target=f"{session}:0.0")


def ask(server: str, argv: list[str]) -> subprocess.CompletedProcess:
    assert argv[0] == "tmux", argv
    return run_isolated_tmux(server, argv[1:], capture_output=True, text=True, timeout=10)


def directorctl_send(server: str, target: str, text: str, scratch: Path) -> int:
    """A send as directorctl makes it: its own exact_tmux_target, then send-keys -l and Enter, on this server."""
    source = (ROOT / "scripts" / "directorctl").read_text(encoding="utf-8")
    function = re.search(r"^exact_tmux_target\(\) \{\n.*?^\}\n", source, re.M | re.S)
    assert function, "directorctl defines exact_tmux_target"
    script = scratch / "send.sh"
    script.write_text("#!/bin/bash\n" + function.group(0) + f"""
tmux -L {server} send-keys -t "$(exact_tmux_target "$1")" -l "$2" || exit $?
tmux -L {server} send-keys -t "$(exact_tmux_target "$1")" Enter
""", encoding="utf-8")
    return subprocess.run(["bash", str(script), target, text], capture_output=True, timeout=10).returncode


def received(path: Path, *, wait: float = 1.0) -> str:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            break
        time.sleep(0.05)
    return path.read_text() if path.exists() else ""


def test_a_prefix_is_never_another_session() -> None:
    from scripts.pane_pid import tmux_pane_pid_args
    from scripts.tmux_session_argv import tmux_current_command_args, tmux_has_session_args

    with tempfile.TemporaryDirectory(prefix="syrd832.") as raw:
        scratch = Path(raw)
        for short, long in PAIRS:
            server = own_isolated_tmux_server(f"syrd832-{short}-{os.getpid()}")
            try:
                inbox_long, inbox_short = scratch / f"{long}.in", scratch / f"{short}.in"
                run_isolated_tmux(server, ["-f", "/dev/null", "new-session", "-d", "-s", long, f"cat > {inbox_long}"],
                                  check=True, timeout=10)
                time.sleep(0.2)
                absent = ask(server, tmux_has_session_args(role(short)))
                check(absent.returncode != 0, f"{short} is absent though {long} runs: {absent}")
                # display-message -p on an absent target exits 0 with empty fields (measured, tmux 3.8); without the
                # exact match it printed the neighbour's pid -- what restart-role read as audit's on mefp.
                pid = ask(server, tmux_pane_pid_args(role(short))).stdout.strip()
                check(pid == "", f"{short}'s pane pid is not read from {long}'s pane: {pid!r}")
                current = ask(server, tmux_current_command_args(role(short))).stdout.strip()
                check(current == "", f"nor its current command: {current!r}")
                code = directorctl_send(server, f"{short}:0.0", f"meant for {short}", scratch)
                check(code != 0 and received(inbox_long, wait=0.5) == "",
                      f"a send to the absent {short} fails and never reaches {long}: rc={code} "
                      f"{long} got {received(inbox_long, wait=0)!r}")

                run_isolated_tmux(server, ["new-session", "-d", "-s", short, f"cat > {inbox_short}"], check=True,
                                  timeout=10)
                time.sleep(0.2)
                check(ask(server, tmux_has_session_args(role(short))).returncode == 0, f"{short}, once started, is present")
                pid_short = ask(server, tmux_pane_pid_args(role(short))).stdout.strip()
                pid_long = ask(server, tmux_pane_pid_args(role(long))).stdout.strip()
                check(pid_short and pid_long and pid_short != pid_long,
                      f"each pane pid is its own: {short}={pid_short} {long}={pid_long}")
                check(directorctl_send(server, f"{short}:0.0", f"meant for {short}", scratch) == 0
                      and f"meant for {short}" in received(inbox_short) and received(inbox_long, wait=0) == "",
                      f"a send to {short} reaches {short}'s pane alone")
            finally:
                end_isolated_tmux_server(server)


def main() -> int:
    import shutil

    if shutil.which("tmux") is None:
        print("tmux_exact_session_target_test: skipped (no tmux)")
        return 0
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    test_a_prefix_is_never_another_session()
    print(f"tmux_exact_session_target_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
