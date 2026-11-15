#!/usr/bin/env python3
"""SYRD-793: an interrupted Claude turn no longer holds a notice for hours, and an idle board tells the Director.

On sapi (2026-11-15) a prompt typed into the Director's pane by mistake was stopped with Esc. Claude Code ran
UserPromptSubmit (the pane hook wrote `busy`) and then nothing: no Stop, no StopFailure, no idle_prompt. The pane
looked idle -- the last turn's "done" line, an empty composer -- but SAPI-67's Director handoff was held as "pane busy"
for 2 h 24 min, and the board stall alert left it out as "its next action is the Director's".

Measured on Claude Code 2.1.318, offline (tests/fixtures/syrd793_claude_interrupt: the probe kit, the hook log and the
two screens it captured): Esc 1.8 s after the prompt, while its request was in flight, runs no hook at all in the
110 s after it -- not Stop, StopFailure or Notification/idle_prompt -- and leaves exactly the idle screen. No
"Interrupted" line is drawn when no output came first, so it is not a signal to rely on.

Now:
* the gate's stale-busy recovery (Codex's since SYRD-584/589) takes Claude's busy too: after the grace (120 s) with no
  tool call, a pane whose screen does not move, shows no question and holds no typed text is served. Unlike Codex's,
  its hook is not rewritten: a pane that really is stuck still reaches the Director through the busy-pane watchdog
  (SYRD-695), and a live one's next turn writes its own state. A moving spinner, a question, typed text or a recent
  tool call still hold, and other runtimes are as before;
* `note_board_stall` counts work waiting on the Director when the whole board is idle -- nothing runnable, no role
  working or running background work -- so after the board stall threshold the Director is told ("Board idle").

Here: the gate on the real screens; the sapi incident through a real listener and gate; and the digest on a real
board (board_stall_test's), quiet with no open ticket, with a role working, or while other work is runnable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)
for _key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
    os.environ.pop(_key)  # the listener reads its project at import; run from a role pane, this is still the rig's

import ticket_board_notify_listener_test as lt  # noqa: E402
from scripts.ticket_board import notify_listener as nl  # noqa: E402

CHECKS = 0
FIXTURES = ROOT / "tests" / "fixtures" / "syrd793_claude_interrupt"
IDLE = (FIXTURES / "idle_after_esc.txt").read_text(encoding="utf-8")
WORKING = (FIXTURES / "working.txt").read_text(encoding="utf-8")
#: Claude Code 2.1.318's AskUserQuestion, as it drew it (probe_ask.sh): the hook stays busy, the screen still, the
#: cursor at column 0, row 12. A notice typed there ends in Enter, which answers the User's question.
QUESTION = (FIXTURES / "question.txt").read_text(encoding="utf-8")
TARGET = "pgu-director:0.0"
GRACE = 120.0
#: The interrupted prompt's moment: a real epoch, as the hook writes (an implausibly old one is never recovered).
T0 = 1_794_778_263.0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


class Tmux:
    """The pane as tmux shows it: `screen` is the text (or frames, one per capture), the cursor where Claude puts it."""

    def __init__(self, screen, cursor=(2, 42)):
        self.frames = screen if isinstance(screen, list) else [screen]
        self.captures = 0
        self.cursor = cursor

    def __call__(self, argv, **_kwargs):
        if "capture-pane" in argv:
            text = self.frames[self.captures % len(self.frames)]
            self.captures += 1
            return subprocess.CompletedProcess(argv, 0, text, "")
        if "display-message" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{self.cursor[0]} {self.cursor[1]} 45\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def gate_for(store, tmux, clock):
    return nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                               pane_pid_runner=tmux, process_table_reader=lambda: [], sleeper=lambda _s: None,
                               director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=GRACE,
                               wall_time=lambda: clock[0], director_target=TARGET, role_runtimes={"director": "claude"})


def interrupted(store, *, source="claude.UserPromptSubmit", at=T0):
    """What the hook wrote: the turn's start, and nothing after it."""
    store.write(TARGET, "busy", source=source, now=at)


def verdict(screen, clock_at, *, source="claude.UserPromptSubmit", cursor=(2, 42), tool_at=None):
    with tempfile.TemporaryDirectory(prefix="syrd793.") as raw:
        store = nl.PaneHookStateStore(raw)
        interrupted(store, source=source)
        if tool_at is not None:
            path = Path(raw) / "pgu-director_0.0.json"
            state = json.loads(path.read_text())
            state["turn_process"] = {"pane_pid": 41, "pane_start_time": 7}
            path.write_text(json.dumps(state))
            # The tool-activity hook's own record beside the state (SYRD-695).
            path.with_name("pgu-director_0.0.activity.json").write_text(
                json.dumps({"at": tool_at, "pane_pid": 41, "pane_start_time": 7}))
        clock = [clock_at]
        tmux = Tmux(screen, cursor)
        gate = gate_for(store, tmux, clock)
        busy = gate.is_busy(TARGET)
        after = store.read(TARGET)
        verdict.captures = tmux.captures
        return busy, gate.last_trace(TARGET).reason, (after.state, after.source)


# -- the gate, on the screens Claude Code drew ------------------------------------------------------------------------
def test_an_interrupted_claude_turn_is_recovered_after_the_grace() -> None:
    busy, reason, after = verdict(IDLE, T0 + GRACE + 10)
    check(not busy and reason == "stale_claude_busy" and after == ("busy", "claude.UserPromptSubmit"),
          f"busy since the interrupted prompt, past the grace, on the idle screen: served, and the hook's word left for "
          f"the busy-pane watchdog (SYRD-695) and the next turn's own hooks: {busy} {reason} {after}")
    busy, reason, after = verdict(IDLE, T0 + GRACE - 30)
    check(busy and reason == "hook_busy" and after[0] == "busy",
          f"within the grace (a turn's first output can take that long) it still holds: {busy} {reason} {after}")
    check(verdict.captures == 0, f"and its screen is not even read then -- Codex's capacity look is Codex's: {verdict.captures}")


def test_what_still_holds_a_claude_pane() -> None:
    spinner = [WORKING, WORKING.replace("✻ Ruminating…", "✶ Ruminating…")]
    busy, reason, after = verdict(spinner, T0 + GRACE + 10)
    check(busy and reason == "pane_content_changed" and after[0] == "busy",
          f"a turn really running -- its spinner moving -- is never recovered: {busy} {reason}")
    typed = IDLE.replace("\n❯\xa0\n", "\n❯\xa0half a message\n", 1)  # Claude's composer: the prompt and a no-break space
    check(typed != IDLE, "the composer line was found")
    busy, reason, after = verdict(typed, T0 + GRACE + 10, cursor=(17, 42))
    check(busy and reason == "human_composing" and after[0] == "busy", f"nor one whose composer holds text: {busy} {reason}")
    busy, reason, after = verdict(IDLE, T0 + GRACE + 10, tool_at=T0 + GRACE - 5)
    check(busy and after[0] == "busy", f"nor one whose turn called a tool within the grace (SYRD-695): {busy} {reason}")
    busy, reason, after = verdict(QUESTION, T0 + GRACE + 10, cursor=(0, 12), tool_at=T0 + 5)
    check(busy and reason.startswith("provider_prompt:") and after[0] == "busy",
          f"nor one showing Claude's own question, past the grace with its tool call long done: {busy} {reason}")
    from unittest.mock import patch
    from scripts.ticket_board import pane_composer

    with patch.object(pane_composer, "composing", lambda *_a, **_k: False):
        busy, reason, after = verdict(QUESTION, T0 + GRACE + 10, cursor=(0, 12), tool_at=T0 + 5)
    check(busy and reason.startswith("provider_prompt:"),
          f"and the question is what holds it, not the composer heuristic: blinded, it still holds: {busy} {reason}")
    busy, reason, after = verdict(IDLE, T0 + GRACE + 10, source="hermes.pre_llm_call")
    check(busy and reason == "hook_busy", f"another runtime's busy is as it was: {busy} {reason}")
    busy, reason, after = verdict(IDLE, T0 + GRACE + 10, source="codex.UserPromptSubmit")
    check(not busy and reason == "stale_codex_busy_recovered", f"and Codex's is recovered as before: {busy} {reason}")


def test_the_measurement_behind_it() -> None:
    record = json.loads((FIXTURES / "hooks.json").read_text())
    esc = next(m["at"] for m in record["marks"] if m["what"] == "esc")
    end = next(m["at"] for m in record["marks"] if m["what"] == "end")
    after = [h["event"] for h in record["hooks"] if esc < h["at"] < end]  # the probe's kill-server ends it: SessionEnd
    prompts = [h for h in record["hooks"] if h["event"] == "UserPromptSubmit"]
    check(record["claude_code"] == "2.1.318" and end - esc > 100 and after == [] and len(prompts) == 2
          and [h["event"] for h in record["hooks"]].count("Stop") == 1,
          f"after Esc, over {end - esc:.0f} s, Claude Code ran no hook: no Stop, StopFailure or idle_prompt: {after}")
    check("esc to interrupt" in WORKING and "esc to interrupt" not in IDLE and "Interrupted" not in IDLE
          and "accidental paste" not in IDLE,
          "the working screen says so; after Esc the screen is an idle one, with no Interrupted line and no prompt shown")


# -- the sapi incident, through a real listener and gate -------------------------------------------------------------
def test_the_directors_handoff_is_delivered_past_an_interrupted_turn() -> None:
    def run(source: str) -> list:
        sent: list = []
        with tempfile.TemporaryDirectory(prefix="syrd793.") as raw:
            store = nl.PaneHookStateStore(raw)
            store.write(TARGET, "busy", source=source, now=time.time() - 900)  # 15 minutes ago, and nothing since
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=Tmux(IDLE), capture_pane_runner=Tmux(IDLE),
                                       pane_pid_runner=Tmux(IDLE), process_table_reader=lambda: [], sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=GRACE,
                                       director_target=TARGET, role_runtimes={"director": "claude"})

            def pane(target: str, message: str) -> None:
                sent.append((target, message))
                store.write(target, "busy", source="claude.UserPromptSubmit")
                store.write(target, "idle", source="claude.Stop")

            conn = lt.FakeConnection([lt.queue_row(1, "PGU-67", target_role="director",
                                                   message="PGU-67 -- Audit signed off; it is in Director review")])
            listener = nl.TicketBoardNotifyListener(conninfo="dbname=test", sender=pane, activity_gate=gate.is_working,
                                                    connector=lambda *a, **k: conn, poll_seconds=0,
                                                    target_exists=lambda _t: True)
            listener.listen_once(max_notifications=1)
        return sent

    check(run("claude.UserPromptSubmit") == [(TARGET, "PGU-67 -- Audit signed off; it is in Director review")],
          "the Director's pane, busy only by an interrupted turn's hook, gets SAPI-67's handoff")


# -- the idle board, on a real board ---------------------------------------------------------------------------------
def test_an_idle_board_tells_the_director() -> None:
    from board_stall_test import Stall, full, short
    from router_role_board_test import Board, temporary_cluster

    idle = {"main": {"state": "idle", "detail": ""}, "app": {"state": "idle", "detail": ""},
            "audit": {"state": "idle", "detail": ""}, "librarian": {"state": "no registered provider process", "detail": ""}}
    working = {**idle, "ops": {"state": "working", "detail": ""}}
    with temporary_cluster(prefix="syrd793-pg-") as cluster:
        board = Board(cluster, "s793", full())
        try:
            stall = Stall(board)
            stall.evaluate(0, idle, threshold=1800)
            check(stall.runnable() == [] and stall.digests() == 0, "no open ticket: nothing to say, however idle")
            waiting = stall.ticket("director_review", "director")
            stall.evaluate(1, idle, threshold=1800)
            check(stall.runnable() == [waiting] and waiting not in stall.excluded(),
                  f"every role idle: the ticket waiting on the Director counts: {stall.state()}")
            stall.evaluate(20, idle, threshold=1800)
            check(stall.digests() == 0, "silent before the threshold")
            stall.evaluate(32, idle, threshold=1800)
            sent = stall.drain()
            to_director = [m for t, m in sent if t.endswith("director:0.0") and m.startswith("Board ")]
            check(stall.digests() == 1 and len(to_director) == 1 and to_director[0].startswith("Board idle: no role has worked for")
                  and waiting in to_director[0] and "waits on you" in to_director[0]
                  and all(t.endswith("director:0.0") for t, m in sent if m.startswith("Board ")),
                  f"after the threshold, one notice to the Director, naming the ticket: {sent}")
            stall.evaluate(33, working, threshold=1800)
            check(stall.runnable() == [] and stall.excluded().get(waiting) == "its next action is the Director's"
                  and stall.episodes()[-1]["closed"] and stall.episodes()[-1]["reason"] == "no_runnable_work",
                  f"a role working: not counted, and the episode closes: {stall.episodes()}")
            stall.evaluate(80, working, threshold=1800)
            check(stall.digests() == 1, "nothing more while a role works")
            stall.evaluate(81, {**idle, "ops": {"state": "background work running", "detail": ""}}, threshold=1800)
            check(stall.runnable() == [], f"nor while a role runs background work: {stall.runnable()}")
            busy = stall.ticket("in_progress", "main")
            stall.evaluate(82, idle, threshold=1800)
            check(stall.runnable() == [busy] and stall.excluded().get(waiting) == "its next action is the Director's",
                  f"while other work is runnable, that work is what counts, as before: {stall.state()}")
            stall.settle([waiting, busy])
            # Only the Director's own work counts when the board is idle: the User's, a held, a snoozed or a parked
            # ticket stays out, however long every role idles.
            users = stall.ticket("user_review", "user")
            held = stall.ticket("in_progress", "main")
            check(short(board.act(held, "set_manually_controlled", "director", manually_controlled=True)) == "ok", "held")
            snoozed = stall.ticket("documentation", "librarian")
            check(board.as_role_sql("director", f"SELECT ticket_board.snooze_reminders(ARRAY['{snoozed}'], clock_timestamp() "
                                                "+ interval '1 day', 'quiet please', true);") == {"ok": True}, "snoozed")
            parked = stall.ticket("in_progress", "app")
            board.sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET parked = true WHERE id = '{parked}';")
            episodes = len(stall.episodes())
            for minute in (90, 100, 140):
                stall.evaluate(minute, idle, threshold=1800)
            check(stall.runnable() == [] and stall.digests() == 1 and len(stall.episodes()) == episodes
                  and stall.excluded().get(users) == "its next action is the User's"
                  and stall.excluded().get(held) == "held by the Director"
                  and stall.excluded().get(snoozed) == "its reminders are snoozed"
                  and stall.excluded().get(parked) == "parked",
                  f"an idle board whose open work is the User's, held, snoozed or parked: quiet, no episode: {stall.state()}")
            mine = stall.ticket("director_review", "director")
            stall.evaluate(141, idle, threshold=1800)
            check(stall.runnable() == [mine] and stall.excluded().get(users) == "its next action is the User's"
                  and parked in stall.excluded() and held in stall.excluded() and snoozed in stall.excluded(),
                  f"with the Director's own ticket added, that ticket alone counts: {stall.state()}")
            stall.settle([users, held, snoozed, parked, mine])
        finally:
            board.close()


def main() -> int:
    for test in (test_the_measurement_behind_it, test_an_interrupted_claude_turn_is_recovered_after_the_grace,
                 test_what_still_holds_a_claude_pane, test_the_directors_handoff_is_delivered_past_an_interrupted_turn,
                 test_an_idle_board_tells_the_director):
        test()
    print(f"claude_interrupted_turn_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
