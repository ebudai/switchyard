#!/usr/bin/env python3
"""SYRD-725: the User's answer to an operator wait reaches the Director, who alone can release it.

On SYRD-691 Ops asked the Director for Eric's approval; the Director recorded `operator:eric`. Eric answered on the
ticket as the User ("approved, and now is a good time"). add_comment told Ops, the owner, who cannot release the
blocker, and the Director heard nothing until Eric noticed the stall: a wait on the Director is refused while the
blocker stands.

On production-built boards (router_role_board_test's: schema.sql, the real migrations, rbac.sql, the HTTP server and
the real listener, only the panes stood in for), main first from a git archive, then this tree, then main's board
upgraded with this tree's migrations:

- SYRD-691 as it ran: on main the User's comment wakes Ops and not the Director. Here it is one Director notice
  naming the blocker, and no Ops notice; the comment is stored as written and the blocker still stands.
- Restart and replay: a new listener sends nothing more, and a notice claimed by a listener that died before
  sending goes out once.
- The Director keeps the blocker: a later User comment, marked urgent, is one more Director notice and says so.
- The Director releases it: one Ops wake-up, saying what the release recorded, and nothing to the Director.
- Released before the notice went out: the notice is dropped, and the owner hears once, from the release.
- A release racing the User's comment, both orders: one Ops wake-up, nothing for the Director.
- Unchanged: a User comment with no blocker, or another ticket as its blocker, or after the release, goes to the
  owner; an urgent role comment goes to the owner; a finished ticket tells nobody; a ticket the Director owns is
  one Director notice, not two.
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "172b5b170afea17e7b62914c585aba8f0b680d69"
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1026_syrd725_operator_response.sql"
APPROVAL = "approved, and now is a good time"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def scenario(root: Path, upgrade_from: Path | None = None) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    from board_stall_test import example, short
    from router_role_board_test import Board, fixture, temporary_cluster
    from scripts.ticket_board import notify_listener
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener

    assert Path(notify_listener.__file__).resolve().is_relative_to(root.resolve()), notify_listener.__file__
    out: dict = {}

    def to(sent, role: str) -> list[str]:
        return [m for t, m in sent if t == f"pgu-{role}:0.0"]

    def deliver(board) -> list:
        """One listener pass, every pane idle: what reached which pane."""
        board.busy.clear()
        return board.one_pass()

    def split(sent) -> dict:
        return {role: to(sent, role) for role in ("director", "ops", "app", "main")}

    def make_due(board) -> None:
        board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                  "WHERE claimed_at IS NULL;")

    def comments(board, t: str) -> list:
        return json.loads(board.sql("SELECT coalesce(json_agg(json_build_array(who, text, urgent) ORDER BY position), "
                                    f"'[]')::text FROM ticket_board.ticket_comments WHERE ticket_id = '{t}';"))

    def blockers(board, t: str) -> list:
        return json.loads(board.sql("SELECT coalesce(json_agg(blocker_ticket_id || CASE WHEN resolved THEN ' resolved' "
                                    "ELSE '' END ORDER BY position), '[]')::text FROM ticket_board.ticket_blockers "
                                    f"WHERE ticket_id = '{t}';"))

    def traces(board, t: str) -> list:
        """What the listener did with each Director notice about the User's answer: [event, reason]."""
        return json.loads(board.sql(
            "SELECT coalesce(json_agg(json_build_array(event, coalesce(busy_reason, '')) ORDER BY id), '[]')::text "
            f"FROM ticket_board.notification_trace WHERE ticket_id = '{t}' AND event IN ('enqueue', 'send', 'drop') "
            "AND notification_id IN (SELECT notification_id FROM ticket_board.notification_trace WHERE event = "
            "'enqueue' AND detail->'payload'->>'kind' = 'operator_response');"))

    def ticket(board, number: int, owner: str) -> str:
        """Routed from Analysis to its owner, the hand-over delivered."""
        t = f"PGU-{number}"
        fixture.seed_postgres_ticket(board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
        out.setdefault("routes", {})[t] = short(board.act(t, "route", "director", state="in_progress", assignee=owner))
        out.setdefault("handed_over", {})[t] = len(to(deliver(board), owner))
        return t

    def boundary(cluster, dbname: str, *, strip: bool) -> dict:
        """Main's board as deployed, then pgu1026 alone, as the runner applies one file: before its exit-time
        revocation and before rbac.sql. Who may execute each new function then, and after rbac.sql."""
        from router_role_board_test import build_board, migrate

        admin = build_board(cluster, dbname, before=upgrade_from)
        text = (root / MIGRATION.relative_to(ROOT)).read_text()
        if strip:  # the control: the candidate Audit returned, without its revocations
            text = "".join(l for l in text.splitlines(True) if not l.startswith("REVOKE EXECUTE ON FUNCTION"))
        fixture.psql(admin, text)
        roles = ("director", "user", "ops", "app", "main", "audit", fixture.SERVICE_ROLE, "ticket_board_listener")
        functions = ("standing_operator_blockers(text)", "notify_operator_response(text, text[], boolean)",
                     "operator_response_is_current(jsonb)")

        def who() -> dict:
            return {f.split("(")[0]: sorted(r for r in roles if fixture.psql(
                admin, f"SELECT has_function_privilege('{r}', 'ticket_board.{f}', 'EXECUTE');").strip() == "t")
                for f in functions}
        seen = {"window": who()}
        # The rest of the upgrade, as production runs it: the runner's pending migrations (a later ticket's too), then
        # rbac.sql, whose grants may name their functions.
        migrate(admin, root)
        seen["after_rbac"] = who()
        return seen

    with temporary_cluster(prefix="syrd725-pg-") as cluster:
        if upgrade_from:
            out["boundary"] = boundary(cluster, "syrd725_boundary", strip=False)
            out["boundary_control"] = boundary(cluster, "syrd725_control", strip=True)
        board = Board(cluster, "syrd691", example(), before=upgrade_from)
        try:
            if upgrade_from:
                out["migrations"] = board.sql("SELECT count(*) FROM ticket_board.schema_migrations "
                                              f"WHERE name = '{MIGRATION.name}';")
            # -- SYRD-691: Ops asks the Director for Eric; the Director records operator:eric --------------------
            t = ticket(board, 691, "ops")
            out["request"] = short(board.act(t, "request_dependency", "ops", role="director",
                                             reason="Eric must approve the privileged install and its timing."))
            out["request_sent"] = split(deliver(board))
            out["blocked"] = short(board.act(t, "set_blockers", "director", blocked_by=["operator:eric"],
                                             blocked_reason="Eric approves the privileged install and its timing."))
            out["blocked_sent"] = split(deliver(board))
            out["awaiting_after_block"] = board.sql("SELECT coalesce(awaiting_role, '') FROM "
                                                    f"ticket_board.ticket_notification_state WHERE ticket_id = '{t}';")
            out["depend_while_blocked"] = short(board.act(t, "request_dependency", "ops", role="director",
                                                          reason="Eric answered; please release operator:eric."))
            make_due(board)
            out["handoff_silent"] = split(deliver(board))
            # -- Eric answers as the User --------------------------------------------------------------------------
            out["answer"] = short(board.act(t, "add_comment", "user", text=APPROVAL))
            out["answer_sent"] = split(deliver(board))
            out["comments_after_answer"] = comments(board, t)[-1]
            out["blockers_after_answer"] = blockers(board, t)
            out["queued_after_answer"] = board.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                                   f"WHERE ticket_id = '{t}';")
            # -- restart and replay --------------------------------------------------------------------------------
            board.listener = TicketBoardNotifyListener(
                conninfo=board.listener_url, sender=board.listener.sender, activity_gate=lambda x: x in board.busy,
                target_exists=lambda _x: True, submission_witness=lambda x, _s: any(y == x for y, _m in board.sent),
                poll_seconds=0, project="pgu")
            out["restart_sent"] = split(deliver(board))
            out["restart_traces"] = traces(board, t)
            # A notice a listener claimed and died holding: past the claim timeout it goes out once.
            out["second"] = short(board.act(t, "add_comment", "user", text="Also: the window closes at 18:00."))
            board.sql("UPDATE ticket_board.ticket_notification_queue SET claimed_at = clock_timestamp() - "
                      f"interval '10 minutes', attempts = attempts + 1 WHERE ticket_id = '{t}';")
            out["reclaimed_sent"] = split(deliver(board))
            out["reclaimed_again"] = split(deliver(board))
            # -- the Director keeps the blocker; the User writes again, urgently -------------------------------------
            out["retain"] = short(board.act(t, "add_comment", "director",
                                            text="Seen; operator:eric stays until the run's log is on the ticket."))
            out["retain_sent"] = split(deliver(board))
            out["urgent"] = short(board.act(t, "add_comment", "user", text="Go ahead now, please.", urgent=True))
            out["urgent_sent"] = split(deliver(board))
            out["blockers_after_retain"] = blockers(board, t)
            # -- the Director releases it --------------------------------------------------------------------------
            out["release"] = short(board.act(t, "release_external_blocker", "director", ref="operator:eric",
                                             reason="Eric approved on the ticket; run the approved install now."))
            out["release_sent"] = split(deliver(board))
            out["blockers_after_release"] = blockers(board, t)
            out["after_release"] = short(board.act(t, "add_comment", "user", text="Thanks."))
            out["after_release_sent"] = split(deliver(board))
            out["traces"] = traces(board, t)

            # -- released before the Director's notice went out ------------------------------------------------------
            stale = ticket(board, 692, "app")
            board.act(stale, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="Eric's key.")
            deliver(board)
            out["stale_answer"] = short(board.act(stale, "add_comment", "user", text="The key is on the share."))
            out["stale_release"] = short(board.act(stale, "release_external_blocker", "director",
                                                   ref="operator:eric", reason="the key is on the share"))
            out["stale_sent"] = split(deliver(board))
            out["stale_traces"] = traces(board, stale)

            # -- unchanged routing -----------------------------------------------------------------------------------
            plain = ticket(board, 693, "main")
            out["plain_user"] = split((board.act(plain, "add_comment", "user", text="a thought"), deliver(board))[1])
            out["plain_urgent"] = split((board.act(plain, "add_comment", "user", text="now", urgent=True),
                                         deliver(board))[1])
            out["role_urgent"] = split((board.act(plain, "add_comment", "audit", text="look", urgent=True),
                                        deliver(board))[1])
            out["role_plain"] = split((board.act(plain, "add_comment", "audit", text="fyi"), deliver(board))[1])
            # A blocker that is another ticket is not a person's wait.
            board.act(stale, "set_blockers", "director", blocked_by=[plain], blocked_reason="needs PGU-693")
            deliver(board)
            out["ticket_blocked_user"] = split((board.act(stale, "add_comment", "user", text="any news?"),
                                                deliver(board))[1])
            # A ticket the Director owns, waiting on Eric: one notice to the Director, not two.
            owned = "PGU-694"
            fixture.seed_postgres_ticket(board.admin, owned, title="Fixture PGU-694", state="analysis",
                                         assignee="director")
            board.act(owned, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="Eric decides")
            deliver(board)
            out["owned_user"] = split((board.act(owned, "add_comment", "user", text="decided: yes"),
                                       deliver(board))[1])
            # Two answers before the Director reads either: a notice each, one pass at a time (the pane is busy
            # with the first until its turn ends).
            board.act(owned, "add_comment", "user", text="one more thing")
            board.act(owned, "add_comment", "user", text="and another")
            out["owned_twice"] = []
            for _ in range(3):
                out["owned_twice"].append(len(to(deliver(board), "director")))
                make_due(board)
            # A finished ticket still blocked by Eric tells nobody. No move finishes a blocked ticket (a cancel is
            # refused), so the row is made by hand.
            done = "PGU-697"
            fixture.seed_postgres_ticket(board.admin, done, title="Fixture PGU-697", state="cancelled",
                                         assignee="director")
            board.sql("INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position) "
                      f"VALUES ('{done}', 'operator:eric', 0);")
            out["finished_blockers"] = blockers(board, done)
            out["finished_user"] = split((board.act(done, "add_comment", "user", text="ok"), deliver(board))[1])
            out["finished_traces"] = traces(board, done)

            # -- the race: a release and the User's comment at once, both orders -----------------------------------
            def waiting(n: int) -> bool:
                for _ in range(200):
                    if board.sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                 "AND datname = current_database();") == str(n):
                        return True
                    time.sleep(0.05)
                return False

            def race(number: int, first: str) -> dict:
                rt = ticket(board, number, "main" if number % 2 else "app")
                board.act(rt, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="Eric")
                deliver(board)
                answers: dict = {}
                moves = {"release": lambda: answers.update(release=short(board.act(
                             rt, "release_external_blocker", "director", ref="operator:eric", reason="approved"))),
                         "comment": lambda: answers.update(comment=short(board.act(
                             rt, "add_comment", "user", text=APPROVAL)))}
                with psycopg.connect(board.admin) as conn:
                    conn.execute(f"SELECT 1 FROM ticket_board.tickets WHERE id = '{rt}' FOR UPDATE")
                    threads = []
                    for n, move in enumerate((first, "comment" if first == "release" else "release"), 1):
                        threads.append(threading.Thread(target=moves[move]))
                        threads[-1].start()
                        answers[f"waited_{n}"] = waiting(n)
                for thread in threads:
                    thread.join(60)
                answers["sent"] = split(deliver(board))
                answers["blockers"] = blockers(board, rt)
                answers["owner"] = "main" if number % 2 else "app"
                return answers

            # PGU-695 is main's and PGU-696 app's: each implementer holds one active ticket.
            board.act(plain, "defer", "director")
            board.act("PGU-692", "defer", "director")
            out["race_release_first"] = race(695, "release")
            out["race_comment_first"] = race(696, "comment")

            # -- the database alone ------------------------------------------------------------------------------
            if board.sql("SELECT to_regprocedure('ticket_board.operator_response_is_current(jsonb)') IS NOT NULL;") != "t":
                out["probes"] = "absent"
            else:
                def current(payload: dict) -> str:
                    try:
                        return board.sql("SELECT ticket_board.operator_response_is_current("
                                         f"'{json.dumps(payload)}'::jsonb);", board.listener_url)
                    except AssertionError as exc:  # a refusal is an answer
                        return f"refused: {str(exc).splitlines()[0]}"
                eric = ["operator:eric"]
                out["probes"] = {
                    "standing": current({"id": owned, "operators": eric}),
                    "one_of_two": current({"id": owned, "operators": ["operator:alice", "operator:eric"]}),
                    "other_operator": current({"id": owned, "operators": ["operator:alice"]}),
                    "released": current({"id": t, "operators": eric}),
                    "finished": current({"id": done, "operators": eric}),
                    "missing_ticket": current({"id": "PGU-999", "operators": eric}),
                    "no_operators": current({"id": owned}),
                }
                # A resolved row names nobody waited on.
                board.sql(f"UPDATE ticket_board.ticket_blockers SET resolved = true WHERE ticket_id = '{owned}';")
                out["probes"]["resolved_row"] = current({"id": owned, "operators": eric})
                board.sql(f"UPDATE ticket_board.ticket_blockers SET resolved = false WHERE ticket_id = '{owned}';")
                # Addressed to anyone but the Director, it is not delivered.
                board.sql(f"SELECT ticket_board.enqueue_notification('{owned}', 'ticket_update', 'ops', 'misaddressed', "
                          f"'{{\"kind\": \"operator_response\", \"id\": \"{owned}\", \"operators\": "
                          f"[\"operator:eric\"]}}'::jsonb, 'misaddressed');")
                out["probes"]["misaddressed"] = split(deliver(board))
                # Raised again for a comment already delivered and acknowledged, twice: nothing more.
                board.sql(f"SELECT ticket_board.notify_operator_response('{owned}', ARRAY['operator:eric'], false); "
                          f"SELECT ticket_board.notify_operator_response('{owned}', ARRAY['operator:eric'], true);")
                out["probes"]["queued_again"] = board.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                                          f"WHERE ticket_id = '{owned}';")
                out["probes"]["again_sent"] = split(deliver(board))
                out["probes"]["record"] = json.loads(board.sql(
                    "SELECT json_agg(json_build_array(r.comment_id = (SELECT max(c.id) FROM ticket_board.ticket_comments c "
                    "WHERE c.ticket_id = r.ticket_id), r.operators, r.urgent))::text FROM "
                    f"ticket_board.ticket_operator_responses r WHERE r.ticket_id = '{owned}';"))
        finally:
            board.close()
    return out


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, upgrade_from: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL",
                                                                     "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree),
                            *([str(upgrade_from)] if upgrade_from else [])],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=500,
                           stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


CHANGED = "(in in_progress, that you own) was changed by"


def only(sent: dict, role: str, *words: str) -> bool:
    """Exactly one notice, to `role`, saying each of `words`; nothing to anyone else."""
    return (len(sent[role]) == 1 and all(w in sent[role][0] for w in words)
            and not any(sent[r] for r in sent if r != role))


def common(got: dict) -> None:
    """What main and this tree agree on: SYRD-691's wait as it ran, and every route this ticket keeps."""
    check(set(got["routes"].values()) == {"ok"} and set(got["handed_over"].values()) == {1},
          f"each ticket is routed and handed over: {got['routes']} {got['handed_over']}")
    check(got["request"] == "ok" and only(got["request_sent"], "director", "ops is awaiting director"),
          f"Ops' request reaches the Director: {got['request']} {got['request_sent']}")
    check(got["blocked"] == "ok" and only(got["blocked_sent"], "ops", f"PGU-691 -- Fixture PGU-691 {CHANGED} "
                                          "director: blockers"),
          f"the Director records operator:eric and Ops is told: {got['blocked']} {got['blocked_sent']}")
    check(got["awaiting_after_block"] == "" and "unresolved blocker prevents an awaiting-role handoff: operator:eric"
          in got["depend_while_blocked"], f"the formal hand-over closed and cannot reopen while Eric is waited on: "
          f"{got['awaiting_after_block']!r} {got['depend_while_blocked']}")
    check(not any(got["handoff_silent"].values()), f"its later reminders are silent: {got['handoff_silent']}")
    check(got["answer"] == "ok" and got["comments_after_answer"] == ["user", APPROVAL, False]
          and got["blockers_after_answer"] == ["operator:eric"],
          f"the User's answer is stored as written, by the User, and releases nothing: {got['comments_after_answer']} "
          f"{got['blockers_after_answer']}")
    check(not any(got["restart_sent"].values()) and not any(got["reclaimed_again"].values()),
          f"a restarted listener sends nothing again: {got['restart_sent']} {got['reclaimed_again']}")
    check(got["retain"] == "ok" and only(got["retain_sent"], "ops", f"{CHANGED} director: new comment")
          and got["blockers_after_retain"] == ["operator:eric"],
          f"the Director's own comment still reaches Ops, and the blocker stands: {got['retain_sent']}")
    check(got["release"] == "ok" and got["blockers_after_release"] == []
          and only(got["after_release_sent"], "ops", f"{CHANGED} user: new comment"),
          f"once released, the User's comment goes to the owner again: {got['after_release_sent']}")
    check(only(got["plain_user"], "main", f"{CHANGED} user: new comment")
          and only(got["plain_urgent"], "main", f"{CHANGED} user: new comment")
          and only(got["role_urgent"], "main", f"{CHANGED} audit: new comment")
          and not any(got["role_plain"].values()),
          f"with no blocker, User, urgent and role comments route as before: {got['plain_user']} "
          f"{got['plain_urgent']} {got['role_urgent']} {got['role_plain']}")
    check(only(got["ticket_blocked_user"], "app", f"{CHANGED} user: new comment"),
          f"a ticket blocked by another ticket, not a person, tells its owner: {got['ticket_blocked_user']}")
    check(got["finished_blockers"] == ["operator:eric"] and not any(got["finished_user"].values()),
          f"a finished ticket tells nobody, blocker or not: {got['finished_user']}")
    c = got["race_comment_first"]
    check(c["waited_1"] and c["waited_2"] and c["comment"] == "ok" and c["release"] == "ok" and c["blockers"] == []
          and only(c["sent"], "app", f"{CHANGED} director: external blocker"),
          f"the User's comment, then the Director's release, queued on the row: one app wake-up: {c}")
    if "migrations" in got:
        check(got["migrations"] == "1", f"main's board takes this tree's migration once: {got['migrations']}")
    if "boundary" in got:
        b, c = got["boundary"], got["boundary_control"]
        nobody = {"standing_operator_blockers": [], "notify_operator_response": [], "operator_response_is_current": []}
        check(b["window"] == nobody,
              f"pgu1026 applied alone, before rbac.sql: no role may execute its new functions: {b['window']}")
        check(b["after_rbac"] == {**nobody, "operator_response_is_current": ["ticket_board_listener"]},
              f"after rbac.sql the listener alone may ask whether a notice is current: {b['after_rbac']}")
        check(all("app" in c["window"][f] and "ops" in c["window"][f] for f in nobody)
              and c["after_rbac"] == b["after_rbac"],
              f"the control, without the revocations: pane roles may execute them until rbac.sql: {c}")


def before(got: dict) -> None:
    """Main: the User's answer wakes Ops, who cannot release the blocker, and never the Director."""
    common(got)
    check(only(got["answer_sent"], "ops", f"{CHANGED} user: new comment"),
          f"main: Eric's answer goes to Ops and not to the Director: {got['answer_sent']}")
    check(only(got["urgent_sent"], "ops", f"{CHANGED} user: new comment"),
          f"main: an urgent answer too: {got['urgent_sent']}")
    check(only(got["release_sent"], "ops", f"{CHANGED} director: external blocker released -- "),
          f"main: the release's wake-up does not say what it recorded: {got['release_sent']}")
    check("deadlock detected" in got["race_release_first"].get("comment", ""),
          f"main: a release, then the User's comment, deadlock and the comment is refused: {got['race_release_first']}")
    check(got["probes"] == "absent", f"main has no operator_response_is_current: {got['probes']}")


def candidate(got: dict) -> None:
    common(got)
    check(only(got["answer_sent"], "director", "PGU-691 -- Fixture PGU-691 (in in_progress, owned by ops) is blocked "
               "by operator:eric, and the User answered on it. Only the Director can inspect that comment and release "
               "or keep the blocker", "not proof the prerequisite is met, and nothing was released", "ops is not "
               "told while the blocker stands; releasing it tells ops."),
          f"Eric's answer is one Director notice naming the blocker, and no Ops notice: {got['answer_sent']}")
    check(got["queued_after_answer"] == "0" and got["restart_traces"] == [["enqueue", ""], ["send", "idle"]],
          f"delivered once and gone from the queue: {got['queued_after_answer']} {got['restart_traces']}")
    check(only(got["reclaimed_sent"], "director", "is blocked by operator:eric, and the User answered on it."),
          f"a notice claimed by a listener that died goes out once: {got['reclaimed_sent']}")
    check(only(got["urgent_sent"], "director", "the User answered on it, marked urgent."),
          f"an urgent answer is one Director notice that says so: {got['urgent_sent']}")
    check(only(got["release_sent"], "ops", f"{CHANGED} director: external blocker released (operator:eric): Eric "
               "approved on the ticket; run the approved install now. -- re-read"),
          f"the release is one Ops wake-up with what it recorded, and nothing to the Director: {got['release_sent']}")
    check(got["traces"] == [["enqueue", ""], ["send", "idle"]] * 3,
          f"three answers, three Director notices, each sent once: {got['traces']}")
    check(only(got["stale_sent"], "app", f"{CHANGED} director: external blocker released (operator:eric): the key")
          and got["stale_traces"] == [["enqueue", ""], ["drop", "stale_notification"]],
          f"released before delivery: the notice is dropped, the owner told once: {got['stale_sent']} "
          f"{got['stale_traces']}")
    check(only(got["owned_user"], "director", "(in analysis, owned by director) is blocked by operator:eric")
          and "is not told" not in got["owned_user"]["director"][0],
          f"a ticket the Director owns: one Director notice, not two: {got['owned_user']}")
    check(got["owned_twice"] == [1, 1, 0], f"two answers, two Director notices: {got['owned_twice']}")
    check(got["finished_traces"] == [], f"a finished ticket raises no notice: {got['finished_traces']}")
    r = got["race_release_first"]
    check(r["waited_1"] and r["waited_2"] and r["release"] == "ok" and r["comment"] == "ok" and r["blockers"] == []
          and only(r["sent"], "main", CHANGED),
          f"the release, then the User's comment, queued on the row: both land, one main wake-up: {r}")
    c = got["race_comment_first"]
    check(only(c["sent"], "app", f"{CHANGED} director: external blocker released (operator:eric): approved"),
          f"the comment, then the release: its Director notice is moot, one app wake-up: {c['sent']}")
    p = got["probes"]
    check({k: p[k] for k in ("standing", "one_of_two", "other_operator", "released", "finished", "missing_ticket",
                             "no_operators")} == {"standing": "t", "one_of_two": "t", "other_operator": "f",
                                                  "released": "f", "finished": "f", "missing_ticket": "f",
                                                  "no_operators": "f"},
          f"the listener's question, asked as the listener: current only while a named blocker stands: {p}")
    check(p["resolved_row"] == "f" and not any(p["misaddressed"].values()),
          f"a resolved row is no wait, and a notice addressed past the Director is not delivered: {p}")
    check(p["queued_again"] == "0" and not any(p["again_sent"].values())
          and p["record"] == [[False, ["operator:eric"], False]] * 2 + [[True, ["operator:eric"], False]],
          f"one record per comment, three answers: raised again after its delivery, nothing is queued or sent: {p}")


def mirrors() -> None:
    """schema.sql's last definition of each of pgu1026's functions is pgu1026's, byte for byte, so fresh and upgraded
    boards agree. A later ticket's functions may follow it."""
    body = MIGRATION.read_text().split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;\n", 1)[0]
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    at = schema.rfind(body)
    heads = [f"CREATE OR REPLACE FUNCTION ticket_board.{name}(" for name in (
        "standing_operator_blockers", "notify_operator_response", "operator_response_is_current", "add_comment",
        "release_external_blocker")]
    check(at >= 0 and all(at <= schema.rfind(h) < at + len(body) for h in heads),
          "schema.sql's last copies of pgu1026's functions are pgu1026's, byte for byte")


def main() -> int:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) == 4 else None), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd725-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD725_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree))
        mirrors()
        candidate(run(ROOT))
        candidate(run(ROOT, main_tree))
    print(f"operator_response_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
