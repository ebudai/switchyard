#!/usr/bin/env python3
"""SYRD-675: a priority stall alert is about no progress, on the one clock every stall alert reads.

On SYRD-674 Ops took the ticket at 14:24:30 UTC, posted a durable design
checkpoint at 14:28:27, and ran related suites; at about 14:35 the Director was
sent "PRIORITY SYRD-674 ... its pane has said busy since, with nothing moving on
screen". Ops's next checkpoint was at 14:54:31, its submission at 15:23:34.

Here a production-built board's own functions are asked as the listener asks
them, each pass at an explicit instant, on tickets whose records are dated in
the past as the timeline dates them:

* SYRD-674's timeline, a pass every half minute from the hand-over to the
  submission, with the pane busy and nothing moving: no alert;
* the same pane with no checkpoint: one alert, past the board's stall
  threshold (30 minutes by default), and not before;
* a configured threshold of 10 minutes, counted from the latest checkpoint;
* a bounded child (an execution lease) holds the alert off while it runs, and
  only up to its deadline;
* an alert queued, then a checkpoint, a bounded child, a replaced runtime, or
  the owner's hook moving before it is delivered: delivery drops it, saying
  why, and the next pass ends its episode, so the owner is not left muted;
* the unresolved-turn escalation waits a full grace after an owner checkpoint;
* an alert dropped at delivery for its own reason (resolved for now) does not
  leave the owner without its prompts: the next pass ends its episode;
* the Director's hand-back starts the clock again;
* the listener's pass hands delivery the owner's hook as it is then;
* a listener's first pass, before the board stall pass records the threshold,
  uses the threshold the listener is configured with;
* while an episode is escalated, the Director's other stall notices about it
  are not delivered;
* schema.sql's copies are the migration's.

The before-run is main's code, from a git archive, with the same steps.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = 0
GRACE = 600
#: SYRD-674, in seconds after the hand-over (14:24:30): the checkpoints, and the submission.
CHECKPOINTS = (237, 1801)
SUBMITTED = 3544
FUNCTIONS = ("role_progress_at", "role_stall_escalation_stale_reason", "note_role_stall", "role_stall_notice_is_current",
             "notify_unresolved_turn_end")


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path) -> dict:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import notify_listener as nl

    seen: dict = {}
    with temporary_cluster(prefix="s675", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        api = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        has_clock = (root / "scripts/ticket_board/migrations/pgu1015_syrd675_progress_clock.sql").is_file()

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def replica(statement: str) -> None:
            # The fixture dates its records, past the board's triggers: it arranges history, it is not a write.
            sql(f"SET session_replication_role = replica; {statement} SET session_replication_role = origin;")

        def as_listener(statement: str, params: tuple = ()):
            with psycopg.connect(listener_url, autocommit=True) as conn:
                value = conn.execute(statement, params).fetchone()[0]
            return value.decode() if isinstance(value, bytes) else value  # the listener's text comes as bytes

        # Every ticket's story is dated from one instant far enough back that the whole timeline is in the past.
        base = datetime.fromisoformat(sql("SELECT (date_trunc('second', now()) - interval '5000 seconds')::text;"))

        def at(offset: float) -> str:
            return (base + timedelta(seconds=offset)).isoformat()

        tickets = iter(range(1, 100))
        state: dict = {}

        def begin(role: str, title: str, start: float = 0) -> str:
            """A ticket handed to `role` at `start`, its hand-over delivered then; the role's earlier one done."""
            if role in state:
                fixture.post_json(api, f"/api/tickets/{state[role]}/actions/force_move",
                                  {"state": "done", "assignee": "unassigned"}, caller="director")
            ticket = f"PGU-{next(tickets)}"
            fixture.seed_postgres_ticket(admin, ticket, title=title, state="analysis", assignee="director",
                                         commit_exempt=True)
            fixture.post_json(api, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")
            sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")
            sql(f"SELECT ticket_board.record_notification_trace('{ticket}', {9000 + int(ticket[4:])}, '{role}', "
                "'transition', 'send');")
            replica(f"UPDATE ticket_board.ticket_notification_state SET current_assignment_at = '{at(start)}', "
                    f"entered_current_state_at = '{at(start)}' WHERE ticket_id = '{ticket}'; "
                    f"UPDATE ticket_board.notification_trace SET ts = '{at(start)}' WHERE ticket_id = '{ticket}'; "
                    f"UPDATE ticket_board.ticket_comments SET ts = '{at(start)}' WHERE ticket_id = '{ticket}'; "
                    f"UPDATE ticket_board.board_progress_events SET at = '{at(start)}' WHERE ticket_id = '{ticket}';")
            state[role] = ticket
            return ticket

        def checkpoint(ticket: str, role: str, offset: float) -> None:
            """The owner's durable checkpoint, as the owner writes it, dated `offset`."""
            sql(f"SELECT ticket_board.append_ticket_comment('{ticket}', '{role}', $c${role} checkpoint at {offset}$c$);")
            replica(f"UPDATE ticket_board.ticket_comments SET ts = '{at(offset)}' WHERE ticket_id = '{ticket}' "
                    f"AND text = $c${role} checkpoint at {offset}$c$; "
                    f"UPDATE ticket_board.board_progress_events SET at = '{at(offset)}' WHERE id = "
                    f"(SELECT max(id) FROM ticket_board.board_progress_events WHERE ticket_id = '{ticket}');")

        def report(role: str, offset: float, *, turn: float = 1) -> dict:
            """The listener's report: the hook busy since the turn the hand-over started, nothing on the screen
            moving -- which the listener says once the hook has been busy past the grace."""
            return {role: {"state": "busy", "updated_at": at(turn), "turn_started_at": at(turn),
                           "busy_still": offset - turn >= GRACE, "condition": "Running the related suites"}}

        def tick(role: str, offset: float, **kw) -> int:
            return int(as_listener("SELECT ticket_board.note_role_stall(%s::jsonb, 3, %s::interval, %s::timestamptz)",
                                   (json.dumps(report(role, offset, **kw)), f"{GRACE} seconds", at(offset))))

        def escalation(ticket: str) -> dict | None:
            row = sql(f"SELECT payload::text FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                      "AND target_role = 'director' AND payload->>'kind' = 'stall_escalation' ORDER BY id DESC LIMIT 1;")
            return json.loads(row) if row else None

        def episode(ticket: str) -> str:
            return sql("SELECT coalesce(string_agg(phase || '/' || coalesce(close_reason, 'open'), ',' ORDER BY id), '') "
                       f"FROM ticket_board.role_stall_episodes WHERE ticket_id = '{ticket}';")

        def lease(ticket: str, role: str, offset: float, deadline: float, seen_at: float, child: str) -> None:
            sql("INSERT INTO ticket_board.execution_leases (role, child, ticket_id, pid, pgid, start_ticks, started_at, "
                "command, deadline, deadline_source, first_seen_at, last_seen_at) VALUES "
                f"('{role}', '{child}', '{ticket}', 4242, 4242, 1, '{at(offset)}', 'pytest -q tests/', '{at(deadline)}', "
                f"'declared', '{at(offset)}', '{at(seen_at)}');")

        def seen_lease(child: str, seen_at: float) -> None:
            sql(f"UPDATE ticket_board.execution_leases SET last_seen_at = '{at(seen_at)}' WHERE child = '{child}';")

        def listener() -> object:
            return nl.TicketBoardNotifyListener(conninfo=listener_url, project="pgu", sender=lambda *_a: None,
                                                activity_gate=lambda _t: True, target_exists=lambda _t: True,
                                                poll_seconds=0)

        def deliverable(ticket: str, payload: dict, hook: dict | None = None) -> bool:
            held = listener()
            if hook is not None:
                held.eligibility.stall_owner_hook = lambda _role: hook
            with psycopg.connect(listener_url, autocommit=True) as conn:
                held.refresh_workflow(conn)
                return held.eligibility._notification_is_current(conn, ticket, "director", json.dumps(payload))

        def deliver_or_drop(ticket: str) -> dict:
            """The listener's pre-send recheck of the queued escalation: dropped, and the trace it leaves."""
            notice = int(sql(f"SELECT id FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                             "AND payload->>'kind' = 'stall_escalation' ORDER BY id DESC LIMIT 1;"))
            payload = sql(f"SELECT payload::text FROM ticket_board.ticket_notification_queue WHERE id = {notice};")
            held = listener()
            with psycopg.connect(listener_url, autocommit=True) as conn:
                held.refresh_workflow(conn)
                dropped = held.eligibility.drop_if_stale(conn, notification_id=notice, ticket_id=ticket,
                                                          target_role="director", kind="stall_escalation",
                                                          payload=payload, phase="pre_send_recheck")
            trace = sql("SELECT coalesce(detail->>'stall_premise', '-') FROM ticket_board.notification_trace "
                        f"WHERE notification_id = {notice} AND event = 'drop' ORDER BY id DESC LIMIT 1;")
            return {"dropped": dropped, "premise": trace}

        # -- SYRD-674's timeline ------------------------------------------------------------------------------------
        ticket = begin("main", "Report decision kept once confirmed")
        fired: list[float] = []
        offset = 30.0
        while offset < SUBMITTED:
            for mark in CHECKPOINTS:
                if offset - 30 < mark <= offset:
                    checkpoint(ticket, "main", mark)
            if tick("main", offset):
                fired.append(offset)
            offset += 30
        seen["timeline_fired"] = fired
        seen["timeline_message"] = (escalation(ticket) or {}).get("message", "")

        # -- the control: the same pane, no checkpoint -------------------------------------------------------------
        ticket = begin("app", "Busy with nothing moving")
        seen["control"] = [tick("app", t) for t in (GRACE + 30, 1790, 1830)]
        seen["control_payload"] = {k: v for k, v in (escalation(ticket) or {}).items() if k != "message"}
        seen["control_deliverable"] = deliverable(ticket, escalation(ticket) or {}, report("app", 1830)["app"])

        # -- a configured threshold of ten minutes, from the latest checkpoint -----------------------------------------
        sql("UPDATE ticket_board.board_stall_state SET threshold_seconds = 600 WHERE id;")
        ticket = begin("main", "Ten minute threshold")
        checkpoint(ticket, "main", 240)
        seen["configured"] = [tick("main", t) for t in (601, 830, 850)]
        sql("UPDATE ticket_board.board_stall_state SET threshold_seconds = 1800 WHERE id;")

        # -- a bounded child holds it off, up to its deadline ----------------------------------------------------------
        ticket = begin("app", "Suites under a lease")
        lease(ticket, "app", 100, 2400, 100, "lease-suites")
        held_off = []
        for t in (1900, 2390, 4190):
            seen_lease("lease-suites", t)  # still running: the listener sees it on every pass
            held_off.append(tick("app", t))
        seen_lease("lease-suites", 4210)
        held_off.append(tick("app", 4210))
        seen["lease"] = held_off

        # -- queued, then something moved before delivery ---------------------------------------------------------------
        ticket = begin("main", "Checkpoint before delivery")
        seen["stale_checkpoint_queued"] = tick("main", 1830)
        checkpoint(ticket, "main", 1840)
        seen["stale_checkpoint_deliverable"] = deliverable(ticket, escalation(ticket) or {}, report("main", 1850)["main"])
        if has_clock:
            seen["stale_checkpoint_drop"] = deliver_or_drop(ticket)
        else:
            seen["stale_checkpoint_drop"] = {}
        tick("main", 1860)
        seen["stale_checkpoint_episode"] = episode(ticket)

        ticket = begin("app", "Child started before delivery")
        seen["stale_lease_queued"] = tick("app", 1830)
        lease(ticket, "app", 1840, 4000, 1845, "lease-late")
        seen["stale_lease_deliverable"] = deliverable(ticket, escalation(ticket) or {}, report("app", 1850)["app"])
        tick("app", 1850)  # the next pass, before any delivery
        seen["stale_lease_pass"] = [episode(ticket), escalation(ticket) is None, sql(
            f"SELECT coalesce(string_agg(busy_reason, ','), '') FROM ticket_board.notification_trace "
            f"WHERE ticket_id = '{ticket}' AND event = 'discard';")]

        ticket = begin("main", "Hook moved before delivery")
        seen["stale_hook_queued"] = tick("main", 1830)
        payload = escalation(ticket) or {}
        seen["stale_hook"] = [deliverable(ticket, payload, {"state": "busy", "updated_at": at(1), "turn_started_at": at(1)}),
                              deliverable(ticket, payload, {"state": "busy", "updated_at": at(1845),
                                                            "turn_started_at": at(1845)}),
                              deliverable(ticket, payload, {"state": "idle", "updated_at": at(1), "turn_started_at": at(1)})]
        # While it is escalated, the Director's other stall notices about it are not delivered.
        seen["deduplicated"] = deliverable(ticket, {"id": ticket, "kind": "unresolved_turn", "state": "in_progress",
                                                    "assignee": "main", "target_role": "director"})
        sql("INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
            "process_pid, process_start_time, process_uid, generation, registered_at) VALUES "
            f"('main', 'claude', 'pgu-main:0.0', '/tmp', '/tmp', 4243, 1, 1000, 2, '{at(1850)}');")
        seen["stale_runtime"] = deliverable(ticket, payload, report("main", 1860)["main"])
        if has_clock:
            seen["stale_reasons"] = [as_listener("SELECT ticket_board.role_stall_escalation_stale_reason(%s::jsonb)",
                                                 (json.dumps(payload),))]
        sql("DELETE FROM ticket_board.role_runtime_assignments WHERE role = 'main';")

        # -- dropped at delivery for its own reason (resolved for now): the owner is not left muted ----------------
        ticket = begin("app", "Resolved for now at delivery")
        seen["stale_resolved_queued"] = tick("app", 1830)
        sql(f"SELECT ticket_board.enqueue_notification('{ticket}', 'transition', 'app', 'm', jsonb_build_object("
            f"'kind', 'transition', 'id', '{ticket}', 'new_state', 'in_progress', 'assignee', 'app'), 'probe:{ticket}');")
        dropped = deliver_or_drop(ticket) if has_clock else {}
        sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")
        tick("app", 1840)
        seen["stale_resolved"] = [dropped, episode(ticket)]

        # -- the Director hands it back: a recovery, and the clock starts again from it -----------------------------
        ticket = begin("main", "Handed back by the Director")
        seen["handback_queued"] = tick("main", 1830)
        notice = sql(f"SELECT id FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                     "AND payload->>'kind' = 'stall_escalation';")
        # Delivered to the Director, as the listener delivers it.
        sql(f"SELECT ticket_board.record_notification_trace('{ticket}', {notice}, 'director', 'unresolved_turn', 'send');")
        as_listener("SELECT ticket_board.ack_notification(%s::bigint) IS NULL", (int(notice),))
        sql(f"SELECT ticket_board.append_ticket_comment('{ticket}', 'director', $c$Back to you: run them bounded.$c$);")
        replica(f"UPDATE ticket_board.ticket_comments SET ts = '{at(1900)}' WHERE ticket_id = '{ticket}' "
                "AND who = 'director' AND text LIKE 'Back to you%';")
        seen["handback"] = [tick("main", t) for t in (1910, 1920, 3690, 3710)]

        # -- the unresolved-turn escalation's grace, from the owner's latest progress ---------------------------------
        def unresolved(ticket: str, role: str, offset: float) -> int:
            as_listener("SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, %s::timestamptz, %s::interval, "
                        "%s::jsonb)", (at(offset), f"{GRACE} seconds",
                                       json.dumps({role: {"state": "idle", "updated_at": at(2), "turn_started_at": at(1)}})))
            return int(sql(f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                           "AND target_role = 'director' AND kind = 'unresolved_turn';"))

        def prompted(ticket: str, role: str) -> None:
            key = sql(f"SELECT 'repair:' || ticket_board.turn_unresolved_identity('{ticket}', 'in_progress', '{role}', '') "
                      "|| 'turn-1';")
            notice = 9500 + int(ticket[4:])
            sql(f"SELECT ticket_board.record_notification_trace('{ticket}', {notice}, '{role}', 'unresolved_turn_repair', "
                f"'enqueue', NULL, NULL, NULL, jsonb_build_object('dedupe_key', '{key}'));")
            sql(f"SELECT ticket_board.record_notification_trace('{ticket}', {notice}, '{role}', 'unresolved_turn_repair', "
                "'send');")
            replica(f"UPDATE ticket_board.notification_trace SET ts = '{at(10)}' WHERE notification_id = {notice} "
                    "AND event = 'enqueue'; "
                    f"UPDATE ticket_board.notification_trace SET ts = '{at(15)}' WHERE notification_id = {notice} "
                    "AND event = 'send';")

        ticket = begin("app", "Prompted, no checkpoint")
        prompted(ticket, "app")
        seen["unresolved_control"] = [unresolved(ticket, "app", 400), unresolved(ticket, "app", 620)]
        ticket = begin("main", "Prompted, then a checkpoint")
        prompted(ticket, "main")
        checkpoint(ticket, "main", 300)
        seen["unresolved_checkpoint"] = [unresolved(ticket, "main", 620), unresolved(ticket, "main", 890),
                                         unresolved(ticket, "main", 910)]

        # -- the listener's pass hands delivery the owner's hook as it is then ----------------------------------------
        if has_clock:
            import logging
            from types import SimpleNamespace

            from scripts.ticket_board import stall_episode

            class Gate:
                role_targets = {"main": "pgu-main:0.0"}

                def __init__(self) -> None:
                    self.state_store = self

                def read(self, _target):
                    return SimpleNamespace(state="idle", updated_at=base.timestamp() + 1990,
                                           turn_started_at=base.timestamp() + 1900)

                def gate(self, _target) -> bool:
                    return True

                def cursor_position_runner(self, *_a, **_k):
                    raise OSError("no tmux here")

                capture_pane_runner = cursor_position_runner

            pane = SimpleNamespace(activity_gate=Gate().gate, eligibility=SimpleNamespace(),
                                   unresolved_turn_grace_seconds=GRACE, logger=logging.getLogger("syrd675"))
            with psycopg.connect(listener_url, autocommit=True) as conn:
                stall_episode.run(pane, conn)
            read = getattr(pane.eligibility, "stall_owner_hook", None)
            seen["wired"] = read("main") if callable(read) else None
            seen["wired_expected"] = {"state": "idle", "updated_at": at(1990), "turn_started_at": at(1900)}

            # A listener's first pass after it starts, or after its threshold was raised: the board stall pass that
            # records the threshold runs after this one, so the stored value is still the old one (Audit's B1).
            class BusyGate(Gate):
                idle_working_timer_sample_delay_seconds = 0

                def __init__(self, role: str, turn: float) -> None:
                    self.state_store = self
                    self.role_targets = {role: f"pgu-{role}:0.0"}
                    self.turn = turn

                def read(self, _target):
                    return SimpleNamespace(state="busy", updated_at=self.turn, turn_started_at=self.turn)

                def wall_time(self) -> float:
                    return time.time()

                def _working_timer_trace(self, *_a, **_k):
                    return None

                def _open_prompt_trace(self, *_a, **_k):
                    return None

            def first_pass(role: str, configured: int) -> int:
                os.environ["TICKET_BOARD_BOARD_STALL_SECONDS"] = str(configured)
                try:
                    pane = SimpleNamespace(activity_gate=BusyGate(role, base.timestamp() + 3101).gate,
                                           eligibility=SimpleNamespace(), unresolved_turn_grace_seconds=GRACE,
                                           logger=logging.getLogger("syrd675"))
                    with psycopg.connect(listener_url, autocommit=True) as conn:
                        return stall_episode.run(pane, conn)
                finally:
                    os.environ.pop("TICKET_BOARD_BOARD_STALL_SECONDS", None)

            sql("UPDATE ticket_board.board_stall_state SET threshold_seconds = 1800 WHERE id;")
            begin("app", "Busy since a hand-over 1900 seconds ago", start=3100)
            seen["first_pass"] = [first_pass("app", 3600), first_pass("app", 1800),
                                  sql("SELECT threshold_seconds FROM ticket_board.board_stall_state WHERE id;")]

        # -- schema.sql's copies --------------------------------------------------------------------------------------
        digest = ("SELECT string_agg(p.proname || ':' || md5(pg_get_functiondef(p.oid)), ',' ORDER BY p.proname) "
                  "FROM pg_proc p WHERE p.pronamespace = 'ticket_board'::regnamespace AND p.proname IN ("
                  + ", ".join(f"'{name}'" for name in FUNCTIONS) + ")")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        seen["schema_equal"] = fixture.psql(fresh, digest + ";").strip() == sql(digest + ";")
        seen["functions"] = sql(digest + ";").count(":")
        server.shutdown()
        server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def base_commit() -> str:
    """Main as it stood before this ticket: the commit before the one that added this test."""
    found = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                            "tests/role_stall_progress_clock_test.py"], check=True, capture_output=True, text=True).stdout.split()
    assert found, "this test is not committed; the run of main needs the commit that added it"
    return found[-1] + "^"


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "PG"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)], capture_output=True,
                           text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
    assert child.returncode == 0, child.stderr[-3000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(s: dict) -> None:
    """Main: the busy pane is escalated ten minutes after the hand-over, whatever moved since."""
    print("OBSERVED main " + json.dumps(s), file=sys.stderr)
    check(s["timeline_fired"] and s["timeline_fired"][0] <= GRACE + 60,
          f"main: SYRD-674's timeline is escalated about ten minutes after the hand-over: {s['timeline_fired']}")
    check(s["control"][0] == 1, f"main: a busy pane is escalated past the unresolved-turn grace: {s['control']}")
    check(s["configured"][0] == 1, f"main: a checkpoint four minutes in does not count: {s['configured']}")
    check(s["lease"][0] == 1, f"main: a running bounded child does not count: {s['lease']}")
    check(s["stale_lease_deliverable"], "main: a child started after the alert was queued leaves it deliverable")


def candidate(s: dict) -> None:
    print("OBSERVED candidate " + json.dumps(s), file=sys.stderr)
    check(s["timeline_fired"] == [],
          f"SYRD-674's timeline, a pass every half minute to the submission: no alert: {s['timeline_fired']}")
    check(s["control"] == [0, 0, 1],
          f"the same pane with no checkpoint: one alert, past the board's stall threshold and not before: {s['control']}")
    payload = s["control_payload"]
    check(payload.get("reason") == "busy_no_progress" and payload.get("threshold_seconds") == 1800
          and payload.get("progress_at") and payload.get("round_at") and payload.get("episode"),
          f"bound to what it saw: the clock, the round, the threshold, the episode: {payload}")
    check(s["control_deliverable"], "and deliverable while nothing has moved since")
    check(s["configured"] == [0, 0, 1],
          f"a configured threshold of ten minutes counts from the latest checkpoint: {s['configured']}")
    check(s["lease"] == [0, 0, 0, 1],
          f"a running bounded child holds it off, and only to its deadline plus the threshold: {s['lease']}")
    check(s["stale_checkpoint_queued"] == 1 and not s["stale_checkpoint_deliverable"]
          and s["stale_checkpoint_drop"] == {"dropped": True, "premise": "progress"},
          f"a checkpoint after it was queued: dropped at delivery, saying why: {s['stale_checkpoint_drop']}")
    check(s["stale_checkpoint_episode"] == "director/progress",
          f"and the next pass ends its episode: {s['stale_checkpoint_episode']}")
    check(s["stale_lease_queued"] == 1 and not s["stale_lease_deliverable"]
          and s["stale_lease_pass"] == ["director/progress", True, "stall_premise_execution_lease"],
          f"a bounded child started after it was queued: not deliverable, and the next pass withdraws it, saying "
          f"why: {s['stale_lease_pass']}")
    check(s["stale_hook_queued"] == 1 and s["stale_hook"] == [True, False, False],
          f"the owner's hook moving, or its turn ending, after it was queued: not deliverable: {s['stale_hook']}")
    check(not s["deduplicated"], "while it is escalated, the Director's other stall notices about it are not delivered")
    check(not s["stale_runtime"] and s["stale_reasons"] == ["runtime_replaced"],
          f"a replaced runtime: not deliverable: {s['stale_reasons']}")
    check(s["stale_resolved_queued"] == 1 and s["stale_resolved"] == [{"dropped": True, "premise": "-"}, "director/progress"],
          f"dropped at delivery for its own reason: the next pass ends its episode, so the owner is not left without "
          f"its prompts: {s['stale_resolved']}")
    check(s["handback_queued"] == 1 and s["handback"] == [0, 0, 0, 1],
          f"handed back by the Director: the clock starts again from the hand-back, and runs out a threshold "
          f"later: {s['handback']}")
    def instants(hook: dict | None) -> dict | None:
        return None if hook is None else {k: v if k == "state" else datetime.fromisoformat(v) for k, v in hook.items()}

    check(s["wired"] is not None and instants(s["wired"]) == instants(s["wired_expected"]),
          f"the listener's pass hands delivery the owner's hook as it is then: {s.get('wired')}")
    check(s["first_pass"] == [0, 1, "1800"],
          f"a listener's first pass uses the threshold it is configured with, not the one the board last stored: "
          f"raised to an hour, a pane busy 1900 s is not escalated; at 1800 it is: {s['first_pass']}")
    check(s["unresolved_control"] == [0, 1],
          f"the unresolved-turn escalation, with no checkpoint: after the grace: {s['unresolved_control']}")
    check(s["unresolved_checkpoint"] == [0, 0, 1],
          f"and a full grace after the owner's checkpoint: {s['unresolved_checkpoint']}")
    check(s["schema_equal"] and s["functions"] == len(FUNCTIONS), "schema.sql's copies are the migration's")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd675-before.") as tmp:
        before(run(tree_at(base_commit(), Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"role_stall_progress_clock_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
