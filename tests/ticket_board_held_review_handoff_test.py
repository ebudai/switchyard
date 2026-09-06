#!/usr/bin/env python3
"""SYRD-22: legacy held approvals through real API, PostgreSQL and pane delivery."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import psycopg
import ticket_board_write_api_test as fixture
from scripts.ticket_board.notify_listener import (
    TicketBoardNotifyListener,
    DirectorctlSender,
)

MIGRATION = (
    ROOT / "scripts/ticket_board/migrations/pgu922_syrd22_held_review_handoff.sql"
)


def main():
    with tempfile.TemporaryDirectory(prefix="syrd22-") as temp:
        root = Path(temp)
        data, sock = root / "pg", root / "socket"
        sock.mkdir()
        port = fixture.free_port()
        admin = fixture.conninfo(sock, port, "held_test")
        fixture.run(
            [
                "initdb",
                "-D",
                str(data),
                "-A",
                "trust",
                "--no-locale",
                "--username=postgres",
            ]
        )
        fixture.run(
            [
                "pg_ctl",
                "-D",
                str(data),
                "-o",
                f"-k {sock} -p {port} -h ''",
                "-l",
                str(root / "postgres.log"),
                "-w",
                "start",
            ]
        )
        try:
            fixture.run(
                [
                    "createdb",
                    "-h",
                    str(sock),
                    "-p",
                    str(port),
                    "-U",
                    "postgres",
                    "held_test",
                ]
            )
            if "--upgrade" in sys.argv:
                schema = subprocess.check_output(
                    [
                        "git",
                        "show",
                        "8779d592ca2a72968db56ae468cddf72072be111:scripts/ticket_board/schema.sql",
                    ],
                    text=True,
                )
            else:
                schema = fixture.SCHEMA_PATH.read_text()
            fixture.psql(admin, schema)
            fixture.create_roles(admin)
            fixture.psql(admin, fixture.RBAC_PATH.read_text())
            if "--upgrade" in sys.argv:
                fixture.psql(admin, "BEGIN;\n" + MIGRATION.read_text() + "\nCOMMIT;")
            app = fixture.TicketBoardApp(
                root / "frames",
                root / "assets",
                project="pgu",
                ticket_prefix="PGU",
                database_url=fixture.conninfo(
                    sock, port, "held_test", fixture.SERVICE_ROLE
                ),
            )
            server = fixture.TicketBoardServer(
                ("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier()
            )
            fixture.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def sql(query):
                return fixture.psql(admin, query).strip()

            def action(ticket, operation, actor, **payload):
                return fixture.post_json(
                    base,
                    f"/api/tickets/{ticket}/actions/{operation}",
                    payload,
                    caller=actor,
                )["ticket"]

            def generation(ticket):
                return sql(
                    f"SELECT awaiting_since_at FROM ticket_board.ticket_notification_state WHERE ticket_id='{ticket}'"
                )

            def count(ticket):
                return int(
                    sql(
                        f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket}' AND kind='awaiting_role'"
                    )
                )

            try:
                cases = [
                    (
                        1,
                        "audit",
                        "audit",
                        True,
                        False,
                        "audit_sign_off",
                        "audit_signoff",
                        "director",
                    ),
                    (
                        2,
                        "audit",
                        "audit",
                        True,
                        True,
                        "audit_sign_off",
                        "audit_signoff",
                        "director",
                    ),
                    (
                        3,
                        "inspection",
                        "inspector",
                        True,
                        False,
                        "inspector_sign_off",
                        "inspector_signoff",
                        "audit",
                    ),
                    (
                        4,
                        "inspection",
                        "inspector",
                        False,
                        False,
                        "inspector_sign_off",
                        "inspector_signoff",
                        "director",
                    ),
                    (
                        5,
                        "inspection",
                        "inspector",
                        False,
                        True,
                        "inspector_sign_off",
                        "inspector_signoff",
                        "director",
                    ),
                    (
                        6,
                        "user_review",
                        "user",
                        True,
                        True,
                        "user_sign_off",
                        "user_signoff",
                        "director",
                    ),
                ]
                for (
                    number,
                    stage,
                    owner,
                    audit,
                    uat,
                    operation,
                    flag,
                    recipient,
                ) in cases:
                    ticket = f"PGU-{number}"
                    fixture.seed_postgres_ticket(
                        admin,
                        ticket,
                        title="Completed held review fixture",
                        state=stage,
                        assignee=owner,
                        needs_inspection=stage == "inspection",
                        needs_audit=audit,
                        needs_user_signoff=uat,
                        audit_signoff=stage == "user_review",
                        commit_exempt=True,
                    )
                    app.update_ticket(
                        ticket, {"manually_controlled": True}, caller_role="director"
                    )
                    fixture.post_json(
                        base,
                        f"/api/tickets/{ticket}/actions/{operation}",
                        {"text": "Unauthorized approval"},
                        caller="main",
                        expect=403,
                    )
                    assert app.get_ticket(ticket)[flag] is False
                    assert count(ticket) == 0
                    updated = action(ticket, operation, owner, text="Review completed.")
                    assert (
                        updated["state"],
                        updated["assignee"],
                        updated["awaiting_role"],
                    ) == (stage, owner, recipient), updated
                    assert (
                        updated[flag] is True and updated["manually_controlled"] is True
                    )
                    assert count(ticket) == 4
                    before = generation(ticket)
                    fixture.psql(
                        admin, "BEGIN;\n" + MIGRATION.read_text() + "\nCOMMIT;"
                    )
                    action(ticket, operation, owner, text="Repeated review.")
                    assert generation(ticket) == before and count(ticket) == 4
                    # Direct helper invocation cannot bypass reviewer authorization.
                    with app._pg_connect() as conn:
                        app._pg_set_caller_role(conn, "main")
                        try:
                            conn.execute(
                                "SELECT ticket_board.notify_held_review_completion(%s,%s)",
                                (ticket, stage),
                            )
                        except psycopg.errors.InsufficientPrivilege:
                            conn.rollback()
                        else:
                            raise AssertionError("private handoff helper was callable")
                # Only retained PGU-1 rows participate in the focused delivery test.
                sql(
                    "DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id NOT IN ('PGU-1','PGU-6') OR kind<>'awaiting_role'"
                )
                sql(
                    "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp()+interval '1 hour' WHERE ticket_id='PGU-6'"
                )
                sent = []
                listener_url = fixture.conninfo(
                    sock, port, "held_test", "ticket_board_listener"
                )

                def worker(sender, busy=False):
                    return TicketBoardNotifyListener(
                        conninfo=listener_url,
                        sender=sender,
                        activity_gate=lambda target: busy,
                        target_exists=lambda target: True,
                    )

                def process(listener):
                    with psycopg.connect(listener_url, autocommit=True) as conn:
                        return listener.process_due_notifications(conn)

                def due():
                    sql(
                        "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp() WHERE ticket_id='PGU-1' AND payload->>'step'='1'"
                    )

                assert (
                    process(
                        worker(
                            lambda target, message: sent.append((target, message)),
                            busy=True,
                        )
                    )
                    == 0
                )
                assert count("PGU-1") == 4
                due()

                def failed(target, message):
                    raise OSError("fixture lost delivery")

                assert process(worker(failed)) == 0 and count("PGU-1") == 4
                due()
                # Send with the shipped directorctl into a private tmux server.
                tmux = shutil.which("tmux")
                assert tmux
                name = f"syrd22-{os.getpid()}"
                received = root / "received.txt"
                receiver = root / "receiver.sh"
                receiver.write_text(
                    '#!/bin/sh\nwhile IFS= read -r line; do printf "%s\\n" "$line" >> '
                    + shlex.quote(str(received))
                    + '; printf "received:%s\\n" "$line"; done\n'
                )
                receiver.chmod(0o755)
                wrapper = root / "tmux"
                wrapper.write_text(
                    "#!/bin/sh\nexec "
                    + shlex.quote(tmux)
                    + " -L "
                    + shlex.quote(name)
                    + ' "$@"\n'
                )
                wrapper.chmod(0o755)
                subprocess.run(
                    [
                        tmux,
                        "-L",
                        name,
                        "new-session",
                        "-d",
                        "-s",
                        "pgu-director",
                        str(receiver),
                    ],
                    check=True,
                )
                try:
                    with patch.dict(
                        os.environ,
                        {
                            "PATH": str(root) + os.pathsep + os.environ["PATH"],
                            "DIRECTORCTL_ENTER_DELAY": "0",
                        },
                    ):
                        installed = root / "directorctl"
                        subprocess.run(
                            ["bash", str(ROOT / "scripts/install-directorctl")],
                            env={
                                **os.environ,
                                "DIRECTORCTL_INSTALL_PATH": str(installed),
                            },
                            check=True,
                            capture_output=True,
                        )
                        assert process(worker(DirectorctlSender(str(installed)))) == 1
                    for attempt in range(50):
                        if received.exists() and "PGU-1" in received.read_text():
                            break
                        time.sleep(0.02)
                    assert "awaiting director" in received.read_text()
                finally:
                    subprocess.run([tmux, "-L", name, "kill-server"], check=True)
                assert (
                    app.get_ticket("PGU-1")["awaiting_role"] == "director"
                    and count("PGU-1") == 3
                )
                # ACK and repeated signoff retain the same pending generation.
                before = generation("PGU-1")
                action("PGU-1", "audit_sign_off", "audit", text="Repeat after receipt")
                assert generation("PGU-1") == before and count("PGU-1") == 3
                app.clear_awaiting_role("PGU-1", caller_role="director")
                action(
                    "PGU-1", "audit_sign_off", "audit", text="Repeat after resolution"
                )
                assert app.get_ticket("PGU-1")["awaiting_role"] == ""
                sql(
                    "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp() WHERE ticket_id='PGU-1'"
                )
                assert (
                    process(
                        worker(lambda target, message: sent.append((target, message)))
                    )
                    == 0
                )
                assert count("PGU-1") == 0
                # Retarget a UAT held completion; original Director delivery is stale.
                old_generation = generation("PGU-6")
                app.set_awaiting_role("PGU-6", "ops", caller_role="director")
                assert generation("PGU-6") != old_generation
                sql(
                    "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp() WHERE ticket_id='PGU-6' AND (payload->>'awaiting_since_at')::timestamptz='"
                    + old_generation
                    + "'"
                )
                assert (
                    process(
                        worker(lambda target, message: sent.append((target, message)))
                    )
                    == 1
                )
                assert sent[-1][0] == "pgu-ops:0.0"
                assert (
                    sql(
                        "SELECT count(*) FROM ticket_board.ticket_notification_queue "
                        "WHERE ticket_id='PGU-6' AND kind='awaiting_role' "
                        "AND (payload->>'awaiting_since_at')::timestamptz='"
                        + old_generation
                        + "'"
                    )
                    == "0"
                )
                app.clear_awaiting_role("PGU-6", caller_role="ops")
                released = app.update_ticket(
                    "PGU-6", {"manually_controlled": False}, caller_role="director"
                )
                assert released["state"] == "director_review"
                sql(
                    "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at=clock_timestamp() WHERE kind='awaiting_role'"
                )
                before_sent = len(sent)
                process(worker(lambda target, message: sent.append((target, message))))
                assert not any(
                    "awaiting ops" in message for target, message in sent[before_sent:]
                )
                # Legacy custom stage ownership determines the next recipient.
                sql(
                    "UPDATE ticket_board.workflow_stages SET owner_roles=ARRAY['research'] WHERE name='audit'"
                )
                fixture.seed_postgres_ticket(
                    admin,
                    "PGU-8",
                    title="Custom audit owner",
                    state="inspection",
                    assignee="inspector",
                    needs_inspection=True,
                    commit_exempt=True,
                )
                app.update_ticket(
                    "PGU-8", {"manually_controlled": True}, caller_role="director"
                )
                assert (
                    action("PGU-8", "inspector_sign_off", "inspector")["awaiting_role"]
                    == "research"
                )
                sql(
                    "UPDATE ticket_board.workflow_stages SET owner_roles=ARRAY['audit'] WHERE name='audit'"
                )
                # Unheld audit still advances normally and creates no held wait.
                fixture.seed_postgres_ticket(
                    admin,
                    "PGU-7",
                    title="Unheld review",
                    state="audit",
                    assignee="audit",
                    commit_exempt=True,
                )
                assert (
                    action("PGU-7", "audit_sign_off", "audit", text="Unheld approval")[
                        "state"
                    ]
                    == "director_review"
                )
                assert count("PGU-7") == 0
                print(
                    "held review: audit/inspection/UAT gates, state hold, persisted schedule, busy/failure retry, real pane receipt, repeat/clear/retarget and migration idempotence passed"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        finally:
            fixture.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"])


if __name__ == "__main__":
    main()
