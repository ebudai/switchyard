#!/usr/bin/env python3
"""SYRD-83: the Director's generic edit has to survive the door, not just the lock.

`TicketBoardHandler.require_operation_allowed` decides a non-transition
operation from the caller's DECLARED capabilities. A tenant running a
declarative workflow therefore refuses an operation its document does not name,
before any database function is reached -- so a Director on a real board was
rejected at the handler even though ticket_board.director_edit would have
accepted them.

This drives the real handler, over HTTP, against a real cluster with a real
declarative workflow applied: granted, ungranted, and the upgrade that grants it
to whatever role already holds control authority.
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

#: The upgrade path itself, applied as the migration runner would apply it. A
#: retyped copy could pass here while the file a tenant actually runs drifted.
UPGRADE = (
    ROOT / "scripts" / "ticket_board" / "migrations" / "pgu928_syrd83_director_edit.sql"
).read_text()

#: The state of a tenant at the release this migration was written for: without
#: the capability it grants, and without the ones later releases added, whose
#: names the validator this migration reinstalls does not know (SYRD-93).
STRIP_CAPABILITY = """
UPDATE ticket_board.workflow_configuration
SET document = jsonb_set(document, '{roles}', (
    SELECT jsonb_agg(jsonb_set(role, '{capabilities}',
        (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
         FROM jsonb_array_elements_text(role->'capabilities') c
         WHERE c NOT IN ('director_edit', 'request_publication', 'resolve_publication'))) ORDER BY ordinality)
    FROM jsonb_array_elements(document->'roles') WITH ORDINALITY AS elements(role, ordinality)
))
WHERE singleton;
UPDATE ticket_board.workflow_roles
SET definition = jsonb_set(definition, '{capabilities}',
    (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
     FROM jsonb_array_elements_text(definition->'capabilities') c
     WHERE c NOT IN ('director_edit', 'request_publication', 'resolve_publication')));
"""


def main() -> int:
    with temporary_cluster(prefix="director-edit-handler-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "director_edit_handler_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.seed_postgres_ticket(
            admin, "PGU-1", title="Handler edit", state="audit", assignee="audit"
        )
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        cfg = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
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
            assert app.workflow_configuration() is not None

            # The provisioned document grants it, so the handler lets it
            # through and the operation does the work.
            granted = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/director_edit",
                {"patch": {"title": "Renamed through the handler"}, "reason": "handler path"},
                caller="director",
            )
            assert granted["ticket"]["title"] == "Renamed through the handler", granted
            assert app.get_ticket("PGU-1")["title"] == "Renamed through the handler"

            # Nobody else gets past the door, and nothing changes.
            for role in ("main", "audit", "inspector"):
                denied = t.post_json(
                    base,
                    "/api/tickets/PGU-1/actions/director_edit",
                    {"patch": {"title": "Not yours"}, "reason": "try"},
                    caller=role,
                    expect=403,
                )
                assert f"{role} cannot call director_edit" in str(denied), (role, denied)
            assert app.get_ticket("PGU-1")["title"] == "Renamed through the handler"

            # The state every tenant provisioned before this operation is in: a
            # stored document whose control role does not declare it. The
            # database function would accept this caller; the handler does not.
            t.psql(admin, STRIP_CAPABILITY)
            ungranted = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/director_edit",
                {"patch": {"title": "Refused at the door"}, "reason": "try"},
                caller="director",
                expect=403,
            )
            assert "director cannot call director_edit" in str(ungranted), ungranted
            assert app.get_ticket("PGU-1")["title"] == "Renamed through the handler"

            # And the same tenant has never been granted the function either,
            # because the function is new. Both halves are the migration's to
            # repair, so both are taken away here.
            t.psql(
                admin,
                f"REVOKE EXECUTE ON FUNCTION ticket_board.director_edit(text, jsonb, text) "
                f"FROM {t.SERVICE_ROLE};\n"
                f"REVOKE SELECT, INSERT ON ticket_board.ticket_field_audit FROM {t.SERVICE_ROLE};",
            )
            assert t.psql(
                admin,
                "SELECT has_function_privilege("
                f"'{t.SERVICE_ROLE}', "
                "'ticket_board.director_edit(text, jsonb, text)', 'EXECUTE');",
            ) == "f"

            # The upgrade grants the capability to whatever role already holds
            # control authority, and the privilege to whichever database role is
            # already the board's writer -- neither named by hand.
            t.psql(admin, UPGRADE)
            assert t.psql(
                admin,
                "SELECT has_function_privilege("
                f"'{t.SERVICE_ROLE}', "
                "'ticket_board.director_edit(text, jsonb, text)', 'EXECUTE');",
            ) == "t"
            restored = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/director_edit",
                {"patch": {"title": "Granted again on upgrade"}, "reason": "after upgrade"},
                caller="director",
            )
            assert restored["ticket"]["title"] == "Granted again on upgrade", restored
            marker = t.psql(
                admin,
                "SELECT document->'migrations'->>'director_edit_capability' "
                "FROM ticket_board.workflow_configuration WHERE singleton;",
            )
            assert marker == "true", marker
            # Idempotent: running it twice grants nothing twice.
            t.psql(admin, UPGRADE)
            caps = t.psql(
                admin,
                "SELECT definition->>'capabilities' FROM ticket_board.workflow_roles "
                "WHERE name = 'director';",
            )
            assert caps.count("director_edit") == 1, caps
            # And it granted nothing to a role that is not the control role.
            others = t.psql(
                admin,
                "SELECT count(*)::text FROM ticket_board.workflow_roles "
                "WHERE definition->'capabilities' ? 'director_edit' "
                "AND NOT definition->'capabilities' ?& ARRAY['set_manually_controlled','merge'];",
            )
            assert others == "0", others
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("director_edit_handler_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
