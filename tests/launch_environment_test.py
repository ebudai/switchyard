#!/usr/bin/env python3
"""SYRD-752: the environment a launch for another pane starts from -- the caller's, without its pane identity or any
supported runtime's nesting variables, and nothing else taken away -- and put back exactly afterwards."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from scripts import launch_environment as le  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def test_each_runtimes_nesting_variables_are_dropped() -> None:
    nested = ["CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
              "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_SOMETHING_NEW", "CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID",
              "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "HERMES_SESSION_ID", "HERMES_SESSION_SOURCE",
              "HERMES_YOLO_MODE", "HERMES_TUI_RESUME", "COPILOT_CLI_VERSION", "COPILOT_SEA_EXTRACT_ONLY",
              "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CSRF_TOKEN"]
    check(all(le.is_runtime_nesting(name) for name in nested),
          f"each supported runtime's own child variables, prefixes included: {[n for n in nested if not le.is_runtime_nesting(n)]}")
    check(set(le.RUNTIME_NESTING_ENV) == set(launcher.EFFORT_STYLE_BY_CLI),
          f"every supported runtime declares its own: {sorted(le.RUNTIME_NESTING_ENV)} {sorted(launcher.EFFORT_STYLE_BY_CLI)}")


def test_only_the_callers_identity_and_nesting_go() -> None:
    kept = {"PATH": "/usr/bin", "HOME": "/home/o", "TMUX": "/tmp/tmux-1/default,1,0", "TMUX_TMPDIR": "/tmp",
            "TICKET_BOARD_PANE_STATE_DIR": "/run/user/1/p-ticket-board/pane-state",
            "TICKET_BOARD_PANE_SESSION_DIR": "/home/o/.local/state/pgu/pane-sessions",
            "PGU_TICKET_BOARD_PANE_STATE_DIR": "/run/x", "PGU_TICKET_BOARD_PANE_SESSION_DIR": "/home/x",
            # A runtime's configuration is read, not set for children: not the caller's identity.
            "CODEX_HOME": "/home/o/.codex", "HERMES_HOME": "/home/o/.hermes", "COPILOT_HOME": "/home/o/.copilot",
            "CLAUDE_CONFIG_DIR": "/home/o/.claude", "XDG_STATE_HOME": "/home/o/.local/state"}
    gone = {"TICKET_BOARD_PANE_SESSION_ID": "c", "PGU_PANE_SESSION_ID": "c", "TICKET_BOARD_PANE_TARGET": "p-director:0.0",
            "PGU_PANE_TARGET": "p-director:0.0", "TICKET_BOARD_CALLER_ROLE": "director", "TMUX_PANE": "%3",
            "CLAUDECODE": "1", "CODEX_THREAD_ID": "t",
            **{key: "x" for key in launcher.PRESENTATION_HANDOFF_ENV_KEYS}}
    built = le.launch_environment({**kept, **gone})
    check(built == kept, f"exactly the caller's identity and nesting go: {sorted(set(built) ^ set(kept))}")


def test_applied_puts_the_environment_back_however_the_launch_ends() -> None:
    with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "TICKET_BOARD_CALLER_ROLE": "director", "KEEP_ME": "k"}):
        before = dict(os.environ)
        seen: dict[str, str] = {}
        try:
            with le.applied() as clean:
                seen.update(os.environ)
                check(clean == dict(os.environ), "inside, this process's environment is the launch environment")
                raise RuntimeError("the launch failed")
        except RuntimeError:
            pass
        check("CLAUDECODE" not in seen and "TICKET_BOARD_CALLER_ROLE" not in seen and seen.get("KEEP_ME") == "k",
              f"inside, without the caller's identity and nesting, and nothing else gone: {sorted(set(before) - set(seen))}")
        check(dict(os.environ) == before, "and afterwards, exactly as before, though the launch raised")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"launch_environment_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
