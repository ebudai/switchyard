#!/usr/bin/env python3
"""SYRD-685: every upgrade dry run ends on exactly one verdict -- READY TO APPLY or NOT READY -- that says nothing changed.

During PIE's recovery, `sudo switchyard upgrade pie --deploy-ref <commit> --dry-run` succeeded after a long run of
source selection, would-change actions, deploy commands and phase statuses, with nothing at the end saying whether
the applying command was safe. A refusal said STOP; success said nothing.

Here the real `upgrade_project_command` runs with each phase stood in on the launcher, through every way out:
- each phase stopping it, whatever it answers: a code, a refusal of zero, or a foreign answer;
- the role models it cannot hold;
- the finish answering 0 with the release moving, already deployed or withheld, or answering nonzero;
- the role models' check changing that answer;
- `--only upstream-report`;
- a phase raising a worded refusal, a bare exit, or a crash.
Each dry run's last printed line is its one verdict, naming the project and, when ready, the exact commit. Every
answer is unchanged, and so is every raise but one. A worded refusal raised in a dry run is printed where Python
would have printed it, before the verdict, and exits 1 as Python would. Otherwise Python printed it after the
verdict, which a real process shows and an injected printer does not (Audit of 4f3817a9). An upgrade that applies
prints no verdict at all, and raises as before.
tests/team_launcher_upgrade_cutover_test.py runs the same verdicts over real fixtures, end to end.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import team_launcher as t  # noqa: E402
from scripts import upgrade_verdict  # noqa: E402

CHECKS = 0
TARGET = "a" * 40
CURRENT = "b" * 40
PHASES = ("_pin_upgrade_source", "_recover_upgrade_state", "_refresh_upgrade_artifacts", "_stage_upgrade_tooling",
          "_upgrade_identities_and_accounts", "_finish_upgrade")


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def upgrade(*, dry_run: bool = True, answers: dict | None = None, release: dict | None = None, after=None,
            only: str = "") -> tuple[object, list[str], list[str]]:
    """The real command over stand-in phases: (its answer or what it raised, every line it printed, phases run)."""
    answers, ran, said = dict(answers or {}), [], []
    config = SimpleNamespace(project="p685")

    def phase(name, going):
        def run(*args, **kwargs):
            ran.append(name)
            said.append(f"switchyard: {name} detail")
            answer = answers.get(name, "go")
            if isinstance(answer, BaseException):
                raise answer
            if answer == "foreign":
                return SimpleNamespace(label="a foreign answer")
            if answer != "go":
                return answer
            return going(*args, **kwargs)
        return run

    def finish(cfg, **kwargs):
        if release is not None and kwargs.get("verdict") is not None:
            kwargs["verdict"].release(status=SimpleNamespace(**release) if "withheld" not in release else None,
                                      withheld=bool(release.get("withheld")))
        return 0

    going = {
        "_pin_upgrade_source": lambda *a, **k: t.UpgradeSourcePinned(desktop_choice=None, deploy_ref_chosen=True,
                                                                     source_repo=None, commit_git_dir=None,
                                                                     deploy_ref="v685-pinned"),
        "_recover_upgrade_state": lambda *a, **k: t.UpgradeStateReady(source_repo=None, effective_source_repo=Path("/x"),
                                                                      config=config, cutover="cutover"),
        "_refresh_upgrade_artifacts": lambda cfg, **k: cfg,
        "_stage_upgrade_tooling": lambda *a, **k: t.UpgradeToolingStaged(trusted_release_root=None,
                                                                         publication_detail=""),
        "_upgrade_identities_and_accounts": lambda cfg, **k: t.UpgradeIdentitiesDone(config=cfg,
                                                                                    release_report_config=cfg),
        "_finish_upgrade": finish,
    }
    with ExitStack() as stack:
        for name in PHASES:
            stack.enter_context(mock.patch.object(t, name, phase(name, going[name])))
        stack.enter_context(mock.patch.object(
            t, "hold_role_selections_for_upgrade",
            lambda *a, **k: None if answers.get("hold") == "refuse" else {"p685": "held"}))
        stack.enter_context(mock.patch.object(
            t, "role_selections_after_upgrade", lambda held, *, config_path, code, print_func: after(code) if after else code))
        stack.enter_context(mock.patch.object(
            t, "connect_upstream_report_command", lambda *a, **k: said.append("switchyard: upstream detail")
            or answers.get("only", 0)))
        try:
            got = t.upgrade_project_command(config, config_path=Path("/x/p685.json"), dry_run=dry_run, only=only,
                                            print_func=said.append)
        except BaseException as exc:  # noqa: BLE001 - what it raised is the answer under test
            got = exc
    return got, said, ran


def verdicts(said: list[str]) -> list[str]:
    return [line for line in said if line.startswith(upgrade_verdict.PREFIX)]


def one_verdict(said: list[str], what: str) -> str:
    found = verdicts(said)
    check(len(found) == 1 and said[-1] == found[0], f"{what}: exactly one verdict, and it is the last line: {said}")
    return found[0]


# ------------------------------------------------------------------ ready


def test_a_dry_run_that_goes_through_is_ready_and_names_the_exact_commit() -> None:
    got, said, ran = upgrade(release={"target_sha": TARGET, "current_sha": CURRENT, "unchanged": False})
    line = one_verdict(said, "release moving")
    check(got == 0 and ran == list(PHASES), f"the answer and the phases are unchanged: {got} {ran}")
    check(line == f"switchyard: dry-run verdict: READY TO APPLY: p685, release {TARGET}, moving the board from "
                  f"{CURRENT}. Nothing changed; run the same command without --dry-run to apply it. "
                  f"[verdict=ready project=p685 commit={TARGET} exit=0]",
          f"the project, the exact commit it moves to, from which, that nothing changed, and how to apply: {line}")
    _got, said, _ran = upgrade(release={"target_sha": TARGET, "current_sha": TARGET, "unchanged": True})
    line = one_verdict(said, "retained pin")
    check(f"READY TO APPLY: p685, release {TARGET}, already deployed: the board release does not move." in line
          and f"commit={TARGET} exit=0]" in line, f"a retained pin is ready, and says the release stays: {line}")
    _got, said, _ran = upgrade(release={"withheld": True})
    line = one_verdict(said, "withheld")
    check("READY TO APPLY: p685, its release deploy withheld until its roles are on their own accounts" in line
          and "pinned to v685-pinned" in line and "commit=- exit=0]" in line,
          f"a deploy the finish withheld is said, with no commit claimed: {line}")
    _got, said, _ran = upgrade()
    line = one_verdict(said, "no release reported")
    check("READY TO APPLY: p685, pinned to v685-pinned, which the release phase did not resolve to a commit" in line,
          f"without a release status, the pinned ref, and no commit claimed: {line}")


# ------------------------------------------------------------------ not ready


def test_every_early_stop_is_not_ready_whatever_it_answered() -> None:
    # U3 hands on a configuration and cannot stop the upgrade; the other four can.
    for index, name in ((i, n) for i, n in enumerate(PHASES[:-1]) if n != "_refresh_upgrade_artifacts"):
        for answer in (7, 0, "foreign"):
            got, said, ran = upgrade(answers={name: answer})
            line = one_verdict(said, f"{name} answering {answer!r}")
            check(ran == list(PHASES[:index + 1]) and (got == answer if answer != "foreign"
                                                       else getattr(got, "label", "") == "a foreign answer"),
                  f"{name}: it still stops there, and its answer is still returned as it came: {got!r} {ran}")
            exit_status = answer if isinstance(answer, int) else "none"
            check(line == "switchyard: dry-run verdict: NOT READY: p685: the blocking reasons are printed above. "
                          f"Nothing changed. [verdict=not-ready project=p685 exit={exit_status}]",
                  f"{name} answering {answer!r}: not ready, even for a refusal of zero: {line}")
    got, said, ran = upgrade(answers={"hold": "refuse"})
    line = one_verdict(said, "role models")
    check(got == 1 and ran == [] and "NOT READY: p685" in line and "exit=1]" in line,
          f"role models it cannot hold stop it before any phase: {line}")


def test_a_nonzero_finish_is_not_ready_and_the_role_models_check_has_the_last_word() -> None:
    got, said, _ran = upgrade(answers={"_finish_upgrade": 16})
    line = one_verdict(said, "finish 16")
    check(got == 16 and "NOT READY: p685" in line and "exit=16]" in line, f"U6's answer is the verdict's: {line}")
    got, said, _ran = upgrade(release={"target_sha": TARGET, "current_sha": CURRENT, "unchanged": False},
                              after=lambda code: 1)
    line = one_verdict(said, "role models changed it")
    check(got == 1 and "NOT READY" in line, f"a finish of 0 the role models' check turns to 1 is not ready: {line}")


def test_a_raised_refusal_is_not_ready_and_still_raised() -> None:
    text = "switchyard: refusing to upgrade p685: its board is the wrong release.\nNothing was changed."
    refusal = SystemExit(text)
    errors = io.StringIO()
    with redirect_stderr(errors):
        got, said, _ran = upgrade(answers={"_stage_upgrade_tooling": refusal})
    line = one_verdict(said, "worded refusal")
    check(isinstance(got, SystemExit) and got.code == 1 and errors.getvalue() == text + "\n",
          f"its words are printed where Python would have, before the verdict, and it exits 1 as Python would: "
          f"{got!r} {errors.getvalue()!r}")
    check(isinstance(got, SystemExit) and got.__suppress_context__, "and nothing more is printed about it")
    errors = io.StringIO()
    with redirect_stderr(errors):
        applying, _said, _ran = upgrade(dry_run=False, answers={"_stage_upgrade_tooling": refusal})
    check(applying is refusal and errors.getvalue() == "", f"an applying refusal is raised as it was: {applying!r}")
    check(line == "switchyard: dry-run verdict: NOT READY: p685: it stopped: refusing to upgrade p685: its board is "
                  "the wrong release. Nothing changed. [verdict=not-ready project=p685 exit=1]",
          f"its words are in the verdict, on one line, its own 'Nothing was changed' not said twice: {line}")
    bare = SystemExit(3)
    got, said, _ran = upgrade(answers={"_recover_upgrade_state": bare})
    line = one_verdict(said, "bare exit")
    check(got is bare and "it stopped; the reasons are above" in line and "exit=3]" in line,
          f"a bare exit keeps its status: {line}")
    crash = RuntimeError("p685: the journal is unreadable")
    got, said, _ran = upgrade(answers={"_refresh_upgrade_artifacts": crash})
    line = one_verdict(said, "crash")
    check(got is crash and "it failed: RuntimeError: p685: the journal is unreadable" in line and "exit=raised]" in line,
          f"a crash is not ready, and is raised on: {line}")


def test_only_upstream_report_ends_on_its_verdict_too() -> None:
    got, said, ran = upgrade(only="upstream-report")
    line = one_verdict(said, "only, 0")
    check(got == 0 and ran == [] and "READY TO APPLY: p685, its upstream-report step only, and no release." in line
          and "commit=- exit=0]" in line, f"the narrow step, ready, and named: {line}")
    got, said, _ran = upgrade(only="upstream-report", answers={"only": 2})
    line = one_verdict(said, "only, 2")
    check(got == 2 and "NOT READY: p685" in line and "exit=2]" in line, f"the narrow step, refused: {line}")


def test_the_verdict_is_said_once_whatever_reports_to_it() -> None:
    said: list[str] = []
    verdict = upgrade_verdict.UpgradeDryRunVerdict("p685", dry_run=True, print_func=said.append)
    with verdict:
        check(verdict.finished(0) == 0 and verdict.stopped(3) == 3 and verdict.finished(1) == 1,
              "every answer is handed back unchanged")
    check(len(said) == 1 and "READY TO APPLY: p685" in said[0], f"only the first is said: {said}")
    try:
        with upgrade_verdict.UpgradeDryRunVerdict("p685", dry_run=True, print_func=said.append) as again:
            again.stopped(4)
            raise SystemExit("switchyard: after it")
    except SystemExit:
        pass
    check(len(said) == 2 and "exit=4]" in said[1], f"a raise after the verdict was said says nothing more: {said}")


#: A real process, as `scripts/switchyard` is: the command's answer is its exit, and a refusal Python renders as it
#: exits. Its combined output is read as the operator sees it (Audit of 4f3817a9).
PROCESS = r"""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from scripts import team_launcher as t
how = sys.argv[2]
def pinned(*a, **k):
    print("switchyard: U1 detail")
    if how == "worded":
        raise SystemExit("switchyard: refusing to upgrade p685: its board is the wrong release.\nNothing was changed.")
    if how == "bare":
        raise SystemExit(3)
    if how == "crash":
        raise RuntimeError("p685: the journal is unreadable")
    return 1
t._pin_upgrade_source = pinned
t.hold_role_selections_for_upgrade = lambda *a, **k: {"p685": "held"}
raise SystemExit(t.upgrade_project_command(SimpleNamespace(project="p685"), config_path=Path("/x/p685.json"),
                                           dry_run=True))
"""


def process(how: str, *, unbuffered: bool) -> subprocess.CompletedProcess[str]:
    """The process's combined output, unbuffered as a terminal shows it (Audit's reproduction), or block-buffered
    as a pipe takes it."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    return subprocess.run([sys.executable, *(["-u"] if unbuffered else []), "-c", PROCESS, str(ROOT), how],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60, env=env,
                          stdin=subprocess.DEVNULL)


def test_the_verdict_is_the_last_line_a_real_process_prints() -> None:
    for unbuffered in (True, False):
        for how, status in (("worded", 1), ("bare", 3), ("returned", 1)):
            done = process(how, unbuffered=unbuffered)
            lines = done.stdout.splitlines()
            found = [line for line in lines if line.startswith(upgrade_verdict.PREFIX)]
            check(done.returncode == status and len(found) == 1 and lines[-1] == found[0] and "NOT READY: p685" in found[0],
                  f"{how}, unbuffered={unbuffered}: exit {status}, and its one verdict is the last line of the combined "
                  f"output: {done.returncode} {lines}")
            if how == "worded":
                check(lines[:-1] == ["switchyard: U1 detail", "switchyard: refusing to upgrade p685: its board is the "
                                     "wrong release.", "Nothing was changed."],
                      f"unbuffered={unbuffered}: the refusal's own words come once, in order, as Python would have "
                      f"printed them: {lines}")
    done = subprocess.run([sys.executable, "-c", PROCESS, str(ROOT), "crash"], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=60, stdin=subprocess.DEVNULL)
    check(done.returncode == 1 and "RuntimeError: p685: the journal is unreadable" in done.stdout.splitlines()[-1]
          and sum(line.startswith(upgrade_verdict.PREFIX) for line in done.stdout.splitlines()) == 1,
          f"a crash is not a refusal: its traceback follows its one verdict: {done.stdout[-600:]}")


def test_an_upgrade_that_applies_prints_no_verdict() -> None:
    for kwargs in ({}, {"answers": {"_pin_upgrade_source": 7}}, {"answers": {"_finish_upgrade": 16}},
                   {"only": "upstream-report"}):
        got, said, _ran = upgrade(dry_run=False, release={"target_sha": TARGET, "current_sha": CURRENT,
                                                          "unchanged": False}, **kwargs)
        check(verdicts(said) == [] and isinstance(got, int), f"applying {kwargs}: its output is as before: {said}")
    refusal = SystemExit("switchyard: refusing")
    got, said, _ran = upgrade(dry_run=False, answers={"_stage_upgrade_tooling": refusal})
    check(got is refusal and verdicts(said) == [], f"an applying refusal raises as before, with no verdict: {said}")


def main() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    print(f"upgrade_dry_run_verdict_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
