#!/usr/bin/env python3
"""SYRD-93: an existing board gets publication without anybody editing a document.

A tenant provisioned before this operation existed stores a workflow document
whose roles cannot name it, and the HTTP handler decides a non-transition
operation from that document -- so on an upgraded board the implementer is
refused before ticket_board.request_publication runs and the control role cannot
answer. This drives the upgrade that fixes it, through the real handler.
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

#: The upgrade path itself, applied as the migration runner would apply it.
UPGRADE = (
    ROOT / "scripts" / "ticket_board" / "migrations" / "pgu930_syrd93_publication_requests.sql"
).read_text()

STRIP = """
UPDATE ticket_board.workflow_configuration
SET document = jsonb_set(document, '{roles}', (
    SELECT jsonb_agg(jsonb_set(role, '{capabilities}',
        (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
         FROM jsonb_array_elements_text(role->'capabilities') c
         WHERE c NOT IN ('request_publication', 'resolve_publication'))) ORDER BY ordinality)
    FROM jsonb_array_elements(document->'roles') WITH ORDINALITY AS elements(role, ordinality)
))
WHERE singleton;
UPDATE ticket_board.workflow_roles
SET definition = jsonb_set(definition, '{capabilities}',
    (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
     FROM jsonb_array_elements_text(definition->'capabilities') c
     WHERE c NOT IN ('request_publication', 'resolve_publication')));
"""

COMMIT = "e" * 40
BUNDLE = "/home/agent/.local/state/switchyard/publish-outbox/cerulean/PGU-1.bundle"


def capabilities(admin: str, role: str) -> list[str]:
    return json.loads(t.psql(
        admin,
        "SELECT coalesce(definition->'capabilities', '[]'::jsonb) "
        f"FROM ticket_board.workflow_roles WHERE name = '{role}';",
    ))


def main() -> int:
    with temporary_cluster(prefix="publication-upgrade-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "publication_capability_upgrade_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.seed_postgres_ticket(admin, "PGU-1", title="Ops work", state="in_progress", assignee="ops")
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
        ask = {"ref": "ops/syrd-92", "commit": COMMIT, "bundle": BUNDLE}
        try:
            t.post_json(
                base,
                "/api/tickets/actions/configure_workflow",
                {"document": cfg, "expected_revision": 0},
                caller="director",
            )

            # The state of every board provisioned before this existed.
            t.psql(admin, STRIP)
            refused = t.post_json(
                base, "/api/tickets/PGU-1/actions/request_publication", ask,
                caller="ops", expect=403,
            )
            assert "ops cannot call request_publication" in str(refused), refused
            assert app.publication_requests() == []

            # The upgrade grants each half to the roles that already are what
            # they are: implementers ask, whoever holds control authority
            # answers. Nobody edits a document by hand.
            t.psql(admin, UPGRADE)
            asked = t.post_json(
                base, "/api/tickets/PGU-1/actions/request_publication", ask, caller="ops",
            )
            request_id = asked["request"]["id"]
            resolved = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": request_id, "outcome": "published", "detail": "pushed"},
                caller="director",
            )
            assert resolved["request"]["state"] == "published", resolved

            # It granted what each role needed and nothing else: a reviewer does
            # not become a publisher on upgrade.
            assert "request_publication" in capabilities(admin, "ops")
            assert "resolve_publication" in capabilities(admin, "director")
            for role in ("audit", "inspector", "user"):
                assert not {"request_publication", "resolve_publication"} & set(capabilities(admin, role)), role

            # Idempotent: a second run grants nothing twice and writes no
            # revision nobody asked for.
            revision = t.psql(admin, "SELECT revision FROM ticket_board.workflow_configuration;")
            t.psql(admin, UPGRADE)
            assert t.psql(admin, "SELECT revision FROM ticket_board.workflow_configuration;") == revision
            assert capabilities(admin, "director").count("resolve_publication") == 1
            marker = t.psql(
                admin,
                "SELECT document->'migrations'->>'publication_capabilities' "
                "FROM ticket_board.workflow_configuration WHERE singleton;",
            )
            assert marker == "true", marker
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("publication_capability_upgrade_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
