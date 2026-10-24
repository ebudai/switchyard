"""The Oracle board's schema and its ordered, restart-safe migrations (SYRD-642).

Oracle commits DDL as it goes, so a migration cannot be one transaction the
way a PostgreSQL migration file can. Each migration here is an ordered list of
steps, and each step is one statement with a probe that says whether it has
already happened. `migrate` records every step it completes in
`board_schema_migrations`. When a step fails it stops and raises
`MigrationFailed`, naming the migration, the step, the statement and Oracle's
own error, and saying which steps are already in place. The next run resumes
at exactly that step: a step whose effect is present but which was not yet
recorded (a crash between the DDL and the record) is recognised by its probe
and recorded rather than run twice. Nothing is ever dropped or rolled back
silently.

Concurrent runs are serialised by a row lock held on a separate connection, so
the lock survives the implicit commits of the DDL connection.

The schema is deliberately narrow: tables, keys, sequences and row locks only,
with no stored procedures, triggers or Oracle-specific rules. The board's rules
live in the board process (`workflow_engine`, `oracle_app`). Oracle's empty
string is NULL, so optional text is nullable here and read back as "".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .oracle_config import OracleConfig, connect

TSTZ = "TIMESTAMP WITH TIME ZONE"


@dataclass(frozen=True)
class Step:
    sql: str
    probe: str  # a query whose first column is > 0 when this step's effect is already present


def _table(name: str, body: str) -> Step:
    return Step(f"CREATE TABLE {name} ({body})",
                f"SELECT count(*) FROM user_tables WHERE table_name = '{name.upper()}'")


def _index(name: str, sql: str) -> Step:
    return Step(sql, f"SELECT count(*) FROM user_indexes WHERE index_name = '{name.upper()}'")


def _sequence(name: str) -> Step:
    return Step(f"CREATE SEQUENCE {name} START WITH 1 INCREMENT BY 1 NOCACHE",
                f"SELECT count(*) FROM user_sequences WHERE sequence_name = '{name.upper()}'")


def _column(table: str, column: str, definition: str) -> Step:
    return Step(f"ALTER TABLE {table} ADD ({column} {definition})",
                f"SELECT count(*) FROM user_tab_columns WHERE table_name = '{table.upper()}' "
                f"AND column_name = '{column.upper()}'")


#: Every migration, in order. Never edit a published one: add the next.
MIGRATIONS: list[tuple[str, list[Step]]] = [
    ("ora0001_core", [
        _table("board_meta", "k VARCHAR2(64) PRIMARY KEY, v VARCHAR2(4000)"),
        _table("workflow_document",
               f"id NUMBER(1) DEFAULT 1 PRIMARY KEY CHECK (id = 1), revision NUMBER NOT NULL, "
               f"document CLOB NOT NULL CHECK (document IS JSON), updated_at {TSTZ} NOT NULL"),
        _sequence("ticket_number_seq"),
        _table("tickets",
               f"id VARCHAR2(64) PRIMARY KEY, ticket_number NUMBER NOT NULL UNIQUE, title VARCHAR2(1000) NOT NULL, "
               f"body CLOB, state VARCHAR2(64) NOT NULL, assignee VARCHAR2(64) NOT NULL, commit_hash VARCHAR2(64), "
               f"commit_exempt NUMBER(1) DEFAULT 0 NOT NULL, manually_controlled NUMBER(1) DEFAULT 0 NOT NULL, "
               f"parked NUMBER(1) DEFAULT 0 NOT NULL, flags CLOB NOT NULL CHECK (flags IS JSON), "
               f"parent_id VARCHAR2(64), implementation CLOB, audit_prompt CLOB, last_implementer VARCHAR2(64), "
               f"revision NUMBER DEFAULT 1 NOT NULL, created_at {TSTZ} NOT NULL, updated_at {TSTZ} NOT NULL, "
               f"entered_state_at {TSTZ} NOT NULL"),
        _index("tickets_state_idx", "CREATE INDEX tickets_state_idx ON tickets (state, assignee)"),
        _table("ticket_comments",
               f"id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ticket_id VARCHAR2(64) NOT NULL "
               f"REFERENCES tickets(id) ON DELETE CASCADE, position NUMBER NOT NULL, who VARCHAR2(64) NOT NULL, "
               f"text CLOB NOT NULL, urgent NUMBER(1) DEFAULT 0 NOT NULL, created_at {TSTZ} NOT NULL, "
               f"CONSTRAINT ticket_comments_position UNIQUE (ticket_id, position)"),
        _table("ticket_blockers",
               "ticket_id VARCHAR2(64) NOT NULL REFERENCES tickets(id) ON DELETE CASCADE, "
               "blocker VARCHAR2(200) NOT NULL, position NUMBER NOT NULL, resolved NUMBER(1) DEFAULT 0 NOT NULL, "
               "CONSTRAINT ticket_blockers_pk PRIMARY KEY (ticket_id, blocker)"),
    ]),
    ("ora0002_notifications", [
        _table("notification_queue",
               f"id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ticket_id VARCHAR2(64) NOT NULL "
               f"REFERENCES tickets(id) ON DELETE CASCADE, kind VARCHAR2(64) NOT NULL, "
               f"target_role VARCHAR2(64) NOT NULL, message CLOB NOT NULL, payload CLOB NOT NULL CHECK (payload IS JSON), "
               f"dedupe_key VARCHAR2(400) NOT NULL UNIQUE, attempts NUMBER DEFAULT 0 NOT NULL, "
               f"next_attempt_at {TSTZ} NOT NULL, claimed_at {TSTZ}, last_error VARCHAR2(4000), "
               f"created_at {TSTZ} NOT NULL"),
        _index("notification_queue_due_idx", "CREATE INDEX notification_queue_due_idx ON notification_queue "
                                             "(next_attempt_at, id)"),
        _table("notification_trace",
               f"id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ts {TSTZ} NOT NULL, ticket_id VARCHAR2(64), "
               f"notification_id NUMBER, target_role VARCHAR2(64), kind VARCHAR2(64), event VARCHAR2(64) NOT NULL, "
               f"detail VARCHAR2(4000)"),
    ]),
    ("ora0003_runtime_and_claims", [
        _table("role_runtime_assignments",
               f"role VARCHAR2(64) PRIMARY KEY, runtime VARCHAR2(32) NOT NULL, actual_target VARCHAR2(200) NOT NULL "
               f"UNIQUE, worktree VARCHAR2(1000) NOT NULL, session_dir VARCHAR2(1000) NOT NULL, "
               f"process_pid NUMBER NOT NULL CHECK (process_pid > 1), "
               f"process_start_time NUMBER NOT NULL CHECK (process_start_time > 0), "
               f"process_uid NUMBER NOT NULL CHECK (process_uid >= 0), "
               f"generation NUMBER DEFAULT 1 NOT NULL CHECK (generation > 0), registered_at {TSTZ} NOT NULL, "
               f"CONSTRAINT runtime_process UNIQUE (process_pid, process_start_time, process_uid)"),
        _table("pull_claims",
               f"id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ticket_id VARCHAR2(64) NOT NULL "
               f"REFERENCES tickets(id) ON DELETE CASCADE, role VARCHAR2(64) NOT NULL, via VARCHAR2(16) NOT NULL, "
               f"claimed_at {TSTZ} NOT NULL"),
        _column("tickets", "queued_for_assignee", "VARCHAR2(64)"),
    ]),
    # One row per implementer, locked while its active ticket is decided: PostgreSQL's
    # implementer_assignment_lock_key advisory lock, for an account that holds rows only.
    ("ora0004_implementer_locks", [
        _table("implementer_locks", "role VARCHAR2(64) PRIMARY KEY"),
    ]),
]

#: What the steady-state account may do on each object: rows only, never DDL.
RUNTIME_GRANTS = {
    "board_meta": "SELECT", "workflow_document": "SELECT, INSERT, UPDATE",
    "tickets": "SELECT, INSERT, UPDATE", "ticket_comments": "SELECT, INSERT",
    "ticket_blockers": "SELECT, INSERT, UPDATE, DELETE",
    "notification_queue": "SELECT, INSERT, UPDATE, DELETE", "notification_trace": "SELECT, INSERT",
    "role_runtime_assignments": "SELECT, INSERT, UPDATE", "pull_claims": "SELECT, INSERT",
    "implementer_locks": "SELECT, INSERT, UPDATE",
    "ticket_number_seq": "SELECT", "board_schema_migrations": "SELECT",
}

LEDGER = [
    _table("board_schema_migrations",
           f"name VARCHAR2(200) NOT NULL, step NUMBER NOT NULL, applied_at {TSTZ} NOT NULL, "
           "CONSTRAINT board_schema_migrations_pk PRIMARY KEY (name, step)"),
    _table("board_schema_lock", "id NUMBER(1) PRIMARY KEY CHECK (id = 1)"),
]


class MigrationFailed(RuntimeError):
    """One step failed: what is in place, what failed, and that a rerun resumes there."""


def _scalar(cursor: Any, sql: str) -> int:
    row = cursor.execute(sql).fetchone()
    return int(row[0]) if row else 0


def applied_steps(cursor: Any) -> dict[str, set[int]]:
    # The board's schema, not the session user's: status reads it as the runtime account.
    if not _scalar(cursor, "SELECT count(*) FROM all_tables WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
                           "AND table_name = 'BOARD_SCHEMA_MIGRATIONS'"):
        return {}
    done: dict[str, set[int]] = {}
    for name, step in cursor.execute("SELECT name, step FROM board_schema_migrations"):
        done.setdefault(name, set()).add(int(step))
    return done


def pending(cursor: Any) -> list[tuple[str, int]]:
    """Every (migration, step) not yet recorded, in order."""
    done = applied_steps(cursor)
    return [(name, index) for name, steps in MIGRATIONS for index in range(len(steps))
            if index not in done.get(name, set())]


def migrate(config: OracleConfig, *, project: str, ticket_prefix: str,
            report: Callable[[str], None] = print, connector: Callable[[OracleConfig], Any] = connect) -> list[str]:
    """Bring the board's schema up to date as its owner, then grant the steady-state account. Returns the steps run."""
    config = config.as_bootstrap()
    if config.credentials()[0] != config.schema:
        raise MigrationFailed(f"migrate connects as the schema owner: bootstrap_credentials_file must name the "
                              f"account {config.schema}")
    runtime_user = config.runtime_user()
    if runtime_user == config.schema:
        raise MigrationFailed("the runtime account must not be the schema owner: it is to hold rows only")
    ran: list[str] = []
    ddl = connector(config)
    lock = connector(config)
    try:
        cursor = ddl.cursor()
        for step in LEDGER:
            if not _scalar(cursor, step.probe):
                cursor.execute(step.sql)
        lock_cursor = lock.cursor()
        lock_cursor.execute("MERGE INTO board_schema_lock l USING dual ON (l.id = 1) "
                            "WHEN NOT MATCHED THEN INSERT (id) VALUES (1)")
        lock.commit()
        lock_cursor.execute("SELECT id FROM board_schema_lock WHERE id = 1 FOR UPDATE")
        done = applied_steps(cursor)
        for name, steps in MIGRATIONS:
            for index, step in enumerate(steps):
                if index in done.get(name, set()):
                    continue
                label = f"{name} step {index + 1}/{len(steps)}"
                try:
                    if _scalar(cursor, step.probe):
                        report(f"{label}: already in place, recorded")
                    else:
                        cursor.execute(step.sql)
                        report(f"{label}: applied")
                        ran.append(label)
                    cursor.execute("INSERT INTO board_schema_migrations (name, step, applied_at) "
                                   "VALUES (:name, :step, SYSTIMESTAMP)", name=name, step=index)
                    ddl.commit()
                except Exception as exc:
                    ddl.rollback()
                    in_place = sum(len(v) for v in applied_steps(cursor).values())
                    raise MigrationFailed(
                        f"{label} failed: {str(exc).splitlines()[0]}. Statement: {step.sql[:200]}. {in_place} step(s) "
                        "are in place and recorded; nothing was rolled back or dropped. Fix the cause and run "
                        "migrate again: it resumes at this step.") from None
        for key, value in (("project", project), ("ticket_prefix", ticket_prefix)):
            cursor.execute("MERGE INTO board_meta m USING dual ON (m.k = :k) WHEN NOT MATCHED THEN "
                           "INSERT (k, v) VALUES (:k, :v)", k=key, v=value)
        # Grant on what the applied migrations created: an object a later migration adds is granted when
        # that migration has run, so an older or partly-migrated schema is granted exactly what it has.
        present = {row[0].lower() for row in cursor.execute("SELECT object_name FROM user_objects "
                                                             "WHERE object_type IN ('TABLE', 'SEQUENCE')")}
        for name, privileges in RUNTIME_GRANTS.items():
            if name in present:
                cursor.execute(f"GRANT {privileges} ON {name} TO {runtime_user}")
        ddl.commit()
    finally:
        try:
            lock.rollback()
        finally:
            lock.close()
            ddl.close()
    return ran
