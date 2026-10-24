#!/usr/bin/env python3
"""SYRD-642: the Oracle core behaves as the PostgreSQL board does, through the same board server.

The same scenario runs on two boards. Both use the real `TicketBoardServer` and
are driven by the same HTTP calls a pane makes:
* a production-built PostgreSQL board (schema.sql, roles, rbac.sql, the real
  migrations), as router_role_board_test builds it;
* an Oracle board, `OracleBoardApp`, on a disposable schema created on the
  SYRD-642 test service and migrated by `oracle_schema.migrate`.

After each step the two tickets are compared: state, owner, commit, sign-offs,
gates, comments and the actions each board offers. The scenario covers:
* creating a ticket in Triage;
* the Director's route to Implementation;
* a comment;
* submission with a real commit, verified against a real git store;
* Audit's kick-back, which clears the sign-off and the commit and returns the
  work to its implementer;
* resubmission;
* Audit's approval, through the gate walk past a closed gate to Final Sign-Off;
* the Director's close.

What only the Oracle board needs proving runs on it alone:
* the durable queue, delivered by `oracle_listener`, including a claim held by
  a listener that died;
* atomic concurrent pull claims;
* restart, and a database outage through a local TCP proxy;
* restart-safe migrations: a crash between a DDL step and its record, a
  failing step and its resumed retry, and an upgrade from the first
  migration's schema;
* secret handling;
* process authority from runtime registrations;
* a board server and listener that import nothing of PostgreSQL.

Without the test service or python-oracledb the suite says it is skipped, and
why. It never passes without having run.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests"), str(ROOT / "tests" / "oracle")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import oracle_fixture  # noqa: E402

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def example() -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


class OracleBoard:
    """An Oracle board behind the real board server."""

    def __init__(self, runtime_config, root: Path, store_path: Path):
        import ticket_board_write_api_test as fixture
        from scripts.ticket_board.oracle_app import OracleBoardApp

        self.fixture = fixture
        self.app = OracleBoardApp(root / "frames", root / "assets", runtime_config, project="pgu",
                                  ticket_prefix="PGU", commit_git_dir=str(store_path))
        self.direct: list[dict] = []  # the server's direct "ticket created" sender: for stores with no queue only
        notifier = type("Recorder", (fixture.QuietNotifier,), {"notify_ticket_created": lambda _s, t: self.direct.append(t)})
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=notifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def act(self, ticket: str | None, operation: str, actor: str, **payload) -> dict:
        self.fixture.TEST_WRITE_TOKEN = self.server.write_token
        path = f"/api/tickets/{ticket}/actions/{operation}" if ticket else f"/api/tickets/actions/{operation}"
        try:
            return {"ok": self.fixture.post_json(self.base, path, payload, caller=actor)}
        except AssertionError as exc:
            return {"refused": str(exc)}

    def ticket(self, ticket: str) -> dict:
        import urllib.request
        with urllib.request.urlopen(f"{self.base}/api/tickets/{ticket}") as response:
            return json.loads(response.read())

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()


def view(ticket: dict) -> dict:
    """What the scenario compares: the workflow's facts about a ticket, on either backend."""
    return {"state": ticket["state"], "assignee": ticket["assignee"], "commit": ticket.get("commit_hash", ""),
            "audit_signoff": bool(ticket.get("audit_signoff")), "needs_audit": bool(ticket.get("needs_audit")),
            "needs_user_signoff": bool(ticket.get("needs_user_signoff")),
            "comments": [(c["who"], c["text"]) for c in ticket.get("comments", [])],
            "actions": sorted(a["action"] for a in ticket.get("workflow_actions", []))}


def act(board, ticket: str | None, operation: str, actor: str, **payload) -> dict:
    """One write over HTTP, as a pane makes it, on either board; a refusal is an answer."""
    import ticket_board_write_api_test as fixture

    fixture.TEST_WRITE_TOKEN = board.server.write_token
    path = f"/api/tickets/{ticket}/actions/{operation}" if ticket else f"/api/tickets/actions/{operation}"
    try:
        return {"ok": fixture.post_json(board.base, path, payload, caller=actor,
                                        expect=201 if operation == "create_ticket" else 200)}
    except AssertionError as exc:
        return {"refused": str(exc)}


def scenario(board, commits: tuple[str, str]) -> list[tuple[str, dict]]:
    """The shared scenario on one board; returns each step's answer and the ticket after it."""
    seen = []

    def step(name: str, answer: dict, ticket_id: str) -> None:
        seen.append((name, {"ok": "ok" in answer, "ticket": view(board.ticket(ticket_id))}))

    created = act(board, None, "create_ticket", "director", title="Oracle parity", body="the same on both",
                        state="analysis", assignee="director")
    check("ok" in created, f"create: {created}")
    t = created["ok"]["ticket"]["id"]
    step("create", created, t)
    step("route", act(board, t, "route", "director", state="in_progress", assignee="app"), t)
    step("comment", act(board, t, "add_comment", "app", text="starting on it"), t)
    first, second = commits
    step("refused: unknown commit", act(board, t, "submit_to_audit", "app", commit_hash="deadbeef" * 5), t)
    step("submit", act(board, t, "submit_to_audit", "app", commit_hash=first), t)
    step("kick back", act(board, t, "audit_kick_back", "audit", reason="tests missing"), t)
    step("refused: Director approves", act(board, t, "audit_sign_off", "director"), t)
    step("resubmit", act(board, t, "submit_to_audit", "app", commit_hash=second), t)
    step("approve", act(board, t, "audit_sign_off", "audit"), t)
    step("close", act(board, t, "mark_done", "director", commit_hash=second), t)
    return seen


#: Declared actions the Oracle core refuses by design (docs/syrd-642-oracle-backend.md): never drawn by the walk.
NOT_IN_CORE = {"relay_user_sign_off", "release_draft"}
ROLES = ("director", "main", "app", "ops", "audit", "inspector", "user")
GATES = ("needs_inspection", "needs_audit", "needs_user_signoff")


def status(answer: dict) -> int:
    """The HTTP status of an answer from act(): 200 for success, else the refusal's own."""
    if "ok" in answer:
        return 200
    match = re.match(r"\('[^']*', (\d+),", answer["refused"])
    return int(match.group(1)) if match else 0


def walk(postgres, oracle, commits: tuple[str, ...], seed: int, steps: int) -> int:
    """A seeded random walk, one request at a time to both boards; every answer and ticket compared.

    Mostly advertised actions by an actor allowed to take them, sometimes anything by anyone, with a reason,
    a commit, a target and an assignee drawn at random; sometimes the Director sets a gate. Returns how many
    of the steps were accepted.
    """
    import random

    rng = random.Random(seed)
    cfg = example()
    actions = sorted({t["action"] for t in cfg["transitions"]} - NOT_IN_CORE)
    made = [act(board, None, "create_ticket", "director", title=f"Walk {seed}", body="b", state="analysis",
                assignee="director") for board in (postgres, oracle)]
    ids = [m["ok"]["ticket"]["id"] for m in made]
    accepted = 0
    for step in range(steps):
        current = postgres.ticket(ids[0])
        if rng.random() < 0.1:
            operation, actor = "set_workflow_flags", rng.choice(("director", "director", "main"))
            payload = {rng.choice(GATES): rng.random() < 0.5}
        else:
            advertised = [a for a in current["workflow_actions"] if a["action"] not in NOT_IN_CORE]
            if advertised and rng.random() < 0.8:
                chosen = rng.choice(advertised)
                operation = chosen["action"]
                actor = rng.choice(chosen["actors"]) if rng.random() < 0.85 else rng.choice(ROLES)
                if chosen["owner_scoped"] and rng.random() < 0.85:
                    actor = current["assignee"]
                targets = [a["to"] for a in advertised if a["action"] == operation]
            else:
                operation, actor = rng.choice(actions), rng.choice(ROLES)
                targets = sorted({t["to"] for t in cfg["transitions"] if t["action"] == operation})
            payload = {}
            if rng.random() < 0.6:
                payload["reason"] = f"reason {step}"
            if rng.random() < 0.5:
                payload["commit_hash"] = rng.choice(commits)
            if len(targets) > 1 or rng.random() < 0.2:
                payload["target"] = rng.choice(targets)
            if rng.random() < 0.2:
                payload["assignee"] = rng.choice(ROLES)
        answers = [act(board, ticket, operation, actor, **payload) for board, ticket in zip((postgres, oracle), ids)]
        want, have = view(postgres.ticket(ids[0])), view(oracle.ticket(ids[1]))
        refused = answers[1].get("refused", "")
        check("ok" not in answers[0] or ("not in the Oracle core" not in refused and "not yet supported" not in refused),
              f"walk {seed} step {step}: {operation} by {actor} {payload}: PostgreSQL took it, and Oracle refused it "
              f"as unsupported: {refused}")
        check([status(a) for a in answers][0] == [status(a) for a in answers][1] and want == have,
              f"walk {seed} step {step}: {operation} by {actor} {payload} from {current['state']}:\n"
              f"  PostgreSQL {answers[0]}\n  Oracle     {answers[1]}\n  PostgreSQL ticket {want}\n"
              f"  Oracle ticket     {have}")
        accepted += "ok" in answers[0]
    # End the walk terminal on both boards, so the next walk's ticket is never queued behind this one by
    # PostgreSQL's serial focus, which the Oracle core does not have (a documented gap, not a difference here).
    for operation in ("cancel", "defer", "cancel"):  # a review stage cannot be cancelled unsigned: defer first
        if postgres.ticket(ids[0])["state"] in ("done", "cancelled"):
            break
        closed = [act(board, ticket, operation, "director", reason="walk over") for board, ticket in
                  zip((postgres, oracle), ids)]
        check(status(closed[0]) == status(closed[1]) and view(postgres.ticket(ids[0])) == view(oracle.ticket(ids[1])),
              f"walk {seed}: both boards answer {operation} alike while closing the walk: {closed}")
    check(postgres.ticket(ids[0])["state"] in ("done", "cancelled"), f"walk {seed}: the walked ticket is closed")
    return accepted


def parity(cluster, root: Path) -> None:
    """The same scenario, compared step by step; then seeded random walks, compared request by request."""
    from documentation_gate_board_test import Store
    from router_role_board_test import Board
    from scripts.ticket_board import oracle_schema

    store = Store(root / "store")
    base = store.commit([], {"README.md": "x\n"}, "base")
    first = store.commit([base], {"src/one.py": "print(1)\n"}, "one")
    commits = (first, store.commit([first], {"src/one.py": "print(1)\n", "tests/one.py": "assert 1\n"}, "two"))
    postgres = Board(cluster, "parity", example())
    postgres.app.commit_git_dirs = (store.path,)
    try:
        expected = scenario(postgres, commits)
        with oracle_fixture.disposable_schema("P") as (bootstrap, runtime, _private):
            oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _line: None)
            oracle = OracleBoard(runtime, root / "oracle", store.path)
            try:
                applied = act(oracle, None, "configure_workflow", "director", document=example(),
                              expected_revision=0, dry_run=False)
                check("ok" in applied, f"the Oracle board takes the workflow: {applied}")
                got = scenario(oracle, commits)
                check([name for name, _ in got] == [name for name, _ in expected], "the same steps ran")
                for (name, want), (_, have) in zip(expected, got):
                    check(have == want, f"{name}: Oracle answered {have}, PostgreSQL {want}")
                answered = dict(expected)
                check(not answered["refused: Director approves"]["ok"] and not answered["refused: unknown commit"]["ok"]
                      and expected[-1][1]["ticket"]["state"] == "done", "the scenario exercised refusals and reached done")
                check(oracle.direct == [], f"a queued backend never uses the direct created-notice sender: {oracle.direct}")
                try:
                    oracle.app._validate_commit_hash("deadbeef" * 5)
                    unknown = "accepted"
                except ValueError as exc:
                    unknown = str(exc)
                check(unknown.startswith("unknown commit_hash: deadbeef"), f"an unknown commit is named as unknown: {unknown}")
                accepted = sum(walk(postgres, oracle, commits + (base,), seed, WALK_STEPS) for seed in WALK_SEEDS)
                check(accepted >= len(WALK_SEEDS) * WALK_STEPS // 5,
                      f"the walks moved the tickets, not only collected refusals: {accepted} accepted")
            finally:
                oracle.close()
    finally:
        postgres.close()


WALK_SEEDS = tuple(range(int(os.environ.get("ORACLE_WALKS", "6"))))
WALK_STEPS = 60


class Sleepers:
    """Real processes standing in for role panes: what a runtime registration names."""

    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}

    def start(self, role: str) -> tuple[int, int]:
        from scripts.ticket_board.peer_identity import read_process

        proc = subprocess.Popen(["sleep", "600"])
        self.processes[role] = proc
        return proc.pid, read_process(proc.pid).start_time

    def stop(self, role: str) -> None:
        proc = self.processes.pop(role)
        proc.kill()
        proc.wait()

    def close(self) -> None:
        for role in list(self.processes):
            self.stop(role)


def register(app, sleepers: Sleepers, role: str) -> None:
    pid, start = sleepers.start(role)
    cfg = app.workflow_configuration()
    declared = next(r for r in cfg["roles"] if r["name"] == role)
    current = app.runtime_assignment(role)
    app.register_runtime_assignment(role=role, runtime=declared["runtime"], target=declared["target"],
                                    worktree=f"/w/{role}", session_dir=f"/s/{role}", process_pid=pid,
                                    process_start_time=start, process_uid=os.getuid(),
                                    expected_generation=int(current["generation"]) if current else 0)


def owner_sql(bootstrap, statement: str, **binds) -> None:
    from scripts.ticket_board.oracle_config import connect

    conn = connect(bootstrap)
    try:
        conn.cursor().execute(statement, **binds)
        conn.commit()
    finally:
        conn.close()


def queue_rows(bootstrap) -> list[tuple]:
    from scripts.ticket_board.oracle_config import connect

    conn = connect(bootstrap)
    try:
        return conn.cursor().execute("SELECT target_role, kind, attempts, claimed_at, last_error FROM "
                                     "notification_queue ORDER BY id").fetchall()
    finally:
        conn.close()


def queue_and_listener(root: Path) -> None:
    """The durable queue, delivered by the listener: to the live pane, once; a busy or missing pane waits."""
    from scripts.ticket_board import oracle_listener, oracle_schema
    from scripts.ticket_board.oracle_config import connect

    sleepers = Sleepers()
    with oracle_fixture.disposable_schema("L") as (bootstrap, runtime, _private):
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _line: None)
        board = OracleBoard(runtime, root / "queue", root / "store")
        try:
            act(board, None, "configure_workflow", "director", document=example(), expected_revision=0, dry_run=False)
            for role in ("director", "app"):
                register(board.app, sleepers, role)
            created = act(board, None, "create_ticket", "director", title="Queued", body="b", state="analysis",
                          assignee="director")["ok"]["ticket"]["id"]
            act(board, created, "route", "director", state="in_progress", assignee="app")
            act(board, created, "add_comment", "director", text="please start")
            act(board, created, "add_comment", "app", text="the owner's own note")
            queued = [(row[0], row[1]) for row in queue_rows(bootstrap)]
            # The Director's create notifies the Director, as PostgreSQL does when no process authority
            # stands behind the caller (its source is the user); the route and comment notify the new owner.
            check(queued == [("director", "transition"), ("app", "transition"), ("app", "ticket_update")],
                  f"each change queued one notice for its owner, in the transaction: {queued}")
            sent: list[tuple[str, str]] = []
            conn = connect(runtime)
            try:
                busy = oracle_listener.deliver_due(conn, lambda t, m: sent.append((t, m)), busy=lambda _t: True)
                rows = queue_rows(bootstrap)
                check(busy == 0 and sent == [] and len(rows) == 3 and all(r[3] is None and "busy" in r[4] for r in rows),
                      f"a busy pane is not typed into: the notices wait, unclaimed: {rows}")
                owner_sql(bootstrap, "UPDATE notification_queue SET next_attempt_at = SYSTIMESTAMP")
                # A listener that died holding a claim: nothing is sent until the claim times out.
                dead = oracle_listener.claim(conn)
                check(dead is not None and oracle_listener.deliver_due(conn, lambda t, m: sent.append((t, m))) == 2,
                      "with one notice claimed by a dead listener, the others are delivered")
                owner_sql(bootstrap, "UPDATE notification_queue SET claimed_at = SYSTIMESTAMP - INTERVAL '10' MINUTE")
                check(oracle_listener.deliver_due(conn, lambda t, m: sent.append((t, m))) == 1 and queue_rows(bootstrap) == [],
                      "the dead listener's claim times out, and the notice is delivered, not lost")
                check(sorted(t for t, _ in sent) == ["pgu-app:0.0", "pgu-app:0.0", "pgu-director:0.0"]
                      and any(m.startswith(f"{created} -- Queued entered") for _, m in sent)
                      and any("was changed by director: new comment" in m for _, m in sent),
                      f"each reached its role's registered pane, once: {sent}")
                # The owner's pane gone: the notice waits for a live registration.
                sleepers.stop("app")
                act(board, created, "add_comment", "director", text="still there?")
                owner_sql(bootstrap, "UPDATE notification_queue SET next_attempt_at = SYSTIMESTAMP")
                check(oracle_listener.deliver_due(conn, lambda t, m: sent.append((t, m))) == 0
                      and "has no live registered pane" in (queue_rows(bootstrap)[0][4] or ""),
                      "no notice is typed into a pane that is not the role's live registration")
                second = act(board, None, "create_ticket", "director", title="Back to me", body="b", state="analysis",
                             assignee="director")["ok"]["ticket"]["id"]
                before = [row[0] for row in queue_rows(bootstrap)].count("director")
                cancelled = act(board, second, "cancel", "director", reason="not now")
                reopened = act(board, second, "reopen", "director", reason="now")
                after = [row[0] for row in queue_rows(bootstrap)].count("director")
                check("ok" in cancelled and "ok" in reopened and board.ticket(second)["assignee"] == "director"
                      and after == before, f"a transition back to its own maker queues no notice for it: {before} -> {after}")
            finally:
                conn.close()
        finally:
            board.close()
            sleepers.close()


def concurrent_claims(root: Path) -> None:
    """Atomic pull claims: many workers at once, each ticket claimed once, each worker holding one."""
    from concurrent.futures import ThreadPoolExecutor
    from pull_scheduling_policy_test import pull_document
    from scripts.ticket_board import oracle_schema

    doc = pull_document()
    doc["project"] = "pgu"
    for role in doc["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    with oracle_fixture.disposable_schema("C") as (bootstrap, runtime, _private):
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _line: None)
        board = OracleBoard(runtime, root / "claims", root / "store")
        try:
            check("ok" in act(board, None, "configure_workflow", "director", document=doc, expected_revision=0,
                              dry_run=False), "the pull workflow applies")
            ready = [act(board, None, "create_ticket", "director", title=f"Ready {n}", body="b", state="ready",
                         assignee="unassigned")["ok"]["ticket"]["id"] for n in range(8)]
            barrier = threading.Barrier(12)

            def claim(role: str) -> dict:
                barrier.wait()
                return board.app.claim_next(role)

            with ThreadPoolExecutor(12) as pool:
                answers = list(pool.map(claim, ["main", "app", "ops"] * 4))
            claimed = [a for a in answers if a["claimed"]]
            check(len(claimed) == 3 and len({a["ticket"] for a in claimed}) == 3
                  and all(a["reason"] == "holding" for a in answers if not a["claimed"]),
                  f"twelve simultaneous claims: three tickets, one per worker, the rest told they hold one: {answers}")
            owners = {board.ticket(a["ticket"])["assignee"] for a in claimed}
            check(owners == {"main", "app", "ops"} and all(board.ticket(t)["state"] == "ready"
                                                           for t in ready if t not in {a["ticket"] for a in claimed}),
                  f"each claimed ticket is its claimant's; the rest still wait: {owners}")
            http = act(board, None, "claim_next", "main")
            check(http.get("ok", {}).get("reason") == "holding", f"the HTTP claim is the same claim: {http}")
            refused = act(board, None, "claim_next", "audit")
            check("refused" in refused and "cannot claim" in refused["refused"], f"a non-claimant cannot: {refused}")
            # The race, made certain: another session holds the claimant's implementer lock, so its claim waits.
            for t in [a["ticket"] for a in claimed]:
                act(board, t, "cancel", "director", reason="free the workers")
            from scripts.ticket_board.oracle_config import connect
            holder = connect(bootstrap)
            try:
                holder.cursor().execute("SELECT role FROM implementer_locks WHERE role = 'main' FOR UPDATE").fetchone()
                done: list[dict] = []
                waiting = threading.Thread(target=lambda: done.append(board.app.claim_next("main")), daemon=True)
                waiting.start()
                waiting.join(3)
                held_back = not done
            finally:
                holder.rollback()
                holder.close()
            waiting.join(30)
            check(held_back and done and done[0]["claimed"],
                  f"a claim waits for the claimant's implementer lock, then claims: {done}")
        finally:
            board.close()


class Proxy:
    """A local TCP relay to the test database, so a test can cut the connection and restore it."""

    def __init__(self, upstream: tuple[str, int]):
        self.upstream, self.port = upstream, 0
        self.listener: socket.socket | None = None
        self.links: list[socket.socket] = []

    def start(self) -> int:
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", self.port))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(16)
        threading.Thread(target=self._accept, args=(self.listener,), daemon=True).start()
        return self.port

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            try:
                server = socket.create_connection(self.upstream)
            except OSError:
                client.close()
                continue
            self.links += [client, server]
            for a, b in ((client, server), (server, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for s in (source, sink):
                try:
                    s.close()
                except OSError:
                    pass

    def stop(self) -> None:
        if self.listener:
            try:  # wakes the thread blocked in accept(); a bare close() leaves the port listening on Linux
                self.listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.listener.close()
            self.listener = None
        for link in self.links:
            try:
                link.shutdown(socket.SHUT_RDWR)
                link.close()
            except OSError:
                pass
        self.links = []


def restart_and_outage(root: Path) -> None:
    """A board and listener that lose the database and get it back, and a listener restarted mid-queue."""
    import dataclasses
    from scripts.ticket_board import oracle_listener, oracle_schema

    sleepers = Sleepers()
    proxy = Proxy(("127.0.0.1", 1521))
    with oracle_fixture.disposable_schema("O") as (bootstrap, runtime, _private):
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _line: None)
        port = proxy.start()
        relayed = dataclasses.replace(runtime, port=port)
        board = OracleBoard(relayed, root / "outage", root / "store")
        sent: list[tuple[str, str]] = []
        stop = threading.Event()
        listener = threading.Thread(target=oracle_listener.run_forever, args=(relayed, lambda t, m: sent.append((t, m))),
                                    kwargs={"stop": stop, "poll_seconds": 0.2, "reconnect_seconds": 0.2}, daemon=True)
        try:
            act(board, None, "configure_workflow", "director", document=example(), expected_revision=0, dry_run=False)
            register(board.app, sleepers, "app")
            t = act(board, None, "create_ticket", "director", title="Outage", body="b", state="in_progress",
                    assignee="app")["ok"]["ticket"]["id"]
            listener.start()
            deadline = time.monotonic() + 20
            while not sent and time.monotonic() < deadline:
                time.sleep(0.1)
            check(len(sent) == 1, f"delivered before the outage: {sent}")
            proxy.stop()
            during = act(board, t, "add_comment", "director", text="while the database is away")
            check("refused" in during and ("ORA-" in during["refused"] or "DPY-" in during["refused"]),
                  f"during the outage a write fails plainly, and nothing is half-done: {during}")
            time.sleep(1.0)
            proxy.port = port
            proxy.start()
            after = act(board, t, "add_comment", "director", text="the database is back")
            check("ok" in after, f"after recovery the board writes again: {after}")
            deadline = time.monotonic() + 20
            while len(sent) < 2 and time.monotonic() < deadline:
                time.sleep(0.1)
            check(len(sent) == 2 and "was changed by director" in sent[1][1] and board.ticket(t)["comments"][-1]["text"]
                  == "the database is back" and len(board.ticket(t)["comments"]) == 1,
                  f"the listener reconnected and delivered what was queued after; the failed write left nothing: {sent}")
            # A listener stopped and a new one started: the next notice is delivered once.
            stop.set()
            listener.join(10)
            act(board, t, "add_comment", "director", text="for the new listener")
            stop2 = threading.Event()
            second = threading.Thread(target=oracle_listener.run_forever, args=(relayed, lambda t_, m: sent.append((t_, m))),
                                      kwargs={"stop": stop2, "poll_seconds": 0.2}, daemon=True)
            second.start()
            deadline = time.monotonic() + 20
            while len(sent) < 3 and time.monotonic() < deadline:
                time.sleep(0.1)
            stop2.set()
            second.join(10)
            check(len(sent) == 3 and queue_rows(bootstrap) == [], f"a restarted listener delivers, once: {sent}")
            restarted = OracleBoard(relayed, root / "outage2", root / "store")
            try:
                check(view(restarted.ticket(t)) == view(board.ticket(t)), "a restarted board reads the same ticket")
            finally:
                restarted.close()
        finally:
            stop.set()
            board.close()
            proxy.stop()
            sleepers.close()


def migrations() -> None:
    """Fresh, repeated, upgraded from older schemas, resumed after a crash and after a failed step."""
    from scripts.ticket_board import oracle_schema
    from scripts.ticket_board.oracle_config import connect

    all_steps = list(oracle_schema.MIGRATIONS)
    total = sum(len(steps) for _name, steps in all_steps)
    for older in (1, 2):  # the schema as each earlier release left it, then upgraded
        with oracle_fixture.disposable_schema(f"U{older}") as (bootstrap, _runtime, _private):
            oracle_schema.MIGRATIONS[:] = all_steps[:older]
            try:
                first = oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
            except Exception as exc:  # noqa: BLE001 -- an older release's migrate must work on its own schema
                first = []
                check(False, f"the first {older} migration(s) apply on their own: {exc}")
            finally:
                oracle_schema.MIGRATIONS[:] = all_steps
            upgraded = oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
            again = oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
            check(len(first) + len(upgraded) == total and again == [],
                  f"from the first {older} migration(s): the rest applied once, then nothing: {len(first)}+{len(upgraded)}")
    with oracle_fixture.disposable_schema("R") as (bootstrap, _runtime, _private):
        oracle_schema.MIGRATIONS[:] = all_steps[:1]
        try:
            oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
        finally:
            oracle_schema.MIGRATIONS[:] = all_steps
        # A crash between the next DDL and its record: the table exists, the ledger does not say so.
        owner_sql(bootstrap, all_steps[1][1][0].sql)
        said: list[str] = []
        try:
            oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=said.append)
        except oracle_schema.MigrationFailed as exc:
            said.append(f"failed: {exc}")
        check(said[0] == f"{all_steps[1][0]} step 1/{len(all_steps[1][1])}: already in place, recorded",
              f"the half-done step is recognised and recorded, never run twice: {said[:2]}")
        # A failing step: stop there, say what is in place, resume there once it is fixed.
        bad = oracle_schema.Step("CREATE TABLE board_meta (k NUMBER)", "SELECT 0 FROM dual")
        good = oracle_schema._table("syrd642_probe", "a NUMBER")
        later = oracle_schema._table("syrd642_probe2", "a NUMBER")
        oracle_schema.MIGRATIONS[:] = all_steps + [("ora9999_test", [good, bad, later])]
        try:
            try:
                oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
                failure = ""
            except oracle_schema.MigrationFailed as exc:
                failure = str(exc)
            check("ora9999_test step 2/3 failed: ORA-00955" in failure and "resumes at this step" in failure
                  and "nothing was rolled back or dropped" in failure,
                  f"a failed step is reported honestly, with Oracle's own error: {failure}")
            oracle_schema.MIGRATIONS[:] = all_steps + [("ora9999_test", [good, oracle_schema._table("syrd642_fix", "a NUMBER"), later])]
            said = []
            oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=said.append)
            check(said == ["ora9999_test step 2/3: applied", "ora9999_test step 3/3: applied"],
                  f"the retry resumed at the failed step, and the step before it was not run again: {said}")
        finally:
            oracle_schema.MIGRATIONS[:] = all_steps
        conn = connect(bootstrap)
        try:
            grants = {row[0]: row[1] for row in conn.cursor().execute(
                "SELECT table_name, listagg(privilege, ',') WITHIN GROUP (ORDER BY privilege) FROM user_tab_privs "
                "WHERE grantee = :g GROUP BY table_name", g=bootstrap.runtime_user())}
        finally:
            conn.close()
        check(grants.get("TICKETS") == "INSERT,SELECT,UPDATE" and "DELETE" not in grants.get("TICKETS", "")
              and grants.get("BOARD_META") == "SELECT" and grants.get("NOTIFICATION_TRACE") == "INSERT,SELECT",
              f"the runtime account holds rows only, the least each table needs: {grants}")


def secrets_and_authority(root: Path) -> None:
    """No secret in what the configuration prints; authority comes from the local process, as before."""
    import dataclasses
    from scripts.ticket_board import oracle_config, oracle_schema
    from scripts.ticket_board.local_peer_authority import CallerIdentityError, PeerCredentials, ProcessRoleAuthority
    from scripts.ticket_board.peer_identity import SessionIdentity, read_process

    with oracle_fixture.disposable_schema("S") as (bootstrap, runtime, private):
        user, password = runtime.credentials()
        shown = repr(runtime) + repr(bootstrap) + json.dumps(runtime.describe()) + json.dumps(bootstrap.describe())
        check(password not in shown and user not in shown and "runtime.credentials" in shown,
              f"repr and describe name the credentials files, never a user name or password: {shown}")
        loose = private / "loose.credentials"
        loose.write_text(json.dumps({"user": user, "password": password}))
        loose.chmod(0o644)
        board_json = json.loads((private / "board.json").read_text())
        for label, change, words in (
                ("loose", {"runtime_credentials_file": str(loose)}, "make it mode 0600"),
                ("inline password", {"password": password}, "belong in the credentials files"),
                ("inline user", {"user": user}, "belong in the credentials files"),
                ("dsn", {"dsn": "127.0.0.1:1521/FREEPDB1"}, "give host, port and exactly one of sid or service_name"),
                ("both identifiers", {"sid": "FREE"}, "needs exactly one of sid and service_name (it gives both)")):
            (private / "variant.json").write_text(json.dumps({**board_json, **change}))
            try:
                oracle_config.connect(oracle_config.load(private / "variant.json"))
                said = "accepted"
            except Exception as exc:  # noqa: BLE001 -- refused here, or (wrongly) attempted and failed
                said = f"{type(exc).__name__}: {exc}"
            check(words in said and password not in said and user not in said,
                  f"{label}: refused, without echoing a secret: {said}")
        wrong = private / "wrong.credentials"
        oracle_fixture.write_credentials(wrong, user, "Wrongpassword123456")
        try:
            oracle_config.connect(dataclasses.replace(runtime, runtime_credentials_file=wrong))
            said = "accepted"
        except Exception as exc:  # noqa: BLE001
            said = str(exc)
        check("ORA-01017" in said and "Wrongpassword" not in said and user not in said,
              f"a refused login says so, without the password or the user: {said}")
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
        board = OracleBoard(runtime, root / "authority", root / "store")
        sleepers = Sleepers()
        try:
            act(board, None, "configure_workflow", "director", document=example(), expected_revision=0, dry_run=False)
            register(board.app, sleepers, "app")
            current = board.app.runtime_assignment("app")
            declared = next(r for r in board.app.workflow_configuration()["roles"] if r["name"] == "app")
            refusals = []
            for change in ({"expected_generation": int(current["generation"]) - 1},
                           {"target": "pgu-app:1.0", "expected_generation": int(current["generation"])}):
                try:
                    board.app.register_runtime_assignment(**{
                        "role": "app", "runtime": declared["runtime"], "target": declared["target"],
                        "worktree": "/w/x", "session_dir": "/s/x", "process_pid": current["process_pid"],
                        "process_start_time": current["process_start_time"], "process_uid": os.getuid(), **change})
                    refusals.append("accepted")
                except ValueError as exc:
                    refusals.append(str(exc))
            check(refusals == ["runtime assignment generation changed",
                               "role/runtime/target is not active workflow configuration"]
                  and board.app.runtime_assignment("app") == current,
                  f"a stale registration and an undeclared target are refused, and change nothing: {refusals}")
            authority = ProcessRoleAuthority(board.app, "x", resolve_uid=lambda _a: os.getuid(),
                                             resolve_session=lambda pid: SessionIdentity(pid, read_process(pid).start_time))
            pane = sleepers.processes["app"].pid
            check(authority.role_for_peer(PeerCredentials(pane, os.getuid(), os.getgid())) == "app",
                  "the registered live pane is its role, read from the Oracle board")
            stranger = subprocess.Popen(["sleep", "60"])
            try:
                try:
                    authority.role_for_peer(PeerCredentials(stranger.pid, os.getuid(), os.getgid()))
                    said = "admitted"
                except CallerIdentityError as exc:
                    said = str(exc)
            finally:
                stranger.kill()
                stranger.wait()
            check("no PostgreSQL role assignment" in said,
                  f"a live process of the same account that is not registered is no role: {said}")
            check(board.app.runtime_assignment_for_process(pane, 1, os.getuid()) is None,
                  "a reused pid with another start time is not the registered pane")
        finally:
            board.close()
            sleepers.close()


def engine_rules() -> None:
    """Rules of workflow_engine the HTTP walks cannot reach: the server refuses first, or the example workflow
    declares no such transition. Decided directly, as a workflow that does declare them would be."""
    import copy
    from scripts.ticket_board import workflow_engine as we
    from scripts.ticket_board.workflow_config import validate

    cfg = validate(example(), project="pgu")

    def ticket(state: str, assignee: str, **rest) -> we.TicketState:
        return we.TicketState(id="PGU-9", state=state, assignee=assignee, flags=we.initial_flags(cfg), **rest)

    def outcome(config, t, action, actor, **payload):
        try:
            return we.decide(config, t, action, actor, payload).ticket
        except (we.ActorRefused, we.TransitionRefused) as exc:
            return f"{type(exc).__name__}: {exc}"

    other = outcome(cfg, ticket("in_progress", "app"), "submit_to_audit", "main", commit_hash="a" * 40)
    check(other == "ActorRefused: main cannot perform workflow action submit_to_audit",
          f"an owner-scoped action by another allowed role is refused as an actor refusal: {other}")
    exempt = outcome(cfg, ticket("in_progress", "app", commit_exempt=True), "submit_to_audit", "app")
    check(isinstance(exempt, we.TicketState) and exempt.state == "audit",
          f"a commit-exempt ticket takes a commit-requiring transition without a commit: {exempt}")
    signed = {**we.initial_flags(cfg), "user_signoff": True}
    kept = outcome(cfg, we.TicketState(id="PGU-9", state="in_progress", assignee="app", commit_hash="a" * 40,
                                       flags=dict(signed)), "submit_to_audit", "app", commit_hash="a" * 40)
    changed = outcome(cfg, we.TicketState(id="PGU-9", state="in_progress", assignee="app", commit_hash="a" * 40,
                                          flags=dict(signed)), "submit_to_audit", "app", commit_hash="b" * 40)
    check(kept.flags["user_signoff"] is True and changed.flags["user_signoff"] is False,
          f"leaving implementation with a different commit clears every sign-off, and the same commit keeps them: "
          f"{kept.flags} {changed.flags}")
    terminal = copy.deepcopy(cfg)
    route = next(t for t in terminal["transitions"] if t["action"] == "route")
    terminal["transitions"].append({**route, "from": "cancelled", "action": "revive"})
    revived = outcome(terminal, ticket("cancelled", "unassigned"), "revive", "director",
                      assignee="app")
    check(revived == "TransitionRefused: terminal exit requires reopen",
          f"a terminal stage is left only by reopen, whatever else a workflow declares: {revived}")
    unskipped = copy.deepcopy(cfg)
    next(st for st in unskipped["stages"] if st["name"] == "audit")["skip_to"] = None
    entered = outcome(unskipped, we.TicketState(id="PGU-9", state="in_progress", assignee="app", commit_hash="a" * 40,
                                                flags={**we.initial_flags(cfg), "audit_signoff": True}),
                      "submit_to_audit", "app", commit_hash="a" * 40)
    check(isinstance(entered, we.TicketState) and entered.state == "audit" and entered.flags["audit_signoff"] is False,
          f"a review stage is entered with its own sign-off cleared: {entered}")
    handback = copy.deepcopy(cfg)
    kick = next(t for t in handback["transitions"] if t["action"] == "audit_kick_back")
    handback["transitions"].append({**kick, "action": "audit_hand_back", "primitive": "move", "require_reason": False,
                                    "clear_signoffs": []})
    flags = {**we.initial_flags(cfg), "inspector_signoff": True}
    handed = outcome(handback, we.TicketState(id="PGU-9", state="audit", assignee="audit", commit_hash="a" * 40,
                                              flags=dict(flags), last_implementer="app"), "audit_hand_back", "audit")
    # A move is not a return: the implementer is not restored, and the stage's first owner takes it.
    check(isinstance(handed, we.TicketState) and handed.state == "in_progress" and handed.assignee == "main"
          and handed.flags["inspector_signoff"] is True and handed.commit_hash == "a" * 40,
          f"a review handing work back moves unsigned and keeps its record: {handed}")


def boundary(root: Path) -> None:
    """What the core does not do is refused plainly, and leaves the board as it was."""
    from scripts.ticket_board import extension_operations, oracle_app, oracle_schema, workflow_engine
    from scripts.ticket_board.documentation_gate import GATE_ACTIONS

    with oracle_fixture.disposable_schema("B") as (bootstrap, runtime, _private):
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
        board = OracleBoard(runtime, root / "boundary", root / "store")
        try:
            act(board, None, "configure_workflow", "director", document=example(), expected_revision=0, dry_run=False)
            t = act(board, None, "create_ticket", "director", title="Edge", body="b", state="analysis",
                    assignee="director")["ok"]["ticket"]["id"]
            before = board.app.store_signature()
            said = []
            for name in oracle_app.UNSUPPORTED_OPERATIONS:
                try:
                    getattr(board.app, name)(t, {}, caller_role="director")
                    said.append((name, "accepted"))
                except workflow_engine.Unsupported as exc:
                    said.append((name, "" if f"{name} is not in the Oracle core yet" in str(exc)
                                 and "nothing was changed" in str(exc) else str(exc)))
            check(all(text == "" for _name, text in said), f"each operation outside the core says so: {said}")
            stale = act(board, None, "configure_workflow", "director", document=example(), expected_revision=0,
                        dry_run=False)
            check("workflow revision changed; reread before applying" in stale.get("refused", ""),
                  f"a workflow applied over a stale revision is refused: {stale}")
            edits = act(board, t, "edit_fields", "director", title="renamed")
            blocked = act(board, None, "create_ticket", "director", title="Blocked", body="b", state="analysis",
                          assignee="director", blocked_by=[t])
            check("not in the Oracle core yet" in edits.get("refused", "") + str(edits)
                  and "creating a blocked ticket is not in the Oracle core yet" in blocked.get("refused", ""),
                  f"an edit and a blocked create are refused as outside the core: {edits} {blocked}")
            check(board.app.store_signature() == before, "nothing was changed by any refusal")

            class Untouchable:
                store_backend = "oracle"

                def __getattr__(self, name):
                    raise AssertionError(f"the documentation and size checks reached the app: {name}")

            check(extension_operations.before_transition(Untouchable(), t, "route", {}, caller_role="director") is None,
                  "an ordinary transition on Oracle is not measured or gated, and touches nothing")
            refused = []
            for action in GATE_ACTIONS:
                try:
                    extension_operations.before_transition(Untouchable(), t, action, {}, caller_role="director")
                except NotImplementedError as exc:
                    refused.append("never taken unchecked" in str(exc))
            check(refused == [True] * len(GATE_ACTIONS), "a documentation-gate decision is refused on Oracle")
        finally:
            board.close()


def real_server(root: Path) -> None:
    """The shipped server entry point on Oracle, written to by the shipped clients through its Unix socket.

    Caller authority is the local socket's: the peer's uid maps to its role, and the remote database has no
    say in it. A client that claims another role is refused before the database is asked.
    """
    import pwd
    import urllib.request
    from scripts.ticket_board import oracle_schema

    me = pwd.getpwuid(os.getuid()).pw_name
    with oracle_fixture.disposable_schema("E") as (bootstrap, runtime, private):
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
        place = root / "real"
        place.mkdir()
        sock = place / "board.sock"
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        # Nothing of the pane's own board may leak in: these clients would otherwise write to it.
        base = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD", "PGU_TICKET_BOARD", "PG"))}
        server_env = {**base, "TICKET_BOARD_BACKEND": "oracle", "TICKET_BOARD_ORACLE_CONFIG": str(private / "board.json"),
                      "TICKET_BOARD_PROJECT": "pgu", "TICKET_BOARD_ROLE_ACCOUNTS": f"director={me}"}
        log = open(place / "server.log", "w")
        server = subprocess.Popen([sys.executable, "-B", str(ROOT / "scripts/ticket-board.py"), "--host", "127.0.0.1",
                                   "--port", str(port), "--unix-socket", str(sock), "--frames", str(place),
                                   "--assets", str(place)], cwd=ROOT, env=server_env, stdout=log,
                                  stderr=subprocess.STDOUT, start_new_session=True)
        url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 60
            board = None
            while time.monotonic() < deadline and server.poll() is None:
                try:
                    board = json.loads(urllib.request.urlopen(f"{url}/api/board", timeout=5).read())
                    if sock.exists():
                        break
                except OSError:
                    time.sleep(0.3)
            check(board is not None and board["store_backend"] == "oracle" and sock.exists(),
                  f"ticket-board.py serves an Oracle board over HTTP and its Unix socket: "
                  f"{(place / 'server.log').read_text()[-1500:]}")
            client_env = {**base, "TICKET_BOARD_SOCKET": str(sock), "TICKET_BOARD_URL": url,
                          "TICKET_BOARD_CALLER_ROLE": "director", "PYTHONPATH": str(ROOT / "scripts")}

            def client(*argv: str) -> tuple[int, str]:
                done = subprocess.run([sys.executable, "-B", *argv], cwd=ROOT, env=client_env, capture_output=True,
                                      text=True, timeout=120, stdin=subprocess.DEVNULL)
                return done.returncode, done.stdout + done.stderr

            (place / "workflow.json").write_text(json.dumps(example()))
            # The write client's own configure_workflow, on the socket only (ticket-board-workflow apply would
            # also rewrite a tenant's launcher configuration, which this board does not have).
            rc, out = client("-c", "import json, sys; from ticket_board.write_client import TicketBoardWriteClient; "
                             "c = TicketBoardWriteClient(board_url=sys.argv[1], caller_role='director', "
                             "socket_path=sys.argv[2]); print(c.configure_workflow(json.load(open(sys.argv[3])), "
                             "expected_revision=0, same_endpoint=True)['revision'])", url, str(sock),
                             str(place / "workflow.json"))
            check(rc == 0 and out.strip().endswith("1"), f"the write client applies the workflow through the socket: {out}")
            rc, out = client(str(ROOT / "scripts/ticket-board-write"), "create-ticket", "--title", "Through the socket",
                             "--body", "real clients, real peer credentials", "--state", "analysis",
                             "--assignee", "director")
            made = json.loads(urllib.request.urlopen(f"{url}/api/tickets/PGU-1", timeout=10).read()) if rc == 0 else {}
            check(rc == 0 and made.get("title") == "Through the socket" and made.get("store_backend") == "oracle",
                  f"ticket-board-write creates a ticket on Oracle as the peer's role: {out}")
            rc, out = client(str(ROOT / "scripts/ticket-board-write"), "--caller-role", "audit", "add-comment", "PGU-1",
                             "--text", "as audit?")
            after = json.loads(urllib.request.urlopen(f"{url}/api/tickets/PGU-1", timeout=10).read())
            check(rc != 0 and "this pane is registered as director, not audit" in out and after["comments"] == [],
                  f"a client claiming a role its peer credentials do not carry is refused, and nothing is written: {out}")
        finally:
            server.terminate()
            try:
                server.wait(15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
            log.close()


NO_POSTGRES = r"""
import importlib.abc, json, os, sys, threading, urllib.request
mode = sys.argv[3]
if mode == "unimportable":  # PostgreSQL's driver is not even installed
    class Refuse(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "psycopg" or name.startswith(("psycopg.", "psycopg_")):
                raise ImportError("PostgreSQL's driver is not on this host")
    sys.meta_path.insert(0, Refuse())
else:  # the driver library is installed (the board requires it), but no PostgreSQL may be reached
    import psycopg
    def refuse(*_a, **_k):
        raise AssertionError("an Oracle board connected to PostgreSQL")
    psycopg.connect = psycopg.Connection.connect = refuse
sys.path[:0] = [".", "scripts"]
from scripts.ticket_board import oracle_config, oracle_listener
result = {"mode": mode, "pg_env": sorted(k for k in os.environ if k.startswith("PG"))}
config = oracle_config.load(sys.argv[2])
if mode == "unimportable":
    from scripts.ticket_board.oracle_app import OracleBoardApp
    from scripts.ticket_board.server import TicketBoardServer
    from scripts.ticket_board.board_notifications import DirectorNotifier
    from scripts.ticket_board.cli import run_server  # the server entry point's module
    app = OracleBoardApp(__import__("pathlib").Path(sys.argv[1]), __import__("pathlib").Path(sys.argv[1]), config,
                         project="pgu", ticket_prefix="PGU")
    server = TicketBoardServer(("127.0.0.1", 0), app, director_notifier=DirectorNotifier())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    result["backend"] = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/board").read())["store_backend"]
    conn = oracle_config.connect(config)
    oracle_listener.deliver_due(conn, lambda t, m: None)
    server.shutdown()
    import contextlib, io, runpy
    sys.argv, shown = ["ticket-board-notify-listener", "--help"], io.StringIO()
    with contextlib.redirect_stdout(shown):
        try:
            runpy.run_path("scripts/ticket-board-notify-listener", run_name="__main__")
        except SystemExit:
            pass
    result["listener_entry"] = "Deliver an Oracle board's queued notices" in shown.getvalue()
    result["psycopg_loaded"] = any(m.startswith("psycopg") for m in sys.modules)
else:
    send, busy = oracle_listener.delivery("/nonexistent/directorctl")  # the production sender and activity gate
    result["delivery"] = [type(send).__name__, type(busy.__self__).__name__]
print(json.dumps(result))
"""


def no_postgres() -> None:
    """An Oracle board needs no PostgreSQL: no server, no connection, no settings, and mostly not even the driver."""
    from scripts.ticket_board import oracle_schema
    import tempfile

    with oracle_fixture.disposable_schema("N") as (bootstrap, _runtime, private), \
            tempfile.TemporaryDirectory() as frames:
        oracle_schema.migrate(bootstrap, project="pgu", ticket_prefix="PGU", report=lambda _l: None)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "TICKET_BOARD_DATABASE"))}
        results = []
        for mode in ("unimportable", "importable"):
            done = subprocess.run([sys.executable, "-c", NO_POSTGRES, frames, str(private / "board.json"), mode],
                                  cwd=ROOT, capture_output=True, text=True, timeout=120,
                                  env={**env, "TICKET_BOARD_BACKEND": "oracle"})
            results.append(json.loads(done.stdout.strip().splitlines()[-1]) if done.returncode == 0
                           else {"error": done.stderr[-800:]})
        check(results[0] == {"mode": "unimportable", "pg_env": [], "backend": "oracle", "listener_entry": True,
                             "psycopg_loaded": False},
              f"the Oracle board serves its board and delivers its queue with PostgreSQL's driver unimportable: {results[0]}")
        check(results[1] == {"mode": "importable", "pg_env": [],
                             "delivery": ["DirectorctlSender", "PaneActivityGate"]},
              f"the listener's production sender and activity gate are built with every PostgreSQL connect refused; "
              f"the gate imports the driver library through notify_listener (a documented gap): {results[1]}")


def main() -> int:
    why = oracle_fixture.available()
    if why:
        print(f"oracle_board_test: SKIPPED, not run: {why}")
        return 0
    import tempfile
    from temporary_cluster import temporary_cluster

    with tempfile.TemporaryDirectory(prefix="syrd642-") as tmp, temporary_cluster(prefix="syrd642-pg-") as cluster:
        root = Path(tmp)
        parity(cluster, root)
        for case in (queue_and_listener, concurrent_claims, restart_and_outage, secrets_and_authority):
            case(root)
        migrations()
        engine_rules()
        boundary(root)
        real_server(root)
        no_postgres()
    print(f"oracle_board_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
