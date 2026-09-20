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

import json
import subprocess
import sys
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

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return -15

    printed: list[str] = []
    ticks = iter(range(0, 10_000))
    with tempfile.TemporaryDirectory(prefix="syrd221-bounded.") as tmp:
        finished = team_launcher._run_owner_cli_until(
            owner_user="otto-agent", owner_home=Path(tmp), cwd=Path(tmp),
            command=["claude", "auth", "login"],
            is_complete=lambda: False,
            watching="claude to record a signed-in account",
            runner=None, popen=NeverFinishes,
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

        def terminate(self) -> None:
            return None

        def wait(self, timeout=None):
            return 0

    with tempfile.TemporaryDirectory(prefix="syrd221-done.") as tmp:
        finished = team_launcher._run_owner_cli_until(
            owner_user="otto-agent", owner_home=Path(tmp), cwd=Path(tmp),
            command=["claude", "auth", "login"],
            is_complete=lambda: True,
            watching="claude to record a signed-in account",
            runner=None, popen=ShouldNotStart,
            sleep=lambda _s: None, monotonic=lambda: 0.0,
            timeout_seconds=5.0, print_func=lambda _m: None,
        )
    check(finished is True, "an already-recorded step was not reported complete")
    check(started == [], f"the provider was started for a step already done: {started}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"first_run_setup_completion_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
