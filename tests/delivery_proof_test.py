#!/usr/bin/env python3
"""SYRD-565: a notice is delivered when it left the composer and a turn took it -- not before, and not only inside 15 s.

On otto (release 2951c5c9) four "receipt not witnessed" notices were real turns
whose first act, a slow preflight compression, started 15-16 s after the send:
past the listener's fixed witness window, so they were recorded unconfirmed for
good. And a nudge was typed into a Hermes composer and never submitted, for 50
minutes, while the board believed it delivered.

Production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
with the example workflow, the real HTTP server, the real notify listener and
the real PaneActivityGate and hook-state store. Only tmux is stood in for: a
pane whose composer the sender types into, and whose recipient's hooks the
scenario writes. The before-run is main's code and SQL, in a child built from a
git archive.
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
BEFORE = "98ec343f24696c985a08a5eebbbc6114b23e51eb"  # main before SYRD-565
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
RULE = "─" * 100
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Pane:
    """One recipient's pane: what its composer holds, and its cursor there."""

    def __init__(self) -> None:
        self.composer = ""

    def rows(self) -> tuple[list[str], int, int]:
        lines = ["", "  the last answer, finished", "", RULE, "❯\xa0" + self.composer, RULE, "  ⏵⏵ status"]
        return lines + [""] * 13, 2 + len(self.composer), 4


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix="s565", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd565-state.") as state_dir:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        panes = {f"pgu-{role}:0.0": Pane() for role in ("main", "app", "ops", "director")}
        store = nl.PaneHookStateStore(Path(state_dir))
        for target in panes:
            store.write(target, "idle", source="claude.Stop", now=time.time() - 120)

        def tmux(argv, **_kwargs):
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            pane = panes.get(target)
            if pane is None:
                raise AssertionError(f"no fake pane for {target!r}: {argv}")
            if argv[:2] == ["tmux", "capture-pane"]:
                lines, _x, _y = pane.rows() if pane else ([], 0, 0)
                return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                if "cursor_x" in fmt:
                    _lines, x, y = pane.rows()
                    return subprocess.CompletedProcess(argv, 0, f"{x} {y} 20\n", "")
                if fmt == "#{pane_pid}":
                    return subprocess.CompletedProcess(argv, 0, "100\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")

        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, process_table_reader=lambda: [], sleeper=lambda _s: None,
                                   role_runtimes={role: "claude" for role in ("main", "app", "ops", "director")})
        sends: list[tuple[str, str]] = []
        behaviour: dict[str, str] = {}

        def turn_starts(target: str) -> None:
            store.write(target, "busy", source="claude.UserPromptSubmit")

        def directorctl(target: str, message: str) -> dict:
            """Types the text, then does what this recipient's composer does with it."""
            sends.append((target, message))
            pane = panes[target]
            pane.composer = message
            how = behaviour.get(target, "submits")
            if how in ("submits", "submits_slowly"):
                pane.composer = ""
                if how == "submits":
                    turn_starts(target)
            elif how == "fails_after_typing":
                raise subprocess.CalledProcessError(1, ["directorctl", "send"], "",
                                                    f"warning: {target} did not show a submission transition")
            return {}

        presses: list[str] = []
        press_behaviour: dict[str, str] = {}

        def submit(target: str) -> None:
            presses.append(target)
            if press_behaviour.get(target) == "takes":
                panes[target].composer = ""
                turn_starts(target)

        def new_listener():
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=gate.is_working,
                target_exists=lambda _target: True, poll_seconds=0, submission_confirm_seconds=0.4,
                submission_poll_seconds=0.05)
            listener.delivery_submitter = submit
            return listener

        def one_pass(listener) -> None:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                try:
                    from scripts.ticket_board import delivery_proof
                except ImportError:  # main has none
                    delivery_proof = None
                if delivery_proof is not None:
                    delivery_proof.run(listener, conn)
                listener.process_due_notifications(conn)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def queued(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, target_role, coalesce(last_error, ''), "
                                  "dead_lettered_at IS NOT NULL) ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}';"))

        def events(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(event, coalesce(busy_reason, '')) ORDER BY id), '[]')::text "
                                  f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' "
                                  "AND event IN ('send', 'send_unconfirmed', 'send_unsubmitted', 'send_failed', 'submitted', 'ack');"))

        def proof(ticket: str):
            try:
                return sql("SELECT coalesce(string_agg(state || '/' || submit_attempts || '/' || (director_told_at IS NOT NULL), ','), '') "
                           f"FROM ticket_board.notification_proofs WHERE ticket_id = '{ticket}';")
            except AssertionError:
                return None  # main has no such table

        def delivery(ticket: str) -> dict:
            import urllib.request
            with urllib.request.urlopen(f"{base}/api/tickets/{ticket}") as response:
                return json.loads(response.read()).get("active_work_delivery") or {}

        def director_notices(ticket: str) -> list:
            """Proof notices the Director was sent, or that still wait for the Director."""
            waiting = json.loads(sql("SELECT coalesce(json_agg(message ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                     f"WHERE ticket_id = '{ticket}' AND target_role = 'director' AND payload->>'kind' = 'delivery_proof';"))
            return [m for t, m in sends if t == "pgu-director:0.0" and m.startswith(f"{ticket}: ")] + waiting

        def finish(ticket: str, target: str) -> None:
            """Close the role's ticket so the next one is its active work (SYRD-568), and settle its pane."""
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")
            panes[target].composer = ""
            behaviour.pop(target, None)
            store.write(target, "idle", source="claude.Stop", now=time.time() - 1)

        def sends_to(target: str) -> int:
            return sum(1 for t, _m in sends if t == target)

        # 1. An ordinary notice: typed, submitted, taken at once.
        route("PGU-701", "main")
        one_pass(new_listener())
        seen["normal"] = [events("PGU-701"), queued("PGU-701"), proof("PGU-701"), delivery("PGU-701").get("state"),
                          sends_to("pgu-main:0.0")]
        finish("PGU-701", "pgu-main:0.0")

        # 2. A slow preflight: the text left the composer, and the turn starts
        #    only after the short window has passed.
        behaviour["pgu-app:0.0"] = "submits_slowly"
        route("PGU-702", "app")
        one_pass(new_listener())
        seen["slow_at_window"] = [events("PGU-702"), queued("PGU-702"), proof("PGU-702"), delivery("PGU-702")]
        # A pass while the preflight is still running: inside the horizon, no
        # turn yet -- still pending, nobody told.
        one_pass(new_listener())
        seen["slow_still_pending"] = [proof("PGU-702"), director_notices("PGU-702")]
        turn_starts("pgu-app:0.0")
        one_pass(new_listener())  # a listener restarted since
        one_pass(new_listener())
        seen["slow_after_turn"] = [events("PGU-702"), proof("PGU-702"), delivery("PGU-702").get("state"),
                                   sends_to("pgu-app:0.0")]

        # 2b. Submitted and never taken: reported once the horizon passes.
        finish("PGU-702", "pgu-app:0.0")
        behaviour["pgu-app:0.0"] = "submits_slowly"
        route("PGU-703", "app")
        one_pass(new_listener())
        try:
            sql("UPDATE ticket_board.notification_proofs SET horizon_at = clock_timestamp() - interval '1 second' "
                "WHERE ticket_id = 'PGU-703';")
        except AssertionError:
            pass
        one_pass(new_listener())
        one_pass(new_listener())
        seen["missed"] = [events("PGU-703"), proof("PGU-703"), delivery("PGU-703"), director_notices("PGU-703")]
        finish("PGU-703", "pgu-app:0.0")

        # 3. The text stays in the composer (otto's Hermes nudge).
        behaviour["pgu-ops:0.0"] = "stays_in_composer"
        route("PGU-704", "ops")
        one_pass(new_listener())
        seen["unsubmitted_at_send"] = [events("PGU-704"), queued("PGU-704"), proof("PGU-704"), delivery("PGU-704"),
                                       panes["pgu-ops:0.0"].composer[:12]]
        # A restarted listener presses submit -- typing nothing -- and it takes.
        press_behaviour["pgu-ops:0.0"] = "takes"
        one_pass(new_listener())
        one_pass(new_listener())
        seen["unsubmitted_after_press"] = [events("PGU-704"), queued("PGU-704"), proof("PGU-704"),
                                           delivery("PGU-704").get("state"), sends_to("pgu-ops:0.0"), presses.count("pgu-ops:0.0")]

        # 3b. Submit never takes: pressed a bounded number of times, the Director
        #     told once, and the notice never acknowledged or typed again.
        finish("PGU-704", "pgu-ops:0.0")
        behaviour["pgu-ops:0.0"] = "stays_in_composer"
        press_behaviour["pgu-ops:0.0"] = "ignored"
        presses.clear()
        route("PGU-705", "ops")
        for _ in range(6):
            one_pass(new_listener())
        seen["stuck"] = [queued("PGU-705"), proof("PGU-705"), director_notices("PGU-705"),
                         sum(1 for t, m in sends if m.startswith("PGU-705 ")), presses.count("pgu-ops:0.0")]

        # 3c. Somebody edits text left in the composer: it is theirs; submit is never pressed on it.
        behaviour["pgu-main:0.0"] = "stays_in_composer"
        route("PGU-706", "main")
        one_pass(new_listener())
        panes["pgu-main:0.0"].composer += " -- and something the User added"
        presses.clear()
        press_behaviour["pgu-main:0.0"] = "takes"
        one_pass(new_listener())
        one_pass(new_listener())
        seen["edited"] = [proof("PGU-706"), presses.count("pgu-main:0.0"), director_notices("PGU-706"), queued("PGU-706"),
                          sum(1 for t, m in sends if m.startswith("PGU-706 "))]

        # 4. directorctl fails after typing (its own submit check gave up).
        behaviour["pgu-app:0.0"] = "fails_after_typing"
        route("PGU-707", "app")
        for _ in range(3):
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE ticket_id = 'PGU-707' AND last_error IS DISTINCT FROM 'unsubmitted_in_composer';")
            one_pass(new_listener())
        seen["failed_after_typing"] = [events("PGU-707"), queued("PGU-707"), proof("PGU-707"),
                                       sum(1 for _t, m in sends if m.startswith("PGU-707 "))]

        # 5. Superseded while unsubmitted: the ticket is cancelled with its text
        #    still in app's composer. Submitting it would tell app something no
        #    longer true: it is discarded, never pressed, and the Director told.
        presses_before = presses.count("pgu-app:0.0")
        fixture.post_json(base, "/api/tickets/PGU-707/actions/force_move", {"state": "cancelled", "assignee": "unassigned"},
                          caller="director")
        one_pass(new_listener())
        one_pass(new_listener())
        seen["superseded"] = [queued("PGU-707"), proof("PGU-707"), presses.count("pgu-app:0.0") - presses_before,
                              director_notices("PGU-707"), panes["pgu-app:0.0"].composer[:12]]

        # 6. Somebody else submits the text left in main's composer (PGU-706,
        #    edited and so never pressed by the board) and the turn starts: it
        #    is in -- acknowledged and delivered, and never typed again.
        panes["pgu-main:0.0"].composer = ""
        turn_starts("pgu-main:0.0")
        one_pass(new_listener())
        seen["submitted_by_someone"] = [queued("PGU-706"), proof("PGU-706"), events("PGU-706")[-1],
                                        sum(1 for t, m in sends if m.startswith("PGU-706 "))]

        # 7. Somebody clears the text and nothing took it: the composer is empty,
        #    so typing it again is safe -- and it is.
        panes["pgu-app:0.0"].composer = ""
        store.write("pgu-app:0.0", "idle", source="claude.Stop", now=time.time() - 1)
        behaviour["pgu-app:0.0"] = "stays_in_composer"
        route("PGU-709", "app")
        one_pass(new_listener())
        panes["pgu-app:0.0"].composer = ""
        one_pass(new_listener())
        one_pass(new_listener())
        seen["cleared"] = [proof("PGU-709"), sum(1 for t, m in sends if m.startswith("PGU-709 ")),
                           [e for e in events("PGU-709") if e[0] == "send_unsubmitted"]]

        # Who may run the new functions, and a fresh schema.sql board installs the same text.
        new = ("record_unsubmitted_notification(bigint, text, timestamp with time zone, text, interval)",
               "watch_notification_receipt(bigint, text, timestamp with time zone, timestamp with time zone, interval)",
               "open_notification_proofs()", "note_notification_submit_attempt(bigint)",
               "resolve_notification_proof(bigint, text, text, text)")
        try:
            seen["grantees"] = {f: sql("SELECT string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, '+' "
                                       "ORDER BY CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END) "
                                       f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{f}'::regprocedure "
                                       "AND a.privilege_type = 'EXECUTE';") for f in new}
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            definition = "SELECT md5(pg_get_functiondef('ticket_board.{}'::regprocedure));"
            seen["fresh_equals_migrated"] = {f: fixture.psql(fresh, definition.format(f)).strip() == sql(definition.format(f)) for f in new}
        except AssertionError as exc:  # main has none of them
            seen["grantees"] = seen["fresh_equals_migrated"] = f"unavailable: {str(exc)[-120:]}"
        server.shutdown()
        server.server_close()
    return seen


def composer_reads() -> dict:
    """pane_composer.composer_text on the shapes a send can leave behind."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board.pane_composer import composer_text

    return {
        "empty box": composer_text([RULE, "❯\xa0", RULE], 2, 1),
        "placeholder right of the cursor": composer_text([RULE, "❯\xa0Try \"fix the failing test\"", RULE], 2, 1),
        "typed, cursor at its end": composer_text([RULE, "❯\xa0PGU-1 -- a notice", RULE], 20, 1),
        "multiline draft, empty current row": composer_text([RULE, "❯\xa0first", "  second", "", RULE], 2, 3),
        "Hermes redraw residue": composer_text(["out", "", "❯ ❯ ❯ ❯ "], 8, 2),
        "Hermes prompted draft": composer_text(["out", "❯ first", "❯ second"], 8, 2),
        "output above an empty prompt": composer_text(["• Ran the suite", "› "], 2, 1),
        "cursor below a short capture": composer_text([RULE, "❯\xa0first"], 2, 3),
        "negative cursor": composer_text([RULE], -1, 0),
    }


def real_directorctl_submit() -> list[str]:
    """The shipped default once: DirectorctlSender.submit through the installed directorctl, against a recording tmux."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board.notification_dispatch import DirectorctlSender

    with tempfile.TemporaryDirectory(prefix="syrd565-directorctl.") as tmp:
        tmp_path = Path(tmp)
        installed = tmp_path / "bin" / "directorctl"
        env = {"PATH": f"{tmp}:/usr/bin:/bin", "HOME": tmp, "LANG": "C.UTF-8", "TMUX_LOG_PATH": f"{tmp}/tmux.log"}
        subprocess.run([str(ROOT / "scripts/install-directorctl")], check=True, capture_output=True,
                       env={**env, "DIRECTORCTL_INSTALL_PATH": str(installed)})
        (tmp_path / "tmux").write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = display-message ] && [ "${*: -1}" = "#{pane_in_mode}" ]; then echo 0; exit 0; fi\n'
            'printf "%s\\n" "$*" >>"$TMUX_LOG_PATH"\n')
        (tmp_path / "tmux").chmod(0o755)
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            DirectorctlSender(str(installed)).submit("pgu-ops:0.0")
        except subprocess.CalledProcessError as exc:  # an answer, so the check can say so
            return [f"failed: {exc}"]
        finally:
            os.environ.clear()
            os.environ.update(saved)
        log = tmp_path / "tmux.log"
        return log.read_text().splitlines() if log.exists() else []


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd565.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    after = json.loads(json.dumps(scenario(ROOT), default=str))

    # main: a slow turn stays unconfirmed for good; text left in the composer is
    # acknowledged and forgotten; a directorctl that gives up after typing leaves
    # the text there and every later notice to that pane held behind it.
    check(before["slow_after_turn"][2] == "unconfirmed" and ["send", "receipt_late"] not in before["slow_after_turn"][0],
          f"before: a turn that started after the window was never recorded delivered: {before['slow_after_turn']}")
    check(before["unsubmitted_at_send"][1] == [] and ["send_unconfirmed", "no_submission_witnessed"] in before["unsubmitted_at_send"][0],
          f"before: text left in the composer was acknowledged and dropped: {before['unsubmitted_at_send']}")
    check(before["failed_after_typing"][1] and before["failed_after_typing"][1][0][2] == "pane busy"
          and any(event[0] == "send_failed" for event in before["failed_after_typing"][0]),
          f"before: a send that failed after typing was requeued behind its own text: {before['failed_after_typing']}")

    # 1. Normal: typed, submitted, taken -- delivered, nothing left owed.
    normal = after["normal"]
    check(["send", "working_timer_idle"] in normal[0] and not any(e[0] == "send_unconfirmed" for e in normal[0])
          and normal[1] == [] and normal[2] == "" and normal[3] == "delivered" and normal[4] == 1,
          f"an ordinary notice is delivered once and leaves no proof owed: {normal}")
    # 2. Slow preflight: in, receipt watched; the late turn records the delivery.
    slow = after["slow_at_window"]
    check(["send_unconfirmed", "receipt_pending"] in slow[0] and slow[1] == [] and slow[2] == "receipt_pending/0/false"
          and slow[3].get("state") == "unconfirmed" and slow[3].get("reason") == "receipt_pending",
          f"past the window with the composer empty: acknowledged, receipt pending, shown so on the ticket: {slow}")
    check(after["slow_still_pending"] == ["receipt_pending/0/false", []],
          f"inside the horizon with no turn yet it stays pending, and nobody is told: {after['slow_still_pending']}")
    late = after["slow_after_turn"]
    check(late[0][-1] == ["send", "receipt_late"] and late[1] == "delivered/0/false" and late[2] == "delivered" and late[3] == 1,
          f"the late turn makes it delivered, through a restarted listener, sent once: {late}")
    missed = after["missed"]
    check(["send_unconfirmed", "receipt_missed"] in missed[0] and missed[1] == "receipt_missed/0/true"
          and missed[2].get("reason") == "receipt_missed" and len(missed[3]) == 1 and "may not have been taken" in missed[3][0],
          f"no turn by the horizon: reported missed, the Director told once: {missed}")
    # 3. Text left in the composer: never acknowledged; submit pressed, never retyped.
    stuck_at_send = after["unsubmitted_at_send"]
    marker = ["send_unsubmitted", "text_left_in_composer"]
    tail = stuck_at_send[0][stuck_at_send[0].index(marker):] if marker in stuck_at_send[0] else stuck_at_send[0]
    check(tail == [["send_unsubmitted", "text_left_in_composer"]]
          and stuck_at_send[1] == [["transition", "ops", "unsubmitted_in_composer", False]]
          and stuck_at_send[2] == "unsubmitted/0/false" and stuck_at_send[3].get("state") == "pending"
          and stuck_at_send[3].get("reason") == "unsubmitted_in_composer" and stuck_at_send[4] == "PGU-704 -- P",
          f"text still in the composer: kept queued, never acknowledged, shown pending on the ticket: {stuck_at_send}")
    pressed = after["unsubmitted_after_press"]
    check(pressed[0][-3:] == [["submitted", "submit_pressed"], ["ack", ""], ["send", "receipt_late"]] and pressed[1] == []
          and pressed[2] == "delivered/1/false" and pressed[3] == "delivered" and pressed[4] == 1 and pressed[5] == 1,
          f"a restarted listener presses submit once, typing nothing, and it is delivered: {pressed}")
    stuck = after["stuck"]
    check(stuck[0] == [["transition", "ops", "unsubmitted_in_composer", False]] and stuck[1] == "unsubmitted/3/true"
          and len(stuck[2]) == 1 and "pressing submit did not send it" in stuck[2][0] and stuck[3] == 1 and stuck[4] == 3,
          f"submit that never takes: three presses, the Director told once, never acknowledged or retyped: {stuck}")
    edited = after["edited"]
    check(edited[0] == "unsubmitted/0/true" and edited[1] == 0 and len(edited[2]) == 1 and "has since been edited" in edited[2][0]
          and edited[3][0] == ["transition", "main", "unsubmitted_in_composer", False] and edited[4] == 1,
          f"text edited after the send is somebody's: submit is never pressed on it: {edited}")
    # 4. directorctl gave up after typing: parked as unsubmitted, not retyped.
    failed = after["failed_after_typing"]
    check(["send_unsubmitted", "sender_failed_after_input"] in failed[0] and not any(e[0] == "send_failed" for e in failed[0])
          and failed[1] == [["transition", "app", "unsubmitted_in_composer", False]] and failed[2].startswith("unsubmitted/")
          and failed[3] == 1,
          f"a send that failed after typing is parked, never typed again: {failed}")
    superseded = after["superseded"]
    check(not [row for row in superseded[0] if row[1] == "app"] and superseded[1] == "abandoned/2/true" and superseded[2] == 0 and len(superseded[3]) == 1
          and "no longer applies" in superseded[3][0] and superseded[4] == "PGU-707 -- P",
          f"a notice superseded while unsubmitted is discarded, never pressed, the Director told once: {superseded}")
    someone = after["submitted_by_someone"]
    check(not [row for row in someone[0] if row[1] == "main"] and someone[1].startswith("delivered/") and someone[2] == ["ack", ""] and someone[3] == 1,
          f"text somebody else submitted, and a turn took, is delivered and acknowledged, never retyped: {someone}")
    cleared = after["cleared"]
    check(cleared[0].startswith("unsubmitted/") and cleared[1] == 2
          and [e for e in cleared[2] if e[1] == "text_left_in_composer"] == [["send_unsubmitted", "text_left_in_composer"]] * 2,
          f"text cleared with nothing taking it is typed again into the empty composer: {cleared}")
    reads = composer_reads()
    check(reads == {"empty box": "", "placeholder right of the cursor": "", "typed, cursor at its end": "PGU-1 -- a notice",
                    "multiline draft, empty current row": "first\nsecond", "Hermes redraw residue": "",
                    "Hermes prompted draft": "first\nsecond", "output above an empty prompt": "",
                    "cursor below a short capture": "first", "negative cursor": None},
          f"the composer is read as it holds text, never a placeholder or residue: {reads}")
    # The shipped submit, once, for real: Enter on the target, and nothing typed.
    pressed_for_real = real_directorctl_submit()
    check(pressed_for_real == ["send-keys -t pgu-ops:0.0 Enter"],
          f"directorctl submit presses Enter on the target and types nothing: {pressed_for_real}")
    # Grants and copy parity last: they must not stand in for a behaviour check under mutation.
    if os.environ.get("SYRD565_MUTATION_SKIP_COPY_PARITY") != "1":
        check(all(value == "postgres+ticket_board_listener" for value in after["grantees"].values()),
              f"only the listener (and the owner) may run the proof functions: {after['grantees']}")
        check(all(after["fresh_equals_migrated"].values()),
              f"a fresh schema.sql board installs the same functions: {after['fresh_equals_migrated']}")
    print(f"delivery_proof_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
