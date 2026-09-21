#!/usr/bin/env python3
"""A pane that shows what was typed into it, which is what a real one does.

The listener acks a notice only once it can show the notice arrived: the text
has to appear on the pane more times after the send than before (SYRD-225).
Suites that fake the sender and the pane separately -- a sender that appends to
a list, a capture that returns a canned screen -- model a pane that swallows
every notice, and against the real listener that now reads as a delivery that
never landed.

This couples them the way the real system is coupled: what is sent is what the
pane then shows. It answers the listener's proof read directly rather than
through the canned capture runners, because those pop one screen per call and
an extra read would consume a screen some other probe in the test was
expecting.

A suite that wants the OTHER case -- a pane that does not show the notice --
constructs the real listener class itself.
"""

from __future__ import annotations

from typing import Any, Callable


class TypedPane:
    def __init__(self) -> None:
        self.typed: dict[str, list[str]] = {}

    def sending_through(self, sender: Callable[[str, str], Any]) -> Callable[[str, str], Any]:
        def send(target: str, message: str) -> Any:
            self.typed.setdefault(target, []).append(message)
            return sender(target, message)

        return send

    def text(self, target: str) -> str:
        return "\n".join(self.typed.get(target, []))


def listener_whose_panes_show_what_they_are_sent(listener_class: type) -> type:
    """The real listener, wired to panes that behave like real ones."""

    class Listener(listener_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, sender: Any = None, **kwargs: Any) -> None:
            pane = TypedPane()
            if sender is not None:
                sender = pane.sending_through(sender)
            super().__init__(*args, sender=sender, **kwargs)
            gate_owner = getattr(self.activity_gate, "__self__", None)
            if gate_owner is not None and callable(getattr(gate_owner, "pane_text", None)):
                gate_owner.pane_text = pane.text
            self.typed_pane = pane

    Listener.__name__ = listener_class.__name__
    Listener.__qualname__ = listener_class.__qualname__
    return Listener
