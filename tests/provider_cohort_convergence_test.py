#!/usr/bin/env python3
"""SYRD-628: a mixed pool converges on its cohort targets one drained worker at a time.

`switchyard worker-pool <project> converge` brings each provider cohort's
running count to its target by stopping idle workers and starting stopped ones
-- never by switching a worker's runtime -- and records every change on the
board. This drives it pass by pass over a declared bench of twelve (MEFP
declares more workers than it runs), eight running and started imbalanced:
two Claude, six Codex, all busy. The board is production-built and real, and
so are the drains, claims and records; the fleet of sessions is simulated, and
it fails the test if a stop ever lands on a worker that is in a turn or holds a
ticket.

Cases: everything busy (nothing moves, the pool stays at eight); one-at-a-time
drain to Claude 4 / Codex 4, skipping a worker off its cohort's pins; a lost
provider process restarted; exhaustion holding a cohort's starts and recovery
releasing them; a claim racing a stop, caught by the drain; a dry run; repeated
deferrals recorded once; and the plan's own rules.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ticket_board_write_api_test as fixture  # noqa: E402
from provider_cohort_claims_test import build  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from scripts import team_launcher, worker_cohorts, worker_pool  # noqa: E402
from scripts.ticket_board import workflow_config  # noqa: E402
from scripts.ticket_board.cohort_client import CohortWriter  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
POOL = {"name": "luna", "runtime": "claude", "size": 12, "kind": "implementer", "ephemeral": True}
CLAUDE = [f"luna-{n}" for n in range(1, 7)]
CODEX = [f"luna-{n}" for n in range(7, 13)]
COHORTS = [{"runtime": "claude", "target": 4, "ceiling": 4, "model": "claude-sonnet-5-5", "effort": "high"},
           {"runtime": "codex", "target": 4, "ceiling": 4, "model": "gpt-5.6-luna", "effort": "xhigh"}]


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def bench_document() -> dict:
    doc = pull_document(cohorts=COHORTS)
    doc["project"] = "pgu"
    for role in doc["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        if role["name"] in ("main", "app", "ops"):
            role["ephemeral"] = False
    doc, _ = worker_pool.expand_pool(doc, team_launcher.parse_worker_pool(POOL), project="pgu")
    next(t for t in doc["transitions"] if t["action"] == "claim")["actors"] = ["luna-1"]
    for role in doc["roles"]:
        if role["name"] in CODEX:
            role["runtime"] = "codex"
    return workflow_config.validate(doc)


class Fleet:
    """The sessions: which run and which are in a turn. A stop that would interrupt anything is recorded."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.running = set(CLAUDE[:2] + CODEX)
        self.busy: set[str] = set()
        self.violations: list[str] = []
        self.models = {r: ("claude-sonnet-5-5", "high") for r in CLAUDE} | {r: ("gpt-5.6-luna", "xhigh") for r in CODEX}
        self.models["luna-3"] = ("claude-sonnet-5-5", "medium")  # off its cohort's pins: never started by convergence

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def readiness(self) -> list:
        slots = self.get("/api/reservations")["reservations"]
        return [worker_pool.WorkerReadiness(
            role=r, target=f"pgu-{r}:0.0", declared=True, routed=("in_progress",), onboarding=True, skill="present",
            runtime="authenticated", account=True, worktree=True, trusted=True, session=r in self.running,
            reserved_by=(slots.get(r) or {}).get("ticket") or "", reservation_known=True, claimant=True)
            for r in CLAUDE + CODEX]

    def busy_of(self, role: str) -> bool:
        return role in self.busy

    def stop(self, role: str):
        held = (self.get("/api/reservations")["reservations"].get(role) or {}).get("ticket")
        if role in self.busy or held:
            self.violations.append(f"{role} stopped while {'busy' if role in self.busy else 'holding ' + held}")
        self.running.discard(role)
        return worker_pool.WorkerAction(role, "stopped", "session gone")

    def start(self, role: str):
        self.running.add(role)
        return worker_pool.WorkerAction(role, "started", "observed live")


class Rig:
    def __init__(self, cluster) -> None:
        self.admin = build(cluster, "converge", "fresh")
        self.app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                          database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "converge",
                                                                        fixture.SERVICE_ROLE))
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.document = bench_document()
        self.app.apply_workflow(self.document, expected_revision=0, dry_run=False, caller_role="director")
        self.fleet = Fleet(self.base)
        self.writer = CohortWriter(self.base, caller_role="director", write_token=self.server.write_token)
        self.config = SimpleNamespace(project="pgu", board_url=self.base,
                                      roles=[SimpleNamespace(role=r, model=m, effort=e) for r, (m, e) in self.fleet.models.items()])
        self.pool = SimpleNamespace(name="luna")
        self.lines: list[str] = []

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def sql(self, statement: str) -> str:
        return fixture.psql(self.admin, statement).strip()

    def op(self, operation: str, actor: str, expect: int = 200, **payload):
        return fixture.post_json(self.base, f"/api/tickets/actions/{operation}", payload, caller=actor, expect=expect)

    def act(self, ticket: str, operation: str, actor: str, **payload):
        return fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

    def admit(self, *tickets: str) -> None:
        for ticket in tickets:
            fixture.seed_postgres_ticket(self.admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True)
            self.act(ticket, "admit", "director")

    def release(self, ticket: str, author: str) -> None:
        self.act(ticket, "submit_to_inspection", author)
        if self.sql(f"SELECT state FROM ticket_board.tickets WHERE id='{ticket}';") == "inspection":
            self.act(ticket, "inspector_sign_off", "inspector")
        self.act(ticket, "audit_sign_off", "audit", text="Audited.")

    def converge(self, *, dry_run: bool = False, watch: bool = False, passes: int | None = None,
                 writer=None, action: str = "converge") -> int:
        self.lines.clear()
        return worker_cohorts.run_command(
            self.config, self.pool, document=self.document, action=action, config_path=None, dry_run=dry_run,
            watch=watch, passes=passes, read_readiness=self.fleet.readiness, read_snapshot=lambda: self.fleet.get("/api/board"),
            read_status=lambda: self.fleet.get("/api/cohorts"), busy_of=self.fleet.busy_of, writer=writer or self.writer,
            start=self.fleet.start, stop=self.fleet.stop, sleep=lambda _s: None, print_func=self.lines.append)

    def events(self, since: int = 0) -> list[str]:
        rows = self.sql("SELECT coalesce(string_agg(runtime || ':' || role || ':' || change || ':' || source || "
                        f"coalesce(':' || (detail->>'phase'), ''), ',' ORDER BY id), '') FROM ticket_board.cohort_events WHERE id > {since};")
        return rows.split(",") if rows else []

    def last_event(self) -> int:
        return int(self.sql("SELECT coalesce(max(id), 0) FROM ticket_board.cohort_events;"))

    def counts(self) -> tuple[int, int]:
        running = self.fleet.running
        return len(running & set(CLAUDE)), len(running & set(CODEX))


def test_a_busy_imbalanced_pool_drains_to_its_targets_one_worker_at_a_time() -> None:
    with temporary_cluster(prefix="syrd628-converge.", shutdown="immediate") as cluster:
        rig = Rig(cluster)
        try:
            # Imbalanced and all busy: both Claude workers and four Codex workers hold tickets (the Codex
            # ceiling), and the other two Codex workers are in turns.
            rig.admit(*[f"PGU-{n}" for n in range(1, 9)])
            held = {}
            for role in CLAUDE[:2] + CODEX[:4]:
                held[role] = rig.op("claim_next", role)["ticket"]
            rig.fleet.busy = set(CLAUDE[:2] + CODEX)
            mark = rig.last_event()
            check(rig.converge() == 0 and rig.counts() == (2, 6) and not rig.fleet.violations,
                  f"all busy: nothing stops and nothing starts -- the pool stays at eight: {rig.counts()} {rig.lines}")
            check(any("claude: target 4, ceiling 4 (now 4), holding 2; pinned model=claude-sonnet-5-5 effort=high" in l for l in rig.lines)
                  and any("live 2, busy 2, idle 0; deficit 2" in l for l in rig.lines)
                  and any("live 6, busy 6, idle 0; excess 2" in l for l in rig.lines)
                  and any("luna-3: stopped, off-pin: effort medium, the cohort wants high" in l for l in rig.lines)
                  and any("the pool runs 8 of the 8 its targets add up to" in l for l in rig.lines),
                  f"status says live, busy, idle, deficit and excess, the off-pin worker and why nothing starts: {rig.lines}")
            deferred = rig.events(mark)
            check(deferred == [f"codex:{r}:deferred:operator" for r in CODEX] + ["claude:luna-3:deferred:operator"],
                  f"each busy Codex worker in the excess, and the off-pin Claude one, is recorded as deferred: {deferred}")

            # Drain 1: a Codex worker's turn ends with nothing held. One pass: it stops, and a Claude worker on
            # the cohort's pins starts in its place (not luna-3, which is off them).
            rig.fleet.busy.discard("luna-12")
            mark = rig.last_event()
            rig.converge()
            check(rig.counts() == (3, 5) and "luna-12" not in rig.fleet.running and "luna-4" in rig.fleet.running
                  and "luna-3" not in rig.fleet.running,
                  f"drain 1: luna-12 stops and luna-4 starts: {sorted(rig.fleet.running)}")
            moved = [e for e in rig.events(mark) if ":deferred:" not in e]
            check(moved == ["codex:luna-12:stop:operator:intended", "codex:luna-12:stop:operator:done",
                            "claude:luna-4:start:operator:intended", "claude:luna-4:start:operator:done"],
                  f"each move is recorded before and after it happens: {moved}")
            check(rig.sql("SELECT count(*) FROM ticket_board.cohort_drains;") == "0",
                  "the drain that guarded the stop is ended")

            # A Codex worker that holds a ticket finishes it: Audit releases it, and it too drains.
            rig.release(held["luna-7"], "luna-7")
            rig.fleet.busy.discard("luna-7")
            rig.converge()
            check(rig.counts() == (4, 4) and "luna-7" not in rig.fleet.running and "luna-5" in rig.fleet.running,
                  f"drain 2: the released worker stops and the next Claude worker starts: {sorted(rig.fleet.running)}")
            rig.fleet.busy.discard("luna-11")
            mark = rig.last_event()
            rig.converge()
            check(rig.counts() == (4, 4) and rig.events(mark) == [] and any("nothing to do" in l for l in rig.lines),
                  f"on target, a pass changes and records nothing: {rig.events(mark)} {rig.lines[-2:]}")

            # A provider process is lost (or the launcher restarted without it): the next pass restarts a worker.
            rig.fleet.running.discard("luna-5")
            rig.converge()
            check(rig.counts() == (4, 4) and "luna-5" in rig.fleet.running,
                  f"a lost worker is started again on the next pass: {sorted(rig.fleet.running)}")

            # Exhaustion: Codex at ceiling 0, and a Codex worker lost. Its cohort is under target, but nothing is
            # started in it; recovery releases the start.
            rig.op("set_cohort_quota", "director", runtime="codex", ceiling=0, reason="Codex usage exhausted")
            rig.fleet.running.discard("luna-10")
            rig.converge()
            check(rig.counts() == (4, 3) and any("codex is 1 under target but exhausted" in l for l in rig.lines),
                  f"exhausted: the Codex gap is not filled: {rig.counts()} {rig.lines}")
            rig.op("clear_cohort_quota", "director", runtime="codex", reason="window reset")
            rig.converge()
            check(rig.counts() == (4, 4), f"recovered: the Codex gap is filled on the next pass: {sorted(rig.fleet.running)}")

            claims = rig.sql("SELECT count(*) || '/' || count(DISTINCT ticket_id) FROM ticket_board.pull_claims;")
            check(not rig.fleet.violations and claims.split("/")[0] == claims.split("/")[1],
                  f"no stop ever landed on a busy or holding worker, and no ticket was claimed twice: "
                  f"{rig.fleet.violations} {claims}")
        finally:
            rig.close()


def test_a_claim_racing_a_stop_is_caught_by_the_drain() -> None:
    with temporary_cluster(prefix="syrd628-drain.", shutdown="immediate") as cluster:
        rig = Rig(cluster)
        try:
            rig.admit("PGU-1", "PGU-2")
            # The drain itself: claims for a draining worker are refused, and allowed again once it ends.
            rig.writer.begin_cohort_drain("luna-12", reason="probe")
            refused = rig.op("claim_next", "luna-12")
            rig.writer.end_cohort_drain("luna-12")
            allowed = rig.op("claim_next", "luna-12")
            check(refused.get("reason") == "ceiling" and "luna-12 is being stopped by convergence" in refused.get("detail", "")
                  and allowed.get("claimed") is True,
                  f"while draining, no claim lands on it; after, it claims again: {refused} {allowed}")
            rig.release(allowed["ticket"], "luna-12")
            # Codex runs six, two over target, all idle. Between the plan and the stop, luna-12 claims a
            # ticket: the drain's own read sees it, and nothing is stopped.
            rig.fleet.busy = set()
            rig.fleet.running = set(CLAUDE[:4] + CODEX)

            class Racing(CohortWriter):
                def begin_cohort_drain(self, role, **kwargs):
                    if role == "luna-12":
                        rig.op("claim_next", "luna-12")
                    return super().begin_cohort_drain(role, **kwargs)

            racer = Racing(rig.base, caller_role="director", write_token=rig.server.write_token)
            mark = rig.last_event()
            rig.converge(writer=racer)
            check("luna-12" in rig.fleet.running and not rig.fleet.violations
                  and any(e.startswith("codex:luna-12:deferred") for e in rig.events(mark))
                  and any("kept luna-12: it took PGU-" in l for l in rig.lines),
                  f"the worker that took work is kept and the deferral recorded: {rig.events(mark)} {rig.lines}")
            check(rig.sql("SELECT count(*) FROM ticket_board.cohort_drains;") == "0", "the drain is ended either way")
        finally:
            rig.close()


def test_a_replacement_start_waits_for_the_stop_it_replaces() -> None:
    """Right-sized and imbalanced (Claude 2/4, Codex 6/4, all idle): the start fills only what the stop frees."""
    with temporary_cluster(prefix="syrd628-replace.", shutdown="immediate") as cluster:
        rig = Rig(cluster)
        try:
            rig.admit("PGU-1")
            rig.fleet.busy = set()
            cohorts, _ = worker_cohorts.gather(
                rig.document, board_status=rig.fleet.get("/api/cohorts"), readiness=rig.fleet.readiness(),
                models=dict(rig.fleet.models), orders={r: int(r.split("-")[1]) for r in CLAUDE + CODEX},
                busy_of=rig.fleet.busy_of, pinned={})
            planned = worker_cohorts.plan(cohorts)
            check([(m.kind, m.role, m.after_stop) for m in planned if m.kind != "deferred"]
                  == [("stop", "luna-12", ""), ("start", "luna-4", "luna-12")],
                  f"the plan says the start replaces the stop: {planned}")

            # The stop is raced: luna-12 claims a ticket just before the drain. Nothing stops, so nothing starts.
            class Racing(CohortWriter):
                def begin_cohort_drain(self, role, **kwargs):
                    if role == "luna-12":
                        rig.op("claim_next", "luna-12")
                    return super().begin_cohort_drain(role, **kwargs)

            mark = rig.last_event()
            rig.converge(writer=Racing(rig.base, caller_role="director", write_token=rig.server.write_token))
            events = rig.events(mark)
            check(rig.counts() == (2, 6) and "codex:luna-12:stop:operator:intended" in events
                  and "claude:luna-4:deferred:operator" in events
                  and not any(e.startswith("claude:luna-4:start") for e in events)
                  and any("kept luna-4 stopped: not started: it was to replace luna-12, which was not stopped" in l
                          for l in rig.lines),
                  f"a raced drain: no stop, no start, and the start's deferral recorded -- still eight: {rig.counts()} {events}")

            # The stop fails. Again nothing starts in its place.
            rig.fleet.busy = {"luna-12"}  # luna-12 now holds PGU-1; luna-11 is the idle one
            failing = rig.fleet.stop

            def stop_fails(role):
                return worker_pool.WorkerAction(role, "failed to stop", "tmux kill-session exited 1")

            rig.fleet.stop = stop_fails
            mark = rig.last_event()
            rig.converge()
            events = rig.events(mark)
            rig.fleet.stop = failing
            check(rig.counts() == (2, 6) and "codex:luna-11:stop_failed:operator:failed" in events
                  and "claude:luna-4:deferred:operator" in events
                  and not any(e.startswith("claude:luna-4:start") for e in events),
                  f"a failed stop: nothing starts in its place -- still eight: {rig.counts()} {events}")

            # Under its size, a start needs no stop: a lost worker is replaced even while nothing can stop.
            rig.fleet.busy = set(CODEX)
            rig.fleet.running.discard("luna-2")
            rig.converge()
            check(rig.counts() == (2, 6) and "luna-2" in rig.fleet.running,
                  f"seven of eight: the start does not wait for a stop: {sorted(rig.fleet.running)}")
        finally:
            rig.close()


def test_a_dry_run_changes_nothing_and_watch_records_a_deferral_once() -> None:
    with temporary_cluster(prefix="syrd628-dry.", shutdown="immediate") as cluster:
        rig = Rig(cluster)
        try:
            rig.fleet.busy = set(CLAUDE[:2] + CODEX)
            rig.fleet.busy.discard("luna-12")
            mark = rig.last_event()
            rig.converge(dry_run=True)
            check(rig.counts() == (2, 6) and rig.events(mark) == []
                  and any(l.strip().startswith("would stop  luna-12 (codex)") for l in rig.lines)
                  and any(l.strip().startswith("would start luna-4 (claude)") for l in rig.lines)
                  and any("defer luna-11 (codex): a turn is in progress" in l for l in rig.lines)
                  and rig.lines[-1].startswith("switchyard: dry run"),
                  f"the dry run shows the plan and who is deferred because busy, and changes nothing: {rig.lines}")
            rig.fleet.busy.add("luna-12")
            rig.converge(watch=True, passes=3)
            deferred = [e for e in rig.events(mark) if ":deferred:" in e]
            check(len(deferred) == 7 and all(e.endswith(":auto") for e in deferred),
                  f"three automatic passes over an unchanged pool record each deferral once: {deferred}")
        finally:
            rig.close()


def test_the_plan_keeps_its_own_rules() -> None:
    def worker(role, runtime, order, **kw):
        base = dict(running=True, busy=False, held_known=True)
        base.update(kw)
        return worker_cohorts.Worker(role=role, runtime=runtime, order=order, **base)

    pinned_idle = worker("c-1", "codex", 1, pinned=("T-9",))
    unknown = worker("c-2", "codex", 2, held_known=False)
    prompt = worker("c-3", "codex", 3, stopped_at="trust this folder?")
    codex = worker_cohorts.Cohort(runtime="codex", target=0, effective_ceiling=4, workers=(pinned_idle, unknown, prompt))
    moves = worker_cohorts.plan([codex])
    check([m.kind for m in moves] == ["deferred"] * 3
          and [m.reason for m in moves] == ["rework is pinned to it: T-9", "the board did not report its serial slot",
                                             "stopped on a prompt: trust this folder?"],
          f"never stopped: pinned rework, an unreported slot, a worker on a prompt: {moves}")
    stopped_pinned = worker("a-2", "claude", 2, running=False, pinned=("T-4",))
    stopped_plain = worker("a-1", "claude", 1, running=False)
    blocked = worker("a-3", "claude", 3, running=False, blockers=("its runtime reports unauthenticated",))
    claude = worker_cohorts.Cohort(runtime="claude", target=2, effective_ceiling=2,
                                   workers=(stopped_plain, stopped_pinned, blocked))
    moves = worker_cohorts.plan([claude])
    check(moves[0].kind == "start" and moves[0].role == "a-2" and "rework is pinned to it: T-4" in moves[0].reason
          and any(m.role == "a-3" and "start would refuse it" in m.reason for m in moves),
          f"a stopped worker its rework waits for starts first; one start would refuse is deferred: {moves}")


def test_cohorts_reports_and_converge_refuses_without_a_policy() -> None:
    with temporary_cluster(prefix="syrd628-none.", shutdown="immediate") as cluster:
        rig = Rig(cluster)
        try:
            check(rig.converge(action="cohorts") == 0 and any("live 2, busy 0, idle 2; deficit 2" in l for l in rig.lines),
                  f"cohorts is read-only status with the plan: {rig.lines}")
            plain = json.loads(json.dumps(rig.document))
            plain["scheduling"].pop("cohorts")
            rig.document = plain
            check(rig.converge() == 1 and "declares no scheduling cohorts" in rig.lines[0],
                  f"without cohorts there is nothing to converge, said so: {rig.lines}")
        finally:
            rig.close()


def test_an_operators_start_or_stop_of_a_member_is_recorded_as_theirs() -> None:
    with temporary_cluster(prefix="syrd628-operator.", shutdown="immediate") as cluster:
        rig = Rig(cluster)
        try:
            record = worker_cohorts.operator_change(rig.config, rig.document, "luna-12", "stop", writer=rig.writer,
                                                    print_func=rig.lines.append)
            check(worker_cohorts.operator_change(rig.config, rig.document, "main", "stop", writer=rig.writer) is None
                  and record is not None, "a member's start or stop is recorded; a persistent implementer's is not a cohort's")
            record("stop", "intended")
            record("stop", "done", "switchyard: luna-12 stopped: session gone")
            check(rig.events() == ["codex:luna-12:stop:operator:intended", "codex:luna-12:stop:operator:done"]
                  and rig.sql("SELECT reason FROM ticket_board.cohort_events ORDER BY id DESC LIMIT 1;")
                  == "switchyard worker-pool stop luna-12, run by the operator",
                  f"recorded as the operator's, with what was run: {rig.events()}")
            refused = worker_cohorts.operator_change(rig.config, rig.document, "luna-12", "stop",
                                                     writer=CohortWriter(rig.base, caller_role="app",
                                                                         write_token=rig.server.write_token),
                                                     print_func=rig.lines.append)
            refused("stop", "intended")
            check(any("the board did not record luna-12's stop" in l for l in rig.lines),
                  f"a record the board refuses is said, not hidden: {rig.lines}")
        finally:
            rig.close()


def test_the_real_stop_is_worker_pools_own() -> None:
    """Every case injects the fleet; this proves the default wiring reaches worker-pool's stop and start."""
    calls: list[tuple] = []
    saved = (worker_pool.stop_worker, worker_pool.start_worker)
    worker_pool.stop_worker = lambda config, member, runner=None: calls.append(("stop", member, runner)) or \
        worker_pool.WorkerAction(member, "stopped", "x")
    worker_pool.start_worker = lambda config, pool, member, **kw: calls.append(("start", member, kw.get("runner"))) or \
        worker_pool.WorkerAction(member, "started", "x")
    try:
        with temporary_cluster(prefix="syrd628-wiring.", shutdown="immediate") as cluster:
            rig = Rig(cluster)
            try:
                rig.fleet.busy = set()
                runner = object()
                worker_cohorts.run_command(
                    rig.config, rig.pool, document=rig.document, action="converge", config_path=Path(tempfile.gettempdir()),
                    read_readiness=rig.fleet.readiness, read_snapshot=lambda: rig.fleet.get("/api/board"),
                    read_status=lambda: rig.fleet.get("/api/cohorts"), busy_of=rig.fleet.busy_of, writer=rig.writer,
                    runner=runner, print_func=rig.lines.append)
                check(calls == [("stop", "luna-12", runner), ("start", "luna-3", runner)] or
                      calls == [("stop", "luna-12", runner), ("start", "luna-4", runner)],
                      f"converge stops and starts through worker-pool's own verbs, with the caller's runner: {calls}")
            finally:
                rig.close()
    finally:
        worker_pool.stop_worker, worker_pool.start_worker = saved


def main() -> int:
    only = set(sys.argv[1:])  # named cases only, e.g. for a mutant that one case covers
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case) and (not only or name in only):
            case()
    print(f"provider_cohort_convergence_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
