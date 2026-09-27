"""The board-authority launch preflight: refusing to launch against a board that lacks process authority.

`process_authority_board_compatibility` asks the project's running board, over
its local socket, for its runtime assignments, and answers whether that board
already authorizes roles by project-account process authority. The launcher
asks it before a launch changes any layout, worktree, state file or tmux
session, so a config repatriated by a newer launcher never starts against an
older board (SYRD-69).

It reads nothing from `scripts/team_launcher.py`; its board connection is its
own function-level import.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-334). `team_launcher`
imports this module at its top, still exports the function, and calls it by
that name, so the suites' rebinding of it still reaches the launch. This module
never imports `team_launcher`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig

def process_authority_board_compatibility(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[bool, str]:
    """Prove the running board understands shared-account process authority.

    This check happens before launch mutates layouts, worktrees, state files, or
    tmux.  It is the mixed-version boundary: a config repatriated by the new
    launcher must not start against an older board that would either reject the
    shared uid or authorize it with the retired uid map (SYRD-69).
    """
    if not config.role_state_isolation:
        return True, "legacy account authority"
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/runtime-assignments")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return False, f"runtime assignment probe returned HTTP {response.status}: {body}"
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("project") != config.project:
            return False, "runtime assignment probe returned another project's identity"
        if payload.get("authority_mode") != "process":
            return False, "running board still uses legacy uid authority"
        if not isinstance(payload.get("assignments"), dict):
            return False, "runtime assignment probe omitted its assignments object"
    except Exception as exc:  # noqa: BLE001 - refusal must preserve the exact boundary failure
        return False, str(exc)
    return True, "process authority ready"
