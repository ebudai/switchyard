#!/usr/bin/env python3
"""Workflow previews neither deadlock each other nor wedge the ticket store (SYRD-572).

On MEFP (2026-10-07) eight concurrent `set-role-runtime --dry-run` calls hit
PostgreSQL deadlocks, and the next one wedged every ticket read for fifteen
minutes:

* a ticket read (`_pg_get_ticket`) ran its row query on one connection -- an
  open transaction holding a share lock on `tickets` -- and then, to validate
  the row's state, opened a *second* connection to read `workflow_stages`;
* a preview (`apply_declared_workflow`, which alters constraints on `tickets`
  even when it will roll back) held `workflow_stages` exclusively and queued for
  `tickets` behind the read's share lock;
* every later reader queued behind that exclusive request, and nothing had a
  timeout. PostgreSQL saw one session idle in a transaction, not a deadlock:
  the edge between the read's two connections lives in one Python thread.

These run the board exactly as a tenant does -- `schema.sql`, the real
`ticket-board-migrate`, `rbac.sql`, the service role -- on a throwaway cluster,
and send the workflow documents `set-role-runtime` sends. Each case is bounded:
a wedge fails its case and is then cleared by terminating this cluster's own
sessions, never anything on the host.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import psycopg  # noqa: E402

import ticket_board_write_api_test as t  # noqa: E402
from changed_commit_fresh_audit_test import mefp_shaped  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

from scripts import role_runtime  # noqa: E402
from scripts.ticket_board import app as app_module  # noqa: E402
try:  # absent before SYRD-572: on that release every case still runs, as it ships
    from scripts.ticket_board import workflow_locking  # noqa: E402
except ImportError:  # pragma: no cover - base-release proof only
    workflow_locking = None  # type: ignore[assignment]
from scripts.ticket_board.workflow_config import validate  # noqa: E402

DB_PREFIX = "previews"
CHECKS = 0
#: How long any one case may take before it is called a wedge.
CASE_BOUND_SECONDS = 45.0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Board:
    """A tenant board on its own database: production install order, MEFP-shaped workflow."""

    def __init__(self, cluster: Any, db: str) -> None:
        self.cluster = cluster
        self.db = db
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, """
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
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, (ROOT / "scripts/ticket_board/schema.sql").read_text())
        migrated = subprocess.run(
            ["bash", str(ROOT / "scripts/ticket-board-migrate")],
            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin},
            capture_output=True, text=True,
        )
        assert migrated.returncode == 0, migrated.stderr
        t.psql(self.admin, (ROOT / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean",
            ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        document = mefp_shaped()
        # A custom implementer, as otto's `uiux` is: a role outside the built-in
        # assignee list, whose tickets are validated against the declared workflow.
        ops = next(r for r in document["roles"] if r["name"] == "ops")
        document["roles"].append({**copy.deepcopy(ops), "name": "uiux", "label": "UIUX",
                                  "target": ops["target"].replace("-ops", "-uiux"), "slot": None})
        next(s for s in document["stages"] if s["name"] == "in_progress")["owners"].append("uiux")
        self.app.apply_workflow(validate(document, project="cerulean"), expected_revision=0, dry_run=False,
                                caller_role="director")
        for number in range(1, 7):
            t.seed_postgres_ticket(self.admin, f"PGU-{number}", title=f"Fixture {number}",
                                   state="in_progress", assignee="main", commit_exempt=True)
        t.seed_postgres_ticket(self.admin, "PGU-7", title="Custom implementer", state="in_progress",
                               assignee="uiux", commit_exempt=True)
        self.server: Any = None
        self._prove_the_probe()

    def _prove_the_probe(self) -> None:
        """The session probe must see what it is asked about, or every clean check is vacuous."""
        held = self.app._pg_connect()
        try:
            held.execute("SELECT 1 FROM ticket_board.tickets LIMIT 1").fetchall()
            seen = [s for s in self.sessions() if s["pid"] == held.info.backend_pid]
            assert seen and seen[0]["state"] == "idle in transaction", f"the probe cannot see the board: {seen}"
            try:
                self.assert_clean("probe self-test")
            except AssertionError:
                pass
            else:
                raise AssertionError("assert_clean passed over a session idle in a transaction")
        finally:
            held.rollback()
            held.close()

    def document(self) -> tuple[dict[str, Any], int]:
        current = self.app.workflow_document()
        return copy.deepcopy(current["document"]), int(current["revision"])

    def implementers(self) -> list[str]:
        document, _revision = self.document()
        return [r["name"] for r in document["roles"] if r.get("runtime") and r["name"] != "director"]

    def serve(self) -> str:
        self.server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier())
        t.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def admin_connection(self) -> Any:
        """An admin connection that reads text as text.

        This cluster's default client encoding hands text back as bytes, so a
        comparison such as `state == "idle in transaction"` would never match
        and a check built on it could never fail. The board's own connections
        set UTF8 the same way (`_pg_connect`).
        """
        conn = psycopg.connect(self.admin, autocommit=True)
        conn.execute("SET client_encoding TO 'UTF8'")
        return conn

    def sessions(self) -> list[dict[str, Any]]:
        """The board's own sessions on this database, as PostgreSQL sees them."""
        with self.admin_connection() as conn:
            rows = conn.execute(
                "SELECT pid, state, wait_event_type, now()-state_change AS age, left(query, 60) AS query "
                "FROM pg_stat_activity WHERE datname = %s AND usename = %s AND pid <> pg_backend_pid()",
                (self.db, t.SERVICE_ROLE),
            ).fetchall()
        return [dict(zip(("pid", "state", "wait", "age", "query"), row)) for row in rows]

    def assert_clean(self, label: str) -> None:
        """No board session left in a transaction, and none waiting on a lock."""
        deadline = time.monotonic() + 10
        while True:
            stuck = [s for s in self.sessions()
                     if str(s["state"]).startswith("idle in transaction") or s["wait"] == "Lock"]
            if not stuck or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        check(stuck == [], f"{label}: board sessions left in a transaction or waiting on a lock: {stuck}")

    def unwedge(self) -> None:
        """Clear a wedge this case made: this cluster's board sessions on this database only."""
        with self.admin_connection() as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND usename = %s AND pid <> pg_backend_pid()",
                (self.db, t.SERVICE_ROLE),
            )

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def http_post(base: str, path: str, payload: dict[str, Any], *, caller: str = "director",
              timeout: float = 30.0) -> tuple[int, str]:
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", t.CALLER_ROLE_HEADER: caller,
                 t.WRITE_TOKEN_HEADER: str(t.TEST_WRITE_TOKEN)},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def http_get(base: str, path: str, *, timeout: float = 30.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def bounded(threads: list[threading.Thread], seconds: float) -> list[str]:
    deadline = time.monotonic() + seconds
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return [thread.name for thread in threads if thread.is_alive()]


def wait_until(predicate: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def other_runtime(document: dict[str, Any], role: str) -> str:
    """A runtime the role is not on now, so the document really changes."""
    current = next(r for r in document["roles"] if r["name"] == role).get("runtime")
    return "claude" if current == "codex" else "codex"


def preview_payload(document: dict[str, Any], revision: int, role: str, runtime: str, *, dry_run: bool) -> dict[str, Any]:
    """What `set-role-runtime` sends: the workflow with one role's runtime changed."""
    return {
        "document": role_runtime.document_with_runtime(document, role=role, runtime=runtime),
        "expected_revision": revision,
        "dry_run": dry_run,
    }


def case_concurrent_previews_then_a_read_then_a_change(board: Board) -> None:
    """The ticket's acceptance, end to end over HTTP."""
    base = board.serve()
    document, revision = board.document()
    roles = board.implementers()
    check(len(roles) >= 4, f"the fixture declares several implementers: {roles}")
    previews: dict[str, tuple[int, str]] = {}
    reads: list[tuple[int, str]] = []
    stop_reading = threading.Event()

    def preview(role: str, index: int) -> None:
        runtime = other_runtime(document, role) if index % 2 else "agy"
        previews[f"{role}#{index}"] = http_post(
            base, "/api/tickets/actions/configure_workflow",
            preview_payload(document, revision, role, runtime, dry_run=True),
        )

    def read() -> None:
        while not stop_reading.is_set():
            # A reader that keeps finding the state cache empty takes the
            # path that opened the second connection.
            board.app._workflow_states_cache = None
            reads.append(http_get(base, "/api/tickets/PGU-1", timeout=30))

    workers = [threading.Thread(target=preview, args=(roles[i % len(roles)], i), name=f"preview-{i}")
               for i in range(8)]
    readers = [threading.Thread(target=read, name=f"reader-{i}", daemon=True) for i in range(3)]
    for thread in readers + workers:
        thread.start()
    wedged = bounded(workers, CASE_BOUND_SECONDS)
    stop_reading.set()
    wedged += bounded(readers, 30)
    if wedged:
        board.unwedge()
    check(wedged == [], f"previews or readers never finished: {wedged}")
    failed = {key: value for key, value in previews.items() if value[0] != 200}
    check(len(previews) == 8 and failed == {}, f"every concurrent preview completed: {failed}")
    for key, (_status, body) in previews.items():
        check(json.loads(body).get("dry_run") is True, f"{key} was a preview: {body[:120]}")
    check(reads and all(status == 200 for status, _body in reads),
          f"every read during the previews answered: {[r for r in reads if r[0] != 200][:3]}")
    board.assert_clean("after the concurrent previews")
    _after, revision_after = board.document()
    check(revision_after == revision, f"previews changed nothing: revision {revision} -> {revision_after}")

    status, body = http_get(base, "/api/tickets/PGU-1", timeout=10)
    check(status == 200 and json.loads(body)["id"] == "PGU-1", f"an ordinary read: {status} {body[:120]}")
    status, body = http_get(base, "/api/board", timeout=10)
    check(status == 200, f"the board: {status} {body[:120]}")

    # One sequential runtime change, as set-role-runtime makes it: preview, then apply.
    role = roles[0]
    target = other_runtime(document, role)
    for dry_run in (True, False):
        status, body = http_post(base, "/api/tickets/actions/configure_workflow",
                                 preview_payload(document, revision, role, target, dry_run=dry_run), timeout=30)
        check(status == 200, f"sequential change (dry_run={dry_run}): {status} {body[:200]}")
    changed, revision_changed = board.document()
    # Revisions come from a sequence, and a sequence is not rolled back: every
    # preview above spent a number. So the change is newer, not exactly +1.
    check(revision_changed > revision, f"the change applied: {revision} -> {revision_changed}")
    check(next(r for r in changed["roles"] if r["name"] == role)["runtime"] == target, "and moved the role")
    board.assert_clean("after the sequential change")


def case_a_read_never_waits_on_a_second_connection(board: Board) -> None:
    """The MEFP wedge, made deterministic.

    The read takes its share lock on `tickets`; a preview then takes
    `workflow_stages` and queues for `tickets`; only then does the read go on to
    validate the row's state. With that lookup on a second connection the two
    wait on each other where PostgreSQL cannot see it. On the read's own
    connection, the deadlock is PostgreSQL's to detect and the preview's to retry.
    """
    document, revision = board.document()
    role = board.implementers()[0]
    validated = validate(role_runtime.document_with_runtime(document, role=role, runtime=other_runtime(document, role)),
                         project="cerulean")
    holding = threading.Event()
    reader_thread = threading.current_thread
    results: dict[str, Any] = {}
    original = app_module.select_ticket_rows
    gate = threading.local()

    def preview_queued() -> bool:
        return any(s["wait"] == "Lock" and "apply_declared_workflow" in str(s["query"]) for s in board.sessions())

    def staged_rows(conn: Any, ticket_id: str | None = None):
        rows = original(conn, ticket_id=ticket_id)
        if getattr(gate, "staged", False):
            holding.set()
            # Hold here, inside the read's transaction, until the preview has
            # taken workflow_stages and is waiting for tickets.
            results["queued"] = wait_until(preview_queued, 10)
            # Another request (any /api/board does) empties the shared state
            # cache now. Validation must use the names this read already took,
            # not look them up again -- after `tickets`, against the apply's order.
            board.app._workflow_states_cache = None
        return rows

    def read() -> None:
        gate.staged = True
        board.app._workflow_states_cache = None
        try:
            results["read"] = board.app.get_ticket("PGU-1")
        except BaseException as exc:  # noqa: BLE001
            results["read"] = exc

    def preview() -> None:
        holding.wait(10)
        try:
            results["preview"] = board.app.apply_workflow(validated, expected_revision=revision, dry_run=True,
                                                          caller_role="director")
        except BaseException as exc:  # noqa: BLE001
            results["preview"] = exc

    del reader_thread
    app_module.select_ticket_rows = staged_rows
    # One attempt: a read that takes tickets before workflow_stages makes this a
    # detected deadlock, which a retry would paper over. Only the apply's own
    # lock order lets the preview through first time.
    saved_attempts = getattr(workflow_locking, "ATTEMPTS", None)
    if saved_attempts is not None:
        workflow_locking.ATTEMPTS = 1
    try:
        threads = [threading.Thread(target=read, name="read"), threading.Thread(target=preview, name="preview")]
        for thread in threads:
            thread.start()
        wedged = bounded(threads, 20)
        if wedged:
            board.unwedge()
            bounded(threads, 10)
    finally:
        app_module.select_ticket_rows = original
        if saved_attempts is not None:
            workflow_locking.ATTEMPTS = saved_attempts
    check(results.get("queued") is True, f"the staging reached the MEFP shape: {results}")
    check(wedged == [], f"a ticket read and a preview wedged each other: {wedged}")
    check(isinstance(results.get("read"), dict) and results["read"]["id"] == "PGU-1",
          f"the read returned its ticket: {results.get('read')!r}")
    outcome = results.get("preview")
    check(isinstance(outcome, dict) and outcome.get("dry_run") is True,
          f"the preview completed first time, no deadlock: {outcome!r}")
    board.assert_clean("after the staged read and preview")


def case_a_blocked_preview_refuses_and_never_holds_readers(board: Board) -> None:
    """Something outside the board keeps a share lock on `tickets` (a report, a psql).

    The preview cannot take its exclusive lock. It must give up within its
    bound and say so, and the reads arriving meanwhile must not queue behind it
    indefinitely.
    """
    document, revision = board.document()
    role = board.implementers()[0]
    validated = validate(role_runtime.document_with_runtime(document, role=role, runtime=other_runtime(document, role)),
                         project="cerulean")
    results: dict[str, Any] = {}
    holder = psycopg.connect(board.admin)
    try:
        holder.execute("SELECT count(*) FROM ticket_board.tickets").fetchone()  # share lock, left open

        def preview() -> None:
            started = time.monotonic()
            try:
                results["preview"] = board.app.apply_workflow(validated, expected_revision=revision, dry_run=True,
                                                              caller_role="director")
            except BaseException as exc:  # noqa: BLE001
                results["preview"] = exc
            results["preview_seconds"] = time.monotonic() - started

        def read() -> None:
            started = time.monotonic()
            try:
                results["read"] = board.app.get_ticket("PGU-2")
            except BaseException as exc:  # noqa: BLE001
                results["read"] = exc
            results["read_seconds"] = time.monotonic() - started

        preview_thread = threading.Thread(target=preview, name="preview")
        preview_thread.start()
        queued = wait_until(lambda: any(s["wait"] == "Lock" and "apply_declared_workflow" in str(s["query"])
                                        for s in board.sessions()), 10)
        read_thread = threading.Thread(target=read, name="read")
        read_thread.start()
        wedged = bounded([preview_thread, read_thread], 30)
        if wedged:
            holder.rollback()
            bounded([preview_thread, read_thread], 15)
    finally:
        holder.rollback()
        holder.close()
    check(queued, "the preview reached its wait for tickets")
    check(wedged == [], f"a preview held readers behind a lock it could not get: {wedged} {results}")
    outcome = results.get("preview")
    check(isinstance(outcome, Exception) and "nothing was changed" in str(outcome),
          f"the preview refused, saying nothing changed: {outcome!r}")
    check(isinstance(results.get("read"), dict) and results["read"]["id"] == "PGU-2",
          f"the read during the wait returned its ticket: {results.get('read')!r}")
    board.assert_clean("after the refused preview")
    _after, revision_after = board.document()
    check(revision_after == revision, "the refused preview changed nothing")


def case_a_brief_hold_is_retried_not_refused(board: Board) -> None:
    """A share lock released soon after the preview's table-lock bound: the retry gets through."""
    document, revision = board.document()
    role = board.implementers()[0]
    validated = validate(role_runtime.document_with_runtime(document, role=role, runtime=other_runtime(document, role)),
                         project="cerulean")
    saved = getattr(workflow_locking, "TABLE_LOCK_WAIT_SECONDS", None)
    if saved is not None:
        workflow_locking.TABLE_LOCK_WAIT_SECONDS = 1.0
    holder = psycopg.connect(board.admin)
    results: dict[str, Any] = {}
    try:
        holder.execute("SELECT count(*) FROM ticket_board.tickets").fetchone()
        release = threading.Timer(1.6, holder.rollback)
        release.start()
        started = time.monotonic()
        try:
            results["preview"] = board.app.apply_workflow(validated, expected_revision=revision, dry_run=True,
                                                          caller_role="director")
        except BaseException as exc:  # noqa: BLE001
            results["preview"] = exc
        results["seconds"] = time.monotonic() - started
        release.join()
    finally:
        if saved is not None:
            workflow_locking.TABLE_LOCK_WAIT_SECONDS = saved
        holder.rollback()
        holder.close()
    outcome = results["preview"]
    check(isinstance(outcome, dict) and outcome.get("dry_run") is True,
          f"the preview got through once the hold ended: {outcome!r}")
    check(results["seconds"] > 1.0, f"its first attempt timed out and a retry succeeded: {results['seconds']:.2f}s")
    board.assert_clean("after the retried preview")


def case_a_ticket_read_uses_one_connection(board: Board) -> None:
    """The invariant behind the wedge: a read never opens a second connection inside its transaction."""
    opened: list[str] = []
    real = board.app._pg_connect

    def counting() -> Any:
        opened.append(threading.current_thread().name)
        return real()

    board.app._pg_connect = counting
    try:
        board.app._workflow_states_cache = None
        ticket = board.app.get_ticket("PGU-7")
    finally:
        del board.app._pg_connect
    check(ticket["id"] == "PGU-7" and ticket["assignee"] == "uiux", ticket)
    check(len(opened) == 1,
          f"a read of a custom implementer's ticket, state cache empty, opened {len(opened)} connections")


def case_a_statement_bound_is_applied(board: Board) -> None:
    """The apply's statement bound is really set: an absurdly small one refuses the preview."""
    document, revision = board.document()
    role = board.implementers()[0]
    validated = validate(role_runtime.document_with_runtime(document, role=role, runtime=other_runtime(document, role)),
                         project="cerulean")
    saved = getattr(workflow_locking, "STATEMENT_SECONDS", None)
    if saved is not None:
        workflow_locking.STATEMENT_SECONDS = 0.001
    try:
        try:
            outcome: Any = board.app.apply_workflow(validated, expected_revision=revision, dry_run=True,
                                                    caller_role="director")
        except BaseException as exc:  # noqa: BLE001
            outcome = exc
    finally:
        if saved is not None:
            workflow_locking.STATEMENT_SECONDS = saved
    check(isinstance(outcome, Exception) and "QueryCanceled" in str(outcome) and "nothing was changed" in str(outcome),
          f"a 1 ms statement bound refused the preview: {outcome!r}")
    board.assert_clean("after the statement-bounded preview")


CASES = (
    case_concurrent_previews_then_a_read_then_a_change,
    case_a_read_never_waits_on_a_second_connection,
    case_a_blocked_preview_refuses_and_never_holds_readers,
    case_a_brief_hold_is_retried_not_refused,
    case_a_ticket_read_uses_one_connection,
    case_a_statement_bound_is_applied,
)


def main() -> int:
    failures = 0
    with temporary_cluster(prefix="syrd572-", shutdown="immediate") as cluster:
        for index, case in enumerate(CASES):
            board = Board(cluster, f"{DB_PREFIX}{index}")
            try:
                case(board)
                print(f"PASS {case.__name__}", flush=True)
            except AssertionError as exc:
                failures += 1
                print(f"FAILED {case.__name__}: AssertionError: {exc}", flush=True)
            except BaseException as exc:  # noqa: BLE001
                failures += 1
                print(f"FAILED {case.__name__}: {type(exc).__name__}: {exc}", flush=True)
            finally:
                board.close()
    print(f"workflow_preview_locking_test: {CHECKS} checks, {failures} failed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
