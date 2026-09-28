"""A tenant board's workflow, read over the board's own socket.

`read_board_declared_workflow` answers the workflow document the running board
is enforcing; `read_board_workflow_state` answers its revision as well, the
`expected_revision` an install is compared against. Both ask the board rather
than any file, never raise -- any failure to read is "cannot say", with the
reason -- and take a `connection_factory` so a caller can stand the socket in.

Moved out of `scripts/team_launcher.py` unchanged (SYRD-437). The launcher
imports this module and re-exports both names; `pane_rebind.py`,
`tenant_config_records.py`, `workflow_adoption.py` and `workflow_presence.py`
still read them there. Neither reads anything from the launcher: each keeps its
own call-time import of `UnixHTTPConnection` from
`scripts.ticket_board.write_client`, exactly as before, and the config type is
named only in annotations, imported under TYPE_CHECKING. This module never
imports `team_launcher`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scripts.team_launcher import ProjectConfig


def read_board_declared_workflow(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[dict | None, str]:
    """The workflow document the running board is enforcing, over its own socket.

    Asked of the board rather than of any file, because this is the thing an
    adoption has to agree with: the board's copy is what decides every
    transition and capability right now, and it can only have been installed
    through the write API's own authority (SYRD-166).
    """
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/workflow")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return None, f"the board answered HTTP {response.status} for its workflow"
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return None, f"the board's workflow could not be read: {exc}"
    if not isinstance(payload, dict):
        return None, "the board's workflow response is not a document"
    document = payload.get("document")
    if document is None:
        return None, "the board is running no declared workflow"
    if not isinstance(document, dict):
        return None, "the board's workflow response carries no document"
    return document, ""


def read_board_workflow_state(
    config: ProjectConfig,
    *,
    connection_factory: Callable[[str, float], Any] | None = None,
) -> tuple[int, dict | None, str]:
    """The board's workflow revision AND document, over its own socket.

    `read_board_declared_workflow` answers only the document, and the revision
    is what makes an install safe: it is the `expected_revision` the database
    compares under an advisory lock, so a board that changed underneath this is
    a refused write rather than a lost one.
    """
    try:
        from scripts.ticket_board.write_client import UnixHTTPConnection

        factory = connection_factory or (
            lambda socket_path, timeout: UnixHTTPConnection(socket_path, timeout=timeout)
        )
        connection = factory(config.board_socket, 3)
        try:
            connection.request("GET", "/api/workflow")
            response = connection.getresponse()
            body = response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()
        if response.status != 200:
            return 0, None, f"the board answered HTTP {response.status} for its workflow"
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001 - any failure to read is "cannot say"
        return 0, None, f"the board's workflow could not be read: {exc}"
    if not isinstance(payload, dict):
        return 0, None, "the board's workflow response is not a document"
    document = payload.get("document")
    revision = payload.get("revision")
    return (
        int(revision) if isinstance(revision, int) else 0,
        document if isinstance(document, dict) else None,
        "",
    )
