"""Shared test helpers for isolating ticket-board pane hook state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

SESSION_ENV_KEYS = (
    "TICKET_BOARD_PANE_SESSION_ID",
    "PGU_PANE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
    "GEMINI_SESSION_ID",
    "SESSION_ID",
)
HOOK_PATH_ENV_KEYS = (
    "TICKET_BOARD_PANE_SESSION_DIR",
    "PGU_TICKET_BOARD_PANE_SESSION_DIR",
    "TICKET_BOARD_PANE_STATE_DIR",
    "PGU_TICKET_BOARD_PANE_STATE_DIR",
)
HOOK_TARGET_ENV_KEYS = (
    "TICKET_BOARD_PANE_TARGET",
    "PGU_PANE_TARGET",
)
STATE_HOME_ENV_KEYS = ("XDG_STATE_HOME",)
TICKET_BOARD_PANE_ENV_KEYS = (*SESSION_ENV_KEYS, *HOOK_PATH_ENV_KEYS, *HOOK_TARGET_ENV_KEYS, *STATE_HOME_ENV_KEYS)


def strip_ticket_board_pane_env(environ: dict[str, str], *, keep: tuple[str, ...] = ()) -> dict[str, str]:
    saved: dict[str, str] = {}
    keep_set = set(keep)
    for key in TICKET_BOARD_PANE_ENV_KEYS:
        if key in keep_set:
            continue
        if key in environ:
            saved[key] = environ.pop(key)
    return saved


def restore_env(environ: dict[str, str], saved: Mapping[str, str]) -> None:
    for key in TICKET_BOARD_PANE_ENV_KEYS:
        environ.pop(key, None)
    environ.update(saved)


def stripped_ticket_board_pane_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    strip_ticket_board_pane_env(env)
    env.update(extra)
    return env


def candidate_live_pane_paths(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = environ or os.environ
    candidates: list[Path] = []
    for key in ("TICKET_BOARD_PANE_SESSION_DIR", "PGU_TICKET_BOARD_PANE_SESSION_DIR"):
        value = env.get(key, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    for key in ("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR"):
        value = env.get(key, "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    xdg_state_home = env.get("XDG_STATE_HOME", "").strip()
    state_home = Path(xdg_state_home).expanduser() if xdg_state_home else Path.home() / ".local" / "state"
    candidates.append(state_home / "pgu-ticket-board" / "pane-sessions")
    candidates.append(Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-sessions"))
    candidates.append(Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state"))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(candidate.resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(candidate)
    return unique


def snapshot_path(path: Path) -> dict[str, bytes] | None:
    try:
        if not path.exists():
            return None
        if not path.is_dir():
            return {}
        snapshot: dict[str, bytes] = {}
        for child in sorted(path.rglob("*")):
            if child.is_file():
                snapshot[str(child.relative_to(path))] = child.read_bytes()
        return snapshot
    except OSError:
        return {}


def snapshot_paths(paths: list[Path]) -> dict[str, dict[str, bytes] | None]:
    return {str(path): snapshot_path(path) for path in paths}


def snapshot_diff(
    before: dict[str, dict[str, bytes] | None],
    after: dict[str, dict[str, bytes] | None],
) -> list[str]:
    changed: list[str] = []
    for root in sorted(set(before) | set(after)):
        before_files = before.get(root)
        after_files = after.get(root)
        if before_files is None or after_files is None:
            if before_files != after_files:
                changed.append(root)
            continue
        for name in sorted(set(before_files) | set(after_files)):
            if before_files.get(name) != after_files.get(name):
                changed.append(f"{root}/{name}")
    return changed
