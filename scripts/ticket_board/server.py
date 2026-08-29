"""HTTP serving for the ticket-board tool."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import threading
import time
import urllib.parse
from email.utils import formatdate, parsedate_to_datetime
from hashlib import sha256
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping

from PIL import Image

from .app import CALLER_ROLES as APP_CALLER_ROLES
from .app import TicketBoardApp, iso_now
from .frontend import HTML

LOGGER = logging.getLogger(__name__)
DIRECTOR_TARGET = "pgu-director:0.0"
DEFAULT_DIRECTORCTL = "/home/agent/bin/directorctl"
DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS = 0.35
REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER_ROLE_HEADER = "X-Ticket-Board-Caller-Role"
WRITE_TOKEN_HEADER = "X-Ticket-Board-Write-Token"
REPORT_TOKEN_HEADER = "X-Ticket-Board-Report-Token"
LEGACY_CALLER_ROLE_HEADER = "X-PGU-Caller-Role"
LEGACY_WRITE_TOKEN_HEADER = "X-PGU-Write-Token"
SO_PEERCRED_FORMAT = "3i"
PANE_SOCKET_MODE = 0o666
IMAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
THUMBNAIL_MAX_SIZE = 512
THUMBNAIL_QUALITY = 80
CALLER_ROLES = set(APP_CALLER_ROLES)
DEFAULT_IMPLEMENTER_ROLES = ("main", "app", "ops", "perf", "research")


def _role_set_from_env(name: str, default: tuple[str, ...]) -> set[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return set(default)
    roles = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return roles or set(default)


IMPLEMENTER_ROLES = _role_set_from_env("TICKET_BOARD_IMPLEMENTER_ROLES", DEFAULT_IMPLEMENTER_ROLES)
DRAFT_ROLES = _role_set_from_env("TICKET_BOARD_DRAFT_ROLES", ())
TASK_ROLES = IMPLEMENTER_ROLES | {"director", "audit", "inspector"}
OPERATION_ALLOWED_ROLES = {
    "create_ticket": {"director", "user"},
    "file_bug": IMPLEMENTER_ROLES | {"audit"},
    "release_draft": DRAFT_ROLES | {"director", "user"},
    "route": {"director"},
    "force_move": {"director"},
    "override_move": {"director"},
    "start_work": IMPLEMENTER_ROLES,
    "submit_to_inspection": IMPLEMENTER_ROLES,
    "submit_to_audit": IMPLEMENTER_ROLES,
    "implementer_kick_back": IMPLEMENTER_ROLES,
    "request_commit_exempt": IMPLEMENTER_ROLES,
    "start_task": TASK_ROLES,
    "complete_task": TASK_ROLES,
    "await_role": CALLER_ROLES,
    "clear_awaiting_role": CALLER_ROLES,
    "inspector_sign_off": {"inspector"},
    "inspector_kick_back": {"inspector"},
    "audit_sign_off": {"audit"},
    "audit_kick_back": {"audit"},
    "director_dat_sign_off": {"director"},
    "director_dat_kick_back": {"director"},
    "user_sign_off": {"user"},
    "user_reopen": {"user"},
    "mark_done": {"director"},
    "defer": {"director"},
    "cancel": {"director"},
    "set_manually_controlled": {"director"},
    "set_blockers": {"director"},
    "add_comment": CALLER_ROLES,
    "edit_fields": CALLER_ROLES,
    "crop_attachment": {"director", "user"},
    "merge": {"director"},
}
EDIT_FIELD_NAMES = {
    "title",
    "body",
    "parent_id",
    "screenshots",
    "screenshot",
    "implementation",
    "audit_prompt",
    "needs_audit",
    "needs_inspection",
    "needs_user_signoff",
    "commit_exempt",
    "regression",
    "commit_hash",
    "audit_signoff",
    "inspector_signoff",
    "user_signoff",
}

RELEASE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def build_id_from_release_path(module_path: Path) -> str:
    parts = module_path.resolve().parts
    for index, part in enumerate(parts[:-1]):
        if part != "releases":
            continue
        candidate = parts[index + 1].strip()
        if RELEASE_SHA_RE.fullmatch(candidate):
            return candidate.lower()
    return ""


def build_id_from_file(module_path: Path) -> str:
    for parent in module_path.resolve().parents:
        for name in ("BUILD", "BUILD_ID", "VERSION", ".build-id"):
            candidate_path = parent / name
            if not candidate_path.is_file():
                continue
            try:
                lines = candidate_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            except OSError:
                continue
            candidate = lines[0].strip() if lines else ""
            if candidate:
                return candidate
    return ""


def board_build_id(
    *,
    environ: Mapping[str, str] = os.environ,
    repo_root: Path = REPO_ROOT,
    module_path: Path = Path(__file__),
) -> str:
    explicit = environ.get("PGU_TICKET_BOARD_BUILD_ID", "").strip()
    if explicit:
        return explicit
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = proc.stdout.strip()
    if proc.returncode == 0 and value:
        return value
    release_id = build_id_from_release_path(module_path)
    if release_id:
        return release_id
    file_id = build_id_from_file(module_path)
    return file_id or "unknown"


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int


@dataclass
class CallerRegistration:
    role: str
    uid: int
    gid: int
    connection_count: int = 0


class CallerRegistry:
    def __init__(self, *, pid_alive: Callable[[int], bool] | None = None) -> None:
        self._pid_alive = pid_alive or self._default_pid_alive
        self._lock = threading.Lock()
        self._by_pid: dict[int, CallerRegistration] = {}
        self._role_to_pid: dict[str, int] = {}

    @staticmethod
    def _default_pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def register(self, credentials: PeerCredentials, role: str) -> None:
        normalized_role = role.strip().lower()
        if normalized_role not in CALLER_ROLES - {"user"}:
            raise ValueError(f"invalid local caller role: {role}")
        with self._lock:
            self._drop_stale_locked()
            existing_pid = self._role_to_pid.get(normalized_role)
            if existing_pid is not None and existing_pid != credentials.pid:
                LOGGER.warning(
                    "Rejected caller registration for role %s from pid=%s uid=%s gid=%s; already held by live pid=%s",
                    normalized_role,
                    credentials.pid,
                    credentials.uid,
                    credentials.gid,
                    existing_pid,
                )
                raise PermissionError(f"role {normalized_role} is already registered by live pid {existing_pid}")
            existing = self._by_pid.get(credentials.pid)
            if existing is not None and existing.role != normalized_role:
                LOGGER.warning(
                    "Rejected caller registration for pid=%s as %s; pid already registered as %s",
                    credentials.pid,
                    normalized_role,
                    existing.role,
                )
                raise PermissionError(f"pid {credentials.pid} is already registered as {existing.role}")
            if existing is None:
                self._by_pid[credentials.pid] = CallerRegistration(
                    role=normalized_role,
                    uid=credentials.uid,
                    gid=credentials.gid,
                    connection_count=1,
                )
                self._role_to_pid[normalized_role] = credentials.pid
            else:
                existing.connection_count += 1
            LOGGER.info(
                "Registered local caller role %s for pid=%s uid=%s gid=%s",
                normalized_role,
                credentials.pid,
                credentials.uid,
                credentials.gid,
            )

    def release(self, pid: int) -> None:
        with self._lock:
            registration = self._by_pid.get(pid)
            if registration is None:
                return
            registration.connection_count -= 1
            if registration.connection_count > 0:
                return
            self._by_pid.pop(pid, None)
            if self._role_to_pid.get(registration.role) == pid:
                self._role_to_pid.pop(registration.role, None)
            LOGGER.info("Released local caller role %s for pid=%s", registration.role, pid)

    def role_for_pid(self, pid: int) -> str:
        with self._lock:
            registration = self._by_pid.get(pid)
            if registration is None:
                raise ValueError(f"pid {pid} has not registered a caller role")
            if not self._pid_alive(pid):
                self._release_locked(pid)
                raise ValueError(f"pid {pid} registration is stale")
            return registration.role

    def pid_for_role(self, role: str) -> int | None:
        with self._lock:
            self._drop_stale_locked()
            return self._role_to_pid.get(role)

    def _release_locked(self, pid: int) -> None:
        registration = self._by_pid.pop(pid, None)
        if registration is not None and self._role_to_pid.get(registration.role) == pid:
            self._role_to_pid.pop(registration.role, None)

    def _drop_stale_locked(self) -> None:
        for pid in list(self._by_pid):
            if not self._pid_alive(pid):
                self._release_locked(pid)


def peer_credentials(connection: socket.socket) -> PeerCredentials:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(SO_PEERCRED_FORMAT))
    pid, uid, gid = struct.unpack(SO_PEERCRED_FORMAT, raw)
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def send_director_message(payload: str, target: str = DIRECTOR_TARGET) -> None:
    subprocess.run(
        [DEFAULT_DIRECTORCTL, "send", target, payload],
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
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self._local_peer_credentials: PeerCredentials | None = None
        self._registered_local_pid: int | None = None
        if self.caller_registry is not None:
            self._local_peer_credentials = peer_credentials(self.connection)  # type: ignore[arg-type]

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            if self._registered_local_pid is not None and self.caller_registry is not None:
                self.caller_registry.release(self._registered_local_pid)

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

    @property
    def caller_registry(self) -> CallerRegistry | None:
        return getattr(self.server, "caller_registry", None)

    @property
    def write_token(self) -> str:
        return self.server.write_token  # type: ignore[attr-defined]

    @property
    def report_token(self) -> str:
        return getattr(self.server, "report_token", "")

    def send_no_cache_headers(self) -> None:
        self.send_header("Cache-Control", "no-cache")

    def cache_headers_for_file(self, path: Path, *, variant: str = "original") -> tuple[str, str]:
        stat = path.stat()
        etag_seed = f"{variant}\0{path.resolve()}\0{stat.st_mtime_ns}\0{stat.st_size}".encode("utf-8")
        etag = f'"{sha256(etag_seed).hexdigest()}"'
        last_modified = formatdate(stat.st_mtime, usegmt=True)
        return etag, last_modified

    def request_cache_matches(self, etag: str, last_modified: str) -> bool:
        if_none_match = self.headers.get("If-None-Match", "")
        if if_none_match:
            requested_etags = {item.strip() for item in if_none_match.split(",")}
            if "*" in requested_etags or etag in requested_etags:
                return True
        if_modified_since = self.headers.get("If-Modified-Since")
        if if_modified_since:
            try:
                requested_date = parsedate_to_datetime(if_modified_since)
                current_date = parsedate_to_datetime(last_modified)
            except (TypeError, ValueError):
                return False
            return requested_date >= current_date
        return False

    def send_cached_bytes(self, body: bytes, *, content_type: str, source_path: Path, variant: str = "original") -> None:
        etag, last_modified = self.cache_headers_for_file(source_path, variant=variant)
        if self.request_cache_matches(etag, last_modified):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("Cache-Control", IMAGE_CACHE_CONTROL)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", IMAGE_CACHE_CONTROL)
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_modified)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def thumbnail_cache_path(self, source_path: Path, size: int) -> Path:
        stat = source_path.stat()
        cache_key = sha256(f"{source_path.resolve()}\0{size}\0{stat.st_mtime_ns}\0{stat.st_size}".encode("utf-8")).hexdigest()
        return self.app.asset_dir / ".thumb-cache" / f"{cache_key}.jpg"

    def thumbnail_bytes_for(self, source_path: Path, size: int = THUMBNAIL_MAX_SIZE) -> bytes:
        size = max(64, min(size, THUMBNAIL_MAX_SIZE))
        cache_path = self.thumbnail_cache_path(source_path, size)
        if cache_path.is_file():
            return cache_path.read_bytes()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with Image.open(source_path) as image:
            image.load()
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode in ("RGBA", "LA"):
                    background.paste(image, mask=image.getchannel("A"))
                    output = background
                else:
                    output = image.convert("RGB")
            else:
                output = image.convert("RGB") if image.mode == "L" else image
            output.save(tmp_path, format="JPEG", quality=THUMBNAIL_QUALITY, optimize=True)
        tmp_path.replace(cache_path)
        return cache_path.read_bytes()

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_no_cache_headers()
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
        if self.caller_registry is not None:
            if self._local_peer_credentials is None:
                raise ValueError("local socket request missing peer credentials")
            return self.caller_registry.role_for_pid(self._local_peer_credentials.pid)
        # Keep the legacy name while clients transition. A server-side alias only
        # protects old clients on a new board; clients must dual-send to support
        # new tooling talking to an older deployed board.
        raw = self.headers.get(CALLER_ROLE_HEADER, "") or self.headers.get(LEGACY_CALLER_ROLE_HEADER, "")
        role = raw.strip().lower()
        if not role:
            raise ValueError(f"missing {CALLER_ROLE_HEADER}")
        if role not in CALLER_ROLES:
            raise ValueError(f"invalid caller role: {raw}")
        return role

    def require_http_write_token(self) -> None:
        if self.caller_registry is not None:
            return
        raw = self.headers.get(WRITE_TOKEN_HEADER, "") or self.headers.get(LEGACY_WRITE_TOKEN_HEADER, "")
        if not raw or not secrets.compare_digest(raw, self.write_token):
            raise PermissionError(f"missing or invalid {WRITE_TOKEN_HEADER}")

    def require_http_report_token(self) -> None:
        if self.caller_registry is not None:
            raise PermissionError("tenant report filing is only available over HTTP")
        if not self.report_token:
            raise PermissionError(f"{REPORT_TOKEN_HEADER} is not configured")
        raw = self.headers.get(REPORT_TOKEN_HEADER, "")
        if not raw or not secrets.compare_digest(raw, self.report_token):
            raise PermissionError(f"missing or invalid {REPORT_TOKEN_HEADER}")

    def handle_register_caller(self, payload: dict[str, object]) -> None:
        if self.caller_registry is None or self._local_peer_credentials is None:
            raise ValueError("caller registration is only available on the local Unix socket")
        role = str(payload.get("role", "")).strip().lower()
        if self._registered_local_pid is not None:
            current_role = self.caller_registry.role_for_pid(self._registered_local_pid)
            if role != current_role:
                raise PermissionError(f"connection is already registered as {current_role}")
            self.send_json({"role": current_role, "pid": self._registered_local_pid})
            return
        self.caller_registry.register(self._local_peer_credentials, role)
        self._registered_local_pid = self._local_peer_credentials.pid
        self.send_json({"role": role, "pid": self._local_peer_credentials.pid})

    def require_operation_allowed(self, operation: str, caller_role: str, ticket_id: str | None = None) -> None:
        allowed = OPERATION_ALLOWED_ROLES.get(operation)
        if allowed is None:
            raise ValueError(f"unknown ticket operation: {operation}")
        if caller_role not in allowed:
            raise PermissionError(f"{caller_role} cannot call {operation}")
        if operation in {
            "start_work",
            "submit_to_inspection",
            "submit_to_audit",
            "implementer_kick_back",
            "request_commit_exempt",
            "start_task",
            "complete_task",
        } and ticket_id is not None:
            ticket = self.app.get_ticket(ticket_id)
            if caller_role != "director" and str(ticket.get("assignee", "")).strip().lower() != caller_role:
                raise PermissionError(f"{caller_role} cannot call {operation} for ticket assigned to {ticket.get('assignee')}")

    def create_ticket_from_payload(self, payload: dict[str, object], caller_role: str | None = None) -> dict[str, object]:
        state = str(payload.get("initial_state", payload.get("state", "analysis"))).strip() or "analysis"
        implementation = str(payload.get("implementation", ""))
        audit_prompt = str(payload.get("audit_prompt", ""))
        parent_id = str(payload.get("parent_id", "")).strip().upper()
        commit_hash = str(payload.get("commit_hash", ""))
        commit_exempt = bool(payload.get("commit_exempt", False))
        regression = bool(payload.get("regression", False))
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
                bool(payload.get("blocked_by")),
                bool(str(payload.get("blocked_reason", "")).strip()),
                commit_hash,
                commit_exempt,
                regression,
                comment_text,
                bool(payload.get("audit_signoff", False)),
                bool(payload.get("inspector_signoff", False)),
                bool(payload.get("needs_inspection", False)),
                bool(payload.get("user_signoff", False)),
            )
        )
        if advanced_create and caller_role is None:
            raise ValueError("advanced create fields require /api/tickets/actions/create_ticket")
        if bool(payload.get("needs_inspection", False)) and caller_role != "director":
            raise PermissionError("needs_inspection can only be set by director")
        if payload.get("needs_audit", True) is False and caller_role != "director":
            raise PermissionError("needs_audit can only be set to false by director")
        if bool(payload.get("commit_exempt", False)) and caller_role != "director":
            raise PermissionError("commit_exempt can only be set by director")
        if not advanced_create:
            return self.app.create_ticket(
                title=str(payload.get("title", "")),
                body=str(payload.get("body", "")),
                screenshot=payload.get("screenshot"),  # type: ignore[arg-type]
                screenshots=payload.get("screenshots"),  # type: ignore[arg-type]
                assignee=str(payload.get("assignee", "unassigned")),
                needs_user_signoff=bool(payload.get("needs_user_signoff", False)),
                needs_audit=bool(payload.get("needs_audit", True)),
                needs_inspection=bool(payload.get("needs_inspection", False)),
                regression=regression,
                blocked_by=payload.get("blocked_by"),  # type: ignore[arg-type]
                blocked_reason=str(payload.get("blocked_reason", "")),
                notification_source_role=self.notification_source_role("create_ticket", caller_role),
                caller_role=caller_role,
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
            needs_audit=bool(payload.get("needs_audit", True)),
            needs_inspection=bool(payload.get("needs_inspection", False)),
            inspector_signoff=bool(payload.get("inspector_signoff", False)),
            needs_user_signoff=bool(payload.get("needs_user_signoff", False)),
            user_signoff=bool(payload.get("user_signoff", False)),
            regression=regression,
            comments=comments,
            blocked_reason=str(payload.get("blocked_reason", "")),
            commit_hash=commit_hash,
            commit_exempt=commit_exempt,
            notification_source_role=self.notification_source_role("create_ticket", caller_role),
            caller_role=caller_role,
            created=created,
            updated=created,
        )

    def notification_source_role(self, operation: str, caller_role: str | None) -> str | None:
        if operation == "create_ticket" and caller_role == "director" and self.caller_registry is None:
            return "user"
        return None

    def action_comment_text(self, payload: dict[str, object]) -> str:
        return str(payload.get("recommendations", payload.get("reason", payload.get("text", "")))).strip()

    def send_ticket_created(
        self,
        created: dict[str, object],
        before_signature: tuple[tuple[object, ...], ...],
        *,
        notification_source_role: str | None = None,
    ) -> None:
        after_signature = self.verify_created_ticket_persisted(created, before_signature)
        self.events.notify_change(after_signature)
        if (
            notification_source_role != "director"
            and str(created.get("state", "")).strip() == "analysis"
            and not list(created.get("blocked_by") or [])
        ):
            self.director_notifier.notify_ticket_created(created)
        self.send_json({"ticket": created}, HTTPStatus.CREATED)

    def handle_file_report(self, payload: dict[str, object]) -> None:
        forbidden_fields = sorted(
            set(payload)
            & {
                "assignee",
                "state",
                "initial_state",
                "blocked_by",
                "blocked_reason",
                "parent_id",
                "source_ticket_id",
                "needs_audit",
                "needs_inspection",
                "needs_user_signoff",
                "audit_signoff",
                "inspector_signoff",
                "user_signoff",
                "commit_hash",
                "commit_exempt",
                "manually_controlled",
            }
        )
        if forbidden_fields:
            raise PermissionError(f"file_report cannot set: {', '.join(forbidden_fields)}")
        before_signature = self.app.store_signature()
        created = self.app.file_report(
            title=str(payload.get("title", "")),
            body=str(payload.get("body", "")),
            origin_project=str(payload.get("origin_project", "")),
            external_source_ref=str(payload.get("external_source_ref", payload.get("source_ref", ""))),
        )
        self.send_ticket_created(created, before_signature, notification_source_role="tenant_report")

    def handle_ticket_action(self, operation: str, payload: dict[str, object], ticket_id: str | None = None) -> None:
        caller = self.caller_role()
        self.require_operation_allowed(operation, caller, ticket_id)

        if operation == "create_ticket":
            before_signature = self.app.store_signature()
            created = self.create_ticket_from_payload(payload, caller)
            self.send_ticket_created(
                created,
                before_signature,
                notification_source_role=self.notification_source_role(operation, caller) or caller,
            )
            return

        if operation == "file_bug":
            if bool(payload.get("needs_inspection", False)) and caller != "director":
                raise PermissionError("needs_inspection can only be set by director")
            if payload.get("needs_audit", True) is False and caller != "director":
                raise PermissionError("needs_audit can only be set to false by director")
            if bool(payload.get("commit_exempt", False)) and caller != "director":
                raise PermissionError("commit_exempt can only be set by director")
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
                needs_audit=bool(payload.get("needs_audit", True)),
                needs_inspection=bool(payload.get("needs_inspection", False)),
                inspector_signoff=False,
                needs_user_signoff=bool(payload.get("needs_user_signoff", False)),
                user_signoff=False,
                regression=bool(payload.get("regression", False)),
                comments=[],
                parent_id=source_ticket_id,
                blocked_reason=str(payload.get("blocked_reason", "")),
                caller_role=caller,
            )
            self.send_ticket_created(created, before_signature, notification_source_role=caller)
            return

        if ticket_id is None:
            raise ValueError(f"{operation} requires a ticket id")

        patch: dict[str, object]
        if operation == "route":
            updated = self.app.route_ticket(
                ticket_id,
                str(payload.get("state", payload.get("new_state", ""))),
                str(payload.get("assignee", "")),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        if operation == "release_draft":
            updated = self.app.release_draft(ticket_id, caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation in {"force_move", "override_move"}:
            updated = self.app.force_move_ticket(
                ticket_id,
                str(payload.get("state", payload.get("new_state", ""))),
                str(payload.get("assignee", "")),
                suppress_notification=bool(payload.get("suppress_notification", payload.get("no_notify", False))),
                caller_role=caller,
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
        elif operation == "submit_to_inspection":
            patch = {"state": "inspection", "assignee": "inspector"}
        elif operation == "submit_to_audit":
            patch = {"state": "audit", "commit_hash": str(payload.get("commit_hash", ""))}
        elif operation == "implementer_kick_back":
            comment_text = self.action_comment_text(payload)
            if not comment_text:
                raise ValueError("implementer_kick_back requires a non-empty reason")
            updated = self.app.implementer_kick_back(ticket_id, comment_text, caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "request_commit_exempt":
            updated = self.app.request_commit_exempt(ticket_id, self.action_comment_text(payload), caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "start_task":
            updated = self.app.start_task(ticket_id, self.action_comment_text(payload), caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "complete_task":
            updated = self.app.complete_task(ticket_id, self.action_comment_text(payload), caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "await_role":
            updated = self.app.set_awaiting_role(ticket_id, str(payload.get("role", payload.get("awaiting_role", ""))), caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "clear_awaiting_role":
            updated = self.app.clear_awaiting_role(ticket_id, caller_role=caller)
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "inspector_sign_off":
            patch = {"state": "audit", "inspector_signoff": True}
        elif operation == "inspector_kick_back":
            patch = {"state": "in_progress"}
            comment_text = self.action_comment_text(payload)
            if comment_text:
                patch["comment"] = {"who": caller, "text": comment_text}
            if payload.get("target_assignee") or payload.get("assignee"):
                patch["assignee"] = str(payload.get("target_assignee", payload.get("assignee", "")))
        elif operation == "audit_sign_off":
            comment_text = self.action_comment_text(payload)
            if not comment_text:
                raise ValueError("audit_sign_off requires a non-empty comment")
            patch = {"audit_signoff": True, "comment": {"who": caller, "text": comment_text}}
        elif operation == "audit_kick_back":
            patch = {"state": "in_progress"}
            comment_text = self.action_comment_text(payload)
            if comment_text:
                patch["comment"] = {"who": caller, "text": comment_text}
            if payload.get("target_assignee") or payload.get("assignee"):
                patch["assignee"] = str(payload.get("target_assignee", payload.get("assignee", "")))
        elif operation == "director_dat_sign_off":
            comment_text = self.action_comment_text(payload)
            patch = {"state": "user_review"}
            if comment_text:
                patch["comment"] = {"who": caller, "text": comment_text}
        elif operation == "director_dat_kick_back":
            patch = {"state": "in_progress"}
            comment_text = self.action_comment_text(payload)
            if comment_text:
                patch["comment"] = {"who": caller, "text": comment_text}
            if payload.get("target_assignee") or payload.get("assignee"):
                patch["assignee"] = str(payload.get("target_assignee", payload.get("assignee", "")))
        elif operation == "user_sign_off":
            comment_text = self.action_comment_text(payload)
            patch = {"user_signoff": True}
            if comment_text:
                patch["comment"] = {"who": caller, "text": comment_text}
        elif operation == "user_reopen":
            patch = {"state": "analysis"}
            comment_text = self.action_comment_text(payload)
            if comment_text:
                patch["comment"] = {"who": caller, "text": comment_text}
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
            patch = {"comment": {"who": caller, "text": str(payload.get("text", "")), "urgent": bool(payload.get("urgent", False))}}
        elif operation == "edit_fields":
            invalid_fields = sorted(set(payload) - EDIT_FIELD_NAMES)
            if invalid_fields:
                raise ValueError(f"edit_fields cannot update: {', '.join(invalid_fields)}")
            if payload.get("audit_signoff") is True:
                raise ValueError("audit_signoff=true requires audit_sign_off")
            if payload.get("inspector_signoff") is True:
                raise ValueError("inspector_signoff=true requires inspector_sign_off")
            if payload.get("user_signoff") is True:
                raise ValueError("user_signoff=true requires user_sign_off")
            if "needs_inspection" in payload and caller != "director":
                raise PermissionError("needs_inspection can only be edited by director")
            if payload.get("needs_audit", True) is False and caller != "director":
                raise PermissionError("needs_audit can only be set to false by director")
            if "commit_exempt" in payload and caller != "director":
                raise PermissionError("commit_exempt can only be edited by director")
            patch = dict(payload)
        elif operation == "crop_attachment":
            updated = self.app.crop_attachment(
                ticket_id,
                source_path=str(payload.get("source_path", "")),
                rect=payload.get("rect", {}),  # type: ignore[arg-type]
                feedback_number=int(payload["feedback_number"]) if payload.get("feedback_number") not in (None, "") else None,
                set_label=str(payload.get("label", "")),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        else:
            raise ValueError(f"unknown ticket operation: {operation}")

        updated = self.app.update_ticket(ticket_id, patch, caller_role=caller)
        self.events.notify_change(self.app.store_signature())
        self.send_json({"ticket": updated})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            token_script = (
                f"  <script>window.PGU_TICKET_BOARD_WRITE_TOKEN = {json.dumps(self.write_token)};"
                f" window.PGU_TICKET_BOARD_BUILD_ID = {json.dumps(self.server.build_id)};</script>\n"
            )
            body = HTML.replace("  <script>\n", token_script + "  <script>\n", 1).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_no_cache_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/board":
            payload = self.app.snapshot()
            payload["build_id"] = self.server.build_id
            self.send_json(payload)
            return
        if parsed.path == "/api/client-config":
            self.send_json({"build_id": self.server.build_id, "write_token": self.write_token})
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
            self.send_cached_bytes(body, content_type=content_type or "application/octet-stream", source_path=path)
            return
        if parsed.path.startswith("/api/thumb/"):
            raw = urllib.parse.unquote(parsed.path.removeprefix("/api/thumb/"))
            query = urllib.parse.parse_qs(parsed.query)
            requested_size = query.get("w", [str(THUMBNAIL_MAX_SIZE)])[0]
            try:
                size = int(requested_size)
            except ValueError:
                size = THUMBNAIL_MAX_SIZE
            try:
                path = self.app.resolve_image(raw)
                body = self.thumbnail_bytes_for(path, size)
            except FileNotFoundError as exc:
                self.send_text(str(exc), HTTPStatus.NOT_FOUND)
                return
            except OSError as exc:
                self.send_text(f"thumbnail generation failed: {exc}", HTTPStatus.BAD_REQUEST)
                return
            self.send_cached_bytes(body, content_type="image/jpeg", source_path=path, variant=f"thumb-{max(64, min(size, THUMBNAIL_MAX_SIZE))}")
            return
        if parsed.path.startswith("/api/tickets/") and "/actions/" not in parsed.path:
            ticket_id = urllib.parse.unquote(parsed.path.removeprefix("/api/tickets/").strip("/"))
            if ticket_id:
                try:
                    self.send_json(self.app.get_ticket(ticket_id))
                except FileNotFoundError:
                    self.send_text("ticket not found", HTTPStatus.NOT_FOUND)
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
            self.wfile.write(f"event: version\ndata: {json.dumps({'build_id': self.server.build_id})}\n\n".encode("utf-8"))
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
            body = self.rfile.read(length) if length > 0 else b""
            if parsed.path == "/api/tickets/actions/file_report":
                self.require_http_report_token()
                payload = json.loads(body or b"{}")
                self.handle_file_report(payload)
                return
            self.require_http_write_token()
            if parsed.path == "/api/upload":
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    raise ValueError("upload requires an image/* Content-Type")
                query = urllib.parse.parse_qs(parsed.query)
                uploaded = self.app.save_uploaded_image(
                    body,
                    upload_set=query.get("set", [""])[0],
                    set_label=query.get("label", [""])[0],
                    attempt_number=query.get("attempt", [""])[0],
                    original_filename=query.get("filename", [""])[0],
                )
                self.send_json({"image": uploaded}, HTTPStatus.CREATED)
                return
            payload = json.loads(body or b"{}")
            if parsed.path == "/api/register-caller":
                self.handle_register_caller(payload)
                return
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
        events: TicketBoardEventHub | None = None,
        caller_registry: CallerRegistry | None = None,
        write_token: str | None = None,
        report_token: str | None = None,
    ) -> None:
        self.app = app
        self.events = events or TicketBoardEventHub(app)
        self._owns_events = events is None
        self.director_notifier = director_notifier or DirectorNotifier()
        self._owns_director_notifier = director_notifier is None
        self.caller_registry = caller_registry
        self.build_id = board_build_id()
        self.write_token = write_token or secrets.token_urlsafe(32)
        self.report_token = (report_token or "").strip()
        super().__init__(address, TicketBoardHandler)

    def server_close(self) -> None:
        if self._owns_events:
            self.events.close()
        if self._owns_director_notifier:
            self.director_notifier.close()
        super().server_close()


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class TicketBoardUnixServer(ThreadingUnixHTTPServer):
    def __init__(
        self,
        socket_path: Path,
        app: TicketBoardApp,
        *,
        events: TicketBoardEventHub,
        director_notifier: DirectorNotifier,
        caller_registry: CallerRegistry | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.app = app
        self.events = events
        self.director_notifier = director_notifier
        self.caller_registry = caller_registry or CallerRegistry()
        self.build_id = board_build_id()
        self.write_token = ""
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        super().__init__(str(socket_path), TicketBoardHandler)
        socket_path.chmod(PANE_SOCKET_MODE)

    def server_close(self) -> None:
        super().server_close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
