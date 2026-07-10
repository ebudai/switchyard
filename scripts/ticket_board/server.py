"""HTTP serving for the ticket-board tool."""

from __future__ import annotations

import json
import logging
import mimetypes
import queue
import subprocess
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .app import TicketBoardApp
from .frontend import HTML
try:
    from ticket_store_io import _ticket_number
except ModuleNotFoundError:
    from scripts.ticket_store_io import _ticket_number

LOGGER = logging.getLogger(__name__)
DIRECTOR_TARGET = "pgu-director:0.0"
DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS = 0.35
REPO_ROOT = Path(__file__).resolve().parents[2]


def send_director_message(payload: str, target: str = DIRECTOR_TARGET) -> None:
    subprocess.run(
        [str(REPO_ROOT / "scripts" / "directorctl"), "send", target, payload],
        check=True,
        capture_output=True,
        text=True,
    )


class DirectorNotifier:
    def __init__(
        self,
        *,
        sender: Callable[[str], None] | None = None,
        batch_window_seconds: float = DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS,
    ) -> None:
        self.sender = sender or send_director_message
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
    def __init__(self, app: TicketBoardApp, scan_interval_seconds: float = 1.0) -> None:
        self.app = app
        self.scan_interval_seconds = scan_interval_seconds
        self._listeners: set[queue.Queue[int]] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._version = 0
        self._signature = self.app.store_signature()
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

    def notify_change(self, signature: tuple[tuple[str, int, int], ...] | None = None) -> int:
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
            signature = self.app.store_signature()
            if signature != self._signature:
                self._signature = signature
                self.notify_change()


class TicketBoardHandler(BaseHTTPRequestHandler):
    server_version = "PGUTicketBoard/0.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def app(self) -> TicketBoardApp:
        return self.server.app  # type: ignore[attr-defined]

    @property
    def events(self) -> TicketBoardEventHub:
        return self.server.events  # type: ignore[attr-defined]

    @property
    def director_notifier(self) -> DirectorNotifier:
        return self.server.director_notifier  # type: ignore[attr-defined]

    def send_no_cache_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, payload: str, status: HTTPStatus) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def verify_created_ticket_persisted(
        self,
        created: dict[str, object],
        before_signature: tuple[tuple[str, int, int], ...],
    ) -> tuple[tuple[str, int, int], ...]:
        ticket_id = str(created.get("id", "")).strip()
        title = str(created.get("title", "")).strip()
        body = str(created.get("body", ""))
        if not ticket_id or not title:
            raise ValueError("created ticket missing id/title")

        before_names = {name for name, _, _ in before_signature}
        before_max = max((_ticket_number(Path(name).stem) for name in before_names), default=0)
        created_number = _ticket_number(ticket_id)
        if created_number <= before_max:
            raise ValueError(f"create returned non-new ticket id: {ticket_id}")
        if f"{ticket_id}.json" in before_names:
            raise ValueError(f"create collided with existing ticket id: {ticket_id}")

        persisted_path = self.app.store_dir / f"{ticket_id}.json"
        if not persisted_path.is_file():
            raise ValueError(f"created ticket was not persisted: {ticket_id}")
        persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
        persisted_id = str(persisted.get("id", persisted_path.stem)).strip()
        if persisted_id != ticket_id:
            raise ValueError(f"created ticket id mismatch on disk: {persisted_id} != {ticket_id}")
        if str(persisted.get("title", "")).strip() != title:
            raise ValueError(f"created ticket title mismatch on disk: {ticket_id}")
        if str(persisted.get("body", "")) != body:
            raise ValueError(f"created ticket body mismatch on disk: {ticket_id}")

        after_signature = self.app.store_signature()
        if after_signature == before_signature:
            raise ValueError(f"ticket create did not change the store: {ticket_id}")
        return after_signature

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_no_cache_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/board":
            self.send_json(self.app.snapshot())
            return
        if parsed.path == "/events":
            self.serve_events()
            return
        if parsed.path.startswith("/api/image/"):
            raw = urllib.parse.unquote(parsed.path.removeprefix("/api/image/"))
            try:
                path = self.app.resolve_image(raw)
            except FileNotFoundError as exc:
                self.send_text(str(exc), HTTPStatus.NOT_FOUND)
                return
            body = path.read_bytes()
            content_type, _ = mimetypes.guess_type(path.name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_text("Not found", HTTPStatus.NOT_FOUND)

    def serve_events(self) -> None:
        listener, version = self.events.register()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_no_cache_headers()
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(f"event: board\ndata: {json.dumps({'version': version})}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    next_version = listener.get(timeout=15.0)
                    self.wfile.write(f"event: board\ndata: {json.dumps({'version': next_version})}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.events.unregister(listener)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if parsed.path == "/api/upload":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    raise ValueError("upload requires an image/* Content-Type")
                uploaded = self.app.save_uploaded_image(self.rfile.read(length))
                self.send_json({"image": uploaded}, HTTPStatus.CREATED)
                return
            payload = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/tickets":
                before_signature = self.app.store_signature()
                created = self.app.create_ticket(
                    title=str(payload.get("title", "")),
                    body=str(payload.get("body", "")),
                    screenshot=payload.get("screenshot"),
                    screenshots=payload.get("screenshots"),
                    assignee=str(payload.get("assignee", "unassigned")),
                    needs_eric_signoff=bool(payload.get("needs_eric_signoff", False)),
                    blocked_by=payload.get("blocked_by"),
                    blocked_reason=str(payload.get("blocked_reason", "")),
                )
                after_signature = self.verify_created_ticket_persisted(created, before_signature)
                self.events.notify_change(after_signature)
                self.director_notifier.notify_ticket_created(created)
                self.send_json({"ticket": created}, HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/tickets/") and parsed.path.endswith("/merge"):
                source_ticket_id = parsed.path.removeprefix("/api/tickets/").removesuffix("/merge").rstrip("/")
                merged = self.app.merge_tickets(
                    source_ticket_id,
                    str(payload.get("target_id", "")),
                    actor=str(payload.get("actor", "")),
                )
                self.events.notify_change(self.app.store_signature())
                self.send_json(merged)
                return
            if parsed.path.startswith("/api/tickets/"):
                ticket_id = parsed.path.removeprefix("/api/tickets/")
                updated = self.app.update_ticket(ticket_id, payload)
                self.events.notify_change(self.app.store_signature())
                self.send_json({"ticket": updated})
                return
        except FileNotFoundError as exc:
            self.send_text(str(exc), HTTPStatus.NOT_FOUND)
            return
        except Exception as exc:  # noqa: BLE001
            self.send_text(str(exc), HTTPStatus.BAD_REQUEST)
            return
        self.send_text("Not found", HTTPStatus.NOT_FOUND)


class TicketBoardServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        app: TicketBoardApp,
        director_notifier: DirectorNotifier | None = None,
    ) -> None:
        self.app = app
        self.events = TicketBoardEventHub(app)
        self.director_notifier = director_notifier or DirectorNotifier()
        super().__init__(address, TicketBoardHandler)

    def server_close(self) -> None:
        self.events.close()
        self.director_notifier.close()
        super().server_close()
