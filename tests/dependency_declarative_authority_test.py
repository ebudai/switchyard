#!/usr/bin/env python3
"""SYRD-194: request_dependency has to survive the door, not just the lock.

SYRD-133 says in the source, on the line above the grant itself, that
`request_dependency` carries "the same permission as await_role, because it IS
await_role plus the sentence that explains it". The legacy table honours that.
The declarative path did not: `require_operation_allowed` checked the OPERATION
NAME against the role's declared capabilities, and `request_dependency` is
deliberately absent from `workflow_config.CAPABILITIES` -- so no document can
name it, none ever has, and every role on every declared board was refused
before any database function was reached.

Live proof that it mattered: on SYRD-193 the App role ended a turn with a
Director question in its pane, having been refused `app cannot call
request_dependency`, and the Board had no event to deliver.

SYRD-133's own suite did not catch it because it drives the function as SQL --
`SELECT ticket_board.request_dependency(...)` -- which is the lock. This drives
the door: the real handler, over HTTP, as App, against a real cluster with a
real declarative workflow applied.

Third instance of this bug class, after SYRD-83 (director_edit) and SYRD-180
(the control overrides), so the negative cases matter as much as the positive
one: admitting a composite must not admit a role that holds only half of it.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus

isolate_tmux_bus()
import ticket_board_write_api_test as t
from scripts.ticket_board.workflow_config import validate
from temporary_cluster import temporary_cluster

#: Take one half of the composite away from App, leaving the other. A role like
#: this must still be refused, or "admitted by the capabilities it is built
#: from" would be a way to reach an operation the document declines.
def strip(capability: str, role: str = "app") -> str:
    # BOTH copies. `workflow_roles.definition` is what the database reads and
    # `workflow_configuration.document` is what the handler reads, so stripping
    # one and not the other tests the wrong layer -- the first draft of this
    # test stripped only the former, the handler admitted the composite from a
    # document that still granted it, and the DATABASE refused instead. That is
    # the backstop working, but it is not this test's subject.
    return f"""
UPDATE ticket_board.workflow_roles
SET definition = jsonb_set(definition, '{{capabilities}}',
    (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
     FROM jsonb_array_elements_text(definition->'capabilities') c
     WHERE c <> '{capability}'))
WHERE name = '{role}';
UPDATE ticket_board.workflow_configuration
SET document = jsonb_set(document, '{{roles}}', (
    SELECT jsonb_agg(
        CASE WHEN r->>'name' = '{role}'
             THEN jsonb_set(r, '{{capabilities}}',
                  (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
                   FROM jsonb_array_elements_text(r->'capabilities') c
                   WHERE c <> '{capability}'))
             ELSE r END ORDER BY ordinality)
    FROM jsonb_array_elements(document->'roles') WITH ORDINALITY AS e(r, ordinality)
))
WHERE singleton;
"""


def restore(capability: str, role: str = "app") -> str:
    return f"""
UPDATE ticket_board.workflow_roles
SET definition = jsonb_set(definition, '{{capabilities}}',
    (definition->'capabilities') || to_jsonb('{capability}'::text))
WHERE name = '{role}'
  AND NOT (definition->'capabilities' ? '{capability}');
UPDATE ticket_board.workflow_configuration
SET document = jsonb_set(document, '{{roles}}', (
    SELECT jsonb_agg(
        CASE WHEN r->>'name' = '{role}' AND NOT (r->'capabilities' ? '{capability}')
             THEN jsonb_set(r, '{{capabilities}}',
                  (r->'capabilities') || to_jsonb('{capability}'::text))
             ELSE r END ORDER BY ordinality)
    FROM jsonb_array_elements(document->'roles') WITH ORDINALITY AS e(r, ordinality)
))
WHERE singleton;
"""


def comments_of(app, ticket_id: str) -> list[str]:
    ticket = app.get_ticket(ticket_id)
    return [str(c.get("text") or "") for c in (ticket.get("comments") or [])]


def main() -> int:
    checks = 0
    with temporary_cluster(prefix="dependency-declarative-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "dependency_declarative_authority_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.seed_postgres_ticket(
            admin, "PGU-1", title="App needs the Director", state="in_progress", assignee="app"
        )
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        board = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        server = t.TicketBoardServer(("127.0.0.1", 0), board, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            t.post_json(
                base,
                "/api/tickets/actions/configure_workflow",
                {"document": cfg, "expected_revision": 0},
                caller="director",
            )
            assert board.workflow_configuration() is not None
            checks += 1

            # The document this board actually ships: every role holds both
            # halves, and NO role holds `request_dependency`, because the
            # capability vocabulary has no such name to hold.
            declared = {r["name"]: set(r["capabilities"]) for r in board.workflow_configuration()["roles"]}
            assert "add_comment" in declared["app"] and "await_role" in declared["app"], declared["app"]
            assert not any("request_dependency" in caps for caps in declared.values()), declared
            checks += 1

            # THE REGRESSION. App holds both halves, so the atomic form is
            # admitted -- and it was not, before this fix.
            granted = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_dependency",
                {"role": "director", "reason": "Which release should this pin to?"},
                caller="app",
            )
            assert granted, granted
            checks += 1

            # Atomic: the wait and the sentence that explains it both exist.
            ticket = board.get_ticket("PGU-1")
            assert ticket["awaiting_role"] == "director", ticket["awaiting_role"]
            assert any(
                "Which release should this pin to?" in c for c in comments_of(board, "PGU-1")
            ), comments_of(board, "PGU-1")
            checks += 2

            # Neither half may be reachable alone. Strip one, and the composite
            # is refused again -- with the ticket left exactly as it was.
            before = board.get_ticket("PGU-1")
            for half in ("await_role", "add_comment"):
                t.psql(admin, strip(half))
                denied = t.post_json(
                    base,
                    "/api/tickets/PGU-1/actions/request_dependency",
                    {"role": "director", "reason": f"missing {half}"},
                    caller="app",
                    expect=403,
                )
                assert "app cannot call request_dependency" in str(denied), (half, denied)
                after = board.get_ticket("PGU-1")
                assert after["awaiting_role"] == before["awaiting_role"], (half, after)
                assert len(after.get("comments") or []) == len(before.get("comments") or []), half
                t.psql(admin, restore(half))
                checks += 2

            # Restored, it works again: the refusal was the capability, not a
            # one-shot side effect of the first call.
            t.post_json(
                base,
                "/api/tickets/PGU-1/actions/clear_awaiting_role",
                {},
                caller="app",
            )
            again = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_dependency",
                {"role": "director", "reason": "Asking a second time."},
                caller="app",
            )
            assert again, again
            assert board.get_ticket("PGU-1")["awaiting_role"] == "director"
            checks += 2
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print(f"dependency_declarative_authority_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
