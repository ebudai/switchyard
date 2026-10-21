#!/usr/bin/env python3
"""`switchyard new` asks its one confirmation last, after the whole review (SYRD-599 UAT).

Live UAT: with Router offered and off by default, pressing Enter at the Router
question went straight into provisioning, into Claude's first-run sign-in. The
"Proceed? [y/N]" was asked before any role was chosen, so the answer to the
last role question -- Router, now the final one -- was what started creating
the tenant, and the review was printed on the way past.

Each case drives the real interactive `switchyard_new_command` (`yes=False`,
the guided role plan) with Enter -- the default -- for every question it asks,
records every line it prints and every question it asks in one sequence, and
answers only the confirmation. P1, the first phase that can install or change
anything, is fenced: reaching it is recorded and stops the run.

Nothing real is touched: a fresh temporary home base, registry and config
directory, an owner name no host has, and a fake runner whose calls are
checked.
"""

from __future__ import annotations

import pwd
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from team_launcher_test_helpers import FakeRunner  # noqa: E402

from scripts import team_launcher as launcher  # noqa: E402

OWNER = "zz599-agent"
PROCEED = "Proceed? [y/N]: "
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class P1Reached(SystemExit):
    pass


def run(*, proceed: str = "n", yes: bool = False, answers: dict[str, str] | None = None,
        implementers: str = "") -> dict:
    """One interactive `switchyard new`; returns the sequence, the outcome, the runner and what exists."""
    log: list[tuple[str, str]] = []
    answers = dict(answers or {})

    def ask(prompt: str) -> str:
        log.append(("ask", prompt))
        if prompt == PROCEED:
            return proceed
        if prompt.startswith("Implementer roles") and implementers:
            return implementers
        return next((value for key, value in answers.items() if prompt.startswith(key)), "")

    def preflight(**_kwargs: object) -> None:
        log.append(("phase", "P1"))
        raise P1Reached("P1 reached")

    saved = launcher._check_new_project_preflight
    with tempfile.TemporaryDirectory(prefix="syrd599-confirm.") as raw:
        tmp = Path(raw)
        runner = FakeRunner()
        launcher._check_new_project_preflight = preflight
        try:
            outcome: object = launcher.switchyard_new_command(
                desktop_policy=Path("headless"), slug="zz599", agent_name=OWNER, project_name="ZZ Five Nine Nine",
                source_repo=tmp / "src", yes=yes, home_base=tmp / "home", euid_getter=lambda: 0, runner=runner,
                input_func=ask, print_func=lambda line: log.append(("say", line)),
                port_in_use=lambda _port: False, socket_exists=lambda _path: False,
                registry_dir=tmp / "registry", config_dir=tmp / "config",
            )
        except SystemExit as exc:
            outcome = str(exc)
        finally:
            launcher._check_new_project_preflight = saved
        created = sorted(str(p.relative_to(tmp)) for p in tmp.rglob("*"))
    return {"log": log, "outcome": outcome, "calls": runner.calls, "created": created}


def asked(result: dict) -> list[str]:
    return [text for kind, text in result["log"] if kind == "ask"]


def said_before(result: dict, prompt: str) -> list[str]:
    index = result["log"].index(("ask", prompt))
    return [text for kind, text in result["log"][:index] if kind == "say"]


def test_enter_on_router_off_is_reviewed_and_then_confirmed() -> None:
    result = run(proceed="n")
    questions = asked(result)
    check(any(q.startswith("Router (optional)") for q in questions), f"Router is offered: {questions}")
    router_at = next(i for i, q in enumerate(questions) if q.startswith("Router (optional)"))
    # SYRD-624: the documentation gate's menu follows Router; the confirmation is still the last question.
    check(questions[router_at + 1:] == ["Documentation gate (optional) [off]: ", PROCEED],
          f"Router, then the documentation gate, and the only question after them is the confirmation: {questions[router_at:]}")
    review = said_before(result, PROCEED)
    check("switchyard: roles to create:" in review, f"the roles are reviewed before the question: {review[-6:]}")
    roles_at = review.index("switchyard: roles to create:")
    check([line.split(":")[0].strip() for line in review[roles_at + 1:roles_at + 6]]
          == ["designer", "director", "audit", "main", "ops"],
          f"every role is in the review before the question: {review[roles_at:]}")
    check(any(line.startswith("switchyard: Router: off") for line in review)
          and "switchyard: owner user: zz599-agent" in review and "switchyard: slug: zz599" in review,
          f"and Router's choice and the project itself: {review[-8:]}")
    router_review = [i for i, line in enumerate(review) if line.startswith("switchyard: Router: off")][0]
    check(router_review > roles_at, "Router's line belongs to the review, after the roles")
    gate_review = [i for i, line in enumerate(review) if line.startswith("switchyard: Documentation gate: off")]
    check(gate_review and gate_review[0] > router_review, f"and the documentation gate's, after Router's: {review[-8:]}")
    identity = [i for i, line in enumerate(review) if line.startswith("switchyard: project path: ")]
    check(len(identity) == 2 and identity[0] < roles_at < router_review < identity[1] == len(review) - 1,
          f"the project is said when resolved, and again as the last line of the review, right before the question: {identity}")


def test_no_at_the_confirmation_creates_nothing() -> None:
    result = run(proceed="n")
    check(result["outcome"] == "switchyard: cancelled", f"it stops: {result['outcome']}")
    check(("phase", "P1") not in result["log"], "P1 is never reached")
    check(result["calls"] == [], f"nothing on the host was asked to do anything: {result['calls']}")
    check(result["created"] == [], f"and nothing was created: {result['created']}")


def test_yes_at_the_confirmation_goes_on_to_p1() -> None:
    result = run(proceed="y")
    check(result["outcome"] == "P1 reached" and result["log"][-1] == ("phase", "P1"), f"on to P1: {result['outcome']}")
    check(result["log"].index(("ask", PROCEED)) < result["log"].index(("phase", "P1")),
          "and only after the confirmation was answered")
    check(result["calls"] == [], f"with nothing asked of the host before it: {result['calls']}")


def test_yes_flag_reviews_without_asking() -> None:
    result = run(yes=True)
    check(PROCEED not in asked(result) and result["outcome"] == "P1 reached",
          f"--yes is the confirmation: not asked, and it goes on: {asked(result)[-2:]}")
    said = [text for kind, text in result["log"] if kind == "say"]
    check("switchyard: roles to create:" in said and any(line.startswith("switchyard: Router: off") for line in said),
          "the review is still printed")


def test_router_on_is_reviewed_and_then_confirmed() -> None:
    result = run(proceed="n", answers={"Router (optional)": "2"})
    questions = asked(result)
    check(questions[-1] == PROCEED and any(q.startswith("router runtime") for q in questions),
          f"Router's own runtime, model and effort come before the confirmation, which is last: {questions[-5:]}")
    review = said_before(result, PROCEED)
    check(any(line.strip().startswith("router: ") for line in review)
          and any(line.startswith("switchyard: Router: on") for line in review),
          f"the review names Router's selection: {review[-10:]}")
    check(result["outcome"] == "switchyard: cancelled" and result["calls"] == [] and result["created"] == [],
          "and no creates nothing")


def test_one_implementer_is_confirmed_last_too() -> None:
    result = run(proceed="n", implementers="1")
    questions = asked(result)
    check(not any(q.startswith("Router (optional)") for q in questions) and questions[-1] == PROCEED,
          f"with no Router question, the confirmation is still the last one: {questions[-3:]}")
    check(any("Router is not offered" in line for line in said_before(result, PROCEED)),
          "and the reason Router is not offered is part of what was shown")


def main() -> int:
    try:
        pwd.getpwnam(OWNER)
    except KeyError:
        pass
    else:
        raise SystemExit(f"new_project_final_confirmation_test: {OWNER} exists on this host; the cases need an owner nobody has")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"new_project_final_confirmation_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
