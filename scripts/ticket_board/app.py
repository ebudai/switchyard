"""Ticket-board storage and validation."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from io import BytesIO
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from .commit_repos import commit_git_dirs_for_project

ASSET_DIR_DEFAULT = Path("~/.claude/pgu-tickets-assets").expanduser()
FRAME_DIR_DEFAULT = Path("/tmp/pgu-frames")
REPO_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
POSTGRES_DSN_DEFAULT = os.environ.get("TICKET_BOARD_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
DEFAULT_ASSIGNEES = ("unassigned", "main", "app", "perf", "ops", "audit", "inspector", "agent", "director", "research", "user")
DEFAULT_CALLER_ROLES = ("director", "main", "app", "ops", "perf", "audit", "inspector", "research", "user")


def _role_list_from_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    roles: list[str] = []
    for item in raw.split(","):
        role = item.strip().lower()
        if not role:
            continue
        if role not in roles:
            roles.append(role)
    return tuple(roles) or default


#: Where a publication proves itself (SYRD-118). The privileged publisher writes
#: this namespace into the tenant's trusted commit cache only after it has
#: pushed and read the exact commit back from the public remote, so a ref here
#: is the board's own sight of a completed publication. `refs/heads/<ref>` in
#: the same repository is not: `switchyard-request-publication` creates that
#: locally when the ask is filed, and accepting it would prove only that
#: somebody asked.
PUBLISHED_REF_NAMESPACE = "refs/remotes/origin"
#: How long the board will wait for its own copy of the repository to catch up
#: with a commit somebody has just published. Short enough that a submission
#: does not hang on an unreachable forge, long enough for an ordinary fetch.
COMMIT_REFRESH_TIMEOUT_SECONDS = 30
#: Branch names this will hand to git. The board already refuses anything else
#: when the ask is filed; asked again here so no ref shape can become an
#: argument to the command that is supposed to be reading it.
PUBLISHABLE_REF = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*(?:/[0-9A-Za-z][0-9A-Za-z._-]*)*")


ASSIGNEES = _role_list_from_env("TICKET_BOARD_ASSIGNEES", DEFAULT_ASSIGNEES)
CALLER_ROLES = _role_list_from_env("TICKET_BOARD_CALLER_ROLES", DEFAULT_CALLER_ROLES)
LEGACY_ASSIGNEE_ALIASES = {"ui": "app"}
LEGACY_STATE_ALIASES = {"open": "analysis"}
STATES = (
    "draft",
    "backlog",
    "analysis",
    "in_progress",
    "inspection",
    "audit",
    "dat",
    "user_review",
    "director_review",
    "done",
    "cancelled",
)
TERMINAL_STATES = {"done", "cancelled"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
TICKET_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
TICKET_NUMBER_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-([0-9]+)$")
# SYRD-270: a blocker on another board, `<project>:<PREFIX>-<n>`. It never
# resolves by itself; only release_external_blocker removes it.
# SYRD-273: or a person, `operator:<name>`; `operator` is never a project.
EXTERNAL_BLOCKER_PATTERN = re.compile(r"^((?!operator:)[a-z][a-z0-9_]*:[A-Z][A-Z0-9]*-[0-9]+|operator:[a-z][a-z0-9_]*)$")


def project_slug(environ: dict[str, str] | os._Environ[str] = os.environ) -> str:
    return environ.get("TICKET_BOARD_PROJECT", "").strip() or environ.get("PGU_TICKET_BOARD_PROJECT", "").strip() or "pgu"


def project_name_for_project(
    project: str | None = None,
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> str:
    explicit = environ.get("TICKET_BOARD_PROJECT_NAME", "").strip() or environ.get("PGU_TICKET_BOARD_PROJECT_NAME", "").strip()
    if explicit:
        return explicit
    resolved_project = (project if project is not None else project_slug(environ)).strip()
    return "PGU" if resolved_project.lower() == "pgu" else resolved_project


def ticket_prefix_for_project(project: str | None = None, environ: dict[str, str] | os._Environ[str] = os.environ) -> str:
    explicit = environ.get("TICKET_BOARD_TICKET_PREFIX", "").strip() or environ.get("PGU_TICKET_BOARD_TICKET_PREFIX", "").strip()
    raw = explicit or (project if project is not None else project_slug(environ))
    return normalize_ticket_prefix(raw)


def normalize_ticket_prefix(raw: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "", raw).upper()
    if not normalized:
        normalized = "PGU"
    if normalized[0].isdigit():
        normalized = f"T{normalized}"
    return normalized


def valid_ticket_id(ticket_id: str) -> bool:
    return bool(TICKET_ID_PATTERN.fullmatch(str(ticket_id).strip().upper()))


def normalize_blocker_ref(raw: str) -> str:
    """A local id upper case; a qualified reference as `project:PREFIX-N` (as ticket_board.normalize_blocker_ref)."""
    value = str(raw).strip()
    if ":" in value and value.split(":", 1)[0].strip().lower() == "operator":
        return value.lower()
    if ":" in value:
        project, _, ticket = value.partition(":")
        return f"{project.strip().lower()}:{ticket.strip().upper()}"
    return value.upper()


def is_external_blocker(ref: str) -> bool:
    return bool(EXTERNAL_BLOCKER_PATTERN.fullmatch(str(ref)))


def ticket_id_sentinel(prefix: str) -> str:
    return f"{prefix}-0"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_timestamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def ticket_number(ticket_id: str) -> int:
    value = str(ticket_id).strip().upper()
    match = TICKET_NUMBER_PATTERN.fullmatch(value)
    if not match:
        return 0
    return int(match.group(1))


def upload_set_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    return slug[:80]


def uploaded_filename_slug(raw: str) -> str:
    name = Path(str(raw or "").replace("\\", "/")).name
    suffix = Path(name).suffix.lower()
    stem = name[: -len(suffix)] if suffix else name
    slug = re.sub(r"[^A-Za-z0-9]+", "-", stem.strip().lower()).strip("-")[:120]
    if not slug:
        return ""
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".png"
    return f"{slug}{suffix}"


def crop_filename_slug(raw: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
    return slug[:80] or "render"



#: What this board last published at the same ref. The publisher leases a
#: replacement candidate against exactly this value, so a rebuilt candidate can
#: move the public ref off what the board put there and off nothing else
#: (SYRD-119). Asked once, through one query, so that the row the publisher acts
#: on and the row a reader sees cannot be two different answers.
PREVIOUS_PUBLISHED_COMMIT_QUERY = (
    "SELECT coalesce((SELECT prior.commit_hash FROM ticket_board.publication_requests prior"
    " WHERE prior.ref = %s AND prior.state = 'published' AND prior.id <> %s"
    " ORDER BY prior.decided_at DESC NULLS LAST, prior.id DESC LIMIT 1), '')"
)


def _publication_row(row: Any) -> dict[str, Any]:
    """One publication request, as JSON the socket, CLI and UI all read."""
    if row is None:
        return {}
    record = dict(row)
    for key in ("requested_at", "decided_at"):
        value = record.get(key)
        record[key] = value.isoformat() if hasattr(value, "isoformat") else ("" if value is None else str(value))
    record["id"] = int(record.get("id") or 0)
    return record

def _publication_row_with_history(conn: Any, row: Any) -> dict[str, Any]:
    """A publication row, plus what this board last published at its ref.

    Computed for every path that returns a publication rather than only the
    listing the publisher happens to read today: a field that is present in one
    caller's JSON and absent from another's is the kind of difference that gets
    discovered by something failing at the far end (SYRD-119).
    """
    record = _publication_row(row)
    if not record:
        return record
    ref = str(record.get("ref") or "")
    if not ref:
        record["previous_published_commit"] = ""
        return record
    found = conn.execute(
        PREVIOUS_PUBLISHED_COMMIT_QUERY, (ref, int(record.get("id") or 0))
    ).fetchone()
    value = found[0] if not isinstance(found, dict) else next(iter(found.values()))
    record["previous_published_commit"] = str(value or "")
    return record


class TicketBoardApp:
    def __init__(
        self,
        frame_dir: Path = FRAME_DIR_DEFAULT,
        asset_dir: Path = ASSET_DIR_DEFAULT,
        repo_root: Path = REPO_ROOT_DEFAULT,
        commit_git_dir: Path | str | None = None,
        database_url: str = POSTGRES_DSN_DEFAULT,
        project: str | None = None,
        project_name: str | None = None,
        ticket_prefix: str | None = None,
    ) -> None:
        self.frame_dir = frame_dir.resolve()
        self.asset_dir = asset_dir.expanduser().resolve()
        self.repo_root = repo_root.resolve()
        if isinstance(commit_git_dir, str):
            commit_git_dirs = tuple(Path(item) for item in commit_git_dir.split(os.pathsep) if item.strip())
        elif commit_git_dir is not None:
            commit_git_dirs = (Path(commit_git_dir),)
        else:
            commit_git_dirs = commit_git_dirs_for_project()
        if not commit_git_dirs:
            raise ValueError("commit_hash verification repository list is empty")
        self.commit_git_dirs = tuple(path.expanduser().resolve(strict=False) for path in commit_git_dirs)
        self.commit_git_dir = self.commit_git_dirs[0]
        self.store_backend = "postgres"
        self.database_url = database_url
        self.project = (project or project_slug()).strip().lower() or "pgu"
        self.project_name = (project_name or project_name_for_project(self.project)).strip() or self.project
        self.ticket_prefix = ticket_prefix_for_project(self.project) if ticket_prefix is None else normalize_ticket_prefix(ticket_prefix)
        self._workflow_states_cache: tuple[str, ...] | None = None
        self.asset_dir.mkdir(parents=True, exist_ok=True)

    def workflow_configuration(self) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        from .workflow_config import read_configuration
        with self._pg_connect() as conn:
            return read_configuration(conn)

    def workflow_document(self) -> dict[str, Any]:
        with self._pg_connect() as conn:
            from .workflow_config import read_configuration
            cfg = read_configuration(conn)
            if cfg is None:
                return {"revision": 0, "document": None}
            row = conn.execute("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton").fetchone()
            return {"revision": row["revision"], "document": cfg}

    def workflow_roles(self) -> list[str]:
        cfg = self.workflow_configuration()
        return [r["name"] for r in cfg["roles"] if r["active"]] if cfg else list(CALLER_ROLES)

    def runtime_assignment_for_process(self, pid: int, start_time: int, uid: int) -> dict[str, Any] | None:
        """Resolve authority from the exact live process recorded by the launcher."""
        with self._pg_connect() as conn:
            row = conn.execute(
                """
SELECT a.role, a.runtime, a.actual_target, a.worktree, a.session_dir,
       a.process_pid, a.process_start_time, a.process_uid, a.generation
FROM ticket_board.role_runtime_assignments AS a
JOIN ticket_board.workflow_roles AS r ON r.name = a.role
WHERE a.process_pid = %s AND a.process_start_time = %s AND a.process_uid = %s
  AND (r.definition->>'active')::boolean
  AND r.definition->>'runtime' = a.runtime
  AND r.definition->>'target' = a.actual_target
""",
                (pid, start_time, uid),
            ).fetchone()
            return dict(row) if row else None

    def runtime_assignment(self, role: str) -> dict[str, Any] | None:
        with self._pg_connect() as conn:
            row = conn.execute(
                "SELECT * FROM ticket_board.role_runtime_assignments WHERE role = %s",
                (role.strip().lower(),),
            ).fetchone()
            return dict(row) if row else None

    def register_runtime_assignment(
        self,
        *,
        role: str,
        runtime: str,
        target: str,
        worktree: str,
        session_dir: str,
        process_pid: int,
        process_start_time: int,
        process_uid: int,
        expected_generation: int,
    ) -> dict[str, Any]:
        """Atomically publish routing data and the process that may wield it."""
        if not target.split(":", 1)[0].startswith(f"{self.project}-"):
            raise ValueError("runtime target belongs to another project")
        with self._pg_connect() as conn:
            row = conn.execute(
                """
SELECT * FROM ticket_board.register_role_runtime(
    %s::text, %s::text, %s::text, %s::text, %s::text,
    %s::bigint, %s::bigint, %s::bigint, %s::bigint
)
""",
                (
                    role, runtime, target, worktree, session_dir, process_pid,
                    process_start_time, process_uid, expected_generation,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("runtime registration returned no row")
            return dict(row)

    def runtime_targets(self) -> dict[str, dict[str, Any]]:
        """Current routing rows used by delivery and presentation consumers."""
        with self._pg_connect() as conn:
            rows = conn.execute(
                """
SELECT a.role, a.runtime, a.actual_target, a.worktree, a.session_dir,
       a.process_pid, a.process_start_time, a.process_uid, a.generation
FROM ticket_board.role_runtime_assignments AS a
JOIN ticket_board.workflow_roles AS r ON r.name=a.role
WHERE (r.definition->>'active')::boolean
  AND r.definition->>'runtime'=a.runtime
  AND r.definition->>'target'=a.actual_target
"""
            ).fetchall()
            return {str(row["role"]): dict(row) for row in rows}

    def apply_workflow(self, document: Any, *, expected_revision: int, dry_run: bool, caller_role: str) -> dict[str, Any]:
        from .workflow_config import validate
        if caller_role != "director":
            raise PermissionError("only director may configure workflow")
        cfg = validate(document, project=self.project)
        with self._pg_connect() as conn:
            self._pg_set_caller_role(conn, caller_role)
            row = conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb,%s) AS revision", (json.dumps(cfg), expected_revision)).fetchone()
            result = {"revision": row["revision"], "document": cfg, "dry_run": dry_run}
            if dry_run:
                conn.rollback()
            self._workflow_states_cache = None
            return result

    def set_workflow_flags(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
        with self._pg_connect() as conn:
            self._pg_set_caller_role(conn, caller_role)
            self._pg_call(conn, "SELECT ticket_board.set_declared_flags(%s,%s::jsonb)", (ticket_id,json.dumps(patch)))
            return self._pg_get_ticket(ticket_id,conn)

    def perform_workflow_action(self, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
        if set(payload) - {"target", "assignee", "commit_hash", "text", "reason"}:
            raise ValueError("unknown workflow action payload field")
        payload = dict(payload)
        if "commit_hash" in payload:
            payload["commit_hash"] = self._validate_commit_hash(payload["commit_hash"])
        with self._pg_connect() as conn:
            self._pg_set_caller_role(conn, caller_role)
            self._pg_call(conn, "SELECT ticket_board.perform_workflow_action(%s,%s,%s::jsonb)", (ticket_id, action, json.dumps(payload)))
            return self._pg_get_ticket(ticket_id, conn)

    def snapshot(self) -> dict[str, object]:
        self._workflow_states_cache = None
        tickets, errors = self.list_tickets()
        try:
            columns = self.workflow_columns()
        except Exception as exc:  # noqa: BLE001
            errors = [*errors, {"file": "postgres", "error": str(exc)}]
            columns = [{"key": state, "label": state} for state in STATES]
        states = [column["key"] for column in columns] or list(STATES)
        cfg = self.workflow_configuration()
        if cfg:
            from .workflow_config import available_transitions
            for ticket in tickets:
                ticket["workflow_actions"] = available_transitions(cfg, ticket)
        return {
            "project": self.project,
            "project_name": self.project_name,
            "ticket_prefix": self.ticket_prefix,
            "tickets": tickets,
            "errors": errors,
            "states": states,
            "columns": columns,
            "assignees": [r["name"] for r in cfg["roles"] if r["active"]] if cfg else list(ASSIGNEES),
            "caller_roles": [r["name"] for r in cfg["roles"] if r["active"] and r["name"] != "unassigned"] if cfg else list(CALLER_ROLES),
            "workflow": cfg,
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

    def workflow_columns(self) -> list[dict[str, str]]:
        return self._pg_workflow_columns()

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
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        prefix = self._upload_filename_prefix(upload_set, set_label, attempt_number)
        base_name = uploaded_filename_slug(original_filename) or f"upload_{time.time_ns()}.png"
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
        return {
            "path": str(path.resolve()),
            "name": path.name,
            "modified": format_timestamp(path),
        }

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
        ticket_id = str(ticket_id).strip().upper()
        ticket = self.get_ticket(ticket_id)
        source = self.resolve_image(source_path)
        normalized_source = str(source.resolve())
        attached_paths = {str(path) for path in ticket.get("screenshots", [])}
        if normalized_source not in attached_paths:
            raise ValueError("crop source must already be attached to the ticket")

        with Image.open(source) as image:
            image.load()
            source_width, source_height = image.size
            crop_rect = self._normalize_crop_rect(rect, source_width, source_height)
            cropped = image.crop(
                (
                    crop_rect["x"],
                    crop_rect["y"],
                    crop_rect["x"] + crop_rect["w"],
                    crop_rect["y"] + crop_rect["h"],
                )
            )
            if cropped.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                cropped = cropped.convert("RGBA")
            feedback = feedback_number or self._next_feedback_number(ticket)
            if feedback <= 0 or feedback > 999:
                raise ValueError("feedback crop requires feedback number 1-999")
            label_slug = upload_set_slug(set_label)
            prefix = f"feedback-{feedback:03d}"
            if label_slug:
                prefix = f"{prefix}-{label_slug}"
            source_slug = crop_filename_slug(source.stem)
            crop_suffix = f"x{crop_rect['x']}-y{crop_rect['y']}-w{crop_rect['w']}-h{crop_rect['h']}"
            destination = self._dedupe_asset_path(f"{prefix}__crop-of-{source_slug}-{crop_suffix}.png")
            cropped.save(destination, format="PNG")

        metadata = {
            "kind": "crop",
            "source_path": normalized_source,
            "source_name": source.name,
            "rect": crop_rect,
            "source_size": {"w": source_width, "h": source_height},
            "feedback_number": feedback,
            "caption": (
                f"crop of {source.name} @ "
                f"{crop_rect['x']},{crop_rect['y']},{crop_rect['w']},{crop_rect['h']}"
            ),
        }
        with self._pg_connect() as conn:
            self._pg_set_caller_role(conn, caller_role or "director")
            self._pg_call(
                conn,
                "SELECT ticket_board.append_ticket_attachment(%s, %s, %s::jsonb);",
                (ticket_id, str(destination.resolve()), json.dumps(metadata)),
            )
            return self._pg_get_ticket(ticket_id, conn)

    def _normalize_crop_rect(self, raw: dict[str, Any], image_width: int, image_height: int) -> dict[str, int]:
        def number(name: str) -> int:
            try:
                return int(round(float(raw.get(name))))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"crop rect requires numeric {name}") from exc

        x = number("x")
        y = number("y")
        w = number("w")
        h = number("h")
        if w <= 0 or h <= 0:
            raise ValueError("crop width and height must be positive")
        x = max(0, min(x, image_width - 1))
        y = max(0, min(y, image_height - 1))
        w = max(1, min(w, image_width - x))
        h = max(1, min(h, image_height - y))
        return {"x": x, "y": y, "w": w, "h": h}

    def _next_feedback_number(self, ticket: dict[str, Any]) -> int:
        highest = 0
        for path in ticket.get("screenshots", []) or []:
            match = re.search(r"(?:^|/)feedback-(\d+)", str(path))
            if match:
                highest = max(highest, int(match.group(1)))
        return min(highest + 1, 999)

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
        state = self._validate_create_state(state)
        assignee = self._validate_assignee(assignee)
        if state == "draft" and assignee != "unassigned":
            raise ValueError("draft tickets cannot be created with an assignee; release and route the draft instead")
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
            needs_audit=needs_audit,
            needs_inspection=needs_inspection,
            inspector_signoff=False,
            needs_user_signoff=needs_user_signoff,
            user_signoff=False,
            regression=regression,
            comments=[],
            blocked_reason=blocked_reason,
            commit_hash="",
            commit_exempt=False,
            notification_source_role=notification_source_role,
            caller_role=caller_role,
        )

    def _validate_create_state(self, state: str) -> str:
        normalized = str(state).strip().lower() or "analysis"
        if normalized not in {"draft", "analysis", "backlog"}:
            raise ValueError(f"invalid create state: {state}; allowed: draft, analysis, backlog")
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
        needs_audit: bool = True,
        needs_inspection: bool = False,
        inspector_signoff: bool = False,
        needs_user_signoff: bool,
        user_signoff: bool,
        regression: bool = False,
        comments: list[dict[str, Any]],
        parent_id: str = "",
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
        notification_source_role: str | None = None,
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
            needs_audit=needs_audit,
            needs_inspection=needs_inspection,
            inspector_signoff=inspector_signoff,
            needs_user_signoff=needs_user_signoff,
            user_signoff=user_signoff,
            regression=regression,
            comments=comments,
            parent_id=parent_id,
            blocked_reason=blocked_reason,
            commit_hash=commit_hash,
            commit_exempt=commit_exempt,
            created=created,
            updated=updated,
            notification_source_role=notification_source_role,
            caller_role=caller_role,
        )

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        return self._pg_update_ticket(ticket_id, patch, caller_role=caller_role)

    def route_ticket(self, ticket_id: str, state: str, assignee: str, *, caller_role: str | None = None) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        state = self._validate_state(str(state))
        assignee = self._validate_assignee(str(assignee))
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                return self._pg_get_ticket(ticket_id, conn)

    def reassign_ticket(
        self,
        ticket_id: str,
        assignee: str,
        *,
        reason: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        """Change a ticket's owner without moving it through the workflow.

        No state and no gate is passed in, because there is nothing here for a
        caller to choose: the stage the ticket already occupies is the only one
        this operation can leave it in, apart from the serial-focus redirect the
        database resolves from the target's own reservation.
        """
        ticket_id = str(ticket_id).strip().upper()
        assignee = self._validate_assignee(str(assignee))
        reason = str(reason).strip()
        if not reason:
            raise ValueError("reassign requires a reason")
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.reassign(%s, %s, %s);",
                    (ticket_id, assignee, reason),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def release_draft(self, ticket_id: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.release_draft(%s);", (ticket_id,))
                return self._pg_get_ticket(ticket_id, conn)

    def force_move_ticket(
        self,
        ticket_id: str,
        state: str,
        assignee: str,
        *,
        suppress_notification: bool = False,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        state = self._validate_state(str(state))
        assignee = self._validate_assignee(str(assignee))
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.force_move(%s, %s, %s, %s);",
                    (ticket_id, state, assignee, suppress_notification),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def director_edit_ticket(
        self,
        ticket_id: str,
        patch: Any,
        *,
        reason: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        """The Director's generic edit. Every check that matters is in the database.

        Nothing is filtered or normalised here beyond the ticket id: the point
        of this operation is that one place decides what a Director may change
        and what no path may manufacture, and that place has to be the one every
        caller goes through (SYRD-83).
        """
        ticket_id = str(ticket_id).strip().upper()
        if not isinstance(patch, dict):
            raise ValueError("director_edit patch must be an object")
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.director_edit(%s, %s::jsonb, %s);",
                    (ticket_id, json.dumps(patch), str(reason)),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def request_publication(
        self,
        ticket_id: str,
        *,
        ref: str,
        commit: str,
        bundle: str,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        """An implementer asks for its ref to be published. No credential involved.

        Everything that decides whether the ask is legitimate -- who owns the
        ticket, whose namespace the ref is in, whether an earlier ask is being
        retried or replaced -- is in the database, so the socket, the CLI and
        the UI cannot disagree about it (SYRD-93).
        """
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                row = conn.execute(
                    "SELECT * FROM ticket_board.request_publication(%s, %s, %s, %s);",
                    (ticket_id, str(ref), str(commit), str(bundle)),
                ).fetchone()
                return {
                    "request": _publication_row_with_history(conn, row),
                    "ticket": self._pg_get_ticket(ticket_id, conn),
                }

    def resolve_publication(
        self,
        request_id: int,
        *,
        outcome: str,
        detail: str = "",
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        """The control role records what became of one ask.

        A published verdict is the one outcome this board will not take on the
        deciding role's word (SYRD-118). It resolves the requested ref through
        the trusted publication path first, and hands what it found to the
        database, which refuses the verdict unless that is the commit the
        implementer asked for. A verdict that cannot be proven records nothing:
        the request stays open and the failure says what is missing, because a
        request that is neither published nor rejected is one the control role
        can still act on, and a false 'published' is one nobody can undo.
        """
        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                proof = ""
                if str(outcome).strip().lower() == "published":
                    # Authority first, and from the database, so a caller with
                    # no standing to decide is refused for that reason instead
                    # of being answered about a ref it could not have published.
                    conn.execute("SELECT ticket_board.require_publication_control();")
                    proof = self._prove_publication(conn, int(request_id))
                row = conn.execute(
                    "SELECT * FROM ticket_board.resolve_publication(%s::bigint, %s, %s, %s);",
                    (int(request_id), str(outcome), str(detail), proof),
                ).fetchone()
                request = _publication_row_with_history(conn, row)
                return {
                    "request": request,
                    "ticket": self._pg_get_ticket(str(request["ticket_id"]), conn),
                }

    def _prove_publication(self, conn: Any, request_id: int) -> str:
        """What the trusted publication path says about one open request.

        Returns the commit the requested ref resolves to when that is the
        commit the request named, and raises otherwise. Only a request that is
        still open is checked: re-recording a verdict that already landed is a
        retry, and the row already carries what was proven the first time.
        """
        row = conn.execute(
            "SELECT ref, commit_hash, state FROM ticket_board.publication_requests WHERE id = %s;",
            (int(request_id),),
        ).fetchone()
        if row is None or str(row["state"]) != "requested":
            # Not this function's refusal to make: the database says "not
            # found" or "already published/rejected" in its own words.
            return ""
        ref, commit = str(row["ref"]), str(row["commit_hash"]).lower()
        published = self.published_ref_commit(ref)
        if published == commit:
            return commit
        if published:
            raise ValueError(
                f"{ref} is published at {published[:12]}, not the requested {commit[:12]}. "
                "Nothing was recorded and the request is still open: publish the commit that "
                "was asked for, or reject the request with a reason."
            )
        if not self._readable_commit_repos():
            # A tenant whose cache is missing proves nothing either way, and
            # saying "not published" here would blame the publisher for a
            # misconfigured board. Still a refusal: unproven is unproven.
            raise ValueError(
                f"{ref} cannot be proven published: none of this board's commit repositories "
                f"({', '.join(str(path) for path in self.commit_git_dirs)}) can be read, so it "
                "has no trusted copy of anything. Nothing was recorded and the request is still "
                "open."
            )
        raise ValueError(
            f"{ref} is not published: {self._published_ref_absence(ref, commit)} "
            "Nothing was recorded and the request is still open -- publish the ref and record "
            "the outcome again, or reject the request with a reason."
        )

    def _readable_commit_repos(self) -> bool:
        for commit_git_dir in self.commit_git_dirs:
            try:
                self._commit_repo_git_args(commit_git_dir)
            except ValueError:
                continue
            return True
        return False

    def _published_ref_absence(self, ref: str, commit: str) -> str:
        """Why the proof is missing, said precisely enough to act on."""
        local = self._cache_ref_commit(f"refs/heads/{ref}")
        if local == commit:
            return (
                f"the trusted commit cache has no {PUBLISHED_REF_NAMESPACE}/{ref}. Its local "
                f"refs/heads/{ref} is at {commit[:12]}, but that branch is what filing the "
                "request creates, not evidence that anything reached the remote."
            )
        return f"the trusted commit cache has no {PUBLISHED_REF_NAMESPACE}/{ref}."

    def published_ref_commit(self, ref: str) -> str:
        """Resolve one published ref in the tenant's trusted commit cache.

        Local git against the root-configured repositories and nothing else: no
        remote is named, contacted, or taken at its word, so this stays safe to
        re-run and cannot be pointed at a remote of the caller's choosing.
        """
        return self._cache_ref_commit(f"{PUBLISHED_REF_NAMESPACE}/{ref}")

    def _cache_ref_commit(self, refname: str) -> str:
        candidate = refname.strip()
        if not PUBLISHABLE_REF.fullmatch(candidate) or ".." in candidate:
            return ""
        for commit_git_dir in self.commit_git_dirs:
            try:
                git_args = self._commit_repo_git_args(commit_git_dir)
            except ValueError:
                continue
            resolved = subprocess.run(
                [*git_args, "rev-parse", "--verify", "--quiet", "--end-of-options",
                 f"{candidate}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if resolved.returncode == 0 and resolved.stdout.strip():
                return resolved.stdout.strip().lower()
        return ""

    def publication_requests(
        self,
        *,
        ticket_id: str = "",
        state: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if ticket_id:
            clauses.append("ticket_id = %s")
            parameters.append(str(ticket_id).strip().upper())
        if state:
            clauses.append("state = %s")
            parameters.append(str(state).strip().lower())
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        parameters.append(int(limit))
        with self._pg_connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ticket_board.publication_requests"
                f"{where} ORDER BY id DESC LIMIT %s",
                tuple(parameters),
            ).fetchall()
            return [_publication_row_with_history(conn, row) for row in rows]

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
        conn.execute("SELECT set_config('ticket_board.ticket_prefix', %s, false);", (self.ticket_prefix,))
        conn.execute("SELECT set_config('ticket_board.project', %s, false);", (self.project,))
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
    -- What the board actually renders, not just when a writer remembered to
    -- stamp a timestamp. ticket_board.perform_workflow_action moves state,
    -- assignee and commit_hash without touching row_updated_at, and no trigger
    -- maintains it, so a signature built only from timestamps cannot see a
    -- committed transition -- from this server, another one, a migration, or
    -- psql. Reading the columns themselves removes that whole class.
    t.state,
    t.assignee,
    t.commit_hash,
    t.manually_controlled,
    t.queued_for_assignee,
    t.queued_behind_ticket,
    t.needs_inspection,
    t.needs_audit,
    t.needs_user_signoff,
    COALESCE(to_jsonb(t)->'workflow_flags', '{}'::jsonb)::text AS workflow_flags,
    COALESCE(
        (SELECT ns.awaiting_role FROM ticket_board.ticket_notification_state ns WHERE ns.ticket_id = t.id),
        ''
    ) AS awaiting_role,
    (SELECT count(*)::int FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id) AS blocker_count,
    (SELECT count(*)::int FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id AND b.resolved) AS resolved_blocker_count,
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
            from .workflow_config import read_configuration
            workflow_signature = ()
            if read_configuration(conn) is not None:
                revision = conn.execute("SELECT revision FROM ticket_board.workflow_configuration WHERE singleton").fetchone()["revision"]
                workflow_signature = (("workflow_revision", revision),)
        return workflow_signature + tuple(
            (
                str(row["id"]),
                str(row["row_updated_at"]),
                str(row["updated_text"]),
                str(row["state"]),
                str(row["assignee"]),
                str(row["commit_hash"]),
                bool(row["manually_controlled"]),
                str(row["queued_for_assignee"]),
                str(row["queued_behind_ticket"]),
                bool(row["needs_inspection"]),
                bool(row["needs_audit"]),
                bool(row["needs_user_signoff"]),
                str(row["workflow_flags"]),
                str(row["awaiting_role"]),
                int(row["blocker_count"]),
                int(row["resolved_blocker_count"]),
                int(row["comment_count"]),
                int(row["attachment_count"]),
                str(row["last_transition_send_trace_id"]),
            )
            for row in rows
        )

    def _pg_select_ticket_rows(self, conn: Any, ticket_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE t.id = %s" if ticket_id is not None else ""
        params = (ticket_id,) if ticket_id is not None else ()
        from .workflow_config import read_configuration
        workflow = read_configuration(conn)
        configured = workflow is not None
        owner_sql = "ticket_board.transition_target_role(scoped.state, scoped.assignee)" if configured else """CASE
            WHEN scoped.state = 'in_progress' THEN NULLIF(scoped.assignee, 'unassigned')
            WHEN scoped.state IN ('analysis', 'dat', 'director_review') THEN 'director'
            WHEN scoped.state = 'inspection' THEN 'inspector'
            WHEN scoped.state = 'audit' THEN 'audit'
            ELSE NULL END"""
        if configured:
            # The persisted document has already passed workflow_config.validate,
            # but still quote stage names as data before embedding the compact
            # read-only scope. This avoids granting the board service execution
            # rights on Director-only workflow mutation helpers.
            active_stages = [
                str(stage["name"])
                for stage in workflow["stages"]
                if not stage["terminal"] and stage["kind"] != "draft"
            ]
            active_stage_sql = ", ".join(
                "'" + stage.replace("'", "''") + "'" for stage in active_stages
            ) or "NULL"
            scope_sql = f"scoped.state IN ({active_stage_sql})"
        else:
            scope_sql = "scoped.state IN ('analysis', 'in_progress', 'inspection', 'audit', 'dat', 'director_review')"
        return conn.execute(
            f"""
WITH notification_scope AS (
    SELECT
        scoped.id,
        scoped.state,
        scoped.ticket_number,
        scoped.manually_controlled,
        scoped.queued_for_assignee,
        scoped.queued_behind_ticket,
        notification_state.entered_current_state_at,
        COALESCE(notification_state.awaiting_role, '') AS awaiting_role,
        {owner_sql} AS owner_role
    FROM ticket_board.tickets scoped
    LEFT JOIN ticket_board.ticket_notification_state notification_state
        ON notification_state.ticket_id = scoped.id
    WHERE {scope_sql}
),
notification_candidates AS (
    SELECT
        notification_scope.id,
        notification_scope.state,
        notification_scope.ticket_number,
        notification_scope.owner_role,
        notification_scope.entered_current_state_at,
        -- Delivery can legitimately be deferred while the assigned pane is
        -- busy. Current-work visibility therefore comes from durable workflow
        -- ownership and serial-focus state, never from a successful send.
        -- A logical role, not merely a non-NULL value. An ownerless stage
        -- resolves to NULL, but a stage that names an owner as the empty
        -- string would partition every such ticket together and highlight one
        -- of them for a role nobody is: the invariant is one ticket per role,
        -- so the row has to name a role (SYRD-72).
        NULLIF(notification_scope.owner_role, '') IS NOT NULL
            AND NOT notification_scope.manually_controlled
            AND notification_scope.awaiting_role = ''
            AND notification_scope.queued_for_assignee = ''
            AND notification_scope.queued_behind_ticket = ''
            AND NOT EXISTS (
                SELECT FROM ticket_board.ticket_blockers blocker
                WHERE blocker.ticket_id = notification_scope.id
                  AND NOT blocker.resolved
            )
            AS is_actionable_current,
        sent.last_sent_at AS active_work_notified_at,
        queued.attempts AS active_work_delivery_attempts,
        queued.last_error AS active_work_delivery_last_error,
        queued.dead_lettered_at AS active_work_delivery_dead_lettered_at,
        queued.terminal_reason AS active_work_delivery_terminal_reason,
        queued.next_attempt_at AS active_work_delivery_next_attempt_at,
        queued.id IS NOT NULL AS active_work_delivery_queued,
        unconfirmed.last_unconfirmed_at AS active_work_delivery_unconfirmed_at,
        unconfirmed.unconfirmed_reason AS active_work_delivery_unconfirmed_reason
    FROM notification_scope
    LEFT JOIN LATERAL (
        SELECT max(trace.ts) AS last_sent_at
        FROM ticket_board.notification_trace trace
        WHERE trace.ticket_id = notification_scope.id
          AND trace.target_role = notification_scope.owner_role
          AND trace.kind = 'transition'
          AND trace.event = 'send'
          AND trace.ticket_state_at_event = notification_scope.state
          -- This VISIT to the stage, not any earlier one: a ticket sent to Ops,
          -- routed back to analysis and returned to Ops must not report the
          -- first visit's send while the second notice is pending or dead
          -- (SYRD-264 Final Sign-Off). A legacy row with no recorded entry is
          -- not bounded rather than hidden.
          AND (notification_scope.entered_current_state_at IS NULL
               OR trace.ts >= notification_scope.entered_current_state_at)
    ) sent ON true
    -- The notice that has NOT been delivered, if there is one: acknowledged
    -- notices are deleted, so a remaining row for this owner and this stage is
    -- either still pending or dead-lettered. Without this the board showed a
    -- ticket as the owner's current work -- which MEFP-1 was -- and nothing at
    -- all about its notice having been dead-lettered (SYRD-264).
    LEFT JOIN LATERAL (
        SELECT q.id, q.attempts, q.last_error, q.dead_lettered_at, q.terminal_reason,
               q.next_attempt_at
        FROM ticket_board.ticket_notification_queue q
        WHERE q.ticket_id = notification_scope.id
          AND q.target_role = notification_scope.owner_role
          AND q.kind = 'transition'
          AND COALESCE(q.payload->>'new_state', q.payload->>'state') = notification_scope.state
          AND (notification_scope.entered_current_state_at IS NULL
               OR q.created_at >= notification_scope.entered_current_state_at)
        ORDER BY q.id DESC
        LIMIT 1
    ) queued ON true
    -- Sent, and never seen to arrive: the recipient's own hooks recorded no
    -- turn afterwards, or there was no hook record to read. Not a send, so it
    -- cannot read as delivered -- which is how MEFP-1's Final Sign-Off notice
    -- was reported (SYRD-268). The latest one, with the listener's reason.
    LEFT JOIN LATERAL (
        SELECT trace.ts AS last_unconfirmed_at, trace.busy_reason AS unconfirmed_reason
        FROM ticket_board.notification_trace trace
        WHERE trace.ticket_id = notification_scope.id
          AND trace.target_role = notification_scope.owner_role
          AND trace.kind = 'transition'
          AND trace.event = 'send_unconfirmed'
          AND trace.ticket_state_at_event = notification_scope.state
          AND (notification_scope.entered_current_state_at IS NULL
               OR trace.ts >= notification_scope.entered_current_state_at)
        ORDER BY trace.ts DESC
        LIMIT 1
    ) unconfirmed ON true
),
active_work AS (
    SELECT
        notification_candidates.id,
        notification_candidates.owner_role,
        notification_candidates.active_work_notified_at,
        notification_candidates.active_work_delivery_attempts,
        notification_candidates.active_work_delivery_last_error,
        notification_candidates.active_work_delivery_dead_lettered_at,
        notification_candidates.active_work_delivery_terminal_reason,
        notification_candidates.active_work_delivery_next_attempt_at,
        notification_candidates.active_work_delivery_queued,
        notification_candidates.active_work_delivery_unconfirmed_at,
        notification_candidates.active_work_delivery_unconfirmed_reason,
        -- There is exactly one visible current ticket per logical owner. The
        -- send timestamp remains separate evidence and does not rank work.
        notification_candidates.is_actionable_current
            AND row_number() OVER (
                PARTITION BY notification_candidates.owner_role
                ORDER BY notification_candidates.is_actionable_current DESC,
                    notification_candidates.entered_current_state_at NULLS LAST,
                    notification_candidates.ticket_number
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
    t.origin_project,
    t.external_source_ref,
    t.blocked_reason,
    t.queued_for_assignee,
    t.queued_behind_ticket,
    t.implementation,
    t.audit_prompt,
    t.audit_signoff,
    t.needs_audit,
    t.needs_inspection,
    t.inspector_signoff,
    t.needs_user_signoff,
    t.user_signoff,
    t.regression,
    t.manually_controlled,
    t.commit_hash,
    t.commit_exempt,
    COALESCE(to_jsonb(t)->'workflow_flags', '{{}}'::jsonb) AS workflow_flags,
    t.created_text,
    t.updated_text,
    active_work.owner_role AS active_work_owner_role,
    active_work.active_work_notified_at,
    active_work.active_work_delivery_attempts,
    active_work.active_work_delivery_last_error,
    active_work.active_work_delivery_dead_lettered_at,
    active_work.active_work_delivery_terminal_reason,
    active_work.active_work_delivery_next_attempt_at,
    COALESCE(active_work.active_work_delivery_queued, false) AS active_work_delivery_queued,
    active_work.active_work_delivery_unconfirmed_at,
    active_work.active_work_delivery_unconfirmed_reason,
    COALESCE(active_work.active_work_highlight, false) AS active_work_highlight,
    COALESCE(notification_state.awaiting_role, '') AS awaiting_role,
    COALESCE(
        (SELECT array_agg(b.blocker_ticket_id ORDER BY b.position)
         FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = t.id
           AND NOT b.resolved),
        ARRAY[]::text[]
    ) AS blocked_by,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('id', b.blocker_ticket_id, 'resolved', b.resolved) ORDER BY b.position)
         FROM ticket_board.ticket_blockers b
         WHERE b.ticket_id = t.id),
        '[]'::jsonb
    ) AS blockers,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('who', c.who, 'text', c.text, 'ts', c.ts_text, 'urgent', c.urgent) ORDER BY c.position)
         FROM ticket_board.ticket_comments c
         WHERE c.ticket_id = t.id),
        '[]'::jsonb
    ) AS comments,
    COALESCE(
        (SELECT jsonb_agg(jsonb_build_object('path', a.path, 'metadata', a.metadata) ORDER BY a.position)
         FROM ticket_board.ticket_attachments a
         WHERE a.ticket_id = t.id),
        '[]'::jsonb
    ) AS screenshots
FROM ticket_board.tickets t
LEFT JOIN active_work ON active_work.id = t.id
LEFT JOIN ticket_board.ticket_notification_state notification_state
    ON notification_state.ticket_id = t.id
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

    def _pg_workflow_columns(self) -> list[dict[str, str]]:
        with self._pg_connect() as conn:
            rows = conn.execute(
                """
SELECT name, display_label
FROM ticket_board.workflow_stages
ORDER BY rank;
"""
            ).fetchall()
        return [{"key": str(row["name"]), "label": str(row["display_label"])} for row in rows]

    def _pg_get_ticket(self, ticket_id: str, conn: Any | None = None) -> dict[str, Any]:
        if conn is None:
            with self._pg_connect() as own_conn:
                return self._pg_get_ticket(ticket_id, own_conn)
        rows = self._pg_select_ticket_rows(conn, ticket_id=ticket_id)
        if not rows:
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        ticket = self._pg_row_to_ticket(rows[0])
        from .workflow_config import read_configuration, available_transitions
        cfg = read_configuration(conn)
        if cfg:
            ticket["workflow_actions"] = available_transitions(cfg, ticket)
        # SYRD-93: what this ticket is waiting on, if it is waiting on a
        # publication. It travels with the ticket so the panel, the CLI and a
        # reader of the JSON all see the same thing without a second call.
        open_request = conn.execute(
            "SELECT * FROM ticket_board.publication_requests "
            "WHERE ticket_id = %s AND state = 'requested' ORDER BY id DESC LIMIT 1",
            (ticket["id"],),
        ).fetchone()
        ticket["publication"] = _publication_row_with_history(conn, open_request)
        return ticket

    def _pg_row_to_ticket(self, row: dict[str, Any]) -> dict[str, Any]:
        comments = row["comments"]
        if isinstance(comments, str):
            comments = json.loads(comments)
        blockers = row["blockers"]
        if isinstance(blockers, str):
            blockers = json.loads(blockers)
        screenshots = row["screenshots"] or []
        if isinstance(screenshots, str):
            screenshots = json.loads(screenshots)
        ticket = {
            "id": str(row["id"]),
            "title": self._require_text(row["title"], "title"),
            "body": self._require_body(row["body"]),
            "assignee": self._validate_assignee(str(row["assignee"])),
            "state": self._validate_state(str(row["state"])),
            "blocked_by": self._validate_blocked_by(list(row["blocked_by"] or []), str(row["id"])),
            "blockers": self._validate_blockers(blockers, str(row["id"])),
            "parent_id": str(row["parent_id"] or ""),
            "origin_project": self._require_plain_string(row["origin_project"], "origin_project"),
            "external_source_ref": self._require_plain_string(row["external_source_ref"], "external_source_ref"),
            "blocked_reason": self._require_plain_string(row["blocked_reason"], "blocked_reason"),
            "queued_for_assignee": self._require_plain_string(row["queued_for_assignee"], "queued_for_assignee"),
            "queued_behind_ticket": self._require_plain_string(row["queued_behind_ticket"], "queued_behind_ticket"),
            "implementation": self._require_plain_string(row["implementation"], "implementation"),
            "audit_prompt": self._require_plain_string(row["audit_prompt"], "audit_prompt"),
            "audit_signoff": bool(row["audit_signoff"]),
            "needs_audit": bool(row["needs_audit"]),
            "needs_inspection": bool(row["needs_inspection"]),
            "inspector_signoff": bool(row["inspector_signoff"]),
            "needs_user_signoff": bool(row["needs_user_signoff"]),
            "user_signoff": bool(row["user_signoff"]),
            "regression": bool(row["regression"]),
            "manually_controlled": bool(row["manually_controlled"]),
            "commit_hash": str(row["commit_hash"] or ""),
            "commit_exempt": bool(row["commit_exempt"]),
            "workflow_flags": dict(row.get("workflow_flags") or {}),
            "created": self._require_text(row["created_text"], "created"),
            "updated": self._require_text(row["updated_text"], "updated"),
            "active_work_highlight": bool(row["active_work_highlight"]),
            "active_work_owner_role": str(row["active_work_owner_role"] or ""),
            "active_work_notified_at": self._format_optional_datetime(row["active_work_notified_at"]),
            "active_work_delivery": self._active_work_delivery(row),
            # awaiting_role has TWO consumers and they are easy to mistake for
            # one. read_client.needs_director() reads it from here to put a
            # ticket in the director's attention queue, and the nudge queries in
            # schema.sql read it straight from ticket_notification_state,
            # through ticket_awaiting_role_is_active(), to SUPPRESS nudges for
            # four hours. Dropping this field does not merely hide a queue entry:
            # it restores the state where a ticket was silenced and invisible at
            # the same time, which is PGU-906. Two deliberate asymmetries: queue
            # visibility is not timeout-aware, so an expired flag still shows the
            # ticket rather than losing it at the moment nudges resume; and
            # needs_director() is the only queue consumer, so awaiting_role at
            # any other role still only suppresses nudges.
            "awaiting_role": str(row["awaiting_role"] or "").strip().lower(),
            "comments": self._validate_comments(comments),
        }
        self._set_screenshot_fields(ticket, self._build_screenshot_entries(list(screenshots)))
        return ticket

    def _active_work_delivery(self, row: Any) -> dict[str, Any]:
        """Whether the current owner's notice for this stage reached them.

        `active_work_highlight` says whose current work a ticket is; it has
        never said the owner was told, and on mefp a ticket was highlighted as
        Ops's work while its one notice sat dead-lettered (SYRD-264). This says
        which of the three it is, from the durable records:

        * ``delivered`` -- a send is traced for this owner at this stage;
        * ``failed`` -- the notice was dead-lettered, with its reason;
        * ``pending`` -- the notice is queued and has not been delivered yet,
          with the last error, if a send has been tried and failed;
        * ``unconfirmed`` -- a notice was sent and nothing says it arrived:
          the recipient's own hooks recorded no turn afterwards, or there was
          no hook record to read -- with the listener's reason (SYRD-268);
        * ``none`` -- the ticket has an owner and no notice is recorded at all.

        Empty ``state`` when the ticket has no current owner to notify.
        """
        get = row.get if hasattr(row, "get") else (lambda key, default=None: row[key])
        owner = str(get("active_work_owner_role", "") or "")
        if not owner:
            return {"state": ""}
        notified_at = self._format_optional_datetime(get("active_work_notified_at"))
        if notified_at:
            return {"state": "delivered", "at": notified_at}
        if get("active_work_delivery_queued", False):
            attempts = int(get("active_work_delivery_attempts", 0) or 0)
            dead_at = self._format_optional_datetime(get("active_work_delivery_dead_lettered_at"))
            if dead_at:
                return {
                    "state": "failed",
                    "at": dead_at,
                    "reason": str(get("active_work_delivery_terminal_reason", "") or ""),
                    "attempts": attempts,
                }
            return {
                "state": "pending",
                "attempts": attempts,
                "reason": str(get("active_work_delivery_last_error", "") or ""),
                "next_attempt_at": self._format_optional_datetime(
                    get("active_work_delivery_next_attempt_at")
                ),
            }
        unconfirmed_at = self._format_optional_datetime(get("active_work_delivery_unconfirmed_at"))
        if unconfirmed_at:
            # Sent, and no turn followed in the recipient's pane. Reported as
            # exactly that, and never as delivered (SYRD-268).
            return {
                "state": "unconfirmed",
                "at": unconfirmed_at,
                "reason": get("active_work_delivery_unconfirmed_reason") or "no_submission_witnessed",
            }
        return {"state": "none"}

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
        needs_audit: bool = True,
        needs_inspection: bool = False,
        inspector_signoff: bool = False,
        needs_user_signoff: bool,
        user_signoff: bool,
        regression: bool = False,
        comments: list[dict[str, Any]],
        parent_id: str = "",
        blocked_reason: str = "",
        commit_hash: str = "",
        commit_exempt: bool = False,
        created: str | None = None,
        updated: str | None = None,
        notification_source_role: str | None = None,
        caller_role: str | None = None,
    ) -> dict[str, Any]:
        title = self._require_text(title, "title").strip()
        assignee = self._validate_assignee(assignee)
        state = self._validate_state(state)
        create_state = self._validate_create_state(state)
        if create_state == "draft" and assignee != "unassigned":
            raise ValueError("draft tickets cannot be created with an assignee; release and route the draft instead")
        attachment_patch: dict[str, Any] = {}
        if screenshots not in (None, [], ""):
            attachment_patch["screenshots"] = screenshots
        elif screenshot not in (None, "", "null"):
            attachment_patch["screenshot"] = screenshot
        implementation = self._require_plain_string(implementation, "implementation")
        audit_prompt = self._require_plain_string(audit_prompt, "audit_prompt")
        if audit_prompt.strip():
            raise ValueError("postgres function API does not support audit_prompt writes yet")
        if audit_signoff or inspector_signoff or user_signoff:
            raise ValueError("postgres function API does not support initial signoff fields yet")
        if commit_hash or commit_exempt:
            raise ValueError("postgres function API does not support initial commit fields yet")
        normalized_comments = self._validate_comments(comments)
        blocked_by = self._validate_blocked_by(blocked_by or [], ticket_id_sentinel(self.ticket_prefix))
        blocked_reason = self._require_plain_string(blocked_reason, "blocked_reason")
        self._enforce_blocked_reason_rule(blocked_by, blocked_reason)

        with self._pg_connect() as conn:
            with conn.transaction():
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                if notification_source_role:
                    self._pg_set_notification_source_role(conn, notification_source_role)
                self._validate_blocker_ticket_states(conn, blocked_by)
                parent_id = "" if parent_id in (None, "", "null") else str(parent_id).strip().upper()
                created_via_file_bug = False
                use_file_bug = parent_id and create_state == "analysis" and caller_role not in {"director", "user"}
                if use_file_bug:
                    ticket_id = self._pg_call_scalar(
                        conn,
                        "SELECT ticket_board.file_bug(%s, %s, %s, %s, %s, %s, %s) AS id;",
                        (title, body.strip(), parent_id, assignee, blocked_by, blocked_reason, needs_audit),
                    )
                    created_via_file_bug = True
                else:
                    ticket_id = self._pg_call_scalar(
                        conn,
                        "SELECT ticket_board.create_ticket(%s, %s, %s, %s, %s, %s, %s, %s) AS id;",
                        (title, body.strip(), create_state, assignee, blocked_by, blocked_reason, needs_user_signoff, needs_audit),
                    )
                    if parent_id:
                        self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"parent_id": parent_id})))
                if created_via_file_bug and needs_user_signoff:
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"needs_user_signoff": True})))
                if implementation:
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"implementation": implementation})))
                if needs_inspection:
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"needs_inspection": True})))
                if regression:
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps({"regression": True})))
                if create_state != state:
                    raise ValueError(f"invalid create state: {state}; allowed: draft, analysis, backlog")
                if attachment_patch:
                    current = self._pg_get_ticket(ticket_id, conn)
                    self._materialize_edit_field_attachments(attachment_patch, ticket_id, current)
                    self._pg_call(conn, "SELECT ticket_board.edit_fields(%s, %s::jsonb);", (ticket_id, json.dumps(attachment_patch)))
                for comment in normalized_comments:
                    self._pg_set_caller_role(conn, comment["who"])
                    self._pg_call(conn, "SELECT ticket_board.add_comment(%s, %s, %s);", (ticket_id, comment["text"], bool(comment.get("urgent", False))))
                return self._pg_get_ticket(ticket_id, conn)

    def file_report(
        self,
        *,
        title: str,
        body: str,
        origin_project: str,
        external_source_ref: str = "",
    ) -> dict[str, Any]:
        title = self._require_text(title, "title").strip()
        body = str(body or "")
        origin_project = self._require_plain_string(origin_project, "origin_project").strip()
        external_source_ref = self._require_plain_string(external_source_ref, "external_source_ref").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                ticket_id = self._pg_call_scalar(
                    conn,
                    "SELECT ticket_board.file_report(%s, %s, %s, %s) AS id;",
                    (title, body, origin_project, external_source_ref),
                )
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
                if "commit_hash" in patch and "state" not in patch:
                    raise ValueError("commit_hash must be written with submit_to_audit or mark_done, not edit_fields")
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
                    self._validate_blocker_ticket_states(conn, blocked_by)
                    self._pg_call(conn, "SELECT ticket_board.set_blockers(%s, %s, %s);", (ticket_id, blocked_by, blocked_reason))

                comment_text = ""
                comment_who = ""
                comment_urgent = False
                if "comment" in patch:
                    comment = patch["comment"]
                    comment_who = str(comment.get("who", "")).strip()
                    comment_text = str(comment.get("text", "")).strip()
                    comment_urgent = bool(comment.get("urgent", False))
                    if not comment_who:
                        raise ValueError("comment requires an author")
                    if not comment_text and "state" not in patch:
                        raise ValueError("comment requires non-empty text")
                    self._pg_set_caller_role(conn, comment_who)

                state = self._validate_state(str(patch["state"])) if "state" in patch else current["state"]
                assignee = self._validate_assignee(str(patch["assignee"])) if "assignee" in patch else current["assignee"]
                commit_hash = str(patch.get("commit_hash", current.get("commit_hash", "")) or "").strip()
                if (
                    "commit_hash" in patch
                    and state not in {"audit", "done"}
                    and not (
                        state == "director_review"
                        and current["state"] == "in_progress"
                        and self._pg_transition_configured(conn, "in_progress", "director_review", "submit_to_audit")
                    )
                ):
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
                if "user_signoff" in patch:
                    if not bool(patch["user_signoff"]):
                        raise ValueError("postgres function API only supports setting user_signoff true")
                    self._pg_call(conn, "SELECT ticket_board.user_sign_off(%s, %s);", (ticket_id, comment_text))
                    comment_text = ""

                if "state" in patch:
                    if inspector_signoff_handled and state == "audit":
                        pass
                    elif state == "in_progress" and current["state"] == "inspection":
                        target_assignee = assignee if "assignee" in patch else ""
                        self._pg_call(conn, "SELECT ticket_board.inspector_kick_back(%s, %s, %s);", (ticket_id, comment_text, target_assignee))
                        comment_text = ""
                    elif state == "in_progress" and current["state"] == "audit":
                        target_assignee = assignee if "assignee" in patch else ""
                        self._pg_call(conn, "SELECT ticket_board.audit_kick_back(%s, %s, %s);", (ticket_id, comment_text, target_assignee))
                        comment_text = ""
                    elif state == "in_progress" and current["state"] == "dat":
                        target_assignee = assignee if "assignee" in patch else ""
                        self._pg_call(conn, "SELECT ticket_board.director_dat_kick_back(%s, %s, %s);", (ticket_id, comment_text, target_assignee))
                        comment_text = ""
                    elif state == "analysis" and current["state"] == "draft":
                        self._pg_call(conn, "SELECT ticket_board.release_draft(%s);", (ticket_id,))
                    elif state == "analysis" and current["state"] in {"user_review", "director_review", "done"}:
                        self._pg_call(conn, "SELECT ticket_board.user_reopen(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    elif state == "in_progress" and "assignee" in patch:
                        self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                    elif state == "in_progress":
                        self._pg_call(conn, "SELECT ticket_board.start_work(%s);", (ticket_id,))
                    elif state == "inspection":
                        self._pg_call(conn, "SELECT ticket_board.submit_to_inspection(%s);", (ticket_id,))
                    elif state == "audit":
                        commit_hash = self._validate_commit_hash(commit_hash)
                        self._pg_call(conn, "SELECT ticket_board.submit_to_audit(%s, %s);", (ticket_id, commit_hash))
                    elif (
                        state == "director_review"
                        and current["state"] == "in_progress"
                        and self._pg_transition_configured(conn, "in_progress", "director_review", "submit_to_audit")
                    ):
                        commit_hash = self._validate_commit_hash(commit_hash)
                        self._pg_call(conn, "SELECT ticket_board.submit_to_audit(%s, %s);", (ticket_id, commit_hash))
                    elif state == "user_review" and current["state"] == "dat":
                        self._pg_call(conn, "SELECT ticket_board.director_dat_sign_off(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    elif state == "done":
                        commit_hash = self._validate_commit_hash(commit_hash)
                        self._pg_call(conn, "SELECT ticket_board.mark_done(%s, %s);", (ticket_id, commit_hash))
                    elif state == "backlog":
                        self._pg_call(conn, "SELECT ticket_board.defer(%s);", (ticket_id,))
                    elif state == "cancelled":
                        if not comment_text:
                            raise ValueError("cancelling a ticket requires a non-empty comment explaining why")
                        self._pg_call(conn, "SELECT ticket_board.cancel(%s, %s);", (ticket_id, comment_text))
                        comment_text = ""
                    elif state == "analysis" and comment_text and current["state"] == "audit":
                        target_assignee = assignee if "assignee" in patch else ""
                        self._pg_call(conn, "SELECT ticket_board.audit_kick_back(%s, %s, %s);", (ticket_id, comment_text, target_assignee))
                        comment_text = ""
                    else:
                        self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, state, assignee))
                elif "assignee" in patch:
                    self._pg_call(conn, "SELECT ticket_board.route(%s, %s, %s);", (ticket_id, current["state"], assignee))

                if comment_text:
                    self._pg_call(conn, "SELECT ticket_board.add_comment(%s, %s, %s);", (ticket_id, comment_text, comment_urgent))
                return self._pg_get_ticket(ticket_id, conn)

    def submit_to_audit_without_commit(self, ticket_id: str, reason: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        reason = self._require_text(reason, "reason").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.submit_to_audit_without_commit(%s, %s);",
                    (ticket_id, reason),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def request_commit_exempt(self, ticket_id: str, reason: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        reason = self._require_text(reason, "reason").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.request_commit_exempt(%s, %s);", (ticket_id, reason))
                return self._pg_get_ticket(ticket_id, conn)

    def implementer_kick_back(self, ticket_id: str, reason: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        reason = self._require_text(reason, "reason").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.implementer_kick_back(%s, %s);", (ticket_id, reason))
                return self._pg_get_ticket(ticket_id, conn)

    def start_task(self, ticket_id: str, note: str = "", *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        note = self._require_plain_string(note, "note").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.start_task(%s, %s);", (ticket_id, note))
                return self._pg_get_ticket(ticket_id, conn)

    def complete_task(self, ticket_id: str, completion_note: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        completion_note = self._require_text(completion_note, "completion_note").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.complete_task(%s, %s);", (ticket_id, completion_note))
                return self._pg_get_ticket(ticket_id, conn)

    def recover_stalled_ticket(self, ticket_id: str, *, reason: str, caller_role: str) -> dict[str, Any]:
        """Take the transition a stalled ticket's owner did not take.

        Bounded by construction: the database picks the one declared no-code
        transition the owner could have taken and runs it through the ordinary
        executor, so no gate, sign-off or blocker is skipped and the work lands
        at its next required gate rather than anywhere the caller names
        (SYRD-133).
        """
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.recover_stalled_ticket(%s, %s);",
                    (ticket_id, str(reason)),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def request_dependency(
        self, ticket_id: str, *, awaiting_role: str, reason: str, caller_role: str
    ) -> dict[str, Any]:
        """Record why this work is waiting and who it waits on, in one act.

        The two halves used to be two calls, and the durable one was the half
        that got left out: on SYRD-131 the reason was written as a comment and
        `awaiting_role` stayed empty, so nothing held the work and nobody was
        told (SYRD-133). One database function, one transaction: the ticket
        records both or neither. The assignee is untouched, because the work is
        still the requesting role's -- it is waiting, not handed over.
        """
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.request_dependency(%s, %s, %s);",
                    (ticket_id, str(awaiting_role), str(reason)),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def release_external_blocker(
        self, ticket_id: str, *, ref: str, reason: str, commit: str = "", caller_role: str
    ) -> dict[str, Any]:
        """End a wait on another board's work, explicitly, with why (SYRD-270).

        Nothing that happens on the other board releases it: the foreign
        ticket moving is not the thing this ticket was waiting for. When the
        wait was for a commit this board could not see, `commit` is checked
        against THIS board's own repository first -- the same check a
        submission makes -- and the release is refused until it resolves.
        The release moves nothing; the owner still takes the work through
        every gate.
        """
        ticket_id = str(ticket_id).strip().upper()
        evidence = ""
        if str(commit or "").strip():
            resolved = self._validate_commit_hash(str(commit))
            evidence = f"This board resolves commit {resolved}."
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(
                    conn,
                    "SELECT ticket_board.release_external_blocker(%s, %s, %s, %s);",
                    (ticket_id, normalize_blocker_ref(ref), str(reason), evidence),
                )
                return self._pg_get_ticket(ticket_id, conn)

    def set_awaiting_role(self, ticket_id: str, awaiting_role: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        awaiting_role = self._require_text(awaiting_role, "awaiting_role").strip().lower()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.set_awaiting_role(%s, %s);", (ticket_id, awaiting_role))
                return self._pg_get_ticket(ticket_id, conn)

    def clear_awaiting_role(self, ticket_id: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.clear_awaiting_role(%s);", (ticket_id,))
                return self._pg_get_ticket(ticket_id, conn)

    def dismiss_notification(self, notification_id: int, *, reason: str = "", caller_role: str) -> dict[str, Any]:
        if notification_id <= 0:
            raise ValueError("notification_id must be a positive integer")
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.dismiss_notification(%s::bigint, %s::text);", (notification_id, reason))
        return {"notification_id": notification_id, "dismissed": True}

    def dismiss_notification_by_key(
        self,
        *,
        ticket_id: str,
        target_role: str,
        kind: str = "transition",
        reason: str = "",
        caller_role: str,
    ) -> dict[str, Any]:
        ticket_id = self._require_text(ticket_id, "ticket_id").strip().upper()
        target_role = self._require_text(target_role, "target_role").strip().lower()
        kind = str(kind or "transition").strip().lower() or "transition"
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                result = conn.execute(
                    "SELECT ticket_board.dismiss_notification_by_key(%s::text, %s::text, %s::text, %s::text) AS notification_id;",
                    (ticket_id, target_role, kind, reason),
                ).fetchone()
        notification_id = int(result["notification_id"]) if result else 0
        return {"notification_id": notification_id, "dismissed": True}

    def _pg_call(self, conn: Any, sql: str, params: tuple[Any, ...]) -> None:
        conn.execute(sql, params)

    def _pg_set_caller_role(self, conn: Any, caller_role: str) -> None:
        conn.execute("SELECT set_config('ticket_board.caller_role', %s, true);", (caller_role,))

    def _pg_set_notification_source_role(self, conn: Any, notification_source_role: str) -> None:
        conn.execute(
            "SELECT set_config('ticket_board.notification_source_role', %s, true);",
            (notification_source_role,),
        )

    def _pg_call_scalar(self, conn: Any, sql: str, params: tuple[Any, ...]) -> str:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise ValueError("postgres function returned no row")
        value = next(iter(row.values()))
        return str(value)

    def _pg_transition_configured(self, conn: Any, from_stage: str, to_stage: str, action_name: str) -> bool:
        row = conn.execute(
            """
SELECT EXISTS (
    SELECT 1
    FROM ticket_board.workflow_transitions
    WHERE from_stage = %s
      AND to_stage = %s
      AND action_name = %s
) AS configured;
""",
            (from_stage, to_stage, action_name),
        ).fetchone()
        return bool(row and row["configured"])

    def _pg_edit_field_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        editable = {
            "title",
            "body",
            "parent_id",
            "screenshots",
            "screenshot",
            "implementation",
            "audit_prompt",
            "needs_inspection",
            "needs_audit",
            "needs_user_signoff",
            "commit_exempt",
            "regression",
        }
        if patch.get("inspector_signoff") is False:
            editable = set(editable)
            editable.add("inspector_signoff")
        if patch.get("audit_signoff") is False:
            editable = set(editable)
            editable.add("audit_signoff")
        if patch.get("user_signoff") is False:
            editable = set(editable)
            editable.add("user_signoff")
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

    def _validate_comments(self, raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, list):
            raise ValueError("comments must be a list")
        comments: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("comment entries must be objects")
            comments.append(
                {
                    "who": self._require_text(item.get("who"), "comment.who"),
                    "text": self._require_text(item.get("text"), "comment.text"),
                    "ts": self._require_text(item.get("ts"), "comment.ts"),
                    "urgent": bool(item.get("urgent", False)),
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
            blocker_id = normalize_blocker_ref(item)
            if not blocker_id:
                raise ValueError("blocked_by entries must not be empty")
            if not (valid_ticket_id(blocker_id) or is_external_blocker(blocker_id)):
                raise ValueError(f"invalid blocked_by ticket id: {item}")
            if is_external_blocker(blocker_id) and blocker_id.split(":", 1)[1].rsplit("-", 1)[0] == self.ticket_prefix:
                local_id = blocker_id.split(":", 1)[1]
                raise ValueError(
                    f"external blocker {blocker_id} names a ticket on this board; block on {local_id} instead"
                )
            if blocker_id == ticket_id:
                raise ValueError("ticket cannot be blocked_by itself")
            if blocker_id not in blocked_by:
                blocked_by.append(blocker_id)
        return blocked_by

    def _validate_blockers(self, raw: Any, ticket_id: str) -> list[dict[str, Any]]:
        if raw in (None, "", "null"):
            return []
        if not isinstance(raw, list):
            raise ValueError("blockers must be a list")
        blockers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("blockers entries must be objects")
            blocker_id = str(item.get("id", "")).strip().upper()
            normalized_id = self._validate_blocked_by([blocker_id], ticket_id)[0]
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            blockers.append({"id": normalized_id, "resolved": bool(item.get("resolved"))})
        return blockers

    def _validate_blocker_ticket_states(self, conn: Any, blocked_by: list[str]) -> None:
        if not blocked_by:
            return
        # A blocker on another board has no ticket here to check (SYRD-270).
        local = [blocker_id for blocker_id in blocked_by if not is_external_blocker(blocker_id)]
        rows = conn.execute(
            "SELECT id, state FROM ticket_board.tickets WHERE id = ANY(%s);",
            (local,),
        ).fetchall()
        state_by_id = {str(row["id"]): str(row["state"]) for row in rows}
        for blocker_id in local:
            blocker_state = state_by_id.get(blocker_id)
            if blocker_state is None:
                raise ValueError(f"blocker ticket not found: {blocker_id}")
            if blocker_state in TERMINAL_STATES:
                raise ValueError(f"terminal tickets cannot block other tickets: {blocker_id} is {blocker_state}")

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
        if not re.fullmatch(r"[0-9A-Fa-f]{7,40}", value):
            raise ValueError("commit_hash must be a 7-40 character hex commit")
        resolved, missing_repos = self._resolve_known_commit(value)
        if resolved:
            return resolved
        # Not here yet. Implementers publish by pushing to the project's remote
        # (SYRD-123), so a commit this board has never seen is the ordinary case
        # for work that has just been published rather than a sign of anything
        # wrong. The cache is refreshed once, from its own configured remote,
        # and the question is asked again.
        if self._refresh_commit_repos():
            resolved, missing_repos = self._resolve_known_commit(value)
            if resolved:
                return resolved
        if missing_repos and len(missing_repos) == len(self.commit_git_dirs):
            missing = ", ".join(str(path) for path in missing_repos)
            raise ValueError(f"commit_hash verification repository not found: {missing}")
        raise ValueError(
            f"unknown commit_hash: {value}. It is not in this board's copy of the project "
            "repository, even after refreshing it -- push the commit to the project remote "
            "and submit it again."
        )

    def _resolve_known_commit(self, value: str) -> tuple[str, list[Path]]:
        """The commit as this board's own copies of the repository resolve it."""
        missing_repos: list[Path] = []
        for commit_git_dir in self.commit_git_dirs:
            try:
                git_args = self._commit_repo_git_args(commit_git_dir)
            except ValueError:
                missing_repos.append(commit_git_dir)
                continue
            proc = subprocess.run(
                [*git_args, "cat-file", "-e", f"{value}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                continue
            resolved = subprocess.run(
                [*git_args, "rev-parse", "--verify", f"{value}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if resolved.returncode == 0 and resolved.stdout.strip():
                return resolved.stdout.strip(), missing_repos
        return "", missing_repos

    def _refresh_commit_repos(self) -> bool:
        """Fetch each verification repository from its own configured remote.

        Bounded and credential-free by construction: the remote is whatever that
        repository already names -- for a tenant's cache, the project's public
        URL -- and nothing here is told a remote by a caller, so a submission
        cannot point this at a repository of its choosing. A fetch that fails or
        hangs is not an error in itself; it only means the commit stays unknown,
        which the caller is then told plainly.
        """
        refreshed = False
        for commit_git_dir in self.commit_git_dirs:
            try:
                git_args = self._commit_repo_git_args(commit_git_dir)
            except ValueError:
                continue
            try:
                fetched = subprocess.run(
                    [*git_args, "fetch", "--quiet", "--prune", "origin"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=COMMIT_REFRESH_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                continue
            refreshed = refreshed or fetched.returncode == 0
        return refreshed

    def _commit_repo_git_args(self, commit_git_dir: Path) -> list[str]:
        if (commit_git_dir / ".git").exists():
            return ["git", "-C", str(commit_git_dir)]
        if not commit_git_dir.exists():
            raise ValueError(f"commit_hash verification repository not found: {commit_git_dir}")
        return ["git", f"--git-dir={commit_git_dir}"]

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

    def _build_screenshot_entries(self, paths: list[Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for item in paths:
            if isinstance(item, dict):
                path = str(item.get("path", ""))
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            else:
                path = str(item)
                metadata = {}
            entry: dict[str, Any] = {"path": path, "available": Path(path).is_file()}
            if metadata:
                entry["metadata"] = metadata
            entries.append(entry)
        return entries

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
        if state not in self._workflow_state_names():
            state = LEGACY_STATE_ALIASES.get(state, state)
        if state not in self._workflow_state_names():
            raise ValueError(f"invalid state: {state}")
        return state

    def _workflow_state_names(self) -> tuple[str, ...]:
        if self._workflow_states_cache is not None:
            return self._workflow_states_cache
        try:
            with self._pg_connect() as conn:
                rows = conn.execute("SELECT name FROM ticket_board.workflow_stages ORDER BY rank;").fetchall()
        except Exception as exc:  # noqa: BLE001
            if self.database_url:
                raise RuntimeError("could not load configured workflow states") from exc
            return STATES
        states = tuple(str(row["name"]) for row in rows)
        self._workflow_states_cache = states or STATES
        return self._workflow_states_cache

    def _validate_assignee(self, assignee: str) -> str:
        cfg = self.workflow_configuration() if assignee in LEGACY_ASSIGNEE_ALIASES else None
        if not cfg or not any(r["name"] == assignee for r in cfg["roles"]):
            assignee = LEGACY_ASSIGNEE_ALIASES.get(assignee, assignee)
        if assignee not in ASSIGNEES and assignee not in {r["name"] for r in (self.workflow_configuration() or {}).get("roles", [])}:
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
