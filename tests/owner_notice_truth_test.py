#!/usr/bin/env python3
"""SYRD-797: an owner's change notice says something true when it is read.

On SAPI-69 (2026-11-15) ops kept being woken by Director notices whose event was not in the ticket when it read it:
* "the wait on director was cleared", with no Director comment yet: the Director clears the wait and then comments
  its decision, two writes, and the clear's wake went at once;
* the same, with the ticket again `awaiting_role: director`: a wake queued while ops was busy reached ops after it
  had asked again;
* "new comment", with ops' own comment the newest: an approval and ops' comment had crossed by two seconds.

Now a wait's clearing wakes its owner after a 30-second moment, and the decision's comment, when it follows, replaces
that wake and goes at once; a "wait cleared" notice is dropped while a wait stands on the ticket; and a comment's
notice names it, by the time the ticket shows for it.

On a production-built board (schema.sql, roles, rbac.sql, every migration) with the shipped workflow, the real HTTP
server and the real notify listener with an injected sender (SYRD-772's rig).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import user_review_serial_release_test as urs  # noqa: E402 - its production-built board, listener and helpers

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def to_main(batch: list[tuple[str, str]], ticket: str, role: str = "main") -> list[str]:
    return urs.about(batch, role, ticket)


def due_in(board, ticket: str, role: str = "main") -> float:
    """Seconds until the owner's pending change notice on `ticket` is due; -1 when there is none."""
    value = board.sql("SELECT coalesce(extract(epoch FROM max(next_attempt_at) - clock_timestamp()), -1) FROM "
                      f"ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' AND kind = 'ticket_update' "
                      f"AND target_role = '{role}';")
    return float(value)


def make_due(board, ticket: str) -> None:
    board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
              f"WHERE ticket_id = '{ticket}' AND claimed_at IS NULL;")


def comments(board, ticket: str) -> list[dict]:
    return board.ticket_json(ticket).get("comments") or []


def test_owner_notices() -> None:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix="syrd797-", shutdown="immediate") as cluster:
        admin = urs.build_board(fixture, cluster, ROOT, "board")
        board = urs.Board(fixture, cluster, admin, "board", "a")
        try:
            board.app.apply_workflow(urs.workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            T, U, V = "PGU-69", "PGU-70", "PGU-71"
            for ticket in (T, U, V):
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             commit_exempt=True)
            board.one_pass()
            # Each its owner's active ticket: a ticket queued behind another wakes nobody (SYRD-582).
            for ticket, owner in ((T, "main"), (U, "app"), (V, "ops")):
                board.act(ticket, "route", "director", state="in_progress", assignee=owner)
            board.one_pass()

            # Ops asks; the Director clears the wait, then comments its approval.
            board.act(T, "request_dependency", "main", role="director", reason="Approve step 1 exactly as posted.")
            board.one_pass()
            board.act(T, "clear_awaiting_role", "director")
            held = board.one_pass()
            check(to_main(held, T) == [] and 25 < due_in(board, T) <= 30,
                  f"the clear alone wakes main only after its moment: {to_main(held, T)} due in {due_in(board, T):.1f}s")
            board.act(T, "add_comment", "director", text="Director: step 1 APPROVED as posted.")
            woke = to_main(board.one_pass(), T)
            stamp = comments(board, T)[-1]["ts"]
            check(len(woke) == 1 and f"new comment at {stamp}" in woke[0] and "wait on director was cleared" not in woke[0],
                  f"the comment's wake replaces the clear's and goes at once, naming the comment the ticket shows: "
                  f"{woke} {stamp}")
            check(to_main(board.one_pass(), T) == [], "and only once")

            # A clear with no comment still wakes main, after the moment.
            board.act(U, "request_dependency", "app", role="director", reason="Is the user at the desktop?")
            board.one_pass()
            board.act(U, "clear_awaiting_role", "director")
            check(to_main(board.one_pass(), U, "app") == [] and 25 < due_in(board, U, "app") <= 30, "not at once")
            make_due(board, U)
            later = to_main(board.one_pass(), U, "app")
            check(len(later) == 1 and "the wait on director was cleared" in later[0],
                  f"after the moment, the clear is told: {later}")

            # A clear's wake that would reach main after it has asked again says nothing false: it is dropped.
            board.act(V, "request_dependency", "ops", role="director", reason="Approve step 2.")
            board.one_pass()
            board.act(V, "clear_awaiting_role", "director")
            queued = due_in(board, V, "ops")
            board.act(V, "request_dependency", "ops", role="director", reason="Approve step 3.")
            make_due(board, V)
            stale = to_main(board.one_pass(), V, "ops")
            dropped = board.sql("SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-71' "
                                "AND event = 'drop' AND busy_reason = 'stale_notification';")
            check(queued > 0 and stale == [] and int(dropped) >= 1 and board.sql(
                      "SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-71';") == "director",
                  f"a 'wait cleared' notice while the wait stands again is dropped, not delivered: {stale} dropped={dropped}")

            # Crossed comments: the Director's approval, then main's own comment. The wake names the approval.
            board.act(T, "add_comment", "director", text="Director: step 2 APPROVED.")
            approval = comments(board, T)[-1]["ts"]
            board.act(T, "add_comment", "main", text="Step 2 ready; no later approval seen.")
            crossed = to_main(board.one_pass(), T)
            check(len(crossed) == 1 and f"new comment at {approval}" in crossed[0] and comments(board, T)[-1]["who"] == "main",
                  f"with main's own comment newest, the wake still names the Director's approval: {crossed}")
            # SAPI-69's real flow: main waits on the Director, which comments its approval without clearing the wait.
            # Only a "wait cleared" notice is dropped while a wait stands; the comment's wake must still reach main.
            board.act(T, "request_dependency", "main", role="director", reason="Approve step 3 exactly as posted.")
            board.one_pass()
            board.act(T, "add_comment", "director", text="Director: step 3 APPROVED as posted.")
            approved = comments(board, T)[-1]["ts"]
            standing = to_main(board.one_pass(), T)
            check(len(standing) == 1 and f"new comment at {approved}" in standing[0] and board.sql(
                      "SELECT awaiting_role FROM ticket_board.ticket_notification_state WHERE ticket_id = 'PGU-69';") == "director",
                  f"a comment's wake reaches main while its wait on the Director stands: {standing}")
        finally:
            board.close()


def main() -> int:
    test_owner_notices()
    print(f"owner_notice_truth_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
