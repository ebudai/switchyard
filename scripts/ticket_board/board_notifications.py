"""Who the board tells when it changes: its director and its open browsers.

DirectorNotifier batches new-ticket notices into one directorctl send.
TicketBoardEventHub versions changes for the event stream and rescans the
store for changes made elsewhere. The server constructs and closes both; the
handler signals them after writes and serves the stream.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from typing import TYPE_CHECKING, Callable

from .app import project_slug
from .runtime_paths import directorctl_path

if TYPE_CHECKING:
    from .app import TicketBoardApp

# Records keep the server's logger name, where operators and tests have always
# found these failures.
LOGGER = logging.getLogger(__name__.rpartition(".")[0] + ".server")
DEFAULT_DIRECTORCTL = directorctl_path(__file__)
DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS = 0.35


def director_target(project: str | None = None) -> str:
    resolved_project = (project or project_slug()).strip().lower() or "pgu"
    return f"{resolved_project}-director:0.0"


def send_director_message(payload: str, target: str | None = None) -> None:
    subprocess.run(
        [DEFAULT_DIRECTORCTL, "send", target or director_target(), payload],
        check=True,
        capture_output=True,
        text=True,
    )


class DirectorNotifier:
    def __init__(
        self,
        *,
        sender: Callable[[str], None] | None = None,
        project: str | None = None,
        batch_window_seconds: float = DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS,
    ) -> None:
        target = director_target(project)
        self.sender = sender or (lambda payload: send_director_message(payload, target))
        self.batch_window_seconds = batch_window_seconds
        self._lock = threading.Lock()
        self._pending_created: list[tuple[str, str]] = []
        self._timer: threading.Timer | None = None

    def notify_ticket_created(self, ticket: dict[str, object]) -> None:
        ticket_id = str(ticket.get("id", "")).strip()
        title = str(ticket.get("title", "")).strip()
        if not ticket_id or not title:
            return
        with self._lock:
            self._pending_created.append((ticket_id, title))
            if self._timer is None:
                self._timer = threading.Timer(self.batch_window_seconds, self._flush_created)
                self._timer.daemon = True
                self._timer.start()

    def close(self) -> None:
        with self._lock:
            timer = self._timer
        if timer is not None:
            timer.join(timeout=self.batch_window_seconds + 0.5)
        self._flush_created()

    def _flush_created(self) -> None:
        with self._lock:
            pending = self._pending_created
            self._pending_created = []
            self._timer = None
        if not pending:
            return
        if len(pending) == 1:
            payload = f"New ticket for you: {pending[0][0]} -- {pending[0][1]}"
        else:
            payload = "New tickets for you: " + "; ".join(f"{ticket_id} -- {title}" for ticket_id, title in pending)
        try:
            self.sender(payload)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Failed to send director ticket notification")


class TicketBoardEventHub:
    #: Consecutive failed scans before an open board is told its live updates
    #: are unreliable. One transient error is not worth a banner; a reconciler
    #: that keeps failing is, because the alternative is a board that looks
    #: current and is not.
    DEGRADED_AFTER_FAILURES = 3

    #: What a browser is told. A database error's text routinely carries the
    #: connection string, host, user and database name, and this frame is sent
    #: to every open client. The raw exception is logged server-side, where the
    #: operator who may already see the conninfo is; what crosses the wire is
    #: this fixed string and nothing derived from the failure.
    DEGRADED_REASON = "the board service cannot read its own state"

    def __init__(self, app: TicketBoardApp, scan_interval_seconds: float = 1.0) -> None:
        self.app = app
        self.scan_interval_seconds = scan_interval_seconds
        self._listeners: set[queue.Queue[int]] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._version = 0
        self._consecutive_failures = 0
        self.degraded_reason = ""
        try:
            self._signature = self.app.store_signature()
        except Exception:  # noqa: BLE001 - a board that cannot read must still serve
            LOGGER.warning("board reconciliation could not read its initial state", exc_info=True)
            self._signature = ()
            self.degraded_reason = self.DEGRADED_REASON
        self._thread = threading.Thread(target=self._watch_loop, name="ticket-board-events", daemon=True)
        self._thread.start()

    def register(self) -> tuple[queue.Queue[int], int]:
        listener: queue.Queue[int] = queue.Queue(maxsize=1)
        with self._lock:
            self._listeners.add(listener)
            version = self._version
        return listener, version

    def unregister(self, listener: queue.Queue[int]) -> None:
        with self._lock:
            self._listeners.discard(listener)

    def notify_change(self, signature: tuple[tuple[object, ...], ...] | None = None) -> int:
        with self._lock:
            if signature is not None:
                self._signature = signature
            self._version += 1
            version = self._version
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                while True:
                    listener.get_nowait()
            except queue.Empty:
                pass
            try:
                listener.put_nowait(version)
            except queue.Full:
                pass
        return version

    def close(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=self.scan_interval_seconds * 2.0 + 0.5)

    def _watch_loop(self) -> None:
        while not self._stop_event.wait(self.scan_interval_seconds):
            try:
                signature = self.app.store_signature()
            except Exception as exc:  # noqa: BLE001 - one bad scan must not end the loop
                # Before this, a single raised scan killed the thread for the
                # life of the process: pushes kept working, reconciliation
                # silently did not, and nothing said so.
                self._record_scan_failure(exc)
                continue
            self._record_scan_success()
            if signature != self._signature:
                self._signature = signature
                self.notify_change()

    def _record_scan_failure(self, exc: BaseException) -> None:
        with self._lock:
            self._consecutive_failures += 1
            crossed = (
                self._consecutive_failures >= self.DEGRADED_AFTER_FAILURES
                and not self.degraded_reason
            )
            if crossed:
                self.degraded_reason = self.DEGRADED_REASON
        # Raw here, where the operator is; never in the frame the browser gets.
        LOGGER.warning("board reconciliation scan failed: %s", exc)
        if crossed:
            # Wake every open client so it learns the board may be stale.
            self.notify_change()

    def _record_scan_success(self) -> None:
        with self._lock:
            recovered = bool(self.degraded_reason)
            self._consecutive_failures = 0
            self.degraded_reason = ""
        if recovered:
            self.notify_change()
