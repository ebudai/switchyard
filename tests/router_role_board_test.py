#!/usr/bin/env python3
"""SYRD-599: Router chooses an Implementation owner, or hands the ticket back, and nothing else.

Production-built boards (schema.sql, companion roles, rbac.sql, the real
ticket-board-migrate, rbac.sql again), the real HTTP server and the real notify
listener with an injected sender:

* a Router board, from the shipped example workflow with `router_setup.with_router`;
* the same example without Router, which must behave exactly as before;
* main's board as it shipped before pgu997, upgraded with this tree's
  migrations, so the migration's copies are what run.

Every refusal is asked twice, independently: over HTTP as the Router pane would,
and in PostgreSQL as the service with caller role `router`, so neither boundary
is proved by the other.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402
import ticket_board_write_api_test as fixture  # noqa: E402
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.router_setup import with_router, without_router  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu997_syrd599_router_role.sql"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def example(router: bool) -> dict:
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return with_router(cfg, runtime="claude") if router else cfg


def migrate(admin: str, source: Path = ROOT) -> None:
    """An upgrade as production deploys one: that tree's every pending migration, then its rbac.sql."""
    result = subprocess.run(["bash", str(source / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    fixture.psql(admin, (source / "scripts/ticket_board/rbac.sql").read_text())


def create_roles_once(admin: str) -> None:
    """Pane roles are the cluster's, not one database's: the second board finds them made."""
    if fixture.psql(admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
        fixture.create_roles(admin)


def before_tree(into: Path) -> Path:
    """The tree as it stood before pgu997 joined it: that release's schema, migrations and grants."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).split()
    assert adding, f"{MIGRATION.name} is not committed; clone-based checks see only commits"
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^", "scripts"], check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    assert not (into / "scripts/ticket_board/migrations" / MIGRATION.name).exists()
    return into


def build_board(cluster, dbname: str, *, before: Path | None = None) -> str:
    """A board as production builds one, from this tree or from `before`'s."""
    source = before or ROOT
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, (source / "scripts/ticket_board/schema.sql").read_text())
    create_roles_once(admin)
    fixture.psql(admin, (source / "scripts/ticket_board/rbac.sql").read_text())
    migrate(admin, source)
    return admin


class Board:
    """One board: its HTTP server, its listener, and the questions the checks ask of it."""

    def __init__(self, cluster, dbname: str, document: dict, *, before: Path | None = None):
        self.admin = build_board(cluster, dbname, before=before)
        if before:
            migrate(self.admin)  # the upgrade: this tree's migrations, then rbac.sql
        self.service = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE)
        self.app = fixture.TicketBoardApp(cluster.root / f"{dbname}-frames", cluster.root / f"{dbname}-assets",
                                          project="pgu", ticket_prefix="PGU", database_url=self.service)
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=fixture.QuietNotifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")
        # Every pane the listener delivers to, registered as a running process the
        # way a pane registers: Router's detached session like any other.
        from scripts.ticket_board.peer_identity import read_process
        self.sleepers = []
        for role in document["roles"]:
            if role.get("active") and role.get("runtime") and role.get("target"):
                sleeper = subprocess.Popen(["sleep", "3600"])
                self.sleepers.append(sleeper)
                self.sql("INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                         f"session_dir, process_pid, process_start_time, process_uid) VALUES ('{role['name']}', "
                         f"'{role['runtime']}', '{role['target']}', '/w/{role['name']}', '/s/{role['name']}', "
                         f"{sleeper.pid}, {read_process(sleeper.pid).start_time}, {os.getuid()});")
        self.sent: list[tuple[str, str]] = []
        self.busy: set[str] = set()
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")

        def deliver(target: str, message: str) -> None:
            self.sent.append((target, message))
            self.busy.add(target)  # a pane handed a notice is working on it until its turn ends

        self.listener = TicketBoardNotifyListener(
            conninfo=self.listener_url, sender=deliver, activity_gate=lambda target: target in self.busy,
            target_exists=lambda _target: True,
            submission_witness=lambda target, _since: any(t == target for t, _m in self.sent),
            poll_seconds=0, project="pgu")

    def act(self, ticket: str, operation: str, actor: str, **payload) -> dict:
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        try:
            return {"ok": fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)}
        except AssertionError as exc:  # a refusal is an answer
            return {"refused": str(exc)}

    def sql(self, statement: str, url: str | None = None) -> str:
        return fixture.psql(url or self.admin, statement).strip()

    def as_router_sql(self, statement: str) -> dict:
        """The statement as the board service runs it for a `router` caller: the database's own answer."""
        return self.as_role_sql("router", statement)

    def as_role_sql(self, role: str, statement: str) -> dict:
        try:
            with psycopg.connect(self.service, autocommit=False) as conn:
                conn.execute("SELECT set_config('ticket_board.caller_role', %s, true)", (role,))
                conn.execute(statement)
                conn.commit()
            return {"ok": True}
        except Exception as exc:
            return {"refused": str(exc).splitlines()[0]}

    def where(self, ticket: str) -> list[str]:
        return self.sql(f"SELECT state || '|' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';").split("|")

    def snapshot(self, ticket: str) -> str:
        """Everything about a ticket a refused write must leave alone."""
        return self.sql(
            "SELECT ((to_jsonb(t) - 'row_updated_at' - 'source_json') || jsonb_build_object("
            "'blockers', (SELECT coalesce(jsonb_agg(b ORDER BY b.position), '[]') FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id),"
            "'comments', (SELECT count(*) FROM ticket_board.ticket_comments c WHERE c.ticket_id = t.id),"
            "'decisions', (SELECT count(*) FROM ticket_board.router_decisions d WHERE d.ticket_id = t.id)))::text "
            f"FROM ticket_board.tickets t WHERE t.id = '{ticket}';")

    def ticket(self, ticket: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/tickets/{ticket}") as response:
            return json.loads(response.read())

    def revision(self, ticket: str) -> str:
        return str((self.ticket(ticket).get("routing") or {}).get("revision") or "")

    def reservations(self) -> dict:
        return json.loads(self.sql("SELECT coalesce(json_object_agg(implementer, ticket_id), '{}')::text "
                                   "FROM ticket_board.serial_reservations();"))

    def serial_queue_roles(self) -> list[str]:
        return json.loads(self.sql("SELECT coalesce(json_agg(DISTINCT implementer), '[]')::text FROM ticket_board.serial_queue();"))

    def one_pass(self) -> list[tuple[str, str]]:
        before = len(self.sent)
        with psycopg.connect(self.listener_url, autocommit=True) as conn:
            self.listener.refresh_workflow(conn)
            self.listener.process_due_notifications(conn)
        return self.sent[before:]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        for sleeper in self.sleepers:
            sleeper.kill()
            sleeper.wait()


def to(batch: list[tuple[str, str]], role: str) -> list[str]:
    return [m for t, m in batch if t == f"pgu-{role}:0.0"]


def refused(answer: dict, *words: str) -> bool:
    text = answer.get("refused", "")
    return "refused" in answer and all(w in text for w in words)


def router_board(board: Board, seen: dict) -> None:
    T = [f"PGU-{700 + n}" for n in range(1, 20)]
    for t in T:
        fixture.seed_postgres_ticket(board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director",
                                     commit_exempt=True)
    board.one_pass()
    board.sent.clear()
    board.busy.clear()

    # 1. The Director admits six tickets to Implementation the ordinary way: naming no owner, which on a
    # Router board means Router (one names `router` itself, which is the same request).
    seen["routes"] = {t: board.act(t, "route", "director", state="in_progress") for t in T[:5]}
    seen["routes"][T[5]] = board.act(T[5], "route", "director", state="in_progress", assignee="router")
    seen["placed"] = {t: board.where(t) for t in T[:6]}
    seen["router_reserved"] = [board.reservations().get("router"), "router" in board.serial_queue_roles(),
                               [board.sql(f"SELECT ticket_board.ticket_serial_waiting('{t}');") for t in T[:6]],
                               board.sql("SELECT count(*) FROM ticket_board.serial_focus WHERE implementer = 'router';"),
                               [board.sql("SELECT coalesce(last_implementer_assignee, '') FROM "
                                          f"ticket_board.ticket_notification_state WHERE ticket_id = '{t}';") for t in T[:6]]]
    # Delivered one at a time: a pane handed a notice is busy until its turn ends.
    passes = []
    for _ in range(7):
        passes.append(to(board.one_pass(), "router"))
        board.busy.clear()  # router's turn ended; what the busy pane deferred is due again
        board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                  "WHERE target_role = 'router' AND dead_lettered_at IS NULL;")
    seen["router_deliveries"] = passes

    # 2. One read gives the revision and the owners Router may choose.
    first = board.ticket(T[0])
    seen["routing_field"] = first.get("routing")
    seen["routing_actions"] = [a["action"] for a in first.get("workflow_actions", []) if "router" in a["actors"]]
    revisions = {t: board.revision(t) for t in T[:6]}

    # Clear examples to each owner; main is then busy, so its second waits in its queue.
    before_main = board.snapshot(T[0])
    seen["assign_main"] = board.act(T[0], "router_assign", "router", assignee="main", revision=revisions[T[0]])
    seen["assign_main_changed"] = sorted(k for k, v in json.loads(board.snapshot(T[0])).items()
                                         if json.loads(before_main).get(k) != v)
    seen["assign_app"] = board.act(T[1], "router_assign", "router", assignee="app", revision=revisions[T[1]])
    seen["assign_ops"] = board.act(T[2], "router_assign", "router", assignee="ops", revision=revisions[T[2]])
    seen["assign_main_second"] = board.act(T[3], "router_assign", "router", assignee="main", revision=revisions[T[3]])
    seen["after_assign"] = {t: board.where(t) for t in T[:4]}
    seen["reservations"] = board.reservations()
    seen["main_second_waiting"] = board.sql(f"SELECT ticket_board.ticket_serial_waiting('{T[3]}');")
    # Not merely undelivered: nothing is queued for it, as reassign queues nothing for waiting work.
    seen["main_second_queued"] = board.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                           f"WHERE ticket_id = '{T[3]}' AND target_role = 'main';")
    batch = board.one_pass()
    seen["owner_notices"] = {r: to(batch, r) for r in ("main", "app", "ops")}
    seen["decisions"] = board.sql("SELECT json_agg(json_build_array(ticket_id, router_role, decision, assignee, "
                                  "classified_revision) ORDER BY decision_id)::text FROM ticket_board.router_decisions;")
    seen["revisions"] = revisions

    # 3. An ambiguous ticket goes back to the Director, with its reason, once.
    board.busy.clear()
    seen["return"] = board.act(T[4], "router_return", "router", reason="Spans app and ops; needs the Director.",
                               revision=revisions[T[4]])
    seen["returned"] = board.where(T[4])
    seen["return_comment"] = board.sql("SELECT who || '|' || text FROM ticket_board.ticket_comments "
                                       f"WHERE ticket_id = '{T[4]}' ORDER BY id DESC LIMIT 1;")
    seen["director_handoff"] = to(board.one_pass(), "director")

    # 4/5. Everything else Router might try, over HTTP and in PostgreSQL.
    held = T[5]
    rev = board.revision(held)
    snap = board.snapshot(held)
    http = {
        "route": board.act(held, "route", "router", state="analysis", assignee="director"),
        "reassign": board.act(held, "reassign", "router", assignee="main", reason="x"),
        "set_blockers": board.act(held, "set_blockers", "router", blocked_by=[T[0]], blocked_reason="x"),
        "edit_parent": board.act(held, "edit_fields", "router", parent_id=T[0]),
        "edit_body": board.act(held, "edit_fields", "router", body="rewritten"),
        "flags": board.act(held, "set_workflow_flags", "router", needs_audit=False),
        "comment": board.act(held, "add_comment", "router", text="I think main"),
        "force_move": board.act(held, "force_move", "router", state="in_progress", assignee="main"),
        "override_move": board.act(held, "override_move", "router", state="analysis", assignee="director"),
        "director_edit": board.act(held, "director_edit", "router", set={"needs_audit": False}, reason="x"),
        "hold": board.act(held, "set_manually_controlled", "router", manually_controlled=True),
        "kick_back": board.act(held, "implementer_kick_back", "router", reason="x"),
        "submit": board.act(held, "submit_to_audit_without_commit", "router", reason="x"),
        "assign_director": board.act(held, "router_assign", "router", assignee="director", revision=rev),
        "assign_audit": board.act(held, "router_assign", "router", assignee="audit", revision=rev),
        "assign_user": board.act(held, "router_assign", "router", assignee="user", revision=rev),
        "assign_unassigned": board.act(held, "router_assign", "router", assignee="unassigned", revision=rev),
        "assign_router": board.act(held, "router_assign", "router", assignee="router", revision=rev),
        "assign_inactive": board.act(held, "router_assign", "router", assignee="perf", revision=rev),
        "assign_made_up": board.act(held, "router_assign", "router", assignee="wizard", revision=rev),
        "assign_extra_field": board.act(held, "router_assign", "router", assignee="main", revision=rev, state="audit"),
        "assign_parent_payload": board.act(held, "router_assign", "router", assignee="main", revision=rev, parent_id=T[0]),
        "assign_stale": board.act(held, "router_assign", "router", assignee="main", revision="0" * 32),
        "return_empty": board.act(held, "router_return", "router", reason="  ", revision=rev),
        "return_with_assignee": board.act(held, "router_return", "router", reason="x", revision=rev, assignee="main"),
        "director_assigns": board.act(held, "router_assign", "director", assignee="main", revision=rev),
        "main_returns": board.act(held, "router_return", "main", reason="x", revision=rev),
        "not_routers": board.act(T[0], "router_assign", "router", assignee="app", revision=revisions[T[0]]),
        "duplicate": board.act(T[1], "router_assign", "router", assignee="app", revision=revisions[T[1]]),
        "in_triage": board.act(T[6], "router_assign", "router", assignee="main", revision=rev),
    }
    seen["http"] = http
    q = lambda s: s.replace("'", "''")  # noqa: E731
    pg = {
        "reassign": board.as_router_sql(f"SELECT ticket_board.reassign('{held}', 'main', 'x');"),
        "route": board.as_router_sql(f"SELECT ticket_board.route('{held}', 'analysis', 'director');"),
        "set_blockers": board.as_router_sql(f"SELECT ticket_board.set_blockers('{held}', ARRAY['{T[0]}'], 'x');"),
        "edit_fields": board.as_router_sql(f"SELECT ticket_board.edit_fields('{held}', '{q(json.dumps({'parent_id': T[0]}))}'::jsonb);"),
        "add_comment": board.as_router_sql(f"SELECT ticket_board.add_comment('{held}', 'I think main');"),
        "workflow_return": board.as_router_sql(f"SELECT ticket_board.perform_workflow_action('{held}', 'router_return', "
                                               "'{\"reason\": \"x\"}'::jsonb);"),
        "update": board.as_router_sql(f"UPDATE ticket_board.tickets SET assignee = 'main' WHERE id = '{held}';"),
        "assign_reviewer": board.as_router_sql(f"SELECT ticket_board.router_assign('{held}', 'audit', '{rev}');"),
        "assign_stale": board.as_router_sql(f"SELECT ticket_board.router_assign('{held}', 'main', '{'1' * 32}');"),
        "assign_not_routers": board.as_router_sql(f"SELECT ticket_board.router_assign('{T[0]}', 'app', '{revisions[T[0]]}');"),
        "return_empty": board.as_router_sql(f"SELECT ticket_board.router_return('{held}', ' ', '{rev}');"),
    }
    # The window itself, opened by hand by a caller the grants would stop first:
    # as postgres, which skips grants but not triggers, it still admits only its change.
    window = ("SELECT set_config('ticket_board.caller_role', 'router', false); "
              f"SELECT set_config('ticket_board.router_ticket', '{held}', false); "
              "SELECT set_config('ticket_board.router_action', 'assign', false); ")
    for name, change in (("window_parent", f"parent_id = '{T[0]}'"), ("window_flag", "needs_audit = false"),
                         ("window_stage", "state = 'analysis', assignee = 'director'"),
                         ("window_reviewer", "assignee = 'audit'"), ("window_body", "body = 'x'")):
        try:
            board.sql(window + f"UPDATE ticket_board.tickets SET {change} WHERE id = '{held}';")
            pg[name] = {"ok": True}
        except Exception as exc:
            pg[name] = {"refused": next((line for line in str(exc).splitlines() if line.startswith("ERROR:")), str(exc)[-300:])}
    # And with no window at all: Router is not the Director, so the trigger refuses an in-place owner change.
    try:
        board.sql("SELECT set_config('ticket_board.caller_role', 'router', false); "
                  f"UPDATE ticket_board.tickets SET assignee = 'main' WHERE id = '{held}';")
        pg["no_window"] = {"ok": True}
    except Exception as exc:
        pg["no_window"] = {"refused": next((line for line in str(exc).splitlines() if line.startswith("ERROR:")), str(exc)[-300:])}
    seen["pg"] = pg
    seen["held_unchanged"] = board.snapshot(held) == snap
    seen["held_where"] = board.where(held)
    seen["director_comment_for_router"] = board.act(held, "add_comment", "director", text="Director note")

    # 6. Stale after a Director edit; then the fresh revision works, once.
    old = board.revision(held)
    board.act(held, "edit_fields", "director", body="The Director clarified: this is ops work.")
    seen["stale_after_edit"] = board.act(held, "router_assign", "router", assignee="ops", revision=old)
    fresh = board.revision(held)
    seen["fresh_changed"] = fresh != old
    seen["fresh_assign"] = board.act(held, "router_assign", "router", assignee="ops", revision=fresh)
    seen["fresh_again"] = board.act(held, "router_assign", "router", assignee="ops", revision=fresh)
    seen["fresh_where"] = board.where(held)

    # A blocked ticket goes back all the same: handing work back promotes nothing.
    blocked, held, again, kept = T[9], T[10], T[11], T[12]
    for ticket in (blocked, held, again, kept):
        board.act(ticket, "route", "director", state="in_progress")
    board.act(blocked, "set_blockers", "director", blocked_by=[T[6]], blocked_reason="after the triage one")
    seen["blocked_return"] = board.act(blocked, "router_return", "router", reason="Blocked; the Director decides when.",
                                       revision=board.revision(blocked))
    seen["blocked_where"] = board.where(blocked)
    # A ticket the Director holds is not Router's to move.
    board.act(held, "set_manually_controlled", "director", manually_controlled=True)
    seen["held_assign"] = board.act(held, "router_assign", "router", assignee="ops", revision=board.revision(held))
    seen["held_assign_pg"] = board.as_router_sql(f"SELECT ticket_board.router_assign('{held}', 'ops', "
                                                 f"'{board.revision(held)}');")
    seen["held_where"] = board.where(held)
    # A new round with the same content is a new revision: the Director hands Router's pick back to it.
    first_round = board.revision(again)
    board.act(again, "router_assign", "router", assignee="main", revision=first_round)
    seen["back_to_router"] = board.act(again, "reassign", "director", assignee="router", reason="Route it again.")
    seen["old_round"] = board.act(again, "router_assign", "router", assignee="app", revision=first_round)
    seen["new_round"] = board.act(again, "router_assign", "router", assignee="app", revision=board.revision(again))
    seen["again_where"] = board.where(again)
    # The workflow can still change while Router holds work; only removing Router cannot.
    document = json.loads(board.sql("SELECT document::text FROM ticket_board.workflow_configuration;"))
    revision = int(board.sql("SELECT revision FROM ticket_board.workflow_configuration;"))
    next(r for r in document["roles"] if r["name"] == "router")["label"] = "Router (cheap)"
    try:
        board.app.apply_workflow(document, expected_revision=revision, dry_run=False, caller_role="director")
        seen["reapply_while_holding"] = [board.where(kept), board.reservations().get("router")]
    except Exception as exc:
        seen["reapply_while_holding"] = str(exc)[:300]
    board.act(kept, "router_return", "router", reason="Clearing up.", revision=board.revision(kept))
    board.act(held, "set_manually_controlled", "director", manually_controlled=False)
    board.act(held, "router_return", "router", reason="Clearing up.", revision=board.revision(held))

    # What Router reads includes the comments, where an ownership decision is often written: a Director
    # comment after Router's read makes that read stale, at both boundaries (Audit, 2026-10-17).
    commented, attached = T[16], T[17]
    for ticket in (commented, attached):
        board.act(ticket, "route", "director", state="in_progress")
    read_before = board.revision(commented)
    board.act(commented, "add_comment", "director", text="Routing correction: this belongs to ops, not main.")
    seen["comment_changed_revision"] = board.revision(commented) != read_before
    seen["comment_stale_http"] = board.act(commented, "router_assign", "router", assignee="main", revision=read_before)
    seen["comment_stale_pg"] = board.as_router_sql(f"SELECT ticket_board.router_assign('{commented}', 'main', '{read_before}');")
    seen["comment_where"] = board.where(commented)
    seen["comment_fresh"] = board.act(commented, "router_assign", "router", assignee="ops",
                                      revision=board.revision(commented))
    seen["comment_after"] = board.where(commented)
    # And an attachment added after the read, the other thing the read shows that is not a ticket column.
    read_before = board.revision(attached)
    board.sql("INSERT INTO ticket_board.ticket_attachments (ticket_id, position, path, source_field) "
              f"VALUES ('{attached}', 0, '/assets/ownership.png', 'screenshots');")
    seen["attachment_stale"] = board.act(attached, "router_assign", "router", assignee="main", revision=read_before)
    board.act(attached, "router_return", "router", reason="Clearing up.", revision=board.revision(attached))

    # A round trip that leaves nothing visible changed -- deferred and routed back, no comment on the
    # way -- is still a new round, so a decision from before it is stale.
    bounced = T[18]
    board.act(bounced, "route", "director", state="in_progress")
    read_before = board.revision(bounced)
    comments_before = board.sql(f"SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = '{bounced}';")
    seen["bounce_moves"] = [board.act(bounced, "defer", "director", state="backlog"),
                            board.act(bounced, "route", "director", state="analysis"),
                            board.act(bounced, "route", "director", state="in_progress")]
    seen["bounce_silent"] = board.sql(f"SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = '{bounced}';") == comments_before
    seen["bounce_where"] = board.where(bounced)
    seen["bounce_stale"] = board.act(bounced, "router_assign", "router", assignee="main", revision=read_before)
    board.act(bounced, "router_return", "router", reason="Clearing up.", revision=board.revision(bounced))

    # The Director's direct route stays first-class: named owner, ordinary rules, no Router decision.
    direct, by_sql, stranded = T[13], T[14], T[15]
    seen["direct"] = board.act(direct, "route", "director", state="in_progress", assignee="ops")
    seen["direct_where"] = board.where(direct)
    seen["direct_decisions"] = board.sql(f"SELECT count(*) FROM ticket_board.router_decisions WHERE ticket_id = '{direct}';")
    seen["direct_routing"] = board.ticket(direct).get("routing")
    # The default is the database's, not the client's: the route as the board service runs it, no owner named.
    seen["default_pg"] = board.as_role_sql("director", f"SELECT ticket_board.perform_workflow_action('{by_sql}', 'route', "
                                                       "'{\"target\": \"in_progress\"}'::jsonb);")
    seen["default_pg_where"] = board.where(by_sql)
    board.act(by_sql, "router_return", "router", reason="Clearing up.", revision=board.revision(by_sql))
    # Router not running: the default refuses; the direct route still works; nothing falls back.
    registration = board.sql("SELECT row_to_json(a)::text FROM ticket_board.role_runtime_assignments a WHERE role = 'router';")
    board.sql("DELETE FROM ticket_board.role_runtime_assignments WHERE role = 'router';")
    seen["unavailable_default"] = board.act(stranded, "route", "director", state="in_progress")
    seen["unavailable_named_router"] = board.act(stranded, "route", "director", state="in_progress", assignee="router")
    seen["unavailable_where"] = board.where(stranded)
    seen["unavailable_direct"] = board.act(stranded, "route", "director", state="in_progress", assignee="app")
    seen["unavailable_direct_where"] = board.where(stranded)
    board.sql("INSERT INTO ticket_board.role_runtime_assignments SELECT * FROM json_populate_record("
              f"NULL::ticket_board.role_runtime_assignments, '{registration}'::json);")

    # The implementer's escape: main hands back router's pick; the Director corrects another.
    seen["kick_back"] = board.act(T[0], "implementer_kick_back", "main", reason="This is app's renderer, not main's.")
    seen["kicked"] = board.where(T[0])
    seen["correction"] = board.act(T[1], "reassign", "director", assignee="ops", reason="App is on leave.")
    seen["outcomes"] = board.sql(
        "SELECT json_agg(json_build_array(ticket_id, outcome, outcome_role, outcome_reason) ORDER BY decision_id)::text "
        "FROM ticket_board.router_decisions WHERE outcome IS NOT NULL;")
    seen["history"] = board.ticket(T[0]).get("routing")

    # Turning Router off while it holds work is refused; once it holds none, it is allowed.
    board.act(T[7], "route", "director", state="in_progress", assignee="router")
    revision = int(board.sql("SELECT revision FROM ticket_board.workflow_configuration;"))
    document = json.loads(board.sql("SELECT document::text FROM ticket_board.workflow_configuration;"))
    try:
        board.app.apply_workflow(without_router(document), expected_revision=revision, dry_run=False, caller_role="director")
        seen["disable_while_holding"] = "applied"
    except Exception as exc:
        seen["disable_while_holding"] = str(exc)[:300]
    board.act(T[7], "router_return", "router", reason="Turning Router off.", revision=board.revision(T[7]))
    try:
        board.app.apply_workflow(without_router(document), expected_revision=revision, dry_run=False, caller_role="director")
        seen["disable_empty"] = "applied"
    except Exception as exc:
        seen["disable_empty"] = str(exc)[:300]
    board.act(T[8], "route", "director", state="in_progress", assignee="router")
    # Not Router's any more: the board's existing rule for a non-owner assignee
    # (the stage's first owner) applies, exactly as it does to any other role.
    seen["route_after_disable"] = [board.where(T[8]), board.reservations().get("router")]


def variant_board(board: Board, seen: dict, name: str) -> None:
    """Router on a pull-scheduled board, and on one whose Implementation has a single owner."""
    tickets = [f"PGU-{850 + n}" for n in range(1, 4)]
    for ticket in tickets:
        fixture.seed_postgres_ticket(board.admin, ticket, title=ticket, state="analysis", assignee="director",
                                     commit_exempt=True)
    seen[f"{name}_routes"] = {t: board.act(t, "route", "director", state="in_progress", assignee="router") for t in tickets}
    seen[f"{name}_where"] = {t: board.where(t) for t in tickets}
    seen[f"{name}_queued"] = board.sql("SELECT coalesce(string_agg(id || ':' || queued_for_assignee, ',' ORDER BY id), '') "
                                       "FROM ticket_board.tickets WHERE queued_for_assignee <> '';")
    seen[f"{name}_assign"] = board.act(tickets[0], "router_assign", "router", assignee="main",
                                       revision=board.revision(tickets[0]))
    seen[f"{name}_assigned"] = board.where(tickets[0])
    if name == "single":
        handed = "PGU-861"
        fixture.seed_postgres_ticket(board.admin, handed, title=handed, state="analysis", assignee="director",
                                     commit_exempt=True)
        seen["single_hand_over"] = board.act(handed, "hand_over", "audit", state="in_progress")
        seen["single_handed"] = board.where(handed)
    if name == "pull":
        # Only the Director's route is Router's by default: an implementer pulling admitted work keeps it.
        claimed = "PGU-860"
        fixture.seed_postgres_ticket(board.admin, claimed, title=claimed, state="analysis", assignee="director",
                                     commit_exempt=True)
        seen["pull_admit"] = board.act(claimed, "admit", "director", state="ready")
        seen["pull_claim"] = board.act(claimed, "claim", "app", state="in_progress")
        seen["pull_claimed"] = board.where(claimed)
        # A busy owner on a pull board: Router cannot queue its pick behind the owner's active ticket,
        # because the board would move it to the ready stage. Refused at both boundaries; nothing moves.
        # The claim above is app's active ticket.
        picked = "PGU-871"
        fixture.seed_postgres_ticket(board.admin, picked, title=picked, state="analysis", assignee="director",
                                     commit_exempt=True)
        board.act(picked, "route", "director", state="in_progress")
        revision = board.revision(picked)
        seen["pull_busy_http"] = board.act(picked, "router_assign", "router", assignee="app", revision=revision)
        seen["pull_busy_pg"] = board.as_router_sql(f"SELECT ticket_board.router_assign('{picked}', 'app', '{revision}');")
        seen["pull_busy_where"] = board.sql("SELECT state || '|' || assignee || '|' || queued_for_assignee FROM "
                                            f"ticket_board.tickets WHERE id = '{picked}';")
        seen["pull_busy_decisions"] = board.sql(f"SELECT count(*) FROM ticket_board.router_decisions WHERE ticket_id = '{picked}';")
        seen["pull_free"] = board.act(picked, "router_assign", "router", assignee="ops", revision=board.revision(picked))
        seen["pull_free_where"] = board.where(picked)


def pull_router() -> dict:
    from pull_scheduling_policy_test import pull_document
    cfg = pull_document()
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    return with_router(cfg, runtime="claude")


def single_owner_router() -> dict:
    cfg = example(router=False)
    next(s for s in cfg["stages"] if s["kind"] == "implementation")["owners"] = ["main"]
    # A tenant's own move into Implementation by a role that is not the Director, naming nobody: it is
    # decided as it always was (the stage's owner), because Router is the default for the Director's route only.
    template = next(x for x in cfg["transitions"] if x["action"] == "route" and x["to"] == "in_progress")
    cfg["transitions"].append({**template, "action": "hand_over", "label": "Hand over", "actors": ["audit"]})
    return with_router(cfg, runtime="claude")


def plain_board(board: Board, seen: dict) -> None:
    for t in ("PGU-801", "PGU-802"):
        fixture.seed_postgres_ticket(board.admin, t, title=t, state="analysis", assignee="director", commit_exempt=True)
    board.act("PGU-801", "route", "director", state="in_progress", assignee="router")
    seen["plain_route_router"] = board.where("PGU-801")
    seen["plain_route_main"] = board.act("PGU-802", "route", "director", state="in_progress", assignee="main")
    fixture.seed_postgres_ticket(board.admin, "PGU-803", title="PGU-803", state="analysis", assignee="director",
                                 commit_exempt=True)
    board.act("PGU-803", "route", "director", state="in_progress")
    seen["plain_default"] = board.where("PGU-803")
    seen["plain_where"] = board.where("PGU-802")
    seen["plain_reservations"] = board.reservations()
    seen["plain_router_op"] = board.act("PGU-802", "router_assign", "director", assignee="app", revision="0" * 32)
    seen["plain_routing_field"] = board.ticket("PGU-802").get("routing")


def scenario() -> dict:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    seen: dict = {}
    # Every board unless narrowed (a mutation run that changes only what one board exercises).
    wanted = set(filter(None, os.environ.get("SYRD599_BOARDS", "fresh,upgraded,pull,single,plain").split(",")))
    with temporary_cluster(prefix="syrd599-", shutdown="immediate") as cluster:
        for name, before in (("fresh", None), ("upgraded", None)):
            if name not in wanted:
                continue
            before = before_tree(cluster.root / "before") if name == "upgraded" else None
            board = Board(cluster, f"router_{name}", example(router=True), before=before)
            try:
                part: dict = {}
                router_board(board, part)
                seen[name] = part
            finally:
                board.close()
        for name, document in (("pull", pull_router()), ("single", single_owner_router())):
            if name not in wanted:
                continue
            board = Board(cluster, f"router_{name}", document)
            try:
                variant_board(board, seen, name)
            finally:
                board.close()
        if "plain" in wanted:
            board = Board(cluster, "plain", example(router=False))
            try:
                plain_board(board, seen)
            finally:
                board.close()
    return seen


_OWNERS = "router may assign only to an active owner of Implementation (app, main, ops), not"
#: Each attempt, and the refusal that proves the boundary answered it -- not a
#: missing function or a typo (SYRD-599). Generic operations fail on Router's
#: missing capability or transition; Router's own on what they check.
HTTP_REFUSALS = {
    **{op: (f"router cannot call {name}",) for op, name in (
        ("route", "route"), ("reassign", "reassign"), ("set_blockers", "set_blockers"), ("edit_parent", "edit_fields"),
        ("edit_body", "edit_fields"), ("comment", "add_comment"), ("force_move", "force_move"),
        ("override_move", "override_move"), ("director_edit", "director_edit"), ("hold", "set_manually_controlled"),
        ("kick_back", "implementer_kick_back"), ("submit", "submit_to_audit_without_commit"))},
    "flags": ("403", "only director may set gates"),
    **{f"assign_{who}": (f"{_OWNERS} {name}",) for who, name in (
        ("director", "director"), ("audit", "audit"), ("user", "user"), ("unassigned", "unassigned"),
        ("router", "router"), ("inactive", "perf"), ("made_up", "wizard"))},
    "assign_extra_field": ("router_assign takes exactly assignee, revision",),
    "assign_parent_payload": ("router_assign takes exactly assignee, revision",),
    "assign_stale": ("stale revision for PGU-706",),
    "return_empty": ("router_return requires a non-empty reason",),
    "return_with_assignee": ("router_return takes exactly reason, revision",),
    "director_assigns": ("director cannot call router_assign",),
    "main_returns": ("main cannot call router_return",),
    "not_routers": ("PGU-701 is not router work: it is in_progress / main",),
    "duplicate": ("PGU-702 is not router work: it is in_progress / app",),
    "in_triage": ("PGU-707 is not router work: it is analysis / director",),
}
PG_REFUSALS = {
    **{op: (f"role router cannot call {op}",) for op in ("reassign", "route", "set_blockers", "edit_fields")},
    "add_comment": ("role router cannot call add_comment",),
    "workflow_return": ("router_return is taken only through ticket_board.router_return",),
    "update": ("permission denied for table tickets",),
    "assign_reviewer": (f"{_OWNERS} audit",),
    "assign_stale": ("stale revision for PGU-706",),
    "assign_not_routers": ("PGU-701 is not router work: it is in_progress / main",),
    "return_empty": ("router_return requires a non-empty reason",),
    "window_parent": ("router may change only who owns PGU-706, not parent_id",),
    "window_flag": ("router may change only who owns PGU-706, not needs_audit",),
    "window_body": ("router may change only who owns PGU-706, not body",),
    "window_stage": ("router_assign changes only the assignee, to an Implementation owner",),
    "window_reviewer": ("router_assign changes only the assignee, to an Implementation owner",),
    "no_window": ("only director may reassign without transition",),
}


def verify(seen: dict) -> None:
    for name in [n for n in ("fresh", "upgraded") if n in seen]:
        s = seen[name]
        tag = f"[{name}] "
        check(all("ok" in r for r in s["routes"].values()), tag + f"the Director routes to router: {s['routes']}")
        check(all(w == ["in_progress", "router"] for w in s["placed"].values()), tag + f"all wait with router: {s['placed']}")
        reserved, in_queue, waiting, focus, remembered = s["router_reserved"]
        check(reserved is None and not in_queue and all(w == "f" for w in waiting) and focus == "0"
              and all(r == "" for r in remembered),
              tag + f"router holds no slot, is in no queue and is nobody's implementer: {s['router_reserved']}")
        deliveries = s["router_deliveries"]
        check([len(d) for d in deliveries] == [1, 1, 1, 1, 1, 1, 0],
              tag + f"router is told about each ticket once, one at a time: {deliveries}")
        check(sorted(d[0].split(" ")[0] for d in deliveries if d) == [f"PGU-{700 + n}" for n in range(1, 7)],
              tag + f"every routed ticket reaches router: {deliveries}")
        routing = s["routing_field"] or {}
        check(routing.get("holder") == "router" and len(routing.get("revision", "")) == 32
              and routing.get("assignable") == ["app", "main", "ops"],
              tag + f"one read gives router its revision and the owners it may choose: {routing}")
        check(s["routing_actions"] == ["router_return"], tag + f"router's only declared move: {s['routing_actions']}")
        check(all("ok" in s[k] for k in ("assign_main", "assign_app", "assign_ops", "assign_main_second")),
              tag + "clear examples are assigned to main, app and ops: "
              + str({k: s[k] for k in ("assign_main", "assign_app", "assign_ops", "assign_main_second")})[:1500])
        check(s["assign_main_changed"] == ["assignee", "decisions"],
              tag + f"an assignment changes the owner, records the decision, and nothing else: {s['assign_main_changed']}")
        check(s["after_assign"] == {"PGU-701": ["in_progress", "main"], "PGU-702": ["in_progress", "app"],
                                    "PGU-703": ["in_progress", "ops"], "PGU-704": ["in_progress", "main"]},
              tag + f"assigned tickets stay in Implementation: {s['after_assign']}")
        check(s["reservations"].get("main") == "PGU-701" and s["reservations"].get("app") == "PGU-702"
              and s["reservations"].get("ops") == "PGU-703" and "router" not in s["reservations"],
              tag + f"each owner's slot is decided by the existing queue: {s['reservations']}")
        check(s["main_second_waiting"] == "t" and s["main_second_queued"] == "0",
              tag + f"main's second ticket waits in main's queue, and nothing is queued to tell main: {s['main_second_queued']}")
        notices = s["owner_notices"]
        check(len(notices["app"]) == 1 and len(notices["ops"]) == 1
              and [m.split(" ")[0] for m in notices["main"]] == ["PGU-701"],
              tag + f"each owner is told once, and not about work waiting in its queue: {notices}")
        decisions = json.loads(s["decisions"])
        check([d[:4] for d in decisions] == [["PGU-701", "router", "assign", "main"], ["PGU-702", "router", "assign", "app"],
                                              ["PGU-703", "router", "assign", "ops"], ["PGU-704", "router", "assign", "main"]]
              and all(d[4] == s["revisions"][d[0]] for d in decisions),
              tag + f"each decision records router, the role and the revision it classified: {decisions}")
        check("ok" in s["return"] and s["returned"] == ["analysis", "director"],
              tag + f"an ambiguous ticket returns to analysis/director: {s['return']} {s['returned']}")
        check(s["return_comment"] == "router|Router returned this to the Director's triage. Reason: Spans app and ops; needs the Director.",
              tag + f"the reason is the Director's handoff: {s['return_comment']}")
        check(len(s["director_handoff"]) == 1 and s["director_handoff"][0].startswith("PGU-705 "),
              tag + f"the Director gets one handoff: {s['director_handoff']}")
        for key, answer in s["http"].items():
            check(refused(answer, *HTTP_REFUSALS[key]), tag + f"HTTP refuses router's {key} for the right reason: {answer}")
        check(set(s["http"]) == set(HTTP_REFUSALS), tag + "every HTTP attempt is pinned")
        for key, answer in s["pg"].items():
            check(refused(answer, *PG_REFUSALS[key]), tag + f"PostgreSQL refuses router's {key} for the right reason: {answer}")
        check(set(s["pg"]) == set(PG_REFUSALS), tag + "every PostgreSQL attempt is pinned")
        check(refused(s["http"]["assign_stale"], "stale revision"), tag + f"a stale revision says so: {s['http']['assign_stale']}")
        check(refused(s["pg"]["assign_stale"], "stale revision"), tag + f"so does the database: {s['pg']['assign_stale']}")
        check(refused(s["pg"]["window_parent"], "router may change only who owns")
              and refused(s["pg"]["window_reviewer"], "Implementation owner"),
              tag + f"the window admits only its own change: {s['pg']['window_parent']} {s['pg']['window_reviewer']}")
        check(refused(s["pg"]["workflow_return"], "router_return is taken only through"),
              tag + f"the generic executor cannot take router_return: {s['pg']['workflow_return']}")
        check(s["held_unchanged"] and s["held_where"] == ["in_progress", "router"],
              tag + f"every refused write left the ticket exactly as it was: {s['held_where']}")
        check("ok" in s["director_comment_for_router"], tag + "the Director still works the same ticket")
        check(refused(s["stale_after_edit"], "stale revision") and s["fresh_changed"] and "ok" in s["fresh_assign"],
              tag + f"a Director edit makes router's earlier read stale: {s['stale_after_edit']}")
        check("refused" in s["fresh_again"] and s["fresh_where"] == ["in_progress", "ops"],
              tag + f"a duplicate submission moves nothing twice: {s['fresh_again']} {s['fresh_where']}")
        check("ok" in s["blocked_return"] and s["blocked_where"] == ["analysis", "director"],
              tag + f"a blocked ticket is handed back all the same: {s['blocked_return']} {s['blocked_where']}")
        check(refused(s["held_assign"], "held by the Director") and refused(s["held_assign_pg"], "held by the Director")
              and s["held_where"] == ["in_progress", "router"],
              tag + f"a ticket the Director holds is not Router's to move: {s['held_assign']} {s['held_assign_pg']}")
        check("ok" in s["back_to_router"] and refused(s["old_round"], "stale revision") and "ok" in s["new_round"]
              and s["again_where"] == ["in_progress", "app"],
              tag + f"a new round is a new revision, even with the same content: {s['old_round']} {s['again_where']}")
        check(s["reapply_while_holding"] == [["in_progress", "router"], None],
              tag + f"the workflow can change while Router holds work: {s['reapply_while_holding']}")
        check(s["comment_changed_revision"] and refused(s["comment_stale_http"], "stale revision for PGU-717")
              and refused(s["comment_stale_pg"], "stale revision for PGU-717") and s["comment_where"] == ["in_progress", "router"],
              tag + f"a Director comment after Router's read makes it stale, over HTTP and in PostgreSQL: "
              f"{s['comment_stale_http']} {s['comment_stale_pg']}")
        check("ok" in s["comment_fresh"] and s["comment_after"] == ["in_progress", "ops"],
              tag + f"and Router's fresh read, with the comment, decides: {s['comment_after']}")
        check(all("ok" in m for m in s["bounce_moves"]) and s["bounce_silent"] and s["bounce_where"] == ["in_progress", "router"]
              and refused(s["bounce_stale"], "stale revision for PGU-719"),
              tag + f"a silent defer-and-reroute is a new round, so the earlier read is stale: {s['bounce_moves']} {s['bounce_stale']}")
        check(refused(s["attachment_stale"], "stale revision for PGU-718"),
              tag + f"an attachment added after the read makes it stale too: {s['attachment_stale']}")
        check("ok" in s["direct"] and s["direct_where"] == ["in_progress", "ops"] and s["direct_decisions"] == "0"
              and s["direct_routing"] is None,
              tag + f"the Director's direct route lands with the owner it names and is no Router decision: {s['direct_where']}")
        check("ok" in s["default_pg"] and s["default_pg_where"] == ["in_progress", "router"],
              tag + f"the database itself sends an unnamed route to Router: {s['default_pg']} {s['default_pg_where']}")
        check(refused(s["unavailable_default"], "Router is not running, so PGU-716 cannot be routed through it",
                      "route it to an Implementation owner directly (main, app, ops)")
              and refused(s["unavailable_named_router"], "Router is not running")
              and s["unavailable_where"] == ["analysis", "director"],
              tag + f"with Router down the default is refused, not given to an implementer: {s['unavailable_default']}")
        check("ok" in s["unavailable_direct"] and s["unavailable_direct_where"] == ["in_progress", "app"],
              tag + f"and the direct route still works: {s['unavailable_direct_where']}")
        check("ok" in s["kick_back"] and s["kicked"] == ["analysis", "director"],
              tag + f"an implementer hands a misroute back to the Director: {s['kick_back']} {s['kicked']}")
        check("ok" in s["correction"], tag + f"the Director corrects an assignment: {s['correction']}")
        outcomes = json.loads(s["outcomes"])
        check(outcomes == [["PGU-701", "misroute", "main", "This is app's renderer, not main's."],
                           ["PGU-702", "director_correction", "director", None],
                           ["PGU-712", "director_correction", "director", None]],
              tag + f"both are recorded against router's decision: {outcomes}")
        history = (s["history"] or {}).get("decisions") or []
        check("holder" not in (s["history"] or {}) and history and history[-1].get("outcome") == "misroute",
              tag + f"the ticket carries its routing history: {s['history']}")
        check("orphan" in s["disable_while_holding"], tag + f"Router cannot be turned off holding work: {s['disable_while_holding']}")
        check(s["disable_empty"] == "applied" and s["route_after_disable"] == [["in_progress", "main"], None],
              tag + f"once empty it can, and then no work waits with it: {s['disable_empty']} {s['route_after_disable']}")
    for name in [n for n in ("pull", "single") if f"{n}_routes" in seen]:
        check(all("ok" in r for r in seen[f"{name}_routes"].values())
              and all(w == ["in_progress", "router"] for w in seen[f"{name}_where"].values())
              and seen[f"{name}_queued"] == "",
              f"[{name}] work routed to Router stays with it, never diverted or defaulted: {seen[f'{name}_where']}")
        check("ok" in seen[f"{name}_assign"] and seen[f"{name}_assigned"] == ["in_progress", "main"],
              f"[{name}] and Router assigns it: {seen[f'{name}_assign']} {seen[f'{name}_assigned']}")
    if "single_hand_over" in seen:
        check("ok" in seen["single_hand_over"] and seen["single_handed"] == ["in_progress", "main"],
              f"[single] another role's move into Implementation is decided as before, not given to Router: "
              f"{seen['single_hand_over']} {seen['single_handed']}")
    if "pull_busy_http" in seen:
        busy = "app already has PGU-860 active; on this pull-scheduled board Router cannot queue work behind it"
        check(refused(seen["pull_busy_http"], busy) and refused(seen["pull_busy_pg"], busy)
              and seen["pull_busy_where"] == "in_progress|router|" and seen["pull_busy_decisions"] == "0",
              f"[pull] Router's pick of a busy owner is refused at both boundaries and nothing moves: "
              f"{seen['pull_busy_http']} {seen['pull_busy_pg']} {seen['pull_busy_where']}")
        check("ok" in seen["pull_free"] and seen["pull_free_where"] == ["in_progress", "ops"],
              f"[pull] a free owner is assigned in place: {seen['pull_free_where']}")
    if "pull_claim" in seen:
        check("ok" in seen["pull_admit"] and "ok" in seen["pull_claim"] and seen["pull_claimed"] == ["in_progress", "app"],
              f"[pull] an implementer's own claim stays the implementer's: {seen['pull_claim']} {seen['pull_claimed']}")
    if "plain_route_router" not in seen:
        return
    check(seen["plain_route_router"] == ["in_progress", "main"],
          f"without Router nothing waits with it; the board's rule for a non-owner applies: {seen['plain_route_router']}")
    check(seen["plain_default"] == ["in_progress", "main"],
          f"without Router an unnamed route is decided as before: {seen['plain_default']}")
    check("ok" in seen["plain_route_main"] and seen["plain_where"] == ["in_progress", "main"]
          and seen["plain_reservations"].get("main") == "PGU-801",
          f"without Router the Director routes straight to an implementer, as before: {seen['plain_where']}")
    check("refused" in seen["plain_router_op"] and seen["plain_routing_field"] is None,
          f"and Router's operations do not exist for anyone: {seen['plain_router_op']}")


def main() -> int:
    seen = scenario()
    if not os.environ.get("SYRD599_BOARDS"):
        check({"fresh", "upgraded", "pull_routes", "single_routes", "plain_route_router"} <= set(seen),
              f"every board ran: {sorted(seen)}")
    if os.environ.get("SYRD599_DUMP"):
        Path(os.environ["SYRD599_DUMP"]).write_text(json.dumps({k: seen["fresh"][k] for k in ("http", "pg")}, indent=1))
    verify(seen)
    print(f"router_role_board_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
