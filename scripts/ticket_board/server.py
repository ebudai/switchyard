"""HTTP serving for the ticket-board tool."""

from __future__ import annotations

import json
import mimetypes
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import TicketBoardApp
from .frontend import HTML


class TicketBoardHandler(BaseHTTPRequestHandler):
    server_version = "PGUTicketBoard/0.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def app(self) -> TicketBoardApp:
        return self.server.app  # type: ignore[attr-defined]

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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/board":
            self.send_json(self.app.snapshot())
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

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if parsed.path == "/api/tickets":
                created = self.app.create_ticket(
                    title=str(payload.get("title", "")),
                    body=str(payload.get("body", "")),
                    screenshot=payload.get("screenshot"),
                    assignee=str(payload.get("assignee", "unassigned")),
                    needs_eric_signoff=bool(payload.get("needs_eric_signoff", False)),
                )
                self.send_json({"ticket": created}, HTTPStatus.CREATED)
                return
            if parsed.path.startswith("/api/tickets/"):
                ticket_id = parsed.path.removeprefix("/api/tickets/")
                updated = self.app.update_ticket(ticket_id, payload)
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
    def __init__(self, address: tuple[str, int], app: TicketBoardApp) -> None:
        self.app = app
        super().__init__(address, TicketBoardHandler)
