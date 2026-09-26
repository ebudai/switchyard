#!/usr/bin/env python3
"""SYRD-276: a tenant can keep an implementer reserved for a ticket's whole life.

MEFP wants the implementer of a bug reserved through Audit, DAT, User UAT and
the Director's close, so a kickback can never leave two active tickets on one
worker. Measured on boards built the production way, the reservation already
held through every review stage -- but MEFP's User kickback, `user_reopen`,
sends the ticket to analysis, which released the implementer, and the
serial-focus wakeup told the Director a second ticket could be routed to them.

Every board here is built as provisioning builds one -- the companion roles,
schema.sql, the real `ticket-board-migrate`, rbac.sql, a declared workflow --
from the tree before this change (the reproduction) and from this tree, with
the default policy and with `"reservation": "lifecycle"`.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from workflow_document_eras import before_relaying  # noqa: E402

CHECKS = 0
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu964_syrd276_lifecycle_reservation.sql"
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"
A, B = "PGU-9", "PGU-10"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mefp_shaped(policy: str | None) -> dict:
    """The shipped document with MEFP's User kickback: user_reopen, user_review -> analysis."""
    document = copy.deepcopy(before_relaying())
    document["project"] = "cerulean"
    document.setdefault("reassign", {})
    document.setdefault("remove_stages", [])
    document["transitions"].append({
        "action": "user_reopen", "from": "user_review", "to": "analysis", "actors": ["user"],
        "primitive": "reopen", "owner_scoped": False, "allow_no_code": False, "clear_signoffs": [],
        "require_commit": False, "require_reason": True, "label": "Reopen",
    })
    if policy is not None:
        document["reservation"] = policy
    return document


def tree_before(into: Path) -> Path:
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                             str(MIGRATION.relative_to(ROOT))], check=True, capture_output=True, text=True).stdout.split()
    assert adding, "the migration is not committed; clone-based checks see only commits"
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^", "scripts/ticket_board",
                              "scripts/ticket-board-migrate"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


class Board:
    def __init__(self, cluster, db: str, tree: Path, document: dict) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        self.listener = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
""")
        try:
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, (tree / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin},
                                capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        self.applied = runner.stderr + runner.stdout
        t.psql(self.admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        self.app._resolve_known_commit = lambda value: (COMMIT, [])
        self.apply(document)
        t.seed_postgres_ticket(self.admin, A, title="Bug A", state="in_progress", assignee="ops", needs_user_signoff=True)
        t.seed_postgres_ticket(self.admin, B, title="Bug B", state="analysis", assignee="director",
                               needs_user_signoff=True)

    def apply(self, document: dict) -> None:
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()

    def act(self, ticket: str, action: str, payload: dict, role: str) -> dict:
        return self.app.perform_workflow_action(ticket, action, payload, caller_role=role)

    def reserved(self, implementer: str) -> str:
        return t.psql(self.admin, f"SELECT coalesce(ticket_board.ticket_current_reserved_ticket('{implementer}'), '-');").strip()

    def routable_notices(self, ticket: str) -> list[str]:
        t.psql(self.listener, "SELECT ticket_board.notify_serial_focus_queue_wakeups(clock_timestamp());")
        raw = t.psql(self.admin, f"SELECT coalesce(jsonb_agg(message ORDER BY id), '[]')::text FROM "
                                 f"ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")
        return [m for m in json.loads(raw) if "can be routed to" in m]

    def through_uat(self, ticket: str, owner: str) -> None:
        self.act(ticket, "submit_to_audit", {"commit_hash": COMMIT}, owner)
        self.act(ticket, "audit_sign_off", {"text": "approved"}, "audit")
        uat = self.act(ticket, "director_dat_sign_off", {"text": "ready for UAT"}, "director")
        check((uat["state"], uat["assignee"]) == ("user_review", "user"), f"{ticket} is in User UAT: {uat['state']}")

    def queue_b(self) -> dict:
        return self.act(B, "route", {"target": "in_progress", "assignee": "ops"}, "director")


def run_before(cluster, tmp: Path) -> None:
    """MEFP's gap, on the board as it shipped."""
    board = Board(cluster, "before", tree_before(tmp / "before"), mefp_shaped(None))
    board.through_uat(A, "ops")
    check(board.reserved("ops") == A, f"A holds ops through User UAT already: {board.reserved('ops')}")
    queued = board.queue_b()
    check((queued["state"], queued["queued_behind_ticket"]) == ("backlog", A), f"B queues behind A: {queued['state']}")
    reopened = board.act(A, "user_reopen", {"reason": "not what I asked for"}, "user")
    check((reopened["state"], board.reserved("ops")) == ("analysis", "-"),
          f"reproduced: the User's kickback releases ops: {reopened['state']} {board.reserved('ops')}")
    check(len(board.routable_notices(B)) == 1, "and the Director is told B can be routed to ops -- the competing ticket")


def run_default(cluster) -> None:
    """The default is unchanged -- and switching policy applies to tickets already in flight."""
    board = Board(cluster, "default", ROOT, mefp_shaped(None))
    check("apply pgu964_syrd276_lifecycle_reservation.sql" in board.applied, "the runner applied this migration")
    board.through_uat(A, "ops")
    board.queue_b()
    board.act(A, "user_reopen", {"reason": "not what I asked for"}, "user")
    check(board.reserved("ops") == "-", f"by default a reopen still releases, as before: {board.reserved('ops')}")
    stored = json.loads(t.psql(board.admin, "SELECT ticket_board.declared_workflow()::text;"))
    check("reservation" not in stored, "and a document that does not opt in is not rewritten")
    # MEFP opts in while A sits in analysis: the reservation is computed, so
    # it holds again at once -- no data migration, no second chance for B.
    board.apply({**mefp_shaped("lifecycle")})
    check(board.reserved("ops") == A, f"switching to lifecycle reserves ops for A at once: {board.reserved('ops')}")


def run_lifecycle(cluster) -> None:
    board = Board(cluster, "lifecycle", ROOT, mefp_shaped("lifecycle"))
    board.through_uat(A, "ops")
    check(board.reserved("ops") == A, "A holds ops through User UAT")
    queued = board.queue_b()
    check((queued["state"], queued["queued_behind_ticket"]) == ("backlog", A), f"B queues behind A: {queued['state']}")

    # The User's kickback: A leaves review for analysis, and still holds ops.
    reopened = board.act(A, "user_reopen", {"reason": "not what I asked for"}, "user")
    check((reopened["state"], board.reserved("ops")) == ("analysis", A),
          f"after the User's reopen A still holds ops: {reopened['state']} {board.reserved('ops')}")
    check(board.routable_notices(B) == [], "and nobody is told B can be routed to ops")
    still = board.app.get_ticket(B)
    check((still["state"], still["queued_for_assignee"], still["queued_behind_ticket"]) == ("backlog", "ops", A),
          f"B stays queued for ops behind A: {still['state']} {still['queued_for_assignee']} {still['queued_behind_ticket']}")

    # Back to the same worker, then an Audit kickback returns it to them again.
    back = board.act(A, "route", {"target": "in_progress", "assignee": "ops"}, "director")
    check((back["state"], back["assignee"]) == ("in_progress", "ops"), f"A goes back to ops: {back['state']}/{back['assignee']}")
    board.act(A, "submit_to_audit", {"commit_hash": COMMIT}, "ops")
    kicked = board.act(A, "audit_kick_back", {"text": "one more thing"}, "audit")
    check((kicked["state"], kicked["assignee"], board.reserved("ops")) == ("in_progress", "ops", A),
          f"Audit's kickback returns A to ops, still holding: {kicked['state']}/{kicked['assignee']}")

    # Only closing A frees ops.
    board.through_uat(A, "ops")
    board.act(A, "user_sign_off", {"text": "accepted"}, "user")
    check(board.reserved("ops") == A, "A still holds ops in the Director's final review")
    check(board.routable_notices(B) == [], "and B is still not offered")
    done = board.act(A, "mark_done", {"commit_hash": COMMIT}, "director")
    check((done["state"], board.reserved("ops")) == ("done", "-"), f"closing A frees ops: {done['state']}")
    check(len(board.routable_notices(B)) == 1, "and only now is the Director told B can go to ops")

    # Parking is the other way out; manual control never held.
    t.seed_postgres_ticket(board.admin, "PGU-11", title="Bug C", state="in_progress", assignee="main", needs_user_signoff=True)
    board.through_uat("PGU-11", "main")
    board.act("PGU-11", "user_reopen", {"reason": "again"}, "user")
    check(board.reserved("main") == "PGU-11", "C holds main in analysis")
    parked = board.act("PGU-11", "defer", {}, "director")
    check((parked["state"], board.reserved("main")) == ("backlog", "-"), f"parking C frees main: {parked['state']}")


def run_system_parking(cluster) -> None:
    """A parking stage is a shape, not a kind: one declared `system` still releases."""
    document = mefp_shaped("lifecycle")
    for stage in document["stages"]:
        if stage["name"] == "backlog":
            stage["kind"] = "system"
    board = Board(cluster, "system_parking", ROOT, document)
    board.through_uat(A, "ops")
    board.act(A, "user_reopen", {"reason": "again"}, "user")
    check(board.reserved("ops") == A, "A holds ops in analysis")
    parked = board.act(A, "defer", {}, "director")
    check((parked["state"], board.reserved("ops")) == ("backlog", "-"),
          f"parking A in a system-kind parking stage frees ops: {parked['state']} {board.reserved('ops')}")


def run_validation(cluster) -> None:
    for label, call in (
        ("the API's validator", lambda: validate(mefp_shaped("forever"))),
        ("the database", lambda: Board(cluster, "invalid", ROOT, mefp_shaped("forever"))),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
            refused = str(exc)
        else:
            refused = ""
        check("invalid reservation policy" in refused, f"{label} refuses an unknown policy: {refused[:200]!r}")
    check(validate(mefp_shaped("lifecycle"))["reservation"] == "lifecycle", "and accepts lifecycle")


def main() -> int:
    before_only = "--before" in sys.argv
    with tempfile.TemporaryDirectory(prefix="syrd276.") as tmp:
        with temporary_cluster(prefix="syrd276-", shutdown="immediate") as cluster:
            run_before(cluster, Path(tmp))
            if not before_only:
                run_default(cluster)
                run_lifecycle(cluster)
                run_system_parking(cluster)
                run_validation(cluster)
    print(f"lifecycle_reservation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
