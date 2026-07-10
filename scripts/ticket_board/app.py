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

try:
    from ticket_store_io import _ticket_number, atomic_write_json, reserve_next_ticket_id
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.ticket_store_io import _ticket_number, atomic_write_json, reserve_next_ticket_id

STORE_DIR_DEFAULT = Path("~/.claude/pgu-tickets").expanduser()
ASSET_DIR_DEFAULT = Path("~/.claude/pgu-tickets-assets").expanduser()
FRAME_DIR_DEFAULT = Path("/tmp/pgu-frames")
REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
COMMIT_GIT_DIR_DEFAULT = Path("/data/git/pgu.git")
STORE_BACKEND_DEFAULT = os.environ.get("TICKET_BOARD_STORE_BACKEND", "json")
POSTGRES_DSN_DEFAULT = os.environ.get("TICKET_BOARD_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "agent", "director", "research")
LEGACY_ASSIGNEE_ALIASES = {"ui": "app"}
LEGACY_STATE_ALIASES = {"open": "analysis"}
STATES = ("backlog", "analysis", "ready", "in_progress", "audit", "eric_review", "director_review", "done", "cancelled")
TERMINAL_STATES = {"done", "cancelled"}
LEGAL_STATE_TRANSITIONS = {
    "backlog": {"analysis", "ready"},
    "analysis": {"ready", "backlog", "cancelled"},
    "ready": {"in_progress", "analysis", "backlog", "cancelled"},
    "in_progress": {"audit", "ready", "analysis", "backlog", "cancelled"},
    "audit": {"eric_review", "director_review", "analysis", "backlog", "cancelled"},
    "eric_review": {"director_review", "audit", "analysis", "backlog", "cancelled"},
    "director_review": {"done", "analysis", "backlog", "cancelled"},
    "done": {"analysis", "backlog"},
    "cancelled": {"analysis", "backlog"},
}
ACTIVE_STATES = tuple(state for state in STATES if state not in TERMINAL_STATES)
REOPEN_RESET_TARGET_STATES = {"open", "backlog", "analysis", "ready", "in_progress"}
REVIEWED_STATES = {"director_review", "audit", "eric_review"}
RESET_REVIEW_ARTIFACT_SOURCE_STATES = REVIEWED_STATES | TERMINAL_STATES
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class TicketBoardApp:
    def __init__(
        self,
        store_dir: Path = STORE_DIR_DEFAULT,
        frame_dir: Path = FRAME_DIR_DEFAULT,
        asset_dir: Path = ASSET_DIR_DEFAULT,
        repo_root: Path = REPO_ROOT_DEFAULT,
        commit_git_dir: Path = COMMIT_GIT_DIR_DEFAULT,
        store_backend: str = STORE_BACKEND_DEFAULT,
        database_url: str = POSTGRES_DSN_DEFAULT,
    ) -> None:
        self.store_dir = store_dir.expanduser().resolve()
        self.frame_dir = frame_dir.resolve()
        self.asset_dir = asset_dir.expanduser().resolve()
        self.repo_root = repo_root.resolve()
        self.commit_git_dir = commit_git_dir.resolve()
        self.store_backend = self._validate_store_backend(store_backend)
        self.database_url = database_url
        if self.store_backend == "json":
            self.store_dir.mkdir(parents=True, exist_ok=True)
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
            "store_path": str(self.store_dir),
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def store_signature(self) -> tuple[tuple[object, ...], ...]:
        if self.store_backend == "postgres":
            return self._pg_store_signature()
        signature: list[tuple[str, int, int]] = []
        for path in sorted(self.store_dir.glob("PGU-*.json")):
            stat = path.stat()
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def list_tickets(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        if self.store_backend == "postgres":
            try:
                return self._pg_list_tickets(), []
            except Exception as exc:  # noqa: BLE001
                return [], [{"file": "postgres", "error": str(exc)}]

        tickets: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for path in sorted(self.store_dir.glob("PGU-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                ticket = self._validate_ticket(payload, path)
                if self._payload_needs_state_migration(payload):
                    self._atomic_write(path, self._serialize_ticket(ticket))
                tickets.append(ticket)
            except Exception as exc:  # noqa: BLE001
                errors.append({"file": path.name, "error": str(exc)})
        tickets.sort(
            key=lambda ticket: (ticket["state"] not in TERMINAL_STATES, ticket["updated"], ticket["id"]),
            reverse=True,
        )
        return tickets, errors

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
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
    ) -> dict[str, Any]:
        return self.create_ticket_record(
            title=title,
            body=body,
            screenshot=screenshot,
            screenshots=screenshots,
            assignee=assignee,
            state="analysis",
            blocked_by=blocked_by,
            parent_id="",
            implementation="",
            audit_prompt="",
            audit_signoff=False,
            needs_eric_signoff=needs_eric_signoff,
            eric_signoff=False,
            comments=[],
            blocked_reason=blocked_reason,
            commit_hash="",
            commit_exempt=False,
        )

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
        needs_eric_signoff: bool,
        eric_signoff: bool,
        comments: list[dict[str, Any]],
        parent_id: str = "",
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
    ) -> dict[str, Any]:
        if self.store_backend == "postgres":
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
                needs_eric_signoff=needs_eric_signoff,
                eric_signoff=eric_signoff,
                comments=comments,
                parent_id=parent_id,
                blocked_reason=blocked_reason,
                commit_hash=commit_hash,
                commit_exempt=commit_exempt,
                created=created,
                updated=updated,
            )

        title = self._require_text(title, "title").strip()
        assignee = self._validate_assignee(assignee)
        state = self._validate_state(state)
        with reserve_next_ticket_id(self.store_dir) as ticket_id:
            blocked_by = self._validate_blocked_by(blocked_by or [], ticket_id)
            parent_id = self._validate_parent_id(parent_id, ticket_id)
            implementation = self._require_plain_string(implementation, "implementation")
            audit_prompt = self._require_plain_string(audit_prompt, "audit_prompt")
            blocked_reason = self._require_plain_string(blocked_reason, "blocked_reason")
            normalized_comments = self._validate_comments(comments)
            self._enforce_blocked_reason_rule(blocked_by, blocked_reason)
            screenshot_paths = self._materialize_attachments(
                screenshots if screenshots is not None else screenshot,
                ticket_id,
            )
            created_ts = self._require_text(created, "created") if created is not None else iso_now()
            updated_ts = self._require_text(updated, "updated") if updated is not None else created_ts
            ticket = {
                "id": ticket_id,
                "title": title,
                "body": body.strip(),
                "assignee": assignee,
                "state": state,
                "blocked_by": blocked_by,
                "parent_id": parent_id,
                "blocked_reason": blocked_reason,
                "implementation": implementation,
                "audit_prompt": audit_prompt,
                "audit_signoff": bool(audit_signoff),
                "needs_eric_signoff": bool(needs_eric_signoff),
                "eric_signoff": bool(eric_signoff),
                "commit_hash": self._validate_commit_hash(commit_hash),
                "commit_exempt": bool(commit_exempt),
                "created": created_ts,
                "updated": updated_ts,
                "comments": normalized_comments,
            }
            self._set_screenshot_fields(ticket, self._build_screenshot_entries(screenshot_paths))
            self._normalize_signoff_state(ticket)
            self._enforce_workflow_rules(ticket)
            self._enforce_initial_state_rules(ticket)
            if ticket["state"] == "done":
                self._enforce_done_requirements(ticket)
            if ticket["state"] == "cancelled":
                self._enforce_cancel_requirements(
                    previous_state=None,
                    ticket=ticket,
                    has_reason_comment=bool(normalized_comments),
                )
            self._atomic_write(self.store_dir / f"{ticket_id}.json", self._serialize_ticket(ticket))
        return ticket

    def update_ticket(self, ticket_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if self.store_backend == "postgres":
            return self._pg_update_ticket(ticket_id, patch)

        path = self.store_dir / f"{ticket_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        current = self._validate_ticket(json.loads(path.read_text(encoding="utf-8")), path)
        previous_state = current["state"]
        has_reason_comment = False
        if "state" in patch:
            current["state"] = self._validate_state(str(patch["state"]))
        if "title" in patch:
            current["title"] = self._require_text(patch["title"], "title").strip()
        if "assignee" in patch:
            current["assignee"] = self._validate_assignee(str(patch["assignee"]))
        if "blocked_by" in patch:
            current["blocked_by"] = self._validate_blocked_by(patch["blocked_by"], ticket_id)
        if "parent_id" in patch:
            current["parent_id"] = self._validate_parent_id(patch["parent_id"], ticket_id)
        if "blocked_reason" in patch:
            current["blocked_reason"] = self._require_plain_string(patch["blocked_reason"], "blocked_reason")
        if "screenshots" in patch or "screenshot" in patch:
            screenshot_paths = self._materialize_attachments(
                patch["screenshots"] if "screenshots" in patch else patch["screenshot"],
                ticket_id,
                current_paths=current.get("screenshots", []),
            )
            self._set_screenshot_fields(current, self._build_screenshot_entries(screenshot_paths))
        if "implementation" in patch:
            current["implementation"] = self._require_plain_string(patch["implementation"], "implementation")
        if "audit_prompt" in patch:
            current["audit_prompt"] = self._require_plain_string(patch["audit_prompt"], "audit_prompt")
        if "needs_eric_signoff" in patch:
            current["needs_eric_signoff"] = bool(patch["needs_eric_signoff"])
        if "audit_signoff" in patch:
            current["audit_signoff"] = bool(patch["audit_signoff"])
        if "eric_signoff" in patch:
            current["eric_signoff"] = bool(patch["eric_signoff"])
        if "commit_hash" in patch:
            current["commit_hash"] = self._validate_commit_hash(patch["commit_hash"])
        if "commit_exempt" in patch:
            current["commit_exempt"] = bool(patch["commit_exempt"])
        if "comment" in patch:
            comment = patch["comment"]
            who = str(comment.get("who", "")).strip()
            text = str(comment.get("text", "")).strip()
            if not who or not text:
                raise ValueError("comment requires non-empty who and text")
            current["comments"].append({"who": who, "text": text, "ts": iso_now()})
            has_reason_comment = True
        if "blocked_by" in patch or "blocked_reason" in patch:
            self._enforce_blocked_reason_rule(current["blocked_by"], current["blocked_reason"])
        self._normalize_signoff_state(current)
        self._enforce_transition_rules(previous_state, current, has_reason_comment=has_reason_comment)
        self._enforce_workflow_rules(current)
        if current["state"] == "done" and previous_state != "done":
            self._enforce_done_requirements(current)
        current["updated"] = iso_now()
        self._atomic_write(path, self._serialize_ticket(current))
        return current

    def merge_tickets(self, source_ticket_id: str, target_ticket_id: str, *, actor: str) -> dict[str, dict[str, Any]]:
        if self.store_backend == "postgres":
            raise ValueError("ticket merge is not available on the postgres backend yet")

        actor_normalized = str(actor).strip().lower()
        if actor_normalized != "director":
            raise ValueError("ticket merge requires actor=director")

        source_id = str(source_ticket_id).strip().upper()
        target_id = str(target_ticket_id).strip().upper()
        if not source_id or not target_id:
            raise ValueError("merge requires both source and target ticket IDs")
        if source_id == target_id:
            raise ValueError("cannot merge a ticket into itself")

        source_path = self.store_dir / f"{source_id}.json"
        target_path = self.store_dir / f"{target_id}.json"
        if not source_path.is_file():
            raise FileNotFoundError(f"ticket not found: {source_id}")
        if not target_path.is_file():
            raise FileNotFoundError(f"ticket not found: {target_id}")

        source = self._validate_ticket(json.loads(source_path.read_text(encoding="utf-8")), source_path)
        target = self._validate_ticket(json.loads(target_path.read_text(encoding="utf-8")), target_path)

        now = iso_now()
        target["comments"].extend(
            {
                "who": comment["who"],
                "text": f"[merged from {source_id}] {comment['text']}",
                "ts": comment["ts"],
            }
            for comment in source["comments"]
        )
        target["comments"].append(
            {
                "who": "director",
                "text": f"Merged in {source_id}: {source['title']}",
                "ts": now,
            }
        )
        target["screenshots"] = self._unique_paths([*target.get("screenshots", []), *source.get("screenshots", [])])
        self._set_screenshot_fields(target, self._build_screenshot_entries(target["screenshots"]))
        target["updated"] = now

        source["state"] = "done"
        source["commit_exempt"] = True
        source["comments"].append(
            {
                "who": "director",
                "text": f"Merged into {target_id}",
                "ts": now,
            }
        )
        source["updated"] = now

        self._atomic_write(target_path, self._serialize_ticket(target))
        self._atomic_write(source_path, self._serialize_ticket(source))
        return {"source": source, "target": target}

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        if not ticket_id:
            raise FileNotFoundError("ticket not found")
        if self.store_backend == "postgres":
            return self._pg_get_ticket(ticket_id)
        path = self.store_dir / f"{ticket_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        return self._validate_ticket(json.loads(path.read_text(encoding="utf-8")), path)

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

        if self.store_backend == "postgres":
            before_ids = {str(row[0]) for row in before_signature if row}
            before_max = max((_ticket_number(ticket_id) for ticket_id in before_ids), default=0)
            created_number = _ticket_number(ticket_id)
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

        before_names = {str(name) for name, *_ in before_signature}
        before_max = max((_ticket_number(Path(name).stem) for name in before_names), default=0)
        created_number = _ticket_number(ticket_id)
        if created_number <= before_max:
            raise ValueError(f"create returned non-new ticket id: {ticket_id}")
        if f"{ticket_id}.json" in before_names:
            raise ValueError(f"create collided with existing ticket id: {ticket_id}")

        persisted_path = self.store_dir / f"{ticket_id}.json"
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

        after_signature = self.store_signature()
        if after_signature == before_signature:
            raise ValueError(f"ticket create did not change the store: {ticket_id}")
        return after_signature

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        atomic_write_json(path, payload)

    def _validate_store_backend(self, raw: str) -> str:
        backend = str(raw or "json").strip().lower()
        if backend not in {"json", "postgres"}:
            raise ValueError(f"invalid ticket store backend: {raw}")
        return backend

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
    (SELECT count(*)::int FROM ticket_board.ticket_attachments a WHERE a.ticket_id = t.id) AS attachment_count
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
            )
            for row in rows
        )

    def _pg_select_ticket_rows(self, conn: Any, ticket_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE t.id = %s" if ticket_id is not None else ""
        params = (ticket_id,) if ticket_id is not None else ()
        return conn.execute(
            f"""
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
    t.needs_eric_signoff,
    t.eric_signoff,
    t.commit_hash,
    t.commit_exempt,
    t.created_text,
    t.updated_text,
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
            "needs_eric_signoff": bool(row["needs_eric_signoff"]),
            "eric_signoff": bool(row["eric_signoff"]),
            "commit_hash": str(row["commit_hash"] or ""),
            "commit_exempt": bool(row["commit_exempt"]),
            "created": self._require_text(row["created_text"], "created"),
            "updated": self._require_text(row["updated_text"], "updated"),
            "comments": self._validate_comments(comments),
        }
        self._set_screenshot_fields(ticket, self._build_screenshot_entries(list(screenshots)))
        return ticket

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
        needs_eric_signoff: bool,
        eric_signoff: bool,
        comments: list[dict[str, Any]],
        parent_id: str = "",
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
    ) -> dict[str, Any]:
        title = self._require_text(title, "title").strip()
        assignee = self._validate_assignee(assignee)
        state = self._validate_state(state)
        if screenshot not in (None, "", "null") or screenshots not in (None, [], ""):
            raise ValueError("postgres function API does not support attachment writes yet")
        if any(value is not None for value in (created, updated)):
            raise ValueError("postgres function API does not support caller-supplied timestamps")
        implementation = self._require_plain_string(implementation, "implementation")
        audit_prompt = self._require_plain_string(audit_prompt, "audit_prompt")
        if implementation.strip():
            raise ValueError("postgres function API does not support implementation writes yet")
        if audit_prompt.strip():
            raise ValueError("postgres function API does not support audit_prompt writes yet")
        if audit_signoff or needs_eric_signoff or eric_signoff:
            raise ValueError("postgres function API does not support initial signoff fields yet")
        if commit_hash or commit_exempt:
            raise ValueError("postgres function API does not support initial commit fields yet")
        normalized_comments = self._validate_comments(comments)
        blocked_by = self._validate_blocked_by(blocked_by or [], "PGU-0")
        blocked_reason = self._require_plain_string(blocked_reason, "blocked_reason")
        self._enforce_blocked_reason_rule(blocked_by, blocked_reason)

        with self._pg_connect() as conn:
            with conn.transaction():
                parent_id = "" if parent_id in (None, "", "null") else str(parent_id).strip().upper()
                if parent_id:
                    ticket_id = self._pg_call_scalar(
                        conn,
                        "SELECT ticket_board.file_bug(%s, %s, %s) AS id;",
                        (title, body.strip(), parent_id),
                    )
                else:
                    ticket_id = self._pg_call_scalar(
                        conn,
                        "SELECT ticket_board.create_ticket(%s, %s) AS id;",
                        (title, body.strip()),
                    )
                if assignee != "unassigned" or state != "analysis":
                    self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                if blocked_by:
                    self._pg_call(conn, "SELECT ticket_board.set_blockers(%s, %s, %s);", (ticket_id, blocked_by, blocked_reason))
                for comment in normalized_comments:
                    self._pg_call(conn, "SELECT ticket_board.add_comment(%s, %s);", (ticket_id, comment["text"]))
                return self._pg_get_ticket(ticket_id, conn)

    def _pg_update_ticket(self, ticket_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                current = self._pg_get_ticket(ticket_id, conn)
                self._pg_reject_unsupported_patch_fields(patch)
                if "blocked_by" in patch or "blocked_reason" in patch:
                    blocked_by = self._validate_blocked_by(patch.get("blocked_by", current["blocked_by"]), ticket_id)
                    blocked_reason = self._require_plain_string(
                        patch.get("blocked_reason", current["blocked_reason"]),
                        "blocked_reason",
                    )
                    self._enforce_blocked_reason_rule(blocked_by, blocked_reason)
                    self._pg_call(conn, "SELECT ticket_board.set_blockers(%s, %s, %s);", (ticket_id, blocked_by, blocked_reason))

                comment_text = ""
                if "comment" in patch:
                    comment = patch["comment"]
                    who = str(comment.get("who", "")).strip()
                    comment_text = str(comment.get("text", "")).strip()
                    if not who or not comment_text:
                        raise ValueError("comment requires non-empty who and text")

                state = self._validate_state(str(patch["state"])) if "state" in patch else current["state"]
                assignee = self._validate_assignee(str(patch["assignee"])) if "assignee" in patch else current["assignee"]
                commit_hash = str(patch.get("commit_hash", current.get("commit_hash", "")) or "").strip()
                if "commit_hash" in patch and state not in {"audit", "done"}:
                    raise ValueError("postgres function API only accepts commit_hash when submitting to audit or marking done")

                if "audit_signoff" in patch:
                    if not bool(patch["audit_signoff"]):
                        raise ValueError("postgres function API only supports setting audit_signoff true")
                    self._pg_call(conn, "SELECT ticket_board.audit_sign_off(%s);", (ticket_id,))
                if "eric_signoff" in patch:
                    if not bool(patch["eric_signoff"]):
                        raise ValueError("postgres function API only supports setting eric_signoff true")
                    self._pg_call(conn, "SELECT ticket_board.eric_sign_off(%s);", (ticket_id,))

                if "state" in patch:
                    if state == "in_progress":
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

    def _pg_call(self, conn: Any, sql: str, params: tuple[Any, ...]) -> None:
        conn.execute(sql, params)

    def _pg_call_scalar(self, conn: Any, sql: str, params: tuple[Any, ...]) -> str:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ValueError("postgres function returned no row")
        value = next(iter(row.values()))
        return str(value)

    def _pg_reject_unsupported_patch_fields(self, patch: dict[str, Any]) -> None:
        unsupported = {
            "title",
            "parent_id",
            "screenshots",
            "screenshot",
            "implementation",
            "audit_prompt",
            "needs_eric_signoff",
            "commit_exempt",
        }
        present = sorted(field for field in unsupported if field in patch)
        if present:
            joined = ", ".join(present)
            raise ValueError(f"postgres function API does not support direct updates for: {joined}")

    def _pg_validate_parent_id(self, conn: Any, raw: Any, ticket_id: str) -> str:
        if raw in (None, "", "null"):
            return ""
        if not isinstance(raw, str):
            raise ValueError("parent_id must be a ticket ID string")
        parent_id = raw.strip().upper()
        if not parent_id:
            return ""
        if not parent_id.startswith("PGU-") or not parent_id[4:].isdigit():
            raise ValueError(f"invalid parent_id ticket id: {raw}")
        if parent_id == ticket_id:
            raise ValueError("ticket cannot parent_id itself")

        seen = {ticket_id}
        current_parent_id = parent_id
        while current_parent_id:
            if current_parent_id in seen:
                raise ValueError(f"parent_id would create a cycle through {current_parent_id}")
            seen.add(current_parent_id)
            row = conn.execute(
                "SELECT parent_id FROM ticket_board.tickets WHERE id = %s;",
                (current_parent_id,),
            ).fetchone()
            if row is None:
                if current_parent_id == parent_id:
                    raise ValueError(f"parent_id ticket not found: {parent_id}")
                return parent_id
            current_parent_id = str(row["parent_id"] or "").strip().upper()
        return parent_id

    def _pg_enforce_initial_state_rules(self, conn: Any, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "ready":
            self._enforce_ready_requirements(ticket)
        elif ticket["state"] == "in_progress":
            self._pg_enforce_in_progress_requirements(conn, ticket, previous_state=None)

    def _pg_enforce_in_progress_requirements(
        self,
        conn: Any,
        ticket: dict[str, Any],
        *,
        previous_state: str | None,
    ) -> None:
        if previous_state == "analysis":
            raise ValueError("analysis tickets must move through ready before entering in_progress")
        if ticket["assignee"] == "unassigned":
            raise ValueError("assignee must not be unassigned before a ticket can enter in_progress")
        if not ticket["implementation"].strip():
            raise ValueError("implementation must be non-empty before a ticket can enter in_progress")
        conflicting_ticket_id = self._pg_find_other_in_progress_ticket(conn, ticket)
        if conflicting_ticket_id is not None:
            raise ValueError(
                f"{ticket['assignee']} already has an in-progress ticket {conflicting_ticket_id}; finish or move it first"
            )

    def _pg_find_other_in_progress_ticket(self, conn: Any, ticket: dict[str, Any]) -> str | None:
        assignee = ticket["assignee"]
        if assignee == "unassigned":
            return None
        current_root_id = self._pg_ticket_cluster_root_id(
            conn,
            ticket_id=ticket["id"],
            parent_id=ticket.get("parent_id", ""),
        )
        rows = conn.execute(
            """
SELECT id, parent_id
FROM ticket_board.tickets
WHERE id <> %s AND state = 'in_progress' AND assignee = %s
ORDER BY ticket_number;
""",
            (ticket["id"], assignee),
        ).fetchall()
        for row in rows:
            other_root_id = self._pg_ticket_cluster_root_id(conn, ticket_id=row["id"], parent_id=row["parent_id"])
            if other_root_id != current_root_id:
                return str(row["id"])
        return None

    def _pg_ticket_cluster_root_id(self, conn: Any, *, ticket_id: str, parent_id: Any) -> str:
        current_root_id = ticket_id
        if not isinstance(parent_id, str):
            return current_root_id
        current_parent_id = parent_id.strip().upper()
        seen = {ticket_id}
        while current_parent_id:
            if current_parent_id in seen:
                break
            seen.add(current_parent_id)
            current_root_id = current_parent_id
            row = conn.execute("SELECT parent_id FROM ticket_board.tickets WHERE id = %s;", (current_parent_id,)).fetchone()
            if row is None:
                break
            current_parent_id = str(row["parent_id"] or "").strip().upper()
        return current_root_id

    def _validate_ticket(self, payload: dict[str, Any], path: Path) -> dict[str, Any]:
        ticket_id = str(payload.get("id", path.stem))
        if not ticket_id.startswith("PGU-"):
            raise ValueError("id must look like PGU-N")
        screenshot_entries = self._validate_stored_screenshots(payload.get("screenshots"), payload.get("screenshot"))
        ticket = {
            "id": ticket_id,
            "title": self._require_text(payload.get("title"), "title"),
            "body": self._require_body(payload.get("body")),
            "assignee": self._validate_assignee(str(payload.get("assignee", "unassigned"))),
            "state": self._validate_state(str(payload.get("state", "analysis"))),
            "blocked_by": self._validate_blocked_by(payload.get("blocked_by", []), ticket_id),
            "parent_id": self._validate_parent_id(payload.get("parent_id", ""), ticket_id),
            "blocked_reason": self._require_plain_string(payload.get("blocked_reason", ""), "blocked_reason"),
            "implementation": self._require_plain_string(payload.get("implementation", ""), "implementation"),
            "audit_prompt": self._require_plain_string(payload.get("audit_prompt", ""), "audit_prompt"),
            "audit_signoff": bool(payload.get("audit_signoff", False)),
            "needs_eric_signoff": bool(payload.get("needs_eric_signoff", False)),
            "eric_signoff": bool(payload.get("eric_signoff", False)),
            "commit_hash": self._validate_commit_hash(payload.get("commit_hash", "")),
            "commit_exempt": bool(payload.get("commit_exempt", False)),
            "created": self._require_text(payload.get("created"), "created"),
            "updated": self._require_text(payload.get("updated"), "updated"),
            "comments": self._validate_comments(payload.get("comments", [])),
        }
        self._set_screenshot_fields(ticket, screenshot_entries)
        self._normalize_signoff_state(ticket)
        self._enforce_workflow_rules(ticket)
        return ticket

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

    def _validate_parent_id(self, raw: Any, ticket_id: str) -> str:
        if raw in (None, "", "null"):
            return ""
        if not isinstance(raw, str):
            raise ValueError("parent_id must be a ticket ID string")
        parent_id = raw.strip().upper()
        if not parent_id:
            return ""
        if not parent_id.startswith("PGU-") or not parent_id[4:].isdigit():
            raise ValueError(f"invalid parent_id ticket id: {raw}")
        if parent_id == ticket_id:
            raise ValueError("ticket cannot parent_id itself")
        parent_path = self.store_dir / f"{parent_id}.json"
        if not parent_path.is_file():
            raise ValueError(f"parent_id ticket not found: {parent_id}")
        self._assert_parent_link_acyclic(ticket_id, parent_id)
        return parent_id

    def _assert_parent_link_acyclic(self, ticket_id: str, parent_id: str) -> None:
        seen = {ticket_id}
        current_parent_id = parent_id
        while current_parent_id:
            if current_parent_id in seen:
                raise ValueError(f"parent_id would create a cycle through {current_parent_id}")
            seen.add(current_parent_id)
            current_path = self.store_dir / f"{current_parent_id}.json"
            if not current_path.is_file():
                return
            try:
                payload = json.loads(current_path.read_text(encoding="utf-8"))
            except Exception:
                return
            raw_next = payload.get("parent_id", "")
            if raw_next in (None, "", "null"):
                return
            if not isinstance(raw_next, str):
                raise ValueError(f"parent ticket {current_parent_id} has invalid parent_id")
            next_parent_id = raw_next.strip().upper()
            if not next_parent_id:
                return
            if not next_parent_id.startswith("PGU-") or not next_parent_id[4:].isdigit():
                raise ValueError(f"parent ticket {current_parent_id} has invalid parent_id {raw_next!r}")
            current_parent_id = next_parent_id

    def _normalize_signoff_state(self, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "eric_review" and not ticket["needs_eric_signoff"]:
            ticket["state"] = "audit"
            ticket["audit_signoff"] = False
        if not ticket["needs_eric_signoff"]:
            ticket["eric_signoff"] = False
        if ticket["state"] == "eric_review" and ticket["eric_signoff"]:
            ticket["state"] = "director_review"
            ticket["assignee"] = "director"
        if ticket["state"] == "audit" and ticket["audit_signoff"]:
            ticket["state"] = "eric_review" if ticket["needs_eric_signoff"] else "director_review"
            ticket["assignee"] = "director"

    def _enforce_blocked_reason_rule(self, blocked_by: list[str], blocked_reason: str) -> None:
        if blocked_by and not blocked_reason.strip():
            raise ValueError("blocked_reason must be non-empty when blocked_by is set")

    def _enforce_workflow_rules(self, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "eric_review" and not ticket["audit_signoff"]:
            raise ValueError("audit_signoff must be true before a ticket can enter eric_review")
        if ticket["state"] == "director_review" and ticket["needs_eric_signoff"] and not ticket["eric_signoff"]:
            raise ValueError("eric_signoff must be true before a ticket can enter director_review")

    def _enforce_transition_rules(
        self,
        previous_state: str,
        ticket: dict[str, Any],
        *,
        has_reason_comment: bool = False,
    ) -> None:
        if previous_state in RESET_REVIEW_ARTIFACT_SOURCE_STATES and ticket["state"] in REOPEN_RESET_TARGET_STATES:
            ticket["audit_signoff"] = False
            ticket["commit_hash"] = ""
        if previous_state != ticket["state"]:
            self._enforce_legal_state_transition(previous_state, ticket["state"])
        if previous_state == "audit" and ticket["state"] == "analysis":
            ticket["assignee"] = "unassigned"
        if previous_state != "cancelled" and ticket["state"] == "cancelled":
            self._enforce_cancel_requirements(
                previous_state=previous_state,
                ticket=ticket,
                has_reason_comment=has_reason_comment,
            )
        if previous_state == "analysis" and ticket["state"] == "in_progress":
            raise ValueError("analysis tickets must move through ready before entering in_progress")
        if previous_state in {"open", "backlog", "analysis"} and ticket["state"] == "ready":
            self._enforce_ready_requirements(ticket)
        if previous_state != "in_progress" and ticket["state"] == "in_progress":
            self._enforce_in_progress_requirements(ticket, previous_state=previous_state)
        if previous_state != "director_review" and ticket["state"] == "director_review" and not ticket["audit_signoff"]:
            raise ValueError("audit_signoff must be true before a ticket can enter director_review")
        if previous_state not in {"audit", "eric_review", "director_review"} and ticket["state"] == "director_review":
            raise ValueError("tickets must pass through audit before entering director_review")
        if previous_state == "audit" and ticket["state"] == "director_review" and ticket["needs_eric_signoff"]:
            raise ValueError("tickets requiring Eric signoff must pass through eric_review before entering director_review")
        if previous_state == "audit" and ticket["state"] in {"eric_review", "director_review", "done"} and not ticket["audit_signoff"]:
            raise ValueError("audit_signoff must be true before a ticket can leave audit")
        if (
            previous_state == "eric_review"
            and ticket["state"] == "director_review"
            and ticket["needs_eric_signoff"]
            and not ticket["eric_signoff"]
        ):
            raise ValueError("eric_signoff must be true before a ticket can leave eric_review")
        if ticket["state"] == "done" and previous_state not in {"done", "director_review"}:
            raise ValueError("tickets can only enter done from director_review")

    def _enforce_legal_state_transition(self, previous_state: str, next_state: str) -> None:
        previous_state = LEGACY_STATE_ALIASES.get(previous_state, previous_state)
        next_state = LEGACY_STATE_ALIASES.get(next_state, next_state)
        allowed_next_states = LEGAL_STATE_TRANSITIONS.get(previous_state, set())
        if next_state not in allowed_next_states:
            raise ValueError(f"illegal state transition: {previous_state} -> {next_state}")

    def _enforce_done_requirements(self, ticket: dict[str, Any]) -> None:
        if ticket.get("commit_exempt"):
            return
        if not ticket.get("commit_hash"):
            raise ValueError("commit_hash is required before a ticket can enter done")
        if not self._commit_is_on_main(ticket["commit_hash"]):
            raise ValueError(f"commit_hash {ticket['commit_hash']} is not on main - merge the branch first")

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

    def _enforce_initial_state_rules(self, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "ready":
            self._enforce_ready_requirements(ticket)
        elif ticket["state"] == "in_progress":
            self._enforce_in_progress_requirements(ticket, previous_state=None)

    def _enforce_cancel_requirements(
        self,
        *,
        previous_state: str | None,
        ticket: dict[str, Any],
        has_reason_comment: bool,
    ) -> None:
        if previous_state is not None and previous_state not in ACTIVE_STATES:
            raise ValueError("only active tickets can be cancelled")
        if not has_reason_comment:
            raise ValueError("cancelling a ticket requires a non-empty comment explaining why")

    def _enforce_ready_requirements(self, ticket: dict[str, Any]) -> None:
        if ticket["assignee"] == "unassigned":
            raise ValueError("assignee must not be unassigned before a ticket can enter ready")
        if not ticket["implementation"].strip():
            raise ValueError("implementation must be non-empty before a ticket can enter ready")

    def _enforce_in_progress_requirements(self, ticket: dict[str, Any], *, previous_state: str | None) -> None:
        if previous_state == "analysis":
            raise ValueError("analysis tickets must move through ready before entering in_progress")
        if ticket["assignee"] == "unassigned":
            raise ValueError("assignee must not be unassigned before a ticket can enter in_progress")
        if not ticket["implementation"].strip():
            raise ValueError("implementation must be non-empty before a ticket can enter in_progress")
        conflicting_ticket_id = self._find_other_in_progress_ticket(ticket)
        if conflicting_ticket_id is not None:
            raise ValueError(
                f"{ticket['assignee']} already has an in-progress ticket {conflicting_ticket_id}; finish or move it first"
            )

    def _find_other_in_progress_ticket(self, ticket: dict[str, Any]) -> str | None:
        assignee = ticket["assignee"]
        exclude_ticket_id = ticket["id"]
        if assignee == "unassigned":
            return None
        current_root_id = self._ticket_cluster_root_id(ticket_id=exclude_ticket_id, parent_id=ticket.get("parent_id", ""))
        for path in sorted(self.store_dir.glob("PGU-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            ticket_id = str(payload.get("id", path.stem))
            if ticket_id == exclude_ticket_id:
                continue
            try:
                state = self._validate_state(str(payload.get("state", "analysis")))
                ticket_assignee = self._validate_assignee(str(payload.get("assignee", "unassigned")))
            except ValueError:
                continue
            if state != "in_progress" or ticket_assignee != assignee:
                continue
            other_root_id = self._ticket_cluster_root_id(
                ticket_id=ticket_id,
                parent_id=payload.get("parent_id", ""),
            )
            if other_root_id == current_root_id:
                continue
            if state == "in_progress" and ticket_assignee == assignee:
                return ticket_id
        return None

    def _ticket_cluster_root_id(self, *, ticket_id: str, parent_id: Any) -> str:
        current_root_id = ticket_id
        if not isinstance(parent_id, str):
            return current_root_id
        current_parent_id = parent_id.strip().upper()
        seen = {ticket_id}
        while current_parent_id:
            if current_parent_id in seen:
                break
            seen.add(current_parent_id)
            current_root_id = current_parent_id
            current_path = self.store_dir / f"{current_parent_id}.json"
            if not current_path.is_file():
                break
            try:
                payload = json.loads(current_path.read_text(encoding="utf-8"))
            except Exception:
                break
            raw_next_parent = payload.get("parent_id", "")
            if not isinstance(raw_next_parent, str):
                break
            next_parent_id = raw_next_parent.strip().upper()
            if not next_parent_id:
                break
            if not next_parent_id.startswith("PGU-") or not next_parent_id[4:].isdigit():
                break
            current_parent_id = next_parent_id
        return current_root_id

    def _path_in_allowed_image_dirs(self, path: Path) -> bool:
        return self.frame_dir in path.parents or self.asset_dir in path.parents

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
            if normalized in normalized_current:
                screenshot_paths.append(normalized_current[normalized])
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

    def _serialize_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in ticket.items()
            if key not in {"screenshot_available", "screenshots_info"}
        }

    def _validate_state(self, state: str) -> str:
        state = LEGACY_STATE_ALIASES.get(state, state)
        if state not in STATES:
            raise ValueError(f"invalid state: {state}")
        return state

    def _payload_needs_state_migration(self, payload: dict[str, Any]) -> bool:
        raw_state = payload.get("state")
        return isinstance(raw_state, str) and raw_state == "open"

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
