#!/usr/bin/env python3
"""SYRD-112: what polkit would decide, asked before anything is attempted.

The live failure this exists for: a Director invoked a privileged operation and
the pane sat in authorization for three minutes with no prompt, no child
process and nothing on the board. The operation had not failed; it had not
started, and nothing said so.

The last case here asks the real `pkcheck` on this host, because the exit codes
every other case is built on are a measured contract rather than a documented
one.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts.ticket_board import polkit_preflight  # noqa: E402

CHECKS = 0
ACTION = "org.switchyard.privileged.deploy-release"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _runner(returncode: int, stdout: str = "", stderr: str = ""):
    seen: list[list[str]] = []

    def run(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(list(args), returncode, stdout=stdout, stderr=stderr)

    return run, seen


def test_each_polkit_answer_becomes_its_own_state() -> None:
    """Refused, unregistered and "needs a human" are three different problems."""
    cases = [
        (0, "", polkit_preflight.AUTHORIZED),
        (1, "", polkit_preflight.REFUSED),
        (2, "polkit\\56result=auth_admin_keep", polkit_preflight.NEEDS_AUTHENTICATION),
        (127, f"Error checking for authorization {ACTION}: ... is not registered",
         polkit_preflight.NOT_REGISTERED),
    ]
    for code, output, expected in cases:
        run, _seen = _runner(code, stderr=output)
        decision = polkit_preflight.polkit_decision(ACTION, pid=123, runner=run)
        check(decision.state == expected, f"exit {code} -> {decision.state}, wanted {expected}")
        check(decision.action_id == ACTION, f"the action is named back: {decision.action_id}")
        check(
            ACTION in decision.describe(),
            f"and named in what an operator reads: {decision.describe()}",
        )


def test_only_an_authorized_answer_may_proceed() -> None:
    for code, may in ((0, True), (1, False), (2, False), (127, False)):
        run, _seen = _runner(code)
        decision = polkit_preflight.polkit_decision(ACTION, pid=1, runner=run)
        check(decision.may_proceed is may, f"exit {code}: may_proceed={decision.may_proceed}")


def test_the_process_is_named_by_identity_not_by_uid_alone() -> None:
    """A uid is shared by every role in a tenant; a pane's process is not."""
    run, seen = _runner(0)
    polkit_preflight.polkit_decision(ACTION, pid=4242, start_time=99887766, uid=1002, runner=run)
    argv = seen[0]
    check("--process" in argv, f"the process is named: {argv}")
    check(
        argv[argv.index("--process") + 1] == "4242,99887766,1002",
        f"as pid,start_time,uid: {argv}",
    )
    check(
        "--allow-user-interaction" not in argv and "-u" not in argv,
        f"and it never asks for interaction, which is what used to hang: {argv}",
    )


def test_a_pid_without_a_start_time_is_still_asked_about() -> None:
    run, seen = _runner(0)
    polkit_preflight.polkit_decision(ACTION, pid=7, runner=run)
    argv = seen[0]
    check(argv[argv.index("--process") + 1] == "7", f"bare pid: {argv}")


def test_polkit_not_answering_is_not_the_same_as_saying_no() -> None:
    """"I could not ask" must not be reported as "you are not allowed".

    Reporting it as a refusal sends an operator looking for a permission that
    was never the problem.
    """
    def timing_out(args, **kwargs):
        raise subprocess.TimeoutExpired(list(args), kwargs.get("timeout", 10))

    decision = polkit_preflight.polkit_decision(ACTION, pid=1, runner=timing_out, timeout=3)
    check(decision.state == polkit_preflight.UNAVAILABLE, decision.state)
    check("did not answer within 3s" in decision.detail, decision.detail)
    check(decision.may_proceed is False, "and nothing proceeds on it")

    def missing(args, **kwargs):
        raise FileNotFoundError("no pkcheck")

    decision = polkit_preflight.polkit_decision(ACTION, pid=1, runner=missing)
    check(decision.state == polkit_preflight.UNAVAILABLE, decision.state)
    check("could not be run" in decision.detail, decision.detail)

    run, _seen = _runner(42, stderr="something new")
    decision = polkit_preflight.polkit_decision(ACTION, pid=1, runner=run)
    check(
        decision.state == polkit_preflight.UNAVAILABLE,
        f"an exit code this does not know is not a refusal: {decision.state}",
    )
    check("exited 42" in decision.detail, decision.detail)


def test_an_unregistered_action_is_recognised_by_what_polkit_says() -> None:
    """Not only by the exit code: the message is the durable part."""
    run, _seen = _runner(1, stderr=f"GDBus.Error:...: Action {ACTION} is not registered")
    decision = polkit_preflight.polkit_decision(ACTION, pid=1, runner=run)
    check(decision.state == polkit_preflight.NOT_REGISTERED, decision.state)
    check(
        "switchyard upgrade" in decision.describe(),
        f"and it says how to install it: {decision.describe()}",
    )


def test_every_state_says_something_an_operator_can_act_on() -> None:
    for state in (
        polkit_preflight.AUTHORIZED,
        polkit_preflight.NEEDS_AUTHENTICATION,
        polkit_preflight.NOT_REGISTERED,
        polkit_preflight.REFUSED,
        polkit_preflight.UNAVAILABLE,
    ):
        described = polkit_preflight.PolkitDecision(state, ACTION, "why").describe()
        check(described.strip() != "" and ACTION in described, f"{state}: {described!r}")
        check(
            "\n" not in described,
            f"{state}: one line, so it fits a board comment and a log: {described!r}",
        )


def test_the_real_pkcheck_answers_the_way_this_is_built_on() -> None:
    """The measured contract, against the polkit actually installed here.

    Every case above encodes exit 0/1/2/127. If this host's polkit ever stops
    agreeing, the rest of this file is fiction and this is what says so.
    """
    check(bool(shutil.which("pkcheck")), "pkcheck is required")

    # An action nobody installed: this is the "missing policy" case, and it is
    # the state a Switchyard action is in before its release installs it.
    decision = polkit_preflight.polkit_decision(
        "org.switchyard.test.definitely-not-installed", pid=os.getpid(), timeout=15
    )
    check(
        decision.state == polkit_preflight.NOT_REGISTERED,
        f"an uninstalled action reads as not-registered: {decision.state} {decision.detail}",
    )

    # And something every session may do without being asked anything.
    decision = polkit_preflight.polkit_decision(
        "org.freedesktop.login1.reboot", pid=os.getpid(), timeout=15
    )
    check(
        decision.state in {polkit_preflight.AUTHORIZED, polkit_preflight.NEEDS_AUTHENTICATION,
                           polkit_preflight.REFUSED},
        f"a real action gets a real answer rather than an unknown one: "
        f"{decision.state} {decision.detail}",
    )
    check(
        decision.state != polkit_preflight.UNAVAILABLE,
        f"polkit was reachable: {decision.detail}",
    )


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("polkit_preflight_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"polkit_preflight_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
