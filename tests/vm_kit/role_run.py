#!/usr/bin/env python3
"""Run one command as a role of a test tenant, inside a tests/vm_kit VM only (SYRD-857).

    sudo python3 -I role_run.py PROJECT ROLE [--timeout SECONDS] -- COMMAND...

It types COMMAND into the role's live pane as a `!` line, which the kit's stand-in agent CLI runs as its own child
(the board accepts a role's write only from that pane's process tree), then prints what it wrote and exits with its
status. So a command behaves exactly as it would typed in that pane: its account, directory, environment, board
socket and caller role. It refuses outside a kit VM, and when no single live pane matches.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = Path("/etc/switchyard-test-vm")


def pane_environment(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    return dict(item.split("=", 1) for item in raw.decode("utf-8", "replace").split("\0") if "=" in item)


def panes(project: str, role: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            environment = pane_environment(int(entry.name))
        except OSError:
            continue
        if b"/usr/local/bin/codex" not in cmdline[:3]:
            continue
        if environment.get("TICKET_BOARD_PROJECT") == project and environment.get("TICKET_BOARD_CALLER_ROLE") == role:
            found.append(int(entry.name))
    return found


def main(argv: list[str]) -> int:
    if "--" not in argv or len(argv) < argv.index("--") + 2:
        raise SystemExit("usage: role_run.py PROJECT ROLE [--timeout SECONDS] -- COMMAND...")
    head, command = argv[:argv.index("--")], argv[argv.index("--") + 1:]
    timeout = float(head[head.index("--timeout") + 1]) if "--timeout" in head else 600.0
    project, role = head[0], head[1]
    if not MARKER.is_file():
        raise SystemExit(f"role_run: refusing: not a switchyard-test-vm review VM ({MARKER} is missing)")
    if os.geteuid() != 0:
        raise SystemExit("role_run: run it with sudo: it reads the pane's environment and types as its account")
    found = panes(project, role)
    if len(found) != 1:
        raise SystemExit(f"role_run: {project} {role}: {len(found)} live panes match, not one: {found}")
    pid = found[0]
    environment = pane_environment(pid)
    uid = os.stat(f"/proc/{pid}").st_uid
    socket_path, target = environment.get("TMUX", "").split(",")[0], environment.get("TICKET_BOARD_PANE_TARGET", "")
    if not socket_path or not target:
        raise SystemExit(f"role_run: {project} {role}: the pane names no tmux socket or target")
    scratch = Path(tempfile.mkdtemp(prefix="role-run."))
    os.chown(scratch, uid, -1)
    out, status = scratch / "out", scratch / "status"
    line = f"!{shlex.join(command)} >{shlex.quote(str(out))} 2>&1 </dev/null; echo $? >{shlex.quote(str(status))}"
    account = ["setpriv", f"--reuid={uid}", f"--regid={os.stat(f'/proc/{pid}').st_gid}", "--init-groups"]
    subprocess.run([*account, "tmux", "-S", socket_path, "send-keys", "-t", target, "-l", line], check=True)
    subprocess.run([*account, "tmux", "-S", socket_path, "send-keys", "-t", target, "Enter"], check=True)
    deadline = time.monotonic() + timeout
    while not status.exists() or not status.read_text().strip():
        if time.monotonic() > deadline:
            sys.stdout.write(out.read_text(errors="replace") if out.exists() else "")
            raise SystemExit(f"role_run: {project} {role}: no status within {timeout:.0f}s")
        time.sleep(0.5)
    sys.stdout.write(out.read_text(errors="replace"))
    code = int(status.read_text().strip())
    subprocess.run(["rm", "-rf", str(scratch)], check=False)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
