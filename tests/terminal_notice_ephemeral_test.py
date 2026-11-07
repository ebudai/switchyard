#!/usr/bin/env python3
"""SYRD-734: an ended ticket's notice does not start a model on an idle ephemeral worker, and a notice a Hermes turn
took is not reported as sitting unsubmitted in its composer.

Live on MEFP (2026-11-05):
- **The notice:** MEFP-748 was cancelled while luna-1, an ephemeral pool worker on the local Qwen model, held no
  ticket. Its "entered Cancelled" notice was handed to luna-1 like a new ticket: a fresh Hermes conversation, then a
  three-minute local-model turn about a ticket nobody could act on.
- **The false alert:** while that turn ran, the listener read Hermes's input row, `⚕ ❯ msg=interrupt · …`, as the
  notice left in the composer. It parked the notice, pressed submit into the running turn, and then told the
  Director the notice "has sat unsubmitted in its composer… pressing submit did not send it".

The input rows here are real: Hermes 0.15.2's own frames, captured offline (tests/fixtures/syrd734_probe, recorded in
tests/fixtures/syrd734_hermes_0.15.2_turn.json). A production-built board with the example workflow runs here: app is
an ephemeral Hermes role, ops a Hermes role, with the real HTTP server, the real notify listener, and the real
PaneActivityGate and hook-state store. Only tmux and directorctl are stood in for: a pane that shows those frames,
and whose Hermes writes its turn-start hook as it starts a turn.

Main runs first, from a git archive. These cases run on both:
- **A ticket handed to app, Hermes's hook prompt:** main parks the notice, presses submit three times and tells the
  Director it sat unsubmitted. Here it is delivered, nothing is pressed, nobody is told.
- **The same, with the hook late** (Hermes initialises before its first model call): delivered once the turn starts,
  whether the row then shows the running turn or the starting one.
- **A ticket cancelled while app holds no conversation for it:** main clears app's session and types the notice. Here
  nothing is typed, and the notice is discarded with its reason.
- **A ticket cancelled while app holds its conversation:** the notice is still delivered.
- **A notice really left in ops's composer:** it is still parked and submitted; and when someone else submits it,
  it is delivered, not reported as edited.
- **A short word from the notice in ops's composer after the send:** never parked as the notice,
  never submitted (Audit's finding on 60eebbe6: any fragment of the notice was taken for it).
- **A notice left in ops's composer when ops's pane is then stopped:** nothing is pressed, nobody is told, and the
  notice ends as one whose target is missing.
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
BEFORE = "6d96bacb790f12b9a887e946ba659ee24404e736"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
FRAMES = json.loads((ROOT / "tests" / "fixtures" / "syrd734_hermes_0.15.2_turn.json").read_text(encoding="utf-8"))
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def frame(name: str) -> tuple[list[str], int, int]:
    shot = FRAMES["frames"][name]
    return list(shot["lines"]), shot["cursor"][0], shot["cursor"][1]


class HermesPane:
    """A Hermes 0.15.2 pane, drawn from its real frames: idle, something typed, or a turn running."""

    def __init__(self) -> None:
        self.state, self.text = "idle", ""

    def rows(self) -> tuple[list[str], int, int]:
        if self.state == "running":
            return frame("turn_running")
        if self.state == "starting":
            return frame("turn_starting")
        lines, x, y = frame("idle")
        if self.state == "typed":
            lines[y] = f"❯ {self.text}"
            x = 2 + len(self.text)
        return lines, x, y


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import hermes_ticket_boundary, notify_listener as nl
    from temporary_cluster import temporary_cluster

    # The pane's own Hermes config closes its memory and session search: the boundary the clear checks is closed.
    hermes_ticket_boundary.open_reason = lambda _target: ""
    seen: dict = {}
    with temporary_cluster(prefix="s734", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd734-state.") as state_dir:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        document = workflow(root)
        for role in document["roles"]:
            if role["name"] in ("app", "ops"):
                role["runtime"] = "hermes"
            if role["name"] == "app":
                role["ephemeral"] = True  # luna-1's shape: a pool worker that starts each ticket fresh
        app.apply_workflow(document, expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        panes = {f"pgu-{role}:0.0": HermesPane() for role in ("main", "app", "ops", "director")}
        gone: set[str] = set()
        store = nl.PaneHookStateStore(Path(state_dir))
        for target in panes:
            store.write(target, "idle", source="hermes.post_llm_call", now=time.time() - 120)

        def tmux(argv, **_kwargs):
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            if target in gone:
                raise subprocess.CalledProcessError(1, argv, "", f"can't find pane: {target}")
            pane = panes.get(target)
            if pane is None:
                raise AssertionError(f"no fake pane for {target!r}: {argv}")
            if argv[:2] == ["tmux", "capture-pane"]:
                lines, _x, _y = pane.rows()
                return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                if "cursor_x" in fmt:
                    _lines, x, y = pane.rows()
                    return subprocess.CompletedProcess(argv, 0, f"{x} {y} 45\n", "")
                if fmt == "#{pane_pid}":
                    return subprocess.CompletedProcess(argv, 0, "100\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")

        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, process_table_reader=lambda: [], sleeper=lambda _s: None,
                                   role_runtimes={"main": "claude", "app": "hermes", "ops": "hermes", "director": "claude"})
        sends: list[tuple[str, str]] = []
        behaviour: dict[str, str] = {}

        def turn_starts(target: str) -> None:
            store.write(target, "busy", source="hermes.pre_llm_call")

        def turn_ends(target: str) -> None:
            panes[target].state = "idle"
            store.write(target, "idle", source="hermes.post_llm_call", now=time.time() + 0.01)

        def directorctl(target: str, message: str) -> dict:
            """Types the text and presses Enter, as directorctl does; the pane then does what Hermes does."""
            if target in gone:
                raise subprocess.CalledProcessError(1, ["directorctl"], "", f"can't find pane: {target}")
            sends.append((target, message))
            pane = panes[target]
            if message.startswith("/new"):
                return {}  # a fresh conversation: the pane is idle again at once
            how = behaviour.get(target, "takes")
            if how == "stays":
                pane.state, pane.text = "typed", message
                return {}
            if how == "unrelated":
                # The notice went somewhere; the composer now shows a short word from it that is not it -- a draft
                # begun with a word the notice also uses, say.
                pane.state, pane.text = "typed", next(word for word in message.split()[1:] if word.isalpha() and len(word) > 3)
                unrelated.append(pane.text)
                return {}
            # Hermes took it: initialising, or already `⚕ ❯ msg=interrupt …` under the transcript.
            pane.state = "starting" if how == "starts_slowly" else "running"
            if how == "takes":
                turn_starts(target)
            return {}

        presses: list[str] = []
        unrelated: list[str] = []

        def submit(target: str) -> None:
            presses.append(target)
            pane = panes[target]
            if pane.state == "typed":
                pane.state = "running"
                turn_starts(target)

        def new_listener():
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=gate.is_working,
                target_exists=lambda target: target not in gone, poll_seconds=0, submission_confirm_seconds=0.4,
                submission_poll_seconds=0.05)
            listener.delivery_submitter = submit
            return listener

        def one_pass() -> None:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            listener = new_listener()
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                from scripts.ticket_board import delivery_proof
                delivery_proof.run(listener, conn)
                listener.process_due_notifications(conn)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def cancel(ticket: str, assignee: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": "cancelled", "assignee": assignee},
                              caller="director")

        def queue(ticket: str) -> list:
            """What is still queued for the ticket's worker: an acknowledged or discarded notice is gone."""
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(target_role, payload->>'kind', coalesce(terminal_reason, last_error, ''), "
                "dead_lettered_at IS NOT NULL) ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                f"WHERE ticket_id = '{ticket}' AND target_role <> 'director';"))

        def drops(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(coalesce(busy_reason, '') ORDER BY id), '[]')::text "
                                  f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' AND event = 'drop';"))

        def proof(ticket: str) -> str:
            return sql("SELECT coalesce(string_agg(state || '/' || submit_attempts || '/' || (director_told_at IS NOT NULL), ','), '') "
                       f"FROM ticket_board.notification_proofs WHERE ticket_id = '{ticket}';")

        def director_told(ticket: str) -> list:
            waiting = json.loads(sql("SELECT coalesce(json_agg(message ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                     f"WHERE ticket_id = '{ticket}' AND target_role = 'director' AND payload->>'kind' = 'delivery_proof';"))
            return [m for t, m in sends if t == "pgu-director:0.0" and m.startswith(f"{ticket}: ")] + waiting

        def to(target: str, ticket: str, since: int) -> list[str]:
            """What was typed into the target for this case: a clear names no ticket, so the case's own sends."""
            return [("clear" if m.startswith("/new") else "notice") for t, m in sends[since:] if t == target
                    and (m.startswith("/new") or m.startswith(f"{ticket} "))]

        def settle(target: str) -> None:
            """The worker's turn ends, and its ticket leaves its hands so the next one is its active work."""
            turn_ends(target)
            behaviour.pop(target, None)

        def finish(ticket: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")

        # 1. A ticket handed to app; Hermes takes it and its hook says so at once.
        mark = len(sends)
        route("PGU-801", "app")
        for _ in range(5):
            one_pass()
        seen["taken"] = {"sends": to("pgu-app:0.0", "PGU-801", mark), "proof": proof("PGU-801"),
                         "presses": presses.count("pgu-app:0.0"), "told": director_told("PGU-801"),
                         "queue": queue("PGU-801")}
        settle("pgu-app:0.0")
        finish("PGU-801")

        # 2. The same, but Hermes initialises before its first model call: the hook comes after the window.
        behaviour["pgu-app:0.0"] = "takes_late"
        presses.clear()
        mark = len(sends)
        route("PGU-802", "app")
        one_pass()
        seen["late_at_send"] = {"proof": proof("PGU-802"), "queue": queue("PGU-802")}
        turn_starts("pgu-app:0.0")
        for _ in range(4):
            one_pass()
        seen["late"] = {"sends": to("pgu-app:0.0", "PGU-802", mark), "proof": proof("PGU-802"),
                        "presses": presses.count("pgu-app:0.0"), "told": director_told("PGU-802"),
                        "queue": queue("PGU-802")}
        settle("pgu-app:0.0")
        finish("PGU-802")

        # 2b. Caught while Hermes initialises: its row shows the starting turn, not the notice, and the hook is late.
        behaviour["pgu-app:0.0"] = "starts_slowly"
        presses.clear()
        mark = len(sends)
        route("PGU-807", "app")
        one_pass()
        seen["starting_at_send"] = {"proof": proof("PGU-807")}
        panes["pgu-app:0.0"].state = "running"
        turn_starts("pgu-app:0.0")
        for _ in range(3):
            one_pass()
        seen["starting"] = {"sends": to("pgu-app:0.0", "PGU-807", mark), "proof": proof("PGU-807"),
                            "presses": presses.count("pgu-app:0.0"), "told": director_told("PGU-807")}
        settle("pgu-app:0.0")
        finish("PGU-807")

        # 3. MEFP-748: a ticket app never held is cancelled with app as its assignee, app idle and holding no ticket.
        fixture.seed_postgres_ticket(admin, "PGU-803", title="PGU-803 canary", state="in_progress", assignee="app",
                                     commit_exempt=True)
        sql("DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-803';")
        mark = len(sends)
        cancel("PGU-803", "app")
        for _ in range(3):
            one_pass()
        seen["ended_no_conversation"] = {"sends": to("pgu-app:0.0", "PGU-803", mark), "queue": queue("PGU-803"),
                                         "drops": drops("PGU-803"), "turn": store.read("pgu-app:0.0").state}

        # 4. A ticket app is working on is cancelled: its pane holds the conversation, and is told.
        route("PGU-804", "app")
        for _ in range(3):
            one_pass()
        settle("pgu-app:0.0")
        mark = len(sends)
        cancel("PGU-804", "app")
        for _ in range(3):
            one_pass()
        seen["ended_in_conversation"] = {"sends": to("pgu-app:0.0", "PGU-804", mark), "queue": queue("PGU-804"),
                                         "drops": drops("PGU-804")}
        settle("pgu-app:0.0")

        # 5. A notice really left in ops's composer is still parked, and submitted.
        behaviour["pgu-ops:0.0"] = "stays"
        presses.clear()
        route("PGU-805", "ops")
        one_pass()
        seen["really_unsubmitted_at_send"] = {"proof": proof("PGU-805"), "composer": panes["pgu-ops:0.0"].state}
        for _ in range(3):
            one_pass()
        seen["really_unsubmitted"] = {"proof": proof("PGU-805"), "presses": presses.count("pgu-ops:0.0"),
                                      "told": director_told("PGU-805")}
        settle("pgu-ops:0.0")
        finish("PGU-805")

        # 5b. Left in ops's composer, then someone submits it and the turn starts: the row shows the starting turn.
        behaviour["pgu-ops:0.0"] = "stays"
        presses.clear()
        route("PGU-808", "ops")
        one_pass()
        seen["taken_by_someone_at_send"] = {"proof": proof("PGU-808")}
        panes["pgu-ops:0.0"].state = "starting"
        turn_starts("pgu-ops:0.0")
        for _ in range(3):
            one_pass()
        seen["taken_by_someone"] = {"proof": proof("PGU-808"), "presses": presses.count("pgu-ops:0.0"),
                                    "told": director_told("PGU-808"), "typed": sum(1 for t, m in sends if m.startswith("PGU-808 "))}
        settle("pgu-ops:0.0")
        finish("PGU-808")

        # 5c. After the send, ops's composer shows a short word the notice uses, which is not the notice.
        behaviour["pgu-ops:0.0"] = "unrelated"
        presses.clear()
        route("PGU-809", "ops")
        one_pass()
        seen["unrelated_at_send"] = {"proof": proof("PGU-809")}
        for _ in range(3):
            one_pass()
        seen["unrelated"] = {"proof": proof("PGU-809"), "presses": presses.count("pgu-ops:0.0"),
                             "composer": [panes["pgu-ops:0.0"].state, panes["pgu-ops:0.0"].text], "word": unrelated[-1:],
                             "in_notice": any(unrelated[-1] in m.split() for t, m in sends if m.startswith("PGU-809 "))}
        panes["pgu-ops:0.0"].text = ""
        settle("pgu-ops:0.0")
        finish("PGU-809")

        # 6. Left in ops's composer, and ops's worker is then stopped on purpose.
        behaviour["pgu-ops:0.0"] = "stays"
        presses.clear()
        route("PGU-806", "ops")
        one_pass()
        seen["stopped_at_send"] = {"proof": proof("PGU-806")}
        gone.add("pgu-ops:0.0")
        for _ in range(5):
            one_pass()
        seen["stopped"] = {"proof": proof("PGU-806"), "presses": presses.count("pgu-ops:0.0"),
                           "told": director_told("PGU-806"), "queue": queue("PGU-806")}
        server.shutdown()
        server.server_close()
    return seen


def composer_reads(root: Path) -> dict:
    """That tree's pane_composer.composer_text on the real Hermes frames, and on its status bar."""
    sys.path.insert(0, str(root))
    from scripts.ticket_board import pane_composer

    reads = {name: pane_composer.composer_text(*frame(name)) for name in FRAMES["frames"]}
    lines, _x, y = frame("turn_running")
    status = next(index for index, line in enumerate(lines) if line.lstrip().startswith("⚕ Qwen"))
    reads["status_bar"] = pane_composer.composer_text(lines, len(lines[status]), status)
    from scripts.ticket_board import delivery_proof
    try:  # a long notice's composer scrolls: what shows is its tail, and still the notice
        reads["long_left"] = delivery_proof.left_in_composer("", reads["typed_long"], FRAMES["typed_long"])
        long_notice, short_notice = FRAMES["typed_long"], FRAMES["typed"]
        middle = long_notice[len(long_notice) // 3:len(long_notice) // 3 + 400]
        # Text that occurs in a notice and is not the notice: Audit's examples, a short notice's end, a long middle.
        reads["identity"] = {
            **{f"long:{text}": delivery_proof.left_in_composer("", text, long_notice)
               for text in ("a", "Audit", "notice", "Qwen worker", "word399", middle)},
            **{f"short:{text}": delivery_proof.left_in_composer("", text, short_notice)
               for text in ("Cancelled", "canary entered Cancelled", short_notice)},
        }
    except TypeError:  # main's takes no typed text
        reads["long_left"] = reads["identity"] = None
    return reads


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path, part: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), f"--{part}", str(root)],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict, reads: dict) -> None:
    check(reads["turn_running"] == "⚕ ❯", f"main reads Hermes's running-turn row as text in the composer: {reads}")
    check(got["taken"]["presses"] == 3 and got["taken"]["proof"].startswith("unsubmitted/3/")
          and any("sat unsubmitted in its composer" in told for told in got["taken"]["told"]),
          f"main: a notice Hermes took is pressed three times and reported unsubmitted: {got['taken']}")
    check(got["ended_no_conversation"]["sends"] == ["clear", "notice"],
          f"main: a cancelled ticket's notice clears an idle ephemeral worker and starts a turn: "
          f"{got['ended_no_conversation']}")


def candidate(got: dict, reads: dict) -> None:
    check(reads["idle"] == "" and reads["turn_running"] == "" and reads["typed"] == FRAMES["typed"],
          f"the composer reads empty idle and while a turn runs, and what was typed when typed: {reads}")
    identity = reads["identity"]
    check(identity == {key: key == f"short:{FRAMES['typed']}" for key in identity},
          f"text that merely occurs in a notice is not the notice; the whole notice is: {identity}")
    check(reads["long_left"] is True and len(reads["typed_long"]) < len(FRAMES["typed_long"]),
          f"a long notice left in the composer, which shows only its tail, is still the notice left: {reads['long_left']}")
    check("⚕ Qwen3.8-27B │" in reads["status_bar"], f"and the status bar's ⚕ is still text: {reads['status_bar']!r}")
    taken = got["taken"]
    check(taken["sends"] == ["clear", "notice"] and taken["proof"] == "" and taken["presses"] == 0 and taken["told"] == [],
          f"a notice Hermes took is delivered: nothing parked, pressed or reported: {taken}")
    check(taken["queue"] == [], f"and acknowledged: {taken['queue']}")
    check(got["late_at_send"]["proof"].startswith("receipt_pending/"),
          f"with the hook late, the notice is in and its turn awaited, not parked: {got['late_at_send']}")
    late = got["late"]
    check(late["proof"].startswith("delivered/0/") and late["presses"] == 0 and late["told"] == [],
          f"and delivered when the turn starts, nothing pressed or reported: {late}")
    check(got["starting_at_send"]["proof"].startswith("receipt_pending/0/"),
          f"caught while Hermes initialises, the notice is in and its turn awaited, not parked: {got['starting_at_send']}")
    starting = got["starting"]
    check(starting["sends"] == ["clear", "notice"] and starting["proof"].startswith("delivered/0/")
          and starting["presses"] == 0 and starting["told"] == [],
          f"and delivered when the turn starts, typed once, nothing pressed or reported: {starting}")
    ended = got["ended_no_conversation"]
    check(ended["sends"] == [] and ended["turn"] == "idle",
          f"a cancelled ticket's notice types nothing into an idle ephemeral worker that never held it: {ended}")
    check(ended["queue"] == [] and ended["drops"] == ["terminal_notice_without_conversation"],
          f"its notice is discarded, saying why: {ended}")
    kept = got["ended_in_conversation"]
    check(kept["sends"] == ["notice"] and "terminal_notice_without_conversation" not in kept["drops"],
          f"a worker that holds the ticket's conversation is still told it ended, with no clear: {kept}")
    check(got["really_unsubmitted_at_send"]["proof"].startswith("unsubmitted/0/")
          and got["really_unsubmitted"]["proof"] == "delivered/1/false",
          f"a notice really left in the composer is still parked and submitted: {got['really_unsubmitted_at_send']} "
          f"{got['really_unsubmitted']}")
    check(got["really_unsubmitted"]["presses"] == 1 and got["really_unsubmitted"]["told"] == [],
          f"once: {got['really_unsubmitted']}")
    someone = got["taken_by_someone"]
    check(got["taken_by_someone_at_send"]["proof"].startswith("unsubmitted/0/")
          and someone["proof"].startswith("delivered/0/") and someone["presses"] == 0 and someone["told"] == []
          and someone["typed"] == 1,
          f"parked text someone submits, the row showing the starting turn: delivered, not reported as edited: "
          f"{got['taken_by_someone_at_send']} {someone}")
    unrelated = got["unrelated"]
    check(not got["unrelated_at_send"]["proof"].startswith("unsubmitted")
          and not unrelated["proof"].startswith("unsubmitted") and unrelated["presses"] == 0
          and unrelated["in_notice"] and unrelated["composer"] == ["typed", *unrelated["word"]],
          f"a short word in the composer after the send is not parked, and never submitted: "
          f"{got['unrelated_at_send']} {unrelated}")
    stopped = got["stopped"]
    check(got["stopped_at_send"]["proof"].startswith("unsubmitted/0/") and stopped["proof"] == "abandoned/0/false",
          f"a parked notice whose pane is stopped is abandoned: {got['stopped_at_send']} {stopped}")
    check(stopped["presses"] == 0 and stopped["told"] == [], f"with nothing pressed and nobody told: {stopped}")
    check([row[2:] for row in stopped["queue"]] == [["tmux_target_missing", True]],
          f"and the notice ends as one whose target is missing: {stopped['queue']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--reads":
        print(json.dumps(composer_reads(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD734_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd734-before.", dir="/tmp") as tmp:
            tree = tree_at(BEFORE, Path(tmp) / "before")
            before(run(tree, "scenario"), run(tree, "reads"))
    candidate(run(ROOT, "scenario"), run(ROOT, "reads"))
    print(f"terminal_notice_ephemeral_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
