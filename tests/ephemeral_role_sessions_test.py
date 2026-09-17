#!/usr/bin/env python3
"""SYRD-135: an ephemeral role starts each ticket on a cleared session.

A role that carries the last ticket's conversation into the next one reasons
about the wrong ticket, and does it confidently. The fix is one declared
boolean and one extra thing the listener sends: before the first notification
that hands a ticket to an ephemeral role, that role's CLI is told to clear.

What is actually hard here is everything around "first". The listener retries,
restarts, and re-reads the same queue; the same ticket comes back to the same
role after a kick-back; comments and idle reminders arrive on work already in
hand; and a role that is busy must not have its conversation deleted out from
under it. So the clear is recorded in the database rather than in the process,
recorded only after it has actually happened, and sent only once every delivery
gate has already agreed this role is free to take this ticket.

Both halves are exercised: the board's own durable state and validator against
a real cluster, and the listener's delivery order against a real
TicketBoardNotifyListener.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_notify_listener_test as lt  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

from scripts.ticket_board.notify_listener import (  # noqa: E402
    SESSION_CLEAR_COMMANDS,
    SESSION_CLEAR_KINDS,
    SESSION_CLEAR_UNSUPPORTED_RUNTIME_ERROR,
    TicketBoardNotifyListener,
)
from scripts.ticket_board.workflow_config import (  # noqa: E402
    RUNTIMES,
    ephemeral_roles,
    validate,
)

MIGRATION = "pgu940_syrd135_ephemeral_role_sessions.sql"
CANONICAL = json.loads((ROOT / "examples/workflows/inspection.json").read_text())


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------


def document(*, ephemeral: tuple[str, ...] = (), runtimes: dict[str, str] | None = None) -> dict[str, Any]:
    """The canonical example, retargeted at the listener's default project."""
    cfg = copy.deepcopy(CANONICAL)
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        role.pop("ephemeral", None)
        if role.get("target"):
            role["target"] = f"pgu-{role['name']}:0.0"
        if role["name"] in ephemeral:
            role["ephemeral"] = True
        if runtimes and role["name"] in runtimes:
            role["runtime"] = runtimes[role["name"]]
    return cfg


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout


def schema_before_this_change() -> str:
    """schema.sql as it stood before this migration joined the tree.

    Once the change is committed that is the parent of the commit that added
    the migration. Before then it is HEAD, which is the same release for the
    same reason: the working tree is the only place this change exists.
    """
    adding = git(
        "log", "--format=%H", "--diff-filter=A", "--",
        f"scripts/ticket_board/migrations/{MIGRATION}",
    ).strip().splitlines()
    ref = f"{adding[-1]}^" if adding else "HEAD"
    return git("show", f"{ref}:scripts/ticket_board/schema.sql")


# --------------------------------------------------------------------------
# the listener half
# --------------------------------------------------------------------------


class WorkflowConnection(lt.FakeConnection):
    """A FakeConnection that also answers the questions this change asks.

    The declared document, so the listener can see which roles are ephemeral
    and what each of them is running, and the two clear-state functions, whose
    rows are kept here exactly as the table keeps them: one per (ticket, role),
    written only when the listener says the clear succeeded.
    """

    def __init__(
        self,
        queue_rows: list[tuple[int, str, str, str, str, int]],
        *,
        cfg: dict[str, Any],
        cleared: set[tuple[str, str]] | None = None,
        queue_identity: tuple[str, str] = ("", ""),
        **kwargs: Any,
    ) -> None:
        super().__init__(queue_rows, **kwargs)
        self.cfg = cfg
        self.queue_identity = queue_identity
        self.cleared: set[tuple[str, str]] = set(cleared or ())
        self.clear_reads: list[tuple[str, str]] = []
        self.clear_writes: list[tuple[str, str]] = []

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> lt.FakeResult:
        text = str(statement)
        result = super().execute(statement, params)
        if "to_regclass" in text:
            return lt.FakeResult([("ticket_board.workflow_configuration",)])
        if "FROM ticket_board.workflow_configuration WHERE singleton" in text:
            return lt.FakeResult([(json.dumps(self.cfg),)])
        if "role_session_clear_pending" in text:
            assert params is not None
            pair = (str(params[0]), str(params[1]))
            self.clear_reads.append(pair)
            return lt.FakeResult([(pair not in self.cleared,)])
        if "record_role_session_clear" in text:
            assert params is not None
            pair = (str(params[0]), str(params[1]))
            self.clear_writes.append(pair)
            first = pair not in self.cleared
            self.cleared.add(pair)
            return lt.FakeResult([(first,)])
        if "awaiting_since_at = %s::timestamptz" in text:
            # The handoff-still-open probe, and only that one: the superseding
            # query nearby reads five columns from the same table.
            return lt.FakeResult([(True,)])
        if "queued_for_assignee, queued_behind_ticket" in text:
            return lt.FakeResult([self.queue_identity])
        return result


def awaiting_row(
    notification_id: int,
    ticket_id: str,
    *,
    target_role: str,
    awaiting_role: str | None = None,
    attempts: int = 1,
) -> tuple[int, str, str, str, str, int]:
    """A durable handoff notification, the other way a ticket becomes a role's."""
    message = f"{ticket_id} is waiting on you"
    payload = json.dumps(
        {
            "kind": "awaiting_role",
            "id": ticket_id,
            "target_role": target_role,
            "awaiting_role": awaiting_role or target_role,
            "awaiting_since_at": "2026-09-13T00:00:00+00:00",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "message": message,
        }
    )
    return (notification_id, ticket_id, target_role, message, payload, attempts)


def queue_announcement_row(
    notification_id: int, ticket_id: str, *, target_role: str
) -> tuple[int, str, str, str, str, int]:
    """"Reserved for you, behind something else" -- not the ticket becoming theirs."""
    message = f"{ticket_id} is queued for {target_role}"
    payload = json.dumps(
        {
            "kind": "transition",
            "id": ticket_id,
            "target_role": target_role,
            "queued_for": target_role,
            "reserved_by": "PGU-1",
            "message": message,
        }
    )
    return (notification_id, ticket_id, target_role, message, payload, 1)


def drive(
    conn: WorkflowConnection,
    *,
    tmp_path: Path,
    idle_targets: tuple[str, ...] = ("pgu-ops:0.0",),
    sender: Any = None,
    sent: list[tuple[str, str]] | None = None,
    max_notifications: int = 1,
) -> int:
    store = lt.PaneHookStateStore(tmp_path)
    gate = lt.PaneActivityGate(
        state_store=store,
        cursor_position_runner=lt.constant_cursor_runner(),
        capture_pane_runner=lt.sequenced_capture_runner(""),
    )
    for target in idle_targets:
        store.write(target, "idle", source="codex.Stop", now=100.0)
    if sender is None:
        assert sent is not None

        def sender(target: str, message: str) -> None:
            sent.append((target, message))

    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=sender,
        activity_gate=gate.is_working,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
        session_clear_settle_seconds=0.0,
        target_exists=lambda _target: True,
    )
    return listener.listen_once(max_notifications=max_notifications)


def test_the_vocabulary_covers_every_runtime_the_document_accepts() -> None:
    """A runtime the board will accept and the listener cannot clear is a trap.

    The document's runtime list is the authority on what a tenant may declare.
    If the two ever drift, an ephemeral role on the new runtime would hold its
    notifications forever rather than be handed a ticket uncleared -- correct,
    and still a stall. This is the check that keeps the lists together.
    """
    assert set(SESSION_CLEAR_COMMANDS) == RUNTIMES, sorted(SESSION_CLEAR_COMMANDS)
    assert all(command.startswith("/") for command in SESSION_CLEAR_COMMANDS.values())


def test_clear_precedes_the_ticket_and_goes_to_the_same_pane() -> None:
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection([lt.queue_row(1, "PGU-501")], cfg=document(ephemeral=("ops",)))
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent) == 1
    assert sent == [
        ("pgu-ops:0.0", "/clear"),
        ("pgu-ops:0.0", "New ticket for you: PGU-501 -- Queue"),
    ], sent
    assert conn.clear_writes == [("PGU-501", "ops")], conn.clear_writes
    assert conn.acked == [1]
    assert "session_clear" in lt.trace_events(conn)


def test_a_non_ephemeral_role_is_never_cleared() -> None:
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection([lt.queue_row(2, "PGU-502")], cfg=document())
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent) == 1
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-502 -- Queue")], sent
    assert conn.clear_reads == [], conn.clear_reads
    assert conn.clear_writes == []


def test_the_same_ticket_clears_once_and_a_different_ticket_clears_again() -> None:
    """Once per (ticket, role), and the pair is what makes that sentence true."""
    sent: list[tuple[str, str]] = []
    cfg = document(ephemeral=("ops",))
    conn = WorkflowConnection(
        [
            lt.queue_row(3, "PGU-503"),
            lt.queue_row(4, "PGU-503", kind="ticket_update", message="PGU-503 -- new comment"),
            lt.queue_row(5, "PGU-503", message="PGU-503 kicked back to you"),
            lt.queue_row(6, "PGU-504"),
        ],
        cfg=cfg,
    )
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent, max_notifications=4) == 4
    assert sent == [
        ("pgu-ops:0.0", "/clear"),
        ("pgu-ops:0.0", "New ticket for you: PGU-503 -- Queue"),
        ("pgu-ops:0.0", "PGU-503 -- new comment"),
        ("pgu-ops:0.0", "PGU-503 kicked back to you"),
        ("pgu-ops:0.0", "/clear"),
        ("pgu-ops:0.0", "New ticket for you: PGU-504 -- Queue"),
    ], sent
    assert conn.clear_writes == [("PGU-503", "ops"), ("PGU-504", "ops")], conn.clear_writes


def test_a_restart_does_not_clear_the_same_pair_again() -> None:
    """The record is the board's, not the process's, so a restart reads it back."""
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection(
        [lt.queue_row(7, "PGU-505")],
        cfg=document(ephemeral=("ops",)),
        cleared={("PGU-505", "ops")},
    )
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent) == 1
    assert sent == [("pgu-ops:0.0", "New ticket for you: PGU-505 -- Queue")], sent
    assert conn.clear_reads == [("PGU-505", "ops")]
    assert conn.clear_writes == []


def test_reminders_and_comments_never_clear() -> None:
    """Everything that is about work already in hand.

    An idle reminder says "you look stopped on this" and a comment is somebody
    talking about the ticket the pane is holding. Clearing on either would
    delete the context of the very thing being asked about.
    """
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection(
        [
            lt.queue_row(8, "PGU-506", kind="idle_reminder", message="PGU-506 -- still yours?"),
            lt.queue_row(9, "PGU-506", kind="nudge", message="PGU-506 -- nudge"),
            lt.queue_row(10, "PGU-506", kind="ticket_update", message="PGU-506 -- new comment"),
        ],
        cfg=document(ephemeral=("ops",)),
    )
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent, max_notifications=3) == 3
    assert [message for _target, message in sent] == [
        "PGU-506 -- still yours?",
        "PGU-506 -- nudge",
        "PGU-506 -- new comment",
    ], sent
    assert conn.clear_reads == [], conn.clear_reads


def test_the_directors_escalation_about_somebody_else_never_clears() -> None:
    """The one kind that is about another role's stall, asked at the decision.

    Delivering to a director pane end to end needs the human-composing probes
    that gate it, and none of that is what this is about: the question is
    whether an escalation is ever a first handoff. It is not, and the answer is
    reached without the board being asked -- `clear_reads` stays empty, which
    is the short-circuit itself.
    """
    conn = WorkflowConnection([], cfg=document(ephemeral=("ops", "director")))
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: None,
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
    )
    listener.ephemeral_roles = {"ops", "director"}
    for kind in ("escalation", "nudge", "idle_reminder", "ticket_update", "triage", "publication_request"):
        payload = json.dumps({"kind": kind, "id": "PGU-506", "target_role": "director"})
        assert listener._session_clear_is_due(conn, "PGU-506", "director", kind, payload) is False, kind
    assert conn.clear_reads == [], conn.clear_reads
    assert SESSION_CLEAR_KINDS == {"transition", "awaiting_role"}


def test_a_durable_handoff_clears_the_awaited_role() -> None:
    """`await-role` hands the ticket to somebody else; that is a first handoff."""
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection(
        [awaiting_row(11, "PGU-507", target_role="ops")],
        cfg=document(ephemeral=("ops",)),
    )
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent) == 1
    assert sent == [
        ("pgu-ops:0.0", "/clear"),
        ("pgu-ops:0.0", "PGU-507 is waiting on you"),
    ], sent
    assert conn.clear_writes == [("PGU-507", "ops")]


def test_a_queue_announcement_does_not_clear_the_role_it_reserves() -> None:
    """Being told a ticket is reserved for you later is not it becoming yours.

    This is the serial-focus case the acceptance calls out: the announcement
    arrives while the role is still doing something else, and the ticket only
    becomes actionable when the reservation clears and the real handoff is
    delivered. Clearing on the announcement would erase the work in progress.
    """
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection(
        [queue_announcement_row(12, "PGU-508", target_role="ops")],
        cfg=document(ephemeral=("ops",)),
        queue_identity=("ops", "PGU-1"),
    )
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sent=sent) == 1
    assert sent == [("pgu-ops:0.0", "PGU-508 is queued for ops")], sent
    assert conn.clear_reads == [], conn.clear_reads


def test_a_busy_role_is_not_cleared_and_the_ticket_waits() -> None:
    sent: list[tuple[str, str]] = []
    conn = WorkflowConnection([lt.queue_row(13, "PGU-509")], cfg=document(ephemeral=("ops",)))
    with lt.TemporaryStateDir() as tmp_path:
        store = lt.PaneHookStateStore(tmp_path)
        gate = lt.PaneActivityGate(
            state_store=store,
            cursor_position_runner=lt.constant_cursor_runner(),
            capture_pane_runner=lt.sequenced_capture_runner(""),
        )
        store.write("pgu-ops:0.0", "busy", source="codex.UserPromptSubmit", now=100.0)
        listener = TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=lambda target, message: sent.append((target, message)),
            activity_gate=gate.is_working,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
            session_clear_settle_seconds=0.0,
            target_exists=lambda _target: True,
        )
        assert listener.listen_once(max_notifications=1) == 0
    assert sent == [], sent
    assert conn.clear_reads == [], conn.clear_reads
    assert [notification_id for notification_id, _params in conn.requeued] == [13]


def test_a_failed_clear_holds_the_ticket_and_stays_retryable() -> None:
    """The whole point of recording after the fact rather than before it."""
    sent: list[tuple[str, str]] = []

    def failing_sender(target: str, message: str) -> None:
        if message == "/clear":
            raise subprocess.CalledProcessError(1, ["directorctl", "send"], stderr="pane refused")
        sent.append((target, message))

    conn = WorkflowConnection([lt.queue_row(14, "PGU-510")], cfg=document(ephemeral=("ops",)))
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(conn, tmp_path=tmp_path, sender=failing_sender) == 0
    assert sent == [], sent
    assert conn.clear_writes == [], conn.clear_writes
    assert conn.acked == []
    assert [notification_id for notification_id, _params in conn.requeued] == [14]
    assert "session_clear_failed" in lt.trace_events(conn)

    # Nothing was recorded, so the retry is a first attempt again -- and this
    # time it lands.
    retry_sent: list[tuple[str, str]] = []
    retry = WorkflowConnection(
        [lt.queue_row(14, "PGU-510", attempts=2)],
        cfg=document(ephemeral=("ops",)),
        cleared=conn.cleared,
    )
    with lt.TemporaryStateDir() as tmp_path:
        assert drive(retry, tmp_path=tmp_path, sent=retry_sent) == 1
    assert retry_sent == [
        ("pgu-ops:0.0", "/clear"),
        ("pgu-ops:0.0", "New ticket for you: PGU-510 -- Queue"),
    ], retry_sent


def test_every_supported_cli_is_cleared_through_the_same_transport() -> None:
    """One send path, four products, and each one asked in its own vocabulary."""
    for index, runtime in enumerate(sorted(RUNTIMES)):
        sent: list[tuple[str, str]] = []
        cfg = document(ephemeral=("ops",), runtimes={"ops": runtime})
        conn = WorkflowConnection([lt.queue_row(20 + index, f"PGU-52{index}")], cfg=cfg)
        with lt.TemporaryStateDir() as tmp_path:
            assert drive(conn, tmp_path=tmp_path, sent=sent) == 1
        assert sent[0] == ("pgu-ops:0.0", SESSION_CLEAR_COMMANDS[runtime]), (runtime, sent)
        assert len(sent) == 2, (runtime, sent)
        assert conn.clear_writes == [(f"PGU-52{index}", "ops")], runtime


def test_a_runtime_with_no_clear_command_holds_the_ticket_instead_of_delivering() -> None:
    """Fail closed at the seam where a new runtime would arrive.

    Today the document cannot express this: the validator accepts only the four
    runtimes, all four have a command, and a notifying owner must declare a
    pane target -- which is why this is exercised against the method rather
    than end to end. It is still the branch that matters most. The day a fifth
    runtime is added to the document's vocabulary and not to the command table,
    the choice is between handing an ephemeral role a ticket on top of the last
    one's context and making it wait. It waits, loudly, and the notification
    stays retryable.
    """
    conn = WorkflowConnection([], cfg=document(ephemeral=("ops",)))
    listener = TicketBoardNotifyListener(
        conninfo="dbname=test",
        sender=lambda target, message: (_ for _ in ()).throw(AssertionError("must not send")),
        connector=lambda *args, **kwargs: conn,
        poll_seconds=0,
        target_exists=lambda _target: True,
    )
    listener.ephemeral_roles = {"ops"}
    listener.role_runtimes = {"ops": "some-new-cli"}
    proceed = listener._clear_role_session(
        conn,
        notification_id=30,
        ticket_id="PGU-530",
        target_role="ops",
        kind="transition",
        target="pgu-ops:0.0",
        message="New ticket for you: PGU-530",
        attempts=1,
    )
    assert proceed is False
    assert conn.clear_writes == [], conn.clear_writes
    assert [notification_id for notification_id, _params in conn.requeued] == [30]
    assert SESSION_CLEAR_UNSUPPORTED_RUNTIME_ERROR in str(conn.requeued[0][1])
    assert "session_clear_failed" in lt.trace_events(conn)


def launcher_config(
    document_cfg: dict[str, Any], *, already_ephemeral: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Project a document over an existing generated config, as an edit does."""
    from scripts.workflow_launcher import project_roles

    raw = {
        "project": "pgu",
        "repository": "/tmp/pgu",
        "roles": [
            {
                "role": role["name"],
                "cli": [role["runtime"]],
                "target": role["target"],
                "tmux_session": role["target"].split(":")[0],
                **({"ephemeral": True} if role["name"] in already_ephemeral else {}),
            }
            for role in document_cfg["roles"]
            if role.get("runtime")
        ],
    }
    return project_roles(raw, document_cfg)


def test_the_generated_config_records_what_the_document_declared() -> None:
    """The projection is how a role gets described twice; twice must agree.

    The document is where a director writes the policy and the launcher config
    is what the machine reads, so a field that stops at the document is a field
    that two people will read differently. Turning it back off has to reach the
    generated file too, which is why the key is removed rather than left at its
    last value.
    """
    from scripts.team_launcher import _role_from_json

    projected = {
        role["role"]: role
        for role in launcher_config(document(ephemeral=("ops",)))["roles"]
    }
    assert projected["ops"]["ephemeral"] is True, projected["ops"]
    assert "ephemeral" not in projected["audit"], projected["audit"]

    parsed = _role_from_json("pgu", projected["ops"], base=Path("/tmp"), default_workdir=Path("/tmp"))
    assert parsed.ephemeral is True
    assert _role_from_json(
        "pgu", projected["audit"], base=Path("/tmp"), default_workdir=Path("/tmp")
    ).ephemeral is False

    # And turning it off in the document reaches a config that already said
    # true, rather than leaving last week's answer in the generated file.
    turned_off = {
        role["role"]: role
        for role in launcher_config(document(), already_ephemeral=("ops",))["roles"]
    }
    assert "ephemeral" not in turned_off["ops"], turned_off["ops"]
    assert _role_from_json(
        "pgu", turned_off["ops"], base=Path("/tmp"), default_workdir=Path("/tmp")
    ).ephemeral is False


def test_the_generated_config_rejects_a_value_that_is_not_a_boolean() -> None:
    from scripts.team_launcher import _role_from_json

    role = {
        "role": "ops",
        "cli": ["claude"],
        "target": "pgu-ops:0.0",
        "tmux_session": "pgu-ops",
        "ephemeral": "true",
    }
    refused(
        lambda: _role_from_json("pgu", role, base=Path("/tmp"), default_workdir=Path("/tmp")),
        "role ops ephemeral must be a JSON boolean",
    )


# --------------------------------------------------------------------------
# the board half
# --------------------------------------------------------------------------


def listener_call(admin: str, sql: str) -> str:
    return t.psql(admin, f"SET ROLE ticket_board_listener;\n{sql}\nRESET ROLE;")


def scalar(raw: str) -> str:
    for line in reversed(raw.splitlines()):
        value = line.strip()
        if value and value != "RESET" and not value.startswith("SET"):
            return value
    raise AssertionError(raw)


def refused(call: Any, expected: str) -> None:
    try:
        call()
    except (Exception, SystemExit) as exc:
        # ValueError from the workflow validator, AssertionError from psql, and
        # SystemExit from the launcher, which refuses by exiting.
        assert expected in str(exc), (expected, str(exc))
        return
    raise AssertionError(f"expected refusal containing {expected!r}")


def run_board_checks(cluster: Any) -> int:
    checks = 0
    dbname = "syrd135_ephemeral"
    admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))
    t.create_roles(admin)
    t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

    # 1. Both validators accept the field and agree about what a value is.
    accepted = document(ephemeral=("audit",))
    validate(accepted, project="pgu")
    t.psql(
        admin,
        "SELECT ticket_board.validate_declared_workflow('"
        + json.dumps(accepted).replace("'", "''")
        + "'::jsonb);",
    )
    assert ephemeral_roles(accepted) == {"audit"}, ephemeral_roles(accepted)
    assert ephemeral_roles(document()) == set()
    checks += 1

    # 2. And both reject a value that is not a boolean, naming the role.
    for bad in ("true", 1, None):
        broken = document()
        for role in broken["roles"]:
            if role["name"] == "audit":
                role["ephemeral"] = bad
        refused(lambda: validate(copy.deepcopy(broken), project="pgu"), "ephemeral must be a boolean: audit")
        refused(
            lambda: t.psql(
                admin,
                "SELECT ticket_board.validate_declared_workflow('"
                + json.dumps(broken).replace("'", "''")
                + "'::jsonb);",
            ),
            "ephemeral must be a boolean: audit",
        )
    checks += 1

    # 3. The durable record: asked, written once, and stable across a reread.
    t.seed_postgres_ticket(admin, "PGU-601", title="First", state="in_progress", assignee="ops")
    t.seed_postgres_ticket(admin, "PGU-602", title="Second", state="in_progress", assignee="ops")
    assert scalar(listener_call(admin, "SELECT ticket_board.role_session_clear_pending('PGU-601', 'ops');")) == "t"
    assert scalar(listener_call(admin, "SELECT ticket_board.record_role_session_clear('PGU-601', 'ops');")) == "t"
    assert scalar(listener_call(admin, "SELECT ticket_board.role_session_clear_pending('PGU-601', 'ops');")) == "f"
    # Recording twice is not an error and is not a second clear.
    assert scalar(listener_call(admin, "SELECT ticket_board.record_role_session_clear('PGU-601', 'ops');")) == "f"
    # A different role on the same ticket, and a different ticket, are their own.
    assert scalar(listener_call(admin, "SELECT ticket_board.role_session_clear_pending('PGU-601', 'audit');")) == "t"
    assert scalar(listener_call(admin, "SELECT ticket_board.role_session_clear_pending('PGU-602', 'ops');")) == "t"
    rows = t.psql(admin, "SELECT count(*)::text FROM ticket_board.ticket_role_session_clears;")
    assert rows == "1", rows
    checks += 1

    # 4. Only the listener may ask or record, and no pane role may read the table.
    refused(
        lambda: t.psql(admin, "SET ROLE ops;\nSELECT ticket_board.role_session_clear_pending('PGU-601', 'ops');"),
        "permission denied",
    )
    refused(
        lambda: t.psql(admin, "SET ROLE director;\nSELECT ticket_board.record_role_session_clear('PGU-601', 'ops');"),
        "permission denied",
    )
    # Reading is the house norm for every board table; writing is not. Nobody
    # writes this one by hand -- the record exists to say a clear happened, and
    # a hand-written row would be a claim that one did.
    for role in ("ops", "director", "ticket_board_service", "ticket_board_listener"):
        refused(
            lambda role=role: t.psql(
                admin,
                f"SET ROLE {role};\n"
                "INSERT INTO ticket_board.ticket_role_session_clears(ticket_id, role) "
                "VALUES ('PGU-602', 'ops');",
            ),
            "permission denied",
        )
        refused(
            lambda role=role: t.psql(
                admin,
                f"SET ROLE {role};\nDELETE FROM ticket_board.ticket_role_session_clears;",
            ),
            "permission denied",
        )
    checks += 1

    # 5. A ticket that goes away takes its clear record with it, so a reused id
    #    cannot inherit somebody else's "already cleared".
    t.psql(admin, "DELETE FROM ticket_board.tickets WHERE id = 'PGU-601';")
    assert t.psql(admin, "SELECT count(*)::text FROM ticket_board.ticket_role_session_clears;") == "0"
    checks += 1
    return checks


def run_upgrade_checks(cluster: Any) -> int:
    """A board that predates this change, brought up by the migration alone."""
    dbname = "syrd135_upgrade"
    admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    t.psql(admin, schema_before_this_change())
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise

    before = document()
    t.psql(
        admin,
        "INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('seed', '"
        + json.dumps(before).replace("'", "''")
        + "'::jsonb);\n"
        "INSERT INTO ticket_board.workflow_configuration(singleton, revision, document) VALUES "
        "(true, (SELECT max(revision) FROM ticket_board.workflow_revisions), '"
        + json.dumps(before).replace("'", "''")
        + "'::jsonb) ON CONFLICT (singleton) DO UPDATE SET revision=EXCLUDED.revision, document=EXCLUDED.document;\n"
        "INSERT INTO ticket_board.workflow_roles(name, definition) SELECT x->>'name', x "
        "FROM jsonb_array_elements('"
        + json.dumps(before).replace("'", "''")
        + "'::jsonb->'roles') x ON CONFLICT (name) DO UPDATE SET definition=EXCLUDED.definition;",
    )
    migration = (ROOT / "scripts/ticket_board/migrations" / MIGRATION).read_text(encoding="utf-8")
    t.psql(admin, migration)
    # Idempotent: a runner that applies it twice writes nothing new.
    t.psql(admin, migration)
    # And then the rest of the series, in the order the runner applies it. The
    # grants below are the CURRENT rbac.sql, which names every function the
    # current release has; stopping the replay at this ticket's own migration
    # would ask a board of that vintage to grant on functions a later migration
    # creates, and the failure would look like this change's rather than like a
    # replay that stopped early.
    later = sorted(
        path for path in (ROOT / "scripts/ticket_board/migrations").glob("*.sql")
        if path.name > MIGRATION
    )
    for path in later:
        t.psql(admin, path.read_text(encoding="utf-8"))
    t.psql(admin, t.RBAC_PATH.read_text(encoding="utf-8"))

    # After: the field is accepted, the record works, and the tenant's stored
    # document is exactly what it was -- nothing is backfilled onto a role.
    t.psql(
        admin,
        "SELECT ticket_board.validate_declared_workflow('"
        + json.dumps(document(ephemeral=("audit",))).replace("'", "''")
        + "'::jsonb);",
    )
    stored = json.loads(t.psql(admin, "SELECT document::text FROM ticket_board.workflow_configuration WHERE singleton;"))
    assert stored == before, "the upgrade must not rewrite a stored document"
    assert ephemeral_roles(stored) == set()
    t.seed_postgres_ticket(admin, "PGU-610", title="Upgraded", state="in_progress", assignee="ops")
    assert scalar(listener_call(admin, "SELECT ticket_board.record_role_session_clear('PGU-610', 'ops');")) == "t"
    assert scalar(listener_call(admin, "SELECT ticket_board.role_session_clear_pending('PGU-610', 'ops');")) == "f"
    return 2


def main() -> int:
    checks = 0
    listener_tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in listener_tests:
        test()
        checks += 1

    if shutil_which("initdb") and shutil_which("psql"):
        with temporary_cluster(prefix="syrd135-ephemeral-", shutdown="immediate") as cluster:
            checks += run_board_checks(cluster)
            checks += run_upgrade_checks(cluster)
    else:
        print("ephemeral_role_sessions_test: no PostgreSQL binaries; board half skipped")

    print(f"ephemeral_role_sessions_test: {checks} checks ok")
    return 0


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


if __name__ == "__main__":
    raise SystemExit(main())
