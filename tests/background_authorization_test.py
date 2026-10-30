#!/usr/bin/env python3
"""SYRD-708: work the Director has authorized -- a lease extension, a reminder snooze -- raises no background-work alert.

Live syrd, 2026-10-30: App ran one declared, bounded 350-suite comparison for
SYRD-706 as process group 2036002 (lease 394). At 08:53 the Director extended
that lease through 10:23, and snoozed SYRD-706's reminders through 10:30. At
09:15 the board still told the Director that App's pane had held one SYRD-706
notice for 31 minutes behind that process, and the sixteen PostgreSQL
processes the comparison had just started under it.

The board is production-built (schema.sql, roles, rbac.sql, every migration):
* its own `note_background_hold` decides the 30-minute boundary;
* leases are recorded by the listener's own `observe_execution_lease` and
  extended by the Director's own `extend_lease`;
* snoozes are the Director's own `snooze_reminders`;
* every call is made as the listener's database role.

The processes are a fake /proc tree shaped as the live one. Nothing sleeps:
the hold's age is moved back in the database, as the board's other hold tests
do, and a deadline is moved to the past.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from background_episode_test import PANE, PROVIDER, Proc, pane  # noqa: E402
from scripts.ticket_board import background_authorization as authorization  # noqa: E402
from scripts.ticket_board import background_child, background_episode, background_hold  # noqa: E402
from scripts.ticket_board import execution_lease_ops, reminder_snooze  # noqa: E402
from scripts.ticket_board.notification_ledger import NotificationLedger  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402

CHECKS = 0
SWEEP = 2036002
WORK, OTHER = "PGU-706", "PGU-707"


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def comparison(proc: Proc) -> None:
    """App's pane: the 350-suite comparison under its own session, and PostgreSQL's children in sessions of their own."""
    pane(proc)
    proc.add(SWEEP, ppid=PROVIDER, session=SWEEP, age=38 * 60, argv=["zsh", "-c", "./compare.sh 350"])
    proc.add(SWEEP + 10, ppid=SWEEP, session=SWEEP, age=60, argv=["python3", "tests/s654_test.py"])
    proc.add(SWEEP + 20, ppid=SWEEP + 10, session=SWEEP, age=3, argv=["postgres", "-D", "/tmp/s654/pgdata"])
    for n in range(4):
        pid = 2835139 + n
        proc.add(pid, ppid=SWEEP + 20, session=pid, age=3, argv=[f"postgres: io worker {n}"])


def scenario(tmp: Path) -> None:
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from temporary_cluster import temporary_cluster

    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    proc = Proc(tmp / "proc")
    comparison(proc)
    saved = (authorization.PROC_ROOT, background_episode.PROC_ROOT)
    authorization.PROC_ROOT = background_episode.PROC_ROOT = proc.root
    with temporary_cluster(prefix="s708", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, ROOT, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
        for ticket in (WORK, OTHER):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="in_progress", assignee="app",
                                         commit_exempt=True)
        listener = psycopg.connect(fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener"),
                                   autocommit=True)
        ledger = SimpleNamespace(logger=logging.getLogger("s708"))

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def one(statement: str, params: tuple = ()) -> Any:
            return listener.execute(statement, params).fetchone()[0]

        def notice(ticket: str) -> int:
            return int(sql(f"SELECT ticket_board.enqueue_notification('{ticket}', 'ticket_update', 'app', "
                           f"'{ticket} changed', jsonb_build_object('kind', 'ticket_update', 'id', '{ticket}'), "
                           f"'s708:' || clock_timestamp()::text);"))

        def alerts() -> list[str]:
            """Every held-notice alert for the Director, sent or waiting."""
            return json.loads(sql("SELECT coalesce(json_agg(message ORDER BY id), '[]')::text FROM "
                                  "ticket_board.ticket_notification_queue WHERE target_role = 'director' "
                                  "AND payload->>'kind' = 'background_hold' AND payload->>'cleared' IS NULL;"))

        def episode() -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(alerted_at IS NOT NULL, notification_ids) "
                                  "ORDER BY id), '[]')::text FROM ticket_board.background_hold_episodes "
                                  "WHERE target_role = 'app' AND cleared_at IS NULL;"))

        def held_for(minutes: int) -> None:
            sql(f"UPDATE ticket_board.background_hold_episodes SET held_since = clock_timestamp() - "
                f"interval '{minutes} minutes' WHERE target_role = 'app' AND cleared_at IS NULL;")

        def hold(notification_id: int, ticket: str, holders: list[background_child.Holder]) -> None:
            """What the listener's ledger notes on each requeue of a notice its pane's children hold."""
            NotificationLedger(logger=ledger.logger, requeue_base_seconds=1, requeue_max_seconds=1).trace_deferral_once(
                listener, notification_id=notification_id, ticket_id=ticket, target_role="app", kind="ticket_update",
                event="gate_defer", pane_busy=True, busy_reason="prior_turn_child_work",
                detail=lambda: {"anti_clobber": {"holders": [holder.as_detail() for holder in holders]}})

        def reset() -> None:
            """The held notices leave the queue: the episode closes and any alert waiting for it is withdrawn."""
            sql("DELETE FROM ticket_board.ticket_notification_queue WHERE target_role IN ('app', 'director');")

        def lease(pid: int, ticket: str) -> int:
            ticks = background_child.start_ticks(proc.root, pid)
            started = proc.started(pid)
            row = one("SELECT ticket_board.observe_execution_lease(%s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, NULL, "
                      "to_timestamp(%s), 'default', false, '[]'::jsonb)",
                      ("app", f"{pid}@{int(started)}", pid, pid, ticks, started, "zsh -c [redacted]", ticket,
                       started + 900))
            return int((row if isinstance(row, dict) else json.loads(row))["id"])

        def extend(lease_id: int, minutes: int = 90) -> None:
            execution_lease_ops.perform(app, "extend_lease", {"lease": lease_id, "minutes": minutes,
                                                              "reason": "one declared bounded 350-suite comparison"},
                                        caller_role="director")

        def snooze(ticket: str) -> int:
            until = sql("SELECT to_char((clock_timestamp() + interval '95 minutes') AT TIME ZONE 'UTC', "
                        "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"');")
            return int(reminder_snooze.perform(app, "snooze_reminders", {
                "tickets": [ticket], "until": until, "reason": "do not poll the pane", "apply": True},
                caller_role="director")["batch"])

        holders = [proc.holder(SWEEP, finished_turn=True), *(proc.holder(2835139 + n) for n in range(4))]
        lease_id = lease(SWEEP, WORK)

        # 1. Without any authorization, the board's own boundary: told at 30 minutes, not at 29.
        first = notice(WORK)
        hold(first, WORK, holders)
        held_for(29)
        hold(first, WORK, holders)
        check(alerts() == [], f"a hold of 29 minutes is not told: {alerts()}")
        held_for(31)
        hold(first, WORK, holders)
        check(len(alerts()) == 1 and "for 31 minutes" in alerts()[0], f"one of 31 minutes is: {alerts()}")
        reset()

        # 2. SYRD-706's: the exact process under a Director's extension, PostgreSQL's children under it.
        extend(lease_id)
        first = notice(WORK)
        hold(first, WORK, holders)
        held_for(31)
        hold(first, WORK, holders)
        check(alerts() == [], f"a hold behind work the Director extended is not told at 31 minutes: {alerts()}")
        check(episode() == [[False, [first]]], f"the hold is still recorded, its clock running: {episode()}")
        check(int(sql(f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE id = {first};")) == 1,
              "and the held notice is still queued for delivery, untouched")
        why = authorization.covered(listener, "app", holders, ticket=WORK)
        check(why == f"lease {lease_id} on {WORK}, extended by the Director until " + why.rsplit("until ", 1)[-1],
              f"the authorization is named: {why}")

        # 3. A new, unrelated notice held behind the same work is told: the extension is for PGU-706's work.
        other = notice(OTHER)
        hold(other, OTHER, holders)
        check(len(alerts()) == 1 and "(PGU-706, PGU-707)" in alerts()[0],
              f"an unrelated notice held as long is told at once: {alerts()}")
        reset()

        # 4. A genuinely new command beside it, not under the lease, is told.
        proc.add(3000000, ppid=PROVIDER, session=3000000, age=600, argv=["bash", "-c", "./vm.sh"])
        first = notice(WORK)
        hold(first, WORK, [*holders, proc.holder(3000000)])
        held_for(31)
        hold(first, WORK, [*holders, proc.holder(3000000)])
        check(len(alerts()) == 1, f"a new command beside the extended one is told: {alerts()}")
        proc.remove(3000000)
        reset()

        # 5. Expiry: the moment the extension runs out, the hold that is already over 30 minutes is told.
        first = notice(WORK)
        hold(first, WORK, holders)
        held_for(45)
        hold(first, WORK, holders)
        check(alerts() == [], f"still inside the extension: {alerts()}")
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() - interval '1 second' "
            f"WHERE id = {lease_id};")
        hold(first, WORK, holders)
        check(len(alerts()) == 1 and "for 45 minutes" in alerts()[0],
              f"after expiry it is told at once, for the whole hold: {alerts()}")
        reset()
        extend(lease_id)

        # 6. Process replacement: the same number, started again, is not the process the Director extended.
        proc.add(SWEEP, ppid=PROVIDER, session=SWEEP, age=30 * 60 + 5, argv=["zsh", "-c", "./compare.sh 350"])
        replaced = [proc.holder(SWEEP, finished_turn=True), *holders[1:]]
        first = notice(WORK)
        hold(first, WORK, replaced)
        held_for(31)
        hold(first, WORK, replaced)
        check(len(alerts()) == 1, f"a replaced process is told: {alerts()}")
        reset()
        check(authorization.covered(listener, "app", holders, ticket=WORK) == "",
              "and the old identity, now gone, authorizes nothing")
        proc.add(SWEEP, ppid=PROVIDER, session=SWEEP, age=38 * 60, argv=["zsh", "-c", "./compare.sh 350"])
        lease_id = lease(SWEEP, WORK)  # the comparison as it was, under a lease of its own
        holders = [proc.holder(SWEEP, finished_turn=True), *(proc.holder(2835139 + n) for n in range(4))]
        sql("UPDATE ticket_board.execution_leases SET ended_at = clock_timestamp(), end_reason = 'replaced' "
            f"WHERE role = 'app' AND id <> {lease_id};")
        extend(lease_id)
        check(authorization.covered(listener, "app", holders, ticket=WORK).startswith(f"lease {lease_id} "),
              "the comparison's own extended lease covers it again")

        # 7. Contradictory durable state: an interrupt asked for, or the ticket gone to another role.
        execution_lease_ops.perform(app, "interrupt_lease", {"lease": lease_id, "reason": "stop it"},
                                    caller_role="director")
        check(authorization.covered(listener, "app", holders, ticket=WORK) == "",
              "an interrupt asked of the lease ends its authorization")
        sql(f"UPDATE ticket_board.execution_leases SET interrupt = NULL WHERE id = {lease_id};")
        sql(f"BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); UPDATE ticket_board.tickets SET assignee = 'main' WHERE id = '{WORK}'; COMMIT;")
        check(authorization.covered(listener, "app", holders, ticket=WORK) == "",
              "so does its ticket going to another role")
        sql(f"BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); UPDATE ticket_board.tickets SET assignee = 'app' WHERE id = '{WORK}'; COMMIT;")
        sql(f"UPDATE ticket_board.execution_leases SET state = 'stale' WHERE id = {lease_id};")
        check(authorization.covered(listener, "app", holders, ticket=WORK) == "", "and a lease marked stale")
        sql(f"UPDATE ticket_board.execution_leases SET state = 'running', deadline_source = 'default', "
            f"deadline = clock_timestamp() + interval '10 minutes' WHERE id = {lease_id};")
        check(authorization.covered(listener, "app", holders, ticket=WORK) == "",
              "a lease within its own default deadline is no Director's authorization")
        sql(f"UPDATE ticket_board.execution_leases SET deadline_source = 'extended' WHERE id = {lease_id};")
        gone = proc.holder(2835139)
        proc.add(2835139, ppid=SWEEP + 20, session=2835139, age=1, argv=["postgres: io worker 9"])
        check(authorization.covered(listener, "app", [gone], ticket=WORK) == "",
              "a holder whose number now names another process, even under the extended work, authorizes nothing")
        proc.add(2835139, ppid=SWEEP + 20, session=2835139, age=3, argv=["postgres: io worker 0"])
        holders = [proc.holder(SWEEP, finished_turn=True), *(proc.holder(2835139 + n) for n in range(4))]
        check(authorization.covered(listener, "app", holders, ticket=WORK).startswith(f"lease {lease_id} "),
              "the extended lease, once more")
        sql(f"UPDATE ticket_board.execution_leases SET deadline_source = 'default', "
            f"deadline = clock_timestamp() - interval '1 second' WHERE id = {lease_id};")
        check(authorization.covered(listener, "app", holders, ticket=WORK) == "",
              "a lease past its own deadline, never extended, authorizes nothing")

        # 8. A snooze of the work's ticket, without any extension: not told while it stands.
        batch = snooze(WORK)
        first = notice(WORK)
        hold(first, WORK, holders)
        held_for(31)
        hold(first, WORK, holders)
        check(alerts() == [], f"a hold of a snoozed ticket's notice behind its own work is not told: {alerts()}")
        other = notice(OTHER)
        hold(other, OTHER, holders)
        check(len(alerts()) == 1, f"a notice for a ticket not snoozed is: {alerts()}")
        reset()
        first = notice(WORK)
        hold(first, WORK, holders)
        held_for(31)
        hold(first, WORK, holders)
        check(alerts() == [], f"a new hold under the same snooze is quiet too: {alerts()}")
        reminder_snooze.perform(app, "clear_reminder_snooze", {"batch": batch, "tickets": [WORK],
                                                               "reason": "done waiting", "apply": True},
                                caller_role="director")
        hold(first, WORK, holders)
        check(len(alerts()) == 1, f"once the snooze is cleared, the hold is told at once: {alerts()}")
        reset()

        # 9. The other background-work alert: after a delivery goes past it (SYRD-583/704).
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() + interval '90 minutes', "
            f"deadline_source = 'extended' WHERE id = {lease_id};")
        first = notice(WORK)
        gate = SimpleNamespace(proc_root=str(proc.root), role_targets={"app": "pgu-app:0.0"},
                               _pane_pid=lambda _target: PANE, state_store=None)
        told = background_episode.tell(ledger, listener, first, holders, {}, gate=gate, role="app")
        check(told == 0, f"a delivery past extended work tells nobody about it: {told}")
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() - interval '1 second' "
            f"WHERE id = {lease_id};")
        told = background_episode.tell(ledger, listener, first, holders, {}, gate=gate, role="app")
        check(told == 1, f"past its extension, the same delivery tells the Director once: {told}")

        # 10. An alert already queued when the authorization begins is held back, not dropped (Audit).
        class Gate:
            """The listener's activity gate, as far as the delivery check asks it: what last held each pane."""

            def __init__(self, held: list[background_child.Holder]) -> None:
                self.proc_root, self.role_targets, self.held = str(proc.root), {"app": "pgu-app:0.0"}, held

            def is_working(self, _target: str) -> bool:
                return False

            def child_holders(self, target: str) -> tuple:
                return tuple(self.held) if target == "pgu-app:0.0" else ()

        pane_gate = Gate(holders)
        deliverer = TicketBoardNotifyListener(conninfo="offline", activity_gate=pane_gate.is_working)

        def queued(kind: str) -> tuple[int, str]:
            row = sql("SELECT id || '|' || payload::text FROM ticket_board.ticket_notification_queue WHERE "
                      f"target_role = 'director' AND payload->>'kind' = '{kind}' AND payload->>'cleared' IS NULL "
                      "ORDER BY id DESC LIMIT 1;")
            alert_id, payload = row.split("|", 1)
            return int(alert_id), payload

        def held_back(alert: tuple[int, str], phase: str = "claim") -> bool:
            """The claim's and the pre-send recheck's own question: is this notice deferred for background work?"""
            return deliverer._defer_for_background_work(listener, alert[0], 1, "ticket_update", "director",
                                                         alert[1], phase)

        def still_queued(alert_id: int) -> str:
            return sql(f"SELECT coalesce(last_error, '') FROM ticket_board.ticket_notification_queue WHERE id = {alert_id};")

        # 10a. The delivery-past alert from case 9, queued for the process whose extension has just passed.
        child_alert = queued("background_child")
        check(not held_back(child_alert), "a queued alert about work no longer authorized goes out")
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() + interval '90 minutes' "
            f"WHERE id = {lease_id};")
        check(held_back(child_alert) and held_back(child_alert, "pre_send_recheck"),
              "extended before its send, it is held back at claim and again before the send")
        check(still_queued(child_alert[0]).startswith(authorization.HELD_ERROR),
              f"requeued, not dropped: the same alert waits, its one-time record kept: {still_queued(child_alert[0])}")
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() - interval '1 second' "
            f"WHERE id = {lease_id};")
        check(not held_back(child_alert), "once the extension has passed, the same alert goes out")
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() + interval '90 minutes' "
            f"WHERE id = {lease_id};")
        proc.add(SWEEP, ppid=PROVIDER, session=SWEEP, age=60, argv=["zsh", "-c", "./compare.sh 350"])
        check(not held_back(child_alert), "nor is it held for a process that has replaced the one it names")
        proc.add(SWEEP, ppid=PROVIDER, session=SWEEP, age=37 * 60, argv=["zsh", "-c", "./compare.sh 350"])
        lease_id = lease(SWEEP, WORK)  # the comparison under a lease of its own, started in another second
        sql(f"UPDATE ticket_board.execution_leases SET ended_at = clock_timestamp(), end_reason = 'replaced' "
            f"WHERE role = 'app' AND id <> {lease_id};")
        holders = [proc.holder(SWEEP, finished_turn=True), *(proc.holder(2835139 + n) for n in range(4))]
        pane_gate.held = holders
        reset()

        # 10b. The held-notice alert, queued at 31 minutes before anything was authorized.
        # Its hold is moved back 31 minutes, which would take in the earlier cases' unrelated notices: forget them.
        sql(f"DELETE FROM ticket_board.notification_trace WHERE ticket_id = '{OTHER}';")
        first = notice(WORK)
        hold(first, WORK, holders)
        held_for(31)
        hold(first, WORK, holders)
        check(len(alerts()) == 1, f"queued before any authorization: {alerts()}")
        hold_alert = queued("background_hold")
        check(not held_back(hold_alert), "unauthorized, it goes out")
        extend(lease_id)
        check(held_back(hold_alert) and held_back(hold_alert, "pre_send_recheck"),
              "the Director extends the exact process before its send: it is held back at claim and before the send")
        hold(first, WORK, holders)
        check(len(alerts()) == 1 and queued("background_hold")[0] == hold_alert[0] and episode()[0][0] is True,
              f"the same alert, the episode told once, no second alert: {alerts()} {episode()}")
        pane_gate.held = [*holders, proc.holder(PROVIDER)]
        check(not held_back(hold_alert), "a holder beyond the extension lets it out")
        pane_gate.held = []
        check(not held_back(hold_alert), "so does a pane whose holders the gate no longer knows")
        pane_gate.held = holders
        other = notice(OTHER)
        hold(other, OTHER, holders)
        check(not held_back(hold_alert), "and an unrelated notice held since it was queued")
        sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE id = {other};")
        sql(f"DELETE FROM ticket_board.notification_trace WHERE ticket_id = '{OTHER}';")
        check(held_back(hold_alert), "without it, held back again")
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() - interval '1 second' "
            f"WHERE id = {lease_id};")
        check(not held_back(hold_alert), "the extension expires: the same alert goes out")

        # 10c. A snooze begun after the alert was queued holds it back the same way, until it is cleared.
        batch = snooze(WORK)
        check(held_back(hold_alert), "a snooze of the work's ticket holds it back")
        reminder_snooze.perform(app, "clear_reminder_snooze", {"batch": batch, "tickets": [WORK],
                                                               "reason": "done waiting", "apply": True},
                                caller_role="director")
        check(not held_back(hold_alert), "cleared, the alert goes out")
        check(len(alerts()) == 1 and episode()[0][0] is True, f"still one alert, still one record: {alerts()}")

        class Unreadable:
            def execute(self, *_args: Any) -> Any:
                raise RuntimeError("board went away")

        extend(lease_id)
        check(held_back(hold_alert), "authorized once more")
        try:
            answer = deliverer._defer_for_background_work(Unreadable(), hold_alert[0], 1, "ticket_update", "director",
                                                          hold_alert[1], "claim")
        except Exception as exc:  # the defect this check exists for
            answer = repr(exc)
        check(answer is False, f"an authorization that cannot be read holds nothing back: {answer}")
        check(not deliverer._defer_for_background_work(listener, first, 1, "ticket_update", "app",
                                                       json.dumps({"kind": "ticket_update", "id": WORK}), "claim"),
              "a role's own notice is never held by this")
        authorized_child = json.dumps({**json.loads(child_alert[1]), "child": f"{SWEEP}@{int(proc.started(SWEEP))}"})
        check(deliverer._defer_for_background_work(listener, child_alert[0], 1, "ticket_update", "director",
                                                   authorized_child, "claim"),
              "an alert to the Director about the extended comparison is held back")
        check(not deliverer._defer_for_background_work(listener, first, 1, "ticket_update", "app", authorized_child, "claim"),
              "nor is any notice to a role other than the Director, whatever its payload says")
        # A queued alert naming a number that a different process, under the extended work, now has.
        reused = {**json.loads(child_alert[1]), "child": f"2835139@{int(proc.started(2835139)) - 600}"}
        check(authorization.alert_authority(pane_gate, listener, reused) == "",
              "an alert naming a process by a number now reused under the extended work is not authorized")
        check(authorization.alert_authority(pane_gate, listener, {**reused, "child": f"2835139@{int(proc.started(2835139))}"})
              .startswith(f"lease {lease_id} "), "the process that number names now would be")
        reset()

        # A hand-over (SYRD-636) reports only work whose ticket is no longer the role's, or unknown: nothing
        # authorizes that, so its report is unchanged. The extension of a ticket gone to another role covers nothing.
        sql(f"UPDATE ticket_board.execution_leases SET deadline = clock_timestamp() + interval '90 minutes' "
            f"WHERE id = {lease_id};")
        check(authorization.covered(listener, "app", holders).startswith(f"lease {lease_id} "), "extended again")
        sql(f"BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); UPDATE ticket_board.tickets SET assignee = 'main' WHERE id = '{WORK}'; COMMIT;")
        check(authorization.covered(listener, "app", holders) == "",
              "the extension of a ticket no longer app's covers nothing")
        listener.close()
    authorization.PROC_ROOT, background_episode.PROC_ROOT = saved


def units() -> None:
    """The parts that need no board."""
    check(authorization.covered(None, "app", []) == "", "no holders, nothing authorized")
    check(authorization.covered(None, "", [background_child.Holder(1, 1, 1, 1.0)]) == "", "no role, nothing authorized")

    class Broken:
        def execute(self, *_args: Any) -> Any:
            raise RuntimeError("board went away")

    with tempfile.TemporaryDirectory(prefix="s708u-") as tmp:
        proc = Proc(Path(tmp) / "proc")
        comparison(proc)
        try:
            answer = authorization.covered(Broken(), "app", [proc.holder(SWEEP)], proc_root=proc.root)
        except Exception as exc:  # the defect this check exists for
            answer = repr(exc)
        check(answer == "", f"an authorization that cannot be read is none: the alert is told as before: {answer}")
        chain = authorization._ancestry(proc.root, 2835139)
        check([pid for pid, _ticks in chain] == [2835139, SWEEP + 20, SWEEP + 10, SWEEP, PROVIDER, PANE],
              f"a PostgreSQL child's ancestry reaches the comparison's shell: {chain}")
        check(all(ticks == background_child.start_ticks(proc.root, pid) for pid, ticks in chain),
              "each with its own start tick")


def main() -> int:
    units()
    with tempfile.TemporaryDirectory(prefix="s708-") as tmp:
        scenario(Path(tmp))
    print(f"background_authorization_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
