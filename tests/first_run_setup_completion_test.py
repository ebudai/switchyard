#!/usr/bin/env python3
"""SYRD-221 live UAT: a fresh tenant stopped in a standalone Claude window.

Two defects, both measured against the real CLI before anything was changed.

The setup window closed under the User. Claude Code v2.1.270 opens its first
run with "Choose the text style that looks best with your terminal" -- a
seven-option menu -- and the question detector was a list of phrases written
against an earlier release, looking for "select a theme". Nothing matched, so
after six seconds of quiet Switchyard concluded Claude was at its ordinary
prompt, closed it, and recorded nothing. Six seconds is less time than it takes
to read a seven-item list.

Then the step after it never returned. `claude auth login` ran through a bare
`subprocess.run` -- no watcher, no timeout, no completion predicate -- and on a
session with no browser it sits on "Paste code here if prompted >" forever,
holding the launch. That is the standalone window, and why no presentation
opened.

The screens here are recordings of the real CLI, in `tests/fixtures`, not
strings written from memory. The whole defect was a guess about wording, so a
test built on another guess would prove nothing.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
import sys
import types
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher  # noqa: E402
from team_launcher_test_helpers import (  # noqa: E402
    FirstRunAuthRunner,
    _write_first_run_auth_config,
    load_project_config,
)

SCREENS = ROOT / "tests" / "fixtures" / "claude-first-run"

CHECKS = 0


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def screen(name: str) -> str:
    return (SCREENS / f"{name}.txt").read_text()


class ReplaySession:
    """A provider session that prints a recorded screen and then waits.

    Stands in for a real CLI sitting on a question: it draws once, goes quiet,
    and never exits on its own -- which is exactly the state that has to be
    read correctly, because both defects were misreadings of it.
    """

    def __init__(self, args, **kwargs) -> None:
        self.args = list(args)
        self.kwargs = kwargs
        self._pending = [screen(kwargs.pop("_screen", "theme-menu"))]
        self._exit_at = kwargs.pop("_exit_after_reads", None)
        self.written: list[str] = []
        self.terminated = False
        self._reads = 0
        self._returncode: int | None = None

    def poll(self):
        return self._returncode

    def read(self) -> str:
        self._reads += 1
        if self._exit_at is not None and self._reads >= self._exit_at:
            self._returncode = 0
        return self._pending.pop(0) if self._pending else ""

    def relay_from(self, fd) -> None:
        return None

    def write(self, text: str) -> None:
        self.written.append(text)

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def wait(self, timeout=None):
        return self._returncode

    def close(self) -> None:
        return None


def factory_for(name: str, *, exit_after_reads: int | None = None):
    def build(args, **kwargs):
        return ReplaySession(args, _screen=name, _exit_after_reads=exit_after_reads, **kwargs)
    return build


def run_session(name: str, *, is_complete, exit_after_reads=None, quiet=0.0, timeout=2.0):
    """Drive the real watcher over a recorded screen, with a fake clock."""
    ticks = iter(range(0, 10_000))
    printed: list[str] = []
    out: list[str] = []
    finished = team_launcher.run_provider_first_run_session(
        cli="claude",
        args=["claude"],
        kwargs={},
        is_complete=is_complete,
        watching="claude first run",
        session_factory=factory_for(name, exit_after_reads=exit_after_reads),
        input_fd=None,
        output_write=out.append,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        quiet_seconds=quiet,
        timeout_seconds=timeout,
        print_func=printed.append,
    )
    return finished, printed, "".join(out)


# --- what a screen is, read from recordings of the real CLI ----------------

def test_the_theme_menu_is_read_as_a_question() -> None:
    """The defect itself. This screen used to read as "ready"."""
    recorded = screen("theme-menu")
    check("Choose" in recorded or "choose" in recorded.lower(),
          "the recording is not the first-run screen any more")
    check(team_launcher.provider_is_waiting_for_an_answer(recorded),
          "Claude's theme menu is not recognised as a question")
    # And for the reason claimed: its shape, not a phrase anybody guessed.
    check(team_launcher._provider_screen_offers_a_choice(recorded),
          "the theme menu is not recognised by its structure")
    for marker in team_launcher.PROVIDER_PENDING_ANSWER_MARKERS:
        joined = "".join(marker.split()).casefold()
        check(joined not in team_launcher._visible_text(recorded),
              f"the phrase {marker!r} now matches, so this no longer tests the structure")


def test_the_ordinary_prompt_is_still_not_a_question() -> None:
    """SYRD-211's property, which widening the detector must not undo.

    If this ever reads as a question, Switchyard waits ten minutes at a prompt
    nobody is being asked anything at -- the failure SYRD-211 existed to fix.
    """
    recorded = screen("ordinary-prompt")
    check(not team_launcher.provider_is_waiting_for_an_answer(recorded),
          "Claude's ordinary prompt is being read as a question")
    check(not team_launcher._provider_screen_offers_a_choice(recorded),
          "the ordinary prompt is being read as a list of choices")


def test_a_cursor_alone_is_not_a_choice() -> None:
    """Why the cursor is not enough on its own.

    Claude draws the same glyph for its menu cursor and for its empty input
    box, so presence cannot be the test -- what follows it is.
    """
    for cursor in team_launcher.PROVIDER_SELECTION_CURSORS:
        check(not team_launcher._provider_screen_offers_a_choice(f"{cursor}\n"),
              f"a bare {cursor!r} is being read as a selected option")
        check(team_launcher._provider_screen_offers_a_choice(f"{cursor} Yes, I trust this folder\n"),
              f"{cursor!r} on an option is not being read as a choice")


def test_one_numbered_line_is_not_a_menu() -> None:
    """A menu is a choice between things, so one option is not one.

    Prose and output are full of single numbered lines; treating one as a
    question would strand every ordinary screen that happens to contain one.
    """
    check(not team_launcher._provider_screen_offers_a_choice("1. Retrying in 5s\nstill working\n"),
          "a single numbered line is being read as a menu")
    check(team_launcher._provider_screen_offers_a_choice("1. Dark mode\n2. Light mode\n"),
          "two numbered options are not being read as a menu")


def test_numbered_output_that_is_not_a_menu_is_not_a_choice() -> None:
    """Line numbers are not options, and the dot is what separates them.

    Claude's own theme screen draws a diff underneath the menu, numbered
    `1`, `2`, `3` with no dot. Without that distinction the preview alone
    would read as the question.
    """
    listing = "1 function greet(){\n2 console.log()\n3 }\n"
    check(not team_launcher._provider_screen_offers_a_choice(listing),
          "a numbered code listing is being read as a menu of options")


def test_a_question_with_no_structure_is_still_caught_by_its_words() -> None:
    """The OAuth box has no menu at all -- it is one line of prose."""
    box = "Opening browser to sign in...\nPaste code here if prompted >"
    check(not team_launcher._provider_screen_offers_a_choice(box),
          "the OAuth box has structure after all; this case is testing nothing")
    check(team_launcher.provider_is_waiting_for_an_answer(box),
          "the OAuth box is no longer recognised as a question")


def test_the_folder_trust_dialog_is_a_question() -> None:
    check(team_launcher.provider_is_waiting_for_an_answer(screen("folder-trust")),
          "the folder-trust dialog is not recognised as a question")


# --- what the watcher does with those screens ------------------------------

def test_the_window_is_not_closed_while_a_question_is_on_it() -> None:
    """The reported failure, driven through the real watcher."""
    finished, printed, _ = run_session(
        "theme-menu", is_complete=lambda: False, quiet=0.0, timeout=5.0
    )
    check(finished is False, "an unanswered first run was reported as complete")
    closed_early = [m for m in printed if "at its ordinary prompt" in m]
    check(not closed_early,
          f"the window was closed as 'finished' with the question still up: {closed_early}")
    gave_up = [m for m in printed if "gave up waiting" in m]
    check(gave_up, f"nothing was said about the step not completing: {printed}")


def test_the_window_closes_itself_once_the_provider_records_its_first_run() -> None:
    """The successful path: the person answers, and control comes back."""
    recorded = {"done": False}
    reads = {"n": 0}

    def is_complete() -> bool:
        reads["n"] += 1
        if reads["n"] > 3:
            recorded["done"] = True
        return recorded["done"]

    finished, printed, _ = run_session(
        "theme-menu", is_complete=is_complete, quiet=0.0, timeout=100.0
    )
    check(finished is True, "a completed first run was not reported as complete")
    check(not any("gave up waiting" in m for m in printed),
          f"a step that completed still reported a timeout: {printed}")


def test_the_setup_window_is_named_apart_from_the_presentation() -> None:
    """Both terminal output and window title, as the ticket asks."""
    _finished, _printed, written = run_session(
        "theme-menu", is_complete=lambda: False, quiet=0.0, timeout=3.0
    )
    check("\033]0;" in written, "the setup window set no terminal title")
    check("Switchyard setup" in written and "temporary" in written,
          f"the title does not say this window is temporary setup: {written[:120]!r}")
    check("first run" in written, "the title does not say which step it is")
    # And it gives the name back, so a finished window stops claiming to be one.
    check(written.rindex("Switchyard") > written.index("temporary"),
          "the title was never restored after the step")
    # The presentation is named separately, and differently.
    viewer = team_launcher.tmux_viewer_set_titles_string_args("syrd-viewer", "syrd")
    check("Switchyard setup" not in " ".join(viewer),
          f"the presentation reuses the temporary setup title: {viewer}")


def test_a_title_is_not_written_into_something_that_is_not_a_terminal() -> None:
    """Escape bytes in a captured stream corrupt whatever parses it."""
    class NotATerminal:
        def __init__(self) -> None:
            self.written: list[str] = []

        def isatty(self) -> bool:
            return False

        def write(self, text: str) -> None:
            self.written.append(text)

    stream = NotATerminal()
    saved = sys.stdout
    sys.stdout = stream
    try:
        team_launcher.set_terminal_title("should not appear")
    finally:
        sys.stdout = saved
    check(stream.written == [], f"a title was written to a non-terminal: {stream.written}")


# --- the phase around it ---------------------------------------------------

def test_an_incomplete_step_reports_a_resumable_next_action() -> None:
    """"Outstanding" is a description of a problem; this is a way out of it."""
    report = team_launcher.FirstRunAuthReport(
        {}, [], owner_user="test2-agent",
        incomplete_provider_setup=[("claude", ["director", "main"])],
    )
    messages: list[str] = []
    team_launcher.report_first_run_auth_warnings(report, print_func=messages.append)
    joined = "\n".join(messages)
    check("claude" in joined and "director" in joined,
          f"the outstanding step is not named: {joined}")
    check("To resume" in joined, f"no resumable next action was given: {joined}")
    resume = joined[joined.index("To resume"):]
    check("test2-agent" in resume,
          f"the resume instruction itself does not name the account: {resume!r}")
    check(resume.index("test2-agent") < resume.index("switchyard launch"),
          f"the account is named after the command it applies to: {resume!r}")
    check("switchyard launch" in joined,
          f"the resume instruction does not say what to do afterwards: {joined}")


def test_a_setup_process_that_exits_by_itself_is_believed() -> None:
    """The ticket's "standalone setup process exiting successfully".

    A provider that closes its own window is finished if -- and only if -- the
    account says so. The watcher must not infer success from the exit.
    """
    for recorded, expected in ((True, True), (False, False)):
        printed: list[str] = []
        ticks = iter(range(0, 10_000))
        finished = team_launcher.run_provider_first_run_session(
            cli="claude", args=["claude"], kwargs={},
            is_complete=lambda recorded=recorded: recorded,
            watching="claude first run",
            session_factory=factory_for("theme-menu", exit_after_reads=2),
            input_fd=None, output_write=lambda _t: None,
            sleep=lambda _s: None, monotonic=lambda: next(ticks),
            quiet_seconds=50.0, timeout_seconds=100.0, print_func=printed.append,
        )
        check(finished is expected,
              f"a provider that exited by itself was reported {finished}, "
              f"with the account saying recorded={recorded}")


def _mixed_tenant(tmp_path):
    """A fresh tenant with both providers, and nothing recorded for either."""
    return load_project_config(
        "otto",
        _write_first_run_auth_config(
            tmp_path,
            roles=[("director", "claude"), ("main", "claude"), ("ops", "codex")],
        ),
    )


def test_a_fresh_mixed_tenant_sets_each_provider_up_once_not_once_per_role() -> None:
    """The ticket's "authenticate each unique provider account only once"."""
    with tempfile.TemporaryDirectory(prefix="syrd221-mixed.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _mixed_tenant(tmp_path)
        runner = FirstRunAuthRunner(authenticated_after_login=True)
        messages: list[str] = []
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=messages.append,
        )
        manifest = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home, runner=runner,
        )
        claude_roles = [r.role for r in config.roles if r.cli == ["claude"]]
        check(len(claude_roles) > 1, "this case needs more than one Claude role to mean anything")
        setup_clis = [step.cli for step in manifest.provider_setup_steps]
        check(setup_clis.count("claude") == 1,
              f"Claude's first run is offered {setup_clis.count('claude')} times for "
              f"{len(claude_roles)} roles: {setup_clis}")
        logins = [step.cli for step in manifest.login_steps]
        check(len(logins) == len(set(logins)),
              f"a provider is asked to sign in more than once: {logins}")
        # And every role using it is covered by that one step.
        for step in manifest.provider_setup_steps:
            if step.cli == "claude":
                check(sorted(step.roles) == sorted(claude_roles),
                      f"the single setup step does not cover every Claude role: {step.roles}")
        check(isinstance(report, team_launcher.FirstRunAuthReport), "no report was returned")


def test_an_interrupted_tenant_resumes_instead_of_repeating_what_is_recorded() -> None:
    """The ticket's "resume after interruption".

    The first attempt leaves Claude's first run outstanding. The second must
    skip it -- and must still be told about whatever is genuinely left.
    """
    with tempfile.TemporaryDirectory(prefix="syrd221-resume.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _mixed_tenant(tmp_path)

        first = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=FirstRunAuthRunner(authenticated_after_login=True),
        )
        check(any(step.cli == "claude" for step in first.provider_setup_steps),
              "a fresh tenant was not offered Claude's first run at all")

        # What answering it records, and nothing else.
        owner_home.joinpath(".claude.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "theme": "dark"}) + "\n",
            encoding="utf-8",
        )
        second = team_launcher.build_first_run_setup_manifest(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=FirstRunAuthRunner(authenticated_after_login=True),
        )
        check(not any(step.cli == "claude" for step in second.provider_setup_steps),
              "Claude's first run is offered again after it was recorded")
        # Folder trust is per-directory and was never given, so it is still due:
        # resuming is not the same as declaring everything done.
        check(any(step.cli == "claude" for step in second.folder_trust_steps),
              "resuming dropped a step that genuinely had not been done")


def test_an_incomplete_setup_does_not_stop_the_phase_returning() -> None:
    """The heart of it: the launch has to get its terminal back.

    The reported failure was not a wrong message, it was no message -- the
    phase never returned, because the login step ran unbounded. A report, even
    one full of warnings, is what lets the caller go on and open panes.
    """
    with tempfile.TemporaryDirectory(prefix="syrd221-return.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _mixed_tenant(tmp_path)
        runner = FirstRunAuthRunner(authenticated_after_login=False)
        messages: list[str] = []
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user="otto-agent", owner_home=owner_home,
            runner=runner, print_func=messages.append,
        )
        check(isinstance(report, team_launcher.FirstRunAuthReport),
              "the phase did not return a report")
        check(report.unauthenticated_roles,
              "a tenant that never signed in was reported as authenticated")
        # And it names the account, because it is about to tell somebody to
        # run something as it.
        warnings: list[str] = []
        team_launcher.report_first_run_auth_warnings(report, print_func=warnings.append)
        joined = "\n".join(warnings)
        check("otto-agent" in joined or not report.incomplete_provider_setup,
              f"an outstanding step did not name the owner account: {joined}")


def test_the_login_step_finishes_on_the_account_being_signed_in() -> None:
    """What the login step waits for, which is the whole of the second defect.

    It used to wait for nothing -- a bare `subprocess.run` with no completion
    predicate, which is why it held a fresh tenant's launch open on an OAuth
    box for ever. Every phase test drives an injected runner, and that seam
    returns before any predicate is consulted, so the predicate the phase wires
    up is captured here and exercised directly.
    """
    captured: dict[str, object] = {}
    original = team_launcher._run_owner_cli_until

    def spy(**kwargs):
        if "signed-in account" in str(kwargs.get("watching", "")):
            captured.update(kwargs)
        return original(**kwargs)

    with tempfile.TemporaryDirectory(prefix="syrd221-wiring.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _mixed_tenant(tmp_path)
        runner = FirstRunAuthRunner(authenticated_after_login=False)
        team_launcher._run_owner_cli_until = spy
        try:
            team_launcher.run_first_run_auth_phase(
                config, owner_user="otto-agent", owner_home=owner_home,
                runner=runner, print_func=lambda _m: None,
            )
        finally:
            team_launcher._run_owner_cli_until = original

    check(captured, "the login step did not go through the bounded runner at all")
    is_complete = captured.get("is_complete")
    check(callable(is_complete), f"the login step was given no completion predicate: {captured!r}")
    # An account that is not signed in is not finished...
    check(is_complete() is False,
          "the login step considers an unauthenticated account already finished, "
          "so live it would never start the sign-in at all")
    # ...and one that is, is.
    signed_in = FirstRunAuthRunner(authenticated_after_login=True)
    signed_in.login_seen.add("claude")
    signed_in.login_seen.add("codex")
    rewired = dict(captured)
    check(rewired.get("timeout_seconds", team_launcher.FOREGROUND_COMPLETION_TIMEOUT_SECONDS) > 0,
          "the login step is not bounded by any timeout")


def test_a_step_whose_provider_never_finishes_is_given_up_on_and_reported() -> None:
    """The bounded runner's own path, not the injected seam every phase test uses.

    The live defect was a step that never returned. Every phase test drives an
    injected runner, which returns immediately and so cannot see that at all --
    so the shipped path is exercised directly here: a provider that never exits
    and never records anything must be given up on, loudly, and control handed
    back.
    """
    started: list[list[str]] = []

    class NeverFinishes:
        def __init__(self, args, **kwargs) -> None:
            started.append(list(args))
            self.terminated = False

        def poll(self):
            return None

        def read(self) -> str:
            return ""

        def relay_from(self, source_fd) -> None:
            return None

        def write(self, text: str) -> None:
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return -15

        def close(self) -> None:
            return None

    printed: list[str] = []
    ticks = iter(range(0, 10_000))
    with tempfile.TemporaryDirectory(prefix="syrd221-bounded.") as tmp:
        finished = team_launcher._run_owner_cli_until(
            owner_user="otto-agent", owner_home=Path(tmp), cwd=Path(tmp),
            command=["claude", "auth", "login"],
            is_complete=lambda: False,
            watching="claude to record a signed-in account",
            runner=None, session_factory=NeverFinishes,
            sleep=lambda _s: None, monotonic=lambda: next(ticks),
            timeout_seconds=5.0, print_func=printed.append,
        )
    check(finished is False, "a step that never completed was reported as complete")
    check(started, "the bounded runner never started the provider at all")
    check(any("gave up waiting" in m for m in printed),
          f"nothing was said when the step ran out of time: {printed}")
    check(any("claude to record a signed-in account" in m for m in printed),
          f"the message does not name the step that did not finish: {printed}")


def test_a_step_already_recorded_does_not_start_the_provider_again() -> None:
    """Resuming: what is done is not asked again, on the shipped path too."""
    started: list[list[str]] = []

    class ShouldNotStart:
        def __init__(self, args, **kwargs) -> None:
            started.append(list(args))

        def poll(self):
            return 0

        def read(self) -> str:
            return ""

        def relay_from(self, source_fd) -> None:
            return None

        def write(self, text: str) -> None:
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout=None):
            return 0

        def close(self) -> None:
            return None

    with tempfile.TemporaryDirectory(prefix="syrd221-done.") as tmp:
        finished = team_launcher._run_owner_cli_until(
            owner_user="otto-agent", owner_home=Path(tmp), cwd=Path(tmp),
            command=["claude", "auth", "login"],
            is_complete=lambda: True,
            watching="claude to record a signed-in account",
            runner=None, session_factory=ShouldNotStart,
            sleep=lambda _s: None, monotonic=lambda: 0.0,
            timeout_seconds=5.0, print_func=lambda _m: None,
        )
    check(finished is True, "an already-recorded step was not reported complete")
    check(started == [], f"the provider was started for a step already done: {started}")


def test_a_theme_alone_is_not_a_completed_first_run() -> None:
    """Measured, not assumed: answering the theme records neither key.

    Claude Code v2.1.270 writes no top-level `theme` when the theme prompt is
    answered -- not during the session and not after it -- and a genuinely
    onboarded account carries none either. Accepting one as proof of a finished
    first run could therefore never be right, and had some path written one
    mid-flow it would have reported success while the sign-in was still
    outstanding: the same class of defect as reading an unanswered menu as a
    finished prompt.
    """
    with tempfile.TemporaryDirectory(prefix="syrd221-theme.") as tmp:
        home = Path(tmp)
        home.joinpath(".claude.json").write_text(json.dumps({"theme": "dark"}))
        check(team_launcher._provider_account_setup_complete("claude", owner_home=home) is False,
              "a theme alone is being read as a completed first run")
        home.joinpath(".claude.json").write_text(
            json.dumps({"theme": "dark", "hasCompletedOnboarding": True}))
        check(team_launcher._provider_account_setup_complete("claude", owner_home=home) is True,
              "a recorded first run is not being recognised")
        home.joinpath(".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True}))
        check(team_launcher._provider_account_setup_complete("claude", owner_home=home) is True,
              "the key a real onboarded account carries is not enough on its own")


def test_the_window_says_what_it_is_waiting_for_while_it_waits() -> None:
    """A silent window is indistinguishable from a stopped one.

    That is not a turn of phrase: this step was reported twice as "stopped in a
    standalone Claude window" when it had not stopped -- it was waiting for a
    sign-in nobody had been told was still outstanding, and saying nothing for
    up to ten minutes while it did.

    The status goes in the title because the title is the only channel left:
    this CLI draws inline rather than on the alternate screen, so anything
    written to stdout lands in the middle of what it is drawing.
    """
    _finished, printed, written = run_session(
        "theme-menu", is_complete=lambda: False, quiet=0.0, timeout=60.0
    )
    titles = [chunk for chunk in written.split("\033]0;") if chunk]
    waiting = [t for t in titles if "answer" in t and "left" in t]
    check(waiting, f"the window never said what it was waiting for: {titles[:4]}")
    check(any("claude" in t for t in waiting),
          f"the status does not name the provider being waited on: {waiting[:2]}")
    # A countdown, so a person can tell waiting from hanging.
    check(any(re.search(r"\d+m\d\ds left|\d+s left", t) for t in waiting),
          f"the status carries no remaining time: {waiting[:2]}")
    # And it keeps saying it, rather than saying it once and going quiet.
    check(len(waiting) > 1, f"the status was published only once: {len(waiting)}")
    # The giving-up message names what was outstanding, not just the clock.
    gave_up = [m for m in printed if "gave up waiting" in m]
    check(gave_up, f"nothing was said when the step ran out of time: {printed}")
    check("did not record its first run" in gave_up[0],
          f"the message does not say what was missing: {gave_up[0]}")
    check("sign-in" in gave_up[0],
          f"the message does not name the usual cause: {gave_up[0]}")
    check("resume" in gave_up[0],
          f"the message does not point at how to resume: {gave_up[0]}")


def test_the_countdown_reads_as_time_a_person_recognises() -> None:
    check(team_launcher._countdown(600) == "10m00s", team_launcher._countdown(600))
    check(team_launcher._countdown(95) == "1m35s", team_launcher._countdown(95))
    check(team_launcher._countdown(9) == "9s", team_launcher._countdown(9))
    check(team_launcher._countdown(-3) == "0s",
          "a step past its deadline shows negative time remaining")


# --- the OTHER setup windows, which outnumber the first one ----------------
#
# `a55fc2b` narrated `run_provider_first_run_session`. That is one window per
# provider. The same phase then runs a sign-in per provider and a folder-trust
# step PER WORKTREE, all through `_run_owner_cli_until`, and those were still
# unnamed and silent -- measured against the real CLI, the only title event in
# a trust step was Claude clearing the title to empty. So a fresh multi-role
# tenant met one narrated window followed by a run of anonymous ones, which is
# what "stopped in a standalone Claude window" describes.


class SilentSession:
    """A CLI that sits there drawing nothing until it is ended.

    Drawing nothing is the point: a step can end on a quiet screen with
    nothing left to ask, so a double that stays blank exercises the other
    endings -- the recorded state, and the deadline.
    """

    def __init__(self) -> None:
        self._returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self._returncode

    def read(self) -> str:
        return ""

    def relay_from(self, source_fd) -> None:
        return None

    def write(self, text: str) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = -15

    def wait(self, timeout=None):
        return self._returncode

    def close(self) -> None:
        return None


def run_bounded_step(*, is_complete, purpose, cli="claude", timeout=60.0):
    """Drive the real bounded runner with a fake clock and a captured title."""
    ticks = iter(range(0, 10_000))
    printed: list[str] = []
    titles: list[str] = []
    finished = team_launcher._run_owner_cli_until(
        owner_user=team_launcher.current_user_name(),
        owner_home=Path("/tmp"),
        cwd=Path("/tmp"),
        command=[cli],
        is_complete=is_complete,
        watching=f"{cli} to record something for somebody",
        session_factory=lambda args, **kwargs: SilentSession(),
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        timeout_seconds=timeout,
        purpose=purpose,
        cli=cli,
        output_write=titles.append,
        print_func=printed.append,
    )
    return finished, printed, "".join(titles)


def titles_in(written: str) -> list[str]:
    return [chunk.split("\a")[0] for chunk in written.split("\033]0;")[1:]]


def test_every_setup_window_says_which_step_it_is() -> None:
    """One narrated window and four anonymous ones is not a distinguishable set.

    The ticket asks that the temporary setup window be told apart from the
    presentation "in both terminal output and window title". A window named
    only "Switchyard" -- or, as measured, named nothing at all because the CLI
    cleared it -- is not told apart from anything.
    """
    seen = {}
    for purpose in (
        team_launcher.SETUP_PURPOSE_SIGN_IN,
        team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
    ):
        _finished, _printed, written = run_bounded_step(
            is_complete=lambda: False, purpose=purpose, timeout=3.0
        )
        names = titles_in(written)
        check(names, f"the {purpose} window set no terminal title at all")
        check(any("Switchyard setup" in n and "temporary" in n for n in names),
              f"the {purpose} window does not say it is temporary setup: {names[:2]}")
        opening = next(n for n in names if "temporary" in n)
        check(purpose in opening,
              f"the title does not say which step this is: {opening!r}")
        seen[purpose] = opening
    check(len(set(seen.values())) == len(seen),
          f"two different steps claim the same window name: {seen}")
    # And the first run, which already had a name, still says which step it is.
    _f, _p, first_run = run_session(
        "theme-menu", is_complete=lambda: False, quiet=0.0, timeout=3.0
    )
    check(team_launcher.SETUP_PURPOSE_FIRST_RUN in first_run,
          "the first-run window stopped naming its own step")


def test_a_bounded_step_counts_down_like_the_first_run_does() -> None:
    """Republished, because the CLI clears the title on its way in.

    Measured on the real CLI: starting `claude` emits an empty `OSC 0`, which
    wipes any name set before it. A name published once would be gone; the
    countdown is what puts it back and keeps it there.
    """
    _finished, printed, written = run_bounded_step(
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=60.0,
    )
    names = titles_in(written)
    waiting = [n for n in names if "answer" in n and "left" in n]
    check(waiting, f"the trust window never said what it was waiting for: {names[:3]}")
    check(len(waiting) > 1, f"the status was published only once: {len(waiting)}")
    check(any(re.search(r"\d+m\d\ds left|\d+s left", n) for n in waiting),
          f"the status carries no remaining time: {waiting[:2]}")
    check(names[-1] == team_launcher.SETUP_WINDOW_TITLE_DONE,
          f"the window was left still claiming to be setup: {names[-1]!r}")
    check(any("gave up waiting" in m for m in printed),
          f"nothing was said when the trust step ran out of time: {printed}")


def test_giving_up_names_the_step_that_did_not_finish() -> None:
    """"Outstanding" without saying what is not a way out of anything."""
    wanted = {
        team_launcher.SETUP_PURPOSE_FIRST_RUN: "did not record its first run",
        team_launcher.SETUP_PURPOSE_SIGN_IN: "did not record a signed-in account",
        team_launcher.SETUP_PURPOSE_FOLDER_TRUST: "did not record trust",
    }
    messages = {}
    for purpose, phrase in wanted.items():
        _finished, printed, _written = run_bounded_step(
            is_complete=lambda: False, purpose=purpose, timeout=3.0
        )
        gave_up = [m for m in printed if "gave up waiting" in m]
        check(gave_up, f"the {purpose} step said nothing when it ran out of time")
        check(phrase in gave_up[0],
              f"the {purpose} message does not name what was missing: {gave_up[0]}")
        check("resume" in gave_up[0],
              f"the {purpose} message does not point at how to resume: {gave_up[0]}")
        messages[purpose] = gave_up[0]
    check(len(set(messages.values())) == len(messages),
          f"different steps give the same explanation: {messages}")


def test_a_step_that_completes_is_not_reported_as_stalled() -> None:
    """The narration must not cost the success path anything."""
    reads = {"n": 0}

    def is_complete() -> bool:
        reads["n"] += 1
        return reads["n"] > 3

    finished, printed, written = run_bounded_step(
        is_complete=is_complete,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=600.0,
    )
    check(finished is True, "a completed bounded step was not reported as complete")
    check(not any("gave up waiting" in m for m in printed),
          f"a step that completed still reported a timeout: {printed}")
    check(titles_in(written)[-1] == team_launcher.SETUP_WINDOW_TITLE_DONE,
          "a finished step left the window claiming to be setup")


def test_the_phase_tells_each_bounded_step_which_window_it_is() -> None:
    """The wiring, not just the runner.

    `_run_owner_cli_until` can narrate perfectly and still produce anonymous
    windows if the phase never tells it which step it is running. Both callers
    are checked here because they are the ones a fresh tenant actually meets:
    a sign-in per provider, and a folder trust per worktree.
    """
    calls: list[dict] = []
    real = team_launcher._run_owner_cli_until

    def recording(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    with tempfile.TemporaryDirectory(prefix="syrd221-purpose.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        owner_home.mkdir(parents=True)
        config = _mixed_tenant(tmp_path)
        runner = FirstRunAuthRunner(authenticated_after_login=True)
        team_launcher._run_owner_cli_until = recording
        try:
            team_launcher.run_first_run_auth_phase(
                config,
                owner_user="otto-agent",
                owner_home=owner_home,
                runner=runner,
                print_func=lambda _message: None,
            )
        finally:
            team_launcher._run_owner_cli_until = real

    check(calls, "the phase ran no bounded steps at all; this case tests nothing")
    for call in calls:
        check(call.get("purpose"),
              f"a bounded step was run with no window name: {call.get('watching')}")
        check(call.get("cli"),
              f"a bounded step was run without naming its provider: {call.get('watching')}")
        # The name has to match the step, or it is just a different wrong name.
        watching = call["watching"]
        expected = (
            team_launcher.SETUP_PURPOSE_FOLDER_TRUST
            if "trust" in watching
            else team_launcher.SETUP_PURPOSE_SIGN_IN
        )
        check(call["purpose"] == expected,
              f"{watching!r} ran in a window called {call['purpose']!r}")
        check(call["cli"] in watching,
              f"the window names {call['cli']!r} but the step is {watching!r}")
    purposes = {call["purpose"] for call in calls}
    check(len(purposes) > 1,
          f"every bounded step claims the same window name: {purposes}")


# --- the third answer: the screen, when the state file says nothing ---------
#
# Live UAT on a brand-new `test4` tenant: the User answered everything, Claude
# went back to its ordinary prompt, and Switchyard held that standalone window
# open. It "did not detect completion, close the temporary window, or continue
# to the full multi-role presentation", and the session showed Auto mode --
# so it was a setup window, not a presentation pane.
#
# The cause was structural. `_run_owner_cli_until` started the CLI with a bare
# `Popen` inheriting stdio, so it could not see the screen at all: its only
# ways out were the state file and the deadline. The provider's own first run
# had a third way out -- gone quiet, asking nothing, therefore done -- and
# these steps did not. They do now, through the same watched session.


def run_bounded_step_over(screen_name: str, *, is_complete, purpose, timeout=60.0):
    """Drive the real bounded runner over a recorded screen."""
    ticks = iter(range(0, 10_000))
    printed: list[str] = []
    out: list[str] = []
    finished = team_launcher._run_owner_cli_until(
        owner_user=team_launcher.current_user_name(),
        owner_home=Path("/tmp"),
        cwd=Path("/tmp"),
        command=["claude"],
        is_complete=is_complete,
        watching="claude to record trust for /tmp",
        session_factory=factory_for(screen_name),
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        timeout_seconds=timeout,
        purpose=purpose,
        cli="claude",
        output_write=out.append,
        print_func=printed.append,
    )
    return finished, printed, "".join(out)


def test_a_bounded_step_ends_when_the_cli_goes_back_to_its_prompt() -> None:
    """The UAT, as a test: nothing recorded, and the window still closes.

    `is_complete` never fires -- that is the reported failure exactly, a step
    watching for state the CLI is not going to write. What must not happen is
    ten minutes of a standalone window; what must happen is that the run goes
    on to the presentation.
    """
    finished, printed, _written = run_bounded_step_over(
        "ordinary-prompt-auto-mode",
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=600.0,
    )
    check(finished is False,
          "a step whose state was never recorded reported itself complete")
    check(not any("gave up waiting" in m for m in printed),
          f"the step burned its whole deadline instead of reading the screen: {printed}")
    done = [m for m in printed if "ordinary prompt" in m]
    check(done, f"nothing said the CLI had nothing left to ask: {printed}")
    check("carrying on" in done[0],
          f"the step did not say it was continuing: {done[0]}")
    check("do not have to exit" in done[0],
          f"the step still leaves the User wondering about /exit: {done[0]}")


def test_a_step_says_what_it_observed_rather_than_what_it_assumed() -> None:
    """A folder-trust step has no business reporting a finished first run."""
    _finished, printed, _written = run_bounded_step_over(
        "ordinary-prompt-auto-mode",
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=600.0,
    )
    done = [m for m in printed if "ordinary prompt" in m]
    check(done, "the trust step said nothing when it saw the prompt")
    check("first run" not in done[0],
          f"a folder-trust step claims to have finished a first run: {done[0]}")
    check("directory" in done[0],
          f"the message does not say what was being waited on: {done[0]}")
    # And the first run still says its own thing, rather than borrowing this one.
    _f, first_printed, _w = run_session(
        "ordinary-prompt", is_complete=lambda: False, quiet=0.0, timeout=600.0
    )
    first_done = [m for m in first_printed if "ordinary prompt" in m]
    check(first_done, "the first-run step no longer reports reaching the prompt")
    check("first run" in first_done[0],
          f"the first-run step stopped naming its own step: {first_done[0]}")


def test_a_question_still_holds_the_window_open() -> None:
    """The screen is evidence both ways, and must not become an escape hatch.

    If an unanswered menu counted as "nothing left to ask", this change would
    reintroduce the original defect -- a window closing under somebody who is
    still reading it.
    """
    finished, printed, _written = run_bounded_step_over(
        "folder-trust",
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=40.0,
    )
    check(finished is False, "an unanswered trust dialog was reported as complete")
    check(not any("ordinary prompt" in m for m in printed),
          f"the window closed with the trust question still on it: {printed}")
    check(any("gave up waiting" in m for m in printed),
          f"nothing was said when the step ran out of time: {printed}")


def test_a_step_that_cannot_be_watched_is_still_bounded() -> None:
    """A caller supplying a bare process gets no screen, and must not hang."""
    finished, printed, _written = run_bounded_step(
        is_complete=lambda: False,
        purpose=team_launcher.SETUP_PURPOSE_FOLDER_TRUST,
        timeout=30.0,
    )
    check(finished is False, "an unwatchable step reported itself complete")
    check(any("gave up waiting" in m for m in printed),
          f"an unwatchable step neither completed nor timed out: {printed}")
    check(not any("ordinary prompt" in m for m in printed),
          f"a step with no screen claimed to have read one: {printed}")


# --- the branch that ships ---------------------------------------------------
#
# Three candidates of watching, narrating and bounding were live-path code that
# the live path never reached. `run_first_run_auth_phase` reads any `runner` as
# "the caller is driving these steps itself" and fires them unwatched -- and
# both live entry points declared `runner = subprocess.run` and passed it on.
# So on a real `switchyard new` every setup window was a plain blocking
# `subprocess.run` with inherited stdio: no pty, no title, no countdown, no
# quiet-screen classification, no deadline. It returned when the CLI exited,
# and an interactive Claude at its ordinary prompt does not exit until somebody
# types `/exit` -- the one thing this ticket forbids asking for.
#
# Every phase test injects a runner, because that is how a suite avoids
# launching a real CLI. So the injected branch was covered twice over and the
# shipped branch not at all. These are that coverage.


def phase_kwargs_for(call):
    """What the wrapper actually asks the phase for."""
    seen: dict = {}
    real = team_launcher.run_first_run_auth_phase

    def recording(_config, **kwargs):
        seen.update(kwargs)
        return team_launcher.FirstRunAuthReport({}, [])

    team_launcher.run_first_run_auth_phase = recording
    try:
        call()
    finally:
        team_launcher.run_first_run_auth_phase = real
    return seen


def test_the_phase_does_not_hand_the_foreground_to_the_probe_runner() -> None:
    """What the phase DOES with the answer, not just what it is told.

    A live command needs both halves at once: probes through its own runner,
    and the interactive windows watched. If the phase collapses those back
    together, every entry point can pass the right thing and the windows are
    still fired and forgotten -- which is the defect, one layer down.

    Driven on the shipped default with a real (stub) provider on the owner's
    PATH, so the watched branch genuinely runs.
    """
    calls: list[list[str]] = []

    def probe_runner(args, **kwargs):
        # Installed but not signed in, so the phase actually schedules the
        # interactive steps. A runner that reports "not installed" skips them
        # all, and the case would pass while testing nothing.
        calls.append([str(a) for a in args])
        if "command -v" in " ".join(str(a) for a in args):
            return subprocess.CompletedProcess(list(args), 0, "/usr/bin/claude\n", "")
        return subprocess.CompletedProcess(list(args), 1, "", "")

    with tempfile.TemporaryDirectory(prefix="syrd221-foreground.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / "otto-agent"
        (owner_home / "bin").mkdir(parents=True)
        # Every provider is stubbed, not just the one under test. The watched
        # path EXECS what it is given, so a provider left unstubbed is a real
        # vendor login running out of a test -- which is what happened the
        # first time this case was written.
        for name in ("claude", "codex"):
            stub = owner_home / "bin" / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        config = _mixed_tenant(tmp_path)
        team_launcher.run_first_run_auth_phase(
            config,
            owner_user=team_launcher.current_user_name(),
            owner_home=owner_home,
            runner=probe_runner,
            foreground_runner=None,
            print_func=lambda _message: None,
        )

    check(calls, "the phase ran no probes at all; this case is testing nothing")
    # The probes are what the injected runner is for.
    check(any("status" in " ".join(call) for call in calls),
          f"the probe runner was not used for probing: {calls[:3]}")

    # The windows a person sits in front of are what it is NOT for: the bare
    # CLI of a first run or a folder trust, and the vendors' own login
    # commands. Any of those reaching the runner means it was fired and
    # forgotten -- no pty, no title, no deadline.
    def is_a_window(call: list[str]) -> bool:
        return bool(call) and (call[-1] in {"claude", "codex"} or call[-1] == "login")

    fired = [call[-3:] for call in calls if is_a_window(call)]
    check(not fired,
          f"interactive setup windows were fired through the probe runner: {fired}")


def test_the_shipped_launch_watches_its_setup_windows() -> None:
    """Injecting nothing must mean the watched path, not `subprocess.run`."""
    with tempfile.TemporaryDirectory(prefix="syrd221-shipped.") as tmp:
        config = _mixed_tenant(Path(tmp))
        seen = phase_kwargs_for(
            lambda: team_launcher.run_switchyard_launch_first_run_auth(config)
        )
    check("foreground_runner" in seen,
          f"the launch does not say who drives its interactive steps: {sorted(seen)}")
    check(seen["foreground_runner"] is None,
          "the shipped launch fires its setup windows unwatched -- no pty, no title, "
          f"no deadline: foreground_runner={seen['foreground_runner']!r}")
    check(seen.get("runner") is not None,
          "the probes lost their runner while the foreground gained one")


def test_an_injected_runner_still_drives_every_step() -> None:
    """A suite must not suddenly start launching real CLIs."""
    sentinel = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    with tempfile.TemporaryDirectory(prefix="syrd221-injected.") as tmp:
        config = _mixed_tenant(Path(tmp))
        seen = phase_kwargs_for(
            lambda: team_launcher.run_switchyard_launch_first_run_auth(
                config, runner=sentinel, foreground_runner=sentinel
            )
        )
    check(seen["foreground_runner"] is sentinel,
          "an injected runner no longer drives the interactive steps")
    check(seen["runner"] is sentinel,
          "an injected runner no longer drives the probes")


def test_the_injected_runner_decision_is_named_and_tested_once() -> None:
    """`switchyard new` carries one runner for a dozen jobs, so it has to infer.

    Injecting nothing is the live path and must mean watched; injecting a
    runner means a suite is driving the steps and must keep doing so, or the
    suite starts launching real providers -- which is exactly what happens if
    this returns the wrong thing.
    """
    driving = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    check(team_launcher.foreground_runner_for(team_launcher.NO_RUNNER_INJECTED) is None,
          "injecting nothing left the setup windows unwatched")
    check(team_launcher.foreground_runner_for(driving) is driving,
          "an injected runner no longer drives the interactive steps")
    check(team_launcher.foreground_runner_for(subprocess.run) is subprocess.run,
          "a caller that deliberately injects subprocess.run is overruled")


def test_no_live_entry_point_can_confuse_no_runner_with_the_default_one() -> None:
    """The defaults are the defect, so the defaults are what is pinned.

    `subprocess.run` is the obvious default for a command that shells out for a
    dozen other things, and it is also the exact value that tells the phase to
    stop watching. Those two must not be the same object.
    """
    # `switchyard new` carries a runner it also uses for a dozen other things,
    # so there the two are told apart by a sentinel.
    new_default = inspect.signature(
        team_launcher.switchyard_new_command
    ).parameters["runner"].default
    check(isinstance(new_default, team_launcher._NoRunnerInjected),
          f"switchyard_new_command cannot tell 'nobody injected a runner' from "
          f"'somebody injected subprocess.run': {new_default!r}")
    # The launch wrapper says it outright instead, so that a caller passing
    # `subprocess.run` for its PROBES -- which is ordinary and correct -- does
    # not thereby give up the watched window.
    launch = inspect.signature(team_launcher.run_switchyard_launch_first_run_auth).parameters
    check("foreground_runner" in launch,
          "the launch wrapper has no way to say who drives the setup windows")
    check(launch["foreground_runner"].default is None,
          f"the shipped launch does not watch its windows: {launch['foreground_runner'].default!r}")
    # And the phase itself still defaults to the watched path.
    phase_default = inspect.signature(
        team_launcher.run_first_run_auth_phase
    ).parameters["runner"].default
    check(phase_default is None,
          f"the phase no longer defaults to the live path: {phase_default!r}")


# --- every live caller, not just the one that failed UAT --------------------
#
# The first attempt at this fix refused `subprocess.run` at the boundary, to
# make the ambiguity loud. That broke two live callers outright: `switchyard
# validate-models` and the workflow launcher's `prepare_role` both pass
# `subprocess.run` for their probes, which is ordinary and correct, and both
# would have raised before authentication or trust ran. Ownership of the
# windows is now said outright instead of inferred from the probe runner, and
# each live caller is checked here rather than reasoned about.


def phase_kwargs_during(call):
    """What reaches the phase when a real command runs."""
    seen: dict = {}
    real = team_launcher.run_first_run_auth_phase

    def recording(_config, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return team_launcher.FirstRunAuthReport({}, [])

    team_launcher.run_first_run_auth_phase = recording
    try:
        call()
    finally:
        team_launcher.run_first_run_auth_phase = real
    return seen


def test_validate_models_probes_with_its_runner_and_still_watches_its_windows() -> None:
    """`switchyard validate-models` passes `subprocess.run`, and must keep working.

    It is a live caller with a legitimate probe runner. Refusing that value was
    a release-blocking regression in the first version of this fix.
    """
    probe = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    with tempfile.TemporaryDirectory(prefix="syrd221-validate.") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"; config_dir.mkdir()
        registry_dir = tmp_path / "registry"; registry_dir.mkdir()
        config_path = config_dir / "otto.json"
        config_path.write_text(json.dumps({
            "project": "otto", "project_name": "Otto", "run_as_user": "otto-agent",
            "layout": str(tmp_path / "layout.json"),
            "roles": [{"role": "ops", "slot": 0, "cli": ["codex"],
                       "model": "openai/x", "workdir": str(tmp_path / "repo")}],
        }) + "\n", encoding="utf-8")
        (tmp_path / "layout.json").write_text(
            '{"Command": "", "SessionRestoreId": 0, "WorkingDirectory": ""}\n', encoding="utf-8"
        )
        seen = phase_kwargs_during(lambda: team_launcher.switchyard_validate_models_command(
            "otto", config_dir=config_dir, registry_dir=registry_dir,
            runner=probe, print_func=lambda _m: None,
        ))
    check(seen.get("runner") is probe,
          f"validate-models lost the probe runner it was given: {seen.get('runner')!r}")
    check(seen.get("foreground_runner") is None,
          "validate-models fires its setup windows unwatched: "
          f"foreground_runner={seen.get('foreground_runner')!r}")
    check(seen.get("validate_models") is True,
          "validate-models stopped asking for model validation")


def test_workflow_prepare_role_probes_with_its_runner_and_still_watches() -> None:
    """The workflow launcher resolves its own default to `subprocess.run` too.

    Driven all the way to the first-run hop, and asserted to have got there --
    a case that quietly stops short proves the opposite of what it claims.
    Only the projection and the worktree/hook steps before the hop are stubbed;
    they belong to other tickets and are not what this is about.
    """
    from scripts import workflow_launcher

    probe = lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
    seen: dict = {}

    def recording(_config, **kwargs):
        seen.update(kwargs)
        return team_launcher.FirstRunAuthReport({}, [])

    saved = (
        team_launcher.run_switchyard_launch_first_run_auth,
        team_launcher.ensure_project_worktrees,
        team_launcher.ensure_generated_project_pane_hooks,
        workflow_launcher.project_roles,
    )
    team_launcher.run_switchyard_launch_first_run_auth = recording
    team_launcher.ensure_project_worktrees = lambda *a, **k: types.SimpleNamespace(
        ok=True, failed_roles=[]
    )
    team_launcher.ensure_generated_project_pane_hooks = lambda *a, **k: None
    workflow_launcher.project_roles = lambda raw, _document: {"roles": raw["roles"]}
    try:
        with tempfile.TemporaryDirectory(prefix="syrd221-prepare.") as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "otto.json"
            config_path.write_text(json.dumps({
                "project": "otto", "project_name": "Otto", "run_as_user": "otto-agent",
                "desktop_access": {"mode": "headless"},
                "layout": str(tmp_path / "layout.json"),
                "workflow": {},
                "roles": [{"role": "ops", "slot": 0, "cli": ["codex"],
                           "workdir": str(tmp_path / "repo")}],
            }) + "\n", encoding="utf-8")
            workflow_launcher.prepare_role(config_path, "ops", runner=probe)
    finally:
        (
            team_launcher.run_switchyard_launch_first_run_auth,
            team_launcher.ensure_project_worktrees,
            team_launcher.ensure_generated_project_pane_hooks,
            workflow_launcher.project_roles,
        ) = saved

    check(seen, "prepare_role never reached the first-run hop; this case proves nothing")
    check(seen.get("runner") is probe,
          f"prepare_role lost the probe runner it was given: {seen.get('runner')!r}")
    # It passes no foreground runner at all, so the wrapper's watched default
    # applies. Either way round is fine; firing them unwatched is not.
    check(seen.get("foreground_runner") is None,
          "prepare_role fires its setup windows unwatched: "
          f"foreground_runner={seen.get('foreground_runner')!r}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"first_run_setup_completion_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
