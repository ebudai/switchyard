#!/usr/bin/env python3
"""SYRD-631: a held notice is asked again whether it is current before it is sent.

On 2026-10-18 SYRD-614 waited behind Ops's active work while Ops's pane was
held by background work. The Director blocked SYRD-614; its "changed by
director: blockers -- re-read it before continuing" notice waited an hour
behind the busy pane and was then delivered, telling Ops to continue a ticket
that was neither its active work nor workable.

On a production-built board with the real server and listener:

* the incident: the notice about a queued ticket that is blocked while the pane
  is busy is dropped, with its reason recorded, when the pane frees; serial
  focus stays on the active ticket, and the next eligible notice still goes;
* the owner of a ticket it was already handed still hears that it was blocked;
* races at the final submission boundary -- a reassignment, a terminal move and
  a blocker landing while the pane is probed, after the notice was claimed as
  current -- each drop the notice there, recorded as the pre-send recheck;
* blocker resolution: a ticket blocked before it was ever handed over reaches
  its worker once, when it is activated.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import psycopg  # noqa: E402

import ticket_board_write_api_test as fixture  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from user_review_serial_release_test import build_board, workflow  # noqa: E402

CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Gate:
    """The pane activity gate: busy panes, and something to do the next time a pane is probed as idle."""

    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.on_idle: dict[str, list] = {}

    def __call__(self, target: str) -> bool:
        if target in self.busy:
            return True
        for action in self.on_idle.pop(target, []):
            action()
        return False


class Board:
    def __init__(self, cluster, admin: str) -> None:
        self.admin = admin
        self.app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu",
                                          ticket_prefix="PGU", database_url=fixture.conninfo(
                                              cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        self.sent: list[tuple[str, str]] = []
        self.gate = Gate()
        self.missing: set[str] = set()
        self.during_send: list = []  # something to do while the pane is being sent to, once
        self.listener = TicketBoardNotifyListener(
            conninfo=self.listener_url, sender=self.send,
            activity_gate=self.gate, target_exists=lambda target: target not in self.missing,
            submission_witness=lambda target, _since: any(t == target for t, _m in self.sent),
            poll_seconds=0, project="pgu")

    def send(self, target: str, message: str) -> None:
        for action in self.during_send:
            action()
        self.during_send = []
        self.sent.append((target, message))

    def write_waits(self, write) -> tuple[threading.Thread, list[str]]:
        """Start a board write in a thread and return once it is waiting on a lock (or has finished)."""
        done: list[str] = []
        thread = threading.Thread(target=lambda: done.append(write()), daemon=True)
        thread.start()
        for _ in range(100):
            if done or self.sql("SELECT count(*) FROM pg_locks WHERE NOT granted;") != "0":
                break
            thread.join(0.05)
        return thread, done

    def act(self, ticket: str, operation: str, actor: str, **payload) -> str:
        try:
            fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - a refusal is an answer
            return f"refused: {exc}"

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def one_pass(self) -> list[tuple[str, str]]:
        """One iteration of the listener's loop, as listen_once runs it, with every held notice due now."""
        self.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                 "WHERE dead_lettered_at IS NULL;")
        before = len(self.sent)
        with psycopg.connect(self.listener_url, autocommit=True) as conn:
            self.listener.refresh_workflow(conn)
            for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                         "process_serial_focus_queue_wakeups"):
                if hasattr(self.listener, step):
                    getattr(self.listener, step)(conn)
            self.listener.process_due_notifications(conn)
        return self.sent[before:]

    def pending(self, ticket: str, role: str) -> list[str]:
        """The notices still queued for `role` about `ticket`, by kind."""
        listed = self.sql("SELECT coalesce(string_agg(kind, ',' ORDER BY id), '') FROM ticket_board.ticket_notification_queue "
                          f"WHERE ticket_id = '{ticket}' AND target_role = '{role}' AND dead_lettered_at IS NULL;")
        return listed.split(",") if listed else []

    def drops(self, ticket: str, role: str) -> list[dict]:
        """The stale-notification drops recorded for `role` about `ticket`."""
        return json.loads(self.sql(
            "SELECT coalesce(jsonb_agg(jsonb_build_object('kind', kind, 'detail', detail) ORDER BY id), '[]') "
            f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' AND target_role = '{role}' "
            "AND event = 'drop' AND busy_reason = 'stale_notification';"))

    def reservation(self, role: str) -> str:
        return self.sql("SELECT coalesce(max(ticket_id), '') FROM ticket_board.serial_reservations() "
                        f"WHERE implementer = '{role}';")


def about(batch: list[tuple[str, str]], role: str, ticket: str) -> list[str]:
    return [m for target, m in batch if target == f"pgu-{role}:0.0" and ticket in m]


def main() -> int:
    with temporary_cluster(prefix="syrd631-", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, ROOT, "board")
        board = Board(cluster, admin)
        try:
            board.app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            tickets = {"active": "PGU-613", "queued": "PGU-614", "blocker": "PGU-630", "handed": "PGU-640",
                       "reassigned": "PGU-651", "terminal": "PGU-652", "late_block": "PGU-653", "resolves": "PGU-654",
                       "other_blocker": "PGU-655", "held_then_blocked": "PGU-656",
                       "earlier_visit": "PGU-661", "ahead": "PGU-662", "never_handed": "PGU-663",
                       "next_up": "PGU-664", "main_next": "PGU-641",
                       "window": "PGU-680", "during_send": "PGU-681",
                       "busy": "PGU-682", "window_cancel": "PGU-685", "window_block": "PGU-686",
                       "between_reads": "PGU-687"}
            for ticket in tickets.values():
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             commit_exempt=True)
            board.one_pass()

            # -- the incident --------------------------------------------------------------------------------------
            A, B, C = tickets["active"], tickets["queued"], tickets["blocker"]
            check(board.act(A, "route", "director", state="in_progress", assignee="ops") == "ok", "A routed to Ops")
            check(about(board.one_pass(), "ops", A), "Ops is handed A, its active work")
            check(board.act(B, "route", "director", state="in_progress", assignee="ops") == "ok", "B routed to Ops")
            check(not about(board.one_pass(), "ops", B) and board.reservation("ops") == A,
                  "B waits behind A, which keeps Ops's serial focus")
            board.gate.busy.add("pgu-ops:0.0")  # SYRD-613's background comparison holds Ops's pane
            check(board.act(B, "set_blockers", "director", blocked_by=[C], blocked_reason="waits on PGU-630") == "ok",
                  "the Director blocks B while Ops's pane is held")
            check("ticket_update" in board.pending(B, "ops"), f"the blockers notice is queued: {board.pending(B, 'ops')}")
            check(not about(board.one_pass(), "ops", B), "and not sent while the pane is busy")
            board.gate.busy.clear()  # the hold ends
            released = board.one_pass()
            check(not about(released, "ops", B), f"no stale prompt about B reaches Ops: {about(released, 'ops', B)}")
            check(board.pending(B, "ops") == [], f"the notice is gone from the queue: {board.pending(B, 'ops')}")
            check([d["kind"] for d in board.drops(B, "ops")] == ["ticket_update"],
                  f"and durably dropped as stale, with its reason: {board.drops(B, 'ops')}")
            check(board.reservation("ops") == A, f"serial focus stays on A: {board.reservation('ops')}")
            check(board.act(A, "add_comment", "director", text="DIRECTOR: keep going on A.") == "ok", "a comment on A")
            check(about(board.one_pass(), "ops", A), "and the next eligible notice, about A, still reaches Ops")

            # -- a ticket its owner was handed still says it was blocked -------------------------------------------
            D = tickets["handed"]
            check(board.act(D, "route", "director", state="in_progress", assignee="main") == "ok", "D routed to Main")
            check(about(board.one_pass(), "main", D), "Main is handed D")
            Q = tickets["main_next"]
            check(board.act(Q, "route", "director", state="in_progress", assignee="main") == "ok"
                  and not about(board.one_pass(), "main", Q), "Q waits behind D")
            check(board.act(D, "set_blockers", "director", blocked_by=[C], blocked_reason="waits on PGU-630") == "ok",
                  "the Director blocks D, Main's active work")
            batch = board.one_pass()
            told = about(batch, "main", D)
            check(told and "blockers" in told[0] and board.reservation("main") == Q,
                  f"Main, which was working on D, hears it was blocked, though Q is now its active work: {told}")

            # -- a hand-over held behind a busy pane, then blocked, then released --------------------------------------
            G = tickets["held_then_blocked"]
            board.gate.busy.add("pgu-app:0.0")
            check(board.act(G, "route", "director", state="in_progress", assignee="app") == "ok", "G routed to App")
            check(not about(board.one_pass(), "app", G) and board.pending(G, "app") == ["transition"],
                  f"G's hand-over is held behind App's busy pane: {board.pending(G, 'app')}")
            check(board.act(G, "set_blockers", "director", blocked_by=[C], blocked_reason="waits on PGU-630") == "ok",
                  "the Director blocks G during the hold")
            board.gate.busy.clear()
            released = board.one_pass()
            handed = [m for m in about(released, "app", G) if "entered Implementation" in m]
            check(handed == [] and "transition" not in board.pending(G, "app")
                  and "transition" in [d["kind"] for d in board.drops(G, "app")],
                  f"when the hold ends the stale hand-over is dropped and recorded, not sent: {about(released, 'app', G)}")

            # -- races at the final submission boundary --------------------------------------------------------------
            def race(ticket: str, action, what: str) -> None:
                check(board.act(ticket, "route", "director", state="in_progress", assignee="app") == "ok",
                      f"{ticket} routed to App, which is free")
                check("transition" in board.pending(ticket, "app"), f"{ticket}'s hand-over is queued")
                landed: list[str] = []
                board.gate.on_idle["pgu-app:0.0"] = [lambda: landed.append(action())]  # after the claim found it current
                sent = board.one_pass()
                check(landed == ["ok"], f"{what} landed while App's pane was probed: {landed}")
                stale = [m for m in about(sent, "app", ticket) if "entered Implementation" in m]
                check(stale == [], f"{what}: App is not handed {ticket} (what it is sent instead is the new news): "
                                   f"{about(sent, 'app', ticket)}")
                dropped = [d for d in board.drops(ticket, "app") if d["kind"] == "transition"]
                check(len(dropped) == 1 and dropped[0]["detail"].get("phase") == "pre_send_recheck"
                      and "transition" not in board.pending(ticket, "app"),
                      f"{what}: dropped at the final recheck, with the reason recorded: {dropped}")
                later = [m for m in about(board.one_pass(), "app", ticket) if "entered Implementation" in m]
                check(later == [] and board.pending(ticket, "app") == [],
                      f"{what}: nothing more about {ticket} waits for App: {later} {board.pending(ticket, 'app')}")

            race(tickets["reassigned"], lambda: board.act(tickets["reassigned"], "reassign", "director", assignee="main",
                                                          reason="Main takes it."), "a reassignment")
            race(tickets["terminal"], lambda: board.act(tickets["terminal"], "cancel", "director"), "a terminal move")
            race(tickets["late_block"], lambda: board.act(tickets["late_block"], "set_blockers", "director",
                                                          blocked_by=[C], blocked_reason="late"), "a new blocker")

            # -- Audit's window: a write made after the last read, before the send ---------------------------------
            # The read and the send are one step, holding the ticket row (pgu1005): a write that
            # arrives in between waits on them, and the notice yields to it -- it commits first, and the notice is
            # then stale and dropped. App, the role it retired, is handed nothing.
            original = board.listener.eligibility.drop_if_stale
            seen: dict = {}

            def window_race(ticket: str, write, what: str, after: str) -> None:
                """`write` lands after the last read found `ticket`'s hand-over to App current, before the send."""
                check(board.act(ticket, "route", "director", state="in_progress", assignee="app") == "ok",
                      f"{ticket} routed to App")
                seen.clear()

                def after_the_read(conn, **notice):
                    stale = original(conn, **notice)
                    # The last read, holding the ticket row, has just found the hand-over current.
                    if notice.get("phase") == "held_recheck" and notice["ticket_id"] == ticket and not stale \
                            and "thread" not in seen:
                        seen["thread"], seen["done"] = board.write_waits(write)
                        seen["waiting"] = not seen["done"]
                    return stale

                board.listener.eligibility.drop_if_stale = after_the_read
                try:
                    sent = board.one_pass()
                finally:
                    board.listener.eligibility.drop_if_stale = original
                seen["thread"].join(30)
                handed = [m for m in about(sent, "app", ticket) if "entered Implementation" in m]
                check(handed == [], f"{what}: App, retired after the read found it current, is not handed {ticket}: {handed}")
                check(seen.get("waiting") is True and seen["done"] == ["ok"],
                      f"{what}: the write, made after the read, waited and then landed: {seen.get('done')}")
                check(board.sql(f"SELECT {after} FROM ticket_board.tickets WHERE id = '{ticket}';") == "t",
                      f"{what}: it is in effect")
                later = [m for m in about(board.one_pass(), "app", ticket) if "entered Implementation" in m]
                check(later == [] and "transition" not in board.pending(ticket, "app")
                      and "transition" in [d["kind"] for d in board.drops(ticket, "app")],
                      f"{what}: App's hand-over is dropped as stale, durably recorded: {board.drops(ticket, 'app')}")

            # A write that commits between the first recheck and the hold (here: from the background-work check)
            # is caught by the recheck under the hold.
            WB2 = tickets["between_reads"]
            check(board.act(WB2, "route", "director", state="in_progress", assignee="app") == "ok", f"{WB2} routed to App")
            background = board.listener._defer_for_background_work
            landed: list[str] = []

            def between(conn, notification_id, attempts, kind, role, payload, phase):
                if phase == "pre_send_recheck" and WB2 in payload and not landed:
                    landed.append(board.act(WB2, "reassign", "director", assignee="main", reason="Main takes it."))
                return background(conn, notification_id, attempts, kind, role, payload, phase)

            board.listener._defer_for_background_work = between
            try:
                sent = board.one_pass()
            finally:
                board.listener._defer_for_background_work = background
            handed = [m for m in about(sent, "app", WB2) if "entered Implementation" in m]
            check(landed == ["ok"] and handed == [] and [d["detail"].get("phase") for d in board.drops(WB2, "app")
                                                         if d["kind"] == "transition"] == ["held_recheck"],
                  f"a reassignment between the two reads is caught under the hold: {landed} {handed} {board.drops(WB2, 'app')}")

            W, WC, WB = tickets["window"], tickets["window_cancel"], tickets["window_block"]
            window_race(W, lambda: board.act(W, "reassign", "director", assignee="main", reason="Main takes it."),
                        "a reassignment", "assignee = 'main'")
            window_race(WC, lambda: board.act(WC, "cancel", "director"), "a terminal move", "state = 'cancelled'")
            window_race(WB, lambda: board.act(WB, "set_blockers", "director", blocked_by=[C], blocked_reason="late"),
                        "a blocker", "ticket_board.ticket_has_unresolved_blockers(id)")

            # A write that arrives while the pane is being sent to waits for the send: it cannot overtake it.
            V = tickets["during_send"]
            check(board.act(V, "route", "director", state="in_progress", assignee="app") == "ok", "V routed to App")

            def mid_send() -> None:
                seen["send_thread"], seen["send_done"] = board.write_waits(lambda: board.act(
                    V, "reassign", "director", assignee="main", reason="Main takes it."))
                seen["blocked_during_send"] = not seen["send_done"]

            board.during_send = [mid_send]
            sent = board.one_pass()
            seen["send_thread"].join(30)
            check(seen.get("blocked_during_send") is True and about(sent, "app", V),
                  f"a reassignment made during the send waits for it, so the hand-over App gets was true when sent: "
                  f"{about(sent, 'app', V)}")
            check(seen["send_done"] == ["ok"] and board.sql(f"SELECT assignee FROM ticket_board.tickets WHERE id = '{V}';")
                  == "main", f"and then lands, after the send: V is Main's: {seen['send_done']}")

            # A write in flight when the listener comes to its last read: the listener does not wait on it, and
            # the notice waits a pass instead, then goes once the write is done.
            U = tickets["busy"]
            check(board.act(U, "route", "director", state="in_progress", assignee="app") == "ok", "U routed to App")
            writer = psycopg.connect(board.admin)
            writer.execute("SELECT 1 FROM ticket_board.tickets WHERE id = %s FOR NO KEY UPDATE", (U,))  # as an UPDATE holds it
            release = threading.Timer(10, writer.rollback)  # were the listener to wait, it would wait this long
            release.start()
            started = time.monotonic()
            sent = board.one_pass()
            check(not about(sent, "app", U) and time.monotonic() - started < 8 and board.pending(U, "app") == ["transition"]
                  and board.sql(f"SELECT last_error FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{U}';")
                  == "board write in flight", f"the hand-over waits a pass, without blocking the listener: {about(sent, 'app', U)}")
            release.cancel()
            writer.rollback()
            writer.close()
            check(len(about(board.one_pass(), "app", U)) == 1, "and goes once the write is done")
            check(board.act(U, "cancel", "director") == "ok", "U is done with")
            board.one_pass()

            # -- blocker resolution: the worker is handed the ticket once, when it is activated ----------------------
            E, F = tickets["resolves"], tickets["other_blocker"]
            check(board.act(E, "set_blockers", "director", blocked_by=[F], blocked_reason="waits on PGU-655") == "ok",
                  "E is blocked before it is routed")
            check(board.act(E, "route", "director", state="in_progress", assignee="app") == "ok", "E routed to App, now free")
            check(not about(board.one_pass(), "app", E), "App is not handed blocked work")
            check(board.act(F, "cancel", "director") == "ok", "E's blocker is resolved")
            first = about(board.one_pass(), "app", E)
            again = about(board.one_pass(), "app", E)
            check(len(first) == 1 and again == [] and board.reservation("app") == E,
                  f"App is handed E once, as its active work: {first} {again} {board.reservation('app')}")

            # -- "handed over" means in this visit to the stage, and by its hand-over ---------------------------------
            K, M, N = tickets["earlier_visit"], tickets["ahead"], tickets["never_handed"]
            check(board.act(E, "cancel", "director") == "ok", "E is done with, so App is free again")
            board.one_pass()
            check(board.act(K, "route", "director", state="in_progress", assignee="app") == "ok", "K routed to App")
            check(about(board.one_pass(), "app", K), "App is handed K")
            check(board.act(M, "route", "director", state="in_progress", assignee="app") == "ok"
                  and not about(board.one_pass(), "app", M), "M waits behind K")
            check(board.act(K, "defer", "director") == "ok", "K is deferred")
            check(about(board.one_pass(), "app", M) and board.reservation("app") == M, "M becomes App's active work")
            check(board.act(K, "route", "director", state="analysis", assignee="director") == "ok", "K back to triage")
            back = board.act(K, "route", "director", state="in_progress", assignee="app")
            check(back == "ok", f"K is routed back to App: {back}")
            told = about(board.one_pass(), "app", K)
            check(told == [] and board.reservation("app") == M, f"K is queued behind M, not handed over again: {told}")
            check(board.act(K, "set_blockers", "director", blocked_by=[C], blocked_reason="later") == "ok", "K is blocked")
            check(not about(board.one_pass(), "app", K) and board.pending(K, "app") == [],
                  "a hand-over in an earlier visit does not count: App is not told about K")
            check(board.act(M, "cancel", "director") == "ok" and board.act(N, "route", "director", state="in_progress",
                                                                             assignee="app") == "ok", "N routed to App")
            P = tickets["next_up"]
            board.missing.add("pgu-app:0.0")
            check(not about(board.one_pass(), "app", N), "N's hand-over is dead-lettered: App's pane is missing")
            board.missing.clear()
            check(board.act(N, "add_comment", "director", text="DIRECTOR: N, when you can.") == "ok"
                  and about(board.one_pass(), "app", N), "a comment on N, App's active work, reaches App")
            check(board.act(P, "route", "director", state="in_progress", assignee="app") == "ok"
                  and not about(board.one_pass(), "app", P), "P waits behind N")
            check(board.act(N, "set_blockers", "director", blocked_by=[C], blocked_reason="blocked") == "ok", "N is blocked")
            batch = board.one_pass()
            check(not about(batch, "app", N) and board.pending(N, "app") == [] and about(batch, "app", P),
                  f"a comment is not a hand-over: App, never handed N, is not told it was blocked, and is handed P: "
                  f"{about(batch, 'app', N)} {about(batch, 'app', P)}")

            # -- an owner whose only work is blocked is still told (SYRD-148's operator wait) -------------------------
            check(board.act(P, "cancel", "director") == "ok", "P, App's active work, is cancelled; the rest is blocked")
            board.one_pass()
            check(board.reservation("app") == "", f"App holds no active work: {board.reservation('app')}")
            check(board.act(N, "add_comment", "director", text="DIRECTOR: N is all you have; it waits on PGU-630.") == "ok",
                  "the Director comments on N, App's only, blocked, work")
            check(about(board.one_pass(), "app", N), "App, with nothing else in hand, hears about N")
        finally:
            board.server.shutdown()
            board.server.server_close()
    print(f"held_notice_revalidation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
