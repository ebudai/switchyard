#!/usr/bin/env python3
"""SYRD-192: an unresolved blocker must not stop work being put down.

Reproduced live during SYRD-117/SYRD-37 recovery. SYRD-37 sat in in_progress
behind an unresolved blocker; the Director used the ordinary `defer` to park it
and free Main's serial slot, and the board refused with *unresolved blocker
prevents forward promotion*. The workaround was to clear the blocker, defer, and
put the blocker back: three writes, a window in which the ticket looked
unblocked, and a blocked ticket holding an implementer's serial slot throughout.

A blocker stops work going **forward**. A parking stage is not terminal, owns
nobody and notifies nobody, so nothing is promoted by landing there — and being
blocked is the very reason to park. The declared executor exempted only
`return` and `reopen`, and `defer` is primitive `move`.

Everything here runs against real PostgreSQL through the real handler, because
the rule lives in `enforce_declared_ticket_update` and a Python-level fake
would only prove what the fake was told to do. The exemption is keyed on the
destination's *shape* via `declared_parking_stage` — the same predicate
`workflow_config.parking_stage_names` uses — so a tenant whose parking stage is
not called `backlog` gets it too.
"""

from __future__ import annotations

import copy
import json
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_director_defer_backlog_test as defer_suite  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.workflow_config import parking_stage_names, validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
CANONICAL = defer_suite.CANONICAL
MIGRATION_PATH = ROOT / "scripts/ticket_board/migrations/pgu954_syrd192_park_blocked.sql"
SCHEMA_PATH = ROOT / "scripts/ticket_board/schema.sql"
ENFORCER = (
    "CREATE OR REPLACE FUNCTION ticket_board.enforce_declared_ticket_update"
    "(previous ticket_board.tickets, proposed ticket_board.tickets)"
)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def refused(call, expected: str = "") -> str:
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - the refusal is the assertion
        if expected:
            assert expected in str(exc), f"expected {expected!r} in: {exc}"
        return str(exc)
    raise AssertionError(f"expected a refusal containing {expected!r}, and it was allowed")


def function_body(text: str) -> str:
    start = text.index(ENFORCER)
    return text[start : text.index("\n$$;", start) + 4]


def ticket_row(admin: str, ticket_id: str) -> dict:
    row = t.psql(
        admin,
        "SELECT state || '|' || assignee || '|' || parked::text || '|' || blocked_reason "
        "|| '|' || commit_hash || '|' || audit_signoff::text || '|' || needs_audit::text "
        f"FROM ticket_board.tickets WHERE id = '{ticket_id}';",
    )
    state, assignee, parked, reason, commit_hash, audit_signoff, needs_audit = row.split("|", 6)
    return {
        "state": state,
        "assignee": assignee,
        "parked": parked == "true",
        "blocked_reason": reason,
        "commit_hash": commit_hash,
        "audit_signoff": audit_signoff == "true",
        "needs_audit": needs_audit == "true",
    }


def blockers(admin: str, ticket_id: str) -> list[tuple[str, bool]]:
    rows = t.psql(
        admin,
        "SELECT coalesce(string_agg(blocker_ticket_id || ':' || resolved::text, ',' "
        f"ORDER BY position), '') FROM ticket_board.ticket_blockers WHERE ticket_id = '{ticket_id}';",
    )
    return [
        (entry.split(":")[0], entry.split(":")[1] == "true")
        for entry in rows.split(",")
        if entry
    ]


def reserved(admin: str, role: str) -> str:
    return t.psql(
        admin, f"SELECT coalesce(ticket_board.ticket_current_reserved_ticket('{role}'), '');"
    )


def comment_count(admin: str, ticket_id: str) -> str:
    return t.psql(
        admin, f"SELECT count(*)::text FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket_id}';"
    )


def block(admin: str, ticket_id: str, blocker_id: str, reason: str) -> None:
    """Put a real unresolved blocker on a ticket, with the reason beside it.

    The reason lives on the ticket row, so writing it goes through the declared
    enforcement trigger and needs an actor -- the same one a director would be.
    `set_config` is local to the statement, exactly as the handler sets it.
    """
    t.psql(
        admin,
        "SELECT set_config('ticket_board.caller_role', 'director', false);\n"
        "INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position, resolved) "
        f"VALUES ('{ticket_id}', '{blocker_id}', 0, false);\n"
        f"UPDATE ticket_board.tickets SET blocked_reason = '{reason}' WHERE id = '{ticket_id}';",
    )


# --- the shape the exemption is keyed on -----------------------------------


def test_the_parking_stage_is_recognised_by_shape_in_both_layers() -> None:
    """SQL and Python must agree, or the exemption applies in only one of them."""
    cfg = validate(copy.deepcopy(CANONICAL))
    parking = parking_stage_names(cfg)
    check(parking == {"backlog"}, f"{parking}")
    stage = next(s for s in cfg["stages"] if s["name"] == "backlog")
    check(
        stage["terminal"] is False and stage["owners"] == [] and stage["notify"]["kind"] == "none",
        f"nothing is promoted by landing there: {stage}",
    )
    # And the transition this ticket is about really is an ordinary move, not
    # a return or a reopen -- which is why the old exemption list missed it.
    defers = [x for x in cfg["transitions"] if x["from"] == "in_progress" and x["to"] == "backlog"]
    check(len(defers) == 1, f"{defers}")
    check(defers[0]["primitive"] == "move", f"defer is primitive {defers[0]['primitive']}")
    check(defers[0]["action"] == "defer" and "director" in defers[0]["actors"], f"{defers[0]}")


def test_the_fix_lives_in_both_bodies_and_they_do_not_drift() -> None:
    """The migration copy is what an upgraded board runs; schema.sql is a fresh one.

    A fix in only one of them works on exactly half the tenants, and which half
    depends on when they were provisioned.
    """
    from schema_function_drift import owning_migration

    # The copy an upgraded board runs is the NEWEST migration that defines the
    # function, which stops being this one as soon as a later fix redefines it
    # (SYRD-263 did); the exemption this ticket added has to survive there.
    schema_body = function_body(SCHEMA_PATH.read_text())
    migration_body = function_body(owning_migration("enforce_declared_ticket_update").read_text())
    check(schema_body == migration_body, "the two bodies are identical")
    for label, body in (("schema.sql", schema_body), ("the migration", migration_body)):
        check("declared_parking_stage(tr->>'to')" in body, f"{label} carries the exemption")
        check(
            "unresolved_blocker_list(previous.id)" in body,
            f"{label} names the blocker in its refusal",
        )


def test_the_migration_sorts_after_every_migration_before_it() -> None:
    """The runner applies them by sorted filename, so ordering is the filename."""
    names = sorted(path.name for path in MIGRATION_PATH.parent.glob("*.sql"))
    check(names[-1] == MIGRATION_PATH.name, f"it applies last: {names[-3:]}")
    body = MIGRATION_PATH.read_text()
    check(body.lstrip().startswith("--"), "it says what it is for")
    check("BEGIN;" in body and body.rstrip().endswith("COMMIT;"), "and it is one transaction")
    check(
        "CREATE OR REPLACE" in body and "DROP FUNCTION" not in body,
        "and re-applying it changes nothing",
    )


# --- the live board --------------------------------------------------------


def run_board_checks(cluster) -> None:
    app, admin = defer_suite.board(cluster, "parked", copy.deepcopy(CANONICAL))

    # --- the reproduction -------------------------------------------------
    #
    # SYRD-37's shape: implementation work, an implementer's serial slot, and a
    # blocker that is the reason the director wants it put down.
    t.seed_postgres_ticket(
        admin, "PGU-37", title="Worker pool", state="in_progress", assignee="app",
        commit_hash="a" * 40, audit_signoff=False,
    )
    block(admin, "PGU-37", "PGU-117", "waiting on the release phase to close")
    t.psql(
        admin,
        "INSERT INTO ticket_board.ticket_comments"
        "(ticket_id, position, who, ts_text, text, source_json) VALUES "
        "('PGU-37', 0, 'director', '2026-09-23T00:00:00+00:00', "
        "'a note that must survive being put down', "
        "'{\"who\":\"director\",\"ts\":\"2026-09-23T00:00:00+00:00\","
        "\"text\":\"a note that must survive being put down\"}'::jsonb);",
    )
    before = ticket_row(admin, "PGU-37")
    comments_before = comment_count(admin, "PGU-37")
    check(reserved(admin, "app") == "PGU-37", "the implementer's slot is held")

    parked = app.perform_workflow_action("PGU-37", "defer", {}, caller_role="director")
    check(parked["state"] == "backlog", f"the ordinary defer is allowed: {parked['state']}")

    row = ticket_row(admin, "PGU-37")
    check(row["assignee"] == "unassigned", f"parked tickets are nobody's: {row}")
    check(row["parked"] is True, f"and marked parked: {row}")
    check(reserved(admin, "app") == "", "and the serial slot is released")

    # --- and nothing else moved -------------------------------------------
    check(
        blockers(admin, "PGU-37") == [("PGU-117", False)],
        f"the blocker row survives, still unresolved: {blockers(admin, 'PGU-37')}",
    )
    check(
        row["blocked_reason"] == before["blocked_reason"] and row["blocked_reason"] != "",
        f"and so does the reason: {row['blocked_reason']!r}",
    )
    check(row["commit_hash"] == before["commit_hash"], f"commit provenance: {row}")
    check(
        (row["audit_signoff"], row["needs_audit"]) == (before["audit_signoff"], before["needs_audit"]),
        f"review evidence: {row}",
    )
    check(comment_count(admin, "PGU-37") == comments_before, "comments are untouched")

    # --- forward transitions are still refused, and now name the blocker ---
    # Commit-exempt on purpose: submitting validates the commit against the
    # board's copy of the repository BEFORE it reaches the blocker check, and a
    # fabricated sha would make this case prove the wrong refusal.
    t.seed_postgres_ticket(
        admin, "PGU-38", title="Still blocked", state="in_progress", assignee="main",
        commit_exempt=True,
    )
    block(admin, "PGU-38", "PGU-117", "same dependency")
    message = refused(
        lambda: app.perform_workflow_action("PGU-38", "submit_to_audit", {}, caller_role="main"),
        "unresolved blocker",
    )
    check("PGU-117" in message, f"the refusal names what is in the way: {message}")
    check(
        ticket_row(admin, "PGU-38")["state"] == "in_progress",
        "and the ticket did not move",
    )

    # --- a parked blocked ticket cannot be revived while still blocked -----
    #
    # The other half of the ticket: parking is not a loophole out of the
    # blocker. Coming BACK is a forward move and stays refused.
    revive = refused(
        lambda: app.perform_workflow_action("PGU-37", "route", {}, caller_role="director"),
        "unresolved blocker",
    )
    check("PGU-117" in revive, f"named again on the way back: {revive}")
    check(ticket_row(admin, "PGU-37")["state"] == "backlog", "it stays parked")

    # --- and is revived once the blocker resolves --------------------------
    t.psql(
        admin,
        "UPDATE ticket_board.ticket_blockers SET resolved = true "
        "WHERE ticket_id = 'PGU-37' AND blocker_ticket_id = 'PGU-117';",
    )
    revived = app.perform_workflow_action("PGU-37", "route", {}, caller_role="director")
    check(revived["state"] == "analysis", f"only then does it come back: {revived['state']}")
    revived_row = ticket_row(admin, "PGU-37")
    check(revived_row["parked"] is False, f"and it is no longer parked: {revived_row}")
    check(
        revived_row["commit_hash"] == before["commit_hash"],
        "with its provenance still intact after the round trip",
    )
    check(
        blockers(admin, "PGU-37") == [("PGU-117", True)],
        f"and the blocker is kept as resolved history: {blockers(admin, 'PGU-37')}",
    )

    # --- every active stage can put blocked work down ----------------------
    #
    # The ticket says "declared transitions to a parking stage", not "defer
    # from in_progress". A director blocked in review has the same problem.
    # `audit` carries its stage signoff because deferring OUT of a review stage
    # requires it -- a separate, pre-existing rule that has nothing to do with
    # blockers. Seeding it keeps this case about the one thing it is for; the
    # SYRD-92 suite covers that rule itself.
    for index, (stage, assignee, signed) in enumerate(
        (("draft", "director", False), ("analysis", "director", False), ("audit", "audit", True))
    ):
        ticket = f"PGU-4{index}"
        t.seed_postgres_ticket(
            admin, ticket, title=f"Blocked in {stage}", state=stage, assignee=assignee,
            commit_hash="c" * 40, audit_signoff=signed, needs_audit=True,
        )
        block(admin, ticket, "PGU-117", f"blocked while in {stage}")
        result = app.perform_workflow_action(ticket, "defer", {}, caller_role="director")
        check(result["state"] == "backlog", f"{stage} can be parked while blocked: {result['state']}")
        check(
            blockers(admin, ticket) == [("PGU-117", False)],
            f"{stage} keeps its blocker: {blockers(admin, ticket)}",
        )

    # --- no workaround is required ----------------------------------------
    #
    # The clear/defer/restore dance the director had to perform. If parking
    # needed it, this ticket would still be open.
    t.seed_postgres_ticket(
        admin, "PGU-50", title="No dance", state="in_progress", assignee="ops",
    )
    block(admin, "PGU-50", "PGU-117", "still waiting")
    unresolved_before = blockers(admin, "PGU-50")
    app.perform_workflow_action("PGU-50", "defer", {}, caller_role="director")
    check(
        blockers(admin, "PGU-50") == unresolved_before,
        "the blocker was never cleared and restored to get there",
    )
    check(ticket_row(admin, "PGU-50")["state"] == "backlog", "and it parked anyway")


def test_the_live_board_parks_blocked_work_and_still_refuses_promotion() -> None:
    with temporary_cluster(prefix="syrd192-park-", shutdown="immediate") as cluster:
        run_board_checks(cluster)


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("ticket_board_park_blocked_postgres_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(900)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"ticket_board_park_blocked_postgres_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
