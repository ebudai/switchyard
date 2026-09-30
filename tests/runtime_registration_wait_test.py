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

SYRD-336: the fixture used to BE the live `testing` tenant -- its board socket
under /run/testing-ticket-board, its board port, its owner account -- and the
readiness cases read that tenant's tmux server (`sudo -u testing-agent tmux
list-panes`) and its board socket for real. Only this account's lack of access
stopped them; the tenant, root or the board service running the suite would
have reached the live board, and the "passes" case could only pass when they
did. Now the fixture is a project, an owner account and a board nothing on any
host has: the board is a real HTTP server on a Unix socket inside this suite's
own directory, the owner's tmux server is a runner that answers exactly its one
question, and every case runs under a guard that refuses any other socket or
process -- whatever this host would have allowed.
"""

from __future__ import annotations

import atexit
import functools
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import presentation_controller  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402

#: A project and an owner account no host has: nothing this suite names can be
#: a live tenant's (SYRD-336).
PROJECT = "syrd336"
OWNER = "syrd336-owner"
ROLE_NAMES = ("designer", "director", "audit", "main", "ops")
#: Everything this suite owns on disk -- its configuration, its owner's home and
#: its board's socket -- lives under here, and nothing else may be reached.
SANDBOX = Path(tempfile.mkdtemp(prefix="syrd336."))
BOARD_SOCKET = SANDBOX / "board" / "ticket-board.sock"
# Gone with the process that made it, including for the suites that import this fixture.
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)


class Refused(AssertionError):
    """Something tried to reach past this suite's own sandbox."""


class Isolation:
    """Refuse every connection outside SANDBOX and every process, and record the attempts.

    Not a permission check: a socket the host would happily have let this
    account open is refused all the same, so what the suite proves does not
    depend on who runs it.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def _refuse(self, what: str):
        self.attempts.append(what)
        raise Refused(f"refused by the SYRD-336 isolation guard: {what}")

    def __enter__(self) -> "Isolation":
        real_connect = self._real_connect = socket.socket.connect
        guard = self

        def connect(sock, address):
            if sock.family == socket.AF_UNIX:
                path = os.path.realpath(address if isinstance(address, str) else address.decode())
                if not path.startswith(str(SANDBOX.resolve()) + os.sep):
                    guard._refuse(f"connect {address}")
            elif sock.family in (socket.AF_INET, socket.AF_INET6):
                guard._refuse(f"connect {address}")
            return real_connect(sock, address)

        self._real_popen = subprocess.Popen.__init__

        def popen(process, args, *rest, **kwargs):
            guard._refuse(f"spawn {args}")

        socket.socket.connect = connect
        subprocess.Popen.__init__ = popen
        return self

    def __exit__(self, *_exc) -> None:
        socket.socket.connect = self._real_connect
        subprocess.Popen.__init__ = self._real_popen


def isolated(case):
    """Run a case under the guard, and fail it for anything it tried to reach."""
    @functools.wraps(case)
    def run():
        with Isolation() as guard:
            case()
        assert guard.attempts == [], f"{case.__name__} reached past its sandbox: {guard.attempts}"
    return run


class Board:
    """A real board, in miniature: HTTP on a Unix socket inside SANDBOX."""

    def __init__(self, assignments: dict | None = None) -> None:
        self.asked: list[str] = []
        payload = {"project": PROJECT, "authority_mode": "process", "assignments": assignments or {}}
        board = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                board.asked.append(self.path)
                body, status = (json.dumps(payload).encode(), 200) if self.path == "/api/runtime-assignments" \
                    else (b"not found", 404)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def address_string(self):  # a Unix peer has no host to name
                return "board-socket"

            def log_message(self, *_args):
                pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        BOARD_SOCKET.parent.mkdir(parents=True, exist_ok=True)
        if BOARD_SOCKET.exists():
            BOARD_SOCKET.unlink()
        self.server = Server(str(BOARD_SOCKET), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()
        BOARD_SOCKET.unlink(missing_ok=True)


class OwnerTmux:
    """The owner's tmux server, answering the one question readiness asks of it."""

    PROBE = ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"]

    def __init__(self, targets: list[str]) -> None:
        self.targets = targets
        self.asked: list[list[str]] = []

    def __call__(self, args, **_kwargs):
        argv = [str(part) for part in args]
        self.asked.append(argv)
        if argv[-len(self.PROBE):] != self.PROBE:
            raise Refused(f"readiness asked the owner's side something else: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout="".join(f"{target}\n" for target in self.targets), stderr="")


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
        directory = SANDBOX / "config"
        directory.mkdir(parents=True, exist_ok=True)
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
                    "ticket_prefix": "SYRDT",
                    "layout": str(layout),
                    # The discard port: nothing answers, and the guard refuses it anyway.
                    "board_url": "http://127.0.0.1:9",
                    "board_socket": str(BOARD_SOCKET),
                    "run_as_user": OWNER,
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


@isolated
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


@isolated
def test_every_role_arriving_late_is_still_a_success() -> None:
    """Journal 0037: the whole set, immediately after a refresh."""
    said: list[str] = []
    result, clock = wait_for(readings(set(), set(), set(ROLE_NAMES)), said=said)

    assert result.registered, result
    assert clock.slept == [2.0, 2.0]
    assert all(name in said[0] for name in ROLE_NAMES), said


@isolated
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


@isolated
def test_a_session_that_has_exited_is_not_waited_for_at_all() -> None:
    """It will not register however long anyone waits, so say so now."""
    never = set(ROLE_NAMES) - {"ops"}
    result, clock = wait_for(
        readings(never), alive=lambda role: role.role != "ops", timeout=90.0
    )

    assert result.exited == ("ops",), result
    assert result.missing == (), result
    assert clock.slept == [], "a dead session must not be waited on"


@isolated
def test_a_live_role_beside_a_dead_one_is_still_reported_as_missing() -> None:
    """Two different facts, and the operator needs both by name."""
    present = set(ROLE_NAMES) - {"main", "ops"}
    result, _clock = wait_for(
        readings(present), alive=lambda role: role.role != "ops", timeout=90.0
    )

    assert result.exited == ("ops",), result
    assert result.missing == ("main",), result


@isolated
def test_a_board_that_cannot_be_read_is_said_rather_than_guessed() -> None:
    result, _clock = wait_for(readings("the board's runtime assignments could not be read: nope"), timeout=4.0)

    assert not result.registered
    assert "could not be read" in result.problem, result
    assert sorted(result.missing) == sorted(ROLE_NAMES), result.missing


@isolated
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


@isolated
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

    registry = Path("/nonexistent/registry.json")
    # The owner's tmux server holds every pane, and the board has registered
    # none of them yet: the moment right after a launch, which liveness counts
    # as live on the tmux evidence and the registration wait above resolves.
    tmux = OwnerTmux([role.target for role in project.roles])
    with Board() as board:
        problems = launcher.recovery_readiness_problems(
            _plan(),
            project,
            Path("/nonexistent/syrd336.json"),
            registry_path=registry,
            runner=tmux,
            process_commands=[f"agy TICKET_BOARD_PANE_TARGET={role.target}" for role in project.roles],
            completion=launcher.PacketCompletion(),
            registration=registration,
            print_func=said.append,
        )
    # The registry line is the only complaint left: every role registered.
    assert problems == [f"{PROJECT} is not registered at {registry}"], problems
    # And both halves of liveness were really read -- from the sandbox, only.
    assert board.asked == ["/api/runtime-assignments"], board.asked
    assert len(tmux.asked) == 1 and tmux.asked[0][:3] == ["sudo", "-u", OWNER], tmux.asked


@isolated
def test_resume_readiness_names_a_dead_session_rather_than_a_late_one() -> None:
    project = config()
    with Board():
        problems = launcher.recovery_readiness_problems(
            _plan(),
            project,
            Path("/nonexistent/syrd336.json"),
            registry_path=Path("/nonexistent/registry.json"),
            runner=OwnerTmux([role.target for role in project.roles if role.role != "ops"]),
            process_commands=[],
            completion=launcher.PacketCompletion(),
            registration=launcher.RuntimeRegistrationWait(missing=("main",), exited=("ops",)),
            print_func=lambda _line: None,
        )
    assert any("ops has no running session" in line for line in problems), problems
    assert any("main did not register a runtime" in line for line in problems), problems


def _plan():
    """A provisioning plan for the sandbox project, homed inside SANDBOX."""
    from scripts.ticket_board.project_provision import build_plan

    return build_plan(project=PROJECT, owner_user=OWNER, owner_home=SANDBOX / "home")


@isolated
def test_readiness_believes_the_board_over_a_pane_that_merely_exists() -> None:
    """A registration naming another pane is not live, read through the real board reader."""
    project = config()
    foreign = {"ops": {"actual_target": "somebody-else-ops:0.0", "runtime": "codex"}}
    with Board(foreign) as board:
        problems = launcher.recovery_readiness_problems(
            _plan(), project, Path("/nonexistent/syrd336.json"),
            registry_path=Path("/nonexistent/registry.json"),
            runner=OwnerTmux([role.target for role in project.roles]),
            process_commands=[], completion=launcher.PacketCompletion(),
            registration=launcher.RuntimeRegistrationWait(),
            print_func=lambda _line: None,
        )
    assert board.asked == ["/api/runtime-assignments"], board.asked
    assert any("ops has no running pane" in line and "somebody-else-ops:0.0" in line for line in problems), problems


def test_the_guard_refuses_what_the_host_would_have_allowed() -> None:
    """Isolation is the guard's, not the host's: a socket this account CAN open is refused all the same."""
    outside = Path(tempfile.mkdtemp(prefix="syrd336-outside."))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(outside / "reachable.sock"))
        server.listen(1)
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.connect(str(outside / "reachable.sock"))  # reachable: nothing on this host forbids it
        probe.close()
        with Isolation() as guard:
            for attempt in (
                lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(str(outside / "reachable.sock")),
                lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect("/run/testing-ticket-board/ticket-board.sock"),
                lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
                lambda: subprocess.run(["true"], check=False),
            ):
                try:
                    attempt()
                except Refused:
                    continue
                except OSError as exc:
                    raise AssertionError(f"the guard let an attempt reach the host: {exc}") from exc
                raise AssertionError("the guard let something past the sandbox")
        assert len(guard.attempts) == 4, guard.attempts
        assert any("/run/testing-ticket-board" in item for item in guard.attempts), guard.attempts
    finally:
        server.close()
        (outside / "reachable.sock").unlink(missing_ok=True)
        outside.rmdir()


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


@isolated
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


@isolated
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


@isolated
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
