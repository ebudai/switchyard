"""The board-authority launch preflight: refusing to launch against a board that lacks process authority.

`process_authority_board_compatibility` asks the project's running board, over
its local socket, for its runtime assignments, and answers whether that board
already authorizes roles by project-account process authority. The launcher
asks it before a launch changes any layout, worktree, state file or tmux
session, so a config repatriated by a newer launcher never starts against an
older board (SYRD-69).

`board_socket_answer` is the same request asked only whether this project's
board answers it: how a resume knows the board it just started is up before
anything asks it for authority (SYRD-670).

It reads nothing from `scripts/team_launcher.py`; its board connection is its
own function-level import.

As root, both ask the board root's own unit names, over a connection the kernel
shows that board's process holds (`board_identity.board_connection_factory`),
never the `board_socket` the tenant's configuration names; and an exception's
words reach root's terminal as plain text only (SYRD-769).

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
    from scripts.ticket_board.phone_text import clean_text

    try:
        from scripts.ticket_board.board_identity import board_connection_factory
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = board_connection_factory(config, connection_factory) or (
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
            # The board's body, shown to root at `start`: plain text only (SYRD-766).
            return False, f"runtime assignment probe returned HTTP {response.status}: {clean_text(body, 300)}"
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("project") != config.project:
            return False, "runtime assignment probe returned another project's identity"
        if payload.get("authority_mode") != "process":
            return False, "running board still uses legacy uid authority"
        if not isinstance(payload.get("assignments"), dict):
            return False, "runtime assignment probe omitted its assignments object"
    except Exception as exc:  # noqa: BLE001 - refusal must preserve the exact boundary failure
        # A raw status line or reason phrase can ride in it: plain text only (SYRD-769).
        return False, clean_text(exc, 300)
    return True, "process authority ready"


def board_socket_answer(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[str, str]:
    """Whether this project's board answers on its write socket: ("answered", ""), ("silent", why) while nothing
    answers there yet, or ("refused", why) for an answer that is not this board's.

    Only HTTP 200 with a JSON object naming this project is this board: the
    route every board this release resumes serves, to any caller on its socket.
    Another project's name, no name, a body that is not JSON, or any other status
    is something else answering there -- an incompatible board, or not a board
    -- and no reason to wait (Audit of 4ae2184d).
    """
    from scripts.ticket_board.board_identity import BoardIdentityError, board_connection_factory
    from scripts.ticket_board.phone_text import clean_text

    root = board_connection_factory(config, connection_factory)
    where = (f"what answers on {config.board_socket}" if root is None or root is connection_factory
             else f"what answers on the socket root's unit names for {config.project}")
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = root or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/runtime-assignments")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
    except BoardIdentityError as exc:  # root's unit unreadable, or not its board answering: no reason to wait
        return "refused", f"{where} is not {config.project}'s board: {clean_text(exc, 300)}"
    except Exception as exc:  # noqa: BLE001 - not answering, whatever the reason, is the answer
        return "silent", clean_text(exc, 300)
    if response.status != 200:
        return "refused", (f"{where} answered GET /api/runtime-assignments with HTTP {response.status}, which no board "
                           f"this release resumes does: {body[:120]!r}")
    try:
        payload = json.loads(body)
    except ValueError:
        return "refused", f"{where} answered with a body that is not JSON: {body[:120]!r}"
    named = payload.get("project") if isinstance(payload, dict) else None
    if named != config.project:
        return "refused", (f"{where} answers as {named!r}, not {config.project!r}" if isinstance(named, str)
                           else f"{where} names no project")
    return "answered", ""
