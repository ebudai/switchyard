"""Reusable Playwright harness for ticket-board browser UI tests."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.app import (
    ASSIGNEES,
    CALLER_ROLES,
    IMAGE_EXTENSIONS,
    STATES,
    format_timestamp,
    iso_now,
    ticket_number,
    upload_set_slug,
    uploaded_filename_slug,
)
from scripts.ticket_board.server import DirectorNotifier, TicketBoardServer

WORKFLOW_COLUMNS = [
    {"key": "draft", "label": "Draft"},
    {"key": "backlog", "label": "Backlog"},
    {"key": "analysis", "label": "Triage"},
    {"key": "in_progress", "label": "Implementation"},
    {"key": "inspection", "label": "Inspection"},
    {"key": "audit", "label": "Audit"},
    {"key": "dat", "label": "DAT"},
    {"key": "user_review", "label": "UAT"},
    {"key": "director_review", "label": "Final Sign-Off"},
    {"key": "done", "label": "Done"},
    {"key": "cancelled", "label": "Cancelled"},
]


def load_playwright() -> object:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        venv_roots = [
            ROOT / ".venv" / "lib",
            Path("/home/agent/Projects/pgu/.venv/lib"),
            Path("/tmp/pgu-playwright-venv/lib"),
        ]
        candidates = []
        for repo_venv in venv_roots:
            candidates.extend(sorted(repo_venv.glob("python*/site-packages")))
        if not candidates:
            raise
        sys.path.insert(0, str(candidates[-1]))
        from playwright.sync_api import sync_playwright
    return sync_playwright


class QuietNotifier(DirectorNotifier):
    def __init__(self) -> None:
        super().__init__(sender=lambda payload: None, batch_window_seconds=0.01)


def ticket_payload(
    ticket_id: str,
    title: str,
    *,
    body: str = "",
    state: str = "analysis",
    assignee: str = "unassigned",
    created_offset_minutes: int = 0,
    blocked_by: list[str] | None = None,
    blocked_reason: str = "",
    comments: list[dict[str, Any]] | None = None,
    screenshots: list[str] | None = None,
    parent_id: str = "",
    implementation: str = "",
    audit_prompt: str = "",
    audit_signoff: bool = False,
    needs_inspection: bool = False,
    inspector_signoff: bool = False,
    needs_audit: bool = True,
    needs_user_signoff: bool = False,
    user_signoff: bool = False,
    regression: bool = False,
    manually_controlled: bool = False,
    commit_hash: str = "",
    commit_exempt: bool = False,
    active_work_highlight: bool = False,
) -> dict[str, Any]:
    created = (datetime(2026, 7, 17, tzinfo=timezone.utc) + timedelta(minutes=created_offset_minutes)).isoformat()
    screenshot_paths = screenshots or []
    return {
        "id": ticket_id,
        "title": title,
        "body": body,
        "assignee": assignee,
        "state": state,
        "blocked_by": blocked_by or [],
        "blockers": [],
        "blocked_reason": blocked_reason,
        "implementation": implementation,
        "audit_prompt": audit_prompt,
        "audit_signoff": audit_signoff,
        "needs_audit": needs_audit,
        "needs_inspection": needs_inspection,
        "inspector_signoff": inspector_signoff,
        "needs_user_signoff": needs_user_signoff,
        "user_signoff": user_signoff,
        "manually_controlled": manually_controlled,
        "parked": state == "backlog",
        "commit_hash": commit_hash,
        "commit_exempt": commit_exempt,
        "regression": regression,
        "parent_id": parent_id,
        "created": created,
        "updated": created,
        "comments": comments or [],
        "screenshots": screenshot_paths,
        "screenshots_info": [{"path": path, "available": Path(path).is_file()} for path in screenshot_paths],
        "screenshot": screenshot_paths[0] if screenshot_paths else None,
        "screenshot_available": bool(screenshot_paths),
        "active_work_highlight": active_work_highlight,
        "active_work_owner_role": assignee if active_work_highlight else "",
    }


class InMemoryTicketBoardApp:
    store_backend = "memory"

    def __init__(self, tickets: list[dict[str, Any]], *, frame_dir: Path, asset_dir: Path) -> None:
        self.frame_dir = frame_dir.resolve()
        self.asset_dir = asset_dir.resolve()
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.tickets: dict[str, dict[str, Any]] = {ticket["id"]: copy.deepcopy(ticket) for ticket in tickets}
        self.upload_count = 0
        self.action_log: list[tuple[str, str | None, dict[str, Any]]] = []
        self.operation_log: list[tuple[str, str, str | None, dict[str, Any]]] = []
        self._refresh_blockers()

    def snapshot(self) -> dict[str, object]:
        self._refresh_blockers()
        return {
            "tickets": [copy.deepcopy(ticket) for ticket in sorted(self.tickets.values(), key=lambda item: ticket_number(item["id"]))],
            "errors": [],
            "states": list(STATES),
            "columns": copy.deepcopy(WORKFLOW_COLUMNS),
            "assignees": list(ASSIGNEES),
            "caller_roles": list(CALLER_ROLES),
            "screenshots": self.list_screenshots(),
            "store_backend": self.store_backend,
            "store_path": "in-memory test board",
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def store_signature(self) -> tuple[tuple[object, ...], ...]:
        self._refresh_blockers()
        rows = []
        for ticket in sorted(self.tickets.values(), key=lambda item: ticket_number(item["id"])):
            rows.append(
                (
                    ticket["id"],
                    ticket["updated"],
                    ticket["state"],
                    ticket["assignee"],
                    tuple(ticket.get("blocked_by") or []),
                    ticket.get("blocked_reason", ""),
                    len(ticket.get("comments") or []),
                    len(ticket.get("screenshots") or []),
                    bool(ticket.get("audit_signoff")),
                    bool(ticket.get("user_signoff")),
                )
            )
        return tuple(rows)

    def list_screenshots(self) -> list[dict[str, str]]:
        paths = sorted(self.frame_dir.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True)
        return [{"path": str(path.resolve()), "name": path.name, "modified": format_timestamp(path)} for path in paths]

    def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket = self.tickets.get(str(ticket_id).strip().upper())
        if ticket is None:
            raise FileNotFoundError(ticket_id)
        self._refresh_blockers()
        return copy.deepcopy(ticket)

    def verify_created_ticket_persisted(
        self,
        created: dict[str, object],
        before_signature: tuple[tuple[object, ...], ...],
    ) -> tuple[tuple[object, ...], ...]:
        ticket_id = str(created.get("id", "")).strip().upper()
        if ticket_id not in self.tickets:
            raise ValueError(f"created ticket did not persist: {ticket_id}")
        after_signature = self.store_signature()
        if after_signature == before_signature:
            raise ValueError(f"ticket create did not change the store: {ticket_id}")
        return after_signature

    def create_ticket(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None,
        screenshots: list[str] | None = None,
        assignee: str,
        needs_user_signoff: bool,
        needs_audit: bool = True,
        needs_inspection: bool = False,
        regression: bool = False,
        blocked_by: list[str] | None = None,
        blocked_reason: str = "",
        state: str = "analysis",
        implementation: str = "",
        notification_source_role: str | None = None,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self.create_ticket_record(
            title=title,
            body=body,
            screenshot=screenshot,
            screenshots=screenshots,
            assignee=assignee,
            state=state,
            blocked_by=blocked_by,
            implementation=implementation,
            audit_prompt="",
            audit_signoff=False,
            needs_audit=needs_audit,
            needs_inspection=needs_inspection,
            inspector_signoff=False,
            needs_user_signoff=needs_user_signoff,
            user_signoff=False,
            regression=regression,
            comments=[],
            blocked_reason=blocked_reason,
            caller_role=caller_role,
        )

    def create_ticket_record(self, **kwargs: Any) -> dict[str, Any]:
        title = str(kwargs.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        next_number = max((ticket_number(ticket_id) for ticket_id in self.tickets), default=0) + 1
        ticket_id = f"PGU-{next_number}"
        screenshots = list(kwargs.get("screenshots") or [])
        if not screenshots and kwargs.get("screenshot"):
            screenshots = [str(kwargs["screenshot"])]
        created = str(kwargs.get("created") or iso_now())
        ticket = ticket_payload(
            ticket_id,
            title,
            body=str(kwargs.get("body", "")),
            state=str(kwargs.get("state", "analysis") or "analysis"),
            assignee=str(kwargs.get("assignee", "unassigned") or "unassigned"),
            blocked_by=[str(item).strip().upper() for item in kwargs.get("blocked_by") or [] if str(item).strip()],
            blocked_reason=str(kwargs.get("blocked_reason", "")),
            comments=copy.deepcopy(kwargs.get("comments") or []),
            screenshots=screenshots,
            parent_id=str(kwargs.get("parent_id", "")),
            implementation=str(kwargs.get("implementation", "")),
            audit_prompt=str(kwargs.get("audit_prompt", "")),
            audit_signoff=bool(kwargs.get("audit_signoff", False)),
            needs_audit=bool(kwargs.get("needs_audit", True)),
            needs_inspection=bool(kwargs.get("needs_inspection", False)),
            inspector_signoff=bool(kwargs.get("inspector_signoff", False)),
            needs_user_signoff=bool(kwargs.get("needs_user_signoff", False)),
            user_signoff=bool(kwargs.get("user_signoff", False)),
            regression=bool(kwargs.get("regression", False)),
            commit_hash=str(kwargs.get("commit_hash", "")),
            commit_exempt=bool(kwargs.get("commit_exempt", False)),
        )
        ticket["created"] = created
        ticket["updated"] = str(kwargs.get("updated") or created)
        self.tickets[ticket_id] = ticket
        self._refresh_blockers()
        return copy.deepcopy(ticket)

    def route_ticket(self, ticket_id: str, state: str, assignee: str, *, caller_role: str | None = None) -> dict[str, Any]:
        self.operation_log.append(("route", str(ticket_id).strip().upper(), caller_role, {"state": state, "assignee": assignee}))
        return self._mutate_ticket(ticket_id, {"state": state, "assignee": assignee}, caller_role=caller_role)

    def release_draft(self, ticket_id: str, *, caller_role: str) -> dict[str, Any]:
        self.operation_log.append(("release_draft", str(ticket_id).strip().upper(), caller_role, {}))
        return self._mutate_ticket(ticket_id, {"state": "analysis", "assignee": "unassigned"}, caller_role=caller_role)

    def force_move_ticket(
        self,
        ticket_id: str,
        state: str,
        assignee: str,
        *,
        suppress_notification: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        return self._mutate_ticket(ticket_id, {"state": state, "assignee": assignee}, caller_role=caller_role, force=True)

    def merge_tickets(self, source_ticket_id: str, target_ticket_id: str, *, actor: str) -> dict[str, dict[str, Any]]:
        source = self._ticket_ref(source_ticket_id)
        target = self._ticket_ref(target_ticket_id)
        target["comments"].extend(copy.deepcopy(source.get("comments") or []))
        target["screenshots"] = self._unique([*(target.get("screenshots") or []), *(source.get("screenshots") or [])])
        self._sync_attachment_fields(target)
        source["state"] = "done"
        source["parent_id"] = target["id"]
        source["commit_exempt"] = True
        self._touch(source)
        self._touch(target)
        self._refresh_blockers()
        return {"source": copy.deepcopy(source), "target": copy.deepcopy(target)}

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        return self._mutate_ticket(ticket_id, patch, caller_role=caller_role)

    def request_commit_exempt(self, ticket_id: str, reason: str, *, caller_role: str) -> dict[str, Any]:
        return self._mutate_ticket(ticket_id, {"commit_exempt": True, "comment": {"who": caller_role, "text": reason}}, caller_role=caller_role)

    def start_task(self, ticket_id: str, note: str = "", *, caller_role: str) -> dict[str, Any]:
        return self._mutate_ticket(ticket_id, {"active_work_highlight": True}, caller_role=caller_role)

    def complete_task(self, ticket_id: str, completion_note: str, *, caller_role: str) -> dict[str, Any]:
        updated = self._mutate_ticket(
            ticket_id,
            {"state": "director_review", "comment": {"who": caller_role, "text": completion_note}},
            caller_role=caller_role,
        )
        self._promote_queued_for(caller_role)
        return updated

    def set_awaiting_role(self, ticket_id: str, awaiting_role: str, *, caller_role: str) -> dict[str, Any]:
        return self._mutate_ticket(ticket_id, {"awaiting_role": awaiting_role}, caller_role=caller_role)

    def clear_awaiting_role(self, ticket_id: str, *, caller_role: str) -> dict[str, Any]:
        return self._mutate_ticket(ticket_id, {"awaiting_role": ""}, caller_role=caller_role)

    def save_uploaded_image(
        self,
        raw_bytes: bytes,
        *,
        upload_set: str = "",
        set_label: str = "",
        attempt_number: str = "",
        original_filename: str = "",
    ) -> dict[str, str]:
        if not raw_bytes:
            raise ValueError("uploaded image is empty")
        prefix = self._upload_filename_prefix(upload_set, set_label, attempt_number)
        self.upload_count += 1
        base_name = uploaded_filename_slug(original_filename) or f"upload_{self.upload_count}.png"
        if prefix:
            base_name = f"{prefix}__{base_name}"
        path = self._dedupe_asset_path(base_name)
        with Image.open(BytesIO(raw_bytes)) as image:
            image.load()
            output_format = self._image_save_format(path.suffix)
            if output_format == "JPEG" and image.mode not in ("RGB", "L"):
                output = image.convert("RGB")
            else:
                output = image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")
            output.save(path, format=output_format)
        return {"path": str(path.resolve()), "name": path.name, "modified": format_timestamp(path)}

    def crop_attachment(
        self,
        ticket_id: str,
        *,
        source_path: str,
        rect: dict[str, Any],
        feedback_number: int | None = None,
        set_label: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        ticket = self._ticket_ref(ticket_id)
        source = Path(source_path)
        if str(source) not in ticket.get("screenshots", []):
            raise ValueError("crop source must already be attached to the ticket")
        with Image.open(source) as image:
            image.load()
            crop_rect = {
                "x": int(round(float(rect["x"]))),
                "y": int(round(float(rect["y"]))),
                "w": int(round(float(rect["w"]))),
                "h": int(round(float(rect["h"]))),
            }
            crop = image.crop(
                (
                    crop_rect["x"],
                    crop_rect["y"],
                    crop_rect["x"] + crop_rect["w"],
                    crop_rect["y"] + crop_rect["h"],
                )
            )
            feedback = feedback_number or 1
            destination = self.asset_dir / (
                f"feedback-{feedback:03d}__crop-of-{source.stem}-"
                f"x{crop_rect['x']}-y{crop_rect['y']}-w{crop_rect['w']}-h{crop_rect['h']}.png"
            )
            crop.save(destination)
        metadata = {
            "kind": "crop",
            "source_path": str(source),
            "source_name": source.name,
            "rect": crop_rect,
            "feedback_number": feedback,
            "caption": f"crop of {source.name} @ {crop_rect['x']},{crop_rect['y']},{crop_rect['w']},{crop_rect['h']}",
        }
        ticket.setdefault("screenshots", []).append(str(destination))
        ticket.setdefault("screenshots_info", []).append({"path": str(destination), "available": True, "metadata": metadata})
        self._touch(ticket)
        self._sync_attachment_fields(ticket)
        return copy.deepcopy(ticket)

    def resolve_image(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser().resolve()
        if not (self.asset_dir == path.parent or self.frame_dir == path.parent or self.asset_dir in path.parents or self.frame_dir in path.parents):
            raise FileNotFoundError(f"screenshot path escapes allowed asset roots: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"screenshot not found: {path}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise FileNotFoundError(f"unsupported image type: {path.name}")
        return path

    def _dedupe_asset_path(self, filename: str) -> Path:
        candidate = self.asset_dir / filename
        if not candidate.exists():
            return candidate
        suffix = candidate.suffix
        stem = candidate.stem
        for index in range(2, 10000):
            candidate = self.asset_dir / f"{stem}-{index}{suffix}"
            if not candidate.exists():
                return candidate
        raise ValueError(f"could not allocate unique upload filename for {filename}")

    def _image_save_format(self, suffix: str) -> str:
        normalized = suffix.lower()
        if normalized in {".jpg", ".jpeg"}:
            return "JPEG"
        if normalized == ".webp":
            return "WEBP"
        return "PNG"

    def _upload_filename_prefix(self, upload_set: str, set_label: str, attempt_number: str) -> str:
        normalized_set = upload_set_slug(upload_set)
        label_slug = upload_set_slug(set_label)
        if normalized_set in {"", "ungrouped"}:
            return ""
        if normalized_set == "target":
            return "target"
        if normalized_set == "attempt":
            try:
                attempt = int(str(attempt_number).strip())
            except ValueError as exc:
                raise ValueError("attempt upload set requires an attempt number") from exc
            if attempt <= 0 or attempt > 999:
                raise ValueError("attempt upload set requires attempt number 1-999")
            prefix = f"attempt-{attempt:03d}"
            if label_slug:
                prefix = f"{prefix}-{label_slug}"
            return prefix
        if normalized_set == "feedback":
            try:
                feedback = int(str(attempt_number).strip())
            except ValueError as exc:
                raise ValueError("feedback upload set requires a feedback number") from exc
            if feedback <= 0 or feedback > 999:
                raise ValueError("feedback upload set requires feedback number 1-999")
            prefix = f"feedback-{feedback:03d}"
            if label_slug:
                prefix = f"{prefix}-{label_slug}"
            return prefix
        return label_slug or normalized_set

    def _ticket_ref(self, ticket_id: str) -> dict[str, Any]:
        normalized = str(ticket_id).strip().upper()
        if normalized not in self.tickets:
            raise FileNotFoundError(normalized)
        return self.tickets[normalized]

    def _mutate_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None, force: bool = False) -> dict[str, Any]:
        ticket = self._ticket_ref(ticket_id)
        self.action_log.append((ticket["id"], caller_role, copy.deepcopy(patch)))
        previous_state = ticket["state"]
        previous_assignee = ticket["assignee"]
        if "comment" in patch:
            comment = patch["comment"] or {}
            text = str(comment.get("text", "")).strip()
            if text:
                ticket.setdefault("comments", []).append(
                    {
                        "who": str(comment.get("who") or caller_role or "director"),
                        "text": text,
                        "ts": iso_now(),
                        "urgent": bool(comment.get("urgent", False)),
                    }
                )

        metadata_fields = {
            "title",
            "body",
            "parent_id",
            "implementation",
            "audit_prompt",
            "needs_inspection",
            "needs_user_signoff",
            "commit_exempt",
            "regression",
            "commit_hash",
            "audit_signoff",
            "inspector_signoff",
            "user_signoff",
            "manually_controlled",
            "active_work_highlight",
            "awaiting_role",
        }
        for field in metadata_fields:
            if field in patch:
                ticket[field] = patch[field]
        if "screenshots" in patch:
            ticket["screenshots"] = self._unique([str(path) for path in patch["screenshots"] or []])
            self._sync_attachment_fields(ticket)
        elif "screenshot" in patch:
            ticket["screenshots"] = [str(patch["screenshot"])] if patch["screenshot"] else []
            self._sync_attachment_fields(ticket)

        if "blocked_by" in patch:
            blocked_by = self._unique([str(item).strip().upper() for item in patch.get("blocked_by") or [] if str(item).strip()])
            if ticket["id"] in blocked_by:
                raise ValueError("ticket cannot block itself")
            for blocker_id in blocked_by:
                blocker = self.tickets.get(blocker_id)
                if blocker is None:
                    raise ValueError(f"blocker ticket not found: {blocker_id}")
                if blocker["state"] in {"done", "cancelled"}:
                    raise ValueError(f"terminal tickets cannot block other tickets: {blocker_id}")
            ticket["blocked_by"] = blocked_by
        if "blocked_reason" in patch:
            ticket["blocked_reason"] = str(patch["blocked_reason"])
        if ticket.get("blocked_by") and not str(ticket.get("blocked_reason", "")).strip():
            raise ValueError("blocked_reason must be non-empty when blocked_by is set")

        if patch.get("user_signoff") is True and ticket.get("state") == "user_review":
            ticket["state"] = "director_review"
            ticket["assignee"] = "director"

        if "state" in patch:
            next_state = str(patch["state"])
            next_assignee = str(patch.get("assignee", ticket.get("assignee", "unassigned")))
            if next_state == "in_progress" and not force and self._role_busy(next_assignee, excluding=ticket["id"]):
                ticket["state"] = "backlog"
                ticket["assignee"] = next_assignee
                ticket["parked"] = True
                ticket["active_work_highlight"] = False
            else:
                ticket["state"] = next_state
                ticket["assignee"] = next_assignee
                ticket["parked"] = next_state == "backlog"
                ticket["active_work_highlight"] = next_state == "in_progress"
                ticket["active_work_owner_role"] = next_assignee if next_state == "in_progress" else ""
        elif "assignee" in patch:
            ticket["assignee"] = str(patch["assignee"])

        if ticket["state"] == "cancelled" and not any(c["text"].strip() for c in ticket.get("comments", [])):
            raise ValueError("cancelling a ticket requires a non-empty comment explaining why")

        self._touch(ticket)
        self._refresh_blockers()
        if previous_state == "in_progress" and ticket["state"] != "in_progress":
            self._promote_queued_for(previous_assignee)
        return copy.deepcopy(ticket)

    def _role_busy(self, role: str, *, excluding: str = "") -> bool:
        normalized = str(role).strip().lower()
        return any(
            ticket["id"] != excluding and ticket["state"] == "in_progress" and ticket["assignee"] == normalized
            for ticket in self.tickets.values()
        )

    def _promote_queued_for(self, role: str) -> None:
        normalized = str(role).strip().lower()
        if not normalized or self._role_busy(normalized):
            return
        queued = sorted(
            (
                ticket
                for ticket in self.tickets.values()
                if ticket["state"] == "backlog" and ticket["assignee"] == normalized and not ticket.get("manually_controlled")
            ),
            key=lambda item: (item["created"], ticket_number(item["id"])),
        )
        if not queued:
            return
        ticket = queued[0]
        ticket["state"] = "in_progress"
        ticket["parked"] = False
        ticket["active_work_highlight"] = True
        ticket["active_work_owner_role"] = normalized
        self._touch(ticket)

    def _refresh_blockers(self) -> None:
        for ticket in self.tickets.values():
            blockers = []
            for blocker_id in ticket.get("blocked_by") or []:
                blocker = self.tickets.get(blocker_id)
                blockers.append({"id": blocker_id, "resolved": bool(blocker and blocker["state"] in {"done", "cancelled"})})
            ticket["blockers"] = blockers
            self._sync_attachment_fields(ticket)

    def _sync_attachment_fields(self, ticket: dict[str, Any]) -> None:
        metadata_by_path = {
            str(entry.get("path", "")): entry.get("metadata", {})
            for entry in ticket.get("screenshots_info") or []
            if isinstance(entry, dict)
        }
        paths = self._unique([str(path) for path in ticket.get("screenshots") or [] if str(path)])
        ticket["screenshots"] = paths
        ticket["screenshots_info"] = [
            {
                "path": path,
                "available": Path(path).is_file(),
                **({"metadata": metadata_by_path[path]} if metadata_by_path.get(path) else {}),
            }
            for path in paths
        ]
        ticket["screenshot"] = paths[0] if paths else None
        ticket["screenshot_available"] = bool(paths and Path(paths[0]).is_file())

    def _touch(self, ticket: dict[str, Any]) -> None:
        ticket["updated"] = iso_now()

    @staticmethod
    def _unique(items: list[str]) -> list[str]:
        unique = []
        seen = set()
        for item in items:
            if item and item not in seen:
                unique.append(item)
                seen.add(item)
        return unique


class BoardHarness(AbstractContextManager["BoardHarness"]):
    def __init__(self, tickets: list[dict[str, Any]]) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ticket-board-ui.")
        self.root = Path(self._tmp.name)
        self.frames = self.root / "frames"
        self.assets = self.root / "assets"
        self.app = InMemoryTicketBoardApp(tickets, frame_dir=self.frames, asset_dir=self.assets)
        self.server = TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=QuietNotifier())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "BoardHarness":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server.events.close()
        self._tmp.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/"

    def snapshot(self) -> dict[str, Any]:
        with urlopen(f"{self.url}api/board", timeout=10) as response:
            return json.load(response)

    def ticket(self, ticket_id: str) -> dict[str, Any]:
        with urlopen(f"{self.url}api/tickets/{ticket_id}", timeout=10) as response:
            return json.load(response)

    def make_png(self, name: str, color: tuple[int, int, int] = (80, 150, 210)) -> Path:
        path = self.root / name
        Image.new("RGB", (32, 32), color=color).save(path)
        return path


def wait_for_ticket_field(page: Any, base_url: str, ticket_id: str, expression: str, timeout: int = 5000) -> None:
    page.wait_for_function(
        """async ([baseUrl, ticketId, expr]) => {
          const response = await fetch(`${baseUrl}api/tickets/${ticketId}`, { cache: 'no-store' });
          if (!response.ok) return false;
          const ticket = await response.json();
          return Function('ticket', `return (${expr});`)(ticket);
        }""",
        arg=[base_url, ticket_id, expression],
        timeout=timeout,
    )


def create_clipboard_png_payload() -> str:
    return (
        "const canvas = document.createElement('canvas');"
        "canvas.width = 8; canvas.height = 8;"
        "const ctx = canvas.getContext('2d');"
        "ctx.fillStyle = '#66ccff'; ctx.fillRect(0, 0, 8, 8);"
        "canvas.toBlob((blob) => {"
        "  const file = new File([blob], 'pasted.png', { type: 'image/png' });"
        "  const item = { type: 'image/png', getAsFile: () => file };"
        "  const event = new Event('paste', { bubbles: true, cancelable: true });"
        "  Object.defineProperty(event, 'clipboardData', { value: { items: [item] } });"
        "  document.dispatchEvent(event);"
        "}, 'image/png');"
    )
