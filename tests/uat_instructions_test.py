#!/usr/bin/env python3
"""SYRD-629: the User's UAT instructions are current ticket data, and User Review needs them.

The User opened tickets in User Review and could not see what to do: the steps
were buried in comments, so they asked the Director to restate them. Now the
Director prepares three short parts -- Do this, You should see, Then choose --
as data on the ticket, and the board refuses to put a ticket in User Review
without them, by any path. They are written for the ticket's commit and stop
being current when the ticket leaves User Review without the User's acceptance
or goes back to implementation, so a returned or resubmitted ticket reaches the
User only with instructions replaced or reconfirmed. The User's question
(Ask Director) is an attributed comment that leaves the ticket where it is and
tells the Director exactly once.

Driven through the real HTTP handler against a real cluster: on a board with
the shipped declared workflow, and on a legacy board.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()
import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board import uat_instructions  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SHIPPED = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
A, B = "a" * 40, "b" * 40
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def board(cluster, db: str, *, first: bool):
    root, sock, port = cluster.root, cluster.socket_dir, cluster.port
    admin = t.conninfo(sock, port, db)
    t.run(["createdb", "-h", str(sock), "-p", str(port), "-U", "postgres", db])
    t.psql(admin, t.SCHEMA_PATH.read_text())
    if first:  # roles are the cluster's, not the database's
        t.create_roles(admin)
    t.psql(admin, t.RBAC_PATH.read_text())
    (root / db / "frames").mkdir(parents=True, exist_ok=True)
    (root / db / "assets").mkdir(parents=True, exist_ok=True)
    app = t.TicketBoardApp(root / db / "frames", root / db / "assets", project="cerulean", ticket_prefix="PGU",
                           database_url=t.conninfo(sock, port, db, t.SERVICE_ROLE))
    server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
    t.TEST_WRITE_TOKEN = server.write_token
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return admin, app, server, f"http://127.0.0.1:{server.server_port}"


class Board:
    def __init__(self, admin: str, app, server, base: str) -> None:
        self.admin, self.app, self.server, self.base = admin, app, server, base

    def sql(self, query: str) -> str:
        return t.psql(self.admin, query)

    def act(self, ticket_id: str, operation: str, actor: str, expect: int = 200, **payload):
        t.TEST_WRITE_TOKEN = self.server.write_token
        return t.post_json(self.base, f"/api/tickets/{ticket_id}/actions/{operation}", payload, caller=actor,
                           expect=expect)

    def setup(self, statement: str) -> str:
        """A fixture's own write, past the board's triggers: it arranges a ticket, it is not a move under test."""
        return self.sql(f"SET session_replication_role = replica; {statement} SET session_replication_role = origin;")

    def ticket(self, ticket_id: str) -> dict:
        return self.app.get_ticket(ticket_id)

    def queued(self, ticket_id: str) -> list[dict]:
        return json.loads(self.sql(
            "SELECT coalesce(jsonb_agg(jsonb_build_object('role',target_role,'kind',kind,'payload',payload,"
            f"'message',message) ORDER BY id),'[]') FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';"))

    def deliverable(self, ticket_id: str) -> bool:
        """The listener's own currentness check, as delivery asks it, of the ticket's latest queued notice."""
        import psycopg
        from scripts.ticket_board import notify_listener

        listener_url = self.admin.replace("user=postgres", "user=ticket_board_listener")
        row = self.sql("SELECT ticket_id || '|' || target_role || '|' || payload::text FROM ticket_board.ticket_notification_queue "
                       f"WHERE ticket_id='{ticket_id}' ORDER BY id DESC LIMIT 1;").split("|", 2)
        listener = notify_listener.TicketBoardNotifyListener(
            conninfo=listener_url, project="cerulean", sender=lambda *_a: None, activity_gate=lambda _t: True,
            target_exists=lambda _t: True, poll_seconds=0)
        with psycopg.connect(listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            return listener.eligibility._notification_is_current(conn, row[0], row[1], row[2])

    def drain(self, ticket_id: str) -> None:
        self.sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id='{ticket_id}';")

    def comments(self, ticket_id: str) -> list[dict]:
        return json.loads(self.sql(
            "SELECT coalesce(jsonb_agg(jsonb_build_object('who',who,'text',text) ORDER BY position),'[]') "
            f"FROM ticket_board.ticket_comments WHERE ticket_id='{ticket_id}';"))

    def at_dat(self, ticket_id: str, *, commit: str = A, no_code: bool = False) -> None:
        """A candidate Audit passed, at the Director's acceptance, on its way to the User."""
        self.sql(f"DELETE FROM ticket_board.tickets WHERE id='{ticket_id}';")
        t.seed_postgres_ticket(self.admin, ticket_id, title="Tenant launch repair", state="dat", assignee="director",
                               needs_audit=True, audit_signoff=True, needs_user_signoff=True,
                               commit_hash="" if no_code else commit, commit_exempt=no_code)
        self.sql("UPDATE ticket_board.ticket_notification_state "
                 f"SET last_implementer_assignee='main' WHERE ticket_id='{ticket_id}';")

    def prepare(self, ticket_id: str, *, do: str = "Run `switchyard status otto` in a terminal.",
                see: str = "The line `otto: running` with no error.", then: str = "", expect: int = 200,
                actor: str = "director", reconfirm: bool = False):
        return self.act(ticket_id, "set_uat_instructions", actor, expect=expect, do_this=do, you_should_see=see,
                        then_choose=then, reconfirm=reconfirm)


def instructions_rules(b: Board, phase: str, enter_review: str) -> None:
    """What the Director can write, and the guard on the way into User Review."""
    b.at_dat("PGU-1")
    refused = b.act("PGU-1", enter_review, "director", expect=400)
    check("UAT instructions required" in str(refused) and "none were prepared" in str(refused)
          and "ticket-board-write uat-instructions PGU-1" in str(refused),
          f"{phase}: User Review is refused without instructions, and says how to prepare them: {refused}")
    check(b.ticket("PGU-1")["state"] == "dat", f"{phase}: and nothing moved")

    # Only the Director prepares them.
    for actor in ("user", "main", "audit"):
        denied = b.prepare("PGU-1", actor=actor, expect=403)
        check("cannot call set_uat_instructions" in str(denied), f"{phase}: {actor} may not write them: {denied}")
    # Whole, and short.
    for do, see, part in (("", "x", "Do this"), ("x", "  ", "You should see")):
        blank = b.prepare("PGU-1", do=do, see=see, expect=400)
        check(f'"{part}" is blank' in str(blank), f"{phase}: a blank part is refused, by name: {blank}")
    long = b.prepare("PGU-1", do="x" * 1501, expect=400)
    check("keep each part short" in str(long), f"{phase}: and a long one: {long}")
    nothing = b.prepare("PGU-1", do="", see="", reconfirm=True, expect=400)
    check("no UAT instructions to reconfirm" in str(nothing), f"{phase}: nothing to reconfirm yet: {nothing}")

    written = b.prepare("PGU-1")["uat"]["instructions"]
    check(written["do_this"] == "Run `switchyard status otto` in a terminal."
          and written["you_should_see"] == "The line `otto: running` with no error."
          and written["then_choose"] == uat_instructions.STANDARD_THEN_CHOOSE,
          f"{phase}: the three parts are stored, Then choose by its standard sentence: {written}")
    check(written["prepared_by"] == "director" and written["prepared_at"] and written["for_commit"] == A
          and written["current"] is True, f"{phase}: who wrote them, when, for which commit: {written}")
    moved = b.act("PGU-1", enter_review, "director")["ticket"]
    check(moved["state"] == "user_review", f"{phase}: with them it enters User Review: {moved['state']}")
    shown = b.ticket("PGU-1")["uat"]["instructions"]
    check(shown["current"] is True and shown["do_this"] == written["do_this"],
          f"{phase}: and the ticket's JSON carries them: {shown}")


def staleness(b: Board, phase: str, enter_review: str, leave: tuple[str, dict]) -> None:
    """Written for one commit; superseded by a return; a resubmission brings new ones."""
    b.at_dat("PGU-2")
    b.prepare("PGU-2", do="Open the board and pick PGU-2.", see="The UAT panel at the top.")
    b.setup("UPDATE ticket_board.tickets SET commit_hash='" + B + "' WHERE id='PGU-2';")
    other = b.act("PGU-2", enter_review, "director", expect=400)
    check(f"written for {A}" in str(other) and f"now carries {B}" in str(other),
          f"{phase}: instructions for another commit are not current: {other}")
    check(b.ticket("PGU-2")["uat"]["instructions"]["current"] is False, f"{phase}: and read as not current")
    b.prepare("PGU-2", reconfirm=True, do="", see="")
    check(b.act("PGU-2", enter_review, "director")["ticket"]["state"] == "user_review",
          f"{phase}: reconfirmed for the commit it now carries, it goes on")
    history = json.loads(b.sql("SELECT jsonb_agg(jsonb_build_object('for',for_commit,'again',reconfirmed) ORDER BY id) "
                               "FROM ticket_board.ticket_uat_instruction_history WHERE ticket_id='PGU-2';"))
    check(history == [{"for": A, "again": False}, {"for": B, "again": True}],
          f"{phase}: every version is kept: {history}")

    action, payload = leave
    b.act("PGU-2", action, "director", **payload)
    gone = b.ticket("PGU-2")["uat"]["instructions"]
    check(gone["current"] is False and gone["superseded_at"] and "without the User's acceptance" in gone["superseded_reason"],
          f"{phase}: leaving User Review undecided supersedes them: {gone}")
    b.setup(f"UPDATE ticket_board.tickets SET state='dat', assignee='director', commit_hash='{B}', audit_signoff=true "
            "WHERE id='PGU-2';")
    back = b.act("PGU-2", enter_review, "director", expect=400)
    check("stopped being current" in str(back), f"{phase}: so the resubmission needs them again: {back}")
    b.prepare("PGU-2", do="Restart otto, then run `switchyard status otto`.", see="`otto: running` again.")
    check(b.act("PGU-2", enter_review, "director")["ticket"]["state"] == "user_review",
          f"{phase}: and with the changed steps it goes on")
    check(b.ticket("PGU-2")["uat"]["instructions"]["do_this"] == "Restart otto, then run `switchyard status otto`.",
          f"{phase}: showing the new steps, not the old")


def every_path(b: Board, phase: str) -> None:
    """A Director edit and a forced move are no way round it."""
    b.at_dat("PGU-3")
    edited = b.act("PGU-3", "director_edit", "director", expect=400,
                   patch={"state": "user_review", "assignee": "user"}, reason="straight to the User")
    check("UAT instructions required" in str(edited), f"{phase}: a Director edit is refused too: {edited}")
    forced = b.act("PGU-3", "force_move", "director", expect=400, state="user_review", assignee="user")
    check("UAT instructions required" in str(forced), f"{phase}: and a forced move: {forced}")
    check(b.ticket("PGU-3")["state"] == "dat", f"{phase}: nothing moved")


def no_code(b: Board, phase: str, enter_review: str) -> None:
    b.at_dat("PGU-4", no_code=True)
    written = b.prepare("PGU-4", do="Read the new paragraph in the onboarding guide.",
                        see="It names the Accept button.")["uat"]["instructions"]
    check(written["for_commit"] == "" and written["current"] is True, f"{phase}: a no-code ticket's are for no commit")
    check(b.act("PGU-4", enter_review, "director")["ticket"]["state"] == "user_review",
          f"{phase}: and it enters User Review")


def through_the_cli(b: Board, phase: str, scratch: Path) -> None:
    """The Director's real command, as the skill and the guard's refusal print it."""
    import contextlib
    import io

    from scripts.ticket_board import write_client

    def cli(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = write_client.main(["--board-url", b.base, "--caller-role", "director",
                                          f"--write-token={b.server.write_token}", *argv])
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue()

    b.at_dat("PGU-7")
    code, out = cli("uat-instructions", "PGU-7", "--do-this", "Run `switchyard status` in a terminal.",
                    "--you-should-see", "A line for each project, each saying running.")
    saved = b.ticket("PGU-7")["uat"]["instructions"]
    check(code == 0 and saved["do_this"] == "Run `switchyard status` in a terminal."
          and saved["then_choose"] == uat_instructions.STANDARD_THEN_CHOOSE,
          f"{phase}: ticket-board-write uat-instructions writes them, Then choose by default: {code} {out[-300:]}")
    steps = scratch / "do-this.txt"
    steps.write_text("```\nswitchyard status otto\n```\nThen open the board.\n")
    code, out = cli("uat-instructions", "PGU-7", "--do-this-file", str(steps), "--you-should-see", "`otto: running`.")
    check(code == 0 and b.ticket("PGU-7")["uat"]["instructions"]["do_this"].startswith("```\nswitchyard status otto"),
          f"{phase}: and from a file, commands intact: {code} {out[-300:]}")
    code, out = cli("uat-instructions", "PGU-7", "--reconfirm", "--do-this", "something else")
    check(code == 2 and "either --reconfirm or new steps" in out, f"{phase}: --reconfirm with new text is refused: {out[-200:]}")
    code, out = cli("uat-instructions", "PGU-7", "--reconfirm")
    check(code == 0 and b.act("PGU-7", "director_dat_sign_off", "director")["ticket"]["state"] == "user_review",
          f"{phase}: --reconfirm restamps them, and the ticket goes on: {code} {out[-200:]}")


def back_before_review(b: Board, phase: str, enter_review: str) -> None:
    """Steps written before User Review stop being current when the work goes back to implementation, even with
    no commit to tell the rounds apart; and a finished ticket has no UAT to prepare."""
    b.at_dat("PGU-6", no_code=True)
    b.prepare("PGU-6", do="Read the onboarding guide's new paragraph.", see="It names the Accept button.")
    b.act("PGU-6", "force_move", "director", state="in_progress", assignee="main")
    gone = b.ticket("PGU-6")["uat"]["instructions"]
    check(gone["current"] is False and "went back to in_progress" in gone["superseded_reason"],
          f"{phase}: work back in implementation supersedes the steps: {gone}")
    b.setup("UPDATE ticket_board.tickets SET state='dat', assignee='director' WHERE id='PGU-6';")
    back = b.act("PGU-6", enter_review, "director", expect=400)
    check("went back to in_progress" in str(back), f"{phase}: so the next round needs its own: {back}")
    b.setup("UPDATE ticket_board.tickets SET state='done', assignee='director' WHERE id='PGU-6';")
    done = b.prepare("PGU-6", expect=400)
    check("there is no UAT to prepare" in str(done), f"{phase}: a finished ticket has none to prepare: {done}")


def ask_director(b: Board, phase: str) -> None:
    """The User's question: a comment, the ticket stays, the Director is told once."""
    b.drain("PGU-1")
    before = b.ticket("PGU-1")
    asked = b.act("PGU-1", "ask_director", "user", question="Which terminal: mine or the tenant's?")["uat"]
    question = asked["questions"][-1]
    check(question["asked_by"] == "user" and question["question"] == "Which terminal: mine or the tenant's?",
          f"{phase}: the question is recorded: {question}")
    after = b.ticket("PGU-1")
    check((after["state"], after["assignee"], after["user_signoff"]) == (before["state"], before["assignee"], False),
          f"{phase}: the ticket stays in User Review, undecided: {after['state']}")
    last = b.comments("PGU-1")[-1]
    check(last == {"who": "user", "text": "Question for the Director: Which terminal: mine or the tenant's?"},
          f"{phase}: as an attributed comment: {last}")
    notes = b.queued("PGU-1")
    check(len(notes) == 1 and notes[0]["role"] == "director" and notes[0]["payload"]["kind"] == "uat_question"
          and "Which terminal" in notes[0]["message"], f"{phase}: one notice, to the Director: {notes}")
    check(b.deliverable("PGU-1"), f"{phase}: and delivery sends it to the Director, though the ticket is the User's")
    b.act("PGU-1", "ask_director", "user", question="And do I restart first?")
    check(len(b.queued("PGU-1")) == 2, f"{phase}: each question is told once, none merged away")
    for bad, why in (("   ", "must say something"), ("x" * 4001, "4000 at most")):
        refused = b.act("PGU-1", "ask_director", "user", question=bad, expect=400)
        check(why in str(refused), f"{phase}: {why}: {refused}")
    denied = b.act("PGU-1", "ask_director", "main", question="not mine", expect=403)
    check("cannot call ask_director" in str(denied), f"{phase}: it is the User's: {denied}")
    # The database's own refusal, for a caller that reaches the function without the operation layer.
    try:
        b.sql("BEGIN; SET LOCAL ROLE ticket_board_service; SELECT set_config('ticket_board.caller_role', 'main', true); "
              "SELECT ticket_board.ask_director('PGU-1', 'not mine'); COMMIT;")
        direct = "ok"
    except AssertionError as exc:
        direct = str(exc)
    check("main cannot call ask_director" in direct and len(b.queued("PGU-1")) == 2,
          f"{phase}: and the database refuses it too: {direct}")
    b.at_dat("PGU-5")
    outside = b.act("PGU-5", "ask_director", "user", question="early?", expect=400)
    check("for a ticket in User Review" in str(outside), f"{phase}: and only in User Review: {outside}")


def acceptance_keeps(b: Board, phase: str, accept: tuple[str, dict]) -> None:
    action, payload = accept
    b.act("PGU-4", action, "user", **payload)
    kept = b.ticket("PGU-4")["uat"]["instructions"]
    check(kept["superseded_at"] is None, f"{phase}: the User's acceptance keeps them as the record: {kept}")


def main() -> int:
    with temporary_cluster(prefix="syrd629-", shutdown="immediate") as cluster:
        servers = []
        try:
            admin, app, server, base = board(cluster, "uat_declared", first=True)
            servers.append(server)
            b = Board(admin, app, server, base)
            revision = int(b.sql("SELECT coalesce(max(revision), 0) FROM ticket_board.workflow_configuration;"))
            t.post_json(base, "/api/tickets/actions/configure_workflow",
                        {"document": validate(json.loads(json.dumps(SHIPPED))), "expected_revision": revision},
                        caller="director")
            instructions_rules(b, "declared", "director_dat_sign_off")
            staleness(b, "declared", "director_dat_sign_off", ("director_withdraw", {"reason": "found a defect"}))
            every_path(b, "declared")
            no_code(b, "declared", "director_dat_sign_off")
            back_before_review(b, "declared", "director_dat_sign_off")
            through_the_cli(b, "declared", cluster.root)
            ask_director(b, "declared")
            acceptance_keeps(b, "declared", ("user_sign_off", {}))

            admin, app, server, base = board(cluster, "uat_legacy", first=False)
            servers.append(server)
            b = Board(admin, app, server, base)
            instructions_rules(b, "legacy", "director_dat_sign_off")
            staleness(b, "legacy", "director_dat_sign_off", ("force_move", {"state": "analysis", "assignee": "director"}))
            no_code(b, "legacy", "director_dat_sign_off")
            back_before_review(b, "legacy", "director_dat_sign_off")
            ask_director(b, "legacy")
            acceptance_keeps(b, "legacy", ("user_sign_off", {}))
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
    print(f"uat_instructions_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
