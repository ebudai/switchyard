#!/usr/bin/env python3
"""SYRD-285: a blocked_reason is a note, not a wait -- and the wait MEFP-14 needed exists.

MEFP-14's deployment waited on Switchyard's SYRD-284. The MEFP Director called
set_blockers with blocked_by=[] and a reason, was told it succeeded, and Ops
kept being reminded of unresolved work: nothing had been blocked. A reason
without a blocker row suppresses nothing, by design -- it is text.

This replays it on boards built the production way (companion roles,
schema.sql, the real ticket-board-migrate, rbac.sql, a declared workflow):

* on MEFP's installed release (49abeb4): the reason-only write lands and the
  owner is still reminded; `syrd:SYRD-284` is not accepted there at all;
* on this tree: the set_blockers operation refuses a reason with nothing to
  wait on, and says what to name instead; the reason as a note is unchanged;
* and the supported wait -- `syrd:SYRD-284` (SYRD-270) -- keeps MEFP-14 with
  Ops, silences reminders and handoffs, refuses submission, and on the
  Director's explicit release wakes Ops exactly once through the real listener.
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
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.write_client import TicketBoardWriteClient  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from workflow_document_eras import before_relaying  # noqa: E402

CHECKS = 0
INSTALLED = "49abeb4b00d47805e515af455ce506cb4ae41888"  # MEFP's release when this was reported
TICKET = "PGU-14"
OPS = "pgu-ops:0.0"
REASON = "Deployment waits on the independently audited publication of SYRD-284."
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts/ticket_board",
                              "scripts/ticket-board-migrate"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


class Board:
    def __init__(self, cluster, db: str, tree: Path) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        self.listener_url = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
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
        t.psql(self.admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        self.app._resolve_known_commit = lambda value: (COMMIT, [])
        document = copy.deepcopy(before_relaying())
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()
        t.seed_postgres_ticket(self.admin, TICKET, title="Deploy the fix", state="in_progress", assignee="ops")
        t.psql(self.admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        self.delivered: list[tuple[str, str]] = []

    def over_http(self, role: str, operation: str, payload: dict) -> tuple[bool, str]:
        server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = TicketBoardWriteClient(f"http://127.0.0.1:{server.server_port}", caller_role=role,
                                            write_token=server.write_token)
            try:
                client._ticket_action(TICKET, operation, payload, caller_role=role)
            except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
                return False, str(exc)
            return True, ""
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def reminders(self) -> list[str]:
        """Every reminder the board generates, for the owner and the Director, as overdue."""
        t.psql(self.admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        for role in ("ops", "director"):
            t.psql(self.listener_url, f"""
SELECT ticket_board.notify_idle_stall_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text), clock_timestamp());
SELECT ticket_board.notify_idle_turn_end_nudges(
    jsonb_build_object('{role}', (clock_timestamp() - interval '3 hours')::text),
    clock_timestamp(), interval '0 seconds', '{{}}'::jsonb);
""")
        t.psql(self.admin, "SELECT ticket_board.notify_due_nudges(clock_timestamp() + interval '6 hours');")
        raw = t.psql(self.admin, f"SELECT coalesce(jsonb_agg(kind || ':' || target_role), '[]')::text FROM "
                                 f"ticket_board.ticket_notification_queue WHERE ticket_id = '{TICKET}';")
        return json.loads(raw)

    def listen(self) -> None:
        t.psql(self.admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                           "WHERE dead_lettered_at IS NULL;")
        listener = TicketBoardNotifyListener(
            conninfo=self.listener_url, project="cerulean",
            sender=lambda target, message: self.delivered.append((target, message)),
            activity_gate=lambda _target: False, target_exists=lambda _target: True,
            submission_witness=lambda target, _since: any(sent == target for sent, _m in self.delivered),
        )
        with psycopg.connect(self.listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            listener.process_due_notifications(conn)

    def to_ops(self) -> list[str]:
        return [message for target, message in self.delivered if target == OPS and TICKET in message]


def refused(call) -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
        return str(exc)
    return ""


def run_installed(cluster, tmp: Path) -> None:
    """MEFP-14 on MEFP's installed release."""
    board = Board(cluster, "installed", tree_at(INSTALLED, tmp / "installed"))
    check(board.reminders(), "the probe arrives: an idle owner is reminded before anything is recorded")
    # The installed server's set_blockers built exactly this patch and applied it.
    ticket = board.app.update_ticket(TICKET, {"blocked_by": [], "blocked_reason": REASON}, caller_role="director")
    check(ticket["blocked_reason"] == REASON and ticket["blocked_by"] == [],
          f"reproduced: the reason-only write lands: {ticket['blocked_by']} {ticket['blocked_reason']!r}")
    check(board.reminders(), "reproduced: and Ops is still reminded -- nothing was blocked")
    external = refused(lambda: board.app.update_ticket(
        TICKET, {"blocked_by": ["syrd:SYRD-284"], "blocked_reason": REASON}, caller_role="director"))
    check("syrd:syrd-284" in external.lower() and ("invalid" in external or "not found" in external),
          f"and the installed release cannot record the cross-board wait: {external!r}")


def run_current(cluster) -> None:
    board = Board(cluster, "current", ROOT)

    # The trap is closed where it was sprung: the operation says what to name.
    ok, why = board.over_http("director", "set_blockers", {"blocked_by": [], "blocked_reason": REASON})
    check(not ok and "a blocked_reason alone is a note, not a wait" in why and "project:PREFIX-N" in why,
          f"set_blockers refuses a reason with nothing to wait on: {why!r}")
    check(board.app.get_ticket(TICKET)["blocked_reason"] == "", "and records nothing")
    ok, why = board.over_http("director", "set_blockers", {"blocked_by": [""], "blocked_reason": REASON})
    check(not ok and "a note, not a wait" in why, f"blank entries are nothing to wait on either: {why!r}")

    # The reason as a note is unchanged: it can be written, and it blocks nothing.
    noted = board.app.update_ticket(TICKET, {"blocked_reason": "context for whoever picks this up"},
                                    caller_role="director")
    check(noted["blocked_reason"] and noted["blocked_by"] == [], "a note can still be kept on the ticket")
    check(board.reminders(), "and, being a note, it suppresses nothing -- the documented semantic")

    # MEFP-14's supported wait: the cross-board blocker.
    ok, why = board.over_http("director", "set_blockers", {"blocked_by": ["syrd:SYRD-284"], "blocked_reason": REASON})
    check(ok, f"the Director records syrd:SYRD-284: {why!r}")
    ticket = board.app.get_ticket(TICKET)
    check((ticket["state"], ticket["assignee"], ticket["blocked_by"]) == ("in_progress", "ops", ["syrd:SYRD-284"]),
          f"MEFP-14 stays in_progress/ops, waiting on syrd:SYRD-284: {ticket['state']}/{ticket['assignee']} {ticket['blocked_by']}")
    check(board.reminders() == [], "no reminder, stall nudge, turn-end nudge or escalation while it waits")
    handoff = refused(lambda: board.app.request_dependency(TICKET, awaiting_role="director", reason="chase it",
                                                           caller_role="ops"))
    check("unresolved blocker prevents an awaiting-role handoff: syrd:SYRD-284" in handoff,
          f"Ops cannot reopen the Director handoff: {handoff!r}")
    submit = refused(lambda: board.app.perform_workflow_action(TICKET, "submit_to_audit", {"commit_hash": COMMIT},
                                                               caller_role="ops"))
    check("unresolved blocker prevents forward promotion: syrd:SYRD-284" in submit, f"and cannot go forward: {submit!r}")
    ok, why = board.over_http("ops", "set_blockers", {"blocked_by": [], "blocked_reason": ""})
    check(not ok and "cannot call set_blockers" in why, f"Ops still cannot touch blockers: {why!r}")

    # Released on purpose, once SYRD-284 is really published: Ops wakes once.
    t.psql(board.admin, "DELETE FROM ticket_board.ticket_notification_queue;")
    ok, why = board.over_http("director", "release_external_blocker",
                              {"ref": "syrd:SYRD-284", "reason": "SYRD-284 audited, published and deployed."})
    check(ok, f"the Director releases it: {why!r}")
    board.listen()
    check(len(board.to_ops()) == 1, f"Ops is woken exactly once: {board.to_ops()}")
    board.listen()
    check(len(board.to_ops()) == 1, "and nothing repeats")
    check(board.app.get_ticket(TICKET)["state"] == "in_progress", "nothing moved: Ops still submits through Audit")

    # Clearing stays what it was.
    ok, why = board.over_http("director", "set_blockers", {"blocked_by": [], "blocked_reason": ""})
    check(ok, f"clearing blockers and reason together is still allowed: {why!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd285.") as tmp:
        with temporary_cluster(prefix="syrd285-", shutdown="immediate") as cluster:
            run_installed(cluster, Path(tmp))
            run_current(cluster)
    print(f"reason_only_blocker_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
