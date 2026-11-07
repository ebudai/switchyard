#!/usr/bin/env python3
"""SYRD-746: a Director restores a mistaken reopen in one bounded step.

MEFP-922 (2026-11-07): a done, audited, publicly integrated ticket was reopened by mistake. The reopen cleared its
commit and sign-offs, as a reopen must, and nothing could put them back but two narrated overrides and a fresh
mark-done. On a production-built board (schema, rbac, every migration) with the shipped declared workflow, through the
real HTTP server:

* A done ticket with its commit and Audit's sign-off is reopened, with a reason: the board records the closed
  disposition it left.
* Refused: an implementer; the Director with no reason; a ticket never reopened.
* The Director's restore puts back the stage, owner, commit and sign-offs at once -- no review repeated, no sign-off
  that was not there -- and records it: a comment, a field-audit row per field, the record marked restored. A second
  restore is refused.
* Bounded: a ticket moved on since its reopen (routed to an implementer) cannot be restored, nor one carrying a new
  candidate, nor one with a sign-off of its own; a ticket reopened out of Cancelled restores to Cancelled.
* MEFP-922's own path: a narrated override out of Done is recorded too, and restores the same way.
* The window it uses cannot be abused: a write that names the restore window but carries another commit, or a
  sign-off the record does not hold, is refused by the database.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as fixture  # noqa: E402
from assigned_implementation_queue_test import build_board, workflow  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
COMMIT = "86fdf6d6a061cca0c4c44f820d1c15ffd498b2ff"  # MEFP-922's restored public commit, as a stand-in value


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Board:
    def __init__(self, cluster) -> None:
        self.admin = build_board(fixture, cluster, ROOT, "board")
        self.app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                          database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def act(self, ticket: str, action: str, caller: str = "director", **body) -> dict:
        """The answer, or the refusal as {"refused": status, "text": ...}: a refusal is a case's to check."""
        try:
            return fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{action}", body, caller=caller)
        except AssertionError as exc:
            return {"refused": str(exc)[:600]}

    def row(self, ticket: str) -> dict:
        return json.loads(self.sql(
            "SELECT json_build_object('state', state, 'assignee', assignee, 'commit', commit_hash, 'exempt', commit_exempt, "
            "'audit', audit_signoff, 'inspector', inspector_signoff, 'user', user_signoff)::text "
            f"FROM ticket_board.tickets WHERE id = '{ticket}';"))

    def records(self, ticket: str) -> list[dict]:
        return json.loads(self.sql("SELECT coalesce(json_agg(r ORDER BY r.id), '[]')::text FROM "
                                   f"ticket_board.ticket_reopen_records r WHERE ticket_id = '{ticket}';"))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def done(board: Board, ticket: str, **fields) -> dict:
    fixture.seed_postgres_ticket(board.admin, ticket, title=ticket, state=fields.pop("state", "done"),
                                 assignee=fields.pop("assignee", "director"), commit_hash=fields.pop("commit_hash", COMMIT),
                                 audit_signoff=fields.pop("audit_signoff", True), **fields)
    return board.row(ticket)


def scenario(board: Board) -> None:
    # The mistaken reopen.
    closed = done(board, "PGU-1")
    reopened = board.act("PGU-1", "reopen", reason="the repository did not match, I thought")
    after = board.row("PGU-1")
    check("refused" not in reopened and after["state"] == "analysis" and after["commit"] == "" and not after["audit"],
          f"the reopen clears the commit and sign-offs, as a reopen must: {reopened} {after}")
    rec = board.records("PGU-1")
    check(len(rec) == 1 and rec[0]["from_state"] == "done" and rec[0]["to_state"] == "analysis"
          and rec[0]["commit_hash"] == COMMIT and rec[0]["signoffs"] == {"audit_signoff": True, "inspector_signoff": False,
                                                                         "user_signoff": False}
          and rec[0]["reopened_by"] == "director" and rec[0]["assignee"] == "director",
          f"the board records the closed disposition the reopen took away: {rec}")

    # Who may, and with what.
    by_app = board.act("PGU-1", "restore_reopened", caller="app", reason="mine")
    no_reason = board.act("PGU-1", "restore_reopened", reason="  ")
    fixture.seed_postgres_ticket(board.admin, "PGU-9", title="PGU-9", state="analysis", assignee="director")
    never = board.act("PGU-9", "restore_reopened", reason="nothing to restore")
    check("403" in by_app.get("refused", "") and "refused" in no_reason and "no recorded reopen" in never.get("refused", "")
          and board.row("PGU-1")["state"] == "analysis",
          f"refused to an implementer, without a reason, and where nothing was reopened: {by_app} {no_reason} {never}")

    # The restore.
    restored = board.act("PGU-1", "restore_reopened", reason="MEFP-922: compared the wrong repository; fixpatch main verified")
    check(board.row("PGU-1") == closed and restored.get("restored") is True and restored.get("commit_hash") == COMMIT,
          f"stage, owner, commit and sign-offs are exactly as they were before the reopen: {board.row('PGU-1')} vs {closed}")
    said = json.loads(board.sql("SELECT coalesce(json_agg(who || ': ' || text ORDER BY id), '[]')::text FROM "
                                "ticket_board.ticket_comments WHERE ticket_id = 'PGU-1' AND text LIKE 'Restored%';"))
    audit = json.loads(board.sql("SELECT coalesce(json_agg(field ORDER BY field), '[]')::text FROM ticket_board.ticket_field_audit "
                                 "WHERE ticket_id = 'PGU-1' AND actor = 'director' AND reason LIKE 'restore_reopened: MEFP-922%';"))
    check(len(said) == 1 and said[0].startswith("director: Restored") and COMMIT in said[0] and "audit_signoff yes" in said[0]
          and "No review was repeated or made" in said[0] and "compared the wrong repository" in said[0],
          f"one comment, by the Director, naming what was restored and why: {said}")
    check(audit == ["audit_signoff", "commit_hash", "state"] and board.records("PGU-1")[0]["restored_by"] == "director",
          f"a field-audit row for each field moved, and the record marked restored: {audit}")
    again = board.act("PGU-1", "restore_reopened", reason="once more")
    check("already restored" in again.get("refused", ""), f"a second restore is refused: {again}")

    # Bounded: moved on since; a new candidate; a sign-off of its own.
    done(board, "PGU-2")
    board.act("PGU-2", "reopen", reason="rework")
    moved = board.act("PGU-2", "route", state="in_progress", assignee="app")
    late = board.act("PGU-2", "restore_reopened", reason="too late")
    check("refused" not in moved and "has moved since" in late.get("refused", "") and board.row("PGU-2")["state"] == "in_progress",
          f"a ticket moved on since its reopen is not restored: {moved} {late}")
    done(board, "PGU-7")
    board.act("PGU-7", "reopen", reason="rework")
    board.act("PGU-7", "route", state="in_progress", assignee="app")
    back7 = board.act("PGU-7", "force_move", state="analysis", assignee="director", suppress_notification=True)
    round_trip = board.act("PGU-7", "restore_reopened", reason="it is back where the reopen left it")
    check("refused" not in back7 and board.row("PGU-7")["state"] == "analysis" and "has moved since" in round_trip.get("refused", ""),
          f"nor one that moved on and came back to where the reopen left it: {round_trip}")
    done(board, "PGU-3")
    board.act("PGU-3", "reopen", reason="rework")
    # Stand-ins for what happened since, written past the workflow's triggers (as the superuser, for one statement).
    stand_in = "BEGIN; SET LOCAL session_replication_role = replica; UPDATE ticket_board.tickets SET {} WHERE id = 'PGU-3'; COMMIT;"
    board.sql(stand_in.format("commit_hash = 'abcdef1234567'"))
    candidate = board.act("PGU-3", "restore_reopened", reason="would discard it")
    board.sql(stand_in.format("commit_hash = '', user_signoff = true"))
    signed = board.act("PGU-3", "restore_reopened", reason="a review is under way")
    check("new candidate" in candidate.get("refused", "") and "sign-off of its own" in signed.get("refused", ""),
          f"nor one carrying a new candidate, nor one with a sign-off of its own: {candidate} {signed}")
    cancelled = done(board, "PGU-4", state="cancelled", commit_hash="", audit_signoff=False)
    board.act("PGU-4", "reopen", reason="not a duplicate after all? it was")
    back = board.act("PGU-4", "restore_reopened", reason="it was a duplicate after all")
    check("refused" not in back and board.row("PGU-4") == cancelled, f"a reopen out of Cancelled restores to Cancelled: {back}")

    # MEFP-922's own path: a narrated override out of Done.
    closed5 = done(board, "PGU-5", inspector_signoff=True, needs_inspection=True)
    forced = board.act("PGU-5", "force_move", state="director_review", assignee="director", suppress_notification=True)
    rec5 = board.records("PGU-5")
    restored5 = board.act("PGU-5", "restore_reopened", reason="the override out of Done was the mistake")
    check("refused" not in forced and len(rec5) == 1 and rec5[0]["to_state"] == "director_review"
          and "refused" not in restored5 and board.row("PGU-5") == closed5,
          f"an override out of Done is recorded, and restores the same way: {forced} {rec5} {restored5}")

    # The window cannot be abused: another commit, or a sign-off the record does not hold.
    done(board, "PGU-6")
    board.act("PGU-6", "reopen", reason="rework")
    for label, assignment in (("another commit", "commit_hash = 'abcdef1234567', state = 'done', audit_signoff = true"),
                              ("a sign-off not recorded",
                               f"commit_hash = '{COMMIT}', state = 'done', audit_signoff = true, user_signoff = true")):
        try:
            # As the table's owner: the service may not write the table at all, so the window alone stands here.
            fixture.psql(board.admin, "BEGIN; "
                                      "SELECT set_config('ticket_board.caller_role', 'director', true); "
                                      "SELECT set_config('ticket_board.restore_reopen', 'PGU-6', true); "
                                      f"UPDATE ticket_board.tickets SET {assignment}, assignee = 'director' WHERE id = 'PGU-6'; COMMIT;")
            refused = ""
        except AssertionError as exc:
            refused = str(exc)
        check("only the closed disposition recorded" in refused and board.row("PGU-6")["state"] == "analysis",
              f"the restore window refuses {label}: {refused[-300:]}")


def test_a_fresh_and_an_upgraded_board_run_the_same_functions() -> None:
    from schema_function_drift import assert_no_drift, owning_migration

    names = ("enforce_declared_ticket_update", "record_ticket_reopen", "reopen_restore_admits", "restore_reopened_ticket")
    try:
        assert_no_drift(*names)
        drift = ""
    except AssertionError as exc:
        drift = str(exc)
    # A later migration may own enforce_declared_ticket_update (SYRD-745's pgu1033 adds its own window), as long as its
    # copy still carries this ticket's restore window.
    owners = {owning_migration(name).name for name in names if name != "enforce_declared_ticket_update"}
    keeps = "ticket_board.restore_reopen" in owning_migration("enforce_declared_ticket_update").read_text()
    check(drift == "" and owners == {"pgu1032_syrd746_restore_reopened.sql"} and keeps,
          f"schema.sql's copies are their owning migrations', character for character, and the trigger's owner keeps the "
          f"restore window: {drift} {owners} {keeps}")


def main() -> int:
    test_a_fresh_and_an_upgraded_board_run_the_same_functions()
    with temporary_cluster(prefix="s746", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd746."):
        board = Board(cluster)
        try:
            scenario(board)
        finally:
            board.close()
    print(f"reopen_restore_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
