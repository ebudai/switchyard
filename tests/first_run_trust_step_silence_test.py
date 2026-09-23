#!/usr/bin/env python3
"""SYRD-245: a trust step must never go silent on the person answering it.

Fresh Zorin acceptance on the disposable tenant `test15`. The window said:

    switchyard: claude is at its ordinary prompt with nothing left to ask about
    this directory, so this step is done; closing it and carrying on.
    switchyard: claude will now run in /home/test15-agent/test15-worktrees/audit
    as this project's owner so it can be trusted once for audit. Answer the
    trust prompt; the terminal comes back on its own once the answer is
    recorded.

and then nothing. The title was still "Switchyard", the screen showed a cursor
and no Claude trust prompt, and `ps -u test15-agent` found no provider process
at all -- no Claude, no Codex, nothing the announcement had promised.

Two things in this step could produce exactly that, and both are here:

* the terminal is taken raw BEFORE the provider is started, so a spawn that
  fails or never returns leaves a raw terminal, a cursor, and no prompt;
* the window was named AFTER the spawn, so a step that never got that far had
  nothing on the screen saying which step it was or that it was waiting.

The requirement is a disjunction, and the cases below are written as one: a step
must show the provider's prompt, or advance because the answer is already
recorded, or stop with a bounded diagnostic saying what is missing. What it may
never do is the fourth thing -- sit silently.

Nothing here claims to reproduce the live `test15` wait point, which the report
says was not captured. These are the failures this step can have that look like
what was seen, each made impossible to have silently.
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
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher  # noqa: E402
from team_launcher_test_helpers import (  # noqa: E402
    FirstRunAuthRunner,
    _mark_first_run_setup_complete,
    _write_first_run_auth_config,
    load_project_config,
)

CHECKS = 0

#: The tenant and the worktree the run stopped on, kept so this reads as the
#: report does.
OWNER = "test15-agent"
AUDIT_WORKTREE = Path("/home/test15-agent/test15-worktrees/audit")


def check(condition: object, what: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(what)
    CHECKS += 1


def run_trust_step(session_factory, *, is_complete=lambda: False):
    """Drive the real bounded step over a provider that never appears."""
    said: list[str] = []
    window: list[str] = []
    ticks = iter(range(0, 10_000))
    finished = team_launcher._run_owner_cli_until(
        owner_user=OWNER,
        owner_home=Path(f"/home/{OWNER}"),
        cwd=AUDIT_WORKTREE,
        command=["claude"],
        is_complete=is_complete,
        watching=f"claude to record trust for {AUDIT_WORKTREE}",
        session_factory=session_factory,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        cli="claude",
        output_write=window.append,
        print_func=said.append,
    )
    return finished, said, "".join(window)


def test_a_provider_that_cannot_start_says_so_instead_of_raising() -> None:
    """The silence, ended.

    Before this the step printed nothing at all and let the error out as a bare
    exception: the operator had just been told to answer a trust prompt, and the
    next thing they saw was either a traceback with no worktree in it or
    nothing.
    """
    def cannot_start(args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(AUDIT_WORKTREE))

    finished, said, _window = run_trust_step(cannot_start)

    check(finished is False, "a step that never ran did not report itself done")
    check(said, "the step said something rather than nothing")
    message = said[0]
    check("could not be started" in message, f"it says the provider never ran: {message}")
    check(str(AUDIT_WORKTREE) in message, f"and which directory it was: {message}")
    check("claude" in message, f"and which provider: {message}")
    check("nothing is waiting" in message,
          f"and that nobody is being asked for anything: {message}")
    check("sudo -u test15-agent" in message,
          f"and the command to run by hand to see for yourself: {message}")


def test_the_window_is_named_before_the_provider_is_started() -> None:
    """What the screen says while a spawn is still happening.

    On test15 the title was "Switchyard" and the screen a bare cursor: the
    window was named only after the provider had been started, so a step that
    never got that far named itself nowhere.
    """
    named_before: list[str] = []

    def record_then_fail(args, **_kwargs):
        named_before.append("spawn")
        raise OSError(13, "Permission denied")

    _finished, _said, window = run_trust_step(record_then_fail)

    check(named_before == ["spawn"], "the spawn was attempted")
    check("folder trust" in window,
          f"and the window said which step it was before that: {window!r}")
    check("Switchyard setup" in window,
          f"and that it is a temporary setup window: {window!r}")


def test_a_step_whose_answer_is_already_recorded_reports_done() -> None:
    """The second arm of the disjunction: advance, do not ask again."""
    def must_not_run(args, **_kwargs):
        raise AssertionError("a provider was started for a directory already trusted")

    finished, said, _window = run_trust_step(must_not_run, is_complete=lambda: True)

    check(finished is True, "an already-recorded answer is reported as done")
    check(said == [], f"and nothing is said about answering anything: {said}")


def test_an_already_trusted_worktree_is_not_announced_as_a_prompt() -> None:
    """The phase, not just the step.

    Claude records trust for whichever directory its first run was answered in,
    and the manifest was built before that run. So a worktree can be trusted by
    the time this loop reaches it -- and announcing "answer the trust prompt"
    for a step that then completes instantly is a stall as far as anybody
    watching is concerned.
    """
    with tempfile.TemporaryDirectory(prefix="syrd245-trusted.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / OWNER
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path, roles=[("director", "claude"), ("audit", "claude")]
            ),
        )
        # Signed in, first run recorded, and every worktree already trusted --
        # the state Claude's own first run leaves behind.
        _mark_first_run_setup_complete(owner_home, config, clis={"claude"}, trust=True)
        runner = FirstRunAuthRunner(authenticated_after_login=True)
        runner.login_seen.add("claude")
        said: list[str] = []
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user=OWNER, owner_home=owner_home,
            runner=runner, print_func=said.append,
        )

    asked = [line for line in said if "Answer the trust prompt" in line]
    check(asked == [], f"nobody was asked to answer a prompt that is already recorded: {asked}")
    check(report.untrusted_roles == [],
          f"and no role is reported untrusted for a worktree that is: {report.untrusted_roles}")


def test_trust_recorded_by_the_first_run_is_not_asked_for_again() -> None:
    """The reachable case, which the manifest cannot filter in advance.

    The manifest is built at the start of the phase, before the provider's own
    first run. That run records trust for whichever directory it was answered
    in -- so a worktree listed as needing trust can already have it by the time
    this loop reaches it. The manifest was right when it was built and is stale
    when it is used, and announcing "answer the trust prompt" for a step that is
    already done is exactly the shape the test15 report describes (SYRD-245).
    """
    with tempfile.TemporaryDirectory(prefix="syrd245-midphase.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / OWNER
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path, roles=[("director", "claude"), ("audit", "claude")]
            ),
        )
        trusted_paths = [
            str(Path(role.workdir).resolve(strict=False)) for role in config.roles
        ]

        class RecordsTrustDuringItsFirstRun(FirstRunAuthRunner):
            """Claude's first run, which records trust as it is answered."""

            def __call__(self, args, **kwargs):
                # Matched on the tail rather than by stripping a prefix: the
                # desktop branch inserts `env -u VAR ...` pairs ahead of the
                # program, and a bare first run is the invocation whose last
                # word is the CLI itself.
                if list(args)[-1:] == ["claude"]:
                    owner_home.joinpath(".claude.json").write_text(
                        json.dumps(
                            {
                                "hasCompletedOnboarding": True,
                                "theme": "dark",
                                "projects": {
                                    path: {"hasTrustDialogAccepted": True}
                                    for path in trusted_paths
                                },
                            }
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                return super().__call__(args, **kwargs)

        runner = RecordsTrustDuringItsFirstRun(authenticated_after_login=True)
        runner.login_seen.add("claude")
        said: list[str] = []
        report = team_launcher.run_first_run_auth_phase(
            config, owner_user=OWNER, owner_home=owner_home,
            runner=runner, print_func=said.append,
        )

    asked = [line for line in said if "Answer the trust prompt" in line]
    already = [line for line in said if "already trusts" in line]
    check(asked == [],
          f"a worktree the first run trusted is not asked for again: {asked}")
    check(already, f"and the step says it is already done: {said}")
    check(report.untrusted_roles == [],
          f"with nothing reported untrusted: {report.untrusted_roles}")


def test_the_ordinary_step_still_shows_the_provider_and_records_the_answer() -> None:
    """The first arm: a provider that does appear is still shown and watched.

    The whole disjunction fails if making the other two arms safe quietly broke
    this one.
    """
    drawn = "Do you trust the files in this folder?\n"
    recorded = {"trusted": False}

    class Prompting:
        """Draws the trust question, then the person answers it.

        The answer is what records the trust -- Switchyard never writes that
        state itself -- so the second screen is the CLI back at its ordinary
        prompt with the directory now trusted.
        """

        def __init__(self, _args, **_kwargs) -> None:
            self._screens = [drawn, "\x1b[2G> "]
            self._returncode = None

        def poll(self):
            return self._returncode

        def read(self) -> str:
            if not self._screens:
                return ""
            chunk = self._screens.pop(0)
            if chunk != drawn:
                recorded["trusted"] = True
            return chunk

        def relay_from(self, _fd) -> None:
            return None

        def write(self, _text: str) -> None:
            self._returncode = 0

        def terminate(self) -> None:
            self._returncode = -15

        def wait(self, timeout=None):
            return self._returncode

        def close(self) -> None:
            return None

    finished, said, window = run_trust_step(
        Prompting, is_complete=lambda: recorded["trusted"]
    )

    check(drawn in window, f"the provider's own prompt reached the screen: {window!r}")
    check(finished is True, f"and the answer was recorded: {finished}")
    check(not any("could not be started" in line for line in said),
          f"with no failure claimed: {said}")


# --- the wait after the trust steps -----------------------------------------
#
# test16, on the release carrying the fix above, halted again -- and after
# Claude trust was reported COMPLETE. By then the setup window has been handed
# back (its title is literally "Switchyard") and the screen cleared, and what
# runs next is model validation: one provider invocation per role, stdin at
# /dev/null, stdout and stderr captured, nothing printed before or between
# them, and no timeout on any of it. A launcher waiting there is silent by
# construction, which is the shape both fresh tenants showed.


def test_a_probe_that_never_answers_is_given_up_on_rather_than_waited_on() -> None:
    """The bound. Without it this call cannot return."""
    asked: list[float] = []

    def never_answers(args, **kwargs):
        asked.append(kwargs.get("timeout", -1.0))
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=kwargs.get("timeout", 0))

    proc = team_launcher._run_owner_cli_probe(
        owner_user=OWNER,
        owner_home=Path(f"/home/{OWNER}"),
        command=["claude", "-p", "prove it"],
        runner=never_answers,
        timeout_seconds=12.0,
    )

    check(asked == [12.0], f"the probe was given a bound to run under: {asked}")
    check(proc.returncode == team_launcher.PROBE_TIMED_OUT_STATUS,
          f"and giving up is recorded as a failure, not a pass: {proc.returncode}")
    check("gave up" in proc.stderr, f"saying it gave up: {proc.stderr}")
    check("12s" in proc.stderr, f"after how long: {proc.stderr}")
    check("sudo -u test15-agent" in proc.stderr,
          f"and the command to run by hand: {proc.stderr}")


def test_a_model_that_never_answers_becomes_a_named_failure() -> None:
    """A stalled probe reaches the operator as a role, a CLI and a model."""
    def never_answers(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=list(args), timeout=kwargs.get("timeout", 0))

    with tempfile.TemporaryDirectory(prefix="syrd245-probe.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / OWNER
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path, roles=[("director", "claude")],
                role_models={"director": "claude-opus-5"},
            ),
        )
        said: list[str] = []
        failures = team_launcher.validate_role_models(
            config.roles, owner_user=OWNER, owner_home=owner_home,
            runner=never_answers, print_func=said.append,
        )

    check(len(failures) == 1, f"the role is reported rather than waited on: {failures}")
    failure = failures[0]
    check((failure.role, failure.cli, failure.model) == ("director", "claude", "claude-opus-5"),
          f"named by role, CLI and model: {failure}")
    check("gave up" in failure.reason or "gave up" in " ".join(failure.evidence),
          f"and the reason says it was given up on: {failure.reason} {failure.evidence}")


def test_the_phase_says_which_model_it_is_checking_before_it_checks() -> None:
    """No silent gap between the last trust step and the panes.

    The window has been handed back and the screen cleared by this point, so a
    probe that says nothing leaves a cursor on an empty screen -- indisting-
    uishable from a launcher that has stopped, which is exactly how test15 and
    test16 were read.
    """
    answered = subprocess.CompletedProcess([], 0, stdout="model-ok\n", stderr="")

    def answers(args, **_kwargs):
        return answered

    with tempfile.TemporaryDirectory(prefix="syrd245-progress.") as tmp:
        tmp_path = Path(tmp)
        owner_home = tmp_path / "home" / OWNER
        owner_home.mkdir(parents=True)
        config = load_project_config(
            "otto",
            _write_first_run_auth_config(
                tmp_path, roles=[("director", "claude"), ("audit", "claude")],
                role_models={"director": "claude-opus-5", "audit": "claude-opus-5"},
            ),
        )
        said: list[str] = []
        team_launcher.validate_role_models(
            config.roles, owner_user=OWNER, owner_home=owner_home,
            runner=answers, print_func=said.append,
        )

    check(len(said) == 2, f"one line per role, before its probe: {said}")
    check(all("checking" in line for line in said), f"each says what it is doing: {said}")
    check(any("director's model (claude-opus-5)" in line for line in said),
          f"naming the role and the model: {said}")
    check(all(str(int(team_launcher.OWNER_CLI_PROBE_TIMEOUT_SECONDS)) in line for line in said),
          f"and how long it may take: {said}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"first_run_trust_step_silence_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
