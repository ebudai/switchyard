#!/usr/bin/env python3
"""SYRD-714: the board records each event that needs the User, and nothing else, without touching pane delivery.

Production-built boards (schema.sql, companion roles, rbac.sql, the real ticket-board-migrate, rbac.sql again) with
the shipped workflow, the real HTTP server and the real notify listener with an injected sender. Main runs first,
from a git archive of its own scripts, examples and tests, then this tree; the same steps on each:

- a ticket reaches the User's review, goes back to the Director and returns: two `user_review` events, and one for a
  ticket the User has already signed off when read, which no longer stands;
- `operator:eric` is set, set again beside another blocker (set_blockers deletes and re-inserts it), released, and set
  once more: two `operator_blocker` events; `operator:alice` is its own;
- privileged jobs: running then failed, timed out, and succeeded: two `operation_failed` events;
- comments, routing, reassignment and every other transition: nothing;
- the role-pane notification queue and trace after all of it are identical on main and here;
- GET /api/user-events is bounded, ordered, keyed, carries no body or comment, and is the service's read only.

Main's board, upgraded with this tree's migrations, records events from then on, and schema.sql's last definitions
are the migration's.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "16b8f0681008a1fbd4b4544e21216ffc0c439804"
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1021_syrd714_user_events.sql"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
SECRET_BODY = "BODY-must-never-be-served"
SECRET_COMMENT = "COMMENT-must-never-be-served"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def migrate(fixture, admin: str, root: Path) -> None:
    result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())


def build_board(fixture, cluster, root: Path, dbname: str, *, roles: bool) -> str:
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
    if roles:
        fixture.create_roles(admin)
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
    migrate(fixture, admin, root)
    return admin


def workflow(root: Path) -> dict:
    cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return cfg


class Board:
    def __init__(self, fixture, cluster, admin: str, dbname: str) -> None:
        import psycopg
        from scripts.ticket_board.notify_listener import TicketBoardNotifyListener

        self.fixture, self.admin, self.psycopg = fixture, admin, psycopg
        self.service = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE)
        self.app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}",
                                          project="pgu", ticket_prefix="PGU", database_url=self.service)
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        self.sent: list[tuple[str, str]] = []
        self.listener = TicketBoardNotifyListener(
            conninfo=self.listener_url, sender=lambda target, message: self.sent.append((target, message)),
            activity_gate=lambda _target: False, target_exists=lambda _target: True,
            submission_witness=lambda target, _since: any(t == target for t, _m in self.sent),
            poll_seconds=0, project="pgu")

    def act(self, ticket: str, operation: str, actor: str, **payload) -> dict:
        try:
            return {"ok": self.fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload,
                                                 caller=actor)}
        except Exception as exc:  # a refusal is an answer
            return {"refused": str(exc)[:300]}

    def sql(self, statement: str, url: str | None = None) -> str:
        return self.fixture.psql(url or self.admin, statement).strip()

    def as_service(self, statement: str) -> str:
        try:
            with self.psycopg.connect(self.service, autocommit=True) as conn:
                row = conn.execute(statement).fetchone()
            return f"ok {row[0] if row else ''}"
        except Exception as exc:
            return f"refused {str(exc).splitlines()[0]}"

    def get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"{self.base}{path}", timeout=30) as response:
                return response.status, response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def one_pass(self) -> None:
        with self.psycopg.connect(self.listener_url, autocommit=True) as conn:
            self.listener.refresh_workflow(conn)
            self.listener.process_due_notifications(conn)

    def uat(self) -> bool:
        return self.sql("SELECT to_regprocedure('ticket_board.set_uat_instructions(text,text,text,text,boolean)') "
                        "IS NOT NULL;") == "t"

    def to_user(self, ticket: str, author: str) -> list[dict]:
        steps = [self.act(ticket, "route", "director", state="in_progress", assignee=author),
                 self.act(ticket, "submit_to_audit_without_commit", author, reason="finished"),
                 self.act(ticket, "audit_sign_off", "audit", text="Audited.")]
        if self.uat():
            steps.append(self.act(ticket, "set_uat_instructions", "director", do_this="Open it.",
                                  you_should_see="It works."))
        steps.append(self.act(ticket, "director_dat_sign_off", "director"))
        return steps

    def job(self, job_id: str, ticket: str, action: str, state: str) -> None:
        self.sql("INSERT INTO ticket_board.privileged_jobs (job_id, ticket_id, action, action_id, state, record_dir) "
                 f"VALUES ('{job_id}', '{ticket}', '{action}', '{action}', '{state}', '/var/lib/x/{job_id}');")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def delivery(board: Board) -> dict:
    """Everything role-pane delivery holds, without ids and clocks: what must be the same with and without events."""
    return {
        "queue": board.sql("SELECT coalesce(json_agg(json_build_array(ticket_id, kind, target_role, message, "
                           "attempts, dead_lettered_at IS NOT NULL) ORDER BY ticket_id, kind, target_role, message), '[]') "
                           "FROM ticket_board.ticket_notification_queue;"),
        "trace": board.sql("SELECT coalesce(json_agg(json_build_array(ticket_id, kind, target_role, event) "
                           "ORDER BY ticket_id, kind, target_role, event), '[]') FROM ticket_board.notification_trace;"),
        "sent": sorted(board.sent),
    }


def steps(board: Board, fixture) -> dict:
    """The same board history on every tree."""
    A, B, C, D, E = "PGU-901", "PGU-902", "PGU-903", "PGU-904", "PGU-905"
    for ticket in (A, B, C, D, E):
        fixture.seed_postgres_ticket(board.admin, ticket, title=f"{ticket} title", state="analysis",
                                     assignee="director", commit_exempt=True, needs_user_signoff=True)
    seen: dict = {"bodies": [board.act(t, "edit_fields", "director", body=SECRET_BODY) for t in (A, B, C, D, E)]
                  + [board.act(A, "add_comment", "director", text=SECRET_COMMENT)]}
    board.one_pass()
    seen["a_to_user"] = board.to_user(A, "app")
    board.one_pass()
    if board.uat():
        seen["a_back"] = [board.act(A, "user_send_to_director", "user", reason="UAT: not yet"),
                          board.act(A, "set_uat_instructions", "director", do_this="Open it again.",
                                    you_should_see="It works now."),
                          board.act(A, "director_return_to_uat", "director")]
    else:
        seen["a_back"] = [board.act(A, "user_send_to_director", "user", reason="UAT: not yet")]
    # A write that names the stage and assignee without changing them is not a new entry to User Review.
    try:
        board.sql("BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); UPDATE ticket_board.tickets "
                  f"SET state = state, assignee = assignee WHERE id = '{A}'; COMMIT;")
        seen["rewrite"] = [{"ok": True}]
    except AssertionError as exc:
        seen["rewrite"] = [{"refused": str(exc)[:300]}]
    seen["d_to_user"] = board.to_user(D, "ops") + [board.act(D, "user_sign_off", "user")]
    board.one_pass()
    seen["b_blockers"] = [
        board.act(B, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="needs Eric's pkexec"),
        board.act(B, "set_blockers", "director", blocked_by=["operator:eric", C], blocked_reason="and C"),
        board.act(B, "release_external_blocker", "director", ref="operator:eric", reason="Eric approved"),
        board.act(B, "set_blockers", "director", blocked_by=["operator:eric", C], blocked_reason="again"),
        board.act(C, "set_blockers", "director", blocked_by=["operator:alice"], blocked_reason="Alice's"),
    ]
    board.one_pass()
    # Noise: none of it is the User's.
    seen["noise"] = [board.act(E, "add_comment", "director", text="routine"),
                     board.act(E, "route", "director", state="in_progress", assignee="main"),
                     board.act(E, "add_comment", "main", text="progress: tests pass"),
                     board.act(E, "submit_to_audit_without_commit", "main", reason="done"),
                     board.act(E, "audit_kick_back", "audit", reason="not yet")]
    board.one_pass()
    board.job("0001-20261031T000000Z", E, "deploy-release", "running")
    board.sql("UPDATE ticket_board.privileged_jobs SET state = 'failed' WHERE job_id = '0001-20261031T000000Z';")
    board.job("0002-20261031T000000Z", E, "upgrade-tenant", "timed_out")
    board.job("0003-20261031T000000Z", E, "deploy-release", "succeeded")
    board.one_pass()
    seen["delivery"] = delivery(board)
    seen["where"] = {t: board.sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{t}';")
                     for t in (A, B, C, D, E)}
    return seen


def reads(board: Board) -> dict:
    seen: dict = {}
    seen["all"] = board.get("/api/user-events?after=0&limit=50")
    seen["capped"] = board.get("/api/user-events?after=0&limit=500")
    seen["page"] = board.get("/api/user-events?after=1&limit=2")
    seen["bad_after"] = board.get("/api/user-events?after=abc")
    seen["service_select"] = board.as_service("SELECT count(*) FROM ticket_board.user_events")
    seen["service_insert"] = board.as_service("INSERT INTO ticket_board.user_events (kind, ticket_id) "
                                              "VALUES ('user_review', 'PGU-905') RETURNING id")
    try:
        seen["listener_read"] = board.sql("SELECT ticket_board.user_events_after(0, 5)::text;", board.listener_url)
    except AssertionError as exc:  # psql's error
        seen["listener_read"] = str(exc)[:300]
    return seen


class Quiet:
    def notify_ticket_created(self, *a, **k) -> None: ...
    def notify_change(self, *a, **k) -> None: ...


def socket_post(path: Path, operation: str, payload: dict) -> tuple[int, dict | str]:
    import http.client
    import socket as socket_mod

    class Connection(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
            self.sock.connect(str(path))

    connection = Connection("localhost", timeout=60)
    try:
        connection.request("POST", f"/api/tickets/actions/{operation}", body=json.dumps(payload),
                           headers={"Content-Type": "application/json", "X-Ticket-Board-Caller-Role": "director"})
        response = connection.getresponse()
        text = response.read().decode("utf-8", "replace")
    finally:
        connection.close()
    try:
        return response.status, json.loads(text)
    except json.JSONDecodeError:
        return response.status, text


def director_notices(board: Board, fixture, cluster) -> dict:
    """The Director's ad hoc notification from its registered pane over the board's socket, under process authority:
    this process is registered as the Director's live pane."""
    from scripts.ticket_board.peer_identity import SessionIdentity, read_process
    from scripts.ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer

    me, start = os.getpid(), read_process(os.getpid()).start_time
    sleeper = subprocess.Popen(["sleep", "600"])
    register = ("DELETE FROM ticket_board.role_runtime_assignments WHERE role IN ('director', 'app'); "
                "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, session_dir, "
                "process_pid, process_start_time, process_uid) VALUES ('{role}', 'claude', 'pgu-{role}:0.0', '/w', '/s', "
                "{pid}, {start}, {uid});")
    board.sql(register.format(role="director", pid=me, start=start, uid=os.getuid()))
    authority = ProcessRoleAuthority(board.app, "tenant", resolve_uid=lambda _a: os.getuid(),
                                     resolve_session=lambda pid: SessionIdentity(pid, read_process(pid).start_time),
                                     session_live=lambda _i: True)
    path = cluster.root / "board.sock"
    server = TicketBoardUnixServer(path, board.app, events=Quiet(), director_notifier=Quiet(), role_authority=authority)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    seen: dict = {}
    count = lambda: board.sql("SELECT count(*) FROM ticket_board.director_notices;")  # noqa: E731
    try:
        before = (delivery(board), board.sql("SELECT state || assignee || (SELECT count(*) FROM "
                                             "ticket_board.ticket_comments WHERE ticket_id = 'PGU-901') FROM "
                                             "ticket_board.tickets WHERE id = 'PGU-901';"))
        note = {"title": "Deploy finished", "message": "The release is live; nothing needs you yet.", "ticket": "PGU-901"}
        try:
            seen["http"] = {"ok": fixture.post_json(board.base, "/api/tickets/actions/notify_user", note,
                                                    caller="director")}
        except AssertionError as exc:
            seen["http"] = {"refused": str(exc)[:300]}
        seen["preview"] = socket_post(path, "notify_user", note)
        if seen["preview"][0] != 200:  # main: there is no such action
            return seen
        digest = seen["preview"][1]["digest"]
        # The same preview through `ticket-board-write notify-user`, from this process: the command's own wiring.
        import contextlib
        from scripts.ticket_board import write_cli
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = write_cli.main(["--socket", str(path), "--caller-role", "director", "notify-user", "--title",
                                   note["title"], "--message", note["message"], "--ticket", "pgu-901"])
        seen["cli"] = [code, json.loads(printed.getvalue() or "{}").get("digest") == digest]
        seen["previewed_count"] = count()
        seen["wrong_digest"] = socket_post(path, "notify_user", {**note, "confirm": "0" * 64})
        seen["changed"] = socket_post(path, "notify_user", {**note, "message": "Something else.", "confirm": digest})
        seen["sent"] = socket_post(path, "notify_user", {**note, "confirm": digest})
        seen["again"] = socket_post(path, "notify_user", {**note, "confirm": digest})
        seen["after_count"] = count()
        seen["requester"] = board.sql("SELECT requester_role || ' ' || requester_pid || ' ' || requester_start_time "
                                      "|| ' ' || requester_generation FROM ticket_board.director_notices ORDER BY id "
                                      "LIMIT 1;")
        seen["expected_requester"] = f"director {me} {start} 1"
        seen["after"] = (delivery(board), board.sql("SELECT state || assignee || (SELECT count(*) FROM "
                                                    "ticket_board.ticket_comments WHERE ticket_id = 'PGU-901') FROM "
                                                    "ticket_board.tickets WHERE id = 'PGU-901';"))
        seen["before"] = before
        token = board.server.write_token
        piece = next(token[i:i + 20] for i in range(len(token) - 19)  # 20 of its characters, letters and digits
                     if re.search(r"[0-9]", token[i:i + 20]) and re.search(r"[A-Za-z]", token[i:i + 20]))
        bad = {"newline": {"title": "Deploy\nfinished"}, "url": {"message": "see https://evil.example/x"},
               "www": {"message": "see www.example.com"}, "token": {"message": "key " + "A1b2" * 8},
               "write_token": {"message": f"use {piece}"},
               "secret": {"message": "password: hunter2"}, "qualified": {"message": "like otto:OTTO-5"},
               "other_prefix": {"message": "like OTTO-5"}, "other_ticket": {"ticket": "OTTO-5"},
               "long": {"title": "x" * 81}, "empty": {"message": "  "}, "bidi": {"title": "a\u202eb"},
               "unknown_field": {"recipient": "someone"}, "missing_ticket": {"ticket": "PGU-999"},
               "urgent_no_reason": {"urgent": True}, "reason_not_urgent": {"reason": "because"}}
        seen["bad"] = {}
        for name, change in bad.items():
            payload = {**note, **change}
            first = socket_post(path, "notify_user", payload)
            if first[0] == 200 and isinstance(first[1], dict) and "digest" in first[1]:
                first = socket_post(path, "notify_user", {**payload, "confirm": first[1]["digest"]})
            seen["bad"][name] = [first[0], str(first[1])[:200]]
        # The write token itself, whole, is refused even where no shape catches it.
        seen["bad"]["write_token_whole"] = list(socket_post(path, "notify_user", {**note, "message": token}))
        seen["bad_count"] = count()

        def send(**change) -> tuple[int, dict | str]:
            payload = {**note, **change}
            shown = socket_post(path, "notify_user", payload)
            return socket_post(path, "notify_user", {**payload, "confirm": shown[1]["digest"]}) \
                if shown[0] == 200 else shown
        seen["urgent"] = send(message="Urgent one.", urgent=True, reason="the deploy is half done")
        seen["urgent_again"] = send(message="Urgent two.", urgent=True, reason="still")
        seen["normals"] = [send(message=f"Normal {n}.") for n in range(3)]
        seen["over_rate"] = send(message="One too many.")
        seen["rate_count"] = count()
        seen["events"] = board.get("/api/user-events?after=0&limit=50")
        # A retired Director: its registration moves to another process, and this one is nobody's pane.
        board.sql(register.format(role="director", pid=sleeper.pid, start=read_process(sleeper.pid).start_time,
                                  uid=os.getuid()))
        seen["retired"] = socket_post(path, "notify_user", note)
        # A pane of another role.
        board.sql("INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                  f"session_dir, process_pid, process_start_time, process_uid) VALUES ('app', 'claude', 'pgu-app:0.0', "
                  f"'/w', '/s', {me}, {start}, {os.getuid()});")
        seen["app_pane"] = socket_post(path, "notify_user", note)
        # The database alone, as the service with caller role director, naming a process that is not the pane.
        try:
            seen["db_stale"] = board.fixture.psql(board.service, (
                "BEGIN; SELECT set_config('ticket_board.caller_role', 'director', true); SELECT "
                f"ticket_board.record_director_notice({me}, {start}, {os.getuid()}, 't', 'm', NULL, false, '', "
                f"'{'a' * 64}'); COMMIT;"))
        except AssertionError as exc:
            seen["db_stale"] = str(exc)[:300]
        try:
            seen["db_app"] = board.fixture.psql(board.service, (
                "BEGIN; SELECT set_config('ticket_board.caller_role', 'app', true); SELECT "
                f"ticket_board.record_director_notice({me}, {start}, {os.getuid()}, 't', 'm', NULL, false, '', "
                f"'{'b' * 64}'); COMMIT;"))
        except AssertionError as exc:
            seen["db_app"] = str(exc)[:300]
    finally:
        server.shutdown()
        server.server_close()
        sleeper.kill()
        sleeper.wait()
    return seen


def resilience(board: Board, fixture) -> dict:
    """This tree only: the read's cap with real volume, recording that fails, and pruning."""
    seen: dict = {}
    board.sql("INSERT INTO ticket_board.privileged_jobs (job_id, ticket_id, action, action_id, state, record_dir) "
              "SELECT lpad(n::text, 4, '0') || '-20261101T000000Z', 'PGU-905', 'deploy-release', 'deploy-release', "
              "'failed', '/var/lib/x/' || n FROM generate_series(100, 160) n;")
    seen["capped"] = len(json.loads(board.get("/api/user-events?after=0&limit=500")[1])["events"])
    seen["floor"] = len(json.loads(board.get("/api/user-events?after=0&limit=0")[1])["events"])
    # The record refuses every row: the change that would have caused an event still happens.
    board.sql("ALTER TABLE ticket_board.user_events ADD CONSTRAINT syrd714_refuse CHECK (false) NOT VALID;")
    fixture.seed_postgres_ticket(board.admin, "PGU-960", title="still works", state="analysis", assignee="director",
                                 commit_exempt=True, needs_user_signoff=True)
    before = board.sql("SELECT count(*) FROM ticket_board.user_events;")
    seen["blocked_anyway"] = board.act("PGU-960", "set_blockers", "director", blocked_by=["operator:eric"],
                                       blocked_reason="the record is broken")
    seen["unrecorded"] = board.sql("SELECT count(*) FROM ticket_board.user_events;") == before
    board.sql("ALTER TABLE ticket_board.user_events DROP CONSTRAINT syrd714_refuse;")
    # Pruning: an event older than 30 days goes when the next one is recorded.
    board.sql("UPDATE ticket_board.user_events SET at = clock_timestamp() - interval '31 days' WHERE id = 1;")
    board.act("PGU-960", "release_external_blocker", "director", ref="operator:eric", reason="done")
    board.act("PGU-960", "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="again")
    seen["pruned"] = board.sql("SELECT count(*) FROM ticket_board.user_events WHERE id = 1;") == "0"
    seen["recorded_again"] = board.sql("SELECT count(*) FROM ticket_board.user_events WHERE ticket_id = 'PGU-960';")
    return seen


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    assert Path(fixture.__file__).resolve().is_relative_to(root.resolve()), fixture.__file__
    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board", roles=True)
        board = Board(fixture, cluster, admin, "board")
        try:
            board.app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
            out["steps"] = steps(board, fixture)
            out["reads"] = reads(board)
            out["director"] = director_notices(board, fixture, cluster)
            if board.sql("SELECT to_regclass('ticket_board.user_events') IS NOT NULL;") == "t":
                out["resilience"] = resilience(board, fixture)
        finally:
            board.close()
    return out


def upgrade(before_root: Path, prefix: str) -> dict:
    """Main's board, built by main, then upgraded with this tree's migrations and rbac.sql, as a deploy does."""
    for extra in (str(ROOT), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    out: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, before_root, "board", roles=True)
        fixture.seed_postgres_ticket(admin, "PGU-950", title="before the upgrade", state="analysis",
                                     assignee="director", commit_exempt=True)
        migrate(fixture, admin, ROOT)
        board = Board(fixture, cluster, admin, "board")
        try:
            board.app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            out["blocked"] = board.act("PGU-950", "set_blockers", "director", blocked_by=["operator:eric"],
                                       blocked_reason="after the upgrade")
            out["events"] = board.get("/api/user-events")
        finally:
            board.close()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path, prefix: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), prefix],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def every_step_answered(seen: dict) -> None:
    for name in ("bodies", "a_to_user", "a_back", "rewrite", "d_to_user", "b_blockers", "noise"):
        refused = [step for step in seen[name] if "refused" in step]
        check(not refused, f"{name}: every step is accepted, on either tree: {refused}")


def before(got: dict) -> None:
    every_step_answered(got["steps"])
    check(got["reads"]["all"][0] == 404, f"main: no user-events read: {got['reads']['all']}")
    check(got["reads"]["service_select"].startswith("refused") and "user_events" in got["reads"]["service_select"],
          f"main: no record of the User's events at all: {got['reads']['service_select']}")
    check(got["director"]["preview"][0] >= 400, f"main: no Director's notification: {got['director']['preview']}")


def director(got: dict) -> None:
    d = got["director"]
    check("refused" in d["http"] and "process authority" in d["http"]["refused"],
          f"over HTTP, a role named in a header is no Director: {d['http']}")
    status, shown = d["preview"]
    check(status == 200 and shown["recorded"] is False and shown["preview"]["title"] == "pgu Director: Deploy finished"
          and shown["preview"]["message"] == "The release is live; nothing needs you yet. -- PGU-901"
          and re.fullmatch(r"[0-9a-f]{64}", shown["digest"]) and d["previewed_count"] == "0",
          f"the Director's pane gets a preview of exactly what the phone shows, and nothing is recorded: {d['preview']}")
    check(d["cli"] == [0, True], f"ticket-board-write notify-user previews the same notification: {d['cli']}")
    check(d["wrong_digest"][0] == 400 and "not the notification that was previewed" in str(d["wrong_digest"][1])
          and d["changed"][0] == 400 and "not the notification that was previewed" in str(d["changed"][1]),
          f"a send must name the digest of exactly what it sends: {d['wrong_digest']} {d['changed']}")
    sent, again = d["sent"][1], d["again"][1]
    check(d["sent"][0] == 200 and sent["recorded"] is True and sent["duplicate"] is False
          and d["again"][0] == 200 and again["duplicate"] is True and again["notice"] == sent["notice"]
          and d["after_count"] == "1",
          f"confirmed, it is recorded once; the same confirmation again is the same notice: {sent} {again}")
    check(d["requester"] == d["expected_requester"],
          f"the ledger names the pane the kernel named: role, pid, start time, generation: {d['requester']}")
    check(d["after"] == d["before"], "advisory: no comment, move, sign-off or pane notice comes of it")
    refusals = {name: answer for name, answer in d["bad"].items()}
    expected = {"newline": "control", "url": "URL", "www": "URL", "token": "credential", "write_token": "credential",
                "write_token_whole": "credential", "secret": "credential", "qualified": "another board",
                "other_prefix": "another board", "other_ticket": "this board's PGU-N", "long": "longer than 80",
                "empty": "empty", "bidi": "control", "unknown_field": "does not take recipient",
                "missing_ticket": "not on this board", "urgent_no_reason": "reason is empty",
                "reason_not_urgent": "urgent notification only"}
    for name, word in expected.items():
        check(refusals[name][0] >= 400 and word in refusals[name][1], f"{name}: refused ({word}): {refusals[name]}")
    check(d["bad_count"] == "1", f"none of them was recorded: {d['bad_count']}")
    check(d["urgent"][0] == 200 and d["urgent"][1]["recorded"] and d["urgent_again"][0] == 400
          and "at most 1 urgent" in str(d["urgent_again"][1]),
          f"an urgent notification needs a reason, and only one an hour: {d['urgent']} {d['urgent_again']}")
    check(all(answer[0] == 200 for answer in d["normals"]) and d["over_rate"][0] == 400
          and "at most 5" in str(d["over_rate"][1]) and d["rate_count"] == "5",
          f"at most five an hour: {d['over_rate']} {d['rate_count']}")
    events = [e for e in json.loads(d["events"][1])["events"] if e["kind"] == "director_ad_hoc"]
    first = events[0]
    check(len(events) == 5 and first["ticket"] == "PGU-901" and first["detail"] == "normal"
          and first["notice"]["title"] == "Deploy finished" and first["notice"]["digest"] == shown["digest"]
          and first["notice"]["requester_role"] == "director" and first["notice"]["urgent"] is False
          and events[1]["detail"] == "urgent" and events[1]["notice"]["reason"] == "the deploy is half done"
          and first["key"].startswith("director_ad_hoc:PGU-901:"),
          f"each is a director_ad_hoc event for the bridge, with its notice and digest: {events[:2]}")
    check(d["retired"][0] == 403 and "no PostgreSQL role assignment" in str(d["retired"][1]),
          f"a retired Director process is nobody's pane: {d['retired']}")
    check(d["app_pane"][0] == 403 and "app cannot call notify_user" in str(d["app_pane"][1]),
          f"another role's pane cannot send one: {d['app_pane']}")
    check("is not director's registered live pane" in d["db_stale"]
          and "only the control role sends" in d["db_app"],
          f"the database refuses a stale process and a non-control role itself: {d['db_stale']} {d['db_app']}")


def resilient(got: dict) -> None:
    r = got["resilience"]
    check(r["capped"] == 50 and r["floor"] == 1, f"a read returns at most 50 events, and at least one: {r}")
    check("ok" in r["blocked_anyway"] and r["unrecorded"] is True,
          f"an event the record cannot take never fails the change that caused it: {r['blocked_anyway']}")
    check(r["pruned"] and r["recorded_again"] == "1", f"events older than 30 days are pruned as new ones arrive: {r}")


def candidate(got: dict, main: dict) -> None:
    every_step_answered(got["steps"])
    check(got["steps"]["where"] == main["steps"]["where"], f"every ticket ends where it did on main: "
                                                           f"{got['steps']['where']} {main['steps']['where']}")
    check(got["steps"]["delivery"] == main["steps"]["delivery"],
          "the role-pane queue, trace and sends are exactly main's: recording the User's events changes no pane's "
          f"delivery: {json.dumps(got['steps']['delivery'])[:1500]}")
    status, text = got["reads"]["all"]
    check(status == 200, f"GET /api/user-events answers: {status} {text[:300]}")
    answer = json.loads(text)
    events = answer["events"]
    summary = [(e["kind"], e["ticket"], e["detail"], e["standing"]) for e in events]
    check(summary == [
        ("user_review", "PGU-901", "user_review", True),
        ("user_review", "PGU-901", "user_review", True),
        ("user_review", "PGU-904", "user_review", False),
        ("operator_blocker", "PGU-902", "eric", True),
        ("operator_blocker", "PGU-902", "eric", True),
        ("operator_blocker", "PGU-903", "alice", True),
        ("operation_failed", "PGU-905", "deploy-release", True),
        ("operation_failed", "PGU-905", "upgrade-tenant", True),
    ],
          f"exactly the User's events, in order: two entries to User Review, one already signed off; operator:eric "
          f"twice (its re-set beside C is the same wait); alice's; two failed jobs, not the one that succeeded: "
          f"{summary}")
    ids = [e["id"] for e in events]
    check(ids == sorted(ids) and len(set(ids)) == len(ids) and answer["latest"] == ids[-1],
          f"ordered by id, and the latest is the last: {ids} {answer['latest']}")
    check(all(e["key"] == f"{e['kind']}:{e['ticket']}:{e['id']}:{e['key'].rsplit(':', 1)[-1]}" for e in events)
          and len({e["key"] for e in events}) == len(events), f"each event has its own stable key: {events}")
    check(all(e["title"] == f"{e['ticket']} title" for e in events) and SECRET_BODY not in text
          and SECRET_COMMENT not in text and all(e["notice"] is None for e in events)
          and set(events[0]) == {"id", "key", "kind", "ticket", "detail", "at", "title", "standing", "notice"},
          f"each event carries its short title and nothing else of the ticket: {events[0]}")
    again = json.loads(got["reads"]["capped"][1])
    check(len(again["events"]) == len(events) and [e["key"] for e in again["events"]] == [e["key"] for e in events],
          f"read again, the same events under the same keys; a limit of 500 is capped: {again}")
    page = json.loads(got["reads"]["page"][1])
    check([e["id"] for e in page["events"]] == [i for i in ids if i > 1][:2],
          f"after and limit page through them: {page}")
    check(got["reads"]["bad_after"][0] >= 400, f"a cursor that is not a number is refused: {got['reads']['bad_after']}")
    check(got["reads"]["service_insert"].startswith("refused")
          and "permission denied for table user_events" in got["reads"]["service_insert"]
          and got["reads"]["service_select"] == f"ok {len(events)}",
          f"only the triggers write the record: the service cannot, though it reads it as it reads every board table: "
          f"{got['reads']['service_select']} {got['reads']['service_insert']}")
    check("permission denied for function user_events_after" in got["reads"]["listener_read"],
          f"the bridge's read is the service's alone, not the listener's: {got['reads']['listener_read'][:200]}")


def upgraded(got: dict) -> None:
    check("ok" in got["blocked"], f"upgraded board: the blocker is set: {got['blocked']}")
    status, text = got["events"]
    events = json.loads(text)["events"] if status == 200 else []
    check([(e["kind"], e["ticket"], e["detail"]) for e in events] == [("operator_blocker", "PGU-950", "eric")],
          f"main's board, upgraded by this tree's migrations, records events from then on: {status} {text[:300]}")


def mirrored() -> None:
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    body = MIGRATION.read_text().split("BEGIN;\n", 1)[1].rsplit("COMMIT;", 1)[0].strip("\n")
    check(schema.rfind(body) != -1 and schema.rfind(body) == schema.rfind(body.split("\n", 1)[0]),
          "schema.sql's last copy of these objects is the migration's, byte for byte")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3])))
        return 0
    if len(sys.argv) == 4 and sys.argv[1] == "--upgrade":
        print(json.dumps(upgrade(Path(sys.argv[2]), sys.argv[3])))
        return 0
    mirrored()
    if os.environ.get("SYRD714_MUTATION_SKIP_BEFORE") == "1":  # a mutant changes this tree only
        got = run(ROOT, "s714a")
        candidate(got, got)
        director(got)
        resilient(got)
        print(f"user_events_board_test: {CHECKS} checks ok (mutation run: main's replay and the upgrade skipped)")
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd714-before.", dir="/tmp") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        main_run = run(before_root, "s714b")
        before(main_run)
        env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--upgrade", str(before_root), "s714u"],
                               capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        upgraded(json.loads(child.stdout.strip().splitlines()[-1]))
    got = run(ROOT, "s714a")
    candidate(got, main_run)
    director(got)
    resilient(got)
    print(f"user_events_board_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
