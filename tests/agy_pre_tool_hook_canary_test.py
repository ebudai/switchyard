#!/usr/bin/env python3
"""SYRD-890 canary: the agy installed on this host runs an agy role's tool calls through Switchyard's PreToolUse guard.

On ocstab (2026-11-27) a Researcher on agy 1.5.17 had every tool call refused, "tool call denied by pre-tool hook: "
with no reason, for two hours: the shipped guard answered `{}` on an allowed call, and agy 1.5.17 requires a
`decision` on every PreToolUse answer and denies on none. The guard's own tests ran the guard, never agy; and agy
updates outside Switchyard's deploys (its package, not a release), so only the installed agy can say what it accepts.

This runs the host's real `agy` offline, in a private user and network namespace (loopback only), with a throwaway
HOME and a fake account -- tests/fixtures/syrd890_probe, built on SYRD-756's rig -- and this tree's real hook installer
and shell guard:

* an ordinary command (`echo PROBE-TOOL-RAN`) runs: the guard was called once and answered `allow`;
* board prose the shell would expand (`ticket-board-write add-comment ... --text "Run `printf marker` first."`) is
  denied, and the tool result agy hands the model carries the guard's reason (SYRD-753's refusal);
* if agy ran no hook at all, nothing was proved and the canary fails, naming the likely cause: the stand-in no longer
  answers the experiment flags agy runs hooks under.

Run it after agy updates, on the host whose agy the roles run. Where there is no `agy`, or no user and network
namespace (inside the sweep sandbox, for one), it says SKIP and why, and passes: it proves nothing there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "fixtures" / "syrd890_probe" / "tool_probe.py"
REFUSED = 'ticket-board-write add-comment X-1 --text "Run `printf marker` first."'
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def skip_reason() -> str:
    if shutil.which("agy") is None:
        return "agy is not installed on this host"
    for tool in ("unshare", "tmux", "openssl"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, text=True)
    if probe.returncode != 0:
        return f"no private user and network namespace here ({probe.stderr.strip()[:200]})"
    return ""


def probe(out: Path, command: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    done = subprocess.run(["unshare", "-rn", sys.executable, str(PROBE), str(out), command], capture_output=True,
                          text=True, env={**env, "PROBE_REPO": str(ROOT)})
    summary = out / "summary.json"
    check(summary.exists(), f"the probe ran and wrote its summary: exit {done.returncode} {done.stderr[-800:]}")
    return json.loads(summary.read_text())


def main() -> int:
    reason = skip_reason()
    if reason:
        print(f"agy_pre_tool_hook_canary_test: SKIP: {reason}")
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd890-canary.") as tmp:
        seen = probe(Path(tmp) / "allowed", "echo PROBE-TOOL-RAN")
        version = seen.get("versions")
        check(seen.get("guard_calls", 0) >= 1,
              f"agy {version} ran the PreToolUse hook (else nothing here was proved: the stand-in may no longer turn "
              f"on the experiment flags agy runs hooks under): {seen}")
        check(seen.get("tool_ran") and not seen.get("denied_by_hook"),
              f"agy {version} ran the ordinary command the guard passed: {seen.get('function_response')}")
        check(seen.get("guard_printed") == ['{"decision": "allow"}'],
              f"the guard answered agy {version}'s ordinary call with allow: {seen.get('guard_printed')}")
        seen = probe(Path(tmp) / "refused", REFUSED)
        response = str(seen.get("function_response") or "")
        check(seen.get("denied_by_hook") and not seen.get("tool_ran"),
              f"agy {version} denied board prose the guard refused: {response}")
        check("Switchyard refused this command before it ran" in response,
              f"and handed the model the guard's reason, not an empty one: {response}")
    print(f"agy_pre_tool_hook_canary_test: {CHECKS} checks ok (agy {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
