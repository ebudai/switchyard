"""SYRD-809: a fake pane that takes what it is sent, for listener suites.

Since 87f04565 (2026-09-25) the listener counts a send as delivered only once the recipient's own hooks record a turn
that started after it (SYRD-268): its submission witness. A suite whose pane is a fake sender has no hooks, so every
send was recorded unconfirmed and `listen_once` returned 0 -- three suites stopped at their first delivery and hid
everything after it.

`taking_listener(**kwargs)` builds the real listener with a fake pane that takes what it is sent, as a real one does:
* with a fake gate, the pane's own witness: each send the sender completed is one turn the pane started, taken once;
* with a real gate that reads hook state, the pane writes what a CLI's hooks write when a prompt starts a turn -- a
  turn start (`UserPromptSubmit`, or its runtime's own event) -- and then the record it showed before, as a turn that
  ended at once. The gate's own witness reads it, as in production.
A case that gives its own witness is left as it is, and so is a pane with no hook state ("cannot tell"): a case about
unconfirmed delivery says so explicitly.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any, Callable

from scripts.ticket_board.notify_listener import DirectorctlSender, TicketBoardNotifyListener


class PaneTakesWhatItIsSent:
    """Wraps a sender: a send that returns is a turn the pane starts. A send that raises is none."""

    def __init__(self, sender: Callable[[str, str], Any]) -> None:
        self.sender = sender
        self.started: Counter[str] = Counter()
        self._lock = threading.Lock()

    def send(self, target: str, message: str) -> Any:
        result = self.sender(target, message)
        with self._lock:
            self.started[target] += 1
        return result

    def witness(self, target: str, _since: float) -> bool:
        """A turn started after the send: once per completed send, never twice for one."""
        with self._lock:
            if self.started[target] > 0:
                self.started[target] -= 1
                return True
        return False


#: The hook event each runtime writes when a prompt starts a turn (pane_state.TURN_START_EVENTS).
TURN_START = {"claude": "UserPromptSubmit", "codex": "UserPromptSubmit", "gemini": "PreInvocation",
              "agy": "PreInvocation", "hermes": "pre_llm_call"}


class HookedPaneTakesWhatItIsSent:
    """Wraps a sender for a pane whose hooks a real gate reads: a send that returns starts a turn, recorded."""

    def __init__(self, sender: Callable[[str, str], Any], store: Any, clock: Callable[[], float]) -> None:
        self.sender, self.store, self.clock = sender, store, clock

    def send(self, target: str, message: str) -> Any:
        result = self.sender(target, message)
        before = self.store.read(target)
        if before is not None:  # no hook state: nothing to write to, and the gate says "cannot tell"
            runtime = (before.source or "").split(".", 1)[0]
            self.store.write(target, "busy", source=f"{runtime or 'claude'}.{TURN_START.get(runtime, 'UserPromptSubmit')}",
                             now=self.clock())
            # The turn ends at once: the record reads as before, written after its start as a turn's end is, its turn
            # start carried (pane_state.hook_turn_started_at).
            self.store.write(target, before.state, source=before.source, now=self.clock())
        return result


def answers_for_itself(gate: Any) -> bool:
    return callable(getattr(getattr(gate, "__self__", None), "submission_witnessed", None))


def taking_listener(**kwargs: Any) -> TicketBoardNotifyListener:
    if "submission_witness" not in kwargs:
        sender = kwargs.get("sender") or DirectorctlSender()
        gate = getattr(kwargs.get("activity_gate"), "__self__", None)
        if not answers_for_itself(kwargs.get("activity_gate")):
            pane = PaneTakesWhatItIsSent(sender)
            kwargs["sender"] = pane.send
            kwargs["submission_witness"] = pane.witness
        elif getattr(gate, "state_store", None) is not None:
            kwargs["sender"] = HookedPaneTakesWhatItIsSent(sender, gate.state_store,
                                                           kwargs.get("wall_clock") or time.time).send
    return TicketBoardNotifyListener(**kwargs)
