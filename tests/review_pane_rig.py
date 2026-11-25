#!/usr/bin/env python3
"""A production-built board whose Audit stage two reviewers own, for review_recovery_test and review_return_test.

The board is built from schema.sql and rbac.sql and runs the shipped example workflow under a pull policy. `audit-2`
is added to Audit as MEFP has it: ephemeral, on Codex, templated on `audit`. The real HTTP server and notify listener
run, and the real gate reads hook state that the real pane hook's writer records. Every role is a live stand-in
provider process, registered as a pane registers it.

The panes are recording providers. A `/clear` starts a new session, announced with SessionStart(clear). A reviewer
handed a ticket stays busy with it until the test ends its turn (`idle`). All tickets are synthetic.

`review_board(root, prefix)` yields the helpers the scenarios use. `root` is the tree under test: a scenario imports
the board code from it, so the same scenario runs against main's tree and this one.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "SWITCHYARD_")
ROLES = ("director", "main", "app", "ops", "audit", "audit-2", "inspector")


@contextmanager
def review_board(root: Path, prefix: str, *, extra_reviewers: tuple[str, ...] = ()) -> Iterator[SimpleNamespace]:
    """`extra_reviewers` are more owners of Audit after `audit` and `audit-2`: Claude, not ephemeral, templated on audit."""
    roles = ROLES + tuple(extra_reviewers)
    reviewers = ("audit", "audit-2", *extra_reviewers)
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    for key in [key for key in os.environ if key.startswith(PANE_ENV_PREFIXES) or key in ("TMUX", "TMUX_PANE")]:
        del os.environ[key]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from pull_claim_postgres_test import build
    from pull_pickup_test import Gate, proc_entry
    from pull_scheduling_policy_test import pull_document
    from ticket_context_restore_test import HOOK_MODULE
    from scripts.ticket_board import session_context
    from scripts.ticket_board.notify_listener import PaneHookStateStore, TicketBoardNotifyListener
    from scripts.ticket_board.peer_identity import SessionIdentity, read_process
    from temporary_cluster import temporary_cluster

    session_context.BIND_SECONDS = 1.5
    session_context.CONFIRM_SECONDS = 1.5
    session_context.RESUME_WAIT_SECONDS = 0.6

    targets = {role: f"pgu-{role}:0.0" for role in roles}
    by_target = {target: role for role, target in targets.items()}
    # Every role a live stand-in provider process, registered as a pane registers it (the listener types into none
    # that is not); the gate and the hook records name the same process.
    sleepers = {role: subprocess.Popen(["sleep", "3600"]) for role in roles}
    identities = {role: (p.pid, read_process(p.pid).start_time) for role, p in sleepers.items()}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build(cluster, "board", "fresh")
        tmp = Path(tempfile.mkdtemp(prefix=f"{prefix}."))
        app = fixture.TicketBoardApp(cluster.root / "f", cluster.root / "a", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            sql.admin = admin

            def act(ticket: str, op: str, who: str, **body):
                return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{op}", body, caller=who)

            def where(ticket: str) -> str:
                return sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")

            doc = pull_document()
            doc["project"] = "pgu"
            audit = next(r for r in doc["roles"] if r["name"] == "audit")
            doc["roles"].append({**audit, "name": "audit-2", "label": "Audit 2", "target": targets["audit-2"],
                                 "slot": None, "runtime": "codex", "ephemeral": True, "template_role": "audit"})
            for name in extra_reviewers:
                doc["roles"].append({**audit, "name": name, "label": name.title(), "target": targets[name],
                                     "slot": None, "template_role": "audit"})
            for role in doc["roles"]:
                if role["name"] in targets:
                    role["target"] = targets[role["name"]]
                elif role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            for stage in doc["stages"]:
                if stage["name"] == "audit":
                    stage["owners"] = list(reviewers)
            for transition in doc["transitions"]:
                if transition["from"] == "audit" and "audit" in transition["actors"]:
                    transition["actors"] = sorted(set(transition["actors"]) | set(reviewers[1:]))
            app.apply_workflow(doc, expected_revision=0, dry_run=False, caller_role="director")
            runtimes = {r["name"]: r.get("runtime") for r in doc["roles"]}
            for role, (pid, start) in identities.items():
                sql("INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                    f"process_pid, process_start_time, process_uid) VALUES ('{role}', '{runtimes[role]}', '{targets[role]}', "
                    f"'/work/{role}', '/s/{role}', {pid}, {start}, {os.getuid()});")

            state_dir = tmp / "state"
            gate = Gate(state_store=PaneHookStateStore(state_dir))
            gate.proc_root = tmp / "proc"
            gate.role_targets = dict(targets)
            gate.busy = set()
            gate.role_identities = {}
            for role, (pid, start) in identities.items():
                proc_entry(gate.proc_root, pid, start)
                gate.role_identities[role] = SessionIdentity(pid, start)
            sent: list = []
            counter = {"n": 0}

            def session_start(role: str, session_id: str, source: str) -> None:
                pid, start = identities[role]
                HOOK_MODULE._write_state(state_dir, targets[role], "idle", source="claude.SessionStart", session_start={
                    "session_id": session_id, "source": source, "at": time.time(), "pane_pid": pid,
                    "pane_start_time": start, "checkout": {"cwd": f"/work/{role}", "branch": "topic", "head": "d" * 40}})

            def idle(role: str) -> None:
                gate.busy.discard(targets[role])
                HOOK_MODULE._write_state(state_dir, targets[role], "idle", source="claude.Stop",
                                         checkout={"cwd": f"/work/{role}", "branch": "topic", "head": "d" * 40,
                                                   "at": time.time()})

            def busy(role: str) -> None:
                gate.busy.add(targets[role])
                HOOK_MODULE._write_state(state_dir, targets[role], "busy", source="claude.UserPromptSubmit")

            def panes(target: str, text: str) -> None:
                role = by_target.get(target)
                sent.append((role, text))
                if role in (None, "director"):
                    return
                if text == "/clear":
                    counter["n"] += 1
                    session_start(role, f"{role}-s{counter['n']}", "clear")
                elif text.startswith("/"):
                    pass  # a /resume: Codex has none, and nothing here answers one
                elif role in reviewers and " -- " in text:
                    busy(role)  # a reviewer handed a ticket reviews it until its turn ends
                else:
                    idle(role)

            for role in roles:
                session_start(role, f"{role}-launch", "startup")

            def listener() -> TicketBoardNotifyListener:
                made = TicketBoardNotifyListener(
                    conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener"),
                    sender=panes, activity_gate=gate.is_working, target_exists=lambda _t: True,
                    submission_witness=lambda *_a: True, poll_seconds=0, session_clear_settle_seconds=0,
                    project="pgu", logger=logging.getLogger(prefix))
                made.role_targets = dict(targets)
                made.unresolved_turn_grace_seconds = 3600
                return made

            current = listener()

            def settle(count: int = 6) -> None:
                for _ in range(count):
                    # The listener's cap counts every delivery it ever made, so each pass allows 20 more.
                    current.listen_once(max_notifications=current.delivered_count + 20)
                    time.sleep(0.2)

            def handed(role: str, ticket: str, since: int = 0) -> list:
                return [text for r, text in sent[since:] if r == role and text.startswith(ticket + " -- ")]

            def to_audit(ticket: str, author: str) -> None:
                sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET commit_exempt = true "
                    f"WHERE id = '{ticket}';")
                act(ticket, "submit_to_inspection", author)
                if where(ticket).startswith("inspection/"):
                    act(ticket, "inspector_sign_off", "inspector")

            def seeded(ticket: str, author: str, state: str = "in_progress") -> None:
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state=state, assignee=author,
                                             commit_exempt=True)

            def hold(role: str) -> None:
                """`role`'s provider out of quota (SYRD-644), as the listener records it from the provider's own hook."""
                registration = json.loads(sql("SELECT row_to_json(a)::text FROM (SELECT generation, process_pid AS pid, "
                                              "process_start_time AS start_time FROM ticket_board.role_runtime_assignments "
                                              f"WHERE role = '{role}') a;"))
                evidence = json.dumps({"runtime": runtimes[role], **registration, "source": "claude.StopFailure",
                                       # A failure after its last turn end, so the hold is not ended as recovered.
                                       "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5)),
                                       "turn": "stopfailure:test", "reset_at": time.strftime(
                                           "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)), "message": "limit"})
                sql(f"SET ROLE ticket_board_listener; SELECT ticket_board.note_provider_quota('{role}', '{evidence}'::jsonb);")

            def release(role: str) -> None:
                sql("UPDATE ticket_board.provider_quota_holds SET state = 'ended', ended_at = clock_timestamp(), "
                    f"end_reason = 'cleared: test' WHERE role = '{role}' AND state IN ('held', 'released');")

            def stop(role: str) -> None:
                """`role`'s registered provider process gone, as the listener's gate sees it: its pid now names a
                process that started later."""
                pid, started = identities[role]
                proc_entry(gate.proc_root, pid, started + 1)

            def start(role: str) -> None:
                proc_entry(gate.proc_root, *identities[role])

            def comments(ticket: str, needle: str) -> list:
                return [c["text"] for c in app.get_ticket(ticket)["comments"] if needle in c["text"]]

            yield SimpleNamespace(
                sql=sql, act=act, where=where, settle=settle, handed=handed, to_audit=to_audit, seeded=seeded,
                idle=idle, busy=busy, hold=hold, release=release, stop=stop, start=start, comments=comments,
                session_start=session_start, listener=current,
                sent=sent, app=app, cluster=cluster, fixture=fixture, session_context=session_context, admin=admin)
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp, ignore_errors=True)
            for sleeper in sleepers.values():
                sleeper.kill()
                sleeper.wait()
