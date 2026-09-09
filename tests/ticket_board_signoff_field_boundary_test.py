#!/usr/bin/env python3
"""SYRD-80: a field write cannot become a review.

The Director may edit a ticket's fields and may clear a stale approval during
rework. What it may never do is write `audit_signoff`, `inspector_signoff` or
`user_signoff` true, because that is the reviewer's decision and not a property
of the record. This checks that at the boundary that actually decides -- real
PostgreSQL, through the shipped functions -- rather than at the Python layer in
front of it, since a direct socket or HTTP variant reaches the database without
passing the handler's own guards.

Every refusal is checked for atomicity: the flag stays false, and nothing else
the same call would have written lands either.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

from temporary_cluster import temporary_cluster  # noqa: E402

SIGNOFFS = ("audit_signoff", "inspector_signoff", "user_signoff")
# Whose decision each one is. Nobody else may write it true, the Director least
# of all, since it is the role the other slices of SYRD-71 are about.
OWNER = {"audit_signoff": "audit", "inspector_signoff": "inspector", "user_signoff": "user"}


def rejected(call, expected: str = ""):
    try:
        call()
    except Exception as exc:  # noqa: BLE001 -- the refusal is the assertion
        if expected:
            assert expected in str(exc), f"expected {expected!r} in: {exc}"
        return str(exc)
    raise AssertionError(f"expected a refusal containing {expected!r}")


def flags(admin: str, ticket_id: str) -> dict[str, bool]:
    row = t.psql(
        admin,
        "SELECT audit_signoff::text || ' ' || inspector_signoff::text || ' ' || user_signoff::text "
        f"FROM ticket_board.tickets WHERE id = '{ticket_id}';",
    )
    values = row.split()
    return dict(zip(SIGNOFFS, [value == "true" for value in values]))


def comment_count(admin: str, ticket_id: str) -> int:
    return int(t.psql(admin, f"SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket_id}';"))


def title_of(admin: str, ticket_id: str) -> str:
    return t.psql(admin, f"SELECT title FROM ticket_board.tickets WHERE id = '{ticket_id}';")


def main() -> int:
    with temporary_cluster(prefix="signoff-boundary-", shutdown="immediate") as cluster:
        root, sock, port = cluster.root, cluster.socket_dir, cluster.port
        db = "signoff_boundary_test"
        admin = t.conninfo(sock, port, db)
        t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        t.create_roles(admin)
        t.psql(admin, t.RBAC_PATH.read_text())

        for number, state, assignee in (
            (1, "audit", "audit"),
            (2, "inspection", "inspector"),
            (3, "in_progress", "app"),
        ):
            t.seed_postgres_ticket(
                admin, f"PGU-{number}", title=f"Fixture {number}", state=state, assignee=assignee, commit_exempt=True
            )

        app = t.TicketBoardApp(
            root / "frames",
            root / "assets",
            project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
        )
        document = validate(json.loads((ROOT / "examples/workflows/inspection.json").read_text()))
        app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")

        run_checks(app, admin, root)
    print("ticket_board_signoff_field_boundary_test: ok")
    return 0


def run_checks(app, admin: str, root: Path) -> None:
    # --- the Director cannot write any approval through edit_fields ----------
    #
    # Checked in the database function, not in the handler, because that is the
    # boundary a direct socket or HTTP variant still has to cross.
    for flag in SIGNOFFS:
        before_flags = flags(admin, "PGU-1")
        before_comments = comment_count(admin, "PGU-1")
        message = rejected(
            lambda flag=flag: app.update_ticket(
                "PGU-1", {flag: True, "title": "renamed while forging a review"}, caller_role="director"
            )
        )
        assert flag.split("_")[0] in message or "sign_off" in message, (flag, message)
        # Atomic: the flag did not move and neither did the field the same call
        # would have written alongside it.
        assert flags(admin, "PGU-1") == before_flags, (flag, flags(admin, "PGU-1"))
        assert comment_count(admin, "PGU-1") == before_comments, flag
        assert title_of(admin, "PGU-1") == "Fixture 1", (flag, title_of(admin, "PGU-1"))

    # --- and neither can anyone else, including the reviewer's neighbours ----
    for flag in SIGNOFFS:
        for caller in ("app", "ops", "main", "director"):
            before_flags = flags(admin, "PGU-1")
            rejected(lambda flag=flag, caller=caller: app.update_ticket("PGU-1", {flag: True}, caller_role=caller))
            assert flags(admin, "PGU-1") == before_flags, (flag, caller)

    # --- the edit_fields SQL entry point refuses it on its own ---------------
    #
    # Reached directly, with no Python in front of it at all.
    for flag in SIGNOFFS:
        before_flags = flags(admin, "PGU-1")
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            rejected(
                lambda flag=flag, conn=conn: conn.execute(
                    "SELECT ticket_board.edit_fields(%s, %s::jsonb);", ("PGU-1", json.dumps({flag: True}))
                ),
                f"{flag}=true requires",
            )
            conn.rollback()
        assert flags(admin, "PGU-1") == before_flags, flag

    # --- the reviewer's own action still works (or the negatives prove nothing)
    assert app.perform_workflow_action("PGU-2", "inspector_sign_off", {}, caller_role="inspector")[
        "inspector_signoff"
    ] is True
    assert flags(admin, "PGU-2")["inspector_signoff"] is True

    # --- clearing belongs to rework, and rework is a transition -------------
    #
    # A same-state field edit cannot clear an approval either: a sign-off moves
    # through something that records why it moved. That is not the Director
    # losing the ability to clear -- it is where the ability lives.
    before_flags = flags(admin, "PGU-2")
    rejected(
        lambda: app.update_ticket("PGU-2", {"inspector_signoff": False}, caller_role="director"),
        "flag change requires authorized workflow action",
    )
    assert flags(admin, "PGU-2") == before_flags, flags(admin, "PGU-2")

    # The Director's own kick-back is the route, and it does clear. Sending work
    # back is not forging the review it discards (SYRD-81), and refusing to let
    # it clear would strand a ticket behind a stale approval.
    t.seed_postgres_ticket(
        admin,
        "PGU-4",
        title="Stale approval",
        state="director_review",
        assignee="director",
        audit_signoff=True,
        commit_exempt=True,
    )
    assert flags(admin, "PGU-4")["audit_signoff"] is True, flags(admin, "PGU-4")
    returned = app.perform_workflow_action(
        "PGU-4", "director_kick_back", {"text": "stale approval, back to implementation"}, caller_role="director"
    )
    assert returned["state"] == "in_progress", returned
    assert returned["audit_signoff"] is False, returned
    assert flags(admin, "PGU-4")["audit_signoff"] is False, flags(admin, "PGU-4")

    # And that same kick-back cannot raise one on the way past.
    for flag in SIGNOFFS:
        assert flags(admin, "PGU-4")[flag] is False, (flag, flags(admin, "PGU-4"))

    # --- an import cannot arrive already approved ---------------------------
    #
    # The database create function has no sign-off parameter at all, so there is
    # nothing to pass; the Python layer refuses before it gets that far.
    for flag in SIGNOFFS:
        rejected(
            lambda flag=flag: app.create_ticket_record(
                title="Imported already approved",
                body="",
                screenshot=None,
                screenshots=None,
                assignee="unassigned",
                state="analysis",
                blocked_by=None,
                implementation="",
                audit_prompt="",
                audit_signoff=flag == "audit_signoff",
                needs_audit=True,
                needs_inspection=False,
                inspector_signoff=flag == "inspector_signoff",
                needs_user_signoff=False,
                user_signoff=flag == "user_signoff",
                comments=[],
                caller_role="director",
            ),
            "initial signoff fields",
        )
    signature = t.psql(
        admin,
        "SELECT count(*) FROM information_schema.parameters WHERE specific_schema='ticket_board' "
        "AND parameter_name IN ('audit_signoff','inspector_signoff','user_signoff');",
    )
    assert signature == "0", f"a ticket_board function takes a sign-off parameter: {signature}"

    # --- the same refusal over the real Unix socket -------------------------
    run_socket_checks(app, admin, root)


def run_socket_checks(app, admin: str, root: Path) -> None:
    """A direct socket variant reaches the same boundary and is refused there."""
    import http.client
    import socket as socket_module
    import tempfile

    class UnixConnection(http.client.HTTPConnection):
        def __init__(self, path: str) -> None:
            super().__init__("localhost")
            self._path = path

        def connect(self) -> None:
            connection = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            connection.connect(self._path)
            self.sock = connection

    with tempfile.TemporaryDirectory(prefix="signoff-boundary-socket.") as tmp:
        socket_path = Path(tmp) / "board.sock"
        server = t.TicketBoardUnixServer(
            socket_path,
            app,
            events=t.TicketBoardEventHub(app),
            director_notifier=t.QuietNotifier(),
            role_authority=t.local_role_authority_as("director"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for flag in SIGNOFFS:
                before_flags = flags(admin, "PGU-1")
                connection = UnixConnection(str(socket_path))
                try:
                    connection.request(
                        "POST",
                        "/api/tickets/PGU-1/actions/edit_fields",
                        body=json.dumps({flag: True}),
                        headers={"Content-Type": "application/json"},
                    )
                    response = connection.getresponse()
                    status, body = response.status, response.read().decode("utf-8", errors="replace")
                finally:
                    connection.close()
                assert status in (400, 403), (flag, status, body)
                assert "sign_off" in body or "requires" in body, (flag, body)
                assert flags(admin, "PGU-1") == before_flags, (flag, flags(admin, "PGU-1"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
