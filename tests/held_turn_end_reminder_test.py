#!/usr/bin/env python3
"""SYRD-517: a held ticket is not told, turn after turn, that its turn ended unresolved.

MEFP-114 was deliberately held (manually_controlled) awaiting an operator's
export. Its Director kept receiving "MEFP-114 is still yours and this turn
ended without resolving it" and then the grace escalation. Measured on a
board built as production builds one: the generator already skips a held
ticket (ticket_turn_is_resolved counts a hold as resolved), but a prompt or
escalation queued BEFORE the hold was still delivered after it -- and a busy
pane requeues it, so it surfaced at the next idle, turn after turn. The
listener's delivery check had no rule for these two kinds.

Each scenario runs on a board built the production way (companion roles,
schema.sql, the real ticket-board-migrate, rbac.sql, a declared workflow)
with the real generator and the real notify listener:

* from f9d4f10, the last main before this change -- code and schema both, in a
  child process -- the stale prompt and escalation are delivered after the
  hold (the defect, reproduced);
* from this tree: they are dropped, nothing else is lost, and once the hold is
  lifted the next unresolved turn is prompted and escalated as before.
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
BEFORE = "f9d4f10a5f43e7cca2583af11549df4e9bbd23f9"  # main before SYRD-517
HELD, CONTROL = "PGU-114", "PGU-115"
DIRECTOR = "pgu-director:0.0"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path, cluster_prefix: str) -> dict:
    """Everything observable, from the tree at `root` (its code and its SQL)."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as t
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    seen: dict = {}
    with temporary_cluster(prefix=cluster_prefix, shutdown="immediate") as cluster:
        db = "held"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        listener_url = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, """
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
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin},
                                capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = t.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        document = copy.deepcopy(before_relaying())
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()
        for ticket in (HELD, CONTROL):
            t.seed_postgres_ticket(admin, ticket, title=ticket, state="director_review", assignee="director",
                                   commit_hash="6d4ee1aa99147e8118f59e637be02b660d62d064", audit_signoff=True)
        t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        delivered: list[tuple[str, str]] = []

        def hold(ticket: str, on: bool) -> None:
            app.update_ticket(ticket, {"manually_controlled": on}, caller_role="director")

        def turn_ends(turn: str, later: str = "0 seconds") -> None:
            t.psql(listener_url, "SELECT ticket_board.notify_unresolved_turn_end("
                                 f"jsonb_build_object('director', '{turn}'), clock_timestamp() + interval '{later}', "
                                 "interval '10 minutes');")

        def queued(ticket: str) -> list[str]:
            return json.loads(t.psql(admin, "SELECT coalesce(jsonb_agg(kind ORDER BY id), '[]')::text FROM "
                                            f"ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                                            "AND dead_lettered_at IS NULL;"))

        def listen() -> list[str]:
            t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                          "WHERE dead_lettered_at IS NULL;")
            before = len(delivered)
            listener = TicketBoardNotifyListener(
                conninfo=listener_url, project="cerulean",
                sender=lambda target, message: delivered.append((target, message)),
                activity_gate=lambda _target: False, target_exists=lambda _target: True,
                submission_witness=lambda target, _since: any(sent == target for sent, _m in delivered),
            )
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                listener.process_due_notifications(conn)
            return [message for target, message in delivered[before:] if target == DIRECTOR]

        # Generation: a held ticket is not prompted; an unheld one is, then escalated.
        hold(HELD, True)
        turn_ends("turn-1")
        turn_ends("turn-1", "1 hour")
        seen["generated_held"] = queued(HELD)
        seen["generated_control"] = queued(CONTROL)
        # Pending delivery: CONTROL's prompt and escalation are queued; THEN it is held.
        hold(CONTROL, True)
        seen["delivered_after_hold"] = [m for m in listen() if CONTROL in m]
        seen["queued_after_hold"] = queued(CONTROL)
        # Unhold: the next unresolved turn is prompted, then escalated, as ever.
        hold(CONTROL, False)
        turn_ends("turn-2")
        seen["delivered_after_unhold"] = [m for m in listen() if CONTROL in m]
        turn_ends("turn-2", "1 hour")
        seen["escalated_after_unhold"] = [m for m in listen() if CONTROL in m]
        # And the held ticket was never delivered anything.
        seen["delivered_held"] = [m for _t, m in delivered if HELD in m]
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


# A role pane's own board identity (TICKET_BOARD_PROJECT and friends) would
# otherwise decide the listener's targets, and a scenario that delivers nothing
# passes "nothing delivered after the hold" for the wrong reason. Both runs --
# this process and the child -- get the same clean identity.
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")


def clean_board_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def main() -> int:
    clean_board_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        # Child: the scenario from another tree's code and SQL.
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd517b-")))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd517.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_board_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-2000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(before["generated_held"] == [], f"before: the generator already skipped the held ticket: {before}")
    check(before["generated_control"] == ["unresolved_turn_repair", "unresolved_turn"],
          f"before: the unheld one was prompted and escalated: {before['generated_control']}")
    check(len(before["delivered_after_hold"]) == 2
          and "still yours and this turn ended without resolving it" in before["delivered_after_hold"][0]
          and "grace period" in before["delivered_after_hold"][1],
          f"reproduced: both were delivered after the hold: {before['delivered_after_hold']}")

    after = scenario(ROOT, "syrd517a-")
    check(after["generated_held"] == [] and after["delivered_held"] == [],
          f"a held ticket is neither prompted nor delivered anything: {after}")
    check(after["generated_control"] == ["unresolved_turn_repair", "unresolved_turn"],
          f"the probe arrives: an unheld ticket is prompted and escalated: {after['generated_control']}")
    check(after["delivered_after_hold"] == [], f"what was queued before the hold is not delivered after it: {after}")
    check(after["queued_after_hold"] == [], f"and is dropped, not left to surface later: {after['queued_after_hold']}")
    check(len(after["delivered_after_unhold"]) == 1
          and "still yours and this turn ended without resolving it" in after["delivered_after_unhold"][0],
          f"after the hold is lifted, the next unresolved turn is prompted: {after['delivered_after_unhold']}")
    check(len(after["escalated_after_unhold"]) == 1 and "grace period" in after["escalated_after_unhold"][0],
          f"and escalated after grace: {after['escalated_after_unhold']}")
    print(f"held_turn_end_reminder_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
