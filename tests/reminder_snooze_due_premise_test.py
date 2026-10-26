#!/usr/bin/env python3
"""SYRD-679: a reminder snooze's deadline notice is about the tickets still snoozed, when written and when sent.

Batch 50 snoozed SYRD-677, and batch 51 SYRD-684. Each ticket reached Done
before its batch's deadline, and its snooze read `invalidated`. At the deadline
the Director was still sent "Reminder snooze batch ... is due. Ready: none.
Changed while snoozed: ...".

On a board built the production way (schema.sql, the real ticket-board-migrate,
rbac.sql, a declared workflow), with the real notify listener writing and
delivering the notice, each batch snoozes its own tickets:

* `done-before`: the ticket is completed before the deadline (SYRD-677, 684);
* `kicked-before`: the ticket goes back to App before the deadline;
* `done-after`: the notice is written, then the ticket is completed before the
  listener's next pass;
* `done-in-send`: the ticket is completed while the listener is sending it,
  between its claim and the pre-send recheck;
* `snoozed-again`: the notice is written, then the Director snoozes the ticket
  again in a new batch before it is sent;
* `valid`: nothing changes;
* `mixed`: one ticket is completed, the other is unchanged;
* `cleared-then-done`: the Director clears one ticket early; the notice is
  written for the other, which is then completed before it is sent;
* `last-ready-in-send`: two tickets are ready at the deadline; the anchor is
  completed before the claim, and the sender itself completes the other, the
  last one ready, before it returns (Audit's reproduction);
* `member-busy`: a write holds the second ready ticket's row when the
  listener comes to send.

On main, from a git archive, every one of them reaches the Director, the last
ready ticket completing inside the send. Here only `valid`, `mixed`,
`last-ready-in-send` and `member-busy` do, once; the others are written never,
or dropped acknowledged with the reason traced, and no later pass sends them.
The delivery hold covers every ticket that keeps the notice current: the last
ready one's completion waits for the send, and a write in flight on it makes
the notice wait a pass. A fresh schema.sql board has the migration's functions.
"""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "130fc35d5244d9fbe1be491925863240c558337d"  # main before SYRD-679
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"
BATCHES = {  # name: its tickets
    "done-before": ("PGU-1",), "kicked-before": ("PGU-2",), "done-after": ("PGU-3",), "done-in-send": ("PGU-4",),
    "snoozed-again": ("PGU-5",), "valid": ("PGU-6",), "mixed": ("PGU-7", "PGU-8"), "cleared-then-done": ("PGU-10", "PGU-11"),
    "last-ready-in-send": ("PGU-12", "PGU-13"), "member-busy": ("PGU-14", "PGU-15"),
}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


def scenario(root: Path) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as t
    from scripts.ticket_board import reminder_snooze
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster
    from workflow_document_eras import before_relaying

    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    out: dict = {"stale_reasons": {}}
    with temporary_cluster(prefix="syrd679-", shutdown="immediate") as cluster:
        admin = t.conninfo(cluster.socket_dir, cluster.port, "board")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "board"])
        t.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        if t.psql(admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
            t.create_roles(admin)
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        migrated = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                  env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert migrated.returncode == 0, migrated.stderr[-800:]
        t.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        listener_url = t.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app = t.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="cerulean",
                               ticket_prefix="PGU",
                               database_url=t.conninfo(cluster.socket_dir, cluster.port, "board", t.SERVICE_ROLE))
        doc = copy.deepcopy(before_relaying())
        doc["project"] = "cerulean"
        doc.setdefault("reassign", {})
        doc.setdefault("remove_stages", [])
        app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
        tickets = [tid for members in BATCHES.values() for tid in members]
        for tid in tickets:
            t.seed_postgres_ticket(admin, tid, title=tid, state="director_review", assignee="director",
                                   commit_hash=COMMIT, audit_signoff=True)

        def sql(statement: str, url: str = admin) -> str:
            return t.psql(url, statement).strip()

        def snooze(members: tuple[str, ...], reason: str) -> int:
            result = reminder_snooze.perform(app, "snooze_reminders", {
                "tickets": list(members), "until": sql("SELECT to_char((clock_timestamp() + interval '1 hour') "
                                                       "AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');"),
                "reason": reason, "apply": True}, caller_role="director")
            return int(result["batch"])

        def deadline(batch: int) -> None:
            """The batch's hour is up: the disposable clock, as the listener's next pass sees it."""
            sql(f"UPDATE ticket_board.reminder_snooze_batches SET due_at = clock_timestamp() - interval '1 second' "
                f"WHERE id = {batch};")

        def complete(tid: str) -> None:
            app.perform_workflow_action(tid, "mark_done", {}, caller_role="director")

        delivered: list[tuple[str, str]] = []

        def listen(*, emit: bool = False, during_send=None, on_send=None) -> tuple[int, list[str]]:
            before = len(delivered)
            extra = {}
            if during_send is not None:
                def sleeper(_seconds: float) -> None:
                    during_send()
                extra = {"sleeper": sleeper, "pre_send_recheck_delay_seconds": 0.25}
            listener = TicketBoardNotifyListener(
                conninfo=listener_url, project="cerulean", **extra,
                sender=lambda target, message: (delivered.append((target, message)), on_send and on_send(message)),
                activity_gate=lambda _target: False, target_exists=lambda _target: True,
                submission_witness=lambda target, _since: any(sent == target for sent, _m in delivered),
            )
            emitted = 0
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                if emit:
                    emitted = listener.process_reminder_snooze_due(conn)
                listener.process_due_notifications(conn)
            return emitted, [message for _target, message in delivered[before:]]

        def due_messages(batch: int, messages: list[str]) -> list[str]:
            return [m for m in messages if f"Reminder snooze batch {batch} is due" in m]

        def record(name: str, batch: int, emitted: int, messages: list[str]) -> None:
            later = [listen(emit=True) for _ in range(2)]
            out[name] = {
                "batch": batch, "emitted": emitted, "sent": due_messages(batch, messages),
                "later_emitted": [e for e, _m in later], "later_sent": [due_messages(batch, m) for _e, m in later],
                "row": json.loads(sql(
                    "SELECT jsonb_build_object('state', state, 'close_reason', close_reason, 'notice', notification_id, "
                    "'members', (SELECT jsonb_agg(jsonb_build_object('ticket', m.ticket_id, 'released', "
                    "m.release_reason) ORDER BY m.position) FROM ticket_board.reminder_snooze_members m "
                    f"WHERE m.batch_id = b.id))::text FROM ticket_board.reminder_snooze_batches b WHERE id = {batch};")),
                "queued": sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                              f"WHERE dedupe_key = 'reminder-snooze-due:{batch}';"),
                "drops": json.loads(sql(
                    "SELECT coalesce(jsonb_agg(jsonb_build_object('phase', d.detail ->> 'phase', 'premise', "
                    "d.detail ->> 'snooze_premise') ORDER BY d.id), '[]')::text "
                    "FROM ticket_board.notification_trace d JOIN ticket_board.notification_trace e "
                    "ON e.notification_id = d.notification_id AND e.event = 'enqueue' "
                    f"WHERE d.event = 'drop' AND e.detail -> 'payload' ->> 'batch' = '{batch}';")),
            }

        # Changed before the deadline: completed (SYRD-677, SYRD-684), or returned to App.
        batch = snooze(BATCHES["done-before"], "waiting for the deploy")
        complete("PGU-1")
        out["done_before_ticket"] = app.get_ticket("PGU-1").get("reminder_snooze")
        deadline(batch)
        record("done-before", batch, *listen(emit=True))
        batch = snooze(BATCHES["kicked-before"], "waiting for the deploy")
        app.perform_workflow_action("PGU-2", "director_kick_back", {"reason": "integration conflict"},
                                    caller_role="director")
        deadline(batch)
        record("kicked-before", batch, *listen(emit=True))

        # Written at the deadline, then completed before the listener's next pass.
        batch = snooze(BATCHES["done-after"], "waiting for the deploy")
        deadline(batch)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            written = reminder_snooze.emit_due(conn, __import__("logging").getLogger("s679"))
        complete("PGU-3")
        record("done-after", batch, written, listen()[1])

        # Completed while the listener is sending it: after its claim, before the pre-send recheck.
        batch = snooze(BATCHES["done-in-send"], "waiting for the deploy")
        deadline(batch)
        completed: list[str] = []

        def finish() -> None:
            if not completed:
                completed.append("PGU-4")
                complete("PGU-4")

        record("done-in-send", batch, *listen(emit=True, during_send=finish))
        out["done_in_send_completed"] = completed

        # Snoozed again in a later batch after its notice was written.
        batch = snooze(BATCHES["snoozed-again"], "waiting for the deploy")
        deadline(batch)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            written = reminder_snooze.emit_due(conn, __import__("logging").getLogger("s679"))
        again = snooze(BATCHES["snoozed-again"], "and the next one")
        record("snoozed-again", batch, written, listen()[1])
        out["snoozed_again_still"] = sql(f"SELECT state FROM ticket_board.reminder_snooze_batches WHERE id = {again};")

        # Unchanged, and half changed: one notice each, delivered once.
        batch = snooze(BATCHES["valid"], "waiting for the deploy")
        deadline(batch)
        record("valid", batch, *listen(emit=True))
        batch = snooze(BATCHES["mixed"], "waiting for the deploy")
        complete("PGU-7")
        deadline(batch)
        record("mixed", batch, *listen(emit=True))
        # A ticket cleared early is unchanged, but it was not released as ready: it keeps nothing current.
        batch = snooze(BATCHES["cleared-then-done"], "waiting for the deploy")
        reminder_snooze.perform(app, "clear_reminder_snooze", {"batch": batch, "tickets": ["PGU-10"],
                                                               "reason": "awake", "apply": True}, caller_role="director")
        deadline(batch)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            written = reminder_snooze.emit_due(conn, __import__("logging").getLogger("s679"))
        complete("PGU-11")
        record("cleared-then-done", batch, written, listen()[1])

        # Audit: two ready at the deadline; the anchor completed before the claim; the sender itself completes the
        # other -- the last one ready -- before it returns.
        batch = snooze(BATCHES["last-ready-in-send"], "waiting for the deploy")
        deadline(batch)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            written = reminder_snooze.emit_due(conn, __import__("logging").getLogger("s679"))
        complete("PGU-12")
        race: dict = {}

        def complete_in_sender(message: str) -> None:
            if race or f"Reminder snooze batch {batch} is due" not in message:
                return
            done = threading.Event()
            race["thread"] = threading.Thread(target=lambda: (complete("PGU-13"), done.set()), daemon=True)
            race["thread"].start()
            until = time.monotonic() + 30
            while not done.is_set() and time.monotonic() < until and sql(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = 'board' AND wait_event_type = 'Lock';") == "0":
                time.sleep(0.05)
            race["completed_in_sender"] = done.is_set()

        record("last-ready-in-send", batch, written, listen(on_send=complete_in_sender)[1])
        if "thread" in race:
            race["thread"].join(60)
        out["last_ready_in_send"] = {"completed_in_sender": race.get("completed_in_sender"), "states": [
            app.get_ticket(tid)["state"] for tid in BATCHES["last-ready-in-send"]]}

        # A write holds the second ready ticket's row when the listener comes to send: the notice waits a pass.
        batch = snooze(BATCHES["member-busy"], "waiting for the deploy")
        deadline(batch)
        with psycopg.connect(admin) as writer:
            writer.execute("SELECT 1 FROM ticket_board.tickets WHERE id = 'PGU-15' FOR UPDATE")
            emitted, busy = listen(emit=True)
            out["member_busy"] = {"sent_while_held": due_messages(batch, busy), "error": sql(
                f"SELECT coalesce(last_error, '') FROM ticket_board.ticket_notification_queue "
                f"WHERE dedupe_key = 'reminder-snooze-due:{batch}';")}
        sql(f"UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
            f"WHERE dedupe_key = 'reminder-snooze-due:{batch}';")
        record("member-busy", batch, emitted, listen()[1])
        # Any other notice still takes the hold and is sent: only a deadline notice holds its batch's tickets too.
        sql("SELECT ticket_board.enqueue_notification('PGU-6', 'ticket_update', 'director', 'SYRD-679 ordinary notice', "
            "'{\"kind\": \"ticket_context\", \"id\": \"PGU-6\"}'::jsonb, 'syrd679-ordinary');")
        out["ordinary_sent"] = [m for m in listen()[1] if "SYRD-679 ordinary notice" in m]
        # And a stale one is dropped as before, with no snooze premise in its trace.
        sql("SELECT ticket_board.enqueue_notification('PGU-1', 'ticket_update', 'director', 'SYRD-679 stale notice', "
            "'{\"kind\": \"ticket_update\", \"id\": \"PGU-1\", \"state\": \"director_review\"}'::jsonb, "
            "'syrd679-stale');")
        out["ordinary_stale"] = {"sent": [m for m in listen()[1] if "SYRD-679 stale notice" in m], "drops": json.loads(sql(
            "SELECT coalesce(jsonb_agg(d.detail - 'payload' ORDER BY d.id), '[]')::text FROM ticket_board.notification_trace d "
            "JOIN ticket_board.notification_trace e ON e.notification_id = d.notification_id AND e.event = 'enqueue' "
            "WHERE d.event = 'drop' AND e.detail ->> 'dedupe_key' = 'syrd679-stale';"))}

        if (root / "scripts/ticket_board/migrations/pgu1017_syrd679_reminder_snooze_due_premise.sql").exists():
            t.seed_postgres_ticket(admin, "PGU-9", title="PGU-9", state="director_review", assignee="director",
                                   commit_hash=COMMIT, audit_signoff=True)
            open_batch = snooze(("PGU-9",), "still open")
            for name, payload in (("missing", {"kind": "reminder_snooze_due", "batch": 999}),
                                  ("not_a_number", {"kind": "reminder_snooze_due", "batch": "x"}),
                                  ("open", {"kind": "reminder_snooze_due", "batch": open_batch}),
                                  ("closed_without_notice", {"kind": "reminder_snooze_due",
                                                             "batch": out["done-before"]["batch"]}),
                                  ("valid", {"kind": "reminder_snooze_due", "batch": out["valid"]["batch"]})):
                out["stale_reasons"][name] = sql(
                    f"SELECT coalesce(ticket_board.reminder_snooze_due_stale_reason('{json.dumps(payload)}'::jsonb), "
                    "'NULL');", listener_url)
            def holds(batch_id: int, busy: str) -> bool:
                """The hold, asked while a write holds `busy`'s row."""
                with psycopg.connect(admin) as writer, psycopg.connect(listener_url) as held:
                    writer.execute("SELECT 1 FROM ticket_board.tickets WHERE id = %s FOR UPDATE", (busy,))
                    return bool(held.execute("SELECT ticket_board.hold_reminder_snooze_due(%s::jsonb)", (
                        json.dumps({"kind": "reminder_snooze_due", "batch": batch_id}),)).fetchone()[0])

            mixed = out["mixed"]["batch"]  # PGU-7 completed before the deadline, PGU-8 ready
            out["holds"] = {"ready_busy": holds(mixed, "PGU-8"), "changed_busy": holds(mixed, "PGU-7"),
                            "other_batch_busy": holds(mixed, "PGU-6")}
            # A board built from schema.sql alone has the migration's functions.
            fresh = t.conninfo(cluster.socket_dir, cluster.port, "fresh")
            t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            t.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            t.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
            definitions = ("SELECT string_agg(pg_get_functiondef(p.oid), E'\\n' ORDER BY p.proname) FROM pg_proc p "
                           "WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN "
                           "('emit_due_reminder_snoozes', 'reminder_snooze_due_stale_reason', "
                           "'hold_reminder_snooze_due');")
            out["fresh_is_migrated"] = sql(definitions, fresh) == sql(definitions) != ""
            out["service_may_ask"] = sql(
                "SELECT has_function_privilege('ticket_board_service', "
                "'ticket_board.reminder_snooze_due_stale_reason(jsonb)', 'EXECUTE')::text || "
                "has_function_privilege('ticket_board_service', 'ticket_board.hold_reminder_snooze_due(jsonb)', "
                "'EXECUTE')::text || has_function_privilege('ticket_board_listener', "
                "'ticket_board.hold_reminder_snooze_due(jsonb)', 'EXECUTE')::text;")
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL",
                                                                     "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree)],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=400,
                           stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    check(got["done_before_ticket"]["status"] == "invalidated" and "state" in got["done_before_ticket"]["changed"],
          f"before: a ticket completed while snoozed reads invalidated: {got['done_before_ticket']}")
    for name in ("done-before", "kicked-before", "done-after", "done-in-send", "snoozed-again", "valid", "mixed",
                 "cleared-then-done", "last-ready-in-send"):
        seen = got[name]
        check(len(seen["sent"]) == 1 and seen["drops"] == [],
              f"before ({name}): the Director is sent the batch's notice: {seen}")
    check("Ready: none. Changed while snoozed: PGU-1 (" in got["done-before"]["sent"][0],
          f"before: as for SYRD-677 and SYRD-684, a notice with nothing ready: {got['done-before']['sent']}")
    check(got["done_in_send_completed"] == ["PGU-4"],
          f"before: the ticket was completed during the send: {got['done_in_send_completed']}")
    check(got["last_ready_in_send"] == {"completed_in_sender": True, "states": ["done", "done"]},
          f"before (Audit): the last ready ticket completes inside the send: {got['last_ready_in_send']}")
    check(len(got["ordinary_sent"]) == 1, f"before: an ordinary notice is sent: {got['ordinary_sent']}")
    check(len(got["member_busy"]["sent_while_held"]) == 1,
          f"before: the notice is sent while a write holds a ready ticket: {got['member_busy']}")


def candidate(got: dict) -> None:
    for name in ("done-before", "kicked-before"):
        seen = got[name]
        check(seen["emitted"] == 0 and seen["sent"] == [] and seen["queued"] == "0"
              and seen["row"]["state"] == "due_emitted" and seen["row"]["notice"] is None
              and seen["row"]["close_reason"] == "due; every ticket changed while snoozed, so no notice"
              and seen["row"]["members"][0]["released"].startswith("due; ")
              and seen["later_emitted"] == [0, 0] and seen["later_sent"] == [[], []],
              f"{name}: no notice is written; the batch closes, saying why, and no later pass writes one: {seen}")
    for name, phase in (("done-after", None), ("done-in-send", "pre_send_recheck"), ("snoozed-again", None),
                        ("cleared-then-done", None)):
        seen = got[name]
        check(seen["emitted"] == 1 and seen["sent"] == [] and seen["queued"] == "0"
              and [d["premise"] for d in seen["drops"]] == ["no_ticket_still_snoozed"]
              and seen["later_emitted"] == [0, 0] and seen["later_sent"] == [[], []],
              f"{name}: the written notice is dropped, its reason traced, and never sent later: {seen}")
        if phase:
            check(seen["drops"][0]["phase"] == phase, f"{name}: dropped at {phase}: {seen['drops']}")
    check(got["done_in_send_completed"] == ["PGU-4"],
          f"the ticket was completed during the send: {got['done_in_send_completed']}")
    check(got["snoozed_again_still"] == "snoozed",
          f"the later batch is untouched by the earlier notice's drop: {got['snoozed_again_still']}")
    for name, ready in (("valid", "Ready: PGU-6 (director_review, director)."),
                        ("mixed", "Ready: PGU-8 (director_review, director). Changed while snoozed: PGU-7 (")):
        seen = got[name]
        check(seen["emitted"] == 1 and len(seen["sent"]) == 1 and ready in seen["sent"][0] and seen["drops"] == []
              and seen["later_emitted"] == [0, 0] and seen["later_sent"] == [[], []] and seen["queued"] == "0",
              f"{name}: one notice, delivered once: {seen}")
    seen = got["last-ready-in-send"]
    check(seen["emitted"] == 1 and len(seen["sent"]) == 1 and "Ready: PGU-12 (director_review, director), "
          "PGU-13 (director_review, director)." in seen["sent"][0] and seen["drops"] == []
          and seen["later_sent"] == [[], []] and seen["queued"] == "0"
          and got["last_ready_in_send"] == {"completed_in_sender": False, "states": ["done", "done"]},
          f"last-ready-in-send (Audit): the last ready ticket's completion waits for the send, then lands: "
          f"{seen} {got['last_ready_in_send']}")
    seen = got["member-busy"]
    check(got["member_busy"] == {"sent_while_held": [], "error": "board write in flight"} and seen["emitted"] == 1
          and len(seen["sent"]) == 1 and seen["drops"] == [] and seen["later_sent"] == [[], []] and seen["queued"] == "0",
          f"member-busy: a write in flight on a ready ticket that is not the anchor holds the notice a pass: "
          f"{got['member_busy']} {seen}")
    check(len(got["ordinary_sent"]) == 1, f"any other notice is still held and sent once: {got['ordinary_sent']}")
    check(got["ordinary_stale"]["sent"] == [] and len(got["ordinary_stale"]["drops"]) == 1
          and not any("snooze_premise" in d for d in got["ordinary_stale"]["drops"]),
          f"any other stale notice is dropped with no snooze premise: {got['ordinary_stale']}")
    check(got["holds"] == {"ready_busy": False, "changed_busy": True, "other_batch_busy": True},
          f"the hold is on the batch's ready tickets, no others: {got['holds']}")
    check(got["stale_reasons"] == {"missing": "batch_missing", "not_a_number": "batch_missing",
                                   "open": "batch_mismatch", "closed_without_notice": "batch_mismatch",
                                   "valid": "NULL"},
          f"the batch identity decides first: {got['stale_reasons']}")
    check(got["fresh_is_migrated"] is True, f"a fresh schema.sql board has the migration's functions: {got['fresh_is_migrated']}")
    check(got["service_may_ask"] == "falsefalsetrue",
          f"only the listener asks or holds: service, service, listener {got['service_may_ask']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    if os.environ.get("SYRD679_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd679-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"reminder_snooze_due_premise_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
