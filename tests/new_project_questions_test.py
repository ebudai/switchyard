#!/usr/bin/env python3
"""SYRD-778: `switchyard new` asks each decision it needs with its other questions, instead of refusing for a flag.

On 2026-11-15 `switchyard new` asked test11's name, slug, agent user and path, then stopped: two boards (sapi, syrd)
were declared to take tenant reports, so it said "Name one with --upstream-report-board <sapi|syrd> ... then run this
again". Now that decision is a question in the same sequence, before anything is created, with the Switchyard
project's own board (syrd here) the default, as the User decided. Several signed-in desktops are asked about the
same way (`desktop_policy_boundary_test`), with no default. And a Claude role's model question defaults to Opus 5.5.

`switchyard new`'s real choice phase (`_resolve_new_project_choices`) runs on the phase guard's fakes
(`new_project_phases_boundary_test`) against `upstream_report_default_test`'s sandboxed host: real registry records,
real boards answering on 127.0.0.1, real report tokens in a sandboxed home. Scripted answers stand in for the
operator; one case runs the phase in a child on a pty, so the real `input` reads the answer from a terminal, and
again with no terminal, where nobody can answer.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import new_project_phases_boundary_test as p0  # noqa: E402 - the phase guard's fakes for P0's other steps
import upstream_report_default_test as d  # noqa: E402 - SYRD-674's sandboxed host and real boards
from scripts import new_project_questions, team_launcher  # noqa: E402
from scripts import upstream_report_sinks as sinks  # noqa: E402

CHECKS = 0
QUESTION = "Switchyard reports board"


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def name(h: object, slug: str, value: str) -> None:
    """The project name root's registry records for `slug`."""
    path = h.registry / f"{slug}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["name"] = value
    path.write_text(json.dumps(record), encoding="utf-8")


def boards(h: object, *declared: str, switchyard: str = "syrd", undeclared: tuple[str, ...] = ("test3",)) -> None:
    """The host: `declared` boards declared to take reports, `switchyard` the one registered as Switchyard."""
    for slug in (*declared, *undeclared):
        h.add_board(slug, declared=slug in declared)
    for slug in (*declared, *undeclared):
        name(h, slug, "Switchyard" if slug == switchyard else {"sapi": "Stellaris API"}.get(slug, slug.upper()))


def new(h: object, *answers: str, can_ask: bool = True, **overrides: object) -> tuple[object, list[str], list[str],
                                                                                         list[str]]:
    """`switchyard new test11`'s real choice phase: what it returned or raised, what it asked, said, and its steps."""
    queue, asked, said = list(answers), [], []

    def operator(prompt: str) -> str:
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"FAILED CHECK: asked more than was answered: {prompt!r}")
        return queue.pop(0)

    world = p0.World(h.tmp)
    values = p0.inputs(h.tmp, role_clis=(("director", "claude"), ("main", "codex")), registry_dir=h.registry,
                       slug="test11", input_func=operator, print_func=said.append)
    values.update(overrides)
    if can_ask:
        got = p0.run(world, values)
    else:
        with patch.object(new_project_questions, "can_ask", lambda _input_func: False):
            got = p0.run(world, values)
    check(not queue, f"every scripted answer was asked for: {queue} left, asked {asked}")
    return got, asked, said, world.names()


def choice(got: object) -> tuple[str, str]:
    report = getattr(got, "upstream_report", None)
    return (getattr(report, "state", ""), getattr(report, "board", "")) if report is not None else ("raised", str(got))


# -- the reports board ------------------------------------------------------------------------------------------------
def test_two_declared_boards_are_asked_with_syrd_the_default() -> None:
    for answer, expected in (("", (sinks.CONNECT, "syrd")), ("sapi", (sinks.CONNECT, "sapi")),
                             ("1", (sinks.CONNECT, "sapi")), ("none", (sinks.OPTED_OUT, ""))):
        with d.host() as h:
            boards(h, "sapi", "syrd")
            got, asked, said, steps = new(h, answer)
            check(choice(got) == expected, f"answer {answer!r}: {expected}: {choice(got)} {said[-6:]}")
            check(asked == [f"{QUESTION} [syrd]: "], f"answer {answer!r}: asked once, syrd in brackets: {asked}")
            offered = [line for line in said if line.startswith("  ")]
            check(offered == [f"  1) sapi: Stellaris API at {h.boards['sapi'].url}",
                              f"  2) syrd: Switchyard at {h.boards['syrd'].url} [default]",
                              "  3) none: test11 cannot file Switchyard reports"],
                  f"answer {answer!r}: the declared boards by slug and name, and none; nothing undeclared: {offered}")
            question = said[:said.index("  3) none: test11 cannot file Switchyard reports") + 1]
            check(any("2 boards are declared to take tenant reports (sapi, syrd)" in line for line in question)
                  and not any("--upstream-report-board" in line or "run this again" in line for line in question),
                  f"answer {answer!r}: the question says why, and names no flag: {question}")
            summary = next((line for line in said if line.startswith("switchyard: Switchyard reports:")
                            or line.startswith("switchyard: WARNING: test11 will not be able to file")), "")
            check((expected[1] and f"files them to {expected[1]} at" in summary)
                  or (not expected[1] and "the operator chose none" in summary),
                  f"answer {answer!r}: the summary before the confirmation says the answer: {summary}")
            check(steps.index("confirm") > steps.index("registration") and "confirm" in steps,
                  f"answer {answer!r}: asked before the one confirmation: {steps}")


def test_yes_takes_syrd() -> None:
    with d.host() as h:
        boards(h, "sapi", "syrd")
        got, asked, said, _steps = new(h, yes=True)
        check(choice(got) == (sinks.CONNECT, "syrd") and asked == [],
              f"--yes takes the default, syrd, and asks nothing: {choice(got)} {asked}")
        check(any("files them to syrd" in line and "the Switchyard board, the default" in line for line in said),
              f"and says it was the default: {said[-4:]}")


def test_one_declared_board_is_the_default_unasked() -> None:
    for declared in ("sapi", "syrd"):
        with d.host() as h:
            boards(h, declared, undeclared=("test3", "sapi" if declared == "syrd" else "syrd"))
            got, asked, _said, _steps = new(h)
            check(choice(got) == (sinks.CONNECT, declared) and asked == [],
                  f"one declared board ({declared}) is the default, as before, and nothing is asked: {choice(got)}")


def test_a_flag_answers_the_question() -> None:
    for flag, expected in (({"upstream_report_board": "sapi"}, (sinks.CONNECT, "sapi")),
                           ({"no_upstream_report": True}, (sinks.OPTED_OUT, ""))):
        with d.host() as h:
            boards(h, "sapi", "syrd")
            got, asked, _said, _steps = new(h, **flag)
            check(choice(got) == expected and asked == [], f"{flag}: answered by the flag, not asked: {choice(got)}")


def test_nobody_to_ask_keeps_the_refusal() -> None:
    for yes in (False, True):
        with d.host() as h:
            boards(h, "sapi", "syrd", switchyard="")
            before = h.tree()
            got, asked, _said, steps = new(h, can_ask=False, yes=yes)
            text = str(got)
            check(isinstance(got, SystemExit) and "Name one with --upstream-report-board <sapi|syrd>" in text
                  and "--no-upstream-report" in text and text.endswith("Nothing was created.")
                  and asked == [] and "confirm" not in steps and h.tree() == before,
                  f"yes={yes}, no Switchyard board and nobody to answer: today's refusal, naming its flag: {text}")
    with d.host() as h:
        boards(h, "sapi", "syrd")
        got, asked, _said, _steps = new(h, can_ask=False)
        check(isinstance(got, SystemExit) and "--upstream-report-board" in str(got) and asked == [],
              f"nobody to answer and no --yes: refused, the default not taken for anyone: {got}")


def test_no_switchyard_board_means_no_default() -> None:
    with d.host() as h:
        boards(h, "sapi", "otto", switchyard="")
        got, asked, said, _steps = new(h, "", "otto")
        check(choice(got) == (sinks.CONNECT, "otto") and asked == [f"{QUESTION}: ", f"{QUESTION}: "],
              f"no Switchyard board declared: no default, so Enter answers nothing and it is asked again: "
              f"{choice(got)} {asked}")
        check(any("this question has no default" in line for line in said), f"saying so: {said}")
    with d.host() as h:
        boards(h, "sapi", "otto", switchyard="")
        got, asked, _said, _steps = new(h, yes=True)
        check(isinstance(got, SystemExit) and "--upstream-report-board <otto|sapi>" in str(got) and asked == [],
              f"--yes with no default to take: refused, naming the flag: {got}")


def test_none_declared_offers_only_none() -> None:
    with d.host() as h:
        boards(h, undeclared=("sapi", "syrd"))
        got, asked, said, _steps = new(h, "none")
        offered = [line for line in said if line.startswith("  ")]
        check(choice(got) == (sinks.OPTED_OUT, "") and asked == [f"{QUESTION}: "]
              and offered == ["  1) none: test11 cannot file Switchyard reports"],
              f"no board declared: an undeclared board is never offered (SYRD-686); none is: {offered} {asked}")
        check(any("--only accept-reports" in line for line in said),
              f"and the declaration the host lacks is said: {said}")


def test_too_many_wrong_answers_stop() -> None:
    with d.host() as h:
        boards(h, "sapi", "syrd")
        got, asked, _said, steps = new(h, *(["nonsense"] * team_launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS))
        check(isinstance(got, SystemExit) and QUESTION in str(got) and "confirm" not in steps
              and len(asked) == team_launcher.SWITCHYARD_PROMPT_MAX_ATTEMPTS,
              f"an answer that is none of them is asked again, then stops before anything: {got} {asked}")


# -- which desktop ----------------------------------------------------------------------------------------------------
def desktop(*answers: str, owners: tuple[str, ...] = ("alice778", "bob778"), can_ask: bool = True,
            **overrides: object) -> tuple[object, list[str], list[str]]:
    """`switchyard new`'s real desktop decision on a host where `owners` are signed in and none is chosen for it."""
    from scripts import desktop_policy
    from scripts.desktop_access import DesktopAccessError

    queue, asked, said = list(answers), [], []

    def operator(prompt: str) -> str:
        asked.append(prompt)
        if not queue:
            raise AssertionError(f"FAILED CHECK: asked more than was answered: {prompt!r}")
        return queue.pop(0)

    def resolve(preferred: str) -> str:
        if len(owners) == 1 and preferred in ("", owners[0]):
            return owners[0]
        raise DesktopAccessError(f"{len(owners) or 'no'} desktops; name one with --desktop-gui-user USER")

    options = dict(desktop_policy=None, headless=False, gui_user="", project="test11", tenant="test11-agent",
                   yes=False, input_func=operator, print_func=said.append, owner_resolver=resolve,
                   owners_lister=lambda: list(owners))
    options.update(overrides)
    with patch.object(team_launcher, "read_host_desktop_approval", lambda path=None: {}), \
            patch.object(new_project_questions, "can_ask", lambda _f: can_ask):
        try:
            got = desktop_policy._resolve_desktop_policy(**options)
        except SystemExit as exc:
            got = exc
    check(not queue, f"every scripted answer was asked for: {queue} left, asked {asked}")
    return got, asked, said


def test_several_desktops_are_asked_with_no_default() -> None:
    got, asked, said = desktop("", "bob778", "headless")
    check(got == ({"mode": "headless"}, "chosen_headless")
          and asked == ["Desktop user: ", "Desktop user: ", "Desktop access [desktop]: "],
          f"which desktop is asked, with no default -- Enter answers nothing -- then that desktop's access: "
          f"{got} {asked}")
    check(any("2 accounts are signed into this host's desktop (alice778, bob778)" in line for line in said)
          and any("bob778's session" in line for line in said) and not any("--desktop-gui-user" in line
                                                                          for line in said),
          f"saying why, offering the chosen one's session, and naming no flag: {said}")
    for label, answers, overrides in (("--yes", (), {"yes": True}),
                                      ("a named desktop not signed in", (), {"gui_user": "carol778"}),
                                      ("nobody to answer", (), {"can_ask": False})):
        got, asked, _said = desktop(*answers, **overrides)
        check(isinstance(got, SystemExit) and "--desktop-gui-user" in str(got) and asked == [],
              f"{label}: refused as before, naming the flag; nothing asked: {got} {asked}")


def test_yes_on_a_host_with_no_desktop_takes_headless() -> None:
    got, asked, said = desktop(owners=(), yes=True)
    check(got == ({"mode": "headless"}, "chosen_headless") and asked == []
          and any("--yes takes the default" in line for line in said),
          f"--yes takes the headless question's default, which grants nothing: {got} {asked} {said}")
    got, asked, _said = desktop("n", owners=())
    check(isinstance(got, SystemExit) and "headless was declined" in str(got)
          and asked == ["Install this project headless (no screenshots or clipboard) [Y/n]: "],
          f"interactively it is still asked: {got} {asked}")


# -- the real prompt on a terminal ------------------------------------------------------------------------------------
CHILD = r'''
import builtins, json, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
import new_project_questions_test as q
import upstream_report_default_test as d
from scripts import team_launcher
with d.host() as h:
    q.boards(h, "sapi", "syrd")
    got, _asked, said, _steps = q.new(h, input_func=builtins.input)
    result = {"choice": q.choice(got), "said": said[-3:]}
    if not isinstance(got, SystemExit):
        plan = team_launcher._prompt_role_runtime_plan("director", default_cli="claude", input_func=builtins.input,
                                                       print_func=lambda _line: None)
        result["model"] = plan.model
    print("RESULT " + json.dumps(result), flush=True)
'''


def child(*, on_a_terminal: bool, lines: str) -> tuple[dict, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX"))}
    argv = [sys.executable, "-c", CHILD, str(ROOT)]
    if not on_a_terminal:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120, env=env,
                              cwd=str(ROOT))
        out = done.stdout + done.stderr
    else:
        master, slave = pty.openpty()
        proc = subprocess.Popen(argv, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                cwd=str(ROOT))
        os.close(slave)
        os.write(master, lines.encode())
        chunks, deadline = [], time.monotonic() + 120
        while proc.poll() is None and time.monotonic() < deadline:
            ready, _w, _x = select.select([proc.stdout, master], [], [], 0.5)
            if proc.stdout in ready:
                chunks.append(os.read(proc.stdout.fileno(), 65536).decode("utf-8", "replace"))
            if master in ready:
                try:
                    os.read(master, 65536)  # the terminal's echo, drained
                except OSError:
                    pass
        if proc.poll() is None:
            proc.kill()
        chunks.append(proc.stdout.read().decode("utf-8", "replace"))
        os.close(master)
        out = "".join(chunks)
    result = next((json.loads(line.split("RESULT ", 1)[1]) for line in out.splitlines() if "RESULT " in line), {})
    return result, out


def test_the_real_prompt_on_a_terminal_and_without_one() -> None:
    got, out = child(on_a_terminal=True, lines="\n\n\n\n")
    check(got.get("choice") == [sinks.CONNECT, "syrd"] and f"{QUESTION} [syrd]: " in out,
          f"on a terminal, the real input asks, and Enter takes syrd: {got} {out[-1500:]}")
    check(got.get("model") == "claude-opus-5-5" and "director model [Opus 5.5]: " in out,
          f"and a Claude role's model question defaults to Opus 5.5, which Enter takes: {got} {out[-800:]}")
    got, out = child(on_a_terminal=False, lines="")
    check(got.get("choice", ["", ""])[0] == "raised" and "--upstream-report-board <sapi|syrd>" in got["choice"][1]
          and QUESTION not in out,
          f"with no terminal and no --yes, nobody is asked: today's refusal, naming its flag: {got} {out[-1500:]}")


# -- a Claude role's model --------------------------------------------------------------------------------------------
def test_a_claude_role_defaults_to_opus_5_5() -> None:
    from scripts.ticket_board import runtime_catalog

    offered = [c.value for c in runtime_catalog.RECORDED_MODELS["claude"]]
    check(offered[:2] == ["claude-opus-5-5", "claude-opus-5"],
          f"Opus 5.5 first, the default; Opus 5 still offered: {offered}")
    for role in ("director", "audit", "designer"):
        asked: list[str] = []
        shown: list[str] = []
        plan = team_launcher._prompt_role_runtime_plan(
            role, default_cli="claude", input_func=lambda prompt: asked.append(prompt) or "", print_func=shown.append)
        check(plan.cli == "claude" and plan.model == "claude-opus-5-5" and f"{role} model [Opus 5.5]: " in asked,
              f"{role}: Enter at the model question records claude-opus-5-5: {plan} {asked}")
        check(any("Opus 5 -- " in line for line in shown), f"{role}: and Opus 5 is listed: {shown}")
    plan = team_launcher._prompt_role_runtime_plan("director", default_cli="claude",
                                                   input_func=lambda _prompt, said=iter(["", "2", ""]): next(said),
                                                   print_func=lambda _l: None)
    check(plan.model == "claude-opus-5", f"Opus 5 is still chosen by number: {plan}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"new_project_questions_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
