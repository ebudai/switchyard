"""Resolve runtime tools from the selected immutable Switchyard release."""

from __future__ import annotations

import os
from pathlib import Path


def release_root(module_file: str | Path = __file__) -> Path:
    """Return the release or source root containing ``scripts/ticket_board``."""

    return Path(module_file).resolve().parents[2]


def directorctl_path(module_file: str | Path = __file__) -> str:
    """Return an explicit directorctl override or this release's reviewed copy.

    Runtime callers deliberately do not search PATH: a per-checkout or another
    tenant's stale helper must never win over the code shipped with the board.
    """

    configured = os.environ.get("TICKET_BOARD_DIRECTORCTL", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise RuntimeError("TICKET_BOARD_DIRECTORCTL must be an absolute path")
        return str(path)
    return str(release_root(module_file) / "scripts" / "directorctl")
