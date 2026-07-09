"""JSON-backed ticket-board storage and validation."""

from __future__ import annotations

import json
import os
import subprocess
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
REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
COMMIT_GIT_DIR_DEFAULT = Path("/data/git/pgu.git")
ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "agent", "director", "research")
LEGACY_ASSIGNEE_ALIASES = {"ui": "app"}
LEGACY_STATE_ALIASES = {"open": "analysis"}
STATES = ("analysis", "ready", "in_progress", "director_review", "audit", "eric_review", "done")
REOPEN_RESET_TARGET_STATES = {"open", "analysis", "ready", "in_progress"}
REVIEWED_STATES = {"director_review", "audit", "eric_review"}
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
    ) -> None:
        self.store_dir = store_dir.expanduser().resolve()
        self.frame_dir = frame_dir.resolve()
        self.asset_dir = asset_dir.expanduser().resolve()
        self.repo_root = repo_root.resolve()
        self.commit_git_dir = commit_git_dir.resolve()
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
                if self._payload_needs_state_migration(payload):
                    self._atomic_write(path, self._serialize_ticket(ticket))
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
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
    ) -> dict[str, Any]:
        title = self._require_text(title, "title").strip()
        assignee = self._validate_assignee(assignee)
        state = self._validate_state(state)
        ticket_id, handle = self._reserve_ticket_file()
        blocked_by = self._validate_blocked_by(blocked_by or [], ticket_id)
        implementation = self._require_plain_string(implementation, "implementation")
        audit_prompt = self._require_plain_string(audit_prompt, "audit_prompt")
        blocked_reason = self._require_plain_string(blocked_reason, "blocked_reason")
        normalized_comments = self._validate_comments(comments)
        self._enforce_blocked_reason_rule(blocked_by, blocked_reason)
        with handle:
            try:
                screenshot_paths = self._materialize_attachments(
                    screenshots if screenshots is not None else screenshot,
                    ticket_id,
                )
                created_ts = self._require_text(created, "created") if created is not None else iso_now()
                updated_ts = self._require_text(updated, "updated") if updated is not None else created_ts
                ticket: dict[str, Any] = {
                    "id": ticket_id,
                    "title": title,
                    "body": body.strip(),
                    "assignee": assignee,
                    "state": state,
                    "blocked_by": blocked_by,
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
        previous_state = current["state"]
        if "state" in patch:
            current["state"] = self._validate_state(str(patch["state"]))
        if "title" in patch:
            current["title"] = self._require_text(patch["title"], "title").strip()
        if "assignee" in patch:
            current["assignee"] = self._validate_assignee(str(patch["assignee"]))
        if "blocked_by" in patch:
            current["blocked_by"] = self._validate_blocked_by(patch["blocked_by"], ticket_id)
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
        if "blocked_by" in patch or "blocked_reason" in patch:
            self._enforce_blocked_reason_rule(current["blocked_by"], current["blocked_reason"])
        self._normalize_signoff_state(current)
        self._enforce_transition_rules(previous_state, current)
        self._enforce_workflow_rules(current)
        if current["state"] == "done" and previous_state != "done":
            self._enforce_done_requirements(current)
        current["updated"] = iso_now()
        self._atomic_write(path, self._serialize_ticket(current))
        return current

    def merge_tickets(self, source_ticket_id: str, target_ticket_id: str, *, actor: str) -> dict[str, dict[str, Any]]:
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
        screenshot_entries = self._validate_stored_screenshots(payload.get("screenshots"), payload.get("screenshot"))
        ticket = {
            "id": ticket_id,
            "title": self._require_text(payload.get("title"), "title"),
            "body": self._require_body(payload.get("body")),
            "assignee": self._validate_assignee(str(payload.get("assignee", "unassigned"))),
            "state": self._validate_state(str(payload.get("state", "analysis"))),
            "blocked_by": self._validate_blocked_by(payload.get("blocked_by", []), ticket_id),
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

    def _normalize_signoff_state(self, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "eric_review":
            ticket["needs_eric_signoff"] = True
        if not ticket["needs_eric_signoff"]:
            ticket["eric_signoff"] = False
            if ticket["state"] == "eric_review":
                ticket["state"] = "audit"

    def _enforce_blocked_reason_rule(self, blocked_by: list[str], blocked_reason: str) -> None:
        if blocked_by and not blocked_reason.strip():
            raise ValueError("blocked_reason must be non-empty when blocked_by is set")

    def _enforce_workflow_rules(self, ticket: dict[str, Any]) -> None:
        if ticket["state"] == "eric_review" and not ticket["audit_signoff"]:
            raise ValueError("audit_signoff must be true before a ticket can enter eric_review")

    def _enforce_transition_rules(self, previous_state: str, ticket: dict[str, Any]) -> None:
        if previous_state in REVIEWED_STATES and ticket["state"] in REOPEN_RESET_TARGET_STATES:
            ticket["audit_signoff"] = False
            ticket["commit_hash"] = ""
        if previous_state == "analysis" and ticket["state"] == "in_progress":
            raise ValueError("analysis tickets must move through ready before entering in_progress")
        if previous_state in {"open", "analysis"} and ticket["state"] == "ready":
            self._enforce_ready_requirements(ticket)
        if ticket["state"] == "in_progress":
            self._enforce_in_progress_requirements(ticket, previous_state=previous_state)
        if previous_state == "director_review" and ticket["state"] == "audit" and not ticket["audit_prompt"].strip():
            raise ValueError("audit_prompt must be non-empty before a ticket can enter audit")
        if previous_state == "audit" and ticket["state"] in {"eric_review", "done"} and not ticket["audit_signoff"]:
            raise ValueError("audit_signoff must be true before a ticket can leave audit")

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
        conflicting_ticket_id = self._find_other_in_progress_ticket(ticket["assignee"], exclude_ticket_id=ticket["id"])
        if conflicting_ticket_id is not None:
            raise ValueError(
                f"{ticket['assignee']} already has an in-progress ticket {conflicting_ticket_id}; finish or move it first"
            )

    def _find_other_in_progress_ticket(self, assignee: str, *, exclude_ticket_id: str) -> str | None:
        if assignee == "unassigned":
            return None
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
            if state == "in_progress" and ticket_assignee == assignee:
                return ticket_id
        return None

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

    def _ticket_number(self, stem: str) -> int:
        try:
            return int(stem.split("-", 1)[1])
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid ticket id stem: {stem}") from exc

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
