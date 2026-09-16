#!/usr/bin/env python3
"""SYRD-162: asking whether a pane has registered, a moment after starting it.

During SYRD-146 the sessions were healthy every time. Testing journal 0032
started all five roles and then refused the launch because `ops` had no runtime
assignment yet; seconds later `/api/runtime-assignments` had all five. Journal
0037 reported all five missing straight after a workflow and runtime refresh;
seconds later, again, all five were there.

Registration is the pane's own asynchronous work -- the role's CLI starts,
`ticket-board-register-runtime` announces it, the board records the row -- so a
check that samples the instant the launcher returns is asking before the answer
exists. It then told an operator that a successful recovery had failed, and to
retry it by hand.

So the two places that ask now wait, within a bound, and say what they are
waiting for. What they must not do is make the answer arrive: nothing here
restarts a pane, clears a session, or relaxes what the board will accept as an
assignment. A session that has actually exited is the one case that does not
wait at all -- it will not register however long anyone stands there, and its
name is more useful immediately.

The clock is injected, so these cases decide the timing rather than sleeping
through it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import presentation_controller  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

PROJECT = "testing"
ROLE_NAMES = ("designer", "director", "audit", "main", "ops")


class Clock:
    """A clock these cases move themselves, so nothing waits in real time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


#: Built by the product's own loader from a written configuration, so the roles
#: here carry every default a real project's roles do.
_LOADED: dict[str, launcher.ProjectConfig] = {}


def config(**overrides) -> launcher.ProjectConfig:
    if "base" not in _LOADED:
        import tempfile

        holder = tempfile.mkdtemp(prefix="syrd162-config.")
        directory = Path(holder)
        layout = directory / "layout.json"
        layout.write_text(
            json.dumps({"Orientation": "Horizontal", "Widgets": []}), encoding="utf-8"
        )
        config_path = directory / f"{PROJECT}.json"
        config_path.write_text(
            json.dumps(
                {
                    "desktop_access": {"mode": "headless"},
                    "project": PROJECT,
                    "ticket_prefix": "TESTING",
                    "layout": str(layout),
                    "board_url": "http://127.0.0.1:25310",
                    "board_socket": "/run/testing-ticket-board/ticket-board.sock",
                    "run_as_user": "testing-agent",
                    "session_dir": str(directory / "sessions"),
                    "roles": [
                        {
                            "role": name,
                            "slot": index,
                            "target": f"{PROJECT}-{name}:0.0",
                            "cli": ["codex"],
                            "workdir": str(directory / "worktrees" / name),
                        }
                        for index, name in enumerate(ROLE_NAMES)
                    ],
                }
            ),
            encoding="utf-8",
        )
        _LOADED["base"] = launcher.load_project_config(PROJECT, config_path)
    base = _LOADED["base"]
    return replace(base, **overrides) if overrides else base


def readings(*answers: set[str] | str):
    """A board that answers differently each time it is asked."""
    remaining = list(answers)

    def read() -> tuple[set[str], str]:
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(answer, str):
            return set(), answer
        return answer, ""

    return read


def wait_for(read, *, alive=None, timeout=90.0, clock: Clock | None = None, said=None):
    clock = clock or Clock()
    return (
        launcher.await_runtime_registration(
            config(),
            read=read,
            alive=alive,
            timeout_seconds=timeout,
            poll_seconds=2.0,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            print_func=(said.append if said is not None else (lambda _line: None)),
        ),
        clock,
    )


# --------------------------------------------------------------------------


def test_one_role_that_registers_a_moment_later_is_waited_for() -> None:
    """Journal 0032: four roles there, `ops` a few seconds behind."""
    said: list[str] = []
    late = set(ROLE_NAMES) - {"ops"}
    result, clock = wait_for(readings(late, late, set(ROLE_NAMES)), said=said)

    assert result.registered, result
    assert result.missing == () and result.exited == ()
    assert clock.slept == [2.0, 2.0], clock.slept
    # Observable: it says what it is waiting for, once, not on every poll.
    assert len(said) == 1, said
    assert "ops" in said[0] and "90s" in said[0], said


def test_every_role_arriving_late_is_still_a_success() -> None:
    """Journal 0037: the whole set, immediately after a refresh."""
    said: list[str] = []
    result, clock = wait_for(readings(set(), set(), set(ROLE_NAMES)), said=said)

    assert result.registered, result
    assert clock.slept == [2.0, 2.0]
    assert all(name in said[0] for name in ROLE_NAMES), said


def test_a_bound_is_a_bound_and_it_names_exactly_who_is_missing() -> None:
    """On timeout the answer is the roles still missing, not a count."""
    never = set(ROLE_NAMES) - {"main", "ops"}
    result, clock = wait_for(readings(never), timeout=10.0)

    assert not result.registered
    assert sorted(result.missing) == ["main", "ops"], result.missing
    assert result.exited == ()
    # Bounded: five polls of two seconds, and then an answer.
    assert sum(clock.slept) <= 10.0, clock.slept
    assert result.waited_seconds <= 10.0


def test_a_session_that_has_exited_is_not_waited_for_at_all() -> None:
    """It will not register however long anyone waits, so say so now."""
    never = set(ROLE_NAMES) - {"ops"}
    result, clock = wait_for(
        readings(never), alive=lambda role: role.role != "ops", timeout=90.0
    )

    assert result.exited == ("ops",), result
    assert result.missing == (), result
    assert clock.slept == [], "a dead session must not be waited on"


def test_a_live_role_beside_a_dead_one_is_still_reported_as_missing() -> None:
    """Two different facts, and the operator needs both by name."""
    present = set(ROLE_NAMES) - {"main", "ops"}
    result, _clock = wait_for(
        readings(present), alive=lambda role: role.role != "ops", timeout=90.0
    )

    assert result.exited == ("ops",), result
    assert result.missing == ("main",), result


def test_a_board_that_cannot_be_read_is_said_rather_than_guessed() -> None:
    result, _clock = wait_for(readings("the board's runtime assignments could not be read: nope"), timeout=4.0)

    assert not result.registered
    assert "could not be read" in result.problem, result
    assert sorted(result.missing) == sorted(ROLE_NAMES), result.missing


def test_nothing_is_waited_for_when_there_is_nothing_to_wait_for() -> None:
    """Already registered: no announcement, no sleep, no delay to a retry.

    This is the repeated resume. The second run must not pause, and must not
    print that it is waiting for something that is already true.
    """
    said: list[str] = []
    result, clock = wait_for(readings(set(ROLE_NAMES)), said=said)

    assert result.registered
    assert clock.slept == [] and said == [], (clock.slept, said)


# ------------------------------------------------- through the two callers --


def test_resume_readiness_passes_once_the_registrations_arrive() -> None:
    """The readiness check resume-provision runs, with a late board."""
    said: list[str] = []
    late = set(ROLE_NAMES) - {"ops"}
    clock = Clock()
    project = config()
    registration = launcher.await_runtime_registration(
        project,
        read=readings(late, set(ROLE_NAMES)),
        alive=lambda _role: True,
        timeout_seconds=90.0,
        poll_seconds=2.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        print_func=said.append,
    )
    assert registration.registered, registration

    from scripts.ticket_board.project_provision import build_plan

    plan = build_plan(project=PROJECT, owner_user="testing-agent")
    registry = Path("/nonexistent/registry.json")
    problems = launcher.recovery_readiness_problems(
        plan,
        project,
        Path("/nonexistent/testing.json"),
        registry_path=registry,
        process_commands=[f"agy TICKET_BOARD_PANE_TARGET={role.target}" for role in project.roles],
        completion=launcher.PacketCompletion(),
        registration=registration,
        print_func=said.append,
    )
    # The registry line is the only complaint left: every role registered.
    assert problems == [f"{PROJECT} is not registered at {registry}"], problems


def test_resume_readiness_names_a_dead_session_rather_than_a_late_one() -> None:
    from scripts.ticket_board.project_provision import build_plan

    project = config()
    plan = build_plan(project=PROJECT, owner_user="testing-agent")
    problems = launcher.recovery_readiness_problems(
        plan,
        project,
        Path("/nonexistent/testing.json"),
        registry_path=Path("/nonexistent/registry.json"),
        process_commands=[],
        completion=launcher.PacketCompletion(),
        registration=launcher.RuntimeRegistrationWait(missing=("main",), exited=("ops",)),
        print_func=lambda _line: None,
    )
    assert any("ops has no running session" in line for line in problems), problems
    assert any("main did not register a runtime" in line for line in problems), problems


def assignment_payload(present: set[str]) -> dict:
    return {
        "project": PROJECT,
        "authority_mode": "process",
        "assignments": {
            name: {"actual_target": f"{PROJECT}-{name}:0.0", "runtime": "codex"}
            for name in sorted(present)
        },
    }


def opener_for(*answers: set[str]):
    remaining = list(answers)

    class Response:
        def __init__(self, payload: dict) -> None:
            self.payload = json.dumps(payload).encode("utf-8")

        def read(self, *_args) -> bytes:
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def opener(_url: str) -> Response:
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return Response(assignment_payload(answer))

    return opener


def test_the_launch_waits_for_an_assignment_that_is_a_moment_late() -> None:
    """Journal 0032 again, through the resolver the launch actually calls."""
    said: list[str] = []
    clock = Clock()
    project = replace(config(), role_state_isolation=True)
    late = set(ROLE_NAMES) - {"ops"}

    resolved = presentation_controller.runtime_assignment_config(
        project,
        opener=opener_for(late, set(ROLE_NAMES)),
        wait_seconds=90.0,
        poll_seconds=2.0,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        print_func=said.append,
    )

    assert {role.role for role in resolved.roles} == set(ROLE_NAMES)
    assert clock.slept == [2.0], clock.slept
    assert said and "ops" in said[0], said


def test_a_caller_that_did_not_start_anything_still_asks_once() -> None:
    """Every other caller keeps the reading it had: no wait, same refusal."""
    project = replace(config(), role_state_isolation=True)
    clock = Clock()
    try:
        presentation_controller.runtime_assignment_config(
            project,
            opener=opener_for(set(ROLE_NAMES) - {"ops"}),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    except SystemExit as exc:
        assert "no live runtime assignment for configured role(s): ops" in str(exc), exc
        assert clock.slept == [], clock.slept
        return
    raise AssertionError("a missing assignment must still be refused without a wait")


def test_waiting_never_relaxes_what_an_assignment_must_be() -> None:
    """Acceptance 3: the identity checks are not races and do not get a retry."""
    project = replace(config(), role_state_isolation=True)
    clock = Clock()

    def foreign(_url: str):
        payload = assignment_payload(set(ROLE_NAMES))
        payload["assignments"]["ops"]["actual_target"] = "somebody-else-ops:0.0"

        class Response:
            def read(self, *_args) -> bytes:
                return json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> bool:
                return False

        return Response()

    try:
        presentation_controller.runtime_assignment_config(
            project, opener=foreign, wait_seconds=90.0,
            sleep=clock.sleep, monotonic=clock.monotonic,
        )
    except SystemExit as exc:
        assert "refusing foreign runtime assignment for ops" in str(exc), exc
        assert clock.slept == [], "a foreign assignment is not something to wait out"
        return
    raise AssertionError("a foreign runtime assignment must be refused")


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"runtime_registration_wait_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
