"""Ticket-board storage and validation."""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import attachment_store, commit_cache, extension_operations, new_asset_files, workflow_locking
from .commit_cache import COMMIT_REFRESH_TIMEOUT_SECONDS, PUBLISHABLE_REF, PUBLISHED_REF_NAMESPACE
from .commit_repos import commit_git_dirs_for_project
from .image_asset_policy import (  # re-exported: callers import these from the app
    IMAGE_EXTENSIONS,
    crop_filename_slug,
    format_timestamp,
    upload_set_slug,
    uploaded_filename_slug,
)
from .ticket_read_query import select_ticket_rows
from .ticket_input_policy import (
    EXTERNAL_BLOCKER_PATTERN,
    TICKET_ID_PATTERN,
    enforce_blocked_reason_rule,
    is_external_blocker,
    normalize_blocker_ref,
    require_body,
    require_plain_string,
    require_text,
    valid_ticket_id,
    validate_blocked_by,
    validate_blockers,
    validate_comments,
)

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
TICKET_NUMBER_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*-([0-9]+)$")


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


def ticket_id_sentinel(prefix: str) -> str:
    return f"{prefix}-0"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ticket_number(ticket_id: str) -> int:
    value = str(ticket_id).strip().upper()
    match = TICKET_NUMBER_PATTERN.fullmatch(value)
    if not match:
        return 0
    return int(match.group(1))


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

    def workflow_configuration(self, conn: Any | None = None) -> dict[str, Any] | None:
        if not self.database_url:
            return None
        from .workflow_config import read_configuration
        if conn is not None:  # SYRD-572: inside a caller's transaction, never a second connection
            return read_configuration(conn)
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

    def serial_reservations(self) -> dict[str, dict[str, str] | None]:
        """Which ticket holds each implementer's one serial slot, None for a free one.

        The routing gate's own answer, read rather than recomputed: which stages
        hold depends on the reservation policy and on the kind each tenant gave
        its stages, and a report that worked it out separately could disagree
        with the gate that acts on it (SYRD-476).
        """
        with self._pg_connect() as conn:
            rows = conn.execute(
                "SELECT implementer, ticket_id, state, assignee FROM ticket_board.serial_reservations()"
            ).fetchall()
        return {
            str(row["implementer"]): (
                {"ticket": str(row["ticket_id"]), "state": str(row["state"]), "assignee": str(row["assignee"])}
                if row["ticket_id"]
                else None
            )
            for row in rows
        }

    def apply_workflow(self, document: Any, *, expected_revision: int, dry_run: bool, caller_role: str) -> dict[str, Any]:
        from . import pull_queue
        from .workflow_config import validate
        if caller_role != "director":
            raise PermissionError("only director may configure workflow")
        cfg = validate(document, project=self.project)
        # SYRD-572: serialized first, every lock wait bounded, retried, refused plainly.
        result = workflow_locking.apply_workflow_bounded(
            self._pg_connect, cfg, expected_revision=expected_revision, dry_run=dry_run, caller_role=caller_role,
            set_caller_role=self._pg_set_caller_role, reservations=pull_queue.reservations,
            reservation_changes=pull_queue.reservation_changes, attempts=workflow_locking.ATTEMPTS,
            table_lock_wait_seconds=workflow_locking.TABLE_LOCK_WAIT_SECONDS, statement_seconds=workflow_locking.STATEMENT_SECONDS)
        self._workflow_states_cache = None
        return result

    def set_workflow_flags(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
        with self._pg_connect() as conn:
            self._pg_set_caller_role(conn, caller_role)
            self._pg_call(conn, "SELECT ticket_board.set_declared_flags(%s,%s::jsonb)", (ticket_id,json.dumps(patch)))
            return self._pg_get_ticket(ticket_id,conn)

    def perform_workflow_action(self, ticket_id: str, action: str, payload: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
        payload = dict(payload)
        # SYRD-307: the kick-back commands' own keys, in the declared schema.
        # An Inspector's recommendations are its reason.
        if "recommendations" in payload and not str(payload.get("reason") or payload.get("text") or "").strip():
            payload["reason"] = payload["recommendations"]
        payload.pop("recommendations", None)
        # A declared kick-back is a `return`: the executor sends the ticket to
        # its recorded implementer and ignores any other request, so a target
        # cannot be chosen -- and letting a reviewer choose one would be new
        # authority. --target-assignee is therefore a confirmation: accepted
        # when it names exactly where the return goes, refused before anything
        # changes when it does not. Any other action has no target to name.
        target = str(payload.pop("target_assignee", "") or "").strip().lower()
        if target:
            cfg = self.workflow_configuration()
            ticket = self.get_ticket(ticket_id)
            transition = next((t for t in (cfg or {}).get("transitions", [])
                               if t["from"] == ticket["state"] and t["action"] == action), None)
            if transition is None or transition.get("primitive") != "return":
                raise ValueError(f"{action} takes no target assignee; the ticket goes where the workflow sends it")
            owners = next((s.get("owners") or [] for s in cfg["stages"] if s["name"] == transition["to"]), [])
            with self._pg_connect() as conn:
                row = conn.execute("SELECT last_implementer_assignee FROM ticket_board.ticket_notification_state "
                                   "WHERE ticket_id = %s", (ticket_id,)).fetchone()
            recorded = str((row["last_implementer_assignee"] if isinstance(row, dict) else row[0]) or "") if row else ""
            if recorded in owners:
                returns_to = recorded
            elif ticket["assignee"] in owners:
                returns_to = ticket["assignee"]
            else:
                returns_to = owners[0] if owners else ""
            if target != returns_to:
                raise ValueError(
                    f"{action} returns {ticket_id} to its recorded implementer, {returns_to or 'nobody'}; it cannot "
                    f"send it to {target}. Kick it back without --target-assignee, then have the Director reassign it"
                )
        if set(payload) - {"target", "assignee", "commit_hash", "text", "reason"}:
            raise ValueError("unknown workflow action payload field")
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
            from .workflow_config import advertised_transitions
            for ticket in tickets:
                ticket["workflow_actions"] = advertised_transitions(cfg, ticket)
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
        return attachment_store.list_frames(self.frame_dir)

    def resolve_image(self, raw_path: str) -> Path:
        return attachment_store.resolve_image(raw_path, self.frame_dir, self.asset_dir)

    def save_uploaded_image(
        self,
        raw_bytes: bytes,
        *,
        upload_set: str = "",
        set_label: str = "",
        attempt_number: str = "",
        original_filename: str = "",
    ) -> dict[str, str]:
        return attachment_store.save_uploaded_image(
            self.asset_dir,
            raw_bytes,
            upload_set=upload_set,
            set_label=set_label,
            attempt_number=attempt_number,
            original_filename=original_filename,
        )

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

        # SYRD-520: the crop is kept only if the connection's exit commits it
        # (psycopg commits there, then closes without raising); a refusal or a
        # failed commit removes it.
        with new_asset_files.discarded_on_failure() as new_files:
            destination, metadata = attachment_store.write_crop(
                source,
                self.asset_dir,
                ticket,
                rect,
                feedback_number=feedback_number,
                set_label=set_label,
                normalized_source=normalized_source,
                new_files=new_files,
            )
            with self._pg_connect() as conn:
                self._pg_set_caller_role(conn, caller_role or "director")
                self._pg_call(
                    conn,
                    "SELECT ticket_board.append_ticket_attachment(%s, %s, %s::jsonb);",
                    (ticket_id, str(destination.resolve()), json.dumps(metadata)),
                )
                return self._pg_get_ticket(ticket_id, conn)

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
        if not commit_cache.readable_commit_repos(self.commit_git_dirs):
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
            f"{ref} is not published: {commit_cache.published_ref_absence(self.commit_git_dirs, ref, commit)} "
            "Nothing was recorded and the request is still open -- publish the ref and record "
            "the outcome again, or reject the request with a reason."
        )

    def published_ref_commit(self, ref: str) -> str:
        """Resolve one published ref in the tenant's trusted commit cache.

        Local git against the root-configured repositories and nothing else: no
        remote is named, contacted, or taken at its word, so this stays safe to
        re-run and cannot be pointed at a remote of the caller's choosing.
        """
        return commit_cache.published_ref_commit(self.commit_git_dirs, ref)

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

    @contextlib.contextmanager
    def _attachment_transaction(self, conn: Any) -> Iterator[new_asset_files.NewAssetFiles]:
        """`conn.transaction()` for operations that write attachment files (SYRD-520).

        Files the operation creates in the asset directory are kept only if the
        transaction commits; if anything raises first -- a refusal, a failed
        copy, the commit itself -- exactly those files are removed. Nothing
        after the commit can remove them.
        """
        with new_asset_files.discarded_on_failure() as new_files:
            with conn.transaction():
                yield new_files

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


    def _pg_list_tickets(self) -> list[dict[str, Any]]:
        with self._pg_connect() as conn:
            rows = select_ticket_rows(conn)
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
        # SYRD-572: stages before tickets -- a workflow apply's order -- or the two wait on each other.
        # Kept here, not re-read from the shared cache: another request may empty it meanwhile.
        state_names = self._workflow_state_names(conn)
        rows = select_ticket_rows(conn, ticket_id=ticket_id)
        if not rows:
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        ticket = self._pg_row_to_ticket(rows[0], conn, state_names)
        from .workflow_config import read_configuration, advertised_transitions
        cfg = read_configuration(conn)
        if cfg:
            ticket["workflow_actions"] = advertised_transitions(cfg, ticket)
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

    def _pg_row_to_ticket(self, row: dict[str, Any], conn: Any | None = None,
                          state_names: tuple[str, ...] | None = None) -> dict[str, Any]:
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
            "title": require_text(row["title"], "title"),
            "body": require_body(row["body"]),
            "assignee": self._validate_assignee(str(row["assignee"]), conn),
            "state": self._validate_state(str(row["state"]), conn, state_names),
            "blocked_by": validate_blocked_by(list(row["blocked_by"] or []), str(row["id"]), self.ticket_prefix),
            "blockers": validate_blockers(blockers, str(row["id"]), self.ticket_prefix),
            "parent_id": str(row["parent_id"] or ""),
            "origin_project": require_plain_string(row["origin_project"], "origin_project"),
            "external_source_ref": require_plain_string(row["external_source_ref"], "external_source_ref"),
            "blocked_reason": require_plain_string(row["blocked_reason"], "blocked_reason"),
            "queued_for_assignee": require_plain_string(row["queued_for_assignee"], "queued_for_assignee"),
            "queued_behind_ticket": require_plain_string(row["queued_behind_ticket"], "queued_behind_ticket"),
            "implementation": require_plain_string(row["implementation"], "implementation"),
            "audit_prompt": require_plain_string(row["audit_prompt"], "audit_prompt"),
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
            "created": require_text(row["created_text"], "created"),
            "updated": require_text(row["updated_text"], "updated"),
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
            # Fields extension modules own: SYRD-537's snooze, SYRD-541's size review.
            **extension_operations.ticket_fields(row),
            "comments": validate_comments(comments),
        }
        attachment_store.set_screenshot_fields(ticket, attachment_store.screenshot_entries(list(screenshots)))
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
        title = require_text(title, "title").strip()
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
        implementation = require_plain_string(implementation, "implementation")
        audit_prompt = require_plain_string(audit_prompt, "audit_prompt")
        if audit_prompt.strip():
            raise ValueError("postgres function API does not support audit_prompt writes yet")
        if audit_signoff or inspector_signoff or user_signoff:
            raise ValueError("postgres function API does not support initial signoff fields yet")
        if commit_hash or commit_exempt:
            raise ValueError("postgres function API does not support initial commit fields yet")
        normalized_comments = validate_comments(comments)
        blocked_by = validate_blocked_by(blocked_by or [], ticket_id_sentinel(self.ticket_prefix), self.ticket_prefix)
        blocked_reason = require_plain_string(blocked_reason, "blocked_reason")
        enforce_blocked_reason_rule(blocked_by, blocked_reason)

        with self._pg_connect() as conn:
            with self._attachment_transaction(conn) as new_files:
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
                    attachment_store.materialize_edit_field_attachments(attachment_patch, ticket_id, current, self.frame_dir, self.asset_dir, new_files)
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
        title = require_text(title, "title").strip()
        body = str(body or "")
        origin_project = require_plain_string(origin_project, "origin_project").strip()
        external_source_ref = require_plain_string(external_source_ref, "external_source_ref").strip()
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
            with self._attachment_transaction(conn) as new_files:
                if caller_role:
                    self._pg_set_caller_role(conn, caller_role)
                current = self._pg_get_ticket(ticket_id, conn)
                edit_fields = self._pg_edit_field_patch(patch)
                patch = {key: value for key, value in patch.items() if key not in edit_fields}
                if edit_fields:
                    attachment_store.materialize_edit_field_attachments(edit_fields, ticket_id, current, self.frame_dir, self.asset_dir, new_files)
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
                    blocked_by = validate_blocked_by(patch.get("blocked_by", current["blocked_by"]), ticket_id, self.ticket_prefix)
                    blocked_reason = require_plain_string(
                        patch.get("blocked_reason", current["blocked_reason"]),
                        "blocked_reason",
                    )
                    enforce_blocked_reason_rule(blocked_by, blocked_reason)
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
        reason = require_text(reason, "reason").strip()
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
        reason = require_text(reason, "reason").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.request_commit_exempt(%s, %s);", (ticket_id, reason))
                return self._pg_get_ticket(ticket_id, conn)

    def implementer_kick_back(self, ticket_id: str, reason: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        reason = require_text(reason, "reason").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.implementer_kick_back(%s, %s);", (ticket_id, reason))
                return self._pg_get_ticket(ticket_id, conn)

    def start_task(self, ticket_id: str, note: str = "", *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        note = require_plain_string(note, "note").strip()
        with self._pg_connect() as conn:
            with conn.transaction():
                self._pg_set_caller_role(conn, caller_role)
                self._pg_call(conn, "SELECT ticket_board.start_task(%s, %s);", (ticket_id, note))
                return self._pg_get_ticket(ticket_id, conn)

    def complete_task(self, ticket_id: str, completion_note: str, *, caller_role: str) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        completion_note = require_text(completion_note, "completion_note").strip()
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
        awaiting_role = require_text(awaiting_role, "awaiting_role").strip().lower()
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
        ticket_id = require_text(ticket_id, "ticket_id").strip().upper()
        target_role = require_text(target_role, "target_role").strip().lower()
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
        if commit_cache.refresh_commit_repos(self.commit_git_dirs):
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
        """The commit as this board's own copies of the repository resolve it.

        Kept on the app (SYRD-502): tests stub it per instance so a submission
        never reaches the host's real commit repositories.
        """
        return commit_cache.resolve_known_commit(self.commit_git_dirs, value)

    def _validate_state(self, state: str, conn: Any | None = None, names: tuple[str, ...] | None = None) -> str:
        names = names if names is not None else self._workflow_state_names(conn)
        if state not in names:
            state = LEGACY_STATE_ALIASES.get(state, state)
        if state not in names:
            raise ValueError(f"invalid state: {state}")
        return state

    def _workflow_state_names(self, conn: Any | None = None) -> tuple[str, ...]:
        if self._workflow_states_cache is not None:
            return self._workflow_states_cache
        query = "SELECT name FROM ticket_board.workflow_stages ORDER BY rank;"
        try:
            if conn is not None:
                # SYRD-572: the caller's own transaction. A second connection here waited on a workflow
                # preview that was itself waiting on the caller -- a cycle PostgreSQL could not see.
                rows = conn.execute(query).fetchall()
            else:
                with self._pg_connect() as own:
                    rows = own.execute(query).fetchall()
        except Exception as exc:  # noqa: BLE001
            if self.database_url:
                raise RuntimeError("could not load configured workflow states") from exc
            return STATES
        states = tuple(str(row["name"]) for row in rows)
        self._workflow_states_cache = states or STATES
        return self._workflow_states_cache

    def _validate_assignee(self, assignee: str, conn: Any | None = None) -> str:
        cfg = self.workflow_configuration(conn) if assignee in LEGACY_ASSIGNEE_ALIASES else None
        if not cfg or not any(r["name"] == assignee for r in cfg["roles"]):
            assignee = LEGACY_ASSIGNEE_ALIASES.get(assignee, assignee)
        if assignee not in ASSIGNEES and assignee not in {r["name"] for r in (self.workflow_configuration(conn) or {}).get("roles", [])}:
            raise ValueError(f"invalid assignee: {assignee}")
        return assignee
