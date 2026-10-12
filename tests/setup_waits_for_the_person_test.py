#!/usr/bin/env python3
"""SYRD-544: a setup step that may be asking the person waits for them, however long they take.

The User, in first-run setup on a tenant using Hermes, was asked for an API key,
went to look it up, and came back to find the Hermes step closed and setup on
agy's sign-in. Recorded offline from Hermes 0.15.2 (`hermes model` on a pty,
in its own user and network namespace with a throwaway HERMES_HOME; nothing was
sent anywhere, no credential exists), the prompt is

    OPENROUTER_API_KEY (or Enter to cancel):

which no marker recognised, so six seconds of quiet read it as "finished at its
prompt"; and the 600 s deadline would have closed it anyway. Its curses
provider menu, `→ (●) Nous Portal`, read as idle too. The captures are
`tests/fixtures/hermes-model/`.

Every loop case here is the real `run_provider_first_run_session` over a fake
session and a fake clock that only moves when the loop sleeps: the session
draws one screen, records what is typed into it and when it is ended, and ends
itself only when the person ends it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import first_run_setup_completion_test as completion  # noqa: E402
from scripts import provider_session, team_launcher  # noqa: E402
from scripts.provider_screen import (  # noqa: E402
    provider_is_waiting_for_an_answer,
    provider_screen_is_at_ordinary_prompt,
)

CHECKS = 0
HERMES = ROOT / "tests" / "fixtures" / "hermes-model"
CLAUDE = ROOT / "tests" / "fixtures" / "claude-first-run"
#: Hours, not seconds: well past both the quiet window and the deadline.
AWAY = 3 * 3600.0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def screen(directory: Path, name: str) -> str:
    return (directory / f"{name}.txt").read_text(encoding="utf-8")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)


class PersonSession:
    """Draws one screen, then waits for a person who ends it at `ends_at` (or never)."""

    def __init__(self, clock: Clock, first: str, *, ends_at: float | None = None,
                 answered_at: float | None = None, after_answer: str = "") -> None:
        self.clock, self.pending, self.ends_at = clock, [first], ends_at
        self.answered_at, self.after_answer = answered_at, after_answer
        self.written: list[tuple[float, str]] = []
        self.terminated_at: float | None = None
        self.returncode: int | None = None

    def poll(self):
        if self.returncode is None and self.ends_at is not None and self.clock.now >= self.ends_at:
            self.returncode = 0
        return self.returncode

    def read(self) -> str:
        if self.answered_at is not None and self.clock.now >= self.answered_at and self.after_answer:
            text, self.after_answer = self.after_answer, ""
            return text
        return self.pending.pop(0) if self.pending else ""

    def relay_from(self, fd) -> None:
        return None

    def write(self, text: str) -> None:
        self.written.append((self.clock.now, text))

    def terminate(self) -> None:
        if self.terminated_at is None:
            self.terminated_at = self.clock.now
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def close(self) -> None:
        return None


def run(first: str, *, cli: str = "hermes", is_complete=lambda: False, **session) -> tuple[bool, list[str], PersonSession, Clock]:
    clock = Clock()
    made: list[PersonSession] = []

    def factory(args, **kwargs):
        made.append(PersonSession(clock, first, **session))
        return made[0]

    printed: list[str] = []
    finished = provider_session.run_provider_first_run_session(
        cli=cli, args=[cli], kwargs={}, is_complete=is_complete, watching=f"{cli} to record its setup",
        session_factory=factory, input_fd=None, output_write=lambda _text: None,
        sleep=clock.sleep, monotonic=clock.monotonic, print_func=printed.append,
        purpose=provider_session.SETUP_PURPOSE_SIGN_IN)
    return finished, printed, made[0], clock


def test_hermes_screens_read_as_asking_and_none_as_idle() -> None:
    for name in ("provider-menu", "numbered-menu", "api-key-prompt"):
        raw = screen(HERMES, name)
        check(provider_is_waiting_for_an_answer(raw), f"hermes {name}: asking")
        check(not provider_screen_is_at_ordinary_prompt(raw), f"hermes {name}: not the ordinary prompt")
    for name in ("ordinary-prompt", "ordinary-prompt-auto-mode", "prompt-after-trust"):
        check(provider_screen_is_at_ordinary_prompt(screen(CLAUDE, name)), f"claude {name}: positively the ordinary prompt")
    for name in ("theme-menu", "folder-trust", "sign-in-box", "codex-login-waiting", "focus-out-reassert", "trust-answered-erase"):
        check(not provider_screen_is_at_ordinary_prompt(screen(CLAUDE, name)), f"claude {name}: not the ordinary prompt")


def test_the_hermes_api_key_prompt_waits_until_the_person_ends_it() -> None:
    finished, printed, session, clock = run(screen(HERMES, "api-key-prompt"), ends_at=AWAY)
    check(session.terminated_at is None and session.written == [] and clock.now >= AWAY,
          f"open past the quiet window and the deadline, nothing typed into it, until the person ended it "
          f"at {clock.now:.0f}s: terminated at {session.terminated_at}, typed {session.written}")
    check(finished is False and not any("gave up waiting" in line or "ordinary prompt" in line for line in printed),
          f"and nothing claimed it finished or gave up: {printed}")


def test_an_input_prompt_no_marker_knows_is_never_closed_by_quiet_or_deadline() -> None:
    unknown = "\r\nSome future provider needs a value.\r\nPaste the value here: "
    check(not provider_is_waiting_for_an_answer(unknown), "the case is a prompt nothing recognises")
    finished, printed, session, clock = run(unknown, cli="futurecli", ends_at=AWAY)
    check(session.terminated_at is None and session.written == [] and clock.now >= AWAY,
          f"an unrecognised prompt waits for the person: terminated at {session.terminated_at}")
    check(not any("gave up waiting" in line for line in printed), f"and is never given up on: {printed}")


def test_once_the_key_is_recorded_the_step_ends_on_its_own() -> None:
    recorded_at = 900.0
    holder: dict = {}

    def recorded() -> bool:
        return holder["clock"].now >= recorded_at

    clock = Clock()
    holder["clock"] = clock
    made: list[PersonSession] = []

    def factory(args, **kwargs):
        made.append(PersonSession(clock, screen(HERMES, "api-key-prompt"), answered_at=recorded_at,
                                  after_answer="\r\nSaved OPENROUTER_API_KEY to ~/.hermes/.env\r\n"))
        return made[0]

    printed: list[str] = []
    finished = provider_session.run_provider_first_run_session(
        cli="hermes", args=["hermes"], kwargs={}, is_complete=recorded, watching="hermes to record a key",
        session_factory=factory, input_fd=None, output_write=lambda _text: None, sleep=clock.sleep,
        monotonic=clock.monotonic, print_func=printed.append, purpose=provider_session.SETUP_PURPOSE_SIGN_IN)
    check(finished is True and made[0].terminated_at is not None and recorded_at <= made[0].terminated_at < recorded_at + 10,
          f"past 600 s it waited; once recorded it ended itself within seconds: ended at {made[0].terminated_at}")


def test_claude_at_its_ordinary_prompt_is_still_closed_after_the_quiet_window() -> None:
    finished, printed, session, clock = run(screen(CLAUDE, "ordinary-prompt"), cli="claude")
    check(session.terminated_at is not None and session.terminated_at < 60,
          f"a screen positively at rest still closes after the quiet window, as SYRD-221 needs: {session.terminated_at}")
    check(any(text == "/exit\r" for _when, text in session.written), f"with Claude's own exit: {session.written}")


def test_the_window_counts_the_time_waited_not_a_time_left() -> None:
    titles: list[str] = []
    clock = Clock()
    window = provider_session._SetupWindowNarrator(cli="hermes", purpose="sign-in", deadline=600.0,
                                                   monotonic=clock.monotonic, write=titles.append, interval=5.0)
    clock.now = 3700.0
    window.tick()
    check(titles and "waiting for you, 61m40s so far" in titles[-1] and "left" not in titles[-1],
          f"an hour in, the title says how long it has waited and promises no end: {titles}")


# --- moved from first_run_setup_completion_test (SYRD-544): the cases that drive its helpers over
# a screen that may be asking, which now wait for the person instead of reaching a bound ---


def test_the_window_is_not_closed_while_a_question_is_on_it() -> None:
    """The reported failure, driven through the real watcher."""
    finished, printed, _ = completion.run_session(
        "theme-menu", is_complete=lambda: False, quiet=0.0, timeout=5.0, exit_after_reads=completion.AWAY_READS
    )
    check(finished is False, "an unanswered first run was reported as complete")
    closed_early = [m for m in printed if "at its ordinary prompt" in m]
    check(not closed_early,
          f"the window was closed as 'finished' with the question still up: {closed_early}")
    # SYRD-544: and not by the deadline either. It stayed until the person ended it.
    gave_up = [m for m in printed if "gave up waiting" in m]
    check(not gave_up, f"the deadline closed a window with the question still up: {gave_up}")


def test_a_question_still_holds_the_window_open() -> None:
    """The screen is evidence both ways, and must not become an escape hatch.

    If an unanswered menu counted as "nothing left to ask", this change would
    reintroduce the original defect -- a window closing under somebody who is
    still reading it.
    """
    finished, printed, _written = completion.run_bounded_step_over(
        "folder-trust",
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=40.0,
        exit_after_reads=completion.AWAY_READS,
    )
    check(finished is False, "an unanswered trust dialog was reported as complete")
    check(not any("ordinary prompt" in m for m in printed),
          f"the window closed with the trust question still on it: {printed}")
    # SYRD-544: nor by the deadline -- it held until the person ended it.
    check(not any("gave up waiting" in m for m in printed),
          f"the deadline closed the window with the trust question still on it: {printed}")


def test_a_step_that_shows_nothing_waits_for_the_person() -> None:
    """A provider that draws nothing is not known to be idle (SYRD-544).

    It used to be bounded like any other. But a blank screen may be somebody's
    prompt drawn in a way nothing reads, so it waits -- the window still says
    what it is waiting for, and how long it has been -- until the person ends it.
    """
    finished, printed, written = completion.run_bounded_step(
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=30.0,
    )
    check(finished is False, "an unwatchable step reported itself complete")
    check(not any("gave up waiting" in m for m in printed),
          f"a step that may still be asking was given up on: {printed}")
    check(not any("ordinary prompt" in m for m in printed),
          f"a step with no screen claimed to have read one: {printed}")
    check(any("so far" in n for n in completion.titles_in(written)), "and its window still said it was waiting")


def test_the_window_is_not_closed_while_a_sign_in_is_outstanding() -> None:
    """Driven through the real watcher, not just the predicate."""
    finished, printed, _written = completion.run_session(
        "codex-login-waiting", is_complete=lambda: False, quiet=0.0, timeout=30.0,
        exit_after_reads=completion.AWAY_READS,
    )
    check(finished is False, "an unfinished sign-in was reported as complete")
    closed = [m for m in printed if "ordinary prompt" in m]
    check(not closed,
          f"the sign-in was closed as 'finished' while it waited: {closed}")
    # SYRD-544: the sign-in is waited for until the person ends it, never timed out.
    check(not any("gave up waiting" in m for m in printed),
          f"the deadline closed an outstanding sign-in: {printed}")


def test_leaving_for_the_browser_does_not_close_the_sign_in() -> None:
    """test8, on the recorded bytes and the timing the User actually had."""
    session, printed, _out = completion.watch_timeline(
        [(0.0, completion.screen("sign-in-box")), (7.0, completion.screen("focus-out-reassert"))],
        person_ends_at=completion.AWAY,
    )
    check("ordinary prompt" not in printed,
          f"the sign-in box was taken for an ordinary prompt: {printed!r}")
    # SYRD-544: and it was waited out by nobody -- open until the person ended it.
    check("gave up waiting" not in printed and not session.terminated,
          f"the sign-in was ended under the person: {printed!r}")


def test_a_step_still_asking_types_nothing_into_the_screen() -> None:
    """Whatever it is still asking, `/exit` would be taken as the answer.

    On Claude's sign-in box it is pasted as the authorization code. Before
    SYRD-221's fix the deadline path typed it exactly as the finished path does;
    since SYRD-544 the deadline does not end an asking screen at all.
    """
    session, printed, _out = completion.watch_timeline([(0.0, completion.screen("sign-in-box"))], timeout=20.0, person_ends_at=completion.AWAY)
    check("gave up waiting" not in printed, f"the step was given up on while still asking: {printed!r}")
    check(not any("/exit" in text for text in session.written),
          f"the exit input was typed into an unanswered sign-in: {session.written!r}")
    check(not session.terminated, "the provider was ended under the person")



def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print(f"setup_waits_for_the_person_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
