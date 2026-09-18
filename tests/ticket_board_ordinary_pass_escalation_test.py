#!/usr/bin/env python3
"""The escalation half must run on a pass that carries no turn end.

Live on SYRD-206: the owner was prompted correctly and alone, the grace expired,
and the Director heard nothing -- until some OTHER role happened to end a turn.
The SQL guard was correct the whole time; calling it with an empty turn map in a
rolled-back transaction produced exactly one Director escalation. It was simply
never called, because `process_idle_turn_end_nudges` returned at
`if not idle_since` before reaching `_process_unresolved_turn_end`.

SYRD-203 had made that inner method tolerate an empty map and documented it in
its docstring, one screen below the return that made it unreachable. The existing
listener suites drive the guard directly, so nothing noticed (SYRD-207).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from standalone_test_runner import run_module_tests
from scripts.ticket_board.notify_listener import TicketBoardNotifyListener


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class RecordingConnection:
    """Records which generators a pass invoked, and with what.

    Deliberately narrow: this is about which SQL the listener reaches, not what
    the SQL then does. The guard's own behaviour has its own suites, and was
    never the thing at fault here.
    """

    def __init__(self, *, unresolved_result: int = 0, idle_result: int = 0) -> None:
        self.unresolved_calls: list[tuple[Any, ...] | None] = []
        self.idle_calls: list[tuple[Any, ...] | None] = []
        self.continuation_calls: list[tuple[Any, ...] | None] = []
        self.unresolved_result = unresolved_result
        self.idle_result = idle_result

    def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> _Result:
        text = str(statement)
        if "notify_unresolved_turn_end" in text:
            self.unresolved_calls.append(params)
            return _Result([(self.unresolved_result,)])
        if "notify_idle_turn_end_nudges" in text:
            self.idle_calls.append(params)
            return _Result([(self.idle_result,)])
        if "consume_turn_continuation" in text:
            self.continuation_calls.append(params)
            return _Result([(None,)])
        return _Result([])


def _listener(**kwargs: Any) -> TicketBoardNotifyListener:
    return TicketBoardNotifyListener(
        conninfo="",
        activity_gate=lambda *_a, **_k: False,
        sender=lambda _target, _message: None,
        **kwargs,
    )


def _turn_map(call: tuple[Any, ...] | None) -> dict[str, str]:
    assert call is not None
    return json.loads(str(call[0]))


def test_an_ordinary_pass_with_no_turn_end_still_calls_the_guard() -> None:
    """The defect, stated as the behaviour that was missing.

    No pane state at all, so no role ended a turn. Before SYRD-207 the pass
    returned without asking the database anything, and an owner who had already
    been prompted could sit past the grace indefinitely.
    """
    listener = _listener()
    conn = RecordingConnection()

    assert listener.process_idle_turn_end_nudges(conn) == 0

    assert len(conn.unresolved_calls) == 1, conn.unresolved_calls
    assert _turn_map(conn.unresolved_calls[0]) == {}, conn.unresolved_calls[0]


def test_the_reminder_generator_is_not_run_without_a_turn_end() -> None:
    """Only the escalation half gains the ordinary pass.

    The reminder generator beside it has nothing to say without a turn end, and
    running it on every pass would be a different regression: it is what told
    Audit it had not advanced a ticket it had just been handed (SYRD-163).
    """
    listener = _listener()
    conn = RecordingConnection()

    listener.process_idle_turn_end_nudges(conn)

    assert conn.idle_calls == [], conn.idle_calls
    assert conn.continuation_calls == [], conn.continuation_calls


def test_the_grace_is_passed_on_an_ordinary_pass_too() -> None:
    """The escalation is a statement about elapsed time, so it needs the window."""
    listener = _listener()
    listener.unresolved_turn_grace_seconds = 42
    conn = RecordingConnection()

    listener.process_idle_turn_end_nudges(conn)

    call = conn.unresolved_calls[0]
    assert call is not None
    assert "42 seconds" in str(call[1]), call


def test_a_failing_guard_on_an_ordinary_pass_is_not_fatal() -> None:
    """A pass must survive it: this now runs on every pass, not only rare ones."""

    class Exploding(RecordingConnection):
        def execute(self, statement: Any, params: tuple[Any, ...] | None = None) -> _Result:
            if "notify_unresolved_turn_end" in str(statement):
                self.unresolved_calls.append(params)
                raise RuntimeError("guard unavailable")
            return super().execute(statement, params)

    listener = _listener()
    conn = Exploding()

    assert listener.process_idle_turn_end_nudges(conn) == 0
    assert len(conn.unresolved_calls) == 1


def test_the_owner_prompt_no_longer_claims_the_director_knows() -> None:
    """The wording half, asserted where it lives: the guard's own message.

    I reported this as coming from the directorctl prompt template. It does not
    -- it is the message the SQL guard builds, which is why correcting it needs a
    migration. Checked on the LAST definition in schema.sql, because that is the
    one a fresh install ends up executing.
    """
    import re

    schema = (ROOT / "scripts" / "ticket_board" / "schema.sql").read_text(encoding="utf-8")
    pattern = re.compile(
        r"CREATE OR REPLACE FUNCTION ticket_board\.notify_unresolved_turn_end\s*\(.*?\n\$\$;",
        re.DOTALL,
    )
    definitions = pattern.findall(schema)
    assert definitions, "the guard is missing from schema.sql"
    effective = definitions[-1]

    assert "'already been told.'," not in effective, (
        "the owner prompt still tells the owner the Director knows, before the "
        "escalation that staging exists to delay"
    )
    assert "The Director has NOT " in effective, effective[-1200:]

    migration = (
        ROOT / "scripts" / "ticket_board" / "migrations"
        / "pgu951_syrd207_owner_prompt_wording.sql"
    ).read_text(encoding="utf-8")
    assert pattern.findall(migration)[-1].strip() == effective.strip(), (
        "pgu951 and schema.sql disagree about the guard, so a fresh install and "
        "an upgraded board would send different prompts"
    )


if __name__ == "__main__":
    run_module_tests(globals())
    print("ticket_board_ordinary_pass_escalation_test: ok")
