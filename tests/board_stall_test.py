#!/usr/bin/env python3
"""SYRD-637: the Director is told once when runnable board work makes no durable progress, on real boards.

Production-built boards (schema.sql, companion roles, rbac.sql, the real
ticket-board-migrate, rbac.sql again), the real HTTP server and the real notify
listener with an injected sender, as router_role_board_test builds them; a pull
board from pull_scheduling_policy_test's document; and main's board upgraded by
this tree's migration.

Time is the evaluator's `p_now`, given explicitly: each case reads the board's
clock once (T0) and asks at T0 + minutes. Progress is recorded by the board's
own triggers at the moment it happens; where a case needs that moment to be a
simulated one, it says so and stamps the event it just caused, and nothing else.

Cases: direct assignment, the whole episode (silent before the threshold, one
digest after it, nothing again on retries or a fresh connection, delivered to
the Director alone, closed by a checkpoint, a second independent episode,
closed when nothing runnable remains, a digest whose episode closed before
delivery dropped); every exclusion (Draft, Triage, Backlog, Done, Cancelled,
blockers internal and external, a dependency, a hold, a snooze, a serial queue,
a review lane, the User's and the Director's own stages, a ticket already
escalated by its own stall episode); every counted stage (Implementation,
Documentation with a Librarian, Inspection, Audit, Publication); stopped,
providerless and busy roles as evidence, not resets; heartbeats and nudges not
progress; a pull queue; the listener's own pass; the read-only status over HTTP
and `ticket-board-read stall`; and the upgraded board.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster  # noqa: E402
from scripts.ticket_board import board_stall, read_client  # noqa: E402
from scripts.ticket_board.documentation_gate import with_documentation_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher  # noqa: E402

#: Main's commit this ticket is cut from: the upgrade case's before-tree.
BEFORE = "9af9af13120306175baefc5fbb7ad812af7a0918"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def example() -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


def full() -> dict:
    """Every counted stage the ticket names: a Librarian's documentation gate and the Publisher too."""
    return with_publisher(with_documentation_gate(example(), librarian={"runtime": "claude", "template_role": "main"}))


def short(answer: dict) -> str:
    return "ok" if "ok" in answer else "refused: " + answer.get("refused", "")[:600]


class Stall:
    """One board, its simulated clock, and the questions the cases ask of its stall state."""

    def __init__(self, board: Board):
        self.board = board
        self.t0 = board.sql("SELECT clock_timestamp()::text;")
        self.number = 300

    def at(self, minutes: float) -> str:
        return f"'{self.t0}'::timestamptz + interval '{minutes * 60} seconds'"

    def ticket(self, state: str, assignee: str, **extra) -> str:
        self.number += 1
        t = f"PGU-{self.number}"
        fixture.seed_postgres_ticket(self.board.admin, t, title=f"Fixture {t}", state=state, assignee=assignee, **extra)
        return t

    def evaluate(self, minutes: float, roles: dict | None = None, *, threshold: int = 1800) -> dict:
        """The evaluator, as the listener calls it, at T0 + minutes."""
        with psycopg.connect(self.board.listener_url, autocommit=True) as conn:
            row = conn.execute(f"SELECT ticket_board.note_board_stall(%s::jsonb, %s, {self.at(minutes)})",
                               (json.dumps(roles or {}), threshold)).fetchone()
        value = row[0]
        return json.loads(value) if isinstance(value, (str, bytes)) else value

    def state(self) -> dict:
        return json.loads(self.board.sql("SELECT ticket_board.board_stall_status(clock_timestamp())::text;"))

    def runnable(self) -> list[str]:
        return [item["ticket"] for item in self.state()["runnable"]]

    def excluded(self) -> dict:
        return self.state()["excluded"]

    def episodes(self) -> list[dict]:
        return json.loads(self.board.sql(
            "SELECT coalesce(json_agg(json_build_object('id', id, 'closed', closed_at IS NOT NULL, "
            "'reason', close_reason, 'notification', notification_id, 'tickets', tickets) ORDER BY id), '[]')::text "
            "FROM ticket_board.board_stall_episodes;"))

    def digests(self) -> int:
        """Every board-stall digest ever enqueued, as the trace remembers them (the queue forgets on delivery)."""
        return int(self.board.sql("SELECT count(*) FROM ticket_board.notification_trace "
                                  "WHERE event = 'enqueue' AND detail->>'dedupe_key' LIKE 'board_stall:%';"))

    def drain(self) -> list[tuple[str, str]]:
        """Deliver everything due, every pane finishing its turn between passes (a notice to a busy pane waits)."""
        sent: list[tuple[str, str]] = []
        for _ in range(6):
            self.board.busy.clear()
            self.board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                           "WHERE claimed_at IS NULL;")
            batch = self.board.one_pass()
            sent += batch
            if not batch:
                break
        return sent

    def stamp_progress(self, since_id: int, minutes: float) -> None:
        """The progress the case just caused happened at T0 + minutes: its events, and only those, say so."""
        self.board.sql(f"UPDATE ticket_board.board_progress_events SET at = {self.at(minutes)} WHERE id > {since_id};")

    def last_event_id(self) -> int:
        return int(self.board.sql("SELECT coalesce(max(id), 0) FROM ticket_board.board_progress_events;"))

    def settle(self, tickets: list[str]) -> None:
        """Put the case's tickets down, so the next case starts with nothing runnable."""
        for t in tickets:
            self.board.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET state = 'cancelled', "
                           f"assignee = 'unassigned' WHERE id = '{t}';")


def episode_lifecycle(stall: Stall) -> None:
    """Direct assignment: silent before the threshold, one digest after, closed by a checkpoint, then again."""
    board = stall.board
    triage = stall.ticket("analysis", "director")
    check(stall.evaluate(0)["runnable"] == 0 and triage not in stall.excluded(),
          "a ticket in Triage is not admitted work: not counted, not even as an exclusion")
    t = stall.ticket("analysis", "director")
    before_route = stall.last_event_id()
    check(short(board.act(t, "route", "director", state="in_progress", assignee="app")) == "ok", "routed to app")
    stall.drain()  # the routing's own notices, delivered as before: the hand-over is progress too
    events = board.sql(f"SELECT string_agg(event, ' | ' ORDER BY id) FROM ticket_board.board_progress_events "
                       f"WHERE id > {before_route};")
    check(f"{t} moved analysis -> in_progress" in events and f"{t} transition notice delivered to app" in events,
          f"the routing and its delivered hand-over are each recorded as progress: {events}")
    stall.stamp_progress(before_route, 0)
    check(stall.evaluate(0)["runnable"] == 1 and stall.runnable() == [t], f"{t} is runnable: {stall.runnable()}")
    check(stall.evaluate(29)["episode"] is None and stall.digests() == 0, "silent before the threshold")
    working = {"app": {"state": "working", "detail": "background work running: pytest"}}
    opened = stall.evaluate(31, working)
    check(opened["episode"] is not None and stall.digests() == 1, f"one digest at the threshold: {opened}")
    queued = json.loads(board.sql("SELECT json_build_object('target', target_role, 'ticket', ticket_id, 'message', "
                                  "message, 'payload', payload)::text FROM ticket_board.ticket_notification_queue "
                                  "WHERE dedupe_key LIKE 'board_stall:%';"))
    check(queued["target"] == "director" and queued["ticket"] == t and queued["payload"]["kind"] == "board_stall"
          and f"{t} (in_progress, app; working: background work running: pytest" in queued["message"]
          and "no durable progress for 31 minutes" in queued["message"] and "Last progress:" in queued["message"],
          f"to the Director, naming the ticket, its role, what the role is doing and the last progress: {queued}")
    for minutes in (31, 32, 45, 59):
        stall.evaluate(minutes, working)
    with psycopg.connect(board.listener_url, autocommit=True) as conn:  # a restarted listener asks again
        conn.execute(f"SELECT ticket_board.note_board_stall('{{}}'::jsonb, 1800, {stall.at(60)})")
    check(stall.digests() == 1 and len(stall.episodes()) == 1,
          "retries, later passes and a fresh connection add nothing: one episode, one digest")
    sent = [(target, message) for target, message in stall.drain() if "Board stall" in message]
    check([target for target, _ in sent] == ["pgu-director:0.0"],
          f"the digest reaches the Director, and nobody else: {sent}")
    check(not any("Board stall" in message for target, message in board.sent if target != "pgu-director:0.0"),
          "no implementer is told about the board's stall")
    # Heartbeats are not progress: a nudge delivered, the listener's own trace, the episode's own digest.
    before = stall.last_event_id()
    board.sql(f"INSERT INTO ticket_board.notification_trace (ticket_id, target_role, kind, event) "
              f"VALUES ('{t}', 'app', 'nudge', 'ack'), ('{t}', 'app', 'idle_reminder', 'ack');")
    board.sql(f"UPDATE ticket_board.ticket_notification_state SET last_activity_at = clock_timestamp() "
              f"WHERE ticket_id = '{t}';")
    check(stall.last_event_id() == before, "nudges and reminders delivered, and activity touches, record no progress")
    # The owner's own checkpoint is progress: the episode closes, and a new clock starts from it.
    check(short(board.act(t, "add_comment", "app", text="tests pass locally; pushing next")) == "ok", "checkpoint")
    check(stall.last_event_id() > before, "the owner's comment is recorded as progress")
    stall.stamp_progress(before, 62)
    stall.evaluate(63)
    check(stall.episodes()[-1]["closed"] and stall.episodes()[-1]["reason"] == "progress", stall.episodes())
    check(stall.evaluate(91)["episode"] is None and stall.digests() == 1, "the new clock runs from the checkpoint")
    second = stall.evaluate(93)
    check(second["episode"] is not None and second["episode"] != opened["episode"] and stall.digests() == 2,
          f"a second, independent stall is a second episode and a second digest: {second}")
    for minute in range(94, 154):  # an hour of passes with nothing moving: no storm
        stall.evaluate(minute + 0.5, working)
    check(stall.digests() == 2 and len(stall.episodes()) == 2, "an hour of passes adds no digest and no episode")
    # Closed before delivery: the queued digest is dropped, not delivered late.
    board.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET state = 'backlog', "
              f"assignee = 'unassigned', parked = true WHERE id = '{t}';")
    stall.evaluate(155)
    check(stall.episodes()[-1]["reason"] == "no_runnable_work" and stall.state()["runnable_since"] is None,
          f"nothing runnable closes the episode and stops the clock: {stall.episodes()[-1]}")
    late = [m for _, m in stall.drain() if "Board stall" in m]
    check(late == [] and board.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                   "WHERE dedupe_key LIKE 'board_stall:%';") == "0",
          f"a digest whose episode closed before delivery is dropped: {late}")
    stall.settle([t, triage])


def exclusions(stall: Stall) -> None:
    """Each kind of ticket that is not runnable now, and why the status says it is not."""
    board = stall.board
    uncounted = [stall.ticket(state, assignee) for state, assignee in (
        ("draft", "director"), ("analysis", "director"), ("backlog", "unassigned"), ("done", "unassigned"),
        ("cancelled", "unassigned"))]
    active = stall.ticket("in_progress", "main")
    queued = stall.ticket("in_progress", "main")
    blocked = stall.ticket("in_progress", "app")
    check(short(board.act(blocked, "set_blockers", "director", blocked_by=[active], blocked_reason="needs it")) == "ok",
          "blocked on another ticket")
    external = stall.ticket("in_progress", "ops")
    check(short(board.act(external, "set_blockers", "director", blocked_by=["operator:release_manager"],
                          blocked_reason="a person")) == "ok", "blocked on a person")
    waiting = stall.ticket("audit", "audit")
    check(short(board.act(waiting, "request_dependency", "audit", role="director", reason="need a decision")) == "ok",
          "waiting on a dependency")
    held = stall.ticket("inspection", "inspector")
    check(short(board.act(held, "set_manually_controlled", "director", manually_controlled=True)) == "ok", "held")
    snoozed = stall.ticket("documentation", "librarian")
    check(board.as_role_sql("director", f"SELECT ticket_board.snooze_reminders(ARRAY['{snoozed}'], clock_timestamp() "
                                        "+ interval '1 day', 'quiet please', true);") == {"ok": True}, "snoozed")
    lane_first = stall.ticket("publication", "publisher")
    users = stall.ticket("user_review", "user")
    directors = stall.ticket("director_review", "director")
    escalated = stall.ticket("in_progress", "app")
    prompted = stall.ticket("audit", "audit")
    for ticket, owner, state, phase in ((escalated, "app", "in_progress", "director"), (prompted, "audit", "audit", "role")):
        board.sql("INSERT INTO ticket_board.role_stall_episodes (ticket_id, owner_role, ticket_state, ticket_assignee, "
                  f"round_at, phase, phase_no, phase_started_at, max_attempts, opened_at) VALUES ('{ticket}', '{owner}', "
                  f"'{state}', '{owner}', clock_timestamp(), '{phase}', 1, clock_timestamp(), 3, clock_timestamp());")
    stall.evaluate(0)
    runnable, excluded = stall.runnable(), stall.excluded()
    check(not set(uncounted) & (set(runnable) | set(excluded)),
          f"Draft, Triage, Backlog, Done and Cancelled are never counted: {runnable} {sorted(excluded)}")
    expect = {
        queued: "waiting behind main's current " + active,
        blocked: "blocked by " + active,
        external: "blocked by operator:release_manager",
        waiting: "waiting on director",
        held: "held by the Director",
        snoozed: "its reminders are snoozed",
        users: "its next action is the User's",
        directors: "its next action is the Director's",
        escalated: "its own stall is already with the Director",
        prompted: "its owner is being prompted about its own stall",
    }
    check({t: excluded.get(t) for t in expect} == expect, f"each exclusion, with its reason: {excluded}")
    check(set(runnable) == {active, lane_first},
          f"only what is runnable now counts, Publication included: {runnable}")
    stall.settle(uncounted + [active, queued, blocked, external, waiting, held, snoozed, lane_first, users,
                              directors, escalated, prompted])
    stall.evaluate(1)


def counted_stages(stall: Stall) -> None:
    """Every admitted stage counts, with its owner responsible; a review lane counts its current ticket only."""
    tickets = {
        "in_progress": stall.ticket("in_progress", "ops"),
        "documentation": stall.ticket("documentation", "librarian"),
        "inspection": stall.ticket("inspection", "inspector"),
        "audit": stall.ticket("audit", "audit"),
        "publication": stall.ticket("publication", "publisher"),
    }
    behind = stall.ticket("audit", "audit")
    roles = {"ops": {"state": "no registered provider process", "detail": ""},
             "librarian": {"state": "registered provider process is gone", "detail": ""},
             "inspector": {"state": "stopped on a prompt", "detail": ""},
             "audit": {"state": "working", "detail": ""},
             "publisher": {"state": "system actor", "detail": "no pane; run by the board's listener"}}
    t0 = stall.last_event_id()
    stall.stamp_progress(t0 - 20, 100)
    stall.evaluate(100, roles)
    state = stall.state()
    seen = {item["ticket"]: (item["state"], item["role"], item["evidence"]) for item in state["runnable"]}
    check(seen == {tickets["in_progress"]: ("in_progress", "ops", "no registered provider process"),
                   tickets["documentation"]: ("documentation", "librarian", "registered provider process is gone"),
                   tickets["inspection"]: ("inspection", "inspector", "stopped on a prompt"),
                   tickets["audit"]: ("audit", "audit", "working"),
                   tickets["publication"]: ("publication", "publisher",
                                            "system actor: no pane; run by the board's listener")},
          f"each stage's owner is responsible, and a stopped or providerless role is reported, not skipped: {seen}")
    check(state["excluded"].get(behind) == f"waiting behind audit's current {tickets['audit']}",
          f"the second Audit ticket waits in the reviewer's lane: {state['excluded']}")
    opened = stall.evaluate(131, roles)
    check(opened["episode"] is not None, "stopped, gone, blocked and busy roles do not hold the alert back")
    digest = json.loads(stall.board.sql("SELECT message::json::text FROM (SELECT to_json(message) AS message FROM "
                                        "ticket_board.ticket_notification_queue WHERE dedupe_key = "
                                        f"'board_stall:{opened['episode']}') q;"))
    check(all(t in digest for t in tickets.values()) and "no registered provider process" in digest
          and "stopped on a prompt" in digest and behind not in digest.split("Runnable:", 1)[1],
          f"the digest lists every runnable ticket with its evidence, and nothing that is not runnable: {digest}")
    stall.settle(list(tickets.values()) + [behind])
    stall.evaluate(132)


def pull_queue(cluster) -> None:
    """A pull policy's ready stage: runnable while a claimant may claim it, the claimants responsible."""
    doc = pull_document()
    doc["project"] = "pgu"
    for role in doc["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    board = Board(cluster, "pullstall", doc)
    try:
        stall = Stall(board)
        ready = stall.ticket("ready", "unassigned")
        pinned_blocked = stall.ticket("ready", "unassigned")
        check(short(board.act(pinned_blocked, "set_blockers", "director", blocked_by=[ready],
                              blocked_reason="after it")) == "ok", "blocked")
        # Audit's reproduction: each claimant in a different state, and the board's own episodes for two of them.
        roles = {"app": {"state": "no registered provider process", "detail": ""},
                 "main": {"state": "working", "detail": "background work running: pytest"},
                 "ops": {"state": "idle", "detail": ""}}
        board.sql("INSERT INTO ticket_board.provider_capacity_episodes (target_role, ticket_id, ticket_state, "
                  f"ticket_assignee, condition, opened_at) VALUES ('ops', '{ready}', 'ready', 'unassigned', "
                  f"'Selected model is at capacity', {stall.at(-5)});")
        board.sql("INSERT INTO ticket_board.blocked_pane_episodes (target_role, blocked_since, source, classification, "
                  f"opened_at, last_seen_at) VALUES ('main', {stall.at(-3)}, 'hook', 'permission_prompt', "
                  f"{stall.at(-3)}, {stall.at(-1)});")
        stall.evaluate(0, roles)
        state = stall.state()
        runnable = {item["ticket"]: item for item in state["runnable"]}
        check(set(runnable) == {ready} and stall.excluded().get(pinned_blocked) == "no claimant may claim it",
              f"admitted work waits for the claimants; blocked work nobody may claim: {runnable} {stall.excluded()}")
        claimants = runnable[ready]["responsible"]
        check([c["role"] for c in claimants] == ["app", "main", "ops"]
              and claimants[0]["evidence"] == "no registered provider process"
              and claimants[1]["evidence"].startswith("working: background work running: pytest; pane blocked "
                                                      "(permission_prompt) since ")
              and claimants[2]["evidence"].startswith("idle; provider at capacity since "),
              f"the status keeps every claimant, each with its own runtime, pane and capacity evidence: {claimants}")
        check("claimants" not in runnable[ready]["evidence"], f"never the generic word: {runnable[ready]['evidence']}")
        opened = stall.evaluate(31, roles)
        check(opened["episode"] is not None, "unclaimed ready work stalls like any other")
        digest = board.sql(f"SELECT message FROM ticket_board.ticket_notification_queue WHERE dedupe_key = "
                           f"'board_stall:{opened['episode']}';")
        check("app: no registered provider process | main: working: background work running: pytest; pane blocked "
              "(permission_prompt) since" in digest and "| ops: idle; provider at capacity since" in digest,
              f"the Director's digest tells each claimant apart: {digest}")
    finally:
        board.close()


def listener_pass(stall: Stall) -> None:
    """The listener's own pass reports roles and evaluates, at the listener's time, with the policy's threshold."""
    board = stall.board
    t = stall.ticket("in_progress", "main")
    listener = board.listener
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        listener.refresh_workflow(conn)
        result = board_stall.run(listener, conn)
    state = stall.state()
    check(result is not None and result["runnable"] >= 1 and state["evaluated_at"] is not None
          and state["threshold_seconds"] == 1800,
          f"the pass evaluated, under the default 30 minutes: {result} {state['threshold_seconds']}")
    # And inside the listener's own loop, as the unit runs it: the evaluation moves on.
    before = state["evaluated_at"]
    board.busy.clear()
    listener.listen_once(max_notifications=listener.delivered_count + 20)
    after = stall.state()["evaluated_at"]
    check(after is not None and after != before, f"the listener's loop evaluates on its own: {before} -> {after}")
    item = next(i for i in state["runnable"] if i["ticket"] == t)
    check(item["evidence"] == "unknown: the listener has no activity gate",
          f"what the listener could not see is said so: {item}")
    check(board_stall.threshold_seconds({}) == 1800 and board_stall.threshold_seconds({board_stall.THRESHOLD_ENV: "5"})
          == 60 and board_stall.threshold_seconds({board_stall.THRESHOLD_ENV: "999999"}) == 86400
          and board_stall.threshold_seconds({board_stall.THRESHOLD_ENV: "900"}) == 900
          and board_stall.threshold_seconds({board_stall.THRESHOLD_ENV: "soon"}) == 1800,
          "one bounded policy seam, 30 minutes by default")

    class Gate:
        role_targets: dict = {}
        role_identities: dict = {}

    class Fake:
        workflow = listener.workflow
        activity_gate = type("G", (), {"is_working": lambda self, target: False})().is_working

    Fake.activity_gate.__self__.role_targets = {}
    Fake.activity_gate.__self__.role_identities = {}
    seen = board_stall.role_evidence(Fake)
    check(seen.get("main") == {"state": "no registered provider process", "detail": ""}
          and seen.get("publisher", {}).get("state") == "system actor" and "director" not in seen and "user" not in seen,
          f"pull pickup's own classifier reports each role: {seen}")
    stall.settle([t])
    stall.evaluate(200)


def status_surfaces(stall: Stall) -> None:
    """GET /api/board-stall and `ticket-board-read stall` say why the alert did or did not fire."""
    t = stall.ticket("in_progress", "ops")
    stall.evaluate(300)
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{stall.board.base}/api/board-stall") as response:
            state = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        state = {"http_error": exc.code}
    check("http_error" not in state, f"GET /api/board-stall is served: {state.get('http_error')}")
    check(state["available"] and [i["ticket"] for i in state["runnable"]] == [t] and state["fires_at"]
          and state["threshold_seconds"] == 1800 and "last_progress" in state,
          f"the board serves the state read-only: {sorted(state)}")
    out = io.StringIO()
    with redirect_stdout(out):
        code = read_client.main(["--board-url", stall.board.base, "stall"])
    text = out.getvalue()
    check(code == 0 and f"{t} in_progress -> ops" in text and "fires at" in text and "open episode: none" in text,
          f"ticket-board-read stall: {text}")
    stall.settle([t])
    stall.evaluate(301)


def upgraded(cluster, root: Path) -> None:
    """Main's board, upgraded by this tree's migration, alerts like a fresh one."""
    before = root / "before"
    before.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", BEFORE, "scripts"], check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(before)], input=archive.stdout, check=True)
    check(not (before / "scripts/ticket_board/migrations/pgu1006_syrd637_board_stall.sql").exists(),
          "the before-tree has no stall alert")
    board = Board(cluster, "stallup", example(), before=before)
    try:
        stall = Stall(board)
        t = stall.ticket("in_progress", "app")
        stall.evaluate(0)
        check(stall.evaluate(31)["episode"] is not None and stall.digests() == 1 and stall.runnable() == [t],
              "the upgraded board opens the episode and enqueues its digest")
    finally:
        board.close()


def mirrors() -> None:
    """schema.sql carries pgu1006's text byte for byte, and rbac.sql its grants: fresh and upgraded boards agree."""
    migration = (ROOT / "scripts/ticket_board/migrations/pgu1006_syrd637_board_stall.sql").read_text()
    body = migration.split("BEGIN;\n", 1)[1].split("-- schema.sql stops here", 1)[0].rstrip("\n") + "\n"
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    start = "-- SYRD-637: the board stall alert (identical to migrations/pgu1006_syrd637_board_stall.sql).\n"
    copy_ = schema.split(start, 1)[1].split("-- End of SYRD-637's board stall alert.\n", 1)[0] if start in schema else ""
    check(copy_ == body, "schema.sql carries pgu1006 byte for byte")
    rbac = (ROOT / "scripts/ticket_board/rbac.sql").read_text()
    grants = [line for line in migration.split("-- schema.sql stops here", 1)[1].splitlines() if line.startswith("GRANT")]
    check(grants and all(line in rbac for line in grants), "and rbac.sql grants what the migration grants")


def chosen(name: str) -> bool:
    """Every case, or those SYRD637_CASES names (comma-separated): for a quick rerun, never for the record."""
    only = os.environ.get("SYRD637_CASES", "").strip()
    return not only or name in only.split(",")


def main() -> int:
    mirrors()
    with tempfile.TemporaryDirectory(prefix="syrd637-") as tmp, temporary_cluster(prefix="syrd637-pg-") as cluster:
        board = Board(cluster, "stall", full())
        try:
            stall = Stall(board)
            for case in (episode_lifecycle, exclusions, counted_stages, listener_pass, status_surfaces):
                if chosen(case.__name__):
                    case(stall)
        finally:
            board.close()
        if chosen("pull_queue"):
            pull_queue(cluster)
        if chosen("upgraded"):
            upgraded(cluster, Path(tmp))
    print(f"board_stall_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
