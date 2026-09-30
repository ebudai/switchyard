#!/usr/bin/env python3
"""A runtime switch keeps the launcher's projection whole, and a failed one leaves one runtime.

SYRD-486 (syrd Main, claude -> codex), SYRD-447 (MEFP Main, codex -> claude),
SYRD-485 (MEFP Audit, claude -> codex) and SYRD-388 (syrd Audit, agy -> codex)
are one defect seen four times:

1. `set-role-runtime` rewrote the role's `cli` but not its `live_commands`, so
   the start verifier, which accepts only the listed names, called a healthy
   fresh session of the new runtime absent: "did not leave a live session";
2. the rollback undid the workflow with `expected_revision=0`, which the board
   refuses whenever a revision exists -- and the switch's own write had just
   made one: "workflow revision changed; reread before applying";
3. the projection had already been restored, so the config named the old
   runtime while the board declared the new one, and the old worker's restart
   was refused against that mismatch -- MEFP Audit ended with no session.

The suite next door never saw it: its board treats revision 0 as "any", and
its start replaces the launcher. These cases use a board with the real SQL's
revision rule -- itself checked against PostgreSQL below -- and the real start
path and verifier, which read a real process tree. tmux is stood in for; the
process each start launches is a copy of `sleep` named after the CLI the
launcher config names at that moment, so no provider CLI ever runs.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from scripts import role_runtime, team_launcher  # noqa: E402

import role_runtime_test as base  # noqa: E402

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class StrictBoard(base.FakeBoard):
    """The board's workflow apply as the SQL has it: `expected_revision IS DISTINCT FROM actual`.

    Zero is not "any": it matches only a board with no revision at all. The
    real function is exercised against this rule in the PostgreSQL case.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Something else moving the workflow while the switch is in flight.
        self.bump_after_forward = False
        #: ...and, if set, changing this role's runtime as it does so.
        self.concurrent_runtime: tuple[str, str] | None = None

    def configure_workflow(self, document, *, expected_revision: int, dry_run: bool = False):
        self.applies.append((json.loads(json.dumps(document)), dry_run))
        if self.fail_apply and not dry_run:
            raise RuntimeError(self.fail_apply)
        if expected_revision != self.revision:
            raise RuntimeError("workflow revision changed; reread before applying")
        if dry_run:
            return {"revision": self.revision + 1, "dry_run": True}
        self.document = json.loads(json.dumps(document))
        self.revision += 1
        applied = self.revision
        if self.bump_after_forward:
            # Another writer lands after this apply returned: the switch holds
            # the revision it made, and the board has already moved past it.
            self.bump_after_forward = False
            self.revision += 1
            if self.concurrent_runtime is not None:
                role, runtime = self.concurrent_runtime
                for entry in self.document["roles"]:
                    if entry["name"] == role:
                        entry["runtime"] = runtime
        return {"revision": applied}


class Host:
    """tmux stood in for; every started session is a real process named after its CLI."""

    def __init__(self, tmp: Path, config_path: Path, *, live: tuple[str, ...] = ("audit",),
                 dies: set[str] = frozenset(), impostor: set[str] = frozenset()) -> None:
        self.tmp = tmp
        self.config_path = config_path
        self.bin = tmp / "standins"
        self.bin.mkdir()
        self.processes: dict[str, subprocess.Popen] = {}
        #: Every process ever started, whatever became of its session.
        self.started_processes: list[subprocess.Popen] = []
        self.dies = set(dies)          # runtimes whose process exits at once
        self.impostor = set(impostor)  # runtimes that come up under another name
        self.calls: list[list[str]] = []
        #: Sessions that are not a role's -- the presentation's display
        #: sessions. They exist once created; nothing runs in them here.
        self.other_sessions: set[str] = set()
        for role in live:
            self._launch(f"porter-{role}", self._configured_cli(role))

    def _configured_cli(self, role: str) -> str | None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        entry = next((item for item in raw["roles"] if item["role"] == role), None)
        return team_launcher._command_name(entry["cli"][0]) if entry else None

    def _launch(self, session: str, cli: str) -> None:
        name = f"{cli}-impostor" if cli in self.impostor else cli
        program = self.bin / name
        if not program.exists():
            shutil.copy2(shutil.which("sleep"), program)
        replaced = self.processes.pop(session, None)
        if replaced is not None and replaced.poll() is None:
            replaced.kill()  # nothing started here may outlive the case
            replaced.wait(timeout=10)
        self.processes[session] = subprocess.Popen(
            [str(program), "0" if cli in self.dies else "300"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.started_processes.append(self.processes[session])

    def _alive(self, session: str) -> bool:
        proc = self.processes.get(session)
        return proc is not None and proc.poll() is None

    def running(self, session: str) -> str:
        """The command name of what the session is running, or ""."""
        if not self._alive(session):
            return ""
        return Path(self.processes[session].args[0]).name

    def __call__(self, args, **_kwargs):
        command = [str(part) for part in args]
        self.calls.append(command)
        if command[:1] != ["tmux"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        verb = command[1]
        target = command[command.index("-t") + 1].removeprefix("=") if "-t" in command else ""
        session = target.split(":", 1)[0]
        if verb == "has-session":
            alive = self._alive(session) or session in self.other_sessions
            return subprocess.CompletedProcess(command, 0 if alive else 1)
        if verb == "kill-session":
            self.other_sessions.discard(session)
            proc = self.processes.pop(session, None)
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=10)
            return subprocess.CompletedProcess(command, 0)
        if verb == "new-session":
            session = command[command.index("-s") + 1]
            cli = self._configured_cli(session.removeprefix("porter-"))
            if cli is None:
                self.other_sessions.add(session)
            else:
                # What the launcher execs is what the config names right now.
                self._launch(session, cli)
            return subprocess.CompletedProcess(command, 0)
        if verb == "display-message":
            if command[-1] == "#{pane_pid}":
                pid = self.processes[session].pid if self._alive(session) else 0
                return subprocess.CompletedProcess(command, 0 if pid else 1, stdout=f"{pid}\n")
            if command[-1] == "#{pane_current_command}":
                return subprocess.CompletedProcess(command, 0, stdout=f"{self.running(session)}\n")
            # Anything else asked of a pane -- the presentation's slot queries --
            # answers as the neighbouring suite's runner does.
            return subprocess.CompletedProcess(command, 0, stdout="0\n")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def close(self) -> None:
        for proc in self.started_processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


def _tenant(tmp: Path, *, live_commands: list[str] | None = ("claude",), cli: str = "claude") -> Path:
    config_path = base._write_config(tmp)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    for entry in raw["roles"]:
        if entry["role"] == "audit":
            entry["cli"] = [cli]
            if live_commands is None:
                entry.pop("live_commands", None)
            else:
                entry["live_commands"] = list(live_commands)
    config_path.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


def _entry(config_path: Path, role: str = "audit") -> dict:
    return next(item for item in json.loads(config_path.read_text(encoding="utf-8"))["roles"] if item["role"] == role)


def _switch(config_path: Path, host: Host, board: StrictBoard, runtime: str, **kwargs):
    said: list[str] = []
    config = team_launcher.load_project_config("porter", config_path)
    # Provider readiness (installed, signed in, folder trusted) has its own
    # suites; the fixture's worktrees are not a provider's to trust.
    readiness = role_runtime._readiness_blockers
    role_runtime._readiness_blockers = base._ready
    try:
        result = role_runtime.switch_role_runtime(
            config, config_path=config_path, role_name="audit", runtime=runtime,
            environ=base.DIRECTOR_ENV, runner=host, client=board, workflow_reader=board.reader,
            pane_state_dir=config_path.parent / "pane-state", print_func=said.append,
            busy_check=lambda *a, **k: False, **kwargs,
        )
        return result, "", said
    except SystemExit as exc:
        return None, str(exc), said
    finally:
        role_runtime._readiness_blockers = readiness


def _sandbox(case):
    def run() -> None:
        with tempfile.TemporaryDirectory(prefix="syrd486.") as tmp:
            previous = os.environ.get("HOME")
            os.environ["HOME"] = tmp  # nothing here may reach the real home
            try:
                case(Path(tmp))
            finally:
                if previous is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = previous
    run.__name__ = case.__name__
    run.__doc__ = case.__doc__
    return run


def _journal(config_path: Path) -> Path:
    config = team_launcher.load_project_config("porter", config_path)
    return role_runtime.journal_path_for(config, config_path=config_path, role_name="audit")


# --------------------------------------------------------------------------
# The switch itself
# --------------------------------------------------------------------------


@_sandbox
def test_a_switch_moves_live_commands_with_the_runtime_and_proves_the_new_worker(tmp: Path) -> None:
    """The observed case: an explicit stale list, the real start path and verifier."""
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path)
    board = StrictBoard()
    try:
        check(host.running("porter-audit") == "claude", "fixture: audit runs claude")
        result, refused, said = _switch(config_path, host, board, "codex")
        check(result is not None, (refused, said))
        check(result.live_session_changed, result)
        check(_entry(config_path)["cli"] == ["codex"], _entry(config_path))
        check(_entry(config_path)["live_commands"] == ["codex"], _entry(config_path))
        check(board.runtime_of("audit") == "codex", board.document)
        check(host.running("porter-audit") == "codex", host.running("porter-audit"))
        check(not _journal(config_path).exists(), "a clean switch leaves no journal")
    finally:
        host.close()


@_sandbox
def test_a_list_a_previous_switch_left_stale_is_repaired_and_custom_names_are_kept(tmp: Path) -> None:
    """MEFP after SYRD-447: cli=codex, live_commands=[claude], plus a custom name."""
    config_path = _tenant(tmp, cli="codex", live_commands=["claude", "audit-wrapper"])
    host = Host(tmp, config_path)
    board = StrictBoard()
    board.document["roles"][1]["runtime"] = "codex"
    try:
        result, refused, said = _switch(config_path, host, board, "agy")
        check(result is not None, (refused, said))
        check(_entry(config_path)["live_commands"] == ["agy", "audit-wrapper"], _entry(config_path))
    finally:
        host.close()


@_sandbox
def test_no_list_stays_no_list(tmp: Path) -> None:
    config_path = _tenant(tmp, live_commands=None)
    host = Host(tmp, config_path)
    try:
        result, refused, said = _switch(config_path, host, StrictBoard(), "codex")
        check(result is not None, (refused, said))
        check("live_commands" not in _entry(config_path), _entry(config_path))
    finally:
        host.close()


@_sandbox
def test_preflight_refuses_a_switch_it_could_not_confirm_before_anything_stops(tmp: Path) -> None:
    """The guard behind the fix: a projection the verifier cannot recognise stops at preflight."""
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path)
    board = StrictBoard()
    try:
        original = role_runtime.project_role_runtime

        def leaves_live_commands(entry, *, runtime, model=None):
            kept = copy.deepcopy(entry.get("live_commands"))
            original(entry, runtime=runtime, model=model)
            entry["live_commands"] = kept

        role_runtime.project_role_runtime = leaves_live_commands
        try:
            result, refused, said = _switch(config_path, host, board, "codex")
        finally:
            role_runtime.project_role_runtime = original
    finally:
        host.close()
    check(result is None and "could not be confirmed after the switch" in refused, (refused, said))
    check(not [c for c in host.calls if c[:2] == ["tmux", "kill-session"]], "the working session was stopped")
    check([dry for _doc, dry in board.applies] == [], f"the board was written: {board.applies}")
    check(_entry(config_path)["cli"] == ["claude"], "the config was changed")


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------


@_sandbox
def test_a_genuine_start_failure_rolls_back_the_board_the_projection_and_the_worker(tmp: Path) -> None:
    config_path = _tenant(tmp, live_commands=["claude"])
    before = config_path.read_text(encoding="utf-8")
    host = Host(tmp, config_path, dies={"codex"})
    board = StrictBoard()
    start_revision = board.revision
    try:
        result, refused, said = _switch(config_path, host, board, "codex")
        check(result is None and "still runs claude; the switch was undone" in refused, (refused, said))
        check(not any("rollback of" in line for line in said), said)
        check(board.runtime_of("audit") == "claude", board.document)
        check(board.revision == start_revision + 2, "forward write, then its undo on top of it")
        check(config_path.read_text(encoding="utf-8") == before, "the projection is back byte for byte")
        check(host.running("porter-audit") == "claude", f"the old worker is back: {host.running('porter-audit')}")
        check(not _journal(config_path).exists(), "a clean rollback leaves no journal")
    finally:
        host.close()


@_sandbox
def test_a_live_attempt_the_check_rejected_is_replaced_not_kept_on_rollback(tmp: Path) -> None:
    """SYRD-447's shape: the new runtime is up, its check says no, and it must not survive."""
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path, impostor={"codex"})
    board = StrictBoard()
    try:
        result, refused, said = _switch(config_path, host, board, "codex")
        check(result is None and "still runs claude" in refused, (refused, said))
        check(host.running("porter-audit") == "claude",
              f"the old runtime owns the pane again: {host.running('porter-audit')}")
    finally:
        host.close()


@_sandbox
def test_a_workflow_undo_the_board_refuses_leaves_one_runtime_and_a_recovery(tmp: Path) -> None:
    """SYRD-485's shape, bounded: the board moved on, so nothing reverts half-way."""
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path, dies={"codex"})
    board = StrictBoard()
    board.bump_after_forward = True
    try:
        result, refused, said = _switch(config_path, host, board, "codex")
        check(result is None and "left between runtimes" in refused, (refused, said))
        check(any("rollback of workflow failed" in line for line in said), said)
        # One runtime, declared by both: the board and the launcher agree.
        check(board.runtime_of("audit") == "codex", board.document)
        check(_entry(config_path)["cli"] == ["codex"] and _entry(config_path)["live_commands"] == ["codex"],
              _entry(config_path))
        # Nothing was restarted under the runtime the board no longer declares.
        starts = [c for c in host.calls if c[:2] == ["tmux", "new-session"]]
        check(len(starts) == 1, f"only the forward attempt started: {starts}")
        journal = json.loads(_journal(config_path).read_text(encoding="utf-8"))
        check(journal["applied_workflow_revision"] == 8, journal)
        check(journal["rollback_problems"] and "workflow" in journal["rollback_problems"][0], journal)
        # What the three sources showed -- the board is at the other writer's revision.
        check(journal["observed"] == {"board_revision": 9, "board_runtime": "codex",
                                      "launcher_runtime": "codex", "launcher_live_commands": ["codex"],
                                      "worker_live": False}, journal["observed"])
        check("switchyard present porter recover audit" in journal["recovery"], journal["recovery"])
        check("switchyard present porter recover audit" in refused, refused)
    finally:
        host.close()


def _remedy(recovery: str, *, cli: str) -> str:
    """The `set-role-runtime ... --cli <cli>` command the recovery printed, parsed back."""
    import shlex

    commands = [part.split("`", 1)[0] for part in recovery.split("`switchyard ")[1:]]
    for command in commands:
        argv = shlex.split(command)
        if argv[:1] == ["set-role-runtime"] and argv[argv.index("--cli") + 1] == cli:
            check(argv[1:3] == ["porter", "audit"], argv)
            return cli
    raise AssertionError(f"no `set-role-runtime ... --cli {cli}` in: {recovery}")


@_sandbox
def test_a_projection_that_never_landed_is_reported_as_it_is_and_the_worker_kept(tmp: Path) -> None:
    """The Director's probe: the forward projection write fails and the board refuses the undo."""
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path)
    board = StrictBoard()
    board.bump_after_forward = True
    original_pid = host.processes["porter-audit"].pid
    write = role_runtime._write_runtime_projection

    def disk_full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    try:
        role_runtime._write_runtime_projection = disk_full
        try:
            result, refused, said = _switch(config_path, host, board, "codex")
        finally:
            role_runtime._write_runtime_projection = write
        check(result is None and "left between runtimes" in refused, (refused, said))
        journal = json.loads(_journal(config_path).read_text(encoding="utf-8"))
        check(journal["steps_applied"] == ["workflow"], journal["steps_applied"])
        # Observed, not inferred: the board moved on to 9, the launcher never changed.
        check(journal["observed"] == {"board_revision": 9, "board_runtime": "codex",
                                      "launcher_runtime": "claude", "launcher_live_commands": ["claude"],
                                      "worker_live": True, "worker_command": "claude"}, journal["observed"])
        check("They disagree" in journal["recovery"], journal["recovery"])
        check("both declare" not in journal["recovery"], journal["recovery"])
        # The intact original worker was left exactly as it was.
        check(host.processes["porter-audit"].pid == original_pid and host.running("porter-audit") == "claude",
              "the original worker was touched")
        check(not [c for c in host.calls if c[:2] in (["tmux", "kill-session"], ["tmux", "new-session"])],
              "a worker was stopped or started")
        # The printed remedy runs, and leaves one runtime everywhere.
        runtime = _remedy(journal["recovery"], cli="codex")
        result, refused, said = _switch(config_path, host, board, runtime)
        check(result is not None, (refused, said))
        check(board.runtime_of("audit") == "codex" and _entry(config_path)["cli"] == ["codex"]
              and host.running("porter-audit") == "codex", (board.document, _entry(config_path)))
    finally:
        host.close()


@_sandbox
def test_a_runtime_another_writer_chose_is_what_the_recovery_names(tmp: Path) -> None:
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path, dies={"codex"})
    board = StrictBoard()
    board.bump_after_forward = True
    board.concurrent_runtime = ("audit", "agy")
    try:
        result, refused, said = _switch(config_path, host, board, "codex")
        check(result is None and "left between runtimes" in refused, (refused, said))
        journal = json.loads(_journal(config_path).read_text(encoding="utf-8"))
        check(journal["observed"]["board_runtime"] == "agy" and journal["observed"]["launcher_runtime"] == "codex",
              journal["observed"])
        check("--cli agy" in journal["recovery"] and "--cli codex" in journal["recovery"], journal["recovery"])
        check(f"switchyard present porter recover audit" in journal["recovery"], "the stopped worker's way up")
        runtime = _remedy(journal["recovery"], cli="agy")
        result, refused, said = _switch(config_path, host, board, runtime)
        check(result is not None, (refused, said))
        check(board.runtime_of("audit") == "agy" and _entry(config_path)["cli"] == ["agy"]
              and _entry(config_path)["live_commands"] == ["agy"], (board.document, _entry(config_path)))
    finally:
        host.close()


@_sandbox
def test_a_projection_that_cannot_be_restored_stops_the_rollback_before_the_worker(tmp: Path) -> None:
    config_path = _tenant(tmp, live_commands=["claude"])
    host = Host(tmp, config_path, dies={"codex"})
    board = StrictBoard()

    def read_only(_journal):
        raise PermissionError(13, "Permission denied")

    try:
        restore = role_runtime._restore_projection
        role_runtime._restore_projection = read_only
        try:
            result, refused, said = _switch(config_path, host, board, "codex")
        finally:
            role_runtime._restore_projection = restore
        check(result is None and "left between runtimes" in refused, (refused, said))
        check(any("rollback of projection failed" in line for line in said), said)
        # The board came back; the worker was not started from the switch's config.
        starts = [c for c in host.calls if c[:2] == ["tmux", "new-session"]]
        check(len(starts) == 1, f"only the forward attempt started: {starts}")
        journal = json.loads(_journal(config_path).read_text(encoding="utf-8"))
        check(journal["observed"] == {"board_revision": 9, "board_runtime": "claude",
                                      "launcher_runtime": "codex", "launcher_live_commands": ["codex"],
                                      "worker_live": False}, journal["observed"])
        check("projection" in journal["rollback_problems"][0], journal["rollback_problems"])
        # The board undo did land, so the text must not claim nothing changed.
        check("rollback stopped at the step that failed" in journal["recovery"]
              and "nothing further" not in journal["recovery"], journal["recovery"])
        runtime = _remedy(journal["recovery"], cli="claude")
        result, refused, said = _switch(config_path, host, board, runtime)
        check(result is not None, (refused, said))
        check(_entry(config_path)["cli"] == ["claude"] and _entry(config_path)["live_commands"] == ["claude"]
              and board.runtime_of("audit") == "claude", _entry(config_path))
    finally:
        host.close()


@_sandbox
def test_the_recorded_revision_is_the_one_the_board_accepts(tmp: Path) -> None:
    """Against the real board: a forward apply returns its revision; 0 is refused; that one is not."""
    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster

    document = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8"))
    document = {**document, "project": "cerulean", "reassign": {}, "remove_stages": []}
    with temporary_cluster(prefix="syrd486-", shutdown="immediate") as cluster:
        admin = t.conninfo(cluster.socket_dir, cluster.port, "syrd486")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "syrd486"])
        t.psql(admin, t.SCHEMA_PATH.read_text())
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(admin, t.RBAC_PATH.read_text())
        app = t.TicketBoardApp(
            cluster.root / "frames", cluster.root / "assets", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, "syrd486", t.SERVICE_ROLE),
        )
        # Read the way the SQL itself decides it.
        def revision() -> int:
            return int(t.psql(admin, "SELECT coalesce(max(revision),0) FROM ticket_board.workflow_configuration;").strip())

        def runtime_of(role: str) -> str:
            workflow = app.workflow_document()
            document = workflow.get("document", workflow)
            return next(entry["runtime"] for entry in document["roles"] if entry["name"] == role)

        apply = lambda doc, expected: app.apply_workflow(doc, expected_revision=expected, dry_run=False,
                                                         caller_role="director")
        apply(document, revision())
        before = revision()
        check(runtime_of("audit") == "claude", "fixture: audit runs claude")
        # The switch's forward write, built by the switch's own helper.
        switched = role_runtime.document_with_runtime(document, role="audit", runtime="codex")
        forward = apply(switched, before)
        check(forward["revision"] == before + 1 == revision(), (forward["revision"], before, revision()))
        check(runtime_of("audit") == "codex", "the forward write took")
        # What the rollback used to send, refused by the board itself.
        try:
            apply(document, 0)
        except Exception as exc:  # the board's own refusal
            check("workflow revision changed" in str(exc), exc)
        else:
            raise AssertionError("revision 0 was accepted over an existing revision")
        check(runtime_of("audit") == "codex", "and it changed nothing")
        # What it sends now: the revision the forward write returned.
        undo = apply(document, forward["revision"])
        check(undo["revision"] == forward["revision"] + 1, undo["revision"])
        check(runtime_of("audit") == "claude", "the undo restored the previous runtime")


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"role_runtime_consistency_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
