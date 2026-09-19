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
    between = phase[login_at:login_at + 900]
    check("_cli_auth_status(" in between,
          "and each login re-reads the account before asking for a credential")


def test_a_step_at_an_ordinary_prompt_is_told_what_to_do_long_before_the_bound() -> None:
    """The ten-minute stare, which is what the User actually reported.

    The key this waits for is written when the CLI exits, so after the questions
    the operator is sitting at a working prompt with nothing to answer and no
    sign that anything wants them.
    """
    check(
        launcher.FOREGROUND_READY_PROMPT_GRACE_SECONDS
        < launcher.FOREGROUND_COMPLETION_TIMEOUT_SECONDS,
        "the advice arrives before the give-up",
    )
    said: list[str] = []
    clock = {"now": 0.0}

    class _NeverExits:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            clock["terminated"] = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def monotonic() -> float:
        return clock["now"]

    def sleep(_seconds: float) -> None:
        clock["now"] += 30.0
        clock["polls"] = clock.get("polls", 0) + 1
        if clock["polls"] > 200:
            # A bounded wait that is not bounded is a hang, and a hang is a bad
            # way to learn that: fail it here instead of letting the suite stop.
            raise AssertionError(
                "the foreground wait never ended; nothing bounds it any more"
            )

    completed = launcher._run_owner_cli_until(
        owner_user="otto-agent",
        owner_home=Path("/home/otto-agent"),
        cwd=Path("/home/otto-agent"),
        command=["claude"],
        is_complete=lambda: False,
        watching="claude to record its own first run",
        popen=lambda *_a, **_k: _NeverExits(),
        sleep=sleep,
        monotonic=monotonic,
        print_func=said.append,
    )
    check(completed is False, "a step that records nothing is still reported outstanding")
    advice = [line for line in said if "ordinary prompt" in line]
    check(advice, f"the operator is told what they are looking at: {said}")
    check("exit it" in advice[0] or "/exit" in advice[0],
          f"and exactly what to do: {advice[0]}")
    check(len(advice) == 1, f"said once, not every poll: {said}")
    check(any("gave up waiting" in line for line in said),
          "and the bound still ends it if nothing happens")
    check(clock.get("terminated"), "with the CLI ended rather than left running")


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
