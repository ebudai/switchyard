#!/usr/bin/env python3
"""SYRD-726: the owner asks the Director to review a standing external blocker.

On SYRD-693 Eric approved the bounded canary and its timing; the Director kept operator:eric standing until the
canary was done. Main ran it, recorded the evidence, and could not tell the Director the blocker was ready to
release: request-dependency is refused while an external blocker stands, and Main's plain "please release" comment
notified nobody. The ticket stalled until Eric told the Director himself.

On production-built boards (router_role_board_test's: schema.sql, the real migrations, rbac.sql, the HTTP server and
the real listener, only the panes stood in for), with the real Unix-socket server under process authority for the
owner's registered pane; main first from a git archive, then this tree, then main's board upgraded:

- SYRD-693 as it ran: the User's approval reaches the Director (SYRD-725), who keeps the blocker; Main's plain
  comment reaches nobody. Here Main's review request is one Director notice with the reference and the reason, and
  nothing to Main; the blocker, stage, assignee and sign-offs are untouched. The release wakes Main once with its
  reason, and Main submits.
- Bounded: the same request again (from the pane, or the CLI) is the one already made, and a restarted listener sends
  nothing; a different one waits until the Director comments or records the blocker again.
- Released before the notice went out: dropped, and the owner told once, by the release. A release racing the
  request, both orders: one owner wake-up, no Director notice.
- Refused, changing nothing: another role's ticket, a ticket that is not the owner's active one, a deferred ticket, a
  blocker the ticket does not have, a blocker that is a ticket, no reason or too long a reason, an extra field, the
  HTTP port, the Director, a reviewer, a retired runtime.
- Privileges: the migration alone, before rbac.sql, lets no role execute its functions; rbac.sql then grants the
  service the request and the listener its question.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "98563090206f07a29ebbaa1223d89533129a055b"
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1027_syrd726_blocker_review.sql"
APPROVAL = "Approved: run the bounded canary now."
EVIDENCE = "Canary done at 14:02: exit 0, 3/3 probes green; log and sha256 on the ticket."
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Quiet:
    def notify_ticket_created(self, *a, **k) -> None: ...
    def notify_change(self, *a, **k) -> None: ...


def scenario(root: Path, upgrade_from: Path | None = None) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import http.client
    import socket as socket_mod

    import psycopg
    from board_stall_test import example, short
    from router_role_board_test import Board, build_board, fixture, migrate, temporary_cluster
    from scripts.ticket_board import notify_listener, write_cli
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from scripts.ticket_board.peer_identity import SessionIdentity, read_process
    from scripts.ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer

    assert Path(notify_listener.__file__).resolve().is_relative_to(root.resolve()), notify_listener.__file__
    out: dict = {}
    me, uid = os.getpid(), os.getuid()

    def to(sent, role: str) -> list[str]:
        return [m for t, m in sent if t == f"pgu-{role}:0.0"]

    def split(sent) -> dict:
        return {role: to(sent, role) for role in ("director", "main", "app", "ops")}

    def deliver(board) -> dict:
        """One listener pass, every pane idle: what reached which pane."""
        board.busy.clear()
        return split(board.one_pass())

    def state(board, t: str) -> dict:
        """Everything about a ticket a review request must not change."""
        return json.loads(board.sql(
            "SELECT json_build_object('state', t.state, 'assignee', t.assignee, 'reason', t.blocked_reason, "
            "'commit', t.commit_hash, 'audit', t.audit_signoff, 'user', t.user_signoff, 'flags', t.workflow_flags, "
            "'manual', t.manually_controlled, 'awaiting', coalesce(n.awaiting_role, ''), "
            "'blockers', (SELECT coalesce(json_agg(b.blocker_ticket_id || CASE WHEN b.resolved THEN ' resolved' "
            "ELSE '' END ORDER BY b.position), '[]') FROM ticket_board.ticket_blockers b WHERE b.ticket_id = t.id))"
            "::text FROM ticket_board.tickets t LEFT JOIN ticket_board.ticket_notification_state n "
            f"ON n.ticket_id = t.id WHERE t.id = '{t}';"))

    def last_comment(board, t: str) -> list:
        return json.loads(board.sql("SELECT json_build_array(who, text, urgent)::text FROM ticket_board.ticket_comments "
                                    f"WHERE ticket_id = '{t}' ORDER BY position DESC LIMIT 1;"))

    def reviews(board, t: str) -> list:
        if board.sql("SELECT to_regclass('ticket_board.ticket_blocker_reviews') IS NOT NULL;") != "t":
            return ["absent"]
        return json.loads(board.sql(
            "SELECT coalesce(json_agg(json_build_array(ref, requested_by, ended_at IS NULL) ORDER BY id), '[]')::text "
            f"FROM ticket_board.ticket_blocker_reviews WHERE ticket_id = '{t}';"))

    def traces(board, t: str) -> list:
        """What the listener did with each Director notice of a review request: [event, reason]."""
        return json.loads(board.sql(
            "SELECT coalesce(json_agg(json_build_array(event, coalesce(busy_reason, '')) ORDER BY id), '[]')::text "
            f"FROM ticket_board.notification_trace WHERE ticket_id = '{t}' AND event IN ('enqueue', 'send', 'drop') "
            "AND notification_id IN (SELECT notification_id FROM ticket_board.notification_trace WHERE event = "
            "'enqueue' AND detail->'payload'->>'kind' = 'blocker_review');"))

    def ticket(board, number: int, owner: str) -> str:
        """Routed from Analysis to its owner, the hand-over delivered."""
        t = f"PGU-{number}"
        fixture.seed_postgres_ticket(board.admin, t, title=f"Fixture {t}", state="analysis", assignee="director")
        out.setdefault("routes", {})[t] = short(board.act(t, "route", "director", state="in_progress", assignee=owner))
        out.setdefault("handed_over", {})[t] = len(deliver(board)[owner])
        return t

    def boundary(cluster, dbname: str, *, strip: bool) -> dict:
        """Main's board as deployed, then pgu1027 alone, as the runner applies one file: before its exit-time
        revocation and before rbac.sql. Who may execute each new function then, and after rbac.sql."""
        admin = build_board(cluster, dbname, before=upgrade_from)
        text = (root / MIGRATION.relative_to(ROOT)).read_text()
        if strip:  # the control: the same migration without its revocations
            text = "".join(l for l in text.splitlines(True) if not l.startswith("REVOKE EXECUTE ON FUNCTION"))
        fixture.psql(admin, text)
        roles = ("director", "user", "ops", "app", "main", "audit", fixture.SERVICE_ROLE, "ticket_board_listener")
        functions = ("end_blocker_reviews()", "request_blocker_review(text, text, text, bigint, bigint, bigint)",
                     "blocker_review_is_current(jsonb)")

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

    with temporary_cluster(prefix="syrd726-pg-") as cluster:
        if upgrade_from:
            out["boundary"] = boundary(cluster, "syrd726_boundary", strip=False)
            out["boundary_control"] = boundary(cluster, "syrd726_control", strip=True)
        board = Board(cluster, "syrd693", example(), before=upgrade_from)
        authority = ProcessRoleAuthority(board.app, "tenant", resolve_uid=lambda _a: uid,
                                         resolve_session=lambda pid: SessionIdentity(pid, read_process(pid).start_time),
                                         session_live=lambda _i: True)
        path = cluster.root / "syrd693.sock"
        unix = TicketBoardUnixServer(path, board.app, events=Quiet(), director_notifier=Quiet(), role_authority=authority)
        threading.Thread(target=unix.serve_forever, daemon=True).start()

        panes = board.sql("SELECT json_agg(to_jsonb(a))::text FROM ticket_board.role_runtime_assignments a;")

        def retire() -> None:
            """This process is no role's runtime: whichever role it stood in for has its own pane back."""
            board.sql(f"DELETE FROM ticket_board.role_runtime_assignments WHERE process_pid = {me} RETURNING role \\gset\n"
                      "INSERT INTO ticket_board.role_runtime_assignments SELECT * FROM jsonb_populate_recordset("
                      f"NULL::ticket_board.role_runtime_assignments, '{panes}'::jsonb) WHERE role = :'role' "
                      "ON CONFLICT DO NOTHING;" if board.sql("SELECT count(*) FROM ticket_board.role_runtime_assignments "
                                                             f"WHERE process_pid = {me};") != "0" else "SELECT 1;")

        def register(role: str) -> None:
            """This process is `role`'s live runtime, for the runtime and target the role declares."""
            retire()
            start = read_process(me).start_time
            board.sql(f"DELETE FROM ticket_board.role_runtime_assignments WHERE role = '{role}'; "
                      "INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, worktree, "
                      "session_dir, process_pid, process_start_time, process_uid) SELECT name, definition->>'runtime', "
                      f"definition->>'target', '/w', '/s', {me}, {start}, {uid} FROM ticket_board.workflow_roles "
                      f"WHERE name = '{role}';")

        def pane(t: str, payload: dict, role: str, operation: str = "request_blocker_review") -> list:
            """A request from this process's registered pane, over the board's socket: [status, body]."""
            class Connection(http.client.HTTPConnection):
                def connect(self) -> None:
                    self.sock = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
                    self.sock.connect(str(path))

            connection = Connection("localhost", timeout=60)
            try:
                connection.request("POST", f"/api/tickets/{t}/actions/{operation}", body=json.dumps(payload),
                                   headers={"Content-Type": "application/json", "X-Ticket-Board-Caller-Role": role})
                response = connection.getresponse()
                text = response.read().decode("utf-8", "replace")
            finally:
                connection.close()
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text[:400]
            if isinstance(body, dict) and response.status == 200:
                body = {k: body[k] for k in ("ref", "duplicate") if k in body}
            return [response.status, body if response.status == 200 else str(body)[:400]]

        def review(t: str, reason: str, role: str = "main", ref: str = "operator:eric") -> list:
            return pane(t, {"ref": ref, "reason": reason}, role)

        def cli(*argv: str) -> list:
            printed, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(errors):
                try:
                    code = write_cli.main(["--socket", str(path), "--caller-role", "main", *argv])
                except SystemExit as exc:
                    code = exc.code
                except Exception as exc:  # an answer, not a crash
                    code, _ = "raised", errors.write(str(exc))
            return [code, printed.getvalue().strip()[-300:], errors.getvalue().strip()[-300:]]

        try:
            if upgrade_from:
                out["migrations"] = board.sql("SELECT count(*) FROM ticket_board.schema_migrations "
                                              f"WHERE name = '{MIGRATION.name}';")
            # -- SYRD-693: Main asks for Eric; the Director records operator:eric; Eric approves; it is kept --------
            t = ticket(board, 693, "main")
            board.act(t, "request_dependency", "main", role="director", reason="Eric must approve the canary.")
            deliver(board)
            out["blocked"] = short(board.act(t, "set_blockers", "director", blocked_by=["operator:eric"],
                                             blocked_reason="Eric approves the canary and its timing; kept until it "
                                                            "is done."))
            deliver(board)
            out["approval"] = short(board.act(t, "add_comment", "user", text=APPROVAL))
            out["approval_sent"] = deliver(board)
            out["retain"] = short(board.act(t, "add_comment", "director",
                                            text="Approved. operator:eric stays until the canary's log is on the ticket."))
            out["retain_sent"] = deliver(board)
            # A blocked ticket gives up its owner's serial slot: it is no longer the reservation, yet it is the
            # owner's current work in implementation.
            out["reserved"] = board.sql("SELECT coalesce(ticket_board.ticket_current_reserved_ticket('main'), '');")
            # -- Main finishes the canary ---------------------------------------------------------------------------
            out["plain"] = short(board.act(t, "add_comment", "main", text="Canary done; please release operator:eric."))
            out["plain_sent"] = deliver(board)
            register("main")
            out["before"] = state(board, t)
            out["submit_blocked"] = short(board.act(t, "submit_to_audit_without_commit", "main", reason="canary done"))
            touched = board.sql(f"SELECT updated_at FROM ticket_board.tickets WHERE id = '{t}';")
            out["review"] = review(t, EVIDENCE)
            out["touched"] = board.sql(f"SELECT updated_at > '{touched}' FROM ticket_board.tickets WHERE id = '{t}';")
            out["review_sent"] = deliver(board)
            out["after"] = state(board, t)
            out["review_comment"] = last_comment(board, t)
            out["review_api"] = (board.ticket(t).get("comments") or [{}])[-1].get("text", "")
            out["review_payload"] = json.loads(board.sql(
                "SELECT coalesce((SELECT detail->'payload' FROM ticket_board.notification_trace WHERE event = 'enqueue' "
                f"AND ticket_id = '{t}' AND detail->'payload'->>'kind' = 'blocker_review' ORDER BY id LIMIT 1), "
                "'{}')::text;"))
            # -- replay and restart ---------------------------------------------------------------------------------
            out["replay"] = review(t, f"  {EVIDENCE} ")
            out["replay_cli"] = cli("request-blocker-review", t, "--ref", "OPERATOR:Eric", "--reason", EVIDENCE)
            board.listener = TicketBoardNotifyListener(
                conninfo=board.listener_url, sender=board.listener.sender, activity_gate=lambda x: x in board.busy,
                target_exists=lambda _x: True, submission_witness=lambda x, _s: any(y == x for y, _m in board.sent),
                poll_seconds=0, project="pgu")
            out["replay_sent"] = deliver(board)
            # -- something new before the Director acts: it waits ----------------------------------------------------
            board.act(t, "add_comment", "main", text="The canary's log is also kept in its own directory.")
            out["pending"] = review(t, "Also: the probe log is attached.")
            out["pending_sent"] = deliver(board)
            # -- the Director keeps it and asks for more; Main answers -----------------------------------------------
            out["ask_more"] = short(board.act(t, "add_comment", "director", text="Keeping it: add the log's sha256."))
            out["ask_more_sent"] = deliver(board)
            out["second"] = review(t, EVIDENCE + " sha256 4f2a...e9 in the comment above.")
            out["second_sent"] = deliver(board)
            out["second_replay"] = review(t, EVIDENCE + " sha256 4f2a...e9 in the comment above.")
            out["reviews"] = reviews(board, t)
            # -- the Director releases it -----------------------------------------------------------------------------
            out["release"] = short(board.act(t, "release_external_blocker", "director", ref="operator:eric",
                                             reason="the canary passed; its log and sha256 are on the ticket"))
            out["release_sent"] = deliver(board)
            out["reviews_after_release"] = reviews(board, t)
            out["after_release"] = review(t, EVIDENCE)
            out["submit"] = short(board.act(t, "submit_to_audit_without_commit", "main", reason="canary done"))
            out["traces"] = traces(board, t)

            # -- refusals, on app's ticket ---------------------------------------------------------------------------
            other = ticket(board, 700, "app")
            fixture.seed_postgres_ticket(board.admin, "PGU-701", title="Fixture PGU-701", state="analysis",
                                         assignee="director")
            board.act(other, "set_blockers", "director", blocked_by=["operator:eric", "PGU-701"],
                      blocked_reason="Eric's key, and PGU-701")
            deliver(board)
            out["refusal_before"] = state(board, other)
            refused: dict = {}
            register("main")
            refused["others_ticket"] = review(other, EVIDENCE, "main")
            register("app")
            refused["absent_ref"] = review(other, EVIDENCE, "app", "operator:alice")
            refused["ticket_ref"] = review(other, EVIDENCE, "app", "PGU-701")
            refused["malformed_ref"] = review(other, EVIDENCE, "app", "eric")
            refused["no_reason"] = review(other, "   ", "app")
            refused["long_reason"] = review(other, "x" * 2001, "app")
            refused["extra_field"] = pane(other, {"ref": "operator:eric", "reason": EVIDENCE, "release": True}, "app")
            refused["not_text"] = pane(other, {"ref": ["operator:eric"], "reason": EVIDENCE}, "app")
            refused["http_port"] = short(board.act(other, "request_blocker_review", "app", ref="operator:eric",
                                                   reason=EVIDENCE))
            register("director")
            refused["director"] = review(other, EVIDENCE, "director")
            fixture.seed_postgres_ticket(board.admin, "PGU-704", title="Fixture PGU-704", state="analysis",
                                         assignee="director")
            board.act("PGU-704", "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="Eric decides")
            refused["own_triage"] = review("PGU-704", EVIDENCE, "director")
            register("audit")
            refused["audit"] = review(other, EVIDENCE, "audit")
            register("app")
            retire()  # app's own pane is back; this process is not it
            refused["retired"] = review(other, EVIDENCE, "app")
            # The database alone, asked by the board's writer for app with this process, which is not app's pane.
            try:
                with psycopg.connect(board.service) as conn:
                    conn.execute("SELECT set_config('ticket_board.caller_role', 'app', false)")
                    conn.execute("SELECT ticket_board.request_blocker_review(%s, 'operator:eric', 'x', %s, %s, %s)",
                                 (other, me, read_process(me).start_time, uid))
                refused["db_retired"] = "ok"
            except Exception as exc:  # a refusal is an answer
                refused["db_retired"] = str(exc).splitlines()[0][:300]
            # A second ticket of app's, to defer below: deferred work is no one's.
            queued = ticket(board, 702, "app")
            board.act(queued, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="Eric")
            register("app")
            out["refused"] = refused
            out["refused_sent"] = deliver(board)
            out["refusal_after"] = state(board, other)
            out["refusal_reviews"] = reviews(board, other) + reviews(board, queued)

            # -- the blocker recorded again ends a pending request -----------------------------------------------------
            out["first_other"] = review(other, "Eric's key is on the share.", "app")
            out["first_other_sent"] = deliver(board)
            out["pending_other"] = review(other, "And the fingerprint matches.", "app")
            out["rerecord"] = short(board.act(other, "set_blockers", "director", blocked_by=["operator:eric", "PGU-701"],
                                              blocked_reason="Eric's key (checking the fingerprint), and PGU-701"))
            out["rerecord_sent"] = deliver(board)
            out["after_rerecord"] = review(other, "And the fingerprint matches.", "app")
            # -- released before the Director's notice went out -------------------------------------------------------
            out["stale_release"] = short(board.act(other, "release_external_blocker", "director", ref="operator:eric",
                                                   reason="fingerprint matches"))
            out["stale_sent"] = deliver(board)
            out["stale_traces"] = traces(board, other)
            # A deferred ticket is no one's active work.
            out["defer"] = short(board.act(queued, "defer", "director"))
            board.act(other, "defer", "director")
            deliver(board)
            out["deferred"] = review(queued, EVIDENCE, "app")

            # -- a release and the owner's request at once, both orders -------------------------------------------
            def waiting(n: int) -> bool:
                for _ in range(200):
                    if board.sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                 "AND datname = current_database();") == str(n):
                        return True
                    time.sleep(0.05)
                return False

            def race(number: int, owner: str, first: str) -> dict:
                rt = ticket(board, number, owner)
                board.act(rt, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="Eric")
                deliver(board)
                register(owner)
                answers: dict = {}
                moves = {"release": lambda: answers.update(release=short(board.act(
                             rt, "release_external_blocker", "director", ref="operator:eric", reason="done"))),
                         "request": lambda: answers.update(request=review(rt, EVIDENCE, owner))}
                with psycopg.connect(board.admin) as conn:
                    conn.execute(f"SELECT 1 FROM ticket_board.tickets WHERE id = '{rt}' FOR UPDATE")
                    threads = []
                    for n, move in enumerate((first, "request" if first == "release" else "release"), 1):
                        threads.append(threading.Thread(target=moves[move]))
                        threads[-1].start()
                        answers[f"waited_{n}"] = waiting(n)
                for thread in threads:
                    thread.join(60)
                answers["sent"] = deliver(board)
                answers["blockers"] = state(board, rt)["blockers"]
                return answers

            # Each owner holds one active ticket: ops, which has had none, and app, whose tickets are deferred.
            out["race_release_first"] = race(710, "ops", "release")
            out["race_request_first"] = race(711, "app", "request")

            # -- two external blockers on one ticket: a request each is a notice each ------------------------------
            two = "PGU-710"
            board.act(two, "set_blockers", "director", blocked_by=["operator:eric", "pie:PIE-17"],
                      blocked_reason="Eric's sign-off, and PIE-17's export")
            deliver(board)
            register("ops")
            out["two_requests"] = [review(two, "Eric signed off on the ticket.", "ops"),
                                   review(two, "PIE-17's export is published.", "ops", "pie:PIE-17")]
            out["two_sent"] = []
            for _ in range(3):
                out["two_sent"].append(len(deliver(board)["director"]))
                board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                          "WHERE claimed_at IS NULL;")
            # -- the blockers recorded again before the notice went out: the request is over, the notice dropped -----
            board.act(two, "add_comment", "director", text="Keeping both for now.")
            deliver(board)
            out["before_rerecord"] = review(two, "Eric's sign-off is in his own comment, with the time.", "ops")
            out["early_rerecord"] = short(board.act(two, "set_blockers", "director",
                                                    blocked_by=["operator:eric", "pie:PIE-17"],
                                                    blocked_reason="Eric's sign-off (checking it), and PIE-17's export"))
            out["early_rerecord_sent"] = deliver(board)
            out["two_traces"] = traces(board, two)
            # An open request's notice, addressed to anyone but the Director, is not delivered.
            out["open_again"] = review(two, "PIE-17's export is published, checksum on the ticket.", "ops", "pie:PIE-17")
            if reviews(board, two) != ["absent"]:
                board.sql("SELECT ticket_board.enqueue_notification(t.id, 'ticket_update', 'ops', 'misaddressed', "
                          "jsonb_build_object('kind', 'blocker_review', 'id', t.id, 'review', max(r.id)), "
                          "'misaddressed') FROM ticket_board.tickets t JOIN ticket_board.ticket_blocker_reviews r "
                          f"ON r.ticket_id = t.id WHERE t.id = '{two}' GROUP BY t.id;")
            out["misaddressed"] = deliver(board)

            # -- the listener's question, asked as the listener --------------------------------------------------
            if board.sql("SELECT to_regprocedure('ticket_board.blocker_review_is_current(jsonb)') IS NOT NULL;") == "t":
                def current(payload: dict) -> str:
                    return board.sql(f"SELECT ticket_board.blocker_review_is_current('{json.dumps(payload)}'::jsonb);",
                                     board.listener_url)
                ended = board.sql("SELECT min(id) FROM ticket_board.ticket_blocker_reviews;") or "0"
                out["probes"] = {"ended": current({"review": int(ended)}), "missing": current({"review": 999999}),
                                 "none": current({})}
            else:
                out["probes"] = "absent"
        finally:
            unix.shutdown()
            unix.server_close()
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


def none(sent: dict) -> bool:
    return not any(sent.values())


def common(got: dict) -> None:
    """What main and this tree agree on: SYRD-693's wait as it ran, and the release."""
    check(set(got["routes"].values()) == {"ok"} and set(got["handed_over"].values()) == {1},
          f"each ticket is routed and handed over: {got['routes']} {got['handed_over']}")
    check(got["blocked"] == "ok" and got["approval"] == "ok"
          and only(got["approval_sent"], "director", "is blocked by operator:eric, and the User answered on it"),
          f"Eric's approval reaches the Director (SYRD-725): {got['approval_sent']}")
    check(got["retain"] == "ok" and only(got["retain_sent"], "main", f"{CHANGED} director: new comment"),
          f"the Director keeps operator:eric and says so: {got['retain_sent']}")
    check(got["reserved"] == "", f"a blocked ticket gives up its owner's serial slot: {got['reserved']!r}")
    check(got["plain"] == "ok" and none(got["plain_sent"]),
          f"Main's plain 'please release' comment reaches nobody: {got['plain_sent']}")
    check("unresolved blocker prevents forward promotion: operator:eric" in got["submit_blocked"],
          f"Main cannot submit while it stands: {got['submit_blocked']}")
    check(got["release"] == "ok" and only(got["release_sent"], "main", f"{CHANGED} director: external blocker "
                                          "released (operator:eric): the canary passed")
          and got["submit"] == "ok",
          f"the release wakes Main once with its reason, and Main submits: {got['release_sent']} {got['submit']}")
    if "migrations" in got:
        check(got["migrations"] == "1", f"main's board takes this tree's migration once: {got['migrations']}")


def before(got: dict) -> None:
    """Main: Main has no way to tell the Director the retained blocker is ready."""
    common(got)
    check(got["review"][0] != 200 and none(got["review_sent"]) and got["after"] == got["before"],
          f"main: there is no review request, and nothing reaches the Director: {got['review']} {got['review_sent']}")
    check(got["reviews"] == ["absent"] and got["probes"] == "absent", "main has no review record")


def candidate(got: dict) -> None:
    common(got)
    check(got["review"] == [200, {"ref": "operator:eric", "duplicate": False}]
          and only(got["review_sent"], "director", 'PGU-693 -- Fixture PGU-693 (in in_progress): main asks you to '
                   f'review operator:eric, which still blocks it: "{EVIDENCE}" Release it with the result (that tells '
                   "main once), keep it and say why in a comment, or replace it. Nothing was released or moved."),
          f"Main's request is one Director notice with the blocker and the reason, and nothing to Main: "
          f"{got['review']} {got['review_sent']}")
    check(got["after"] == got["before"], f"the blocker, stage, assignee and sign-offs are untouched: {got['after']}")
    check(got["touched"] == "t", f"the ticket shows it was updated, as a comment does: {got['touched']}")
    check(got["review_comment"] == ["main", f"Review of operator:eric requested: {EVIDENCE}", False]
          and got["review_api"] == f"Review of operator:eric requested: {EVIDENCE}",
          f"the request is on the ticket as Main's comment, as the board shows it: {got['review_comment']} "
          f"{got['review_api']!r}")
    pay = got["review_payload"]
    check({k: pay.get(k) for k in ("kind", "id", "target_role", "ref", "requested_by", "reason")} == {
        "kind": "blocker_review", "id": "PGU-693", "target_role": "director", "ref": "operator:eric",
        "requested_by": "main", "reason": EVIDENCE} and isinstance(pay.get("review"), int),
          f"the queued notice records the blocker, the reason and who asked: {pay}")
    check(got["replay"] == [200, {"ref": "operator:eric", "duplicate": True}] and got["replay_cli"][0] == 0
          and '"duplicate": true' in got["replay_cli"][1] and none(got["replay_sent"]),
          f"the same request again, from the pane or the CLI, and a restarted listener: nothing more: {got['replay']} "
          f"{got['replay_cli']} {got['replay_sent']}")
    check(got["pending"][0] == 400 and "the Director has not acted on it" in got["pending"][1]
          and none(got["pending_sent"]), f"something new before the Director acts waits: {got['pending']}")
    check(got["ask_more"] == "ok" and only(got["ask_more_sent"], "main", f"{CHANGED} director: new comment")
          and got["second"] == [200, {"ref": "operator:eric", "duplicate": False}]
          and only(got["second_sent"], "director", "sha256 4f2a...e9 in the comment above.")
          and got["second_replay"] == [200, {"ref": "operator:eric", "duplicate": True}],
          f"after the Director comments, a new request notifies once more: {got['second_sent']}")
    check(got["reviews"] == [["operator:eric", "main", True]] * 2
          and got["reviews_after_release"] == [["operator:eric", "main", False]] * 2,
          f"two requests on record, both ended by the release: {got['reviews']} {got['reviews_after_release']}")
    check(got["after_release"][0] == 400 and "has no standing external blocker operator:eric" in got["after_release"][1],
          f"once released there is nothing to review: {got['after_release']}")
    check(got["traces"] == [["enqueue", ""], ["send", "idle"]] * 2, f"each notice sent once: {got['traces']}")
    r = got["refused"]
    words = {"others_ticket": "main may ask for a blocker review only of a ticket it owns in implementation; PGU-700 is "
                              "in_progress/app",
             "absent_ref": "PGU-700 has no standing external blocker operator:alice: name one of operator:eric, PGU-701",
             "ticket_ref": "PGU-700 has no standing external blocker PGU-701",
             "malformed_ref": "PGU-700 has no standing external blocker ERIC",
             "no_reason": "a blocker review needs the reason it is ready",
             "long_reason": "a blocker review needs the reason it is ready",
             "extra_field": "names a blocker and a reason only, not release",
             "not_text": "takes ref (the standing external blocker) and reason, both as text",
             "director": "director may ask for a blocker review only of a ticket it owns in implementation",
             "audit": "audit may ask for a blocker review only of a ticket it owns in implementation",
             "own_triage": "director may ask for a blocker review only of a ticket it owns in implementation; "
                           "PGU-704 is analysis/director",
             "retired": "this live pane has no PostgreSQL role assignment"}
    check(all(r[k][0] != 200 and w in r[k][1] for k, w in words.items()),
          f"refused from the socket, each for its own reason: {r}")
    check("403" in r["http_port"] and "only from the owning role's own pane" in r["http_port"]
          and "is not app's registered live runtime" in r["db_retired"],
          f"the HTTP port, and the database asked for a process that is not app's pane: {r['http_port']} "
          f"{r['db_retired']}")
    check(only(got["refused_sent"], "app", "PGU-702") and got["refusal_after"] == got["refusal_before"]
          and got["refusal_reviews"] == [], f"refusals change nothing and tell no one: {got['refused_sent']} "
          f"{got['refusal_after']} {got['refusal_reviews']}")
    check(got["first_other"][0] == 200 and only(got["first_other_sent"], "director", "app asks you to review operator:eric")
          and "the Director has not acted on it" in got["pending_other"][1]
          and got["rerecord"] == "ok" and only(got["rerecord_sent"], "app", f"{CHANGED} director: blockers")
          and got["after_rerecord"] == [200, {"ref": "operator:eric", "duplicate": False}],
          f"the blocker recorded again ends a pending request: {got['pending_other']} {got['after_rerecord']}")
    check(only(got["stale_sent"], "app", "external blocker released (operator:eric): fingerprint matches")
          and got["stale_traces"] == [["enqueue", ""], ["send", "idle"], ["enqueue", ""], ["drop", "stale_notification"]],
          f"released before delivery: the notice dropped, the owner told once: {got['stale_sent']} {got['stale_traces']}")
    check(got["defer"] == "ok" and "only of a ticket it owns in implementation; PGU-702 is backlog/unassigned"
          in got["deferred"][1], f"a deferred ticket is no one's to ask about: {got['deferred']}")
    a, b = got["race_release_first"], got["race_request_first"]
    check(a["waited_1"] and a["waited_2"] and a["release"] == "ok" and "has no standing external blocker" in a["request"][1]
          and only(a["sent"], "ops", "external blocker released (operator:eric): done") and a["blockers"] == [],
          f"the release, then the request, queued on the row: the request finds nothing, one ops wake-up: {a}")
    check(b["waited_1"] and b["waited_2"] and b["request"][0] == 200 and b["release"] == "ok"
          and only(b["sent"], "app", "external blocker released (operator:eric): done") and b["blockers"] == [],
          f"the request, then the release: its Director notice is moot, one app wake-up: {b}")
    check(got["two_requests"] == [[200, {"ref": "operator:eric", "duplicate": False}],
                                  [200, {"ref": "pie:PIE-17", "duplicate": False}]] and got["two_sent"] == [1, 1, 0],
          f"two external blockers, a request each: two Director notices: {got['two_requests']} {got['two_sent']}")
    check(got["before_rerecord"][0] == 200 and got["early_rerecord"] == "ok"
          and only(got["early_rerecord_sent"], "ops", f"{CHANGED} director: blockers")
          and got["two_traces"][-2:] == [["enqueue", ""], ["drop", "stale_notification"]],
          f"the blockers recorded again before delivery: the request is over and its notice dropped: "
          f"{got['early_rerecord_sent']} {got['two_traces']}")
    check(got["open_again"] == [200, {"ref": "pie:PIE-17", "duplicate": False}]
          and only(got["misaddressed"], "director", "ops asks you to review pie:PIE-17"),
          f"an open request's notice reaches the Director, and a copy addressed to ops does not: {got['misaddressed']}")
    check(got["probes"] == {"ended": "f", "missing": "f", "none": "f"},
          f"the listener's question: an ended, missing or unnamed request is not current: {got['probes']}")
    if "boundary" in got:
        x, c = got["boundary"], got["boundary_control"]
        nobody = {"end_blocker_reviews": [], "request_blocker_review": [], "blocker_review_is_current": []}
        check(x["window"] == nobody, f"pgu1027 alone, before rbac.sql: no role may execute its functions: {x['window']}")
        check(x["after_rbac"] == {**nobody, "request_blocker_review": ["ticket_board_service"],
                                  "blocker_review_is_current": ["ticket_board_listener"]},
              f"after rbac.sql: the service may ask, the listener may check: {x['after_rbac']}")
        check(all("app" in c["window"][f] and "ops" in c["window"][f] for f in nobody)
              and c["after_rbac"] == x["after_rbac"],
              f"the control, without the revocations: pane roles may execute them until rbac.sql: {c}")


def mirrors() -> None:
    """schema.sql's last definition of each of pgu1027's functions is pgu1027's, byte for byte. A later ticket's may
    follow it."""
    body = MIGRATION.read_text().split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;\n", 1)[0]
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    at = schema.rfind(body)
    heads = [f"CREATE OR REPLACE FUNCTION ticket_board.{name}(" for name in (
        "end_blocker_reviews", "request_blocker_review", "blocker_review_is_current")]
    check(at >= 0 and all(at <= schema.rfind(h) < at + len(body) for h in heads),
          "schema.sql's last copies of pgu1027's functions are pgu1027's, byte for byte")


def main() -> int:
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) == 4 else None), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd726-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD726_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree))
        mirrors()
        candidate(run(ROOT))
        candidate(run(ROOT, main_tree))
    print(f"blocker_review_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
