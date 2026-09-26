"""Environment lookups the launcher and its modules resolve before anything else runs.

`_env_first` returns the first non-empty variable among its names.
`DEFAULT_PANE_STATE_DIR` is where pane state lives by default. Several
functions take it as a default argument -- the role pane entry points and the
launcher's own -- so it lives in this leaf, which they all import, and each
default binds the same object.

The suites rebind `team_launcher.DEFAULT_PANE_STATE_DIR` and
`team_launcher._env_first`. Those rebindings reach whatever reads them through
the launcher when it runs, as `scripts/session_paths.py` does.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-313); `team_launcher`
still exports both names.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


DEFAULT_PANE_STATE_DIR = (
    Path(_env_first("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")).expanduser()
    if _env_first("TICKET_BOARD_PANE_STATE_DIR", "PGU_TICKET_BOARD_PANE_STATE_DIR")
    else Path(f"/run/user/{os.getuid()}/pgu-ticket-board/pane-state")
)
