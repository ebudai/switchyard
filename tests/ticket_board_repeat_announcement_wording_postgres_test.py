#!/usr/bin/env python3
"""SYRD-124: a notification must not claim a stage the ticket never entered.

Live on 2026-09-13, twice, on SYRD-122 and SYRD-111: "... entered
Implementation" arrived for a ticket that was already in_progress and already
being worked by the role it was sent to. Nothing transitioned. What happened
both times was that a blocker reached done and the unblock trigger announced
the ticket's current stage as an arrival.

Two things in the same sentence produced that. The declared-workflow
`transition_message` accepted `p_already_announced` and ignored it, so every
transition notification on a board with a declared workflow said "entered" --
the legacy branch has always drawn the distinction. And the unblock path passed
`false` outright, so even a corrected message would have claimed a first
arrival.

Nothing is suppressed here, which is the point worth keeping straight: a
blocker clearing on a ticket whose owner has not been told is still that
owner's only way to learn the work is theirs, and that notification is
unchanged. What changes is what the repeat says.

Run against a real cluster with a real declared workflow, because the wording
is chosen in SQL from the workflow's own stage labels and from the durable
send trace. A fake connection would assert the shape of a query.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.app import TicketBoardApp  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

PROJECT = "cerulean"


class Board:
    def __init__(self, app: TicketBoardApp, admin: str, reader: str) -> None:
        self.app = app
        self.admin = admin
        self.reader = reader

    def route(self, ticket_id: str, assignee: str) -> None:
        self.app.perform_workflow_action(
            ticket_id, "route", {"assignee": assignee}, caller_role="director"
        )

    def queued_messages(self, ticket_id: str) -> list[str]:
        raw = t.psql(self.reader, f"""
SELECT coalesce(jsonb_agg(message ORDER BY id), '[]'::jsonb)::text
FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';
""")
        return json.loads(raw)

    def clear_queue(self, ticket_id: str) -> None:
        t.psql(self.admin, f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket_id}';")

    def record_send(self, ticket_id: str, role: str, state: str) -> None:
        """Say the role was told, the way delivery says it.

        `ticket_state_already_announced` reads `send` rows out of the durable
        trace, so this writes the row delivery would have written rather than
        setting a flag this test invented.
        """
        t.psql(self.admin, f"""
INSERT INTO ticket_board.notification_trace
    (ticket_id, notification_id, target_role, kind, event, ticket_state_at_event)
VALUES ('{ticket_id}', 0, '{role}', 'transition', 'send', '{state}');
""")

    def resolve_blocker(self, blocker_id: str) -> None:
        """Finish the blocking ticket through the board, not around it.

        A raw UPDATE is refused under a declared workflow, and rightly: the
        unblock trigger is reached by a real terminal transition, so driving it
        any other way would be exercising a path the board does not have.
        """
        self.app.perform_workflow_action(
            blocker_id, "cancel", {"reason": "no longer needed"}, caller_role="director"
        )

    def set_blockers(self, ticket_id: str, blocker_id: str) -> None:
        self.app.update_ticket(
            ticket_id,
            {"blocked_by": [blocker_id], "blocked_reason": f"waiting on {blocker_id}"},
            caller_role="director",
        )


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="syrd124-wording-", shutdown="immediate") as cluster:
        dbname = "syrd124_wording"
        admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
        service = t.conninfo(cluster.socket_dir, cluster.port, dbname, t.SERVICE_ROLE)
        reader = t.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

        # The legacy branch first, before a workflow is declared: it has always
        # drawn this distinction and must keep drawing it.
        first = t.psql(admin, "SELECT ticket_board.transition_message('PGU-1', 'A ticket', 'analysis', 'in_progress', false);")
        again = t.psql(admin, "SELECT ticket_board.transition_message('PGU-1', 'A ticket', 'analysis', 'in_progress', true);")
        assert first == "New ticket for you: PGU-1 -- A ticket", first
        assert again == "PGU-1 -- A ticket is active again", again
        checks += 1

        for number, state, assignee in ((1, "in_progress", "app"), (2, "in_progress", "ops")):
            t.seed_postgres_ticket(
                admin, f"PGU-{number}", title="Reserved work", state=state,
                assignee=assignee, commit_exempt=True,
            )
        t.seed_postgres_ticket(admin, "PGU-9", title="Subject", state="analysis", assignee="director")

        with tempfile.TemporaryDirectory(prefix="syrd124-assets.") as tmpdir:
            root = Path(tmpdir)
            (root / "frames").mkdir()
            (root / "assets").mkdir()
            app = TicketBoardApp(
                root / "frames", root / "assets",
                project=PROJECT, ticket_prefix="PGU", database_url=service,
            )
            cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            board = Board(app, admin, reader)

            # THE DECLARED BRANCH used to ignore the flag entirely.
            declared_first = t.psql(admin, "SELECT ticket_board.transition_message('PGU-9', 'Subject', 'analysis', 'in_progress', false);")
            declared_again = t.psql(admin, "SELECT ticket_board.transition_message('PGU-9', 'Subject', 'analysis', 'in_progress', true);")
            assert declared_first == "PGU-9 -- Subject entered Implementation", declared_first
            assert declared_again == "PGU-9 -- Subject is active again in Implementation", declared_again
            assert declared_first != declared_again
            checks += 1

            # THE LIVE SEQUENCE. The ticket is routed to an implementer, that
            # role is told, the ticket is blocked, and the blocker then clears.
            board.route("PGU-9", "main")
            arrival = [m for m in board.queued_messages("PGU-9") if "PGU-9" in m]
            assert any("entered Implementation" in m for m in arrival), arrival
            checks += 1

            board.record_send("PGU-9", "main", "in_progress")
            board.clear_queue("PGU-9")
            board.set_blockers("PGU-9", "PGU-1")
            board.clear_queue("PGU-9")
            board.resolve_blocker("PGU-1")

            unblocked = board.queued_messages("PGU-9")
            assert unblocked, unblocked
            assert all("entered" not in m for m in unblocked), unblocked
            assert any(m == "PGU-9 -- Subject is unblocked" for m in unblocked), unblocked
            checks += 1

            # AND THE CASE THAT MUST NOT GO QUIET. The same ticket, in the
            # same stage, with the same owner -- and nothing in the trace
            # saying that owner was ever told. A blocker clearing is then the
            # only way they learn the work is theirs, so the arrival wording is
            # right and must survive this change.
            t.seed_postgres_ticket(admin, "PGU-3", title="Second blocker", state="analysis", assignee="director")
            board.set_blockers("PGU-9", "PGU-3")
            board.clear_queue("PGU-9")
            t.psql(admin, "DELETE FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-9';")
            board.resolve_blocker("PGU-3")

            untold = board.queued_messages("PGU-9")
            assert untold, untold
            assert any("entered Implementation" in m for m in untold), untold
            assert not any("is unblocked" in m for m in untold), untold
            checks += 1

    print(f"ticket_board_repeat_announcement_wording_postgres_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
