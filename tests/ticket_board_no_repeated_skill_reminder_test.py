#!/usr/bin/env python3
"""SYRD-106: a hand-off carries the ticket, not a reminder to load a skill.

Every transition notification used to end with "Load the switchyard-board
skill, then read the whole ticket before acting", and every session start --
fresh, resumed, compacted or cleared -- used to begin with the same sentence.
The skills are installed for each runtime and advertised through its own
catalog, whose trigger already names a ticket, so those lines taught nothing
and were paid for on every hand-off and every recovery.

This pins the payloads themselves, on both sides and for every adapter, so the
sentence cannot return by way of a helper someone reintroduces upstream of the
send. It deliberately checks exact payloads rather than the absence of one
particular wording: a reworded reminder is the same defect.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ticket_board_pane_env import strip_ticket_board_pane_env  # noqa: E402

strip_ticket_board_pane_env(os.environ)

from scripts.ticket_board import board_skill, notify_listener  # noqa: E402

HOOK = ROOT / "scripts" / "ticket-board-pane-idle-hook"
# Every runtime whose start the hook answers. Each names its start differently,
# and the point of listing them here is that a payload for any of them must not
# carry a skill sentence.
SESSION_START_SOURCES = (
    "startup",
    "resume",
    "compact",
    "clear",
    "hermes.on_session_start",
    "gemini.SessionStart",
    "codex.SessionStart",
)
SKILL_NAMES = tuple(skill.name for skill in board_skill.CANONICAL_SKILLS)


def load_hook() -> Any:
    loader = importlib.machinery.SourceFileLoader("syrd106_pane_hook", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def assert_says_nothing_about_skills(text: str, where: str) -> None:
    """No instruction to load a skill, in any wording.

    Phrasing is the thing to test rather than one removed sentence: a reworded
    reminder is the same defect. Document names are excluded first, because the
    director's onboarding guide is legitimately called
    switchyard-director-guide.md and naming a file to read is not telling a
    session to load a skill.
    """
    stripped = re.sub(r"[\w./-]*switchyard-[a-z-]+\.md", "", text)
    for name in SKILL_NAMES:
        assert name not in stripped, f"{where} names the {name} skill: {text!r}"
    for phrase in (
        "Load the",
        "load the switchyard",
        "before reading from or writing to the ticket board",
        "then read the whole ticket before acting",
    ):
        assert phrase not in text, f"{where} still instructs: {text!r}"


# --- the listener side ------------------------------------------------------


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, target: str, message: str) -> None:
        self.sent.append((target, message))


def test_the_listener_has_no_way_left_to_append_a_pointer() -> None:
    """Removed rather than disabled: there is no helper to call again."""
    for attribute in (
        "with_board_skill_instruction",
        "board_skill_instruction_for_role",
        "BOARD_SKILL_INSTRUCTION",
    ):
        assert not hasattr(notify_listener, attribute), attribute
    source = HOOK.read_text(encoding="utf-8")
    for attribute in (
        "_with_board_skill_instruction",
        "_skill_instruction_for_role",
        "BOARD_SKILL_INSTRUCTION",
        "DIRECTOR_SKILL_INSTRUCTION",
    ):
        assert attribute not in source, f"the pane hook still defines {attribute}"


def test_a_transition_is_delivered_verbatim_for_every_role() -> None:
    """Director and implementer alike: what was enqueued is what arrives."""
    from ticket_board_notify_listener_test import FakeConnection, queue_row

    for role, target in (
        ("director", "pgu-director:0.0"),
        ("ops", "pgu-ops:0.0"),
        ("app", "pgu-app:0.0"),
    ):
        message = f"PGU-106 -- Title entered Implementation for {role}"
        sender = RecordingSender()
        conn = FakeConnection([queue_row(1, "PGU-106", target_role=role, message=message)])
        listener = notify_listener.TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=sender,
            activity_gate=lambda _target: False,
            target_exists=lambda _target: True,
            connector=lambda *args, **kwargs: conn,
            poll_seconds=0,
        )
        assert listener.listen_once(max_notifications=1) == 1
        assert sender.sent == [(target, message)], (role, sender.sent)
        assert_says_nothing_about_skills(sender.sent[0][1], f"the {role} queue hand-off")


def test_the_notify_driven_delivery_path_is_verbatim_too() -> None:
    """There are two ways a hand-off reaches a pane and both had the pointer.

    listen_once drains due rows; deliver_payload answers a LISTEN notification
    directly. Testing only the first left the second free to reacquire the
    sentence, which is exactly what a mutation of it showed.
    """
    for role, target in (("director", "pgu-director:0.0"), ("ops", "pgu-ops:0.0")):
        message = f"PGU-106 -- Title entered Implementation for {role}"
        sender = RecordingSender()
        listener = notify_listener.TicketBoardNotifyListener(
            conninfo="dbname=test",
            sender=sender,
            activity_gate=lambda _target: False,
            target_exists=lambda _target: True,
            poll_seconds=0,
        )
        payload = json.dumps(
            {
                "kind": "transition",
                "id": "PGU-106",
                "target_role": role,
                "message": message,
                "new_state": "in_progress",
            }
        )
        assert listener.deliver_payload(payload) is True, role
        assert sender.sent == [(target, message)], (role, sender.sent)
        assert_says_nothing_about_skills(sender.sent[0][1], f"the {role} notify hand-off")


# --- the hook side ----------------------------------------------------------


def hook_session_start(source: str, *, role: str = "ops", **environ: str) -> Any:
    hook = load_hook()
    return hook._session_start_additional_context_output(
        target=f"porter-{role}:0.0",
        payload={"source": source},
        environ={
            "TICKET_BOARD_CALLER_ROLE_MAP": json.dumps({f"porter-{role}": role}),
            **environ,
        },
    )


def test_no_session_start_of_any_kind_carries_a_skill_sentence() -> None:
    for source in SESSION_START_SOURCES:
        for role in ("director", "ops"):
            output = hook_session_start(source, role=role)
            if output is None:
                continue
            text = str(output["hookSpecificOutput"]["additionalContext"])
            assert_says_nothing_about_skills(text, f"{role} start {source!r}")


def test_a_fresh_start_with_nothing_to_say_emits_nothing() -> None:
    """It used to emit the pointer alone, which is the repetition this removes.

    A director on a tenant that predates stored onboarding still gets the
    packet through the legacy bridge, so that case is pinned with the marker
    set, which is what a migrated tenant looks like.
    """
    assert hook_session_start("startup", role="ops") is None
    assert (
        hook_session_start(
            "startup", role="director", TICKET_BOARD_ROLE_ONBOARDING_MIGRATED="1"
        )
        is None
    )


def test_onboarding_still_reaches_a_fresh_session_unadorned() -> None:
    for role in ("director", "ops"):
        output = hook_session_start(
            "startup", role=role, TICKET_BOARD_ROLE_ONBOARDING_PROMPT="Your remit body."
        )
        text = str(output["hookSpecificOutput"]["additionalContext"])
        assert "Your remit body." in text, role
        assert text.startswith(f"Your {role} session started."), text
        assert_says_nothing_about_skills(text, f"{role} onboarding")


def test_the_skills_are_still_installed_and_still_advertised() -> None:
    """Removing the reminder must not remove the thing it pointed at."""
    assert [skill.name for skill in board_skill.skills_for_role("director")] == [
        "switchyard-board",
        "switchyard-director",
    ]
    assert [skill.name for skill in board_skill.skills_for_role("ops")] == ["switchyard-board"]
    for skill in board_skill.CANONICAL_SKILLS:
        body = (ROOT / "skills" / skill.name / "SKILL.md").read_text(encoding="utf-8")
        assert body.strip(), skill.name
        # The requirement the notification used to repeat lives here, where a
        # session reads it once.
        if skill.name == "switchyard-board":
            assert "read the whole ticket" in body.lower(), "the skill stopped saying it"


def test_an_installed_pane_hook_stays_silent_on_a_fresh_start() -> None:
    """Through the real program, not its imported functions."""
    with tempfile.TemporaryDirectory(prefix="syrd106-hook.") as tmp:
        state_dir = Path(tmp) / "state"
        session_dir = Path(tmp) / "sessions"
        for source in ("startup", "hermes.on_session_start", "gemini.SessionStart"):
            proc = subprocess.run(
                [
                    str(HOOK),
                    "idle",
                    "--target",
                    "porter-ops:0.0",
                    "--source",
                    source,
                    "--state-dir",
                    str(state_dir),
                    "--session-dir",
                    str(session_dir),
                ],
                input=json.dumps({"source": source, "session_id": "s1"}),
                text=True,
                capture_output=True,
                check=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": tmp,
                    "TICKET_BOARD_CALLER_ROLE_MAP": json.dumps({"porter-ops": "ops"}),
                },
            )
            assert proc.stdout.strip() in ("", "{}"), (source, proc.stdout)


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"ticket_board_no_repeated_skill_reminder_test: ok ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
