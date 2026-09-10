#!/usr/bin/env python3
"""SYRD-93: publication is admitted by declared capability, never by role name.

A project configures its own roles. If either half of publication were admitted
because a caller is called `ops`, or refused because it is not called
`director`, the operation would work only for projects that happen to use this
project's names -- and a tenant that renamed or added a role would find its
implementers unable to ask and its controller unable to answer.

So this drives roles the static tables have never heard of, through the real
handler and the real database, on a real declarative board.
"""

from __future__ import annotations

import copy
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

COMMIT = "f" * 40
BUNDLE = "/home/agent/.local/state/switchyard/publish-outbox/cerulean/PGU-1.bundle"


def document_with_configured_roles() -> dict:
    """The example workflow, plus roles no static table in this repo names.

    `delta` is an implementer this project invented, carrying the ask. `steward`
    holds control authority beside the Director, so answering can be shown to
    follow the capabilities rather than the name. `clerk` holds the answering
    capability and no control authority at all.
    """
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    template = next(r for r in cfg["roles"] if r["name"] == "ops")
    director = next(r for r in cfg["roles"] if r["name"] == "director")

    delta = copy.deepcopy(template)
    # A stage that notifies its owners needs a pane to notify, so delta is a
    # whole role rather than a name in a list.
    delta.update({
        "name": "delta",
        "label": "Delta",
        "slot": None,
        "runtime": "claude",
        "target": "cerulean-delta:0.0",
    })
    steward = copy.deepcopy(director)
    steward.update({"name": "steward", "label": "Steward", "slot": None, "runtime": None, "target": None})
    clerk = copy.deepcopy(template)
    clerk.update({
        "name": "clerk",
        "label": "Clerk",
        "kind": "support",
        "slot": None,
        "runtime": None,
        "target": None,
        "capabilities": ["add_comment", "resolve_publication"],
    })
    cfg["roles"] = [*cfg["roles"], delta, steward, clerk]

    # Delta works like any other implementer: it owns work in the same stages
    # and takes the same transitions.
    for stage in cfg["stages"]:
        if "ops" in (stage.get("owners") or []):
            stage["owners"] = [*stage["owners"], "delta"]
    for transition in cfg["transitions"]:
        if "ops" in (transition.get("actors") or []):
            transition["actors"] = [*transition["actors"], "delta"]
    return cfg


def main() -> int:
    with temporary_cluster(prefix="publication-authority-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "publication_declarative_authority_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        # Seeded before the document exists, so it starts with a role the
        # pre-configuration tables know and is handed to delta once delta is a
        # role at all.
        t.seed_postgres_ticket(admin, "PGU-1", title="Delta work", state="in_progress", assignee="ops")
        t.seed_postgres_ticket(admin, "PGU-2", title="Ops work", state="in_progress", assignee="ops")
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = server.write_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            configured = document_with_configured_roles()
            # `ops` keeps every capability except the one under test, so its
            # refusal below is about the capability and not about the role.
            for role in configured["roles"]:
                if role["name"] == "ops":
                    role["capabilities"] = [
                        c for c in role["capabilities"] if c != "request_publication"
                    ]
            t.post_json(
                base,
                "/api/tickets/actions/configure_workflow",
                {"document": validate(configured), "expected_revision": 0},
                caller="director",
            )

            handed = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/reassign",
                {"assignee": "delta", "reason": "delta is taking this"},
                caller="director",
            )
            assert handed["ticket"]["assignee"] == "delta", handed

            # A role this repo has never heard of, carrying the capability,
            # asks -- and is admitted by the capability alone.
            asked = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "roles/delta/first-work", "commit": COMMIT, "bundle": BUNDLE},
                caller="delta",
            )
            request_id = asked["request"]["id"]
            assert asked["request"]["requested_by"] == "delta", asked

            # A role bearing a name every static table in this repo knows,
            # whose document does not give it the capability, is refused --
            # atomically, with nothing recorded.
            refused = t.post_json(
                base,
                "/api/tickets/PGU-2/actions/request_publication",
                {"ref": "ops/second-work", "commit": COMMIT, "bundle": BUNDLE},
                caller="ops",
                expect=403,
            )
            assert "ops cannot call request_publication" in str(refused), refused
            assert app.publication_requests(ticket_id="PGU-2") == []

            # Answering follows control authority, not the name: `clerk` holds
            # the capability, so the handler admits it, and the database refuses
            # it because it controls nothing.
            clerk = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": request_id, "outcome": "published", "detail": "not mine to give"},
                caller="clerk",
                expect=400,
            )
            assert "only the control role may resolve a publication request" in str(clerk), clerk
            assert app.publication_requests(state="requested"), "the ask must still be open"

            # And a role that is not called director but holds control
            # authority answers it.
            resolved = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": request_id, "outcome": "published", "detail": "pushed"},
                caller="steward",
            )
            assert resolved["request"]["state"] == "published", resolved
            assert resolved["request"]["decided_by"] == "steward", resolved

            # The notification went to a control role by capability too.
            queued = t.psql(
                admin,
                "SELECT coalesce(jsonb_agg(target_role), '[]'::jsonb) "
                "FROM ticket_board.ticket_notification_queue WHERE kind = 'publication';",
            )
            assert "delta" in queued, queued

            # A stored document whose controller has lost its control
            # capabilities declares nobody who could answer. The ask is refused
            # rather than routed to whichever role is called director -- the
            # database half of the same rule the publisher enforces.
            t.psql(
                admin,
                """
                UPDATE ticket_board.workflow_roles
                SET definition = jsonb_set(definition, '{capabilities}',
                    (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
                     FROM jsonb_array_elements_text(definition->'capabilities') c
                     WHERE c NOT IN ('merge', 'set_manually_controlled')));
                """,
            )
            headless = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "roles/delta/second-work", "commit": COMMIT, "bundle": BUNDLE},
                caller="delta",
                expect=400,
            )
            assert "declares no role with control authority" in str(headless), headless
            assert app.publication_requests(state="requested") == [], "nothing may be recorded"

            # Neither half is reachable on a board with no declared workflow:
            # there is no capability to admit a caller by, and admitting by name
            # is what this is for.
            t.psql(admin, "DELETE FROM ticket_board.workflow_configuration;")
            app._workflow_states_cache = None
            # Callers this board's pre-declarative tables do know, so what is
            # being shown is the operation refusing rather than the role being
            # unrecognised.
            for operation, caller in (
                ("request_publication", "ops"),
                ("resolve_publication", "director"),
            ):
                legacy = t.post_json(
                    base,
                    f"/api/tickets/PGU-1/actions/{operation}",
                    {"ref": "roles/delta/x", "commit": COMMIT, "bundle": BUNDLE,
                     "request_id": request_id, "outcome": "rejected", "detail": "no"},
                    caller=caller,
                    expect=403,
                )
                assert "requires a declared workflow" in str(legacy), (operation, legacy)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("publication_declarative_authority_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
