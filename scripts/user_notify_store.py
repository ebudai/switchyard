"""The phone bridge's durable outbox: root's SQLite file under /var/lib/switchyard/user-notify (SYRD-714).

One row per event, keyed by (project, the board's stable key), so an event read again -- after a restart, a lost
answer, or a board restored from a dump -- is never a second row. Each tenant's cursor moves in the same
transaction as the rows it read, so a crash between the two cannot lose or repeat one.

A row is `pending` until it is `sent` or `dead`; `suppressed` (an opt-out, the global disable, already resolved) and
`drained` (the drain control) are never sent. Every row keeps why. Sending is at-least-once: an attempt is recorded
before the provider is asked, so a crash mid-send retries it at its next due time rather than losing it.

Retries back off exponentially from 30 s to an hour, at most MAX_ATTEMPTS times, and an event still unsent a day
after it was recorded is dead as expired: a phone alert that late is noise. `due` picks the next messages fairly: one
per tenant per round, the tenant served least recently first, so a noisy tenant cannot starve another.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

STATES = ("pending", "sent", "dead", "suppressed", "drained")
MAX_ATTEMPTS = 8
FIRST_RETRY_SECONDS, LAST_RETRY_SECONDS = 30.0, 3600.0
EXPIRY_SECONDS = 86400.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    project TEXT PRIMARY KEY,
    after INTEGER,
    ok_at REAL,
    failing_since REAL,
    error TEXT NOT NULL DEFAULT '',
    served_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    key TEXT NOT NULL,
    kind TEXT NOT NULL,
    ticket TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    requester TEXT NOT NULL DEFAULT '',
    digest TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'dead', 'suppressed', 'drained')),
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL DEFAULT 0,
    receipt TEXT NOT NULL DEFAULT '',
    finished REAL,
    UNIQUE (project, key)
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox (state, due);
"""


def backoff(attempts: int) -> float:
    """Seconds before the next try, after `attempts` tries."""
    return min(FIRST_RETRY_SECONDS * 2 ** max(attempts - 1, 0), LAST_RETRY_SECONDS)


class Outbox:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30.0)
        self.db.row_factory = sqlite3.Row
        if fresh:
            os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def _write(self, statements: Callable[[sqlite3.Connection], Any]) -> Any:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = statements(self.db)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")
        return result

    # -- tenants ------------------------------------------------------------------------------------------------------
    def tenant(self, project: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM tenants WHERE project = ?", (project,)).fetchone()
        return dict(row) if row else {"project": project, "after": None, "ok_at": None, "failing_since": None,
                                      "error": "", "served_at": 0.0}

    def tenants(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute("SELECT * FROM tenants ORDER BY project")]

    def note_failure(self, project: str, error: str) -> float:
        """Record a failed read; returns since when the tenant has been failing."""
        now = self.clock()

        def write(db: sqlite3.Connection) -> float:
            db.execute("INSERT INTO tenants (project, failing_since, error) VALUES (?, ?, ?) ON CONFLICT (project) "
                       "DO UPDATE SET failing_since = coalesce(failing_since, excluded.failing_since), "
                       "error = excluded.error", (project, now, error))
            return float(db.execute("SELECT failing_since FROM tenants WHERE project = ?", (project,)).fetchone()[0])
        return self._write(write)

    def ingest(self, project: str, latest: int, events: Iterable[dict[str, Any]],
               decide: Callable[[dict[str, Any]], tuple[str, str]], *, error: str = "") -> int:
        """Record what one read returned and move the cursor, in one transaction; returns rows added.

        A tenant seen for the first time joins at the board's latest event: what happened before is history.
        `latest` below the cursor means the board's record was recreated; the tenant joins again at its latest."""
        now = self.clock()

        def write(db: sqlite3.Connection) -> int:
            row = db.execute("SELECT after FROM tenants WHERE project = ?", (project,)).fetchone()
            known = row is not None and row[0] is not None and latest >= int(row[0])
            after = int(row[0]) if known else latest
            added = 0
            if known:
                for event in events:
                    state, reason = decide(event)
                    cursor = db.execute(
                        "INSERT INTO outbox (project, key, kind, ticket, title, detail, message, requester, digest, "
                        "created, state, reason, due) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT (project, key) DO NOTHING",
                        (project, event["key"], event["kind"], event.get("ticket", ""), event.get("title", ""),
                         event.get("detail", ""), event.get("message", ""), event.get("requester", ""),
                         event.get("digest", ""), float(event.get("created") or now), state, reason, now))
                    added += cursor.rowcount
                    after = max(after, int(event["id"]))
            db.execute("INSERT INTO tenants (project, after, ok_at, failing_since, error) VALUES (?, ?, ?, NULL, ?) "
                       "ON CONFLICT (project) DO UPDATE SET after = excluded.after, ok_at = excluded.ok_at, "
                       "failing_since = NULL, error = excluded.error", (project, after, now, error))
            return added
        return self._write(write)

    # -- rows ---------------------------------------------------------------------------------------------------------
    def add(self, project: str, key: str, kind: str, *, state: str, reason: str = "", ticket: str = "",
            title: str = "", detail: str = "") -> int | None:
        """One event the bridge itself observed (an outage, a test); None when the key is already recorded."""
        now = self.clock()

        def write(db: sqlite3.Connection) -> int | None:
            cursor = db.execute(
                "INSERT INTO outbox (project, key, kind, ticket, title, detail, created, state, reason, due) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (project, key) DO NOTHING",
                (project, key, kind, ticket, title, detail, now, state, reason, now))
            return cursor.lastrowid if cursor.rowcount else None
        return self._write(write)

    def row(self, seq: int) -> dict[str, Any]:
        return dict(self.db.execute("SELECT * FROM outbox WHERE seq = ?", (seq,)).fetchone())

    def expire(self) -> int:
        now = self.clock()
        return self._write(lambda db: db.execute(
            "UPDATE outbox SET state = 'dead', reason = 'expired: still unsent a day after it was recorded', finished = ? "
            "WHERE state = 'pending' AND created < ?", (now, now - EXPIRY_SECONDS)).rowcount)

    def due(self, limit: int) -> list[dict[str, Any]]:
        """Up to `limit` pending rows now due: round-robin across tenants, least recently served first."""
        now = self.clock()
        rows = self.db.execute(
            "SELECT o.*, coalesce(t.served_at, 0) AS served_at FROM (SELECT *, row_number() OVER "
            "(PARTITION BY project ORDER BY seq) AS turn FROM outbox WHERE state = 'pending' AND due <= ?) o "
            "LEFT JOIN tenants t ON t.project = o.project WHERE o.turn <= ? ORDER BY o.turn, served_at, o.project",
            (now, limit)).fetchall()
        return [dict(row) for row in rows[:limit]]

    def attempt(self, seq: int) -> int:
        """Record a try before it is made; returns the attempt's number."""
        now = self.clock()

        def write(db: sqlite3.Connection) -> int:
            attempts = int(db.execute("SELECT attempts FROM outbox WHERE seq = ?", (seq,)).fetchone()[0]) + 1
            db.execute("UPDATE outbox SET attempts = ?, due = ? WHERE seq = ?", (attempts, now + backoff(attempts), seq))
            db.execute("UPDATE tenants SET served_at = ? WHERE project = (SELECT project FROM outbox WHERE seq = ?)",
                       (now, seq))
            return attempts
        return self._write(write)

    def finish(self, seq: int, kind: str, detail: str, receipt: str = "") -> str:
        """Record how a try went; returns the row's state."""
        now = self.clock()

        def write(db: sqlite3.Connection) -> str:
            attempts = int(db.execute("SELECT attempts FROM outbox WHERE seq = ?", (seq,)).fetchone()[0])
            reason = detail
            if kind in ("sent", "suppressed"):
                state = kind
            elif kind == "retry" and attempts < MAX_ATTEMPTS:
                state = "pending"
            else:
                state = "dead"
                reason = detail if kind == "dead" else f"gave up after {attempts} attempts: {detail}"
            db.execute("UPDATE outbox SET state = ?, reason = ?, receipt = ?, finished = ? WHERE seq = ?",
                       (state, reason, receipt, None if state == "pending" else now, seq))
            return state
        return self._write(write)

    def drain(self, reason: str) -> int:
        now = self.clock()
        return self._write(lambda db: db.execute(
            "UPDATE outbox SET state = 'drained', reason = ?, finished = ? WHERE state = 'pending'",
            (reason, now)).rowcount)

    def recorded_since(self, project: str, kind: str, since: float) -> int:
        """How many of this tenant's events of this kind were recorded to be sent since then."""
        return int(self.db.execute("SELECT count(*) FROM outbox WHERE project = ? AND kind = ? AND created >= ? "
                                   "AND state IN ('pending', 'sent', 'dead')", (project, kind, since)).fetchone()[0])

    def counts(self) -> dict[str, int]:
        found = {state: 0 for state in STATES}
        for row in self.db.execute("SELECT state, count(*) FROM outbox GROUP BY state"):
            found[row[0]] = int(row[1])
        return found

    def recent(self, state: str, limit: int = 10) -> list[dict[str, Any]]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM outbox WHERE state = ? ORDER BY seq DESC LIMIT ?", (state, limit))]
