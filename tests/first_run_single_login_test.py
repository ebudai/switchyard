#!/usr/bin/env python3
"""SYRD-211 live UAT: one sign-in per provider, and a step that lets go.

The Zorin run reached first-run setup and then failed there. What the User saw:

    1. Claude login
    2. Codex login
    3. Claude login AGAIN
    4. Claude's first-run questions (theme, folder trust)
    5. an ordinary Claude Code session in /home/test-agent -- and no five panes

Two separate faults.

The repeated sign-in: logins ran before each provider's own first run, and
Claude's welcome flow signs in as part of itself. Whoever answered the first
`claude login` was asked for the same credential again a minute later. Setup
now runs first and the account is re-read immediately before each login, so a
provider the welcome flow already settled is not asked twice -- and an account
it does not settle still gets its login.

The session that never ended: the step watches for the key the account writes
when its first run completes, and that key is written when the CLI EXITS. After
the questions Claude drops to its ordinary prompt, so the watcher sees nothing
and holds the terminal for the full ten-minute bound while the operator looks
at a working prompt with no idea anything is waiting. It now says what to do
long before that.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402

def _bounded(clock: dict, step: float, ceiling: float = 2000.0):
    """A fake clock that fails instead of looping.

    A wait that is no longer bounded shows up as a hung suite, which is a poor
    way to learn it: this turns that into an assertion.
    """

    def sleep(_seconds: float) -> None:
        clock["now"] += step
        if clock["now"] > ceiling:
            raise AssertionError("the session wait never ended; nothing bounds it")

    return sleep


class _BlockedOnInput(Exception):
    """Raised out of a blocking read so a hang fails instead of stopping."""


class watchdog:
    """Turn "this blocked forever" into a named failure.

    A hang is the worst way to learn that something blocks: the suite simply
    stops, with no line to read. These cases deliberately drive code with a real
    idle terminal, which is exactly where blocking shows up.
    """

    def __init__(self, seconds: int, what: str) -> None:
        self.seconds = seconds
        self.what = what

    def __enter__(self):
        import signal

        def fire(_signum, _frame):
            raise _BlockedOnInput(self.what)

        self._previous = signal.signal(signal.SIGALRM, fire)
        signal.alarm(self.seconds)
        return self

    def __exit__(self, *_exc):
        import signal

        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._previous)
        return False


CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


class _WelcomeFlowSignsIn:
    """A provider whose own first run signs the account in, as Claude's does.

    Modelled from the behaviour this codebase already measured and documented:
    the welcome flow asks to sign in even when credentials exist, which means it
    is a sign-in and not merely a theme picker.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.signed_in = False

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        command = args[3:] if args[:2] == ["sudo", "-u"] else list(args)
        while command and ("=" in command[0] or command[0] in {"env", "-H"}):
            command = command[1:]
        if command[:1] == ["sh"]:
            return subprocess.CompletedProcess(args, 0, stdout="/usr/local/bin/claude\n")
        if command[:3] == ["claude", "auth", "status"]:
            # The shape the real probe reads: `loggedIn` decides, not the exit.
            payload = '{"loggedIn": true}' if self.signed_in else '{"loggedIn": false}'
            return subprocess.CompletedProcess(args, 0, stdout=payload)
        if command == ["claude"]:
            # The welcome flow: it signs in, and that is the whole point.
            self.signed_in = True
            return subprocess.CompletedProcess(args, 0)
        if command[:3] == ["claude", "auth", "login"]:
            self.signed_in = True
            return subprocess.CompletedProcess(args, 0)
        return subprocess.CompletedProcess(args, 0)

    @property
    def logins(self) -> list[list[str]]:
        return [call for call in self.calls if call[-3:] == ["claude", "auth", "login"]]

    @property
    def welcome_runs(self) -> list[list[str]]:
        return [call for call in self.calls if call[-1:] == ["claude"]]


def test_a_welcome_flow_that_signs_in_is_not_followed_by_a_login() -> None:
    """The duplicate the User was asked to sit through."""
    runner = _WelcomeFlowSignsIn()
    step = launcher.FirstRunAuthLoginStep(
        cli="claude", roles=("designer", "director"),
        command=tuple(launcher.FIRST_RUN_AUTH_LOGIN_COMMANDS["claude"]),
    )
    # The welcome flow runs first, exactly as the phase now orders it.
    runner(["sudo", "-u", "otto-agent", "claude"])
    status = launcher._cli_auth_status(
        "claude", owner_user="otto-agent", owner_home=Path("/home/otto-agent"), runner=runner
    )
    check(status == "authenticated",
          f"the welcome flow left the account signed in: {status}")
    check(runner.logins == [],
          f"so no separate login was needed: {runner.logins}")
    del step


def test_an_account_the_welcome_flow_does_not_settle_still_gets_its_login() -> None:
    """The safety half: skipping a login must never strand an account."""

    class _WelcomeFlowDoesNotSignIn(_WelcomeFlowSignsIn):
        def __call__(self, args, **kwargs):
            command = args[3:] if args[:2] == ["sudo", "-u"] else list(args)
            if command == ["claude"]:
                # Runs, records nothing, signs nobody in.
                self.calls.append(list(args))
                return subprocess.CompletedProcess(args, 0)
            return super().__call__(args, **kwargs)

    runner = _WelcomeFlowDoesNotSignIn()
    runner(["sudo", "-u", "otto-agent", "claude"])
    check(
        launcher._cli_auth_status(
            "claude", owner_user="otto-agent",
            owner_home=Path("/home/otto-agent"), runner=runner,
        ) == "unauthenticated",
        "the account is still not signed in",
    )
    # Which is exactly when the phase runs the login it kept.
    runner(["sudo", "-u", "otto-agent", "claude", "auth", "login"])
    check(
        launcher._cli_auth_status(
            "claude", owner_user="otto-agent",
            owner_home=Path("/home/otto-agent"), runner=runner,
        ) == "authenticated",
        "and the login it kept settles it",
    )


def test_setup_runs_before_the_logins_it_can_make_unnecessary() -> None:
    """Order is the fix; re-reading is what makes the order pay."""
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    phase = source[source.index("def run_first_run_auth_phase("):]
    phase = phase[: phase.index("\ndef ", 1)]
    setup_at = phase.index("for step in manifest.provider_setup_steps:")
    login_at = phase.index("for step in manifest.login_steps:")
    check(setup_at < login_at,
          "the provider's own first run is attempted before any login step")
    # Anchored on the credential itself rather than a character count: what has
    # to hold is that nothing between entering the loop and building the login
    # command asks for a credential without re-reading the account first.
    between = phase[login_at:phase.index("login_command = list(step.command)", login_at)]
    check("_cli_auth_status(" in between,
          "and each login re-reads the account before asking for a credential")


def test_a_provider_at_its_ordinary_prompt_is_ended_by_switchyard() -> None:
    """The failed acceptance, as a fixture.

    The provider asks its questions, the person answers them, and it settles at
    an ordinary prompt and stays there -- exactly what the User saw. Switchyard
    has to end it and carry on: no `/exit`, no Ctrl-D, no window to close, and
    without waiting out the ten-minute bound.
    """
    clock = {"now": 0.0}
    written: list[str] = []

    class _ProviderThatReachesItsPrompt:
        """Questions, then a ready prompt, then silence -- and it stays alive."""

        def __init__(self, _args, **_kwargs) -> None:
            self.screens = [
                "Select\x1b[8Ga\x1b[10Gtheme",
                "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted\x1b[30G>",
                "\x1b[38;5;246m>\x1b[39m  Try \"fix the build\"",
            ]
            self.terminated = False
            self.exited = False

        def read(self) -> str:
            return self.screens.pop(0) if self.screens else ""

        def write(self, text: str) -> None:
            written.append(text)
            # A real provider goes when it is told to; this one does not, so the
            # fallback has to be what actually ends it.

        def relay_from(self, _fd: int) -> None:
            return None

        def poll(self):
            return 0 if self.exited else None

        def terminate(self) -> None:
            self.terminated = True
            self.exited = True

        def wait(self, timeout=None) -> int:
            return 0

        def close(self) -> None:
            return None

    sessions: list[_ProviderThatReachesItsPrompt] = []

    def factory(args, **kwargs):
        session = _ProviderThatReachesItsPrompt(args, **kwargs)
        sessions.append(session)
        return session

    def monotonic() -> float:
        return clock["now"]

    def sleep(_seconds: float) -> None:
        clock["now"] += 1.0
        if clock["now"] > 300.0:
            raise AssertionError("the session was never ended; nothing bounds it")

    said: list[str] = []
    completed = launcher.run_provider_first_run_session(
        cli="claude",
        args=["sudo", "-u", "otto-agent", "claude"],
        kwargs={},
        # The account never records it -- the key is written on exit, which is
        # the whole reason watching for it could not work.
        is_complete=lambda: False,
        watching="claude to record its own first run",
        session_factory=factory,
        sleep=sleep,
        monotonic=monotonic,
        print_func=said.append,
        output_write=lambda _text: None,
    )

    session = sessions[0]
    check(session.terminated, "Switchyard ended the provider's session itself")
    check(written == ["/exit\r"],
          f"by sending the provider's own exit input first: {written}")
    check(clock["now"] < launcher.FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
          f"well before the ten-minute bound: {clock['now']}s")
    check(not any("gave up waiting" in line for line in said),
          f"so the bound was never reached: {said}")
    check(any("ordinary prompt" in line for line in said),
          f"and it says why it closed: {said}")
    # The account never recorded it, so the closing line may not say the step is
    # done -- the screen is not the authority (SYRD-221).
    check(not any("carrying on" in line for line in said),
          f"without claiming the step finished: {said}")
    check(any("not recorded" in line and "switchyard command again" in line
              for line in said),
          f"telling the person what is still missing and how to finish it: {said}")
    check(completed is False,
          "the account still has not recorded it, and that is reported honestly")


def test_a_provider_still_asking_something_is_never_cut_off() -> None:
    """Quiet is not finished. A person reading an OAuth box is silent too."""
    clock = {"now": 0.0}

    class _ProviderStuckOnOAuth:
        def __init__(self, _args, **_kwargs) -> None:
            # Captured from a real Claude first run on this host.
            self.sent = False
            self.terminated = False

        def read(self) -> str:
            if self.sent:
                return ""
            self.sent = True
            return "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted\x1b[30G>"

        def write(self, text: str) -> None:
            # Allowed once the bound gives up -- the provider's own way out is
            # still the polite first move. What must not happen is being closed
            # while the question is still on screen.
            written_while_asking.append((clock["now"], text))

        def relay_from(self, _fd: int) -> None:
            return None

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None) -> int:
            return 0

        def close(self) -> None:
            return None

    written_while_asking: list[tuple[float, str]] = []
    made: list[_ProviderStuckOnOAuth] = []

    def factory(args, **kwargs):
        session = _ProviderStuckOnOAuth(args, **kwargs)
        made.append(session)
        return session

    said: list[str] = []
    launcher.run_provider_first_run_session(
        cli="claude",
        args=["sudo", "-u", "otto-agent", "claude"],
        kwargs={},
        is_complete=lambda: False,
        watching="claude to record its own first run",
        session_factory=factory,
        sleep=_bounded(clock, 5.0),
        monotonic=lambda: clock["now"],
        print_func=said.append,
        output_write=lambda _text: None,
    )
    check(clock["now"] >= launcher.FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
          f"the person was given the whole bound to answer: {clock['now']}s")
    check(any("gave up waiting" in line for line in said),
          f"and only the bound ended it: {said}")
    check(not any("ordinary prompt" in line for line in said),
          f"it was never mistaken for a ready prompt: {said}")
    check(all(at >= launcher.FOREGROUND_COMPLETION_TIMEOUT_SECONDS
              for at, _ in written_while_asking),
          f"nothing was sent into it while the question stood: {written_while_asking}")


def test_a_provider_that_has_printed_nothing_yet_is_not_closed_for_being_quiet() -> None:
    """Silence before anything is drawn is a provider starting up, not one done.

    Ending on quiet alone would close a session in the gap between `exec` and
    its first frame, and the person would never see the questions at all.
    """
    clock = {"now": 0.0}

    class _StillStarting:
        def __init__(self, _args, **_kwargs) -> None:
            self.closed_at: float | None = None

        def read(self) -> str:
            return ""

        def write(self, _text: str) -> None:
            if self.closed_at is None:
                self.closed_at = clock["now"]

        def relay_from(self, _fd: int) -> None:
            return None

        def poll(self):
            return None

        def terminate(self) -> None:
            if self.closed_at is None:
                self.closed_at = clock["now"]

        def wait(self, timeout=None) -> int:
            return 0

        def close(self) -> None:
            return None

    made: list[_StillStarting] = []
    said: list[str] = []
    launcher.run_provider_first_run_session(
        cli="claude",
        args=["sudo", "-u", "otto-agent", "claude"],
        kwargs={},
        is_complete=lambda: False,
        watching="claude to record its own first run",
        session_factory=lambda a, **k: (made.append(_StillStarting(a, **k)) or made[-1]),
        sleep=_bounded(clock, 5.0),
        monotonic=lambda: clock["now"],
        print_func=said.append,
        output_write=lambda _text: None,
    )
    check(made[0].closed_at is not None, "it was ended eventually")
    check(made[0].closed_at >= launcher.FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
          f"but only by the bound, never for being quiet: closed at {made[0].closed_at}s")
    check(not any("ordinary prompt" in line for line in said),
          f"and it was never called ready: {said}")


def test_an_account_that_records_its_first_run_ends_the_session_at_once() -> None:
    """The fast path: when the provider does write the key, do not wait for quiet."""
    clock = {"now": 0.0}
    recorded = {"done": False}

    class _RecordsThenSitsThere:
        def __init__(self, _args, **_kwargs) -> None:
            # The question, then what a real CLI draws once it is answered.
            # This used to record with the question still the last screen and
            # require an immediate close anyway; Claude does record while its
            # trust question is up, and closing there is what test9 rejected
            # (SYRD-221 UAT). The property is unchanged: recorded and not
            # asking closes at once, without waiting for quiet.
            self.frames = ["Select\x1b[8Ga\x1b[10Gtheme", "\x1b[2J\x1b[H> "]
            self.closed_at: float | None = None

        def read(self) -> str:
            if self.frames:
                if len(self.frames) == 1:
                    # Answering the question is what records it.
                    recorded["done"] = True
                return self.frames.pop(0)
            return ""

        def write(self, _text: str) -> None:
            if self.closed_at is None:
                self.closed_at = clock["now"]

        def relay_from(self, _fd: int) -> None:
            return None

        def poll(self):
            return None

        def terminate(self) -> None:
            if self.closed_at is None:
                self.closed_at = clock["now"]

        def wait(self, timeout=None) -> int:
            return 0

        def close(self) -> None:
            return None

    made: list[_RecordsThenSitsThere] = []
    completed = launcher.run_provider_first_run_session(
        cli="claude",
        args=["sudo", "-u", "otto-agent", "claude"],
        kwargs={},
        is_complete=lambda: recorded["done"],
        watching="claude to record its own first run",
        session_factory=lambda a, **k: (made.append(_RecordsThenSitsThere(a, **k)) or made[-1]),
        sleep=_bounded(clock, 1.0),
        monotonic=lambda: clock["now"],
        print_func=lambda _l: None,
        output_write=lambda _text: None,
    )
    check(completed is True, "the account recorded its first run")
    check(made[0].closed_at is not None, "and the session was ended")
    check(made[0].closed_at < launcher.PROVIDER_READY_QUIET_SECONDS * 2,
          f"immediately, without waiting for the screen to go quiet: {made[0].closed_at}s")


def test_a_real_oauth_screen_reads_as_a_question_and_a_prompt_does_not() -> None:
    """Measured against bytes captured from a live first run, not invented.

    These interfaces place every word with a cursor move rather than a space, so
    the raw stream carries `Paste<ESC>[8Gcode<ESC>[13Ghere`. Stripping escapes
    alone leaves `Pastecodehere`, and a marker written with spaces in it matches
    nothing -- which is how a too-tidy version of this check would have closed
    somebody's session mid-sign-in.
    """
    oauth = "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted\x1b[30G>"
    check(launcher.provider_is_waiting_for_an_answer(oauth),
          "the real OAuth box reads as a question")
    check(launcher.provider_is_waiting_for_an_answer("Select\x1b[8Ga\x1b[10Gtheme"),
          "and so does the theme picker")
    check(launcher.provider_is_waiting_for_an_answer("Do you trust the files in this folder?"),
          "and a trust question")
    check(not launcher.provider_is_waiting_for_an_answer(
        "\x1b[38;5;246m>\x1b[39m  Try \"fix the build\""),
        "an ordinary prompt does not")


def test_relaying_a_terminal_with_nothing_typed_does_not_block() -> None:
    """The real relay, on a real terminal, with nobody typing.

    Every session fake in this file stubs `relay_from`, and `input_fd` defaults
    to None -- so the shipped relay was never run by any of them. On a tty
    `os.read(0, ...)` blocks until somebody types, and this call sits inside the
    loop that watches the provider: the screen stops being read, the quiet
    window stops advancing, and a session at its ordinary prompt is never
    closed (SYRD-211 Audit kick-back).
    """
    import os
    import pty
    import time

    keyboard, terminal = pty.openpty()  # stands in for the operator's tty
    child_out, child_in = pty.openpty()
    try:
        session = launcher.PtyForegroundSession.__new__(launcher.PtyForegroundSession)
        session._master = child_in  # noqa: SLF001 - the relay's only dependency
        started = time.monotonic()
        # Nothing has been typed. This must return, not wait.
        with watchdog(10, "relay_from blocked on a terminal with nothing typed"):
            session.relay_from(terminal)
        elapsed = time.monotonic() - started
        check(elapsed < 1.0, f"the relay returned without waiting for input: {elapsed:.2f}s")

        # And when something HAS been typed, it reaches the provider unchanged.
        os.write(keyboard, b"hello\r")
        time.sleep(0.05)
        session.relay_from(terminal)
        # Watchdogged as well: if the relay dropped it, this read has nothing to
        # wait for and would stop the suite rather than fail it.
        with watchdog(10, "what was typed never reached the provider"):
            forwarded = os.read(child_out, 1024)
        check(b"hello" in forwarded, f"what was typed reached the provider: {forwarded!r}")
    finally:
        for fd in (keyboard, terminal, child_out, child_in):
            try:
                os.close(fd)
            except OSError:
                pass


def test_the_watch_loop_advances_while_a_real_terminal_sits_idle() -> None:
    """End to end on the shipped class, with a real idle tty as the input.

    No fake session and no stubbed relay: a real provider process on a real pty,
    a real terminal handed in as `input_fd`, and nobody typing into it. The loop
    has to keep reading the provider and close it at its ordinary prompt.
    """
    import os
    import pty
    import sys as _sys
    import time

    keyboard, terminal = pty.openpty()
    said: list[str] = []
    # A provider that prints an ordinary prompt and then waits forever.
    child = [
        _sys.executable, "-c",
        "import sys,time; sys.stdout.write('> Try \"fix the build\"'); "
        "sys.stdout.flush(); time.sleep(300)",
    ]
    started = time.monotonic()
    try:
        with watchdog(20, "the watch loop blocked on an idle terminal instead of polling it"):
            completed = launcher.run_provider_first_run_session(
                cli="claude",
                args=child,
                kwargs={},
                is_complete=lambda: False,
                watching="a provider that reaches its prompt",
                input_fd=terminal,
                quiet_seconds=1.0,
                timeout_seconds=30.0,
                    print_func=said.append,                output_write=lambda _text: None,
            )
    finally:
        for fd in (keyboard, terminal):
            try:
                os.close(fd)
            except OSError:
                pass
    elapsed = time.monotonic() - started
    check(elapsed < 20.0,
          f"the loop kept running while the terminal sat idle: {elapsed:.1f}s")
    check(any("ordinary prompt" in line for line in said),
          f"and closed the provider at its prompt: {said}")
    check(not any("gave up waiting" in line for line in said),
          f"rather than by the bound: {said}")
    check(completed is False, "the account recorded nothing, reported honestly")


def main() -> int:
    failures = 0
    for name, value in sorted(globals().items()):
        if not (name.startswith("test_") and callable(value)):
            continue
        try:
            value()
        except BaseException as exc:  # noqa: BLE001
            failures += 1
            print(f"FAILED {name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"first_run_single_login_test: {failures} failed")
        return 1
    print(f"first_run_single_login_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
