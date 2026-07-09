"""JSON-backed ticket-board storage and validation."""

from __future__ import annotations

import json
import os
import tempfile
import time
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

STORE_DIR_DEFAULT = Path("~/.claude/pgu-tickets").expanduser()
ASSET_DIR_DEFAULT = Path("~/.claude/pgu-tickets-assets").expanduser()
FRAME_DIR_DEFAULT = Path("/tmp/pgu-frames")
ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "agent", "director")
LEGACY_ASSIGNEE_ALIASES = {"ui": "app"}
STATES = ("open", "in_progress", "director_review", "audit", "eric_review", "done")
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
    ) -> None:
        self.store_dir = store_dir.expanduser().resolve()
        self.frame_dir = frame_dir.resolve()
        self.asset_dir = asset_dir.expanduser().resolve()
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
            "store_path": str(self.store_dir),
            "frame_dir": str(self.frame_dir),
            "asset_dir": str(self.asset_dir),
            "refreshed_at": iso_now(),
        }

    def store_signature(self) -> tuple[tuple[str, int, int], ...]:
        signature: list[tuple[str, int, int]] = []
        for path in sorted(self.store_dir.glob("PGU-*.json")):
            stat = path.stat()
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

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
        assignee: str,
        needs_eric_signoff: bool,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise ValueError("title must not be empty")
        self._validate_assignee(assignee)
        ticket_id, handle = self._reserve_ticket_file()
        with handle:
            try:
                screenshot_path = self._materialize_attachment(screenshot, ticket_id)
                now = iso_now()
                ticket: dict[str, Any] = {
                    "id": ticket_id,
                    "title": title,
                    "body": body.strip(),
                    "screenshot": screenshot_path,
                    "screenshot_available": screenshot_path is not None,
                    "assignee": assignee,
                    "state": "open",
                    "implementation": "",
                    "audit_prompt": "",
                    "audit_signoff": False,
                    "needs_eric_signoff": bool(needs_eric_signoff),
                    "eric_signoff": False,
                    "created": now,
                    "updated": now,
                    "comments": [],
                }
                json.dump(self._serialize_ticket(ticket), handle, indent=2, sort_keys=True)
                handle.write("\n")
            except Exception:
                (self.store_dir / f"{ticket_id}.json").unlink(missing_ok=True)
                raise
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
            current["screenshot"] = self._materialize_attachment(
                patch["screenshot"],
                ticket_id,
                current_path=current.get("screenshot"),
            )
            current["screenshot_available"] = current["screenshot"] is not None
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
        if "comment" in patch:
            comment = patch["comment"]
            who = str(comment.get("who", "")).strip()
            text = str(comment.get("text", "")).strip()
            if not who or not text:
                raise ValueError("comment requires non-empty who and text")
            current["comments"].append({"who": who, "text": text, "ts": iso_now()})
        self._normalize_signoff_state(current)
        current["updated"] = iso_now()
        self._atomic_write(path, self._serialize_ticket(current))
        return current

    def _reserve_ticket_file(self) -> tuple[str, Any]:
        existing = [self._ticket_number(path.stem) for path in self.store_dir.glob("PGU-*.json")]
        next_number = max(existing, default=0) + 1
        while True:
            ticket_id = f"PGU-{next_number}"
            path = self.store_dir / f"{ticket_id}.json"
            try:
                # Reserve the next ticket filename atomically so concurrent creators
                # cannot claim the same PGU-N and overwrite each other.
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
        screenshot_path, screenshot_available = self._validate_stored_screenshot(payload.get("screenshot"))
        ticket = {
            "id": ticket_id,
            "title": self._require_text(payload.get("title"), "title"),
            "body": self._require_body(payload.get("body")),
            "screenshot": screenshot_path,
            "screenshot_available": screenshot_available,
            "assignee": self._validate_assignee(str(payload.get("assignee", "unassigned"))),
            "state": self._validate_state(str(payload.get("state", "open"))),
            "implementation": self._require_plain_string(payload.get("implementation", ""), "implementation"),
            "audit_prompt": self._require_plain_string(payload.get("audit_prompt", ""), "audit_prompt"),
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

    def _path_in_allowed_image_dirs(self, path: Path) -> bool:
        return self.frame_dir in path.parents or self.asset_dir in path.parents

    def _validate_stored_screenshot(self, raw: Any) -> tuple[str | None, bool]:
        if raw in (None, "", "null"):
            return None, False
        if not isinstance(raw, str):
            raise ValueError("screenshot must be a path string or null")
        path = Path(raw).expanduser().resolve()
        if not self._path_in_allowed_image_dirs(path):
            raise ValueError(f"screenshot path escapes allowed asset roots: {path}")
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"unsupported image type: {path.name}")
        return str(path), path.is_file()

    def _materialize_attachment(self, raw: Any, ticket_id: str, current_path: str | None = None) -> str | None:
        if raw in (None, "", "null"):
            return None
        if not isinstance(raw, str):
            raise ValueError("screenshot must be a path string or null")
        source = self.resolve_image(raw)
        normalized = str(source)
        if current_path and normalized == current_path:
            return current_path
        destination = self.asset_dir / f"{ticket_id}-{time.time_ns()}.png"
        with Image.open(source) as image:
            image.load()
            output = image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")
            output.save(destination, format="PNG")
        if source.parent == self.asset_dir and source.name.startswith("upload_"):
            source.unlink(missing_ok=True)
        return str(destination.resolve())

    def _serialize_ticket(self, ticket: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in ticket.items() if key != "screenshot_available"}

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
