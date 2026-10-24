"""The board's Oracle core: a remote Oracle database behind the same board server (SYRD-642, direction C).

`OracleBoardApp` stands where `TicketBoardApp` stands for PostgreSQL. It is
built by `cli.run_server` when `TICKET_BOARD_BACKEND=oracle`. The HTTP server,
the Unix socket and its process authority are the same code, so a remote
database never authenticates a caller. The board process does, from the
caller's local peer credentials and the runtime registrations it keeps here.

The core covers:

* reading tickets and the board;
* creating tickets;
* comments;
* every declared transition, decided by `workflow_engine` and applied with the
  ticket row locked (`SELECT … FOR UPDATE`);
* the Director's gate flags;
* the declared workflow document (read and apply);
* runtime registration for process authority;
* the pull claim, atomic with `FOR UPDATE SKIP LOCKED`;
* a durable notification queue written in the same transaction as each change.
  `oracle_listener` delivers it with bounded polling, with no push channel.

Everything else the PostgreSQL board does is refused with `Unsupported`, which
names the follow-on slice (docs/syrd-642-oracle-backend.md). Nothing is
pretended or silently skipped.

The database is reached as the steady-state account only: rows, no DDL.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import commit_cache, commit_repos, workflow_engine
from .oracle_config import OracleConfig, connect, create_pool
from .workflow_config import advertised_transitions, notification_role, pull_claimant_pool, unassigned_stage_owner, validate

#: How long a delivery claim stands before another listener pass may take the notice again.
CLAIM_TIMEOUT_SECONDS = 120


class Unsupported(workflow_engine.Unsupported):
    """Not in the Oracle core yet; nothing was changed."""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _json(value: Any, default: Any = None) -> Any:
    """A JSON column's value: python-oracledb returns an `IS JSON` column already parsed, or as text."""
    if value is None:
        return default
    return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    return _text(value)


def _rows(cursor: Any) -> list[dict[str, Any]]:
    names = [column[0].lower() for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def clobs(cursor: Any, *names: str) -> Any:
    """Declare these binds as CLOBs: thin mode otherwise binds text as VARCHAR2, which stops at 32 KB."""
    import oracledb

    cursor.setinputsizes(**{name: oracledb.DB_TYPE_CLOB for name in names})
    return cursor


def enqueue(cursor: Any, ticket_id: str, kind: str, target_role: str, message: str, payload: dict[str, Any],
            dedupe_key: str) -> None:
    """A durable notice, written in the caller's transaction: one per dedupe key, refreshed when queued again.

    An update, then an insert when there was nothing to update: Oracle refuses a large text bound twice in
    one statement, which a MERGE needs. Two writers racing on one key meet the unique key, and the loser
    refreshes the winner's row.
    """
    import oracledb

    binds = dict(dedupe=dedupe_key, message=message, payload=json.dumps(payload))
    refresh = ("UPDATE notification_queue SET message = :message, payload = :payload, next_attempt_at = SYSTIMESTAMP, "
               "claimed_at = NULL WHERE dedupe_key = :dedupe")
    clobs(cursor, "message", "payload").execute(refresh, **binds)
    if cursor.rowcount == 0:
        try:
            clobs(cursor, "message", "payload").execute(
                "INSERT INTO notification_queue (ticket_id, kind, target_role, message, payload, dedupe_key, "
                "next_attempt_at, created_at) VALUES (:ticket, :kind, :target, :message, :payload, :dedupe, "
                "SYSTIMESTAMP, SYSTIMESTAMP)", ticket=ticket_id, kind=kind, target=target_role, **binds)
        except oracledb.IntegrityError:
            clobs(cursor, "message", "payload").execute(refresh, **binds)
    cursor.execute("INSERT INTO notification_trace (ts, ticket_id, target_role, kind, event, detail) "
                   "VALUES (SYSTIMESTAMP, :ticket, :target, :kind, 'enqueue', :detail)",
                   ticket=ticket_id, target=target_role, kind=kind, detail=dedupe_key[:4000])


def jsonb_text(value: dict[str, Any]) -> str:
    """`value` as PostgreSQL prints a jsonb object: keys shortest first, then bytewise, ", " and ": " between."""
    keys = sorted(value, key=lambda k: (len(k.encode()), k.encode()))
    return "{" + ", ".join(f"{json.dumps(k)}: {json.dumps(value[k])}" for k in keys) + "}"


def add_comment(cursor: Any, ticket_id: str, who: str, text: str, urgent: bool = False) -> int:
    """Append a comment at the ticket's next position (the ticket row is locked by the caller); returns it."""
    position = int(cursor.execute("SELECT count(*) FROM ticket_comments WHERE ticket_id = :id",
                                  id=ticket_id).fetchone()[0])
    clobs(cursor, "text").execute("INSERT INTO ticket_comments (ticket_id, position, who, text, urgent, created_at) "
                                  "VALUES (:t, :p, :who, :text, :urgent, SYSTIMESTAMP)", t=ticket_id, p=position,
                                  who=who, text=text, urgent=int(urgent))
    return position


def lock_implementer(cursor: Any, role: str) -> None:
    """Hold `role`'s implementer lock until the transaction ends, as PostgreSQL's advisory lock does.

    Every decision about which ticket a role is actively implementing takes it first, so two claims (or a
    claim and a route) for one role cannot both find it free. The row is created on first use; a racing
    creator meets the primary key and then waits on the row like everyone else.
    """
    import oracledb

    try:
        cursor.execute("INSERT INTO implementer_locks (role) VALUES (:r)", r=role)
    except oracledb.IntegrityError:
        pass
    cursor.execute("SELECT role FROM implementer_locks WHERE role = :r FOR UPDATE", r=role).fetchone()


class OracleBoardApp:
    store_backend = "oracle"

    def __init__(self, frame_dir: Path, asset_dir: Path, config: OracleConfig, *, commit_git_dir: Any = None,
                 project: str | None = None, project_name: str | None = None, ticket_prefix: str | None = None,
                 connector: Callable[[OracleConfig], Any] | None = None) -> None:
        """`connector` replaces the session pool with one connection per call (the pool is the default)."""
        import os

        self.frame_dir = Path(frame_dir).resolve()
        self.asset_dir = Path(asset_dir).expanduser().resolve()
        self.config = config
        self._connector = connector
        self._pool = None if connector else create_pool(config)
        meta = self._meta()
        self.project = project or os.environ.get("TICKET_BOARD_PROJECT", "").strip() or meta.get("project", "")
        self.project_name = project_name or self.project
        self.ticket_prefix = (ticket_prefix or meta.get("ticket_prefix") or self.project or "BOARD").upper()
        explicit = (Path(str(commit_git_dir)),) if commit_git_dir else ()
        self.commit_git_dirs = explicit or commit_repos.commit_git_dirs_for_project(project=self.project)
        self._workflow_cache: tuple[int, dict[str, Any]] | None = None

    # -- connection -------------------------------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        conn = self._pool.acquire() if self._pool else self._connector(self.config)
        try:
            yield conn
        finally:
            try:
                conn.rollback()  # nothing uncommitted outlives its operation, in the pool or out of it
            except Exception:  # noqa: BLE001 -- a broken session: the pool drops it on release
                pass
            if self._pool:
                self._pool.release(conn)
            else:
                conn.close()

    def close(self) -> None:
        """Close the session pool, so the board holds no sessions once it stops."""
        if self._pool:
            self._pool.close(force=True)
            self._pool = None

    def _meta(self) -> dict[str, str]:
        with self._connect() as conn:
            return {k: _text(v) for k, v in conn.cursor().execute("SELECT k, v FROM board_meta")}

    # -- workflow ---------------------------------------------------------------------------------------------

    def workflow_configuration(self, conn: Any | None = None) -> dict[str, Any] | None:
        if conn is None:
            with self._connect() as own:
                return self.workflow_configuration(own)
        row = conn.cursor().execute("SELECT revision, document FROM workflow_document WHERE id = 1").fetchone()
        if row is None:
            return None
        revision = int(row[0])
        if self._workflow_cache is None or self._workflow_cache[0] != revision:
            self._workflow_cache = (revision, validate(_json(row[1]), project=self.project or None))
        return self._workflow_cache[1]

    def workflow_document(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.cursor().execute("SELECT revision FROM workflow_document WHERE id = 1").fetchone()
            return {"revision": int(row[0]) if row else 0, "document": self.workflow_configuration(conn)}

    def workflow_roles(self) -> list[str]:
        """The declared roles; before a workflow is declared, the built-in ones, as the PostgreSQL board answers."""
        from .app import CALLER_ROLES

        cfg = self.workflow_configuration()
        return [r["name"] for r in cfg["roles"] if r["active"]] if cfg else list(CALLER_ROLES)

    def workflow_columns(self) -> list[dict[str, str]]:
        cfg = self.workflow_configuration()
        return [{"key": s["name"], "label": s["label"]} for s in cfg["stages"]] if cfg else []

    def apply_workflow(self, document: Any, *, expected_revision: int, dry_run: bool, caller_role: str) -> dict[str, Any]:
        if caller_role != "director":
            raise PermissionError("only director may configure workflow")
        cfg = validate(document, project=self.project or None)
        for declared, name in ((cfg.get("user_waits") is not None, "user_waits (waits on the User)"),
                               ("cohorts" in (cfg.get("scheduling") or {}), "scheduling cohorts (claim ceilings)")):
            if declared:  # each would be accepted and then silently not applied
                raise Unsupported(f"{name} is not in the Oracle core yet; the workflow was not changed")
        stages = {s["name"] for s in cfg["stages"]}
        with self._connect() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT revision FROM workflow_document WHERE id = 1 FOR UPDATE").fetchone()
            current = int(row[0]) if row else 0
            if current != expected_revision:
                raise ValueError("workflow revision changed; reread before applying")
            stranded = [r[0] for r in cursor.execute("SELECT DISTINCT state FROM tickets") if r[0] not in stages]
            if stranded:
                raise ValueError(f"configuration omits stages that hold tickets: {', '.join(sorted(stranded))}")
            if dry_run:
                conn.rollback()
                return {"revision": current, "dry_run": True, "document": cfg}
            # An update or an insert, never a MERGE: Oracle refuses a large text bound twice in one statement.
            # The row is locked above (or absent, and its primary key admits one insert).
            sql = ("UPDATE workflow_document SET revision = :rev, document = :doc, updated_at = SYSTIMESTAMP "
                   "WHERE id = 1") if row else ("INSERT INTO workflow_document (id, revision, document, updated_at) "
                                                "VALUES (1, :rev, :doc, SYSTIMESTAMP)")
            clobs(cursor, "doc").execute(sql, rev=current + 1, doc=json.dumps(cfg))
            conn.commit()
        self._workflow_cache = None
        return {"revision": current + 1, "dry_run": False, "document": cfg}

    # -- reads ------------------------------------------------------------------------------------------------

    def _ticket_rows(self, cursor: Any, ticket_id: str | None = None, *, lock: bool = False) -> list[dict[str, Any]]:
        """Every ticket, or one; `lock` holds that row until the transaction ends."""
        cursor.execute("SELECT * FROM tickets" + (" WHERE id = :id" if ticket_id else "") + " ORDER BY ticket_number"
                       + (" FOR UPDATE" if lock else ""), **({"id": ticket_id} if ticket_id else {}))
        return _rows(cursor)

    def _shape(self, cfg: dict[str, Any] | None, row: dict[str, Any], comments: list[dict[str, Any]],
               blockers: list[dict[str, Any]]) -> dict[str, Any]:
        flags = _json(row["flags"], {})
        state = self._state(row, blockers)
        builtins = workflow_engine.builtin_columns(state, cfg) if cfg else {}
        custom = {k: v for k, v in flags.items() if k not in builtins}
        ticket = {
            "id": row["id"], "title": _text(row["title"]), "body": _text(row["body"]), "state": row["state"],
            "assignee": row["assignee"], "commit_hash": _text(row["commit_hash"]),
            "commit_exempt": bool(row["commit_exempt"]), "manually_controlled": bool(row["manually_controlled"]),
            "parked": bool(row["parked"]), "parent_id": _text(row["parent_id"]),
            "implementation": _text(row["implementation"]), "audit_prompt": _text(row["audit_prompt"]),
            "created": _iso(row["created_at"]), "updated": _iso(row["updated_at"]),
            "blocked_by": [b["blocker"] for b in blockers if not b["resolved"]],
            "blockers": [{"ticket_id": b["blocker"], "resolved": bool(b["resolved"])} for b in blockers],
            "blocked_reason": "", "comments": comments, "workflow_flags": custom, **builtins,
            "queued_for_assignee": _text(row.get("queued_for_assignee")), "queued_behind_ticket": "",
            "awaiting_role": "", "regression": False, "origin_project": "", "external_source_ref": "",
            "screenshot": "", "screenshots": [], "screenshots_info": [], "screenshot_available": False,
            "store_backend": "oracle",
        }
        if cfg:
            ticket["workflow_actions"] = advertised_transitions(cfg, ticket)
        return ticket

    @staticmethod
    def _state(row: dict[str, Any], blockers: list[dict[str, Any]]) -> workflow_engine.TicketState:
        return workflow_engine.TicketState(
            id=row["id"], state=row["state"], assignee=row["assignee"], commit_hash=_text(row["commit_hash"]),
            commit_exempt=bool(row["commit_exempt"]), manually_controlled=bool(row["manually_controlled"]),
            parked=bool(row["parked"]), flags=_json(row["flags"], {}),
            blocked=any(not b["resolved"] for b in blockers), last_implementer=_text(row["last_implementer"]))

    def _comments(self, cursor: Any, ticket_ids: list[str], *, one: bool = False) -> dict[str, list[dict[str, Any]]]:
        """Comments by ticket; `one` reads only the single ticket named."""
        found: dict[str, list[dict[str, Any]]] = {t: [] for t in ticket_ids}
        cursor.execute("SELECT ticket_id, who, text, urgent, created_at FROM ticket_comments"
                       + (" WHERE ticket_id = :id" if one else "") + " ORDER BY ticket_id, position",
                       **({"id": ticket_ids[0]} if one else {}))
        for ticket_id, who, text, urgent, at in cursor:
            if ticket_id in found:
                found[ticket_id].append({"who": who, "text": _text(text), "urgent": bool(urgent), "ts": _iso(at)})
        return found

    def _blockers(self, cursor: Any, ticket_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        """Blockers by ticket: every ticket's, or only `ticket_id`'s."""
        found: dict[str, list[dict[str, Any]]] = {}
        cursor.execute("SELECT ticket_id, blocker, resolved FROM ticket_blockers"
                       + (" WHERE ticket_id = :id" if ticket_id else "") + " ORDER BY ticket_id, position",
                       **({"id": ticket_id} if ticket_id else {}))
        for ticket_id, blocker, resolved in cursor:
            found.setdefault(ticket_id, []).append({"blocker": blocker, "resolved": bool(resolved)})
        return found

    def get_ticket(self, ticket_id: str, conn: Any | None = None) -> dict[str, Any]:
        ticket_id = str(ticket_id).strip().upper()
        if conn is None:
            with self._connect() as own:
                return self.get_ticket(ticket_id, own)
        cursor = conn.cursor()
        rows = self._ticket_rows(cursor, ticket_id)
        if not rows:
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        cfg = self.workflow_configuration(conn)
        return self._shape(cfg, rows[0], self._comments(cursor, [ticket_id], one=True)[ticket_id],
                           self._blockers(cursor, ticket_id).get(ticket_id, []))

    def list_tickets(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cfg = self.workflow_configuration(conn)
                rows = self._ticket_rows(cursor)
                comments = self._comments(cursor, [r["id"] for r in rows])
                blockers = self._blockers(cursor)
                return [self._shape(cfg, r, comments[r["id"]], blockers.get(r["id"], [])) for r in rows], []
        except Exception as exc:  # noqa: BLE001 -- the board page shows a store error rather than dying
            return [], [{"file": "oracle", "error": str(exc).splitlines()[0]}]

    def snapshot(self) -> dict[str, object]:
        tickets, errors = self.list_tickets()
        cfg = self.workflow_configuration()
        columns = self.workflow_columns()
        active = [r["name"] for r in cfg["roles"] if r["active"]] if cfg else []
        return {"project": self.project, "project_name": self.project_name, "ticket_prefix": self.ticket_prefix,
                "tickets": tickets, "errors": errors, "states": [c["key"] for c in columns], "columns": columns,
                "assignees": active, "caller_roles": [r for r in active if r != "unassigned"], "workflow": cfg,
                "screenshots": [], "store_backend": "oracle", "store_path": self.config.endpoint,
                "frame_dir": str(self.frame_dir), "asset_dir": str(self.asset_dir),
                "refreshed_at": datetime.now(timezone.utc).isoformat()}

    def store_signature(self) -> tuple[tuple[object, ...], ...]:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT t.id, t.revision, t.state, t.assignee, t.commit_hash, "
                           "(SELECT count(*) FROM ticket_comments c WHERE c.ticket_id = t.id) "
                           "FROM tickets t ORDER BY t.id")
            return tuple(tuple(row) for row in cursor.fetchall())

    def verify_created_ticket_persisted(self, created: dict[str, object],
                                        before_signature: tuple[tuple[object, ...], ...]) -> tuple[tuple[object, ...], ...]:
        ticket_id = str(created.get("id", "")).strip()
        if not ticket_id or ticket_id in {str(row[0]) for row in before_signature}:
            raise ValueError(f"create returned a non-new ticket id: {ticket_id}")
        persisted = self.get_ticket(ticket_id)
        if persisted["title"] != str(created.get("title", "")).strip():
            raise ValueError(f"created ticket title mismatch in oracle: {ticket_id}")
        return self.store_signature()

    def serial_reservations(self) -> dict[str, dict[str, str] | None]:
        return {}

    def publication_requests(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    # -- writes -----------------------------------------------------------------------------------------------

    def _validate_commit_hash(self, raw: Any) -> str:
        value = str(raw or "").strip()
        if not value:
            return ""
        if not re.fullmatch(r"[0-9A-Fa-f]{7,40}", value):
            raise ValueError("commit_hash must be a 7-40 character hex commit")
        if not self.commit_git_dirs:
            return value.lower()
        resolved, _missing = commit_cache.resolve_known_commit(self.commit_git_dirs, value)
        if not resolved and commit_cache.refresh_commit_repos(self.commit_git_dirs):
            resolved, _missing = commit_cache.resolve_known_commit(self.commit_git_dirs, value)
        if not resolved:
            raise ValueError(f"unknown commit_hash: {value}. Push it to the project remote and submit it again.")
        return resolved

    def _notify_transition(self, cursor: Any, cfg: dict[str, Any], ticket_id: str, title: str, state: str,
                           assignee: str, revision: int, source_role: str | None) -> None:
        """The new owner's notice; never to the role that made the change, as PostgreSQL's queue trigger rules."""
        target = notification_role(cfg, state, assignee) or unassigned_stage_owner(cfg, state, assignee)
        if not target or target == source_role:
            return
        label = next((s["label"] for s in cfg["stages"] if s["name"] == state), state)
        enqueue(cursor, ticket_id, "transition", target, f"{ticket_id} -- {title} entered {label}",
                {"kind": "transition", "id": ticket_id, "state": state, "assignee": assignee,
                 "target_role": target}, f"transition:{ticket_id}:{revision}")

    def create_ticket_record(self, *, title: str, body: str, assignee: str, state: str,
                             blocked_by: list[str] | None = None, implementation: str = "", audit_prompt: str = "",
                             comments: list[dict[str, Any]] | None = None, parent_id: str = "",
                             commit_hash: str = "", commit_exempt: bool = False, caller_role: str | None = None,
                             **flags_and_rest: Any) -> dict[str, Any]:
        if blocked_by:
            raise Unsupported("creating a blocked ticket is not in the Oracle core yet (blockers slice)")
        if flags_and_rest.get("screenshots") or flags_and_rest.get("screenshot"):
            raise Unsupported("screenshots are not in the Oracle core yet (attachments slice)")
        title = str(title or "").strip()
        if not title:
            raise ValueError("title is required")
        commit = self._validate_commit_hash(commit_hash)
        with self._connect() as conn:
            cursor = conn.cursor()
            cfg = self.workflow_configuration(conn)
            if cfg is None:
                raise ValueError("this board has no declared workflow; apply one first")
            stage = next((s for s in cfg["stages"] if s["name"] == state), None)
            if stage is None:
                raise ValueError(f"invalid state: {state}")
            owners = stage["owners"]
            assignee = (assignee or "unassigned").strip().lower()
            if owners and assignee not in owners:
                raise ValueError(f"{assignee} does not own {state}")
            gates = {k: v for k, v in flags_and_rest.items() if k in cfg["flags"] and cfg["flags"][k]["kind"] == "gate"}
            flags = workflow_engine.initial_flags(cfg, gates)
            number = int(cursor.execute("SELECT ticket_number_seq.NEXTVAL FROM dual").fetchone()[0])
            ticket_id = f"{self.ticket_prefix}-{number}"
            clobs(cursor, "body", "flags", "impl", "audit_text").execute(
                "INSERT INTO tickets (id, ticket_number, title, body, state, assignee, commit_hash, commit_exempt, "
                "flags, parent_id, implementation, audit_prompt, last_implementer, revision, created_at, updated_at, "
                "entered_state_at) VALUES (:id, :num, :title, :body, :state, :assignee, :commit_hash, :exempt, :flags, "
                ":parent, :impl, :audit_text, :last, 1, SYSTIMESTAMP, SYSTIMESTAMP, SYSTIMESTAMP)",
                id=ticket_id, num=number, title=title, body=body or None, state=state, assignee=assignee,
                commit_hash=commit or None, exempt=int(bool(commit_exempt)), flags=json.dumps(flags),
                parent=(parent_id or None), impl=implementation or None, audit_text=audit_prompt or None,
                last=assignee if stage["kind"] == "implementation" else None)
            for comment in comments or []:
                add_comment(cursor, ticket_id, str(comment.get("who") or caller_role or "unassigned"),
                            str(comment.get("text") or ""), bool(comment.get("urgent")))
            self._notify_transition(cursor, cfg, ticket_id, title, state, assignee, 1,
                                    flags_and_rest.get("notification_source_role") or caller_role)
            conn.commit()
            return self.get_ticket(ticket_id, conn)

    def create_ticket(self, *, title: str, body: str, assignee: str, state: str = "analysis",
                      caller_role: str | None = None, **rest: Any) -> dict[str, Any]:
        """The server's create, as TicketBoardApp.create_ticket: a draft is created unassigned."""
        assignee = (assignee or "unassigned").strip().lower()
        if state == "draft" and assignee != "unassigned":
            raise ValueError("draft tickets cannot be created with an assignee; release and route the draft instead")
        return self.create_ticket_record(title=title, body=body, assignee=assignee, state=state,
                                         caller_role=caller_role, **rest)

    def update_ticket(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str | None = None) -> dict[str, Any]:
        if set(patch) != {"comment"}:
            raise Unsupported(f"{', '.join(sorted(set(patch) - {'comment'}))} is not in the Oracle core yet; "
                              "it takes comments and declared transitions")
        comment = patch["comment"] or {}
        text = str(comment.get("text") or "").strip()
        if not text:
            raise ValueError("comment text must be non-empty")
        who = str(comment.get("who") or caller_role or "").strip()
        ticket_id = str(ticket_id).strip().upper()
        with self._connect() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT title, state, assignee, revision FROM tickets WHERE id = :id FOR UPDATE",
                                 id=ticket_id).fetchone()
            if row is None:
                raise FileNotFoundError(f"ticket not found: {ticket_id}")
            position = add_comment(cursor, ticket_id, who, text, bool(comment.get("urgent")))
            cursor.execute("UPDATE tickets SET updated_at = SYSTIMESTAMP, revision = revision + 1 WHERE id = :id",
                           id=ticket_id)
            title, state, assignee, _revision = row
            cfg = self.workflow_configuration(conn)
            target = notification_role(cfg, state, assignee) if cfg else None
            if target and target != who:
                enqueue(cursor, ticket_id, "ticket_update", target,
                        f"{ticket_id} -- {title} (in {state}, that you own) was changed by {who}: new comment -- "
                        "re-read it before continuing.",
                        {"kind": "comment", "id": ticket_id, "state": state, "assignee": assignee,
                         "target_role": target}, f"comment:{ticket_id}:{position}")
            conn.commit()
            return self.get_ticket(ticket_id, conn)

    def perform_workflow_action(self, ticket_id: str, action: str, payload: dict[str, Any], *,
                                caller_role: str) -> dict[str, Any]:
        payload = dict(payload)
        if "recommendations" in payload and not str(payload.get("reason") or payload.get("text") or "").strip():
            payload["reason"] = payload["recommendations"]
        payload.pop("recommendations", None)
        if payload.pop("target_assignee", None):
            raise Unsupported("--target-assignee is not in the Oracle core yet; a return goes to its implementer")
        if set(payload) - {"target", "assignee", "commit_hash", "text", "reason"}:
            raise ValueError("unknown workflow action payload field")
        if "commit_hash" in payload:
            payload["commit_hash"] = self._validate_commit_hash(payload["commit_hash"])
            if not payload["commit_hash"] and action == "mark_done":
                del payload["commit_hash"]
        ticket_id = str(ticket_id).strip().upper()
        with self._connect() as conn:
            cursor = conn.cursor()
            cfg = self.workflow_configuration(conn)
            if cfg is None:
                raise ValueError("this board has no declared workflow")
            rows = self._ticket_rows(cursor, ticket_id, lock=True)
            if not rows:
                raise FileNotFoundError(f"ticket not found: {ticket_id}")
            before = rows[0]
            blockers = self._blockers(cursor, ticket_id).get(ticket_id, [])
            decision = workflow_engine.decide(cfg, self._state(before, blockers), action, caller_role, payload)
            after = decision.ticket
            moved = after.state != before["state"]
            clobs(cursor, "flags").execute(
                "UPDATE tickets SET state = :state, assignee = :assignee, commit_hash = :commit_hash, "
                "commit_exempt = :exempt, parked = :parked, flags = :flags, last_implementer = :last, "
                "revision = revision + 1, updated_at = SYSTIMESTAMP"
                + (", entered_state_at = SYSTIMESTAMP" if moved else "") + " WHERE id = :id",
                state=after.state, assignee=after.assignee, commit_hash=after.commit_hash or None,
                exempt=int(after.commit_exempt), parked=int(after.parked), flags=json.dumps(after.flags),
                last=after.last_implementer or None, id=ticket_id)
            revision = int(before["revision"]) + 1
            if decision.comment:
                add_comment(cursor, ticket_id, caller_role, decision.comment)
            if moved or after.assignee != before["assignee"]:
                self._notify_transition(cursor, cfg, ticket_id, _text(before["title"]), after.state, after.assignee,
                                        revision, caller_role)
            conn.commit()
            return self.get_ticket(ticket_id, conn)

    def set_workflow_flags(self, ticket_id: str, patch: dict[str, Any], *, caller_role: str) -> dict[str, Any]:
        if caller_role != "director":
            raise PermissionError("only director may set gates")
        ticket_id = str(ticket_id).strip().upper()
        with self._connect() as conn:
            cursor = conn.cursor()
            cfg = self.workflow_configuration(conn) or {"flags": {}}
            bad = sorted(k for k in patch if (cfg["flags"].get(k) or {}).get("kind") != "gate"
                         or not isinstance(patch[k], bool))
            if bad:
                raise ValueError(f"only declared gates take a boolean here: {', '.join(bad)}")
            row = cursor.execute("SELECT flags FROM tickets WHERE id = :id FOR UPDATE", id=ticket_id).fetchone()
            if row is None:
                raise FileNotFoundError(f"ticket not found: {ticket_id}")
            flags = {**_json(row[0], {}), **patch}
            clobs(cursor, "flags").execute("UPDATE tickets SET flags = :flags, revision = revision + 1, "
                                           "updated_at = SYSTIMESTAMP "
                           "WHERE id = :id", flags=json.dumps(flags), id=ticket_id)
            add_comment(cursor, ticket_id, "director", f"Updated workflow gates: {jsonb_text(patch)}")
            conn.commit()
            return self.get_ticket(ticket_id, conn)

    def claim_next(self, role: str) -> dict[str, Any]:
        """The pull claim, for `role` itself: the oldest ready ticket it may take, atomically, or why none."""
        role = str(role or "").strip().lower()
        with self._connect() as conn:
            cursor = conn.cursor()
            cfg = self.workflow_configuration(conn)
            policy = (cfg or {}).get("scheduling")
            if not policy:
                raise ValueError("this workflow does not use pull scheduling")
            if role not in pull_claimant_pool(cfg):
                raise PermissionError(f"role {role} cannot claim ready work")
            lock_implementer(cursor, role)
            implementation = next(s for s in cfg["stages"] if s["kind"] == "implementation")["name"]
            held = cursor.execute("SELECT id FROM tickets WHERE state = :impl AND assignee = :role AND parked = 0 "
                                  "AND manually_controlled = 0", impl=implementation, role=role).fetchone()
            if held:
                return {"claimed": False, "ticket": held[0], "reason": "holding"}
            claim_cursor = conn.cursor()
            claim_cursor.prefetchrows = 1  # lock only the row fetched
            claim_cursor.arraysize = 1
            claim_cursor.execute(
                "SELECT id, title, revision FROM tickets t WHERE state = :ready AND manually_controlled = 0 "
                "AND (queued_for_assignee IS NULL OR queued_for_assignee = :role) "
                "AND NOT EXISTS (SELECT 1 FROM ticket_blockers b WHERE b.ticket_id = t.id AND b.resolved = 0) "
                "ORDER BY CASE WHEN queued_for_assignee = :role THEN 0 ELSE 1 END, entered_state_at, ticket_number "
                "FOR UPDATE SKIP LOCKED", ready=policy["ready_stage"], role=role)
            row = claim_cursor.fetchone()
            claim_cursor.close()
            if row is None:
                conn.rollback()
                return {"claimed": False, "ticket": None, "reason": "nothing ready"}
            ticket_id, title, revision = row
            cursor.execute("UPDATE tickets SET state = :impl, assignee = :role, queued_for_assignee = NULL, "
                           "last_implementer = :role, revision = revision + 1, updated_at = SYSTIMESTAMP, "
                           "entered_state_at = SYSTIMESTAMP WHERE id = :id", impl=implementation, role=role,
                           id=ticket_id)
            cursor.execute("INSERT INTO pull_claims (ticket_id, role, via, claimed_at) VALUES (:t, :r, 'self', "
                           "SYSTIMESTAMP)", t=ticket_id, r=role)
            self._notify_transition(cursor, cfg, ticket_id, _text(title), implementation, role, int(revision) + 1,
                                    role)
            conn.commit()
            return {"claimed": True, "ticket": ticket_id, "via": "self"}

    # -- runtime registration (process authority) -------------------------------------------------------------

    _RUNTIME_COLUMNS = ("role, runtime, actual_target, worktree, session_dir, process_pid, process_start_time, "
                        "process_uid, generation")

    def _active_runtime(self, cfg: dict[str, Any] | None, row: dict[str, Any]) -> bool:
        role = next((r for r in (cfg or {}).get("roles", []) if r["name"] == row["role"]), None)
        return bool(role and role["active"] and role.get("runtime") == row["runtime"]
                    and role.get("target") == row["actual_target"])

    def runtime_targets(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            cfg = self.workflow_configuration(conn)
            cursor = conn.cursor()
            cursor.execute(f"SELECT {self._RUNTIME_COLUMNS} FROM role_runtime_assignments")
            return {row["role"]: row for row in _rows(cursor) if self._active_runtime(cfg, row)}

    def runtime_assignment(self, role: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT {self._RUNTIME_COLUMNS} FROM role_runtime_assignments WHERE role = :r",
                           r=role.strip().lower())
            rows = _rows(cursor)
            return rows[0] if rows else None

    def runtime_assignment_for_process(self, pid: int, start_time: int, uid: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            cfg = self.workflow_configuration(conn)
            cursor = conn.cursor()
            cursor.execute(f"SELECT {self._RUNTIME_COLUMNS} FROM role_runtime_assignments WHERE process_pid = :p "
                           "AND process_start_time = :s AND process_uid = :u", p=pid, s=start_time, u=uid)
            rows = [row for row in _rows(cursor) if self._active_runtime(cfg, row)]
            return rows[0] if rows else None

    def register_runtime_assignment(self, *, role: str, runtime: str, target: str, worktree: str, session_dir: str,
                                    process_pid: int, process_start_time: int, process_uid: int,
                                    expected_generation: int) -> dict[str, Any]:
        """Publish the process that may act as `role`, as ticket_board.register_role_runtime does."""
        role = role.strip().lower()
        if not target.split(":", 1)[0].startswith(f"{self.project}-"):
            raise ValueError("runtime target belongs to another project")
        if (not re.fullmatch(r"[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+", target) or not worktree.startswith("/")
                or not session_dir.startswith("/") or process_pid <= 1 or process_start_time <= 0 or process_uid < 0):
            raise ValueError("invalid role runtime registration")
        with self._connect() as conn:
            cfg = self.workflow_configuration(conn)
            if not self._active_runtime(cfg, {"role": role, "runtime": runtime, "actual_target": target}):
                raise ValueError("role/runtime/target is not active workflow configuration")
            cursor = conn.cursor()
            existing = cursor.execute("SELECT generation FROM role_runtime_assignments WHERE role = :r FOR UPDATE",
                                      r=role).fetchone()
            values = dict(r=role, rt=runtime, tg=target, wt=worktree, sd=session_dir, p=process_pid,
                          s=process_start_time, u=process_uid)
            if existing is None:
                cursor.execute("INSERT INTO role_runtime_assignments (role, runtime, actual_target, worktree, "
                               "session_dir, process_pid, process_start_time, process_uid, generation, registered_at) "
                               "VALUES (:r, :rt, :tg, :wt, :sd, :p, :s, :u, 1, SYSTIMESTAMP)", **values)
            elif int(existing[0]) != int(expected_generation):
                raise ValueError("runtime assignment generation changed")
            else:
                cursor.execute("UPDATE role_runtime_assignments SET runtime = :rt, actual_target = :tg, worktree = :wt, "
                               "session_dir = :sd, process_pid = :p, process_start_time = :s, process_uid = :u, "
                               "generation = generation + 1, registered_at = SYSTIMESTAMP WHERE role = :r", **values)
            conn.commit()
        return self.runtime_assignment(role) or {}


#: The board server's other operations, each refused on this backend with the same plain answer, never attempted.
UNSUPPORTED_OPERATIONS = (
    "route_ticket", "reassign_ticket", "release_draft", "director_edit_ticket", "request_publication",
    "resolve_publication", "force_move_ticket", "merge_tickets", "implementer_kick_back",
    "submit_to_audit_without_commit", "request_commit_exempt", "start_task", "complete_task",
    "recover_stalled_ticket", "release_external_blocker", "request_dependency", "set_awaiting_role",
    "clear_awaiting_role", "dismiss_notification", "dismiss_notification_by_key", "crop_attachment",
    "save_uploaded_image", "resolve_image",
)


def _unsupported(name: str) -> Callable[..., Any]:
    def refuse(self: OracleBoardApp, *_args: Any, **_kwargs: Any) -> Any:
        raise Unsupported(f"{name} is not in the Oracle core yet (docs/syrd-642-oracle-backend.md lists the "
                          "follow-on slices); nothing was changed")
    refuse.__name__ = name
    return refuse


for _name in UNSUPPORTED_OPERATIONS:
    setattr(OracleBoardApp, _name, _unsupported(_name))
