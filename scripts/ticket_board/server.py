"""HTTP serving for the ticket-board tool."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import secrets
import socket
import socketserver
# The server runs no process itself; suites patch server.subprocess.run to reach
# board_notifications' directorctl send (SYRD-504).
import subprocess  # noqa: F401
import threading
import time
import urllib.parse
from email.utils import formatdate, parsedate_to_datetime
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image

from .app import TicketBoardApp, iso_now
from .board_notifications import (
    DEFAULT_DIRECTORCTL,
    DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS,
    DirectorNotifier,
    TicketBoardEventHub,
    director_target,
    send_director_message,
)
from .build_identity import (
    REPO_ROOT,
    RELEASE_SHA_RE,
    board_build_id,
    build_id_from_file,
    build_id_from_release_path,
)
from .frontend import render_html
from .local_peer_authority import (
    PANE_SOCKET_MODE,
    SO_PEERCRED_FORMAT,
    CallerIdentityError,
    LocalRoleAuthority,
    PeerCredentials,
    ProcessRoleAuthority,
    RoleAccount,
    allowed_peer_uids,
    peer_credentials,
    restrict_socket_to_tenant,
)
from .operation_role_policy import (
    CALLER_ROLES,
    COMPOSED_OPERATION_CAPABILITIES,
    CONTROL_OVERRIDE_OPERATIONS,
    DEFAULT_IMPLEMENTER_ROLES,
    DEFAULT_OPERATION_ALLOWED_ROLES,
    DRAFT_ROLES,
    IMPLEMENTER_ROLES,
    OPERATION_ALLOWED_ROLES,
    PUBLICATION_OPERATIONS,
    TASK_ROLES,
)
from .peer_identity import SessionIdentity
from . import extension_operations, pull_queue
from .workflow_config import DIRECTOR_IDENTIFYING_CAPABILITIES, LEGACY_ASSIGNEE_SCOPED_OPERATIONS

LOGGER = logging.getLogger(__name__)
CALLER_ROLE_HEADER = "X-Ticket-Board-Caller-Role"
WRITE_TOKEN_HEADER = "X-Ticket-Board-Write-Token"
REPORT_TOKEN_HEADER = "X-Ticket-Board-Report-Token"
LEGACY_CALLER_ROLE_HEADER = "X-PGU-Caller-Role"
LEGACY_WRITE_TOKEN_HEADER = "X-PGU-Write-Token"
IMAGE_CACHE_CONTROL = "public, max-age=31536000, immutable"
THUMBNAIL_MAX_SIZE = 512
THUMBNAIL_QUALITY = 80
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
    "audit_signoff",
    "inspector_signoff",
    "user_signoff",
}

class TicketBoardHandler(BaseHTTPRequestHandler):
    server_version = "PGUTicketBoard/0.1"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self._local_peer_credentials: PeerCredentials | None = None
        if self.role_authority is not None:
            self._local_peer_credentials = peer_credentials(self.connection)  # type: ignore[arg-type]

    def finish(self) -> None:
        # Nothing to release: role authority is resolved per request from the
        # peer's uid, so no state is held across connections or restarts.
        super().finish()

    def require_allowed_peer(self) -> PeerCredentials:
        """Refuse local peers from outside this tenant.

        Socket group membership is the enforcing boundary; this is the second
        check that makes a misconfigured socket fail closed and leaves a log
        line naming the uid that tried.

        The project's own role accounts are admitted here by construction: they
        come from the same authoritative table the role is then resolved
        against, so this check can never refuse a role the board would go on to
        authorize. Deriving them separately is what made a rendered unit admit
        only the legacy owner and lock every role out (SYRD-39).
        """
        if self._local_peer_credentials is None:
            raise ValueError("local socket request missing peer credentials")
        allowed = allowed_peer_uids()
        authority = self.role_authority
        if authority is not None:
            allowed = allowed | authority.uids()
        if allowed and self._local_peer_credentials.uid not in allowed:
            LOGGER.warning(
                "Rejected local board connection from uid=%s pid=%s: not this tenant's account "
                "or one of its role accounts",
                self._local_peer_credentials.uid,
                self._local_peer_credentials.pid,
            )
            raise PermissionError("this board socket does not serve that local user")
        return self._local_peer_credentials

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
    def role_authority(self) -> LocalRoleAuthority | None:
        return getattr(self.server, "role_authority", None)

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
        if self.role_authority is not None:
            credentials = self.require_allowed_peer()
            # New boards bind the kernel-observed pane process to PostgreSQL;
            # LocalRoleAuthority remains only for an explicit legacy test or
            # compatibility caller. Headers are never consulted on this path.
            if hasattr(self.role_authority, "role_for_peer"):
                return self.role_authority.role_for_peer(credentials)
            return self.role_authority.role_for_uid(credentials.uid)
        # Keep the legacy name while clients transition. A server-side alias only
        # protects old clients on a new board; clients must dual-send to support
        # new tooling talking to an older deployed board.
        raw = self.headers.get(CALLER_ROLE_HEADER, "") or self.headers.get(LEGACY_CALLER_ROLE_HEADER, "")
        role = raw.strip().lower()
        if not role:
            raise ValueError(f"missing {CALLER_ROLE_HEADER}")
        if role not in getattr(self.app, "workflow_roles", lambda: list(CALLER_ROLES))():
            raise ValueError(f"invalid caller role: {raw}")
        return role

    def require_http_write_token(self) -> None:
        if self.role_authority is not None:
            return
        raw = self.headers.get(WRITE_TOKEN_HEADER, "") or self.headers.get(LEGACY_WRITE_TOKEN_HEADER, "")
        if not raw or not secrets.compare_digest(raw, self.write_token):
            raise PermissionError(f"missing or invalid {WRITE_TOKEN_HEADER}")

    def require_http_report_token(self) -> None:
        if self.role_authority is not None:
            raise PermissionError("tenant report filing is only available over HTTP")
        if not self.report_token:
            raise PermissionError(f"{REPORT_TOKEN_HEADER} is not configured")
        raw = self.headers.get(REPORT_TOKEN_HEADER, "")
        if not raw or not secrets.compare_digest(raw, self.report_token):
            raise PermissionError(f"missing or invalid {REPORT_TOKEN_HEADER}")

    def handle_register_caller(self, payload: dict[str, object]) -> None:
        """Register a launcher pane once, or report its existing assignment.

        The server derives the pane PID/start time and project UID. The payload
        supplies routing data that PostgreSQL validates against the active
        workflow; subsequent writes resolve only from the stored process key.
        """
        if self.role_authority is None:
            raise ValueError("caller registration is only available on the local Unix socket")
        credentials = self.require_allowed_peer()
        claimed = str(payload.get("role", "")).strip().lower()
        if hasattr(self.role_authority, "session_for_peer"):
            authority = self.role_authority
            session = authority.session_for_peer(credentials)
            existing = self.app.runtime_assignment_for_process(
                session.pid, session.start_time, credentials.uid
            )
            if existing is None:
                required = {"role", "runtime", "target", "worktree", "session_dir"}
                if not required <= set(payload):
                    raise CallerIdentityError(
                        "pane is not registered; the launcher must publish its runtime assignment"
                    )
                previous = self.app.runtime_assignment(claimed)
                if previous is not None:
                    old = SessionIdentity(
                        int(previous["process_pid"]), int(previous["process_start_time"])
                    )
                    if authority.session_live(old) and old != session:
                        raise CallerIdentityError(f"role {claimed} is held by a live pane")
                existing = self.app.register_runtime_assignment(
                    role=claimed,
                    runtime=str(payload["runtime"]),
                    target=str(payload["target"]),
                    worktree=str(payload["worktree"]),
                    session_dir=str(payload["session_dir"]),
                    process_pid=session.pid,
                    process_start_time=session.start_time,
                    process_uid=credentials.uid,
                    expected_generation=int(previous["generation"]) if previous else 0,
                )
            derived = str(existing["role"])
        else:
            derived = self.role_authority.role_for_uid(credentials.uid)
        if claimed and claimed != derived:
            LOGGER.warning(
                "Refused role claim %r from peer pid=%s uid=%s: that account is %s",
                claimed,
                credentials.pid,
                credentials.uid,
                derived,
            )
            raise CallerIdentityError(
                f"this pane is registered as {derived}, not {claimed}"
            )
        self.send_json({"role": derived, "uid": credentials.uid, "pid": credentials.pid})

    def require_operation_allowed(self, operation: str, caller_role: str, ticket_id: str | None = None) -> None:
        cfg = getattr(self.app, "workflow_configuration", lambda: None)()
        if cfg:
            role = next((r for r in cfg["roles"] if r["name"] == caller_role and r["active"]), None)
            if role is None:
                raise PermissionError("inactive or unknown workflow actor")
            if ticket_id:
                ticket = self.app.get_ticket(ticket_id)
                from .workflow_config import available_transitions
                if any(t["action"] == operation for t in available_transitions(cfg, ticket, caller_role)):
                    return
            if operation not in role["capabilities"]:
                if operation in CONTROL_OVERRIDE_OPERATIONS and (
                    DIRECTOR_IDENTIFYING_CAPABILITIES <= set(role["capabilities"])
                ):
                    # The control role, found by what it can do rather than by
                    # its name. The database makes the same determination and
                    # is still the authority; refusing here only meant the one
                    # documented escape hatch was unusable on every declared
                    # board (SYRD-180).
                    return
                composed = COMPOSED_OPERATION_CAPABILITIES.get(operation)
                if composed and composed <= set(role["capabilities"]):
                    # Built from capabilities this role already holds, so the
                    # atomic form conveys no authority the sequence did not.
                    return
                raise PermissionError(f"{caller_role} cannot call {operation}")
            return
        if operation in PUBLICATION_OPERATIONS:
            raise PermissionError(
                f"{operation} requires a declared workflow; this board has none, so there is "
                "no capability to admit a caller by"
            )
        allowed = OPERATION_ALLOWED_ROLES.get(operation)
        if allowed is None:
            raise ValueError(f"unknown ticket operation: {operation}")
        if caller_role not in allowed:
            raise PermissionError(f"{caller_role} cannot call {operation}")
        if operation in LEGACY_ASSIGNEE_SCOPED_OPERATIONS and ticket_id is not None:
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
        # SYRD-521: a declared stage of kind draft with one owner gives every new
        # ticket to that owner (the insert trigger), and a stage that notifies
        # its assignee then tells them at once. So an EXPLICIT request for an
        # unassigned ticket there cannot be honoured: MEFP's importer asked for
        # inert Draft intake and got a Draft owned by -- and acted on by -- the
        # designer. Refused before anything is written or sent, with where
        # inert intake does work. Leaving the assignee out still means "the
        # stage's owner", which is ordinary Draft ownership.
        if "assignee" in payload and str(payload.get("assignee") or "").strip().lower() == "unassigned":
            cfg = getattr(self.app, "workflow_configuration", lambda: None)()
            stage = next((s for s in (cfg or {}).get("stages", []) if s.get("name") == state), None)
            if stage and stage.get("kind") == "draft" and len(stage.get("owners") or []) == 1:
                from .workflow_config import parking_stage_names

                owner = stage["owners"][0]
                inert = " or ".join(sorted(parking_stage_names(cfg))) or "a stage nobody owns"
                raise ValueError(
                    f"an explicitly unassigned {state} ticket would not stay unassigned: this workflow gives "
                    f"every new {state} ticket to its owner, {owner}. For inert intake create it in {inert} "
                    f"(owned by nobody, notifies nobody); to hand it to {owner}, leave the assignee out"
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
        if operation == "create_ticket" and caller_role == "director" and self.role_authority is None:
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
        # PostgreSQL inserts already enqueue durable transition delivery. The
        # direct sender is only a fallback for stores without that queue.
        if (
            getattr(self.app, "store_backend", "") != "postgres"
            and notification_source_role != "director"
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
        if operation == "configure_workflow" and ticket_id is None:
            if type(payload.get("expected_revision")) is not int or type(payload.get("dry_run", False)) is not bool:
                raise ValueError("workflow apply requires integer expected_revision and boolean dry_run")
            self.send_json(self.app.apply_workflow(payload.get("document"), expected_revision=payload["expected_revision"], dry_run=payload.get("dry_run",False), caller_role=caller))
            return
        if operation == "set_workflow_flags" and ticket_id is not None:
            if caller != "director": raise PermissionError("only director may set gates")
            self.send_json({"ticket": self.app.set_workflow_flags(ticket_id, payload, caller_role=caller)})
            return
        if operation in pull_queue.OPERATIONS and ticket_id is None:  # SYRD-539: the database authorises a self-claim
            return self.send_json(pull_queue.perform(self.app, operation, payload, caller_role=caller))
        self.require_operation_allowed(operation, caller, ticket_id)
        if operation in extension_operations.OPERATIONS:  # SYRD-537, SYRD-541
            self.send_json(extension_operations.perform(self.app, operation, payload, caller_role=caller, ticket_id=ticket_id))
            return

        cfg = getattr(self.app, "workflow_configuration", lambda: None)()
        if cfg and ticket_id and any(t["action"] == operation for t in cfg["transitions"]):
            payload = dict(payload)
            if "state" in payload:
                payload["target"] = payload.pop("state")
            extension_operations.before_transition(self.app, ticket_id, operation, payload, caller_role=caller)
            ticket = self.app.perform_workflow_action(ticket_id, operation, payload, caller_role=caller)
            # Every declared transition is a committed mutation, so it pushes
            # like the per-operation handlers below. Without this an open board
            # sat on the previous stage until someone reloaded it, and the
            # reconciler could not cover for it either (see store_signature).
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": ticket})
            return

        if operation == "create_ticket":
            before_signature = self.app.store_signature()
            created = self.create_ticket_from_payload(payload, caller)
            self.send_ticket_created(
                created,
                before_signature,
                notification_source_role=self.notification_source_role(operation, caller) or caller,
            )
            return

        if operation == "dismiss_notification":
            raw_notification_id = payload.get("notification_id")
            if raw_notification_id not in (None, ""):
                dismissed = self.app.dismiss_notification(
                    int(raw_notification_id),
                    reason=str(payload.get("reason", "")),
                    caller_role=caller,
                )
            else:
                dismissed = self.app.dismiss_notification_by_key(
                    ticket_id=str(payload.get("ticket_id", "")),
                    target_role=str(payload.get("target_role", "")),
                    kind=str(payload.get("kind", "transition")),
                    reason=str(payload.get("reason", "")),
                    caller_role=caller,
                )
            self.events.notify_change(self.app.store_signature())
            self.send_json(dismissed)
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
        if operation == "reassign":
            updated = self.app.reassign_ticket(
                ticket_id,
                str(payload.get("assignee", payload.get("target_assignee", ""))),
                reason=str(payload.get("reason", payload.get("text", ""))),
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
        elif operation == "director_edit":
            updated = self.app.director_edit_ticket(
                ticket_id,
                payload.get("patch", {}),
                reason=str(payload.get("reason", "")),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "request_publication":
            result = self.app.request_publication(
                ticket_id,
                ref=str(payload.get("ref", "")),
                commit=str(payload.get("commit", payload.get("commit_hash", ""))),
                bundle=str(payload.get("bundle", payload.get("bundle_path", ""))),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json(result)
            return
        elif operation == "resolve_publication":
            result = self.app.resolve_publication(
                int(payload.get("request_id", payload.get("request", 0)) or 0),
                outcome=str(payload.get("outcome", "")),
                detail=str(payload.get("detail", payload.get("reason", ""))),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json(result)
            return
        elif operation in CONTROL_OVERRIDE_OPERATIONS:
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
        elif operation == "submit_to_audit_without_commit":
            reason = self.action_comment_text(payload)
            if not reason:
                raise ValueError("submit_to_audit_without_commit requires a non-empty reason")
            updated = self.app.submit_to_audit_without_commit(ticket_id, reason, caller_role=caller)
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
        elif operation == "recover_stalled_ticket":
            updated = self.app.recover_stalled_ticket(
                ticket_id,
                reason=str(payload.get("reason", payload.get("text", ""))),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "release_external_blocker":
            updated = self.app.release_external_blocker(
                ticket_id,
                ref=str(payload.get("ref", payload.get("blocker", ""))),
                reason=str(payload.get("reason", payload.get("text", ""))),
                commit=str(payload.get("commit", "") or ""),
                caller_role=caller,
            )
            self.events.notify_change(self.app.store_signature())
            self.send_json({"ticket": updated})
            return
        elif operation == "request_dependency":
            updated = self.app.request_dependency(
                ticket_id,
                awaiting_role=str(payload.get("role", payload.get("awaiting_role", ""))),
                reason=str(payload.get("reason", payload.get("text", ""))),
                caller_role=caller,
            )
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
            # A reason with nothing to wait on records no wait: the ticket is
            # not blocked, its owner is reminded as before, and the reason is
            # only a note. MEFP-14's Director set exactly that for a
            # cross-board dependency and was told it succeeded (SYRD-285).
            # Clearing -- no blockers, no reason -- is still allowed; so is
            # editing the note on the ticket itself.
            listed = patch["blocked_by"] if isinstance(patch["blocked_by"], list) else [patch["blocked_by"]]
            if not any(str(item or "").strip() for item in listed) and patch["blocked_reason"].strip():
                raise ValueError(
                    "set_blockers needs something to wait on: a blocked_reason alone is a note, not a "
                    "wait. Name it in blocked_by -- a ticket here (PREFIX-N), work on another board "
                    "(project:PREFIX-N), or a person (operator:<name>)"
                )
        elif operation == "add_comment":
            patch = {"comment": {"who": caller, "text": str(payload.get("text", "")), "urgent": bool(payload.get("urgent", False))}}
        elif operation == "edit_fields":
            if "commit_hash" in payload:
                raise ValueError("commit_hash must be written with submit_to_audit or mark_done, not edit_fields")
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
                f"  <script>window.TICKET_BOARD_WRITE_TOKEN = {json.dumps(self.write_token)};"
                f" window.PGU_TICKET_BOARD_WRITE_TOKEN = {json.dumps(self.write_token)};"
                f" window.TICKET_BOARD_BUILD_ID = {json.dumps(self.server.build_id)};"
                f" window.PGU_TICKET_BOARD_BUILD_ID = {json.dumps(self.server.build_id)};</script>\n"
            )
            page = render_html(
                project=getattr(self.app, "project", "pgu"),
                project_name=getattr(self.app, "project_name", "PGU"),
                ticket_prefix=getattr(self.app, "ticket_prefix", "PGU"),
            )
            body = page.replace("  <script>\n", token_script + "  <script>\n", 1).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_no_cache_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/board":
            payload = self.app.snapshot()
            payload.setdefault("project", getattr(self.app, "project", "pgu"))
            payload.setdefault("project_name", getattr(self.app, "project_name", "PGU"))
            payload.setdefault("ticket_prefix", getattr(self.app, "ticket_prefix", "PGU"))
            payload["build_id"] = self.server.build_id
            self.send_json(payload)
            return
        if parsed.path == "/api/workflow":
            self.send_json(self.app.workflow_document())
            return
        if parsed.path == "/api/reservations":
            # Each implementer's serial slot as the routing gate sees it, so a
            # worker's capacity is read from the board rather than guessed from
            # its queue (SYRD-476). Absent on a board older than this route.
            self.send_json({
                "project": getattr(self.app, "project", "pgu"),
                "reservations": self.app.serial_reservations(),
                **extension_operations.reservation_fields(self.app),  # SYRD-539 pull_queue, SYRD-568 queues
            })
            return
        if parsed.path == "/api/reminder-snoozes":  # SYRD-537
            self.send_json({"batches": extension_operations.reminder_snoozes(self.app)})
            return
        if parsed.path == "/api/runtime-assignments":
            self.send_json({
                "project": getattr(self.app, "project", "pgu"),
                "authority_mode": (
                    "process"
                    if os.environ.get("TICKET_BOARD_PROCESS_AUTHORITY", "").strip() == "1"
                    else "legacy_uid"
                ),
                "assignments": self.app.runtime_targets(),
            })
            return
        if parsed.path.startswith("/api/runtime-assignments/"):
            role = urllib.parse.unquote(
                parsed.path.removeprefix("/api/runtime-assignments/").strip("/")
            ).strip().lower()
            assignment = self.app.runtime_targets().get(role) if role else None
            if assignment is None:
                self.send_text("runtime assignment not found", HTTPStatus.NOT_FOUND)
            else:
                self.send_json({
                    "project": getattr(self.app, "project", "pgu"),
                    "authority_mode": (
                        "process"
                        if os.environ.get("TICKET_BOARD_PROCESS_AUTHORITY", "").strip() == "1"
                        else "legacy_uid"
                    ),
                    "assignment": assignment,
                })
            return
        if parsed.path == "/api/publications":
            query = urllib.parse.parse_qs(parsed.query)
            self.send_json({
                "project": getattr(self.app, "project", "pgu"),
                "requests": self.app.publication_requests(
                    ticket_id=(query.get("ticket") or [""])[0],
                    state=(query.get("state") or ["requested"])[0],
                ),
            })
            return
        if parsed.path == "/api/client-config":
            self.send_json({
                "build_id": self.server.build_id,
                "write_token": self.write_token,
                "project": getattr(self.app, "project", "pgu"),
                "project_name": getattr(self.app, "project_name", "PGU"),
                "ticket_prefix": getattr(self.app, "ticket_prefix", "PGU"),
                # Where this board verifies a commit_hash. A role pane is not
                # told this by its environment -- only the board unit carries it
                # -- so a client that has just published has no way to put the
                # commit somewhere the board will find it without asking
                # (SYRD-125). Paths, not contents: nothing here is secret, and
                # the board remains the one authority on which repository it
                # actually reads.
                "commit_repositories": [str(path) for path in getattr(self.app, "commit_git_dirs", ())],
            })
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
            announced = self._write_sync_health(None)
            self.wfile.flush()
            while True:
                try:
                    next_version = listener.get(timeout=15.0)
                    self.wfile.write(f"event: board\ndata: {json.dumps({'version': next_version})}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                announced = self._write_sync_health(announced)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.events.unregister(listener)

    def _write_sync_health(self, announced: str | None) -> str:
        """Tell an open board when its live updates stop being trustworthy.

        Sent on connect and whenever it changes, so a client that has been open
        for hours learns about a reconciler that started failing after it
        connected -- the case where the board looks current and is not.
        """
        reason = self.events.degraded_reason
        if reason == announced:
            return reason
        payload = json.dumps({"degraded": bool(reason), "reason": reason})
        self.wfile.write(f"event: sync\ndata: {payload}\n\n".encode("utf-8"))
        return reason

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
            if parsed.path == "/api/workflow":
                caller = self.caller_role()
                if set(payload) - {"document", "expected_revision", "dry_run"} or type(payload.get("expected_revision")) is not int or type(payload.get("dry_run", False)) is not bool:
                    raise ValueError("workflow apply requires document, integer expected_revision and boolean dry_run")
                self.send_json(self.app.apply_workflow(payload.get("document"), expected_revision=payload["expected_revision"], dry_run=payload.get("dry_run", False), caller_role=caller))
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
        role_authority: LocalRoleAuthority | None = None,
        write_token: str | None = None,
        report_token: str | None = None,
    ) -> None:
        self.app = app
        self.events = events or TicketBoardEventHub(app)
        self._owns_events = events is None
        self.director_notifier = director_notifier or DirectorNotifier(project=getattr(app, "project", None))
        self._owns_director_notifier = director_notifier is None
        self.role_authority = role_authority
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
        role_authority: LocalRoleAuthority | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.app = app
        self.events = events
        self.director_notifier = director_notifier
        self.role_authority = role_authority or (
            ProcessRoleAuthority(app, os.environ.get("TICKET_BOARD_TENANT_USER", ""))
            if os.environ.get("TICKET_BOARD_PROCESS_AUTHORITY", "").strip() == "1"
            else LocalRoleAuthority.from_environ()
        )
        self.build_id = board_build_id()
        self.write_token = ""
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        super().__init__(str(socket_path), TicketBoardHandler)
        socket_path.chmod(PANE_SOCKET_MODE)
        restrict_socket_to_tenant(socket_path)

    def server_close(self) -> None:
        super().server_close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
