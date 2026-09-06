#!/usr/bin/env python3
"""Configured workflow against real PostgreSQL and authenticated HTTP boundaries."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import ticket_board_write_api_test as t
from scripts.ticket_board.workflow_config import validate


def rejected(fn, reason=""):
    try:
        fn()
    except Exception as exc:
        if reason:
            assert reason in str(exc), str(exc)
        return
    raise AssertionError("expected rejection")


def main():
    with tempfile.TemporaryDirectory(prefix="workflow-db-") as tmp:
        root = Path(tmp)
        data = root / "data"
        sock = root / "socket"
        sock.mkdir()
        port = t.free_port()
        db = "workflow_test"
        admin = t.conninfo(sock, port, db)
        t.run(
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
        t.run(
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
            t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
            schema = t.SCHEMA_PATH.read_text()
            if "--upgrade" in sys.argv:
                # The pre-foundation schema is retained verbatim above the opt-in
                # section. Exercise installation twice, including RBAC parity.
                legacy = schema.split("-- Opt-in foundation.")[0] + "\nCOMMIT;"
                t.psql(admin, legacy)
                t.create_roles(admin)
                t.psql(admin, t.RBAC_PATH.read_text())
                migration = (
                    ROOT
                    / "scripts/ticket_board/migrations/pgu921_syrd11_declarative_workflow.sql"
                ).read_text()
                t.psql(admin, "BEGIN;\n" + migration + "\nCOMMIT;")
                t.psql(admin, "BEGIN;\n" + migration + "\nCOMMIT;")
            else:
                t.psql(admin, schema)
                t.create_roles(admin)
            t.psql(admin, t.RBAC_PATH.read_text())
            for number, assignee, inspect, audit in [
                (1, "main", True, True),
                (2, "app", False, True),
                (3, "ops", False, False),
            ]:
                t.seed_postgres_ticket(
                    admin,
                    f"PGU-{number}",
                    title="Fixture",
                    state="in_progress",
                    assignee=assignee,
                    needs_inspection=inspect,
                    needs_audit=audit,
                    commit_exempt=True,
                )
            t.seed_postgres_ticket(
                admin, "PGU-4", title="Existing draft", state="draft"
            )
            app = t.TicketBoardApp(
                root / "frames",
                root / "assets",
                project="cerulean",
                ticket_prefix="PGU",
                database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE),
            )
            cfg = validate(
                json.loads((ROOT / "examples/workflows/inspection.json").read_text())
            )
            assert app.workflow_configuration() is None
            server = t.TicketBoardServer(
                ("127.0.0.1", 0), app, director_notifier=t.QuietNotifier()
            )
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                hostile = copy.deepcopy(cfg)
                next(r for r in hostile["roles"] if r["name"] == "inspector")[
                    "target"
                ] = "other-inspector:0.0"
                response = t.post_json(
                    base,
                    "/api/tickets/actions/configure_workflow",
                    {"document": hostile, "expected_revision": 0},
                    caller="director",
                    expect=400,
                )
                assert "selected project/role" in str(response), response
                assert app.workflow_configuration() is None
                response = t.post_json(
                    base,
                    "/api/tickets/actions/configure_workflow",
                    {"document": cfg, "expected_revision": 0},
                    caller="main",
                    expect=403,
                )
                assert "only director" in str(response)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            rejected(
                lambda: app.apply_workflow(
                    cfg, expected_revision=0, dry_run=False, caller_role="main"
                ),
                "only director",
            )
            assert app.apply_workflow(
                cfg, expected_revision=0, dry_run=True, caller_role="director"
            )["dry_run"]
            assert app.workflow_configuration() is None
            rev = app.apply_workflow(
                cfg, expected_revision=0, dry_run=False, caller_role="director"
            )["revision"]
            assert app.get_ticket("PGU-4")["assignee"] == "director"
            with app._pg_connect() as unauthorized:
                app._pg_set_caller_role(unauthorized, "unknown_role")
                rejected(
                    lambda: unauthorized.execute(
                        "SELECT ticket_board.apply_declared_workflow(%s::jsonb,%s)",
                        (json.dumps(cfg), rev),
                    ),
                    "invalid configured caller role",
                )
                unauthorized.rollback()

            assert (
                app.apply_workflow(
                    cfg, expected_revision=rev, dry_run=False, caller_role="director"
                )["revision"]
                == rev
            )
            rejected(
                lambda: app.apply_workflow(
                    cfg, expected_revision=0, dry_run=False, caller_role="director"
                ),
                "revision changed",
            )

            def act(ticket, action, actor, **payload):
                return app.perform_workflow_action(
                    ticket, action, payload, caller_role=actor
                )

            rejected(
                lambda: act("PGU-1", "submit_to_audit", "inspector"), "cannot perform"
            )
            assert act("PGU-1", "submit_to_audit", "main")["state"] == "inspection"
            rejected(
                lambda: act("PGU-1", "inspector_sign_off", "main"), "cannot perform"
            )
            rejected(
                lambda: act("PGU-1", "audit_sign_off", "inspector"),
                "unknown or ambiguous",
            )
            assert (
                act(
                    "PGU-1", "inspector_kick_back", "inspector", text="Fixture finding"
                )["assignee"]
                == "main"
            )
            assert (
                act(
                    "PGU-1",
                    "submit_to_audit_without_commit",
                    "main",
                    text="Configuration-only correction",
                )["state"]
                == "inspection"
            )
            assert act("PGU-1", "inspector_sign_off", "inspector")["inspector_signoff"]
            assert app.get_ticket("PGU-1")["state"] == "audit"
            assert act("PGU-2", "submit_to_audit", "app")["state"] == "audit"
            assert act("PGU-3", "submit_to_audit", "ops")["state"] == "director_review"
            rejected(
                lambda: act("PGU-2", "audit_sign_off", "inspector"), "cannot perform"
            )
            assert act("PGU-1", "audit_sign_off", "audit")["state"] == "director_review"
            assert act("PGU-1", "mark_done", "director")["state"] == "done"
            assert (
                act("PGU-2", "audit_kick_back", "audit", text="Fixture correction")[
                    "assignee"
                ]
                == "app"
            )
            # A second reviewer/stage uses data application, in this same board process.
            second = copy.deepcopy(cfg)
            role = copy.deepcopy(
                next(r for r in second["roles"] if r["name"] == "inspector")
            )
            role.update(
                name="verifier",
                label="Verifier",
                target="cerulean-verifier:0.0",
                slot=None,
            )
            second["roles"].append(role)
            second["flags"].update(
                needs_verification={"kind": "gate", "default": True},
                verification_signoff={"kind": "signoff", "default": False},
            )
            stage = copy.deepcopy(
                next(s for s in second["stages"] if s["name"] == "inspection")
            )
            stage.update(
                name="verification",
                label="Verification",
                owners=["verifier"],
                gate="needs_verification",
                skip_to="audit",
                signoff="verification_signoff",
            )
            second["stages"].insert(5, stage)
            for tr in second["transitions"]:
                if tr["from"] == "inspection" and tr["to"] == "audit":
                    tr["to"] = "verification"
            approve = copy.deepcopy(
                next(
                    tr
                    for tr in second["transitions"]
                    if tr["action"] == "inspector_sign_off"
                )
            )
            approve.update(
                **{
                    "from": "verification",
                    "to": "audit",
                    "action": "verify_accept",
                    "label": "Accept verification",
                    "actors": ["verifier"],
                }
            )
            second["transitions"].append(approve)
            app.apply_workflow(
                validate(second),
                expected_revision=rev,
                dry_run=False,
                caller_role="director",
            )
            assert "verifier" in app.workflow_roles()
            assert "verification" in [c["key"] for c in app.workflow_columns()]
            # Existing implementation -> inspection -> second custom review.
            app.update_ticket(
                "PGU-2", {"needs_inspection": True}, caller_role="director"
            )
            assert (
                act(
                    "PGU-2",
                    "submit_to_audit_without_commit",
                    "app",
                    text="No-code correction",
                )["state"]
                == "inspection"
            )
            assert (
                act("PGU-2", "inspector_sign_off", "inspector")["state"]
                == "verification"
            )
            assert (
                t.psql(
                    admin,
                    "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-2' AND target_role='verifier' AND message LIKE '%Verification%'",
                )
                == "1"
            )
            from scripts.ticket_board.notify_listener import (
                TicketBoardNotifyListener,
                PaneActivityGate,
            )
            import psycopg

            sent = []
            gate = PaneActivityGate()
            pane_socket = root / "tmux.sock"
            subprocess.run(
                [
                    "tmux",
                    "-S",
                    str(pane_socket),
                    "new-session",
                    "-d",
                    "-s",
                    "cerulean-verifier",
                    "cat",
                ],
                check=True,
            )

            def receipt_sender(target, message):
                sent.append((target, message))
                if target == "cerulean-verifier:0.0":
                    subprocess.run(
                        [
                            "tmux",
                            "-S",
                            str(pane_socket),
                            "send-keys",
                            "-t",
                            target,
                            "-l",
                            message,
                        ],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "tmux",
                            "-S",
                            str(pane_socket),
                            "send-keys",
                            "-t",
                            target,
                            "Enter",
                        ],
                        check=True,
                    )

            listener = TicketBoardNotifyListener(
                conninfo=t.conninfo(sock, port, db, "ticket_board_listener"),
                project="cerulean",
                sender=receipt_sender,
                activity_gate=gate.is_busy,
                target_exists=lambda target: True,
            )
            with psycopg.connect(listener.conninfo, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                assert listener.role_targets["verifier"] == "cerulean-verifier:0.0"
                assert gate.role_runtimes["verifier"] == "claude"
                assert (
                    gate._expected_runtime_for_target("cerulean-verifier:0.0")
                    == "claude"
                )
                # No GUI/agent is impersonated: idle gate and target existence are explicit fixtures.
                listener.activity_gate = lambda target: False
                assert listener.process_due_notifications(conn) > 0
                assert any(
                    target == "cerulean-verifier:0.0" and "Verification" in message
                    for target, message in sent
                ), sent
                receipt = subprocess.check_output(
                    [
                        "tmux",
                        "-S",
                        str(pane_socket),
                        "capture-pane",
                        "-p",
                        "-t",
                        "cerulean-verifier:0.0",
                    ],
                    text=True,
                )
                assert "PGU-2" in receipt and "Verification" in receipt, receipt
            subprocess.run(["tmux", "-S", str(pane_socket), "kill-server"], check=True)
            # A held approval through the actual served UI records a decision and
            # durable next-owner handoff while preserving the explicit hold.
            app.update_ticket(
                "PGU-2", {"manually_controlled": True}, caller_role="director"
            )
            server = t.TicketBoardServer(
                ("127.0.0.1", 0), app, director_notifier=t.QuietNotifier()
            )
            t.TEST_WRITE_TOKEN = server.write_token
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                # Supported management CLI performs authenticated CAS application,
                # local projection and rollback against the same running board.
                local_config = root / "launcher.json"
                local_config.write_text(
                    json.dumps(
                        {
                            "project": "cerulean",
                            "layout": "layout.json",
                            "repository": str(root),
                            "desktop_access": {"mode": "headless"},
                            "roles": [],
                        }
                    )
                )
                desired = copy.deepcopy(second)
                desired["roles"][-1]["label"] = "Independent verifier"
                policy = root / "management.json"
                policy.write_text(json.dumps(desired))
                journal = root / "management-before.json"
                cli_env = {
                    k: v
                    for k, v in os.environ.items()
                    if not k.startswith(("TICKET_BOARD_", "PGU_"))
                }
                cli_env.update(
                    TICKET_BOARD_CALLER_ROLE="director",
                    TICKET_BOARD_WRITE_TOKEN=server.write_token,
                    PYTHONDONTWRITEBYTECODE="1",
                )
                command = [
                    sys.executable,
                    str(ROOT / "scripts/ticket-board-workflow"),
                    "apply",
                    "--board-url",
                    base,
                    "--config",
                    str(local_config),
                    "--document",
                    str(policy),
                    "--journal",
                    str(journal),
                ]
                before = app.workflow_document()["revision"]
                before_signature = app.store_signature()
                proc = subprocess.run(
                    command + ["--dry-run"], env=cli_env, text=True, capture_output=True
                )
                assert proc.returncode == 0, proc.stderr
                assert (
                    app.workflow_document()["revision"] == before
                    and not journal.exists()
                )
                proc = subprocess.run(
                    command, env=cli_env, text=True, capture_output=True
                )
                assert proc.returncode == 0, proc.stderr
                assert app.workflow_document()["revision"] > before
                assert app.store_signature() != before_signature
                proc = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/ticket-board-workflow"),
                        "rollback",
                        "--board-url",
                        base,
                        "--config",
                        str(local_config),
                        "--journal",
                        str(journal),
                    ],
                    env=cli_env,
                    text=True,
                    capture_output=True,
                )
                assert proc.returncode == 0, proc.stderr
                assert app.workflow_configuration()["roles"][-1]["label"] == "Verifier"
                import ticket_board_tenant_identity_browser_test as browser_test

                with browser_test.load_playwright()() as playwright:
                    options = {"headless": True}
                    if Path("/opt/google/chrome/chrome").exists():
                        options["executable_path"] = "/opt/google/chrome/chrome"
                    browser = playwright.chromium.launch(**options)
                    try:
                        page = browser.new_page(
                            viewport={"width": 1500, "height": 1000}
                        )
                        page.goto(base)
                        page.locator(
                            ".card", has=page.locator(".card-id", has_text="PGU-2")
                        ).click()
                        button = page.get_by_role(
                            "button", name="Accept verification", exact=True
                        )
                        assert button.is_enabled()
                        assert (
                            page.get_by_role(
                                "button",
                                name="Comment + Return to Implementation",
                                exact=True,
                            ).count()
                            == 0
                        )
                        button.click()
                        page.wait_for_function(
                            "async () => (await (await fetch('/api/tickets/PGU-2')).json()).workflow_flags.verification_signoff === true"
                        )
                    finally:
                        browser.close()
                held = app.get_ticket("PGU-2")
                assert (held["state"], held["assignee"], held["awaiting_role"]) == (
                    "verification",
                    "verifier",
                    "audit",
                ), held
                generation = t.psql(
                    admin,
                    "SELECT awaiting_since_at FROM ticket_board.ticket_notification_state WHERE ticket_id='PGU-2'",
                )
                t.post_json(
                    base,
                    "/api/tickets/PGU-2/actions/verify_accept",
                    {},
                    caller="verifier",
                )
                assert generation == t.psql(
                    admin,
                    "SELECT awaiting_since_at FROM ticket_board.ticket_notification_state WHERE ticket_id='PGU-2'",
                )
                assert (
                    t.psql(
                        admin,
                        "SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-2' AND kind='awaiting_role'",
                    )
                    == "4"
                )
                app.clear_awaiting_role("PGU-2", caller_role="audit")
                t.post_json(
                    base,
                    "/api/tickets/PGU-2/actions/verify_accept",
                    {},
                    caller="verifier",
                )
                assert app.get_ticket("PGU-2")["awaiting_role"] == ""
                # Arbitrary reviewer can request a fresh durable handoff, retarget,
                # and resolve without falling through legacy role/stage allowlists.
                app.set_awaiting_role("PGU-2", "inspector", caller_role="verifier")
                app.set_awaiting_role("PGU-2", "director", caller_role="verifier")
                with psycopg.connect(listener.conninfo, autocommit=True) as conn:
                    listener.process_due_notifications(conn)
                assert any(
                    target == "cerulean-director:0.0" and "awaiting director" in message
                    for target, message in sent
                )
                app.clear_awaiting_role("PGU-2", caller_role="director")
                app.update_ticket(
                    "PGU-2", {"manually_controlled": False}, caller_role="director"
                )
                assert act("PGU-2", "verify_accept", "verifier")["state"] == "audit"
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            assert "workflow_actions" in app.snapshot()["tickets"][0]
            # A five-stage tenant has a legal serial queue without any backlog stage.
            act("PGU-4", "release_draft", "director")
            compact = copy.deepcopy(second)
            names = {"analysis", "in_progress", "audit", "director_review", "done"}
            compact["stages"] = [
                stage for stage in compact["stages"] if stage["name"] in names
            ]
            compact["transitions"] = [
                tr
                for tr in compact["transitions"]
                if tr["from"] in names and tr["to"] in names
            ]
            for stage in compact["stages"]:
                if stage["name"] == "audit":
                    stage["skip_to"] = "director_review"
            submit = copy.deepcopy(
                next(
                    tr
                    for tr in second["transitions"]
                    if tr["action"] == "submit_to_audit"
                )
            )
            submit["to"] = "audit"
            compact["transitions"].append(submit)
            approve = copy.deepcopy(
                next(
                    tr
                    for tr in second["transitions"]
                    if tr["action"] == "audit_sign_off"
                )
            )
            approve["to"] = "director_review"
            compact["transitions"].append(approve)
            compact["remove_stages"] = [
                stage["name"]
                for stage in second["stages"]
                if stage["name"] not in names
            ]
            for ticket_id in ["PGU-2", "PGU-3", "PGU-4"]:
                app.set_workflow_flags(
                    ticket_id, {"needs_verification": False}, caller_role="director"
                )
            compact["reassign"] = {}
            compact["queue"] = {"stage": "analysis", "assignee": "director"}
            rev = app.workflow_document()["revision"]
            accidental = copy.deepcopy(compact)
            accidental["remove_stages"] = []
            rejected(
                lambda: app.apply_workflow(
                    validate(accidental),
                    expected_revision=rev,
                    dry_run=False,
                    caller_role="director",
                ),
                "omits an existing stage",
            )
            protected = copy.deepcopy(compact)
            protected["flags"]["commit_exempt"] = {"kind": "signoff", "default": False}
            rejected(lambda: validate(protected), "protected ticket field")
            app.apply_workflow(
                validate(compact),
                expected_revision=rev,
                dry_run=False,
                caller_role="director",
            )
            assert "backlog" not in [c["key"] for c in app.workflow_columns()]
            queued = act("PGU-4", "route", "director", assignee="app")
            assert (queued["state"], queued["assignee"]) == (
                "analysis",
                "director",
            ), queued
            t.seed_postgres_ticket(
                admin,
                "PGU-5",
                title="Second queued work",
                state="in_progress",
                assignee="app",
            )
            queued = app.get_ticket("PGU-5")
            assert (queued["state"], queued["assignee"]) == ("analysis", "director")
            # Invalid boolean/signoff edits cannot change either kind of flag.
            rejected(
                lambda: app.set_workflow_flags(
                    "PGU-2", {"verification_signoff": True}, caller_role="director"
                ),
                "only declared boolean gates",
            )
            rejected(
                lambda: app.set_workflow_flags(
                    "PGU-2", {"needs_verification": False}, caller_role="verifier"
                ),
                "only director",
            )
            assert (
                app.set_workflow_flags(
                    "PGU-2", {"needs_verification": False}, caller_role="director"
                )["workflow_flags"]["needs_verification"]
                is False
            )
            # Rename the implementation stage itself by data. Empty old stage is
            # deliberately retired; queue routing and last-owner tracking remain generic.
            renamed = copy.deepcopy(compact)
            renamed["remove_stages"].append("in_progress")
            for stage in renamed["stages"]:
                if stage["name"] == "in_progress":
                    stage["name"] = "building"
                    stage["label"] = "Building"
            for transition in renamed["transitions"]:
                for endpoint in ["from", "to"]:
                    if transition[endpoint] == "in_progress":
                        transition[endpoint] = "building"
            app.apply_workflow(
                validate(renamed),
                expected_revision=app.workflow_document()["revision"],
                dry_run=False,
                caller_role="director",
            )
            assert (
                act("PGU-4", "route", "director", assignee="main")["state"]
                == "building"
            )
            assert (
                t.psql(
                    admin,
                    "SELECT last_implementer_assignee FROM ticket_board.ticket_notification_state WHERE ticket_id='PGU-4'",
                )
                == "main"
            )
            assert (
                app.set_awaiting_role("PGU-4", "verifier", caller_role="main")[
                    "awaiting_role"
                ]
                == "verifier"
            )
            retiring = copy.deepcopy(renamed)
            next(role for role in retiring["roles"] if role["name"] == "verifier")[
                "active"
            ] = False
            rejected(
                lambda: app.apply_workflow(
                    retiring,
                    expected_revision=app.workflow_document()["revision"],
                    dry_run=False,
                    caller_role="director",
                ),
                "unresolved handoff",
            )
            app.clear_awaiting_role("PGU-4", caller_role="verifier")
            # Execute both fresh provisioning variants, then upgrade an existing
            # nine-stage syrd-shaped tenant with a retained draft and comments.
            from scripts.ticket_board.project_provision import (
                build_plan,
                render_workflow_sql,
            )

            for enabled in [True, False]:
                t.psql(admin, "DROP SCHEMA ticket_board CASCADE;\n" + schema)
                t.psql(admin, t.RBAC_PATH.read_text())
                fresh_cfg = copy.deepcopy(cfg)
                if not enabled:
                    fresh_cfg["remove_stages"] = ["inspection"]
                    fresh_cfg["stages"] = [
                        stage
                        for stage in fresh_cfg["stages"]
                        if stage["name"] != "inspection"
                    ]
                    fresh_cfg["transitions"] = [
                        tr
                        for tr in fresh_cfg["transitions"]
                        if tr["from"] != "inspection"
                    ]
                    for tr in fresh_cfg["transitions"]:
                        if tr["to"] == "inspection":
                            tr["to"] = "audit"
                    next(
                        role
                        for role in fresh_cfg["roles"]
                        if role["name"] == "inspector"
                    ).update(active=False, slot=None)
                plan = build_plan(
                    project="cerulean", owner_user="cerulean-worker", workflow=fresh_cfg
                )
                t.psql(admin, render_workflow_sql(plan))
                t.psql(admin, render_workflow_sql(plan))
                assert (
                    "inspection" in [c["key"] for c in app.workflow_columns()]
                ) == enabled
            t.psql(admin, "DROP SCHEMA ticket_board CASCADE;\n" + schema)
            t.psql(admin, t.RBAC_PATH.read_text())
            legacy_plan = build_plan(project="cerulean", owner_user="cerulean-worker")
            t.psql(admin, render_workflow_sql(legacy_plan))
            t.seed_postgres_ticket(
                admin, "PGU-91", title="Retained designer draft", state="draft"
            )
            t.psql(
                admin,
                "BEGIN; SET LOCAL ROLE ticket_board_service; SELECT set_config('ticket_board.caller_role','director',true); SELECT ticket_board.add_comment('PGU-91','Historical draft note'); COMMIT;",
            )
            existing = copy.deepcopy(cfg)
            existing["stages"] = [
                stage for stage in existing["stages"] if stage["name"] != "backlog"
            ]
            existing["transitions"] = [
                tr
                for tr in existing["transitions"]
                if tr["from"] != "backlog" and tr["to"] != "backlog"
            ]
            existing["queue"] = {"stage": "analysis", "assignee": "director"}
            app.apply_workflow(
                existing, expected_revision=0, dry_run=False, caller_role="director"
            )
            retained = app.get_ticket("PGU-91")
            assert retained["state"] == "draft" and retained["assignee"] == "director"
            assert any(
                comment["text"] == "Historical draft note"
                for comment in retained["comments"]
            )
            print(
                "declarative workflow: gates, actor checks, kickback owner, configuration CAS/dry-run/idempotence and second reviewer passed"
            )
        finally:
            subprocess.run(
                ["tmux", "-S", str(root / "tmux.sock"), "kill-server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            t.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
