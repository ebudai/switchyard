"""Ticket-board storage and validation."""

from __future__ import annotations

import json
import os
import subprocess
import time
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

ASSET_DIR_DEFAULT = Path("~/.claude/pgu-tickets-assets").expanduser()
FRAME_DIR_DEFAULT = Path("/tmp/pgu-frames")
REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
COMMIT_GIT_DIR_DEFAULT = Path("/data/git/pgu.git")
POSTGRES_DSN_DEFAULT = os.environ.get("TICKET_BOARD_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "inspector", "agent", "director", "research")
LEGACY_ASSIGNEE_ALIASES = {"ui": "app"}
LEGACY_STATE_ALIASES = {"open": "analysis"}
STATES = (
    "backlog",
    "analysis",
    "in_progress",
    "inspection",
    "audit",
    "eric_review",
    "director_review",
    "done",
    "cancelled",
)
TERMINAL_STATES = {"done", "cancelled"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def ticket_number(ticket_id: str) -> int:
    value = str(ticket_id).strip().upper()
    if not value.startswith("PGU-") or not value[4:].isdigit():
        return 0
    return int(value[4:])


class TicketBoardApp:
    def __init__(
        self,
        frame_dir: Path = FRAME_DIR_DEFAULT,
        asset_dir: Path = ASSET_DIR_DEFAULT,
        repo_root: Path = REPO_ROOT_DEFAULT,
        commit_git_dir: Path = COMMIT_GIT_DIR_DEFAULT,
        database_url: str = POSTGRES_DSN_DEFAULT,
    ) -> None:
        self.frame_dir = frame_dir.resolve()
        self.asset_dir = asset_dir.expanduser().resolve()
        self.repo_root = repo_root.resolve()
        self.commit_git_dir = commit_git_dir.resolve()
        self.store_backend = "postgres"
        self.database_url = database_url
        self.asset_dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict[str, object]:
        tickets, errors = self.list_tickets()
        return {
            "tickets": tickets,
            "errors": errors,
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "screenshots": self.list_screenshots(),
            "store_backend": self.store_backend,
            "store_path": "postgres",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def store_signature(self) -> tuple[tuple[object, ...], ...]:
        return self._pg_store_signature()

    def list_tickets(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        try:
            return self._pg_list_tickets(), []
        except Exception as exc:  # noqa: BLE001
            return [], [{"file": "postgres", "error": str(exc)}]

    def list_screenshots(self) -> list[dict[str, str]]:
        if not self.frame_dir.is_dir():
            return []
        items: list[dict[str, str]] = []
        for path in sorted(self.frame_dir.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True):
            items.append(
                {
                    "path": str(path.resolve()),
                    "name": path.name,
                    "modified": format_timestamp(path),
                }
            )
        return items

    def resolve_image(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser().resolve()
        if not self._path_in_allowed_image_dirs(path):
            raise FileNotFoundError(f"screenshot path escapes allowed asset roots: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"screenshot not found: {path}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise FileNotFoundError(f"unsupported image type: {path.name}")
        return path

    def save_uploaded_image(self, raw_bytes: bytes) -> dict[str, str]:
        if not raw_bytes:
            raise ValueError("uploaded image is empty")
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(BytesIO(raw_bytes)) as image:
            image.load()
            output = image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")
            path = self.asset_dir / f"upload_{time.time_ns()}.png"
            output.save(path, format="PNG")
        return {
            "path": str(path.resolve()),
            "name": path.name,
            "modified": format_timestamp(path),
        }

    def create_ticket(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None,
        screenshots: list[str] | None = None,
        assignee: str,
        needs_eric_signoff: bool,
        needs_inspection: bool = False,
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        state: str = "analysis",
        implementation: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        state = self._validate_create_state(state)
        assignee = "unassigned" if state == "backlog" else assignee
        return self.create_ticket_record(
            title=title,
            body=body,
            screenshot=screenshot,
            screenshots=screenshots,
            assignee=assignee,
            state=state,
            blocked_by=blocked_by,
            parent_id="",
            implementation=implementation,
            audit_prompt="",
            audit_signoff=False,
            needs_inspection=needs_inspection,
            inspector_signoff=False,
            needs_eric_signoff=needs_eric_signoff,
            eric_signoff=False,
            comments=[],
            blocked_reason=blocked_reason,
            commit_hash="",
            commit_exempt=False,
            caller_role=caller_role,
        )

    def _validate_create_state(self, state: str) -> str:
        normalized = str(state).strip().lower() or "analysis"
        if normalized not in {"analysis", "backlog"}:
            raise ValueError(f"invalid create state: {state}; allowed: analysis, backlog")
        return normalized

    def create_ticket_record(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None,
        screenshots: list[str] | None,
        assignee: str,
        state: str,
        blocked_by: list[str] | None,
        implementation: str,
        audit_prompt: str,
        audit_signoff: bool,
        needs_inspection: bool = False,
        inspector_signoff: bool = False,
        needs_eric_signoff: bool,
        eric_signoff: bool,
        comments: list[dict[str, Any]],
        parent_id: str = "",
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._pg_create_ticket_record(
            title=title,
            body=body,
            screenshot=screenshot,
            screenshots=screenshots,
            assignee=assignee,
            state=state,
            blocked_by=blocked_by,
            implementation=implementation,
            audit_prompt=audit_prompt,
            audit_signoff=audit_signoff,
            needs_inspection=needs_inspection,
            inspector_signoff=inspector_signoff,
            needs_eric_signoff=needs_eric_signoff,
            eric_signoff=eric_signoff,
            comments=comments,
            parent_id=parent_id,
            blocked_reason=blocked_reason,
            commit_hash=commit_hash,
            commit_exempt=commit_exempt,
            created=created,
            updated=updated,
            caller_role=caller_role,
        )

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        return self._pg_update_ticket(ticket_id, patch, caller_role=caller_role)

    def route_ticket(self, ticket_id: str, state: str, assignee: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        state = self._validate_state(str(state))
        assignee = self._validate_assignee(str(assignee))
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                return self._pg_get_ticket(ticket_id, conn)

    def merge_tickets(self, source_ticket_id: str, target_ticket_id: str, *, actor: str) -> dict[str, dict[str, Any]]:
        actor_normalized = str(actor).strip().lower()
        if actor_normalized != "director":
            raise ValueError("ticket merge requires actor=director")
        source_id = str(source_ticket_id).strip().upper()
        target_id = str(target_ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, actor_normalized)
                self._pg_call(conn, "SELECT ticket_board.merge(%s, %s);", (source_id, target_id))
                return {
                    "source": self._pg_get_ticket(source_id, conn),
                    "target": self._pg_get_ticket(target_id, conn),
                }

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        if not ticket_id:
            raise FileNotFoundError("ticket not found")
        return self._pg_get_ticket(ticket_id)

    def verify_created_ticket_persisted(
        self,
        created: dict[str, object],
        before_signature: tuple[tuple[object, ...], ...],
    ) -> tuple[tuple[object, ...], ...]:
        ticket_id = str(created.get("id", "")).strip()
        title = str(created.get("title", "")).strip()
        body = str(created.get("body", ""))
        if not ticket_id or not title:
            raise ValueError("created ticket missing id/title")

        before_ids = {str(row[0]) for row in before_signature if row}
        before_max = max((ticket_number(ticket_id) for ticket_id in before_ids), default=0)
        created_number = ticket_number(ticket_id)
        if created_number <= before_max:
            raise ValueError(f"create returned non-new ticket id: {ticket_id}")
        if ticket_id in before_ids:
            raise ValueError(f"create collided with existing ticket id: {ticket_id}")
        persisted = self.get_ticket(ticket_id)
        if str(persisted.get("title", "")).strip() != title:
            raise ValueError(f"created ticket title mismatch in postgres: {ticket_id}")
        if str(persisted.get("body", "")) != body:
            raise ValueError(f"created ticket body mismatch in postgres: {ticket_id}")
        after_signature = self.store_signature()
        if after_signature == before_signature:
            raise ValueError(f"ticket create did not change the store: {ticket_id}")
        return after_signature

    def _pg_imports(self) -> tuple[Any, Any, Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on deployment environment
            raise RuntimeError(
                "postgres ticket store requires psycopg3; install the 'psycopg' Python package"
            ) from exc
        return psycopg, dict_row, Jsonb

    def _pg_connect(self) -> Any:
        psycopg, dict_row, _ = self._pg_imports()
        conn = psycopg.connect(self.database_url or "", row_factory=dict_row)
        conn.autocommit = True
        conn.execute("SET client_encoding TO 'UTF8';")
        conn.autocommit = False
        return conn

    def _pg_store_signature(self) -> tuple[tuple[object, ...], ...]:
        with self._pg_connect() as conn:
            rows = conn.execute(
                """
SELECT
    t.id,
    t.row_updated_at::text AS row_updated_at,
    t.updated_text,
    (SELECT count(*)::int FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id) AS blocker_count,
    (SELECT count(*)::int FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id) AS comment_count,
    (SELECT count(*)::int FROM ticket_board.ticket_attachments a WHERE a.ticket_id = t.id) AS attachment_count,
    COALESCE(
        (
            SELECT max(nt.id)::text
            FROM ticket_board.notification_trace nt
            WHERE nt.ticket_id = t.id
              AND nt.kind = 'transition'
              AND nt.event = 'send'
        ),
        ''
    ) AS last_transition_send_trace_id
FROM ticket_board.tickets t
ORDER BY t.ticket_number;
"""
            ).fetchall()
        return tuple(
            (
                str(row["id"]),
                str(row["row_updated_at"]),
                str(row["updated_text"]),
                int(row["blocker_count"]),
                int(row["comment_count"]),
                int(row["attachment_count"]),
                str(row["last_transition_send_trace_id"]),
            )
            for row in rows
        )

    def _pg_select_ticket_rows(self, conn: Any, ticket_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE t.id = %s" if ticket_id is not None else ""
        params = (ticket_id,) if ticket_id is not None else ()
        return conn.execute(
            f"""
WITH notification_scope AS (
    SELECT
        scoped.id,
        scoped.state,
        scoped.ticket_number,
        CASE
            WHEN scoped.state = 'in_progress' THEN NULLIF(scoped.assignee, 'unassigned')
            WHEN scoped.state IN ('analysis', 'director_review') THEN 'director'
            WHEN scoped.state = 'inspection' THEN 'inspector'
            WHEN scoped.state = 'audit' THEN 'audit'
            ELSE NULL
        END AS owner_role
    FROM ticket_board.tickets scoped
    WHERE scoped.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'director_review')
),
notification_candidates AS (
    SELECT
        notification_scope.id,
        notification_scope.state,
        notification_scope.ticket_number,
        notification_scope.owner_role,
        CASE
            WHEN notification_scope.state = 'in_progress'
                THEN notification_scope.state || ':' || notification_scope.owner_role
            ELSE notification_scope.state
        END AS active_work_partition,
        sent.last_sent_at AS active_work_notified_at
    FROM notification_scope
    LEFT JOIN LATERAL (
        SELECT max(trace.ts) AS last_sent_at
        FROM ticket_board.notification_trace trace
        WHERE trace.ticket_id = notification_scope.id
          AND trace.target_role = notification_scope.owner_role
          AND trace.kind = 'transition'
          AND trace.event = 'send'
          AND trace.ticket_state_at_event = notification_scope.state
    ) sent ON true
),
active_work AS (
    SELECT
        notification_candidates.id,
        notification_candidates.owner_role,
        notification_candidates.active_work_notified_at,
        notification_candidates.active_work_notified_at IS NOT NULL
            AND notification_candidates.owner_role IS NOT NULL
            AND row_number() OVER (
                PARTITION BY notification_candidates.active_work_partition
                ORDER BY notification_candidates.active_work_notified_at DESC NULLS LAST,
                    notification_candidates.ticket_number DESC
            ) = 1 AS active_work_highlight
    FROM notification_candidates
)
SELECT
    t.id,
    t.title,
    t.body,
    t.state,
    t.assignee,
    t.parent_id,
    t.blocked_reason,
    t.implementation,
    t.audit_prompt,
    t.audit_signoff,
    t.needs_inspection,
    t.inspector_signoff,
    t.needs_eric_signoff,
    t.eric_signoff,
    t.manually_controlled,
    t.commit_hash,
    t.commit_exempt,
    t.created_text,
    t.updated_text,
    active_work.owner_role AS active_work_owner_role,
    active_work.active_work_notified_at,
    COALESCE(active_work.active_work_highlight, false) AS active_work_highlight,
    COALESCE(
        (SELECT array_agg(b.blocker_ticket_id ORDER BY b.position)
         FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = t.id),
        ARRAY[]::text[]
    ) AS blocked_by,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('who', c.who, 'text', c.text, 'ts', c.ts_text) ORDER BY c.position)
         FROM ticket_board.ticket_comments c
         WHERE c.ticket_id = t.id),
        '[]'::jsonb
    ) AS comments,
    COALESCE(
        (SELECT array_agg(a.path ORDER BY a.position)
         FROM ticket_board.ticket_attachments a
         WHERE a.ticket_id = t.id),
        ARRAY[]::text[]
    ) AS screenshots
FROM ticket_board.tickets t
LEFT JOIN active_work ON active_work.id = t.id
{where}
ORDER BY t.ticket_number
;
""",
            params,
        ).fetchall()

    def _pg_list_tickets(self) -> list[dict[str, Any]]:
        with self._pg_connect() as conn:
            rows = self._pg_select_ticket_rows(conn)
        tickets = [self._pg_row_to_ticket(row) for row in rows]
        tickets.sort(
            key=lambda ticket: (ticket["state"] not in TERMINAL_STATES, ticket["updated"], ticket["id"]),
            reverse=True,
        )
        return tickets

    def _pg_get_ticket(self, ticket_id: str, conn: Any | None = None) -> dict[str, Any]:
        if conn is None:
            with self._pg_connect() as own_conn:
                return self._pg_get_ticket(ticket_id, own_conn)
        rows = self._pg_select_ticket_rows(conn, ticket_id=ticket_id)
        if not rows:
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        return self._pg_row_to_ticket(rows[0])

    def _pg_row_to_ticket(self, row: dict[str, Any]) -> dict[str, Any]:
        comments = row["comments"]
        if isinstance(comments, str):
            comments = json.loads(comments)
        screenshots = row["screenshots"] or []
        ticket = {
            "id": str(row["id"]),
            "title": self._require_text(row["title"], "title"),
            "body": self._require_body(row["body"]),
            "assignee": self._validate_assignee(str(row["assignee"])),
            "state": self._validate_state(str(row["state"])),
            "blocked_by": self._validate_blocked_by(list(row["blocked_by"] or []), str(row["id"])),
            "parent_id": str(row["parent_id"] or ""),
            "blocked_reason": self._require_plain_string(row["blocked_reason"], "blocked_reason"),
            "implementation": self._require_plain_string(row["implementation"], "implementation"),
            "audit_prompt": self._require_plain_string(row["audit_prompt"], "audit_prompt"),
            "audit_signoff": bool(row["audit_signoff"]),
            "needs_inspection": bool(row["needs_inspection"]),
            "inspector_signoff": bool(row["inspector_signoff"]),
            "needs_eric_signoff": bool(row["needs_eric_signoff"]),
            "eric_signoff": bool(row["eric_signoff"]),
            "manually_controlled": bool(row["manually_controlled"]),
            "commit_hash": str(row["commit_hash"] or ""),
            "commit_exempt": bool(row["commit_exempt"]),
            "created": self._require_text(row["created_text"], "created"),
            "updated": self._require_text(row["updated_text"], "updated"),
            "active_work_highlight": bool(row["active_work_highlight"]),
            "active_work_owner_role": str(row["active_work_owner_role"] or ""),
            "active_work_notified_at": self._format_optional_datetime(row["active_work_notified_at"]),
            "comments": self._validate_comments(comments),
        }
        self._set_screenshot_fields(ticket, self._build_screenshot_entries(list(screenshots)))
        return ticket

    def _format_optional_datetime(self, value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    def _pg_create_ticket_record(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None,
        screenshots: list[str] | None,
        assignee: str,
        state: str,
        blocked_by: list[str] | None,
        implementation: str,
        audit_prompt: str,
        audit_signoff: bool,
        needs_inspection: bool = False,
        inspector_signoff: bool = False,
        needs_eric_signoff: bool,
        eric_signoff: bool,
        comments: list[dict[str, Any]],
        parent_id: str = "",
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        title = self._require_text(title, "title").strip()
        assignee = self._validate_assignee(assignee)
        state = self._validate_state(state)
        create_state = self._validate_create_state(state)
        if create_state == "backlog":
            assignee = "unassigned"
        attachment_patch: dict[str, Any] = {}
        if screenshots not in (None, [], ""):
            attachment_patch["screenshots"] = screenshots
        elif screenshot not in (None, "", "null"):
            attachment_patch["screenshot"] = screenshot
        implementation = self._require_plain_string(implementation, "implementation")
        audit_prompt = self._require_plain_string(audit_prompt, "audit_prompt")
        if audit_prompt.strip():
            raise ValueError("postgres function API does not support audit_prompt writes yet")
        if audit_signoff or inspector_signoff or needs_eric_signoff or eric_signoff:
            raise ValueError("postgres function API does not support initial signoff fields yet")
        if commit_hash or commit_exempt:
            raise ValueError("postgres function API does not support initial commit fields yet")
        normalized_comments = self._validate_comments(comments)
        blocked_by = self._validate_blocked_by(blocked_by or [], "PGU-0")
        blocked_reason = self._require_plain_string(blocked_reason, "blocked_reason")
        self._enforce_blocked_reason_rule(blocked_by, blocked_reason)

        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                parent_id = "" if parent_id in (None, "", "null") else str(parent_id).strip().upper()
                if parent_id and create_state == "analysis" and not blocked_by:
                    ticket_id = self._pg_call_scalar(
                        conn,
                        "SELECT ticket_board.file_bug(%s, %s, %s) AS id;",
                        (title, body.strip(), parent_id),
                    )
                else:
                    ticket_id = self._pg_call_scalar(
                        conn,
                        "SELECT ticket_board.create_ticket(%s, %s, %s, %s, %s) AS id;",
                        (title, body.strip(), create_state, blocked_by, blocked_reason),
                    )
                    if parent_id:
                        self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"parent_id": parent_id})))
                if implementation:
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"implementation": implementation})))
                if needs_inspection:
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"needs_inspection": True})))
                if create_state != state:
                    raise ValueError(f"invalid create state: {state}; allowed: analysis, backlog")
                if create_state == "analysis" and assignee != "unassigned":
                    self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                if attachment_patch:
                    current = self._pg_get_ticket(ticket_id, conn)
                    self._materialize_edit_field_attachments(attachment_patch, ticket_id, current)
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps(attachment_patch)))
                for comment in normalized_comments:
                    self._pg_set_caller_role(conn, comment["who"])
                    self._pg_call(conn, "SELECT ticket_board.add_comment(%s, %s);", (ticket_id, comment["text"]))
                return self._pg_get_ticket(ticket_id, conn)

    def _pg_update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                current = self._pg_get_ticket(ticket_id, conn)
                edit_fields = self._pg_edit_field_patch(patch)
                patch = {key: value for key, value in patch.items() if key not in edit_fields}
                if edit_fields:
                    self._materialize_edit_field_attachments(edit_fields, ticket_id, current)
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps(edit_fields)))
                if "manually_controlled" in patch:
                    self._pg_call(
                        conn,
                        "SELECT ticket_board.set_manually_controlled(%s, %s);",
                        (ticket_id, bool(patch["manually_controlled"])),
                    )
                if "blocked_by" in patch or "blocked_reason" in patch:
                    blocked_by = self._validate_blocked_by(patch.get("blocked_by", current["blocked_by"]), ticket_id)
                    blocked_reason = self._require_plain_string(
                        patch.get("blocked_reason", current["blocked_reason"]),
                        "blocked_reason",
                    )
                    self._enforce_blocked_reason_rule(blocked_by, blocked_reason)
                    self._pg_call(conn, "SELECT ticket_board.set_blockers(%s, %s, %s);", (ticket_id, blocked_by, blocked_reason))

                comment_text = ""
                comment_who = ""
                if "comment" in patch:
                    comment = patch["comment"]
                    comment_who = str(comment.get("who", "")).strip()
                    comment_text = str(comment.get("text", "")).strip()
                    if not comment_who or not comment_text:
                        raise ValueError("comment requires non-empty who and text")
                    self._pg_set_caller_role(conn, comment_who)

                state = self._validate_state(str(patch["state"])) if "state" in patch else current["state"]
                assignee = self._validate_assignee(str(patch["assignee"])) if "assignee" in patch else current["assignee"]
                commit_hash = str(patch.get("commit_hash", current.get("commit_hash", "")) or "").strip()
                if "commit_hash" in patch and state not in {"audit", "done"}:
                    raise ValueError("postgres function API only accepts commit_hash when submitting to audit or marking done")

                if "audit_signoff" in patch:
                    if not bool(patch["audit_signoff"]):
                        raise ValueError("postgres function API only supports setting audit_signoff true")
                    self._pg_call(conn, "SELECT ticket_board.audit_sign_off(%s, %s);", (ticket_id, comment_text))
                    comment_text = ""
                inspector_signoff_handled = False
                if "inspector_signoff" in patch:
                    if not bool(patch["inspector_signoff"]):
                        raise ValueError("postgres function API only supports setting inspector_signoff true")
                    self._pg_call(conn, "SELECT ticket_board.inspector_sign_off(%s);", (ticket_id,))
                    inspector_signoff_handled = True
                if "eric_signoff" in patch:
                    if not bool(patch["eric_signoff"]):
                        raise ValueError("postgres function API only supports setting eric_signoff true")
                    self._pg_call(conn, "SELECT ticket_board.eric_sign_off(%s, %s);", (ticket_id, comment_text))
                    comment_text = ""

                if "state" in patch:
                    if inspector_signoff_handled and state == "audit":
                        pass
                    elif state == "in_progress" and comment_text and current["state"] == "inspection":
                        self._pg_call(conn, "SELECT ticket_board.inspector_kick_back(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    elif state == "in_progress":
                        self._pg_call(conn, "SELECT ticket_board.start_work(%s);", (ticket_id,))
                    elif state == "audit":
                        self._pg_call(conn, "SELECT ticket_board.submit_to_audit(%s, %s);", (ticket_id, commit_hash))
                    elif state == "done":
                        self._pg_call(conn, "SELECT ticket_board.mark_done(%s, %s);", (ticket_id, commit_hash))
                    elif state == "backlog":
                        self._pg_call(conn, "SELECT ticket_board.defer(%s);", (ticket_id,))
                    elif state == "cancelled":
                        if not comment_text:
                            raise ValueError("cancelling a ticket requires a non-empty comment explaining why")
                        self._pg_call(conn, "SELECT ticket_board.cancel(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    elif state == "analysis" and comment_text and current["state"] == "audit":
                        self._pg_call(conn, "SELECT ticket_board.audit_kick_back(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    elif state == "analysis" and comment_text and current["state"] in {"eric_review", "director_review", "done"}:
                        self._pg_call(conn, "SELECT ticket_board.eric_reopen(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    else:
                        self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                elif "assignee" in patch:
                    self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, current["state"], assignee))

                if comment_text:
                    self._pg_call(conn, "SELECT ticket_board.add_comment(%s, %s);", (ticket_id, comment_text))
                return self._pg_get_ticket(ticket_id, conn)

    def request_commit_exempt(self, ticket_id: str, reason: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        reason = self._require_text(reason, "reason").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.request_commit_exempt(%s, %s);", (ticket_id, reason))
                return self._pg_get_ticket(ticket_id, conn)

    def _pg_call(self, conn: Any, sql: str, params: tuple[Any, ...]) -> None:
        conn.execute(sql, params)

    def _pg_set_caller_role(self, conn: Any, caller_role: str) -> None:
        conn.execute("SELECT set_config('ticket_board.caller_role', %s, true);", (caller_role,))

    def _pg_call_scalar(self, conn: Any, sql: str, params: tuple[Any, ...]) -> str:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ValueError("postgres function returned no row")
        value = next(iter(row.values()))
        return str(value)

    def _pg_edit_field_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        editable = {
            "title",
            "parent_id",
            "screenshots",
            "screenshot",
            "implementation",
            "audit_prompt",
            "needs_inspection",
            "needs_eric_signoff",
            "commit_exempt",
            "commit_hash",
        }
        if patch.get("inspector_signoff") is False:
            editable = set(editable)
            editable.add("inspector_signoff")
        if patch.get("audit_signoff") is False:
            editable = set(editable)
            editable.add("audit_signoff")
        if patch.get("eric_signoff") is False:
            editable = set(editable)
            editable.add("eric_signoff")
        if "state" in patch:
            editable = set(editable)
            editable.discard("commit_hash")
        if "blocked_by" in patch:
            editable = set(editable)
        return {field: patch[field] for field in editable if field in patch}

    def _materialize_edit_field_attachments(self, edit_fields: dict[str, Any], ticket_id: str, current: dict[str, Any]) -> None:
        if "screenshots" in edit_fields:
            edit_fields["screenshots"] = self._materialize_attachments(
                edit_fields["screenshots"],
                ticket_id,
                current_paths=current.get("screenshots", []),
            )
            edit_fields["screenshot"] = edit_fields["screenshots"][0] if edit_fields["screenshots"] else ""
        elif "screenshot" in edit_fields:
            paths = self._materialize_attachments(
                edit_fields["screenshot"],
                ticket_id,
                current_paths=current.get("screenshots", []),
            )
            edit_fields["screenshots"] = paths
            edit_fields["screenshot"] = paths[0] if paths else ""

    def _validate_comments(self, raw: Any) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            raise ValueError("comments must be a list")
        comments: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("comment entries must be objects")
            comments.append(
                {
                    "who": self._require_text(item.get("who"), "comment.who"),
                    "text": self._require_text(item.get("text"), "comment.text"),
                    "ts": self._require_text(item.get("ts"), "comment.ts"),
                }
            )
        return comments

    def _validate_blocked_by(self, raw: Any, ticket_id: str) -> list[str]:
        if raw in (None, "", "null"):
            return []
        if not isinstance(raw, list):
            raise ValueError("blocked_by must be a list of ticket IDs")
        blocked_by: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError("blocked_by entries must be strings")
            blocker_id = item.strip().upper()
            if not blocker_id:
                raise ValueError("blocked_by entries must not be empty")
            if not blocker_id.startswith("PGU-") or not blocker_id[4:].isdigit():
                raise ValueError(f"invalid blocked_by ticket id: {item}")
            if blocker_id == ticket_id:
                raise ValueError("ticket cannot be blocked_by itself")
            if blocker_id not in blocked_by:
                blocked_by.append(blocker_id)
        return blocked_by

    def _enforce_blocked_reason_rule(self, blocked_by: list[str], blocked_reason: str) -> None:
        if blocked_by and not blocked_reason.strip():
            raise ValueError("blocked_reason must be non-empty when blocked_by is set")

    def _validate_commit_hash(self, raw: Any) -> str:
        if raw in (None, ""):
            return ""
        if not isinstance(raw, str):
            raise ValueError("commit_hash must be a string")
        value = raw.strip()
        if not value:
            return ""
        proc = subprocess.run(
            ["git", f"--git-dir={self.commit_git_dir}", "cat-file", "-e", f"{value}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"unknown commit_hash: {value}")
        resolved = subprocess.run(
            ["git", f"--git-dir={self.commit_git_dir}", "rev-parse", "--verify", f"{value}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode != 0 or not resolved.stdout.strip():
            raise ValueError(f"unknown commit_hash: {value}")
        return resolved.stdout.strip()

    def _commit_is_on_main(self, commit_hash: str) -> bool:
        proc = subprocess.run(
            ["git", f"--git-dir={self.commit_git_dir}", "merge-base", "--is-ancestor", commit_hash, "refs/heads/main"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0

    def _path_in_allowed_image_dirs(self, path: Path) -> bool:
        return self.frame_dir in path.parents or self.asset_dir in path.parents

    def _path_in_asset_dir(self, path: Path) -> bool:
        return self.asset_dir == path or self.asset_dir in path.parents

    def _validate_stored_screenshots(self, raw_screenshots: Any, raw_screenshot: Any) -> list[dict[str, Any]]:
        raw_items: list[Any] = []
        if raw_screenshots not in (None, "", "null"):
            if not isinstance(raw_screenshots, list):
                raise ValueError("screenshots must be a list of paths")
            raw_items.extend(raw_screenshots)
        elif raw_screenshot not in (None, "", "null"):
            raw_items.append(raw_screenshot)

        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, str):
                raise ValueError("screenshot entries must be path strings")
            normalized = self._normalize_image_path(item)
            if normalized in seen:
                continue
            path = Path(normalized)
            if not self._path_in_allowed_image_dirs(path):
                raise ValueError(f"screenshot path escapes allowed asset roots: {path}")
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                raise ValueError(f"unsupported image type: {path.name}")
            entries.append({"path": normalized, "available": path.is_file()})
            seen.add(normalized)
        return entries

    def _materialize_attachments(self, raw: Any, ticket_id: str, current_paths: list[str] | None = None) -> list[str]:
        if raw in (None, "", "null"):
            return []

        if isinstance(raw, str):
            raw_items = [raw]
        elif isinstance(raw, list):
            raw_items = raw
        else:
            raise ValueError("screenshots must be a path string, list of paths, or null")

        normalized_current = {
            self._normalize_image_path(path): path
            for path in (current_paths or [])
            if isinstance(path, str) and path
        }
        screenshot_paths: list[str] = []
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, str):
                raise ValueError("screenshot entries must be path strings")
            normalized = self._normalize_image_path(item)
            if normalized in seen:
                continue
            path = Path(normalized)
            if normalized in normalized_current and self._path_in_asset_dir(Path(normalized_current[normalized])):
                screenshot_paths.append(normalized_current[normalized])
            elif self._path_in_asset_dir(path):
                self.resolve_image(normalized)
                screenshot_paths.append(normalized)
            else:
                screenshot_paths.append(self._copy_attachment(normalized, ticket_id))
            seen.add(normalized)
        return screenshot_paths

    def _copy_attachment(self, raw: str, ticket_id: str) -> str:
        source = self.resolve_image(raw)
        destination = self.asset_dir / f"{ticket_id}-{time.time_ns()}.png"
        with Image.open(source) as image:
            image.load()
            output = image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")
            output.save(destination, format="PNG")
        if source.parent == self.asset_dir and source.name.startswith("upload_"):
            source.unlink(missing_ok=True)
        return str(destination.resolve())

    def _build_screenshot_entries(self, paths: list[str]) -> list[dict[str, Any]]:
        return [{"path": path, "available": Path(path).is_file()} for path in paths]

    def _unique_paths(self, paths: list[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for path in paths:
            if not path or path in seen:
                continue
            unique.append(path)
            seen.add(path)
        return unique

    def _set_screenshot_fields(self, ticket: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        ticket["screenshots"] = [entry["path"] for entry in entries]
        ticket["screenshots_info"] = entries
        if entries:
            ticket["screenshot"] = entries[0]["path"]
            ticket["screenshot_available"] = entries[0]["available"]
        else:
            ticket["screenshot"] = None
            ticket["screenshot_available"] = False

    def _normalize_image_path(self, raw: str) -> str:
        return str(Path(raw).expanduser().resolve())

    def _validate_state(self, state: str) -> str:
        state = LEGACY_STATE_ALIASES.get(state, state)
        if state not in STATES:
            raise ValueError(f"invalid state: {state}")
        return state

    def _validate_assignee(self, assignee: str) -> str:
        assignee = LEGACY_ASSIGNEE_ALIASES.get(assignee, assignee)
        if assignee not in ASSIGNEES:
            raise ValueError(f"invalid assignee: {assignee}")
        return assignee

    def _require_text(self, raw: Any, field: str) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return raw

    def _require_body(self, raw: Any) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise ValueError("body must be a string")
        return raw

    def _require_plain_string(self, raw: Any, field: str) -> str:
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise ValueError(f"{field} must be a string")
        return raw
