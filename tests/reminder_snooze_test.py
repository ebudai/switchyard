#!/usr/bin/env python3
"""SYRD-537: a Director's reminder snooze for named tickets, until a deadline.

MEFP batches user-approved Final Sign-Off overnight. Until now the board kept
waking the Director and the reviewers about exactly those tickets all night --
idle reminders, nudges, escalations, unresolved-turn prompts -- and the only
ways to stop it (stage notify none, manually_controlled, fake blockers) silence
handoffs or bypass gates. Now the Director snoozes the named tickets' optional
reminders until a deadline, and at the deadline gets one queue-ready notice.

Every scenario runs on a board built the production way (companion roles,
schema.sql, the real ticket-board-migrate, rbac.sql, a declared workflow), with
the real reminder generators on a disposable clock, the real notify listener,
the real write CLI talking to the real board server over its Unix socket:

* from 792774a, main before this change -- code and SQL, in a child from a
  git archive -- the same night reminds about every overnight ticket, and the
  CLI has no snooze (the request, measured);
* from this tree: members get nothing optional all night from any producer,
  queued reminders are discarded without delivery accounting, gates still
  refuse, other tickets and every handoff are unchanged, a substantive change
  ends a member's snooze at once, an early clear resumes reminders, and the
  deadline notice is created once and delivered once under the listener's
  receipt semantics, across concurrent passes, a rolled-back pass, a busy pane
  and a listener restart.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "792774a3b1d2de6002d08ffcb53c8ca1d3b37565"  # main before SYRD-537
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"
MEMBERS = ("PGU-1", "PGU-2", "PGU-3", "PGU-5", "PGU-10")  # director_review x2, audit, a wait on Audit, analysis
REMINDED = ("PGU-1", "PGU-2", "PGU-3", "PGU-10")  # the members without a wait
CONTROLS = ("PGU-8", "PGU-9")  # the same shapes, not snoozed
ROLES = ("director", "audit", "app", "ops", "inspector")
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mentions(message: str, ticket: str) -> bool:
    """Whether a notice is about `ticket` -- PGU-1, never PGU-11."""
    return re.search(rf"\b{re.escape(ticket)}\b", message) is not None


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


NEW_FUNCTIONS = ("reminder_snooze_snapshot", "reminder_snooze_changes", "ticket_reminders_snoozed",
                 "reminder_snooze_utc", "reminder_snooze_member_status", "reminder_snooze_batch_json",
                 "ticket_reminder_snooze", "reminder_snoozes", "reminder_snooze_ticket_ids", "snooze_reminders",
                 "clear_reminder_snooze", "emit_due_reminder_snoozes")
SNAPSHOT_KEYS = ("state", "assignee", "commit_hash", "audit_signoff", "needs_audit", "needs_inspection",
                 "inspector_signoff", "needs_user_signoff", "user_signoff", "manually_controlled", "parked",
                 "workflow_flags", "awaiting_role", "awaiting_since_at", "blockers")
REDEFINED = ("ticket_turn_is_resolved", "notify_idle_turn_end_nudges", "notify_idle_stall_nudges", "notify_due_nudges")


def scenario(root: Path, prefix: str) -> dict:
    """Everything observable, from the tree at `root` (its code and its SQL)."""
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as t
    from scripts.ticket_board import write_cli
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    has_snooze = (root / "scripts/ticket_board/reminder_snooze.py").exists()
    seen: dict = {"has_snooze": has_snooze}

    def build(cluster, db: str, *, migrate: bool) -> str:
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
""")
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        if migrate:
            runner = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")],
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin},
                                    capture_output=True, text=True)
            assert runner.returncode == 0, runner.stderr
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        return admin

    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        db = "board"
        admin = build(cluster, db, migrate=True)
        listener_url = t.conninfo(cluster.socket_dir, cluster.port, db, "ticket_board_listener")
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="cerulean",
                               ticket_prefix="PGU",
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE))
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")

        # The overnight batch: user-approved, at the Director's Final Sign-Off,
        # and one awaiting Audit. The controls are the same shapes, not snoozed.
        for tid in ("PGU-1", "PGU-2", "PGU-5", "PGU-6", "PGU-7", "PGU-9"):
            t.seed_postgres_ticket(admin, tid, title=tid, state="director_review", assignee="director",
                                   commit_hash=COMMIT, audit_signoff=True, needs_user_signoff=True, user_signoff=True)
        for tid in ("PGU-3", "PGU-8"):
            t.seed_postgres_ticket(admin, tid, title=tid, state="audit", assignee="audit", commit_hash=COMMIT)
        # Work that arrives during the night, for other roles: implementer and Ops
        # routes, and an Inspector sign-off that hands a ticket to Audit.
        # A Director triage ticket in analysis: the other half of the nudge generators.
        t.seed_postgres_ticket(admin, "PGU-10", title="PGU-10", state="analysis", assignee="director")
        for tid in ("PGU-11", "PGU-12"):
            t.seed_postgres_ticket(admin, tid, title=tid, state="analysis", assignee="director",
                                   implementation="Ready to implement.")
        t.seed_postgres_ticket(admin, "PGU-13", title="PGU-13", state="inspection", assignee="inspector",
                               commit_hash=COMMIT, needs_inspection=True)
        # A member with an unapproved review, for the gate check.
        t.seed_postgres_ticket(admin, "PGU-4", title="PGU-4", state="user_review", assignee="user",
                               commit_hash=COMMIT, audit_signoff=True, needs_user_signoff=True, user_signoff=False)
        t.psql(admin, "DELETE FROM ticket_board.ticket_notification_queue;")
        # PGU-5 waits on Audit: a handoff, then the wait's own reminder schedule.
        app.set_awaiting_role("PGU-5", "audit", caller_role="director")

        delivered: list[tuple[str, str]] = []

        def make_listener(*, busy: bool = False, sleeper=None, target_exists=lambda _target: True, **extra):
            if sleeper is not None:
                extra.update(sleeper=sleeper, pre_send_recheck_delay_seconds=0.25)
            return TicketBoardNotifyListener(
                conninfo=listener_url, project="cerulean", **extra,
                sender=lambda target, message: delivered.append((target, message)),
                activity_gate=lambda _target: busy, target_exists=target_exists,
                submission_witness=lambda target, _since: any(sent == target for sent, _m in delivered),
            )

        def listen(*, busy: bool = False, emit: bool = False, sleeper=None) -> list[str]:
            before = len(delivered)
            listener = make_listener(busy=busy, sleeper=sleeper)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                if emit:
                    listener.process_reminder_snooze_due(conn)
                listener.process_due_notifications(conn)
            return [message for _target, message in delivered[before:]]

        def scalar(sql: str, url: str = admin) -> str:
            return t.psql(url, sql).strip()

        def queued_kinds(tickets) -> list[str]:
            ids = ",".join(f"'{tid}'" for tid in tickets)
            return json.loads(scalar(
                "SELECT coalesce(jsonb_agg(kind || ':' || target_role || ':' || ticket_id ORDER BY id), '[]')::text "
                f"FROM ticket_board.ticket_notification_queue WHERE ticket_id IN ({ids});"))

        sock = cluster.root / "board.sock"
        servers: dict = {}

        def cli(role: str, *args: str) -> tuple[int, str, str]:
            """The real `ticket-board-write`, as `role`, against the real server."""
            server = servers.get(role)
            if server is None:
                from scripts.ticket_board.server import TicketBoardUnixServer
                from scripts.ticket_board.board_notifications import TicketBoardEventHub
                for other in list(servers.values()):
                    other[0].shutdown()
                    other[0].server_close()
                    other[1].close()
                servers.clear()
                events = TicketBoardEventHub(app)
                srv = TicketBoardUnixServer(sock, app, events=events, director_notifier=t.QuietNotifier(),
                                            role_authority=t.local_role_authority_as(role))
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                servers[role] = server = (srv, events)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = write_cli.main(["--socket", str(sock), "--caller-role", role, *args])
                except SystemExit as exc:  # argparse's answer is an answer, not a crash
                    code = int(exc.code or 0)
            return code, out.getvalue(), err.getvalue()

        t0 = scalar("SELECT clock_timestamp()::text;")
        idle = json.dumps({role: t0 for role in ROLES})

        def night(hours: range) -> None:
            """Every reminder producer, once an hour, on the disposable clock."""
            for h in hours:
                now = f"'{t0}'::timestamptz + interval '{h} hours'"
                t.psql(listener_url, f"SELECT ticket_board.notify_idle_turn_end_nudges('{idle}'::jsonb, {now}, "
                                     "interval '10 minutes', '{}'::jsonb);")
                t.psql(listener_url, f"SELECT ticket_board.notify_idle_stall_nudges('{idle}'::jsonb, {now}, "
                                     "interval '10 minutes', interval '30 minutes', 2, '{}'::jsonb);")
                t.psql(admin, f"SELECT ticket_board.notify_due_nudges({now}, interval '30 minutes', 3);")
                t.psql(listener_url, "SELECT ticket_board.notify_unresolved_turn_end(jsonb_build_object("
                                     f"'director', 'dturn-{h}', 'audit', 'aturn-{h}'), {now}, interval '10 minutes');")
                if h == 5:
                    # Audit's pane stops on a permission prompt. The escalation hangs on
                    # Audit's first ticket -- a snoozed member -- and is about the pane.
                    t.psql(listener_url, "SELECT ticket_board.notify_permission_prompt_waits("
                                         f"jsonb_build_object('audit', '{t0}'), {now}, interval '2 minutes');")

        # Reminders already queued for the members before anyone snoozes them --
        # an hour in, so past the idle grace and an idle reminder among them.
        night(range(1, 2))
        seen["queued_before_snooze"] = queued_kinds(MEMBERS)
        seen["reminded_before_snooze"] = scalar("SELECT string_agg(ticket_id || '=' || idle_reminder_count, ',' "
                                                "ORDER BY ticket_id) FROM ticket_board.ticket_notification_state "
                                                "WHERE ticket_id IN ('PGU-1','PGU-2','PGU-3');")

        # Past the last simulated hour of the night (11), so no hour reaches it by accident.
        until = scalar("SELECT to_char((clock_timestamp() + interval '12 hours') AT TIME ZONE 'UTC', "
                       "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');")
        snooze_args = ["snooze-reminders", "--ticket", "PGU-1", "--ticket", "pgu-2", "--ticket", "PGU-3",
                       "--ticket", "PGU-5", "--ticket", "PGU-10",
                       "--until", until, "--reason", "overnight Final Sign-Off integration batch"]
        rows_before_preview = scalar("SELECT count(*) FROM ticket_board.ticket_notification_queue;")
        seen["preview"] = cli("director", *snooze_args)
        if has_snooze:
            seen["preview_writes"] = scalar("SELECT count(*) FROM ticket_board.reminder_snooze_batches;")
            seen["preview_queue_unchanged"] = rows_before_preview == scalar(
                "SELECT count(*) FROM ticket_board.ticket_notification_queue;")
            # Refused before anything is written: another role, Ops, a local time, the past.
            seen["refused_app"] = cli("app", *snooze_args, "--apply")
            seen["refused_ops"] = cli("ops", *snooze_args, "--apply")
            seen["refused_local_time"] = cli("director", *[a if a != until else until[:-1] for a in snooze_args], "--apply")
            seen["refused_past"] = cli("director", *[a if a != until else "2020-01-01T00:00:00Z" for a in snooze_args], "--apply")
            seen["refused_sql_app"] = ""
            try:
                with app._pg_connect() as conn:
                    app._pg_set_caller_role(conn, "app")
                    conn.execute("SELECT ticket_board.snooze_reminders(ARRAY['PGU-1'], clock_timestamp() + "
                                 "interval '1 hour', 'x', true)")
            except Exception as exc:  # noqa: BLE001 -- the database's refusal is the answer
                seen["refused_sql_app"] = str(exc).splitlines()[0]
            seen["writes_after_refusals"] = scalar("SELECT count(*) FROM ticket_board.reminder_snooze_batches;")
        seen["apply"] = cli("director", *snooze_args, "--apply")
        applied_at = scalar("SELECT clock_timestamp()::text;")
        if has_snooze:
            seen["refused_again"] = cli("director", "snooze-reminders", "--ticket", "PGU-1", "--until", until,
                                        "--reason", "again", "--apply")
        seen["snooze_ticket_json"] = app.get_ticket("PGU-1").get("reminder_snooze")
        seen["control_ticket_json"] = app.get_ticket("PGU-9").get("reminder_snooze", "absent")

        # What was queued before the snooze is not delivered, and not counted as sent.
        # The wait's second step is made due now, on the disposable clock; its
        # third and fourth stay scheduled, still queued when the deadline comes.
        t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                      "WHERE dedupe_key LIKE 'awaiting_role:PGU-5:%:2';")
        first = listen()
        seen["delivered_members_first_pass"] = [m for m in first if any(mentions(m, tid) for tid in REMINDED)]
        seen["wait_steps_delivered"] = [m for m in first if mentions(m, "PGU-5")]
        seen["wait_steps_discarded"] = scalar(
            "SELECT coalesce(string_agg(q.detail -> 'payload' ->> 'step', ',' ORDER BY q.detail -> 'payload' ->> 'step'), '') "
            "FROM ticket_board.notification_trace d JOIN ticket_board.notification_trace q "
            "ON q.notification_id = d.notification_id AND q.event = 'enqueue' "
            "WHERE d.event = 'listener_discard' AND d.ticket_id = 'PGU-5';")
        seen["delivered_controls_first_pass"] = sorted({tid for m in first for tid in CONTROLS if mentions(m, tid)})
        seen["queued_members_after_first_pass"] = queued_kinds(REMINDED)
        seen["reminded_after_snooze"] = scalar("SELECT string_agg(ticket_id || '=' || idle_reminder_count, ',' "
                                               "ORDER BY ticket_id) FROM ticket_board.ticket_notification_state "
                                               "WHERE ticket_id IN ('PGU-1','PGU-2','PGU-3');")
        seen["acked_member_reminders"] = scalar(
            "SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'listener_ack' AND kind IN "
            "('idle_reminder', 'nudge', 'escalation', 'unresolved_turn_repair', 'unresolved_turn') "
            "AND ticket_id IN ('PGU-1', 'PGU-2', 'PGU-3', 'PGU-5');")
        seen["discard_traces"] = scalar("SELECT count(*) FROM ticket_board.notification_trace WHERE event = "
                                        "'listener_discard' AND detail ->> 'reason' = 'reminder_snoozed';") \
            if has_snooze else "0"

        # The night: every producer, every hour, delivered as it goes.
        night_delivered: list[str] = []
        for hour in range(1, 9):
            night(range(hour, hour + 1))
            night_delivered += listen()
        seen["permission_prompt"] = [m for m in night_delivered if "stopped on a permission prompt" in m]
        seen["night_member_enqueues"] = scalar(
            "SELECT coalesce(string_agg(kind || ':' || ticket_id, ',' ORDER BY id), '') FROM ticket_board.notification_trace "
            f"WHERE event = 'enqueue' AND ts > '{applied_at}' AND ticket_id IN ('PGU-1','PGU-2','PGU-3','PGU-5','PGU-10') "
            "AND kind IN ('idle_reminder', 'nudge', 'escalation', 'unresolved_turn_repair', 'unresolved_turn', 'awaiting_role') "
            "AND detail -> 'payload' ->> 'reason' IS DISTINCT FROM 'permission_prompt';")
        seen["night_control_enqueues"] = scalar(
            "SELECT count(*) FROM ticket_board.notification_trace "
            f"WHERE event = 'enqueue' AND ts > '{applied_at}' AND ticket_id IN ('PGU-8','PGU-9');")
        seen["night_member_messages"] = [m for m in night_delivered if any(mentions(m, tid) for tid in MEMBERS)]
        seen["night_member_rows"] = [k for k in queued_kinds(MEMBERS) if not k.startswith("awaiting_role:")]
        seen["night_control_tickets"] = sorted({tid for m in night_delivered for tid in CONTROLS if mentions(m, tid)})
        seen["night_control_count"] = sum(1 for m in night_delivered if any(mentions(m, tid) for tid in CONTROLS))
        reads = scalar("SELECT md5(coalesce(string_agg(to_jsonb(m)::text, '|' ORDER BY batch_id, ticket_id), '')) "
                       "FROM ticket_board.reminder_snooze_members m;") if has_snooze else ""
        for _ in range(3):
            app.get_ticket("PGU-1")
            app.snapshot()
        if has_snooze:
            from scripts.ticket_board import reminder_snooze
            reminder_snooze.batches(app)
            seen["reads_change_nothing"] = reads == scalar(
                "SELECT md5(coalesce(string_agg(to_jsonb(m)::text, '|' ORDER BY batch_id, ticket_id), '')) "
                "FROM ticket_board.reminder_snooze_members m;")

        # Handoffs during the night: implementer, Ops and Audit, exactly as ever.
        app.perform_workflow_action("PGU-11", "route", {"target": "in_progress", "assignee": "app"},
                                    caller_role="director")
        app.perform_workflow_action("PGU-12", "route", {"target": "in_progress", "assignee": "ops"},
                                    caller_role="director")
        app.perform_workflow_action("PGU-13", "inspector_sign_off", {}, caller_role="inspector")
        handoffs = listen()
        seen["handoffs"] = sorted(m.split("\n")[0][:120] for m in handoffs
                                  if any(mentions(m, tid) for tid in ("PGU-11", "PGU-12", "PGU-13")))

        # Gates: the snooze grants nothing. The unapproved member cannot be completed.
        gate_refusals = []
        for action, role in (("mark_done", "director"), ("user_sign_off", "director")):
            try:
                app.perform_workflow_action("PGU-4", action, {}, caller_role=role)
                gate_refusals.append(f"{action}: ALLOWED")
            except Exception as exc:  # noqa: BLE001
                gate_refusals.append(f"{action}: {str(exc).splitlines()[0][:80]}")
        seen["gate_refusals"] = gate_refusals
        seen["pgu4_flags"] = scalar("SELECT state || ',' || user_signoff || ',' || needs_user_signoff "
                                    "FROM ticket_board.tickets WHERE id = 'PGU-4';")
        if not has_snooze:
            return seen

        # A substantive change ends that member's snooze at once: PGU-2 goes back
        # to App. Its handoff is delivered, and its own reminders resume.
        app.perform_workflow_action("PGU-2", "director_kick_back", {"reason": "integration conflict"},
                                    caller_role="director")
        seen["changed_snoozed"] = scalar("SELECT ticket_board.ticket_reminders_snoozed('PGU-2', clock_timestamp());",
                                         listener_url)
        seen["changed_ticket_json"] = app.get_ticket("PGU-2").get("reminder_snooze")
        seen["changed_handoff"] = [m for m in listen() if mentions(m, "PGU-2")]
        night(range(9, 10))
        seen["changed_resumes"] = [k for k in queued_kinds(["PGU-2"]) if not k.startswith("transition")]

        # Early clear: PGU-3's reminders resume at once; the others stay snoozed.
        seen["clear_preview"] = cli("director", "clear-reminder-snooze", "--batch", "1", "--ticket", "PGU-3",
                                    "--reason", "Audit is awake")
        seen["clear_apply"] = cli("director", "clear-reminder-snooze", "--batch", "1", "--ticket", "PGU-3",
                                  "--reason", "Audit is awake", "--apply")
        night(range(10, 11))
        seen["cleared_resumes"] = queued_kinds(["PGU-3"])
        seen["still_snoozed"] = queued_kinds(["PGU-1"])

        # The deadline. Nothing optional about a ready member -- generated after
        # it, or queued long before -- reaches anyone ahead of the batch's one
        # notice; ordinary reminders follow it.
        t.psql(admin, "UPDATE ticket_board.reminder_snooze_batches SET due_at = clock_timestamp() - interval '1 second';")
        deadline_at = scalar("SELECT clock_timestamp()::text;")
        night(range(11, 12))  # producers after the deadline, before any notice is written
        # Two passes at once: one row, from whichever holds the lock. The other
        # must not wait on it (a bounded wait, so a pass that would is an answer).
        with psycopg.connect(listener_url) as held, psycopg.connect(listener_url, autocommit=True) as other:
            other.execute("SET lock_timeout = '3s'")

            def other_pass():
                try:
                    return other.execute("SELECT ticket_board.emit_due_reminder_snoozes(clock_timestamp())").fetchone()[0]
                except Exception as exc:  # noqa: BLE001
                    return f"error: {str(exc).splitlines()[0]}"
            first_pass = held.execute("SELECT ticket_board.emit_due_reminder_snoozes(clock_timestamp())").fetchone()[0]
            concurrent = other_pass()
            held.commit()
            after_commit = other_pass()
        seen["emits"] = [first_pass, concurrent, after_commit]
        night(range(12, 13))  # producers after the notice is written, before it is delivered
        seen["pending_json"] = app.get_ticket("PGU-1").get("reminder_snooze")
        seen["due_rows"] = scalar("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                  "WHERE dedupe_key LIKE 'reminder-snooze-due:%';")
        seen["due_message"] = scalar("SELECT message FROM ticket_board.ticket_notification_queue "
                                     "WHERE dedupe_key = 'reminder-snooze-due:1';")
        seen["batch_after_due"] = json.loads(scalar("SELECT ticket_board.reminder_snoozes(clock_timestamp())::text;"))
        # The wait's later steps, queued before the snooze and older than the
        # notice, come due now.
        t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                      "WHERE dedupe_key LIKE 'awaiting_role:PGU-5:%';")
        since_deadline = len(delivered)
        # Busy Director: the notice is requeued, nothing sent; producers run again
        # meanwhile. A restarted listener then delivers it once.
        seen["busy_pass"] = [m for m in listen(busy=True, emit=True) if "Reminder snooze batch" in m]
        seen["busy_error"] = scalar("SELECT coalesce(last_error, '') FROM ticket_board.ticket_notification_queue "
                                    "WHERE dedupe_key = 'reminder-snooze-due:1';")
        night(range(13, 14))
        seen["held_enqueues"] = scalar(
            "SELECT coalesce(string_agg(kind || ':' || ticket_id, ',' ORDER BY id), '') FROM ticket_board.notification_trace "
            f"WHERE event = 'enqueue' AND ts > '{deadline_at}' AND ticket_id IN ('PGU-1','PGU-5','PGU-10') "
            "AND kind IN ('idle_reminder', 'nudge', 'escalation', 'unresolved_turn_repair', 'unresolved_turn', 'awaiting_role') "
            "AND detail -> 'payload' ->> 'reason' IS DISTINCT FROM 'permission_prompt';")
        t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp();")
        seen["restart_pass"] = [m for m in listen(emit=True) if "Reminder snooze batch" in m]
        seen["later_passes"] = [m for m in listen(emit=True) + listen(emit=True) if "Reminder snooze batch" in m]
        seen["sends"] = scalar("SELECT count(*) FROM ticket_board.notification_trace tr "
                               "JOIN ticket_board.reminder_snooze_batches b ON b.notification_id = tr.notification_id "
                               "WHERE b.id = 1 AND tr.event = 'send';")
        seen["held_discards"] = scalar(
            "SELECT coalesce(string_agg(q.detail -> 'payload' ->> 'step', ',' ORDER BY q.detail -> 'payload' ->> 'step'), '') "
            "FROM ticket_board.notification_trace d JOIN ticket_board.notification_trace q "
            "ON q.notification_id = d.notification_id AND q.event = 'enqueue' "
            f"WHERE d.event = 'listener_discard' AND d.ticket_id = 'PGU-5' AND d.ts > '{deadline_at}';")
        seen["after_due"] = scalar("SELECT ticket_board.ticket_reminders_snoozed('PGU-1', clock_timestamp());",
                                   listener_url)
        # Then ordinary policy: the next producer pass reminds, and it is delivered.
        night(range(14, 15))
        seen["after_due_resumes"] = queued_kinds(["PGU-1"])
        listen()
        seen["deadline_sequence"] = [
            "notice" if "Reminder snooze batch 1" in m else "member"
            for _target, m in delivered[since_deadline:]
            if "Reminder snooze batch 1" in m or any(mentions(m, tid) for tid in ("PGU-1", "PGU-5", "PGU-10"))
        ]

        # A pass that dies before it commits creates nothing; the next one creates it.
        cli("director", "snooze-reminders", "--ticket", "PGU-8", "--until", until, "--reason", "second batch",
            "--apply")
        t.psql(admin, "UPDATE ticket_board.reminder_snooze_batches SET due_at = clock_timestamp() - interval "
                      "'1 second' WHERE state = 'snoozed';")
        with psycopg.connect(listener_url) as crashing:
            crashing.execute("SELECT ticket_board.emit_due_reminder_snoozes(clock_timestamp())")
            crashing.rollback()
        seen["after_rollback"] = scalar("SELECT state FROM ticket_board.reminder_snooze_batches WHERE id = 2;")
        # The listener's own loop writes it and delivers it: one pass, one notice.
        t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() + "
                      "interval '1 day' WHERE dead_lettered_at IS NULL;")
        before_loop = len(delivered)
        make_listener(poll_seconds=0).listen_once(max_notifications=1)
        # Its notice hangs on an Audit-owned ticket and is addressed to the Director.
        seen["second_digest"] = delivered[before_loop:]
        # Created once: the trace keeps every enqueue after delivery removes the row.
        seen["second_due_rows"] = scalar("SELECT count(*) FROM ticket_board.notification_trace WHERE event = "
                                         "'enqueue' AND detail ->> 'dedupe_key' = 'reminder-snooze-due:2';")

        # A notice that cannot be delivered does not silence its members for good:
        # dead-lettered, it releases them.
        cli("director", "snooze-reminders", "--ticket", "PGU-9", "--until", until, "--reason", "fourth batch", "--apply")
        t.psql(admin, "UPDATE ticket_board.reminder_snooze_batches SET due_at = clock_timestamp() - interval "
                      "'1 second' WHERE state = 'snoozed';")
        t.psql(listener_url, "SELECT ticket_board.emit_due_reminder_snoozes(clock_timestamp());")
        seen["dead_letter_held"] = scalar("SELECT ticket_board.ticket_reminders_snoozed('PGU-9', clock_timestamp());",
                                          listener_url)
        listener = make_listener(target_exists=lambda _target: False)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            listener.process_due_notifications(conn)
        seen["dead_lettered"] = scalar("SELECT count(*) FROM ticket_board.ticket_notification_queue q JOIN "
                                       "ticket_board.reminder_snooze_batches b ON b.notification_id = q.id "
                                       "WHERE b.reason = 'fourth batch' AND q.dead_lettered_at IS NOT NULL;")
        seen["dead_letter_released"] = scalar(
            "SELECT ticket_board.ticket_reminders_snoozed('PGU-9', clock_timestamp());", listener_url)

        # A new blocker ends a member's snooze too, through the real CLI.
        cli("director", "snooze-reminders", "--ticket", "PGU-6", "--until", until, "--reason", "third batch", "--apply")
        seen["blocker_before"] = scalar("SELECT ticket_board.ticket_reminders_snoozed('PGU-6', clock_timestamp());",
                                        listener_url)
        seen["set_blockers"] = cli("director", "set-blockers", "PGU-6", "--blocked-by", "PGU-11",
                                   "--blocked-reason", "integration needs PGU-11")[0]
        seen["blocker_after"] = scalar("SELECT ticket_board.ticket_reminders_snoozed('PGU-6', clock_timestamp());",
                                       listener_url)
        seen["blocker_json"] = app.get_ticket("PGU-6").get("reminder_snooze")
        seen["snapshot_keys"] = scalar("SELECT string_agg(k, ',' ORDER BY k) FROM ticket_board.reminder_snooze_members m, "
                                       "jsonb_object_keys(m.snapshot) k WHERE m.ticket_id = 'PGU-6';")

        # A snooze set between claim and send: the pre-send recheck discards it.
        night(range(15, 16))
        t.psql(admin, "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() + "
                      "interval '1 day' WHERE ticket_id <> 'PGU-7' AND dead_lettered_at IS NULL;")
        seen["window_queued"] = queued_kinds(["PGU-7"])
        snoozed_mid_send: list[int] = []

        def snooze_mid_send(_seconds: float) -> None:
            if not snoozed_mid_send:
                snoozed_mid_send.append(cli("director", "snooze-reminders", "--ticket", "PGU-7", "--until", until,
                                            "--reason", "set while a reminder was in flight", "--apply")[0])
        seen["window_delivered"] = [m for m in listen(sleeper=snooze_mid_send) if mentions(m, "PGU-7")]
        seen["window_snoozed"] = snoozed_mid_send
        seen["window_discards"] = scalar("SELECT coalesce(string_agg(DISTINCT detail ->> 'phase', ','), '') "
                                         "FROM ticket_board.notification_trace WHERE event = 'listener_discard' "
                                         "AND ticket_id = 'PGU-7' AND detail ->> 'reason' = 'reminder_snoozed';")

        # Schema: a fresh schema.sql board installs what the migrated board has.
        fresh = build(cluster, "fresh", migrate=False)
        names = ",".join(f"'{n}'" for n in NEW_FUNCTIONS + REDEFINED)
        definitions = ("SELECT string_agg(p.proname || md5(pg_get_functiondef(p.oid)), ',' ORDER BY p.proname) "
                       f"FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ({names});")
        tables = ("SELECT string_agg(table_name || ':' || column_name || ':' || data_type || ':' || "
                  "coalesce(column_default, ''), ',' ORDER BY table_name, ordinal_position) "
                  "FROM information_schema.columns WHERE table_schema = 'ticket_board' "
                  "AND table_name LIKE 'reminder_snooze%';")
        seen["fresh_equals_migrated"] = [scalar(definitions, fresh) == scalar(definitions),
                                         scalar(tables, fresh) == scalar(tables),
                                         len(scalar(definitions).split(","))]
        grantees = ("SELECT string_agg(p.proname || '=' || (SELECT string_agg(DISTINCT coalesce(r.rolname, 'PUBLIC'), "
                    "'+' ORDER BY coalesce(r.rolname, 'PUBLIC')) FROM aclexplode(coalesce(p.proacl, "
                    "acldefault('f', p.proowner))) a LEFT JOIN pg_roles r ON r.oid = a.grantee WHERE "
                    "a.privilege_type = 'EXECUTE'), ',' ORDER BY p.proname) FROM pg_proc p "
                    f"WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ({names});")
        seen["grantees"] = scalar(grantees)
        seen["grantees_fresh"] = scalar(grantees, fresh)
        for srv, events in servers.values():
            srv.shutdown()
            srv.server_close()
            events.close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd537b-")))
        return 0

    with tempfile.TemporaryDirectory(prefix="syrd537.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(before["preview"][0] != 0 and "invalid choice: 'snooze-reminders'" in before["preview"][2],
          f"before: the board has no reminder snooze: {before['preview']}")
    check(len(before["night_member_messages"]) >= 9
          and {tid for m in before["night_member_messages"] for tid in MEMBERS if mentions(m, tid)} == set(REMINDED),
          f"before: the overnight tickets were reminded about all night: {len(before['night_member_messages'])} "
          f"{before['night_member_messages'][:3]}")

    after = scenario(ROOT, "syrd537a-")
    # Preview, refusals, apply.
    code, out, err = after["preview"]
    preview = json.loads(out) if code == 0 else {}
    check(code == 0 and preview["applied"] is False and [m["ticket"] for m in preview["members"]] == list(MEMBERS)
          and preview["refusals"] == [] and preview["until"].endswith("Z"),
          f"the preview names exactly the members and the deadline in UTC: {after['preview']}")
    check(after["preview_writes"] == "0" and after["preview_queue_unchanged"],
          f"and writes nothing: {after['preview_writes']}")
    check(after["refused_app"][0] != 0 and "app cannot call snooze_reminders" in after["refused_app"][2]
          and after["refused_ops"][0] != 0 and "ops cannot call snooze_reminders" in after["refused_ops"][2],
          f"App and Ops are refused at the board: {after['refused_app']} {after['refused_ops']}")
    check(after["refused_sql_app"].startswith("role app cannot call set_manually_controlled"),
          f"and by the database itself: {after['refused_sql_app']}")
    check(after["refused_local_time"][0] != 0 and "explicit offset" in after["refused_local_time"][2]
          and after["refused_past"][0] != 0 and "is not in the future" in after["refused_past"][2]
          and after["writes_after_refusals"] == "0",
          f"a deadline without an offset, or in the past, is refused and writes nothing: "
          f"{after['refused_local_time'][2][:200]} {after['refused_past'][2][:200]}")
    code, out, _err = after["apply"]
    applied = json.loads(out) if code == 0 else {}
    check(code == 0 and applied["applied"] is True and applied["batch"] == 1 and applied["by"] == "director",
          f"the Director snoozes them, attributed: {after['apply']}")
    check(after["refused_again"][0] != 0 and "already snoozed in batch 1 until" in after["refused_again"][2],
          f"a ticket already snoozed is refused, not silently moved to a new batch: {after['refused_again']}")
    snooze = after["snooze_ticket_json"]
    check(snooze and snooze["status"] == "snoozed" and snooze["batch"] == 1 and snooze["by"] == "director"
          and snooze["reason"] == "overnight Final Sign-Off integration batch" and after["control_ticket_json"] is None,
          f"the ticket says so, and an unsnoozed one says nothing: {snooze} {after['control_ticket_json']}")

    # Queued before the snooze: discarded, not delivered, not counted.
    check(len(after["queued_before_snooze"]) >= 3, f"the probe arrives: reminders were queued: {after['queued_before_snooze']}")
    check(len(before["wait_steps_delivered"]) == 2 and before["wait_steps_delivered"][0].count("New handoff.") == 1,
          f"before: a wait delivers its handoff and then its reminders: {before['wait_steps_delivered']}")
    check(after["wait_steps_delivered"] == before["wait_steps_delivered"][:1]
          and after["wait_steps_discarded"] == "2",
          f"snoozed, the handoff is delivered and the schedule's later steps are discarded: "
          f"{after['wait_steps_delivered']} discarded steps {after['wait_steps_discarded']}")
    check(after["delivered_members_first_pass"] == [] and after["queued_members_after_first_pass"] == []
          and after["delivered_controls_first_pass"] == list(CONTROLS),
          f"queued member reminders are not delivered, the controls' are: {after['delivered_members_first_pass']} "
          f"{after['delivered_controls_first_pass']}")
    check(int(before["acked_member_reminders"]) >= 3 and "=1" in before["reminded_after_snooze"],
          f"before: the same reminders were delivered, acked and counted: {before['acked_member_reminders']} "
          f"{before['reminded_after_snooze']}")
    check("idle_reminder" in " ".join(after["queued_before_snooze"])
          and after["reminded_after_snooze"] == after["reminded_before_snooze"]
          and after["acked_member_reminders"] == "0" and int(after["discard_traces"]) >= 3,
          f"discarded, not acked: no reminder counted as sent: {after['reminded_before_snooze']} -> "
          f"{after['reminded_after_snooze']}, acked {after['acked_member_reminders']}, discards {after['discard_traces']}")
    # The night.
    check(after["night_member_enqueues"] == "" and int(after["night_control_enqueues"]) >= 6,
          f"overnight no producer even generates an optional reminder about a member, while it does for the "
          f"controls: {after['night_member_enqueues'][:300]} / {after['night_control_enqueues']}")
    check(after["night_member_messages"] == [] and after["night_member_rows"] == [],
          f"overnight the members get nothing optional from any producer: {after['night_member_messages'][:3]} "
          f"{after['night_member_rows']}")
    check(len(before["permission_prompt"]) == 1 and len(after["permission_prompt"]) == 1
          and after["permission_prompt"][0].split(" since ")[0] == before["permission_prompt"][0].split(" since ")[0]
          == "audit is stopped on a permission prompt in its pane and cannot answer it itself. It has been waiting",
          f"a permission-prompt escalation is about the pane, and is delivered as on main: {after['permission_prompt']}")
    check(after["night_control_tickets"] == list(CONTROLS) and after["night_control_count"] >= 6,
          f"while the same producers keep reminding about the controls: {after['night_control_count']}")
    check(after["reads_change_nothing"], "status reads leave scheduling untouched")
    # Handoffs and gates.
    check(after["handoffs"] == before["handoffs"] and len(after["handoffs"]) == 3,
          f"implementer, Ops and Audit handoffs are exactly main's: {after['handoffs']} vs {before['handoffs']}")
    check(after["gate_refusals"] == before["gate_refusals"] and all("ALLOWED" not in g for g in after["gate_refusals"])
          and after["pgu4_flags"] == "user_review,false,true",
          f"the gates still refuse the unapproved member, as on main: {after['gate_refusals']}")
    # A substantive change.
    check(after["changed_snoozed"] == "f" and after["changed_ticket_json"]["status"] == "invalidated"
          and {"state", "assignee"} <= set(after["changed_ticket_json"]["changed"]),
          f"a member that changes is no longer snoozed, and says why: {after['changed_ticket_json']}")
    check(len(after["changed_handoff"]) == 1 and "PGU-2 entered Implementation" in after["changed_handoff"][0],
          f"its handoff is delivered: {after['changed_handoff']}")
    check(after["changed_resumes"], f"and its reminders resume: {after['changed_resumes']}")
    # Early clear.
    code, out, _err = after["clear_preview"]
    check(code == 0 and json.loads(out)["releases"] == ["PGU-3"] and json.loads(out)["applied"] is False,
          f"a clear previews its exact scope: {after['clear_preview']}")
    check(after["clear_apply"][0] == 0 and after["cleared_resumes"] and after["still_snoozed"] == [],
          f"an early clear resumes that ticket's reminders only: {after['cleared_resumes']} {after['still_snoozed']}")
    # The deadline.
    check(after["held_enqueues"] == "",
          f"from the deadline until its notice is delivered -- before it is written, while it is queued, through a "
          f"busy-pane retry -- no producer enqueues an optional reminder about a ready member: {after['held_enqueues']}")
    check(after["pending_json"] and after["pending_json"]["status"] == "due",
          f"meanwhile the ticket says its snooze is due, notice pending: {after['pending_json']}")
    check(after["held_discards"] == "3,4",
          f"the wait's later steps, queued before the snooze and older than the notice, are discarded while it is "
          f"pending: {after['held_discards']}")
    check(after["deadline_sequence"][:1] == ["notice"] and "member" in after["deadline_sequence"][1:],
          f"the first optional word about the ready members is the batch's notice, and ordinary reminders follow it: "
          f"{after['deadline_sequence']}")
    check(after["emits"] == [1, 0, 0] and after["due_rows"] == "1",
          f"concurrent passes create the notice once: {after['emits']} rows {after['due_rows']}")
    message = after["due_message"]
    check("Ready: PGU-1 (director_review, director)" in message and "PGU-2 (" in message
          and "Changed while snoozed: PGU-2" in message and not mentions(message, "PGU-3"),
          f"it says which are ready and which changed; the cleared one is gone: {message}")
    batch = after["batch_after_due"][0]
    check(batch["state"] == "due_emitted" and [(m["ticket"], m["release_reason"]) for m in batch["members"]] == [
              ("PGU-1", "due"),
              # The kick-back is a return: it also cleared the candidate and the sign-offs.
              ("PGU-2", "due; assignee, audit_signoff, commit_hash, state, user_signoff changed"),
              ("PGU-3", "cleared: Audit is awake"), ("PGU-5", "due"), ("PGU-10", "due")],
          f"the batch closes, every release attributed: {batch}")
    check(after["busy_pass"] == [] and after["busy_error"] == "pane busy",
          f"a busy pane postpones it: {after['busy_error']}")
    check(len(after["restart_pass"]) == 1 and after["later_passes"] == [] and after["sends"] == "1",
          f"a restarted listener delivers it once, and no later pass again: {after['restart_pass']} "
          f"{after['later_passes']} sends {after['sends']}")
    check(after["after_due"] == "f" and after["after_due_resumes"],
          f"after the deadline ordinary reminders resume: {after['after_due_resumes']}")
    check(after["dead_letter_held"] == "t" and after["dead_lettered"] == "1" and after["dead_letter_released"] == "f",
          "a notice that is dead-lettered releases its members, rather than silencing them for good")
    check(after["after_rollback"] == "snoozed" and after["second_due_rows"] == "1",
          "a pass that rolls back creates nothing, and the next creates it once")
    check(len(after["second_digest"]) == 1 and "director" in after["second_digest"][0][0]
          and "Reminder snooze batch 2 is due" in after["second_digest"][0][1]
          and "Ready: PGU-8 (audit, audit)" in after["second_digest"][0][1],
          f"the listener's own loop writes and delivers it; hung on another role's ticket, it still reaches the "
          f"role that set it: {after['second_digest']}")
    check(after["blocker_before"] == "t" and after["set_blockers"] == 0 and after["blocker_after"] == "f"
          and after["blocker_json"]["status"] == "invalidated" and after["blocker_json"]["changed"] == ["blockers"],
          f"a new blocker ends that member's snooze: {after['blocker_json']}")
    check(after["snapshot_keys"] == ",".join(sorted(SNAPSHOT_KEYS)),
          f"the snapshot covers stage, owner, candidate, every sign-off and gate, holds, waits and blockers: "
          f"{after['snapshot_keys']}")
    check(after["window_queued"] and after["window_snoozed"] == [0] and after["window_delivered"] == []
          # the one in flight at the recheck, and any claimed after the snooze at claim
          and "pre_send_recheck" in after["window_discards"].split(","),
          f"a snooze set between claim and send stops the send: {after['window_queued']} {after['window_delivered']} "
          f"{after['window_discards']}")
    # Schema and grants.
    same_functions, same_tables, count = after["fresh_equals_migrated"]
    check(same_functions and same_tables and count == len(NEW_FUNCTIONS) + len(REDEFINED),
          f"a fresh schema.sql board installs the same functions and tables: {after['fresh_equals_migrated']}")
    grants = dict(item.split("=", 1) for item in after["grantees"].split(","))
    expected = {name: "postgres" for name in NEW_FUNCTIONS}
    expected.update({"snooze_reminders": "postgres+ticket_board_service", "clear_reminder_snooze": "postgres+ticket_board_service",
                     "ticket_reminder_snooze": "postgres+ticket_board_service", "reminder_snoozes": "postgres+ticket_board_service",
                     "ticket_reminders_snoozed": "postgres+ticket_board_listener",
                     "emit_due_reminder_snoozes": "postgres+ticket_board_listener"})
    check({k: grants[k] for k in NEW_FUNCTIONS} == expected and after["grantees_fresh"] == after["grantees"],
          f"each new function is executable only by its caller, on both boards: {grants}")
    # The ticket view says it, from the ticket JSON the board serves; the card does not.
    sys.path.insert(0, str(ROOT / "tests"))
    from active_work_delivery_frontend_test import function_source

    program = function_source("reminderSnoozeText") + """
process.stdout.write(JSON.stringify(%s.map((snooze) => reminderSnoozeText(snooze))));
""" % json.dumps([after["snooze_ticket_json"], after["changed_ticket_json"], None])
    proc = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    snoozed_line, invalidated_line, nothing = json.loads(proc.stdout)
    check(snoozed_line == f"Reminders: snoozed until {after['snooze_ticket_json']['until']} (batch 1, by director: "
                         "overnight Final Sign-Off integration batch)"
          and invalidated_line.startswith("Reminders: snooze no longer applies, assignee, ") and nothing == "",
          f"the ticket view says when, who and why, or that it no longer applies: {snoozed_line!r} {invalidated_line!r}")
    check("const snoozeText = reminderSnoozeText(ticket.reminder_snooze);" in function_source("renderDetail")
          and "snoozeLine.textContent = snoozeText;" in function_source("renderDetail")
          and "meta.appendChild(snoozeLine);" in function_source("renderDetail")
          and "reminder_snooze" not in function_source("renderCard"),
          "the ticket view shows it, and the card does not (SYRD-266)")
    print(f"reminder_snooze_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
