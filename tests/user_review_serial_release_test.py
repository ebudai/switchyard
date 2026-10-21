#!/usr/bin/env python3
"""SYRD-582: a ticket with the User releases its implementer's slot, and queued work is never delivered early.

Live on Otto (release f92a3d4d, 2026-10-10/11), designer's queue:
  * OTTO-134 was routed to the designer while OTTO-128 waited in user_review with
    the User. The board queued it (position 2), but the Director's routing
    comment raised the owner notice, which never asked whether the ticket was
    waiting, so the designer was handed it, did it, and had the submission
    refused ("waiting in designer's queue behind OTTO-128").
  * OTTO-133 was routed the same way while OTTO-134 waited with the User: queued,
    never delivered, and the designer sat idle until the User noticed.

Production-built board (companion roles, schema.sql, the real
ticket-board-migrate, rbac.sql) with the shipped workflow on Otto's queue
settings and Otto's Director re-audit route, the real HTTP server and the real
notify listener with an injected sender. The before-run is main's code and SQL,
in a child built from a git archive; the upgrade case builds main's board in
Otto's exact state and applies this tree's migrations and rbac.sql to it.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "532fbcec63023b600d3dcbcd9b420038a290b0f1"  # main before SYRD-582
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def migrate(fixture, admin: str, root: Path) -> None:
    """An upgrade as production deploys one: every migration, then rbac.sql."""
    result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())


def build_board(fixture, cluster, root: Path, dbname: str, roles: bool = True) -> str:
    """A board as production builds one: schema.sql, roles, rbac.sql, every migration, rbac.sql again.

    The companion roles belong to the cluster, so a second board in it reuses them.
    """
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
    if roles:
        fixture.create_roles(admin)
    fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
    migrate(fixture, admin, root)
    return admin


def workflow(root: Path, reservation: str | None = None) -> dict:
    """The shipped document with Otto's queue settings and Otto's Director re-audit out of User review."""
    cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
    cfg["project"] = "pgu"
    cfg["queue"] = {"stage": "backlog", "assignee": "unassigned"}  # Otto's
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
    route = next(t for t in cfg["transitions"] if t["action"] == "route" and t["from"] == "analysis")
    cfg["transitions"].append({**route, "from": "user_review", "to": "audit"})
    if reservation:
        cfg["reservation"] = reservation
    return cfg


class Board:
    """The real server and listener on one production-built board."""

    def __init__(self, fixture, cluster, admin: str, dbname: str, label: str) -> None:
        import psycopg
        from scripts.ticket_board.notify_listener import TicketBoardNotifyListener

        self.fixture, self.admin, self.psycopg, self.Listener = fixture, admin, psycopg, TicketBoardNotifyListener
        app = fixture.TicketBoardApp(cluster.root / f"frames-{label}", cluster.root / f"assets-{label}", project="pgu",
                                     ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        self.app = app
        self.server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = self.server.write_token
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        self.sent: list[tuple[str, str]] = []
        self.listener = self.new_listener()

    @property
    def uat(self) -> bool:
        """Whether this board has the User's UAT steps and Send to Director (SYRD-629): main's replay and the
        board built before the upgrade do not."""
        return self.sql("SELECT to_regprocedure('ticket_board.set_uat_instructions(text,text,text,text,boolean)') "
                        "IS NOT NULL;") == "t"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def new_listener(self):
        return self.Listener(conninfo=self.listener_url, sender=lambda target, message: self.sent.append((target, message)),
                             activity_gate=lambda _target: False, target_exists=lambda _target: True,
                             submission_witness=lambda target, _since: any(t == target for t, _m in self.sent),
                             poll_seconds=0, project="pgu")

    def act(self, ticket: str, operation: str, actor: str, **payload) -> dict:
        try:
            return {"ok": self.fixture.post_json(self.base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)}
        except Exception as exc:  # a refusal is an answer
            return {"refused": str(exc)}

    def sql(self, statement: str, url: str | None = None) -> str:
        return self.fixture.psql(url or self.admin, statement).strip()

    def one_pass(self, listener=None) -> list[tuple[str, str]]:
        """One iteration of the listener's loop, as listen_once runs it, without its wait."""
        current = listener or self.listener
        before = len(self.sent)
        with self.psycopg.connect(self.listener_url, autocommit=True) as conn:
            current.refresh_workflow(conn)
            for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                         "process_serial_focus_queue_wakeups"):
                if hasattr(current, step):
                    getattr(current, step)(conn)
            current.process_due_notifications(conn)
        return self.sent[before:]

    def reservation(self, role: str) -> str:
        return self.sql(f"SELECT coalesce(max(ticket_id), '') FROM ticket_board.serial_reservations() WHERE implementer = '{role}';")

    def waiting(self, ticket: str) -> str:
        return self.sql(f"SELECT ticket_board.ticket_serial_waiting('{ticket}');")

    def where(self, ticket: str) -> list[str]:
        return self.sql(f"SELECT state || '|' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';").split("|")

    def ticket_json(self, ticket: str) -> dict:
        with urllib.request.urlopen(f"{self.base}/api/tickets/{ticket}") as response:
            return json.loads(response.read())


def about(batch: list[tuple[str, str]], role: str, ticket: str) -> list[str]:
    """Every message the role's pane was sent that names the ticket."""
    return [m for target, m in batch if target == f"pgu-{role}:0.0" and ticket in m]


def to_user(board: Board, ticket: str) -> dict:
    """The Director's acceptance to the User, with the User's UAT steps prepared first where the board has
    them (SYRD-629); main's replay runs on a tree from before."""
    if board.uat:
        prepared = board.act(ticket, "set_uat_instructions", "director", do_this="Open the schedule.",
                             you_should_see="This week's entries.")
        if "refused" in prepared:
            return prepared
    return board.act(ticket, "director_dat_sign_off", "director")


def to_user_review(board: Board, ticket: str, author: str) -> list[dict]:
    """Implementation through Audit and Director acceptance to the User, as Otto's tickets went."""
    return [board.act(ticket, "submit_to_audit_without_commit", author, reason="finished"),
            board.act(ticket, "audit_sign_off", "audit", text="Audited."),
            to_user(board, ticket)]


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        board = Board(fixture, cluster, admin, "board", "a")
        board.app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        A, B = "PGU-801", "PGU-802"          # app: Otto's OTTO-133 sequence, then the User's rework
        E, F = "PGU-811", "PGU-812"          # ops: Otto's OTTO-134 sequence, then the User's sign-off
        G, H = "PGU-821", "PGU-822"          # main: the lifecycle policy
        K = "PGU-823"                        # main: active work that gets blocked
        for ticket in (A, B, E, F, G, H, K):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True, needs_user_signoff=True)
        board.one_pass()

        # --- app: B is routed while A is still in Director acceptance, so it waits.
        board.act(A, "route", "director", state="in_progress", assignee="app")
        seen["a_handed"] = about(board.one_pass(), "app", A)
        seen["a_review"] = [board.act(A, "submit_to_audit_without_commit", "app", reason="finished"),
                            board.act(A, "audit_sign_off", "audit", text="Audited.")]
        seen["dat_holds"] = [board.where(A), board.reservation("app")]
        seen["b_route"] = board.act(B, "route", "director", state="in_progress", assignee="app")
        seen["b_waits"] = [board.where(B), board.waiting(B), about(board.one_pass(), "app", B)]
        # The Director's comment on the waiting ticket wakes nobody with it.
        seen["b_comment"] = board.act(B, "add_comment", "director", text="DIRECTOR ROUTING: to app, after A.")
        seen["b_comment_sent"] = about(board.one_pass(), "app", B)
        seen["b_comment_raised"] = board.sql(
            "SELECT (SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE ticket_id = "
            f"'{B}' AND kind = 'ticket_update') + (SELECT count(*) FROM ticket_board.notification_trace "
            f"WHERE ticket_id = '{B}' AND kind = 'ticket_update');")
        # A reaches the User: app is free, and B is handed over, once.
        seen["a_to_user"] = to_user(board, A)
        first = board.one_pass()
        again = board.one_pass(board.new_listener()) + board.one_pass()
        seen["a_with_user"] = board.where(A)
        seen["b_activated"] = [about(first, "app", B), about(again, "app", B), board.reservation("app"), board.waiting(B)]
        seen["b_activation_rows"] = board.sql(
            "SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'send' "
            f"AND ticket_id = '{B}' AND target_role = 'app' AND kind = 'transition';")
        seen["b_queue"] = board.ticket_json(B).get("serial_queue")
        seen["a_queue"] = board.ticket_json(A).get("serial_queue")
        # The User sends A back -- to the Director, who sends it to app (SYRD-629): the rework queues behind B
        # and does not interrupt it.
        if board.uat:
            sent = board.act(A, "user_send_to_director", "user", reason="UAT: the schedule is wrong")
            seen["a_rework"] = sent if "refused" in sent else board.act(
                A, "director_send_to_implementer", "director", reason="The User found the schedule wrong.")
        else:
            seen["a_rework"] = board.act(A, "user_kick_back", "user", reason="UAT: the schedule is wrong")
        seen["a_rework_waits"] = [board.where(A), board.waiting(A), board.waiting(B), board.reservation("app"),
                                  about(board.one_pass(), "app", A)]
        seen["a_rework_submit"] = board.act(A, "submit_to_audit_without_commit", "app", reason="fixed")
        # B goes on through Audit, which holds app as it always did; A waits there too.
        seen["b_review"] = [board.act(B, "submit_to_audit_without_commit", "app", reason="finished"),
                            board.act(B, "audit_sign_off", "audit", text="Audited.")]
        seen["b_audit_holds"] = [board.reservation("app"), board.waiting(A), about(board.one_pass(), "app", A)]
        # B reaches the User: the rework is app's active ticket again, handed over once.
        seen["b_to_user"] = to_user(board, B)
        rework = board.one_pass() + board.one_pass(board.new_listener())
        seen["a_reactivated"] = [about(rework, "app", A), board.reservation("app"), board.waiting(A)]
        # The rework is new work for Audit: it holds app there again, until it is with the User.
        seen["a_resubmit"] = [board.act(A, "submit_to_audit_without_commit", "app", reason="fixed"),
                              board.where(A), board.reservation("app")]

        # --- ops: OTTO-134 exactly. E is with the User; the Director routes F, then comments.
        board.act(E, "route", "director", state="in_progress", assignee="ops")
        board.one_pass()
        seen["e_steps"] = to_user_review(board, E, "ops")
        board.one_pass()
        board.sent.clear()
        seen["f_route"] = board.act(F, "route", "director", state="in_progress", assignee="ops")
        seen["f_comment"] = [board.act(F, "add_comment", "director", text="DIRECTOR ROUTING: to ops. Independent of E."),
                             board.act(F, "add_comment", "director", text="DIRECTOR NOTE: queued behind E?")]
        delivered = board.one_pass() + board.one_pass(board.new_listener())
        seen["f_delivered"] = [about(delivered, "ops", F), board.where(F), board.reservation("ops"), board.waiting(F)]
        # The User signs E off; Director review does not take ops back from F.
        seen["e_sign_off"] = board.act(E, "user_sign_off", "user")
        seen["after_sign_off"] = [board.where(E), board.reservation("ops"), board.waiting(F)]
        # Nor does the Director's re-audit of a ticket the User already had.
        seen["f_submit"] = board.act(F, "submit_to_audit_without_commit", "ops", reason="done")

        # --- main: blocking the ticket an implementer is working on still tells them, once.
        board.act(K, "route", "director", state="in_progress", assignee="main")
        seen["k_handed"] = about(board.one_pass(), "main", K)
        seen["k_block"] = board.act(K, "set_blockers", "director", blocked_by=[H], blocked_reason="after H")
        seen["k_told"] = [about(board.one_pass(), "main", K), board.waiting(K)]

        # --- main, lifecycle: the tenant's choice to keep the author through User review stands.
        revision = int(board.sql("SELECT revision FROM ticket_board.workflow_configuration ORDER BY revision DESC LIMIT 1;"))
        board.app.apply_workflow(workflow(root, "lifecycle"), expected_revision=revision, dry_run=False, caller_role="director")
        board.act(G, "route", "director", state="in_progress", assignee="main")
        board.one_pass()
        seen["g_steps"] = to_user_review(board, G, "main")
        board.act(H, "route", "director", state="in_progress", assignee="main")
        seen["lifecycle"] = [board.where(G), board.reservation("main"), board.waiting(H), about(board.one_pass(), "main", H)]

        # --- a pull board releasing after User review keeps its own release point.
        from pull_scheduling_policy_test import pull_document

        pull = pull_document(release_after="user_review")
        pull["project"] = "pgu"
        for role in pull["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        pull_admin = build_board(fixture, cluster, root, "pullboard", roles=False)
        pull_board = Board(fixture, cluster, pull_admin, "pullboard", "p")
        pull_board.app.apply_workflow(pull, expected_revision=0, dry_run=False, caller_role="director")
        P = "PGU-831"
        fixture.seed_postgres_ticket(pull_admin, P, title=P, state="analysis", assignee="director",
                                     commit_exempt=True, needs_user_signoff=True)
        pull_board.act(P, "route", "director", state="in_progress", assignee="app")
        seen["pull_steps"] = to_user_review(pull_board, P, "app")
        seen["pull"] = [pull_board.where(P), pull_board.reservation("app")]
        pull_board.close()
        board.fixture.TEST_WRITE_TOKEN = board.server.write_token

        # Copy parity and grants: a fresh schema.sql board installs what the migrations do.
        try:
            functions = ("ticket_current_reserved_ticket(text, text)", "notify_ticket_owner_in_place_change(text, text)",
                         "record_user_review_release()")
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            definition = "SELECT md5(pg_get_functiondef('ticket_board.{}'::regprocedure));"
            seen["fresh_equals_migrated"] = {f: fixture.psql(fresh, definition.format(f)).strip() == board.sql(definition.format(f))
                                             for f in functions}
            triggers = ("SELECT string_agg(tgname || ':' || tgfoid::regproc::text, ',' ORDER BY tgname) FROM pg_trigger "
                        "WHERE tgname LIKE '%user_review_release%';")
            seen["fresh_triggers"] = [fixture.psql(fresh, triggers).strip(), board.sql(triggers)]
            seen["grants"] = board.sql("SELECT string_agg(grantee || ':' || privilege_type, ',' ORDER BY grantee, privilege_type) "
                                       "FROM information_schema.role_table_grants WHERE table_schema = 'ticket_board' "
                                       "AND table_name = 'user_review_releases' AND grantee LIKE 'ticket_board_%';")
        except Exception as exc:  # main has none of them
            seen["fresh_equals_migrated"] = seen["fresh_triggers"] = seen["grants"] = f"unavailable: {str(exc)[-160:]}"
        board.close()
    return seen


def upgrade(before_root: Path, prefix: str) -> dict:
    """main's board in Otto's state -- X with the User, Y and W queued behind it -- then this tree's migrations."""
    sys.path.insert(0, str(ROOT / "tests"))
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, before_root, "old")
        board = Board(fixture, cluster, admin, "old", "old")
        board.app.apply_workflow(workflow(before_root), expected_revision=0, dry_run=False, caller_role="director")
        X, Y, W, V = "PGU-901", "PGU-902", "PGU-903", "PGU-904"
        for ticket in (X, Y, W, V):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True, needs_user_signoff=True)
        board.act(X, "route", "director", state="in_progress", assignee="app")
        seen["x_steps"] = to_user_review(board, X, "app")
        seen["y_route"] = board.act(Y, "route", "director", state="in_progress", assignee="app")
        # V is in Audit for ops: a review before the User, which the upgrade must leave holding.
        board.act(V, "route", "director", state="in_progress", assignee="ops")
        seen["v_submit"] = board.act(V, "submit_to_audit_without_commit", "ops", reason="finished")
        board.one_pass()
        # W's routing comment was queued for app on main and has not gone out yet.
        board.act(W, "route", "director", state="in_progress", assignee="app")
        board.act(W, "add_comment", "director", text="DIRECTOR ROUTING: to app, after Y.")
        seen["before"] = [board.where(X), board.reservation("app"), board.waiting(Y), board.waiting(W)]
        seen["w_pending_before"] = board.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                             f"WHERE ticket_id = '{W}' AND target_role = 'app' AND dead_lettered_at IS NULL;")
        board.close()
        seen["queued_before"] = board.sql("SELECT coalesce(string_agg(ticket_id || ':' || kind, ',' ORDER BY id), '') "
                                          "FROM ticket_board.ticket_notification_queue WHERE dead_lettered_at IS NULL;")
        migrate(fixture, admin, ROOT)
        seen["queued_after"] = board.sql("SELECT coalesce(string_agg(ticket_id || ':' || kind, ',' ORDER BY id), '') "
                                         "FROM ticket_board.ticket_notification_queue WHERE dead_lettered_at IS NULL;")
        try:
            seen["released"] = board.sql("SELECT coalesce(string_agg(ticket_id, ',' ORDER BY ticket_id), '') "
                                         "FROM ticket_board.user_review_releases;")
        except AssertionError as exc:  # main has no such table
            seen["released"] = f"unavailable: {str(exc)[-120:]}"
        board = Board(fixture, cluster, admin, "old", "new")
        first = board.one_pass()
        again = board.one_pass(board.new_listener()) + board.one_pass()
        seen["after"] = [about(first, "app", Y), about(first, "app", W), about(again, "app", Y) + about(again, "app", W),
                         board.reservation("app"), board.waiting(Y), board.waiting(W)]
        seen["v_holds"] = [board.where(V), board.reservation("ops")]
        seen["dropped"] = board.sql("SELECT coalesce(string_agg(kind || ':' || coalesce(busy_reason, ''), ',' ORDER BY id), '') "
                                    f"FROM ticket_board.notification_trace WHERE ticket_id = '{W}' AND event = 'drop';")
        seen["y_submit"] = board.act(Y, "submit_to_audit_without_commit", "app", reason="done")
        board.close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    env = clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "s582b")))
        return 0
    # A mutation run edits this tree alone: main's replay cannot change, so it is
    # skipped, and so is copy parity (the mutant edits the migration's copy only).
    mutation_run = os.environ.get("SYRD582_MUTATION_RUN") == "1"
    with tempfile.TemporaryDirectory(prefix="syrd582.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        before: dict = {}
        if not mutation_run:
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                                   capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        upgraded = upgrade(before_root, "s582u")

    after = scenario(ROOT, "s582a")
    if not mutation_run:
        main_as_otto_saw_it(before, after)
    else:
        before = {"lifecycle": after["lifecycle"], "pull": after["pull"]}
    after_checks(before, after, upgraded)
    if mutation_run:
        print(f"user_review_serial_release_test: {CHECKS} checks ok (mutation run: main's replay and copy parity skipped)")
        return 0
    parity_checks(after)
    print(f"user_review_serial_release_test: {CHECKS} checks ok")
    return 0


def main_as_otto_saw_it(before: dict, after: dict) -> None:
    A, E, F = "PGU-801", "PGU-811", "PGU-812"
    # main, as Otto saw it: OTTO-133 queued silently behind a ticket only the User
    # could move; OTTO-134 delivered by its comment, then refused.
    check(before["a_with_user"] == ["user_review", "user"] and before["b_activated"][0] == []
          and before["b_activated"][2] == A and before["b_activated"][3] == "t",
          f"before: with A at the User, B stayed queued behind it and was never handed over: {before['b_activated']}")
    check(before["b_comment_sent"] and "new comment" in before["b_comment_sent"][0],
          f"before: the Director's comment on a waiting ticket handed it over: {before['b_comment_sent']}")
    check(before["f_delivered"][0] and before["f_delivered"][3] == "t" and before["f_delivered"][2] == E
          and "refused" in before["f_submit"] and f"{F} is waiting in ops" in before["f_submit"]["refused"],
          f"before: OTTO-134 -- F was delivered by its comment while queued behind E, and its submission refused: "
          f"{before['f_delivered']} {before['f_submit']}")


def after_checks(before: dict, after: dict, upgraded: dict) -> None:
    A, B, F, G = "PGU-801", "PGU-802", "PGU-812", "PGU-821"
    # Review stages before the User still hold their author.
    check(len(after["a_handed"]) == 1 and after["dat_holds"] == [["dat", "director"], A],
          f"A in Director acceptance still holds app: {after['dat_holds']}")
    check("ok" in after["b_route"] and after["b_waits"] == [["in_progress", "app"], "t", []],
          f"B routed behind it waits, assigned, and is handed nothing: {after['b_waits']}")
    check("ok" in after["b_comment"] and after["b_comment_sent"] == [],
          f"a Director's comment on a waiting ticket wakes nobody with it: {after['b_comment_sent']}")
    check(after["b_comment_raised"] == "0",
          f"the board raises no owner notice for it at all, so nothing waits to be delivered: {after['b_comment_raised']}")
    # 1. With A at the User, app is free and B is handed over exactly once.
    check(after["a_with_user"] == ["user_review", "user"], f"A is with the User: {after['a_with_user']}")
    check(len(after["b_activated"][0]) == 1 and after["b_activated"][0][0].startswith(f"{B} -- {B} entered Implementation")
          and "now your active ticket" in after["b_activated"][0][0]
          and after["b_activated"][1] == [] and after["b_activated"][2:] == [B, "f"] and after["b_activation_rows"] == "1",
          f"B becomes app's active ticket and is handed over once, across a listener restart: "
          f"{after['b_activated']} sends={after['b_activation_rows']}")
    check(after["b_queue"] and after["b_queue"]["active"] is True and after["b_queue"]["position"] == 1
          and after["a_queue"] is None,
          f"the queue publishes B as active, and A with the User in no queue: {after['b_queue']} {after['a_queue']}")
    # 2. The User's rework queues behind B and does not interrupt it.
    check("ok" in after["a_rework"] and after["a_rework_waits"] == [["in_progress", "app"], "t", "f", B, []],
          f"the User's rework returns to app, waits behind B, and is handed nothing: {after['a_rework_waits']}")
    check("refused" in after["a_rework_submit"] and f"behind {B}" in after["a_rework_submit"]["refused"],
          f"and cannot be submitted while it waits: {after['a_rework_submit']}")
    check(all("ok" in s for s in after["b_review"]) and after["b_audit_holds"] == [B, "t", []],
          f"B in Audit still holds app, as review always did: {after['b_audit_holds']}")
    check("ok" in after["b_to_user"] and len(after["a_reactivated"][0]) == 1
          and "now your active ticket" in after["a_reactivated"][0][0] and after["a_reactivated"][1:] == [A, "f"],
          f"B at the User hands the rework back, once: {after['a_reactivated']}")
    check("ok" in after["a_resubmit"][0] and after["a_resubmit"][1:] == [["audit", "audit"], A],
          f"the resubmitted rework holds app in Audit again: {after['a_resubmit']}")
    # 3. OTTO-134 exactly: delivered once, as active work, and accepted.
    check(all("ok" in s for s in after["e_steps"]) and "ok" in after["f_route"] and all("ok" in c for c in after["f_comment"]),
          f"E reached the User; F was routed and commented on: {after['e_steps']} {after['f_comment']}")
    entered = [m for m in after["f_delivered"][0] if " entered Implementation" in m]
    check(len(entered) == 1 and after["f_delivered"][1:] == [["in_progress", "ops"], F, "f"],
          f"F is ops's active ticket, handed over once: {after['f_delivered']}")
    check("ok" in after["e_sign_off"] and after["after_sign_off"] == [["director_review", "director"], F, "f"],
          f"the User's sign-off does not take ops back from F in Director review: {after['after_sign_off']}")
    check("ok" in after["f_submit"], f"and F's finished work is accepted: {after['f_submit']}")
    check(len(after["k_handed"]) == 1 and "ok" in after["k_block"] and len(after["k_told"][0]) == 1
          and "changed by director: blockers" in after["k_told"][0][0] and after["k_told"][1] == "t",
          f"blocking an implementer's active ticket still tells them, once: {after['k_told']}")
    # 4. The lifecycle policy keeps the author through User review, as its tenant chose.
    check(all("ok" in s for s in after["g_steps"]) and after["lifecycle"] == [["user_review", "user"], G, "t", []],
          f"lifecycle: G with the User still holds main, and H waits unannounced: {after['lifecycle']}")
    check(before["lifecycle"] == after["lifecycle"], f"exactly as on main: {before['lifecycle']}")
    # 5. A pull board keeps its declared release point, here the User's approval.
    check(all("ok" in s for s in after["pull_steps"]) and after["pull"] == [["user_review", "user"], "PGU-831"]
          and before["pull"] == after["pull"],
          f"pull: a ticket with the User still holds its author until the User approves, as on main: "
          f"{after['pull']} {before['pull']}")
    # 6. Upgrading Otto's board hands the waiting ticket over once, and drops the comment queued for W.
    check(upgraded["before"] == [["user_review", "user"], "PGU-901", "t", "t"] and upgraded["w_pending_before"] == "1",
          f"main's board in Otto's state, with W's comment notice still queued: {upgraded['before']}")
    check(upgraded["released"] == "PGU-901", f"the migration records X, which is with the User: {upgraded['released']}")
    check("ok" in upgraded["v_submit"] and upgraded["v_holds"] == [["audit", "audit"], "PGU-904"],
          f"and leaves V, in Audit, holding ops: {upgraded['v_holds']}")
    y_sent, w_sent, repeated, held, y_waits, w_waits = upgraded["after"]
    check(len(y_sent) == 1 and "now your active ticket" in y_sent[0] and w_sent == [] and repeated == []
          and (held, y_waits, w_waits) == ("PGU-902", "f", "t"),
          f"Y is handed over once, W's queued comment is not delivered, and nothing repeats: {upgraded['after']}")
    check("ticket_update:stale_notification" in upgraded["dropped"],
          f"the listener drops W's notice as stale: {upgraded['dropped']}")
    check("ok" in upgraded["y_submit"], f"and Y can be submitted: {upgraded['y_submit']}")


def parity_checks(after: dict) -> None:
    # Last: a check that the copies agree must not stand in for one of behaviour.
    check(all(after["fresh_equals_migrated"].values()) and after["fresh_triggers"][0] == after["fresh_triggers"][1]
          == "tickets_z_user_review_release:ticket_board.record_user_review_release"
          and after["grants"] == "ticket_board_listener:SELECT,ticket_board_service:SELECT",
          f"a fresh schema.sql board installs the same functions, trigger and grants: "
          f"{after['fresh_equals_migrated']} {after['fresh_triggers']} {after['grants']}")


if __name__ == "__main__":
    raise SystemExit(main())
