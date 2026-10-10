#!/usr/bin/env python3
"""SYRD-570: a composer may hold a role's notices, but not for hours without the Director hearing of it.

On otto, Hermes redraw residue made idle panes' composers look typed into, and
OTTO-71's and OTTO-79's notices were held as "pane busy" for hours while the
runtime's own trusted hook said each turn was over. SYRD-550 fixed that residue
classification; any other ambiguous or genuinely stuck composer still held
silently.

Production-built board (schema.sql, the real ticket-board-migrate, rbac.sql)
with the example workflow, the real HTTP server, the real notify listener and
the real PaneActivityGate, hook-state store and pane_composer. Every pass is a
new listener, so every case also covers a restart. Stood in for: tmux (each
pane's screen and cursor, and a refusal of anything that could type into or
clear one), the process table, and /proc. The before-run is main's code and SQL,
in a child built from a git archive.
"""

from __future__ import annotations

import importlib.machinery  # noqa: F401  (kept for parity with the SYRD-557 harness)
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "81a9c37efd0d3ebb06d12c5fad45058c800660be"  # main before SYRD-570
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
ALERT_ENV = "TICKET_BOARD_COMPOSER_HOLD_ALERT_SECONDS"
ROLES = ("main", "app", "ops", "director")
RULE = "─" * 100
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board.peer_identity import SessionIdentity
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    os.environ.pop(ALERT_ENV, None)
    with temporary_cluster(prefix="s570", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd570-state.") as tmp:
        tmp_path = Path(tmp)
        state_dir, proc = tmp_path / "state", tmp_path / "proc"
        state_dir.mkdir()
        proc.mkdir()

        def proc_entry(pid: int, ppid: int, start: int, comm: str) -> None:
            (proc / str(pid)).mkdir(exist_ok=True)
            (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {' '.join(['0'] * 17)} {start} 0 0\n")

        proc_entry(900, 1, 10, "tmux: server")
        targets = {role: f"pgu-{role}:0.0" for role in ROLES}
        runtimes = {"main": "hermes", "app": "claude", "ops": "claude", "director": "claude"}
        for n, role in enumerate(ROLES, start=1):
            proc_entry(4100 + n, 900, 5000 + n, runtimes[role])

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

        store = nl.PaneHookStateStore(state_dir)
        idle_source = {"main": "hermes.post_llm_call", "app": "claude.Stop", "ops": "claude.Stop", "director": "claude.Stop"}
        for role, target in targets.items():
            store.write(target, "idle", source=idle_source[role], now=time.time() - 120)

        epoch = time.time() - 86_400.0

        def process_table() -> tuple:
            return tuple(row for n, _role in enumerate(ROLES, start=1)
                         for row in ((1000 + n, 1, 0, 1000 + n, epoch), (2000 + n, 1000 + n, 500, 1000 + n, epoch)))

        #: What each pane's composer holds, and whether its cursor can be read.
        composer = {target: "" for target in targets.values()}
        unreadable: set[str] = set()
        pane_mutations: list[list[str]] = []

        def tmux(argv, **_kwargs):
            target = argv[argv.index("-t") + 1] if "-t" in argv else ""
            if target not in targets.values():
                raise AssertionError(f"no fake pane for {target!r}: {argv}")
            if argv[:2] == ["tmux", "capture-pane"]:
                lines = ["", "  the last answer, finished", "", RULE, "❯\xa0" + composer[target], RULE, "  status"] + [""] * 13
                return subprocess.CompletedProcess(argv, 0, "".join(line + "\n" for line in lines), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                if "cursor_x" in fmt:
                    if target in unreadable:
                        return subprocess.CompletedProcess(argv, 0, "\n", "")
                    return subprocess.CompletedProcess(argv, 0, f"{2 + len(composer[target])} 4 20\n", "")
                if fmt == "#{pane_pid}":
                    n = ROLES.index(target[4:-4]) + 1
                    return subprocess.CompletedProcess(argv, 0, f"{1000 + n}\n", "")
                return subprocess.CompletedProcess(argv, 0, "0\n", "")
            pane_mutations.append(list(argv))  # send-keys, clear-history ... anything that could touch a composer
            raise AssertionError(f"unexpected tmux call {argv}")

        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=process_table,
                                   sleeper=lambda _s: None, role_runtimes=runtimes)
        gate.proc_root = proc
        store.write(targets["director"], "idle", source="claude.Stop")
        gate.role_identities = {role: SessionIdentity(4100 + n, 5000 + n) for n, role in enumerate(ROLES, start=1)}
        sends: list[tuple[str, str]] = []

        def sender(target: str, message: str) -> dict:
            sends.append((target, message))
            return {}

        def one_pass() -> None:
            """One iteration of the listen loop, by a listener started for it: every pass is a restart."""
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                target_exists=lambda _target: True, submission_witness=lambda *_a: True, poll_seconds=0)
            listener._wait_for_notification = lambda _conn: False
            listener.listen_once(max_notifications=10)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def held(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, target_role, coalesce(last_error, '')) "
                                  "ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}' AND target_role <> 'director';"))

        def gate_reason(ticket: str) -> str:
            return sql("SELECT coalesce(max(busy_reason), '') FROM ticket_board.notification_trace "
                       f"WHERE ticket_id = '{ticket}' AND event = 'gate_defer';")

        def episodes(role: str):
            try:
                return json.loads(sql("SELECT coalesce(json_agg(json_build_array(reason, alerted_at IS NOT NULL, "
                                      "coalesce(clear_reason, ''), ticket_ids) ORDER BY id), '[]')::text "
                                      f"FROM ticket_board.composer_hold_episodes WHERE target_role = '{role}';"))
            except AssertionError:
                return None  # main has no such table

        def backdate(role: str, minutes: int) -> None:
            try:
                sql(f"UPDATE ticket_board.composer_hold_episodes SET held_since = held_since - interval '{minutes} minutes' "
                    f"WHERE target_role = '{role}' AND cleared_at IS NULL;")
            except AssertionError:
                pass  # main has nothing to backdate

        def director_heard(ticket: str) -> list:
            waiting = json.loads(sql("SELECT coalesce(json_agg('waiting: ' || message ORDER BY id), '[]')::text "
                                     "FROM ticket_board.ticket_notification_queue "
                                     f"WHERE ticket_id = '{ticket}' AND target_role = 'director';"))
            return [m for t, m in sends if t == targets["director"] and ticket in m] + waiting

        def sent_to(target: str, ticket: str) -> int:
            return sum(1 for t, m in sends if t == target and m.startswith(f"{ticket} -- "))

        def withdrawn(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(busy_reason ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND target_role = 'director' AND event = 'discard';"))

        try:
            # 1. Hermes-style: main's turn is over by its own trusted hook, but its
            #    composer shows leftover text nobody is writing. The notice is
            #    deferred as pane busy, pass after pass.
            composer[targets["main"]] = "redraw residue from the last frame"
            route("PGU-901", "main")
            for _ in range(3):
                one_pass()
            seen["residue_held"] = [held("PGU-901"), gate_reason("PGU-901"), episodes("main"), director_heard("PGU-901")]
            backdate("main", 29)
            one_pass()
            seen["residue_under_threshold"] = [episodes("main"), director_heard("PGU-901")]
            backdate("main", 2)
            one_pass()
            seen["residue_alerted"] = [held("PGU-901"), episodes("main"), director_heard("PGU-901")]
            for _ in range(3):
                one_pass()
            seen["residue_once"] = [held("PGU-901"), episodes("main"), director_heard("PGU-901"), sent_to(targets["main"], "PGU-901"),
                                    pane_mutations[:], composer[targets["main"]]]
            # Somebody looks, as the alert says, and empties the composer.
            composer[targets["main"]] = ""
            one_pass()
            seen["residue_delivered"] = [held("PGU-901"), episodes("main"), director_heard("PGU-901"), sent_to(targets["main"], "PGU-901")]

            # 2. A real human draft in app's composer, configured threshold 10 min.
            #    It is held, the Director told once -- and nothing ever types over it.
            os.environ[ALERT_ENV] = "600"
            composer[targets["app"]] = "a reply I am still writing"
            route("PGU-902", "app")
            one_pass()
            backdate("app", 11)
            for _ in range(4):
                one_pass()
            seen["draft_alerted"] = [held("PGU-902"), episodes("app"), director_heard("PGU-902"), sent_to(targets["app"], "PGU-902"),
                                     pane_mutations[:], composer[targets["app"]]]
            # The person submits their line: app's turn starts. Current work is
            # authoritative now -- the hold is no longer the composer's.
            composer[targets["app"]] = ""
            store.write(targets["app"], "busy", source="claude.UserPromptSubmit")
            one_pass()
            seen["draft_work_resumed"] = [held("PGU-902"), episodes("app"), director_heard("PGU-902"), sent_to(targets["app"], "PGU-902")]
            store.write(targets["app"], "idle", source="claude.Stop")
            one_pass()
            seen["draft_delivered_after_turn"] = [held("PGU-902"), episodes("app"), sent_to(targets["app"], "PGU-902")]

            # 3. An unreadable cursor under a trusted idle hook is a composer hold too;
            #    the ticket then moves on, which supersedes the held notice.
            unreadable.add(targets["ops"])
            route("PGU-903", "ops")
            one_pass()
            backdate("ops", 11)
            one_pass()
            seen["unreadable_alerted"] = [held("PGU-903"), gate_reason("PGU-903"), episodes("ops"), director_heard("PGU-903")]
            fixture.post_json(base, "/api/tickets/PGU-903/actions/force_move", {"state": "cancelled", "assignee": "unassigned"},
                              caller="director")
            one_pass()
            seen["ticket_moved"] = [held("PGU-903"), episodes("ops"), director_heard("PGU-903"), sent_to(targets["ops"], "PGU-903")]
            unreadable.discard(targets["ops"])

            # 4. An alert still waiting -- the Director's own composer holds it --
            #    is withdrawn when work takes over before it could be sent; and the
            #    Director's own hold opens no episode.
            composer[targets["director"]] = "the Director drafting"
            composer[targets["ops"]] = "left over"
            route("PGU-904", "ops")
            one_pass()
            backdate("ops", 11)
            one_pass()
            seen["alert_waits"] = [held("PGU-904"), episodes("ops"), director_heard("PGU-904")]
            seen["director_own_hold"] = episodes("director")
            composer[targets["ops"]] = ""
            store.write(targets["ops"], "busy", source="claude.UserPromptSubmit")
            one_pass()
            seen["alert_withdrawn"] = [held("PGU-904"), episodes("ops"), director_heard("PGU-904"), withdrawn("PGU-904")]
            composer[targets["director"]] = ""
            store.write(targets["ops"], "idle", source="claude.Stop")

            # 5. Not trusted idle, not an episode: the composer holds the pane while
            #    the hook's idle word comes from a source the gate does not trust.
            fixture.post_json(base, "/api/tickets/PGU-902/actions/force_move", {"state": "done", "assignee": "unassigned"},
                              caller="director")
            store.write(targets["app"], "idle", source="claude.SubagentStop")
            composer[targets["app"]] = "someone typing"
            route("PGU-905", "app")
            one_pass()
            backdate("app", 30)
            one_pass()
            seen["untrusted_idle"] = [held("PGU-905"), gate_reason("PGU-905"), episodes("app")]

            new = ("note_composer_hold(bigint, text, boolean, interval)", "close_composer_hold(bigint, text, text)",
                   "composer_hold_notice_left()")
            try:
                seen["grantees"] = {f: sql("SELECT coalesce(string_agg(DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
                                           "ELSE a.grantee::regrole::text END, '+'), '') "
                                           f"FROM pg_proc p, aclexplode(p.proacl) a WHERE p.oid = 'ticket_board.{f}'::regprocedure "
                                           "AND a.privilege_type = 'EXECUTE' AND a.grantee <> p.proowner;") for f in new}
                if os.environ.get("SYRD570_MUTATION_SKIP_COPY_PARITY") == "1":
                    raise AssertionError("copy parity skipped for mutation")
                fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
                fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
                fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
                definition = "SELECT md5(pg_get_functiondef('ticket_board.{}'::regprocedure));"
                trigger = ("SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                           "WHERE tgname = 'composer_hold_notice_left' AND NOT tgisinternal;")
                seen["fresh_equals_migrated"] = {
                    **{f: fixture.psql(fresh, definition.format(f)).strip() == sql(definition.format(f)) for f in new},
                    "trigger": fixture.psql(fresh, trigger).strip() == sql(trigger) != "",
                }
            except AssertionError as exc:  # main has none of them
                seen.setdefault("grantees", f"unavailable: {str(exc)[-120:]}")
                seen["fresh_equals_migrated"] = f"unavailable: {str(exc)[-120:]}"
        finally:
            os.environ.pop(ALERT_ENV, None)
            server.shutdown()
            server.server_close()
    return seen


def configuration() -> dict:
    """The threshold's configuration, what reaches the board, and a failing board."""
    sys.path.insert(0, str(ROOT))
    from scripts.ticket_board import composer_hold

    class Logger:
        def __init__(self) -> None:
            self.warnings: list = []

        def warning(self, *args) -> None:
            self.warnings.append(args[0] % args[1:])

    class Ledger:
        def __init__(self) -> None:
            self.logger = Logger()
            self._composer_hold_present = True

    class Conn:
        def __init__(self, fail: bool = False) -> None:
            self.calls: list = []
            self.fail = fail

        def execute(self, statement, params=None):
            self.calls.append(params)
            if self.fail:
                raise RuntimeError("board unavailable")

    quiet = Ledger()
    conn = Conn()
    composer_hold.note(quiet, conn, 1, "director", "human_composing", True)  # the Director's own pane
    composer_hold.note(quiet, conn, 8, "app", "no_hook_state", False)  # neither a hold nor work: never asked
    composer_hold.note(quiet, conn, 9, "app", "busy", False)
    composer_hold.note(quiet, conn, 2, "main", "hook_busy", False)  # first non-composer deferral: asked once
    composer_hold.note(quiet, conn, 3, "main", "hook_busy", False)  # then settled: not asked again
    composer_hold.note(quiet, conn, 4, "main", "human_composing", False)  # composer without trusted idle: not a hold
    composer_hold.note(quiet, conn, 5, "main", "human_composing", True)  # a hold: always asked
    composer_hold.note(quiet, conn, 6, "main", "hook_busy", False)  # and after a hold, asked again
    failing = Ledger()
    try:
        composer_hold.note(failing, Conn(fail=True), 7, "ops", "human_composing", True)
        failure = failing.logger.warnings
    except Exception as exc:  # the hold must not depend on its bookkeeping
        failure = f"raised: {exc!r}"
    return {
        "calls": [(p[0], p[2]) for p in conn.calls],
        "failing_board": failure,
        "thresholds": {raw: composer_hold.alert_after_seconds({composer_hold.ALERT_AFTER_ENV: raw})
                       for raw in ("", "600", "0", "-5", "half an hour")},
        "unset": composer_hold.alert_after_seconds({}),
    }


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
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    if os.environ.get("SYRD570_SKIP_BEFORE") != "1":
        before_run()
    candidate(after)
    print(f"composer_hold_alert_test: {CHECKS} checks ok")
    return 0


def before_run() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd570.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    once = before["residue_once"]
    check(once[0] == [["transition", "main", "pane busy"]] and once[2] == [] and once[3] == 0,
          f"before: a notice held behind a composer under a trusted idle hook, and the Director never told: {once}")
    draft = before["draft_alerted"]
    check(draft[0] == [["transition", "app", "pane busy"]] and draft[2] == [],
          f"before: a human draft holds a notice, and nobody hears of it either: {draft}")


def candidate(after: dict) -> None:
    # 1. Hermes-style residue under a trusted idle hook.
    residue = after["residue_held"]
    check(residue[0] == [["transition", "main", "pane busy"]] and residue[1] == "human_composing"
          and residue[2] == [["human_composing", False, "", ["PGU-901"]]] and residue[3] == [],
          f"main's notice is deferred as pane busy behind its composer, and an episode opened -- nobody told yet: {residue}")
    check(after["residue_under_threshold"] == [[["human_composing", False, "", ["PGU-901"]]], []],
          f"29 minutes in, under the default half hour: still nobody told: {after['residue_under_threshold']}")
    alerted = after["residue_alerted"]
    check(alerted[0] == residue[0] and alerted[1] == [["human_composing", True, "", ["PGU-901"]]] and len(alerted[2]) == 1
          and alerted[2][0].startswith("main's pane has held 1 ticket notice (PGU-901) for 31 minutes, ")
          and re.search(r", [0-9]+ delivery attempts?, ", alerted[2][0]) and "(human_composing)" in alerted[2][0]
          and "main's own hook says its turn is over" in alerted[2][0] and "Nothing was typed into, cleared or sent" in alerted[2][0]
          and "If a person is writing there, leave it" in alerted[2][0] and "`switchyard attach pgu main`" in alerted[2][0],
          f"past it, the Director is told once: ticket, attempts, minutes, reason and what a person can do: {alerted}")
    once = after["residue_once"]
    check(once[0] == residue[0] and once[1] == alerted[1] and once[2] == alerted[2] and once[3] == 0,
          f"three more passes, each a restarted listener: one episode, one alert, the notice never sent: {once}")
    check(once[4] == [] and once[5] == "redraw residue from the last frame",
          f"and nothing touched the pane: no tmux call that types or clears, the composer as it was: {once[4]} {once[5]!r}")
    done = after["residue_delivered"]
    check(done[0] == [] and done[1] == [["human_composing", True, "delivered", ["PGU-901"]]] and done[3] == 1
          and len(done[2]) == 2 and "has ended: its ticket notices were delivered" in done[2][1],
          f"the composer is emptied: the notice delivered once, the episode cleared, the Director told it is over: {done}")

    # 2. A real human draft: protected, reported, never typed over.
    draft = after["draft_alerted"]
    check(draft[0] == [["transition", "app", "pane busy"]] and draft[1] == [["human_composing", True, "", ["PGU-902"]]]
          and len(draft[2]) == 1 and draft[2][0].startswith("app's pane has held 1 ticket notice (PGU-902) for 11 minutes")
          and "If a person is writing there, leave it" in draft[2][0] and draft[3] == 0,
          f"a human draft holds app's notice past the configured 10 minutes: told once, still held: {draft}")
    check(draft[4] == [] and draft[5] == "a reply I am still writing",
          f"and the draft is untouched: {draft[4]} {draft[5]!r}")
    resumed = after["draft_work_resumed"]
    check(resumed[0] == [["transition", "app", "pane busy"]] and resumed[1] == [["human_composing", True, "work_resumed", ["PGU-902"]]]
          and len(resumed[2]) == 2 and "app is working again" in resumed[2][1] and resumed[3] == 0,
          f"the person sends their line and app's turn starts: current work is authoritative, the episode ends: {resumed}")
    after_turn = after["draft_delivered_after_turn"]
    check(after_turn[0] == [] and after_turn[1] == resumed[1] and after_turn[2] == 1,
          f"the turn ends and the notice goes out as usual; no second episode: {after_turn}")

    # 3. An unreadable cursor, then the ticket moves.
    unreadable = after["unreadable_alerted"]
    check(unreadable[0] == [["transition", "ops", "pane busy"]] and unreadable[1] == "cursor_state_unavailable"
          and unreadable[2] == [["cursor_state_unavailable", True, "", ["PGU-903"]]] and len(unreadable[3]) == 1
          and "(cursor_state_unavailable)" in unreadable[3][0],
          f"a cursor that cannot be read under a trusted idle hook is a composer hold too: {unreadable}")
    moved = after["ticket_moved"]
    check(moved[0] == [] and moved[1] == [["cursor_state_unavailable", True, "superseded", ["PGU-903"]]] and moved[3] == 0
          and len(moved[2]) == 2 and "no longer needed and were dropped" in moved[2][1],
          f"the ticket is cancelled: the held notice superseded, the episode cleared, the Director told: {moved}")

    # 4. A waiting alert is withdrawn when work takes over; the Director's own hold opens nothing.
    waits = after["alert_waits"]
    check(waits[0] == [["transition", "ops", "pane busy"]] and waits[1][-1] == ["human_composing", True, "", ["PGU-904"]]
          and len(waits[2]) == 1 and waits[2][0].startswith("waiting: ops's pane has held 1 ticket notice (PGU-904)"),
          f"the alert is raised while the Director's own composer holds it, so it waits: {waits}")
    check(after["director_own_hold"] == [], f"the Director's own held pane opens no episode: {after['director_own_hold']}")
    withdrawn = after["alert_withdrawn"]
    check(withdrawn[1][-1] == ["human_composing", True, "work_resumed", ["PGU-904"]] and withdrawn[2] == []
          and withdrawn[3] == ["composer_hold_cleared"],
          f"ops starts working: the episode ends and the alert that never went is withdrawn: {withdrawn}")

    # 5. Untrusted idle opens nothing.
    untrusted = after["untrusted_idle"]
    check(untrusted[0] == [["transition", "app", "pane busy"]] and untrusted[1] == "human_composing"
          and untrusted[2] == [["human_composing", True, "work_resumed", ["PGU-902"]]],
          f"a composer hold under an idle word the gate does not trust opens no episode: {untrusted}")

    config = configuration()
    check(config["calls"] == [(2, False), (5, True), (6, False)],
          f"the Director's own pane and a reason that is neither hold nor work never ask; work asks once per role until a hold is noted again: {config['calls']}")
    check(config["thresholds"] == {"": 1800.0, "600": 600.0, "0": 0.0, "-5": 1800.0, "half an hour": 1800.0}
          and config["unset"] == 1800.0 and len(config["failing_board"]) == 1 and "notification 7" in config["failing_board"][0],
          f"half an hour unless configured, a bad value falls back, a failing board is logged: {config}")
    check(after["grantees"] == {"note_composer_hold(bigint, text, boolean, interval)": "ticket_board_listener",
                                "close_composer_hold(bigint, text, text)": "", "composer_hold_notice_left()": ""},
          f"only the listener may note a hold; nobody calls the helpers: {after['grantees']}")
    if os.environ.get("SYRD570_MUTATION_SKIP_COPY_PARITY") != "1":
        check(after["fresh_equals_migrated"] == {"note_composer_hold(bigint, text, boolean, interval)": True,
                                                 "close_composer_hold(bigint, text, text)": True,
                                                 "composer_hold_notice_left()": True, "trigger": True},
              f"a schema.sql board installs exactly what the migration does: {after['fresh_equals_migrated']}")


if __name__ == "__main__":
    raise SystemExit(main())
