#!/usr/bin/env python3
"""SYRD-556: Hermes's background self-review never ends -- or starts -- the pane's foreground turn.

Hermes 0.15.2 forks a review agent after a turn (thread `bg-review`) with the
parent's session id; it fires the same pre_llm_call, post_llm_call and
on_session_end hooks as the foreground. Otto measured the result: a review
ending marked a working pane idle, and the board escalated a stall.

Every case drives the real hook PROGRAM with the payload shape Hermes's own
shell-hook serializer produces, in the interleavings a review can take, and
reads the result two ways: the pane state file, and the real gate's reminder
generation (`turn_end_idle_since_by_role` / `idle_since_by_role`, which mint the
stall escalation). The one case that needs Hermes itself reads its installed
prompts and serializer offline -- no session, no model call.
"""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Hermetic in any pane: project modules read the board and pane settings at import.
for _key in [key for key in os.environ
             if key.startswith(("TICKET_BOARD_", "PGU_", "SWITCHYARD_")) or key in ("TMUX", "TMUX_PANE")]:
    del os.environ[_key]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ticket_board.pane_activity_gate import PaneActivityGate  # noqa: E402
from scripts.ticket_board.pane_state import PaneHookStateStore  # noqa: E402

HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"
HERMES_PYTHON = Path("/opt/hermes/venv/bin/python3")
TARGET = "pgu-main:0.0"
STATES = {"pre_llm_call": "busy", "post_llm_call": "idle", "on_session_end": "idle",
          "on_session_finalize": "idle", "on_session_start": "idle"}
FOREGROUND_A = "Pick up PGU-7 and fix the parser."
FOREGROUND_B = "PGU-7 -- PGU-7 (in in_progress, that you own) was changed by director: new comment -- re-read it."
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _load_hook():
    loader = importlib.machinery.SourceFileLoader("pane_idle_hook_556", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HOOK_MODULE = _load_hook()
REVIEW = HOOK_MODULE.HERMES_BACKGROUND_REVIEW_PREFIX + "update two things:\n\n1. Memory ..."


class Gate(PaneActivityGate):
    """The real gate; only its tmux probe is stood in for, so the hook state alone decides."""

    def pre_send_busy(self, target: str) -> bool:
        return False


class Pane:
    """One Hermes pane: its hook state, a recording board for context output, and the real gate reading it."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd556."))
        board = {"workflow": {"stages": [{"name": "in_progress", "terminal": False, "kind": "implementation", "owners": ["main"]}]},
                 "tickets": [{"id": "PGU-7", "title": "Seven", "state": "in_progress", "assignee": "main"}]}

        class Board(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(board).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "TICKET_BOARD_PROJECT": "pgu",
                    "TICKET_BOARD_URL": f"http://127.0.0.1:{self.server.server_port}"}
        self.state_path = HOOK_MODULE._target_path(self.tmp / "state", TARGET)
        self.gate = Gate(state_store=PaneHookStateStore(self.tmp / "state"))
        self.gate.role_targets = {"main": TARGET}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fire(self, event: str, message: str | None = None, *, delay: float = 0.0) -> str:
        """One hook as Hermes installs it, with the stdin its shell-hook serializer writes."""
        extra = {"model": "m", "platform": "cli"}
        if event in ("pre_llm_call", "post_llm_call"):
            extra.update(user_message=message, conversation_history=[{"role": "user", "content": "x" * 50}])
        if event == "on_session_end":
            extra.update(completed=True, interrupted=False)
        payload = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": "hermes-s1",
                   "cwd": str(self.tmp), "extra": extra}
        process = subprocess.Popen([sys.executable, "-B", str(HOOK), STATES[event], "--source", f"hermes.{event}",
                                    "--target", TARGET, "--state-dir", str(self.tmp / "state"),
                                    "--session-dir", str(self.tmp / "sessions")],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        time.sleep(delay)  # a payload that reaches the hook late, as under load
        stdout, stderr = process.communicate(json.dumps(payload), timeout=60)
        assert process.returncode == 0, stderr
        return stdout

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def reminders(self) -> dict:
        """What the gate would mint a stall escalation for: turn-end idle, and plain idle."""
        return {"turn_end": self.gate.turn_end_idle_since_by_role(["main"]), "idle": self.gate.idle_since_by_role(["main"])}


def scenario(steps):
    pane = Pane()
    try:
        return steps(pane)
    finally:
        pane.close()


def test_a_foreground_turn_still_ends_idle() -> None:
    def steps(pane: Pane) -> None:
        pane.fire("on_session_start")
        pane.fire("pre_llm_call", FOREGROUND_A)
        busy = pane.state()
        check(busy["state"] == "busy" and busy["source"] == "hermes.pre_llm_call" and busy.get("turn_started_at"),
              f"a foreground turn starts busy, with its turn start: {busy}")
        check(pane.reminders() == {"turn_end": {}, "idle": {}}, "and nothing is minted while it works")
        pane.fire("post_llm_call", FOREGROUND_A)
        ended = pane.state()
        check(ended["state"] == "idle" and ended["source"] == "hermes.post_llm_call",
              f"its post_llm_call ends it idle, as installed: {ended}")
        check("main" in pane.reminders()["turn_end"], "and the gate sees a real turn end")
        pane.fire("on_session_end")
        check(pane.state()["state"] == "idle" and pane.state()["source"] == "hermes.on_session_end",
              f"its on_session_end too: {pane.state()}")
        # A foreground turn interrupted before its post_llm_call ends on on_session_end alone.
        pane.fire("pre_llm_call", FOREGROUND_B)
        pane.fire("on_session_end")
        check(pane.state()["state"] == "idle" and "main" in pane.reminders()["turn_end"],
              f"an interrupted foreground turn still ends idle on its on_session_end: {pane.state()}")
    scenario(steps)


def _review_while_turn_b_works(order: str):
    def steps(pane: Pane) -> dict:
        pane.fire("pre_llm_call", FOREGROUND_A)
        pane.fire("post_llm_call", FOREGROUND_A)
        if order == "review first":
            pane.fire("pre_llm_call", REVIEW)  # spawned before turn A's own end
        pane.fire("on_session_end")
        pane.fire("pre_llm_call", FOREGROUND_B)
        turn_b = pane.state()
        if order == "turn first":
            pane.fire("pre_llm_call", REVIEW)
        pane.fire("post_llm_call", REVIEW)
        pane.fire("on_session_end")
        during = pane.state()
        held = pane.reminders()
        pane.fire("post_llm_call", FOREGROUND_B)
        after = pane.state()
        return {"turn_b": turn_b, "during": during, "held": held, "after": after, "minted": pane.reminders()}
    return scenario(steps)


def test_the_review_ending_never_ends_the_foreground_turn() -> None:
    for order in ("review first", "turn first"):
        seen = _review_while_turn_b_works(order)
        during, turn_b = seen["during"], seen["turn_b"]
        check(during["state"] == "busy" and during["source"] == "hermes.pre_llm_call"
              and during["updated_at"] == turn_b["updated_at"] and during["turn_started_at"] == turn_b["turn_started_at"],
              f"{order}: the review's post_llm_call and on_session_end leave turn B's busy evidence exactly as it was: {during}")
        check(seen["held"] == {"turn_end": {}, "idle": {}},
              f"{order}: and the gate mints no stall escalation while turn B works: {seen['held']}")
        check(seen["after"]["state"] == "idle" and seen["after"]["source"] == "hermes.post_llm_call"
              and "main" in seen["minted"]["turn_end"],
              f"{order}: turn B's own end still ends it idle: {seen['after']}")
        check(seen["after"]["hermes_runs"] == {"foreground_open": False, "background_open": [], "background_ending": []},
              f"{order}: and nothing is left counted: {seen['after'].get('hermes_runs')}")


def test_the_review_starting_is_not_the_panes_work() -> None:
    def steps(pane: Pane) -> None:
        pane.fire("pre_llm_call", FOREGROUND_A)
        foreground_said = pane.fire("post_llm_call", FOREGROUND_A)
        pane.fire("on_session_end")
        idle = pane.state()
        said = pane.fire("pre_llm_call", REVIEW)
        check(pane.state()["state"] == "idle" and pane.state()["updated_at"] == idle["updated_at"]
              and pane.state().get("turn_started_at") == idle.get("turn_started_at"),
              f"the review starting writes no busy and no turn start (the listener reads turn starts as delivery proof): {pane.state()}")
        check(said == "" and foreground_said == "", f"and is handed no ticket context: {said!r}")
        check("ACTIVE work: PGU-7" in pane.fire("pre_llm_call", FOREGROUND_B),
              "while a foreground turn is still given it")
    scenario(steps)


def test_an_interrupted_turn_during_a_review_ends_when_both_have() -> None:
    def steps(pane: Pane) -> None:
        pane.fire("pre_llm_call", FOREGROUND_A)
        pane.fire("post_llm_call", FOREGROUND_A)
        pane.fire("pre_llm_call", REVIEW)
        pane.fire("on_session_end")
        pane.fire("pre_llm_call", FOREGROUND_B)
        pane.fire("on_session_end")  # whose? either the review's, or B's after an interruption
        check(pane.state()["state"] == "busy" and pane.reminders()["turn_end"] == {},
              f"an end that could be the review's never ends the foreground turn: {pane.state()}")
        pane.fire("on_session_end")
        check(pane.state()["state"] == "idle" and "main" in pane.reminders()["turn_end"],
              f"each run ends once, so the next end is the foreground's: {pane.state()}")
    scenario(steps)


def test_a_review_that_never_ends_stops_counting() -> None:
    def steps(pane: Pane) -> None:
        pane.fire("pre_llm_call", FOREGROUND_A)
        pane.fire("post_llm_call", FOREGROUND_A)
        pane.fire("pre_llm_call", REVIEW)
        pane.fire("on_session_end")
        stored = pane.state()
        stale = time.time() - HOOK_MODULE.HERMES_BACKGROUND_MAX_SECONDS - 5
        stored["hermes_runs"]["background_open"] = [stale]
        pane.state_path.write_text(json.dumps(stored), encoding="utf-8")
        pane.fire("pre_llm_call", FOREGROUND_B)
        pane.fire("on_session_end")
        check(pane.state()["state"] == "idle",
              f"past {HOOK_MODULE.HERMES_BACKGROUND_MAX_SECONDS:.0f} s a silent review is no longer counted: {pane.state()}")
    scenario(steps)


def test_a_late_payload_is_still_read() -> None:
    def steps(pane: Pane) -> None:
        pane.fire("pre_llm_call", FOREGROUND_A)
        pane.fire("post_llm_call", FOREGROUND_A)
        pane.fire("pre_llm_call", REVIEW, delay=0.5)
        pane.fire("pre_llm_call", FOREGROUND_B)
        pane.fire("post_llm_call", REVIEW, delay=0.5)
        check(pane.state()["state"] == "busy" and pane.state()["source"] == "hermes.pre_llm_call",
              f"a review prompt that arrives half a second late is still read as the review's: {pane.state()}")
    scenario(steps)


def test_a_session_boundary_has_no_foreground_turn_open() -> None:
    def steps(pane: Pane) -> None:
        for boundary in ("on_session_finalize", "on_session_start"):
            pane.fire("pre_llm_call", FOREGROUND_A)
            check(pane.state()["hermes_runs"]["foreground_open"] is True, "a foreground turn is open")
            pane.fire(boundary)
            check(pane.state()["hermes_runs"]["foreground_open"] is False and pane.state()["state"] == "idle",
                  f"{boundary}: the session ended or began, so no foreground turn is open: {pane.state()}")
    scenario(steps)


def test_concurrent_hooks_lose_no_run() -> None:
    def steps(pane: Pane) -> None:
        pane.fire("on_session_start")
        workers = [threading.Thread(target=pane.fire, args=("pre_llm_call", REVIEW)) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        check(len(pane.state()["hermes_runs"]["background_open"]) == 8,
              f"eight reviews starting at once are all counted: {pane.state().get('hermes_runs')}")
    scenario(steps)


def test_every_installed_review_prompt_is_background() -> None:
    """Read from the installed Hermes, offline: its review prompts, through its own shell-hook serializer."""
    probe = (
        "import json\n"
        "from agent.background_review import _MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT\n"
        "from agent.shell_hooks import _serialize_payload\n"
        "print(json.dumps([json.loads(_serialize_payload('post_llm_call', {'session_id': 's', 'user_message': p,"
        " 'assistant_response': 'ok', 'model': 'm', 'platform': 'cli'}))"
        " for p in (_MEMORY_REVIEW_PROMPT, _SKILL_REVIEW_PROMPT, _COMBINED_REVIEW_PROMPT)]))\n"
    )
    check(HERMES_PYTHON.exists(), f"Hermes is installed at {HERMES_PYTHON}")
    done = subprocess.run([str(HERMES_PYTHON), "-I", "-c", probe], capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "HOME": tempfile.mkdtemp(prefix="syrd556-hermes.")})
    check(done.returncode == 0, f"the installed Hermes answered: {done.stderr[-800:]}")
    payloads = json.loads(done.stdout.strip().splitlines()[-1])
    check(len(payloads) == 3 and all(HOOK_MODULE._hermes_run_is_background(p) is True for p in payloads),
          f"every review prompt Hermes can send is recognized as the review: {[p['extra']['user_message'][:50] for p in payloads]}")
    check(HOOK_MODULE._hermes_run_is_background({"extra": {"user_message": FOREGROUND_A}}) is False
          and HOOK_MODULE._hermes_run_is_background({"extra": {}}) is None,
          "a board prompt is foreground, and a payload without a prompt says nothing")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"hermes_background_review_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
