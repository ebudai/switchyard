#!/usr/bin/env python3
"""SYRD-538: a turn that ends on running background work is not an unresolved turn.

On SYRD-537 App's Claude session started a background Explore subagent and
ended its turn to wait for it. The board prompted "this turn ended without
resolving it" five times in four minutes while the subagent ran: every prompt
woke the session, the woken turn ended, and that new turn end was prompted
again.

Claude's own Stop input says what is still in flight (`background_tasks`). The
pane hook now records it, bound to the pane root that wrote it; the gate counts
it only while that is the role's registered, still-running provider process
and younger than a ceiling; reminders about that owner wait while it lasts; and
the board prompts once per unresolved episode instead of once per turn end.

Every case runs against disposable state: a private tmux server, a synthetic
/proc, a fake connection, and temporary PostgreSQL clusters.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts.ticket_board import background_work  # noqa: E402
from scripts.ticket_board.notify_listener import (  # noqa: E402
    PaneActivityGate,
    PaneHookStateStore,
    TicketBoardNotifyListener,
)
from scripts.ticket_board.peer_identity import SessionIdentity, session_identity  # noqa: E402
from ticket_board_notify_listener_test import (  # noqa: E402
    FakeConnection,
    FakeResult,
    constant_cursor_runner,
    queue_row,
    targeted_capture_runner,
)

CHECKS = 0
HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"
ROLE = "app"
#: The target the listener itself routes this role to: from the project in the
#: environment, so the cases hold in a role pane as well as under env -i.
from scripts.ticket_board.notify_listener import ROLE_TO_TARGET  # noqa: E402
TARGET = ROLE_TO_TARGET.get(ROLE, "pgu-app:0.0")
#: The shape of Claude Code 2.1.284's Stop input with one background subagent.
STOP_WITH_SUBAGENT = {
    "session_id": "sess-538", "hook_event_name": "Stop", "stop_hook_active": False,
    "background_tasks": [{"id": "a1b2", "type": "subagent", "status": "running",
                          "description": "Survey the notification system", "agent_type": "Explore"}],
    "session_crons": [],
}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _hook_module():
    loader = importlib.machinery.SourceFileLoader("ticket_board_pane_idle_hook_538", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HOOK_MODULE = _hook_module()


class FakeProc:
    """A /proc with just the processes a case says exist."""

    def __init__(self, root: Path) -> None:
        self.root = root / "proc"
        self.root.mkdir()

    def add(self, pid: int, ppid: int, start: int, comm: str) -> None:
        (self.root / str(pid)).mkdir(exist_ok=True)
        # Field 22 of stat (index 19 after the comm) is the start time.
        rest = " ".join(["0"] * 17)
        (self.root / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {rest} {start} 0 0\n")

    def remove(self, pid: int) -> None:
        shutil.rmtree(self.root / str(pid))


class UnresolvedBoard(FakeConnection):
    """The board's answer for these rows: the turn is unresolved and in the current round.

    `snoozed` is SYRD-537's answer for the ticket: a Director's reminder snooze.
    """

    snoozed = False

    def execute(self, statement, params=None):
        text = str(statement)
        if "ticket_turn_is_resolved" in text or "current_assignment_at" in text:
            self.executed.append((statement, params))
            return FakeResult([(False,)])
        if "to_regprocedure('ticket_board.ticket_reminders_snoozed" in text:
            return FakeResult([(True,)])
        if "ticket_board.ticket_reminders_snoozed(" in text:
            return FakeResult([(self.snoozed,)])
        return super().execute(statement, params)


def _gate(state_dir: Path, proc: FakeProc, *, registered: SessionIdentity | None) -> PaneActivityGate:
    gate = PaneActivityGate(
        state_store=PaneHookStateStore(state_dir),
        cursor_position_runner=constant_cursor_runner(),
        capture_pane_runner=targeted_capture_runner(TARGET, "quiet\n"),
        pane_pid_runner=lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="not a pid\n"),
        process_table_reader=lambda: (),
        sleeper=lambda _seconds: None,
    )
    gate.proc_root = proc.root
    gate.role_identities = {ROLE: registered} if registered else {}
    return gate


def _stop(state_dir: Path, *, pid: int | None, proc: FakeProc, payload: dict = STOP_WITH_SUBAGENT,
          source: str = "claude.Stop", at: float | None = None) -> None:
    """What the real hook writes for this Stop, through its own writer and its own reading of the input."""
    work = HOOK_MODULE._background_work(payload, source, pid=pid, proc_root=proc.root)
    HOOK_MODULE._write_state(state_dir, TARGET, "idle", source=source, background_work=work)
    if at is not None:
        path = Path(state_dir) / f"{TARGET.replace(':', '_')}.json"
        data = json.loads(path.read_text())
        data["updated_at"] = at
        path.write_text(json.dumps(data))


def test_the_real_hook_in_a_real_pane_records_the_work_and_its_provider() -> None:
    """Claude's Stop input, through the installed hook, in a private tmux pane."""
    tmp = Path(tempfile.mkdtemp(prefix="syrd538-tmux."))
    socket = tmp / "tmux.sock"
    env = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
    env.update({"TMUX_TMPDIR": str(tmp), "HOME": str(tmp)})
    state_dir, session_dir = tmp / "state", tmp / "sessions"
    payloads = {name: tmp / f"{name}.json" for name in ("subagent", "nothing", "crons")}
    payloads["subagent"].write_text(json.dumps(STOP_WITH_SUBAGENT))
    payloads["nothing"].write_text(json.dumps({**STOP_WITH_SUBAGENT, "background_tasks": []}))
    payloads["crons"].write_text(json.dumps({**STOP_WITH_SUBAGENT, "background_tasks": [],
                                             "session_crons": [{"id": "c1", "schedule": "*/5 * * * *",
                                                                "recurring": True, "prompt": "check"}]}))

    def tmux(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["tmux", "-S", str(socket), "-f", "/dev/null", *args],
                              env=env, capture_output=True, text=True, timeout=20)

    def hook(source: str, payload: str, state: str = "idle") -> str:
        return (f"{sys.executable} -B {HOOK} {state} --source {source} --target {TARGET} --state-dir {state_dir} "
                f"--session-dir {session_dir} --stdin-timeout 2 < {payloads[payload] if payload else '/dev/null'}")

    def run_in_pane(command: str) -> dict:
        marker = tmp / f"done-{time.monotonic_ns()}"
        tmux("send-keys", "-t", "w", f"{command}; touch {marker}", "Enter")
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        check(marker.exists(), f"the hook ran in the pane: {tmux('capture-pane', '-p', '-t', 'w').stdout[-400:]}")
        return json.loads((state_dir / f"{TARGET.replace(':', '_')}.json").read_text())

    try:
        started = tmux("new-session", "-d", "-s", "w", "-x", "120", "-y", "20", "/bin/sh")
        check(started.returncode == 0 and socket.exists(), f"a private tmux server on {socket}: {started.stderr}")
        pane_pid = int(tmux("display-message", "-p", "-t", "w", "#{pane_pid}").stdout.strip())
        identity = session_identity(pane_pid)
        check(identity is not None and identity.pid == pane_pid,
              f"the pane root the board would register: {identity} for pane pid {pane_pid}")

        written = run_in_pane(hook("claude.Stop", "subagent"))
        work = written.get("background_work") or {}
        check(written["state"] == "idle" and written["source"] == "claude.Stop", written)
        check((work.get("pane_pid"), work.get("pane_start_time")) == (identity.pid, identity.start_time)
              and work.get("session_id") == "sess-538"
              and work.get("tasks") == [{"id": "a1b2", "type": "subagent", "status": "running"}],
              f"the in-flight subagent, bound to the registered pane root: {work}")

        for source, payload, why in (("claude.Stop", "nothing", "a Stop with nothing in flight"),
                                     ("claude.Stop", "crons", "a scheduled wakeup is not running work"),
                                     ("codex.Stop", "subagent", "no other runtime is claimed to report it")):
            run_in_pane(hook("claude.Stop", "subagent"))
            written = run_in_pane(hook(source, payload))
            check("background_work" not in written and written["state"] == "idle", f"{why}: {written}")
        run_in_pane(hook("claude.Stop", "subagent"))
        written = run_in_pane(hook("claude.UserPromptSubmit", "", state="busy"))
        check("background_work" not in written and written["state"] == "busy",
              f"the next turn's start replaces the record: {written}")
    finally:
        tmux("kill-server")
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_gate_counts_only_live_work_of_the_registered_provider() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="syrd538-gate."))
    try:
        proc = FakeProc(tmp)
        proc.add(900, 1, 10, "tmux: server")
        proc.add(4100, 900, 5000, "claude")      # the pane root, as registered
        proc.add(4200, 4100, 5100, "python3")    # the hook, under it
        registered = SessionIdentity(4100, 5000)
        state_dir = tmp / "state"

        def gate_after(**kwargs) -> PaneActivityGate:
            return _gate(state_dir, proc, registered=kwargs.get("registered", registered))

        _stop(state_dir, pid=4200, proc=proc)
        gate = gate_after()
        check(gate.background_work_roles([ROLE]) == {ROLE}, "a live subagent of the registered provider counts")
        check(gate.turn_end_idle_since_by_role([ROLE]) == {} and gate.idle_since_by_role([ROLE]) == {},
              "and its turn end mints no unresolved-turn prompt and no idle reminder")
        check(_gate(state_dir, proc, registered=registered).background_work_roles([ROLE]) == {ROLE},
              "a restarted listener reads the same answer from the same state")

        _stop(state_dir, pid=4200, proc=proc, payload={**STOP_WITH_SUBAGENT, "background_tasks": []})
        gate = gate_after()
        check(gate.background_work_roles([ROLE]) == set() and ROLE in gate.turn_end_idle_since_by_role([ROLE]),
              "completion: the next Stop with nothing in flight is an ordinary turn end again")

        for label, setup, undo, registered_as in (
            ("provider died", lambda: proc.remove(4100), lambda: proc.add(4100, 900, 5000, "claude"), registered),
            ("pid reused by a new process", lambda: proc.add(4100, 900, 7777, "claude"),
             lambda: proc.add(4100, 900, 5000, "claude"), registered),
            ("provider restarted: a new registration", lambda: None, lambda: None, SessionIdentity(4300, 6000)),
            ("listener has no registration yet", lambda: None, lambda: None, None),
        ):
            _stop(state_dir, pid=4200, proc=proc)
            setup()
            gate = gate_after(registered=registered_as)
            check(gate.background_work_roles([ROLE]) == set() and ROLE in gate.turn_end_idle_since_by_role([ROLE]),
                  f"{label}: the record counts for nothing, the turn end is ordinary")
            undo()

        _stop(state_dir, pid=4200, proc=proc, at=time.time() - background_work.DEFAULT_LIMIT_SECONDS - 5)
        gate = gate_after()
        check(gate.background_work_roles([ROLE]) == set() and ROLE in gate.idle_since_by_role([ROLE]),
              "a job older than the ceiling is idle work again (SYRD-403: a hung job must surface)")

        # An unrelated live child, with no in-flight list from the provider, is not background work.
        proc.add(4400, 4100, 5200, "sleep")
        _stop(state_dir, pid=4200, proc=proc, payload={**STOP_WITH_SUBAGENT, "background_tasks": []})
        check(gate_after().background_work_roles([ROLE]) == set(), "a live descendant alone suppresses nothing")
        # Written from a pane under a different provider (a prior session's pane root).
        proc.add(4500, 900, 4000, "claude")
        proc.add(4600, 4500, 4100, "python3")
        _stop(state_dir, pid=4600, proc=proc)
        check(gate_after().background_work_roles([ROLE]) == set(), "a prior session's record suppresses nothing")
        check(background_work.limit_seconds({background_work.LIMIT_ENV: "120"}) == 120.0
              and background_work.limit_seconds({background_work.LIMIT_ENV: "x"}) == background_work.DEFAULT_LIMIT_SECONDS,
              "the ceiling is configurable, and a bad value keeps the default")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_queued_reminders_wait_for_the_work_and_nothing_else_does() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="syrd538-queue."))
    try:
        proc = FakeProc(tmp)
        proc.add(900, 1, 10, "tmux: server")
        proc.add(4100, 900, 5000, "claude")
        proc.add(4200, 4100, 5100, "python3")
        state_dir = tmp / "state"
        gate = _gate(state_dir, proc, registered=SessionIdentity(4100, 5000))
        _stop(state_dir, pid=4200, proc=proc)
        ticket = {"state": "in_progress", "assignee": ROLE}

        def row(kind: str, target_role: str, **extra) -> tuple:
            notification_id, ticket_id, target, message, payload, attempts = queue_row(
                7, "PGU-538", kind=kind, target_role=target_role, **ticket)
            return (notification_id, ticket_id, target, message, json.dumps({**json.loads(payload), **extra}), attempts)

        def outcome(queued: tuple, *, snoozed: bool = False) -> tuple[list, list]:
            sent: list = []
            conn = UnresolvedBoard([queued], ticket_rows={"PGU-538": ("in_progress", ROLE, False, False, False)})
            conn.snoozed = snoozed
            outcomes.append(conn)
            listener = TicketBoardNotifyListener(conninfo="dbname=test", sender=lambda t, m: sent.append((t, m)),
                                                 activity_gate=gate.is_working, connector=lambda *a, **k: conn,
                                                 poll_seconds=0)
            listener.listen_once(max_notifications=1)
            return conn.requeued, sent

        outcomes: list = []
        held = {
            "the owner's repair prompt": row("unresolved_turn_repair", ROLE),
            "the owner's idle reminder": row("idle_reminder", ROLE),
            "the Director's grace escalation about the owner": row("unresolved_turn", "director", owner_role=ROLE),
            "the Director's idle escalation about the owner": row("escalation", "director", owner_role=ROLE,
                                                                  idle_since="2026-10-02T10:52:18+00:00"),
        }
        for label, queued in held.items():
            requeued, sent = outcome(queued)
            check(len(requeued) == 1 and "owner_background_work (claim)" in str(requeued[0][1]) and not sent,
                  f"{label} waits, from its claim, while the owner's background work runs: {requeued} {sent}")
            check(requeued[0][1][1] == "60 seconds", f"{label} is looked at again in a minute: {requeued[0][1]}")

        # SYRD-537 composition: a snoozed reminder is discarded by the snooze,
        # ahead of the currency checks, before this deferral ever sees it.
        for label, queued in held.items():
            requeued, sent = outcome(queued, snoozed=True)
            traced = " ".join(str(trace) for trace in outcomes[-1].traces)
            check(not any("owner_background_work" in str(r[1]) for r in requeued) and not sent
                  and "reminder_snoozed" in traced,
                  f"{label}, snoozed and about a working owner, is the snooze's to discard: {requeued} {traced[:300]}")

        for label, queued in {
            "a new assignment": row("transition", ROLE),
            "a permission-prompt escalation": row("escalation", "director", reason="permission_prompt", role=ROLE),
            "an in-place ticket update": row("ticket_update", ROLE),
        }.items():
            requeued, _sent = outcome(queued)
            check(not any("owner_background_work" in str(r[1]) for r in requeued),
                  f"{label} is not held for background work: {requeued}")

        _stop(state_dir, pid=4200, proc=proc, payload={**STOP_WITH_SUBAGENT, "background_tasks": []})
        for label, queued in held.items():
            requeued, _sent = outcome(queued)
            check(not any("owner_background_work" in str(r[1]) for r in requeued),
                  f"after the work ends, {label} goes back to the ordinary checks: {requeued}")
        check(background_work.reminder_owner("escalation", "director", json.dumps({"reason": "permission_prompt"})) == ""
              and background_work.reminder_owner("idle_reminder", ROLE, "not json") == "",
              "classification needs the reminder's own shape")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_work_that_starts_between_claim_and_send_still_holds_the_reminder() -> None:
    """Director review of c5c1c862: the owner's work began while the listener waited to send.

    The real delivery loop; the pre-send wait is where the owner's Stop hook
    writes its record, through the hook's own writer, read by the real gate.
    """
    tmp = Path(tempfile.mkdtemp(prefix="syrd538-presend."))
    try:
        proc = FakeProc(tmp)
        proc.add(900, 1, 10, "tmux: server")
        proc.add(4100, 900, 5000, "claude")
        proc.add(4200, 4100, 5100, "python3")
        state_dir = tmp / "state"
        gate = _gate(state_dir, proc, registered=SessionIdentity(4100, 5000))

        class Witness:
            """The real gate's background evidence; every pane otherwise free to take a notice."""

            def background_work_roles(self, roles):
                return gate.background_work_roles(roles)

            def is_working(self, _target):
                return False

        def deliver(kind: str, target_role: str, *, starts=None, snooze=False, **extra) -> tuple[list, list, str]:
            _stop(state_dir, pid=4200, proc=proc, payload={**STOP_WITH_SUBAGENT, "background_tasks": []})
            notification_id, ticket_id, target, message, payload, attempts = queue_row(
                9, "PGU-538", kind=kind, target_role=target_role, state="in_progress", assignee=ROLE)
            conn = UnresolvedBoard([(notification_id, ticket_id, target, message,
                                     json.dumps({**json.loads(payload), **extra}), attempts)],
                                   ticket_rows={"PGU-538": ("in_progress", ROLE, False, False, False)})
            sent: list = []
            witness = Witness()

            def owner_acts_while_the_listener_waits(_seconds: float) -> None:
                if starts is not None:
                    starts()
                conn.snoozed = snooze

            listener = TicketBoardNotifyListener(
                conninfo="dbname=unused", sender=lambda t, m: sent.append((t, m)),
                activity_gate=witness.is_working, target_exists=lambda _t: True,
                submission_witness=lambda *_a: True, pre_send_recheck_delay_seconds=0.01,
                sleeper=owner_acts_while_the_listener_waits)
            listener.process_due_notifications(conn, max_notifications=1)
            return conn.requeued, sent, " ".join(str(trace) for trace in conn.traces)

        def starts() -> None:
            _stop(state_dir, pid=4200, proc=proc)

        def starts_already_expired() -> None:
            _stop(state_dir, pid=4200, proc=proc, at=time.time() - background_work.DEFAULT_LIMIT_SECONDS - 5)

        for label, kind, target_role, extra in (
            ("the Director's grace escalation about the owner", "unresolved_turn", "director", {"owner_role": ROLE}),
            ("the owner's repair prompt", "unresolved_turn_repair", ROLE, {}),
        ):
            requeued, sent, _ = deliver(kind, target_role, starts=None, **extra)
            check(len(sent) == 1 and not requeued, f"control: with no work, {label} is sent: {sent} {requeued}")
            requeued, sent, _ = deliver(kind, target_role, starts=starts, **extra)
            check(not sent and len(requeued) == 1 and "owner_background_work (pre_send_recheck)" in str(requeued[0][1]),
                  f"{label}: work that began after the claim holds it at the send: {sent} {requeued}")
            requeued, sent, _ = deliver(kind, target_role, starts=starts_already_expired, **extra)
            check(len(sent) == 1, f"{label}: a record already past the ceiling holds nothing: {sent} {requeued}")
            requeued, sent, traced = deliver(kind, target_role, starts=starts, snooze=True, **extra)
            check(not sent and not any("owner_background_work" in str(r[1]) for r in requeued)
                  and "reminder_snoozed" in traced,
                  f"{label}: a snooze in the same window is still the snooze's to discard (SYRD-537 first): {requeued}")
        requeued, sent, _ = deliver("transition", ROLE, starts=starts)
        check(len(sent) == 1 and not any("owner_background_work" in str(r[1]) for r in requeued),
              f"a new assignment is sent though the owner's work began: {sent} {requeued}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_listener_hands_the_gate_each_role_s_registered_provider() -> None:
    """The identity the gate binds background work to comes from the board's runtime rows."""
    from scripts.ticket_board.peer_identity import read_process

    me = read_process(os.getpid())
    check(me is not None, "this process can read its own /proc entry")

    class Rows(FakeResult):
        def fetchall(self):
            return list(self.rows)

    class Assignments:
        """The board's runtime rows, as the listener's own query reads them."""

        def __init__(self, rows):
            self.rows = rows

        def execute(self, statement, params=None):
            return Rows(self.rows if "role_runtime_assignments" in str(statement) else [])

    for shape, rows in (
        ("dict rows", [{"role": ROLE, "actual_target": "pgu-app:0.5", "runtime": "claude",
                        "process_pid": os.getpid(), "process_start_time": me.start_time}]),
        ("tuple rows", [(ROLE, "pgu-app:0.5", "claude", os.getpid(), me.start_time)]),
    ):
        tmp = Path(tempfile.mkdtemp(prefix="syrd538-refresh."))
        try:
            gate = PaneActivityGate(state_store=PaneHookStateStore(tmp))
            listener = TicketBoardNotifyListener(conninfo="dbname=test", activity_gate=gate.is_working, poll_seconds=0)
            listener.refresh_workflow(Assignments(rows))
            check(gate.role_targets.get(ROLE) == "pgu-app:0.5"
                  and gate.role_identities.get(ROLE) == SessionIdentity(os.getpid(), me.start_time),
                  f"{shape}: the gate holds the registered provider for the role: {gate.role_identities}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_one_prompt_per_unresolved_episode_on_every_board_shape() -> None:
    import ticket_board_write_api_test as fixture
    from schema_function_drift import schema_before
    from temporary_cluster import temporary_cluster

    migration = ROOT / "scripts/ticket_board/migrations/pgu971_syrd538_unresolved_turn_episode.sql"

    def board(cluster, dbname: str, shape: str) -> str:
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        if shape == "upgraded":
            before = schema_before(migration)
            check("unresolved EPISODE" not in before, "the before-schema predates this change")
            fixture.psql(admin, before)
        else:
            fixture.psql(admin, fixture.SCHEMA_PATH.read_text())
        fixture.create_roles(admin)
        if shape == "upgraded":
            # The board as its release left it: that release's grants.
            from schema_function_drift import rbac_before
            fixture.psql(admin, rbac_before(migration))
        else:
            fixture.psql(admin, fixture.RBAC_PATH.read_text())
        if shape == "migrated":
            result = subprocess.run(["bash", str(ROOT / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=240)
            check(result.returncode == 0, f"every migration applies: {result.stderr[-1500:]}")
            fixture.psql(admin, fixture.RBAC_PATH.read_text())
        if shape == "upgraded":
            # Every migration from this one on, as the runner applies them,
            # before this release's rbac.sql: it grants on what later ones create.
            from schema_function_drift import migrations_from
            fixture.psql(admin, migration.read_text())
            for later in migrations_from(migration):
                fixture.psql(admin, later.read_text())
            fixture.psql(admin, fixture.RBAC_PATH.read_text())
        return admin

    definitions: dict[str, tuple[str, str]] = {}
    for shape in ("fresh", "migrated", "upgraded"):
        with temporary_cluster(prefix="syrd538-episode.", shutdown="immediate") as cluster:
            admin = board(cluster, f"episode_{shape}", shape)

            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def as_listener(statement: str) -> str:
                raw = fixture.psql(admin, f"SET ROLE ticket_board_listener;\n{statement}\nRESET ROLE;")
                return "\n".join(line for line in raw.splitlines() if line.strip() not in {"SET", "RESET"}).strip()

            def deliver_handovers() -> None:
                for queued in sql("SELECT coalesce(string_agg(id::text, ' '), '') FROM ticket_board.ticket_notification_queue "
                                  "WHERE kind = 'transition' AND dead_lettered_at IS NULL;").split():
                    as_listener(f"SELECT ticket_board.ack_notification({queued});")

            def turn_ended(owner: str, turn: str, offset: str = "0 minutes") -> int:
                deliver_handovers()
                return int(as_listener(
                    f"SELECT ticket_board.notify_unresolved_turn_end('{json.dumps({owner: turn})}'::jsonb, "
                    f"clock_timestamp() + interval '{offset}', interval '10 minutes');"))

            def escalations(ticket: str, offset: str) -> list[str]:
                deliver_handovers()
                as_listener("SELECT ticket_board.notify_unresolved_turn_end('{}'::jsonb, "
                            f"clock_timestamp() + interval '{offset}', interval '10 minutes');")
                keys = sql("SELECT coalesce(string_agg(dedupe_key, ' ' ORDER BY id), '') FROM ticket_board.ticket_notification_queue "
                           f"WHERE ticket_id = '{ticket}' AND kind = 'unresolved_turn';")
                return keys.split() if keys else []

            def prompts(ticket: str) -> int:
                return int(sql("SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = "
                               f"'{ticket}' AND kind = 'unresolved_turn_repair' AND event = 'enqueue';"))

            T = "PGU-538"
            fixture.seed_postgres_ticket(admin, T, title=T, state="in_progress", assignee="app", commit_exempt=True)
            check(turn_ended("app", "turn-1") == 1 and prompts(T) == 1, f"{shape}: an idle unresolved turn is prompted")
            for n, after in ((2, "1 minute"), (3, "2 minutes"), (4, "4 minutes"), (5, "9 minutes")):
                check(turn_ended("app", f"turn-{n}", after) == 0 and prompts(T) == 1,
                      f"{shape}: the prompt's woken turn {n}, {after} later, is the same episode: no new prompt")
            check(escalations(T, "5 minutes") == [], f"{shape}: nothing escalates inside the grace")
            check(escalations(T, "11 minutes") == ["unresolved-turn:PGU-538:in_progress:app:turn-1"],
                  f"{shape}: the episode escalates once, from its first prompt: grace not reset")
            check(turn_ended("app", "turn-5b", "6 minutes") == 1 and prompts(T) == 2,
                  f"{shape}: once the Director has been told, the episode is over: a fresh turn prompts again")
            check(turn_ended("app", "turn-5c", "7 minutes") == 0 and prompts(T) == 2,
                  f"{shape}: and that prompt's own woken turn is its episode: no new prompt")
            check(turn_ended("app", "turn-6", "30 minutes") == 1 and prompts(T) == 3,
                  f"{shape}: after the grace, a later unresolved turn starts a new episode and is prompted")

            # THE LOOP: a prompt the listener delivered wakes the session, and that
            # woken turn ends unresolved. Delivered and acked as the listener does.
            D = "PGU-541"
            fixture.seed_postgres_ticket(admin, D, title=D, state="in_progress", assignee="research", commit_exempt=True)
            if turn_ended("research", "d-1") == 1:
                sent = sql(f"SELECT id FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{D}' "
                           "AND kind = 'unresolved_turn_repair';")
                as_listener(f"SELECT ticket_board.record_notification_trace('{D}'::text, {sent}::bigint, 'research'::text, "
                            "'unresolved_turn_repair'::text, 'send'::text, NULL::text, 'hook_idle'::text, NULL::text, '{}'::jsonb);")
                as_listener(f"SELECT ticket_board.ack_notification({sent});")
                check(turn_ended("research", "d-2", "1 minute") == 0,
                      f"{shape}: the delivered prompt's woken turn is the same episode: no new prompt")
                # Removed without being sent (a drain, a lost row): it never reached the owner.
                sql(f"UPDATE ticket_board.notification_trace SET event = 'listener_claim' WHERE notification_id = {sent} AND event = 'send';")
                check(turn_ended("research", "d-3", "2 minutes") == 1,
                      f"{shape}: a prompt that never reached the owner opens no episode")
            else:
                check(False, f"{shape}: research is an owner the guard reports for")

            # A new assignment is a new episode at once. Away to a role holding
            # nothing, so serial focus cannot make the return a queued wait.
            R = "PGU-539"
            fixture.seed_postgres_ticket(admin, R, title=R, state="in_progress", assignee="ops", commit_exempt=True)
            check(turn_ended("ops", "r-1") == 1, f"{shape}: the first assignee is prompted")
            sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET assignee = 'main' WHERE id = '{R}';")
            sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET assignee = 'ops' WHERE id = '{R}';")
            reprompted = turn_ended("ops", "r-2")
            check(reprompted == 1, f"{shape}: after a reassignment away and back, the owner is prompted afresh: {reprompted}; "
                  + sql("SELECT current_assignment_at::text || ' resolved=' || ticket_board.ticket_turn_is_resolved("
                        f"'{R}', clock_timestamp())::text FROM ticket_board.ticket_notification_state WHERE ticket_id = '{R}';")
                  + " prompts " + sql("SELECT string_agg(ts::text || ' ' || (detail->>'dedupe_key'), '; ') FROM "
                                      f"ticket_board.notification_trace WHERE ticket_id = '{R}' AND event = 'enqueue';"))

            # A prompt delivery dropped as stale never reached the owner (a
            # hold, SYRD-517): the next unresolved turn is prompted afresh.
            H = "PGU-540"
            fixture.seed_postgres_ticket(admin, H, title=H, state="in_progress", assignee="main", commit_exempt=True)
            check(turn_ended("main", "h-1") == 1, f"{shape}: prompted")
            dropped = sql(f"SELECT id FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{H}' "
                          "AND kind = 'unresolved_turn_repair';")
            # As the listener's ledger records a stale drop: the same call and casts.
            as_listener(f"SELECT ticket_board.record_notification_trace('{H}'::text, {dropped}::bigint, 'main'::text, "
                        "'unresolved_turn_repair'::text, 'drop'::text, NULL::text, 'stale_notification'::text, NULL::text, "
                        "'{}'::jsonb);")
            as_listener(f"SELECT ticket_board.ack_notification({dropped});")
            check(turn_ended("main", "h-2", "1 minute") == 1,
                  f"{shape}: after a dropped prompt, the next unresolved turn is prompted inside the grace")
            check(turn_ended("main", "h-3", "2 minutes") == 0, f"{shape}: and that delivered one opens the episode")
            # A continuation taken after the prompt is a new promise (SYRD-194):
            # its own turn stays silent, and the next unresolved one is prompted.
            fixture.psql(admin, f"SET ROLE {fixture.SERVICE_ROLE};\nSET ticket_board.caller_role = 'main';\n"
                                f"SELECT ticket_board.grant_turn_continuation('{H}', 'h-4', 'Still verifying.');")
            check(turn_ended("main", "h-4", "3 minutes") == 0, f"{shape}: the continued turn is silent")
            check(turn_ended("main", "h-5", "4 minutes") == 1,
                  f"{shape}: after a continuation, the next unresolved turn inside the grace is prompted")

            definitions[shape] = (
                sql("SELECT md5(pg_get_functiondef('ticket_board.notify_unresolved_turn_end(jsonb, timestamptz, interval)'::regprocedure));"),
                sql("SELECT coalesce(string_agg(a.grantee::regrole::text || ':' || a.privilege_type, ',' ORDER BY 1), '') "
                    "FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = "
                    "'ticket_board.notify_unresolved_turn_end(jsonb, timestamptz, interval)'::regprocedure;"),
            )
    check(len(set(definitions.values())) == 1,
          f"schema.sql, every migration, and an upgrade end with the same function and the same grants: {definitions}")
    check("ticket_board_listener:EXECUTE" in definitions["fresh"][1] and "-:EXECUTE" not in definitions["fresh"][1],
          f"the listener may run it, PUBLIC may not: {definitions['fresh'][1]}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"ticket_board_background_work_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
