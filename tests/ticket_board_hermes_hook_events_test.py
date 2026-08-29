#!/usr/bin/env python3
"""Validate installed Hermes pane hooks against Hermes' accepted event names."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "ticket-board-install-pane-hooks"
HOOK_NAME = "ticket-board-pane-idle-hook"
EXPECTED_HERMES_EVENTS = {
    "on_session_start",
    "pre_llm_call",
    "post_llm_call",
    "on_session_end",
    "on_session_finalize",
}


def _hermes_valid_hook_events(hermes: str) -> set[str]:
    proc = subprocess.run(
        [hermes, "hooks", "test", "__pgu_invalid_hook_event__"],
        text=True,
        capture_output=True,
        check=False,
    )
    text = f"{proc.stdout}\n{proc.stderr}"
    marker = "Valid events:"
    assert marker in text, text
    valid_lines: list[str] = []
    for line in text.split(marker, 1)[1].strip().splitlines():
        stripped = line.strip()
        if not stripped:
            break
        valid_lines.append(stripped)
    return {event.strip() for event in " ".join(valid_lines).split(",") if event.strip()}


def _load_yaml(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_installed_hermes_hooks_use_recognized_turn_level_events() -> None:
    hermes = shutil.which("hermes")
    assert hermes is not None
    with tempfile.TemporaryDirectory(prefix="pgu-hermes-hook-events.") as tmp:
        root = Path(tmp)
        home = root / "home"
        env = os.environ.copy()
        env.update(
            {
                "TICKET_BOARD_PROJECT": "otto",
                "TICKET_BOARD_PANE_STATE_DIR": str(root / "run" / "otto-ticket-board" / "pane-state"),
                "TICKET_BOARD_PANE_SESSION_DIR": str(root / "state" / "otto-ticket-board" / "pane-sessions"),
            }
        )
        subprocess.run(
            [
                str(INSTALLER),
                "install",
                "--home",
                str(home),
                "--bin-path",
                str(home / ".local" / "bin" / HOOK_NAME),
            ],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )

        config = _load_yaml(home / ".hermes" / "config.yaml")
        assert set(config["hooks"]) == EXPECTED_HERMES_EVENTS
        assert EXPECTED_HERMES_EVENTS <= _hermes_valid_hook_events(hermes)


def main() -> int:
    if shutil.which("hermes") is None:
        print(
            "ticket_board_hermes_hook_events_test: skipped, missing hermes CLI; "
            "install hermes to validate Hermes hook event names"
        )
        return 0
    test_installed_hermes_hooks_use_recognized_turn_level_events()
    print("ticket_board_hermes_hook_events_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
