#!/usr/bin/env python3
"""SYRD-723: the implementer that owns a ticket keeps its blocker explanation current, and changes nothing else.

Production-built boards (schema.sql, companion roles, rbac.sql, the real ticket-board-migrate, rbac.sql again) with
the shipped workflow, the real HTTP server for the Director, and the real Unix-socket server under process
authority for the implementers: this process is registered as each implementer's live runtime in turn. Main runs
first, from a git archive of its own scripts, examples and tests, then this tree; the same steps on each.

- On main the owner has no way to change the text: not this operation, not set_blockers, not edit_fields.
- Here the owner sets, revises and clears it, from the socket and through `ticket-board-write set-blocked-reason`;
  each change is one field-audit row naming the role and its runtime, an unchanged text writes none, and nothing
  else about the ticket moves: blockers, state, assignee, the wait (an independent one survives, even its own).
- A standing blocker's explanation can be revised, not removed, and its links stay; once its blocker resolves the
  text can be cleared, and the resolved link still stays.
- Refused, changing nothing: another owner's ticket, a ticket that left implementation (deferred, reassigned), a
  request that also names blocked_by/state/assignee/awaiting_role, the HTTP port, a replaced runtime and a retired
  one (by the server, and by the database alone), a Director and a reviewer.
- A transition race, both orders: whichever commits first, the other sees it.
- The Director's set_blockers and the ticket editor's blocked_reason edit are unchanged; a board that declares no
  workflow, where no runtime can register, says where the operation works.

Main's board, upgraded with this tree's migrations, has the operation, and schema.sql's last copy is the migration's.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "a5641df4014c43cceae2a9e46b3e7ebaa32c7858"
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1024_syrd723_blocker_explanation.sql"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0
#: set_blocked_reason's ACL between the migration runner and rbac.sql: a deploy's window.
ACL_BEFORE_RBAC: list = []


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def migrate(fixture, admin: str, root: Path) -> None:
    result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    ACL_BEFORE_RBAC.append(fixture.psql(admin, "SELECT coalesce(proacl::text, 'default') FROM pg_proc WHERE oid = "
                                        "to_regprocedure('ticket_board.set_blocked_reason(text, text, bigint, bigint, "
                                        "bigint)');").strip())
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())


def build_board(fixture, cluster, root: Path, dbname: str, *, roles: bool = True) -> str:
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
    if roles:  # the cluster's, once
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


class Quiet:
    def notify_ticket_created(self, *a, **k) -> None: ...
    def notify_change(self, *a, **k) -> None: ...


class Board:
    """One board: the Director over HTTP, implementers over the socket as this process's registered runtime."""

    def __init__(self, fixture, cluster, admin: str, dbname: str) -> None:
        import psycopg
        from scripts.ticket_board.peer_identity import SessionIdentity, read_process
        from scripts.ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer

        self.fixture, self.admin, self.psycopg = fixture, admin, psycopg
        self.service = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE)
        self.app = fixture.TicketBoardApp(cluster.root / f"frames-{dbname}", cluster.root / f"assets-{dbname}",
                                          project="pgu", ticket_prefix="PGU", database_url=self.service)
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.me, self.start, self.uid = os.getpid(), read_process(os.getpid()).start_time, os.getuid()
        authority = ProcessRoleAuthority(self.app, "tenant", resolve_uid=lambda _a: self.uid,
                                         resolve_session=lambda pid: SessionIdentity(pid, read_process(pid).start_time),
                                         session_live=lambda _i: True)
        self.path = cluster.root / f"{dbname}.sock"
        self.unix = TicketBoardUnixServer(self.path, self.app, events=Quiet(), director_notifier=Quiet(),
                                          role_authority=authority)
        threading.Thread(target=self.unix.serve_forever, daemon=True).start()

    def register(self, role: str, pid: int | None = None, generation: int = 1, runtime: str = "") -> None:
        """`role`'s live runtime is `pid` (this process by default), for the runtime and target the role declares
        unless `runtime` says otherwise: any other registration of the role, or of this process, is replaced, as a
        pane's re-registration replaces its predecessor."""
        from scripts.ticket_board.peer_identity import read_process

        pid = pid or self.me
        start = read_process(pid).start_time
        declared = f"(SELECT definition->>'runtime' FROM ticket_board.workflow_roles WHERE name = '{role}')"
        self.sql(f"DELETE FROM ticket_board.role_runtime_assignments WHERE role = '{role}' OR process_pid IN ({pid}, "
                 f"{self.me}); INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                 f"session_dir, process_pid, process_start_time, process_uid, generation) VALUES ('{role}', "
                 f"{repr(runtime) if runtime else declared}, 'pgu-{role}:0.0', '/w', '/s', {pid}, {start}, {self.uid}, "
                 f"{generation});")

    def act(self, ticket: str, operation: str, actor: str, **payload) -> dict:
        """The Director (or anyone) over the HTTP port, with the write token."""
        try:
            return {"ok": self.fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload,
                                                 caller=actor)}
        except Exception as exc:  # a refusal is an answer
            return {"refused": str(exc)[:400]}

    def pane(self, ticket: str, operation: str, payload: dict, role: str = "app") -> list:
        """A request from this process's registered pane, over the board's socket: [status, body]."""
        import http.client
        import socket as socket_mod

        path = self.path

        class Connection(http.client.HTTPConnection):
            def connect(self) -> None:
                self.sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                self.sock.connect(str(path))

        connection = Connection("localhost", timeout=60)
        try:
            connection.request("POST", f"/api/tickets/{ticket}/actions/{operation}", body=json.dumps(payload),
                               headers={"Content-Type": "application/json", "X-Ticket-Board-Caller-Role": role})
            response = connection.getresponse()
            text = response.read().decode("utf-8", "replace")
        finally:
            connection.close()
        try:
            return [response.status, json.loads(text)]
        except json.JSONDecodeError:
            return [response.status, text[:400]]

    def explain(self, ticket: str, text: str, role: str = "app", **extra) -> list:
        return self.pane(ticket, "set_blocked_reason", {"blocked_reason": text, **extra}, role)

    def sql(self, statement: str) -> str:
        return self.fixture.psql(self.admin, statement).strip()

    def state(self, ticket: str) -> dict:
        """Everything about the ticket this operation must not change, and the text it may."""
        row = self.sql(
            "SELECT json_build_object('state', t.state, 'assignee', t.assignee, 'reason', t.blocked_reason, "
            "'blockers', (SELECT coalesce(json_agg(b.blocker_ticket_id || CASE WHEN b.resolved THEN ' resolved' ELSE "
            "'' END ORDER BY b.position), '[]') FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id), "
            "'awaiting', coalesce(n.awaiting_role, ''), 'unresolved', ticket_board.ticket_has_unresolved_blockers(t.id), "
            "'audit', (SELECT count(*) FROM ticket_board.ticket_field_audit a WHERE a.ticket_id = t.id), "
            "'updated', t.updated_at) "
            "FROM ticket_board.tickets t LEFT JOIN ticket_board.ticket_notification_state n ON n.ticket_id = t.id "
            f"WHERE t.id = '{ticket}';")
        return json.loads(row)

    def audit(self, ticket: str) -> list:
        return json.loads(self.sql(
            "SELECT coalesce(json_agg(json_build_array(actor, field, old_value, new_value, reason) ORDER BY id), '[]') "
            f"FROM ticket_board.ticket_field_audit WHERE ticket_id = '{ticket}';"))

    def as_service(self, role: str, statement: str) -> str:
        """The database alone: the board's writer, saying the caller is `role`."""
        try:
            with self.psycopg.connect(self.service) as conn:
                conn.execute("SELECT set_config('ticket_board.caller_role', %s, false)", (role,))
                row = conn.execute(statement).fetchone()
            return f"ok {row[0] if row else ''}"
        except Exception as exc:
            return f"refused {str(exc).splitlines()[0][:300]}"

    def close(self) -> None:
        for server in (self.unix, self.server):
            server.shutdown()
            server.server_close()


def seed(board: Board) -> None:
    for ticket, state, assignee in (("PGU-901", "in_progress", "app"), ("PGU-902", "in_progress", "ops"),
                                    ("PGU-903", "in_progress", "main"), ("PGU-904", "analysis", "director"),
                                    ("PGU-905", "analysis", "director"), ("PGU-906", "analysis", "director")):
        board.fixture.seed_postgres_ticket(board.admin, ticket, title=f"ticket {ticket}", state=state,
                                           assignee=assignee, commit_exempt=True)


def main_routes(board: Board) -> dict:
    """Every way the owner might change the text."""
    board.register("app")
    seen = {"before": board.state("PGU-901")}
    seen["operation"] = board.explain("PGU-901", "waiting on the vendor's key")
    seen["set_blockers"] = board.pane("PGU-901", "set_blockers", {"blocked_by": [], "blocked_reason": "note"})
    seen["edit_fields"] = board.pane("PGU-901", "edit_fields", {"blocked_reason": "note"})
    seen["after"] = board.state("PGU-901")
    return seen


def owner(board: Board) -> dict:
    """The owner sets, revises, repeats and clears the text, from its pane and through the command."""
    from scripts.ticket_board import write_cli

    board.register("app")
    seen = {"before": board.state("PGU-901")}
    seen["set"] = board.explain("PGU-901", "  waiting on the vendor's API key  ")
    seen["after_set"] = board.state("PGU-901")
    seen["stored"] = board.sql("SELECT source_json->>'blocked_reason' FROM ticket_board.tickets WHERE id = 'PGU-901';")
    seen["ticket_json"] = board.act("PGU-901", "add_comment", "director", text="seen")  # the ticket as the API shows it
    seen["revise"] = board.explain("PGU-901", "the vendor sent a key; it is for the wrong account")
    seen["after_revise"] = board.state("PGU-901")
    seen["same"] = board.explain("PGU-901", "the vendor sent a key; it is for the wrong account")
    seen["after_same"] = board.state("PGU-901")
    seen["clear"] = board.explain("PGU-901", "")

    def cli(*argv: str) -> list:
        printed, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(errors):
            try:
                code = write_cli.main(["--socket", str(board.path), "--caller-role", "app", *argv])
            except SystemExit as exc:
                code = exc.code
            except Exception as exc:  # an answer, not a crash
                code, _ = f"raised {exc}", errors.write(str(exc))
        return [code, printed.getvalue().strip()[-300:], errors.getvalue().strip()[-300:]]

    seen["cli_set"] = cli("set-blocked-reason", "pgu-901", "--blocked-reason", "from the command, `literally` $HOME")
    seen["cli_text"] = board.state("PGU-901")["reason"]
    seen["cli_clear"] = cli("set-blocked-reason", "PGU-901", "--clear")
    seen["cli_neither"] = cli("set-blocked-reason", "PGU-901")
    seen["cli_both"] = cli("set-blocked-reason", "PGU-901", "--clear", "--blocked-reason", "x")
    seen["after_cli"] = board.state("PGU-901")
    seen["audit"] = board.audit("PGU-901")
    seen["runtime"] = [board.me]
    # Waits: the Director's, recorded by the owner's request, and one on the owner itself, are both left standing.
    seen["depend"] = board.act("PGU-901", "request_dependency", "app", role="director", reason="which account?")
    seen["wait_set"] = board.explain("PGU-901", "asked the Director which account")
    seen["wait_after"] = board.state("PGU-901")
    board.sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = 'app', awaiting_since_at = now() "
              "WHERE ticket_id = 'PGU-901';")
    seen["own_wait_set"] = board.explain("PGU-901", "still asking")
    seen["own_wait_after"] = board.state("PGU-901")
    board.sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = '', awaiting_since_at = NULL "
              "WHERE ticket_id = 'PGU-901';")
    return seen


def other_fields(board: Board) -> dict:
    """The operation carries the text and nothing else, whatever else the request names."""
    board.register("app")
    seen = {"before": board.state("PGU-901")}
    for field, value in (("blocked_by", ["PGU-904"]), ("state", "inspection"), ("assignee", "ops"),
                         ("awaiting_role", "director"), ("needs_audit", False)):
        seen[field] = board.explain("PGU-901", "with something else", **{field: value})
    seen["not_text"] = board.pane("PGU-901", "set_blocked_reason", {"blocked_reason": ["a"]})
    seen["missing"] = board.pane("PGU-901", "set_blocked_reason", {})
    seen["after"] = board.state("PGU-901")
    # The owner's other routes to the field are still refused.
    seen["set_blockers"] = board.pane("PGU-901", "set_blockers", {"blocked_by": ["PGU-904"], "blocked_reason": "x"})
    seen["edit_fields"] = board.pane("PGU-901", "edit_fields", {"blocked_reason": "x"})
    seen["http_port"] = board.act("PGU-901", "set_blocked_reason", "app", blocked_reason="over the port")
    seen["final"] = board.state("PGU-901")
    return seen


def standing(board: Board) -> dict:
    """A standing blocker: its explanation is revised, never removed, and its links stay."""
    board.register("ops")
    seen = {"blocked": board.act("PGU-902", "set_blockers", "director", blocked_by=["PGU-904", "operator:eric"],
                                 blocked_reason="needs PGU-904 and Eric's key")}
    seen["revise"] = board.explain("PGU-902", "PGU-904 is half done; Eric has the key ready", role="ops")
    seen["after_revise"] = board.state("PGU-902")
    seen["clear"] = board.explain("PGU-902", "   ", role="ops")
    seen["after_clear"] = board.state("PGU-902")
    seen["release"] = board.act("PGU-902", "release_external_blocker", "director", ref="operator:eric",
                                reason="Eric sent it")
    seen["revise_again"] = board.explain("PGU-902", "only PGU-904 now", role="ops")
    seen["cancel"] = board.act("PGU-904", "cancel", "director", reason="not needed after all")
    seen["clear_resolved"] = board.explain("PGU-902", "", role="ops")
    seen["after_resolved"] = board.state("PGU-902")
    return seen


def refusals(board: Board) -> dict:
    """Not the owner, not in implementation, not the live runtime: refused, and nothing changes."""
    seen: dict = {}
    board.register("app")
    seen["others_ticket"] = [board.explain("PGU-902", "app on ops's ticket"), board.state("PGU-902")]
    seen["director_ticket"] = [board.explain("PGU-905", "app on the Director's"), board.state("PGU-905")]
    board.register("director")
    seen["director"] = [board.explain("PGU-901", "the Director, this way", role="director"), board.state("PGU-901")]
    board.register("audit")
    seen["reviewer"] = [board.explain("PGU-901", "a reviewer", role="audit"), board.state("PGU-901")]
    # The Director owns PGU-905 in Triage: an owner and the assignee, but not of an implementation stage.
    board.register("director")
    seen["own_triage"] = [board.explain("PGU-905", "the Director's own", role="director"), board.state("PGU-905")]
    # A replaced runtime: app's registration moves to another process, then comes back as generation 2.
    sleeper = subprocess.Popen(["sleep", "600"])
    try:
        board.register("app", sleeper.pid)
        seen["replaced"] = [board.explain("PGU-901", "from the old pane"), board.state("PGU-901")]
        seen["db_replaced"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                                               f"{board.me}, {board.start}, {board.uid})")
        board.sql("DELETE FROM ticket_board.role_runtime_assignments WHERE role = 'app';")
        seen["retired"] = [board.explain("PGU-901", "from no pane"), board.state("PGU-901")]
        seen["db_retired"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                                              f"{board.me}, {board.start}, {board.uid})")
        # A stale one: registered for a runtime app no longer declares.
        board.register("app", runtime="codex")
        seen["stale"] = [board.explain("PGU-901", "from a runtime app no longer declares"), board.state("PGU-901")]
        seen["db_stale"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                                            f"{board.me}, {board.start}, {board.uid})")
        board.register("app", generation=2)
        seen["generation_2"] = board.explain("PGU-901", "from the new pane")
        seen["audit_2"] = (board.audit("PGU-901") or [[""] * 5])[-1]
        # The database alone, naming the right process for the wrong ticket, and another role's process.
        seen["db_others"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-902', 'db', "
                                             f"{board.me}, {board.start}, {board.uid})")
        seen["db_other_process"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                                                    f"{sleeper.pid}, 1, {board.uid})")
        seen["db_other_start"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                                                  f"{board.me}, {board.start + 1}, {board.uid})")
        seen["db_other_uid"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                                                f"{board.me}, {board.start}, {board.uid + 1})")
        seen["db_owner"] = board.as_service("app", "SELECT ticket_board.set_blocked_reason('PGU-901', 'from the db', "
                                            f"{board.me}, {board.start}, {board.uid})")
        seen["db_no_caller"] = board.as_service("", "SELECT ticket_board.set_blocked_reason('PGU-901', 'nobody', "
                                                f"{board.me}, {board.start}, {board.uid})")
        seen["db_listener"] = board.fixture.psql(
            board.admin, "SELECT has_function_privilege('ticket_board_listener', "
            "'ticket_board.set_blocked_reason(text, text, bigint, bigint, bigint)', 'EXECUTE')::text || ' ' || "
            "has_function_privilege('app', 'ticket_board.set_blocked_reason(text, text, bigint, bigint, bigint)', "
            "'EXECUTE')::text;").strip() if board.sql(
                "SELECT to_regprocedure('ticket_board.set_blocked_reason(text, text, bigint, bigint, bigint)') "
                "IS NOT NULL;") == "t" else "absent"
        # The live runtime of a role the workflow no longer lets edit, or no longer lists as the stage's owner.
        call = ("SELECT ticket_board.set_blocked_reason('PGU-901', 'db', "
                f"{board.me}, {board.start}, {board.uid})")
        board.sql("UPDATE ticket_board.workflow_roles SET definition = jsonb_set(definition, '{capabilities}', "
                  "(definition->'capabilities') - 'edit_fields') WHERE name = 'app';")
        seen["no_capability"] = [board.explain("PGU-901", "without edit_fields"), board.as_service("app", call)]
        board.sql("UPDATE ticket_board.workflow_roles SET definition = jsonb_set(definition, '{capabilities}', "
                  "(definition->'capabilities') || '[\"edit_fields\"]') WHERE name = 'app';")
        board.sql("UPDATE ticket_board.workflow_stages SET owner_roles = array_remove(owner_roles, 'app') "
                  "WHERE name = 'in_progress';")
        seen["not_owner"] = [board.explain("PGU-901", "no longer an owner"), board.as_service("app", call)]
        board.sql("UPDATE ticket_board.workflow_stages SET owner_roles = owner_roles || ARRAY['app'] "
                  "WHERE name = 'in_progress';")
        seen["unchanged"] = board.state("PGU-901")
    finally:
        sleeper.kill()
        sleeper.wait()
    # Leaving implementation: deferred, the owner still named on it, then reassigned away.
    seen["deferred"] = board.act("PGU-901", "defer", "director")
    seen["deferred_state"] = board.state("PGU-901")
    seen["after_defer"] = [board.explain("PGU-901", "after it was deferred"), board.state("PGU-901")]
    return seen


def race(board: Board) -> dict:
    """A transition, or a reassignment, at the same moment as the owner's edit: the row lock orders them, and
    whichever commits first, the other sees it."""
    import psycopg

    def waiting(n: int) -> bool:
        for _ in range(200):
            if board.sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = "
                         "current_database();") == str(n):
                return True
            time.sleep(0.05)
        return False

    def director(ticket: str, operation: str, payload: dict, out: dict) -> None:
        request = urllib.request.Request(
            f"{board.base}/api/tickets/{ticket}/actions/{operation}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Ticket-Board-Caller-Role": "director",
                     "X-Ticket-Board-Write-Token": board.server.write_token}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                out["status"] = response.status
        except urllib.error.HTTPError as exc:
            out["status"], out["text"] = exc.code, exc.read().decode()[:300]

    seen: dict = {}
    board.register("main")
    # The Director's defer is queued on the row first, the owner's edit behind it.
    moved, edit = {}, {}
    with psycopg.connect(board.admin) as conn:
        conn.execute("SELECT 1 FROM ticket_board.tickets WHERE id = 'PGU-903' FOR UPDATE")
        first = threading.Thread(target=director, args=("PGU-903", "defer", {}, moved))
        first.start()
        seen["defer_waited"] = waiting(1)
        second = threading.Thread(target=lambda: edit.update(r=board.explain("PGU-903", "too late", role="main")))
        second.start()
        seen["edit_waited"] = waiting(2)
        conn.commit()
    first.join(60)
    second.join(60)
    seen["edit_after_move"] = [moved, edit.get("r"), board.state("PGU-903")]
    # Main owns a ticket again; this time its edit holds the row and the Director's reassignment waits behind it.
    seen["route"] = [board.act("PGU-905", "route", "director", state="in_progress", assignee="main"),
                     board.state("PGU-905")]
    with psycopg.connect(board.service) as conn:
        conn.execute("SELECT set_config('ticket_board.caller_role', 'main', true)")
        try:
            seen["first_edit"] = str(conn.execute(
                "SELECT ticket_board.set_blocked_reason('PGU-905', 'first, then moved', %s, %s, %s)",
                (board.me, board.start, board.uid)).fetchone()[0])
        except psycopg.Error as exc:  # an answer, not a crash
            seen["first_edit"] = f"refused {str(exc).splitlines()[0]}"
            return seen
        moved = {}
        worker = threading.Thread(target=director, args=("PGU-905", "reassign", {"assignee": "app", "reason": "race"},
                                                         moved))
        worker.start()
        seen["move_waited"] = waiting(1)
        conn.commit()
    worker.join(60)
    seen["move_after_edit"] = [moved, board.state("PGU-905"), board.audit("PGU-905")]
    return seen


def director_paths(board: Board) -> dict:
    """The Director's blockers and the ticket editor's blocked_reason edit are as they were."""
    seen = {"set": board.act("PGU-906", "set_blockers", "director", blocked_by=["PGU-903"], blocked_reason="first")}
    seen["after_set"] = board.state("PGU-906")
    seen["editor"] = board.app.update_ticket("PGU-906", {"blocked_reason": "edited in the ticket editor"},
                                             caller_role="director")["blocked_reason"]
    seen["after_editor"] = board.state("PGU-906")
    seen["clear"] = board.act("PGU-906", "set_blockers", "director", blocked_by=[], blocked_reason="")
    seen["after_clear"] = board.state("PGU-906")
    return seen


def legacy(fixture, cluster, root: Path) -> dict:
    """A board that declares no workflow: no role can register a runtime there, so nothing reaches the database."""
    admin = build_board(fixture, cluster, root, "legacy", roles=False)
    board = Board(fixture, cluster, admin, "legacy")
    try:
        fixture.seed_postgres_ticket(admin, "PGU-1", title="legacy work", state="in_progress", assignee="app",
                                     commit_exempt=True)
        return {"declared": board.sql("SELECT ticket_board.declared_workflow() IS NULL;"),
                "http": board.act("PGU-1", "set_blocked_reason", "app", blocked_reason="legacy note"),
                "director": board.act("PGU-1", "set_blocked_reason", "director", blocked_reason="legacy note"),
                "state": board.state("PGU-1")}
    finally:
        board.close()


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
        admin = build_board(fixture, cluster, root, "board")
        board = Board(fixture, cluster, admin, "board")
        try:
            board.app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
            seed(board)
            if board.sql("SELECT to_regprocedure('ticket_board.set_blocked_reason(text, text, bigint, bigint, "
                         "bigint)') IS NULL;") == "t":
                out["main"] = main_routes(board)
                return out
            out["owner"] = owner(board)
            out["other_fields"] = other_fields(board)
            out["standing"] = standing(board)
            out["refusals"] = refusals(board)
            out["race"] = race(board)
            out["director"] = director_paths(board)
            out["acl_before_rbac"] = ACL_BEFORE_RBAC[-1]
        finally:
            board.close()
        out["legacy"] = legacy(fixture, cluster, root)
    return out


def upgrade(before_root: Path, prefix: str) -> dict:
    """Main's board, built by main, then upgraded with this tree's migrations and rbac.sql, as a deploy does."""
    for extra in (str(ROOT), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, before_root, "board")
        fixture.seed_postgres_ticket(admin, "PGU-950", title="before the upgrade", state="in_progress",
                                     assignee="app", commit_exempt=True)
        migrate(fixture, admin, ROOT)
        board = Board(fixture, cluster, admin, "board")
        try:
            board.app.apply_workflow(workflow(ROOT), expected_revision=0, dry_run=False, caller_role="director")
            board.register("app")
            return {"explained": board.explain("PGU-950", "after the upgrade"), "state": board.state("PGU-950"),
                    "acl": board.sql("SELECT proacl::text FROM pg_proc WHERE oid = to_regprocedure("
                                     "'ticket_board.set_blocked_reason(text, text, bigint, bigint, bigint)');")}
        finally:
            board.close()


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def child(flag: str, root: Path, prefix: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), flag, str(root), prefix],
                          capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                          env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def refused(answer: list, status: int, text: str) -> bool:
    return answer[0] == status and text in str(answer[1])


def before(got: dict) -> None:
    m = got["main"]
    check(refused(m["operation"], 403, "app cannot call set_blocked_reason"),
          f"main: the owner has no blocker-explanation operation: {m['operation']}")
    check(refused(m["set_blockers"], 403, "app cannot call set_blockers"),
          f"main: the owner may not use the Director's set_blockers: {m['set_blockers']}")
    check(m["edit_fields"][0] == 400 and "edit_fields cannot update: blocked_reason" in str(m["edit_fields"][1]),
          f"main: edit_fields does not carry blocked_reason: {m['edit_fields']}")
    check(m["after"] == m["before"] and m["after"]["reason"] == "",
          f"main: so the explanation stays whatever the Director last wrote: {m['after']}")


def owner_checks(got: dict) -> None:
    o = got["owner"]
    base = {k: o["before"][k] for k in ("state", "assignee", "blockers", "awaiting", "unresolved")}
    check(o["set"][0] == 200 and o["set"][1] == {"ticket": "PGU-901", "blocked_reason": "waiting on the vendor's API key",
                                                  "changed": True, "runtime_generation": 1},
          f"the owner sets its ticket's explanation, trimmed, from its registered runtime: {o['set']}")
    after = o["after_set"]
    check(after["reason"] == "waiting on the vendor's API key" and after["audit"] == 1
          and o["stored"] == after["reason"],
          f"the text is stored, in the ticket's stored document too, and audited once: {after} {o['stored']!r}")
    check({k: after[k] for k in base} == base and after["blockers"] == [] and after["unresolved"] is False,
          f"saving prose makes no blocker, wait, move or owner change: {after} vs {base}")
    ticket = o["ticket_json"]["ok"]["ticket"]
    check(ticket["blocked_reason"] == "waiting on the vendor's API key" and ticket["blocked_by"] == [],
          f"the ticket the API serves shows the note, and no blocker: {ticket['blocked_reason']} {ticket['blocked_by']}")
    check(o["revise"][0] == 200 and o["revise"][1]["changed"] is True, f"the owner revises it: {o['revise']}")
    check(o["same"][0] == 200 and o["same"][1]["changed"] is False and o["after_same"] == o["after_revise"],
          f"the same text again changes nothing and audits nothing: {o['same']} {o['after_same']}")
    check(after["updated"] != o["before"]["updated"] and o["after_revise"]["updated"] != after["updated"],
          f"each change touches the ticket, so an open board redraws it: {o['before']} {after} {o['after_revise']}")
    check(o["clear"][0] == 200 and o["clear"][1]["blocked_reason"] == "" and o["clear"][1]["changed"] is True,
          f"the owner clears it: {o['clear']}")
    check(o["cli_set"][0] == 0 and (json.loads(o["cli_set"][1] or "{}") or {}).get("changed") is True
          and o["cli_text"] == "from the command, `literally` $HOME",
          f"ticket-board-write set-blocked-reason sets it, literally: {o['cli_set']} {o['cli_text']!r}")
    check(o["cli_clear"][0] == 0 and (json.loads(o["cli_clear"][1] or "{}") or {}).get("blocked_reason") == "",
          f"set-blocked-reason --clear clears it: {o['cli_clear']}")
    for name in ("cli_neither", "cli_both"):
        check(o[name][0] == 2 and "give --blocked-reason with the new text, or --clear" in o[name][2],
              f"set-blocked-reason needs exactly one of the text and --clear ({name}): {o[name]}")
    check(o["after_cli"]["reason"] == "" and o["after_cli"]["audit"] == 5,
          f"five changes, five audit rows; the refused commands wrote nothing: {o['after_cli']}")
    me = o["runtime"][0]
    runtime = f"blocker explanation, from app's registered runtime: generation 1, process {me}"
    check(o["audit"] == [
        ["app", "blocked_reason", "", "waiting on the vendor's API key", runtime],
        ["app", "blocked_reason", "waiting on the vendor's API key",
         "the vendor sent a key; it is for the wrong account", runtime],
        ["app", "blocked_reason", "the vendor sent a key; it is for the wrong account", "", runtime],
        ["app", "blocked_reason", "", "from the command, `literally` $HOME", runtime],
        ["app", "blocked_reason", "from the command, `literally` $HOME", "", runtime]],
          f"each change is one field-audit row: the role, the field, old and new, and the runtime: {o['audit']}")
    check("ok" in o["depend"] and o["wait_set"][0] == 200 and o["wait_after"]["awaiting"] == "director",
          f"an edit leaves the Director wait the owner asked for standing: {o['depend']} {o['wait_after']}")
    check(o["own_wait_set"][0] == 200 and o["own_wait_after"]["awaiting"] == "app",
          f"and a wait recorded on the owner itself: an edit is not the action a wait is for: {o['own_wait_after']}")


def other_field_checks(got: dict) -> None:
    f = got["other_fields"]
    lines = {"blocked_by": "request-dependency", "state": "declared transition", "assignee": "reassign",
             "awaiting_role": "clear-awaiting-role", "needs_audit": "not needs_audit"}
    for field, where in lines.items():
        check(f[field][0] == 403 and f"changes only the blocker explanation, not {field}" in str(f[field][1])
              and where in str(f[field][1]), f"a request that also names {field} is refused, saying how: {f[field]}")
    check(all(refused(f[k], 400, "takes blocked_reason as text") for k in ("not_text", "missing")),
          f"the text must be text: {f['not_text']} {f['missing']}")
    check(f["after"] == f["before"], f"none of them changed anything: {f['after']} vs {f['before']}")
    check(refused(f["set_blockers"], 403, "app cannot call set_blockers")
          and f["edit_fields"][0] == 400 and "cannot update: blocked_reason" in str(f["edit_fields"][1]),
          f"the owner still cannot use set_blockers or edit_fields for it: {f['set_blockers']} {f['edit_fields']}")
    check("refused" in f["http_port"] and "403" in f["http_port"]["refused"]
          and "from the owning role's own pane" in f["http_port"]["refused"],
          f"the HTTP port, with the write token, is no pane: {f['http_port']}")
    check(f["final"] == f["before"], f"and nothing changed: {f['final']}")


def standing_checks(got: dict) -> None:
    s = got["standing"]
    check("ok" in s["blocked"], f"the Director sets two blockers: {s['blocked']}")
    check(s["revise"][0] == 200 and s["after_revise"]["reason"] == "PGU-904 is half done; Eric has the key ready"
          and s["after_revise"]["blockers"] == ["PGU-904", "operator:eric"] and s["after_revise"]["unresolved"],
          f"the owner revises a standing blocker's explanation, and its links stay: {s['after_revise']}")
    check(s["clear"][0] == 400 and "PGU-902 waits on PGU-904, operator:eric: a standing blocker's explanation can be "
          "revised, not removed" in str(s["clear"][1]),
          f"clearing it while they stand is refused, naming them: {s['clear']}")
    check(s["after_clear"] == s["after_revise"], f"and changes nothing: {s['after_clear']}")
    check("ok" in s["release"] and s["revise_again"][0] == 200 and "ok" in s["cancel"],
          f"the Director releases one and cancels the other's ticket: {s['release']} {s['cancel']}")
    check(s["clear_resolved"][0] == 200 and s["after_resolved"]["reason"] == ""
          and s["after_resolved"]["blockers"] == ["PGU-904 resolved"] and s["after_resolved"]["unresolved"] is False,
          f"once none stands the owner clears the text, and the resolved link stays: {s['after_resolved']}")


def refusal_checks(got: dict) -> None:
    r = got["refusals"]
    owns = "may explain the blocker only of a ticket it owns in implementation"
    for name, ticket, text in (("others_ticket", "PGU-902", "app " + owns + "; PGU-902 is in_progress/ops"),
                               ("director_ticket", "PGU-905", "app " + owns + "; PGU-905 is analysis/director"),
                               ("director", "PGU-901", "director " + owns),
                               ("reviewer", "PGU-901", "audit " + owns)):
        answer, state = r[name]
        check(refused(answer, 400, text), f"refused ({name}): {answer}")
    check(r["others_ticket"][1]["reason"] == "" and r["director_ticket"][1]["reason"] == "",
          f"neither ticket was touched: {r['others_ticket'][1]} {r['director_ticket'][1]}")
    check(refused(r["own_triage"][0], 400, "director " + owns + "; PGU-905 is analysis/director")
          and r["own_triage"][1]["reason"] == "",
          f"an owner and assignee outside implementation is refused: {r['own_triage']}")
    check(r["no_capability"][0][0] != 200 and "role app cannot call edit_fields" in r["no_capability"][1],
          f"a role the workflow no longer lets edit is refused, by the database too: {r['no_capability']}")
    check(r["not_owner"][0][0] != 200 and owns in r["not_owner"][1],
          f"a role no longer an owner of the stage is refused, by the database too: {r['not_owner']}")
    check(r["unchanged"]["reason"] == "from the db", f"and neither changed it: {r['unchanged']}")
    check(refused(r["replaced"][0], 403, "no PostgreSQL role assignment"),
          f"a replaced runtime's pane is no role at all to the server: {r['replaced']}")
    check("refused" in r["db_replaced"] and "is not app's registered live runtime" in r["db_replaced"],
          f"and to the database alone it is not app's live runtime: {r['db_replaced']}")
    check(refused(r["retired"][0], 403, "no PostgreSQL role assignment")
          and "is not app's registered live runtime" in r["db_retired"],
          f"a retired runtime likewise, server and database: {r['retired']} {r['db_retired']}")
    check(r["replaced"][1]["reason"] == r["retired"][1]["reason"] and r["retired"][1]["audit"] == r["replaced"][1]["audit"],
          f"neither wrote anything: {r['replaced'][1]} {r['retired'][1]}")
    check(refused(r["stale"][0], 403, "no PostgreSQL role assignment") and "registered live runtime" in r["db_stale"]
          and r["stale"][1]["audit"] == r["retired"][1]["audit"],
          f"a registration for a runtime the role no longer declares is stale, to both: {r['stale']} {r['db_stale']}")
    check(r["generation_2"][0] == 200 and r["generation_2"][1]["runtime_generation"] == 2
          and r["audit_2"][4].startswith("blocker explanation, from app's registered runtime: generation 2, process "),
          f"the new runtime's edit is attributed to it, generation 2: {r['generation_2']} {r['audit_2']}")
    check("refused" in r["db_others"] and owns in r["db_others"], f"the database alone, another's ticket: {r['db_others']}")
    for name in ("db_other_process", "db_other_start", "db_other_uid"):
        check("refused" in r[name] and "registered live runtime" in r[name],
              f"the database alone, another process ({name}: its pid, start time or uid): {r[name]}")
    check(r["db_owner"].startswith("ok ") and "'changed': True" in r["db_owner"],
          f"the database alone admits the owner's live runtime: {r['db_owner']}")
    check("refused" in r["db_no_caller"], f"the database alone, with no caller: {r['db_no_caller']}")
    check(r["db_listener"] == "false false", f"only the board's writer may call it: {r['db_listener']}")
    check("ok" in r["deferred"] and r["deferred_state"]["state"] == "backlog", f"deferred: {r['deferred_state']}")
    check(refused(r["after_defer"][0], 400, owns) and r["after_defer"][1]["reason"] == r["deferred_state"]["reason"],
          f"a ticket that left implementation is refused, whoever it names: {r['after_defer']}")


def race_checks(got: dict) -> None:
    w = got["race"]
    moved, edit, state = w["edit_after_move"]
    check(w["defer_waited"] and w["edit_waited"] and moved.get("status") == 200 and state["state"] == "backlog"
          and refused(edit, 400, "main may explain the blocker only") and state["reason"] == "",
          f"the defer committed first; the waiting edit then found the ticket gone from implementation, and wrote "
          f"nothing: {w['defer_waited']} {w['edit_waited']} {moved} {edit} {state}")
    check("ok" in w["route"][0] and w["route"][1]["assignee"] == "main", f"main owns PGU-905: {w['route']}")
    check("'changed': True" in w["first_edit"], f"main's edit, held open: {w['first_edit']}")
    moved, state, audit = w["move_after_edit"]
    check(w["move_waited"] and moved.get("status") == 200 and state["assignee"] == "app"
          and state["reason"] == "first, then moved" and [a[0] for a in audit] == ["main"],
          f"the edit committed first; the waiting reassignment landed after it: {w['move_waited']} {moved} {state} "
          f"{audit}")


def director_checks(got: dict) -> None:
    d = got["director"]
    check("ok" in d["set"] and d["after_set"]["blockers"] == ["PGU-903"] and d["after_set"]["reason"] == "first",
          f"the Director's set_blockers is unchanged: {d['set']} {d['after_set']}")
    check(d["editor"] == "edited in the ticket editor" and d["after_editor"]["blockers"] == ["PGU-903"],
          f"and the ticket editor's blocked_reason edit: {d['editor']} {d['after_editor']}")
    check("ok" in d["clear"] and d["after_clear"]["reason"] == "" and d["after_clear"]["blockers"] == [],
          f"and clearing both: {d['after_clear']}")
    check(d["after_clear"]["audit"] == 0, f"the Director's own path is audited as before, not here: {d['after_clear']}")


def legacy_checks(got: dict) -> None:
    g = got["legacy"]
    check(g["declared"] == "t" and "403" in str(g["http"]) and "a blocker explanation is set only from the owning role" in str(g["http"]),
          f"a board that declares no workflow admits an implementer to the operation only to say where it works: "
          f"{g['http']}")
    check("403" in str(g["director"]) and "director cannot call set_blocked_reason" in str(g["director"])
          and g["state"]["reason"] == "", f"and not the Director at all: {g['director']} {g['state']}")


def upgraded(got: dict) -> None:
    check(got["explained"][0] == 200 and got["state"]["reason"] == "after the upgrade",
          f"main's board, upgraded by this tree's migrations, has the operation: {got}")
    check({entry.split("=")[0] for entry in got["acl"].strip("{}").split(",")} == {"postgres", "ticket_board_service"},
          f"executable by its owner and the board's writer alone, not PUBLIC: {got['acl']}")


def mirrored() -> None:
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    body = MIGRATION.read_text().split("BEGIN;\n", 1)[1].rsplit("COMMIT;", 1)[0].strip("\n")
    check(schema.rfind(body) != -1 and schema.rfind(body) == schema.rfind(body.split("\n", 1)[0]),
          "schema.sql's last copy of the function is the migration's, byte for byte")


def candidate(got: dict) -> None:
    owner_checks(got)
    other_field_checks(got)
    standing_checks(got)
    refusal_checks(got)
    race_checks(got)
    director_checks(got)
    legacy_checks(got)
    acl = got["acl_before_rbac"]
    check(acl != "default" and not any(entry.startswith("=") for entry in acl.strip("{}").split(",")),
          f"PUBLIC cannot execute it even before rbac.sql runs, in a deploy's window: {acl}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] in ("--scenario", "--upgrade"):
        run = scenario if sys.argv[1] == "--scenario" else upgrade
        print(json.dumps(run(Path(sys.argv[2]), sys.argv[3])))
        return 0
    mirrored()
    if os.environ.get("SYRD723_MUTATION_SKIP_BEFORE") == "1":  # a mutant changes this tree only
        candidate(child("--scenario", ROOT, "s723a"))
        print(f"blocker_explanation_test: {CHECKS} checks ok (mutation run: main's replay and the upgrade skipped)")
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd723-before.", dir="/tmp") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        before(child("--scenario", before_root, "s723b"))
        upgraded(child("--upgrade", before_root, "s723u"))
    candidate(child("--scenario", ROOT, "s723a"))
    print(f"blocker_explanation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
