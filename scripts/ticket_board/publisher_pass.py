"""The listener's Publisher pass: start the Publisher's worker when publication work is waiting (SYRD-625).

On every listener pass, on a board whose workflow declares the Publisher
(`publisher_stage`): when tickets are ready in the publication stage and no
worker is running, one background worker is started with its own database
connection, and it publishes them one at a time (`publisher.publish_ticket`).
The pass itself never waits on git or the network, so notifications keep
flowing while a push is in flight.

A ticket whose run ended without the board recording anything -- the database
unreachable, say -- is not tried again for `RETRY_AFTER_SECONDS`, so a failure
the Publisher cannot record is not repeated on every pass. Its attempt stays
open in the board's journal and the next run resumes it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .publisher_stage import publication_stage

RETRY_AFTER_SECONDS = 60.0


def run(listener: Any, conn: Any) -> int:
    """One pass; returns 1 when it started the worker, else 0."""
    if publication_stage(getattr(listener, "workflow", None)) is None:
        return 0
    worker = getattr(listener, "publisher_worker", None)
    if worker is not None and worker.is_alive():
        return 0
    held = getattr(listener, "publisher_held", {})
    now = time.monotonic()
    from .publisher import Journal

    pending = [ticket for ticket in Journal(conn).pending() if held.get(ticket, 0.0) <= now]
    if not pending:
        return 0
    listener.publisher_held = held
    listener.publisher_worker = threading.Thread(target=work, args=(listener, pending), name="ticket-board-publisher",
                                                 daemon=True)
    listener.publisher_worker.start()
    return 1


def work(listener: Any, tickets: list[str]) -> None:
    """Publish `tickets` in order, each on a fresh connection, logging what the board recorded."""
    from .publisher import Git, board_commit_repositories, publish_ticket, state_root

    root = getattr(listener, "publisher_root", None) or state_root(listener.project)
    for ticket in tickets:
        if listener.stop_event.is_set():
            return
        try:
            with listener.connector(listener.conninfo, **listener._connector_kwargs()) as conn:
                result = publish_ticket(conn, ticket, root=root,
                                        git=getattr(listener, "publisher_git", None) or Git(),
                                        repositories=getattr(listener, "publisher_repositories", None)
                                        or (lambda: board_commit_repositories(dict(os.environ))))
            listener.logger.info("Publisher: %s %s", ticket, result.get("outcome"))
            if result.get("outcome") in ("superseded", "not_ready") or result.get("recorded"):
                listener.publisher_held.pop(ticket, None)
        except Exception as exc:  # recorded nothing: say so, and leave the open attempt for a later run
            listener.publisher_held[ticket] = time.monotonic() + RETRY_AFTER_SECONDS
            listener.logger.warning("Publisher: %s was not recorded (%s); retrying in %.0fs", ticket,
                                    str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                                    RETRY_AFTER_SECONDS)
