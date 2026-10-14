#!/usr/bin/env python3
"""SYRD-591: Codex's update banner is not a composed draft.

MEFP-51's notice to luna-9 was retried 113 times as "pane busy" after SYRD-589
was deployed. The pane was a fresh Codex 0.162.0 at its empty prompt, with the
startup banner `Update available! 0.162.0 -> 0.162.1` above the header. The
trace gave `human_composing`, and its composer snapshot was active with 328
characters (sha256 e9a843c0...).

The banner is a closed, rounded box. Its bottom border is a rule 63 wide, and
the composer reader took it for the top rule of a composer. Every row from
there down to the cursor is indented or blank (the Codex header, the logo, the
prompt), so it read the header as typed text. The snapshot took the last two
rules, which were the banner's own top and bottom borders, and read the
banner's text.

The screens are real (tests/fixtures/syrd591_codex_update_banner.json): Codex
0.162.0, offline, with the banner coming from its own update cache. The banner
screen gives main's snapshot exactly MEFP's length and sha256. Each case runs
the real PaneActivityGate, with tmux standing in only for its captures. The
board case uses a production-built board, the real server, the real listener
and the real gate. The before-run is main's code (BEFORE, the release MEFP
runs), in a child process built from a git archive.
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
BEFORE = "2935434ef364ac9629f47cc57a3ce94d0dce7538"  # main before SYRD-591: MEFP's deployed release
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
WORKER = "pgu-main:0.0"  # the example workflow's Codex implementer
MEFP_SNAPSHOT = (328, "e9a843c0fcd6b85fa46f60c638bb02968d876e5c21b07c5dd205c4034d492efd")  # MEFP-51's trace event 210288
BANNER_ROWS = range(0, 7)
DRAFTS = ("banner_one_line", "banner_draft_empty_second", "banner_two_lines", "banner_two_lines_empty_third")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def screens() -> dict:
    """Every captured screen, and each banner screen again with the banner's rows blank, as Codex draws it without one."""
    captures = json.loads((ROOT / "tests/fixtures/syrd591_codex_update_banner.json").read_text())["captures"]
    out = {}
    for name, capture in captures.items():
        out[name] = {"rows": capture["rows"], "joined": capture["joined"], "cursor": capture["cursor"]}
        if name.startswith("banner_"):
            out[name.replace("banner_", "plain_", 1)] = {
                key: [("" if i in BANNER_ROWS else row) for i, row in enumerate(capture[key])] for key in ("rows", "joined")
            } | {"cursor": capture["cursor"]}
    return out


class Pane:
    def __init__(self, screen: dict, pid: int, started: float) -> None:
        self.screen, self.pid, self.started = screen, pid, started

    def table(self) -> list[tuple]:
        # The pane's Codex and the app-server daemon it starts in a session of its own (measured, SYRD-589).
        return [(self.pid, 1, 50, self.pid, self.started), (self.pid + 89, self.pid, 7, self.pid + 89, self.started + 0.4)]


def fake_tmux(panes: dict[str, Pane]):
    def tmux(argv, **_kwargs):
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        pane = panes.get(target)
        if pane is None:
            raise subprocess.CalledProcessError(1, argv, "", f"can't find pane: {target}")
        if argv[:2] == ["tmux", "capture-pane"]:
            rows = pane.screen["joined" if "-J" in argv else "rows"]
            return subprocess.CompletedProcess(argv, 0, "".join(row + "\n" for row in rows), "")
        if argv[:2] == ["tmux", "display-message"]:
            fmt = argv[-1]
            if "cursor_x" in fmt:
                x, y = pane.screen["cursor"]
                return subprocess.CompletedProcess(argv, 0, f"{x} {y} 45\n", "")
            if fmt == "#{pane_pid}":
                return subprocess.CompletedProcess(argv, 0, f"{pane.pid}\n", "")
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        raise AssertionError(f"unexpected tmux call {argv}")

    def table():
        return [row for pane in panes.values() for row in pane.table()]

    return tmux, table


def gate_cases(nl) -> dict:
    """The real gate on each screen, as luna-9 stood: a fresh provider, the launcher's seed three seconds in."""
    from scripts.ticket_board import delivery_proof

    seen = {}
    for name, screen in screens().items():
        started = time.time() - 300.0
        pane = Pane(screen, 700, started)
        tmux, table = fake_tmux({WORKER: pane})
        with tempfile.TemporaryDirectory(prefix="syrd591-gate.") as state_dir:
            store = nl.PaneHookStateStore(Path(state_dir))
            store.write(WORKER, "idle", source="team_launcher.start", now=started + 3)
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       role_runtimes={"main": "codex"})
            gate.role_targets = {"main": WORKER}
            first = gate.is_busy(WORKER)
            first_reason = gate.last_trace(WORKER).reason
            recheck = gate.pre_send_busy(WORKER)
            recheck_reason = gate.last_trace(WORKER).reason
            snapshot = gate.composer_snapshot(WORKER)
            seen[name] = {"first": [first, first_reason], "recheck": [recheck, recheck_reason],
                          "snapshot": [snapshot.marker_found, snapshot.active, snapshot.content_length, snapshot.content_sha256],
                          "composer": delivery_proof.read_composer(gate, WORKER)}
    return seen


def reader_cases(root: Path) -> dict:
    """The composer readers on every other measured screen, and on boxes a cursor is inside."""
    from scripts.ticket_board import notify_listener as nl
    from scripts.ticket_board import pane_composer

    sys.path.insert(0, str(ROOT / "tests"))
    import pane_composer_test

    shapes = {}
    for fixture in ("syrd550_claude_captures.json", "syrd566_provider_screens.json"):
        for name, capture in json.loads((ROOT / "tests/fixtures" / fixture).read_text())["captures"].items():
            rows = capture.get("lines") or capture.get("rows")
            x, y = (capture["cursor_x"], capture["cursor_y"]) if "lines" in capture else capture["cursor"][:2]
            shapes[f"{fixture}: {name}"] = (rows, x, y)
    for name, case in pane_composer_test.cases().items():
        shapes[f"syrd550 case: {name}"] = (case["lines"], case["cursor_x"], case["cursor_y"])
    box = "─" * 60
    # A composer drawn as a box, the cursor inside it: still a ruled composer.
    shapes["cursor inside a box, typed"] = (["output", f"╭{box}╮", "│ fix the build", f"╰{box}╯"], 15, 2)
    shapes["cursor inside a box, typed, second line empty"] = (["output", f"╭{box}╮", "│ fix the build", "│ ", f"╰{box}╯"], 2, 3)
    shapes["cursor inside a square box, typed"] = (["output", f"┌{box}┐", "  fix the build", f"└{box}┘"], 15, 2)
    shapes["ruled composer under a banner box"] = ([f"╭{box}╮", "│ Update", f"╰{box}╯", "", box, "❯\xa0half typed", box], 13, 5)
    shapes["empty ruled composer under a banner box"] = ([f"╭{box}╮", "│ Update", f"╰{box}╯", "", box, "❯\xa0", box], 2, 5)
    # Codex's continuation lines count only under a draft's prompted first row, and only for a cursor row
    # that has no prompt of its own: output, and an idle prompt under an earlier prompted line, are no draft.
    shapes["indented output above a blank cursor row"] = (["• Ran the suite", "  └ 18 checks ok", ""], 0, 2)
    shapes["a prompted line's continuation above an empty prompt"] = (["› earlier request", "  its second line", "› "], 2, 2)
    seen = {}
    for name, (rows, x, y) in shapes.items():
        snapshot = nl.composer_snapshot_from_pane_text("".join(row + "\n" for row in rows))
        seen[name] = [pane_composer.composing(rows, x, y), pane_composer.composer_text(rows, x, y),
                      [snapshot.marker_found, snapshot.active, snapshot.content_length, snapshot.content_sha256]]
    return seen


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from types import SimpleNamespace

    from scripts.session_paths import seed_initial_pane_idle_state
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    seen: dict = {"gate": gate_cases(nl), "readers": reader_cases(root)}
    shown = screens()
    with temporary_cluster(prefix="s591", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd591-state.") as state_dir:
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

        store = nl.PaneHookStateStore(Path(state_dir))
        # luna-9 restarted: a fresh provider, seeded by the launcher's own writer three seconds in. No turn.
        pane = Pane(shown["banner_idle"], 600, time.time() - 5)
        seed_initial_pane_idle_state(SimpleNamespace(target=WORKER), pane_state_dir=Path(state_dir),
                                     source="team_launcher.start", now=pane.started + 3)
        tmux, table = fake_tmux({WORKER: pane})
        # The listener's gate; its clock runs a minute ahead so the seed reads as settled.
        gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                   pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                   role_runtimes={"main": "codex", "app": "claude", "ops": "claude", "director": "claude"},
                                   wall_time=lambda: time.time() + 60)
        sends: list[tuple[str, str]] = []

        def directorctl(target: str, message: str) -> dict:
            """Typed and submitted; the provider takes it, its turn starts and its composer is empty again."""
            sends.append((target, message))
            store.write(target, "busy", source="codex.UserPromptSubmit")
            return {}

        def real_pass() -> None:
            """The listener's own loop, once: stops after its first delivery, or after a pass that delivers nothing."""
            listener = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=directorctl, activity_gate=gate.is_working,
                target_exists=lambda _target: True, poll_seconds=0, submission_confirm_seconds=0.4,
                submission_poll_seconds=0.05)
            listener.delivery_submitter = lambda _target: None
            listener.listen_once(max_notifications=listener.delivered_count + 1)

        def due_now(ticket: str) -> None:
            """The requeued notice's retry comes due, as it does five minutes later on its own."""
            sql(f"UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() WHERE ticket_id = '{ticket}';")

        def route(ticket: str, state: str, role: str) -> None:
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": state, "assignee": role},
                              caller="director")

        def queued(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(target_role, coalesce(last_error, ''), attempts) "
                                  f"ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';"))

        def holds(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(busy_reason ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND event = 'gate_defer';"))

        def composer_traces(ticket: str) -> list:
            """Each traced decision's composer reading: (event, decision reason, snapshot active, suspected clobber)."""
            return json.loads(sql(
                "SELECT coalesce(json_agg(json_build_array(event, detail->>'decision_reason', detail->'composer_before'->'active', "
                "detail->'suspected_clobber') ORDER BY id), '[]')::text FROM ticket_board.notification_trace "
                f"WHERE ticket_id = '{ticket}' AND detail ? 'composer_before';"))

        def worker_sends(ticket: str) -> list[str]:
            return [message for target, message in sends if target == WORKER and message.startswith(f"{ticket} -- ")]

        def delivery(ticket: str) -> dict:
            import urllib.request
            with urllib.request.urlopen(f"{base}/api/tickets/{ticket}") as response:
                return json.loads(response.read()).get("active_work_delivery") or {}

        # 1. MEFP-51: the ticket is routed to the worker whose fresh pane shows the banner over an empty prompt.
        fixture.seed_postgres_ticket(admin, "PGU-901", title="Banner over an empty prompt", state="analysis",
                                     assignee="director", commit_exempt=True)
        route("PGU-901", "in_progress", "main")
        real_pass()
        first = [worker_sends("PGU-901"), queued("PGU-901"), holds("PGU-901"), delivery("PGU-901").get("state")]
        # The natural retries: main holds it every time; this tree has nothing left to send.
        for _ in range(2):
            due_now("PGU-901")
            real_pass()
        seen["banner_idle"] = first + [worker_sends("PGU-901"), queued("PGU-901"), holds("PGU-901"),
                                       composer_traces("PGU-901")]
        store.write(WORKER, "idle", source="codex.Stop")

        # 2. A person is typing under the banner: the notice waits, and nothing is typed into the draft. Once
        #    the draft is gone (submitted, its turn over), the notice goes, once.
        fixture.post_json(base, "/api/tickets/PGU-901/actions/force_move", {"state": "done", "assignee": "unassigned"},
                          caller="director")  # one active ticket per role: PGU-902 would wait behind it
        fixture.seed_postgres_ticket(admin, "PGU-902", title="Banner over a draft", state="analysis",
                                     assignee="director", commit_exempt=True)
        pane.screen = shown["banner_two_lines_empty_third"]
        route("PGU-902", "in_progress", "main")
        real_pass()
        due_now("PGU-902")
        real_pass()
        held = [worker_sends("PGU-902"), queued("PGU-902"), holds("PGU-902")]
        pane.screen = shown["banner_idle"]
        due_now("PGU-902")
        real_pass()
        due_now("PGU-902")
        real_pass()
        seen["banner_draft"] = held + [worker_sends("PGU-902"), queued("PGU-902")]
        server.shutdown()
        server.server_close()
    return seen


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
    # Mutation runs may keep main's observations in a file: they come from BEFORE's archive and this
    # file's scenario, which no mutant of the tree touches.
    cached = Path(os.environ.get("SYRD591_BEFORE_JSON", "")) if os.environ.get("SYRD591_BEFORE_JSON") else None
    if cached is not None and cached.is_file():
        before = json.loads(cached.read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="syrd591.") as tmp:
            before_root = tree_at(BEFORE, Path(tmp) / "before")
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                                   capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                   timeout=900)
            assert child.returncode == 0, child.stderr[-3000:]
            before = json.loads(child.stdout.strip().splitlines()[-1])
        if cached is not None:
            cached.write_text(json.dumps(before))
    after = json.loads(json.dumps(scenario(ROOT), default=str))
    print("OBSERVED " + json.dumps({"before": before, "after": after}, ensure_ascii=False), file=sys.stderr)

    # main, on the captured screen: MEFP's exact snapshot, and the idle pane held as a person typing.
    idle_before = before["gate"]["banner_idle"]
    check(idle_before["snapshot"] == [True, True, *MEFP_SNAPSHOT],
          f"before: the captured banner gives MEFP-51's snapshot, 328 characters, e9a843c0...: {idle_before['snapshot']}")
    check(idle_before["first"] == [True, "human_composing"] and idle_before["recheck"] == [True, "human_composing"],
          f"before: the idle pane under the banner is held as a person composing: {idle_before}")
    check(idle_before["composer"].startswith("_ OpenAI Codex (v0.162.0)"),
          f"before: the header under the banner reads as the composer's text: {idle_before['composer'][:60]!r}")
    check(before["banner_idle"][0] == [] and before["banner_idle"][1][0][1] == "pane busy"
          and set(before["banner_idle"][2]) == {"human_composing"} and before["banner_idle"][3] != "delivered"
          and before["banner_idle"][4] == [] and before["banner_idle"][5][0][2] == 3,
          f"before: MEFP-51: requeued as pane busy on every retry, never sent: {before['banner_idle']}")

    gate = after["gate"]
    # The idle pane under the banner is deliverable at both gates, and its composer reads empty.
    check(gate["banner_idle"]["first"] == [False, "working_timer_idle"]
          and gate["banner_idle"]["recheck"] == [False, "working_timer_idle"],
          f"the idle pane under the banner is deliverable at both gates: {gate['banner_idle']}")
    check(gate["banner_idle"]["snapshot"] == [False, False, 0, ""] and gate["banner_idle"]["composer"] == "",
          f"the banner is no composer: the snapshot finds none and the composer reads empty: {gate['banner_idle']}")
    # Every draft under the banner is still held, and read whole.
    for name in DRAFTS:
        check(gate[name]["first"] == [True, "human_composing"] and gate[name]["recheck"] == [True, "human_composing"],
              f"{name}: a draft under the banner is still held: {gate[name]}")
    check([gate[name]["composer"] for name in DRAFTS]
          == ["fix the build", "fix the build", "fix the build\nthen run the suite", "fix the build\nthen run the suite"],
          f"each draft reads as what was typed: {[gate[name]['composer'] for name in DRAFTS]}")
    check(all(before["gate"][name]["first"] == [True, "human_composing"] for name in DRAFTS),
          "before: each draft was held too (as every banner screen was)")
    # The banner changes nothing: each screen reads as it does without one.
    for name in ("banner_idle", *DRAFTS):
        plain = name.replace("banner_", "plain_", 1)
        check({k: v for k, v in gate[name].items() if k != "snapshot"} == {k: v for k, v in gate[plain].items() if k != "snapshot"},
              f"{name}: read as the same screen without the banner: {gate[name]} vs {gate[plain]}")
    # Codex's continuation lines: a cursor on the empty third line of a draft. main, without the banner,
    # passed it (the banner hid that); it is held now, with or without one.
    check(before["gate"]["plain_two_lines_empty_third"]["recheck"][0] is False
          and gate["plain_two_lines_empty_third"]["recheck"] == [True, "human_composing"],
          f"a draft's empty third line is held, banner or not: {before['gate']['plain_two_lines_empty_third']} -> "
          f"{gate['plain_two_lines_empty_third']}")
    # Everything else the readers see is read as before: Claude's ruled composer, Codex's own screens,
    # every SYRD-550 shape, and a box the cursor is inside.
    readers_before, readers_after = before["readers"], after["readers"]
    check(set(readers_before) == set(readers_after) and len(readers_after) >= 30,
          f"the same {len(readers_after)} reader shapes on both trees")
    changed = {name: (readers_before[name][:2], readers_after[name][:2]) for name in readers_after
               if readers_before[name][:2] != readers_after[name][:2]}
    check(not changed, f"every other shape is composing, and holds the same text, as before: {changed}")
    # The snapshot reads only Claude Code's ruled composer, as before; a box's edges no longer bound one. A box
    # composer's snapshot always read its side bars as text, so it never told anything.
    boxed = {"cursor inside a box, typed", "cursor inside a box, typed, second line empty", "cursor inside a square box, typed"}
    snapshots = {name for name in readers_after if readers_before[name][2] != readers_after[name][2]}
    check(snapshots == boxed and all(readers_after[name][2] == [False, False, 0, ""] for name in boxed)
          and all(readers_before[name][2][:2] == [True, True] for name in boxed),
          f"only a box's snapshot changed, from its side bars to none: {[(n, readers_before[n][2], readers_after[n][2]) for n in snapshots]}")
    check(readers_after["cursor inside a box, typed"][0] is True
          and readers_after["cursor inside a box, typed, second line empty"][0] is True
          and readers_after["cursor inside a square box, typed"][0] is True,
          f"a draft in a box the cursor is inside is still a draft: {readers_after}")
    check(readers_after["indented output above a blank cursor row"][:2] == [False, ""]
          and readers_after["a prompted line's continuation above an empty prompt"][:2] == [False, ""],
          f"output and an idle prompt are no draft: {readers_after['indented output above a blank cursor row']}, "
          f"{readers_after['a prompted line' + chr(39) + 's continuation above an empty prompt']}")
    check(readers_after["ruled composer under a banner box"][:2] == [True, "half typed"]
          and readers_after["empty ruled composer under a banner box"][:2] == [False, ""],
          f"Claude's ruled composer under a box reads its own rows: {readers_after['ruled composer under a banner box']}, "
          f"{readers_after['empty ruled composer under a banner box']}")

    # The board: MEFP-51's notice reaches the idle pane under the banner, exactly once.
    idle = after["banner_idle"]
    check(len(idle[0]) == 1 and idle[1] == [] and idle[2] == [] and idle[3] == "delivered",
          f"MEFP-51: the notice is delivered at once, alone: {idle[:4]}")
    check(len(idle[4]) == 1 and idle[5] == [] and idle[6] == [],
          f"later passes send nothing more: {idle[4:7]}")
    check(idle[7] and all(active is False and clobber in (False, None) for _event, _reason, active, clobber in idle[7]),
          f"its trace records an empty composer and no suspected clobber: {idle[7]}")
    # A draft under the banner holds the notice; once it is gone the notice goes, once.
    draft = after["banner_draft"]
    check(draft[0] == [] and draft[1] and draft[1][0][1] == "pane busy" and set(draft[2]) == {"human_composing"},
          f"a draft under the banner holds the notice, nothing typed into it: {draft[:3]}")
    check(len(draft[3]) == 1 and draft[4] == [],
          f"the draft gone, the notice is delivered once: {draft[3:]}")
    print(f"codex_update_banner_composer_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
