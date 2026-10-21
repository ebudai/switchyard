#!/usr/bin/env python3
"""SYRD-640: an empty Copilot input box is not a human's draft.

Live on syrd, 2026-10-21 (SYRD-634 acceptance, Copilot CLI 1.0.101): Inspector's
Copilot finished a turn and its hook said `copilot.agentStop`, idle. Copilot drew
its input as a filled box -- `╻▄▄▄…` above, the row `┃` with the cursor at x=2,
`╹▀▀▀…` below -- and the gate read the bar as draft text: `human_composing`, and
handoff SYRD-639 sat queued as "pane busy" through four retries.

`tests/fixtures/syrd640_copilot_input_box.json` is that pane exactly: the 43 rows
`tmux capture-pane -p` returned and the cursor `2 40 43`. This box style is drawn
only for the signed-in account; the same binary offline (bubblewrap, no network,
`--allow-all`, `--experimental`, true colour) draws its plain `❯` between rules.
So the drafts here are that capture with text put behind the bar, as the box
shows its rows.

Everything is real except tmux's answers and the send:
* the gate: the real PaneActivityGate over a real hook-state store;
* the delivery: a production-built board (schema.sql, the real
  ticket-board-migrate, rbac.sql) with the example workflow, the real notify
  listener and its witness, the send standing in for tmux and Copilot, whose
  turn starts as its own hook records one (`copilot.userPromptSubmitted`).

The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "41dd7c84d68152f086fc7d9bfee5416f1c0cd9a3"  # main before SYRD-640
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL", "TMUX")
CHECKS = 0
TARGET = "pgu-inspector:0.0"


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def screens() -> dict:
    """The live capture, and the same pane with what a person types behind the bar."""
    live = json.loads((ROOT / "tests/fixtures/syrd640_copilot_input_box.json").read_text(encoding="utf-8"))
    lines, (x, y, height) = live["lines"], live["cursor"]
    top, bar, bottom = lines[39], lines[40], lines[41]
    assert (top[0], bar, bottom[0], y) == ("╻", "┃", "╹", 40), "the fixture is the live box"

    def box(rows: list[str], cx: int, cy: int) -> dict:
        return {"lines": lines[:40] + rows + lines[41:], "cursor": [cx, cy, height]}

    out = {
        "live: the empty box": {"lines": lines, "cursor": [x, y, height]},
        "a one-line draft": box(["┃ hello there"], 13, 40),
        "a draft with its cursor moved back to its start": box(["┃ hello there"], 2, 40),
        "text against the bar": box(["┃hello"], 6, 40),
        "a two-line draft, cursor at its end": box(["┃ first line", "┃ second line"], 14, 41),
        "a two-line draft, cursor on its empty second row": box(["┃ first line", "┃"], 2, 41),
        # The box's own empty second row, nothing in the first either: still empty.
        "two empty rows": box(["┃", "┃"], 2, 41),
        # Between the edges but not behind the bar: not the box's row, so read as any other.
        "a row between the edges without the bar": box(["x"], 1, 40),
    }
    # The bar alone is no box: without the corners above and below it is whatever it was before.
    cornerless = list(lines)
    cornerless[39] = "▄" * 105
    cornerless[41] = "▀" * 105
    out["a bar row with no box corners"] = {"lines": cornerless, "cursor": [x, y, height]}
    top_only = list(lines)
    top_only[41] = " the next line of output"
    out["a top edge with no bottom edge"] = {"lines": top_only, "cursor": [x, y, height]}
    # Copilot's own question, as SYRD-634 recorded it (screens/permission.txt): it holds the pane.
    recorded = (ROOT / "tests/fixtures/copilot-1.0.101/screens/permission.txt").read_text(encoding="utf-8").splitlines()
    yes = next(i for i, line in enumerate(recorded) if "❯ 1. Yes" in line)
    out["Copilot's permission question"] = {"lines": recorded, "cursor": [2, yes, len(recorded)]}
    return out


def gate_cases(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    from scripts.ticket_board import notify_listener as nl, pane_composer

    out = {}
    for name, screen in screens().items():
        x, y, height = screen["cursor"]
        text = "".join(line + "\n" for line in screen["lines"])

        def tmux(argv, **_kwargs):
            if argv[:2] == ["tmux", "capture-pane"]:
                return subprocess.CompletedProcess(argv, 0, text, "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                return subprocess.CompletedProcess(argv, 0, f"{x} {y} {height}\n" if "cursor_x" in fmt else "\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")

        state = work / name.replace(" ", "_").replace(":", "")
        store = nl.PaneHookStateStore(state)
        store.write(TARGET, "idle", source="copilot.agentStop")
        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux, role_runtimes={"inspector": "copilot"})
        gate.is_busy(TARGET)
        trace = gate.last_trace(TARGET)
        out[name] = {"busy": trace.busy, "reason": trace.reason,
                     "composer_text": pane_composer.composer_text(screen["lines"], x, y)}
    return out


def delivery(root: Path, work: Path) -> dict:
    """One handoff to idle Copilot Inspector, through the real listener: held, or delivered once."""
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    live = screens()["live: the empty box"]
    x, y, height = live["cursor"]
    shown = {"rows": list(live["lines"]), "x": x}
    sends: list[tuple[str, str]] = []
    tmux_calls: list[list[str]] = []
    out: dict = {}
    with temporary_cluster(prefix="s640", shutdown="immediate") as cluster:
        admin = build_board(fixture, cluster, root, "board")
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=service)
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        store = nl.PaneHookStateStore(work / "state")

        def tmux(argv, **_kwargs):
            argv = [str(a) for a in argv]
            tmux_calls.append(argv)
            if argv[:2] == ["tmux", "capture-pane"]:
                return subprocess.CompletedProcess(argv, 0, "".join(row + "\n" for row in shown["rows"]), "")
            if argv[:2] == ["tmux", "display-message"]:
                fmt = argv[-1]
                return subprocess.CompletedProcess(argv, 0, f"{shown['x']} {y} {height}\n" if "cursor_x" in fmt else "\n", "")
            raise AssertionError(f"unexpected tmux call {argv}")

        def sender(target: str, message: str) -> dict:
            sends.append((target, message))
            # Copilot takes the line: its hook records the turn starting, and the box is empty again.
            store.write(target, "busy", source="copilot.userPromptSubmitted")
            return {}

        targets = {"inspector": TARGET, "director": "pgu-director:0.0"}
        store.write(TARGET, "idle", source="copilot.agentStop")
        store.write(targets["director"], "idle", source="claude.Stop")
        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux,
                                   role_runtimes={"inspector": "copilot", "director": "claude"})
        listener = nl.TicketBoardNotifyListener(
            conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener"), project="pgu",
            sender=sender, activity_gate=gate.is_working, target_exists=lambda target: target in targets.values(),
            poll_seconds=0)
        listener._wait_for_notification = lambda _conn: False
        fixture.seed_postgres_ticket(admin, "PGU-639", title="PGU-639", state="analysis", assignee="director",
                                     commit_exempt=True)
        fixture.post_json(base, "/api/tickets/PGU-639/actions/force_move", {"state": "inspection", "assignee": "inspector"},
                          caller="director")
        for _ in range(4):
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")
            listener.listen_once(max_notifications=5)
        out["sends"] = [m for t, m in sends if t == TARGET]
        out["queued"] = json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, coalesce(last_error, ''), attempts) "
                                       "ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                       "WHERE target_role = 'inspector';"))
        out["trace"] = json.loads(sql("SELECT coalesce(json_agg(json_build_array(event, coalesce(busy_reason, '')) ORDER BY id), "
                                      "'[]')::text FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-639' "
                                      "AND target_role = 'inspector' AND event NOT IN ('claim', 'listener_claim', 'listener_ack');"))
        out["typing_calls"] = [c for c in tmux_calls if c[1:2] in (["send-keys"], ["paste-buffer"], ["load-buffer"])]
        server.shutdown()
        server.server_close()
    return out


def scenario(root: Path, work: Path) -> dict:
    return {"gate": gate_cases(root, work / "gate"), "delivery": delivery(root, work / "delivery")}


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd640.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def before(out: dict) -> None:
    live = out["gate"]["live: the empty box"]
    check(live["busy"] and live["reason"] == "human_composing" and live["composer_text"] == "┃",
          f"before: the live empty box is a human's draft, its text the bar: {live}")
    d = out["delivery"]
    check(d["sends"] == [] and [q[:2] for q in d["queued"]] == [["transition", "pane busy"]]
          and ["gate_defer", "human_composing"] in d["trace"],
          f"before: the handoff stays queued behind the idle Copilot pane as pane busy: {d}")


def candidate(out: dict) -> None:
    g = out["gate"]
    live = g["live: the empty box"]
    check(not live["busy"] and live["composer_text"] == "",
          f"the live empty box is empty: the pane is idle and its composer holds nothing: {live}")
    drafts = {name: g[name] for name in ("a one-line draft", "a draft with its cursor moved back to its start",
                                          "text against the bar", "a two-line draft, cursor at its end",
                                          "a two-line draft, cursor on its empty second row")}
    check(all(v["busy"] and v["reason"] == "human_composing" for v in drafts.values())
          and [v["composer_text"] for v in drafts.values()]
          == ["hello there", "hello there", "hello", "first line\nsecond line", "first line"],
          f"a draft behind the bar still holds the pane, single-line, against the bar or multiline, and reads whole: {drafts}")
    check(not g["two empty rows"]["busy"] and g["two empty rows"]["composer_text"] == "",
          f"the box's empty rows hold nothing: {g['two empty rows']}")
    check(g["a bar row with no box corners"]["busy"] and g["a bar row with no box corners"]["reason"] == "human_composing",
          f"a bar with no box corners above and below is not this box, and reads as it did: {g['a bar row with no box corners']}")
    unbarred = g["a row between the edges without the bar"]
    check(unbarred["busy"] and unbarred["reason"] == "human_composing",
          f"a row between the edges that is not behind the bar is no box row, and its text holds the pane: {unbarred}")
    check(g["a top edge with no bottom edge"]["busy"] and g["a top edge with no bottom edge"]["reason"] == "human_composing",
          f"a box needs both its edges: {g['a top edge with no bottom edge']}")
    question = g["Copilot's permission question"]
    check(question["busy"] and question["reason"].startswith("provider_prompt"),
          f"Copilot's own question holds the pane, whatever the box under it: {question}")
    d = out["delivery"]
    check(len(d["sends"]) == 1 and d["sends"][0].startswith("PGU-639 -- ") and d["queued"] == []
          and [e for e, _ in d["trace"]].count("send") == 1 and "gate_defer" not in [e for e, _ in d["trace"]]
          and d["typing_calls"] == [],
          f"the real listener delivers the handoff into the idle Copilot pane once, with no pane-busy retry: {d}")


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD640_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd640-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"copilot_input_box_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
