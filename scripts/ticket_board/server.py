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

from .app import TicketBoardApp, _ticket_number, iso_now
from .frontend import HTML

LOGGER = logging.getLogger(__name__)
DIRECTOR_TARGET = "pgu-director:0.0"
DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS = 0.35
REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER_ROLE_HEADER = "X-PGU-Caller-Role"
IMPLEMENTER_ROLES = {"main", "app", "ops", "perf", "research"}
CALLER_ROLES = IMPLEMENTER_ROLES | {"director", "eric", "audit"}
OPERATION_ALLOWED_ROLES = {
    "create_ticket": {"director", "eric"},
    "file_bug": IMPLEMENTER_ROLES,
    "route": {"director"},
    "start_work": IMPLEMENTER_ROLES,
    "submit_to_audit": IMPLEMENTER_ROLES,
    "audit_sign_off": {"audit"},
    "audit_kick_back": {"audit"},
    "eric_sign_off": {"eric"},
    "eric_reopen": {"eric"},
    "mark_done": {"director"},
    "defer": {"director"},
    "cancel": {"director"},
    "set_manually_controlled": {"director"},
    "set_blockers": {"director"},
    "add_comment": CALLER_ROLES,
    "edit_fields": CALLER_ROLES,
    "merge": {"director"},
}
EDIT_FIELD_NAMES = {
    "title",
    "parent_id",
    "screenshots",
    "screenshot",
    "implementation",
    "audit_prompt",
    "needs_eric_signoff",
    "commit_exempt",
    "commit_hash",
    "blocked_reason",
    "audit_signoff",
    "eric_signoff",
}


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
        before_signature: tuple[tuple[object, ...], ...],
    ) -> tuple[tuple[object, ...], ...]:
        return self.app.verify_created_ticket_persisted(created, before_signature)

    def caller_role(self) -> str:
        raw = self.headers.get(CALLER_ROLE_HEADER, "")
        role = raw.strip().lower()
        if not role:
            raise ValueError(f"missing {CALLER_ROLE_HEADER}")
        if role not in CALLER_ROLES:
            raise ValueError(f"invalid caller role: {raw}")
        return role

    def require_operation_allowed(self, operation: str, caller_role: str, ticket_id: str | None = None) -> None:
        allowed = OPERATION_ALLOWED_ROLES.get(operation)
        if allowed is None:
            raise ValueError(f"unknown ticket operation: {operation}")
        if caller_role not in allowed:
            raise PermissionError(f"{caller_role} cannot call {operation}")
        if operation in {"start_work", "submit_to_audit"} and ticket_id is not None:
            ticket = self.app.get_ticket(ticket_id)
            if str(ticket.get("assignee", "")).strip().lower() != caller_role:
                raise PermissionError(f"{caller_role} cannot call {operation} for ticket assigned to {ticket.get('assignee')}")

    def create_ticket_from_payload(self, payload: dict[str, object], caller_role: str | None = None) -> dict[str, object]:
        state = str(payload.get("state", "analysis")).strip() or "analysis"
        implementation = str(payload.get("implementation", ""))
        audit_prompt = str(payload.get("audit_prompt", ""))
        parent_id = str(payload.get("parent_id", "")).strip().upper()
        commit_hash = str(payload.get("commit_hash", ""))
        commit_exempt = bool(payload.get("commit_exempt", False))
        comment_text = str(payload.get("comment_text", "")).strip()
        if not comment_text and isinstance(payload.get("comment"), dict):
            comment = payload["comment"]  # type: ignore[assignment]
            comment_text = str(comment.get("text", "")).strip()  # type: ignore[union-attr]
        advanced_create = any(
            (
                state != "analysis",
                implementation,
                audit_prompt,
                parent_id,
                commit_hash,
                commit_exempt,
                comment_text,
                bool(payload.get("audit_signoff", False)),
                bool(payload.get("eric_signoff", False)),
            )
        )
        if advanced_create and caller_role is None:
            raise ValueError("advanced create fields require /api/tickets/actions/create_ticket")
        if not advanced_create:
            return self.app.create_ticket(
                title=str(payload.get("title", "")),
                body=str(payload.get("body", "")),
                screenshot=payload.get("screenshot"),  # type: ignore[arg-type]
                screenshots=payload.get("screenshots"),  # type: ignore[arg-type]
                assignee=str(payload.get("assignee", "unassigned")),
                needs_eric_signoff=bool(payload.get("needs_eric_signoff", False)),
                blocked_by=payload.get("blocked_by"),  # type: ignore[arg-type]
                blocked_reason=str(payload.get("blocked_reason", "")),
            )

        created = iso_now()
        comments = []
        if comment_text:
            comments.append({"who": caller_role, "text": comment_text, "ts": created})
        return self.app.create_ticket_record(
            title=str(payload.get("title", "")),
            body=str(payload.get("body", "")),
            screenshot=payload.get("screenshot"),  # type: ignore[arg-type]
            screenshots=payload.get("screenshots"),  # type: ignore[arg-type]
            assignee=str(payload.get("assignee", "unassigned")),
            state=state,
            blocked_by=payload.get("blocked_by"),  # type: ignore[arg-type]
            parent_id=parent_id,
            implementation=implementation,
            audit_prompt=audit_prompt,
            audit_signoff=bool(payload.get("audit_signoff", False)),
            needs_eric_signoff=bool(payload.get("needs_eric_signoff", False)),
            eric_signoff=bool(payload.get("eric_signoff", False)),
            comments=comments,
            blocked_reason=str(payload.get("blocked_reason", "")),
            commit_hash=commit_hash,
            commit_exempt=commit_exempt,
            created=created,
            updated=created,
        )

    def send_ticket_created(self, created: dict[str, object], before_signature: tuple[tuple[object, ...], ...]) -> None:
        after_signature = self.verify_created_ticket_persisted(created, before_signature)
        self.events.notify_change(after_signature)
        self.director_notifier.notify_ticket_created(created)
        self.send_json({"ticket": created}, HTTPStatus.CREATED)

    def handle_ticket_action(self, operation: str, payload: dict[str, object], ticket_id: str | None = None) -> None:
        caller = self.caller_role()
        self.require_operation_allowed(operation, caller, ticket_id)

        if operation == "create_ticket":
            before_signature = self.app.store_signature()
            created = self.create_ticket_from_payload(payload, caller)
            self.send_ticket_created(created, before_signature)
            return

        if operation == "file_bug":
            before_signature = self.app.store_signature()
            source_ticket_id = str(payload.get("source_ticket_id", payload.get("parent_id", ""))).strip().upper()
            created = self.app.create_ticket_record(
                title=str(payload.get("title", "")),
                body=str(payload.get("body", "")),
                screenshot=payload.get("screenshot"),  # type: ignore[arg-type]
                screenshots=payload.get("screenshots"),  # type: ignore[arg-type]
                assignee=str(payload.get("assignee", "unassigned")),
                state="analysis",
                blocked_by=payload.get("blocked_by"),  # type: ignore[arg-type]
                implementation="",
                audit_prompt="",
                audit_signoff=False,
                needs_eric_signoff=bool(payload.get("needs_eric_signoff", False)),
                eric_signoff=False,
                comments=[],
                parent_id=source_ticket_id,
                blocked_reason=str(payload.get("blocked_reason", "")),
            )
            self.send_ticket_created(created, before_signature)
            return

        if ticket_id is None:
            raise ValueError(f"{operation} requires a ticket id")

        patch: dict[str, object]
        if operation == "route":
            updated = self.app.route_ticket(
                ticket_id,
                str(payload.get("state", payload.get("new_state", ""))),
                str(payload.get("assignee", "")),
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "merge":
            merged = self.app.merge_tickets(
                ticket_id,
                str(payload.get("target_id", "")),
                actor=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json(merged)
            return
        elif operation == "start_work":
            patch = {"state": "in_progress"}
        elif operation == "submit_to_audit":
            patch = {"state": "audit", "commit_hash": str(payload.get("commit_hash", ""))}
        elif operation == "audit_sign_off":
            patch = {"audit_signoff": True}
        elif operation == "audit_kick_back":
            patch = {
                "state": "analysis",
                "comment": {"who": caller, "text": str(payload.get("reason", payload.get("text", "")))},
            }
        elif operation == "eric_sign_off":
            patch = {"eric_signoff": True}
        elif operation == "eric_reopen":
            patch = {
                "state": "analysis",
                "comment": {"who": caller, "text": str(payload.get("reason", payload.get("text", "")))},
            }
        elif operation == "mark_done":
            patch = {"state": "done", "commit_hash": str(payload.get("commit_hash", ""))}
        elif operation == "defer":
            patch = {"state": "backlog"}
        elif operation == "cancel":
            patch = {
                "state": "cancelled",
                "comment": {"who": caller, "text": str(payload.get("reason", payload.get("text", "")))},
            }
        elif operation == "set_manually_controlled":
            patch = {"manually_controlled": bool(payload.get("manually_controlled", payload.get("value", False)))}
        elif operation == "set_blockers":
            patch = {
                "blocked_by": payload.get("blocked_by", payload.get("ids", [])),
                "blocked_reason": str(payload.get("blocked_reason", payload.get("reason", ""))),
            }
        elif operation == "add_comment":
            patch = {"comment": {"who": caller, "text": str(payload.get("text", ""))}}
        elif operation == "edit_fields":
            invalid_fields = sorted(set(payload) - EDIT_FIELD_NAMES)
            if invalid_fields:
                raise ValueError(f"edit_fields cannot update: {', '.join(invalid_fields)}")
            if payload.get("audit_signoff") is True:
                raise ValueError("audit_signoff=true requires audit_sign_off")
            if payload.get("eric_signoff") is True:
                raise ValueError("eric_signoff=true requires eric_sign_off")
            patch = dict(payload)
        else:
            raise ValueError(f"unknown ticket operation: {operation}")

        updated = self.app.update_ticket(ticket_id, patch)
        self.events.notify_change(self.app.store_signature())
        self.send_json({"ticket": updated})

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
            if parsed.path.startswith("/api/tickets/actions/"):
                operation = urllib.parse.unquote(parsed.path.removeprefix("/api/tickets/actions/").strip("/"))
                self.handle_ticket_action(operation, payload)
                return
            if parsed.path.startswith("/api/tickets/") and "/actions/" in parsed.path:
                rest = parsed.path.removeprefix("/api/tickets/")
                raw_ticket_id, raw_operation = rest.split("/actions/", 1)
                ticket_id = urllib.parse.unquote(raw_ticket_id.strip("/"))
                operation = urllib.parse.unquote(raw_operation.strip("/"))
                self.handle_ticket_action(operation, payload, ticket_id=ticket_id)
                return
        except FileNotFoundError as exc:
            self.send_text(str(exc), HTTPStatus.NOT_FOUND)
            return
        except PermissionError as exc:
            self.send_text(str(exc), HTTPStatus.FORBIDDEN)
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
