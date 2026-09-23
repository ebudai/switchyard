#!/usr/bin/env python3
"""SYRD-112: the process-identity boundary, and the ways past it that must not work.

polkit cannot tell one role from another inside a tenant: a rule sees
`subject.user`, and every role shares that account. So the helper has to prove
which *process* asked. These are the negative cases that boundary exists for --
a sibling role under the same uid, a reused pid, a replaced session, a board
that is not on process authority, and a caller trying to name its own board.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import privileged_actions as pa  # noqa: E402
from scripts.ticket_board import privileged_helper as ph  # noqa: E402

CHECKS = 0
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def _proc(tmp: Path, entries: dict[int, tuple[int, int, str, int]]) -> Path:
    """A /proc for the ancestry walk: pid -> (ppid, start_time, comm, uid)."""
    root = tmp / "proc"
    for pid, (ppid, start, comm, uid) in entries.items():
        directory = root / str(pid)
        directory.mkdir(parents=True, exist_ok=True)
        fields = ["0"] * 22
        fields[1] = str(ppid)
        fields[19] = str(start)
        (directory / "stat").write_text(f"{pid} ({comm}) S " + " ".join(fields[1:]), encoding="utf-8")
        (directory / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n",
                                          encoding="utf-8")
    return root


def _board(workflow: dict, listing: dict | None):
    asked: list[str] = []

    def board_get(url: str, path: str):
        asked.append(path)
        if path == "/api/workflow":
            return workflow
        if path.startswith("/api/runtime-assignments/"):
            return listing
        raise AssertionError(f"unexpected board read: {path}")

    return board_get, asked


#: A real workflow document: the controller is found by CAPABILITY, and only
#: among active roles. An earlier version of this file invented a
#: `control_authority` flag, which no board emits -- so it agreed with an
#: implementation that would have refused everything on a real board.
CONTROL = ["set_manually_controlled", "merge", "route"]
WORKFLOW = {"document": {"roles": [
    {"name": "director", "active": True, "capabilities": CONTROL},
    {"name": "main", "active": True, "capabilities": ["submit_to_audit"]},
]}}


# --------------------------------------------------------------------------
# Which pane asked
# --------------------------------------------------------------------------


def test_the_pane_is_resolved_by_walking_to_the_tmux_server() -> None:
    with_tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-ancestry.") as raw:
        tmp = Path(raw)
        # helper <- shell <- pane root <- tmux server
        proc = _proc(tmp, {
            500: (400, 111, "python3", 1002),
            400: (300, 222, "bash", 1002),
            300: (200, 333, "bash", 1002),
            200: (1, 444, "tmux: server", 0),
        })
        caller = ph.caller_identity(500, proc_root=proc)
    check(caller.pid == 300, f"the pane root is the child of tmux: {caller.pid}")
    check(caller.start_time == 333, f"with its start time: {caller.start_time}")
    check(caller.uid == 1002, f"and its owner: {caller.uid}")


def test_a_caller_with_no_pane_is_refused_rather_than_guessed() -> None:
    """Detached, from a service, from anything that is not a pane."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-nopane.") as raw:
        tmp = Path(raw)
        proc = _proc(tmp, {
            500: (400, 111, "python3", 1002),
            400: (1, 222, "systemd", 0),
        })
        try:
            ph.caller_identity(500, proc_root=proc)
            check(False, "a caller with no pane was accepted")
        except ph.Refused as exc:
            check("cannot establish which pane" in str(exc), str(exc))
            check("not detached from it" in str(exc), f"and says what is required: {exc}")


# --------------------------------------------------------------------------
# Whether that pane is the registered control pane
# --------------------------------------------------------------------------


def _registered(pid: int, start: int, uid: int, mode: str = "process") -> dict:
    return {
        "authority_mode": mode,
        "assignment": {"process_pid": pid, "process_start_time": start, "process_uid": uid},
    }


def _run_boundary(tmp: Path, caller: ph.CallerIdentity, listing, workflow=WORKFLOW, live=True):
    entries = {caller.pid: (1, caller.start_time if live else caller.start_time + 1,
                            "bash", caller.uid)}
    proc = _proc(tmp, entries)
    board_get, asked = _board(workflow, listing)
    return ph.require_registered_control_caller(
        "mefp", board_get=board_get, board_url="http://board", caller=caller, proc_root=proc,
    ), asked


def test_the_registered_control_pane_is_allowed() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-ok.") as raw:
        caller = ph.CallerIdentity(300, 333, 1002)
        role, asked = _run_boundary(Path(raw), caller, _registered(300, 333, 1002))
    check(role == "director", f"the control role is returned: {role}")
    check("/api/workflow" in asked, f"the workflow decided who that is: {asked}")


def test_a_sibling_role_under_the_same_account_is_refused() -> None:
    """The case polkit structurally cannot catch: same uid, different pane."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-sibling.") as raw:
        caller = ph.CallerIdentity(999, 555, 1002)  # Main's pane, same tenant account
        try:
            _run_boundary(Path(raw), caller, _registered(300, 333, 1002))
            check(False, "a sibling role was accepted")
        except ph.Refused as exc:
            check("only director's registered process" in str(exc), str(exc))
            check("999" in str(exc) and "300" in str(exc),
                  f"naming both processes, so the refusal is checkable: {exc}")
    check(True, "the uid being identical did not help it")


def test_a_reused_pid_with_a_different_start_time_is_refused() -> None:
    """A pid is reused; a start time is not. That is what makes this fail closed."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-reuse.") as raw:
        caller = ph.CallerIdentity(300, 999999, 1002)
        try:
            _run_boundary(Path(raw), caller, _registered(300, 333, 1002))
            check(False, "a reused pid was accepted")
        except ph.Refused as exc:
            check("registered process" in str(exc), str(exc))


def test_a_replaced_session_loses_the_authority_it_inherited() -> None:
    """The row outlives the process. If it is gone, so is the authority."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-dead.") as raw:
        caller = ph.CallerIdentity(300, 333, 1002)
        try:
            _run_boundary(Path(raw), caller, _registered(300, 333, 1002), live=False)
            check(False, "a dead registered process was accepted")
        except ph.Refused as exc:
            check("no longer running" in str(exc), str(exc))


def test_a_board_not_on_process_authority_refuses_outright() -> None:
    """Without process authority there is only a uid, which proves nothing here."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-mode.") as raw:
        caller = ph.CallerIdentity(300, 333, 1002)
        try:
            _run_boundary(Path(raw), caller, _registered(300, 333, 1002, mode="uid"))
            check(False, "a uid-authority board was accepted")
        except ph.Refused as exc:
            check("shared uid alone" in str(exc), str(exc))


def test_a_board_with_no_registered_runtime_or_no_control_role_refuses() -> None:
    import tempfile

    caller = ph.CallerIdentity(300, 333, 1002)
    with tempfile.TemporaryDirectory(prefix="syrd112-none.") as raw:
        try:
            _run_boundary(Path(raw), caller, None)
            check(False, "a board with no runtime was accepted")
        except ph.Refused as exc:
            check("no registered runtime" in str(exc), str(exc))

        try:
            _run_boundary(Path(raw), caller, _registered(300, 333, 1002),
                          workflow={"document": {"roles": [{"name": "main"}]}})
            check(False, "a board with no control role was accepted")
        except ph.Refused as exc:
            check("no role with control authority" in str(exc), str(exc))
            check("is not permission" in str(exc), f"and says so plainly: {exc}")


def test_the_controller_is_found_by_capability_and_never_by_name() -> None:
    """The rule this got wrong once, pinned.

    There is deliberately no fallback to the name `director`: a document that
    is absent, empty or malformed would otherwise mean "whatever registered
    itself under a familiar name may act privileged" (SYRD-49).
    """
    import tempfile

    caller = ph.CallerIdentity(300, 333, 1002)
    listing = _registered(300, 333, 1002)

    with tempfile.TemporaryDirectory(prefix="syrd112-capability.") as raw:
        tmp = Path(raw)
        # A controller not called "director" still holds the authority.
        role, _asked = _run_boundary(tmp, caller, listing, workflow={"document": {"roles": [
            {"name": "conductor", "active": True, "capabilities": CONTROL},
        ]}})
        check(role == "conductor", f"named by capability: {role}")

        # A role named director that does not hold the capabilities does not.
        for document, why in (
            ({"roles": [{"name": "director", "active": True, "capabilities": ["merge"]}]},
             "only half the capabilities"),
            ({"roles": [{"name": "director", "active": False, "capabilities": CONTROL}]},
             "not active"),
            ({"roles": [{"name": "director"}]}, "no capabilities at all"),
            ({"roles": []}, "no roles"),
            ({}, "an empty document"),
            ({"roles": "director"}, "a malformed document"),
        ):
            try:
                _run_boundary(tmp, caller, listing, workflow={"document": document})
                check(False, f"{why} was treated as control authority")
            except ph.Refused as exc:
                check("no role with control authority" in str(exc), f"{why}: {exc}")

        # Two controllers: the first by name, deterministically, never whichever
        # the board happened to list first.
        role, _asked = _run_boundary(tmp, caller, listing, workflow={"document": {"roles": [
            {"name": "zulu", "active": True, "capabilities": CONTROL},
            {"name": "alpha", "active": True, "capabilities": CONTROL},
        ]}})
        check(role == "alpha", f"sorted by name, so it cannot depend on ordering: {role}")


# --------------------------------------------------------------------------
# Which board it asks
# --------------------------------------------------------------------------


def test_the_board_comes_from_root_owned_registration_not_from_the_caller() -> None:
    """A caller that could name the board could name one that agrees with it."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="syrd112-registry.") as raw:
        tmp = Path(raw)
        registry = tmp / "projects"
        registry.mkdir()
        config = tmp / "mefp-config.json"
        config.write_text(json.dumps({"project": "mefp", "board_url": "http://real-board"}),
                          encoding="utf-8")
        (registry / "mefp.json").write_text(
            json.dumps({"schema": "switchyard.project-registry.v1", "slug": "mefp",
                        "config_path": str(config)}), encoding="utf-8")
        url = ph.board_url_for("mefp", registry_dir=registry)
        check(url == "http://real-board", f"the registered board: {url}")

        try:
            ph.board_url_for("not-a-tenant", registry_dir=registry)
            check(False, "an unregistered project was accepted")
        except ph.Refused as exc:
            check("not registered on this host" in str(exc), str(exc))

        config.write_text(json.dumps({"project": "mefp"}), encoding="utf-8")
        try:
            ph.board_url_for("mefp", registry_dir=registry)
            check(False, "a configuration with no board was accepted")
        except ph.Refused as exc:
            check("records no board URL" in str(exc), str(exc))

    import inspect

    signature = inspect.signature(ph.board_url_for)
    check(
        list(signature.parameters) == ["project", "registry_dir"],
        f"it takes a project and where to look, never a URL: {list(signature.parameters)}",
    )
    # And no reader either. A boundary whose read can be replaced is a boundary
    # whose read can be replaced with one that follows symlinks (SYRD-242).
    check(
        "load" not in signature.parameters,
        "the only read it makes is the no-follow one",
    )
    check(
        signature.parameters["registry_dir"].default == ph.DEFAULT_REGISTRY_DIR,
        "and the shipped default is the root-owned registry",
    )


# --------------------------------------------------------------------------
# What may be asked for
# --------------------------------------------------------------------------


def test_the_request_surface_takes_names_and_typed_values_only() -> None:
    accepted, values = ph.parse_request(["deploy-release", "project=mefp", f"commit={COMMIT}"])
    check(accepted.name == "deploy-release", accepted.name)
    check(values == {"project": "mefp", "commit": COMMIT}, f"{values}")

    for argv, why in (
        ([], "nothing"),
        (["rm"], "an uncatalogued action"),
        (["deploy-release", "--force", "project=mefp"], "an option"),
        (["deploy-release", "mefp"], "a bare positional"),
        (["deploy-release", "project=mefp", "project=syrd"], "a repeated key"),
        (["deploy-release", "project=mefp", f"commit={COMMIT}", "env=X=1"], "an extra value"),
        (["deploy-release", "project=../etc", f"commit={COMMIT}"], "a path as a project"),
        (["deploy-release", "project=mefp", "commit=HEAD"], "a ref instead of a commit"),
    ):
        try:
            ph.parse_request(argv)
            check(False, f"{why} was accepted: {argv}")
        except ph.Refused as exc:
            check(str(exc).strip() != "", f"{why} refused with a reason: {exc}")


# -- the durable record ------------------------------------------------------


class FakeAttempt:
    """A rollout attempt that records the ORDER it was used in."""

    def __init__(self, project, command, *, target_commit="", log=None, fail_open=False):
        self.project = project
        self.command = list(command)
        self.target_commit = target_commit
        self.attempt = "0001"
        self.closed = None
        self.log = log if log is not None else []
        self.fail_open = fail_open

    def open(self):
        if self.fail_open:
            raise OSError("read-only file system")
        self.log.append("open")
        return Path("/var/lib/switchyard/rollouts/mefp/0001")

    def close(self, *, status, exit_status, detail=""):
        self.log.append(f"close:{status}")
        self.closed = {"status": status, "exit_status": exit_status, "detail": detail}
        return self.closed


def _factory(log, **kwargs):
    def make(project, command, **extra):
        made = FakeAttempt(project, command, log=log, **kwargs, **extra)
        log.append("made")
        return made

    return make


class Completed:
    def __init__(self, returncode):
        self.returncode = returncode


def test_the_attempt_is_open_before_the_command_runs() -> None:
    """The whole reason the SYRD-102 failure was invisible."""
    log: list[str] = []
    action = pa.action_for("upgrade-tenant")

    def runner(command, *, timeout):
        log.append("ran")
        return Completed(0)

    code = ph.run_privileged_action(
        action, {"project": "mefp"}, project="mefp",
        attempt_factory=_factory(log), runner=runner,
        command_for=lambda name, values: ["/opt/switchyard/current/switchyard", "upgrade", "mefp"],
        print_func=lambda _line: None,
    )
    check(code == 0, f"{code}")
    check(log == ["made", "open", "ran", "close:succeeded"], f"the order is the requirement: {log}")


def test_a_failed_action_is_recorded_as_failed_with_its_exit_status() -> None:
    log: list[str] = []
    made: list = []

    def factory(project, command, **extra):
        attempt = FakeAttempt(project, command, log=log, **extra)
        made.append(attempt)
        return attempt

    code = ph.run_privileged_action(
        pa.action_for("upgrade-tenant"), {"project": "mefp"}, project="mefp",
        attempt_factory=factory, runner=lambda command, *, timeout: Completed(7),
        command_for=lambda name, values: ["/opt/switchyard/current/switchyard"],
        print_func=lambda _line: None,
    )
    check(code == 7, f"the exit status is passed through: {code}")
    check(made[0].closed["status"] == "failed", str(made[0].closed))
    check(made[0].closed["exit_status"] == 7, str(made[0].closed))


def test_a_timeout_closes_the_record_rather_than_leaving_it_open() -> None:
    """An abandoned run is a failed attempt with a duration, not an absent one."""
    log: list[str] = []
    made: list = []
    printed: list[str] = []

    def factory(project, command, **extra):
        attempt = FakeAttempt(project, command, log=log, **extra)
        made.append(attempt)
        return attempt

    def runner(command, *, timeout):
        raise TimeoutError("took too long")

    code = ph.run_privileged_action(
        pa.action_for("upgrade-tenant"), {"project": "mefp"}, project="mefp",
        attempt_factory=factory, runner=runner, timeout=900.0,
        command_for=lambda name, values: ["/opt/switchyard/current/switchyard"],
        print_func=printed.append,
    )
    check(code == 124, f"a timeout has its own exit status: {code}")
    check(
        made[0].closed is not None,
        "the attempt was closed rather than left open for somebody to find later",
    )
    check(made[0].closed["status"] == "failed", str(made[0].closed))
    check("900s" in made[0].closed["detail"], f"and the record says how long: {made[0].closed}")
    report = "\n".join(printed)
    check("did not complete within 900s" in report, report)
    check("recorded as failed" in report, f"and says the record is durable: {report}")
    check("nothing further will happen" in report, f"and that it stopped: {report}")


def test_an_unexpected_failure_still_closes_the_record() -> None:
    made: list = []

    def factory(project, command, **extra):
        attempt = FakeAttempt(project, command, log=[], **extra)
        made.append(attempt)
        return attempt

    def runner(command, *, timeout):
        raise MemoryError("out of memory")

    try:
        ph.run_privileged_action(
            pa.action_for("upgrade-tenant"), {"project": "mefp"}, project="mefp",
            attempt_factory=factory, runner=runner,
            command_for=lambda name, values: ["/opt/switchyard/current/switchyard"],
            print_func=lambda _line: None,
        )
        check(False, "the exception was swallowed")
    except MemoryError:
        check(made[0].closed is not None, "the record was closed on the way out")
        check(made[0].closed["status"] == "failed", str(made[0].closed))
        check("MemoryError" in made[0].closed["detail"], str(made[0].closed))


def test_a_journal_that_cannot_be_written_stops_the_action() -> None:
    """No durable record means no mutation. The record is not optional."""
    ran: list = []

    def runner(command, *, timeout):
        ran.append(command)
        return Completed(0)

    try:
        ph.run_privileged_action(
            pa.action_for("upgrade-tenant"), {"project": "mefp"}, project="mefp",
            attempt_factory=_factory([], fail_open=True), runner=runner,
            command_for=lambda name, values: ["/opt/switchyard/current/switchyard"],
            print_func=lambda _line: None,
        )
        check(False, "it ran without a record")
    except OSError as exc:
        check("read-only" in str(exc), str(exc))
    check(not ran, "and nothing privileged was run")


def test_a_decision_not_to_run_leaves_no_attempt_behind() -> None:
    """A refusal is not a run, so root's journal must not gain an entry for it."""
    from scripts.ticket_board import privileged_operations as po

    opened: list = []
    ran: list = []

    def factory(project, command, **extra):
        opened.append(command)
        return FakeAttempt(project, command, log=[], **extra)

    for failure in (
        po.NoTrustedSource("no trusted source on this host holds release"),
        po.NotExecutableYet("no bounded command activates a shared release"),
    ):
        def build(name, values, failure=failure):
            raise failure

        try:
            ph.run_privileged_action(
                pa.action_for("upgrade-tenant"), {"project": "mefp"}, project="mefp",
                attempt_factory=factory, runner=lambda c, *, timeout: ran.append(c),
                command_for=build, print_func=lambda _line: None,
            )
            check(False, f"{type(failure).__name__} did not refuse")
        except ph.Refused as exc:
            check(str(failure) in str(exc), str(exc))
    check(not opened, "no attempt was opened for a decision that was never a run")
    check(not ran, "and nothing ran")


def test_a_host_wide_action_files_its_record_somewhere_that_is_not_a_tenant() -> None:
    check(ph.journal_project({"project": "mefp"}) == "mefp", "a tenant action files under its tenant")
    check(
        ph.journal_project({"commit": "a" * 40}) == ph.HOST_JOURNAL_PROJECT,
        f"a host-wide one does not: {ph.journal_project({})}",
    )
    try:
        pa.action_for("upgrade-tenant").validate({"project": ph.HOST_JOURNAL_PROJECT})
        check(False, "a project could shadow the host journal")
    except pa.ArgumentError:
        check(True, "and no project slug can collide with it")


# -- the installed entry point ----------------------------------------------


class swapped:
    """Replace module attributes for the duration of a case, then put them back.

    The real `main` is driven, not a reimplementation of it: these are the
    boundaries it reaches out through, and everything between them is the code
    that actually ships.
    """

    def __init__(self, module, **replacements):
        self.module = module
        self.replacements = replacements

    def __enter__(self):
        self.previous = {name: getattr(self.module, name) for name in self.replacements}
        for name, value in self.replacements.items():
            setattr(self.module, name, value)
        return self

    def __exit__(self, *_exc):
        for name, value in self.previous.items():
            setattr(self.module, name, value)
        return False


def test_main_refuses_a_drifted_installation_before_it_asks_who_called() -> None:
    """A mutation that skipped this check survived every other case.

    polkit's `exec.path` names a path, not a hash, so it will run a helper
    whose mode has drifted. The helper checks its own installation for exactly
    that: not to defend against a rewritten helper, which could skip the check,
    but so that DRIFT -- a mode widened by a careless install, a package
    directory left group-writable -- fails closed.
    """
    from scripts.ticket_board import privileged_install

    looked_for_caller: list[str] = []

    def identity(*_a, **_k):
        looked_for_caller.append("asked")
        raise AssertionError("the caller was resolved despite a drifted install")

    errors: list[str] = []
    with swapped(
        ph,
        privileged_install=_StubInstall(["the helper is mode 0777"]),
        caller_identity=identity,
    ):
        import contextlib, io

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = ph.main(["upgrade-tenant", "project=mefp"])
        errors.append(buffer.getvalue())
    check(code == 1, f"it refuses: {code}")
    check(not looked_for_caller, "and stops before resolving the caller")
    report = "\n".join(errors)
    check("mode 0777" in report, report)
    check("not the one this release installs" in report, f"and says what is wrong: {report}")


class _StubInstall:
    def __init__(self, problems):
        self.problems = problems

    def verify_installation(self, *_a, **_k):
        return list(self.problems)


def test_main_refuses_an_uncatalogued_action_before_anything_else() -> None:
    import contextlib, io

    checked: list[str] = []

    buffer = io.StringIO()
    with swapped(ph, privileged_install=_StubInstall(["never reached"])):
        with contextlib.redirect_stderr(buffer):
            code = ph.main(["definitely-not-an-action", "project=mefp"])
    check(code == 1, f"{code}")
    report = buffer.getvalue()
    check("not a catalogued action" in report, report)
    check("never reached" not in report, f"the installation was not even consulted: {report}")


def test_main_runs_the_action_once_everything_has_been_proved() -> None:
    """The whole path, with only the outside world replaced."""
    import contextlib, io

    ran: list = []
    opened: list = []

    def attempt_factory(project, command, **kwargs):
        opened.append((project, list(command)))
        return FakeAttempt(project, command, log=[], **kwargs)

    def runner(command, *, timeout):
        ran.append((list(command), timeout))
        return Completed(0)

    def require(project, **kwargs):
        return "director"

    buffer = io.StringIO()
    with swapped(
        ph,
        privileged_install=_StubInstall([]),
        caller_identity=lambda *a, **k: ph.CallerIdentity(4242, 99, 1006),
        board_url_for=lambda project, **k: "http://127.0.0.1:1/",
        require_registered_control_caller=require,
        _attempt_factory=attempt_factory,
        _run_command=runner,
    ):
        with contextlib.redirect_stdout(buffer):
            code = ph.main(["upgrade-tenant", "project=mefp"])
    check(code == 0, f"{code}: {buffer.getvalue()}")
    check(opened and opened[0][0] == "mefp", f"the attempt is filed under the tenant: {opened}")
    check(
        ran and ran[0][0] == ["/opt/switchyard/current/switchyard", "upgrade", "mefp"],
        f"and the pinned launcher ran: {ran}",
    )
    check(ran[0][1] == ph.ACTION_TIMEOUT_SECONDS, f"bounded: {ran[0][1]}")


def test_main_refuses_a_caller_that_is_not_the_registered_control_pane() -> None:
    """The acceptance clause about a sibling role under the shared account."""
    import contextlib, io

    ran: list = []

    def require(project, **kwargs):
        raise ph.Refused(
            "only director's registered process may run a privileged action. This ran "
            "under process 5555 (started 12, uid 1006)"
        )

    buffer = io.StringIO()
    with swapped(
        ph,
        privileged_install=_StubInstall([]),
        caller_identity=lambda *a, **k: ph.CallerIdentity(5555, 12, 1006),
        board_url_for=lambda project, **k: "http://127.0.0.1:1/",
        require_registered_control_caller=require,
        _run_command=lambda command, *, timeout: ran.append(command),
    ):
        with contextlib.redirect_stderr(buffer):
            code = ph.main(["upgrade-tenant", "project=mefp"])
    check(code == 1, f"{code}")
    check(not ran, "and nothing privileged ran for a sibling role")
    check("registered process" in buffer.getvalue(), buffer.getvalue())


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("privileged_helper_boundary_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"privileged_helper_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
