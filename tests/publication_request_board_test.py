#!/usr/bin/env python3
"""SYRD-93: the board half of publication -- the ask, and what became of it.

Every role runs as one Unix account, so an implementer that could push could
push anything. This drives the replacement: the implementer records a durable
ask, the control role records a decision, and neither half is a credential.

Real cluster, real declarative workflow, real HTTP server, so what is proved is
what the handler and the database do rather than what a stub agrees to.
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
from publication_cache_fixture import build_cache, commit_file, publish
from scripts.ticket_board.workflow_config import validate
from temporary_cluster import temporary_cluster

BUNDLE = "/home/agent/.local/state/switchyard/publish-outbox/syrd/PGU-1.bundle"


def requests_for(admin: str, ticket: str) -> list[dict]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(to_jsonb(r) ORDER BY r.id), '[]'::jsonb) "
        f"FROM ticket_board.publication_requests r WHERE ticket_id = '{ticket}';",
    )
    return json.loads(raw)


def awaiting(admin: str, ticket: str) -> str:
    return t.psql(
        admin,
        "SELECT awaiting_role FROM ticket_board.ticket_notification_state "
        f"WHERE ticket_id = '{ticket}';",
    )


def queued_for(admin: str, role: str) -> list[str]:
    raw = t.psql(
        admin,
        "SELECT coalesce(jsonb_agg(message ORDER BY id), '[]'::jsonb) "
        f"FROM ticket_board.ticket_notification_queue WHERE target_role = '{role}' "
        "AND kind = 'publication';",
    )
    return json.loads(raw)


def main() -> int:
    with temporary_cluster(prefix="publication-board-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "publication_request_board_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())
        t.seed_postgres_ticket(admin, "PGU-1", title="Ops work", state="in_progress", assignee="ops")
        t.seed_postgres_ticket(admin, "PGU-2", title="Main work", state="in_progress", assignee="main")
        (root / "frames").mkdir(exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        # Real commits in a real cache: a published verdict is only recorded
        # for a ref the board can resolve there for itself (SYRD-118).
        cache, work = build_cache(root)
        COMMIT_A = commit_file(work, "a")
        COMMIT_B = commit_file(work, "b")
        COMMIT_C = commit_file(work, "c")
        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            commit_git_dir=str(cache),
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

            # The ask itself: no credential, no remote, no push.
            asked = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "ops/syrd-92-defer-backlog", "commit": COMMIT_A, "bundle": BUNDLE},
                caller="ops",
            )
            request = asked["request"]
            assert request["state"] == "requested", request
            assert request["requested_by"] == "ops", request
            assert request["commit_hash"] == COMMIT_A, request
            # The Director is waiting, and knows what for.
            assert awaiting(admin, "PGU-1") == "director"
            messages = queued_for(admin, "director")
            assert any("ops asks to publish ops/syrd-92-defer-backlog" in m for m in messages), messages
            # The ticket did not move, and nothing was signed.
            ticket = app.get_ticket("PGU-1")
            assert ticket["state"] == "in_progress" and ticket["assignee"] == "ops", ticket
            assert ticket["audit_signoff"] is False and ticket["commit_hash"] == "", ticket

            # The same ask again is the same ask: a retry after an interruption
            # records nothing new.
            again = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "ops/syrd-92-defer-backlog", "commit": COMMIT_A, "bundle": BUNDLE},
                caller="ops",
            )
            assert again["request"]["id"] == request["id"], (again, request)
            assert len(requests_for(admin, "PGU-1")) == 1

            # An amended commit replaces the ask, and both stay on the record.
            amended = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "ops/syrd-92-defer-backlog", "commit": COMMIT_B, "bundle": BUNDLE},
                caller="ops",
            )
            rows = requests_for(admin, "PGU-1")
            assert len(rows) == 2, rows
            assert [r["state"] for r in rows] == ["superseded", "requested"], rows
            assert amended["request"]["commit_hash"] == COMMIT_B

            # Somebody else's ticket is not yours to publish.
            denied = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "main/whatever", "commit": COMMIT_A, "bundle": BUNDLE},
                caller="main",
                expect=400,
            )
            assert "own ticket" in str(denied), denied

            # The role called main cannot publish main/..., because git cannot
            # hold refs/heads/main and refs/heads/main/x at once. It is told
            # what does work.
            collision = t.post_json(
                base,
                "/api/tickets/PGU-2/actions/request_publication",
                {"ref": "main/syrd-93", "commit": COMMIT_A, "bundle": BUNDLE},
                caller="main",
                expect=400,
            )
            assert "collides with an integration branch" in str(collision), collision
            assert "roles/main/" in str(collision), collision
            namespaced = t.post_json(
                base,
                "/api/tickets/PGU-2/actions/request_publication",
                {"ref": "roles/main/syrd-93", "commit": COMMIT_A, "bundle": BUNDLE},
                caller="main",
            )
            assert namespaced["request"]["ref"] == "roles/main/syrd-93"

            # Another role's namespace, an integration branch, a short commit
            # and a relative bundle are each refused with nothing recorded.
            for payload, expected in (
                ({"ref": "ops/somebody-elses", "commit": COMMIT_A, "bundle": BUNDLE},
                 "may only publish refs under main/"),
                ({"ref": "release", "commit": COMMIT_A, "bundle": BUNDLE},
                 "collides with an integration branch"),
                ({"ref": "roles/main/x", "commit": "abc123", "bundle": BUNDLE},
                 "full 40-character commit"),
                ({"ref": "roles/main/x", "commit": COMMIT_A, "bundle": "../escape.bundle"},
                 "absolute path"),
            ):
                refused = t.post_json(
                    base,
                    "/api/tickets/PGU-2/actions/request_publication",
                    payload,
                    caller="main",
                    expect=400,
                )
                assert expected in str(refused), (payload, refused)
            assert len(requests_for(admin, "PGU-2")) == 1

            # Deciding is the control role's alone.
            open_request = [r for r in requests_for(admin, "PGU-1") if r["state"] == "requested"][0]
            for role in ("ops", "main", "audit", "inspector"):
                refused = t.post_json(
                    base,
                    "/api/tickets/PGU-1/actions/resolve_publication",
                    {"request_id": open_request["id"], "outcome": "published"},
                    caller=role,
                    expect=403,
                )
                assert f"{role} cannot call resolve_publication" in str(refused), refused
            assert [r for r in requests_for(admin, "PGU-1") if r["state"] == "requested"]

            # A rejection has to say why.
            silent = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": open_request["id"], "outcome": "rejected"},
                caller="director",
                expect=400,
            )
            assert "requires a reason" in str(silent), silent

            # Before anything was pushed the verdict is refused, and the ask
            # survives to be answered again (SYRD-118).
            unproven = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": open_request["id"], "outcome": "published", "detail": "pushed"},
                caller="director",
                expect=400,
            )
            assert "is not published" in str(unproven), unproven
            assert [r for r in requests_for(admin, "PGU-1") if r["state"] == "requested"]

            publish(cache, work, "ops/syrd-92-defer-backlog", COMMIT_B)
            published = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": open_request["id"], "outcome": "published", "detail": "pushed"},
                caller="director",
            )
            assert published["request"]["state"] == "published", published
            assert published["request"]["verified_commit"] == COMMIT_B, published
            assert published["request"]["decided_by"] == "director", published
            # The wait is cleared and the implementer is told, by the board, not
            # by the Director remembering to.
            assert awaiting(admin, "PGU-1") == ""
            assert any(
                "is published at" in message for message in queued_for(admin, "ops")
            ), queued_for(admin, "ops")
            # Publishing did not submit anything for the implementer.
            after = app.get_ticket("PGU-1")
            assert after["state"] == "in_progress" and after["assignee"] == "ops", after

            # Recording the same outcome twice is not an error: a publisher that
            # pushed and then lost its connection re-runs safely.
            repeat = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": open_request["id"], "outcome": "published", "detail": "pushed"},
                caller="director",
            )
            assert repeat["request"]["state"] == "published"
            # But it cannot be turned into a rejection afterwards.
            reversed_ = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": open_request["id"], "outcome": "rejected", "detail": "no"},
                caller="director",
                expect=400,
            )
            assert "already published" in str(reversed_), reversed_

            # A rejection reaches the implementer with the reason attached.
            main_request = requests_for(admin, "PGU-2")[0]
            rejected = t.post_json(
                base,
                "/api/tickets/PGU-2/actions/resolve_publication",
                {"request_id": main_request["id"], "outcome": "rejected",
                 "detail": "bundle does not verify"},
                caller="director",
            )
            assert rejected["request"]["state"] == "rejected", rejected
            assert awaiting(admin, "PGU-2") == ""
            assert any(
                "bundle does not verify" in message for message in queued_for(admin, "main")
            ), queued_for(admin, "main")

            # Two roles can be waiting at once, each on its own ticket.
            t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "ops/syrd-92-defer-backlog", "commit": COMMIT_A, "bundle": BUNDLE},
                caller="ops",
            )
            t.post_json(
                base,
                "/api/tickets/PGU-2/actions/request_publication",
                {"ref": "roles/main/syrd-93", "commit": COMMIT_B, "bundle": BUNDLE},
                caller="main",
            )
            import urllib.request

            with urllib.request.urlopen(base + "/api/publications?state=requested", timeout=5) as response:
                listing = json.loads(response.read().decode("utf-8"))
            waiting = {r["ticket_id"]: r["requested_by"] for r in listing["requests"]}
            assert waiting == {"PGU-1": "ops", "PGU-2": "main"}, listing

            # SYRD-119: what the publisher leases a replacement against. PGU-1's
            # open ask names a ref this board has published before, so the row
            # carries that commit; PGU-2's names one whose only prior ask was
            # rejected, so it carries nothing and the publisher will refuse to
            # replace anything sitting there. Superseded and rejected rows are
            # not publications, and a ref with no record is not one this
            # workflow may overwrite.
            by_ticket = {r["ticket_id"]: r for r in listing["requests"]}
            assert by_ticket["PGU-1"]["previous_published_commit"] == COMMIT_B, by_ticket["PGU-1"]
            assert by_ticket["PGU-2"]["previous_published_commit"] == "", by_ticket["PGU-2"]
            # The open request never names itself, whatever its own state.
            assert by_ticket["PGU-1"]["commit_hash"] == COMMIT_A, by_ticket["PGU-1"]
            # And the same answer travels with the ticket, so the panel and the
            # publisher cannot be looking at two different records.
            assert app.get_ticket("PGU-1")["publication"]["previous_published_commit"] == COMMIT_B

            # A ref published twice reports the LATEST publication, not the
            # first. This is the shape that produced SYRD-119: a candidate is
            # published, rebuilt, published again, and rebuilt again. Leasing
            # against the first publication would refuse every replacement after
            # the second one.
            second = [r for r in requests_for(admin, "PGU-1") if r["state"] == "requested"][0]
            # The replacement really is on the ref before the board records it.
            publish(cache, work, "ops/syrd-92-defer-backlog", COMMIT_A)
            t.post_json(
                base,
                "/api/tickets/PGU-1/actions/resolve_publication",
                {"request_id": second["id"], "outcome": "published", "detail": "pushed again"},
                caller="director",
            )
            third = t.post_json(
                base,
                "/api/tickets/PGU-1/actions/request_publication",
                {"ref": "ops/syrd-92-defer-backlog", "commit": COMMIT_C, "bundle": BUNDLE},
                caller="ops",
            )
            assert third["request"]["previous_published_commit"] == COMMIT_A, third["request"]

            # No row is ever its own predecessor. Only requested rows reach the
            # publisher, where a self-reference cannot arise, but this field is
            # in every listing's JSON and an answer of "itself" would be a trap
            # for whoever reads it next.
            with urllib.request.urlopen(base + "/api/publications?state=published", timeout=5) as response:
                done = json.loads(response.read().decode("utf-8"))
            assert done["requests"], done
            for row in done["requests"]:
                assert row["previous_published_commit"] != row["commit_hash"], row
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    print("publication_request_board_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
