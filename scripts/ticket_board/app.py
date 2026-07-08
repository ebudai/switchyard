"""JSON-backed ticket-board storage and validation."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE_DIR_DEFAULT = Path("~/.claude/pgu-tickets").expanduser()
FRAME_DIR_DEFAULT = Path("/tmp/pgu-frames")
ASSIGNEES = ("unassigned", "main", "ui", "perf", "ops", "audit")
STATES = ("open", "in_progress", "audit", "eric_review", "done")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class TicketBoardApp:
    def __init__(self, store_dir: Path = STORE_DIR_DEFAULT, frame_dir: Path = FRAME_DIR_DEFAULT) -> None:
        self.store_dir = store_dir.expanduser().resolve()
        self.frame_dir = frame_dir.resolve()
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict[str, object]:
        tickets, errors = self.list_tickets()
        return {
            "tickets": tickets,
            "errors": errors,
            "states": list(STATES),
            "assignees": list(ASSIGNEES),
            "screenshots": self.list_screenshots(),
            "store_path": str(self.store_dir),
            "frame_dir": str(self.frame_dir),
            "refreshed_at": iso_now(),
        }

    def list_tickets(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        tickets: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for path in sorted(self.store_dir.glob("PGU-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                ticket = self._validate_ticket(payload, path)
                tickets.append(ticket)
            except Exception as exc:  # noqa: BLE001
                errors.append({"file": path.name, "error": str(exc)})
        tickets.sort(key=lambda ticket: (ticket["state"] != "done", ticket["updated"], ticket["id"]), reverse=True)
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
        if self.frame_dir not in path.parents:
            raise FileNotFoundError(f"screenshot path escapes {self.frame_dir}")
        if not path.is_file():
            raise FileNotFoundError(f"screenshot not found: {path}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise FileNotFoundError(f"unsupported image type: {path.name}")
        return path

    def create_ticket(
        self,
        *,
        title: str,
        body: str,
        screenshot: str | None,
        assignee: str,
        needs_eric_signoff: bool,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        self._validate_assignee(assignee)
        screenshot_path = self._normalize_screenshot(screenshot)
        ticket_id, handle = self._reserve_ticket_file()
        now = iso_now()
        ticket: dict[str, Any] = {
            "id": ticket_id,
            "title": title,
            "body": body.strip(),
            "screenshot": screenshot_path,
            "assignee": assignee,
            "state": "open",
            "audit_signoff": False,
            "needs_eric_signoff": bool(needs_eric_signoff),
            "eric_signoff": False,
            "created": now,
            "updated": now,
            "comments": [],
        }
        with handle:
            json.dump(ticket, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return ticket

    def update_ticket(self, ticket_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        path = self.store_dir / f"{ticket_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        current = self._validate_ticket(json.loads(path.read_text(encoding="utf-8")), path)
        if "state" in patch:
            current["state"] = self._validate_state(str(patch["state"]))
        if "assignee" in patch:
            current["assignee"] = self._validate_assignee(str(patch["assignee"]))
        if "screenshot" in patch:
            current["screenshot"] = self._normalize_screenshot(patch["screenshot"])
        if "needs_eric_signoff" in patch:
            current["needs_eric_signoff"] = bool(patch["needs_eric_signoff"])
        if "audit_signoff" in patch:
            current["audit_signoff"] = bool(patch["audit_signoff"])
        if "eric_signoff" in patch:
            current["eric_signoff"] = bool(patch["eric_signoff"])
        if "comment" in patch:
            comment = patch["comment"]
            who = str(comment.get("who", "")).strip()
            text = str(comment.get("text", "")).strip()
            if not who or not text:
                raise ValueError("comment requires non-empty who and text")
            current["comments"].append({"who": who, "text": text, "ts": iso_now()})
        self._normalize_signoff_state(current)
        current["updated"] = iso_now()
        self._atomic_write(path, current)
        return current

    def _reserve_ticket_file(self) -> tuple[str, Any]:
        existing = [self._ticket_number(path.stem) for path in self.store_dir.glob("PGU-*.json")]
        next_number = max(existing, default=0) + 1
        while True:
            ticket_id = f"PGU-{next_number}"
            path = self.store_dir / f"{ticket_id}.json"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                next_number += 1
                continue
            else:
                return ticket_id, os.fdopen(fd, "w", encoding="utf-8")

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)

    def _validate_ticket(self, payload: dict[str, Any], path: Path) -> dict[str, Any]:
        ticket_id = str(payload.get("id", path.stem))
        if not ticket_id.startswith("PGU-"):
            raise ValueError("id must look like PGU-N")
        ticket = {
            "id": ticket_id,
            "title": self._require_text(payload.get("title"), "title"),
            "body": self._require_body(payload.get("body")),
            "screenshot": self._normalize_screenshot(payload.get("screenshot")),
            "assignee": self._validate_assignee(str(payload.get("assignee", "unassigned"))),
            "state": self._validate_state(str(payload.get("state", "open"))),
            "audit_signoff": bool(payload.get("audit_signoff", False)),
            "needs_eric_signoff": bool(payload.get("needs_eric_signoff", False)),
            "eric_signoff": bool(payload.get("eric_signoff", False)),
            "created": self._require_text(payload.get("created"), "created"),
            "updated": self._require_text(payload.get("updated"), "updated"),
            "comments": self._validate_comments(payload.get("comments", [])),
        }
        self._normalize_signoff_state(ticket)
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

    def _normalize_signoff_state(self, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "eric_review":
            ticket["needs_eric_signoff"] = True
        if not ticket["needs_eric_signoff"]:
            ticket["eric_signoff"] = False
            if ticket["state"] == "eric_review":
                ticket["state"] = "audit"

    def _normalize_screenshot(self, raw: Any) -> str | None:
        if raw in (None, "", "null"):
            return None
        if not isinstance(raw, str):
            raise ValueError("screenshot must be a path string or null")
        return str(self.resolve_image(raw))

    def _ticket_number(self, stem: str) -> int:
        try:
            return int(stem.split("-", 1)[1])
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid ticket id stem: {stem}") from exc

    def _validate_state(self, state: str) -> str:
        if state not in STATES:
            raise ValueError(f"invalid state: {state}")
        return state

    def _validate_assignee(self, assignee: str) -> str:
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
