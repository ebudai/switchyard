#!/usr/bin/env python3
"""SYRD-626: a pulled author keeps its conversation into the next ticket of the same package -- and only then.

The real listener on disposable pull boards, built from schema.sql and rbac.sql
or (parity) through the real migrations. The provider is SYRD-540's recording
pane: a `/clear` starts a new session announced by SessionStart(clear), every
prompt is a turn whose end records the checkout, and its hook state is written
by the real pane hook's writer and read by the real gate. A ticket's first
hand-off either continues the pane's conversation -- no `/clear`, the ticket
bound to the same proven session, its notice prefaced with the board's one line
-- or clears as it always has. The board decides once, durably, and says why.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import ticket_context_restore_test as ctx  # noqa: F401  (hermetic environment, the recording pane)
from ticket_context_restore_test import IDENTITY, TARGETS, WORKTREE, Panes, fixture
from pull_claim_postgres_test import build as pull_build
from pull_pickup_test import Gate, proc_entry
from pull_scheduling_policy_test import pull_document
from scripts.ticket_board import context_continuation, session_context
from scripts.ticket_board.notify_listener import PaneHookStateStore, TicketBoardNotifyListener
from scripts.ticket_board.peer_identity import SessionIdentity
from temporary_cluster import temporary_cluster

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1000_syrd626_context_continuation.sql"
AUTHOR = "app"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def build(cluster, dbname: str, shape: str) -> str:
    if shape != "upgraded":
        return pull_build(cluster, dbname, shape)
    # The board as the release before this one left it, then the runner's tail and this release's grants.
    from schema_function_drift import migrations_from, rbac_before, schema_before
    admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
    fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    before = schema_before(MIGRATION)
    check("ticket_context_continuations" not in before, "the before-schema predates this change")
    fixture.psql(admin, before)
    fixture.create_roles(admin)
    fixture.psql(admin, rbac_before(MIGRATION))
    for migration in migrations_from(MIGRATION):
        fixture.psql(admin, migration.read_text())
    fixture.psql(admin, MIGRATION.read_text())  # replayed: changes nothing
    fixture.psql(admin, fixture.RBAC_PATH.read_text())
    return admin


class FlakyPanes(Panes):
    """The recording pane, which can refuse the next ticket notice once (a send that fails after the decision)."""

    fail_next_notice = False

    def __call__(self, target: str, text: str) -> None:
        if self.fail_next_notice and target == TARGETS[AUTHOR] and " -- " in text and not text.startswith("/"):
            self.fail_next_notice = False
            raise OSError("pane went away mid-send")
        super().__call__(target, text)


@contextlib.contextmanager
def board(label: str, *, shape: str = "fresh", pull: bool = True, ephemeral: bool = True):
    session_context.BIND_SECONDS = 1.0
    with temporary_cluster(prefix="syrd626.", shutdown="immediate") as cluster:
        dbname = f"c626_{label}"
        admin = build(cluster, dbname, shape)
        tmp = Path(tempfile.mkdtemp(prefix="syrd626."))
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_conn = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")
        service_conn = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE)
        try:
            w = SimpleNamespace(admin=admin, app=app, base=base, revision=0)
            w.sql = lambda statement: fixture.psql(admin, statement).strip()
            w.as_listener = lambda statement: fixture.psql(listener_conn, statement).strip()
            w.service_conn = service_conn
            w.listener_conn = listener_conn
            w.act = lambda ticket, op, actor, expect=200, **payload: fixture.post_json(
                base, f"/api/tickets/{ticket}/actions/{op}", payload, caller=actor, expect=expect)
            def attempt(ticket: str, op: str, actor: str, **payload) -> tuple[int, str]:
                """An action whose answer is the test's to judge: (status, body), never asserted here."""
                import urllib.error
                import urllib.request
                request = urllib.request.Request(
                    f"{base}/api/tickets/{ticket}/actions/{op}", data=json.dumps(payload).encode(), method="POST",
                    headers={"Content-Type": "application/json", fixture.CALLER_ROLE_HEADER: actor,
                             fixture.WRITE_TOKEN_HEADER: fixture.TEST_WRITE_TOKEN})
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return response.status, response.read().decode()
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.read().decode()
            w.attempt = attempt
            w.where = lambda ticket: w.sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")
            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
                if role["name"] == AUTHOR:
                    role["ephemeral"] = ephemeral
            if not pull:
                cfg.pop("scheduling")
            w.cfg = cfg
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            w.revision = 1
            state_dir = tmp / "state"
            w.panes = FlakyPanes(state_dir)
            w.gate = Gate(state_store=PaneHookStateStore(state_dir))
            w.gate.proc_root = tmp / "proc"
            w.gate.role_targets = dict(TARGETS)
            w.gate.busy = set()
            proc_entry(w.gate.proc_root, *IDENTITY)
            w.gate.role_identities = {AUTHOR: SessionIdentity(*IDENTITY)}
            w.panes.session_start(AUTHOR, f"{AUTHOR}-launch", "startup")

            def listener() -> TicketBoardNotifyListener:
                made = TicketBoardNotifyListener(
                    conninfo=listener_conn, sender=w.panes, activity_gate=w.gate.is_working, target_exists=lambda _t: True,
                    submission_witness=lambda *_a: True, poll_seconds=0, session_clear_settle_seconds=0,
                    project="pgu", logger=logging.getLogger("syrd626"))
                made.role_targets = dict(TARGETS)
                return made

            w.listener = listener()

            def restart() -> None:
                w.listener = listener()
            w.restart = restart

            def settle(passes: int = 6) -> None:
                for _ in range(passes):
                    # The cap counts the listener's deliveries over its life: ten more each pass.
                    w.listener.listen_once(max_notifications=w.listener.delivered_count + 10)
                    time.sleep(0.2)
            w.settle = settle

            def seed(ticket: str, parent: str = "") -> None:
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
                if parent:
                    w.sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET parent_id='{parent}' WHERE id='{ticket}';")
            w.seed = seed

            def take(ticket: str, parent: str = "", *, seeded: bool = False) -> list[str]:
                """The ticket admitted, pulled by the author and handed over: what the author's pane was sent."""
                mark = len(w.panes.to(AUTHOR))
                if not seeded:
                    seed(ticket, parent)
                if pull:
                    w.act(ticket, "admit", "director")
                else:  # no pull policy: the Director starts it on the author (a self-claim notifies nobody)
                    w.act(ticket, "admit", "director")
                    w.act(ticket, "director_start", "director", assignee=AUTHOR)
                settle()
                time.sleep(session_context.BIND_SECONDS + 0.2)
                settle(2)  # the binding of a cleared ticket's session
                return w.panes.to(AUTHOR)[mark:]
            w.take = take

            def release(ticket: str) -> None:
                w.sql(f"SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET commit_exempt = true WHERE id='{ticket}';")
                w.act(ticket, "submit_to_inspection", AUTHOR)
                if w.where(ticket).startswith("inspection/"):
                    w.act(ticket, "inspector_sign_off", "inspector")
                w.act(ticket, "audit_sign_off", "audit", text="Audited.")
                check(not w.where(ticket).startswith(("in_progress/", "audit/")), f"{ticket} passed Audit: {w.where(ticket)}")
            w.release = release

            def decision(ticket: str) -> dict | None:
                text = w.sql("SELECT coalesce(json_agg(to_jsonb(k)), '[]')::text FROM ticket_board.ticket_context_continuations k "
                             f"WHERE ticket_id='{ticket}';")
                rows = json.loads(text)
                check(len(rows) <= 1, f"{ticket}: one decision at most: {rows}")
                return rows[0] if rows else None
            w.decision = decision
            w.context = lambda ticket: json.loads(w.sql(
                "SELECT coalesce((SELECT to_jsonb(x)::text FROM ticket_board.ticket_role_contexts x "
                f"WHERE ticket_id='{ticket}' AND role='{AUTHOR}'), 'null');"))
            # What the Director is told, as the board enqueued it (the Director's pane is not part of these fixtures).
            w.notices = lambda: json.loads(w.sql(
                "SELECT coalesce(json_agg(detail->'payload'->>'message' ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                "WHERE event = 'enqueue' AND target_role = 'director' AND detail->>'dedupe_key' LIKE 'ticket-context-continuation:%';"))
            w.ticket = lambda ticket: app.get_ticket(ticket)
            yield w
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp, ignore_errors=True)


def note(ticket: str, previous: str, root: str) -> str:
    return context_continuation.NOTE.format(ticket=ticket, previous=previous, root=root)


def assert_continued(w, sent: list[str], ticket: str, previous: str, root: str, ancestor: str, label: str) -> None:
    check("/clear" not in sent, f"{label}: no clear: {sent}")
    handed = [text for text in sent if f"{ticket} --" in text]
    check(len(handed) == 1 and handed[0].startswith(note(ticket, previous, root) + "\n\n" + f"{ticket} --"),
          f"{label}: the ticket arrives once, in the same conversation, with the board's line first: {sent}")
    k = w.decision(ticket)
    check(k and k["decision"] == "continue" and k["from_ticket"] == previous and k["shared_root"] == root
          and k["shared_ancestor"] == ancestor and k["applied_at"] and not k["ambiguous"],
          f"{label}: the decision names where it came from and the hierarchy they share: {k}")
    before, after = w.context(previous), w.context(ticket)
    check(after and after["state"] == "bound" and after["session_id"] == before["session_id"]
          and after["evidence"]["continued_from"] == previous
          and after["evidence"]["session_start"] == before["evidence"]["session_start"],
          f"{label}: bound to the same proven session and its worktree evidence: {before} -> {after}")
    check(w.as_listener(f"SELECT ticket_board.role_session_clear_pending('{ticket}', '{AUTHOR}');") == "f",
          f"{label}: its first hand-off is done, so no later notice of it clears: SYRD-135's own question says so")
    shown = w.ticket(ticket)["context_continuation"]
    check(shown and [d["decision"] for d in shown["decisions"]] == ["continue"] and shown["decisions"][0]["from_ticket"] == previous,
          f"{label}: the ticket's JSON says so: {shown}")


def assert_fresh(w, sent: list[str], ticket: str, why: str, label: str, *, ambiguous: bool = False) -> None:
    check(sent and sent[0] == "/clear" and any(text.startswith(f"{ticket} --") for text in sent),
          f"{label}: cleared, then handed over as always: {sent}")
    k = w.decision(ticket)
    check(k is not None and k["decision"] == "fresh" and why in k["reason"] and k["ambiguous"] is ambiguous
          and not k["applied_at"], f"{label}: a fresh decision with its reason ({why!r}): {k}")


def test_the_next_ticket_of_a_package_continues_and_unrelated_work_clears() -> None:
    with board("family") as w:
        sent = w.take("PGU-1")
        assert_fresh(w, sent, "PGU-1", "has held no earlier ticket's conversation", "the first ticket")
        first = w.context("PGU-1")
        check(first and first["session_id"] == "app-s1", f"bound to its clear's session: {first}")
        w.release("PGU-1")
        for ticket, parent, previous, root, ancestor, label in (
                ("PGU-2", "PGU-1", "PGU-1", "PGU-1", "PGU-1", "parent -> child"),
                ("PGU-3", "PGU-1", "PGU-2", "PGU-1", "PGU-1", "sibling -> sibling"),
                ("PGU-4", "PGU-3", "PGU-3", "PGU-1", "PGU-3", "child -> grandchild"),
                ("PGU-5", "PGU-2", "PGU-4", "PGU-1", "PGU-1", "cousins, a deeper shared ancestor")):
            assert_continued(w, w.take(ticket, parent), ticket, previous, root, ancestor, label)
            w.release(ticket)
        check(w.panes.to(AUTHOR).count("/clear") == 1, f"one clear for the whole package: {w.panes.to(AUTHOR)}")
        # An unrelated ticket: the ordinary clear, a new session.
        assert_fresh(w, w.take("PGU-6"), "PGU-6", "share no parent hierarchy", "unrelated")
        w.release("PGU-6")
        # The package resumed after the interruption: interrupted, so fresh -- and its next child continues from it.
        assert_fresh(w, w.take("PGU-7", "PGU-1"), "PGU-7", "PGU-7 and PGU-6 share no parent hierarchy", "interrupted")
        w.release("PGU-7")
        # Child first, then its parent; and a grandchild's grandparent.
        w.seed("PGU-8")
        w.seed("PGU-9", "PGU-8")
        w.seed("PGU-10", "PGU-9")
        assert_fresh(w, w.take("PGU-10", seeded=True), "PGU-10", "share no parent hierarchy", "a new package's grandchild")
        w.release("PGU-10")
        assert_continued(w, w.take("PGU-8", seeded=True), "PGU-8", "PGU-10", "PGU-8", "PGU-8", "grandchild -> grandparent")
        w.release("PGU-8")
        assert_continued(w, w.take("PGU-9", seeded=True), "PGU-9", "PGU-8", "PGU-8", "PGU-8", "parent -> its own child")
        w.release("PGU-9")
        # Unparented work, twice; then a ticket linked to the last one only by a blocker.
        assert_fresh(w, w.take("PGU-11"), "PGU-11", "share no parent hierarchy", "unparented")
        w.release("PGU-11")
        w.seed("PGU-12")
        w.sql("INSERT INTO ticket_board.ticket_blockers(ticket_id, blocker_ticket_id, position, resolved) "
              "VALUES ('PGU-11', 'PGU-12', 0, false);")  # PGU-11's later work waits on PGU-12: a blocker, not a parent
        assert_fresh(w, w.take("PGU-12", seeded=True), "PGU-12", "PGU-12 and PGU-11 share no parent hierarchy",
                     "linked only by a blocker")
        check(not w.notices(), f"ordinary fresh decisions tell nobody: {w.notices()}")
        check(int(w.sql("SELECT count(*) FROM ticket_board.ticket_context_continuations;")) == 12,
              "one decision per ticket handed over")


def test_a_pane_that_moved_on_starts_fresh() -> None:
    with board("mismatch") as w:
        w.take("PGU-1")
        w.release("PGU-1")
        # Its worktree is elsewhere now.
        w.panes.turn_cwd[AUTHOR] = "/work/elsewhere"
        w.panes.turn_end(AUTHOR)
        assert_fresh(w, w.take("PGU-2", "PGU-1"), "PGU-2", "not PGU-1's worktree /work/app", "another worktree")
        w.panes.turn_cwd.pop(AUTHOR)
        w.panes.turn_end(AUTHOR)  # its next turn ends back in its worktree
        w.release("PGU-2")
        # Back in its worktree, the package's next ticket continues from PGU-2's own session.
        assert_continued(w, w.take("PGU-3", "PGU-1"), "PGU-3", "PGU-2", "PGU-1", "PGU-1", "back in its worktree")
        w.release("PGU-3")
        # The pane announced another session (a manual resume).
        w.panes.session_start(AUTHOR, "app-elsewhere", "resume")
        assert_fresh(w, w.take("PGU-4", "PGU-1"), "PGU-4", "is in session app-elsewhere, not PGU-3's", "another session")
        w.release("PGU-4")
        # Its own session, announced by another process than the registered one.
        session = w.context("PGU-4")["session_id"]
        w.panes.session_start(AUTHOR, session, "resume", identity=(9999, 1))
        assert_fresh(w, w.take("PGU-5", "PGU-1"), "PGU-5", "another process than the registered provider", "another process")
        w.release("PGU-5")
        # The role now runs another runtime.
        for role in w.cfg["roles"]:
            if role["name"] == AUTHOR:
                role["runtime"] = "codex"
        w.app.apply_workflow(w.cfg, expected_revision=w.revision, dry_run=False, caller_role="director")
        assert_fresh(w, w.take("PGU-6", "PGU-1"), "PGU-6", "app now runs codex, and PGU-5's conversation was on claude",
                     "another runtime")


def test_the_release_gate_and_the_directors_fresh_context() -> None:
    with board("gate") as w:
        w.take("PGU-1")
        # Withdrawn before its approval: it never passed the release gate.
        w.act("PGU-1", "defer", "director", reason="Not now.")
        assert_fresh(w, w.take("PGU-2", "PGU-1"), "PGU-2", "PGU-1 has not passed its release gate (audit)", "no release")
        w.release("PGU-2")
        # The Director's package boundary, set before the hand-off.
        w.seed("PGU-3", "PGU-1")
        code, body = w.attempt("PGU-3", "force_fresh_context", AUTHOR, reason="mine")
        check(code == 403 and f"{AUTHOR} cannot call force_fresh_context" in body,
              f"an implementer cannot ask for it, refused by the role policy: {code} {body}")
        refused = subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", w.service_conn], capture_output=True, text=True,
                                 input=f"SET ticket_board.caller_role = '{AUTHOR}'; "
                                       "SELECT ticket_board.request_fresh_ticket_context('PGU-3', 'mine');")
        check(refused.returncode != 0 and w.sql("SELECT count(*) FROM ticket_board.ticket_context_fresh_requests;") == "0",
              f"and by the database itself: {refused.stderr.strip()[:200]}")
        code, body = w.attempt("PGU-3", "force_fresh_context", "director", reason="  ")
        check(code == 400 and "needs a reason" in body, f"it takes a reason: {code} {body}")
        asked = w.act("PGU-3", "force_fresh_context", "director", reason="PGU-3 starts the second package.")
        check(asked["ticket_id"] == "PGU-3" and asked["requested_by"] == "director", f"recorded: {asked}")
        check(w.ticket("PGU-3")["context_continuation"]["fresh_requested"]["reason"] == "PGU-3 starts the second package.",
              f"and shown on the ticket before its hand-off: {w.ticket('PGU-3')['context_continuation']}")
        assert_fresh(w, w.take("PGU-3", seeded=True), "PGU-3", "the Director asked for a fresh context (director): "
                     "PGU-3 starts the second package.", "the Director's fresh context wins")
        code, body = w.attempt("PGU-3", "force_fresh_context", "director", reason="again")
        check(code == 400 and "already decided" in body and "director starts" not in body,
              f"after the decision it is refused, not silently ignored: {code} {body}")
        check(w.ticket("PGU-3")["context_continuation"]["fresh_requested"]["reason"] == "PGU-3 starts the second package.",
              "and the request on record is the one that was decided on")
        w.release("PGU-3")
        # The CLI the Director uses reaches the same operation.
        w.seed("PGU-4", "PGU-1")
        from scripts.ticket_board import write_client
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):  # an explicit --board-url: never the default socket
            code = write_client.main(["--board-url", w.base, "--caller-role", "director",
                                      f"--write-token={fixture.TEST_WRITE_TOKEN}",
                                      "force-fresh-context", "PGU-4", "--reason", "boundary"])
        check(code == 0 and json.loads(out.getvalue())["reason"] == "boundary", f"ticket-board-write force-fresh-context: {out.getvalue()}")
        assert_fresh(w, w.take("PGU-4", seeded=True), "PGU-4", "the Director asked for a fresh context", "from the CLI")


def test_returned_work_follows_its_saved_conversation() -> None:
    with board("rework") as w:
        w.take("PGU-1")
        w.release("PGU-1")
        w.take("PGU-2", "PGU-1")  # continued: PGU-1 and PGU-2 share app-s1
        # PGU-1 comes back from Final Sign-Off while its conversation is still the pane's: delivered as it is.
        for stage, step, actor in (("dat", "director_dat_sign_off", "director"), ("user_review", "user_sign_off", "user")):
            if w.where("PGU-1").startswith(stage + "/"):
                w.act("PGU-1", step, actor, text="Accepted.")
        w.act("PGU-1", "director_kick_back", "director", reason="Final Sign-Off found a gap.")
        w.release("PGU-2")
        mark = len(w.panes.to(AUTHOR))
        w.settle(8)
        back = w.panes.to(AUTHOR)[mark:]
        check(any(text.startswith("PGU-1 --") for text in back) and "/clear" not in back
              and not any(text.startswith("/resume") for text in back) and not any(text.startswith("Board: PGU-1 follows") for text in back),
              f"the rework is SYRD-540's: the pane still holds its conversation, so it is handed back as it is: {back}")
        check(int(w.sql("SELECT count(*) FROM ticket_board.ticket_context_continuations WHERE ticket_id='PGU-1';")) == 1,
              "and no second continuation decision is made for returned work")
        w.release("PGU-1")
        # Unrelated work clears; then PGU-2 comes back: superseded, so SYRD-540 resumes the continued session.
        w.take("PGU-3")
        for stage, step, actor in (("dat", "director_dat_sign_off", "director"), ("user_review", "user_sign_off", "user")):
            if w.where("PGU-2").startswith(stage + "/"):
                w.act("PGU-2", step, actor, text="Accepted.")
        w.act("PGU-2", "director_kick_back", "director", reason="Another gap.")
        check(w.sql("SELECT ticket_board.ticket_context_superseded('PGU-2', 'app');") == "t",
              "a continued ticket is superseded by the clear that came after it")
        w.release("PGU-3")
        mark = len(w.panes.to(AUTHOR))
        w.settle(10)
        time.sleep(session_context.CONFIRM_SECONDS + 0.3)
        w.settle(6)
        back = w.panes.to(AUTHOR)[mark:]
        handed = [i for i, text in enumerate(back) if text.startswith("PGU-2 --")]
        check("/resume app-s1" in back and "/clear" not in back and handed and back.index("/resume app-s1") < handed[0],
              f"PGU-2's saved conversation -- the one it was continued in -- is restored before it is handed back: {back}")


def test_retries_restarts_duplicates_and_crash_windows_decide_once() -> None:
    with board("windows") as w:
        w.take("PGU-1")
        w.release("PGU-1")
        # Decided, then the binding fails (the database refuses it): nothing is sent, the notice waits.
        w.sql("REVOKE EXECUTE ON FUNCTION ticket_board.apply_ticket_context_continuation(text, text) FROM ticket_board_listener;")
        w.seed("PGU-2", "PGU-1")
        mark = len(w.panes.to(AUTHOR))
        w.act("PGU-2", "admit", "director")
        w.settle(3)
        check(w.panes.to(AUTHOR)[mark:] == [] and w.decision("PGU-2")["decision"] == "continue"
              and not w.decision("PGU-2")["applied_at"], f"decided, not bound, nothing sent: {w.panes.to(AUTHOR)[mark:]}")
        decided = w.decision("PGU-2")["decided_at"]
        w.sql("GRANT EXECUTE ON FUNCTION ticket_board.apply_ticket_context_continuation(text, text) TO ticket_board_listener;")
        due = "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() WHERE target_role = 'app';"
        w.sql(due)  # its retry delay has passed
        # The listener restarts, and the ticket notice fails once after the binding.
        w.restart()
        w.panes.fail_next_notice = True
        w.settle(3)
        w.sql(due)
        k = w.decision("PGU-2")
        check(k["decided_at"] == decided and k["applied_at"], f"the same decision, now bound: {k}")
        applied = k["applied_at"]
        # A duplicate hand-off notice for the same round.
        payload = w.sql("SELECT payload::text FROM ticket_board.ticket_notification_queue WHERE ticket_id='PGU-2' AND kind='transition' "
                        "ORDER BY id DESC LIMIT 1;")
        if payload:
            w.sql("SELECT ticket_board.enqueue_notification('PGU-2', 'transition', 'app', 'PGU-2 -- duplicate notice', "
                  f"$p${payload}$p$::jsonb, 'syrd626-duplicate');")
        w.restart()
        w.settle(6)
        sent = w.panes.to(AUTHOR)[mark:]
        check("/clear" not in sent and sent and sent[0].startswith(note("PGU-2", "PGU-1", "PGU-1")),
              f"never a clear, and the notice that arrives carries the line: {sent}")
        check(w.decision("PGU-2")["applied_at"] == applied and w.decision("PGU-2")["decided_at"] == decided,
              "decided once, bound once, through a failed bind, a restart, a failed send and a duplicate")
        check(w.context("PGU-2")["session_id"] == w.context("PGU-1")["session_id"], "in PGU-1's session")
        failed = int(w.sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event='context_continuation_failed' "
                           "AND ticket_id='PGU-2';"))
        continued = int(w.sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event='context_continuation' "
                              "AND ticket_id='PGU-2';"))
        check(failed >= 1 and continued == 1, f"the failed bind and the one continuation are traced: {failed} {continued}")
        # A later round of the same ticket (a kickback) arrives without the line.
        w.act("PGU-2", "submit_to_inspection", AUTHOR)
        if w.where("PGU-2").startswith("inspection/"):
            w.act("PGU-2", "inspector_sign_off", "inspector")
        w.act("PGU-2", "audit_kick_back", "audit", reason="Not yet.")
        mark = len(w.panes.to(AUTHOR))
        w.settle(4)
        again = [text for text in w.panes.to(AUTHOR)[mark:] if text.startswith(("PGU-2 --", "Board: PGU-2 follows"))]
        check(again and not any(text.startswith("Board:") for text in again), f"another round, no line: {again}")


def test_unproven_continuity_starts_fresh_and_tells_the_director_once() -> None:
    with board("unproven") as w:
        w.panes.clear_announces = False  # PGU-1's clear is never announced: its conversation is never confirmed
        w.take("PGU-1")
        check(w.context("PGU-1")["state"] == "unconfirmed", f"PGU-1 unconfirmed: {w.context('PGU-1')}")
        w.panes.clear_announces = True
        w.release("PGU-1")
        assert_fresh(w, w.take("PGU-2", "PGU-1"), "PGU-2", "PGU-1's conversation was never confirmed", "unconfirmed",
                     ambiguous=True)
        w.settle(3)
        told = [text for text in w.notices() if text.startswith("app starts PGU-2 ")]
        check(len(told) == 1 and "could not be proven" in told[0] and "Nothing was carried over" in told[0],
              f"the Director is told once, with why: {w.notices()}")
        w.release("PGU-2")
        # A parent link that names a ticket this board does not have.
        w.seed("PGU-3")
        w.sql("SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET parent_id='PGU-99' WHERE id='PGU-3';")
        assert_fresh(w, w.take("PGU-3", seeded=True), "PGU-3", "names parent PGU-99, which is not on this board",
                     "a dangling parent", ambiguous=True)
        w.release("PGU-3")
        # The check itself failing: fresh, logged and traced.
        w.sql("REVOKE EXECUTE ON FUNCTION ticket_board.decide_ticket_context_continuation(text, text, jsonb) FROM ticket_board_listener;")
        sent = w.take("PGU-4", "PGU-2")
        check(sent and sent[0] == "/clear" and w.decision("PGU-4") is None
              and int(w.sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event='context_continuation_failed' "
                            "AND ticket_id='PGU-4';")) == 1,
              f"a check that fails is a fresh start, said once: {sent}")
        check(len([text for text in w.notices() if text.startswith("app starts PGU-2 ")]) == 1
              and len([text for text in w.notices() if text.startswith("app starts PGU-3 ")]) == 1
              and not any(text.startswith("app starts PGU-4 ") for text in w.notices()),
              f"each unproven decision told once, and the failed check is not a decision: {w.notices()}")


def test_boards_without_a_pull_policy_and_other_roles_are_unchanged() -> None:
    with board("nopull", pull=False) as w:
        check(w.as_listener("SELECT ticket_board.decide_ticket_context_continuation('PGU-1', 'app', '{}'::jsonb) IS NULL;") == "t",
              "no pull policy: nothing is decided")
        sent = w.take("PGU-1")
        check(sent and sent[0] == "/clear", f"an ephemeral role clears as before: {sent}")
        # Without a pull policy a reviewed ticket keeps holding its implementer; free it the Director's way.
        w.act("PGU-1", "defer", "director", reason="Making room.")
        sent = w.take("PGU-2", "PGU-1")
        check(sent and sent[0] == "/clear" and not any(text.startswith("Board:") for text in sent),
              f"its child clears too, with no line: {sent}")
        check(w.sql("SELECT count(*) FROM ticket_board.ticket_context_continuations;") == "0", "and no decision is recorded")
    with board("durable", pull=False, ephemeral=False) as w:
        sent = w.take("PGU-1")
        check("/clear" not in sent and any(text.startswith("PGU-1 --") for text in sent), f"a durable role is never cleared: {sent}")
        check(w.sql("SELECT count(*) FROM ticket_board.ticket_context_continuations;") == "0", "nor decided for")


def test_the_decision_rules_one_by_one() -> None:
    """Each rule on the board's own function, from a pane already in PGU-1's proven conversation."""
    with board("rules") as w:
        w.take("PGU-1")
        w.release("PGU-1")
        bound = w.context("PGU-1")
        good = {"runtime": "claude", "session_id": bound["session_id"], "registered_process": True,
                "checkout": {"cwd": WORKTREE[AUTHOR], "head": "x"}}

        def decide(ticket: str, observed: dict, parent: str = "PGU-1", *, setup: str = "") -> dict:
            w.seed(ticket, parent)
            w.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET state='in_progress', assignee='app' "
                  f"WHERE id='{ticket}';")  # a fixture, not a move: the workflow's triggers are not under test here
            if setup:
                w.sql(setup)
            result = json.loads(w.as_listener("SELECT ticket_board.decide_ticket_context_continuation("
                                              f"'{ticket}', 'app', $j${json.dumps(observed)}$j$::jsonb)::text;"))
            again = json.loads(w.as_listener("SELECT ticket_board.decide_ticket_context_continuation("
                                             f"'{ticket}', 'app', '{{}}'::jsonb)::text;"))
            check(result["recorded"] and not again["recorded"] and again["decision"] == result["decision"]
                  and again["reason"] == result["reason"], f"{ticket}: decided once, the same answer after: {result} {again}")
            return result

        for n, (field, value, why) in enumerate((
                ("runtime", None, "did not report what proves it still holds PGU-1's conversation: its runtime"),
                ("session_id", None, "its session"), ("registered_process", None, "which process announced it"),
                ("checkout", None, "its worktree"), ("checkout", {"cwd": ""}, "its worktree"))):
            got = decide(f"PGU-{20 + n}", {**good, field: value})
            check(got["decision"] == "fresh" and got["ambiguous"] and why in got["reason"], f"missing {field}: {got}")
        check(decide("PGU-30", good)["decision"] == "continue", "the same facts, complete, continue")
        got = decide("PGU-31", good, setup="SET session_replication_role = replica; "
                     "UPDATE ticket_board.tickets SET state='in_progress', assignee='app' WHERE id='PGU-1'; "
                     "DELETE FROM ticket_board.pull_releases WHERE ticket_id='PGU-1';")  # as re-entering implementation does
        check(got["decision"] == "fresh" and "PGU-1 has not passed its release gate (audit)" in got["reason"],
              f"PGU-1 back in implementation is not released: {got}")
        w.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET state='done', assignee='director' WHERE id='PGU-1';")
        w.sql("INSERT INTO ticket_board.pull_releases(ticket_id, action) VALUES ('PGU-1', 'audit_sign_off') ON CONFLICT DO NOTHING;")
        # Returned work of the same author waiting for SYRD-540.
        w.seed("PGU-40")
        w.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET state='in_progress', assignee='app' WHERE id='PGU-40';"
              "INSERT INTO ticket_board.ticket_role_contexts(ticket_id, role, runtime, session_id, state) "
              "VALUES ('PGU-40', 'app', 'claude', 'app-old', 'bound');")
        got = decide("PGU-32", good)
        check(got["decision"] == "fresh" and "PGU-40 is returned work of app" in got["reason"], f"rework first: {got}")
        w.sql("SET session_replication_role = replica; UPDATE ticket_board.tickets SET state='done', assignee='director' WHERE id='PGU-40';")
        w.sql("INSERT INTO ticket_board.ticket_context_restores(ticket_id, role, session_id, reason) VALUES ('PGU-1', 'app', 'x', 'rework');")
        got = decide("PGU-33", good)
        check(got["decision"] == "fresh" and "PGU-1 is returned work" in got["reason"], f"an open restore too: {got}")
        w.sql("DELETE FROM ticket_board.ticket_context_restores;")
        # Not assigned to this role in implementation.
        w.seed("PGU-34", "PGU-1")
        got = json.loads(w.as_listener(f"SELECT ticket_board.decide_ticket_context_continuation('PGU-34', 'app', $j${json.dumps(good)}$j$::jsonb)::text;"))
        check(got["decision"] == "fresh" and "not assigned to app in implementation" in got["reason"], f"not assigned: {got}")
        # A loop in the parent links, between tickets that otherwise share nothing.
        w.seed("PGU-35")
        w.seed("PGU-36", "PGU-35")
        w.sql("SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET parent_id='PGU-36' WHERE id='PGU-35';")
        got = decide("PGU-37", good, parent="PGU-36")
        check(got["decision"] == "fresh" and got["ambiguous"] and "loop back to" in got["reason"], f"a loop: {got}")
        lineage = json.loads(w.sql("SELECT ticket_board.ticket_lineage('PGU-36')::text;"))
        check(lineage == {"chain": ["PGU-36", "PGU-35"], "problem": "the parent links of PGU-36 loop back to PGU-36"},
              f"the chain stops where it loops: {lineage}")
        # Who may ask: only the listener decides, binds or reads the note; only the Director asks for fresh.
        import subprocess
        for statement in ("SELECT ticket_board.decide_ticket_context_continuation('PGU-1', 'app', '{}'::jsonb);",
                          "SELECT ticket_board.apply_ticket_context_continuation('PGU-1', 'app');",
                          "SELECT ticket_board.ticket_context_continuation_note('PGU-1', 'app');",
                          "SET ticket_board.caller_role = 'app'; SELECT ticket_board.request_fresh_ticket_context('PGU-1', 'x');"):
            run = subprocess.run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", w.service_conn], input=statement,
                                 capture_output=True, text=True)
            check(run.returncode != 0 and ("permission denied" in run.stderr or "listener" in run.stderr
                                           or "cannot call" in run.stderr or "not allowed" in run.stderr),
                  f"refused to the service as {statement[:60]!r}: {run.stderr.strip()[:200]}")


def test_a_fresh_request_and_a_first_decision_are_serialized() -> None:
    """Audit's interleaving (SYRD-626): a Director request and the listener's decision in flight at once.

    Each locks the ticket's row before reading the other's record, so whichever
    commits first decides, and the other sees it: a request that came first makes
    the decision fresh, and a decision that came first refuses the request.
    """
    for shape in ("fresh", "migrated"):  # schema.sql's copy, then the migration's (what an upgraded board runs)
        serialized_on(shape)


def serialized_on(shape: str) -> None:
    import threading
    import psycopg

    with board(f"serial_{shape}", shape=shape) as w:
        w.take("PGU-1")
        w.release("PGU-1")
        bound = w.context("PGU-1")
        good = json.dumps({"runtime": "claude", "session_id": bound["session_id"], "registered_process": True,
                           "checkout": {"cwd": WORKTREE[AUTHOR], "head": "x"}})
        listener = w.listener_conn
        service = w.service_conn

        def candidate(ticket: str) -> None:
            w.seed(ticket, "PGU-1")
            w.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET state='in_progress', assignee='app' "
                  f"WHERE id='{ticket}';")  # a fixture: the ticket handed to app, about to be decided

        def decide(conn, ticket: str) -> dict:
            return json.loads(conn.execute("SELECT ticket_board.decide_ticket_context_continuation(%s, 'app', %s::jsonb)::text",
                                           (ticket, good)).fetchone()[0])

        def request(conn, ticket: str):
            conn.execute("SELECT set_config('ticket_board.caller_role', 'director', true)")
            return conn.execute("SELECT ticket_board.request_fresh_ticket_context(%s, 'package boundary')::text",
                                (ticket,)).fetchone()[0]

        def outcome(ticket: str) -> tuple[int, str | None]:
            requests = int(w.sql(f"SELECT count(*) FROM ticket_board.ticket_context_fresh_requests WHERE ticket_id='{ticket}';"))
            k = w.decision(ticket)
            return requests, (k or {}).get("decision")

        def in_thread(call) -> tuple[threading.Thread, dict]:
            box: dict = {}

            def run() -> None:
                try:
                    box["value"] = call()
                except Exception as exc:  # the refusal is a result here
                    box["error"] = str(exc)
            thread = threading.Thread(target=run)
            thread.start()
            return thread, box

        # 1. The Director's request first, still uncommitted: the decision waits for it, then is fresh.
        candidate("PGU-2")
        with psycopg.connect(service) as director, psycopg.connect(listener, autocommit=True) as pane:
            request(director, "PGU-2")
            thread, box = in_thread(lambda: decide(pane, "PGU-2"))
            thread.join(1.0)
            check(thread.is_alive(), f"{shape}: the decision waits while the request is in flight")
            director.commit()
            thread.join(10)
        check(not thread.is_alive() and box.get("value", {}).get("decision") == "fresh"
              and "the Director asked for a fresh context" in box["value"]["reason"] and box["value"]["evidence"]["forced"],
              f"{shape}: request first: the decision is fresh, forced: {box}")
        check(outcome("PGU-2") == (1, "fresh"), f"one request, one fresh decision: {outcome('PGU-2')}")

        # 2. The decision first, still uncommitted: the request waits for it, then is refused.
        candidate("PGU-3")
        with psycopg.connect(listener) as pane, psycopg.connect(service, autocommit=False) as director:
            first = decide(pane, "PGU-3")
            thread, box = in_thread(lambda: (request(director, "PGU-3"), director.commit()))
            thread.join(1.0)
            check(first["decision"] == "continue" and thread.is_alive(), f"the request waits while the decision is in flight: {first}")
            pane.commit()
            thread.join(10)
        check(not thread.is_alive() and "already decided" in box.get("error", ""), f"{shape}: decision first: the request is refused: {box}")
        check(outcome("PGU-3") == (0, "continue"), f"no request, one continue decision: {outcome('PGU-3')}")

        if shape != "fresh":
            return
        # 3. Audit's reproduction: both started together, each insert paused, several times over.
        w.sql("CREATE FUNCTION ticket_board.syrd626_pause() RETURNS trigger LANGUAGE plpgsql AS $$ "
              "BEGIN PERFORM pg_sleep(0.3); RETURN NEW; END $$; "
              "CREATE TRIGGER syrd626_pause BEFORE INSERT ON ticket_board.ticket_context_continuations "
              "FOR EACH ROW EXECUTE FUNCTION ticket_board.syrd626_pause(); "
              "CREATE TRIGGER syrd626_pause BEFORE INSERT ON ticket_board.ticket_context_fresh_requests "
              "FOR EACH ROW EXECUTE FUNCTION ticket_board.syrd626_pause();")
        seen = set()
        for n in range(4, 10):
            ticket = f"PGU-{n}"
            candidate(ticket)
            with psycopg.connect(service, autocommit=True) as director, psycopg.connect(listener, autocommit=True) as pane:
                go = threading.Barrier(2)
                a, a_box = in_thread(lambda: (go.wait(), request(director, ticket)))
                b, b_box = in_thread(lambda: (go.wait(), decide(pane, ticket)))
                a.join(20)
                b.join(20)
            got = outcome(ticket)
            decided = (b_box.get("value") or (None, {}))[1]
            check(got in ((1, "fresh"), (0, "continue")) and decided.get("decision") == got[1]
                  and (got[0] == 1) == ("error" not in a_box),
                  f"{ticket}: exactly one outcome, the request and the decision agreeing on it: {got} {a_box} {b_box}")
            seen.add(got)
        w.sql("DROP TRIGGER syrd626_pause ON ticket_board.ticket_context_continuations; "
              "DROP TRIGGER syrd626_pause ON ticket_board.ticket_context_fresh_requests; DROP FUNCTION ticket_board.syrd626_pause();")
        check(seen, f"the concurrent rounds ran: {seen}")


# --- launch and recovery: a restart is not a ticket boundary ------------------------------------------------------


class LaunchKit:
    """The real launcher's start of the author's pane, on a disposable home, with tmux stood in for."""

    def __init__(self, w, *, runtime_board: str | None = None) -> None:
        import getpass
        import os
        import subprocess as sp
        from dataclasses import replace

        self.home = Path(tempfile.mkdtemp(prefix="syrd626-home."))
        os.environ["HOME"] = str(self.home)  # never the live home (SYRD-627)
        from scripts import team_launcher as launcher

        self.launcher, self.sp = launcher, sp
        work = self.home / "config"
        work.mkdir()
        raw = json.loads((ROOT / "config/team-launcher/pgu.json").read_text(encoding="utf-8"))
        raw["run_as_user"] = getpass.getuser()
        raw["board_url"] = w.base
        for entry in raw["roles"]:
            if entry["role"] == AUTHOR:  # an ephemeral claude worker, detached so attach-role can bring it up
                entry.update(cli=["claude"], workdir=str(self.home / "work"), fresh_session_per_ticket=True,
                             resume_mode="flag", resume_flag="--resume", detached=True)
                entry.pop("slot", None)
        layout = ROOT / "config/team-launcher" / raw["layout"]
        shutil.copy(layout, work / layout.name)
        self.config_path = work / "pgu.json"
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.load = lambda: replace(launcher.load_project_config("pgu", self.config_path),
                                    session_dir=self.home / ".local/state/pgu-ticket-board/pane-sessions",
                                    role_state_isolation=True, desktop_access={"mode": "headless"})
        config = self.load()
        role = next(r for r in config.roles if r.role == AUTHOR)
        if runtime_board is not None:  # another board address, or none at all
            env = {**role.env, "TICKET_BOARD_URL": runtime_board} if runtime_board else {
                k: v for k, v in role.env.items() if k != "TICKET_BOARD_URL"}
            role = replace(role, env=env)
        check(role.fresh_session_per_ticket and role.env.get("TICKET_BOARD_URL", "") == (w.base if runtime_board is None
                                                                                          else runtime_board),
              f"the author is a fresh-per-ticket role naming its board: {role.env.get('TICKET_BOARD_URL')}")
        self.role = role
        self.session_dir = launcher.role_session_dir(config, self.role)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.state = self.home / "pane-state"
        launcher.live_command_matches_role = lambda role, runner=None: True
        launcher.RESUME_STARTUP_TIMEOUT_SECONDS = 0.3
        launcher.RESUME_STARTUP_POLL_SECONDS = 0.05

    def converse(self, session_id: str) -> None:
        """The provider's own store holds this conversation, as a resumable one does."""
        from provider_store_fixture import write_session

        path = self.launcher._claude_project_dir_for_workdir(self.role.workdir, home=self.home) / f"{session_id}.jsonl"
        write_session(path, "claude", session_id, self.role.workdir)  # as Claude files it (SYRD-638)

    def record(self, session_id: str | None) -> None:
        path = self.session_dir / self.launcher.session_file_name(self.role.target)
        if session_id is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"target": self.role.target, "session_id": session_id, "source": "test",
                                        "updated_at": time.time()}) + "\n", encoding="utf-8")

    def history(self) -> list[dict]:
        safe = self.launcher.session_file_name(self.role.target).removesuffix(".json")
        path = self.session_dir / f"{safe}.resume.log"
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []

    def start(self, *, attach: bool = False) -> dict:
        import io
        sp = self.sp
        calls: list[list[str]] = []

        def tmux(args, **_kwargs):
            args = [str(a) for a in args]
            calls.append(args)
            if args[:1] != ["tmux"]:
                raise AssertionError(f"refused: {args}")
            return sp.CompletedProcess(args, 1 if args[1] == "has-session" else 0, "", "")

        before = len(self.history())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.launcher._start_role_session(self.role, session_dir=self.session_dir, pane_state_dir=self.state,
                                                     prefer_resume=True, seed_source="syrd626.start", runner=tmux)
        started = [call[-1] for call in calls if call[1:2] == ["new-session"]]
        return self._seen(code, started, err, before)

    def attach(self) -> dict:
        """attach-role: bringing the detached author back into a slot is a launch too."""
        import io
        sp = self.sp
        calls: list[list[str]] = []

        def tmux(args, **_kwargs):
            args = [str(a) for a in args]
            calls.append(args)
            return sp.CompletedProcess(args, 1 if args[1:2] == ["has-session"] else 0, "", "")

        before = len(self.history())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                code = self.launcher.attach_role_to_slot(self.load(), config_path=self.config_path, role_name=AUTHOR, slot=4,
                                                         session_dir=self.session_dir, pane_state_dir=self.state, runner=tmux,
                                                         print_func=lambda *_: None)
            except SystemExit as exc:
                code = f"exit: {exc}"
        started = [call[-1] for call in calls if call[1:2] == ["new-session"]]
        # attach-role records the role as attached; put it back for the next launch.
        raw = json.loads(self.config_path.read_text())
        for entry in raw["roles"]:
            if entry["role"] == AUTHOR:
                entry["detached"] = True
                entry.pop("slot", None)
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        return self._seen(code, started, err, before)

    def _seen(self, code, started, err, before) -> dict:
        return {"code": code, "started": started, "said": err.getvalue(), "events": [e["event"] for e in self.history()[before:]],
                "resumed": next((part for part in (started[0].split() if started else []) if part.startswith("app-")), "")}


def test_a_restart_resumes_the_ticket_it_holds_and_starts_fresh_only_when_it_holds_none() -> None:
    with board("recovery") as w:
        kit = LaunchKit(w)
        # No ticket held: fresh, as before -- and the next unrelated claim clears as always.
        kit.record("app-launch")
        kit.converse("app-launch")
        got = kit.start()
        check(got["code"] == 0 and got["resumed"] == "" and got["events"][:1] == ["fresh_per_ticket"]
              and "app holds no active ticket" in got["said"], f"holding nothing, a launch starts fresh: {got}")
        assert_fresh(w, w.take("PGU-1"), "PGU-1", "has held no earlier ticket's conversation", "the next claim after it")
        bound = w.context("PGU-1")["session_id"]
        kit.converse(bound)
        # (a) Restarted mid-ticket: its proven conversation, whatever the local record says.
        for label, local in (("its own record", bound), ("no local record (MEFP's luna-9)", None),
                             ("a record naming another session", "app-elsewhere")):
            kit.record(local)
            got = kit.start()
            check(got["code"] == 0 and got["resumed"] == bound and got["events"][:1] == ["recovery_resume"]
                  and "PGU-1 is app's active ticket, in conversation" in got["said"]
                  and "fresh session" not in got["said"],
                  f"mid-ticket, {label}: the ticket's own conversation is resumed, not a fresh one: {got}")
            check(kit.launcher.session_id_for_role(kit.role, kit.session_dir) == bound,
                  f"{label}: and the record names it now")
        again = kit.start()
        check(again["resumed"] == bound and again["events"][:1] == ["recovery_resume"], f"a second restart decides the same: {again}")
        kit.record(None)
        attached = kit.attach()
        check(attached["code"] == 0 and attached["resumed"] == bound and attached["events"][:1] == ["recovery_resume"],
              f"attach-role brings it back in the same conversation, even with no local record: {attached}")
        check(w.decision("PGU-1")["decision"] == "fresh" and w.sql("SELECT count(*) FROM ticket_board.ticket_context_continuations;") == "1",
              "and a launch decides nothing on the board: it only reads")
        # The role now runs another runtime: that conversation cannot be resumed; fresh, said, and SYRD-540's to park.
        from dataclasses import replace
        other = replace(kit.role, cli=["codex"])
        kit.role, saved = other, kit.role
        got = kit.start()
        check(got["code"] == 0 and got["resumed"] == "" and got["events"][:1] == ["recovery_runtime_changed"]
              and "now runs codex, which cannot resume it" in got["said"], f"another runtime: {got}")
        kit.role = saved
        w.release("PGU-1")
        # (b) A continuation decided before the restart, not yet delivered (the bind refused): resume what it continues.
        w.sql("REVOKE EXECUTE ON FUNCTION ticket_board.apply_ticket_context_continuation(text, text) FROM ticket_board_listener;")
        w.seed("PGU-2", "PGU-1")
        w.act("PGU-2", "admit", "director")
        w.settle(3)
        check(w.decision("PGU-2")["decision"] == "continue" and not w.decision("PGU-2")["applied_at"], "decided, not delivered")
        kit.record(None)
        got = kit.start()
        check(got["resumed"] == bound and "PGU-2 continues PGU-1's conversation" in got["said"],
              f"a continuation decided before the restart resumes the conversation it continues: {got}")
        w.sql("GRANT EXECUTE ON FUNCTION ticket_board.apply_ticket_context_continuation(text, text) TO ticket_board_listener;")
        w.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() WHERE target_role = 'app';")
        mark = len(w.panes.to(AUTHOR))
        w.restart()
        w.settle(4)
        sent = w.panes.to(AUTHOR)[mark:]
        check("/clear" not in sent and len([t for t in sent if "PGU-2 --" in t]) == 1
              and w.decision("PGU-2")["applied_at"] and int(w.sql("SELECT count(*) FROM ticket_board.ticket_context_continuations "
                                                                 "WHERE ticket_id='PGU-2';")) == 1,
              f"then delivered once, uncleared, on the one decision: {sent}")
        # ... and after it was delivered: the continued ticket's own binding, the same conversation.
        got = kit.start()
        check(got["resumed"] == bound and "PGU-2 is app's active ticket" in got["said"], f"restarted after delivery: {got}")
        # The ticket's conversation cannot be identified: one error naming the role and the ticket, nothing started.
        w.sql("UPDATE ticket_board.ticket_role_contexts SET state='unconfirmed', session_id=NULL WHERE ticket_id='PGU-2';")
        for attempt in (1, 2):
            got = kit.start()
            check(got["code"] == 1 and got["started"] == [] and got["events"] == ["recovery_refused"]
                  and got["said"].count("not starting app: PGU-2 is app's active ticket, and its conversation was never confirmed") == 1
                  and "the Director can defer PGU-2" in got["said"],
                  f"attempt {attempt}: refused once, by name, and no fresh conversation: {got}")
        refused = kit.attach()
        check(refused["code"] == 1 and refused["started"] == [] and refused["events"] == ["recovery_refused"],
              f"attach-role refuses it the same way: {refused}")
        w.sql("DELETE FROM ticket_board.ticket_role_contexts WHERE ticket_id='PGU-2';")
        got = kit.start()
        check(got["code"] == 1 and "no conversation was recorded for it" in got["said"] and got["started"] == [],
              f"a binding that is gone refuses too: {got}")


def test_a_launch_that_cannot_ask_the_board_never_erases_what_is_recorded() -> None:
    with board("unasked") as w:
        kit = LaunchKit(w, runtime_board="http://127.0.0.1:9")
        kit.record("app-old")
        kit.converse("app-old")
        got = kit.start()
        check(got["code"] == 0 and got["resumed"] == "app-old" and got["events"][:1] == ["recovery_unasked_resume"]
              and "could not ask the board whether app holds an active ticket" in got["said"],
              f"the board cannot be asked: what is recorded is resumed, not discarded: {got}")
        kit.record(None)
        got = kit.start()
        check(got["code"] == 0 and got["resumed"] == "" and got["events"][:1] == ["recovery_unasked_fresh"],
              f"and with nothing recorded, a fresh start, said: {got}")
        quiet = LaunchKit(w, runtime_board="")
        quiet.record("app-old")
        quiet.converse("app-old")
        got = quiet.start()
        check(got["code"] == 0 and got["resumed"] == "" and got["events"][:1] == ["fresh_per_ticket"]
              and "configured for fresh sessions per ticket; starting a fresh session instead of inheriting" in got["said"],
              f"a role that names no board starts exactly as before: {got}")
    with board("recovery_nopull", pull=False) as w:
        kit = LaunchKit(w)
        w.take("PGU-1")
        kit.record("app-mine")
        kit.converse("app-mine")
        got = kit.start()
        check(got["resumed"] == "app-mine" and "PGU-1 is app's active ticket" in got["said"],
              f"no pull policy binds no session: the role's own record is the ticket's: {got}")
        kit.record(None)
        got = kit.start()
        check(got["code"] == 1 and "PGU-1 is app's active ticket, and no session is recorded for it" in got["said"],
              f"and without one, the start is refused by name: {got}")


def test_parity_across_board_shapes() -> None:
    for shape in ("migrated", "upgraded"):
        with board(f"parity_{shape}", shape=shape) as w:
            w.take("PGU-1")
            w.release("PGU-1")
            assert_continued(w, w.take("PGU-2", "PGU-1"), "PGU-2", "PGU-1", "PGU-1", "PGU-1", f"{shape}: parent -> child")
            w.release("PGU-2")
            assert_fresh(w, w.take("PGU-3"), "PGU-3", "share no parent hierarchy", f"{shape}: unrelated")


def main() -> int:
    for name, value in list(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"context_continuation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
